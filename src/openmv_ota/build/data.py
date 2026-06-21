"""Access to the vendored per-board NPU config files.

These are the OpenMV IDE's ``firmware/<BOARD>/`` config files (``vela.ini``,
``neuralart.json`` template, ``stm32n6.mpool``), shipped as package data and
referenced by the relative paths in each board's ``npu_config`` (``iniFilePath`` /
``jsonFilePath`` / ``mpoolFilePath``). Invocation follows the IDE, not the
firmware repo's build scripts.
"""

from __future__ import annotations

from importlib.resources import files

from .errors import BuildError


def read_firmware_file(relpath: str) -> bytes:
    """Read a vendored ``data/firmware/<relpath>`` file (e.g. ``OPENMV_AE3/vela.ini``)."""
    try:
        return files("openmv_ota").joinpath("data/firmware", relpath).read_bytes()
    except (FileNotFoundError, OSError):
        raise BuildError("vendored NPU config %r is missing" % relpath, exit_code=1) from None
