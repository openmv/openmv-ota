"""CLI handlers for ``openmv-ota server``.

    check     validate the resolved settings (deploy preflight)
    migrate   apply pending metadata-store migrations
    init      migrate + one-shot bootstrap (persist the cohort salt) -- the container entrypoint

``run`` / ``token`` land as the backend is built out. Module-level imports stay stdlib-only so
this parses on a base install; the heavy deps are pulled in inside handlers, after
``require_server_extra`` turns a missing extra into a clear hint.
"""

from __future__ import annotations

import argparse
import secrets
import sys

from .errors import ServerError
from .scopes import ALL_SCOPES, SCOPES


def register(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="_subcommand")

    p_check = sub.add_parser("check", help="validate the resolved server settings (preflight)")
    p_check.set_defaults(func=cmd_check, _command="server check")

    p_migrate = sub.add_parser("migrate", help="apply pending metadata-store migrations")
    p_migrate.set_defaults(func=cmd_migrate, _command="server migrate")

    p_init = sub.add_parser("init", help="migrate + bootstrap (idempotent; the container entrypoint)")
    p_init.set_defaults(func=cmd_init, _command="server init")

    p_run = sub.add_parser("run", help="start the ASGI server (uvicorn)")
    p_run.add_argument("--host", help="bind host (default from settings / 0.0.0.0)")
    p_run.add_argument("--port", type=int, help="bind port (default $PORT / 8080)")
    p_run.set_defaults(func=cmd_run, _command="server run")

    p_token = sub.add_parser("token", help="manage admin API tokens")
    tsub = p_token.add_subparsers(dest="_token_cmd")
    p_ti = tsub.add_parser("issue", help="mint a scoped admin token (printed once)")
    p_ti.add_argument("--name", required=True)
    p_ti.add_argument("--scope", action="append", default=[], choices=ALL_SCOPES,
                      help="repeatable; default: all scopes")
    p_ti.add_argument("--account", default="",
                      help="account this token acts for (default: the implicit '' account)")
    p_ti.set_defaults(func=cmd_token_issue, _command="server token issue")
    p_tr = tsub.add_parser("revoke", help="revoke a token by its hash")
    p_tr.add_argument("token_hash")
    p_tr.set_defaults(func=cmd_token_revoke, _command="server token revoke")
    p_tl = tsub.add_parser("list", help="list admin tokens (hashes + scopes, never secrets)")
    p_tl.set_defaults(func=cmd_token_list, _command="server token list")
    p_trot = tsub.add_parser("rotate", help="issue a replacement token + revoke the old (by hash)")
    p_trot.add_argument("token_hash")
    p_trot.set_defaults(func=cmd_token_rotate, _command="server token rotate")

    p_acct = sub.add_parser("account", help="manage tenant accounts (self-host)")
    asub = p_acct.add_subparsers(dest="_account_cmd")
    p_ac = asub.add_parser("create", help="create an account + issue its first admin token")
    p_ac.add_argument("--name", required=True)
    p_ac.set_defaults(func=cmd_account_create, _command="server account create")
    p_al = asub.add_parser("list", help="list accounts")
    p_al.set_defaults(func=cmd_account_list, _command="server account list")


def cmd_check(args: argparse.Namespace) -> int:
    try:
        settings = _settings()
        missing = settings.missing()
        if missing:
            raise ServerError("missing required settings: %s" % ", ".join(missing), exit_code=2)
    except ServerError as e:
        print("error: %s" % e, file=sys.stderr)
        return e.exit_code
    for line in settings.summary():
        print(line)
    print("ok")
    return 0


def cmd_migrate(args: argparse.Namespace) -> int:
    try:
        store = _store(_settings())
    except ServerError as e:
        print("error: %s" % e, file=sys.stderr)
        return e.exit_code
    version = store.migrate()
    store.close()
    print("migrated to schema v%d" % version)
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    try:
        settings = _settings()
        store = _store(settings)
    except ServerError as e:
        print("error: %s" % e, file=sys.stderr)
        return e.exit_code
    version = _bootstrap(store, settings)
    _seed_admin_token(store, settings)
    store.close()
    print("initialized (schema v%d)" % version)
    return 0


def cmd_token_issue(args: argparse.Namespace) -> int:
    try:
        store = _open()
    except ServerError as e:
        print("error: %s" % e, file=sys.stderr)
        return e.exit_code
    from .auth import hash_token
    token = secrets.token_urlsafe(32)
    store.add_token(hash_token(token), args.name, args.scope or list(SCOPES),
                    account_id=args.account)
    store.close()
    print("token issued (store it now -- it is not recoverable):", file=sys.stderr)
    print(token)
    return 0


def cmd_account_create(args: argparse.Namespace) -> int:
    try:
        store = _open()
    except ServerError as e:
        print("error: %s" % e, file=sys.stderr)
        return e.exit_code
    from .auth import hash_token
    account_id = "acct_" + secrets.token_hex(8)
    token = secrets.token_urlsafe(32)
    store.add_account(account_id, args.name)
    store.add_token(hash_token(token), args.name, list(SCOPES), account_id=account_id)
    store.close()
    print("account created: %s" % account_id, file=sys.stderr)
    print("admin token (store it now -- it is not recoverable):", file=sys.stderr)
    print("%s %s" % (account_id, token))
    return 0


def cmd_account_list(args: argparse.Namespace) -> int:
    try:
        store = _open()
    except ServerError as e:
        print("error: %s" % e, file=sys.stderr)
        return e.exit_code
    for a in store.list_accounts():
        print("%s  %s" % (a["account_id"], a["name"]))
    store.close()
    return 0


def cmd_token_revoke(args: argparse.Namespace) -> int:
    try:
        store = _open()
    except ServerError as e:
        print("error: %s" % e, file=sys.stderr)
        return e.exit_code
    store.revoke_token(args.token_hash)
    store.close()
    print("revoked %s" % args.token_hash)
    return 0


def cmd_token_list(args: argparse.Namespace) -> int:
    try:
        store = _open()
    except ServerError as e:
        print("error: %s" % e, file=sys.stderr)
        return e.exit_code
    for t in store.list_tokens():
        print("%s  %-20s %-16s [%s]%s"
              % (t["token_hash"][:16], t["name"], t["account_id"] or "(unassigned)",
                 ",".join(t["scopes"]), "  REVOKED" if t["revoked"] else ""))
    store.close()
    return 0


def cmd_token_rotate(args: argparse.Namespace) -> int:
    try:
        store = _open()
    except ServerError as e:
        print("error: %s" % e, file=sys.stderr)
        return e.exit_code
    from .auth import hash_token
    old = store.get_token(args.token_hash)
    if old is None:
        store.close()
        print("error: no such token", file=sys.stderr)
        return 1
    token = secrets.token_urlsafe(32)
    store.add_token(hash_token(token), old["name"], old["scopes"], account_id=old["account_id"])
    store.revoke_token(args.token_hash)
    store.close()
    print("rotated (old revoked); store the new token now -- not recoverable:", file=sys.stderr)
    print(token)
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    try:
        settings = _settings()
        store = _store(settings)
    except ServerError as e:
        print("error: %s" % e, file=sys.stderr)
        return e.exit_code
    _bootstrap(store, settings)                  # migrate + seed the secret (safe if init already ran)
    from .app import create_app
    app = create_app(settings, metastore=store)
    _serve(app, args.host or settings.host, args.port or settings.port, settings.trusted_proxy_ips)
    return 0


def _bootstrap(store, settings) -> int:
    """Migrate + seed the server HMAC secret if unset. Idempotent."""
    version = store.migrate()
    if not store.get_meta("cohort_salt"):
        store.set_meta("cohort_salt", settings.cohort_salt or secrets.token_hex(16))
    return version


def _seed_admin_token(store, settings) -> None:
    """Seed a root admin token on first init: from ``ADMIN_BOOTSTRAP_TOKEN`` (silent) or a freshly
    generated one printed once. A no-op once any token exists."""
    if store.count_tokens() > 0:
        return
    from .auth import hash_token
    if settings.admin_bootstrap_token:
        store.add_token(hash_token(settings.admin_bootstrap_token), "bootstrap", list(ALL_SCOPES))
        return
    token = secrets.token_urlsafe(32)
    store.add_token(hash_token(token), "bootstrap", list(ALL_SCOPES))
    print("admin bootstrap token (store it now): %s" % token, file=sys.stderr)


def _serve(app, host, port, forwarded_allow_ips):  # pragma: no cover  (blocks; seam monkeypatched)
    import uvicorn
    # proxy_headers + forwarded_allow_ips let X-Forwarded-For set request.client.host behind a proxy,
    # so the per-IP rate limiter sees the real client, not the proxy's single address.
    uvicorn.run(app, host=host, port=port, proxy_headers=True, forwarded_allow_ips=forwarded_allow_ips)


def _settings():
    from ._extras import require_server_extra
    require_server_extra()
    from .settings import ServerSettings
    return ServerSettings()


def _store(settings):
    from .metastore import build_metastore
    return build_metastore(settings)


def _open():
    """Resolve settings + open the metastore, ensuring the schema (idempotent) -- for the token
    ops, which assume `server init`/`migrate` has run but shouldn't fail if it hasn't."""
    store = _store(_settings())
    store.migrate()
    return store
