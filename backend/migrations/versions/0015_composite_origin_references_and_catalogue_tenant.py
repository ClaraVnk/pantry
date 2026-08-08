"""make the three origin references composite, and pin the catalogue's tenant

Revision ID: 0015
Revises: 0014
Create Date: 2026-08-04 00:00:00.000000+00:00

Two defence-in-depth gaps, neither reachable over HTTP today, and both of the
kind whose only guard is a property of the current service layer.

Part one: three references that could name another household's row
------------------------------------------------------------------

``inventory_lot.source_receipt_line_id``,
``shopping_list_item.origin_recipe_suggestion_id`` and
``stock_movement.recipe_suggestion_id`` were single-column foreign keys onto
tenant-scoped parents. Every other such reference in this schema is composite on
``(household_id, parent_id)`` -- ``docs/data-model.md`` section 5.2 calls it layer
two -- and these three were exempted in ``tests/tenancy/test_schema_tenant_guard.py``
with the note "origin trace, not an access path".

The exemption's reasoning was sound and its conclusion was not. It observed that
nothing writes these columns from a request, that the join back is household-scoped
so a foreign row renders as ``NULL``, and that all three are ``ON DELETE SET NULL``.
Each clause is true. None is a property of the *schema*: they describe what the
service layer happens to do, offered as an argument about what the database
permits. And the database permitted it -- PostgreSQL evaluates referential
integrity in triggers that run as the table owner and are **not** subject to
row-level security, so a policy that hides household B's receipt line from
household A does not stop household A's lot from pointing at it. A previous review
recorded that the composite keys already covered this case; they did not.

``ON DELETE SET NULL (column)`` -- PostgreSQL 15 and later -- is what makes the
composite form possible here at all. A bare ``SET NULL`` on a composite key nulls
*every* column of it, and ``household_id`` is ``NOT NULL`` on all three tables, so
deleting a receipt would have failed with a not-null violation instead of
forgetting where a lot came from.

Expand, migrate, contract, in that order and in one revision because the columns
themselves do not change:

* **expand** -- add ``uq_receipt_line_household_id``, which the new reference from
  ``inventory_lot`` needs as a target (``recipe_suggestion`` already carries its
  equivalent), and add the three composite constraints;
* **migrate** -- before adding them, null out any row that already points across a
  household boundary. There should be none, for exactly the reasons the old
  exemption gave; a repair that silently does nothing is the outcome to hope for,
  and the alternative is a migration that aborts on data the application itself
  could produce only through a bug. ``ON DELETE SET NULL`` is the semantics these
  columns already have, so nulling one loses provenance and nothing else;
* **contract** -- drop the three single-column constraints, which is what actually
  removes the permission.

Part two: a household could claim a row out of the shared catalogue
-------------------------------------------------------------------

``product`` is the one table where ``household_id IS NULL`` means "shared", and its
``UPDATE`` policy reads ``USING (own or public) WITH CHECK (own or public)``. A
household could therefore run ``UPDATE product SET household_id = <itself> WHERE
household_id IS NULL`` and take a catalogue entry out of the mutualised cache --
one row per statement, and the entry stops existing for every other household.

The obvious repair is to narrow that ``WITH CHECK`` to ``household_id =
chaudron_current_household()``: read the public, write only your own. Measured
against PostgreSQL 16 as the application role, it does the exact opposite of what
it looks like:

* the claim **still succeeds**. ``WITH CHECK`` is evaluated on the *new* row, and
  the new row of a claim is an own row -- it is the one shape the narrowed
  predicate is written to allow;
* and ``ProductRepository.upsert_public`` **breaks**, with ``new row violates
  row-level security policy for table "product"``, because refreshing a cached
  entry writes back a row whose ``household_id`` is still ``NULL``. That is the
  shared cache of ADR-0008: every barcode scan of an already-known product goes
  through it.

No arrangement of policies can express what is actually wanted, because what is
wanted is a comparison between the old row and the new one: ``USING`` sees only the
old, ``WITH CHECK`` only the new, and multiple permissive policies combine the two
sides independently. So the rule is stated where a rule about a transition can be
stated -- a ``BEFORE UPDATE`` trigger, which fires for the owner as well and is
therefore not another guarantee that evaporates when the connecting role changes.

``product.household_id`` becomes immutable in both directions. Claiming a public
entry is the finding; publishing a private one is the same hole read backwards --
it would let a household push a row of its own into a table every other household
reads, and there is no more reason to permit it. Nothing in the application changes
the column after insert: ``create_private`` sets it once, ``upsert_public`` never
writes it.

What this deliberately does **not** change: a household may still edit the
*content* of a public entry. That is the bargain ADR-0008 struck -- the catalogue is
a shared cache of Open Food Facts answers, written by whichever household scans the
barcode first -- and ``upsert_public`` is the code that depends on it. Closing it
would mean routing that write through a ``SECURITY DEFINER`` function, which is a
change to the repository rather than to the schema.

Rollback
--------

``downgrade`` restores the single-column foreign keys and drops the trigger, in
the reverse order. It cannot restore rows the repair nulled -- provenance, once
forgotten, is gone -- and says so here rather than pretending otherwise.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: ``(child table, column, parent table, single-column constraint name,
#: composite constraint name)``. The names are the ones the naming convention on
#: ``Base.metadata`` produces -- ``fk_%(table_name)s_%(column_0_N_name)s`` --
#: because ``tests/test_schema_naming_guard.py`` compares the catalogue to the
#: model and a hand-chosen name here would fail it.
_ORIGIN_REFERENCES: Final[tuple[tuple[str, str, str, str, str], ...]] = (
    (
        "inventory_lot",
        "source_receipt_line_id",
        "receipt_line",
        "fk_inventory_lot_source_receipt_line_id",
        "fk_inventory_lot_household_id_source_receipt_line_id",
    ),
    (
        "shopping_list_item",
        "origin_recipe_suggestion_id",
        "recipe_suggestion",
        "fk_shopping_list_item_origin_recipe_suggestion_id",
        "fk_shopping_list_item_household_id_origin_recipe_suggestion_id",
    ),
    (
        "stock_movement",
        "recipe_suggestion_id",
        "recipe_suggestion",
        "fk_stock_movement_recipe_suggestion_id",
        "fk_stock_movement_household_id_recipe_suggestion_id",
    ),
)

#: The unique constraint ``inventory_lot``'s new reference needs as a target. A
#: composite foreign key requires a unique constraint on exactly its columns, and
#: ``receipt_line`` carried only its primary key on ``id``.
_RECEIPT_LINE_TENANT_KEY: Final = "uq_receipt_line_household_id"

_PRODUCT_TENANT_TRIGGER: Final = "product_tenant_is_immutable"
_PRODUCT_TENANT_FUNCTION: Final = "chaudron_product_tenant_is_immutable"


def upgrade() -> None:
    # -- expand ------------------------------------------------------------- #
    op.execute(
        f"ALTER TABLE receipt_line ADD CONSTRAINT {_RECEIPT_LINE_TENANT_KEY} "
        f"UNIQUE (household_id, id)"
    )

    for child, column, parent, _old, new in _ORIGIN_REFERENCES:
        # -- migrate: forget a provenance that crosses a household boundary --
        #
        # `ON DELETE SET NULL` is what these columns already do when the parent
        # goes; a row that never had a legitimate parent gets the same treatment.
        # S608: every name interpolated here comes from `_ORIGIN_REFERENCES`
        # above, a module constant of three literal tuples. There is no input to
        # this migration -- an identifier cannot be a bind parameter, which is why
        # DDL and the repair that accompanies it are written this way throughout
        # `migrations/`.
        op.execute(  # noqa: S608
            f"""
            UPDATE {child} AS child
            SET {column} = NULL
            WHERE child.{column} IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM {parent} AS parent
                  WHERE parent.id = child.{column}
                    AND parent.household_id = child.household_id
              )
            """
        )
        op.execute(
            f"""
            ALTER TABLE {child}
            ADD CONSTRAINT {new}
            FOREIGN KEY (household_id, {column})
            REFERENCES {parent} (household_id, id)
            ON DELETE SET NULL ({column})
            """
        )

    # -- contract ----------------------------------------------------------- #
    for child, _column, _parent, old, _new in _ORIGIN_REFERENCES:
        op.execute(f"ALTER TABLE {child} DROP CONSTRAINT {old}")

    # -- the shared catalogue's tenant -------------------------------------- #
    op.execute(
        f"""
        CREATE FUNCTION {_PRODUCT_TENANT_FUNCTION}() RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION
                'product.household_id is immutable: a catalogue entry cannot be '
                'moved between the shared catalogue and a household'
                USING ERRCODE = 'raise_exception';
        END
        $$
        """
    )
    op.execute(
        f"COMMENT ON FUNCTION {_PRODUCT_TENANT_FUNCTION}() IS "
        "'Refuses any UPDATE that changes product.household_id, in either "
        "direction. A row-level security policy cannot express this: WITH CHECK "
        "sees only the new row, and a household claiming a public entry writes a "
        "row that is its own. See revision 0015.'"
    )
    op.execute(
        f"""
        CREATE TRIGGER {_PRODUCT_TENANT_TRIGGER}
        BEFORE UPDATE ON product
        FOR EACH ROW
        WHEN (NEW.household_id IS DISTINCT FROM OLD.household_id)
        EXECUTE FUNCTION {_PRODUCT_TENANT_FUNCTION}()
        """
    )


def downgrade() -> None:
    op.execute(f"DROP TRIGGER IF EXISTS {_PRODUCT_TENANT_TRIGGER} ON product")
    op.execute(f"DROP FUNCTION IF EXISTS {_PRODUCT_TENANT_FUNCTION}()")

    for child, column, parent, old, new in _ORIGIN_REFERENCES:
        op.execute(
            f"""
            ALTER TABLE {child}
            ADD CONSTRAINT {old}
            FOREIGN KEY ({column})
            REFERENCES {parent} (id)
            ON DELETE SET NULL
            """
        )
        op.execute(f"ALTER TABLE {child} DROP CONSTRAINT {new}")

    op.execute(f"ALTER TABLE receipt_line DROP CONSTRAINT {_RECEIPT_LINE_TENANT_KEY}")
