"""JPEGs posted by other devices, plus whether a frame is recent."""

from __future__ import annotations

import threading
import time
from collections.abc import Generator
from dataclasses import dataclass

from app.thermal import mjpeg_part

FRESH_S = 3.0
MAX_JPEG = 1_000_000
SLOW_S = 1.0


@dataclass
class _Shot:
    jpeg: bytes
    at: float


class RemoteFeeds:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._names: dict[str, str] = {}
        self._video: dict[str, _Shot] = {}
        self._thermal: dict[str, _Shot] = {}
        self._slow: dict[str, bool] = {}
        self._pinned: set[str] = set()

    def store_video(self, camera_id: str, name: str, jpeg: bytes, now: float) -> None:
        with self._lock:
            self._names[camera_id] = name or camera_id
            self._video[camera_id] = _Shot(jpeg, now)

    def store_thermal(self, camera_id: str, jpeg: bytes, now: float) -> None:
        with self._lock:
            self._thermal[camera_id] = _Shot(jpeg, now)

    def set_slow(self, camera_id: str, slow: bool) -> bool:
        """Viewer choice. Later posts from the camera do not replace it."""
        with self._lock:
            self._slow[camera_id] = slow
            self._pinned.add(camera_id)
            return slow

    def note_slow(self, camera_id: str, declared: bool) -> bool:
        """A camera that boots slow says so on its first posts."""
        with self._lock:
            if camera_id in self._pinned:
                return self._slow.get(camera_id, False)
            if declared:
                self._slow[camera_id] = True
            return self._slow.get(camera_id, False)

    def slow(self, camera_id: str) -> bool:
        with self._lock:
            return self._slow.get(camera_id, False)

    def video(self, camera_id: str) -> bytes | None:
        with self._lock:
            shot = self._video.get(camera_id)
        return shot.jpeg if shot else None

    def thermal(self, camera_id: str) -> bytes | None:
        with self._lock:
            shot = self._thermal.get(camera_id)
        return shot.jpeg if shot else None

    def rows(self, now: float) -> list[dict[str, object]]:
        with self._lock:
            items = [
                (
                    camera_id,
                    self._names.get(camera_id, camera_id),
                    shot,
                    self._thermal.get(camera_id),
                    self._slow.get(camera_id, False),
                )
                for camera_id, shot in self._video.items()
            ]
        return [
            {
                "id": camera_id,
                "name": name,
                "width": 0,
                "height": 0,
                "fresh": fresh(shot.at, now),
                "thermal": thermal is not None,
                "slow": slow,
            }
            for camera_id, name, shot, thermal, slow in items
        ]


def fresh(arrived_at: float, now: float) -> bool:
    return arrived_at > 0 and (now - arrived_at) <= FRESH_S


def valid_jpeg(body: bytes) -> bool:
    return 0 < len(body) <= MAX_JPEG and body.startswith(b"\xff\xd8")


def mjpeg_frames(latest) -> Generator[bytes, None, None]:
    while True:
        jpeg = latest()
        if jpeg is not None:
            yield mjpeg_part(jpeg)
        time.sleep(0.3)
