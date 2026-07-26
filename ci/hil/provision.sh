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

# 2+3) firmware checkout + micropython #19348 + pegged OTA project -- set up ONCE together and
#    cached. NOT re-synced every run: the project is pegged to this fw commit, so re-syncing to a
#    newer master would only drift it (and drop the #19348 cherry-pick the lock captured). A fresh
#    clone gets shallow submodules (as ci.yml), the ranged-erase patch (before the lock), then the
#    board-pegged OTA project (--dev throwaway key; AE3 auto-scaffolds app-coprocessor). The gate
#    also re-runs if a pre-#19348 cache is present (lock exists but the patch is missing).
FW="$CACHE/openmv"
PROJ="$CACHE/proj-$BOARD"
if [ ! -f "$PROJ/openmv-ota.lock.json" ] \
   || ! grep -q 'MP_VFS_ROM_IOCTL_GET_MIN_PREPARE' "$FW/lib/micropython/extmod/vfs.h" 2>/dev/null; then
  [ -d "$FW/.git" ] || { log "git clone openmv"; git clone -q https://github.com/openmv/openmv.git "$FW"; }
  log "firmware <- $REF"
  git -C "$FW" fetch -q origin
  git -C "$FW" checkout -q "$REF"
  git -C "$FW" submodule update -q --init --depth=1 --no-single-branch
  git -C "$FW/lib/micropython" submodule update -q --init --depth=1
  # TEST: carry the alif MRAM read-while-write fix (coprocessor-partition write). Applied to the
  # BASE here -- before `project new` cherry-picks #19348 on top (a disjoint hunk) -- and committed
  # so the lock captures a clean tree. Pending its home in openmv/micropython; HIL-only for now.
  log "apply alif MRAM RWW fix (coproc write)"
  git -C "$FW/lib/micropython" apply "$CHECKOUT/ci/hil/patches/alif-mram-coproc-write-rww.patch"
  git -C "$FW/lib/micropython" -c user.name=openmv-ota -c user.email=build@openmv.io \
      commit -q -am "alif: mask IRQ + inline MRAM write (coproc read-while-write fix)" >&2
  # `project new` below carries micropython#19348 (ranged romfs erase) into lib/micropython for
  # a v5.0 OTA firmware -- the tool guarantees it, so this script (and any real user) needs no
  # custom step; the lock captures the patched, committed-clean tree.
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
