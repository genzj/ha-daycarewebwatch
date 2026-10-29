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
    """Error codes with a known meaning.

    The live server does not use the numbering found in the app (it returned
    e.g. 1 for "Access Denied!" where the app expects 101), so only codes that
    were observed from the server get special treatment. Every other refusal
    is handled generically using the server's message.
    """

    # Observed from the server.
    ACCESS_DENIED = 1
    CAMERA_DENIED_TODAY = 23  # "You do not have access to this camera today"
    CAMERA_NOT_CURRENTLY_AVAILABLE = 31
    # From the app; kept as hints in case the server uses them.
    INVALID_INSTANCE_ID = 102
    INVALID_SESSION_ID = 103
    INVALID_SECURITY_KEY = 104
    INVALID_SECURITY_TOKEN = 105
    IP_CHANGED = 106
    DAYCARE_CURRENTLY_CLOSED = 254


# Codes that certainly mean "log in again".
RELOGIN_CODES = frozenset(
    {
        ErrorCode.INVALID_INSTANCE_ID,
        ErrorCode.INVALID_SESSION_ID,
        ErrorCode.INVALID_SECURITY_KEY,
        ErrorCode.INVALID_SECURITY_TOKEN,
        ErrorCode.IP_CHANGED,
    }
)
# Camera refusals that are certainly not caused by a stale session.
KNOWN_REFUSAL_CODES = frozenset(
    {
        ErrorCode.CAMERA_DENIED_TODAY,
        ErrorCode.CAMERA_NOT_CURRENTLY_AVAILABLE,
        ErrorCode.DAYCARE_CURRENTLY_CLOSED,
    }
)
# Refusals that will not change before the next day.
DENIED_TODAY_CODES = frozenset({ErrorCode.CAMERA_DENIED_TODAY})


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
    """The server refused to open the camera right now.

    Daycare closed, camera off, no access today, ... This is an expected,
    recurring state and not a failure. `message` is the server's explanation.
    """

    def __init__(self, message: str = "", code: int | None = None) -> None:
        """Initialize the error."""
        super().__init__(message, code)
        self.message = message


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
        """Return None on success, otherwise the error code (-1 if missing)."""
        if int(result.get("Status", 0) or 0) == 1:
            return None
        try:
            return int(result.get("Error", -1))
        except (TypeError, ValueError):
            return -1

    @staticmethod
    def _message(result: dict[str, Any]) -> str:
        return str(result.get("Message") or "")

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
            # Error numbers are unreliable; a refused login is an account problem.
            # Refresh the IP in case the ApiKey was computed from a stale one.
            self._public_ip = None
            raise InvalidAuth(self._message(result), code)
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

    async def _relogin(self, stale_session: str | None) -> None:
        """Log in again unless another call already did."""
        async with self._login_lock:
            if self._session_id == stale_session:
                self._public_ip = None
                await self._login()

    async def _post(
        self, path: str, data: dict[str, str], known_refusals: frozenset[int] = frozenset()
    ) -> tuple[dict[str, Any], int | None]:
        """Send an authenticated POST.

        Any refusal that might be caused by a stale session (or a changed
        public IP) is retried once after logging in again. Returns the result
        and its error code (None on success).
        """
        if self._session_id is None:
            await self.async_login()
        session_id = self._session_id
        result = await self._raw_post(path, data, with_session=True)
        code = self._error_code(result)
        if code is not None and code not in known_refusals:
            _LOGGER.debug("%s refused (code %s), logging in again and retrying", path, code)
            await self._relogin(session_id)
            result = await self._raw_post(path, data, with_session=True)
            code = self._error_code(result)
        return result, code

    async def async_get_cameras(self) -> list[Camera]:
        """Return all cameras listed for the account (not all may be viewable)."""
        result, code = await self._post("parentchildren", {})
        if code is not None:
            raise CannotConnect(self._message(result) or "Camera list refused", code)
        return parse_cameras(result)

    async def async_get_stream_url(self, camera: Camera) -> str:
        """Open the camera and return a playable, freshly tokenized URL.

        Raises ViewingClosed when the server refuses (closed, no access, ...).
        """
        result, code = await self._post(
            "viewcamera",
            {"CameraId": str(camera.id), "LowBandwidth": "0"},
            known_refusals=KNOWN_REFUSAL_CODES,
        )
        if code is not None:
            if code in RELOGIN_CODES:
                raise CannotConnect(self._message(result), code)
            raise ViewingClosed(self._message(result), code)
        token = result.get("VideoToken")
        if not token:
            raise CannotConnect("viewcamera response has no VideoToken")
        return build_stream_url(camera, str(token))
