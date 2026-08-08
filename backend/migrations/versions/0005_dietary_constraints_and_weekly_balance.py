"""dietary constraints, weekly balance, shelf life and suggestion feedback

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-04 06:00:00.000000+00:00

Implements ADR-0009 and the two schema additions that arrived with it. Five new
tables, twelve new enum types, and columns on three existing tables.

Four things in here are load-bearing and would be easy to lose in a rebase.

*``product.allergens_risk`` is a generated column, not an application field.*
It is the mechanism that keeps "no allergen data" from reading as "no
allergen": a product whose upstream record declared nothing carries **all
fourteen** allergens in its risk set, so the obvious exclusion query withholds
it whether or not its author thought about the unknown case. Writing it from
the application would put that guarantee back in the hands of whoever wrote the
last ``UPDATE``.

*Row-level security on ``household_person``.* It carries ``household_id``, so
without a policy it is a hole straight through the isolation revision ``0004``
established -- and the rows in it are health data, which is the worst possible
table to leak. ``scripts/provision_app_role.py --check`` reports any tenant
table without RLS; this revision keeps that report empty.

*``inventory_lot.expiry_date_source`` is NOT NULL with a ``declared``
default.* Every write path that exists today takes the date from a human, so
backfilling existing rows to ``declared`` is not a convenience, it is the
truth about them. The value that licenses a hedged rendering (``estimated``) is
the one nobody can get by omission.

*The reference rows come from Python, not from literals here.* ``PNNS_2019``,
``PNNS_GUIDELINES`` and ``INFANT_RESTRICTIONS`` live in
``chaudron.domain.dietary`` and ``SHELF_LIFE_GUIDELINES`` in
``chaudron.domain.shelf_life``, each next to the source URL it was verified
against. A migration is the wrong place to review whether honey is forbidden
before twelve months; a reviewed module is the right one, and a test asserts
the tables match it.

``downgrade`` is real and has been exercised: it drops the policy, the columns,
the tables and then the enum types, in that order. ``op.drop_table`` leaves a
native enum behind, and an ``upgrade`` after an incomplete ``downgrade`` would
fail on "type already exists" -- the same trap revision ``0001`` documents.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from chaudron.domain.dietary import (
    INFANT_RESTRICTIONS,
    PNNS_2019,
    PNNS_GUIDELINES,
)
from chaudron.domain.models import ALL_ALLERGENS_SQL, AgeBand, Allergen, FoodFamily, PnnsMarker
from chaudron.domain.shelf_life import SHELF_LIFE_GUIDELINES

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: New native enum types, in creation order. Names match the ones the model
#: passes to ``pg_enum`` -- a test compares the two lists.
_ENUM_TYPES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("age_band", tuple(member.value for member in AgeBand)),
    ("diet", ("omnivore", "pescatarian", "vegetarian", "vegan")),
    ("infant_texture", ("smooth", "soft_pieces", "pieces")),
    ("allergen", tuple(member.value for member in Allergen)),
    ("allergen_data_state", ("unknown", "declared")),
    ("pnns_marker", tuple(member.value for member in PnnsMarker)),
    ("pnns_direction", ("at_least", "at_most", "around")),
    ("pnns_unit", ("serving", "gram")),
    ("infant_risk_kind", ("choking", "microbiological", "toxicological", "nutritional")),
    ("food_family", tuple(member.value for member in FoodFamily)),
    ("expiry_date_source", ("declared", "estimated")),
    ("recipe_feedback", ("cooked", "not_interested")),
)

_TENANT_FUNCTION = "chaudron_current_household"
_PERSON_TABLE = "household_person"
_PERSON_POLICY = f"{_PERSON_TABLE}_household_isolation"
_OWN_ROW = f"household_id = {_TENANT_FUNCTION}()"


def _enum(name: str, *, create_type: bool = False) -> postgresql.ENUM:
    """Reference an enum type that already exists, without trying to create it."""
    values = next(members for type_name, members in _ENUM_TYPES if type_name == name)
    return postgresql.ENUM(*values, name=name, create_type=create_type)


def _allergen_array() -> postgresql.ARRAY:
    return postgresql.ARRAY(_enum("allergen"))


def upgrade() -> None:
    for name, values in _ENUM_TYPES:
        postgresql.ENUM(*values, name=name).create(op.get_bind(), checkfirst=False)

    _create_reference_tables()
    _create_household_person()
    _extend_product()
    _extend_inventory_lot()
    _extend_recipe_suggestion()
    _seed_reference_rows()


def downgrade() -> None:
    op.drop_index("ix_recipe_suggestion_feedback", table_name="recipe_suggestion")
    op.drop_constraint(
        "ck_recipe_suggestion_feedback_matches_status", "recipe_suggestion", type_="check"
    )
    op.drop_constraint("ck_recipe_suggestion_feedback_dated", "recipe_suggestion", type_="check")
    op.drop_constraint(
        "fk_recipe_suggestion_feedback_by_user_id", "recipe_suggestion", type_="foreignkey"
    )
    op.drop_column("recipe_suggestion", "feedback_by_user_id")
    op.drop_column("recipe_suggestion", "feedback_at")
    op.drop_column("recipe_suggestion", "feedback")

    op.drop_constraint("ck_inventory_lot_estimate_needs_a_date", "inventory_lot", type_="check")
    op.drop_column("inventory_lot", "expiry_date_source")

    op.drop_index("ix_product_pnns_markers", table_name="product")
    op.drop_index("ix_product_allergens_risk", table_name="product")
    op.drop_constraint("ck_product_tag_arrays_wellformed", "product", type_="check")
    op.drop_constraint("ck_product_allergen_unknown_is_empty", "product", type_="check")
    op.drop_constraint("ck_product_allergen_states_disjoint", "product", type_="check")
    for column in (
        "pnns_markers",
        "allergens_risk",
        "allergens_may_contain",
        "allergens_contains",
        "allergen_state",
        "food_family",
    ):
        op.drop_column("product", column)

    op.execute(f"DROP POLICY IF EXISTS {_PERSON_POLICY} ON {_PERSON_TABLE}")
    op.drop_table(_PERSON_TABLE)

    op.drop_table("shelf_life_guideline")
    op.drop_table("infant_food_restriction")
    op.drop_table("pnns_guideline")
    op.drop_table("nutrition_reference")

    # After the tables: a type still used by a column cannot be dropped.
    for name, _ in reversed(_ENUM_TYPES):
        op.execute(f"DROP TYPE IF EXISTS {name}")


# --------------------------------------------------------------------------- #
# Reference tables (global -- no household_id, therefore no policy)
# --------------------------------------------------------------------------- #


def _create_reference_tables() -> None:
    op.create_table(
        "nutrition_reference",
        sa.Column("version", sa.String(length=32), nullable=False),
        sa.Column("label", sa.String(length=120), nullable=False),
        sa.Column("published_on", sa.Date(), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column(
            "is_current", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.PrimaryKeyConstraint("version", name="pk_nutrition_reference"),
    )
    op.create_index(
        "uq_nutrition_reference_current",
        "nutrition_reference",
        [sa.text("(true)")],
        unique=True,
        postgresql_where=sa.text("is_current"),
    )

    op.create_table(
        "pnns_guideline",
        sa.Column("reference_version", sa.String(length=32), nullable=False),
        sa.Column("marker", _enum("pnns_marker"), nullable=False),
        sa.Column("label", sa.String(length=80), nullable=False),
        sa.Column("direction", _enum("pnns_direction"), nullable=False),
        sa.Column("amount", sa.Numeric(precision=10, scale=3), nullable=False),
        sa.Column("unit", _enum("pnns_unit"), nullable=False),
        sa.Column("window_days", sa.SmallInteger(), nullable=False),
        sa.Column("statement", sa.String(length=200), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("sort_order", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.CheckConstraint("amount > 0", name="ck_pnns_guideline_amount_positive"),
        sa.CheckConstraint("window_days > 0", name="ck_pnns_guideline_window_positive"),
        sa.ForeignKeyConstraint(
            ["reference_version"],
            ["nutrition_reference.version"],
            name="fk_pnns_guideline_reference_version",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("reference_version", "marker", name="pk_pnns_guideline"),
    )

    op.create_table(
        "infant_food_restriction",
        sa.Column("reference_version", sa.String(length=32), nullable=False),
        sa.Column("rule_code", sa.String(length=40), nullable=False),
        sa.Column("label", sa.String(length=120), nullable=False),
        sa.Column("applies_to_bands", postgresql.ARRAY(_enum("age_band")), nullable=False),
        sa.Column("risk", _enum("infant_risk_kind"), nullable=False),
        sa.Column(
            "category_tags",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column(
            "name_patterns",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column("statement", sa.String(length=300), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("sort_order", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.CheckConstraint(
            "cardinality(applies_to_bands) > 0 AND array_position(applies_to_bands, NULL) IS NULL",
            name="ck_infant_food_restriction_bands_present",
        ),
        sa.CheckConstraint(
            "cardinality(category_tags) > 0 OR cardinality(name_patterns) > 0",
            name="ck_infant_food_restriction_matcher_present",
        ),
        sa.ForeignKeyConstraint(
            ["reference_version"],
            ["nutrition_reference.version"],
            name="fk_infant_food_restriction_reference_version",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "reference_version", "rule_code", name="pk_infant_food_restriction"
        ),
    )

    op.create_table(
        "shelf_life_guideline",
        sa.Column("family", _enum("food_family"), nullable=False),
        sa.Column("label", sa.String(length=80), nullable=False),
        sa.Column("unopened_days", sa.SmallInteger(), nullable=True),
        sa.Column("opened_days", sa.SmallInteger(), nullable=True),
        sa.Column(
            "date_kind",
            postgresql.ENUM(name="expiry_date_kind", create_type=False),
            nullable=False,
        ),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "(unopened_days IS NULL OR unopened_days > 0)"
            " AND (opened_days IS NULL OR opened_days > 0)",
            name="ck_shelf_life_guideline_durations_positive",
        ),
        sa.CheckConstraint(
            "unopened_days IS NOT NULL OR opened_days IS NOT NULL",
            name="ck_shelf_life_guideline_some_duration",
        ),
        sa.CheckConstraint(
            "date_kind <> 'unknown'", name="ck_shelf_life_guideline_date_kind_known"
        ),
        sa.PrimaryKeyConstraint("family", name="pk_shelf_life_guideline"),
    )


# --------------------------------------------------------------------------- #
# The eaters
# --------------------------------------------------------------------------- #


def _create_household_person() -> None:
    op.create_table(
        _PERSON_TABLE,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("household_id", sa.Uuid(), nullable=False),
        sa.Column("user_account_id", sa.Uuid(), nullable=True),
        sa.Column("display_name", sa.String(length=120), nullable=False),
        sa.Column(
            "age_band",
            _enum("age_band"),
            server_default=sa.text("'adult'"),
            nullable=False,
            comment=(
                "HEALTH DATA (GDPR art. 9), and for an infant a minor's. Stored as a "
                "band and never as a date of birth. Never logged, never echoed in an "
                "error message, erased with the row."
            ),
        ),
        sa.Column(
            "diet",
            _enum("diet"),
            server_default=sa.text("'omnivore'"),
            nullable=False,
            comment=(
                "SENSITIVE: a diet discloses belief and health. Hard constraint, "
                "applied as a filter on the inventory, never as a prompt instruction."
            ),
        ),
        sa.Column(
            "allergens",
            _allergen_array(),
            server_default=sa.text("'{}'"),
            nullable=False,
            comment=(
                "HEALTH DATA (GDPR art. 9): declared allergies. Never logged, never "
                "included in an error message, never sent to a model. Loaded only "
                "through an explicit undefer(), erased with the row."
            ),
        ),
        sa.Column(
            "infant_texture",
            _enum("infant_texture"),
            nullable=True,
            comment="HEALTH DATA of a minor. Hard constraint; NULL outside an infant band.",
        ),
        sa.Column(
            "free_text_restrictions",
            sa.Text(),
            nullable=True,
            comment=(
                "SENSITIVE free text supplied by the household. Never logged. Passed "
                "to a model as a preference only: it cannot be a filter, because "
                "nothing in the catalogue says which products contain coriander."
            ),
        ),
        sa.Column("sort_order", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(age_band IN ('infant_4_6m', 'infant_6_9m', 'infant_9_12m', 'infant_12_36m'))"
            " = (infant_texture IS NOT NULL)",
            name="ck_household_person_infant_texture_band",
        ),
        sa.CheckConstraint(
            "array_position(allergens, NULL) IS NULL AND cardinality(allergens) <= 14",
            name="ck_household_person_allergens_wellformed",
        ),
        sa.CheckConstraint(
            "free_text_restrictions IS NULL OR char_length(free_text_restrictions) <= 500",
            name="ck_household_person_free_text_length",
        ),
        sa.ForeignKeyConstraint(
            ["household_id"],
            ["household.id"],
            name="fk_household_person_household_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_account_id"],
            ["user_account.id"],
            name="fk_household_person_user_account_id",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_household_person"),
        sa.UniqueConstraint("household_id", "id", name="uq_household_person_household_id"),
    )
    op.create_index(
        "uq_household_person_user_account",
        _PERSON_TABLE,
        ["household_id", "user_account_id"],
        unique=True,
        postgresql_where=sa.text("user_account_id IS NOT NULL"),
    )

    # The isolation half of revision 0004, extended to the table it did not know
    # about. Omitting it would leave a tenant table readable by any connection
    # that forgot a WHERE -- and this one holds allergies.
    op.execute(f"ALTER TABLE {_PERSON_TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY {_PERSON_POLICY} ON {_PERSON_TABLE}
        FOR ALL
        USING ({_OWN_ROW})
        WITH CHECK ({_OWN_ROW})
        """
    )


# --------------------------------------------------------------------------- #
# Existing tables
# --------------------------------------------------------------------------- #


def _extend_product() -> None:
    op.add_column("product", sa.Column("food_family", _enum("food_family"), nullable=True))
    op.add_column(
        "product",
        sa.Column(
            "allergen_state",
            _enum("allergen_data_state"),
            server_default=sa.text("'unknown'"),
            nullable=False,
            comment=(
                "'unknown' means the upstream record carried no allergen fields. It "
                "never means 'contains no allergen': treat it as 'may contain'."
            ),
        ),
    )
    op.add_column(
        "product",
        sa.Column(
            "allergens_contains",
            _allergen_array(),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
    )
    op.add_column(
        "product",
        sa.Column(
            "allergens_may_contain",
            _allergen_array(),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
    )
    # Raw SQL rather than a `Computed` column: the expression casts an array
    # literal to an enum array, which alembic renders with quoting that
    # PostgreSQL then rejects. Written out, it is also the one line of this
    # revision worth reading twice.
    op.execute(
        "ALTER TABLE product ADD COLUMN allergens_risk allergen[] "
        "GENERATED ALWAYS AS ("
        f"CASE WHEN allergen_state = 'unknown' THEN {ALL_ALLERGENS_SQL}"
        " ELSE allergens_contains || allergens_may_contain END"
        ") STORED"
    )
    op.execute(
        "COMMENT ON COLUMN product.allergens_risk IS "
        "'GENERATED. Allergens this product cannot be cleared of, including "
        "every one of them when no data exists. The only column a filter "
        "should read; allergens_contains alone would treat ''unknown'' as safe.'"
    )
    op.add_column(
        "product",
        sa.Column(
            "pnns_markers",
            postgresql.ARRAY(_enum("pnns_marker")),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
    )

    op.create_check_constraint(
        "ck_product_allergen_states_disjoint",
        "product",
        "NOT (allergens_contains && allergens_may_contain)",
    )
    op.create_check_constraint(
        "ck_product_allergen_unknown_is_empty",
        "product",
        "allergen_state <> 'unknown'"
        " OR (cardinality(allergens_contains) = 0 AND cardinality(allergens_may_contain) = 0)",
    )
    op.create_check_constraint(
        "ck_product_tag_arrays_wellformed",
        "product",
        "array_position(allergens_contains, NULL) IS NULL"
        " AND array_position(allergens_may_contain, NULL) IS NULL"
        " AND array_position(pnns_markers, NULL) IS NULL",
    )
    op.create_index(
        "ix_product_allergens_risk", "product", ["allergens_risk"], postgresql_using="gin"
    )
    op.create_index(
        "ix_product_pnns_markers", "product", ["pnns_markers"], postgresql_using="gin"
    )


def _extend_inventory_lot() -> None:
    # NOT NULL with a default backfills every existing row to `declared`, which
    # is what they are: every path that could have written a date so far took it
    # from a human.
    op.add_column(
        "inventory_lot",
        sa.Column(
            "expiry_date_source",
            _enum("expiry_date_source"),
            server_default=sa.text("'declared'"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "ck_inventory_lot_estimate_needs_a_date",
        "inventory_lot",
        "best_before IS NOT NULL OR expiry_date_source = 'declared'",
    )


def _extend_recipe_suggestion() -> None:
    op.add_column(
        "recipe_suggestion", sa.Column("feedback", _enum("recipe_feedback"), nullable=True)
    )
    op.add_column(
        "recipe_suggestion", sa.Column("feedback_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("recipe_suggestion", sa.Column("feedback_by_user_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_recipe_suggestion_feedback_by_user_id",
        "recipe_suggestion",
        "user_account",
        ["feedback_by_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_check_constraint(
        "ck_recipe_suggestion_feedback_dated",
        "recipe_suggestion",
        "(feedback IS NULL) = (feedback_at IS NULL)",
    )
    op.create_check_constraint(
        "ck_recipe_suggestion_feedback_matches_status",
        "recipe_suggestion",
        "feedback IS NULL"
        " OR (feedback = 'cooked' AND status = 'cooked' AND cooked_at IS NOT NULL)"
        " OR (feedback = 'not_interested' AND status = 'discarded')",
    )
    op.create_index(
        "ix_recipe_suggestion_feedback",
        "recipe_suggestion",
        ["provider_mode", "model", "feedback"],
        postgresql_where=sa.text("feedback IS NOT NULL"),
    )


# --------------------------------------------------------------------------- #
# Reference rows
# --------------------------------------------------------------------------- #


def _seed_reference_rows() -> None:
    op.bulk_insert(
        sa.table(
            "nutrition_reference",
            sa.column("version", sa.String),
            sa.column("label", sa.String),
            sa.column("published_on", sa.Date),
            sa.column("source_url", sa.Text),
            sa.column("is_current", sa.Boolean),
        ),
        [
            {
                "version": PNNS_2019.version,
                "label": PNNS_2019.label,
                "published_on": PNNS_2019.published_on,
                "source_url": PNNS_2019.source_url,
                "is_current": PNNS_2019.is_current,
            }
        ],
    )

    op.bulk_insert(
        sa.table(
            "pnns_guideline",
            sa.column("reference_version", sa.String),
            sa.column("marker", sa.Enum(name="pnns_marker")),
            sa.column("label", sa.String),
            sa.column("direction", sa.Enum(name="pnns_direction")),
            sa.column("amount", sa.Numeric),
            sa.column("unit", sa.Enum(name="pnns_unit")),
            sa.column("window_days", sa.SmallInteger),
            sa.column("statement", sa.String),
            sa.column("source_url", sa.Text),
            sa.column("sort_order", sa.SmallInteger),
        ),
        [
            {
                "reference_version": PNNS_2019.version,
                "marker": guideline.marker.value,
                "label": guideline.label,
                "direction": guideline.direction.value,
                "amount": guideline.amount,
                "unit": guideline.unit.value,
                "window_days": guideline.window_days,
                "statement": guideline.statement,
                "source_url": guideline.source_url,
                "sort_order": guideline.sort_order,
            }
            for guideline in PNNS_GUIDELINES
        ],
    )

    op.bulk_insert(
        sa.table(
            "infant_food_restriction",
            sa.column("reference_version", sa.String),
            sa.column("rule_code", sa.String),
            sa.column("label", sa.String),
            sa.column(
                "applies_to_bands",
                postgresql.ARRAY(postgresql.ENUM(name="age_band", create_type=False)),
            ),
            sa.column("risk", sa.Enum(name="infant_risk_kind")),
            sa.column("category_tags", postgresql.ARRAY(sa.Text())),
            sa.column("name_patterns", postgresql.ARRAY(sa.Text())),
            sa.column("statement", sa.String),
            sa.column("source_url", sa.Text),
            sa.column("sort_order", sa.SmallInteger),
        ),
        [
            {
                "reference_version": PNNS_2019.version,
                "rule_code": rule.rule_code,
                "label": rule.label,
                "applies_to_bands": [band.value for band in rule.applies_to_bands],
                "risk": rule.risk.value,
                "category_tags": list(rule.category_tags),
                "name_patterns": list(rule.name_patterns),
                "statement": rule.statement,
                "source_url": rule.source_url,
                "sort_order": rule.sort_order,
            }
            for rule in INFANT_RESTRICTIONS
        ],
    )

    op.bulk_insert(
        sa.table(
            "shelf_life_guideline",
            sa.column("family", sa.Enum(name="food_family")),
            sa.column("label", sa.String),
            sa.column("unopened_days", sa.SmallInteger),
            sa.column("opened_days", sa.SmallInteger),
            sa.column("date_kind", sa.Enum(name="expiry_date_kind")),
            sa.column("source_url", sa.Text),
            sa.column("note", sa.Text),
        ),
        [
            {
                "family": guideline.family.value,
                "label": guideline.label,
                "unopened_days": guideline.unopened_days,
                "opened_days": guideline.opened_days,
                "date_kind": guideline.date_kind.value,
                "source_url": guideline.source_url,
                "note": guideline.note,
            }
            for guideline in SHELF_LIFE_GUIDELINES
        ],
    )
