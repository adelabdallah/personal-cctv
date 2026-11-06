"""PiTFT menu. Up, down, back, and select."""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

from cctv_zero.backlight import Backlight, idle_blank
from cctv_zero.debug import close_battery
from cctv_zero.menu import Menu, press, rows

PIN_UP = 17
PIN_DOWN = 22
PIN_BACK = 23
PIN_SELECT = 27
HOLD_S = 1.5
LOG_MAX = 12


def _claim_display() -> None:
    helper = Path("/usr/local/sbin/claim-pitft-display.sh")
    if not helper.is_file():
        return
    try:
        subprocess.run(["sudo", "-n", str(helper)], check=False, timeout=5)
    except (OSError, subprocess.SubprocessError):
        pass


class App:
    def __init__(self, fb: Path, size: tuple[int, int], backlight: Backlight | None = None) -> None:
        self.fb = fb
        self.width, self.height = size
        self.menu = Menu()
        self.log: list[str] = []
        self._lock = threading.Lock()
        self._dirty = True
        self._start_gen = 0
        self._stopping = False
        self._facts = ""
        self._ssh: list[str] = []
        self._online = False
        self._net_at = 0.0
        self._debug_at = 0.0
        self._phase_at = 0.0
        self._down = {"up": None, "down": None, "back": None, "select": None}
        self._held: set[str] = set()
        self._wake: set[str] = set()
        self._ready_at = float("inf")
        self._last_input = time.monotonic()
        self._blanked = False
        self._backlight = backlight or Backlight()

    def note(self, name: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        with self._lock:
            self.log.append(f"{stamp} {name}")
            self.log = self.log[-LOG_MAX:]
            self._dirty = True

    def _buttons_ready(self) -> bool:
        return time.monotonic() >= self._ready_at

    def on_down(self, name: str) -> None:
        if not self._buttons_ready():
            return
        now = time.monotonic()
        self._last_input = now
        if self._blanked:
            self._blanked = False
            self._backlight.set(True)
            self._wake.add(name)
            self._dirty = True
            return
        self._down[name] = now
        self._held.discard(name)

    def on_up(self, name: str) -> None:
        if name in self._wake:
            self._wake.discard(name)
            self._down[name] = None
            return
        if not self._buttons_ready():
            self._down[name] = None
            return
        started = self._down.get(name)
        self._down[name] = None
        if name in self._held:
            self._held.discard(name)
            return
        if started is None:
            return
        self.handle(name)

    def poll_blank(self, now: float | None = None) -> None:
        moment = time.monotonic() if now is None else now
        if not idle_blank(self._last_input, moment, self._blanked):
            return
        if self._backlight.set(False):
            self._blanked = True

    def poll_holds(self) -> None:
        now = time.monotonic()
        for name, started in self._down.items():
            if started is None or name in self._held:
                continue
            if now - started >= HOLD_S:
                self._held.add(name)
                self.note(f"{name}-hold")

    def handle(self, button: str) -> None:
        self.note(button)
        cameras: tuple[str, ...] | None = None
        with self._lock:
            opening_cameras = button == "select" and self.menu.page == "home" and self.menu.selected == 0
        if opening_cameras:
            cameras = _camera_names()
        with self._lock:
            result = press(self.menu, button)
            menu = result.menu
            if cameras is not None and menu.page == "cameras":
                menu = Menu(
                    page="cameras",
                    phase=menu.phase,
                    cameras=cameras,
                    notice=menu.notice,
                )
            if menu.page == "debug" and self.menu.page != "debug":
                self._debug_at = 0.0
            self.menu = menu
            effect = result.effect
            self._dirty = True
        self._run_effect(effect)

    def _run_effect(self, effect: str) -> None:
        if effect == "stop":
            self._start_gen += 1
            from cctv_ui.service import stop_stream

            stop_stream()
            return
        if effect == "poweroff":
            self._power_off()
            return
        if effect.startswith("start:"):
            self._start_gen += 1
            gen = self._start_gen
            name = effect.split(":", 1)[1]
            threading.Thread(target=self._start, args=(name, gen), name="cctv-zero-start", daemon=True).start()

    def _start(self, name: str, gen: int) -> None:
        from cctv_ui.service import start_stream

        err = start_stream(camera=name)
        with self._lock:
            if gen != self._start_gen:
                return
            if err:
                self.menu = Menu(
                    page="home",
                    cameras=self.menu.cameras,
                    notice=err.replace("\n", " ")[:64],
                )
            else:
                self.menu = Menu(page="home", phase="running", cameras=self.menu.cameras)
            self._dirty = True

    def _power_off(self) -> None:
        from cctv_ui.service import power_action
        from cctv_zero.render import paint_message

        try:
            from cctv_zero.fb import blit

            blit(self.fb, paint_message(self.width, self.height, "Shutting down", "wait for the LED"))
        except Exception:
            pass
        err = power_action("poweroff")
        if err:
            with self._lock:
                self.menu = Menu(
                    page="power",
                    selected=1,
                    phase=self.menu.phase,
                    cameras=self.menu.cameras,
                    notice=err.replace("\n", " ")[:64],
                )
                self._dirty = True
            return
        self._stopping = True

    def sync_phase(self) -> None:
        from cctv_ui.service import is_running

        now = time.monotonic()
        if now - self._phase_at < 1:
            return
        self._phase_at = now
        with self._lock:
            phase = self.menu.phase
        if phase != "running" or is_running():
            return
        with self._lock:
            if self.menu.phase != "running":
                return
            self.menu = Menu(
                page=self.menu.page,
                selected=self.menu.selected,
                cameras=self.menu.cameras,
                notice=self.menu.notice,
            )
            self._dirty = True

    def refresh_debug(self) -> None:
        from cctv_zero.debug import (
            battery_label,
            cpu_temp_c,
            disk_free_label,
            facts_line,
            internet_up,
            ssh_sessions,
            wifi_ssid,
        )

        now = time.monotonic()
        if now - self._net_at >= 3:
            self._online = internet_up()
            self._net_at = now
        facts = facts_line(
            ssid=wifi_ssid(),
            online=self._online,
            disk=disk_free_label(),
            temp=cpu_temp_c(),
            battery=battery_label(),
        )
        peers = ssh_sessions()
        with self._lock:
            self._facts = facts
            self._ssh = peers
            self._dirty = True

    def paint(self) -> None:
        from cctv_zero.fb import blit
        from cctv_zero.render import paint_debug, paint_rows

        with self._lock:
            menu = self.menu
            log = list(self.log)
            facts = self._facts
            peers = list(self._ssh)
        if menu.page == "debug":
            image = paint_debug(self.width, self.height, facts=facts, ssh_rows=peers, log_rows=log)
        elif menu.page == "power":
            image = paint_rows(
                self.width,
                self.height,
                "Power off?",
                rows(menu),
                menu.selected,
                notice=menu.notice,
                legend=("up", "down", "back", "yes"),
            )
        elif menu.page == "cameras":
            image = paint_rows(
                self.width,
                self.height,
                "Cameras",
                rows(menu),
                menu.selected,
                notice=menu.notice,
                legend=("up", "down", "back", "open"),
            )
        else:
            image = paint_rows(
                self.width,
                self.height,
                "zero",
                rows(menu),
                menu.selected,
                notice=menu.notice,
                legend=("up", "down", "·", "open"),
            )
        blit(self.fb, image)

    def run(self) -> int:
        try:
            from gpiozero import Button
        except ImportError:
            print("Install gpiozero: sudo apt install -y python3-gpiozero", file=sys.stderr)
            return 1

        buttons = {
            "up": Button(PIN_UP, pull_up=True, bounce_time=0.04),
            "down": Button(PIN_DOWN, pull_up=True, bounce_time=0.04),
            "back": Button(PIN_BACK, pull_up=True, bounce_time=0.04),
            "select": Button(PIN_SELECT, pull_up=True, bounce_time=0.04),
        }
        for name, button in buttons.items():
            button.when_pressed = lambda *args, name=name: self.on_down(name)
            button.when_released = lambda *args, name=name: self.on_up(name)
        # Claiming the pins can look like a press. Ignore that moment so boot stays on CCTV.
        _claim_display()
        self._ready_at = time.monotonic() + 0.6
        self._last_input = time.monotonic()
        self._backlight.set(True)
        try:
            while not self._stopping:
                self.poll_holds()
                self.poll_blank()
                self.sync_phase()
                now = time.monotonic()
                with self._lock:
                    on_debug = self.menu.page == "debug"
                if on_debug and now - self._debug_at >= 1:
                    self._debug_at = now
                    self.refresh_debug()
                with self._lock:
                    dirty = self._dirty
                    self._dirty = False
                if dirty:
                    self.paint()
                time.sleep(0.05)
        except KeyboardInterrupt:
            return 0
        finally:
            self._backlight.close()
            close_battery()
            for button in buttons.values():
                button.close()
        return 0


def _camera_names() -> tuple[str, ...]:
    from cctv_ui.service import discover_cameras

    return tuple(str(item["name"]) for item in discover_cameras())


def run() -> int:
    from cctv_zero.fb import framebuffer_size, find_framebuffer

    fb = find_framebuffer()
    if fb is None:
        print("No PiTFT framebuffer (/dev/fb1 or /dev/fb0).", file=sys.stderr)
        return 1
    size = framebuffer_size(fb) or (320, 240)
    return App(fb, size).run()
