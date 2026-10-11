"""Health-check decisions for the Pi supervisor."""

from __future__ import annotations

import os
import tempfile
import unittest

from cctv_ui.supervisor import (
    Config,
    Snapshot,
    config_path,
    decide,
    failures,
    gateway_from_route_table,
    load_config,
    run_once,
)


def _down() -> Snapshot:
    return Snapshot(
        uptime_s=1000,
        route_ok=False,
        ping_gateway_ok=False,
        ping_public_ok=False,
        tailscale_ok=False,
        stream_ok=False,
        tunnel_ok=False,
    )


def _up() -> Snapshot:
    return Snapshot(
        uptime_s=1000,
        route_ok=True,
        ping_gateway_ok=True,
        ping_public_ok=True,
        tailscale_ok=True,
        stream_ok=True,
        tunnel_ok=True,
    )


class DecideTest(unittest.TestCase):
    def test_healthy_clears_an_open_failure(self) -> None:
        state = {"unhealthy_since": 10, "repaired_at": 20, "reboots": [50]}
        action, saved, problem = decide(100, _up(), state, Config())
        self.assertEqual(action, "ok")
        self.assertEqual(problem, ())
        self.assertIsNone(saved["unhealthy_since"])
        self.assertIsNone(saved["repaired_at"])
        self.assertEqual(saved["reboots"], [50])

    def test_boot_grace_ignores_failures(self) -> None:
        snapshot = Snapshot(60, False, False, False, False, False, False)
        action, saved, _problem = decide(1000, snapshot, {}, Config(boot_grace_s=180))
        self.assertEqual(action, "ok")
        self.assertIsNone(saved["unhealthy_since"])

    def test_short_failure_waits_then_repairs_then_reboots(self) -> None:
        config = Config(boot_grace_s=0, failure_window_s=10, repair_wait_s=10, max_reboots_per_hour=6)
        action, state, problem = decide(0, _down(), {}, config)
        self.assertEqual(action, "wait")
        self.assertIn("route", problem)
        action, state, _problem = decide(10, _down(), state, config)
        self.assertEqual(action, "repair")
        action, state, _problem = decide(15, _down(), state, config)
        self.assertEqual(action, "wait")
        action, state, _problem = decide(20, _down(), state, config)
        self.assertEqual(action, "reboot")
        self.assertEqual(state["reboots"], [20])

    def test_reboot_cap_repairs_instead_of_rebooting(self) -> None:
        config = Config(boot_grace_s=0, failure_window_s=10, repair_wait_s=10, max_reboots_per_hour=2)
        state = {"unhealthy_since": 0, "repaired_at": 10, "reboots": [50, 80]}
        action, saved, _problem = decide(100, _down(), state, config)
        self.assertEqual(action, "hold")
        self.assertEqual(saved["reboots"], [50, 80])

    def test_disabled_checks_are_ignored(self) -> None:
        config = Config(
            check_route=False,
            check_ping_gateway=False,
            check_ping_public=False,
            check_tailscale=False,
            check_stream=True,
            check_tunnel=False,
        )
        snapshot = Snapshot(1000, False, False, False, False, True, False)
        self.assertEqual(failures(snapshot, config), ())

    def test_gateway_hex_is_little_endian(self) -> None:
        text = "Iface Destination Gateway Flags\nwlan0 00000000 0101A8C0 0003\n"
        self.assertEqual(gateway_from_route_table(text), "192.168.1.1")
        self.assertIsNone(gateway_from_route_table("Iface Destination Gateway\n"))


class RunOnceTest(unittest.TestCase):
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

    def test_config_file_is_created_with_defaults(self) -> None:
        config = load_config()
        self.assertEqual(config.failure_window_s, 300)
        self.assertTrue(config.check_stream)
        self.assertIn("check_tunnel=yes", config_path().read_text(encoding="utf-8"))

    def test_custom_window_is_read_from_the_config_file(self) -> None:
        config_path().write_text("failure_window_s=12\ncheck_tailscale=no\n", encoding="utf-8")
        config = load_config()
        self.assertEqual(config.failure_window_s, 12)
        self.assertFalse(config.check_tailscale)
        self.assertTrue(config.check_route)

    def test_run_once_repairs_without_rebooting_the_machine(self) -> None:
        config_path().write_text(
            "boot_grace_s=0\nfailure_window_s=10\nrepair_wait_s=10\n",
            encoding="utf-8",
        )
        calls: list[str] = []

        def repair(_problem: tuple[str, ...]) -> None:
            calls.append("repair")

        def reboot() -> None:
            calls.append("reboot")

        self.assertEqual(run_once(now=0, probe=lambda _config: _down(), repair=repair, reboot=reboot), "wait")
        self.assertEqual(calls, [])
        self.assertEqual(run_once(now=10, probe=lambda _config: _down(), repair=repair, reboot=reboot), "repair")
        self.assertEqual(calls, ["repair"])
        self.assertEqual(run_once(now=20, probe=lambda _config: _down(), repair=repair, reboot=reboot), "reboot")
        self.assertEqual(calls, ["repair", "reboot"])
        self.assertEqual(run_once(now=30, probe=lambda _config: _up(), repair=repair, reboot=reboot), "ok")


if __name__ == "__main__":
    unittest.main()
