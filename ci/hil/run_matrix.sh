#!/bin/bash
# Run a list of OTA scenarios back-to-back on this node's board, one full provision each,
# writing a trace per scenario to ~/hil-traces/. Used to build the HIL path-coverage catalog.
#   run_matrix.sh <BOARD> <lan|wifi> <scenario> [scenario ...]
set -u
BOARD="$1"; NET="$2"; shift 2
VENV="${OTA_VENV:-$HOME/ota-venv}"
CHECKOUT="${HIL_CHECKOUT:-$HOME/openmv-ota}"
mkdir -p "$HOME/hil-traces"
cd "$CHECKOUT" || exit 1
for sc in "$@"; do
  echo "=== RUN $BOARD/$NET/$sc @ $(date -u +%H:%M:%S) ==="
  "$VENV/bin/python" ci/hil/ota_cycle.py \
    --board "$BOARD" --network "$NET" --scenario "$sc" \
    --checkout "$CHECKOUT" \
    --trace "$HOME/hil-traces/hil-${BOARD}-${NET}-${sc}.json" \
    --timeout 500 > "/tmp/batch-${BOARD}-${NET}-${sc}.log" 2>&1
  echo "=== $sc -> $(grep -h 'RESULT:' "/tmp/batch-${BOARD}-${NET}-${sc}.log" | tail -1)"
done
echo "BATCH-COMPLETE $BOARD/$NET"
