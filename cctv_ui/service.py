"""Start the local viewer and publish a trycloudflare.com link."""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Literal
from urllib.error import URLError
from urllib.request import urlopen

VIEWER_PASSWORD = "pass123"
DEFAULT_PORT = 8000
CAMERA_ID = "camera"
START_WAIT_S = 25.0
TUNNEL_WAIT_S = 20.0
# Quick Tunnel names are several hyphenated words. api.trycloudflare.com is the
# create-tunnel endpoint and shows up in failure lines before the real hostname.
TUNNEL_URL_RE = re.compile(r"https://[a-z0-9]+(?:-[a-z0-9]+)+\.trycloudflare\.com")
PowerAction = Literal["reboot", "poweroff"]


def state_dir() -> Path:
    override = os.environ.get("CCTV_STATE_DIR", "").strip()
    path = Path(override) if override else Path.home() / ".local" / "share" / "cctv"
    path.mkdir(parents=True, exist_ok=True)
    return path


def repo_root() -> Path:
    override = os.environ.get("CCTV_ROOT", "").strip()
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[1]


def url_file() -> Path:
    return state_dir() / "cctv-url"


def meta_file() -> Path:
    return state_dir() / "cctv.json"


def env_file() -> Path:
    return state_dir() / "cctv.env"


def config_file() -> Path:
    return state_dir() / "config.yaml"


def log_file() -> Path:
    return state_dir() / "cctv.log"


def tunnel_log_file() -> Path:
    """Log for the current cloudflared only. Truncated each time a tunnel starts."""
    return state_dir() / "tunnel.log"


def ui_socket_path() -> Path:
    return state_dir() / "ui.sock"


def skip_path() -> Path:
    return state_dir() / "skip-ui"


def parse_tunnel_url(text: str) -> str | None:
    found = TUNNEL_URL_RE.findall(text)
    return found[-1] if found else None


def _saved_url() -> str:
    try:
        return url_file().read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def public_url() -> str | None:
    """Return the hostname of the cloudflared that is running now.

    A saved name from a tunnel that has already exited is dropped. A name is
    pinned to the current process the first time that process logs one, so a
    later line in a shared log cannot replace it.
    """
    meta = _read_meta()
    if not _pid_alive(meta.get("tunnel_pid")):
        if meta.get("url") or _saved_url():
            _forget_public_url()
        return None
    pinned = meta.get("url")
    if isinstance(pinned, str) and parse_tunnel_url(pinned):
        if pinned != _saved_url():
            url_file().write_text(pinned + "\n", encoding="utf-8")
        return pinned
    harvested = _harvest_url()
    if not harvested:
        return None
    meta = _read_meta()
    meta["url"] = harvested
    _write_meta(meta)
    url_file().write_text(harvested + "\n", encoding="utf-8")
    return harvested


def viewer_password() -> str:
    return VIEWER_PASSWORD


def _read_meta() -> dict:
    try:
        data = json.loads(meta_file().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_meta(data: dict) -> None:
    meta_file().write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _forget_public_url() -> None:
    meta = _read_meta()
    meta.pop("url", None)
    _write_meta(meta)
    try:
        url_file().unlink()
    except OSError:
        pass


def _env_value(key: str) -> str | None:
    try:
        lines = env_file().read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip()
    return None


def ensure_env() -> dict[str, str]:
    secret = _env_value("SESSION_SECRET") or secrets.token_hex(32)
    port = _env_value("PORT") or str(DEFAULT_PORT)
    path = env_file()
    path.write_text(
        (
            f"CCTV_PASSWORD={VIEWER_PASSWORD}\n"
            f"SESSION_SECRET={secret}\n"
            f"HOST=127.0.0.1\n"
            f"PORT={port}\n"
        ),
        encoding="utf-8",
    )
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return {
        "CCTV_PASSWORD": VIEWER_PASSWORD,
        "SESSION_SECRET": secret,
        "HOST": "127.0.0.1",
        "PORT": port,
    }


def write_config() -> None:
    config_file().write_text(
        (
            "app_name: CCTV\n"
            "cameras:\n"
            f"  - id: {CAMERA_ID}\n"
            "    name: Camera\n"
            "    backend: picamera2\n"
            "    device_index: 0\n"
            "    width: 640\n"
            "    height: 480\n"
            "    fps: 8\n"
            "    jpeg_quality: 65\n"
        ),
        encoding="utf-8",
    )


def _pid_alive(pid: object) -> bool:
    if not isinstance(pid, int):
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def is_running() -> bool:
    return _pid_alive(_read_meta().get("app_pid"))


def stream_port() -> str:
    port = _read_meta().get("port") or DEFAULT_PORT
    return str(port)


def tunnel_error() -> str | None:
    error = _read_meta().get("tunnel_error")
    return str(error) if error else None


def _wait_health(port: str, timeout: float = START_WAIT_S) -> str | None:
    deadline = time.monotonic() + timeout
    url = f"http://127.0.0.1:{port}/healthz"
    last = "cctv did not become ready"
    while time.monotonic() < deadline:
        try:
            with urlopen(url, timeout=1) as resp:
                if resp.status == 200:
                    return None
        except URLError as exc:
            last = str(exc.reason if getattr(exc, "reason", None) else exc)
        except OSError as exc:
            last = str(exc)
        time.sleep(0.25)
    return last


def running_healthy(timeout: float = 0.4) -> bool:
    if not is_running():
        return False
    return _wait_health(stream_port(), timeout=timeout) is None


def preview_url() -> str:
    return f"http://127.0.0.1:{stream_port()}/preview/{CAMERA_ID}"


def _kill_tree(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGTERM)
    except OSError:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            return
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return
        time.sleep(0.1)
    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            return


def stop_stream() -> None:
    meta = _read_meta()
    for key in ("app_pid", "tunnel_pid"):
        pid = meta.get(key)
        if isinstance(pid, int):
            _kill_tree(pid)
    for stray in _our_cloudflared_pids():
        _kill_tree(stray)
    meta["app_pid"] = None
    meta["tunnel_pid"] = None
    meta.pop("url", None)
    _write_meta(meta)
    try:
        url_file().unlink()
    except OSError:
        pass


def _process_env(settings: dict[str, str]) -> dict[str, str]:
    env = os.environ.copy()
    env.update(settings)
    env["CONFIG_PATH"] = str(config_file())
    root = str(repo_root())
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{root}:{existing}" if existing else root
    local_bin = str(Path.home() / ".local" / "bin")
    env["PATH"] = local_bin + os.pathsep + env.get("PATH", "")
    return env


def _python() -> str:
    return sys.executable or shutil.which("python3") or "python3"


def start_stream(*, restart: bool = False) -> str | None:
    """Start uvicorn and a quick tunnel. Return an error string, or None.

    When the server is already healthy, leave it running and refresh the public
    link. ``restart`` stops that server first and brings a new one up.
    """
    root = repo_root()
    if not (root / "app" / "main.py").is_file():
        return f"personal-cctv is not at {root}"
    settings = ensure_env()
    write_config()
    if running_healthy() and not restart:
        _recover_public_url(settings)
        return None
    stop_stream()
    log = log_file().open("ab")
    env = _process_env(settings)
    try:
        app = subprocess.Popen(
            [
                _python(),
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                settings["PORT"],
            ],
            cwd=str(root),
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError as exc:
        log.close()
        return f"could not start cctv ({exc})"
    meta = _read_meta()
    meta.update({"app_pid": app.pid, "port": settings["PORT"], "repo": str(root)})
    _write_meta(meta)
    err = _wait_health(settings["PORT"])
    if err:
        stop_stream()
        return f"cctv did not become ready ({err})"
    tunnel_err = _start_tunnel(settings["PORT"], env, log)
    if tunnel_err:
        meta = _read_meta()
        meta["tunnel_error"] = tunnel_err
        _write_meta(meta)
        return None
    _wait_public_url()
    return None


def _recover_public_url(settings: dict[str, str]) -> None:
    if public_url():
        return
    log = log_file().open("ab")
    _stop_tunnel()
    err = _start_tunnel(settings["PORT"], _process_env(settings), log)
    if err:
        meta = _read_meta()
        meta["tunnel_error"] = err
        _write_meta(meta)
        return
    _wait_public_url()


def _stop_tunnel() -> None:
    meta = _read_meta()
    pid = meta.get("tunnel_pid")
    if isinstance(pid, int):
        _kill_tree(pid)
    for stray in _our_cloudflared_pids():
        _kill_tree(stray)
    meta["tunnel_pid"] = None
    meta.pop("url", None)
    _write_meta(meta)
    try:
        url_file().unlink()
    except OSError:
        pass


def _cloudflared() -> str | None:
    found = shutil.which("cloudflared")
    if found:
        return found
    candidate = Path.home() / ".local" / "bin" / "cloudflared"
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    return None


def _our_cloudflared_pids() -> list[int]:
    """cloudflared processes this app started. Empty where /proc is unavailable."""
    root = Path("/proc")
    if not root.is_dir():
        return []
    found: list[int] = []
    for entry in root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        command = raw.replace(b"\x00", b" ").decode("utf-8", errors="replace")
        if "cloudflared" in command and "--url" in command and "127.0.0.1" in command:
            found.append(int(entry.name))
    return found


def _start_tunnel(port: str, env: dict[str, str], log) -> str | None:
    del log
    cloudflared = _cloudflared()
    if cloudflared is None:
        return "cloudflared is not installed"
    _stop_tunnel()
    try:
        handle = tunnel_log_file().open("wb")
    except OSError as exc:
        return f"cloudflared failed ({exc})"
    try:
        proc = subprocess.Popen(
            [
                cloudflared,
                "tunnel",
                "--url",
                f"http://127.0.0.1:{port}",
                "--no-autoupdate",
            ],
            env=env,
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError as exc:
        handle.close()
        return f"cloudflared failed ({exc})"
    handle.close()
    meta = _read_meta()
    meta["tunnel_pid"] = proc.pid
    meta["tunnel_log_offset"] = 0
    meta.pop("url", None)
    meta.pop("tunnel_error", None)
    _write_meta(meta)
    return None


def _harvest_url() -> str | None:
    """First quick-tunnel hostname in the current tunnel log."""
    raw_offset = _read_meta().get("tunnel_log_offset")
    try:
        offset = int(raw_offset or 0)
    except (TypeError, ValueError):
        offset = 0
    try:
        with tunnel_log_file().open("rb") as handle:
            if offset:
                handle.seek(offset)
            chunk = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    found = TUNNEL_URL_RE.findall(chunk)
    return found[0] if found else None


def _wait_public_url(timeout: float = TUNNEL_WAIT_S) -> str | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        url = public_url()
        if url:
            return url
        time.sleep(0.25)
    return public_url()


def format_link() -> str:
    if not is_running():
        return "cctv is not running\n"
    url = public_url()
    password = viewer_password()
    error = tunnel_error()
    if error and not url:
        return f"{error}\nviewer password: {password}\n"
    if url:
        return f"{url}\nviewer password: {password}\n"
    if not _pid_alive(_read_meta().get("tunnel_pid")):
        return (
            "cctv is up locally. the public tunnel is not running.\n"
            "run: cctv start\n"
            f"viewer password: {password}\n"
        )
    return (
        "cctv is up locally. the public link is still coming up.\n"
        "wait a few seconds and run: cctv link\n"
        f"viewer password: {password}\n"
    )


def format_start() -> tuple[int, str]:
    ui_command("release", timeout=6.0)
    already = running_healthy()
    err = start_stream()
    ui_command("refresh", timeout=1.0)
    if err:
        return 1, err + "\n"
    body = format_link()
    if already:
        return 0, "cctv already running\n" + body
    return 0, body


def ui_command(command: str, timeout: float = 2.0) -> str | None:
    path = ui_socket_path()
    if not path.exists():
        return None
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(str(path))
        sock.sendall((command.strip() + "\n").encode("utf-8"))
        data = b""
        while b"\n" not in data:
            chunk = sock.recv(256)
            if not chunk:
                break
            data += chunk
    except OSError:
        return None
    finally:
        sock.close()
    text = data.decode("utf-8", errors="replace").strip()
    return text or None


def power_action(action: PowerAction) -> str | None:
    systemctl = shutil.which("systemctl") or "/usr/bin/systemctl"
    try:
        result = subprocess.run(
            ["sudo", "-n", systemctl, action],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        return str(exc)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        if detail:
            return detail
        return f"could not {action}"
    return None
