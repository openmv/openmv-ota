# Device keys

Every camera that has keys holds two P-256 private keys that never leave it:

- the **identity key**, which signs. A signature from it proves which camera is talking;
- the **exchange key**, which only does ECDH. With it a server can wrap something that only
  this camera can open.

One key, one job: the identity key never does ECDH and the exchange key never signs.

Where the keys live depends on the board. Some boards have a **hard secure element**, a
separate chip that holds the keys and does the signing and the ECDH itself. The rest have a
**soft secure element**: the camera keeps the keys in its own flash and the firmware's mbedtls
does the cryptography. Both look the same to code on the camera, through `openmv_ota.se`.

## Per board

| Board | Kind | Identity key | Exchange key |
| --- | --- | --- | --- |
| OpenMV Cam RT1062 | hard: NXP SE050 | NXP's factory key `0xF0000000`, with NXP's certificate at `0xF0000001` | `0x4F4D0002`, generated on the chip |
| Arduino Nicla Vision, Portenta H7 | hard: NXP SE050 | as above | as above |
| Arduino Giga R1 | hard: Microchip ATECC608 | slot 2, generated on the chip | slot 3, generated on the chip |
| OpenMV Cam M4, M7, H7, H7 Plus, Pure Thermal | soft | the key area at the end of the boot partition | the same record |
| OpenMV Cam N6 | soft, sealed by the chip | the key area in the boot partition of its NOR flash | the same record |
| OpenMV Cam AE3 | none yet | | |

On the SE050 the exchange key's policy allows key agreement and reading its public half only:
it can't sign, and it can't be deleted or regenerated. The factory identity key can't be
erased either.

## On the camera

```python
from openmv_ota import se

keys = se.open()                  # None on a board without keys
keys.public_key()                 # 65 bytes, 04 || X || Y: the identity key
keys.certificate()                # the chip maker's DER certificate (SE050), or None
keys.sign(digest)                 # DER ECDSA over a 32-byte SHA-256 digest
keys.random(32)                   # bytes from the hardware RNG
keys.ecdh_public_key()            # 65 bytes: the exchange key
keys.ecdh(peer)                   # 32 bytes: the ECDH secret with a peer's public key
```

`se.open()` only reads. On a camera that was never provisioned it raises `NotProvisioned`, and
on damaged keys it raises `OSError`. An application never creates keys, and a camera in the
field never changes its identity on its own: it either has its keys or it fails.

The romfs ships only the module for the board's own kind of keys: one chip driver, or `soft`.

## Provisioning

Keys are made once, at a desk, by `se.provision()`, and the only place that calls it on the
camera is `boot.py`, booting a **factory image**. A factory image is written only by `openmv-ota
flash factory` and is signed with a factory key; an OTA update never installs one. Booting it,
before the app runs, the camera makes its keys if it has none (never replacing keys it has).

For two seconds of that boot the camera also listens on its USB console for the flashing tool:
`flash factory` sends `OMVKEYS <challenge>` from the moment the camera is back on USB until it
answers with both public keys and a signature over the challenge. The tool verifies the
signature against the identity key and prints both keys:

```
Flashed OPENMV2-firmware.bin -> alt 2 (OPENMV2)
Flashed OPENMV2-factory-romfs.img -> alt 3 (OPENMV2)
keys made: identity 041591b4aef12b0f98f456477bdb7c246b0d5c2262ec3db94febf4856b871ac01217fd3856f9b0af3ffe6ba5f2cc6fab0f635a3fef7d3e28e4debe322fd4fc0a93, exchange 04e3e3e35592ada9f6cd5e02aa4d816612d3cd93c3513aaaf6c630917491eee6b76ab3d3e4743bc132b8cd724341a84c190bb7c49c762bb861018880543616bcd3 (OPENMV2)
```

Nothing interrupts the app to do this, so it works with an app that arms a watchdog as it
starts. A camera that already has keys keeps them and reports `keys present`; a camera whose keys
are damaged fails the flash. `flash firmware` and `flash romfs` don't write a factory image and
don't provision.

What provisioning does per kind:

- **SE050:** generates the exchange key on the chip. The identity key is NXP's.
- **ATECC608:** the chip leaves Microchip's factory blank. Provisioning writes Arduino's
  configuration (ArduinoECCX08's `ECCX08_DEFAULT_TLS_CONFIG`), reads it back and compares it,
  locks the configuration and the data zone, generates both keys, and writes a record of them
  to slot 8. The locks are one-way for the life of the chip; the keys are not, since slots 2
  and 3 allow key generation after the lock. A chip someone else configured is refused.
  Arduino Cloud onboarding regenerates the key in slot 0 every time it runs and never touches
  slots 2, 3 or 8, so the board can still be onboarded there without changing the camera's
  identity. `flash` notes when it configures a blank chip.
- **Soft:** draws both keys from the hardware RNG and writes one record to the key area.

## The soft key area

The key area is 4 KB at the end of the boot partition, past the bootloader. Firmware and romfs
updates never touch the boot partition, so the keys stay where they are, and a later lockdown's
read protection covers them there. The build refuses a bootloader that would grow into it.

| Board | Key area | Bootloader may use |
| --- | --- | --- |
| M4 (STM32F427), M7 (STM32F765) | `0x08007000`–`0x08007FFF` | 28 KB |
| H7, H7 Plus, Pure Thermal (STM32H743) | `0x0801F000`–`0x0801FFFF` | 124 KB |
| N6 (STM32N657) | NOR `0x7F000`–`0x7FFFF`, its own 4 KB sector | 252 KB (the backup bootloader slot starts at `0x40000`) |

On the STM32 boards the key area shares a flash sector with the bootloader, so **flashing the
bootloader erases the camera's keys**. `flash bootloader` warns about it. The next `flash`
provisions new keys: a new identity, to be registered again.

### Format

The area holds sixteen 256-byte slots, filled from the top of the area down. Flash here is
written once and never erased, so a record can't be changed in place: a later record goes in
the next blank slot, and the newest complete record is the camera's keys. Slot 0 is the area's
last 256 bytes.

A record is eight 32-byte words. 32 bytes is the largest flash write unit among these boards
(the H7's, where each 32-byte word can be programmed only once), and every field is aligned to
its own size:

| Word | Offset | Contents |
| --- | --- | --- |
| 0 | `0x00` | description: `OMVK`, version `01`, protection (`00` plain, `01` sealed), key count, a reserved byte, then one type byte per key (`01` identity, `02` exchange) |
| 1–5 | `0x20` | the keys, 32 bytes each, in the order of their types |
| 6 | `0xC0` | sealed records only: GCM IV (bytes 0–11) and tag (bytes 16–31) |
| 7 | `0xE0` | check: SHA-256 of words 0–6 exactly as stored |

Bytes and words with nothing in them are left blank (`0xFF`) and never programmed. The keys
and word 6 are written first; the check is written **last**, and it is what commits the record.

The M4's record, read back over SWD:

```
08007F00 = 4F 4D 56 4B 01 00 02 FF 01 02 FF FF FF FF FF FF
```

### Opening a slot

| The slot's check word | Meaning | What happens |
| --- | --- | --- |
| blank | the write stopped before its last step; its keys were never used | skipped |
| SHA-256 of words 0–6 | a complete record | the newest one is the camera's keys |
| anything else | damaged, or cut off during the final write | an error; never new keys |

A complete record whose version, protection or key types this firmware doesn't know is an
error ("from newer firmware"), never skipped, so an older firmware can't mistake a newer
identity for none. The check is SHA-256 over words 0–6 in every version, for the same reason.

A power cut can only interrupt provisioning at a desk; the H7's flash ECC turns a read of a
half-programmed word into a bus fault, so an H7 with such a slot is provisioned again there.

## Sealed keys on the N6

The N6 has no internal flash. Its key area is in the external NOR it boots from, and that chip
can be read off the board with the N6 not involved. So the N6 seals its keys: they are stored
encrypted with AES-256-GCM in the N6's SAES engine, keyed by the **DHUK**, a key derived in
hardware from a secret ST programs into every chip and that no code can read. The record's
description is authenticated with them. The NOR alone opens nothing, and neither does a copy of
it on another N6.

- The AES-GCM is ST's HAL driver; the camera's code only sets it up.
- Before using the DHUK the camera checks that the chip reports it valid (`BSEC_SR.HVALID`).
  Without it, SAES would silently use a fixed key, so the camera refuses instead.
- The DHUK depends on the chip's hide level (HDPL) and security state. The firmware always
  uses it as it runs: secure, privileged, at HDPL 1. A firmware that ran at another level would
  derive another key and could not unseal the record.
- A record that can't be unsealed is an error, like a damaged one.

## What each kind protects against

| Threat | Hard (SE050, ATECC608) | Soft, plain (STM32) | Soft, sealed (N6) |
| --- | --- | --- | --- |
| Reading the key out of the camera's flash | the key is never in flash | readable until lockdown | the NOR holds only sealed keys |
| Copying the keys to another camera | not possible | possible until lockdown | the sealed keys open on this chip only |
| Code running on the camera using the keys | possible | possible | possible |
| Code running on the camera reading the keys | not possible | possible | possible |

Code on the camera is trusted: anything running there can ask for a signature. Hiding keys
from it is not a goal of this design; keeping them in the camera and out of reach of anyone
holding only its flash is.
