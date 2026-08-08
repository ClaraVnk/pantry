"""Redaction as a property of the log transport, not of whoever wrote the line.

Every other no-leak suite in this repository proves that one adapter does not
quote a credential. This one proves the complement: that *nothing* reaches a log
line without passing the scrubber, whoever emitted it and whatever they forgot.

The distinction is the finding. Redaction used to be a discipline of each call
site -- ``snippet(text, secrets=...)`` here, ``redact(str(exc))`` there -- and the
security review found the discipline broken at three of the four sites that
needed it, including the one that writes to the log. A control that holds only
when every author remembers it is not a control; the formatter is where it
becomes one, because a leak now needs the formatter itself to be wrong.

What this cannot do, and the reason ``infra/db.py`` sets ``hide_parameters=True``:
redaction recognises *shapes*. A member's first name in a bound parameter has none,
and no pattern here will ever catch it.

The one piece of business data that does have a shape is covered here rather than
there, because ``hide_parameters`` cannot reach it: PostgreSQL answers a constraint
violation by quoting the offending row back in the ``DETAIL`` field, which asyncpg
appends to ``str(exc)``. That text is composed by the server, not by the driver.
"""

from __future__ import annotations

import base64
import json
import logging
import secrets
import string
import uuid
from typing import Any

import pytest

from chaudron.infra.logging import JsonFormatter

_FORMATTER = JsonFormatter()

#: One realistic value per credential this application handles, generated rather
#: than hard-coded so a pattern that only matches the literal in a fixture fails.
_MISTRAL_KEY = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(32))
_TODOIST_TOKEN = secrets.token_hex(20)
_FEED_SECRET = base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")
_ANTHROPIC_KEY = "sk-ant-api03-" + secrets.token_urlsafe(24)

#: The shapes the pattern layer used to pass through intact, and the finding.
#:
#: The classifier demanded all three character classes of an unprefixed key, and
#: the probabilistic argument for that ("a randomly drawn key misses a class with
#: probability below 1e-7") only ever held for a mixed-case alphabet. Plenty of
#: providers issue lower-case hex or lower-case alphanumeric keys, and every one of
#: those reached the log file verbatim -- at exactly the call sites that cannot know
#: what they are holding, which is what this layer exists for.
_LOWERCASE_ALPHANUMERIC_KEY = "".join(
    secrets.choice(string.ascii_lowercase + string.digits) for _ in range(32)
)
_LOWERCASE_HEX_KEY = secrets.token_hex(16)
_LOWERCASE_LETTERS_KEY = "".join(secrets.choice(string.ascii_lowercase) for _ in range(32))
#: Upper case with a digit outside the base32 alphabet, which the base32 rule
#: therefore did not catch either.
_UPPERCASE_KEY = "".join(secrets.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ0189") for _ in range(32))

_CREDENTIALS = {
    "mistral": _MISTRAL_KEY,
    "todoist": _TODOIST_TOKEN,
    "calendar_feed": _FEED_SECRET,
    "anthropic": _ANTHROPIC_KEY,
    "lowercase_alphanumeric": _LOWERCASE_ALPHANUMERIC_KEY,
    "lowercase_hex": _LOWERCASE_HEX_KEY,
    "lowercase_letters": _LOWERCASE_LETTERS_KEY,
    "uppercase_alphanumeric": _UPPERCASE_KEY,
}


def _emit(message: str, **extra: Any) -> str:
    record = logging.LogRecord("chaudron.test", logging.WARNING, __file__, 1, message, None, None)
    record.__dict__.update(extra)
    return _FORMATTER.format(record)


def _emit_exception(exc: BaseException) -> str:
    try:
        raise exc
    except BaseException:
        record = logging.LogRecord(
            "chaudron.test", logging.ERROR, __file__, 1, "unhandled_exception", None, None
        )
        import sys

        record.exc_info = sys.exc_info()
        return _FORMATTER.format(record)


@pytest.mark.parametrize("name", sorted(_CREDENTIALS))
def test_a_credential_in_the_message_never_reaches_the_line(name: str) -> None:
    secret = _CREDENTIALS[name]
    line = _emit(f"provider rejected {secret}")

    assert secret not in line
    assert "[redacted]" in line


@pytest.mark.parametrize("name", sorted(_CREDENTIALS))
def test_a_credential_in_an_extra_never_reaches_the_line(name: str) -> None:
    """``extra=`` is the field the application actually uses for diagnostics."""
    secret = _CREDENTIALS[name]
    line = _emit("provider_failure", detail=f"key {secret} is invalid")

    assert secret not in line
    assert json.loads(line)["detail"].endswith("is invalid")


@pytest.mark.parametrize("name", sorted(_CREDENTIALS))
def test_a_credential_in_a_percent_argument_never_reaches_the_line(name: str) -> None:
    """``logger.warning("%s failed", value)`` -- the form ``getMessage`` renders."""
    secret = _CREDENTIALS[name]
    record = logging.LogRecord(
        "chaudron.test", logging.WARNING, __file__, 1, "call to %s failed", (secret,), None
    )

    assert secret not in _FORMATTER.format(record)


@pytest.mark.parametrize("name", sorted(_CREDENTIALS))
def test_a_credential_in_a_traceback_never_reaches_the_line(name: str) -> None:
    """The richest thing this process writes, and the one nobody composed by hand.

    ``api/errors.py`` logs every unhandled exception with ``exc_info``; an SDK that
    embeds the key it was called with in its own message lands here.
    """
    secret = _CREDENTIALS[name]
    line = _emit_exception(RuntimeError(f"authentication failed for {secret}"))

    assert secret not in line
    assert "RuntimeError" in line, "the diagnostic survives; only the credential does not"


def test_a_credential_nested_in_a_structured_extra_is_still_removed() -> None:
    """Structured ``extra=`` values are walked, not stringified and hoped about."""
    line = _emit(
        "provider_failure",
        context={"headers": {"authorization": f"Bearer {_MISTRAL_KEY}"}, "attempts": [1, 2]},
    )

    assert _MISTRAL_KEY not in line
    payload = json.loads(line)
    assert payload["context"]["attempts"] == [1, 2], "the shape is preserved"


def test_an_object_extra_is_rendered_and_scrubbed_rather_than_passed_through() -> None:
    """``json.dumps(default=str)`` used to stringify these *after* any scrubbing."""

    class _Opaque:
        def __str__(self) -> str:
            return f"Opaque(key={_ANTHROPIC_KEY})"

    line = _emit("provider_failure", client=_Opaque())

    assert _ANTHROPIC_KEY not in line


def test_the_line_is_still_one_json_object() -> None:
    """Redaction must not be able to break the format it is applied to."""
    line = _emit(
        "provider_failure",
        detail=f'{{"quoted": "{_ANTHROPIC_KEY}"}}',
        provider="mistral",
        attempt=2,
        succeeded=False,
        missing=None,
    )

    payload = json.loads(line)
    assert payload["provider"] == "mistral"
    assert payload["attempt"] == 2
    assert payload["succeeded"] is False
    assert payload["missing"] is None
    assert "\n" not in line


def test_identifiers_survive_so_a_line_stays_diagnosable() -> None:
    """A scrubber that blanked the whole line would pass every test above.

    These are the values an operator correlates on: the household, the request,
    the provider and model, and the calendar feed *identifier* -- which is not a
    secret (``infra/calendar/credentials.py``: it names no household and
    authorises nothing on its own).
    """
    feed_id = base64.b32encode(secrets.token_bytes(16)).decode().rstrip("=")
    household = uuid.uuid7()
    line = _emit(
        "calendar_feed_polled",
        household_id=str(household),
        feed_id=feed_id,
        provider="mistral",
        model="mistral-small-latest",
        request_id=str(uuid.uuid4()),
    )

    payload = json.loads(line)
    assert payload["household_id"] == str(household)
    assert payload["feed_id"] == feed_id
    assert payload["model"] == "mistral-small-latest"


def test_a_long_alphanumeric_blob_does_not_cost_more_than_its_length() -> None:
    """Log text is partly attacker-influenced, so the scrubber must stay linear.

    The lookahead form of the opaque-token pattern rescanned the run from every
    position it failed at, which is quadratic: a 100 kB alphanumeric blob in an
    ``extra=`` -- a poisoned catalogue label, a model's answer -- would have
    turned one log line into hundreds of milliseconds. This is a bound, not a
    benchmark: it is two orders of magnitude above the linear cost and would only
    fail on a return to quadratic behaviour.
    """
    import time

    blob = "x" * 200_000
    started = time.perf_counter()
    _emit("upstream_body", body=blob)
    elapsed = time.perf_counter() - started

    assert elapsed < 2.0, f"{elapsed:.2f}s to format one line of {len(blob)} characters"


def test_a_postgres_detail_line_does_not_cost_more_than_its_length() -> None:
    """The same bound, for the pattern that strips PostgreSQL's row echo.

    The literal it keys on -- ``Failing row contains (`` -- is attacker-suppliable
    (a product label, a model's answer), so a lazy or backtracking form of that
    pattern would be quadratic in a body that repeats it. Both patterns use nothing
    but negated character classes anchored to a line, so they cannot backtrack.
    """
    import time

    blob = "Failing row contains (" * 10_000
    started = time.perf_counter()
    _emit("upstream_body", body=blob)
    elapsed = time.perf_counter() - started

    assert elapsed < 2.0, f"{elapsed:.2f}s to format one line of {len(blob)} characters"


# --------------------------------------------------------------------------- #
# The cost, measured rather than asserted away
# --------------------------------------------------------------------------- #
#
# Covering the lower-case shapes means length alone now decides, and length alone
# catches things that are not credentials. That is a diagnostic cost and it is
# real; what follows is what it actually amounts to, so the next person weighing
# it does not have to guess.


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("uuid without its dashes", uuid.uuid7().hex),
        ("sha-256 digest", "e" * 64),
        ("git commit", "a1b2c3d4" * 5),
        # Base64 without ``+`` or ``/``, which both break a run: the fragment that
        # actually survives to be matched is the alphanumeric stretch between them.
        ("base64 body fragment", base64.b32encode(secrets.token_bytes(30)).decode()),
    ],
)
def test_what_length_alone_also_blanks(name: str, value: str) -> None:
    """None of these is a secret. All of them disappear, and knowingly."""
    assert "[redacted]" in _emit("diagnostic", value=value), name


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("a dashed uuid -- the form every log line here emits", str(uuid.uuid7())),
        ("a calendar feed identifier, 26 characters", "A" * 26),
        ("a constraint name", "ck_inventory_lot_quantity_positive"),
        ("a table-qualified column", "shopping_list_item.origin_recipe_suggestion_id"),
        ("a model name", "mistral-small-latest"),
        ("a long dotted module path", "chaudron.infra.repositories.inventory"),
    ],
)
def test_what_length_alone_leaves_alone(name: str, value: str) -> None:
    """The control. ``_``, ``-`` and ``.`` all break a run, which is why every
    identifier this application writes down survives a rule keyed on length."""
    payload = json.loads(_emit("diagnostic", value=value))
    assert payload["value"] == value, name


# --------------------------------------------------------------------------- #
# PostgreSQL's row echo
# --------------------------------------------------------------------------- #


def test_a_failing_row_echoed_by_postgres_never_reaches_the_line() -> None:
    """``hide_parameters=True`` hides SQLAlchemy's parameters and not this.

    The text is the *server's*: a CHECK or NOT NULL violation is answered with
    ``DETAIL:  Failing row contains (...)``, every column of the row, and asyncpg
    appends it to ``str(exc)``. ``tests/infra/test_no_database_leaks.py`` proves it
    against a real PostgreSQL; this proves the transport stops it wherever it comes
    from.
    """
    detail = (
        'new row for relation "inventory_lot" violates check constraint '
        '"ck_inventory_lot_quantity_positive"\n'
        "DETAIL:  Failing row contains (0198f0, 0198f1, Yaourt nature, 0.000, g, "
        "mass, 2026-01-04, 2.49, EUR, Frigo)."
    )
    line = _emit_exception(RuntimeError(detail))

    for value in ("Yaourt nature", "2.49", "EUR", "Frigo", "2026-01-04"):
        assert value not in line, f"{value!r} reached the log line"
    assert "ck_inventory_lot_quantity_positive" in line, "the constraint is what gets it fixed"
    assert "inventory_lot" in line


def test_a_unique_violation_keeps_the_column_names_and_drops_the_values() -> None:
    """The other form the server uses, for UNIQUE and for foreign keys.

    The column names are the diagnostic -- they say *which* uniqueness rule -- and
    the values are the row.
    """
    detail = (
        'duplicate key value violates unique constraint "uq_product_gtin_global"\n'
        "DETAIL:  Key (household_id, gtin)=(0198f0aa-0000-7000-8000-000000000001, "
        "3017620422003) already exists."
    )
    line = _emit_exception(RuntimeError(detail))

    assert "3017620422003" not in line
    assert "0198f0aa" not in line
    assert "Key (household_id, gtin)=[redacted]" in line
    assert "uq_product_gtin_global" in line
