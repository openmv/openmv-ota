"""The control stride: 16 everywhere, 32 where flash programs one-shot ECC words.

The H7-classic bench run streamed a perfect image and then EIO'd at the ARM step:
its internal flash writes 32-byte ECC words exactly once, and the default layout
packs two 16-byte markers (or four 8-byte rollback entries) into one word. These
tests pin the stride-32 layout across the host builders and all three device
mirrors, so the geometry can never drift apart again.
"""

import importlib.util
from pathlib import Path

from openmv_ota.ota import rollback, status

_ROOT = Path(__file__).resolve().parents[2]


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, str(_ROOT / rel))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_host_offsets_scale_with_stride():
    assert (status.pending_offset(32), status.tried_offset(32),
            status.confirmed_offset(32), status.repr_offset(32),
            status.counter_offset(32), status.attempts_offset(32)) == (0, 32, 64, 96, 128, 160)
    # default stride reproduces the fielded layout exactly
    assert (status.PENDING_OFFSET, status.TRIED_OFFSET, status.CONFIRMED_OFFSET,
            status.REPR_OFFSET, status.COUNTER_OFFSET, status.ATTEMPTS_OFFSET) == (
        0, 16, 32, 48, 64, 80)


def test_sector_builder_at_stride_32():
    sec = status.build_status_sector(4096, pending=True, tried=False, confirmed=True,
                                     counter=7, stride=32)
    assert sec[0:16] == status.PENDING
    assert sec[16:32] == b"\xff" * 16              # nothing shares PENDING's word
    assert sec[64:80] == status.CONFIRMED
    assert sec[128:136] == status.encode_counter(7)


def test_rollback_walks_at_stride_32():
    sec = bytearray(b"\xff" * 4096)
    sec[0:8] = rollback.encode_entry(5)
    sec[32:40] = rollback.encode_entry(9)
    assert rollback.floor_of(sec, stride=32) == 9
    assert rollback.append_offset(sec, stride=32) == 64


def test_boot_mirror_stride_32():
    B = _load("_stride_boot", "src/openmv_ota/build/device/boot.py")
    try:
        B._set_stride(32)
        sec = status.build_status_sector(4096, pending=True, tried=False, confirmed=True,
                                         counter=3, stride=32)
        assert B._markers(sec) == (True, False, True)
        assert B.install_counter(sec) == 3
        assert B.attempt_offset(sec) == 160        # first attempt unit, stride-spaced
        rb = bytearray(b"\xff" * 4096)
        rb[32:40] = rollback.encode_entry(4)
        assert B._rollback_floor_of(rb) == 4
    finally:
        B._set_stride(16)


def test_runtime_mirror_stride_32():
    import types
    R = _load("_stride_runtime", "src/openmv_ota/build/device/openmv_ota/__init__.py")
    try:
        R._use_cfg_stride(types.SimpleNamespace(CONTROL_STRIDE=32))
        sec = status.build_status_sector(4096, pending=False, tried=False, confirmed=True,
                                         counter=11, stride=32)
        assert R._markers(sec) == (False, False, True)
        assert R._install_counter(sec) == 11
        rb = bytearray(b"\xff" * 256)
        rb[0:8] = rollback.encode_entry(2)
        assert R._rollback_floor_of(rb) == 2
        assert R._rollback_append_offset(rb) == 32
        # a cfg with no stride falls back to the default layout
        R._use_cfg_stride(types.SimpleNamespace())
        assert R._markers(status.build_status_sector(
            4096, pending=False, tried=False, confirmed=True)) == (False, False, True)
    finally:
        R._set_stride(16)


def test_installer_mirror_stride_32():
    inst = _load("_stride_installer", "src/openmv_ota/build/device/openmv_ota/data/installer.py")
    try:
        inst._set_stride(32)
        assert inst._REPR_OFF == 96 and inst._COUNTER_OFF == 128
        assert inst._pad(b"x" * 16) == b"x" * 16 + b"\xff" * 16   # marker fills its word
        assert inst._pad(b"y" * 32) == b"y" * 32                  # already whole words
        rb = bytearray(b"\xff" * 128)
        rb[32:40] = inst._rollback_entry(6) if hasattr(inst, "_rollback_entry") else __import__("struct").pack("<II", 6, 6 ^ 0xFFFFFFFF)
        assert inst._rollback_floor_of(rb) == 6
    finally:
        inst._set_stride(16)


def test_factory_image_for_a_stride_32_board(make_project):
    """OPENMV4 (control_stride 32 in boards.json): the factory status sector lays
    its records one ECC word apart."""
    from openmv_ota.build import romfs as build_mod
    from openmv_ota.build.romfs import build_factory_romfs

    root, repo, app = make_project(boards=("OPENMV4",), ota=True, dev=True, ca="tiny",
                                   app_files={"main.py": "print(1)\n",
                                              "settings.json": '{"app_version": "1.0.0"}\n'})
    build_mod.build_romfs(root, app=app, firmware=repo, compile_py=False,
                          convert_models=False, allow_dev_key=True)
    [fres] = build_factory_romfs(root, app=app, firmware=repo, compile_py=False,
                                 convert_models=False, allow_dev_key=True, no_account=True)
    img = fres.output.read_bytes()
    sec = img[131072 - 2 * 4096:131072 - 4096]     # the status sector
    assert sec[64:80] == status.CONFIRMED           # confirmed at 2*32, not 32
    assert sec[32:48] == b"\xff" * 16               # TRIED's word untouched
    assert sec[128:136] == status.encode_counter(1) # counter at 4*32
