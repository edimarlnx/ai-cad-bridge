"""CAM tools: job, tools, operations, inspection, post-processing and G-code checking.

The CAM (Path) workbench is the bridge's most important output path: a model is
only "done" when a machine can cut it. These tools cover the whole chain and,
crucially, the *validation* end of it — ``cam_inspect`` reads the toolpaths back
and ``cam_gcode_check`` re-parses the posted G-code without FreeCAD, so a claim
about depths and bounds is a measurement, not an intention.

Three FreeCAD 1.1.3 landmines are handled here so callers never meet them:

* ``PathScripts.PathUtils.findToolController`` raises ``UnboundLocalError`` when
  a job has more than one tool controller and no GUI is attached. Operation
  creation therefore runs inside ``_using_tool_controller``, which injects a
  headless stand-in for the GUI's picker.
* every operation is created with SetupSheet *expressions* bound to
  ``StartDepth``, ``FinalDepth``, ``SafeHeight``, ``ClearanceHeight`` and
  ``StepDown``. Assigning those properties silently has no effect: the next
  recompute re-evaluates the expression. ``cam_op_add`` clears the expression of
  every property it writes.
* ``Path.Op.Vcarve`` takes its top surface from
  ``BaseShapes[0].Shape.BoundBox.ZMax``, not from ``StartDepth``. The faces must
  be positioned at the surface the carving starts from.
"""

import contextlib
import importlib
import math
import os
import re

from .common import (
    ToolError,
    expand_path,
    guard_overwrite,
    number,
    resolve_doc,
    resolve_object,
)

# Operation kind -> module under ``Path.Op``. Every one of these was confirmed
# importable headless in FreeCAD 1.1.3 (Flatpak).
OP_MODULES = {
    "profile": "Profile",
    "pocket": "Pocket",
    "pocket_shape": "PocketShape",
    "drilling": "Drilling",
    "engrave": "Engrave",
    "vcarve": "Vcarve",
    "adaptive": "Adaptive",
    "surface": "Surface",
    "millface": "MillFace",
    "helix": "Helix",
    "slot": "Slot",
    "deburr": "Deburr",
}

# Properties that carry a SetupSheet expression on a freshly created operation.
EXPRESSION_BOUND = (
    "StartDepth",
    "FinalDepth",
    "SafeHeight",
    "ClearanceHeight",
    "StepDown",
    "RetractHeight",
    "PeckDepth",
    "ExtensionLengthDefault",
)

SPLIT_ORDER = {"tool": "Tool", "operation": "Operation", "fixture": "Fixture"}


class _ToolControllerChoice:
    """Headless stand-in for the CAM GUI's tool-controller picker."""

    def __init__(self, controller):
        self.controller = controller

    def selectedToolController(self):
        return self.controller

    def chooseToolController(self, controllers):
        return self.controller


@contextlib.contextmanager
def _using_tool_controller(controller):
    """Make operation creation pick ``controller`` instead of raising headless."""
    import PathScripts.PathUtils as PathUtils

    previous = getattr(PathUtils, "UserInput", None)
    PathUtils.UserInput = _ToolControllerChoice(controller)
    try:
        yield
    finally:
        PathUtils.UserInput = previous


def _resolve_job(doc, name=None):
    """Find a CAM job by name/label, or the only one in the document."""
    jobs = [obj for obj in doc.Objects if obj.Name.startswith("Job") or
            getattr(obj, "Proxy", None).__class__.__name__ == "ObjectJob"]
    jobs = [obj for obj in jobs if hasattr(obj, "Operations") and hasattr(obj, "Tools")]
    if name:
        for job in jobs:
            if job.Name == name or job.Label == name:
                return job
        raise ToolError("no CAM job named %r (jobs: %s)"
                        % (name, ", ".join(job.Name for job in jobs) or "none"))
    if not jobs:
        raise ToolError("this document has no CAM job; call cam_job_create first")
    if len(jobs) > 1:
        raise ToolError("several CAM jobs (%s); pass job explicitly"
                        % ", ".join(job.Name for job in jobs))
    return jobs[0]


def _controller(job, name):
    """Find a tool controller in the job by Name, Label or tool number."""
    controllers = list(job.Tools.Group)
    if name is None:
        if not controllers:
            raise ToolError("job %s has no tool controller" % job.Name)
        return controllers[0]
    for controller in controllers:
        if controller.Name == name or controller.Label == name:
            return controller
    if isinstance(name, int) or (isinstance(name, str) and name.isdigit()):
        wanted = int(name)
        for controller in controllers:
            if controller.ToolNumber == wanted:
                return controller
    raise ToolError("no tool controller %r in %s (have: %s)"
                    % (name, job.Name, ", ".join(c.Label for c in controllers)))


def _path_summary(op):
    """Command count, Z range and XY extents of one operation's toolpath."""
    path = getattr(op, "Path", None)
    if path is None or not path.Commands:
        return {"commands": 0}
    box = path.BoundBox
    return {
        "commands": len(path.Commands),
        "z_min": number(box.ZMin, 4),
        "z_max": number(box.ZMax, 4),
        "x_min": number(box.XMin, 4),
        "x_max": number(box.XMax, 4),
        "y_min": number(box.YMin, 4),
        "y_max": number(box.YMax, 4),
    }


def _clear_expressions(op, names):
    """Drop SetupSheet expressions so plain assignments actually stick."""
    cleared = []
    bound = {name for name, _expr in (op.ExpressionEngine or [])}
    for name in names:
        if name in bound:
            try:
                op.setExpression(name, None)
                cleared.append(name)
            except Exception:
                pass
    return cleared


def _assign(obj, prop, value):
    """Set a FreeCAD property, healing the int/float mismatch of Percent props."""
    try:
        setattr(obj, prop, value)
        return
    except TypeError as exc:
        if isinstance(value, float) and value.is_integer():
            try:
                setattr(obj, prop, int(value))
                return
            except Exception:
                pass
        raise ToolError("cannot set %s.%s to %r: %s" % (obj.Name, prop, value, exc))


def _job_create(App, Gui, params):
    doc = resolve_doc(App, params)
    names = params.get("models") or []
    if not names:
        raise ToolError("models is required (a list of solid object names)")
    models = [resolve_object(doc, name) for name in names]

    from Path.Main import Job as PathJob

    job = PathJob.Create(params.get("name") or "Job", models)
    doc.recompute()

    if params.get("stock_to_model", True):
        for prop in ("ExtXneg", "ExtXpos", "ExtYneg", "ExtYpos", "ExtZneg", "ExtZpos"):
            if prop in job.Stock.PropertiesList:
                setattr(job.Stock, prop, 0)
    if params.get("post"):
        job.PostProcessor = params["post"]
    if params.get("post_args"):
        job.PostProcessorArgs = params["post_args"]
    if params.get("geometry_tolerance") is not None:
        job.GeometryTolerance = float(params["geometry_tolerance"])
    doc.recompute()

    box = job.Stock.Shape.BoundBox
    return {
        "job": job.Name,
        "label": job.Label,
        "document": doc.Name,
        "models": [obj.Name for obj in job.Model.Group],
        "stock": {
            "name": job.Stock.Name,
            "x_min": number(box.XMin, 4), "x_max": number(box.XMax, 4),
            "y_min": number(box.YMin, 4), "y_max": number(box.YMax, 4),
            "z_min": number(box.ZMin, 4), "z_max": number(box.ZMax, 4),
        },
        "tools": [{"name": tc.Name, "label": tc.Label, "number": tc.ToolNumber}
                  for tc in job.Tools.Group],
        "post": job.PostProcessor or None,
    }


def _tool_add(App, Gui, params):
    doc = resolve_doc(App, params)
    job = _resolve_job(doc, params.get("job"))

    reuse = params.get("reuse")
    if reuse:
        controller = _controller(job, reuse)
        bit = controller.Tool
    else:
        from Path.Tool.toolbit import ToolBit
        import Path.Tool.Controller as Controller

        shape = params.get("shape") or "endmill"
        try:
            bit = ToolBit.from_shape_id(shape).attach_to_doc(doc=doc)
        except Exception as exc:
            raise ToolError("unknown tool bit shape %r: %s" % (shape, exc))
        controller = Controller.Create(
            params.get("label") or "TC", tool=bit, toolNumber=int(params.get("number") or 1)
        )
        job.Tools.addObject(controller)

    for prop, key in (
        ("Diameter", "diameter"),
        ("CuttingEdgeAngle", "cutting_edge_angle"),
        ("TipDiameter", "tip_diameter"),
        ("CuttingEdgeHeight", "cutting_edge_height"),
        ("Length", "length"),
        ("ShankDiameter", "shank_diameter"),
        ("Flutes", "flutes"),
    ):
        value = params.get(key)
        if value is not None and prop in bit.PropertiesList:
            _assign(bit, prop, value)
    if params.get("bit_label"):
        bit.Label = params["bit_label"]

    if params.get("label"):
        controller.Label = params["label"]
    if params.get("number") is not None:
        controller.ToolNumber = int(params["number"])
    for prop, key in (
        ("HorizFeed", "horiz_feed"),
        ("VertFeed", "vert_feed"),
        ("HorizRapid", "horiz_rapid"),
        ("VertRapid", "vert_rapid"),
    ):
        value = params.get(key)
        if value is not None:
            # Velocity properties are mm/s internally: a bare 600 would post as
            # F36000. Feeds are always given here in mm/min, the CNC convention.
            _assign(controller, prop, "%s mm/min" % float(value))
    if params.get("spindle_speed") is not None:
        _assign(controller, "SpindleSpeed", float(params["spindle_speed"]))
    doc.recompute()

    return {
        "job": job.Name,
        "controller": controller.Name,
        "label": controller.Label,
        "number": controller.ToolNumber,
        "bit": {
            "label": bit.Label,
            "shape": getattr(bit, "ShapeType", None),
            "diameter_mm": number(getattr(bit, "Diameter", None) and bit.Diameter.Value),
            "cutting_edge_angle_deg": (
                number(bit.CuttingEdgeAngle.Value)
                if "CuttingEdgeAngle" in bit.PropertiesList else None
            ),
            "tip_diameter_mm": (
                number(bit.TipDiameter.Value)
                if "TipDiameter" in bit.PropertiesList else None
            ),
        },
        "feeds": {
            "horiz_mm_min": number(controller.HorizFeed.getValueAs("mm/min"), 3),
            "vert_mm_min": number(controller.VertFeed.getValueAs("mm/min"), 3),
            "spindle_rpm": number(controller.SpindleSpeed),
        },
    }


def _op_add(App, Gui, params):
    doc = resolve_doc(App, params)
    job = _resolve_job(doc, params.get("job"))
    kind = (params.get("type") or "").strip().lower()
    if kind not in OP_MODULES:
        raise ToolError("unknown operation type %r; use one of %s"
                        % (kind, ", ".join(sorted(OP_MODULES))))
    controller = _controller(job, params.get("tool"))
    module = importlib.import_module("Path.Op.%s" % OP_MODULES[kind])

    name = params.get("name") or ("op_%s" % kind)
    # parentJob removes any ambiguity when the document holds several jobs.
    with _using_tool_controller(controller):
        try:
            op = module.Create(name, parentJob=job)
        except TypeError:
            op = module.Create(name)
    op.ToolController = controller

    # Geometry, before the depths: assigning Base re-runs updateDepths().
    base = params.get("base") or []
    links = []
    for entry in base:
        obj = resolve_object(doc, entry.get("object"))
        links.append((obj, list(entry.get("subs") or [])))
    if base or params.get("locations"):
        # An empty assignment matters: Drilling pre-fills Base with every
        # drillable feature it can find in the model (for a mold block that is
        # the cavity itself), which would add a hole nobody asked for on top of
        # the explicit locations.
        op.Base = links
    if params.get("base_shapes"):
        op.BaseShapes = [resolve_object(doc, item) for item in params["base_shapes"]]
    if params.get("locations"):
        vectors = []
        for point in params["locations"]:
            if isinstance(point, dict):
                vectors.append(App.Vector(point.get("x", 0), point.get("y", 0), point.get("z", 0)))
            else:
                values = list(point) + [0, 0, 0]
                vectors.append(App.Vector(values[0], values[1], values[2]))
        op.Locations = vectors
    doc.recompute()

    # Now the numbers. Expressions must go first or the values do not stick.
    properties = dict(params.get("properties") or {})
    cleared = _clear_expressions(op, set(properties) | set(EXPRESSION_BOUND))
    unknown = []
    for prop, value in properties.items():
        if prop not in op.PropertiesList:
            unknown.append(prop)
            continue
        _assign(op, prop, value)
    if unknown:
        raise ToolError("operation %s (%s) has no properties: %s (available: %s)"
                        % (name, kind, ", ".join(sorted(unknown)),
                           ", ".join(sorted(op.PropertiesList))))
    doc.recompute()

    result = {
        "job": job.Name,
        "operation": op.Name,
        "label": op.Label,
        "type": kind,
        "tool": controller.Label,
        "tool_number": controller.ToolNumber,
        "state": list(op.State),
        "cleared_expressions": cleared,
        "cycle_time": str(getattr(op, "CycleTime", "")) or None,
    }
    result.update(_path_summary(op))
    for prop in ("StartDepth", "FinalDepth", "StepDown", "SafeHeight", "ClearanceHeight"):
        if prop in op.PropertiesList:
            try:
                result[prop] = number(getattr(op, prop).Value, 4)
            except AttributeError:
                result[prop] = number(getattr(op, prop), 4)
    if not result.get("commands"):
        result["warning"] = (
            "the operation produced no toolpath: check Base/BaseShapes geometry, the "
            "tool diameter against the feature size, and the depth range"
        )
    return result


def _path_length_and_time(op, controller):
    """Cut length (mm) and a feed-based time estimate (s) for one operation."""
    path = getattr(op, "Path", None)
    if path is None or not path.Commands:
        return 0.0, 0.0
    horiz = float(controller.HorizFeed.getValueAs("mm/min")) or 600.0
    vert = float(controller.VertFeed.getValueAs("mm/min")) or 200.0
    rapid = float(controller.HorizRapid.getValueAs("mm/min")) or 2000.0
    position = [0.0, 0.0, 0.0]
    length = 0.0
    seconds = 0.0
    for command in path.Commands:
        gcode = command.Name.upper()
        target = list(position)
        for index, axis in enumerate("XYZ"):
            if axis in command.Parameters:
                target[index] = float(command.Parameters[axis])
        step = math.dist(position, target)
        if gcode in ("G0", "G00"):
            seconds += step / rapid * 60.0 if rapid else 0.0
        elif gcode in ("G1", "G01", "G2", "G02", "G3", "G03"):
            # Path commands carry F in FreeCAD's internal mm/s.
            feed = float(command.Parameters.get("F") or 0) * 60.0 or (
                vert if (target[0] == position[0] and target[1] == position[1]) else horiz
            )
            length += step
            seconds += step / feed * 60.0 if feed else 0.0
        position = target
    return length, seconds


def _inspect(App, Gui, params):
    doc = resolve_doc(App, params)
    job = _resolve_job(doc, params.get("job"))
    stock_box = job.Stock.Shape.BoundBox

    operations = []
    issues = []
    total_seconds = 0.0
    for op in job.Operations.Group:
        controller = op.ToolController
        length, seconds = _path_length_and_time(op, controller)
        total_seconds += seconds
        entry = {
            "operation": op.Name,
            "label": op.Label,
            "active": bool(getattr(op, "Active", True)),
            "tool": controller.Label if controller else None,
            "tool_number": controller.ToolNumber if controller else None,
            "state": list(op.State),
            "cycle_time": str(getattr(op, "CycleTime", "")) or None,
            "cut_length_mm": number(length, 2),
            "estimated_seconds": number(seconds, 1),
        }
        entry.update(_path_summary(op))
        operations.append(entry)

        if "Up-to-date" not in op.State:
            issues.append("%s is not up to date (%s)" % (op.Name, ", ".join(op.State)))
        if not entry.get("commands"):
            issues.append("%s produced no toolpath" % op.Name)
            continue
        if entry["z_min"] < stock_box.ZMin - 1e-6:
            issues.append("%s cuts below the stock (z_min %.3f < %.3f)"
                          % (op.Name, entry["z_min"], stock_box.ZMin))
        for axis, low, high in (("x", stock_box.XMin, stock_box.XMax),
                                ("y", stock_box.YMin, stock_box.YMax)):
            if entry["%s_min" % axis] < low - 1e-6 or entry["%s_max" % axis] > high + 1e-6:
                issues.append("%s leaves the stock in %s (%.3f..%.3f vs %.3f..%.3f)"
                              % (op.Name, axis.upper(), entry["%s_min" % axis],
                                 entry["%s_max" % axis], low, high))

    return {
        "ok": not issues,
        "job": job.Name,
        "document": doc.Name,
        "post": job.PostProcessor or None,
        "job_cycle_time": str(job.CycleTime) or None,
        "estimated_seconds": number(total_seconds, 1),
        "stock": {
            "x_min": number(stock_box.XMin, 4), "x_max": number(stock_box.XMax, 4),
            "y_min": number(stock_box.YMin, 4), "y_max": number(stock_box.YMax, 4),
            "z_min": number(stock_box.ZMin, 4), "z_max": number(stock_box.ZMax, 4),
        },
        "tools": [{"name": tc.Name, "label": tc.Label, "number": tc.ToolNumber,
                   "diameter_mm": number(tc.Tool.Diameter.Value) if tc.Tool else None,
                   "horiz_feed_mm_min": number(tc.HorizFeed.getValueAs("mm/min"), 3),
                   "vert_feed_mm_min": number(tc.VertFeed.getValueAs("mm/min"), 3),
                   "spindle_rpm": number(tc.SpindleSpeed)}
                  for tc in job.Tools.Group],
        "operations": operations,
        "issues": issues,
    }


def _postprocess(App, Gui, params):
    doc = resolve_doc(App, params)
    job = _resolve_job(doc, params.get("job"))
    post_name = params.get("post") or job.PostProcessor or "grbl"

    split_by = (params.get("split_by") or "tool").strip().lower()
    if split_by not in SPLIT_ORDER and split_by != "none":
        raise ToolError("split_by must be tool, operation, fixture or none")
    job.PostProcessor = post_name
    if params.get("post_args") is not None:
        job.PostProcessorArgs = params["post_args"]
    job.SplitOutput = split_by != "none"
    if split_by != "none":
        job.OrderOutputBy = SPLIT_ORDER[split_by]

    output_dir = expand_path(params.get("output_dir") or "~/.cache/freecad-ai-bridge")
    os.makedirs(output_dir, exist_ok=True)
    job.PostProcessorOutputFile = os.path.join(
        output_dir, "%s.gcode" % (params.get("prefix") or job.Name))
    doc.recompute()

    import Path.Post.Processor as PostProcessor

    try:
        post = PostProcessor.PostProcessorFactory.get_post_processor(job, post_name)
    except Exception as exc:
        raise ToolError("no post processor %r: %s" % (post_name, exc))
    sections = post.export()
    if not sections:
        raise ToolError("the post processor returned nothing; are the operations empty?")

    names = params.get("files") or []
    overwrite = bool(params.get("overwrite"))
    prefix = params.get("prefix") or job.Name
    written = []
    for index, (section, text) in enumerate(sections):
        if index < len(names):
            filename = names[index]
        elif len(sections) == 1:
            filename = "%s.gcode" % prefix
        else:
            filename = "%s-%s.gcode" % (prefix, section)
        if not os.path.splitext(filename)[1]:
            filename += ".gcode"
        path = os.path.join(output_dir, filename)
        guard_overwrite(path, overwrite)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        written.append({
            "section": section,
            "path": path,
            "lines": text.count("\n"),
            "bytes": len(text.encode("utf-8")),
        })

    return {
        "ok": True,
        "job": job.Name,
        "post": post_name,
        "post_args": job.PostProcessorArgs or None,
        "split_by": split_by,
        "files": written,
    }


# --- G-code checking --------------------------------------------------------
# Deliberately a pure parser: no FreeCAD involved, so it also validates G-code
# the bridge did not produce and can be unit-tested anywhere.

_WORD = re.compile(r"([A-Za-z])\s*([-+]?[0-9]*\.?[0-9]+)")
_RAPIDS = {"G0", "G00"}
_FEEDS = {"G1", "G01", "G2", "G02", "G3", "G03"}
_DRILL_CYCLES = {"G81", "G82", "G83", "G73", "G85"}
_COMMENT_TOOL = re.compile(r"\bT\s*(\d+)\b")


def _strip_comments(line):
    """Split a line into (code, comment text); ``(...)`` and ``;`` are comments."""
    code = []
    comment = []
    depth = 0
    for char in line:
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        elif char == ";" and depth == 0:
            comment.append(line[line.index(char) + 1:])
            break
        elif depth == 0:
            code.append(char)
        else:
            comment.append(char)
    return "".join(code), "".join(comment)


def _arc_extents(start, end, centre, clockwise):
    """XY extremes swept by a G2/G3 arc, not just its end points.

    An arc from (-2.1, 15.3) to (2.1, -15.3) passes through X = +/-15.4; taking
    the end points alone would under-report the machine envelope by 13 mm.
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

    points = [start[:2], end[:2]]
    for quarter in range(4):
        angle = quarter * math.pi / 2.0
        delta = angle - start_angle
        if clockwise:
            while delta > 0:
                delta -= 2 * math.pi
            inside = delta >= sweep
        else:
            while delta < 0:
                delta += 2 * math.pi
            inside = delta <= sweep
        if inside:
            points.append((centre[0] + radius * math.cos(angle),
                           centre[1] + radius * math.sin(angle)))
    length = abs(sweep) * radius
    return points, length


def _gcode_check(App, Gui, params):
    text = params.get("text")
    path = params.get("path")
    if not text:
        if not path:
            raise ToolError("pass text or path")
        path = expand_path(path)
        if not os.path.exists(path):
            raise ToolError("no such G-code file: %s" % path)
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read()

    bounds = params.get("bounds") or {}
    z_floor = params.get("z_floor")
    safe_height = params.get("safe_height")
    rapid_rate = float(params.get("rapid_rate") or 2000.0)
    expected_tools = params.get("tools")

    position = {"X": 0.0, "Y": 0.0, "Z": 0.0}
    seen_position = {"X": False, "Y": False, "Z": False}
    commented_tools = []
    extents = {axis: [None, None] for axis in "XYZ"}
    motion = None
    feed = None
    spindle_on = False
    spindle_before_cut = None
    tools = []
    counts = {"lines": 0, "rapid": 0, "feed": 0, "arc": 0, "drill_cycles": 0, "plunges": 0}
    seconds = 0.0
    issues = []
    unsafe_rapids = []
    no_feed_moves = 0

    for index, raw in enumerate(text.splitlines(), start=1):
        counts["lines"] += 1
        line, comment = _strip_comments(raw)
        line = line.strip()
        for match in _COMMENT_TOOL.finditer(comment):
            value = int(match.group(1))
            if value not in commented_tools:
                commented_tools.append(value)
        if not line:
            continue
        words = _WORD.findall(line)
        if not words:
            continue
        codes = ["%s%s" % (letter.upper(), _fmt_code(value))
                 for letter, value in words if letter.upper() in ("G", "M", "T")]
        target = dict(position)
        moved = set()
        offsets = {}
        for letter, value in words:
            letter = letter.upper()
            if letter in ("X", "Y", "Z"):
                target[letter] = float(value)
                moved.add(letter)
                seen_position[letter] = True
            elif letter == "F":
                feed = float(value)
            elif letter in ("I", "J"):
                offsets[letter] = float(value)
            elif letter == "S":
                pass
            elif letter == "T":
                number_ = int(float(value))
                if number_ not in tools:
                    tools.append(number_)

        for code in codes:
            if code in ("M3", "M03", "M4", "M04"):
                spindle_on = True
            elif code in ("M5", "M05"):
                spindle_on = False
            elif code in _RAPIDS or code in _FEEDS or code in _DRILL_CYCLES:
                motion = code

        drill = next((code for code in codes if code in _DRILL_CYCLES), None)
        if drill:
            counts["drill_cycles"] += 1
            issues.append(
                "line %d: canned drill cycle %s — GRBL does not implement canned "
                "cycles; post with --translate_drill" % (index, drill))

        if not moved:
            continue
        active = next((code for code in codes if code in _RAPIDS or code in _FEEDS), motion)
        distance = math.dist(
            (position["X"], position["Y"], position["Z"]),
            (target["X"], target["Y"], target["Z"]),
        )

        swept = [(target["X"], target["Y"])]
        if active in _RAPIDS:
            counts["rapid"] += 1
            seconds += distance / rapid_rate * 60.0 if rapid_rate else 0.0
            if safe_height is not None and ("X" in moved or "Y" in moved):
                travel_z = min(position["Z"], target["Z"]) if seen_position["Z"] else target["Z"]
                if travel_z < float(safe_height) - 1e-6:
                    unsafe_rapids.append((index, round(travel_z, 3)))
        elif active in _FEEDS:
            if active in ("G2", "G02", "G3", "G03"):
                counts["arc"] += 1
                centre = (position["X"] + offsets.get("I", 0.0),
                          position["Y"] + offsets.get("J", 0.0))
                swept, arc_length = _arc_extents(
                    (position["X"], position["Y"]), (target["X"], target["Y"]),
                    centre, active in ("G2", "G02"))
                distance = math.hypot(arc_length, target["Z"] - position["Z"])
            else:
                counts["feed"] += 1
            if moved == {"Z"} and target["Z"] < position["Z"]:
                counts["plunges"] += 1
            if feed is None:
                no_feed_moves += 1
            else:
                seconds += distance / feed * 60.0 if feed else 0.0
            # The first cutting move is the hazard, wherever Z0 happens to be:
            # a job zeroed on the top of the stock never goes below Z0 at all.
            if spindle_before_cut is None:
                spindle_before_cut = spindle_on

        for point in swept:
            for axis, value in (("X", point[0]), ("Y", point[1])):
                low, high = extents[axis]
                extents[axis] = [value if low is None else min(low, value),
                                 value if high is None else max(high, value)]
        low, high = extents["Z"]
        value = target["Z"]
        extents["Z"] = [value if low is None else min(low, value),
                        value if high is None else max(high, value)]
        position = target

    if z_floor is not None and extents["Z"][0] is not None:
        if extents["Z"][0] < float(z_floor) - 1e-6:
            issues.append("cuts to Z %.4f, below the allowed floor %.4f"
                          % (extents["Z"][0], float(z_floor)))
    for axis, keys in (("X", ("x_min", "x_max")), ("Y", ("y_min", "y_max"))):
        low, high = extents[axis]
        if low is None:
            continue
        if bounds.get(keys[0]) is not None and low < float(bounds[keys[0]]) - 1e-6:
            issues.append("%s reaches %.4f, outside the allowed %.4f"
                          % (axis, low, float(bounds[keys[0]])))
        if bounds.get(keys[1]) is not None and high > float(bounds[keys[1]]) + 1e-6:
            issues.append("%s reaches %.4f, outside the allowed %.4f"
                          % (axis, high, float(bounds[keys[1]])))
    if unsafe_rapids:
        head = ", ".join("line %d at Z %.3f" % item for item in unsafe_rapids[:5])
        issues.append("%d rapid XY move(s) below the safe height %.3f: %s"
                      % (len(unsafe_rapids), float(safe_height), head))
    if spindle_before_cut is False:
        issues.append("the first cutting move happens with the spindle off (no M3 before it)")
    if spindle_before_cut is None:
        issues.append("the file contains no cutting move (G1/G2/G3) at all")
    if no_feed_moves:
        issues.append("%d feed move(s) before any F word" % no_feed_moves)
    # The grbl post comments the tool change out — GRBL has no tool changer —
    # so a split-per-tool file carries its number only as "( M6 T1 )".
    tool_source = "code"
    if not tools and commented_tools:
        tools = commented_tools
        tool_source = "comment"
    if not tools:
        issues.append("no tool number anywhere in the file, not even in a comment")
    if expected_tools is not None:
        wanted = sorted(int(item) for item in expected_tools)
        if sorted(tools) != wanted:
            issues.append("tool numbers %s, expected %s" % (sorted(tools), wanted))

    return {
        "ok": not issues,
        "path": path,
        "lines": counts["lines"],
        "moves": {"rapid": counts["rapid"], "feed": counts["feed"], "arc": counts["arc"],
                  "plunges": counts["plunges"], "drill_cycles": counts["drill_cycles"]},
        "x_min": number(extents["X"][0], 4), "x_max": number(extents["X"][1], 4),
        "y_min": number(extents["Y"][0], 4), "y_max": number(extents["Y"][1], 4),
        "z_min": number(extents["Z"][0], 4), "z_max": number(extents["Z"][1], 4),
        "tools": tools,
        "tool_source": tool_source,
        "spindle_on_before_first_cut": spindle_before_cut,
        "estimated_seconds": number(seconds, 1),
        "issues": issues,
    }


def _fmt_code(value):
    """``G01`` and ``G1`` must compare equal; normalise the numeric part."""
    text = value.lstrip("0") or "0"
    return text


_DOC_PROPERTY = {
    "type": "string",
    "description": "Document name; defaults to the active document.",
}
_JOB_PROPERTY = {
    "type": "string",
    "description": "CAM job name or label; optional when the document has only one.",
}

TOOLS = [
    {
        "name": "cam_job_create",
        "description": (
            "Create a CAM (Path) job on one or more solids: stock from the model "
            "bounding box, a SetupSheet, an Operations group and a default 5 mm "
            "endmill controller. Set stock_to_model false to keep the default "
            "stock margins."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "models": {"type": "array", "items": {"type": "string"},
                           "description": "Object names of the solids to machine."},
                "name": {"type": "string", "description": "Job name (default 'Job')."},
                "post": {"type": "string", "description": "Post processor, e.g. 'grbl'."},
                "post_args": {"type": "string",
                              "description": "Post arguments, e.g. '--translate_drill'."},
                "stock_to_model": {"type": "boolean",
                                   "description": "Zero the stock margins (default true)."},
                "geometry_tolerance": {"type": "number"},
                "doc": _DOC_PROPERTY,
            },
            "required": ["models"],
            "additionalProperties": False,
        },
        "mutating": True,
        "handler": _job_create,
    },
    {
        "name": "cam_tool_add",
        "description": (
            "Add a tool controller to a job (or edit an existing one with 'reuse'): "
            "bit shape (endmill, v-bit, ballend, bullnose, drill, chamfer...), "
            "geometry and real feeds/speeds. GRBL has no tool changer, so plan on "
            "one posted file per tool number."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "job": _JOB_PROPERTY,
                "reuse": {"type": "string",
                          "description": "Edit this existing controller instead of creating one."},
                "shape": {"type": "string",
                          "description": "Tool bit shape id (default 'endmill'; 'v-bit' for V)."},
                "label": {"type": "string"},
                "bit_label": {"type": "string"},
                "number": {"type": "integer", "description": "Tool number (T word)."},
                "diameter": {"type": "number"},
                "cutting_edge_angle": {"type": "number", "description": "V-bit full angle, degrees."},
                "tip_diameter": {"type": "number"},
                "cutting_edge_height": {"type": "number"},
                "length": {"type": "number"},
                "shank_diameter": {"type": "number"},
                "flutes": {"type": "integer"},
                "horiz_feed": {"type": "number", "description": "mm/min."},
                "vert_feed": {"type": "number", "description": "mm/min (plunge)."},
                "horiz_rapid": {"type": "number"},
                "vert_rapid": {"type": "number"},
                "spindle_speed": {"type": "number", "description": "rpm."},
                "doc": _DOC_PROPERTY,
            },
            "additionalProperties": False,
        },
        "mutating": True,
        "handler": _tool_add,
    },
    {
        "name": "cam_op_add",
        "description": (
            "Add an operation to a job (pocket_shape, pocket, profile, vcarve, "
            "drilling, engrave, adaptive, surface, millface, helix, slot, deburr). "
            "'base' limits it to geometry ([{object, subs:['Face3','Edge7']}]), "
            "'base_shapes' feeds whole objects (vcarve takes its faces this way and "
            "starts from their ZMax), 'locations' gives drilling explicit points. "
            "'properties' sets any operation property (StartDepth, FinalDepth, "
            "StepDown, StepOver, SafeHeight, ClearanceHeight, ...); the tool first "
            "clears the SetupSheet expression bound to it, without which the value "
            "would be silently reverted on the next recompute."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "job": _JOB_PROPERTY,
                "type": {"type": "string", "enum": sorted(OP_MODULES)},
                "name": {"type": "string"},
                "tool": {"type": "string",
                         "description": "Tool controller name, label or number."},
                "base": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "object": {"type": "string"},
                            "subs": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["object"],
                    },
                },
                "base_shapes": {"type": "array", "items": {"type": "string"}},
                "locations": {
                    "type": "array",
                    "items": {"type": "array", "items": {"type": "number"}},
                    "description": "Drilling points [[x, y, z], ...].",
                },
                "properties": {"type": "object",
                               "description": "Operation properties to assign."},
                "doc": _DOC_PROPERTY,
            },
            "required": ["type"],
            "additionalProperties": False,
        },
        "mutating": True,
        "handler": _op_add,
    },
    {
        "name": "cam_inspect",
        "description": (
            "Read the job back: per operation the command count, Z range, XY "
            "extents, cut length, cycle time and a feed-based time estimate, plus "
            "the stock box and the tool table. 'ok' is false when an operation is "
            "in error, produced no path, or leaves the stock."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"job": _JOB_PROPERTY, "doc": _DOC_PROPERTY},
            "additionalProperties": False,
        },
        "mutating": False,
        "handler": _inspect,
    },
    {
        "name": "cam_postprocess",
        "description": (
            "Post-process the job to G-code files. split_by 'tool' writes one file "
            "per tool number (what a GRBL machine without a tool changer needs), "
            "'operation' one per operation, 'none' a single file. Pass 'files' to "
            "name the outputs in section order. Existing files are never replaced "
            "unless overwrite is true."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "job": _JOB_PROPERTY,
                "post": {"type": "string", "description": "Post processor (default 'grbl')."},
                "post_args": {"type": "string",
                              "description": "e.g. '--translate_drill' (GRBL has no G81)."},
                "split_by": {"type": "string", "enum": ["tool", "operation", "fixture", "none"]},
                "output_dir": {"type": "string"},
                "prefix": {"type": "string"},
                "files": {"type": "array", "items": {"type": "string"}},
                "overwrite": {"type": "boolean"},
                "doc": _DOC_PROPERTY,
            },
            "additionalProperties": False,
        },
        "mutating": False,
        "handler": _postprocess,
    },
    {
        "name": "cam_gcode_check",
        "description": (
            "Parse a G-code file (or text) without FreeCAD and prove it is safe to "
            "run: XY inside the given bounds, Z never below z_floor, rapid XY moves "
            "only at or above safe_height, spindle started before the first cut "
            "below Z0, a feed rate present, expected tool numbers, and no canned "
            "drill cycle (GRBL has none). Returns the measured extents, move counts "
            "and a time estimate."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "text": {"type": "string"},
                "bounds": {
                    "type": "object",
                    "properties": {
                        "x_min": {"type": "number"}, "x_max": {"type": "number"},
                        "y_min": {"type": "number"}, "y_max": {"type": "number"},
                    },
                },
                "z_floor": {"type": "number", "description": "Deepest allowed Z (negative)."},
                "safe_height": {"type": "number"},
                "rapid_rate": {"type": "number", "description": "mm/min for the estimate."},
                "tools": {"type": "array", "items": {"type": "integer"}},
            },
            "additionalProperties": False,
        },
        "mutating": False,
        "handler": _gcode_check,
    },
]
