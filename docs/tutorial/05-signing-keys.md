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
(`key_id`) so the device picks the matching public key. Both roles' private keys
stay on your signing machine — a manufacturer receives the signed
`<board>-factory-romfs.img`, never a key.

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

Provision generously: because keys can't be added without re-flashing firmware,
the rotation pool is your entire future supply of OTA keys. `--ota-keys` below 4
warns.

One hazard to know: re-running `new --force` over an existing OTA project
**regenerates the whole key set**. Devices already in the field trust the *old*
keys and will reject updates signed by the new ones — you would have to re-flash
them. `new` warns loudly when this is about to happen; only do it for a fresh
fleet, and back up the old keys first.

### Passphrases

The private keys are **always encrypted at rest** — there is no plaintext mode. So
`new --ota` needs a passphrase to encrypt under, and accepts exactly two sources:
`--key-passphrase-file` (a real passphrase you manage) or `--dev` (a random
throwaway cached in `keys/.dev-passphrase`, so there is nothing to manage — and the
production build rail refuses images signed with dev keys).

**Signing a build** (`build romfs` / `ota-romfs` / `factory-romfs`) resolves the
passphrase in priority order: the project's cached dev passphrase when present,
then `--key-passphrase-file`, then the `OPENMV_OTA_KEY_PASSPHRASE` environment
variable (what CI uses), and finally an **interactive prompt** when running in a
terminal — so day to day you can simply type it; the file flag exists for scripts.
The `keys` verbs are explicit instead: `backup` / `restore` require their own
`--passphrase-file` (the backup's passphrase, which may differ from the signing
one), `encrypt` takes `--key-passphrase-file` or `--dev`, and `rotate` / `revoke` /
`status` need no passphrase at all. (Passphrases travel in files or the environment
rather than on the command line itself, where they would land in shell history and
`ps` output.)

### Provisioning options at `new --ota`

| Flag | Effect |
|---|---|
| `--sig-alg {ES256,ES384,ES512}` | Signature algorithm for the whole set (default `ES256` / P-256). |
| `--ota-keys N` | Rotation-pool size to provision (default 32). |
| `--factory-keys N` | Factory-key reserve, one per manufacturing site (default 8). |
| `--key-passphrase-file FILE` | Passphrase (read from a file) encrypting the private keys at rest; keys are never stored plaintext. |
| `--dev` | Throwaway dev keys with a cached random passphrase — nothing to manage, and the production build rail refuses them. |
| `--backup-passphrase-file FILE` | Passphrase (read from FILE) for the off-machine key backup: `new` then writes `keys-backup.enc` (the same artifact as `project keys backup`) in the very step that creates the keys, so there is never a moment where the only copy lives on this machine. Without it, `new` prints a reminder to back up manually. |

## Managing keys (`project keys`)

```bash
openmv-ota project keys status   # current signer, pool usage, revoked count
openmv-ota project keys rotate   # advance to the next OTA key
openmv-ota project keys revoke 0x0100     # mark a compromised key (reversible)
openmv-ota project keys unrevoke 0x0100
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

- **`revoke`** is the rare exception, for a **compromised** private key (HSM
  breach, leaked CI secret). For normal hygiene you just rotate; revoke is for "an
  attacker has this key and could forge images." It sets `revoked` on the key in
  `keys/trusted_keys.json` (kept, never deleted), so `build romfs` refuses to sign
  with it and `rotate` skips it. It's deliberately **not** auto-applied to fielded
  devices: the device-side reject-list is baked by a firmware build, so a revoked
  key only stops being trusted once a device updates. It's reversible with
  `unrevoke` (for a fat-fingered id or false alarm). Revoking the current signer
  doesn't move it — `build romfs` will refuse until you `rotate`.

## Custody: backups, encryption at rest, external backends

The private keys exist in exactly one place — your signing machine — so their custody
has its own verbs:

```bash
openmv-ota project keys backup  --passphrase-file pass.txt          # -> keys-backup.enc
openmv-ota project keys restore keys-backup.enc --passphrase-file pass.txt
openmv-ota project keys encrypt --key-passphrase-file pass.txt      # (re-)encrypt at rest
openmv-ota project keys backend show | configure | provision
```

- **`backup` / `restore`** — an encrypted archive of the private keys
  (`keys-backup.enc`), for the out-of-band copy the [OTA projects
  page](04-ota-projects.md#files-an-ota-project-adds) tells you to keep. `restore`
  rebuilds `keys/private/` on a replacement machine from that one file.
- **`encrypt`** — (re-)encrypts the private keys at rest under a passphrase read from
  a file; keys are never stored plaintext. `--dev` swaps in a random cached dev
  passphrase (`keys/.dev-passphrase`) — nothing to manage, and the production build
  rail refuses dev-encrypted keys.
- **`backend`** — keys don't have to live on disk at all. `show` lists each key's
  signing backend; `configure` points a trusted key at an **external** signer
  (PKCS#11 / cloud KMS — bring your own key); `provision` generates a fresh key set
  *inside* such a backend, so the private half never exists on this machine.

## See also

- [Trailer format](../reference/trailer.md) — the signature algorithms and the `key_id` / `sig_alg` fields the trailer records.
- [Threat model](../reference/threat-model.md) — the trust root, and why a manufacturer or the update server never holds a key.

---

*[← 4 · OTA projects](04-ota-projects.md) · [Index](00-introduction.md) · [6 · Building →](06-building.md)*
