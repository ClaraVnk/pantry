"""A Todoist double that records what the adapter actually put on the wire.

The point of driving the real adapter over ``httpx.MockTransport`` rather than
stubbing the adapter is that the *request shape* is under test. That shape was
read off the official reference on 2026-08-04 rather than recalled, and this
fixture is what keeps it honest: form-encoded body, ``commands`` as a JSON array,
``Authorization: Bearer``, no ``sync_token`` and no ``resource_types``.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import httpx
import pytest

from chaudron.domain.shopping_export import ExportLine, ShoppingListExport
from chaudron.infra.todo.settings import TodoExportSettings

#: Shaped like a real one -- 40 hex characters, as in Todoist's own example -- so
#: that a leak test is testing something a scrubber could plausibly miss.
FAKE_TOKEN = "0123456789abcdef0123456789abcdef01234567"


@dataclass
class RecordedCall:
    """One outbound request, decoded."""

    method: str
    path: str
    headers: dict[str, str]
    body: str

    @property
    def form(self) -> dict[str, str]:
        return dict(httpx.QueryParams(self.body).items())

    @property
    def commands(self) -> list[dict[str, Any]]:
        decoded: list[dict[str, Any]] = json.loads(self.form["commands"])
        return decoded


@dataclass
class TodoistDouble:
    """A scripted Todoist. Answers what the API documents, or what a test asks for."""

    calls: list[RecordedCall] = field(default_factory=list)
    #: Chosen per call: ``(command_uuids) -> httpx.Response``.
    responder: Callable[[list[str]], httpx.Response] | None = None

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        body = request.content.decode("utf-8")
        self.calls.append(
            RecordedCall(
                method=request.method,
                path=request.url.path,
                headers={key.lower(): value for key, value in request.headers.items()},
                body=body,
            )
        )
        command_uuids = [command["uuid"] for command in self.calls[-1].commands]
        if self.responder is not None:
            return self.responder(command_uuids)
        return ok_response(command_uuids)


def ok_response(command_uuids: list[str]) -> httpx.Response:
    """The documented success shape: every command acknowledged with ``"ok"``."""
    return httpx.Response(
        200,
        json={
            "sync_token": "cdTUEvJoChiaMysD7vJ14UnhN-FRdP-IS3aisOUpl3aGlIQA9qosBgvMmhbn",
            "sync_status": dict.fromkeys(command_uuids, "ok"),
            "temp_id_mapping": {},
            "full_sync": False,
        },
    )


@pytest.fixture
def todoist() -> TodoistDouble:
    return TodoistDouble()


@pytest.fixture
def export_settings() -> TodoExportSettings:
    return TodoExportSettings(timeout_seconds=1.0, max_response_bytes=64 * 1024)


def make_export(*names: str, export_id: uuid.UUID | None = None) -> ShoppingListExport:
    """A small export, with one quantified line so the rendering is exercised too."""
    return ShoppingListExport(
        export_id=export_id or uuid.UUID(int=42),
        shopping_list_id=uuid.UUID(int=1),
        shopping_list_name="Courses",
        lines=tuple(
            ExportLine(name=name, quantity=Decimal("2"), unit_symbol="kg")
            if index == 0
            else ExportLine(name=name)
            for index, name in enumerate(names)
        ),
        generated_at=dt.datetime(2026, 8, 4, 9, 0, tzinfo=dt.UTC),
    )
