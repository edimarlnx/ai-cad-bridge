"""Undo / redo tools: one agent step is one transaction, so one undo.

Both tools are declared non-mutating on purpose: opening a transaction around
an undo would push a new entry onto the very stack we are walking.
"""

from .common import doc_summary, resolve_doc


def _stack(doc):
    return {
        "undo_stack": list(getattr(doc, "UndoNames", []) or []),
        "redo_stack": list(getattr(doc, "RedoNames", []) or []),
    }


def _undo(App, Gui, params):
    doc = resolve_doc(App, params)
    before = _stack(doc)
    steps = max(1, int(params.get("steps") or 1))
    undone = []
    for _ in range(steps):
        names = list(getattr(doc, "UndoNames", []) or [])
        if not names:
            break
        doc.undo()
        undone.append(names[0])
    doc.recompute()
    result = {
        "ok": bool(undone),
        "document": doc.Name,
        "undone": undone,
        "before": before,
        "after": _stack(doc),
        "summary": doc_summary(doc, App),
    }
    if not undone:
        result["reason"] = "nothing to undo (the undo stack is empty)"
    return result


def _redo(App, Gui, params):
    doc = resolve_doc(App, params)
    before = _stack(doc)
    steps = max(1, int(params.get("steps") or 1))
    redone = []
    for _ in range(steps):
        names = list(getattr(doc, "RedoNames", []) or [])
        if not names:
            break
        doc.redo()
        redone.append(names[0])
    doc.recompute()
    result = {
        "ok": bool(redone),
        "document": doc.Name,
        "redone": redone,
        "before": before,
        "after": _stack(doc),
        "summary": doc_summary(doc, App),
    }
    if not redone:
        result["reason"] = "nothing to redo (the redo stack is empty)"
    return result


_STEPS = {
    "type": "integer",
    "minimum": 1,
    "description": "How many steps. Default 1.",
}
_DOC_PROPERTY = {
    "type": "string",
    "description": "Document name; defaults to the active document.",
}

TOOLS = [
    {
        "name": "fc_undo",
        "description": (
            "Undo the last transaction(s) — each bridge call is one entry — and "
            "report the resulting undo/redo stacks."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"steps": _STEPS, "doc": _DOC_PROPERTY},
            "additionalProperties": False,
        },
        "mutating": False,
        "handler": _undo,
    },
    {
        "name": "fc_redo",
        "description": "Redo previously undone transaction(s).",
        "inputSchema": {
            "type": "object",
            "properties": {"steps": _STEPS, "doc": _DOC_PROPERTY},
            "additionalProperties": False,
        },
        "mutating": False,
        "handler": _redo,
    },
]
