"""Geometry tools: measurement, validity checking and export.

These are the agent's validation instruments — after building something with
``fc_exec`` it must confirm the numbers here match the spec before claiming the
model is done.
"""

import os

from .common import (
    ToolError,
    bbox_summary,
    expand_path,
    guard_overwrite,
    number,
    resolve_doc,
    resolve_object,
    resolve_objects,
    shape_summary,
    vector,
)


def _shape_of(obj):
    shape = getattr(obj, "Shape", None)
    if shape is None:
        raise ToolError("object %s (%s) has no shape" % (obj.Name, obj.TypeId))
    return shape


def _measure(App, Gui, params):
    doc = resolve_doc(App, params)
    kind = (params.get("kind") or "").strip().lower()
    names = params.get("objects") or []
    if not names:
        raise ToolError("objects is required (a list of object names)")
    objects = [resolve_object(doc, name) for name in names]
    shapes = [(obj, _shape_of(obj)) for obj in objects]

    if kind == "distance":
        if len(shapes) != 2:
            raise ToolError("kind 'distance' needs exactly two objects")
        (obj_a, shape_a), (obj_b, shape_b) = shapes
        distance, points, _info = shape_a.distToShape(shape_b)
        pair = points[0] if points else (None, None)
        return {
            "kind": kind,
            "unit": "mm",
            "objects": [obj_a.Name, obj_b.Name],
            "distance_mm": number(distance),
            "point_a": vector(pair[0]) if pair[0] is not None else None,
            "point_b": vector(pair[1]) if pair[1] is not None else None,
        }

    if kind == "bbox":
        per_object = []
        mins = [None, None, None]
        maxs = [None, None, None]
        for obj, shape in shapes:
            box = shape.BoundBox
            per_object.append({"name": obj.Name, "bbox": bbox_summary(box)})
            lows = (box.XMin, box.YMin, box.ZMin)
            highs = (box.XMax, box.YMax, box.ZMax)
            for axis in range(3):
                mins[axis] = lows[axis] if mins[axis] is None else min(mins[axis], lows[axis])
                maxs[axis] = highs[axis] if maxs[axis] is None else max(maxs[axis], highs[axis])
        combined = {
            "x_min": number(mins[0]),
            "y_min": number(mins[1]),
            "z_min": number(mins[2]),
            "x_max": number(maxs[0]),
            "y_max": number(maxs[1]),
            "z_max": number(maxs[2]),
            "x_length": number(maxs[0] - mins[0]),
            "y_length": number(maxs[1] - mins[1]),
            "z_length": number(maxs[2] - mins[2]),
        }
        return {
            "kind": kind,
            "unit": "mm",
            "objects": per_object,
            "combined_bbox": combined,
        }

    if kind in ("volume", "area", "center_of_mass"):
        per_object = []
        total = 0.0
        for obj, shape in shapes:
            if kind == "volume":
                value = shape.Volume
                per_object.append({"name": obj.Name, "volume_mm3": number(value)})
                total += value
            elif kind == "area":
                value = shape.Area
                per_object.append({"name": obj.Name, "area_mm2": number(value)})
                total += value
            else:
                per_object.append(
                    {
                        "name": obj.Name,
                        "center_of_mass": vector(shape.CenterOfMass),
                        "volume_mm3": number(shape.Volume),
                    }
                )
        result = {"kind": kind, "objects": per_object}
        if kind == "volume":
            result["unit"] = "mm3"
            result["total_volume_mm3"] = number(total)
        elif kind == "area":
            result["unit"] = "mm2"
            result["total_area_mm2"] = number(total)
        else:
            result["unit"] = "mm"
        return result

    raise ToolError(
        "unknown kind %r; use bbox, volume, area, distance or center_of_mass" % kind
    )


def _check(App, Gui, params):
    doc = resolve_doc(App, params)
    obj = resolve_object(doc, params.get("name"))
    shape = _shape_of(obj)
    summary = shape_summary(shape)

    issues = []
    if not summary.get("is_valid"):
        issues.append("shape is not valid")
    if summary.get("shape_type") == "Solid" and not summary.get("is_closed"):
        issues.append("solid is not closed")
    if (summary.get("counts") or {}).get("solids") == 0:
        issues.append("no solid: the result is a surface/wire, not a printable body")

    # Non-destructive repair report: fix a COPY so the document is untouched.
    fix_report = {}
    try:
        precision = float(params.get("precision") or 1e-7)
        min_tol = float(params.get("min_tolerance") or 1e-7)
        max_tol = float(params.get("max_tolerance") or 1e-5)
        copy = shape.copy()
        changed = bool(copy.fix(precision, min_tol, max_tol))
        fix_report = {
            "attempted": True,
            "changed_something": changed,
            "valid_after_fix": bool(copy.isValid()),
            "volume_after_fix_mm3": number(copy.Volume),
            "note": "applied to a copy only; the document was not modified",
        }
    except Exception as exc:
        fix_report = {"attempted": False, "error": str(exc)}

    tolerance = None
    try:
        tolerance = number(shape.getTolerance(0), 12)
    except Exception:
        pass

    return {
        "ok": not issues,
        "document": doc.Name,
        "name": obj.Name,
        "type": obj.TypeId,
        "shape": summary,
        "tolerance": tolerance,
        "issues": issues,
        "fix": fix_report,
    }


_EXPORT_FORMATS = {
    "step": ".step",
    "stp": ".step",
    "stl": ".stl",
    "obj": ".obj",
    "iges": ".iges",
    "brep": ".brep",
}


def _export(App, Gui, params):
    doc = resolve_doc(App, params)
    fmt = (params.get("format") or "").strip().lower()
    if fmt not in _EXPORT_FORMATS:
        raise ToolError(
            "unknown format %r; use one of %s"
            % (fmt, ", ".join(sorted(_EXPORT_FORMATS)))
        )
    objects = resolve_objects(doc, params.get("objects") or [])
    if not objects:
        raise ToolError("nothing to export: the document has no objects")

    path = params.get("path")
    if path:
        path = expand_path(path)
    else:
        from .common import cache_dir

        path = os.path.join(cache_dir(), "%s%s" % (doc.Name, _EXPORT_FORMATS[fmt]))
    if not os.path.splitext(path)[1]:
        path += _EXPORT_FORMATS[fmt]
    guard_overwrite(path, bool(params.get("overwrite")))

    if fmt in ("stl", "obj"):
        import Mesh

        Mesh.export(objects, path)
    else:
        import Part

        Part.export(objects, path)

    if not os.path.exists(path):
        raise ToolError("export produced no file at %s" % path)
    return {
        "ok": True,
        "path": path,
        "format": fmt,
        "size_bytes": os.path.getsize(path),
        "objects": [obj.Name for obj in objects],
    }


_DOC_PROPERTY = {
    "type": "string",
    "description": "Document name; defaults to the active document.",
}

TOOLS = [
    {
        "name": "fc_measure",
        "description": (
            "Measure objects: bbox, volume (mm3), area (mm2), distance between "
            "two shapes, or center of mass. Use it to prove the model matches "
            "the requested dimensions."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["bbox", "volume", "area", "distance", "center_of_mass"],
                },
                "objects": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Object names (two for 'distance').",
                },
                "doc": _DOC_PROPERTY,
            },
            "required": ["kind", "objects"],
            "additionalProperties": False,
        },
        "mutating": False,
        "handler": _measure,
    },
    {
        "name": "fc_check",
        "description": (
            "Geometry health of one object: isValid, isClosed, element counts, "
            "tolerance, and a non-destructive fix() report run on a copy. "
            "'ok' is false when the shape is invalid, open or has no solid."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Object name or unique label."},
                "doc": _DOC_PROPERTY,
                "precision": {"type": "number"},
                "min_tolerance": {"type": "number"},
                "max_tolerance": {"type": "number"},
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        "mutating": False,
        "handler": _check,
    },
    {
        "name": "fc_export",
        "description": (
            "Export objects to STEP, STL, OBJ, IGES or BREP. Without a path the "
            "file lands in ~/.cache/freecad-ai-bridge/. An existing file is "
            "never replaced unless overwrite is true."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "objects": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Object names; empty means the whole document.",
                },
                "format": {
                    "type": "string",
                    "enum": ["step", "stl", "obj", "iges", "brep"],
                },
                "path": {"type": "string", "description": "Destination file path."},
                "doc": _DOC_PROPERTY,
                "overwrite": {"type": "boolean"},
            },
            "required": ["format"],
            "additionalProperties": False,
        },
        "mutating": False,
        "handler": _export,
    },
]
