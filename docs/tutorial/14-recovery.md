# Recovery

*[← 13 · Logging & the watchdog](13-logging-and-watchdog.md) · [Index](00-introduction.md) · [15 · The update server →](15-update-server.md)*

---

When `boot.py` rejects **every** slot — a torn install interrupted at the worst
moment, corruption that fails both signatures — there is no image left to run.
The device hands off to **firmware-resident recovery**: a frozen flow that
brings up the network and re-downloads until a working image exists, then
reboots into its trial. It lives in the firmware, where no update can erase it,
and reserves no flash of its own.

Because there is nothing below it to fall back to, its rules differ from the
update path's:

- **It never gives up.** It retries forever, backing off so a fleet facing a
  dead server settles into a slow poll instead of a stampede: 5 s, 15 s, 60 s,
  5 min, then every 15 min — capped, so a device that has been down all day
  still notices the fix within minutes of it landing.
- **It assumes nothing is mounted.** `/rom` is gone or unbootable — that is why
  it is running — so it uses only frozen modules and constants stamped into the
  firmware.
- **It trusts nothing it downloads.** The image goes through the same signature,
  identity, and anti-rollback checks as any other install — the trusted keys and
  the floor are exactly the ones an ordinary update faces.

## What the firmware carries

Recovery mostly rides what the firmware already carries: the trusted signing
keys (`boot.py` verifies with them every boot) and the frozen installer. Two
things are stamped in specifically for recovery — the pieces the runtime
normally reads from the romfs, which is exactly what is gone:

| Stamped for recovery | From |
| --- | --- |
| the server URL | `server_url` in `openmv-ota.toml`'s `[ota]` table |
| its own copy of the TLS trust store | the project's `[ota].ca` when set; otherwise the full public CA bundle — the runtime's copy lives in the romfs, which recovery cannot read |

**Set `server_url`.** It is the line recovery cannot function without — a
firmware built without it logs a critical error and stops, because no amount
of retrying fixes a build mistake.

```toml
[ota]
server_url = "https://updates.example.com"
```

The trust store has a working default: on the OpenMV N6, AE3, and RT1062 the
firmware is large enough to carry the full public CA bundle, so a server
behind a public CA needs no configuration at all. On the smaller boards the
~186 KB bundle does not fit in firmware, so `[ota].ca` must point at a PEM
file holding the root(s) your server chains to — one certificate or a small
bundle, a few KB — and both `project new --ota` and `build firmware` refuse
to build those boards without one, rather than ship a recovery with no trust
anchors. Setting `[ota].ca` is never wrong on any board: your device talks to
one server, so its root is all it actually needs.

## The network settings file

The one thing a build cannot know is the **end user's network** — credentials
differ per device and change after it ships, which is precisely the situation
that strands a board. So they live in a hand-editable file on the user disk,
`/flash/openmv-recovery.txt`:

```
# OpenMV recovery network settings -- used ONLY if the device cannot update any other way.
# Safe to delete. Edit, save, eject the drive, power-cycle.
interface    = wifi          # wifi | eth
wifi.ssid    = MyNetwork
wifi.psk     = secret        # rewritten in an obfuscated form on the next boot
ipv4         = dhcp          # dhcp | static
# ipv4.address = 192.168.1.50
# ipv4.netmask = 255.255.255.0
# ipv4.gateway = 192.168.1.1
```

**Being user-visible is the point.** A device stranded after its owner changed
their WiFi is recoverable by dropping a file onto a drive that is already
mounted over USB — no reflash, no JTAG, no RMA. The format is `key = value`
with comments rather than JSON because a person edits it in a bad situation,
and the documented defaults tell them what to type.

Recovery tries the configured interface first but not only: a board with an
Ethernet cable is always worth one DHCP attempt (it needs nothing from the
user), and a file that names `interface = eth` but also carries `wifi.ssid`
credentials gets a WiFi attempt after it — the stranded device is exactly the
one whose primary plan turned out wrong. A static address applies only to the
interface it was written for.

The WiFi passphrase is rewritten in an **obfuscated** form on the next boot —
obfuscation, not security: it is keyed on the device's own id, so anyone who
can read the file can undo it. It still beats a passphrase sitting in
plaintext inside an image anyone can pull.

## The honest limit

Recovery needs the network. A device with no bootable slot *and* no way online
— wrong credentials, no cable, no coverage — needs a physical reflash
([`flash factory`](09-flashing.md)). That is the cost single-image boards
accept in exchange for a whole slot of app room; A/B devices only ever reach
recovery if both slots are damaged at once.

---

*[← 13 · Logging & the watchdog](13-logging-and-watchdog.md) · [Index](00-introduction.md) · [15 · The update server →](15-update-server.md)*
