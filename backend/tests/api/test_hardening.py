"""Request bounds, response headers, and the two identifiers a client must not choose.

Regression cover for AUD-009 (unbounded request body), AUD-013 (the household
oracle in the 401 wording), AUD-014 (a client-chosen incident identifier),
AUD-015 (no security headers), AUD-018 (documentation exposed outside ``local``)
and AUD-026 (three spellings of one household).
"""

from __future__ import annotations

import base64
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from chaudron.api.main import create_app
from chaudron.api.middleware import API_CSP
from chaudron.config import Settings
from tests.conftest import MakeHousehold, household_headers

#: The configured default, and what the tests push past. Comfortably above any
#: v1 request and far below the 50 MB the audit got accepted.
BODY_LIMIT_BYTES = 256 * 1024


#: A valid configuration, as a mapping, so a test can also build an *invalid* one
#: by removing a key -- which is how the "env has no default" rule is checked.
_BASE_SETTINGS: dict[str, Any] = {
    "env": "ci",
    "database_url": SecretStr("postgresql+asyncpg://user:pass@localhost:5432/chaudron"),
    "secret_key": SecretStr("a-secret-key-long-enough-to-pass-validation"),
    "credential_encryption_key": SecretStr(base64.b64encode(b"0" * 32).decode()),
    "cors_origins": ["http://localhost:5173"],
    # A listed origin without credentials is refused at startup now that the
    # session is a cookie (`config.py`).
    "cors_allow_credentials": True,
}


def build_settings(**overrides: Any) -> Settings:
    """A valid configuration built from literals, so no environment leaks in."""
    return Settings(**{**_BASE_SETTINGS, **overrides})


# --------------------------------------------------------------------------- #
# AUD-009 -- request body bounds
# --------------------------------------------------------------------------- #


async def test_a_declared_oversized_body_is_refused_with_413(
    api_client: httpx.AsyncClient,
) -> None:
    """The audit sent 50 MB and got a 422, which proves it was parsed first."""
    response = await api_client.post(
        "/v1/inventory",
        content=b"x" * (BODY_LIMIT_BYTES + 1),
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 413
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["type"].endswith("/request-body-too-large")
    assert body["limit_bytes"] == BODY_LIMIT_BYTES


async def test_the_refusal_happens_before_the_household_is_resolved(
    api_client: httpx.AsyncClient,
) -> None:
    """No header, no valid tenant, no parsing -- and still a 413 rather than a 401.

    That ordering is the point of the control: work refused early is work an
    unauthenticated caller cannot make the process do.
    """
    response = await api_client.post(
        "/v1/recipes/suggest",
        content=b"x" * (BODY_LIMIT_BYTES + 1),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 413


async def test_an_undeclared_oversized_body_is_cut_off_mid_read(
    api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    """A chunked body has no ``Content-Length`` to check, so the read is counted."""

    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(40):
            yield b"x" * 16384

    response = await api_client.post(
        "/v1/inventory",
        content=chunks(),
        headers={
            "Content-Type": "application/json",
            **household_headers(await make_household()),
        },
    )

    assert response.status_code == 413, response.text
    assert response.json()["type"].endswith("/request-body-too-large")


async def test_a_legitimate_body_still_goes_through(
    api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    """The bound must not be so tight that it breaks the contract it protects."""
    household = await make_household()
    response = await api_client.post(
        "/v1/products",
        headers=household_headers(household),
        json={"name": "Farine de blé", "brand": None, "gtin": None, "default_unit": "g"},
    )
    assert response.status_code == 201, response.text


# --------------------------------------------------------------------------- #
# AUD-015 -- response headers
# --------------------------------------------------------------------------- #


async def test_every_response_carries_the_headers_that_apply_to_json(
    api_client: httpx.AsyncClient,
) -> None:
    response = await api_client.get("/healthz")

    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["Cross-Origin-Opener-Policy"] == "same-origin"
    assert response.headers["Content-Security-Policy"] == API_CSP


async def test_household_data_is_never_stored_by_a_cache(
    api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    """The concrete half of AUD-015: the tenant selector is a header caches ignore."""
    household = await make_household()
    response = await api_client.get("/v1/locations", headers=household_headers(household))

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert "X-Household-Id" in response.headers["Vary"]


async def test_hsts_is_absent_outside_production(api_client: httpx.AsyncClient) -> None:
    """It is meaningless over the plain HTTP a developer runs, so it is not sent."""
    response = await api_client.get("/healthz")
    assert "Strict-Transport-Security" not in response.headers


async def test_hsts_is_sent_in_production() -> None:
    """Checked on ``/healthz``, the one route that needs neither database nor tenant.

    ``base_url`` has to be ``https://`` for a production configuration to build at
    all: the session cookie is ``Secure``, so an http production instance could
    never sign anybody in and refuses to start (``config.py``).
    """
    app = create_app(build_settings(env="production", base_url="https://chaudron.example.org"))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/healthz")
    await app.state.catalog.aclose()
    await app.state.database.dispose()

    assert response.status_code == 200
    assert response.headers["Strict-Transport-Security"].startswith("max-age=31536000")


# --------------------------------------------------------------------------- #
# AUD-014 -- the incident identifier
# --------------------------------------------------------------------------- #


async def test_the_request_id_is_generated_and_never_the_client_s(
    api_client: httpx.AsyncClient,
) -> None:
    """An identifier a stranger chooses can neither correlate nor incriminate."""
    forged = "AAAA-attacker-controlled-BBBB"
    response = await api_client.get("/healthz", headers={"X-Request-Id": forged})

    assert response.headers["X-Request-Id"] != forged
    uuid.UUID(response.headers["X-Request-Id"])


async def test_a_forged_request_id_is_not_reflected_into_an_error_body(
    anonymous_client: httpx.AsyncClient,
) -> None:
    forged = "<script>alert(1)</script>"
    response = await anonymous_client.get("/v1/locations", headers={"X-Request-Id": forged})

    assert response.status_code == 401
    assert response.json()["request_id"] != forged
    assert forged not in response.text


# --------------------------------------------------------------------------- #
# AUD-013 / AUD-026 -- the household header
# --------------------------------------------------------------------------- #


async def test_absent_malformed_and_unknown_are_one_indistinguishable_answer(
    api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    """Three different bodies let a caller confirm a household identifier for free.

    Still true under authentication, and now checked for an authenticated caller
    -- which is the only one who gets this far. The three headers are: none, a
    malformed value, and a well-formed value naming a household this account is
    not a member of. All three answer ``403 household-forbidden``, because all
    three are the same branch: a lookup in the caller's own membership list
    (``api/deps.py``).

    A membership is created first so that "no header" is not the separate
    *no household at all* case, which legitimately says something else.
    """
    await make_household()
    answers = []
    for headers in (
        {"X-Household-Id": str(uuid.uuid7())},
        {"X-Household-Id": "not-a-uuid"},
        {"X-Household-Id": str(uuid.uuid7())},
    ):
        response = await api_client.get("/v1/locations", headers=headers)
        assert response.status_code == 403
        body = response.json()
        # The identifier is per-request by construction and is the one field that
        # is *meant* to differ.
        body.pop("request_id", None)
        answers.append(body)

    assert answers[0] == answers[1] == answers[2]


async def test_an_anonymous_caller_learns_nothing_about_a_household_either(
    anonymous_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    """Before the session check, a real identifier and a made-up one look alike."""
    household = await make_household()
    real = await anonymous_client.get("/v1/locations", headers=household_headers(household))
    fake = await anonymous_client.get(
        "/v1/locations", headers={"X-Household-Id": str(uuid.uuid7())}
    )

    assert real.status_code == fake.status_code == 401
    for response in (real, fake):
        body = response.json()
        body.pop("request_id", None)
    assert {k: v for k, v in real.json().items() if k != "request_id"} == {
        k: v for k, v in fake.json().items() if k != "request_id"
    }


@pytest.mark.parametrize(
    "spelling",
    [
        "urn:uuid:{value}",
        "{{{value}}}",
        "{value_hex}",
        "{value_upper}",
    ],
)
async def test_only_the_canonical_uuid_spelling_resolves_a_household(
    api_client: httpx.AsyncClient, make_household: MakeHousehold, spelling: str
) -> None:
    """Several spellings of one identifier are several keys to anything counting."""
    household = await make_household()
    value = str(household.id)
    header = spelling.format(value=value, value_hex=household.id.hex, value_upper=value.upper())

    response = await api_client.get("/v1/locations", headers={"X-Household-Id": header})
    assert response.status_code == 403

    canonical = await api_client.get("/v1/locations", headers=household_headers(household))
    assert canonical.status_code == 200


# --------------------------------------------------------------------------- #
# AUD-018 -- documentation exposure
# --------------------------------------------------------------------------- #


async def test_the_documentation_is_closed_outside_local(
    api_client: httpx.AsyncClient,
) -> None:
    """The suite runs as ``ci``; ``staging`` and a forgotten variable behave the same."""
    assert (await api_client.get("/docs")).status_code == 404
    assert (await api_client.get("/openapi.json")).status_code == 404


@pytest.mark.parametrize(
    ("env", "override", "expected"),
    [
        ("local", None, True),
        ("ci", None, False),
        ("staging", None, False),
        ("production", None, False),
        ("staging", True, True),
        ("local", False, False),
    ],
)
def test_docs_default_closed_and_open_only_on_a_decision(
    env: str, override: bool | None, expected: bool
) -> None:
    settings = build_settings(
        env=env, enable_docs=override, base_url="https://chaudron.example.org"
    )
    assert settings.docs_enabled is expected


# --------------------------------------------------------------------------- #
# CORS
# --------------------------------------------------------------------------- #


def test_a_wildcard_origin_is_refused_at_startup() -> None:
    """The session is a cookie, so ``*`` is both refused by browsers and unsafe."""
    with pytest.raises(ValidationError, match=r"cannot contain"):
        build_settings(cors_origins=["*"])


def test_a_listed_origin_without_credentials_is_refused_at_startup() -> None:
    """A cross-origin client that may not send a cookie can never sign in.

    Left unchecked it produces a frontend where every call answers ``401`` and
    nothing in the logs says why.
    """
    with pytest.raises(ValidationError, match=r"CORS_ALLOW_CREDENTIALS"):
        build_settings(cors_origins=["https://pwa.example.org"], cors_allow_credentials=False)


def test_a_production_instance_must_be_served_over_https() -> None:
    """The session cookie is ``Secure``; over plain HTTP a browser never stores it."""
    with pytest.raises(ValidationError, match=r"must be an https"):
        build_settings(env="production", base_url="http://chaudron.example.org")


def test_debug_logging_is_refused_in_production() -> None:
    """At DEBUG the root logger publishes every bound parameter: allergens included."""
    with pytest.raises(ValidationError, match=r"cannot be DEBUG"):
        build_settings(env="production", base_url="https://chaudron.example.org", log_level="DEBUG")


def test_the_environment_has_no_default() -> None:
    """A blank ``CHAUDRON_ENV`` used to mean ``local``, which opens /docs and drops HSTS."""
    values = {key: value for key, value in _BASE_SETTINGS.items() if key != "env"}
    with pytest.raises(ValidationError, match=r"env"):
        Settings(**values)


def test_inconsistent_concurrency_caps_are_refused_at_startup() -> None:
    with pytest.raises(ValidationError, match=r"cannot be below"):
        build_settings(recipe_max_concurrent_per_household=4, recipe_max_concurrent_total=2)
