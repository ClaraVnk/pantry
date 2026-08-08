"""What the household thought of a suggestion, and what that makes measurable.

Three questions decided the shape of this module, and each answer is a
restriction rather than a feature.

*Where does an opinion live?* On ``recipe_suggestion`` itself -- three columns,
no child table (``domain/models.py`` argues it at length). An opinion changes
(dismissed on Tuesday, cooked on Sunday) and the ranking wants the *current*
verdict, so the latest answer overwrites the previous one. How many times a dish
was actually cooked is a different question, already answered in the right place:
every cooking deducts stock and leaves a dated ``stock_movement`` carrying
``recipe_suggestion_id``. A ledger of opinions here would recount those events
and eventually disagree with the movement journal.

*What may an opinion change?* The **order** of future suggestions, never their
membership. A dish the household waved away sinks; it is never removed. The
distinction is the whole safety argument of the feature: a signal that filters
compounds on itself -- every dismissal shrinks the space the next dismissal is
drawn from -- until a household is shown the same three meals forever, with
nothing on screen to explain the narrowing. A signal that only reorders cannot
run away, because the same candidates keep being generated.

*Where does an opinion never go?* Into a prompt. Feedback is contract 4bis's
"calculable par l'application" class: the application measures, the model writes.
Feeding "they disliked X" back to the model would make the ranking unverifiable
(a model is allowed to ignore an instruction), would put a household's habits in
a third party's request body, and would turn a deterministic sort into a request.

An opinion is also about **one suggestion** -- not a product, not a person. No
per-eater profile is derived here and none can be: nothing in this module reads
``household_member``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from chaudron.domain.labels.normalization import to_comparison_form
from chaudron.domain.models import (
    LlmProviderMode,
    RecipeFeedback,
    RecipeStatus,
    RecipeSuggestion,
)
from chaudron.domain.ports import RecipeSuggestionNotFoundError

#: How many answers a (provider, model) pair needs before a *rate* is published.
#:
#: Below this, only the counts are shown. "2 avis sur 3" is a fact; "67 %" is the
#: same fact dressed as a measurement, and one more tap moves it by seventeen
#: points. Ten is where the 95 % Wilson interval on a proportion first narrows to
#: roughly +/-30 points -- still wide, but no longer wide enough to invert the
#: comparison between two models on a single answer. The number is deliberately
#: on the response (``min_responses``) rather than compiled into the client, so
#: an operator reading "no rate yet" can see what it is waiting for.
MIN_RESPONSES_FOR_RATE: Final = 10

#: How many dismissed titles the ranking will carry. A household that has been
#: dismissing suggestions for two years should not turn every request into an
#: unbounded ``IN`` list, and the ranking only ever needs to recognise a title the
#: model has just produced again -- for which the most recent dismissals are by
#: far the likeliest match.
MAX_DISPREFERRED_TITLES: Final = 200


@dataclass(frozen=True, slots=True)
class SuggestionVerdict:
    """One suggestion's current answer. ``feedback`` is ``None`` when withdrawn."""

    suggestion_id: uuid.UUID
    feedback: RecipeFeedback | None
    feedback_at: datetime | None
    status: RecipeStatus


@dataclass(frozen=True, slots=True)
class ModelQuality:
    """What one (provider mode, model) pair was told, in counts.

    Counts and not a rate: the rate is derived by :meth:`cooked_rate`, which
    refuses to produce one below :data:`MIN_RESPONSES_FOR_RATE`. Keeping the raw
    numbers on the object means the caller can always show the effectif, which is
    the part that makes a small sample readable as a small sample.
    """

    provider_mode: LlmProviderMode
    model: str
    cooked: int
    not_interested: int

    @property
    def responses(self) -> int:
        return self.cooked + self.not_interested

    def cooked_rate(self, *, min_responses: int) -> float | None:
        """The share of answers that were "cooked", or ``None`` on a thin sample."""
        if self.responses < min_responses:
            return None
        return self.cooked / self.responses


@dataclass(frozen=True, slots=True)
class QualityReport:
    """The aggregate that turns ADR-0007's degradation claim into a number.

    ``min_responses`` travels with the rows so a reader knows why a pair shows
    counts and no rate, rather than suspecting a bug.
    """

    min_responses: int
    models: tuple[ModelQuality, ...]


def quality_query() -> Select[tuple[LlmProviderMode, str, int, int]]:
    """The one aggregation, shared by the API and by the operator's script.

    Deliberately carries **no** ``household_id`` predicate. Through a request
    session the row-level policy of migration ``0004`` adds it and the answer is
    the household's own; run by the maintenance identity -- which owns the tables
    and therefore bypasses the policy -- the same query answers across tenants,
    which is what the cross-tenant ``ix_recipe_suggestion_feedback`` exists to
    serve. One definition, so a household screen and an operator report cannot
    drift into counting different things.

    The ``WHERE feedback IS NOT NULL`` matches that index's partial predicate
    exactly, so the scan touches only the rows somebody answered about.
    """
    return (
        select(
            RecipeSuggestion.provider_mode,
            RecipeSuggestion.model,
            func.count().filter(RecipeSuggestion.feedback == RecipeFeedback.COOKED).label("cooked"),
            func.count()
            .filter(RecipeSuggestion.feedback == RecipeFeedback.NOT_INTERESTED)
            .label("not_interested"),
        )
        .where(RecipeSuggestion.feedback.is_not(None))
        .group_by(RecipeSuggestion.provider_mode, RecipeSuggestion.model)
        .order_by(RecipeSuggestion.provider_mode, RecipeSuggestion.model)
    )


class RecipeFeedbackService:
    """Records, changes and withdraws the household's verdict on a suggestion."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(
        self,
        household_id: uuid.UUID,
        suggestion_id: uuid.UUID,
        verdict: RecipeFeedback,
    ) -> SuggestionVerdict:
        """Write the latest verdict, and move ``status`` to match it.

        ``ck_recipe_suggestion_feedback_matches_status`` refuses a row whose
        lifecycle and measurement disagree, so the two are written together here
        rather than left for a caller to remember. A "cooked" verdict also needs
        ``cooked_at``, which the same constraint requires.

        ``cooked_at`` is set once and then left alone. It is the date of the first
        cooking, not a counter: how many times a dish was cooked is a ``count(*)``
        on ``stock_movement``, and a column that pretended to track it would be a
        second, poorer answer to a question already answered exactly.
        """
        suggestion = await self._load(household_id, suggestion_id)
        now = datetime.now(UTC)
        suggestion.feedback = verdict
        suggestion.feedback_at = now
        # `feedback_by_user_id` stays NULL: there are no user accounts in this
        # slice (``api/deps.py``), and inventing one would attribute an opinion to
        # a person the request never identified.
        if verdict is RecipeFeedback.COOKED:
            suggestion.status = RecipeStatus.COOKED
            if suggestion.cooked_at is None:
                suggestion.cooked_at = now
        else:
            suggestion.status = RecipeStatus.DISCARDED
            # `cooked_at` is *not* cleared. A dish cooked in March and dismissed in
            # August was still cooked in March, and the stock movements of that day
            # say so; erasing the date here would make this table contradict them.
        await self._session.flush()
        return _verdict(suggestion)

    async def clear(self, household_id: uuid.UUID, suggestion_id: uuid.UUID) -> SuggestionVerdict:
        """Withdraw the verdict, and put the lifecycle back where it was.

        Clearing an answer must not leave a suggestion permanently ``discarded``:
        the household said "ignore what I said", not "discard this". The status
        falls back to ``cooked`` when a cooking really happened -- ``cooked_at``
        is the evidence, and it is never invented -- and to ``generated``
        otherwise.

        Idempotent: withdrawing an opinion nobody expressed is not an error, so a
        double tap on a flaky connection does not surface one.
        """
        suggestion = await self._load(household_id, suggestion_id)
        suggestion.feedback = None
        suggestion.feedback_at = None
        suggestion.feedback_by_user_id = None
        suggestion.status = (
            RecipeStatus.COOKED if suggestion.cooked_at is not None else RecipeStatus.GENERATED
        )
        await self._session.flush()
        return _verdict(suggestion)

    async def current(self, household_id: uuid.UUID, suggestion_id: uuid.UUID) -> SuggestionVerdict:
        return _verdict(await self._load(household_id, suggestion_id))

    async def quality(self, household_id: uuid.UUID) -> QualityReport:
        """The per-model aggregate, for whoever this session is scoped to.

        Through the API that is one household. The predicate is written out even
        though the row-level policy already applies it: a mechanism that is the
        only thing standing between two families' data should not be the only
        thing, and an explicit ``WHERE`` costs one indexed comparison.
        """
        rows = await self._session.execute(
            quality_query().where(RecipeSuggestion.household_id == household_id)
        )
        return QualityReport(
            min_responses=MIN_RESPONSES_FOR_RATE,
            models=tuple(
                ModelQuality(
                    provider_mode=provider_mode,
                    model=model,
                    cooked=cooked,
                    not_interested=not_interested,
                )
                for provider_mode, model, cooked, not_interested in rows.all()
            ),
        )

    async def dispreferred_titles(self, household_id: uuid.UUID) -> frozenset[str]:
        """Comparison forms of the titles this household last said no to.

        Comparison forms rather than raw titles because the model rewrites its own
        wording between calls: "Gratin de courgettes" and "GRATIN DE COURGETTES"
        are the same refusal, and matching on the exact string would silently stop
        recognising it. :func:`to_comparison_form` is the fold the label lexicon
        already uses, reused rather than re-invented so the two cannot disagree on
        what "the same string" means.

        Titles whose *latest* verdict is a dismissal, so changing one's mind takes
        effect immediately: the row's ``feedback`` column holds one answer, and
        recording "cooked" over "not interested" removes it from this set.
        """
        rows = await self._session.scalars(
            select(RecipeSuggestion.title)
            .where(
                RecipeSuggestion.household_id == household_id,
                RecipeSuggestion.feedback == RecipeFeedback.NOT_INTERESTED,
            )
            .order_by(RecipeSuggestion.feedback_at.desc())
            .limit(MAX_DISPREFERRED_TITLES)
        )
        return frozenset(to_comparison_form(title) for title in rows.all())

    async def _load(self, household_id: uuid.UUID, suggestion_id: uuid.UUID) -> RecipeSuggestion:
        suggestion = await self._session.scalar(
            select(RecipeSuggestion).where(
                RecipeSuggestion.household_id == household_id,
                RecipeSuggestion.id == suggestion_id,
            )
        )
        if suggestion is None:
            raise RecipeSuggestionNotFoundError(suggestion_id)
        return suggestion


def _verdict(suggestion: RecipeSuggestion) -> SuggestionVerdict:
    return SuggestionVerdict(
        suggestion_id=suggestion.id,
        feedback=suggestion.feedback,
        feedback_at=suggestion.feedback_at,
        status=suggestion.status,
    )


__all__ = [
    "MAX_DISPREFERRED_TITLES",
    "MIN_RESPONSES_FOR_RATE",
    "ModelQuality",
    "QualityReport",
    "RecipeFeedbackService",
    "SuggestionVerdict",
    "quality_query",
]
