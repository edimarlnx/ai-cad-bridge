"""AI Bridge — FreeCAD workbench registration (GUI mode only).

Three commands: start the bridge server, stop it, and copy the agent
registration command to the clipboard. All the logic lives in ``server.py`` /
``dispatch.py``, which are exercised headlessly by ``tests/test_headless.py``,
so this layer stays thin.

IMPORTANT — FreeCAD exec's this file with SEPARATE globals/locals dicts. Under
that model top-level names (imports, defs, constants) land in *locals*, while
nested scopes (function bodies, class bodies, methods) resolve names via
*globals*. Any name a nested scope touches must therefore be published with
``globals().update(...)``; otherwise initialization dies with NameError and the
workbench never registers.
"""

import json
import os
import sys

import FreeCAD as App
import FreeCADGui as Gui

# (1) Publish the imports so the helpers below (which read globals) can use them.
globals().update({"json": json, "os": os, "sys": sys, "App": App, "Gui": Gui})


def _tr(text):
    """i18n wrapper for user-facing strings."""
    return App.Qt.translate("AiBridge", text)


def _locate_here():
    """Resolve this add-on's directory WITHOUT relying on ``__file__``."""
    try:
        return os.path.dirname(os.path.abspath(__file__))
    except NameError:
        for path in sys.path:
            if os.path.basename(path.rstrip(os.sep)) == "AiBridge" and os.path.exists(
                os.path.join(path, "InitGui.py")
            ):
                return path
        return os.path.join(App.getUserAppDataDir(), "Mod", "AiBridge")


def _bridge_script_path():
    """Absolute path of the host-side MCP script, recorded by install.sh."""
    marker = os.path.join(_locate_here(), "install-info.json")
    try:
        with open(marker, "r", encoding="utf-8") as handle:
            info = json.load(handle)
        path = info.get("mcp_script")
        if path and os.path.exists(path):
            return path
    except Exception:
        pass
    return None


def _registration_command():
    """The `claude mcp add` line the user should run on the host."""
    path = _bridge_script_path() or "<repo>/bridge/freecad_mcp.py"
    return "claude mcp add freecad -- python3 %s" % path


def _notify(message, error=False):
    """Log to the FreeCAD console (Report view)."""
    text = "[AI Bridge] %s\n" % message
    if error:
        App.Console.PrintError(text)
    else:
        App.Console.PrintMessage(text)


class _CmdStart:
    """Toolbar command: start the loopback JSON-RPC server."""

    def GetResources(self):
        return {
            "MenuText": _tr("Start bridge"),
            "ToolTip": _tr(
                "Start the local AI bridge server (127.0.0.1) so Claude Code or "
                "Codex can drive this FreeCAD session."
            ),
        }

    def IsActive(self):
        import server

        return not server.is_running()

    def Activated(self):
        import server

        try:
            state = server.start()
        except Exception as exc:
            _notify(_tr("could not start: %s") % exc, error=True)
            return
        _notify(
            _tr("listening on http://127.0.0.1:%s — register with: %s")
            % (state["port"], _registration_command())
        )


class _CmdStop:
    """Toolbar command: stop the server."""

    def GetResources(self):
        return {
            "MenuText": _tr("Stop bridge"),
            "ToolTip": _tr("Stop the local AI bridge server."),
        }

    def IsActive(self):
        import server

        return server.is_running()

    def Activated(self):
        import server

        server.stop()
        _notify(_tr("server stopped."))


class _CmdCopyRegistration:
    """Toolbar command: copy the `claude mcp add` line to the clipboard."""

    def GetResources(self):
        return {
            "MenuText": _tr("Copy registration command"),
            "ToolTip": _tr(
                "Copy the 'claude mcp add' command for this machine to the "
                "clipboard (also printed in the Report view)."
            ),
        }

    def IsActive(self):
        return True

    def Activated(self):
        command = _registration_command()
        try:
            from PySide import QtGui

            QtGui.QApplication.clipboard().setText(command)
            _notify(_tr("copied to clipboard: %s") % command)
        except Exception:
            _notify(_tr("run this on the host: %s") % command)


# (2) Publish everything the workbench class body and methods reference.
globals().update(
    {
        "_tr": _tr,
        "_locate_here": _locate_here,
        "_bridge_script_path": _bridge_script_path,
        "_registration_command": _registration_command,
        "_notify": _notify,
        "_CmdStart": _CmdStart,
        "_CmdStop": _CmdStop,
        "_CmdCopyRegistration": _CmdCopyRegistration,
    }
)


class AiBridgeWorkbench(Gui.Workbench):
    """Workbench that starts/stops the local AI agent bridge."""

    MenuText = "AI Bridge"
    ToolTip = "Let Claude Code or Codex drive this FreeCAD session locally"

    def Initialize(self):
        Gui.addCommand("AiBridge_Start", _CmdStart())
        Gui.addCommand("AiBridge_Stop", _CmdStop())
        Gui.addCommand("AiBridge_CopyRegistration", _CmdCopyRegistration())
        commands = ["AiBridge_Start", "AiBridge_Stop", "AiBridge_CopyRegistration"]
        self.appendToolbar(_tr("AI Bridge"), commands)
        self.appendMenu(_tr("AI Bridge"), commands)

    def Activated(self):
        import server

        _notify(
            _tr("workbench activated (server %s).")
            % ("running" if server.is_running() else "stopped")
        )

    def Deactivated(self):
        pass

    def GetClassName(self):
        return "Gui::PythonWorkbench"


Gui.addWorkbench(AiBridgeWorkbench())
