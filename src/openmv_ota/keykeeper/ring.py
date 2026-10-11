"""Keykeeper's keys, and the chain that lets everyone else follow them through rotations.

Keykeeper's job (see :mod:`openmv_ota.ota.lockbox`) needs one private key: the one that opens
seals. It rotates on a schedule, so a publisher must learn each new one -- and must not take the
update server's word for it, or a server that has been broken into could hand out a key of its
own and read every release sealed to it. So each **generation** of keys is announced in a record
signed by the generation before it, and a publisher pins one generation and walks forward:

    {"v": 1, "key_id": 2, "seal": "04...", "sign": "04...", "not_before": "2027-10-10T00:00:00Z"}

* ``seal`` -- publishers seal content keys to it (ECDH). Its private half opens seals.
* ``sign`` -- it signs only the NEXT generation's record. Keeping it apart from ``seal`` means
  no key does two jobs.
* ``not_before`` -- when publishers start sealing to it. There are always two generations live,
  the current one and the next, already announced, so a publisher that pins today follows the
  next rotation without being told.

The chain is public: anyone can hold it, nobody can extend it without the newest generation's
sign key. The first generation signs its own record; trust in it comes from the pin. Old
generations' seal keys are kept, so a release sealed before a rotation still installs.

    <root>/chain.json                         the chain: [{"record": "...", "signature": "..."}]
    <root>/private/kk-0001-seal.pem           private halves, encrypted with the keykeeper
    <root>/private/kk-0001-sign.pem           passphrase (or in an HSM/KMS: see backends)

A leaked keykeeper key is the one thing the chain can't fix: whoever holds the newest sign key
can extend it. That reset is out of band -- new pins with a CLI release.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

from openmv_ota.ota.errors import OtaError
from openmv_ota.ota.keys import load_private_key_pem, private_key_pem, public_point_hex

VERSION = 1
DEFAULT_PERIOD_DAYS = 365
_FIELDS = ("v", "key_id", "seal", "sign", "not_before")


class KeykeeperError(OtaError):
    """A keykeeper key or chain problem, with the reason."""


@dataclass(frozen=True)
class Generation:
    """One verified generation of keykeeper keys (public halves)."""
    key_id: int
    seal: str                                    # uncompressed P-256 point, hex
    sign: str
    not_before: str                              # ISO 8601, UTC

    def pin(self) -> str:
        """``"<key_id>:<sign key>"`` -- what a publisher stores to trust this generation."""
        return "%d:%s" % (self.key_id, self.sign)


def _iso(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _utc(text: str) -> datetime:
    try:
        return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        raise KeykeeperError("%r isn't a UTC timestamp" % (text,)) from None


def _point(hex_point: str) -> ec.EllipticCurvePublicKey:
    try:
        return ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(),
                                                            bytes.fromhex(hex_point))
    except (TypeError, ValueError):
        raise KeykeeperError("%r isn't an uncompressed P-256 public key" % (hex_point,)) from None


def _record_bytes(gen: Generation) -> bytes:
    return json.dumps({"v": VERSION, "key_id": gen.key_id, "seal": gen.seal, "sign": gen.sign,
                       "not_before": gen.not_before}, separators=(",", ":")).encode()


def _parse(record: str) -> Generation:
    try:
        d = json.loads(record)
        if not isinstance(d, dict) or set(d) != set(_FIELDS):
            raise ValueError
        if d["v"] != VERSION:
            raise KeykeeperError("chain record version %r is not one this software reads"
                                 % d["v"])
        if (not isinstance(d["key_id"], int) or isinstance(d["key_id"], bool)
                or d["key_id"] < 1):
            raise ValueError
        gen = Generation(d["key_id"], str(d["seal"]), str(d["sign"]), str(d["not_before"]))
    except (ValueError, TypeError, KeyError):
        raise KeykeeperError("a chain record is malformed") from None
    _point(gen.seal)
    _point(gen.sign)
    _utc(gen.not_before)
    return gen


def verify_chain(chain: list[dict], pin: str) -> list[Generation]:
    """The generations from the pinned one forward, each verified by the one before it. Raises
    if the pinned generation isn't in the chain, or any link after it doesn't verify -- never
    returns a generation the pin doesn't vouch for."""
    try:
        kid_text, _, sign_pub = pin.partition(":")
        pinned_id = int(kid_text)
    except ValueError:
        raise KeykeeperError("a keykeeper pin is '<key_id>:<sign key>', not %r" % pin) from None
    gens = []
    try:
        entries = [(e["record"], bytes.fromhex(e["signature"])) for e in chain]
    except (KeyError, TypeError, ValueError):
        raise KeykeeperError("the keykeeper chain is malformed") from None
    for record, signature in entries:
        gen = _parse(record)
        if not gens:
            if gen.key_id != pinned_id:
                continue
            if gen.sign != sign_pub:
                raise KeykeeperError("the chain's generation %d isn't the one pinned" % pinned_id)
            gens.append(gen)
            continue
        prev = gens[-1]
        if gen.key_id != prev.key_id + 1:
            raise KeykeeperError("the keykeeper chain skips from %d to %d"
                                 % (prev.key_id, gen.key_id))
        try:
            _point(prev.sign).verify(signature, record.encode(), ec.ECDSA(hashes.SHA256()))
        except InvalidSignature:
            raise KeykeeperError("keykeeper generation %d isn't signed by %d"
                                 % (gen.key_id, prev.key_id)) from None
        gens.append(gen)
    if not gens:
        raise KeykeeperError("the pinned keykeeper generation %d isn't in the chain" % pinned_id)
    return gens


def current(gens: list[Generation], now: datetime) -> Generation:
    """The generation publishers seal to now: the newest whose ``not_before`` has passed."""
    now_iso = _iso(now)
    live = [g for g in gens if g.not_before <= now_iso]
    if not live:
        raise KeykeeperError("no keykeeper generation is in effect yet")
    return live[-1]


class Ring:
    """Keykeeper's own keys on disk: the chain, and the private halves under ``private/``."""

    def __init__(self, root: str | Path, passphrase: str):
        self.root = Path(root)
        self.passphrase = passphrase

    # -- reading -----------------------------------------------------------------------------

    @property
    def chain_path(self) -> Path:
        return self.root / "chain.json"

    def chain(self) -> list[dict]:
        """The chain as published: ``[{"record": str, "signature": hex}]``, oldest first."""
        if not self.chain_path.exists():
            raise KeykeeperError("no keykeeper keys in %s -- `openmv-ota keykeeper init` makes "
                                 "them" % self.root)
        try:
            chain = json.loads(self.chain_path.read_text(encoding="utf-8"))["chain"]
            if not isinstance(chain, list) or not chain:
                raise ValueError
            return chain
        except (ValueError, KeyError, TypeError):
            raise KeykeeperError("%s is damaged" % self.chain_path) from None

    def generations(self) -> list[Generation]:
        """Every generation, verified from the first (which this ring holds the keys for)."""
        chain = self.chain()
        return verify_chain(chain, _parse(chain[0]["record"]).pin())

    def genesis_pin(self) -> str:
        return _parse(self.chain()[0]["record"]).pin()

    def _private(self, key_id: int, role: str):
        path = self.root / "private" / ("kk-%04x-%s.pem" % (key_id, role))
        if not path.exists():
            raise KeykeeperError("no %s key for keykeeper generation %d" % (role, key_id))
        return load_private_key_pem(path.read_bytes(), self.passphrase)

    def seal_key(self, key_id: int):
        """The private seal key of generation ``key_id`` -- what opens seals made for it."""
        return self._private(key_id, "seal")

    # -- writing -----------------------------------------------------------------------------

    def _mint(self, key_id: int, not_before: datetime):
        seal = ec.generate_private_key(ec.SECP256R1())
        sign = ec.generate_private_key(ec.SECP256R1())
        private = self.root / "private"
        private.mkdir(parents=True, exist_ok=True)
        for role, key in (("seal", seal), ("sign", sign)):
            (private / ("kk-%04x-%s.pem" % (key_id, role))).write_bytes(
                private_key_pem(key, self.passphrase))
        return Generation(key_id, public_point_hex(seal.public_key()),
                          public_point_hex(sign.public_key()), _iso(not_before)), sign

    def _append(self, chain: list[dict], gen: Generation, signer) -> None:
        record = _record_bytes(gen)
        chain.append({"record": record.decode(),
                      "signature": signer.sign(record, ec.ECDSA(hashes.SHA256())).hex()})

    def _write(self, chain: list[dict]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.chain_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"chain": chain}, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.chain_path)

    def init(self, now: datetime, period_days: int = DEFAULT_PERIOD_DAYS) -> list[Generation]:
        """The first two generations: the current one, and the next, a period later."""
        if self.chain_path.exists():
            raise KeykeeperError("%s already holds keykeeper keys" % self.root)
        first, first_sign = self._mint(1, now)
        chain: list[dict] = []
        self._append(chain, first, first_sign)             # self-signed: the pin vouches for it
        nxt, _ = self._mint(2, now + timedelta(days=period_days))
        self._append(chain, nxt, first_sign)
        self._write(chain)
        return self.generations()

    def extend(self, now: datetime, period_days: int = DEFAULT_PERIOD_DAYS) -> list[Generation]:
        """Keep a next generation announced: while the newest one is already in effect, add one
        a period after it. What a running keykeeper does on its schedule. Returns the new ones."""
        chain, gens, added = self.chain(), self.generations(), []
        while gens[-1].not_before <= _iso(now):
            last = gens[-1]
            gen, _ = self._mint(last.key_id + 1, _utc(last.not_before) + timedelta(days=period_days))
            self._append(chain, gen, self._private(last.key_id, "sign"))
            gens.append(gen)
            added.append(gen)
        if added:
            self._write(chain)
        return added

    def rotate_now(self, now: datetime, period_days: int = DEFAULT_PERIOD_DAYS) -> Generation:
        """An emergency rotation: a generation in effect at once, and a next one after it.
        Seals made from now on use it; it does NOT help if a keykeeper key leaked (see above)."""
        chain = self.chain()
        last = self.generations()[-1]
        gen, _ = self._mint(last.key_id + 1, now)
        self._append(chain, gen, self._private(last.key_id, "sign"))
        self._write(chain)
        self.extend(now, period_days)
        return gen
