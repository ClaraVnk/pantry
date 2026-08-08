"""Recipe suggestions: from what the household really owns to a persisted answer.

Five rules live here and are the reason this layer exists at all.

*The stock comes from the database, never from the client.* The request names
locations; the contents of those locations are read server-side.

*What an eater cannot have never reaches the model.* The screened-out half of the
inventory is removed before the call, so the model cannot propose what it never
saw, and every ingredient it returns is matched back afterwards. An ingredient
that does not resolve -- or resolves to something withheld -- discards the whole
suggestion. Neither half is a prompt instruction: a "no peanuts" line in a prompt
is a polite request to a component allowed to be wrong (ADR-0009).

*``in_stock`` is recomputed.* The model is asked to flag which ingredients the
household already has, and its answer is thrown away: a suggestion that claims the
eggs are in the fridge when they are not sends someone to cook with nothing. The
flag returned to the client comes from the same matcher the safety check uses --
one matcher, so the display and the guarantee cannot disagree.

*The balance is arithmetic, and the expiry pressure is measured.* Both are soft:
they order the suggestions and they are stated, never enforced. Contract 5 fixes
the order between them -- hard constraints, then expiry, then balance -- because
what is about to spoil is sometimes exactly what a household should not eat, and
the tie has to be broken somewhere visible. Past feedback joins them as a fourth
and last key (``services/recipe_feedback.py``): a dish the household dismissed
sinks among suggestions already tied on everything above, and is never removed.

*Every generated suggestion is written down.* ``recipe_suggestion`` keeps the
provider mode, the model, the prompt version, the latency and the stock snapshot
that produced it. What it deliberately does **not** keep is who it was cooked
for: an eater's allergies are erased with their row (``domain/models.py``), and a
copy surviving in a three-month-old JSONB payload would make that erasure a lie.

The write goes through the request's session rather than a repository: this slice
adds one writer and one table, and a repository port with a single implementation
and a single caller would be indirection without a second reader to justify it.
"""

from __future__ import annotations

import enum
import logging
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncSession

from chaudron.domain.balance import WeeklyBalance, shortfall_sentence
from chaudron.domain.constraints import (
    ALLERGEN_LABELS,
    HouseholdConstraints,
    MealTemperature,
    Resolution,
    StockIndex,
    StockLine,
    staples_allowed_for,
)
from chaudron.domain.labels.normalization import to_comparison_form
from chaudron.domain.llm_ports import InventoryItem as ProviderInventoryItem
from chaudron.domain.llm_ports import RecipeRequest, TokenUsage
from chaudron.domain.llm_ports import RecipeSuggestion as GeneratedRecipe
from chaudron.domain.models import (
    AgeBand,
    Allergen,
    AllergenDataState,
    Diet,
    InfantTexture,
    PnnsMarker,
    RecipeStatus,
    RecipeSuggestion,
)
from chaudron.domain.ports import (
    ConstraintViolationDetectedError,
    LocationNotFoundError,
    LocationRepository,
    NoSuggestionWithinConstraintsError,
)
from chaudron.infra.llm.prompts import PROMPT_VERSION
from chaudron.services.balance import BalanceService
from chaudron.services.dietary import DietaryService, ScreenedStock
from chaudron.services.providers import ActiveProvider, ProviderService
from chaudron.services.recipe_feedback import RecipeFeedbackService

logger = logging.getLogger(__name__)

#: The suggestions are written for the household, not for the developer.
RESPONSE_LANGUAGE: Final = "fr"

#: How many stock lines one call may carry. Past this the prompt is mostly noise,
#: the answer is not better, and every item is tokens somebody pays for. The
#: provider layer trims further when the model's context window demands it.
MAX_INVENTORY_ITEMS: Final = 300

#: What "urgent" means, in days. Three rather than seven: a household cooks
#: tonight, and a widget that calls a week-old yoghurt and a week-away packet of
#: rice equally urgent stops meaning anything. Anything already past its date is
#: urgent too -- ``days_left`` simply goes negative.
URGENT_WITHIN_DAYS: Final = 3

#: The sentence contract 5 asks for when nothing on offer saved what is spoiling.
#: Written here, in French, because it is displayed verbatim.
NOTHING_URGENT_USED: Final = (
    "Aucune suggestion n'utilise les produits les plus urgents : les contraintes "
    "ou le reste du stock ne le permettent pas."
)


class BalanceMode(enum.StrEnum):
    """Whether the weekly balance is computed at all. Never a filter either way."""

    WEEKLY = "weekly"
    OFF = "off"


@dataclass(frozen=True, slots=True)
class SuggestRecipesCommand:
    location_ids: tuple[uuid.UUID, ...] = ()
    max_suggestions: int = 3
    notes: str | None = None
    #: Who is eating. Empty means everybody recorded in the household -- the
    #: union of their constraints, never a subset chosen by the server.
    member_ids: tuple[uuid.UUID, ...] = ()
    balance_mode: BalanceMode = BalanceMode.WEEKLY
    meal_temperature: MealTemperature = MealTemperature.ANY


@dataclass(frozen=True, slots=True)
class SuggestedIngredient:
    name: str
    amount: str | None
    unit: str | None
    in_stock: bool


@dataclass(frozen=True, slots=True)
class UrgentItem:
    """One lot the suggestion is credited with saving, and when it runs out.

    ``expires_on`` is the effective date (``domain/shelf_life.py``), so that it
    and ``days_left`` cannot disagree: a reader who subtracts today from the
    first must land on the second, and the printed date on an opened pot would
    not. What is written on the packaging is on the inventory screen.
    """

    inventory_item_id: uuid.UUID
    product_name: str
    expires_on: date | None
    days_left: int


@dataclass(frozen=True, slots=True)
class ExpiryPressure:
    """What the suggestion saved from the bin, and what it left there.

    ``urgent_items_left_unused`` is the honest field. Without it a list of
    recipes that all ignore the yoghurt expiring tomorrow reads as a deliberate
    choice rather than as a limitation (contract 5).
    """

    items_used_expiring_within_days: int
    urgent_items: tuple[UrgentItem, ...]
    urgent_items_left_unused: int


@dataclass(frozen=True, slots=True)
class AllergenAssessment:
    """What can honestly be said about one suggestion's allergens.

    ``statement`` is written here and rendered verbatim: it is negative and
    situated by construction -- "no allergen declared among the products
    identified", never "free of nuts" -- and the client is forbidden from
    composing its own from the other two fields (ADR-0009).
    """

    declared_clear_of: tuple[Allergen, ...]
    unverified_product_count: int
    statement: str


@dataclass(frozen=True, slots=True)
class Preparation:
    """Self-declared by the model, and labelled as such. ``None`` means silent."""

    serving_temperature: str | None
    requires_cooking: bool | None
    requires_oven: bool | None


@dataclass(frozen=True, slots=True)
class SuggestedRecipe:
    id: uuid.UUID
    title: str
    summary: str
    duration_minutes: int | None
    servings: int | None
    ingredients: tuple[SuggestedIngredient, ...]
    steps: tuple[str, ...]
    uses_expiring_soon: bool
    allergen_assessment: AllergenAssessment
    expiry_pressure: ExpiryPressure
    preparation: Preparation


@dataclass(frozen=True, slots=True)
class AppliedConstraints:
    """What was applied, in counts and codes, and never in names.

    The members are named because the caller sent their ids and is entitled to
    read back who it asked for. What is *not* here is which of them contributed
    which constraint: a household screen showing "excluded: peanuts (Camille)"
    would publish one person's medical fact to everybody who opens the tablet.
    """

    members: tuple[tuple[uuid.UUID, str], ...]
    excluded_allergens: tuple[Allergen, ...]
    diet: Diet | None
    infant_texture: InfantTexture | None
    age_bands: tuple[AgeBand, ...]
    products_withheld: int
    products_unverified: int


@dataclass(frozen=True, slots=True)
class SuggestionSet:
    provider_mode: str
    model: str
    recipes: tuple[SuggestedRecipe, ...]
    applied_constraints: AppliedConstraints
    balance: WeeklyBalance | None = None
    #: What the call consumed, or ``None`` when the provider did not report it.
    #: A property of the *call*, not of any one recipe: one request produces up to
    #: ``max_suggestions`` recipes from a single completion.
    usage: TokenUsage | None = None


class RecipeService:
    def __init__(
        self,
        session: AsyncSession,
        providers: ProviderService,
        locations: LocationRepository,
        dietary: DietaryService,
        balance: BalanceService,
        feedback: RecipeFeedbackService,
    ) -> None:
        self._session = session
        self._providers = providers
        self._locations = locations
        self._dietary = dietary
        self._balance = balance
        self._feedback = feedback

    async def suggest(
        self, household_id: uuid.UUID, command: SuggestRecipesCommand
    ) -> SuggestionSet:
        """Ask the household's provider for recipes, and record what it answered.

        Raises the domain provider errors unchanged; the API layer owns the mapping
        onto status codes, and none of them may quote a provider's own message.
        """
        provider = await self._providers.for_recipes(household_id)
        constraints = await self._dietary.constraints_for(household_id, command.member_ids)
        rules = await self._dietary.infant_rules(constraints.age_bands)
        await self._check_locations(household_id, command.location_ids)

        stock = await self._dietary.stock(
            household_id, command.location_ids, limit=MAX_INVENTORY_ITEMS
        )
        screened = self._dietary.screen(stock, constraints, rules)
        if stock and not screened.offered:
            # Not an execution failure. The interface explains which class of
            # constraint emptied the inventory instead of showing a broken model.
            raise NoSuggestionWithinConstraintsError(
                reasons=tuple(sorted(reason.value for reason in screened.reasons)),
                withheld=screened.withheld,
            )

        today = datetime.now(UTC).date()
        balance = await self._weekly_balance(household_id, command, screened)
        staples = staples_allowed_for(constraints, rules)
        strict = bool(constraints.excluded_allergens) or constraints.diet is not Diet.OMNIVORE
        strict = strict or bool(rules)

        request = RecipeRequest(
            inventory=tuple(_to_provider_item(line, today) for line in screened.offered),
            max_suggestions=command.max_suggestions,
            notes=command.notes or None,
            language=RESPONSE_LANGUAGE,
            preferences=constraints.preferences,
            balance_hint=None if balance is None else shortfall_sentence(balance.gaps),
            meal_temperature=command.meal_temperature.value,
            infant_texture=(
                None if constraints.infant_texture is None else constraints.infant_texture.value
            ),
            allowed_staples=tuple(staple.label for staple in staples),
            strict_inventory=strict,
        )
        started = time.perf_counter()
        generated = await provider.generator.suggest(request)
        latency_ms = int((time.perf_counter() - started) * 1000)

        notice = getattr(generated, "degradation_notice", None)
        if isinstance(notice, str):
            # Not returned to the client: the answer shape is fixed by the contract,
            # and the persistent banner is served by the capabilities endpoint.
            logger.info("recipe_suggestions_degraded", extra={"notice": notice})

        usage = getattr(generated, "usage", None)
        if not isinstance(usage, TokenUsage):
            usage = None

        index = StockIndex(screened.offered, staples)
        urgent = tuple(
            line
            for line in screened.offered
            if (days := line.days_left(today)) is not None and days <= URGENT_WITHIN_DAYS
        )
        candidates = list(generated[: command.max_suggestions])
        accepted = self._check(candidates, index, strict=strict)
        if candidates and not accepted:
            raise ConstraintViolationDetectedError(examined=len(candidates))

        gap_markers: frozenset[PnnsMarker] = (
            frozenset() if balance is None else frozenset(gap.marker for gap in balance.gaps)
        )
        dispreferred = await self._feedback.dispreferred_titles(household_id)
        # Contract 5 fixes the first two keys and says why: what is about to spoil
        # is the problem this application exists to solve, and the weekly balance
        # only breaks ties between recipes that are equally urgent. It never makes
        # a product get thrown away to respect a benchmark.
        #
        # Past feedback is the *third* key, below both, and that placement is the
        # decision rather than an accident of ordering. Contract 5 fixes an order
        # this signal has no standing to disturb: a dish the household waved away
        # last month that happens to be the only one using tomorrow's yoghurt must
        # still come first. So a dismissal only separates suggestions that are
        # already tied on everything that matters more -- it demotes, and it can
        # never suppress. Nothing here promotes: "cooked" leaves the order
        # untouched, because a meal eaten on Sunday is not evidence the household
        # wants it again on Monday, and boosting it would narrow the offer in the
        # other direction just as effectively as filtering would.
        ranked = sorted(
            accepted,
            key=lambda entry: (
                -len(_urgent_used(entry[1], urgent)),
                -len(_markers_used(entry[1]) & gap_markers),
                to_comparison_form(entry[0].title) in dispreferred,
            ),
        )

        snapshot = _snapshot(request, command, screened, balance)
        recipes = tuple(
            self._persist(
                household_id,
                provider,
                recipe,
                resolutions=resolutions,
                constraints=constraints,
                urgent=urgent,
                today=today,
                snapshot=snapshot,
                latency_ms=latency_ms,
                # The whole call's usage lands on the first row of the batch and
                # nowhere else. One completion produces N suggestions, so copying it
                # onto each would multiply the household's token spend by N in any
                # `sum()` over the table -- and these columns exist to be summed
                # (`domain/models.py`: "aggregable for the operator"). Attributing it
                # once keeps the total exact; the price is that rows 2..N read as
                # zero-cost, which is why the *call*-level figure is what the API
                # returns and what an interface should show.
                usage=usage if position == 0 else None,
            )
            for position, (recipe, resolutions) in enumerate(ranked)
        )
        await self._session.flush()
        if urgent and not any(recipe.expiry_pressure.urgent_items for recipe in recipes):
            balance = _append_note(balance, NOTHING_URGENT_USED)
        return SuggestionSet(
            provider_mode=provider.config.mode.value,
            model=provider.config.model,
            recipes=recipes,
            applied_constraints=_applied(constraints, screened),
            balance=balance,
            usage=usage,
        )

    async def _check_locations(
        self, household_id: uuid.UUID, location_ids: Sequence[uuid.UUID]
    ) -> None:
        """An unknown location is a 404 rather than an empty inventory.

        Silently suggesting recipes from nothing would look like a broken model
        instead of a stale identifier in the client.
        """
        for location_id in location_ids:
            if not await self._locations.exists(household_id, location_id):
                raise LocationNotFoundError(location_id)

    async def _weekly_balance(
        self,
        household_id: uuid.UUID,
        command: SuggestRecipesCommand,
        screened: ScreenedStock,
    ) -> WeeklyBalance | None:
        if command.balance_mode is BalanceMode.OFF:
            return None
        # The markers come from the *screened* stock: a fish shortfall only the
        # withheld half could have filled is not satisfiable from stock, and
        # saying otherwise turns an explanation into a reproach.
        return await self._balance.weekly(household_id, markers_in_stock=screened.markers)

    def _check(
        self,
        candidates: Sequence[GeneratedRecipe],
        index: StockIndex,
        *,
        strict: bool,
    ) -> list[tuple[GeneratedRecipe, tuple[Resolution, ...]]]:
        """Match every ingredient back, and drop the suggestions that do not.

        ``strict`` is on whenever a hard constraint is in force. When none is --
        a household of omnivorous adults with no declared allergy -- there is
        nothing to violate, and refusing a recipe because the model wrote
        "quelques herbes" would cost every suggestion to protect nobody. The
        resolutions are computed either way, because they also feed ``in_stock``
        and the expiry pressure.

        Nothing is repaired and no line is removed: a suggestion is kept whole or
        dropped whole (ADR-0009).
        """
        accepted: list[tuple[GeneratedRecipe, tuple[Resolution, ...]]] = []
        for recipe in candidates:
            resolutions = tuple(index.resolve(item.name) for item in recipe.ingredients)
            unresolved = [item for item in resolutions if not item.resolved]
            if strict and unresolved:
                logger.info(
                    "recipe_suggestion_discarded",
                    extra={"reason": "unresolved_ingredient", "count": len(unresolved)},
                )
                continue
            accepted.append((recipe, resolutions))
        return accepted

    def _persist(
        self,
        household_id: uuid.UUID,
        provider: ActiveProvider,
        recipe: GeneratedRecipe,
        *,
        resolutions: Sequence[Resolution],
        constraints: HouseholdConstraints,
        urgent: Sequence[StockLine],
        today: date,
        snapshot: dict[str, Any],
        latency_ms: int,
        usage: TokenUsage | None,
    ) -> SuggestedRecipe:
        suggestion_id = uuid.uuid7()
        ingredients = tuple(
            SuggestedIngredient(
                name=ingredient.name,
                amount=ingredient.amount,
                unit=ingredient.unit,
                # As read off the household's own stock, not as claimed by the
                # model, and by the same matcher that decides whether the
                # suggestion is safe at all.
                in_stock=resolution.line is not None,
            )
            for ingredient, resolution in zip(recipe.ingredients, resolutions, strict=False)
        )
        used = _urgent_used(resolutions, urgent)
        pressure = ExpiryPressure(
            items_used_expiring_within_days=len(used),
            urgent_items=tuple(
                UrgentItem(
                    inventory_item_id=line.inventory_item_id,
                    product_name=line.product.name,
                    expires_on=line.expires_on,
                    days_left=line.days_left(today) or 0,
                )
                for line in used
            ),
            urgent_items_left_unused=len(urgent) - len(used),
        )
        assessment = _assess(resolutions, constraints)
        preparation = Preparation(
            serving_temperature=recipe.serving_temperature,
            requires_cooking=recipe.requires_cooking,
            requires_oven=recipe.requires_oven,
        )
        summary = recipe.summary or ""
        # Measured, not believed: the model's own `uses_expiring_soon` is
        # discarded exactly like its `in_stock` claim.
        uses_expiring_soon = bool(used)
        payload: dict[str, Any] = {
            "title": recipe.title,
            "summary": summary,
            # The contract exposes one total duration; the table splits prep from
            # cook. The split is not invented here, so it stays in the payload.
            "duration_minutes": recipe.duration_minutes,
            "servings": recipe.servings,
            "uses_expiring_soon": uses_expiring_soon,
            "steps": list(recipe.steps),
            "ingredients": [
                {
                    "name": ingredient.name,
                    "amount": ingredient.amount,
                    "unit": ingredient.unit,
                    # As shown to the user, not as claimed by the model.
                    "in_stock": ingredient.in_stock,
                }
                for ingredient in ingredients
            ],
            "preparation": {
                "serving_temperature": preparation.serving_temperature,
                "requires_cooking": preparation.requires_cooking,
                "requires_oven": preparation.requires_oven,
            },
            # Counts only. Which allergens were excluded is health data that is
            # erased with the member row; a copy here would outlive the erasure.
            "unverified_product_count": assessment.unverified_product_count,
            "urgent_items_left_unused": pressure.urgent_items_left_unused,
        }
        self._session.add(
            RecipeSuggestion(
                id=suggestion_id,
                household_id=household_id,
                title=recipe.title,
                summary=recipe.summary,
                servings=recipe.servings,
                payload=payload,
                stock_snapshot=snapshot,
                provider_mode=provider.config.mode,
                provider_code=provider.config.provider_code,
                model=provider.config.model,
                prompt_version=PROMPT_VERSION,
                llm_provider_config_id=provider.config_id,
                input_tokens=_column(None if usage is None else usage.input_tokens),
                output_tokens=_column(None if usage is None else usage.output_tokens),
                cached_input_tokens=_column(None if usage is None else usage.cached_input_tokens),
                # Still zero, and deliberately. Tokens are *measured*; a price is
                # not. Turning tokens into money needs a per-model rate card that
                # changes without warning (one of the five providers has an
                # introductory rate expiring this month), that does not exist for
                # Ollama, and that is not the household's rate anyway under `byok` --
                # their tier, their currency, their contract. `cost_micro` also has
                # no currency column beside it, so a number here would not even say
                # what it counted. A stale euro figure shown next to a suggestion is
                # a confident lie; the token counts above are a fact the household
                # can price themselves. Filling this in means a dated, versioned
                # tariff table, which is a decision to record in an ADR, not a
                # constant to inline here.
                cost_micro=0,
                latency_ms=latency_ms,
                status=RecipeStatus.GENERATED,
            )
        )
        return SuggestedRecipe(
            id=suggestion_id,
            title=recipe.title,
            summary=summary,
            duration_minutes=recipe.duration_minutes,
            servings=recipe.servings,
            ingredients=ingredients,
            steps=tuple(recipe.steps),
            uses_expiring_soon=uses_expiring_soon,
            allergen_assessment=assessment,
            expiry_pressure=pressure,
            preparation=preparation,
        )


def _column(value: int | None) -> int:
    """Fit an optional count into a ``NOT NULL`` column.

    ``recipe_suggestion.input_tokens`` and its two siblings are non-nullable with a
    ``0`` default, so the table cannot express "the provider did not say" -- an
    unknown and a genuine zero look identical once written. That is a schema limit,
    not a decision taken here, and lifting it means a migration.

    The distinction survives everywhere it can still be acted on: the service
    returns :class:`TokenUsage` with its ``None``\\ s intact, and the API answers
    with a null ``usage`` rather than a block of zeros. Only the row flattens.
    """
    return 0 if value is None else value


def _snapshot(
    request: RecipeRequest,
    command: SuggestRecipesCommand,
    screened: ScreenedStock,
    balance: WeeklyBalance | None,
) -> dict[str, Any]:
    """Exactly what was sent, so a bad suggestion can be explained afterwards.

    The most sensitive row in the database: a complete inventory of a home. It falls
    under the same retention policy as receipt images (``domain/models.py``).

    What is **not** written here is who was at the table. Member ids, allergens,
    diets and age bands are all absent: deleting a member deletes their
    constraints (ADR-0009), and a snapshot holding a copy would keep an infant's
    age band alive for as long as the suggestion history. The counts and the
    reference version are enough to answer "why did it suggest that?".
    """
    return {
        "language": request.language,
        "notes": request.notes,
        "max_suggestions": request.max_suggestions,
        "location_ids": [str(location_id) for location_id in command.location_ids],
        "balance_mode": command.balance_mode.value,
        "balance_reference": None if balance is None else balance.reference,
        "meal_temperature": command.meal_temperature.value,
        "products_withheld": screened.withheld,
        "products_unverified": screened.unverified,
        "items": [
            {
                "name": item.name,
                "quantity": item.quantity,
                "unit": item.unit,
                "expires_in_days": item.expires_in_days,
            }
            for item in request.inventory
        ],
    }


def _to_provider_item(line: StockLine, today: date) -> ProviderInventoryItem:
    return ProviderInventoryItem(
        name=line.product.name,
        quantity=line.quantity,
        unit=line.unit,
        expires_in_days=line.days_left(today),
    )


def _urgent_used(
    resolutions: Sequence[Resolution], urgent: Sequence[StockLine]
) -> tuple[StockLine, ...]:
    """The urgent lots this suggestion actually draws on, deduplicated."""
    ids = {line.inventory_item_id for line in urgent}
    used: dict[uuid.UUID, StockLine] = {}
    for resolution in resolutions:
        line = resolution.line
        if line is not None and line.inventory_item_id in ids:
            used[line.inventory_item_id] = line
    return tuple(used.values())


def _markers_used(resolutions: Sequence[Resolution]) -> frozenset[PnnsMarker]:
    markers: set[PnnsMarker] = set()
    for resolution in resolutions:
        if resolution.line is not None:
            markers |= resolution.line.product.pnns_markers
    return frozenset(markers)


def _assess(
    resolutions: Sequence[Resolution], constraints: HouseholdConstraints
) -> AllergenAssessment:
    """The one sentence the interface may show, computed from what was used.

    Two facts decide it, and conflating them is the failure this whole feature
    exists to prevent. A product whose record *declared* its allergens supports
    the statement "none was declared here". A product nobody has documented
    supports nothing at all, and is counted separately rather than folded into
    the first group.

    The wording stays negative and situated whichever way it comes out: it says
    what the records declare, never that a recipe is free of anything. Nothing in
    a wiki is a promise.
    """
    declared: set[Allergen] = set()
    unverified: set[uuid.UUID] = set()
    for resolution in resolutions:
        line = resolution.line
        if line is None:
            continue
        if line.product.allergen_state is AllergenDataState.UNKNOWN:
            unverified.add(line.product.product_id)
        else:
            declared |= line.product.allergens_risk
    if declared:
        names = ", ".join(ALLERGEN_LABELS[allergen] for allergen in sorted(declared))
        statement = f"Allergènes déclarés parmi les produits identifiés : {names}."
    else:
        statement = "Aucun allergène déclaré parmi les produits identifiés."
    count = len(unverified)
    if count == 0:
        statement += " Tous les produits identifiés portent des données allergènes."
    elif count == 1:
        statement += " 1 produit n'a pas de données allergènes."
    else:
        statement += f" {count} produits n'ont pas de données allergènes."
    return AllergenAssessment(
        declared_clear_of=tuple(sorted(constraints.excluded_allergens - declared)),
        unverified_product_count=count,
        statement=statement,
    )


def _applied(constraints: HouseholdConstraints, screened: ScreenedStock) -> AppliedConstraints:
    return AppliedConstraints(
        members=tuple((person.id, person.display_name) for person in constraints.members),
        excluded_allergens=tuple(sorted(constraints.excluded_allergens)),
        diet=constraints.diet if constraints.members else None,
        infant_texture=constraints.infant_texture,
        age_bands=tuple(sorted(constraints.age_bands)),
        products_withheld=screened.withheld,
        products_unverified=screened.unverified,
    )


def _append_note(balance: WeeklyBalance | None, sentence: str) -> WeeklyBalance | None:
    if balance is None:
        return None
    note = sentence if balance.note is None else f"{balance.note} {sentence}"
    return WeeklyBalance(
        reference=balance.reference,
        window_days=balance.window_days,
        uncategorised_product_count=balance.uncategorised_product_count,
        gaps=balance.gaps,
        excesses=balance.excesses,
        satisfiable_from_stock=balance.satisfiable_from_stock,
        note=note,
    )


__all__ = [
    "MAX_INVENTORY_ITEMS",
    "NOTHING_URGENT_USED",
    "URGENT_WITHIN_DAYS",
    "AllergenAssessment",
    "AppliedConstraints",
    "BalanceMode",
    "ExpiryPressure",
    "Preparation",
    "RecipeService",
    "SuggestRecipesCommand",
    "SuggestedIngredient",
    "SuggestedRecipe",
    "SuggestionSet",
    "UrgentItem",
]
