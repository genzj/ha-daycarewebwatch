"""Diagnostics support for DaycareWebWatch."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant

from .const import CONF_INSTANCE_ID
from .coordinator import DaycareWebWatchConfigEntry

TO_REDACT = {CONF_EMAIL, CONF_PASSWORD, CONF_INSTANCE_ID, "title", "unique_id", "url", "children"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: DaycareWebWatchConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    data = entry.runtime_data
    return {
        "entry": async_redact_data(entry.as_dict(), TO_REDACT),
        "cameras": [
            async_redact_data(asdict(camera), TO_REDACT)
            for camera in (data.cameras.data or {}).values()
        ],
        "viewing_status": {
            str(camera_id): {"open": status.is_open, "code": status.code}
            for camera_id, status in (data.status.data or {}).items()
        },
        "status_update_interval": str(data.status.update_interval),
        "last_update_success": {
            "cameras": data.cameras.last_update_success,
            "status": data.status.last_update_success,
        },
    }
