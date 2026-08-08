"""The config flow refuses the wrong credential before it stores anything."""

from __future__ import annotations

from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.chaudron.const import DOMAIN

from .conftest import BASE_URL, LOCATIONS_URL, TOKEN


async def _start(hass: HomeAssistant) -> str:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    return result["flow_id"]


async def test_a_working_token_creates_an_entry(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """The happy path, and the only one that stores the token."""
    aioclient_mock.get(LOCATIONS_URL, json=[])
    result = await hass.config_entries.flow.async_configure(
        await _start(hass), {"url": BASE_URL, "access_token": TOKEN}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "chaudron.example.org"
    assert result["data"] == {"url": BASE_URL, "access_token": TOKEN}


async def test_a_trailing_slash_is_not_a_second_instance(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Otherwise the same instance typed twice would be two entries."""
    aioclient_mock.get(LOCATIONS_URL, json=[])
    result = await hass.config_entries.flow.async_configure(
        await _start(hass), {"url": f"{BASE_URL}/", "access_token": TOKEN}
    )
    assert result["data"]["url"] == BASE_URL


@pytest.mark.parametrize(
    ("status", "payload", "expected"),
    [
        (
            401,
            {"type": "https://chaudron.dev/problems/token-not-accepted"},
            "invalid_auth",
        ),
        (
            403,
            {
                "type": "https://chaudron.dev/problems/insufficient-scope",
                "required_scopes": ["inventory:read"],
                "granted_scopes": ["shopping:read"],
            },
            "insufficient_scope",
        ),
        (500, {}, "unknown"),
    ],
)
async def test_a_refusal_is_reported_next_to_the_field(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    status: int,
    payload: dict,
    expected: str,
) -> None:
    """Each refusal the backend documents gets its own sentence."""
    aioclient_mock.get(LOCATIONS_URL, status=status, json=payload)
    result = await hass.config_entries.flow.async_configure(
        await _start(hass), {"url": BASE_URL, "access_token": TOKEN}
    )

    assert result["type"] is FlowResultType.FORM
    assert expected in result["errors"].values()


async def test_a_value_that_is_not_a_token_never_reaches_the_network(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Pasting a password must not be reported as "your token was revoked"."""
    result = await hass.config_entries.flow.async_configure(
        await _start(hass), {"url": BASE_URL, "access_token": "hunter2hunter2hunter2"}
    )

    assert result["errors"] == {"access_token": "invalid_token_format"}
    assert aioclient_mock.call_count == 0


async def test_an_unreachable_instance_blames_the_url(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A connection failure is a URL problem far more often than a token one."""
    aioclient_mock.get(LOCATIONS_URL, exc=TimeoutError)
    result = await hass.config_entries.flow.async_configure(
        await _start(hass), {"url": BASE_URL, "access_token": TOKEN}
    )

    assert result["errors"] == {"url": "cannot_connect"}


async def test_a_url_without_a_scheme_is_refused_at_the_form(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Guessing http or https would either break LAN installs or leak the token."""
    result = await hass.config_entries.flow.async_configure(
        await _start(hass), {"url": "chaudron.example.org", "access_token": TOKEN}
    )

    assert result["errors"] == {"url": "invalid_url"}
    assert aioclient_mock.call_count == 0


async def test_the_same_token_cannot_be_added_twice(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A second household on the same instance is fine; the same token is not."""
    aioclient_mock.get(LOCATIONS_URL, json=[])
    first = await hass.config_entries.flow.async_configure(
        await _start(hass), {"url": BASE_URL, "access_token": TOKEN}
    )
    assert first["type"] is FlowResultType.CREATE_ENTRY

    second = await hass.config_entries.flow.async_configure(
        await _start(hass), {"url": BASE_URL, "access_token": TOKEN}
    )
    assert second["type"] is FlowResultType.ABORT
    assert second["reason"] == "already_configured"


async def test_reauthentication_replaces_the_token_and_its_fingerprint(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
) -> None:
    """A re-issued token must not leave the entry keyed on the dead one."""
    config_entry.add_to_hass(hass)
    aioclient_mock.get(LOCATIONS_URL, json=[])
    replacement = "chdr_" + "b" * 43

    result = await config_entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"access_token": replacement}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert config_entry.data["access_token"] == replacement
    assert config_entry.unique_id != "0123456789abcdef0123456789abcdef"
