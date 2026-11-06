from __future__ import annotations

import io
import logging
import os
import select
import subprocess
import sys
import threading
import time
from collections.abc import Generator

from app.config import CameraConfig
from app.feeds import SLOW_S

logger = logging.getLogger(__name__)


def device_offers_mjpeg(device_index: int) -> bool:
    """True when the USB camera can hand us JPEG frames."""
    try:
        completed = subprocess.run(
            ["v4l2-ctl", "-d", f"/dev/video{device_index}", "--list-formats"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    text = f"{completed.stdout}\n{completed.stderr}".upper()
    return "MJPG" in text or "MJPEG" in text


def v4l2_command(
    device: str,
    width: int,
    height: int,
    fps: int,
    *,
    mjpeg_copy: bool,
    jpeg_quality: int,
) -> list[str]:
    """ffmpeg argv that writes one MJPEG stream to stdout."""
    rate = str(max(int(fps), 1))
    # ffmpeg's q:v runs 2 (clear) to 31 (small). JPEG quality runs the other way.
    qscale = str(max(2, min(31, round((100 - jpeg_quality) * 29 / 99) + 2)))
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-fflags",
        "nobuffer",
        "-f",
        "v4l2",
    ]
    if mjpeg_copy:
        command.extend(["-input_format", "mjpeg"])
    command.extend(
        [
            "-video_size",
            f"{int(width)}x{int(height)}",
            "-framerate",
            rate,
            "-i",
            device,
        ]
    )
    if mjpeg_copy:
        command.extend(["-c:v", "copy"])
    else:
        command.extend(["-vf", f"fps={rate}", "-q:v", qscale])
    command.extend(["-f", "mjpeg", "pipe:1"])
    return command


def take_jpeg(buf: bytearray) -> bytes | None:
    """Pull one JPEG out of a byte stream. Leave a partial frame in buf."""
    start = buf.find(b"\xff\xd8")
    if start < 0:
        if len(buf) > 1:
            del buf[:-1]
        return None
    if start:
        del buf[:start]
    end = buf.find(b"\xff\xd9", 2)
    if end < 0:
        return None
    frame = bytes(buf[: end + 2])
    del buf[: end + 2]
    return frame


def start_picamera2(size: tuple[int, int], camera_num: int = 0, fps: int | None = None):
    """Open the Pi camera module upright, with normal auto exposure.

    Official modules rotate the sensor 180 degrees unless the transform is
    identity. Identity turns this upside-down mount the right way up, and
    the normal exposure constraint keeps the room's colour. fps asks the
    sensor for that rate. Leave it unset and the module keeps its own pace.
    The main stream is YUV420 so the camera's JPEG encoder can take it.
    RGB888 would be converted on the CPU.
    """
    from libcamera import Transform, controls
    from picamera2 import Picamera2

    cam = Picamera2(camera_num=camera_num)
    config = cam.create_preview_configuration(
        main={"size": size, "format": "YUV420"},
        transform=Transform(),
    )
    cam.configure(config)
    sensor = {
        "AeEnable": True,
        "AwbEnable": True,
        "AwbMode": controls.AwbModeEnum.Auto,
        "AeConstraintMode": controls.AeConstraintModeEnum.Normal,
    }
    if fps is not None:
        sensor["FrameRate"] = float(max(fps, 1))
    cam.set_controls(sensor)
    cam.start()
    return cam


def encode_jpeg(frame, quality: int) -> bytes | None:
    try:
        import cv2

        ok, encoded = cv2.imencode(
            ".jpg",
            frame,
            [int(cv2.IMWRITE_JPEG_QUALITY), quality],
        )
        if ok:
            return encoded.tobytes()
    except ImportError:
        pass
    try:
        import simplejpeg

        # OpenCV frames and Picamera2 RGB888 are both BGR in memory.
        return simplejpeg.encode_jpeg(frame, quality=quality, colorspace="BGR")
    except ImportError:
        return None


class Camera:
    def __init__(self, config: CameraConfig) -> None:
        self.config = config
        self._capture = None
        self._picam = None
        self._ffmpeg: subprocess.Popen[bytes] | None = None
        self._jpeg_buf = bytearray()
        self._v4l2_fps: int | None = None
        self._latest_jpeg: bytes | None = None
        self._arrived_at = 0.0
        self._thermal: bytes | None = None
        self._slow = config.slow
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
        self._close()

    def get_latest_jpeg(self) -> bytes | None:
        with self._lock:
            return self._latest_jpeg

    def arrived_at(self) -> float:
        with self._lock:
            return self._arrived_at

    def store_thermal(self, jpeg: bytes) -> None:
        with self._lock:
            self._thermal = jpeg

    def thermal_jpeg(self) -> bytes | None:
        with self._lock:
            return self._thermal

    @property
    def slow(self) -> bool:
        with self._lock:
            return self._slow

    def set_slow(self, slow: bool) -> None:
        with self._lock:
            self._slow = slow

    def frame_interval(self) -> float:
        if self.slow:
            return SLOW_S
        return 1.0 / max(self.config.fps, 1)

    def mjpeg_frames(self) -> Generator[bytes, None, None]:
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
            time.sleep(self.frame_interval())

    def _v4l2_target_fps(self) -> int:
        if self.slow:
            return 1
        return max(int(self.config.fps), 1)

    def _open(self) -> None:
        if self.config.backend == "v4l2":
            target = self._v4l2_target_fps()
            if self._ffmpeg is None or self._v4l2_fps != target or self._ffmpeg.poll() is not None:
                self._close_ffmpeg()
                self._open_v4l2(target)
            return
        if self.config.backend == "picamera2":
            if self._picam is None:
                self._open_picamera2()
            return
        if self._capture is None:
            self._open_opencv()

    def _close(self) -> None:
        self._close_ffmpeg()
        if self._capture is not None:
            self._capture.release()
            self._capture = None
        if self._picam is not None:
            try:
                self._picam.stop()
            except Exception:
                pass
            try:
                self._picam.close()
            except Exception:
                pass
            self._picam = None

    def _close_ffmpeg(self) -> None:
        proc = self._ffmpeg
        self._ffmpeg = None
        self._v4l2_fps = None
        self._jpeg_buf.clear()
        if proc is None:
            return
        if proc.stdout is not None:
            try:
                proc.stdout.close()
            except OSError:
                pass
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    pass

    def _open_v4l2(self, fps: int) -> None:
        device = f"/dev/video{self.config.device_index}"
        command = v4l2_command(
            device,
            self.config.width,
            self.config.height,
            fps,
            mjpeg_copy=self.config.mjpeg_copy,
            jpeg_quality=self.config.jpeg_quality,
        )
        self._jpeg_buf.clear()
        self._ffmpeg = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        self._v4l2_fps = fps
        self.actual_width = self.config.width
        self.actual_height = self.config.height
        self.actual_fps = float(fps)
        logger.info("Camera '%s' opened via v4l2: %s", self.config.id, " ".join(command))

    def _read_v4l2_jpeg(self) -> bytes:
        proc = self._ffmpeg
        if proc is None or proc.stdout is None:
            raise RuntimeError("ffmpeg is not running")
        fd = proc.stdout.fileno()
        while not self._stop_event.is_set():
            if proc.poll() is not None and not self._jpeg_buf:
                raise RuntimeError(f"ffmpeg exited ({proc.returncode})")
            ready, _, _ = select.select([fd], [], [], 0.4)
            if not ready:
                continue
            try:
                chunk = os.read(fd, 8192)
            except OSError as exc:
                raise RuntimeError("ffmpeg read failed") from exc
            if not chunk:
                raise RuntimeError("ffmpeg closed the picture stream")
            self._jpeg_buf.extend(chunk)
            frame = take_jpeg(self._jpeg_buf)
            if frame:
                return frame
        raise RuntimeError("camera stopped")

    def _grab_v4l2(self) -> None:
        self._store_jpeg(self._read_v4l2_jpeg())

    def _open_opencv(self):
        import cv2

        index = self.config.device_index
        if sys.platform == "darwin":
            api = getattr(cv2, "CAP_AVFOUNDATION", 1200)
            capture = cv2.VideoCapture(index, api)
        else:
            api = getattr(cv2, "CAP_V4L2", 200)
            capture = cv2.VideoCapture(f"/dev/video{index}", api)
            if not capture.isOpened():
                capture.release()
                capture = cv2.VideoCapture(index, api)
        if not capture.isOpened():
            capture.release()
            raise RuntimeError(
                f"Could not open camera '{self.config.id}' "
                f"(device_index={index})"
            )

        capture.set(cv2.CAP_PROP_FRAME_WIDTH, self.config.width)
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, self.config.height)
        # Leave the webcam's own frame rate. config.fps only paces the stream.

        self.actual_width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.actual_height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
        self.actual_fps = float(capture.get(cv2.CAP_PROP_FPS))
        self._capture = capture
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

    def _open_picamera2(self) -> None:
        self._picam = start_picamera2(
            (self.config.width, self.config.height),
            camera_num=self.config.device_index,
            fps=self.config.fps,
        )
        self.actual_width = self.config.width
        self.actual_height = self.config.height
        self.actual_fps = float(self.config.fps)
        logger.info(
            "Camera '%s' opened via picamera2 at %dx%d @ %d fps",
            self.config.id,
            self.actual_width,
            self.actual_height,
            self.config.fps,
        )

    def _store_jpeg(self, jpeg: bytes | None) -> None:
        if not jpeg:
            return
        with self._lock:
            self._latest_jpeg = jpeg
            self._arrived_at = time.monotonic()

    def _grab_picamera2(self) -> None:
        assert self._picam is not None
        buf = io.BytesIO()
        try:
            self._picam.capture_file(buf, format="jpeg")
            jpeg = buf.getvalue()
            if jpeg:
                self._store_jpeg(jpeg)
                return
        except Exception:
            logger.exception("picamera2 JPEG capture failed; trying array encode")
        frame = self._picam.capture_array()
        self.actual_height, self.actual_width = frame.shape[0], frame.shape[1]
        self._store_jpeg(encode_jpeg(frame, self.config.jpeg_quality))

    def _grab_opencv(self) -> None:
        import cv2

        if self._capture is None or not self._capture.isOpened():
            self._open_opencv()
        assert self._capture is not None
        ok, frame = self._capture.read()
        if not ok:
            logger.warning("Failed to read frame from camera '%s'", self.config.id)
            self._capture.release()
            self._capture = None
            time.sleep(1)
            return
        self._store_jpeg(encode_jpeg(frame, self.config.jpeg_quality))

    def _capture_loop(self) -> None:
        while not self._stop_event.is_set():
            started = time.monotonic()
            try:
                self._open()
                if self.config.backend == "v4l2":
                    self._grab_v4l2()
                elif self.config.backend == "picamera2":
                    self._grab_picamera2()
                else:
                    self._grab_opencv()
            except Exception:
                if self._stop_event.is_set():
                    break
                logger.exception("Camera '%s' capture loop error", self.config.id)
                self._close()
                time.sleep(1)
                continue
            if self.config.backend == "picamera2":
                remaining = self.frame_interval() - (time.monotonic() - started)
                if remaining > 0:
                    time.sleep(remaining)
            elif self.config.backend not in {"v4l2", "picamera2"}:
                time.sleep(self.frame_interval())


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

    def replace(self, configs: list[CameraConfig]) -> None:
        """Open the cameras in this list and stop any local camera that dropped out."""
        wanted = {config.id: config for config in configs}
        for camera_id, camera in list(self._cameras.items()):
            if camera_id not in wanted or camera.config != wanted[camera_id]:
                camera.stop()
                del self._cameras[camera_id]
        for config in configs:
            if config.id in self._cameras:
                continue
            camera = Camera(config)
            camera.start()
            self._cameras[config.id] = camera
