"""The feedback loop: one verdict per suggestion, and what it is allowed to change.

Four properties are worth more than the write path itself:

* the latest verdict wins, and ``status`` is dragged along with it -- the database
  refuses a row where the lifecycle and the measurement disagree, so a service
  that wrote one without the other would fail at flush rather than quietly;
* a dismissal **demotes and never removes**. The test that matters here runs the
  same generation twice and asserts the dismissed dish is still offered, lower.
  Confusing the two would shrink the space of proposals a little more with every
  tap, invisibly;
* the rate is withheld on a thin sample. "1 avis sur 1" must never render as
  "100 %", and the server -- not the client -- is what refuses to divide;
* another household's suggestion is a 404, not a 403 and not a write.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from chaudron.api.deps import get_provider_ports_builder
from chaudron.domain.models import (
    Household,
    LlmProviderMode,
    RecipeFeedback,
    RecipeStatus,
    RecipeSuggestion,
)
from chaudron.infra.llm import doubles
from chaudron.infra.llm.factory import LlmProviderFactory
from chaudron.infra.llm.settings import LlmSettings
from chaudron.services.providers import ProviderPorts, ProviderPortsBuilder
from chaudron.services.recipe_feedback import MIN_RESPONSES_FOR_RATE
from tests.api.conftest import MakeLocation, MakeProduct
from tests.api.test_providers import add_config
from tests.api.test_recipes import stock_cream, use_provider_double
from tests.conftest import MakeHousehold, household_headers

SUGGEST_URL = "/v1/recipes/suggest"
QUALITY_URL = "/v1/recipes/quality"

#: Two dishes that draw on the same single ingredient, so they tie on every
#: ranking key contract 5 fixes -- same urgency (none), same PNNS markers (none).
#: The only thing that can separate them is the feedback key, which is exactly
#: what the ordering test needs to observe.
_FIRST = "Gratin de courgettes"
_SECOND = "Poêlée de légumes"


def _feedback_url(suggestion_id: str) -> str:
    return f"/v1/recipes/suggestions/{suggestion_id}/feedback"


def _two_suggestions_payload() -> str:
    return json.dumps(
        {
            "suggestions": [
                {
                    "title": title,
                    "summary": "Deux plats interchangeables pour le classement.",
                    "duration_minutes": 20,
                    "servings": 2,
                    "uses_expiring_soon": False,
                    "ingredients": [
                        {"name": "Crème", "amount": "20", "unit": "cl", "in_stock": True}
                    ],
                    "steps": ["Chauffer.", "Servir."],
                }
                for title in (_FIRST, _SECOND)
            ]
        }
    )


def _two_suggestion_transport() -> httpx.MockTransport:
    """The OpenAI envelope the nominal double uses, carrying two recipes.

    Local to this module rather than a ninth entry in ``doubles.SCENARIOS``: that
    tuple is the conformance matrix every adapter is driven through
    (``tests/contracts``), and a scenario added for one ranking assertion would
    make five adapters answer a question none of them is being asked.
    """
    body = {
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": _two_suggestions_payload()}}
        ],
        "usage": {
            "prompt_tokens": doubles.USAGE_INPUT + doubles.USAGE_CACHED,
            "completion_tokens": doubles.USAGE_OUTPUT,
            "total_tokens": doubles.USAGE_INPUT + doubles.USAGE_CACHED + doubles.USAGE_OUTPUT,
            "prompt_tokens_details": {"cached_tokens": doubles.USAGE_CACHED},
        },
    }
    return httpx.MockTransport(lambda _request: httpx.Response(200, json=body))


def use_two_suggestion_double(app: FastAPI, household: Household) -> None:
    def build_ports() -> ProviderPortsBuilder:
        settings = LlmSettings(
            instance_owner_household_id=household.id,
            instance_owner_api_key="sk-test-instance-owner-key",
            timeout_seconds=5.0,
        )
        transport = _two_suggestion_transport()

        def build(provider_code: str) -> ProviderPorts:
            return LlmProviderFactory(settings, transport=transport)

        return build

    app.dependency_overrides[get_provider_ports_builder] = build_ports


async def _suggest(api_client: httpx.AsyncClient, household: Household) -> list[dict[str, Any]]:
    response = await api_client.post(
        SUGGEST_URL, headers=household_headers(household), json={"max_suggestions": 5}
    )
    assert response.status_code == 200, response.text
    suggestions: list[dict[str, Any]] = response.json()["suggestions"]
    return suggestions


async def _answered_row(
    session: AsyncSession,
    household: Household,
    *,
    model: str,
    verdict: RecipeFeedback,
    title: str = "Plat mesuré",
) -> RecipeSuggestion:
    """A suggestion carrying a verdict, written straight to the table.

    The generation path costs a full provider round trip per row, and the
    aggregate under test only reads four columns. Every constraint still applies:
    ``status`` and ``cooked_at`` are set the way the CHECK demands, so a row that
    the service could not have produced cannot be inserted here either.
    """
    now = datetime.now(UTC)
    cooked = verdict is RecipeFeedback.COOKED
    suggestion = RecipeSuggestion(
        id=uuid.uuid7(),
        household_id=household.id,
        title=title,
        summary=None,
        servings=2,
        payload={"title": title, "ingredients": [], "steps": []},
        stock_snapshot={"items": []},
        provider_mode=LlmProviderMode.OLLAMA,
        provider_code="ollama",
        model=model,
        prompt_version="test",
        status=RecipeStatus.COOKED if cooked else RecipeStatus.DISCARDED,
        cooked_at=now if cooked else None,
        feedback=verdict,
        feedback_at=now,
    )
    session.add(suggestion)
    await session.flush()
    return suggestion


# --------------------------------------------------------------------------- #
# Writing a verdict
# --------------------------------------------------------------------------- #


async def test_a_cooked_verdict_moves_the_lifecycle_with_it(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
    make_location: MakeLocation,
    make_product: MakeProduct,
) -> None:
    household = await make_household()
    await add_config(db_session, household)
    await stock_cream(api_client, household, make_location, make_product)
    use_provider_double(api_app, household, "nominal")
    suggestion_id = (await _suggest(api_client, household))[0]["id"]

    response = await api_client.put(
        _feedback_url(suggestion_id),
        headers=household_headers(household),
        json={"feedback": "cooked"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"suggestion_id", "feedback", "feedback_at", "status"}
    assert body["suggestion_id"] == suggestion_id
    assert body["feedback"] == "cooked"
    assert body["status"] == "cooked"
    assert body["feedback_at"].endswith("Z"), "the contract's timestamps end in Z"

    await db_session.commit()
    row = await db_session.get(RecipeSuggestion, uuid.UUID(suggestion_id))
    assert row is not None
    await db_session.refresh(row)
    assert row.feedback is RecipeFeedback.COOKED
    assert row.status is RecipeStatus.COOKED
    # The CHECK demands it, and the interface needs it: "cooked" with no date is a
    # claim the row is not allowed to make.
    assert row.cooked_at is not None
    assert row.feedback_at is not None
    # No user accounts in this slice, so nobody is attributed the opinion.
    assert row.feedback_by_user_id is None


async def test_the_latest_verdict_wins_and_the_first_cooking_date_survives(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
    make_location: MakeLocation,
    make_product: MakeProduct,
) -> None:
    household = await make_household()
    await add_config(db_session, household)
    await stock_cream(api_client, household, make_location, make_product)
    use_provider_double(api_app, household, "nominal")
    suggestion_id = (await _suggest(api_client, household))[0]["id"]
    headers = household_headers(household)

    await api_client.put(_feedback_url(suggestion_id), headers=headers, json={"feedback": "cooked"})
    await db_session.commit()
    first_row = await db_session.get(RecipeSuggestion, uuid.UUID(suggestion_id))
    assert first_row is not None
    await db_session.refresh(first_row)
    cooked_at = first_row.cooked_at

    changed = await api_client.put(
        _feedback_url(suggestion_id), headers=headers, json={"feedback": "not_interested"}
    )

    assert changed.status_code == 200, changed.text
    assert changed.json()["feedback"] == "not_interested"
    assert changed.json()["status"] == "discarded"
    await db_session.commit()
    await db_session.refresh(first_row)
    assert first_row.feedback is RecipeFeedback.NOT_INTERESTED
    # A dish cooked in the past was still cooked. The stock movements of that day
    # say so, and erasing the date here would make this table contradict them.
    assert first_row.cooked_at == cooked_at


async def test_withdrawing_a_verdict_restores_the_lifecycle(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
    make_location: MakeLocation,
    make_product: MakeProduct,
) -> None:
    household = await make_household()
    await add_config(db_session, household)
    await stock_cream(api_client, household, make_location, make_product)
    use_provider_double(api_app, household, "nominal")
    suggestion_id = (await _suggest(api_client, household))[0]["id"]
    headers = household_headers(household)
    await api_client.put(
        _feedback_url(suggestion_id), headers=headers, json={"feedback": "not_interested"}
    )

    cleared = await api_client.delete(_feedback_url(suggestion_id), headers=headers)

    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["feedback"] is None
    assert cleared.json()["feedback_at"] is None
    # Never left `discarded`: the household said "ignore what I said", which is not
    # the same sentence as "throw this away".
    assert cleared.json()["status"] == "generated"

    # Idempotent, so a double tap on a flaky connection is not an error.
    again = await api_client.delete(_feedback_url(suggestion_id), headers=headers)
    assert again.status_code == 200, again.text
    assert again.json()["feedback"] is None


async def test_an_unknown_suggestion_is_a_404(
    api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    household = await make_household()

    response = await api_client.put(
        _feedback_url(str(uuid.uuid7())),
        headers=household_headers(household),
        json={"feedback": "cooked"},
    )

    assert response.status_code == 404, response.text
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["type"] == "https://chaudron.dev/problems/recipe-suggestion-not-found"


async def test_another_households_suggestion_is_indistinguishable_from_an_unknown_one(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
    make_location: MakeLocation,
    make_product: MakeProduct,
) -> None:
    owner = await make_household()
    stranger = await make_household()
    await add_config(db_session, owner)
    await stock_cream(api_client, owner, make_location, make_product)
    use_provider_double(api_app, owner, "nominal")
    suggestion_id = (await _suggest(api_client, owner))[0]["id"]

    response = await api_client.put(
        _feedback_url(suggestion_id),
        headers=household_headers(stranger),
        json={"feedback": "cooked"},
    )

    assert response.status_code == 404, response.text
    # Byte for byte the answer an unknown identifier gets: a distinguishable one
    # would confirm the existence of another household's row.
    assert response.json()["type"] == "https://chaudron.dev/problems/recipe-suggestion-not-found"
    await db_session.commit()
    row = await db_session.get(RecipeSuggestion, uuid.UUID(suggestion_id))
    assert row is not None
    await db_session.refresh(row)
    assert row.feedback is None, "the write was refused, not merely hidden"


@pytest.mark.parametrize("verdict", ["", "loved_it", "5", "cooked_twice"])
async def test_the_vocabulary_is_closed(
    api_client: httpx.AsyncClient, make_household: MakeHousehold, verdict: str
) -> None:
    household = await make_household()

    response = await api_client.put(
        _feedback_url(str(uuid.uuid7())),
        headers=household_headers(household),
        json={"feedback": verdict},
    )

    assert response.status_code == 422, response.text


# --------------------------------------------------------------------------- #
# What the verdict is allowed to change
# --------------------------------------------------------------------------- #


async def test_a_dismissed_dish_sinks_but_is_still_offered(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
    make_location: MakeLocation,
    make_product: MakeProduct,
) -> None:
    """The single most important assertion of the feature.

    The two dishes tie on everything contract 5 ranks above feedback, so the order
    of the second run isolates the feedback key. What is asserted is *both* halves:
    the dismissed dish moved down, **and** it is still there. A filter would pass
    the first half and fail the second, which is why asserting the order alone
    would not be enough.
    """
    household = await make_household()
    await add_config(db_session, household)
    await stock_cream(api_client, household, make_location, make_product)
    use_two_suggestion_double(api_app, household)

    first_run = await _suggest(api_client, household)
    assert [entry["title"] for entry in first_run] == [_FIRST, _SECOND]

    dismissed = await api_client.put(
        _feedback_url(first_run[0]["id"]),
        headers=household_headers(household),
        json={"feedback": "not_interested"},
    )
    assert dismissed.status_code == 200, dismissed.text

    second_run = await _suggest(api_client, household)

    titles = [entry["title"] for entry in second_run]
    assert titles == [_SECOND, _FIRST], "the dismissed dish is ranked last"
    assert _FIRST in titles, "demoted, never filtered out"


async def test_changing_ones_mind_lifts_the_demotion(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
    make_location: MakeLocation,
    make_product: MakeProduct,
) -> None:
    household = await make_household()
    await add_config(db_session, household)
    await stock_cream(api_client, household, make_location, make_product)
    use_two_suggestion_double(api_app, household)
    headers = household_headers(household)
    first_run = await _suggest(api_client, household)
    await api_client.put(
        _feedback_url(first_run[0]["id"]), headers=headers, json={"feedback": "not_interested"}
    )

    await api_client.delete(_feedback_url(first_run[0]["id"]), headers=headers)

    # One opinion per suggestion, so withdrawing it removes the demotion at once:
    # there is no ledger of past dismissals to keep the dish down.
    assert [entry["title"] for entry in await _suggest(api_client, household)] == [_FIRST, _SECOND]


async def test_a_cooked_verdict_does_not_promote(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
    make_location: MakeLocation,
    make_product: MakeProduct,
) -> None:
    """A meal eaten on Sunday is not evidence the household wants it on Monday.

    Boosting what was cooked would narrow the offer just as effectively as
    filtering what was dismissed, only in the flattering direction where nobody
    notices. The order is left exactly as the ranking above it decided.
    """
    household = await make_household()
    await add_config(db_session, household)
    await stock_cream(api_client, household, make_location, make_product)
    use_two_suggestion_double(api_app, household)
    first_run = await _suggest(api_client, household)
    await api_client.put(
        _feedback_url(first_run[1]["id"]),
        headers=household_headers(household),
        json={"feedback": "cooked"},
    )

    assert [entry["title"] for entry in await _suggest(api_client, household)] == [_FIRST, _SECOND]


async def test_the_prompt_never_carries_a_verdict(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
    make_location: MakeLocation,
    make_product: MakeProduct,
) -> None:
    """Feedback is contract 4bis's "calculable par l'application" class.

    The application measures and the model writes. This asserts the negative that
    keeps that true: nothing about a past verdict reaches the wire, so the ranking
    stays a deterministic sort rather than a request a model may ignore.
    """
    household = await make_household()
    await add_config(db_session, household)
    await stock_cream(api_client, household, make_location, make_product)

    sent: list[bytes] = []

    def record(request: httpx.Request) -> httpx.Response:
        sent.append(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": _two_suggestions_payload()},
                    }
                ]
            },
        )

    def build_ports() -> ProviderPortsBuilder:
        settings = LlmSettings(
            instance_owner_household_id=household.id,
            instance_owner_api_key="sk-test-instance-owner-key",
            timeout_seconds=5.0,
        )
        transport = httpx.MockTransport(record)

        def build(provider_code: str) -> ProviderPorts:
            return LlmProviderFactory(settings, transport=transport)

        return build

    api_app.dependency_overrides[get_provider_ports_builder] = build_ports
    first_run = await _suggest(api_client, household)
    await api_client.put(
        _feedback_url(first_run[0]["id"]),
        headers=household_headers(household),
        json={"feedback": "not_interested"},
    )
    sent.clear()

    await _suggest(api_client, household)

    assert sent, "the second run really called the provider"
    wire = b"".join(sent).lower()
    for forbidden in (b"not_interested", b"not interested", b"feedback", b"dislike"):
        assert forbidden not in wire
    # The dismissed *title* is absent too. It is in the household's history, not in
    # the stock, and the prompt only ever carries the stock.
    assert _FIRST.lower().encode() not in wire


# --------------------------------------------------------------------------- #
# The aggregate
# --------------------------------------------------------------------------- #


async def test_a_thin_sample_shows_counts_and_no_rate(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
) -> None:
    household = await make_household()
    await _answered_row(db_session, household, model="llama3", verdict=RecipeFeedback.COOKED)
    await _answered_row(
        db_session, household, model="llama3", verdict=RecipeFeedback.NOT_INTERESTED
    )
    await db_session.commit()

    response = await api_client.get(QUALITY_URL, headers=household_headers(household))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["min_responses"] == MIN_RESPONSES_FOR_RATE
    assert len(body["models"]) == 1
    entry = body["models"][0]
    assert entry["provider_mode"] == "ollama"
    assert entry["model"] == "llama3"
    assert (entry["cooked"], entry["not_interested"], entry["responses"]) == (1, 1, 2)
    # "1 avis sur 2" is a fact; "50 %" is the same fact dressed as a measurement.
    assert entry["cooked_rate"] is None


async def test_the_rate_appears_once_the_sample_is_thick_enough(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
) -> None:
    household = await make_household()
    cooked = MIN_RESPONSES_FOR_RATE - 2
    for index in range(cooked):
        await _answered_row(
            db_session,
            household,
            model="llama3",
            verdict=RecipeFeedback.COOKED,
            title=f"Plat {index}",
        )
    for index in range(2):
        await _answered_row(
            db_session,
            household,
            model="llama3",
            verdict=RecipeFeedback.NOT_INTERESTED,
            title=f"Refus {index}",
        )
    await db_session.commit()

    response = await api_client.get(QUALITY_URL, headers=household_headers(household))

    entry = response.json()["models"][0]
    assert entry["responses"] == MIN_RESPONSES_FOR_RATE
    assert entry["cooked_rate"] == pytest.approx(cooked / MIN_RESPONSES_FOR_RATE)


async def test_the_aggregate_never_crosses_households(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
) -> None:
    mine = await make_household()
    theirs = await make_household()
    await _answered_row(db_session, mine, model="llama3", verdict=RecipeFeedback.COOKED)
    await _answered_row(db_session, theirs, model="gpt-4o", verdict=RecipeFeedback.COOKED)
    await db_session.commit()

    body = (await api_client.get(QUALITY_URL, headers=household_headers(mine))).json()

    assert [entry["model"] for entry in body["models"]] == ["llama3"]


async def test_an_unanswered_household_gets_an_empty_report(
    api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    household = await make_household()

    response = await api_client.get(QUALITY_URL, headers=household_headers(household))

    assert response.status_code == 200, response.text
    # An empty list, not a 404: "nobody has answered yet" is a state the screen
    # renders, not a failure.
    assert response.json() == {"min_responses": MIN_RESPONSES_FOR_RATE, "models": []}


async def test_the_grouping_separates_models_of_the_same_provider(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
) -> None:
    """The reason the index is ``(provider_mode, model, feedback)`` and not just
    the first two: "ollama is worse" is not a finding, "ollama with a 3B model is
    worse than ollama with a 70B one" is."""
    household = await make_household()
    await _answered_row(db_session, household, model="llama3:8b", verdict=RecipeFeedback.COOKED)
    await _answered_row(
        db_session, household, model="llama3:70b", verdict=RecipeFeedback.NOT_INTERESTED
    )
    await db_session.commit()

    body = (await api_client.get(QUALITY_URL, headers=household_headers(household))).json()

    assert {entry["model"]: entry["cooked"] for entry in body["models"]} == {
        "llama3:8b": 1,
        "llama3:70b": 0,
    }


async def test_a_suggestion_nobody_answered_about_is_not_counted(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
    make_location: MakeLocation,
    make_product: MakeProduct,
) -> None:
    household = await make_household()
    await add_config(db_session, household)
    await stock_cream(api_client, household, make_location, make_product)
    use_provider_double(api_app, household, "nominal")
    await _suggest(api_client, household)

    body = (await api_client.get(QUALITY_URL, headers=household_headers(household))).json()

    # Matches the partial index predicate. A silent count of generated-but-unrated
    # rows would drown the signal in the noise the feature exists to filter out.
    assert body["models"] == []
    rows = (
        await db_session.scalars(
            select(RecipeSuggestion).where(RecipeSuggestion.household_id == household.id)
        )
    ).all()
    assert len(rows) == 1
