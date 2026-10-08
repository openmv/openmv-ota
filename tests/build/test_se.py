"""Host tests for the device-side secure element: ``openmv_ota.se`` and its ``se050`` driver.

The driver is pure protocol over ``machine.I2C``, so it runs here against an emulated SE050: real
T=1-over-I2C frames with their CRCs, a chip that NACKs while it works, asks for more time, chains
a long answer, and -- the bench's own failure -- holds on to a reply the host stopped reading.
The certificate and key bytes are the ones read off the bench RT1062's chip.
"""

from __future__ import annotations

import hashlib
import os
import sys
import types

import pytest

from openmv_ota.build.device.openmv_ota.se import se050

# --- the device clock, on the host --------------------------------------------------------


class _Clock:
    def __init__(self):
        self.now = 0

    def ticks_ms(self):
        return self.now

    def ticks_add(self, a, b):
        return a + b

    def ticks_diff(self, a, b):
        return a - b

    def sleep_ms(self, ms):
        self.now += ms


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(se050, "time", c)
    return c


# --- an emulated SE050 ----------------------------------------------------------------------



def _der(body):
    """A DER SEQUENCE around ``body`` (long-form length when it needs one)."""
    n = len(body)
    if n < 0x80:
        return b"\x30" + bytes([n]) + body
    return b"\x30\x82" + n.to_bytes(2, "big") + body


CERT = _der(b"\x01" * 460)                 # 464 bytes of DER, like the factory certificate
CERT_OBJECT = CERT + b"\x00" * 6           # the object pads it to a fixed size (470)
PUB = b"\x04" + bytes(range(64))
KXPUB = b"\x04" + bytes(range(64, 128))    # the exchange key the chip generates
SECRET = bytes(range(100, 132))             # its ECDH secret with any peer, here
SIG = _der(b"\x02\x01\x01\x02\x01\x02")


def _crc(data):
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
    return crc ^ 0xFFFF


def frame(pcb, inf=b"", nad=0xA5):
    head = bytes([nad, pcb, len(inf)]) + inf
    c = _crc(head)
    return head + bytes([c & 0xFF, c >> 8])


LONG = None                                 # force a TLV length form: 0x81 or 0x82


def tlv(tag, value):
    n = len(value)
    if LONG == 0x81:
        return bytes([tag, 0x81, n]) + value
    if LONG == 0x82:
        return bytes([tag, 0x82]) + n.to_bytes(2, "big") + value
    if n < 0x80:
        return bytes([tag, n]) + value
    if n < 0x100:
        return bytes([tag, 0x81, n]) + value
    return bytes([tag, 0x82]) + n.to_bytes(2, "big") + value


ATR = bytes.fromhex("00a0000003960403e800fe020b03e8080100000000640000"
                    "0a4a434f5034204154504f")


class Chip:
    """Speaks T=1 over I2C the way the bench's SE050 does. ``busy`` NACKs the next reads,
    ``wtx`` asks for more time before the next answer, ``chain`` splits it in two, ``sw``
    overrides the status word and ``corrupt`` breaks the next answer's CRC. ``kx`` is whether
    the exchange key exists yet (a provisioned camera's chip: it does)."""

    addr = 0x48

    def __init__(self):
        self.out = b""            # bytes the chip is waiting to send
        self.pending = []         # whole frames queued behind the current one
        self.busy = 0
        self.wtx = 0
        self.chain = False
        self.sw = 0x9000
        self.corrupt = False
        self.rx = []              # (pcb, inf) the chip received
        self.extra = None         # a frame to send instead of the answer
        self.kx = True            # the exchange key 0x4F4D0002 exists

    # machine.I2C surface ---------------------------------------------------------------
    def writeto(self, addr, buf):
        assert addr == self.addr
        buf = bytes(buf)
        assert buf[0] == 0x5A and _crc(buf[:-2]) == buf[-2] | (buf[-1] << 8)
        pcb, inf = buf[1], buf[3:3 + buf[2]]
        self.rx.append((pcb, inf))
        if pcb == 0xCF:
            self.out = frame(0xEF, ATR)
        elif pcb == 0xE3:                       # our WTX answer: now send the reply
            self._next()
        elif pcb & 0xC0 == 0x80:                # R-block: the next chained block
            self._next()
        elif not pcb & 0x80:
            self._answer(inf)

    def readfrom_into(self, addr, buf):
        if self.busy:
            self.busy -= 1
            raise OSError(19)
        if not self.out:
            raise OSError(19)
        n = len(buf)
        buf[:] = self.out[:n]
        self.out = self.out[n:]

    def readfrom(self, addr, n):
        b = bytearray(n)
        self.readfrom_into(addr, b)
        return bytes(b)

    # the applet -------------------------------------------------------------------------
    def _next(self):
        self.out = self.pending.pop(0) if self.pending else b""

    def _answer(self, apdu):
        if len(apdu) < 4:                       # not an APDU at all: the applet refuses it
            self.out = frame(0x00, b"\x69\x85")
            return
        cla, ins, p1, p2 = apdu[:4]
        body = apdu[5:5 + apdu[4]]
        if (cla, ins) == (0x00, 0xA4):
            data = b"\x03\x01\x01\x00\x00\x00\x00"
        elif (ins, p2) == (0x02, 0x07):
            data = tlv(0x41, len(CERT_OBJECT).to_bytes(2, "big"))
        elif (ins, p2) == (0x02, 0x00) and body[2:6] == b"\xf0\x00\x00\x01":
            off = int.from_bytes(body[8:10], "big")
            n = int.from_bytes(body[12:14], "big")
            data = tlv(0x41, CERT_OBJECT[off:off + n])
        elif (ins, p2) == (0x04, 0x27):         # CheckObjectExists
            known = body[2:6] == b"OM\x00\x02" and self.kx or body[2:6] == b"\xf0\x00\x00\x00"
            data = tlv(0x41, b"\x01" if known else b"\x02")
        elif (ins, p1, p2) == (0x01, 0x61, 0x00):   # WriteECKey: generate the key pair
            assert body == se050._KX_POLICY + b"\x41\x04OM\x00\x02\x42\x01\x03"
            self.kx = True
            data = b""
        elif (ins, p1, p2) == (0x03, 0x01, 0x0F):   # ECDHGenerateSharedSecret
            assert body[2:6] == b"OM\x00\x02" and body[6:8] == b"\x42\x41" and len(body) == 73
            data = tlv(0x41, SECRET)
        elif (ins, p2) == (0x02, 0x00) and body[2:6] == b"OM\x00\x02":
            data = tlv(0x41, KXPUB)
        elif (ins, p2) == (0x02, 0x00):
            data = tlv(0x41, PUB)
        elif (ins, p1, p2) == (0x03, 0x0C, 0x09):
            assert body[6:9] == b"\x42\x01\x21" and body[9:11] == b"\x43\x20"
            data = tlv(0x41, SIG)
        elif (ins, p2) == (0x04, 0x49):
            data = tlv(0x41, bytes(range(body[3])))
        else:                                   # pragma: no cover - a test asked for nothing
            raise AssertionError(apdu.hex())
        reply = data + self.sw.to_bytes(2, "big")
        frames = []
        if self.chain:
            half = len(reply) // 2
            frames = [frame(0x20, reply[:half]), frame(0x40, reply[half:])]
            self.chain = False
        else:
            frames = [frame(0x00, reply)]
        if self.corrupt:
            f = bytearray(frames[0])
            f[-1] ^= 0xFF
            frames[0] = bytes(f)
            self.corrupt = False
        if self.extra is not None:
            frames = [self.extra]
            self.extra = None
        if self.wtx:
            self.wtx -= 1
            frames.insert(0, frame(0xC3, b"\x02"))
        self.out = frames[0]
        self.pending = frames[1:]


@pytest.fixture
def chip():
    return Chip()


def _open(chip):
    return se050.SecureElement(chip)


# --- the identity -------------------------------------------------------------------------

def test_session_starts_with_a_soft_reset_then_selects_the_applet(chip):
    _open(chip)
    assert chip.rx[0] == (0xCF, b"")
    pcb, apdu = chip.rx[1]
    assert pcb == 0x00 and apdu[:5] == b"\x00\xa4\x04\x00\x10" and apdu[5:21] == se050._AID


def test_public_key_certificate_sign_and_random(chip):
    se = _open(chip)
    assert se.public_key() == PUB
    assert se.certificate() == CERT                 # the zero padding is cut off
    digest = hashlib.sha256(b"x").digest()
    assert se.sign(digest) == SIG
    apdu = chip.rx[-1][1]
    assert apdu[:4] == b"\x80\x03\x0c\x09" and apdu[5:11] == b"\x41\x04\xf0\x00\x00\x00"
    assert apdu[11:16] == b"\x42\x01\x21\x43\x20" and apdu[16:48] == digest
    assert se.random(32) == bytes(range(32))


def test_block_sequence_numbers_alternate(chip):
    se = _open(chip)
    se.random(4)
    se.random(4)
    # SELECT, the exchange key's CheckObjectExists, then the two
    assert [p for p, _ in chip.rx if not p & 0x80] == [0x00, 0x40, 0x00, 0x40]


def test_the_exchange_key_does_ecdh_on_the_chip(chip):
    se = _open(chip)
    peer = b"\x04" + bytes(64)
    assert se.ecdh_public_key() == KXPUB
    assert se.ecdh(peer) == SECRET
    assert chip.rx[-1][1][:4] == b"\x80\x03\x01\x0f" and chip.rx[-1][1][13:78] == peer
    with pytest.raises(ValueError):
        se.ecdh(peer[:64])


def test_open_refuses_a_chip_without_the_exchange_key_and_provision_makes_it(chip):
    chip.kx = False
    with pytest.raises(se050.NotProvisioned):
        _open(chip)
    se, made = se050.SecureElement.provision(chip)
    assert made and chip.kx and se.ecdh_public_key() == KXPUB
    assert se050.SecureElement.provision(chip)[1] is False      # had it: nothing made


def test_sign_and_random_refuse_bad_sizes(chip):
    se = _open(chip)
    with pytest.raises(ValueError):
        se.sign(b"\x00" * 31)
    for n in (0, 201):
        with pytest.raises(ValueError):
            se.random(n)


# --- the link -------------------------------------------------------------------------------

def test_a_busy_chip_is_polled_with_backoff(chip, clock):
    se = _open(chip)
    chip.busy = 6
    assert se.random(2) == b"\x00\x01"
    assert clock.now == 1 + 2 + 4 + 8 + 8 + 8


def test_a_chip_that_never_answers_times_out(chip):
    se = _open(chip)
    chip.busy = 10_000
    with pytest.raises(OSError, match="no answer"):
        se.random(2)


def test_a_read_that_is_not_a_frame_is_polled_again(chip):
    se = _open(chip)
    real = chip.readfrom_into
    state = {"junk": 1}

    def junk_first(addr, buf):
        if state["junk"] and len(buf) == 3:
            state["junk"] = 0
            buf[:] = b"\xff\xff\xff"
            return
        real(addr, buf)
    chip.readfrom_into = junk_first
    assert se.random(2) == b"\x00\x01"


def test_wait_time_extension_is_answered_with_the_multiplier(chip):
    se = _open(chip)
    chip.wtx = 1
    assert se.random(3) == b"\x00\x01\x02"
    assert (0xE3, b"\x02") in chip.rx


def test_a_chained_answer_is_acknowledged_and_joined(chip):
    se = _open(chip)
    chip.chain = True
    assert se.public_key() == PUB
    assert chip.rx[-1][0] == 0x90                   # R(N(R)) after the I-block with N(S)=0


def test_a_bad_crc_or_a_stray_block_is_an_error(chip):
    se = _open(chip)
    chip.corrupt = True
    with pytest.raises(OSError, match="bad CRC"):
        se.random(2)
    se = _open(chip)
    chip.extra = frame(0x82)                         # an R-block error where an answer belongs
    with pytest.raises(OSError, match="unexpected block"):
        se.random(2)


def test_an_answer_longer_than_the_buffer_is_refused(chip):
    se = _open(chip)
    se._ans = bytearray(8)
    with pytest.raises(OSError, match="too long"):
        se.public_key()


def test_a_status_word_other_than_9000_raises(chip):
    se = _open(chip)
    chip.sw = 0x6985
    with pytest.raises(OSError, match="6985"):
        se.random(2)


def test_no_atr_is_an_error(chip):
    real = chip.writeto

    def no_atr(addr, buf):
        real(addr, buf)
        if bytes(buf)[1] == 0xCF:
            chip.out = frame(0xC0)
    chip.writeto = no_atr
    with pytest.raises(OSError, match="no ATR"):
        _open(chip)


def test_a_half_read_reply_is_drained_before_the_session_starts(chip):
    # The bench failure: the host stopped reading mid-reply (a reset mid-exchange). Starting a
    # session over it fails; draining what the chip still holds first is what recovers it.
    se = _open(chip)
    se._send(0x00, 0)
    chip.out = frame(0x00, b"\x69\x85")
    chip.readfrom(0x48, 3)                           # the header, then nothing more
    again = _open(chip)                              # drains the 4 bytes left, then resets
    assert again.public_key() == PUB


def test_the_drain_is_bounded(chip):
    chip.out = b"\x00" * (se050._DRAIN_MAX + 10)    # a chip that never stops sending
    real = chip.writeto

    def reset_clears(addr, buf):
        chip.out = b""
        real(addr, buf)
    chip.writeto = reset_clears
    _open(chip)


def test_an_enable_pin_powers_the_chip_up_first(chip, clock):
    levels = []
    se050.SecureElement(chip, enable=levels.append)
    assert levels == [0, 1] and clock.now >= 20


def test_a_certificate_bigger_than_the_cap_is_refused(chip, monkeypatch):
    se = _open(chip)
    monkeypatch.setattr(se050, "_CERT_MAX", 100)
    with pytest.raises(OSError, match="certificate of"):
        se.certificate()


def test_answers_with_long_tlv_lengths(chip, monkeypatch):
    # the chip may use the 0x82 form even for a short value (AN12413 4.1.3.2)
    se = _open(chip)
    for form in (0x81, 0x82):
        monkeypatch.setattr(sys.modules[__name__], "LONG", form)
        assert se.public_key() == PUB


def test_der_len_short_and_long_forms():
    assert se050._der_len(b"\x30\x05" + b"\x00" * 9) == 7
    assert se050._der_len(CERT_OBJECT) == len(CERT)


def test_crc_matches_the_t1_test_vectors():
    # S(soft reset), S(get ATR), S(end of APDU), S(resync) as they go on the wire
    for raw in ("5acf00377f", "5ac700f7b1", "5ac5004782", "5ac000fffc"):
        b = bytes.fromhex(raw)
        assert se050._crc(b, 3) == b[3] | (b[4] << 8)


# --- the package entry point -----------------------------------------------------------------

def test_open_returns_none_on_a_board_without_a_secure_element(monkeypatch):
    from openmv_ota.build.device.openmv_ota import se
    monkeypatch.delitem(sys.modules, "openmv_ota.build.device.openmv_ota.se.board", raising=False)
    assert se.open() is None


def test_open_builds_the_board_s_chip_from_its_wiring(monkeypatch):
    from openmv_ota.build.device.openmv_ota import se
    made = {}

    class FakeChip:
        def __init__(self, i2c, addr, enable):
            made.update(i2c=i2c, addr=addr, enable=enable)

    machine = types.ModuleType("machine")
    machine.I2C = lambda bus, freq: ("I2C", bus, freq)
    machine.Pin = type("Pin", (), {"OUT": 1, "__init__": lambda s, name, mode: setattr(s, "n", name)})
    monkeypatch.setitem(sys.modules, "machine", machine)
    for enable in ("SE05X_EN", None):
        board = types.ModuleType("openmv_ota.build.device.openmv_ota.se.board")
        board.SecureElement, board.BUS, board.ADDR = FakeChip, 2, 0x48
        board.ENABLE, board.FREQ = enable, 400000
        monkeypatch.setitem(sys.modules, "openmv_ota.build.device.openmv_ota.se.board", board)
        monkeypatch.setattr(se, "board", board, raising=False)
        se.open()
        assert made["i2c"] == ("I2C", 2, 400000) and made["addr"] == 0x48
        assert (made["enable"].n if enable else made["enable"]) == enable


# --- the board table and the pack ------------------------------------------------------------

def test_board_table_secure_element_entries_are_checked():
    from openmv_ota.romfs import boards as boards_mod
    se = boards_mod._secure_element
    assert se("X", None) is None
    assert se("X", {"chip": "se050", "bus": 2, "addr": 0x48}) == {
        "chip": "se050", "bus": 2, "addr": 0x48, "enable": None, "freq": 400000}
    assert se("X", {"chip": "se050", "bus": 1, "addr": 0x48, "enable": "SE05X_EN",
                    "freq": 100000})["enable"] == "SE05X_EN"
    with pytest.raises(ValueError, match="chip 'tpm'"):
        se("X", {"chip": "tpm", "bus": 2, "addr": 0x48})
    for bad in ({"bus": "2", "addr": 0x48}, {"bus": 2, "addr": 0x90}, {"bus": 2, "addr": None}):
        with pytest.raises(ValueError, match="integer bus and a 7-bit addr"):
            se("X", {"chip": "se050", **bad})
    with pytest.raises(ValueError, match="Pin name"):
        se("X", {"chip": "se050", "bus": 2, "addr": 0x48, "enable": 5})
    assert boards_mod.get_board("OPENMV_RT1060").secure_element["chip"] == "se050"
    assert se("X", {"chip": "soft", "key_area": 0x08007000, "bootloader_max": 0x7000}) == {
        "chip": "soft", "key_area": 0x08007000, "bootloader_max": 0x7000}
    for bad in ({"key_area": "0x0800"}, {"key_area": 1, "bootloader_max": 0}, {"key_area": 1}):
        with pytest.raises(ValueError, match="integer key_area and bootloader_max"):
            se("X", {"chip": "soft", **bad})
    # PINNED: a board's key area never moves -- its cameras' keys are there
    for name, area in (("OPENMV2", 0x08007000), ("OPENMV3", 0x08007000),
                       ("OPENMV4", 0x0801F000), ("OPENMV4P", 0x0801F000),
                       ("OPENMVPT", 0x0801F000)):
        k = boards_mod.get_board(name).secure_element
        # the key area is the last 4 KB of the boot partition, right after the room the
        # bootloader may use (the partition starts at 0x08000000)
        assert k["chip"] == "soft" and k["key_area"] == area == 0x08000000 + k["bootloader_max"]


def test_the_pack_ships_only_the_board_s_chip_and_its_wiring(tmp_path):
    from pathlib import Path

    from openmv_ota.build.romfs import _stage_secure_element
    src = Path(se050.__file__).parent

    def stage_for(board):
        lib = tmp_path / board / "lib" / "openmv_ota"
        (lib / "se").mkdir(parents=True)
        for f in src.glob("*.py"):
            (lib / "se" / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
        (lib / "se" / "atecc608.py").write_text("# another chip\n", encoding="utf-8")
        _stage_secure_element(lib, board)
        return lib / "se"

    rt = stage_for("OPENMV_RT1060")
    assert sorted(p.name for p in rt.iterdir()) == ["__init__.py", "board.py", "se050.py"]
    board = (rt / "board.py").read_text(encoding="utf-8")
    assert "from .se050 import SecureElement" in board
    assert "BUS = 2\nADDR = 0x48\nENABLE = None\nFREQ = 400000\n" in board
    compile(board, "board.py", "exec")

    h7 = stage_for("OPENMV4")                            # its own keys: soft, no wiring
    assert sorted(p.name for p in h7.iterdir()) == ["__init__.py", "board.py", "soft.py"]
    board = (h7 / "board.py").read_text(encoding="utf-8")
    assert "from .soft import SecureElement" in board and "BUS = None\n" in board
    compile(board, "board.py", "exec")

    n6 = stage_for("OPENMV_N6")                          # keys sealed in its own NOR: soft
    assert sorted(p.name for p in n6.iterdir()) == ["__init__.py", "board.py", "soft.py"]
    assert not stage_for("OPENMV_AE3").exists()          # no keys at all (yet): no package
    _stage_secure_element(tmp_path / "plain", "OPENMV_RT1060")   # no se/ staged: nothing to do


# --- the ATECC608 ----------------------------------------------------------------------------

from openmv_ota.build.device.openmv_ota.se import atecc608  # noqa: E402


def _acrc(data):
    crc = 0
    for b in data:
        for k in range(8):
            if ((b >> k) & 1) ^ (crc >> 15):
                crc = ((crc << 1) ^ 0x8005) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


def _aresp(data):
    body = bytes([len(data) + 3]) + data
    c = _acrc(body)
    return body + bytes([c & 0xFF, c >> 8])


XY = bytes(range(1, 65))
KX = bytes(range(65, 129))
RS = b"\x80" + bytes(31) + b"\x00\x00\x01" + bytes(29)   # r needs a 0x00 pad, s has zeros to strip


# Arduino's configuration, copied byte for byte from ArduinoECCX08 (cc52117)
# src/utility/ECCX08DefaultTLSConfig.h -- the oracle the driver's own copy is checked against.
ARD = bytes((
    0x01, 0x23, 0x00, 0x00, 0x00, 0x00, 0x50, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0xC0, 0x71, 0x00,
    0xC0, 0x00, 0x55, 0x00, 0x83, 0x20, 0x87, 0x20, 0x87, 0x20, 0x87, 0x2F, 0x87, 0x2F, 0x8F, 0x8F,
    0x9F, 0x8F, 0xAF, 0x8F, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0xAF, 0x8F, 0xFF, 0xFF, 0xFF, 0xFF, 0x00, 0x00, 0x00, 0x00, 0xFF, 0xFF, 0xFF, 0xFF,
    0x00, 0x00, 0x00, 0x00, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF,
    0xFF, 0xFF, 0xFF, 0xFF, 0x00, 0x00, 0x55, 0x55, 0xFF, 0xFF, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x33, 0x00, 0x33, 0x00, 0x33, 0x00, 0x33, 0x00, 0x33, 0x00, 0x1C, 0x00, 0x1C, 0x00, 0x1C, 0x00,
    0x3C, 0x00, 0x3C, 0x00, 0x3C, 0x00, 0x3C, 0x00, 0x3C, 0x00, 0x3C, 0x00, 0x3C, 0x00, 0x1C, 0x00,
))

# The bench Giga's configuration zone as Microchip shipped it (read 2026-10-08, before provisioning).
FACTORY = bytes.fromhex(
    "012363f2000060034097bfd9ee614900"
    "c0000000832087208f20c48f8f8f8f8f"
    "9f8faf8f000000000000000000000000"
    "0000af8fffffffff00000000ffffffff"
    "00000000000000000000000000000000"
    "0000000000005555ffff000000000000"
    "3300330033001c001c001c001c001c00"
    "3c003c003c003c003c003c003c001c00")


class Ecc:
    """An ATECC608 as the bench Giga's answers it: asleep until a wake pulse, NACKing while it
    executes, status-only answers for Write/Lock/Nonce, and real configuration and data zones.
    ``state`` is how the chip arrives: ``"blank"`` (Microchip's factory), ``"arduino"`` (after
    Arduino Cloud onboarding: Arduino's config, both locks, a key in slot 0, no OpenMV record) or
    ``"ours"`` (provisioned by the driver, key XY in slot 2)."""

    def __init__(self, state="ours"):
        self.config = bytearray(FACTORY)
        self.data = {s: bytearray(416 if s == 8 else 72) for s in range(16)}
        self.keys = {}
        if state != "blank":
            self.config[16:84] = ARD[16:84]
            self.config[88:] = ARD[88:]
            self.config[86] = self.config[87] = 0
        if state == "arduino":
            self.keys[0] = bytes(range(64, 128))
        if state == "ours":
            self.keys[2], self.keys[3] = XY, KX
            self.data[8][:32] = atecc608._MAGIC + hashlib.sha256(XY + KX).digest()[:27]
        self.genkeys = 0
        self.ignore_word = None                   # a config word the chip drops on the floor
        self.cut = None                           # the command number the power fails on
        self.awake = False
        self.out = b""
        self.busy = 0
        self.rx = []
        self.status = 0
        self.no_wake = False
        self.corrupt = False

    def writeto(self, addr, buf):
        buf = bytes(buf)
        if addr == 0x00:
            self.awake = True
            self.out = b"\x00" * 4 if self.no_wake else atecc608._WAKE
            raise OSError(19)
        if buf == b"\x02":
            self.awake = False
            if self.status == 0xEE:
                raise OSError(19)
            return
        assert self.awake and buf[0] == 0x03
        n = buf[1]
        assert _acrc(buf[1:n - 1]) == buf[n - 1] | (buf[n] << 8)
        op, p1, p2 = buf[2], buf[3], buf[4] | (buf[5] << 8)
        self.rx.append((op, p1, p2, buf[6:n - 1]))
        if self.cut == len(self.rx):
            raise OSError(5)                      # power gone: the command never ran
        self.out = _aresp(self._do(op, p1, p2, buf[6:n - 1]))
        if self.corrupt:
            self.out = self.out[:-1] + bytes([self.out[-1] ^ 1])

    def _do(self, op, p1, p2, data):
        cfg = self.config
        if op == 0x02:                            # Read
            if p1 == 0x00:
                return bytes(cfg[p2 * 4:p2 * 4 + 4])
            if p1 == 0x80:
                return bytes(cfg[(p2 >> 3) * 32:(p2 >> 3) * 32 + 32])
            assert p1 == 0x82 and not cfg[86]     # the data zone reads only once locked
            return bytes(self.data[p2 >> 3][:32])
        if op == 0x12:                            # Write
            if p1 == 0x82:
                self.data[p2 >> 3][:32] = data
            else:
                assert p1 == 0x00 and cfg[87] and 4 <= p2 < 32 and p2 != 21 and len(data) == 4
                if p2 != self.ignore_word:
                    cfg[p2 * 4:p2 * 4 + 4] = data
            return b"\x00"
        if op == 0x17:                            # Lock
            if p1 == 0x00:
                assert cfg[87] and _acrc(bytes(cfg)) == p2
                cfg[87] = 0
            else:
                assert p1 == 0x81 and not cfg[87] and cfg[86]
                cfg[86] = 0
            return b"\x00"
        if op == 0x40:                            # GenKey
            if p1 == 0x04:
                assert not cfg[86]
                self.genkeys += 1
                self.keys[p2] = bytes([self.genkeys]) * 64
            return self.keys[p2]
        if op == 0x43:                            # ECDH, the secret in the clear
            assert p1 == 0x00 and p2 == 3 and len(data) == 64 and 3 in self.keys
            return SECRET
        if op == 0x16:
            return bytes([self.status if self.status != 0xEE else 0])
        if op == 0x41:
            return RS
        assert op == 0x1B
        return bytes(range(32))

    def readfrom(self, addr, n):
        assert addr == 0x60
        if self.busy and n == 1:                 # executing: the poll for an answer NACKs
            self.busy -= 1
            raise OSError(19)
        r, self.out = self.out[:n], self.out[n:]
        return r


@pytest.fixture
def clock_us(clock, monkeypatch):
    clock.sleep_us = lambda us: None
    monkeypatch.setattr(atecc608, "time", clock)
    return clock


def _arduino_end_state(chip):
    """The chip's configuration is what Arduino's provisioning leaves: its own bytes 0-15, Arduino's
    16-127 (word 21 aside), both zones locked."""
    cfg = bytes(chip.config)
    return (cfg[:16] == FACTORY[:16] and cfg[16:84] == ARD[16:84] and cfg[88:] == ARD[88:]
            and cfg[86] == 0 and cfg[87] == 0)


def _record(chip):
    keys = chip.keys[2] + chip.keys[3]
    return bytes(chip.data[8][:32]) == atecc608._MAGIC + hashlib.sha256(keys).digest()[:27]


def test_atecc_driver_config_is_arduino_s():
    assert atecc608._CONFIG == ARD[16:]


def test_atecc_open_never_provisions(clock_us):
    for state in ("blank", "arduino"):
        chip = Ecc(state)
        with pytest.raises(atecc608.NotProvisioned):
            atecc608.SecureElement(chip)
        assert not [r for r in chip.rx if r[0] in (0x12, 0x17, 0x40) and r[1] != 0x00]


def test_atecc_provisions_a_blank_chip_as_arduino_would(clock_us):
    chip = Ecc("blank")
    se, made = atecc608.SecureElement.provision(chip)
    assert made and _arduino_end_state(chip)
    writes = [p2 for op, p1, p2, _ in chip.rx if op == 0x12 and p1 == 0x00]
    assert writes == [w for w in range(4, 32) if w != 21]
    locks = [(p1, p2) for op, p1, p2, _ in chip.rx if op == 0x17]
    assert locks[0][0] == 0x00 and locks[1] == (0x81, 0)
    assert [p2 for op, p1, p2, _ in chip.rx if op == 0x40] == [2, 3]   # slots 2 and 3 only
    assert _record(chip) and 0 not in chip.keys
    assert se.public_key() == b"\x04" + chip.keys[2]
    assert se.ecdh_public_key() == b"\x04" + chip.keys[3]
    chip.rx.clear()
    atecc608.SecureElement(chip)                          # provisioned: two reads, nothing else
    assert [(op, p1) for op, p1, _, _ in chip.rx] == [(0x02, 0x00), (0x02, 0x82)]
    assert atecc608.SecureElement.provision(chip)[1] is False and chip.genkeys == 2        # had them: nothing made


def test_atecc_keeps_the_arduino_cloud_key(clock_us):
    chip = Ecc("arduino")
    arduino_key = chip.keys[0]
    atecc608.SecureElement.provision(chip)
    assert not [r for r in chip.rx if r[0] in (0x17,) or (r[0] == 0x12 and r[1] == 0x00)]
    assert chip.keys[0] == arduino_key and _record(chip) and _arduino_end_state(chip)


def test_atecc_resumes_after_a_power_cut_at_any_step(clock_us):
    probe = Ecc("blank")
    atecc608.SecureElement.provision(probe)
    total = len(probe.rx)
    for cut in range(1, total + 1):
        chip = Ecc("blank")
        chip.cut = cut
        with pytest.raises(OSError):
            atecc608.SecureElement.provision(chip)
        chip.cut = None
        se, _ = atecc608.SecureElement.provision(chip)
        assert _arduino_end_state(chip) and _record(chip), cut
        assert se.public_key() == b"\x04" + chip.keys[2]


def test_atecc_refuses_a_chip_someone_else_configured(clock_us):
    chip = Ecc("blank")
    chip.config[20] = 0x00                       # slot 0 configured differently, then locked
    chip.config[87] = 0
    with pytest.raises(OSError, match="not Arduino's configuration"):
        atecc608.SecureElement.provision(chip)
    assert chip.config[86] == 0x55 and not chip.keys        # data left unlocked, no key made


def test_atecc_does_not_lock_a_configuration_that_did_not_take(clock_us):
    chip = Ecc("blank")
    chip.ignore_word = 6                         # factory 8F 20 C4 8F, Arduino 87 20 87 2F
    with pytest.raises(OSError, match="not Arduino's configuration"):
        atecc608.SecureElement.provision(chip)
    assert chip.config[87] == 0x55 and not [r for r in chip.rx if r[0] == 0x17]


def test_atecc_identity_operations(clock_us):
    chip = Ecc()
    se = atecc608.SecureElement(chip)
    assert se.public_key() == b"\x04" + XY
    assert se.certificate() is None
    digest = hashlib.sha256(b"x").digest()
    sig = se.sign(digest)
    assert chip.rx[-2] == (0x16, 0x03, 0, digest) and chip.rx[-1][:3] == (0x41, 0x80, 2)
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
    assert decode_dss_signature(sig) == (int.from_bytes(RS[:32], "big"), int.from_bytes(RS[32:], "big"))
    assert se.random(5) == bytes(range(5))


def test_atecc_exchange_key_does_ecdh_on_the_chip(clock_us):
    chip = Ecc()
    se = atecc608.SecureElement(chip)
    peer = b"\x04" + bytes(range(64))
    assert se.ecdh(peer) == SECRET and chip.rx[-1][:4] == (0x43, 0x00, 3, peer[1:])
    for bad in (peer[:64], b"\x02" + peer[1:]):
        with pytest.raises(ValueError):
            se.ecdh(bad)


def test_atecc_bad_sizes_and_chip_errors(clock_us):
    chip = Ecc()
    se = atecc608.SecureElement(chip)
    with pytest.raises(ValueError):
        se.sign(b"\x00" * 31)
    for n in (0, 33):
        with pytest.raises(ValueError):
            se.random(n)
    chip.status = 0x0F
    with pytest.raises(OSError, match="status 0F"):
        se.sign(b"\x00" * 32)
    chip.status = 0xEE                            # idle write fails: ignored
    assert se.random(1) == b"\x00"
    chip.status = 0
    chip.corrupt = True
    with pytest.raises(OSError, match="bad CRC"):
        se.random(1)
    chip.corrupt = False
    chip.no_wake = True
    with pytest.raises(OSError, match="no wake"):
        se.random(1)


def test_atecc_polls_while_executing_then_times_out(clock_us):
    chip = Ecc()
    se = atecc608.SecureElement(chip)
    chip.busy = 3
    assert se.random(2) == b"\x00\x01"
    chip.busy = 10_000
    with pytest.raises(OSError, match="no answer"):
        se.random(2)


def test_atecc_rejects_a_bad_length(clock_us):
    chip = Ecc()
    se = atecc608.SecureElement(chip)
    real = chip.readfrom
    chip.readfrom = lambda addr, n: b"\x02" if n == 1 and chip.out else real(addr, n)
    with pytest.raises(OSError, match="bad length 2"):
        se.random(2)


def test_atecc_crc_matches_the_chip_s_wake_answer():
    assert atecc608._crc(b"\x04\x11", 0, 2) == 0x4333     # what the bench Giga answered


def test_der_sig_pads_and_strips():
    assert atecc608._der_sig(bytes(64)) == b"\x30\x06\x02\x01\x00\x02\x01\x00"


# --- a board's own keys (no secure element) -------------------------------------------------
#
# The device imports the C modules ``key_store`` and ``ecdsa_verify``; here ``key_store`` is a
# 256-byte flash that follows the C module's rules (whole 32-byte words, blank flash only) and
# ``ecdsa_verify`` is the host's own `cryptography` -- so a signature from the soft key is
# checked by real ECDSA and its ECDH by a real exchange.

from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import utils as ec_utils  # noqa: E402

for _name in ("key_store", "ecdsa_verify"):
    sys.modules.setdefault(_name, types.ModuleType(_name))
from openmv_ota.build.device.openmv_ota.se import soft  # noqa: E402

_N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551


def _pub(priv):
    return priv.public_key().public_bytes(serialization.Encoding.X962,
                                          serialization.PublicFormat.UncompressedPoint)


class _Ecdsa:
    """``ecdsa_verify`` on the host's cryptography; ``bad`` keys are refused once each, as
    mbedtls refuses 0 and n."""

    def __init__(self):
        self.calls = []

    @staticmethod
    def _key(d):
        v = int.from_bytes(d, "big")
        if len(d) != 32 or not 0 < v < _N:
            raise ValueError("bad key")
        return ec.derive_private_key(v, ec.SECP256R1())

    def public_key(self, d, entropy):
        self.calls.append(("public_key", len(entropy)))
        return _pub(self._key(d))

    def sign(self, d, digest, entropy):
        self.calls.append(("sign", len(entropy)))
        der = self._key(d).sign(digest, ec.ECDSA(ec_utils.Prehashed(hashes.SHA256())))
        r, s = ec_utils.decode_dss_signature(der)
        return r.to_bytes(32, "big") + s.to_bytes(32, "big")

    def ecdh(self, d, peer, entropy):
        self.calls.append(("ecdh", len(entropy)))
        point = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), peer)
        return self._key(d).exchange(ec.ECDH(), point)


class _KeyArea:
    """``key_store``: 4 KB of flash, written in whole 32-byte words onto blank flash only.
    ``cut`` stops the Nth write after ``cut_bytes`` bytes, or -- with ``cut_bits`` -- leaves a
    random subset of that write's bits programmed: a power cut mid-write."""

    SIZE = 4096
    BLANK = 0xFF

    def __init__(self):
        self.flash = bytearray(b"\xff" * self.SIZE)
        self.writes = []
        self.cut = None                       # (write index, bytes kept) or (index, "bits", seed)

    def read(self, off, n):
        assert 0 <= off and off + n <= self.SIZE
        return bytes(self.flash[off:off + n])

    def write(self, off, data):
        assert off % 32 == 0 and len(data) % 32 == 0 and off + len(data) <= self.SIZE
        if self.flash[off:off + len(data)] != b"\xff" * len(data):
            raise OSError(17)
        self.writes.append((off, bytes(data)))
        cut = self.cut
        if cut is not None and cut[0] == len(self.writes) - 1:
            if cut[1] == "bits":                # some of the 1->0 transitions made, not all
                import random
                rnd = random.Random(cut[2])
                for i, byte in enumerate(data):
                    keep = rnd.getrandbits(8)
                    self.flash[off + i] = byte | (~keep & 0xFF & ~byte)
            else:                               # programmed in order, stopped part way
                self.flash[off:off + cut[1]] = data[:cut[1]]
            raise OSError(5)
        self.flash[off:off + len(data)] = data


@pytest.fixture
def area(monkeypatch):
    a = _KeyArea()
    monkeypatch.setattr(soft, "key_store", a)
    monkeypatch.setattr(soft, "ecdsa_verify", _Ecdsa())
    return a


TOP = 4096 - 256                                 # slot 0: the top of the area


def _soft_record(types, keys, version=1, protection=0):
    """A record as ``provision`` writes it, for any description (to test what reads it)."""
    desc = b"OMVK" + bytes([version, protection, len(types), 0xFF]) + bytes(types)
    words = desc + b"\xff" * (32 - len(desc)) + b"".join(keys)
    words += b"\xff" * (224 - len(words))
    return words + hashlib.sha256(words).digest()


def _key_bytes(n):
    return bytes([n]) * 32


def test_soft_open_never_makes_keys(area):
    with pytest.raises(soft.NotProvisioned):
        soft.SecureElement()
    assert area.writes == []


def test_soft_provision_makes_keys_once_in_the_top_slot(area):
    se, made = soft.SecureElement.provision()
    assert made
    slot = bytes(area.flash[TOP:])
    desc = slot[:32]
    assert desc[:8] == b"OMVK\x01\x00\x02\xff" and desc[8:10] == b"\x01\x02"
    assert desc[10:] == b"\xff" * 22                       # reserved bytes stay blank
    assert slot[96:224] == b"\xff" * 128                   # spare keys and word 6: never written
    assert slot[224:] == hashlib.sha256(slot[:224]).digest()
    assert [o for o, _ in area.writes] == [TOP, TOP + 224]  # keys, then the check last
    assert area.flash[:TOP] == b"\xff" * TOP
    again, made_again = soft.SecureElement.provision()
    assert not made_again and len(area.writes) == 2
    assert again.public_key() == se.public_key() == soft.SecureElement().public_key()
    assert se.ecdh_public_key() != se.public_key()


def test_soft_identity_signs_and_exchange_key_agrees(area):
    se, _ = soft.SecureElement.provision()
    digest = hashlib.sha256(b"challenge").digest()
    sig = se.sign(digest)
    ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), se.public_key()).verify(
        sig, digest, ec.ECDSA(ec_utils.Prehashed(hashes.SHA256())))
    server = ec.generate_private_key(ec.SECP256R1())                 # the other end of ECDH
    cam_kx = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), se.ecdh_public_key())
    assert se.ecdh(_pub(server)) == server.exchange(ec.ECDH(), cam_kx)
    assert se.certificate() is None and len(se.random(7)) == 7
    with pytest.raises(ValueError):
        se.sign(digest[:31])
    assert all(n == soft._ENTROPY for _, n in soft.ecdsa_verify.calls)


def test_soft_draws_again_for_a_key_mbedtls_refuses(area, monkeypatch):
    draws = iter([bytes(32), _N.to_bytes(32, "big")] + [os.urandom(32) for _ in range(4)])
    real = os.urandom
    monkeypatch.setattr(soft.os, "urandom", lambda n: next(draws) if n == 32 else real(n))
    soft.SecureElement.provision()
    assert soft.SecureElement().public_key()


def _cut_cases():
    """Every way the two writes of a provisioning can be stopped: each write, after each byte;
    and each write with random partial bit patterns."""
    cases = [(w, n) for w, size in ((0, 96), (1, 32)) for n in range(size)]
    cases += [(w, "bits", seed) for w in (0, 1) for seed in range(40)]
    return cases


@pytest.mark.parametrize("cut", _cut_cases())
def test_soft_a_power_cut_at_any_point_never_silently_changes_the_keys(area, cut):
    area.cut = cut
    with pytest.raises(OSError):
        soft.SecureElement.provision()
    area.cut = None
    check = bytes(area.flash[TOP + 224:])
    if check == b"\xff" * 32:
        # cut before the check: the keys were never used -- skipped, made again one slot down
        # (or in the same slot, if nothing at all reached the flash)
        untouched = area.flash[TOP:] == b"\xff" * 256
        with pytest.raises(soft.NotProvisioned):
            soft.SecureElement()
        se, made = soft.SecureElement.provision()
        slot = TOP if untouched else TOP - 256
        assert made and area.writes[-1][0] == slot + 224 and se.public_key()
    else:
        # cut inside the check itself: indistinguishable from damage -- loud, never new keys
        writes = len(area.writes)
        for step in (soft.SecureElement, soft.SecureElement.provision):
            with pytest.raises(OSError, match="damaged"):
                step()
        assert len(area.writes) == writes


def test_soft_a_damaged_record_is_an_error_and_never_replaced(area):
    soft.SecureElement.provision()
    area.flash[TOP + 40] ^= 0x01                           # one bit of the identity key
    writes = len(area.writes)
    for step in (soft.SecureElement, soft.SecureElement.provision):
        with pytest.raises(OSError, match="damaged"):
            step()
    assert len(area.writes) == writes


@pytest.mark.parametrize("record", [
    _soft_record([1, 2], [_key_bytes(1), _key_bytes(2)], version=2),           # a newer format
    _soft_record([1, 2], [_key_bytes(1), _key_bytes(2)], protection=1),       # sealed by a chip
    _soft_record([1, 9], [_key_bytes(1), _key_bytes(2)]),                     # an unknown key type
    _soft_record([1, 1], [_key_bytes(1), _key_bytes(2)]),                     # a type twice
    _soft_record([], []),                                                     # no keys at all
    _soft_record([1, 2, 1, 2, 1, 2], [_key_bytes(1)] * 6),                    # more than five
    b"XXXX" + _soft_record([1, 2], [_key_bytes(1), _key_bytes(2)])[4:],       # not our magic
])
def test_soft_a_complete_record_it_does_not_understand_is_an_error(area, record):
    if record[:4] == b"XXXX":                              # recompute the check over it
        record = record[:224] + hashlib.sha256(record[:224]).digest()
    area.flash[TOP:] = record
    for step in (soft.SecureElement, soft.SecureElement.provision):
        with pytest.raises(OSError, match="newer firmware"):
            step()
    assert area.writes == []


def test_soft_the_newest_complete_record_wins(area):
    area.flash[TOP:] = _soft_record([1, 2], [_key_bytes(1), _key_bytes(2)])
    area.flash[TOP - 256:TOP] = _soft_record([1, 2], [_key_bytes(3), _key_bytes(4)])
    se = soft.SecureElement()
    assert se._id == _key_bytes(3) and se._kx == _key_bytes(4)
    area.flash[TOP + 40] ^= 1                              # damage under a newer record: history
    assert soft.SecureElement()._id == _key_bytes(3)
    area.flash[TOP - 256 + 40] ^= 1                        # the newest damaged: an error
    with pytest.raises(OSError, match="damaged"):
        soft.SecureElement()


def test_soft_keys_are_found_by_type_and_one_job_each(area):
    area.flash[TOP:] = _soft_record([2, 1], [_key_bytes(7), _key_bytes(8)])     # stored the other way
    se = soft.SecureElement()
    assert se._id == _key_bytes(8) and se._kx == _key_bytes(7)
    area.flash[TOP - 256:TOP] = _soft_record([1], [_key_bytes(9)])              # identity only
    se = soft.SecureElement()
    assert se.sign(bytes(32))
    for job in (se.ecdh_public_key, lambda: se.ecdh(b"\x04" + bytes(64))):
        with pytest.raises(OSError, match="no key for that job"):
            job()


def test_soft_a_full_key_area_refuses_new_keys(area):
    for i in range(16):                                    # every slot a cut-off write
        area.flash[4096 - (i + 1) * 256] = 0x00
    with pytest.raises(soft.NotProvisioned):
        soft.SecureElement()
    with pytest.raises(OSError, match="no blank slot"):
        soft.SecureElement.provision()


class _SealedArea(_KeyArea):
    """The N6's key store: the same flash, plus seal/unseal -- real AES-256-GCM (the host's
    cryptography) under a fixed key standing in for the chip's DHUK."""

    DHUK = bytes(range(32))

    def __init__(self, hvalid=True):
        super().__init__()
        self.hvalid = hvalid

    def seal(self, aad, iv, plain):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        assert len(aad) == 32 and len(iv) == 12
        return AESGCM(self.DHUK).encrypt(iv, plain, aad)

    def unseal(self, aad, iv, ct, tag):
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        if not self.hvalid:
            raise OSError(13)
        try:
            return AESGCM(self.DHUK).decrypt(iv, bytes(ct) + bytes(tag), aad)
        except InvalidTag:
            raise OSError(13) from None


@pytest.fixture
def sealed(monkeypatch):
    a = _SealedArea()
    monkeypatch.setattr(soft, "key_store", a)
    monkeypatch.setattr(soft, "ecdsa_verify", _Ecdsa())
    return a


def test_soft_sealed_keys_never_reach_the_flash_in_the_clear(sealed):
    se, made = soft.SecureElement.provision()
    slot = bytes(sealed.flash[TOP:])
    assert made and slot[5] == 1                           # protection: sealed by the chip
    word6 = slot[192:224]
    assert word6[12:16] == b"\xff" * 4                     # IV, reserved, tag
    keys = sealed.unseal(slot[:32], word6[:12], slot[32:96], word6[16:])
    assert se._id == keys[:32] and se._kx == keys[32:]
    assert keys[:32] not in slot and keys[32:] not in slot
    assert [o for o, _ in sealed.writes] == [TOP, TOP + 192, TOP + 224]   # check last
    assert soft.SecureElement().public_key() == se.public_key()


def test_soft_sealed_keys_another_chip_cannot_open(sealed):
    soft.SecureElement.provision()
    sealed.DHUK = bytes(32)                               # the same NOR on another chip
    with pytest.raises(OSError, match="can't unseal"):
        soft.SecureElement()


def test_soft_sealed_keys_refuse_a_chip_without_its_hardware_key(sealed):
    soft.SecureElement.provision()
    sealed.hvalid = False
    for step in (soft.SecureElement, soft.SecureElement.provision):
        with pytest.raises(OSError, match="can't unseal"):
            step()


def test_soft_a_sealed_record_on_a_board_that_cannot_unseal_is_not_understood(sealed, area):
    sealed_record = _SealedArea()
    soft.key_store = sealed_record
    soft.SecureElement.provision()
    area.flash[:] = sealed_record.flash                    # moved to a plain key store
    soft.key_store = area
    with pytest.raises(OSError, match="newer firmware"):
        soft.SecureElement()


@pytest.mark.parametrize("cut", [(w, n) for w in (0, 1, 2) for n in (0, 16, 31)])
def test_soft_a_sealed_record_cut_short_is_skipped_or_loud(sealed, cut):
    sealed.cut = cut
    with pytest.raises(OSError):
        soft.SecureElement.provision()
    sealed.cut = None
    if bytes(sealed.flash[TOP + 224:]) == b"\xff" * 32:
        se, made = soft.SecureElement.provision()
        assert made and se.public_key()
    else:
        with pytest.raises(OSError, match="damaged"):
            soft.SecureElement()


def test_soft_der_sig_pads_and_strips():
    assert soft._der_sig(bytes(64)) == b"\x30\x06\x02\x01\x00\x02\x01\x00"


def test_open_and_provision_take_a_board_s_own_keys_with_no_bus(monkeypatch):
    from openmv_ota.build.device.openmv_ota import se as se_pkg
    made = object()

    class Keys:
        def __init__(self):
            self.opened = made

        @classmethod
        def provision(cls):
            return made, True

    board = types.ModuleType("board")
    board.BUS = None
    board.SecureElement = Keys
    monkeypatch.setitem(sys.modules, "openmv_ota.build.device.openmv_ota.se.board", board)
    monkeypatch.setattr(se_pkg, "board", board, raising=False)
    assert se_pkg.open().opened is made
    assert se_pkg.provision() == (made, True)
    monkeypatch.delitem(sys.modules, "openmv_ota.build.device.openmv_ota.se.board")
    monkeypatch.delattr(se_pkg, "board")
    assert se_pkg.provision() is None                     # a board with no keys at all
