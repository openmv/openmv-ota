"""A project's chain of trust, for server tests: roots an account pins, an issuer those roots
certified, and desk keys the issuer certifies -- what `PUT /roots` and `PUT /desk-keys/{id}` take."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from openmv_ota.ota import certificate as cert_mod
from openmv_ota.ota.algorithms import ES256, algorithm_for
from openmv_ota.ota.keys import generate_private_key, public_point_hex
from openmv_ota.ota.signer import LocalSigner

ALG = algorithm_for(ES256)


def expires(days=90):
    return (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


class Chain:
    """Two roots (0 and 1), an issuer certified by root 0, and the desks it certifies."""

    def __init__(self):
        self.root_privs = {i: generate_private_key(ALG) for i in (0, 1)}
        self.roots = [{"root_id": i, "pubkey": public_point_hex(k.public_key())}
                      for i, k in self.root_privs.items()]
        self.issuer_priv = generate_private_key(ALG)
        self.issuer_pub = bytes.fromhex(public_point_hex(self.issuer_priv.public_key()))
        self.issuer_cert = cert_mod.pack(cert_mod.issue(
            self.root_privs[0], 0, cert_mod.KIND_ISSUER, 0, self.issuer_pub)).hex()
        self.certs: dict[str, str] = {}             # desk pubkey -> its certificate, hex

    def root_signer(self, root_id=0):
        return LocalSigner(self.root_privs[root_id], root_id, ES256, ALG)

    def pin(self, c, headers, statement=None):
        body = {"roots": self.roots}
        if statement is not None:
            body["statement"] = statement
        return c.put("/api/v1/admin/roots", headers=headers, json=body)

    def desk(self):
        """``(signer, pubkey_hex)`` for a new certified desk key; the signer's id is the key's."""
        key = generate_private_key(ALG)
        pub = public_point_hex(key.public_key())
        self.certs[pub] = cert_mod.pack(cert_mod.issue(
            self.issuer_priv, 0, cert_mod.KIND_DESK, 0, bytes.fromhex(pub))).hex()
        return LocalSigner(key, cert_mod.key_id_of(bytes.fromhex(pub)), ES256, ALG), pub

    def body(self, pub, *, expires_at=None, revoked=False, label="bench-1"):
        return {"label": label, "pubkey": pub, "expires": expires_at or expires(),
                "revoked": revoked, "cert": self.certs.get(pub, ""),
                "issuer_cert": self.issuer_cert}

    def push(self, c, headers, pub, *, key_id=None, **kw):
        kid = key_id if key_id is not None else cert_mod.key_id_of(bytes.fromhex(pub))
        return c.put("/api/v1/admin/desk-keys/%d" % kid, headers=headers, json=self.body(pub, **kw))

    def push_desk(self, c, headers, label="bench"):
        """Pin the roots if needed, make a desk and push it; the desk's signer."""
        if not c.get("/api/v1/admin/roots", headers=headers).json()["roots"]:
            assert self.pin(c, headers).status_code == 200
        signer, pub = self.desk()
        r = self.push(c, headers, pub, label=label,
                      expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc).strftime(
                          "%Y-%m-%dT%H:%M:%SZ"))
        assert r.status_code == 200, r.text
        return signer
