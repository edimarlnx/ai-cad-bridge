"""Execution tools: run Python inside FreeCAD and recompute documents.

``fc_exec`` is the escape hatch that makes every workbench reachable (all of
FreeCAD is Python-scriptable). It must stay honest: the traceback is returned
verbatim, the HTTP call still succeeds so the agent can read and fix it, and the
transaction around it is aborted so a failed step leaves no debris.
"""

import contextlib
import io
import traceback

from .common import jsonable, resolve_doc


def _optional(module_name):
    """Import a module if present; ``None`` otherwise (keeps exec usable anywhere)."""
    try:
        return __import__(module_name)
    except Exception:
        return None


def _exec(App, Gui, params):
    code = params.get("code")
    if not isinstance(code, str) or not code.strip():
        return {"ok": False, "error": "code is required (a Python string)"}
    doc = resolve_doc(App, params, required=False)

    namespace = {
        "__name__": "aibridge_exec",
        "App": App,
        "FreeCAD": App,
        "Gui": Gui,
        "FreeCADGui": Gui,
        "doc": doc,
        "Part": _optional("Part"),
        "Sketcher": _optional("Sketcher"),
    }
    stdout = io.StringIO()
    stderr = io.StringIO()
    result = {"ok": True}
    try:
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            exec(compile(code, "<fc_exec>", "exec"), namespace, namespace)  # noqa: S102
    except BaseException:  # noqa: BLE001 - report anything the snippet raised
        result["ok"] = False
        result["error"] = traceback.format_exc()

    result["stdout"] = stdout.getvalue()
    result["stderr"] = stderr.getvalue()
    if "_result" in namespace:
        value = namespace["_result"]
        result["result"] = jsonable(value)
        result["result_repr"] = repr(value)[:2000]
    if doc is not None:
        result["document"] = doc.Name
    elif App.ActiveDocument is not None:
        result["document"] = App.ActiveDocument.Name
    return result


def _recompute(App, Gui, params):
    doc = resolve_doc(App, params)
    force = bool(params.get("force"))
    if force:
        for obj in doc.Objects:
            obj.touch()
    touched = [obj.Name for obj in doc.Objects if "Touched" in (obj.State or [])]
    count = doc.recompute(None, force, True) if force else doc.recompute()
    errors = []
    for obj in doc.Objects:
        state = list(obj.State or [])
        if "Invalid" in state or "Error" in state or "Touched" in state:
            try:
                status = obj.getStatusString()
            except Exception:
                status = None
            errors.append(
                {
                    "name": obj.Name,
                    "label": obj.Label,
                    "type": obj.TypeId,
                    "state": state,
                    "status": status,
                }
            )
    return {
        "ok": not errors,
        "document": doc.Name,
        "recomputed": int(count) if isinstance(count, int) else None,
        "touched_before": touched,
        "errors": errors,
    }


_DOC_PROPERTY = {
    "type": "string",
    "description": "Document name; defaults to the active document.",
}

TOOLS = [
    {
        "name": "fc_exec",
        "description": (
            "Run Python inside the live FreeCAD. Bound names: App (FreeCAD), Gui "
            "(None when headless), doc (the target document), Part, Sketcher. "
            "Set a variable named _result to return a value. stdout/stderr are "
            "captured; on failure the traceback comes back verbatim and the "
            "whole call is rolled back as one transaction. This is the universal "
            "path: every workbench (PartDesign, Assembly, TechDraw, CAM, Draft, "
            "FEM...) is scriptable here — call fc_workbenches and fc_recipes "
            "first if you are unsure of the API."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Python source to execute."},
                "doc": _DOC_PROPERTY,
                "transaction_name": {
                    "type": "string",
                    "description": "Label shown in FreeCAD's undo stack for this step.",
                },
            },
            "required": ["code"],
            "additionalProperties": False,
        },
        "mutating": True,
        "abort_on_failure": True,
        "handler": _exec,
    },
    {
        "name": "fc_recompute",
        "description": (
            "Recompute the document and report every object left touched, "
            "invalid or in error (with FreeCAD's status string). Run this after "
            "every build step."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "doc": _DOC_PROPERTY,
                "force": {
                    "type": "boolean",
                    "description": "Touch every object first and force a full recompute.",
                },
            },
            "additionalProperties": False,
        },
        # Not "mutating": a recompute is not a user edit, and wrapping it in a
        # transaction would push noise onto the undo stack the agent relies on.
        "mutating": False,
        "handler": _recompute,
    },
]
