"""Colours shared with the cyberdeck kiosk, sized for the 3.5 inch panel."""

from __future__ import annotations

from pathlib import Path

BG = (2, 3, 4)
FG = (236, 252, 252)
ACCENT = (0, 232, 232)
APP_NAME = (255, 113, 206)
DIM = (78, 96, 102)
BTN_FACE = (10, 14, 16)
HOT = (255, 138, 64)
HOT_C = 70.0

LOGICAL_W = 480
LOGICAL_H = 320
PANEL_W = 320
PANEL_H = 480
# fbcon=rotate:1 is 90 degrees clockwise. pygame rotates counter-clockwise,
# so 270 degrees counter-clockwise matches the console.
PRESENT_ROTATE = 270

FONT_CANDIDATES = (
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    Path("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"),
)


def load_font(pygame_mod, size: int):
    for path in FONT_CANDIDATES:
        if path.is_file():
            return pygame_mod.font.Font(str(path), size)
    return pygame_mod.font.Font(None, size)
