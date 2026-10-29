"""Shared platform helpers."""

from __future__ import annotations

from collections.abc import Callable

from homeassistant.core import callback

from .api import Camera
from .coordinator import DaycareWebWatchConfigEntry


@callback
def async_add_new_cameras(
    entry: DaycareWebWatchConfigEntry,
    add: Callable[[list[Camera]], None],
) -> None:
    """Call add() for current cameras and for any that appear later."""
    known: set[int] = set()
    cameras = entry.runtime_data.cameras

    @callback
    def _check() -> None:
        new = [cam for cam_id, cam in (cameras.data or {}).items() if cam_id not in known]
        if new:
            known.update(cam.id for cam in new)
            add(new)

    _check()
    entry.async_on_unload(cameras.async_add_listener(_check))
