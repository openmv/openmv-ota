"""The Api HTTP layer: request shaping, auth header, error mapping, the httpx guard."""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from openmv_ota.client.api import Api, _require_httpx
from openmv_ota.client.errors import ClientError


class _Resp:
    def __init__(self, status_code=200, payload=None, text="", content=b"{}"):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.content = content

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class _Client:
    def __init__(self, resp):
        self.resp = resp
        self.calls: list = []

    def request(self, method, path, **kw):
        self.calls.append((method, path, kw))
        return self.resp


def _cfg():
    return SimpleNamespace(server_url="https://s", token="tok")


def _api(resp):
    c = _Client(resp)
    return Api(_cfg(), client=c), c


def test_require_httpx_present():
    assert _require_httpx().__name__ == "httpx"


def test_require_httpx_missing(monkeypatch):
    monkeypatch.setitem(sys.modules, "httpx", None)
    with pytest.raises(ClientError, match=r"pip install openmv-ota\[server\]"):
        _require_httpx()


def test_default_client_is_constructed():
    assert Api(_cfg())._client is not None            # builds a real httpx.Client (no request)


def test_req_adds_bearer_and_returns_json():
    api, c = _api(_Resp(200, {"ok": 1}))
    assert api.fleet() == {"ok": 1}
    method, path, kw = c.calls[0]
    assert (method, path) == ("GET", "/api/v1/admin/fleet")
    assert kw["headers"]["Authorization"] == "Bearer tok"


def test_error_status_maps_to_client_error_with_detail():
    api, _ = _api(_Resp(409, {"detail": "too old"}))
    with pytest.raises(ClientError, match="409: too old") as e:
        api.create_rollout("rel1", "__default__", 5)
    assert e.value.exit_code == 1


def test_error_detail_falls_back_to_text():
    api, _ = _api(_Resp(500, payload=None, text="boom"))
    with pytest.raises(ClientError, match="boom"):
        api.fleet()


def test_publish_shapes_multipart_and_params():
    api, c = _api(_Resp(200, {"release_id": "r1"}))
    api.publish_release(b"MAN", b"IMG", b"DEL", True)
    _, path, kw = c.calls[0]
    assert path == "/api/v1/admin/releases"
    assert set(kw["files"]) == {"manifest", "image", "delta"}
    assert kw["params"] == {"allow_republish": "true"}
    api.publish_release(b"MAN", b"IMG", None, False)          # no delta, no republish
    _, _, kw2 = c.calls[1]
    assert "delta" not in kw2["files"] and kw2["params"] == {}


def test_rollout_calls():
    api, c = _api(_Resp(200, {}))
    api.create_rollout("r1", "beta", 5)
    api.patch_rollout("ro1", percent=50)
    api.rollback_rollout("ro1")
    assert c.calls[0][:2] == ("POST", "/api/v1/admin/rollouts")
    assert c.calls[0][2]["json"] == {"release_id": "r1", "cohort": "beta", "percent": 5}
    assert c.calls[1][:2] == ("PATCH", "/api/v1/admin/rollouts/ro1")
    assert c.calls[1][2]["json"] == {"percent": 50}
    assert c.calls[2][:2] == ("POST", "/api/v1/admin/rollouts/ro1/rollback")


def test_read_calls_carry_params():
    api, c = _api(_Resp(200, {}))
    api.fleet(7)
    api.devices()
    api.releases(7)
    api.audit(3)
    assert c.calls[0][2]["params"] == {"product_id": 7}
    assert c.calls[1][2]["params"] == {}
    assert c.calls[2][:2] == ("GET", "/api/v1/admin/releases")
    assert c.calls[2][2]["params"] == {"product_id": 7}
    assert c.calls[3][2]["params"] == {"since": 3}


def test_devices_filter_and_paging_params():
    api, c = _api(_Resp(200, {}))
    api.devices(7, cohort="beta", limit=2, offset=4)
    assert c.calls[0][2]["params"] == {"product_id": 7, "cohort": "beta", "limit": 2, "offset": 4}
    api.devices()                                              # all-None -> no params
    assert c.calls[1][2]["params"] == {}


def test_cohort_calls():
    api, c = _api(_Resp(200, {}))
    api.list_cohorts(7)
    api.assign_cohort("beta", ["d1", "d2"])
    assert c.calls[0][:2] == ("GET", "/api/v1/admin/cohorts")
    assert c.calls[0][2]["params"] == {"product_id": 7}
    assert c.calls[1][:2] == ("POST", "/api/v1/admin/cohorts/assign")
    assert c.calls[1][2]["json"] == {"cohort": "beta", "device_ids": ["d1", "d2"]}


def test_pin_calls():
    api, c = _api(_Resp(200, {}))
    api.pin_device("d1", "rel1")
    api.pin_cohort(7, "beta", None)
    assert c.calls[0][:2] == ("PATCH", "/api/v1/admin/devices/d1/pin")
    assert c.calls[0][2]["json"] == {"release_id": "rel1"}
    assert c.calls[1][:2] == ("POST", "/api/v1/admin/cohorts/pin")
    assert c.calls[1][2]["json"] == {"product_id": 7, "cohort": "beta", "release_id": None}


def test_bind_call():
    api, c = _api(_Resp(200, {"account_id": "acctA"}))
    api.bind_device("d1")
    assert c.calls[0][:2] == ("POST", "/api/v1/admin/devices/d1/account")


def test_account_calls():
    api, c = _api(_Resp(200, {"account_id": "acct_x", "token": "t"}))
    api.create_account("DroneCo")
    api.list_accounts()
    assert c.calls[0][:2] == ("POST", "/api/v1/admin/accounts")
    assert c.calls[0][2]["json"] == {"name": "DroneCo"}
    assert c.calls[1][:2] == ("GET", "/api/v1/admin/accounts")


def test_releases_paging_params():
    api, c = _api(_Resp(200, {}))
    api.releases(7, limit=2, offset=4)
    assert c.calls[0][2]["params"] == {"product_id": 7, "limit": 2, "offset": 4}


def test_token_calls():
    api, c = _api(_Resp(200, {"token_hash": "th", "token": "t"}))
    api.issue_token("acctA", "ci")
    api.issue_token("acctA", "ro", scopes=["observe"])
    api.list_account_tokens("acctA")
    api.revoke_token("th")
    api.rotate_token("th")
    assert c.calls[0][:2] == ("POST", "/api/v1/admin/accounts/acctA/tokens")
    assert c.calls[0][2]["json"] == {"name": "ci"}                       # no scopes -> server default
    assert c.calls[1][2]["json"] == {"name": "ro", "scopes": ["observe"]}
    assert c.calls[2][:2] == ("GET", "/api/v1/admin/accounts/acctA/tokens")
    assert c.calls[3][:2] == ("POST", "/api/v1/admin/tokens/th/revoke")
    assert c.calls[4][:2] == ("POST", "/api/v1/admin/tokens/th/rotate")


def test_empty_body_returns_empty_dict():
    api, _ = _api(_Resp(200, payload={"x": 1}, content=b""))   # no content -> {}
    assert api.fleet() == {}


def test_make_api_seam_builds_real_api():
    from openmv_ota.client import cli as client_cli
    assert isinstance(client_cli._make_api(_cfg()), Api)       # the real (unmocked) seam
