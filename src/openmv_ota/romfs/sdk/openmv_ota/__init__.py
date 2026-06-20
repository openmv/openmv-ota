"""openmv_ota — on-device OTA SDK (runs on the OpenMV camera, in /rom).

This is the package the customer's app imports:

    import machine, openmv_ota
    wdt = machine.WDT(timeout=30_000)
    openmv_ota.run(server_url="https://updates.example", self_test=my_self_test, wdt=wdt)

It is shipped as package data inside the host-side ``openmv-ota`` install and
copied into the ROMFS by the ROMFS builder (Tool 3) — customers never install or
version-pin it separately.

Public API (see concept plan, Tool 2):
    run(server_url, self_test, wdt, ...)  -> trial-confirm + poll + install loop
    current_version() -> int
    current_slot()    -> 'FRONT' | 'BACK'
    confirm()         -> mark the current trial image confirmed (idempotent)

TODO: implement against boot.py's telemetry hooks (boot.last_slot, etc.) and the
updater in _update.py.
"""


def run(server_url, self_test=None, wdt=None, **kwargs):
    raise NotImplementedError("openmv_ota.run() — see concept plan, Tool 2")


def current_version():
    raise NotImplementedError


def current_slot():
    raise NotImplementedError


def confirm():
    raise NotImplementedError
