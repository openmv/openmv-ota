"""``openmv_cloud`` -- the on-device OpenMV Cloud SDK (shipped in the app romfs).

The home of the cloud-connected wrapper modules, scaffolded into a project's
``app/lib/openmv_cloud/`` by ``openmv-ota project new --ota``:

    from openmv_cloud import csi          # the async camera, OpenMV Live built in
    from openmv_cloud import logs         # console mirrored to the cloud + datalake
    from openmv_cloud import datalog      # structured telemetry to the datalake

Importing a wrapper auto-registers it with the OTA check-in (grants for Live,
console ingest, and telemetry ingest arrive on the same poll) -- the app just
imports and uses; nothing to wire.

Deliberately separate from ``openmv_ota`` (the update runtime: status/confirm/
sync/install) -- that package is about updating the device; this one is about
the device talking to OpenMV Cloud features. Both live in the app romfs, so both
are OTA-updatable; the frozen top-level survival modules (``openmv_log``,
``openmv_wdt``) stay top-level because the installer needs them mid-erase.
"""
