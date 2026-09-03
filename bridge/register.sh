#!/usr/bin/env bash
# Print how to register the FreeCAD bridge with Claude Code and Codex.
#
# It only PRINTS: ~/.codex/config.toml is never edited by this script, and the
# `claude mcp add` line is left for you to run so nothing changes behind your
# back.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="${HERE}/freecad_mcp.py"
PYTHON="$(command -v python3 || echo python3)"
CONFIG="${HOME}/.config/freecad-ai-bridge/config.json"

echo "FreeCAD AI Bridge — agent registration"
echo
echo "MCP server : ${SCRIPT}"
echo "python3    : ${PYTHON}"
echo "bridge cfg : ${CONFIG} $([ -f "${CONFIG}" ] && echo '(present)' || echo '(created when you first start the bridge in FreeCAD)')"
echo
echo "1) Claude Code — run this once:"
echo
echo "   claude mcp add freecad -- ${PYTHON} ${SCRIPT}"
echo
echo "   (add --scope user to make it available in every project)"
echo
echo "2) Codex CLI — add this block to ~/.codex/config.toml by hand:"
echo
echo "   [mcp_servers.freecad]"
echo "   command = \"${PYTHON}\""
echo "   args = [\"${SCRIPT}\"]"
echo
echo "3) In FreeCAD: pick the 'AI Bridge' workbench and press 'Start bridge'."
echo "   The agent then sees the fc_* tools; ask it for fc_status to check."
