"""iCalendar (RFC 5545) serialisation of one expiry task.

``VTODO``, not ``VEVENT``, and the reason is what the user is meant to do with
it. "The yoghurt expires tomorrow" is a thing to act on and then tick off, not an
appointment: it has a due date rather than a start and an end, it can be
completed, and it belongs in the list a person checks before cooking rather than
in the row of meetings they are already looking at. RFC 5545 section 3.6.2 is
exactly that component, and iOS Reminders, Thunderbird, jtx Board, OpenTasks,
Tasks.org, Nextcloud Tasks and Evolution all consume it over CalDAV.

The cost of the choice is named rather than hidden: **Google Calendar's CalDAV
interface refuses ``VTODO`` outright** ("Doesn't support VTODO or VJOURNAL
data"), and so does an ``.ics`` import there. A household that lives in Google
Calendar sees nothing. That is a real gap, and it is still the right trade --
rendering expiry alerts as all-day ``VEVENT``s would land them in the agenda
beside actual appointments, where they cannot be ticked off and where a
fortnight of groceries buries the week.

Three details of this file are not decoration:

* **Every text value goes through :func:`chaudron.infra.untrusted_text.sanitize`
  first.** Product names come from Open Food Facts, which anybody may edit
  (ADR-0008, security audit AUD-006). A newline in a product name is a forged
  iCalendar property; ``sanitize`` makes a value one bounded line, and the
  escaping below then makes it one bounded *property value*.
* **Nothing here reads the clock.** The bytes a poll produces depend only on the
  row, or the ETag changes on every request and every client re-downloads the
  whole collection every fifteen minutes forever.
* **Lines are folded at 75 octets**, on boundaries that never split a UTF-8
  sequence. A long product name is the normal case, not the edge case.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Final

from chaudron.infra.untrusted_text import sanitize

#: What CalDAV asks a calendar object resource to be served as (RFC 4791 §5.2.5).
CALENDAR_CONTENT_TYPE: Final = 'text/calendar; charset=utf-8; component="VTODO"'

#: RFC 5545 §3.1: no line exceeds 75 octets, excluding the line break.
_FOLD_OCTETS: Final = 75

#: Bound on any text value before escaping. Long enough for a real product name
#: with its packaging size, short enough that two hundred tasks stay a payload a
#: phone can fetch over a slow connection.
TEXT_LIMIT: Final = 120

_PRODID: Final = "-//Chaudron//Expiry feed//EN"

#: The only ``STATUS`` this feed emits. A completed task is a client-side state;
#: the feed is read-only, so it never learns about it.
STATUS_NEEDS_ACTION: Final = "NEEDS-ACTION"


def escape_text(value: str) -> str:
    r"""Escape a TEXT value (RFC 5545 §3.3.11).

    The order matters: backslash first, or the escapes introduced afterwards get
    escaped again. ``sanitize`` has already removed line breaks, so the ``\n``
    rule is here for completeness rather than for a case that can arrive.
    """
    return value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def fold(line: str) -> list[str]:
    """Split one content line into folded segments of at most 75 octets.

    Splitting on characters would be wrong for the reason it is always wrong: the
    limit is in octets and one accented character is two of them. Splitting on
    octets without care would be worse -- half a UTF-8 sequence is not text. So
    the walk is over characters, counting their encoded width.
    """
    if len(line.encode("utf-8")) <= _FOLD_OCTETS:
        return [line]

    segments: list[str] = []
    current = ""
    used = 0
    # The first segment may use all 75 octets; every continuation line begins
    # with a space that counts against the same budget.
    budget = _FOLD_OCTETS
    for char in line:
        width = len(char.encode("utf-8"))
        if used + width > budget:
            segments.append(current)
            current = ""
            used = 0
            budget = _FOLD_OCTETS - 1
        current += char
        used += width
    if current:
        segments.append(current)
    return [segments[0], *(f" {segment}" for segment in segments[1:])]


def _property(name: str, value: str, *, params: str = "") -> str:
    return f"{name}{params}:{value}"


def _utc(moment: datetime) -> str:
    """Format an instant as a UTC date-time (RFC 5545 §3.3.5, form 2).

    A naive value would be a bug upstream rather than here -- every column
    involved is ``timestamptz`` -- but formatting one *as if* it were UTC would
    hide that bug for months, so it is refused instead.
    """
    if moment.tzinfo is None:
        raise ValueError("iCalendar timestamps must be timezone-aware")
    return moment.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def _day(value: date) -> str:
    return value.strftime("%Y%m%d")


@dataclass(frozen=True, slots=True)
class VTodo:
    """One task, already reduced to what may leave the home.

    Four fields answer "what, how much, where, when", which is what a person
    needs in order to act on a notification without opening the application.
    Everything else the row carries -- brand, barcode, price, who added it, which
    receipt it came from -- stays in the database. Each field added here is data
    that leaves the household on every poll, onto a device, over a network, into
    somebody's cloud backup.
    """

    uid: str
    summary: str
    due: date
    location: str | None
    created: datetime
    modified: datetime
    #: Absolute instant for the reminder, or ``None`` for no alarm at all.
    alarm_at: datetime | None
    status: str = STATUS_NEEDS_ACTION

    def to_lines(self) -> list[str]:
        summary = escape_text(sanitize(self.summary, limit=TEXT_LIMIT))
        lines = [
            "BEGIN:VTODO",
            _property("UID", self.uid),
            # DTSTAMP is the row's last change, not "now": see the module
            # docstring on why these bytes must not move when the data has not.
            _property("DTSTAMP", _utc(self.modified)),
            _property("CREATED", _utc(self.created)),
            _property("LAST-MODIFIED", _utc(self.modified)),
            _property("SUMMARY", summary),
            _property("DUE", _day(self.due), params=";VALUE=DATE"),
            _property("STATUS", self.status),
        ]
        if self.location is not None:
            location = escape_text(sanitize(self.location, limit=TEXT_LIMIT))
            lines.append(_property("LOCATION", location))
        if self.alarm_at is not None:
            lines.extend(
                [
                    "BEGIN:VALARM",
                    "ACTION:DISPLAY",
                    # The summary again rather than a second wording: whatever a
                    # client shows, it shows the same sentence, and there is no
                    # second string to keep consistent with the first.
                    _property("DESCRIPTION", summary),
                    _property("TRIGGER", _utc(self.alarm_at), params=";VALUE=DATE-TIME"),
                    "END:VALARM",
                ]
            )
        lines.append("END:VTODO")
        return lines


def render_calendar(todos: Sequence[VTodo]) -> str:
    """Wrap tasks in a ``VCALENDAR``, folded and CRLF-terminated.

    RFC 4791 §4.1 forbids mixing component types in one calendar object resource,
    which is why a resource here holds exactly one ``VTODO`` and the collection
    is a list of resources rather than one large file.
    """
    lines: list[str] = ["BEGIN:VCALENDAR", "VERSION:2.0", f"PRODID:{_PRODID}", "CALSCALE:GREGORIAN"]
    for todo in todos:
        lines.extend(todo.to_lines())
    lines.append("END:VCALENDAR")
    return "".join(f"{folded}\r\n" for line in lines for folded in fold(line))
