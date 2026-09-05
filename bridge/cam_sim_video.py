#!/usr/bin/env python3
"""Turn GRBL G-code into an MP4 of a 2.5D material-removal simulation.

Watch the block being machined before any chip is actually cut: the stock is a
top-view heightmap (float32, one cell per ``--resolution`` mm), every cutting
move stamps the tool's own bottom profile into it, and one frame is emitted per
N seconds of machine time.

    python3 bridge/cam_sim_video.py --stock 100x100x10 --origin center \\
      --tool endmill:3.0 gcode/10/sheet2-10-op1-T1-endmill30.gcode \\
      --tool vbit:30:0.1 gcode/10/sheet2-10-op2_4-T2-vbit30.gcode \\
      --out sim/sheet2-10.mp4

A ``--tool`` applies to every file that follows it, and the files are simulated
in order on the SAME stock, so a two-tool job shows up as one continuous run.

This is an OPTIONAL extra of the repo: it needs numpy, Pillow and the ffmpeg
binary. The MCP server and the FreeCAD add-on stay standard library only; this
script fails with a clear message when a dependency is missing.

The G-code dialect is the one ``cam_gcode_check`` already parses (modal G0-G3,
G17 only, G20/G21, G90/G91, ``(...)``/``;`` comments, ``( M6 Tn )`` tool-change
comments), so what you see here is the same file the checker validated.
"""

import argparse
import glob
import json
import math
import os
import re
import subprocess
import sys
from collections import namedtuple

MISSING_DEPS = []
try:
    import numpy as np
except ImportError:  # pragma: no cover - exercised only on a bare machine
    np = None
    MISSING_DEPS.append("numpy")
try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # pragma: no cover - exercised only on a bare machine
    Image = ImageDraw = ImageFont = None
    MISSING_DEPS.append("Pillow")

# Same lexer as freecad/AiBridge/tools/cam.py: one letter plus a signed number.
_WORD = re.compile(r"([A-Za-z])\s*([-+]?[0-9]*\.?[0-9]+)")
_RAPIDS = {"G0", "G00"}
_LINEAR = {"G1", "G01"}
_ARCS_CW = {"G2", "G02"}
_ARCS_CCW = {"G3", "G03"}
_FEEDS = _LINEAR | _ARCS_CW | _ARCS_CCW
_COMMENT_TOOL = re.compile(r"\bT\s*(\d+)\b")

# Codes the simulator understands well enough to ignore without complaining:
# plane/units/distance modes are handled, the rest do not move the tool.
_SILENT_CODES = {
    "G17", "G20", "G21", "G90", "G91", "G90.1", "G91.1", "G93", "G94",
    "G40", "G43", "G49", "G53", "G54", "G55", "G56", "G57", "G58", "G59",
    "G61", "G64", "G80", "G4", "G04",
    "M0", "M00", "M1", "M01", "M2", "M02", "M3", "M03", "M4", "M04",
    "M5", "M05", "M6", "M06", "M7", "M07", "M8", "M08", "M9", "M09",
    "M30", "M99",
}

MAX_CHORD_MM = 0.2  # arc tessellation
MAX_STEP_MM = 0.3  # cutting-move sampling
HOLD_SECONDS = 1.5  # freeze at the end of each file

Move = namedtuple("Move", "kind start end feed line")


def rapid_below_zero(move):
    """A G0 with the tool tip under the stock top — a crash candidate.

    Whether it is a real crash is decided by the heightmap: retracting out of a
    cut and the rapid re-entry of a GRBL peck cycle both run below Z0 through
    material that is already gone. Only a rapid that actually *removes* stock is
    counted, which is what ``Simulation`` measures.
    """
    return move.kind == "rapid" and min(move.start[2], move.end[2]) < 0


def die(message):
    """Fail with a clear message rather than a traceback."""
    raise SystemExit("error: %s" % message)


def require_deps():
    """The optional extras are only needed when the script actually runs."""
    if MISSING_DEPS:
        die(
            "cam_sim_video needs %s. Install them with:\n"
            "    python3 -m pip install --user numpy Pillow\n"
            "and make sure the 'ffmpeg' binary is on PATH."
            % " and ".join(MISSING_DEPS)
        )


# --------------------------------------------------------------------------- #
# G-code parsing
# --------------------------------------------------------------------------- #


def strip_comments(line):
    """Split a line into (code, comment); ``(...)`` and ``;`` are comments."""
    code = []
    comment = []
    depth = 0
    for index, char in enumerate(line):
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        elif char == ";" and depth == 0:
            comment.append(line[index + 1:])
            break
        elif depth == 0:
            code.append(char)
        else:
            comment.append(char)
    return "".join(code), "".join(comment)


def fmt_code(value):
    """``G01`` and ``G1`` must compare equal; normalise the numeric part."""
    if "." in value:
        whole, frac = value.split(".", 1)
        whole = whole.lstrip("0") or "0"
        return "%s.%s" % (whole, frac.rstrip("0") or "0")
    return value.lstrip("0") or "0"


def arc_points(start, end, centre, clockwise, z_start, z_end):
    """Tessellate one arc into points of at most ``MAX_CHORD_MM`` chord length.

    Full circles (start == end, the I/J form) sweep a whole turn, exactly like
    ``cam_gcode_check`` measures them.
    """
    radius = math.hypot(start[0] - centre[0], start[1] - centre[1])
    start_angle = math.atan2(start[1] - centre[1], start[0] - centre[0])
    end_angle = math.atan2(end[1] - centre[1], end[0] - centre[0])
    sweep = end_angle - start_angle
    if clockwise:
        while sweep > 0:
            sweep -= 2 * math.pi
        if abs(sweep) < 1e-9:
            sweep = -2 * math.pi
    else:
        while sweep < 0:
            sweep += 2 * math.pi
        if abs(sweep) < 1e-9:
            sweep = 2 * math.pi

    length = abs(sweep) * radius
    steps = max(1, int(math.ceil(length / MAX_CHORD_MM)))
    points = []
    for index in range(1, steps + 1):
        ratio = index / float(steps)
        angle = start_angle + sweep * ratio
        points.append((
            centre[0] + radius * math.cos(angle),
            centre[1] + radius * math.sin(angle),
            z_start + (z_end - z_start) * ratio,
        ))
    # Land exactly on the commanded end point, whatever the rounding did.
    points[-1] = (end[0], end[1], z_end)
    return points, length


def arc_centre_from_radius(start, end, radius, clockwise):
    """Centre of an ``R`` arc: |R| picks the radius, its sign picks the arc."""
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    chord = math.hypot(dx, dy)
    if chord < 1e-9:
        return None  # R with coincident points is undefined; G2/G3 I/J is the full-circle form
    half = chord / 2.0
    magnitude = abs(radius)
    if magnitude < half - 1e-6:
        return None
    height = math.sqrt(max(0.0, magnitude * magnitude - half * half))
    mid = ((start[0] + end[0]) / 2.0, (start[1] + end[1]) / 2.0)
    perp = (-dy / chord, dx / chord)
    sign = 1.0 if (radius > 0) == (not clockwise) else -1.0
    return (mid[0] + perp[0] * height * sign, mid[1] + perp[1] * height * sign)


class GcodeParser:
    """Modal GRBL parser yielding straight segments in mm, in machine order."""

    def __init__(self, text, rapid_feed=2000.0):
        self.text = text
        self.rapid_feed = float(rapid_feed)
        self.warnings = []
        self._warned = set()
        self.lines = 0
        self.tools = []

    def warn(self, key, message):
        """One warning per distinct code, however many times it shows up."""
        if key in self._warned:
            return
        self._warned.add(key)
        self.warnings.append(message)

    def moves(self):
        position = [0.0, 0.0, 0.0]
        motion = None
        feed = None
        scale = 1.0  # G21 (mm); G20 switches to 25.4
        absolute = True
        commented_tools = []

        for index, raw in enumerate(self.text.splitlines(), start=1):
            self.lines = index
            line, comment = strip_comments(raw)
            for match in _COMMENT_TOOL.finditer(comment):
                value = int(match.group(1))
                if value not in commented_tools:
                    commented_tools.append(value)
            line = line.strip()
            if not line:
                continue
            words = _WORD.findall(line)
            if not words:
                continue

            codes = ["%s%s" % (letter.upper(), fmt_code(value))
                     for letter, value in words if letter.upper() in ("G", "M")]
            axes = {}
            offsets = {}
            radius = None
            for letter, value in words:
                letter = letter.upper()
                if letter in ("X", "Y", "Z"):
                    axes[letter] = float(value)
                elif letter == "F":
                    feed = float(value)
                elif letter in ("I", "J", "K"):
                    offsets[letter] = float(value)
                elif letter == "R":
                    radius = float(value)
                elif letter == "T":
                    number = int(float(value))
                    if number not in self.tools:
                        self.tools.append(number)
                elif letter in ("G", "M", "N", "S", "P", "L", "Q"):
                    # G and M are handled through `codes`; the rest do not move.
                    pass
                else:
                    self.warn("word:%s" % letter,
                              "line %d: ignored word %s" % (index, letter))

            for code in codes:
                if code in ("G20",):
                    scale = 25.4
                elif code in ("G21",):
                    scale = 1.0
                elif code in ("G90",):
                    absolute = True
                elif code in ("G91",):
                    absolute = False
                elif code in _RAPIDS or code in _FEEDS:
                    motion = code
                elif code in ("G18", "G19"):
                    self.warn(code, "line %d: %s (plane other than G17) is not "
                                    "simulated; arcs are treated as XY" % (index, code))
                elif code not in _SILENT_CODES:
                    self.warn(code, "line %d: unknown code %s ignored" % (index, code))

            if not axes:
                continue
            active = next((code for code in codes
                           if code in _RAPIDS or code in _FEEDS), motion)
            if active is None:
                self.warn("no-motion",
                          "line %d: axis words with no motion mode; treated as G1" % index)
                active = "G1"

            target = list(position)
            for axis_index, axis in enumerate("XYZ"):
                if axis in axes:
                    value = axes[axis] * scale
                    target[axis_index] = value if absolute else position[axis_index] + value

            start = tuple(position)
            end = tuple(target)
            if active in _RAPIDS:
                yield Move("rapid", start, end, self.rapid_feed, index)
            elif active in _LINEAR:
                yield Move("cut", start, end, feed, index)
            else:
                clockwise = active in _ARCS_CW
                if "I" in offsets or "J" in offsets:
                    centre = (start[0] + offsets.get("I", 0.0) * scale,
                              start[1] + offsets.get("J", 0.0) * scale)
                elif radius is not None:
                    centre = arc_centre_from_radius(start, end, radius * scale, clockwise)
                else:
                    centre = None
                if centre is None:
                    self.warn("bad-arc:%d" % index,
                              "line %d: arc without a usable centre; cut straight" % index)
                    yield Move("cut", start, end, feed, index)
                else:
                    points, _length = arc_points(start, end, centre, clockwise,
                                                 start[2], end[2])
                    previous = start
                    for point in points:
                        yield Move("cut", previous, point, feed, index)
                        previous = point
            position = target

        if not self.tools and commented_tools:
            self.tools = commented_tools


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #


class ToolKernel:
    """The tool's bottom profile as a grid of Z offsets above the tip.

    ``kernel[j, i]`` is how far above the tip the tool surface sits at that
    cell; ``+inf`` means the cell is outside the tool footprint. Removing
    material is then ``heightmap = min(heightmap, z_tip + kernel)``.

    The array is built once at the radius the tool reaches when its tip sits at
    the bottom of the stock; ``sub()`` hands out the centred sub-array that a
    given depth actually needs, so a V-bit grazing the surface stamps a 3x3
    patch instead of a 111x111 one.
    """

    def __init__(self, spec, resolution, max_depth):
        self.spec = spec
        self.resolution = float(resolution)
        self.kind, self.label, params = parse_tool_spec(spec)
        self.params = params
        max_depth = max(float(max_depth), 0.001)
        self.max_radius = max(self.footprint_radius(max_depth), self.resolution)
        self.max_cells = int(math.ceil(self.max_radius / self.resolution)) + 1

        size = 2 * self.max_cells + 1
        axis = (np.arange(size, dtype=np.float64) - self.max_cells) * self.resolution
        rho = np.hypot(axis[None, :], axis[:, None])
        self.kernel = self.profile(rho).astype(np.float32)
        self._cache = {}

    def profile(self, rho):
        """Z offset of the tool surface above the tip, ``inf`` outside it."""
        if self.kind == "endmill":
            radius = self.params["radius"]
            return np.where(rho <= radius + 1e-9, 0.0, np.inf)
        if self.kind == "ballnose":
            radius = self.params["radius"]
            inside = rho <= radius
            safe = np.minimum(rho, radius)
            profile = radius - np.sqrt(np.maximum(0.0, radius * radius - safe * safe))
            return np.where(inside, profile, np.inf)
        # V-bit: a cone on a flat tip of radius tip_r.
        tip_radius = self.params["tip_radius"]
        slope = self.params["slope"]  # dz per mm of radius beyond the tip
        return np.maximum(0.0, rho - tip_radius) * slope

    def footprint_radius(self, depth):
        """Radius of the tool at ``depth`` mm below its tip (0 above the stock)."""
        if depth <= 0:
            return 0.0
        if self.kind == "endmill":
            return self.params["radius"]
        if self.kind == "ballnose":
            radius = self.params["radius"]
            if depth >= radius:
                return radius
            return math.sqrt(max(0.0, radius * radius - (radius - depth) ** 2))
        return self.params["tip_radius"] + depth / self.params["slope"]

    def cells_for(self, depth):
        """Half-size in cells of the sub-kernel needed at ``depth``."""
        radius = self.footprint_radius(depth)
        cells = int(math.ceil(radius / self.resolution)) + 1
        return max(1, min(cells, self.max_cells))

    def sub(self, cells):
        """Centred (2c+1)^2 view of the kernel — a view, so it costs nothing."""
        patch = self._cache.get(cells)
        if patch is None:
            low = self.max_cells - cells
            high = self.max_cells + cells + 1
            patch = self.kernel[low:high, low:high]
            self._cache[cells] = patch
        return patch


def parse_tool_spec(spec):
    """``endmill:3.0`` / ``ballnose:3.0`` / ``vbit:30:0.1`` -> kind, label, params."""
    parts = str(spec).split(":")
    kind = parts[0].strip().lower()
    try:
        if kind in ("endmill", "flat", "endmill_flat"):
            diameter = float(parts[1])
            if diameter <= 0:
                raise ValueError
            return "endmill", "end mill Ø%.3g mm" % diameter, {"radius": diameter / 2.0}
        if kind in ("ballnose", "ball", "ballend"):
            diameter = float(parts[1])
            if diameter <= 0:
                raise ValueError
            return "ballnose", "ball nose Ø%.3g mm" % diameter, {"radius": diameter / 2.0}
        if kind in ("vbit", "v", "vcarve"):
            angle = float(parts[1])
            tip = float(parts[2]) if len(parts) > 2 else 0.0
            if not 0 < angle < 180:
                raise ValueError
            half = math.radians(angle / 2.0)
            return "vbit", "V-bit %.4g° (tip Ø%.3g mm)" % (angle, tip), {
                "tip_radius": tip / 2.0,
                # dz = (rho - tip_r) / tan(half_angle)
                "slope": 1.0 / math.tan(half),
            }
    except (IndexError, ValueError):
        pass
    die("bad --tool %r; expected endmill:<dia>, ballnose:<dia> or "
        "vbit:<included_angle_deg>[:<tip_dia>]" % (spec,))


# --------------------------------------------------------------------------- #
# The stock heightmap
# --------------------------------------------------------------------------- #


class Stock:
    """A float32 heightmap of the stock top, Z0 = the top face."""

    def __init__(self, width, depth, height, origin="center", resolution=0.1):
        self.width = float(width)
        self.depth = float(depth)
        self.height = float(height)
        self.origin = origin
        self.resolution = float(resolution)
        self.nx = max(1, int(round(self.width / self.resolution)))
        self.ny = max(1, int(round(self.depth / self.resolution)))
        if origin == "center":
            self.x0 = -self.width / 2.0
            self.y0 = -self.depth / 2.0
        elif origin == "corner":
            self.x0 = 0.0
            self.y0 = 0.0
        else:
            die("--origin must be center or corner, got %r" % origin)
        # Row j holds y in [y0 + j*res, y0 + (j+1)*res); rendering flips it.
        self.heightmap = np.zeros((self.ny, self.nx), dtype=np.float32)

    def cell_of(self, x, y):
        return (int(math.floor((x - self.x0) / self.resolution)),
                int(math.floor((y - self.y0) / self.resolution)))

    def stamp(self, kernel, x, y, z, detect=False):
        """Remove everything the tool occupies with its tip at (x, y, z).

        With ``detect`` it also reports whether any material was actually
        removed — the honest way to tell a crashing rapid from one travelling
        through a hole that is already there.
        """
        depth = -z
        if depth <= 0:
            return False
        cells = kernel.cells_for(depth)
        column, row = self.cell_of(x, y)
        left, right = column - cells, column + cells + 1
        bottom, top = row - cells, row + cells + 1
        clipped_left, clipped_right = max(0, left), min(self.nx, right)
        clipped_bottom, clipped_top = max(0, bottom), min(self.ny, top)
        if clipped_left >= clipped_right or clipped_bottom >= clipped_top:
            return False
        patch = kernel.sub(cells)[
            clipped_bottom - bottom:clipped_top - bottom,
            clipped_left - left:clipped_right - left,
        ]
        region = self.heightmap[clipped_bottom:clipped_top, clipped_left:clipped_right]
        surface = np.float32(z) + patch
        removed = bool((surface < region).any()) if detect else False
        np.minimum(region, surface, out=region)
        return removed


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #

TOP_COLOUR = (238, 232, 214)  # ivory: untouched POM
DEEP_COLOUR = (54, 62, 92)  # dark, slightly blue: the deepest cut
HUD_BACKGROUND = (24, 26, 31)
HUD_TEXT = (226, 230, 238)
HUD_DIM = (140, 150, 166)
TOOL_COLOUR = (222, 60, 60)
OUTLINE_COLOUR = (120, 126, 138)

FONT_CANDIDATES = (
    "DejaVuSans.ttf",
    "DejaVuSans-Bold.ttf",
    "LiberationSans-Regular.ttf",
    "NotoSans-Regular.ttf",
    "DroidSans.ttf",
)


def load_font(size):
    """DejaVu Sans when the machine has it, PIL's built-in font otherwise."""
    for name in FONT_CANDIDATES:
        for root in ("/usr/share/fonts", "/usr/local/share/fonts",
                     os.path.expanduser("~/.local/share/fonts")):
            matches = glob.glob(os.path.join(root, "**", name), recursive=True)
            for candidate in matches:
                try:
                    return ImageFont.truetype(candidate, size)
                except OSError:
                    continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


def build_colour_table():
    """256 ivory-to-deep-blue steps, indexed by normalised depth."""
    table = np.zeros((256, 3), dtype=np.float32)
    for index in range(256):
        ratio = (index / 255.0) ** 0.75
        for channel in range(3):
            table[index, channel] = (TOP_COLOUR[channel] * (1.0 - ratio)
                                     + DEEP_COLOUR[channel] * ratio)
    return table


class Renderer:
    """Heightmap -> RGB frame: depth colour, hillshade, tool marker and HUD."""

    def __init__(self, stock, deepest, hud_height=60):
        self.stock = stock
        self.deepest = max(float(deepest), 0.05)
        self.colours = build_colour_table()
        # x264 needs both dimensions even.
        self.width = stock.nx + (stock.nx % 2)
        self.hud_height = hud_height + hud_height % 2
        body = stock.ny
        total = body + self.hud_height
        if total % 2:
            self.hud_height += 1
            total += 1
        self.height = total
        self.font = load_font(18)
        self.small_font = load_font(15)
        # Light from the upper-left, in image orientation.
        light = np.array([-1.0, 1.0, 1.4])
        self.light = light / np.linalg.norm(light)

    def shade(self, heightmap):
        """Hillshade from the XY gradient so the relief actually reads."""
        gradient_y, gradient_x = np.gradient(heightmap, self.stock.resolution)
        norm = np.sqrt(gradient_x * gradient_x + gradient_y * gradient_y + 1.0)
        dot = (-gradient_x * self.light[0]
               - gradient_y * self.light[1]
               + self.light[2]) / norm
        return np.clip(0.55 + 0.55 * dot, 0.35, 1.35).astype(np.float32)

    def body(self, heightmap):
        ratio = np.clip(-heightmap / self.deepest, 0.0, 1.0)
        index = (ratio * 255.0).astype(np.uint8)
        rgb = self.colours[index]
        rgb *= self.shade(heightmap)[:, :, None]
        rgb = np.clip(rgb, 0, 255).astype(np.uint8)
        return np.flipud(rgb)  # row 0 of the image is the largest Y

    def frame(self, heightmap, hud, tool=None, banner=None):
        """One finished RGB frame. ``tool`` is (x, y, radius_mm) or None."""
        image = Image.new("RGB", (self.width, self.height), HUD_BACKGROUND)
        image.paste(Image.fromarray(self.body(heightmap), "RGB"), (0, 0))
        draw = ImageDraw.Draw(image)
        stock = self.stock

        # Stock outline and the XY zero crosshair.
        draw.rectangle([0, 0, stock.nx - 1, stock.ny - 1], outline=OUTLINE_COLOUR)
        zero = self.to_pixels(0.0, 0.0)
        if 0 <= zero[0] < stock.nx and 0 <= zero[1] < stock.ny:
            draw.line([zero[0] - 9, zero[1], zero[0] + 9, zero[1]], fill=OUTLINE_COLOUR)
            draw.line([zero[0], zero[1] - 9, zero[0], zero[1] + 9], fill=OUTLINE_COLOUR)

        if tool is not None:
            column, row = self.to_pixels(tool[0], tool[1])
            radius = max(3.0, tool[2] / stock.resolution)
            draw.ellipse([column - radius, row - radius, column + radius, row + radius],
                         outline=TOOL_COLOUR, width=2)

        self.draw_hud(draw, hud, banner)
        return image

    def to_pixels(self, x, y):
        column = int(round((x - self.stock.x0) / self.stock.resolution))
        row = self.stock.ny - 1 - int(round((y - self.stock.y0) / self.stock.resolution))
        return column, row

    def draw_hud(self, draw, hud, banner):
        top = self.height - self.hud_height
        draw.rectangle([0, top, self.width, self.height], fill=HUD_BACKGROUND)
        draw.text((10, top + 7), hud.get("line1", ""), font=self.font, fill=HUD_TEXT)
        draw.text((10, top + 32), hud.get("line2", ""), font=self.small_font, fill=HUD_DIM)
        if banner:
            draw.text((10, 10), banner, font=self.font, fill=TOOL_COLOUR)


# --------------------------------------------------------------------------- #
# Simulation
# --------------------------------------------------------------------------- #


def format_time(seconds):
    seconds = int(round(seconds))
    return "%d:%02d:%02d" % (seconds // 3600, (seconds % 3600) // 60, seconds % 60)


def scan_file(text, rapid_feed):
    """Dry pass: machine time, Z/XY extents and crash count, without stamping.

    Cheap (parsing only) and it is what lets the colour ramp normalise to the
    real deepest cut of the whole job instead of the stock height.
    """
    parser = GcodeParser(text, rapid_feed=rapid_feed)
    stats = {
        "seconds": 0.0, "cut_moves": 0, "rapid_moves": 0, "rapids_below_zero": 0,
        "z_min": None, "z_max": None,
        "x_min": None, "x_max": None, "y_min": None, "y_max": None,
        "no_feed_moves": 0,
    }
    for move in parser.moves():
        length = math.dist(move.start, move.end)
        feed = move.feed
        if move.kind == "rapid":
            stats["rapid_moves"] += 1
            if rapid_below_zero(move):
                stats["rapids_below_zero"] += 1
        else:
            stats["cut_moves"] += 1
            if not feed:
                stats["no_feed_moves"] += 1
                feed = None
        if feed:
            stats["seconds"] += length / feed * 60.0
        for point in (move.start, move.end):
            for key_low, key_high, value in (("x_min", "x_max", point[0]),
                                             ("y_min", "y_max", point[1]),
                                             ("z_min", "z_max", point[2])):
                low, high = stats[key_low], stats[key_high]
                stats[key_low] = value if low is None else min(low, value)
                stats[key_high] = value if high is None else max(high, value)
    stats["lines"] = parser.lines
    stats["tools"] = parser.tools
    stats["warnings"] = parser.warnings
    return stats


class Simulation:
    """Runs the files in order on one stock, optionally emitting frames."""

    def __init__(self, stock, rapid_feed=2000.0, seconds_per_frame=10.0,
                 on_frame=None, progress=None):
        self.stock = stock
        self.rapid_feed = float(rapid_feed)
        self.seconds_per_frame = float(seconds_per_frame)
        self.on_frame = on_frame
        self.progress = progress
        self.machine_seconds = 0.0
        self.frames = 0
        self.crash_rapids = 0
        self.crash_lines = []
        self.warnings = []
        self.files = []
        self._next_frame = 0.0
        self.state = {"file": "", "tool": "", "z": 0.0, "line": 0, "lines": 0,
                      "x": 0.0, "y": 0.0, "radius": 0.0}

    def emit(self, banner=None, force=False):
        if self.on_frame is None:
            return
        self.on_frame(self, banner=banner)
        self.frames += 1

    def run_file(self, path, text, tool_spec, banner=None):
        kernel = ToolKernel(tool_spec, self.stock.resolution, self.stock.height)
        parser = GcodeParser(text, rapid_feed=self.rapid_feed)
        name = os.path.basename(path)
        scan = scan_file(text, self.rapid_feed)
        total_lines = max(1, scan["lines"])
        self.state.update({"file": name, "tool": kernel.label, "lines": total_lines})

        started = self.machine_seconds
        crashes = 0
        next_progress = 0.05
        resolution = self.stock.resolution
        last_cell = None

        for move in parser.moves():
            length = math.dist(move.start, move.end)
            feed = move.feed if move.kind != "rapid" else self.rapid_feed
            deepest_z = min(move.start[2], move.end[2])
            # A rapid below Z0 still sweeps the tool profile through the block,
            # so it is stamped like a cut; only the ones that remove something
            # are reported as crashes.
            checking = rapid_below_zero(move)
            cutting = move.kind == "cut" or checking
            crashed = False

            if cutting and deepest_z < 0 and length > 0:
                # Sample fine enough that consecutive stamps overlap: the step
                # follows the smallest footprint the tool has along the move.
                shallowest = max(move.start[2], move.end[2])
                radius = kernel.footprint_radius(max(0.0, -shallowest))
                step = min(MAX_STEP_MM, 0.5 * radius) if radius > 0 else resolution
                step = max(step, resolution)
                samples = max(1, int(math.ceil(length / step)))
                for index in range(samples + 1):
                    ratio = index / float(samples)
                    x = move.start[0] + (move.end[0] - move.start[0]) * ratio
                    y = move.start[1] + (move.end[1] - move.start[1]) * ratio
                    z = move.start[2] + (move.end[2] - move.start[2]) * ratio
                    if z >= 0:
                        continue
                    cell = (int((x - self.stock.x0) / resolution),
                            int((y - self.stock.y0) / resolution),
                            int(z / resolution))
                    if cell == last_cell:
                        continue
                    last_cell = cell
                    if self.stock.stamp(kernel, x, y, z, detect=checking):
                        crashed = True

            if crashed:
                crashes += 1
                self.crash_rapids += 1
                self.crash_lines.append(move.line)

            if feed:
                self.machine_seconds += length / feed * 60.0
            self.state.update({
                "x": move.end[0], "y": move.end[1], "z": move.end[2],
                "line": move.line,
                "radius": kernel.footprint_radius(max(0.0, -move.end[2])),
            })

            while (self.on_frame is not None
                   and self.machine_seconds >= self._next_frame):
                self.emit(banner=banner)
                self._next_frame += self.seconds_per_frame

            if self.progress and move.line / float(total_lines) >= next_progress:
                self.progress(name, move.line, total_lines, self.machine_seconds)
                while move.line / float(total_lines) >= next_progress:
                    next_progress += 0.05

        for warning in parser.warnings:
            message = "%s: %s" % (name, warning)
            if message not in self.warnings:
                self.warnings.append(message)

        self.files.append({
            "path": path,
            "tool": tool_spec,
            "tool_label": kernel.label,
            "lines": parser.lines,
            "machine_seconds": round(self.machine_seconds - started, 1),
            "machine_time": format_time(self.machine_seconds - started),
            "cut_moves": scan["cut_moves"],
            "rapid_moves": scan["rapid_moves"],
            "rapids_below_zero": scan["rapids_below_zero"],
            "crash_rapids": crashes,
            "z_min": round(scan["z_min"], 4) if scan["z_min"] is not None else None,
            "z_max": round(scan["z_max"], 4) if scan["z_max"] is not None else None,
            "x_min": round(scan["x_min"], 4) if scan["x_min"] is not None else None,
            "x_max": round(scan["x_max"], 4) if scan["x_max"] is not None else None,
            "y_min": round(scan["y_min"], 4) if scan["y_min"] is not None else None,
            "y_max": round(scan["y_max"], 4) if scan["y_max"] is not None else None,
            "tools": scan["tools"],
        })
        return self.files[-1]


# --------------------------------------------------------------------------- #
# Encoding
# --------------------------------------------------------------------------- #


class Encoder:
    """Raw rgb24 frames piped into ffmpeg/libx264."""

    def __init__(self, path, width, height, fps):
        self.path = path
        command = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "rawvideo", "-pix_fmt", "rgb24",
            "-s", "%dx%d" % (width, height), "-r", str(fps), "-i", "-",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20",
            "-movflags", "+faststart", path,
        ]
        try:
            self.process = subprocess.Popen(command, stdin=subprocess.PIPE)
        except FileNotFoundError:
            die("the 'ffmpeg' binary was not found on PATH; install ffmpeg "
                "or pass --no-video to only write the final PNG")

    def write(self, image):
        try:
            self.process.stdin.write(image.tobytes())
        except BrokenPipeError:
            die("ffmpeg stopped reading frames; see its output above")

    def close(self):
        self.process.stdin.close()
        code = self.process.wait()
        if code != 0:
            die("ffmpeg exited with status %d" % code)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

VALUE_FLAGS = {
    "--stock", "--origin", "--out", "--resolution", "--fps",
    "--machine-seconds-per-frame", "--rapid-feed", "--hud-height",
}


def split_ordered_arguments(argv):
    """Pull out the ordered (--tool, file, file, ...) sequence.

    argparse cannot express "this option applies to the positionals after it",
    so the ordering is read here and the remaining flags go to argparse.
    """
    jobs = []
    rest = []
    current_tool = None
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--tool":
            if index + 1 >= len(argv):
                die("--tool needs a value, e.g. --tool endmill:3.0")
            current_tool = argv[index + 1]
            index += 2
            continue
        if token.startswith("--tool="):
            current_tool = token.split("=", 1)[1]
            index += 1
            continue
        if token.startswith("-"):
            rest.append(token)
            if token in VALUE_FLAGS and "=" not in token:
                if index + 1 >= len(argv):
                    die("%s needs a value" % token)
                rest.append(argv[index + 1])
                index += 2
                continue
            index += 1
            continue
        if current_tool is None:
            die("no --tool given before %s; a --tool applies to the files "
                "that follow it" % token)
        jobs.append((current_tool, token))
        index += 1
    return jobs, rest


def parse_stock(text):
    parts = str(text).lower().split("x")
    if len(parts) != 3:
        die("--stock must be WxDxH in mm, e.g. 100x100x10 (got %r)" % text)
    try:
        values = [float(part) for part in parts]
    except ValueError:
        die("--stock must be numbers, e.g. 100x100x10 (got %r)" % text)
    if min(values) <= 0:
        die("--stock dimensions must be positive (got %r)" % text)
    return values


def build_parser():
    parser = argparse.ArgumentParser(
        prog="cam_sim_video.py",
        description="Simulate GRBL G-code as a 2.5D material-removal video.")
    parser.add_argument("--stock", required=True, metavar="WxDxH",
                        help="stock size in mm, e.g. 100x100x10 (Z0 = the top).")
    parser.add_argument("--origin", default="center", choices=("center", "corner"),
                        help="where XY zero sits on the stock (default: center).")
    parser.add_argument("--out", required=True, help="output .mp4 path.")
    parser.add_argument("--resolution", type=float, default=0.1,
                        help="heightmap cell size in mm (default: 0.1).")
    parser.add_argument("--fps", type=int, default=30, help="video frame rate.")
    parser.add_argument("--machine-seconds-per-frame", type=float, default=10.0,
                        help="machine seconds between frames (default: 10).")
    parser.add_argument("--rapid-feed", type=float, default=2000.0,
                        help="G0 feed in mm/min used for timing (default: 2000).")
    parser.add_argument("--depth-scale", type=float, default=None,
                        help="depth in mm that maps to the darkest colour "
                             "(default: the deepest cut of the job). Lower it when "
                             "a few deep holes flatten the shallow relief.")
    parser.add_argument("--hud-height", type=int, default=60,
                        help="HUD bar height in pixels (default: 60).")
    parser.add_argument("--no-video", action="store_true",
                        help="skip ffmpeg; only write the final PNG and the JSON.")
    return parser


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    jobs, rest = split_ordered_arguments(argv)
    options = build_parser().parse_args(rest)
    require_deps()
    if not jobs:
        die("no G-code files given")

    width, depth, height = parse_stock(options.stock)
    stock = Stock(width, depth, height, origin=options.origin,
                  resolution=options.resolution)

    sources = []
    for tool_spec, path in jobs:
        expanded = os.path.expanduser(path)
        if not os.path.exists(expanded):
            die("no such G-code file: %s" % path)
        with open(expanded, "r", encoding="utf-8", errors="replace") as handle:
            sources.append((tool_spec, expanded, handle.read()))

    # Dry pass first: the colour ramp normalises to the real deepest cut, and
    # the progress line can quote the total run time from the start.
    deepest = 0.0
    total_estimate = 0.0
    for _tool, _path, text in sources:
        scan = scan_file(text, options.rapid_feed)
        if scan["z_min"] is not None:
            deepest = max(deepest, -scan["z_min"])
        total_estimate += scan["seconds"]
    deepest = min(max(deepest, 0.2), height)
    if options.depth_scale:
        deepest = max(float(options.depth_scale), 0.05)

    out_path = os.path.expanduser(options.out)
    out_dir = os.path.dirname(os.path.abspath(out_path))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    png_path = out_path + ".final.png"

    renderer = Renderer(stock, deepest, hud_height=options.hud_height)
    encoder = None
    if not options.no_video:
        encoder = Encoder(out_path, renderer.width, renderer.height, options.fps)

    def hud_for(simulation, banner=None):
        state = simulation.state
        return {
            "line1": "%s   %s" % (state["file"], state["tool"]),
            "line2": "t %s / %s   Z %+.3f mm   line %d/%d   warnings %d"
                     % (format_time(simulation.machine_seconds),
                        format_time(total_estimate),
                        state["z"], state["line"], state["lines"],
                        len(simulation.warnings) + simulation.crash_rapids),
        }

    def on_frame(simulation, banner=None):
        state = simulation.state
        image = renderer.frame(
            stock.heightmap, hud_for(simulation),
            tool=(state["x"], state["y"], state["radius"]),
            banner=banner)
        if encoder is not None:
            encoder.write(image)

    def progress(name, line, total, seconds):
        sys.stderr.write("  %-38s %3d%%  machine %s\n"
                         % (name, int(100.0 * line / total), format_time(seconds)))
        sys.stderr.flush()

    simulation = Simulation(
        stock, rapid_feed=options.rapid_feed,
        seconds_per_frame=options.machine_seconds_per_frame,
        on_frame=on_frame if encoder is not None else None,
        progress=progress)

    hold_frames = int(round(HOLD_SECONDS * options.fps))
    for index, (tool_spec, path, text) in enumerate(sources):
        sys.stderr.write("simulating %s with %s\n" % (os.path.basename(path), tool_spec))
        simulation.run_file(path, text, tool_spec)
        if encoder is not None:
            following = sources[index + 1][0] if index + 1 < len(sources) else None
            banner = ("tool change → %s" % parse_tool_spec(following)[1]) if following else None
            for _ in range(hold_frames):
                on_frame(simulation, banner=banner)
                simulation.frames += 1

    if encoder is not None:
        encoder.close()

    final = renderer.frame(stock.heightmap, {
        "line1": "%s   done" % os.path.basename(out_path),
        "line2": "machine time %s   deepest %.3f mm   %d frames"
                 % (format_time(simulation.machine_seconds), deepest, simulation.frames),
    })
    final.save(png_path)

    reached = {
        "x_min": min(entry["x_min"] for entry in simulation.files),
        "x_max": max(entry["x_max"] for entry in simulation.files),
        "y_min": min(entry["y_min"] for entry in simulation.files),
        "y_max": max(entry["y_max"] for entry in simulation.files),
    }
    outside = (reached["x_min"] < stock.x0 - 1e-6
               or reached["x_max"] > stock.x0 + stock.width + 1e-6
               or reached["y_min"] < stock.y0 - 1e-6
               or reached["y_max"] > stock.y0 + stock.depth + 1e-6)
    summary = {
        "stock": {"width": width, "depth": depth, "height": height,
                  "origin": options.origin, "resolution": options.resolution,
                  "grid": [stock.nx, stock.ny]},
        "files": simulation.files,
        "total_machine_seconds": round(simulation.machine_seconds, 1),
        "total_machine_time": format_time(simulation.machine_seconds),
        "frames": simulation.frames,
        "video_seconds": round(simulation.frames / float(options.fps), 2),
        "deepest_z": round(float(stock.heightmap.min()), 4),
        "crash_rapids": simulation.crash_rapids,
        "crash_lines": simulation.crash_lines[:20],
        "xy_reached": {key: round(value, 4) for key, value in reached.items()},
        "xy_outside_stock": bool(outside),
        "warnings": simulation.warnings,
        "outputs": {"video": None if encoder is None else out_path, "final_png": png_path},
    }
    json.dump(summary, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
