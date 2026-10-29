"""Data update coordinators for DaycareWebWatch."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    Camera,
    CameraGone,
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
)
from .schedule import ViewingWindow

_LOGGER = logging.getLogger(__name__)

# Delay between per-camera status probes, to avoid bursts.
PROBE_SPACING = 1.0

REASONS: dict[int, str] = {
    ErrorCode.DAYCARE_CURRENTLY_CLOSED: "daycare_closed",
    ErrorCode.ROOM_NOT_ACTIVE: "room_inactive",
    ErrorCode.CAMERA_NOT_ACTIVE: "camera_off",
    ErrorCode.ACCESS_DENIED: "access_denied",
    ErrorCode.DAYCARE_ACCESS_DENIED: "access_denied",
    ErrorCode.CAMERA_ACCESS_DENIED: "access_denied",
    ErrorCode.DAYCARE_NOT_ACTIVE: "daycare_closed",
    ErrorCode.CAMERA_NOT_AVAILABLE: "camera_off",
    ErrorCode.CHILD_WAS_ABSENT: "child_absent",
    ErrorCode.CAMERA_NOT_FOUND: "camera_removed",
    ErrorCode.ROOM_NOT_FOUND: "camera_removed",
}


@dataclass(frozen=True, slots=True)
class ViewingStatus:
    """Whether a camera can be viewed right now."""

    is_open: bool
    code: int | None = None

    @property
    def reason(self) -> str | None:
        """Translation-friendly reason for a closed camera."""
        if self.is_open:
            return None
        return REASONS.get(self.code or 0, "unavailable")


OPEN = ViewingStatus(is_open=True)


@dataclass(slots=True)
class DaycareWebWatchData:
    """Runtime data stored on the config entry."""

    client: DaycareWebWatchClient
    cameras: CameraListCoordinator
    status: ViewingStatusCoordinator


type DaycareWebWatchConfigEntry = ConfigEntry[DaycareWebWatchData]


class CameraListCoordinator(DataUpdateCoordinator[dict[int, Camera]]):
    """Fetches the list of accessible cameras."""

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
        except (CannotConnect, ViewingClosed, CameraGone) as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="cannot_connect",
                translation_placeholders={"error": str(err)},
            ) from err
        ir.async_delete_issue(self.hass, DOMAIN, "unsupported_api_version")
        return {camera.id: camera for camera in cameras}


class ViewingStatusCoordinator(DataUpdateCoordinator[dict[int, ViewingStatus]]):
    """Tracks whether each camera is viewable, polling slowly when closed."""

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

    def is_open(self, camera_id: int) -> bool:
        """Return True if the camera is currently viewable."""
        if self.data is None:
            return False
        return self.data.get(camera_id, OPEN).is_open

    def status(self, camera_id: int) -> ViewingStatus | None:
        """Return the status for a camera."""
        return None if self.data is None else self.data.get(camera_id)

    def _interval_for(self, data: dict[int, ViewingStatus]) -> timedelta:
        if any(status.is_open for status in data.values()):
            return STATUS_OPEN_INTERVAL
        return self.window.closed_interval(
            dt_util.now(), STATUS_CLOSED_IN_WINDOW_INTERVAL, STATUS_CLOSED_MAX_INTERVAL
        )

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
                _LOGGER.info("Camera %s is not viewable (%s)", name, status.reason)

    async def _async_update_data(self) -> dict[int, ViewingStatus]:
        cameras = self.cameras.data or {}
        data: dict[int, ViewingStatus] = {}
        closed_daycares: set[str] = set()
        refresh_list = False
        first = True
        try:
            for camera in sorted(cameras.values(), key=lambda c: (c.daycare_id, c.id)):
                if camera.daycare_id in closed_daycares:
                    # The whole daycare is closed; no need to probe its other cameras.
                    data[camera.id] = ViewingStatus(False, ErrorCode.DAYCARE_CURRENTLY_CLOSED)
                    continue
                if not first:
                    await asyncio.sleep(PROBE_SPACING)
                first = False
                try:
                    await self.client.async_check_permission(camera.id)
                except ViewingClosed as err:
                    data[camera.id] = ViewingStatus(False, err.code)
                    if err.code == ErrorCode.DAYCARE_CURRENTLY_CLOSED:
                        closed_daycares.add(camera.daycare_id)
                except CameraGone as err:
                    data[camera.id] = ViewingStatus(False, err.code)
                    refresh_list = True
                else:
                    data[camera.id] = OPEN
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
        if refresh_list:
            self.hass.async_create_task(self.cameras.async_request_refresh())
        self._log_transitions(data)
        self.update_interval = self._interval_for(data)
        return data

    def async_set_camera_status(self, camera_id: int, status: ViewingStatus) -> None:
        """Record a status learned outside a poll (e.g. from a stream request)."""
        current = dict(self.data or {})
        if current.get(camera_id) == status:
            return
        current[camera_id] = status
        self._log_transitions(current)
        self.update_interval = self._interval_for(current)
        self.async_set_updated_data(current)
