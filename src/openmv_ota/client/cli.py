"""CLI handlers for ``openmv-ota client``.

    login / logout           save/remove the server URL + admin token
    publish                  upload a built release (+ optional rollout)
    rollout raise|pause|resume|rollback
    fleet / devices / audit  read fleet status

``login``/``logout`` need only the standard library; the API verbs use httpx from the ``server``
extra (via ``api.Api``).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import config
from .errors import ClientError


def _creds(p: argparse.ArgumentParser) -> None:
    p.add_argument("--server", help="server URL (else OPENMV_OTA_SERVER / saved profile)")
    p.add_argument("--token", help="admin token (else OPENMV_OTA_TOKEN / saved profile)")


def register(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="_subcommand")

    p_login = sub.add_parser("login", help="save the server URL + admin token")
    p_login.add_argument("--server", required=True, help="server base URL (https://...)")
    p_login.add_argument("--token", help="admin API token (else OPENMV_OTA_TOKEN, else stdin)")
    p_login.set_defaults(func=cmd_login, _command="client login")

    p_logout = sub.add_parser("logout", help="remove the saved server URL + token")
    p_logout.set_defaults(func=cmd_logout, _command="client logout")

    p_pub = sub.add_parser("publish", help="upload a built release (+ optional rollout)")
    p_pub.add_argument("project", nargs="?", default=".", help="project dir (default: .)")
    p_pub.add_argument("-b", "--board", required=True, help="board to publish")
    p_pub.add_argument("-o", "--output", help="artifact dir (default: <project>/build)")
    p_pub.add_argument("--rollout", metavar="COHORT:PCT",
                       help="create a rollout after publishing, e.g. beta:5 or 5")
    p_pub.add_argument("--allow-republish", action="store_true")
    _creds(p_pub)
    p_pub.set_defaults(func=cmd_publish, _command="client publish")

    p_ro = sub.add_parser("rollout", help="raise/pause/resume/rollback a rollout")
    rsub = p_ro.add_subparsers(dest="_ro")
    for action, needs_pct in (("raise", True), ("pause", False), ("resume", False),
                              ("rollback", False)):
        pr = rsub.add_parser(action)
        pr.add_argument("--id", required=True)
        if needs_pct:
            pr.add_argument("--percent", type=float, required=True)
        _creds(pr)
        pr.set_defaults(func=cmd_rollout, _command="client rollout " + action, action=action)

    p_co = sub.add_parser("cohort", help="list cohorts / assign devices to one")
    cosub = p_co.add_subparsers(dest="_co")
    p_col = cosub.add_parser("list")
    p_col.add_argument("--board-id", type=int)
    _creds(p_col)
    p_col.set_defaults(func=cmd_cohort, _command="client cohort list", action="list")
    p_coa = cosub.add_parser("assign")
    p_coa.add_argument("--cohort", required=True)
    p_coa.add_argument("--device", action="append", dest="devices", required=True, metavar="DEVICE_ID",
                       help="device id to assign (repeatable)")
    _creds(p_coa)
    p_coa.set_defaults(func=cmd_cohort, _command="client cohort assign", action="assign")

    for name, handler in (("fleet", cmd_fleet), ("devices", cmd_devices), ("audit", cmd_audit)):
        p = sub.add_parser(name, help="read %s status" % name)
        if name in ("fleet", "devices"):
            p.add_argument("--board-id", type=int)
        else:
            p.add_argument("--since", type=int, default=0)
        _creds(p)
        p.set_defaults(func=handler, _command="client " + name)


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


def _make_api(cfg):
    from .api import Api
    return Api(cfg)


def _parse_rollout(spec: str):
    cohort, _, pct = spec.rpartition(":")
    try:
        return (cohort or "__default__"), float(pct)
    except ValueError:
        raise ClientError("bad --rollout %r (want cohort:percent, e.g. beta:5)" % spec) from None


def cmd_publish(args: argparse.Namespace) -> int:
    try:
        cfg = config.resolve(args.server, args.token)
        out = Path(args.output) if args.output else Path(args.project) / "build"
        manifest = out / ("%s-manifest.bin" % args.board)
        image = out / ("%s-ota.img.gz" % args.board)
        delta = out / ("%s-ota.delta.gz" % args.board)
        if not manifest.exists() or not image.exists():
            raise ClientError("no built release for %s in %s -- run `build ota-romfs` first"
                              % (args.board, out))
        api = _make_api(cfg)
        res = api.publish_release(manifest.read_bytes(), image.read_bytes(),
                                  delta.read_bytes() if delta.exists() else None,
                                  args.allow_republish)
        print("published %s  version %s  (%s)" % (res["release_id"], res.get("version"),
                                                  ", ".join(res["representations"])))
        if args.rollout:
            cohort, pct = _parse_rollout(args.rollout)
            ro = api.create_rollout(res["release_id"], cohort, pct)
            print("rollout %s  %s%%  cohort=%s" % (ro["rollout_id"], ro["percent"], cohort))
    except ClientError as e:
        print("error: %s" % e, file=sys.stderr)
        return e.exit_code
    return 0


def cmd_rollout(args: argparse.Namespace) -> int:
    try:
        api = _make_api(config.resolve(args.server, args.token))
        if args.action == "raise":
            ro = api.patch_rollout(args.id, percent=args.percent)
        elif args.action == "pause":
            ro = api.patch_rollout(args.id, state="paused")
        elif args.action == "resume":
            ro = api.patch_rollout(args.id, state="active")
        else:
            ro = api.rollback_rollout(args.id)
        print("rollout %s -> %s" % (args.id, ro.get("state", "")))
    except ClientError as e:
        print("error: %s" % e, file=sys.stderr)
        return e.exit_code
    return 0


def cmd_cohort(args: argparse.Namespace) -> int:
    try:
        api = _make_api(config.resolve(args.server, args.token))
        if args.action == "list":
            print(json.dumps(api.list_cohorts(args.board_id), indent=2))
        else:
            res = api.assign_cohort(args.cohort, args.devices)
            print("assigned %d/%d device(s) to cohort %s"
                  % (res["assigned"], len(args.devices), res["cohort"]))
    except ClientError as e:
        print("error: %s" % e, file=sys.stderr)
        return e.exit_code
    return 0


def _read(args, call) -> int:
    try:
        print(json.dumps(call(_make_api(config.resolve(args.server, args.token))), indent=2))
    except ClientError as e:
        print("error: %s" % e, file=sys.stderr)
        return e.exit_code
    return 0


def cmd_fleet(args: argparse.Namespace) -> int:
    return _read(args, lambda api: api.fleet(args.board_id))


def cmd_devices(args: argparse.Namespace) -> int:
    return _read(args, lambda api: api.devices(args.board_id))


def cmd_audit(args: argparse.Namespace) -> int:
    return _read(args, lambda api: api.audit(args.since))
