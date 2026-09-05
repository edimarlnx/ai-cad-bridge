# Build the CNC open molds for the "moeda zCloud" coins and post them to GRBL.
#
# Run it through run.sh (which exports the SVG faces with openscad on the host
# first); on its own:
#
#   flatpak run --command=FreeCADCmd org.freecad.FreeCAD examples/moeda-zcloud/build_molds.py
#
# Everything goes through the bridge's own CAM tools (freecad/AiBridge/tools),
# so this script is also an end-to-end test of them: whatever the agent can do
# over MCP, this file does with the same handlers.
#
# Geometry, per denomination and per coin face (source: moeda-zcloud-master.scad
# and moeda-zcloud.md section 3c):
#
#   block  square (d + 2*8) x 8 mm, top face at Z=0, XY zero at the cavity centre
#   cavity cylinder of diameter d, flat floor at Z=-1.8 (espessura/2)
#   relief the 2D coin face, V-carved DOWN from the floor to at most Z=-2.9
#   pearls 52 V-bit dimples on radius d/2-2.6, 0.55 mm below the floor
#   serration 130 radial V-bit notches at the cavity wall, 0.5 mm below the floor
#
# The relief is raised on the coin, so in the mold it is recessed below the
# cavity floor. Both faces are mirrored (in Y) so the cast half reads
# correctly; the obverse is not.
#
# Two layouts, selected by MOEDA_LAYOUT:
#
#   single (default)  one block per coin face, side d + 2*8, height 8 mm. This is
#                     the original output and is kept byte-for-byte compatible.
#   sheet2            ONE 100 x 100 x 10 mm block cut from a 100 mm wide POM
#                     sheet, holding four cavities on a 50 mm grid: the top row
#                     (Y = +25) is two obverse cavities, the bottom row
#                     (Y = -25) two reverse ones. One XY zero (block centre),
#                     one Z0 (block top), one file per tool for the whole block.
#                     After machining the block is sawn at X=0 and Y=0 into four
#                     50 x 50 half-molds: two complete coins of the same value.
import json
import math
import os
import shutil
import sys
import time
import traceback

import FreeCAD as App
import Part
import importSVG

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, os.path.join(_REPO, "freecad", "AiBridge"))

from tools import cam  # noqa: E402  (needs the sys.path line above)

# --- parameters -------------------------------------------------------------
OUT_DIR = os.path.expanduser(
    os.environ.get("MOEDA_OUT_DIR")
    or os.path.join(os.path.dirname(os.path.abspath(__file__)), "out", "cnc")
)
SVG_DIR = os.path.join(_HERE, "svg")
REPO_OUT = os.path.join(_HERE, "out")

LAYOUT = (os.environ.get("MOEDA_LAYOUT") or "single").strip().lower()
if LAYOUT not in ("single", "sheet2"):
    raise SystemExit("MOEDA_LAYOUT must be 'single' or 'sheet2', got %r" % LAYOUT)

ALL_DENOMINATIONS = [
    {"value": "1", "diameter": 34.0},
    {"value": "5", "diameter": 37.0},
    {"value": "10", "diameter": 40.0},
]
_WANTED_VALUES = (os.environ.get("MOEDA_VALUES") or "").split()
DENOMINATIONS = [item for item in ALL_DENOMINATIONS
                 if not _WANTED_VALUES or item["value"] in _WANTED_VALUES]
if not DENOMINATIONS:
    raise SystemExit("MOEDA_VALUES matched no denomination (have 1, 5, 10)")
FACES = ["anverso", "reverso"]

MOLD_MARGIN = 8.0          # molde_margem
BLOCK_HEIGHT = 8.0         # molde_alt, single layout

# sheet2: the POM sheet is 100 mm wide x 500 mm long x 10 mm thick. One block is
# a 100 mm length of it, so the block is square and the sheet yields five of them.
SHEET2_SIDE = 100.0
SHEET2_HEIGHT = 10.0
SHEET2_PITCH = 50.0        # cell pitch on both axes; centres at +/- PITCH/2
SHEET2_CELLS = [
    {"name": "c1", "face": "anverso", "strip": "A", "centre": (-25.0, 25.0)},
    {"name": "c2", "face": "anverso", "strip": "A", "centre": (25.0, 25.0)},
    {"name": "c3", "face": "reverso", "strip": "B", "centre": (-25.0, -25.0)},
    {"name": "c4", "face": "reverso", "strip": "B", "centre": (25.0, -25.0)},
]

# Alignment pins. The block is sawn ONCE, along Y=0, into two 100 x 50 strips:
# strip A (Y > 0) carries the two obverse cavities, strip B (Y < 0) the two
# reverse ones. Strip B is then flipped 180 degrees about the X axis onto strip
# A, which closes both coins at once. That flip maps (x, y) -> (x, -y), so the
# holes must be mirrored in Y — NOT point-symmetric — for one set to land on the
# other.
PIN_HOLES = [
    {"name": "p1", "strip": "A", "centre": (-44.0, 6.0)},
    {"name": "p2", "strip": "A", "centre": (44.0, 6.0)},
    {"name": "p3", "strip": "B", "centre": (-44.0, -6.0)},
    {"name": "p4", "strip": "B", "centre": (44.0, -6.0)},
]
PIN_HOLE_DEPTH = 6.0       # blind, in a 10 mm block
PIN_LENGTH = 11.0
PIN_CHAMFER = 0.5          # 0.5 x 45 degrees on both ends
PIN_MIN_EDGE = 4.0         # hole edge to block edge and to the saw line
PIN_MIN_CAVITY = 5.0       # hole edge to cavity edge
PIN_RETRACT_HEIGHT = 1.0   # peck retract; rapids still travel at SAFE_HEIGHT
# PIN_HOLE_DIAMETER, PIN_DIAMETER and PIN_PECK_DEPTH follow the T1 profile below:
# the holes are plunged with the roughing bit, so the bore IS the tool diameter.

# Block height: 10 mm for the sheet, 8 mm for the legacy single blocks.
MOLD_HEIGHT = float(os.environ.get("MOEDA_MOLD_HEIGHT")
                    or (SHEET2_HEIGHT if LAYOUT == "sheet2" else BLOCK_HEIGHT))

CAVITY_DEPTH = 1.8         # espessura / 2
RELIEF = 1.1               # relevo
PEARL_COUNT = 52           # perola_n
PEARL_INSET = 2.6          # pearls sit on radius d/2 - 2.6
PEARL_DEPTH = 0.55         # V-bit plunge depth
SERRATION_COUNT = 130      # serrilha_n
SERRATION_DEPTH = 0.5
SERRATION_LENGTH = 0.8     # radial length of each notch, ending at the wall

SAFE_HEIGHT = 5.0
CLEARANCE_HEIGHT = 8.0
Z_FLOOR = -(CAVITY_DEPTH + RELIEF)          # -2.9, the deepest legal cut
Z_FLOOR_TOLERANCE = -3.1                    # the check limit asked for

# 3018 router, POM/nylon. The roughing bit is a parameter: MOEDA_T1_DIAMETER
# picks one of the profiles below, feeds and all, because a 4-flute HSS Ø3.0
# does not want the same numbers as a 2-flute carbide Ø3.175.
T1_PROFILES = {
    3.0: {
        "label": "T1 endmill 3.0 HSS 4F", "flutes": 4, "shank": 6.0,
        "description": "flat end mill Ø3.0 mm HSS, 4 flutes, 6 mm shank",
        "horiz_feed": 500.0, "vert_feed": 150.0,
        "step_down": 0.5, "step_over": 40.0, "peck_depth": 1.0,
    },
    3.175: {
        "label": "T1 endmill 3.175", "flutes": 2, "shank": 3.175,
        "description": "flat end mill Ø3.175 mm, 2 flutes",
        "horiz_feed": 600.0, "vert_feed": 200.0,
        "step_down": 0.6, "step_over": 40.0, "peck_depth": 1.5,
    },
}
T1_DIAMETER = round(float(os.environ.get("MOEDA_T1_DIAMETER") or 3.0), 4)
if T1_DIAMETER not in T1_PROFILES:
    raise SystemExit("MOEDA_T1_DIAMETER must be one of %s, got %s"
                     % (sorted(T1_PROFILES), T1_DIAMETER))
_T1_PROFILE = T1_PROFILES[T1_DIAMETER]
T1 = {
    "number": 1, "shape": "endmill", "label": _T1_PROFILE["label"],
    "diameter": T1_DIAMETER, "flutes": _T1_PROFILE["flutes"],
    "shank_diameter": _T1_PROFILE["shank"],
    "description": _T1_PROFILE["description"],
    "horiz_feed": _T1_PROFILE["horiz_feed"], "vert_feed": _T1_PROFILE["vert_feed"],
    "horiz_rapid": 2000.0, "vert_rapid": 800.0, "spindle_speed": 10000.0,
    "step_down": _T1_PROFILE["step_down"], "step_over": _T1_PROFILE["step_over"],
}

# Pin holes are plunged with T1, so the hole is the tool diameter exactly and the
# pin is turned 0.1 mm under it.
PIN_HOLE_DIAMETER = T1["diameter"]
# File-name tag for T1, derived from the diameter: 3.0 -> "endmill30", 3.175 -> "endmill3175".
T1_TAG = "endmill" + ("%g" % T1["diameter"]).replace(".", "")
PIN_DIAMETER = round(PIN_HOLE_DIAMETER - 0.1, 3)
PIN_PECK_DEPTH = _T1_PROFILE["peck_depth"]
T2 = {
    "number": 2, "shape": "v-bit", "label": "T2 v-bit 30",
    "diameter": 3.175, "cutting_edge_angle": 30.0, "tip_diameter": 0.1,
    "cutting_edge_height": 6.0,
    "horiz_feed": 400.0, "vert_feed": 150.0,
    "horiz_rapid": 2000.0, "vert_rapid": 800.0, "spindle_speed": 10000.0,
}
POST = "grbl"
POST_ARGS = "--translate_drill"   # GRBL has no canned cycles: expand G81 to G0/G1

_LOG = []


def log(message):
    _LOG.append(message)
    sys.stderr.write("[build_molds] %s\n" % message)
    sys.stderr.flush()


def call(tool_name, **params):
    """Invoke one bridge CAM tool exactly as the MCP dispatcher would."""
    for tool in cam.TOOLS:
        if tool["name"] == tool_name:
            return tool["handler"](App, None, params)
    raise RuntimeError("no such tool: %s" % tool_name)


# --- geometry ---------------------------------------------------------------
def build_block_solid(diameter):
    """Block with the cylindrical cavity and the 52 pearl dimples cut in it.

    The relief and the serration are produced by toolpaths only: V-carving a
    text outline as a solid boolean is slow and fragile, and the machined result
    is defined by the tool, not by a modelled pocket. What the STEP/STL show is
    therefore the block, the cavity and the pearls.
    """
    side = diameter + 2 * MOLD_MARGIN
    block = Part.makeBox(
        side, side, MOLD_HEIGHT, App.Vector(-side / 2, -side / 2, -MOLD_HEIGHT))
    solid = block.cut(Part.Compound(cavity_cuts(diameter, (0.0, 0.0))))
    if not solid.isValid():
        raise RuntimeError("the mold block solid is not valid")
    return solid


def build_sheet2_solid(diameter):
    """The 100 x 100 x 10 mm block with the four cavities of one denomination."""
    block = Part.makeBox(
        SHEET2_SIDE, SHEET2_SIDE, MOLD_HEIGHT,
        App.Vector(-SHEET2_SIDE / 2, -SHEET2_SIDE / 2, -MOLD_HEIGHT))
    cuts = []
    for cell in SHEET2_CELLS:
        cuts.extend(cavity_cuts(diameter, cell["centre"]))
    for hole in PIN_HOLES:
        cx, cy = hole["centre"]
        cuts.append(Part.makeCylinder(
            PIN_HOLE_DIAMETER / 2, PIN_HOLE_DEPTH,
            App.Vector(cx, cy, -PIN_HOLE_DEPTH)))
    solid = block.cut(Part.Compound(cuts))
    if not solid.isValid():
        raise RuntimeError("the sheet2 block solid is not valid")
    return solid


def check_pin_clearances(diameter):
    """Prove the four pin holes clear the block edges, the saw line and the cavities."""
    hole_radius = PIN_HOLE_DIAMETER / 2
    cavity_radius = diameter / 2
    measured = []
    for hole in PIN_HOLES:
        cx, cy = hole["centre"]
        to_edge = min(SHEET2_SIDE / 2 - abs(cx), SHEET2_SIDE / 2 - abs(cy)) - hole_radius
        to_saw = abs(cy) - hole_radius
        to_cavity = min(
            math.hypot(cx - cell["centre"][0], cy - cell["centre"][1])
            for cell in SHEET2_CELLS) - cavity_radius - hole_radius
        if to_edge < PIN_MIN_EDGE - 1e-6:
            raise RuntimeError("pin %s is %.2f mm from the block edge, minimum %.2f"
                               % (hole["name"], to_edge, PIN_MIN_EDGE))
        if to_saw < PIN_MIN_EDGE - 1e-6:
            raise RuntimeError("pin %s is %.2f mm from the saw line, minimum %.2f"
                               % (hole["name"], to_saw, PIN_MIN_EDGE))
        if to_cavity < PIN_MIN_CAVITY - 1e-6:
            raise RuntimeError("pin %s is %.2f mm from a cavity edge, minimum %.2f"
                               % (hole["name"], to_cavity, PIN_MIN_CAVITY))
        measured.append({"name": hole["name"], "strip": hole["strip"],
                         "x": cx, "y": cy,
                         "to_block_edge_mm": round(to_edge, 3),
                         "to_saw_line_mm": round(to_saw, 3),
                         "to_cavity_edge_mm": round(to_cavity, 3)})
    # The flip that closes the mold is a 180 degree turn about the X axis, so
    # (x, y) -> (x, -y): strip B's holes must land exactly on strip A's.
    mirrored = {(hole["centre"][0], -hole["centre"][1])
                for hole in PIN_HOLES if hole["strip"] == "B"}
    if mirrored != {hole["centre"] for hole in PIN_HOLES if hole["strip"] == "A"}:
        raise RuntimeError("the strip B pin holes do not mirror onto strip A in Y")
    return measured


def cavity_cuts(diameter, centre):
    """Cavity cylinder plus the 52 pearl dimples, around one cell centre."""
    cx, cy = centre
    cuts = [Part.makeCylinder(diameter / 2, CAVITY_DEPTH, App.Vector(cx, cy, -CAVITY_DEPTH))]
    # A 30 degree V-bit plunged 0.55 mm leaves a cone of this radius at the floor.
    dimple_radius = PEARL_DEPTH * math.tan(math.radians(15.0))
    for index in range(PEARL_COUNT):
        px, py = pearl_centre(diameter, index, centre)
        cuts.append(Part.makeCone(
            dimple_radius, 0.0, PEARL_DEPTH,
            App.Vector(px, py, -CAVITY_DEPTH - PEARL_DEPTH)))
    return cuts


def pearl_centre(diameter, index, centre=(0.0, 0.0)):
    radius = diameter / 2 - PEARL_INSET
    angle = 2 * math.pi * index / PEARL_COUNT
    return (centre[0] + radius * math.cos(angle), centre[1] + radius * math.sin(angle))


def serration_edges(diameter, centre=(0.0, 0.0)):
    """130 short radial segments ending at the cavity wall, on the floor plane."""
    edges = []
    outer = diameter / 2
    inner = outer - SERRATION_LENGTH
    for index in range(SERRATION_COUNT):
        angle = 2 * math.pi * index / SERRATION_COUNT
        cos_a, sin_a = math.cos(angle), math.sin(angle)
        edges.append(Part.makeLine(
            App.Vector(centre[0] + inner * cos_a, centre[1] + inner * sin_a, -CAVITY_DEPTH),
            App.Vector(centre[0] + outer * cos_a, centre[1] + outer * sin_a, -CAVITY_DEPTH)))
    return edges


def import_face_shape(doc, svg_path, mirror_x, centre=(0.0, 0.0)):
    """Load one exported coin face as a set of faces sitting on the cavity floor.

    importSVG returns a mix of faces and shells; rebuilding one face set from
    all the closed wires with the Bullseye face maker resolves the nesting
    (counters inside letters stay holes). The result is translated to Z=-1.8
    because Vcarve reads its top surface from BaseShapes[0].Shape.BoundBox.ZMax.
    """
    before = {obj.Name for obj in doc.Objects}
    importSVG.insert(svg_path, doc.Name)
    doc.recompute()
    imported = [obj for obj in doc.Objects if obj.Name not in before]
    wires = []
    for obj in imported:
        shape = getattr(obj, "Shape", None)
        if shape is not None:
            wires.extend(shape.Wires)
    if not wires:
        raise RuntimeError("no closed contour imported from %s" % svg_path)
    shape = Part.makeFace(wires, "Part::FaceMakerBullseye")
    for obj in imported:
        doc.removeObject(obj.Name)
    # A mold cavity shows the design MIRRORED relative to how the cast reads:
    # the SCAD block does it with rotate([180,0,0]) on the half coin, which is
    # a mirror in Y for BOTH faces. Do the same here so the machined cavity
    # matches the OpenSCAD blocks exactly (no face is ever left unmirrored).
    if mirror_x:
        shape = shape.mirror(App.Vector(0, 0, 0), App.Vector(0, 1, 0))
    shape.translate(App.Vector(centre[0], centre[1], -CAVITY_DEPTH))
    return shape


def cavity_floor_face(clone, centre=None, radius=None):
    """Index (1-based) of the flat cavity floor on the job's model clone.

    With four cavities on one block the biggest flat face at Z=-1.8 is not
    enough: ``centre`` restricts the search to the faces whose bounding box is
    centred on that cell (within ``radius``, the cavity radius by default).
    """
    best_index, best_area = None, 0.0
    for index, face in enumerate(clone.Shape.Faces):
        box = face.BoundBox
        if abs(box.ZMax + CAVITY_DEPTH) > 1e-6 or box.ZLength > 1e-6:
            continue
        if centre is not None:
            if math.hypot(box.Center.x - centre[0], box.Center.y - centre[1]) > (radius or 1.0):
                continue
        if face.Area > best_area:
            best_area, best_index = face.Area, index + 1
    if best_index is None:
        raise RuntimeError("could not find the cavity floor face at Z=%.3f (centre %s)"
                           % (-CAVITY_DEPTH, centre))
    return best_index, best_area


# --- one mold block ---------------------------------------------------------
def build_face(doc, value, diameter, face_name, gcode_dir, models_dir):
    side = diameter + 2 * MOLD_MARGIN
    tag = "%s-%s" % (face_name, value)
    log("=== %s d=%.0f side=%.0f ===" % (tag, diameter, side))

    mold = doc.addObject("Part::Feature", "Mold_%s" % face_name)
    mold.Label = "molde-%s-%szcloud" % (face_name, value)
    mold.Shape = build_block_solid(diameter)

    relief = doc.addObject("Part::Feature", "Relief_%s" % face_name)
    relief.Shape = import_face_shape(
        doc, os.path.join(SVG_DIR, "%s-%s.svg" % (face_name, value)),
        mirror_x=True)
    box = relief.Shape.BoundBox
    log("relief x=[%.2f, %.2f] y=[%.2f, %.2f] z=%.2f faces=%d (mirrored in Y, mold)" % (
        box.XMin, box.XMax, box.YMin, box.YMax, box.ZMax, len(relief.Shape.Faces)))
    serration = doc.addObject("Part::Feature", "Serration_%s" % face_name)
    serration.Shape = Part.Compound(serration_edges(diameter))
    doc.recompute()

    job_name = "Job_%s" % face_name
    job_info = call("cam_job_create", models=[mold.Name], name=job_name,
                    post=POST, post_args=POST_ARGS, doc=doc.Name)
    job_name = job_info["job"]
    log("job %s stock %s" % (job_name, job_info["stock"]))

    tool1 = call("cam_tool_add", job=job_name, doc=doc.Name,
                 reuse=job_info["tools"][0]["name"], bit_label="Endmill 3.175",
                 **{k: v for k, v in T1.items()
                    if k not in ("shape", "step_down", "step_over", "description")})
    tool2 = call("cam_tool_add", job=job_name, doc=doc.Name, bit_label="V-bit 30",
                 **{k: v for k, v in T2.items()})

    job = doc.getObject(job_name)
    clone = job.Model.Group[0]
    floor_index, floor_area = cavity_floor_face(clone)
    log("cavity floor Face%d area=%.1f mm2" % (floor_index, floor_area))

    heights = {"SafeHeight": SAFE_HEIGHT, "ClearanceHeight": CLEARANCE_HEIGHT}

    op1 = call("cam_op_add", job=job_name, doc=doc.Name, type="pocket_shape",
               name="op1_cavity_%s" % face_name, tool=tool1["controller"],
               base=[{"object": clone.Name, "subs": ["Face%d" % floor_index]}],
               properties=dict(heights, UseOutline=True, StartDepth=0.0,
                               FinalDepth=-CAVITY_DEPTH, StepDown=T1["step_down"],
                               StepOver=T1["step_over"], FinishDepth=0.2,
                               CutMode="Climb"))
    op2 = call("cam_op_add", job=job_name, doc=doc.Name, type="vcarve",
               name="op2_relief_%s" % face_name, tool=tool2["controller"],
               base_shapes=[relief.Name],
               properties=dict(heights, StartDepth=-CAVITY_DEPTH,
                               FinalDepth=-(CAVITY_DEPTH + RELIEF), StepDown=1.0,
                               Discretize=0.15))
    op3 = call("cam_op_add", job=job_name, doc=doc.Name, type="drilling",
               name="op3_pearls_%s" % face_name, tool=tool2["controller"],
               locations=[list(pearl_centre(diameter, i)) + [0.0]
                          for i in range(PEARL_COUNT)],
               properties=dict(heights, StartDepth=-CAVITY_DEPTH,
                               FinalDepth=-(CAVITY_DEPTH + PEARL_DEPTH),
                               PeckEnabled=False, RetractHeight=SAFE_HEIGHT))
    op4 = call("cam_op_add", job=job_name, doc=doc.Name, type="engrave",
               name="op4_serration_%s" % face_name, tool=tool2["controller"],
               base=[{"object": serration.Name,
                      "subs": ["Edge%d" % (i + 1) for i in range(SERRATION_COUNT)]}],
               properties=dict(heights, StartDepth=-CAVITY_DEPTH,
                               FinalDepth=-(CAVITY_DEPTH + SERRATION_DEPTH),
                               StepDown=1.0))
    for op in (op1, op2, op3, op4):
        log("  %-22s cmds=%5d z=[%s, %s] Final=%s %s" % (
            op["operation"], op.get("commands", 0), op.get("z_min"), op.get("z_max"),
            op.get("FinalDepth"), op.get("warning", "")))

    inspection = call("cam_inspect", job=job_name, doc=doc.Name)
    if not inspection["ok"]:
        raise RuntimeError("cam_inspect rejected %s: %s" % (tag, inspection["issues"]))

    files = ["%s-%s-op1-T1-%s.gcode" % (face_name, value, T1_TAG),
             "%s-%s-op2_4-T2-vbit30.gcode" % (face_name, value)]
    posted = call("cam_postprocess", job=job_name, doc=doc.Name, post=POST,
                  post_args=POST_ARGS, split_by="tool", output_dir=gcode_dir,
                  files=files, overwrite=True)
    if len(posted["files"]) != 2:
        raise RuntimeError("expected one G-code file per tool, got %d for %s"
                           % (len(posted["files"]), tag))

    # Every posted file must survive the pure-parser check.
    limit = side / 2 - T1["diameter"] / 2
    checks = []
    for entry, expected_tool in zip(posted["files"], (1, 2)):
        check = call("cam_gcode_check", path=entry["path"],
                     bounds={"x_min": -limit, "x_max": limit,
                             "y_min": -limit, "y_max": limit},
                     z_floor=Z_FLOOR_TOLERANCE, safe_height=SAFE_HEIGHT,
                     rapid_rate=T1["horiz_rapid"], tools=[expected_tool])
        check["file"] = os.path.basename(entry["path"])
        check["lines"] = entry["lines"]
        checks.append(check)
        log("  check %-42s ok=%s z=[%s, %s] x=[%s, %s] est=%ss %s" % (
            check["file"], check["ok"], check["z_min"], check["z_max"],
            check["x_min"], check["x_max"], check["estimated_seconds"],
            check["issues"]))
        if not check["ok"]:
            raise RuntimeError("G-code check failed for %s: %s"
                               % (check["file"], check["issues"]))

    # Sanity numbers the README quotes.
    op1_floor = op1.get("z_min")
    if abs(op1_floor + CAVITY_DEPTH) > 1e-4:
        raise RuntimeError("op1 floor is %.4f, expected %.4f" % (op1_floor, -CAVITY_DEPTH))
    t2_file = posted["files"][1]["path"]
    pearl_plunges = count_plunges(t2_file, "op3_pearls", -(CAVITY_DEPTH + PEARL_DEPTH))
    if pearl_plunges != PEARL_COUNT:
        raise RuntimeError("%d pearl plunges, expected %d" % (pearl_plunges, PEARL_COUNT))
    notches = count_plunges(t2_file, "op4_serration", -(CAVITY_DEPTH + SERRATION_DEPTH))
    if notches != SERRATION_COUNT:
        raise RuntimeError("%d serration notches, expected %d" % (notches, SERRATION_COUNT))
    log("  counted %d pearl plunges and %d serration notches" % (pearl_plunges, notches))

    for fmt in ("step", "stl"):
        path = os.path.join(models_dir, "molde-%s-%szcloud-d%.0f.%s"
                            % (face_name, value, diameter, fmt))
        if os.path.exists(path):
            os.remove(path)
        if fmt == "step":
            Part.export([mold], path)
        else:
            import Mesh

            Mesh.export([mold], path)

    return {
        "face": face_name,
        "value": value,
        "diameter_mm": diameter,
        "block_side_mm": side,
        "job": job_name,
        "cavity_floor_face": floor_index,
        "operations": [{k: op.get(k) for k in
                        ("operation", "type", "tool_number", "commands",
                         "z_min", "z_max", "x_min", "x_max", "StartDepth",
                         "FinalDepth", "cycle_time")} for op in (op1, op2, op3, op4)],
        "job_estimated_seconds": inspection["estimated_seconds"],
        "pearl_plunges": pearl_plunges,
        "serration_notches": notches,
        "gcode": [{k: check.get(k) for k in
                   ("file", "ok", "lines", "z_min", "z_max", "x_min", "x_max",
                    "y_min", "y_max", "tools", "estimated_seconds", "moves")}
                  for check in checks],
    }


# --- one sheet2 block (four cavities, two coins) -----------------------------
def build_sheet2(doc, value, diameter, gcode_dir, models_dir):
    """One 100 x 100 x 10 mm block: 2 obverse cavities on top, 2 reverse below."""
    tag = "sheet2-%s" % value
    log("=== %s d=%.0f block=%.0fx%.0fx%.0f cells=%d ==="
        % (tag, diameter, SHEET2_SIDE, SHEET2_SIDE, MOLD_HEIGHT, len(SHEET2_CELLS)))
    clearances = check_pin_clearances(diameter)
    for entry in clearances:
        log("  pin %s (%.0f, %.0f) edge=%.2f saw=%.2f cavity=%.2f mm"
            % (entry["name"], entry["x"], entry["y"], entry["to_block_edge_mm"],
               entry["to_saw_line_mm"], entry["to_cavity_edge_mm"]))

    mold = doc.addObject("Part::Feature", "Mold_sheet2")
    mold.Label = "molde-sheet2-%szcloud" % value
    mold.Shape = build_sheet2_solid(diameter)

    reliefs, serrations = {}, {}
    for cell in SHEET2_CELLS:
        relief = doc.addObject("Part::Feature", "Relief_%s" % cell["name"])
        relief.Shape = import_face_shape(
            doc, os.path.join(SVG_DIR, "%s-%s.svg" % (cell["face"], value)),
            mirror_x=True, centre=cell["centre"])
        box = relief.Shape.BoundBox
        log("  %s %-8s relief x=[%.2f, %.2f] y=[%.2f, %.2f] z=%.2f faces=%d"
            % (cell["name"], cell["face"], box.XMin, box.XMax, box.YMin, box.YMax,
               box.ZMax, len(relief.Shape.Faces)))
        reliefs[cell["name"]] = relief
        serration = doc.addObject("Part::Feature", "Serration_%s" % cell["name"])
        serration.Shape = Part.Compound(serration_edges(diameter, cell["centre"]))
        serrations[cell["name"]] = serration
    doc.recompute()

    job_info = call("cam_job_create", models=[mold.Name], name="Job_sheet2",
                    post=POST, post_args=POST_ARGS, doc=doc.Name)
    job_name = job_info["job"]
    log("job %s stock %s" % (job_name, job_info["stock"]))

    tool1 = call("cam_tool_add", job=job_name, doc=doc.Name,
                 reuse=job_info["tools"][0]["name"], bit_label="Endmill 3.175",
                 **{k: v for k, v in T1.items()
                    if k not in ("shape", "step_down", "step_over", "description")})
    tool2 = call("cam_tool_add", job=job_name, doc=doc.Name, bit_label="V-bit 30",
                 **{k: v for k, v in T2.items()})

    job = doc.getObject(job_name)
    clone = job.Model.Group[0]
    floors = []
    for cell in SHEET2_CELLS:
        index, area = cavity_floor_face(clone, centre=cell["centre"])
        floors.append(index)
        log("  %s cavity floor Face%d area=%.1f mm2" % (cell["name"], index, area))

    heights = {"SafeHeight": SAFE_HEIGHT, "ClearanceHeight": CLEARANCE_HEIGHT}

    # T1: one pocket operation carrying the four cavity floors, so the whole
    # block is roughed in a single posted file with a single tool.
    op1 = call("cam_op_add", job=job_name, doc=doc.Name, type="pocket_shape",
               name="op1_cavities", tool=tool1["controller"],
               base=[{"object": clone.Name,
                      "subs": ["Face%d" % index for index in floors]}],
               properties=dict(heights, UseOutline=True, StartDepth=0.0,
                               FinalDepth=-CAVITY_DEPTH, StepDown=T1["step_down"],
                               StepOver=T1["step_over"], FinishDepth=0.2,
                               CutMode="Climb"))
    # T1, still: the four alignment pin holes, after the pockets. A Ø3.2 hole is
    # bored with the Ø3.175 end mill, so the tool change count stays at one.
    op_pins = add_pin_holes_op(doc, job_name, clone, tool1["controller"], heights)
    ops = [op1, op_pins]
    # T2: relief, pearls and serration, cell by cell.
    for cell in SHEET2_CELLS:
        name = cell["name"]
        ops.append(call(
            "cam_op_add", job=job_name, doc=doc.Name, type="vcarve",
            name="op2_relief_%s" % name, tool=tool2["controller"],
            base_shapes=[reliefs[name].Name],
            properties=dict(heights, StartDepth=-CAVITY_DEPTH,
                            FinalDepth=-(CAVITY_DEPTH + RELIEF), StepDown=1.0,
                            Discretize=0.15)))
        ops.append(call(
            "cam_op_add", job=job_name, doc=doc.Name, type="drilling",
            name="op3_pearls_%s" % name, tool=tool2["controller"],
            locations=[list(pearl_centre(diameter, i, cell["centre"])) + [0.0]
                       for i in range(PEARL_COUNT)],
            properties=dict(heights, StartDepth=-CAVITY_DEPTH,
                            FinalDepth=-(CAVITY_DEPTH + PEARL_DEPTH),
                            PeckEnabled=False, RetractHeight=SAFE_HEIGHT)))
        ops.append(call(
            "cam_op_add", job=job_name, doc=doc.Name, type="engrave",
            name="op4_serration_%s" % name, tool=tool2["controller"],
            base=[{"object": serrations[name].Name,
                   "subs": ["Edge%d" % (i + 1) for i in range(SERRATION_COUNT)]}],
            properties=dict(heights, StartDepth=-CAVITY_DEPTH,
                            FinalDepth=-(CAVITY_DEPTH + SERRATION_DEPTH),
                            StepDown=1.0)))
    for op in ops:
        log("  %-22s cmds=%5d z=[%s, %s] Final=%s %s" % (
            op["operation"], op.get("commands", 0), op.get("z_min"), op.get("z_max"),
            op.get("FinalDepth"), op.get("warning", "")))

    inspection = call("cam_inspect", job=job_name, doc=doc.Name)
    if not inspection["ok"]:
        raise RuntimeError("cam_inspect rejected %s: %s" % (tag, inspection["issues"]))

    files = ["sheet2-%s-op1-T1-%s.gcode" % (value, T1_TAG),
             "sheet2-%s-op2_4-T2-vbit30.gcode" % value]
    posted = call("cam_postprocess", job=job_name, doc=doc.Name, post=POST,
                  post_args=POST_ARGS, split_by="tool", output_dir=gcode_dir,
                  files=files, overwrite=True)
    if len(posted["files"]) != 2:
        raise RuntimeError("expected one G-code file per tool, got %d for %s"
                           % (len(posted["files"]), tag))

    limit = SHEET2_SIDE / 2 - T1["diameter"] / 2
    # T1 also bores the pin holes, so its floor is the hole depth, not the relief.
    z_limits = (-(PIN_HOLE_DEPTH + 0.1), Z_FLOOR_TOLERANCE)
    checks = []
    for entry, expected_tool, z_limit in zip(posted["files"], (1, 2), z_limits):
        check = call("cam_gcode_check", path=entry["path"],
                     bounds={"x_min": -limit, "x_max": limit,
                             "y_min": -limit, "y_max": limit},
                     z_floor=z_limit, safe_height=SAFE_HEIGHT,
                     rapid_rate=T1["horiz_rapid"], tools=[expected_tool])
        check["file"] = os.path.basename(entry["path"])
        check["lines"] = entry["lines"]
        checks.append(check)
        log("  check %-42s ok=%s z=[%s, %s] x=[%s, %s] est=%ss %s" % (
            check["file"], check["ok"], check["z_min"], check["z_max"],
            check["x_min"], check["x_max"], check["estimated_seconds"],
            check["issues"]))
        if not check["ok"]:
            raise RuntimeError("G-code check failed for %s: %s"
                               % (check["file"], check["issues"]))

    # Every one of the four cavity floors must actually be cut by the T1 file.
    t1_file, t2_file = posted["files"][0]["path"], posted["files"][1]["path"]
    reached = cavity_moves_per_cell(t1_file, diameter, -CAVITY_DEPTH)
    empty = [name for name, count in reached.items() if count == 0]
    if empty:
        raise RuntimeError("no floor move inside cavities %s in %s"
                           % (", ".join(empty), os.path.basename(t1_file)))
    log("  floor moves per cell: %s"
        % ", ".join("%s=%d" % (cell["name"], reached[cell["name"]]) for cell in SHEET2_CELLS))
    op1_floor = op1.get("z_min")
    if abs(op1_floor + CAVITY_DEPTH) > 1e-4:
        raise RuntimeError("op1 floor is %.4f, expected %.4f" % (op1_floor, -CAVITY_DEPTH))

    # The four pin holes must bottom out at -6.0 where they were asked for, and
    # nothing else in the T1 file may go below the cavity floor.
    bottoms = pin_hole_bottoms(t1_file)
    for hole in PIN_HOLES:
        hit = [point for point in bottoms
               if math.hypot(point[0] - hole["centre"][0],
                             point[1] - hole["centre"][1]) <= 1.0]
        if not hit:
            raise RuntimeError("pin %s: no move at Z=%.2f within 1 mm of (%.1f, %.1f)"
                               % (hole["name"], -PIN_HOLE_DEPTH,
                                  hole["centre"][0], hole["centre"][1]))
    stray = [point for point in bottoms
             if not any(math.hypot(point[0] - hole["centre"][0],
                                   point[1] - hole["centre"][1]) <= 1.0
                        for hole in PIN_HOLES)]
    if stray:
        raise RuntimeError("%d move(s) at the pin depth away from any pin: %s"
                           % (len(stray), stray[:5]))
    offenders = deep_moves_outside_pins(t1_file, -(CAVITY_DEPTH + 0.1))
    if offenders:
        raise RuntimeError("%d T1 move(s) below Z-%.2f outside the pin holes: %s"
                           % (len(offenders), CAVITY_DEPTH + 0.1, offenders[:5]))
    log("  four pin holes bottom at Z-%.2f, nothing else in T1 below Z-%.2f"
        % (PIN_HOLE_DEPTH, CAVITY_DEPTH + 0.1))

    expected_pearls = PEARL_COUNT * len(SHEET2_CELLS)
    expected_notches = SERRATION_COUNT * len(SHEET2_CELLS)
    pearl_plunges = count_plunges(t2_file, "op3_pearls", -(CAVITY_DEPTH + PEARL_DEPTH))
    if pearl_plunges != expected_pearls:
        raise RuntimeError("%d pearl plunges, expected %d" % (pearl_plunges, expected_pearls))
    notches = count_plunges(t2_file, "op4_serration", -(CAVITY_DEPTH + SERRATION_DEPTH))
    if notches != expected_notches:
        raise RuntimeError("%d serration notches, expected %d" % (notches, expected_notches))
    log("  counted %d pearl plunges and %d serration notches" % (pearl_plunges, notches))

    base = "sheet2-%s-d%.0f" % (value, diameter)
    for fmt in ("step", "stl"):
        path = os.path.join(models_dir, "%s.%s" % (base, fmt))
        if os.path.exists(path):
            os.remove(path)
        if fmt == "step":
            Part.export([mold], path)
        else:
            import Mesh

            Mesh.export([mold], path)

    return {
        "layout": "sheet2",
        "value": value,
        "diameter_mm": diameter,
        "block_side_mm": SHEET2_SIDE,
        "block_height_mm": MOLD_HEIGHT,
        "cell_pitch_mm": SHEET2_PITCH,
        "cells": [{"name": cell["name"], "face": cell["face"], "strip": cell["strip"],
                   "x": cell["centre"][0], "y": cell["centre"][1],
                   "floor_moves": reached[cell["name"]]} for cell in SHEET2_CELLS],
        "pin_holes": {
            "diameter_mm": PIN_HOLE_DIAMETER,
            "depth_mm": PIN_HOLE_DEPTH,
            "peck_depth_mm": PIN_PECK_DEPTH,
            "retract_height_mm": PIN_RETRACT_HEIGHT,
            "operation": op_pins["operation"],
            "type": op_pins["type"],
            "bottom_points": len(bottoms),
            "clearances": clearances,
        },
        "pin": {"diameter_mm": PIN_DIAMETER, "length_mm": PIN_LENGTH,
                "chamfer_mm": PIN_CHAMFER, "count": len(PIN_HOLES)},
        "job": job_name,
        "cavity_floor_faces": floors,
        "operations": [{k: op.get(k) for k in
                        ("operation", "type", "tool_number", "commands",
                         "z_min", "z_max", "x_min", "x_max", "StartDepth",
                         "FinalDepth", "cycle_time")} for op in ops],
        "job_estimated_seconds": inspection["estimated_seconds"],
        "pearl_plunges": pearl_plunges,
        "serration_notches": notches,
        "gcode": [{k: check.get(k) for k in
                   ("file", "ok", "lines", "z_min", "z_max", "x_min", "x_max",
                    "y_min", "y_max", "tools", "estimated_seconds", "moves")}
                  for check in checks],
    }


def add_pin_holes_op(doc, job_name, clone, controller, heights):
    """Plunge the four pin holes with T1, pecking, at the controller's plunge feed.

    The hole is the tool: it is cut by plunging the roughing bit, so its diameter
    is T1's and the pin is turned 0.1 mm under that. A helix was tried in an
    earlier revision and rejected — with the tool nearly filling the hole the
    helix radius is a few microns, FreeCAD posts those arcs at the *horizontal*
    feed and rapids the microns in XY at full depth, which is exactly the pattern
    cam_gcode_check exists to catch.

    RetractHeight is the peck retract, well below SafeHeight: G83 pecks start at
    the retract plane, so leaving it at 5 mm made the first pecks cut air above
    the block. Rapids between holes still travel at ClearanceHeight.
    """
    del clone
    op = call("cam_op_add", job=job_name, doc=doc.Name, type="drilling",
              name="op1b_pins", tool=controller,
              locations=[list(hole["centre"]) + [0.0] for hole in PIN_HOLES],
              properties=dict(heights, StartDepth=0.0, FinalDepth=-PIN_HOLE_DEPTH,
                              PeckEnabled=True, PeckDepth=PIN_PECK_DEPTH,
                              RetractHeight=PIN_RETRACT_HEIGHT))
    if not op.get("commands"):
        raise RuntimeError("the pin hole operation produced no toolpath")
    log("  pin holes Ø%.3f plunged with %.1f mm pecks, retract %.1f (%d commands)"
        % (PIN_HOLE_DIAMETER, PIN_PECK_DEPTH, PIN_RETRACT_HEIGHT, op["commands"]))
    return op


def pin_hole_bottoms(path):
    """XY of every point the T1 file reaches at the pin-hole depth."""
    points = []
    position = {"X": 0.0, "Y": 0.0, "Z": 0.0}
    for motion, position in _iter_moves(path, position):
        if motion in ("G1", "G2", "G3") and abs(position["Z"] + PIN_HOLE_DEPTH) <= 1e-3:
            points.append((position["X"], position["Y"]))
    return points


def deep_moves_outside_pins(path, limit):
    """Cutting moves below ``limit`` that are not inside a pin hole (a T1 hazard)."""
    offenders = []
    position = {"X": 0.0, "Y": 0.0, "Z": 0.0}
    radius = PIN_HOLE_DIAMETER / 2 + 1.0
    for motion, position in _iter_moves(path, position):
        if motion not in ("G0", "G1", "G2", "G3") or position["Z"] > limit - 1e-6:
            continue
        if any(math.hypot(position["X"] - hole["centre"][0],
                          position["Y"] - hole["centre"][1]) <= radius
               for hole in PIN_HOLES):
            continue
        offenders.append((round(position["X"], 3), round(position["Y"], 3),
                          round(position["Z"], 3)))
    return offenders


def _iter_moves(path, position):
    """Yield (motion code, modal position) for every motion line of a G-code file."""
    for line in open(path, "r", encoding="utf-8", errors="replace"):
        code = line.split("(")[0].split(";")[0].strip()
        if not code:
            continue
        words = {token[0].upper(): token[1:] for token in code.split() if len(token) > 1}
        motion = ("G" + (words["G"].lstrip("0") or "0")) if "G" in words else None
        for axis in ("X", "Y", "Z"):
            if axis in words:
                try:
                    position[axis] = float(words[axis])
                except ValueError:
                    pass
        yield motion, position


def cavity_moves_per_cell(path, diameter, depth):
    """Count the cutting moves that land on each cell's cavity floor.

    Pure text pass over the posted file: it proves the pocket file really cuts
    all four cavities, not only the one the operation happened to start with.
    """
    counts = {cell["name"]: 0 for cell in SHEET2_CELLS}
    radius = diameter / 2.0
    position = {"X": 0.0, "Y": 0.0, "Z": 0.0}
    for motion, position in _iter_moves(path, position):
        if motion not in ("G1", "G2", "G3"):
            continue
        if abs(position["Z"] - depth) > 1e-3:
            continue
        for cell in SHEET2_CELLS:
            if math.hypot(position["X"] - cell["centre"][0],
                          position["Y"] - cell["centre"][1]) <= radius + 1e-6:
                counts[cell["name"]] += 1
                break
    return counts


def count_plunges(path, operation, depth):
    """Count the entry plunges of one operation in a posted file.

    Every pearl and every serration notch is entered with a single G1 at the
    tool controller's vertical feed down to its final Z; counting those lines
    inside the operation's own block is a direct count of the features cut.
    """
    count = 0
    current = None
    for line in open(path, "r", encoding="utf-8", errors="replace"):
        stripped = line.strip()
        if stripped.startswith("(Begin operation:"):
            current = stripped[len("(Begin operation:"):].strip(" )")
            continue
        if current is None or not current.startswith(operation):
            continue
        code = stripped.split("(")[0].strip()
        if not code.startswith(("G1 ", "G01 ")):
            continue
        words = {token[0].upper(): token[1:] for token in code.split() if len(token) > 1}
        try:
            if abs(float(words.get("Z", "nan")) - depth) > 1e-3:
                continue
            if abs(float(words.get("F", "nan")) - T2["vert_feed"]) > 1e-3:
                continue
        except ValueError:
            continue
        count += 1
    return count


# --- main -------------------------------------------------------------------
def main():
    started = time.time()
    for directory in (OUT_DIR, os.path.join(OUT_DIR, "models"),
                      os.path.join(OUT_DIR, "svg"), REPO_OUT):
        os.makedirs(directory, exist_ok=True)

    blocks = []
    for denomination in DENOMINATIONS:
        value = denomination["value"]
        diameter = denomination["diameter"]
        gcode_dir = os.path.join(OUT_DIR, "gcode", value)
        os.makedirs(gcode_dir, exist_ok=True)
        doc = App.newDocument("molde_%s" % value)
        if LAYOUT == "sheet2":
            blocks.append(build_sheet2(doc, value, diameter, gcode_dir,
                                       os.path.join(OUT_DIR, "models")))
            fcstd = os.path.join(OUT_DIR, "models",
                                 "sheet2-%s-d%.0f.FCStd" % (value, diameter))
        else:
            for face_name in FACES:
                blocks.append(build_face(doc, value, diameter, face_name, gcode_dir,
                                         os.path.join(OUT_DIR, "models")))
            fcstd = os.path.join(OUT_DIR, "models", "molde-%s-d%.0f.FCStd" % (value, diameter))
        if os.path.exists(fcstd):
            os.remove(fcstd)
        doc.saveAs(fcstd)
        App.closeDocument(doc.Name)
        log("saved %s" % fcstd)

    for face_name in FACES:
        for denomination in DENOMINATIONS:
            name = "%s-%s.svg" % (face_name, denomination["value"])
            shutil.copyfile(os.path.join(SVG_DIR, name),
                            os.path.join(OUT_DIR, "svg", name))

    report = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "freecad": ".".join(App.Version()[:3]),
        "layout": LAYOUT,
        "block_height_mm": MOLD_HEIGHT,
        "post": POST,
        "post_args": POST_ARGS,
        "tools": {"T1": T1, "T2": T2},
        "safe_height_mm": SAFE_HEIGHT,
        "clearance_height_mm": CLEARANCE_HEIGHT,
        "z_floor_mm": Z_FLOOR,
        "z_check_limit_mm": Z_FLOOR_TOLERANCE,
        "blocks": blocks,
        "seconds": round(time.time() - started, 1),
    }
    write_report(report)
    log("done in %.0f s: %d blocks, %d G-code files"
        % (report["seconds"], len(blocks), sum(len(b["gcode"]) for b in blocks)))


def hms(seconds):
    """Minutes and seconds, promoted to hours once the total earns them."""
    seconds = int(round(seconds or 0))
    if seconds >= 3600:
        return "%d:%02d:%02d" % (seconds // 3600, (seconds % 3600) // 60, seconds % 60)
    return "%d:%02d" % (seconds // 60, seconds % 60)


def write_report(report):
    """Write the report next to the files it describes, one name per layout.

    The two layouts are independent deliverables that live in the same output
    folder, so sheet2 writes README-sheet2.md / validation-sheet2.json and never
    overwrites the single-block set.
    """
    suffix = "-sheet2" if LAYOUT == "sheet2" else ""
    readme = render_sheet2_readme(report) if LAYOUT == "sheet2" else render_readme(report)
    for directory in (OUT_DIR, REPO_OUT):
        with open(os.path.join(directory, "validation%s.json" % suffix), "w",
                  encoding="utf-8") as handle:
            json.dump(report, handle, indent=2)
        with open(os.path.join(directory, "README%s.md" % suffix), "w",
                  encoding="utf-8") as handle:
            handle.write(readme)


def render_readme(report):
    lines = []
    add = lines.append
    add("# Moeda zCloud — CNC open molds (GRBL, 3018)\n")
    add("Generated by `examples/moeda-zcloud/run.sh` in the `ai-cad-bridge` repo on ")
    add("%s with FreeCAD %s. Do not edit by hand: re-run the script.\n"
        % (report["generated_at"], report["freecad"]))
    add("\n## Zero convention\n")
    add("\n- **XY zero: the centre of the cavity.** Both files of a block share it.")
    add("\n- **Z zero: the top face of the block** (the flat parting face).")
    add("\n- Safe height %.0f mm, clearance %.0f mm, both above Z0."
        % (report["safe_height_mm"], report["clearance_height_mm"]))
    add("\n- Deepest legal cut: Z = %.2f mm (cavity floor %.2f + relief %.2f). Every "
        "posted file was checked against Z >= %.2f.\n"
        % (report["z_floor_mm"], -CAVITY_DEPTH, RELIEF, report["z_check_limit_mm"]))
    add("\n## Blocks\n\n")
    add("| denomination | coin Ø | block | cavity | files |\n|---|---|---|---|---|\n")
    for block in report["blocks"]:
        add("| %s zcloud, %s | %.0f mm | %.0f x %.0f x %.0f mm | Ø%.0f x %.1f mm | `gcode/%s/` |\n"
            % (block["value"], block["face"], block["diameter_mm"],
               block["block_side_mm"], block["block_side_mm"], MOLD_HEIGHT,
               block["diameter_mm"], CAVITY_DEPTH, block["value"]))
    add("\nThe two blocks of one denomination live in the same `models/molde-<value>-d<Ø>.FCStd`, ")
    add("both centred on the origin: each is set up and zeroed on the machine on its own.\n")
    add("\n## Tools and feeds (POM / nylon on a 3018)\n\n")
    add("| tool | bit | spindle | feed | plunge | step-down | step-over |\n")
    add("|---|---|---|---|---|---|---|\n")
    t1, t2 = report["tools"]["T1"], report["tools"]["T2"]
    add("| T1 | flat end mill Ø%.3f mm | %.0f rpm | %.0f mm/min | %.0f mm/min | %.1f mm | %.0f%% |\n"
        % (t1["diameter"], t1["spindle_speed"], t1["horiz_feed"], t1["vert_feed"],
           t1["step_down"], t1["step_over"]))
    add("| T2 | V-bit %.0f° (tip Ø%.1f mm) | %.0f rpm | %.0f mm/min | %.0f mm/min | — | — |\n"
        % (t2["cutting_edge_angle"], t2["tip_diameter"], t2["spindle_speed"],
           t2["horiz_feed"], t2["vert_feed"]))
    add("\n## Run order\n")
    add("\n1. Fixture the block, find the cavity centre, set **X0 Y0** there.")
    add("\n2. Touch off the **top face** of the block: **Z0**.")
    add("\n3. Run `<face>-<value>-op1-T1-%s.gcode` (T1 clears the Ø d cavity to Z-1.80)." % T1_TAG)
    add("\n4. Change to the V-bit. **Do not touch X/Y.** Re-zero **Z only** on the same top face.")
    add("\n5. Run `<face>-<value>-op2_4-T2-vbit30.gcode` (relief, pearls, serration).")
    add("\n6. Deburr, blow out the chips, apply release agent before casting.\n")
    add("\nPosted with the `%s` post processor, arguments `%s` "
        "(GRBL has no canned cycles, so the drilling cycle is expanded to G0/G1).\n"
        % (report["post"], report["post_args"]))
    add("\n## Validation\n\n")
    add("Every file below was re-parsed outside FreeCAD (`cam_gcode_check`): XY inside ")
    add("the block minus the tool radius, Z never below the limit, rapid XY moves only at ")
    add("or above the safe height, spindle on before the first cut, feed rates present, ")
    add("one tool number per file.\n\n")
    add("| file | lines | Z min | Z max | X extent | Y extent | T | est. time |\n")
    add("|---|---|---|---|---|---|---|---|\n")
    total = 0.0
    for block in report["blocks"]:
        for check in block["gcode"]:
            total += check["estimated_seconds"] or 0.0
            add("| `%s/%s` | %d | %.3f | %.3f | %.2f … %.2f | %.2f … %.2f | %s | %s |\n"
                % (block["value"], check["file"], check["lines"], check["z_min"],
                   check["z_max"], check["x_min"], check["x_max"], check["y_min"],
                   check["y_max"], ", ".join(str(t) for t in check["tools"]),
                   hms(check["estimated_seconds"])))
    add("\nTotal estimated cutting time for the six blocks: **%s** "
        "(feed-based; a 3018 will be slower).\n" % hms(total))
    add("\nPer block: %d pearl plunges and %d serration notches, as designed.\n"
        % (PEARL_COUNT, SERRATION_COUNT))
    add("\n## Honest notes\n")
    add("\n- **Pearls.** The master models them as Ø1.1 mm beads. A 30° V-bit plunged "
        "0.55 mm leaves a cone about Ø0.30 mm wide at the floor, so the cast pearls are "
        "smaller dots than the SLA master's. Going to Ø1.1 mm with this bit would mean "
        "plunging 2.05 mm, well past the relief floor. A Ø1 mm ball nose is the fix if "
        "the dots read too fine (moeda-zcloud.md already suggests it).")
    add("\n- **Serration.** Modelled as 130 radial notches on the cavity floor at the "
        "wall, 0.5 mm deep, not as grooves in the vertical wall: a 2.5-axis machine "
        "cannot cut a vertical wall from above. This is the simplification the design "
        "note allows.")
    add("\n- **Relief depth.** V-carving is width-driven: a stroke only reaches the full "
        "1.1 mm when it is at least ~0.69 mm wide. Thin serifs come out shallower, which "
        "is what gives V-carved lettering its draft. The op is clamped at Z%.2f."
        % report["z_floor_mm"])
    add("\n- **The STEP/STL** show the block, the cavity and the pearl dimples. The "
        "relief and the serration exist as toolpaths only — their machined shape is "
        "defined by the V-bit, not by a modelled pocket.\n")
    return "".join(lines)


SHEET2_MAP = """\
                                Y
                                ^
      +-------------------------|-------------------------+   Y = +50
      |                         |                         |
      |        c1               |              c2         |  strip A
      |     (-25, +25)          |          (+25, +25)     |  ANVERSO
      |      anverso            |           anverso       |  (Y > 0)
      |  o p1 (-44, +6)         |       o p2 (+44, +6)    |
   ---+-------------------------O-------------------------+---> X   SAW, Y = 0
      |  o p3 (-44, -6)         |       o p4 (+44, -6)    |
      |        c3               |              c4         |  strip B
      |     (-25, -25)          |          (+25, -25)     |  REVERSO
      |      reverso            |           reverso       |  (Y < 0)
      |                         |                         |
      +-------------------------|-------------------------+   Y = -50
   X = -50                                             X = +50

   O = the single XY zero (block centre). Z0 = the top face of the block.
   o = alignment pin hole. ONE saw cut, along Y = 0.

   Closing: flip strip B 180 degrees about the X axis onto strip A. That maps
   (x, y) -> (x, -y), so c3 lands on c1, c4 on c2, p3 on p1 and p4 on p2 — both
   coins close at once, centred by the two steel pins.
"""


def render_sheet2_readme(report):
    """The sheet2 deliverable: one 100 x 100 x 10 block, four cavities, two coins."""
    lines = []
    add = lines.append
    add("# Moeda zCloud — CNC molds, **sheet2** layout (GRBL, 3018)\n")
    add("Generated by `examples/moeda-zcloud/run.sh` (`MOEDA_LAYOUT=sheet2`) in the ")
    add("`ai-cad-bridge` repo on %s with FreeCAD %s. Do not edit by hand: re-run the "
        "script.\n" % (report["generated_at"], report["freecad"]))

    add("\n## The block\n")
    add("\nCut from a POM sheet **100 mm wide x 500 mm long x 10 mm thick**: one block is ")
    add("a **%.0f x %.0f x %.0f mm** square (the sheet yields five of them). "
        % (SHEET2_SIDE, SHEET2_SIDE, report["block_height_mm"]))
    add("It carries **four cavities on a %.0f mm grid**, centres at (±%.0f, ±%.0f) — "
        "two obverse on the top row, two reverse on the bottom row. "
        % (SHEET2_PITCH, SHEET2_PITCH / 2, SHEET2_PITCH / 2))
    add("That is **two complete coins of the same value per block**.\n")
    add("\n```\n%s```\n" % SHEET2_MAP)
    add("\nAfter machining, the block is sawn **once**, along **Y = 0**, into two ")
    add("100 x 50 x %.0f mm strips: **A** (Y > 0, the two obverse cavities) and **B** "
        "(Y < 0, the two reverse ones). To close the mold, flip strip B 180° about the "
        "**X axis** and lay it on strip A: both coins close at the same time.\n"
        % report["block_height_mm"])
    add("\nMargins with the largest coin (Ø40): **5 mm** from a cavity wall to the block ")
    add("edge and **10 mm** between neighbouring cavities. The Ø%.3f mm end mill stays "
        "inside ±%.4f mm in X and Y, which the validation below measures.\n"
        % (report["tools"]["T1"]["diameter"],
           SHEET2_SIDE / 2 - report["tools"]["T1"]["diameter"] / 2))

    pin = report["blocks"][0]["pin"]
    holes = report["blocks"][0]["pin_holes"]
    add("\n## Alignment pins\n")
    add("\nTwo holes per strip, one at each end: **(±44, +6)** in strip A and ")
    add("**(±44, −6)** in strip B. The pair is mirrored in Y — not point-symmetric — ")
    add("precisely because the closing flip is about the X axis, so p3→p1 and p4→p2.\n")
    add("\n- **Holes: Ø%.1f mm** — the T1 bit itself, **plunged**, so the bore is the "
        "tool diameter and nothing else. **%.1f mm deep** (blind, in a %.0f mm block), "
        "cut right after the four pockets, so the job still needs a single tool change. "
        "Plunge %.0f mm/min, %.1f mm pecks, peck retract %.1f mm.\n"
        % (holes["diameter_mm"], holes["depth_mm"], report["block_height_mm"],
           report["tools"]["T1"]["vert_feed"], holes["peck_depth_mm"],
           holes["retract_height_mm"]))
    add("- **Pins:** turn **%d** of them on the lathe — **Ø%.2f mm × %.0f mm**, steel or "
        "brass, **%.1f × 45° chamfer on both ends**. That is %.1f mm of clearance in "
        "the Ø%.1f mm hole; prove the fit on a scrap hole before turning all four, and "
        "take another 0.05 mm off if it binds. Each pin spans %.1f mm of the two "
        "%.1f mm holes when the halves are closed.\n"
        % (pin["count"], pin["diameter_mm"], pin["length_mm"], pin["chamfer_mm"],
           holes["diameter_mm"] - pin["diameter_mm"], holes["diameter_mm"],
           pin["length_mm"] / 2, holes["depth_mm"]))
    add("\n| pin | strip | XY | to block edge | to saw line | to cavity edge |\n")
    add("|---|---|---|---|---|---|\n")
    worst = max(report["blocks"], key=lambda item: item["diameter_mm"])
    for entry in worst["pin_holes"]["clearances"]:
        add("| %s | %s | (%+.0f, %+.0f) | %.2f mm | %.2f mm | %.2f mm |\n"
            % (entry["name"], entry["strip"], entry["x"], entry["y"],
               entry["to_block_edge_mm"], entry["to_saw_line_mm"],
               entry["to_cavity_edge_mm"]))
    add("\n(Clearances shown for the largest coin, Ø%.0f; the script asserts ≥ %.0f mm to "
        "the block edge and the saw line and ≥ %.0f mm to any cavity, per denomination.)\n"
        % (max(b["diameter_mm"] for b in report["blocks"]), PIN_MIN_EDGE, PIN_MIN_CAVITY))

    add("\n## Zero convention\n")
    add("\n- **XY zero: the centre of the block** (where the saw line Y = 0 meets the "
        "vertical mid-line), **not** the centre of a cavity. Both files of a block "
        "share it.")
    add("\n- **Z zero: the top face of the block** (the flat parting face).")
    add("\n- Safe height %.0f mm, clearance %.0f mm, both above Z0."
        % (report["safe_height_mm"], report["clearance_height_mm"]))
    add("\n- Deepest cut in the **T2** file: Z = %.2f mm (cavity floor %.2f + relief "
        "%.2f); it is checked against Z >= %.2f."
        % (report["z_floor_mm"], -CAVITY_DEPTH, RELIEF, report["z_check_limit_mm"]))
    add("\n- Deepest cut in the **T1** file: Z = %.2f mm, the four pin holes; everything "
        "else in that file stops at the cavity floor, %.2f mm.\n"
        % (-PIN_HOLE_DEPTH, -CAVITY_DEPTH))

    add("\n## Blocks\n\n")
    add("| denomination | coin Ø | block | cavity | cells | files |\n")
    add("|---|---|---|---|---|---|\n")
    for block in report["blocks"]:
        add("| %s zcloud | %.0f mm | %.0f x %.0f x %.0f mm | Ø%.0f x %.1f mm | %s | `gcode/%s/` |\n"
            % (block["value"], block["diameter_mm"], block["block_side_mm"],
               block["block_side_mm"], block["block_height_mm"], block["diameter_mm"],
               CAVITY_DEPTH,
               ", ".join("%s %s" % (cell["name"], cell["face"]) for cell in block["cells"]),
               block["value"]))
    add("\nOne `models/sheet2-<value>-d<Ø>.FCStd` per denomination, plus the STEP and STL ")
    add("of the block.\n")

    add("\n## Tools and feeds (POM / nylon on a 3018)\n\n")
    add("| tool | bit | spindle | feed | plunge | step-down | step-over |\n")
    add("|---|---|---|---|---|---|---|\n")
    t1, t2 = report["tools"]["T1"], report["tools"]["T2"]
    add("| T1 | %s | %.0f rpm | %.0f mm/min | %.0f mm/min | %.1f mm | %.0f%% |\n"
        % (t1["description"], t1["spindle_speed"], t1["horiz_feed"], t1["vert_feed"],
           t1["step_down"], t1["step_over"]))
    add("| T2 | V-bit %.0f° (tip Ø%.1f mm) | %.0f rpm | %.0f mm/min | %.0f mm/min | — | — |\n"
        % (t2["cutting_edge_angle"], t2["tip_diameter"], t2["spindle_speed"],
           t2["horiz_feed"], t2["vert_feed"]))

    add("\n## Run order (one setup, one tool change, per block)\n")
    add("\n1. Fixture the 100 x 100 block. Find the **block centre** and set **X0 Y0** there.")
    add("\n2. Touch off the **top face**: **Z0**.")
    add("\n3. Run `sheet2-<value>-op1-T1-%s.gcode` \u2014 T1 clears all four "
        "cavities to Z-%.2f **and bores the four pin holes to Z-%.2f**, in one file."
        % (T1_TAG, CAVITY_DEPTH, PIN_HOLE_DEPTH))
    add("\n4. Change to the V-bit. **Do not touch X/Y.** Re-zero **Z only** on the same "
        "top face.")
    add("\n5. Run `sheet2-<value>-op2_4-T2-vbit30.gcode` \u2014 relief, pearls and "
        "serration, cell by cell (c1, c2, c3, c4).")
    add("\n6. Deburr, blow out the chips.")
    add("\n7. Saw **once**, along **Y = 0**: two 100 x 50 strips, A (obverse) and B "
        "(reverse).")
    add("\n8. Press the pins into strip A, flip strip B 180\u00b0 about the X axis onto "
        "it and check that the halves seat flat. Release agent, then cast.\n")
    add("\nPosted with the `%s` post processor, arguments `%s` "
        "(GRBL has no canned cycles, so the drilling cycle is expanded to G0/G1).\n"
        % (report["post"], report["post_args"]))

    add("\n## Validation\n\n")
    add("Every file below was re-parsed outside FreeCAD (`cam_gcode_check`): XY inside ")
    add("±%.4f mm (the block half-side minus the tool radius), rapid XY moves only "
        "at or above the safe height, spindle on before the first cut, feed rates "
        "present, one tool number per file. The Z floor is per file: **%.2f mm for T1** "
        "(it bores the pin holes) and **%.2f mm for T2**.\n\n"
        % (SHEET2_SIDE / 2 - t1["diameter"] / 2,
           -(PIN_HOLE_DEPTH + 0.1), report["z_check_limit_mm"]))
    add("On top of that, in the T1 file: a cutting move on **each** of the four cavity "
        "floors at Z-%.2f, a bottom at Z-%.2f within 1 mm of **each** of the four pin "
        "holes, no move at the pin depth anywhere else, and **nothing below Z-%.2f "
        "outside the pin holes**. In the T2 file: %d pearl plunges and %d serration "
        "notches (4 x %d and 4 x %d).\n\n"
        % (CAVITY_DEPTH, PIN_HOLE_DEPTH, CAVITY_DEPTH + 0.1,
           PEARL_COUNT * len(SHEET2_CELLS), SERRATION_COUNT * len(SHEET2_CELLS),
           PEARL_COUNT, SERRATION_COUNT))
    add("| file | lines | Z min | Z max | X extent | Y extent | T | est. time |\n")
    add("|---|---|---|---|---|---|---|---|\n")
    total = 0.0
    for block in report["blocks"]:
        for check in block["gcode"]:
            total += check["estimated_seconds"] or 0.0
            add("| `%s/%s` | %d | %.3f | %.3f | %.2f … %.2f | %.2f … %.2f | %s | %s |\n"
                % (block["value"], check["file"], check["lines"], check["z_min"],
                   check["z_max"], check["x_min"], check["x_max"], check["y_min"],
                   check["y_max"], ", ".join(str(t) for t in check["tools"]),
                   hms(check["estimated_seconds"])))
    add("\n| block | cells reached (floor moves) | pin holes | pearls | serration | job est. |\n")
    add("|---|---|---|---|---|---|\n")
    for block in report["blocks"]:
        add("| %s zcloud | %s | %d/%d at Z-%.2f | %d | %d | %s |\n"
            % (block["value"],
               ", ".join("%s=%d" % (cell["name"], cell["floor_moves"])
                         for cell in block["cells"]),
               len(PIN_HOLES), len(PIN_HOLES), PIN_HOLE_DEPTH,
               block["pearl_plunges"], block["serration_notches"],
               hms(block["job_estimated_seconds"])))
    add("\nTotal estimated cutting time for the %d blocks (= %d coins): **%s** "
        "(feed-based; a 3018 will be slower).\n"
        % (len(report["blocks"]), 2 * len(report["blocks"]), hms(total)))

    add("\n## Honest notes\n")
    add("\n- **Why one file per tool and not per cavity.** The 3018 has no tool changer "
        "and re-zeroing is where mistakes happen: one XY zero for the whole block and "
        "a single Z re-zero at the tool change is the safest sequence. The cost is that "
        "an aborted run loses more work than a per-cavity file would.")
    add("\n- **Saw allowance.** The single saw line (Y = 0) passes 5 mm from the nearest "
        "Ø40 cavity wall and %.2f mm from a pin hole. Keep the parting face flat: the "
        "mold halves close on it, and the pins only centre them, they do not clamp."
        % min(entry["to_saw_line_mm"] for entry in worst["pin_holes"]["clearances"]))
    add("\n- **Pin holes: plunged, not bored.** The hole is Ø%.1f mm because the T1 bit "
        "is Ø%.1f mm and it is simply plunged — no interpolation, so no runout error on "
        "the hole size, and no second tool. A helix was tried in an earlier revision and "
        "dropped: with the tool nearly filling the hole the helix radius is a few "
        "microns, and FreeCAD posts those arcs at the horizontal feed while rapiding the "
        "microns in XY at full depth, which is what cam_gcode_check is there to catch."
        % (PIN_HOLE_DIAMETER, t1["diameter"]))
    add("\n- **Four flutes in plastic.** The %s packs its gullets with POM swarf far "
        "faster than a 2-flute would. Keep the passes shallow — the file already uses "
        "%.1f mm per step-down and %.1f mm pecks — and blow or vacuum the chips out "
        "between passes; a clogged flute rubs, melts the POM and welds a collar to the "
        "tool." % (t1["description"], t1["step_down"], PIN_PECK_DEPTH))
    add("\n- **Pearls.** A 30° V-bit plunged %.2f mm leaves a cone about Ø0.30 mm wide at "
        "the floor, smaller than the master's Ø1.1 mm beads. A Ø1 mm ball nose is the fix "
        "if the dots read too fine." % PEARL_DEPTH)
    add("\n- **Serration.** 130 radial notches on the cavity floor at the wall, %.1f mm "
        "deep, not grooves in the vertical wall: a 2.5-axis machine cannot cut a vertical "
        "wall from above." % SERRATION_DEPTH)
    add("\n- **Relief depth.** V-carving is width-driven: a stroke only reaches the full "
        "%.1f mm when it is at least ~0.69 mm wide. Thin serifs come out shallower. The "
        "op is clamped at Z%.2f." % (RELIEF, report["z_floor_mm"]))
    add("\n- **The STEP/STL** show the block, the four cavities, the four pin holes and "
        "the pearl dimples. The "
        "relief and the serration exist as toolpaths only — their machined shape is "
        "defined by the V-bit, not by a modelled pocket.\n")
    return "".join(lines)


try:
    main()
except Exception:
    traceback.print_exc()
    sys.stderr.write("[build_molds] FAILED\n")
    sys.exit(1)
