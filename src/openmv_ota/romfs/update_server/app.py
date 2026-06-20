"""Update server application (stub).

Public API (devices):
  POST /api/v1/check       -> {available, version, url, size, sha256}
  GET  /releases/<board>/<version>.bin
  POST /api/v1/telemetry

Admin API (customer CI):
  POST /api/v1/admin/release   (with rollout policy: canary %, allowlist, schedule)
  GET  /api/v1/admin/fleet
  GET  /api/v1/admin/audit

TODO: pick a web framework + object-storage adapter; implement the endpoints.
"""
