"""Device-side sources the firmware build freezes into the openmv firmware.

These run under MicroPython, so they avoid host-only constructs (no
``from __future__`` import, no dataclasses/typing) and import only modules
present on-device. ``boot.py`` is the module openmv runs at boot; its pure logic
is unit-testable on the host, and its device entry stays inert unless the
build-generated ``_ota_config`` (keys + geometry + ids) is present beside it. The
firmware build freezes ``boot.py``, generates ``_ota_config``, and drops the ECDSA
C module into the firmware's ``modules/`` dir.
"""
