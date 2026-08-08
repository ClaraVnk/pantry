"""Resolve a household's export destination into an adapter.

The same job ``infra/llm/factory.py`` does for model providers, and the same rule
governs it: the service that assembles a shopping list receives a
:class:`~chaudron.domain.shopping_export.ShoppingListExporter` and cannot tell
which destination answered. Selection happens here because a destination is *data
about the household*, not a deployment constant.

Two sources, tried in that order, and the order matters:

1. **The household's own registered destination**, from
   :class:`~chaudron.infra.todo.store.ShoppingExportTargetStore`. This is the real
   answer and the only one that scales past one household. Backed by
   ``shopping_export_target`` since migration ``0008`` and wired in
   ``api/routers/shopping_export.py``; ``store`` stays optional so a caller that
   has no session -- a test, a script -- can still resolve the instance
   destination below.
2. **The instance's own destination**, from the environment, usable by exactly
   one household. This is ``instance_owner`` from ADR-0007 applied to a different
   secret, and for the identical reason: the token belongs to the operator, and a
   token every household could use would put strangers' groceries in the
   operator's list. Unset means nobody, which is the closed default the failure
   mode demands.

Consent is checked here rather than in the adapters, because it is a property of
the *arrangement*, not of the protocol. For a stored target it is the dated column
the row carries. For the instance target it is the operator having written their
own token into their own instance's environment for their own household -- an act
that is explicit, informed and revocable by deleting a line, which is what the
requirement asks for. ADR-0010 records both readings.
"""

from __future__ import annotations

import uuid

import httpx

from chaudron.domain.shopping_export import (
    ExportContext,
    ExportRegistrantHasLeft,
    ExportTargetNotConfigured,
    ShoppingExportConsentMissing,
    ShoppingListExporter,
)
from chaudron.infra.crypto import CredentialCipher
from chaudron.infra.todo.credentials import open_export_token
from chaudron.infra.todo.settings import TODOIST_TOKEN_ENV_VAR, TodoExportSettings
from chaudron.infra.todo.store import ShoppingExportTargetStore
from chaudron.infra.todo.todoist import TARGET_CODE as TODOIST_CODE
from chaudron.infra.todo.todoist import TodoistExporter, build_todoist_client

__all__ = ["SUPPORTED_TARGETS", "ShoppingExportFactory"]

#: Every destination this instance can send to. One entry, and ADR-0010 argues
#: the emptiness of the rest of the set rather than leaving it to look accidental.
SUPPORTED_TARGETS: frozenset[str] = frozenset({TODOIST_CODE})


class ShoppingExportFactory:
    """Turns ``(household, target code)`` into something that can send a list.

    ``transport`` is the test seam: the adapter under test is the real one, only
    the socket is a double. ``cipher`` is what makes a *stored* destination
    usable; a household whose token is sealed on an instance with no cipher is
    refused rather than silently downgraded, because an instance that cannot
    decrypt has to say so.
    """

    __slots__ = ("_cipher", "_settings", "_store", "_transport")

    def __init__(
        self,
        settings: TodoExportSettings,
        *,
        cipher: CredentialCipher | None = None,
        store: ShoppingExportTargetStore | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._cipher = cipher
        self._store = store
        self._transport = transport

    def supports(self, target_code: str) -> bool:
        return target_code in SUPPORTED_TARGETS

    async def exporter_for(self, household_id: uuid.UUID, target_code: str) -> ShoppingListExporter:
        """Build the adapter, or explain precisely why there is none.

        Every refusal names a remedy. "Not configured" without one is the support
        ticket this whole layer exists to avoid: a household cannot tell an
        unregistered destination from a revoked token from a feature the operator
        never switched on.
        """
        if not self.supports(target_code):
            supported = ", ".join(sorted(SUPPORTED_TARGETS))
            raise ExportTargetNotConfigured(
                f"{target_code!r} is not a destination this instance can send to; "
                f"supported: {supported}",
                context=ExportContext(target_code, "unknown_target"),
            )

        stored = await self._stored_exporter(household_id, target_code)
        if stored is not None:
            return stored
        return self._instance_exporter(household_id, target_code)

    # -- the household's own destination ----------------------------------- #

    async def _stored_exporter(
        self, household_id: uuid.UUID, target_code: str
    ) -> ShoppingListExporter | None:
        """The registered row, if a store is wired and holds one.

        Returns ``None`` -- rather than refusing -- when there is no store or no
        row, so the instance destination below still gets its turn. A row that
        exists but cannot be used is a different matter and raises: silently
        falling through to the operator's own account would send one household's
        list to another's Todoist.
        """
        if self._store is None:
            return None
        target = await self._store.target_for(household_id, target_code)
        if target is None:
            return None
        if not target.is_consented:
            raise ShoppingExportConsentMissing(
                "this household has not agreed to its shopping list being sent to "
                f"{target_code}, or has withdrawn that agreement; grant it again "
                "from the export settings",
                context=ExportContext(target_code, "consent_missing"),
            )
        if target.registrant_has_left:
            # Checked here, next to the consent, because it *is* the consent: the
            # agreement was given by somebody who is no longer part of this
            # household, and the token behind it files into their personal
            # account. Nothing cascades from `household_member` to this table, so
            # without this the row would keep exporting for as long as it existed.
            raise ExportRegistrantHasLeft(
                "the person who registered this "
                f"{target_code} destination is no longer a member of this household, "
                "so their agreement no longer stands and their token is still the one "
                "in use; an owner must register a destination again or withdraw it",
                context=ExportContext(target_code, "registrant_has_left"),
            )
        if self._cipher is None:
            raise ExportTargetNotConfigured(
                "this instance cannot decrypt stored destination tokens; it was "
                "started without a credential cipher",
                context=ExportContext(target_code, "no_cipher"),
            )
        # The plaintext begins and ends inside this method.
        token = open_export_token(self._cipher, target.sealed_token)
        return self._build(target_code, token, project_id=target.external_list_id)

    # -- the instance's own destination ------------------------------------ #

    def _instance_exporter(self, household_id: uuid.UUID, target_code: str) -> ShoppingListExporter:
        """The operator's own token, for the operator's own household, or nothing."""
        if target_code != TODOIST_CODE:
            raise ExportTargetNotConfigured(
                f"no {target_code} destination is registered for this household",
                context=ExportContext(target_code, "not_registered"),
            )
        owner = self._settings.todoist_household_id
        token = self._settings.todoist_token
        if owner is None or not token:
            raise ExportTargetNotConfigured(
                "no Todoist destination is registered for this household, and this "
                f"instance holds none of its own; set {TODOIST_TOKEN_ENV_VAR} "
                "(podman secret) and restart, or register a token for the household",
                context=ExportContext(target_code, "not_registered"),
            )
        if household_id != owner:
            # The locked door of ADR-0007, transposed. The failure this prevents
            # is a shared instance quietly filing every household's groceries
            # into the operator's personal Todoist.
            raise ExportTargetNotConfigured(
                "this instance's Todoist destination belongs to another household; "
                "register your own token to export your list",
                context=ExportContext(target_code, "not_permitted"),
            )
        return self._build(target_code, token, project_id=self._settings.todoist_project_id)

    def _build(
        self, target_code: str, token: str, *, project_id: str | None
    ) -> ShoppingListExporter:
        if target_code == TODOIST_CODE:
            return TodoistExporter(
                build_todoist_client(token, self._settings, transport=self._transport),
                project_id=project_id,
            )
        raise ExportTargetNotConfigured(  # pragma: no cover -- guarded by `supports`
            f"no adapter for destination {target_code!r}",
            context=ExportContext(target_code, "unknown_target"),
        )
