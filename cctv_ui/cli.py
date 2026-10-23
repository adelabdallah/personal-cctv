"""cctv start, camera, reboot, shutdown, link, ui, boot, and supervise."""

from __future__ import annotations

import argparse
import sys
from typing import Literal

from cctv_ui.service import format_link, format_start, power_action, ui_command


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cctv")
    sub = parser.add_subparsers(dest="command", required=True)
    start = sub.add_parser("start", help="start the camera server and publish it on Tailscale")
    start.add_argument(
        "--takeover",
        action="store_true",
        help="become the host when another device is already hosting",
    )
    sub.add_parser("camera", help="show the camera on the panel")
    sub.add_parser("reboot", help="restart the Pi")
    sub.add_parser("shutdown", help="shut down the Pi")
    sub.add_parser("link", help="print the public stream link and password")
    sub.add_parser("ui", help="run the touchscreen UI")
    sub.add_parser("boot", help="after boot, wait for a route and start the stream")
    sub.add_parser("supervise", help="repair or reboot if the network or stream stays down")
    args = parser.parse_args(argv)
    handlers = {
        "camera": cmd_camera,
        "reboot": cmd_reboot,
        "shutdown": cmd_shutdown,
        "link": cmd_link,
        "ui": cmd_ui,
        "boot": cmd_boot,
        "supervise": cmd_supervise,
    }
    if args.command == "start":
        return cmd_start(args.takeover)
    return handlers[args.command]()


def cmd_start(takeover: bool) -> int:
    code, text = format_start(takeover=takeover)
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


def cmd_boot() -> int:
    from cctv_ui.supervisor import wait_for_route

    wait_for_route()
    return cmd_start(False)


def cmd_supervise() -> int:
    from cctv_ui.supervisor import run_once

    run_once()
    return 0


def _power(action: Literal["reboot", "poweroff"]) -> int:
    err = power_action(action)
    if err:
        print(err)
        return 1
    return 0
