"""Admin API authentication -- hashed, scoped bearer tokens (the self-host default).

**Pluggable:** OpenMV's website injects its own auth via ``create_app(admin_auth=...)``. An auth
object implements ``authenticate(authorization_header) -> Principal`` and raises ``HTTPException``
on failure. ``require_scope`` is the FastAPI dependency the admin routes hang off.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from fastapi import HTTPException, Request

from .scopes import SCOPES, allows, expand

__all__ = ["SCOPES", "Principal", "TokenAuth", "hash_token", "require_scope"]


@dataclass(frozen=True)
class Principal:
    name: str
    scopes: list
    account_id: str = ""       # the account this admin credential acts for (the website injects it)
    products: tuple = ()       # product ids this credential is limited to; () = the whole account

    def may(self, product_id) -> bool:
        """Whether this credential may touch ``product_id``.

        A token with no allow-list acts for the whole account, which is the ordinary
        case. A limited token is how a platform hands its own customer a credential for
        one product without giving away the fleet; `products` says what it may act on,
        `scopes` says what it may do."""
        return not self.products or int(product_id) in self.products

    def scoped(self):
        """The allow-list in the shape the metastore reads take: ``None`` for a token that
        sees the whole account, a list for one that does not. It is deliberately not the
        empty list for the unlimited case -- there, an empty list means "sees nothing"."""
        return list(self.products) or None


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
        # A deactivated account's token is refused here, not only revoked: one issued or
        # rotated while the account was being deactivated passed the active check before the
        # deactivate, and was minted after its revoke-all.
        if row is None or row["revoked"] or not row["account_active"]:
            raise HTTPException(status_code=401, detail="invalid token")
        return Principal(name=row["name"], scopes=expand(row["scopes"]),
                         account_id=row.get("account_id", "") or "",
                         products=tuple(row.get("products") or ()))


def require_scope(scope: str):
    """A FastAPI dependency: authenticate the request, then require ``scope``.

    The scope is stamped onto the returned function as ``openmv_scope``: the OpenAPI
    hook reads it back off the route to publish the bearer requirement and name the
    scope in the reference. Without that, a client generated from the schema sends no
    Authorization header at all, because this dependency reads the header by hand and
    FastAPI has nothing to infer a security scheme from."""
    def dep(request: Request) -> Principal:
        principal = request.app.state.admin_auth.authenticate(
            request.headers.get("Authorization", ""))
        if not allows(principal.scopes, scope):       # the ladder: publish implies manage, observe
            raise HTTPException(status_code=403, detail="missing scope: %s" % scope)
        return principal
    dep.openmv_scope = scope
    return dep


def require_account_scope(scope: str):
    """:func:`require_scope`, for a route that acts on the ACCOUNT and not on one product:
    its webhooks, its trust anchors and desk keys, its registrations, its cohorts. A token
    limited to a product list is the credential a platform hands its own customer, and it
    stops here with a 403 -- the product gate that keeps it in its lane has nothing to
    check on these routes."""
    inner = require_scope(scope)

    def dep(request: Request) -> Principal:
        principal = inner(request)
        if principal.products:
            raise HTTPException(status_code=403,
                                detail="this token is limited to a list of products; "
                                       "this route acts on the whole account")
        return principal
    dep.openmv_scope = scope
    return dep
