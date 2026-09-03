"""Tool registry and main-thread dispatch for the AiBridge add-on.

Two responsibilities:

1. **Thread affinity.** FreeCAD's document API is not thread safe — not even
   headless, where ``Document.saveAs`` from a worker thread segfaults the
   process (measured on 1.1.3). HTTP requests arrive on worker threads, so the
   handler is always marshalled to the main thread:

   * with a GUI up, through the Qt event loop (queued signal to a main-thread QObject);
   * headless, through a queue that the main thread pumps
     (``install_main_thread_pump`` + ``pump_until``/``run_pump``, which is what
     ``server.serve()`` and the headless test use).

   The worker blocks on a ``threading.Event`` until the main thread answers or
   ``MAIN_THREAD_TIMEOUT`` elapses. Only when no pump is installed and no GUI
   exists does the handler run inline (a direct in-process call).

2. **Transactions.** Every tool declared ``mutating`` runs inside
   ``doc.openTransaction(name)`` / ``commitTransaction()``. The transaction is
   aborted when the handler raises, and — for tools flagged
   ``abort_on_failure`` — also when the handler returns ``{"ok": False, ...}``.
   ``fc_exec`` reports failures that way, since the agent must read the
   traceback rather than an HTTP error, yet a half-built step must not persist.
"""

import queue
import json
import threading
import time
import traceback

MAIN_THREAD_TIMEOUT = 120.0

_registry = None
_registry_lock = threading.RLock()
_main_queue = None


class ToolError(Exception):
    """Raised by tool handlers for expected, user-facing failures."""

    def __init__(self, message, data=None):
        super().__init__(message)
        self.data = data


def gui_module():
    """Return the ``FreeCADGui`` module when a real GUI is up, else ``None``.

    ``FreeCAD.GuiUp`` is checked FIRST and the import is skipped when it is
    false. Importing ``FreeCADGui`` under FreeCADCmd half-registers the Qt
    resource system, and any later ``import Draft`` then SIGSEGVs while it reads
    ``:/ui/preferences-*.ui`` — which is exactly what CAM's ``Job.Create`` does
    (it lazily calls ``Draft.clone``). Measured on FreeCAD 1.1.3: with the
    import, cam_job_create crashes the process; without it, it works.
    """
    try:
        import FreeCAD

        if not FreeCAD.GuiUp:
            return None
    except Exception:
        return None
    try:
        import FreeCADGui
    except Exception:
        return None
    try:
        if FreeCADGui.getMainWindow() is None:
            return None
    except Exception:
        return None
    return FreeCADGui


def registry():
    """Lazily build and return the ``{name: tool}`` registry."""
    global _registry
    with _registry_lock:
        if _registry is None:
            import tools

            _registry = tools.build_registry()
        return _registry


def list_tools():
    """Public tool catalogue: name, description and JSON Schema for the input."""
    catalogue = []
    for name in sorted(registry()):
        tool = registry()[name]
        catalogue.append(
            {
                "name": tool["name"],
                "description": tool["description"],
                "inputSchema": tool["inputSchema"],
                "mutating": bool(tool.get("mutating")),
            }
        )
    return catalogue


def call_tool(name, params):
    """Run one tool by name; returns the handler's dictionary result."""
    tool = registry().get(name)
    if tool is None:
        raise ToolError("unknown tool: %s" % name)
    params = _coerce_params(tool, params)

    def run():
        return _invoke(tool, params)

    if threading.current_thread() is threading.main_thread():
        return run()
    if gui_module() is not None:
        return _run_on_main_thread(run)
    if _main_queue is not None:
        return _run_via_pump(run)
    # No GUI and no pump: nothing else can run this, so run it here.
    return run()


def _coerce_params(tool, params):
    """Parse JSON strings for params the schema types as non-strings.

    An MCP client that connected while the bridge was offline only knows an
    untyped schema and may send ``"[\"Box\"]"`` for an array parameter. Being
    lenient here costs nothing and turns a confusing failure into a call that
    just works.
    """
    if not isinstance(params, dict):
        return params
    properties = (tool.get("inputSchema") or {}).get("properties") or {}
    fixed = dict(params)
    for key, value in params.items():
        if not isinstance(value, str):
            continue
        expected = (properties.get(key) or {}).get("type")
        if expected in ("array", "object", "number", "integer", "boolean"):
            text = value.strip()
            if expected == "boolean" and text.lower() in ("true", "false"):
                fixed[key] = text.lower() == "true"
                continue
            try:
                parsed = json.loads(text)
            except ValueError:
                continue
            if expected == "array" and isinstance(parsed, list):
                fixed[key] = parsed
            elif expected == "object" and isinstance(parsed, dict):
                fixed[key] = parsed
            elif expected in ("number", "integer") and isinstance(
                parsed, (int, float)
            ):
                fixed[key] = parsed
    return fixed


# -- headless main-thread pump -------------------------------------------
def install_main_thread_pump():
    """Route tool calls to the thread that pumps the queue (headless sessions)."""
    global _main_queue
    if _main_queue is None:
        _main_queue = queue.Queue()
    return _main_queue


def uninstall_main_thread_pump():
    """Stop routing; queued jobs already waiting are dropped."""
    global _main_queue
    _main_queue = None


def pump_once(timeout=0.05):
    """Run at most one queued job on the calling thread. True when one ran."""
    pending = _main_queue
    if pending is None:
        return False
    try:
        job = pending.get(timeout=timeout)
    except queue.Empty:
        return False
    job()
    return True


def pump_until(stop_event, timeout=None):
    """Pump jobs on the calling thread until ``stop_event`` is set."""
    deadline = None if timeout is None else time.time() + timeout
    while not stop_event.is_set():
        pump_once(0.05)
        if deadline is not None and time.time() > deadline:
            return False
    # Drain whatever arrived in the last instant.
    while pump_once(0.0):
        pass
    return True


def _run_via_pump(function):
    """Hand ``function`` to the pumping thread and wait for its result."""
    box = {}
    done = threading.Event()

    def runner():
        try:
            box["result"] = function()
        except BaseException as exc:  # noqa: BLE001 - re-raised on the caller
            box["error"] = exc
            box["traceback"] = traceback.format_exc()
        finally:
            done.set()

    pending = _main_queue
    if pending is None:  # uninstalled between the check and here
        return function()
    pending.put(runner)
    if not done.wait(MAIN_THREAD_TIMEOUT):
        raise ToolError(
            "timed out after %.0fs waiting for the FreeCAD main thread"
            % MAIN_THREAD_TIMEOUT
        )
    return _unbox(box)


def _invoke(tool, params):
    """Call the handler, wrapping mutating tools in a document transaction."""
    import FreeCAD as App

    gui = gui_module()
    handler = tool["handler"]
    if not tool.get("mutating"):
        return handler(App, gui, params)

    doc = _transaction_document(App, params)
    label = params.get("transaction_name") or tool["name"]
    if doc is None:
        # No document yet (fc_doc_new / fc_doc_open): nothing to undo.
        return handler(App, gui, params)

    # Console mode leaves undo off by default; the agent needs it either way.
    try:
        if not getattr(doc, "UndoMode", 0):
            doc.UndoMode = 1
    except Exception:
        pass

    doc.openTransaction(str(label))

    def _finish(abort):
        # The handler may have closed the document it was called on
        # (fc_exec closing/reopening files); a dead proxy must not turn a
        # finished call into an error.
        try:
            if abort:
                doc.abortTransaction()
            else:
                doc.commitTransaction()
        except ReferenceError:
            pass

    try:
        result = handler(App, gui, params)
    except Exception:
        _finish(abort=True)
        raise
    _finish(
        abort=bool(
            tool.get("abort_on_failure")
            and isinstance(result, dict)
            and result.get("ok") is False
        )
    )
    return result


def _transaction_document(App, params):
    """Resolve the document a mutating call should be recorded against."""
    name = params.get("doc")
    if name:
        return App.listDocuments().get(name)
    return App.ActiveDocument


_invoker = None


def _main_thread_invoker():
    """A QObject living on the Qt main thread whose signal queues callables.

    ``QTimer.singleShot`` from an HTTP worker thread never fires: the timer is
    owned by the calling thread, which has no Qt event loop. A queued signal
    connection, on the other hand, is delivered on the thread that owns the
    receiver object, so we park one QObject on the main thread and emit to it.
    """
    global _invoker
    from PySide import QtCore

    if _invoker is not None:
        return _invoker

    class _Invoker(QtCore.QObject):
        call = QtCore.Signal(object)

        def __init__(self):
            super().__init__()
            self.call.connect(self._run, QtCore.Qt.QueuedConnection)

        def _run(self, function):
            function()

    invoker = _Invoker()
    app = QtCore.QCoreApplication.instance()
    if app is not None and invoker.thread() is not app.thread():
        invoker.moveToThread(app.thread())
    _invoker = invoker
    return invoker


def _run_on_main_thread(function):
    """Execute ``function`` on the Qt main thread and return its result."""
    box = {}
    done = threading.Event()

    def runner():
        try:
            box["result"] = function()
        except BaseException as exc:  # noqa: BLE001 - re-raised on the caller
            box["error"] = exc
            box["traceback"] = traceback.format_exc()
        finally:
            done.set()

    _main_thread_invoker().call.emit(runner)
    if not done.wait(MAIN_THREAD_TIMEOUT):
        raise ToolError(
            "timed out after %.0fs waiting for the FreeCAD main thread; "
            "is a modal dialog open?" % MAIN_THREAD_TIMEOUT
        )
    return _unbox(box)


def _unbox(box):
    """Return the job's result, re-raising whatever it raised on this thread."""
    if "error" in box:
        error = box["error"]
        if isinstance(error, ToolError):
            raise error
        raise ToolError(
            "%s: %s" % (type(error).__name__, error),
            {"traceback": box.get("traceback")},
        )
    return box.get("result")
