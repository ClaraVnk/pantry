"""Recipe-suggestion feedback aggregated across every household on this instance.

Run from ``backend/``::

    uv run python scripts/quality_report.py

The question it answers is the one ADR-0007 asserts and never measured: *does the
cheap local model produce recipes people actually cook?* Token counts and
latencies say what a call consumed, never whether it was worth making, and
``ix_recipe_suggestion_feedback`` -- ``(provider_mode, model, feedback)``, partial
on ``feedback IS NOT NULL`` -- exists for exactly this grouping.

**Why a script and not a route.** The aggregation is cross-tenant, and nothing in
the HTTP API is authenticated as an operator: a household names itself with a
header (``api/deps.py``), which is the provisional resolution the contract flags
as such. A route serving other households' counts to whoever guesses a UUID would
be the largest hole in the application. The API therefore exposes the same figures
scoped to one household (``GET /v1/recipes/quality``), and the instance-wide view
lives here, behind the same shell access as ``alembic upgrade`` -- an operator who
can run this can already read the database.

That also means this script depends on connecting as the **maintenance identity**:
the role owning the tables, which bypasses the policies of migration ``0004``
because that revision deliberately leaves ``FORCE ROW LEVEL SECURITY`` off. Run
with the application's least-privileged DSN instead and the output is empty rather
than wrong -- no tenant is posted, so every policy matches nothing. The script says
so instead of printing a silent zero.

Counts first, rates second, and only above a threshold: see
``services/recipe_feedback.MIN_RESPONSES_FOR_RATE``. A "100 % cooked" built on one
answer is the failure mode this report exists to avoid, not a result.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from chaudron.config import ConfigurationError, get_settings
from chaudron.domain.models import RecipeSuggestion
from chaudron.services.recipe_feedback import (
    MIN_RESPONSES_FOR_RATE,
    ModelQuality,
    quality_query,
)

logger = logging.getLogger("chaudron.quality_report")


async def collect(session: AsyncSession) -> tuple[ModelQuality, ...]:
    """The same query the API runs, minus the household predicate RLS would add."""
    rows = await session.execute(quality_query())
    return tuple(
        ModelQuality(
            provider_mode=provider_mode,
            model=model,
            cooked=cooked,
            not_interested=not_interested,
        )
        for provider_mode, model, cooked, not_interested in rows.all()
    )


async def count_answered(session: AsyncSession) -> int:
    """Rows carrying any verdict at all, to tell "nothing yet" from "nothing visible"."""
    total = await session.scalar(
        select(func.count())
        .select_from(RecipeSuggestion)
        .where(RecipeSuggestion.feedback.is_not(None))
    )
    return total or 0


def render(models: tuple[ModelQuality, ...]) -> str:
    """A fixed-width table. Every rate carries the effectif that produced it."""
    if not models:
        return "No suggestion has been answered about yet."

    header = f"{'PROVIDER':<16}{'MODEL':<34}{'COOKED':>8}{'DISMISSED':>11}{'RATE':>22}"
    lines = [header, "-" * len(header)]
    for entry in models:
        rate = entry.cooked_rate(min_responses=MIN_RESPONSES_FOR_RATE)
        # The effectif travels with the percentage, always. A bare "67 %" invites
        # a comparison between two models that three taps could invert.
        verdict = (
            f"{rate:.0%} ({entry.cooked}/{entry.responses})"
            if rate is not None
            else f"- ({entry.responses}/{MIN_RESPONSES_FOR_RATE} answers)"
        )
        lines.append(
            f"{entry.provider_mode.value:<16}{entry.model[:33]:<34}"
            f"{entry.cooked:>8}{entry.not_interested:>11}{verdict:>22}"
        )
    return "\n".join(lines)


async def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        settings = get_settings()
    except ConfigurationError as exc:
        logger.error("%s", exc)
        return 2

    engine = create_async_engine(settings.database_url.get_secret_value())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        # No tenant is posted on purpose: this transaction is meant to be
        # cross-tenant, which only works as the owning role.
        async with factory() as session, session.begin():
            models = await collect(session)
            answered = await count_answered(session)
    finally:
        await engine.dispose()

    logger.info(
        "cross-tenant feedback report (rate shown from %d answers per model)",
        MIN_RESPONSES_FOR_RATE,
    )
    logger.info("\n%s", render(models))
    if answered == 0:
        logger.warning(
            "no answered suggestion is visible. Either nobody has given feedback yet, "
            "or this DSN names the least-privileged application role, whose row-level "
            "policies match nothing outside a request. Use the maintenance DSN."
        )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
