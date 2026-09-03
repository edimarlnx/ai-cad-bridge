#!/usr/bin/env bash
# Install the AI Bridge add-on into the Flatpak FreeCAD Mod directory.
#
# FreeCAD (Flatpak org.freecad.FreeCAD) loads user add-ons from its per-VERSION
# data dir. FreeCAD 1.1 uses a versioned path:
#   ~/.var/app/org.freecad.FreeCAD/data/FreeCAD/v1-1/Mod/<AddOn>/
# (older builds used .../FreeCAD/Mod). We pick the newest v*/Mod if present,
# else fall back to the unversioned Mod dir. (NOT ~/.local/share.)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_DIR="${HERE}/AiBridge"
MCP_SCRIPT="$(cd "${HERE}/.." && pwd)/bridge/freecad_mcp.py"
DATA_DIR="${HOME}/.var/app/org.freecad.FreeCAD/data/FreeCAD"
# newest versioned dir (v1-1, v1-2, ...) wins; fall back to unversioned
VER_DIR="$(ls -d "${DATA_DIR}"/v*/ 2>/dev/null | sort -V | tail -1 || true)"
if [ -n "${VER_DIR}" ]; then
  MOD_DIR="${VER_DIR%/}/Mod"
else
  MOD_DIR="${DATA_DIR}/Mod"
fi
DEST_DIR="${MOD_DIR}/AiBridge"

if [ ! -d "${SRC_DIR}" ]; then
  echo "error: add-on source not found at ${SRC_DIR}" >&2
  exit 1
fi

mkdir -p "${MOD_DIR}"

# Prefer rsync (clean sync, drops removed files); fall back to cp -r.
if command -v rsync >/dev/null 2>&1; then
  rsync -a --delete \
    --exclude '__pycache__' --exclude '*.pyc' --exclude 'install-info.json' \
    "${SRC_DIR}/" "${DEST_DIR}/"
else
  rm -rf "${DEST_DIR}"
  mkdir -p "${DEST_DIR}"
  cp -r "${SRC_DIR}/." "${DEST_DIR}/"
  find "${DEST_DIR}" -name '__pycache__' -type d -prune -exec rm -rf {} + || true
fi

# Record where the host-side MCP script lives, so the workbench's
# "Copy registration command" button can print the right path.
cat > "${DEST_DIR}/install-info.json" <<JSON
{
  "mcp_script": "${MCP_SCRIPT}",
  "source": "${SRC_DIR}",
  "installed_at": "$(date -Iseconds)"
}
JSON

echo "AI Bridge installed to:"
echo "  ${DEST_DIR}"
echo
echo "Next:"
echo "  1. Restart FreeCAD and pick the 'AI Bridge' workbench, then 'Start bridge'."
echo "  2. Register the agent:  bash $(cd "${HERE}/.." && pwd)/bridge/register.sh"
