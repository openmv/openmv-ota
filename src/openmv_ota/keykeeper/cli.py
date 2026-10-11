"""CLI handlers for ``openmv-ota keykeeper``.

    init      make the first two generations of keys (the current one, and the next)
    serve     run the private service (needs the [server] extra); --init makes the keys on
              the first start, for a container whose command can't chain two steps
    chain     print the chain of public keys, as JSON
    pin       print the pin a publisher starts from (the first generation)
    rotate    an emergency rotation: a new generation in effect now

Everything is read from the environment, so the same command runs on a laptop and in a
container: ``OPENMV_KEYKEEPER_DIR`` (default ``./keykeeper``), ``OPENMV_KEYKEEPER_PASSPHRASE``
(or ``--passphrase-file``) for the private keys, ``OPENMV_KEYKEEPER_TOKEN`` -- the secret the
update server sends -- for ``serve``, and ``OPENMV_KEYKEEPER_PERIOD_DAYS`` (default 365).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

DIR_VAR = "OPENMV_KEYKEEPER_DIR"
PASSPHRASE_VAR = "OPENMV_KEYKEEPER_PASSPHRASE"  # noqa: S105 - an env var name, not a secret
TOKEN_VAR = "OPENMV_KEYKEEPER_TOKEN"  # noqa: S105 - an env var name, not a secret
PERIOD_VAR = "OPENMV_KEYKEEPER_PERIOD_DAYS"


def register(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="_subcommand")
    for name, func, text in (("init", cmd_init, "make the first two generations of keys"),
                             ("serve", cmd_serve, "run the private service"),
                             ("chain", cmd_chain, "print the chain of public keys (JSON)"),
                             ("pin", cmd_pin, "print the pin a publisher starts from"),
                             ("rotate", cmd_rotate, "emergency rotation: new keys in effect now")):
        p = sub.add_parser(name, help=text)
        p.add_argument("--dir", help="where the keys live (default $%s or ./keykeeper)" % DIR_VAR)
        p.add_argument("--passphrase-file", help="file holding the keys' passphrase "
                       "(default $%s)" % PASSPHRASE_VAR)
        if name == "serve":
            p.add_argument("--host", default="127.0.0.1",
                           help="bind host (default 127.0.0.1: keykeeper is private)")
            p.add_argument("--port", type=int, default=8090, help="bind port (default 8090)")
            p.add_argument("--init", action="store_true",
                           help="make the keys first if there are none (a container's first start "
                                "on an empty disk); keys that exist are never replaced")
        if name == "rotate":
            p.add_argument("--now", action="store_true", required=True,
                           help="required: rotation happens on its own schedule otherwise")
        p.set_defaults(func=func, _command="keykeeper %s" % name)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _period() -> int:
    try:
        return int(os.environ.get(PERIOD_VAR, "365"))
    except ValueError:
        raise SystemExit("%s must be a whole number of days" % PERIOD_VAR) from None


def _ring(args, *, need_passphrase=True):
    from .ring import Ring
    root = Path(args.dir or os.environ.get(DIR_VAR) or "keykeeper")
    passphrase = ""
    if args.passphrase_file:
        passphrase = Path(args.passphrase_file).read_text(encoding="utf-8").strip()
    else:
        passphrase = os.environ.get(PASSPHRASE_VAR, "")
    if need_passphrase and not passphrase:
        raise SystemExit("keykeeper's keys are encrypted: set %s or pass --passphrase-file"
                         % PASSPHRASE_VAR)
    return Ring(root, passphrase)


def _fail(e) -> int:
    print("error: %s" % e, file=sys.stderr)
    return 1


def cmd_init(args) -> int:
    from .ring import KeykeeperError
    try:
        gens = _ring(args).init(_now(), _period())
    except KeykeeperError as e:
        return _fail(e)
    print("Keykeeper keys made: generation %d now, generation %d from %s."
          % (gens[0].key_id, gens[1].key_id, gens[1].not_before))
    print("Publishers start from this pin (put it in openmv-ota.toml):")
    print("  %s" % gens[0].pin())
    return 0


def cmd_chain(args) -> int:
    from .ring import KeykeeperError
    try:
        print(json.dumps({"chain": _ring(args, need_passphrase=False).chain()}, indent=2))
    except KeykeeperError as e:
        return _fail(e)
    return 0


def cmd_pin(args) -> int:
    from .ring import KeykeeperError
    try:
        print(_ring(args, need_passphrase=False).genesis_pin())
    except KeykeeperError as e:
        return _fail(e)
    return 0


def cmd_rotate(args) -> int:
    from .ring import KeykeeperError
    try:
        gen = _ring(args).rotate_now(_now(), _period())
    except KeykeeperError as e:
        return _fail(e)
    print("Generation %d is in effect now; publishers pick it up from the chain." % gen.key_id)
    return 0


def _serve(app, host, port):  # pragma: no cover  (blocks; seam monkeypatched)
    import uvicorn
    uvicorn.run(app, host=host, port=port)


def cmd_serve(args) -> int:
    from openmv_ota.server._extras import require_server_extra
    require_server_extra()
    from .ring import KeykeeperError
    from .service import Keykeeper, create_app
    ring = _ring(args)
    try:
        if args.init and not ring.chain_path.exists():
            gens = ring.init(_now(), _period())
            print("Keykeeper keys made; publishers start from this pin: %s" % gens[0].pin(),
                  flush=True)
        keeper = Keykeeper(ring, os.environ.get(TOKEN_VAR, ""), ring.root / "audit.jsonl",
                           _period())
    except KeykeeperError as e:
        return _fail(e)
    _serve(create_app(keeper), args.host, args.port)
    return 0
