"""Data update coordinators for DaycareWebWatch."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    DENIED_TODAY_CODES,
    Camera,
    CannotConnect,
    DaycareWebWatchClient,
    ErrorCode,
    InvalidAuth,
    UnsupportedApiVersion,
    ViewingClosed,
)
from .const import (
    CAMERA_LIST_INTERVAL,
    CONF_WINDOW_DAYS,
    CONF_WINDOW_END,
    CONF_WINDOW_START,
    DEFAULT_WINDOW_DAYS,
    DEFAULT_WINDOW_END,
    DEFAULT_WINDOW_START,
    DOMAIN,
    STATUS_CLOSED_IN_WINDOW_INTERVAL,
    STATUS_CLOSED_MAX_INTERVAL,
    STATUS_ERROR_MAX_INTERVAL,
    STATUS_ERROR_MIN_INTERVAL,
    STATUS_OPEN_INTERVAL,
    TOKEN_REUSE_MAX_AGE,
)
from .schedule import ViewingWindow

_LOGGER = logging.getLogger(__name__)

# Delay between per-camera status probes, to avoid bursts.
PROBE_SPACING = 1.0


@dataclass(frozen=True, slots=True)
class ViewingStatus:
    """Whether a camera can be viewed right now."""

    is_open: bool
    code: int | None = None
    message: str | None = None

    @property
    def reason(self) -> str | None:
        """The server's explanation for a closed camera."""
        if self.is_open:
            return None
        return self.message or "Not available"


OPEN = ViewingStatus(is_open=True)


@dataclass(slots=True)
class DaycareWebWatchData:
    """Runtime data stored on the config entry."""

    client: DaycareWebWatchClient
    cameras: CameraListCoordinator
    status: ViewingStatusCoordinator


type DaycareWebWatchConfigEntry = ConfigEntry[DaycareWebWatchData]


class CameraListCoordinator(DataUpdateCoordinator[dict[int, Camera]]):
    """Fetches the cameras listed for the account."""

    config_entry: DaycareWebWatchConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: DaycareWebWatchConfigEntry,
        client: DaycareWebWatchClient,
    ) -> None:
        """Initialize."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} cameras",
            update_interval=CAMERA_LIST_INTERVAL,
        )
        self.client = client

    async def _async_update_data(self) -> dict[int, Camera]:
        try:
            cameras = await self.client.async_get_cameras()
        except InvalidAuth as err:
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN, translation_key="invalid_auth"
            ) from err
        except UnsupportedApiVersion as err:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                "unsupported_api_version",
                is_fixable=False,
                severity=ir.IssueSeverity.ERROR,
                translation_key="unsupported_api_version",
            )
            raise UpdateFailed(
                translation_domain=DOMAIN, translation_key="unsupported_api_version"
            ) from err
        except CannotConnect as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="cannot_connect",
                translation_placeholders={"error": str(err)},
            ) from err
        ir.async_delete_issue(self.hass, DOMAIN, "unsupported_api_version")
        return {camera.id: camera for camera in cameras}


class ViewingStatusCoordinator(DataUpdateCoordinator[dict[int, ViewingStatus]]):
    """Tracks whether each camera is viewable, polling slowly when closed.

    The only reliable way to learn whether a camera can be watched is to open
    it (viewcamera). To keep those requests rare:
    - open cameras are re-checked hourly,
    - cameras refused "for today" are not asked again until the next opening,
    - while everything is closed, checks follow the configured viewing window.
    """

    config_entry: DaycareWebWatchConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: DaycareWebWatchConfigEntry,
        client: DaycareWebWatchClient,
        cameras: CameraListCoordinator,
    ) -> None:
        """Initialize."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} viewing status",
            update_interval=STATUS_OPEN_INTERVAL,
            always_update=False,
        )
        self.client = client
        self.cameras = cameras
        self.window = ViewingWindow.from_options(
            entry.options.get(CONF_WINDOW_START, DEFAULT_WINDOW_START),
            entry.options.get(CONF_WINDOW_END, DEFAULT_WINDOW_END),
            entry.options.get(CONF_WINDOW_DAYS, DEFAULT_WINDOW_DAYS),
        )
        self._error_interval: timedelta | None = None
        # Cameras that must not be opened again before the given time.
        self._hold_until: dict[int, datetime] = {}
        # Most recent stream URL per camera and when its token was issued.
        self._urls: dict[int, tuple[str, datetime]] = {}
        # Cameras with a healthy, watched stream (set by the camera entities).
        self.streaming: set[int] = set()

    def remember_url(self, camera_id: int, url: str) -> datetime:
        """Store a freshly issued stream URL; return its issue time."""
        issued = dt_util.utcnow()
        self._urls[camera_id] = (url, issued)
        return issued

    def recent_url(self, camera_id: int) -> tuple[str, datetime] | None:
        """Return a stream URL whose token can still open connections."""
        if (entry := self._urls.get(camera_id)) is None:
            return None
        if dt_util.utcnow() - entry[1] >= TOKEN_REUSE_MAX_AGE:
            del self._urls[camera_id]
            return None
        return entry

    def is_open(self, camera_id: int) -> bool:
        """Return True if the camera is currently viewable."""
        if self.data is None:
            return False
        return self.data.get(camera_id, OPEN).is_open

    def status(self, camera_id: int) -> ViewingStatus | None:
        """Return the status for a camera."""
        return None if self.data is None else self.data.get(camera_id)

    def _next_day_opening(self, now: datetime) -> datetime:
        midnight = dt_util.start_of_local_day(now) + timedelta(days=1)
        return self.window.next_start(midnight - timedelta(microseconds=1)) or midnight

    def _remember(self, camera_id: int, status: ViewingStatus, now: datetime) -> None:
        if not status.is_open:
            self._urls.pop(camera_id, None)
        if status.is_open:
            self._hold_until.pop(camera_id, None)
        elif status.code in DENIED_TODAY_CODES:
            self._hold_until[camera_id] = self._next_day_opening(now)

    def _interval_for(self, data: dict[int, ViewingStatus], now: datetime) -> timedelta:
        if any(status.is_open for status in data.values()):
            return STATUS_OPEN_INTERVAL
        interval = self.window.closed_interval(
            now, STATUS_CLOSED_IN_WINDOW_INTERVAL, STATUS_CLOSED_MAX_INTERVAL
        )
        if data and all(cam_id in self._hold_until for cam_id in data):
            # Nothing to probe until the earliest hold expires.
            earliest = min(self._hold_until[cam_id] for cam_id in data)
            interval = max(interval, min(earliest - now, STATUS_CLOSED_MAX_INTERVAL))
        return interval

    def _log_transitions(self, data: dict[int, ViewingStatus]) -> None:
        previous = self.data or {}
        cameras = self.cameras.data or {}
        for camera_id, status in data.items():
            old = previous.get(camera_id)
            if old is not None and old.is_open == status.is_open:
                continue
            if old is None and status.is_open:
                continue
            name = cameras[camera_id].name if camera_id in cameras else camera_id
            if status.is_open:
                _LOGGER.info("Camera %s is now viewable", name)
            else:
                _LOGGER.info("Camera %s is not viewable: %s", name, status.reason)

    def _skip_reason(
        self,
        camera: Camera,
        previous: dict[int, ViewingStatus],
        closed_daycares: dict[str, ViewingStatus],
        now: datetime,
    ) -> tuple[str, ViewingStatus] | None:
        """Return why (and with which status) a camera needs no request now."""
        held = self._hold_until.get(camera.id)
        if held is not None and now < held and camera.id in previous:
            retry = dt_util.as_local(held).isoformat(timespec="minutes")
            return f"refused for today, retry at {retry}", previous[camera.id]
        if camera.daycare_id in closed_daycares:
            # The whole daycare is closed; no need to open its other cameras.
            return "daycare closed", closed_daycares[camera.daycare_id]
        if camera.id in self.streaming:
            return "being watched", OPEN
        if self.recent_url(camera.id) is not None:
            return "opened less than 4 minutes ago", OPEN
        return None

    async def _async_update_data(self) -> dict[int, ViewingStatus]:
        cameras = self.cameras.data or {}
        previous = self.data or {}
        now = dt_util.now()
        data: dict[int, ViewingStatus] = {}
        closed_daycares: dict[str, ViewingStatus] = {}
        first = True
        try:
            for camera in sorted(cameras.values(), key=lambda c: (c.daycare_id, c.id)):
                if (
                    skipped := self._skip_reason(camera, previous, closed_daycares, now)
                ) is not None:
                    reason, status = skipped
                    _LOGGER.debug("Status check camera %s skipped: %s", camera.id, reason)
                    data[camera.id] = status
                    continue
                if not first:
                    await asyncio.sleep(PROBE_SPACING)
                first = False
                try:
                    url = await self.client.async_get_stream_url(camera)
                except ViewingClosed as err:
                    status = ViewingStatus(False, err.code, err.message)
                    if err.code == ErrorCode.DAYCARE_CURRENTLY_CLOSED:
                        closed_daycares[camera.daycare_id] = status
                else:
                    # Keep the URL so a viewer arriving soon doesn't need another token.
                    self.remember_url(camera.id, url)
                    status = OPEN
                data[camera.id] = status
                self._remember(camera.id, status, now)
        except InvalidAuth as err:
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN, translation_key="invalid_auth"
            ) from err
        except (CannotConnect, UnsupportedApiVersion) as err:
            self._error_interval = min(
                (self._error_interval or STATUS_ERROR_MIN_INTERVAL / 2) * 2,
                STATUS_ERROR_MAX_INTERVAL,
            )
            self.update_interval = self._error_interval
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="cannot_connect",
                translation_placeholders={"error": str(err)},
            ) from err

        self._error_interval = None
        self._log_transitions(data)
        self.update_interval = self._interval_for(data, now)
        _LOGGER.debug(
            "Viewing status: %s; next check in %s",
            ", ".join(f"{cid}={'open' if st.is_open else st.code}" for cid, st in data.items())
            or "no cameras",
            self.update_interval,
        )
        return data

    def async_set_camera_status(self, camera_id: int, status: ViewingStatus) -> None:
        """Record a status learned outside a poll (e.g. from a stream request)."""
        current = dict(self.data or {})
        if current.get(camera_id) == status:
            return
        now = dt_util.now()
        current[camera_id] = status
        self._remember(camera_id, status, now)
        self._log_transitions(current)
        self.update_interval = self._interval_for(current, now)
        self.async_set_updated_data(current)
