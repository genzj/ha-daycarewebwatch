"""Tests for the config flow."""

from __future__ import annotations

from unittest.mock import AsyncMock

from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.daycarewebwatch.api import (
    CannotConnect,
    InvalidAuth,
    UnsupportedApiVersion,
)
from custom_components.daycarewebwatch.const import CONF_INSTANCE_ID, DOMAIN

from .conftest import EMAIL, INSTANCE_ID, PASSWORD


async def test_user_flow(hass: HomeAssistant, mock_client: AsyncMock) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"email": f" {EMAIL} ", "password": PASSWORD}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == EMAIL
    assert result["data"]["email"] == EMAIL
    assert len(result["data"][CONF_INSTANCE_ID]) == 36
    assert result["result"].unique_id == EMAIL


@pytest.mark.parametrize(
    ("exc", "error"),
    [
        (InvalidAuth, "invalid_auth"),
        (CannotConnect, "cannot_connect"),
        (UnsupportedApiVersion, "unsupported_api_version"),
        (RuntimeError, "unknown"),
    ],
)
async def test_user_flow_errors_recover(
    hass: HomeAssistant, mock_client: AsyncMock, exc: type[Exception], error: str
) -> None:
    mock_client.async_login.side_effect = exc
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"email": EMAIL, "password": PASSWORD}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}

    mock_client.async_login.side_effect = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"email": EMAIL, "password": PASSWORD}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_user_flow_duplicate(
    hass: HomeAssistant, mock_client: AsyncMock, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"email": EMAIL.upper(), "password": PASSWORD}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth(
    hass: HomeAssistant, mock_client: AsyncMock, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    result = await config_entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    mock_client.async_login.side_effect = InvalidAuth
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"password": "x"})
    assert result["errors"] == {"base": "invalid_auth"}
    mock_client.async_login.side_effect = None
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"password": "new"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    await hass.async_block_till_done()  # let the triggered reload finish
    assert config_entry.data["password"] == "new"
    assert config_entry.data[CONF_INSTANCE_ID] == INSTANCE_ID


async def test_reconfigure(
    hass: HomeAssistant, mock_client: AsyncMock, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    result = await config_entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure"
    mock_client.async_login.side_effect = CannotConnect
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"email": EMAIL, "password": "new"}
    )
    assert result["errors"] == {"base": "cannot_connect"}
    mock_client.async_login.side_effect = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"email": EMAIL, "password": "new"}
    )
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()  # let the triggered reload finish
    assert config_entry.data["password"] == "new"


async def test_reconfigure_wrong_account(
    hass: HomeAssistant, mock_client: AsyncMock, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    result = await config_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"email": "other@example.com", "password": "new"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_account"


async def test_options_flow(
    hass: HomeAssistant, mock_client: AsyncMock, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    hass.config.time_zone = "America/Vancouver"
    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["description_placeholders"] == {"time_zone": "America/Vancouver"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"window_start": "18:00:00", "window_end": "07:00:00", "window_days": ["mon"]},
    )
    assert result["errors"] == {"window_end": "end_before_start"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "window_start": "07:00:00",
            "window_end": "18:00:00",
            "window_days": ["mon", "sat"],
            "use_webrtc": True,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert config_entry.options["use_webrtc"] is True
    await hass.async_block_till_done()
    assert config_entry.runtime_data.status.window.days == frozenset({0, 5})
