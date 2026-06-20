"""ROMFS OTA subsystem.

Signed, anti-rollback ROMFS updates with a golden-image (BACK) fallback, driven
by a frozen ``boot.py`` and host-side build/sign/serve tooling.

Layout (see the concept plan for detail):

* :mod:`~openmv_ota.romfs.firmware_build` — Tool 1: build firmware with the
  frozen ``boot.py`` and the ``ed25519_verify`` C module baked in.
* :mod:`~openmv_ota.romfs.romfs_build` — Tool 3: compose + sign factory / OTA
  ROMFS slot images.
* :mod:`~openmv_ota.romfs.update_server` — Tool 4: stateless update server.
* ``sdk/`` — Tool 2: the on-device ``openmv_ota`` package, shipped as package
  data and bundled into the ROMFS at build time.
"""
