"""cctv start, camera, reboot, shutdown, link, and ui."""

from __future__ import annotations

import argparse
import sys
from typing import Literal

from cctv_ui.service import format_link, format_start, power_action, ui_command


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cctv")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("start", help="start the camera server and a Cloudflare quick tunnel")
    sub.add_parser("camera", help="show the camera on the panel")
    sub.add_parser("reboot", help="restart the Pi")
    sub.add_parser("shutdown", help="shut down the Pi")
    sub.add_parser("link", help="print the public stream link and password")
    sub.add_parser("ui", help="run the touchscreen UI")
    args = parser.parse_args(argv)
    handlers = {
        "start": cmd_start,
        "camera": cmd_camera,
        "reboot": cmd_reboot,
        "shutdown": cmd_shutdown,
        "link": cmd_link,
        "ui": cmd_ui,
    }
    return handlers[args.command]()


def cmd_start() -> int:
    code, text = format_start()
    sys.stdout.write(text)
    return code


def cmd_camera() -> int:
    reply = ui_command("camera")
    if reply is None:
        print("cctv ui is not running")
        return 1
    if reply != "ok":
        print(reply)
        return 1
    return 0


def cmd_reboot() -> int:
    return _power("reboot")


def cmd_shutdown() -> int:
    return _power("poweroff")


def cmd_link() -> int:
    text = format_link()
    sys.stdout.write(text)
    return 1 if "not running" in text else 0


def cmd_ui() -> int:
    from cctv_ui.kiosk import run

    return run()


def _power(action: Literal["reboot", "poweroff"]) -> int:
    err = power_action(action)
    if err:
        print(err)
        return 1
    return 0
