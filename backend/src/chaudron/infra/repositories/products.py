"""Product catalogue persistence: the shared public entries and the private ones.

The public rows (``household_id IS NULL``) are the barcode cache mandated by
ADR-0008. Scoping them per household would multiply the outbound calls by the
number of tenants for byte-identical content, which the Open Food Facts rate
limit forbids.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Final

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from chaudron.domain.models import Product, ProductSource
from chaudron.domain.ports import CatalogRecord, ProductDraft, ProductView

#: Marks a public row that exists only to remember an *absence*. A dedicated
#: column would be cleaner; a key in the payload we already store keeps the
#: negative cache from costing a schema change for a transient optimisation.
NEGATIVE_CACHE_KEY: Final = "chaudron_absent"


def _to_view(product: Product) -> ProductView:
    return ProductView(
        id=product.id,
        name=product.name,
        brand=product.brand,
        gtin=product.gtin,
        image_url=product.image_url,
        default_unit_code=product.default_unit_code,
        category_tag=product.category_tag,
        is_public=product.household_id is None,
    )


def _is_negative_cache(product: Product) -> bool:
    payload = product.off_payload
    return payload is not None and bool(payload.get(NEGATIVE_CACHE_KEY))


class SqlProductRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_visible(
        self, household_id: uuid.UUID, product_id: uuid.UUID
    ) -> ProductView | None:
        """The household's own product, or a public one. Never another tenant's.

        The ``household_id`` predicate is the whole isolation guarantee for this
        table: ``product`` is the one place the schema cannot express it as a
        composite foreign key, because its tenant column is nullable
        (``docs/data-model.md`` section 5.2).
        """
        product = await self._session.scalar(
            select(Product).where(
                Product.id == product_id,
                Product.archived_at.is_(None),
                # Not `in_([household_id, None])`: SQL never matches NULL with
                # IN, so the public catalogue would silently become invisible.
                or_(Product.household_id == household_id, Product.household_id.is_(None)),
            )
        )
        return None if product is None else _to_view(product)

    async def create_private(self, household_id: uuid.UUID, draft: ProductDraft) -> ProductView:
        product = Product(
            household_id=household_id,
            name=draft.name,
            brand=draft.brand,
            gtin=draft.gtin,
            default_unit_code=draft.default_unit_code,
            source=draft.source,
        )
        self._session.add(product)
        await self._session.flush()
        return _to_view(product)

    async def find_cached(self, gtin: str) -> tuple[ProductView | None, datetime | None] | None:
        product = await self._session.scalar(
            select(Product).where(Product.household_id.is_(None), Product.gtin == gtin)
        )
        if product is None:
            return None
        if _is_negative_cache(product):
            return None, product.off_synced_at
        return _to_view(product), product.off_synced_at

    async def upsert_public(self, record: CatalogRecord) -> ProductView:
        """Write a freshly fetched catalogue entry into the shared cache.

        A public entry whose source is ``manual`` was corrected by a human here
        and wins over any later refresh (ADR-0008): only its freshness marker is
        bumped, so the correction is not silently undone on the next scan.
        """
        product = await self._session.scalar(
            select(Product).where(Product.household_id.is_(None), Product.gtin == record.gtin)
        )
        now = datetime.now(UTC)

        if product is not None and product.source is ProductSource.MANUAL:
            product.off_synced_at = now
            await self._session.flush()
            return _to_view(product)

        payload: dict[str, Any] | None = record.payload
        if product is None:
            product = Product(household_id=None, gtin=record.gtin, name=record.name)
            self._session.add(product)

        product.name = record.name
        product.brand = record.brand
        product.image_url = record.image_url
        product.category_tag = record.category_tag
        product.net_content_value = record.net_content_value
        product.net_content_unit_code = record.net_content_unit_code
        # Overwritten wholesale on every refresh, including back to `unknown`.
        # A record that has *lost* its allergen fields upstream -- a contributor
        # blanking a page is a thing that happens on a wiki -- must stop
        # supporting the claim it used to support, rather than keeping a stale
        # declaration that now describes nothing.
        product.allergen_state = record.allergen_state
        product.allergens_contains = list(record.allergens_contains)
        product.allergens_may_contain = list(record.allergens_may_contain)
        product.pnns_markers = list(record.pnns_markers)
        product.food_family = record.food_family
        product.source = ProductSource.OPEN_FOOD_FACTS
        product.off_payload = payload
        product.off_synced_at = now
        # Clears a previous negative-cache entry for the same code: the product
        # exists now, and leaving it archived would hide it from every lookup.
        product.archived_at = None

        await self._session.flush()
        return _to_view(product)

    async def remember_absent(self, gtin: str) -> None:
        """Cache the absence, so a code nobody knows is not asked for on every scan.

        Stored archived: the row must never surface in a search or a listing --
        it is not a product, it is the memory of a question already answered.
        """
        product = await self._session.scalar(
            select(Product).where(Product.household_id.is_(None), Product.gtin == gtin)
        )
        now = datetime.now(UTC)
        if product is None:
            product = Product(
                household_id=None,
                gtin=gtin,
                name=gtin,
                source=ProductSource.OPEN_FOOD_FACTS,
            )
            self._session.add(product)
        elif not _is_negative_cache(product):
            # A known product that has since disappeared upstream keeps its data;
            # overwriting it with a tombstone would lose a usable entry.
            return

        product.off_payload = {NEGATIVE_CACHE_KEY: True}
        product.off_synced_at = now
        product.archived_at = now
        await self._session.flush()
