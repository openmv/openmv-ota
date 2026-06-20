"""Tool 4 — update server.

Stateless API + object storage so it scales horizontally and runs anywhere.
Devices call the public API (/api/v1/check, /releases/..., /api/v1/telemetry);
the customer's CI calls the admin API (release upload, fleet status, audit).
The server never signs anything and never sees private keys.

See the concept plan, "Tool 4: Update server".
"""
