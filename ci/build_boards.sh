#!/usr/bin/env bash
#
# Black-box CI driver: exercise the *installed* openmv-ota CLI exactly as a
# pip-installed user would -- only `openmv-ota ...` plus standard unix tools
# (awk, wc, grep, python3). Nothing here imports the package; zip extraction uses
# python3 -m zipfile so no external `unzip` binary is required.
#
# For each board it knows the expected capability (the table below) and asserts
# the CLI's behaviour:
#
#   full     (OTA-capable):    project new --ota --dev (throwaway CI signing key;
#                              the signing builds then need --allow-dev-key);
#                              build firmware + romfs + factory-romfs; inspect +
#                              verify the OTA bundle (bundle and loose
#                              body+trailer); a corrupted body must FAIL verify;
#                              the factory image is the full partition.
#   classic  (romfs, small):   BOTH paths, because under v2 these boards are OTA-capable
#                              -- a partition with room for only one slot builds in
#                              SINGLE-IMAGE mode instead of being refused. So:
#                              `project new --ota --dev` must SUCCEED, and `build romfs`
#                              must budget against the SINGLE-image slot (the A/B
#                              arithmetic yields a negative budget on a one-erase-sector
#                              board). The scaffolded app does not fit these partitions,
#                              so that build is asserted to refuse CLEANLY quoting the
#                              real single-image budget. AND the non-OTA path still works
#                              (project new; build firmware + single-image romfs;
#                              `build factory-romfs` fails cleanly without an OTA project).
#   noromfs  (no partition):   `project new` must fail cleanly (no partition size).
#
# Every expected failure is asserted to be a clean tool error (no Python
# traceback, an `error:` line) -- a board the tool can't serve says so
# structurally, it does not explode.
#
# Usage:   ci/build_boards.sh <firmware-checkout> <board> [<board> ...]
# Env:     INSTALL_SDK=1   pass --install-sdk to `project new`
#          NO_FIRMWARE=1   skip the slow firmware compile (romfs/factory still run)
#          WORKDIR=DIR     where projects are created (default: a temp dir)
#          OPENMV_OTA_BIN  the CLI to invoke (default: openmv-ota)

set -uo pipefail

OTA="${OPENMV_OTA_BIN:-openmv-ota}"
PY="${PYTHON:-python3}"   # for stdlib helpers (zipfile) -- no external unzip needed
FW="${1:?usage: build_boards.sh <firmware-checkout> <board> [<board> ...]}"; shift
[ "$#" -ge 1 ] || { echo "error: at least one board is required" >&2; exit 2; }

SDK_FLAG=""
[ -n "${INSTALL_SDK:-}" ] && SDK_FLAG="--install-sdk"
WORKDIR="${WORKDIR:-$(mktemp -d "${TMPDIR:-/tmp}/openmv-ota-ci.XXXXXX")}"
mkdir -p "$WORKDIR"

PASS_N=0
FAIL_N=0
LAST_OUT=""
LAST_RC=0

pass() { PASS_N=$((PASS_N + 1)); echo "  [PASS] $1"; }
fail() {
  FAIL_N=$((FAIL_N + 1))
  echo "  [FAIL] $1"
  [ -n "${2:-}" ] && printf '%s\n' "$2" | sed 's/^/         /'
  return 0
}

runcmd() { LAST_OUT="$("$@" 2>&1)"; LAST_RC=$?; }

# A board / OS the tool can't serve must say so structurally: non-zero exit, no
# Python traceback, an `error:` line, and (optionally) a specific code / message.
expect_clean_fail() {  # label  code|""  needle|""  cmd...
  local label="$1" code="$2" needle="$3"; shift 3
  runcmd "$@"
  case "$LAST_OUT" in
    *"Traceback (most recent call last)"*) fail "$label" "Python traceback:
$LAST_OUT"; return;;
  esac
  [ "$LAST_RC" -eq 0 ] && { fail "$label" "expected failure but exit 0:
$LAST_OUT"; return; }
  [ -n "$code" ] && [ "$LAST_RC" -ne "$code" ] && { fail "$label" "expected exit $code, got $LAST_RC:
$LAST_OUT"; return; }
  case "$LAST_OUT" in
    *"error:"*) ;;
    *) fail "$label" "no 'error:' line:
$LAST_OUT"; return;;
  esac
  if [ -n "$needle" ]; then
    case "$LAST_OUT" in
      *"$needle"*) ;;
      *) fail "$label" "missing message '$needle':
$LAST_OUT"; return;;
    esac
  fi
  pass "$label"
}

expect_success() {  # label  cmd...
  local label="$1"; shift
  runcmd "$@"
  if [ "$LAST_RC" -eq 0 ]; then pass "$label"; else fail "$label" "exit $LAST_RC:
$LAST_OUT"; fi
}

expect_file() {  # label  path
  if [ -f "$2" ]; then pass "$1"; else fail "$1" "missing file: $2"; fi
}

# A verification *verdict* (exit 1, "FAILED"), distinct from a tool error.
expect_verify_reject() {  # label  cmd...
  local label="$1"; shift
  runcmd "$@"
  case "$LAST_OUT" in *"Traceback"*) fail "$label" "$LAST_OUT"; return;; esac
  if [ "$LAST_RC" -eq 1 ]; then
    case "$LAST_OUT" in *FAILED*) pass "$label"; return;; esac
  fi
  fail "$label" "expected exit 1 + FAILED, got exit $LAST_RC:
$LAST_OUT"
}

# partition_size for a board's first (main) partition, parsed from `project show`
# text (the line is e.g. "OPENMV_N6  part[0] main  25165824  front 12582912 ...").
# Takes the first all-numeric field after `part[` so it's robust to the role column.
# Empty if it can't be read.
part_size() {  # proj  board
  $OTA project show "$1" 2>/dev/null | awk -v b="$2" '
    $1 == b { for (i = 1; i <= NF; i++) if ($i ~ /^part\[/) {
                for (j = i + 1; j <= NF; j++) if ($j ~ /^[0-9]+$/) { print $j; exit } } }'
}

build_firmware() {  # proj  board
  [ -n "${NO_FIRMWARE:-}" ] && return 0
  expect_success "build firmware" $OTA build firmware "$1" -b "$2"
  if ls "$1/build/$2"-firmware*.bin >/dev/null 2>&1; then
    pass "firmware image written (<board>-firmware*.bin)"
  else
    fail "firmware image written (<board>-firmware*.bin)" "none under $1/build"
  fi
}

verify_factory_size() {  # proj  board  img
  local psize isize
  psize="$(part_size "$1" "$2")"
  isize="$(wc -c < "$3" | tr -d ' ')"
  if [ -n "$psize" ] && [ "$psize" = "$isize" ]; then
    pass "factory image is the full partition ($psize bytes)"
  elif [ -n "$psize" ]; then
    fail "factory image is the full partition" "image=$isize, partition=$psize"
  else
    echo "  [skip] partition-size check (couldn't parse 'project show')"
  fi
}

# True if BOARD has a coprocessor (slaved second-core) partition, per `project show`.
has_coprocessor() {  # proj  board
  $OTA project show "$1" 2>/dev/null | awk -v b="$2" '$1 == b && /coprocessor/ { f = 1 } END { exit !f }'
}

# Size of the coprocessor partition (the `part[N] coprocessor ...` line). Empty if none.
copro_part_size() {  # proj  board
  $OTA project show "$1" 2>/dev/null | awk -v b="$2" '
    $1 == b && /coprocessor/ { for (i = 1; i <= NF; i++) if ($i ~ /^part\[/) {
                for (j = i + 1; j <= NF; j++) if ($j ~ /^[0-9]+$/) { print $j; exit } } }'
}

# A coprocessor image must be a *plain* romfs (no trailer/zip) that fits its partition.
verify_coprocessor() {  # proj  board  img
  local csize isize
  expect_file "coprocessor romfs written (<board>-coprocessor-romfs.img)" "$3"
  [ -f "$3" ] || return 0
  if [ "$(head -c2 "$3")" = "PK" ]; then   # an OTA .zip bundle starts with "PK"
    fail "coprocessor image is a plain romfs (not an OTA bundle)" "starts with PK (zip)"
  else
    pass "coprocessor image is a plain romfs (not an OTA bundle)"
  fi
  csize="$(copro_part_size "$1" "$2")"
  isize="$(wc -c < "$3" | tr -d ' ')"
  if [ -n "$csize" ] && [ "$isize" -le "$csize" ]; then
    pass "coprocessor image fits its partition ($isize <= $csize bytes)"
  elif [ -n "$csize" ]; then
    fail "coprocessor image fits its partition" "image=$isize, partition=$csize"
  else
    echo "  [skip] coprocessor partition-size check (couldn't parse 'project show')"
  fi
}

do_full() {  # board  work
  local board="$1" work="$2" proj="$2/ota" keys
  expect_success "project new --ota" \
    $OTA project new "$proj" -f "$FW" -b "$board" --ota --dev $SDK_FLAG
  [ "$LAST_RC" -eq 0 ] || return 0
  keys="$proj/keys/trusted_keys.json"

  build_firmware "$proj" "$board"

  expect_success "build romfs (OTA bundle)" \
    $OTA build romfs "$proj" -b "$board" --allow-dev-key
  local zip="$proj/build/$board-romfs.zip"
  expect_file "OTA bundle written (<board>-romfs.zip)" "$zip"
  [ -f "$zip" ] || return 0

  expect_success "build inspect decodes the bundle" $OTA build inspect "$zip"
  expect_success "build verify (bundle)" $OTA build verify "$zip" --trusted-keys "$keys"

  # Multi-core boards (e.g. AE3) also build a plain coprocessor romfs from
  # app-coprocessor/ for the slaved second core, alongside the main OTA bundle.
  local cimg="$proj/build/$board-coprocessor-romfs.img"
  if has_coprocessor "$proj" "$board"; then
    verify_coprocessor "$proj" "$board" "$cimg"
  fi

  local ud="$work/unzip"; rm -rf "$ud"; mkdir -p "$ud"
  # Extract with Python's stdlib zipfile (always present -- the toolchain is Python), so the
  # minimal self-hosted HIL runners don't need the `unzip` binary installed.
  expect_success "unzip bundle -> romfs.img + trailer.bin" "$PY" -m zipfile -e "$zip" "$ud"
  expect_success "build verify (loose body + trailer)" \
    $OTA build verify "$ud/romfs.img" "$ud/trailer.bin" --trusted-keys "$keys"
  printf 'X' >> "$ud/romfs.img"   # append one byte: body no longer matches the trailer
  expect_verify_reject "build verify REJECTS a corrupted body (exit 1)" \
    $OTA build verify "$ud/romfs.img" "$ud/trailer.bin" --trusted-keys "$keys"

  rm -f "$cimg"   # so the next check proves factory-romfs regenerates it too
  # --no-account: CI burns a self-host golden on purpose (no [product].account_id).
  expect_success "build factory-romfs" \
    $OTA build factory-romfs "$proj" -b "$board" --allow-dev-key --no-account
  local img="$proj/build/$board-factory-romfs.img"
  expect_file "factory image written (<board>-factory-romfs.img)" "$img"
  if [ -f "$img" ]; then
    verify_factory_size "$proj" "$board" "$img"
    # The factory image is signed too: inspect decodes both slots, verify checks both.
    expect_success "build inspect (factory FRONT+BACK)" $OTA build inspect "$img"
    expect_success "build verify (factory image, both slots)" \
      $OTA build verify "$img" --trusted-keys "$keys"
    local fbad="$work/factory-corrupt.img"; cp "$img" "$fbad"
    dd if=/dev/zero of="$fbad" bs=1 seek=0 count=32 conv=notrunc 2>/dev/null  # wreck FRONT body
    expect_verify_reject "build verify REJECTS a corrupted factory slot (exit 1)" \
      $OTA build verify "$fbad" --trusted-keys "$keys"
  fi
  # factory-romfs also emits the plain coprocessor image (it has no golden/trial form).
  if has_coprocessor "$proj" "$board"; then
    verify_coprocessor "$proj" "$board" "$cimg"
  fi
}

do_classic() {  # board  work
  local board="$1" work="$2" proj="$2/plain" ota="$2/ota" keys zip fimg fbad
  # v2 FLIPPED THIS. Under v1 a partition with room for only one slot was refused as "not
  # OTA-capable"; v2 builds it in SINGLE-IMAGE mode instead (one slot, no fallback -- see
  # geometry.derive_mode), which is the whole point of the mode. So the assertion is now that
  # these boards are ACCEPTED. What is still refused is a partition with no room for an image
  # even in single mode, which is pure arithmetic -- and that is the `noromfs` class below.
  expect_success "project new --ota (single-image mode on a one-slot partition)" \
    $OTA project new "$ota" -f "$FW" -b "$board" --ota --dev $SDK_FLAG
  # ...and then actually BUILD it. Asserting only that the project can be created proves the
  # capability check flipped, not that the mode produces a usable image -- and single-image is
  # the whole reason these boards are OTA-capable at all. The artifacts are the same shape as
  # the A/B ones (a signed bundle + a factory image that fills the partition); what differs is
  # that there is one slot inside, which `build inspect` reports.
  if [ "$LAST_RC" -eq 0 ]; then
    build_firmware "$ota" "$board"
    # THE SLOT BUDGET MUST BE THE SINGLE-MODE ONE. `front_size` is an A/B concept and is 0 in
    # SINGLE mode, so the A/B arithmetic used to report a NEGATIVE budget here ("the OTA slot
    # holds -16384") and every image was over budget by construction -- `build romfs` could not
    # produce an image for ANY one-erase-sector board, which is precisely the class SINGLE is for.
    #
    # The scaffolded app does not fit these partitions either (it carries the ~200 KB CA bundle,
    # and an M4/H7-classic ROMFS slot holds 114688 bytes), so the honest assertion is that the
    # refusal is CLEAN and quotes the real single-image budget -- which is also what proves the
    # mode-aware capacity is in use, since the A/B path cannot produce that wording.
    expect_clean_fail "build romfs quotes the SINGLE-image slot budget (not a negative A/B one)" \
      1 "OTA slot (single image)" \
      $OTA build romfs "$ota" -b "$board" --allow-dev-key
  fi

  expect_success "project new (non-OTA)" \
    $OTA project new "$proj" -f "$FW" -b "$board" $SDK_FLAG
  [ "$LAST_RC" -eq 0 ] || return 0

  build_firmware "$proj" "$board"

  expect_success "build romfs (single image)" $OTA build romfs "$proj" -b "$board"
  expect_file "single image written (<board>-romfs.img)" "$proj/build/$board-romfs.img"

  expect_clean_fail "build factory-romfs refused cleanly (needs OTA project)" 1 "OTA project" \
    $OTA build factory-romfs "$proj" -b "$board"
}

do_noromfs() {  # board  work
  expect_clean_fail "project new refused cleanly (no ROMFS partition)" "" "" \
    $OTA project new "$2/plain" -f "$FW" -b "$1" $SDK_FLAG
}

# Expected capability per board (black-box: known board -> known behaviour).
class_of() {
  case "$1" in
    OPENMV4P|OPENMVPT|OPENMV_RT1060|OPENMV_AE3|OPENMV_N6) echo full;;
    ARDUINO_PORTENTA_H7|ARDUINO_GIGA|ARDUINO_NICLA_VISION) echo full;;
    OPENMV2|OPENMV3|OPENMV4) echo classic;;
    ARDUINO_NANO_33_BLE_SENSE|ARDUINO_NANO_RP2040_CONNECT) echo noromfs;;
    *) echo unknown;;
  esac
}

for board in "$@"; do
  cls="$(class_of "$board")"
  echo
  echo "=== $board  ($cls) ==="
  work="$WORKDIR/$board"; rm -rf "$work"; mkdir -p "$work"
  case "$cls" in
    full) do_full "$board" "$work";;
    classic) do_classic "$board" "$work";;
    noromfs) do_noromfs "$board" "$work";;
    *) fail "unknown board '$board' (no expected capability in this script)";;
  esac
done

echo
echo "============================================================"
if [ "$FAIL_N" -ne 0 ]; then
  echo "FAILED $FAIL_N check(s); $PASS_N passed"
  exit 1
fi
echo "OK: all $PASS_N checks passed across $# board(s)"
