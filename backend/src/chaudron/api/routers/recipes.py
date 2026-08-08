"""Recipe suggestions, and the translation of provider failures into HTTP.

One rule governs every path out of this module: **nothing a provider said reaches
the client**. A vendor SDK puts the key it was called with in its own error message
(security review, SEC-003), and the domain errors that cross this boundary may quote
a snippet of a model's answer. Responses therefore carry sentences written here, and
the log line goes through :func:`chaudron.infra.redaction.redact`.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends

from chaudron.api.deps import (
    HouseholdDep,
    MemberDep,
    RecipeFeedbackServiceDep,
    RecipeServiceDep,
    enforce_recipe_limits,
    require_member,
)
from chaudron.api.errors import ProblemError
from chaudron.api.schemas import (
    AllergenAssessmentOut,
    AppliedConstraintsOut,
    ExpiryPressureOut,
    MemberRefOut,
    ModelQualityOut,
    PreparationOut,
    RecipeFeedbackIn,
    RecipeFeedbackOut,
    RecipeIngredientOut,
    RecipeSuggestionOut,
    SuggestionQualityOut,
    SuggestRecipesIn,
    SuggestRecipesOut,
    TokenUsageOut,
    UrgentItemOut,
)
from chaudron.api.serialisers import to_balance_out
from chaudron.domain.constraints import MealTemperature
from chaudron.domain.llm_ports import (
    LlmError,
    ProviderCapabilityUnavailable,
    ProviderNotConfigured,
    ProviderQuotaExceeded,
    ProviderResponseInvalid,
    ProviderUnavailable,
)
from chaudron.domain.models import RecipeFeedback, RecipeStatus
from chaudron.infra.redaction import redact
from chaudron.services.recipe_feedback import QualityReport, SuggestionVerdict
from chaudron.services.recipes import BalanceMode, SuggestionSet, SuggestRecipesCommand

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/recipes", tags=["recipes"])


@router.post(
    "/suggest",
    response_model=SuggestRecipesOut,
    summary="Suggest recipes from the current stock",
    dependencies=[
        # Declared here as well as on the signature below, and the order is the
        # reason: FastAPI solves a decorator's dependencies in the order given
        # and before any the signature asks for, so listing the role guard first
        # is what refuses a viewer *before* the household's suggestion budget is
        # charged for a call that will not happen. It is solved once -- the
        # solver caches per ``(callable, scopes)``, and both spellings are
        # ``Depends(require_member)`` with no scopes.
        Depends(require_member),
        # The only endpoint whose cost is money rather than milliseconds. The guard
        # runs before the household's provider is even resolved, and holds a
        # concurrency slot for the whole call (``api/throttling.py``).
        Depends(enforce_recipe_limits),
    ],
)
async def suggest_recipes(
    household_id: MemberDep, service: RecipeServiceDep, payload: SuggestRecipesIn
) -> SuggestRecipesOut:
    """Suggest recipes, and record that we did.

    ``MemberDep`` rather than ``HouseholdDep``, for three reasons that compound.
    It **writes**: every call persists a ``recipe_suggestion`` row carrying a
    stock snapshot, the provider mode, token counts and a latency
    (``services/recipes.py``), so the census rule "a viewer reads" applies to it
    exactly as written. It **spends**: the household's own key under ``byok``,
    the operator's under ``instance_owner``. And it **transmits health data to a
    third party**: the prompt interpolates the infant-texture signal for the
    member ids the caller names (``infra/llm/prompts.py``), which is the same
    shape as the export route next door, guarded for the same reason.

    Latent rather than live, and worth stating plainly so nobody re-opens this on
    the strength of a severity: no route mints a ``viewer`` today. Registration
    creates an ``owner`` (``services/auth.py``) and ``POST /v1/members`` creates
    an eater, not a membership. It becomes live the day an invite route lands,
    which is precisely when nobody will re-read this endpoint.
    """
    try:
        result = await service.suggest(
            household_id,
            SuggestRecipesCommand(
                location_ids=tuple(payload.location_ids),
                max_suggestions=payload.max_suggestions,
                notes=payload.notes.strip() or None,
                member_ids=tuple(payload.member_ids),
                balance_mode=BalanceMode(payload.balance_mode),
                meal_temperature=MealTemperature(payload.meal_temperature),
            ),
        )
    except LlmError as error:
        _log(error)
        # `from None`: the chain would carry the provider's own message into the
        # traceback of any handler that logs it.
        raise _problem_for(error) from None
    return _to_out(result)


# --------------------------------------------------------------------------- #
# Feedback
# --------------------------------------------------------------------------- #
#
# No throttle on any of the three below, and the asymmetry with ``/suggest`` is
# the reason: that endpoint is rate-limited because every call is a billed
# inference, while these are one indexed UPDATE and one grouped SELECT on rows
# the household already owns. A limiter here would buy nothing and would make a
# double tap on a flaky connection look like a failure -- which is precisely how
# a feedback loop stops collecting data.


@router.put(
    "/suggestions/{suggestion_id}/feedback",
    response_model=RecipeFeedbackOut,
    summary="Record or change the household's verdict on one suggestion",
)
async def set_feedback(
    household_id: MemberDep,
    service: RecipeFeedbackServiceDep,
    suggestion_id: uuid.UUID,
    payload: RecipeFeedbackIn,
) -> RecipeFeedbackOut:
    """``PUT`` rather than ``POST``: the latest verdict wins, and saying so twice
    must land in the same place as saying it once. There is exactly one opinion
    per suggestion, so the request names the state it wants rather than appending
    to a log -- which is also why re-tapping the same button is harmless."""
    verdict = await service.record(household_id, suggestion_id, RecipeFeedback(payload.feedback))
    return _feedback_out(verdict)


@router.delete(
    "/suggestions/{suggestion_id}/feedback",
    response_model=RecipeFeedbackOut,
    summary="Withdraw the household's verdict on one suggestion",
)
async def clear_feedback(
    household_id: MemberDep,
    service: RecipeFeedbackServiceDep,
    suggestion_id: uuid.UUID,
) -> RecipeFeedbackOut:
    """Answers with the resulting state rather than ``204``.

    The client renders one thing -- "what is the verdict on this card now" -- and
    a body-less success would force it to reconstruct that from the request it
    happened to send, which is the version that goes wrong when two taps race.
    """
    return _feedback_out(await service.clear(household_id, suggestion_id))


@router.get(
    "/quality",
    response_model=SuggestionQualityOut,
    summary="Feedback aggregated by provider and model",
)
async def read_quality(
    household_id: HouseholdDep, service: RecipeFeedbackServiceDep
) -> SuggestionQualityOut:
    """Household-scoped, like everything else on this API.

    An operator running a multi-household instance reads the cross-tenant version
    with ``scripts/quality_report.py``: the request session connects as a role
    subject to the row-level policies of migration ``0004``, so it *cannot* see
    another household's rows, and there is no operator identity in this slice
    that would justify a route which could.
    """
    return _quality_out(await service.quality(household_id))


def _feedback_out(verdict: SuggestionVerdict) -> RecipeFeedbackOut:
    return RecipeFeedbackOut(
        suggestion_id=verdict.suggestion_id,
        feedback=_feedback_value(verdict.feedback),
        feedback_at=verdict.feedback_at,
        status=_status_value(verdict.status),
    )


def _feedback_value(value: RecipeFeedback | None) -> Literal["cooked", "not_interested"] | None:
    """Restate the enum in the response model's own vocabulary.

    Mapped rather than cast so that a member added to :class:`RecipeFeedback`
    breaks this build instead of escaping into a response shape the contract does
    not describe.
    """
    match value:
        case RecipeFeedback.COOKED:
            return "cooked"
        case RecipeFeedback.NOT_INTERESTED:
            return "not_interested"
        case None:
            return None


def _status_value(value: RecipeStatus) -> Literal["generated", "saved", "cooked", "discarded"]:
    match value:
        case RecipeStatus.GENERATED:
            return "generated"
        case RecipeStatus.SAVED:
            return "saved"
        case RecipeStatus.COOKED:
            return "cooked"
        case RecipeStatus.DISCARDED:
            return "discarded"


def _quality_out(report: QualityReport) -> SuggestionQualityOut:
    return SuggestionQualityOut(
        min_responses=report.min_responses,
        models=[
            ModelQualityOut(
                provider_mode=entry.provider_mode.value,
                model=entry.model,
                cooked=entry.cooked,
                not_interested=entry.not_interested,
                responses=entry.responses,
                # Computed by the service, which owns the threshold. The router
                # never divides: a client-side or router-side division is how a
                # "100 %" built on one answer reaches a screen.
                cooked_rate=entry.cooked_rate(min_responses=report.min_responses),
            )
            for entry in report.models
        ],
    )


def _to_out(result: SuggestionSet) -> SuggestRecipesOut:
    applied = result.applied_constraints
    return SuggestRecipesOut(
        provider_mode=result.provider_mode,
        model=result.model,
        suggestions=[
            RecipeSuggestionOut(
                id=recipe.id,
                title=recipe.title,
                summary=recipe.summary,
                duration_minutes=recipe.duration_minutes,
                servings=recipe.servings,
                ingredients=[
                    RecipeIngredientOut(
                        name=ingredient.name,
                        amount=ingredient.amount,
                        unit=ingredient.unit,
                        in_stock=ingredient.in_stock,
                    )
                    for ingredient in recipe.ingredients
                ],
                steps=list(recipe.steps),
                uses_expiring_soon=recipe.uses_expiring_soon,
                allergen_assessment=AllergenAssessmentOut(
                    declared_clear_of=list(recipe.allergen_assessment.declared_clear_of),
                    unverified_product_count=(recipe.allergen_assessment.unverified_product_count),
                    # Verbatim, and composed by the service. The client is
                    # forbidden from writing its own (ADR-0009).
                    statement=recipe.allergen_assessment.statement,
                ),
                expiry_pressure=ExpiryPressureOut(
                    items_used_expiring_within_days=(
                        recipe.expiry_pressure.items_used_expiring_within_days
                    ),
                    urgent_items=[
                        UrgentItemOut(
                            inventory_item_id=item.inventory_item_id,
                            product_name=item.product_name,
                            expires_on=item.expires_on,
                            days_left=item.days_left,
                        )
                        for item in recipe.expiry_pressure.urgent_items
                    ],
                    urgent_items_left_unused=recipe.expiry_pressure.urgent_items_left_unused,
                ),
                preparation=PreparationOut(
                    serving_temperature=_temperature(recipe.preparation.serving_temperature),
                    requires_cooking=recipe.preparation.requires_cooking,
                    requires_oven=recipe.preparation.requires_oven,
                ),
            )
            for recipe in result.recipes
        ],
        applied_constraints=AppliedConstraintsOut(
            members=[
                MemberRefOut(id=member_id, display_name=display_name)
                for member_id, display_name in applied.members
            ],
            excluded_allergens=list(applied.excluded_allergens),
            diet=applied.diet,
            infant_texture=applied.infant_texture,
            age_bands=list(applied.age_bands),
            products_withheld=applied.products_withheld,
            products_unverified=applied.products_unverified,
        ),
        balance=None if result.balance is None else to_balance_out(result.balance),
        # Mapped field by field rather than by `model_validate`: the domain type
        # and the schema agree on these three names today, and a rename on either
        # side should break the build here instead of silently returning `null`.
        usage=(
            None
            if result.usage is None
            else TokenUsageOut(
                input_tokens=result.usage.input_tokens,
                output_tokens=result.usage.output_tokens,
                cached_input_tokens=result.usage.cached_input_tokens,
            )
        ),
    )


def _temperature(value: str | None) -> Literal["hot", "cold", "either"] | None:
    """Narrow the reader's already-closed vocabulary for the response model.

    ``payloads._read_preparation`` guarantees the value is one of the three or
    ``None``; this restates it where the schema can check it, so a widening of
    the reader breaks the build rather than the client.
    """
    match value:
        case "hot" | "cold" | "either":
            return value
        case _:
            return None


def _log(error: LlmError) -> None:
    """Record the failure with the three facts a support ticket needs, scrubbed.

    ``provider`` and ``model`` come from our own context object, never from the
    provider's message; that message is redacted before it is written at all.
    """
    context = error.context
    logger.warning(
        "recipe_provider_failure",
        extra={
            "error": type(error).__name__,
            "provider": None if context is None else context.provider,
            "model": None if context is None else context.model,
            "failure_mode": None if context is None else context.failure_mode,
            "detail": redact(str(error)),
        },
    )


def _problem_for(error: LlmError) -> ProblemError:
    """Map a provider failure onto the status code the client can act on.

    Every ``detail`` below is written here. None of them interpolates ``error``:
    that is what keeps a household's API key out of an HTTP response.
    """
    match error:
        case ProviderNotConfigured():
            # Also catches ProviderNotPermitted: a household that may not use the
            # instance's own key needs the configuration screen, same as one that
            # configured nothing at all.
            return ProblemError(
                slug="provider-not-configured",
                title="Model provider not configured",
                status=409,
                detail=(
                    "This household has no usable model provider. Configure one before "
                    "asking for recipe suggestions."
                ),
            )
        case ProviderCapabilityUnavailable():
            return ProblemError(
                slug="provider-capability-unavailable",
                title="Model capability unavailable",
                status=409,
                detail=(
                    "The configured model cannot do what this request needs. Choose a "
                    "model that supports it."
                ),
                # Our own token from the capability taxonomy, not provider text.
                capability=error.capability,
            )
        case ProviderQuotaExceeded():
            return ProblemError(
                slug="provider-quota-exceeded",
                title="Model provider quota exceeded",
                status=429,
                detail=(
                    "The model provider refused the request for rate-limit or credit "
                    "reasons. Retry later, or check the account behind the key."
                ),
                headers=_retry_after(error),
            )
        case ProviderUnavailable():
            return ProblemError(
                slug="provider-unavailable",
                title="Model provider unavailable",
                status=503,
                detail="The model provider did not answer. Retry shortly.",
            )
        case ProviderResponseInvalid():
            return ProblemError(
                slug="provider-response-invalid",
                title="Unreadable model response",
                status=502,
                detail=(
                    "The model provider answered with something this application could "
                    "not read. Retry, or choose another model."
                ),
            )
        case _:
            return ProblemError(
                slug="provider-error",
                title="Model provider failure",
                status=502,
                detail="The model provider could not serve this request.",
            )


def _retry_after(error: ProviderQuotaExceeded) -> dict[str, str] | None:
    """Forward a retry delay when the domain error carries one.

    No adapter attaches it today. Read defensively rather than assumed absent, so
    the header appears the moment one does -- and so this code does not invent a
    delay nobody measured.
    """
    delay: Any = getattr(error, "retry_after", None)
    if isinstance(delay, int) and delay > 0:
        return {"Retry-After": str(delay)}
    return None
