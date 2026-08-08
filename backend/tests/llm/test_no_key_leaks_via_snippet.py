"""The one path in the LLM adapters that quotes what came back, and therefore leaks.

``tests/llm/test_no_key_leaks.py`` covers the paths that *translate*: an SDK error
in, a domain error out, nothing quoted. This file covers the one path that quotes
on purpose -- the excerpt of a body that is not JSON, which is how an operator
diagnoses a gateway -- and it exists because the security review found that
excerpt taken **without** the key to remove:

* ``infra/llm/http.py`` excerpted a non-JSON response body with no ``secrets=``;
* ``infra/llm/payloads.py`` excerpted a non-JSON model answer with no ``secrets=``.

The exploit is not theoretical. ``base_url`` is household-supplied on the Ollama
topology, and an OpenAI-compatible gateway that answers ``200`` with a proxy error
page echoing ``Authorization`` -- a captive portal, a corporate proxy, a
misconfigured reverse proxy -- puts the household's key into a domain error, and
from there into a JSON log line.

Two shapes of key are driven through every case, and the distinction is the point:

* a **Mistral AI** key, 32 bare alphanumerics with no vendor prefix, which no
  pattern recognised before this change (Mistral is a first-rank provider here);
* an **opaque gateway token**, which no pattern will ever recognise, because it
  has no published shape at all. Only the literal ``secrets=`` path catches it.
"""

from __future__ import annotations

import json
import logging
import secrets as secretslib
import string

import httpx
import pytest

from chaudron.domain.llm_ports import (
    LlmError,
    ProviderCapabilities,
    ProviderContext,
    ProviderResponseInvalid,
    RecipeRequest,
)
from chaudron.infra.llm.base import Completion, CompletionRequest, ModelRecipeGenerator
from chaudron.infra.llm.gemini_provider import build_gemini_client
from chaudron.infra.llm.http import GuardedHttpClient
from chaudron.infra.llm.openai_compatible import (
    MISTRAL_PROVIDER_CODE,
    ChatCompletionsTransport,
    build_chat_client,
    mistral_capabilities,
)
from chaudron.infra.llm.payloads import read_recipes
from chaudron.infra.llm.settings import LlmSettings
from chaudron.infra.logging import JsonFormatter
from chaudron.infra.redaction import redact

#: 32 bare alphanumerics: the shape Mistral AI issues, and the one that used to
#: pass every pattern untouched.
MISTRAL_KEY = "".join(secretslib.choice(string.ascii_letters + string.digits) for _ in range(32))

#: What a self-hosted gateway hands out. Lower case, hyphenated, short enough that
#: no shape-based rule could ever claim it -- which is the whole argument for
#: removing the credential by literal match rather than by pattern.
OPAQUE_KEY = "gw-local-token-hunter2-fridge"

KEYS = {"mistral_shaped": MISTRAL_KEY, "opaque_gateway": OPAQUE_KEY}

_SETTINGS = LlmSettings()
_CONTEXT = ProviderContext("mistral", "mistral-small-latest", None)
_FORMATTER = JsonFormatter()


def _proxy_page(request: httpx.Request) -> httpx.Response:
    """A 200 carrying HTML that quotes the credential the request just sent.

    Exactly what a captive portal or a proxy error page does, and the reason this
    excerpt is dangerous: the status says success, so nothing upstream treats it
    as a failure until the body fails to parse.

    The credential is echoed **bare**, with the ``Bearer`` prefix stripped. That is
    deliberate: with the prefix, the existing ``Bearer <token>`` pattern would
    scrub it and every assertion below would pass without the literal-secret path
    doing anything at all. Bare is also what a proxy that parsed the header and
    reported the value it read would print.
    """
    sent = request.headers.get("Authorization", "").removeprefix("Bearer ")
    sent = sent or request.headers.get("x-goog-api-key", "")
    return httpx.Response(
        200,
        text=f"<html><body>Proxy authentication required<br>sent: {sent}</body></html>",
        headers={"Content-Type": "text/html"},
    )


def _assert_clean(error: BaseException, key: str) -> None:
    """The message, its repr, the chain behind it, and the log line it becomes."""
    for rendered in (str(error), repr(error)):
        assert key not in rendered
        assert redact(rendered) == rendered, "nothing key-shaped should need scrubbing"

    cause = error.__cause__
    assert cause is None, "an explicit chain prints with the traceback"
    context = error.__context__
    assert context is None or error.__suppress_context__, (
        "an implicit chain must be suppressed, so a formatted traceback stops here"
    )
    if context is not None:
        assert key not in str(context)


# --------------------------------------------------------------------------- #
# The shapes the patterns recognise, and the ones they must not
# --------------------------------------------------------------------------- #


def test_the_unprefixed_vendor_key_shape_is_recognised() -> None:
    """Mistral AI is a first-rank provider and its key had no pattern at all.

    This is the second, independent layer: it catches a key at a site that does
    not know it is holding one -- a third-party log line, an SDK message nobody
    wrote.
    """
    cleaned = redact(f"provider rejected {MISTRAL_KEY}")

    assert MISTRAL_KEY not in cleaned
    assert "[redacted]" in cleaned


@pytest.mark.parametrize(
    "value",
    [
        "0123456789abcdef0123456789abcdef01234567",  # a 40-hex token (Todoist)
        "MZXW6YTBOIQGC3TEEBSXQZLMNRQXG2DP",  # 32 base32 characters: the feed secret
    ],
    ids=["hex_digest", "base32"],
)
def test_the_other_unprefixed_token_shapes_are_recognised(value: str) -> None:
    assert value not in redact(f"token {value} rejected")


@pytest.mark.parametrize(
    "value",
    [
        "019fcb5c-19e5-7762-9c41-12645bfaaaeb",  # a household identifier
        "M55HOZJYJDQ5ZKGDETFURCWBCQ",  # a calendar feed identifier: 26 base32
        "ck_inventory_lot_quantity_positive",  # a constraint name
        "mistral-small-latest",  # a model
        "chaudron.infra.repositories.inventory",  # a logger
    ],
    ids=["uuid", "feed_id", "constraint", "model", "logger"],
)
def test_identifiers_are_not_mistaken_for_credentials(value: str) -> None:
    """The other half of a length-based rule: what it must leave alone.

    A scrubber that blanked these would make every log line unreadable, and an
    unreadable log is the thing the redaction is meant to leave behind, not the
    thing it is meant to prevent. ``_``, ``-`` and ``.`` all break an alphanumeric
    run, which is why every identifier this application writes down survives.
    """
    assert redact(f"looking up {value}") == f"looking up {value}"


@pytest.mark.parametrize(
    "value",
    [
        "019fcb5c19e577629c4112653989090a",  # a household identifier, as bare hex
        "c460c942d13c62b114234d854c862d19afba5920af64dbe3952f814179e5a983",  # a sha256
    ],
    ids=["uuid_hex", "sha256"],
)
def test_the_separator_less_identifiers_are_blanked_and_that_is_the_trade(value: str) -> None:
    """These two used to survive, and the rule that spared them is the finding.

    The classifier demanded all three character classes of a long run, so a
    lower-case hex string was never a credential -- and neither was a Mistral key
    issued in lower-case hex, nor any of the lower-case alphanumeric keys other
    providers hand out. Covering those means length alone decides, and length alone
    cannot tell a digest from a token.

    The cost is bounded and stated rather than discovered: a UUID *written with its
    dashes* is untouched, and that is the form every log line in this application
    emits (``household_id``, ``request_id``, ``incident``). What is lost is a bare
    hex identifier nothing here logs, and ``receipt.image_sha256`` if anything ever
    did.
    """
    assert redact(f"looking up {value}") == "looking up [redacted]"


# --------------------------------------------------------------------------- #
# The transport: a body that is not JSON
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("shape", sorted(KEYS))
async def test_a_gateway_echoing_the_authorization_header_does_not_leak_it(shape: str) -> None:
    """The excerpt survives, the credential does not. Both halves matter.

    Dropping the excerpt would close the leak and lose the diagnostic that made
    quoting worth doing; keeping it unscrubbed is what the review found.
    """
    key = KEYS[shape]
    client = build_chat_client(
        MISTRAL_PROVIDER_CODE, key, _SETTINGS, transport=httpx.MockTransport(_proxy_page)
    )

    with pytest.raises(ProviderResponseInvalid) as raised:
        await client.post_json(
            "/v1/chat/completions", {}, context=_CONTEXT, provider_label="Mistral AI"
        )

    _assert_clean(raised.value, key)
    assert "Proxy authentication required" in str(raised.value)


@pytest.mark.parametrize("shape", sorted(KEYS))
async def test_the_same_holds_for_a_key_sent_in_a_custom_header(shape: str) -> None:
    """Gemini authenticates with ``x-goog-api-key``, not with ``Authorization``.

    The client reads its credentials off *its own headers*, so an adapter that
    invents a header name is covered without having to remember to declare it.
    """
    key = KEYS[shape]
    client = build_gemini_client(key, _SETTINGS, transport=httpx.MockTransport(_proxy_page))

    with pytest.raises(ProviderResponseInvalid) as raised:
        await client.post_json("/v1/models", {}, context=_CONTEXT, provider_label="Gemini")

    _assert_clean(raised.value, key)


@pytest.mark.parametrize("shape", sorted(KEYS))
async def test_the_excerpt_is_scrubbed_on_a_get_as_well(shape: str) -> None:
    """``get_json`` and ``post_json`` share one guarded path; both are asserted."""
    key = KEYS[shape]
    client = build_chat_client(
        MISTRAL_PROVIDER_CODE, key, _SETTINGS, transport=httpx.MockTransport(_proxy_page)
    )

    with pytest.raises(ProviderResponseInvalid) as raised:
        await client.get_json("/v1/models", context=_CONTEXT, provider_label="Mistral AI")

    _assert_clean(raised.value, key)


def test_a_client_without_credentials_holds_none() -> None:
    """Ollama sends no key, and an empty secret must not blank whole diagnostics."""
    client = GuardedHttpClient(httpx.URL("http://ollama:11434"), _SETTINGS)

    assert client._secrets == ()


# --------------------------------------------------------------------------- #
# The reader: a model answer that is not JSON
# --------------------------------------------------------------------------- #


class _EchoingTransport(ChatCompletionsTransport):
    """A transport whose "model answer" is the proxy page quoting its own key.

    The realistic shape of this: the gateway answers ``200 application/json`` with
    a completion whose *content* is its own error page, so the HTTP layer sees a
    valid response and the reader is the one that fails.
    """

    def __init__(self, key: str) -> None:
        super().__init__(
            build_chat_client(MISTRAL_PROVIDER_CODE, key, _SETTINGS),
            mistral_capabilities("mistral-small-latest"),
            api_key=key,
        )
        self._answer = f"<html>proxy denied, the key it read was {key}</html>"

    async def complete(self, request: CompletionRequest) -> Completion:
        return Completion(text=self._answer)


@pytest.mark.parametrize("shape", sorted(KEYS))
async def test_an_unparseable_model_answer_is_excerpted_without_the_key(shape: str) -> None:
    key = KEYS[shape]
    generator = ModelRecipeGenerator(_EchoingTransport(key))

    with pytest.raises(LlmError) as raised:
        await generator.suggest(RecipeRequest(inventory=(), max_suggestions=1))

    _assert_clean(raised.value, key)
    assert "proxy denied" in str(raised.value), "the useful part of the excerpt survives"


@pytest.mark.parametrize("shape", sorted(KEYS))
def test_the_reader_scrubs_what_it_is_told_to_scrub(shape: str) -> None:
    """The unit underneath, so a future caller that forgets ``secrets=`` is visible."""
    key = KEYS[shape]

    with pytest.raises(ProviderResponseInvalid) as raised:
        read_recipes(f"not json, key {key}", context=_CONTEXT, secrets=(key,))

    assert key not in str(raised.value)


def test_a_transport_without_a_key_reports_no_secrets() -> None:
    capabilities: ProviderCapabilities = mistral_capabilities("mistral-small-latest")
    transport = ChatCompletionsTransport(
        build_chat_client(MISTRAL_PROVIDER_CODE, "", _SETTINGS), capabilities
    )

    assert transport.secrets == ()


# --------------------------------------------------------------------------- #
# The log line, which is where a leaked key would actually come to rest
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("shape", sorted(KEYS))
async def test_the_failure_reaches_the_log_without_the_key(shape: str) -> None:
    """``api/routers/recipes.py`` logs the failure detail; this is that line.

    Two independent controls have to fail for a key to land here: the excerpt is
    built with the credential removed, *and* the formatter redacts what it writes.
    The opaque key proves the first one is doing the work, since no pattern in the
    formatter would recognise it.
    """
    key = KEYS[shape]
    client = build_chat_client(
        MISTRAL_PROVIDER_CODE, key, _SETTINGS, transport=httpx.MockTransport(_proxy_page)
    )

    with pytest.raises(ProviderResponseInvalid) as raised:
        await client.post_json("/v1/chat", {}, context=_CONTEXT, provider_label="Mistral AI")

    record = logging.LogRecord(
        "chaudron.api.routers.recipes",
        logging.WARNING,
        __file__,
        1,
        "recipe_provider_failure",
        None,
        None,
    )
    record.__dict__.update(
        {
            "error": type(raised.value).__name__,
            "provider": "mistral",
            "model": "mistral-small-latest",
            "detail": redact(str(raised.value)),
        }
    )
    line = _FORMATTER.format(record)

    assert key not in line
    assert json.loads(line)["provider"] == "mistral", "the line is still worth reading"
