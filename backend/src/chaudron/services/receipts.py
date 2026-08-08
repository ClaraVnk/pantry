"""Reading a till receipt or a drive recap in, and letting a human accept it.

Contract 6ter and 7. Two entrances, one review, one write -- the shape of
``services/shopping_import.py``, with three differences that are all consequences
of what a receipt is.

**The proposal is stored.** The shopping-list import recomputes its proposal on
every call because reading a text file is free. Reading a photograph is not: it
is a model call the household pays for, and losing it to a page reload would
charge them twice. It is also money, and contract 6ter makes a receipt count
towards the budget as soon as it carries a total and a currency -- which cannot
live in a response body. The corollary is :meth:`ReceiptImportService.discard`:
anything that counts towards a budget without being confirmed must be deletable,
or an abandoned import inflates the spend forever.

**The PDF path never calls a model.** A drive recap carries selectable text.
``services/receipt_text.py`` reads it for the price of one ``pypdf`` call, and
the result is *more* faithful than a transcription because nothing generated it.
The vision model is what the photograph path needs and what the PDF path
therefore does not.

**The image is not kept.** ``docs/api-contract-v1.1-dietary.md`` section 6ter:
"il n'y a donc aucune raison de conserver l'image du ticket après extraction du
total et des lignes, et le suivi de budget ne doit pas devenir le prétexte à la
garder." A receipt carries the chain, the date, the hour, the amount and
sometimes four digits of a card; ``docs/security-model.md`` classes the resulting
history alongside the inventory as asset A3. So ``receipt.image_object_key`` is
written ``NULL`` on every path, always, and migration ``0012`` made the column
nullable for exactly that. Nothing this application writes -- no row, no object
store, no log line -- ever receives the bytes.

The one place they do touch a disk, said plainly because "read in memory" would
not be true: Starlette spools a multipart part to a temporary file once it passes
1 MiB (``starlette/formparsers.py``), and the receipt ceiling is 6 MiB, so an
ordinary phone photograph takes that path rather than the exceptional one. The
file is created in ``TMPDIR`` and **unlinked immediately** -- it has no name for
any other process to open, and it is released when the handler closes the upload
-- but on a default deployment ``TMPDIR`` is ``/tmp`` on the root filesystem,
which is persistent storage rather than a tmpfs. Blocks holding a receipt
therefore exist on that disk for the length of one request. Pointing ``TMPDIR``
at a tmpfs closes it, and that is an operations decision (``ops/``) rather than a
line of code here.

What *is* kept of the image is its SHA-256, and it earns its place: it is the
duplicate guard (``uq_receipt_household_sha256``) for the most ordinary mobile
failure there is, a photo re-sent after a perceived timeout. A hash of bytes
nobody stored cannot reconstruct them, and it retains nothing a household did not
already tell us by importing the receipt.

Matching, and the failure mode that was chosen
----------------------------------------------

Labels go through ``domain/labels`` -- 195 entries, precision 0.946, **recall
0.412** -- and then through *exact normalised equality* against the catalogue,
the same blunt rule as the shopping-list import and for the same reason
(``docs/technical-notes-ingestion.md``, decision 9). No trigram, no embedding, no
Open Food Facts search: a miss costs the reviewer one tap on a screen they are
already looking at, and a wrong match puts the wrong food in a cupboard where
somebody with an allergy will find it. The 0.588 of labels that resolve to
nothing are shown as the text they are, with no product attached and no claim
that one was found.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final, Literal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from chaudron.domain.labels import Confidence, expand_label
from chaudron.domain.llm_ports import (
    LlmError,
    ParsedReceipt,
    ProviderCapabilityUnavailable,
    ProviderNotConfigured,
)
from chaudron.domain.models import (
    ExpiryDateKind,
    Household,
    LlmProviderMode,
    Product,
    ProductSource,
    Receipt,
    ReceiptLine,
    ReceiptLineMatchStatus,
    ReceiptStatus,
    StockEntrySource,
)
from chaudron.domain.ports import (
    ProductDraft,
    ProductNotFoundError,
    ProductRepository,
    UnknownUnitError,
)
from chaudron.domain.receipts import (
    ConfirmedReceipt,
    ConfirmReceiptLine,
    ProposedReceiptLine,
    ReceiptAlreadyConfirmedError,
    ReceiptImportUnavailableError,
    ReceiptNotFoundError,
    ReceiptProposal,
    ReceiptReadBy,
    ReceiptSource,
    ReceiptSummary,
    SameReceiptTwiceError,
)
from chaudron.domain.shopping import DocumentUnreadableError
from chaudron.infra.documents import extract_pdf_text_isolated, sniff_image
from chaudron.infra.untrusted_text import sanitize, sanitize_optional
from chaudron.services.budget import resolve_zone
from chaudron.services.inventory import AddItemCommand, InventoryService
from chaudron.services.providers import ProviderService
from chaudron.services.receipt_text import (
    MAX_RAW_LABEL,
    looks_like_receipt_text,
    parse_receipt_text,
    retailer_slug,
)
from chaudron.services.shopping_import import UnitLexicon

logger = logging.getLogger(__name__)

__all__ = [
    "MAX_CONFIRM_LINES",
    "MAX_MERCHANT_LENGTH",
    "ReceiptBounds",
    "ReceiptImportService",
]

#: ``receipt.merchant_name`` is ``text``; this is a readability bound and the one
#: that keeps a merchant name a model invented from becoming a paragraph.
MAX_MERCHANT_LENGTH: Final = 80

#: Lines accepted by the confirm endpoint. A receipt with more lines than this is
#: a supermarket's monthly statement, not a shop.
MAX_CONFIRM_LINES: Final = 300

#: ``receipt.currency`` is ``char(3)`` and the value is an ISO 4217 code. Anything
#: else -- a symbol, a word, whatever a model felt like -- is dropped rather than
#: truncated: contract 6ter counts a total with no currency as *missing*, which is
#: strictly better than counting it against the wrong one.
_CURRENCY_LENGTH: Final = 3

#: numeric(4,3) on ``receipt_line.match_confidence``, and constrained to 0..1.
#: These are the values the label expansion's three confidence levels map onto.
#: They describe *the abbreviation reading*, never the model: no provider in
#: ADR-0005 exposes logprobs, and a fabricated score would look authoritative
#: (``docs/technical-notes-ingestion.md`` section 3.5).
_EXPANSION_CONFIDENCE: Final[dict[Confidence, Decimal]] = {
    Confidence.HIGH: Decimal("0.900"),
    Confidence.MEDIUM: Decimal("0.700"),
    Confidence.LOW: Decimal("0.400"),
}

#: The stored enum, mapped onto the contract's vocabulary. See :func:`_match_status`.
_MATCH_STATUS: Final[
    dict[
        ReceiptLineMatchStatus, Literal["pending", "suggested", "confirmed", "rejected", "ignored"]
    ]
] = {
    ReceiptLineMatchStatus.PENDING: "pending",
    ReceiptLineMatchStatus.SUGGESTED: "suggested",
    ReceiptLineMatchStatus.CONFIRMED: "confirmed",
    ReceiptLineMatchStatus.REJECTED: "rejected",
    ReceiptLineMatchStatus.IGNORED: "ignored",
}

_CENTS: Final = Decimal("0.01")


@dataclass(frozen=True, slots=True)
class ReceiptBounds:
    """The configured ceilings, passed in rather than read from the environment."""

    max_bytes: int
    max_pdf_pages: int
    max_text_chars: int
    max_lines: int


@dataclass(frozen=True, slots=True)
class _Reading:
    """What one of the two entrances produced, before anything is written."""

    parsed: ParsedReceipt
    source: ReceiptSource
    read_by: ReceiptReadBy
    truncated: bool


class ReceiptImportService:
    """Turn an upload into a stored proposal, and a reviewed proposal into stock.

    Takes the session directly, like :class:`ShoppingImportService`: one writer,
    a handful of reads of reference tables, no second consumer that would justify
    a repository. Never commits -- the request's transaction is opened and closed
    in ``infra/db.py``.

    :class:`InventoryService` is injected rather than reimplemented. Writing a lot
    means converting to the canonical unit, honouring the merge key and recording
    a stock movement, and a second implementation of those three would be a second
    set of rules to keep in step with the first.
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        bounds: ReceiptBounds,
        inventory: InventoryService,
        products: ProductRepository,
        providers: ProviderService | None = None,
    ) -> None:
        self._session = session
        self._bounds = bounds
        self._inventory = inventory
        self._products = products
        self._providers = providers

    # -- Reading in --------------------------------------------------------- #

    async def propose(
        self,
        household_id: uuid.UUID,
        *,
        data: bytes,
        media_type: str,
        reads_as_pdf: bool,
        uploaded_by_user_id: uuid.UUID | None = None,
    ) -> ReceiptProposal:
        """Read an upload and store the proposal. Writes no stock.

        ``reads_as_pdf`` is decided by the router from the media type and the file
        extension, and it only chooses which reader runs: both readers re-check
        the bytes themselves, so a ``.pdf`` full of JPEG still fails on the magic
        number rather than deep inside a parser.
        """
        digest = hashlib.sha256(data).hexdigest()
        existing = await self._session.scalar(
            select(Receipt.id).where(
                Receipt.household_id == household_id, Receipt.image_sha256 == digest
            )
        )
        if existing is not None:
            # The mobile double-submit. Refusing with the identifier lets the
            # client open the review it already has instead of asking the user to
            # photograph the receipt again.
            raise SameReceiptTwiceError(existing)

        reading = (
            await self._read_pdf(data)
            if reads_as_pdf
            else await self._read_photo(household_id, data, media_type)
        )
        receipt = await self._store(
            household_id,
            reading,
            digest=digest,
            uploaded_by_user_id=uploaded_by_user_id,
        )
        logger.info(
            "receipt_import_proposed",
            extra={
                "receipt_id": str(receipt.id),
                "source": reading.source,
                "read_by": reading.read_by,
                "line_count": len(reading.parsed.lines),
                "has_total": reading.parsed.total is not None,
                # Counts and flags only. A receipt says where a household shops,
                # when, and for how much; a log file is the one place the image
                # this endpoint promised not to keep would quietly survive in
                # transcribed form.
            },
        )
        return await self._proposal(household_id, receipt.id)

    async def _read_pdf(self, data: bytes) -> _Reading:
        """The drive recap. Text first, and no model at all when it works.

        A PDF with no extractable text is a scan, and the honest answer is to say
        so rather than to fall back on vision. Falling back would mean *rendering*
        the document to pixels, which is the one operation ``infra/documents``
        exists to refuse (a renderer is a large parser fed by a stranger, and
        PyMuPDF was already rejected on that ground and on its licence). The user
        is told to photograph the receipt instead, which routes them to the path
        that does read images -- one sentence, one tap, no new attack surface.

        Awaited rather than called, and in a bounded worker process. The same
        synchronous parse on the shopping-list path was measured freezing every
        other request in the instance for 48 seconds, and this path takes a
        *six*-megabyte ceiling rather than one -- the same 693:1 expansion applied
        to a 6.4 MB upload reached 4.43 GB.
        """
        text = await extract_pdf_text_isolated(
            data,
            max_pages=self._bounds.max_pdf_pages,
            max_chars=self._bounds.max_text_chars,
        )
        if not looks_like_receipt_text(text):
            raise DocumentUnreadableError(
                "this PDF carries no selectable text, so it is a scan rather than an "
                "order recap. Photograph the receipt instead, or export the recap "
                "again from your account."
            )
        parsed, truncated = parse_receipt_text(text, max_lines=self._bounds.max_lines)
        return _Reading(parsed=parsed, source="pdf", read_by="text", truncated=truncated)

    async def _read_photo(
        self, household_id: uuid.UUID, data: bytes, declared_type: str
    ) -> _Reading:
        """The photographed receipt. Needs a model, and needs it to have eyes.

        ``declared_type`` is not believed: :func:`sniff_image` returns what the
        bytes actually are, and that is what reaches the provider. A file named
        ``.png``, typed ``image/png`` and holding a JPEG would otherwise be sent
        with a media type its content contradicts, which some providers reject
        and others silently mis-decode.
        """
        media_type = sniff_image(data, max_bytes=self._bounds.max_bytes)
        if self._providers is None:  # pragma: no cover - wired everywhere it is used
            raise ReceiptImportUnavailableError(
                reason=(
                    "Cette instance n'a aucun fournisseur de modèle : la photo d'un "
                    "ticket ne peut pas être lue."
                ),
            )
        try:
            active = await self._providers.for_receipts(household_id)
        except ProviderCapabilityUnavailable as error:
            raise ReceiptImportUnavailableError(
                reason=(
                    "Le modèle configuré pour ce foyer ne sait pas lire les images : "
                    "l'import de tickets par photo est désactivé."
                ),
                remedy=error.remedy,
            ) from None
        except ProviderNotConfigured:
            raise ReceiptImportUnavailableError(
                reason=(
                    "Aucun fournisseur de modèle utilisable n'est configuré pour ce "
                    "foyer : la photo d'un ticket ne peut pas être lue."
                ),
                remedy=(
                    "Enregistrez un fournisseur multimodal dans la configuration du "
                    "foyer, ou importez le PDF de votre commande drive, qui se lit "
                    "sans modèle."
                ),
            ) from None

        parsed = await active.parser.parse(data, media_type)
        lines = parsed.lines[: self._bounds.max_lines]
        return _Reading(
            parsed=ParsedReceipt(
                lines=lines,
                merchant=parsed.merchant,
                purchased_on=parsed.purchased_on,
                currency=parsed.currency,
                total=parsed.total,
                degradation_notice=parsed.degradation_notice,
            ),
            source="photo",
            read_by="model",
            truncated=len(parsed.lines) > len(lines),
        )

    # -- Storing the proposal ----------------------------------------------- #

    async def _store(
        self,
        household_id: uuid.UUID,
        reading: _Reading,
        *,
        digest: str,
        uploaded_by_user_id: uuid.UUID | None,
    ) -> Receipt:
        """Persist the receipt and its lines at ``parsed``, matching as it goes."""
        parsed = reading.parsed
        merchant = sanitize_optional(parsed.merchant, limit=MAX_MERCHANT_LENGTH)
        retailer = retailer_slug(merchant)
        zone = resolve_zone(await self._timezone(household_id))

        receipt = Receipt(
            household_id=household_id,
            uploaded_by_user_id=uploaded_by_user_id,
            # NULL, on every path and by design. See the module docstring: the
            # image is not stored anywhere, so there is no key to point at, and a
            # placeholder string here would be a lie that outlives this comment.
            image_object_key=None,
            image_sha256=digest,
            status=ReceiptStatus.PARSED,
            merchant_name=merchant,
            purchased_at=(
                None
                if parsed.purchased_on is None
                else dt.datetime.combine(parsed.purchased_on, dt.time(12), tzinfo=zone)
            ),
            total_amount=_money(parsed.total),
            currency=_currency(parsed.currency),
            # Both computed by the reader and, until revision `0019`, both dropped
            # here -- so `truncated` was reported as False and `degradation_notice`
            # as null on every response the API ever sent. The proposal is re-read
            # from this row before it is confirmed, which is the moment a household
            # needs to be told that the reading omits purchases.
            lines_truncated=reading.truncated,
            degradation_notice=parsed.degradation_notice,
            parsed_at=dt.datetime.now(dt.UTC),
        )
        if reading.read_by == "model":
            receipt.provider_mode, receipt.provider_code, receipt.model = await self._provenance(
                household_id
            )
        self._session.add(receipt)
        try:
            await self._session.flush()
        except IntegrityError:
            # Two uploads of the same photograph racing each other. The unique
            # constraint is the arbiter; this turns its violation into the same
            # answer the pre-check gives, rather than a 500.
            raise SameReceiptTwiceError(receipt.id) from None

        lexicon = await UnitLexicon.load(self._session)
        labels = [sanitize(line.label, limit=MAX_RAW_LABEL) for line in parsed.lines]
        readings = [expand_label(label, retailer=retailer) if label else None for label in labels]
        display = [_readable(raw, retailer=retailer)[0] for raw in labels]
        matches = await self._match_products(household_id, display)

        for index, (line, raw_label) in enumerate(zip(parsed.lines, labels, strict=True)):
            if not raw_label:
                continue
            expansion = readings[index]
            unit = lexicon.get(line.unit) if line.unit else None
            amount = _quantity(line.quantity)
            if amount is None or unit is None:
                # A half-quantity is no quantity: the schema stores value, code
                # and dimension together or not at all, and a number with no unit
                # is exactly what the reviewer is best placed to complete.
                amount, unit = None, None
            matched = matches.get(index)
            self._session.add(
                ReceiptLine(
                    household_id=household_id,
                    receipt_id=receipt.id,
                    line_no=index + 1,
                    raw_label=raw_label,
                    quantity_value=amount,
                    quantity_unit_code=None if unit is None else unit.code,
                    quantity_dimension=None if unit is None else unit.dimension,
                    unit_price=_money(line.unit_price),
                    total_price=_money(line.total_price),
                    matched_product_id=matched,
                    match_confidence=(
                        None
                        if matched is None or expansion is None or expansion.best is None
                        else _EXPANSION_CONFIDENCE.get(expansion.best.confidence)
                    ),
                    match_status=(
                        ReceiptLineMatchStatus.SUGGESTED
                        if matched is not None
                        else ReceiptLineMatchStatus.PENDING
                    ),
                )
            )
        await self._session.flush()
        return receipt

    async def _provenance(
        self, household_id: uuid.UUID
    ) -> tuple[LlmProviderMode | None, str | None, str | None]:
        """Which model read this receipt, copied onto the row rather than referenced.

        A configuration can be edited or deleted while the artefact has to stay
        describable: ``provider_mode`` is what separates "our default model misread
        it" from "a local vision model was asked to read a crumpled photograph".
        Best-effort -- a failure here must not lose a receipt that was read.
        """
        if self._providers is None:  # pragma: no cover - wired in production
            return None, None, None
        try:
            active = await self._providers.for_receipts(household_id)
        except LlmError:  # pragma: no cover - the call above already succeeded
            return None, None, None
        return active.config.mode, active.config.provider_code, active.config.model

    async def _match_products(
        self, household_id: uuid.UUID, labels: Sequence[str]
    ) -> dict[int, uuid.UUID]:
        """Exact normalised equality against the visible catalogue, and no more.

        One query for the whole receipt rather than one per line -- the ten
        searches a minute Open Food Facts allows would be exhausted by a single
        shop (``docs/technical-notes-ingestion.md`` section 3.6), which is also why
        nothing here goes near a network. A household's own product wins over a
        public one of the same name: it is the row that household curated.
        """
        wanted = {label.casefold() for label in labels if label}
        if not wanted:
            return {}
        rows = (
            await self._session.scalars(
                select(Product).where(
                    func.lower(Product.name).in_(wanted),
                    Product.archived_at.is_(None),
                    # Not `in_([household_id, None])`: SQL never matches NULL with
                    # IN, so the public catalogue would silently become invisible.
                    (Product.household_id == household_id) | Product.household_id.is_(None),
                )
            )
        ).all()

        by_name: dict[str, Product] = {}
        for row in rows:
            key = row.name.casefold()
            current = by_name.get(key)
            if current is None or (current.household_id is None and row.household_id is not None):
                by_name[key] = row

        return {
            index: found.id
            for index, label in enumerate(labels)
            if label and (found := by_name.get(label.casefold())) is not None
        }

    # -- Reads -------------------------------------------------------------- #

    async def get(self, household_id: uuid.UUID, receipt_id: uuid.UUID) -> ReceiptProposal:
        return await self._proposal(household_id, receipt_id)

    async def list_pending(
        self, household_id: uuid.UUID, *, limit: int = 20
    ) -> tuple[ReceiptSummary, ...]:
        """Receipts read but not yet accepted into stock, newest first.

        The screen this feeds is not a nicety. A receipt counts towards the budget
        from the moment it is parsed (contract 6ter), so one abandoned mid-review
        is money on a screen with nothing pointing at it. This is what points at
        it, and what makes discarding it possible.
        """
        rows = (
            await self._session.execute(
                select(
                    Receipt.id,
                    Receipt.status,
                    Receipt.merchant_name,
                    Receipt.purchased_at,
                    Receipt.total_amount,
                    Receipt.currency,
                    Receipt.created_at,
                    Receipt.provider_code,
                    select(func.count(ReceiptLine.id))
                    .where(
                        ReceiptLine.receipt_id == Receipt.id,
                        ReceiptLine.household_id == Receipt.household_id,
                    )
                    .scalar_subquery()
                    .label("line_count"),
                )
                .where(
                    Receipt.household_id == household_id,
                    Receipt.status == ReceiptStatus.PARSED,
                )
                .order_by(Receipt.created_at.desc())
                .limit(limit)
            )
        ).all()
        return tuple(
            ReceiptSummary(
                id=row.id,
                source="photo" if row.provider_code else "pdf",
                status="parsed",
                merchant=row.merchant_name,
                purchased_at=row.purchased_at,
                total_amount=row.total_amount,
                currency=row.currency,
                line_count=int(row.line_count),
                created_at=row.created_at,
            )
            for row in rows
        )

    async def _proposal(self, household_id: uuid.UUID, receipt_id: uuid.UUID) -> ReceiptProposal:
        receipt = await self._require(household_id, receipt_id)
        rows = (
            await self._session.scalars(
                select(ReceiptLine)
                .where(
                    ReceiptLine.household_id == household_id,
                    ReceiptLine.receipt_id == receipt_id,
                )
                .order_by(ReceiptLine.line_no)
            )
        ).all()

        # Recomputed on every read rather than stored beside the raw label.
        # ``expand_label`` is pure -- no I/O, no clock, no network -- so
        # recomputing costs nothing, and it means a line imported before an
        # abbreviation was added to ``lexicon.toml`` reads correctly the next time
        # somebody opens it. A stored expansion would freeze the reading at the
        # moment of the import, which is exactly the wrong moment: the lexicon
        # only ever grows.
        retailer = retailer_slug(receipt.merchant_name)
        readings = {row.id: _readable(row.raw_label, retailer=retailer) for row in rows}
        lines = tuple(
            ProposedReceiptLine(
                id=row.id,
                line_no=row.line_no,
                raw_label=row.raw_label,
                label=readings[row.id][0],
                suggested_label=readings[row.id][1],
                quantity=row.quantity_value,
                unit_code=row.quantity_unit_code,
                unit_price=row.unit_price,
                total_price=row.total_price,
                matched_product_id=row.matched_product_id,
                match_status=_match_status(row.match_status),
                match_confidence=row.match_confidence,
            )
            for row in rows
        )
        return ReceiptProposal(
            id=receipt.id,
            source="photo" if receipt.provider_code else "pdf",
            read_by="model" if receipt.provider_code else "text",
            status=_status(receipt.status),
            merchant=receipt.merchant_name,
            retailer=retailer_slug(receipt.merchant_name),
            purchased_at=receipt.purchased_at,
            total_amount=receipt.total_amount,
            currency=receipt.currency,
            lines=lines,
            line_sum=_line_sum(lines),
            truncated=receipt.lines_truncated,
            degradation_notice=receipt.degradation_notice,
        )

    async def _require(self, household_id: uuid.UUID, receipt_id: uuid.UUID) -> Receipt:
        receipt = await self._session.scalar(
            select(Receipt).where(Receipt.household_id == household_id, Receipt.id == receipt_id)
        )
        if receipt is None:
            raise ReceiptNotFoundError(f"no receipt {receipt_id} belongs to this household")
        return receipt

    async def _timezone(self, household_id: uuid.UUID) -> str | None:
        name = await self._session.scalar(
            select(Household.timezone).where(Household.id == household_id)
        )
        return None if name is None else str(name)

    # -- The only call that writes stock ------------------------------------ #

    async def confirm(
        self,
        household_id: uuid.UUID,
        receipt_id: uuid.UUID,
        *,
        lines: Sequence[ConfirmReceiptLine],
        location_id: uuid.UUID | None = None,
    ) -> ConfirmedReceipt:
        """Write the reviewed lines into stock. Idempotent by refusal, not by replay.

        Every value in ``lines`` is validated again here -- the unit against the
        ``unit`` table, the product against what this household may see, the label
        through the same neutralisation as any untrusted text -- because a client
        is not a source of truth about a household's data. What the client *is*
        authoritative about is which lines the human kept, and that is the only
        thing taken from it unchecked.

        A line absent from the body is not a line that was never read: it is one
        the reviewer removed. It becomes ``ignored`` and **keeps its price**,
        because "do not put this in my cupboard" is not "this was not on the
        receipt", and the line sum shown beside the total has to stay the sum of
        what was printed.

        A line named **twice** is a line named once. The body is a set of decisions
        about the receipt's own lines, not a list of writes to perform: a client
        that repeats an identifier -- a retry, a double tap, a replay -- used to
        get one lot per repetition, silently multiplying the quantity that went
        into the cupboard and reporting a lot count no row in the database
        supported.
        """
        receipt = await self._require(household_id, receipt_id)
        if receipt.status is ReceiptStatus.CONFIRMED:
            raise ReceiptAlreadyConfirmedError(
                f"receipt {receipt_id} has already been written to stock"
            )

        stored = {
            row.id: row
            for row in (
                await self._session.scalars(
                    select(ReceiptLine).where(
                        ReceiptLine.household_id == household_id,
                        ReceiptLine.receipt_id == receipt_id,
                    )
                )
            ).all()
        }

        created_lots = 0
        created_products = 0
        kept: set[uuid.UUID] = set()
        for line in lines:
            row = stored.get(line.id)
            if row is None:
                # A line identifier that belongs to another receipt, or to none.
                # Dropped rather than fatal: the rest of the review is the user's
                # work and losing it to one stale identifier would be the worse
                # outcome. It is logged, because it should not happen.
                logger.warning(
                    "receipt_confirm_unknown_line", extra={"receipt_id": str(receipt_id)}
                )
                continue
            if row.id in kept:
                # The same line, decided twice. Ignored rather than applied again:
                # ``_add_stock`` below writes a lot and a stock movement, so a body
                # repeating one identifier fifty times used to put fifty times the
                # quantity in the cupboard from a review screen that showed one.
                logger.warning(
                    "receipt_confirm_duplicate_line", extra={"receipt_id": str(receipt_id)}
                )
                continue
            kept.add(row.id)
            product_id, was_created = await self._resolve_target(household_id, line, row)
            created_products += int(was_created)
            if await self._add_stock(household_id, line, row, product_id, location_id):
                created_lots += 1
            row.matched_product_id = product_id
            row.match_status = ReceiptLineMatchStatus.CONFIRMED

        ignored = 0
        for row in stored.values():
            if row.id not in kept:
                row.match_status = ReceiptLineMatchStatus.IGNORED
                ignored += 1

        receipt.status = ReceiptStatus.CONFIRMED
        await self._session.flush()

        logger.info(
            "receipt_confirmed",
            extra={
                "receipt_id": str(receipt_id),
                "created_lot_count": created_lots,
                "ignored_line_count": ignored,
            },
        )
        return ConfirmedReceipt(
            receipt_id=receipt_id,
            created_lot_count=created_lots,
            ignored_line_count=ignored,
            created_product_count=created_products,
        )

    async def _resolve_target(
        self, household_id: uuid.UUID, line: ConfirmReceiptLine, row: ReceiptLine
    ) -> tuple[uuid.UUID, bool]:
        """The product this line stocks, creating a private one when there is none.

        Creating rather than refusing is the point of the whole feature: 0.588 of
        labels resolve to no catalogue entry by measurement, and an import that
        stopped there would put nothing in the cupboard on most shops. The row is
        private to the household and carries ``receipt_import`` as its source, so
        it never pollutes the shared catalogue and stays traceable to the line
        that caused it.
        """
        if line.product_id is not None:
            product = await self._session.scalar(
                select(Product).where(
                    Product.id == line.product_id,
                    Product.archived_at.is_(None),
                    (Product.household_id == household_id) | Product.household_id.is_(None),
                )
            )
            if product is None:
                raise ProductNotFoundError(line.product_id)
            return product.id, False

        label = sanitize_optional(line.label, limit=MAX_RAW_LABEL) or row.raw_label
        existing = await self._session.scalar(
            select(Product).where(
                func.lower(Product.name) == label.casefold(),
                Product.archived_at.is_(None),
                (Product.household_id == household_id) | Product.household_id.is_(None),
            )
        )
        if existing is not None:
            # The reviewer typed a name the catalogue already holds. Reusing it
            # keeps "2 kg of flour" one line in the inventory instead of two rows
            # spelled identically, which is the readability failure that makes
            # people stop opening the application.
            return existing.id, False

        created = await self._products.create_private(
            household_id,
            ProductDraft(name=label, source=ProductSource.RECEIPT_IMPORT),
        )
        return created.id, True

    async def _add_stock(
        self,
        household_id: uuid.UUID,
        line: ConfirmReceiptLine,
        row: ReceiptLine,
        product_id: uuid.UUID,
        location_id: uuid.UUID | None,
    ) -> bool:
        """Add one lot, or none when the line carries no usable quantity.

        Returning ``False`` rather than inventing "1 piece" is deliberate. A
        receipt line whose quantity nobody could read is a line the reviewer chose
        to keep without completing, and a made-up quantity would be a made-up
        inventory -- the exact failure contract 6ter separates the budget from the
        stock to avoid. The line is still marked ``confirmed``: the human saw it,
        and the money it represents is in the total either way.
        """
        amount = line.amount if line.amount is not None else row.quantity_value
        unit_code = line.unit_code if line.unit_code is not None else row.quantity_unit_code
        if amount is None or unit_code is None or amount <= 0:
            return False
        try:
            await self._inventory.add_item(
                household_id,
                AddItemCommand(
                    amount=amount,
                    unit=unit_code,
                    product_id=product_id,
                    location_id=location_id,
                    expiry_kind=ExpiryDateKind.BEST_BEFORE,
                    source=StockEntrySource.RECEIPT_IMPORT,
                    source_receipt_line_id=row.id,
                ),
            )
        except UnknownUnitError:
            # The reviewer sent a unit this instance's table does not have. One
            # line is lost rather than the whole confirmation, and the exception
            # would otherwise roll back the lines already written.
            logger.warning("receipt_confirm_unknown_unit", extra={"receipt_line_id": str(row.id)})
            return False
        return True

    # -- Giving up on one --------------------------------------------------- #

    async def discard(self, household_id: uuid.UUID, receipt_id: uuid.UUID) -> None:
        """Delete a proposal the household does not want. Refuses once confirmed.

        The counterpart of storing the proposal. A parsed receipt already counts
        towards the budget, so without this an abandoned import is a permanent
        overstatement of what a household spent; with it, the way out is one
        gesture. A *confirmed* receipt is not deletable here -- its lines are lots
        in a cupboard, and deleting the receipt would leave them with a dangling
        provenance and the spend they were bought with silently gone.
        """
        receipt = await self._require(household_id, receipt_id)
        if receipt.status is ReceiptStatus.CONFIRMED:
            raise ReceiptAlreadyConfirmedError(
                f"receipt {receipt_id} is already in stock and cannot be discarded"
            )
        await self._session.delete(receipt)
        await self._session.flush()
        logger.info("receipt_discarded", extra={"receipt_id": str(receipt_id)})


# --------------------------------------------------------------------------- #
# Coercions
# --------------------------------------------------------------------------- #


def _money(value: Decimal | None) -> Decimal | None:
    """A price at the schema's two decimals, or ``None`` when it is not one.

    A negative amount survives: French receipts print discounts as negative
    lines, and dropping them would make the line sum disagree with the total for
    a reason that is nobody's mistake.
    """
    if value is None:
        return None
    if abs(value) >= Decimal("1000000000"):
        return None
    return value.quantize(_CENTS)


def _quantity(value: Decimal | None) -> Decimal | None:
    if value is None or value <= 0 or value > Decimal("100000"):
        return None
    return value.quantize(Decimal("0.001"))


def _currency(value: str | None) -> str | None:
    """An ISO 4217 code, or nothing. Never a symbol and never a guess."""
    if value is None:
        return None
    code = value.strip().upper()
    if len(code) != _CURRENCY_LENGTH or not code.isalpha():
        return None
    return code


def _readable(raw_label: str, *, retailer: str | None) -> tuple[str, str | None]:
    """The label to show, and the reading to *offer* when it is not shown.

    Three outcomes, and the difference between the last two is the whole point.

    * The lexicon produced one complete, confident reading in which something was
      actually abbreviated: it becomes the label. ``CHIPS PDT SEL ET POIVRE 150G``
      reads as ``chips pomme de terre sel et poivre``.
    * It produced a *complete* reading it is not confident about -- ambiguous, or
      low confidence: the **raw label is shown**, and the reading travels
      separately as a suggestion the reviewer can accept in one tap. Presenting
      it as the label would assert something the lexicon explicitly declined to
      assert.
    * It produced nothing usable, or nothing complete: the raw label, and no
      suggestion. This is the *common* case -- recall is 0.412 by measurement --
      and it is the accepted half of the trade in ``domain/labels``: the chosen
      failure is the miss.

    The third case swallows one outcome worth naming, because it was visible on
    the first real screen this drew. ``SAUM FUME 4TR`` expands to ``saum fume tr``
    at high confidence with ``tr`` left in the ``unresolved`` list -- a reading
    the module itself declined to finish. Offering that as a label is offering a
    phrase that is not French, and a suggestion mechanism that proposes nonsense
    once stops being read. So an incomplete reading produces no suggestion at
    all: it is not a worse label than the printed one, it is not a label.

    An unabbreviated label is never replaced by its own normalised form either.
    ``BANANES`` staying ``BANANES`` rather than becoming ``bananes`` costs nothing
    and keeps the screen showing what the paper shows.
    """
    if not raw_label:
        return raw_label, None
    reading = expand_label(raw_label, retailer=retailer)
    best = reading.best
    if best is None or reading.unresolved:
        return raw_label, None
    if not reading.requires_review and reading.expanded:
        return best.text, None
    if best.text.casefold() == raw_label.casefold():
        return raw_label, None
    return raw_label, best.text


def _match_status(
    value: ReceiptLineMatchStatus,
) -> Literal["pending", "suggested", "confirmed", "rejected", "ignored"]:
    """The stored enum, narrowed onto the contract's vocabulary.

    A mapping rather than ``value.value``: the two are the same strings today, and
    a type checker that is told so is a type checker that will notice when a
    member is added to one and not the other.
    """
    return _MATCH_STATUS[value]


def _status(value: ReceiptStatus) -> Literal["parsed", "confirmed", "failed"]:
    """The three states the API exposes.

    ``uploaded`` and ``parsing`` never surface: this pipeline is synchronous, so a
    row is only written once it has been read. They stay in the enum because the
    queue index in ``domain/models.py`` is built for the day it is not.
    """
    if value is ReceiptStatus.CONFIRMED:
        return "confirmed"
    if value is ReceiptStatus.FAILED:
        return "failed"
    return "parsed"


def _line_sum(lines: Sequence[ProposedReceiptLine]) -> Decimal | None:
    """The sum of the line prices, or ``None`` when any line has none.

    Strict, and the strictness is the point (``services/budget.py`` uses the same
    rule). A partial sum can never disprove a printed total, so reporting a gap on
    the strength of one would raise an alarm on every half-read receipt and teach
    the reviewer to ignore the alarm that matters.
    """
    if not lines:
        return None
    total = Decimal("0")
    for line in lines:
        if line.total_price is None:
            return None
        total += line.total_price
    return total
