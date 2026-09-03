"""Viewport tool: screenshots of the 3D view (GUI only)."""

import os
import time

from .common import ToolError, cache_dir, expand_path, resolve_doc

_VIEWS = {
    "iso": "viewIsometric",
    "isometric": "viewIsometric",
    "axonometric": "viewAxonometric",
    "front": "viewFront",
    "rear": "viewRear",
    "back": "viewRear",
    "left": "viewLeft",
    "right": "viewRight",
    "top": "viewTop",
    "bottom": "viewBottom",
}


def _screenshot(App, Gui, params):
    if Gui is None:
        return {
            "available": False,
            "reason": (
                "no GUI: this FreeCAD runs headless (FreeCADCmd), so there is no "
                "3D view to capture. Validate with fc_measure / fc_check instead."
            ),
        }
    doc = resolve_doc(App, params)
    try:
        Gui.setActiveDocument(doc.Name)
        view = Gui.ActiveDocument.ActiveView
    except Exception as exc:
        return {"available": False, "reason": "no active 3D view (%s)" % exc}
    if view is None:
        return {"available": False, "reason": "no active 3D view"}

    view_name = (params.get("view") or "iso").strip().lower()
    method = _VIEWS.get(view_name)
    if method is None:
        raise ToolError(
            "unknown view %r; use one of %s" % (view_name, ", ".join(sorted(_VIEWS)))
        )
    getattr(view, method)()
    if params.get("fit", True):
        view.fitAll()
    try:
        Gui.updateGui()
    except Exception:
        pass

    width = int(params.get("width") or 1024)
    height = int(params.get("height") or 768)
    background = params.get("background") or "White"
    path = params.get("path")
    path = (
        expand_path(path)
        if path
        else os.path.join(
            cache_dir(), "%s-%s-%d.png" % (doc.Name, view_name, int(time.time()))
        )
    )
    view.saveImage(path, width, height, background)
    if not os.path.exists(path):
        raise ToolError("screenshot was not written to %s" % path)
    return {
        "available": True,
        "path": path,
        "view": view_name,
        "width": width,
        "height": height,
        "size_bytes": os.path.getsize(path),
    }


TOOLS = [
    {
        "name": "fc_screenshot",
        "description": (
            "Capture the 3D view as a PNG under ~/.cache/freecad-ai-bridge/ "
            "(GUI sessions only; headless returns available:false). Use it as a "
            "sanity check after the numeric validation, not instead of it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "view": {
                    "type": "string",
                    "enum": sorted(set(_VIEWS)),
                    "description": "Camera preset. Default 'iso'.",
                },
                "width": {"type": "integer", "minimum": 64},
                "height": {"type": "integer", "minimum": 64},
                "fit": {"type": "boolean", "description": "Fit all objects. Default true."},
                "background": {
                    "type": "string",
                    "description": "'White', 'Black', 'Transparent' or 'Current'.",
                },
                "path": {"type": "string", "description": "Optional destination PNG path."},
                "doc": {"type": "string"},
            },
            "additionalProperties": False,
        },
        "mutating": False,
        "handler": _screenshot,
    },
]
