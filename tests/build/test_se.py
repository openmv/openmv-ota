"""Host tests for the device-side secure element: ``openmv_ota.se`` and its ``se050`` driver.

The driver is pure protocol over ``machine.I2C``, so it runs here against an emulated SE050: real
T=1-over-I2C frames with their CRCs, a chip that NACKs while it works, asks for more time, chains
a long answer, and -- the bench's own failure -- holds on to a reply the host stopped reading.
The certificate and key bytes are the ones read off the bench RT1062's chip.
"""

from __future__ import annotations

import hashlib
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
    overrides the status word and ``corrupt`` breaks the next answer's CRC."""

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
    assert [p for p, _ in chip.rx if not p & 0x80] == [0x00, 0x40, 0x00]


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

    assert not stage_for("OPENMV_N6").exists()          # no secure element: no package
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
RS = b"\x80" + bytes(31) + b"\x00\x00\x01" + bytes(29)   # r needs a 0x00 pad, s has zeros to strip


class Ecc:
    """An ATECC608 as the bench Giga's answers it: asleep until a wake pulse, NACKing while it
    executes, a status-only answer for Nonce, and ``locked`` deciding the lock bytes."""

    def __init__(self, locked=True):
        self.locked = locked
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
        if op == 0x02:
            data = b"\x00\x00\x00\x00" if self.locked else b"\x00\x00\x55\x55"
        elif op == 0x40:
            data = XY
        elif op == 0x16:
            data = bytes([self.status if self.status != 0xEE else 0])
        elif op == 0x41:
            data = RS
        elif op == 0x1B:
            data = bytes(range(32))
        else:                                    # pragma: no cover - a test asked for nothing
            raise AssertionError(op)
        self.out = _aresp(data)
        if self.corrupt:
            self.out = self.out[:-1] + bytes([self.out[-1] ^ 1])

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


def test_atecc_refuses_an_unprovisioned_chip(clock_us):
    with pytest.raises(OSError, match="not provisioned"):
        atecc608.SecureElement(Ecc(locked=False))


def test_atecc_identity_operations(clock_us):
    chip = Ecc()
    se = atecc608.SecureElement(chip)
    assert se.public_key() == b"\x04" + XY
    assert se.certificate() is None
    digest = hashlib.sha256(b"x").digest()
    sig = se.sign(digest)
    assert chip.rx[-2] == (0x16, 0x03, 0, digest) and chip.rx[-1][:3] == (0x41, 0x80, 0)
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
    assert decode_dss_signature(sig) == (int.from_bytes(RS[:32], "big"), int.from_bytes(RS[32:], "big"))
    assert se.random(5) == bytes(range(5))


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
