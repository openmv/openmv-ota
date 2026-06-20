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

**Layer 2 — the OTA layers (stubs).** Everything that makes an image updatable;
each consumes Layer 1 and adds higher-level concerns. See the concept plan.

* :mod:`~openmv_ota.romfs.firmware_build` — firmware with the frozen ``boot.py``
  and the ``ed25519_verify`` C module baked in.
* :mod:`~openmv_ota.romfs.romfs_build` — compose + sign factory / OTA slots.
* :mod:`~openmv_ota.romfs.update_server` — stateless update server.
* ``sdk/`` — the on-device ``openmv_ota`` package, bundled into the ROMFS.
"""
