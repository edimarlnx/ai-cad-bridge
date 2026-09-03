"""Knowledge tools: what this FreeCAD can do, what an API looks like, how to use it.

``fc_exec`` already makes every workbench reachable — all of FreeCAD is
scriptable. What an agent lacks is *knowledge*: which modules exist in this
build, what properties a ``PartDesign::Pad`` really has, and a working snippet
to start from. These three tools close that gap without guessing:

* ``fc_workbenches`` — honest import probe of the usual workbenches.
* ``fc_api_help``    — live introspection (modules, classes, TypeIds). A TypeId
  with no instance in the document is probed by creating an object inside a
  transaction that is immediately ABORTED, so nothing persists.
* ``fc_recipes``     — curated snippets shipped as Markdown under ``recipes/``.
"""

import os

from .common import ToolError

MAX_ENTRIES = 200
MAX_DOC = 200

# Modules whose import is fatal in a headless bridge session (measured: Draft
# imports PySide/pivy and SIGSEGVs FreeCADCmd when imported from a request).
_UNSAFE_HEADLESS_IMPORTS = {"Draft"}

RECIPES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "recipes")

# module name -> (note, safe_to_import)
#
# safe_to_import is False for the Python-implemented workbench modules that pull
# Qt/pivy at import time. Measured on this build: importing Draft from inside a
# bridge request ABORTS a headless FreeCAD (SIGSEGV) even though the same import
# succeeds in a plain FreeCADCmd script. So this tool never executes them — it
# reports presence with importlib.util.find_spec, which loads nothing.
_WORKBENCHES = [
    ("Part", "Solid primitives and boolean operations (OCCT). Always available.", True),
    ("PartDesign", "Body/Sketch/Pad/Pocket/Hole feature modeling.", False),
    ("Sketcher", "2D constrained sketches; the base of PartDesign.", True),
    ("Assembly", "Built-in Assembly workbench (FreeCAD 1.x): assemblies and joints.", False),
    ("UtilsAssembly", "Helper module of the Assembly workbench.", False),
    ("JointObject", "Joint definitions of the Assembly workbench (imports pivy/Qt).", False),
    ("TechDraw", "2D drawings: pages, views, dimensions, DXF export.", True),
    ("Path", "CAM/Path workbench core module (the GUI calls it 'CAM').", False),
    ("CAM", "Namespace alias for the CAM workbench.", False),
    ("Mesh", "Mesh objects, STL/OBJ import and export.", True),
    ("MeshPart", "Shape to mesh tessellation.", True),
    ("Draft", "2D drafting. Imports Qt: unsafe to import in a headless session.", False),
    ("Spreadsheet", "Spreadsheet objects used to drive parametric models.", True),
    ("Fem", "Finite element analysis.", True),
    ("Import", "STEP/IGES import and export.", True),
]


def _module_version(module):
    """Read a version string from a module.

    Never *calls* anything: a blind ``module.Version()`` on FreeCAD's C++
    modules can run arbitrary machinery (one of them segfaults FreeCADCmd), so
    only plain string/tuple attributes are read.
    """
    for attribute in ("__version__", "Version", "version"):
        value = getattr(module, attribute, None)
        if isinstance(value, str):
            return value
        if isinstance(value, (list, tuple)) and value and isinstance(value[0], (str, int)):
            return ".".join(str(part) for part in value[:3])
    return None


def _workbenches(App, Gui, params):
    import importlib.util
    import sys

    entries = []
    for name, note, safe_to_import in _WORKBENCHES:
        entry = {"name": name, "note": note}
        module = sys.modules.get(name)
        if module is None:
            try:
                spec = importlib.util.find_spec(name)
            except Exception as exc:
                spec = None
                entry["error"] = "%s: %s" % (type(exc).__name__, exc)
            entry["importable"] = spec is not None
            entry["origin"] = getattr(spec, "origin", None) if spec else None
            entry["loaded"] = False
            # Only execute the modules known not to drag Qt into a headless run.
            if spec is not None and (safe_to_import or Gui is not None):
                try:
                    module = __import__(name)
                except Exception as exc:
                    entry["importable"] = False
                    entry["error"] = "%s: %s" % (type(exc).__name__, exc)
                    module = None
        else:
            entry["importable"] = True
            entry["origin"] = getattr(module, "__file__", None)
        if module is not None:
            entry["loaded"] = True
            entry["version"] = _module_version(module)
            entry["public_names"] = len(
                [item for item in dir(module) if not item.startswith("_")]
            )
        entries.append(entry)
    return {
        "gui": Gui is not None,
        "note": (
            "importable=true means the module is on the FreeCAD module path; "
            "loaded=true means it is already executed in this session. Modules "
            "that pull Qt (Draft, JointObject, Path/CAM, Assembly helpers) are "
            "NOT imported by this tool, because doing so from a bridge request "
            "aborts a headless FreeCAD on this build — import them from fc_exec "
            "in a GUI session, where it is safe. Read the matching fc_recipes "
            "entry before using a workbench for the first time."
        ),
        "workbenches": entries,
        "recipes_available": _recipe_names(),
    }


def _first_doc_line(value):
    doc = getattr(value, "__doc__", None)
    if not isinstance(doc, str):
        return None
    text = " ".join(doc.strip().split())
    return text[:MAX_DOC] + ("…" if len(text) > MAX_DOC else "")


def _describe_module(module, query=None):
    entries = []
    for name in sorted(dir(module)):
        if name.startswith("_"):
            continue
        try:
            value = getattr(module, name)
        except Exception:
            continue
        doc = _first_doc_line(value)
        if query:
            haystack = ("%s %s" % (name, doc or "")).lower()
            if query not in haystack:
                continue
        entries.append({"name": name, "kind": type(value).__name__, "doc": doc})
    truncated = len(entries) > MAX_ENTRIES
    return entries[:MAX_ENTRIES], truncated


def _describe_properties(obj):
    properties = []
    for prop in getattr(obj, "PropertiesList", []) or []:
        entry = {"name": prop}
        for key, getter in (
            ("type", "getTypeIdOfProperty"),
            ("group", "getGroupOfProperty"),
            ("doc", "getDocumentationOfProperty"),
        ):
            try:
                value = getattr(obj, getter)(prop)
                if isinstance(value, str) and value:
                    entry[key] = " ".join(value.split())[:MAX_DOC]
            except Exception:
                pass
        properties.append(entry)
    return properties


def _find_instance(App, type_id):
    for doc in App.listDocuments().values():
        for obj in doc.Objects:
            if obj.TypeId == type_id:
                return obj
    return None


def _probe_type_id(App, type_id):
    """List a TypeId's properties by creating an object in an aborted transaction."""
    existing = _find_instance(App, type_id)
    if existing is not None:
        return _describe_properties(existing), "existing object %s" % existing.Name

    temp_doc = None
    doc = App.ActiveDocument
    if doc is None:
        temp_doc = App.newDocument("AiBridgeProbe")
        doc = temp_doc
    probe_name = None
    try:
        try:
            if not getattr(doc, "UndoMode", 0):
                doc.UndoMode = 1
        except Exception:
            pass
        doc.openTransaction("__aibridge_probe")
        obj = doc.addObject(type_id, "AiBridgeProbeObject")
        if obj is None:
            raise ToolError("FreeCAD refused to create an object of type %r" % type_id)
        probe_name = obj.Name
        properties = _describe_properties(obj)
    except ToolError:
        raise
    except Exception as exc:
        raise ToolError(
            "cannot introspect %r: %s: %s" % (type_id, type(exc).__name__, exc)
        )
    finally:
        try:
            doc.abortTransaction()
        except Exception:
            pass
        # Belt and braces: the transaction abort should have removed it.
        try:
            if probe_name and doc.getObject(probe_name) is not None:
                doc.removeObject(probe_name)
        except Exception:
            pass
        if temp_doc is not None:
            try:
                App.closeDocument(temp_doc.Name)
            except Exception:
                pass
    return properties, "temporary probe object (created and rolled back)"


def _api_help(App, Gui, params):
    module_name = params.get("module")
    name = params.get("name")
    query = (params.get("query") or "").strip().lower() or None

    # A TypeId such as "PartDesign::Pad" or "TechDraw::DrawViewPart".
    target = name or module_name or ""
    if "::" in target:
        properties, source = _probe_type_id(App, target)
        if query:
            properties = [
                item
                for item in properties
                if query in ("%s %s" % (item.get("name"), item.get("doc") or "")).lower()
            ]
        return {
            "kind": "type_id",
            "type_id": target,
            "source": source,
            "property_count": len(properties),
            "properties": properties[:MAX_ENTRIES],
            "truncated": len(properties) > MAX_ENTRIES,
        }

    if not module_name:
        raise ToolError(
            "give a module (e.g. 'PartDesign'), a TypeId (e.g. 'PartDesign::Pad') "
            "or both a module and a name"
        )
    import sys

    if (
        module_name.split(".")[0] in _UNSAFE_HEADLESS_IMPORTS
        and Gui is None
        and module_name not in sys.modules
        and not params.get("force")
    ):
        raise ToolError(
            "%s pulls Qt at import time and importing it from a bridge request "
            "aborts this headless FreeCAD; introspect it from a GUI session, or "
            "pass force:true if you accept losing the session" % module_name
        )
    try:
        module = __import__(module_name)
    except Exception as exc:
        raise ToolError(
            "cannot import %r here: %s: %s (see fc_workbenches)"
            % (module_name, type(exc).__name__, exc)
        )

    if name:
        try:
            value = getattr(module, name)
        except AttributeError:
            raise ToolError("%s has no attribute %r" % (module_name, name))
        members, truncated = ([], False)
        if hasattr(value, "__dict__") or isinstance(value, type):
            members, truncated = _describe_module(value, query)
        doc = getattr(value, "__doc__", None)
        return {
            "kind": "attribute",
            "module": module_name,
            "name": name,
            "type": type(value).__name__,
            "doc": (" ".join(doc.split())[:2000] if isinstance(doc, str) else None),
            "signature": _signature(value),
            "members": members,
            "truncated": truncated,
        }

    entries, truncated = _describe_module(module, query)
    return {
        "kind": "module",
        "module": module_name,
        "file": getattr(module, "__file__", None),
        "count": len(entries),
        "truncated": truncated,
        "attributes": entries,
    }


def _signature(value):
    """Best-effort callable signature (many FreeCAD builtins have none)."""
    try:
        import inspect

        return str(inspect.signature(value))
    except Exception:
        return None


def _recipe_names():
    try:
        return sorted(
            os.path.splitext(item)[0]
            for item in os.listdir(RECIPES_DIR)
            if item.endswith(".md")
        )
    except Exception:
        return []


def _recipes(App, Gui, params):
    available = _recipe_names()
    workbench = (params.get("workbench") or "").strip().lower()
    if not workbench:
        return {"available": available, "note": "pass workbench to read one"}
    path = os.path.join(RECIPES_DIR, "%s.md" % workbench)
    if not os.path.isfile(path):
        raise ToolError(
            "no recipe for %r; available: %s" % (workbench, ", ".join(available) or "none")
        )
    with open(path, "r", encoding="utf-8") as handle:
        text = handle.read()
    return {
        "workbench": workbench,
        "path": path,
        "available": available,
        "content": text,
    }


TOOLS = [
    {
        "name": "fc_workbenches",
        "description": (
            "Which FreeCAD workbenches/modules can actually be imported in THIS "
            "session (PartDesign, Sketcher, Assembly, TechDraw, CAM/Path, Mesh, "
            "Draft, Spreadsheet, FEM...), with versions and GUI caveats. Call it "
            "before using a workbench you have not used yet in this session."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "mutating": False,
        "handler": _workbenches,
    },
    {
        "name": "fc_api_help",
        "description": (
            "Live introspection of the running FreeCAD instead of guessing API "
            "names. Pass 'module' (e.g. 'PartDesign') for its public attributes "
            "with one-line docs; 'module'+'name' for one attribute; or a TypeId "
            "in 'name' (e.g. 'PartDesign::Pad', 'TechDraw::DrawViewPart', "
            "'Assembly::JointObject') for its property list with types and "
            "documentation. TypeId probing creates nothing permanent: it uses a "
            "transaction that is rolled back. 'query' filters case-insensitively."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "module": {"type": "string", "description": "Python module name."},
                "name": {
                    "type": "string",
                    "description": "Attribute name, or a TypeId containing '::'.",
                },
                "query": {"type": "string", "description": "Case-insensitive filter."},
                "force": {
                    "type": "boolean",
                    "description": (
                        "Import a module flagged unsafe headless (Draft). It can "
                        "abort the FreeCAD session; default false."
                    ),
                },
            },
            "additionalProperties": False,
        },
        "mutating": False,
        "handler": _api_help,
    },
    {
        "name": "fc_recipes",
        "description": (
            "Curated snippets per workbench (partdesign, assembly, techdraw, "
            "cam, part), verified headless on this FreeCAD build. Without "
            "arguments it lists what is available. Read the recipe before "
            "writing fc_exec code for a "
            "workbench for the first time — it encodes the API sequence and the "
            "known GUI-only limitations."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "workbench": {
                    "type": "string",
                    "description": "Recipe name, e.g. 'partdesign'.",
                },
            },
            "additionalProperties": False,
        },
        "mutating": False,
        "handler": _recipes,
    },
]
