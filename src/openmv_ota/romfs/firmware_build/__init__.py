"""Tool 1 — firmware builder.

Clones openmv at a pinned commit, renders ``templates/boot.py.in`` into a frozen
``boot.py`` (substituting ``TRUSTED_KEYS``, ``FRONT_SIZE``, ``PARTITION_SIZE``,
``OPENMV_FIRMWARE_VER`` from ``board.json`` + ``trusted_keys.json``), drops the
``ed25519_verify`` C module into the port, builds ``firmware.bin``, and emits the
audit artefacts (SBOM, CVE report, reproducibility manifest, security.txt, …).

See the concept plan, "Tool 1: Firmware builder".
"""
