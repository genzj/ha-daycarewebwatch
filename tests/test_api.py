"""Tests for the API client."""

from __future__ import annotations

from collections import defaultdict, deque
import hashlib
import json
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)

from custom_components.daycarewebwatch.api import (
    API_SALT,
    BASE_URL,
    IP_URL,
    Camera,
    CannotConnect,
    DaycareWebWatchClient,
    InvalidAuth,
    UnsupportedApiVersion,
    ViewingClosed,
    build_stream_url,
    parse_cameras,
)

from .conftest import INSTANCE_ID

LOGIN = f"{BASE_URL}/mobile/login"
CHILDREN = f"{BASE_URL}/mobile/parentchildren"
VIEW = f"{BASE_URL}/mobile/viewcamera"

CHILDREN_RESPONSE = {
    "Status": 1,
    "Children": [
        {
            "ChildId": 5,
            "FirstName": "Ann",
            "LastName": "Lee",
            "DaycareId": 10,
            "DaycareName": "Sunny",
            "Cameras": [
                {"CameraId": 1, "CameraName": "Toddler Room", "Video": 3},
                {"CameraId": 2, "CameraName": "Yard", "Url": "https://x/y.m3u8", "IsMuted": 1},
            ],
        },
        {
            "ChildId": 6,
            "FirstName": "Bo",
            "DaycareId": 10,
            "DaycareName": "Sunny",
            "Cameras": [{"CameraId": 1, "CameraName": "Toddler Room", "Video": 3}],
        },
    ],
}


class FakeServer:
    """Queue responses per URL on top of aioclient_mock."""

    def __init__(self, mocker: AiohttpClientMocker) -> None:
        self.mocker = mocker
        self.queues: dict[str, deque[dict[str, Any]]] = defaultdict(deque)
        self.bodies: dict[str, list[dict[str, str]]] = defaultdict(list)
        self.registered: set[tuple[str, str]] = set()

    def add(self, method: str, url: str, **response: Any) -> None:
        self.queues[url].append(response)
        if (method, url) in self.registered:
            return
        self.registered.add((method, url))

        async def responder(method: str, url_: Any, data: Any) -> AiohttpClientMockResponse:
            self.bodies[url].append(dict(data or {}))
            spec = self.queues[url].popleft()
            if "exc" in spec:
                raise spec["exc"]
            return AiohttpClientMockResponse(
                method,
                url_,
                status=spec.get("status", 200),
                text=spec.get("text"),
                json=spec.get("json"),
            )

        self.mocker.request(method, url, side_effect=responder)

    def get(self, url: str, **response: Any) -> None:
        self.add("GET", url, **response)

    def post(self, url: str, **response: Any) -> None:
        self.add("POST", url, **response)


@pytest.fixture
def m(aioclient_mock: AiohttpClientMocker) -> FakeServer:
    return FakeServer(aioclient_mock)


@pytest.fixture
async def client(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> DaycareWebWatchClient:
    return DaycareWebWatchClient(async_get_clientsession(hass), "a@b.c", "pw", INSTANCE_ID)


def test_parse_cameras_dedupes_and_merges_children() -> None:
    cameras = parse_cameras(CHILDREN_RESPONSE)
    assert [c.id for c in cameras] == [1, 2]
    assert cameras[0].children == ["Ann Lee", "Bo"]
    assert cameras[0].server == 3
    assert cameras[1].url == "https://x/y.m3u8"
    assert cameras[1].muted is True
    assert cameras[0].daycare_id == "10"


def test_build_stream_url() -> None:
    cam = Camera(7, "c", 2, "", False, "1", "d")
    assert build_stream_url(cam, "t=1") == (
        "http://wowza2.daycarewebwatch.com:443/dcww/7.stream/playlist.m3u8?t=1"
    )
    cam.url = " https://custom/p.m3u8 "
    assert build_stream_url(cam, "t=1") == "https://custom/p.m3u8?t=1"


async def test_login_and_list(client: DaycareWebWatchClient, m: FakeServer) -> None:
    if True:
        m.get(IP_URL, text="1.2.3.4\n")
        m.post(
            LOGIN, json={"Status": 1, "SessionId": "sess", "Token": "remember-me", "APIVersion": 1}
        )
        m.post(CHILDREN, json=CHILDREN_RESPONSE)
        cameras = await client.async_get_cameras()
        assert len(cameras) == 2
        body = m.bodies[LOGIN][0]
        assert body["Email"] == "a@b.c"
        assert body["Password"] == "pw"
        assert body["AppInstanceId"] == INSTANCE_ID
        expected = hashlib.md5(f"{INSTANCE_ID}1.2.3.4{API_SALT}".encode()).hexdigest()
        assert body["ApiKey"] == expected
        assert "SessionId" not in body
        assert json.loads(body["DeviceInfo"])["DeviceUUID"] == INSTANCE_ID
        assert m.bodies[CHILDREN][0]["SessionId"] == "sess"
        assert client.request_counts == {"ip.php": 1, "login": 1, "parentchildren": 1}


async def test_relogin_on_refusal(client: DaycareWebWatchClient, m: FakeServer) -> None:
    if True:
        m.get(IP_URL, text="1.2.3.4")
        m.post(LOGIN, json={"Status": 1, "SessionId": "s1"})
        m.post(CHILDREN, json={"Status": 0, "Error": 106})
        m.get(IP_URL, text="5.6.7.8")
        m.post(LOGIN, json={"Status": 1, "SessionId": "s2"})
        m.post(CHILDREN, json=CHILDREN_RESPONSE)
        assert len(await client.async_get_cameras()) == 2
        calls = m.bodies[CHILDREN]
        assert calls[1]["SessionId"] == "s2"
        expected = hashlib.md5(f"{INSTANCE_ID}5.6.7.8{API_SALT}".encode()).hexdigest()
        assert calls[1]["ApiKey"] == expected


@pytest.mark.parametrize("code", [150, 1, 106, "junk"])
async def test_any_login_refusal_is_invalid_auth(
    client: DaycareWebWatchClient, m: FakeServer, code: object
) -> None:
    m.get(IP_URL, text="1.2.3.4")
    m.post(LOGIN, json={"Status": 0, "Error": code, "Message": "bad"})
    with pytest.raises(InvalidAuth):
        await client.async_login()
    # The IP is re-fetched next time in case the ApiKey used a stale one.
    assert client._public_ip is None


async def test_unsupported_api_version(client: DaycareWebWatchClient, m: FakeServer) -> None:
    m.get(IP_URL, text="1.2.3.4")
    m.post(LOGIN, json={"Status": 1, "SessionId": "s", "APIVersion": 2.0})
    with pytest.raises(UnsupportedApiVersion):
        await client.async_login()


@pytest.mark.parametrize(
    "payload",
    [{"Status": 1}, {"Status": 1, "SessionId": "s", "APIVersion": "x"}],
)
async def test_login_odd_responses(
    client: DaycareWebWatchClient, m: FakeServer, payload: dict
) -> None:
    m.get(IP_URL, text="1.2.3.4")
    m.post(LOGIN, json=payload)
    if "SessionId" in payload:
        await client.async_login()
    else:
        with pytest.raises(CannotConnect):
            await client.async_login()


async def test_camera_list_refused_after_relogin(
    client: DaycareWebWatchClient, m: FakeServer
) -> None:
    m.get(IP_URL, text="1.2.3.4")
    m.post(LOGIN, json={"Status": 1, "SessionId": "s1"})
    m.post(CHILDREN, json={"Status": 0, "Error": 1, "Message": "Access Denied!"})
    m.get(IP_URL, text="1.2.3.4")
    m.post(LOGIN, json={"Status": 1, "SessionId": "s2"})
    m.post(CHILDREN, json={"Status": 0, "Error": 1, "Message": "Access Denied!"})
    with pytest.raises(CannotConnect, match="Access Denied"):
        await client.async_get_cameras()
    assert len(m.bodies[LOGIN]) == 2


async def test_stream_url(client: DaycareWebWatchClient, m: FakeServer) -> None:
    cam = Camera(1, "c", 1, "", False, "10", "d")
    m.get(IP_URL, text="1.2.3.4")
    m.post(LOGIN, json={"Status": 1, "SessionId": "s"})
    m.post(VIEW, json={"Status": 1, "VideoToken": "wowzatoken=abc"})
    url = await client.async_get_stream_url(cam)
    assert url.endswith("/dcww/1.stream/playlist.m3u8?wowzatoken=abc")
    assert m.bodies[VIEW][0]["LowBandwidth"] == "0"

    m.post(VIEW, json={"Status": 1})
    with pytest.raises(CannotConnect):
        await client.async_get_stream_url(cam)


@pytest.mark.parametrize("code", [23, 31, 254])
async def test_known_refusal_no_relogin(
    client: DaycareWebWatchClient, m: FakeServer, code: int
) -> None:
    cam = Camera(1, "c", 1, "", False, "10", "d")
    m.get(IP_URL, text="1.2.3.4")
    m.post(LOGIN, json={"Status": 1, "SessionId": "s"})
    m.post(VIEW, json={"Status": 0, "Error": code, "Message": "No access today"})
    with pytest.raises(ViewingClosed) as err:
        await client.async_get_stream_url(cam)
    assert err.value.code == code
    assert err.value.message == "No access today"
    assert len(m.bodies[LOGIN]) == 1
    assert len(m.bodies[VIEW]) == 1


async def test_unknown_refusal_retried_after_relogin(
    client: DaycareWebWatchClient, m: FakeServer
) -> None:
    cam = Camera(1, "c", 1, "", False, "10", "d")
    m.get(IP_URL, text="1.2.3.4")
    m.post(LOGIN, json={"Status": 1, "SessionId": "s1"})
    # Unknown code: could be a stale session or IP -> log in again, retry once.
    m.post(VIEW, json={"Status": 0, "Error": 1, "Message": "Access Denied!"})
    m.get(IP_URL, text="5.6.7.8")
    m.post(LOGIN, json={"Status": 1, "SessionId": "s2"})
    m.post(VIEW, json={"Status": 1, "VideoToken": "t"})
    assert (await client.async_get_stream_url(cam)).endswith("?t")
    assert m.bodies[VIEW][1]["SessionId"] == "s2"
    expected = hashlib.md5(f"{INSTANCE_ID}5.6.7.8{API_SALT}".encode()).hexdigest()
    assert m.bodies[VIEW][1]["ApiKey"] == expected

    # Still refused after re-login: the camera is simply not viewable.
    m.post(VIEW, json={"Status": 0, "Error": 1, "Message": "Access Denied!"})
    m.get(IP_URL, text="5.6.7.8")
    m.post(LOGIN, json={"Status": 1, "SessionId": "s3"})
    m.post(VIEW, json={"Status": 0, "Error": 1, "Message": "Access Denied!"})
    with pytest.raises(ViewingClosed, match="Access Denied"):
        await client.async_get_stream_url(cam)


async def test_session_error_persists(client: DaycareWebWatchClient, m: FakeServer) -> None:
    cam = Camera(1, "c", 1, "", False, "10", "d")
    m.get(IP_URL, text="1.2.3.4")
    m.post(LOGIN, json={"Status": 1, "SessionId": "s1"})
    m.post(VIEW, json={"Status": 0, "Error": 103})
    m.get(IP_URL, text="1.2.3.4")
    m.post(LOGIN, json={"Status": 1, "SessionId": "s2"})
    m.post(VIEW, json={"Status": 0, "Error": 103})
    with pytest.raises(CannotConnect):
        await client.async_get_stream_url(cam)


async def test_missing_error_code(client: DaycareWebWatchClient, m: FakeServer) -> None:
    cam = Camera(1, "c", 1, "", False, "10", "d")
    m.get(IP_URL, text="1.2.3.4")
    m.post(LOGIN, json={"Status": 1, "SessionId": "s1"})
    m.post(VIEW, json={"Status": 0, "Error": "junk"})
    m.get(IP_URL, text="1.2.3.4")
    m.post(LOGIN, json={"Status": 1, "SessionId": "s2"})
    m.post(VIEW, json={"Status": 0})
    with pytest.raises(ViewingClosed) as err:
        await client.async_get_stream_url(cam)
    assert err.value.code == -1


async def test_network_errors(client: DaycareWebWatchClient, m: FakeServer) -> None:
    if True:
        m.get(IP_URL, exc=aiohttp.ClientError("boom"))
        with pytest.raises(CannotConnect):
            await client.async_login()
        m.get(IP_URL, text="1.2.3.4")
        m.post(LOGIN, status=500)
        with pytest.raises(CannotConnect):
            await client.async_login()
        m.post(LOGIN, text="not json")
        with pytest.raises(CannotConnect):
            await client.async_login()
        m.post(LOGIN, text="[1, 2]")
        with pytest.raises(CannotConnect):
            await client.async_login()
