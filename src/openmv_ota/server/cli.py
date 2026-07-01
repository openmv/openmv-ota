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


def register(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="_subcommand")

    p_check = sub.add_parser("check", help="validate the resolved server settings (preflight)")
    p_check.set_defaults(func=cmd_check, _command="server check")

    p_migrate = sub.add_parser("migrate", help="apply pending metadata-store migrations")
    p_migrate.set_defaults(func=cmd_migrate, _command="server migrate")

    p_init = sub.add_parser("init", help="migrate + bootstrap (idempotent; the container entrypoint)")
    p_init.set_defaults(func=cmd_init, _command="server init")


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
    version = store.migrate()
    if not store.get_meta("cohort_salt"):        # stable per-device staged-% bucketing
        store.set_meta("cohort_salt", settings.cohort_salt or secrets.token_hex(16))
    store.close()
    print("initialized (schema v%d)" % version)
    return 0


def _settings():
    from ._extras import require_server_extra
    require_server_extra()
    from .settings import ServerSettings
    return ServerSettings()


def _store(settings):
    from .metastore import build_metastore
    return build_metastore(settings)
