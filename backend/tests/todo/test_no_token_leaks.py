"""SEC-003, applied to export tokens: nothing key-shaped comes out of an adapter.

The finding behind ``tests/llm/test_no_key_leaks.py`` is not specific to model
providers. A task API rejecting a bearer token routinely quotes it in the very
error explaining the rejection, and one ``str(exc)`` in a log line, a traceback or
an HTTP response leaks a third party's credential -- which then outlives every
rotation policy, because a log file is forever.

So the destinations here are scripted to be maximally hostile in the one way that
matters: **every failure body contains the token that was sent**. The suite drives
the real adapter through every failure mode and asserts that neither the message,
nor its ``repr``, nor its ``__cause__``, nor the exception chain behind it carries
the token or anything shaped like one.

The same rule covers the *configuration* path, and the last section of this file
is where it is exercised: registering a destination is the one operation in the
application that accepts an export token, so it is the one that could hand it
back. Nothing it returns, and nothing it refuses with, may contain the value that
was submitted -- including on the paths where quoting the input would be the
natural thing to do (a token too short, a destination that does not exist, a
validation error pydantic would happily echo).
"""

from __future__ import annotations

import logging
import re
import uuid
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from chaudron.domain.llm_ports import CredentialDecryptionError
from chaudron.domain.shopping_export import ExportCredentialUnreadable, ShoppingExportError
from chaudron.infra.crypto import (
    EXPORT_TOKEN_DOMAIN,
    PROVIDER_KEY_DOMAIN,
    CredentialCipher,
    SealedCredential,
)
from chaudron.infra.redaction import redact
from chaudron.infra.todo.credentials import (
    open_export_token,
    seal_export_token,
    sealed_export_token,
)
from chaudron.infra.todo.settings import TodoExportSettings
from chaudron.infra.todo.todoist import TodoistExporter, build_todoist_client
from tests.conftest import MakeHousehold, household_headers
from tests.todo.conftest import FAKE_TOKEN, TodoistDouble, make_export

#: A bearer header still carrying something that is not the redaction marker.
_BEARER_WITH_VALUE = re.compile(r"Bearer\s+(?!\[redacted\])\S", re.IGNORECASE)

_HOUSEHOLD = uuid.UUID(int=7)
_TARGET = uuid.UUID(int=8)

#: Every way this adapter can fail against a live endpoint, each answering with a
#: body that embeds the credential -- which is what real APIs do.
_HOSTILE_ANSWERS: dict[str, int] = {
    "unauthorised": 401,
    "forbidden": 403,
    "not_found": 404,
    "rate_limited": 429,
    "server_error": 500,
    "teapot": 418,
}


def _leaky_body() -> str:
    return f"authentication failed for token {FAKE_TOKEN}; Bearer {FAKE_TOKEN} is not valid"


@pytest.mark.parametrize("scenario", sorted(_HOSTILE_ANSWERS))
async def test_no_http_failure_message_carries_the_token(
    todoist: TodoistDouble, export_settings: TodoExportSettings, scenario: str
) -> None:
    todoist.responder = lambda _uuids: httpx.Response(
        _HOSTILE_ANSWERS[scenario], text=_leaky_body()
    )
    exporter = TodoistExporter(
        build_todoist_client(FAKE_TOKEN, export_settings, transport=todoist.transport())
    )

    with pytest.raises(ShoppingExportError) as raised:
        await exporter.send(make_export("Lait"))

    _assert_clean(raised.value)


async def test_a_body_that_is_not_json_is_excerpted_with_the_token_removed(
    todoist: TodoistDouble, export_settings: TodoExportSettings
) -> None:
    """The one place quoting is genuinely useful -- and therefore the one to scrub.

    A 200 carrying HTML (a captive portal, a proxy error page) is worth an
    excerpt, because it is how an operator diagnoses a network they control. The
    excerpt goes through the same scrubber, with our own token passed as a
    literal to remove, so a token echoed by a proxy does not survive it.
    """
    todoist.responder = lambda _uuids: httpx.Response(
        200, text=f"<html>proxy denied Bearer {FAKE_TOKEN}</html>"
    )
    exporter = TodoistExporter(
        build_todoist_client(FAKE_TOKEN, export_settings, transport=todoist.transport())
    )

    with pytest.raises(ShoppingExportError) as raised:
        await exporter.send(make_export("Lait"))

    _assert_clean(raised.value)
    assert "proxy denied" in str(raised.value), "the useful part of the excerpt survives"


async def test_a_rejected_command_does_not_echo_what_the_recipient_wrote(
    todoist: TodoistDouble, export_settings: TodoExportSettings
) -> None:
    """Per-command error objects are counted and discarded, never quoted."""
    todoist.responder = lambda uuids: httpx.Response(
        200,
        json={
            "sync_status": {
                uuids[0]: {"error": f"bad token {FAKE_TOKEN}", "error_code": 401},
            },
            "temp_id_mapping": {},
        },
    )
    exporter = TodoistExporter(
        build_todoist_client(FAKE_TOKEN, export_settings, transport=todoist.transport())
    )

    with pytest.raises(ShoppingExportError) as raised:
        await exporter.send(make_export("Lait"))

    _assert_clean(raised.value)


def test_the_exporter_repr_does_not_print_its_credential(
    export_settings: TodoExportSettings,
) -> None:
    """A debugger session, a traceback frame and a settings dump all call ``repr``."""
    exporter = TodoistExporter(build_todoist_client(FAKE_TOKEN, export_settings))
    for rendered in (repr(exporter), str(exporter)):
        assert FAKE_TOKEN not in rendered


def test_the_settings_object_does_not_print_its_token() -> None:
    settings = TodoExportSettings(todoist_token=FAKE_TOKEN, todoist_household_id=_HOUSEHOLD)
    assert FAKE_TOKEN not in repr(settings)


# --------------------------------------------------------------------------- #
# The stored token, from sealing to every way opening it fails
# --------------------------------------------------------------------------- #


def _sealed(cipher: CredentialCipher, *, household_id: uuid.UUID = _HOUSEHOLD) -> SealedCredential:
    stored = seal_export_token(cipher, FAKE_TOKEN, household_id=household_id, target_id=_TARGET)
    return sealed_export_token(
        household_id=household_id,
        target_id=_TARGET,
        ciphertext=stored.ciphertext,
        key_id=stored.key_id,
    )


def test_a_sealed_token_round_trips() -> None:
    cipher = CredentialCipher(b"A" * 32)
    assert open_export_token(cipher, _sealed(cipher)) == FAKE_TOKEN


def test_a_sealed_token_carries_no_plaintext() -> None:
    """It is persisted and may be returned from a service, so it must be inert."""
    stored = seal_export_token(
        CredentialCipher(b"A" * 32), FAKE_TOKEN, household_id=_HOUSEHOLD, target_id=_TARGET
    )
    assert FAKE_TOKEN not in repr(stored)
    assert stored.last4 == FAKE_TOKEN[-4:]


def test_a_token_sealed_for_one_household_cannot_be_opened_by_another() -> None:
    """The AAD binding, which is what makes a stolen row unusable elsewhere.

    An attacker with write access copying a ciphertext onto another household's
    destination row must not thereby gain that household's ability to export --
    or, worse, file a stranger's groceries into their own account.
    """
    cipher = CredentialCipher(b"A" * 32)
    intact = _sealed(cipher)
    replayed = sealed_export_token(
        household_id=uuid.UUID(int=99),
        target_id=_TARGET,
        ciphertext=intact.ciphertext,
        key_id=intact.key_id,
    )

    with pytest.raises(ExportCredentialUnreadable) as raised:
        open_export_token(cipher, replayed)

    _assert_clean(raised.value)


# --------------------------------------------------------------------------- #
# Domain separation: the same master key, two purposes, no crossing over
# --------------------------------------------------------------------------- #


def test_an_export_token_does_not_open_under_the_provider_domain() -> None:
    """The prefix ADR-0010 made a prerequisite of the table, proved rather than asserted.

    Before it, an export token was sealed under
    ``chaudron/llm_provider_config/api_key/v1`` and cross-purpose replay was
    prevented by the row identifier alone -- which held only because two
    independently drawn UUIDs do not collide. This test is what makes the binding
    a property of the design: identical household, identical row identifier,
    identical master key, and the ciphertext still refuses to open as the other
    purpose.
    """
    cipher = CredentialCipher(b"A" * 32)
    stored = seal_export_token(cipher, FAKE_TOKEN, household_id=_HOUSEHOLD, target_id=_TARGET)
    as_provider_key = SealedCredential(
        _HOUSEHOLD, _TARGET, stored.ciphertext, stored.key_id, domain=PROVIDER_KEY_DOMAIN
    )

    with pytest.raises(CredentialDecryptionError) as raised:
        cipher.decrypt(as_provider_key)

    _assert_no_token(raised.value)


def test_a_provider_key_does_not_open_under_the_export_domain() -> None:
    """The other direction, which is the one an attacker would actually try.

    A household's model-provider key is worth money; lifting its ciphertext onto
    the export-target row of the *same* household -- where the identifiers could
    be made to match by choosing the row -- must not turn it into a token this
    application will send to a third party.
    """
    cipher = CredentialCipher(b"A" * 32)
    stored = cipher.encrypt(FAKE_TOKEN, household_id=_HOUSEHOLD, config_id=_TARGET)
    lifted = sealed_export_token(
        household_id=_HOUSEHOLD,
        target_id=_TARGET,
        ciphertext=stored.ciphertext,
        key_id=stored.key_id,
    )

    with pytest.raises(ExportCredentialUnreadable) as raised:
        open_export_token(cipher, lifted)

    _assert_clean(raised.value)


def test_the_two_domains_are_distinct_constants() -> None:
    """A copy-paste that made them equal would make both tests above pass vacuously.

    Written through a set rather than as ``!=`` because mypy narrows two ``Final``
    literals and rejects the comparison as non-overlapping -- which is the same
    property, proved at type-check time, but not one a reader of this file would
    notice had gone away.
    """
    domains: set[bytes] = {EXPORT_TOKEN_DOMAIN, PROVIDER_KEY_DOMAIN}
    assert len(domains) == 2
    assert EXPORT_TOKEN_DOMAIN == b"chaudron/shopping_export_target/token/v1"


def test_a_rotated_master_key_fails_loudly_and_says_which_variable(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The diagnostic goes to the operator; the household is told something else.

    It used to go to both, because :func:`open_export_token` re-raised the
    cipher's :class:`CredentialDecryptionError` unchanged and the sentence naming
    the environment variable was the only sentence there was. That error belongs
    to the model-provider hierarchy, so it crossed the export router uncaught and
    the household got a 500 (audit O-06) -- and the day it were caught, an
    instance-level remedy would be shown to somebody with no shell on the
    instance.

    Both halves are asserted here because losing either is a regression: an
    operator who cannot see which variable to restore is back to guessing, and a
    household told about ``CHAUDRON_CREDENTIAL_ENCRYPTION_KEY`` has been told
    about the instance's secrets.
    """
    intact = _sealed(CredentialCipher(b"A" * 32))

    with (
        caplog.at_level(logging.ERROR, logger="chaudron"),
        pytest.raises(ExportCredentialUnreadable) as raised,
    ):
        open_export_token(CredentialCipher(b"B" * 32), intact)

    # Read off the record rather than `caplog.text`: the diagnostic travels in an
    # `extra=` field, which the JSON formatter writes and pytest's default one
    # does not.
    (logged,) = [r for r in caplog.records if r.message == "export_token_undecryptable"]
    assert "CHAUDRON_CREDENTIAL_ENCRYPTION_KEY" in getattr(logged, "reason", "")
    assert "CHAUDRON_CREDENTIAL_ENCRYPTION_KEY" not in str(raised.value)
    assert "register the destination again" in str(raised.value)
    _assert_clean(raised.value)


def test_an_empty_token_is_refused_before_it_is_stored() -> None:
    with pytest.raises(ValueError, match="cannot be empty"):
        seal_export_token(
            CredentialCipher(b"A" * 32), "   ", household_id=_HOUSEHOLD, target_id=_TARGET
        )


# --------------------------------------------------------------------------- #
# The configuration path: the one endpoint that accepts a token
# --------------------------------------------------------------------------- #

_TARGETS_PATH = "/v1/shopping-lists/export/targets"
_TODOIST_PATH = f"{_TARGETS_PATH}/todoist"


def _registration(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"token": FAKE_TOKEN, "consent_granted": True}
    body.update(overrides)
    return body


async def test_registering_a_destination_never_echoes_the_token(
    api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    """The response to the very request that carried the token.

    The most obvious leak and the easiest one to write by accident: a handler
    that returns the model it was given. ``token_last4`` is asserted present in
    the same breath, because a response that leaked nothing by returning nothing
    would pass this test and be useless.
    """
    household = await make_household()
    response = await api_client.put(
        _TODOIST_PATH, json=_registration(), headers=household_headers(household)
    )

    assert response.status_code == 200, response.text
    assert FAKE_TOKEN not in response.text
    assert response.json()["token_last4"] == FAKE_TOKEN[-4:]


async def test_reading_a_destination_never_returns_the_token(
    api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    """Both reads: the availability list and the settings view."""
    household = await make_household()
    headers = household_headers(household)
    await api_client.put(_TODOIST_PATH, json=_registration(), headers=headers)

    for path in (_TARGETS_PATH, _TODOIST_PATH):
        response = await api_client.get(path, headers=headers)
        assert response.status_code == 200, response.text
        assert FAKE_TOKEN not in response.text

    # And after a withdrawal, when the row is still there to be rendered.
    revoked = await api_client.delete(_TODOIST_PATH, headers=headers)
    assert revoked.status_code == 200, revoked.text
    assert FAKE_TOKEN not in revoked.text


@pytest.mark.parametrize(
    ("path", "body"),
    [
        # Refused by the schema: pydantic would echo the submitted value in its
        # `input` field, and `api/errors.py` strips it. This is what pins that.
        (_TODOIST_PATH, {"token": FAKE_TOKEN[:4], "consent_granted": True}),
        # Refused by the schema on a *sibling* field, so the whole body -- token
        # included -- is what pydantic was looking at when it failed.
        (_TODOIST_PATH, _registration(external_list_id="not a project id")),
        # Refused by the service: no agreement.
        (_TODOIST_PATH, _registration(consent_granted=False)),
        # Refused by the service: no such destination in this build.
        (f"{_TARGETS_PATH}/bring", _registration()),
    ],
    ids=["token_too_short", "bad_list_id", "no_consent", "unsupported_target"],
)
async def test_a_refused_registration_never_quotes_the_token(
    api_client: httpx.AsyncClient,
    make_household: MakeHousehold,
    path: str,
    body: dict[str, Any],
) -> None:
    """Every refusal, including the ones where quoting the input is the natural thing."""
    household = await make_household()
    response = await api_client.put(path, json=body, headers=household_headers(household))

    assert response.status_code >= 400, response.text
    assert FAKE_TOKEN not in response.text
    assert FAKE_TOKEN[:4] not in response.text
    assert redact(response.text) == response.text


async def test_registering_a_destination_writes_no_token_to_the_log(
    api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    """A log file is forever, so a token in one outlives every rotation policy.

    Two pieces of plumbing, both of which are the reason this is not four lines of
    ``caplog``:

    * ``configure_logging`` clears the *root* handlers when the application is
      built, which happens during this test's own setup -- so the handler is
      attached to the emitting logger directly;
    * the session fixture runs Alembic, whose ``env.py`` calls ``fileConfig``, and
      ``logging.config`` disables every logger that already exists unless told
      otherwise. Every ``chaudron.*`` logger is therefore ``disabled`` for the
      rest of the suite, and a test that did not undo it would assert nothing
      while looking like it asserted something. Production is unaffected: Alembic
      runs there in its own process.
    """
    records: list[logging.LogRecord] = []

    class _Collector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger("chaudron.services.export_targets")
    handler = _Collector(level=logging.DEBUG)
    logger.addHandler(handler)
    previous, was_disabled = logger.level, logger.disabled
    logger.setLevel(logging.DEBUG)
    logger.disabled = False
    try:
        household = await make_household()
        headers = household_headers(household)
        assert (
            await api_client.put(_TODOIST_PATH, json=_registration(), headers=headers)
        ).status_code == 200
        assert (await api_client.delete(_TODOIST_PATH, headers=headers)).status_code == 200
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)
        logger.disabled = was_disabled

    assert records, "the registration path is expected to log that it happened"
    for record in records:
        rendered = f"{record.getMessage()} {record.__dict__}"
        assert FAKE_TOKEN not in rendered
        assert redact(rendered) == rendered


def test_no_operation_on_a_destination_takes_a_token_out_of_the_body(
    api_app: FastAPI,
) -> None:
    """A token in a path or a query string lands in access logs and browser history.

    Read off the generated OpenAPI document rather than off the route table, so
    it also covers a parameter added through a dependency -- which is the way one
    would get there without noticing.
    """
    schema = api_app.openapi()
    offenders: list[str] = []
    for path, operations in schema["paths"].items():
        if not path.startswith(_TARGETS_PATH):
            continue
        for method, operation in operations.items():
            for parameter in operation.get("parameters", []):
                if "token" in parameter["name"].lower():
                    offenders.append(f"{method.upper()} {path}: {parameter['name']}")
    assert not offenders, f"a token must never be a path or query parameter: {offenders}"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _assert_no_token(error: BaseException) -> None:
    for rendered in (str(error), repr(error)):
        assert FAKE_TOKEN not in rendered
        assert redact(rendered) == rendered, "nothing key-shaped should need scrubbing"


def _assert_clean(error: BaseException) -> None:
    """The message, its repr, and the whole chain behind it.

    ``__cause__`` matters as much as the message: ``raise ... from exc`` leaves
    the underlying failure one frame from any handler, and a handler logging
    ``exc_info`` writes whatever it holds to disk.
    """
    _assert_no_token(error)
    # "Bearer" as a bare word may survive -- it is not a secret. What must not
    # survive is a token behind it, which `redact` replaces with its marker.
    assert not _BEARER_WITH_VALUE.search(str(error))
    cause = error.__cause__
    assert cause is None, "an explicit chain prints with the traceback"
    context = error.__context__
    assert context is None or error.__suppress_context__, (
        "an implicit chain must be suppressed, so a formatted traceback stops here"
    )
    if context is not None:
        assert FAKE_TOKEN not in str(context)
