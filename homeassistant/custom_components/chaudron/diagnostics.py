"""What to hand somebody who is helping you debug this, and nothing else.

A diagnostics download ends up attached to a public issue. Two things therefore
never appear in it.

*The access token*, which would let the reader empty the household's fridge.
Redacted by key, and the unique identifier is redacted with it: it is a digest of
the token, which is not reversible, but it is still a value that identifies the
credential across reports.

*The contents of the pantry.* Product names, brands and shopping-list lines are
what the household eats and buys -- asset A3 of the backend's own threat model,
and potentially special-category data under GDPR article 9 for a household that
buys around an allergy. What is published instead is the *shape*: how many lots,
how many expiring, how many list lines, whether dates parsed. That is what
diagnoses an integration bug; the names diagnose nothing.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_ACCESS_TOKEN
from homeassistant.core import HomeAssistant

from .coordinator import ChaudronConfigEntry

TO_REDACT = {CONF_ACCESS_TOKEN, "unique_id"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ChaudronConfigEntry
) -> dict[str, Any]:
    """A structural report on one household's connection."""
    coordinator = entry.runtime_data
    snapshot = coordinator.data
    budget = snapshot.budget

    return {
        "entry": async_redact_data(
            {
                "data": dict(entry.data),
                "options": dict(entry.options),
                "unique_id": entry.unique_id,
            },
            TO_REDACT,
        ),
        "coordinator": {
            "last_update_success": coordinator.last_update_success,
            "update_interval_seconds": (
                None
                if coordinator.update_interval is None
                else coordinator.update_interval.total_seconds()
            ),
            "expiring_within_days": coordinator.expiring_within_days,
            # Empty until a 403 has taught us the answer, which is itself worth
            # reporting: it means every surface this integration asks for was
            # granted.
            "granted_scopes": sorted(coordinator.granted_scopes),
            "shopping_list_available": coordinator.has_shopping_list,
            "shopping_list_writable": coordinator.can_edit_shopping_list,
            "budget_available": coordinator.has_budget,
        },
        "snapshot": {
            "stock_count": snapshot.stock_count,
            "expired_count": len(snapshot.expired),
            "expiring_soon_count": len(snapshot.expiring_soon),
            "has_next_expiry": snapshot.next_expiry is not None,
            "expiring_page_truncated": snapshot.truncated,
            "shopping_item_count": (
                None if snapshot.shopping is None else len(snapshot.shopping.items)
            ),
            "shopping_checked_count": (
                None
                if snapshot.shopping is None
                else sum(1 for item in snapshot.shopping.items if item.checked)
            ),
            "budget_currencies": (
                None
                if budget is None
                else [spend.currency for spend in budget.currencies]
            ),
            "budget_receipts_missing_total": (
                None if budget is None else budget.receipts_missing_total
            ),
        },
    }
