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
# cavity floor. The reverse face is mirrored in X so the cast half reads
# correctly; the obverse is not.
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
OUT_DIR = os.path.expanduser("~/Projetos/edimar/my-ideas/eletronica/moeda-zcloud-cnc")
SVG_DIR = os.path.join(_HERE, "svg")
REPO_OUT = os.path.join(_HERE, "out")

DENOMINATIONS = [
    {"value": "1", "diameter": 34.0},
    {"value": "5", "diameter": 37.0},
    {"value": "10", "diameter": 40.0},
]
FACES = ["anverso", "reverso"]

MOLD_MARGIN = 8.0          # molde_margem
BLOCK_HEIGHT = 8.0         # molde_alt
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

# 3018 router, POM/nylon.
T1 = {
    "number": 1, "shape": "endmill", "label": "T1 endmill 3.175",
    "diameter": 3.175, "flutes": 2,
    "horiz_feed": 600.0, "vert_feed": 200.0,
    "horiz_rapid": 2000.0, "vert_rapid": 800.0, "spindle_speed": 10000.0,
    "step_down": 0.6, "step_over": 40.0,
}
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
        side, side, BLOCK_HEIGHT, App.Vector(-side / 2, -side / 2, -BLOCK_HEIGHT))
    cuts = [Part.makeCylinder(diameter / 2, CAVITY_DEPTH, App.Vector(0, 0, -CAVITY_DEPTH))]
    # A 30 degree V-bit plunged 0.55 mm leaves a cone of this radius at the floor.
    dimple_radius = PEARL_DEPTH * math.tan(math.radians(15.0))
    for index in range(PEARL_COUNT):
        angle = 2 * math.pi * index / PEARL_COUNT
        centre = pearl_centre(diameter, index)
        cuts.append(Part.makeCone(
            dimple_radius, 0.0, PEARL_DEPTH,
            App.Vector(centre[0], centre[1], -CAVITY_DEPTH - PEARL_DEPTH)))
        del angle
    solid = block.cut(Part.Compound(cuts))
    if not solid.isValid():
        raise RuntimeError("the mold block solid is not valid")
    return solid


def pearl_centre(diameter, index):
    radius = diameter / 2 - PEARL_INSET
    angle = 2 * math.pi * index / PEARL_COUNT
    return (radius * math.cos(angle), radius * math.sin(angle))


def serration_edges(diameter):
    """130 short radial segments ending at the cavity wall, on the floor plane."""
    edges = []
    outer = diameter / 2
    inner = outer - SERRATION_LENGTH
    for index in range(SERRATION_COUNT):
        angle = 2 * math.pi * index / SERRATION_COUNT
        cos_a, sin_a = math.cos(angle), math.sin(angle)
        edges.append(Part.makeLine(
            App.Vector(inner * cos_a, inner * sin_a, -CAVITY_DEPTH),
            App.Vector(outer * cos_a, outer * sin_a, -CAVITY_DEPTH)))
    return edges


def import_face_shape(doc, svg_path, mirror_x):
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
    if mirror_x:
        shape = shape.mirror(App.Vector(0, 0, 0), App.Vector(1, 0, 0))
    shape.translate(App.Vector(0, 0, -CAVITY_DEPTH))
    return shape


def cavity_floor_face(clone):
    """Index (1-based) of the flat cavity floor on the job's model clone."""
    best_index, best_area = None, 0.0
    for index, face in enumerate(clone.Shape.Faces):
        box = face.BoundBox
        if abs(box.ZMax + CAVITY_DEPTH) < 1e-6 and box.ZLength < 1e-6 and face.Area > best_area:
            best_area, best_index = face.Area, index + 1
    if best_index is None:
        raise RuntimeError("could not find the cavity floor face at Z=%.3f" % -CAVITY_DEPTH)
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
        mirror_x=(face_name == "reverso"))
    box = relief.Shape.BoundBox
    log("relief x=[%.2f, %.2f] y=[%.2f, %.2f] z=%.2f faces=%d%s" % (
        box.XMin, box.XMax, box.YMin, box.YMax, box.ZMax, len(relief.Shape.Faces),
        " (mirrored in X)" if face_name == "reverso" else ""))
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
                 **{k: v for k, v in T1.items() if k not in ("shape", "step_down", "step_over")})
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

    files = ["%s-%s-op1-T1-endmill3175.gcode" % (face_name, value),
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
    for directory in (OUT_DIR, REPO_OUT):
        with open(os.path.join(directory, "validation.json"), "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2)
    readme = render_readme(report)
    for directory in (OUT_DIR, REPO_OUT):
        with open(os.path.join(directory, "README.md"), "w", encoding="utf-8") as handle:
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
               block["block_side_mm"], block["block_side_mm"], BLOCK_HEIGHT,
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
    add("\n3. Run `<face>-<value>-op1-T1-endmill3175.gcode` (T1 clears the Ø d cavity to Z-1.80).")
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


try:
    main()
except Exception:
    traceback.print_exc()
    sys.stderr.write("[build_molds] FAILED\n")
    sys.exit(1)
