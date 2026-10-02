"""Caller-visible behaviour for the panel commands and the touch mapping."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from cctv_ui.cli import main
from cctv_ui.display import kmsdrm_device_index, logical_to_panel, panel_to_logical, set_backlight
from cctv_ui.kiosk import BLANK_AFTER_S, UiServer, UiState, _mark_input, _maybe_blank, home_buttons, read_cpu_temp_c
from cctv_ui.service import (
    ensure_env,
    env_file,
    format_link,
    log_file,
    meta_file,
    parse_tunnel_url,
    ui_command,
    url_file,
    viewer_password,
)
from cctv_ui.theme import LOGICAL_H, LOGICAL_W


class StateDirTest(unittest.TestCase):
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

    def test_link_when_stopped(self) -> None:
        text = format_link()
        self.assertIn("not running", text)
        buf = StringIO()
        with redirect_stdout(buf):
            code = main(["link"])
        self.assertEqual(code, 1)
        self.assertIn("not running", buf.getvalue())

    def test_link_prints_password_and_url(self) -> None:
        meta_file().write_text(
            json.dumps({"app_pid": os.getpid(), "port": "8000"}) + "\n",
            encoding="utf-8",
        )
        url_file().write_text("https://yard-cam.trycloudflare.com\n", encoding="utf-8")
        text = format_link()
        self.assertIn("https://yard-cam.trycloudflare.com", text)
        self.assertIn(viewer_password(), text)
        self.assertEqual(viewer_password(), "pass123")
        buf = StringIO()
        with redirect_stdout(buf):
            code = main(["link"])
        self.assertEqual(code, 0)
        self.assertIn("pass123", buf.getvalue())

    def test_link_replaces_api_host_when_tunnel_url_appears(self) -> None:
        log_file().write_text(
            'failed to request quick Tunnel: Post "https://api.trycloudflare.com/tunnel"\n'
            "Visit it at https://picks-antenna-dare-athletics.trycloudflare.com\n",
            encoding="utf-8",
        )
        meta_file().write_text(
            json.dumps({"app_pid": os.getpid(), "tunnel_log_offset": 0}) + "\n",
            encoding="utf-8",
        )
        url_file().write_text("https://api.trycloudflare.com\n", encoding="utf-8")
        text = format_link()
        self.assertIn("https://picks-antenna-dare-athletics.trycloudflare.com", text)
        self.assertNotIn("api.trycloudflare.com", text)
        self.assertEqual(
            url_file().read_text(encoding="utf-8").strip(),
            "https://picks-antenna-dare-athletics.trycloudflare.com",
        )

    def test_link_waits_when_only_the_api_host_is_logged(self) -> None:
        log_file().write_text(
            'Post "https://api.trycloudflare.com/tunnel": server misbehaving\n',
            encoding="utf-8",
        )
        meta_file().write_text(
            json.dumps({"app_pid": os.getpid(), "tunnel_log_offset": 0}) + "\n",
            encoding="utf-8",
        )
        url_file().write_text("https://api.trycloudflare.com\n", encoding="utf-8")
        text = format_link()
        self.assertIn("still coming up", text)
        self.assertNotIn("api.trycloudflare.com", text)

    def test_password_stays_fixed_and_secret_is_kept(self) -> None:
        first = ensure_env()
        second = ensure_env()
        self.assertEqual(first["CCTV_PASSWORD"], "pass123")
        self.assertEqual(second["SESSION_SECRET"], first["SESSION_SECRET"])
        self.assertIn("CCTV_PASSWORD=pass123", env_file().read_text(encoding="utf-8"))

    def test_ui_socket_roundtrip(self) -> None:
        server = UiServer(lambda command: "ok" if command == "camera" else "err")
        server.start()
        try:
            self.assertEqual(ui_command("camera", timeout=2), "ok")
            self.assertEqual(ui_command("nope", timeout=2), "err")
        finally:
            server.close()


class MappingTest(unittest.TestCase):
    def test_parse_tunnel_url_ignores_argotunnel(self) -> None:
        text = "connect https://region1.v2.argotunnel.com then https://abc-def.trycloudflare.com"
        self.assertEqual(parse_tunnel_url(text), "https://abc-def.trycloudflare.com")
        self.assertIsNone(parse_tunnel_url("https://region1.v2.argotunnel.com"))
        self.assertIsNone(parse_tunnel_url('Post "https://api.trycloudflare.com/tunnel"'))
        later = (
            'Post "https://api.trycloudflare.com/tunnel"\n'
            "https://one-two-three.trycloudflare.com\n"
            "https://four-five-six.trycloudflare.com\n"
        )
        self.assertEqual(parse_tunnel_url(later), "https://four-five-six.trycloudflare.com")

    def test_panel_mapping_roundtrip(self) -> None:
        for rotate in (90, 270, 0):
            for lx in range(0, LOGICAL_W, 40):
                for ly in range(0, LOGICAL_H, 40):
                    px, py = logical_to_panel(lx, ly, rotate)
                    self.assertEqual(panel_to_logical(px, py, rotate), (lx, ly))

    def test_kms_prefers_spi_over_hdmi(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._status(root, "card0-SPI-1", "connected")
            self._status(root, "card1-HDMI-A-1", "connected")
            self._status(root, "card1-Writeback-1", "connected")
            self.assertEqual(kmsdrm_device_index(root), "0")

    def test_home_buttons_cover_four_actions(self) -> None:
        buttons = home_buttons()
        self.assertEqual([button.action for button in buttons], ["start", "camera", "reboot", "shutdown"])
        self.assertEqual(buttons[0].label, "Start Stream")
        running = home_buttons(running=True)
        self.assertEqual(running[0].label, "Restart Stream")
        self.assertEqual(running[0].action, "start")
        self.assertEqual(running[0].rect, buttons[0].rect)
        rects = [button.rect for button in buttons]
        for x, y, w, h in rects:
            self.assertGreater(w, 80)
            self.assertGreater(h, 60)
            self.assertGreaterEqual(x, 0)
            self.assertGreaterEqual(y, 0)
            self.assertLessEqual(x + w, LOGICAL_W)
            self.assertLessEqual(y + h, LOGICAL_H)
        for index, rect in enumerate(rects):
            for other in rects[index + 1 :]:
                self.assertFalse(_overlaps(rect, other))

    def test_backlight_blanks_after_ten_minutes_and_wakes(self) -> None:
        self.assertEqual(BLANK_AFTER_S, 600)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "brightness"
            path.write_text("1\n", encoding="utf-8")
            state = UiState(last_input=0.0)
            _maybe_blank(state, 599, path)
            self.assertFalse(state.blanked)
            self.assertEqual(path.read_text(encoding="utf-8"), "1\n")
            _maybe_blank(state, 600, path)
            self.assertTrue(state.blanked)
            self.assertEqual(path.read_text(encoding="utf-8"), "0\n")
            self.assertTrue(_mark_input(state, 601, path))
            self.assertFalse(state.blanked)
            self.assertEqual(path.read_text(encoding="utf-8"), "1\n")
            self.assertFalse(_mark_input(state, 602, path))
            missing = Path(tmp) / "missing" / "brightness"
            self.assertFalse(set_backlight(False, missing))

    def test_cpu_temp_millidegrees(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "temp"
            path.write_text("45321\n", encoding="utf-8")
            self.assertEqual(read_cpu_temp_c(path), 45.321)
            path.write_text("nope", encoding="utf-8")
            self.assertIsNone(read_cpu_temp_c(path))

    def _status(self, root: Path, name: str, status: str) -> None:
        folder = root / name
        folder.mkdir()
        (folder / "status").write_text(status + "\n", encoding="utf-8")


def _overlaps(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> bool:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah


if __name__ == "__main__":
    unittest.main()
