# openmv-ota — working notes

Secure OTA tooling for OpenMV cameras: a host-side CLI (project/build/romfs/flash),
an update server, an on-device installer, and the device-side SDK.

## Layout

- `src/openmv_ota/` — host-side CLI, build, signing, server.
- `src/openmv_ota/build/device/` — **device code**. Ships to the camera and runs
  there. Everything under this directory is bound by the RAM budget below.
  - `openmv_ota/` — the update runtime (check-in loop, status/confirm/sync) and
    `data/installer.py` (streamed flash install).
  - `openmv_cloud/` — the cloud SDK (`csi` live video, `logs`, `datalog`) plus
    `_lib.py`, the shared plumbing every wrapper imports.
  - `boot.py`, `openmv_log.py`, `openmv_wdt.py` — frozen survival modules.

## RAM budget (device code)

**Device code runs inside the user's application. Our memory is their memory.**
A camera has a few hundred KB of heap and the app wants it for frame buffers. If
our logging or update machinery takes a big bite, we break the user's program —
and we break it intermittently, under exactly the conditions (long outage, big
file, slow network) that are hardest to reproduce.

The rule: **no allocation may be sized by something we do not control.**

Things we do not control: a spool file's size, an HTTP response body, a length
field off the wire, a queue that grows while the network is down, a length
varint inside a patch we haven't verified yet.

In practice:

1. **Read in bounded windows.** A few KB (`_CHUNK = 4096` is the house size).
   Never `f.read()`, never `reader.read(-1)`, never a `read_all()`.
2. **Stream anything larger.** Process a window, advance an offset, repeat. The
   installer is the reference implementation: chunked write + verify, delta
   applied as a generator, neither image ever whole in RAM.
3. **Cap every queue in bytes** and drop (or spill to disk) at the ceiling.
   An unbounded queue is just a slow OOM.
4. **Alias, don't copy.** `memoryview` over an encoder's buffer;
   `uctypes.bytearray_at` over XIP flash. `csi` sends JPEGs zero-copy and
   `boot.py` reads whole partitions with zero allocation — match that bar.
5. **Ceiling anything the wire declares** *before* it reaches a reader, and
   reject rather than allocate.
6. **Don't build a big buffer just to hand it over.** Write header and body
   separately; spill a backlog record-by-record instead of joining it.

Large allocations are not forbidden — *uncontrolled* ones are. When you must
hold something big (a JPEG frame), size it deliberately, reuse one buffer, and
say so in a comment.

### Enforcement

- Every device module carries a `RAM BUDGET:` note in its docstring.
- `tests/build/test_device_ram_budget.py` fails CI on `read()`/`read(-1)`/
  `read_all()` in device code, and on any device module missing the note. A
  genuine exception takes `# ram-ok: <reason>` on the line.

## Testing

- 100% coverage is enforced (`--cov-fail-under=100`); `ruff check .` must pass.
- Device code is host-tested for its pure logic; socket/filesystem entry points
  are `# pragma: no cover` and exercised on hardware. Because those paths never
  run in CI, check them with `ruff check --select F` (undefined names) — a bad
  reference there will not fail a test.
