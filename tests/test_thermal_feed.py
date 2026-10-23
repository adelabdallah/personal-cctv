"""Thermal flag, viewer button, and local JPEG post."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ["CCTV_PASSWORD"] = "test-password"
os.environ["SESSION_SECRET"] = "test-secret"
os.environ["INGEST_TOKEN"] = "test-token"
os.environ["CCTV_CLUSTER"] = "0"

from app.camera import Camera
from app.config import CameraConfig, get_settings, load_thermal_enabled
from app.thermal import ThermalFeed, mjpeg_part

JPEG = b"\xff\xd8\xff\xd9"


def _config(root: Path, thermal: bool) -> Path:
    path = root / "cctv.yaml"
    flag = "thermal: true\n" if thermal else ""
    path.write_text(
        (
            "app_name: Cyberdeck\n"
            f"{flag}"
            "cameras:\n"
            "  - id: deck\n"
            "    name: Deck\n"
            "    backend: opencv\n"
            "    device_index: 99\n"
            "    width: 640\n"
            "    height: 480\n"
        ),
        encoding="utf-8",
    )
    return path


class ThermalFeedTests(unittest.TestCase):
    def test_flag_is_on_only_for_true(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self.assertTrue(load_thermal_enabled(_config(root, True)))
            self.assertFalse(load_thermal_enabled(_config(root, False)))

    def test_mjpeg_part_carries_the_jpeg(self) -> None:
        part = mjpeg_part(JPEG)
        self.assertIn(b"image/jpeg", part)
        self.assertIn(JPEG, part)

    def test_store_returns_the_latest_jpeg(self) -> None:
        feed = ThermalFeed()
        self.assertIsNone(feed.latest())
        feed.store(JPEG)
        self.assertEqual(feed.latest(), JPEG)

    def test_picamera2_open_uses_device_index(self) -> None:
        camera = Camera(
            CameraConfig(
                id="deck",
                name="Deck",
                backend="picamera2",
                device_index=2,
                width=640,
                height=480,
            )
        )
        with patch("app.camera.start_picamera2", return_value=object()) as start:
            camera._open_picamera2()
        start.assert_called_once_with((640, 480), camera_num=2)

    def test_encode_jpeg_passes_bgr_to_simplejpeg(self) -> None:
        from app.camera import encode_jpeg

        seen: dict[str, object] = {}

        def _encode(frame: object, quality: int, colorspace: str = "RGB") -> bytes:
            seen["frame"] = frame
            seen["quality"] = quality
            seen["colorspace"] = colorspace
            return JPEG

        simple = type("SimpleJpeg", (), {"encode_jpeg": staticmethod(_encode)})
        frame = object()
        with patch.dict("sys.modules", {"cv2": None, "simplejpeg": simple}):
            self.assertEqual(encode_jpeg(frame, 70), JPEG)
        self.assertEqual(seen["colorspace"], "BGR")
        self.assertEqual(seen["quality"], 70)
        self.assertIs(seen["frame"], frame)

    def test_opencv_opens_the_v4l_node_without_forcing_fps(self) -> None:
        camera = Camera(
            CameraConfig(
                id="deck",
                name="HD Webcam",
                backend="opencv",
                device_index=10,
                width=640,
                height=480,
                fps=8,
            )
        )

        class _Capture:
            def __init__(self) -> None:
                self.opened = True
                self.props: list[tuple[int, float]] = []

            def isOpened(self) -> bool:
                return self.opened

            def release(self) -> None:
                self.opened = False

            def set(self, prop: int, value: float) -> bool:
                self.props.append((prop, value))
                return True

            def get(self, prop: int) -> float:
                return 0.0

        capture = _Capture()
        calls: list[tuple[object, ...]] = []

        class _Cv2:
            CAP_V4L2 = 200
            CAP_PROP_FRAME_WIDTH = 3
            CAP_PROP_FRAME_HEIGHT = 4
            CAP_PROP_FPS = 5

            @staticmethod
            def VideoCapture(*args: object) -> _Capture:
                calls.append(args)
                return capture

        with patch.dict("sys.modules", {"cv2": _Cv2()}):
            camera._open_opencv()
        self.assertEqual(calls, [("/dev/video10", 200)])
        self.assertEqual([prop for prop, _value in capture.props], [3, 4])

    def test_local_post_and_viewer_button(self) -> None:
        from fastapi.testclient import TestClient

        from app.main import app

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            os.environ["CONFIG_PATH"] = str(_config(root, True))
            get_settings.cache_clear()
            with (
                patch.object(Camera, "start", lambda self: None),
                patch.object(Camera, "stop", lambda self: None),
            ):
                with TestClient(app, client=("203.0.113.8", 50000)) as remote:
                    denied = remote.post("/thermal", content=JPEG)
                self.assertEqual(denied.status_code, 403)
                with TestClient(app, client=("127.0.0.1", 50000)) as client:
                    posted = client.post("/thermal", content=JPEG)
                    self.assertEqual(posted.status_code, 200)
                    locked = client.get("/thermal/deck")
                    self.assertEqual(locked.status_code, 401)
                    client.post("/login", data={"password": "test-password"})
                    page = client.get("/")
                    self.assertEqual(page.status_code, 200)
                    html = page.text
                    self.assertLess(
                        html.index("Show thermal feed"),
                        html.index('class="resolution"'),
                    )
                    with client.stream("GET", "/thermal/deck") as response:
                        self.assertEqual(response.status_code, 200)
                        chunk = next(response.iter_bytes())
                    self.assertIn(JPEG, chunk)

    def test_button_stays_off_without_the_flag(self) -> None:
        from fastapi.testclient import TestClient

        from app.main import app

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            os.environ["CONFIG_PATH"] = str(_config(root, False))
            get_settings.cache_clear()
            with (
                patch.object(Camera, "start", lambda self: None),
                patch.object(Camera, "stop", lambda self: None),
                TestClient(app, client=("127.0.0.1", 50000)) as client,
            ):
                missing = client.post("/thermal", content=JPEG)
                self.assertEqual(missing.status_code, 404)
                signed = client.post(
                    "/login",
                    data={"password": "test-password"},
                    follow_redirects=False,
                )
                self.assertEqual(signed.status_code, 303)
                page = client.get("/")
                self.assertIn("data-thermal-toggle hidden", page.text)


if __name__ == "__main__":
    unittest.main()
