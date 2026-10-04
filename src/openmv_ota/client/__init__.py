"""The OTA client -- talks to an update server's admin API over HTTPS.

Turns ``build ota-romfs`` output into a published release + rollout without the user ever typing a
URL. ``login``/``logout`` manage a saved profile (server URL + admin token); the API-calling verbs
(``publish``/``rollout``/``fleet``/…) use ``httpx``. All of it works on a base
``pip install openmv-ota`` -- the ``server`` extra is only for running a server.
"""
