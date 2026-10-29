"""Camera platform for DaycareWebWatch.

Stream tokens can open new connections for about 5 minutes; a connection that
is already playing keeps working. So a token is needed whenever the HLS worker
(re)connects, and never while it plays. To keep requests to the service low:

- a URL issued less than TOKEN_REUSE_MAX_AGE ago (e.g. by a status check) is
  reused instead of requesting another token,
- a new token is requested only when a new viewer starts an idle stream or a
  running stream fails, and at most once per STREAM_MIN_REFRESH_INTERVAL,
- still images never open the camera: they are a placeholder, or the last
  frame captured from a stream someone was already watching.
"""

from __future__ import annotations

from datetime import datetime
import logging
from pathlib import Path
from typing import Any

from homeassistant.components.camera import Camera
from homeassistant.components.camera.const import CameraEntityFeature
from homeassistant.components.stream import Stream
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from .api import Camera as DwwCamera, DaycareWebWatchError, ViewingClosed
from .const import (
    CONF_USE_WEBRTC,
    DEFAULT_USE_WEBRTC,
    STREAM_CHECK_INTERVAL,
    STREAM_MIN_REFRESH_INTERVAL,
    TOKEN_REUSE_MAX_AGE,
)
from .coordinator import DaycareWebWatchConfigEntry, DaycareWebWatchData, ViewingStatus
from .entity import DaycareWebWatchEntity
from .helpers import async_add_new_cameras

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0

PLACEHOLDER_PATH = Path(__file__).parent / "placeholder.jpg"
_PLACEHOLDER: bytes | None = None


async def _async_placeholder(hass: HomeAssistant) -> bytes:
    global _PLACEHOLDER  # read-once module cache
    if _PLACEHOLDER is None:
        _PLACEHOLDER = await hass.async_add_executor_job(PLACEHOLDER_PATH.read_bytes)
    return _PLACEHOLDER


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
    # Stills must not start streams (each start costs a token request).
    _attr_use_stream_for_stills = False

    def __init__(self, data: DaycareWebWatchData, camera: DwwCamera) -> None:
        """Initialize."""
        DaycareWebWatchEntity.__init__(self, data, camera, "camera")
        Camera.__init__(self)
        self._was_open = data.status.is_open(camera.id)
        self._source_issued: datetime | None = None  # token time of self.stream's URL
        self._last_refresh: datetime | None = None  # last repair attempt
        self._watched: Stream | None = None  # stream whose callback we installed
        self._snapshot: bytes | None = None

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
        """Start the stream health check timer."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_track_time_interval(
                self.hass,
                self._async_check_stream,
                STREAM_CHECK_INTERVAL,
                name=f"{self.entity_id} stream check",
            )
        )
        self.async_on_remove(lambda: self.coordinator.streaming.discard(self.camera_id))

    # -- tokens ---------------------------------------------------------------

    async def _async_url(self, avoid: str | None = None) -> tuple[str, datetime] | None:
        """Return a usable stream URL, reusing a recent one when possible.

        `avoid` is a URL known not to work (e.g. the one a stream just failed with).
        Closed cameras are recorded in the status coordinator instead of raising.
        """
        recent = self.coordinator.recent_url(self.camera_id)
        if recent is not None and recent[0] != avoid:
            age = (dt_util.utcnow() - recent[1]).total_seconds()
            _LOGGER.debug("Camera %s: reusing stream URL issued %ds ago", self.camera_id, age)
            return recent
        _LOGGER.debug("Camera %s: requesting a new stream URL", self.camera_id)
        if (camera := self.camera) is None:
            return None
        try:
            url = await self.runtime.client.async_get_stream_url(camera)
        except ViewingClosed as err:
            self.coordinator.async_set_camera_status(
                self.camera_id, ViewingStatus(False, err.code, err.message)
            )
            return None
        except DaycareWebWatchError as err:
            _LOGGER.debug("Could not get stream URL for %s: %s", self.entity_id, err)
            return None
        return url, self.coordinator.remember_url(self.camera_id, url)

    async def stream_source(self) -> str | None:
        """Return a tokenized HLS URL, or None while closed."""
        if not self.is_on or (result := await self._async_url()) is None:
            return None
        url, self._source_issued = result
        return url

    def _source_is_stale(self) -> bool:
        return (
            self._source_issued is None
            or dt_util.utcnow() - self._source_issued >= TOKEN_REUSE_MAX_AGE
        )

    # -- stream lifecycle -----------------------------------------------------

    async def async_create_stream(self) -> Stream | None:
        """Give a new viewer of an idle stream a URL that can still connect."""
        stream = self.stream
        if stream is not None and not stream.outputs() and self._source_is_stale():
            _LOGGER.debug("Camera %s: idle stream has an expired URL, replacing", self.camera_id)
            await self._async_drop_stream()
        stream = await super().async_create_stream()
        if stream is not None and stream is not self._watched:
            self._watched = stream
            stream.set_update_callback(self._handle_stream_update)
        return stream

    @callback
    def _handle_stream_update(self) -> None:
        """React to the stream worker going up or down."""
        if (stream := self.stream) is None:
            return
        if stream.available and stream.outputs():
            self.coordinator.streaming.add(self.camera_id)
        else:
            self.coordinator.streaming.discard(self.camera_id)
        _LOGGER.debug("Camera %s: stream %s", self.camera_id, "up" if stream.available else "down")
        if not stream.available:
            self.hass.async_create_task(self._async_check_stream())
        self.async_write_ha_state()

    async def _async_drop_stream(self) -> None:
        """Stop and forget the stream so the next viewer gets a fresh one."""
        self.coordinator.streaming.discard(self.camera_id)
        if (stream := self.stream) is not None:
            self.stream = None
            self._watched = None
            self._source_issued = None
            await stream.stop()

    async def _async_check_stream(self, _now: datetime | None = None) -> None:
        """Keep the stream healthy with as few token requests as possible.

        - no viewers or camera closed: drop the stream,
        - healthy: do nothing (an open HLS session keeps working),
        - failed (worker retrying with an expired token): restart it with a
          usable URL, at most once per STREAM_MIN_REFRESH_INTERVAL.
        """
        if (stream := self.stream) is None:
            return
        if not self.is_on or not stream.outputs():
            _LOGGER.debug(
                "Camera %s: closing stream (%s)",
                self.camera_id,
                "camera off" if not self.is_on else "no viewers",
            )
            await self._async_drop_stream()
            return
        if stream.available:
            return
        now = dt_util.utcnow()
        if self._last_refresh is not None and now - self._last_refresh < (
            STREAM_MIN_REFRESH_INTERVAL
        ):
            _LOGGER.debug("Camera %s: stream failed, waiting before retrying", self.camera_id)
            return
        _LOGGER.debug("Camera %s: stream failed, restarting it", self.camera_id)
        self._last_refresh = now
        if (result := await self._async_url(avoid=str(stream.source))) is None:
            if not self.is_on:
                await self._async_drop_stream()
            return
        url, self._source_issued = result
        stream.update_source(url)

    # -- WebRTC -----------------------------------------------------------------

    async def async_refresh_providers(self, *, write_state: bool = True) -> None:
        """Only offer WebRTC (go2rtc) when the user opted in.

        Without a WebRTC provider the frontend uses HA's HLS player directly.
        Skipping provider selection also avoids its stream_source() call.
        """
        options = self.coordinator.config_entry.options
        if options.get(CONF_USE_WEBRTC, DEFAULT_USE_WEBRTC):
            await super().async_refresh_providers(write_state=write_state)

        # -- stills -----------------------------------------------------------------

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return a still without contacting the service.

        While someone is watching, grab the current frame from that stream;
        otherwise return the last captured frame, or a placeholder.
        """
        stream = self.stream
        if (
            stream is not None
            and stream.available
            and stream.outputs()
            and (image := await stream.async_get_image(width, height))
        ):
            self._snapshot = image
        return self._snapshot or await _async_placeholder(self.hass)

    @callback
    def _handle_coordinator_update(self) -> None:
        """Stop an active stream when viewing closes."""
        is_open = self.is_on
        if self._was_open and not is_open and self.stream is not None:
            self.hass.async_create_task(self._async_drop_stream())
        self._was_open = is_open
        super()._handle_coordinator_update()
