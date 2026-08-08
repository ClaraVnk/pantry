"""Todoist, over the Sync endpoint: one HTTP call for a whole shopping list.

Every element of the request below was read off the official reference at
<https://developer.todoist.com/api/v1/> on 2026-08-04, not recalled. The
distinction has cost this project before -- an adapter once POSTed to Ollama's
``/api/version``, which answers ``405`` -- so what is quoted is quoted, and what
is inferred says so.

**Verified verbatim in the documentation.**

* The base URL is ``https://api.todoist.com`` and the endpoint is
  ``POST /api/v1/sync``.
* Authentication is the header ``Authorization: Bearer {token}``.
* The body is ``application/x-www-form-urlencoded``, and ``commands`` is a
  JSON-encoded array carried in a form field. The published write example is a
  ``curl`` sending **only** ``commands`` -- no ``sync_token``, no
  ``resource_types`` -- which is what makes a write-only call possible: asking
  for a read would hand back the account's whole dataset for nothing.
* A command is ``{"type", "uuid", "args"}`` with an optional ``temp_id``; the
  ``uuid`` is "a unique identifier for result mapping and idempotency".
* Adding a task is ``item_add``; ``content`` is its required argument and
  ``project_id`` is optional.
* The answer contains ``sync_token``, ``sync_status`` -- a map from each command
  ``uuid`` to ``"ok"`` or to an error object -- and ``temp_id_mapping``.

**Not verified, and treated accordingly.** The documented "Request Limits"
section did not render when it was fetched, so the number of commands one request
may carry is unknown. The batch size here is therefore *our* bound
(:data:`~chaudron.infra.todo.settings.MAX_COMMANDS_PER_REQUEST`), chosen low, and
a longer list is sent as successive batches rather than assumed to fit.

**Why the personal token rather than OAuth.** ADR-0007 already sells a product
where the household brings its own credentials to a self-hosted instance. OAuth
would additionally require *the operator* to register an application with
Todoist, host a redirect URI, and keep a client secret -- per deployment, for a
feature whose entire value is that it takes an afternoon. A personal token is
pasted once, is revocable from the same settings page, and needs nothing from us.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Final

import httpx

from chaudron.domain.shopping_export import (
    ExportContext,
    ExportLine,
    ExportReceipt,
    ShoppingExportPartiallyApplied,
    ShoppingExportResponseInvalid,
    ShoppingListExport,
    render_line,
)
from chaudron.infra.todo.http import BoundedTodoClient, HttpAnswer, translate_export_status
from chaudron.infra.todo.settings import MAX_COMMANDS_PER_REQUEST, TodoExportSettings

__all__ = ["TARGET_CODE", "TodoistExporter", "build_todoist_client"]

#: The stable code this destination is known by, in configuration, in the API and
#: in a log line.
TARGET_CODE: Final = "todoist"

_BASE_URL: Final = "https://api.todoist.com"
_SYNC_PATH: Final = "/api/v1/sync"
_ITEM_ADD: Final = "item_add"

#: Ceiling on one task title. Todoist does not publish a limit for ``content``
#: that this check could quote, so the bound is ours: it exists to keep a
#: pathological label from making the request enormous, not to enforce a vendor
#: rule we would be inventing.
_MAX_CONTENT_CHARS: Final = 500

#: Namespace for the per-item command identifiers. A fixed UUID rather than a
#: random one, because the whole value of deriving them is that the *same* export
#: replayed produces the *same* identifiers -- which is what Todoist's
#: "idempotency" note on ``uuid`` then makes matter.
_COMMAND_NAMESPACE: Final = uuid.UUID("6f8bb0f6-1a4a-5c1e-9e5e-2f3a9a2b7c11")


def build_todoist_client(
    token: str,
    settings: TodoExportSettings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> BoundedTodoClient:
    """The bounded client for Todoist, with the token in the header and nowhere else."""
    return BoundedTodoClient(
        _BASE_URL,
        target=TARGET_CODE,
        settings=settings,
        headers={"Authorization": f"Bearer {token}"},
        # Handed over so that any excerpt this client ever puts in a message has
        # the token removed by literal match, whatever shape it has.
        secret=token,
        transport=transport,
    )


class TodoistExporter:
    """Adds a household's pending shopping-list items as Todoist tasks.

    Implements :class:`~chaudron.domain.shopping_export.ShoppingListExporter`,
    which is the only type the service knows. It holds the token; the token
    appears in exactly one place, the ``Authorization`` header built in
    :func:`build_todoist_client`, and in no attribute of anything that is logged,
    returned or repr'd.
    """

    __slots__ = ("_client", "_project_id")

    def __init__(self, client: BoundedTodoClient, *, project_id: str | None = None) -> None:
        self._client = client
        #: ``None`` means the account's Inbox: ``item_add`` without a
        #: ``project_id`` is documented as optional, and the Inbox is where a
        #: household that never chose a project expects to find its list.
        self._project_id = project_id

    def __repr__(self) -> str:
        return f"TodoistExporter(project_id={self._project_id!r})"

    async def send(self, export: ShoppingListExport) -> ExportReceipt:
        """Send the list, in as few calls as the batch bound allows.

        An empty list is not a call. Sending zero commands would be a request
        that costs a round trip, may count against a rate limit, and can only
        ever answer "nothing happened".
        """
        if export.is_empty:
            return ExportReceipt(
                target=TARGET_CODE,
                export_id=export.export_id,
                accepted_line_count=0,
                external_list_id=self._project_id,
            )

        commands = [
            self._item_add(export.export_id, index, line) for index, line in enumerate(export.lines)
        ]
        accepted = 0
        rejected = 0
        for start in range(0, len(commands), MAX_COMMANDS_PER_REQUEST):
            batch = commands[start : start + MAX_COMMANDS_PER_REQUEST]
            ok, failed = await self._post_batch(batch)
            accepted += ok
            rejected += failed

        if rejected:
            # Partial success is reported as a failure with counts, never as a
            # success: a household shopping from a list quietly missing three
            # items is the outcome this whole feature exists to avoid.
            raise ShoppingExportPartiallyApplied(
                accepted=accepted,
                rejected=rejected,
                context=ExportContext(TARGET_CODE, "command_rejected"),
            )
        return ExportReceipt(
            target=TARGET_CODE,
            export_id=export.export_id,
            accepted_line_count=accepted,
            external_list_id=self._project_id,
        )

    # -- the wire --------------------------------------------------------- #

    def _item_add(self, export_id: uuid.UUID, index: int, line: ExportLine) -> dict[str, Any]:
        """One ``item_add`` command.

        ``content`` is the same string the plain-text export renders, so an item
        reads identically in the share sheet and in Todoist. ``uuid`` is derived
        from ``(export_id, index)`` rather than drawn at random: Todoist
        documents that identifier as carrying idempotency, so a client resending
        an export it already sent -- the identifier is in the response it holds
        -- adds nothing twice.
        """
        content = render_line(line)
        args: dict[str, Any] = {"content": content[:_MAX_CONTENT_CHARS]}
        if self._project_id:
            args["project_id"] = self._project_id
        return {
            "type": _ITEM_ADD,
            "uuid": str(uuid.uuid5(_COMMAND_NAMESPACE, f"{export_id}:{index}")),
            "args": args,
        }

    async def _post_batch(self, commands: list[dict[str, Any]]) -> tuple[int, int]:
        """Send one batch; return ``(accepted, rejected)``.

        ``commands`` is a form field holding JSON -- the shape the documented
        ``curl`` example uses -- and nothing else is sent. ``sync_token`` and
        ``resource_types`` are omitted deliberately: including them turns a write
        into a read and returns the account's data, which is both slow and more
        of somebody's life than this application has any business receiving.
        """
        answer = await self._client.post_form(
            _SYNC_PATH, {"commands": json.dumps(commands, ensure_ascii=False)}
        )
        if isinstance(answer, HttpAnswer):
            raise translate_export_status(answer, target=TARGET_CODE)
        return _count_statuses(answer, expected=len(commands))


def _count_statuses(answer: dict[str, Any], *, expected: int) -> tuple[int, int]:
    """Read ``sync_status``, counting outcomes and quoting none of them.

    A 200 from this endpoint says the *request* was understood, not that the
    commands succeeded: each one has its own entry, ``"ok"`` or an error object.
    Reading only the status code is the classic way to report a successful export
    that added nothing.

    The error objects are counted and discarded. They are written by the
    recipient, about a request that carried our bearer token, and this project's
    rule for such text is that it goes nowhere near a message.
    """
    status = answer.get("sync_status")
    if not isinstance(status, dict):
        raise ShoppingExportResponseInvalid(
            "Todoist answered without the sync_status map the API documents",
            context=ExportContext(TARGET_CODE, "malformed_payload"),
        )
    accepted = sum(1 for outcome in status.values() if outcome == "ok")
    # Commands absent from the map are counted as rejected rather than assumed
    # fine: an item nobody can account for did not reach anyone's list.
    return accepted, expected - accepted
