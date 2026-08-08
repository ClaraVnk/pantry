"""The CalDAV surface: enough of RFC 4791 for a phone to subscribe, and no more.

**Why a server and not a file.** The PWA cannot push on iOS unless the user
added it to the home screen (ADR-0004), and expiry alerts are the retention
loop: without them the application is a chore somebody has to remember to open.
Writing into iCloud Reminders from a server is impossible -- no API, no CloudKit
path, and Apple disabled the CalDAV one in iOS 13. The direction that *does*
work is the opposite one: the Reminders app is a perfectly good **client** of a
third-party CalDAV server. So Chaudron is the server.

**Why not a ``webcal://`` subscription, which would be a tenth of this code.**
Because it was checked rather than assumed, and it does not land. A subscribed
``.ics`` of ``VTODO``s has no proven path into iOS Reminders -- the one user
report asking exactly that went unanswered on Apple's own forum, Todoist (who
have the same need and every incentive) converts its tasks to ``VEVENT`` rather
than shipping ``VTODO`` in its feed, and Tasks.org on Android has had an open
ticket for subscribing to an ICS task feed since 2015 with no implementation.
Google Calendar refuses ``VTODO`` outright, by documentation. A read-only
subscription would have been an acceptable answer *if it worked*; the evidence
says it does not, and a feed nobody's phone displays is worse than none.

**What a client actually needs, which is the part that decides the size of this
file.** A CalDAV account setup is a conversation, not a download:

``PROPFIND`` on ``/.well-known/caldav`` (RFC 6764 §6, a 301 to the real context
path) → ``DAV:current-user-principal`` → ``PROPFIND`` on the principal for
``CALDAV:calendar-home-set`` → ``PROPFIND Depth: 1`` on the home to enumerate
collections, reading ``DAV:resourcetype``,
``CALDAV:supported-calendar-component-set`` (this is what makes iOS file the
collection under *Reminders* rather than *Calendars*) and
``CS:getctag`` → then either ``PROPFIND Depth: 1`` for per-resource ETags
followed by a ``calendar-multiget``, or a ``calendar-query`` in one shot.
Everything implemented below is on that path. Everything not on it -- ``LOCK``,
``MKCALENDAR``, ``PUT``, ``DELETE``, free-busy, scheduling, ACL reports -- is
not implemented, and ``DAV: 1, 3, calendar-access`` says so honestly rather than
advertising a class 2 (locking) this server does not have.

**Read-only, and it says so in the protocol.** ``DAV:current-user-privilege-set``
returns ``DAV:read`` alone, so a client knows before it tries that ticking a task
off will not travel back. Write methods answer ``403`` rather than pretending.
"""

from __future__ import annotations

import base64
import binascii
import logging
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from email.utils import format_datetime
from typing import Annotated, Final
from xml.etree.ElementTree import Element

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from chaudron.api.deps import HouseholdDep, OwnerDep, SessionDep, get_settings_dep
from chaudron.api.errors import ProblemError, rate_limited
from chaudron.api.throttling import AtCapacityError
from chaudron.config import Settings
from chaudron.infra.calendar.credentials import (
    FeedCredentials,
    FeedKeyring,
    TooManyHouseholdsError,
    looks_issued,
)
from chaudron.infra.calendar.ical import CALENDAR_CONTENT_TYPE, render_calendar
from chaudron.infra.calendar.webdav import (
    CALENDAR_MULTIGET,
    CALENDAR_QUERY,
    SYNC_COLLECTION,
    XML_CONTENT_TYPE,
    MalformedRequestError,
    ResourceResponse,
    build_error,
    build_multistatus,
    caldav,
    calendarserver,
    dav,
    element,
    parse_propfind,
    parse_report,
)
from chaudron.infra.logging import household_id_var
from chaudron.infra.rate_limits import BucketPolicy, SharedRateLimiter
from chaudron.services.calendar import (
    FUTURE_WINDOW_DAYS,
    MAX_TASKS,
    PAST_WINDOW_DAYS,
    CalendarFeedService,
    FeedSnapshot,
    FeedTask,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["calendar"])

#: Root of the CalDAV context path. ``/.well-known/caldav`` redirects here, and
#: this is what a user types into the "Server" field of an account setup screen.
CALDAV_ROOT: Final = "/caldav/"

#: What the collection is called on the user's phone. English, like every other
#: string this API emits; it is the name of the list, and the user may rename it
#: locally on any client worth using.
CALENDAR_DISPLAY_NAME: Final = "Chaudron expiry"
CALENDAR_DESCRIPTION: Final = "Food in this household's stock that is about to expire."
PRINCIPAL_DISPLAY_NAME: Final = "Chaudron"

#: Classes this server genuinely implements. **Not** class 2: that is locking,
#: which a read-only server has no use for and must not claim. sabre/dav -- the
#: server most iOS clients have been tested against -- does not advertise it
#: either.
DAV_COMPLIANCE: Final = "1, 3, calendar-access"

_ALLOWED_METHODS: Final = "OPTIONS, GET, HEAD, PROPFIND, REPORT"

#: Largest calendar object this server will ever produce, quoted to clients
#: through ``CALDAV:max-resource-size`` so they do not have to guess.
_MAX_RESOURCE_SIZE: Final = 16 * 1024

#: Polls per hour per feed. A well-behaved client syncs at most four times an
#: hour and spends a handful of requests doing it; this leaves room for several
#: devices in one household and still caps a loop.
_FEED_REQUESTS_PER_HOUR: Final = 240

#: Failed authentications per hour per source address. Guessing a 160-bit secret
#: is not the threat -- the cost of *checking* one is, since each attempt reads
#: the household list and derives a MAC per row. Charged **before** that read and
#: refunded once the request is admitted, so the count bounds the work rather than
#: describing it after the fact (:func:`authenticate`).
_FAILED_AUTH_PER_HOUR: Final = 30

_HOUR: Final = 3600.0


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


def _unauthorized() -> ProblemError:
    """One answer for every authentication failure, with the Basic challenge.

    Missing header, malformed header, unknown feed and wrong secret are
    indistinguishable, for the reason ``api/deps.py`` gives about
    ``X-Household-Id``: three distinguishable answers make the endpoint an oracle
    that confirms which feed identifiers exist.
    """
    return ProblemError(
        slug="calendar-feed-unauthorized",
        title="Calendar feed credentials required",
        status=401,
        detail="This feed requires the user name and password shown in Chaudron.",
        headers={"WWW-Authenticate": 'Basic realm="Chaudron calendar", charset="UTF-8"'},
    )


def _not_found() -> ProblemError:
    return ProblemError(
        slug="calendar-resource-not-found",
        title="Not found",
        status=404,
        detail="No such calendar resource.",
    )


def _read_only() -> ProblemError:
    return ProblemError(
        slug="calendar-feed-read-only",
        title="Read-only calendar",
        status=403,
        detail="This calendar is published read-only; changes made here are not stored.",
    )


def _bad_request(detail: str) -> ProblemError:
    return ProblemError(
        slug="calendar-request-invalid", title="Invalid request", status=400, detail=detail
    )


# --------------------------------------------------------------------------- #
# Throttling
# --------------------------------------------------------------------------- #
#
# Built lazily on ``app.state`` rather than by the application factory, so that
# adding this feature touches ``api/main.py`` in exactly one line. Same scope as
# every other rate cap: rows in PostgreSQL, shared by every worker of every
# replica (``infra/rate_limits.py``). What is per-process here is only the pair
# of Python objects -- the buckets they read are not, so the failure cap below
# still means thirty an hour on a deployment running four workers.


@dataclass(frozen=True, slots=True)
class _FeedThrottles:
    polls: SharedRateLimiter
    failures: SharedRateLimiter


def _throttles(request: Request) -> _FeedThrottles:
    existing: _FeedThrottles | None = getattr(request.app.state, "calendar_throttles", None)
    if existing is not None:
        return existing
    # The limiters' own pool, for the reason `infra/db.py` sets out: this one is
    # reached while the request holds a session connection, and drawing both from
    # one pool is how a rate limiter becomes the outage it exists to prevent.
    engine = request.app.state.database.limiter_engine
    prefix: str = request.app.state.rate_limit_scope_prefix
    built = _FeedThrottles(
        polls=SharedRateLimiter(
            engine,
            BucketPolicy(
                scope=f"{prefix}calendar_feed_polls",
                limit=_FEED_REQUESTS_PER_HOUR,
                window_seconds=_HOUR,
            ),
        ),
        failures=SharedRateLimiter(
            engine,
            BucketPolicy(
                scope=f"{prefix}calendar_feed_failures",
                limit=_FAILED_AUTH_PER_HOUR,
                window_seconds=_HOUR,
            ),
        ),
    )
    request.app.state.calendar_throttles = built
    return built


def _client_key(request: Request) -> str:
    """Best available identity for an unauthenticated caller: the real client address.

    **Not the proxy's**, and that is worth stating because the obvious reading of
    this line says otherwise. In the deployed shape ``scope["client"]`` has
    already been rewritten by uvicorn's proxy-headers middleware, and three
    things have to hold together for the value to be both true and unforgeable:

    * ``scripts/entrypoint.sh`` passes ``--proxy-headers --forwarded-allow-ips``
      only when ``FORWARDED_ALLOW_IPS`` names something, and **refuses to start**
      on ``FORWARDED_ALLOW_IPS=*``. Unset means ``--no-proxy-headers``: forwarded
      headers are not read at all;
    * ``ops/chaudron.container`` sets that variable to the reverse proxy's pinned
      address on ``chaudron-net`` (``10.89.7.10``), and nothing else on that
      network may reach this port;
    * uvicorn takes the **rightmost address that is not trusted**, so a header a
      client prepends is behind the proxy's own entry and never wins. Caddy
      overwrites ``X-Forwarded-For`` with the peer it actually accepted
      (``ops/Caddyfile.example`` sets no ``trusted_proxies``), so the value is
      verified by something rather than merely believed.

    So each subscriber has a bucket of their own. Measured against
    ``_TrustedHosts`` rather than paraphrased, because the rule reads backwards
    and is easy to state upside down: with the proxy pinned, ``203.0.113.7,
    198.51.100.42`` resolves to ``198.51.100.42`` -- the **rightmost**, the entry
    the nearest trusted hop wrote. Under ``*`` the same header resolves to
    ``203.0.113.7``, the half a client can type, which is the whole reason the
    entrypoint refuses that value.

    **What that leaves.** A client looping on a credential its household revoked
    locks *itself* out for the hour, which is the intended behaviour and not a
    bug to route around. And a bare container run without the proxy falls back to
    the real peer, which under rootless Podman is the network gateway -- so a
    development shape does collapse callers together. The deployed one does not,
    and the single configuration that would reintroduce it, ``*``, is the one the
    entrypoint will not boot with.
    """
    return request.client.host if request.client is not None else "unknown"


# --------------------------------------------------------------------------- #
# Authentication
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class FeedContext:
    """A resolved feed: which household, under which opaque identifier."""

    household_id: uuid.UUID
    feed_id: str
    service: CalendarFeedService


def _require_enabled(settings: Settings) -> None:
    """A disabled feed is absent, not forbidden.

    ``404`` rather than ``403``: an instance that has never turned this on should
    not answer differently from one that has no such route at all.
    """
    if not settings.calendar_feed_enabled:
        raise _not_found()


def _basic_credentials(request: Request) -> tuple[str, str]:
    header = request.headers.get("Authorization", "")
    scheme, _, encoded = header.partition(" ")
    if scheme.lower() != "basic" or not encoded:
        raise _unauthorized()
    try:
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
    except binascii.Error, UnicodeDecodeError, ValueError:
        raise _unauthorized() from None
    feed_id, separator, secret = decoded.partition(":")
    if not separator:
        raise _unauthorized()
    # Shape-checked before anything touches the database: length *and* alphabet,
    # because the derivation emits base32 and nothing else. Rejecting here keeps a
    # flood of malformed guesses off the household scan, and it keeps a non-ASCII
    # one away from ``hmac.compare_digest``, which raises rather than returning
    # ``False`` on text it cannot encode. See :func:`looks_issued`.
    if not looks_issued(feed_id, secret):
        raise _unauthorized()
    return feed_id, secret


async def authenticate(
    request: Request,
    session: SessionDep,
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> FeedContext:
    """Resolve the Basic credentials, then post the tenant for the transaction.

    Order is load-bearing twice over.

    **The household is discovered first**, from the credential and from nothing
    the client asserts, and only then written to ``household_id_var`` -- which is
    what makes the session layer emit ``SET LOCAL chaudron.household_id`` before
    the next statement (``infra/db.py``). Every read after that line is inside
    row-level security; anything that ran before it read identifiers only.

    **The failure limiter is charged before the work it bounds, not after.** The
    threat this endpoint faces is not somebody guessing a 160-bit secret; it is
    the *cost of checking* one, which is a read of the household table and one MAC
    per row. A limiter placed after the scan changes the response and not the
    work: every attempt still pays, and the ``429`` arrives having spent exactly
    what it was meant to save. So the token is taken up front and given back once
    the request has been admitted, which leaves a subscriber polling normally at a
    net cost of zero and leaves a failed attempt paid for.

    The refund is withheld when the poll limiter refuses, and that is the one
    deliberate edge. A client hammering a valid credential has already had its
    scan; refunding there would leave the cost unbounded for anyone holding one
    credential, which on an instance that accepts registrations is anyone at all.
    A client that stays under :data:`_FEED_REQUESTS_PER_HOUR` -- four polls an
    hour is the normal figure, and the cap is sixty times that -- never reaches it.
    """
    _require_enabled(settings)
    throttles = _throttles(request)
    feed_id, secret = _basic_credentials(request)

    client_key = _client_key(request)
    try:
        await throttles.failures.acquire(client_key)
    except AtCapacityError as exc:
        raise rate_limited(
            detail="Too many failed calendar feed authentications.",
            retry_after=exc.retry_after,
        ) from None

    service = CalendarFeedService(session, FeedKeyring.from_settings(settings))
    try:
        household_id = await service.resolve_household(feed_id, secret)
    except TooManyHouseholdsError:
        logger.error("calendar_feed_scan_limit_reached")
        raise ProblemError(
            slug="calendar-feed-unavailable",
            title="Calendar feed unavailable",
            status=503,
            detail="The calendar feed is not available on this instance.",
        ) from None

    if household_id is None:
        raise _unauthorized()

    try:
        await throttles.polls.acquire(feed_id)
    except AtCapacityError as exc:
        raise rate_limited(
            detail="This calendar feed is being polled too often.", retry_after=exc.retry_after
        ) from None
    await throttles.failures.refund(client_key)

    household_id_var.set(str(household_id))
    return FeedContext(household_id=household_id, feed_id=feed_id, service=service)


FeedDep = Annotated[FeedContext, Depends(authenticate)]


def _own_feed(feed: FeedContext, feed_id: str) -> None:
    """The path must name the feed the credentials authenticated.

    ``404``, not ``403``: a caller holding one valid credential must not be able
    to probe whether another identifier exists.
    """
    if feed_id != feed.feed_id:
        raise _not_found()


# --------------------------------------------------------------------------- #
# URLs
# --------------------------------------------------------------------------- #


def _principal_href(feed_id: str) -> str:
    return f"{CALDAV_ROOT}p/{feed_id}/"


def _home_href(feed_id: str) -> str:
    return f"{_principal_href(feed_id)}cal/"


def _calendar_href(feed_id: str) -> str:
    return f"{_home_href(feed_id)}expiry/"


def _resource_href(feed_id: str, resource_name: str) -> str:
    return f"{_calendar_href(feed_id)}{resource_name}.ics"


# --------------------------------------------------------------------------- #
# Responses
# --------------------------------------------------------------------------- #


def _dav_headers() -> dict[str, str]:
    return {
        "DAV": DAV_COMPLIANCE,
        # Every response on this tree is one household's inventory. The security
        # middleware only marks ``/v1/*`` as private, so this path says it here.
        "Cache-Control": "no-store",
    }


def _multistatus(
    responses: Iterable[ResourceResponse], *, sync_token: str | None = None
) -> Response:
    return Response(
        content=build_multistatus(responses, sync_token=sync_token),
        status_code=207,
        media_type=XML_CONTENT_TYPE,
        headers=_dav_headers(),
    )


def _split(
    available: Mapping[str, Element], requested: Sequence[str], *, all_properties: bool
) -> tuple[dict[str, Element], list[str]]:
    """Split what was asked for into what exists here and what does not."""
    if all_properties:
        return dict(available), []
    found = {name: available[name] for name in requested if name in available}
    missing = [name for name in requested if name not in available]
    return found, missing


def _href(target: str) -> Element:
    return element(dav("href"), target)


def _resourcetype(*kinds: str) -> Element:
    return element(dav("resourcetype"), None, *(element(kind) for kind in kinds))


def _privilege_set() -> Element:
    """``DAV:read`` and nothing else -- the protocol's way of saying read-only."""
    return element(
        dav("current-user-privilege-set"),
        None,
        element(dav("privilege"), None, element(dav("read"))),
        element(dav("privilege"), None, element(dav("read-current-user-privilege-set"))),
    )


def _supported_reports(*names: Element) -> Element:
    return element(
        dav("supported-report-set"),
        None,
        *(
            element(dav("supported-report"), None, element(dav("report"), None, name))
            for name in names
        ),
    )


def _root_properties(feed_id: str) -> dict[str, Element]:
    return {
        dav("resourcetype"): _resourcetype(dav("collection")),
        dav("displayname"): element(dav("displayname"), PRINCIPAL_DISPLAY_NAME),
        dav("current-user-principal"): element(
            dav("current-user-principal"), None, _href(_principal_href(feed_id))
        ),
        dav("principal-URL"): element(dav("principal-URL"), None, _href(_principal_href(feed_id))),
        caldav("calendar-home-set"): element(
            caldav("calendar-home-set"), None, _href(_home_href(feed_id))
        ),
    }


def _principal_properties(feed_id: str) -> dict[str, Element]:
    principal = _href(_principal_href(feed_id))
    return {
        dav("resourcetype"): _resourcetype(dav("collection"), dav("principal")),
        dav("displayname"): element(dav("displayname"), PRINCIPAL_DISPLAY_NAME),
        dav("current-user-principal"): element(dav("current-user-principal"), None, principal),
        dav("principal-URL"): element(dav("principal-URL"), None, _href(_principal_href(feed_id))),
        dav("owner"): element(dav("owner"), None, _href(_principal_href(feed_id))),
        caldav("calendar-home-set"): element(
            caldav("calendar-home-set"), None, _href(_home_href(feed_id))
        ),
        dav("current-user-privilege-set"): _privilege_set(),
    }


def _home_properties(feed_id: str) -> dict[str, Element]:
    return {
        dav("resourcetype"): _resourcetype(dav("collection")),
        dav("displayname"): element(dav("displayname"), PRINCIPAL_DISPLAY_NAME),
        dav("current-user-principal"): element(
            dav("current-user-principal"), None, _href(_principal_href(feed_id))
        ),
        dav("owner"): element(dav("owner"), None, _href(_principal_href(feed_id))),
        caldav("calendar-home-set"): element(
            caldav("calendar-home-set"), None, _href(_home_href(feed_id))
        ),
        dav("current-user-privilege-set"): _privilege_set(),
    }


def _calendar_properties(feed_id: str, snapshot: FeedSnapshot) -> dict[str, Element]:
    """The properties that decide how a client files this collection.

    ``supported-calendar-component-set`` naming ``VTODO`` alone is the one that
    matters most: it is what makes iOS offer the collection under *Reminders*
    instead of listing it among calendars, and what lets DAVx5 route it to a
    task application on Android.
    """
    return {
        dav("resourcetype"): _resourcetype(dav("collection"), caldav("calendar")),
        dav("displayname"): element(dav("displayname"), CALENDAR_DISPLAY_NAME),
        dav("current-user-principal"): element(
            dav("current-user-principal"), None, _href(_principal_href(feed_id))
        ),
        dav("owner"): element(dav("owner"), None, _href(_principal_href(feed_id))),
        dav("current-user-privilege-set"): _privilege_set(),
        dav("sync-token"): element(dav("sync-token"), snapshot.sync_token),
        dav("supported-report-set"): _supported_reports(
            element(caldav(CALENDAR_QUERY)),
            element(caldav(CALENDAR_MULTIGET)),
            element(dav(SYNC_COLLECTION)),
        ),
        caldav("calendar-description"): element(
            caldav("calendar-description"), CALENDAR_DESCRIPTION
        ),
        caldav("supported-calendar-component-set"): element(
            caldav("supported-calendar-component-set"),
            None,
            _component("VTODO"),
        ),
        caldav("supported-calendar-data"): element(
            caldav("supported-calendar-data"), None, _calendar_data_type()
        ),
        caldav("max-resource-size"): element(caldav("max-resource-size"), str(_MAX_RESOURCE_SIZE)),
        calendarserver("getctag"): element(calendarserver("getctag"), snapshot.ctag),
    }


def _component(name: str) -> Element:
    node = element(caldav("comp"))
    node.set("name", name)
    return node


def _calendar_data_type() -> Element:
    node = element(caldav("calendar-data"))
    node.set("content-type", "text/calendar")
    node.set("version", "2.0")
    return node


def _http_date(moment: datetime) -> str:
    return format_datetime(moment, usegmt=True)


def _task_properties(task: FeedTask) -> dict[str, Element]:
    return {
        dav("resourcetype"): element(dav("resourcetype")),
        dav("getetag"): element(dav("getetag"), task.etag),
        dav("getcontenttype"): element(dav("getcontenttype"), CALENDAR_CONTENT_TYPE),
        dav("getcontentlength"): element(
            dav("getcontentlength"), str(len(task.payload.encode("utf-8")))
        ),
        dav("getlastmodified"): element(dav("getlastmodified"), _http_date(task.todo.modified)),
        dav("current-user-privilege-set"): _privilege_set(),
        caldav("calendar-data"): element(caldav("calendar-data"), task.payload),
    }


def _depth(request: Request, *, default: str = "0") -> str:
    """Read and validate ``Depth``.

    ``infinity`` is refused with the precondition RFC 4918 §9.1 defines for it.
    A depth-infinity ``PROPFIND`` is unbounded work requested by a protocol whose
    callers are strangers until proven otherwise; servers are explicitly allowed
    to decline it, and every client falls back to ``Depth: 1``.

    A **missing** header is read as ``0`` rather than as the ``infinity`` the RFC
    suggests. That is a deliberate deviation and the only one that makes sense
    here: reading it as ``infinity`` would mean answering ``403`` to a request
    that omitted a header, where returning the resource itself is both useful and
    safe. Every client the feed is meant for sends ``Depth`` explicitly.
    """
    depth = request.headers.get("Depth", default).strip().lower()
    if depth == "infinity":
        raise ProblemError(
            slug="calendar-depth-infinity",
            title="Depth: infinity is not supported",
            status=403,
            detail="Use Depth: 0 or Depth: 1.",
        )
    if depth not in {"0", "1"}:
        raise _bad_request("Depth must be 0 or 1.")
    return depth


async def _propfind_body(request: Request) -> tuple[tuple[str, ...], bool]:
    try:
        parsed = parse_propfind(await request.body())
    except MalformedRequestError as exc:
        raise _bad_request(str(exc)) from None
    return parsed.properties, parsed.all_properties


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #


async def well_known(
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> Response:
    """RFC 6764 §6: point a client that only knows the host at the context path."""
    _require_enabled(settings)
    return Response(status_code=301, headers={"Location": CALDAV_ROOT, **_dav_headers()})


async def options_root(settings: Annotated[Settings, Depends(get_settings_dep)]) -> Response:
    """Answered without credentials: a client sends it before it has any.

    It discloses nothing about whether any particular feed exists -- only that
    this host speaks CalDAV, which the source code says too.
    """
    _require_enabled(settings)
    return Response(status_code=204, headers={**_dav_headers(), "Allow": _ALLOWED_METHODS})


async def propfind_root(request: Request, feed: FeedDep) -> Response:
    """The entry point every client starts from: "who am I here?"."""
    _depth(request)
    requested, everything = await _propfind_body(request)
    found, missing = _split(_root_properties(feed.feed_id), requested, all_properties=everything)
    return _multistatus([ResourceResponse(href=CALDAV_ROOT, found=found, missing=missing)])


async def propfind_principal(request: Request, feed_id: str, feed: FeedDep) -> Response:
    _own_feed(feed, feed_id)
    _depth(request)
    requested, everything = await _propfind_body(request)
    found, missing = _split(_principal_properties(feed_id), requested, all_properties=everything)
    return _multistatus(
        [ResourceResponse(href=_principal_href(feed_id), found=found, missing=missing)]
    )


async def propfind_home(request: Request, feed_id: str, feed: FeedDep) -> Response:
    """The home collection, and at ``Depth: 1`` the one calendar it holds."""
    _own_feed(feed, feed_id)
    depth = _depth(request)
    requested, everything = await _propfind_body(request)

    found, missing = _split(_home_properties(feed_id), requested, all_properties=everything)
    responses = [ResourceResponse(href=_home_href(feed_id), found=found, missing=missing)]

    if depth == "1":
        snapshot = await feed.service.snapshot(feed.household_id)
        found, missing = _split(
            _calendar_properties(feed_id, snapshot), requested, all_properties=everything
        )
        responses.append(
            ResourceResponse(href=_calendar_href(feed_id), found=found, missing=missing)
        )
    return _multistatus(responses)


# --------------------------------------------------------------------------- #
# The calendar collection
# --------------------------------------------------------------------------- #


async def propfind_calendar(request: Request, feed_id: str, feed: FeedDep) -> Response:
    """The collection, and at ``Depth: 1`` an ETag per task.

    This is the request a client repeats forever once subscribed, so it is the
    one whose cost matters: one bounded query and one hash per task.
    """
    _own_feed(feed, feed_id)
    depth = _depth(request)
    requested, everything = await _propfind_body(request)
    snapshot = await feed.service.snapshot(feed.household_id)

    found, missing = _split(
        _calendar_properties(feed_id, snapshot), requested, all_properties=everything
    )
    responses = [ResourceResponse(href=_calendar_href(feed_id), found=found, missing=missing)]
    if depth == "1":
        responses.extend(_task_responses(feed_id, snapshot.tasks, requested, everything))
    return _multistatus(responses)


def _task_responses(
    feed_id: str,
    tasks: Iterable[FeedTask],
    requested: Sequence[str],
    all_properties: bool,
) -> list[ResourceResponse]:
    responses = []
    for task in tasks:
        found, missing = _split(_task_properties(task), requested, all_properties=all_properties)
        responses.append(
            ResourceResponse(
                href=_resource_href(feed_id, task.resource_name), found=found, missing=missing
            )
        )
    return responses


async def report_calendar(request: Request, feed_id: str, feed: FeedDep) -> Response:
    """``calendar-query``, ``calendar-multiget`` and ``sync-collection``.

    The three reports a subscribing client uses, and nothing else. Free-busy and
    the scheduling reports are meaningless for a read-only task collection.
    """
    _own_feed(feed, feed_id)
    try:
        report = parse_report(await request.body())
    except MalformedRequestError as exc:
        raise _bad_request(str(exc)) from None

    snapshot = await feed.service.snapshot(feed.household_id)

    if report.kind == CALENDAR_QUERY:
        # A filter naming only VEVENT gets an empty answer rather than a wrong
        # one: this collection holds tasks, and returning nothing is the honest
        # reading of a filter that asks for something else.
        tasks: Sequence[FeedTask] = (
            snapshot.tasks if not report.components or "VTODO" in report.components else ()
        )
        return _multistatus(
            _task_responses(feed_id, tasks, report.properties, all_properties=False)
        )

    if report.kind == CALENDAR_MULTIGET:
        by_name = snapshot.by_name()
        responses: list[ResourceResponse] = []
        for href in report.hrefs:
            task = by_name.get(_resource_name_from_href(href))
            if task is None:
                responses.append(ResourceResponse(href=href, found={}, missing=[]))
                continue
            found, missing = _split(_task_properties(task), report.properties, all_properties=False)
            responses.append(
                ResourceResponse(
                    href=_resource_href(feed_id, task.resource_name),
                    found=found,
                    missing=missing,
                )
            )
        return _multistatus(responses)

    return _sync_collection(feed_id, snapshot, report.sync_token, report.properties)


def _sync_collection(
    feed_id: str, snapshot: FeedSnapshot, token: str | None, requested: Sequence[str]
) -> Response:
    """RFC 6578, with the one honest answer a stateless server can give.

    An empty token means "first synchronisation": send everything. A token equal
    to the current one means "nothing changed": send an empty answer, which saves
    the client the whole collection. **Any other token is refused** with
    ``DAV:valid-sync-token``, because this server keeps no history and therefore
    cannot say which resources were *removed* since then. Replying with the
    current collection instead would leave a deleted task on the phone forever,
    which is precisely the failure this report exists to prevent -- and a 403
    here is the documented signal that makes the client fall back to a full
    ``PROPFIND``.
    """
    if not token:
        return _multistatus(
            _task_responses(feed_id, snapshot.tasks, requested, all_properties=False),
            sync_token=snapshot.sync_token,
        )
    if token == snapshot.sync_token:
        return _multistatus([], sync_token=snapshot.sync_token)
    return Response(
        content=build_error(dav("valid-sync-token")),
        status_code=403,
        media_type=XML_CONTENT_TYPE,
        headers=_dav_headers(),
    )


def _resource_name_from_href(href: str) -> str:
    """The opaque name inside an href, without trusting the rest of it.

    A multiget href is a client-supplied path. Nothing here resolves it against
    the filesystem or the URL space; only the final segment is read, stripped of
    its extension, and looked up in a dictionary built from this household's own
    snapshot. A traversal attempt reaches a dictionary miss.
    """
    tail = href.rstrip("/").rsplit("/", maxsplit=1)[-1]
    return tail.removesuffix(".ics")


async def get_calendar(feed_id: str, feed: FeedDep) -> Response:
    """The whole collection as one iCalendar document.

    Not part of CalDAV -- collections are not ``GET``-able there -- and no client
    subscribes this way. It exists so a person can check with ``curl`` that their
    credentials work and that the content is what they expect, which is the first
    thing anybody asks when a phone shows an empty list.
    """
    _own_feed(feed, feed_id)
    snapshot = await feed.service.snapshot(feed.household_id)
    return Response(
        content=render_calendar([task.todo for task in snapshot.tasks]),
        media_type=CALENDAR_CONTENT_TYPE,
        headers={**_dav_headers(), "ETag": f'"{snapshot.ctag}"'},
    )


# --------------------------------------------------------------------------- #
# One task
# --------------------------------------------------------------------------- #


async def get_task(feed_id: str, resource_name: str, feed: FeedDep) -> Response:
    _own_feed(feed, feed_id)
    snapshot = await feed.service.snapshot(feed.household_id)
    task = snapshot.by_name().get(resource_name)
    if task is None:
        raise _not_found()
    return Response(
        content=task.payload,
        media_type=CALENDAR_CONTENT_TYPE,
        headers={**_dav_headers(), "ETag": task.etag},
    )


async def propfind_task(
    request: Request, feed_id: str, resource_name: str, feed: FeedDep
) -> Response:
    _own_feed(feed, feed_id)
    _depth(request)
    requested, everything = await _propfind_body(request)
    snapshot = await feed.service.snapshot(feed.household_id)
    task = snapshot.by_name().get(resource_name)
    if task is None:
        raise _not_found()
    found, missing = _split(_task_properties(task), requested, all_properties=everything)
    return _multistatus(
        [
            ResourceResponse(
                href=_resource_href(feed_id, resource_name), found=found, missing=missing
            )
        ]
    )


async def refuse_write(feed: FeedDep) -> Response:
    """``PUT``/``DELETE``/``MKCALENDAR`` and friends, answered honestly.

    A client that ticks a task off gets a refusal rather than a success it would
    then have to reconcile. Authentication still runs first, so an anonymous
    caller cannot use this to learn that the path exists.
    """
    raise _read_only()


# --------------------------------------------------------------------------- #
# Subscription details, for the household that owns the feed
# --------------------------------------------------------------------------- #


class CalendarSubscriptionOut(BaseModel):
    """Everything a person has to type into a phone, and nothing they can guess.

    ``password`` is a bearer credential. It is returned here and nowhere else, it
    is never logged, and it is never stored: it is re-derived on each request from
    the instance key and this household's ``calendar_feed_epoch``.

    **Owner-only, and the reason is stronger than "it is a secret".** A member's
    access ends when their membership does, on the next request. This credential
    does not: it is a bearer secret that keeps working until somebody revokes it
    on purpose, so handing it out is a decision with a longer shadow than granting
    the read access it duplicates. That decision belongs to the owner, who is also
    the only person who can take it back -- see :func:`revoke_subscription`.

    This route used to sit behind ``X-Household-Id`` alone, which was an address
    rather than a proof (audit AUD-001); its author flagged it in
    ``docs/calendar-feed.md`` as owner-only work for whenever authentication
    landed. It has landed, and :class:`chaudron.api.deps.OwnerDep` is the change.
    """

    server_url: str = Field(description="What to type in the Server field of a CalDAV account.")
    username: str = Field(description="The feed identifier. Opaque; names no household.")
    password: str = Field(description="The feed secret. Treat it as a password.")
    calendar_url: str = Field(description="Direct URL of the collection, for clients that ask.")
    window_days_past: int
    window_days_future: int
    max_tasks: int


def _require_feed_enabled(settings: Settings) -> None:
    if settings.calendar_feed_enabled:
        return
    raise ProblemError(
        slug="calendar-feed-disabled",
        title="Calendar feed disabled",
        status=404,
        detail=(
            "This instance does not publish calendar feeds. "
            "Set CHAUDRON_CALENDAR_FEED_ENABLED=true to enable it."
        ),
    )


def _subscription_out(settings: Settings, credentials: FeedCredentials) -> CalendarSubscriptionOut:
    base = settings.base_url.rstrip("/")
    return CalendarSubscriptionOut(
        server_url=f"{base}{CALDAV_ROOT}",
        username=credentials.feed_id,
        password=credentials.secret,
        calendar_url=f"{base}{_calendar_href(credentials.feed_id)}",
        window_days_past=PAST_WINDOW_DAYS,
        window_days_future=FUTURE_WINDOW_DAYS,
        max_tasks=MAX_TASKS,
    )


def _feed_service(session: AsyncSession, settings: Settings) -> CalendarFeedService:
    return CalendarFeedService(session, FeedKeyring.from_settings(settings))


async def subscription(
    household_id: HouseholdDep,
    _owner: OwnerDep,
    session: SessionDep,
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> CalendarSubscriptionOut:
    """Show this household its feed credentials. Owner only."""
    _require_feed_enabled(settings)
    credentials = await _feed_service(session, settings).credentials_for(household_id)
    if credentials is None:  # pragma: no cover - OwnerDep resolved a live household
        raise _not_found()
    return _subscription_out(settings, credentials)


async def revoke_subscription(
    household_id: HouseholdDep,
    _owner: OwnerDep,
    session: SessionDep,
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> CalendarSubscriptionOut:
    """Withdraw this household's feed credential and issue the pair that replaces it.

    **What it breaks, said plainly because the caller has to relay it.** Every
    device subscribed to *this* household stops synchronising on its next poll --
    each phone, tablet and desktop client has to be given the new user name and
    password by hand. Nothing else changes: no session is signed out, no other
    household is touched, and the stock itself is not altered.

    **What it is for.** The credential is derived, never stored, and therefore
    cannot be deleted. It also outlives membership: somebody who opened this page
    once keeps reading the household's stock after their membership is withdrawn,
    because the CalDAV tree authenticates with the credential and not with a
    session. This is the control that ends that -- the counter it increments is an
    input to the derivation, so the old pair resolves to nothing from the next
    request onwards.

    Owner-only for the same reason the page itself is: a member who could revoke
    could also lock every other device in the household out of the feed.
    """
    _require_feed_enabled(settings)
    credentials = await _feed_service(session, settings).revoke_credentials(household_id)
    if credentials is None:  # pragma: no cover - OwnerDep resolved a live household
        raise _not_found()
    logger.info("calendar_feed_revoked")
    return _subscription_out(settings, credentials)


# --------------------------------------------------------------------------- #
# Routing
# --------------------------------------------------------------------------- #
#
# Registered with ``add_api_route`` rather than with decorators because the
# methods are not the HTTP verbs FastAPI has decorators for. ``include_in_schema``
# is off throughout: OpenAPI has no way to describe ``PROPFIND``, and a CalDAV
# client has never read a schema in its life.

_WRITE_METHODS = [
    "PUT",
    "DELETE",
    "POST",
    "PATCH",
    "PROPPATCH",
    "MKCALENDAR",
    "MKCOL",
    "MOVE",
    "COPY",
]

router.add_api_route(
    "/.well-known/caldav",
    well_known,
    methods=["GET", "PROPFIND", "OPTIONS"],
    include_in_schema=False,
)
router.add_api_route(CALDAV_ROOT, options_root, methods=["OPTIONS"], include_in_schema=False)
router.add_api_route(CALDAV_ROOT, propfind_root, methods=["PROPFIND"], include_in_schema=False)

router.add_api_route(
    "/caldav/p/{feed_id}/", propfind_principal, methods=["PROPFIND"], include_in_schema=False
)
router.add_api_route(
    "/caldav/p/{feed_id}/", options_root, methods=["OPTIONS"], include_in_schema=False
)
router.add_api_route(
    "/caldav/p/{feed_id}/cal/", propfind_home, methods=["PROPFIND"], include_in_schema=False
)
router.add_api_route(
    "/caldav/p/{feed_id}/cal/", options_root, methods=["OPTIONS"], include_in_schema=False
)
router.add_api_route(
    "/caldav/p/{feed_id}/cal/expiry/",
    propfind_calendar,
    methods=["PROPFIND"],
    include_in_schema=False,
)
router.add_api_route(
    "/caldav/p/{feed_id}/cal/expiry/",
    report_calendar,
    methods=["REPORT"],
    include_in_schema=False,
)
router.add_api_route(
    "/caldav/p/{feed_id}/cal/expiry/", get_calendar, methods=["GET"], include_in_schema=False
)
router.add_api_route(
    "/caldav/p/{feed_id}/cal/expiry/",
    options_root,
    methods=["OPTIONS"],
    include_in_schema=False,
)
router.add_api_route(
    "/caldav/p/{feed_id}/cal/expiry/",
    refuse_write,
    methods=_WRITE_METHODS,
    include_in_schema=False,
)
router.add_api_route(
    "/caldav/p/{feed_id}/cal/expiry/{resource_name}.ics",
    get_task,
    methods=["GET"],
    include_in_schema=False,
)
router.add_api_route(
    "/caldav/p/{feed_id}/cal/expiry/{resource_name}.ics",
    propfind_task,
    methods=["PROPFIND"],
    include_in_schema=False,
)
router.add_api_route(
    "/caldav/p/{feed_id}/cal/expiry/{resource_name}.ics",
    refuse_write,
    methods=_WRITE_METHODS,
    include_in_schema=False,
)

router.add_api_route(
    "/v1/calendar/subscription",
    subscription,
    methods=["GET"],
    response_model=CalendarSubscriptionOut,
    summary="CalDAV subscription details for the current household",
)
router.add_api_route(
    "/v1/calendar/subscription/revoke",
    revoke_subscription,
    methods=["POST"],
    response_model=CalendarSubscriptionOut,
    summary="Revoke this household's calendar credentials and issue new ones",
    description=(
        "Owner only. Every device subscribed to **this household's** calendar feed "
        "stops synchronising and has to be given the new user name and password by "
        "hand. No other household is affected, nobody is signed out, and the stock "
        "is unchanged. Use it when the credential has been seen by somebody who "
        "should no longer read the stock -- a lost phone, or a person who has left "
        "the household."
    ),
)
