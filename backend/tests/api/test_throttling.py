"""The caps on the two endpoints that spend money or a shared quota (AUD-007, AUD-008).

Two layers are tested, because they fail differently. The limiters themselves are
exercised in isolation -- the in-process :class:`ConcurrencyLimiter` here, the
shared rate buckets in ``tests/infra/test_shared_rate_limits.py`` against a real
PostgreSQL -- and the wiring is exercised over HTTP, because a limiter nobody
attached to a route protects nothing.

The wiring half runs against the real buckets, so every application it drives
needs scopes of its own: the rows are committed on the limiter's own connection
and outlive the test that wrote them, by design. :func:`tight_throttles` takes
the application for exactly that reason.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from chaudron.api.deps import get_catalog
from chaudron.api.throttling import (
    AtCapacityError,
    ConcurrencyLimiter,
    Throttles,
)
from chaudron.domain.models import Household
from chaudron.infra.rate_limits import BucketPolicy, SharedRateLimiter
from tests.api.conftest import MakeLocation, MakeProduct, RecordingCatalog
from tests.api.test_products import MILK_GTIN, MILK_RECORD, MILK_STORED
from tests.api.test_providers import add_config
from tests.api.test_recipes import SUGGEST_URL, stock_cream, use_provider_double
from tests.conftest import MakeHousehold, household_headers
from tests.support.pdfs import build_pdf

# --------------------------------------------------------------------------- #
# ConcurrencyLimiter
# --------------------------------------------------------------------------- #


def test_a_key_cannot_hold_more_slots_than_its_share() -> None:
    limiter = ConcurrencyLimiter(per_key=2, total=10)
    with limiter.slot("household"), limiter.slot("household"), pytest.raises(AtCapacityError):
        limiter.slot("household").__enter__()


def test_the_process_wide_cap_applies_across_keys() -> None:
    limiter = ConcurrencyLimiter(per_key=2, total=2)
    with limiter.slot("a"), limiter.slot("b"), pytest.raises(AtCapacityError):
        limiter.slot("c").__enter__()


def test_a_slot_is_released_even_when_the_request_fails() -> None:
    """Otherwise one 500 permanently narrows the endpoint for that household."""
    limiter = ConcurrencyLimiter(per_key=1, total=1)

    with pytest.raises(RuntimeError), limiter.slot("household"):
        raise RuntimeError("the endpoint blew up")

    with limiter.slot("household"):
        pass


def test_a_cap_below_the_per_key_one_is_refused_at_construction() -> None:
    with pytest.raises(ValueError, match="cannot be below"):
        ConcurrencyLimiter(per_key=4, total=2)


# --------------------------------------------------------------------------- #
# Wiring: /v1/products/lookup
# --------------------------------------------------------------------------- #


def install(app: FastAPI, throttles: Throttles) -> None:
    app.state.throttles = throttles


def tight_throttles(
    app: FastAPI,
    *,
    lookups: int = 100,
    suggestions: int = 100,
    per_household: int = 4,
    total: int = 8,
    logins: int = 1000,
    receipts: int = 100,
    imports: int = 100,
) -> Throttles:
    """Limiters small enough to reach, in scopes no other test can reach.

    Takes the application for the two things it owns and this cannot invent: the
    engine the buckets are rows in, and the scope prefix the harness made unique
    for this test (``tests/conftest.py``). The second is not tidiness. A
    deduction is committed on the limiter's own connection so that the caller's
    rollback cannot undo it -- that is the property the move to PostgreSQL was
    for -- and the same commit is why the row survives the test. Two tests
    sharing a scope would share a bucket, and the second would find it already
    drained.

    The ``tight:`` segment keeps these apart from the application's own throttles
    as well, so a test that installs a cap of one cannot be handed a bucket some
    earlier request opened at a hundred.
    """
    engine = app.state.database.engine
    prefix = f"{app.state.rate_limit_scope_prefix}tight:"

    def rate(name: str, limit: int, window_seconds: float) -> SharedRateLimiter:
        return SharedRateLimiter(
            engine,
            BucketPolicy(scope=f"{prefix}{name}", limit=limit, window_seconds=window_seconds),
        )

    return Throttles(
        recipe_suggestions=rate("recipe_suggestions", suggestions, 3600.0),
        recipe_inferences=ConcurrencyLimiter(per_key=per_household, total=total),
        product_lookups=rate("product_lookups", lookups, 60.0),
        receipt_imports=rate("receipt_imports", receipts, 3600.0),
        receipt_inferences=ConcurrencyLimiter(per_key=per_household, total=total),
        shopping_imports=rate("shopping_imports", imports, 3600.0),
        shopping_import_documents=ConcurrencyLimiter(per_key=per_household, total=total),
        # Deliberately slack: these tests tighten the *spend* caps, and a
        # sign-in limiter that refused here would fail them for the wrong reason.
        # `tests/api/test_authentication.py` tightens these three on their own.
        login_attempts_by_ip=rate("login_attempts_by_ip", logins, 3600.0),
        login_attempts_by_account=rate("login_attempts_by_account", logins, 3600.0),
        registrations=rate("registrations", logins, 3600.0),
        machine_token_attempts=rate("machine_token_attempts", logins, 3600.0),
    )


@pytest.fixture
def catalog(api_app: FastAPI) -> RecordingCatalog:
    double = RecordingCatalog({MILK_STORED: MILK_RECORD})
    api_app.dependency_overrides[get_catalog] = lambda: double
    return double


async def test_a_household_cannot_drain_the_shared_catalogue_budget(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    catalog: RecordingCatalog,
    make_household: MakeHousehold,
) -> None:
    """AUD-007: ten requests a minute used to take barcodes down for everyone.

    The Open Food Facts budget is instance-wide (ADR-0008), so the inbound limit
    has to bite before the outbound one does, and it has to bite per household.
    """
    install(api_app, tight_throttles(api_app, lookups=2))
    household = await make_household()
    other = await make_household()
    url = f"/v1/products/lookup?gtin={MILK_GTIN}"

    for _ in range(2):
        allowed = await api_client.get(url, headers=household_headers(household))
        assert allowed.status_code == 200, allowed.text

    refused = await api_client.get(url, headers=household_headers(household))
    assert refused.status_code == 429
    assert refused.headers["content-type"].startswith("application/problem+json")
    assert refused.json()["type"].endswith("/rate-limited")
    assert int(refused.headers["Retry-After"]) >= 1

    served = await api_client.get(url, headers=household_headers(other))
    assert served.status_code == 200, "one household's flood must not throttle another"


# --------------------------------------------------------------------------- #
# Wiring: /v1/recipes/suggest
# --------------------------------------------------------------------------- #


async def test_recipe_suggestions_are_capped_per_household(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
    make_location: MakeLocation,
    make_product: MakeProduct,
) -> None:
    """AUD-008: the endpoint that spends real money had no ceiling at all."""
    install(api_app, tight_throttles(api_app, suggestions=1))
    household = await make_household()
    await add_config(db_session, household)
    await stock_cream(api_client, household, make_location, make_product)
    use_provider_double(api_app, household, "nominal")

    first = await api_client.post(
        SUGGEST_URL, headers=household_headers(household), json={"max_suggestions": 1}
    )
    assert first.status_code == 200, first.text

    second = await api_client.post(
        SUGGEST_URL, headers=household_headers(household), json={"max_suggestions": 1}
    )
    assert second.status_code == 429
    assert second.json()["type"].endswith("/rate-limited")
    assert int(second.headers["Retry-After"]) >= 1


async def test_a_second_tab_does_not_double_the_bill(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
    make_location: MakeLocation,
    make_product: MakeProduct,
) -> None:
    """The concurrency slot, proved by holding it while a real request arrives.

    Occupying the limiter directly rather than racing two HTTP calls: the race
    would pass or fail on how fast the provider double answers, which is not the
    property under test.
    """
    throttles = tight_throttles(api_app, per_household=1, total=1)
    install(api_app, throttles)
    household = await make_household()
    await add_config(db_session, household)
    await stock_cream(api_client, household, make_location, make_product)
    use_provider_double(api_app, household, "nominal")

    with throttles.recipe_inferences.slot(str(household.id)):
        refused = await api_client.post(
            SUGGEST_URL, headers=household_headers(household), json={"max_suggestions": 1}
        )
    assert refused.status_code == 429
    assert int(refused.headers["Retry-After"]) >= 1

    admitted = await api_client.post(
        SUGGEST_URL, headers=household_headers(household), json={"max_suggestions": 1}
    )
    assert admitted.status_code == 200, "the slot must be free again once the request ends"


async def test_an_unresolved_household_is_refused_before_any_budget_is_spent(
    api_app: FastAPI, anonymous_client: httpx.AsyncClient
) -> None:
    """The limiter keys on a resolved household, so it can never be keyed on ``None``.

    Authentication runs first now, so the request never reaches the limiter at
    all -- which is the same property, established one dependency earlier.
    """
    install(api_app, tight_throttles(api_app, suggestions=1, lookups=1))

    for _ in range(3):
        response = await anonymous_client.post(SUGGEST_URL, json={"max_suggestions": 1})
        assert response.status_code == 401


# --------------------------------------------------------------------------- #
# Wiring: /v1/receipts/import
# --------------------------------------------------------------------------- #


async def test_receipt_imports_are_capped_per_household(
    api_app: FastAPI, api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    """The third endpoint that spends something the caller does not own.

    A photographed receipt is a billed inference *and* a multi-megabyte upload
    held in memory while base64 doubles it, so a loop on this route costs a
    household its credit and the instance its RAM. The PDF path is throttled
    identically even though it calls no model: it still holds an upload and still
    runs a parser, and a limiter that had to know which kind of file it was
    before reading it would be a rule applied too late to help.

    The bound is proved to bite on the *second* call rather than the first, so a
    limiter accidentally set to zero would fail this test rather than pass it.
    """
    install(api_app, tight_throttles(api_app, receipts=1))
    household = await make_household()
    other = await make_household()
    pdf = build_pdf(
        [
            [
                "SUPER U CHAMPTOCEAUX",
                "PAIN DE CAMPAGNE                 2,00",
                "YAOURT NATURE X8                 2,10",
                "TOTAL A PAYER EUR                4,10",
            ]
        ]
    )

    async def send(target: Household, name: str) -> httpx.Response:
        return await api_client.post(
            "/v1/receipts/import",
            files={"file": (name, pdf, "application/pdf")},
            headers=household_headers(target),
        )

    first = await send(household, "un.pdf")
    assert first.status_code == 201, first.text

    refused = await send(household, "deux.pdf")
    assert refused.status_code == 429
    assert refused.headers["content-type"].startswith("application/problem+json")
    assert refused.json()["type"].endswith("/rate-limited")
    assert int(refused.headers["Retry-After"]) >= 1

    served = await send(other, "trois.pdf")
    assert served.status_code == 201, "one household's flood must not throttle another"


# --------------------------------------------------------------------------- #
# Wiring: /v1/shopping-lists/import
# --------------------------------------------------------------------------- #


async def test_shopping_imports_are_capped_per_household(
    api_app: FastAPI, api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    """The endpoint that spends nothing a provider bills for, which is why it had no cap.

    That was the finding, and the reasoning behind it was the wrong way round:
    "costs no tokens" was read as "costs nothing". Reading a document is CPU the
    caller does not own, fifteen concurrent one-megabyte uploads were answered
    fifteen times with ``200`` while every other request in the process waited,
    and sign-up is open -- so the cost of an unusable instance was one account and
    a loop.

    The per-document limits in ``infra/documents/sandbox.py`` bound one parse.
    This is what bounds how many, and it is the half that cannot be replaced.
    """
    install(api_app, tight_throttles(api_app, imports=1))
    household = await make_household()
    other = await make_household()
    pdf = build_pdf([["PAIN COMPLET", "LAIT 1 L", "POMMES 1 kg"]])

    async def send(target: Household, name: str) -> httpx.Response:
        return await api_client.post(
            "/v1/shopping-lists/import",
            files={"file": (name, pdf, "application/pdf")},
            headers=household_headers(target),
        )

    first = await send(household, "courses.pdf")
    assert first.status_code == 200, first.text

    refused = await send(household, "courses-2.pdf")
    assert refused.status_code == 429
    assert refused.headers["content-type"].startswith("application/problem+json")
    assert refused.json()["type"].endswith("/rate-limited")
    assert int(refused.headers["Retry-After"]) >= 1

    served = await send(other, "courses-3.pdf")
    assert served.status_code == 200, "one household's flood must not throttle another"


async def test_the_pasted_text_entrance_spends_the_same_budget(
    api_app: FastAPI, api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    """Two entrances, one budget. Capping only the file one moves the attack."""
    install(api_app, tight_throttles(api_app, imports=1))
    household = await make_household()

    first = await api_client.post(
        "/v1/shopping-lists/import",
        json={"text": "pain\nlait 1 L"},
        headers=household_headers(household),
    )
    assert first.status_code == 200, first.text

    refused = await api_client.post(
        "/v1/shopping-lists/import",
        json={"text": "pommes"},
        headers=household_headers(household),
    )
    assert refused.status_code == 429
