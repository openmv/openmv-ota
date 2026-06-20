"""Firmware build orchestration (stub).

TODO (see concept plan, Tool 1):
  1. Clone openmv at the specified commit.
  2. Read board.json -> sizing constants + OPENMV_FIRMWARE_VER.
  3. Read trusted_keys.json -> TRUSTED_KEYS map.
  4. Render templates/boot.py.in -> frozen boot.py.
  5. Copy ed25519_verify into ports/<port>/modules/.
  6. Patch the firmware build to add the module and freeze boot.py.
  7. Build firmware.bin for the target board.
  8. Emit audit artefacts (SBOM, reproducibility, conformity, security.txt).
  9. CVE-scan the SBOM against NVD/OSV.
"""
