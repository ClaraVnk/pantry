"""``scripts/seed.py`` must not be one exported DSN away from a live database.

The script writes a sign-in account whose password is a constant in the source of
a public repository. Whether that is harmless or a published administrator
credential depends entirely on which database it is pointed at -- and pointing it
somewhere is one ``export CHAUDRON_DATABASE_URL`` away, on a terminal where the
previous command was probably a production ``psql``.

So the refusal is asserted here rather than trusted to the ``if`` that implements
it. A guard nobody tests is a guard that survives exactly until someone widens
the condition to unblock themselves.

No database is touched: the point of every test below is that the engine is never
built at all, which is also what makes them run without PostgreSQL.
"""

from __future__ import annotations

import base64
from typing import Any

import pytest
from pydantic import SecretStr
from scripts import seed

from chaudron.config import Environment, Settings


class _EngineWasBuiltError(AssertionError):
    """Raised in place of connecting, so "got past the guard" is observable."""


def _settings(env: Environment) -> Settings:
    return Settings(
        env=env,
        log_level="WARNING",
        # A DSN that would be catastrophic to seed, and that the guard must refuse
        # to look at rather than to parse.
        database_url=SecretStr("postgresql+asyncpg://chaudron:secret@db.example/chaudron"),
        secret_key=SecretStr("k" * 48),
        credential_encryption_key=SecretStr(base64.b64encode(b"0" * 32).decode()),
        # `https` because production demands it; harmless in every other
        # environment, and not what this file is about.
        base_url="https://chaudron.example",
    )


@pytest.fixture
def refuse_to_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    def _explode(*_args: Any, **_kwargs: Any) -> Any:
        raise _EngineWasBuiltError

    monkeypatch.setattr(seed, "create_async_engine", _explode)


@pytest.mark.parametrize("env", ["production", "staging", "ci"])
async def test_seeding_is_refused_outside_local(
    env: Environment, monkeypatch: pytest.MonkeyPatch, refuse_to_connect: None
) -> None:
    """``ci`` is in this list on purpose.

    It used to be permitted, and no pipeline ever used it -- ``.github/workflows``
    runs migrations and pytest and never this script. An environment that is
    allowed but unused is a door held open for nobody.
    """
    monkeypatch.setattr(seed, "get_settings", lambda: _settings(env))

    assert await seed.main() == 2, "a non-local environment must be refused"


@pytest.mark.parametrize("env", ["production", "staging", "ci"])
async def test_the_refusal_says_what_to_do_about_it(
    env: Environment,
    monkeypatch: pytest.MonkeyPatch,
    refuse_to_connect: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A refusal without a remedy is a refusal somebody works around blindly."""
    monkeypatch.setattr(seed, "get_settings", lambda: _settings(env))

    with caplog.at_level("ERROR", logger="chaudron.seed"):
        await seed.main()

    message = caplog.text
    assert env in message, "the refusal names the environment it saw"
    assert "CHAUDRON_ENV=local" in message, "and how to proceed if this really is a sandbox"


async def test_the_refusal_never_prints_the_credentials_it_would_have_written(
    monkeypatch: pytest.MonkeyPatch, refuse_to_connect: None, caplog: pytest.LogCaptureFixture
) -> None:
    """The demonstration password is the thing worth not broadcasting.

    A refusal that quoted "the account it would have created" would put that
    password on the terminal, and into the shell log, of the very operator who
    just aimed this script at something they should not have.
    """
    monkeypatch.setattr(seed, "get_settings", lambda: _settings("production"))

    with caplog.at_level("ERROR", logger="chaudron.seed"):
        await seed.main()

    assert seed.DEMO_PASSWORD not in caplog.text
    assert "secret@db.example" not in caplog.text, "nor the DSN it was pointed at"


async def test_a_local_environment_is_allowed_through(
    monkeypatch: pytest.MonkeyPatch, refuse_to_connect: None
) -> None:
    """The other half: a guard that refused everything would pass every test above."""
    monkeypatch.setattr(seed, "get_settings", lambda: _settings("local"))

    with pytest.raises(_EngineWasBuiltError):
        await seed.main()
