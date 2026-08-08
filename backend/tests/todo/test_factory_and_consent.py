"""Who is allowed to export where, and on what agreement.

Two rules are enforced by the factory and are worth their own suite, because both
fail silently if they are wrong:

* the instance's own token is usable by exactly one household (ADR-0007's locked
  door, transposed) -- get this wrong on a shared instance and every household's
  groceries land in the operator's personal Todoist;
* a registered destination without a live consent is refused (ADR-0010, GDPR) --
  get this wrong and personal data leaves the house on nobody's authority.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from chaudron.domain.shopping_export import (
    ExportTargetNotConfigured,
    ShoppingExportConsentMissing,
)
from chaudron.infra.crypto import CredentialCipher
from chaudron.infra.todo.credentials import seal_export_token, sealed_export_token
from chaudron.infra.todo.factory import SUPPORTED_TARGETS, ShoppingExportFactory
from chaudron.infra.todo.http import BoundedTodoClient
from chaudron.infra.todo.settings import (
    TODOIST_HOUSEHOLD_ENV_VAR,
    TodoExportSettings,
    load_settings,
)
from chaudron.infra.todo.store import ShoppingExportTargetStore, StoredExportTarget
from chaudron.infra.todo.todoist import TodoistExporter
from tests.todo.conftest import FAKE_TOKEN

_OWNER = uuid.UUID(int=1)
_STRANGER = uuid.UUID(int=2)
_TARGET_ROW = uuid.UUID(int=3)


class _Store:
    """A store holding one row, standing in for the table that does not exist yet."""

    def __init__(self, target: StoredExportTarget | None) -> None:
        self._target = target

    async def target_for(
        self, household_id: uuid.UUID, target_code: str
    ) -> StoredExportTarget | None:
        if self._target is None:
            return None
        if self._target.household_id != household_id or self._target.target_code != target_code:
            return None
        return self._target


def _stored(
    cipher: CredentialCipher,
    *,
    household_id: uuid.UUID = _STRANGER,
    consented_at: dt.datetime | None = dt.datetime(2026, 8, 1, tzinfo=dt.UTC),
    revoked_at: dt.datetime | None = None,
) -> StoredExportTarget:
    encrypted = seal_export_token(
        cipher, FAKE_TOKEN, household_id=household_id, target_id=_TARGET_ROW
    )
    return StoredExportTarget(
        target_id=_TARGET_ROW,
        household_id=household_id,
        target_code="todoist",
        sealed_token=sealed_export_token(
            household_id=household_id,
            target_id=_TARGET_ROW,
            ciphertext=encrypted.ciphertext,
            key_id=encrypted.key_id,
        ),
        external_list_id="6HWcc9PJCvPjCxC9",
        consented_at=consented_at,
        consent_revoked_at=revoked_at,
    )


def _instance_settings() -> TodoExportSettings:
    return TodoExportSettings(todoist_household_id=_OWNER, todoist_token=FAKE_TOKEN)


# --------------------------------------------------------------------------- #
# The instance's own destination
# --------------------------------------------------------------------------- #


async def test_the_instance_token_serves_the_household_it_names() -> None:
    factory = ShoppingExportFactory(_instance_settings())
    exporter = await factory.exporter_for(_OWNER, "todoist")
    assert isinstance(exporter, TodoistExporter)


async def test_the_instance_token_refuses_every_other_household() -> None:
    """The failure this prevents is a stranger's groceries in the operator's list."""
    factory = ShoppingExportFactory(_instance_settings())

    with pytest.raises(ExportTargetNotConfigured, match="another household"):
        await factory.exporter_for(_STRANGER, "todoist")


async def test_an_instance_with_no_token_refuses_and_names_the_variable() -> None:
    """Unset means nobody, which is the closed default the failure mode demands."""
    factory = ShoppingExportFactory(TodoExportSettings())

    with pytest.raises(ExportTargetNotConfigured) as raised:
        await factory.exporter_for(_OWNER, "todoist")

    assert "CHAUDRON_TODOIST_TOKEN" in str(raised.value)


async def test_an_unknown_destination_is_refused_with_the_list_of_known_ones() -> None:
    factory = ShoppingExportFactory(_instance_settings())

    with pytest.raises(ExportTargetNotConfigured) as raised:
        await factory.exporter_for(_OWNER, "bring")

    assert "todoist" in str(raised.value)
    assert set(SUPPORTED_TARGETS) == {"todoist"}


# --------------------------------------------------------------------------- #
# A household's registered destination
# --------------------------------------------------------------------------- #


async def test_a_registered_destination_wins_over_the_instance_one() -> None:
    cipher = CredentialCipher(b"A" * 32)
    factory = ShoppingExportFactory(
        _instance_settings(), cipher=cipher, store=_Store(_stored(cipher))
    )

    exporter = await factory.exporter_for(_STRANGER, "todoist")

    assert isinstance(exporter, TodoistExporter)
    assert "6HWcc9PJCvPjCxC9" in repr(exporter), "the household's own project, not the operator's"


async def test_a_destination_without_consent_is_refused() -> None:
    """A row can exist before an agreement does; sending is what the agreement gates."""
    cipher = CredentialCipher(b"A" * 32)
    factory = ShoppingExportFactory(
        _instance_settings(), cipher=cipher, store=_Store(_stored(cipher, consented_at=None))
    )

    with pytest.raises(ShoppingExportConsentMissing):
        await factory.exporter_for(_STRANGER, "todoist")


async def test_withdrawn_consent_stops_the_export_without_deleting_the_row() -> None:
    """Withdrawal must take effect on the next call, not on the next cleanup job."""
    cipher = CredentialCipher(b"A" * 32)
    target = _stored(cipher, revoked_at=dt.datetime(2026, 8, 3, tzinfo=dt.UTC))
    assert not target.is_consented
    factory = ShoppingExportFactory(_instance_settings(), cipher=cipher, store=_Store(target))

    with pytest.raises(ShoppingExportConsentMissing):
        await factory.exporter_for(_STRANGER, "todoist")


async def test_a_stored_destination_on_an_instance_without_a_cipher_is_refused() -> None:
    """An instance that cannot decrypt says so; it does not fall through to the
    operator's own account with somebody else's list."""
    cipher = CredentialCipher(b"A" * 32)
    factory = ShoppingExportFactory(
        _instance_settings(), cipher=None, store=_Store(_stored(cipher))
    )

    with pytest.raises(ExportTargetNotConfigured, match="cannot decrypt"):
        await factory.exporter_for(_STRANGER, "todoist")


async def test_an_empty_store_falls_through_to_the_instance_destination() -> None:
    factory = ShoppingExportFactory(_instance_settings(), store=_Store(None))
    assert isinstance(await factory.exporter_for(_OWNER, "todoist"), TodoistExporter)


def test_the_store_protocol_is_satisfied_by_the_double() -> None:
    """A runtime check, so the port and its only implementation cannot drift apart."""
    assert isinstance(_Store(None), ShoppingExportTargetStore)


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #


def test_a_malformed_owner_household_fails_at_load_rather_than_at_export() -> None:
    """A typo here points the operator's list at nobody -- or, if the comparison were
    ever loosened, at somebody else."""
    with pytest.raises(ValueError, match=TODOIST_HOUSEHOLD_ENV_VAR):
        load_settings({TODOIST_HOUSEHOLD_ENV_VAR: "not-a-uuid"})


def test_an_empty_environment_disables_the_instance_destination() -> None:
    settings = load_settings({})
    assert not settings.has_instance_todoist


def test_the_outbound_bounds_are_configurable_and_validated() -> None:
    """Both bounds are read from the environment, and neither accepts nonsense."""
    settings = load_settings(
        {
            "CHAUDRON_TODO_EXPORT_TIMEOUT_SECONDS": "3.5",
            "CHAUDRON_TODO_EXPORT_MAX_RESPONSE_BYTES": "2048",
        }
    )
    assert (settings.timeout_seconds, settings.max_response_bytes) == (3.5, 2048)

    for variable, value in [
        ("CHAUDRON_TODO_EXPORT_TIMEOUT_SECONDS", "soon"),
        ("CHAUDRON_TODO_EXPORT_TIMEOUT_SECONDS", "0"),
        ("CHAUDRON_TODO_EXPORT_MAX_RESPONSE_BYTES", "lots"),
        ("CHAUDRON_TODO_EXPORT_MAX_RESPONSE_BYTES", "-1"),
    ]:
        with pytest.raises(ValueError, match=variable):
            load_settings({variable: value})


def test_a_destination_could_not_be_declared_over_cleartext() -> None:
    """A guard on this package's own constants: a bearer token over http is a
    credential given away, so the client refuses to be built at all."""
    with pytest.raises(ValueError, match="must be https"):
        BoundedTodoClient("http://api.todoist.com", target="todoist", settings=TodoExportSettings())
