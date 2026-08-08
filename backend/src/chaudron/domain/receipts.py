"""Till receipts and drive order recaps: the vocabulary of reading one in.

Contract 6ter and 7. This module holds what the receipt import exchanges and
nothing that knows how to fetch, parse or store it -- the same split as
``domain/shopping.py``, and for the same reason: the rules below are decisions,
and a decision that lives inside a handler is a decision nobody can find again.

Three of them are the feature.

**The total is one number and the lines are thirty.**
``docs/technical-notes-ingestion.md`` section 3.4 measures, over 10 656 real
receipts, that vision models *fabricate or alter lines to force their sum onto
the printed total*: the line-item field peaks at 0.49 F1 while every other field
clears 0.85. So :attr:`ReceiptProposal.total_amount` is what the budget rests on
(``services/budget.py`` refuses to sum the lines), :attr:`ReceiptProposal.line_sum`
is computed **only** to be shown next to it, and the difference is never
reconciled. A receipt whose arithmetic falls out even is not evidence of
correctness; it is sometimes the symptom of the opposite, and quietly adjusting a
line to close the gap would destroy the only signal we have that a line was
invented.

**Nothing reaches the inventory without a human.** A model reading ``PDT NOUV
1KG`` is right about half the time, and a silently wrong stock is worse than an
empty one. The proposal is persisted so it survives a reload, but every line
arrives at ``pending`` or ``suggested`` and only :class:`ConfirmReceiptLine`
writes a lot. There is no confidence threshold above which the review is skipped:
ADR-0005 warns that trusting a frontier model and distrusting a local one is
exactly the shortcut the arithmetic laundering makes dangerous.

**A miss beats a wrong match.** Label reconciliation goes through
``domain/labels`` -- 195 entries, precision 0.946, **recall 0.412**. The low
recall is the accepted half of that trade: a label nobody could expand is shown
as the text it is, with no product attached and no claim that one was found.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

__all__ = [
    "ConfirmReceiptLine",
    "ConfirmedReceipt",
    "ProposedReceiptLine",
    "ReceiptAlreadyConfirmedError",
    "ReceiptImportError",
    "ReceiptImportUnavailableError",
    "ReceiptNotFoundError",
    "ReceiptProposal",
    "ReceiptReadBy",
    "ReceiptSource",
    "ReceiptSummary",
    "SameReceiptTwiceError",
]

#: What was uploaded. ``photo`` is a picture of a paper till receipt, ``pdf`` the
#: order recap a drive sends by mail. They are shown differently because they
#: promise differently: one went through a vision model, the other did not.
ReceiptSource = Literal["photo", "pdf"]

#: How the lines were obtained. ``text`` means the PDF carried selectable text
#: and no model was called at all -- no token spent, and a transcription that
#: cannot hallucinate because nothing generated it. ``model`` means a vision
#: model read an image. The interface says which, because the expected accuracy
#: is not the same and the user is entitled to know which one they got.
ReceiptReadBy = Literal["text", "model"]


# --------------------------------------------------------------------------- #
# Failures
# --------------------------------------------------------------------------- #


class ReceiptImportError(Exception):
    """Base of the failures the receipt import can name.

    Deliberately not a :class:`chaudron.domain.ports.DomainError`, on the same
    grounds as :class:`chaudron.domain.shopping.ShoppingImportError`: that
    hierarchy has one translation table mapping anything unrecognised onto
    ``400``, and these owe the caller a status they can act on.
    ``routers/receipts.py`` maps each one explicitly.
    """


class ReceiptNotFoundError(ReceiptImportError):
    """No receipt with this identifier belongs to this household. Answered ``404``.

    One branch for "does not exist" and "belongs to someone else", so the
    endpoint cannot be used to discover that a receipt exists.
    """


class ReceiptAlreadyConfirmedError(ReceiptImportError):
    """The lines of this receipt are already in stock. Answered ``409``.

    Confirming twice would double the inventory, and the second call is what a
    phone does when the first response was lost rather than what a user meant.
    """


class SameReceiptTwiceError(ReceiptImportError):
    """These exact bytes were already imported by this household. Answered ``409``.

    ``uq_receipt_household_sha256`` is the enforcement; this is the sentence. The
    case is the ordinary one on mobile -- a photo re-sent after a perceived
    timeout -- so the answer carries the existing receipt rather than only
    refusing, and the client can open the review it already has.
    """

    def __init__(self, receipt_id: uuid.UUID) -> None:
        super().__init__(f"receipt {receipt_id} already holds these bytes")
        self.receipt_id = receipt_id


class ReceiptImportUnavailableError(ReceiptImportError):
    """The feature is switched off for this household, with the reason. Answered ``409``.

    The ``unavailable`` case of the ADR-0005 taxonomy, and the reason it exists
    as its own type: a model that cannot see the image would happily produce a
    plausible receipt, so the button is disabled *with an explanation* rather
    than allowed to fail at the moment of the click. :attr:`reason` and
    :attr:`remedy` are French: they are interface, not diagnostics.
    """

    def __init__(self, *, reason: str, remedy: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.remedy = remedy


# --------------------------------------------------------------------------- #
# The proposal
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ProposedReceiptLine:
    """One line as it was read, and what -- if anything -- it was matched to.

    :attr:`raw_label` is what the receipt printed and is never overwritten: it is
    the corpus that will improve matching, and the only evidence of what was on
    the paper when a household disputes the result.
    :attr:`label` is the readable form -- the abbreviation expansion when
    ``domain/labels`` produced exactly one complete, confident reading, the raw
    text otherwise. The two being equal is the normal outcome, not a failure:
    recall is 0.412 by measurement.

    :attr:`suggested_label` is the third state, and it exists so that "I am not
    sure" does not have to mean "I have nothing to offer". When the lexicon
    produced a reading it declined to trust -- ambiguous, low-confidence, or with
    a token it could not expand -- the raw label is shown *and* the reading
    travels here, so the review screen can offer it as one tap rather than
    asserting it or discarding it.
    """

    id: uuid.UUID
    line_no: int
    raw_label: str
    label: str
    suggested_label: str | None
    quantity: Decimal | None
    unit_code: str | None
    unit_price: Decimal | None
    total_price: Decimal | None
    matched_product_id: uuid.UUID | None
    #: ``pending`` -- nothing was matched; ``suggested`` -- a product is proposed
    #: and a human still has to accept it. Never ``confirmed`` before the confirm
    #: call, whatever the confidence.
    match_status: Literal["pending", "suggested", "confirmed", "rejected", "ignored"]
    #: Between ``0`` and ``1``, and only ever a claim about the *label expansion*: no model
    #: here reports a calibrated confidence, and inventing one would be worse
    #: than the blank (``docs/technical-notes-ingestion.md`` section 3.5).
    match_confidence: Decimal | None


@dataclass(frozen=True, slots=True)
class ReceiptProposal:
    """A stored, unconfirmed reading of one receipt.

    Persisted -- unlike the shopping-list proposal, which is recomputed on every
    call. Two reasons, both about what this document is. A photographed receipt
    costs a model call to read, so losing the reading to a reload means spending
    the household's money again; and contract 6ter makes the receipt count
    towards the budget as soon as it carries a total and a currency, which is a
    fact about money that cannot live in a response body.

    The corollary is :meth:`services.receipts.ReceiptImportService.discard`: a
    proposal that counts towards the budget must be deletable, or an abandoned
    import inflates the spend forever with no way out.
    """

    id: uuid.UUID
    source: ReceiptSource
    read_by: ReceiptReadBy
    status: Literal["parsed", "confirmed", "failed"]
    merchant: str | None
    #: The retailer slug ``domain/labels`` scoped its lexicon to, when the
    #: merchant was recognised. ``None`` is common and harmless: the lexicon then
    #: uses only its chain-independent entries.
    retailer: str | None
    purchased_at: dt.datetime | None
    total_amount: Decimal | None
    currency: str | None
    lines: tuple[ProposedReceiptLine, ...]
    #: Sum of the priced lines, or ``None`` when at least one line has no price.
    #: Strict on purpose: a partial sum can never disprove a total, and reporting
    #: a gap on the strength of one would cry wolf on every half-read receipt.
    line_sum: Decimal | None
    #: ``True`` when the document held more candidate lines than the ceiling.
    truncated: bool
    #: What the answer left out, in French, when a capability was missing
    #: (ADR-0005 ``degraded``). ``None`` when nothing was left out.
    degradation_notice: str | None = None

    @property
    def line_sum_delta(self) -> Decimal | None:
        """``total_amount - line_sum``, or ``None`` when either is unknown.

        Shown at review, never acted on. It is the best indicator available that
        a line was invented, and the one number on this screen that a silent
        correction would erase.
        """
        if self.total_amount is None or self.line_sum is None:
            return None
        return self.total_amount - self.line_sum


@dataclass(frozen=True, slots=True)
class ReceiptSummary:
    """One row of "receipts waiting to be reviewed"."""

    id: uuid.UUID
    source: ReceiptSource
    status: Literal["parsed", "confirmed", "failed"]
    merchant: str | None
    purchased_at: dt.datetime | None
    total_amount: Decimal | None
    currency: str | None
    line_count: int
    created_at: dt.datetime


# --------------------------------------------------------------------------- #
# Confirmation -- the only call that writes stock
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ConfirmReceiptLine:
    """One reviewed line, as the human left it.

    Addressed by :attr:`id` because the proposal *is* stored: the client returns
    the lines it was given, corrected, and the server re-reads every value from
    this body rather than from what it parsed. A client is not a source of truth
    about a household's stock, but it is the only source of truth about what the
    human decided.

    Exactly one of :attr:`product_id` and :attr:`label` carries the target, the
    way ``ConfirmLine`` does for the shopping list: sending both would store a
    catalogue name and a free text that disagree.
    """

    id: uuid.UUID
    product_id: uuid.UUID | None = None
    label: str | None = None
    amount: Decimal | None = None
    unit_code: str | None = None


@dataclass(frozen=True, slots=True)
class ConfirmedReceipt:
    """What the write actually did. Counts, never a promise."""

    receipt_id: uuid.UUID
    created_lot_count: int
    #: Lines the reviewer removed. They keep their price -- "do not put this in
    #: my cupboard" is not "this was not on the receipt" -- so the line sum shown
    #: next to the total stays the sum of what was printed.
    ignored_line_count: int
    created_product_count: int
