"""Sending a shopping list out of the house, without knowing where it lands.

The shape of this module is taken from ADR-0005: the service that assembles a
list must not be able to tell Todoist from anything else, exactly as
``services/recipes.py`` cannot tell Anthropic from Ollama. What crosses this
boundary is *a shopping list*: names, amounts, units. No token, no header, no
project identifier, no vendor vocabulary of any kind.

Three properties are decided here rather than in the adapters.

**Plain text is the format, and it is a domain concern.** :func:`render_plain_text`
is what ``navigator.share({ text })`` sends (ADR-0010, and the research note
§4.7, which ranks it first by a wide margin), and it is also what every adapter
puts in a task title. One renderer means "Lait 1 L" reads the same in the share
sheet and in Todoist. It lives here because it is a rule about the *list*, not
about any recipient.

**One line per item, guaranteed structurally.** A label a household typed can
contain a newline, and a list where one item silently becomes two is worse than
a list that fails: nobody checks a plausible-looking list. :func:`render_line`
collapses whitespace itself rather than trusting its caller to have done it,
because the format's central promise cannot depend on an upstream convention.

**Nothing here writes.** The export is read-only over the household's data, and
the port has no method that could remove or tick an item. The list is a personal
record leaving the home for a third party (ADR-0010, GDPR): the smallest thing
that does the job is what should leave, and the smallest thing is a name and an
amount.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Protocol, runtime_checkable

__all__ = [
    "COUNT_PREFIX",
    "ExportContext",
    "ExportCredentialUnreadable",
    "ExportLine",
    "ExportReceipt",
    "ExportTargetNotConfigured",
    "ExportTargetRejected",
    "ShoppingExportConsentMissing",
    "ShoppingExportError",
    "ShoppingExportPartiallyApplied",
    "ShoppingExportQuotaExceeded",
    "ShoppingExportResponseInvalid",
    "ShoppingExportUnavailable",
    "ShoppingListExport",
    "ShoppingListExporter",
    "render_line",
    "render_plain_text",
]


# --------------------------------------------------------------------------- #
# What leaves the house
# --------------------------------------------------------------------------- #

#: How a countable amount is written: U+00D7 MULTIPLICATION SIGN, as in "Oeufs
#: <sign> 6" rather than "Oeufs 6 pc". The symbol of the ``piece`` unit is an
#: abbreviation a reader has to decode, and the multiplication sign is the
#: notation a shopping list already uses on paper. Written as an escape so that
#: no ambiguous-character lint has to be silenced to read this file.
COUNT_PREFIX: str = "\u00d7"


@dataclass(frozen=True, slots=True)
class ExportLine:
    """One thing to buy, reduced to what a stranger's application can use.

    ``unit_symbol`` is the *display* symbol from the ``unit`` table (``L``,
    ``c. à s.``), not the code: the destination is a human reading a task title,
    and no recipient of this data has any use for our unit vocabulary.

    ``is_count`` rather than the dimension enum, for the same reason as the rest
    of this module: the renderer needs to know whether to write a count as a
    multiplication, and it does not need to know that Chaudron models dimensions
    at all.
    """

    name: str
    quantity: Decimal | None = None
    unit_symbol: str | None = None
    is_count: bool = False

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("an export line must name something")
        if self.quantity is not None and self.quantity <= 0:
            raise ValueError("an exported quantity must be positive")


@dataclass(frozen=True, slots=True)
class ShoppingListExport:
    """A list, at one instant, on its way out.

    :attr:`export_id` is not decoration. It is the seed adapters derive their
    per-item idempotency keys from, so a client that retries the *same* export
    -- the identifier is returned to it -- adds nothing twice. A fresh
    identifier per call would make every retry a duplicate list.
    """

    export_id: uuid.UUID
    shopping_list_id: uuid.UUID
    shopping_list_name: str
    lines: tuple[ExportLine, ...]
    generated_at: dt.datetime

    def __post_init__(self) -> None:
        if self.generated_at.tzinfo is None:
            raise ValueError("generated_at must be timezone-aware")

    @property
    def is_empty(self) -> bool:
        return not self.lines


@dataclass(frozen=True, slots=True)
class ExportReceipt:
    """What the recipient accepted. Counts only -- never an echo of the list.

    ``target`` is the stable code of the destination (``todoist``), which is what
    an interface shows and what a log line may carry. ``external_list_id`` is
    reported when the recipient names the container it wrote into, so a user who
    cannot find their items has something to look for; it is ``None`` whenever
    the destination was the recipient's own default inbox.
    """

    target: str
    export_id: uuid.UUID
    accepted_line_count: int
    external_list_id: str | None = None


# --------------------------------------------------------------------------- #
# Rendering -- the universal format
# --------------------------------------------------------------------------- #


def _format_amount(amount: Decimal) -> str:
    """A number a human reads: no exponent, no trailing zeros, a decimal point.

    A point rather than a comma even though the interface is French. This string
    is pasted into applications, spreadsheets and messages we do not control, and
    a comma is a field separator in half of them. ``1,5`` becoming two cells is a
    silent corruption; ``1.5`` is merely not idiomatic.
    """
    try:
        normalised = amount.normalize()
        if normalised == normalised.to_integral_value():
            normalised = normalised.quantize(Decimal(1))
    except InvalidOperation, ValueError:
        # Only reachable for a magnitude no quantity column can hold. Printing
        # the value plainly beats failing an export over a formatting nicety.
        return f"{amount:f}"
    return f"{normalised:f}"


def render_line(line: ExportLine) -> str:
    """One item, on exactly one line.

    The whitespace collapse is the guarantee, not a tidy-up: a label containing a
    newline would otherwise turn one item into two in every recipient at once --
    the share sheet, the task application, the clipboard. Callers sanitise their
    input too (``infra/untrusted_text.py``); this is the property being enforced
    where the format is defined, so it holds even for a caller that forgot.
    """
    name = " ".join(line.name.split())
    if line.quantity is None:
        return name
    amount = _format_amount(line.quantity)
    if line.is_count:
        return f"{name} {COUNT_PREFIX} {amount}"
    if line.unit_symbol:
        symbol = " ".join(line.unit_symbol.split())
        return f"{name} {amount} {symbol}"
    return f"{name} {amount}"


def render_plain_text(lines: Iterable[ExportLine]) -> str:
    """The whole list as text, ready for ``navigator.share({ text })``.

    No title, no bullets, no checkboxes, and each of those absences is a
    decision. A title line becomes a spurious first task the moment somebody
    pastes the block into a task application; a ``-`` prefix survives into the
    task name in several of them; a ``[ ]`` box is redundant everywhere it lands
    and noise everywhere else. What is left -- one item per line -- is the only
    thing every destination reads correctly, which is what makes it universal.

    No trailing newline: several share sheets render it as an empty final item.
    """
    return "\n".join(render_line(line) for line in lines)


# --------------------------------------------------------------------------- #
# The port
# --------------------------------------------------------------------------- #


@runtime_checkable
class ShoppingListExporter(Protocol):
    """Hand a shopping list to somewhere the household already looks.

    One method, taking domain objects and returning a count. An implementation
    holds its own credential and its own destination; neither is representable in
    this signature, which is what keeps the service unable to leak either.
    """

    async def send(self, export: ShoppingListExport) -> ExportReceipt: ...


# --------------------------------------------------------------------------- #
# Failures
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ExportContext:
    """The two facts a support answer needs, and nothing a token could hide in."""

    target: str
    #: Short, stable token naming *how* it failed (``timeout``, ``rate_limited``).
    failure_mode: str | None = None


class ShoppingExportError(Exception):
    """Base of every export failure the domain understands.

    Same rule as :class:`chaudron.domain.llm_ports.LlmError`, for the same
    reason: the message is built from our own fields, never from the recipient's
    response. A third-party API routinely quotes the bearer token it rejected,
    and a token in a log line outlives every rotation policy (SEC-003).
    """

    def __init__(self, message: str, *, context: ExportContext | None = None) -> None:
        self.context = context
        if context is not None:
            suffix = f" [target={context.target}"
            if context.failure_mode:
                suffix += f" failure={context.failure_mode}"
            suffix += "]"
            message += suffix
        super().__init__(message)


class ShoppingExportUnavailable(ShoppingExportError):  # noqa: N818 -- mirrors llm_ports
    """The recipient could not be reached, timed out, or returned a server error."""


class ShoppingExportQuotaExceeded(ShoppingExportError):  # noqa: N818 -- mirrors llm_ports
    """Rate limited by the recipient. The household waits; nothing here can help."""


class ShoppingExportResponseInvalid(ShoppingExportError):  # noqa: N818 -- mirrors llm_ports
    """The recipient answered with something that is not the documented shape."""


class ExportTargetNotConfigured(ShoppingExportError):  # noqa: N818 -- mirrors llm_ports
    """No usable destination: none registered, or its credential is unreadable."""


class ExportTargetRejected(ExportTargetNotConfigured):
    """The recipient refused the credential. The household must issue a new one.

    A subclass of :class:`ExportTargetNotConfigured` because that is what it
    means to the caller -- there is no working destination and the way out is to
    enter a token again -- while staying a distinct type, because "your token was
    revoked" and "you never configured this" are different sentences to show.
    """


class ExportCredentialUnreadable(ExportTargetNotConfigured):
    """The destination is registered, but its stored token cannot be decrypted.

    A subclass of :class:`ExportTargetNotConfigured` for the same reason
    :class:`ExportTargetRejected` is -- there is no working destination and the
    way out is to enter a token again -- and a distinct type for the same reason
    too: "register a destination" and "the one you registered can no longer be
    read" are different sentences, and only one of them is true here.

    It exists because the failure had no domain type at all and therefore no
    exit (audit O-06). ``infra/crypto.py`` raises
    :class:`~chaudron.domain.llm_ports.CredentialDecryptionError`, which is a
    ``ProviderNotConfigured`` -- a *model provider* error, disjoint from this
    hierarchy -- so it crossed ``routers/shopping_export.py`` untouched and the
    household got an unexplained 500. The case is ordinary rather than exotic:
    the operator restored the instance without
    ``CHAUDRON_CREDENTIAL_ENCRYPTION_KEY``, or rotated it, and every stored token
    stopped opening at once.

    The message is written for the household, not the operator. It says the
    credential can no longer be read and to enter it again; it says nothing about
    which key, whose key, or that a key exists -- that half belongs in the log,
    where ``infra/todo/credentials.py`` puts it.
    """


class ShoppingExportConsentMissing(ShoppingExportError):  # noqa: N818 -- mirrors llm_ports
    """The household never agreed to this list leaving the instance.

    A shopping list is personal data about identifiable people: what a household
    eats, therefore sometimes its members' health or religion. Sending it to a
    third party needs an explicit, recorded, revocable agreement (ADR-0010), and
    the absence of one is a refusal rather than a default.
    """


class ExportRegistrantHasLeft(ShoppingExportConsentMissing):
    """The person who registered this destination is no longer in the household.

    A subclass of :class:`ShoppingExportConsentMissing` because that is what it
    amounts to -- nobody currently in this household is standing behind the
    agreement -- while staying a distinct type, because the remedy is different
    and the difference matters. A withdrawn agreement is re-granted by ticking a
    box. This one is a token belonging to somebody who has gone, still filing this
    household's groceries into *their* account, and the only sound answer is for
    an owner to look at it.

    The failure it closes was silent and durable: nothing cascades from
    ``household_member`` to ``shopping_export_target``, so a member excluded from
    a household left behind a consented row that kept exporting to them
    indefinitely (audit AUD-027). The check is made on every send rather than at
    the moment of exclusion, because there is no exclusion path in the application
    to hang it on -- a membership disappears through SQL, and a rule that only
    runs when somebody remembers to call it is not a rule.
    """


class ShoppingExportPartiallyApplied(ShoppingExportError):  # noqa: N818 -- see above
    """Some items were accepted and some were not, and the caller must be told.

    The alternative -- reporting success because the request returned 200 -- is
    how a household ends up shopping from a list missing three items it can see
    in Chaudron. The counts are carried so the interface can say which.
    """

    def __init__(
        self,
        *,
        accepted: int,
        rejected: int,
        context: ExportContext | None = None,
    ) -> None:
        self.accepted = accepted
        self.rejected = rejected
        super().__init__(
            f"{rejected} of {accepted + rejected} items were refused by the destination",
            context=context,
        )
