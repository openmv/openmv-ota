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
                       help="over-the-air project: halve each partition for a regular + golden image")
    p_new.add_argument("--sig-alg", choices=("ES256", "ES384", "ES512"), default="ES256",
                       help="OTA signature algorithm (default ES256 / P-256)")
    p_new.add_argument("--ota-keys", type=int, default=32, metavar="N",
                       help="OTA rotation-pool size to provision (default 32)")
    p_new.add_argument("--factory-keys", type=int, default=8, metavar="N",
                       help="factory-key reserve to provision, one per site (default 8)")
    p_new.add_argument("--force", action="store_true", help="overwrite an existing project")
    p_new.add_argument("--key-passphrase-file", metavar="FILE",
                       help="passphrase (from a file) to encrypt the signing keys at rest; keys "
                            "are never stored plaintext")
    p_new.add_argument("--dev", action="store_true",
                       help="throwaway dev keys: a random cached passphrase (keys/.dev-passphrase), "
                            "no passphrase to manage -- the production build rail refuses these")
    p_new.add_argument("--backup-passphrase-file", metavar="FILE",
                       help="auto-write an encrypted key backup using this passphrase (else a "
                            "reminder is printed for OTA projects)")
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

    p_kb = keys_sub.add_parser("backup", help="write an encrypted backup of the private keys")
    p_kb.add_argument("--passphrase-file", required=True, metavar="FILE",
                      help="file whose contents are the backup passphrase")
    p_kb.add_argument("dir", nargs="?", default=".", help="project directory (default: .)")
    p_kb.set_defaults(func=cmd_keys_backup, _command="project keys backup")

    p_krs = keys_sub.add_parser("restore", help="restore private keys from an encrypted backup")
    p_krs.add_argument("backup", help="the keys-backup.enc file")
    p_krs.add_argument("--passphrase-file", required=True, metavar="FILE",
                       help="file whose contents are the backup passphrase")
    p_krs.add_argument("dir", nargs="?", default=".", help="project directory (default: .)")
    p_krs.set_defaults(func=cmd_keys_restore, _command="project keys restore")

    p_ke = keys_sub.add_parser("encrypt",
                               help="encrypt an old project's plaintext private keys at rest")
    p_ke.add_argument("--key-passphrase-file", metavar="FILE",
                      help="passphrase (from a file) to encrypt under")
    p_ke.add_argument("--dev", action="store_true",
                      help="use a random cached dev passphrase (keys/.dev-passphrase) instead")
    p_ke.add_argument("dir", nargs="?", default=".", help="project directory (default: .)")
    p_ke.set_defaults(func=cmd_keys_encrypt, _command="project keys encrypt")

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
        out = proj.backup_private_keys(args.dir, _read_passphrase(args.passphrase_file))
    except ProjectError as e:
        print("error: %s" % e, file=sys.stderr)
        return e.exit_code
    history.record(args.dir, "keys-backup", file=out.name)
    print("Wrote encrypted key backup: %s" % out)
    print("  MOVE IT OFF THIS MACHINE (a vault / offline drive) — a backup sitting next to "
          "the keys doesn't survive a lost laptop.")
    return 0


def cmd_keys_restore(args: argparse.Namespace) -> int:
    try:
        pw = _read_passphrase(args.passphrase_file)
        try:
            blob = Path(args.backup).read_bytes()
        except OSError as e:
            raise ProjectError("can't read backup %s: %s" % (args.backup, e)) from None
        names = proj.restore_private_keys(args.dir, blob, pw)
    except ProjectError as e:
        print("error: %s" % e, file=sys.stderr)
        return e.exit_code
    history.record(args.dir, "keys-restore", count=len(names))
    print("Restored %d private key(s): %s" % (len(names), ", ".join(names)))
    return 0


def cmd_keys_encrypt(args: argparse.Namespace) -> int:
    import secrets

    from . import keys as keys_mod
    from . import passphrase as passphrase_mod
    try:
        if args.dev:
            passphrase = secrets.token_hex(16)
        elif args.key_passphrase_file:
            passphrase = _read_passphrase(args.key_passphrase_file)
        else:
            raise ProjectError("pass --key-passphrase-file (a real passphrase) or --dev")
        done = keys_mod.encrypt_private_keys(args.dir, passphrase)
        if args.dev:
            dp = passphrase_mod.dev_passphrase_path(args.dir)
            dp.write_text(passphrase, encoding="utf-8")
            dp.chmod(0o600)
    except ProjectError as e:
        print("error: %s" % e, file=sys.stderr)
        return e.exit_code
    history.record(args.dir, "keys-encrypt", count=len(done))
    if done:
        print("Encrypted %d plaintext key(s): %s" % (len(done), ", ".join(done)))
    else:
        print("No plaintext keys to encrypt (all already encrypted).")
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
            ota_keys=args.ota_keys,
            factory_keys=args.factory_keys,
            now=_now(),
            key_passphrase=(_read_passphrase(args.key_passphrase_file)
                            if args.key_passphrase_file else None),
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
        if args.backup_passphrase_file:
            try:
                out = proj.backup_private_keys(args.dir, _read_passphrase(args.backup_passphrase_file))
                history.record(args.dir, "keys-backup", file=out.name)
                print("Wrote an encrypted key backup: %s — MOVE IT OFF THIS MACHINE." % out)
            except ProjectError as e:   # the project IS created -- don't fail it over a backup
                print("warning: key backup skipped (%s); run `openmv-ota project keys "
                      "backup` manually" % e, file=sys.stderr)
        else:
            print("IMPORTANT: back up your signing keys off-machine now — "
                  "`openmv-ota project keys backup --passphrase-file <file>`. Without them you "
                  "can never update this fleet again.")
        print("Next: set product_id per board in openmv-ota.toml, and your app "
              "version in app/settings.json.")
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
    print("  mode:        %s" % ("OTA (partition split into regular + golden)" if lock.ota
                                 else "single image (fills the partition)"))
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
