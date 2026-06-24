"""ROMFS image tooling and the OTA layers built on top of it.

Two clearly separated layers live under this package.

**Layer 1 — the generic ROMFS image tool (implemented).** Builds and inspects
OpenMV ROMFS images with board-aware alignment. It has no knowledge of OTA,
signing, or updates. See :doc:`docs/romfs.md`.

* :mod:`~openmv_ota.romfs.container` — the ROMFS format: ``VfsRomWriter`` /
  ``VfsRomReader`` (a faithful port of the OpenMV IDE's reference implementation).
* :mod:`~openmv_ota.romfs.boards` — per-board alignment rules + partition sizes.
* :mod:`~openmv_ota.romfs.builder` — directory <-> image, capacity + verify.
* :mod:`~openmv_ota.romfs.cli` — the ``openmv-ota romfs`` command group.

**Layer 2 — the OTA layers.** Everything that makes an image updatable. Compose +
sign (factory / OTA slots) and the firmware build with the frozen ``boot.py`` are
implemented in :mod:`openmv_ota.build` and :mod:`openmv_ota.ota` (ECDSA-over-mbedtls
signing). What remains under this package are forward stubs:

* :mod:`~openmv_ota.romfs.update_server` — stateless update server.
* ``sdk/`` — the on-device ``openmv_ota`` package, bundled into the ROMFS.
"""
