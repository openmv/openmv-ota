"""``build stub`` -- compile the boot stub for each board that has one.

The stub is C (``build/device/stub/``): its own Makefile and linker script per board,
built against the vendor HAL and CMSIS in the project's firmware checkout. This module
writes the header that bakes the project's trust into it -- the root public keys, the
board's product id, the attempt budget -- runs make, and copies ``<board>-stub.bin``
into the build directory, sealed with its footer: the stub's sector as flashed, the
code followed by 0xFF and a signed 512-byte footer (``openmv_ota.ota.stub_footer``)
that names the code's hash, the stub version and the product id under the project's
factory key -- what a bootloader verifies under the root. The stub is flashed over USB
alongside the firmware; it never travels in an OTA image and never updates itself.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from openmv_ota.project.config import derive_product_id
from openmv_ota.project.project import ProjectError, ProjectPaths, load_project
from openmv_ota.romfs.boards import firmware_image_name, get_board

from .errors import BuildError
from .firmware import MAKE, _select_boards

STUB_DIR = Path(__file__).resolve().parent / "device" / "stub"
KEYS_HEADER = "stub_keys.h"
COMBINED_SUFFIX = "firmware-with-stub.bin"   # <board>-firmware-with-stub.bin: what DFU writes


@dataclass
class StubResult:
    board: str
    output: Path
    size: int          # bytes of stub code
    sector: int = 0    # bytes written: the sealed sector (code, pad, footer)
    key_id: int = 0    # the factory key that signed the footer


def build_stub(
    project: str | Path,
    *,
    output: str | Path | None = None,
    boards: list[str] | None = None,
    firmware: str | Path | None = None,
    jobs: int | None = None,
    factory_key: int | None = None,
    key_passphrase_file: str | Path | None = None,
    allow_dev_key: bool = False,
) -> list[StubResult]:
    project = Path(project)
    try:
        p = load_project(project, firmware=firmware)
    except ProjectError as e:
        raise BuildError(str(e), exit_code=e.exit_code) from None
    if not p.config.ota:
        raise BuildError("not an OTA project: the boot stub carries the project's root keys")

    names = _select_boards(p.targets, boards)
    if not names:
        raise BuildError("no matching boards in this project")
    for name in names:
        if get_board(name).stub is None:
            raise BuildError("%s has no boot-stub layout (boards.json 'stub')" % name)

    from openmv_ota.ota.errors import OtaError
    from openmv_ota.ota.keys import read_root_keys
    try:
        roots = read_root_keys(ProjectPaths(p.root).root_keys)
    except OtaError as e:
        raise BuildError(str(e)) from None

    signer = _footer_signer(p, factory_key, key_passphrase_file, allow_dev_key)
    out_dir = Path(output) if output else project / "build"
    out_dir.mkdir(parents=True, exist_ok=True)
    return [_build_one(p, name, roots, out_dir, jobs, signer) for name in names]


def build_for_firmware(p, name: str, out_dir: Path, jobs, *, key_passphrase_file=None,
                       allow_dev_key: bool = False) -> list[Path]:
    """The stub a ``build firmware`` of a stub board also produces (the firmware is linked for
    it, so one is not flashable without the other): ``[<out_dir>/<board>-stub.bin]``, or ``[]``
    on a board with no stub layout or in a non-OTA project (no root keys to bake in)."""
    from openmv_ota.ota.keys import read_root_keys
    if not p.config.ota or get_board(name).stub is None:
        return []
    roots = read_root_keys(ProjectPaths(p.root).root_keys)
    signer = _footer_signer(p, None, key_passphrase_file, allow_dev_key)
    return [_build_one(p, name, roots, out_dir, jobs, signer).output]


def _footer_signer(p, factory_key, key_passphrase_file, allow_dev_key):
    """The factory key that seals the stub's footer -- the key that signs the factory image
    the stub is flashed with (``FACTORY_KEY_ID_BASE`` unless ``factory_key`` names another),
    resolved the way every signing build resolves its key."""
    from openmv_ota.ota.keys import FACTORY_KEY_ID_BASE

    from .romfs import _key_backend
    key_id = factory_key if factory_key is not None else FACTORY_KEY_ID_BASE
    _entry, _alg, backend = _key_backend(p, key_id, require_role="factory",
                                         key_passphrase_file=key_passphrase_file,
                                         allow_dev_key=allow_dev_key)
    return backend


def stub_version() -> int:
    """``STUB_VERSION`` from the stub sources (``common/stub.h``): the value the stub writes
    into its install record, and the one its footer names."""
    text = (STUB_DIR / "common" / "stub.h").read_text(encoding="utf-8")
    m = re.search(r"(?m)^#define\s+STUB_VERSION\s+\(?(\d+)\)?", text)
    if m is None:
        raise BuildError("common/stub.h does not define STUB_VERSION")
    return int(m.group(1))


def compose_image(stub: bytes, firmware: bytes, layout) -> bytes:
    """``stub || 0xFF pad || firmware`` as one image for the FIRMWARE partition: the stub at
    the partition start (where the bootloader jumps), the firmware at its destination. A
    sealed stub is already its whole sector, so the pad is for a bare one."""
    if len(stub) > layout.stub_max:
        raise BuildError("stub is %d bytes but its sector holds %d" % (len(stub), layout.stub_max))
    return stub + b"\xff" * (layout.stub_max - len(stub)) + firmware


def combined_image(board: str, out_dir: Path) -> Path | None:
    """For a stub board: write ``<board>-firmware-with-stub.bin`` from the build's stub and
    firmware and return its path -- what ``flash firmware``/``flash factory`` download to the
    FIRMWARE partition. None for a board without a stub layout. A missing stub or firmware is
    an error naming the file (``build firmware`` makes both)."""
    layout = get_board(board).stub
    if layout is None:
        return None
    stub, fw = out_dir / ("%s-stub.bin" % board), out_dir / firmware_image_name(board)
    for f in (stub, fw):
        if not f.is_file():
            raise BuildError("missing %s -- `build firmware` produces it" % f)
    out = out_dir / ("%s-%s" % (board, COMBINED_SUFFIX))
    out.write_bytes(compose_image(stub.read_bytes(), fw.read_bytes(), layout))
    return out


def stub_keys_header(roots, product_id: int, max_attempts: int) -> str:
    """The C header the stub builds in: root public keys indexed by ``root_id``, the
    product id (the cross-flash guard) and the attempt budget -- the same values the
    firmware's ``_ota_config`` stamps for ``boot.py``."""
    by_id = {r.root_id: r for r in roots}
    if sorted(by_id) != list(range(len(roots))) or not roots:
        raise BuildError("root keys must be numbered 0..n-1 (got %s)"
                         % sorted(r.root_id for r in roots))
    lines = [
        "// Generated by openmv-ota build stub -- the project's trust, baked in.",
        "#ifndef STUB_KEYS_H",
        "#define STUB_KEYS_H",
        "#include <stdint.h>",
        "#define STUB_PRODUCT_ID %dULL" % product_id,
        "#define STUB_MAX_ATTEMPTS %d" % max_attempts,
        "#define STUB_ROOT_KEY_COUNT %d" % len(roots),
        "static const uint8_t STUB_ROOT_KEYS[%d][64] = {" % len(roots),
    ]
    for i in range(len(roots)):
        point = bytes.fromhex(by_id[i].pubkey)
        if len(point) != 65 or point[0] != 0x04:
            raise BuildError("root key %d is not an uncompressed P-256 point" % i)
        body = ", ".join("0x%02x" % b for b in point[1:])
        lines.append("    { %s }," % body)
    lines += ["};", "#endif // STUB_KEYS_H", ""]
    return "\n".join(lines)


def _product_id(p, name: str) -> int:
    override = p.config.overrides.get(name, {})
    bid = override.get("product_id")
    return int(bid) if bid is not None else derive_product_id(p.config.name, name)


def _build_one(p, name: str, roots, out_dir: Path, jobs, signer) -> StubResult:
    from openmv_ota.ota import stub_footer
    from openmv_ota.ota.errors import OtaError
    tmp = Path(tempfile.mkdtemp(prefix="openmv-ota-stub-"))
    try:
        (tmp / KEYS_HEADER).write_text(
            stub_keys_header(roots, _product_id(p, name), p.config.max_attempts),
            encoding="utf-8")
        build = tmp / "build"
        args = [
            "-C", str(STUB_DIR),
            "OMV_DIR=%s" % p.firmware_path,
            "BOARD=%s" % name,
            "KEYS_DIR=%s" % tmp,
            "BUILD=%s" % build,
            "-j%d" % (jobs or os.cpu_count() or 1),
        ]
        # The firmware's own Makefile puts the SDK toolchain on its PATH; the stub's does
        # not, so name it -- the project's SDK is the toolchain the firmware was built with.
        cross = _cross_compile(p.sdk_home)
        if cross is not None:
            args.append("CROSS_COMPILE=%s" % cross)
        _run_make(args)
        built = build / "stub.bin"
        if not built.is_file():
            raise BuildError("stub build produced no stub.bin for %s" % name)
        image = built.read_bytes()
        try:
            sealed = stub_footer.seal(image, get_board(name).stub.stub_max,
                                      stub_version=stub_version(), product_id=_product_id(p, name),
                                      certificate=signer.certificate, signer=signer)
        except OtaError as e:
            raise BuildError("%s: %s" % (name, e)) from None
        out = out_dir / ("%s-stub.bin" % name)
        out.write_bytes(sealed)
        return StubResult(name, out, len(image), len(sealed), signer.key_id)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _cross_compile(sdk_home: Path) -> str | None:
    """The SDK's ``arm-none-eabi-`` prefix when its gcc is installed, else None (PATH)."""
    gcc = Path(sdk_home) / "gcc" / "bin" / "arm-none-eabi-gcc"
    return str(gcc)[:-len("gcc")] if gcc.is_file() else None


def _run_make(args: list[str]) -> None:
    try:
        subprocess.run([MAKE, *args], check=True)
    except FileNotFoundError:
        raise BuildError("make not found - a firmware build toolchain is required",
                         exit_code=2) from None
    except subprocess.CalledProcessError as e:
        raise BuildError("stub build failed (make %s): exit %d"
                         % (" ".join(args), e.returncode), exit_code=e.returncode) from None
