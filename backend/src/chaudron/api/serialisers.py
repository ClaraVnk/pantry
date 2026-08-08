"""Shared domain-to-wire mappings for the objects two routers both return.

Only the weekly balance lives here today, and it is here for one reason: it is
returned by ``/v1/balance`` and embedded in ``/v1/recipes/suggest``, and two
hand-written mappings of the same object drift -- one of them would eventually
stop emitting ``uncategorised_product_count``, which is the field whose absence
turns "we do not know" into "everything is categorised" (contract 8).

Mapped field by field rather than by ``model_validate``: the domain type and the
schema agree on these names today, and a rename on either side should break the
build here instead of silently returning ``null``.
"""

from __future__ import annotations

from chaudron.api.schemas import BalanceExcessOut, BalanceGapOut, WeeklyBalanceOut
from chaudron.domain.balance import WeeklyBalance

__all__ = ["to_balance_out"]


def to_balance_out(balance: WeeklyBalance) -> WeeklyBalanceOut:
    return WeeklyBalanceOut(
        reference=balance.reference,
        window_days=balance.window_days,
        uncategorised_product_count=balance.uncategorised_product_count,
        gaps=[
            BalanceGapOut(
                marker=gap.marker,
                label=gap.label,
                target=gap.target,
                observed=gap.observed,
                shortfall=gap.shortfall,
                statement=gap.statement,
                source_url=gap.source_url,
            )
            for gap in balance.gaps
        ],
        excesses=[
            BalanceExcessOut(
                marker=excess.marker,
                label=excess.label,
                target=excess.target,
                observed_grams=excess.observed_grams,
                observed=excess.observed,
                unit=excess.unit,
                statement=excess.statement,
                source_url=excess.source_url,
            )
            for excess in balance.excesses
        ],
        satisfiable_from_stock=balance.satisfiable_from_stock,
        note=balance.note,
    )
