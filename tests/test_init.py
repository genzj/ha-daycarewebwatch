"""Tests for setup, coordinators and entities."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, issue_registry as ir
from homeassistant.setup import async_setup_component
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.daycarewebwatch.api import (
    Camera,
    CameraGone,
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
from custom_components.daycarewebwatch.coordinator import ViewingStatus
from custom_components.daycarewebwatch.diagnostics import async_get_config_entry_diagnostics

from .conftest import EMAIL, make_cameras

CAM1 = "camera.sunny_daycare_toddler_room"
CAM2 = "camera.sunny_daycare_playground"
BS1 = "binary_sensor.sunny_daycare_toddler_room_viewing_available"

# Wednesday 2026-10-28 10:00 local (in-window); HA test tz is US/Pacific.
WEDNESDAY_10 = "2026-10-28 10:00:00-07:00"
WEDNESDAY_20 = "2026-10-28 20:00:00-07:00"
FRIDAY_19 = "2026-10-30 19:00:00-07:00"


async def setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


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
    assert state is not None
    assert state.attributes["children"] == ["Ann Lee"]
    assert state.attributes["daycare"] == "Sunny Daycare"
    assert hass.states.get(BS1).state == STATE_ON
    assert hass.states.get(BS1).attributes["reason"] is None

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
    mock_client.async_check_permission.side_effect = InvalidAuth
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.SETUP_ERROR


async def test_daycare_closed_at_night_and_reopens(
    hass: HomeAssistant,
    mock_client: AsyncMock,
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    freezer.move_to(WEDNESDAY_20)
    mock_client.async_check_permission.side_effect = ViewingClosed("closed", 254)
    await setup(hass, config_entry)

    # Daycare-wide closure: only one probe for both cameras.
    assert mock_client.async_check_permission.await_count == 1
    status = config_entry.runtime_data.status
    assert status.last_update_success
    # Next probe at 06:30 would be 10.5 h away -> capped at 3 h.
    assert status.update_interval == timedelta(hours=3)
    assert hass.states.get(BS1).state == STATE_OFF
    assert hass.states.get(BS1).attributes["reason"] == "daycare_closed"
    # Closed is not unavailable.
    assert hass.states.get(CAM1).state != STATE_UNAVAILABLE
    assert "Error" not in caplog.text

    # Camera refuses to stream while closed, without calling the API.
    camera_entity = hass.data["camera"].get_entity(CAM1)
    assert await camera_entity.stream_source() is None
    mock_client.async_get_stream_url.assert_not_awaited()

    # Next morning the daycare opens.
    mock_client.async_check_permission.side_effect = None
    freezer.move_to("2026-10-29 06:31:00-07:00")
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(BS1).state == STATE_ON
    assert status.update_interval == STATUS_OPEN_INTERVAL
    assert "is now viewable" in caplog.text


async def test_room_closed_in_window_polls_fast(
    hass: HomeAssistant, mock_client: AsyncMock, config_entry: MockConfigEntry, frozen
) -> None:
    mock_client.async_check_permission.side_effect = ViewingClosed("off", 351)
    await setup(hass, config_entry)
    # Per-camera code: both cameras probed.
    assert mock_client.async_check_permission.await_count == 2
    assert config_entry.runtime_data.status.update_interval == STATUS_CLOSED_IN_WINDOW_INTERVAL
    assert hass.states.get(BS1).attributes["reason"] == "camera_off"


async def test_weekend_interval(
    hass: HomeAssistant,
    mock_client: AsyncMock,
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(FRIDAY_19)
    mock_client.async_check_permission.side_effect = ViewingClosed("closed", 254)
    await setup(hass, config_entry)
    status = config_entry.runtime_data.status
    calls = mock_client.async_check_permission.await_count
    # Over the whole weekend: one probe per 3 h at most.
    for _ in range(20):
        freezer.tick(timedelta(hours=3))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
    assert mock_client.async_check_permission.await_count - calls <= 20
    assert status.update_interval <= timedelta(hours=3)


async def test_network_error_backoff_and_recovery(
    hass: HomeAssistant,
    mock_client: AsyncMock,
    config_entry: MockConfigEntry,
    frozen: FrozenDateTimeFactory,
) -> None:
    await setup(hass, config_entry)
    status = config_entry.runtime_data.status
    mock_client.async_check_permission.side_effect = CannotConnect("down")
    expected = [5, 10, 20, 40, 60, 60]
    for minutes in expected:
        frozen.tick(status.update_interval)
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
        assert status.update_interval == timedelta(minutes=minutes)
        assert not status.last_update_success
    assert hass.states.get(BS1).state == STATE_UNAVAILABLE

    mock_client.async_check_permission.side_effect = None
    frozen.tick(status.update_interval)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert status.update_interval == STATUS_OPEN_INTERVAL
    assert hass.states.get(BS1).state == STATE_ON


async def test_camera_gone_refreshes_list_and_allows_removal(
    hass: HomeAssistant,
    mock_client: AsyncMock,
    config_entry: MockConfigEntry,
    frozen: FrozenDateTimeFactory,
) -> None:
    await setup(hass, config_entry)
    assert await async_setup_component(hass, "config", {})
    mock_client.async_get_cameras.return_value = make_cameras()[:1]

    async def check(camera_id: int) -> None:
        if camera_id == 2:
            raise CameraGone("gone", 350)

    mock_client.async_check_permission.side_effect = check
    await config_entry.runtime_data.status.async_refresh()
    await hass.async_block_till_done()
    # Camera list refreshed through the debouncer.
    frozen.tick(timedelta(seconds=11))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert 2 not in config_entry.runtime_data.cameras.data
    assert hass.states.get(CAM2).state == STATE_UNAVAILABLE

    device_registry = dr.async_get(hass)
    gone = device_registry.async_get_device_by_identifier((DOMAIN, "2"), config_entry.entry_id)
    kept = device_registry.async_get_device_by_identifier((DOMAIN, "1"), config_entry.entry_id)
    from custom_components.daycarewebwatch import async_remove_config_entry_device

    assert await async_remove_config_entry_device(hass, config_entry, gone)
    assert not await async_remove_config_entry_device(hass, config_entry, kept)


async def test_new_camera_added_dynamically(
    hass: HomeAssistant, mock_client: AsyncMock, config_entry: MockConfigEntry, frozen
) -> None:
    await setup(hass, config_entry)
    mock_client.async_get_cameras.return_value = [
        *make_cameras(),
        Camera(3, "Infant Room", 1, "", False, "10", "Sunny Daycare", []),
    ]
    await config_entry.runtime_data.cameras.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get("camera.sunny_daycare_infant_room") is not None
    assert hass.states.get("binary_sensor.sunny_daycare_infant_room_viewing_available") is not None


async def test_stream_source_and_refresh(
    hass: HomeAssistant,
    mock_client: AsyncMock,
    config_entry: MockConfigEntry,
    frozen: FrozenDateTimeFactory,
) -> None:
    await setup(hass, config_entry)
    entity = hass.data["camera"].get_entity(CAM1)
    assert entity.is_on
    assert await entity.stream_source() == "http://wowza1.example/s.m3u8?tok"

    # No stream: the refresh timer does nothing.
    calls = mock_client.async_get_stream_url.await_count
    await entity._async_refresh_stream()
    assert mock_client.async_get_stream_url.await_count == calls

    # Idle stream (no viewers) is dropped instead of refreshed.
    idle = MagicMock(outputs=MagicMock(return_value={}), stop=AsyncMock())
    entity.stream = idle
    await entity._async_refresh_stream()
    assert entity.stream is None
    idle.stop.assert_awaited_once()
    assert mock_client.async_get_stream_url.await_count == calls

    # Healthy active stream: swap URL without restart.
    active = MagicMock(outputs=MagicMock(return_value={"hls": 1}), available=True, stop=AsyncMock())
    entity.stream = active
    mock_client.async_get_stream_url.return_value = "http://new?tok2"
    await entity._async_refresh_stream()
    assert active.source == "http://new?tok2"
    active.update_source.assert_not_called()

    # Failed stream: restart with the new URL.
    active.available = False
    await entity._async_refresh_stream()
    active.update_source.assert_called_once_with("http://new?tok2")

    # Transient error: keep the stream.
    mock_client.async_get_stream_url.side_effect = CannotConnect("x")
    await entity._async_refresh_stream()
    assert entity.stream is active

    # Closed while streaming: stream stopped, camera off.
    mock_client.async_get_stream_url.side_effect = ViewingClosed("closed", 254)
    await entity._async_refresh_stream()
    await hass.async_block_till_done()
    assert entity.stream is None
    active.stop.assert_awaited()
    assert not entity.is_on
    assert hass.states.get(BS1).state == STATE_OFF


async def test_stream_source_learns_closed(
    hass: HomeAssistant, mock_client: AsyncMock, config_entry: MockConfigEntry, frozen
) -> None:
    await setup(hass, config_entry)
    entity = hass.data["camera"].get_entity(CAM1)
    mock_client.async_get_stream_url.side_effect = ViewingClosed("off", 301)
    assert await entity.stream_source() is None
    await hass.async_block_till_done()
    assert hass.states.get(BS1).state == STATE_OFF
    assert hass.states.get(BS1).attributes["reason"] == "room_inactive"
    # Same status again is a no-op.
    config_entry.runtime_data.status.async_set_camera_status(1, ViewingStatus(False, 301))


async def test_coordinator_close_stops_stream(
    hass: HomeAssistant, mock_client: AsyncMock, config_entry: MockConfigEntry, frozen
) -> None:
    await setup(hass, config_entry)
    entity = hass.data["camera"].get_entity(CAM1)
    stream = MagicMock(stop=AsyncMock())
    entity.stream = stream
    mock_client.async_check_permission.side_effect = ViewingClosed("closed", 101)
    await config_entry.runtime_data.status.async_refresh()
    await hass.async_block_till_done()
    stream.stop.assert_awaited_once()
    assert entity.stream is None
    assert hass.states.get(BS1).attributes["reason"] == "access_denied"


async def test_status_helpers_before_data(hass: HomeAssistant, config_entry, mock_client) -> None:
    from custom_components.daycarewebwatch.coordinator import (
        CameraListCoordinator,
        ViewingStatusCoordinator,
    )

    config_entry.add_to_hass(hass)
    cams = CameraListCoordinator(hass, config_entry, mock_client)
    status = ViewingStatusCoordinator(hass, config_entry, mock_client, cams)
    assert not status.is_open(1)
    assert status.status(1) is None
    assert ViewingStatus(False, 999).reason == "unavailable"


async def test_diagnostics(
    hass: HomeAssistant, mock_client: AsyncMock, config_entry: MockConfigEntry, frozen
) -> None:
    await setup(hass, config_entry)
    diag = await async_get_config_entry_diagnostics(hass, config_entry)
    text = str(diag)
    assert EMAIL not in text
    assert "secret" not in text
    assert diag["cameras"][0]["name"] == "Toddler Room"
    assert diag["viewing_status"]["1"] == {"open": True, "code": None}
