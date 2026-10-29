"""Config flow for DaycareWebWatch."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any
import uuid

from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
    TimeSelector,
)
import probatio as vol

from .api import CannotConnect, DaycareWebWatchClient, InvalidAuth, UnsupportedApiVersion
from .const import (
    CONF_INSTANCE_ID,
    CONF_WINDOW_DAYS,
    CONF_WINDOW_END,
    CONF_WINDOW_START,
    DEFAULT_WINDOW_DAYS,
    DEFAULT_WINDOW_END,
    DEFAULT_WINDOW_START,
    DOMAIN,
    WEEKDAYS,
)
from .coordinator import DaycareWebWatchConfigEntry

_LOGGER = logging.getLogger(__name__)

EMAIL_SELECTOR = TextSelector(
    TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username")
)
PASSWORD_SELECTOR = TextSelector(
    TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="current-password")
)


def _credentials_schema(email: str | None = None) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_EMAIL, default=email or vol.UNDEFINED): EMAIL_SELECTOR,
            vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR,
        }
    )


class DaycareWebWatchConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for DaycareWebWatch."""

    VERSION = 1

    async def _async_validate(self, email: str, password: str, instance_id: str) -> dict[str, str]:
        """Try to log in; return form errors."""
        client = DaycareWebWatchClient(
            async_get_clientsession(self.hass), email, password, instance_id
        )
        try:
            await client.async_login()
        except InvalidAuth:
            return {"base": "invalid_auth"}
        except UnsupportedApiVersion:
            return {"base": "unsupported_api_version"}
        except CannotConnect:
            return {"base": "cannot_connect"}
        except Exception:
            _LOGGER.exception("Unexpected error during login")
            return {"base": "unknown"}
        return {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for account credentials."""
        errors: dict[str, str] = {}
        if user_input is not None:
            email = user_input[CONF_EMAIL].strip()
            await self.async_set_unique_id(email.lower())
            self._abort_if_unique_id_configured()
            instance_id = str(uuid.uuid4())
            errors = await self._async_validate(email, user_input[CONF_PASSWORD], instance_id)
            if not errors:
                return self.async_create_entry(
                    title=email,
                    data={
                        CONF_EMAIL: email,
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                        CONF_INSTANCE_ID: instance_id,
                    },
                )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(_credentials_schema(), user_input),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Start reauthentication."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the new password."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = await self._async_validate(
                entry.data[CONF_EMAIL],
                user_input[CONF_PASSWORD],
                entry.data[CONF_INSTANCE_ID],
            )
            if not errors:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_PASSWORD: user_input[CONF_PASSWORD]}
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR}),
            description_placeholders={"email": entry.data[CONF_EMAIL]},
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Update the credentials of the same account."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            email = user_input[CONF_EMAIL].strip()
            await self.async_set_unique_id(email.lower())
            self._abort_if_unique_id_mismatch(reason="wrong_account")
            errors = await self._async_validate(
                email, user_input[CONF_PASSWORD], entry.data[CONF_INSTANCE_ID]
            )
            if not errors:
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={
                        CONF_EMAIL: email,
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                    },
                )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_credentials_schema(entry.data[CONF_EMAIL]),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: DaycareWebWatchConfigEntry,
    ) -> DaycareWebWatchOptionsFlow:
        """Return the options flow."""
        return DaycareWebWatchOptionsFlow()


class DaycareWebWatchOptionsFlow(OptionsFlowWithReload):
    """Configure the expected viewing window."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Manage the options."""
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input[CONF_WINDOW_START] >= user_input[CONF_WINDOW_END]:
                errors[CONF_WINDOW_END] = "end_before_start"
            else:
                return self.async_create_entry(data=user_input)
        options = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_WINDOW_START,
                    default=options.get(CONF_WINDOW_START, DEFAULT_WINDOW_START.isoformat()),
                ): TimeSelector(),
                vol.Required(
                    CONF_WINDOW_END,
                    default=options.get(CONF_WINDOW_END, DEFAULT_WINDOW_END.isoformat()),
                ): TimeSelector(),
                vol.Required(
                    CONF_WINDOW_DAYS,
                    default=options.get(CONF_WINDOW_DAYS, DEFAULT_WINDOW_DAYS),
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=WEEKDAYS,
                        multiple=True,
                        mode=SelectSelectorMode.LIST,
                        translation_key="weekday",
                    )
                ),
            }
        )
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(schema, user_input),
            errors=errors,
        )
