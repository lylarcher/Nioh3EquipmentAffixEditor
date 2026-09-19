# -*- coding: utf-8 -*-
"""Generate the application icon and logo (original geometric art, stdlib only).

The motif is a **勾玉 (magatama)** -- the comma-shaped jewel that reads as
Japanese and as "treasure/amulet", which is what this editor works on -- drawn in
gold on a dark ink chip.  Nothing here is copied from the game: every shape is
computed from circles, so the art is reproducible, licence-free and reviewable in
code instead of being an unexplained binary blob.

Geometry of the jewel: the area inside a head circle of radius ``R`` minus a
*internally tangent* bite circle of radius ``r``, with ``|head - bite| = R - r``.
Tangency is what makes it a magatama rather than a crescent: the two boundaries
meet at a single point, so the jewel has one fat round head and a tail that
tapers to a point.

    assets/app.ico        16/20/24/32/40/48/64/128/256 px, used as the exe icon
    assets/logo.png       256 px logo, transparent outside the rounded chip
    assets/logo-64.png    64 px, native resolution (no resampling in the GUI)
    assets/logo-32.png    32 px, native resolution (header logo)

Usage:
    python tools/make_icon.py                 # write assets/
    python tools/make_icon.py --check         # verify assets/ matches this script
    python tools/make_icon.py --preview p.png # contact sheet for visual review
"""

from __future__ import annotations

import argparse
import json
import math
import struct
import sys
import zlib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ASSETS_DIR = PROJECT_ROOT / "assets"

#: Sizes stored inside the .ico (Windows picks the closest one).
ICON_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)
#: Sizes stored as PNG inside the .ico (Vista+); the rest use plain BMP (DIB).
PNG_ENTRY_SIZES = (128, 256)
#: Standalone logo files: name -> pixel size.
LOGO_FILES = {"logo.png": 256, "logo-64.png": 64, "logo-32.png": 32}

ICON_NAME = "app.ico"

#: Palette: dark ink chip, gold jewel, one red seal accent (very 仁王).
INK_TOP = (0x26, 0x30, 0x46)
INK_BOTTOM = (0x09, 0x0C, 0x14)
FRAME_GOLD = (0xC9, 0xA2, 0x27)
GOLD_LIGHT = (0xFF, 0xEC, 0xA6)
GOLD_MID = (0xE9, 0xC4, 0x4E)
GOLD_DARK = (0x76, 0x53, 0x0A)
RED_SEAL = (0xB4, 0x2A, 0x2A)

#: Anti-aliasing factor: geometry is evaluated at size*SUPERSAMPLE and boxed down.
SUPERSAMPLE = 4

#: Tail direction (unit vector): the jewel's head is up-right, its tail points
#: down-left, which is how a magatama is normally drawn.
TAIL = (-0.7071, 0.7071)


# --------------------------------------------------------------------------
# Raster helpers
# --------------------------------------------------------------------------

class Canvas:
    """An RGBA raster with float colour and source-over compositing."""

    __slots__ = ("width", "height", "pixels")

    def __init__(self, width: int, height: int) -> None:
        self.width = width
        self.height = height
        self.pixels = [[0.0, 0.0, 0.0, 0.0] for _ in range(width * height)]

    def blend(self, x: int, y: int, color: tuple[float, float, float],
              alpha: float) -> None:
        if alpha <= 0.0:
            return
        if alpha > 1.0:
            alpha = 1.0
        destination = self.pixels[y * self.width + x]
        inverse = 1.0 - alpha
        destination[0] = color[0] * alpha + destination[0] * inverse
        destination[1] = color[1] * alpha + destination[1] * inverse
        destination[2] = color[2] * alpha + destination[2] * inverse
        destination[3] = alpha + destination[3] * inverse

    def box_downsample(self, factor: int) -> "Canvas":
        """Average factor x factor blocks -- this is the anti-aliasing step."""
        width, height = self.width // factor, self.height // factor
        result = Canvas(width, height)
        area = float(factor * factor)
        for y in range(height):
            for x in range(width):
                red = green = blue = alpha = 0.0
                for dy in range(factor):
                    row = (y * factor + dy) * self.width + x * factor
                    for dx in range(factor):
                        pixel = self.pixels[row + dx]
                        red += pixel[0]
                        green += pixel[1]
                        blue += pixel[2]
                        alpha += pixel[3]
                result.pixels[y * width + x] = [red / area, green / area,
                                                blue / area, alpha / area]
        return result

    def rgba_rows(self) -> bytes:
        """Encode as raw RGBA scan lines with the PNG filter byte prepended."""
        out = bytearray()
        for y in range(self.height):
            out.append(0)  # filter type 0 (None)
            for x in range(self.width):
                red, green, blue, alpha = self.pixels[y * self.width + x]
                out += bytes((_clamp8(red), _clamp8(green), _clamp8(blue),
                              _clamp8(alpha)))
        return bytes(out)


def _clamp8(value: float) -> int:
    return 0 if value <= 0.0 else (255 if value >= 255.0 else int(value + 0.5))


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def _mix(color_a, color_b, t: float) -> tuple[float, float, float]:
    return (_lerp(color_a[0], color_b[0], t),
            _lerp(color_a[1], color_b[1], t),
            _lerp(color_a[2], color_b[2], t))


def _smoothstep(edge0: float, edge1: float, value: float) -> float:
    if edge1 == edge0:
        return 0.0 if value < edge0 else 1.0
    t = (value - edge0) / (edge1 - edge0)
    t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
    return t * t * (3.0 - 2.0 * t)


def _distance(x: float, y: float, cx: float, cy: float) -> float:
    return math.hypot(x - cx, y - cy)


# --------------------------------------------------------------------------
# Geometry
# --------------------------------------------------------------------------

def _rounded_square_inside(x: float, y: float, inset: float, radius: float) -> bool:
    """Is the point inside a rounded square of the unit square?"""
    left, top = inset, inset
    right, bottom = 1.0 - inset, 1.0 - inset
    if x < left or x > right or y < top or y > bottom:
        return False
    dx = min(x - left, right - x)
    dy = min(y - top, bottom - y)
    if dx >= radius or dy >= radius:
        return True
    corner_x = left + radius if x - left < radius else right - radius
    corner_y = top + radius if y - top < radius else bottom - radius
    return _distance(x, y, corner_x, corner_y) <= radius


def _parameters(size: int) -> dict:
    """Size-dependent geometry: small icons need a chunkier, simpler jewel."""
    # t = 0 at 16 px (little room), 1 from 96 px up (full detail).
    t = _smoothstep(16.0, 96.0, float(size))

    # Small icons are drawn slightly larger and much bolder: a 16 px taskbar
    # entry needs a chunky comma, not a thin ring (measured in tests/test_icon.py).
    radius = _lerp(0.320, 0.315, t)                 # head circle radius R
    thickness = _lerp(0.235, 0.175, t)              # ring thickness at the head
    # |head - bite| = tangency * (R - r); slightly less than 1 rounds the tail.
    tangency = _lerp(0.72, 0.93, t)
    gap = thickness / (1.0 + tangency)              # R - r
    bite_radius = radius - gap

    head_x, head_y = 0.560, 0.395
    bite_x = head_x + TAIL[0] * gap * tangency
    bite_y = head_y + TAIL[1] * gap * tangency

    # Cord hole near the head: only where there are pixels to spare, and only
    # when it leaves a visible rim on both sides of the ring.
    hole_radius = 0.046 if size >= 40 else 0.0
    hole_x = hole_radius and head_x - TAIL[0] * (radius - thickness * 0.55)
    hole_y = hole_radius and head_y - TAIL[1] * (radius - thickness * 0.55)

    return {
        "chip": _lerp(0.0, 0.020, t),
        "chip_radius": _lerp(0.17, 0.21, t),
        "frame": 0.017 if size >= 32 else 0.0,
        "head_x": head_x,
        "head_y": head_y,
        "radius": radius,
        "bite_x": bite_x,
        "bite_y": bite_y,
        "bite_radius": bite_radius,
        "hole_x": hole_x or 0.0,
        "hole_y": hole_y or 0.0,
        "hole_radius": hole_radius,
        "bevel": _lerp(0.028, 0.050, t),
        "tail_tip_x": head_x + TAIL[0] * radius,
        "tail_tip_y": head_y + TAIL[1] * radius,
        "seal": size >= 32,
    }


def _jewel_depth(x: float, y: float, params: dict) -> float:
    """Distance from the point to the jewel's silhouette (<=0 when outside)."""
    head = _distance(x, y, params["head_x"], params["head_y"])
    bite = _distance(x, y, params["bite_x"], params["bite_y"])
    depth = min(params["radius"] - head, bite - params["bite_radius"])
    if params["hole_radius"] > 0.0:
        hole = _distance(x, y, params["hole_x"], params["hole_y"])
        depth = min(depth, hole - params["hole_radius"])
    return depth


# --------------------------------------------------------------------------
# Painting
# --------------------------------------------------------------------------

def render(size: int) -> bytes:
    """Render one square icon as RGBA bytes (``size * size * 4``)."""
    factor = SUPERSAMPLE
    big = size * factor
    canvas = Canvas(big, big)
    params = _parameters(size)
    step = 1.0 / big

    # Pass 1: the chip (background, sheen, gold frame).
    for row in range(big):
        y = (row + 0.5) * step
        for column in range(big):
            x = (column + 0.5) * step
            if not _rounded_square_inside(x, y, params["chip"], params["chip_radius"]):
                continue
            ink = _mix(INK_TOP, INK_BOTTOM, _smoothstep(0.0, 1.0, y))
            sheen = 0.12 * _smoothstep(0.9, 0.0, _distance(x, y, 0.26, 0.20))
            canvas.blend(column, row,
                         (min(255.0, ink[0] * (1.0 + sheen)),
                          min(255.0, ink[1] * (1.0 + sheen)),
                          min(255.0, ink[2] * (1.0 + sheen))), 1.0)
            if params["frame"] > 0.0 and not _rounded_square_inside(
                    x, y, params["chip"] + params["frame"],
                    max(0.01, params["chip_radius"] - params["frame"] * 0.6)):
                canvas.blend(column, row, FRAME_GOLD, 1.0)

    # Pass 2: the red seal (a stamped corner mark, behind the jewel).
    if params["seal"]:
        seal_x, seal_y, seal_radius = 0.225, 0.780, 0.072
        for row in range(big):
            y = (row + 0.5) * step
            for column in range(big):
                x = (column + 0.5) * step
                seal = _distance(x, y, seal_x, seal_y)
                if seal > seal_radius:
                    continue
                if not _rounded_square_inside(x, y, params["chip"],
                                              params["chip_radius"]):
                    continue
                alpha = 1.0 - _smoothstep(seal_radius - 0.012, seal_radius, seal)
                canvas.blend(column, row, RED_SEAL, alpha * 0.85)

    # Pass 3: the jewel.
    for row in range(big):
        y = (row + 0.5) * step
        for column in range(big):
            x = (column + 0.5) * step
            if not _rounded_square_inside(x, y, params["chip"], params["chip_radius"]):
                continue
            depth = _jewel_depth(x, y, params)
            if depth <= 0.0:
                continue

            # Bevel: bright in the belly, darkening towards the silhouette.
            height = _smoothstep(0.0, params["bevel"], depth)
            color = _mix(GOLD_DARK, GOLD_MID, height)
            # Broad glint on the fat head, plus a light line along the inner
            # (bite) curve so the jewel reads as a rounded, polished bead.
            glint = math.exp(-((x - 0.53) ** 2 + (y - 0.30) ** 2) / 0.020)
            color = _mix(color, GOLD_LIGHT, min(1.0, glint * 0.75))
            inner_curve = abs(_distance(x, y, params["bite_x"], params["bite_y"])
                              - params["bite_radius"])
            rim = _smoothstep(0.024, 0.0, inner_curve - 0.012)
            color = _mix(color, GOLD_LIGHT, rim * 0.30)
            canvas.blend(column, row, color, 1.0)

    return _flatten(canvas.box_downsample(factor))


def _flatten(canvas: Canvas) -> bytes:
    """Return plain RGBA bytes for a finished (not composited) canvas."""
    out = bytearray()
    for red, green, blue, alpha in canvas.pixels:
        out += bytes((_clamp8(red), _clamp8(green), _clamp8(blue),
                      _clamp8(alpha * 255.0)))
    return bytes(out)


# --------------------------------------------------------------------------
# File formats
# --------------------------------------------------------------------------

def _png_chunk(tag: bytes, payload: bytes) -> bytes:
    return (struct.pack(">I", len(payload)) + tag + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))


def encode_png(width: int, height: int, filtered_rows: bytes) -> bytes:
    """Encode raw filtered scan lines as a PNG (no timestamps: reproducible)."""
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)  # 8-bit RGBA
    return (b"\x89PNG\r\n\x1a\n"
            + _png_chunk(b"IHDR", header)
            + _png_chunk(b"IDAT", zlib.compress(filtered_rows, 9))
            + _png_chunk(b"IEND", b""))


def png_bytes(size: int, rgba: bytes | None = None) -> bytes:
    """Encode one rendered square as PNG."""
    rgba = render(size) if rgba is None else rgba
    rows = bytearray()
    stride = size * 4
    for row in range(size):
        rows.append(0)
        rows += rgba[row * stride:(row + 1) * stride]
    return encode_png(size, size, bytes(rows))


def bmp_entry(size: int, rgba: bytes) -> bytes:
    """Encode one icon image as a 32-bit DIB (what an .ico stores)."""
    header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0,
                         size * size * 4, 0, 0, 0, 0)
    pixels = bytearray()
    for row in range(size - 1, -1, -1):  # DIB rows are bottom-up
        for column in range(size):
            index = (row * size + column) * 4
            red, green, blue, alpha = rgba[index:index + 4]
            pixels += bytes((blue, green, red, alpha))
    # AND mask: unused (the 32-bit alpha wins) but every consumer expects it.
    mask_stride = ((size + 31) // 32) * 4
    return bytes(header) + bytes(pixels) + bytes(mask_stride * size)


def ico_bytes(sizes: tuple[int, ...] = ICON_SIZES) -> bytes:
    """Assemble a multi-resolution .ico (BMP for small sizes, PNG for large)."""
    images: list[tuple[int, bytes]] = []
    for size in sizes:
        rgba = render(size)
        if size in PNG_ENTRY_SIZES:
            images.append((size, png_bytes(size, rgba)))
        else:
            images.append((size, bmp_entry(size, rgba)))

    directory = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries = bytearray()
    payload = bytearray()
    for size, data in images:
        entries += struct.pack("<BBBBHHII",
                               0 if size >= 256 else size,
                               0 if size >= 256 else size,
                               0, 0, 1, 32, len(data), offset)
        payload += data
        offset += len(data)
    return bytes(directory + entries + payload)


def contact_sheet(path: Path, magnify_under: int = 64, scale: int = 4,
                  background: tuple[int, int, int] = (0x2B, 0x2B, 0x2B)) -> Path:
    """Write a preview: every icon size, small ones magnified for inspection."""
    tiles = [(size, render(size)) for size in ICON_SIZES]
    drawn = [size if size >= magnify_under else size * scale for size, _ in tiles]
    gap = 8
    width = gap + sum(tile + gap for tile in drawn)
    height = gap + max(drawn) + gap
    canvas = Canvas(width, height)
    for index in range(width * height):
        canvas.pixels[index] = [float(background[0]), float(background[1]),
                                float(background[2]), 255.0]
    x = gap
    for (size, rgba), tile in zip(tiles, drawn):
        for row in range(tile):
            for column in range(tile):
                source_row, source_column = row * size // tile, column * size // tile
                index = (source_row * size + source_column) * 4
                red, green, blue, alpha = rgba[index:index + 4]
                canvas.pixels[(row + gap) * width + x + column] = [
                    float(red), float(green), float(blue), float(alpha)]
        x += tile + gap
    path.write_bytes(encode_png(width, height, canvas.rgba_rows()))
    return path


# --------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------

def artifacts() -> dict[str, bytes]:
    """Everything this generator owns, as ``{file name: bytes}``."""
    files = {ICON_NAME: ico_bytes()}
    for name, size in LOGO_FILES.items():
        files[name] = png_bytes(size)
    return files


def write_assets(directory: Path | None = None) -> list[Path]:
    directory = Path(directory) if directory is not None else ASSETS_DIR
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    for name, data in sorted(artifacts().items()):
        target = directory / name
        target.write_bytes(data)
        written.append(target)
    return written


def check_assets(directory: Path | None = None) -> list[str]:
    """Report every shipped asset this script would not reproduce."""
    directory = Path(directory) if directory is not None else ASSETS_DIR
    problems: list[str] = []
    for name, data in sorted(artifacts().items()):
        target = directory / name
        if not target.is_file():
            problems.append(f"缺少 {name}（运行 python tools/make_icon.py 生成）")
        elif target.read_bytes() != data:
            problems.append(f"{name} 与生成器输出不一致（重新生成或说明手工改动）")
    return problems


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="生成程序图标与 logo")
    parser.add_argument("--output-dir", default=str(ASSETS_DIR))
    parser.add_argument("--check", action="store_true",
                        help="只校验 assets/ 与生成器一致（不写入）")
    parser.add_argument("--preview", default=None, metavar="PNG",
                        help="输出各尺寸预览图（人工核对用）")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.preview:
        print(f"预览图: {contact_sheet(Path(args.preview))}")
        return 0

    if args.check:
        problems = check_assets(Path(args.output_dir))
        if args.json:
            print(json.dumps({"problems": problems}, ensure_ascii=False, indent=2))
        elif problems:
            for problem in problems:
                print(f"错误: {problem}", file=sys.stderr)
        else:
            print("OK: assets/ 与生成器一致")
        return 1 if problems else 0

    written = write_assets(Path(args.output_dir))
    if args.json:
        print(json.dumps({"written": [str(path) for path in written]},
                         ensure_ascii=False, indent=2))
    else:
        for path in written:
            print(f"已生成 {path.name} ({path.stat().st_size} B)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
