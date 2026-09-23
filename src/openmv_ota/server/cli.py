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
from .scopes import ALL_SCOPES, SCOPES, expand


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
    p_ti.add_argument("--name", required=True, help="label for the token, e.g. ci")
    p_ti.add_argument("--scope", action="append", default=[], choices=ALL_SCOPES,
                      help="the highest rung (implies the ones below); default: all scopes")
    p_ti.add_argument("--account-id", default="",
                      help="account this token acts for (default: the implicit '' account)")
    p_ti.set_defaults(func=cmd_token_issue, _command="server token issue")
    p_tr = tsub.add_parser("revoke", help="revoke a token by its hash")
    p_tr.add_argument("token_hash", help="hash from `token list` (never the secret)")
    p_tr.set_defaults(func=cmd_token_revoke, _command="server token revoke")
    p_tl = tsub.add_parser("list", help="list admin tokens (hashes + scopes, never secrets)")
    p_tl.set_defaults(func=cmd_token_list, _command="server token list")
    p_trot = tsub.add_parser("rotate", help="issue a replacement token + revoke the old (by hash)")
    p_trot.add_argument("token_hash", help="hash of the token to replace")
    p_trot.set_defaults(func=cmd_token_rotate, _command="server token rotate")

    p_acct = sub.add_parser("account", help="manage tenant accounts (self-host)")
    asub = p_acct.add_subparsers(dest="_account_cmd")
    p_ac = asub.add_parser("create", help="create an account + issue its first admin token")
    p_ac.add_argument("--name", required=True, help="human-readable account name")
    p_ac.set_defaults(func=cmd_account_create, _command="server account create")
    p_al = asub.add_parser("list", help="list accounts")
    p_al.set_defaults(func=cmd_account_list, _command="server account list")
    p_arn = asub.add_parser("rename", help="rename an account")
    p_arn.add_argument("--account-id", required=True, metavar="ACCOUNT_ID", help="account to rename")
    p_arn.add_argument("--name", required=True, help="the new name")
    p_arn.set_defaults(func=cmd_account_rename, _command="server account rename")
    p_ade = asub.add_parser("deactivate", help="revoke all tokens + disable an account")
    p_ade.add_argument("--account-id", required=True, metavar="ACCOUNT_ID",
                       help="account to deactivate (revokes all of its tokens)")
    p_ade.set_defaults(func=cmd_account_deactivate, _command="server account deactivate")
    p_adl = asub.add_parser("delete", help="delete a DEACTIVATED account and everything it "
                                           "owned (its audit history is kept)")
    p_adl.add_argument("--account-id", required=True, metavar="ACCOUNT_ID",
                       help="the account to delete; it must already be deactivated")
    p_adl.add_argument("--yes", action="store_true",
                       help="do it; without this the command only says what it would delete")
    p_adl.set_defaults(func=cmd_account_delete, _command="server account delete")
    p_aac = asub.add_parser("activate", help="re-enable an account")
    p_aac.add_argument("--account-id", required=True, metavar="ACCOUNT_ID",
                       help="account to re-enable (issue fresh tokens afterwards)")
    p_aac.set_defaults(func=cmd_account_activate, _command="server account activate")


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
    if store.token_name_in_use(args.account_id, args.name):
        store.close()
        print("error: token name already in use: %s" % args.name, file=sys.stderr)
        return 2
    token = secrets.token_urlsafe(32)
    store.add_token(hash_token(token), args.name, expand(args.scope or SCOPES),
                    account_id=args.account_id)
    store.close()
    print("token issued (store it now -- it is not recoverable):", file=sys.stderr)
    print(token)
    return 0


def _clean_name(store, name, except_id=None):
    """A non-empty, unique (case-insensitive) account name, or an error string. Shared by the
    account create + rename CLI verbs (mirrors the API's _clean_name)."""
    name = (name or "").strip()
    if not name:
        return None, "account name must not be empty"
    if store.account_name_exists(name, except_id):
        return None, "an account named %r already exists" % name
    return name, None


def cmd_account_create(args: argparse.Namespace) -> int:
    try:
        store = _open()
    except ServerError as e:
        print("error: %s" % e, file=sys.stderr)
        return e.exit_code
    name, err = _clean_name(store, args.name)
    if err:
        store.close()
        print("error: %s" % err, file=sys.stderr)
        return 1
    from .auth import hash_token
    account_id = "acct_" + secrets.token_hex(8)
    token = secrets.token_urlsafe(32)
    store.add_account(account_id, name)
    store.add_token(hash_token(token), name, list(SCOPES), account_id=account_id)
    store.close()
    print("account created: %s" % account_id, file=sys.stderr)
    print("working token (store it now -- it is not recoverable):", file=sys.stderr)
    print("%s %s" % (account_id, token))
    return 0


def cmd_account_list(args: argparse.Namespace) -> int:
    try:
        store = _open()
    except ServerError as e:
        print("error: %s" % e, file=sys.stderr)
        return e.exit_code
    for a in store.list_accounts():
        print("%s  %-20s %s" % (a["account_id"], a["name"], "" if a["active"] else "(inactive)"))
    store.close()
    return 0


def _account_action(args, do, ok):
    """Open the store, 404-guard the account, run ``do(store)``, print ``ok``. Shared by the
    account rename/deactivate/activate CLI verbs."""
    try:
        store = _open()
    except ServerError as e:
        print("error: %s" % e, file=sys.stderr)
        return e.exit_code
    if store.get_account(args.account_id) is None:
        store.close()
        print("error: no such account", file=sys.stderr)
        return 1
    msg = do(store)
    store.close()
    print(ok if msg is None else msg)
    return 0


def cmd_account_rename(args: argparse.Namespace) -> int:
    try:
        store = _open()
    except ServerError as e:
        print("error: %s" % e, file=sys.stderr)
        return e.exit_code
    if store.get_account(args.account_id) is None:
        store.close()
        print("error: no such account", file=sys.stderr)
        return 1
    name, err = _clean_name(store, args.name, except_id=args.account_id)
    if err:
        store.close()
        print("error: %s" % err, file=sys.stderr)
        return 1
    store.rename_account(args.account_id, name)
    store.close()
    print("renamed %s to %s" % (args.account_id, name))
    return 0


def cmd_account_deactivate(args: argparse.Namespace) -> int:
    def do(s):
        n = s.revoke_account_tokens(args.account_id)
        s.set_account_active(args.account_id, False)
        return "deactivated %s (%d token(s) revoked)" % (args.account_id, n)
    return _account_action(args, do, None)


def cmd_account_delete(args: argparse.Namespace) -> int:
    """The one hard delete the server has, and it lives only here: deactivate is the
    API's off-switch (a fielded fleet keeps being served), this removes a deactivated
    account's rows and artifacts for good. Rows go first, then the objects, so what a
    storage failure can leave behind is an orphaned blob, never a dangling release."""
    try:
        settings = _settings()
        store = _store(settings)
        store.migrate()
    except ServerError as e:
        print("error: %s" % e, file=sys.stderr)
        return e.exit_code
    acct = store.get_account(args.account_id)
    if acct is None:
        store.close()
        print("error: no such account", file=sys.stderr)
        return 1
    if acct.get("active"):
        store.close()
        print("error: %s is active; `server account deactivate` it first" % args.account_id,
              file=sys.stderr)
        return 1
    if not args.yes:
        store.close()
        print("would delete %s (%s) and everything it owns -- tokens, products, releases and "
              "their artifacts, rollouts, cohorts, devices, webhooks; its audit history is "
              "kept. Run again with --yes." % (args.account_id, acct.get("name", "")))
        return 1
    from .storage import build_storage
    storage = build_storage(settings)
    res = store.delete_account(args.account_id)
    store.close()
    removed = 0
    for key in res["keys"]:
        try:
            storage.delete(key)
            removed += 1
        except Exception as e:                      # noqa: BLE001 - reported, never fatal
            print("warning: artifact %s was not removed: %s" % (key, e), file=sys.stderr)
    rows = ", ".join("%d %s" % (n, t) for t, n in res["rows"].items() if n)
    print("deleted %s: %s; %d of %d artifact(s) removed"
          % (args.account_id, rows or "no rows", removed, len(res["keys"])))
    return 0


def cmd_account_activate(args: argparse.Namespace) -> int:
    return _account_action(args, lambda s: s.set_account_active(args.account_id, True),
                           "activated %s" % args.account_id)


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
    _schedule_advisory_scans(app, settings)
    _serve(app, args.host or settings.host, args.port or settings.port, settings.trusted_proxy_ips)
    return 0


def _schedule_advisory_scans(app, settings) -> None:
    """Arm the daily CVE-scan loop on the served app. Lives on the SERVE path,
    not in create_app: tests and the website's in-process mount create apps by
    the dozen and must not each spawn a sleeping task."""
    if settings.advisory_scan_interval_s <= 0:
        return

    @app.on_event("startup")
    async def _start():                          # pragma: no cover - loop plumbing
        import asyncio

        from . import advisor

        async def loop():
            while True:
                await asyncio.sleep(settings.advisory_scan_interval_s)
                for acct in app.state.metastore.list_accounts():
                    try:
                        await asyncio.to_thread(advisor.scan_account, app.state,
                                                acct["account_id"])
                    except Exception as e:       # noqa: BLE001 - keep the loop alive
                        print("advisory scan failed for %s: %s"
                              % (acct["account_id"], e), file=sys.stderr)

        app.state.advisory_task = asyncio.create_task(loop())


def _bootstrap(store, settings) -> int:
    """Migrate + seed the server HMAC secret if unset. Idempotent."""
    version = store.migrate()
    if not store.get_meta("capability_secret"):
        store.set_meta("capability_secret", settings.capability_secret or secrets.token_hex(16))
    return version


def _seed_admin_token(store, settings) -> None:
    """Seed the root admin token, and keep it in step with the environment.

    First init: from ``ADMIN_BOOTSTRAP_TOKEN`` (silent) or a freshly generated one, printed
    once. After that the variable stays authoritative for the token NAMED ``bootstrap``: a
    value that is not a live token rotates that row to it, revoking the old hash, so a
    changed or regenerated value -- a fresh Blueprint over a kept database, a restore, a
    deliberate rotation -- is a rotation, never a root the server silently refuses while
    the environment claims otherwise. Two things are left alone: a value that already IS a
    live token (whatever its name), and a ``bootstrap`` row an operator revoked on purpose,
    which is never resurrected."""
    from .auth import hash_token
    if store.count_tokens() == 0:
        if settings.admin_bootstrap_token:
            store.add_token(hash_token(settings.admin_bootstrap_token), "bootstrap",
                            list(ALL_SCOPES))
            return
        token = secrets.token_urlsafe(32)
        store.add_token(hash_token(token), "bootstrap", list(ALL_SCOPES))
        print("admin bootstrap token (store it now): %s" % token, file=sys.stderr)
        return
    if not settings.admin_bootstrap_token:
        return
    want = hash_token(settings.admin_bootstrap_token)
    row = store.get_token(want)
    if row is not None and not row["revoked"]:
        return                                   # the environment names a live token
    boots = [t for t in store.list_tokens("") if t["name"] == "bootstrap"]
    if row is not None or (boots and not any(not t["revoked"] for t in boots)):
        # the token the environment names, or the bootstrap root itself, was revoked
        # by hand: that decision stands (`server token issue` mints a new root)
        print("warning: OPENMV_OTA_ADMIN_BOOTSTRAP_TOKEN names a revoked token; leaving it "
              "revoked", file=sys.stderr)
        return
    live = [t for t in boots if not t["revoked"]]
    for t in live:
        store.revoke_token(t["token_hash"])
    store.add_token(want, "bootstrap", list(ALL_SCOPES))
    print("bootstrap token %s from OPENMV_OTA_ADMIN_BOOTSTRAP_TOKEN"
          % ("rotated" if live else "added"), file=sys.stderr)


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
