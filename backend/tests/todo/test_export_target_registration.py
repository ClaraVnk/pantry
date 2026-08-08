"""Registering a destination per household, against a real PostgreSQL.

The table is the point of this suite, so nothing here is a double. What is being
asserted is what the schema, the policies and the cipher do together:

* a household's destination is *its own* -- two households register under the same
  code and neither can see or overwrite the other's row;
* an agreement is dated on the way in and cannot be omitted, because migration
  ``0008`` makes ``consented_at`` ``NOT NULL`` and the service refuses before the
  database has to;
* a withdrawal keeps the row and stops the sending, which is two separate claims
  and therefore two separate assertions;
* the export path actually reads the row -- a registered token that the factory
  ignored in favour of the instance destination would pass every test above and
  send a household's groceries to the operator.

``tests/todo/test_no_token_leaks.py`` owns the "and none of this returns the
token" half; this file is about what the feature does.
"""

from __future__ import annotations

import datetime as dt
import uuid

import httpx
import pytest
from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from chaudron.domain.models import (
    Household,
    HouseholdMember,
    ShoppingExportTarget,
    UserAccount,
)
from chaudron.domain.shopping_export import ShoppingExportConsentMissing
from chaudron.infra.repositories import SqlShoppingExportTargetStore
from chaudron.infra.todo.credentials import open_export_token
from chaudron.infra.todo.factory import ShoppingExportFactory
from chaudron.infra.todo.settings import TodoExportSettings
from chaudron.infra.todo.todoist import TodoistExporter
from tests.conftest import (
    MakeHousehold,
    MakeMember,
    MakeUser,
    SignedIn,
    build_test_cipher,
    household_headers,
)
from tests.todo.conftest import FAKE_TOKEN

pytestmark = pytest.mark.integration

_TARGETS_PATH = "/v1/shopping-lists/export/targets"
_TODOIST_PATH = f"{_TARGETS_PATH}/todoist"

_OTHER_TOKEN = "fedcba9876543210fedcba9876543210fedcba98"
_PROJECT_ID = "6HWcc9PJCvPjCxC9"


def _registration(
    *, token: str = FAKE_TOKEN, consent: bool = True, project: str | None = None
) -> dict[str, object]:
    body: dict[str, object] = {"token": token, "consent_granted": consent}
    if project is not None:
        body["external_list_id"] = project
    return body


async def _register(
    client: httpx.AsyncClient, household: Household, **kwargs: object
) -> httpx.Response:
    return await client.put(
        _TODOIST_PATH,
        json=_registration(**kwargs),  # type: ignore[arg-type]
        headers=household_headers(household),
    )


# --------------------------------------------------------------------------- #
# Registering
# --------------------------------------------------------------------------- #


async def test_a_registered_destination_is_readable_and_consented(
    api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    household = await make_household()
    created = await _register(api_client, household, project=_PROJECT_ID)

    assert created.status_code == 200, created.text
    body = created.json()
    assert body["target"] == "todoist"
    assert body["external_list_id"] == _PROJECT_ID
    assert body["is_consented"] is True
    assert body["consent_revoked_at"] is None
    assert dt.datetime.fromisoformat(body["consented_at"]).tzinfo is not None


async def test_the_availability_list_is_an_empty_array_before_anything_is_registered(
    api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    """Empty is the normal state, and it must not be spelled ``404``.

    A client that had to read a status code as data would be one refactor away
    from treating a genuine outage as "no destination configured".
    """
    household = await make_household()
    response = await api_client.get(_TARGETS_PATH, headers=household_headers(household))

    assert response.status_code == 200
    assert response.json() == []


async def test_the_settings_view_is_404_until_something_is_registered(
    api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    household = await make_household()
    response = await api_client.get(_TODOIST_PATH, headers=household_headers(household))

    assert response.status_code == 404
    assert response.json()["type"].endswith("export-target-not-registered")


async def test_registering_again_replaces_the_token_rather_than_adding_a_row(
    api_client: httpx.AsyncClient, make_household: MakeHousehold, db_session: AsyncSession
) -> None:
    """Storing a token over an existing one *is* the rotation procedure (ADR-0007)."""
    household = await make_household()
    await _register(api_client, household)
    second = await _register(api_client, household, token=_OTHER_TOKEN, project=_PROJECT_ID)

    assert second.status_code == 200, second.text
    assert second.json()["token_last4"] == _OTHER_TOKEN[-4:]

    rows = (
        await db_session.scalars(
            select(ShoppingExportTarget).where(ShoppingExportTarget.household_id == household.id)
        )
    ).all()
    assert len(rows) == 1


async def test_a_registration_without_an_agreement_is_refused_and_writes_nothing(
    api_client: httpx.AsyncClient, make_household: MakeHousehold, db_session: AsyncSession
) -> None:
    """The ``NOT NULL`` of migration ``0008``, enforced one layer earlier as a sentence.

    Nothing is written, which is the part worth asserting: a row created and left
    without an agreement -- to be filled in "later" -- is exactly what the column
    exists to make impossible.
    """
    household = await make_household()
    refused = await _register(api_client, household, consent=False)

    assert refused.status_code == 422
    assert refused.json()["type"].endswith("export-consent-required")

    rows = (
        await db_session.scalars(
            select(ShoppingExportTarget).where(ShoppingExportTarget.household_id == household.id)
        )
    ).all()
    assert rows == []


async def test_a_destination_this_build_cannot_reach_is_refused_by_name(
    api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    """The companion of ``test_bring_is_refused_by_name``: no row for an absent adapter.

    ADR-0010 refuses Bring! on contractual grounds. A registration endpoint that
    accepted the code anyway would store a token for a destination nothing can
    send to, and the refusal would move to the first export -- long after the
    household believed it had set the feature up.
    """
    household = await make_household()
    response = await api_client.put(
        f"{_TARGETS_PATH}/bring",
        json=_registration(),
        headers=household_headers(household),
    )

    assert response.status_code == 409
    body = response.json()
    assert body["type"].endswith("export-target-not-configured")
    assert body["supported"] == ["todoist"]


# --------------------------------------------------------------------------- #
# Withdrawing
# --------------------------------------------------------------------------- #


async def test_withdrawing_keeps_the_row_and_removes_it_from_the_availability_list(
    api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    """Two claims, because ADR-0010 makes both and they pull in opposite directions.

    The row survives so the household can see what it once authorised; the
    destination disappears from what an interface reads to decide whether to
    offer a button. A build that satisfied only the first would put the button
    back the moment consent was withdrawn.
    """
    household = await make_household()
    headers = household_headers(household)
    await _register(api_client, household)

    withdrawn = await api_client.delete(_TODOIST_PATH, headers=headers)
    assert withdrawn.status_code == 200, withdrawn.text
    assert withdrawn.json()["is_consented"] is False
    assert withdrawn.json()["consent_revoked_at"] is not None

    assert (await api_client.get(_TARGETS_PATH, headers=headers)).json() == []

    kept = await api_client.get(_TODOIST_PATH, headers=headers)
    assert kept.status_code == 200
    assert kept.json()["consent_revoked_at"] == withdrawn.json()["consent_revoked_at"]


async def test_withdrawing_twice_keeps_the_first_date(
    api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    """Idempotent, and specifically *not* by moving the date forward.

    Overwriting it would quietly rewrite when the household stopped consenting,
    which is the one fact the surviving row exists to hold.
    """
    household = await make_household()
    headers = household_headers(household)
    await _register(api_client, household)

    first = await api_client.delete(_TODOIST_PATH, headers=headers)
    second = await api_client.delete(_TODOIST_PATH, headers=headers)

    assert second.status_code == 200
    assert second.json()["consent_revoked_at"] == first.json()["consent_revoked_at"]


async def test_withdrawing_something_never_registered_is_a_404(
    api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    household = await make_household()
    response = await api_client.delete(_TODOIST_PATH, headers=household_headers(household))
    assert response.status_code == 404


async def test_registering_again_after_a_withdrawal_restores_the_agreement(
    api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    """Re-consenting means restating the token, which is the design and not a gap.

    There is no operation that flips the date back on a row nobody looked at: the
    household re-registers, which puts the sentence about what leaves the
    instance back in front of them.
    """
    household = await make_household()
    headers = household_headers(household)
    await _register(api_client, household)
    await api_client.delete(_TODOIST_PATH, headers=headers)

    again = await _register(api_client, household, token=_OTHER_TOKEN)
    assert again.status_code == 200
    assert again.json()["is_consented"] is True
    assert again.json()["consent_revoked_at"] is None
    assert len((await api_client.get(_TARGETS_PATH, headers=headers)).json()) == 1


# --------------------------------------------------------------------------- #
# Tenancy
# --------------------------------------------------------------------------- #


async def test_one_household_cannot_see_or_replace_another_destination(
    api_client: httpx.AsyncClient, make_household: MakeHousehold, db_session: AsyncSession
) -> None:
    """The unique constraint is ``(household_id, target_code)``, not ``target_code``.

    Both households register ``todoist``; if the constraint had forgotten the
    tenant, the second would fail with an error confirming the first exists.
    """
    first = await make_household()
    second = await make_household()

    assert (await _register(api_client, first)).status_code == 200
    assert (await _register(api_client, second, token=_OTHER_TOKEN)).status_code == 200

    seen_by_second = await api_client.get(_TODOIST_PATH, headers=household_headers(second))
    assert seen_by_second.json()["token_last4"] == _OTHER_TOKEN[-4:]

    rows = (
        await db_session.scalars(
            select(ShoppingExportTarget).where(ShoppingExportTarget.household_id == first.id)
        )
    ).all()
    assert len(rows) == 1
    assert rows[0].token_last4 == FAKE_TOKEN[-4:]


# --------------------------------------------------------------------------- #
# What the export path actually reads
# --------------------------------------------------------------------------- #


async def test_the_stored_token_round_trips_through_the_read_store(
    api_client: httpx.AsyncClient, make_household: MakeHousehold, db_session: AsyncSession
) -> None:
    """End to end through the column: registered over HTTP, opened by the store.

    This is the assertion that would fail if the AAD domain, the row identifier
    or the key id drifted between the write and the read -- each of which is a
    silent corruption until the first export.
    """
    household = await make_household()
    assert (await _register(api_client, household, project=_PROJECT_ID)).status_code == 200

    stored = await SqlShoppingExportTargetStore(db_session).target_for(household.id, "todoist")

    assert stored is not None
    assert stored.is_consented
    assert stored.external_list_id == _PROJECT_ID
    assert open_export_token(build_test_cipher(), stored.sealed_token) == FAKE_TOKEN


async def test_the_ciphertext_is_not_loaded_by_an_ordinary_select(
    api_client: httpx.AsyncClient, make_household: MakeHousehold, db_session: AsyncSession
) -> None:
    """``deferred=True`` is the enforcement, not the documentation.

    A plain ``select`` must not put a secret in memory; reading it has to be an
    explicit, greppable ``undefer``. Asserted through the SQL rather than through
    the mapper, because the property that matters is that the bytes never left
    the database.
    """
    household = await make_household()
    assert (await _register(api_client, household)).status_code == 200
    db_session.expunge_all()

    row = await db_session.scalar(
        select(ShoppingExportTarget).where(ShoppingExportTarget.household_id == household.id)
    )
    assert row is not None
    assert "token_ciphertext" not in row.__dict__, (
        "a plain select loaded the secret column; the deferral is gone"
    )

    undeferred = await db_session.scalar(
        select(ShoppingExportTarget)
        .where(ShoppingExportTarget.household_id == household.id)
        .options(undefer(ShoppingExportTarget.token_ciphertext))
    )
    assert undeferred is not None
    assert undeferred.token_ciphertext


async def test_the_factory_builds_an_exporter_from_the_registered_row(
    api_client: httpx.AsyncClient, make_household: MakeHousehold, db_session: AsyncSession
) -> None:
    """The household's own destination wins, and it does not need an instance token.

    ``TodoExportSettings()`` here holds no instance destination at all, so an
    exporter coming back proves the stored row is what answered.
    """
    household = await make_household()
    assert (await _register(api_client, household, project=_PROJECT_ID)).status_code == 200

    factory = ShoppingExportFactory(
        TodoExportSettings(),
        cipher=build_test_cipher(),
        store=SqlShoppingExportTargetStore(db_session),
    )
    exporter = await factory.exporter_for(household.id, "todoist")

    assert isinstance(exporter, TodoistExporter)
    assert _PROJECT_ID in repr(exporter)


async def test_a_withdrawn_agreement_stops_the_export_at_the_next_send(
    api_client: httpx.AsyncClient, make_household: MakeHousehold, db_session: AsyncSession
) -> None:
    """Withdrawal takes effect on the next call, not at some later cleanup.

    Refused with ``ShoppingExportConsentMissing`` rather than by falling through
    to the instance destination, which would send this household's list to the
    operator's account on an agreement that had just been withdrawn.
    """
    household = await make_household()
    headers = household_headers(household)
    assert (await _register(api_client, household)).status_code == 200
    assert (await api_client.delete(_TODOIST_PATH, headers=headers)).status_code == 200

    factory = ShoppingExportFactory(
        # An instance destination that would otherwise answer, pointed at this
        # very household: the refusal has to come from the consent, not from the
        # absence of an alternative.
        TodoExportSettings(todoist_household_id=household.id, todoist_token=_OTHER_TOKEN),
        cipher=build_test_cipher(),
        store=SqlShoppingExportTargetStore(db_session),
    )
    with pytest.raises(ShoppingExportConsentMissing):
        await factory.exporter_for(household.id, "todoist")


async def test_an_unregistered_household_is_not_served_by_another_households_row(
    api_client: httpx.AsyncClient, make_household: MakeHousehold, db_session: AsyncSession
) -> None:
    """The store is keyed by household, and the failure mode is somebody else's Todoist."""
    registered = await make_household()
    stranger = await make_household()
    assert (await _register(api_client, registered)).status_code == 200

    store = SqlShoppingExportTargetStore(db_session)
    assert await store.target_for(stranger.id, "todoist") is None
    assert await store.target_for(registered.id, "bring") is None
    assert await store.target_for(uuid.uuid7(), "todoist") is None


# --------------------------------------------------------------------------- #
# Who registered it, and what happens when they leave
# --------------------------------------------------------------------------- #
#
# Nothing cascades from `household_member` to `shopping_export_target`, and
# nothing should: deleting the row on exclusion would delete the household's
# record of what it once authorised. What was missing was the other half -- the
# row said a household had agreed and never said *who agreed on its behalf*, so an
# excluded member's token kept filing the household's groceries into their
# personal account, indefinitely, while the settings screen showed four characters
# of a credential and no name (audit AUD-027).


async def test_the_registration_records_who_made_it(
    api_client: httpx.AsyncClient, make_household: MakeHousehold, signed_in: SignedIn
) -> None:
    household = await make_household()

    created = await _register(api_client, household)

    assert created.status_code == 200, created.text
    assert created.json()["registered_by"] == signed_in.user.display_name
    assert created.json()["registrant_is_member"] is True


async def _orphan(
    session: AsyncSession, household: Household, make_user: MakeUser, make_member: MakeMember
) -> UserAccount:
    """Move the registration onto somebody, then take their membership away.

    Two steps rather than one, because "registered by a stranger" and "registered
    by somebody who has since been excluded" are the same row and only the second
    is a real history. Doing it through the tables is the only route available:
    there is no membership-removal endpoint in this application, which is also why
    the check has to run on every send rather than at the moment of exclusion.
    """
    departed = await make_user(display_name="Ancien colocataire")
    await make_member(household, departed)
    await session.execute(
        sa_update(ShoppingExportTarget)
        .where(ShoppingExportTarget.household_id == household.id)
        .values(registered_by_user_id=departed.id)
    )
    await session.execute(
        sa_delete(HouseholdMember).where(
            HouseholdMember.household_id == household.id,
            HouseholdMember.user_id == departed.id,
        )
    )
    await session.flush()
    return departed


async def test_a_destination_whose_registrant_left_is_named_and_flagged(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
    make_user: MakeUser,
    make_member: MakeMember,
) -> None:
    """The settings view keeps showing it, with the name and the fact.

    Hiding it would be the worst of both: the household would keep exporting -- no,
    worse, would keep *not* exporting -- with nothing on screen to explain why.
    """
    household = await make_household()
    assert (await _register(api_client, household)).status_code == 200
    departed = await _orphan(db_session, household, make_user, make_member)

    read = await api_client.get(_TODOIST_PATH, headers=household_headers(household))

    assert read.status_code == 200, read.text
    assert read.json()["registered_by"] == departed.display_name
    assert read.json()["registrant_is_member"] is False
    # The agreement itself was never withdrawn, and the row still says so. The two
    # facts are separate because their remedies are.
    assert read.json()["is_consented"] is True


async def test_a_destination_whose_registrant_left_is_not_offered_as_usable(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
    make_user: MakeUser,
    make_member: MakeMember,
) -> None:
    """The listing is what an interface reads to decide whether to show a button.

    A button that produces a 403 nobody can explain is worse than no button.
    """
    household = await make_household()
    assert (await _register(api_client, household)).status_code == 200

    offered = await api_client.get(_TARGETS_PATH, headers=household_headers(household))
    assert [entry["target"] for entry in offered.json()] == ["todoist"]

    await _orphan(db_session, household, make_user, make_member)

    offered = await api_client.get(_TARGETS_PATH, headers=household_headers(household))
    assert offered.json() == []


async def test_the_export_refuses_a_destination_whose_registrant_left(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
    make_user: MakeUser,
    make_member: MakeMember,
) -> None:
    """The claim that matters: the token stops being used, on the next send.

    Refused before any socket is opened -- the factory reads the membership at the
    same moment it reads the consent -- so an excluded person's Todoist account
    never sees the request at all.
    """
    household = await make_household()
    assert (await _register(api_client, household)).status_code == 200
    await _orphan(db_session, household, make_user, make_member)

    response = await api_client.post(
        f"/v1/shopping-lists/{uuid.uuid4()}/export/todoist",
        headers=household_headers(household),
    )

    assert response.status_code == 403, response.text
    assert response.json()["type"].endswith("/export-registrant-has-left")


async def test_a_row_with_no_recorded_registrant_still_exports(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
) -> None:
    """``NULL`` is "registered before the column existed", not "registered by nobody".

    Migration ``0014`` deliberately does not backfill: inventing a plausible owner
    would make an unaudited consent look audited. Reading the absence as a
    departure would instead break every existing installation on upgrade, which is
    the failure this asserts against.
    """
    household = await make_household()
    assert (await _register(api_client, household)).status_code == 200
    await db_session.execute(
        sa_update(ShoppingExportTarget)
        .where(ShoppingExportTarget.household_id == household.id)
        .values(registered_by_user_id=None)
    )
    await db_session.flush()

    read = await api_client.get(_TODOIST_PATH, headers=household_headers(household))
    assert read.json()["registered_by"] is None
    assert read.json()["registrant_is_member"] is True

    offered = await api_client.get(_TARGETS_PATH, headers=household_headers(household))
    assert [entry["target"] for entry in offered.json()] == ["todoist"]
