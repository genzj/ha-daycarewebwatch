"""Async client for the DaycareWebWatch mobile API.

This module has no Home Assistant imports so it can be split into a standalone
library later. It mirrors the request flow of the official Android app.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import IntEnum
import hashlib
import json
import logging
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

BASE_URL = "https://www.daycarewebwatch.com"
IP_URL = f"{BASE_URL}/ip.php"
API_SALT = "AJmvPzcDuzyPBfQ6rJSR6mJx"
SUPPORTED_API_VERSION = 1.0
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)


class ErrorCode(IntEnum):
    """Server error codes (from the app's error enum)."""

    SUCCESS = 0
    ACCESS_DENIED = 101
    INVALID_INSTANCE_ID = 102
    INVALID_SESSION_ID = 103
    INVALID_SECURITY_KEY = 104
    INVALID_SECURITY_TOKEN = 105
    IP_CHANGED = 106
    ERROR_IN_PROCESSING_REQUEST = 107
    INVALID_EMAIL_ADDRESS_PASSWORD = 150
    EMAIL_NOT_VERIFIED = 154
    ACCOUNT_NOT_FOUND = 200
    ACCOUNT_NOT_ACTIVE = 201
    DAYCARE_ACCESS_DENIED = 207
    DAYCARE_NOT_ACTIVE = 251
    DAYCARE_NOT_SUBSCRIBED = 253
    DAYCARE_CURRENTLY_CLOSED = 254
    DAYCARE_HAS_NO_CAMERAS = 255
    ROOM_NOT_FOUND = 300
    ROOM_NOT_ACTIVE = 301
    CAMERA_NOT_FOUND = 350
    CAMERA_NOT_ACTIVE = 351
    CAMERA_NOT_AVAILABLE = 352
    CAMERA_ACCESS_DENIED = 353  # also "access denied today"
    CHILD_WAS_ABSENT = 403


RELOGIN_CODES = frozenset(
    {
        ErrorCode.INVALID_INSTANCE_ID,
        ErrorCode.INVALID_SESSION_ID,
        ErrorCode.INVALID_SECURITY_KEY,
        ErrorCode.INVALID_SECURITY_TOKEN,
        ErrorCode.IP_CHANGED,
    }
)
AUTH_CODES = frozenset(
    {
        ErrorCode.INVALID_EMAIL_ADDRESS_PASSWORD,
        ErrorCode.EMAIL_NOT_VERIFIED,
        ErrorCode.ACCOUNT_NOT_FOUND,
        ErrorCode.ACCOUNT_NOT_ACTIVE,
    }
)
CLOSED_CODES = frozenset(
    {
        ErrorCode.ACCESS_DENIED,
        ErrorCode.DAYCARE_ACCESS_DENIED,
        ErrorCode.DAYCARE_NOT_ACTIVE,
        ErrorCode.DAYCARE_CURRENTLY_CLOSED,
        ErrorCode.ROOM_NOT_ACTIVE,
        ErrorCode.CAMERA_NOT_ACTIVE,
        ErrorCode.CAMERA_NOT_AVAILABLE,
        ErrorCode.CAMERA_ACCESS_DENIED,
        ErrorCode.CHILD_WAS_ABSENT,
    }
)
GONE_CODES = frozenset({ErrorCode.CAMERA_NOT_FOUND, ErrorCode.ROOM_NOT_FOUND})


class DaycareWebWatchError(Exception):
    """Base error."""

    def __init__(self, message: str = "", code: int | None = None) -> None:
        """Initialize the error."""
        super().__init__(f"{message or 'error'} (code {code})" if code is not None else message)
        self.code = code


class CannotConnect(DaycareWebWatchError):
    """Network failure or unexpected server response."""


class InvalidAuth(DaycareWebWatchError):
    """Credentials were rejected."""


class UnsupportedApiVersion(DaycareWebWatchError):
    """Server API is newer than this client understands."""


class ViewingClosed(DaycareWebWatchError):
    """Viewing is not possible right now (daycare closed, camera off, ...).

    This is an expected, recurring state and not a failure.
    """


class CameraGone(DaycareWebWatchError):
    """The camera or its room no longer exists."""


@dataclass(slots=True)
class Camera:
    """A camera the account can access."""

    id: int
    name: str
    server: int
    url: str
    muted: bool
    daycare_id: str
    daycare_name: str
    children: list[str] = field(default_factory=list)


def build_stream_url(camera: Camera, token: str) -> str:
    """Build the HLS URL exactly like the app does."""
    if camera.url:
        return f"{camera.url.strip()}?{token}"
    return (
        f"http://wowza{camera.server}.daycarewebwatch.com:443"
        f"/dcww/{camera.id}.stream/playlist.m3u8?{token}"
    )


def parse_cameras(data: dict[str, Any]) -> list[Camera]:
    """Parse the parentchildren response into unique cameras."""
    cameras: dict[int, Camera] = {}
    for child in data.get("Children") or []:
        child_name = " ".join(
            part for part in (child.get("FirstName"), child.get("LastName")) if part
        )
        for raw in child.get("Cameras") or []:
            cam_id = int(raw["CameraId"])
            if (camera := cameras.get(cam_id)) is None:
                camera = cameras[cam_id] = Camera(
                    id=cam_id,
                    name=str(raw.get("CameraName") or f"Camera {cam_id}"),
                    server=int(raw.get("Video", 1) or 0),
                    url=str(raw.get("Url") or ""),
                    muted=int(raw.get("IsMuted", 0) or 0) == 1,
                    daycare_id=str(child.get("DaycareId", "")),
                    daycare_name=str(child.get("DaycareName", "")),
                )
            if child_name and child_name not in camera.children:
                camera.children.append(child_name)
    return list(cameras.values())


class DaycareWebWatchClient:
    """DaycareWebWatch API client."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        email: str,
        password: str,
        instance_id: str,
    ) -> None:
        """Initialize the client."""
        self._session = session
        self._email = email
        self._password = password
        self._instance_id = instance_id
        self._session_id: str | None = None
        self._public_ip: str | None = None
        self._login_lock = asyncio.Lock()

    @property
    def device_info(self) -> str:
        """DeviceInfo JSON in the shape the Android app sends."""
        return json.dumps(
            {
                "AppId": 1,
                "DeviceType": 2,
                "DeviceUUID": self._instance_id,
                "Manufacturer": "HomeAssistant",
                "Model": "HomeAssistant",
                "OS": "Android",
                "Version": "14",
                "API": "34",
                "ScreenSize": "1080x1920",
                "Locale": "en_US",
                "Language": "en",
                "PushGranted": 0,
                "PushDeviceToken": "",
            },
            separators=(",", ":"),
        )

    async def _get_public_ip(self) -> str:
        if self._public_ip is None:
            try:
                async with self._session.get(IP_URL, timeout=REQUEST_TIMEOUT) as resp:
                    resp.raise_for_status()
                    self._public_ip = (await resp.text()).strip()
            except (aiohttp.ClientError, TimeoutError) as err:
                raise CannotConnect(f"Unable to get public IP: {err}") from err
        return self._public_ip

    async def _params(self, with_session: bool) -> dict[str, str]:
        ip = await self._get_public_ip()
        params = {
            "AppInstanceId": self._instance_id,
            "ApiKey": hashlib.md5(  # noqa: S324 - protocol-defined, not security
                f"{self._instance_id}{ip}{API_SALT}".encode()
            ).hexdigest(),
        }
        if with_session:
            params["SessionId"] = self._session_id or ""
        return params

    async def _raw_post(
        self, path: str, data: dict[str, str], with_session: bool
    ) -> dict[str, Any]:
        payload = await self._params(with_session) | data
        try:
            async with self._session.post(
                f"{BASE_URL}/mobile/{path}", data=payload, timeout=REQUEST_TIMEOUT
            ) as resp:
                resp.raise_for_status()
                result = json.loads(await resp.text())
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise CannotConnect(f"Request to {path} failed: {err}") from err
        if not isinstance(result, dict):
            raise CannotConnect(f"Unexpected response from {path}")
        return result

    @staticmethod
    def _error_code(result: dict[str, Any]) -> int | None:
        """Return None on success, otherwise the error code."""
        if int(result.get("Status", 0) or 0) == 1:
            return None
        try:
            return int(result.get("Error", -1))
        except (TypeError, ValueError):
            return -1

    @staticmethod
    def _raise_for(code: int, result: dict[str, Any]) -> None:
        message = str(result.get("Message") or "")
        if code in AUTH_CODES:
            raise InvalidAuth(message, code)
        if code in CLOSED_CODES:
            raise ViewingClosed(message, code)
        if code in GONE_CODES:
            raise CameraGone(message, code)
        raise CannotConnect(message, code)

    async def async_login(self) -> None:
        """Log in and store the session token."""
        async with self._login_lock:
            await self._login()

    async def _login(self) -> None:
        result = await self._raw_post(
            "login",
            {
                "DeviceInfo": self.device_info,
                "Email": self._email,
                "Password": self._password,
            },
            with_session=False,
        )
        if (code := self._error_code(result)) is not None:
            if code in RELOGIN_CODES:
                # Instance / IP mismatch on login itself: refresh IP and give up.
                self._public_ip = None
            self._raise_for(code, result)
        try:
            api_version = float(result.get("APIVersion", SUPPORTED_API_VERSION))
        except (TypeError, ValueError):
            api_version = SUPPORTED_API_VERSION
        if api_version > SUPPORTED_API_VERSION:
            raise UnsupportedApiVersion(f"API version {api_version}")
        # "SessionId" authenticates later calls. ("Token" is the app's
        # remember-me access token and is not accepted as a session.)
        session_id = result.get("SessionId")
        if not session_id:
            raise CannotConnect("Login response has no SessionId")
        self._session_id = str(session_id)

    async def _post(self, path: str, data: dict[str, str]) -> dict[str, Any]:
        """Send an authenticated POST, logging in again once on session errors."""
        if self._session_id is None:
            await self.async_login()
        session_id = self._session_id
        result = await self._raw_post(path, data, with_session=True)
        code = self._error_code(result)
        if code in RELOGIN_CODES:
            _LOGGER.debug("Session rejected by %s (code %s), logging in again", path, code)
            async with self._login_lock:
                # Another call may already have refreshed the session.
                if self._session_id == session_id:
                    if code == ErrorCode.IP_CHANGED:
                        self._public_ip = None
                    await self._login()
            result = await self._raw_post(path, data, with_session=True)
            code = self._error_code(result)
        if code is not None:
            self._raise_for(code, result)
        return result

    async def async_get_cameras(self) -> list[Camera]:
        """Return all cameras accessible by the account."""
        return parse_cameras(await self._post("parentchildren", {}))

    async def async_check_permission(self, camera_id: int) -> None:
        """Raise ViewingClosed/CameraGone if the camera can't be viewed now."""
        await self._post("checkcamerapermission", {"CameraId": str(camera_id)})

    async def async_get_stream_url(self, camera: Camera) -> str:
        """Request a fresh stream token and return the playable URL."""
        result = await self._post("viewcamera", {"CameraId": str(camera.id), "LowBandwidth": "0"})
        token = result.get("VideoToken")
        if not token:
            raise CannotConnect("viewcamera response has no VideoToken")
        return build_stream_url(camera, str(token))
