"""A scanned barcode must not reach the application log (audit S-16).

``infra/logging.py`` stamps ``household_id`` on every record it writes. So a
``gtin`` on any line is not a product identifier, it is the correlated sentence
"this household holds this product" -- durable, timestamped, and past the reach
of an article 17 erasure once it is in journald. Redaction does not cover it and
is not supposed to: it recognises credential *shapes*, and a barcode has none.
That is the whole argument for why ``uvicorn.access`` is left unwired, where the
same barcode travels in the query string.

Only one call site in ``src/`` ever emitted one -- the "the catalogue is down, so
here is a stale entry" warning in ``services/products.py`` -- and this file is
what keeps it from coming back. The assertion is made on the **rendered JSON
line**, not on the ``extra=`` mapping, because that is what lands on disk: a
barcode reintroduced by the formatter, by a context variable or by an
``exc_info`` would be just as readable and would pass a test that inspected the
record's fields.

No database here. The service is three ports and a TTL, so it is exercised with
three doubles.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from chaudron.domain.ports import (
    CatalogRecord,
    ProductCatalogUnavailableError,
    ProductDraft,
    ProductView,
    UnitInfo,
    normalize_gtin,
)
from chaudron.infra.logging import JsonFormatter, household_id_var
from chaudron.services.products import ProductService

#: A real EAN-13 (Nutella), because the padding to GTIN-14 that
#: :func:`normalize_gtin` applies is exactly the kind of transformation a naive
#: substring assertion would miss. Both forms are searched for below.
SCANNED = "3017620422003"

HOUSEHOLD = uuid.UUID("11111111-1111-4111-8111-111111111111")


class StubRepository:
    """The three states :meth:`find_cached` distinguishes, fixed at construction."""

    def __init__(self, view: ProductView | None, synced_at: datetime | None) -> None:
        self._cached: tuple[ProductView | None, datetime | None] | None = (view, synced_at)

    async def find_cached(self, gtin: str) -> tuple[ProductView | None, datetime | None] | None:
        return self._cached

    async def get_visible(
        self, household_id: uuid.UUID, product_id: uuid.UUID
    ) -> ProductView | None:  # pragma: no cover - not on the path under test
        raise NotImplementedError

    async def create_private(
        self, household_id: uuid.UUID, draft: ProductDraft
    ) -> ProductView:  # pragma: no cover - not on the path under test
        raise NotImplementedError

    async def upsert_public(
        self, record: CatalogRecord
    ) -> ProductView:  # pragma: no cover - not on the path under test
        raise NotImplementedError

    async def remember_absent(self, gtin: str) -> None:  # pragma: no cover - not on the path
        raise NotImplementedError


class UnreachableCatalog:
    """Open Food Facts, down. The only catalogue state this file cares about."""

    async def lookup(self, gtin: str) -> CatalogRecord | None:
        raise ProductCatalogUnavailableError("Open Food Facts is unreachable", retry_after=60)


class StubUnits:
    async def get(self, code: str) -> UnitInfo | None:  # pragma: no cover - not on the path
        raise NotImplementedError


def _service(*, synced_at: datetime | None, view: ProductView | None) -> ProductService:
    return ProductService(
        StubRepository(view, synced_at),
        StubUnits(),
        UnreachableCatalog(),
        cache_ttl_seconds=3600,
    )


def _cached_view() -> ProductView:
    return ProductView(
        id=uuid.UUID("22222222-2222-4222-8222-222222222222"),
        name="Pate a tartiner",
        brand="Nutella",
        gtin=normalize_gtin(SCANNED),
        image_url=None,
    )


def _rendered(records: list[logging.LogRecord]) -> str:
    """Every captured record as the JSON the process would actually write."""
    formatter = JsonFormatter()
    return "\n".join(formatter.format(record) for record in records)


@pytest.fixture
def correlated_household() -> object:
    """Put a ``household_id`` on the records, which is what makes S-16 a finding.

    A barcode alone is a number off a package. A barcode next to a household
    identifier is that household's shopping, and the formatter attaches the
    second one whether or not the call site asked for it.
    """
    token = household_id_var.set(str(HOUSEHOLD))
    yield None
    household_id_var.reset(token)


@pytest.mark.asyncio
@pytest.mark.usefixtures("correlated_household")
async def test_the_stale_catalogue_warning_carries_no_barcode(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The regression itself: serving a stale entry must not log which one."""
    stale = datetime.now(UTC) - timedelta(days=9)
    service = _service(synced_at=stale, view=_cached_view())

    with caplog.at_level(logging.DEBUG, logger="chaudron"):
        served = await service.lookup_by_barcode(SCANNED)

    assert served.name == "Pate a tartiner"
    assert caplog.records, "the stale-serving path must still say something to an operator"

    line = _rendered(caplog.records)
    assert "catalog_unavailable_serving_stale" in line
    # Both spellings: what the scanner produced, and the GTIN-14 storage form the
    # service normalises it to. The finding was the second one.
    assert SCANNED not in line
    assert normalize_gtin(SCANNED) not in line
    # The correlation that makes either of them matter is present, so the
    # assertions above are not passing because the line is empty.
    assert str(HOUSEHOLD) in line


@pytest.mark.asyncio
async def test_the_stale_catalogue_warning_still_says_how_stale(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Dropping the barcode must not turn the line into an event with no content.

    The age is what an operator reads this warning for -- "we are answering from
    a cache nine days old" -- and it names nobody, so it is the field that
    replaces the one that had to go.
    """
    stale = datetime.now(UTC) - timedelta(days=9)
    service = _service(synced_at=stale, view=_cached_view())

    with caplog.at_level(logging.DEBUG, logger="chaudron"):
        await service.lookup_by_barcode(SCANNED)

    (record,) = [r for r in caplog.records if r.message == "catalog_unavailable_serving_stale"]
    age = getattr(record, "cached_age_seconds", None)
    assert isinstance(age, int)
    assert timedelta(days=8).total_seconds() < age < timedelta(days=10).total_seconds()


@pytest.mark.asyncio
async def test_an_unreachable_catalogue_with_nothing_cached_logs_nothing_and_raises(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The other branch, which has no stale entry to serve and no line to write.

    Asserted because it is the branch a future edit is most likely to "fix" by
    adding a warning -- and the barcode is right there in scope.
    """
    service = _service(synced_at=None, view=None)

    with (
        caplog.at_level(logging.DEBUG, logger="chaudron"),
        pytest.raises(ProductCatalogUnavailableError),
    ):
        await service.lookup_by_barcode(SCANNED)

    line = _rendered(caplog.records)
    assert SCANNED not in line
    assert normalize_gtin(SCANNED) not in line
