"""The project's view of keykeeper: which generation of its keys releases are sealed to.

Trust starts at ``[ota].keykeeper_pin`` (a generation, from ``openmv-ota keykeeper pin``) and
moves forward only along the chain, each generation signed by the one before it
(:mod:`openmv_ota.keykeeper.ring`). ``client keykeeper sync`` fetches the chain -- from the update
server, which is fine: it can't forge a link -- verifies it from the newest generation this project
already trusts, and keeps it here:

    keys/keykeeper.json     {"pin": "<the generation in effect when last synced>", "chain": [...]}

So the pin follows keykeeper's rotations with no commit and no CLI release per rotation. A build
seals to the generation in effect now, and refuses once the kept chain has no next generation
announced: keykeeper has rotated since, and sealing to a retiring key would be sealing to the
past. Sync, and build again.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from openmv_ota.keykeeper.ring import Generation, KeykeeperError, current, verify_chain

from .errors import ProjectError

FILE_NAME = "keykeeper.json"


def path_for(root: str | Path) -> Path:
    return Path(root) / "keys" / FILE_NAME


def _read(root) -> dict | None:
    p = path_for(root)
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(d.get("pin"), str) or not isinstance(d.get("chain"), list):
            raise ValueError
        return d
    except (ValueError, AttributeError):
        raise ProjectError("%s is damaged" % p) from None


def trusted_pin(root, config_pin: str) -> str:
    """The newest generation this project trusts: the synced one, else the configured one."""
    state = _read(root)
    if state is not None:
        return state["pin"]
    if not config_pin:
        raise ProjectError("no keykeeper pin: set [ota].keykeeper_pin to `openmv-ota keykeeper "
                           "pin` from your keykeeper (the hosted one is printed in the docs)")
    return config_pin


def sync(root, config_pin: str, chain: list, now: datetime | None = None) -> list[Generation]:
    """Verify ``chain`` from the trusted pin and keep it, advancing the pin to the generation in
    effect now. Raises if the chain doesn't extend what this project already trusts."""
    now = now or datetime.now(timezone.utc)
    try:
        gens = verify_chain(chain, trusted_pin(root, config_pin))
        pin = current(gens, now).pin()
    except KeykeeperError as e:
        raise ProjectError("the keykeeper chain doesn't verify from this project's pin: %s -- "
                           "if keykeeper was reset after a leak, set a new [ota].keykeeper_pin "
                           "and delete %s" % (e, path_for(root))) from None
    p = path_for(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"pin": pin, "chain": chain}, indent=2) + "\n", encoding="utf-8")
    return gens


def sealing_generation(root, now: datetime | None = None) -> Generation:
    """The generation a release built now is sealed to. Raises, saying to sync, when there is no
    kept chain or it is a rotation behind."""
    now = now or datetime.now(timezone.utc)
    state = _read(root)
    if state is None:
        raise ProjectError("per-camera encryption needs keykeeper's keys: run `openmv-ota "
                           "client keykeeper sync` first")
    try:
        gens = verify_chain(state["chain"], state["pin"])
        gen = current(gens, now)
    except KeykeeperError as e:
        raise ProjectError("%s: %s" % (path_for(root), e)) from None
    if gen is gens[-1]:
        raise ProjectError("keykeeper has rotated since this project last synced (no next "
                           "generation is announced): run `openmv-ota client keykeeper sync`")
    return gen
