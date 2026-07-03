"""HTTP calls to an update server's admin API.

``Api`` wraps an ``httpx.Client``-like transport (injectable -- tests pass a FastAPI
``TestClient`` for a real client<->server round-trip) and adds the bearer token + error mapping.
A non-2xx becomes a ``ClientError`` carrying the server's ``detail``.
"""

from __future__ import annotations

from .errors import ClientError


def _require_httpx():
    try:
        import httpx
    except ImportError:
        raise ClientError("the client needs extra packages -- run: pip install openmv-ota[server]",
                          exit_code=2) from None
    return httpx


def _detail(resp) -> str:
    try:
        return resp.json().get("detail", resp.text)
    except Exception:
        return resp.text


class Api:
    def __init__(self, cfg, *, client=None):
        self._token = cfg.token
        if client is None:
            client = _require_httpx().Client(base_url=cfg.server_url, timeout=30.0)
        self._client = client

    def _req(self, method: str, path: str, **kw):
        headers = kw.pop("headers", {})
        headers["Authorization"] = "Bearer " + self._token
        resp = self._client.request(method, path, headers=headers, **kw)
        if resp.status_code >= 400:
            raise ClientError("%s %s -> %d: %s" % (method, path, resp.status_code, _detail(resp)),
                              exit_code=1)
        return resp.json() if resp.content else {}

    def publish_release(self, manifest: bytes, image: bytes, delta: bytes | None,
                        allow_republish: bool):
        files = {"manifest": ("manifest.bin", manifest, "application/octet-stream"),
                 "image": ("image.gz", image, "application/gzip")}
        if delta is not None:
            files["delta"] = ("delta.gz", delta, "application/gzip")
        params = {"allow_republish": "true"} if allow_republish else {}
        return self._req("POST", "/api/v1/admin/releases", files=files, params=params)

    def create_rollout(self, release_id: str, cohort: str, percent: float):
        return self._req("POST", "/api/v1/admin/rollouts",
                         json={"release_id": release_id, "cohort": cohort, "percent": percent})

    def patch_rollout(self, rollout_id: str, **body):
        return self._req("PATCH", "/api/v1/admin/rollouts/%s" % rollout_id, json=body)

    def rollback_rollout(self, rollout_id: str):
        return self._req("POST", "/api/v1/admin/rollouts/%s/rollback" % rollout_id)

    def list_cohorts(self, product_id=None):
        params = {"product_id": product_id} if product_id is not None else {}
        return self._req("GET", "/api/v1/admin/cohorts", params=params)

    def assign_cohort(self, cohort, device_ids):
        return self._req("POST", "/api/v1/admin/cohorts/assign",
                         json={"cohort": cohort, "device_ids": device_ids})

    def pin_device(self, device_id, release_id):
        return self._req("PATCH", "/api/v1/admin/devices/%s/pin" % device_id,
                         json={"release_id": release_id})

    def pin_cohort(self, product_id, cohort, release_id):
        return self._req("POST", "/api/v1/admin/cohorts/pin",
                         json={"product_id": product_id, "cohort": cohort, "release_id": release_id})

    def fleet(self, product_id=None):
        params = {"product_id": product_id} if product_id is not None else {}
        return self._req("GET", "/api/v1/admin/fleet", params=params)

    def devices(self, product_id=None):
        params = {"product_id": product_id} if product_id is not None else {}
        return self._req("GET", "/api/v1/admin/devices", params=params)

    def releases(self, product_id=None):
        params = {"product_id": product_id} if product_id is not None else {}
        return self._req("GET", "/api/v1/admin/releases", params=params)

    def audit(self, since: int = 0):
        return self._req("GET", "/api/v1/admin/audit", params={"since": since})
