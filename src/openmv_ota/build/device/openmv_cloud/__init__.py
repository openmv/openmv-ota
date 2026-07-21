"""``openmv_cloud`` -- the on-device OpenMV Cloud SDK (shipped in the app romfs).

The home of the cloud-connected wrapper modules, scaffolded into a project's
``app/lib/openmv_cloud/`` by ``openmv-ota project new --ota``:

    from openmv_cloud import csi          # the async camera, OpenMV Live built in
    # future: from openmv_cloud import datalog, ...

Deliberately separate from ``openmv_ota`` (the update runtime: status/confirm/
sync/install) -- that package is about updating the device; this one is about
the device talking to OpenMV Cloud features. Both live in the app romfs, so both
are OTA-updatable; the frozen top-level survival modules (``openmv_log``,
``openmv_wdt``) stay top-level because the installer needs them mid-erase.
"""
