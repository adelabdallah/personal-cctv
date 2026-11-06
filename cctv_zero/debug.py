"""SSH sessions, internet, and the facts line for the debug screen."""

from __future__ import annotations

import re
import socket
import subprocess
from pathlib import Path
from shutil import disk_usage

_SSH_PROC = re.compile(r"sshd:\s+(\S+?)(?:@|\s|$)")
_battery_pin = None
_battery_failed = False


def hostname() -> str:
    return socket.gethostname() or "pi"


def wifi_ssid() -> str:
    try:
        out = subprocess.check_output(
            ["iwgetid", "-r"],
            text=True,
            timeout=1,
            stderr=subprocess.DEVNULL,
        ).strip()
        return out
    except (OSError, subprocess.SubprocessError):
        return ""


def internet_up() -> bool:
    """One short ping. A miss means the debug line says down."""
    try:
        completed = subprocess.run(
            ["ping", "-c", "1", "-W", "2", "1.1.1.1"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def disk_free_label() -> str:
    try:
        free = disk_usage("/").free
    except OSError:
        return "?"
    gib = free / (1024**3)
    if gib >= 1:
        return f"{gib:.1f}G"
    return f"{free / (1024**2):.0f}M"


def cpu_temp_c() -> str:
    path = Path("/sys/class/thermal/thermal_zone0/temp")
    try:
        milli = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return "?C"
    return f"{milli // 1000}C"


def close_battery() -> None:
    global _battery_pin
    pin = _battery_pin
    _battery_pin = None
    if pin is None:
        return
    try:
        pin.close()
    except Exception:
        pass


def battery_label() -> str:
    """Zero LiPo pulls GPIO 4 low under about 3.4V."""
    global _battery_pin, _battery_failed
    if _battery_failed:
        return "batt ?"
    if _battery_pin is None:
        try:
            from gpiozero import DigitalInputDevice

            _battery_pin = DigitalInputDevice(4, pull_up=True)
        except Exception:
            _battery_failed = True
            return "batt ?"
    try:
        return "batt low" if _battery_pin.is_active else "batt ok"
    except Exception:
        return "batt ?"


def facts_line(*, ssid: str, online: bool, disk: str, temp: str, battery: str) -> str:
    name = ssid or hostname()
    state = "up" if online else "down"
    return f"{name} {state}  {disk}  {battery}  {temp}"


def _command(argv: list[str]) -> str:
    return subprocess.check_output(argv, text=True, timeout=2, stderr=subprocess.DEVNULL)


def _peer_host(addr: str) -> str:
    addr = addr.strip()
    if addr.startswith("[") and "]" in addr:
        return addr[1 : addr.index("]")]
    if addr.count(":") >= 1 and addr.rsplit(":", 1)[-1].isdigit():
        return addr.rsplit(":", 1)[0]
    return addr


def _is_local(host: str) -> bool:
    return host.strip("[]") in {"", "*", "127.0.0.1", "::1", "localhost", ":0", "0.0.0.0"}


def ssh_sessions(run=_command) -> list[str]:
    """Active remote SSH peers."""
    rows: list[str] = []
    seen: set[str] = set()

    def add(label: str) -> None:
        label = " ".join(label.split())
        if not label or label in seen:
            return
        seen.add(label)
        rows.append(label)

    try:
        for line in run(["who"]).splitlines():
            if "(" not in line or ")" not in line:
                continue
            host = line[line.rfind("(") + 1 : line.rfind(")")]
            user = line.split(maxsplit=1)[0]
            if host and not _is_local(host):
                add(f"{user}  {host}")
    except (OSError, subprocess.SubprocessError, IndexError):
        pass

    try:
        for line in run(["ss", "-tn"]).splitlines():
            if "ESTAB" not in line.upper() or ":22" not in line:
                continue
            parts = line.split()
            if len(parts) < 5:
                continue
            peer = _peer_host(parts[4])
            if peer and not _is_local(peer):
                add(f"ssh  {peer}")
    except (OSError, subprocess.SubprocessError):
        pass

    try:
        for line in run(["ps", "-eo", "args="]).splitlines():
            if not line.startswith("sshd:"):
                continue
            match = _SSH_PROC.search(line)
            if match is None:
                continue
            add(f"{match.group(1)}  ssh")
    except (OSError, subprocess.SubprocessError):
        pass

    return rows
