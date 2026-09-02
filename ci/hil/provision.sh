#!/usr/bin/env bash
#
# Provision the RUNNER-OWNED OTA HIL tooling for a board, idempotently and cached under the
# runner's own $HOME. The HIL nodes are DISPOSABLE infra (built once from the node image): they carry
# no hand-set-up state, so the test workflow brings everything OTA-specific itself. A reimage
# wipes the cache and the next run re-provisions from scratch.
#
# It mirrors the reproducible CI build recipe (clone the firmware, `project new --install-sdk`)
# and adds the HIL-only pieces: a venv with the server extra + the serial tooling (pyserial and
# mpremote are core deps), the pegged project, and J-Link userspace for J-Link boards. The SDK's
# dfu-util / blhost cover the other flashers.
#
# Anything needing ROOT (the runner user, USB/serial groups + udev, git safe.directory, the
# system build deps) belongs in the node image (see ci/README.md), NOT here -- this script assumes only an
# unprivileged runner with python3 + git + a working toolchain on PATH.
#
# Usage:  eval "$(ci/hil/provision.sh <board> <checkout>)"
# Env:    OPENMV_REF   firmware ref to build (default: master, as CI)
#         OPENMV_URL   firmware repo to fetch that ref from (default: openmv/openmv). Exists for
#                      boards whose firmware changes are fork-maintained: the classic legs pin
#                      the exact proven bring-up commit (a SHA in hil-ota.yml -- GitHub serves
#                      fetch-by-SHA), and moving them is an explicit SHA bump there.
#         HIL_CACHE    where the runner-owned tooling lives (default: ~/.cache/openmv-ota-hil)
set -euo pipefail

BOARD="${1:?usage: provision.sh <board> <checkout>}"
CHECKOUT="${2:?usage: provision.sh <board> <checkout>}"
CACHE="${HIL_CACHE:-$HOME/.cache/openmv-ota-hil}"
REF="${OPENMV_REF:-master}"
URL="${OPENMV_URL:-https://github.com/openmv/openmv.git}"

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
# Drop a stale git index.lock EVERY run (cache-hit or miss): a provision KILLED mid-git -- a cancelled
# run, or two runs contending the same runner cache (a manual dispatch racing the PR gate) -- leaves
# `.git/index.lock` behind, and every subsequent fetch/reset then dies with "Unable to create
# index.lock: File exists". The runner-owned cache can't be hand-cleaned off-box, so the node would
# stay wedged forever. No git process survives a finished job, so any lock here is stale -> remove it
# (main repo + every submodule under .git/modules). Idempotent and safe.
[ -d "$FW/.git" ] && find "$FW/.git" -name index.lock -delete 2>/dev/null || true
# Scrub leftover UNTRACKED files from the firmware EVERY run (cache-hit or miss): `build firmware`
# drops modules/ecdsa_verify.c and removes it in a finally, but a build killed mid-flight (or one that
# found it already present -> _install_verify_module returns None and never cleans it) leaves it
# untracked. `reset --hard` doesn't drop untracked files either, so once leaked the firmware reads
# dirty forever and every subsequent `build firmware` refuses the tree. Unconditional clean fixes it.
[ -d "$FW/.git" ] && git -C "$FW" clean -qfd >/dev/null 2>&1 || true
# Re-provision when: no lock yet; the pre-#19348 cache is present (lock exists but the patch is
# missing); OR the tool's commit changed since the cached project was built (so a firmware-feature
# add/change re-locks against what the current tool carries -- otherwise the cached lock drifts).
# Keyed on the checkout's HEAD sha, NOT a file mtime: the runner reuses its workspace and only
# rewrites CHANGED files, so an unchanged project.py keeps an old mtime and an mtime test misfires.
# The stamp carries the firmware url+ref too: a leg whose pin changes (e.g. the classic legs
# moving off the bring-up branch back to master) must re-provision even when the tool commit
# didn't move, or the cached project keeps locking against the OLD firmware tree.
SHA="$(git -C "$CHECKOUT" rev-parse HEAD 2>/dev/null || echo unknown) $URL $REF"
if [ ! -f "$PROJ/openmv-ota.lock.json" ] \
   || [ "$(cat "$PROJ/.provision-sha" 2>/dev/null)" != "$SHA" ] \
   || ! grep -q 'MP_VFS_ROM_IOCTL_GET_MIN_PREPARE' "$FW/lib/micropython/extmod/vfs.h" 2>/dev/null; then
  [ -d "$FW/.git" ] || { log "git clone openmv"; git clone -q "$URL" "$FW"; }
  log "firmware <- $URL $REF (pristine reset; project new re-carries the current features)"
  # Reset HARD to the pinned ref so a prior run's feature-carry commits don't accumulate and the
  # cherry-picks (incl. any fork-compat fixup) re-apply cleanly onto the original tree; `clean -fdx`
  # then wipes build artifacts + any untracked cruft so the tree is pristine to lock against.
  # Fetch by URL, not by the cached remote: the cache may have been cloned from a different repo
  # (the pin changed), and fetching a URL works either way without rewriting the remote.
  git -C "$FW" fetch -q "$URL" "$REF"
  git -C "$FW" reset -q --hard FETCH_HEAD
  git -C "$FW" clean -qfdx
  git -C "$FW" submodule update -q --init --force --depth=1 --no-single-branch
  git -C "$FW/lib/micropython" submodule update -q --init --force --depth=1
  # `project new` below carries micropython#19348 (ranged romfs erase) into lib/micropython for
  # a v5.0 OTA firmware -- the tool guarantees it, so this script (and any real user) needs no
  # custom step; the lock captures the patched, committed-clean tree.
  # Boards whose firmware cannot carry the ~186 KB public CA bundle (no recovery_ca_bundle in
  # boards.json: the classics, whose ROMFS slot can't hold it either, and the 1792 KB H7-family
  # boards) are refused by `project new --ota` without a root of your own -- the same refusal a
  # real user hits, resolved the same way: generate a throwaway ~1 KB bench root once and cache
  # it. (The classic legs never do TLS at all -- file transport -- so for them it is pure
  # trust-store ballast the build requires.) Kept OUT of the N6/AE3/RT1060 projects -- those
  # freeze the real public bundle for recovery, exactly like a production build, which also
  # makes every fleet run the link-fit proof of that default.
  CA_ARGS=()
  case "$BOARD" in
    OPENMV2|OPENMV3|OPENMV4|OPENMV4P|OPENMVPT|ARDUINO_PORTENTA_H7|ARDUINO_GIGA|ARDUINO_NICLA_VISION)
      CA="$CACHE/bench-root.pem"
      if [ ! -f "$CA" ]; then
        log "generate bench root CA (this board's firmware can't hold the public bundle; needs --ca)"
        openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 \
          -keyout "$CACHE/bench-root-key.pem" -out "$CA" -days 3650 -nodes \
          -subj "/CN=OpenMV HIL bench root" >&2 2>&1
      fi
      CA_ARGS=(--ca "$CA")
      ;;
  esac
  log "openmv-ota project new -b $BOARD --ota --dev --install-sdk ${CA_ARGS[*]-}"
  rm -rf "$PROJ"
  "$VENV/bin/openmv-ota" project new "$PROJ" -f "$FW" -b "$BOARD" --ota --dev --install-sdk \
    ${CA_ARGS+"${CA_ARGS[@]}"} >&2
  echo "$SHA" > "$PROJ/.provision-sha"      # stamp the commit this project+lock was built with
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
