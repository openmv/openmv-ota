"""CLI handlers for ``openmv-ota client``.

    login / logout           save/remove the server URL + admin token
    release publish|list|show|sbom|bases|rename|artifact|manifest
    rollout create|raise|pause|resume|stop|status|list|rename
    cohort  list|assign|rename|delete|pin
    advisories list|scan
    device  list|show|pin|bind
    account / token          tenant + credential management
    fleet / audit            the account-wide reads

One verb per entity; every action lives under its entity.

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


def _list_flags(p: argparse.ArgumentParser, cols: str, paging: bool = True) -> None:
    """The list contract every `list` verb shares: --sort/--dir, --limit/--offset."""
    p.add_argument("--sort", metavar="COL", help="sort column: " + cols)
    p.add_argument("--dir", choices=("asc", "desc"), default=None, help="sort direction")
    if paging:
        p.add_argument("--limit", type=int, help="page size")
        p.add_argument("--offset", type=int, help="page offset")


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

    p_rel = sub.add_parser("release", help="publish / list / show / sbom / bases releases")
    rlsub = p_rel.add_subparsers(dest="_rel")
    p_pub = rlsub.add_parser("publish", help="upload a built release (+ optional rollout)")
    p_pub.add_argument("project", nargs="?", default=".", help="project dir (default: .)")
    p_pub.add_argument("-b", "--board", required=True, help="board to publish")
    p_pub.add_argument("-o", "--output", help="artifact dir (default: <project>/build)")
    # Staging at publish time uses the SAME flags as `rollout create` -- one syntax for
    # one concept. --percent is the trigger; --cohort refines it.
    p_pub.add_argument("--percent", type=float,
                       help="also stage the release: create a rollout at this percent")
    p_pub.add_argument("--cohort", default=None,
                       help="cohort for that rollout (default: __default__, the un-assigned devices)")
    p_pub.add_argument("--name", default="",
                       help="display name for the release (a label; rename any time)")
    p_pub.add_argument("--allow-republish", action="store_true",
                       help="re-upload a version the server already has (dev loop)")
    _creds(p_pub)
    p_pub.set_defaults(func=cmd_publish, _command="client release publish")
    p_rll = rlsub.add_parser("list", help="the publish history, newest first (JSON)")
    p_rll.add_argument("--product-id", type=int, help="only this product")
    _list_flags(p_rll, "version, product, size, uploaded, name, release")
    _creds(p_rll)
    p_rll.set_defaults(func=cmd_releases, _command="client release list")
    p_rsh = rlsub.add_parser("show", help="one release (JSON)")
    p_rsh.add_argument("--release-id", required=True, metavar="RELEASE_ID",
                       help="the release to read")
    _creds(p_rsh)
    p_rsh.set_defaults(func=cmd_release_show, _command="client release show")
    p_rsb = rlsub.add_parser("sbom", help="download a release's SBOM (CycloneDX JSON)")
    p_rsb.add_argument("--release-id", required=True, metavar="RELEASE_ID",
                       help="the release whose SBOM to fetch")
    p_rsb.add_argument("-o", "--output", help="write to this file (default: stdout)")
    _creds(p_rsb)
    p_rsb.set_defaults(func=cmd_release_sbom, _command="client release sbom")
    p_art = rlsub.add_parser("artifact", help="download one artifact (full image or a delta)")
    p_art.add_argument("--release-id", required=True, metavar="RELEASE_ID",
                       help="the release to read")
    p_art.add_argument("--filename", required=True,
                       help="artifact filename as `release show` lists it")
    p_art.add_argument("-o", "--output", help="write to this file (default: the filename)")
    _creds(p_art)
    p_art.set_defaults(func=cmd_release_artifact, _command="client release artifact")
    p_man = rlsub.add_parser("manifest", help="download the SIGNED manifest, byte-exact")
    p_man.add_argument("--release-id", required=True, metavar="RELEASE_ID",
                       help="the release to read")
    p_man.add_argument("-o", "--output", help="write to this file (default: manifest.bin)")
    _creds(p_man)
    p_man.set_defaults(func=cmd_release_manifest, _command="client release manifest")
    p_rrn = rlsub.add_parser("rename", help="set a release's display name (a label; --clear removes)")
    p_rrn.add_argument("--release-id", required=True, metavar="RELEASE_ID", help="release to rename")
    grn = p_rrn.add_mutually_exclusive_group(required=True)
    grn.add_argument("--name", help="the display name (max 64 chars)")
    grn.add_argument("--clear", action="store_true", help="remove the display name")
    _creds(p_rrn)
    p_rrn.set_defaults(func=cmd_release_rename, _command="client release rename")

    p_ro = sub.add_parser("rollout", help="create / drive / read rollouts")
    rsub = p_ro.add_subparsers(dest="_ro")
    p_rc = rsub.add_parser("create", help="stage an already-published release to a cohort")
    p_rc.add_argument("--release-id", required=True, metavar="RELEASE_ID",
                      help="release to stage (ids come from `client release list`)")
    p_rc.add_argument("--cohort", default="__default__",
                      help="cohort to stage it to (default: __default__, the un-assigned devices)")
    p_rc.add_argument("--percent", type=float, required=True,
                      help="share of the cohort to offer it to, 0-100")
    p_rc.add_argument("--failure-threshold", type=float, default=0.05,
                      help="fallback rate among offered devices that auto-pauses it (default 0.05)")
    p_rc.add_argument("--name", default="",
                      help="display name for the rollout (a label; rename any time)")
    _creds(p_rc)
    p_rc.set_defaults(func=cmd_rollout, _command="client rollout create", action="create")
    for action, needs_pct, blurb in (
            ("raise", True, "widen the rollout to --percent of the cohort"),
            ("pause", False, "stop offering it (it auto-pauses on failures too)"),
            ("resume", False, "start offering it again after a pause"),
            ("stop", False, "stop offering it for good; devices that took it keep it")):
        pr = rsub.add_parser(action, help=blurb)
        pr.add_argument("--rollout-id", required=True, metavar="ROLLOUT_ID",
                        help="the rollout to act on (ids come from `client rollout list`)")
        if needs_pct:
            # positional: the percent IS the action ("raise 50"), not a modifier of it
            pr.add_argument("percent", type=float,
                            help="share of the cohort to offer it to, 0-100")
        _creds(pr)
        pr.set_defaults(func=cmd_rollout, _command="client rollout " + action, action=action)
    p_rs = rsub.add_parser("status", help="a rollout's counters (attempted/updated/failures)")
    p_rs.add_argument("--rollout-id", required=True, metavar="ROLLOUT_ID",
                      help="the rollout to read (ids come from `client rollout list`)")
    _creds(p_rs)
    p_rs.set_defaults(func=cmd_rollout, _command="client rollout status", action="status")
    p_rol = rsub.add_parser("list", help="every rollout, newest first (JSON)")
    p_rol.add_argument("--product-id", type=int, help="only this product")
    p_rol.add_argument("--state", choices=("active", "paused", "stopped"),
                       help="only rollouts in this state")
    p_rol.add_argument("--cohort", help="only rollouts targeting this cohort")
    _list_flags(p_rol, "created, percent, state, cohort, product, name, devices, rollout")
    _creds(p_rol)
    p_rol.set_defaults(func=cmd_rollouts, _command="client rollout list")
    p_ron = rsub.add_parser("rename", help="set a rollout's display name (a label; --clear removes)")
    p_ron.add_argument("--rollout-id", required=True, metavar="ROLLOUT_ID", help="rollout to rename")
    gon = p_ron.add_mutually_exclusive_group(required=True)
    gon.add_argument("--name", help="the display name (max 64 chars)")
    gon.add_argument("--clear", action="store_true", help="remove the display name")
    _creds(p_ron)
    p_ron.set_defaults(func=cmd_rollout_rename, _command="client rollout rename")

    p_pr = sub.add_parser("product", help="the account's products")
    prsub = p_pr.add_subparsers(dest="_pr")
    p_prl = prsub.add_parser("list", help="every product: id, friendly name, device/release counts (JSON)")
    _creds(p_prl)
    p_prl.set_defaults(func=cmd_products, _command="client product list")

    p_co = sub.add_parser("cohort", help="list / create / assign / rename / delete / pin cohorts")
    cosub = p_co.add_subparsers(dest="_co")
    p_col = cosub.add_parser("list", help="list cohorts in use, with a device count each")
    p_col.add_argument("--product-id", type=int, help="only this product's cohorts")
    _list_flags(p_col, "cohort, devices, products, pins")
    _creds(p_col)
    p_col.set_defaults(func=cmd_cohort, _command="client cohort list", action="list")
    p_coc = cosub.add_parser("create", help="declare an empty cohort to assign devices into later")
    p_coc.add_argument("--cohort", required=True, help="the label to create")
    _creds(p_coc)
    p_coc.set_defaults(func=cmd_cohort, _command="client cohort create", action="create")
    p_coa = cosub.add_parser("assign", help="move devices into a cohort (by id, or a whole product)")
    p_coa.add_argument("--cohort", required=True, help="cohort to move the devices into")
    ga = p_coa.add_mutually_exclusive_group(required=True)   # surgical or bulk, exactly one
    ga.add_argument("--device-id", action="append", dest="devices", metavar="DEVICE_ID",
                    help="device id to assign (repeatable)")
    ga.add_argument("--product-id", type=int,
                    help="assign EVERY device of this product instead")
    _creds(p_coa)
    p_coa.set_defaults(func=cmd_cohort, _command="client cohort assign", action="assign")
    p_corn = cosub.add_parser("rename", help="relabel a cohort everywhere (devices, rollouts, pins)")
    p_corn.add_argument("--cohort", required=True, help="the label to rename")
    p_corn.add_argument("--name", required=True, help="the new label (must not be in use)")
    _creds(p_corn)
    p_corn.set_defaults(func=cmd_cohort, _command="client cohort rename", action="rename")
    p_cod = cosub.add_parser("delete", help="retire a label: devices return to __default__")
    p_cod.add_argument("--cohort", required=True, help="the label to delete")
    _creds(p_cod)
    p_cod.set_defaults(func=cmd_cohort, _command="client cohort delete", action="delete")
    p_cop = cosub.add_parser("pin", help="pin a whole cohort to a release (or --clear)")
    p_cop.add_argument("--product-id", type=int, required=True,
                       help="product the cohort belongs to")
    p_cop.add_argument("--cohort", required=True, help="cohort to pin")
    gc = p_cop.add_mutually_exclusive_group(required=True)
    gc.add_argument("--release-id", help="release to pin to")
    gc.add_argument("--clear", action="store_true", help="unpin")
    _creds(p_cop)
    p_cop.set_defaults(func=cmd_pin, _command="client cohort pin", target="cohort")

    p_bases = rlsub.add_parser("bases", help="download release images to build deltas from")
    p_bases.add_argument("-b", "--board", required=True,
                         help="board these bases are for (names the files)")
    p_bases.add_argument("--product-id", type=int,
                         help="only this product's releases (required with --fleet)")
    gb = p_bases.add_mutually_exclusive_group()
    gb.add_argument("--fleet", action="store_true",
                    help="fetch the bases the FLEET is actually running (asks the server's "
                         "fleet report) instead of the most recent releases")
    gb.add_argument("--last", type=int, default=3,
                    help="how many recent releases to fetch (default: 3)")
    p_bases.add_argument("-o", "--output", default="build/bases",
                         help="directory to write into (default: build/bases)")
    _creds(p_bases)
    p_bases.set_defaults(func=cmd_bases, _command="client release bases")


    p_dev = sub.add_parser("device", help="list / show / pin / bind devices")
    dsub = p_dev.add_subparsers(dest="_dev")
    p_dvl = dsub.add_parser("list", help="the per-device rows (JSON)")
    p_dvl.add_argument("--product-id", type=int, help="only this product")
    p_dvl.add_argument("--cohort", help="only devices in this cohort")
    p_dvl.add_argument("--not-cohort", dest="cohort_not", metavar="COHORT",
                       help="exclude devices in this cohort")
    p_dvl.add_argument("--q", metavar="TEXT", help="name-or-id substring, case-insensitive")
    _list_flags(p_dvl, "seen, device, product, version, cohort, first_seen")
    _creds(p_dvl)
    p_dvl.set_defaults(func=cmd_devices, _command="client device list")
    p_dvs = dsub.add_parser("show", help="one device (JSON)")
    p_dvs.add_argument("--device-id", required=True, metavar="DEVICE_ID",
                       help="the device to read")
    _creds(p_dvs)
    p_dvs.set_defaults(func=cmd_device_show, _command="client device show")
    p_dvp = dsub.add_parser("pin", help="pin ONE device to a release (or --clear to unpin)")
    p_dvp.add_argument("--device-id", required=True, metavar="DEVICE_ID", help="device to pin")
    gd = p_dvp.add_mutually_exclusive_group(required=True)
    gd.add_argument("--release-id", help="release to pin to")
    gd.add_argument("--clear", action="store_true", help="unpin")
    _creds(p_dvp)
    p_dvp.set_defaults(func=cmd_pin, _command="client device pin", target="device")
    p_dvn = dsub.add_parser("rename", help="set a device's display name (a label; --clear removes)")
    p_dvn.add_argument("--device-id", required=True, metavar="DEVICE_ID", help="device to rename")
    gn = p_dvn.add_mutually_exclusive_group(required=True)
    gn.add_argument("--name", help="the display name (max 64 chars)")
    gn.add_argument("--clear", action="store_true", help="remove the display name")
    _creds(p_dvn)
    p_dvn.set_defaults(func=cmd_device_rename, _command="client device rename")
    p_bind = dsub.add_parser("bind", help="bind a device to your account (re-account / recover)")
    p_bind.add_argument("--device-id", required=True, metavar="DEVICE_ID",
                        help="device to bind to the caller's account")
    _creds(p_bind)
    p_bind.set_defaults(func=cmd_bind, _command="client device bind")

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
    p_acr.add_argument("--account-id", required=True, metavar="ACCOUNT_ID", help="account to rename")
    p_acr.add_argument("--name", required=True, help="the new name")
    _creds(p_acr)
    p_acr.set_defaults(func=cmd_account, _command="client account rename", action="rename")
    p_acd = acsub.add_parser("deactivate", help="revoke all tokens + disable an account")
    p_acd.add_argument("--account-id", required=True, metavar="ACCOUNT_ID",
                       help="account to deactivate (revokes all of its tokens)")
    _creds(p_acd)
    p_acd.set_defaults(func=cmd_account, _command="client account deactivate", action="deactivate")
    p_aca = acsub.add_parser("activate", help="re-enable an account (issue fresh tokens after)")
    p_aca.add_argument("--account-id", required=True, metavar="ACCOUNT_ID",
                       help="account to re-enable (issue fresh tokens afterwards)")
    _creds(p_aca)
    p_aca.set_defaults(func=cmd_account, _command="client account activate", action="activate")

    p_tok = sub.add_parser("token", help="manage account API tokens (needs accounts scope)")
    tksub = p_tok.add_subparsers(dest="_tok")
    p_tki = tksub.add_parser("issue", help="issue a token for an account")
    p_tki.add_argument("--account-id", required=True, metavar="ACCOUNT_ID",
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
    p_tkl.add_argument("--account-id", required=True, metavar="ACCOUNT_ID",
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

    p_adv = sub.add_parser("advisories", help="CVE findings from SBOM scans")
    advsub = p_adv.add_subparsers(dest="_adv")
    p_advl = advsub.add_parser("list", help="the account's findings (JSON)")
    p_advl.add_argument("--release-id", metavar="RELEASE_ID", help="only this release")
    _list_flags(p_advl, "severity, advisory, component, release, first_seen, last_seen")
    p_advl.add_argument("--all", action="store_true",
                        help="include cleared findings (the monitoring history)")
    _creds(p_advl)
    p_advl.set_defaults(func=cmd_advisories, _command="client advisories list", action="list")
    p_advs = advsub.add_parser("scan", help="scan now (one release, or the live fleet)")
    p_advs.add_argument("--release-id", metavar="RELEASE_ID", help="only this release")
    _creds(p_advs)
    p_advs.set_defaults(func=cmd_advisories, _command="client advisories scan", action="scan")

    p_fl = sub.add_parser("fleet", help="the fleet summary (JSON)")
    p_fl.add_argument("--product-id", type=int, help="only this product")
    p_fl.add_argument("--cohort", help="only devices in this cohort")
    _creds(p_fl)
    p_fl.set_defaults(func=cmd_fleet, _command="client fleet")

    p_au = sub.add_parser("audit", help="the append-only audit log (JSON)")
    p_au.add_argument("--entity-id", metavar="ID",
                      help="only events for one release/rollout/device id")
    _list_flags(p_au, "when, action, actor, entity")
    p_au.add_argument("--not-action", dest="action_not", metavar="ACTION",
                      help="hide one action (e.g. advisory.scan)")
    p_au.add_argument("--since", type=int, default=0, metavar="SEQ",
                      help="only events after this sequence number (a cursor, not an offset)")
    _creds(p_au)
    p_au.set_defaults(func=cmd_audit, _command="client audit")


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
        if args.fleet:
            return _fleet_bases(args, api, out)
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


def _fleet_bases(args: argparse.Namespace, api, out: Path) -> int:
    """``release bases --fleet``: ask the server which (version, exact-bytes) bases the
    fleet is RUNNING (its check-ins report each slot's body sha) and download the stored
    release matching each -- the curated base set ``build ota-romfs --delta-from`` wants.
    The two groups no download can cover are named here, at fetch time: a version with
    no stored release (never published through the server), and a republish whose stored
    bytes differ from what devices run. Both are safe -- those devices take the full
    image -- but silence would read as covered."""
    import gzip

    from openmv_ota.ota import geometry
    from openmv_ota.ota.errors import OtaError
    from openmv_ota.ota.trailer import parse_trailer

    if args.product_id is None:
        raise ClientError("--fleet needs --product-id (whose fleet to ask; see `client fleet`)")
    rows = api.fleet_bases(args.product_id)["bases"]
    releases = {r["payload_version"]: r
                for r in api.releases(args.product_id, limit=500)["releases"]}
    got, lines = [], []
    for row in rows:
        if not row["body_sha256"]:
            continue                       # a sha-less device takes the full image by design
        rel = releases.get(row["payload_version"])
        if rel is None:
            print("warning: %d device(s) run %s but no stored release matches "
                  "-- they will take the full image" % (row["devices"], row["version"]),
                  file=sys.stderr)
            continue
        data = api.release_image(rel["release_id"])
        try:
            stored_sha = parse_trailer(
                gzip.decompress(data)[-geometry.control_block():]).body_sha256.hex()
        except OtaError:
            stored_sha = ""                      # unverifiable bytes can't cover anyone
        if stored_sha != row["body_sha256"]:
            print("warning: %d device(s) run %s with different bytes than the store "
                  "(republished version?) -- they will take the full image"
                  % (row["devices"], row["version"]), file=sys.stderr)
            continue
        path = out / ("%s%s%s.img.gz" % (args.board, BASE_PREFIX, rel["version"]))
        path.write_bytes(data)
        got.append({"path": str(path), "release_id": rel["release_id"],
                    "version": rel["version"], "devices": row["devices"],
                    "bytes": path.stat().st_size})
        lines.append("%s  (%s, %d device(s), %d bytes)"
                     % (path, rel["version"], row["devices"], path.stat().st_size))
    if not got:
        lines = ["no coverable fleet bases -- the next release ships full-image only"]
    return _emit(args, {"bases": got}, *lines)


def cmd_publish(args: argparse.Namespace) -> int:
    try:
        cfg = config.resolve(args.server, args.token)
        out = Path(args.output) if args.output else Path(args.project) / "build"
        manifest = out / ("%s-manifest.bin" % args.board)
        image = out / ("%s-ota.img.gz" % args.board)
        if not manifest.exists() or not image.exists():
            raise ClientError("no built release for %s in %s -- run `build ota-romfs` first"
                              % (args.board, out))
        if args.cohort is not None and args.percent is None:
            raise ClientError("--cohort stages a rollout only with --percent (how much of it)")
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
                                  sbom=sbom_bytes, display_name=args.name)
        lines = ["published %s  version %s  (%s)" % (res["release_id"], res.get("version"),
                                                     ", ".join(res["representations"]))]
        payload = dict(res)
        if sbom_bytes is not None:
            # Surface CVEs IMMEDIATELY -- publish succeeded either way, but the
            # maker should walk away knowing what the new release carries.
            try:
                api.scan_advisories(res["release_id"])
                # ACTIVE findings, not just this scan's news: the publish-time
                # background scan may have recorded them a moment earlier.
                adv = api.advisories(res["release_id"])["advisories"]
                payload["advisories"] = adv
                for f in adv:
                    lines.append("advisory: %s  %s  %s %s"
                                 % (f["vuln_id"], f.get("severity", "?"),
                                    f["component"], f.get("version", "")))
                if not adv:
                    lines.append("advisory scan: no known vulnerabilities")
            except ClientError as e:
                lines.append("advisory scan unavailable (%s)" % e)
        if args.percent is not None:
            ro = api.create_rollout(res["release_id"], args.cohort or "__default__", args.percent)
            lines.append("rollout %s  %s%%  cohort=%s"
                         % (ro["rollout_id"], ro["percent"], ro["cohort"]))
            # ONE object for one command: `publish --percent` is two API calls, and a caller
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
        if args.action == "create":
            ro = api.create_rollout(args.release_id, args.cohort, args.percent,
                                    failure_threshold=args.failure_threshold,
                                    display_name=args.name)
            return _emit(args, ro, "rollout %s  %s%%  cohort=%s"
                         % (ro["rollout_id"], ro["percent"], ro["cohort"]))
        if args.action == "status":
            print(json.dumps(api.rollout_status(args.rollout_id), indent=2))
            return 0
        if args.action == "raise":
            ro = api.patch_rollout(args.rollout_id, percent=args.percent)
        elif args.action == "pause":
            ro = api.patch_rollout(args.rollout_id, state="paused")
        elif args.action == "resume":
            ro = api.patch_rollout(args.rollout_id, state="active")
        else:
            ro = api.stop_rollout(args.rollout_id)
        return _emit(args, ro, "rollout %s -> %s" % (args.rollout_id, ro.get("state", "")))
    except ClientError as e:
        print("error: %s" % e, file=sys.stderr)
        return e.exit_code
    return 0


def cmd_cohort(args: argparse.Namespace) -> int:
    try:
        api = _make_api(config.resolve(args.server, args.token))
        if args.action == "list":
            print(json.dumps(api.list_cohorts(args.product_id, limit=args.limit,
                                              offset=args.offset, sort=args.sort,
                                              direction=args.dir), indent=2))
        elif args.action == "create":
            res = api.create_cohort(args.cohort)
            return _emit(args, res, "cohort %s created (no devices yet)" % res["cohort"])
        elif args.action == "rename":
            res = api.rename_cohort(args.cohort, args.name)
            return _emit(args, res, "cohort %s renamed to %s (%d device(s), %d rollout(s), "
                         "%d pin(s))" % (res["renamed_from"], res["cohort"], res["devices"],
                                         res["rollouts"], res["pins"]))
        elif args.action == "delete":
            res = api.delete_cohort(args.cohort)
            return _emit(args, res, "cohort %s deleted (%d device(s) back to __default__, "
                         "%d pin(s) dropped)" % (res["cohort"], res["devices"], res["pins"]))
        elif args.devices is not None:
            res = api.assign_cohort(args.cohort, device_ids=args.devices)
            return _emit(args, res, "assigned %d/%d device(s) to cohort %s"
                         % (res["assigned"], len(args.devices), res["cohort"]))
        else:
            res = api.assign_cohort(args.cohort, product_id=args.product_id)
            return _emit(args, res, "assigned %d device(s) (product %d) to cohort %s"
                         % (res["assigned"], args.product_id, res["cohort"]))
    except ClientError as e:
        print("error: %s" % e, file=sys.stderr)
        return e.exit_code
    return 0


def cmd_pin(args: argparse.Namespace) -> int:
    try:
        api = _make_api(config.resolve(args.server, args.token))
        release = None if args.clear else args.release_id
        if args.target == "device":
            res = api.pin_device(args.device_id, release)
            return _emit(args, res, "device %s pinned to %s"
                         % (args.device_id, res["pinned_release_id"] or "(unpinned)"))
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
        res = _make_api(config.resolve(args.server, args.token)).bind_device(args.device_id)
        return _emit(args, res,
                     "device %s bound to %s" % (args.device_id, _account_label(res["account_id"])))
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
                         "working token (store it now -- not recoverable): %s" % res["token"])
        elif args.action == "rename":
            res = api.rename_account(args.account_id, args.name)
            return _emit(args, res, "account %s renamed to %s" % (args.account_id, args.name))
        elif args.action == "deactivate":
            res = api.deactivate_account(args.account_id)
            return _emit(args, res, "account %s deactivated (%d token(s) revoked)"
                         % (args.account_id, res["tokens_revoked"]))
        elif args.action == "activate":
            res = api.activate_account(args.account_id)
            return _emit(args, res, "account %s activated" % args.account_id)
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
            res = api.issue_token(args.account_id, args.name, args.scope or None)
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
            print(json.dumps(api.list_account_tokens(args.account_id), indent=2))
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


def cmd_release_show(args: argparse.Namespace) -> int:
    return _read(args, lambda api: api.release(args.release_id))


def cmd_device_show(args: argparse.Namespace) -> int:
    return _read(args, lambda api: api.device(args.device_id))


def cmd_device_rename(args: argparse.Namespace) -> int:
    """Set (or --clear) the device's operator-facing display name -- a label
    for dashboards and lists; the device_id stays the identity everywhere."""
    name = "" if args.clear else args.name
    try:
        out = _make_api(config.resolve(args.server, args.token)).rename_device(
            args.device_id, name)
    except ClientError as e:
        print("error: %s" % e, file=sys.stderr)
        return e.exit_code
    if name:
        return _emit(args, out, "device %s named %r" % (args.device_id, name))
    return _emit(args, out, "device %s name cleared" % args.device_id)


def cmd_release_rename(args: argparse.Namespace) -> int:
    """Set (or --clear) a release's display name -- a label for dashboards and
    lists; the release_id stays the identity everywhere."""
    name = "" if args.clear else args.name
    try:
        out = _make_api(config.resolve(args.server, args.token)).rename_release(
            args.release_id, name)
    except ClientError as e:
        print("error: %s" % e, file=sys.stderr)
        return e.exit_code
    if name:
        return _emit(args, out, "release %s named %r" % (args.release_id, name))
    return _emit(args, out, "release %s name cleared" % args.release_id)


def cmd_rollout_rename(args: argparse.Namespace) -> int:
    """Set (or --clear) a rollout's display name -- same label rules as releases."""
    name = "" if args.clear else args.name
    try:
        out = _make_api(config.resolve(args.server, args.token)).rename_rollout(
            args.rollout_id, name)
    except ClientError as e:
        print("error: %s" % e, file=sys.stderr)
        return e.exit_code
    if name:
        return _emit(args, out, "rollout %s named %r" % (args.rollout_id, name))
    return _emit(args, out, "rollout %s name cleared" % args.rollout_id)


def cmd_advisories(args: argparse.Namespace) -> int:
    """Read or trigger CVE scans of the SBOMs the fleet still runs."""
    try:
        api = _make_api(config.resolve(args.server, args.token))
        if args.action == "scan":
            out = api.scan_advisories(args.release_id)
            lines = ["scanned %d release(s): %d finding(s), %d new"
                     % (out["releases_scanned"], out["findings"], len(out["new"]))]
            for f in out["new"]:
                lines.append("  NEW %s  %s  %s %s" % (f["vuln_id"], f.get("severity", "?"),
                                                      f["component"], f.get("version", "")))
            return _emit(args, out, *lines)
        print(json.dumps(api.advisories(args.release_id, active_only=not args.all,
                                        limit=args.limit, offset=args.offset,
                                        sort=args.sort, direction=args.dir), indent=2))
        return 0
    except ClientError as e:
        print("error: %s" % e, file=sys.stderr)
        return e.exit_code


def cmd_release_manifest(args: argparse.Namespace) -> int:
    """The signed manifest as published -- what a device actually verifies."""
    try:
        data = _make_api(config.resolve(args.server, args.token)).release_manifest(
            args.release_id)
        out = Path(args.output or "manifest.bin")
        out.write_bytes(data)
        return _emit(args, {"saved": str(out), "bytes": len(data)},
                     "saved %s (%d bytes)" % (out, len(data)))
    except ClientError as e:
        print("error: %s" % e, file=sys.stderr)
        return e.exit_code


def cmd_release_artifact(args: argparse.Namespace) -> int:
    """Download one artifact by the filename the manifest declares."""
    try:
        data = _make_api(config.resolve(args.server, args.token)).release_artifact(
            args.release_id, args.filename)
        out = Path(args.output or args.filename)
        out.write_bytes(data)
        return _emit(args, {"saved": str(out), "bytes": len(data)},
                     "saved %s (%d bytes)" % (out, len(data)))
    except ClientError as e:
        print("error: %s" % e, file=sys.stderr)
        return e.exit_code


def cmd_release_sbom(args: argparse.Namespace) -> int:
    """The release's SBOM, to stdout (pipeable into a scanner) or -o FILE."""
    try:
        data = _make_api(config.resolve(args.server, args.token)).release_sbom(args.release_id)
        if args.output:
            Path(args.output).write_bytes(data)
            return _emit(args, {"saved": args.output, "bytes": len(data)},
                         "saved %s (%d bytes)" % (args.output, len(data)))
        sys.stdout.write(data.decode("utf-8"))
        return 0
    except ClientError as e:
        print("error: %s" % e, file=sys.stderr)
        return e.exit_code


def cmd_fleet(args: argparse.Namespace) -> int:
    return _read(args, lambda api: api.fleet(args.product_id, cohort=args.cohort))


def cmd_products(args: argparse.Namespace) -> int:
    return _read(args, lambda api: api.products())


def cmd_devices(args: argparse.Namespace) -> int:
    return _read(args, lambda api: api.devices(args.product_id, cohort=args.cohort,
                                               limit=args.limit, offset=args.offset,
                                               sort=args.sort, direction=args.dir,
                                               q=args.q, cohort_not=args.cohort_not))


def cmd_releases(args: argparse.Namespace) -> int:
    return _read(args, lambda api: api.releases(args.product_id, limit=args.limit,
                                                offset=args.offset, sort=args.sort,
                                                direction=args.dir))


def cmd_rollouts(args: argparse.Namespace) -> int:
    return _read(args, lambda api: api.list_rollouts(args.product_id, limit=args.limit,
                                                     offset=args.offset, state=args.state,
                                                     cohort=args.cohort, sort=args.sort,
                                                     direction=args.dir))


def cmd_audit(args: argparse.Namespace) -> int:
    return _read(args, lambda api: api.audit(args.since, entity_id=args.entity_id,
                                             limit=args.limit, offset=args.offset,
                                             sort=args.sort, direction=args.dir,
                                             action_not=args.action_not))
