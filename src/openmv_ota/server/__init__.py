"""The OTA update server -- hosts signed releases and drives fleet rollouts.

Design (see the plan):
* **The server never signs and never holds a private key.** Releases are signed *locally* by
  ``build ota-romfs``; the server stores + distributes the already-signed bytes and runs policy.
  The device verifies signatures itself against firmware-baked ``TRUSTED_KEYS``.
* **Registration gates everything.** Every device is validated against the central OpenMV
  registration registry (openmv-swd-ids); an unregistered ``(board, id)`` gets ``{update:false}``
  and leaves *zero footprint* -- no row, no telemetry, no artifact -- so an attacker looping the
  id-space can't exhaust storage or bandwidth.
* **Library first, CLI second.** ``create_app(settings, ...)`` is an embeddable, fully-injectable
  factory (storage / metadata-store / registration-verifier / admin-auth are pluggable); the
  ``server`` CLI wraps it for self-hosters. OpenMV's private website embeds the factory and
  supplies its own auth + infra.

Everything here lives behind the ``server`` optional-dependency extra
(``pip install openmv-ota[server]``); the CLI guards on it (see ``_extras``).
"""
