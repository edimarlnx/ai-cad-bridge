"""Document structure tools: the object tree and per-object detail."""

from .common import (
    bbox_summary,
    jsonable,
    resolve_doc,
    resolve_object,
    shape_summary,
)

# Properties that would dump geometry or huge blobs into the answer.
_SKIPPED_PROPERTIES = {"Shape", "Mesh", "Points", "Proxy", "RawCode", "Code"}


def _object_state(obj):
    """State flags plus FreeCAD's own status string, when available."""
    state = list(getattr(obj, "State", []) or [])
    try:
        status = obj.getStatusString()
    except Exception:
        status = None
    return state, status


def _visible(obj, Gui):
    """Visibility, GUI only (headless documents have no ViewObject)."""
    if Gui is None:
        return None
    try:
        return bool(obj.ViewObject.Visibility)
    except Exception:
        return None


def _node(obj, Gui, with_bbox=True):
    state, status = _object_state(obj)
    node = {
        "name": obj.Name,
        "label": obj.Label,
        "type": obj.TypeId,
        "state": state,
        "status": status,
        "visible": _visible(obj, Gui),
    }
    if with_bbox:
        try:
            node["bbox"] = bbox_summary(obj.Shape.BoundBox)
        except Exception:
            node["bbox"] = None
    return node


def _tree(App, Gui, params):
    doc = resolve_doc(App, params)
    depth = params.get("depth")
    depth = 6 if depth is None else max(1, int(depth))
    with_bbox = params.get("bbox", True)

    # Roots are objects nobody else claims as a child.
    children_of = {}
    claimed = set()
    for obj in doc.Objects:
        kids = []
        for child in getattr(obj, "OutList", []) or []:
            kids.append(child.Name)
            claimed.add(child.Name)
        children_of[obj.Name] = kids
    roots = [obj for obj in doc.Objects if obj.Name not in claimed]
    if not roots:  # cyclic or fully linked document: fall back to everything
        roots = list(doc.Objects)

    visited = set()

    def build(obj, level):
        node = _node(obj, Gui, with_bbox)
        node["parents"] = [parent.Name for parent in getattr(obj, "InList", []) or []]
        if obj.Name in visited:
            node["repeated"] = True
            return node
        visited.add(obj.Name)
        if level < depth:
            kids = []
            for child_name in children_of.get(obj.Name, []):
                child = doc.getObject(child_name)
                if child is not None:
                    kids.append(build(child, level + 1))
            if kids:
                node["children"] = kids
        elif children_of.get(obj.Name):
            node["children_truncated"] = len(children_of[obj.Name])
        return node

    tree = [build(obj, 1) for obj in roots]
    return {
        "document": doc.Name,
        "object_count": len(doc.Objects),
        "tree": tree,
    }


def _object_get(App, Gui, params):
    doc = resolve_doc(App, params)
    obj = resolve_object(doc, params.get("name"))
    state, status = _object_state(obj)
    properties = {}
    for prop in getattr(obj, "PropertiesList", []) or []:
        if prop in _SKIPPED_PROPERTIES:
            continue
        entry = {}
        try:
            entry["type"] = obj.getTypeIdOfProperty(prop)
        except Exception:
            entry["type"] = None
        try:
            entry["value"] = jsonable(getattr(obj, prop))
        except Exception as exc:
            entry["value"] = "<unreadable: %s>" % exc
        properties[prop] = entry

    result = {
        "document": doc.Name,
        "name": obj.Name,
        "label": obj.Label,
        "type": obj.TypeId,
        "state": state,
        "status": status,
        "visible": _visible(obj, Gui),
        "in_list": [item.Name for item in getattr(obj, "InList", []) or []],
        "out_list": [item.Name for item in getattr(obj, "OutList", []) or []],
        "properties": properties,
    }
    try:
        result["shape"] = shape_summary(obj.Shape)
    except Exception:
        result["shape"] = None
    return result


_DOC_PROPERTY = {
    "type": "string",
    "description": "Document name; defaults to the active document.",
}

TOOLS = [
    {
        "name": "fc_tree",
        "description": (
            "Object tree of a document: name, label, type, parents, state, "
            "visibility and bounding box (mm). Start here to see what exists."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "doc": _DOC_PROPERTY,
                "depth": {
                    "type": "integer",
                    "description": "Maximum nesting depth. Default 6.",
                    "minimum": 1,
                },
                "bbox": {
                    "type": "boolean",
                    "description": "Include bounding boxes. Default true.",
                },
            },
            "additionalProperties": False,
        },
        "mutating": False,
        "handler": _tree,
    },
    {
        "name": "fc_object_get",
        "description": (
            "Full detail of one object: typed properties, dependencies and a "
            "shape summary (volume, area, bbox, center of mass, validity, "
            "element counts). Shapes are summarized, never dumped."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Internal object name (or unique label).",
                },
                "doc": _DOC_PROPERTY,
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        "mutating": False,
        "handler": _object_get,
    },
]
