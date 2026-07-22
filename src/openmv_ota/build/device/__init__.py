"""Device-side sources the firmware build freezes into the openmv firmware.

These run under MicroPython, so they avoid host-only constructs (no
``from __future__`` import, no dataclasses/typing) and import only modules
present on-device. ``boot.py`` is the module openmv runs at boot; its pure logic
is unit-testable on the host, and its device entry stays inert unless the
build-generated ``_ota_config`` (keys + geometry + ids) is present beside it. The
firmware build freezes ``boot.py``, generates ``_ota_config``, and drops the ECDSA
C module into the firmware's ``modules/`` dir.

RAM BUDGET: this module runs inside your application, so its memory is your
memory. Every buffer here has a ceiling. Nothing is sized by a file's length, a
response body, a length field off the wire, or a queue that grows while the
network is down: reads use bounded windows of a few KB, anything larger is
streamed, and large data is aliased with memoryview/bytearray_at rather than
copied.
"""
