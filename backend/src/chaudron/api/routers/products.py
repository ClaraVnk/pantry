"""Barcode resolution and manual product creation."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from chaudron.api.deps import (
    HouseholdDep,
    MemberDep,
    ProductServiceDep,
    enforce_product_lookup_limit,
)
from chaudron.api.schemas import ProductCreateIn, ProductDetailOut
from chaudron.domain.ports import GTIN_STORAGE_LENGTH, ProductDraft, ProductView, display_gtin

router = APIRouter(prefix="/v1/products", tags=["products"])


def _to_out(product: ProductView) -> ProductDetailOut:
    return ProductDetailOut(
        id=product.id,
        name=product.name,
        brand=product.brand,
        gtin=None if product.gtin is None else display_gtin(product.gtin),
        image_url=product.image_url,
        default_unit=product.default_unit_code,
        category_tag=product.category_tag,
    )


@router.get(
    "/lookup",
    response_model=ProductDetailOut,
    summary="Resolve a barcode",
    dependencies=[Depends(enforce_product_lookup_limit)],
)
async def lookup_product(
    household_id: HouseholdDep,
    service: ProductServiceDep,
    gtin: Annotated[str, Query(min_length=8, max_length=GTIN_STORAGE_LENGTH)],
) -> ProductDetailOut:
    """Resolve a barcode from the shared cache, then from Open Food Facts.

    ``household_id`` is required even though the answer is not tenant-scoped:
    the shared catalogue is a resource of this instance, and letting unauthenticated
    callers drive its outbound requests would hand a stranger the instance's whole
    rate-limit budget (ADR-0008). It is also the key the per-household inbound
    limit counts on -- ``429`` with ``Retry-After`` past it, so that the outbound
    budget stops being the first thing to saturate.
    """
    return _to_out(await service.lookup_by_barcode(gtin))


@router.post(
    "",
    response_model=ProductDetailOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a product manually",
)
async def create_product(
    household_id: MemberDep, service: ProductServiceDep, payload: ProductCreateIn
) -> ProductDetailOut:
    product = await service.create_manual(
        household_id,
        ProductDraft(
            name=payload.name,
            brand=payload.brand,
            gtin=payload.gtin,
            default_unit_code=payload.default_unit,
        ),
    )
    return _to_out(product)
