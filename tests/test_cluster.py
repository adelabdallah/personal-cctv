"""Host election and posted feeds on the page."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ["CCTV_PASSWORD"] = "test-password"
os.environ["SESSION_SECRET"] = "test-secret"
os.environ["INGEST_TOKEN"] = "test-token"
os.environ["CCTV_CLUSTER"] = "0"

from app.camera import Camera
from app.cluster import Claim, Cluster, decide, power_rank
from app.config import CameraConfig, get_settings
from app.feeds import RemoteFeeds, fresh

JPEG = b"\xff\xd8\xff\xd9"
TOKEN = "test-token"


def _config(root: Path) -> Path:
    path = root / "cctv.yaml"
    path.write_text(
        (
            "cameras:\n"
            "  - id: local-cam\n"
            "    name: Local\n"
            "    device_index: 99\n"
        ),
        encoding="utf-8",
    )
    return path


class ElectionTests(unittest.TestCase):
    def _cluster(self, peers: list[Claim], clock: dict[str, float]) -> Cluster:
        cluster = Cluster(
            port=8000,
            token=TOKEN,
            frames=lambda: [],
            peers=lambda: list(peers),
            publish=lambda: "https://me.example",
            clock=lambda: clock["now"],
        )
        cluster.want_takeover = False
        return cluster

    def test_no_host_makes_this_device_the_host(self) -> None:
        cluster = self._cluster([], {"now": 100})
        cluster.step()
        self.assertEqual(cluster.role, "host")
        self.assertEqual(cluster.started_at, 100)
        self.assertIsNone(cluster.takeover_at)
        self.assertEqual(cluster.snapshot()["public_url"], "https://me.example")

    def test_longer_host_keeps_the_page(self) -> None:
        clock = {"now": 100}
        peer = Claim("http://10.0.0.8:8000", started_at=50, takeover_at=None, public_url="https://old.example")
        cluster = self._cluster([peer], clock)
        cluster.step()
        self.assertEqual(cluster.role, "follower")
        self.assertEqual(cluster.upstream, "http://10.0.0.8:8000")

        ours = self._cluster([], clock)
        ours.step()
        clock["now"] = 200
        later = Claim("http://10.0.0.9:8000", started_at=150, takeover_at=None, public_url="https://new.example")
        ours._peers = lambda: [later]
        ours.step()
        self.assertEqual(ours.role, "host")
        self.assertEqual(ours.started_at, 100)

    def test_takeover_replaces_the_longer_host(self) -> None:
        clock = {"now": 100}
        peer = Claim("http://10.0.0.8:8000", started_at=50, takeover_at=None, public_url="https://old.example")
        cluster = self._cluster([peer], clock)
        cluster.step()
        self.assertEqual(cluster.role, "follower")
        clock["now"] = 300
        cluster.request_takeover()
        cluster.step()
        self.assertEqual(cluster.role, "host")
        self.assertEqual(cluster.takeover_at, 300)
        self.assertEqual(cluster.snapshot()["public_url"], "https://me.example")

        newer = Claim("http://10.0.0.9:8000", started_at=10, takeover_at=400, public_url="https://strong.example")
        cluster._peers = lambda: [newer]
        cluster.step()
        self.assertEqual(cluster.role, "follower")
        self.assertEqual(cluster.upstream, "http://10.0.0.9:8000")
        self.assertEqual(cluster.snapshot()["public_url"], "https://strong.example")

    def test_decide_without_a_peer_starts_a_host(self) -> None:
        role, claim = decide(None, [], want_takeover=False, now=5)
        self.assertEqual(role, "host")
        self.assertEqual(claim.started_at, 5)

    def test_stronger_machine_replaces_a_host_that_just_started(self) -> None:
        clock = {"now": 100}
        peer = Claim(
            "http://10.0.0.2:8000",
            started_at=95,
            takeover_at=None,
            public_url="https://weak.example",
            power=10,
        )
        cluster = self._cluster([peer], clock)
        cluster.power = 40
        cluster.step()
        self.assertEqual(cluster.role, "host")

    def test_stable_weaker_host_keeps_the_page(self) -> None:
        clock = {"now": 100}
        peer = Claim(
            "http://10.0.0.2:8000",
            started_at=50,
            takeover_at=None,
            public_url="https://weak.example",
            power=10,
        )
        cluster = self._cluster([peer], clock)
        cluster.power = 40
        cluster.step()
        self.assertEqual(cluster.role, "follower")
        self.assertEqual(cluster.upstream, "http://10.0.0.2:8000")

    def test_pi_3b_plus_outranks_a_pi_zero_w(self) -> None:
        plus = power_rank("Raspberry Pi 3 Model B Plus Rev 1.3", 4)
        zero = power_rank("Raspberry Pi Zero W Rev 1.1", 1)
        four = power_rank("Raspberry Pi 4 Model B Rev 1.5", 4)
        self.assertGreater(plus, zero)
        self.assertGreater(four, plus)


class IngestTests(unittest.TestCase):
    def test_posted_jpeg_and_thermal_show_on_that_tile(self) -> None:
        from fastapi.testclient import TestClient

        from app.camera import Camera
        from app.main import app

        board = RemoteFeeds()
        board.store_video("door", "Porch", JPEG, now=10)
        self.assertTrue(fresh(10, 12))
        self.assertFalse(fresh(10, 14))
        self.assertTrue(board.rows(12)[0]["fresh"])
        self.assertFalse(board.rows(12)[0]["thermal"])

        with tempfile.TemporaryDirectory() as raw:
            os.environ["CONFIG_PATH"] = str(_config(Path(raw)))
            get_settings.cache_clear()
            with (
                patch.object(Camera, "start", lambda self: None),
                patch.object(Camera, "stop", lambda self: None),
                TestClient(app) as client,
            ):
                denied = client.post("/ingest/door", content=JPEG)
                self.assertEqual(denied.status_code, 401)
                wrong = client.post(
                    "/ingest/door",
                    content=JPEG,
                    headers={"Authorization": "Bearer nope"},
                )
                self.assertEqual(wrong.status_code, 401)
                local = client.post(
                    "/ingest/local-cam",
                    content=JPEG,
                    headers={"Authorization": f"Bearer {TOKEN}"},
                )
                self.assertEqual(local.status_code, 409)
                posted = client.post(
                    "/ingest/door",
                    content=JPEG,
                    headers={
                        "Authorization": f"Bearer {TOKEN}",
                        "X-Camera-Name": "Porch",
                    },
                )
                self.assertEqual(posted.status_code, 200)
                thermal = client.post(
                    "/ingest/door/thermal",
                    content=JPEG,
                    headers={"Authorization": f"Bearer {TOKEN}"},
                )
                self.assertEqual(thermal.status_code, 200)
                yard = client.post(
                    "/ingest/yard",
                    content=JPEG,
                    headers={"Authorization": f"Bearer {TOKEN}", "X-Camera-Name": "Yard"},
                )
                self.assertEqual(yard.status_code, 200)
                client.post("/login", data={"password": "test-password"})
                page = client.get("/")
                self.assertEqual(page.status_code, 200)
                door = page.text.split('data-camera-id="door"', 1)[1].split("</section>", 1)[0]
                yard_html = page.text.split('data-camera-id="yard"', 1)[1].split("</section>", 1)[0]
                self.assertIn("Porch", door)
                self.assertIn("data-expand", door)
                self.assertNotIn('src="/stream/', page.text)
                self.assertIn("data-thermal-toggle", door)
                self.assertNotIn("data-thermal-toggle hidden", door)
                self.assertIn("data-thermal-toggle hidden", yard_html)
                status = client.get("/status")
                feeds = {row["id"]: row for row in status.json()["feeds"]}
                self.assertTrue(feeds["door"]["fresh"])
                self.assertTrue(feeds["door"]["thermal"])
                self.assertFalse(feeds["yard"]["thermal"])
                with client.stream("GET", "/thermal/door") as response:
                    self.assertEqual(response.status_code, 200)
                    chunk = next(response.iter_bytes())
                self.assertIn(JPEG, chunk)
                missing = client.get("/thermal/yard")
                self.assertEqual(missing.status_code, 404)

    def test_reload_opens_a_camera_on_a_running_host(self) -> None:
        from fastapi.testclient import TestClient

        from app.camera import Camera
        from app.main import app

        with tempfile.TemporaryDirectory() as raw:
            config = _config(Path(raw))
            with (
                patch.dict(os.environ, {"CONFIG_PATH": str(config)}),
                patch.object(Camera, "start"),
                patch.object(Camera, "stop"),
            ):
                get_settings.cache_clear()
                with TestClient(app, client=("203.0.113.8", 9)) as remote:
                    self.assertEqual(remote.post("/local/cameras/reload").status_code, 403)
                with TestClient(app, client=("127.0.0.1", 9)) as local:
                    config.write_text(
                        "cameras:\n  - id: porch\n    name: Porch\n    device_index: 1\n",
                        encoding="utf-8",
                    )
                    reloaded = local.post("/local/cameras/reload")
                    self.assertEqual(reloaded.status_code, 200)
                    self.assertEqual(reloaded.json()["cameras"][0]["id"], "porch")
                    local.post("/login", data={"password": "test-password"})
                    feeds = {row["id"]: row for row in local.get("/status").json()["feeds"]}
                    self.assertIn("porch", feeds)
                    self.assertNotIn("local-cam", feeds)
                get_settings.cache_clear()


class SlowTests(unittest.TestCase):
    def test_slow_camera_posts_once_per_second(self) -> None:
        mono = {"now": 10.0}
        posts: list[bool] = []

        def fake(url: str, jpeg: bytes, token: str, name: str | None = None, *, slow: bool = False):
            posts.append(slow)
            return 200, True

        cluster = Cluster(
            port=8000,
            token=TOKEN,
            frames=lambda: [("door", "Porch", JPEG, JPEG, True)],
            peers=lambda: [],
            publish=lambda: None,
            mono=lambda: mono["now"],
            post=fake,
        )
        cluster.role = "follower"
        cluster.upstream = "http://10.0.0.8:8000"
        cluster.push_once()
        cluster.push_once()
        self.assertEqual(len(posts), 2)
        mono["now"] = 10.5
        cluster.push_once()
        self.assertEqual(len(posts), 2)
        mono["now"] = 11.0
        cluster.push_once()
        self.assertEqual(len(posts), 4)
        self.assertTrue(all(posts))

    def test_fast_camera_posts_every_tick(self) -> None:
        posts: list[str] = []

        def fake(url: str, jpeg: bytes, token: str, name: str | None = None, *, slow: bool = False):
            posts.append(url)
            return 200, False

        cluster = Cluster(
            port=8000,
            token=TOKEN,
            frames=lambda: [("door", "Porch", JPEG, None, False)],
            peers=lambda: [],
            publish=lambda: None,
            post=fake,
        )
        cluster.role = "follower"
        cluster.upstream = "http://10.0.0.8:8000"
        cluster.push_once()
        cluster.push_once()
        self.assertEqual(len(posts), 2)

    def test_page_toggle_and_boot_header(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            with patch.dict(os.environ, {"CONFIG_PATH": str(config)}), patch("app.camera.Camera.start"):
                get_settings.cache_clear()
                from fastapi.testclient import TestClient

                from app.main import app

                with TestClient(app) as client:
                    posted = client.post(
                        "/ingest/door",
                        content=JPEG,
                        headers={"Authorization": f"Bearer {TOKEN}", "X-Slow": "1"},
                    )
                    self.assertEqual(posted.status_code, 200)
                    self.assertTrue(posted.json()["slow"])
                    client.post("/login", data={"password": "test-password"})
                    page = client.get("/")
                    door = page.text.split('data-camera-id="door"', 1)[1].split("</section>", 1)[0]
                    self.assertIn('aria-pressed="true"', door)
                    self.assertIn("Slow mode on", door)
                    local = page.text.split('data-camera-id="local-cam"', 1)[1].split("</section>", 1)[0]
                    self.assertIn('aria-pressed="false"', local)
                    off = client.post("/cameras/door/slow", json={"slow": False})
                    self.assertEqual(off.status_code, 200)
                    self.assertFalse(off.json()["slow"])
                    again = client.post(
                        "/ingest/door",
                        content=JPEG,
                        headers={"Authorization": f"Bearer {TOKEN}", "X-Slow": "1"},
                    )
                    self.assertFalse(again.json()["slow"])
                    on = client.post("/cameras/local-cam/slow", json={"slow": True})
                    self.assertTrue(on.json()["slow"])
                get_settings.cache_clear()

    def test_config_starts_slow(self) -> None:
        camera = Camera(
            CameraConfig(id="pico", name="Pico", device_index=0, fps=12, slow=True)
        )
        self.assertTrue(camera.slow)
        self.assertEqual(camera.frame_interval(), 1.0)
        camera.set_slow(False)
        self.assertEqual(camera.frame_interval(), 1.0 / 12)


if __name__ == "__main__":
    unittest.main()
