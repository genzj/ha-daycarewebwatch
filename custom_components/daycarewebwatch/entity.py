"""Base entity for DaycareWebWatch."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import Camera
from .const import DOMAIN
from .coordinator import DaycareWebWatchData, ViewingStatusCoordinator


class DaycareWebWatchEntity(CoordinatorEntity[ViewingStatusCoordinator]):
    """An entity tied to one DaycareWebWatch camera device."""

    _attr_has_entity_name = True

    def __init__(self, data: DaycareWebWatchData, camera: Camera, key: str) -> None:
        """Initialize."""
        super().__init__(data.status)
        self.runtime = data
        self.camera_id = camera.id
        entry = data.status.config_entry
        self._attr_unique_id = f"{entry.unique_id}_{camera.id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, str(camera.id))},
            name=camera.name,
            manufacturer="DaycareWebWatch",
            model="Classroom camera",
            suggested_area=camera.daycare_name or None,
        )

    @property
    def camera(self) -> Camera | None:
        """Current camera data, or None if it disappeared."""
        return (self.runtime.cameras.data or {}).get(self.camera_id)

    @property
    def available(self) -> bool:
        """Available while the service is reachable and the camera exists.

        Closed daycare hours do not make the entity unavailable.
        """
        return (
            super().available
            and self.runtime.cameras.last_update_success
            and self.camera is not None
        )

    async def async_added_to_hass(self) -> None:
        """Also follow camera-list updates."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self.runtime.cameras.async_add_listener(self._handle_coordinator_update)
        )
