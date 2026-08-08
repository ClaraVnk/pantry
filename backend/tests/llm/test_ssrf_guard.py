"""The Ollama URL is user-supplied and server-dialled: these are its guard rails.

ADR-0007 calls this out as a textbook SSRF primitive whose usual mitigation --
rejecting private ranges -- does not apply, because a co-located Ollama's address
*is* private. What replaces it is asserted here.
"""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import json
import socket
import uuid
from collections.abc import AsyncGenerator

import httpx
import pytest
from pydantic import SecretStr

from chaudron.config import Settings
from chaudron.domain.llm_ports import (
    CapabilitySource,
    ProviderCapabilities,
    ProviderContext,
    ProviderNotConfigured,
    RecipeRequest,
    RecipeSuggestion,
)
from chaudron.domain.models import LlmProviderMode
from chaudron.infra.crypto import CredentialCipher
from chaudron.infra.llm.doubles import valid_recipe_payload
from chaudron.infra.llm.factory import HouseholdProviderConfig
from chaudron.infra.llm.http import (
    GuardedHttpClient,
    HttpFailure,
    Resolver,
    validate_ollama_base_url,
)
from chaudron.infra.llm.ollama_provider import build_guarded_client, build_pinned_client
from chaudron.infra.llm.settings import (
    ALLOWED_HOSTS_ENV_VAR,
    DEFAULT_OLLAMA_PORT,
    LlmSettings,
    load_settings,
)
from chaudron.services.providers import provider_ports_builder

_ALLOWED = LlmSettings(ollama_allowed_hosts=frozenset({"ollama", "ollama.internal"}))
_CONTEXT = ProviderContext(provider="ollama", model="llama3.2")


def test_allowlisted_host_is_accepted() -> None:
    url = validate_ollama_base_url("http://ollama:11434", _ALLOWED)
    assert url.host == "ollama"
    assert url.port == 11434


@pytest.mark.parametrize(
    ("raw_url", "expected_fragment"),
    [
        # The whole point: a private address is legitimate *only* if allowlisted.
        ("http://169.254.169.254/latest/meta-data", "this instance permits"),
        ("http://localhost:11434", "this instance permits"),
        ("http://ollama.evil.example", "this instance permits"),
        # Scheme confusion is the other half of the classic SSRF toolbox.
        ("file:///etc/passwd", "http or https"),
        ("gopher://ollama:11434", "http or https"),
        # Credentials in a URL end up in logs and proxy traces.
        ("http://user:secret@ollama:11434", "credentials"),
        ("", "empty"),
    ],
)
def test_rejected_urls(raw_url: str, expected_fragment: str) -> None:
    with pytest.raises(ProviderNotConfigured) as raised:
        validate_ollama_base_url(raw_url, _ALLOWED)
    assert expected_fragment in str(raised.value)


def test_case_is_not_a_bypass() -> None:
    assert validate_ollama_base_url("http://OLLAMA:11434", _ALLOWED).host == "ollama"


def test_empty_allowlist_disables_the_mode_and_names_the_variable() -> None:
    """A closed default, with the remedy in the message rather than in a wiki page."""
    with pytest.raises(ProviderNotConfigured) as raised:
        validate_ollama_base_url("http://ollama:11434", LlmSettings())
    assert ALLOWED_HOSTS_ENV_VAR in str(raised.value)


async def test_dns_rebinding_is_refused_before_the_call() -> None:
    """A name allowlisted at save time must not be dialled once it moves.

    Without the second resolution, a host that answers ``10.89.0.7`` while the
    household is saving the form and ``169.254.169.254`` a second later would pass
    every check and reach the cloud metadata endpoint.
    """

    async def rebound_resolver(host: str, port: int) -> frozenset[str]:
        return frozenset({"169.254.169.254"})

    client = GuardedHttpClient(
        httpx.URL("http://ollama:11434"),
        _ALLOWED,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})),
        resolver=rebound_resolver,
        pinned_addresses=frozenset({"10.89.0.7"}),
    )
    with pytest.raises(ProviderNotConfigured) as raised:
        await client.post_json("/api/chat", {}, context=_CONTEXT, provider_label="Ollama")
    assert "rebinding" in str(raised.value)


async def test_redirects_are_not_followed() -> None:
    """A permitted host must not be able to bounce the server onto another one."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(302, headers={"Location": "http://169.254.169.254/"})

    client = GuardedHttpClient(
        httpx.URL("http://ollama:11434"),
        _ALLOWED,
        transport=httpx.MockTransport(handler),
    )
    result = await client.post_json("/api/chat", {}, context=_CONTEXT, provider_label="Ollama")
    assert not isinstance(result, dict)
    assert result.status == 302
    assert seen == ["http://ollama:11434/api/chat"]


async def test_oversized_response_is_abandoned() -> None:
    """A hostile or broken endpoint must not be able to exhaust memory."""
    from chaudron.domain.llm_ports import ProviderResponseInvalid

    settings = LlmSettings(ollama_allowed_hosts=frozenset({"ollama"}), max_response_bytes=64)
    client = GuardedHttpClient(
        httpx.URL("http://ollama:11434"),
        settings,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"x" * 5000)),
    )
    with pytest.raises(ProviderResponseInvalid) as raised:
        await client.post_json("/api/chat", {}, context=_CONTEXT, provider_label="Ollama")
    assert "ceiling" in str(raised.value)


# --------------------------------------------------------------------------- #
# AUD-005: the allowlist constrains the endpoint, not just the host
# --------------------------------------------------------------------------- #


def test_a_listed_host_on_an_unlisted_port_is_refused() -> None:
    """The finding, in one line.

    ``CHAUDRON_OLLAMA_ALLOWED_HOSTS`` used to be compared against ``url.host``,
    which never carries a port, so permitting ``ollama`` permitted ``ollama:22``
    and ``ollama:5432`` as well. On the co-located topology of ADR-0007 the allowed
    host is a Podman service name sitting on the same network as everything else.
    """
    settings = LlmSettings(ollama_allowed_hosts=frozenset({"ollama:11434"}))
    assert validate_ollama_base_url("http://ollama:11434", settings).port == 11434
    for port in (22, 80, 5432, 6379, 11435):
        with pytest.raises(ProviderNotConfigured):
            validate_ollama_base_url(f"http://ollama:{port}", settings)


def test_a_bare_entry_means_ollamas_port_and_only_it() -> None:
    """A permissive reading of an unqualified entry is what created the hole.

    ``ollama`` is what an operator writes when they mean their Ollama, so it means
    port 11434 -- never "any port", and not the scheme's default port either, which
    would refuse every legitimate URL while showing an allowlist that contains the
    host.
    """
    settings = LlmSettings(ollama_allowed_hosts=frozenset({"ollama"}))
    assert settings.allows_endpoint("ollama", DEFAULT_OLLAMA_PORT)
    assert not settings.allows_endpoint("ollama", 80)
    assert not settings.allows_endpoint("ollama", 443)
    assert not settings.allows_endpoint("ollama", 22)


def test_an_omitted_port_in_the_url_is_the_schemes_default_not_a_wildcard() -> None:
    """``http://ollama`` dials port 80, and is judged as port 80."""
    on_eighty = LlmSettings(ollama_allowed_hosts=frozenset({"ollama:80"}))
    assert validate_ollama_base_url("http://ollama", on_eighty).host == "ollama"
    assert validate_ollama_base_url("http://ollama:80", on_eighty).host == "ollama"
    with pytest.raises(ProviderNotConfigured):
        validate_ollama_base_url("https://ollama", on_eighty)

    on_https = LlmSettings(ollama_allowed_hosts=frozenset({"ollama:443"}))
    assert validate_ollama_base_url("https://ollama", on_https).host == "ollama"


def test_bracketed_ipv6_entries_are_understood() -> None:
    settings = LlmSettings(ollama_allowed_hosts=frozenset({"[::1]:11434"}))
    assert validate_ollama_base_url("http://[::1]:11434", settings).host == "::1"
    with pytest.raises(ProviderNotConfigured):
        validate_ollama_base_url("http://[::1]:22", settings)


def test_the_refusal_does_not_read_the_allowlist_back_to_the_household() -> None:
    """Listing it would hand out a map of the instance's private network (A10)."""
    settings = LlmSettings(ollama_allowed_hosts=frozenset({"ollama-internal:11434"}))
    with pytest.raises(ProviderNotConfigured) as raised:
        validate_ollama_base_url("http://ollama:11434", settings)
    message = str(raised.value)
    assert "ollama-internal" not in message
    assert ALLOWED_HOSTS_ENV_VAR in message


@pytest.mark.parametrize(
    "raw",
    ["ollama:notaport", "ollama:0", "ollama:70000", "::1", "[::1", "ollama:11434:9"],
)
def test_a_malformed_allowlist_entry_fails_the_instance_at_startup(raw: str) -> None:
    """Fail fast and name the variable, rather than drop the line and refuse later."""
    with pytest.raises(ValueError, match=ALLOWED_HOSTS_ENV_VAR):
        load_settings({ALLOWED_HOSTS_ENV_VAR: raw})


# --------------------------------------------------------------------------- #
# AUD-005, second half: the scanning oracle
# --------------------------------------------------------------------------- #


def _closed_port() -> int:
    """A port on the loopback that nothing is listening on."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
    return port


async def _listener(accepted: list[str], label: str, *, speak_http: bool) -> AsyncGenerator[int]:
    """A real server on a real loopback port, recording every connection it gets."""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        accepted.append(label)
        if speak_http:
            await reader.read(1)
            writer.write(b"HTTP/1.1 404 Not Found\r\ncontent-length: 0\r\n\r\n")
            await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    try:
        yield int(server.sockets[0].getsockname()[1])
    finally:
        server.close()
        await server.wait_closed()


async def test_a_port_scan_of_an_allowed_host_is_refused_without_opening_a_socket() -> None:
    """The proven attack, replayed against real sockets rather than a mock.

    The audit showed three distinguishable answers -- a listening HTTP server, a
    listening non-HTTP service, and a closed port -- reachable on any port of an
    allowed host, which is a port scanner with an oracle. Here all three exist for
    real on the loopback, and the assertion is twofold: nothing connects to them,
    and the caller cannot tell them apart from what it gets back.
    """
    accepted: list[str] = []
    silent = _listener(accepted, "non-http", speak_http=False)
    talking = _listener(accepted, "http", speak_http=True)
    silent_port = await anext(silent)
    talking_port = await anext(talking)
    closed_port = _closed_port()
    try:
        settings = LlmSettings(ollama_allowed_hosts=frozenset({"127.0.0.1:11434"}))
        outcomes: list[str] = []
        for port in (closed_port, silent_port, talking_port):
            with pytest.raises(ProviderNotConfigured) as raised:
                client = build_guarded_client(f"http://127.0.0.1:{port}", settings)
                await client.get_json("/api/version", context=_CONTEXT, provider_label="Ollama")
            outcomes.append(str(raised.value).replace(str(port), "<port>"))

        assert accepted == [], "the guard dialled a port the operator never declared"
        assert len(set(outcomes)) == 1, f"the answers still tell the ports apart: {outcomes}"
        assert "11434" not in outcomes[0]
    finally:
        await silent.aclose()
        await talking.aclose()


async def test_the_declared_endpoint_is_still_reached_for_real() -> None:
    """The control of the test above: without this, "nothing connected" proves nothing."""
    accepted: list[str] = []
    talking = _listener(accepted, "http", speak_http=True)
    port = await anext(talking)
    try:
        settings = LlmSettings(ollama_allowed_hosts=frozenset({f"127.0.0.1:{port}"}))
        client = build_guarded_client(f"http://127.0.0.1:{port}", settings)
        result = await client.get_json("/api/version", context=_CONTEXT, provider_label="Ollama")
    finally:
        await talking.aclose()

    assert accepted == ["http"]
    assert isinstance(result, HttpFailure)
    assert result.status == 404


# --------------------------------------------------------------------------- #
# AUD-028: the pinning check compares sets, and how
# --------------------------------------------------------------------------- #


async def test_an_answer_that_merely_overlaps_the_pinned_set_is_refused() -> None:
    """A hostile resolver answering ``[allowed, hostile]`` used to pass.

    The check required a non-empty intersection, so adding an address was enough to
    satisfy it; the connection could then leave for the added one. Requiring the
    current answer to be contained in the pinned set removes that.
    """

    async def widening_resolver(host: str, port: int) -> frozenset[str]:
        return frozenset({"10.89.0.7", "169.254.169.254"})

    client = GuardedHttpClient(
        httpx.URL("http://ollama:11434"),
        _ALLOWED,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})),
        resolver=widening_resolver,
        pinned_addresses=frozenset({"10.89.0.7"}),
    )
    with pytest.raises(ProviderNotConfigured) as raised:
        await client.post_json("/api/chat", {}, context=_CONTEXT, provider_label="Ollama")
    assert "rebinding" in str(raised.value)


# --------------------------------------------------------------------------- #
# AUD-028, the part that was missing: the check has to be *reached* in production
# --------------------------------------------------------------------------- #
#
# Everything above this line tested `GuardedHttpClient` with a pin handed to it by
# the test. That is how the control passed review for months while
# `provider_ports_builder` built its factory without a resolver and without a pin,
# so `assert_stable_resolution` returned on its first line for every real request:
# green tests, and nothing guarded.
#
# The tests below therefore start from the *production wiring function* and supply
# no pin at all. If the resolver stops being passed in `services/providers.py`, the
# pin is never taken, the comparison never happens, and the rebinding case reaches
# the socket -- which is exactly what these assert it must not.


async def _ollama_listener(connections: list[str]) -> AsyncGenerator[int]:
    """A real Ollama-shaped server on the loopback, recording every connection.

    Real sockets rather than a mock transport, for the same reason the port-scan
    test above uses them: "the guard refused" and "the guard dialled anyway and the
    mock happened to answer" are indistinguishable unless something genuinely
    listens and can report that nobody knocked.
    """
    body = json.dumps(
        {
            "model": "llama3.2",
            "message": {"role": "assistant", "content": valid_recipe_payload()},
            "prompt_eval_count": 1234,
            "eval_count": 56,
        }
    ).encode()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        connections.append("dialled")
        try:
            headers = await reader.readuntil(b"\r\n\r\n")
            # Drain the request body before answering: closing the connection while
            # httpx is still writing turns a clean assertion into a transport error.
            for line in headers.split(b"\r\n"):
                if line.lower().startswith(b"content-length:"):
                    await reader.readexactly(int(line.split(b":")[1]))
                    break
        except TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError:
            pass
        writer.write(
            b"HTTP/1.1 200 OK\r\ncontent-type: application/json\r\ncontent-length: "
            + str(len(body)).encode()
            + b"\r\n\r\n"
            + body
        )
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    try:
        yield int(server.sockets[0].getsockname()[1])
    finally:
        server.close()
        await server.wait_closed()


def _production_settings(port: int) -> Settings:
    """A real ``Settings``, built from literals so no environment leaks in."""
    return Settings(
        env="ci",
        database_url=SecretStr("postgresql+asyncpg://user:pass@localhost:5432/chaudron"),
        secret_key=SecretStr("a-secret-key-long-enough-to-pass-validation"),
        credential_encryption_key=SecretStr(base64.b64encode(b"0" * 32).decode()),
        cors_origins=["http://localhost:5173"],
        cors_allow_credentials=True,
        ollama_allowed_hosts=[f"127.0.0.1:{port}"],
    )


def _ollama_config(port: int) -> HouseholdProviderConfig:
    return HouseholdProviderConfig(
        household_id=uuid.UUID(int=7),
        mode=LlmProviderMode.OLLAMA,
        provider_code="ollama",
        model="llama3.2",
        base_url=f"http://127.0.0.1:{port}",
        probed_capabilities=ProviderCapabilities(
            provider="ollama",
            model="llama3.2",
            context_window=8192,
            supports_structured_output=True,
            supports_vision=False,
            supports_prompt_caching=False,
            source=CapabilitySource.PROBED,
            probed_at=dt.datetime(2026, 3, 3, tzinfo=dt.UTC),
        ),
    )


async def _generate(port: int, resolver: Resolver) -> list[RecipeSuggestion]:
    """Drive the production wiring end to end, from settings to a suggestion."""
    build = provider_ports_builder(
        _production_settings(port), CredentialCipher(b"0" * 32), resolver=resolver
    )
    generator = await build("ollama").recipe_generator(_ollama_config(port))
    return await generator.suggest(RecipeRequest(inventory=(), max_suggestions=1))


async def test_the_production_wiring_refuses_a_host_that_moves_between_the_two_lookups() -> None:
    """The regression guard for the branchement itself.

    Delete ``resolver=resolver`` from ``provider_ports_builder`` and this fails:
    with no resolver there is no pin, ``assert_stable_resolution`` returns
    immediately, and the rebound address is dialled instead of refused.
    """
    connections: list[str] = []
    listener = _ollama_listener(connections)
    port = await anext(listener)
    answers = iter([frozenset({"127.0.0.1"}), frozenset({"169.254.169.254"})])

    async def rebinding_resolver(host: str, port_: int) -> frozenset[str]:
        return next(answers)

    try:
        with pytest.raises(ProviderNotConfigured) as raised:
            await _generate(port, rebinding_resolver)
    finally:
        await listener.aclose()

    assert "rebinding" in str(raised.value)
    assert connections == [], "the guard dialled a host that had moved under it"


async def test_the_production_wiring_still_reaches_a_host_that_stays_put() -> None:
    """The control. Without it, the test above passes for a guard that refuses everything."""
    connections: list[str] = []
    listener = _ollama_listener(connections)
    port = await anext(listener)

    async def stable_resolver(host: str, port_: int) -> frozenset[str]:
        return frozenset({"127.0.0.1"})

    try:
        suggestions = await _generate(port, stable_resolver)
    finally:
        await listener.aclose()

    assert connections == ["dialled"]
    assert suggestions[0].title == "Gratin de courgettes"


async def test_a_resolver_without_a_pin_would_guard_nothing() -> None:
    """The near-miss this design exists to rule out, stated as an assertion.

    Handing ``GuardedHttpClient`` a resolver but no pinned address produces a client
    that *looks* guarded -- it has a resolver, the check is on the call path -- and
    refuses nothing, because the comparison short-circuits on the missing pin. That
    is the shape a synchronous build path could produce, and the reason
    :func:`build_pinned_client` is a coroutine.
    """
    hostile = frozenset({"169.254.169.254"})

    async def rebound_resolver(host: str, port: int) -> frozenset[str]:
        return hostile

    inert = GuardedHttpClient(
        httpx.URL("http://ollama:11434"),
        _ALLOWED,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True})),
        resolver=rebound_resolver,
        pinned_addresses=None,
    )
    answered = await inert.post_json("/x", {}, context=_CONTEXT, provider_label="Ollama")
    assert answered == {"ok": True}, "a pinless client refuses nothing, however armed it looks"

    # The builder that *takes* the pin, given a resolver that moves between the two
    # lookups, refuses -- same resolver interface, opposite outcome.
    answers = iter([frozenset({"10.89.0.7"}), hostile])

    async def moving_resolver(host: str, port: int) -> frozenset[str]:
        return next(answers)

    armed = await build_pinned_client(
        "http://ollama:11434",
        _ALLOWED,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True})),
        resolver=moving_resolver,
    )
    with pytest.raises(ProviderNotConfigured):
        await armed.post_json("/x", {}, context=_CONTEXT, provider_label="Ollama")
