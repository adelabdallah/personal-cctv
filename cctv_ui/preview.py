"""Camera frames for the panel, either from the live stream or Picamera2."""

from __future__ import annotations

import io
import logging
import threading
from urllib.error import URLError
from urllib.request import urlopen

from cctv_ui.service import preview_url, running_healthy

log = logging.getLogger(__name__)


class FramePump:
    """One camera client at a time. The stream server wins when it is up."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._mode: str | None = None
        self._jpeg: bytes | None = None
        self.error: str | None = None

    def jpeg(self) -> bytes | None:
        with self._lock:
            return self._jpeg

    def active(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def sync(self, stream_up: bool | None = None) -> None:
        want = "stream" if (stream_up if stream_up is not None else running_healthy()) else "local"
        if self._mode == want and self._thread and self._thread.is_alive():
            return
        self.stop()
        self._stop.clear()
        self._mode = want
        self.error = None
        target = self._stream_loop if want == "stream" else self._local_loop
        self._thread = threading.Thread(target=target, name=f"cctv-{want}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        self._thread = None
        self._mode = None
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=4)
        with self._lock:
            self._jpeg = None

    def _store(self, jpeg: bytes | None) -> None:
        if not jpeg:
            return
        with self._lock:
            self._jpeg = jpeg

    def _stream_loop(self) -> None:
        url = preview_url()
        while not self._stop.is_set():
            try:
                with urlopen(url, timeout=1) as resp:
                    if resp.status == 200:
                        self._store(resp.read())
                        self.error = None
            except (OSError, URLError) as exc:
                self.error = "waiting for stream"
                log.debug("preview fetch failed: %s", exc)
            self._stop.wait(0.12)

    def _local_loop(self) -> None:
        try:
            from app.camera import start_picamera2
        except ImportError:
            self.error = "picamera2 is not installed"
            return
        cam = None
        try:
            cam = start_picamera2((640, 480))
            while not self._stop.is_set():
                buf = io.BytesIO()
                cam.capture_file(buf, format="jpeg")
                self._store(buf.getvalue())
                self.error = None
                self._stop.wait(0.08)
        except Exception as exc:
            log.exception("local camera failed")
            self.error = str(exc)
        finally:
            if cam is not None:
                try:
                    cam.stop()
                except Exception:
                    log.debug("picamera2 stop failed", exc_info=True)
                try:
                    cam.close()
                except Exception:
                    log.debug("picamera2 close failed", exc_info=True)
