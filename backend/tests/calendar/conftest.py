"""Fixtures for the calendar feed: an application with the feed switched on.

The feed is off by default (``CHAUDRON_CALENDAR_FEED_ENABLED``), so these tests
build their own settings rather than reusing ``api_app``. Everything else is the
shared harness: the real application, one dependency overridden, every write
rolled back with the test.
"""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Protocol

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from chaudron.api.deps import CSRF_HEADER, SESSION_COOKIE, get_session
from chaudron.api.main import create_app
from chaudron.config import Settings
from chaudron.domain.models import (
    ExpiryDateKind,
    Household,
    InventoryLot,
    Product,
    ProductSource,
    QuantityDimension,
    StockEntrySource,
    StorageKind,
    StorageLocation,
)
from chaudron.infra.calendar.credentials import FeedHousehold, FeedKeyring
from tests.conftest import SignedIn, build_test_settings, isolate_rate_limit_buckets

BASE_URL = "https://chaudron.test"


def calendar_settings(database_url: str, *, enabled: bool = True) -> Settings:
    return build_test_settings(database_url).model_copy(
        update={"calendar_feed_enabled": enabled, "base_url": BASE_URL}
    )


@pytest.fixture
def calendar_app(initialised_database: str, db_session: AsyncSession) -> Iterator[FastAPI]:
    app = create_app(calendar_settings(initialised_database))
    # The feed's failure limiter is keyed on the source address, which is the same
    # for every test in the suite, and one test here deliberately spends the whole
    # hourly budget. Its buckets have to be this application's alone.
    isolate_rate_limit_buckets(app)

    async def _use_test_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_session] = _use_test_session
    yield app
    app.dependency_overrides.clear()


@pytest.fixture
async def calendar_client(
    calendar_app: FastAPI, signed_in: SignedIn
) -> AsyncIterator[httpx.AsyncClient]:
    """The feed application, with a session attached.

    The CalDAV tree authenticates with HTTP Basic and ignores the cookie
    entirely -- a phone cannot carry one. The session is here for the one route
    on this application that is part of the ordinary API,
    ``GET /v1/calendar/subscription``, which is owner-only since authentication
    landed (``routers/calendar.py``).

    ``https://`` because the session cookie is ``Secure`` and ``__Host-``-prefixed;
    see the note on ``api_client`` in ``tests/conftest.py``.
    """
    transport = httpx.ASGITransport(app=calendar_app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://testserver",
        cookies={SESSION_COOKIE: signed_in.token},
        headers={CSRF_HEADER: signed_in.csrf_token},
    ) as client:
        yield client
    await calendar_app.state.catalog.aclose()
    await calendar_app.state.database.dispose()


@pytest.fixture
async def anonymous_calendar_client(calendar_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """The same application with no session: what a CalDAV client is."""
    transport = httpx.ASGITransport(app=calendar_app)
    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as client:
        yield client
    await calendar_app.state.catalog.aclose()
    await calendar_app.state.database.dispose()


@pytest.fixture
def keyring(calendar_app: FastAPI) -> FeedKeyring:
    """The very keyring the application under test derives its credentials from."""
    settings: Settings = calendar_app.state.settings
    return FeedKeyring.from_settings(settings)


def feed_household(household: Household) -> FeedHousehold:
    """The household as the derivation sees it: identifier plus revocation counter.

    The counter is read off the row rather than assumed to be its default, so a
    test that revokes and re-derives gets the credential the server would issue
    and not the one it just withdrew.
    """
    return FeedHousehold(id=household.id, feed_epoch=household.calendar_feed_epoch)


def basic_auth(keyring: FeedKeyring, household: Household) -> dict[str, str]:
    """The ``Authorization`` header a subscribed client would send."""
    credentials = keyring.credentials_for(feed_household(household))
    return basic_header(credentials.feed_id, credentials.secret)


def basic_header(feed_id: str, secret: str) -> dict[str, str]:
    raw = f"{feed_id}:{secret}".encode()
    return {"Authorization": f"Basic {base64.b64encode(raw).decode()}"}


def feed_id_of(keyring: FeedKeyring, household: Household) -> str:
    return keyring.credentials_for(feed_household(household)).feed_id


class MakeFridge(Protocol):
    async def __call__(self, household: Household, *, name: str = ...) -> StorageLocation: ...


class MakeLot(Protocol):
    async def __call__(
        self,
        household: Household,
        *,
        best_before: date | None,
        name: str = ...,
        quantity: str = ...,
        unit: str = ...,
        dimension: QuantityDimension = ...,
        location: StorageLocation | None = ...,
        depleted: bool = ...,
    ) -> InventoryLot: ...


@pytest.fixture
def make_fridge(db_session: AsyncSession) -> MakeFridge:
    async def _make_fridge(household: Household, *, name: str = "Frigo") -> StorageLocation:
        location = StorageLocation(household_id=household.id, name=name, kind=StorageKind.FRIDGE)
        db_session.add(location)
        await db_session.flush()
        return location

    return _make_fridge


@pytest.fixture
def make_lot(db_session: AsyncSession) -> MakeLot:
    """One inventory lot, with the product row it needs created alongside."""

    async def _make_lot(
        household: Household,
        *,
        best_before: date | None,
        name: str = "Lait demi-écrémé",
        quantity: str = "1",
        unit: str = "l",
        dimension: QuantityDimension = QuantityDimension.VOLUME,
        location: StorageLocation | None = None,
        depleted: bool = False,
    ) -> InventoryLot:
        product = Product(household_id=None, name=name, source=ProductSource.MANUAL)
        db_session.add(product)
        await db_session.flush()

        lot = InventoryLot(
            household_id=household.id,
            product_id=product.id,
            storage_location_id=None if location is None else location.id,
            quantity_value=Decimal(quantity),
            quantity_unit_code=unit,
            quantity_dimension=dimension,
            quantity_canonical=Decimal(quantity),
            initial_quantity_canonical=Decimal(quantity),
            best_before=best_before,
            date_kind=(
                ExpiryDateKind.UNKNOWN if best_before is None else ExpiryDateKind.BEST_BEFORE
            ),
            entry_source=StockEntrySource.MANUAL,
        )
        if depleted:
            lot.quantity_canonical = Decimal(0)
            lot.depleted_at = datetime.now(UTC)
        db_session.add(lot)
        await db_session.flush()
        return lot

    return _make_lot
