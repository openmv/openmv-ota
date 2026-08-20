# Signing keys

*[← 4 · OTA projects](04-ota-projects.md) · [Index](00-introduction.md) · [6 · Building →](06-building.md)*

---

A camera trusts exactly the public keys baked into its firmware — nothing can be added
later without re-flashing. That single fact shapes everything on this page: the whole
key set is provisioned up front at `new --ota`, and `openmv-ota project keys` manages it
for the product's life.

## The key set

The key set has two roles, generated on the curve `--sig-alg` selects (ES256 →
P-256 by default; ES384/ES512 raise the curve and signature size):

| Role | id range | Default count | Purpose |
|---|---|---|---|
| `factory` | `0x0001`+ | 8 (`--factory-keys`) | One per manufacturing run; *you* sign the factory image with it and ship the manufacturer the finished binary. A distinct id per run is for **attribution** (telling which run cut an image) and `revoke`, not key isolation. |
| `ota` | `0x0100`+ | 32 (`--ota-keys`) | The rotation pool; over-the-air updates are signed with one of these, rotated over the product's life. |

The two ranges are well-separated so the pools never collide at realistic counts.
The current signer is the first OTA key (`0x0100`), recorded as `signing_key_id`
in `openmv-ota.toml`'s `[ota]` section ([page 4](04-ota-projects.md#files-an-ota-project-adds)).
`build romfs` signs with that key, and a trailer records *which* key signed
(`key_id`) so the device picks the matching public key.

`keys/trusted_keys.json` is the committed public set the firmware build will bake
into its `TRUSTED_KEYS` table. Each entry is a key id, its COSE algorithm, its
role, and the public key as an uncompressed EC point in hex:

```json
{
  "schema": 1,
  "keys": [
    {"key_id": 1,   "alg": -7, "role": "factory", "pubkey": "04…"},
    {"key_id": 256, "alg": -7, "role": "ota",     "pubkey": "04…"}
  ]
}
```

## Provisioning (`new --ota`)

| Flag | Effect |
|---|---|
| `--sig-alg {ES256,ES384,ES512}` | Signature algorithm for the whole set (default `ES256`). |
| `--ota-keys N` | Rotation-pool size to provision (default 32). |
| `--factory-keys N` | Factory-key reserve, one per manufacturing site (default 8). |
| `--key-passphrase-file FILE` | Passphrase (read from FILE) that encrypts the private keys at rest. |
| `--dev` | Throwaway dev keys with a cached random passphrase — nothing to manage, and the production build rail refuses them. |

**Provision generously.** Because keys can't be added without re-flashing firmware,
the rotation pool is your entire future supply of OTA keys — `--ota-keys` below 4
warns.

**Backed up at birth.** `new` always writes the one-file key backup
(`keys-backup.bin`, the same artifact as `project keys backup`) in the very step
that creates the keys, so there is never a moment where the only copy lives on
this machine — **move it off this machine** (a vault, an offline drive). A `--dev`
project skips it: its keys are disposable.

**One hazard to know:** re-running `new --force` over an existing OTA project
**regenerates the whole key set**. Devices already in the field trust the *old*
keys and will reject updates signed by the new ones — you would have to re-flash
them. `new` warns loudly when this is about to happen; only do it for a fresh
fleet, and back up the old keys first.

### The passphrase

The private keys are **always encrypted at rest** — there is no plaintext mode. So
`new --ota` needs a passphrase to encrypt under, and accepts exactly two sources:
`--key-passphrase-file` (a real passphrase you manage) or `--dev` (a random
throwaway cached in `keys/.dev-passphrase`, so there is nothing to manage — and the
production build rail refuses images signed with dev keys). On a terminal, omitting
both gets you an **interactive prompt, typed twice and required to match** — a
mistyped new passphrase would seal your future key supply under a string nobody
knows. Provisioning reads **no environment variable**: an invisible source has no
place at this moment. (Passphrases travel in files or a prompt rather than on the
command line itself, where they would land in shell history and `ps` output.)

## Day to day: `status` and `rotate`

```bash
openmv-ota project keys status   # current signer, pool usage, revoked count
openmv-ota project keys rotate   # advance to the next OTA key
```

- **`status`** reports the current signing key + algorithm, how far through the
  pool you are (`#3 of 32`), how many keys are retired / remaining / revoked, the
  factory-key count, and how many private PEMs are on this machine (so you know if
  you're on the signing machine).

- **`rotate`** advances `[ota].signing_key_id` to the next key in the
  pre-provisioned pool — it doesn't mint a key. Old releases keep verifying (their
  key stays trusted); rotation just limits how much any one key signs. It errors
  when the pool is exhausted (you'd need a firmware reflash with a new set). Commit
  `openmv-ota.toml` — git is your rotation log.

## Compromise: `revoke`

```bash
openmv-ota project keys revoke 0x0100     # mark a compromised key (reversible)
openmv-ota project keys unrevoke 0x0100
```

**`revoke`** is the rare exception, for a **compromised** private key (HSM
breach, leaked CI secret). For normal hygiene you just rotate; revoke is for "an
attacker has this key and could forge images." It sets `revoked` on the key in
`keys/trusted_keys.json` (kept, never deleted), so `build romfs` refuses to sign
with it and `rotate` skips it. It's deliberately **not** auto-applied to fielded
devices: the trusted/revoked set is baked into the **firmware** by `build
firmware` — a romfs OTA update never changes it — so a revoked key only stops
being trusted once a device's firmware is re-flashed (today a physical reflash;
there is no firmware-over-OTA). It's reversible with `unrevoke` (for a
fat-fingered id or false alarm). Revoking the current signer doesn't move it —
`build romfs` will refuse until you `rotate`.

## Custody: backups and external backends

Unless an external backend holds them (below), the private keys exist in exactly
one place — your signing machine — so custody has its own verbs:

```bash
openmv-ota project keys backup                        # -> keys-backup.bin
openmv-ota project keys restore keys-backup.bin
```

**`backup` / `restore`** — the off-machine copy, and the way back. `backup`
writes every private PEM into one integrity-checked file, `keys-backup.bin` —
the same file `new` already wrote, since the key set never changes. `restore`
rebuilds `keys/private/` from it on a replacement machine. Neither takes a
passphrase: the PEMs are archived exactly as they sit on disk, already
encrypted. A `--dev` project is refused — its passphrase is cached beside the
keys, so a copy would be plaintext in effect.

### External backends (HSM / cloud KMS)

The keys don't have to live on disk at all. `keys/backends.json` (committed) maps a
trusted `key_id` to how *this project* reaches that key's private material; a key
with no record uses its local encrypted PEM, so external and local keys mix freely
in one set. The records hold only **non-secret** references — ARNs, token and
object labels, module paths. Secrets (a PKCS#11 PIN, cloud credentials) come from
`openmv-ota.local.toml` or the ambient cloud environment, never the repo — which is
what lets teammates and CI commit-and-sign without reconfiguring.

```bash
openmv-ota project keys backend show                  # each key's backend
openmv-ota project keys backend configure 0x0100 --backend aws-kms \
    --set uri=arn:aws:kms:us-east-1:111122223333:key/example
openmv-ota project keys backend provision --backend pkcs11 \
    --set pkcs11_module=/usr/lib/softhsm/libsofthsm2.so --set token_label=openmv
```

**`configure`** is bring-your-own-key: it points one **existing** trusted key at
external material you already hold. **`provision`** goes further — it mints a whole
fresh key set *inside* the backend (`C_GenerateKeyPair` on a token, the provider's
create-key API in a KMS) and rewrites `keys/trusted_keys.json` +
`keys/backends.json` from the returned public halves, so the private half **never
exists on this machine**. It defaults to `--ota-keys 4 --factory-keys 1` because
external keys are often billable. Note what provisioning means: **it re-keys the
fleet** — fielded devices trust the new set only after a firmware update carries
it, exactly like the `--force` hazard above. Commit both files.

Each backend's record, and the pip extra that enables it — extras rather than
default dependencies because the cloud SDKs are heavy, conflict-prone, and each
one you install is supply-chain surface you own; nearly every project uses at
most one:

| `--backend` | Record fields (`--set k=v`) | Extra |
|---|---|---|
| `pkcs11` | `pkcs11_module` (the token's `.so`), `token_label`, `object_label` (defaults to `<role>-<keyid>`); the PIN is machine-local, never committed | `openmv-ota[hsm]` |
| `aws-kms` | `uri` — the key ARN | `openmv-ota[aws-kms]` |
| `gcp-kms` | `uri` — the crypto-key **version** resource name | `openmv-ota[gcp-kms]` |
| `azure-kms` | `uri` — the vault key URL (`provision` takes `vault_url`) | `openmv-ota[azure-kms]` |
| `custom` | `factory` — `pkg.module:callable` returning a `Signer`; bring anything | — |

At build time nothing changes on the surface: `build romfs` looks up the signing
key's record and signs through it — a token signs the digest on-token, a KMS signs
in the cloud (the raw `R||S` length is checked either way) — and `backup` /
`restore` simply don't apply to external keys, because there is nothing on disk to
lose.

---

*[← 4 · OTA projects](04-ota-projects.md) · [Index](00-introduction.md) · [6 · Building →](06-building.md)*
