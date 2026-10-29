"""The DaycareWebWatch integration."""

from __future__ import annotations

from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceEntry

from .api import DaycareWebWatchClient
from .const import CONF_INSTANCE_ID, DOMAIN
from .coordinator import (
    CameraListCoordinator,
    DaycareWebWatchConfigEntry,
    DaycareWebWatchData,
    ViewingStatusCoordinator,
)

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.CAMERA]


async def async_setup_entry(hass: HomeAssistant, entry: DaycareWebWatchConfigEntry) -> bool:
    """Set up DaycareWebWatch from a config entry."""
    client = DaycareWebWatchClient(
        async_get_clientsession(hass),
        entry.data[CONF_EMAIL],
        entry.data[CONF_PASSWORD],
        entry.data[CONF_INSTANCE_ID],
    )
    cameras = CameraListCoordinator(hass, entry, client)
    await cameras.async_config_entry_first_refresh()
    status = ViewingStatusCoordinator(hass, entry, client, cameras)
    await status.async_config_entry_first_refresh()

    entry.runtime_data = DaycareWebWatchData(client=client, cameras=cameras, status=status)

    # Probe new cameras as soon as they appear in the list (only then, so the
    # slow night/weekend status polling is not undone by list refreshes).
    @callback
    def _probe_new_cameras() -> None:
        if set(cameras.data or {}) - set(status.data or {}):
            hass.async_create_task(status.async_request_refresh())

    entry.async_on_unload(cameras.async_add_listener(_probe_new_cameras))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: DaycareWebWatchConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: DaycareWebWatchConfigEntry, device: DeviceEntry
) -> bool:
    """Allow removing a device only once its camera is gone from the account."""
    cameras = entry.runtime_data.cameras.data or {}
    return not any(
        domain == DOMAIN and identifier in {str(cam_id) for cam_id in cameras}
        for domain, identifier in device.identifiers
    )
