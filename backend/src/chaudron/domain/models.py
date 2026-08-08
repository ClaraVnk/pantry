# Scoping skeleton: tables, constraints and indexes only -- not wired to anything yet.
"""Chaudron data model skeleton.

Companion document: ``docs/data-model.md`` (French, internal design note). Every
non-obvious decision taken here is argued there; comments below only record the
*why* that a reader of the schema cannot reconstruct on their own.

Deliberately absent from this file:

* ORM ``relationship()`` declarations. Loading strategy is a repository concern,
  and defaults set here would silently leak into every query in the codebase.
* Business logic. Unit conversion, FEFO allocation and lot merging live in the
  application layer; the schema only makes their invariants representable.
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any, ClassVar, Final

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    event,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.engine import Connection
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column

# Without an explicit convention PostgreSQL names constraints itself, and Alembic
# then emits DROP statements against names it cannot reproduce on another database.
NAMING_CONVENTION: Final[dict[str, str]] = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s",
    "pk": "pk_%(table_name)s",
}

# Reusable type instances. Money is 2 decimals, quantities 3: never float, because a
# stock that drifts to -0.0000001 g leaves a ghost row no user can delete.
MONEY = Numeric(12, 2)
QUANTITY = Numeric(12, 3)
CANONICAL_QUANTITY = Numeric(14, 3)


class Base(DeclarativeBase):
    """Declarative base.

    ``type_annotation_map`` makes ``datetime`` mean ``timestamptz`` everywhere: a
    naive timestamp is a bug waiting for the first user who travels.
    """

    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    type_annotation_map: ClassVar[dict[Any, Any]] = {
        uuid.UUID: Uuid(as_uuid=True),
        datetime: DateTime(timezone=True),
        date: Date(),
        Decimal: QUANTITY,
        dict[str, Any]: JSONB,
    }


# --------------------------------------------------------------------------- #
# Required PostgreSQL extensions
# --------------------------------------------------------------------------- #

# `ix_product_name_trgm` is a GIN index using `gin_trgm_ops`, which does not
# exist until `pg_trgm` is installed. Without this listener, `create_all` fails
# on a stock PostgreSQL image with:
#
#   operator class "gin_trgm_ops" does not exist for access method "gin"
#
# Emitting the extension from the metadata keeps the schema self-sufficient:
# tests, a throwaway container and the first Alembic migration all get it
# without anyone having to remember a manual step. Requires the connecting role
# to be a superuser or to hold CREATE on the database — true for the standard
# postgres image and for a database the deploy owns.
REQUIRED_EXTENSIONS: Final[tuple[str, ...]] = ("pg_trgm",)


@event.listens_for(Base.metadata, "before_create")
def _install_required_extensions(
    target: MetaData,
    connection: Connection,
    **kw: Any,
) -> None:
    """Install the extensions the schema depends on, before any table is built.

    A typed listener rather than ``DDL(...)``: the latter is unannotated in the
    SQLAlchemy stubs and would cost a ``type: ignore`` under strict mypy.
    """
    for extension in REQUIRED_EXTENSIONS:
        # Not an injection vector: REQUIRED_EXTENSIONS is a module constant and
        # never contains user input. Identifier quoting is applied regardless.
        connection.execute(text(f'CREATE EXTENSION IF NOT EXISTS "{extension}"'))


# --------------------------------------------------------------------------- #
# Enumerations
# --------------------------------------------------------------------------- #


def pg_enum(python_enum: type[enum.Enum], name: str) -> Enum:
    """Native PostgreSQL enum storing member *values*, not Python member names."""
    return Enum(
        python_enum,
        name=name,
        native_enum=True,
        validate_strings=True,
        values_callable=lambda e: [member.value for member in e],
    )


class MembershipRole(enum.StrEnum):
    OWNER = "owner"
    MEMBER = "member"
    VIEWER = "viewer"


class QuantityDimension(enum.StrEnum):
    """Canonical unit per dimension: gram, millilitre, piece."""

    MASS = "mass"
    VOLUME = "volume"
    COUNT = "count"


class StorageKind(enum.StrEnum):
    """Behaviour depends on the kind, not the label: a freezer suspends expiry."""

    FRIDGE = "fridge"
    FREEZER = "freezer"
    # "pantry" here is the cupboard, not the project. Do not rename it along
    # with the application.
    PANTRY = "pantry"
    CELLAR = "cellar"
    OTHER = "other"


class ProductSource(enum.StrEnum):
    OPEN_FOOD_FACTS = "open_food_facts"
    MANUAL = "manual"
    RECEIPT_IMPORT = "receipt_import"


class ExpiryDateKind(enum.StrEnum):
    """``use_by`` is a safety limit, ``best_before`` a quality one.

    Conflating them yields either anxious alerts on dry pasta or silence on minced
    meat; the wording of every notification depends on this distinction.
    """

    USE_BY = "use_by"
    BEST_BEFORE = "best_before"
    UNKNOWN = "unknown"


class StockEntrySource(enum.StrEnum):
    MANUAL = "manual"
    BARCODE_SCAN = "barcode_scan"
    RECEIPT_IMPORT = "receipt_import"
    SHOPPING_LIST = "shopping_list"
    RECIPE_LEFTOVER = "recipe_leftover"


class StockMovementKind(enum.StrEnum):
    INTAKE = "intake"
    CONSUMPTION = "consumption"
    WASTE = "waste"
    ADJUSTMENT = "adjustment"
    TRANSFER = "transfer"


class ShoppingItemOrigin(enum.StrEnum):
    MANUAL = "manual"
    LOW_STOCK = "low_stock"
    RECIPE = "recipe"


class ReceiptStatus(enum.StrEnum):
    UPLOADED = "uploaded"
    PARSING = "parsing"
    PARSED = "parsed"
    CONFIRMED = "confirmed"
    FAILED = "failed"


class ReceiptLineMatchStatus(enum.StrEnum):
    PENDING = "pending"
    SUGGESTED = "suggested"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    IGNORED = "ignored"


class RecipeStatus(enum.StrEnum):
    GENERATED = "generated"
    SAVED = "saved"
    COOKED = "cooked"
    DISCARDED = "discarded"


class IngredientAvailability(enum.StrEnum):
    IN_STOCK = "in_stock"
    PARTIAL = "partial"
    MISSING = "missing"
    UNKNOWN = "unknown"


class LlmProviderMode(enum.StrEnum):
    """Who provides -- and pays for -- the model access.

    ``instance_owner`` is the only mode whose cost lands on the operator; the other
    two are billed to the household or cost nothing (self-hosted).
    """

    BYOK = "byok"
    OLLAMA = "ollama"
    INSTANCE_OWNER = "instance_owner"


class LlmPurpose(enum.StrEnum):
    """Receipt parsing needs vision, recipe generation does not.

    Hence a binding per purpose rather than a single provider per household.
    """

    RECIPE_GENERATION = "recipe_generation"
    RECEIPT_PARSING = "receipt_parsing"


class LlmConfigStatus(enum.StrEnum):
    UNVERIFIED = "unverified"
    VERIFIED = "verified"
    INVALID_CREDENTIALS = "invalid_credentials"
    DISABLED = "disabled"


# --------------------------------------------------------------------------- #
# Dietary constraints (ADR-0009)
# --------------------------------------------------------------------------- #


class AgeBand(enum.StrEnum):
    """How old an eater is, to the precision the rules actually need.

    A band and never a date of birth. The infant rules are thresholds -- honey
    before twelve months, whole firm foods before the molars -- so the band is
    sufficient to apply every one of them, while a date of birth is a far more
    identifying fact about a minor for no additional capability (ADR-0009).
    """

    ADULT = "adult"
    CHILD = "child"
    INFANT_4_6M = "infant_4_6m"
    INFANT_6_9M = "infant_6_9m"
    INFANT_9_12M = "infant_9_12m"
    INFANT_12_36M = "infant_12_36m"


class Diet(enum.StrEnum):
    """A hard constraint, applied as a filter and never as a prompt instruction."""

    OMNIVORE = "omnivore"
    PESCATARIAN = "pescatarian"
    VEGETARIAN = "vegetarian"
    VEGAN = "vegan"


class InfantTexture(enum.StrEnum):
    """Texture stage of an infant. Hard constraint, same treatment as an allergen."""

    SMOOTH = "smooth"
    SOFT_PIECES = "soft_pieces"
    PIECES = "pieces"


class Allergen(enum.StrEnum):
    """The fourteen substances EU regulation 1169/2011 annex II requires declaring.

    Closed on purpose, and closed on the *regulatory* list rather than on what
    people report. An intolerance outside it (lactose, FODMAP, histamine) goes to
    the free-text field, which is a preference passed to the model -- because no
    catalogue field says which products contain it, and a filter that cannot be
    computed must not look like one.
    """

    GLUTEN = "gluten"
    CRUSTACEANS = "crustaceans"
    EGGS = "eggs"
    FISH = "fish"
    PEANUTS = "peanuts"
    SOYBEANS = "soybeans"
    MILK = "milk"
    NUTS = "nuts"
    CELERY = "celery"
    MUSTARD = "mustard"
    SESAME = "sesame"
    SULPHITES = "sulphites"
    LUPIN = "lupin"
    MOLLUSCS = "molluscs"


class AllergenDataState(enum.StrEnum):
    """Whether anybody has ever declared allergens for a product.

    Two members, and the distinction between them is the whole safety property:
    ``declared`` means the upstream record carried allergen fields, so an
    allergen absent from both lists was *asserted* absent. ``unknown`` means no
    such fields existed -- which is not the same statement, and must never be
    read as one. Open Food Facts is a wiki: the majority state of a fresh product
    is ``unknown``, which is why it is the column default.
    """

    UNKNOWN = "unknown"
    DECLARED = "declared"


class PnnsMarker(enum.StrEnum):
    """Food groups the weekly balance is expressed in (PNNS, France).

    Frequencies and masses per week rather than macronutrients: they are public,
    checkable by the household, and computable from what left the stock. A
    calorie count displayed to two decimals from patchy nutrition data would
    inspire a confidence it has not earned (ADR-0009).

    Wider than the list of benchmarks on purpose. Several members carry no
    :class:`PnnsGuideline` row -- the official wording for added fats and sugary
    foods is "limit them", with no figure anywhere, and inventing one would be
    the exact failure this design avoids elsewhere. They exist so that a bar of
    chocolate resolves to *something*: without them it would land in the
    unresolved count, and "unresolved" has to keep meaning "we do not know what
    this is" rather than "we know and do not score it". Conflating the two makes
    the unresolved figure -- the only warning that the balance rests on a badly
    catalogued pantry -- useless.
    """

    FRUITS_VEGETABLES = "fruits_vegetables"
    LEGUMES = "legumes"
    WHOLE_GRAINS = "whole_grains"
    STARCHY_FOODS = "starchy_foods"
    NUTS = "nuts"
    DAIRY = "dairy"
    FISH = "fish"
    OILY_FISH = "oily_fish"
    RED_MEAT = "red_meat"
    POULTRY = "poultry"
    PROCESSED_MEAT = "processed_meat"
    EGGS = "eggs"
    ADDED_FATS = "added_fats"
    SUGARY_FOODS = "sugary_foods"
    SUGARY_DRINKS = "sugary_drinks"
    UNSWEETENED_DRINKS = "unsweetened_drinks"


class PnnsDirection(enum.StrEnum):
    """Whether a benchmark is a floor, a ceiling, or a figure to sit near.

    Stored rather than inferred from the marker: "at least two fish" and "at most
    500 g of red meat" are the same shape of row and opposite advice, and a
    reader who guesses wrong tells a household to eat more charcuterie.

    ``around`` exists because one official benchmark is genuinely neither. The
    dairy page is titled "towards a *sufficient but limited* consumption": read
    as a floor it would have this application urge more cheese on somebody
    already over the mark, which the source does not say.
    """

    AT_LEAST = "at_least"
    AT_MOST = "at_most"
    AROUND = "around"


class PnnsUnit(enum.StrEnum):
    """A benchmark counts servings or weighs grams; the two never mix in a total."""

    SERVING = "serving"
    GRAM = "gram"


class FoodFamily(enum.StrEnum):
    """Coarse keeping family of a product, derived from its catalogue categories.

    Nine families, and deliberately no more. The point is to pre-fill an expiry
    date at scan time so that a bag of shopping does not cost eighteen date
    entries -- an interface nobody uses collects no data, and every downstream
    feature (urgency ranking, weekly balance, alerts) is fed by data. A finer
    table would look authoritative without being more right: the shelf life of
    *this* pack depends on the cold chain it travelled through, which no
    reference table knows.

    A product whose categories resolve to no family gets **no** suggestion --
    not a cautious default. "Probably seven days" on an unidentified product is
    precisely the invented fact this column exists to avoid.
    """

    FRESH_DAIRY = "fresh_dairy"
    CHEESE = "cheese"
    FRESH_MEAT = "fresh_meat"
    FRESH_FISH = "fresh_fish"
    EGGS = "eggs"
    PRODUCE = "produce"
    FROZEN = "frozen"
    DRY_GOODS = "dry_goods"
    CANNED = "canned"


class ExpiryDateSource(enum.StrEnum):
    """Where a lot's expiry date came from. The distinction is the safety property.

    ``declared`` is a date a human read off the packaging and accepted.
    ``estimated`` is one this application proposed from
    :class:`ShelfLifeGuideline`. They must never be displayed the same way: an
    estimate presented as a use-by date is the application asserting a fact about
    fresh meat that it made up, and the household has no way to tell.
    """

    DECLARED = "declared"
    ESTIMATED = "estimated"


class RecipeFeedback(enum.StrEnum):
    """What the household thought of a suggestion. Two answers, and no scale.

    A five-star widget collects nothing on a phone in a kitchen; "I cooked this"
    and "not interested" are one tap each and are the two facts the ranking and
    the per-provider quality comparison actually need.
    """

    COOKED = "cooked"
    NOT_INTERESTED = "not_interested"


class InfantRiskKind(enum.StrEnum):
    """Why a food is withheld from a young child. Drives the wording, not the rule.

    The rule itself is the same in every case -- withhold before the call,
    invalidate after -- but "he could choke" and "it could carry botulism" are
    not answered by the same substitution, and a household deserves to know
    which one it is looking at.
    """

    CHOKING = "choking"
    MICROBIOLOGICAL = "microbiological"
    TOXICOLOGICAL = "toxicological"
    NUTRITIONAL = "nutritional"


#: Every allergen, as a PostgreSQL array literal. Derived from the enum so the
#: generated column below cannot fall behind a member added to it.
ALL_ALLERGENS_SQL: Final = "'{" + ",".join(member.value for member in Allergen) + "}'::allergen[]"

#: The expression behind ``Product.allergens_risk``.
#:
#: This is the safety mechanism of the whole feature, and it is one CASE. A
#: product nobody has declared allergens for carries **all fourteen** in its risk
#: set, so the obvious filter -- ``NOT (member_allergens && product.allergens_risk)``
#: -- withholds it without the author of that query having thought about it. The
#: dangerous version of this schema is the one where "no allergen row" reads as
#: "no allergen"; here there is no such reading, because the column is generated
#: and an unknown product is maximally, not minimally, excluded.
_ALLERGENS_RISK_SQL: Final = (
    f"CASE WHEN allergen_state = 'unknown' THEN {ALL_ALLERGENS_SQL}"
    " ELSE allergens_contains || allergens_may_contain END"
)


# --------------------------------------------------------------------------- #
# Mixins
# --------------------------------------------------------------------------- #


class UuidPkMixin:
    """UUIDv7 primary key, generated application-side.

    Three reasons, in order: the PWA must be able to create a row *offline* and sync
    it without renumbering; a visible sequence would leak every household's activity
    volume once the instance is public; and unlike v4, v7 is time-ordered, so it does
    not destroy B-tree locality.
    """

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid7)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class HouseholdScopedMixin:
    """Tenant key, carried even where a join could derive it.

    Denormalised on purpose: it keeps every index local, makes future RLS policies a
    local predicate instead of a join, and lets any table be audited per tenant on
    its own. ``CASCADE`` because deleting a household is a GDPR erasure and must be
    total and atomic.
    """

    @declared_attr
    def household_id(cls) -> Mapped[uuid.UUID]:  # noqa: N805
        return mapped_column(ForeignKey("household.id", ondelete="CASCADE"), nullable=False)


# --------------------------------------------------------------------------- #
# Tenancy
# --------------------------------------------------------------------------- #


class Household(UuidPkMixin, TimestampMixin, Base):
    """The unit of ownership: stock, lists and receipts belong here, never to a person."""

    __tablename__ = "household"

    name: Mapped[str] = mapped_column(String(120))
    timezone: Mapped[str] = mapped_column(String(64), server_default=text("'UTC'"))
    default_currency: Mapped[str] = mapped_column(String(3), server_default=text("'CHF'"))
    # Locked by default: only this household may use the instance-wide API key.
    is_instance_owner: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    archived_at: Mapped[datetime | None]
    #: Revocation counter for this household's CalDAV feed credential, which is
    #: derived rather than stored and therefore cannot be deleted. Mixed into the
    #: derivation (``infra/calendar/credentials.py``), so incrementing it -- what
    #: ``POST /v1/calendar/subscription/revoke`` does -- invalidates every device
    #: subscribed to *this* household and touches no other. Without it the only
    #: lever is instance-wide, and a person whose membership was withdrawn keeps
    #: reading the stock.
    calendar_feed_epoch: Mapped[int] = mapped_column(
        Integer,
        server_default=text("1"),
        comment=(
            "Revocation counter for this household's CalDAV feed credential, which is "
            "derived rather than stored. Mixed into the derivation, so incrementing it "
            "invalidates every device subscribed to this household and no other."
        ),
    )

    __table_args__ = (
        CheckConstraint("default_currency ~ '^[A-Z]{3}$'", name="currency_format"),
        # The derivation serialises this counter as an unsigned integer; zero and
        # negative values have no credential to name.
        CheckConstraint("calendar_feed_epoch > 0", name="calendar_feed_epoch_positive"),
        # At most one instance owner, enforced by the database rather than by an
        # operating convention: a mistake here makes the operator pay for a stranger.
        Index(
            "uq_household_instance_owner",
            text("(true)"),
            unique=True,
            postgresql_where=text("is_instance_owner"),
        ),
    )


class UserAccount(UuidPkMixin, TimestampMixin, Base):
    """A person's identity, deliberately independent of any household.

    Putting the household here would forbid dual membership (family home *and*
    flatshare) and force duplicating accounts -- hence passwords, hence half-working
    resets -- the day someone moves.
    """

    __tablename__ = "user_account"

    email: Mapped[str] = mapped_column(String(320))
    # Nullable: an account created through an external identity provider has none.
    password_hash: Mapped[str | None] = mapped_column(Text())
    display_name: Mapped[str] = mapped_column(String(120))
    last_login_at: Mapped[datetime | None]
    disabled_at: Mapped[datetime | None]

    __table_args__ = (
        # Serves the login lookup *and* guarantees uniqueness. A plain unique on
        # `email` would let "Kevin@" and "kevin@" both through.
        Index("uq_user_account_email_lower", text("lower(email)"), unique=True),
    )


class HouseholdMember(Base):
    """Membership of an account in a household, with its role.

    Composite primary key: nobody references a membership by id, and the key doubles
    as the index for the access check run on every single request.
    """

    __tablename__ = "household_member"

    household_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("household.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("user_account.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[MembershipRole] = mapped_column(pg_enum(MembershipRole, "membership_role"))
    joined_at: Mapped[datetime] = mapped_column(server_default=func.now())
    invited_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user_account.id", ondelete="SET NULL")
    )

    __table_args__ = (
        # "Which households does this user belong to?" -- the primary key has the
        # wrong prefix for that question.
        Index("ix_household_member_user_id", "user_id"),
    )


class UserSession(UuidPkMixin, Base):
    """One signed-in browser, held server-side so that signing out means something.

    **Why a row and not a JWT.** A token this application merely *validates* is a
    token it cannot withdraw: revocation would need a deny-list, which is this
    table with extra steps and a window during which a stolen credential still
    works. Chaudron holds a minor's health data (``household_person``) and
    third-party provider tokens (``llm_provider_config``); "log out everywhere"
    has to cut access at the instant it is asked for, not at expiry.

    **The token is stored hashed, exactly like a password.** ``token_hash`` is a
    SHA-256 of the value in the cookie, so a database leak yields no live
    session. SHA-256 rather than Argon2 *here* and only here: the token is 256
    bits from ``secrets``, so there is no dictionary to slow down, and running a
    64 MiB memory-hard function on every single request would be a denial of
    service this application performs on itself.

    ``csrf_token`` is stored in the clear on purpose. It is not a credential: it
    is worthless without the cookie, which is hashed, and it has to be readable
    to be handed back to the client that must echo it (``api/deps.py``).

    Two deadlines rather than one. ``expires_at`` is the absolute end of the
    session and never moves; ``idle_expires_at`` slides forward with use, so a
    forgotten tab on a shared machine stops working long before the absolute
    limit. A session is live only while both are in the future and ``revoked_at``
    is NULL.
    """

    __tablename__ = "user_session"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("user_account.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment=(
            "SHA-256 of the cookie value, lowercase hex. The plaintext is never "
            "stored: a dump of this table yields no live session."
        ),
    )
    csrf_token: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment=(
            "Echoed by the client in X-CSRF-Token on every unsafe method. Stored "
            "in the clear on purpose: it is worthless without the cookie."
        ),
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    last_used_at: Mapped[datetime] = mapped_column(server_default=func.now())
    expires_at: Mapped[datetime]
    idle_expires_at: Mapped[datetime]
    revoked_at: Mapped[datetime | None]

    __table_args__ = (
        # Unique because it is the lookup key of every authenticated request, and
        # because two sessions sharing a digest would mean the generator broke.
        Index("uq_user_session_token_hash", "token_hash", unique=True),
        # "Revoke everything for this account", and the expiry sweep.
        Index("ix_user_session_user_id", "user_id"),
    )


class MachineTokenScope(enum.StrEnum):
    """What a machine token is allowed to do. Contract v1.1 section 10.

    A closed vocabulary, additive, and never implicit: a token holds the scopes it
    was issued with and nothing follows from holding one of them. There is no
    ``*``, no ``admin``, and no scope that implies another -- ``inventory:write``
    does not grant ``inventory:read``, because a caller that needs both says so
    and a caller that needs one must not silently get the other.

    **Two absences are the design, not an oversight.**

    *No scope reaches recipe suggestions.* It is the one endpoint that spends
    money -- a billed inference per call, or the CPU of a colocated Ollama -- and
    a long-lived credential sitting in a home-automation appliance is the worst
    possible place to hold that power. A stolen token would run up a bill rather
    than read a pantry.

    *No scope reaches household members.* ``household_person`` carries allergens
    and infant age bands: health data under GDPR article 9, and for an infant a
    minor's. It leaves through a browser session belonging to a person, or it does
    not leave.

    Adding a member here is a contract change, and both absences above are the
    two additions that need an argument rather than a commit.
    """

    INVENTORY_READ = "inventory:read"
    INVENTORY_WRITE = "inventory:write"
    SHOPPING_READ = "shopping:read"
    SHOPPING_WRITE = "shopping:write"
    BUDGET_READ = "budget:read"


class MachineToken(UuidPkMixin, HouseholdScopedMixin, Base):
    """A bearer credential for a program: an integration, a script, a dashboard.

    The second door into the same house (contract v1.1 section 10). A browser gets
    a ``__Host-`` cookie and a CSRF token; a machine gets one of these and carries
    it in ``Authorization: Bearer``. The difference is not cosmetic -- an
    ``Authorization`` header is never attached by a browser to a cross-site
    request, so there is no forgery to defend against and no CSRF token to check.

    **Scoped to one household, not to an account.** ``household_id`` is the
    tenant, resolved once when the token is issued and never re-selected by a
    header afterwards. An account that belongs to a family home *and* a flatshare
    issues two tokens: installing an integration for one must not open the other,
    and a token that carried an account would do exactly that.

    ``user_id`` records who issued it, and it is load-bearing rather than
    decorative: the resolver refuses a token whose issuer has since lost their
    membership or had their account disabled (migration ``0011``). That is what
    makes "a member may issue one" safe -- the token grants no more than the
    person behind it still has, and it dies when they do.

    **The token is stored hashed, exactly like a session token.** ``token_hash``
    is a SHA-256 of the value handed out once at creation. SHA-256 rather than
    Argon2, for the reason spelled out on :class:`UserSession`: the value is 256
    bits from ``secrets``, so there is no dictionary to slow down, and a
    memory-hard function on every request would be a denial of service performed
    on ourselves. A dump of this table yields no usable token.

    ``prefix`` and ``last4`` are the only parts that survive the creation
    response, and they exist so an owner can tell two tokens apart in a list. They
    are not a credential and cannot be assembled into one.

    ``expires_at`` is optional -- the contract lets a household choose "no expiry"
    -- and is applied in the resolver's ``WHERE`` clause rather than after the
    lookup, so an expired token and an unknown one are refused by the same branch,
    with the same body, in the same time.
    """

    __tablename__ = "machine_token"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("user_account.id", ondelete="CASCADE"),
        nullable=False,
        comment=(
            "Who issued this token. Checked on every request: the token stops "
            "working when this account loses its membership or is disabled."
        ),
    )
    name: Mapped[str] = mapped_column(
        String(120), comment="Chosen by the person who created it, so a list is readable."
    )
    token_hash: Mapped[str] = mapped_column(
        String(64),
        comment=(
            "SHA-256 of the presented value, lowercase hex. The plaintext is never "
            "stored: a dump of this table yields no usable token."
        ),
    )
    prefix: Mapped[str] = mapped_column(
        String(16),
        comment=(
            "The fixed scheme marker the token carries, stored per row so a list "
            "still renders correctly if the format ever changes."
        ),
    )
    last4: Mapped[str] = mapped_column(
        String(4),
        comment="The last four characters, to tell two tokens apart. Not a credential.",
    )
    scopes: Mapped[list[MachineTokenScope]] = mapped_column(
        ARRAY(pg_enum(MachineTokenScope, "machine_token_scope")),
        comment=(
            "Closed vocabulary, additive, never implicit. A token holds these and "
            "nothing that follows from them."
        ),
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    #: NULL until the token is first presented, which is the answer to "has this
    #: ever been used?" -- the question an owner asks before deleting one.
    last_used_at: Mapped[datetime | None]
    #: NULL means "no expiry", which the contract allows explicitly.
    expires_at: Mapped[datetime | None]
    revoked_at: Mapped[datetime | None]

    __table_args__ = (
        # A token with no scope can do nothing, so a row with no scope is a row
        # that exists only to mislead a reader of the list.
        CheckConstraint("cardinality(scopes) > 0", name="scopes_present"),
        # The lookup key of every token-authenticated request. Not tenant-scoped,
        # and it cannot be: the digest is what *determines* the household, so a
        # composite index on (household_id, token_hash) would have to be probed
        # with the answer it is being asked for.
        Index("uq_machine_token_token_hash", "token_hash", unique=True),
        # "The tokens of this household", which is the whole of `GET /v1/tokens`.
        Index("ix_machine_token_household_id", "household_id"),
    )


class HouseholdPerson(UuidPkMixin, HouseholdScopedMixin, TimestampMixin, Base):
    """Somebody who eats here, and what they can eat. The ``/v1/members`` resource.

    **Why this is not columns on ``HouseholdMember``.** That table is an access
    control row: it says which *account* may open which household, and with what
    role. Three things break if the dietary profile is bolted onto it.

    *A six-month-old has no account.* Extending ``household_member`` makes
    ``user_account`` mandatory for every eater, so cooking for an infant would
    require minting an account -- an email address, a password reset path and a
    login surface -- for a person who cannot consent to any of it. The same
    applies to the grandmother who eats here on Sundays and will never log in.

    *The contract needs one opaque id.* ``GET /v1/members`` returns ``id`` and
    ``PATCH /v1/members/{id}`` addresses it. ``household_member`` is keyed
    ``(household_id, user_id)``, so the only id it could expose is the account
    id -- publishing a person's cross-household identity on a dietary endpoint.

    *The two lifecycles are not the same.* Revoking somebody's access must not
    delete the constraints the household still cooks by; erasing health data must
    not silently revoke access. One row cannot be deleted for one reason and kept
    for the other.

    So: its own identity, its own tenant column, and an *optional* link to an
    account. ``user_account_id`` is a courtesy -- it lets the interface say "this
    is you" -- never an access path.

    **Everything below the name is health data** (GDPR article 9), and for an
    infant it is a minor's. ``allergens`` and ``free_text_restrictions`` are
    ``deferred``, exactly as ``LlmProviderConfig.api_key_ciphertext`` is: a plain
    ``select(HouseholdPerson)`` -- resolving a display name for a suggestion, say
    -- does not load them, and the code that legitimately needs them says so with
    an ``undefer()`` that is visible in review and findable with grep. Deleting
    the row deletes the constraints with it; there is no ``archived_at`` here on
    purpose, because "erased" must mean erased.
    """

    __tablename__ = "household_person"

    # Nullable, and a plain foreign key rather than a composite one: `user_account`
    # is global (a person may belong to two households), so there is no
    # `(household_id, id)` on it to point at. Not an access path -- membership is
    # decided by `household_member`, and nothing reads scope from this column.
    user_account_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user_account.id", ondelete="SET NULL")
    )
    display_name: Mapped[str] = mapped_column(String(120))

    age_band: Mapped[AgeBand] = mapped_column(
        pg_enum(AgeBand, "age_band"),
        server_default=text("'adult'"),
        comment=(
            "HEALTH DATA (GDPR art. 9), and for an infant a minor's. Stored as a "
            "band and never as a date of birth. Never logged, never echoed in an "
            "error message, erased with the row."
        ),
    )
    diet: Mapped[Diet] = mapped_column(
        pg_enum(Diet, "diet"),
        server_default=text("'omnivore'"),
        comment=(
            "SENSITIVE: a diet discloses belief and health. Hard constraint, "
            "applied as a filter on the inventory, never as a prompt instruction."
        ),
    )
    # SECRET-adjacent. Never logged, never returned in an error body, never
    # interpolated into a prompt: the model is told what it may cook *with*, not
    # who it is cooking for. `deferred=True` is the enforcement rather than the
    # documentation -- see the class docstring.
    allergens: Mapped[list[Allergen]] = mapped_column(
        ARRAY(pg_enum(Allergen, "allergen")),
        deferred=True,
        server_default=text("'{}'"),
        comment=(
            "HEALTH DATA (GDPR art. 9): declared allergies. Never logged, never "
            "included in an error message, never sent to a model. Loaded only "
            "through an explicit undefer(), erased with the row."
        ),
    )
    infant_texture: Mapped[InfantTexture | None] = mapped_column(
        pg_enum(InfantTexture, "infant_texture"),
        comment="HEALTH DATA of a minor. Hard constraint; NULL outside an infant band.",
    )
    # Reaches the prompt as a preference, so it goes through the same
    # neutralisation as a catalogue label (infra/untrusted_text.py). It is capped
    # in the database as well, because a bound enforced only by the API is a bound
    # the next writer forgets.
    free_text_restrictions: Mapped[str | None] = mapped_column(
        Text(),
        deferred=True,
        comment=(
            "SENSITIVE free text supplied by the household. Never logged. Passed "
            "to a model as a preference only: it cannot be a filter, because "
            "nothing in the catalogue says which products contain coriander."
        ),
    )
    sort_order: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"))

    __table_args__ = (
        UniqueConstraint("household_id", "id", name="uq_household_person_household_id"),
        # One profile per account per household. Partial, because the people with
        # no account -- the infant, the guest -- are the reason this table exists
        # and NULLs must not collide with each other.
        Index(
            "uq_household_person_user_account",
            "household_id",
            "user_account_id",
            unique=True,
            postgresql_where=text("user_account_id IS NOT NULL"),
        ),
        # The 422 of the contract, expressed where it cannot be forgotten: a
        # texture without an infant band is meaningless, and an infant band
        # without a texture leaves a hard constraint undefined.
        CheckConstraint(
            "(age_band IN ('infant_4_6m', 'infant_6_9m', 'infant_9_12m', 'infant_12_36m'))"
            " = (infant_texture IS NOT NULL)",
            name="infant_texture_band",
        ),
        # A NULL inside the array would compare as unknown in every containment
        # test, which is the one answer a filter on allergies must never give.
        CheckConstraint(
            "array_position(allergens, NULL) IS NULL AND cardinality(allergens) <= 14",
            name="allergens_wellformed",
        ),
        CheckConstraint(
            "free_text_restrictions IS NULL OR char_length(free_text_restrictions) <= 500",
            name="free_text_length",
        ),
    )


# --------------------------------------------------------------------------- #
# Reference data (global, not tenant-scoped)
# --------------------------------------------------------------------------- #


class Unit(Base):
    """Measurement units and their factor to the canonical unit of their dimension.

    A reference table rather than an enum: an enum cannot carry the conversion
    factor, and adding "tablespoon" must not be a type migration. Seeded by an
    Alembic data migration so the reference stays versioned and reproducible.
    """

    __tablename__ = "unit"

    code: Mapped[str] = mapped_column(String(16), primary_key=True)
    dimension: Mapped[QuantityDimension] = mapped_column(
        pg_enum(QuantityDimension, "quantity_dimension")
    )
    factor_to_canonical: Mapped[Decimal] = mapped_column(Numeric(18, 9))
    symbol: Mapped[str] = mapped_column(String(16))
    is_canonical: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    sort_order: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"))

    __table_args__ = (
        # Redundant with the primary key on its own, but it is the target of the
        # composite foreign keys that make ('ml', 'mass') unrepresentable.
        UniqueConstraint("code", "dimension", name="uq_unit_code_dimension"),
        CheckConstraint("factor_to_canonical > 0", name="factor_positive"),
        Index(
            "uq_unit_canonical_per_dimension",
            "dimension",
            unique=True,
            postgresql_where=text("is_canonical"),
        ),
    )


class NutritionReference(Base):
    """One published edition of the nutrition guidance this application quotes.

    It exists so a *version* is a row a foreign key can point at. The weekly
    balance is computed against published benchmarks, and those get revised;
    when they do, a suggestion generated last spring must keep saying which
    edition it was judged by. Without this table the version would be a string
    copied into a column, and a revision would rewrite three months of history
    by making every past shortfall refer to numbers nobody applied at the time.
    """

    __tablename__ = "nutrition_reference"

    version: Mapped[str] = mapped_column(String(32), primary_key=True)
    label: Mapped[str] = mapped_column(String(120))
    published_on: Mapped[date]
    #: The public page a household can open to check what this application claims.
    #: Not decoration: the argument for frequency benchmarks over an opaque score
    #: is that the household can go and read them (ADR-0009).
    source_url: Mapped[str] = mapped_column(Text())
    is_current: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))

    __table_args__ = (
        # At most one current edition, guaranteed by the database rather than by
        # whoever writes the next seed migration.
        Index(
            "uq_nutrition_reference_current",
            text("(true)"),
            unique=True,
            postgresql_where=text("is_current"),
        ),
    )


class PnnsGuideline(Base):
    """One food-group benchmark of one edition: how much, of what, over how long.

    A table rather than constants in code because the version has to travel with
    a persisted suggestion, and because a household that disputes "you are short
    one fish this week" must be able to be shown the row, the wording and the URL
    it came from.
    """

    __tablename__ = "pnns_guideline"

    reference_version: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("nutrition_reference.version", ondelete="RESTRICT"),
        primary_key=True,
    )
    marker: Mapped[PnnsMarker] = mapped_column(pg_enum(PnnsMarker, "pnns_marker"), primary_key=True)
    #: Displayed to the household, therefore French -- it is data, not an identifier.
    label: Mapped[str] = mapped_column(String(80))
    direction: Mapped[PnnsDirection] = mapped_column(pg_enum(PnnsDirection, "pnns_direction"))
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 3))
    unit: Mapped[PnnsUnit] = mapped_column(pg_enum(PnnsUnit, "pnns_unit"))
    window_days: Mapped[int] = mapped_column(SmallInteger)
    #: The official wording, quoted rather than paraphrased. A benchmark
    #: reworded by us stops being checkable against the source we cite.
    statement: Mapped[str] = mapped_column(String(200))
    source_url: Mapped[str] = mapped_column(Text())
    sort_order: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"))

    __table_args__ = (
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint("window_days > 0", name="window_positive"),
    )


class InfantFoodRestriction(Base):
    """Foods withheld from a young child, by age band. A row, never a rule in code.

    This is a safety control, and safety controls belong somewhere a human can
    read them end to end. Scattered across services, "no honey before twelve
    months" becomes an ``if`` that a refactor can drop without any test noticing;
    here it is a row with its age bands, its reason, its official wording and its
    source, and the list can be reviewed by somebody who does not read Python.

    Matching is deliberately dual. ``category_tags`` is the reliable half --
    catalogue taxonomy, exact. ``name_patterns`` is the half that catches the
    manually entered "miel de châtaignier" that carries no category at all. Both
    are inclusive: a false withholding costs a suggestion, a missed one costs a
    hospital visit.
    """

    __tablename__ = "infant_food_restriction"

    reference_version: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("nutrition_reference.version", ondelete="RESTRICT"),
        primary_key=True,
    )
    rule_code: Mapped[str] = mapped_column(String(40), primary_key=True)
    label: Mapped[str] = mapped_column(String(120))
    #: The bands the rule applies to, listed rather than derived from an order on
    #: the enum. "Before twelve months" and "before five years" do not share a
    #: threshold, and inferring one from member order would silently change the
    #: meaning of every row the day somebody inserts a band in the middle.
    applies_to_bands: Mapped[list[AgeBand]] = mapped_column(ARRAY(pg_enum(AgeBand, "age_band")))
    risk: Mapped[InfantRiskKind] = mapped_column(pg_enum(InfantRiskKind, "infant_risk_kind"))
    category_tags: Mapped[list[str]] = mapped_column(ARRAY(Text()), server_default=text("'{}'"))
    name_patterns: Mapped[list[str]] = mapped_column(ARRAY(Text()), server_default=text("'{}'"))
    statement: Mapped[str] = mapped_column(String(300))
    source_url: Mapped[str] = mapped_column(Text())
    sort_order: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"))

    __table_args__ = (
        # A rule that applies to nobody is a rule somebody thinks is protecting them.
        CheckConstraint(
            "cardinality(applies_to_bands) > 0 AND array_position(applies_to_bands, NULL) IS NULL",
            name="bands_present",
        ),
        # A rule that matches nothing is the same trap, one level down.
        CheckConstraint(
            "cardinality(category_tags) > 0 OR cardinality(name_patterns) > 0",
            name="matcher_present",
        ),
    )


class ShelfLifeGuideline(Base):
    """Indicative keeping time of a food family, unopened and once opened.

    Feeds a *pre-filled* date at scan time. Never an assertion: what this table
    produces lands in ``InventoryLot.best_before`` with
    ``expiry_date_source = 'estimated'``, which the interface must render
    differently from a date somebody read off the packaging.

    Unopened and opened are separate columns because they are separate facts by
    an order of magnitude -- a sealed yoghurt and an open one have nothing in
    common -- and because "consume within N days of opening" is the rule the
    application can actually enforce, since it knows ``opened_at``.
    """

    __tablename__ = "shelf_life_guideline"

    family: Mapped[FoodFamily] = mapped_column(pg_enum(FoodFamily, "food_family"), primary_key=True)
    label: Mapped[str] = mapped_column(String(80))
    #: NULL where no honest number exists: dry goods keep for years and the date
    #: on the packet is the only one worth showing.
    unopened_days: Mapped[int | None] = mapped_column(SmallInteger)
    opened_days: Mapped[int | None] = mapped_column(SmallInteger)
    #: Which kind of date the suggestion is. A use-by is a safety limit and a
    #: best-before a quality one; the tone of every notification depends on it,
    #: and guessing it from the family is how dry pasta starts raising alarms.
    date_kind: Mapped[ExpiryDateKind] = mapped_column(pg_enum(ExpiryDateKind, "expiry_date_kind"))
    source_url: Mapped[str] = mapped_column(Text())
    note: Mapped[str | None] = mapped_column(Text())

    __table_args__ = (
        CheckConstraint(
            "(unopened_days IS NULL OR unopened_days > 0)"
            " AND (opened_days IS NULL OR opened_days > 0)",
            name="durations_positive",
        ),
        # A row that suggests nothing at all would be indistinguishable from an
        # unresolved family, which is the state this table exists to contrast with.
        CheckConstraint(
            "unopened_days IS NOT NULL OR opened_days IS NOT NULL",
            name="some_duration",
        ),
        CheckConstraint("date_kind <> 'unknown'", name="date_kind_known"),
    )


class LlmProvider(Base):
    """Supported AI providers and what they require.

    A table, not an enum: adding a provider must be an additive migration, and a
    provider must be switchable off (``is_enabled``) without deleting the household
    configurations that point at it.
    """

    __tablename__ = "llm_provider"

    code: Mapped[str] = mapped_column(String(40), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(80))
    requires_api_key: Mapped[bool] = mapped_column(Boolean)
    requires_base_url: Mapped[bool] = mapped_column(Boolean)
    default_model: Mapped[str | None] = mapped_column(String(120))
    # Defaults only: for Ollama the real capability depends on the pulled model, so
    # the effective values live on the household's configuration row.
    default_supports_vision: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    default_supports_structured_output: Mapped[bool] = mapped_column(
        Boolean, server_default=text("false")
    )
    default_max_context_tokens: Mapped[int | None] = mapped_column(Integer)
    is_enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    sort_order: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"))


class Product(Base):
    """Catalogue entry: *what* an item is, independently of how much is owned."""

    __tablename__ = "product"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid7)
    # NULL means the public, shared catalogue: scanning flour must not create one row
    # per household. NOT NULL isolates "the carrots from the market", which have no
    # barcode and no business being public. Only place in the schema where ownership
    # is optional -- and therefore the only cross-tenant reference the database
    # cannot guard with a composite foreign key (see docs/data-model.md, 5.2).
    household_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("household.id", ondelete="CASCADE")
    )
    gtin: Mapped[str | None] = mapped_column(String(14))
    name: Mapped[str] = mapped_column(Text())
    brand: Mapped[str | None] = mapped_column(Text())
    category_tag: Mapped[str | None] = mapped_column(Text())
    image_url: Mapped[str | None] = mapped_column(Text())

    net_content_value: Mapped[Decimal | None] = mapped_column(QUANTITY)
    net_content_unit_code: Mapped[str | None] = mapped_column(
        String(16), ForeignKey("unit.code", ondelete="RESTRICT")
    )
    # The unit this product is normally counted in, independent of the packaged
    # net content: eggs come in a box of six but are stocked as pieces. Required
    # by `POST /v1/products` (docs/api-contract-v1.md), and it cannot borrow
    # net_content_unit_code, which `ck_product_net_content_pair` forbids setting
    # without a value.
    default_unit_code: Mapped[str | None] = mapped_column(
        String(16), ForeignKey("unit.code", ondelete="RESTRICT")
    )
    # The two scalars that allow crossing dimensions at all. Absent them, "2 onions"
    # and "300 g of onions" simply stay two lines -- which beats inventing a total.
    unit_weight_g: Mapped[Decimal | None] = mapped_column(QUANTITY)
    density_g_per_ml: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    default_shelf_life_days: Mapped[int | None] = mapped_column(SmallInteger)
    # Coarse keeping family, resolved from `category_tag`/`categories_tags` at
    # ingestion. NULL means unresolved, and unresolved means no date is proposed
    # at all -- `default_shelf_life_days` above stays the per-product answer when
    # one is known, this is the fallback for the family, and neither invents a
    # number for a product nobody has categorised.
    food_family: Mapped[FoodFamily | None] = mapped_column(pg_enum(FoodFamily, "food_family"))

    # ---- Allergens -------------------------------------------------------- #
    #
    # Three states, never two, and never conflated (ADR-0009). `allergen_state`
    # says whether anybody declared anything at all; the two lists say what was
    # declared. The tri-state of one allergen on one product is read off the
    # three together, and `allergens_risk` below is the only one of them a filter
    # should ever touch.
    allergen_state: Mapped[AllergenDataState] = mapped_column(
        pg_enum(AllergenDataState, "allergen_data_state"),
        server_default=text("'unknown'"),
        comment=(
            "'unknown' means the upstream record carried no allergen fields. It "
            "never means 'contains no allergen': treat it as 'may contain'."
        ),
    )
    allergens_contains: Mapped[list[Allergen]] = mapped_column(
        ARRAY(pg_enum(Allergen, "allergen")), server_default=text("'{}'")
    )
    allergens_may_contain: Mapped[list[Allergen]] = mapped_column(
        ARRAY(pg_enum(Allergen, "allergen")), server_default=text("'{}'")
    )
    # Generated, and therefore impossible to write, forget or let drift. A
    # product with no allergen data carries **all fourteen** here, so the natural
    # exclusion query -- `NOT (:member_allergens && allergens_risk)` -- withholds
    # it by default. The failure mode of the obvious query is over-exclusion,
    # which costs a suggestion; the failure mode this design removes is
    # under-exclusion, which costs a hospital visit.
    allergens_risk: Mapped[list[Allergen]] = mapped_column(
        ARRAY(pg_enum(Allergen, "allergen")),
        Computed(_ALLERGENS_RISK_SQL, persisted=True),
        comment=(
            "GENERATED. Allergens this product cannot be cleared of, including "
            "every one of them when no data exists. The only column a filter "
            "should read; allergens_contains alone would treat 'unknown' as safe."
        ),
    )

    # PNNS food-group markers resolved from the catalogue categories, so the
    # weekly balance can be computed from stock movements alone. An empty array
    # is "unresolved", and the count of unresolved products is exposed to the
    # household: a badly categorised pantry must produce a visible gap in the
    # evidence, not a confident "you are missing a fish".
    pnns_markers: Mapped[list[PnnsMarker]] = mapped_column(
        ARRAY(pg_enum(PnnsMarker, "pnns_marker")), server_default=text("'{}'")
    )

    source: Mapped[ProductSource] = mapped_column(pg_enum(ProductSource, "product_source"))
    # Raw Open Food Facts response: their schema changes without notice, and keeping
    # it lets us extract a field we had not anticipated without rescanning anything.
    off_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    off_synced_at: Mapped[datetime | None]

    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user_account.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())
    archived_at: Mapped[datetime | None]

    __table_args__ = (
        CheckConstraint("gtin ~ '^[0-9]{8,14}$'", name="gtin_digits"),
        CheckConstraint(
            "(net_content_value IS NULL) = (net_content_unit_code IS NULL)",
            name="net_content_pair",
        ),
        # An allergen is in exactly one state. Declared present and "traces of"
        # at once is not a stricter warning, it is two contradictory facts, and
        # whichever one a reader picks first becomes the answer.
        CheckConstraint(
            "NOT (allergens_contains && allergens_may_contain)",
            name="allergen_states_disjoint",
        ),
        # No data means no declarations. The reverse -- declarations recorded
        # against a product marked as having no data -- would be a row that lies
        # about its own provenance.
        CheckConstraint(
            "allergen_state <> 'unknown'"
            " OR (cardinality(allergens_contains) = 0 AND cardinality(allergens_may_contain) = 0)",
            name="allergen_unknown_is_empty",
        ),
        # A NULL element compares as unknown in every containment test, which is
        # the one answer an allergen filter must never return.
        CheckConstraint(
            "array_position(allergens_contains, NULL) IS NULL"
            " AND array_position(allergens_may_contain, NULL) IS NULL"
            " AND array_position(pnns_markers, NULL) IS NULL",
            name="tag_arrays_wellformed",
        ),
        # One barcode, one public product. Also serves the scan lookup.
        Index(
            "uq_product_gtin_global",
            "gtin",
            unique=True,
            postgresql_where=text("household_id IS NULL AND gtin IS NOT NULL"),
        ),
        Index(
            "uq_product_household_gtin",
            "household_id",
            "gtin",
            unique=True,
            postgresql_where=text("household_id IS NOT NULL AND gtin IS NOT NULL"),
        ),
        # Fuzzy matching of receipt labels against the catalogue. Without it a
        # 30-line receipt is 30 sequential scans. Requires the pg_trgm extension.
        Index(
            "ix_product_name_trgm",
            "name",
            postgresql_using="gin",
            postgresql_ops={"name": "gin_trgm_ops"},
        ),
        # "My own products" in the manual entry screen; partial because most rows are
        # public and have nothing to do in this index.
        Index(
            "ix_product_household_id",
            "household_id",
            postgresql_where=text("household_id IS NOT NULL"),
        ),
        # The allergen filter runs over the whole inventory before every
        # suggestion, and `&&` on an array is only cheap with a GIN index.
        Index("ix_product_allergens_risk", "allergens_risk", postgresql_using="gin"),
        # "What did this household eat from each food group last week", joined
        # from stock movements.
        Index("ix_product_pnns_markers", "pnns_markers", postgresql_using="gin"),
    )


# --------------------------------------------------------------------------- #
# Stock
# --------------------------------------------------------------------------- #


class StorageLocation(UuidPkMixin, HouseholdScopedMixin, Base):
    """Where a lot physically sits. Per-household, because "garage fridge" exists."""

    __tablename__ = "storage_location"

    name: Mapped[str] = mapped_column(String(80))
    kind: Mapped[StorageKind] = mapped_column(pg_enum(StorageKind, "storage_kind"))
    sort_order: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    # Archived rather than deleted: depleted lots still point here.
    archived_at: Mapped[datetime | None]

    __table_args__ = (
        # Target of the composite foreign keys that make it impossible to store a lot
        # in another household's fridge.
        UniqueConstraint("household_id", "id", name="uq_storage_location_household_id"),
        # Two active "Fridge" is a typo; one active and one archived is history.
        Index(
            "uq_storage_location_name",
            "household_id",
            text("lower(name)"),
            unique=True,
            postgresql_where=text("archived_at IS NULL"),
        ),
    )


class InventoryLot(UuidPkMixin, HouseholdScopedMixin, TimestampMixin, Base):
    """A physical batch: *this* bag of flour, bought then, expiring then, stored there.

    "Stock item" and "lot" are two views of the same thing and one table carries
    both: "I have 1.5 kg of flour" is a SUM over active lots, not a stored row. A
    denormalised per-product aggregate would introduce a second truth to reconcile;
    it can come later as a materialised view, driven by a profile rather than a hunch.
    """

    __tablename__ = "inventory_lot"

    product_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("product.id", ondelete="RESTRICT"))
    storage_location_id: Mapped[uuid.UUID | None]

    # Dual storage. The display pair is what the user typed and is never recomputed;
    # the canonical pair is what we sum, compare and index. Conversion happens once,
    # on write, in the application layer -- never on read (the busiest screen would
    # pay a join for nothing) and never in a trigger (invisible, untestable).
    quantity_value: Mapped[Decimal] = mapped_column(QUANTITY)
    quantity_unit_code: Mapped[str] = mapped_column(String(16))
    quantity_dimension: Mapped[QuantityDimension] = mapped_column(
        pg_enum(QuantityDimension, "quantity_dimension")
    )
    quantity_canonical: Mapped[Decimal] = mapped_column(CANONICAL_QUANTITY)
    initial_quantity_canonical: Mapped[Decimal] = mapped_column(CANONICAL_QUANTITY)

    # Nullable on purpose: blocking a scan on a date field loses the user in two
    # weeks. A dateless lot simply never appears in expiry alerts.
    best_before: Mapped[date | None]
    date_kind: Mapped[ExpiryDateKind] = mapped_column(pg_enum(ExpiryDateKind, "expiry_date_kind"))
    # Where that date came from. Nobody types eighteen dates while putting the
    # shopping away, so the application proposes one from `ShelfLifeGuideline`;
    # this column is what keeps the proposal honest afterwards. Without it, an
    # estimate and a date read off the packaging are the same row, and the
    # interface ends up asserting a use-by date the application invented -- on
    # fresh meat, which is where it matters.
    # NOT NULL with a `declared` default, rather than nullable. Every write path
    # that exists today takes the date from a human, so the default is factually
    # right for all of them and no caller has to remember a column; and the value
    # that licenses a hedged rendering is the one nobody gets by accident.
    expiry_date_source: Mapped[ExpiryDateSource] = mapped_column(
        pg_enum(ExpiryDateSource, "expiry_date_source"),
        server_default=text("'declared'"),
    )
    # Feeds the "eat within 3 days of opening" rule. The effective date is computed,
    # never stored: opening a jar would invalidate the stored value.
    opened_at: Mapped[date | None]

    acquired_on: Mapped[date | None]
    unit_price: Mapped[Decimal | None] = mapped_column(MONEY)
    currency: Mapped[str | None] = mapped_column(String(3))

    entry_source: Mapped[StockEntrySource] = mapped_column(
        pg_enum(StockEntrySource, "stock_entry_source")
    )
    # Composite, and the constraint lives in `__table_args__` below: a plain
    # `receipt_line.id` reference is one PostgreSQL will happily satisfy from
    # another household's receipt, because referential-integrity triggers run as
    # the table owner and are not subject to row-level security.
    source_receipt_line_id: Mapped[uuid.UUID | None]
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user_account.id", ondelete="SET NULL")
    )
    note: Mapped[str | None] = mapped_column(Text())
    # Not a deletion: a business state, and the input of every consumption statistic.
    depleted_at: Mapped[datetime | None]

    __table_args__ = (
        UniqueConstraint("household_id", "id", name="uq_inventory_lot_household_id"),
        ForeignKeyConstraint(
            ["household_id", "storage_location_id"],
            ["storage_location.household_id", "storage_location.id"],
            ondelete="RESTRICT",
        ),
        # `ON DELETE SET NULL (source_receipt_line_id)`, and the column list is the
        # whole reason this is spelled out: a bare `SET NULL` on a composite key
        # nulls *every* column of it, and `household_id` is NOT NULL, so deleting a
        # receipt would fail instead of forgetting where the lot came from.
        # PostgreSQL 15 and later.
        ForeignKeyConstraint(
            ["household_id", "source_receipt_line_id"],
            ["receipt_line.household_id", "receipt_line.id"],
            ondelete="SET NULL (source_receipt_line_id)",
        ),
        # Makes ('ml', 'mass') unrepresentable: the denormalised dimension cannot
        # contradict the unit it was derived from.
        ForeignKeyConstraint(
            ["quantity_unit_code", "quantity_dimension"],
            ["unit.code", "unit.dimension"],
            ondelete="RESTRICT",
        ),
        CheckConstraint("quantity_value > 0 AND quantity_canonical >= 0", name="quantity_positive"),
        # An active lot at zero is an inconsistency that silently falsifies the stock.
        CheckConstraint(
            "depleted_at IS NOT NULL OR quantity_canonical > 0",
            name="depleted_consistency",
        ),
        CheckConstraint("(unit_price IS NULL) = (currency IS NULL)", name="price_pair"),
        CheckConstraint("date_kind <> 'unknown' OR best_before IS NULL", name="date_kind_coherent"),
        # A lot with no date cannot be carrying an estimate of one.
        CheckConstraint(
            "best_before IS NOT NULL OR expiry_date_source = 'declared'",
            name="estimate_needs_a_date",
        ),
        # THE merge key. Scanning the same pack twice yields one lot, not two, and the
        # upsert is atomic so two phones scanning at once cannot race.
        # NULLS NOT DISTINCT (PostgreSQL 15+) is mandatory: without it two dateless
        # lots never conflict and the stock fragments on every scan.
        Index(
            "uq_inventory_lot_merge_key",
            "household_id",
            "product_id",
            "storage_location_id",
            "best_before",
            "quantity_dimension",
            unique=True,
            postgresql_where=text("depleted_at IS NULL"),
            postgresql_nulls_not_distinct=True,
        ),
        # Main screen: "show me my fridge". Busiest query of the application.
        Index(
            "ix_inventory_lot_location_active",
            "household_id",
            "storage_location_id",
            postgresql_where=text("depleted_at IS NULL"),
        ),
        # "Expiring soon" widget and the daily notification job.
        Index(
            "ix_inventory_lot_expiry_active",
            "household_id",
            "best_before",
            postgresql_where=text("depleted_at IS NULL AND best_before IS NOT NULL"),
        ),
        # "Do I have flour?" -- once per ingredient during recipe generation.
        Index(
            "ix_inventory_lot_product_active",
            "household_id",
            "product_id",
            postgresql_where=text("depleted_at IS NULL"),
        ),
        # "Delete this receipt and everything it created."
        Index("ix_inventory_lot_source_receipt_line", "source_receipt_line_id"),
    )


class StockMovement(UuidPkMixin, HouseholdScopedMixin, Base):
    """Append-only ledger of every quantity change.

    Justified by three product-level needs an ``UPDATE ... SET quantity = quantity - x``
    destroys: measuring waste, feeding suggestions with real consumption history, and
    offering undo on a one-handed tap in front of an open fridge.

    Invariant: ``InventoryLot.quantity_canonical`` is a cache of ``SUM(delta_canonical)``
    maintained in the same transaction. The ledger is the historical truth, the column
    the read truth. Drift is possible and is handled by a reconciliation job that
    alerts instead of silently correcting.
    """

    __tablename__ = "stock_movement"

    inventory_lot_id: Mapped[uuid.UUID]
    kind: Mapped[StockMovementKind] = mapped_column(
        pg_enum(StockMovementKind, "stock_movement_kind")
    )
    delta_canonical: Mapped[Decimal] = mapped_column(CANONICAL_QUANTITY)
    quantity_dimension: Mapped[QuantityDimension] = mapped_column(
        pg_enum(QuantityDimension, "quantity_dimension")
    )
    occurred_at: Mapped[datetime] = mapped_column(server_default=func.now())
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user_account.id", ondelete="SET NULL")
    )
    # Composite; see `InventoryLot.source_receipt_line_id`.
    recipe_suggestion_id: Mapped[uuid.UUID | None]
    reason: Mapped[str | None] = mapped_column(Text())

    __table_args__ = (
        ForeignKeyConstraint(
            ["household_id", "inventory_lot_id"],
            ["inventory_lot.household_id", "inventory_lot.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["household_id", "recipe_suggestion_id"],
            ["recipe_suggestion.household_id", "recipe_suggestion.id"],
            ondelete="SET NULL (recipe_suggestion_id)",
        ),
        CheckConstraint("delta_canonical <> 0", name="delta_nonzero"),
        # Lot history display and reconciliation.
        Index(
            "ix_stock_movement_lot",
            "inventory_lot_id",
            text("occurred_at DESC"),
        ),
        # "Recent activity" screen and monthly waste statistics.
        Index(
            "ix_stock_movement_household_occurred",
            "household_id",
            text("occurred_at DESC"),
        ),
    )


# --------------------------------------------------------------------------- #
# Shopping
# --------------------------------------------------------------------------- #


class ShoppingList(UuidPkMixin, HouseholdScopedMixin, TimestampMixin, Base):
    """What needs buying. Several coexist; exactly one is the default."""

    __tablename__ = "shopping_list"

    name: Mapped[str] = mapped_column(String(120))
    is_default: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    archived_at: Mapped[datetime | None]

    __table_args__ = (
        UniqueConstraint("household_id", "id", name="uq_shopping_list_household_id"),
        # Guaranteed by the database, not by an application convention that
        # concurrency will eventually work around.
        Index(
            "uq_shopping_list_default",
            "household_id",
            unique=True,
            postgresql_where=text("is_default AND archived_at IS NULL"),
        ),
    )


class ShoppingListItem(UuidPkMixin, HouseholdScopedMixin, Base):
    """A line on a list. Either a catalogue product or free text -- often free text.

    Forcing a catalogue product at add time turns a two-second gesture into a form;
    "some bread" is a legitimate and very common entry.
    """

    __tablename__ = "shopping_list_item"

    shopping_list_id: Mapped[uuid.UUID]
    product_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("product.id", ondelete="SET NULL")
    )
    label: Mapped[str | None] = mapped_column(Text())

    quantity_value: Mapped[Decimal | None] = mapped_column(QUANTITY)
    quantity_unit_code: Mapped[str | None] = mapped_column(String(16))
    quantity_dimension: Mapped[QuantityDimension | None] = mapped_column(
        pg_enum(QuantityDimension, "quantity_dimension")
    )

    origin: Mapped[ShoppingItemOrigin] = mapped_column(
        pg_enum(ShoppingItemOrigin, "shopping_item_origin")
    )
    # Composite; see `InventoryLot.source_receipt_line_id`.
    origin_recipe_suggestion_id: Mapped[uuid.UUID | None]
    sort_order: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    checked_at: Mapped[datetime | None]
    checked_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user_account.id", ondelete="SET NULL")
    )
    added_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user_account.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (
        ForeignKeyConstraint(
            ["household_id", "shopping_list_id"],
            ["shopping_list.household_id", "shopping_list.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["household_id", "origin_recipe_suggestion_id"],
            ["recipe_suggestion.household_id", "recipe_suggestion.id"],
            ondelete="SET NULL (origin_recipe_suggestion_id)",
        ),
        ForeignKeyConstraint(
            ["quantity_unit_code", "quantity_dimension"],
            ["unit.code", "unit.dimension"],
            ondelete="RESTRICT",
        ),
        CheckConstraint("product_id IS NOT NULL OR label IS NOT NULL", name="target_present"),
        CheckConstraint(
            "(quantity_value IS NULL) = (quantity_unit_code IS NULL)"
            " AND (quantity_value IS NULL) = (quantity_dimension IS NULL)",
            name="quantity_triplet",
        ),
        # The only hot query: the list as currently displayed. Checked items stay for
        # history but leave the index.
        Index(
            "ix_shopping_list_item_pending",
            "household_id",
            "shopping_list_id",
            "sort_order",
            postgresql_where=text("checked_at IS NULL"),
        ),
        # "Is this product already on a list?" -- asked on every automatic add.
        Index(
            "ix_shopping_list_item_product",
            "product_id",
            postgresql_where=text("product_id IS NOT NULL"),
        ),
    )


class DeclinedRepurchase(UuidPkMixin, HouseholdScopedMixin, Base):
    """A product this household refused to be offered again when it runs out.

    The row *is* the refusal: its presence means "never propose this again", so
    revoking one is a delete rather than a flag (contract 6bis --
    ``DELETE /v1/shopping-lists/declined/{product_id}``).

    **There is deliberately no expiry column.** A refusal that quietly forgot
    itself after a few months would re-propose exactly what the household waved
    away, and nothing on the screen would explain why it came back. The cost of
    perpetuity is one revocation endpoint; the cost of an expiry is a feature
    that argues with its user.

    No ``updated_at`` either, and none is missing: nothing about a refusal can
    change. ``declined_at`` is when it was taken, and that is the whole record.
    """

    __tablename__ = "declined_repurchase"

    product_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("product.id", ondelete="CASCADE"))
    declined_at: Mapped[datetime] = mapped_column(server_default=func.now())
    # Nullable, and not only because there are no user accounts in this slice: a
    # member can be removed from the household while the household's decision
    # stands. Losing who decided must not lose the decision.
    declined_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user_account.id", ondelete="SET NULL")
    )

    __table_args__ = (
        # One refusal per product, enforced here rather than by a read-then-write
        # in the service: declining twice is what a user does when a proposal is
        # offered on two devices, and it must be idempotent rather than a 500.
        UniqueConstraint(
            "household_id", "product_id", name="uq_declined_repurchase_household_product"
        ),
    )


# --------------------------------------------------------------------------- #
# Receipt import
# --------------------------------------------------------------------------- #


class Receipt(UuidPkMixin, HouseholdScopedMixin, TimestampMixin, Base):
    """A photographed till receipt and its machine interpretation, before validation."""

    __tablename__ = "receipt"

    uploaded_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user_account.id", ondelete="SET NULL")
    )
    # NULL, on every path this application takes, and migration 0012 dropped the
    # NOT NULL to say so. The receipt is read in memory and the bytes are
    # discarded: api contract 6ter refuses to let budget tracking become the
    # pretext for keeping a photograph that carries the shop, the hour, the
    # loyalty number and often four digits of a card. The column survives for a
    # deployment that decides otherwise -- and if one does, the key must be
    # namespaced by household_id in object storage too, because a correctly
    # filtered row protects nothing if the bucket is enumerable.
    image_object_key: Mapped[str | None] = mapped_column(
        Text(),
        comment=(
            "NULL means no image was retained, which is what this application "
            "always writes: the receipt is read in memory and discarded (api "
            "contract 6ter). The column exists for a deployment that decides "
            "otherwise and has a retention policy to go with it."
        ),
    )
    image_sha256: Mapped[str] = mapped_column(String(64))
    status: Mapped[ReceiptStatus] = mapped_column(pg_enum(ReceiptStatus, "receipt_status"))

    merchant_name: Mapped[str | None] = mapped_column(Text())
    purchased_at: Mapped[datetime | None]
    total_amount: Mapped[Decimal | None] = mapped_column(MONEY)
    currency: Mapped[str | None] = mapped_column(String(3))

    # Provenance of the parse. Copied here rather than only referenced, because a
    # configuration can be edited or deleted while the artefact must stay
    # describable: provider_mode is what separates "our default model misread it"
    # from "a local vision-less model was asked to read an image".
    provider_mode: Mapped[LlmProviderMode | None] = mapped_column(
        pg_enum(LlmProviderMode, "llm_provider_mode")
    )
    provider_code: Mapped[str | None] = mapped_column(String(40))
    model: Mapped[str | None] = mapped_column(String(120))
    prompt_version: Mapped[str | None] = mapped_column(String(40))
    llm_provider_config_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("llm_provider_config.id", ondelete="SET NULL")
    )
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    # Micro-units of currency: a single call often costs less than a cent, and
    # rounding to two decimals makes cost tracking useless as soon as it is summed.
    cost_micro: Mapped[int | None] = mapped_column(BigInteger)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    # Kept: when a user reports an invented line, the raw output confronted with the
    # prompt version is the only debuggable artefact of a non-deterministic pipeline.
    raw_response: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    parse_error: Mapped[str | None] = mapped_column(Text())
    parsed_at: Mapped[datetime | None]

    #: Whether the reading was cut short, and what it admitted leaving out.
    #:
    #: Stored rather than recomputed, because the proposal is persisted and re-read
    #: -- see :class:`domain.receipts.ReceiptProposal` on why. A notice that lived
    #: only in the first response would disappear on the reload that precedes every
    #: confirmation, which is the one moment it needed to be on screen.
    #:
    #: Truncation is the more dangerous of the two: the ceiling exists so a hostile
    #: document cannot exhaust the instance, but a legitimate long receipt that
    #: trips it yields a proposal that silently *omits purchases*. Confirming it
    #: writes a stock that is short, with nothing having said so.
    lines_truncated: Mapped[bool] = mapped_column(
        Boolean,
        server_default=text("false"),
        comment=(
            "True when the document held more candidate lines than the ceiling, so "
            "the stored proposal omits purchases. Surfaced at review: confirming a "
            "truncated reading writes a stock that is short, and nothing else on "
            "that screen would say so. Rows written before revision 0019 carry the "
            "default rather than a finding."
        ),
    )
    degradation_notice: Mapped[str | None] = mapped_column(
        Text(),
        comment=(
            "What the reading left out, in French, when a capability was missing "
            "(ADR-0005 'degraded'). NULL means nothing was left out. Stored rather "
            "than recomputed because the proposal is persisted and re-read: a "
            "notice that lived only in the first response would vanish on the "
            "reload that precedes every confirmation."
        ),
    )

    __table_args__ = (
        UniqueConstraint("household_id", "id", name="uq_receipt_household_id"),
        # Blocks the very common double import on mobile (photo re-sent after a
        # perceived timeout). A constraint rather than a check, because the two
        # uploads are concurrent.
        UniqueConstraint("household_id", "image_sha256", name="uq_receipt_household_sha256"),
        Index(
            "ix_receipt_household_purchased",
            "household_id",
            text("purchased_at DESC NULLS LAST"),
        ),
        # Worker queue. Deliberately not prefixed by household_id: it is a
        # cross-tenant queue, and the partial predicate keeps it a few rows long.
        Index(
            "ix_receipt_pending",
            "created_at",
            postgresql_where=text("status IN ('uploaded', 'parsing')"),
        ),
        # Operator billing: the only calls the operator actually pays for.
        Index(
            "ix_receipt_operator_cost",
            "created_at",
            postgresql_where=text("provider_mode = 'instance_owner'"),
        ),
    )


class ReceiptLine(UuidPkMixin, HouseholdScopedMixin, Base):
    """One parsed line of a receipt, and its (fuzzy) match to a catalogue product.

    The link to stock exists in one direction only
    (``InventoryLot.source_receipt_line_id``): reciprocal foreign keys would create a
    cycle and two truths to keep in sync.
    """

    __tablename__ = "receipt_line"

    receipt_id: Mapped[uuid.UUID]
    line_no: Mapped[int] = mapped_column(SmallInteger)
    # Kept even after matching: it is the corpus that will improve matching, and the
    # only evidence of what was printed when a user disputes the result.
    raw_label: Mapped[str] = mapped_column(Text())

    quantity_value: Mapped[Decimal | None] = mapped_column(QUANTITY)
    quantity_unit_code: Mapped[str | None] = mapped_column(String(16))
    quantity_dimension: Mapped[QuantityDimension | None] = mapped_column(
        pg_enum(QuantityDimension, "quantity_dimension")
    )
    unit_price: Mapped[Decimal | None] = mapped_column(MONEY)
    total_price: Mapped[Decimal | None] = mapped_column(MONEY)

    matched_product_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("product.id", ondelete="SET NULL")
    )
    match_confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    match_status: Mapped[ReceiptLineMatchStatus] = mapped_column(
        pg_enum(ReceiptLineMatchStatus, "receipt_line_match_status")
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (
        ForeignKeyConstraint(
            ["household_id", "receipt_id"],
            ["receipt.household_id", "receipt.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["quantity_unit_code", "quantity_dimension"],
            ["unit.code", "unit.dimension"],
            ondelete="RESTRICT",
        ),
        # The target of `inventory_lot`'s composite reference back to this table.
        # A composite foreign key needs a unique constraint on exactly its columns;
        # the primary key on `id` alone will not do.
        UniqueConstraint("household_id", "id", name="uq_receipt_line_household_id"),
        # Receipt order is meaningful, and a re-parse must not duplicate lines.
        UniqueConstraint("receipt_id", "line_no", name="uq_receipt_line_no"),
        CheckConstraint("match_confidence BETWEEN 0 AND 1", name="confidence_range"),
        # "Lines to review" screen -- the main friction point of the receipt flow.
        Index(
            "ix_receipt_line_pending",
            "household_id",
            "created_at",
            postgresql_where=text("match_status IN ('pending', 'suggested')"),
        ),
    )


# --------------------------------------------------------------------------- #
# LLM access (per household -- there is no application-wide API key)
# --------------------------------------------------------------------------- #


class LlmProviderConfig(UuidPkMixin, HouseholdScopedMixin, TimestampMixin, Base):
    """One household's access to a model: mode, endpoint, model, encrypted key.

    Credential and assignment are separate entities on purpose (see
    ``LlmPurposeBinding``): an API key must exist in exactly one place, or the
    household that uses it for two purposes has to rotate it twice -- and the day it
    rotates only one, half the application fails with ``invalid_credentials`` for no
    visible reason.
    """

    __tablename__ = "llm_provider_config"

    label: Mapped[str] = mapped_column(String(80))
    mode: Mapped[LlmProviderMode] = mapped_column(pg_enum(LlmProviderMode, "llm_provider_mode"))
    provider_code: Mapped[str] = mapped_column(
        String(40), ForeignKey("llm_provider.code", ondelete="RESTRICT")
    )
    model: Mapped[str] = mapped_column(String(120))
    base_url: Mapped[str | None] = mapped_column(Text())

    # SECRET. Never expose this column through the API, in any schema, in any form,
    # decrypted or not. `deferred=True` is the enforcement, not the documentation: a
    # plain `select(LlmProviderConfig)` does NOT load it, so reading the ciphertext
    # requires an explicit `undefer()` -- a deliberate, greppable, reviewable gesture.
    # Contents: AES-256-GCM, key from the environment (podman secret) and NEVER from
    # the database, with (household_id, id) as additional authenticated data so a
    # ciphertext copied onto another row fails to decrypt.
    api_key_ciphertext: Mapped[bytes | None] = mapped_column(
        LargeBinary,
        deferred=True,
        comment=(
            "SECRET: AES-256-GCM ciphertext. Encryption key comes from the "
            "environment, never from this database. Never returned by the API; "
            "users only ever see api_key_last4."
        ),
    )
    # The only part a user ever sees again: enough to recognise which of their keys
    # is installed, useless to anyone else.
    api_key_last4: Mapped[str | None] = mapped_column(String(4))
    # Without a key version, rotating the encryption key means re-encrypting
    # everything at once, with the application stopped.
    api_key_encryption_key_id: Mapped[str | None] = mapped_column(String(32))
    api_key_set_at: Mapped[datetime | None]

    # Effective capabilities, seeded from LlmProvider defaults then corrected by a
    # connection probe. No reference table can cover Ollama, where the same endpoint
    # may serve a vision model or not depending on a free-text model name -- only
    # asking the endpoint can tell. This is what lets the UI disable receipt import
    # instead of failing at runtime.
    supports_vision: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    supports_structured_output: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    max_context_tokens: Mapped[int | None] = mapped_column(Integer)

    #: The household's agreement that its data may go to *this* third party, and
    #: the withdrawal of it. Added in revision ``0016``; ``docs/security-model.md``
    #: section 12 had required it since before there was code.
    #:
    #: **Per configuration rather than per household, and that is not a widening.**
    #: A configuration is household-scoped already, so the consent is per household
    #: as the security model asks. Splitting it per configuration is what makes it
    #: *specific* (art. 4(11)): the same document names Mistral (EU) and Ollama
    #: (local) as the two setups with no transfer at all, and shows that as a
    #: selection criterion. Agreeing to an EU processor is not the same act as
    #: agreeing to a Chapter V transfer to the United States, and one flag covering
    #: both would be the blanket consent that reasoning exists to avoid.
    #:
    #: **Nullable, unlike its opposite number on**
    #: :class:`ShoppingExportTarget`. There, a row is born of an agreement, so
    #: ``NOT NULL`` costs nothing. Here rows predate the column, and revision 0016
    #: refused to backfill them: an invented date would not mislabel a record, it
    #: would manufacture the legal basis for a transfer nobody agreed to. ``NULL``
    #: therefore means *no agreement on record*, and the provider load refuses on it
    #: -- fail-closed, with the reason and the remedy in the banner the household
    #: already sees.
    #:
    #: ``mode = 'ollama'`` never needs either: local inference transmits to nobody,
    #: and asking for consent to send data to one's own machine is how consent
    #: screens stop being read. ADR-0007's premise survives intact.
    consented_at: Mapped[datetime | None] = mapped_column(
        comment=(
            "When this household agreed that its data may be sent to this "
            "third-party provider (RGPD art. 6(1)(a), art. 9(2)(a) for the health "
            "signals in a recipe prompt). NULL means no agreement on record: the "
            "provider is refused before any credential is decrypted. Never "
            "backfilled -- an invented date would manufacture the legal basis for a "
            "transfer nobody consented to. Not required for mode 'ollama', which "
            "transmits to nobody."
        )
    )
    #: What withdrawal writes. **The row survives it**, so a household can still
    #: answer "who did you send this to, and when did we agree?" -- and so the
    #: withdrawal itself stays auditable. Read on every provider load, so it takes
    #: effect at the next request rather than at the next cleanup.
    consent_revoked_at: Mapped[datetime | None] = mapped_column(
        comment=(
            "When the household withdrew that agreement. The row survives "
            "withdrawal so the household can still answer 'who did you send this "
            "to, and when did we agree?'. Takes effect at the next request, because "
            "consent is re-read on every provider load."
        )
    )

    status: Mapped[LlmConfigStatus] = mapped_column(pg_enum(LlmConfigStatus, "llm_config_status"))
    last_verified_at: Mapped[datetime | None]
    last_error: Mapped[str | None] = mapped_column(Text())
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user_account.id", ondelete="SET NULL")
    )
    archived_at: Mapped[datetime | None]

    __table_args__ = (
        UniqueConstraint("household_id", "id", name="uq_llm_provider_config_household_id"),
        Index(
            "uq_llm_provider_config_label",
            "household_id",
            text("lower(label)"),
            unique=True,
            postgresql_where=text("archived_at IS NULL"),
        ),
        # A ciphertext without a key id is undecryptable after the first rotation;
        # a last4 without a ciphertext shows the user a key that does not exist.
        CheckConstraint(
            "(api_key_ciphertext IS NULL) = (api_key_last4 IS NULL)"
            " AND (api_key_ciphertext IS NULL)"
            " = (api_key_encryption_key_id IS NULL)",
            name="secret_triplet",
        ),
        # Makes "the instance key is never copied into the database" checkable by the
        # database itself, rather than written in a document nobody will re-read.
        CheckConstraint(
            "(mode = 'byok' AND api_key_ciphertext IS NOT NULL)"
            " OR (mode = 'ollama' AND api_key_ciphertext IS NULL"
            " AND base_url IS NOT NULL)"
            " OR (mode = 'instance_owner' AND api_key_ciphertext IS NULL)",
            name="mode_requirements",
        ),
        CheckConstraint("char_length(api_key_last4) = 4", name="last4_length"),
        # Withdrawing an agreement before it was given is not a state this
        # application can produce; a row in it would make the dates on screen
        # disagree with each other. Same name and same reason as the one on
        # `ShoppingExportTarget` -- but deliberately NOT the same predicate.
        #
        # That table can write `consent_revoked_at >= consented_at` alone because
        # its `consented_at` is NOT NULL. Here the column is nullable, and
        # `<date> >= NULL` is NULL rather than FALSE -- which a CHECK accepts. The
        # verbatim copy would therefore have permitted precisely the row its name
        # forbids: withdrawn, having never agreed. The `IS NOT NULL` conjunct is
        # what makes the constraint mean what it is called.
        CheckConstraint(
            "consent_revoked_at IS NULL"
            " OR (consented_at IS NOT NULL AND consent_revoked_at >= consented_at)",
            name="revocation_follows_consent",
        ),
        # The refused case has to be as cheap as the banner reporting it: every
        # recipe suggestion and every receipt import loads the household's active
        # configurations and now reads these two columns.
        Index(
            "ix_llm_provider_config_unconsented",
            "household_id",
            postgresql_where=text(
                "archived_at IS NULL AND (consented_at IS NULL OR consent_revoked_at IS NOT NULL)"
            ),
        ),
        Index(
            "ix_llm_provider_config_household_active",
            "household_id",
            postgresql_where=text("archived_at IS NULL"),
        ),
        # "Your key no longer works" banner, shown on every page: must be free.
        Index(
            "ix_llm_provider_config_invalid",
            "household_id",
            postgresql_where=text("status = 'invalid_credentials'"),
        ),
    )


class LlmPurposeBinding(Base):
    """Which configuration serves which purpose, for one household.

    Composite primary key ``(household_id, purpose)``: at most one active
    configuration per purpose, with no "is_active" flag to demote and therefore no
    window where two are active at once.

    The composite foreign key below is not hygiene, it is a security control: without
    it a guessed identifier would be enough to spend another household's API credit.
    """

    __tablename__ = "llm_purpose_binding"

    household_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("household.id", ondelete="CASCADE"), primary_key=True
    )
    purpose: Mapped[LlmPurpose] = mapped_column(
        pg_enum(LlmPurpose, "llm_purpose"), primary_key=True
    )
    llm_provider_config_id: Mapped[uuid.UUID]
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        ForeignKeyConstraint(
            ["household_id", "llm_provider_config_id"],
            ["llm_provider_config.household_id", "llm_provider_config.id"],
            ondelete="CASCADE",
        ),
    )


# --------------------------------------------------------------------------- #
# Recipe generation
# --------------------------------------------------------------------------- #


class RecipeSuggestion(UuidPkMixin, HouseholdScopedMixin, TimestampMixin, Base):
    """A model-generated recipe, plus the full trace of how it was produced."""

    __tablename__ = "recipe_suggestion"

    requested_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user_account.id", ondelete="SET NULL")
    )
    title: Mapped[str] = mapped_column(Text())
    summary: Mapped[str | None] = mapped_column(Text())
    servings: Mapped[int | None] = mapped_column(SmallInteger)
    prep_minutes: Mapped[int | None] = mapped_column(SmallInteger)
    cook_minutes: Mapped[int | None] = mapped_column(SmallInteger)

    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    # What we sent to the model. Without it, "why did it suggest that when I had no
    # eggs?" is unanswerable. Also the most sensitive row in the database: it is a
    # complete inventory of a home, and falls under the same retention policy as
    # receipt images.
    stock_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)

    # Copied from the configuration rather than only referenced: the configuration
    # can change or be deleted, while a three-month-old suggestion must keep saying
    # what produced it. A poor suggestion from a small local model is a support case;
    # the same from the instance default is a product regression. Two different
    # queues, and nothing else in the schema separates them.
    provider_mode: Mapped[LlmProviderMode] = mapped_column(
        pg_enum(LlmProviderMode, "llm_provider_mode")
    )
    provider_code: Mapped[str] = mapped_column(String(40))
    model: Mapped[str] = mapped_column(String(120))
    prompt_version: Mapped[str] = mapped_column(String(40))
    llm_provider_config_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("llm_provider_config.id", ondelete="SET NULL")
    )

    input_tokens: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    output_tokens: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    cached_input_tokens: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    # Only aggregable for the operator when provider_mode = 'instance_owner'; in byok
    # it is the user's own spend at their own pricing, and in ollama it is zero.
    cost_micro: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    # Detects truncation, which otherwise looks like a badly written recipe.
    finish_reason: Mapped[str | None] = mapped_column(String(40))

    status: Mapped[RecipeStatus] = mapped_column(pg_enum(RecipeStatus, "recipe_status"))
    rating: Mapped[int | None] = mapped_column(SmallInteger)
    cooked_at: Mapped[datetime | None]

    # ---- Usage feedback --------------------------------------------------- #
    #
    # The only place in the product that measures whether the suggestions are any
    # good. Everything else -- token counts, latency, cost -- says what a call
    # consumed, never whether it was worth making. Without these three columns
    # "the small local model degrades gracefully" (ADR-0007) stays an assertion
    # nobody can check, because there is no number to group by provider_mode.
    #
    # **Cardinality: one opinion per suggestion, and it may change.** Columns
    # rather than a child table, deliberately.
    #
    # *Can an opinion change?* Yes -- dismissed on Tuesday, cooked on Sunday --
    # so the column is updatable and the latest answer wins. A ledger of
    # successive opinions would be a table nobody queries: the ranking wants the
    # current verdict, not its history.
    #
    # *Can the same suggestion be cooked twice?* Yes, and that multiplicity is
    # already recorded, in the right place: every cooking deducts stock and
    # leaves a dated ``stock_movement`` carrying ``recipe_suggestion_id``. A
    # second table would count the same events again and eventually disagree
    # with the ledger. So: how many times it was cooked is a ``count(*)`` on
    # movements; whether the household liked it is here, once.
    feedback: Mapped[RecipeFeedback | None] = mapped_column(
        pg_enum(RecipeFeedback, "recipe_feedback")
    )
    feedback_at: Mapped[datetime | None]
    feedback_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user_account.id", ondelete="SET NULL")
    )

    __table_args__ = (
        UniqueConstraint("household_id", "id", name="uq_recipe_suggestion_household_id"),
        CheckConstraint("rating BETWEEN 1 AND 5", name="rating_range"),
        CheckConstraint("(feedback IS NULL) = (feedback_at IS NULL)", name="feedback_dated"),
        # `status` is the lifecycle and `feedback` the measurement, and they are
        # the same fact seen twice. Rather than pick one and lose either the
        # existing state machine or the groupable signal, the database refuses to
        # let them disagree -- so a quality report and a screen can never tell
        # two different stories about the same row.
        CheckConstraint(
            "feedback IS NULL"
            " OR (feedback = 'cooked' AND status = 'cooked' AND cooked_at IS NOT NULL)"
            " OR (feedback = 'not_interested' AND status = 'discarded')",
            name="feedback_matches_status",
        ),
        CheckConstraint(
            "input_tokens >= 0 AND output_tokens >= 0"
            " AND cached_input_tokens >= 0 AND cost_micro >= 0",
            name="tokens_nonneg",
        ),
        Index(
            "ix_recipe_suggestion_household_created",
            "household_id",
            text("created_at DESC"),
        ),
        # Operator cost report. The partial predicate is not an optimisation, it is
        # the business definition of the index: byok and ollama calls are paid by the
        # household and have no place in an operator invoice.
        Index(
            "ix_recipe_suggestion_operator_cost",
            "created_at",
            "model",
            postgresql_where=text("provider_mode = 'instance_owner'"),
        ),
        # "Does the cheap local model produce recipes people actually cook?" --
        # the only query that turns ADR-0007's graceful-degradation claim into a
        # number. Cross-tenant like the two above, and for the same reason: it
        # answers an operator's question, not a household's. The partial
        # predicate keeps it to the rows somebody bothered to answer about.
        Index(
            "ix_recipe_suggestion_feedback",
            "provider_mode",
            "model",
            "feedback",
            postgresql_where=text("feedback IS NOT NULL"),
        ),
    )


class RecipeSuggestionIngredient(UuidPkMixin, HouseholdScopedMixin, Base):
    """The resolved projection of a recipe's ingredients.

    Exists because two flows need it: "add what is missing to the shopping list" and
    "I cooked this, deduct it from stock". Both require a fuzzy label-to-product
    resolution we do not want to redo on every render. The raw ingredients also stay
    in ``RecipeSuggestion.payload``; this table is the usable projection, not the
    source of truth.
    """

    __tablename__ = "recipe_suggestion_ingredient"

    recipe_suggestion_id: Mapped[uuid.UUID]
    position: Mapped[int] = mapped_column(SmallInteger)
    raw_label: Mapped[str] = mapped_column(Text())

    quantity_value: Mapped[Decimal | None] = mapped_column(QUANTITY)
    quantity_unit_code: Mapped[str | None] = mapped_column(String(16))
    quantity_dimension: Mapped[QuantityDimension | None] = mapped_column(
        pg_enum(QuantityDimension, "quantity_dimension")
    )

    product_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("product.id", ondelete="SET NULL")
    )
    # Snapshot taken at generation time, not a live view of the stock.
    availability: Mapped[IngredientAvailability] = mapped_column(
        pg_enum(IngredientAvailability, "ingredient_availability")
    )
    is_optional: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))

    __table_args__ = (
        ForeignKeyConstraint(
            ["household_id", "recipe_suggestion_id"],
            ["recipe_suggestion.household_id", "recipe_suggestion.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["quantity_unit_code", "quantity_dimension"],
            ["unit.code", "unit.dimension"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "recipe_suggestion_id",
            "position",
            name="uq_recipe_suggestion_ingredient_position",
        ),
        # "Add the missing ingredients to the list" in a single query.
        Index(
            "ix_recipe_ingredient_missing",
            "household_id",
            "product_id",
            postgresql_where=text("availability IN ('missing', 'partial')"),
        ),
    )


# --------------------------------------------------------------------------- #
# Shopping budget (api contract 6ter)
# --------------------------------------------------------------------------- #


class BudgetPeriod(enum.StrEnum):
    """The two calendar windows a target can be set over.

    Calendar, never rolling: households reason in salary months, and a sliding
    window makes "what have I spent this month" incomparable from one day to the
    next (contract 6ter).
    """

    WEEK = "week"
    MONTH = "month"


class BudgetTarget(UuidPkMixin, HouseholdScopedMixin, TimestampMixin, Base):
    """An optional spending target, at most one per household and period.

    Optional is the design, not a shortcut: a target triggers a *display* and
    nothing else -- no alert, no block, no judgement. Overspending on food is a
    fact, not a fault, and an application that scolds gets uninstalled
    (contract 6ter).

    There is deliberately no history table. A target is a current intention the
    household restates when it changes; keeping every past value would turn an
    optional convenience into a record of how a family's means moved over time,
    which ``docs/security-model.md`` classifies alongside the inventory as an A3
    asset and which nothing in the product needs.
    """

    __tablename__ = "budget_target"

    period: Mapped[BudgetPeriod] = mapped_column(pg_enum(BudgetPeriod, "budget_period"))
    amount: Mapped[Decimal] = mapped_column(MONEY)
    # Never converted, anywhere. A target in EUR is compared with spending in EUR
    # and with nothing else; a conversion would need a rate, a rate date, and a
    # decision that does not belong to this application (contract 6ter).
    currency: Mapped[str] = mapped_column(String(3))

    __table_args__ = (
        # One target per period, so ``PUT`` is a replacement rather than an
        # accumulation the interface would have to pick a winner from.
        UniqueConstraint("household_id", "period", name="uq_budget_target_household_period"),
        CheckConstraint("amount > 0", name="amount_positive"),
        # ISO 4217 is three uppercase letters. Storing 'eur' and 'EUR' as two
        # currencies would split one household's spending in half.
        CheckConstraint(
            "currency ~ '^[A-Z]{3}$'",
            name="currency_iso_4217",
        ),
    )


# --------------------------------------------------------------------------- #
# Shopping-list export destinations (ADR-0010)
# --------------------------------------------------------------------------- #


class ShoppingExportTarget(UuidPkMixin, HouseholdScopedMixin, TimestampMixin, Base):
    """Where one household has agreed its shopping list may be sent, and on what.

    Per household, because a destination is *data about the household* rather
    than a deployment constant: the token belongs to a person and files items
    into their account. The instance-owned destination read from the environment
    (``infra/todo/settings.py``) stays as the special case ADR-0010 always meant
    it to be -- one operator, their own household, their own token.

    **``consented_at`` is ``NOT NULL``, and that is a legal requirement rather
    than an engineering one.** A shopping list says what identifiable people eat,
    which in a household with an allergy or an observance is health or religious
    data (RGPD art. 9). Sending it to a third party needs an agreement that was
    actually given, is dated, and can be withdrawn. A nullable column would let a
    row exist without one, and the first ``INSERT`` that forgot it would be a
    breach rather than a bug.

    ``consent_revoked_at`` is what withdrawal writes. **The row survives it**, so
    the household can still see what it once authorised and when -- an erasure
    that also erased the record of the authorisation would leave nobody able to
    answer "who did you send this to?". Withdrawal takes effect at the next send
    rather than at the next cleanup, because
    :meth:`chaudron.infra.todo.store.StoredExportTarget.is_consented` is read on
    every export.
    """

    __tablename__ = "shopping_export_target"

    #: The stable code of the destination (``todoist``). Text rather than an enum:
    #: the set of destinations is decided by which adapters this build ships
    #: (``infra/todo/factory.py``), and a native enum would make adding one a
    #: migration -- with a ``DROP TYPE`` to remember on the way back down.
    target_code: Mapped[str] = mapped_column(Text())

    # SECRET. Never expose this column through the API, in any schema, in any
    # form, decrypted or not. `deferred=True` is the enforcement, not the
    # documentation: a plain `select(ShoppingExportTarget)` does NOT load it, so
    # reading the ciphertext requires an explicit `undefer()` -- a deliberate,
    # greppable, reviewable gesture. Contents: AES-256-GCM, key from the
    # environment (podman secret) and NEVER from the database, with
    # (EXPORT_TOKEN_DOMAIN, household_id, id) as additional authenticated data.
    token_ciphertext: Mapped[bytes] = mapped_column(
        LargeBinary,
        deferred=True,
        comment=(
            "SECRET: AES-256-GCM ciphertext of a third party's personal token. "
            "Encryption key comes from the environment, never from this database. "
            "Never returned by the API; users only ever see token_last4."
        ),
    )
    #: The only part a user ever sees again: enough to recognise which of their
    #: tokens is installed, useless to anyone else.
    token_last4: Mapped[str] = mapped_column(String(4))
    #: Without a key version, rotating the master key means re-encrypting
    #: everything at once, with the application stopped.
    token_encryption_key_id: Mapped[str] = mapped_column(String(32))

    #: The container items land in -- a Todoist project id. ``NULL`` means the
    #: account's own inbox, which is what ``item_add`` does with no ``project_id``
    #: and where a household that never chose a project expects to find its list.
    external_list_id: Mapped[str | None] = mapped_column(Text())

    consented_at: Mapped[datetime]
    consent_revoked_at: Mapped[datetime | None]

    #: Who pasted the token and ticked the box. Recorded since migration ``0014``,
    #: and it closes a gap that was not a detail: the row said a household had
    #: agreed and never said *who agreed on its behalf*, so an owner reading the
    #: settings screen saw four characters of somebody's credential and had no way
    #: to tell whose (audit AUD-027). Registering is owner-only now, but the
    #: question survives the answer -- a household has several owners over its
    #: life, and "which of them" is what makes the consent auditable.
    #:
    #: **Nullable, and the nullability carries meaning.** ``NULL`` is a row
    #: registered before this column existed: the registrant is genuinely unknown
    #: and cannot be invented. It is *not* the same state as a registrant who has
    #: since left, which is a known identity with no membership -- and which stops
    #: the export (``infra/todo/factory.py``). Backfilling a plausible-looking
    #: owner would have made an unaudited consent look audited.
    #:
    #: ``ON DELETE SET NULL`` rather than ``CASCADE``: erasing an account must not
    #: silently erase the household's agreement along with it. The dated consent
    #: is what the household answers "who did you send this to?" with.
    registered_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user_account.id", ondelete="SET NULL"),
        comment=(
            "Who registered this destination and granted the consent. NULL means a "
            "row that predates migration 0014, not an anonymous agreement."
        ),
    )

    __table_args__ = (
        # One destination of a given kind per household. Two rows for `todoist`
        # would make "which token exports this list?" a question with two answers
        # and no tie-breaker.
        UniqueConstraint(
            "household_id", "target_code", name="uq_shopping_export_target_household_code"
        ),
        # The composite key every tenant table carries, so anything referencing a
        # destination later can do so through a household-anchored foreign key
        # rather than a bare id (data-model.md section 5.2).
        UniqueConstraint("household_id", "id", name="uq_shopping_export_target_household_id"),
        # A last4 that is not four characters shows the user a fragment of a key
        # that is not theirs to recognise.
        CheckConstraint("char_length(token_last4) = 4", name="last4_length"),
        # Withdrawing an agreement before it was given is not a state this
        # application can produce; a row in it would make `is_consented` and the
        # dates on screen disagree.
        CheckConstraint(
            "consent_revoked_at IS NULL OR consent_revoked_at >= consented_at",
            name="revocation_follows_consent",
        ),
    )


# --------------------------------------------------------------------------- #
# Rate limiting
# --------------------------------------------------------------------------- #


class RateLimitBucket(Base):
    """One token bucket, shared by every worker of every replica.

    ``api/throttling.py`` keeps the in-process limiters and says what they are
    *for*; this table is the shared state its own docstring has been asking for
    since it was written, because per-process counters mean two workers grant two
    budgets -- including on the login and machine-token limiters, where that is the
    difference between a rate-limited password spray and an unlimited one.

    **No ``household_id``, and the absence is the design.** Every other business
    table carries the tenant (ADR-0006) and is under row-level security. The keys
    here include client addresses seen *before* any household is known and
    normalised e-mail addresses of accounts that may not exist, so a tenant column
    would be null on exactly the rows that matter most. Declared in the tenancy
    guard's ``GLOBAL_TABLES`` with that reason rather than left to be rediscovered.

    Written on its own connection, outside the request transaction -- see
    ``infra/rate_limits.py``. A count that rolls back with the request counts only
    the attempts that succeeded.
    """

    __tablename__ = "rate_limit_bucket"

    #: Which limiter. Part of the key rather than a table each, because eight
    #: tables differing only in a name is eight schemas to keep in step.
    scope: Mapped[str] = mapped_column(
        String(48),
        primary_key=True,
        comment="Which limiter this bucket belongs to; see infra/rate_limits.py.",
    )
    #: That limiter's unit of account. 320 characters is the longest addressable
    #: e-mail, which is the widest of the three kinds of key.
    bucket_key: Mapped[str] = mapped_column(
        String(320),
        primary_key=True,
        comment=(
            "That limiter's unit of account: a household id, a client address, or a "
            "normalised e-mail. 320 characters is the longest addressable e-mail, "
            "which is the widest of the three."
        ),
    )
    #: Fractional, and it has to be. The limiter is a *continuous* token bucket
    #: rather than a fixed window, precisely so that a caller cannot spend one
    #: budget at the end of a window and the next at the start of the following
    #: one. An integer column would have restored that behaviour in silence.
    tokens: Mapped[float] = mapped_column(
        Float,
        comment=(
            "Fractional on purpose: this is a continuous token bucket, and an "
            "integer column would silently make it the fixed window the in-memory "
            "limiter was written to avoid."
        ),
    )
    #: Server time, always: refill is measured by PostgreSQL, because two workers
    #: have two clocks and a bucket refilled against the last to arrive grants more
    #: than it says.
    updated_at: Mapped[datetime] = mapped_column(
        comment=(
            "Server time, always. Refill is computed from the interval PostgreSQL "
            "measures, because two workers have two clocks and a bucket refilled "
            "against the last one to arrive grants more than it says."
        )
    )

    __table_args__ = (
        # A bucket below zero would mean a refusal that still charged, and the
        # retry-after computed from it would be a lie in the caller's favour.
        CheckConstraint("tokens >= 0.0", name="tokens_not_negative"),
        # The sweep deletes by age across every scope at once; lookups go through
        # the primary key.
        Index("ix_rate_limit_bucket_updated_at", "updated_at"),
        {
            "comment": (
                "Shared token buckets for the API rate limiters. Deliberately carries "
                "no household_id: its keys include client addresses seen before any "
                "household is known. Written outside the request transaction so a "
                "rollback cannot erase an attempt that was made."
            )
        },
    )
