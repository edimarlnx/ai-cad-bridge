#!/usr/bin/env python3
"""MCP stdio server that puts a running FreeCAD in reach of Claude Code / Codex.

Pure standard library on purpose: it must run with whatever ``python3`` the host
has, with no virtualenv and no ``mcp`` SDK.

    claude mcp add freecad -- python3 <repo>/bridge/freecad_mcp.py

Protocol: MCP ``2025-06-18`` over newline-delimited JSON-RPC on stdin/stdout.
Handled methods are ``initialize``, ``notifications/initialized``, ``ping``,
``tools/list``, ``tools/call``, ``resources/list`` and ``resources/read``;
anything else answers ``-32601``.

Every tool call is proxied to the add-on's HTTP JSON-RPC server on 127.0.0.1,
whose port and token come from ``~/.config/freecad-ai-bridge/config.json``. When
FreeCAD is not running (or the bridge is stopped) the server stays useful:
``tools/list`` returns a static catalogue and ``tools/call`` answers with an
``isError`` message explaining how to start the bridge, instead of failing the
whole MCP session.

NOTHING but JSON-RPC may reach stdout; logs go to stderr.
"""

import json
import os
import sys
import urllib.error
import urllib.request

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "freecad"
SERVER_VERSION = "0.1.0"
REQUEST_TIMEOUT = 180

CONFIG_PATH = os.path.join(
    os.environ.get("AIBRIDGE_CONFIG_DIR")
    or os.path.join(
        # Deliberately NOT XDG_CONFIG_HOME: inside the FreeCAD Flatpak it points at
    # ~/.var/app/org.freecad.FreeCAD/config, so the host bridge would never find
    # the file. HOME is the real home on both sides (the sandbox has host access).
    os.path.expanduser("~/.config"),
        "freecad-ai-bridge",
    ),
    "config.json",
)
DEFAULT_PORT = 8765

_HERE = os.path.dirname(os.path.abspath(__file__))
RECIPES_DIR = os.path.join(_HERE, os.pardir, "freecad", "AiBridge", "recipes")
RECIPE_SCHEME = "freecad://recipes/"

INSTRUCTIONS = """\
You are driving a live FreeCAD session. Work in a do -> validate loop and never
claim a model is correct until the numbers say so:

1. fc_status, then fc_tree to see what already exists.
2. Before using a workbench for the first time, call fc_workbenches (what this
   build has) and fc_recipes (a verified snippet for PartDesign, Assembly,
   TechDraw, CAM or Part). fc_api_help introspects the real API -- use it
   instead of guessing property names.
3. Build with fc_exec. Each call is ONE undoable transaction; give it a
   transaction_name so the user can undo your step. On failure you get the
   traceback verbatim and the step is rolled back: read it and fix the code.
4. fc_recompute -- any object left touched, invalid or in error must be fixed
   before moving on.
5. fc_measure and fc_check -- prove the volume, bounding box, distances and
   validity match what was asked. fc_sketch_summary must reach DoF 0 with no
   conflicting or redundant constraints for a finished sketch.
6. fc_screenshot as a sanity check when a GUI is attached (it returns
   available:false when FreeCAD runs headless -- that is not an error).
7. fc_export only when asked. Files are never overwritten unless you pass
   overwrite:true, and fc_doc_save_as never writes over the user's original
   file implicitly.

Report the measured numbers, not your intentions.
"""

# Used when the add-on is unreachable, so an agent can still see what exists.
# An MCP client caches the catalogue it receives at connect time, so this list
# has to carry the SAME schemas as the live tools: without them array arguments
# arrive as strings for the rest of the session. fallback_tools.json is the
# generated, schema-complete copy (bridge/gen_fallback.py); the inline pairs
# below are only a last resort if that file is missing.
FALLBACK_TOOLS_PATH = os.path.join(_HERE, "fallback_tools.json")

FALLBACK_TOOLS = [
    ("fc_status", "FreeCAD version, GUI or headless, active document, server uptime."),
    ("fc_doc_list", "List open documents."),
    ("fc_doc_new", "Create a new document."),
    ("fc_doc_open", "Open an existing .FCStd file."),
    ("fc_doc_save_as", "Save the document to a new path (never overwrites)."),
    ("fc_tree", "Object tree: names, types, state, visibility, bounding boxes."),
    ("fc_object_get", "Properties and shape summary of one object."),
    ("fc_exec", "Run Python inside FreeCAD as one undoable transaction."),
    ("fc_recompute", "Recompute and report objects in error."),
    ("fc_measure", "bbox, volume, area, distance or centre of mass."),
    ("fc_check", "Geometry validity of one object, plus a non-destructive fix report."),
    ("fc_screenshot", "PNG of the 3D view (GUI sessions only)."),
    ("fc_export", "Export objects to STEP, STL, OBJ, IGES or BREP."),
    ("fc_undo", "Undo the last transaction(s)."),
    ("fc_redo", "Redo undone transaction(s)."),
    ("fc_sketch_summary", "Sketch geometry, constraints and degrees of freedom."),
    ("fc_workbenches", "Which FreeCAD workbenches this build can use."),
    ("fc_api_help", "Live introspection of modules, attributes and TypeIds."),
    ("fc_recipes", "Verified snippets per workbench."),
    ("cam_job_create", "Create a CAM job (model + stock + tools)."),
    ("cam_tool_add", "Add or edit a tool controller with real feeds and speeds."),
    ("cam_op_add", "Add a CAM operation (pocket, vcarve, drilling, engrave, ...)."),
    ("cam_inspect", "Read the toolpaths back: counts, Z range, XY bounds, time."),
    ("cam_postprocess", "Post-process to G-code files, one per tool if asked."),
    ("cam_gcode_check", "Re-parse G-code: bounds, depth, safe rapids, spindle, tools."),
]


def load_fallback_tools():
    """The generated catalogue, or the inline names when it is not there."""
    try:
        with open(FALLBACK_TOOLS_PATH, "r", encoding="utf-8") as handle:
            catalogue = json.load(handle).get("tools")
        if catalogue:
            return [
                {
                    "name": tool["name"],
                    "description": "%s [FreeCAD bridge offline: start it to use this]"
                    % tool.get("description", ""),
                    "inputSchema": tool.get("inputSchema")
                    or {"type": "object", "properties": {}},
                }
                for tool in catalogue
            ]
    except Exception as exc:
        log("no generated fallback catalogue (%s); using the inline names" % exc)
    return [
        {
            "name": name,
            "description": "%s [FreeCAD bridge offline: start it to use this]" % description,
            "inputSchema": {"type": "object", "additionalProperties": True},
        }
        for name, description in FALLBACK_TOOLS
    ]

NOT_RUNNING_HINT = (
    "The FreeCAD AI Bridge is not reachable at %s. Start it: open FreeCAD, pick "
    "the 'AI Bridge' workbench and press 'Start bridge' (or run FreeCAD with "
    "AIBRIDGE_AUTOSTART=1). Details: %s"
)


def log(message):
    """Diagnostics go to stderr; stdout is reserved for the protocol."""
    print("[freecad-mcp] %s" % message, file=sys.stderr, flush=True)


def read_config():
    """Port and token written by the add-on on its first start."""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
            config = json.load(handle)
        if isinstance(config, dict):
            return config
    except Exception:
        pass
    return {}


def addon_url():
    config = read_config()
    return "http://127.0.0.1:%s/rpc" % (config.get("port") or DEFAULT_PORT)


class AddonError(Exception):
    """The add-on could not be reached at all (FreeCAD closed, bridge stopped)."""


def addon_rpc(method, params=None, request_id=1):
    """One JSON-RPC round trip to the add-on inside FreeCAD."""
    config = read_config()
    token = config.get("token")
    if not token:
        raise AddonError(
            "no config at %s yet — start the bridge inside FreeCAD once" % CONFIG_PATH
        )
    payload = json.dumps(
        {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}
    ).encode("utf-8")
    request = urllib.request.Request(
        addon_url(),
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer %s" % token,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise AddonError(
                "the bridge rejected the token in %s; restart it in FreeCAD"
                % CONFIG_PATH
            )
        raise AddonError("HTTP %s from the bridge" % exc.code)
    except Exception as exc:
        raise AddonError("%s: %s" % (type(exc).__name__, exc))


# -- MCP payload helpers -------------------------------------------------
def text_content(payload, is_error=False):
    """MCP tool result carrying JSON as text."""
    text = payload if isinstance(payload, str) else json.dumps(payload, indent=2)
    result = {"content": [{"type": "text", "text": text}]}
    if is_error:
        result["isError"] = True
    return result


def recipe_files():
    try:
        return sorted(
            item for item in os.listdir(RECIPES_DIR) if item.endswith(".md")
        )
    except Exception:
        return []


def list_resources():
    resources = []
    for filename in recipe_files():
        name = os.path.splitext(filename)[0]
        resources.append(
            {
                "uri": "%s%s" % (RECIPE_SCHEME, name),
                "name": "%s recipe" % name,
                "title": "FreeCAD %s recipe" % name,
                "description": (
                    "Verified snippets for the %s workbench: the API sequence "
                    "that works on this FreeCAD build and its known limits." % name
                ),
                "mimeType": "text/markdown",
            }
        )
    return resources


def read_resource(uri):
    if not uri.startswith(RECIPE_SCHEME):
        raise ValueError("unknown resource: %s" % uri)
    name = uri[len(RECIPE_SCHEME):].strip("/")
    path = os.path.join(RECIPES_DIR, "%s.md" % name)
    if not os.path.isfile(path):
        raise ValueError(
            "no recipe %r; available: %s"
            % (name, ", ".join(os.path.splitext(f)[0] for f in recipe_files()))
        )
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


# -- method handlers -----------------------------------------------------
def handle_initialize(params):
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {"tools": {}, "resources": {}},
        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        "instructions": INSTRUCTIONS,
    }


def handle_tools_list(params):
    try:
        answer = addon_rpc("tools.list")
        if "result" in answer:
            tools = []
            for tool in answer["result"].get("tools", []):
                tools.append(
                    {
                        "name": tool["name"],
                        "description": tool.get("description", ""),
                        "inputSchema": tool.get("inputSchema")
                        or {"type": "object", "properties": {}},
                    }
                )
            if tools:
                return {"tools": tools}
        log("tools.list returned no tools; using the fallback catalogue")
    except AddonError as exc:
        log("add-on unreachable (%s); using the fallback catalogue" % exc)
    return {"tools": load_fallback_tools()}


def handle_tools_call(params):
    name = params.get("name")
    arguments = params.get("arguments") or {}
    try:
        answer = addon_rpc("tools.call", {"name": name, "arguments": arguments})
    except AddonError as exc:
        return text_content(NOT_RUNNING_HINT % (addon_url(), exc), is_error=True)
    if "error" in answer:
        error = answer["error"]
        payload = {"error": error.get("message"), "code": error.get("code")}
        if error.get("data"):
            payload["data"] = error["data"]
        return text_content(payload, is_error=True)
    return text_content(answer.get("result"))


def handle_resources_list(params):
    return {"resources": list_resources()}


def handle_resources_read(params):
    uri = params.get("uri") or ""
    return {
        "contents": [
            {"uri": uri, "mimeType": "text/markdown", "text": read_resource(uri)}
        ]
    }


HANDLERS = {
    "initialize": handle_initialize,
    "ping": lambda params: {},
    "tools/list": handle_tools_list,
    "tools/call": handle_tools_call,
    "resources/list": handle_resources_list,
    "resources/read": handle_resources_read,
}
# Notifications never get a reply.
NOTIFICATIONS = {
    "notifications/initialized",
    "notifications/cancelled",
    "initialized",
}


def dispatch(message):
    """Return a response object, or None for notifications."""
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") or {}

    if method in NOTIFICATIONS or request_id is None:
        return None

    handler = HANDLERS.get(method)
    if handler is None:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32601, "message": "method not found: %s" % method},
        }
    try:
        return {"jsonrpc": "2.0", "id": request_id, "result": handler(params)}
    except ValueError as exc:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32602, "message": str(exc)},
        }
    except Exception as exc:
        import traceback

        log(traceback.format_exc())
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {
                "code": -32603,
                "message": "%s: %s" % (type(exc).__name__, exc),
            },
        }


def main():
    log("ready (config: %s)" % CONFIG_PATH)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except Exception as exc:
            sys.stdout.write(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": -32700, "message": "parse error: %s" % exc},
                    }
                )
                + "\n"
            )
            sys.stdout.flush()
            continue
        if isinstance(message, list):  # batch
            responses = [item for item in (dispatch(one) for one in message) if item]
            if responses:
                sys.stdout.write(json.dumps(responses) + "\n")
                sys.stdout.flush()
            continue
        response = dispatch(message)
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()
    log("stdin closed, exiting")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
