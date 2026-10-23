"""Start the local viewer. The server publishes the page with Tailscale Funnel."""

from __future__ import annotations

import json
import os
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
from urllib.request import Request, urlopen

VIEWER_PASSWORD = "pass123"
DEFAULT_PORT = 8000
CAMERA_ID = "camera"
START_WAIT_S = 25.0
LINK_WAIT_S = 20.0
DEFAULT_INGEST_TOKEN = "cctv-ingest"
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


def ui_socket_path() -> Path:
    return state_dir() / "ui.sock"


def skip_path() -> Path:
    return state_dir() / "skip-ui"


def public_url() -> str | None:
    """Return the viewer URL published by the running server."""
    if not is_running():
        return None
    try:
        with urlopen(f"http://127.0.0.1:{stream_port()}/cluster", timeout=1) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (OSError, URLError, json.JSONDecodeError, ValueError, TimeoutError):
        return None
    if not isinstance(payload, dict):
        return None
    url = payload.get("public_url")
    if not isinstance(url, str) or not url:
        return None
    url_file().write_text(url + "\n", encoding="utf-8")
    return url


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
    token = _env_value("INGEST_TOKEN") or DEFAULT_INGEST_TOKEN
    path = env_file()
    path.write_text(
        (
            f"CCTV_PASSWORD={VIEWER_PASSWORD}\n"
            f"SESSION_SECRET={secret}\n"
            f"INGEST_TOKEN={token}\n"
            f"HOST=0.0.0.0\n"
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
        "INGEST_TOKEN": token,
        "HOST": "0.0.0.0",
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


def _our_cloudflared_pids() -> list[int]:
    """Leftover quick tunnels from before Funnel. Empty where /proc is unavailable."""
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


def start_stream(*, restart: bool = False, takeover: bool = False) -> str | None:
    """Start uvicorn. The process joins the tailnet and publishes Funnel when it is host.

    When the server is already healthy, leave it running. ``takeover`` asks that
    process to become the host. ``restart`` stops the server first.
    """
    root = repo_root()
    if not (root / "app" / "main.py").is_file():
        return f"personal-cctv is not at {root}"
    settings = ensure_env()
    write_config()
    if running_healthy() and not restart:
        if takeover:
            _request_takeover(settings["PORT"])
        return None
    stop_stream()
    log = log_file().open("ab")
    env = _process_env(settings)
    env["CCTV_TAKEOVER"] = "1" if takeover else "0"
    env["CCTV_CLUSTER"] = "1"
    try:
        app = subprocess.Popen(
            [
                _python(),
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "0.0.0.0",
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
    _wait_public_url()
    return None


def _request_takeover(port: str) -> None:
    request = Request(f"http://127.0.0.1:{port}/cluster/takeover", data=b"", method="POST")
    try:
        urlopen(request, timeout=2)
    except (OSError, URLError, TimeoutError):
        return


def _wait_public_url(timeout: float = LINK_WAIT_S) -> str | None:
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
    if url:
        return f"{url}\nviewer password: {password}\n"
    if error:
        return f"{error}\nviewer password: {password}\n"
    return (
        "cctv is up locally. the public link is not up.\n"
        f"viewer password: {password}\n"
    )


def format_start(*, takeover: bool = False) -> tuple[int, str]:
    ui_command("release", timeout=6.0)
    already = running_healthy()
    err = start_stream(takeover=takeover)
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
