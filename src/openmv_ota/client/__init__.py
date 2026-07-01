"""The OTA client -- talks to an update server's admin API over HTTPS.

Turns ``build ota-romfs`` output into a published release + rollout without the user ever typing a
URL. ``login``/``logout`` manage a saved profile (server URL + admin token) and need only the
standard library; the API-calling verbs (``publish``/``rollout``/``fleet``/…) use ``httpx`` from
the ``server`` optional-dependency extra.
"""
