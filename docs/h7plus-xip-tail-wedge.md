# The H7 Plus delta stall: a bulk XIP read that reaches the end of the QSPI (SOLVED)

Found and fixed 2026-08-09. Kept because the failure mode is a **silent brick** — no fault, no
exception, no log, no reset — and because the first two rounds of investigation reached the
wrong conclusion twice. If a board ever goes quiet mid-install again, start here.

## The symptom

On `openmv4p-ov5640` (OpenMV H7 Plus, ATWINC1500 shield, wifi), every delta-based scenario
timed out while `full` passed. The device always stopped at the same place:

```
install: representation delta
install: writing B
install: back read XIP        <- last line, then nothing
```

## The cause

**On the STM32H7 QUADSPI, a memory-mapped *burst* that reaches the final address of the mapped
device leaves the peripheral wedged. Every later memory-mapped read then hangs the AHB
forever.** The CPU keeps running — the VM is still executing bytecode — but nothing that touches
the flash window ever returns, so the install never advances and nothing is ever logged again.

The H7 Plus is exposed because its romfs partition ends *exactly* at the end of its 32 MiB QSPI:
`0x91800000 + 8 MiB = 0x92000000`.

Measured at the REPL, with the failure reproduced in about two minutes:

| what | result |
|---|---|
| 1024 × 4 KiB bulk reads of slot B, never the final block | fine |
| **one** 4 KiB bulk compare of the final block | **wedges it** — the next read of any address hangs |
| single scalar byte read at the very last address | fine (returns 0, not 0xFF) |
| bulk read stopping **16** bytes short of the end | **wedges it** |
| bulk read stopping **512** bytes short of the end | fine |

Scalar reads are safe; it is the burst. That is why `boot.py` was never affected — it reads the
same final block but `parse_trailer` only touches `data[:body_end]`, the header/meta/sig at the
*start* of the block.

## Why it was a v2 regression, and why only `delta`

- **v1** put the update target in the FRONT slot, so no loop ever walked to the end of the
  partition. Under **v2** slot B *is* the end of the partition, so `_install_stream`'s
  erase-verify walks straight into the last block on every install.
- `full` survived **on luck**: its next flash touch after the verify is a driver-mediated write
  (`rom_ioctl(4)` → `mp_spiflash`), which resets the peripheral before anything reads XIP again.
  `delta` reads its patch base straight off XIP and dies there.
- The slot survey also bulk-reads slot B's trailer block, and also survived on luck — the erase
  that follows it clears the wedge.

So the bug was never in the delta code, which is where all the early effort went.

## The fix

`_XIP_TAIL_GUARD = 512`, and every XIP alias clamped through `_clamp_to` so none can reach the
guarded tail. Reads are **shortened, never moved**, and only at the very end of the last slot,
which is trailer padding. The guarded bytes are still *written* — they are just not read back.
The write-verify compares the prefix it got; every other port returns the full `n`, so that
slice never runs there.

`_clamp_to` is deliberately lifted out of the closure that uses it so it is host-tested rather
than only witnessed on hardware: an off-by-one re-opens the brick in one direction and silently
stops verifying the tail of every slot in the other.

Verified: `delta` on the H7 Plus PASSES in 295s, writing slot B in 3 seconds and promoting
1.1.0/B.

## Two wrong conclusions, so they are not reached a third time

1. **"ulab is slow."** A single stacked-PC sample landed in `ndarray_inplace_ams`. It was
   sampling bias — ulab does 4 KiB in-place adds at ~36 MB/s on this board, ~115 ms for a whole
   slot.
2. **"A C-level park in the WINC download."** Plausible on shape (silent, no timeout) and wrong.
   The delta artifact is 4,675 bytes; there was nothing to stall on.

What actually worked, and what to reach for first next time:

- **Trace with a log line *before* each risky statement**, so the last line printed names the
  operation that killed the board. Three rounds of this narrowed it from "somewhere in the delta
  path" to one statement.
- **Get off the 40-minute HIL cycle.** The breakthrough was flashing a golden whose `main.py`
  is just `print("IDLE APP")`, which leaves the REPL free (the normal bench app blocks in C
  between check-ins and `mpremote` can never take the raw REPL). That turned a 40-minute
  experiment into a 2-minute one, and every measurement in the table above came from it.
- **Run the control.** J-Link reported the QUADSPI registers as all-zero and the flash window as
  unreadable, which looked like a dead peripheral — until the same read on a healthy booted
  board showed exactly the same thing. The debugger simply cannot reach that block.
- `ci/hil/recover.py --reset` needs `OTA_VENV` in the environment (`eval "$(ci/hil/provision.sh
  <board> <checkout>)"` first). Without it, it fails deep inside `subprocess` and every "reset"
  silently does nothing. Resetting via J-Link (`connect` / `r` / `go`) always works.
