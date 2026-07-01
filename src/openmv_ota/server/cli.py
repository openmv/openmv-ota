"""CLI handlers for ``openmv-ota server``.

    check   validate the resolved settings (deploy preflight)

``run`` / ``init`` / ``migrate`` / ``token`` land as the backend is built out. Module-level imports
stay stdlib-only so this parses on a base install; the heavy deps are pulled in inside handlers,
after ``require_server_extra`` turns a missing extra into a clear hint.
"""

from __future__ import annotations

import argparse
import sys

from .errors import ServerError


def register(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="_subcommand")

    p_check = sub.add_parser("check", help="validate the resolved server settings (preflight)")
    p_check.set_defaults(func=cmd_check, _command="server check")


def cmd_check(args: argparse.Namespace) -> int:
    from ._extras import require_server_extra
    try:
        require_server_extra()
        from .settings import ServerSettings
        settings = ServerSettings()
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
