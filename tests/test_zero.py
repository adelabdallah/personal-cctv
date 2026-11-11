"""Pi Zero menu, USB profile, and MJPEG capture."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.camera import Camera, take_jpeg, v4l2_command
from app.config import CameraConfig, load_camera_configs
from cctv_ui.cli import cmd_boot, main
from cctv_ui.service import (
    _csi_from_sysfs,
    config_file,
    is_original_zero_w,
    mark_stream_wanted,
    node_camera_id,
    pull_latest,
    start_stream,
    stream_should_run,
    wanted_path,
    write_config,
)
from cctv_zero.backlight import idle_blank
from cctv_zero.debug import facts_line, ssh_sessions
from cctv_zero.menu import Menu, home_label, press, rows
from cctv_zero.ui import App


class JpegStreamTest(unittest.TestCase):
    def test_take_jpeg_splits_a_stream_and_keeps_a_partial_frame(self) -> None:
        buf = bytearray(b"xx\xff\xd8\x00\xff\xd9YY\xff\xd8\xff\xd9")
        self.assertEqual(take_jpeg(buf), b"\xff\xd8\x00\xff\xd9")
        self.assertEqual(take_jpeg(buf), b"\xff\xd8\xff\xd9")
        self.assertEqual(buf, bytearray())

        partial = bytearray(b"\xff\xd8\x01")
        self.assertIsNone(take_jpeg(partial))
        partial.extend(b"\xff\xd9")
        self.assertEqual(take_jpeg(partial), b"\xff\xd8\x01\xff\xd9")

    def test_v4l2_command_copies_mjpeg_or_encodes(self) -> None:
        copied = v4l2_command("/dev/video0", 640, 480, 5, mjpeg_copy=True, jpeg_quality=65)
        self.assertIn("mjpeg", copied)
        self.assertIn("copy", copied)
        self.assertIn("640x480", copied)
        self.assertNotIn("fps=5", copied)

        encoded = v4l2_command("/dev/video1", 320, 240, 2, mjpeg_copy=False, jpeg_quality=65)
        self.assertIn("fps=2", encoded)
        self.assertIn("320x240", encoded)
        self.assertNotIn("copy", encoded)

    def test_slow_mode_drops_v4l2_capture_to_one_frame_a_second(self) -> None:
        camera = Camera(
            CameraConfig(id="cam", name="HD Webcam", backend="v4l2", fps=5, mjpeg_copy=True)
        )
        self.assertEqual(camera._v4l2_target_fps(), 5)
        camera.set_slow(True)
        self.assertEqual(camera._v4l2_target_fps(), 1)
        self.assertEqual(camera.frame_interval(), 1.0)


class ProfileTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self._prev = os.environ.get("CCTV_STATE_DIR")
        os.environ["CCTV_STATE_DIR"] = self.tmp.name

    def tearDown(self) -> None:
        if self._prev is None:
            os.environ.pop("CCTV_STATE_DIR", None)
        else:
            os.environ["CCTV_STATE_DIR"] = self._prev
        self.tmp.cleanup()

    def test_model_check_skips_zero_2(self) -> None:
        self.assertTrue(is_original_zero_w("Raspberry Pi Zero W Rev 1.1"))
        self.assertFalse(is_original_zero_w("Raspberry Pi Zero 2 W"))
        self.assertFalse(is_original_zero_w("Raspberry Pi 4 Model B"))

    def test_zero_w_copies_mjpeg_at_five_fps(self) -> None:
        camera = {"name": "HD Webcam", "backend": "opencv", "device_index": 0}
        with (
            patch("cctv_ui.service.read_model", return_value="Raspberry Pi Zero W Rev 1.1"),
            patch("app.camera.device_offers_mjpeg", return_value=True),
        ):
            write_config(camera)
        text = config_file().read_text(encoding="utf-8")
        self.assertIn("backend: v4l2", text)
        self.assertIn("width: 640", text)
        self.assertIn("height: 480", text)
        self.assertIn("fps: 5", text)
        self.assertIn("slow: false", text)
        self.assertIn("mjpeg_copy: true", text)
        loaded = load_camera_configs(config_file())
        self.assertEqual(loaded[0].backend, "v4l2")
        self.assertEqual(loaded[0].fps, 5)
        self.assertFalse(loaded[0].slow)
        self.assertTrue(loaded[0].mjpeg_copy)

    def test_zero_w_encodes_when_the_webcam_has_no_mjpeg(self) -> None:
        camera = {"name": "USB camera", "backend": "opencv", "device_index": 1}
        with (
            patch("cctv_ui.service.read_model", return_value="Raspberry Pi Zero W Rev 1.1"),
            patch("app.camera.device_offers_mjpeg", return_value=False),
        ):
            write_config(camera)
        text = config_file().read_text(encoding="utf-8")
        self.assertIn("width: 320", text)
        self.assertIn("height: 240", text)
        self.assertIn("fps: 2", text)
        self.assertIn("mjpeg_copy: false", text)

    def test_zero_w_csi_streams_at_five_fps(self) -> None:
        camera = {"name": "imx219", "backend": "picamera2", "device_index": 0}
        with patch("cctv_ui.service.read_model", return_value="Raspberry Pi Zero W Rev 1.1"):
            write_config(camera)
        text = config_file().read_text(encoding="utf-8")
        self.assertIn("backend: picamera2", text)
        self.assertIn("width: 640", text)
        self.assertIn("height: 480", text)
        self.assertIn("fps: 5", text)
        self.assertNotIn("mjpeg_copy", text)
        loaded = load_camera_configs(config_file())
        self.assertEqual(loaded[0].backend, "picamera2")
        self.assertEqual(loaded[0].fps, 5)

    def test_other_boards_keep_the_csi_profile(self) -> None:
        camera = {"name": "imx219", "backend": "picamera2", "device_index": 0}
        with patch("cctv_ui.service.read_model", return_value="Raspberry Pi 4 Model B"):
            write_config(camera)
        text = config_file().read_text(encoding="utf-8")
        self.assertIn("backend: picamera2", text)
        self.assertIn("fps: 8", text)
        self.assertNotIn("slow:", text)

    def test_other_boards_keep_the_opencv_profile(self) -> None:
        camera = {"name": "HD Webcam", "backend": "opencv", "device_index": 0}
        with (
            patch("cctv_ui.service.read_model", return_value="Raspberry Pi 4 Model B"),
            patch("app.camera.device_offers_mjpeg", return_value=True),
        ):
            write_config(camera)
        text = config_file().read_text(encoding="utf-8")
        self.assertIn("backend: opencv", text)
        self.assertIn("fps: 8", text)
        self.assertNotIn("slow:", text)
        self.assertNotIn("mjpeg_copy", text)

    def test_csi_list_reads_the_sensor_name_without_opening_it(self) -> None:
        root = Path(self.tmp.name) / "i2c"
        (root / "i2c-11").mkdir(parents=True)
        (root / "i2c-11" / "name").write_text("bcm2835 (i2c@7e205000)\n", encoding="utf-8")
        sensor = root / "10-0010"
        sensor.mkdir()
        (sensor / "name").write_text("imx219\n", encoding="utf-8")
        self.assertEqual(
            _csi_from_sysfs(root),
            [{"name": "imx219", "backend": "picamera2", "device_index": 0}],
        )

    def test_stream_id_is_the_hostname_so_it_does_not_hide_the_host_camera(self) -> None:
        with patch("cctv_ui.service.socket.gethostname", return_value="zero.tail419e62.ts.net"):
            self.assertEqual(node_camera_id(), "zero")
            write_config({"name": "imx219", "backend": "picamera2", "device_index": 0})
        text = config_file().read_text(encoding="utf-8")
        self.assertIn("id: zero", text)
        self.assertNotIn("id: camera", text)

    def test_a_selected_zero_camera_comes_back_and_stop_keeps_it_off(self) -> None:
        with patch("cctv_ui.service.read_model", return_value="Raspberry Pi Zero W Rev 1.1"):
            self.assertFalse(stream_should_run())
            config_file().write_text(
                "cameras:\n  - id: zero\n    name: imx219\n    device_index: 0\n",
                encoding="utf-8",
            )
            self.assertTrue(stream_should_run())
            mark_stream_wanted(False)
            self.assertFalse(stream_should_run())
            mark_stream_wanted(True)
            self.assertTrue(stream_should_run())
        wanted_path().unlink()
        with patch("cctv_ui.service.read_model", return_value="Raspberry Pi 4 Model B"):
            self.assertTrue(stream_should_run())

    def test_zero_boot_starts_the_first_camera_or_stays_on_the_menu(self) -> None:
        imx = {"name": "imx219", "backend": "picamera2", "device_index": 0}
        with (
            patch("cctv_ui.service.read_model", return_value="Raspberry Pi Zero W Rev 1.1"),
            patch("cctv_ui.supervisor.wait_for_route", return_value=True),
            patch("cctv_ui.service.pull_latest") as pull,
            patch("cctv_ui.service.choose_camera", return_value=(imx, None)),
            patch("cctv_ui.cli.cmd_start", return_value=0) as start,
        ):
            self.assertEqual(cmd_boot(), 0)
        pull.assert_called_once_with()
        start.assert_called_once_with(False, "imx219")

        with (
            patch("cctv_ui.service.read_model", return_value="Raspberry Pi Zero W Rev 1.1"),
            patch("cctv_ui.supervisor.wait_for_route", return_value=True),
            patch("cctv_ui.service.pull_latest") as pull,
            patch("cctv_ui.service.choose_camera", return_value=(None, None)),
            patch("cctv_ui.service.mark_stream_wanted") as mark,
            patch("cctv_ui.cli.cmd_start", return_value=0) as start,
        ):
            self.assertEqual(cmd_boot(), 0)
        pull.assert_called_once_with()
        start.assert_not_called()
        mark.assert_called_once_with(False)

    def test_boot_fetch_updates_a_new_tree_and_keeps_it_when_fetch_fails(self) -> None:
        calls: list[tuple[str, ...]] = []

        def run(argv: list[str], **kwargs: object) -> object:
            calls.append(tuple(argv[3:]))
            name = argv[3]
            if name == "remote":
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
            if name == "fetch":
                return subprocess.CompletedProcess(argv, 1, stdout="", stderr="denied")
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        with (
            tempfile.TemporaryDirectory() as raw,
            patch.dict(os.environ, {"CCTV_ROOT": raw}),
            patch("cctv_ui.service.shutil.which", return_value="/usr/bin/git"),
            patch("cctv_ui.service.subprocess.run", side_effect=run),
        ):
            pull_latest()
        self.assertIn(("init",), calls)
        self.assertIn(("remote", "add", "origin", "git@github.com:adelabdallah/personal-cctv.git"), calls)
        self.assertIn(("fetch", "origin", "main"), calls)
        self.assertFalse(any(call[:1] == ("checkout",) for call in calls))

    def test_zero_stream_runs_behind_wifi_and_tailscale(self) -> None:
        camera = {"name": "imx219", "backend": "picamera2", "device_index": 0}
        with (
            patch("cctv_ui.service.read_model", return_value="Raspberry Pi Zero W Rev 1.1"),
            patch("cctv_ui.service.choose_camera", return_value=(camera, None)),
            patch("cctv_ui.service.running_healthy", return_value=False),
            patch("cctv_ui.service.stop_stream"),
            patch("cctv_ui.service.ensure_env", return_value={"PORT": "8000"}),
            patch("cctv_ui.service._wait_health", return_value=None),
            patch("cctv_ui.service._wait_public_url", return_value=None),
            patch("cctv_ui.service.subprocess.Popen") as popen,
            patch("cctv_ui.service.os.nice") as nice,
        ):
            popen.return_value.pid = 1
            self.assertIsNone(start_stream())
            preexec = popen.call_args.kwargs["preexec_fn"]
            preexec()
            nice.assert_called_once_with(10)

        with (
            patch("cctv_ui.service.read_model", return_value="Raspberry Pi 4 Model B"),
            patch("cctv_ui.service.choose_camera", return_value=(camera, None)),
            patch("cctv_ui.service.running_healthy", return_value=False),
            patch("cctv_ui.service.stop_stream"),
            patch("cctv_ui.service.ensure_env", return_value={"PORT": "8000"}),
            patch("cctv_ui.service._wait_health", return_value=None),
            patch("cctv_ui.service._wait_public_url", return_value=None),
            patch("cctv_ui.service.subprocess.Popen") as popen,
        ):
            popen.return_value.pid = 1
            self.assertIsNone(start_stream())
        self.assertIsNone(popen.call_args.kwargs["preexec_fn"])


class MenuTest(unittest.TestCase):
    def test_selecting_a_camera_returns_home_and_marks_starting(self) -> None:
        opened = press(Menu(), "select")
        self.assertEqual(opened.menu.page, "cameras")
        started = press(Menu(page="cameras", cameras=("HD Webcam",)), "select")
        self.assertEqual(started.effect, "start:HD Webcam")
        self.assertEqual(started.menu.page, "home")
        self.assertEqual(started.menu.phase, "starting")
        self.assertEqual(home_label("starting"), "CCTV (starting)")
        self.assertEqual(rows(Menu(phase="running"))[0], "CCTV (running)")

    def test_stop_clears_the_running_label(self) -> None:
        menu = Menu(page="cameras", phase="running", cameras=("HD Webcam",), selected=1)
        self.assertEqual(rows(menu), ("HD Webcam", "Stop"))
        stopped = press(menu, "select")
        self.assertEqual(stopped.effect, "stop")
        self.assertEqual(stopped.menu.phase, "idle")
        self.assertEqual(rows(stopped.menu)[0], "CCTV")

    def test_debug_and_power_off(self) -> None:
        debug = press(press(Menu(), "down").menu, "select")
        self.assertEqual(debug.menu.page, "debug")
        home = press(debug.menu, "back")
        self.assertEqual(home.menu.page, "home")
        self.assertEqual(home.menu.selected, 1)

        power = press(Menu(selected=2), "select")
        self.assertEqual(power.menu.page, "power")
        self.assertEqual(power.menu.selected, 1)
        confirmed = press(Menu(page="power", selected=0), "select")
        self.assertEqual(confirmed.effect, "poweroff")

    def test_cctv_ui_on_a_zero_w_opens_the_button_menu(self) -> None:
        with (
            patch("cctv_ui.service.read_model", return_value="Raspberry Pi Zero W Rev 1.1"),
            patch("cctv_zero.ui.run", return_value=0) as ui_run,
        ):
            code = main(["ui"])
        self.assertEqual(code, 0)
        ui_run.assert_called_once()


class DebugTextTest(unittest.TestCase):
    def test_facts_line_names_the_network(self) -> None:
        text = facts_line(ssid="Guesto", online=True, disk="25.0G", temp="48C", battery="batt ok")
        self.assertEqual(text, "Guesto up  25.0G  batt ok  48C")
        down = facts_line(ssid="", online=False, disk="?", temp="?C", battery="batt ?")
        self.assertIn("down", down)

    def test_ssh_sessions_list_remote_peers(self) -> None:
        def run(argv: list[str]) -> str:
            if argv == ["who"]:
                return "adel pts/0 2026-11-06 08:00 (100.104.214.43)\n"
            if argv == ["ss", "-tn"]:
                return "ESTAB 0 0 10.0.0.8:22 100.9.9.9:51234\n"
            if argv == ["ps", "-eo", "args="]:
                return "sshd: adel [priv]\n"
            raise AssertionError(argv)

        rows = ssh_sessions(run)
        self.assertIn("adel 100.104.214.43", rows)
        self.assertIn("ssh 100.9.9.9", rows)
        self.assertIn("adel ssh", rows)


class _Light:
    def __init__(self) -> None:
        self.on = True
        self.calls: list[bool] = []

    def set(self, on: bool) -> bool:
        self.calls.append(on)
        self.on = on
        return True

    def close(self) -> None:
        return None


class BacklightIdleTest(unittest.TestCase):
    def test_two_quiet_minutes_blanks_and_the_next_press_only_wakes(self) -> None:
        self.assertFalse(idle_blank(0, 119, False))
        self.assertTrue(idle_blank(0, 120, False))
        self.assertFalse(idle_blank(0, 120, True))

        from pathlib import Path

        light = _Light()
        app = App(Path("fb"), (320, 240), backlight=light)  # type: ignore[arg-type]
        app._ready_at = 0
        app._last_input = 0
        app.poll_blank(now=120)
        self.assertTrue(app._blanked)
        self.assertEqual(light.calls, [False])

        app.on_down("select")
        app.on_up("select")
        self.assertFalse(app._blanked)
        self.assertTrue(light.on)
        self.assertEqual(app.menu.page, "home")
        self.assertEqual(app.menu.selected, 0)

        app.on_down("down")
        app.on_up("down")
        self.assertEqual(app.menu.selected, 1)
