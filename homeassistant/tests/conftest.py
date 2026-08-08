"""Fixtures shared by the integration's tests.

The instance is always a mock: these tests prove that this integration reads the
contract correctly, not that a Chaudron server implements it. The backend has its
own suite for that, and one that stood up a real API here would be slower, less
deterministic, and would still not catch the failure this suite is for -- a
response shape this client misreads.
"""

from __future__ import annotations

from collections.abc import Generator
from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.chaudron.const import DOMAIN

BASE_URL = "https://chaudron.example.org"
#: Shaped like a machine token, and is not one: the fixture instance below
#: accepts it because it is the only value it is told about.
TOKEN = "chdr_" + "a" * 43

INVENTORY_URL = f"{BASE_URL}/v1/inventory"
LOCATIONS_URL = f"{BASE_URL}/v1/locations"
SHOPPING_URL = f"{BASE_URL}/v1/shopping-lists/current"
SHOPPING_ITEMS_URL = f"{BASE_URL}/v1/shopping-lists/current/items"
BUDGET_URL = f"{BASE_URL}/v1/budget"

#: One lot already over its date, one due in two days, one due in a fortnight.
#: Sorted the way the backend sorts: effective expiry ascending.
EXPIRING_ITEMS: list[dict[str, Any]] = [
    {
        "id": "11111111-1111-4111-8111-111111111111",
        "product": {
            "name": "Yaourt nature",
            "brand": "Malo",
            "gtin": None,
            "image_url": None,
        },
        "location": {"id": "aaaa", "name": "Frigo", "kind": "fridge"},
        "quantity": {"amount": "4.000", "unit": "pc"},
        "expires_on": "2026-01-01",
        "expiry_kind": "use_by",
        "opened_at": None,
        "effective_expires_on": "2026-01-01",
        "source": "manual",
        "created_at": "2025-12-20T10:00:00Z",
    },
    {
        "id": "22222222-2222-4222-8222-222222222222",
        "product": {
            "name": "Crème fraîche",
            "brand": None,
            "gtin": None,
            "image_url": None,
        },
        "location": None,
        "quantity": {"amount": "1.000", "unit": "pot"},
        "expires_on": "2026-08-08",
        "expiry_kind": "best_before",
        "opened_at": None,
        "effective_expires_on": "2026-08-08",
        "source": "manual",
        "created_at": "2026-08-01T10:00:00Z",
    },
]

SHOPPING_PAYLOAD: dict[str, Any] = {
    "id": "33333333-3333-4333-8333-333333333333",
    "name": "Courses",
    "items": [
        {
            "id": "44444444-4444-4444-8444-444444444444",
            "product_id": None,
            "product_name": None,
            "free_text": "Farine",
            "quantity": {"amount": "1.000", "unit": "kg"},
            "source": "manual",
            "checked": False,
            "sort_order": 1,
        },
        {
            "id": "55555555-5555-4555-8555-555555555555",
            "product_id": "66666666-6666-4666-8666-666666666666",
            "product_name": "Lait demi-écrémé",
            "free_text": None,
            "quantity": None,
            "source": "depleted",
            "checked": True,
            "sort_order": 2,
        },
    ],
}

BUDGET_PAYLOAD: dict[str, Any] = {
    "period": "month",
    "period_start": "2026-08-01",
    "period_end": "2026-08-31",
    "currencies": [
        {
            "currency": "EUR",
            "spent": "184.20",
            "receipt_count": 3,
            "line_sum_mismatch_count": 0,
            "target": "400.00",
        }
    ],
    "coverage": {
        "receipts_with_total": 3,
        "receipts_missing_total": 1,
        "stock_items_added_without_receipt": 12,
    },
}

INSUFFICIENT_SCOPE = {
    "type": "https://chaudron.dev/problems/insufficient-scope",
    "title": "Insufficient token scope",
    "status": 403,
    "required_scopes": ["shopping:read"],
    "granted_scopes": ["inventory:read"],
}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    enable_custom_integrations: None,
) -> Generator[None]:
    """Let Home Assistant load ``custom_components/chaudron`` at all."""
    yield


@pytest.fixture
def config_entry() -> MockConfigEntry:
    """An entry for one household on one instance."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="chaudron.example.org",
        unique_id="0123456789abcdef0123456789abcdef",
        data={"url": BASE_URL, "access_token": TOKEN},
        options={"expiring_within_days": 3, "scan_interval_minutes": 10},
    )


def mock_instance(
    aioclient_mock: AiohttpClientMocker,
    *,
    shopping: bool = True,
    budget: bool = True,
) -> None:
    """Register a whole, well-behaved instance.

    ``shopping`` and ``budget`` off simulate the token that was issued without
    those scopes, which is the configuration this integration has to degrade into
    rather than fail on.
    """
    aioclient_mock.get(LOCATIONS_URL, json=[])
    aioclient_mock.get(
        INVENTORY_URL, params={"limit": "1"}, json={"total": 42, "items": []}
    )
    aioclient_mock.get(
        INVENTORY_URL,
        params={"expiring_within_days": "3"},
        json={"total": len(EXPIRING_ITEMS), "items": EXPIRING_ITEMS},
    )
    if shopping:
        aioclient_mock.get(SHOPPING_URL, json=SHOPPING_PAYLOAD)
    else:
        aioclient_mock.get(SHOPPING_URL, status=403, json=INSUFFICIENT_SCOPE)
    if budget:
        aioclient_mock.get(BUDGET_URL, json=BUDGET_PAYLOAD)
    else:
        aioclient_mock.get(
            BUDGET_URL,
            status=403,
            json={**INSUFFICIENT_SCOPE, "required_scopes": ["budget:read"]},
        )
