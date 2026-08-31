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

from openmv_ota.server.scopes import ALL_SCOPES, SCOPES

from . import config
from .errors import ClientError


def _creds(p: argparse.ArgumentParser) -> None:
    p.add_argument("--server", help="server URL (else OPENMV_OTA_SERVER / saved profile)")
    p.add_argument("--token", help="admin token (else OPENMV_OTA_TOKEN / saved profile)")
    # THE READS HAVE ALWAYS PRINTED JSON (see `_read`); the writes printed prose only, so
    # publishing a release or issuing a token could not be scripted without parsing English.
    # `_creds` is on every verb that talks to the server, which makes it exactly the right
    # place for this -- the flag lands on the whole remote surface and nowhere else.
    p.add_argument("--json", action="store_true",
                   help="print the server's response as JSON instead of a summary")


def _emit(args: argparse.Namespace, payload, *lines: str) -> int:
    """The server's response as JSON under ``--json``, else the human summary.

    Prints the response VERBATIM rather than a re-rendering of it, so a field the summary does
    not bother to mention is still there for a caller that needs it -- the same reason the API's
    own schemas document without filtering."""
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2))
    else:
        for line in lines:
            print(line)
    return 0


def register(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="_subcommand")

    p_login = sub.add_parser("login", help="save the server URL + admin token")
    p_login.add_argument("--server",
                         help="server base URL (else OPENMV_OTA_SERVER; default: %s)"
                              % config.DEFAULT_SERVER_URL)
    p_login.add_argument("--token", help="admin API token (else OPENMV_OTA_TOKEN, else stdin)")
    p_login.add_argument("--json", action="store_true",
                         help="print the saved profile as JSON instead of a summary")
    p_login.set_defaults(func=cmd_login, _command="client login")

    p_logout = sub.add_parser("logout", help="remove the saved server URL + token")
    p_logout.add_argument("--json", action="store_true",
                          help="print what was removed as JSON instead of a summary")
    p_logout.set_defaults(func=cmd_logout, _command="client logout")

    p_pub = sub.add_parser("publish", help="upload a built release (+ optional rollout)")
    p_pub.add_argument("project", nargs="?", default=".", help="project dir (default: .)")
    p_pub.add_argument("-b", "--board", required=True, help="board to publish")
    p_pub.add_argument("-o", "--output", help="artifact dir (default: <project>/build)")
    p_pub.add_argument("--rollout", metavar="COHORT:PCT",
                       help="create a rollout after publishing, e.g. beta:5 or 5")
    p_pub.add_argument("--allow-republish", action="store_true",
                       help="re-upload a version the server already has (dev loop)")
    _creds(p_pub)
    p_pub.set_defaults(func=cmd_publish, _command="client publish")

    p_ro = sub.add_parser("rollout", help="raise/pause/resume/rollback a rollout")
    rsub = p_ro.add_subparsers(dest="_ro")
    for action, needs_pct, blurb in (
            ("raise", True, "widen the rollout to --percent of the cohort"),
            ("pause", False, "stop offering it (it auto-pauses on failures too)"),
            ("resume", False, "start offering it again after a pause"),
            ("rollback", False, "stop offering it for good; devices that took it keep it")):
        pr = rsub.add_parser(action, help=blurb)
        pr.add_argument("--id", required=True, metavar="ROLLOUT_ID",
                        help="the rollout to act on (`client rollout` ids come from publish)")
        if needs_pct:
            pr.add_argument("--percent", type=float, required=True,
                            help="share of the cohort to offer it to, 0-100")
        _creds(pr)
        pr.set_defaults(func=cmd_rollout, _command="client rollout " + action, action=action)

    p_co = sub.add_parser("cohort", help="list cohorts / assign devices to one")
    cosub = p_co.add_subparsers(dest="_co")
    p_col = cosub.add_parser("list", help="list cohorts in use, with a device count each")
    p_col.add_argument("--product-id", type=int, help="only this product's cohorts")
    _creds(p_col)
    p_col.set_defaults(func=cmd_cohort, _command="client cohort list", action="list")
    p_coa = cosub.add_parser("assign", help="move devices into a cohort")
    p_coa.add_argument("--cohort", required=True, help="cohort to move the devices into")
    p_coa.add_argument("--device", action="append", dest="devices", required=True, metavar="DEVICE_ID",
                       help="device id to assign (repeatable)")
    _creds(p_coa)
    p_coa.set_defaults(func=cmd_cohort, _command="client cohort assign", action="assign")

    p_bases = sub.add_parser("bases", help="download recent release images to build deltas from")
    p_bases.add_argument("-b", "--board", required=True,
                         help="board these bases are for (names the files)")
    p_bases.add_argument("--product-id", type=int, help="only this product's releases")
    p_bases.add_argument("--last", type=int, default=3,
                         help="how many recent releases to fetch (default: 3)")
    p_bases.add_argument("-o", "--output", default="build/bases",
                         help="directory to write into (default: build/bases)")
    _creds(p_bases)
    p_bases.set_defaults(func=cmd_bases, _command="client bases")

    p_prune = sub.add_parser("prune", help="delete a release's stored artifacts (keeps history)")
    p_prune.add_argument("--release", required=True, help="release id whose objects to delete")
    p_prune.add_argument("--force", action="store_true",
                         help="delete even while a rollout still offers this release")
    _creds(p_prune)
    p_prune.set_defaults(func=cmd_prune, _command="client prune")

    p_bind = sub.add_parser("bind", help="bind a device to your account (re-account / recover)")
    p_bind.add_argument("--id", required=True, metavar="DEVICE_ID",
                        help="device to bind to the caller's account")
    _creds(p_bind)
    p_bind.set_defaults(func=cmd_bind, _command="client bind")

    p_acct = sub.add_parser("account", help="create/list tenant accounts (needs accounts)")
    acsub = p_acct.add_subparsers(dest="_acct")
    p_acc = acsub.add_parser("create", help="create an account + get its first admin token")
    p_acc.add_argument("--name", required=True, help="human-readable account name")
    _creds(p_acc)
    p_acc.set_defaults(func=cmd_account, _command="client account create", action="create")
    p_acl = acsub.add_parser("list", help="list accounts")
    _creds(p_acl)
    p_acl.set_defaults(func=cmd_account, _command="client account list", action="list")
    p_acr = acsub.add_parser("rename", help="rename an account")
    p_acr.add_argument("--id", required=True, metavar="ACCOUNT_ID", help="account to rename")
    p_acr.add_argument("--name", required=True, help="the new name")
    _creds(p_acr)
    p_acr.set_defaults(func=cmd_account, _command="client account rename", action="rename")
    p_acd = acsub.add_parser("deactivate", help="revoke all tokens + disable an account")
    p_acd.add_argument("--id", required=True, metavar="ACCOUNT_ID",
                       help="account to deactivate (revokes all of its tokens)")
    _creds(p_acd)
    p_acd.set_defaults(func=cmd_account, _command="client account deactivate", action="deactivate")
    p_aca = acsub.add_parser("activate", help="re-enable an account (issue fresh tokens after)")
    p_aca.add_argument("--id", required=True, metavar="ACCOUNT_ID",
                       help="account to re-enable (issue fresh tokens afterwards)")
    _creds(p_aca)
    p_aca.set_defaults(func=cmd_account, _command="client account activate", action="activate")

    p_tok = sub.add_parser("token", help="manage account API tokens (needs accounts scope)")
    tksub = p_tok.add_subparsers(dest="_tok")
    p_tki = tksub.add_parser("issue", help="issue a token for an account")
    p_tki.add_argument("--account", required=True, metavar="ACCOUNT_ID",
                       help="account the token acts for")
    p_tki.add_argument("--name", required=True, help="label for the token, e.g. ci")
    # SAME `choices` AS `server token issue`. The API validates scopes against ALL_SCOPES and
    # 400s on an unknown one, so without this a typo ("observer") costs a round trip and comes
    # back as a server error, while the identical mistake on the server CLI is caught at the
    # prompt. scopes.py is dependency-free precisely so the client can import it on a base
    # install -- it just was not doing so.
    p_tki.add_argument("--scope", action="append", default=[], choices=ALL_SCOPES,
                       help="repeatable; default: the worker scopes (%s)" % ", ".join(SCOPES))
    _creds(p_tki)
    p_tki.set_defaults(func=cmd_token, _command="client token issue", action="issue")
    p_tkl = tksub.add_parser("list", help="list an account's tokens (metadata only, no secrets)")
    p_tkl.add_argument("--account", required=True, metavar="ACCOUNT_ID",
                       help="account whose tokens to list")
    _creds(p_tkl)
    p_tkl.set_defaults(func=cmd_token, _command="client token list", action="list")
    p_tkr = tksub.add_parser("revoke", help="revoke a token by its hash")
    p_tkr.add_argument("token_hash", help="hash from `token list` (never the secret)")
    _creds(p_tkr)
    p_tkr.set_defaults(func=cmd_token, _command="client token revoke", action="revoke")
    p_tkrot = tksub.add_parser("rotate", help="issue a replacement + revoke the old (by hash)")
    p_tkrot.add_argument("token_hash", help="hash of the token to replace")
    _creds(p_tkrot)
    p_tkrot.set_defaults(func=cmd_token, _command="client token rotate", action="rotate")

    p_pin = sub.add_parser("pin", help="pin a device/cohort to a release (overrides rollouts)")
    pinsub = p_pin.add_subparsers(dest="_pin")
    p_pd = pinsub.add_parser("device", help="pin ONE device to a release (or --clear to unpin)")
    p_pd.add_argument("--id", required=True, metavar="DEVICE_ID", help="device to pin")
    gd = p_pd.add_mutually_exclusive_group(required=True)
    gd.add_argument("--release", help="release id to pin to")
    gd.add_argument("--clear", action="store_true", help="unpin")
    _creds(p_pd)
    p_pd.set_defaults(func=cmd_pin, _command="client pin device", target="device")
    p_pc = pinsub.add_parser("cohort", help="pin a whole cohort to a release (or --clear)")
    p_pc.add_argument("--product-id", type=int, required=True,
                      help="product the cohort belongs to")
    p_pc.add_argument("--cohort", required=True, help="cohort to pin")
    gc = p_pc.add_mutually_exclusive_group(required=True)
    gc.add_argument("--release", help="release id to pin to")
    gc.add_argument("--clear", action="store_true", help="unpin")
    _creds(p_pc)
    p_pc.set_defaults(func=cmd_pin, _command="client pin cohort", target="cohort")

    for name, handler in (("fleet", cmd_fleet), ("devices", cmd_devices),
                          ("releases", cmd_releases), ("audit", cmd_audit)):
        p = sub.add_parser(name, help="read %s status" % name)
        if name in ("fleet", "devices", "releases"):
            p.add_argument("--product-id", type=int, help="only this product")
        else:
            p.add_argument("--since", type=int, default=0, metavar="SEQ",
                           help="only events after this sequence number (a cursor, not an offset)")
        if name in ("devices", "releases"):
            p.add_argument("--limit", type=int, help="page size")
            p.add_argument("--offset", type=int, help="page offset")
        if name == "devices":
            p.add_argument("--cohort", help="only devices in this cohort")
        _creds(p)
        p.set_defaults(func=handler, _command="client " + name)


def _account_label(account_id: str) -> str:
    """How an account_id reads in human output. '' is the sentinel for 'no account assigned'
    (unset firmware / a self-host that never made accounts) -- render it, never store a row for it."""
    return account_id or "(unassigned)"


def cmd_login(args: argparse.Namespace) -> int:
    # The same resolution every other verb uses, minus the profile (login WRITES it):
    # flag > OPENMV_OTA_SERVER > the hosted default. Requiring the flag here while the
    # rest of the surface honoured the env var meant a CI setup had the value and login
    # would not take it -- and a fresh pip install has neither, which is what the
    # hosted default is for.
    server = args.server or os.environ.get("OPENMV_OTA_SERVER") or config.DEFAULT_SERVER_URL
    token = args.token or os.environ.get("OPENMV_OTA_TOKEN") or sys.stdin.readline().strip()
    if not token:
        print("error: no token (pass --token, set OPENMV_OTA_TOKEN, or pipe it on stdin)",
              file=sys.stderr)
        return 2
    # login/logout are LOCAL (they write the saved profile, they do not call the API), but they
    # take --json anyway: a group where one verb speaks JSON and its neighbour does not is the
    # inconsistency this change exists to remove, and a setup script wants the path it wrote.
    path = config.save(server.rstrip("/"), token)
    return _emit(args, {"saved": str(path), "server": server.rstrip("/")},
                 "saved %s" % path)


def cmd_logout(args: argparse.Namespace) -> int:
    if config.remove():
        return _emit(args, {"removed": str(config.config_path())},
                     "removed %s" % config.config_path())
    return _emit(args, {"removed": None}, "no saved profile")


def _make_api(cfg):
    from .api import Api
    return Api(cfg)


def _parse_rollout(spec: str):
    cohort, _, pct = spec.rpartition(":")
    try:
        return (cohort or "__default__"), float(pct)
    except ValueError:
        raise ClientError("bad --rollout %r (want cohort:percent, e.g. beta:5)" % spec) from None


def _declared_deltas(manifest: Path, out: Path) -> dict:
    """The delta files this manifest declares, as ``{filename: bytes}``.

    Read from the SIGNED manifest rather than guessed from a filename pattern: a release now
    ships one delta per base version, so there is no single name to look for, and the manifest
    is the only authority on which artifacts belong to it. A declared file that is missing is
    an error here rather than a 400 from the server, because the fix is local."""
    from openmv_ota.ota.errors import OtaError
    from openmv_ota.ota.manifest import DELTA_FORMAT, parse_manifest

    try:
        body = parse_manifest(manifest.read_bytes()).body
    except OtaError as e:
        raise ClientError("unreadable manifest %s: %s" % (manifest, e)) from None
    deltas = {}
    for rep in body.get("representations", []):
        if rep.get("format") != DELTA_FORMAT:
            continue
        name = rep["url"].rsplit("/", 1)[-1]
        path = out / name
        if not path.exists():
            raise ClientError("%s declares delta %s but %s is missing -- rebuild with "
                              "`build ota-romfs`" % (manifest.name, name, path))
        deltas[name] = path.read_bytes()
    return deltas


BASE_PREFIX = "-base-"       # <board>-base-<version>.img.gz, what `build --delta-from <dir>` picks up


def cmd_bases(args: argparse.Namespace) -> int:
    """Download recent release images to build deltas FROM.

    A device patches against the release it is running, so a fleet mid-rollout needs one delta
    base per version still out there. Those bases are the published images, and the server
    keeps them -- so a build machine does not have to. This pulls the most recent N back into
    a directory that `build ota-romfs --delta-from <dir>` reads directly."""
    try:
        cfg = config.resolve(args.server, args.token)
        api = _make_api(cfg)
        out = Path(args.output)
        out.mkdir(parents=True, exist_ok=True)
        releases = api.releases(args.product_id, limit=args.last)["releases"]
        if not releases:
            raise ClientError("no retained releases to use as delta bases")
        # `bases` has no single API response to echo -- it WRITES files -- so the JSON is what
        # a caller actually needs from it: where each base landed and which release it is. A
        # build script chaining `bases` into `build ota-romfs --delta-from` reads exactly this.
        got, lines = [], []
        for rel in releases:
            path = out / ("%s%s%s.img.gz" % (args.board, BASE_PREFIX, rel["version"]))
            path.write_bytes(api.release_image(rel["release_id"]))
            got.append({"path": str(path), "release_id": rel["release_id"],
                        "version": rel["version"], "bytes": path.stat().st_size})
            lines.append("%s  (%s, %d bytes)" % (path, rel["version"], path.stat().st_size))
        return _emit(args, {"bases": got}, *lines)
    except ClientError as e:
        print("error: %s" % e, file=sys.stderr)
        return e.exit_code
    return 0


def cmd_prune(args: argparse.Namespace) -> int:
    """Delete a release's stored objects. The release row (audit + version history) stays."""
    try:
        cfg = config.resolve(args.server, args.token)
        res = _make_api(cfg).delete_release_artifacts(args.release, force=args.force)
        return _emit(args, res,
                     "deleted %d object(s) for %s" % (len(res["deleted"]), res["release_id"]))
    except ClientError as e:
        print("error: %s" % e, file=sys.stderr)
        return e.exit_code
    return 0


def cmd_publish(args: argparse.Namespace) -> int:
    try:
        cfg = config.resolve(args.server, args.token)
        out = Path(args.output) if args.output else Path(args.project) / "build"
        manifest = out / ("%s-manifest.bin" % args.board)
        image = out / ("%s-ota.img.gz" % args.board)
        if not manifest.exists() or not image.exists():
            raise ClientError("no built release for %s in %s -- run `build ota-romfs` first"
                              % (args.board, out))
        api = _make_api(cfg)
        # The SBOM is rendered fresh from the committed lock (deterministic, no firmware
        # checkout needed) and rides with the release -- dependency evidence beside the bytes
        # it describes. A project the renderer cannot read publishes without one, with a
        # warning: evidence is worth carrying, never worth blocking a release over.
        sbom_bytes = None
        try:
            from openmv_ota.build.sbom import render_sbom
            sbom_bytes = render_sbom(args.project).encode()
        except Exception as e:                                    # noqa: BLE001
            print("warning: no SBOM attached (%s)" % e, file=sys.stderr)
        res = api.publish_release(manifest.read_bytes(), image.read_bytes(),
                                  _declared_deltas(manifest, out), args.allow_republish,
                                  sbom=sbom_bytes)
        lines = ["published %s  version %s  (%s)" % (res["release_id"], res.get("version"),
                                                     ", ".join(res["representations"]))]
        payload = dict(res)
        if args.rollout:
            cohort, pct = _parse_rollout(args.rollout)
            ro = api.create_rollout(res["release_id"], cohort, pct)
            lines.append("rollout %s  %s%%  cohort=%s" % (ro["rollout_id"], ro["percent"], cohort))
            # ONE object for one command: `publish --rollout` is two API calls, and a caller
            # scripting it needs both ids. Nesting the rollout keeps the release fields where a
            # plain `publish` leaves them, so parsers do not need a special case.
            payload["rollout"] = ro
        return _emit(args, payload, *lines)
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
        return _emit(args, ro, "rollout %s -> %s" % (args.id, ro.get("state", "")))
    except ClientError as e:
        print("error: %s" % e, file=sys.stderr)
        return e.exit_code
    return 0


def cmd_cohort(args: argparse.Namespace) -> int:
    try:
        api = _make_api(config.resolve(args.server, args.token))
        if args.action == "list":
            print(json.dumps(api.list_cohorts(args.product_id), indent=2))
        else:
            res = api.assign_cohort(args.cohort, args.devices)
            return _emit(args, res, "assigned %d/%d device(s) to cohort %s"
                  % (res["assigned"], len(args.devices), res["cohort"]))
    except ClientError as e:
        print("error: %s" % e, file=sys.stderr)
        return e.exit_code
    return 0


def cmd_pin(args: argparse.Namespace) -> int:
    try:
        api = _make_api(config.resolve(args.server, args.token))
        release = None if args.clear else args.release
        if args.target == "device":
            res = api.pin_device(args.id, release)
            return _emit(args, res, "device %s pinned to %s"
                         % (args.id, res["pinned_release_id"] or "(unpinned)"))
        else:
            res = api.pin_cohort(args.product_id, args.cohort, release)
            return _emit(args, res, "cohort %s pinned to %s"
                         % (args.cohort, res["release_id"] or "(unpinned)"))
    except ClientError as e:
        print("error: %s" % e, file=sys.stderr)
        return e.exit_code
    return 0


def cmd_bind(args: argparse.Namespace) -> int:
    try:
        res = _make_api(config.resolve(args.server, args.token)).bind_device(args.id)
        return _emit(args, res,
                     "device %s bound to %s" % (args.id, _account_label(res["account_id"])))
    except ClientError as e:
        print("error: %s" % e, file=sys.stderr)
        return e.exit_code
    return 0


def cmd_account(args: argparse.Namespace) -> int:
    try:
        api = _make_api(config.resolve(args.server, args.token))
        if args.action == "create":
            res = api.create_account(args.name)
            # The secret is IN the JSON under --json, which is the point: this is the one moment
            # it exists, and a script that cannot capture it has to mint another account.
            return _emit(args, res, "account %s created" % res["account_id"],
                         "admin token (store it now -- not recoverable): %s" % res["token"])
        elif args.action == "rename":
            res = api.rename_account(args.id, args.name)
            return _emit(args, res, "account %s renamed to %s" % (args.id, args.name))
        elif args.action == "deactivate":
            res = api.deactivate_account(args.id)
            return _emit(args, res, "account %s deactivated (%d token(s) revoked)"
                         % (args.id, res["tokens_revoked"]))
        elif args.action == "activate":
            res = api.activate_account(args.id)
            return _emit(args, res, "account %s activated" % args.id)
        else:
            print(json.dumps(api.list_accounts(), indent=2))
    except ClientError as e:
        print("error: %s" % e, file=sys.stderr)
        return e.exit_code
    return 0


def cmd_token(args: argparse.Namespace) -> int:
    try:
        api = _make_api(config.resolve(args.server, args.token))
        if args.action == "issue":
            res = api.issue_token(args.account, args.name, args.scope or None)
            return _emit(args, res,
                         "token %s issued for %s" % (res["token_hash"][:16], res["account_id"]),
                         "token (store it now -- not recoverable): %s" % res["token"])
        elif args.action == "rotate":
            res = api.rotate_token(args.token_hash)
            return _emit(args, res, "rotated -> %s (old revoked)" % res["token_hash"][:16],
                         "token (store it now -- not recoverable): %s" % res["token"])
        elif args.action == "revoke":
            res = api.revoke_token(args.token_hash)
            return _emit(args, res, "revoked %s" % args.token_hash[:16])
        else:
            print(json.dumps(api.list_account_tokens(args.account), indent=2))
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
    return _read(args, lambda api: api.fleet(args.product_id))


def cmd_devices(args: argparse.Namespace) -> int:
    return _read(args, lambda api: api.devices(args.product_id, cohort=args.cohort,
                                               limit=args.limit, offset=args.offset))


def cmd_releases(args: argparse.Namespace) -> int:
    return _read(args, lambda api: api.releases(args.product_id, limit=args.limit,
                                                offset=args.offset))


def cmd_audit(args: argparse.Namespace) -> int:
    return _read(args, lambda api: api.audit(args.since))
