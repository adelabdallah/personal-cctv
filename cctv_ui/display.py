"""Map the landscape UI onto the portrait SPI panel."""

from __future__ import annotations

from pathlib import Path

from cctv_ui.theme import LOGICAL_H, LOGICAL_W, PANEL_H, PANEL_W, PRESENT_ROTATE

# GPIO backlight is on or off. brightness is writable by the video group.
# bl_power is root-only.
BACKLIGHT_BRIGHTNESS = Path("/sys/class/backlight/backlight_gpio/brightness")


def logical_to_panel(lx: int, ly: int, rotate: int = PRESENT_ROTATE) -> tuple[int, int]:
    """Map a logical 480x320 pixel into the 320x480 framebuffer."""
    if rotate == 270:
        return PANEL_W - 1 - ly, lx
    if rotate == 90:
        return ly, LOGICAL_W - 1 - lx
    return lx, ly


def panel_to_logical(px: int, py: int, rotate: int = PRESENT_ROTATE) -> tuple[int, int]:
    """Inverse of logical_to_panel. Touch hardware reports panel pixels."""
    if rotate == 270:
        return py, PANEL_W - 1 - px
    if rotate == 90:
        return LOGICAL_W - 1 - py, px
    return px, py


def set_backlight(on: bool, path: Path = BACKLIGHT_BRIGHTNESS) -> bool:
    try:
        path.write_text("1\n" if on else "0\n", encoding="utf-8")
    except OSError:
        return False
    return True


def kmsdrm_device_index(drm_root: Path | None = None) -> str | None:
    """Pick the DRM card whose connector is the SPI panel, not HDMI."""
    root = drm_root or Path("/sys/class/drm")
    if not root.is_dir():
        return None
    connected: list[tuple[int, str]] = []
    for status_path in sorted(root.glob("card*-*/status")):
        try:
            status = status_path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if status != "connected":
            continue
        name = status_path.parent.name
        if not name.startswith("card") or "-" not in name:
            continue
        card, connector = name.split("-", 1)
        try:
            index = int(card[4:])
        except ValueError:
            continue
        if connector.startswith("Writeback"):
            continue
        connected.append((index, connector))
    for index, connector in connected:
        if connector.startswith("SPI") or connector.startswith("DPI"):
            return str(index)
    for index, connector in connected:
        if not connector.startswith("HDMI"):
            return str(index)
    if connected:
        return str(connected[0][0])
    return None
