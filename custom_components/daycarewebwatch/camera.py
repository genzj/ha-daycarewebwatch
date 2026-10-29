"""Camera platform for DaycareWebWatch."""

from __future__ import annotations

from datetime import datetime
import logging
from typing import Any

from homeassistant.components.camera import Camera
from homeassistant.components.camera.const import CameraEntityFeature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval

from .api import Camera as DwwCamera, CameraGone, DaycareWebWatchError, ViewingClosed
from .const import STREAM_REFRESH_INTERVAL
from .coordinator import DaycareWebWatchConfigEntry, DaycareWebWatchData, ViewingStatus
from .entity import DaycareWebWatchEntity
from .helpers import async_add_new_cameras

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DaycareWebWatchConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up camera entities."""
    async_add_new_cameras(
        entry,
        lambda cameras: async_add_entities(
            DaycareCamera(entry.runtime_data, camera) for camera in cameras
        ),
    )


class DaycareCamera(DaycareWebWatchEntity, Camera):
    """Live view of a DaycareWebWatch camera."""

    _attr_name = None
    _attr_supported_features = CameraEntityFeature.STREAM
    _attr_use_stream_for_stills = True

    def __init__(self, data: DaycareWebWatchData, camera: DwwCamera) -> None:
        """Initialize."""
        DaycareWebWatchEntity.__init__(self, data, camera, "camera")
        Camera.__init__(self)
        self._was_open = data.status.is_open(camera.id)

    @property
    def is_on(self) -> bool:
        """The camera is 'off' while the daycare is closed."""
        return self.coordinator.is_open(self.camera_id)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Children and daycare for this camera."""
        if (camera := self.camera) is None:
            return {}
        return {"children": camera.children, "daycare": camera.daycare_name}

    async def async_added_to_hass(self) -> None:
        """Start the stream URL refresh timer."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_track_time_interval(
                self.hass,
                self._async_refresh_stream,
                STREAM_REFRESH_INTERVAL,
                name=f"{self.entity_id} stream refresh",
            )
        )

    async def _async_fresh_url(self) -> str | None:
        """Get a new stream URL, recording closed state instead of failing."""
        if (camera := self.camera) is None:
            return None
        try:
            return await self.runtime.client.async_get_stream_url(camera)
        except (ViewingClosed, CameraGone) as err:
            self.coordinator.async_set_camera_status(self.camera_id, ViewingStatus(False, err.code))
        except DaycareWebWatchError as err:
            _LOGGER.debug("Could not get stream URL for %s: %s", self.entity_id, err)
        return None

    async def stream_source(self) -> str | None:
        """Return a freshly tokenized HLS URL, or None while closed."""
        if not self.is_on:
            return None
        return await self._async_fresh_url()

    async def _async_drop_stream(self) -> None:
        """Stop and forget the stream so the next viewer gets a new token."""
        if (stream := self.stream) is not None:
            self.stream = None
            await stream.stop()

    async def _async_refresh_stream(self, _now: datetime | None = None) -> None:
        """Keep the active stream's tokenized URL fresh.

        The new URL is swapped in without restarting a healthy stream; the
        stream worker picks it up on its next reconnect. A stream that has
        already failed is restarted immediately. Idle streams are dropped
        instead of being refreshed, so no tokens are requested while nobody
        is watching.
        """
        if (stream := self.stream) is None:
            return
        if not self.is_on or not stream.outputs():
            await self._async_drop_stream()
            return
        if (url := await self._async_fresh_url()) is None:
            if not self.is_on:
                await self._async_drop_stream()
            return
        if stream.available:
            stream.source = url
        else:
            stream.update_source(url)

    @callback
    def _handle_coordinator_update(self) -> None:
        """Stop an active stream when viewing closes."""
        is_open = self.is_on
        if self._was_open and not is_open and self.stream is not None:
            self.hass.async_create_task(self._async_drop_stream())
        self._was_open = is_open
        super()._handle_coordinator_update()
