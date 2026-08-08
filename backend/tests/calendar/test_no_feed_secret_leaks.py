"""SEC-003, applied to the calendar feed secret: the credential nobody can rotate.

``tests/todo/test_no_token_leaks.py`` does this for an export token and
``tests/llm/test_no_key_leaks.py`` for a model key. The feed secret had one
assertion -- that ``FeedCredentials.__repr__`` hides it -- and nothing else: not
the error messages, not the exception chain, not the log, not the response
bodies. This file closes that.

It is the credential where a leak costs the most, for two reasons the design
records openly (``infra/calendar/credentials.py``):

* it is **not revocable per household**. It is derived from the household
  identifier and the instance key, so it never changes on its own; the only lever
  is ``CHAUDRON_CALENDAR_FEED_EPOCH``, which disconnects every phone on the
  deployment. A leaked feed secret is permanent read access to one household's
  entire inventory until every other household is disconnected too.
* it travels in **HTTP Basic**, base64 of ``feed_id:secret``. That hid it from
  every redaction pattern this repository had, which is the second half of what
  is fixed here.

The feed *identifier* is deliberately treated as public throughout: it names no
household, authorises nothing on its own, and it is the value that makes a failed
subscription diagnosable. A test asserting it survives sits at the end, because a
scrubber that blanked everything would satisfy every other test in this file.
"""

from __future__ import annotations

import base64
import logging
import uuid
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from chaudron.infra.calendar.credentials import (
    FEED_ID_LENGTH,
    FEED_SECRET_LENGTH,
    FeedHousehold,
    FeedKeyring,
)
from chaudron.infra.logging import JsonFormatter
from chaudron.infra.redaction import redact
from chaudron.services.calendar import CalendarFeedService
from tests.calendar.conftest import basic_auth, feed_id_of
from tests.conftest import TenantPair

_FORMATTER = JsonFormatter()

PROPFIND_ALL = b'<d:propfind xmlns:d="DAV:"><d:allprop/></d:propfind>'


def _calendar_path(feed_id: str) -> str:
    return f"/caldav/p/{feed_id}/cal/expiry/"


def _secret_of(keyring: FeedKeyring, household_id: uuid.UUID) -> str:
    return keyring.credentials_for(FeedHousehold(id=household_id)).secret


def _basic(feed_id: str, secret: str) -> dict[str, str]:
    raw = base64.b64encode(f"{feed_id}:{secret}".encode()).decode()
    return {"Authorization": f"Basic {raw}"}


# --------------------------------------------------------------------------- #
# The shape itself
# --------------------------------------------------------------------------- #


def test_the_secret_shape_is_recognised_by_the_scrubber() -> None:
    """No pattern matched 32 unpadded base32 characters before this change.

    Without it, the last line of defence -- the log formatter -- would write the
    secret out in full at any site that did not know it was holding one.
    """
    secret = FeedKeyring(b"k" * 32).credentials_for(FeedHousehold(id=uuid.uuid7())).secret

    assert len(secret) == FEED_SECRET_LENGTH
    assert secret not in redact(f"feed authentication failed for {secret}")


def test_the_basic_header_shape_is_recognised_by_the_scrubber() -> None:
    """How the secret actually travels: base64, where no other pattern can see it."""
    keyring = FeedKeyring(b"k" * 32)
    credentials = keyring.credentials_for(FeedHousehold(id=uuid.uuid7()))
    encoded = base64.b64encode(f"{credentials.feed_id}:{credentials.secret}".encode()).decode()

    cleaned = redact(f"upstream sent Authorization: Basic {encoded}")

    assert encoded not in cleaned
    assert "[redacted]" in cleaned


def test_the_credentials_object_does_not_print_its_secret() -> None:
    """A dataclass that prints its own secret ends up in a traceback frame."""
    credentials = FeedKeyring(b"k" * 32).credentials_for(FeedHousehold(id=uuid.uuid7()))

    for rendered in (repr(credentials), str(credentials)):
        assert credentials.secret not in rendered
        assert credentials.feed_id in rendered, "the identifier is not a secret"


# --------------------------------------------------------------------------- #
# Every way authentication fails, over HTTP
# --------------------------------------------------------------------------- #

#: Each entry is a credential that must be refused, built from the *real* secret
#: so that a handler echoing "what you sent" would be caught.
_REFUSALS = ("wrong_secret", "wrong_feed", "no_separator", "not_base64", "wrong_scheme", "absent")


def _hostile_headers(case: str, feed_id: str, secret: str) -> dict[str, str]:
    match case:
        case "wrong_secret":
            return _basic(feed_id, "A" * FEED_SECRET_LENGTH)
        case "wrong_feed":
            return _basic("B" * FEED_ID_LENGTH, secret)
        case "no_separator":
            raw = base64.b64encode(f"{feed_id}{secret}".encode()).decode()
            return {"Authorization": f"Basic {raw}"}
        case "not_base64":
            return {"Authorization": f"Basic {feed_id}:{secret}"}
        case "wrong_scheme":
            return {"Authorization": f"Bearer {secret}"}
        case _:
            return {}


@pytest.mark.parametrize("case", _REFUSALS)
async def test_a_refused_poll_never_quotes_the_credential(
    calendar_client: httpx.AsyncClient, keyring: FeedKeyring, tenant_pair: TenantPair, case: str
) -> None:
    """Including the cases where echoing the input is the natural thing to do.

    ``not_base64`` and ``wrong_scheme`` carry the secret in the clear in the
    header, which is precisely when a handler explaining what was wrong with it
    would quote it.
    """
    household = tenant_pair.household_a.id
    feed_id, secret = feed_id_of(keyring, tenant_pair.household_a), _secret_of(keyring, household)

    response = await calendar_client.request(
        "PROPFIND",
        _calendar_path(feed_id),
        headers=_hostile_headers(case, feed_id, secret),
        content=PROPFIND_ALL,
    )

    assert response.status_code == 401
    assert secret not in response.text
    assert redact(response.text) == response.text


async def test_a_successful_poll_never_returns_the_credential(
    calendar_client: httpx.AsyncClient, keyring: FeedKeyring, tenant_pair: TenantPair
) -> None:
    """The multistatus document quotes hrefs, which are built from the identifier.

    That is fine and it is the point of the split: the identifier is in the URL,
    the secret is in ``Authorization`` and nowhere else. This is what pins the
    second half of that sentence.
    """
    household = tenant_pair.household_a
    feed_id = feed_id_of(keyring, household)

    response = await calendar_client.request(
        "PROPFIND",
        _calendar_path(feed_id),
        headers={**basic_auth(keyring, household), "Depth": "1"},
        content=PROPFIND_ALL,
    )

    assert response.status_code == 207, response.text
    assert _secret_of(keyring, household.id) not in response.text
    assert feed_id in response.text, "the identifier is what the client navigates by"


# --------------------------------------------------------------------------- #
# The log, which is where a leaked credential comes to rest
# --------------------------------------------------------------------------- #


async def test_an_unhandled_failure_mid_poll_writes_no_credential_to_the_log(
    calendar_app: FastAPI,
    keyring: FeedKeyring,
    tenant_pair: TenantPair,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The realistic leak: a library that quotes the request it failed on.

    ``api/errors.py`` logs every unhandled exception with ``exc_info``, so the
    formatted traceback is what reaches disk -- and a driver or client library
    rendering the request it was given renders the ``Authorization`` header with
    it. The resolution is monkeypatched to fail that way rather than mocked
    around: what is under test is the whole path from the raise to the log line.

    Three pieces of plumbing. Two are the ones
    ``tests/todo/test_no_token_leaks.py`` documents: ``configure_logging`` clears
    the root handlers when the app is built, and Alembic's ``fileConfig`` disables
    every ``chaudron.*`` logger for the rest of the session -- both are undone
    here, or this test would assert nothing while looking like it asserted
    something. The third is ``raise_app_exceptions=False``: Starlette's
    ``ServerErrorMiddleware`` re-raises after its handler has run, so the default
    transport would hand the exception to pytest instead of the 500 the client
    would really receive.
    """
    household = tenant_pair.household_a
    feed_id = feed_id_of(keyring, household)
    secret = _secret_of(keyring, household.id)
    encoded = base64.b64encode(f"{feed_id}:{secret}".encode()).decode()

    async def _explode(self: CalendarFeedService, feed: str, given: str) -> uuid.UUID | None:
        raise RuntimeError(f"backend refused: Authorization: Basic {encoded} ({feed}:{given})")

    monkeypatch.setattr(CalendarFeedService, "resolve_household", _explode)

    records: list[logging.LogRecord] = []

    class _Collector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger("chaudron.api.errors")
    handler = _Collector(level=logging.DEBUG)
    logger.addHandler(handler)
    previous, was_disabled = logger.level, logger.disabled
    logger.setLevel(logging.DEBUG)
    logger.disabled = False
    transport = httpx.ASGITransport(app=calendar_app, raise_app_exceptions=False)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.request(
                "PROPFIND",
                _calendar_path(feed_id),
                headers=basic_auth(keyring, household),
                content=PROPFIND_ALL,
            )
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)
        logger.disabled = was_disabled
        await calendar_app.state.catalog.aclose()
        await calendar_app.state.database.dispose()

    assert response.status_code == 500
    assert secret not in response.text
    assert encoded not in response.text

    assert records, "an unhandled exception is expected to be logged"
    for record in records:
        line = _FORMATTER.format(record)
        assert secret not in line, "the feed secret reached a log line"
        assert encoded not in line, "the Basic blob reached a log line"
        assert "unhandled_exception" in line, "the line is still worth reading"


def test_no_calendar_operation_takes_the_secret_out_of_the_header(api_app: FastAPI) -> None:
    """A secret in a path or a query string lands in access logs and browser history.

    Read off the generated OpenAPI document rather than the route table, so a
    parameter introduced through a dependency is covered too.
    """
    schema = api_app.openapi()
    offenders: list[str] = []
    for path, operations in schema["paths"].items():
        if "calendar" not in path and "caldav" not in path:
            continue
        for method, operation in operations.items():
            if not isinstance(operation, dict):
                continue
            for parameter in operation.get("parameters", []):
                name = parameter["name"].lower()
                if "secret" in name or "password" in name:
                    offenders.append(f"{method.upper()} {path}: {parameter['name']}")
    assert not offenders, f"a feed secret must never be a path or query parameter: {offenders}"


def test_the_identifier_still_survives_every_scrubber() -> None:
    """The counterweight: a file this aggressive must not blank the diagnostics.

    The identifier is what an operator correlates a poll with, and what a user
    reads back to support. It is 26 base32 characters, deliberately short of the
    32 that make a run credential-shaped.
    """
    credentials = FeedKeyring(b"k" * 32).credentials_for(FeedHousehold(id=uuid.uuid7()))
    line: dict[str, Any] = {"feed_id": credentials.feed_id}

    assert len(credentials.feed_id) == FEED_ID_LENGTH
    assert redact(str(line)) == str(line)
