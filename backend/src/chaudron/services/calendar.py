"""What the calendar feed publishes, and the rules that bound it.

This layer owns three decisions, and none of them belong in the HTTP handler.

**What is published.** Active lots carrying a date, inside a window, capped in
number. Everything else in the inventory stays home. The date is the *effective*
one -- the printed date shortened by the after-opening rule of
``docs/data-model.md`` 7.4 -- because a reminder that fires on a date the lot has
already outlived is the one failure a reminder cannot recover from. Changing this
moves the ``DUE`` of tasks already sitting in subscribers' phones, and publishes
new ones for opened lots that carry no printed date at all; both are covered in
``docs/calendar-feed.md``.

**What each task says.** Product, quantity, place, date -- the four things
somebody needs in order to act on a notification without unlocking their phone
and opening the application. Not the brand, not the barcode, not the price, not
who added it. The feed is a copy of asset A3 leaving the home on a schedule; the
right size for it is the smallest one that still works.

**When it rings.** One alarm per task, the morning before the date, in the
household's own timezone -- a notification at 03:00 UTC is not a reminder, it is
a nuisance. An alarm whose moment has already passed is dropped: an overdue task
already sits at the top of every list that shows it, and re-ringing on every
poll is how a person turns the feed off.

Two things are computed here rather than in the transport because they are
properties of the *data*: the ETag of each resource and the collection tag. Both
are content hashes, so they change when and only when what a client would
download changes. A tag derived from the clock would make every poll a full
re-download; a tag derived from ``max(updated_at)`` would miss the fact that a
lot leaving the window changes the collection without changing any row.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from decimal import Decimal
from typing import Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from chaudron.infra.calendar.credentials import (
    MAX_RESOLVABLE_HOUSEHOLDS,
    FeedCredentials,
    FeedHousehold,
    FeedKeyring,
)
from chaudron.infra.calendar.ical import VTodo, render_calendar
from chaudron.infra.calendar.repository import (
    ExpiringLot,
    SqlExpiringLotReader,
    count_feed_households,
    get_feed_epoch,
    get_household_timezone,
    list_feed_households,
    revoke_feed,
)
from chaudron.infra.db import Database

logger = logging.getLogger(__name__)

#: How far back an already-expired lot keeps being published. Long enough that a
#: weekend away does not make a task vanish before anybody saw it, short enough
#: that a forgotten jar does not sit in somebody's reminder list for a month.
PAST_WINDOW_DAYS: Final = 7

#: How far ahead. Beyond a month, "expiring" stops being an alert and becomes an
#: inventory listing, which is what the application itself is for.
FUTURE_WINDOW_DAYS: Final = 30

#: Hard cap on published tasks. A client fetches the whole collection on its
#: first sync and after every token invalidation, so this is the size of the
#: largest response the feed can ever produce: about 60 KB of iCalendar.
MAX_TASKS: Final = 200

#: Local time of day the reminder fires, on the day before the date. Morning,
#: because that is when a person can still decide what to cook tonight.
ALARM_LOCAL_TIME: Final = time(hour=9, minute=0)

#: Days before the date the alarm fires.
ALARM_LEAD_DAYS: Final = 1

#: Sync tokens are URIs (RFC 6578 §3.1). The path carries a content hash, so a
#: token is comparable without any server-side state to remember.
_SYNC_TOKEN_PREFIX: Final = "https://chaudron.dev/ns/sync/"  # noqa: S105 - a URI, not a secret

#: Separates the product from its quantity in a task's title. An em dash rather
#: than a comma: a comma is a legal character inside a product name and reads as
#: part of it.
_SUMMARY_SEPARATOR: Final = " — "

#: Fraction of :data:`MAX_RESOLVABLE_HOUSEHOLDS` at which the startup report stops
#: being silent. Four fifths: far enough from the wall that adding capacity is a
#: plan rather than an incident, close enough that it is not noise.
_HEADROOM_WARNING_RATIO: Final = 0.8


@dataclass(frozen=True, slots=True)
class FeedTask:
    """One published task: its opaque file name, its bytes, and its ETag."""

    resource_name: str
    todo: VTodo
    payload: str
    etag: str


@dataclass(frozen=True, slots=True)
class FeedSnapshot:
    """The whole collection as of one read, and the tags describing it."""

    tasks: tuple[FeedTask, ...]
    ctag: str

    @property
    def sync_token(self) -> str:
        return f"{_SYNC_TOKEN_PREFIX}{self.ctag}"

    def by_name(self) -> dict[str, FeedTask]:
        return {task.resource_name: task for task in self.tasks}


def format_quantity(value: Decimal) -> str:
    """Render a stored quantity the way it was typed.

    ``NUMERIC`` comes back with its scale intact, so 1 litre is ``1.000``.
    ``normalize`` removes the trailing zeros but re-spells whole numbers in
    scientific notation past a certain size (``1500`` becomes ``1.5E+3``), which
    is why the integral case is quantised back rather than trusted.
    """
    normalised = value.normalize()
    if normalised == normalised.to_integral_value():
        normalised = normalised.quantize(Decimal(1))
    return f"{normalised:f}"


def build_summary(lot: ExpiringLot) -> str:
    """``"Lait demi-écrémé — 1 L"``. Sanitising happens in the renderer."""
    quantity = f"{format_quantity(lot.quantity_value)} {lot.quantity_symbol}"
    return f"{lot.product_name}{_SUMMARY_SEPARATOR}{quantity}"


def _etag(payload: str) -> str:
    """A quoted strong ETag over the exact bytes a ``GET`` would return."""
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
    return f'"{digest}"'


def resolve_zone(name: str | None) -> tzinfo:
    """The household's zone, or UTC if it names one this system does not have.

    A missing tzdata entry must not take the feed down: the consequence of
    falling back is a reminder at the wrong hour, and the consequence of raising
    is no reminders at all.
    """
    if not name:
        return UTC
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError, ValueError:
        logger.warning("calendar_feed_unknown_timezone", extra={"timezone": name})
        return UTC


def alarm_instant(due: date, zone: tzinfo, *, now: datetime) -> datetime | None:
    """When to ring for a task due on ``due``, or ``None`` if that moment is past."""
    local = datetime.combine(due - timedelta(days=ALARM_LEAD_DAYS), ALARM_LOCAL_TIME, tzinfo=zone)
    return None if local <= now else local


class CalendarFeedService:
    """Turns a household's inventory into the tasks its phones may read."""

    def __init__(
        self,
        session: AsyncSession,
        keyring: FeedKeyring,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session = session
        self._keyring = keyring
        self._reader = SqlExpiringLotReader(session)
        self._clock = clock if clock is not None else lambda: datetime.now(UTC)

    async def credentials_for(self, household_id: uuid.UUID) -> FeedCredentials | None:
        """This household's current pair, or ``None`` if it has no live row.

        Asynchronous where the derivation is pure, because the epoch it derives
        from lives in the database: a credential computed without reading the row
        would be the credential that was valid before the last revocation.
        """
        epoch = await get_feed_epoch(self._session, household_id)
        if epoch is None:
            return None
        return self._keyring.credentials_for(FeedHousehold(id=household_id, feed_epoch=epoch))

    async def revoke_credentials(self, household_id: uuid.UUID) -> FeedCredentials | None:
        """Withdraw this household's feed and return the pair that replaces it.

        The old pair stops resolving on the next request -- there is no cache and
        no session to expire -- and every device subscribed to *this* household
        has to be given the new one. No other household is affected, which is the
        entire reason the counter is a column rather than an environment variable.
        """
        epoch = await revoke_feed(self._session, household_id)
        if epoch is None:
            return None
        return self._keyring.credentials_for(FeedHousehold(id=household_id, feed_epoch=epoch))

    async def resolve_household(self, feed_id: str, secret: str) -> uuid.UUID | None:
        """The household these CalDAV credentials name, or ``None``.

        Runs before any tenant is posted, and reads nothing but identifiers and
        revocation counters -- see ``infra/calendar/repository.py`` on why that is
        the one query allowed to be unscoped.
        """
        households = await list_feed_households(self._session)
        return self._keyring.resolve(feed_id, secret, households)

    async def snapshot(self, household_id: uuid.UUID) -> FeedSnapshot:
        """Read the household's window and render every task in it."""
        now = self._clock()
        today = now.date()
        lots = await self._reader.list_expiring(
            household_id,
            earliest=today - timedelta(days=PAST_WINDOW_DAYS),
            latest=today + timedelta(days=FUTURE_WINDOW_DAYS),
            limit=MAX_TASKS,
        )
        zone = resolve_zone(await get_household_timezone(self._session, household_id))
        tasks = tuple(self._build_task(household_id, lot, zone, now=now) for lot in lots)
        return FeedSnapshot(tasks=tasks, ctag=_collection_tag(tasks))

    def _build_task(
        self, household_id: uuid.UUID, lot: ExpiringLot, zone: tzinfo, *, now: datetime
    ) -> FeedTask:
        name = self._keyring.resource_name(household_id, lot.lot_id)
        todo = VTodo(
            # The UID is the resource name: stable across polls, opaque, and
            # already unique per household. A client that sees the same UID under
            # the same href knows it is the same task, which is the whole
            # contract.
            uid=f"{name}@chaudron",
            summary=build_summary(lot),
            # The effective date, not the printed one: a task due on a date the
            # lot has already outlived is a reminder that arrives too late, and
            # the subscriber cannot see the opening date from their lock screen.
            due=lot.effective_expiry,
            location=lot.location_name,
            created=lot.created_at,
            modified=lot.updated_at,
            alarm_at=alarm_instant(lot.effective_expiry, zone, now=now),
        )
        payload = render_calendar([todo])
        return FeedTask(resource_name=name, todo=todo, payload=payload, etag=_etag(payload))


async def report_feed_scan_headroom(database: Database) -> None:
    """Tell the operator at startup whether credential resolution still works.

    Resolution scans the household table (``infra/calendar/credentials.py``), and
    past :data:`MAX_RESOLVABLE_HOUSEHOLDS` it refuses with ``503`` -- for every
    caller, including a subscriber whose credentials are perfectly valid. The
    refusal is deliberate: an instance that large has to persist an indexed
    identifier, and paying a linear cost per request in silence is the outcome the
    bound exists to prevent.

    **Why the bound is not simply replaced by an index.** Nothing here is stored:
    the identifier is a keyed derivation of the instance secret, the instance
    epoch and the household's own counter, and PostgreSQL holds none of the three
    inputs it would need to compute it. Indexing it means persisting it, and a
    persisted identifier has to be rewritten on each of three independent rotation
    paths -- ``CHAUDRON_SECRET_KEY``, ``CHAUDRON_CALENDAR_FEED_EPOCH``, and every
    per-household revocation -- two of which live in the environment where no
    migration and no trigger can see them. A stale row would then fail *closed*
    and silently: the feed stops resolving for a household nobody touched, and
    nothing in the database says why. That is a different identifier design, not
    an index on this one, and it is written up in ``docs/calendar-feed.md``.

    So the bound stays, and what changes is when the operator hears about it: at
    startup, where adding capacity is a decision, rather than on the first poll of
    the first phone. Never fatal -- a calendar feature must not keep the API from
    booting -- and a database that is not up yet is reported as "unverified"
    rather than as a failure, for the reason ``verify_row_level_security`` gives.
    """
    try:
        async with database.session() as session:
            households = await count_feed_households(session)
    except (SQLAlchemyError, OSError) as exc:
        logger.warning("calendar_feed_headroom_unverified", extra={"error": type(exc).__name__})
        return
    if households > MAX_RESOLVABLE_HOUSEHOLDS:
        logger.error(
            "calendar_feed_scan_limit_exceeded",
            extra={"households": households, "limit": MAX_RESOLVABLE_HOUSEHOLDS},
        )
    elif households >= MAX_RESOLVABLE_HOUSEHOLDS * _HEADROOM_WARNING_RATIO:
        logger.warning(
            "calendar_feed_scan_limit_approaching",
            extra={"households": households, "limit": MAX_RESOLVABLE_HOUSEHOLDS},
        )


def _collection_tag(tasks: Sequence[FeedTask]) -> str:
    """A hash over every resource name and ETag in the collection.

    Covers the three ways a collection changes: a task appears, a task
    disappears, a task's content changes. A tag built from the newest
    ``updated_at`` would miss the middle one -- a lot ageing out of the window
    modifies no row at all.
    """
    digest = hashlib.sha256()
    for task in tasks:
        digest.update(task.resource_name.encode("ascii"))
        digest.update(b"\x00")
        digest.update(task.etag.encode("ascii"))
        digest.update(b"\x00")
    return digest.hexdigest()[:32]
