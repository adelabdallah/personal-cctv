"""Reboot the Pi when the network or the stream stays down, and start the stream at boot."""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

from cctv_ui.service import format_start, power_action, public_url, running_healthy, state_dir

Action = Literal["ok", "wait", "repair", "reboot", "hold"]
REPAIR_SCRIPT = "/usr/local/sbin/cctv-repair-network"
HOUR_S = 3600.0

DEFAULT_CONFIG_TEXT = """\
# Copied on first use. Edit this file to change when the Pi reboots itself.
# Times are seconds.

boot_grace_s=180
failure_window_s=300
repair_wait_s=90
max_reboots_per_hour=6

check_route=yes
check_ping_gateway=yes
check_ping_public=yes
check_tailscale=yes
check_stream=yes
check_tunnel=yes

ping_public=1.1.1.1
interface=wlan0
"""


@dataclass(frozen=True)
class Config:
    boot_grace_s: float = 180
    failure_window_s: float = 300
    repair_wait_s: float = 90
    max_reboots_per_hour: int = 6
    check_route: bool = True
    check_ping_gateway: bool = True
    check_ping_public: bool = True
    check_tailscale: bool = True
    check_stream: bool = True
    check_tunnel: bool = True
    ping_public: str = "1.1.1.1"
    interface: str = "wlan0"


@dataclass(frozen=True)
class Snapshot:
    uptime_s: float
    route_ok: bool
    ping_gateway_ok: bool
    ping_public_ok: bool
    tailscale_ok: bool
    stream_ok: bool
    tunnel_ok: bool


def config_path() -> Path:
    return state_dir() / "supervisor.conf"


def state_path() -> Path:
    return state_dir() / "supervisor.json"


def load_config() -> Config:
    path = config_path()
    if not path.exists():
        path.write_text(DEFAULT_CONFIG_TEXT, encoding="utf-8")
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip()
    defaults = Config()
    return Config(
        boot_grace_s=_float(values, "boot_grace_s", defaults.boot_grace_s),
        failure_window_s=_float(values, "failure_window_s", defaults.failure_window_s),
        repair_wait_s=_float(values, "repair_wait_s", defaults.repair_wait_s),
        max_reboots_per_hour=_int(values, "max_reboots_per_hour", defaults.max_reboots_per_hour),
        check_route=_bool(values, "check_route", defaults.check_route),
        check_ping_gateway=_bool(values, "check_ping_gateway", defaults.check_ping_gateway),
        check_ping_public=_bool(values, "check_ping_public", defaults.check_ping_public),
        check_tailscale=_bool(values, "check_tailscale", defaults.check_tailscale),
        check_stream=_bool(values, "check_stream", defaults.check_stream),
        check_tunnel=_bool(values, "check_tunnel", defaults.check_tunnel),
        ping_public=values.get("ping_public") or defaults.ping_public,
        interface=values.get("interface") or defaults.interface,
    )


def failures(snapshot: Snapshot, config: Config) -> tuple[str, ...]:
    found: list[str] = []
    if config.check_route and not snapshot.route_ok:
        found.append("route")
    if config.check_ping_gateway and not snapshot.ping_gateway_ok:
        found.append("gateway")
    if config.check_ping_public and not snapshot.ping_public_ok:
        found.append("public")
    if config.check_tailscale and not snapshot.tailscale_ok:
        found.append("tailscale")
    if config.check_stream and not snapshot.stream_ok:
        found.append("stream")
    if config.check_tunnel and not snapshot.tunnel_ok:
        found.append("tunnel")
    return tuple(found)


def decide(now: float, snapshot: Snapshot, state: dict, config: Config) -> tuple[Action, dict, tuple[str, ...]]:
    """Choose ok, wait, repair, reboot, or hold. The returned state is what to save."""
    reboots = [stamp for stamp in _reboots(state) if now - stamp < HOUR_S]
    problem = failures(snapshot, config)
    if snapshot.uptime_s < config.boot_grace_s or not problem:
        return "ok", {"unhealthy_since": None, "repaired_at": None, "reboots": reboots}, problem

    since = state.get("unhealthy_since")
    if not isinstance(since, (int, float)):
        since = now
    saved = {"unhealthy_since": since, "repaired_at": state.get("repaired_at"), "reboots": reboots}
    if now - since < config.failure_window_s:
        return "wait", saved, problem

    repaired_at = saved["repaired_at"]
    if not isinstance(repaired_at, (int, float)):
        saved["repaired_at"] = now
        return "repair", saved, problem
    if now - repaired_at < config.repair_wait_s:
        return "wait", saved, problem
    if len(reboots) >= config.max_reboots_per_hour:
        saved["repaired_at"] = now
        return "hold", saved, problem
    saved["reboots"] = reboots + [now]
    return "reboot", saved, problem


def run_once(
    *,
    now: float | None = None,
    probe: Callable[[Config], Snapshot] | None = None,
    repair: Callable[[tuple[str, ...]], None] | None = None,
    reboot: Callable[[], None] | None = None,
) -> Action:
    moment = time.time() if now is None else now
    config = load_config()
    snapshot = (probe or collect_snapshot)(config)
    action, new_state, problem = decide(moment, snapshot, _read_state(), config)
    _write_state(new_state)
    detail = ", ".join(problem) if problem else "healthy"
    print(f"supervisor {action}: {detail}")
    if action in ("repair", "hold"):
        (repair or _repair)(problem)
    elif action == "reboot":
        (reboot or _reboot)()
    return action


def wait_for_route(timeout_s: float = 180) -> bool:
    """Block until a default route exists, or the timeout passes."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if default_gateway() is not None:
            return True
        time.sleep(2)
    return default_gateway() is not None


def collect_snapshot(config: Config) -> Snapshot:
    gateway = default_gateway()
    return Snapshot(
        uptime_s=read_uptime_s(),
        route_ok=gateway is not None,
        ping_gateway_ok=_ping(gateway) if config.check_ping_gateway and gateway else gateway is not None,
        ping_public_ok=_ping(config.ping_public) if config.check_ping_public else True,
        tailscale_ok=_tailscale_running() if config.check_tailscale else True,
        stream_ok=running_healthy(timeout=1.0) if config.check_stream else True,
        tunnel_ok=public_url() is not None if config.check_tunnel else True,
    )


def default_gateway() -> str | None:
    try:
        text = Path("/proc/net/route").read_text(encoding="utf-8")
    except OSError:
        return None
    return gateway_from_route_table(text)


def gateway_from_route_table(text: str) -> str | None:
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 3 or parts[1] != "00000000" or parts[2] == "00000000":
            continue
        try:
            octets = bytes.fromhex(parts[2])
        except ValueError:
            continue
        if len(octets) != 4:
            continue
        return ".".join(str(byte) for byte in octets[::-1])
    return None


def read_uptime_s() -> float:
    try:
        raw = Path("/proc/uptime").read_text(encoding="utf-8").split()[0]
        return float(raw)
    except (OSError, ValueError, IndexError):
        return 0.0


def _repair(problem: tuple[str, ...]) -> None:
    network = {"route", "gateway", "public", "tailscale"}
    if network.intersection(problem):
        subprocess.run(["sudo", "-n", REPAIR_SCRIPT], check=False)
    if {"stream", "tunnel"}.intersection(problem):
        _code, text = format_start()
        print(text, end="")


def _reboot() -> None:
    err = power_action("reboot")
    if err:
        print(err)


def _ping(host: str | None) -> bool:
    if not host:
        return False
    try:
        result = subprocess.run(
            ["ping", "-c", "1", "-W", "2", host],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _tailscale_running() -> bool:
    try:
        result = subprocess.run(
            ["tailscale", "status", "--json"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0:
        return False
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return False
    return data.get("BackendState") == "Running"


def _read_state() -> dict:
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(data: dict) -> None:
    state_path().write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _reboots(state: dict) -> list[float]:
    raw = state.get("reboots")
    if not isinstance(raw, list):
        return []
    return [float(stamp) for stamp in raw if isinstance(stamp, (int, float))]


def _float(values: dict[str, str], key: str, default: float) -> float:
    try:
        return float(values[key])
    except (KeyError, ValueError):
        return default


def _int(values: dict[str, str], key: str, default: int) -> int:
    try:
        return int(values[key])
    except (KeyError, ValueError):
        return default


def _bool(values: dict[str, str], key: str, default: bool) -> bool:
    raw = values.get(key)
    if raw is None:
        return default
    return raw.lower() in {"1", "yes", "true", "on"}
