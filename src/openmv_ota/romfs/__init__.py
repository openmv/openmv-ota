"""ROMFS image tooling and the OTA layers built on top of it.

Two clearly separated layers live under this package.

**Layer 1 — the generic ROMFS image tool (implemented).** Builds and inspects
OpenMV ROMFS images with board-aware alignment. It has no knowledge of OTA,
signing, or updates. See :doc:`docs/tutorial/01-romfs.md`.

* :mod:`~openmv_ota.romfs.container` — the ROMFS format: ``VfsRomWriter`` /
  ``VfsRomReader`` (a faithful port of the OpenMV IDE's reference implementation).
* :mod:`~openmv_ota.romfs.boards` — per-board alignment rules + partition sizes.
* :mod:`~openmv_ota.romfs.builder` — directory <-> image, capacity + verify.
* :mod:`~openmv_ota.romfs.cli` — the ``openmv-ota romfs`` command group.

**Layer 2 — the OTA layers** live elsewhere, and nothing under this package is a
placeholder for them any more: composing + signing the slots is
:mod:`openmv_ota.build` and :mod:`openmv_ota.ota`, the device runtime is
``openmv_ota.build.device``, and the update server is :mod:`openmv_ota.server`.
"""
