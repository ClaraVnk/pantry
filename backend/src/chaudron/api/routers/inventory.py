"""Inventory: listing, adding, correcting and removing stock.

**``DELETE`` answers ``200``, not the ``204`` contract v1 froze.** Contract v1.1
section 6bis requires the body of this very call to carry ``depleted``, and a
``204 No Content`` has no body to carry it in; one of the two had to give. The
deviation is recorded in ``docs/api-contract-v1.md`` at the ``DELETE`` and argued
in section 6bis of ``docs/api-contract-v1.1-dietary.md``, so a reader of either
document finds it without reading this module.

**The proposal never endangers the mutation.** Removing a lot is what the user
asked for; offering to buy the product again is a courtesy computed afterwards.
It therefore runs inside a savepoint and behind a ``try`` -- if the proposal
fails, the removal stands and ``depleted`` is ``null``. The reverse (a courtesy
that rolls back a deletion the user watched succeed) is not a trade anyone would
accept.
"""

from __future__ import annotations

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from chaudron.api.deps import (
    DepletionServiceDep,
    InventoryReadDep,
    InventoryServiceDep,
    InventoryWriteDep,
    SessionDep,
)
from chaudron.api.schemas import (
    DepletedOut,
    InventoryCreateIn,
    InventoryItemMutatedOut,
    InventoryItemOut,
    InventoryPageOut,
    InventoryPatchIn,
    InventoryRemovalOut,
    LocationRefOut,
    ProductOut,
    QuantityOut,
    RemovalReason,
)
from chaudron.domain.ports import (
    UNSET,
    InventoryFilter,
    InventoryItem,
    Maybe,
    ProductDraft,
    StockEntrySource,
    UnsetType,
    display_gtin,
)
from chaudron.domain.shopping import DepletionEvent
from chaudron.services.inventory import AddItemCommand, DepletionSignal, UpdateItemCommand
from chaudron.services.shopping_import import DepletionService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/inventory", tags=["inventory"])

#: A page big enough to be useful, small enough that no single request can pin a
#: connection for a second. The contract's default is 50.
MAX_PAGE_SIZE = 200


def _to_out(item: InventoryItem) -> InventoryItemOut:
    return InventoryItemOut(
        id=item.id,
        product=ProductOut(
            id=item.product.id,
            name=item.product.name,
            brand=item.product.brand,
            gtin=None if item.product.gtin is None else display_gtin(item.product.gtin),
            image_url=item.product.image_url,
        ),
        location=(
            None
            if item.location is None
            else LocationRefOut(
                id=item.location.id, name=item.location.name, kind=item.location.kind
            )
        ),
        quantity=QuantityOut(amount=item.quantity_value, unit=item.quantity_unit_code),
        expires_on=item.best_before,
        expiry_kind=item.date_kind,
        opened_at=item.opened_at,
        effective_expires_on=item.effective_expiry,
        source=item.entry_source,
        created_at=item.created_at,
    )


@router.get("", response_model=InventoryPageOut, summary="List the current stock")
async def list_inventory(
    household_id: InventoryReadDep,
    service: InventoryServiceDep,
    location_id: uuid.UUID | None = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
    expiring_within_days: Annotated[int | None, Query(ge=0, le=3650)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> InventoryPageOut:
    page = await service.list_items(
        household_id,
        InventoryFilter(
            location_id=location_id,
            query=q,
            expiring_within_days=expiring_within_days,
            limit=limit,
            offset=offset,
        ),
    )
    return InventoryPageOut(total=page.total, items=[_to_out(item) for item in page.items])


@router.post(
    "",
    response_model=InventoryItemOut,
    status_code=status.HTTP_201_CREATED,
    summary="Add stock",
)
async def add_inventory_item(
    household_id: InventoryWriteDep, service: InventoryServiceDep, payload: InventoryCreateIn
) -> InventoryItemOut:
    item = await service.add_item(
        household_id,
        AddItemCommand(
            amount=payload.amount,
            unit=payload.unit,
            product_id=payload.product_id,
            new_product=(
                None
                if payload.product is None
                else ProductDraft(
                    name=payload.product.name,
                    brand=payload.product.brand,
                    gtin=payload.product.gtin,
                    default_unit_code=payload.product.default_unit,
                )
            ),
            location_id=payload.location_id,
            expires_on=payload.expires_on,
            expiry_kind=payload.expiry_kind,
            opened_at=payload.opened_at,
            source=StockEntrySource(payload.source),
        ),
    )
    return _to_out(item)


@router.patch("/{item_id}", response_model=InventoryItemMutatedOut, summary="Correct a stock item")
async def patch_inventory_item(
    household_id: InventoryWriteDep,
    service: InventoryServiceDep,
    depletion_service: DepletionServiceDep,
    session: SessionDep,
    item_id: uuid.UUID,
    payload: InventoryPatchIn,
) -> InventoryItemMutatedOut:
    provided = payload.model_fields_set

    def maybe[T](field: str, value: T) -> Maybe[T]:
        """``UNSET`` unless the client actually sent the field.

        Without this, ``PATCH {"amount": 2}`` would read as "and clear the expiry
        date", because every unsent optional field defaults to ``None``.
        """
        return value if field in provided else UNSET

    updated = await service.update_item(
        household_id,
        item_id,
        UpdateItemCommand(
            amount=_required(maybe("amount", payload.amount)),
            unit=_required(maybe("unit", payload.unit)),
            location_id=maybe("location_id", payload.location_id),
            expires_on=maybe("expires_on", payload.expires_on),
            expiry_kind=_required(maybe("expiry_kind", payload.expiry_kind)),
            opened_at=maybe("opened_at", payload.opened_at),
        ),
    )
    return InventoryItemMutatedOut(
        **_to_out(updated.item).model_dump(),
        depleted=await _propose(session, depletion_service, household_id, updated.depletion),
    )


@router.delete(
    "/{item_id}",
    response_model=InventoryRemovalOut,
    summary="Remove a stock item",
)
async def delete_inventory_item(
    household_id: InventoryWriteDep,
    service: InventoryServiceDep,
    depletion_service: DepletionServiceDep,
    session: SessionDep,
    item_id: uuid.UUID,
    reason: RemovalReason = "consumed",
) -> InventoryRemovalOut:
    """Deplete a lot, and say what the household may want to buy again.

    ``200`` with a body, where contract v1 wrote ``204``. See the module
    docstring: ``depleted`` and "no content" are mutually exclusive.
    """
    signal = await service.remove_item(household_id, item_id, reason)
    return InventoryRemovalOut(
        depleted=await _propose(session, depletion_service, household_id, signal)
    )


async def _propose(
    session: AsyncSession,
    service: DepletionService,
    household_id: uuid.UUID,
    signal: DepletionSignal | None,
) -> DepletedOut | None:
    """The repurchase proposal for what just ran out, or ``None``.

    Three properties, in order of how much they matter.

    *It cannot undo the mutation.* The lot has been depleted or corrected and the
    ledger written; a proposal is a suggestion computed afterwards. Any failure
    is swallowed into ``None``, and the ``SAVEPOINT`` is what makes swallowing
    honest: a database error inside a transaction poisons it, so a bare ``except``
    would leave the request to die at ``COMMIT`` and roll back the very deletion
    the user watched succeed.

    *It does not decide.* ``signal.reason`` is handed over verbatim, ``correction``
    included, and :class:`DepletionService` applies contract 6bis' rule. A second
    filter here would be a second place for the rule to be wrong.

    *It costs nothing when there is nothing.* No signal, no savepoint, no query --
    which is every ``PATCH`` and every removal of an already-empty lot.
    """
    if signal is None:
        return None
    try:
        async with session.begin_nested():
            proposals = await service.propose(
                household_id, [DepletionEvent(product_id=signal.product_id, reason=signal.reason)]
            )
    except Exception:
        # Never re-raised: the mutation is done and correct, and a household that
        # asked to throw away the yoghurt does not care that the shopping-list
        # hint failed.
        logger.warning("depletion_proposal_failed", exc_info=True)
        return None

    # One lot, one product: the service groups by construction, so this sequence
    # holds at most one proposal -- and holds none when the reason was a
    # correction, which is where "a correction never proposes" actually happens.
    if not proposals:
        return None
    proposal = proposals[0]
    return DepletedOut(
        product_id=proposal.product_id,
        product_name=proposal.product_name,
        reason=proposal.reason,
        already_on_list=proposal.already_on_list,
        previously_declined=proposal.previously_declined,
    )


def _required[T](value: Maybe[T | None]) -> Maybe[T]:
    """Drop an explicit ``null`` on a field that cannot be cleared.

    ``amount``, ``unit`` and ``expiry_kind`` are always present on a lot; sending
    ``null`` for them is meaningless, and treating it as "leave alone" is kinder
    than a validation error nobody can act on.
    """
    if isinstance(value, UnsetType) or value is None:
        return UNSET
    return value
