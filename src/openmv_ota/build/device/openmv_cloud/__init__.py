"""``openmv_cloud`` -- the on-device OpenMV Cloud SDK (shipped in the app romfs).

The home of the cloud-connected wrapper modules, scaffolded into a project's
``app/lib/openmv_cloud/`` by ``openmv-ota project new --ota``:

    from openmv_cloud import csi          # the async camera, OpenMV Live built in
    from openmv_cloud import logs         # console mirrored to the cloud + datalake
    from openmv_cloud import datalog      # structured telemetry to the datalake

Importing a wrapper auto-registers it with the OTA check-in (grants for Live,
console ingest, and telemetry ingest arrive on the same poll) -- the app just
imports and uses; nothing to wire.

It is YOUR heap, so every RAM knob is yours to set. Call ``configure()`` before
enabling a sink::

    from openmv_cloud import configure
    configure(budget_bytes=32 * 1024,   # total RAM buffered across ALL sinks
              batch_bytes=4 * 1024,     # bytes per ingest POST
              topics_max=64)            # datalog topics

The defaults are deliberately modest (16 KiB of total buffering). Raise them if
you have heap to spare and want more history to survive a long outage; lower
them if your application needs that memory more.

Deliberately separate from ``openmv_ota`` (the update runtime: status/confirm/
sync/install) -- that package is about updating the device; this one is about
the device talking to OpenMV Cloud features. Both live in the app romfs, so both
are OTA-updatable; the frozen top-level survival modules (``openmv_log``,
``openmv_wdt``) stay top-level because the installer needs them mid-erase.

RAM BUDGET: this module runs inside your application, so its memory is your
memory. Every buffer here has a ceiling. Nothing is sized by a file's length, a
response body, a length field off the wire, or a queue that grows while the
network is down: reads use bounded windows of a few KB, anything larger is
streamed, and large data is aliased with memoryview/bytearray_at rather than
copied.

The ceilings are yours to set -- see ``openmv_cloud.configure()``.
"""

from ._lib import configure, limits  # noqa: F401  (the app-facing RAM knobs)
