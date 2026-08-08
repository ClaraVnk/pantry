"""Adding a household, re-authenticating it, and tuning how often it is polled.

**The credential is checked before it is stored.** One ``GET /v1/locations`` --
the cheapest route guarded by the one scope this integration cannot work
without. A token that is revoked, expired or issued by somebody who has since
left the household comes back ``401``; one that is fine but was minted without
``inventory:read`` comes back ``403`` naming the scopes it *does* hold. The two
are different mistakes and get different sentences.

**The identity of an entry is the credential, not the household.** A machine
token names one household and the backend publishes no household identifier to
it, so there is nothing else to key on. The unique identifier is therefore a
digest of the instance URL and the token: adding the same token twice is
refused, adding a second household on the same instance is not, and re-issuing a
token goes through re-authentication, which moves the identifier with it.
"""

from __future__ import annotations

from hashlib import sha256
from typing import Any

from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_ACCESS_TOKEN, CONF_URL
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
import voluptuous as vol
from yarl import URL

from .api import (
    ChaudronAuthError,
    ChaudronClient,
    ChaudronConnectionError,
    ChaudronError,
    ChaudronScopeError,
    normalise_base_url,
)
from .const import (
    CONF_EXPIRING_WITHIN_DAYS,
    CONF_SCAN_INTERVAL_MINUTES,
    DEFAULT_EXPIRING_WITHIN_DAYS,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    LOGGER,
    MAX_EXPIRING_WITHIN_DAYS,
    MAX_SCAN_INTERVAL_MINUTES,
    MIN_EXPIRING_WITHIN_DAYS,
    MIN_SCAN_INTERVAL_MINUTES,
    TOKEN_PREFIX,
)
from .coordinator import ChaudronConfigEntry

_TOKEN_FIELD = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))

USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_URL): TextSelector(
            TextSelectorConfig(type=TextSelectorType.URL)
        ),
        vol.Required(CONF_ACCESS_TOKEN): _TOKEN_FIELD,
    }
)

REAUTH_SCHEMA = vol.Schema({vol.Required(CONF_ACCESS_TOKEN): _TOKEN_FIELD})


class ChaudronConfigFlow(ConfigFlow, domain=DOMAIN):
    """Connect one Chaudron household."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """The only entrance.

        There is no discovery step and there will not be one: a self-hosted
        instance behind a reverse proxy advertises nothing on the local network,
        and the household types the address it already uses in a browser.
        """
        errors: dict[str, str] = {}
        if user_input is not None:
            url, error, field = await self._async_validate(
                user_input[CONF_URL], user_input[CONF_ACCESS_TOKEN]
            )
            if error is None:
                await self.async_set_unique_id(
                    _fingerprint(url, user_input[CONF_ACCESS_TOKEN])
                )
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=_title(url),
                    data={
                        CONF_URL: url,
                        CONF_ACCESS_TOKEN: user_input[CONF_ACCESS_TOKEN],
                    },
                )
            errors[field] = error

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(USER_SCHEMA, user_input),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """Entered when a poll is answered ``401``: the token has to be replaced."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Take a fresh token for the instance already configured.

        The URL is not asked for again -- moving an entry to another instance is
        a reconfiguration, not a re-authentication, and conflating the two lets a
        mistyped host quietly become a different household.
        """
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            url, error, field = await self._async_validate(
                entry.data[CONF_URL], user_input[CONF_ACCESS_TOKEN]
            )
            if error is None:
                return self.async_update_reload_and_abort(
                    entry,
                    # The identifier follows the credential it was derived from;
                    # left stale, a later attempt to add the new token by hand
                    # would not be recognised as the entry that already holds it.
                    unique_id=_fingerprint(url, user_input[CONF_ACCESS_TOKEN]),
                    data_updates={CONF_ACCESS_TOKEN: user_input[CONF_ACCESS_TOKEN]},
                )
            errors[field] = error

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=REAUTH_SCHEMA,
            description_placeholders={"url": entry.data[CONF_URL]},
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Point an existing entry at another URL, another token, or both."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            url, error, field = await self._async_validate(
                user_input[CONF_URL], user_input[CONF_ACCESS_TOKEN]
            )
            if error is None:
                await self.async_set_unique_id(
                    _fingerprint(url, user_input[CONF_ACCESS_TOKEN])
                )
                self._abort_if_unique_id_mismatch(reason="already_configured")
                return self.async_update_reload_and_abort(
                    entry,
                    title=_title(url),
                    data_updates={
                        CONF_URL: url,
                        CONF_ACCESS_TOKEN: user_input[CONF_ACCESS_TOKEN],
                    },
                )
            errors[field] = error

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                USER_SCHEMA, user_input or dict(entry.data)
            ),
            errors=errors,
        )

    async def _async_validate(
        self, raw_url: str, token: str
    ) -> tuple[str, str | None, str]:
        """``(url, error, field)``. ``error`` is ``None`` when the token works.

        The prefix check is not security -- the backend decides that -- it is
        there so pasting a session cookie, a password or a provider key is
        refused at the form, next to the field, rather than by a ``401`` that
        reads as "your token was revoked".
        """
        try:
            url = normalise_base_url(raw_url)
        except ValueError:
            return raw_url, "invalid_url", CONF_URL

        if not token.startswith(TOKEN_PREFIX):
            return url, "invalid_token_format", CONF_ACCESS_TOKEN

        client = ChaudronClient(async_get_clientsession(self.hass), url, token)
        try:
            await client.async_verify()
        except ChaudronAuthError:
            return url, "invalid_auth", CONF_ACCESS_TOKEN
        except ChaudronScopeError:
            return url, "insufficient_scope", CONF_ACCESS_TOKEN
        except ChaudronConnectionError:
            return url, "cannot_connect", CONF_URL
        except ChaudronError as err:
            # Logged without the client, whose header dictionary holds the token.
            LOGGER.debug("Chaudron refused the verification call: %s", err)
            return url, "unknown", "base"
        return url, None, "base"

    @staticmethod
    @callback
    def async_get_options_flow(entry: ChaudronConfigEntry) -> ChaudronOptionsFlow:
        """The options this entry offers once it is set up."""
        return ChaudronOptionsFlow()


class ChaudronOptionsFlow(OptionsFlowWithReload):
    """How often to poll, and what counts as "soon"."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the two numbers, and reload the entry when either changes."""
        if user_input is not None:
            return self.async_create_entry(
                data={
                    CONF_EXPIRING_WITHIN_DAYS: int(
                        user_input[CONF_EXPIRING_WITHIN_DAYS]
                    ),
                    CONF_SCAN_INTERVAL_MINUTES: int(
                        user_input[CONF_SCAN_INTERVAL_MINUTES]
                    ),
                }
            )

        options = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(CONF_EXPIRING_WITHIN_DAYS): NumberSelector(
                    NumberSelectorConfig(
                        min=MIN_EXPIRING_WITHIN_DAYS,
                        max=MAX_EXPIRING_WITHIN_DAYS,
                        step=1,
                        mode=NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(CONF_SCAN_INTERVAL_MINUTES): NumberSelector(
                    NumberSelectorConfig(
                        min=MIN_SCAN_INTERVAL_MINUTES,
                        max=MAX_SCAN_INTERVAL_MINUTES,
                        step=1,
                        mode=NumberSelectorMode.BOX,
                    )
                ),
            }
        )
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                schema,
                {
                    CONF_EXPIRING_WITHIN_DAYS: options.get(
                        CONF_EXPIRING_WITHIN_DAYS, DEFAULT_EXPIRING_WITHIN_DAYS
                    ),
                    CONF_SCAN_INTERVAL_MINUTES: options.get(
                        CONF_SCAN_INTERVAL_MINUTES,
                        int(DEFAULT_SCAN_INTERVAL.total_seconds() // 60),
                    ),
                },
            ),
        )


def _fingerprint(url: str, token: str) -> str:
    """A stable identifier for "this credential on this instance".

    A digest, never the value: a unique identifier is stored in ``.storage`` in
    clear and shown in diagnostics, so keying on the token itself would publish
    it. SHA-256 over 256 bits of entropy has no dictionary to grind, and half the
    digest is far more than enough to separate the handful of entries one Home
    Assistant will ever hold.
    """
    return sha256(f"{url}|{token}".encode()).hexdigest()[:32]


def _title(url: str) -> str:
    """The host, which is what a person recognises in a list of integrations."""
    return URL(url).host or url
