"""Keykeeper's HTTP service: seals in, lockboxes out. Private: never put it on the internet.

Run it next to the update server (localhost, a private network, a private service on a PaaS) and
give the two of them a shared secret. It has one real endpoint:

    POST /v1/lockbox   {"sealed": {...}, "record": "...", "signature": "..."}
                    -> {"lockbox": "<145 bytes, hex>", "device_id": "...", "seal_key_id": n}

``sealed`` is a release's content key as its publisher sealed it (with the account it belongs to
and that account's desk keys at the time); ``record`` and ``signature`` are a camera's
registration (:mod:`openmv_ota.ota.registration`). Keykeeper opens the seal, checks the record
against the desk keys IN THE SEAL and the account IN THE SEAL -- nothing the server says -- and
wraps the content key for the record's exchange key. So the most a server that has been broken
into can get from it is a lockbox for a real, desk-registered camera of that account: the key
that camera would have got anyway.

Every request is appended to an audit log (JSON lines). The chain of keykeeper keys is served at
``GET /v1/chain`` so the update server can pass it on to publishers; it's public, and verified
by them from their pin, so serving it needs no trust.

Keykeeper extends its own chain on a schedule (:meth:`Ring.extend`): a running keykeeper always
has the next generation announced.
"""

import hmac
import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from openmv_ota.ota import lockbox as lb
from openmv_ota.ota import registration as reg

from .ring import DEFAULT_PERIOD_DAYS, KeykeeperError, Ring

_EXTEND_EVERY_S = 3600
SEALED_VERSION = 1


def _now() -> datetime:
    return datetime.now(timezone.utc)


def desk_keys_of(contents: dict) -> list[reg.DeskKey]:
    """The desk keys a seal vouches for, as a verifier takes them."""
    try:
        return [reg.DeskKey(int(d["key_id"]), str(d["pubkey"]), str(d["expires"]),
                            bool(d.get("revoked", False))) for d in contents["desks"]]
    except (KeyError, TypeError, ValueError):
        raise lb.LockboxError("the seal's desk keys are malformed") from None


def lockbox_for(contents: dict, record: bytes, signature: str) -> tuple[bytes, reg.Registration]:
    """The lockbox a seal's contents make for one registered camera, and what was registered.
    Raises :class:`~openmv_ota.ota.lockbox.LockboxError` or
    :class:`~openmv_ota.ota.registration.RegistrationError` saying why not."""
    if contents.get("v") != SEALED_VERSION:
        raise lb.LockboxError("sealed contents version %r is not one this keykeeper reads"
                              % contents.get("v"))
    account = contents.get("account")
    if not isinstance(account, str) or not account:
        raise lb.LockboxError("the seal names no account")
    try:
        content_key = bytes.fromhex(contents["content_key"])
    except (KeyError, TypeError, ValueError):
        raise lb.LockboxError("the seal's content key is malformed") from None
    got = reg.verify(record, signature, desk_keys_of(contents), account=account)
    return lb.make_lockbox(content_key, got.exchange), got


class Keykeeper:
    """The service's state: the ring, its open seal keys, the audit log, the schedule."""

    def __init__(self, ring: Ring, token: str, audit_path: str | Path,
                 period_days: int = DEFAULT_PERIOD_DAYS, clock=_now):
        if not token:
            raise KeykeeperError("keykeeper needs a shared secret: a server must prove it is the "
                                 "one allowed to ask")
        self.ring = ring
        self.token = token
        self.audit_path = Path(audit_path)
        self.period_days = period_days
        self.clock = clock
        self._seal_keys: dict[int, object] = {}
        self._lock = threading.Lock()
        self._extended = 0.0
        self.extend()

    def extend(self) -> None:
        """Announce the next generation when it's due; cheap when it isn't."""
        with self._lock:
            self.ring.extend(self.clock(), self.period_days)
            self._extended = time.monotonic()

    def maybe_extend(self) -> None:
        if time.monotonic() - self._extended > _EXTEND_EVERY_S:
            self.extend()

    def authorized(self, header: str | None) -> bool:
        scheme, _, given = (header or "").partition(" ")
        return scheme.lower() == "bearer" and hmac.compare_digest(given.encode(),
                                                                  self.token.encode())

    def seal_key(self, key_id: int):
        with self._lock:
            if key_id not in self._seal_keys:
                self._seal_keys[key_id] = self.ring.seal_key(key_id)
            return self._seal_keys[key_id]

    def audit(self, **event) -> None:
        event["ts"] = self.clock().strftime("%Y-%m-%dT%H:%M:%SZ")
        line = json.dumps(event, sort_keys=True) + "\n"
        with self._lock:
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)
            with self.audit_path.open("a", encoding="utf-8") as f:
                f.write(line)

    def lockbox(self, body: dict) -> dict:
        """One request's answer, or raise with the reason (audited either way)."""
        event = {"action": "lockbox"}
        try:
            sealed, record, signature = body["sealed"], body["record"], body["signature"]
            if not (isinstance(sealed, dict) and isinstance(record, str)
                    and isinstance(signature, str)):
                raise TypeError
        except (KeyError, TypeError):
            self.audit(ok=False, reason="malformed request", **event)
            raise lb.LockboxError("a lockbox request is {sealed, record, signature}") from None
        try:
            key_id = lb.seal_key_id(sealed)
            event["seal_key_id"] = key_id
            contents = lb.open_seal(sealed, self.seal_key(key_id))
            event["account"] = contents.get("account")
            event["release"] = contents.get("release")
            box, got = lockbox_for(contents, record.encode("utf-8"), signature)
        except (lb.LockboxError, reg.RegistrationError, KeykeeperError) as e:
            self.audit(ok=False, reason=str(e), **event)
            raise
        self.audit(ok=True, device_id=got.device_id, desk=got.desk, **event)
        return {"lockbox": box.hex(), "device_id": got.device_id, "seal_key_id": key_id}


def create_app(keeper: Keykeeper):
    """The ASGI app (needs the [server] extra, like the update server)."""
    from fastapi import FastAPI, HTTPException, Request

    app = FastAPI(title="openmv-ota keykeeper", docs_url=None, redoc_url=None, openapi_url=None)

    def _auth(request: Request) -> None:
        if not keeper.authorized(request.headers.get("authorization")):
            raise HTTPException(status_code=401, detail="unauthorized")

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/v1/chain")
    def chain(request: Request):
        _auth(request)
        keeper.maybe_extend()
        return {"chain": keeper.ring.chain()}

    @app.post("/v1/lockbox")
    async def lockbox(request: Request):
        _auth(request)
        keeper.maybe_extend()
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(status_code=422, detail="not JSON") from None
        if not isinstance(body, dict):
            raise HTTPException(status_code=422, detail="not a JSON object")
        try:
            return keeper.lockbox(body)
        except (lb.LockboxError, reg.RegistrationError, KeykeeperError) as e:
            raise HTTPException(status_code=422, detail=str(e)) from None

    return app
