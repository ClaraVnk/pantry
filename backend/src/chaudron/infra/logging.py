"""Structured logging: one JSON object per line, with the request context attached.

``docs/architecture.md`` section 7 asks for ``household_id`` and a request
identifier on every line. Carrying them as function arguments would mean
threading a logger through every call; context variables put them on the
records emitted by libraries too, which is where the interesting lines usually
come from.

**Everything written here goes through :func:`redact` first**, and that is a
security control rather than a tidiness one. Redaction used to be a discipline of
each call site: whoever built a message was expected to scrub it, and a single
site that forgot -- one ``snippet()`` without ``secrets=``, one third-party log
line nobody wrote -- put a credential on disk forever. Applying it at the
transport makes it an invariant: a leak now needs the *formatter* to be wrong, not
any one of the several dozen places that log.

What it cannot do is recognise a secret it has no shape for, which is why the
literal ``secrets=`` path upstream still matters, and why ``infra/db.py`` sets
``hide_parameters=True`` -- business data in a traceback (a member's name, an
allergen) is not credential-shaped and no pattern will ever catch it.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Mapping, Sequence
from contextvars import ContextVar
from typing import Any, Final

from chaudron.infra.redaction import redact

request_id_var: ContextVar[str | None] = ContextVar("chaudron_request_id", default=None)
household_id_var: ContextVar[str | None] = ContextVar("chaudron_household_id", default=None)

#: Attributes ``logging`` puts on every record; anything else was passed by the
#: caller through ``extra=`` and belongs in the output.
_STANDARD_ATTRS: Final[frozenset[str]] = frozenset(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__
) | {"message", "asctime", "taskName"}


#: How deep :func:`_scrub` walks a structured ``extra=`` before it stops
#: descending and renders what is left as text. Four levels covers every shape the
#: application logs; the bound exists so a self-referential or pathologically
#: nested value cannot turn one log line into a stack overflow.
_MAX_SCRUB_DEPTH: Final = 4


def _scrub(value: Any, depth: int = 0) -> Any:
    """Redact every string reachable from an ``extra=`` value, keeping its shape.

    Numbers and booleans are returned untouched -- no credential is an ``int``,
    and converting them would change the type a log pipeline indexes on. Anything
    that is neither a container nor a scalar is rendered with ``str`` and scrubbed:
    that is what ``json.dumps(default=str)`` would have done with it anyway, so the
    output is unchanged and the text now passes through the filter.
    """
    if isinstance(value, str):
        return redact(value)
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, bytes | bytearray):
        # A `Sequence` too, and one that would otherwise be rendered as a list of
        # integers -- unreadable, and no longer scrubbable by anything downstream.
        return redact(str(value))
    if depth >= _MAX_SCRUB_DEPTH:
        return redact(str(value))
    if isinstance(value, Mapping):
        return {redact(str(key)): _scrub(item, depth + 1) for key, item in value.items()}
    if isinstance(value, Sequence | set | frozenset):
        return [_scrub(item, depth + 1) for item in value]
    return redact(str(value))


class JsonFormatter(logging.Formatter):
    """Render a record as a single JSON line, with every string redacted.

    The redaction covers the three ways text reaches a record: the message and its
    ``%``-arguments (through ``getMessage``), the caller's ``extra=`` fields, and
    the formatted traceback -- which is the one that carries an SDK's exception
    text, and therefore the credential that SDK was called with.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage()),
        }
        request_id = request_id_var.get()
        if request_id is not None:
            payload["request_id"] = request_id
        household_id = household_id_var.get()
        if household_id is not None:
            payload["household_id"] = household_id
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS:
                payload[key] = _scrub(value)
        if record.exc_info is not None:
            # Server-side only: the client never sees a traceback (RFC 9457
            # handler in `chaudron.api.errors`). It is still the richest thing
            # this process writes to disk, so it is scrubbed like everything else.
            payload["exception"] = redact(self.formatException(record.exc_info))
        if record.stack_info:
            payload["stack"] = redact(self.formatStack(record.stack_info))
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(level: str) -> None:
    """Install the JSON formatter on the root logger, replacing any earlier one."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    # uvicorn installs its own colourised handlers; left alone, every startup and
    # error line would be emitted twice, once structured and once not.
    #
    # `uvicorn.access` is deliberately **not** in this list, and its absence is the
    # point rather than an oversight. The entrypoint runs uvicorn with
    # `--no-access-log` (see `Containerfile`), because a request line here carries
    # a scanned product's GTIN in the query string and a calendar feed identifier
    # in the path -- into journald, with no retention, where an article 17 erasure
    # cannot reach it. So the logger emits nothing, and reparenting it would be
    # wiring that looks live and is not.
    #
    # It would also be wiring that reassures for the wrong reason. Everything this
    # formatter writes is redacted, so a reader would reasonably conclude that
    # access lines are safe if they are ever switched back on. They are not:
    # redaction recognises *credential shapes*, and a barcode, a feed identifier
    # and a household identifier have none. The control for those lines is not
    # emitting them.
    for name in ("uvicorn", "uvicorn.error"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
