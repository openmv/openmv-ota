"""The update server as the datalake's operator: purging a forgotten device's data, and
reading an account's usage on its behalf.

Everything else between the two services is a signed grant the device or a dashboard
carries; these calls need the datalake's admin token (``OPENMV_OTA_DATALAKE_ADMIN_TOKEN``). Unconfigured means the
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

    def _call(self, method: str, path: str) -> dict:
        """One admin request's JSON body; DatalakeError on any failure."""
        if self._http is None:                     # pragma: no cover - wired in prod only
            import httpx
            self._http = httpx.Client(timeout=30)
        try:
            r = self._http.request(method, self._base + path,
                                   headers={"Authorization": "Bearer " + self._token})
        except Exception as e:                     # noqa: BLE001 - one failure class for the caller
            raise DatalakeError(str(e)[:120]) from e
        if r.status_code != 200:
            raise DatalakeError(f"HTTP {r.status_code}")
        return r.json()

    def purge_device(self, account_id: str, device_id: str) -> dict:
        """``{"deleted": n, "bytes": b}`` once the datalake has removed everything the
        device stored under the account. Raises DatalakeError on any failure."""
        body = self._call("DELETE", f"/api/v1/admin/accounts/{account_id}/devices/{device_id}")
        return {"deleted": int(body.get("deleted") or 0), "bytes": int(body.get("bytes") or 0)}

    def account_usage(self, account: str) -> dict:
        """The account's datalake reading: bytes stored, bytes uploaded today against the
        daily quota (refused batches included, as the quota counts them), the batches the
        quota refused today, and the retention window -- an override where the account has
        one, else the deployment's default. Raises DatalakeError on any failure."""
        usage = self._call("GET", f"/api/v1/admin/accounts/{account}/usage")
        limits = self._call("GET", f"/api/v1/admin/accounts/{account}/limits")
        defaults = limits.get("defaults") or {}

        def lever(name):
            own = limits.get(name)
            return own if own is not None else defaults.get(name)
        return {"stored_bytes": int(usage.get("bytes") or 0),
                "ingested_today": int(usage.get("ingested_today") or 0),
                "quota_bytes_per_day": lever("bytes_per_day"),
                "rejected_today": int(usage.get("rejected_today") or 0),
                "retention_days": lever("retention_days")}
