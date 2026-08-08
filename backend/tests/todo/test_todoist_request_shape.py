"""What the Todoist adapter actually puts on the wire.

Every assertion here corresponds to a line of the official reference read on
2026-08-04, quoted in ``infra/todo/todoist.py``. This file exists because of a
specific past failure on this project: an adapter written from memory POSTed to
Ollama's ``/api/version``, which answers ``405``. A double written from the same
memory would have agreed with it. So these tests assert the *documented* shape,
and the docstring of each says which line of the documentation it stands for.
"""

from __future__ import annotations

import uuid

import httpx
import pytest

from chaudron.domain.shopping_export import (
    ExportTargetRejected,
    ShoppingExportPartiallyApplied,
    ShoppingExportQuotaExceeded,
    ShoppingExportResponseInvalid,
    ShoppingExportUnavailable,
)
from chaudron.infra.todo.settings import MAX_COMMANDS_PER_REQUEST, TodoExportSettings
from chaudron.infra.todo.todoist import TodoistExporter, build_todoist_client
from tests.todo.conftest import FAKE_TOKEN, RecordedCall, TodoistDouble, make_export


def _exporter(
    todoist: TodoistDouble,
    settings: TodoExportSettings,
    *,
    project_id: str | None = None,
) -> TodoistExporter:
    return TodoistExporter(
        build_todoist_client(FAKE_TOKEN, settings, transport=todoist.transport()),
        project_id=project_id,
    )


async def test_the_whole_list_goes_in_one_call_to_the_documented_endpoint(
    todoist: TodoistDouble, export_settings: TodoExportSettings
) -> None:
    """``POST /api/v1/sync`` -- one request for a list, not one per item."""
    receipt = await _exporter(todoist, export_settings).send(
        make_export("Pommes de terre", "Lait", "Pain")
    )

    assert len(todoist.calls) == 1, "one call for the whole list is the point of /sync"
    call = todoist.calls[0]
    assert call.method == "POST"
    assert call.path == "/api/v1/sync"
    assert receipt.accepted_line_count == 3
    assert receipt.target == "todoist"


async def test_authentication_is_a_bearer_header(
    todoist: TodoistDouble, export_settings: TodoExportSettings
) -> None:
    """ "Authorization: Bearer {token}" -- and the token appears nowhere else."""
    await _exporter(todoist, export_settings).send(make_export("Lait"))

    call = todoist.calls[0]
    assert call.headers["authorization"] == f"Bearer {FAKE_TOKEN}"
    assert FAKE_TOKEN not in call.body, "the credential belongs in the header, not the body"


async def test_the_body_is_form_encoded_with_commands_as_json(
    todoist: TodoistDouble, export_settings: TodoExportSettings
) -> None:
    """The documented content type is ``application/x-www-form-urlencoded``.

    ``commands`` is a JSON array carried in a form field -- not a JSON request
    body with a ``commands`` key, which is the shape a plausible guess produces.
    """
    await _exporter(todoist, export_settings).send(make_export("Lait"))

    call = todoist.calls[0]
    assert call.headers["content-type"].startswith("application/x-www-form-urlencoded")
    assert set(call.form) == {"commands"}
    assert isinstance(call.commands, list)


async def test_a_write_sends_neither_sync_token_nor_resource_types(
    todoist: TodoistDouble, export_settings: TodoExportSettings
) -> None:
    """The published write example sends only ``commands``.

    Sending ``sync_token`` would turn a write into a read and return the
    account's entire dataset -- slow, and more of somebody's life than this
    application has any business receiving.
    """
    await _exporter(todoist, export_settings).send(make_export("Lait"))

    form = todoist.calls[0].form
    assert "sync_token" not in form
    assert "resource_types" not in form


async def test_each_item_is_an_item_add_command_carrying_the_rendered_line(
    todoist: TodoistDouble, export_settings: TodoExportSettings
) -> None:
    """``item_add``, whose required argument is ``content``.

    The content is the same string the plain-text export renders, so an item
    reads identically in the share sheet and in Todoist.
    """
    await _exporter(todoist, export_settings).send(make_export("Pommes de terre", "Pain"))

    commands = todoist.calls[0].commands
    assert [command["type"] for command in commands] == ["item_add", "item_add"]
    assert [command["args"]["content"] for command in commands] == ["Pommes de terre 2 kg", "Pain"]
    for command in commands:
        assert uuid.UUID(command["uuid"]), "every command carries its own uuid"


async def test_a_project_is_sent_only_when_one_is_configured(
    todoist: TodoistDouble, export_settings: TodoExportSettings
) -> None:
    """``project_id`` is optional; omitting it is what puts items in the Inbox."""
    await _exporter(todoist, export_settings).send(make_export("Lait"))
    assert "project_id" not in todoist.calls[0].commands[0]["args"]

    await _exporter(todoist, export_settings, project_id="6HWcc9PJCvPjCxC9").send(
        make_export("Lait")
    )
    assert todoist.calls[1].commands[0]["args"]["project_id"] == "6HWcc9PJCvPjCxC9"


async def test_command_uuids_are_stable_for_the_same_export(
    todoist: TodoistDouble, export_settings: TodoExportSettings
) -> None:
    """Todoist documents ``uuid`` as carrying idempotency, so it must be derived.

    A client that resends an export it already sent -- it holds the identifier,
    the response returned it -- must not get a second copy of its groceries.
    """
    export_id = uuid.uuid4()
    await _exporter(todoist, export_settings).send(make_export("Lait", export_id=export_id))
    await _exporter(todoist, export_settings).send(make_export("Lait", export_id=export_id))

    first, second = todoist.calls
    assert [c["uuid"] for c in first.commands] == [c["uuid"] for c in second.commands]

    await _exporter(todoist, export_settings).send(make_export("Lait", export_id=uuid.uuid4()))
    assert todoist.calls[2].commands[0]["uuid"] != first.commands[0]["uuid"]


async def test_an_empty_list_costs_no_request(
    todoist: TodoistDouble, export_settings: TodoExportSettings
) -> None:
    """A request that can only answer "nothing happened" is not worth a round trip."""
    receipt = await _exporter(todoist, export_settings).send(make_export())

    assert todoist.calls == []
    assert receipt.accepted_line_count == 0


async def test_a_long_list_is_batched_rather_than_refused(
    todoist: TodoistDouble, export_settings: TodoExportSettings
) -> None:
    """The published limit did not render, so the bound is ours -- and it is a bound,
    not a refusal: a household with a long list still gets its list."""
    names = [f"Article {index}" for index in range(MAX_COMMANDS_PER_REQUEST + 5)]
    receipt = await _exporter(todoist, export_settings).send(make_export(*names))

    assert len(todoist.calls) == 2
    assert len(todoist.calls[0].commands) == MAX_COMMANDS_PER_REQUEST
    assert len(todoist.calls[1].commands) == 5
    assert receipt.accepted_line_count == len(names)


# --------------------------------------------------------------------------- #
# Reading the answer
# --------------------------------------------------------------------------- #


async def test_a_rejected_command_is_not_reported_as_a_success(
    todoist: TodoistDouble, export_settings: TodoExportSettings
) -> None:
    """A 200 says the request was understood, not that the commands succeeded.

    ``sync_status`` carries one entry per command. Reading only the status code
    is how an export reports success having added nothing.
    """

    def half_fails(command_uuids: list[str]) -> httpx.Response:
        head, *tail = command_uuids
        status: dict[str, object] = {head: "ok"}
        status.update(
            {command_uuid: {"error": "Invalid argument", "error_code": 15} for command_uuid in tail}
        )
        return httpx.Response(200, json={"sync_status": status, "temp_id_mapping": {}})

    todoist.responder = half_fails

    with pytest.raises(ShoppingExportPartiallyApplied) as raised:
        await _exporter(todoist, export_settings).send(make_export("Lait", "Pain", "Sel"))

    assert raised.value.accepted == 1
    assert raised.value.rejected == 2


async def test_a_command_missing_from_sync_status_counts_as_rejected(
    todoist: TodoistDouble, export_settings: TodoExportSettings
) -> None:
    """An item nobody can account for did not reach anyone's list."""
    todoist.responder = lambda command_uuids: httpx.Response(
        200, json={"sync_status": {command_uuids[0]: "ok"}, "temp_id_mapping": {}}
    )

    with pytest.raises(ShoppingExportPartiallyApplied) as raised:
        await _exporter(todoist, export_settings).send(make_export("Lait", "Pain"))

    assert (raised.value.accepted, raised.value.rejected) == (1, 1)


async def test_an_answer_without_sync_status_is_refused(
    todoist: TodoistDouble, export_settings: TodoExportSettings
) -> None:
    todoist.responder = lambda _uuids: httpx.Response(200, json={"sync_token": "x"})

    with pytest.raises(ShoppingExportResponseInvalid):
        await _exporter(todoist, export_settings).send(make_export("Lait"))


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, ExportTargetRejected),
        (403, ExportTargetRejected),
        (429, ShoppingExportQuotaExceeded),
        (500, ShoppingExportUnavailable),
        (418, ShoppingExportUnavailable),
    ],
)
async def test_http_failures_become_domain_errors(
    todoist: TodoistDouble,
    export_settings: TodoExportSettings,
    status: int,
    expected: type[Exception],
) -> None:
    todoist.responder = lambda _uuids: httpx.Response(status, text="nope")

    with pytest.raises(expected):
        await _exporter(todoist, export_settings).send(make_export("Lait"))


async def test_a_redirect_is_a_failure_rather_than_a_hop(
    todoist: TodoistDouble, export_settings: TodoExportSettings
) -> None:
    """Following it would send the bearer token to whatever host answered."""
    todoist.responder = lambda _uuids: httpx.Response(
        302, headers={"Location": "https://elsewhere.example/api/v1/sync"}
    )

    with pytest.raises(ShoppingExportUnavailable):
        await _exporter(todoist, export_settings).send(make_export("Lait"))

    assert len(todoist.calls) == 1, "the redirect was not followed"


async def test_a_response_past_the_ceiling_is_abandoned(todoist: TodoistDouble) -> None:
    """A hostile or broken endpoint must not be able to exhaust this process."""
    settings = TodoExportSettings(timeout_seconds=1.0, max_response_bytes=64)
    todoist.responder = lambda _uuids: httpx.Response(200, text="x" * 4096)

    with pytest.raises(ShoppingExportResponseInvalid, match="ceiling"):
        await _exporter(todoist, settings).send(make_export("Lait"))


async def test_a_timeout_is_translated_not_propagated(export_settings: TodoExportSettings) -> None:
    """No ``httpx`` exception crosses the boundary: it renders the request it failed
    on, and that request carries the ``Authorization`` header."""

    def time_out(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=_request)

    exporter = TodoistExporter(
        build_todoist_client(FAKE_TOKEN, export_settings, transport=httpx.MockTransport(time_out))
    )

    with pytest.raises(ShoppingExportUnavailable) as raised:
        await exporter.send(make_export("Lait"))

    assert raised.value.__cause__ is None


def test_the_recorded_call_helper_decodes_what_the_adapter_sent() -> None:
    """Guard on the fixture itself: a decoder that lies makes every test above vacuous."""
    call = RecordedCall(method="POST", path="/api/v1/sync", headers={}, body="commands=%5B%5D")
    assert call.commands == []
