"""Fullscreen launcher for the Waveshare panel."""

from __future__ import annotations

import io
import logging
import os
import socket
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from cctv_ui.display import kmsdrm_device_index, logical_to_panel, panel_to_logical
from cctv_ui.preview import FramePump
from cctv_ui.service import (
    format_link,
    is_running,
    power_action,
    public_url,
    skip_path,
    start_stream,
    state_dir,
    tunnel_error,
    ui_socket_path,
)
from cctv_ui.theme import (
    ACCENT,
    APP_NAME,
    BG,
    BTN_FACE,
    DIM,
    FG,
    HOT,
    HOT_C,
    LOGICAL_H,
    LOGICAL_W,
    PANEL_H,
    PANEL_W,
    PRESENT_ROTATE,
    load_font,
)
from cctv_ui.touch import TouchReader

log = logging.getLogger(__name__)

HEADER_H = 34
STATUS_H = 24
MARGIN = 10
GAP = 8
TEMP_PATH = Path("/sys/class/thermal/thermal_zone0/temp")
TEMP_PERIOD_S = 2.0
STATUS_PERIOD_S = 0.5


@dataclass
class Button:
    action: str
    label: str
    rect: tuple[int, int, int, int]


@dataclass
class UiState:
    page: str = "home"
    confirm: str | None = None
    status: str = "idle"
    starting: bool = False
    temp_c: float | None = None
    temp_at: float = 0.0
    status_at: float = 0.0
    hold_until: float = 0.0
    stream_up: bool = False
    rotate: int = PRESENT_ROTATE


def home_buttons() -> tuple[Button, ...]:
    top = HEADER_H + 4
    height = LOGICAL_H - top - STATUS_H - MARGIN
    width = LOGICAL_W - MARGIN * 2 - GAP
    tile_w = width // 2
    tile_h = (height - GAP) // 2
    labels = (
        ("start", "Start Stream"),
        ("camera", "Camera"),
        ("reboot", "Reboot"),
        ("shutdown", "Shut down"),
    )
    buttons: list[Button] = []
    for index, (action, label) in enumerate(labels):
        col = index % 2
        row = index // 2
        x = MARGIN + col * (tile_w + GAP)
        y = top + row * (tile_h + GAP)
        buttons.append(Button(action, label, (x, y, tile_w, tile_h)))
    return tuple(buttons)


def confirm_buttons() -> tuple[Button, ...]:
    width = 140
    height = 64
    y = LOGICAL_H // 2
    gap = 24
    left = LOGICAL_W // 2 - gap // 2 - width
    right = LOGICAL_W // 2 + gap // 2
    return (
        Button("yes", "Yes", (left, y, width, height)),
        Button("no", "No", (right, y, width, height)),
    )


def back_button() -> Button:
    return Button("back", "Back", (8, 4, 78, 26))


def hit_button(buttons: tuple[Button, ...], x: int, y: int) -> str | None:
    for button in buttons:
        bx, by, bw, bh = button.rect
        if bx <= x < bx + bw and by <= y < by + bh:
            return button.action
    return None


def read_cpu_temp_c(path: Path = TEMP_PATH) -> float | None:
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw.isdigit():
        return None
    return int(raw) / 1000.0


def status_line(stream_up: bool, url: str | None, error: str | None) -> str:
    if url:
        return url
    if error and stream_up:
        return error
    if stream_up:
        return "starting public link"
    return "idle"


class UiServer:
    def __init__(self, handler) -> None:
        self._handler = handler
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sock: socket.socket | None = None

    def start(self) -> None:
        path = ui_socket_path()
        try:
            path.unlink()
        except OSError:
            pass
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.bind(str(path))
        except OSError:
            log.exception("ui socket bind failed")
            sock.close()
            return
        sock.listen(2)
        sock.settimeout(0.4)
        self._sock = sock
        self._thread = threading.Thread(target=self._loop, name="cctv-ui-sock", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        sock = self._sock
        self._sock = None
        if sock is not None:
            sock.close()
        if self._thread is not None:
            self._thread.join(timeout=1)
        try:
            ui_socket_path().unlink()
        except OSError:
            pass

    def _loop(self) -> None:
        sock = self._sock
        if sock is None:
            return
        while not self._stop.is_set():
            try:
                conn, _addr = sock.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with conn:
                try:
                    data = b""
                    while b"\n" not in data:
                        chunk = conn.recv(256)
                        if not chunk:
                            break
                        data += chunk
                    command = data.decode("utf-8", errors="replace").strip()
                    reply = self._handler(command) if command else "err empty"
                    conn.sendall((reply + "\n").encode("utf-8"))
                except OSError:
                    log.debug("ui socket client failed", exc_info=True)


def _choose_driver() -> None:
    os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
    if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
        return
    os.environ["SDL_VIDEODRIVER"] = "kmsdrm"
    index = kmsdrm_device_index()
    if index is not None:
        os.environ["SDL_KMSDRM_DEVICE_INDEX"] = index


def _rotation_matches(pygame, rotate: int) -> bool:
    canvas = pygame.Surface((LOGICAL_W, LOGICAL_H))
    canvas.fill((0, 0, 0))
    canvas.set_at((10, 20), (1, 2, 3))
    frame = pygame.transform.rotate(canvas, rotate)
    px, py = logical_to_panel(10, 20, rotate)
    if px < 0 or py < 0 or px >= frame.get_width() or py >= frame.get_height():
        return False
    return tuple(frame.get_at((px, py))[:3]) == (1, 2, 3)


def _open_screen(pygame):
    flags = pygame.FULLSCREEN
    size = (PANEL_W, PANEL_H)
    try:
        return pygame.display.set_mode(size, flags)
    except pygame.error:
        log.exception("fullscreen %s failed, trying windowed", size)
        return pygame.display.set_mode((LOGICAL_W, LOGICAL_H))


def _present(pygame, screen, canvas, rotate: int) -> None:
    if rotate:
        frame = pygame.transform.rotate(canvas, rotate)
    else:
        frame = canvas
    if frame.get_size() != screen.get_size():
        frame = pygame.transform.scale(frame, screen.get_size())
    screen.blit(frame, (0, 0))
    pygame.display.flip()


def _fit(font, text: str, width: int) -> str:
    if font.size(text)[0] <= width:
        return text
    trimmed = text
    while trimmed and font.size(trimmed + "...")[0] > width:
        trimmed = trimmed[:-1]
    return trimmed + "..."


def _draw_icon(pygame, canvas, action: str, rect: tuple[int, int, int, int]) -> None:
    x, y, w, h = rect
    cx = x + w // 2
    cy = y + h // 2 - 12
    if action == "start":
        pygame.draw.circle(canvas, ACCENT, (cx, cy), 16, 2)
        pygame.draw.polygon(
            canvas,
            ACCENT,
            [(cx - 5, cy - 8), (cx - 5, cy + 8), (cx + 8, cy)],
        )
    elif action == "camera":
        pygame.draw.rect(canvas, ACCENT, (cx - 18, cy - 12, 36, 26), 2, border_radius=4)
        pygame.draw.circle(canvas, ACCENT, (cx, cy + 1), 7, 2)
        pygame.draw.rect(canvas, ACCENT, (cx - 8, cy - 16, 12, 5))
    elif action == "reboot":
        pygame.draw.arc(canvas, ACCENT, (cx - 16, cy - 16, 32, 32), 0.6, 5.4, 2)
        pygame.draw.polygon(canvas, ACCENT, [(cx + 12, cy - 12), (cx + 18, cy - 2), (cx + 6, cy - 2)])
    elif action == "shutdown":
        pygame.draw.circle(canvas, ACCENT, (cx, cy + 2), 14, 2)
        pygame.draw.line(canvas, BG, (cx, cy - 16), (cx, cy + 2), 4)
        pygame.draw.line(canvas, ACCENT, (cx, cy - 16), (cx, cy - 2), 2)
    elif action in {"yes", "no", "back"}:
        return


def _draw_button(pygame, canvas, font, button: Button) -> None:
    x, y, w, h = button.rect
    pygame.draw.rect(canvas, BTN_FACE, button.rect, border_radius=8)
    pygame.draw.rect(canvas, ACCENT, button.rect, 2, border_radius=8)
    _draw_icon(pygame, canvas, button.action, button.rect)
    label = font.render(button.label, True, FG)
    if button.action in {"yes", "no", "back"}:
        label_y = y + (h - label.get_height()) // 2
    else:
        label_y = y + h - label.get_height() - 10
    canvas.blit(label, (x + (w - label.get_width()) // 2, label_y))


def _draw_temp(canvas, font, temp_c: float | None) -> None:
    if temp_c is None:
        label = "--"
        colour = DIM
    else:
        label = f"{temp_c:.0f}°C"
        colour = HOT if temp_c >= HOT_C else ACCENT
    surf = font.render(label, True, colour)
    canvas.blit(surf, (LOGICAL_W - MARGIN - surf.get_width(), 6))


def _draw_home(pygame, canvas, title_font, label_font, small, state: UiState) -> None:
    canvas.fill(BG)
    title = title_font.render("CCTV", True, APP_NAME)
    canvas.blit(title, (MARGIN, 4))
    _draw_temp(canvas, small, state.temp_c)
    for button in home_buttons():
        _draw_button(pygame, canvas, label_font, button)
    bar = small.render(_fit(small, state.status, LOGICAL_W - MARGIN * 2), True, DIM)
    canvas.blit(bar, (MARGIN, LOGICAL_H - STATUS_H))


def _draw_camera(pygame, canvas, label_font, small, state: UiState, pump: FramePump, surface) -> None:
    canvas.fill(BG)
    _draw_button(pygame, canvas, small, back_button())
    _draw_temp(canvas, small, state.temp_c)
    dest_top = HEADER_H
    dest = (0, dest_top, LOGICAL_W, LOGICAL_H - dest_top)
    if surface is not None:
        sw, sh = surface.get_size()
        dw, dh = dest[2], dest[3]
        scale = min(dw / sw, dh / sh)
        size = (max(1, int(sw * scale)), max(1, int(sh * scale)))
        scaled = pygame.transform.scale(surface, size)
        x = (dw - size[0]) // 2
        y = dest_top + (dh - size[1]) // 2
        canvas.blit(scaled, (x, y))
    else:
        message = pump.error or "waiting for camera"
        text = label_font.render(message, True, FG)
        canvas.blit(
            text,
            ((LOGICAL_W - text.get_width()) // 2, (LOGICAL_H - text.get_height()) // 2),
        )


def _draw_confirm(pygame, canvas, title_font, label_font, small, state: UiState) -> None:
    canvas.fill(BG)
    _draw_temp(canvas, small, state.temp_c)
    question = "Shut down?" if state.confirm == "shutdown" else "Reboot?"
    text = title_font.render(question, True, FG)
    canvas.blit(text, ((LOGICAL_W - text.get_width()) // 2, LOGICAL_H // 2 - 70))
    for button in confirm_buttons():
        _draw_button(pygame, canvas, label_font, button)


def _refresh_status(state: UiState, now: float) -> None:
    if now - state.status_at >= STATUS_PERIOD_S:
        state.status_at = now
        state.stream_up = is_running()
    if state.starting or now < state.hold_until:
        return
    if state.stream_up:
        state.status = status_line(True, public_url(), tunnel_error())
    else:
        state.status = "idle"


def _refresh_temp(state: UiState, now: float) -> None:
    if now - state.temp_at < TEMP_PERIOD_S:
        return
    state.temp_at = now
    state.temp_c = read_cpu_temp_c()


def _begin_start(state: UiState, pump: FramePump) -> None:
    if state.starting:
        return
    pump.stop()
    state.starting = True
    state.page = "home"
    state.status = "starting stream"

    def worker() -> None:
        err = start_stream()
        if err:
            state.status = err
            state.hold_until = time.monotonic() + 8
        else:
            state.stream_up = True
            state.hold_until = 0.0
            state.status = status_line(True, public_url(), tunnel_error())
        state.starting = False

    threading.Thread(target=worker, name="cctv-start", daemon=True).start()


def _activate(state: UiState, pump: FramePump, action: str) -> None:
    if state.page == "confirm":
        if action == "yes" and state.confirm in {"reboot", "shutdown"}:
            if state.confirm == "shutdown":
                state.status = "shutting down"
                err = power_action("poweroff")
            else:
                state.status = "rebooting"
                err = power_action("reboot")
            if err:
                state.status = err
                state.hold_until = time.monotonic() + 8
                state.page = "home"
            return
        if action in {"no", "back"}:
            state.page = "home"
            state.confirm = None
        return
    if action == "back":
        pump.stop()
        state.page = "home"
        return
    if action == "start":
        _begin_start(state, pump)
    elif action == "camera":
        state.page = "camera"
    elif action == "reboot":
        state.confirm = "reboot"
        state.page = "confirm"
    elif action == "shutdown":
        state.confirm = "shutdown"
        state.page = "confirm"


def _handle_command(command: str, state: UiState, pump: FramePump) -> str:
    if command == "release":
        pump.stop()
        return "ok"
    if command == "camera":
        state.page = "camera"
        state.confirm = None
        return "ok"
    if command == "refresh":
        state.status_at = 0.0
        return "ok"
    if command == "link":
        return format_link().strip() or "ok"
    return "err unknown"


def _pointer_from_window(x: int, y: int, window: tuple[int, int], rotate: int) -> tuple[int, int]:
    if window == (PANEL_W, PANEL_H):
        return panel_to_logical(x, y, rotate)
    return x, y


def _active_buttons(state: UiState) -> tuple[Button, ...]:
    if state.page == "confirm":
        return confirm_buttons()
    if state.page == "camera":
        return (back_button(),)
    return home_buttons()


def _on_key(state: UiState, pump: FramePump, key: int, pygame) -> None:
    if key == pygame.K_ESCAPE:
        _activate(state, pump, "back")
        return
    if state.page == "confirm":
        if key in {pygame.K_y, pygame.K_RETURN, pygame.K_KP_ENTER}:
            _activate(state, pump, "yes")
        elif key == pygame.K_n:
            _activate(state, pump, "no")
        return
    keys = {
        pygame.K_1: "start",
        pygame.K_KP1: "start",
        pygame.K_2: "camera",
        pygame.K_KP2: "camera",
        pygame.K_3: "reboot",
        pygame.K_KP3: "reboot",
        pygame.K_4: "shutdown",
        pygame.K_KP4: "shutdown",
    }
    action = keys.get(key)
    if action:
        _activate(state, pump, action)


def _jpeg_surface(pygame, jpeg: bytes, cache: dict):
    token = (len(jpeg), jpeg[:16], jpeg[-16:])
    if cache.get("token") == token:
        return cache.get("surface")
    try:
        surface = pygame.image.load(io.BytesIO(jpeg), "preview.jpg")
    except pygame.error:
        log.debug("jpeg decode failed", exc_info=True)
        return cache.get("surface")
    cache["token"] = token
    cache["surface"] = surface
    return surface


def _setup_logging() -> None:
    path = state_dir() / "ui.log"
    logging.basicConfig(
        filename=str(path),
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )


def run() -> int:
    _setup_logging()
    log.info("cctv ui starting")
    _choose_driver()
    try:
        import pygame
    except ImportError:
        print("pygame is not installed. On the Pi: sudo apt install python3-pygame", file=sys.stderr)
        return 1
    pygame.init()
    pygame.mouse.set_visible(False)
    try:
        screen = _open_screen(pygame)
    except pygame.error as exc:
        log.exception("display open failed")
        print(f"could not open the panel: {exc}", file=sys.stderr)
        pygame.quit()
        return 1
    window = screen.get_size()
    rotate = PRESENT_ROTATE if window == (PANEL_W, PANEL_H) else 0
    if rotate and not _rotation_matches(pygame, rotate):
        log.warning("touch mapping does not match pygame rotate %s", rotate)
    else:
        log.info("panel %sx%s rotate %s", window[0], window[1], rotate)
    canvas = pygame.Surface((LOGICAL_W, LOGICAL_H))
    title_font = load_font(pygame, 26)
    label_font = load_font(pygame, 20)
    small = load_font(pygame, 16)
    clock = pygame.time.Clock()
    state = UiState(rotate=rotate)
    pump = FramePump()
    touch = TouchReader()
    touch.open()
    server = UiServer(lambda command: _handle_command(command, state, pump))
    server.start()
    cache: dict = {}
    try:
        while True:
            if skip_path().is_file():
                log.info("skip-ui set, leaving the panel")
                return 0
            now = time.monotonic()
            _refresh_temp(state, now)
            _refresh_status(state, now)
            if (
                not touch.ready
                and not state.starting
                and now >= state.hold_until
                and not state.stream_up
                and state.page == "home"
            ):
                state.status = "touch bus is off"
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    return 0
                if event.type == pygame.KEYDOWN:
                    _on_key(state, pump, event.key, pygame)
                elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                    lx, ly = _pointer_from_window(event.pos[0], event.pos[1], window, rotate)
                    action = hit_button(_active_buttons(state), lx, ly)
                    if action:
                        _activate(state, pump, action)
                elif event.type == pygame.FINGERDOWN:
                    lx, ly = _pointer_from_window(
                        int(event.x * max(window[0] - 1, 1)),
                        int(event.y * max(window[1] - 1, 1)),
                        window,
                        rotate,
                    )
                    action = hit_button(_active_buttons(state), lx, ly)
                    if action:
                        _activate(state, pump, action)
            point = touch.poll()
            if point is not None:
                lx, ly = panel_to_logical(point[0], point[1], rotate)
                log.info("touch panel=%s logical=%s", point, (lx, ly))
                action = hit_button(_active_buttons(state), lx, ly)
                if action:
                    _activate(state, pump, action)
            if state.page == "camera":
                pump.sync(state.stream_up)
                jpeg = pump.jpeg()
                surface = _jpeg_surface(pygame, jpeg, cache) if jpeg else None
                _draw_camera(pygame, canvas, label_font, small, state, pump, surface)
            else:
                if pump.active():
                    pump.stop()
                if state.page == "confirm":
                    _draw_confirm(pygame, canvas, title_font, label_font, small, state)
                else:
                    _draw_home(pygame, canvas, title_font, label_font, small, state)
            _present(pygame, screen, canvas, rotate)
            clock.tick(20)
    finally:
        pump.stop()
        server.close()
        touch.close()
        pygame.quit()
