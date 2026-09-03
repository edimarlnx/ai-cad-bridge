"""Loopback HTTP JSON-RPC server exposed by the AiBridge FreeCAD add-on.

The server binds 127.0.0.1 only and requires a bearer token that lives in
``~/.config/freecad-ai-bridge/config.json`` (mode 0600, created on first start).
The host-side MCP server reads the same file, so registration needs no manual
copy of secrets.

Endpoints
---------
``GET /health``  -- no auth, returns ``{ok, gui, version}`` so a caller can tell
                    whether FreeCAD is up and whether a GUI is attached.
``POST /rpc``    -- JSON-RPC 2.0. Methods: ``ping``, ``tools.list``,
                    ``tools.call`` (``{name, arguments}``).

This module is importable under FreeCADCmd (headless) with no Qt: it only pulls
``dispatch`` which itself imports FreeCAD lazily.
"""

import json
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import dispatch

DEFAULT_PORT = 8765
# AIBRIDGE_CONFIG_DIR exists so the test suite can run without touching the
# user's real config (and therefore without stealing their port/token).
CONFIG_DIR = os.environ.get("AIBRIDGE_CONFIG_DIR") or os.path.join(
    # Deliberately NOT XDG_CONFIG_HOME: inside the FreeCAD Flatpak it points at
    # ~/.var/app/org.freecad.FreeCAD/config, so the host bridge would never find
    # the file. HOME is the real home on both sides (the sandbox has host access).
    os.path.expanduser("~/.config"),
    "freecad-ai-bridge",
)
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")

# Maximum accepted request body (a big fc_exec payload is still tiny).
MAX_BODY_BYTES = 4 * 1024 * 1024

# JSON-RPC error codes: standard ones plus -32000 for tool/runtime failures.
E_PARSE = -32700
E_INVALID_REQUEST = -32600
E_METHOD_NOT_FOUND = -32601
E_INVALID_PARAMS = -32602
E_INTERNAL = -32603
E_TOOL = -32000

_state = {
    "httpd": None,
    "thread": None,
    "port": None,
    "token": None,
    "started_at": None,
}
_lock = threading.RLock()


def config_path():
    """Absolute path of the shared config file (port + token)."""
    return CONFIG_PATH


def read_config():
    """Return the stored ``{port, token}`` config, or ``{}`` when absent."""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_config(config):
    """Persist the config with owner-only permissions."""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    try:
        os.chmod(CONFIG_DIR, 0o700)
    except OSError:
        pass
    tmp_path = CONFIG_PATH + ".tmp"
    # Create with 0600 from the start so the token is never world readable.
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(config, handle, indent=2)
        handle.write("\n")
    os.replace(tmp_path, CONFIG_PATH)
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except OSError:
        pass


def ensure_config(port=None):
    """Load (or create) the config, honouring ``AIBRIDGE_PORT`` and ``port``."""
    config = read_config()
    token = config.get("token")
    if not isinstance(token, str) or len(token) < 20:
        token = secrets.token_urlsafe(32)
    if port is None:
        env_port = os.environ.get("AIBRIDGE_PORT")
        if env_port:
            try:
                port = int(env_port)
            except ValueError:
                port = None
    if port is None:
        port = config.get("port") or DEFAULT_PORT
    config = {"port": int(port), "token": token}
    _write_config(config)
    return config


def _gui_up():
    """True when a FreeCAD GUI main window exists."""
    return dispatch.gui_module() is not None


def _freecad_version():
    try:
        import FreeCAD

        return ".".join(str(part) for part in FreeCAD.Version()[:3])
    except Exception:
        return "unknown"


class _Handler(BaseHTTPRequestHandler):
    """Tiny JSON-RPC handler. One instance per request (threaded server)."""

    server_version = "AiBridge/0.1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        """Silence the default stderr access log (it pollutes FreeCAD's console)."""
        return

    # -- helpers ---------------------------------------------------------
    def _send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def _authorized(self):
        header = self.headers.get("Authorization", "")
        prefix = "Bearer "
        if not header.startswith(prefix):
            return False
        expected = _state.get("token") or ""
        return bool(expected) and secrets.compare_digest(
            header[len(prefix):].strip(), expected
        )

    # -- routes ----------------------------------------------------------
    def do_GET(self):  # noqa: N802 (BaseHTTPRequestHandler API)
        if self.path.split("?")[0] != "/health":
            self._send_json(404, {"error": "not found"})
            return
        self._send_json(
            200,
            {
                "ok": True,
                "gui": _gui_up(),
                "version": _freecad_version(),
            },
        )

    def do_POST(self):  # noqa: N802 (BaseHTTPRequestHandler API)
        if self.path.split("?")[0] != "/rpc":
            self._send_json(404, {"error": "not found"})
            return
        if not self._authorized():
            self._send_json(401, {"error": "unauthorized"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > MAX_BODY_BYTES:
            self._send_json(413, {"error": "request too large"})
            return
        raw = self.rfile.read(length) if length else b""
        try:
            request = json.loads(raw.decode("utf-8")) if raw else None
        except Exception as exc:
            self._send_json(200, _error(None, E_PARSE, "invalid JSON: %s" % exc))
            return
        if not isinstance(request, dict):
            self._send_json(
                200, _error(None, E_INVALID_REQUEST, "request must be an object")
            )
            return
        self._send_json(200, handle_rpc(request))


def _error(request_id, code, message, data=None):
    """Build a JSON-RPC error envelope."""
    error = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def _result(request_id, result):
    """Build a JSON-RPC success envelope."""
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def handle_rpc(request):
    """Execute one JSON-RPC request object and return the response object."""
    request_id = request.get("id")
    method = request.get("method")
    params = request.get("params") or {}
    if not isinstance(params, dict):
        return _error(request_id, E_INVALID_PARAMS, "params must be an object")

    if method == "ping":
        return _result(request_id, {"pong": True, "uptime_s": uptime_seconds()})

    if method == "tools.list":
        return _result(request_id, {"tools": dispatch.list_tools()})

    if method == "tools.call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(name, str) or not name:
            return _error(request_id, E_INVALID_PARAMS, "missing tool name")
        if not isinstance(arguments, dict):
            return _error(request_id, E_INVALID_PARAMS, "arguments must be an object")
        try:
            return _result(request_id, dispatch.call_tool(name, arguments))
        except dispatch.ToolError as exc:
            return _error(request_id, E_TOOL, str(exc), exc.data)
        except Exception as exc:  # pragma: no cover - defensive
            import traceback

            return _error(
                request_id,
                E_INTERNAL,
                "%s: %s" % (type(exc).__name__, exc),
                {"traceback": traceback.format_exc()},
            )

    return _error(request_id, E_METHOD_NOT_FOUND, "unknown method: %r" % (method,))


# -- lifecycle -----------------------------------------------------------
def start(port=None):
    """Start the server in a background thread. Idempotent."""
    with _lock:
        if _state["httpd"] is not None:
            return status()
        config = ensure_config(port=port)
        # With a GUI, park the main-thread invoker now, while we ARE on the
        # main thread (the workbench command), so worker threads only emit.
        try:
            import FreeCADGui

            if FreeCADGui.getMainWindow() is not None:
                dispatch._main_thread_invoker()
        except Exception:
            pass
        httpd = ThreadingHTTPServer(("127.0.0.1", config["port"]), _Handler)
        httpd.daemon_threads = True
        # A port of 0 means "pick one"; record the real one in the config.
        real_port = httpd.server_address[1]
        if real_port != config["port"]:
            config["port"] = real_port
            _write_config(config)
        thread = threading.Thread(
            target=httpd.serve_forever, name="AiBridgeServer", daemon=True
        )
        thread.start()
        _state.update(
            {
                "httpd": httpd,
                "thread": thread,
                "port": real_port,
                "token": config["token"],
                "started_at": time.time(),
            }
        )
        _log("listening on http://127.0.0.1:%d (config: %s)" % (real_port, CONFIG_PATH))
        return status()


def stop():
    """Stop the server if running. Idempotent."""
    with _lock:
        httpd = _state["httpd"]
        if httpd is None:
            return status()
        httpd.shutdown()
        httpd.server_close()
        thread = _state["thread"]
        _state.update(
            {"httpd": None, "thread": None, "port": None, "started_at": None}
        )
    if thread is not None:
        thread.join(timeout=5)
    _log("stopped")
    return status()


def serve(port=None):
    """Headless daemon mode: start, then pump tool calls on THIS thread forever.

    FreeCADCmd has no Qt event loop, and FreeCAD's document API is not thread
    safe, so a headless session must give the bridge its main thread::

        flatpak run --command=FreeCADCmd org.freecad.FreeCAD serve.py
        # serve.py:  import AiBridge; AiBridge.serve()

    Blocks until interrupted.
    """
    state = start(port=port)
    dispatch.install_main_thread_pump()
    stop_event = threading.Event()
    try:
        _log("serving on the main thread; press Ctrl+C to stop")
        dispatch.pump_until(stop_event)
    except KeyboardInterrupt:
        _log("interrupted")
    finally:
        dispatch.uninstall_main_thread_pump()
        stop()
    return state


def is_running():
    """True when the background server thread is serving."""
    return _state["httpd"] is not None


def uptime_seconds():
    """Seconds since the server started, or 0 when stopped."""
    started = _state["started_at"]
    return round(time.time() - started, 3) if started else 0


def status():
    """Server status dictionary (never contains the token)."""
    return {
        "running": is_running(),
        "port": _state["port"],
        "uptime_s": uptime_seconds(),
        "config_path": CONFIG_PATH,
        "gui": _gui_up(),
    }


def _log(message):
    """Print through FreeCAD's console when available, else stdout."""
    text = "[AI Bridge] %s\n" % message
    try:
        import FreeCAD

        FreeCAD.Console.PrintMessage(text)
    except Exception:
        print(text, end="")
