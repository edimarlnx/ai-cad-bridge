#!/usr/bin/env python3
"""Host-side test of bridge/freecad_mcp.py — no FreeCAD involved.

Runs a fake add-on HTTP server implementing ``tools.list`` / ``tools.call``,
spawns the MCP server as a subprocess, and drives the real protocol over pipes:
initialize, notifications/initialized, ping, tools/list, tools/call (success and
tool error), resources/list, resources/read, an unknown method, and the offline
fallback when no add-on is listening.

    python3 tests/test_mcp_stdio.py
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
MCP_SCRIPT = os.path.join(_REPO, "bridge", "freecad_mcp.py")

TOKEN = "test-token-abcdefghijklmnopqrstuvwxyz"

FAKE_TOOLS = [
    {
        "name": "fc_status",
        "description": "fake status",
        "inputSchema": {"type": "object", "properties": {}},
        "mutating": False,
    },
    {
        "name": "fc_exec",
        "description": "fake exec",
        "inputSchema": {
            "type": "object",
            "properties": {"code": {"type": "string"}},
            "required": ["code"],
        },
        "mutating": True,
    },
]

_FAILURES = []
_CHECKS = [0]


def check(label, condition, detail=""):
    _CHECKS[0] += 1
    if condition:
        print("  ok   %s" % label)
    else:
        print("  FAIL %s %s" % (label, detail))
        _FAILURES.append("%s %s" % (label, detail))


def section(title):
    print("\n--- %s ---" % title)


class FakeAddonHandler(BaseHTTPRequestHandler):
    """Minimal stand-in for the in-FreeCAD server."""

    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        return

    def do_POST(self):  # noqa: N802
        auth = self.headers.get("Authorization", "")
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        request = json.loads(body.decode("utf-8"))
        self.server.seen.append((auth, request))
        if auth != "Bearer %s" % TOKEN:
            self._reply(401, {"error": "unauthorized"})
            return
        method = request.get("method")
        request_id = request.get("id")
        if method == "tools.list":
            self._reply(200, {"jsonrpc": "2.0", "id": request_id, "result": {"tools": FAKE_TOOLS}})
            return
        if method == "tools.call":
            name = request["params"]["name"]
            if name == "fc_status":
                self._reply(
                    200,
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {"gui": False, "freecad_version": "1.1.3"},
                    },
                )
                return
            self._reply(
                200,
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {
                        "code": -32000,
                        "message": "object 'Nope' not found",
                        "data": {"traceback": "…"},
                    },
                },
            )
            return
        self._reply(
            200,
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": "unknown"},
            },
        )

    def _reply(self, status, payload):
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class McpClient:
    """Drives the MCP server over its stdio pipes."""

    def __init__(self, config_dir):
        env = dict(os.environ)
        env["AIBRIDGE_CONFIG_DIR"] = config_dir
        self.process = subprocess.Popen(
            [sys.executable, MCP_SCRIPT],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            text=True,
            bufsize=1,
        )
        self.next_id = 0
        self.stderr_lines = []
        threading.Thread(target=self._drain_stderr, daemon=True).start()

    def _drain_stderr(self):
        for line in self.process.stderr:
            self.stderr_lines.append(line.rstrip())

    def send(self, message):
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def request(self, method, params=None):
        self.next_id += 1
        self.send(
            {
                "jsonrpc": "2.0",
                "id": self.next_id,
                "method": method,
                "params": params or {},
            }
        )
        line = self.process.stdout.readline()
        if not line:
            raise AssertionError(
                "no response to %s; stderr:\n%s" % (method, "\n".join(self.stderr_lines))
            )
        return json.loads(line)

    def notify(self, method, params=None):
        self.send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def close(self):
        try:
            self.process.stdin.close()
        except Exception:
            pass
        try:
            self.process.wait(timeout=10)
        except Exception:
            self.process.kill()


def write_config(directory, port, token=TOKEN):
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, "config.json"), "w", encoding="utf-8") as handle:
        json.dump({"port": port, "token": token}, handle)


def run_online_checks(config_dir, addon):
    client = McpClient(config_dir)
    try:
        section("initialize")
        answer = client.request("initialize", {"protocolVersion": "2025-06-18"})
        result = answer["result"]
        check("response echoes the id", answer["id"] == 1, answer)
        check("protocolVersion is 2025-06-18", result["protocolVersion"] == "2025-06-18", result)
        check("declares the tools capability", "tools" in result["capabilities"], result)
        check("declares the resources capability", "resources" in result["capabilities"], result)
        check("serverInfo has a name", result["serverInfo"]["name"] == "freecad", result)
        check("instructions describe the loop", "fc_recompute" in result["instructions"])

        section("notifications and ping")
        client.notify("notifications/initialized")
        pong = client.request("ping")
        check("ping answered after the notification", pong["result"] == {}, pong)
        check("the notification produced no reply", pong["id"] == 2, pong)

        section("tools/list is proxied")
        listing = client.request("tools/list")["result"]["tools"]
        names = [tool["name"] for tool in listing]
        check("both fake tools came through", names == ["fc_status", "fc_exec"], names)
        check(
            "inputSchema is preserved",
            listing[1]["inputSchema"]["required"] == ["code"],
            listing[1],
        )
        check(
            "the bearer token was sent",
            any(auth == "Bearer %s" % TOKEN for auth, _ in addon.seen),
            [auth for auth, _ in addon.seen],
        )

        section("tools/call success")
        called = client.request(
            "tools/call", {"name": "fc_status", "arguments": {}}
        )["result"]
        check("no isError on success", "isError" not in called, called)
        check("content is one text block", called["content"][0]["type"] == "text", called)
        payload = json.loads(called["content"][0]["text"])
        check("payload is the add-on result", payload["freecad_version"] == "1.1.3", payload)

        section("tools/call error becomes isError")
        failed = client.request(
            "tools/call", {"name": "fc_object_get", "arguments": {"name": "Nope"}}
        )["result"]
        check("isError is set", failed.get("isError") is True, failed)
        text = failed["content"][0]["text"]
        check("the message survives", "not found" in text, text)
        check("it is still a normal result, not a JSON-RPC error", "error" not in failed)

        section("resources")
        resources = client.request("resources/list")["result"]["resources"]
        uris = {item["uri"] for item in resources}
        check(
            "recipes are exposed as resources",
            {"freecad://recipes/partdesign", "freecad://recipes/cam"} <= uris,
            sorted(uris),
        )
        content = client.request(
            "resources/read", {"uri": "freecad://recipes/partdesign"}
        )["result"]["contents"][0]
        check("recipe content is markdown", content["mimeType"] == "text/markdown", content)
        check("recipe mentions Pad", "PartDesign::Pad" in content["text"])
        missing = client.request("resources/read", {"uri": "freecad://recipes/nope"})
        check("unknown recipe is -32602", missing["error"]["code"] == -32602, missing)

        section("unknown method")
        unknown = client.request("tools/nope")
        check("unknown method is -32601", unknown["error"]["code"] == -32601, unknown)

        section("stdout stayed clean")
        check("server logged to stderr", any("ready" in line for line in client.stderr_lines),
              client.stderr_lines)
    finally:
        client.close()


def run_offline_checks(config_dir):
    """No add-on listening: the session must degrade, not break."""
    client = McpClient(config_dir)
    try:
        section("offline fallback")
        client.request("initialize", {"protocolVersion": "2025-06-18"})
        listing = client.request("tools/list")["result"]["tools"]
        names = {tool["name"] for tool in listing}
        check("the static catalogue is returned", "fc_exec" in names and len(names) >= 19, sorted(names))
        check("the CAM tools are in it too", "cam_gcode_check" in names, sorted(names))
        check(
            "descriptions say the bridge is offline",
            all("offline" in tool["description"] for tool in listing),
        )
        # A client caches this catalogue for the session, so the offline schemas
        # must be typed exactly like the live ones or array arguments arrive as
        # strings from then on.
        by_name = {tool["name"]: tool for tool in listing}
        measure = by_name.get("fc_measure", {}).get("inputSchema", {})
        check(
            "offline schemas are the generated, typed ones",
            (measure.get("properties", {}).get("objects", {}).get("type") == "array"
             and measure.get("required") == ["kind", "objects"]),
            measure,
        )
        called = client.request("tools/call", {"name": "fc_status", "arguments": {}})["result"]
        check("call reports isError", called.get("isError") is True, called)
        check(
            "and explains how to start the bridge",
            "AI Bridge" in called["content"][0]["text"],
            called["content"][0]["text"],
        )
    finally:
        client.close()


def main():
    print("=== MCP stdio server checks (host python3, fake add-on) ===")
    addon = ThreadingHTTPServer(("127.0.0.1", 0), FakeAddonHandler)
    addon.seen = []
    addon.daemon_threads = True
    threading.Thread(target=addon.serve_forever, daemon=True).start()
    port = addon.server_address[1]
    print("fake add-on on 127.0.0.1:%d" % port)

    with tempfile.TemporaryDirectory() as workdir:
        online_dir = os.path.join(workdir, "online")
        write_config(online_dir, port)
        run_online_checks(online_dir, addon)

        offline_dir = os.path.join(workdir, "offline")
        # A port nothing listens on: the add-on is "installed" but stopped.
        write_config(offline_dir, 1)
        run_offline_checks(offline_dir)

    addon.shutdown()
    print("\n=== %d checks run, %d failed ===" % (_CHECKS[0], len(_FAILURES)))
    if _FAILURES:
        for failure in _FAILURES:
            print("  - %s" % failure)
        return 1
    print("=== ALL MCP STDIO CHECKS PASSED ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
