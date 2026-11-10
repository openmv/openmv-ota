#!/usr/bin/env python3
"""Is a board's firmware ciphertext where it is stored? A judgement over bytes.

A board whose boot stub keeps the firmware area encrypted (the OpenMV N6) deciphers it inside
the chip as it runs, so nothing the running camera says shows what its flash chip holds. The
bootloader does: it runs before the stub, with the cipher engine off, and a DFU read of the
FIRMWARE partition returns the stored bytes. ``judge`` compares such a read-back with what the
build produced -- the stub image and the firmware, both plaintext on the host -- and says why
it is not a firmware encrypted at rest, if it is not. The read itself is ota_cycle's
``check_at_rest``.
"""

import math

BLOCK = 16                      # the cipher's block, and the flash's write unit
MIN_ENTROPY = 7.9               # bits per byte: a firmware image measures ~7.4, ciphertext ~8.0
_SAMPLE = 32                    # bytes of the build looked for anywhere in the area
_SAMPLES = 64


def entropy(data):
    """Shannon entropy of ``data`` in bits per byte (0.0 for nothing)."""
    if not data:
        return 0.0
    counts = [0] * 256
    for b in data:
        counts[b] += 1
    n = len(data)
    return -sum(c / n * math.log2(c / n) for c in counts if c)


def samples(firmware):
    """Up to ``_SAMPLES`` pieces of the build, evenly spread, each varied enough to be the
    build's and not padding (a run of one byte would be found in any blank flash)."""
    out = []
    step = max(_SAMPLE, len(firmware) // _SAMPLES)
    for off in range(0, len(firmware) - _SAMPLE + 1, step):
        piece = firmware[off:off + _SAMPLE]
        if len(set(piece)) >= 8:
            out.append(piece)
    return out[:_SAMPLES]


def judge(dump, stub, firmware, firmware_offset, area_end):
    """``(problems, numbers)`` for ``dump``, a read-back of the FIRMWARE partition from its
    start: ``firmware_offset`` is where the firmware area starts in it (after the stub's
    sector), ``area_end`` where it ends (the install record). ``problems`` is empty when the
    partition holds this build's stub in the clear and its firmware as ciphertext, and nothing
    else:

      - no 16-byte block of the firmware is stored as built;
      - no sample of the build appears anywhere in the area (plaintext at another offset);
      - the stored firmware has the entropy of ciphertext -- the one test that does not lean
        on the build, so some other firmware in the clear fails it too;
      - everything after the firmware, up to the record, is blank (an older, longer firmware's
        tail was found there in the clear on the first read-back ever made)."""
    if len(dump) < area_end or firmware_offset + len(firmware) > area_end:
        return (["the read-back (%d bytes) does not cover the firmware area (0x%x..0x%x, "
                 "firmware %d bytes)" % (len(dump), firmware_offset, area_end, len(firmware))], {})
    area = dump[firmware_offset:area_end]
    stored = area[:len(firmware)]
    blocks = len(firmware) // BLOCK
    same = sum(1 for i in range(0, blocks * BLOCK, BLOCK)
               if stored[i:i + BLOCK] == firmware[i:i + BLOCK])
    found = sum(1 for piece in samples(firmware) if area.find(piece) >= 0)
    end = -(-len(firmware) // BLOCK) * BLOCK         # programmed in whole write units
    tail = area[end:]
    n = {"stub_ok": dump[:len(stub)] == stub, "blocks": blocks, "same": same, "found": found,
         "entropy": round(entropy(stored), 3), "stale": len(tail) - tail.count(0xFF)}
    problems = []
    if not n["stub_ok"]:
        problems.append("the partition does not start with this build's stub")
    if same:
        problems.append("%d of %d firmware blocks are stored exactly as built" % (same, blocks))
    if found:
        problems.append("%d samples of the build appear in the firmware area" % found)
    if n["entropy"] < MIN_ENTROPY:
        problems.append("the stored firmware has %.3f bits of entropy per byte (ciphertext "
                        "has close to 8)" % n["entropy"])
    if n["stale"]:
        problems.append("%d bytes after the firmware are not blank" % n["stale"])
    return problems, n


def judge_slots(dump, slot_size, images, control=4096):
    """``(problems, numbers)`` for ``dump``, a read-back of the ROMFS partition from its start,
    on a board whose stub keeps the slots ciphertext at rest. ``images`` are the plaintext slot
    images that may be in it (each a whole slot: body, pad, status sector, trailer); a slot is
    matched to its image by the trailer, which is stored in the clear. ``problems`` is empty
    when both slots hold a known image as ciphertext and nothing else:

      - the trailer names one of the images;
      - no 16-byte block of the ciphered range (the body and every component, to the cipher's
        grain) is stored as built;
      - no sample of the image appears anywhere in the slot's body;
      - the ciphered range has the entropy of ciphertext;
      - the pad between the ciphered range and the status sector is blank."""
    from openmv_ota.ota import slot_crypt
    from openmv_ota.ota.trailer import parse_trailer

    if len(dump) < 2 * slot_size:
        return (["the read-back (%d bytes) does not cover two %d-byte slots" % (len(dump), slot_size)],
                {})
    problems, per = [], []
    for i, name in enumerate("AB"):
        s = dump[i * slot_size:(i + 1) * slot_size]
        trailer = s[slot_size - control:]
        image = next((img for img in images if img[slot_size - control:slot_size] == trailer), None)
        if image is None:
            problems.append("slot %s holds no trailer of a known image" % name)
            per.append({"slot": name})
            continue
        csize = slot_crypt.crypt_size(parse_trailer(trailer), slot_body_end=slot_size - 2 * control)
        stored, plain = s[:csize], image[:csize]
        blocks = csize // BLOCK
        same = sum(1 for off in range(0, blocks * BLOCK, BLOCK)
                   if stored[off:off + BLOCK] == plain[off:off + BLOCK])
        body = s[:slot_size - 2 * control]
        found = sum(1 for piece in samples(plain) if body.find(piece) >= 0)
        pad = s[csize:slot_size - 2 * control]
        n = {"slot": name, "blocks": blocks, "same": same, "found": found,
             "entropy": round(entropy(stored), 3), "stale": len(pad) - pad.count(0xFF)}
        per.append(n)
        if same:
            problems.append("slot %s: %d of %d blocks are stored exactly as built" % (name, same, blocks))
        if found:
            problems.append("slot %s: %d samples of its image appear in the clear" % (name, found))
        if n["entropy"] < MIN_ENTROPY:
            problems.append("slot %s: %.3f bits of entropy per byte (ciphertext has close to 8)"
                            % (name, n["entropy"]))
        if n["stale"]:
            problems.append("slot %s: %d bytes between its body and its status sector are not blank"
                            % (name, n["stale"]))
    return problems, {"slots": per}
