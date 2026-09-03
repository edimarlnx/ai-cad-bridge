"""Sketcher tool: geometry, constraints and degrees of freedom of a sketch.

Degrees of freedom are the honest measure of whether a sketch is finished, so
the summary always runs ``solve()`` first and reports DoF, conflicts and
redundancies alongside the geometry.
"""

from .common import ToolError, number, resolve_doc, resolve_object, vector

# Sketcher point positions (Sketcher::PointPos) as readable names.
_POINT_POS = {0: "none", 1: "start", 2: "end", 3: "mid"}


def _geometry_entry(index, geo, construction):
    entry = {
        "index": index,
        "type": type(geo).__name__,
        "construction": bool(construction),
    }
    for attribute, key in (
        ("StartPoint", "start"),
        ("EndPoint", "end"),
        ("Center", "center"),
        ("Location", "location"),
    ):
        value = getattr(geo, attribute, None)
        if value is not None and hasattr(value, "x"):
            entry[key] = vector(value)
    for attribute, key in (
        ("Radius", "radius_mm"),
        ("MajorRadius", "major_radius_mm"),
        ("MinorRadius", "minor_radius_mm"),
    ):
        value = getattr(geo, attribute, None)
        if isinstance(value, (int, float)):
            entry[key] = number(value)
    try:
        entry["length_mm"] = number(geo.length())
    except Exception:
        pass
    return entry


def _constraint_entry(index, constraint):
    entry = {
        "index": index,
        "type": constraint.Type,
        "name": constraint.Name or None,
        "first": constraint.First,
        "first_pos": _POINT_POS.get(constraint.FirstPos, constraint.FirstPos),
    }
    if constraint.Second != -2000:
        entry["second"] = constraint.Second
        entry["second_pos"] = _POINT_POS.get(constraint.SecondPos, constraint.SecondPos)
    if constraint.Third != -2000:
        entry["third"] = constraint.Third
        entry["third_pos"] = _POINT_POS.get(constraint.ThirdPos, constraint.ThirdPos)
    try:
        entry["value"] = number(constraint.Value)
    except Exception:
        pass
    return entry


def _sketch_summary(App, Gui, params):
    doc = resolve_doc(App, params)
    sketch = resolve_object(doc, params.get("name"))
    if not sketch.TypeId.startswith("Sketcher::"):
        raise ToolError(
            "%s is a %s, not a sketch" % (sketch.Name, sketch.TypeId)
        )

    solver_status = None
    try:
        solver_status = int(sketch.solve())
    except Exception as exc:
        solver_status = "error: %s" % exc

    geometry = []
    for index, geo in enumerate(sketch.Geometry):
        construction = False
        try:
            construction = bool(sketch.getConstruction(index))
        except Exception:
            construction = bool(getattr(geo, "Construction", False))
        geometry.append(_geometry_entry(index, geo, construction))

    constraints = [
        _constraint_entry(index, constraint)
        for index, constraint in enumerate(sketch.Constraints)
    ]

    dof = None
    try:
        dof = int(sketch.DoF)
    except Exception:
        pass
    conflicting = list(getattr(sketch, "ConflictingConstraints", []) or [])
    redundant = list(getattr(sketch, "RedundantConstraints", []) or [])
    partially_redundant = list(
        getattr(sketch, "PartiallyRedundantConstraints", []) or []
    )
    malformed = list(getattr(sketch, "MalformedConstraints", []) or [])

    return {
        "ok": dof == 0 and not conflicting and not redundant,
        "document": doc.Name,
        "name": sketch.Name,
        "label": sketch.Label,
        "type": sketch.TypeId,
        "solver_status": solver_status,
        "dof": dof,
        "fully_constrained": bool(getattr(sketch, "FullyConstrained", False)),
        "geometry_count": len(geometry),
        "constraint_count": len(constraints),
        "conflicting_constraints": conflicting,
        "redundant_constraints": redundant,
        "partially_redundant_constraints": partially_redundant,
        "malformed_constraints": malformed,
        "geometry": geometry,
        "constraints": constraints,
        "placement": {
            "base": vector(sketch.Placement.Base),
            "axis": vector(sketch.Placement.Rotation.Axis),
        },
    }


TOOLS = [
    {
        "name": "fc_sketch_summary",
        "description": (
            "Everything about a sketch: geometry, constraints, degrees of "
            "freedom after solve(), fully-constrained flag and any conflicting, "
            "redundant or malformed constraints. DoF 0 with no conflicts is the "
            "success condition for a finished sketch."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Sketch name or unique label."},
                "doc": {"type": "string"},
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        "mutating": False,
        "handler": _sketch_summary,
    },
]
