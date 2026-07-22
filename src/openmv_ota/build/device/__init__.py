"""Device-side sources the firmware build freezes into the openmv firmware.

These run under MicroPython, so they avoid host-only constructs (no
``from __future__`` import, no dataclasses/typing) and import only modules
present on-device. ``boot.py`` is the module openmv runs at boot; its pure logic
is unit-testable on the host, and its device entry stays inert unless the
build-generated ``_ota_config`` (keys + geometry + ids) is present beside it. The
firmware build freezes ``boot.py``, generates ``_ota_config``, and drops the ECDSA
C module into the firmware's ``modules/`` dir.

RAM BUDGET: this runs on the device inside the *user's* app -- our memory is
their memory. No allocation may be sized by something we don't control (a file's
size, a response body, a length field off the wire, a queue that grows while the
network is down). Use bounded windows of a few KB, stream anything larger, and
alias with memoryview/bytearray_at instead of copying. Every buffer needs a
ceiling you can point at. See the RAM budget section in CLAUDE.md.
"""
