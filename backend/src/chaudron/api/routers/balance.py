"""``GET /v1/balance`` -- the same object a suggestion carries, on its own.

Contract 8. It exists so a dashboard can show the week without spending a model
call, and it answers the *unscreened* stock: nobody has said who is eating, so
narrowing the pantry to one person's constraints would be inventing a question
the client did not ask.
"""

from __future__ import annotations

from fastapi import APIRouter

from chaudron.api.deps import BalanceServiceDep, HouseholdDep
from chaudron.api.schemas import WeeklyBalanceOut
from chaudron.api.serialisers import to_balance_out

router = APIRouter(prefix="/v1/balance", tags=["balance"])


@router.get(
    "",
    response_model=WeeklyBalanceOut,
    summary="Weekly balance against the PNNS benchmarks",
)
async def read_balance(household_id: HouseholdDep, service: BalanceServiceDep) -> WeeklyBalanceOut:
    return to_balance_out(await service.weekly(household_id))
