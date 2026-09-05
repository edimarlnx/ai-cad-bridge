#!/usr/bin/env bash
# Run both test suites and fail on the first red one.
#
#   bash tests/run.sh
#
# 1. test_headless.py runs INSIDE the real FreeCAD (Flatpak, FreeCADCmd): it
#    starts the add-on server in-process and drives every tool over HTTP.
# 2. test_mcp_stdio.py runs on the host python3 against a fake add-on.
# 3. test_sim_video.py measures the G-code simulator's heightmap. It needs the
#    optional numpy/Pillow extras and skips with a message when they are absent.
#
# The FreeCAD script must live under $HOME — the Flatpak sandbox cannot see /tmp.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "${HERE}/.." && pwd)"
FLATPAK_APP="org.freecad.FreeCAD"

echo "### 1/3 headless FreeCAD add-on tests"
if ! command -v flatpak >/dev/null 2>&1; then
  echo "error: flatpak not found; cannot run the FreeCAD suite" >&2
  exit 1
fi
# Never launch the GUI: --command=FreeCADCmd keeps it headless.
flatpak run --command=FreeCADCmd "${FLATPAK_APP}" "${HERE}/test_headless.py"

echo
echo "### 2/3 MCP stdio server tests"
python3 "${HERE}/test_mcp_stdio.py"

echo
echo "### 3/3 G-code simulator tests (optional extras: numpy, Pillow)"
python3 "${HERE}/test_sim_video.py"

echo
echo "### all suites passed"
