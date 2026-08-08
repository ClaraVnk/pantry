"""The CalDAV publication of expiry alerts: credentials, iCalendar, WebDAV XML.

Split by concern rather than by layer, because each piece has a different reason
to change: the credential derivation follows the security model, the iCalendar
writer follows RFC 5545, and the XML helpers follow RFC 4791 and RFC 6578.
"""

from __future__ import annotations

from chaudron.infra.calendar.credentials import (
    FEED_ID_LENGTH,
    FEED_SECRET_LENGTH,
    FeedCredentials,
    FeedKeyring,
)
from chaudron.infra.calendar.ical import CALENDAR_CONTENT_TYPE, VTodo, render_calendar
from chaudron.infra.calendar.repository import ExpiringLot, SqlExpiringLotReader

__all__ = [
    "CALENDAR_CONTENT_TYPE",
    "FEED_ID_LENGTH",
    "FEED_SECRET_LENGTH",
    "ExpiringLot",
    "FeedCredentials",
    "FeedKeyring",
    "SqlExpiringLotReader",
    "VTodo",
    "render_calendar",
]
