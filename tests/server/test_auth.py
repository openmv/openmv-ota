"""Admin auth: hashed bearer tokens + scoped dependency."""

from __future__ import annotations

import types

import pytest
from fastapi import HTTPException

from openmv_ota.server.auth import Principal, TokenAuth, hash_token, require_scope
from openmv_ota.server.metastore import SqliteMetadataStore
from openmv_ota.server.scopes import SCOPES


def _store(scopes=("publish",), revoked=False):
    s = SqliteMetadataStore(":memory:")
    s.migrate()
    s.add_token(hash_token("secret-token"), "ci", list(scopes))
    if revoked:
        s.revoke_token(hash_token("secret-token"))
    return s


def _req(auth, header=""):
    return types.SimpleNamespace(
        app=types.SimpleNamespace(state=types.SimpleNamespace(admin_auth=auth)),
        headers={"Authorization": header} if header else {})


def test_authenticate_valid():
    p = TokenAuth(_store(("publish", "observe"))).authenticate("Bearer secret-token")
    assert p.name == "ci" and set(p.scopes) == {"publish", "observe"}


def test_authenticate_missing_or_wrong_scheme():
    auth = TokenAuth(_store())
    for header in ("", "Basic xyz"):
        with pytest.raises(HTTPException) as e:
            auth.authenticate(header)
        assert e.value.status_code == 401


def test_authenticate_unknown_and_revoked():
    with pytest.raises(HTTPException) as e:
        TokenAuth(_store()).authenticate("Bearer wrong")
    assert e.value.status_code == 401
    with pytest.raises(HTTPException) as e:
        TokenAuth(_store(revoked=True)).authenticate("Bearer secret-token")
    assert e.value.status_code == 401


def test_require_scope_ok():
    p = require_scope("publish")(_req(TokenAuth(_store(("publish",))),
                                            "Bearer secret-token"))
    assert isinstance(p, Principal)


def test_require_scope_missing():
    with pytest.raises(HTTPException) as e:
        require_scope("publish")(_req(TokenAuth(_store(("observe",))),
                                            "Bearer secret-token"))
    assert e.value.status_code == 403


def test_scopes_constant():
    assert SCOPES == ("publish", "manage", "observe")
