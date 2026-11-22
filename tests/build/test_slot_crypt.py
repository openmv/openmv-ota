"""The ROMFS slot cipher, host side: what the manifest names, what the firmware build stamps
and drops, and how the installer ciphers a slot on its way to flash."""

from __future__ import annotations

import binascii
import importlib.util
import os
import types
from pathlib import Path

import pytest

from openmv_ota.build import firmware as fw
from openmv_ota.build import romfs
from openmv_ota.ota import slot_crypt
from openmv_ota.ota import trailer as T
from tests import trust

_INSTALLER = Path(fw.__file__).resolve().parent / "device" / "openmv_ota" / "data" / "installer.py"
_spec = importlib.util.spec_from_file_location("installer_for_slot_crypt_tests", _INSTALLER)
INST = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(INST)


def _trailer(body=b"APP." + bytes(100), components=()):
    return T.parse_trailer(trust.trailer(trust.ROOT_PRIV, body, components=components))


# --- the host helpers -------------------------------------------------------------------

def test_crypt_size_covers_the_body_and_every_component_to_the_grain():
    t = _trailer(body=bytes(5000))
    assert slot_crypt.crypt_size(t) == 8192                       # 5000 -> the next 4 KiB
    comp = T.Component(dest=0x70090000, offset=5008, size=10000, installed_size=1,
                       sha256=bytes(32), installed_sha256=bytes(32))
    t = _trailer(body=bytes(5000), components=[comp])
    assert slot_crypt.crypt_size(t) == 16384                      # 15008 -> 16 KiB
    assert slot_crypt.crypt_size(t, slot_body_end=12288) == 12288  # never past the status sector
    t = _trailer(body=bytes(4096))
    assert slot_crypt.crypt_size(t) == 4096                       # already on the grain


def test_the_trailer_hash_is_over_the_signed_region():
    t = _trailer()
    import hashlib
    assert slot_crypt.trailer_sha256(t) == hashlib.sha256(T.signed_region(t)).digest()


def test_the_manifest_names_the_ciphers_inputs(make_project):
    root, _repo, _app = make_project(ota=True)
    from openmv_ota.project import load_project
    p = load_project(root)
    t = _trailer(body=bytes(6000))
    body = romfs._manifest_body(p, t, b"image", [])
    assert body["trailer_sha256"] == slot_crypt.trailer_sha256(t).hex()
    assert body["crypt_size"] == 8192


# --- the firmware build -----------------------------------------------------------------

def test_slot_crypt_is_stamped_for_an_encrypting_board_only():
    assert fw._slot_crypt("OPENMV_N6") is True
    assert fw._slot_crypt("OPENMV_RT1060") is True
    assert fw._slot_crypt("OPENMV_AE3") is True                    # its slots, not its firmware
    assert fw._slot_crypt("OPENMV4") is False                      # a stub, in the clear
    assert fw._slot_crypt("ARDUINO_NANO_RP2040_CONNECT") is False  # no stub at all


def test_the_partitions_flash_address_is_stamped_where_the_installer_cannot_ask_for_it():
    """The keystream runs on flash addresses. A memory-mapped partition has one the installer
    can take; the RT1062's is a block device, so the build bakes in where it sits."""
    assert fw._slot_crypt_base("OPENMV_RT1060") == 0x60800000
    assert fw._slot_crypt_base("OPENMV_N6") is None                # mapped: the installer asks the map
    assert fw._slot_crypt_base("OPENMV_AE3") is None               # mapped too
    assert fw._slot_crypt_base("OPENMV4") is None                  # not encrypted


def test_the_crypt_module_carries_the_boards_own_engine_stream(tmp_path):
    repo = tmp_path / "fw"
    repo.mkdir()
    text = fw._install_crypt_module(repo, "OPENMV_RT1060").read_text()
    assert "void bee_stream_xor(" in text and "void mce_stream_xor(" not in text
    assert '#include "bee_stream.h"' not in text and "MP_REGISTER_MODULE(MP_QSTR_flash_crypt" in text
    assert text.index("bee_stream_xor(const bee_stream_t") < text.index("omv_flash_crypt_xor(")
    assert fw._stub_port("OPENMV_N6") == "stm32" and fw._stub_port("OPENMV_RT1060") == "mimxrt"
    assert "OMV_KEY_AREA_ADDR" not in text.split("\n")[1]          # only the AE3's module reads the key area


def test_a_stub_port_with_no_engine_has_no_crypt_module(monkeypatch):
    monkeypatch.setattr(fw, "_CRYPT_STREAMS", {"stm32": fw._CRYPT_STREAMS["stm32"]})
    with pytest.raises(fw.BuildError, match="no slot cipher"):
        fw._stub_port("OPENMV_AE3")


def test_the_ae3s_crypt_module_is_the_block_cipher_and_knows_the_key_area(tmp_path, monkeypatch):
    """The AE3's engine is a block cipher keyed by the firmware: its module is the stub's
    ecb_slot sources, the address of the key area the camera's flash key lives in, and the
    replacement for the one function /flash reads through."""
    repo = tmp_path / "fw"
    repo.mkdir()
    text = fw._install_crypt_module(repo, "OPENMV_AE3").read_text()
    assert fw._stub_port("OPENMV_AE3") == "alif"
    assert text.split("\n")[1] == "#define OMV_KEY_AREA_ADDR (0x8001F000UL)"
    assert "void ecb_slot_encrypt(" in text and "void aes128_decrypt(" in text
    assert "void mce_stream_xor(" not in text and '#include "ecb_slot.h"' not in text
    assert "int __wrap_ospi_flash_read(" in text and "MP_REGISTER_MODULE(MP_QSTR_flash_crypt" in text
    assert text.index("void ecb_slot_encrypt(") < text.index("__wrap_ospi_flash_read(")
    # the key area is board data: a table without one cannot build the module
    monkeypatch.setattr(fw, "_soft_key_store", lambda name: None)
    with pytest.raises(fw.BuildError, match="gives it none"):
        fw._flash_crypt_sources("OPENMV_AE3")


def test_install_crypt_module_is_for_encrypting_boards(tmp_path):
    repo = tmp_path / "fw"
    repo.mkdir()
    assert fw._install_crypt_module(repo, "OPENMV4") is None
    dst = fw._install_crypt_module(repo, "OPENMV_N6")
    assert dst == repo / "modules" / "flash_crypt.c"
    text = dst.read_text()
    # the stub's cipher sources, then the module, as one translation unit
    assert "void aes128_init(" in text and "void mce_stream_xor(" in text
    assert "MP_REGISTER_MODULE(MP_QSTR_flash_crypt" in text
    assert '#include "aes128.h"' not in text and '#include "mce_stream.h"' not in text
    assert text.index("mce_stream_xor(const mce_stream_t") < text.index("omv_flash_crypt_xor(")
    assert fw._install_crypt_module(repo, "OPENMV_N6") is None     # not clobbered


# --- the installer ----------------------------------------------------------------------

def _cfg(**kw):
    return types.SimpleNamespace(ROOT_KEYS=trust.ROOTS, PRODUCT_ID=7, PLATFORM_VERSION=0,
                                 last_epoch=0, **kw)


def _manifest(body_extra=None):
    from tests.build.test_installer import _host_manifest, _sd_body
    body = _sd_body()
    body.update(body_extra or {})
    return _host_manifest(body=body)


def test_vet_manifest_on_an_encrypting_board_needs_the_ciphers_inputs():
    vet = INST._vet_manifest
    with pytest.raises(OSError, match="slot cipher"):
        vet("/sd/m.bin", _manifest(), _cfg(SLOT_CRYPT=True), lambda *a: True, 0, 0, True)
    with pytest.raises(OSError, match="slot cipher"):
        vet("/sd/m.bin", _manifest({"trailer_sha256": "ab" * 32}), _cfg(SLOT_CRYPT=True),
            lambda *a: True, 0, 0, True)
    with pytest.raises(OSError, match="slot cipher"):
        vet("/sd/m.bin", _manifest({"trailer_sha256": "ab" * 31, "crypt_size": 4096}),
            _cfg(SLOT_CRYPT=True), lambda *a: True, 0, 0, True)
    out = vet("/sd/m.bin", _manifest({"trailer_sha256": "ab" * 32, "crypt_size": 8192}),
              _cfg(SLOT_CRYPT=True), lambda *a: True, 0, 0, True)
    assert out[6] == (bytes.fromhex("ab" * 32), 8192)
    # a board in the clear ignores them, present or not
    assert vet("/sd/m.bin", _manifest(), _cfg(), lambda *a: True, 0, 0, True)[6] is None


class _Flash:
    def __init__(self, size):
        self.mem = bytearray(b"\xff" * size)
        self.writes = []

    def write(self, off, data):
        self.writes.append((off, bytes(data)))
        self.mem[off:off + len(data)] = data

    def read(self, off, n):
        return bytes(self.mem[off:off + n])


class _Source:
    def __init__(self, data):
        self.data, self.pos = data, 0

    def readinto(self, buf):
        n = min(len(buf), len(self.data) - self.pos)
        buf[:n] = self.data[self.pos:self.pos + n]
        self.pos += n
        return n


def test_install_stream_ciphers_the_body_and_leaves_the_pad_blank():
    block = 4096
    slot = 16 * block
    key, tweak = os.urandom(16), os.urandom(10)
    # an image whose body has a blank chunk INSIDE the ciphered range and a blank pad after it
    body = os.urandom(block) + b"\xff" * block + os.urandom(block // 2)
    crypt_size = 3 * block
    image = body.ljust(slot - 2 * block, b"\xff")
    import hashlib
    image += b"\xff" * block                    # status sector: blank, the camera's to write
    image += b"\xff" * block                    # trailer block (pad; not what this test is about)
    flash = _Flash(slot)
    base = 0x71400000

    def crypt(off, buf):
        buf[:] = slot_crypt.keystream_xor(key, tweak, base + off, bytes(buf))

    INST._install_stream(_Source(image), flash.write, flash.read, slot, block, lambda: None,
                         None, hashlib.sha256(image).hexdigest(), INST.REPR_FULL, None, None,
                         counter=1, floor=0, crypt=crypt, crypt_size=crypt_size)
    stored = flash.read(0, crypt_size)
    # every byte of the ciphered range is stored ciphered, the blank chunk included
    assert slot_crypt.keystream_xor(key, tweak, base, stored) == image[:crypt_size]
    assert stored[block:2 * block] != b"\xff" * block
    # the pad after it is untouched flash
    assert flash.read(crypt_size, slot - 2 * block - crypt_size) == b"\xff" * (slot - 2 * block - crypt_size)
    assert all(off >= crypt_size or off + len(d) <= crypt_size for off, d in flash.writes)


def test_install_stream_without_a_cipher_still_skips_blank_chunks():
    block = 4096
    slot = 8 * block
    body = os.urandom(block) + b"\xff" * block + os.urandom(block)
    image = body.ljust(slot - 2 * block, b"\xff") + b"\xff" * (2 * block)
    flash = _Flash(slot)
    INST._install_stream(_Source(image), flash.write, flash.read, slot, block, lambda: None,
                         None, None, INST.REPR_FULL, None, None, counter=1, floor=0)
    assert flash.read(0, 3 * block) == body
    assert not any(off == block for off, _d in flash.writes)      # the blank chunk was skipped


def test_the_host_twin_of_the_tweak_is_what_the_manifest_leads_to():
    t = _trailer()
    h = slot_crypt.trailer_sha256(t)
    assert INST._slot_tweak(1, h) == slot_crypt.slot_tweak(1, binascii.unhexlify(h.hex()))


def test_the_installers_cipher_reach_is_the_hosts():
    """A delta's base is ciphered only as far as its image reaches; past that the AE3 reads
    the flash as stored. The installer works the reach out from the base's own trailer."""
    raw = b""
    for comps, body in (([], 5000), ([(8192, 6000)], 5000), ([(4096, 100), (20000, 9000)], 4096)):
        components = [T.Component(dest=0x80028000, offset=off, size=size, installed_size=size,
                                  sha256=bytes(32), installed_sha256=bytes(32)) for off, size in comps]
        raw = trust.trailer(trust.ROOT_PRIV, bytes(body), components=components)
        assert INST._trailer_crypt_size(raw, 1 << 20) == slot_crypt.crypt_size(T.parse_trailer(raw))
        assert INST._trailer_crypt_size(raw, 4096) == 4096           # never past the status sector
    assert INST._trailer_crypt_size(b"\xff" * 4096, 1 << 20) == 0   # blank
    assert INST._trailer_crypt_size(raw[:40], 1 << 20) == 0         # cut short
    assert INST._trailer_crypt_size(raw[:100], 1 << 20) == 0        # its components cut off
