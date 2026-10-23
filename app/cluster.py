"""Pick one host on the tailnet and publish that machine with Funnel.

started_at and takeover_at are wall-clock seconds so two machines can compare
them. A frame's age stays on each machine's monotonic clock.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Literal, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.feeds import SLOW_S

logger = logging.getLogger(__name__)

Role = Literal["host", "follower"]
Frames = Callable[[], list[tuple[str, str, Optional[bytes], Optional[bytes], bool]]]
Pace = Callable[[str, bool], None]
Peers = Callable[[], list["Claim"]]
Publish = Callable[[], Optional[str]]


@dataclass(frozen=True)
class Claim:
    base: str
    started_at: float
    takeover_at: float | None
    public_url: str | None


def winner(claims: list[Claim]) -> Claim | None:
    if not claims:
        return None

    def rank(claim: Claim) -> tuple[int, float]:
        if claim.takeover_at is not None:
            return (1, claim.takeover_at)
        return (0, -claim.started_at)

    return max(claims, key=rank)


def decide(
    self_claim: Claim | None,
    peers: list[Claim],
    *,
    want_takeover: bool,
    now: float,
) -> tuple[Role, Claim]:
    claims = list(peers)
    if want_takeover:
        started = self_claim.started_at if self_claim is not None else now
        claims.append(Claim(base="", started_at=started, takeover_at=now, public_url=None))
    elif self_claim is not None:
        claims.append(self_claim)
    if not claims:
        return "host", Claim(base="", started_at=now, takeover_at=None, public_url=None)
    chosen = winner(claims)
    assert chosen is not None
    if chosen.base == "":
        return "host", chosen
    return "follower", chosen


def cluster_enabled() -> bool:
    return os.environ.get("CCTV_CLUSTER", "1").strip().lower() not in {"0", "false", "no"}


def _tailscale() -> str | None:
    return shutil.which("tailscale")


def peer_bases(port: int) -> list[str]:
    binary = _tailscale()
    if binary is None:
        return []
    try:
        completed = subprocess.run(
            [binary, "status", "--json"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if completed.returncode != 0 or not completed.stdout.strip():
        return []
    try:
        data = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return []
    peers = data.get("Peer") or {}
    values = peers.values() if isinstance(peers, dict) else peers
    bases: list[str] = []
    for peer in values:
        if not isinstance(peer, dict) or not peer.get("Online"):
            continue
        ips = [str(ip) for ip in (peer.get("TailscaleIPs") or [])]
        ipv4 = [ip for ip in ips if ":" not in ip]
        chosen = ipv4[0] if ipv4 else (ips[0] if ips else "")
        if not chosen:
            continue
        host = f"[{chosen}]" if ":" in chosen else chosen
        bases.append(f"http://{host}:{port}")
    return bases


def read_host(base: str, timeout: float = 0.4) -> Claim | None:
    try:
        with urlopen(f"{base}/cluster", timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (OSError, URLError, json.JSONDecodeError, ValueError, TimeoutError):
        return None
    if not isinstance(payload, dict) or payload.get("role") != "host":
        return None
    started = payload.get("started_at")
    if not isinstance(started, (int, float)):
        return None
    takeover = payload.get("takeover_at")
    if not isinstance(takeover, (int, float)):
        takeover = None
    public = payload.get("public_url")
    if not isinstance(public, str) or not public:
        public = None
    return Claim(
        base=base,
        started_at=float(started),
        takeover_at=float(takeover) if takeover is not None else None,
        public_url=public,
    )


def find_live_host(port: int, timeout: float = 0.4) -> Claim | None:
    bases = peer_bases(port)
    if not bases:
        return None
    with ThreadPoolExecutor(max_workers=min(8, len(bases))) as pool:
        claims = [claim for claim in pool.map(lambda base: read_host(base, timeout), bases) if claim]
    return winner(claims)


def magic_dns() -> str | None:
    binary = _tailscale()
    if binary is None:
        return None
    try:
        completed = subprocess.run(
            [binary, "status", "--json"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0 or not completed.stdout.strip():
        return None
    try:
        data = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return None
    self_info = data.get("Self") or {}
    if not isinstance(self_info, dict):
        return None
    name = str(self_info.get("DNSName") or "").strip().strip(".")
    return name or None


def publish_funnel(port: int) -> str | None:
    """Turn on Funnel when this tailscale supports it, and return the viewer URL."""
    name = magic_dns()
    if not name:
        return None
    tailnet = f"http://{name}:{port}"
    binary = _tailscale()
    if binary is None:
        return None
    try:
        completed = subprocess.run(
            [binary, "funnel", "--bg", str(port)],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return tailnet
    if completed.returncode != 0:
        return tailnet
    return f"https://{name}"


def post_jpeg(
    url: str,
    jpeg: bytes,
    token: str,
    name: str | None = None,
    *,
    slow: bool = False,
) -> tuple[int | None, bool | None]:
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "image/jpeg"}
    if name:
        headers["X-Camera-Name"] = name
    if slow:
        headers["X-Slow"] = "1"
    request = Request(url, data=jpeg, method="POST", headers=headers)
    try:
        with urlopen(request, timeout=2) as resp:
            return resp.status, _slow_flag(resp.read())
    except HTTPError as exc:
        return exc.code, _slow_flag(exc.read())
    except (OSError, URLError, TimeoutError):
        return None, None


def _slow_flag(body: bytes) -> bool | None:
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeError):
        return None
    if isinstance(payload, dict) and isinstance(payload.get("slow"), bool):
        return payload["slow"]
    return None


class Cluster:
    def __init__(
        self,
        *,
        port: int,
        token: str,
        frames: Frames,
        peers: Peers | None = None,
        publish: Publish | None = None,
        clock: Callable[[], float] | None = None,
        mono: Callable[[], float] | None = None,
        on_pace: Pace | None = None,
        post: Callable[..., tuple[int | None, bool | None]] | None = None,
    ) -> None:
        self.port = port
        self.token = token
        self._frames = frames
        self._peers = peers or (lambda: _probe(port))
        self._publish = publish or (lambda: publish_funnel(port))
        self._clock = clock or time.time
        self._mono = mono or time.monotonic
        self._on_pace = on_pace
        self._post = post or post_jpeg
        self._lock = threading.Lock()
        self._step_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.role: Role = "follower"
        self.started_at: float | None = None
        self.takeover_at: float | None = None
        self.public_url: str | None = None
        self.upstream: str | None = None
        self.want_takeover = os.environ.get("CCTV_TAKEOVER", "").strip().lower() in {"1", "true", "yes"}
        self._own_url: str | None = None
        self._published_at = 0.0
        self._last_post: dict[str, float] = {}

    def hold_host(self) -> None:
        """Serve this machine alone. Used when the tailnet loop is off."""
        with self._lock:
            self.role = "host"
            self.started_at = self._clock()
            self.takeover_at = None
            self.upstream = None

    def request_takeover(self) -> None:
        with self._lock:
            self.want_takeover = True

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "role": self.role,
                "started_at": self.started_at,
                "takeover_at": self.takeover_at,
                "public_url": self.public_url,
            }

    def viewer_redirect(self) -> str | None:
        with self._lock:
            if self.role == "follower" and self.public_url:
                return self.public_url
        return None

    def accepts_ingest(self) -> bool:
        with self._lock:
            return self.role == "host"

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="cctv-cluster", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

    def step(self) -> None:
        with self._step_lock:
            self._step()

    def _step(self) -> None:
        peers = self._peers()
        with self._lock:
            want = self.want_takeover
            self_claim = None
            if self.role == "host" and self.started_at is not None:
                self_claim = Claim(
                    base="",
                    started_at=self.started_at,
                    takeover_at=self.takeover_at,
                    public_url=self.public_url,
                )
        role, claim = decide(self_claim, peers, want_takeover=want, now=self._clock())
        public = self._ensure_published()
        with self._lock:
            was_host = self.role == "host"
            if role == "host":
                if not was_host or self.started_at is None:
                    self.started_at = claim.started_at
                self.role = "host"
                self.upstream = None
                if claim.takeover_at is not None:
                    self.takeover_at = claim.takeover_at
                if public:
                    self.public_url = public
                elif not was_host:
                    self.public_url = None
                if want:
                    self.want_takeover = False
            else:
                self.role = "follower"
                self.started_at = None
                self.takeover_at = None
                self.upstream = claim.base
                if claim.public_url:
                    self.public_url = claim.public_url

    def push_once(self) -> None:
        with self._lock:
            upstream = self.upstream if self.role == "follower" else None
            token = self.token
        if not upstream or not token:
            return
        now = self._mono()
        for camera_id, name, video, thermal, slow in self._frames():
            if slow and now - self._last_post.get(camera_id, 0.0) < SLOW_S:
                continue
            sent = False
            if video:
                sent = self._send(camera_id, f"{upstream}/ingest/{camera_id}", video, token, name, slow) or sent
            if thermal:
                sent = (
                    self._send(
                        camera_id,
                        f"{upstream}/ingest/{camera_id}/thermal",
                        thermal,
                        token,
                        name,
                        slow,
                    )
                    or sent
                )
            if sent:
                self._last_post[camera_id] = now

    def _send(self, camera_id: str, url: str, jpeg: bytes, token: str, name: str, slow: bool) -> bool:
        status, pace = self._post(url, jpeg, token, name, slow=slow)
        if pace is not None and self._on_pace is not None:
            self._on_pace(camera_id, pace)
        return status is not None

    def _ensure_published(self) -> str | None:
        now = time.monotonic()
        if self._published_at and now - self._published_at < 30:
            return self._own_url
        self._published_at = now
        try:
            url = self._publish()
        except Exception:
            logger.exception("Funnel publish failed")
            return self._own_url
        if url:
            self._own_url = url
        return self._own_url

    def _loop(self) -> None:
        last_vote = 0.0
        last_post = 0.0
        while not self._stop.is_set():
            now = time.monotonic()
            if now - last_vote >= 2:
                last_vote = now
                try:
                    self.step()
                except Exception:
                    logger.exception("Cluster step failed")
            if now - last_post >= 0.25:
                last_post = now
                try:
                    self.push_once()
                except Exception:
                    logger.exception("Frame post failed")
            self._stop.wait(0.2)


def _probe(port: int) -> list[Claim]:
    bases = peer_bases(port)
    if not bases:
        return []
    with ThreadPoolExecutor(max_workers=min(8, len(bases))) as pool:
        return [claim for claim in pool.map(lambda base: read_host(base), bases) if claim]
