"""``GET /v1/providers/capabilities`` -- the state the PWA shows permanently.

The endpoint answers ``200`` in every case, including "nothing is configured": the
client has a configuration screen for that state, and a 4xx would make it render a
failure instead. What is asserted here is therefore the *shape* (frozen by
``docs/api-contract-v1.md`` and ``frontend/src/api/types.ts``) and the fact that
``degraded_reasons`` carries sentences a person can act on rather than error codes.

Nothing here calls a provider: capabilities are read from a table or from the stored
probe, so the endpoint costs nothing and needs no credential.
"""

from __future__ import annotations

import datetime as dt
import uuid

import httpx
import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from chaudron.domain.models import (
    Household,
    LlmConfigStatus,
    LlmProviderConfig,
    LlmProviderMode,
    LlmPurpose,
    LlmPurposeBinding,
)
from chaudron.infra.crypto import CredentialCipher
from tests.conftest import MakeHousehold, build_test_cipher, household_headers

CAPABILITIES_URL = "/v1/providers/capabilities"

#: A household's own key, in the shape a provider issues one.
STORED_KEY = "sk-ant-api03-this-households-own-key-cafe"

#: Fixed rather than ``now()``: the ordering constraint compares the two, so a
#: withdrawal has to sit after the agreement it withdraws, and a test that produced
#: both from the same clock tick would pass on ``>=`` while saying nothing.
_CONSENT_GRANTED_AT = dt.datetime(2026, 1, 5, 9, 0, tzinfo=dt.UTC)
_CONSENT_REVOKED_AT = dt.datetime(2026, 3, 17, 18, 30, tzinfo=dt.UTC)


async def add_config(
    session: AsyncSession,
    household: Household,
    *,
    label: str = "default",
    mode: LlmProviderMode = LlmProviderMode.INSTANCE_OWNER,
    provider_code: str = "openai",
    model: str = "gpt-4o",
    base_url: str | None = None,
    status: LlmConfigStatus = LlmConfigStatus.VERIFIED,
    supports_vision: bool = True,
    supports_structured_output: bool = True,
    max_context_tokens: int | None = None,
    last_verified_at: dt.datetime | None = None,
    api_key: str | None = None,
    cipher: CredentialCipher | None = None,
    consented: bool = True,
    consent_revoked: bool = False,
) -> LlmProviderConfig:
    """Insert a provider configuration; no endpoint creates one in this slice.

    ``api_key`` fills the secret triplet the way the application does -- real
    AES-256-GCM, bound to this exact ``(household, configuration)`` pair. Passing a
    ``cipher`` built on a different master key simulates a rotation: the row is
    well-formed and the constraints are satisfied, but the running instance can no
    longer read it.

    The identifier is generated here rather than at flush time because it is part of
    what the ciphertext is bound to.

    ``consented`` defaults to ``True`` because the sentence almost every caller is
    writing is "a household with a working provider", and since revision ``0016``
    such a household has agreed to its data being sent (``services/providers.py``
    refuses otherwise, for every mode but ``ollama``). The default keeps those tests
    saying what they meant, and the two consent states get their own tests below
    rather than being smuggled in as a fixture side effect.
    """
    config_id = uuid.uuid7()
    stored = (
        None
        if api_key is None
        else (cipher or build_test_cipher()).encrypt(
            api_key, household_id=household.id, config_id=config_id
        )
    )
    config = LlmProviderConfig(
        id=config_id,
        household_id=household.id,
        label=label,
        mode=mode,
        provider_code=provider_code,
        model=model,
        base_url=base_url,
        status=status,
        supports_vision=supports_vision,
        supports_structured_output=supports_structured_output,
        max_context_tokens=max_context_tokens,
        last_verified_at=last_verified_at,
        api_key_ciphertext=None if stored is None else stored.ciphertext,
        api_key_last4=None if stored is None else stored.last4,
        api_key_encryption_key_id=None if stored is None else stored.key_id,
        consented_at=_CONSENT_GRANTED_AT if consented else None,
        consent_revoked_at=_CONSENT_REVOKED_AT if consent_revoked else None,
    )
    session.add(config)
    await session.flush()
    return config


async def test_an_unconfigured_household_gets_a_complete_null_state(
    api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    household = await make_household()

    response = await api_client.get(CAPABILITIES_URL, headers=household_headers(household))

    assert response.status_code == 200, response.text
    # Field for field, including the nullability: the frontend is written against
    # this object and will not be changed to accommodate a drift here.
    assert response.json() == {
        "configured": False,
        "mode": None,
        "provider": None,
        "model": None,
        "capabilities": {"vision": False, "structured_output": False},
        "degraded": False,
        "degraded_reasons": [],
    }


async def test_a_capable_provider_reports_no_degradation(
    api_client: httpx.AsyncClient, db_session: AsyncSession, make_household: MakeHousehold
) -> None:
    household = await make_household()
    await add_config(db_session, household)

    response = await api_client.get(CAPABILITIES_URL, headers=household_headers(household))

    assert response.status_code == 200, response.text
    assert response.json() == {
        "configured": True,
        "mode": "instance_owner",
        "provider": "openai",
        "model": "gpt-4o",
        "capabilities": {"vision": True, "structured_output": True},
        "degraded": False,
        "degraded_reasons": [],
    }


async def test_a_small_local_model_explains_every_limit_in_plain_french(
    api_client: httpx.AsyncClient, db_session: AsyncSession, make_household: MakeHousehold
) -> None:
    """The reference degraded configuration: an old Ollama serving a text model."""
    household = await make_household()
    await add_config(
        db_session,
        household,
        mode=LlmProviderMode.OLLAMA,
        provider_code="ollama",
        model="llama3.2",
        base_url="http://ollama:11434",
        supports_vision=False,
        supports_structured_output=False,
        max_context_tokens=8192,
        last_verified_at=dt.datetime(2026, 3, 3, 9, 30, tzinfo=dt.UTC),
    )

    body = (await api_client.get(CAPABILITIES_URL, headers=household_headers(household))).json()

    assert body["configured"] is True
    assert body["capabilities"] == {"vision": False, "structured_output": False}
    assert body["degraded"] is True
    # One sentence per missing capability -- the four of the ADR-0005 taxonomy.
    assert len(body["degraded_reasons"]) == 4
    joined = " ".join(body["degraded_reasons"])
    assert "images" in joined, "the vision limit must be named, not coded"
    assert "fenêtre de contexte" in joined
    for reason in body["degraded_reasons"]:
        assert reason.strip() == reason
        assert reason.endswith("."), "a banner line is a sentence, not a token"
        assert "capability" not in reason.lower()


async def test_an_unprobed_ollama_is_not_reported_as_usable(
    api_client: httpx.AsyncClient, db_session: AsyncSession, make_household: MakeHousehold
) -> None:
    """A configuration whose abilities nobody established promises nothing."""
    household = await make_household()
    await add_config(
        db_session,
        household,
        mode=LlmProviderMode.OLLAMA,
        provider_code="ollama",
        model="llama3.2",
        base_url="http://ollama:11434",
        status=LlmConfigStatus.UNVERIFIED,
        supports_vision=False,
        supports_structured_output=False,
    )

    body = (await api_client.get(CAPABILITIES_URL, headers=household_headers(household))).json()

    assert body["configured"] is False
    # The identifiers stay: the user must recognise which configuration is at fault.
    assert (body["mode"], body["provider"], body["model"]) == ("ollama", "ollama", "llama3.2")
    assert body["degraded"] is True
    assert "détectées" in body["degraded_reasons"][0]


async def test_a_disabled_configuration_says_so(
    api_client: httpx.AsyncClient, db_session: AsyncSession, make_household: MakeHousehold
) -> None:
    household = await make_household()
    await add_config(db_session, household, status=LlmConfigStatus.DISABLED)

    body = (await api_client.get(CAPABILITIES_URL, headers=household_headers(household))).json()

    assert body["configured"] is False
    assert body["model"] == "gpt-4o"
    assert "désactivée" in body["degraded_reasons"][0]


async def test_a_household_that_brought_its_own_key_is_reported_as_ready(
    api_client: httpx.AsyncClient, db_session: AsyncSession, make_household: MakeHousehold
) -> None:
    """The flagship mode of ADR-0007, working end to end.

    The row holds nothing but ciphertext; the endpoint answers by decrypting it with
    the instance's master key and finding a usable configuration.
    """
    household = await make_household()
    await add_config(db_session, household, mode=LlmProviderMode.BYOK, api_key=STORED_KEY)

    response = await api_client.get(CAPABILITIES_URL, headers=household_headers(household))
    body = response.json()

    assert body["configured"] is True, response.text
    assert body["mode"] == "byok"
    assert body["capabilities"] == {"vision": True, "structured_output": True}
    assert body["degraded"] is False
    # And the key itself is nowhere in the answer, in any form.
    assert STORED_KEY not in response.text
    assert "sk-ant" not in response.text


async def test_a_key_the_instance_can_no_longer_decrypt_says_so_instead_of_promising(
    api_client: httpx.AsyncClient, db_session: AsyncSession, make_household: MakeHousehold
) -> None:
    """``CHAUDRON_CREDENTIAL_ENCRYPTION_KEY`` was rotated under a stored key.

    Reporting this configuration as usable would produce a green banner and a 409 on
    the first click. The endpoint says the opposite, and says what to do instead --
    without a stack trace and without quoting anything.
    """
    household = await make_household()
    await add_config(
        db_session,
        household,
        mode=LlmProviderMode.BYOK,
        api_key=STORED_KEY,
        cipher=CredentialCipher(b"a-previous-master-key-32-bytes!!"),
    )

    response = await api_client.get(CAPABILITIES_URL, headers=household_headers(household))
    body = response.json()

    assert body["configured"] is False, response.text
    assert body["degraded"] is True
    reason = body["degraded_reasons"][0]
    assert "déchiffrée" in reason
    assert "Saisissez à nouveau votre clé" in reason, "the banner must carry the remedy"
    assert STORED_KEY not in response.text


async def test_the_purpose_binding_decides_between_two_configurations(
    api_client: httpx.AsyncClient, db_session: AsyncSession, make_household: MakeHousehold
) -> None:
    """Without a binding, two candidates is a question, not a licence to pick one.

    Choosing arbitrarily would spend money on a provider the household did not
    select for this feature.
    """
    household = await make_household()
    await add_config(db_session, household, label="cheap", model="gpt-4o-mini")
    chosen = await add_config(db_session, household, label="good", model="gpt-4o")

    ambiguous = (
        await api_client.get(CAPABILITIES_URL, headers=household_headers(household))
    ).json()
    assert ambiguous["configured"] is False
    assert "Plusieurs configurations" in ambiguous["degraded_reasons"][0]

    db_session.add(
        LlmPurposeBinding(
            household_id=household.id,
            purpose=LlmPurpose.RECIPE_GENERATION,
            llm_provider_config_id=chosen.id,
        )
    )
    await db_session.flush()

    bound = (await api_client.get(CAPABILITIES_URL, headers=household_headers(household))).json()
    assert bound["configured"] is True
    assert bound["model"] == "gpt-4o"


async def test_the_endpoint_needs_a_session(anonymous_client: httpx.AsyncClient) -> None:
    assert (await anonymous_client.get(CAPABILITIES_URL)).status_code == 401


# --------------------------------------------------------------------------- #
# Consent (revision 0016)
# --------------------------------------------------------------------------- #


async def test_a_provider_without_a_recorded_consent_is_refused(
    api_client: httpx.AsyncClient, db_session: AsyncSession, make_household: MakeHousehold
) -> None:
    """No agreement on record means the provider is unusable, not merely flagged.

    ``docs/security-model.md`` section 12 makes consent the legal basis for sending
    anything to a model provider, and requires it to be opt-in. A configuration whose
    ``consented_at`` is ``NULL`` predates revision 0016 or was written straight to the
    database; either way nobody agreed, and the row fails closed.
    """
    household = await make_household()
    await add_config(db_session, household, consented=False)

    body = (await api_client.get(CAPABILITIES_URL, headers=household_headers(household))).json()

    assert body["configured"] is False
    assert body["degraded"] is True
    reason = body["degraded_reasons"][0]
    assert "accord" in reason, reason
    assert "Ollama" in reason, "the remedy must name the mode that needs no consent"


async def test_a_withdrawn_consent_stops_the_provider_and_says_so_differently(
    api_client: httpx.AsyncClient, db_session: AsyncSession, make_household: MakeHousehold
) -> None:
    """Withdrawal takes effect at the next request, and reads as a decision.

    The sentence differs from the never-granted one on purpose: telling somebody who
    has just withdrawn their consent that they never gave any reads as the
    application having mislaid it.
    """
    household = await make_household()
    await add_config(db_session, household, consent_revoked=True)

    body = (await api_client.get(CAPABILITIES_URL, headers=household_headers(household))).json()

    assert body["configured"] is False
    assert "retiré son accord" in body["degraded_reasons"][0]


async def test_ollama_needs_no_consent_because_it_sends_nothing(
    api_client: httpx.AsyncClient, db_session: AsyncSession, make_household: MakeHousehold
) -> None:
    """ADR-0007's premise, held as a test rather than as an intention.

    A household unwilling to send anything to anybody still gets the whole feature,
    and ``docs/security-model.md`` states it as a requirement in the same row that
    demands consent everywhere else: "the ``ollama`` mode must remain fully
    functional without this consent".
    """
    household = await make_household()
    await add_config(
        db_session,
        household,
        mode=LlmProviderMode.OLLAMA,
        provider_code="ollama",
        model="llama3.2-vision",
        base_url="http://127.0.0.1:11434",
        max_context_tokens=131072,
        last_verified_at=dt.datetime(2026, 2, 1, tzinfo=dt.UTC),
        consented=False,
    )

    body = (await api_client.get(CAPABILITIES_URL, headers=household_headers(household))).json()

    assert body["configured"] is True, body
    assert body["mode"] == "ollama"


async def test_consent_cannot_be_withdrawn_before_it_was_given(
    db_session: AsyncSession, make_household: MakeHousehold
) -> None:
    """The ordering constraint, exercised rather than asserted from the model.

    The case here is the one the sibling table cannot reach: **withdrawn, having
    never agreed**. ``shopping_export_target`` forbids it through ``NOT NULL`` and so
    its constraint needs only ``consent_revoked_at >= consented_at``; on a nullable
    column that comparison yields NULL, which a CHECK accepts. Written as a test
    because the first version of this constraint was the verbatim copy, and it
    admitted the row.
    """
    household = await make_household()

    with pytest.raises(IntegrityError) as raised:
        await add_config(
            db_session,
            household,
            consented=False,
            consent_revoked=True,
        )

    assert "revocation_follows_consent" in str(raised.value)
    await db_session.rollback()
