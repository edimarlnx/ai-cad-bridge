"""Session and document tools: status, list, new, open, save-as."""

import os

from .common import (
    ToolError,
    doc_summary,
    expand_path,
    guard_overwrite,
    resolve_doc,
)


def _status(App, Gui, params):
    import server

    active = App.ActiveDocument
    return {
        "freecad_version": ".".join(str(part) for part in App.Version()[:3]),
        "freecad_build": " ".join(str(part) for part in App.Version()[3:5]),
        "python_version": ".".join(str(part) for part in __import__("sys").version_info[:3]),
        "gui": Gui is not None,
        "active_document": active.Name if active else None,
        "open_documents": sorted(App.listDocuments()),
        "server": server.status(),
        "cache_dir": os.path.join(
            os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"),
            "freecad-ai-bridge",
        ),
    }


def _doc_list(App, Gui, params):
    documents = [
        doc_summary(doc, App) for _, doc in sorted(App.listDocuments().items())
    ]
    return {"documents": documents, "count": len(documents)}


def _doc_new(App, Gui, params):
    name = params.get("name") or "Unnamed"
    doc = App.newDocument(str(name))
    App.setActiveDocument(doc.Name)
    return {"ok": True, "document": doc_summary(doc, App)}


def _doc_open(App, Gui, params):
    path = expand_path(params.get("path"))
    if not os.path.isfile(path):
        raise ToolError("file not found: %s" % path)
    doc = App.openDocument(path)
    App.setActiveDocument(doc.Name)
    return {"ok": True, "document": doc_summary(doc, App)}


def _doc_save_as(App, Gui, params):
    doc = resolve_doc(App, params)
    path = expand_path(params.get("path"))
    if not path.lower().endswith(".fcstd"):
        path += ".FCStd"
    guard_overwrite(path, bool(params.get("overwrite")))
    doc.saveAs(path)
    return {"ok": True, "path": path, "document": doc_summary(doc, App)}


_DOC_PROPERTY = {
    "type": "string",
    "description": "Document name; defaults to the active document.",
}

TOOLS = [
    {
        "name": "fc_status",
        "description": (
            "FreeCAD version, GUI or headless, active document, open documents "
            "and bridge server uptime. Call this first."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "mutating": False,
        "handler": _status,
    },
    {
        "name": "fc_doc_list",
        "description": "List open documents (name, label, file, modified, object count).",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "mutating": False,
        "handler": _doc_list,
    },
    {
        "name": "fc_doc_new",
        "description": "Create a new empty document and make it active.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Document name."},
            },
            "additionalProperties": False,
        },
        "mutating": True,
        "handler": _doc_new,
    },
    {
        "name": "fc_doc_open",
        "description": "Open an existing .FCStd file and make it active.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path to the .FCStd file."},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        "mutating": True,
        "handler": _doc_open,
    },
    {
        "name": "fc_doc_save_as",
        "description": (
            "Save the document to a NEW path. Never overwrites an existing file "
            "unless overwrite is true, and never saves over the user's original "
            "file implicitly."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Destination .FCStd path."},
                "doc": _DOC_PROPERTY,
                "overwrite": {
                    "type": "boolean",
                    "description": "Allow replacing an existing file. Default false.",
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        "mutating": True,
        "handler": _doc_save_as,
    },
]
