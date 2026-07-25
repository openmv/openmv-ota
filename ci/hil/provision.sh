#!/usr/bin/env bash
#
# Provision the RUNNER-OWNED OTA HIL tooling for a board, idempotently and cached under the
# runner's own $HOME. The HIL nodes are DISPOSABLE infra (built once by openmv-hil): they carry
# no hand-set-up state, so the test workflow brings everything OTA-specific itself. A reimage
# wipes the cache and the next run re-provisions from scratch.
#
# It mirrors the reproducible CI build recipe (clone the firmware, `project new --install-sdk`)
# and adds the HIL-only pieces: a venv with the server extra + the serial tooling (pyserial and
# mpremote are core deps), the pegged project, and J-Link userspace for J-Link boards. The SDK's
# dfu-util / blhost cover the other flashers.
#
# Anything needing ROOT (the runner user, USB/serial groups + udev, git safe.directory, the
# system build deps) belongs in the openmv-hil image, NOT here -- this script assumes only an
# unprivileged runner with python3 + git + a working toolchain on PATH.
#
# Usage:  eval "$(ci/hil/provision.sh <board> <checkout>)"
# Env:    OPENMV_REF   firmware ref to build (default: master, as CI)
#         HIL_CACHE    where the runner-owned tooling lives (default: ~/.cache/openmv-ota-hil)
set -euo pipefail

BOARD="${1:?usage: provision.sh <board> <checkout>}"
CHECKOUT="${2:?usage: provision.sh <board> <checkout>}"
CACHE="${HIL_CACHE:-$HOME/.cache/openmv-ota-hil}"
REF="${OPENMV_REF:-master}"

log() { echo "provision: $*" >&2; }        # stdout is reserved for the `export` lines
mkdir -p "$CACHE"

# 1) venv -- openmv-ota installed EDITABLE (so it always reflects the checkout under test) plus
#    the server extra for the ephemeral update server; pyserial + mpremote come in as core deps.
#    Rebuilt when the checkout's dependency set (pyproject) is newer than the last build.
VENV="$CACHE/venv"
if [ ! -x "$VENV/bin/openmv-ota" ] || [ "$CHECKOUT/pyproject.toml" -nt "$VENV/.stamp" ]; then
  log "venv <- pip install -e $CHECKOUT[server]"
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install -q --upgrade pip
  "$VENV/bin/pip" install -q -e "$CHECKOUT"[server]
  touch "$VENV/.stamp"
fi

# 2) firmware checkout -- shallow submodules, exactly as ci.yml's build job (openmv's direct
#    submodules, then micropython's own). Cloned once; fetched + re-checked-out each run.
FW="$CACHE/openmv"
[ -d "$FW/.git" ] || { log "git clone openmv"; git clone -q https://github.com/openmv/openmv.git "$FW"; }
log "firmware <- $REF"
git -C "$FW" fetch -q origin
git -C "$FW" checkout -q "$REF"
git -C "$FW" submodule update -q --init --depth=1 --no-single-branch
git -C "$FW/lib/micropython" submodule update -q --init --depth=1

# 3) project -- pegged to this fw checkout, OTA + a throwaway dev signing key (builds pass
#    --allow-dev-key), and the tool installs the SDK into $HOME. One project per board (it's
#    board-pegged; an AE3 project auto-scaffolds app-coprocessor). Created once per board.
PROJ="$CACHE/proj-$BOARD"
if [ ! -f "$PROJ/openmv-ota.lock.json" ]; then
  log "openmv-ota project new -b $BOARD --ota --dev --install-sdk"
  rm -rf "$PROJ"
  "$VENV/bin/openmv-ota" project new "$PROJ" -f "$FW" -b "$BOARD" --ota --dev --install-sdk >&2
fi
SDK="$HOME/openmv-sdk-$(cat "$FW/SDK_VERSION")"

# 4) J-Link userspace -- only the J-Link-flashed boards need it (the SDK carries dfu-util +
#    blhost for the others). Self-contained tarball, no install/root.
JDIR="$CACHE/jlink"
if [ ! -x "$JDIR/JLinkExe" ]; then
  log "download J-Link userspace"
  mkdir -p "$JDIR"
  curl -fsSL -X POST -d accept_license_agreement=accepted -d "submit=Download software" \
    https://www.segger.com/downloads/jlink/JLink_Linux_x86_64.tgz -o "$CACHE/jlink.tgz"
  tar -xzf "$CACHE/jlink.tgz" -C "$JDIR" --strip-components=1
fi

# Hand the resolved, runner-owned paths back to the workflow (it evals these before ota_cycle).
cat <<EOF
export OTA_VENV="$VENV"
export PROJECT_DIR="$PROJ"
export SDK_HOME="$SDK"
export JLINK="$JDIR/JLinkExe"
export DFU_UTIL="$SDK/bin/dfu-util"
export BLHOST="$SDK/python/bin/blhost"
EOF
