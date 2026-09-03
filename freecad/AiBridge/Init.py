"""Console-mode init for the AI Bridge add-on (runs in GUI and FreeCADCmd).

Publishes an importable ``AiBridge`` module so scripts and macros can drive the
bridge without knowing the add-on's file layout::

    import AiBridge
    AiBridge.start()          # honours AIBRIDGE_PORT, default 8765
    AiBridge.status()
    AiBridge.stop()

When ``AIBRIDGE_AUTOSTART=1`` is set in the environment the server starts right
here, which is what the headless test and unattended sessions use.

NOTE: FreeCAD exec's this file with separate globals/locals dicts, so everything
below stays at module top level (no nested scopes reading top-level names).
"""

import os
import sys
import types

import server  # add-on directory is on sys.path when FreeCAD loads the Mod

_module = types.ModuleType("AiBridge")
_module.__doc__ = "Local AI bridge for FreeCAD (start/stop the JSON-RPC server)."
_module.start = server.start
_module.serve = server.serve  # headless: start + pump tool calls on this thread
_module.stop = server.stop
_module.status = server.status
_module.is_running = server.is_running
_module.config_path = server.config_path
_module.server = server
sys.modules["AiBridge"] = _module

if os.environ.get("AIBRIDGE_AUTOSTART") == "1":
    try:
        server.start()
    except Exception as _exc:  # keep FreeCAD usable even if the port is taken
        import FreeCAD as _App

        _App.Console.PrintError("[AI Bridge] autostart failed: %s\n" % _exc)
