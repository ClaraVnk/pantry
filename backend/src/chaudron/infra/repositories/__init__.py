"""SQLAlchemy adapters for the repository ports declared in ``domain/ports.py``.

Every class here takes an :class:`~sqlalchemy.ext.asyncio.AsyncSession` and
never commits: the transaction is owned by the request (``infra/db.py``).
"""

from chaudron.infra.repositories.declined_repurchase import SqlDeclinedRepurchaseStore
from chaudron.infra.repositories.households import SqlHouseholdRepository
from chaudron.infra.repositories.inventory import SqlInventoryRepository
from chaudron.infra.repositories.locations import SqlLocationRepository
from chaudron.infra.repositories.products import SqlProductRepository
from chaudron.infra.repositories.shopping_export_target import SqlShoppingExportTargetStore
from chaudron.infra.repositories.units import SqlUnitRegistry

__all__ = [
    "SqlDeclinedRepurchaseStore",
    "SqlHouseholdRepository",
    "SqlInventoryRepository",
    "SqlLocationRepository",
    "SqlProductRepository",
    "SqlShoppingExportTargetStore",
    "SqlUnitRegistry",
]
