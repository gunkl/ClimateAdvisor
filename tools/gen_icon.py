#!/usr/bin/env python3
"""Generate the Climate Advisor integration icon.

Creates icon.png (256x256) and icon@2x.png (512x512) in the
custom_components/climate_advisor/ directory.

Usage:
    python tools/gen_icon.py
"""

import math
from pathlib import Path

from PIL import Image, ImageDraw

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = REPO_ROOT / "custom_components" / "climate_advisor"
SIZE = 512  # work at 2x, then downscale


def lerp_color(c1: tuple, c2: tuple, t: float) -> tuple:
    """Linearly interpolate between two RGBA colors."""
    return tuple(int(a + (b - a) * t) for a, b in zip(c1, c2))


def draw_rounded_rect(draw, bbox, radius, fill):
    """Draw a filled rounded rectangle."""
    x0, y0, x1, y1 = bbox
    draw.rectangle([x0 + radius, y0, x1 - radius, y1], fill=fill)
    draw.rectangle([x0, y0 + radius, x1, y1 - radius], fill=fill)
    draw.pieslice([x0, y0, x0 + 2 * radius, y0 + 2 * radius], 180, 270, fill=fill)
    draw.pieslice([x1 - 2 * radius, y0, x1, y0 + 2 * radius], 270, 360, fill=fill)
    draw.pieslice([x0, y1 - 2 * radius, x0 + 2 * radius, y1], 90, 180, fill=fill)
    draw.pieslice([x1 - 2 * radius, y1 - 2 * radius, x1, y1], 0, 90, fill=fill)


def make_gradient_bg(size: int) -> Image.Image:
    """Create a rounded-rect background with a cool-blue to warm-orange gradient."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Colors: cool blue (top) → warm orange (bottom)
    top = (41, 128, 185, 255)     # steel blue
    bottom = (230, 126, 34, 255)  # carrot orange

    radius = size // 8
    margin = size // 32

    # Draw gradient line by line inside rounded rect bounds
    for y in range(margin, size - margin):
        t = (y - margin) / (size - 2 * margin)
        color = lerp_color(top, bottom, t)
        draw.line([(margin, y), (size - margin - 1, y)], fill=color)

    # Mask to rounded rect shape
    mask = Image.new("L", (size, size), 0)
    mask_draw = ImageDraw.Draw(mask)
    draw_rounded_rect(mask_draw, (margin, margin, size - margin, size - margin),
                      radius, fill=255)
    img.putalpha(mask)
    return img


def draw_thermometer(draw, cx: int, cy: int, scale: int):
    """Draw a simple thermometer shape in white."""
    white = (255, 255, 255, 255)
    white_semi = (255, 255, 255, 200)

    # Thermometer body (vertical rounded bar)
    body_w = scale // 5
    body_h = scale
    body_top = cy - body_h // 2 - scale // 8
    body_left = cx - body_w // 2

    # Stem (rectangular part)
    draw.rounded_rectangle(
        [body_left, body_top, body_left + body_w, cy + scale // 8],
        radius=body_w // 2,
        fill=white,
    )

    # Bulb at bottom
    bulb_r = body_w * 3 // 4
    bulb_cy = cy + scale // 4
    draw.ellipse(
        [cx - bulb_r, bulb_cy - bulb_r, cx + bulb_r, bulb_cy + bulb_r],
        fill=white,
    )

    # Mercury level (colored fill inside)
    mercury_color = (231, 76, 60, 255)  # warm red
    inner_w = body_w // 2
    mercury_top = cy - scale // 6
    draw.rectangle(
        [cx - inner_w // 2, mercury_top, cx + inner_w // 2, bulb_cy],
        fill=mercury_color,
    )
    inner_bulb_r = bulb_r * 2 // 3
    draw.ellipse(
        [cx - inner_bulb_r, bulb_cy - inner_bulb_r,
         cx + inner_bulb_r, bulb_cy + inner_bulb_r],
        fill=mercury_color,
    )

    # Tick marks on the right side of the stem
    for i in range(4):
        tick_y = body_top + scale // 6 + i * (scale // 5)
        tick_x1 = cx + body_w // 2 - 2
        tick_x2 = tick_x1 + body_w // 3
        draw.line([(tick_x1, tick_y), (tick_x2, tick_y)], fill=white_semi, width=3)


def draw_gear(draw, cx: int, cy: int, r: int):
    """Draw a small gear/cog symbol."""
    white = (255, 255, 255, 230)
    teeth = 8
    outer_r = r
    inner_r = int(r * 0.7)


    # Draw gear teeth as small rectangles around a circle
    for i in range(teeth):
        angle = 2 * math.pi * i / teeth
        # Tooth tip
        tx = cx + int(outer_r * math.cos(angle))
        ty = cy + int(outer_r * math.sin(angle))
        draw.ellipse([tx - 4, ty - 4, tx + 4, ty + 4], fill=white)

    # Gear body circle
    draw.ellipse([cx - inner_r, cy - inner_r, cx + inner_r, cy + inner_r], fill=white)

    # Center hole
    hole_r = inner_r // 2
    draw.ellipse([cx - hole_r, cy - hole_r, cx + hole_r, cy + hole_r],
                 fill=(0, 0, 0, 0))


def generate():
    """Generate the Climate Advisor icon."""
    img = make_gradient_bg(SIZE)
    draw = ImageDraw.Draw(img)

    # Draw thermometer slightly left of center
    thermo_cx = SIZE // 2 - SIZE // 16
    thermo_cy = SIZE // 2
    draw_thermometer(draw, thermo_cx, thermo_cy, SIZE // 3)

    # Draw small gear in the upper-right area (conveying "smart/adaptive")
    gear_cx = SIZE // 2 + SIZE // 5
    gear_cy = SIZE // 2 - SIZE // 6
    draw_gear(draw, gear_cx, gear_cy, SIZE // 12)

    # Save 2x
    img.save(OUT_DIR / "icon@2x.png", "PNG")
    print(f"Saved: {OUT_DIR / 'icon@2x.png'} ({SIZE}x{SIZE})")

    # Save 1x (downscaled)
    img_1x = img.resize((256, 256), Image.LANCZOS)
    img_1x.save(OUT_DIR / "icon.png", "PNG")
    print(f"Saved: {OUT_DIR / 'icon.png'} (256x256)")


if __name__ == "__main__":
    generate()
