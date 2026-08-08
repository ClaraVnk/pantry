"""An unreadable export credential is an answer, not a 500 (audit O-06).

The state is ordinary rather than exotic. ``CHAUDRON_CREDENTIAL_ENCRYPTION_KEY``
is what seals every stored export token, and an instance restored from a backup
without it -- or rotated onto a new one -- stops opening all of them at once.
``infra/crypto.py`` refuses on the key-id mismatch by design, with a sentence
written for the operator.

What that sentence had no route to was the household. The cipher raises
:class:`~chaudron.domain.llm_ports.CredentialDecryptionError`, a
``ProviderNotConfigured`` -- the *model provider* hierarchy, which shares no base
class with ``ShoppingExportError`` -- so ``routers/shopping_export.py`` caught
nothing, ``api/errors.py`` caught it as an unhandled exception, and the household
got an opaque 500 for a problem with a remedy.

So this file asserts the whole path at once, against a real PostgreSQL: a
registered destination, its stored key id moved out from under it, and one POST.
The status, the slug and the sentence are checked, and so is the thing the
sentence must not contain -- the name of the instance's master key is an
operator's business and the household has no shell to act on it.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from chaudron.domain.models import Household, ShoppingExportTarget
from tests.conftest import MakeHousehold, household_headers
from tests.todo.conftest import FAKE_TOKEN

pytestmark = pytest.mark.integration

_TODOIST_TARGET_PATH = "/v1/shopping-lists/export/targets/todoist"


async def _register(client: httpx.AsyncClient, household: Household) -> None:
    response = await client.put(
        _TODOIST_TARGET_PATH,
        json={"token": FAKE_TOKEN, "consent_granted": True},
        headers=household_headers(household),
    )
    assert response.status_code == 200, response.text


async def _rotate_the_master_key_out_from_under_the_row(
    session: AsyncSession, household: Household
) -> None:
    """Simulate the rotation, from the row's side rather than the process's.

    Moving the *stored* key id is equivalent to changing the environment variable
    and cheaper: the cipher compares the two and refuses on the mismatch, which
    is the branch under test. Restarting the application under a second key would
    exercise the same line through a great deal more machinery.
    """
    await session.execute(
        sa_update(ShoppingExportTarget)
        .where(ShoppingExportTarget.household_id == household.id)
        .values(token_encryption_key_id="rotatedkeyid0000")
    )
    await session.flush()


async def test_an_undecryptable_export_credential_answers_409_and_says_what_to_do(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
) -> None:
    household = await make_household()
    await _register(api_client, household)
    await _rotate_the_master_key_out_from_under_the_row(db_session, household)

    # The shopping list need not exist: the credential is opened while the
    # exporter is being built, before anything reads the list -- which is also
    # what keeps a household's groceries out of a request nobody can authorise.
    response = await api_client.post(
        f"/v1/shopping-lists/{uuid.uuid4()}/export/todoist",
        headers=household_headers(household),
    )

    assert response.status_code == 409, response.text
    body = response.json()
    assert body["type"].endswith("/export-credential-unreadable")
    assert body["target"] == "todoist"
    assert "register the destination again" in body["detail"]


async def test_the_answer_names_neither_the_instance_key_nor_the_token(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
) -> None:
    """Two different leaks, and the fix for the first must not open the second.

    The remedy an operator needs -- restore or re-enter
    ``CHAUDRON_CREDENTIAL_ENCRYPTION_KEY`` -- is not actionable by a household
    member and names an instance secret, so it belongs in the log
    (``infra/todo/credentials.py``) and nowhere near a response body. The token
    is not in the response for the older reason: nothing decrypted it.
    """
    household = await make_household()
    await _register(api_client, household)
    await _rotate_the_master_key_out_from_under_the_row(db_session, household)

    response = await api_client.post(
        f"/v1/shopping-lists/{uuid.uuid4()}/export/todoist",
        headers=household_headers(household),
    )

    assert "CHAUDRON_CREDENTIAL_ENCRYPTION_KEY" not in response.text
    assert "ENCRYPTION_KEY" not in response.text
    assert FAKE_TOKEN not in response.text


async def test_the_supported_list_is_absent_because_there_is_nothing_to_choose(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
) -> None:
    """The arm sits before ``ExportTargetNotConfigured``, which it inherits from.

    Ordering decides the answer here: the general arm attaches ``supported`` and
    says "no such export destination", which would send a household to register a
    destination they have already registered. Asserted rather than trusted,
    because a ``match`` arm moved during a later edit fails nothing else.
    """
    household = await make_household()
    await _register(api_client, household)
    await _rotate_the_master_key_out_from_under_the_row(db_session, household)

    response = await api_client.post(
        f"/v1/shopping-lists/{uuid.uuid4()}/export/todoist",
        headers=household_headers(household),
    )

    assert "supported" not in response.json()
