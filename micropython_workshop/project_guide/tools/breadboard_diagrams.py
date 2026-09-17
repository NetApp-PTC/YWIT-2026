#!/usr/bin/env python3
r"""Render breadboard diagrams from coordinates embedded in the project text.

The project guide is the single source of truth: ``\bbhole{name}{coordinate}``
prints a coordinate in the PDF and gives it a stable name. This script reads
those definitions from the .tex file and uses them to draw every component,
wire, and callout.

Usage:
    python3 tools/breadboard_diagrams.py [--source projects/project_1.tex]

Writes PNG files under project_guide/images/. Requires rsvg-convert
(brew install librsvg) to rasterize the generated SVG.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# ---------------------------------------------------------------------------
# Breadboard geometry, in SVG user units. PITCH is one 0.1" hole spacing.
# ---------------------------------------------------------------------------

PITCH = 24
ROWS = 30

COLUMN_Y = {
    "J": 102,
    "I": 126,
    "H": 150,
    "G": 174,
    "F": 198,
    "E": 270,
    "D": 294,
    "C": 318,
    "B": 342,
    "A": 366,
}

RAIL_Y = {"top+": 30, "top-": 54, "bot-": 414, "bot+": 438}

NUM_STRIP_TOP_Y = 78
NUM_STRIP_BOT_Y = 390
CHANNEL_TOP_Y = 210
CHANNEL_BOT_Y = 258

ROW1_X = 88
BOARD_W = ROW1_X + (ROWS - 1) * PITCH + ROW1_X
BOARD_H = 468
LETTER_X_LEFT = 30
LETTER_X_RIGHT = BOARD_W - 30

# Padding around the board so callout labels have somewhere to live. The top
# and bottom are kept tight to one line of label text each, since every jumper
# arc stays inside the board outline and so nothing else needs the room.
MARGIN_L = 70
MARGIN_R = 70
MARGIN_T = 58
MARGIN_B = 54

CANVAS_W = BOARD_W + MARGIN_L + MARGIN_R
CANVAS_H = BOARD_H + MARGIN_T + MARGIN_B

# The single row of callout text above and below the board.
LABEL_Y_TOP = -38
LABEL_Y_BOTTOM = BOARD_H + 40

# ---------------------------------------------------------------------------
# Palette
# ---------------------------------------------------------------------------

BOARD_FILL = "#f4f1e8"
BOARD_EDGE = "#cfc9b8"
STRIP_FILL = "#eae6d9"
HOLE_FILL = "#2f2f2f"
CHANNEL_FILL = "#e2ded0"
LABEL_GREY = "#6b6b6b"
RAIL_RED = "#cc3b33"
RAIL_BLUE = "#3a6ea8"

PCB_FILL = "#1c2b38"
PCB_EDGE = "#0e1720"
SHIELD_FILL = "#b6bcc3"
USB_FILL = "#c6ccd2"
PAD_FILL = "#d8b268"
SILK = "#9fb4c6"

LEAD_FILL = "#c4c4c4"
LEAD_EDGE = "#8d8d8d"

WIRE_RED = "#d62828"
WIRE_BLACK = "#2b2b2b"
WIRE_YELLOW = "#e3b505"
WIRE_ORANGE = "#ef7d19"

ACCENT = "#1f6f8b"
ACCENT_DARK_YELLOW = "#9c7a00"

# XIAO ESP32-C3 pins, ordered row 1 -> row 7 down each pin column. The board's
# own silkscreen is on the underside, so these are labelled with the GPIO
# numbers the project text refers to.
PINS_H = ["5V", "GND", "3V3", "IO10", "IO9", "IO8", "IO20"]
PINS_D = ["IO2", "IO3", "IO4", "IO5", "IO6", "IO7", "IO21"]

PROJECT_GUIDE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_SOURCE = PROJECT_GUIDE_DIR / "projects" / "project_1.tex"
BBHOLE_RE = re.compile(
    r"\\bbhole\s*\{(?P<name>[A-Za-z0-9_.-]+)\}\s*"
    r"\{(?P<coordinate>[A-J](?:[1-9]|[12][0-9]|30))\}"
)
BBREF_RE = re.compile(r"\\bbref\s*\{(?P<name>[A-Za-z0-9_.-]+)\}")

REQUIRED_COORDINATES = {
    "mcu.5v",
    "mcu.gpio2",
    "mcu.gpio20",
    "mcu.gpio21",
    "led.anode",
    "led.cathode",
    "led-resistor.start",
    "led-resistor.end",
    "ldr.first",
    "ldr.second",
    "ldr-resistor.start",
    "ldr-resistor.end",
    "jumper.led.start",
    "jumper.led.end",
    "jumper.led-ground.start",
    "jumper.led-ground.end",
    "jumper.sensor.start",
    "jumper.sensor.end",
    "jumper.ldr-power.start",
    "jumper.ldr-power.end",
    "jumper.ldr-ground.start",
    "jumper.ldr-ground.end",
}

COORDINATES: dict[str, str] = {}


def _without_tex_comments(text: str) -> str:
    """Remove unescaped TeX comments before looking for coordinate commands."""
    cleaned: list[str] = []
    for line in text.splitlines():
        for index, char in enumerate(line):
            if char != "%":
                continue
            backslashes = 0
            cursor = index - 1
            while cursor >= 0 and line[cursor] == "\\":
                backslashes += 1
                cursor -= 1
            if backslashes % 2 == 0:
                line = line[:index]
                break
        cleaned.append(line)
    return "\n".join(cleaned)


def read_coordinates(source: Path) -> dict[str, str]:
    """Read and validate all named breadboard coordinates from a TeX file."""
    text = _without_tex_comments(source.read_text(encoding="utf-8"))
    coordinates: dict[str, str] = {}

    for match in BBHOLE_RE.finditer(text):
        name = match.group("name")
        coordinate = match.group("coordinate")
        if name in coordinates:
            raise ValueError(f"{source}: duplicate \\bbhole name {name!r}")
        coordinates[name] = coordinate

    missing = sorted(REQUIRED_COORDINATES - coordinates.keys())
    if missing:
        raise ValueError(f"{source}: missing \\bbhole definitions: {', '.join(missing)}")

    undefined_refs = sorted(
        {match.group("name") for match in BBREF_RE.finditer(text)} - coordinates.keys()
    )
    if undefined_refs:
        raise ValueError(f"{source}: undefined \\bbref names: {', '.join(undefined_refs)}")

    return coordinates


def named_hole(name: str) -> str:
    """Resolve a semantic name to the coordinate parsed from the project text."""
    try:
        return COORDINATES[name]
    except KeyError as exc:
        raise RuntimeError(f"coordinate {name!r} was not loaded from the project text") from exc


def row_x(row: int) -> float:
    return ROW1_X + (row - 1) * PITCH


def hole(coord: str) -> tuple[float, float]:
    """Return the centre of a breadboard hole such as "B16" or "J7"."""
    column = coord[0].upper()
    row = int(coord[1:])
    if column not in COLUMN_Y:
        raise ValueError(f"unknown breadboard column in {coord!r}")
    if not 1 <= row <= ROWS:
        raise ValueError(f"row out of range in {coord!r}")
    return row_x(row), COLUMN_Y[column]


def xiao_rect() -> tuple[float, float, float, float]:
    """Outline of the seated module: 21 x 17.5 mm around a 0.6" x 0.6" pin grid."""
    x1, y_h = hole(named_hole("mcu.5v"))
    x7, y_d = hole(named_hole("mcu.gpio21"))
    return x1 - 27, y_h - 11, x7 + 27, y_d + 11


def esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class Drawing:
    """Minimal SVG builder."""

    def __init__(self) -> None:
        self.parts: list[str] = []
        self.defs: list[str] = []

    def add(self, markup: str) -> None:
        self.parts.append(markup)

    def define(self, markup: str) -> None:
        self.defs.append(markup)

    def text(
        self,
        x: float,
        y: float,
        content: str,
        size: float = 11,
        fill: str = "#000",
        anchor: str = "middle",
        weight: str = "normal",
    ) -> None:
        self.add(
            f'<text x="{x:.2f}" y="{y:.2f}" font-family="Helvetica, Arial, sans-serif" '
            f'font-size="{size}" font-weight="{weight}" fill="{fill}" '
            f'text-anchor="{anchor}">{esc(content)}</text>'
        )

    def render(self) -> str:
        defs = "\n".join(self.defs)
        body = "\n".join(self.parts)
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{CANVAS_W}" '
            f'height="{CANVAS_H}" viewBox="0 0 {CANVAS_W} {CANVAS_H}">\n'
            f"<defs>\n{defs}\n</defs>\n"
            f'<rect width="{CANVAS_W}" height="{CANVAS_H}" fill="#ffffff"/>\n'
            f'<g transform="translate({MARGIN_L},{MARGIN_T})">\n{body}\n</g>\n'
            "</svg>\n"
        )


# ---------------------------------------------------------------------------
# Breadboard
# ---------------------------------------------------------------------------


def draw_hole(d: Drawing, x: float, y: float, size: float = 7.5) -> None:
    half = size / 2
    d.add(
        f'<rect x="{x - half:.2f}" y="{y - half:.2f}" width="{size}" '
        f'height="{size}" rx="1.6" fill="{HOLE_FILL}"/>'
    )


def draw_breadboard(d: Drawing) -> None:
    d.add(
        f'<rect x="0" y="0" width="{BOARD_W}" height="{BOARD_H}" rx="10" '
        f'fill="{BOARD_FILL}" stroke="{BOARD_EDGE}" stroke-width="2"/>'
    )

    strip_x = row_x(1) - 16
    strip_w = (ROWS - 1) * PITCH + 32
    for top, bottom in (("J", "F"), ("E", "A")):
        d.add(
            f'<rect x="{strip_x:.2f}" y="{COLUMN_Y[top] - 16}" width="{strip_w}" '
            f'height="{COLUMN_Y[bottom] - COLUMN_Y[top] + 32}" rx="5" fill="{STRIP_FILL}"/>'
        )

    d.add(
        f'<rect x="10" y="{CHANNEL_TOP_Y}" width="{BOARD_W - 20}" '
        f'height="{CHANNEL_BOT_Y - CHANNEL_TOP_Y}" rx="3" fill="{CHANNEL_FILL}" '
        f'stroke="{BOARD_EDGE}" stroke-width="1.5"/>'
    )

    # Power rails: 25 holes each, printed in groups of five.
    rail_rows = [r for r in range(2, ROWS + 1) if (r - 2) % 6 != 5]
    for rail, colour in (
        ("top+", RAIL_RED),
        ("top-", RAIL_BLUE),
        ("bot-", RAIL_BLUE),
        ("bot+", RAIL_RED),
    ):
        y = RAIL_Y[rail]
        d.add(
            f'<line x1="28" y1="{y}" x2="{BOARD_W - 28}" y2="{y}" stroke="{colour}" '
            f'stroke-width="1.6" opacity="0.6"/>'
        )
        for row in rail_rows:
            draw_hole(d, row_x(row), y, 7)
        sign = "+" if rail.endswith("+") else "\u2212"
        for x in (16, BOARD_W - 16):
            d.text(x, y + 4.5, sign, size=13, fill=colour, weight="bold")

    for column, y in COLUMN_Y.items():
        for row in range(1, ROWS + 1):
            draw_hole(d, row_x(row), y)
        for x in (LETTER_X_LEFT, LETTER_X_RIGHT):
            d.text(x, y + 4, column, size=12, fill=LABEL_GREY, weight="bold")

    for row in range(1, ROWS + 1):
        emphasis = row == 1 or row % 5 == 0
        for y in (NUM_STRIP_TOP_Y + 4, NUM_STRIP_BOT_Y + 4):
            d.text(
                row_x(row),
                y,
                str(row),
                size=11 if emphasis else 9.5,
                fill=LABEL_GREY,
                weight="bold" if emphasis else "normal",
            )


# ---------------------------------------------------------------------------
# Microcontroller
# ---------------------------------------------------------------------------


def draw_xiao(d: Drawing) -> None:
    """Draw the XIAO at the four pin positions defined in the project text."""
    left, top, right, bottom = xiao_rect()
    mid_y = (top + bottom) / 2

    # USB-C shell hangs off the row-1 end of the board.
    usb_h = 84
    d.add(
        f'<rect x="{left - 15:.2f}" y="{mid_y - usb_h / 2:.2f}" width="23" '
        f'height="{usb_h}" rx="4" fill="{USB_FILL}" stroke="#98a0a7" stroke-width="1.5"/>'
    )
    d.add(
        f'<rect x="{left - 9:.2f}" y="{mid_y - usb_h / 2 + 12:.2f}" width="7" '
        f'height="{usb_h - 24}" rx="3.5" fill="#6d757c"/>'
    )

    d.add(
        f'<rect x="{left:.2f}" y="{top:.2f}" width="{right - left:.2f}" '
        f'height="{bottom - top:.2f}" rx="6" fill="{PCB_FILL}" stroke="{PCB_EDGE}" '
        f'stroke-width="1.5"/>'
    )

    # RF shield, sized to sit between the two rows of pin labels.
    sh_x, sh_y = left + 40, top + 44
    sh_w, sh_h = (right - left) - 78, (bottom - top) - 88
    d.add(
        f'<rect x="{sh_x:.2f}" y="{sh_y:.2f}" width="{sh_w:.2f}" height="{sh_h:.2f}" '
        f'rx="3" fill="{SHIELD_FILL}" stroke="#8d939a" stroke-width="1.2"/>'
    )
    d.text(sh_x + sh_w / 2, sh_y + sh_h / 2 + 3.5, "ESP32-C3", size=9.5, fill="#4c5358", weight="bold")

    # Ceramic antenna at the end away from USB.
    d.add(
        f'<rect x="{right - 22:.2f}" y="{mid_y - 21:.2f}" width="13" height="42" rx="2" '
        f'fill="#efe7d4" stroke="#c7bea7" stroke-width="1"/>'
    )

    # Pin pads with labels drawn inward so they stay on the PCB. Interpolating
    # between the named end pins avoids another copy of the pin coordinates.
    pin_sides = (
        ("mcu.5v", "mcu.gpio20", PINS_H, 14),
        ("mcu.gpio2", "mcu.gpio21", PINS_D, -14),
    )
    for start_name, end_name, labels, inward in pin_sides:
        start_x, start_y = hole(named_hole(start_name))
        end_x, end_y = hole(named_hole(end_name))
        for index, label in enumerate(labels):
            fraction = index / (len(labels) - 1)
            px = start_x + (end_x - start_x) * fraction
            py = start_y + (end_y - start_y) * fraction
            d.add(
                f'<rect x="{px - 5:.2f}" y="{py - 5:.2f}" width="10" height="10" rx="2" '
                f'fill="{PAD_FILL}" stroke="#a8843f" stroke-width="1"/>'
            )
            d.text(px, py + inward + 3, label, size=7.5, fill=SILK)


# ---------------------------------------------------------------------------
# Components
# ---------------------------------------------------------------------------


def draw_led(d: Drawing, anode: str, cathode: str, colour: str = "#e23b3b") -> None:
    """A 5 mm LED seen from above, standing over its two holes."""
    ax, ay = hole(anode)
    cx, cy = hole(cathode)
    mx, my = (ax + cx) / 2, (ay + cy) / 2
    r = 17.5

    d.add(f'<circle cx="{mx + 3:.2f}" cy="{my + 4:.2f}" r="{r}" fill="#000" opacity="0.14"/>')
    d.add(
        f'<circle cx="{mx:.2f}" cy="{my:.2f}" r="{r}" fill="{colour}" '
        f'stroke="#8a1f1f" stroke-width="1.5"/>'
    )
    # The flattened rim on the cathode side of the body.
    flat = 0.78
    d.add(
        f'<line x1="{cx * flat + mx * (1 - flat):.2f}" y1="{my - r * 0.62:.2f}" '
        f'x2="{cx * flat + mx * (1 - flat):.2f}" y2="{my + r * 0.62:.2f}" '
        f'stroke="#8a1f1f" stroke-width="1.6" opacity="0.85"/>'
    )
    d.add(
        f'<ellipse cx="{mx - 4.5:.2f}" cy="{my - 6:.2f}" rx="6.5" ry="4.5" '
        f'fill="#ffffff" opacity="0.5"/>'
    )
    # Polarity reminder on the long-leg side, with a halo so it reads over the board.
    d.add(
        f'<circle cx="{ax - 4:.2f}" cy="{ay - r - 9:.2f}" r="9" fill="#ffffff" opacity="0.85"/>'
    )
    d.text(ax - 4, ay - r - 4, "+", size=17, fill="#8a1f1f", weight="bold")


def draw_ldr(d: Drawing, leg_a: str, leg_b: str) -> None:
    """A 5 mm photoresistor seen from above."""
    ax, ay = hole(leg_a)
    bx, by = hole(leg_b)
    mx, my = (ax + bx) / 2, (ay + by) / 2
    r = 19.0
    face = r - 3.5

    clip_id = "ldr-face"
    d.define(f'<clipPath id="{clip_id}"><circle cx="{mx:.2f}" cy="{my:.2f}" r="{face:.2f}"/></clipPath>')

    d.add(f'<circle cx="{mx + 3:.2f}" cy="{my + 4:.2f}" r="{r}" fill="#000" opacity="0.14"/>')
    d.add(
        f'<circle cx="{mx:.2f}" cy="{my:.2f}" r="{r}" fill="#f0e6cf" '
        f'stroke="#b5a787" stroke-width="1.4"/>'
    )
    d.add(f'<circle cx="{mx:.2f}" cy="{my:.2f}" r="{face:.2f}" fill="#eaa53c"/>')

    # Serpentine cadmium-sulphide track across the face.
    span = face - 1.5
    amp = face * 0.78
    fingers = 5
    xs = [mx - span + (2 * span) * i / fingers for i in range(fingers + 1)]
    segments = [f"M {xs[0]:.2f} {my - amp:.2f}"]
    for i, x in enumerate(xs):
        y_a, y_b = (my - amp, my + amp) if i % 2 == 0 else (my + amp, my - amp)
        segments.append(f"L {x:.2f} {y_a:.2f} L {x:.2f} {y_b:.2f}")
        if i < fingers:
            segments.append(f"L {xs[i + 1]:.2f} {y_b:.2f}")
    d.add(
        f'<path d="{" ".join(segments)}" fill="none" stroke="#3b2a12" stroke-width="2.8" '
        f'stroke-linejoin="round" clip-path="url(#{clip_id})"/>'
    )
    d.add(
        f'<circle cx="{mx:.2f}" cy="{my:.2f}" r="{face:.2f}" fill="none" '
        f'stroke="#c08f2c" stroke-width="1.2"/>'
    )


RESISTOR_BANDS = {
    # 220 ohm: red, red, brown (x10), gold. 10k: brown, black, orange (x1k), gold.
    "220": ["#c0392b", "#c0392b", "#7b4b23", "#d4af37"],
    "10k": ["#7b4b23", "#1c1c1c", "#e07b1f", "#d4af37"],
}


def draw_resistor(d: Drawing, end_a: str, end_b: str, value: str) -> None:
    ax, ay = hole(end_a)
    bx, by = hole(end_b)
    if (bx, by) < (ax, ay):
        ax, ay, bx, by = bx, by, ax, ay
    mx, my = (ax + bx) / 2, (ay + by) / 2
    body_len, body_h = 62, 22

    for width, colour in ((5.5, LEAD_EDGE), (3.2, LEAD_FILL)):
        d.add(
            f'<line x1="{ax:.2f}" y1="{ay:.2f}" x2="{bx:.2f}" y2="{by:.2f}" '
            f'stroke="{colour}" stroke-width="{width}" stroke-linecap="round"/>'
        )

    d.add(f'<g transform="translate({mx:.2f},{my:.2f})">')
    d.add(
        f'<rect x="{-body_len / 2 + 2:.2f}" y="{-body_h / 2 + 4:.2f}" width="{body_len}" '
        f'height="{body_h}" rx="9" fill="#000" opacity="0.13"/>'
    )
    d.add(
        f'<rect x="{-body_len / 2:.2f}" y="{-body_h / 2:.2f}" width="{body_len}" '
        f'height="{body_h}" rx="9" fill="#e2cfab" stroke="#b09873" stroke-width="1.3"/>'
    )
    for i, band in enumerate(RESISTOR_BANDS[value]):
        d.add(
            f'<rect x="{-body_len / 2 + 12 + i * 10.5:.2f}" y="{-body_h / 2 + 1.5:.2f}" '
            f'width="5.6" height="{body_h - 3}" fill="{band}"/>'
        )
    d.add("</g>")


def _quad_point(p0, c, p1, t):
    u = 1 - t
    return (
        u * u * p0[0] + 2 * u * t * c[0] + t * t * p1[0],
        u * u * p0[1] + 2 * u * t * c[1] + t * t * p1[1],
    )


def draw_jumper(d: Drawing, start: str, end: str, colour: str, control: tuple[float, float]) -> None:
    """A jumper wire drawn as an arc that never crosses the module body."""
    p0 = hole(start)
    p1 = hole(end)
    left, top, right, bottom = xiao_rect()

    for i in range(1, 200):
        x, y = _quad_point(p0, control, p1, i / 200)
        if left <= x <= right and top <= y <= bottom:
            raise AssertionError(
                f"jumper {start}->{end} is routed across the microcontroller "
                f"at ({x:.1f}, {y:.1f}); adjust its control point"
            )

    path = f"M {p0[0]:.2f} {p0[1]:.2f} Q {control[0]:.2f} {control[1]:.2f} {p1[0]:.2f} {p1[1]:.2f}"
    d.add(f'<path d="{path}" fill="none" stroke="#000" stroke-width="8" opacity="0.13" transform="translate(2,4)"/>')
    d.add(f'<path d="{path}" fill="none" stroke="#17171a" stroke-width="7.5" stroke-linecap="round"/>')
    d.add(f'<path d="{path}" fill="none" stroke="{colour}" stroke-width="5" stroke-linecap="round"/>')

    for x, y in (p0, p1):
        d.add(
            f'<circle cx="{x:.2f}" cy="{y:.2f}" r="6" fill="{colour}" stroke="#17171a" '
            f'stroke-width="1.8"/>'
        )
        d.add(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="2" fill="#17171a" opacity="0.55"/>')


# ---------------------------------------------------------------------------
# Callouts
# ---------------------------------------------------------------------------


def callout(
    d: Drawing,
    coord: str,
    label: str,
    label_xy: tuple[float, float],
    colour: str = ACCENT,
    anchor: str = "middle",
) -> None:
    """Ring the given hole and run a leader line to a label."""
    hx, hy = hole(coord)
    lx, ly = label_xy

    attach_x = lx + 6 if anchor == "start" else lx - 6 if anchor == "end" else lx
    attach_y = ly + 6 if ly < hy else ly - 13

    d.add(
        f'<path d="M {hx:.2f} {hy:.2f} L {attach_x:.2f} {attach_y:.2f}" fill="none" '
        f'stroke="{colour}" stroke-width="1.3" opacity="0.75"/>'
    )
    d.add(
        f'<circle cx="{hx:.2f}" cy="{hy:.2f}" r="8.5" fill="none" stroke="{colour}" '
        f'stroke-width="2.4"/>'
    )
    d.text(lx, ly, label, size=13.5, fill=colour, anchor=anchor, weight="bold")


# ---------------------------------------------------------------------------
# Diagrams
# ---------------------------------------------------------------------------


def diagram_microcontroller_seated() -> Drawing:
    d = Drawing()
    draw_breadboard(d)
    draw_xiao(d)

    five_volts = named_hole("mcu.5v")
    gpio20 = named_hole("mcu.gpio20")
    gpio2 = named_hole("mcu.gpio2")
    gpio21 = named_hole("mcu.gpio21")
    callout(d, five_volts, f"5V \u2192 {five_volts}", (26, LABEL_Y_TOP), anchor="start")
    callout(d, gpio20, f"GPIO 20 \u2192 {gpio20}", (250, LABEL_Y_TOP), anchor="start")
    callout(d, gpio2, f"GPIO 2 \u2192 {gpio2}", (26, LABEL_Y_BOTTOM), anchor="start")
    callout(d, gpio21, f"GPIO 21 \u2192 {gpio21}", (250, LABEL_Y_BOTTOM), anchor="start")
    return d


def diagram_led_placed() -> Drawing:
    d = Drawing()
    draw_breadboard(d)
    draw_xiao(d)
    resistor_start = named_hole("led-resistor.start")
    resistor_end = named_hole("led-resistor.end")
    anode = named_hole("led.anode")
    cathode = named_hole("led.cathode")
    draw_resistor(d, resistor_start, resistor_end, "220")
    draw_led(d, anode, cathode)

    callout(d, anode, f"{anode} \u00b7 long leg", (330, LABEL_Y_TOP), anchor="end")
    callout(d, cathode, f"{cathode} \u00b7 short leg", (500, LABEL_Y_TOP), anchor="start")
    callout(d, resistor_start, resistor_start, (hole(resistor_start)[0], LABEL_Y_BOTTOM))
    callout(d, resistor_end, resistor_end, (hole(resistor_end)[0], LABEL_Y_BOTTOM))
    return d


def diagram_components_placed() -> Drawing:
    d = Drawing()
    draw_breadboard(d)
    draw_xiao(d)
    led_resistor_start = named_hole("led-resistor.start")
    led_resistor_end = named_hole("led-resistor.end")
    anode = named_hole("led.anode")
    cathode = named_hole("led.cathode")
    ldr_resistor_start = named_hole("ldr-resistor.start")
    ldr_resistor_end = named_hole("ldr-resistor.end")
    ldr_first = named_hole("ldr.first")
    ldr_second = named_hole("ldr.second")
    draw_resistor(d, led_resistor_start, led_resistor_end, "220")
    draw_led(d, anode, cathode)
    draw_resistor(d, ldr_resistor_start, ldr_resistor_end, "10k")
    draw_ldr(d, ldr_first, ldr_second)

    callout(d, ldr_first, ldr_first, (hole(ldr_first)[0] - 48, LABEL_Y_TOP))
    callout(d, ldr_second, ldr_second, (hole(ldr_second)[0] + 48, LABEL_Y_TOP))
    callout(d, ldr_resistor_start, ldr_resistor_start, (hole(ldr_resistor_start)[0], LABEL_Y_BOTTOM))
    callout(d, ldr_resistor_end, ldr_resistor_end, (hole(ldr_resistor_end)[0], LABEL_Y_BOTTOM))
    return d


def diagram_wired_up() -> Drawing:
    d = Drawing()
    draw_breadboard(d)
    draw_xiao(d)
    draw_resistor(d, named_hole("led-resistor.start"), named_hole("led-resistor.end"), "220")
    draw_led(d, named_hole("led.anode"), named_hole("led.cathode"))
    draw_resistor(d, named_hole("ldr-resistor.start"), named_hole("ldr-resistor.end"), "10k")
    draw_ldr(d, named_hole("ldr.first"), named_hole("ldr.second"))

    # Arcs are lifted over the top edge of the module (or dipped under its
    # bottom edge) so that no wire is drawn lying across the microcontroller.
    # Each arc peaks at a different height and row so the wires stay separable
    # where they leave the crowded row 2-7 corner.
    power_start = named_hole("jumper.ldr-power.start")
    power_end = named_hole("jumper.ldr-power.end")
    ldr_ground_start = named_hole("jumper.ldr-ground.start")
    ldr_ground_end = named_hole("jumper.ldr-ground.end")
    led_ground_start = named_hole("jumper.led-ground.start")
    led_ground_end = named_hole("jumper.led-ground.end")
    led_start = named_hole("jumper.led.start")
    led_end = named_hole("jumper.led.end")
    sensor_start = named_hole("jumper.sensor.start")
    sensor_end = named_hole("jumper.sensor.end")

    draw_jumper(d, power_start, power_end, WIRE_ORANGE, (row_x(5), -70))
    draw_jumper(d, ldr_ground_start, ldr_ground_end, WIRE_BLACK, (row_x(6), -18))
    draw_jumper(d, led_ground_start, led_ground_end, WIRE_BLACK, (row_x(9), 26))
    draw_jumper(d, led_start, led_end, WIRE_RED, (row_x(10), 74))
    draw_jumper(d, sensor_start, sensor_end, WIRE_YELLOW, (row_x(5), 400))

    callout(d, led_ground_start, f"GND \u00b7 {led_ground_start}", (30, LABEL_Y_TOP), colour=WIRE_BLACK, anchor="start")
    callout(d, ldr_ground_start, f"GND \u00b7 {ldr_ground_start}", (168, LABEL_Y_TOP), colour=WIRE_BLACK, anchor="start")
    callout(d, power_start, f"3.3 V \u00b7 {power_start}", (306, LABEL_Y_TOP), colour="#c25c00", anchor="start")
    callout(d, led_start, f"GPIO 20 \u00b7 {led_start}", (450, LABEL_Y_TOP), colour=WIRE_RED, anchor="start")
    callout(d, sensor_start, f"GPIO 2 \u00b7 {sensor_start}", (26, LABEL_Y_BOTTOM), colour=ACCENT_DARK_YELLOW, anchor="start")
    return d


DIAGRAMS = {
    "common/microcontroller_seated_in_breadboard.png": diagram_microcontroller_seated,
    "project_1/led_placed.png": diagram_led_placed,
    "project_1/components_placed.png": diagram_components_placed,
    "project_1/wired_up.png": diagram_wired_up,
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--width", type=int, default=2200, help="output PNG width in pixels")
    parser.add_argument(
        "--source",
        type=Path,
        default=DEFAULT_SOURCE,
        help="TeX file containing the named breadboard coordinates",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=PROJECT_GUIDE_DIR / "images",
        help="directory to write PNGs into",
    )
    args = parser.parse_args()

    global COORDINATES
    try:
        COORDINATES = read_coordinates(args.source)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    rsvg = shutil.which("rsvg-convert")
    if rsvg is None:
        print("rsvg-convert not found; install it with: brew install librsvg", file=sys.stderr)
        return 1

    print(f"read {len(COORDINATES)} coordinates from {args.source}")
    for name, builder in DIAGRAMS.items():
        target = args.out / name
        target.parent.mkdir(parents=True, exist_ok=True)
        svg = builder().render()
        with tempfile.NamedTemporaryFile("w", suffix=".svg", delete=False) as fh:
            fh.write(svg)
            svg_path = fh.name
        subprocess.run([rsvg, "-w", str(args.width), "-o", str(target), svg_path], check=True)
        Path(svg_path).unlink()
        print(f"wrote {target}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
