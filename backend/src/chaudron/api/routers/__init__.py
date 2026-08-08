"""HTTP routers. One module per resource of the v1 contract."""

from chaudron.api.routers.balance import router as balance_router
from chaudron.api.routers.budget import router as budget_router
from chaudron.api.routers.export_targets import router as export_targets_router
from chaudron.api.routers.health import router as health_router
from chaudron.api.routers.inventory import router as inventory_router
from chaudron.api.routers.locations import router as locations_router
from chaudron.api.routers.members import router as members_router
from chaudron.api.routers.privacy import router as privacy_router
from chaudron.api.routers.products import router as products_router
from chaudron.api.routers.providers import router as providers_router
from chaudron.api.routers.receipts import router as receipts_router
from chaudron.api.routers.recipes import router as recipes_router
from chaudron.api.routers.shopping import router as shopping_router
from chaudron.api.routers.tokens import router as tokens_router

__all__ = [
    "balance_router",
    "budget_router",
    "export_targets_router",
    "health_router",
    "inventory_router",
    "locations_router",
    "members_router",
    "privacy_router",
    "products_router",
    "providers_router",
    "receipts_router",
    "recipes_router",
    "shopping_router",
    "tokens_router",
]
