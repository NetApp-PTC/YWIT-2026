#!/usr/bin/env python3
r"""Generate project-guide illustrations and breadboard wiring diagrams.

The project guide is the single source of truth: ``\bbhole{name}{coordinate}``
prints a coordinate in the PDF and gives it a stable name. This script reads
those definitions from the .tex file and uses them to draw every component,
wire, and callout.

Every project is drawn on the same breadboard with the same seated module, so the
seating coordinates come from common/microcontroller_seating.tex and each project
contributes only its own components.

Usage:
    python3 tools/diagram_generator.py [--project 3]

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
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
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

RAIL_Y = {"top-": 30, "top+": 54, "bot-": 414, "bot+": 438}

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

# Diagrams showing an upright module need room beside the board to draw it
# face-on, so those margins are per-diagram rather than fixed. Each module is
# drawn on the side of the board its own pins are seated in: the pixel module
# plugs into the back rows, the encoder into the front. The encoder needs the
# deeper margin because its knob stands taller than its PCB.
MARGIN_T_MODULE = 240
MARGIN_B_ENCODER = 304

CANVAS_W = BOARD_W + MARGIN_L + MARGIN_R

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

MODULE_PCB = "#16191d"
MODULE_EDGE = "#05070a"
LED_BODY = "#f6f4ef"
LED_LENS = "#eceae4"
SWITCH_BODY = "#2c3237"
SWITCH_EDGE = "#14181b"
SWITCH_CAP = "#4b5157"

WIRE_BLACK = "#2b2b2b"
WIRE_WHITE = "#f4f4f2"
WIRE_PURPLE = "#7657a8"
WIRE_BLUE = "#2563b8"
WIRE_GREEN = "#1f8a5a"
WIRE_YELLOW = "#e3b505"
WIRE_ORANGE = "#ef7d19"
WIRE_RED = "#d62828"

JUMPER_COLOUR_NAMES = {
    WIRE_BLACK: "black",
    WIRE_WHITE: "white",
    WIRE_PURPLE: "purple",
    WIRE_BLUE: "blue",
    WIRE_GREEN: "green",
    WIRE_YELLOW: "yellow",
    WIRE_ORANGE: "orange",
    WIRE_RED: "red",
}
JUMPERS_PER_COLOUR = 2

ACCENT = "#1f6f8b"
ACCENT_DARK_YELLOW = "#9c7a00"

# XIAO ESP32-C3 pins, ordered row 1 -> row 7 down each pin column. The board's
# own silkscreen is on the underside, so these are labelled with the GPIO
# numbers the project text refers to.
PINS_H = ["5V", "GND", "3V3", "IO10", "IO9", "IO8", "IO20"]
PINS_D = ["IO2", "IO3", "IO4", "IO5", "IO6", "IO7", "IO21"]

PROJECT_GUIDE_DIR = Path(__file__).resolve().parent.parent

# Seating the module is identical in every project, so its coordinates live in
# one shared file that every diagram is drawn against.
COMMON_SOURCE = PROJECT_GUIDE_DIR / "common" / "microcontroller_seating.tex"

COORD_RE = r"(?:[A-J]|top\+|top-|bot\+|bot-)(?:[1-9]|[12][0-9]|30)"

BBHOLE_RE = re.compile(
    r"\\bbhole\s*\{(?P<name>[A-Za-z0-9_.-]+)\}\s*"
    r"\{(?P<coordinate>" + COORD_RE + r")\}"
)
BBREF_RE = re.compile(r"\\bbref\s*\{(?P<name>[A-Za-z0-9_.-]+)\}")
CONNECTION_RE = re.compile(
    r"\\connectionrow\s*\{(?P<name>[A-Za-z0-9_.-]+)\}"
    r"\s*\{[^{}]*\}\s*\{(?P<xiao>[^{}]*)\}"
    r"\s*\{(?P<start>" + COORD_RE + r")\}"
    r"\s*\{(?P<end>" + COORD_RE + r")\}"
)

# A breadboard row is split into two independent nodes by the centre channel.
TOP_COLUMNS = frozenset("FGHIJ")
BOTTOM_COLUMNS = frozenset("ABCDE")

SEATING_COORDINATES = frozenset(
    {"mcu.5v", "mcu.gpio2", "mcu.gpio20", "mcu.gpio21"}
)

PROJECT_1_COORDINATES = frozenset(
    {
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
)

PIXEL_MODULE_COORDINATES = frozenset(
    {
        "pixels.header.power",
        "pixels.header.data",
        "pixels.header.ground",
    }
)

BUTTON_COORDINATES = frozenset({"button.side-a", "button.side-b"})

SPEAKER_COORDINATES = frozenset(
    {"speaker.lead.signal", "speaker.lead.ground"}
)

ENCODER_COORDINATES = frozenset(
    {
        "encoder.header.clk",
        "encoder.header.dt",
        "encoder.header.sw",
        "encoder.header.power",
        "encoder.header.ground",
    }
)

PROJECT_4_COORDINATES = frozenset(
    PIXEL_MODULE_COORDINATES
    | SPEAKER_COORDINATES
    | {
        "pad0.side-a",
        "pad0.side-b",
        "pad1.side-a",
        "pad1.side-b",
        "pad2.side-a",
        "pad2.side-b",
        "jumper.pad0-ground.start",
        "jumper.pad0-ground.end",
        "jumper.pad1-ground.start",
        "jumper.pad1-ground.end",
        "jumper.pad2-ground.start",
        "jumper.pad2-ground.end",
        "jumper.pixels-ground.start",
        "jumper.pixels-ground.end",
        "jumper.speaker-ground.start",
        "jumper.speaker-ground.end",
    }
)

OLED_COORDINATES = frozenset(
    {
        "oled.header.ground",
        "oled.header.power",
        "oled.header.scl",
        "oled.header.sda",
    }
)

MPU6050_COORDINATES = frozenset(
    {
        "mpu.header.vcc",
        "mpu.header.gnd",
        "mpu.header.scl",
        "mpu.header.sda",
        "mpu.header.xda",
        "mpu.header.xcl",
        "mpu.header.ad0",
        "mpu.header.int",
    }
)

PROJECT_6_COORDINATES = OLED_COORDINATES | MPU6050_COORDINATES

PROJECT_7_COORDINATES = (
    OLED_COORDINATES
    | ENCODER_COORDINATES
    | SPEAKER_COORDINATES
    | {
        "jumper.mcu-ground.start",
        "jumper.mcu-ground.end",
        "jumper.encoder-ground.start",
        "jumper.encoder-ground.end",
        "jumper.oled-ground.start",
        "jumper.oled-ground.end",
        "jumper.speaker-ground.start",
        "jumper.speaker-ground.end",
    }
)

COORDINATES: dict[str, str] = {}


def jumper_holes(connection: str) -> tuple[str, str]:
    """Map a \\connectionrow name onto the \\bbhole pair that wires it up."""
    return f"jumper.{connection}.start", f"jumper.{connection}.end"


def _xiao_pin_name(column: str) -> str:
    """Normalise a wiring table's XIAO column onto the board's own pin names."""
    label = re.sub(r"\(.*?\)", "", column).strip()
    gpio = re.fullmatch(r"GPIO\s*(\d+)", label)
    return f"IO{gpio.group(1)}" if gpio else label


def xiao_pin_nodes(coordinates: dict[str, str]) -> dict[str, tuple[frozenset[str], int]]:
    """Map each XIAO pin onto the breadboard node its seated pin shares.

    The two pin rows straddle the centre channel, so a pin on the 5V side can
    only be reached from the top half of the board and vice versa. Deriving this
    from the seating coordinates keeps it true if the module is ever re-seated.
    """
    nodes: dict[str, tuple[frozenset[str], int]] = {}
    for pins, seat in ((PINS_H, coordinates["mcu.5v"]), (PINS_D, coordinates["mcu.gpio2"])):
        columns = TOP_COLUMNS if seat[0] in TOP_COLUMNS else BOTTOM_COLUMNS
        first_row = int(seat[1:])
        for offset, pin in enumerate(pins):
            nodes[pin] = (columns, first_row + offset)
    return nodes


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


def read_coordinates(sources: list[Path], required: frozenset[str]) -> dict[str, str]:
    """Read and validate named breadboard coordinates across several TeX files."""
    coordinates: dict[str, str] = {}
    origin: dict[str, Path] = {}
    references: set[str] = set()
    connections: dict[str, str] = {}

    for source in sources:
        text = _without_tex_comments(source.read_text(encoding="utf-8"))
        for match in BBHOLE_RE.finditer(text):
            name = match.group("name")
            if name in coordinates:
                raise ValueError(
                    f"{source}: duplicate \\bbhole name {name!r}, already defined "
                    f"in {origin[name]}"
                )
            coordinates[name] = match.group("coordinate")
            origin[name] = source
        references |= {match.group("name") for match in BBREF_RE.finditer(text)}
        for match in CONNECTION_RE.finditer(text):
            connection = match.group("name")
            connections[connection] = match.group("xiao")
            for name, coordinate in zip(
                jumper_holes(connection),
                (match.group("start"), match.group("end")),
            ):
                if name in coordinates:
                    raise ValueError(
                        f"{source}: duplicate wiring hole {name!r}, already defined "
                        f"in {origin[name]}"
                    )
                coordinates[name] = coordinate
                origin[name] = source

    where = ", ".join(str(source) for source in sources)

    missing = sorted(required - coordinates.keys())
    if missing:
        raise ValueError(f"{where}: missing \\bbhole definitions: {', '.join(missing)}")

    undefined_refs = sorted(references - coordinates.keys())
    if undefined_refs:
        raise ValueError(f"{where}: undefined \\bbref names: {', '.join(undefined_refs)}")

    # Every connection row contributes exactly two validated breadboard holes.
    for connection in sorted(connections):
        unwired = [hole for hole in jumper_holes(connection) if hole not in coordinates]
        if unwired:
            raise ValueError(
                f"{where}: \\connectionrow {connection!r} has no holes: "
                f"missing {', '.join(unwired)}"
            )

    # A wire only reaches the GPIO its table row claims if it starts in that
    # pin's node. Getting this wrong wires the peripheral to a different pin
    # than the code drives, which no amount of redrawing would reveal.
    pin_nodes = xiao_pin_nodes(coordinates)
    for connection, column in sorted(connections.items()):
        pin = _xiao_pin_name(column)
        mpu_sources = {
            "MPU VCC": "mpu.header.vcc",
            "MPU GND": "mpu.header.gnd",
            "MPU SCL": "mpu.header.scl",
            "MPU SDA": "mpu.header.sda",
        }
        if pin in mpu_sources:
            source_coord = coordinates[mpu_sources[pin]]
            target_cols = BOTTOM_COLUMNS if source_coord[0] in BOTTOM_COLUMNS else TOP_COLUMNS
            target_row = int(source_coord[1:])
            start = coordinates[jumper_holes(connection)[0]]
            if start[0] not in target_cols or int(start[1:]) != target_row:
                raise ValueError(
                    f"{where}: \\connectionrow {connection!r} wires {pin} from {start}, "
                    f"but {pin} is at row {target_row}"
                )
            continue
        if pin not in pin_nodes:
            raise ValueError(
                f"{where}: \\connectionrow {connection!r} names XIAO pin {pin!r}, "
                f"which is not one of {', '.join(sorted(pin_nodes))}"
            )
        columns, row = pin_nodes[pin]
        start = coordinates[jumper_holes(connection)[0]]
        if start[0] not in columns or int(start[1:]) != row:
            reachable = ", ".join(f"{letter}{row}" for letter in sorted(columns))
            raise ValueError(
                f"{where}: \\connectionrow {connection!r} wires {pin} from {start}, "
                f"but the seated module puts {pin} on the node reached at "
                f"{reachable}"
            )

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
    """Return the centre of a breadboard hole such as "B16", "J7", or "top-2"."""
    coord = coord.strip()
    if coord.startswith(("top+", "top-", "bot+", "bot-")):
        rail = coord[:4]
        row = int(coord[4:])
        if not 1 <= row <= ROWS:
            raise ValueError(f"rail row out of range in {coord!r}")
        if rail not in RAIL_Y:
            raise ValueError(f"unknown breadboard rail in {coord!r}")
        return row_x(row), RAIL_Y[rail]
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

    def __init__(
        self,
        margin_top: int = MARGIN_T,
        margin_bottom: int = MARGIN_B,
        canvas_width: int = CANVAS_W,
        canvas_height: int | None = None,
        canvas_margin_left: int = MARGIN_L,
    ) -> None:
        self.margin_top = margin_top
        self.margin_bottom = margin_bottom
        self.canvas_width = canvas_width
        self.canvas_height = canvas_height
        self.canvas_margin_left = canvas_margin_left
        self.parts: list[str] = []
        self.defs: list[str] = []
        self.jumper_colours: Counter[str] = Counter()

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
        canvas_h = self.canvas_height or BOARD_H + self.margin_top + self.margin_bottom
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.canvas_width}" '
            f'height="{canvas_h}" viewBox="0 0 {self.canvas_width} {canvas_h}">\n'
            f"<defs>\n{defs}\n</defs>\n"
            f'<rect width="{CANVAS_W}" height="{canvas_h}" fill="#ffffff"/>\n'
            f'<g transform="translate({self.canvas_margin_left},{self.margin_top})">\n{body}\n</g>\n'
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
        ("top-", RAIL_BLUE),
        ("top+", RAIL_RED),
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


def draw_pixel_module(d: Drawing, pcb_row: int = 16) -> None:
    """The 3-pixel WS2812B module, drawn face-on in the margin above the board.

    Its right-angle header holds the PCB upright once seated, so a plan view
    would show nothing but the board's edge. Drawing it face-on and running
    leaders down to its three holes keeps the LEDs and the pad names readable
    while still saying exactly where it plugs in.
    """
    pads = (
        ("pixels.header.power", "5V"),
        ("pixels.header.data", "DI"),
        ("pixels.header.ground", "GND"),
    )
    pcb_x, pcb_y = row_x(pcb_row), -180.0
    pcb_w, pcb_h = 13 * PITCH, 96.0
    pin_x = pcb_x - 26
    pad_ys = [pcb_y + 24 + index * PITCH for index in range(3)]

    d.text(
        pcb_x + pcb_w / 2,
        pcb_y - 14,
        "3-pixel WS2812B module \u00b7 stands upright in the board",
        size=13,
        fill=LABEL_GREY,
        weight="bold",
    )

    # Header pins first, so the PCB covers the ends that sit behind it.
    for pad_y in pad_ys:
        for width, colour in ((6.5, LEAD_EDGE), (3.6, LEAD_FILL)):
            d.add(
                f'<line x1="{pcb_x:.2f}" y1="{pad_y:.2f}" x2="{pin_x:.2f}" '
                f'y2="{pad_y:.2f}" stroke="{colour}" stroke-width="{width}" '
                f'stroke-linecap="round"/>'
            )

    d.add(
        f'<rect x="{pcb_x + 3:.2f}" y="{pcb_y + 5:.2f}" width="{pcb_w}" '
        f'height="{pcb_h}" rx="7" fill="#000" opacity="0.15"/>'
    )
    d.add(
        f'<rect x="{pcb_x:.2f}" y="{pcb_y:.2f}" width="{pcb_w}" height="{pcb_h}" '
        f'rx="7" fill="{MODULE_PCB}" stroke="{MODULE_EDGE}" stroke-width="1.5"/>'
    )
    d.add(
        f'<rect x="{pcb_x - 4:.2f}" y="{pad_ys[0] - 14:.2f}" width="15" '
        f'height="{pad_ys[-1] - pad_ys[0] + 28:.2f}" rx="2" fill="#101215" '
        f'stroke="{MODULE_EDGE}" stroke-width="1"/>'
    )

    for index in range(3):
        led_x = pcb_x + 86 + index * 70
        led_y = pcb_y + pcb_h / 2
        d.add(
            f'<rect x="{led_x - 29:.2f}" y="{led_y - 29:.2f}" width="58" height="58" '
            f'rx="4" fill="{LED_BODY}" stroke="#c8c3b6" stroke-width="1.5"/>'
        )
        d.add(
            f'<circle cx="{led_x:.2f}" cy="{led_y:.2f}" r="21" fill="{LED_LENS}" '
            f'stroke="#cbc6ba" stroke-width="1.5"/>'
        )
        # Decoupling capacitor alongside each pixel, as on the real board.
        d.add(
            f'<rect x="{led_x - 29:.2f}" y="{led_y - 45:.2f}" width="13" height="8" '
            f'rx="2" fill="#57503f"/>'
        )

    # Unused output pads at the far end of the chain.
    for pad_y in pad_ys:
        d.add(
            f'<circle cx="{pcb_x + pcb_w - 22:.2f}" cy="{pad_y:.2f}" r="7" '
            f'fill="{PAD_FILL}" stroke="#9a7f45" stroke-width="1.5"/>'
        )

    for (name, label), pad_y in zip(pads, pad_ys):
        d.text(pcb_x + 15, pad_y + 3.5, label, size=10, fill="#eef1f4", anchor="start", weight="bold")
        hx, hy = hole(named_hole(name))
        d.add(
            f'<path d="M {pin_x:.2f} {pad_y:.2f} L {hx:.2f} {hy:.2f}" fill="none" '
            f'stroke="{ACCENT}" stroke-width="1.3" stroke-dasharray="5 4" opacity="0.8"/>'
        )
        d.add(
            f'<circle cx="{hx:.2f}" cy="{hy:.2f}" r="8.5" fill="none" stroke="{ACCENT}" '
            f'stroke-width="2.4"/>'
        )


def draw_rotary_encoder(d: Drawing) -> None:
    """Draw the KY-040 face-on in front of the board, over the holes it uses.

    The header is seated in the front row, so the module is drawn in front of
    the board with its pins reaching back into their holes and its knob facing
    out. That keeps the module and its leaders on the same side as its holes,
    leaving the far side of the board free for the wiring callouts.

    The pins are on the same 0.1" pitch as the breadboard, so each one is drawn
    directly in line with the hole it drops into and the leaders run straight
    back. Pins are ordered as the module's own silkscreen reads.
    """
    pins = (
        ("encoder.header.ground", "GND"),
        ("encoder.header.power", "+"),
        ("encoder.header.sw", "SW"),
        ("encoder.header.dt", "DT"),
        ("encoder.header.clk", "CLK"),
    )
    seated = [hole(named_hole(name)) for name, _ in pins]
    gaps = {second[0] - first[0] for first, second in zip(seated, seated[1:])}
    if gaps != {PITCH} or len({y for _, y in seated}) != 1:
        raise ValueError(
            "the KY-040's five pins are one rigid header strip, so they must be "
            "seated in consecutive holes of a single column, in silkscreen order: "
            + ", ".join(named_hole(name) for name, _ in pins)
        )

    pad_xs = [x for x, _ in seated]
    # The knob sits over the header, and the board reaches further to the left of
    # it than to the right to make room for the mounting holes.
    centre_x = (pad_xs[0] + pad_xs[-1]) / 2
    knob_r = 56.0
    pcb_w, pcb_h = 182.0, 162.0
    pcb_x = centre_x - 104.0

    def out(distance: float) -> float:
        """A y coordinate the given distance out from the board's front edge."""
        return BOARD_H + distance

    # Measured out from the board: the pins first, then the PCB carrying the
    # header block and the encoder can, and the knob standing proud of it all.
    pin_tip_y = out(12)
    pcb_y = out(46)
    header_y = pcb_y
    can_y, can_far_y = out(118), out(146)
    skirt_y, cap_y = out(160), out(240)

    d.define(
        '<linearGradient id="knob-cap" x1="0" y1="0" x2="1" y2="1">'
        '<stop offset="0" stop-color="#8b9094"/>'
        '<stop offset="0.35" stop-color="#e8ebec"/>'
        '<stop offset="0.62" stop-color="#5e6367"/>'
        '<stop offset="1" stop-color="#c0c4c7"/>'
        "</linearGradient>"
    )
    d.define(
        '<linearGradient id="knob-skirt" x1="0" y1="0" x2="1" y2="0">'
        '<stop offset="0" stop-color="#0b0b0d"/>'
        '<stop offset="0.28" stop-color="#3a3a40"/>'
        '<stop offset="0.6" stop-color="#1a1a1e"/>'
        '<stop offset="1" stop-color="#08080a"/>'
        "</linearGradient>"
    )

    d.text(
        centre_x,
        out(284),
        "KY-040 rotary encoder \u00b7 stands upright in the board",
        size=13,
        fill=LABEL_GREY,
        weight="bold",
    )
    d.add(
        f'<rect x="{pcb_x + 3:.2f}" y="{pcb_y + 5:.2f}" width="{pcb_w}" '
        f'height="{pcb_h}" rx="7" fill="#000" opacity="0.15"/>'
    )
    d.add(
        f'<rect x="{pcb_x:.2f}" y="{pcb_y:.2f}" width="{pcb_w}" height="{pcb_h}" '
        f'rx="7" fill="{MODULE_PCB}" stroke="{MODULE_EDGE}" stroke-width="1.5"/>'
    )

    # The two silkscreened mounting holes down the left edge of the board.
    for mount_y in (out(78), out(182)):
        d.add(
            f'<circle cx="{pcb_x + 18:.2f}" cy="{mount_y:.2f}" r="12" fill="none" '
            f'stroke="#e8ecef" stroke-width="3.5"/>'
        )
        d.add(f'<circle cx="{pcb_x + 18:.2f}" cy="{mount_y:.2f}" r="7.5" fill="#3b4045"/>')

    # Plated encoder can, with the green solder-side edge showing at the end
    # nearest the pins.
    d.add(
        f'<rect x="{centre_x - 35:.2f}" y="{out(110):.2f}" width="70" height="10" '
        f'rx="2" fill="#1f6f6b" stroke="#12403f" stroke-width="1"/>'
    )
    d.add(
        f'<rect x="{centre_x - 31:.2f}" y="{can_y:.2f}" '
        f'width="62" height="{can_far_y - can_y:.2f}" '
        f'rx="3" fill="#b4b8bb" stroke="#7f858a" stroke-width="1.3"/>'
    )

    # Knurled aluminium knob: a fluted skirt under a brushed, domed cap. It is
    # taller than the PCB and stands proud of its far edge, as on the real part.
    d.add(
        f'<path d="M {centre_x - knob_r:.2f} {cap_y:.2f} L {centre_x - knob_r:.2f} '
        f'{skirt_y:.2f} A {knob_r} 15 0 0 1 {centre_x + knob_r:.2f} '
        f'{skirt_y:.2f} L {centre_x + knob_r:.2f} {cap_y:.2f} Z" '
        f'fill="url(#knob-skirt)"/>'
    )
    for index in range(1, 10):
        flute_x = centre_x - knob_r + index * knob_r / 5
        d.add(
            f'<line x1="{flute_x:.2f}" y1="{cap_y:.2f}" x2="{flute_x:.2f}" '
            f'y2="{skirt_y + 4:.2f}" stroke="#55555c" stroke-width="1.6" '
            f'opacity="0.55"/>'
        )
    d.add(
        f'<ellipse cx="{centre_x:.2f}" cy="{cap_y:.2f}" rx="{knob_r}" ry="21" '
        f'fill="url(#knob-cap)" stroke="#6e7377" stroke-width="1.2"/>'
    )
    d.add(
        f'<ellipse cx="{centre_x:.2f}" cy="{cap_y:.2f}" rx="{knob_r - 13:.2f}" ry="14" '
        f'fill="none" stroke="#f2f4f5" stroke-width="1" opacity="0.35"/>'
    )
    d.add(
        f'<line x1="{centre_x:.2f}" y1="{cap_y - 19:.2f}" x2="{centre_x:.2f}" '
        f'y2="{cap_y + 19:.2f}" stroke="#4e5357" stroke-width="1" opacity="0.5"/>'
    )

    # Header: black plastic block on the board with the pins passing through it.
    d.add(
        f'<rect x="{pad_xs[0] - 14:.2f}" y="{header_y:.2f}" '
        f'width="{pad_xs[-1] - pad_xs[0] + 28:.2f}" height="20" rx="2" '
        f'fill="#0c0e10" stroke="{MODULE_EDGE}" stroke-width="1"/>'
    )

    for (name, label), pad_x in zip(pins, pad_xs):
        for width, colour in ((6.0, LEAD_EDGE), (3.4, LEAD_FILL)):
            d.add(
                f'<line x1="{pad_x:.2f}" y1="{header_y + 16:.2f}" x2="{pad_x:.2f}" '
                f'y2="{pin_tip_y:.2f}" stroke="{colour}" stroke-width="{width}" '
                f'stroke-linecap="round"/>'
            )
        # Rotated the opposite way to the real silkscreen, since the module is
        # drawn from the far side with its pins pointing back at the board.
        d.add(
            f'<text transform="translate({pad_x - 3.5:.2f},{out(72):.2f}) '
            f'rotate(90)" font-family="Helvetica, Arial, sans-serif" font-size="12" '
            f'font-weight="bold" fill="#eef1f4" text-anchor="start">{esc(label)}</text>'
        )

        hx, hy = hole(named_hole(name))
        d.add(
            f'<path d="M {pad_x:.2f} {pin_tip_y:.2f} L {hx:.2f} {hy:.2f}" '
            f'fill="none" stroke="{ACCENT}" stroke-width="1.3" '
            f'stroke-dasharray="5 4" opacity="0.8"/>'
        )
        d.add(
            f'<circle cx="{hx:.2f}" cy="{hy:.2f}" r="8.5" fill="none" '
            f'stroke="{ACCENT}" stroke-width="2.4"/>'
        )


def draw_speaker(d: Drawing) -> None:
    """Draw the external mini speaker and its two adapted Dupont leads.

    Like the encoder, it is drawn in front of the board, on the same side as the
    holes its leads plug into.
    """
    centre_x, centre_y = row_x(25), BOARD_H + 113.0
    radius = 66.0
    signal_hole = hole(named_hole("speaker.lead.signal"))
    ground_hole = hole(named_hole("speaker.lead.ground"))

    d.text(
        centre_x,
        centre_y + radius + 24,
        "1 W, 8 \u03a9 mini speaker",
        size=13,
        fill=LABEL_GREY,
        weight="bold",
    )
    d.add(
        f'<circle cx="{centre_x + 4:.2f}" cy="{centre_y + 5:.2f}" r="{radius}" '
        f'fill="#000" opacity="0.15"/>'
    )
    d.add(
        f'<circle cx="{centre_x:.2f}" cy="{centre_y:.2f}" r="{radius}" '
        f'fill="#343a40" stroke="#171b1f" stroke-width="2"/>'
    )
    d.add(
        f'<circle cx="{centre_x:.2f}" cy="{centre_y:.2f}" r="{radius - 13}" '
        f'fill="#1f2428" stroke="#515960" stroke-width="1.5"/>'
    )
    d.add(
        f'<circle cx="{centre_x:.2f}" cy="{centre_y:.2f}" r="{radius - 28}" '
        f'fill="#454c52" stroke="#171b1f" stroke-width="1.5"/>'
    )
    d.add(
        f'<circle cx="{centre_x - 13:.2f}" cy="{centre_y - 15:.2f}" r="9" '
        f'fill="#7b838a" opacity="0.55"/>'
    )

    terminals = (
        (centre_x - 22, centre_y - radius + 4, signal_hole, WIRE_RED, "+"),
        (centre_x + 22, centre_y - radius + 4, ground_hole, WIRE_BLACK, "\u2212"),
    )
    for start_x, start_y, (end_x, end_y), colour, label in terminals:
        path = (
            f"M {start_x:.2f} {start_y:.2f} "
            f"C {start_x:.2f} {BOARD_H - 30}, {end_x:.2f} {BOARD_H - 95}, "
            f"{end_x:.2f} {end_y:.2f}"
        )
        d.add(
            f'<path d="{path}" fill="none" stroke="#000" stroke-width="8" '
            f'opacity="0.13" transform="translate(2,4)"/>'
        )
        d.add(
            f'<path d="{path}" fill="none" stroke="#17171a" stroke-width="7.5" '
            f'stroke-linecap="round"/>'
        )
        d.add(
            f'<path d="{path}" fill="none" stroke="{colour}" stroke-width="5" '
            f'stroke-linecap="round"/>'
        )
        d.text(
            start_x,
            start_y + 16,
            label,
            size=12,
            fill="#ffffff",
            weight="bold",
        )
        d.add(
            f'<circle cx="{end_x:.2f}" cy="{end_y:.2f}" r="8.5" fill="none" '
            f'stroke="{ACCENT}" stroke-width="2.4"/>'
        )


def draw_tactile_button(
    d: Drawing,
    side_a: str,
    side_b: str,
    label: str | None = "6 \u00d7 6 mm button",
    label_offset_x: float = 94,
    anchor: str = "start",
) -> None:
    """A 6 x 6 mm tactile switch seated with all four legs in one half of the board.

    The legs sit on a rectangle three holes across the columns and two along the
    rows, so they leave the body's top and bottom edges. Each pair is joined
    inside the switch and lands in a single five-hole group, leaving the switch
    to bridge the gap between the two groups.
    """
    ax, ay = hole(side_a)
    bx, _ = hole(side_b)
    paired_y = ay - 3 * PITCH
    centre_x, centre_y = (ax + bx) / 2, (ay + paired_y) / 2
    body = 56.0
    legs = ((ax, ay), (ax, paired_y), (bx, ay), (bx, paired_y))

    for leg_x, leg_y in legs:
        anchor_y = centre_y + 14 if leg_y > centre_y else centre_y - 14
        for width, colour in ((6.5, LEAD_EDGE), (3.6, LEAD_FILL)):
            d.add(
                f'<line x1="{leg_x:.2f}" y1="{anchor_y:.2f}" x2="{leg_x:.2f}" '
                f'y2="{leg_y:.2f}" stroke="{colour}" stroke-width="{width}" '
                f'stroke-linecap="round"/>'
            )

    d.add(
        f'<rect x="{centre_x - body / 2 + 3:.2f}" y="{centre_y - body / 2 + 4:.2f}" '
        f'width="{body}" height="{body}" rx="5" fill="#000" opacity="0.15"/>'
    )
    d.add(
        f'<rect x="{centre_x - body / 2:.2f}" y="{centre_y - body / 2:.2f}" '
        f'width="{body}" height="{body}" rx="5" fill="{SWITCH_BODY}" '
        f'stroke="{SWITCH_EDGE}" stroke-width="1.5"/>'
    )
    d.add(
        f'<circle cx="{centre_x:.2f}" cy="{centre_y:.2f}" r="15" fill="{SWITCH_CAP}" '
        f'stroke="#23282c" stroke-width="1.5"/>'
    )
    d.add(
        f'<circle cx="{centre_x - 4.5:.2f}" cy="{centre_y - 5:.2f}" r="4" fill="#767d84"/>'
    )

    for leg_x, leg_y in legs:
        d.add(f'<circle cx="{leg_x:.2f}" cy="{leg_y:.2f}" r="2.6" fill="#17171a" opacity="0.6"/>')

    if label:
        # The empty centre channel is the one place a label can sit uncluttered.
        d.text(
            centre_x + label_offset_x,
            CHANNEL_TOP_Y + 32,
            label,
            size=12,
            fill=LABEL_GREY,
            anchor=anchor,
            weight="bold",
        )


def _oled_screen_maze(d: Drawing, x: float, y: float, w: float, h: float) -> None:
    """The maze game as it looks on the panel: title, walls, ball, and goal."""
    d.add(
        f'<line x1="{x + 4:.2f}" y1="{y + 14:.2f}" '
        f'x2="{x + w - 4:.2f}" y2="{y + 14:.2f}" stroke="#ffffff" '
        f'stroke-width="0.7" opacity="0.5"/>'
    )
    d.text(
        x + w / 2,
        y + 10,
        "TILTING MAZE",
        size=8,
        fill="#ffffff",
        anchor="middle",
        weight="bold",
    )

    # Monochrome maze walls
    d.add(
        f'<line x1="{x + 16:.2f}" y1="{y + 18:.2f}" '
        f'x2="{x + 16:.2f}" y2="{y + 44:.2f}" stroke="#ffffff" '
        f'stroke-width="1.8"/>'
    )
    d.add(
        f'<line x1="{x + 38:.2f}" y1="{y + 24:.2f}" '
        f'x2="{x + 38:.2f}" y2="{y + 54:.2f}" stroke="#ffffff" '
        f'stroke-width="1.8"/>'
    )
    d.add(
        f'<line x1="{x + 62:.2f}" y1="{y + 18:.2f}" '
        f'x2="{x + 62:.2f}" y2="{y + 42:.2f}" stroke="#ffffff" '
        f'stroke-width="1.8"/>'
    )
    d.add(
        f'<line x1="{x + 16:.2f}" y1="{y + 44:.2f}" '
        f'x2="{x + 62:.2f}" y2="{y + 44:.2f}" stroke="#ffffff" '
        f'stroke-width="1.8"/>'
    )

    # Ball sprite (filled white)
    d.add(f'<circle cx="{x + 27:.2f}" cy="{y + 30:.2f}" r="3.8" fill="#ffffff"/>')

    # Checkered goal area
    d.add(
        f'<rect x="{x + 66:.2f}" y="{y + 42:.2f}" width="16" '
        f'height="10" fill="none" stroke="#ffffff" stroke-width="1"/>'
    )
    for gx in range(int(x + 68), int(x + 80), 3):
        for gy in range(int(y + 44), int(y + 51), 3):
            d.add(f'<rect x="{gx}" y="{gy}" width="1.4" height="1.4" fill="#ffffff"/>')
    d.text(
        x + 74,
        y + 50,
        "GOAL",
        size=5.5,
        fill="#ffffff",
        anchor="middle",
        weight="bold",
    )


def _oled_screen_clock(d: Drawing, x: float, y: float, w: float, h: float) -> None:
    """The desk companion's clock screen: big time, date, and weather line."""
    d.text(
        x + w / 2,
        y + 26,
        "7:12",
        size=22,
        fill="#ffffff",
        anchor="middle",
        weight="bold",
    )
    d.text(
        x + w / 2,
        y + 39,
        "SAT SEP 20",
        size=7,
        fill="#ffffff",
        anchor="middle",
        weight="bold",
    )
    d.add(
        f'<line x1="{x + 6:.2f}" y1="{y + 44:.2f}" x2="{x + w - 6:.2f}" '
        f'y2="{y + 44:.2f}" stroke="#ffffff" stroke-width="0.7" opacity="0.5"/>'
    )
    d.text(
        x + w / 2,
        y + 54,
        "58F CLOUDY",
        size=7.5,
        fill="#ffffff",
        anchor="middle",
        weight="bold",
    )
    # The alarm bell glyph the firmware draws when an alarm is armed.
    d.add(
        f'<path d="M {x + 8:.2f} {y + 12:.2f} l 0 -4 a 3 3 0 0 1 6 0 l 0 4 l 1.5 2 '
        f'l -9 0 Z" fill="#ffffff"/>'
    )


def draw_oled_module(
    d: Drawing,
    draw_screen: Callable[[Drawing, float, float, float, float], None] = _oled_screen_maze,
) -> None:
    """Draw the 0.96-inch SSD1306 OLED module flat on the board (top-down view)."""
    pins = (
        ("oled.header.ground", "GND"),
        ("oled.header.power", "VCC"),
        ("oled.header.scl", "SCL"),
        ("oled.header.sda", "SDA"),
    )
    seated = [hole(named_hole(name)) for name, _ in pins]
    gaps = {second[0] - first[0] for first, second in zip(seated, seated[1:])}
    if gaps != {PITCH} or len({y for _, y in seated}) != 1:
        raise ValueError(
            "the OLED's four pins are one rigid header strip, so they must be "
            "seated in consecutive holes of a single column, in silkscreen order: "
            + ", ".join(named_hole(name) for name, _ in pins)
        )

    pad_xs = [x for x, _ in seated]
    centre_x = (pad_xs[0] + pad_xs[-1]) / 2

    pcb_w, pcb_h = 150.0, 142.0
    pcb_x = centre_x - pcb_w / 2
    # Pins enter row A (y = 366); PCB extends downward
    pcb_y = COLUMN_Y["A"] - 10.0

    # Module drop shadow
    d.add(
        f'<rect x="{pcb_x + 3:.2f}" y="{pcb_y + 4:.2f}" width="{pcb_w}" '
        f'height="{pcb_h}" rx="6" fill="#000" opacity="0.18"/>'
    )
    # PCB body (black)
    d.add(
        f'<rect x="{pcb_x:.2f}" y="{pcb_y:.2f}" width="{pcb_w}" height="{pcb_h}" '
        f'rx="6" fill="{MODULE_PCB}" stroke="{MODULE_EDGE}" stroke-width="1.5"/>'
    )

    # Corner mounting holes
    for mx, my in (
        (pcb_x + 10, pcb_y + 10),
        (pcb_x + pcb_w - 10, pcb_y + 10),
        (pcb_x + 10, pcb_y + pcb_h - 10),
        (pcb_x + pcb_w - 10, pcb_y + pcb_h - 10),
    ):
        d.add(
            f'<circle cx="{mx:.2f}" cy="{my:.2f}" r="6" fill="none" '
            f'stroke="#e8ecef" stroke-width="1.8"/>'
        )
        d.add(f'<circle cx="{mx:.2f}" cy="{my:.2f}" r="3.5" fill="#2c3035"/>')

    # 4 Solder pads for the straight header pins at row A
    for (name, label), pad_x in zip(pins, pad_xs):
        pad_y = COLUMN_Y["A"]
        # Outer copper annular ring
        d.add(
            f'<circle cx="{pad_x:.2f}" cy="{pad_y:.2f}" r="5.5" fill="{PAD_FILL}" '
            f'stroke="#a8843f" stroke-width="1"/>'
        )
        # Inner solder joint with pin head
        d.add(
            f'<circle cx="{pad_x:.2f}" cy="{pad_y:.2f}" r="3.2" fill="#c6ccd2" '
            f'stroke="#70767c" stroke-width="0.8"/>'
        )
        d.add(f'<circle cx="{pad_x:.2f}" cy="{pad_y:.2f}" r="1.4" fill="#17171a"/>')
        # Pin label printed on PCB below pad
        d.text(pad_x, pad_y + 13, label, size=7.5, fill="#eef1f4", anchor="middle", weight="bold")

    # OLED Glass Panel
    glass_x, glass_y = pcb_x + 14, pcb_y + 26
    glass_w, glass_h = pcb_w - 28, pcb_h - 38
    d.add(
        f'<rect x="{glass_x:.2f}" y="{glass_y:.2f}" width="{glass_w:.2f}" '
        f'height="{glass_h:.2f}" rx="3" fill="#080b10" stroke="#1f2833" '
        f'stroke-width="1.3"/>'
    )

    # Active monochrome OLED display area
    disp_x, disp_y = glass_x + 6, glass_y + 6
    disp_w, disp_h = glass_w - 12, glass_h - 12
    d.add(
        f'<rect x="{disp_x:.2f}" y="{disp_y:.2f}" width="{disp_w:.2f}" '
        f'height="{disp_h:.2f}" fill="#040608" stroke="#10141a" stroke-width="0.8"/>'
    )

    draw_screen(d, disp_x, disp_y, disp_w, disp_h)


def draw_mpu6050(d: Drawing) -> None:
    """Draw the MPU-6050 GY-521 (ITG/MPU) breakout board flat on the board (top-down view)."""
    pins = (
        ("mpu.header.int", "INT"),
        ("mpu.header.ad0", "AD0"),
        ("mpu.header.xcl", "XCL"),
        ("mpu.header.xda", "XDA"),
        ("mpu.header.sda", "SDA"),
        ("mpu.header.scl", "SCL"),
        ("mpu.header.gnd", "GND"),
        ("mpu.header.vcc", "VCC"),
    )
    seated = [hole(named_hole(name)) for name, _ in pins]
    gaps = {second[0] - first[0] for first, second in zip(seated, seated[1:])}
    if gaps != {PITCH} or len({y for _, y in seated}) != 1:
        raise ValueError(
            "the MPU-6050's eight pins are one rigid header strip, so they must be "
            "seated in consecutive holes of a single column, in silkscreen order: "
            + ", ".join(named_hole(name) for name, _ in pins)
        )

    pad_xs = [x for x, _ in seated]
    centre_x = (pad_xs[0] + pad_xs[-1]) / 2
    pcb_w, pcb_h = (len(pins) - 1) * PITCH + 34.0, 102.0
    pcb_x = centre_x - pcb_w / 2
    # Pins enter row A (y = 366); PCB extends downward
    pcb_y = COLUMN_Y["A"] - 10.0

    # PCB drop shadow
    d.add(
        f'<rect x="{pcb_x + 3:.2f}" y="{pcb_y + 4:.2f}" width="{pcb_w}" '
        f'height="{pcb_h}" rx="5" fill="#000" opacity="0.18"/>'
    )
    # Royal blue PCB body
    d.add(
        f'<rect x="{pcb_x:.2f}" y="{pcb_y:.2f}" width="{pcb_w}" height="{pcb_h}" '
        f'rx="5" fill="#145da0" stroke="#0c3d6c" stroke-width="1.5"/>'
    )

    # 8 Solder pads for the straight header pins at row A (INT -> VCC)
    for (name, label), pad_x in zip(pins, pad_xs):
        pad_y = COLUMN_Y["A"]
        # Outer copper annular ring
        d.add(
            f'<circle cx="{pad_x:.2f}" cy="{pad_y:.2f}" r="5.5" fill="{PAD_FILL}" '
            f'stroke="#a8843f" stroke-width="1"/>'
        )
        # Inner solder joint with pin head
        d.add(
            f'<circle cx="{pad_x:.2f}" cy="{pad_y:.2f}" r="3.2" fill="#c6ccd2" '
            f'stroke="#70767c" stroke-width="0.8"/>'
        )
        d.add(f'<circle cx="{pad_x:.2f}" cy="{pad_y:.2f}" r="1.4" fill="#17171a"/>')
        # Pin label printed on PCB below pad
        d.text(pad_x, pad_y + 13, label, size=7, fill="#ffffff", anchor="middle", weight="bold")

    # InvenSense MPU-6050 main QFN chip
    chip_w = 38.0
    chip_x = centre_x - chip_w / 2 - 2
    chip_y = pcb_y + 26
    d.add(
        f'<rect x="{chip_x:.2f}" y="{chip_y:.2f}" width="{chip_w}" '
        f'height="{chip_w}" rx="2" fill="#1a1c1e" stroke="#0e0f10" stroke-width="1"/>'
    )
    # QFN lead pads on edges
    for i in range(4):
        offset = 5 + i * 8
        d.add(f'<rect x="{chip_x + offset:.2f}" y="{chip_y - 1.5:.2f}" width="3.5" height="1.5" fill="#silver"/>')
        d.add(f'<rect x="{chip_x + offset:.2f}" y="{chip_y + chip_w:.2f}" width="3.5" height="1.5" fill="#silver"/>')
        d.add(f'<rect x="{chip_x - 1.5:.2f}" y="{chip_y + offset:.2f}" width="1.5" height="3.5" fill="#silver"/>')
        d.add(f'<rect x="{chip_x + chip_w:.2f}" y="{chip_y + offset:.2f}" width="1.5" height="3.5" fill="#silver"/>')
    # Pin 1 dot
    d.add(
        f'<circle cx="{chip_x + 5:.2f}" cy="{chip_y + 5:.2f}" r="1.8" fill="#8d9499"/>'
    )
    d.text(
        chip_x + chip_w / 2,
        chip_y + 15,
        "INVENSENSE",
        size=4.2,
        fill="#b0b5b9",
        anchor="middle",
        weight="bold",
    )
    d.text(
        chip_x + chip_w / 2,
        chip_y + 23,
        "MPU-6050",
        size=5.5,
        fill="#ffffff",
        anchor="middle",
        weight="bold",
    )

    # Tantalum capacitor (orange/tan SMD rectangle) below chip
    cap_x = centre_x - 18
    cap_y = pcb_y + 68
    d.add(
        f'<rect x="{cap_x:.2f}" y="{cap_y:.2f}" width="{20:.2f}" height="{13:.2f}" '
        f'rx="1.5" fill="#c97834" stroke="#8d4a15" stroke-width="0.8"/>'
    )
    d.add(
        f'<rect x="{cap_x:.2f}" y="{cap_y:.2f}" width="{4:.2f}" height="13" '
        f'fill="#7d3b08"/>'
    )
    d.text(cap_x + 11, cap_y + 9, "106C", size=5, fill="#fcedda", anchor="middle", weight="bold")

    # SMD passive resistors / capacitors around the chip
    for rx, ry, rlbl in (
        (chip_x - 14, pcb_y + 28, "102"),
        (chip_x - 14, pcb_y + 40, "472"),
        (chip_x + chip_w + 6, pcb_y + 28, "222"),
        (chip_x + chip_w + 6, pcb_y + 40, "222"),
    ):
        d.add(f'<rect x="{rx:.2f}" y="{ry:.2f}" width="8.5" height="5" rx="0.8" fill="#1e2022" stroke="#555" stroke-width="0.5"/>')
        d.text(rx + 4.25, ry + 4, rlbl, size=3.5, fill="#bbb", anchor="middle")

    for cx, cy in (
        (chip_x - 14, pcb_y + 52),
        (chip_x + chip_w + 6, pcb_y + 52),
    ):
        d.add(f'<rect x="{cx:.2f}" y="{cy:.2f}" width="8.5" height="5" rx="0.8" fill="#c4aa82" stroke="#8c7554" stroke-width="0.5"/>')

    # Voltage regulator & power LED on right side near VCC (pad_xs[7])
    vreg_x = pad_xs[7] - 6
    vreg_y = pcb_y + 58
    d.add(
        f'<rect x="{vreg_x:.2f}" y="{vreg_y:.2f}" width="12" height="8.5" rx="1.2" '
        f'fill="#1f2224" stroke="#111" stroke-width="0.7"/>'
    )
    d.text(vreg_x + 6, vreg_y + 6.2, "DE=A1D", size=3.2, fill="#aaa", anchor="middle")
    d.add(
        f'<rect x="{pad_xs[6] - 3.5:.2f}" y="{pcb_y + 58:.2f}" width="7" height="5" rx="0.8" '
        f'fill="#2a8038" stroke="#1f5a28" stroke-width="0.7"/>'
    )

    # 2 Mounting holes along the bottom edge
    for mx in (pcb_x + 13, pcb_x + pcb_w - 13):
        d.add(
            f'<circle cx="{mx:.2f}" cy="{pcb_y + pcb_h - 14:.2f}" r="7" fill="none" '
            f'stroke="#d8b268" stroke-width="1.8"/>'
        )
        d.add(f'<circle cx="{mx:.2f}" cy="{pcb_y + pcb_h - 14:.2f}" r="4" fill="#2c3035"/>')

    # Silkscreen "ITG/MPU" and "HC" along the bottom
    d.text(
        pcb_x + 24,
        pcb_y + pcb_h - 11,
        "HC",
        size=7,
        fill="#ffffff",
        anchor="start",
        weight="bold",
    )
    d.text(
        pcb_x + 40,
        pcb_y + pcb_h - 11,
        "ITG/MPU",
        size=7.5,
        fill="#ffffff",
        anchor="start",
        weight="bold",
    )

    # Coordinate axes silkscreen on top-left near INT (pad_xs[0])
    axis_x = pcb_x + 16
    axis_y = pcb_y + 36
    d.add(
        f'<line x1="{axis_x:.2f}" y1="{axis_y:.2f}" x2="{axis_x + 12:.2f}" '
        f'y2="{axis_y:.2f}" stroke="#ffffff" stroke-width="1.1"/>'
    )
    d.add(
        f'<polygon points="{axis_x + 12:.2f},{axis_y - 2:.2f} {axis_x + 15:.2f},{axis_y:.2f} {axis_x + 12:.2f},{axis_y + 2:.2f}" fill="#ffffff"/>'
    )
    d.text(axis_x + 17, axis_y + 2.5, "X", size=5.5, fill="#ffffff", anchor="start", weight="bold")
    d.add(
        f'<line x1="{axis_x:.2f}" y1="{axis_y:.2f}" x2="{axis_x:.2f}" '
        f'y2="{axis_y + 10:.2f}" stroke="#ffffff" stroke-width="1.1"/>'
    )
    d.add(
        f'<polygon points="{axis_x - 2:.2f},{axis_y + 10:.2f} {axis_x:.2f},{axis_y + 13:.2f} {axis_x + 2:.2f},{axis_y + 10:.2f}" fill="#ffffff"/>'
    )
    d.text(axis_x, axis_y + 17, "Y", size=5.5, fill="#ffffff", anchor="middle", weight="bold")
    d.add(f'<circle cx="{axis_x:.2f}" cy="{axis_y:.2f}" r="2.5" fill="none" stroke="#ffffff" stroke-width="0.9"/>')
    d.add(f'<circle cx="{axis_x:.2f}" cy="{axis_y:.2f}" r="0.9" fill="#ffffff"/>')


def _quad_point(p0, c, p1, t):
    u = 1 - t
    return (
        u * u * p0[0] + 2 * u * t * c[0] + t * t * p1[0],
        u * u * p0[1] + 2 * u * t * c[1] + t * t * p1[1],
    )


def draw_jumper(d: Drawing, start: str, end: str, colour: str, control: tuple[float, float]) -> None:
    """A jumper wire drawn as an arc that never crosses the module body."""
    try:
        colour_name = JUMPER_COLOUR_NAMES[colour]
    except KeyError as exc:
        available = ", ".join(JUMPER_COLOUR_NAMES.values())
        raise ValueError(
            f"jumper {start}->{end} uses unavailable colour {colour!r}; "
            f"choose from {available}"
        ) from exc
    d.jumper_colours[colour_name] += 1
    if d.jumper_colours[colour_name] > JUMPERS_PER_COLOUR:
        raise ValueError(
            f"diagram uses more than {JUMPERS_PER_COLOUR} {colour_name} jumpers"
        )

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


def diagram_breadboard_overview() -> Drawing:
    d = Drawing()
    draw_breadboard(d)
    draw_xiao(d)

    callout(
        d,
        "top-2",
        "Ground Bar (all connected together)",
        (8, LABEL_Y_TOP),
        colour=RAIL_BLUE,
        anchor="start",
    )
    callout(
        d,
        "top+2",
        "Power Bar (all connected together)",
        (445, LABEL_Y_TOP),
        colour=RAIL_RED,
        anchor="start",
    )

    bus_x = row_x(20)
    bus_top = COLUMN_Y["E"] - 11
    bus_bottom = COLUMN_Y["A"] + 11
    d.add(
        f'<rect x="{bus_x - 8.5:.2f}" y="{bus_top}" width="17" '
        f'height="{bus_bottom - bus_top}" rx="5" fill="#4c9d9a" '
        'fill-opacity="0.12" stroke="#328b91" stroke-width="2"/>'
    )
    for column in "ABCDE":
        _, y = hole(f"{column}20")
        d.add(
            f'<circle cx="{bus_x:.2f}" cy="{y:.2f}" r="6.5" fill="none" '
            'stroke="#328b91" stroke-width="1.7"/>'
        )
    bus_label_x = bus_x
    bus_label_y = (CHANNEL_TOP_Y + CHANNEL_BOT_Y) / 2 + 4
    d.add(
        f'<path d="M {bus_x:.2f} {bus_label_y + 3} L {bus_x:.2f} {bus_top:.2f}" '
        'fill="none" stroke="#328b91" stroke-width="1.5"/>'
    )
    d.text(
        bus_label_x,
        bus_label_y,
        "Bus Strip (all connected together)",
        size=13.5,
        fill="#267d83",
        anchor="middle",
        weight="bold",
    )
    return d


def diagram_button_illustration() -> Drawing:
    d = Drawing(margin_top=0, margin_bottom=0, canvas_width=850, canvas_height=420)
    d.add('<rect x="0" y="0" width="850" height="420" fill="#ffffff"/>')

    d.text(172, 48, "Top view", size=17, fill=LABEL_GREY, weight="bold")
    d.text(400, 48, "Underside", size=17, fill=LABEL_GREY, weight="bold")

    # Top view: square case, round actuator, and four corner terminals.
    top_x, top_y, top_size = 100, 100, 144
    d.add(
        f'<rect x="{top_x + 4}" y="{top_y + 5}" width="{top_size}" height="{top_size}" '
        f'rx="12" fill="#000" opacity="0.14"/>'
    )
    d.add(
        f'<rect x="{top_x}" y="{top_y}" width="{top_size}" height="{top_size}" '
        f'rx="12" fill="{SWITCH_BODY}" stroke="{SWITCH_EDGE}" stroke-width="3"/>'
    )
    for pin_x in (top_x + 15, top_x + top_size - 15):
        for pin_y in (top_y + 15, top_y + top_size - 15):
            d.add(
                f'<circle cx="{pin_x}" cy="{pin_y}" r="8" fill="{LEAD_FILL}" '
                f'stroke="{LEAD_EDGE}" stroke-width="2"/>'
            )
    centre_x = top_x + top_size / 2
    centre_y = top_y + top_size / 2
    d.add(
        f'<circle cx="{centre_x}" cy="{centre_y}" r="42" fill="{SWITCH_CAP}" '
        'stroke="#23282c" stroke-width="3"/>'
    )
    d.add(f'<circle cx="{centre_x - 11}" cy="{centre_y - 13}" r="9" fill="#767d84"/>')

    # Underside: molded plastic rails mark the two internally connected pairs.
    body_x, body_y, body_size = 320, 100, 160
    d.add(
        f'<rect x="{body_x + 4}" y="{body_y + 5}" width="{body_size}" height="{body_size}" '
        f'rx="12" fill="#000" opacity="0.14"/>'
    )
    d.add(
        f'<rect x="{body_x}" y="{body_y}" width="{body_size}" height="{body_size}" '
        f'rx="12" fill="{SWITCH_BODY}" stroke="{SWITCH_EDGE}" stroke-width="3"/>'
    )
    d.add(
        f'<rect x="{body_x + 29}" y="{body_y + 43}" width="{body_size - 58}" '
        f'height="{body_size - 86}" rx="8" fill="#252a2e" stroke="#51575c" stroke-width="2"/>'
    )
    for rail_y in (body_y + 17, body_y + body_size - 43):
        d.add(
            f'<rect x="{body_x + 20}" y="{rail_y}" width="{body_size - 40}" height="26" '
            'rx="6" fill="#555d63" stroke="#858d92" stroke-width="2"/>'
        )
        d.add(
            f'<line x1="{body_x + 31}" y1="{rail_y + 13}" '
            f'x2="{body_x + body_size - 31}" y2="{rail_y + 13}" '
            'stroke="#aab0b4" stroke-width="2" opacity="0.8"/>'
        )

    pair_x1, pair_x2 = body_x + 43, body_x + body_size - 43
    pair_y1, pair_y2 = body_y + 4, body_y + body_size - 4
    terminals = (
        (pair_x1, pair_y1),
        (pair_x2, pair_y1),
        (pair_x1, pair_y2),
        (pair_x2, pair_y2),
    )
    for pin_x, pin_y in terminals:
        d.add(
            f'<rect x="{pin_x - 9}" y="{pin_y - 13}" width="18" height="26" rx="3" '
            f'fill="{LEAD_FILL}" stroke="{LEAD_EDGE}" stroke-width="2"/>'
        )

    pair_colour = "#a62919"
    for pair_y in (pair_y1, pair_y2):
        d.add(
            f'<path d="M {pair_x1} {pair_y} L {pair_x2} {pair_y}" '
            f'fill="none" stroke="{pair_colour}" stroke-width="3" opacity="0.75"/>'
        )
        d.add(
            f'<rect x="{body_x + 15}" y="{pair_y - 18}" width="{body_size - 30}" height="36" '
            f'rx="16" fill="none" stroke="{pair_colour}" stroke-width="3"/>'
        )

    d.text(520, body_y + 12, "Connected", size=17, fill=pair_colour, anchor="start", weight="bold")
    d.add(
        f'<path d="M 510 {body_y + 17} L {pair_x2 + 8} {pair_y1}" '
        f'fill="none" stroke="{pair_colour}" stroke-width="2.5"/>'
    )
    d.text(
        520,
        body_y + body_size - 24,
        "Connected",
        size=17,
        fill=pair_colour,
        anchor="start",
        weight="bold",
    )
    d.add(
        f'<path d="M 510 {body_y + body_size - 30} L {pair_x2 + 8} {pair_y2}" '
        f'fill="none" stroke="{pair_colour}" stroke-width="2.5"/>'
    )
    d.text(
        400,
        330,
        "Plastic rails mark each connected pair.",
        size=14,
        fill=LABEL_GREY,
        weight="bold",
    )
    d.text(
        400,
        352,
        "Pressing the button joins the two pairs.",
        size=14,
        fill=LABEL_GREY,
        weight="bold",
    )
    return d


def diagram_led_polarity() -> Drawing:
    d = Drawing(margin_top=0, margin_bottom=0, canvas_width=1280, canvas_height=400)
    d.add('<rect x="0" y="0" width="1280" height="400" fill="#ffffff"/>')
    d.define(
        '<marker id="polarity-arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" '
        'orient="auto"><path d="M 0 0 L 8 4 L 0 8 Z" fill="#56616a"/></marker>'
    )

    d.text(260, 45, "Side view", size=25, fill=LABEL_GREY, weight="bold")
    d.text(630, 45, "Top view", size=25, fill=LABEL_GREY, weight="bold")

    base_y = 222
    d.add(
        f'<path d="M 205 {base_y} L 205 172 C 205 126, 235 102, 260 102 '
        f'C 285 102, 315 126, 315 172 L 315 {base_y} Z" fill="#d9574f" '
        'stroke="#702d2a" stroke-width="3"/>'
    )
    d.add(
        f'<rect x="198" y="{base_y}" width="124" height="17" rx="2" '
        'fill="#b74138" stroke="#702d2a" stroke-width="2.5"/>'
    )
    d.add(
        f'<line x1="238" y1="{base_y + 17}" x2="238" y2="318" '
        f'stroke="{LEAD_EDGE}" stroke-width="7" stroke-linecap="round"/>'
    )
    d.add(
        f'<line x1="282" y1="{base_y + 17}" x2="282" y2="284" '
        f'stroke="{LEAD_EDGE}" stroke-width="7" stroke-linecap="round"/>'
    )
    d.add('<ellipse cx="238" cy="139" rx="16" ry="10" fill="#ffffff" opacity="0.34"/>')
    d.text(150, 352, "Anode (+)", size=26, fill=RAIL_RED, weight="bold")
    d.text(150, 380, "longer lead", size=24, fill=LABEL_GREY)
    d.text(370, 352, "Cathode (--)", size=26, fill=RAIL_BLUE, weight="bold")
    d.text(370, 380, "shorter lead", size=24, fill=LABEL_GREY)
    d.add(
        f'<path d="M 208 334 L 238 309" fill="none" stroke="{RAIL_RED}" stroke-width="2.5" '
        'marker-end="url(#polarity-arrow)"/>'
    )
    d.add(
        f'<path d="M 351 334 L 282 279" fill="none" stroke="{RAIL_BLUE}" stroke-width="2.5" '
        'marker-end="url(#polarity-arrow)"/>'
    )

    top_x, top_y = 630, 205
    d.add(
        f'<path d="M {top_x + 80} {top_y - 40} A 90 90 0 1 0 {top_x + 80} {top_y + 40} '
        f'L {top_x + 80} {top_y - 40} Z" fill="#b74138" stroke="#702d2a" stroke-width="3"/>'
    )
    d.add(
        f'<circle cx="{top_x}" cy="{top_y}" r="70" fill="#d9574f" '
        'stroke="#702d2a" stroke-width="2"/>'
    )
    d.add(
        f'<line x1="{top_x + 80}" y1="{top_y - 40}" x2="{top_x + 80}" y2="{top_y + 40}" '
        f'stroke="{RAIL_BLUE}" stroke-width="4"/>'
    )
    d.add(
        f'<path d="M 810 190 L {top_x + 82} 200" fill="none" stroke="{RAIL_BLUE}" stroke-width="2.5" '
        'marker-end="url(#polarity-arrow)"/>'
    )
    d.text(820, 185, "Flat on base lip", size=26, fill=RAIL_BLUE, anchor="start", weight="bold")
    d.text(820, 220, "marks the cathode", size=24, fill=LABEL_GREY, anchor="start")
    return d


IDENTIFICATION_BANDS = {
    "220": ("#ff0000", "#ff0000", "#000000", "#000000", "#7b4a12"),
    "10k": ("#7b4a12", "#000000", "#000000", "#ff0000", "#7b4a12"),
}


def diagram_resistor_identification(value: str) -> Drawing:
    d = Drawing(
        margin_top=0,
        margin_bottom=0,
        canvas_width=560,
        canvas_height=140,
        canvas_margin_left=0,
    )
    band_colours = IDENTIFICATION_BANDS[value]
    d.add('<rect x="0" y="0" width="560" height="140" fill="#ffffff"/>')
    d.add(
        '<line x1="20" y1="70" x2="540" y2="70" stroke="#555b60" '
        'stroke-width="5" stroke-linecap="round"/>'
    )
    d.add(
        '<rect x="136" y="29" width="288" height="82" fill="#9fc2e8" '
        'stroke="#454c52" stroke-width="2.5"/>'
    )
    d.add(
        '<rect x="102" y="23" width="70" height="94" rx="19" fill="#9fc2e8" '
        'stroke="#454c52" stroke-width="2.5"/>'
    )
    d.add(
        '<rect x="388" y="23" width="70" height="94" rx="19" fill="#9fc2e8" '
        'stroke="#454c52" stroke-width="2.5"/>'
    )
    bands = (
        (123, 24, 25, 92),
        (201, 29, 26, 82),
        (266, 29, 26, 82),
        (331, 29, 26, 82),
        (411, 24, 25, 92),
    )
    for (x, y, width, height), colour in zip(bands, band_colours):
        d.add(f'<rect x="{x}" y="{y}" width="{width}" height="{height}" fill="{colour}"/>')
    return d


def diagram_voltage_divider() -> Drawing:
    d = Drawing(margin_top=0, margin_bottom=0, canvas_width=1400, canvas_height=340)
    d.add('<rect x="0" y="0" width="1400" height="340" fill="#ffffff"/>')
    d.define(
        '<marker id="flow-arrow" markerWidth="9" markerHeight="9" refX="7" refY="4.5" '
        'orient="auto"><path d="M 0 0 L 9 4.5 L 0 9 Z" fill="#343a40"/></marker>'
    )

    nodes = (
        (105, "3.3 V", "#f6e5e2", "rounded"),
        (355, "LDR", "#f8efc9", "square"),
        (635, "Column 10", "#dce9f4", "circle"),
        (915, "10 kΩ", "#e2edf6", "square"),
        (1185, "GND", "#e7e7e7", "rounded"),
    )
    node_y = 125
    node_w, node_h = 150, 74
    for cx, label, fill, shape in nodes:
        if shape == "circle":
            d.add(
                f'<circle cx="{cx}" cy="{node_y + node_h / 2}" r="52" fill="{fill}" '
                'stroke="#88939b" stroke-width="2"/>'
            )
        else:
            radius = 14 if shape == "rounded" else 5
            d.add(
                f'<rect x="{cx - node_w / 2}" y="{node_y}" width="{node_w}" height="{node_h}" '
                f'rx="{radius}" fill="{fill}" stroke="#88939b" stroke-width="2"/>'
            )
        d.text(cx, node_y + 45, label, size=19, fill="#24282b", weight="bold")

    for start, end in ((180, 280), (430, 560), (710, 840), (990, 1110)):
        d.add(
            f'<line x1="{start}" y1="162" x2="{end}" y2="162" stroke="#343a40" '
            'stroke-width="3" marker-end="url(#flow-arrow)"/>'
        )
    d.add(
        '<line x1="635" y1="230" x2="635" y2="272" stroke="#2563b8" stroke-width="3" '
        'marker-end="url(#flow-arrow)"/>'
    )
    d.text(635, 305, "GPIO 2 (ADC input)", size=16, fill="#2563b8", weight="bold")
    return d


def diagram_jumper_illustration() -> Drawing:
    canvas_width, canvas_height = 1500, 650
    d = Drawing(
        margin_top=0,
        margin_bottom=0,
        canvas_width=canvas_width,
        canvas_height=canvas_height,
        canvas_margin_left=0,
    )
    d.add(
        f'<rect x="0" y="0" width="{canvas_width}" height="{canvas_height}" fill="#ffffff"/>'
    )

    wire_colours = (
        WIRE_RED,
        WIRE_BLACK,
        WIRE_WHITE,
        WIRE_ORANGE,
        WIRE_YELLOW,
        WIRE_GREEN,
        WIRE_BLUE,
        WIRE_PURPLE,
    )
    for index, colour in enumerate(wire_colours):
        y = 82 + index * 68
        left_connector_x = 112 + (index % 2) * 8
        right_connector_x = 1320 - (index % 2) * 8
        bend = (index - (len(wire_colours) - 1) / 2) * 5
        wire_path = (
            f'M {left_connector_x + 58} {y} '
            f'C 440 {y + bend}, 1030 {y - bend}, {right_connector_x} {y}'
        )

        d.add(
            f'<path d="{wire_path}" fill="none" stroke="#000000" stroke-width="17" '
            'stroke-linecap="round" opacity="0.12" transform="translate(0,4)"/>'
        )
        casing_width = 17 if colour == WIRE_WHITE else 14
        insulation_width = 10 if colour == WIRE_WHITE else 10
        d.add(
            f'<path d="{wire_path}" fill="none" stroke="#25292d" stroke-width="{casing_width}" '
            'stroke-linecap="round"/>'
        )
        d.add(
            f'<path d="{wire_path}" fill="none" stroke="{colour}" stroke-width="{insulation_width}" '
            'stroke-linecap="round"/>'
        )

        for connector_x in (left_connector_x, right_connector_x):
            d.add(
                f'<rect x="{connector_x}" y="{y - 16}" width="62" height="32" rx="10" '
                'fill="#202428" stroke="#0d0f11" stroke-width="2"/>'
            )
            d.add(
                f'<path d="M {connector_x + 9} {y - 8} H {connector_x + 46}" '
                'fill="none" stroke="#545b61" stroke-width="3" stroke-linecap="round" '
                'opacity="0.75"/>'
            )

        left_pin_x = left_connector_x
        right_pin_x = right_connector_x + 62
        for pin_x, direction in ((left_pin_x, -1), (right_pin_x, 1)):
            tip_x = pin_x + direction * 42
            d.add(
                f'<path d="M {pin_x} {y - 4} L {tip_x} {y - 3} L {tip_x} {y + 3} '
                f'L {pin_x} {y + 4} Z" fill="#b7bec3" stroke="#727a80" stroke-width="1.5"/>'
            )
            d.add(
                f'<line x1="{pin_x}" y1="{y - 2}" x2="{tip_x}" y2="{y - 2}" '
                'stroke="#eef1f2" stroke-width="1.2"/>'
            )

    return d


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


def _pixel_module_jumpers(d: Drawing, power_colour: str) -> None:
    """The three wires from the module's holes back to the seated module's pins."""
    draw_jumper(
        d,
        named_hole("jumper.pixels-power.start"),
        named_hole("jumper.pixels-power.end"),
        power_colour,
        (row_x(7), -20),
    )
    draw_jumper(
        d,
        named_hole("jumper.pixels-data.start"),
        named_hole("jumper.pixels-data.end"),
        WIRE_GREEN,
        (row_x(10), 30),
    )
    draw_jumper(
        d,
        named_hole("jumper.pixels-ground.start"),
        named_hole("jumper.pixels-ground.end"),
        WIRE_BLACK,
        (row_x(8), -60),
    )


def diagram_project_2_wiring() -> Drawing:
    d = Drawing(MARGIN_T_MODULE)
    draw_breadboard(d)
    draw_xiao(d)
    draw_tactile_button(d, named_hole("button.side-a"), named_hole("button.side-b"))

    _pixel_module_jumpers(d, WIRE_ORANGE)
    draw_jumper(
        d,
        named_hole("jumper.button-signal.start"),
        named_hole("jumper.button-signal.end"),
        WIRE_YELLOW,
        # Runs flat below the module before rising, since the module's lower edge
        # sits just above this wire's starting hole.
        (row_x(11), COLUMN_Y["C"]),
    )
    draw_jumper(
        d,
        named_hole("jumper.button-ground.start"),
        named_hole("jumper.button-ground.end"),
        WIRE_BLACK,
        # Ground is only available in the top half, so this arcs over the module
        # and comes down onto the button's free hole from above.
        (row_x(12), -85),
    )
    draw_pixel_module(d)

    ground = named_hole("jumper.pixels-ground.start")
    button_ground = named_hole("jumper.button-ground.start")
    power = named_hole("jumper.pixels-power.start")
    data = named_hole("jumper.pixels-data.start")
    callout(d, button_ground, f"GND \u00b7 {button_ground}", (26, -30), colour=WIRE_BLACK, anchor="start")
    callout(d, ground, f"GND \u00b7 {ground}", (150, -30), colour=WIRE_BLACK, anchor="start")
    callout(d, power, f"3.3 V \u00b7 {power}", (290, -30), colour="#c25c00", anchor="start")
    callout(d, data, f"GPIO 20 \u00b7 {data}", (455, -30), colour=WIRE_GREEN, anchor="start")

    signal_start = named_hole("jumper.button-signal.start")
    callout(
        d,
        signal_start,
        f"GPIO 21 \u00b7 {signal_start}",
        (26, LABEL_Y_BOTTOM),
        colour=ACCENT_DARK_YELLOW,
        anchor="start",
    )
    return d


def diagram_project_3_wiring() -> Drawing:
    d = Drawing(MARGIN_T, MARGIN_B_ENCODER)
    draw_breadboard(d)
    draw_xiao(d)

    # Every one of these signals lives on the 5V side of the module, so all
    # seven wires start in the top half and cross the channel. Each arcs over
    # the module's corner on its way, and CLK spans the widest gap so it rides
    # highest over the wires it has to cross.
    jumpers = (
        ("encoder-clk", WIRE_YELLOW, (row_x(10), -98)),
        ("encoder-dt", WIRE_GREEN, (row_x(10), -22)),
        ("encoder-sw", WIRE_PURPLE, (row_x(10), 2)),
        ("encoder-power", WIRE_ORANGE, (row_x(9), -54)),
        ("encoder-ground", WIRE_BLACK, (row_x(8), -78)),
        ("speaker-signal", WIRE_RED, (row_x(16), 30)),
        ("speaker-ground", WIRE_BLACK, (row_x(15), -100)),
    )
    for name, colour, control in jumpers:
        draw_jumper(
            d,
            named_hole(f"jumper.{name}.start"),
            named_hole(f"jumper.{name}.end"),
            colour,
            control,
        )

    draw_rotary_encoder(d)
    draw_speaker(d)

    # Every wire starts in column I, so the callouts go behind the board, well
    # clear of the modules drawn in front of it. Ordering the labels by the hole
    # they point at keeps their leaders from crossing.
    callouts = (
        ("jumper.encoder-ground.start", "GND", WIRE_BLACK, 26),
        ("jumper.encoder-power.start", "3.3 V", "#c25c00", 144),
        ("jumper.encoder-clk.start", "GPIO 10", WIRE_YELLOW, 262),
        ("jumper.encoder-dt.start", "GPIO 9", WIRE_GREEN, 400),
        ("jumper.encoder-sw.start", "GPIO 8", WIRE_PURPLE, 518),
        ("jumper.speaker-signal.start", "GPIO 20", WIRE_RED, 636),
    )
    for name, label, colour, label_x in callouts:
        coordinate = named_hole(name)
        callout(
            d,
            coordinate,
            f"{label} \u00b7 {coordinate}",
            (label_x, LABEL_Y_TOP),
            colour=colour,
            anchor="start",
        )
    return d


def diagram_project_4_wiring() -> Drawing:
    d = Drawing(MARGIN_T_MODULE, MARGIN_B_ENCODER)
    draw_breadboard(d)
    draw_xiao(d)

    draw_tactile_button(
        d,
        named_hole("pad0.side-a"),
        named_hole("pad0.side-b"),
        "Pad 0",
        label_offset_x=0,
        anchor="middle",
    )
    draw_tactile_button(
        d,
        named_hole("pad1.side-a"),
        named_hole("pad1.side-b"),
        "Pad 1",
        label_offset_x=0,
        anchor="middle",
    )
    draw_tactile_button(
        d,
        named_hole("pad2.side-a"),
        named_hole("pad2.side-b"),
        "Pad 2",
        label_offset_x=0,
        anchor="middle",
    )

    # MCU to Ground rail
    draw_jumper(
        d,
        named_hole("jumper.mcu-ground.start"),
        named_hole("jumper.mcu-ground.end"),
        WIRE_BLACK,
        (row_x(2), 75),
    )

    # Pad signal jumpers
    draw_jumper(
        d,
        named_hole("jumper.pad0-signal.start"),
        named_hole("jumper.pad0-signal.end"),
        WIRE_YELLOW,
        (row_x(8), COLUMN_Y["C"] + 15),
    )
    draw_jumper(
        d,
        named_hole("jumper.pad1-signal.start"),
        named_hole("jumper.pad1-signal.end"),
        WIRE_GREEN,
        (row_x(10), COLUMN_Y["B"] + 15),
    )
    draw_jumper(
        d,
        named_hole("jumper.pad2-signal.start"),
        named_hole("jumper.pad2-signal.end"),
        WIRE_PURPLE,
        (row_x(12), COLUMN_Y["A"] + 15),
    )

    # Ground jumpers from top ground rail to each pad
    draw_jumper(
        d,
        named_hole("jumper.pad0-ground.start"),
        named_hole("jumper.pad0-ground.end"),
        WIRE_BLACK,
        (row_x(12), 160),
    )
    draw_jumper(
        d,
        named_hole("jumper.pad1-ground.start"),
        named_hole("jumper.pad1-ground.end"),
        WIRE_WHITE,
        (row_x(16), 160),
    )
    draw_jumper(
        d,
        named_hole("jumper.pad2-ground.start"),
        named_hole("jumper.pad2-ground.end"),
        WIRE_PURPLE,
        (row_x(20), 160),
    )

    # WS2812B Power and Data
    draw_jumper(
        d,
        named_hole("jumper.pixels-power.start"),
        named_hole("jumper.pixels-power.end"),
        WIRE_ORANGE,
        (row_x(12), -20),
    )
    draw_jumper(
        d,
        named_hole("jumper.pixels-data.start"),
        named_hole("jumper.pixels-data.end"),
        WIRE_GREEN,
        (row_x(15), 10),
    )
    draw_jumper(
        d,
        named_hole("jumper.pixels-ground.start"),
        named_hole("jumper.pixels-ground.end"),
        WIRE_BLUE,
        (row_x(24), 75),
    )

    # Speaker signal jumper
    draw_jumper(
        d,
        named_hole("jumper.speaker-signal.start"),
        named_hole("jumper.speaker-signal.end"),
        WIRE_RED,
        (row_x(15), -50),
    )

    # Speaker ground jumper from top rail
    draw_jumper(
        d,
        named_hole("jumper.speaker-ground.start"),
        named_hole("jumper.speaker-ground.end"),
        WIRE_WHITE,
        (row_x(28), 160),
    )

    draw_pixel_module(d, pcb_row=18)
    draw_speaker(d)

    # Callouts
    mcu_gnd = named_hole("jumper.mcu-ground.start")
    power = named_hole("jumper.pixels-power.start")
    speaker_sig = named_hole("jumper.speaker-signal.start")
    data = named_hole("jumper.pixels-data.start")

    callout(d, mcu_gnd, f"GND \u00b7 {mcu_gnd}", (26, -30), colour=WIRE_BLACK, anchor="start")
    callout(d, power, f"3.3 V \u00b7 {power}", (160, -30), colour="#c25c00", anchor="start")
    callout(d, speaker_sig, f"GPIO 10 \u00b7 {speaker_sig}", (310, -30), colour=WIRE_RED, anchor="start")
    callout(d, data, f"GPIO 20 \u00b7 {data}", (470, -30), colour=WIRE_GREEN, anchor="start")

    pad2_sig = named_hole("jumper.pad2-signal.start")
    pad1_sig = named_hole("jumper.pad1-signal.start")
    pad0_sig = named_hole("jumper.pad0-signal.start")

    callout(d, pad2_sig, f"GPIO 6 \u00b7 {pad2_sig}", (26, LABEL_Y_BOTTOM), colour=WIRE_PURPLE, anchor="start")
    callout(d, pad1_sig, f"GPIO 7 \u00b7 {pad1_sig}", (160, LABEL_Y_BOTTOM), colour=WIRE_GREEN, anchor="start")
    callout(d, pad0_sig, f"GPIO 21 \u00b7 {pad0_sig}", (294, LABEL_Y_BOTTOM), colour=ACCENT_DARK_YELLOW, anchor="start")

    return d


def diagram_project_5_wiring() -> Drawing:
    d = Drawing(MARGIN_T_MODULE)
    draw_breadboard(d)
    draw_xiao(d)
    _pixel_module_jumpers(d, WIRE_RED)
    draw_pixel_module(d)

    power = named_hole("jumper.pixels-power.start")
    ground = named_hole("jumper.pixels-ground.start")
    data = named_hole("jumper.pixels-data.start")
    callout(d, power, f"5 V \u00b7 {power}", (26, -30), colour=WIRE_RED, anchor="start")
    callout(d, ground, f"GND \u00b7 {ground}", (150, -30), colour=WIRE_BLACK, anchor="start")
    callout(d, data, f"GPIO 20 \u00b7 {data}", (265, -30), colour=WIRE_GREEN, anchor="start")
    return d


def diagram_project_6_wiring() -> Drawing:
    d = Drawing(70, 120)
    draw_breadboard(d)
    draw_xiao(d)

    draw_mpu6050(d)
    draw_oled_module(d)

    # MPU-6050 Jumpers (connecting to row E holes 16-19: SDA, SCL, GND, VCC)
    draw_jumper(
        d,
        named_hole("jumper.mpu-ground.start"),
        named_hole("jumper.mpu-ground.end"),
        WIRE_BLACK,
        (row_x(10), -70),
    )
    draw_jumper(
        d,
        named_hole("jumper.mpu-power.start"),
        named_hole("jumper.mpu-power.end"),
        WIRE_ORANGE,
        (row_x(11), -45),
    )
    draw_jumper(
        d,
        named_hole("jumper.mpu-sda.start"),
        named_hole("jumper.mpu-sda.end"),
        WIRE_GREEN,
        (row_x(10), COLUMN_Y["B"] + 15),
    )
    draw_jumper(
        d,
        named_hole("jumper.mpu-scl.start"),
        named_hole("jumper.mpu-scl.end"),
        WIRE_YELLOW,
        (row_x(11), COLUMN_Y["C"] + 15),
    )

    # OLED Jumpers daisy-chained from MPU rows 18, 19, 17, and 16.
    draw_jumper(
        d,
        named_hole("jumper.oled-ground.start"),
        named_hole("jumper.oled-ground.end"),
        WIRE_BLACK,
        (row_x(22), COLUMN_Y["D"] - 2),
    )
    draw_jumper(
        d,
        named_hole("jumper.oled-power.start"),
        named_hole("jumper.oled-power.end"),
        WIRE_ORANGE,
        (row_x(23), COLUMN_Y["C"] + 4),
    )
    draw_jumper(
        d,
        named_hole("jumper.oled-scl.start"),
        named_hole("jumper.oled-scl.end"),
        WIRE_YELLOW,
        (row_x(22), COLUMN_Y["C"] + 10),
    )
    draw_jumper(
        d,
        named_hole("jumper.oled-sda.start"),
        named_hole("jumper.oled-sda.end"),
        WIRE_GREEN,
        (row_x(22), COLUMN_Y["B"] + 10),
    )

    # Callouts
    mpu_gnd = named_hole("jumper.mpu-ground.start")
    mpu_pwr = named_hole("jumper.mpu-power.start")
    callout(d, mpu_gnd, f"GND \u00b7 {mpu_gnd}", (26, -30), colour=WIRE_BLACK, anchor="start")
    callout(d, mpu_pwr, f"3.3 V \u00b7 {mpu_pwr}", (160, -30), colour="#c25c00", anchor="start")

    mpu_sda = named_hole("jumper.mpu-sda.start")
    mpu_scl = named_hole("jumper.mpu-scl.start")

    callout_y = LABEL_Y_BOTTOM + 45
    callout(d, mpu_sda, f"GPIO 6 (SDA) \u00b7 {mpu_sda}", (26, callout_y), colour=WIRE_GREEN, anchor="start")
    callout(d, mpu_scl, f"GPIO 7 (SCL) \u00b7 {mpu_scl}", (200, callout_y), colour=WIRE_YELLOW, anchor="start")

    return d


def diagram_project_7_wiring() -> Drawing:
    d = Drawing(MARGIN_T, MARGIN_B_ENCODER)
    draw_breadboard(d)
    draw_xiao(d)

    # The encoder's five signals all live on the 5V side of the module, so they
    # arc over its corner exactly as they do in Project 3. Ground is the one
    # supply three peripherals need at once, so it is fed to the top rail first
    # and tapped from there, which leaves the row-2 node free of a wire pile-up.
    jumpers = (
        ("mcu-ground", WIRE_BLACK, (row_x(2), 75)),
        ("encoder-ground", WIRE_BLUE, (row_x(10), 160)),
        ("encoder-power", WIRE_ORANGE, (row_x(7), -54)),
        ("encoder-sw", WIRE_PURPLE, (row_x(9), 2)),
        ("encoder-dt", WIRE_GREEN, (row_x(9), -22)),
        ("encoder-clk", WIRE_YELLOW, (row_x(9), -98)),
        ("oled-ground", WIRE_BLUE, (row_x(19), 160)),
        ("oled-power", WIRE_ORANGE, (row_x(12), -45)),
        ("oled-scl", WIRE_YELLOW, (row_x(14), COLUMN_Y["C"] + 15)),
        ("oled-sda", WIRE_GREEN, (row_x(13), COLUMN_Y["B"] + 15)),
        ("speaker-signal", WIRE_RED, (row_x(20), 20)),
        ("speaker-ground", WIRE_BLACK, (row_x(28), 160)),
    )
    for name, colour, control in jumpers:
        draw_jumper(
            d,
            named_hole(f"jumper.{name}.start"),
            named_hole(f"jumper.{name}.end"),
            colour,
            control,
        )

    draw_rotary_encoder(d)
    draw_speaker(d)
    draw_oled_module(d, _oled_screen_clock)

    # The supply and encoder wires all start behind the board, so their callouts
    # go above it, ordered by the hole they point at so the leaders do not cross.
    callouts = (
        ("jumper.mcu-ground.start", "GND", WIRE_BLACK, 26),
        ("jumper.encoder-power.start", "3.3 V", "#c25c00", 150),
        ("jumper.encoder-clk.start", "GPIO 10", WIRE_YELLOW, 274),
        ("jumper.encoder-dt.start", "GPIO 9", WIRE_GREEN, 412),
        ("jumper.encoder-sw.start", "GPIO 8", WIRE_PURPLE, 530),
        ("jumper.speaker-signal.start", "GPIO 20", WIRE_RED, 648),
    )
    for name, label, colour, label_x in callouts:
        coordinate = named_hole(name)
        callout(
            d,
            coordinate,
            f"{label} \u00b7 {coordinate}",
            (label_x, LABEL_Y_TOP),
            colour=colour,
            anchor="start",
        )

    # The I2C pair starts in front of the board, in among the modules, so those
    # two labels sit below it instead.
    oled_scl = named_hole("jumper.oled-scl.start")
    oled_sda = named_hole("jumper.oled-sda.start")
    callout(d, oled_sda, f"GPIO 6 (SDA) \u00b7 {oled_sda}", (26, LABEL_Y_BOTTOM), colour=WIRE_GREEN, anchor="start")
    callout(d, oled_scl, f"GPIO 7 (SCL) \u00b7 {oled_scl}", (240, LABEL_Y_BOTTOM), colour=WIRE_YELLOW, anchor="start")

    return d


@dataclass(frozen=True)
class ProjectDiagrams:
    """One project's TeX source and the diagrams drawn from its coordinates."""

    source: Path
    coordinates: frozenset[str]
    outputs: dict[str, Callable[[], Drawing]]


PROJECTS = {
    1: ProjectDiagrams(
        source=PROJECT_GUIDE_DIR / "projects" / "project_1.tex",
        coordinates=PROJECT_1_COORDINATES,
        outputs={
            "electronics_preamble/breadboard_illustration.png": diagram_breadboard_overview,
            "electronics_preamble/button_illustration.png": diagram_button_illustration,
            "electronics_preamble/led_polarity.png": diagram_led_polarity,
            "electronics_preamble/resistor_220.png": lambda: diagram_resistor_identification("220"),
            "electronics_preamble/resistor_10k.png": lambda: diagram_resistor_identification("10k"),
            "electronics_preamble/jumper_illustration.png": diagram_jumper_illustration,
            "common/microcontroller_seated_in_breadboard.png": diagram_microcontroller_seated,
            "project_1/led_placed.png": diagram_led_placed,
            "project_1/components_placed.png": diagram_components_placed,
            "project_1/wired_up.png": diagram_wired_up,
            "project_1/voltage_divider.png": diagram_voltage_divider,
        },
    ),
    2: ProjectDiagrams(
        source=PROJECT_GUIDE_DIR / "projects" / "project_2.tex",
        coordinates=PIXEL_MODULE_COORDINATES | BUTTON_COORDINATES,
        outputs={"project_2/wiring.png": diagram_project_2_wiring},
    ),
    3: ProjectDiagrams(
        source=PROJECT_GUIDE_DIR / "projects" / "project_3.tex",
        coordinates=ENCODER_COORDINATES | SPEAKER_COORDINATES,
        outputs={"project_3/wiring.png": diagram_project_3_wiring},
    ),
    4: ProjectDiagrams(
        source=PROJECT_GUIDE_DIR / "projects" / "project_4.tex",
        coordinates=PROJECT_4_COORDINATES,
        outputs={"project_4/wiring.png": diagram_project_4_wiring},
    ),
    5: ProjectDiagrams(
        source=PROJECT_GUIDE_DIR / "projects" / "project_5.tex",
        coordinates=PIXEL_MODULE_COORDINATES,
        outputs={"project_5/wiring.png": diagram_project_5_wiring},
    ),
    6: ProjectDiagrams(
        source=PROJECT_GUIDE_DIR / "projects" / "project_6.tex",
        coordinates=PROJECT_6_COORDINATES,
        outputs={"project_6/wiring.png": diagram_project_6_wiring},
    ),
    7: ProjectDiagrams(
        source=PROJECT_GUIDE_DIR / "projects" / "project_7.tex",
        coordinates=PROJECT_7_COORDINATES,
        outputs={"project_7/wiring.png": diagram_project_7_wiring},
    ),
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--width", type=int, default=2200, help="output PNG width in pixels")
    parser.add_argument(
        "--project",
        choices=("all", *(str(number) for number in PROJECTS)),
        default="all",
        help="which project's diagrams to render",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=PROJECT_GUIDE_DIR / "images",
        help="directory to write PNGs into",
    )
    args = parser.parse_args()

    rsvg = shutil.which("rsvg-convert")
    if rsvg is None:
        print("rsvg-convert not found; install it with: brew install librsvg", file=sys.stderr)
        return 1

    selected = (
        list(PROJECTS.values())
        if args.project == "all"
        else [PROJECTS[int(args.project)]]
    )

    global COORDINATES
    for project in selected:
        try:
            COORDINATES = read_coordinates(
                [COMMON_SOURCE, project.source],
                SEATING_COORDINATES | project.coordinates,
            )
        except (OSError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

        print(f"read {len(COORDINATES)} coordinates for {project.source.name}")
        for name, builder in project.outputs.items():
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
