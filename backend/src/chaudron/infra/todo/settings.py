"""Instance settings for the outbound export calls, read from the environment.

Deliberately *not* modelled on ``infra/llm/settings.py``'s allowlist, and the
difference is the whole point. An Ollama base URL is supplied by a household and
dialled by the server, which is an SSRF primitive and needs an operator-declared
allowlist of endpoints. The destinations here are compiled-in constants
(``api.todoist.com``): no household can move them, so there is nothing for an
allowlist to decide.

What remains necessary is the *other* half of that module's job -- the bounds. A
fixed hostname is not a promise of good behaviour: DNS can be poisoned, a proxy
can be hostile, and a vendor can have a bad afternoon. So the call is bounded in
time, in response size and in redirects (``infra/todo/http.py``), and those
bounds are configurable because an operator behind a slow link is a real case and
editing a constant is not a remedy.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from typing import Final

__all__ = [
    "MAX_COMMANDS_PER_REQUEST",
    "TODOIST_HOUSEHOLD_ENV_VAR",
    "TODOIST_PROJECT_ENV_VAR",
    "TODOIST_TOKEN_ENV_VAR",
    "TodoExportSettings",
    "load_settings",
]

_TIMEOUT_ENV_VAR: Final = "CHAUDRON_TODO_EXPORT_TIMEOUT_SECONDS"
_MAX_RESPONSE_ENV_VAR: Final = "CHAUDRON_TODO_EXPORT_MAX_RESPONSE_BYTES"

#: The instance-owned Todoist destination, and its single permitted household.
#: The pairing mirrors ``instance_owner`` in ADR-0007 for the same reason: this
#: token belongs to a person, and a token usable by every household on the
#: instance would put a stranger's groceries in the operator's own list.
TODOIST_HOUSEHOLD_ENV_VAR: Final = "CHAUDRON_TODOIST_HOUSEHOLD_ID"
TODOIST_TOKEN_ENV_VAR: Final = "CHAUDRON_TODOIST_TOKEN"  # noqa: S105 -- a variable name
TODOIST_PROJECT_ENV_VAR: Final = "CHAUDRON_TODOIST_PROJECT_ID"

#: An export is a user waiting on a button. Fifteen seconds is generous for a
#: single API call and short enough that a hung endpoint is a visible failure
#: rather than a spinner nobody can explain.
_DEFAULT_TIMEOUT_SECONDS: Final = 15.0

#: A sync acknowledgement is a few hundred bytes per command. 512 KiB leaves two
#: orders of magnitude of headroom and still bounds what a hostile or
#: misconfigured endpoint can make this process allocate.
_DEFAULT_MAX_RESPONSE_BYTES: Final = 512 * 1024

#: Commands sent in one request. Todoist's own documentation points at a
#: "Request Limits" section for the number it allows, and that section did not
#: render when it was checked on 2026-08-04 -- so this value is *our* bound, not
#: a quoted one, and it is set low enough to be safe under any plausible published
#: limit. A longer list is sent as successive batches rather than refused; a
#: household with more than a hundred pending items still gets its list.
MAX_COMMANDS_PER_REQUEST: Final = 100


@dataclass(frozen=True, slots=True)
class TodoExportSettings:
    """Bounds and the optional instance-owned destination."""

    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS
    max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES
    #: The one household allowed to use the instance's own Todoist token.
    #: ``None`` means the instance destination is off, which is the default.
    todoist_household_id: uuid.UUID | None = None
    #: ``repr=False``: dumping the settings in a log line, a traceback or a
    #: debugger must not print a third party's billable secret (SEC-003).
    todoist_token: str | None = field(default=None, repr=False)
    #: Where items land. ``None`` means the account's Inbox, which is what
    #: Todoist does when ``item_add`` carries no ``project_id``.
    todoist_project_id: str | None = None

    @property
    def has_instance_todoist(self) -> bool:
        return self.todoist_household_id is not None and bool(self.todoist_token)


def _parse_household_id(raw: str | None) -> uuid.UUID | None:
    if not raw or not raw.strip():
        return None
    try:
        return uuid.UUID(raw.strip())
    except ValueError as exc:
        # Fail fast. A typo here does not disable the feature quietly -- it would
        # point the operator's own shopping list at nobody, or, if the comparison
        # were ever loosened, at somebody else.
        raise ValueError(f"{TODOIST_HOUSEHOLD_ENV_VAR} is not a valid UUID") from exc


def _parse_positive_float(raw: str | None, default: float, name: str) -> float:
    if not raw or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} is not a number") from exc
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return value


def _parse_positive_int(raw: str | None, default: int, name: str) -> int:
    if not raw or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} is not an integer") from exc
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return value


def load_settings(environ: dict[str, str] | None = None) -> TodoExportSettings:
    """Build the settings, raising rather than guessing on malformed input."""
    env = os.environ if environ is None else environ
    return TodoExportSettings(
        timeout_seconds=_parse_positive_float(
            env.get(_TIMEOUT_ENV_VAR), _DEFAULT_TIMEOUT_SECONDS, _TIMEOUT_ENV_VAR
        ),
        max_response_bytes=_parse_positive_int(
            env.get(_MAX_RESPONSE_ENV_VAR), _DEFAULT_MAX_RESPONSE_BYTES, _MAX_RESPONSE_ENV_VAR
        ),
        todoist_household_id=_parse_household_id(env.get(TODOIST_HOUSEHOLD_ENV_VAR)),
        todoist_token=(env.get(TODOIST_TOKEN_ENV_VAR) or "").strip() or None,
        todoist_project_id=(env.get(TODOIST_PROJECT_ENV_VAR) or "").strip() or None,
    )
