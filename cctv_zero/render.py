"""Draw the 320x240 menu."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

BG = (4, 10, 18)
FG = (236, 240, 244)
MUTED = (140, 150, 165)
SELECT_BG = (16, 36, 56)
LEGEND_BG = (8, 16, 28)
BORDER = (30, 50, 70)

HEADER_H = 28
LEGEND_H = 22
ROW_H = 36


def _font(size: int) -> ImageFont.ImageFont:
    for path in (
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"),
    ):
        if path.is_file():
            return ImageFont.truetype(str(path), size)
    return ImageFont.load_default()


def _fit(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, width: int) -> str:
    if draw.textlength(text, font=font) <= width:
        return text
    trimmed = text
    while trimmed and draw.textlength(trimmed + "…", font=font) > width:
        trimmed = trimmed[:-1]
    return trimmed + "…" if trimmed else ""


def _legend(draw: ImageDraw.ImageDraw, width: int, height: int, labels: tuple[str, str, str, str]) -> None:
    top = height - LEGEND_H
    draw.rectangle((0, top, width, height), fill=LEGEND_BG)
    draw.line((0, top, width, top), fill=BORDER)
    font = _font(12)
    slot = width // 4
    for index, label in enumerate(labels):
        x = slot * index + 8
        draw.text((x, top + 4), label, fill=MUTED, font=font)


def paint_rows(
    width: int,
    height: int,
    title: str,
    labels: tuple[str, ...],
    selected: int,
    *,
    notice: str = "",
    legend: tuple[str, str, str, str],
) -> Image.Image:
    img = Image.new("RGB", (width, height), BG)
    draw = ImageDraw.Draw(img)
    title_font = _font(16)
    body = _font(18)
    small = _font(12)
    draw.text((8, 4), title, fill=FG, font=title_font)
    draw.line((0, HEADER_H, width, HEADER_H), fill=BORDER)
    top = HEADER_H + 6
    bottom = height - LEGEND_H - 4
    if notice:
        bottom -= 18
    for index, label in enumerate(labels):
        y = top + index * ROW_H
        if y + ROW_H > bottom:
            break
        if index == selected:
            draw.rectangle((6, y, width - 6, y + ROW_H - 4), fill=SELECT_BG)
        draw.text((14, y + 6), _fit(draw, label, body, width - 28), fill=FG, font=body)
    if notice:
        draw.text((8, height - LEGEND_H - 18), _fit(draw, notice, small, width - 16), fill=MUTED, font=small)
    _legend(draw, width, height, legend)
    return img


def paint_debug(
    width: int,
    height: int,
    *,
    facts: str,
    ssh_rows: list[str],
    log_rows: list[str],
) -> Image.Image:
    img = Image.new("RGB", (width, height), BG)
    draw = ImageDraw.Draw(img)
    title = _font(16)
    small = _font(12)
    body = _font(13)
    draw.text((8, 4), "Debug", fill=FG, font=title)
    draw.rectangle((0, HEADER_H, width, HEADER_H + 18), fill=LEGEND_BG)
    draw.text((6, HEADER_H + 2), _fit(draw, facts, small, width - 12), fill=MUTED, font=small)
    content_bottom = height - LEGEND_H
    split = HEADER_H + 20 + max(36, (content_bottom - HEADER_H - 20) // 2)
    draw.line((0, split, width, split), fill=BORDER)
    row_h = 15
    ssh_top = HEADER_H + 22
    if not ssh_rows:
        draw.text((8, ssh_top), "(no ssh)", fill=MUTED, font=body)
    else:
        visible = max(1, (split - ssh_top) // row_h)
        for index, line in enumerate(ssh_rows[:visible]):
            draw.text((8, ssh_top + index * row_h), _fit(draw, line, body, width - 16), fill=FG, font=body)
    log_top = split + 4
    if not log_rows:
        draw.text((8, log_top), "(button log)", fill=MUTED, font=small)
    else:
        visible = max(1, (content_bottom - log_top) // row_h)
        for index, line in enumerate(log_rows[-visible:]):
            draw.text((8, log_top + index * row_h), _fit(draw, line, small, width - 16), fill=MUTED, font=small)
    _legend(draw, width, height, ("·", "·", "back", "·"))
    return img


def paint_message(width: int, height: int, title: str, body: str) -> Image.Image:
    img = Image.new("RGB", (width, height), BG)
    draw = ImageDraw.Draw(img)
    draw.text((8, height // 2 - 24), title, fill=FG, font=_font(18))
    if body:
        draw.text((8, height // 2 + 4), body, fill=MUTED, font=_font(14))
    return img
