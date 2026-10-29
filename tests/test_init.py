"""Tests for setup, coordinators and entities."""

from __future__ import annotations

from datetime import timedelta
import logging
from unittest.mock import AsyncMock, MagicMock, patch

from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, issue_registry as ir
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.daycarewebwatch import async_remove_config_entry_device
from custom_components.daycarewebwatch.api import (
    Camera,
    CannotConnect,
    InvalidAuth,
    UnsupportedApiVersion,
    ViewingClosed,
)
from custom_components.daycarewebwatch.const import (
    DOMAIN,
    STATUS_CLOSED_IN_WINDOW_INTERVAL,
    STATUS_OPEN_INTERVAL,
)
from custom_components.daycarewebwatch.coordinator import (
    CameraListCoordinator,
    ViewingStatus,
    ViewingStatusCoordinator,
)
from custom_components.daycarewebwatch.diagnostics import async_get_config_entry_diagnostics

from .conftest import EMAIL, make_cameras

CAM1 = "camera.sunny_daycare_toddler_room"
CAM2 = "camera.sunny_daycare_playground"
BS1 = "binary_sensor.sunny_daycare_toddler_room_viewing_available"
BS2 = "binary_sensor.sunny_daycare_playground_viewing_available"

# HA test time zone is US/Pacific.
WEDNESDAY_10 = "2026-10-28 10:00:00-07:00"
WEDNESDAY_20 = "2026-10-28 20:00:00-07:00"
FRIDAY_19 = "2026-10-30 19:00:00-07:00"

CLOSED = ViewingClosed("The childcare centre is currently closed", 254)
DENIED_TODAY = ViewingClosed("You do not have access to this camera today", 23)


async def setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def tick(hass: HomeAssistant, freezer: FrozenDateTimeFactory, delta: timedelta) -> None:
    freezer.tick(delta)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


def per_camera(**outcomes: Exception | None):
    """Side effect for async_get_stream_url keyed by camera id (c1, c2)."""

    async def _side_effect(camera: Camera) -> str:
        outcome = outcomes.get(f"c{camera.id}")
        if outcome is not None:
            raise outcome
        return f"https://wowza/{camera.id}.m3u8?tok"

    return _side_effect


@pytest.fixture
def frozen(freezer: FrozenDateTimeFactory) -> FrozenDateTimeFactory:
    freezer.move_to(WEDNESDAY_10)
    return freezer


async def test_setup_and_unload(
    hass: HomeAssistant, mock_client: AsyncMock, config_entry: MockConfigEntry, frozen
) -> None:
    await setup(hass, config_entry)
    assert config_entry.state is ConfigEntryState.LOADED
    state = hass.states.get(CAM1)
    assert state.attributes["children"] == ["Ann Lee"]
    assert state.attributes["daycare"] == "Sunny Daycare"
    assert hass.states.get(BS1).state == STATE_ON
    assert hass.states.get(BS1).attributes == hass.states.get(BS1).attributes | {
        "reason": None,
        "code": None,
    }
    device = dr.async_get(hass).async_get_device_by_identifier((DOMAIN, "1"), config_entry.entry_id)
    assert device is not None
    assert device.name == "Toddler Room"

    assert await hass.config_entries.async_unload(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.NOT_LOADED


@pytest.mark.parametrize(
    ("exc", "state"),
    [
        (CannotConnect, ConfigEntryState.SETUP_RETRY),
        (InvalidAuth, ConfigEntryState.SETUP_ERROR),
        (UnsupportedApiVersion, ConfigEntryState.SETUP_RETRY),
    ],
)
async def test_setup_failures(
    hass: HomeAssistant,
    mock_client: AsyncMock,
    config_entry: MockConfigEntry,
    exc: type[Exception],
    state: ConfigEntryState,
) -> None:
    mock_client.async_get_cameras.side_effect = exc
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is state
    if exc is InvalidAuth:
        assert any(config_entry.async_get_active_flows(hass, {"reauth"}))
    if exc is UnsupportedApiVersion:
        assert ir.async_get(hass).async_get_issue(DOMAIN, "unsupported_api_version")


async def test_status_auth_failure_starts_reauth(
    hass: HomeAssistant, mock_client: AsyncMock, config_entry: MockConfigEntry, frozen
) -> None:
    mock_client.async_get_stream_url.side_effect = InvalidAuth
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.SETUP_ERROR


async def test_refused_cameras_load_fine(
    hass: HomeAssistant, mock_client: AsyncMock, config_entry: MockConfigEntry, frozen
) -> None:
    """Listed-but-not-accessible cameras are off, not errors."""
    mock_client.async_get_stream_url.side_effect = per_camera(c2=ViewingClosed("Access Denied!", 1))
    await setup(hass, config_entry)
    assert config_entry.state is ConfigEntryState.LOADED
    assert hass.states.get(BS1).state == STATE_ON
    assert hass.states.get(BS2).state == STATE_OFF
    assert hass.states.get(BS2).attributes["reason"] == "Access Denied!"
    assert hass.states.get(BS2).attributes["code"] == 1
    assert hass.states.get(CAM2).state != STATE_UNAVAILABLE
    assert config_entry.runtime_data.status.update_interval == STATUS_OPEN_INTERVAL


async def test_daycare_closed_at_night_and_reopens(
    hass: HomeAssistant,
    mock_client: AsyncMock,
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    freezer.move_to(WEDNESDAY_20)
    mock_client.async_get_stream_url.side_effect = CLOSED
    await setup(hass, config_entry)

    # Daycare-wide closure: only one camera is opened.
    assert mock_client.async_get_stream_url.await_count == 1
    status = config_entry.runtime_data.status
    assert status.last_update_success
    assert status.update_interval == timedelta(hours=3)
    assert hass.states.get(BS1).state == STATE_OFF
    assert hass.states.get(BS2).attributes["reason"] == CLOSED.message
    assert hass.states.get(CAM1).state != STATE_UNAVAILABLE
    assert "ERROR" not in caplog.text

    # The camera refuses to stream while closed, without calling the API.
    calls = mock_client.async_get_stream_url.await_count
    camera_entity = hass.data["camera"].get_entity(CAM1)
    assert await camera_entity.stream_source() is None
    assert mock_client.async_get_stream_url.await_count == calls

    # Next morning the daycare opens.
    mock_client.async_get_stream_url.side_effect = None
    freezer.move_to("2026-10-29 06:31:00-07:00")
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(BS1).state == STATE_ON
    assert status.update_interval == STATUS_OPEN_INTERVAL
    assert "is now viewable" in caplog.text


async def test_closed_in_window_polls_every_10_min(
    hass: HomeAssistant, mock_client: AsyncMock, config_entry: MockConfigEntry, frozen
) -> None:
    mock_client.async_get_stream_url.side_effect = ViewingClosed("Not available", 31)
    await setup(hass, config_entry)
    # Per-camera refusal: both cameras opened.
    assert mock_client.async_get_stream_url.await_count == 2
    assert config_entry.runtime_data.status.update_interval == STATUS_CLOSED_IN_WINDOW_INTERVAL


async def test_denied_today_not_retried_until_next_opening(
    hass: HomeAssistant, mock_client: AsyncMock, config_entry: MockConfigEntry, frozen
) -> None:
    opened: list[int] = []

    async def side_effect(camera: Camera) -> str:
        opened.append(camera.id)
        if camera.id == 2:
            raise DENIED_TODAY
        raise ViewingClosed("Not available", 31)

    mock_client.async_get_stream_url.side_effect = side_effect
    await setup(hass, config_entry)
    assert opened == [1, 2]

    # During the rest of the day only camera 1 is probed again.
    opened.clear()
    for _ in range(6):
        await tick(hass, frozen, STATUS_CLOSED_IN_WINDOW_INTERVAL)
    assert opened
    assert set(opened) == {1}
    assert hass.states.get(BS2).attributes["code"] == 23

    # Next day at opening time camera 2 is tried again.
    opened.clear()
    frozen.move_to("2026-10-29 06:31:00-07:00")
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert 2 in opened


async def test_all_denied_today_sleeps_until_next_day(
    hass: HomeAssistant, mock_client: AsyncMock, config_entry: MockConfigEntry, frozen
) -> None:
    mock_client.async_get_stream_url.side_effect = DENIED_TODAY
    await setup(hass, config_entry)
    # Next opening is Thursday 06:30, 20.5 h away -> capped at 3 h steps.
    assert config_entry.runtime_data.status.update_interval == timedelta(hours=3)
    calls = mock_client.async_get_stream_url.await_count
    for _ in range(4):
        await tick(hass, frozen, timedelta(hours=3))
    assert mock_client.async_get_stream_url.await_count == calls


async def test_weekend_interval(
    hass: HomeAssistant,
    mock_client: AsyncMock,
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(FRIDAY_19)
    mock_client.async_get_stream_url.side_effect = CLOSED
    await setup(hass, config_entry)
    status = config_entry.runtime_data.status
    calls = mock_client.async_get_stream_url.await_count
    # Over the whole weekend (60 h): at most one camera opened every 3 h.
    for _ in range(20):
        await tick(hass, freezer, timedelta(hours=3))
    assert mock_client.async_get_stream_url.await_count - calls <= 20
    assert status.update_interval <= timedelta(hours=3)


async def test_network_error_backoff_and_recovery(
    hass: HomeAssistant,
    mock_client: AsyncMock,
    config_entry: MockConfigEntry,
    frozen: FrozenDateTimeFactory,
) -> None:
    await setup(hass, config_entry)
    status = config_entry.runtime_data.status
    mock_client.async_get_stream_url.side_effect = CannotConnect("down")
    for minutes in [5, 10, 20, 40, 60, 60]:
        await tick(hass, frozen, status.update_interval)
        assert status.update_interval == timedelta(minutes=minutes)
        assert not status.last_update_success
    assert hass.states.get(BS1).state == STATE_UNAVAILABLE

    mock_client.async_get_stream_url.side_effect = None
    await tick(hass, frozen, status.update_interval)
    assert status.update_interval == STATUS_OPEN_INTERVAL
    assert hass.states.get(BS1).state == STATE_ON


async def test_removed_camera_becomes_removable(
    hass: HomeAssistant,
    mock_client: AsyncMock,
    config_entry: MockConfigEntry,
    frozen: FrozenDateTimeFactory,
) -> None:
    await setup(hass, config_entry)
    mock_client.async_get_cameras.return_value = make_cameras()[:1]
    await config_entry.runtime_data.cameras.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(CAM2).state == STATE_UNAVAILABLE

    registry = dr.async_get(hass)
    gone = registry.async_get_device_by_identifier((DOMAIN, "2"), config_entry.entry_id)
    kept = registry.async_get_device_by_identifier((DOMAIN, "1"), config_entry.entry_id)
    assert await async_remove_config_entry_device(hass, config_entry, gone)
    assert not await async_remove_config_entry_device(hass, config_entry, kept)


async def test_new_camera_added_and_probed(
    hass: HomeAssistant,
    mock_client: AsyncMock,
    config_entry: MockConfigEntry,
    frozen: FrozenDateTimeFactory,
) -> None:
    await setup(hass, config_entry)
    mock_client.async_get_cameras.return_value = [
        *make_cameras(),
        Camera(3, "Infant Room", 1, "", False, "10", "Sunny Daycare", []),
    ]
    await config_entry.runtime_data.cameras.async_refresh()
    await tick(hass, frozen, timedelta(seconds=11))  # status refresh debouncer
    assert hass.states.get("camera.sunny_daycare_infant_room") is not None
    assert hass.states.get("binary_sensor.sunny_daycare_infant_room_viewing_available").state == (
        STATE_ON
    )


async def test_list_refresh_does_not_reprobe(
    hass: HomeAssistant,
    mock_client: AsyncMock,
    config_entry: MockConfigEntry,
    frozen: FrozenDateTimeFactory,
) -> None:
    await setup(hass, config_entry)
    calls = mock_client.async_get_stream_url.await_count
    await config_entry.runtime_data.cameras.async_refresh()
    await tick(hass, frozen, timedelta(seconds=11))
    assert mock_client.async_get_stream_url.await_count == calls


async def test_health_check_timer(
    hass: HomeAssistant,
    mock_client: AsyncMock,
    config_entry: MockConfigEntry,
    frozen: FrozenDateTimeFactory,
) -> None:
    await setup(hass, config_entry)
    entity = hass.data["camera"].get_entity(CAM1)
    failed = MagicMock(outputs=MagicMock(return_value={"hls": 1}), available=False)
    entity.stream = failed
    await tick(hass, frozen, timedelta(seconds=61))
    failed.update_source.assert_called_once()


FIVE_MIN = timedelta(minutes=5)


def fake_stream(*, watched: bool = True, available: bool = True, source: str = "old") -> MagicMock:
    return MagicMock(
        outputs=MagicMock(return_value={"hls": 1} if watched else {}),
        available=available,
        source=source,
        stop=AsyncMock(),
        async_get_image=AsyncMock(return_value=b"frame"),
    )


async def test_stream_source_reuses_recent_url(
    hass: HomeAssistant,
    mock_client: AsyncMock,
    config_entry: MockConfigEntry,
    frozen: FrozenDateTimeFactory,
) -> None:
    await setup(hass, config_entry)
    entity = hass.data["camera"].get_entity(CAM1)
    calls = mock_client.async_get_stream_url.await_count
    # The setup status check opened the camera moments ago: reuse that URL.
    assert await entity.stream_source() == "http://wowza1.example/s.m3u8?tok"
    assert mock_client.async_get_stream_url.await_count == calls

    # After 4 minutes the token can no longer open connections: ask again.
    frozen.tick(timedelta(minutes=4))
    mock_client.async_get_stream_url.return_value = "http://new?tok2"
    assert await entity.stream_source() == "http://new?tok2"
    assert mock_client.async_get_stream_url.await_count == calls + 1
    # ...and that one is reused in turn.
    assert await entity.stream_source() == "http://new?tok2"
    assert mock_client.async_get_stream_url.await_count == calls + 1


async def test_status_check_reuses_viewer_url_and_skips_streaming(
    hass: HomeAssistant,
    mock_client: AsyncMock,
    config_entry: MockConfigEntry,
    frozen: FrozenDateTimeFactory,
) -> None:
    await setup(hass, config_entry)
    status = config_entry.runtime_data.status
    calls = mock_client.async_get_stream_url.await_count
    # Recently opened cameras are not opened again by the status check.
    await status.async_refresh()
    assert mock_client.async_get_stream_url.await_count == calls

    # A camera being watched is not opened either, even much later.
    status.streaming.add(1)
    frozen.tick(timedelta(hours=2))
    await status.async_refresh()
    assert mock_client.async_get_stream_url.await_count == calls + 1  # only camera 2
    assert status.is_open(1)


async def test_new_viewer_gets_fresh_stream(
    hass: HomeAssistant,
    mock_client: AsyncMock,
    config_entry: MockConfigEntry,
    frozen: FrozenDateTimeFactory,
) -> None:
    await setup(hass, config_entry)
    entity = hass.data["camera"].get_entity(CAM1)
    first = await entity.async_create_stream()
    assert first is not None
    assert first.source == "http://wowza1.example/s.m3u8?tok"
    # Fresh idle stream is reused.
    assert await entity.async_create_stream() is first

    # Idle stream whose token is too old: replaced, with a new token.
    frozen.tick(FIVE_MIN)
    mock_client.async_get_stream_url.return_value = "http://new?tok2"
    second = await entity.async_create_stream()
    assert second is not first
    assert second.source == "http://new?tok2"

    # A stream someone is watching is always reused.
    frozen.tick(FIVE_MIN)
    watched = fake_stream()
    entity.stream = watched
    assert await entity.async_create_stream() is watched


async def test_stream_failure_repaired_immediately(
    hass: HomeAssistant,
    mock_client: AsyncMock,
    config_entry: MockConfigEntry,
    frozen: FrozenDateTimeFactory,
) -> None:
    await setup(hass, config_entry)
    entity = hass.data["camera"].get_entity(CAM1)
    stream = await entity.async_create_stream()
    status = config_entry.runtime_data.status

    # Simulate a watched, healthy worker: camera is marked streaming.
    stream.add_provider("hls")
    stream._available = True
    entity._handle_stream_update()
    assert 1 in status.streaming

    # The worker fails after the token aged: the stream callback repairs it right away.
    frozen.tick(FIVE_MIN)
    mock_client.async_get_stream_url.return_value = "http://new?tok2"
    calls = mock_client.async_get_stream_url.await_count
    stream._available = False
    stream.update_source = MagicMock()
    entity._handle_stream_update()
    await hass.async_block_till_done()
    assert 1 not in status.streaming
    stream.update_source.assert_called_once_with("http://new?tok2")
    assert mock_client.async_get_stream_url.await_count == calls + 1

    # Repeated failures within 30 s don't request more tokens.
    entity._handle_stream_update()
    await hass.async_block_till_done()
    assert mock_client.async_get_stream_url.await_count == calls + 1
    await stream.stop()


async def test_health_check(
    hass: HomeAssistant,
    mock_client: AsyncMock,
    config_entry: MockConfigEntry,
    frozen: FrozenDateTimeFactory,
) -> None:
    await setup(hass, config_entry)
    entity = hass.data["camera"].get_entity(CAM1)
    frozen.tick(FIVE_MIN)  # setup URL expired
    calls = mock_client.async_get_stream_url.await_count

    await entity._async_check_stream()  # no stream: nothing to do

    idle = fake_stream(watched=False)
    entity.stream = idle
    await entity._async_check_stream()
    assert entity.stream is None
    idle.stop.assert_awaited_once()

    healthy = fake_stream()
    entity.stream = healthy
    await entity._async_check_stream()
    assert mock_client.async_get_stream_url.await_count == calls
    healthy.update_source.assert_not_called()

    # Transient error: keep the stream for the next check.
    failing = fake_stream(available=False)
    entity.stream = failing
    mock_client.async_get_stream_url.side_effect = CannotConnect("x")
    await entity._async_check_stream()
    assert entity.stream is failing

    # Camera closed while the stream was failing: dropped, camera off.
    frozen.tick(timedelta(seconds=31))
    mock_client.async_get_stream_url.side_effect = CLOSED
    await entity._async_check_stream()
    await hass.async_block_till_done()
    assert entity.stream is None
    failing.stop.assert_awaited()
    assert not entity.is_on
    assert hass.states.get(BS1).state == STATE_OFF


async def test_stills_never_contact_the_service(
    hass: HomeAssistant,
    mock_client: AsyncMock,
    config_entry: MockConfigEntry,
    frozen: FrozenDateTimeFactory,
) -> None:
    await setup(hass, config_entry)
    entity = hass.data["camera"].get_entity(CAM1)
    calls = mock_client.async_get_stream_url.await_count
    placeholder = await entity.async_camera_image()
    assert placeholder.startswith(b"\xff\xd8")  # JPEG
    assert entity.stream is None  # no stream was started

    # While someone watches, the current frame is used and remembered.
    entity.stream = fake_stream()
    assert await entity.async_camera_image() == b"frame"
    entity.stream = None
    assert await entity.async_camera_image() == b"frame"
    assert mock_client.async_get_stream_url.await_count == calls


async def test_stream_source_learns_closed(
    hass: HomeAssistant,
    mock_client: AsyncMock,
    config_entry: MockConfigEntry,
    frozen: FrozenDateTimeFactory,
) -> None:
    await setup(hass, config_entry)
    frozen.tick(FIVE_MIN)
    entity = hass.data["camera"].get_entity(CAM1)
    mock_client.async_get_stream_url.side_effect = DENIED_TODAY
    assert await entity.stream_source() is None
    await hass.async_block_till_done()
    assert hass.states.get(BS1).state == STATE_OFF
    assert hass.states.get(BS1).attributes["code"] == 23
    # Same status again is a no-op.
    config_entry.runtime_data.status.async_set_camera_status(
        1, ViewingStatus(False, 23, DENIED_TODAY.message)
    )


async def test_coordinator_close_stops_stream(
    hass: HomeAssistant,
    mock_client: AsyncMock,
    config_entry: MockConfigEntry,
    frozen: FrozenDateTimeFactory,
) -> None:
    await setup(hass, config_entry)
    frozen.tick(FIVE_MIN)
    entity = hass.data["camera"].get_entity(CAM1)
    stream = fake_stream(watched=False)
    entity.stream = stream
    mock_client.async_get_stream_url.side_effect = CLOSED
    await config_entry.runtime_data.status.async_refresh()
    await hass.async_block_till_done()
    stream.stop.assert_awaited_once()
    assert entity.stream is None


@pytest.mark.parametrize("use_webrtc", [False, True])
async def test_webrtc_is_opt_in(
    hass: HomeAssistant,
    mock_client: AsyncMock,
    config_entry: MockConfigEntry,
    frozen: FrozenDateTimeFactory,
    use_webrtc: bool,
) -> None:
    provider = MagicMock(async_register_camera=AsyncMock(), async_unregister_camera=AsyncMock())
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(config_entry, options={"use_webrtc": use_webrtc})
    with patch(
        "homeassistant.components.camera.async_get_supported_provider",
        AsyncMock(return_value=provider),
    ) as get_provider:
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()
        entity = hass.data["camera"].get_entity(CAM1)
        await entity.async_refresh_providers()
    types = {str(t) for t in entity.camera_capabilities.frontend_stream_types}
    if use_webrtc:
        assert get_provider.await_count >= 1
        assert types == {"hls", "web_rtc"}
    else:
        # No provider selection at all: no stream_source() call, HLS only.
        get_provider.assert_not_awaited()
        assert types == {"hls"}


async def test_debug_log_explains_requests(
    hass: HomeAssistant,
    mock_client: AsyncMock,
    config_entry: MockConfigEntry,
    frozen: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="custom_components.daycarewebwatch")
    await setup(hass, config_entry)
    entity = hass.data["camera"].get_entity(CAM1)
    await entity.stream_source()
    assert "Camera 1: reusing stream URL issued 0s ago" in caplog.text
    config_entry.runtime_data.status.streaming.add(1)
    await config_entry.runtime_data.status.async_refresh()
    assert "Status check camera 1 skipped: being watched" in caplog.text
    assert "Viewing status: 1=open, 2=open; next check in 1:00:00" in caplog.text
    assert "tok" not in caplog.text  # never log tokens


async def test_status_helpers_before_data(hass: HomeAssistant, config_entry, mock_client) -> None:
    config_entry.add_to_hass(hass)
    cams = CameraListCoordinator(hass, config_entry, mock_client)
    status = ViewingStatusCoordinator(hass, config_entry, mock_client, cams)
    assert not status.is_open(1)
    assert status.status(1) is None
    assert ViewingStatus(False, 999).reason == "Not available"
    assert ViewingStatus(True).reason is None


async def test_diagnostics(
    hass: HomeAssistant, mock_client: AsyncMock, config_entry: MockConfigEntry, frozen
) -> None:
    await setup(hass, config_entry)
    diag = await async_get_config_entry_diagnostics(hass, config_entry)
    text = str(diag)
    assert EMAIL not in text
    assert "secret" not in text
    assert diag["cameras"][0]["name"] == "Toddler Room"
    assert diag["viewing_status"]["1"] == {"open": True, "code": None, "message": None}
    assert diag["request_counts"] == {"viewcamera": 2}
    assert diag["streaming"] == []
