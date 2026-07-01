"""CLI handlers for ``openmv-ota client``.

    login    save the server URL + admin token
    logout   remove the saved profile

``publish`` / ``rollout`` / ``fleet`` / … (which call the admin API over httpx) land once the
server's admin API exists.
"""

from __future__ import annotations

import argparse
import os
import sys

from . import config


def register(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="_subcommand")

    p_login = sub.add_parser("login", help="save the server URL + admin token")
    p_login.add_argument("--server", required=True, help="server base URL (https://...)")
    p_login.add_argument("--token", help="admin API token (else OPENMV_OTA_TOKEN, else stdin)")
    p_login.set_defaults(func=cmd_login, _command="client login")

    p_logout = sub.add_parser("logout", help="remove the saved server URL + token")
    p_logout.set_defaults(func=cmd_logout, _command="client logout")


def cmd_login(args: argparse.Namespace) -> int:
    token = args.token or os.environ.get("OPENMV_OTA_TOKEN") or sys.stdin.readline().strip()
    if not token:
        print("error: no token (pass --token, set OPENMV_OTA_TOKEN, or pipe it on stdin)",
              file=sys.stderr)
        return 2
    print("saved %s" % config.save(args.server.rstrip("/"), token))
    return 0


def cmd_logout(args: argparse.Namespace) -> int:
    if config.remove():
        print("removed %s" % config.config_path())
    else:
        print("no saved profile")
    return 0
