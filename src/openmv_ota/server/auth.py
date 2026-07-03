"""Admin API authentication -- hashed, scoped bearer tokens (the self-host default).

**Pluggable:** OpenMV's website injects its own auth via ``create_app(admin_auth=...)``. An auth
object implements ``authenticate(authorization_header) -> Principal`` and raises ``HTTPException``
on failure. ``require_scope`` is the FastAPI dependency the admin routes hang off.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from fastapi import HTTPException, Request

from .scopes import SCOPES

__all__ = ["SCOPES", "Principal", "TokenAuth", "hash_token", "require_scope"]


@dataclass(frozen=True)
class Principal:
    name: str
    scopes: list
    account_id: str = ""       # the account this admin credential acts for (the website injects it)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class TokenAuth:
    """The default: opaque bearer tokens stored hashed in the metastore."""

    def __init__(self, metastore):
        self._ms = metastore

    def authenticate(self, authorization: str) -> Principal:
        if not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="missing bearer token")
        row = self._ms.get_token(hash_token(authorization[len("Bearer "):].strip()))
        if row is None or row["revoked"]:
            raise HTTPException(status_code=401, detail="invalid token")
        return Principal(name=row["name"], scopes=row["scopes"])


def require_scope(scope: str):
    """A FastAPI dependency: authenticate the request, then require ``scope``."""
    def dep(request: Request) -> Principal:
        principal = request.app.state.admin_auth.authenticate(
            request.headers.get("Authorization", ""))
        if scope not in principal.scopes:
            raise HTTPException(status_code=403, detail="missing scope: %s" % scope)
        return principal
    return dep
