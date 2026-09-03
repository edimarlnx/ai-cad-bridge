# Regenerate bridge/fallback_tools.json from the add-on's real tool registry.
#
#   flatpak run --command=FreeCADCmd org.freecad.FreeCAD bridge/gen_fallback.py
#
# Why this exists: an MCP client caches the tool list it gets at connect time.
# If FreeCAD happens to be closed at that moment the bridge answers with its
# fallback catalogue, and whatever schema that catalogue carries is the schema
# the client will keep using for the rest of the session. A schema-less fallback
# makes the client send array arguments as strings ('["Bracket"]'), which the
# handlers then reject. So the fallback must be the *same* catalogue as the live
# one — generated here, never written by hand.
#
# The file lives next to freecad_mcp.py and is loaded by it at startup.
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_REPO, "freecad", "AiBridge"))

import tools  # noqa: E402  (needs the sys.path line above)

OUTPUT = os.path.join(_HERE, "fallback_tools.json")


def main():
    registry = tools.build_registry()
    catalogue = [
        {
            "name": name,
            "description": tool.get("description", ""),
            "inputSchema": tool.get("inputSchema") or {"type": "object", "properties": {}},
        }
        for name, tool in sorted(registry.items())
    ]
    with open(OUTPUT, "w", encoding="utf-8") as handle:
        json.dump({"tools": catalogue}, handle, indent=2, sort_keys=False)
        handle.write("\n")
    sys.stderr.write("[gen_fallback] wrote %d tools to %s\n" % (len(catalogue), OUTPUT))


main()
