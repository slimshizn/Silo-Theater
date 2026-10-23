#!/usr/bin/env python3
"""Draw the skin's shapes, masks and icons into resources/skins/Default/media.

Kodi skins are built from images: rounded corners, fades, focus rings and
icons all have to be bitmaps. This script draws them so they can be
regenerated exactly instead of edited by hand. It needs Pillow, which is a
development dependency only; the add-on itself never imports it.

Run: python3 scripts/build_textures.py
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parents[1]
MEDIA = ROOT / "resources/skins/Default/media"
SS = 4  # supersampling factor for smooth edges

WHITE = (255, 255, 255)


def _alpha(value):
    return max(0, min(255, int(round(value * 255))))


def _down(image, size):
    return image.resize(size, Image.LANCZOS)


def _ring_layer(size, shape, box, inner_box, radius, inner_radius, opacity):
    ring = Image.new("L", size, 0)
    ring_draw = ImageDraw.Draw(ring)
    if shape == "ellipse":
        ring_draw.ellipse(box, fill=255)
        ring_draw.ellipse(inner_box, fill=0)
    else:
        ring_draw.rounded_rectangle(box, radius, fill=255)
        ring_draw.rounded_rectangle(inner_box, inner_radius, fill=0)
    layer = Image.new("RGBA", size, WHITE + (0,))
    layer.putalpha(ring.point(lambda value: int(value * opacity)))
    return layer


def _shape(size, shape, radius=0, fill=1.0, line=0.0, line_width=0):
    width, height = size
    big = Image.new("RGBA", (width * SS, height * SS), WHITE + (0,))
    draw = ImageDraw.Draw(big)
    box = [0, 0, width * SS - 1, height * SS - 1]
    if fill:
        if shape == "ellipse":
            draw.ellipse(box, fill=WHITE + (_alpha(fill),))
        else:
            draw.rounded_rectangle(box, radius * SS, fill=WHITE + (_alpha(fill),))
    if line and line_width:
        inset = line_width * SS
        inner = [box[0] + inset, box[1] + inset, box[2] - inset, box[3] - inset]
        big = Image.alpha_composite(big, _ring_layer(
            big.size, shape, box, inner, radius * SS, max(0, radius - line_width) * SS, line))
    return _down(big, size)


def rounded(size, radius, fill=1.0, line=0.0, line_width=0):
    """A white rounded rectangle; fill and line are opacities, sizes in px."""
    return _shape(size, "rounded", radius, fill, line, line_width)


def ellipse(size, fill=1.0, line=0.0, line_width=0):
    return _shape(size, "ellipse", 0, fill, line, line_width)


def glow(size, radius, blur, opacity):
    """A soft white halo around a rounded rectangle inset by ``blur``."""
    width, height = size
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([blur, blur, width - blur - 1, height - blur - 1], radius, fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(blur / 2.5)).point(lambda value: int(value * opacity))
    image = Image.new("RGBA", size, WHITE + (0,))
    image.putalpha(mask)
    return image


def _curve(stops, length):
    """Piecewise smoothstep between (position, opacity) stops."""
    if not stops:
        return [1.0] * length
    values = []
    for index in range(length):
        position = index / max(1, length - 1)
        if position <= stops[0][0]:
            values.append(stops[0][1])
            continue
        value = stops[-1][1]
        for (left, left_value), (right, right_value) in zip(stops, stops[1:]):
            if left <= position <= right:
                eased = (position - left) / ((right - left) or 1)
                eased = eased * eased * (3 - 2 * eased)
                value = left_value + (right_value - left_value) * eased
                break
        values.append(value)
    return values


def gradient_mask(size, horizontal=(), vertical=()):
    """White image whose alpha is the product of the two axes' stops."""
    width, height = size
    across, down = _curve(horizontal, width), _curve(vertical, height)
    alpha = Image.new("L", size)
    alpha.putdata([_alpha(across[x] * down[y]) for y in range(height) for x in range(width)])
    image = Image.new("RGBA", size, WHITE + (0,))
    image.putalpha(alpha)
    return image


def vertical_panel(size, radius, top, bottom, line_top, line_bottom):
    """A dropdown panel: dark vertical gradient with a fading hairline."""
    width, height = size
    big_size = (width * SS, height * SS)
    shape = Image.new("L", big_size, 0)
    ImageDraw.Draw(shape).rounded_rectangle([0, 0, big_size[0] - 1, big_size[1] - 1], radius * SS, fill=255)
    rows = [tuple(int(top[c] + (bottom[c] - top[c]) * (y / (big_size[1] - 1))) for c in range(3)) + (255,)
            for y in range(big_size[1])]
    fill = Image.new("RGBA", big_size)
    fill.putdata([rows[y] for y in range(big_size[1]) for _ in range(big_size[0])])
    fill.putalpha(shape)
    ring = Image.new("L", big_size, 0)
    ring_draw = ImageDraw.Draw(ring)
    ring_draw.rounded_rectangle([0, 0, big_size[0] - 1, big_size[1] - 1], radius * SS, fill=255)
    ring_draw.rounded_rectangle([SS * 2, SS * 2, big_size[0] - 1 - SS * 2, big_size[1] - 1 - SS * 2],
                                (radius - 2) * SS, fill=0)
    fade = Image.new("L", big_size)
    fade.putdata([_alpha(line_top + (line_bottom - line_top) * (y / (big_size[1] - 1)))
                  for y in range(big_size[1]) for _ in range(big_size[0])])
    line = Image.new("RGBA", big_size, WHITE + (0,))
    line.putalpha(ImageChops.multiply(ring, fade))
    return _down(Image.alpha_composite(fill, line), size)


# ----------------------------------------------------------------- icons

OPAQUE = WHITE + (255,)


def icon(draw_fn, size=96):
    big = Image.new("RGBA", (size * SS, size * SS), WHITE + (0,))
    draw_fn(ImageDraw.Draw(big), size * SS)
    return _down(big, (size, size))


def _stroke(draw, points, width):
    draw.line(points, fill=OPAQUE, width=width, joint="curve")
    for x, y in points:
        draw.ellipse([x - width / 2, y - width / 2, x + width / 2, y + width / 2], fill=OPAQUE)


def draw_search(draw, s):
    w = int(s * 0.085)
    draw.ellipse([s * 0.18, s * 0.18, s * 0.64, s * 0.64], outline=OPAQUE, width=w)
    _stroke(draw, [(s * 0.59, s * 0.59), (s * 0.80, s * 0.80)], w)


def draw_play(draw, s):
    draw.polygon([(s * 0.30, s * 0.20), (s * 0.30, s * 0.80), (s * 0.80, s * 0.50)], fill=OPAQUE)


def draw_play_circle(draw, s):
    draw.ellipse([s * 0.06, s * 0.06, s * 0.94, s * 0.94], fill=OPAQUE)
    draw.polygon([(s * 0.40, s * 0.30), (s * 0.40, s * 0.70), (s * 0.72, s * 0.50)], fill=(255, 255, 255, 0))


def draw_check(draw, s):
    _stroke(draw, [(s * 0.24, s * 0.52), (s * 0.42, s * 0.70), (s * 0.76, s * 0.32)], int(s * 0.10))


def draw_restart(draw, s):
    w = int(s * 0.085)
    draw.arc([s * 0.20, s * 0.20, s * 0.80, s * 0.80], 230, 520, fill=OPAQUE, width=w)
    cx, cy, r = s * 0.5, s * 0.5, s * 0.30
    angle = math.radians(230)
    x, y = cx + r * math.cos(angle), cy + r * math.sin(angle)
    draw.polygon([(x - s * 0.04, y - s * 0.15), (x + s * 0.14, y + s * 0.02), (x - s * 0.12, y + s * 0.08)],
                 fill=OPAQUE)


def draw_resume(draw, s):
    draw.rectangle([s * 0.22, s * 0.22, s * 0.32, s * 0.78], fill=OPAQUE)
    draw.polygon([(s * 0.42, s * 0.22), (s * 0.42, s * 0.78), (s * 0.82, s * 0.50)], fill=OPAQUE)


def draw_versions(draw, s):
    w = int(s * 0.075)
    draw.rounded_rectangle([s * 0.16, s * 0.32, s * 0.70, s * 0.82], s * 0.06, outline=OPAQUE, width=w)
    _stroke(draw, [(s * 0.30, s * 0.18), (s * 0.84, s * 0.18), (s * 0.84, s * 0.68)], w)


def draw_subtitles(draw, s):
    w = int(s * 0.075)
    draw.rounded_rectangle([s * 0.12, s * 0.22, s * 0.88, s * 0.78], s * 0.08, outline=OPAQUE, width=w)
    for x0, x1, y in ((0.26, 0.42, 0.44), (0.52, 0.74, 0.44), (0.26, 0.56, 0.60), (0.66, 0.74, 0.60)):
        _stroke(draw, [(s * x0, s * y), (s * x1, s * y)], w)


def draw_chevron_down(draw, s):
    _stroke(draw, [(s * 0.26, s * 0.38), (s * 0.50, s * 0.62), (s * 0.74, s * 0.38)], int(s * 0.09))


def draw_plus(draw, s):
    w = int(s * 0.09)
    _stroke(draw, [(s * 0.5, s * 0.22), (s * 0.5, s * 0.78)], w)
    _stroke(draw, [(s * 0.22, s * 0.5), (s * 0.78, s * 0.5)], w)


def _bookmark_shape(s):
    return [(s * 0.28, s * 0.16), (s * 0.72, s * 0.16), (s * 0.72, s * 0.84), (s * 0.50, s * 0.66),
            (s * 0.28, s * 0.84)]


def draw_bookmark(draw, s):
    draw.line(_bookmark_shape(s) + [_bookmark_shape(s)[0]], fill=OPAQUE, width=int(s * 0.075), joint="curve")


def draw_bookmark_filled(draw, s):
    draw.polygon(_bookmark_shape(s), fill=OPAQUE)


def _heart(draw, s, fill):
    r = s * 0.19
    left, right, top = (s * 0.33, s * 0.38), (s * 0.67, s * 0.38), s * 0.38
    outline = None if fill else OPAQUE
    width = 0 if fill else int(s * 0.075)
    if fill:
        draw.ellipse([left[0] - r, top - r, left[0] + r, top + r], fill=OPAQUE)
        draw.ellipse([right[0] - r, top - r, right[0] + r, top + r], fill=OPAQUE)
        draw.polygon([(s * 0.15, s * 0.44), (s * 0.85, s * 0.44), (s * 0.50, s * 0.84)], fill=OPAQUE)
    else:
        draw.arc([left[0] - r, top - r, left[0] + r, top + r], 150, 360, fill=outline, width=width)
        draw.arc([right[0] - r, top - r, right[0] + r, top + r], 180, 30, fill=outline, width=width)
        _stroke(draw, [(s * 0.165, s * 0.47), (s * 0.50, s * 0.84), (s * 0.835, s * 0.47)], width)


def draw_heart(draw, s):
    _heart(draw, s, False)


def draw_heart_filled(draw, s):
    _heart(draw, s, True)


def draw_more(draw, s):
    for x in (0.26, 0.50, 0.74):
        draw.ellipse([s * x - s * 0.07, s * 0.43, s * x + s * 0.07, s * 0.57], fill=OPAQUE)


def draw_shuffle(draw, s):
    w = int(s * 0.075)
    _stroke(draw, [(s * 0.16, s * 0.30), (s * 0.34, s * 0.30), (s * 0.62, s * 0.70), (s * 0.78, s * 0.70)], w)
    _stroke(draw, [(s * 0.16, s * 0.70), (s * 0.34, s * 0.70), (s * 0.62, s * 0.30), (s * 0.78, s * 0.30)], w)
    for y in (0.30, 0.70):
        draw.polygon([(s * 0.86, s * y), (s * 0.74, s * (y - 0.10)), (s * 0.74, s * (y + 0.10))], fill=OPAQUE)


def brand_light(size):
    """Soft blue, red and orange light for the sign-in and profile screens."""
    width, height = size
    image = Image.new("RGBA", size, (0, 0, 0, 0))
    for x, y, radius, color, opacity in ((0.80, 0.30, 0.34, (0x00, 0x34, 0xFB), 0.55),
                                         (0.92, 0.70, 0.26, (0xF5, 0x0B, 0x4F), 0.42),
                                         (0.66, 0.92, 0.24, (0xFD, 0x74, 0x03), 0.36)):
        blob = Image.new("L", size, 0)
        r = radius * width
        ImageDraw.Draw(blob).ellipse([x * width - r, y * height - r, x * width + r, y * height + r], fill=255)
        blob = blob.filter(ImageFilter.GaussianBlur(r * 0.55)).point(lambda value: int(value * opacity))
        layer = Image.new("RGBA", size, color + (0,))
        layer.putalpha(blob)
        image = Image.alpha_composite(image, layer)
    return image


def textures():
    """Return {file name: image}. Sizes are 1080p skin pixels unless noted."""
    return {
        # Fills tinted with colordiffuse and 9-sliced through texture borders.
        "round6.png": rounded((32, 32), 6),
        "round16.png": rounded((64, 64), 16),
        "round28.png": rounded((80, 80), 28),
        "circle.png": ellipse((256, 256)),
        # Focus ring 4px around a 16px card radius, and the soft glow behind it.
        "ring16.png": rounded((64, 64), 20, fill=0, line=1.0, line_width=4),
        "glow16.png": glow((128, 128), 40, 24, 0.28),
        "ring_circle.png": ellipse((256, 256), fill=0, line=1.0, line_width=8),
        # Buttons: capsules at the heights the layout uses, idle and selected.
        "pill52.png": rounded((104, 52), 26),
        "pill56.png": rounded((112, 56), 28),
        "pill60.png": rounded((120, 60), 30),
        "pill76.png": rounded((152, 76), 38),
        "pill52_idle.png": rounded((104, 52), 26, fill=0.05, line=0.30, line_width=2),
        "pill52_selected.png": rounded((104, 52), 26, fill=0.20, line=0.70, line_width=2),
        "pill56_idle.png": rounded((112, 56), 28, fill=0.08),
        "pill56_selected.png": rounded((112, 56), 28, fill=0.14, line=0.10, line_width=1),
        "pill60_selected.png": rounded((120, 60), 30, fill=0.14, line=0.10, line_width=1),
        "pill76_idle.png": rounded((152, 76), 38, fill=0.10, line=0.34, line_width=3),
        "circle_idle.png": ellipse((152, 152), fill=0.10, line=0.34, line_width=3),
        "badge.png": rounded((32, 32), 6, fill=0.08, line=0.24, line_width=1),
        "chip_outline.png": rounded((32, 32), 6, fill=0, line=0.70, line_width=2),
        "field.png": rounded((80, 80), 28, fill=0.055, line=0.12, line_width=2),
        "field_focus.png": rounded((80, 80), 28, fill=0.08, line=0.94, line_width=2),
        "glass.png": rounded((112, 112), 40, fill=0.72, line=0.14, line_width=1),
        "panel.png": vertical_panel((96, 96), 22, (0x1D, 0x1F, 0x24), (0x17, 0x18, 0x1B), 0.22, 0.05),
        # Artwork masks: alpha multiplies the image; stretched to the control.
        "mask_poster.png": rounded((352, 528), 32),
        "mask_wide.png": rounded((720, 404), 32),
        "mask_grid.png": rounded((520, 780), 32),
        "mask_circle.png": ellipse((256, 256)),
        "mask_hero.png": gradient_mask((614, 378), horizontal=((0, 0), (0.68, 1)), vertical=((0.58, 1), (1, 0))),
        "mask_details.png": gradient_mask(
            (614, 378), horizontal=((0, 0), (0.68, 1)),
            vertical=((0.38, 1), (0.52, 0.88), (0.68, 0.58), (0.81, 0.24), (0.90, 0))),
        "scrim_top.png": gradient_mask((8, 240), vertical=((0, 0.65), (1, 0))),
        "scrim_bottom.png": gradient_mask((8, 128), vertical=((0.5, 0), (1, 0.6))),
        "scrim_left.png": gradient_mask((480, 8), horizontal=((0, 0.80), (0.35, 0.55), (0.70, 0.20), (1, 0.10))),
        "brand_light.png": brand_light((960, 540)),
        # Icons, drawn white for colordiffuse tinting.
        "icon_search.png": icon(draw_search),
        "icon_play.png": icon(draw_play),
        "icon_play_circle.png": icon(draw_play_circle),
        "icon_check.png": icon(draw_check),
        "icon_restart.png": icon(draw_restart),
        "icon_resume.png": icon(draw_resume),
        "icon_versions.png": icon(draw_versions),
        "icon_subtitles.png": icon(draw_subtitles),
        "icon_chevron_down.png": icon(draw_chevron_down),
        "icon_plus.png": icon(draw_plus),
        "icon_bookmark.png": icon(draw_bookmark),
        "icon_bookmark_filled.png": icon(draw_bookmark_filled),
        "icon_heart.png": icon(draw_heart),
        "icon_heart_filled.png": icon(draw_heart_filled),
        "icon_more.png": icon(draw_more),
        "icon_shuffle.png": icon(draw_shuffle),
    }


def main():
    MEDIA.mkdir(parents=True, exist_ok=True)
    for name, image in sorted(textures().items()):
        image.save(MEDIA / name, optimize=True)
        print("wrote", name, image.size)


if __name__ == "__main__":
    main()
