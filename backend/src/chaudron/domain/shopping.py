"""What a shopping-list import is, before anything is written.

The whole feature is shaped by one sentence of the contract (section 7.2): the
import endpoint returns a *proposal*, and a second call carrying the reviewed
lines is the only one that writes. Everything in this module therefore describes
something the user has not yet agreed to, which is why none of it is a database
row.

Three properties are worth stating here rather than discovering in the service:

**The proposal is not stored.** There is no table for it and deliberately so. A
shopping list says what a household is about to eat; keeping a copy of every
abandoned import would be a retention decision nobody asked for. ``import_id`` is
an opaque correlation token that appears in the response, in the log line and in
the confirm URL -- the server never looks it up, because there is nothing to look
it up in. The consequence is explicit: the confirm call is authoritative on its
own body, and the client is responsible for sending what the user approved.

**A model is optional and never load-bearing.** :class:`ShoppingLineSplitter` is
a second pass over the lines the deterministic parser could not split. When no
implementation is available -- a household with no API key, an instance with no
Ollama, which ADR-0007 treats as a normal state rather than an outage -- the
proposal is simply the deterministic one, and the lines it could not read are
marked for review. This is the "emulation with documented loss" case of ADR-0005,
not "explicitly unavailable".

**Dietary constraints signal, they never filter** (contract 7.4). A household may
legitimately buy what one of its members cannot eat. :class:`AllergenScreen`
therefore returns *flags to attach*, and there is no method on it that could
remove a line -- the rule is enforced by the shape of the port, not by a comment
asking the next author to remember it.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, Protocol, TypeIs, runtime_checkable

from chaudron.domain.models import QuantityDimension

__all__ = [
    "DEPLETION_REASONS",
    "AllergenScreen",
    "ConfirmLine",
    "ConfirmedImport",
    "DeclinedRepurchaseStore",
    "DepletionEvent",
    "DepletionProposal",
    "DepletionReason",
    "DocumentTooLargeError",
    "DocumentUnreadableError",
    "ImportProposal",
    "ImportSource",
    "ItemSource",
    "NewShoppingItem",
    "ParseConfidence",
    "ParsedBy",
    "ParsedQuantity",
    "ProposedLine",
    "ShoppingImportError",
    "ShoppingItemNotFoundError",
    "ShoppingItemView",
    "ShoppingLineSplitter",
    "ShoppingListNotFoundError",
    "ShoppingListView",
    "SplitCandidate",
    "UnsupportedDocumentError",
    "is_depletion_reason",
]

#: Where the text came from. ``text`` is a pasted body, ``txt`` an uploaded plain
#: text file, ``pdf`` an uploaded PDF. The interface shows it, because "we read
#: your PDF" and "we read what you pasted" are different promises.
ImportSource = Literal["pdf", "txt", "text"]

#: Whether a model touched the list (contract 7.2). Two values only: the contract
#: asks the interface to be able to say "a model was involved", not to describe
#: which lines it saw.
ParsedBy = Literal["deterministic", "deterministic+model"]

#: How much the parser is willing to claim about a line.
#:
#: * ``high``   -- a quantity was read *and* the label matched a catalogue product;
#: * ``medium`` -- a quantity was read, or a catalogue product matched, not both;
#: * ``low``    -- neither, but the line reads like a plain product label;
#: * ``none``   -- the line was not interpreted at all. These are the lines a model
#:   would be asked about, and the ones counted in ``unparsed_line_count``.
ParseConfidence = Literal["high", "medium", "low", "none"]


# --------------------------------------------------------------------------- #
# Failures
# --------------------------------------------------------------------------- #


class ShoppingImportError(Exception):
    """Base of the failures a document can cause.

    Deliberately *not* a :class:`chaudron.domain.ports.DomainError`: that type has
    a single translation table in ``api/errors.py`` mapping anything unrecognised
    onto ``400``, and a document over the size bound owes the caller a ``413``
    quoting the bound it passed. ``routers/shopping.py`` maps these explicitly,
    the way ``routers/recipes.py`` owns the provider mapping.
    """


class DocumentTooLargeError(ShoppingImportError):
    """A bound was passed. Answered ``413``, never a partial read (contract 7.3)."""

    def __init__(self, *, measure: str, limit: int, detail: str) -> None:
        super().__init__(detail)
        self.measure = measure
        self.limit = limit
        self.detail = detail


class UnsupportedDocumentError(ShoppingImportError):
    """A media type this endpoint does not read."""


class ShoppingListNotFoundError(ShoppingImportError):
    """A list identifier naming nothing this household owns. Answered ``404``."""


class DocumentUnreadableError(ShoppingImportError):
    """The bytes are not a document this endpoint can extract text from.

    Carries no library message: a parser's exception text quotes offsets and
    object numbers from a file a stranger supplied, and echoing it back is how a
    format parser becomes an oracle.
    """


# --------------------------------------------------------------------------- #
# The proposal
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ParsedQuantity:
    """An amount, and the ``unit`` row it resolved to.

    ``unit_code`` and ``dimension`` travel together because
    ``shopping_list_item`` references ``(code, dimension)`` as a composite foreign
    key: writing a dimension that is not the one that unit really has is
    unrepresentable in the schema, so it must not be representable here either.
    """

    amount: Decimal
    unit_code: str
    dimension: QuantityDimension


@dataclass(frozen=True, slots=True)
class ProposedLine:
    """One line of the proposal. Nothing here has been written.

    ``needs_review`` is set by the service, not derived by the reader, and the
    rule is: the parser could not interpret the line, **or** a model interpreted
    it, **or** the line carries a flag. A model-split line is always reviewed --
    it is model output over text a stranger supplied, and that is exactly the
    combination AUD-006 is about.
    """

    raw: str
    quantity: ParsedQuantity | None
    product_name: str
    matched_product_id: uuid.UUID | None
    confidence: ParseConfidence
    needs_review: bool
    #: Signals attached to the line, never reasons to drop it (contract 7.4).
    flags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ImportProposal:
    """The whole answer of ``POST /v1/shopping-lists/import``."""

    import_id: uuid.UUID
    source: ImportSource
    parsed_by: ParsedBy
    lines: tuple[ProposedLine, ...]
    unparsed_line_count: int
    #: ``True`` when the document held more candidate lines than the configured
    #: ceiling. The extra lines are dropped, and saying so is the point: the
    #: alternative is a silently short list.
    truncated: bool


# --------------------------------------------------------------------------- #
# Confirmation -- the only call that writes
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ConfirmLine:
    """A line the user kept, after correcting it."""

    product_id: uuid.UUID | None
    label: str | None
    amount: Decimal | None
    unit_code: str | None


@dataclass(frozen=True, slots=True)
class ConfirmedImport:
    """What was written, so the client can navigate to it."""

    shopping_list_id: uuid.UUID
    shopping_list_name: str
    created_item_count: int


# --------------------------------------------------------------------------- #
# Optional collaborators
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class SplitCandidate:
    """A model's reading of one line, before any of it is believed.

    Every field is a string because none of it is trusted: ``amount`` is validated
    as a positive bounded decimal here, ``unit_code`` is resolved against the
    ``unit`` table, and ``raw`` must be one of the lines that were actually sent.
    A candidate whose ``raw`` was never submitted is dropped -- a splitter cannot
    add items to somebody's shopping list.
    """

    raw: str
    product_name: str
    amount: str | None = None
    unit_code: str | None = None


@runtime_checkable
class ShoppingLineSplitter(Protocol):
    """Second pass over the lines the deterministic parser could not split.

    The port lives in the domain and takes plain strings in both directions: no
    prompt, no message, no token crosses this boundary (ADR-0005). Its absence is
    a supported configuration, so every caller treats it as optional.

    Implementations receive text a stranger wrote. The service sanitises it
    through :mod:`chaudron.infra.untrusted_text` before it is handed over; an
    implementation must not undo that by re-reading the original document.
    """

    async def split(self, lines: Sequence[str]) -> Sequence[SplitCandidate]: ...


@runtime_checkable
class AllergenScreen(Protocol):
    """Attach a flag to a line matching a member's declared allergen.

    Returns one tuple of flags per submitted label, positionally. There is no
    method here that returns a subset of the labels, and that is the design: a
    household may buy what one of its members cannot eat, so the contract (7.4)
    says such a line is *signalled* to the review and never removed. A port that
    cannot express "drop this line" cannot be misused into dropping one.
    """

    async def flags_for(
        self, household_id: uuid.UUID, labels: Sequence[str]
    ) -> Sequence[tuple[str, ...]]: ...


# --------------------------------------------------------------------------- #
# Depletion -> repurchase proposal (contract 6bis)
# --------------------------------------------------------------------------- #

#: A removal reason that means "there is none left". The type exists so that a
#: proposal cannot be *built* carrying ``correction``: the rule below is enforced
#: once, at the boundary, and everything downstream is safe by construction
#: rather than by review.
DepletionReason = Literal["consumed", "wasted"]

#: The removal reasons that mean "there is none left", as opposed to "the number
#: was wrong". ``correction`` is absent and that absence is the whole rule: a
#: household that fixes a mistyped 500 g into 0 has not finished the product, and
#: proposing a repurchase would fill the list with typing errors (contract 6bis).
#: Kept as a frozenset rather than an ``if reason != "correction"`` so that a
#: reason added later has to be classified deliberately instead of defaulting to
#: "propose".
DEPLETION_REASONS: frozenset[DepletionReason] = frozenset({"consumed", "wasted"})


def is_depletion_reason(reason: str) -> TypeIs[DepletionReason]:
    """Whether *reason* means the product ran out, narrowing the type as it says so.

    The single gate. A caller that skips it cannot construct a
    :class:`DepletionProposal`, because the field will not accept a plain
    ``str`` -- which is how "``correction`` never proposes" stops being a rule
    somebody has to remember.
    """
    return reason in DEPLETION_REASONS


@dataclass(frozen=True, slots=True)
class DepletionEvent:
    """A stock line that just reached zero, and why."""

    product_id: uuid.UUID
    reason: str


@dataclass(frozen=True, slots=True)
class DepletionProposal:
    """What the client may offer to add -- and never what it adds by itself.

    The type carries no method that writes. Adding the product goes through the
    ordinary shopping-list write path (contract 6bis), which is a separate,
    explicit call the user makes: "propose, never do" is enforced by there being
    nothing here to call.
    """

    product_id: uuid.UUID
    product_name: str
    reason: DepletionReason
    already_on_list: bool
    previously_declined: bool


@runtime_checkable
class DeclinedRepurchaseStore(Protocol):
    """Remembers that a household waved a repurchase proposal away.

    Contract 6bis requires the refusal to survive: a product dismissed once must
    not be offered again every time it is finished. That is per-household durable
    state with no expiry by design, so it is a table --
    :class:`chaudron.infra.repositories.SqlDeclinedRepurchaseStore` over
    ``declined_repurchase`` (migration ``0007``).

    A port rather than a direct call because the depletion path treats it as
    optional: a :class:`DepletionService` built without one reports
    ``previously_declined: false``, which is what a store remembering nothing
    would say, and the tests build it that way to isolate the reason rule.
    Declining, unlike reading, is never optional -- the endpoints hold a real
    store or the refusal would be accepted and lost.
    """

    async def declined_products(
        self, household_id: uuid.UUID, product_ids: Sequence[uuid.UUID]
    ) -> frozenset[uuid.UUID]: ...

    async def decline(self, household_id: uuid.UUID, product_ids: Sequence[uuid.UUID]) -> None: ...

    async def undecline(self, household_id: uuid.UUID, product_id: uuid.UUID) -> None: ...


# --------------------------------------------------------------------------- #
# The shopping list itself (contract 6bis, "le chemin d'écriture")
# --------------------------------------------------------------------------- #

#: Why an item is on the list. Wider than the persisted enum on purpose: ``source``
#: exists so that "does the repurchase proposal actually get used?" can be
#: measured rather than assumed.
ItemSource = Literal["manual", "depleted", "import"]


@dataclass(frozen=True, slots=True)
class NewShoppingItem:
    """One item to add. Either a catalogue product or free text, never neither."""

    product_id: uuid.UUID | None = None
    free_text: str | None = None
    amount: Decimal | None = None
    unit_code: str | None = None
    source: ItemSource = "manual"


@dataclass(frozen=True, slots=True)
class ShoppingItemView:
    id: uuid.UUID
    product_id: uuid.UUID | None
    product_name: str | None
    free_text: str | None
    quantity: ParsedQuantity | None
    source: ItemSource
    checked: bool
    sort_order: int


@dataclass(frozen=True, slots=True)
class ShoppingListView:
    id: uuid.UUID
    name: str
    items: tuple[ShoppingItemView, ...]


class ShoppingItemNotFoundError(ShoppingImportError):
    """An item identifier naming nothing on this household's list. ``404``."""
