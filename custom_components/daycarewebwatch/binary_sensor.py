"""Binary sensor platform for DaycareWebWatch."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import Camera
from .coordinator import DaycareWebWatchConfigEntry, DaycareWebWatchData
from .entity import DaycareWebWatchEntity
from .helpers import async_add_new_cameras

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DaycareWebWatchConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up binary sensors."""
    async_add_new_cameras(
        entry,
        lambda cameras: async_add_entities(
            ViewingAvailableSensor(entry.runtime_data, camera) for camera in cameras
        ),
    )


class ViewingAvailableSensor(DaycareWebWatchEntity, BinarySensorEntity):
    """On while the camera can be viewed (daycare open, camera on)."""

    _attr_translation_key = "viewing_available"

    def __init__(self, data: DaycareWebWatchData, camera: Camera) -> None:
        """Initialize."""
        super().__init__(data, camera, "viewing_available")

    @property
    def is_on(self) -> bool:
        """Return True if viewable."""
        return self.coordinator.is_open(self.camera_id)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Why viewing is unavailable (the server's message and code)."""
        status = self.coordinator.status(self.camera_id)
        if status is None or status.is_open:
            return {"reason": None, "code": None}
        return {"reason": status.reason, "code": status.code}
