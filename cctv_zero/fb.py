"""RGB565 blit for the PiTFT framebuffer."""

from __future__ import annotations

import mmap
import subprocess
from pathlib import Path

from PIL import Image

FB_CANDIDATES = (Path("/dev/fb1"), Path("/dev/fb0"))


def framebuffer_size(fb: Path) -> tuple[int, int] | None:
    try:
        out = subprocess.check_output(["fbset", "-fb", str(fb), "-s"], text=True, timeout=2)
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        if "geometry" not in line:
            continue
        parts = line.split()
        if len(parts) >= 3 and parts[1].isdigit() and parts[2].isdigit():
            return int(parts[1]), int(parts[2])
    return None


def find_framebuffer() -> Path | None:
    """Prefer the 320x240 PiTFT when several framebuffers exist."""
    found: list[Path] = []
    for path in FB_CANDIDATES:
        if path.exists():
            found.append(path)
    for path in found:
        size = framebuffer_size(path)
        if size == (320, 240):
            return path
    return found[0] if found else None


def image_to_rgb565(img: Image.Image) -> bytes:
    rgb = img.convert("RGB")
    try:
        import numpy as np
    except ImportError:
        px = rgb.tobytes()
        out = bytearray((len(px) // 3) * 2)
        j = 0
        for i in range(0, len(px), 3):
            red, green, blue = px[i], px[i + 1], px[i + 2]
            val = ((red & 0xF8) << 8) | ((green & 0xFC) << 3) | (blue >> 3)
            out[j] = val & 0xFF
            out[j + 1] = (val >> 8) & 0xFF
            j += 2
        return bytes(out)
    arr = np.asarray(rgb, dtype=np.uint16)
    red = arr[:, :, 0]
    green = arr[:, :, 1]
    blue = arr[:, :, 2]
    vals = ((red & 0xF8) << 8) | ((green & 0xFC) << 3) | (blue >> 3)
    return vals.astype("<u2").tobytes()


def blit(fb: Path, img: Image.Image) -> None:
    data = image_to_rgb565(img)
    with fb.open("r+b", buffering=0) as handle:
        try:
            mapped = mmap.mmap(handle.fileno(), len(data), access=mmap.ACCESS_WRITE)
        except (ValueError, OSError):
            handle.seek(0)
            handle.write(data)
            return
        try:
            mapped[: len(data)] = data
        finally:
            mapped.close()
