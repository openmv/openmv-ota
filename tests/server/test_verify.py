"""The registration verifier: parse, fail-closed, positive-only caching."""

from __future__ import annotations

from openmv_ota.server.settings import ServerSettings
from openmv_ota.server.verify import Registration, RegistrationVerifier, build_verifier


class _Resp:
    def __init__(self, status_code=200, payload=None, raise_json=False):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self._raise = raise_json

    def json(self):
        if self._raise:
            raise ValueError("bad json")
        return self._payload


class _Client:
    def __init__(self, resp=None, exc=None):
        self._resp = resp
        self._exc = exc
        self.calls: list = []

    def post(self, url, data, headers, timeout):
        self.calls.append((url, data, headers, timeout))
        if self._exc:
            raise self._exc
        return self._resp


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_registered_parses_and_sends_auth():
    c = _Client(_Resp(200, {"registered": True, "board": "N6", "id": "abc", "registrar_ref": "o1"}))
    reg = RegistrationVerifier("https://swd/verify", "tok", c).verify("N6", "abc")
    assert reg == Registration(True, "o1")
    url, body, headers, _ = c.calls[0]
    assert url == "https://swd/verify" and body == {"board": "N6", "id": "abc"}
    assert headers["Authorization"] == "Bearer tok"


def test_unregistered_is_false():
    v = RegistrationVerifier("u", "t", _Client(_Resp(200, {"registered": False})))
    assert v.verify("b", "d") == Registration(False)


def test_non_200_fail_closed():
    v = RegistrationVerifier("u", "t", _Client(_Resp(503, {"registered": True})))
    assert v.verify("b", "d").registered is False


def test_transport_error_fail_closed():
    v = RegistrationVerifier("u", "t", _Client(exc=RuntimeError("connrefused")))
    assert v.verify("b", "d").registered is False


def test_bad_json_fail_closed():
    v = RegistrationVerifier("u", "t", _Client(_Resp(200, raise_json=True)))
    assert v.verify("b", "d").registered is False


def test_positive_result_cached_until_ttl():
    clk = _Clock()
    c = _Client(_Resp(200, {"registered": True}))
    v = RegistrationVerifier("u", "t", c, cache_ttl=100, now=clk)
    assert v.verify("b", "d").registered and v.verify("b", "d").registered
    assert len(c.calls) == 1                       # served from cache
    clk.t += 101
    assert v.verify("b", "d").registered
    assert len(c.calls) == 2                       # TTL expired -> re-queried


def test_negative_result_never_cached():
    c = _Client(_Resp(200, {"registered": False}))
    v = RegistrationVerifier("u", "t", c)
    v.verify("b", "d")
    v.verify("b", "d")
    assert len(c.calls) == 2                        # each miss re-queries (no unbounded neg cache)


def test_build_verifier_from_settings():
    v = build_verifier(ServerSettings(swd_ids_verify_url="https://swd", swd_ids_verify_token="tk"))
    assert isinstance(v, RegistrationVerifier) and v._url == "https://swd" and v._token == "tk"
