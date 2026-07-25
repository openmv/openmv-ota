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

# --- micropython PR #19348 (ranged romfs erase), carried for v5.0 firmware --------------
#
# dpgeorge's micropython#19348 adds the ranged (4-arg) WRITE_PREPARE + GET_MIN_PREPARE
# (rom_ioctl 6). The OTA installer needs it on XIP ports (N6/AE3): without it the whole FRONT
# slot is erased in ONE rom_ioctl(3) -- seconds of dead time in a single C call that stalls USB
# and faults partway through on a large slot (the N6's 12 MiB XSPI) -- so the installer falls
# back to that legacy erase and the incremental path is never exercised. It isn't in
# openmv/micropython yet, so a v5.0 firmware checkout cherry-picks the PR commits here, BEFORE
# `project new` locks the tree (the build's drift guard refuses a post-lock change) and commits
# the submodule bump so the firmware tree stays clean (the guard also refuses a dirty checkout).
#
# TEMPORARY: the maintainer carries #19348 in openmv/micropython directly and will retire this;
# a rebased PR changes the SHAs -- update _PARTIAL_ERASE_COMMITS then. Idempotent + v5.0-gated.
_PARTIAL_ERASE_COMMITS=(
  6a4062f9974640ee60fbd0d52224b973712b6f80   # extmod/vfs: GET_MIN_PREPARE constant
  61fadc0ec8cc9ff58ddab27ad62c59aa9344307b   # alif: 4-arg WRITE_PREPARE + GET_MIN_PREPARE
  893850436a799cc0c31126614e704abc9eb2cae5   # samd: 4-arg WRITE_PREPARE + GET_MIN_PREPARE
  9f9b28ecb3360851e50606db822523ccb28f0a56   # stm32: flash_get_max_sector_size helper
  14074d10871cef76b14c5a3c8bf12d8afca9430e   # stm32: 4-arg WRITE_PREPARE + GET_MIN_PREPARE
  720f797d08912d3f9c8994b31663cb16e47d5efd   # mpremote: incremental romfs deploy
)
apply_partial_erase() {                     # $1 = firmware checkout
  local fw="$1" mpy="$1/lib/micropython" h="$1/protocol/omv_protocol.h" id
  grep -qE 'OMV_FIRMWARE_VERSION_MAJOR +\(5\)' "$h" 2>/dev/null || return 0   # v5 line only
  grep -qE 'OMV_FIRMWARE_VERSION_MINOR +\(0\)' "$h" 2>/dev/null || return 0   # v5.0 only
  grep -q 'MP_VFS_ROM_IOCTL_GET_MIN_PREPARE' "$mpy/extmod/vfs.h" 2>/dev/null && return 0  # done
  log "cherry-pick micropython#19348 (ranged romfs erase) into lib/micropython"
  id=(-c user.name=openmv-ota -c user.email=build@openmv.io)
  git -C "$mpy" fetch -q https://github.com/micropython/micropython pull/19348/head
  git -C "$mpy" "${id[@]}" cherry-pick "${_PARTIAL_ERASE_COMMITS[@]}" >&2
  # Commit the submodule bump: `openmv-ota build` refuses a dirty/drifted checkout, so the lock
  # (written by `project new` next) must capture a CLEAN tree at the cherry-picked micropython.
  git -C "$fw" "${id[@]}" commit -q -m \
    "carry micropython#19348 (ranged romfs erase, v5.0 OTA)" lib/micropython >&2
}

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
  apply_partial_erase "$FW"
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
