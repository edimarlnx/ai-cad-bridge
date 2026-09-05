#!/usr/bin/env bash
# Watch the "moeda zCloud" blocks being machined, before cutting anything.
#
#   bash examples/moeda-zcloud/simulate.sh            # the sheet2 10-zcloud block
#   MOEDA_VALUES="1 5 10" bash examples/moeda-zcloud/simulate.sh
#
# Reads the G-code that run.sh posted and renders one MP4 per block: the stock
# as a top-view heightmap, material removed move by move, at ten seconds of
# machine time per frame.
#
# Optional extras: numpy, Pillow and the ffmpeg binary (the bridge itself stays
# standard library only).
#
# Environment:
#   MOEDA_OUT_DIR   where run.sh put models/ and gcode/ (same default as run.sh).
#   MOEDA_VALUES    denominations to simulate (default "10").
#   MOEDA_SIM_DIR   where the videos go (default "${MOEDA_OUT_DIR}/sim").
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "${HERE}/../.." && pwd)"
OUT_DIR="${MOEDA_OUT_DIR:-${HERE}/out/cnc}"
SIM_DIR="${MOEDA_SIM_DIR:-${OUT_DIR}/sim}"
VALUES="${MOEDA_VALUES:-10}"

command -v ffmpeg >/dev/null 2>&1 || { echo "error: ffmpeg not found" >&2; exit 1; }
mkdir -p "${SIM_DIR}"

for value in ${VALUES}; do
  op1="${OUT_DIR}/gcode/${value}/sheet2-${value}-op1-T1-endmill30.gcode"
  op2="${OUT_DIR}/gcode/${value}/sheet2-${value}-op2_4-T2-vbit30.gcode"
  if [ ! -f "${op1}" ] || [ ! -f "${op2}" ]; then
    echo "error: no sheet2 G-code for ${value} in ${OUT_DIR}/gcode/${value}" >&2
    echo "       run: MOEDA_LAYOUT=sheet2 bash examples/moeda-zcloud/run.sh" >&2
    exit 1
  fi
  # One 100 x 100 x 10 mm block, XY zero at its centre, Z0 on the top face.
  # T1 clears the cavities and bores the pin holes, then T2 V-carves the relief.
  python3 "${REPO}/bridge/cam_sim_video.py" \
    --stock 100x100x10 --origin center \
    --tool endmill:3.0 "${op1}" \
    --tool vbit:30:0.1 "${op2}" \
    --out "${SIM_DIR}/sheet2-${value}.mp4" \
    --machine-seconds-per-frame 10 --fps 30 --resolution 0.1
done
