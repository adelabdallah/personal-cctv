"""Host CPU temperature and battery for the viewer header."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

TEMP_TTL_S = 10.0
BATTERY_TTL_S = 60.0
JSONL_FRESH_S = 90.0
HOT_C = 70.0
LOW_BATTERY_PERCENT = 20.0

_SYS_TEMP = Path("/sys/class/thermal/thermal_zone0/temp")
_POWER_SUPPLY = Path("/sys/class/power_supply")
_BATTERY_LOG = (
    Path.home() / ".local" / "share" / "cyberdeck" / "battery-log.jsonl"
)
_VCGENCMD_TEMP = re.compile(r"temp=([0-9.]+)", re.I)
_PMSET_LINE = re.compile(r"(\d+)\s*%;\s*([^;]*)")

_lock = threading.Lock()
_temp_c: float | None = None
_temp_at = 0.0
_temp_ready = False
_battery: BatteryReading | None = None
_battery_at = 0.0
_battery_ready = False


@dataclass(frozen=True)
class BatteryReading:
    percent: float | None
    charging: bool | None
    source: str | None


@dataclass(frozen=True)
class HostStatus:
    temp_c: float | None
    battery_percent: float | None
    charging: bool | None
    battery_source: str | None

    def as_dict(self) -> dict[str, float | bool | str | None]:
        return {
            "temp_c": None if self.temp_c is None else round(self.temp_c, 1),
            "battery_percent": (
                None
                if self.battery_percent is None
                else round(self.battery_percent, 1)
            ),
            "charging": self.charging,
            "battery_source": self.battery_source,
        }


def parse_sysfs_temp(text: str) -> float | None:
    raw = text.strip()
    if not raw.isdigit():
        return None
    return int(raw) / 1000.0


def parse_vcgencmd_temp(text: str) -> float | None:
    match = _VCGENCMD_TEMP.search(text)
    if match is None:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def parse_pmset_battery(text: str) -> BatteryReading | None:
    for line in text.splitlines():
        match = _PMSET_LINE.search(line)
        if match is None:
            continue
        try:
            percent = float(match.group(1))
        except ValueError:
            continue
        status = match.group(2).strip().lower()
        charging = not status.startswith("discharg")
        return BatteryReading(percent=percent, charging=charging, source="os")
    return None


def _run(command: list[str], timeout: float = 1.5) -> str:
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return completed.stdout or ""


def _read_sysfs_temp() -> float | None:
    try:
        return parse_sysfs_temp(
            _SYS_TEMP.read_text(encoding="utf-8", errors="replace")
        )
    except OSError:
        return None


def _read_vcgencmd_temp() -> float | None:
    vcgencmd = shutil.which("vcgencmd")
    if not vcgencmd:
        return None
    return parse_vcgencmd_temp(_run([vcgencmd, "measure_temp"]))


def _read_cpu_temp() -> float | None:
    temp_c = _read_sysfs_temp()
    if temp_c is not None:
        return temp_c
    return _read_vcgencmd_temp()


def _read_sysfs_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _read_linux_battery() -> BatteryReading | None:
    if not _POWER_SUPPLY.is_dir():
        return None
    try:
        supplies = sorted(_POWER_SUPPLY.iterdir())
    except OSError:
        return None
    for supply in supplies:
        if _read_sysfs_file(supply / "type").lower() != "battery":
            continue
        raw_capacity = _read_sysfs_file(supply / "capacity")
        if not raw_capacity:
            continue
        try:
            percent = float(raw_capacity)
        except ValueError:
            continue
        status = _read_sysfs_file(supply / "status").lower()
        charging = status in {"charging", "full", "not charging"}
        return BatteryReading(percent=percent, charging=charging, source="os")
    return None


def _read_pmset_battery() -> BatteryReading | None:
    if sys.platform != "darwin":
        return None
    pmset = shutil.which("pmset")
    if not pmset:
        return None
    return parse_pmset_battery(_run([pmset, "-g", "batt"]))


def _read_ups_battery() -> BatteryReading | None:
    try:
        from cyberdeck.hardware.ups import read_ups
    except ImportError:
        return None
    try:
        shot = read_ups()
    except Exception:
        return None
    if getattr(shot, "missing", True):
        return None
    try:
        percent = float(shot.percent)
    except (TypeError, ValueError):
        return None
    return BatteryReading(
        percent=percent,
        charging=bool(getattr(shot, "charging", False)),
        source="ups",
    )


def _read_jsonl_battery(path: Path | None = None) -> BatteryReading | None:
    log_path = path if path is not None else _BATTERY_LOG
    try:
        text = log_path.read_text(encoding="utf-8")
    except OSError:
        return None
    now = time.time()
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except ValueError:
            continue
        if not isinstance(raw, dict):
            continue
        try:
            ts = float(raw["ts"])
            percent = float(raw["percent"])
        except (KeyError, TypeError, ValueError):
            continue
        if now - ts > JSONL_FRESH_S:
            return None
        return BatteryReading(
            percent=percent,
            charging=bool(raw.get("charging", False)),
            source="log",
        )
    return None


def _read_battery() -> BatteryReading:
    for reader in (
        _read_linux_battery,
        _read_pmset_battery,
        _read_ups_battery,
        _read_jsonl_battery,
    ):
        reading = reader()
        if reading is not None and reading.percent is not None:
            return reading
    return BatteryReading(percent=None, charging=None, source=None)


def read_host_status() -> HostStatus:
    global _temp_c, _temp_at, _temp_ready
    global _battery, _battery_at, _battery_ready
    now = time.monotonic()
    with _lock:
        need_temp = not _temp_ready or now - _temp_at >= TEMP_TTL_S
        need_battery = not _battery_ready or now - _battery_at >= BATTERY_TTL_S
    temp_c = _read_cpu_temp() if need_temp else None
    battery = _read_battery() if need_battery else None
    with _lock:
        if need_temp:
            _temp_c = temp_c
            _temp_at = time.monotonic()
            _temp_ready = True
        if need_battery:
            _battery = battery
            _battery_at = time.monotonic()
            _battery_ready = True
        reading = _battery or BatteryReading(None, None, None)
        return HostStatus(
            temp_c=_temp_c,
            battery_percent=reading.percent,
            charging=reading.charging,
            battery_source=reading.source,
        )
