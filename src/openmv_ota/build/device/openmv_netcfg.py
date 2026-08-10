"""The recovery network settings on ``/flash`` -- the ONLY thing a device cannot be told at
build time.

Everything else recovery needs is the maker's and constant per build, so the firmware carries
it: the server URL, the TLS CA, the trusted keys, the geometry. The network credentials belong
to the **end user**, they differ per device, and they change after the device ships -- which is
precisely the situation that strands a board. So they live on ``/flash``, the one area neither
an OTA nor a firmware update rewrites.

**Being user-visible is the point.** A device stranded because someone changed their WiFi after
a bad update is recoverable by dropping a file onto a drive that is already mounted over USB --
no reflash, no JTAG, no RMA. That is worth designing for, and it is why the format is
``key = value`` with ``#`` comments rather than JSON: this file is hand-edited by a person in a
bad situation, JSON cannot carry the documented defaults that tell them what to type, and its
failure mode is a silent parse error from a stray comma or a smart quote pasted by a phone.

    # OpenMV recovery network settings -- used ONLY if the device cannot update any other way.
    # Safe to delete. Edit, save, eject the drive, power-cycle.
    interface    = wifi          # wifi | eth
    wifi.ssid    = MyNetwork
    wifi.psk     = secret        # rewritten in an obfuscated form on the next boot
    ipv4         = dhcp          # dhcp | static
    # ipv4.address = 192.168.1.50
    # ipv4.netmask = 255.255.255.0
    # ipv4.gateway = 192.168.1.1

**The PSK obfuscation is obfuscation, not security, and is documented as such** -- see
:func:`obfuscate`. It is keyed on the device UID, so anyone who can read the file can also read
the UID and undo it. It is still strictly better than the status quo, where the PSK sits in
plaintext in ``main.py`` inside the romfs, readable by anyone who pulls the image.

RAM BUDGET: this module runs inside your application, so its memory is your memory. Every
buffer here has a ceiling. Nothing is sized by a file's length, a response body, a length field
off the wire, or a queue that grows while the network is down: reads use bounded windows of a
few KB, anything larger is streamed, and large data is aliased with memoryview/bytearray_at
rather than copied.
"""

import binascii
import hashlib

PATH = "/flash/openmv-recovery.txt"
BACKUP = "/flash/openmv-recovery.bak"

# A hand-edited settings file is a few hundred bytes. The cap exists so a device that finds a
# multi-megabyte file where its config should be (a mis-drop onto the drive, a filesystem that
# handed back garbage) refuses it instead of reading it into a heap it shares with the app.
MAX_BYTES = 4096

_OBFUSCATED = "enc:"          # marks a PSK that has already been rewritten


def _strip_value_comment(value):
    """Drop a trailing ``# comment`` from a VALUE, keeping ``#`` that is part of the value.

    ``#`` is a common password character, so splitting on every ``#`` -- the obvious
    implementation -- silently truncates the password and presents as "the right password does
    not work", on the recovery path, with nothing to point at. Quoting would be the other fix
    and it is worse: someone hand-editing a file in a bad situation should not have to know our
    quoting rules.

    So a comment is a ``#`` that follows whitespace AND comes after the value has started. That
    keeps ``pa#ss``, keeps a value that begins with ``#``, and still allows the trailing
    comments the shipped file uses to explain itself. The one thing it cannot express is an
    EMPTY value with a trailing comment, which is not a thing anyone writes."""
    value = value.strip()
    for i in range(1, len(value)):
        if value[i] == "#" and value[i - 1] in " \t":
            return value[:i].strip()
    return value


def parse(text):
    """``key = value`` lines into a dict. Comments (``#``) and blank lines are dropped.

    Deliberately forgiving, because the alternative is a stranded device: unknown keys are
    kept rather than rejected (a newer firmware's setting in an older reader is not an error),
    a line with no ``=`` is skipped rather than fatal, keys are lower-cased and stripped, and
    an inline ``#`` comment ends a value. What it will NOT do is guess -- a value it cannot
    make sense of is reported by the caller that needs it, not silently defaulted here."""
    out = {}
    for raw in text.split("\n"):
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().lower()
        if key:
            out[key] = _strip_value_comment(value)
    return out


def obfuscate(psk, uid):
    """Reversibly hide a PSK from a casual look at the drive. **Not encryption.**

    The keystream is SHA-256 of the device UID, repeated. Anyone with USB access can read the
    UID and undo this in a few lines, and the docs say so -- the threat it addresses is a
    person glancing at a mounted drive, or a support ticket with a screenshot of the file
    attached, not an attacker with the hardware in hand. Nothing else in the system trusts
    this: the update itself is protected by the image signature."""
    data = psk.encode() if isinstance(psk, str) else psk
    key = hashlib.sha256(uid).digest()
    out = bytearray(len(data))
    for i in range(len(data)):
        out[i] = data[i] ^ key[i % len(key)]
    return _OBFUSCATED + binascii.hexlify(bytes(out)).decode()


def deobfuscate(value, uid):
    """The plaintext PSK from either form -- an obfuscated value or one the user just typed.

    Accepting both is what makes the file editable: a user types their password in the clear,
    and the next boot rewrites it. A value that claims to be obfuscated but is not decodable is
    returned as-is rather than raising, so a corrupted line degrades to "wrong password" (which
    a person can diagnose) instead of an exception on the recovery path."""
    if not value.startswith(_OBFUSCATED):
        return value
    try:
        data = binascii.unhexlify(value[len(_OBFUSCATED):])
    except Exception:
        return value
    key = hashlib.sha256(uid).digest()
    out = bytearray(len(data))
    for i in range(len(data)):
        out[i] = data[i] ^ key[i % len(key)]
    try:
        return bytes(out).decode()
    except Exception:
        return value


def is_obfuscated(value):
    return value.startswith(_OBFUSCATED)


def settings(cfg, uid):
    """The parsed file as the network bring-up wants it, or ``None`` if it says nothing usable.

    ``None`` is a real answer, not a failure: a device with no credentials file falls back to
    DHCP on a wired interface, which is the right behaviour for a board on a bench with an
    Ethernet cable and no configuration at all."""
    interface = cfg.get("interface", "").lower()
    ssid = cfg.get("wifi.ssid", "")
    if not interface:
        interface = "wifi" if ssid else "eth"
    if interface not in ("wifi", "eth"):
        return None
    if interface == "wifi" and not ssid:
        return None                       # wifi with no network to join is not a configuration
    out = {"interface": interface, "ssid": ssid,
           "psk": deobfuscate(cfg.get("wifi.psk", ""), uid),
           "ipv4": cfg.get("ipv4", "dhcp").lower()}
    if out["ipv4"] == "static":
        # A static address needs all three or it is not one. Falling back to DHCP here would
        # be worse than refusing: on a network without a DHCP server the device would look
        # like it had simply failed, with nothing pointing at the half-filled config.
        for field in ("address", "netmask", "gateway"):
            value = cfg.get("ipv4." + field, "")
            if not value:
                return None
            out[field] = value
    return out


def render(cfg):
    """The file's text with the PSK in whatever form ``cfg`` holds -- used to rewrite a
    plaintext PSK as an obfuscated one, keeping the user's other settings and the comments
    that tell them what the settings mean."""
    lines = ["# OpenMV recovery network settings -- used ONLY if the device cannot update any",
             "# other way. Safe to delete. Edit, save, eject the drive, power-cycle.",
             "#",
             "# The password below is obfuscated, NOT encrypted: anyone with access to this",
             "# drive can recover it. Type a new one in the clear and it will be rewritten.",
             ""]
    for key in sorted(cfg):
        lines.append("%-14s = %s" % (key, cfg[key]))
    return "\n".join(lines) + "\n"
