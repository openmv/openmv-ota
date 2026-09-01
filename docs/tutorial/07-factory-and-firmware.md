# Factory & firmware

*[← 6 · Building](06-building.md) · [Index](00-introduction.md) · [8 · Release artifacts →](08-release-artifacts.md)*

---

Two artifacts a device needs before it has ever taken an update: the full ROMFS
partition image written at manufacture, and the firmware that boots it.

## build factory-romfs

`build factory-romfs` produces the **whole partition image flashed at the
factory**, composed from the signed bundle (body + trailer) that an OTA
project's [`build romfs`](06-building.md#build-romfs) packs — run internally,
so this is still one command from app source. It requires an OTA project: a
plain project has nothing to compose — its image is budgeted against the whole
partition and is flashed as-is at manufacture.

```bash
openmv-ota build factory-romfs ./my-product
# -> build/<board>-factory-romfs.img          (sized to the exact partition)
# -> build/factory/<board>-ota.img.gz         (the same bytes in release form,
#    build/factory/<board>-manifest.bin        publishable as the fleet's first delta base)
```

The `build/factory/` pair is a step one is expected to complete: **publish it to the
update server once, right after manufacture** —

```bash
openmv-ota client publish ./my-product -b OPENMV_N6 -o build/factory
```

— because it is the fleet's **first delta base**. Every device leaves the factory
running exactly these bytes, and the server can only build-plan deltas against images
it stores; skip this and the fleet's first update silently downloads in full instead
of as a small patch. (`client publish` is covered on [page 15](15-the-client.md).)

It composes the same compiled body into both slots:

| Slot | Status sector | Role |
|---|---|---|
| **A** | `confirmed`, install counter **2** | boots first (higher counter) |
| **B** | `confirmed`, install counter **1** | the fallback, and the target of the first update |

Both slots hold the **same signed image** in the **same shape** — neither is a
protected factory copy. Only the install counter tells them apart, so which one
boots is decided by exactly the rule that decides it after every later update, and
a device has a real fallback from its very first boot. The slots are equal-sized
(an OTA image must be installable into either), each ending in the
[two control sectors](04-ota-projects.md#what---ota-changes) with `0xFF` padding
before them; a partition whose half doesn't divide evenly leaves the sub-block
remainder unused. On a board too small for two slots, the same command writes one
slot spanning the partition (single-image mode) — everything above holds, minus
the fallback.

### Signed with a factory key

A factory image is signed with a **factory** key, not an OTA key — the signer
defaults to `0x0001`; pass `--factory-key 0x0002` to select another. The key must
be a `factory`-role entry in `keys/trusted_keys.json` with its private key
present; an `ota`-role key is refused.

**A factory key is *yours*, not the factory's.** You sign, and you ship the
manufacturer the finished `<board>-factory-romfs.img` — a flat binary they write
to flash. They never receive a private key, the project, or this tool; a contract
manufacturer is a flashing station, not a build host. If a third party genuinely
must sign on their own hardware, sign through a service or HSM where the key
never leaves your control.

The per-site key id is therefore for **attribution, not key isolation**: distinct
ids tell you which production run cut an image and let you `revoke` one run
without touching the others. It is *not* an anti-overproduction control — a
manufacturer holding a signed image can flash any number of boards; metering
units is per-device registration's job, separate from signing. Factory keys are
assigned and revocable but never rotated: you retire a compromised run's id, you
don't roll a live one.

## build firmware

`build firmware` runs the firmware repo's own `make` in the pegged checkout, so
the result is byte-for-byte what the firmware build produces:

```bash
openmv-ota build firmware ./my-product
```

For each board it runs `make TARGET=<board>` and collects the results into
`<project>/build/`: an stm32 board's `firmware.bin` as `<board>-firmware.bin`, an
Alif board's per-core images as `<board>-firmware-M55_HP.bin` / `-M55_HE.bin`, and
— when the port builds one — `bootloader.bin` as `<board>-bootloader.bin` (plus,
on the AE3, the padded `firmware_pad.toc`) for `flash bootloader`. Firmware is
built per board, not per partition.

The project's OTA flag steers the build automatically:

- **Non-OTA project:** just builds the firmware.
- **OTA project:** additionally freezes the OTA **`boot.py`** into the image —
  without touching the firmware tree. A temporary *wrapper manifest* `include`s
  the board's own manifest, adds the boot script, and is passed as
  `make FROZEN_MANIFEST=<wrapper>`. The frozen `boot.py` runs after the board's
  stock `_boot.py` and does the slot selection, signature verification, and
  trial-boot machinery; its generated `_ota_config.py` (trusted keys, slot
  geometry, board/product ids) is frozen alongside it.

The build is **clean by default** (`make clean` first), so a stale tree can't
turn into a confusing link-time failure; pass `--incremental` to skip the clean
when the tree is known good. Building firmware needs the firmware toolchain
(`make` + the board's cross compiler).

| Flag | Effect |
|---|---|
| `-b, --board NAME` | Build only this board (repeatable; default: all boards). |
| `-o, --output DIR` | Output directory (default: `<project>/build`). |
| `-j, --jobs N` | Parallel make jobs (default: CPU count). |
| `--incremental` | Skip the clean rebuild (only when the tree is known good). |
| `-f, --firmware PATH` | Firmware checkout override. |
| `--keep-build-dir` | Keep the generated wrapper-manifest dir (OTA builds) for inspection. |

---

*[← 6 · Building](06-building.md) · [Index](00-introduction.md) · [8 · Release artifacts →](08-release-artifacts.md)*
