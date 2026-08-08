"""What the operator is told at startup about the bound on credential resolution.

Resolving a feed credential scans the household table, and past
:data:`MAX_RESOLVABLE_HOUSEHOLDS` the server answers ``503`` -- to *everyone*,
including a subscriber whose credentials are perfectly good. The refusal is
deliberate and stays, but "you have outgrown this" is a capacity decision, and a
capacity decision discovered by the first phone to poll is a decision nobody
made. So it is reported when the process starts.

The query is exercised for real once, on real rows. Everything after that stubs
its *answer*: five thousand households is not a fixture, and what the thresholds
need is a number, not a table.
"""

from __future__ import annotations

import base64
import logging
from datetime import UTC, datetime
from typing import Protocol

import pytest
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from chaudron.config import Settings
from chaudron.infra.calendar.credentials import MAX_RESOLVABLE_HOUSEHOLDS
from chaudron.infra.calendar.repository import count_feed_households
from chaudron.infra.db import Database
from chaudron.services.calendar import report_feed_scan_headroom
from tests.conftest import MakeHousehold


class StubCount(Protocol):
    def __call__(self, total: int) -> None: ...


@pytest.fixture
def stub_count(monkeypatch: pytest.MonkeyPatch) -> StubCount:
    def _stub(total: int) -> None:
        async def _count(_session: AsyncSession) -> int:
            return total

        monkeypatch.setattr("chaudron.services.calendar.count_feed_households", _count)

    return _stub


async def test_the_count_sees_live_households_and_not_archived_ones(
    db_session: AsyncSession, make_household: MakeHousehold
) -> None:
    """The real query, on the real table -- the stub below replaces only its answer.

    Archived households are not scanned during resolution, so counting them would
    report headroom the scan does not actually spend.
    """
    before = await count_feed_households(db_session)
    await make_household()
    archived = await make_household()
    archived.archived_at = datetime.now(UTC)
    await db_session.flush()

    assert await count_feed_households(db_session) == before + 1


@pytest.mark.parametrize(
    ("households", "expected"),
    [
        (1, None),
        (int(MAX_RESOLVABLE_HOUSEHOLDS * 0.5), None),
        (int(MAX_RESOLVABLE_HOUSEHOLDS * 0.8), "calendar_feed_scan_limit_approaching"),
        (MAX_RESOLVABLE_HOUSEHOLDS, "calendar_feed_scan_limit_approaching"),
        (MAX_RESOLVABLE_HOUSEHOLDS + 1, "calendar_feed_scan_limit_exceeded"),
    ],
    ids=["tiny", "half", "four-fifths", "at-the-bound", "past-it"],
)
async def test_the_report_says_only_what_the_count_warrants(
    calendar_app: FastAPI,
    caplog: pytest.LogCaptureFixture,
    stub_count: StubCount,
    households: int,
    expected: str | None,
) -> None:
    stub_count(households)
    database: Database = calendar_app.state.database
    with caplog.at_level(logging.INFO, logger="chaudron.services.calendar"):
        await report_feed_scan_headroom(database)
    await database.dispose()

    messages = [record.message for record in caplog.records]
    if expected is None:
        assert messages == [], "a healthy instance must not be told anything"
    else:
        assert messages == [expected]


async def test_a_database_that_is_not_up_is_unverified_rather_than_broken(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Unable to tell, and too big, are different facts; only one is an error.

    Same reasoning as ``verify_row_level_security``: a database thirty seconds
    late must not turn a report into a boot failure, and a calendar feature must
    never be what keeps the API from starting.
    """
    settings = Settings(
        env="ci",
        database_url=SecretStr("postgresql+asyncpg://nobody:nothing@127.0.0.1:1/absent"),
        secret_key=SecretStr("x" * 40),
        credential_encryption_key=SecretStr(base64.b64encode(b"0" * 32).decode()),
    )
    database = Database(settings)
    with caplog.at_level(logging.INFO, logger="chaudron.services.calendar"):
        await report_feed_scan_headroom(database)
    await database.dispose()

    assert [record.message for record in caplog.records] == ["calendar_feed_headroom_unverified"]
    assert caplog.records[0].levelno == logging.WARNING
