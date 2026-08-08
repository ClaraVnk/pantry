"""The bytes a client downloads, checked by a parser this project did not write.

Every assertion that matters here goes through ``icalendar`` rather than through
a substring search: a serialiser verified against its own idea of the format is
verified against nothing.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from icalendar import Calendar

from chaudron.infra.calendar.ical import VTodo, fold, render_calendar

MODIFIED = datetime(2026, 8, 1, 12, 30, 0, tzinfo=UTC)
CREATED = datetime(2026, 7, 20, 8, 0, 0, tzinfo=UTC)


def a_todo(**overrides: object) -> VTodo:
    defaults: dict[str, object] = {
        "uid": "ABCDEFGHIJKLMNOPQRSTUVWXYZ@chaudron",
        "summary": "Lait demi-écrémé — 1 L",
        "due": date(2026, 8, 10),
        "location": "Frigo",
        "created": CREATED,
        "modified": MODIFIED,
        "alarm_at": None,
    }
    defaults.update(overrides)
    return VTodo(**defaults)  # type: ignore[arg-type]


def parse(payload: str) -> Calendar:
    return Calendar.from_ical(payload)


def test_a_rendered_task_is_a_vtodo_a_real_parser_accepts() -> None:
    calendar = parse(render_calendar([a_todo()]))
    (todo,) = calendar.walk("VTODO")
    assert str(todo["SUMMARY"]) == "Lait demi-écrémé — 1 L"
    assert str(todo["LOCATION"]) == "Frigo"
    assert todo["STATUS"] == "NEEDS-ACTION"


def test_the_due_date_is_a_date_not_a_datetime() -> None:
    """An all-day due date is what a task has; a time would invent precision."""
    (todo,) = parse(render_calendar([a_todo()])).walk("VTODO")
    assert todo["DUE"].dt == date(2026, 8, 10)


def test_a_task_without_a_location_omits_the_property() -> None:
    (todo,) = parse(render_calendar([a_todo(location=None)])).walk("VTODO")
    assert "LOCATION" not in todo


def test_an_alarm_is_emitted_as_an_absolute_trigger() -> None:
    alarm_at = datetime(2026, 8, 9, 7, 0, 0, tzinfo=UTC)
    (todo,) = parse(render_calendar([a_todo(alarm_at=alarm_at)])).walk("VTODO")
    (alarm,) = todo.walk("VALARM")
    assert alarm["ACTION"] == "DISPLAY"
    assert alarm["TRIGGER"].dt == alarm_at


def test_no_alarm_means_no_valarm_component() -> None:
    (todo,) = parse(render_calendar([a_todo()])).walk("VTODO")
    assert todo.walk("VALARM") == []


def test_timestamps_come_from_the_row_not_the_clock() -> None:
    """The ETag is a hash of these bytes; a clock in them would defeat caching."""
    first = render_calendar([a_todo()])
    second = render_calendar([a_todo()])
    assert first == second
    (todo,) = parse(first).walk("VTODO")
    assert todo["DTSTAMP"].dt == MODIFIED
    assert todo["CREATED"].dt == CREATED


def test_a_naive_timestamp_is_refused() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        render_calendar([a_todo(modified=datetime(2026, 8, 1, 12, 0, 0))])  # noqa: DTZ001


def test_every_line_is_folded_to_75_octets() -> None:
    payload = render_calendar([a_todo(summary="Confiture d'abricots de Provence " * 6)])
    for line in payload.split("\r\n"):
        assert len(line.encode("utf-8")) <= 75


def test_folding_never_splits_a_utf8_sequence() -> None:
    """Folding on octets without care turns an accent into two broken bytes."""
    folded = fold("SUMMARY:" + "é" * 80)
    for segment in folded:
        segment.encode("utf-8").decode("utf-8")
    assert "".join(part.removeprefix(" ") for part in folded) == "SUMMARY:" + "é" * 80


def test_a_product_name_cannot_forge_a_property() -> None:
    """Open Food Facts is a wiki; a contributor writes into every household.

    A name carrying a line break and a fake property must come back as *text*,
    not as structure -- that is the whole reason the renderer sanitises before it
    escapes.
    """
    hostile = "Yaourt\r\nX-EVIL:1\r\nSUMMARY:Injected"
    calendar = parse(render_calendar([a_todo(summary=hostile)]))
    (todo,) = calendar.walk("VTODO")
    assert "X-EVIL" not in todo
    assert "Injected" in str(todo["SUMMARY"])
    assert len(calendar.walk("VTODO")) == 1


def test_separators_inside_a_name_are_escaped() -> None:
    (todo,) = parse(render_calendar([a_todo(summary="Sel, poivre; épices")])).walk("VTODO")
    assert str(todo["SUMMARY"]) == "Sel, poivre; épices"


def test_invisible_characters_are_dropped() -> None:
    """Tag-block smuggling: text a human cannot see and a parser reads fine."""
    smuggled = "Lait​\U000e0041\U000e0042"
    (todo,) = parse(render_calendar([a_todo(summary=smuggled)])).walk("VTODO")
    assert str(todo["SUMMARY"]) == "Lait"


def test_an_empty_collection_is_still_a_valid_calendar() -> None:
    calendar = parse(render_calendar([]))
    assert calendar.walk("VTODO") == []
