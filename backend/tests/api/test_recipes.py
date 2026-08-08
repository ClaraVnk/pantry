"""``POST /v1/recipes/suggest`` -- the one endpoint that spends real money.

Nothing here reaches a provider. The application is wired to the real OpenAI-shaped
adapter over ``infra/llm/doubles``, so the code under test is the production path
down to the socket, and the run costs nothing.

Three properties are worth more than the happy path itself:

* a household with no usable provider gets ``409 provider-not-configured`` -- the
  client shows a configuration screen, and a 500 or a 200-with-nothing would both
  hide the one thing the user can fix;
* ``in_stock`` is the server's reading of the household's stock, never the model's
  claim, and the double asserts it by getting both flags wrong on purpose;
* no provider failure carries a credential out of the process, whatever the wire
  said (security review, SEC-003).
"""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from chaudron.api.deps import get_provider_ports_builder
from chaudron.domain.models import (
    Household,
    LlmProviderMode,
    RecipeStatus,
    RecipeSuggestion,
    StorageKind,
)
from chaudron.infra.llm import doubles
from chaudron.infra.llm.factory import LlmProviderFactory
from chaudron.infra.llm.prompts import PROMPT_VERSION
from chaudron.infra.llm.settings import LlmSettings
from chaudron.services.providers import ProviderPorts, ProviderPortsBuilder
from tests.api.conftest import MakeLocation, MakeProduct
from tests.api.test_providers import add_config
from tests.conftest import MakeHousehold, household_headers

SUGGEST_URL = "/v1/recipes/suggest"

#: Stands in for the operator's own key. It never leaves the process; the double
#: has its own, deliberately leaky, one.
_INSTANCE_KEY = "sk-test-instance-owner-key"


def use_provider_double(app: FastAPI, household: Household, scenario: str) -> None:
    """Point the application's factory at a fake socket, adapter included.

    The seam is the *builder*, not the generator: the household configuration, the
    ADR-0007 permission check and the OpenAI request shaping all still run.
    """

    def build_ports() -> ProviderPortsBuilder:
        settings = LlmSettings(
            instance_owner_household_id=household.id,
            instance_owner_api_key=_INSTANCE_KEY,
            timeout_seconds=5.0,
        )
        transport = doubles.chat_completions_transport(scenario)

        def build(provider_code: str) -> ProviderPorts:
            return LlmProviderFactory(settings, transport=transport)

        return build

    app.dependency_overrides[get_provider_ports_builder] = build_ports


async def stock_cream(
    api_client: httpx.AsyncClient,
    household: Household,
    make_location: MakeLocation,
    make_product: MakeProduct,
    *,
    name: str = "Frigo",
    kind: StorageKind = StorageKind.FRIDGE,
    product_name: str = "Crème fraîche épaisse",
) -> str:
    """One lot of something the double's recipe calls for, and nothing else."""
    location = await make_location(household, name=name, kind=kind)
    product = await make_product(name=product_name, brand=None)
    created = await api_client.post(
        "/v1/inventory",
        headers=household_headers(household),
        json={
            "product_id": str(product.id),
            "location_id": str(location.id),
            "amount": "200",
            "unit": "g",
            "expires_on": "2099-01-31",
            "expiry_kind": "use_by",
            "source": "manual",
        },
    )
    assert created.status_code == 201, created.text
    return str(location.id)


async def test_an_unconfigured_household_is_sent_to_the_configuration_screen(
    api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    household = await make_household()

    response = await api_client.post(
        SUGGEST_URL, headers=household_headers(household), json={"max_suggestions": 3}
    )

    assert response.status_code == 409, response.text
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["type"] == "https://chaudron.dev/problems/provider-not-configured"


async def test_suggestions_are_returned_persisted_and_re_checked_against_the_stock(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
    make_location: MakeLocation,
    make_product: MakeProduct,
) -> None:
    household = await make_household()
    config = await add_config(db_session, household)
    await stock_cream(api_client, household, make_location, make_product)
    use_provider_double(api_app, household, "nominal")

    response = await api_client.post(
        SUGGEST_URL,
        headers=household_headers(household),
        json={"location_ids": [], "max_suggestions": 3, "notes": "rapide, sans four"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    # `usage`, `applied_constraints` and `balance` are additions to the frozen v1
    # shape, and additive is the whole point: a client written against v1 ignores
    # them. Everything v1 promised is still here, unchanged in name and in form.
    assert set(body) == {
        "provider_mode",
        "model",
        "suggestions",
        "usage",
        "applied_constraints",
        "balance",
    }
    assert body["provider_mode"] == "instance_owner"
    assert body["model"] == "gpt-4o"
    # The measurement reaches the client, not just the database. The split is the
    # assertion that matters: the double's OpenAI-shaped `prompt_tokens` is all-in
    # (2000), and `input_tokens` here is 1200 because the adapter subtracted the
    # cached part back out. A response echoing 2000 would mean the normalisation
    # in `llm_ports.TokenUsage` had been bypassed somewhere on the way up.
    assert body["usage"] == {
        "input_tokens": doubles.USAGE_INPUT,
        "output_tokens": doubles.USAGE_OUTPUT,
        "cached_input_tokens": doubles.USAGE_CACHED,
    }

    recipe = body["suggestions"][0]
    assert set(recipe) == {
        "id",
        "title",
        "summary",
        "duration_minutes",
        "servings",
        "ingredients",
        "steps",
        "uses_expiring_soon",
        "allergen_assessment",
        "expiry_pressure",
        "preparation",
    }
    assert recipe["title"] == "Gratin de courgettes"
    assert isinstance(recipe["summary"], str), "the contract has no null summary"
    assert recipe["duration_minutes"] == 35

    # The double claims the courgettes are in stock and the cream is not. Both are
    # wrong, and both are corrected here from the household's own inventory.
    flags = {ingredient["name"]: ingredient["in_stock"] for ingredient in recipe["ingredients"]}
    assert flags == {"Courgettes": False, "Crème": True}
    assert set(recipe["ingredients"][0]) == {"name", "amount", "unit", "in_stock"}

    row = (
        await db_session.scalars(
            select(RecipeSuggestion).where(RecipeSuggestion.household_id == household.id)
        )
    ).one()
    assert str(row.id) == recipe["id"], "the client's identifier is the stored row"
    assert row.provider_mode is LlmProviderMode.INSTANCE_OWNER
    assert (row.provider_code, row.model) == ("openai", "gpt-4o")
    # Against the constant, not a literal: the point of the column is that the
    # version moves when the prompt does, so pinning the literal here only means a
    # deliberate bump breaks an unrelated test.
    assert row.prompt_version == PROMPT_VERSION
    assert row.llm_provider_config_id == config.id
    assert row.status is RecipeStatus.GENERATED
    assert row.latency_ms is not None
    # Read off the wire, not defaulted. The double answers in OpenAI's vocabulary,
    # where `prompt_tokens` is all-in, so the adapter has to subtract the cached
    # part back out to fill `input_tokens` -- asserting the raw total here would
    # pass just as well for an adapter that never did.
    assert row.input_tokens == doubles.USAGE_INPUT
    assert row.output_tokens == doubles.USAGE_OUTPUT
    assert row.cached_input_tokens == doubles.USAGE_CACHED
    # Tokens are measured; a price is not. See `services/recipes.py` for why this
    # stays zero rather than being derived from a rate card that would go stale.
    assert row.cost_micro == 0
    # What was sent, so a bad suggestion can be explained three months later.
    assert [item["name"] for item in row.stock_snapshot["items"]] == ["Crème fraîche épaisse"]
    assert row.stock_snapshot["notes"] == "rapide, sans four"
    assert row.payload["ingredients"][0]["in_stock"] is False, "the corrected flag is stored"


async def test_location_ids_narrow_the_stock_that_is_sent(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
    make_location: MakeLocation,
    make_product: MakeProduct,
) -> None:
    household = await make_household()
    await add_config(db_session, household)
    fridge = await stock_cream(api_client, household, make_location, make_product)
    await stock_cream(
        api_client,
        household,
        make_location,
        make_product,
        name="Placard",
        kind=StorageKind.PANTRY,
        product_name="Riz basmati",
    )
    use_provider_double(api_app, household, "nominal")

    response = await api_client.post(
        SUGGEST_URL,
        headers=household_headers(household),
        json={"location_ids": [fridge]},
    )

    assert response.status_code == 200, response.text
    row = (
        await db_session.scalars(
            select(RecipeSuggestion).where(RecipeSuggestion.household_id == household.id)
        )
    ).first()
    assert row is not None
    assert [item["name"] for item in row.stock_snapshot["items"]] == ["Crème fraîche épaisse"]
    assert row.stock_snapshot["location_ids"] == [fridge]


async def test_an_unknown_location_is_a_404_not_an_empty_inventory(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
    make_location: MakeLocation,
    make_product: MakeProduct,
) -> None:
    household = await make_household()
    await add_config(db_session, household)
    stranger = await make_household()
    elsewhere = await make_location(stranger, name="Frigo du voisin")
    use_provider_double(api_app, household, "nominal")

    response = await api_client.post(
        SUGGEST_URL,
        headers=household_headers(household),
        json={"location_ids": [str(elsewhere.id)]},
    )

    assert response.status_code == 404, response.text
    assert response.json()["type"] == "https://chaudron.dev/problems/location-not-found"


async def test_the_number_of_suggestions_is_capped(
    api_client: httpx.AsyncClient, db_session: AsyncSession, make_household: MakeHousehold
) -> None:
    """A ceiling on an endpoint that bills the user for every extra answer."""
    household = await make_household()
    await add_config(db_session, household)

    response = await api_client.post(
        SUGGEST_URL, headers=household_headers(household), json={"max_suggestions": 42}
    )

    assert response.status_code == 422, response.text
    assert response.json()["type"] == "https://chaudron.dev/problems/validation-failed"


@pytest.mark.parametrize(
    ("scenario", "expected"),
    [
        ("connection_refused", 503),
        ("timeout", 503),
        ("server_error", 503),
        ("rate_limited", 429),
        ("quota_exhausted", 429),
        ("malformed_payload", 502),
        ("schema_violation", 502),
    ],
)
async def test_provider_failures_map_onto_status_codes_without_leaking_the_key(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
    make_location: MakeLocation,
    make_product: MakeProduct,
    scenario: str,
    expected: int,
) -> None:
    household = await make_household()
    await add_config(db_session, household)
    await stock_cream(api_client, household, make_location, make_product)
    use_provider_double(api_app, household, scenario)

    response = await api_client.post(SUGGEST_URL, headers=household_headers(household), json={})

    assert response.status_code == expected, response.text
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["type"].startswith("https://chaudron.dev/problems/provider-")
    # The doubles put a key-shaped string in the failures they raise, exactly as
    # the real SDKs do. None of it may reach the client, in any field.
    assert doubles.LEAKY_KEY not in response.text
    assert "sk-" not in response.text
    assert _INSTANCE_KEY not in response.text
    assert "Traceback" not in response.text

    written = (
        await db_session.scalars(
            select(RecipeSuggestion).where(RecipeSuggestion.household_id == household.id)
        )
    ).all()
    assert not written, "a failed call must not leave a suggestion behind"


async def test_a_withdrawn_consent_sends_nothing_to_the_provider(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
    make_location: MakeLocation,
    make_product: MakeProduct,
) -> None:
    """The refusal is measured by what left the process, not by the status code.

    A 409 proves the request failed; it does not prove the household's inventory
    stayed home. So the transport here is one that cannot answer: if the consent gate
    is bypassed -- or moved below the point where the credential is decrypted and the
    prompt assembled -- this test fails with the socket's own error rather than with
    a polite assertion, which is the outcome that says the gate is real.

    This mirrors how the Todoist chain was verified under penetration test, where the
    equivalent check was "outbound calls: 0" after a withdrawal.
    """
    household = await make_household()
    await stock_cream(api_client, household, make_location, make_product)
    await add_config(db_session, household, consent_revoked=True)

    def refuse_to_be_called() -> ProviderPortsBuilder:
        def explode(request: httpx.Request) -> httpx.Response:
            raise AssertionError(
                f"the provider was contacted after consent was withdrawn: {request.url}"
            )

        settings = LlmSettings(
            instance_owner_household_id=household.id,
            instance_owner_api_key=_INSTANCE_KEY,
            timeout_seconds=5.0,
        )
        transport = httpx.MockTransport(explode)

        def build(provider_code: str) -> ProviderPorts:
            return LlmProviderFactory(settings, transport=transport)

        return build

    api_app.dependency_overrides[get_provider_ports_builder] = refuse_to_be_called

    response = await api_client.post(
        SUGGEST_URL, headers=household_headers(household), json={"max_suggestions": 3}
    )

    assert response.status_code == 409, response.text
    assert response.json()["type"] == "https://chaudron.dev/problems/provider-not-configured"
