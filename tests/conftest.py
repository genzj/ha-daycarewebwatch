"""Fixtures for DaycareWebWatch tests."""

from __future__ import annotations

from collections import Counter
from collections.abc import Generator
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.daycarewebwatch.api import Camera
from custom_components.daycarewebwatch.const import CONF_INSTANCE_ID, DOMAIN

EMAIL = "parent@example.com"
PASSWORD = "secret"
INSTANCE_ID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Enable custom integrations."""


@pytest.fixture(autouse=True)
def no_probe_spacing() -> Generator[None]:
    """Don't wait between status probes in tests."""
    with patch("custom_components.daycarewebwatch.coordinator.PROBE_SPACING", 0):
        yield


def make_cameras() -> list[Camera]:
    return [
        Camera(1, "Toddler Room", 1, "", False, "10", "Sunny Daycare", ["Ann Lee"]),
        Camera(2, "Playground", 2, "", True, "10", "Sunny Daycare", ["Ann Lee"]),
    ]


@pytest.fixture
def config_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title=EMAIL,
        unique_id=EMAIL,
        data={"email": EMAIL, "password": PASSWORD, CONF_INSTANCE_ID: INSTANCE_ID},
    )


@pytest.fixture
def mock_client() -> Generator[AsyncMock]:
    """Patch the API client used by the integration and the config flow."""
    with (
        patch("custom_components.daycarewebwatch.DaycareWebWatchClient", autospec=True) as cls,
        patch(
            "custom_components.daycarewebwatch.config_flow.DaycareWebWatchClient",
            new=cls,
        ),
    ):
        client = cls.return_value
        client.request_counts = Counter({"viewcamera": 2})
        client.async_get_cameras.return_value = make_cameras()
        client.async_get_stream_url.return_value = "http://wowza1.example/s.m3u8?tok"
        yield client
