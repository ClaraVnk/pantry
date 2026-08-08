"""align the deployed check-constraint names with the metadata naming convention

Revision ID: 0010
Revises: 0009
Create Date: 2026-08-04 07:00:00.000000+00:00

Revisions ``0005`` and ``0006`` named their check constraints with the
``ck_<table>_`` prefix already applied. The naming convention on
``Base.metadata`` renders a check constraint as
``ck_%(table_name)s_%(constraint_name)s``, so the explicit name is a *component*
of the final name rather than the whole of it -- and Alembic applies that same
convention inside a migration, because ``migrations/env.py`` hands it
``target_metadata``. Every one of those constraints therefore landed in
PostgreSQL with its prefix doubled::

    ck_budget_target_ck_budget_target_amount_positive

Eighteen constraints across eight tables, four of them past the 63-character
identifier limit and truncated by SQLAlchemy to a stem plus a four-hex-digit
hash of the full name (``..._duratio_eb80``). Revision ``0008`` hit the same
trap and caught it before shipping; this revision repairs what ``0005`` and
``0006`` already deployed.

*Why this is not cosmetic.* The name in the database is not the name in
``domain/models.py``, so nothing in the application can address these
constraints: an ``IntegrityError`` handler that wants to map a violated
constraint to a message has no name to match on, a later revision that wants to
alter one has to hardcode a truncation hash, and a hand-written ``ALTER TABLE
... DROP CONSTRAINT`` using the model's name fails. The truncated four are worse
still: their names are not derivable by reading anything, only by querying
``pg_constraint``.

*Why a new revision rather than a fix in place.* ``0005`` and ``0006`` are
applied on existing databases. Editing them would make the history describe a
schema that no deployed database has, and would leave every already-migrated
database with the doubled names and no revision that repairs them.

*Why renames and not drop/recreate.* ``ALTER TABLE ... RENAME CONSTRAINT`` is a
catalogue update: no table rewrite, no revalidation of existing rows, no window
during which the invariant is unenforced. Dropping and recreating each
constraint would take an ``ACCESS EXCLUSIVE`` lock for a full scan per table and
buy nothing.

*The guard.* Each rename checks the catalogue first. A constraint already
carrying its intended name is skipped, so the revision is safe to re-run against
a database repaired by other means; a constraint carrying neither name raises,
because a silent no-op here would leave a database that ``alembic check`` calls
clean while the constraint it was meant to fix is missing.

``downgrade`` renames every constraint back to the doubled form ``0005`` and
``0006`` produced, so ``head -> base -> head`` reproduces exactly the schema
those revisions describe.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: ``(table, deployed name, intended name)``, read out of ``pg_constraint`` on a
#: PostgreSQL 16 migrated to ``0009``, not transcribed from the migration
#: sources -- the four truncated names cannot be obtained any other way. The
#: intended names are what ``Base.metadata`` renders today.
_RENAMES: Final[tuple[tuple[str, str, str], ...]] = (
    # -- revision 0006 ------------------------------------------------------ #
    (
        "budget_target",
        "ck_budget_target_ck_budget_target_amount_positive",
        "ck_budget_target_amount_positive",
    ),
    (
        "budget_target",
        "ck_budget_target_ck_budget_target_currency_iso_4217",
        "ck_budget_target_currency_iso_4217",
    ),
    # -- revision 0005 ------------------------------------------------------ #
    (
        "household_person",
        "ck_household_person_ck_household_person_allergens_wellformed",
        "ck_household_person_allergens_wellformed",
    ),
    (
        "household_person",
        "ck_household_person_ck_household_person_free_text_length",
        "ck_household_person_free_text_length",
    ),
    (
        "household_person",
        "ck_household_person_ck_household_person_infant_texture_band",
        "ck_household_person_infant_texture_band",
    ),
    # Truncated at 63 characters: stem plus a hash of the full name.
    (
        "infant_food_restriction",
        "ck_infant_food_restriction_ck_infant_food_restriction_b_df82",
        "ck_infant_food_restriction_bands_present",
    ),
    (
        "infant_food_restriction",
        "ck_infant_food_restriction_ck_infant_food_restriction_m_21e9",
        "ck_infant_food_restriction_matcher_present",
    ),
    (
        "inventory_lot",
        "ck_inventory_lot_ck_inventory_lot_estimate_needs_a_date",
        "ck_inventory_lot_estimate_needs_a_date",
    ),
    (
        "pnns_guideline",
        "ck_pnns_guideline_ck_pnns_guideline_amount_positive",
        "ck_pnns_guideline_amount_positive",
    ),
    (
        "pnns_guideline",
        "ck_pnns_guideline_ck_pnns_guideline_window_positive",
        "ck_pnns_guideline_window_positive",
    ),
    (
        "product",
        "ck_product_ck_product_allergen_states_disjoint",
        "ck_product_allergen_states_disjoint",
    ),
    (
        "product",
        "ck_product_ck_product_allergen_unknown_is_empty",
        "ck_product_allergen_unknown_is_empty",
    ),
    (
        "product",
        "ck_product_ck_product_tag_arrays_wellformed",
        "ck_product_tag_arrays_wellformed",
    ),
    (
        "recipe_suggestion",
        "ck_recipe_suggestion_ck_recipe_suggestion_feedback_dated",
        "ck_recipe_suggestion_feedback_dated",
    ),
    (
        "recipe_suggestion",
        "ck_recipe_suggestion_ck_recipe_suggestion_feedback_matc_96f3",
        "ck_recipe_suggestion_feedback_matches_status",
    ),
    (
        "shelf_life_guideline",
        "ck_shelf_life_guideline_ck_shelf_life_guideline_date_kind_known",
        "ck_shelf_life_guideline_date_kind_known",
    ),
    (
        "shelf_life_guideline",
        "ck_shelf_life_guideline_ck_shelf_life_guideline_duratio_eb80",
        "ck_shelf_life_guideline_durations_positive",
    ),
    (
        "shelf_life_guideline",
        "ck_shelf_life_guideline_ck_shelf_life_guideline_some_duration",
        "ck_shelf_life_guideline_some_duration",
    ),
)

_EXISTS: Final = sa.text(
    """
    SELECT 1
    FROM pg_constraint AS c
    JOIN pg_class AS t ON t.oid = c.conrelid
    JOIN pg_namespace AS n ON n.oid = t.relnamespace
    WHERE n.nspname = current_schema()
      AND t.relname = :table
      AND c.conname = :name
    """
)


def _exists(table: str, name: str) -> bool:
    return op.get_bind().scalar(_EXISTS, {"table": table, "name": name}) is not None


def _rename(table: str, old: str, new: str) -> None:
    """Rename one constraint, or explain why it could not be found.

    Identifiers are quoted rather than interpolated bare: they are literals from
    :data:`_RENAMES` so there is nothing to inject, but a quoted identifier is
    the form that keeps working if a later name ever needs it.
    """
    if _exists(table, new):
        return
    if not _exists(table, old):
        raise RuntimeError(
            f"{table}: expected a check constraint named {old!r} (to become {new!r}) "
            "and found neither name. This database was not built by revisions 0005 "
            "and 0006 as this repository has them; inspect pg_constraint before "
            "migrating further."
        )
    op.execute(f'ALTER TABLE "{table}" RENAME CONSTRAINT "{old}" TO "{new}"')


def upgrade() -> None:
    for table, deployed, intended in _RENAMES:
        _rename(table, deployed, intended)


def downgrade() -> None:
    for table, deployed, intended in _RENAMES:
        _rename(table, intended, deployed)
