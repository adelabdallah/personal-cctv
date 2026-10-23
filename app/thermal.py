"""Latest thermal JPEG pushed by the host."""

from __future__ import annotations

import threading
import time
from collections.abc import Generator


class ThermalFeed:
    def __init__(self) -> None:
        self._jpeg: bytes | None = None
        self._lock = threading.Lock()

    def store(self, jpeg: bytes) -> None:
        with self._lock:
            self._jpeg = jpeg

    def latest(self) -> bytes | None:
        with self._lock:
            return self._jpeg


def mjpeg_part(jpeg: bytes) -> bytes:
    return (
        b"--frame\r\n"
        b"Content-Type: image/jpeg\r\n"
        b"Content-Length: " + str(len(jpeg)).encode() + b"\r\n\r\n" + jpeg + b"\r\n"
    )


def mjpeg_frames(feed: ThermalFeed) -> Generator[bytes, None, None]:
    while True:
        jpeg = feed.latest()
        if jpeg is not None:
            yield mjpeg_part(jpeg)
        time.sleep(0.3)
