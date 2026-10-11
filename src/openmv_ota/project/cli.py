"""CLI handlers for the ``openmv-ota project`` command group.

    new      peg a project to a local firmware checkout
    setup    reconstruct the pinned checkout + SDK from the committed lock
    show     print the resolved snapshot
    status   check the lock against the current checkout (drift)
    sync     re-resolve and rewrite the lock
    keys     OTA signing-key status / rotation / revocation
    history  the project's append-only operations history
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import history
from . import lock as lock_mod
from . import project as proj
from .errors import ProjectError


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _path(value: str | None) -> Path | None:
    return Path(value) if value else None


def register(project_parser: argparse.ArgumentParser):
    sub = project_parser.add_subparsers(dest="_subcommand")

    p_new = sub.add_parser("new", help="peg a project to a local firmware checkout")
    p_new.add_argument("dir", nargs="?", default=".", help="project directory (default: .)")
    p_new.add_argument("-f", "--firmware", required=True, help="local OpenMV checkout path")
    p_new.add_argument("-b", "--board", action="append", required=True, metavar="NAME",
                       help="target board (repeatable)")
    p_new.add_argument("--product", help="product name (defaults to the directory name)")
    p_new.add_argument("--vendor", help="vendor name")
    p_new.add_argument("--sdk-home", help="SDK install dir (default ~/openmv-sdk-<SDK_VERSION>)")
    p_new.add_argument("--install-sdk", action="store_true", help="download + install the SDK if missing")
    p_new.add_argument("--allow-dirty", action="store_true", help="don't warn on a dirty checkout")
    p_new.add_argument("--ota", action="store_true",
                       help="over-the-air project: two A/B slots per partition (one slot, "
                            "single-image, where a partition cannot hold two)")
    p_new.add_argument("--ca", metavar="PEM",
                       help="TLS roots the device trusts for OTA downloads (a PEM file, copied "
                            "into the project as certs/ and frozen into the firmware). Unset: "
                            "the public bundle where every board's firmware fits it (~186 KB; "
                            "N6, AE3, RT1060), else the hosted OpenMV Cloud's roots (a few KB) "
                            "as certs/root.pem -- self-hosting, pass your server's root here")
    p_new.add_argument("--sig-alg", choices=("ES256", "ES384", "ES512"), default="ES256",
                       help="OTA signature algorithm (default ES256 / P-256)")
    p_new.add_argument("--ota-keys", type=int, default=32, metavar="N",
                       help="OTA rotation-pool size to provision (default 32)")
    p_new.add_argument("--factory-keys", type=int, default=8, metavar="N",
                       help="factory-key reserve to provision, one per site (default 8)")
    p_new.add_argument("--force", action="store_true", help="overwrite an existing project")
    p_new.add_argument("--key-passphrase-file", metavar="FILE",
                       help="passphrase (from a file, else a confirmed prompt on a terminal) to "
                            "encrypt the signing keys at rest; keys "
                            "are never stored plaintext")
    p_new.add_argument("--dev", action="store_true",
                       help="throwaway dev keys: a random cached passphrase (keys/.dev-passphrase), "
                            "no passphrase to manage -- the production build rail refuses these")
    p_new.add_argument("--keys-from", metavar="PROJECT",
                       help="share another project's signing and payload keys instead of minting "
                            "a fresh set. Needed when cameras move between products (a fleet built "
                            "with product_id 0): a camera verifies against the keys in its "
                            "FIRMWARE, which is never replaced over the air, so every project it "
                            "can be moved between has to sign with the same ones")
    p_new.set_defaults(func=cmd_new, _command="project new")

    p_setup = sub.add_parser("setup", help="reconstruct the pinned checkout + SDK")
    p_setup.add_argument("dir", nargs="?", default=".", help="project directory (default: .)")
    p_setup.add_argument("--cache", help="clone cache dir (default: $OPENMV_OTA_CACHE / platform)")
    p_setup.add_argument("--sdk-home", help="SDK install dir override")
    p_setup.add_argument("--no-install-sdk", dest="install_sdk", action="store_false",
                         default=True, help="don't download + install the SDK after cloning")
    p_setup.set_defaults(func=cmd_setup, _command="project setup")

    p_show = sub.add_parser("show", help="print the resolved snapshot")
    p_show.add_argument("dir", nargs="?", default=".", help="project directory (default: .)")
    p_show.add_argument("--json", action="store_true", help="dump the raw lock JSON")
    p_show.set_defaults(func=cmd_show, _command="project show")

    p_status = sub.add_parser("status", help="check the lock against the current checkout")
    p_status.add_argument("dir", nargs="?", default=".", help="project directory (default: .)")
    p_status.add_argument("-f", "--firmware", help="checkout path override")
    p_status.add_argument("-q", "--quiet", action="store_true", help="exit code only")
    p_status.set_defaults(func=cmd_status, _command="project status")

    p_verify = sub.add_parser("verify", help="fail if the firmware no longer matches the lock")
    p_verify.add_argument("dir", nargs="?", default=".", help="project directory (default: .)")
    p_verify.add_argument("-f", "--firmware", help="checkout path override")
    p_verify.set_defaults(func=cmd_verify, _command="project verify")

    p_sync = sub.add_parser("sync", help="re-resolve and rewrite the lock")
    p_sync.add_argument("dir", nargs="?", default=".", help="project directory (default: .)")
    p_sync.add_argument("-f", "--firmware", help="checkout path override")
    p_sync.add_argument("--sdk-home", help="SDK install dir override")
    p_sync.add_argument("--install-sdk", action="store_true", help="download + install the SDK if missing")
    p_sync.add_argument("--allow-dirty", action="store_true", help="don't warn on a dirty checkout")
    p_sync.set_defaults(func=cmd_sync, _command="project sync")

    p_keys = sub.add_parser("keys", help="OTA signing-key status / rotation / revocation")
    keys_sub = p_keys.add_subparsers(dest="_keys_action", required=True)

    p_ks = keys_sub.add_parser("status", help="show the signing key + pool usage")
    p_ks.add_argument("dir", nargs="?", default=".", help="project directory (default: .)")
    p_ks.set_defaults(func=cmd_keys_status, _command="project keys status")

    p_kr = keys_sub.add_parser("rotate", help="advance to the next OTA signing key")
    p_kr.add_argument("dir", nargs="?", default=".", help="project directory (default: .)")
    p_kr.set_defaults(func=cmd_keys_rotate, _command="project keys rotate")

    p_krev = keys_sub.add_parser("revoke", help="mark a compromised key revoked (reversible)")
    p_krev.add_argument("key_id", type=lambda s: int(s, 0), help="key id (e.g. 0x0100 or 256)")
    p_krev.add_argument("dir", nargs="?", default=".", help="project directory (default: .)")
    p_krev.set_defaults(func=cmd_keys_revoke, _command="project keys revoke")

    p_kun = keys_sub.add_parser("unrevoke", help="clear a key's revoked flag")
    p_kun.add_argument("key_id", type=lambda s: int(s, 0), help="key id (e.g. 0x0100 or 256)")
    p_kun.add_argument("dir", nargs="?", default=".", help="project directory (default: .)")
    p_kun.set_defaults(func=cmd_keys_unrevoke, _command="project keys unrevoke")

    p_kd = keys_sub.add_parser("desk", help="desk keys: what registers cameras at a bench or "
                                            "factory station")
    kd_sub = p_kd.add_subparsers(dest="_desk_action", required=True)
    p_kdc = kd_sub.add_parser("create", help="make a key for a desk (expires in --days)")
    p_kdc.add_argument("label", help="which desk, e.g. bench-1 or factory-a")
    p_kdc.add_argument("--days", type=int, default=90, help="days until it expires (default 90)")
    p_kdc.add_argument("--key-passphrase-file", help="file holding the project passphrase")
    p_kdc.add_argument("dir", nargs="?", default=".", help="project directory (default: .)")
    p_kdc.set_defaults(func=cmd_desk_create, _command="project keys desk create")
    p_kdl = kd_sub.add_parser("list", help="list the desk keys")
    p_kdl.add_argument("dir", nargs="?", default=".", help="project directory (default: .)")
    p_kdl.set_defaults(func=cmd_desk_list, _command="project keys desk list")
    p_kdr = kd_sub.add_parser("renew", help="push a desk key's expiry out --days from now")
    p_kdr.add_argument("key_id", type=int, help="desk key id")
    p_kdr.add_argument("--days", type=int, default=90, help="days from now (default 90)")
    p_kdr.add_argument("dir", nargs="?", default=".", help="project directory (default: .)")
    p_kdr.set_defaults(func=cmd_desk_renew, _command="project keys desk renew")
    p_kdv = kd_sub.add_parser("revoke", help="stop trusting a desk key, at once and for good")
    p_kdv.add_argument("key_id", type=int, help="desk key id")
    p_kdv.add_argument("dir", nargs="?", default=".", help="project directory (default: .)")
    p_kdv.set_defaults(func=cmd_desk_revoke, _command="project keys desk revoke")
    p_kdp = kd_sub.add_parser("push", help="send the desk keys' public halves to the server")
    p_kdp.add_argument("--server", help="server URL (default: client config)")
    p_kdp.add_argument("--token", help="a manage token (default: client config)")
    p_kdp.add_argument("dir", nargs="?", default=".", help="project directory (default: .)")
    p_kdp.set_defaults(func=cmd_desk_push, _command="project keys desk push")

    p_kk = keys_sub.add_parser("keykeeper", help="keykeeper's keys: what releases encrypted "
                                                 "for each camera are sealed to")
    kk_sub = p_kk.add_subparsers(dest="_keykeeper_action", required=True)
    p_kks = kk_sub.add_parser("sync", help="fetch keykeeper's chain from the server and verify "
                                           "it from this project's pin")
    p_kks.add_argument("--server", help="server URL (default: client config)")
    p_kks.add_argument("--token", help="an API token (default: client config)")
    p_kks.add_argument("dir", nargs="?", default=".", help="project directory (default: .)")
    p_kks.set_defaults(func=cmd_keykeeper_sync, _command="project keys keykeeper sync")

    p_kb = keys_sub.add_parser("backup",
                               help="archive the private keys (already encrypted at rest) to one file")
    p_kb.add_argument("dir", nargs="?", default=".", help="project directory (default: .)")
    p_kb.set_defaults(func=cmd_keys_backup, _command="project keys backup")

    p_krs = keys_sub.add_parser("restore", help="restore private keys from a backup archive")
    p_krs.add_argument("backup", help="the keys-backup.bin file")
    p_krs.add_argument("dir", nargs="?", default=".", help="project directory (default: .)")
    p_krs.set_defaults(func=cmd_keys_restore, _command="project keys restore")

    p_kbk = keys_sub.add_parser("backend",
                                help="inspect / configure external signing backends (HSM, KMS)")
    kbk_sub = p_kbk.add_subparsers(dest="_backend_action", required=True)

    p_kbs = kbk_sub.add_parser("show", help="list each key's signing backend")
    p_kbs.add_argument("dir", nargs="?", default=".", help="project directory (default: .)")
    p_kbs.set_defaults(func=cmd_keys_backend_show, _command="project keys backend show")

    p_kbc = kbk_sub.add_parser("configure",
                               help="point a trusted key at an external backend (bring your own key)")
    p_kbc.add_argument("key_id", type=lambda s: int(s, 0), help="key id (e.g. 0x0100 or 256)")
    p_kbc.add_argument("--backend", required=True,
                       choices=["encrypted-pem", "pkcs11", "aws-kms", "gcp-kms", "azure-kms",
                                "custom"],
                       help="backend tag")
    p_kbc.add_argument("--set", action="append", default=[], metavar="KEY=VALUE", dest="settings",
                       help="a backend record field (repeatable), e.g. --set uri=arn:aws:...")
    p_kbc.add_argument("dir", nargs="?", default=".", help="project directory (default: .)")
    p_kbc.set_defaults(func=cmd_keys_backend_configure, _command="project keys backend configure")

    p_kbp = kbk_sub.add_parser("provision",
                               help="generate a fresh key set inside an external backend (re-keys)")
    p_kbp.add_argument("--backend", required=True,
                       choices=["pkcs11", "aws-kms", "gcp-kms", "azure-kms"], help="backend tag")
    p_kbp.add_argument("--set", action="append", default=[], metavar="KEY=VALUE", dest="settings",
                       help="a backend config field (repeatable), e.g. --set pkcs11_module=/usr/...")
    p_kbp.add_argument("--ota-keys", type=int, default=4, metavar="N",
                       help="OTA keys to mint (default: 4 — external keys are often billable)")
    p_kbp.add_argument("--factory-keys", type=int, default=1, metavar="N",
                       help="factory keys to mint (default: 1)")
    p_kbp.add_argument("dir", nargs="?", default=".", help="project directory (default: .)")
    p_kbp.set_defaults(func=cmd_keys_backend_provision, _command="project keys backend provision")

    p_hist = sub.add_parser("history", help="print the project's operations history")
    p_hist.add_argument("dir", nargs="?", default=".", help="project directory (default: .)")
    p_hist.add_argument("-n", "--limit", type=int, default=0,
                        help="show only the most recent N events (default: all)")
    p_hist.set_defaults(func=cmd_history, _command="project history")

    return sub


def _warn(warnings: list[str]) -> None:
    for w in warnings:
        print("warning: %s" % w, file=sys.stderr)


def _read_passphrase(path: str) -> str:
    try:
        pw = Path(path).read_text(encoding="utf-8").strip()
    except OSError as e:
        raise ProjectError("can't read passphrase file %s: %s" % (path, e)) from None
    if not pw:
        raise ProjectError("passphrase file %s is empty" % path)
    return pw


def cmd_keys_backup(args: argparse.Namespace) -> int:
    try:
        out = proj.backup_private_keys(args.dir)
    except ProjectError as e:
        print("error: %s" % e, file=sys.stderr)
        return e.exit_code
    history.record(args.dir, "keys-backup", file=out.name)
    print("Wrote key backup: %s (the PEMs inside stay encrypted at rest)" % out)
    print("  MOVE IT OFF THIS MACHINE (a vault / offline drive) — a backup sitting next to "
          "the keys doesn't survive a lost laptop.")
    return 0


def cmd_keys_restore(args: argparse.Namespace) -> int:
    try:
        try:
            blob = Path(args.backup).read_bytes()
        except OSError as e:
            raise ProjectError("can't read backup %s: %s" % (args.backup, e)) from None
        names = proj.restore_private_keys(args.dir, blob)
    except ProjectError as e:
        print("error: %s" % e, file=sys.stderr)
        return e.exit_code
    history.record(args.dir, "keys-restore", count=len(names))
    print("Restored %d private key(s): %s" % (len(names), ", ".join(names)))
    return 0


def _parse_settings(pairs: list[str]) -> dict:
    record: dict = {}
    for pair in pairs:
        if "=" not in pair:
            raise ProjectError("--set expects KEY=VALUE, got %r" % pair)
        key, _, value = pair.partition("=")
        record[key.strip()] = value
    return record


def cmd_keys_backend_show(args: argparse.Namespace) -> int:
    from . import keys as keys_mod
    try:
        rows = keys_mod.backend_summary(Path(args.dir))
    except ProjectError as e:
        print("error: %s" % e, file=sys.stderr)
        return e.exit_code
    print("%-8s  %-8s  %s" % ("key_id", "role", "backend"))
    for key_id, role, backend in rows:
        print("0x%04x    %-8s  %s" % (key_id, role, backend))
    return 0


def cmd_keys_backend_configure(args: argparse.Namespace) -> int:
    from . import keys as keys_mod
    try:
        record = {"backend": args.backend, **_parse_settings(args.settings)}
        keys_mod.set_backend(Path(args.dir), args.key_id, record)
    except ProjectError as e:
        print("error: %s" % e, file=sys.stderr)
        return e.exit_code
    history.record(args.dir, "keys-backend-configure", key_id=args.key_id, backend=args.backend)
    print("Key 0x%04x now signs via the %s backend (keys/backends.json). The build verifies its "
          "public key matches keys/trusted_keys.json." % (args.key_id, args.backend))
    return 0


def cmd_keys_backend_provision(args: argparse.Namespace) -> int:
    from openmv_ota.ota import signer
    from openmv_ota.ota.errors import OtaError

    from . import keys as keys_mod
    try:
        record = {"backend": args.backend, **_parse_settings(args.settings)}
        provisioner = signer.build_provisioner(record)
        signing = keys_mod.provision_backend(
            Path(args.dir), provisioner, n_factory=args.factory_keys, n_ota=args.ota_keys)
    except (ProjectError, OtaError) as e:
        print("error: %s" % e, file=sys.stderr)
        return e.exit_code
    history.record(args.dir, "keys-backend-provision", backend=args.backend,
                   factory=args.factory_keys, ota=args.ota_keys)
    print("Provisioned %d factory + %d ota keys in the %s backend -> keys/trusted_keys.json + "
          "keys/backends.json (no private key on disk). Now signing with 0x%04x."
          % (args.factory_keys, args.ota_keys, args.backend, signing))
    print("IMPORTANT: this re-keyed the fleet — fielded devices trust the new keys only after a "
          "firmware update carrying the new trusted set. Commit both files.")
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    events = history.read(args.dir)
    if args.limit > 0:
        events = events[-args.limit:]
    if not events:
        print("no recorded operations (nothing built/signed yet, or no project here)")
        return 0
    for e in events:
        detail = "  ".join("%s=%s" % (k, e[k]) for k in sorted(e)
                           if k not in ("ts", "action"))
        print("%s  %-18s %s" % (e.get("ts", "?"), e.get("action", "?"), detail))
    return 0


def _new_key_passphrase(args: argparse.Namespace) -> str | None:
    """The passphrase `new --ota` encrypts the fresh key set under: the file when given,
    else a double-checked interactive prompt on a terminal. Never the environment -- an
    invisible source has no place at the moment the future key supply is sealed. Returns
    None when there is nothing to resolve (non-OTA, --dev, or no TTY -- the API then
    raises its explicit file-or-dev error)."""
    if args.key_passphrase_file:
        return _read_passphrase(args.key_passphrase_file)
    if args.ota and not args.dev and sys.stdin.isatty():  # pragma: no cover  (TTY-gated; the prompt itself is unit-tested)
        from .passphrase import prompt_new_passphrase  # pragma: no cover
        return prompt_new_passphrase()  # pragma: no cover
    return None


def cmd_new(args: argparse.Namespace) -> int:
    from openmv_ota.ota.algorithms import ES256, ES384, ES512

    sig_alg = {"ES256": ES256, "ES384": ES384, "ES512": ES512}[args.sig_alg]
    try:
        lock, warnings = proj.create_project(
            Path(args.dir),
            firmware=Path(args.firmware),
            boards=args.board,
            product=args.product,
            vendor=args.vendor,
            sdk_home_override=_path(args.sdk_home),
            install_sdk=args.install_sdk,
            allow_dirty=args.allow_dirty,
            force=args.force,
            ota=args.ota,
            sig_alg=sig_alg,
            ca=args.ca,
            ota_keys=args.ota_keys,
            factory_keys=args.factory_keys,
            keys_from=Path(args.keys_from) if args.keys_from else None,
            now=_now(),
            key_passphrase=_new_key_passphrase(args),
            dev=args.dev,
        )
    except ProjectError as e:
        print("error: %s" % e, file=sys.stderr)
        return e.exit_code
    _warn(warnings)
    history.record(args.dir, "project-new", when=_now(), boards=args.board, ota=args.ota)
    print("Created project in %s" % args.dir)
    print("Scaffolded app/ (main.py, settings.json with your app version).")
    if any(rb.get("role") == "coprocessor" for rb in lock.targets.get("resolved", [])):
        print("Scaffolded app-coprocessor/ for the helper core (plain romfs, written "
              "by the main core).")
    if args.ota:
        print("Provisioned %d factory + %d ota keys -> keys/trusted_keys.json "
              "(private keys gitignored in keys/private/)" % (args.factory_keys, args.ota_keys))
        if not args.dev:                        # dev keys are disposable; backup is refused
            try:
                out = proj.backup_private_keys(args.dir)
                history.record(args.dir, "keys-backup", file=out.name)
                print("Wrote a key backup: %s — MOVE IT OFF THIS MACHINE (a vault / offline "
                      "drive). Without the private keys you can never update this fleet "
                      "again." % out)
            except (ProjectError, OSError) as e:   # the project IS created -- don't fail it
                print("warning: key backup skipped (%s); run `openmv-ota project keys "
                      "backup` manually" % e, file=sys.stderr)
        print("Next: review [targets.*] in openmv-ota.toml (board_name, the "
              "auto-assigned product_id), and set your app version in app/settings.json.")
    _print_summary(lock)
    return 0


def cmd_setup(args: argparse.Namespace) -> int:
    try:
        repo = proj.setup_project(
            Path(args.dir),
            cache_override=args.cache,
            sdk_home_override=_path(args.sdk_home),
            install_sdk=args.install_sdk,
        )
    except ProjectError as e:
        print("error: %s" % e, file=sys.stderr)
        return e.exit_code
    print("Firmware ready at %s" % repo)
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    paths = proj.ProjectPaths(Path(args.dir))
    try:
        locked = lock_mod.read(paths.lock)
    except ProjectError as e:
        print("error: %s" % e, file=sys.stderr)
        return e.exit_code
    if args.json:
        print(json.dumps(locked.to_dict(), indent=2))
        return 0
    _print_summary(locked)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    try:
        changes = proj.status_project(Path(args.dir), firmware=_path(args.firmware), now=_now())
    except ProjectError as e:
        print("error: %s" % e, file=sys.stderr)
        return e.exit_code
    if not changes:
        if not args.quiet:
            print("in sync")
        return 0
    if not args.quiet:
        print("drift detected:")
        for c in changes:
            print("  %s" % c)
    return 1


def cmd_verify(args: argparse.Namespace) -> int:
    try:
        problems = proj.verify_locked(Path(args.dir), firmware=_path(args.firmware))
    except ProjectError as e:
        print("error: %s" % e, file=sys.stderr)
        return e.exit_code
    if not problems:
        print("verified: firmware matches the lock")
        return 0
    print("verification failed:", file=sys.stderr)
    for p in problems:
        print("  - %s" % p, file=sys.stderr)
    return 1


def cmd_sync(args: argparse.Namespace) -> int:
    try:
        lock, warnings = proj.sync_project(
            Path(args.dir),
            firmware=_path(args.firmware),
            sdk_home_override=_path(args.sdk_home),
            install_sdk=args.install_sdk,
            allow_dirty=args.allow_dirty,
            now=_now(),
        )
    except ProjectError as e:
        print("error: %s" % e, file=sys.stderr)
        return e.exit_code
    _warn(warnings)
    print("Re-locked %s" % args.dir)
    _print_summary(lock)
    return 0


def cmd_keys_status(args: argparse.Namespace) -> int:
    from . import keys as keys_mod
    try:
        st = keys_mod.key_status(Path(args.dir))
    except ProjectError as e:
        print("error: %s" % e, file=sys.stderr)
        return e.exit_code
    flag = "  (REVOKED - rotate before building)" if st.signer_revoked else ""
    print("signing key:  ota 0x%04x  (#%d of %d, %s)%s"
          % (st.signing_key_id, st.retired + 1, len(st.ota_ids), st.alg_name, flag))
    print("ota pool:     %d retired, %d remaining, %d revoked"
          % (st.retired, st.remaining, st.revoked))
    print("factory keys: %d" % len(st.factory_ids))
    print("private keys: %d of %d present on this machine" % (st.private_present, st.private_total))
    return 0


def cmd_keys_rotate(args: argparse.Namespace) -> int:
    from . import keys as keys_mod
    try:
        old, new, warnings = keys_mod.rotate_signing_key(Path(args.dir))
    except ProjectError as e:
        print("error: %s" % e, file=sys.stderr)
        return e.exit_code
    _warn(warnings)
    history.record(args.dir, "keys-rotate", old=old, new=new)
    print("Rotated OTA signing key: 0x%04x -> 0x%04x" % (old, new))
    print("Commit openmv-ota.toml to record the rotation.")
    return 0


def _desk_error(fn):
    """Run a desk-key or keykeeper command, turning a project or client error into its message."""
    from openmv_ota.client.errors import ClientError
    try:
        return fn()
    except (ProjectError, ClientError) as e:
        print("error: %s" % e, file=sys.stderr)
        return e.exit_code


def cmd_desk_create(args: argparse.Namespace) -> int:
    from . import desk_keys
    from .passphrase import resolve_passphrase

    def run():
        phrase, _ = resolve_passphrase(args.dir, passphrase_file=args.key_passphrase_file)
        e = desk_keys.create(args.dir, args.label, phrase, days=args.days)
        history.record(args.dir, "desk-key-create", key_id=e.key_id, label=e.label)
        print("Desk key %d for %r, expires %s." % (e.key_id, e.label, e.expires))
        print("  Commit keys/desk_keys.json, then `openmv-ota project keys desk push` so the")
        print("  server accepts the cameras this desk registers.")
        return 0
    return _desk_error(run)


def cmd_desk_list(args: argparse.Namespace) -> int:
    from . import desk_keys

    def run():
        keys = desk_keys.read(args.dir)
        if not keys:
            print("No desk keys -- `openmv-ota project keys desk create <label>` makes one.")
        for e in keys:
            state = "revoked" if e.revoked else "expires %s" % e.expires
            print("%3d  %-20s  %s  %s" % (e.key_id, e.label, state, e.pubkey[:16] + "..."))
        return 0
    return _desk_error(run)


def cmd_desk_renew(args: argparse.Namespace) -> int:
    from . import desk_keys

    def run():
        e = desk_keys.renew(args.dir, args.key_id, days=args.days)
        history.record(args.dir, "desk-key-renew", key_id=e.key_id, expires=e.expires)
        print("Desk key %d now expires %s. Push it: `openmv-ota project keys desk push`."
              % (e.key_id, e.expires))
        return 0
    return _desk_error(run)


def cmd_desk_revoke(args: argparse.Namespace) -> int:
    from . import desk_keys

    def run():
        e = desk_keys.revoke(args.dir, args.key_id)
        history.record(args.dir, "desk-key-revoke", key_id=e.key_id)
        print("Revoked desk key %d (%s). Push it now: `openmv-ota project keys desk push` --"
              % (e.key_id, e.label))
        print("  the server keeps accepting it until it knows. Cameras it registered must be")
        print("  registered again from another desk.")
        return 0
    return _desk_error(run)


def cmd_desk_push(args: argparse.Namespace) -> int:
    from openmv_ota.client import config as client_config
    from openmv_ota.client.api import Api

    from . import desk_keys

    def run():
        keys = desk_keys.read(args.dir)
        api = Api(client_config.resolve(args.server, args.token))
        for e in keys:
            api.put_desk_key(e)
        print("Pushed %d desk key%s." % (len(keys), "" if len(keys) == 1 else "s"))
        return 0
    return _desk_error(run)


def cmd_keykeeper_sync(args: argparse.Namespace) -> int:
    from openmv_ota.client import config as client_config
    from openmv_ota.client.api import Api

    from . import keykeeper_pins
    from .config import CONFIG_NAME, load_config

    def run():
        cfg = load_config(Path(args.dir) / CONFIG_NAME)
        chain = Api(client_config.resolve(args.server, args.token)).keykeeper_chain()
        gens = keykeeper_pins.sync(args.dir, cfg.keykeeper_pin, chain)
        print("Keykeeper chain verified: generations %d-%d; releases seal to the one in effect."
              % (gens[0].key_id, gens[-1].key_id))
        print("  Commit %s." % keykeeper_pins.path_for(args.dir).relative_to(args.dir))
        return 0
    return _desk_error(run)


def cmd_keys_revoke(args: argparse.Namespace) -> int:
    from . import keys as keys_mod
    try:
        key, changed, is_signer = keys_mod.revoke_key(Path(args.dir), args.key_id)
    except ProjectError as e:
        print("error: %s" % e, file=sys.stderr)
        return e.exit_code
    if not changed:
        print("Key 0x%04x is already revoked." % key.key_id)
        return 0
    history.record(args.dir, "keys-revoke", key_id=key.key_id, role=key.role)
    print("Revoked key 0x%04x (%s)." % (key.key_id, key.role))
    print("  Takes effect on the next firmware build; already-fielded devices keep")
    print("  trusting it until they update. Commit keys/trusted_keys.json.")
    print("  Undo with: openmv-ota project keys unrevoke 0x%04x" % key.key_id)
    if is_signer:
        print("warning: this was the current signing key - run "
              "`openmv-ota project keys rotate` before building.", file=sys.stderr)
    return 0


def cmd_keys_unrevoke(args: argparse.Namespace) -> int:
    from . import keys as keys_mod
    try:
        key, changed = keys_mod.unrevoke_key(Path(args.dir), args.key_id)
    except ProjectError as e:
        print("error: %s" % e, file=sys.stderr)
        return e.exit_code
    if changed:
        history.record(args.dir, "keys-unrevoke", key_id=key.key_id, role=key.role)
        print("Unrevoked key 0x%04x (%s). Commit keys/trusted_keys.json." % (key.key_id, key.role))
    else:
        print("Key 0x%04x is not revoked." % key.key_id)
    return 0


def _print_summary(lock: lock_mod.Lock) -> None:
    fw = lock.firmware
    mp = lock.micropython
    tc = lock.toolchain
    dirty = " (dirty)" if fw.get("dirty") else ""
    branch = fw.get("branch") or "detached"
    print("  mode:        %s" % ("OTA (A/B slots; single-image where a partition holds one)"
                                 if lock.ota else "no OTA (the image fills the partition)"))
    print("  firmware:    %s  commit %s%s" % (fw.get("version"), (fw.get("commit") or "")[:12], dirty))
    print("               branch %s  describe %s" % (branch, fw.get("describe")))
    print("  micropython: %s  (.mpy abi %s.%s)"
          % (mp.get("version"), mp.get("mpy_abi_version"), mp.get("mpy_sub_version")))
    print("  sdk:         %s" % lock.sdk.get("version"))
    print("  toolchain:   mpy-cross %s, vela %s, stedgeai %s"
          % (tc.get("mpy_cross", {}).get("version"),
             tc.get("vela", {}).get("version"),
             tc.get("stedgeai", {}).get("version")))
    for rb in lock.targets.get("resolved", []):
        npu = rb.get("npu") or "-"
        role = rb.get("role", "main")
        # The main partition shows its OTA front-slot size; a coprocessor partition is
        # a plain romfs (no slots), so front size doesn't apply.
        geom = ("front %s" % rb.get("front_size") if role == "main"
                else "plain romfs (slaved)")
        print("  %-18s part[%d] %-11s %s  %s  npu %s  (%s)"
              % (rb.get("name"), rb.get("partition_index"), role, rb.get("partition_size"),
                 geom, npu, rb.get("geometry_source")))
