"""SEC-003: an API key must not survive the trip from an SDK error to a domain error.

The finding is concrete: providers routinely echo the credential they were called
with inside their own error messages, so a single ``str(exc)`` in a log line, a
traceback or an HTTP response leaks a third party's billable secret -- and a key that
reached a log outlives every rotation policy.

The doubles deliberately embed a key-shaped string in the failures they raise. This
suite drives every adapter through every failure mode and asserts that nothing
key-shaped comes out the other side. It is the regression test for the rule "the
adapters translate, they never quote".

The second half covers the path a household's *own* key travels -- encryption,
storage, decryption, and the four ways decryption fails. That path handles the
plaintext directly, so it is where a leak would be worst and least visible: a
rotation on a Sunday evening producing a stack trace with a billable secret in it.
"""

from __future__ import annotations

import uuid

import pytest

from chaudron.domain.llm_ports import CredentialDecryptionError, LlmError, ProviderNotConfigured
from chaudron.domain.models import LlmProviderMode
from chaudron.infra.crypto import CredentialCipher, SealedCredential
from chaudron.infra.llm import doubles
from chaudron.infra.llm.contract import CONTRACT_ADAPTERS
from chaudron.infra.llm.factory import HouseholdProviderConfig, LlmProviderFactory
from chaudron.infra.llm.settings import LlmSettings
from chaudron.infra.redaction import redact, snippet

_FAILURE_SCENARIOS = [name for name in doubles.SCENARIOS if name != "nominal"]

#: A household key, shaped like the real thing so the pattern-based scrubber applies.
_STORED_KEY = "sk-ant-api03-a-households-own-billable-key"
_HOUSEHOLD = uuid.UUID(int=7)
_CONFIG = uuid.UUID(int=8)


@pytest.mark.parametrize("provider_key", sorted(CONTRACT_ADAPTERS))
@pytest.mark.parametrize("scenario", _FAILURE_SCENARIOS)
@pytest.mark.parametrize("port_name", ["RecipeGenerator", "ReceiptParser"])
async def test_no_failure_message_carries_a_credential(
    provider_key: str, scenario: str, port_name: str
) -> None:
    adapter = CONTRACT_ADAPTERS[provider_key]
    try:
        implementation = adapter.build_port(port_name, scenario)
    except LookupError:
        pytest.skip(f"{provider_key} does not offer {port_name} in the tested configuration")

    method = getattr(implementation, "suggest" if port_name == "RecipeGenerator" else "parse")
    with pytest.raises(LlmError) as raised:
        await method()

    rendered = str(raised.value)
    assert doubles.LEAKY_KEY not in rendered
    assert "sk-" not in rendered
    # And the same must hold for the whole chain, not only the outermost message:
    # `raise ... from exc` would put the SDK's text one frame away from any handler.
    cause = raised.value.__cause__
    assert cause is None or doubles.LEAKY_KEY not in str(cause)


@pytest.mark.parametrize(
    "text",
    [
        "auth failed for sk-ant-api03-AAAABBBBCCCCDDDD",
        "key AIzaSyD-EXAMPLE-KEY-VALUE-1234",
        "Authorization: Bearer eyJhbGciOi.eyJzdWIiOiIx.QWxhZGRpbg",
        "connect to https://user:hunter2@ollama.internal:11434 failed",
        "AKIAIOSFODNN7EXAMPLE denied",
    ],
)
def test_known_credential_shapes_are_scrubbed(text: str) -> None:
    cleaned = redact(text)
    assert "[redacted]" in cleaned
    for marker in ("sk-ant-api03", "AIzaSyD", "eyJhbGciOi", "hunter2", "AKIAIOSFODNN7"):
        assert marker not in cleaned


def test_literal_secrets_are_removed_even_when_no_pattern_matches() -> None:
    """A self-hosted gateway's opaque token matches no published shape."""
    opaque = "9f2c1ab4-not-a-known-vendor-prefix"
    assert opaque not in redact(f"rejected token {opaque}", secrets=[opaque])


def test_short_values_are_not_treated_as_secrets() -> None:
    """Blanking a two-character 'secret' would only mangle the diagnostic."""
    assert redact("model gpt-4o failed", secrets=["4o"]) == "model gpt-4o failed"


def test_snippets_are_bounded_and_single_line() -> None:
    excerpt = snippet("a\n" * 500, limit=40)
    assert "\n" not in excerpt
    assert len(excerpt) <= 43


# --------------------------------------------------------------------------- #
# The household's own key, from encryption to every way decryption fails
# --------------------------------------------------------------------------- #


def _sealed(box: CredentialCipher, *, household_id: uuid.UUID = _HOUSEHOLD) -> SealedCredential:
    stored = box.encrypt(_STORED_KEY, household_id=household_id, config_id=_CONFIG)
    return SealedCredential(household_id, _CONFIG, stored.ciphertext, stored.key_id)


def _undecryptable() -> dict[str, tuple[CredentialCipher, SealedCredential]]:
    """One entry per way a stored key stops being readable, all four realistic."""
    current, previous = CredentialCipher(b"A" * 32), CredentialCipher(b"B" * 32)
    intact = _sealed(current)
    corrupted = bytearray(intact.ciphertext)
    corrupted[-1] ^= 0x01
    return {
        # The operator rotated CHAUDRON_CREDENTIAL_ENCRYPTION_KEY.
        "rotated_master_key": (previous, intact),
        # A ciphertext copied from another household's row.
        "cross_household_replay": (
            current,
            SealedCredential(uuid.UUID(int=99), _CONFIG, intact.ciphertext, intact.key_id),
        ),
        "tampered_ciphertext": (
            current,
            SealedCredential(_HOUSEHOLD, _CONFIG, bytes(corrupted), intact.key_id),
        ),
        "truncated_column": (
            current,
            SealedCredential(_HOUSEHOLD, _CONFIG, b"short", intact.key_id),
        ),
    }


@pytest.mark.parametrize("failure", sorted(_undecryptable()))
def test_no_decryption_failure_carries_the_key_it_could_not_read(failure: str) -> None:
    """Whatever went wrong, the answer says so without quoting the secret.

    ``__cause__`` matters as much as the message: ``raise ... from exc`` would leave
    the cryptography exception one frame from any handler, and a handler that logs
    ``exc_info`` would then write whatever it holds to disk.
    """
    box, sealed = _undecryptable()[failure]

    with pytest.raises(CredentialDecryptionError) as raised:
        box.decrypt(sealed)

    for rendered in (str(raised.value), repr(raised.value)):
        assert _STORED_KEY not in rendered
        assert "sk-ant" not in rendered
        assert redact(rendered) == rendered, "nothing key-shaped should need scrubbing"
    assert raised.value.__cause__ is None, "an explicit chain prints with the traceback"
    context = raised.value.__context__
    assert context is None or raised.value.__suppress_context__, (
        "an implicit chain must be suppressed, so a formatted traceback stops here"
    )
    assert context is None or _STORED_KEY not in str(context)


def test_the_stored_credential_object_carries_no_plaintext() -> None:
    """It is persisted, logged and returned from a service -- so it must be inert."""
    stored = CredentialCipher(b"A" * 32).encrypt(
        _STORED_KEY, household_id=_HOUSEHOLD, config_id=_CONFIG
    )
    rendered = repr(stored)
    assert _STORED_KEY not in rendered
    assert stored.last4 == _STORED_KEY[-4:], "the four characters are the whole exception"


async def test_the_factory_refuses_an_undecryptable_key_without_quoting_it() -> None:
    """The production BYOK path, end to end, with the master key rotated underneath."""
    config = HouseholdProviderConfig(
        household_id=_HOUSEHOLD,
        mode=LlmProviderMode.BYOK,
        provider_code="anthropic",
        model="claude-opus-5",
        sealed_api_key=_sealed(CredentialCipher(b"A" * 32)),
    )
    factory = LlmProviderFactory(LlmSettings(), cipher=CredentialCipher(b"B" * 32))

    with pytest.raises(ProviderNotConfigured) as raised:
        await factory.build_transport(config)

    assert _STORED_KEY not in str(raised.value)
    assert "CHAUDRON_CREDENTIAL_ENCRYPTION_KEY" in str(raised.value)


def test_a_household_key_is_scrubbed_from_any_text_it_reaches() -> None:
    """The last line of defence: even if a key ever reached a diagnostic."""
    assert _STORED_KEY not in redact(f"provider said: {_STORED_KEY} is invalid")
    assert _STORED_KEY not in snippet(f"garbage {_STORED_KEY}", secrets=[_STORED_KEY])
