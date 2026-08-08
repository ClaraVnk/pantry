"""Two things the database layer must never write down: the DSN, and the rows.

The first is the credential this process holds for the whole of its life. It is
not adapter-shaped -- no ``sk-`` prefix, no bearer header -- so none of the other
no-leak suites covers it, and the mitigations it relies on (``echo=False``, a
readiness probe that logs an exception *type*) are one careless keyword argument
away from being undone. Nothing asserted them until this file.

The second is sharper, and it arrives by **two** routes that look like one.

*The driver's route.* SQLAlchemy renders bound parameters into
``str(StatementError)``, and ``api/errors.py`` logs every unhandled exception
with ``exc_info``. So one ``IntegrityError`` on ``household_person`` used to
write that person's name, their allergens and -- for an infant -- their age band
to the log: health data about a minor (GDPR article 9), on disk, under journald's
retention rather than ours. ``hide_parameters=True`` is what stops it.

*The server's route,* which ``hide_parameters`` does not touch and which nothing
here asserted until the constraint classes below were added. PostgreSQL answers a
violation by quoting the offending row back in the ``DETAIL`` field, and asyncpg
appends that field to ``str(exc)``. For a CHECK or a NOT NULL violation the field
is ``Failing row contains (...)`` -- *every column of the row*, composed by the
server, arriving whole however carefully the driver was configured.

The distinction is what this file got wrong for a while, and it is worth naming.
The one violation it provoked was a duplicate primary key, whose ``DETAIL`` is
``Key (id)=(<uuid>) already exists.`` -- the harmless class, because the columns
in a key are identifiers. The three tests passed, the suite was green, and the
class that echoes the *whole row* had no coverage at all. So the next CHECK
constraint added to a table carrying article 9 data would have leaked it with
nothing going red. Every constraint class PostgreSQL can raise is exercised below,
against a table that carries a name, an allergen and a minor's age band.

The diagnostic has to survive all of it. An operator debugging a constraint
violation needs to know which constraint; they never need the value.
"""

from __future__ import annotations

import logging
import sys
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from chaudron.domain.models import Base
from chaudron.infra.db import Database
from chaudron.infra.logging import JsonFormatter
from tests.conftest import build_test_settings

pytestmark = pytest.mark.integration

_FORMATTER = JsonFormatter()

#: Recognisable, and of the three kinds article 9 covers: a name, a health fact,
#: and a minor's age band.
_PERSON_NAME = "Leontine-Recognisable"
_PERSON_ALLERGEN = "peanuts"
_PERSON_BAND = "infant_6_9m"

_HOUSEHOLD = Base.metadata.tables["household"]
_PERSON = Base.metadata.tables["household_person"]


@asynccontextmanager
async def _database(database_url: str) -> AsyncIterator[Database]:
    """The application's own engine, configured exactly as production builds it."""
    database = Database(build_test_settings(database_url))
    try:
        yield database
    finally:
        await database.dispose()


def _log_line(exc: BaseException) -> str:
    """The line ``api/errors.py`` would write for this exception."""
    try:
        raise exc
    except BaseException:
        record = logging.LogRecord(
            "chaudron.api.errors", logging.ERROR, __file__, 1, "unhandled_exception", None, None
        )
        record.exc_info = sys.exc_info()
        return _FORMATTER.format(record)


def _person_row(household_id: uuid.UUID) -> dict[str, object]:
    """A row carrying all three kinds of article 9 data, and valid as it stands."""
    return {
        "id": uuid.uuid7(),
        "household_id": household_id,
        "display_name": _PERSON_NAME,
        "age_band": _PERSON_BAND,
        "allergens": [_PERSON_ALLERGEN],
        "infant_texture": "smooth",
    }


#: One provoker per class of constraint PostgreSQL can raise, because the class is
#: what decides the shape of the ``DETAIL`` field and therefore what leaks. The
#: substring is the fragment of the message an operator diagnoses from, asserted so
#: that a scrubber which blanked everything cannot pass.
#:
#: ``unique`` was, for a while, the only one covered -- and it is the one whose
#: ``DETAIL`` quotes a *key* rather than a row, which is to say the only harmless
#: one. See the module docstring.
_VIOLATIONS: dict[str, str] = {
    "unique": "pk_household_person",
    "check": "infant_texture_band",
    "not_null": "display_name",
    "foreign_key": "household_id",
}


async def _provoke(database: Database, kind: str) -> IntegrityError:
    """Provoke a real violation of ``kind`` on a row carrying real personal data.

    Everything is rolled back: the transaction is never committed, so the suite
    leaves nothing behind (``tests/test_database_harness.py`` would catch it).
    """
    household_id = uuid.uuid7()
    async with database.engine.connect() as connection:
        transaction = await connection.begin()
        try:
            await connection.execute(
                sa.insert(_HOUSEHOLD).values(id=household_id, name="Leak fixture")
            )
            match kind:
                case "unique":
                    # The same primary key twice, which is what a lost race looks
                    # like. `DETAIL: Key (id)=(<uuid>) already exists.`
                    row = _person_row(household_id)
                    await connection.execute(sa.insert(_PERSON).values(**row))
                    bad: dict[str, object] = row
                case "check":
                    # A texture outside an infant band, violating
                    # `ck_household_person_infant_texture_band`.
                    # `DETAIL: Failing row contains (<the whole person>).`
                    bad = {**_person_row(household_id), "age_band": "adult"}
                case "not_null":
                    # `DETAIL: Failing row contains (...)` too, and the row still
                    # holds the allergen and the band even though the name is what
                    # is missing.
                    bad = {**_person_row(household_id), "display_name": None}
                case "foreign_key":
                    # `DETAIL: Key (household_id)=(<uuid>) is not present in ...`
                    bad = {**_person_row(household_id), "household_id": uuid.uuid7()}
                case _:  # pragma: no cover -- guards the table above
                    raise AssertionError(f"unknown violation kind {kind!r}")

            with pytest.raises(IntegrityError) as raised:
                await connection.execute(sa.insert(_PERSON).values(**bad))
            return raised.value
        finally:
            await transaction.rollback()


# --------------------------------------------------------------------------- #
# Every class of constraint, not just the harmless one
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("kind", sorted(_VIOLATIONS))
async def test_a_constraint_violation_writes_no_personal_data_to_the_log(
    kind: str, initialised_database: str
) -> None:
    """The finding, and the ratchet.

    ``hide_parameters=True`` covers the driver's rendering of the bound values and
    is the whole of the protection for a ``unique`` violation. It is *not* the whole
    of it for ``check`` and ``not_null``, where PostgreSQL composes ``Failing row
    contains (...)`` itself: that text is in ``str(exc)`` before any Python here
    touches it, and the transport (``infra/redaction.py``) is what keeps it off
    disk. Both halves are asserted by the same test on purpose -- the property an
    operator cares about is "the log line does not hold it", not which mechanism
    got there.
    """
    async with _database(initialised_database) as database:
        error = await _provoke(database, kind)

    line = _log_line(error)
    for value in (_PERSON_NAME, _PERSON_ALLERGEN, _PERSON_BAND):
        assert value not in line, f"{value!r} reached the log line on a {kind} violation"


@pytest.mark.parametrize("kind", sorted(_VIOLATIONS))
async def test_a_constraint_violation_stays_diagnosable(
    kind: str, initialised_database: str
) -> None:
    """Only the values disappear. Which constraint failed is what gets it fixed."""
    async with _database(initialised_database) as database:
        error = await _provoke(database, kind)

    rendered = str(error)
    assert "household_person" in rendered
    assert "[SQL:" in rendered, "the statement itself is still shown"

    line = _log_line(error)
    assert "IntegrityError" in line
    assert _VIOLATIONS[kind] in line, f"a {kind} violation no longer says what failed"


async def test_the_row_postgres_echoes_back_is_what_the_transport_has_to_stop(
    initialised_database: str,
) -> None:
    """Names the residual risk instead of leaving it to be rediscovered.

    For a CHECK violation the personal data *is* in ``str(exc)`` -- PostgreSQL put
    it there and no engine flag removes it. So the guarantee for this class is not
    "the exception is clean", it is "nothing that renders it writes it down", and
    that holds because ``infra/logging.py`` runs every line through ``redact``. Any
    future code that renders such an exception outside the logger reopens this.
    """
    async with _database(initialised_database) as database:
        error = await _provoke(database, "check")

    assert _PERSON_NAME in str(error), "if this fails the mechanism changed, not the risk"
    assert _PERSON_NAME not in _log_line(error)


async def test_neither_the_message_nor_the_repr_of_the_error_carries_the_values(
    initialised_database: str,
) -> None:
    """``str`` is what a log line renders; ``repr`` is what a debugger and a
    traceback frame render. Both, and the whole chain behind them.

    Scoped to the ``unique`` class, which is where ``hide_parameters=True`` is the
    control being asserted. The class above has a different control, asserted
    above.
    """
    async with _database(initialised_database) as database:
        error = await _provoke(database, "unique")

    chain: list[BaseException] = []
    current: BaseException | None = error
    while current is not None and current not in chain:
        chain.append(current)
        current = current.__cause__ or current.__context__
    assert len(chain) > 1, "the asyncpg error is chained behind the SQLAlchemy one"

    for link in chain:
        for rendered in (str(link), repr(link)):
            assert _PERSON_NAME not in rendered
            assert _PERSON_BAND not in rendered


# --------------------------------------------------------------------------- #
# The connection string
# --------------------------------------------------------------------------- #

_DSN_PASSWORD = "a-connection-string-password"


def _settings_with_password(database_url: str) -> str:
    """The configured DSN, with a recognisable password substituted into it."""
    url = sa.make_url(database_url).set(password=_DSN_PASSWORD)
    return url.render_as_string(hide_password=False)


def test_the_settings_object_does_not_print_the_connection_string() -> None:
    """A settings dump reaches a log line at startup and a traceback frame at any time."""
    settings = build_test_settings(_settings_with_password("postgresql+asyncpg://u:p@h/db"))

    assert _DSN_PASSWORD not in repr(settings)
    assert _DSN_PASSWORD not in str(settings)


def test_the_engine_does_not_print_the_connection_string(initialised_database: str) -> None:
    """``repr(engine)`` is what an unhandled error in a pool frame renders."""
    # Never connected, so never disposed: building the engine is all this asserts.
    database = Database(build_test_settings(_settings_with_password(initialised_database)))

    assert _DSN_PASSWORD not in repr(database.engine)
    assert _DSN_PASSWORD not in str(database.engine.url)


def test_statement_echo_is_off_and_parameters_are_hidden(initialised_database: str) -> None:
    """The two engine flags every assertion above depends on, pinned.

    ``echo=True`` would put every statement *and its parameters* on stdout,
    unformatted and unredacted -- below the formatter, so nothing would scrub it.
    """
    database = Database(build_test_settings(initialised_database))

    assert database.engine.sync_engine.echo is False
    assert database.engine.sync_engine.hide_parameters is True


async def test_a_failed_readiness_probe_logs_a_type_and_not_a_connection_string() -> None:
    """The one place a connection failure is deliberately logged."""
    dsn = f"postgresql+asyncpg://chaudron:{_DSN_PASSWORD}@127.0.0.1:1/none"
    database = Database(build_test_settings(dsn))
    try:
        with pytest.raises((SQLAlchemyError, OSError)) as raised:
            await database.check_row_level_security()
    finally:
        await database.dispose()

    line = _log_line(raised.value)
    assert _DSN_PASSWORD not in line
