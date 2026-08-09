# H7 Plus: delta installs stall, full installs do not (OPEN)

Recorded 2026-08-09 during the first v2 hardware sweep, so the evidence is not re-gathered
from scratch next time. **Not a v2 regression as far as the evidence goes** — but not proven
pre-existing either, and that distinction is still open.

## What is reproducible

On `openmv4p-ov5640` (OpenMV H7 Plus, ATWINC1500 shield, wifi):

| scenario | publishes | result |
|---|---|---|
| `full` | full image | **PASS**, 362s — writes 4 MiB, 80→100% in ~1s |
| `delta` | delta | **FAIL**, times out ~1400s |
| `rollback` | delta | **FAIL**, times out ~1400s |

Every delta-based scenario times out; the one full-image scenario passes comfortably. The
device goes silent at exactly the same point every time:

```
install: downloading .../OPENMV4P-ota.delta-1.0.0.gz (ocdl)
install: TLS up
install: fetched body
install: representation delta
install: writing B
install: back read XIP        <- last line, then nothing for the whole timeout
```

`back read XIP` is a one-shot witness emitted on the FIRST base read, i.e. inside the first
chunk's reconstruction. So the patch header was read off the network successfully and at
least one delta op was parsed.

## What has been ruled out

- **A malformed or degenerate patch.** The exact published artifact was replayed on the host
  against the same base region through the *device's own* `_delta_stream`: 4,194,304 bytes
  reconstructed in 1028 chunks in 0.0s. The patch is fine and the applier terminates.
- **A crash.** J-Link halt during the silence: `CFSR = 0`, `BFAR = 0`, `IPSR = 0x0F`
  (SysTick). The core is running normally, not faulted. Contrast the N6's 1-byte-write bug,
  which showed CFSR 0x8201 immediately.
- **ulab being slow.** The stacked PC at the moment of halt resolved to
  `ndarray_inplace_ams` (ulab's in-place add) — which looks damning and is **sampling bias**:
  a SysTick sample lands in whatever is hottest. Measured on the board itself, 64 × 4 KiB
  in-place adds take **7 ms — ~36 MB/s**, so the whole 4 MiB slot is ~115 ms of ulab. It is
  not the bottleneck. Do not re-chase this.

## What is still unproven

The remaining suspect is the download stalling mid-patch: a recv that never returns and never
times out, so `_SOCK_TIMEOUT` (30 s) cannot fire and no retry happens. That is the shape of
the documented **C-level park** class, and the WINC1500 is its worst case. But it is NOT
proven — the one halt sampled a hot function rather than the blocked call, and no second
sample was taken while genuinely stalled.

**Next step when the board is free:** re-run `delta` under `ci/hil`-style stall detection and
take *several* stacked-PC samples inside the silent window (MSP+0x18), resolving each with
`arm-none-eabi-addr2line`. A parked mbedtls/WINC read will show up repeatedly; a slow loop
will show a moving PC. One sample is not enough — that is the mistake this document exists to
stop being repeated.

Related: `project_c_level_park_hang`, and the openmv-ota memory note on the Nicla `full`
download hang (same silent shape, seen once).
