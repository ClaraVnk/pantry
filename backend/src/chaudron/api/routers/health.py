"""Liveness and readiness, deliberately separate.

``/healthz`` answers from the process alone. If it touched the database, a brief
outage would make the orchestrator kill a perfectly healthy container and turn a
degradation into a restart loop.

``/readyz`` does touch it, because "ready" means "able to serve", and an API
without its database can serve nothing. It also asks whether row-level security
is genuinely in force on the connection this process holds -- see
:meth:`chaudron.infra.db.Database.check_row_level_security`. An instance whose
DSN names the table owner rather than the application role passes every
functional test and isolates nothing, and that misconfiguration has no symptom a
request could reveal. A probe is the only thing that can see it.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from chaudron.api.deps import get_database, get_settings_dep
from chaudron.api.schemas import HealthOut, ReadinessOut
from chaudron.config import Settings
from chaudron.infra.db import Database

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


@router.get("/healthz", response_model=HealthOut, summary="Liveness probe")
async def healthz() -> HealthOut:
    return HealthOut()


@router.get(
    "/readyz",
    response_model=ReadinessOut,
    summary="Readiness probe",
    responses={503: {"model": ReadinessOut}},
)
async def readyz(
    database: Annotated[Database, Depends(get_database)],
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> JSONResponse:
    """Check the database on a connection of its own.

    Not the request-scoped session: that dependency opens a transaction and
    would fail *before* this function runs, turning an honest 503 into a 500.

    The row-level security answer is reported in every environment and *refuses*
    readiness everywhere except ``local`` and ``ci``. Those two connect as the
    table owner on purpose -- a developer runs migrations and the API from one
    DSN, and ``tests/tenancy/conftest.py`` needs the owner to prove the
    provisioned role behaves differently -- so a 503 there would be a permanent
    red light nobody could act on, and a permanent red light is one people learn
    to ignore, including on the deployment where it means something.

    ``staging`` used to be on the permissive side of that line, because the test
    was ``is_production`` and ``is_production`` is ``env == "production"``
    exactly. An instance deployed there with the owner DSN isolated nothing
    between households and answered this probe ``200``, so a load balancer put it
    into service (audit AUD-029). ``Settings.requires_row_level_security`` is now
    what decides, and it names the two exemptions rather than the one refusal.
    """
    try:
        report = await database.check_row_level_security()
    except (SQLAlchemyError, OSError) as exc:
        # `OSError` too: a refused connection or an unresolvable host arrives from
        # asyncpg's socket rather than through the dialect, and a readiness probe
        # that answered 500 to "the database is down" would be reporting on itself.
        logger.warning("readiness_check_failed", extra={"error": type(exc).__name__})
        return JSONResponse(
            ReadinessOut(status="degraded", checks={"database": "unavailable"}).model_dump(),
            status_code=503,
        )

    checks = {
        "database": "ok",
        "row_level_security": "enforced" if report.is_enforced else "bypassed",
    }
    if report.is_enforced:
        return JSONResponse(
            ReadinessOut(status="ready", checks=checks).model_dump(), status_code=200
        )

    # The reasons name roles and tables, so they go to the log the operator reads
    # and not to the body an unauthenticated caller reads.
    logger.error("row_level_security_not_enforced", extra={"problems": list(report.problems)})
    if not settings.requires_row_level_security:
        return JSONResponse(
            ReadinessOut(status="ready", checks=checks).model_dump(), status_code=200
        )
    return JSONResponse(
        ReadinessOut(status="degraded", checks=checks).model_dump(), status_code=503
    )
