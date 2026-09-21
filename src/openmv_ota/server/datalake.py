"""The update server's one write to the datalake: purging a forgotten device's data.

Everything else between the two services is a signed grant the device or a dashboard
carries; this is the server acting as the datalake's operator, so it needs the
datalake's admin token (``OPENMV_OTA_DATALAKE_ADMIN_TOKEN``). Unconfigured means the
call is skipped, not failed: a self-hosted server with no datalake forgets devices the
same as before.
"""

from __future__ import annotations


class DatalakeError(Exception):
    """The datalake refused or could not be reached; the caller decides what that means."""


class DatalakeAdmin:
    def __init__(self, settings, http=None):
        self._base = (settings.datalake_url or "").rstrip("/")
        self._token = settings.datalake_admin_token or ""
        self._http = http

    @property
    def configured(self) -> bool:
        return bool(self._base and self._token)

    def purge_device(self, account_id: str, device_id: str) -> dict:
        """``{"deleted": n, "bytes": b}`` once the datalake has removed everything the
        device stored under the account. Raises DatalakeError on any failure."""
        if self._http is None:                     # pragma: no cover - wired in prod only
            import httpx
            self._http = httpx.Client(timeout=30)
        try:
            r = self._http.delete(f"{self._base}/api/v1/admin/accounts/{account_id}/devices/{device_id}",
                                  headers={"Authorization": "Bearer " + self._token})
        except Exception as e:                     # noqa: BLE001 - one failure class for the caller
            raise DatalakeError(str(e)[:120]) from e
        if r.status_code != 200:
            raise DatalakeError(f"HTTP {r.status_code}")
        body = r.json()
        return {"deleted": int(body.get("deleted") or 0), "bytes": int(body.get("bytes") or 0)}
