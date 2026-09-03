# In-process proof that the AI Bridge add-on works in the REAL runtime
# (Flatpak FreeCAD 1.1.3, FreeCADCmd, no GUI, no container). Run it with:
#
#   flatpak run --command=FreeCADCmd org.freecad.FreeCAD tests/test_headless.py
#
# The script must live under ~/ because the Flatpak sandbox cannot see /tmp.
# FreeCADCmd executes the file with a module name other than "__main__", so the
# checks run unconditionally (guarding on __main__ would never fire).
#
# It starts the add-on's HTTP server in this very process, then drives every
# tool from a worker thread over 127.0.0.1 with urllib — exactly the path the
# host-side MCP server takes. Any failed assertion exits non-zero.
import json
import os
import sys
import threading
import traceback
import urllib.error
import urllib.request

# Line buffering so a crash inside FreeCAD still shows how far we got.
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
_TEMP = os.path.join(_REPO, "temp")
sys.path.insert(0, os.path.join(_REPO, "freecad", "AiBridge"))

os.makedirs(_TEMP, exist_ok=True)
# Keep the user's real ~/.config/freecad-ai-bridge untouched while testing.
os.environ["AIBRIDGE_CONFIG_DIR"] = os.path.join(_TEMP, "config")

import dispatch  # noqa: E402  (needs the sys.path line above)
import server  # noqa: E402

_FAILURES = []
_CHECKS = [0]


def check(label, condition, detail=""):
    """Record one assertion; keep going so the report shows everything at once."""
    _CHECKS[0] += 1
    if condition:
        print("  ok   %s" % label)
    else:
        print("  FAIL %s %s" % (label, detail))
        _FAILURES.append("%s %s" % (label, detail))


def section(title):
    print("\n--- %s ---" % title)


class Client:
    """Minimal JSON-RPC client, the same shape the MCP bridge uses."""

    def __init__(self, port, token):
        self.url = "http://127.0.0.1:%d/rpc" % port
        self.token = token
        self.next_id = 0

    def rpc(self, method, params=None):
        self.next_id += 1
        payload = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": self.next_id,
                "method": method,
                "params": params or {},
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            self.url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer %s" % self.token,
            },
        )
        with urllib.request.urlopen(request, timeout=180) as response:
            return json.loads(response.read().decode("utf-8"))

    def call(self, name, arguments=None):
        """Call a tool and fail loudly on a JSON-RPC error."""
        answer = self.rpc("tools.call", {"name": name, "arguments": arguments or {}})
        if "error" in answer:
            raise AssertionError("%s failed: %s" % (name, answer["error"]))
        return answer["result"]


def run_checks(client):
    section("health and auth")
    with urllib.request.urlopen(
        "http://127.0.0.1:%d/health" % client_port, timeout=10
    ) as response:
        health = json.loads(response.read().decode("utf-8"))
    check("GET /health without auth", health.get("ok") is True, health)
    check("health reports headless", health.get("gui") is False, health)
    check("health reports version", health.get("version", "").startswith("1."), health)

    bad = urllib.request.Request(
        client.url,
        data=b'{"jsonrpc":"2.0","id":1,"method":"ping"}',
        headers={"Content-Type": "application/json", "Authorization": "Bearer wrong"},
    )
    status = None
    try:
        urllib.request.urlopen(bad, timeout=10)
    except urllib.error.HTTPError as exc:
        status = exc.code
    check("POST /rpc with a wrong token is rejected", status == 401, status)

    section("tools.list")
    catalogue = client.rpc("tools.list")["result"]["tools"]
    names = {tool["name"] for tool in catalogue}
    expected = {
        "fc_status", "fc_doc_list", "fc_doc_new", "fc_doc_open", "fc_doc_save_as",
        "fc_tree", "fc_object_get", "fc_exec", "fc_recompute", "fc_measure",
        "fc_check", "fc_screenshot", "fc_export", "fc_undo", "fc_redo",
        "fc_sketch_summary", "fc_workbenches", "fc_api_help", "fc_recipes",
        "cam_job_create", "cam_tool_add", "cam_op_add", "cam_inspect",
        "cam_postprocess", "cam_gcode_check",
    }
    check("every planned tool is registered", expected <= names, sorted(expected - names))
    check(
        "every tool has an inputSchema",
        all(isinstance(tool.get("inputSchema"), dict) for tool in catalogue),
    )

    section("fc_status / fc_doc_new")
    status_result = client.call("fc_status")
    check("status is headless", status_result["gui"] is False, status_result)
    check("status has the FreeCAD version", status_result["freecad_version"].startswith("1."))
    check("server reports running", status_result["server"]["running"] is True)

    doc_name = client.call("fc_doc_new", {"name": "BridgeTest"})["document"]["name"]
    check("fc_doc_new created a document", bool(doc_name), doc_name)
    listing = client.call("fc_doc_list")
    check("fc_doc_list sees it", any(d["name"] == doc_name for d in listing["documents"]))

    section("fc_exec builds a 10x10x10 box")
    exec_result = client.call(
        "fc_exec",
        {
            "code": (
                "box = doc.addObject('Part::Box', 'Box')\n"
                "box.Length = 10\nbox.Width = 10\nbox.Height = 10\n"
                "doc.recompute()\n"
                "print('built', box.Name)\n"
                "_result = box.Name\n"
            ),
            "transaction_name": "build box",
        },
    )
    check("fc_exec succeeded", exec_result["ok"] is True, exec_result.get("error"))
    check("fc_exec captured stdout", "built Box" in exec_result["stdout"], exec_result["stdout"])
    check("fc_exec returned _result", exec_result.get("result") == "Box", exec_result)

    section("fc_exec reports failures honestly")
    failing = client.call("fc_exec", {"code": "raise ValueError('boom')"})
    check("failing exec returns ok:false", failing["ok"] is False, failing)
    check("traceback is verbatim", "ValueError: boom" in failing.get("error", ""), failing)

    section("fc_recompute / fc_tree / fc_object_get")
    recompute = client.call("fc_recompute")
    check("recompute reports no errors", recompute["ok"] is True, recompute["errors"])

    tree = client.call("fc_tree")
    tree_names = [node["name"] for node in tree["tree"]]
    check("tree contains the box", "Box" in tree_names, tree_names)
    box_node = [node for node in tree["tree"] if node["name"] == "Box"][0]
    check("tree node has a bbox", box_node["bbox"]["x_length"] == 10.0, box_node["bbox"])

    detail = client.call("fc_object_get", {"name": "Box"})
    check("object type is Part::Box", detail["type"] == "Part::Box", detail["type"])
    check("Length property is exposed", "Length" in detail["properties"], sorted(detail["properties"]))
    check("shape summary has a volume", detail["shape"]["volume_mm3"] == 1000.0, detail["shape"])
    check("shape is not dumped", "Shape" not in detail["properties"])

    section("fc_measure")
    volume = client.call("fc_measure", {"kind": "volume", "objects": ["Box"]})
    check("volume is 1000 mm3", volume["total_volume_mm3"] == 1000.0, volume)
    bbox = client.call("fc_measure", {"kind": "bbox", "objects": ["Box"]})
    check("bbox is 10x10x10", bbox["combined_bbox"]["z_length"] == 10.0, bbox["combined_bbox"])
    com = client.call("fc_measure", {"kind": "center_of_mass", "objects": ["Box"]})
    check("center of mass is (5,5,5)", com["objects"][0]["center_of_mass"]["x"] == 5.0, com)

    section("fc_check")
    checked = client.call("fc_check", {"name": "Box"})
    check("box is valid", checked["ok"] is True, checked["issues"])
    check("box is a closed solid", checked["shape"]["is_closed"] is True, checked["shape"])
    check("fix ran on a copy", checked["fix"]["attempted"] is True, checked["fix"])

    section("fc_sketch_summary")
    client.call(
        "fc_exec",
        {
            "code": (
                "import Part, Sketcher\n"
                "sk = doc.addObject('Sketcher::SketchObject', 'Sketch')\n"
                "sk.addGeometry(Part.LineSegment("
                "App.Vector(0,0,0), App.Vector(20,0,0)), False)\n"
                "sk.addGeometry(Part.LineSegment("
                "App.Vector(20,0,0), App.Vector(20,10,0)), False)\n"
                "sk.addConstraint(Sketcher.Constraint('Coincident', 0, 2, 1, 1))\n"
                "sk.addConstraint(Sketcher.Constraint('Horizontal', 0))\n"
                "doc.recompute()\n"
            ),
            "transaction_name": "build sketch",
        },
    )
    sketch = client.call("fc_sketch_summary", {"name": "Sketch"})
    check("sketch has 2 geometries", sketch["geometry_count"] == 2, sketch["geometry_count"])
    check("sketch has 2 constraints", sketch["constraint_count"] == 2, sketch["constraint_count"])
    check("constraint types are named", sketch["constraints"][0]["type"] == "Coincident", sketch["constraints"])
    check("DoF is reported", isinstance(sketch["dof"], int), sketch["dof"])
    check("not fully constrained yet", sketch["fully_constrained"] is False, sketch)

    section("fc_export")
    stl_path = os.path.join(_TEMP, "bridge-test-box.stl")
    if os.path.exists(stl_path):
        os.remove(stl_path)
    export = client.call(
        "fc_export", {"objects": ["Box"], "format": "stl", "path": stl_path}
    )
    check("STL was written", os.path.exists(stl_path), stl_path)
    check("STL is not empty", export["size_bytes"] > 100, export)
    guarded = client.rpc(
        "tools.call",
        {
            "name": "fc_export",
            "arguments": {"objects": ["Box"], "format": "stl", "path": stl_path},
        },
    )
    check("existing files are not overwritten", "error" in guarded, guarded)

    section("fc_screenshot is honest about headless")
    shot = client.call("fc_screenshot")
    check("screenshot unavailable headless", shot["available"] is False, shot)
    check("reason explains why", "headless" in shot["reason"], shot)

    section("fc_undo removes the box")
    before = client.call("fc_tree")
    check("box present before undo", any(n["name"] == "Box" for n in before["tree"]))
    undone = client.call("fc_undo", {"steps": 2})  # sketch, then the box
    check("undo reported steps", len(undone["undone"]) == 2, undone["undone"])
    after = client.call("fc_tree")
    check(
        "box is gone after undo",
        not any(node["name"] == "Box" for node in after["tree"]),
        [node["name"] for node in after["tree"]],
    )
    redone = client.call("fc_redo", {"steps": 2})
    check("redo restored it", redone["ok"] is True, redone)
    restored = client.call("fc_tree")
    check(
        "box is back after redo",
        any(node["name"] == "Box" for node in restored["tree"]),
        [node["name"] for node in restored["tree"]],
    )

    section("knowledge tools")
    workbenches = client.call("fc_workbenches")
    by_name = {entry["name"]: entry for entry in workbenches["workbenches"]}
    check("PartDesign is importable", by_name["PartDesign"]["importable"] is True, by_name["PartDesign"])
    check("Assembly is importable", by_name["Assembly"]["importable"] is True, by_name["Assembly"])
    check("TechDraw is importable", by_name["TechDraw"]["importable"] is True, by_name["TechDraw"])
    check("Path (CAM) is importable", by_name["Path"]["importable"] is True, by_name["Path"])
    check("TechDraw was really loaded", by_name["TechDraw"]["loaded"] is True, by_name["TechDraw"])
    check(
        "Draft is reported without being imported",
        by_name["Draft"]["importable"] is True and by_name["Draft"]["loaded"] is False,
        by_name["Draft"],
    )
    unsafe = client.rpc(
        "tools.call", {"name": "fc_api_help", "arguments": {"module": "Draft"}}
    )
    check("importing Draft headless is refused", "error" in unsafe, unsafe)

    pad_help = client.call("fc_api_help", {"name": "PartDesign::Pad"})
    pad_props = {item["name"] for item in pad_help["properties"]}
    check("Pad has a Length property", "Length" in pad_props, sorted(pad_props)[:10])
    check("property docs came through", any(item.get("doc") for item in pad_help["properties"]))
    after_probe = client.call("fc_tree")
    check(
        "the type probe left nothing behind",
        not any("Probe" in node["name"] for node in after_probe["tree"]),
        [node["name"] for node in after_probe["tree"]],
    )
    module_help = client.call("fc_api_help", {"module": "Part", "query": "makebox"})
    check("module query finds makeBox", any(
        item["name"] == "makeBox" for item in module_help["attributes"]), module_help)

    recipes = client.call("fc_recipes", {"workbench": "partdesign"})
    check("partdesign recipe has content", "PartDesign::Pad" in recipes["content"])
    check("recipes lists all workbenches", {"assembly", "cam", "partdesign", "techdraw"}
          <= set(recipes["available"]), recipes["available"])

    section("fc_doc_save_as")
    save_path = os.path.join(_TEMP, "bridge-test.FCStd")
    if os.path.exists(save_path):
        os.remove(save_path)
    saved = client.call("fc_doc_save_as", {"path": save_path})
    check("document was saved", os.path.exists(save_path), saved)
    check("save reports the path", saved["path"] == save_path, saved)
    reopened = client.rpc(
        "tools.call",
        {"name": "fc_doc_save_as", "arguments": {"path": save_path}},
    )
    check("save_as refuses to overwrite", "error" in reopened, reopened)

    section("error mapping")
    missing = client.rpc("tools.call", {"name": "fc_object_get", "arguments": {"name": "Nope"}})
    check("unknown object becomes a JSON-RPC error", "error" in missing, missing)
    check("error code is -32000", missing.get("error", {}).get("code") == -32000, missing)
    unknown = client.rpc("tools.call", {"name": "fc_nope", "arguments": {}})
    check("unknown tool is an error", "error" in unknown, unknown)
    bad_method = client.rpc("nope.nope")
    check("unknown method is -32601", bad_method["error"]["code"] == -32601, bad_method)

    run_cam_checks(client)


def run_cam_checks(client):
    """Job on a box -> one pocket -> GRBL G-code -> the pure parser accepts it."""
    section("CAM: job, tool, pocket")
    cam_doc = client.call("fc_doc_new", {"name": "CamTest"})["document"]["name"]
    client.call(
        "fc_exec",
        {
            "doc": cam_doc,
            "code": (
                "box = doc.addObject('Part::Box', 'Stock')\n"
                "box.Length = 40\nbox.Width = 40\nbox.Height = 10\n"
                "doc.recompute()\n"
                # The pocket needs the top face, whose index is not guaranteed.
                "top = [i + 1 for i, f in enumerate(box.Shape.Faces)\n"
                "       if abs(f.BoundBox.ZMin - 10) < 1e-6"
                " and f.BoundBox.ZLength < 1e-6]\n"
                "_result = top[0]\n"
            ),
            "transaction_name": "cam stock",
        },
    )
    top_face = client.call("fc_object_get", {"doc": cam_doc, "name": "Stock"})
    check("stock box is 40x40x10", top_face["shape"]["volume_mm3"] == 16000.0, top_face["shape"])

    job = client.call("cam_job_create", {"doc": cam_doc, "models": ["Stock"], "post": "grbl"})
    check("job was created", job["job"].startswith("Job"), job)
    check(
        "stock hugs the model",
        job["stock"]["z_max"] == 10.0 and job["stock"]["x_max"] == 40.0
        and job["stock"]["x_min"] == 0.0,
        job["stock"],
    )
    check("job starts with one tool controller", len(job["tools"]) == 1, job["tools"])

    tool = client.call(
        "cam_tool_add",
        {
            "doc": cam_doc, "job": job["job"], "reuse": job["tools"][0]["name"],
            "label": "T1 endmill 3", "number": 1, "diameter": 3.0,
            "horiz_feed": 600, "vert_feed": 200, "horiz_rapid": 2000,
            "vert_rapid": 800, "spindle_speed": 10000,
        },
    )
    check("tool diameter is 3 mm", tool["bit"]["diameter_mm"] == 3.0, tool["bit"])
    # Velocities are mm/s internally: a bare 600 would post as F36000.
    check("feed is stored as 600 mm/min", tool["feeds"]["horiz_mm_min"] == 600.0, tool["feeds"])

    faces = client.call(
        "fc_exec",
        {
            "doc": cam_doc,
            "code": (
                "clone = doc.getObject(%r).Model.Group[0]\n"
                "_result = [i + 1 for i, f in enumerate(clone.Shape.Faces)\n"
                "           if abs(f.BoundBox.ZMin - 10) < 1e-6"
                " and f.BoundBox.ZLength < 1e-6][0]\n" % job["job"]
            ),
        },
    )
    face_index = int(faces["result"])
    clone_name = client.call("fc_exec", {
        "doc": cam_doc,
        "code": "_result = doc.getObject(%r).Model.Group[0].Name\n" % job["job"],
    })["result"]

    op = client.call(
        "cam_op_add",
        {
            "doc": cam_doc, "job": job["job"], "type": "pocket_shape",
            "name": "op1_pocket", "tool": tool["controller"],
            "base": [{"object": clone_name, "subs": ["Face%d" % face_index]}],
            "properties": {
                "StartDepth": 10.0, "FinalDepth": 8.0, "StepDown": 1.0,
                "StepOver": 50, "SafeHeight": 12.0, "ClearanceHeight": 15.0,
                "UseOutline": True,
            },
        },
    )
    check("the pocket produced a toolpath", op.get("commands", 0) > 10, op)
    # Without clearing the SetupSheet expressions the depths silently revert.
    check("FinalDepth stuck at 8 mm", op.get("FinalDepth") == 8.0, op)
    check("expressions were cleared", "FinalDepth" in op["cleared_expressions"], op)

    section("cam_inspect")
    inspection = client.call("cam_inspect", {"doc": cam_doc, "job": job["job"]})
    check("inspect is happy", inspection["ok"] is True, inspection["issues"])
    check("one operation is listed", len(inspection["operations"]) == 1, inspection["operations"])
    check("a time estimate came back", inspection["estimated_seconds"] > 0, inspection)

    section("cam_postprocess")
    gcode_dir = os.path.join(_TEMP, "cam")
    gcode_path = os.path.join(gcode_dir, "cam-test-T1.gcode")
    if os.path.exists(gcode_path):
        os.remove(gcode_path)
    posted = client.call(
        "cam_postprocess",
        {
            "doc": cam_doc, "job": job["job"], "post": "grbl", "split_by": "tool",
            "output_dir": gcode_dir, "files": ["cam-test-T1.gcode"],
        },
    )
    check("one file per tool", len(posted["files"]) == 1, posted["files"])
    check("the file exists", os.path.exists(gcode_path), gcode_path)
    check("and has real content", posted["files"][0]["lines"] > 20, posted["files"][0])
    guarded = client.rpc(
        "tools.call",
        {
            "name": "cam_postprocess",
            "arguments": {
                "doc": cam_doc, "job": job["job"], "split_by": "tool",
                "output_dir": gcode_dir, "files": ["cam-test-T1.gcode"],
            },
        },
    )
    check("existing G-code is not overwritten", "error" in guarded, guarded)

    section("cam_gcode_check")
    verdict = client.call(
        "cam_gcode_check",
        {
            "path": gcode_path,
            "bounds": {"x_min": -1.0, "x_max": 41.0, "y_min": -1.0, "y_max": 41.0},
            "z_floor": 7.9, "safe_height": 12.0, "tools": [1],
        },
    )
    check("the posted G-code passes", verdict["ok"] is True, verdict["issues"])
    check("it measured the floor", verdict["z_min"] == 8.0, verdict)
    check("it found the tool number", verdict["tools"] == [1], verdict)
    check("the spindle is on before the cut", verdict["spindle_on_before_first_cut"] is True,
          verdict)

    too_deep = client.call(
        "cam_gcode_check",
        {"path": gcode_path, "z_floor": 9.5, "safe_height": 12.0},
    )
    check("a violated depth limit is caught", too_deep["ok"] is False, too_deep["issues"])

    handmade = client.call(
        "cam_gcode_check",
        {
            "text": "G21 G90\nG0 X0 Y0 Z5\nG1 X10 Y0 Z-1 F300\nG0 X20 Y20\nM5\n",
            "z_floor": -2.0, "safe_height": 5.0, "tools": [1],
        },
    )
    check(
        "the pure parser flags an unsafe rapid, a missing spindle and no tool",
        handmade["ok"] is False and len(handmade["issues"]) >= 3,
        handmade["issues"],
    )
    arc = client.call(
        "cam_gcode_check",
        {"text": "G21 G90\nM3 S1000\nG0 X10 Y0 Z5\nG1 Z-1 F100\n"
                 "G3 X-10 Y0 I-10 J0 F300\n", "safe_height": 5.0},
    )
    # Endpoints alone would say Y never leaves 0; the arc sweeps up to Y=10.
    check("arcs are bounded by their sweep, not their end points",
          arc["y_max"] == 10.0, arc)


print("=== AI Bridge headless proof (in-process, Flatpak FreeCAD) ===")
import FreeCAD as App  # noqa: E402

print("FreeCAD build:", App.Version()[:3])

# Bind an ephemeral port so a running GUI session on 8765 does not clash.
state = server.start(port=0)
client_port = state["port"]
config = server.read_config()
print("server:", state)

client = Client(client_port, config["token"])
worker_error = []
finished = threading.Event()

# FreeCAD's document API is not thread safe even headless (saveAs from a worker
# thread segfaults), so this main thread pumps the tool calls while the checks
# run from a worker — mirroring how the GUI marshals to the Qt event loop.
dispatch.install_main_thread_pump()


def worker():
    try:
        run_checks(client)
    except Exception:
        worker_error.append(traceback.format_exc())
    finally:
        finished.set()


thread = threading.Thread(target=worker, name="checks", daemon=True)
thread.start()
completed = dispatch.pump_until(finished, timeout=600)
dispatch.uninstall_main_thread_pump()
if not completed:
    print("\nFAILED: the check thread did not finish in 600 s")
    server.stop()
    sys.exit(1)
thread.join(timeout=10)

server.stop()

if worker_error:
    print("\nUNEXPECTED ERROR:\n%s" % worker_error[0])
    sys.exit(1)

print("\n=== %d checks run, %d failed ===" % (_CHECKS[0], len(_FAILURES)))
if _FAILURES:
    for failure in _FAILURES:
        print("  - %s" % failure)
    sys.exit(1)
print("=== ALL HEADLESS CHECKS PASSED ===")
