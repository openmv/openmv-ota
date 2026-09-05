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

    def publish_release(self, manifest: bytes, image: bytes, deltas, allow_republish: bool,
                        sbom: bytes | None = None, display_name: str = ""):
        """Upload a release. ``deltas`` is ``{filename: gzipped patch}`` -- a release carries
        one per base version, and each is uploaded UNDER THE NAME THE MANIFEST DECLARES,
        because that name is how the server matches an artifact to its representation and how
        a device later asks for it. ``sbom`` (CycloneDX JSON, optional) rides beside the
        artifacts and is served back per release."""
        files = [("manifest", ("manifest.bin", manifest, "application/octet-stream")),
                 ("image", ("image.gz", image, "application/gzip"))]
        for filename, patch in sorted((deltas or {}).items()):
            files.append(("delta", (filename, patch, "application/gzip")))
        if sbom is not None:
            files.append(("sbom", ("sbom.cdx.json", sbom, "application/json")))
        params = {"allow_republish": "true"} if allow_republish else {}
        if display_name:
            params["display_name"] = display_name
        return self._req("POST", "/api/v1/admin/releases", files=files, params=params)

    def fleet_bases(self, product_id=None):
        """The distinct (version, body_sha256) bases the fleet is running, with device
        counts -- what `client release bases --fleet` plans deltas against."""
        params = {} if product_id is None else {"product_id": product_id}
        return self._req("GET", "/api/v1/admin/fleet/bases", params=params)

    def release_manifest(self, release_id: str) -> bytes:
        """The signed manifest, byte-exact as published."""
        resp = self._client.request(
            "GET", "/api/v1/admin/releases/%s/manifest" % release_id,
            headers={"Authorization": "Bearer %s" % self._token})
        if resp.status_code >= 400:
            raise ClientError("GET release manifest %s -> %d: %s"
                              % (release_id, resp.status_code, _detail(resp)),
                              exit_code=1)
        return resp.content

    def release_artifact(self, release_id: str, filename: str) -> bytes:
        """One artifact by its manifest-declared filename (full image or a delta)."""
        resp = self._client.request(
            "GET", "/api/v1/admin/releases/%s/artifacts/%s" % (release_id, filename),
            headers={"Authorization": "Bearer %s" % self._token})
        if resp.status_code >= 400:
            raise ClientError("GET release artifact %s/%s -> %d: %s"
                              % (release_id, filename, resp.status_code, _detail(resp)),
                              exit_code=1)
        return resp.content

    def release_image(self, release_id: str) -> bytes:
        """The raw gzipped image of a retained release -- a delta base."""
        resp = self._client.request(
            "GET", "/api/v1/admin/releases/%s/image" % release_id,
            headers={"Authorization": "Bearer %s" % self._token})
        if resp.status_code >= 400:
            raise ClientError("GET release image %s -> %d: %s"
                              % (release_id, resp.status_code, _detail(resp)), exit_code=1)
        return resp.content

    def release_sbom(self, release_id: str) -> bytes:
        """The release's CycloneDX SBOM, as uploaded at publish."""
        resp = self._client.request(
            "GET", "/api/v1/admin/releases/%s/sbom" % release_id,
            headers={"Authorization": "Bearer %s" % self._token})
        if resp.status_code >= 400:
            raise ClientError("GET release sbom %s -> %d: %s"
                              % (release_id, resp.status_code, _detail(resp)), exit_code=1)
        return resp.content

    def create_rollout(self, release_id: str, cohort: str, percent: float,
                       failure_threshold: float | None = None, display_name: str = ""):
        body = {"release_id": release_id, "cohort": cohort, "percent": percent}
        if failure_threshold is not None:
            body["failure_threshold"] = failure_threshold
        if display_name:
            body["display_name"] = display_name
        return self._req("POST", "/api/v1/admin/rollouts", json=body)

    def patch_rollout(self, rollout_id: str, **body):
        return self._req("PATCH", "/api/v1/admin/rollouts/%s" % rollout_id, json=body)

    def stop_rollout(self, rollout_id: str):
        return self._req("POST", "/api/v1/admin/rollouts/%s/stop" % rollout_id)

    @staticmethod
    def _page(params: dict, limit, offset, sort=None, direction=None) -> dict:
        """The list contract's paging/sorting knobs, added only when given."""
        for k, v in (("limit", limit), ("offset", offset), ("sort", sort), ("dir", direction)):
            if v is not None:
                params[k] = v
        return params

    def products(self):
        """The account's product directory (id, friendly name, device/release counts)."""
        return self._req("GET", "/api/v1/admin/products")

    def list_rollouts(self, product_id=None, limit=None, offset=None, state=None,
                      cohort=None, sort=None, direction=None):
        params = self._page({}, None, None, sort, direction)
        if product_id is not None:
            params["product_id"] = product_id
        if state is not None:
            params["state"] = state
        if cohort is not None:
            params["cohort"] = cohort
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset
        return self._req("GET", "/api/v1/admin/rollouts", params=params)

    def rollout_status(self, rollout_id: str):
        return self._req("GET", "/api/v1/admin/rollouts/%s/status" % rollout_id)

    def list_cohorts(self, product_id=None, limit=None, offset=None, sort=None, direction=None):
        params = {"product_id": product_id} if product_id is not None else {}
        return self._req("GET", "/api/v1/admin/cohorts",
                         params=self._page(params, limit, offset, sort, direction))

    def assign_cohort(self, cohort, device_ids=None, product_id=None):
        body = {"cohort": cohort}
        if device_ids is not None:
            body["device_ids"] = device_ids
        if product_id is not None:
            body["product_id"] = product_id
        return self._req("POST", "/api/v1/admin/cohorts/assign", json=body)

    def create_cohort(self, cohort):
        return self._req("POST", "/api/v1/admin/cohorts/create", json={"cohort": cohort})

    def rename_cohort(self, cohort, name):
        return self._req("POST", "/api/v1/admin/cohorts/rename",
                         json={"cohort": cohort, "name": name})

    def delete_cohort(self, cohort):
        return self._req("POST", "/api/v1/admin/cohorts/delete", json={"cohort": cohort})

    def pin_device(self, device_id, release_id):
        return self._req("PATCH", "/api/v1/admin/devices/%s/pin" % device_id,
                         json={"release_id": release_id})

    def rename_device(self, device_id, name):
        """Set the device's display name ('' clears). A label, never identity."""
        return self._req("PATCH", "/api/v1/admin/devices/%s/name" % device_id,
                         json={"name": name})

    def advisories(self, release_id=None, active_only=True, limit=None, offset=None, sort=None,
                   direction=None):
        """The account's CVE findings from SBOM scans (active by default)."""
        params = {"active_only": "true" if active_only else "false"}
        if release_id is not None:
            params["release_id"] = release_id
        return self._req("GET", "/api/v1/admin/advisories",
                         params=self._page(params, limit, offset, sort, direction))

    def scan_advisories(self, release_id=None):
        """Run a scan now: one release, or every release the fleet still runs."""
        body = {} if release_id is None else {"release_id": release_id}
        return self._req("POST", "/api/v1/admin/advisories/scan", json=body)

    def rename_release(self, release_id, name):
        """Set a release's display name ('' clears). A label, never identity."""
        return self._req("PATCH", "/api/v1/admin/releases/%s/name" % release_id,
                         json={"name": name})

    def rename_rollout(self, rollout_id, name):
        """Set a rollout's display name ('' clears). A label, never identity."""
        return self._req("PATCH", "/api/v1/admin/rollouts/%s/name" % rollout_id,
                         json={"name": name})

    def bind_device(self, device_id):
        return self._req("POST", "/api/v1/admin/devices/%s/account" % device_id)

    def create_account(self, name):
        return self._req("POST", "/api/v1/admin/accounts", json={"name": name})

    def list_accounts(self):
        return self._req("GET", "/api/v1/admin/accounts")

    def rename_account(self, account_id, name):
        return self._req("PATCH", "/api/v1/admin/accounts/%s" % account_id, json={"name": name})

    def deactivate_account(self, account_id):
        return self._req("POST", "/api/v1/admin/accounts/%s/deactivate" % account_id)

    def activate_account(self, account_id):
        return self._req("POST", "/api/v1/admin/accounts/%s/activate" % account_id)

    def issue_token(self, account_id, name, scopes=None):
        body = {"name": name}
        if scopes is not None:
            body["scopes"] = scopes
        return self._req("POST", "/api/v1/admin/accounts/%s/tokens" % account_id, json=body)

    def list_account_tokens(self, account_id):
        return self._req("GET", "/api/v1/admin/accounts/%s/tokens" % account_id)

    def revoke_token(self, token_hash):
        return self._req("POST", "/api/v1/admin/tokens/%s/revoke" % token_hash)

    def rotate_token(self, token_hash):
        return self._req("POST", "/api/v1/admin/tokens/%s/rotate" % token_hash)

    def pin_cohort(self, product_id, cohort, release_id):
        return self._req("POST", "/api/v1/admin/cohorts/pin",
                         json={"product_id": product_id, "cohort": cohort, "release_id": release_id})

    def fleet(self, product_id=None, cohort=None):
        params = {}
        if product_id is not None:
            params["product_id"] = product_id
        if cohort is not None:
            params["cohort"] = cohort
        return self._req("GET", "/api/v1/admin/fleet", params=params)

    def devices(self, product_id=None, cohort=None, limit=None, offset=None, sort=None,
                direction=None, q=None, cohort_not=None):
        params = {}
        if product_id is not None:
            params["product_id"] = product_id
        if cohort is not None:
            params["cohort"] = cohort
        if q:
            params["q"] = q
        if cohort_not is not None:
            params["cohort_not"] = cohort_not
        self._page(params, limit, offset, sort, direction)
        return self._req("GET", "/api/v1/admin/devices", params=params)

    def releases(self, product_id=None, limit=None, offset=None, sort=None, direction=None):
        params = {}
        if product_id is not None:
            params["product_id"] = product_id
        return self._req("GET", "/api/v1/admin/releases",
                         params=self._page(params, limit, offset, sort, direction))

    def device(self, device_id: str):
        """One device. The detail read behind a UI's device page -- `devices()` can only page a
        list, which is a lot of requests to answer "show me this one"."""
        return self._req("GET", "/api/v1/admin/devices/%s" % device_id)

    def release(self, release_id: str):
        """One release, same reasoning as `device()`."""
        return self._req("GET", "/api/v1/admin/releases/%s" % release_id)

    def audit(self, since: int = 0, entity_id: str | None = None, limit=None, offset=None,
              sort=None, direction=None, newest=None):
        params = {"since": since}
        if entity_id is not None:
            params["entity_id"] = entity_id
        if newest:
            params["newest"] = "true"
        return self._req("GET", "/api/v1/admin/audit",
                         params=self._page(params, limit, offset, sort, direction))
