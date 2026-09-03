#!/usr/bin/env bash
# Build the "moeda zCloud" CNC molds end to end.
#
#   bash examples/moeda-zcloud/run.sh
#
# Two stages, because the two tools live on different sides of the sandbox:
#
# 1. openscad runs on the HOST and exports the six coin faces as flat SVG
#    (three denominations x obverse/reverse) into examples/moeda-zcloud/svg/.
# 2. FreeCAD runs inside the Flatpak (headless, FreeCADCmd — never the GUI) and
#    builds the blocks, the CAM jobs and the GRBL G-code with the bridge's own
#    cam_* tools.
#
# Everything the script writes lands in the output folder below, plus a copy of
# the README and the validation JSON in examples/moeda-zcloud/out/.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "${HERE}/../.." && pwd)"
FLATPAK_APP="org.freecad.FreeCAD"
MASTER="${HOME}/Projetos/edimar/my-ideas/eletronica/moeda-zcloud-master.scad"
SVG_DIR="${HERE}/svg"

command -v openscad >/dev/null 2>&1 || { echo "error: openscad not found" >&2; exit 1; }
command -v flatpak >/dev/null 2>&1 || { echo "error: flatpak not found" >&2; exit 1; }
[ -f "${MASTER}" ] || { echo "error: coin master not found at ${MASTER}" >&2; exit 1; }

mkdir -p "${SVG_DIR}"

echo "### 1/2 exporting the coin faces with openscad (host)"
# denomination -> coin diameter, from moeda-zcloud.md section 3.
for pair in "1:34" "5:37" "10:40"; do
  value="${pair%%:*}"
  diameter="${pair##*:}"
  for face in anverso reverso; do
    out="${SVG_DIR}/${face}-${value}.svg"
    openscad -o "${out}" \
      -D 'vista="none"' \
      -D "face=\"${face}\"" \
      -D "diametro=${diameter}" \
      -D "valor=\"${value}\"" \
      "${HERE}/face2d.scad" 2>/dev/null
    printf '  %-24s %6s bytes\n' "$(basename "${out}")" "$(stat -c %s "${out}")"
  done
done

echo
echo "### 2/2 building the molds and the G-code (FreeCAD headless)"
flatpak run --command=FreeCADCmd "${FLATPAK_APP}" "${HERE}/build_molds.py" >/dev/null

echo
echo "### output"
sed -n '/^| file/,/^$/p' "${HERE}/out/README.md"
echo "README and validation.json: ${HERE}/out/"
