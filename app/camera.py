from __future__ import annotations

import logging
import threading
import time
from collections.abc import Generator

import cv2

from app.config import CameraConfig

logger = logging.getLogger(__name__)


class Camera:
    def __init__(self, config: CameraConfig) -> None:
        self.config = config
        self._capture: cv2.VideoCapture | None = None
        self._latest_jpeg: bytes | None = None
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self.actual_width = 0
        self.actual_height = 0
        self.actual_fps = 0.0

    @property
    def id(self) -> str:
        return self.config.id

    @property
    def name(self) -> str:
        return self.config.name

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return

        self._stop_event.clear()
        if self._capture is None or not self._capture.isOpened():
            self._capture = self._open_capture()

        self._thread = threading.Thread(
            target=self._capture_loop,
            name=f"camera-{self.config.id}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

        if self._capture is not None:
            self._capture.release()
            self._capture = None

    def get_latest_jpeg(self) -> bytes | None:
        with self._lock:
            return self._latest_jpeg

    def mjpeg_frames(self) -> Generator[bytes, None, None]:
        frame_interval = 1.0 / max(self.config.fps, 1)
        boundary = b"frame"

        while not self._stop_event.is_set():
            jpeg = self.get_latest_jpeg()
            if jpeg is not None:
                yield (
                    b"--" + boundary + b"\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    b"Content-Length: " + str(len(jpeg)).encode() + b"\r\n\r\n"
                    + jpeg
                    + b"\r\n"
                )
            time.sleep(frame_interval)

    def _open_capture(self) -> cv2.VideoCapture:
        capture = cv2.VideoCapture(self.config.device_index)
        if not capture.isOpened():
            raise RuntimeError(
                f"Could not open camera '{self.config.id}' "
                f"(device_index={self.config.device_index})"
            )

        capture.set(cv2.CAP_PROP_FRAME_WIDTH, self.config.width)
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, self.config.height)
        capture.set(cv2.CAP_PROP_FPS, self.config.fps)

        self.actual_width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.actual_height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
        self.actual_fps = float(capture.get(cv2.CAP_PROP_FPS))

        logger.info(
            "Camera '%s' opened at %dx%d @ %.1f fps (requested %dx%d @ %d fps)",
            self.config.id,
            self.actual_width,
            self.actual_height,
            self.actual_fps,
            self.config.width,
            self.config.height,
            self.config.fps,
        )
        return capture

    def _capture_loop(self) -> None:
        frame_interval = 1.0 / max(self.config.fps, 1)

        while not self._stop_event.is_set():
            try:
                if self._capture is None or not self._capture.isOpened():
                    self._capture = self._open_capture()

                ok, frame = self._capture.read()
                if not ok:
                    logger.warning("Failed to read frame from camera '%s'", self.config.id)
                    if self._capture is not None:
                        self._capture.release()
                        self._capture = None
                    time.sleep(1)
                    continue

                ok, encoded = cv2.imencode(
                    ".jpg",
                    frame,
                    [int(cv2.IMWRITE_JPEG_QUALITY), self.config.jpeg_quality],
                )
                if not ok:
                    continue

                with self._lock:
                    self._latest_jpeg = encoded.tobytes()

            except Exception:
                logger.exception("Camera '%s' capture loop error", self.config.id)
                if self._capture is not None:
                    self._capture.release()
                    self._capture = None
                time.sleep(1)
                continue

            time.sleep(frame_interval)


class CameraManager:
    def __init__(self, cameras: list[CameraConfig]) -> None:
        self._cameras = {config.id: Camera(config) for config in cameras}

    def start_all(self) -> None:
        for camera in self._cameras.values():
            camera.start()

    def stop_all(self) -> None:
        for camera in self._cameras.values():
            camera.stop()

    def list_cameras(self) -> list[Camera]:
        return list(self._cameras.values())

    def get(self, camera_id: str) -> Camera | None:
        return self._cameras.get(camera_id)
