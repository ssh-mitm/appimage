#!/usr/bin/env python3
"""Generate default_icon.svg (and optionally PNG) for the appimage project.

Layout: A snake (in Python colours) whose sinuous body winds around to form
a gear-like silhouette.  The snake head points upward and features a forked
tongue extending to the left.

Usage:
    python generate_icon.py            # writes SVG only
    python generate_icon.py --png      # writes SVG + PNG (requires cairosvg)
"""

import argparse
import math
from pathlib import Path

HERE = Path(__file__).parent

# ── tuneable parameters ────────────────────────────────────────────────────
W, H   = 256, 256
CX, CY = 128.0, 128.0

# Background box
BOX_MARGIN = 6
BOX_RADIUS = 22

# Snake body
R       = 72    # base radius from centre (smaller → head has more room)
A       = 15    # tooth amplitude (gear teeth height)
N       = 8     # number of teeth (even → head at base radius at sin=0 angles)
BODY_W  = 29    # stroke width = snake body thickness
GAP     = 0.22  # angular gap at head position [radians]
N_PTS   = 600   # path resolution

# Python colours
SNAKE_DARK  = "#306998"   # Python blue dark
SNAKE_LIGHT = "#5A9FD4"   # Python blue light
HEAD_COLOR  = "#2a5c88"   # slightly darker for head fill
EYE_WHITE   = "#ffffff"
PUPIL       = "#0a0a0a"
TONGUE      = "#cc1111"   # red tongue

# Arrow (yellow, AppImage reference)
ARROW_COLOR      = "#FFD43B"   # Python yellow
ARROW_SHAFT_W    = 20          # shaft half-width × 2
ARROW_HEAD_W     = 54          # arrowhead width
ARROW_HEAD_H     = 28          # arrowhead height
ARROW_TOP        = 94          # y where shaft starts
ARROW_TIP_Y      = 163         # y of arrow tip

# Head geometry
HEAD_LEN = 32   # distance from neck to snout tip (bigger = more prominent)
HEAD_W   = 27   # max half-width of head (clearly wider than body)

# -5π/8 ≈ 11-o'clock; sin(8·angle)=0 → head sits at base radius R
HEAD_ANGLE = -5 * math.pi / 8
# ──────────────────────────────────────────────────────────────────────────


def _body_points():
    """Return list of (x, y) points along the snake body path."""
    s = HEAD_ANGLE + GAP / 2
    e = HEAD_ANGLE + 2 * math.pi - GAP / 2
    pts = []
    for i in range(N_PTS + 1):
        t = s + (e - s) * i / N_PTS
        r = R + A * math.sin(N * t)
        pts.append((CX + r * math.cos(t), CY + r * math.sin(t)))
    return pts


def body_path_d(pts):
    return "M " + " L ".join(f"{x:.2f},{y:.2f}" for x, y in pts)


def tail_path_d(pts):
    """Triangular tail tip at the start of the path."""
    tx = pts[1][0] - pts[0][0]
    ty = pts[1][1] - pts[0][1]
    tl = math.hypot(tx, ty)
    tx, ty = tx / tl, ty / tl          # unit tangent from tail toward body
    # perpendicular
    px, py = -ty, tx
    w = BODY_W / 2 * 0.45
    tip_x = pts[0][0] - tx * 10
    tip_y = pts[0][1] - ty * 10
    return (f"M {pts[0][0] + px*w:.2f},{pts[0][1] + py*w:.2f} "
            f"L {tip_x:.2f},{tip_y:.2f} "
            f"L {pts[0][0] - px*w:.2f},{pts[0][1] - py*w:.2f}")


def head_elements():
    """Return (head_d, tongue_d, eye_x, eye_y) for the snake head."""
    head_r = R + A * math.sin(N * HEAD_ANGLE)
    hx = CX + head_r * math.cos(HEAD_ANGLE)
    hy = CY + head_r * math.sin(HEAD_ANGLE)

    # outward unit vector (toward snout) and perpendicular
    ox, oy = math.cos(HEAD_ANGLE), math.sin(HEAD_ANGLE)
    px, py = -oy, ox    # 90° counter-clockwise

    snout_x = hx + ox * HEAD_LEN
    snout_y = hy + oy * HEAD_LEN

    # Head outline via cubic bezier: neck-left → cheek-left → snout → cheek-right → neck-right
    hl_x, hl_y = hx + px * (BODY_W / 2),      hy + py * (BODY_W / 2)
    hr_x, hr_y = hx - px * (BODY_W / 2),      hy - py * (BODY_W / 2)
    cl_x = hx + ox * HEAD_LEN * 0.45 + px * HEAD_W
    cl_y = hy + oy * HEAD_LEN * 0.45 + py * HEAD_W
    cr_x = hx + ox * HEAD_LEN * 0.45 - px * HEAD_W
    cr_y = hy + oy * HEAD_LEN * 0.45 - py * HEAD_W

    head_d = (
        f"M {hl_x:.2f},{hl_y:.2f} "
        f"C {hl_x + ox*8:.2f},{hl_y + oy*8:.2f} {cl_x:.2f},{cl_y:.2f} "
        f"  {snout_x:.2f},{snout_y:.2f} "
        f"C {cr_x:.2f},{cr_y:.2f} {hr_x + ox*8:.2f},{hr_y + oy*8:.2f} "
        f"  {hr_x:.2f},{hr_y:.2f} Z"
    )

    # Eye: on the right side of the head (px direction), roughly 60 % toward snout
    eye_x = hx + ox * HEAD_LEN * 0.58 + px * (HEAD_W * 0.48)
    eye_y = hy + oy * HEAD_LEN * 0.58 + py * (HEAD_W * 0.48)

    # Tongue: exits snout and forks to the LEFT (−px direction)
    tb_x = snout_x - px * 2            # tongue base, slightly left of snout centre
    tb_y = snout_y - py * 2
    left_x  = -px                      # unit vector to the left
    left_y  = -py
    fwd_x, fwd_y = ox, oy             # still slightly forward

    fork_len  = 10
    fork_side = 5
    mid_x = tb_x + left_x * fork_len
    mid_y = tb_y + left_y * fork_len

    tip1_x = mid_x + left_x * fork_side + fwd_x * 4
    tip1_y = mid_y + left_y * fork_side + fwd_y * 4
    tip2_x = mid_x + left_x * fork_side - fwd_x * 4
    tip2_y = mid_y + left_y * fork_side - fwd_y * 4

    tongue_d = (
        f"M {tb_x:.2f},{tb_y:.2f} L {mid_x:.2f},{mid_y:.2f} "
        f"M {mid_x:.2f},{mid_y:.2f} L {tip1_x:.2f},{tip1_y:.2f} "
        f"M {mid_x:.2f},{mid_y:.2f} L {tip2_x:.2f},{tip2_y:.2f}"
    )

    return head_d, tongue_d, eye_x, eye_y


def arrow_path_d():
    sw  = ARROW_SHAFT_W / 2
    hw  = ARROW_HEAD_W  / 2
    sb  = ARROW_TIP_Y - ARROW_HEAD_H   # y where shaft meets arrowhead
    cx  = CX
    return (
        f"M {cx - sw:.1f},{ARROW_TOP:.1f} "
        f"L {cx + sw:.1f},{ARROW_TOP:.1f} "
        f"L {cx + sw:.1f},{sb:.1f} "
        f"L {cx + hw:.1f},{sb:.1f} "
        f"L {cx},{ARROW_TIP_Y:.1f} "
        f"L {cx - hw:.1f},{sb:.1f} "
        f"L {cx - sw:.1f},{sb:.1f} Z"
    )


def build_svg():
    pts    = _body_points()
    body_d = body_path_d(pts)
    tail_d = tail_path_d(pts)
    head_d, tongue_d, eye_x, eye_y = head_elements()

    arrow_d = arrow_path_d()
    eye_r   = 5.5
    pupil_r = 3.6

    return f'''<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">
  <defs>
    <radialGradient id="bg" cx="40%" cy="35%" r="70%">
      <stop offset="0" stop-color="#e8eef4"/>
      <stop offset="1" stop-color="#c8d8e8"/>
    </radialGradient>
    <linearGradient id="body-grad" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="{SNAKE_LIGHT}"/>
      <stop offset="1" stop-color="{SNAKE_DARK}"/>
    </linearGradient>
    <radialGradient id="shadow-r" cx="50%" cy="50%" r="50%">
      <stop offset="0" stop-color="#000" stop-opacity="0.3"/>
      <stop offset="1" stop-color="#000" stop-opacity="0"/>
    </radialGradient>
  </defs>

  <!-- drop shadow -->
  <ellipse cx="{W//2}" cy="{H - 4}" rx="108" ry="5" fill="url(#shadow-r)"/>

  <!-- background box -->
  <rect x="{BOX_MARGIN}" y="{BOX_MARGIN}"
        width="{W - 2*BOX_MARGIN}" height="{H - 2*BOX_MARGIN}"
        rx="{BOX_RADIUS}" fill="url(#bg)"/>
  <rect x="{BOX_MARGIN}" y="{BOX_MARGIN}"
        width="{W - 2*BOX_MARGIN}" height="{H - 2*BOX_MARGIN}"
        rx="{BOX_RADIUS}" fill="none" stroke="white"
        stroke-width="1.2" stroke-opacity="0.5"/>

  <!-- snake body -->
  <path d="{body_d}"
        fill="none" stroke="url(#body-grad)"
        stroke-width="{BODY_W}" stroke-linecap="round" stroke-linejoin="round"/>

  <!-- tail tip -->
  <path d="{tail_d}" fill="{SNAKE_DARK}" stroke="none"/>

  <!-- head fill -->
  <path d="{head_d}" fill="{HEAD_COLOR}"/>
  <!-- head highlight -->
  <path d="{head_d}" fill="none" stroke="{SNAKE_LIGHT}"
        stroke-width="1" opacity="0.4"/>

  <!-- yellow arrow (AppImage reference) -->
  <path d="{arrow_d}" fill="{ARROW_COLOR}"/>

  <!-- eye -->
  <circle cx="{eye_x:.2f}" cy="{eye_y:.2f}" r="{eye_r}" fill="{EYE_WHITE}"/>
  <circle cx="{eye_x:.2f}" cy="{eye_y:.2f}" r="{pupil_r}" fill="{PUPIL}"/>
  <circle cx="{eye_x + 0.9:.2f}" cy="{eye_y - 0.9:.2f}" r="0.9" fill="{EYE_WHITE}"/>
</svg>'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--png", action="store_true",
                        help="also export PNG via cairosvg")
    args = parser.parse_args()

    svg_path = HERE / "default_icon.svg"
    svg_path.write_text(build_svg(), encoding="utf-8")
    print(f"Written: {svg_path}")

    if args.png:
        import cairosvg
        png_path = HERE / "default_icon.png"
        cairosvg.svg2png(url=str(svg_path), write_to=str(png_path),
                         output_width=W, output_height=H)
        print(f"Written: {png_path}")


if __name__ == "__main__":
    main()
