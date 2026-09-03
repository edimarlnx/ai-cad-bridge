"""Shared helpers for AiBridge tools: JSON coercion, shape and document summaries.

Rules followed everywhere in ``tools/``:

* all lengths are millimetres, areas mm2, volumes mm3 (FreeCAD's internal units);
* shapes are *summarized*, never dumped (an agent must not receive a BREP);
* nothing is written outside ``~/.cache/freecad-ai-bridge/`` unless the caller
  passes an explicit path, and an existing file is never overwritten without
  ``overwrite: true``.
"""

import math
import os

from dispatch import ToolError  # re-exported for tool modules

__all__ = [
    "ToolError",
    "CACHE_DIR",
    "cache_dir",
    "number",
    "jsonable",
    "vector",
    "bbox_summary",
    "shape_summary",
    "doc_summary",
    "resolve_doc",
    "resolve_object",
    "resolve_objects",
    "expand_path",
    "guard_overwrite",
]

CACHE_DIR = os.path.join(
    os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"),
    "freecad-ai-bridge",
)

MAX_STRING = 2000


def cache_dir():
    """Create (if needed) and return the add-on's cache directory."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    return CACHE_DIR


def number(value, digits=6):
    """Round a float for transport; non-finite values become ``None``."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    return round(value, digits)


def vector(vec, digits=6):
    """Convert a FreeCAD vector-like object to ``{x, y, z}``."""
    if vec is None:
        return None
    return {
        "x": number(vec.x, digits),
        "y": number(vec.y, digits),
        "z": number(vec.z, digits),
    }


def jsonable(value, depth=0):
    """Best-effort conversion of a FreeCAD value into JSON-safe data."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return number(value)
    if isinstance(value, str):
        return value if len(value) <= MAX_STRING else value[:MAX_STRING] + "…"
    if isinstance(value, bytes):
        return "<%d bytes>" % len(value)
    if depth > 4:
        return _fallback(value)
    if isinstance(value, dict):
        return {str(key): jsonable(item, depth + 1) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        items = list(value)
        truncated = len(items) > 200
        result = [jsonable(item, depth + 1) for item in items[:200]]
        if truncated:
            result.append("… %d more" % (len(items) - 200))
        return result

    type_name = type(value).__name__
    # FreeCAD.Vector
    if type_name == "Vector" and hasattr(value, "x"):
        return vector(value)
    # FreeCAD.Placement
    if type_name == "Placement" and hasattr(value, "Base"):
        return {
            "base": vector(value.Base),
            "axis": vector(value.Rotation.Axis),
            "angle_deg": number(math.degrees(value.Rotation.Angle)),
        }
    if type_name == "Rotation" and hasattr(value, "Axis"):
        return {
            "axis": vector(value.Axis),
            "angle_deg": number(math.degrees(value.Angle)),
        }
    # Units.Quantity
    if hasattr(value, "Value") and hasattr(value, "Unit"):
        return {"value": number(value.Value), "unit": str(value.Unit)}
    # Document objects become references, never nested dumps.
    if hasattr(value, "Name") and hasattr(value, "TypeId"):
        return {"$object": value.Name, "type": value.TypeId}
    return _fallback(value)


def _fallback(value):
    """Last resort: a truncated repr."""
    try:
        text = str(value)
    except Exception:
        text = "<unrepresentable %s>" % type(value).__name__
    return text if len(text) <= MAX_STRING else text[:MAX_STRING] + "…"


def bbox_summary(bbox):
    """Axis-aligned bounding box as plain numbers (mm)."""
    if bbox is None:
        return None
    try:
        return {
            "x_min": number(bbox.XMin),
            "y_min": number(bbox.YMin),
            "z_min": number(bbox.ZMin),
            "x_max": number(bbox.XMax),
            "y_max": number(bbox.YMax),
            "z_max": number(bbox.ZMax),
            "x_length": number(bbox.XLength),
            "y_length": number(bbox.YLength),
            "z_length": number(bbox.ZLength),
            "center": vector(bbox.Center),
        }
    except Exception:
        return None


def shape_summary(shape):
    """Compact, JSON-safe description of a ``Part.Shape``."""
    if shape is None:
        return None
    summary = {}
    try:
        if shape.isNull():
            return {"null": True}
    except Exception:
        return None
    summary["shape_type"] = getattr(shape, "ShapeType", None)
    for key, getter in (
        ("is_valid", lambda: bool(shape.isValid())),
        ("is_closed", lambda: bool(shape.isClosed())),
        ("volume_mm3", lambda: number(shape.Volume)),
        ("area_mm2", lambda: number(shape.Area)),
    ):
        try:
            summary[key] = getter()
        except Exception:
            summary[key] = None
    try:
        summary["bbox"] = bbox_summary(shape.BoundBox)
    except Exception:
        summary["bbox"] = None
    try:
        summary["center_of_mass"] = vector(shape.CenterOfMass)
    except Exception:
        summary["center_of_mass"] = None
    counts = {}
    for key, attribute in (
        ("solids", "Solids"),
        ("shells", "Shells"),
        ("faces", "Faces"),
        ("wires", "Wires"),
        ("edges", "Edges"),
        ("vertexes", "Vertexes"),
    ):
        try:
            counts[key] = len(getattr(shape, attribute))
        except Exception:
            counts[key] = None
    summary["counts"] = counts
    return summary


def doc_summary(doc, App=None):
    """Compact description of an open document."""
    if doc is None:
        return None
    active = False
    if App is not None and App.ActiveDocument is not None:
        active = App.ActiveDocument.Name == doc.Name
    return {
        "name": doc.Name,
        "label": getattr(doc, "Label", None),
        "file_name": getattr(doc, "FileName", "") or None,
        "modified": bool(getattr(doc, "Modified", False)),
        "object_count": len(doc.Objects),
        "active": active,
        "undo_count": len(getattr(doc, "UndoNames", []) or []),
        "redo_count": len(getattr(doc, "RedoNames", []) or []),
    }


def resolve_doc(App, params, required=True):
    """Return the document named by ``params['doc']`` or the active one."""
    name = params.get("doc")
    if name:
        doc = App.listDocuments().get(name)
        if doc is None:
            raise ToolError(
                "no open document named %r (open: %s)"
                % (name, ", ".join(sorted(App.listDocuments())) or "none")
            )
        return doc
    doc = App.ActiveDocument
    if doc is None and required:
        raise ToolError("no active document; call fc_doc_new or fc_doc_open first")
    return doc


def resolve_object(doc, name):
    """Find an object by internal Name, falling back to Label."""
    obj = doc.getObject(name)
    if obj is not None:
        return obj
    matches = [item for item in doc.Objects if item.Label == name]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ToolError(
            "label %r is ambiguous (%s); use the internal name"
            % (name, ", ".join(item.Name for item in matches))
        )
    raise ToolError(
        "object %r not found in %s (objects: %s)"
        % (name, doc.Name, ", ".join(item.Name for item in doc.Objects) or "none")
    )


def resolve_objects(doc, names):
    """Resolve a list of object names; empty list means every visible object."""
    if not names:
        return list(doc.Objects)
    return [resolve_object(doc, name) for name in names]


def expand_path(path):
    """Expand ``~`` and environment variables, returning an absolute path."""
    if not path:
        raise ToolError("path is required")
    return os.path.abspath(os.path.expanduser(os.path.expandvars(path)))


def guard_overwrite(path, overwrite):
    """Refuse to clobber an existing file unless explicitly allowed."""
    if os.path.exists(path) and not overwrite:
        raise ToolError(
            "refusing to overwrite existing file %s (pass overwrite: true to force)"
            % path
        )
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    return path
