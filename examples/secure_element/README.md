# Secure element demos

Three short scripts that use a camera's keys through `openmv_ota.se`, the same drivers the
camera signs its check-ins with. [Device keys](../../docs/reference/device-keys.md) says where
each board keeps them.

| Script | Runs on | What it does |
| --- | --- | --- |
| `read_keys.py` | the camera | Prints what holds the keys, the identity key, the exchange key, and the chip maker's certificate for the identity key where there is one (the SE050's, from NXP, as PEM). |
| `sign_challenge.py` | the camera | Signs a challenge with the identity key and prints the host command that checks the signature. |
| `verify_challenge.py` | the host | Checks that signature: prints `OK` (exit 0) or `FAILED` (exit 1). |

The keys are made once, at a desk, by `openmv-ota flash factory`; these scripts only use them.
A camera that was never flashed that way has no keys yet, and the scripts say so.

The **identity key** is the one the camera signs its check-ins with, and the one `flash
factory` registers with the server. On a board with a secure element (SE050, ATECC608) its
private half never leaves the chip: the chip does the signing. The **exchange key** only does
ECDH.

## On the camera

The scripts import `openmv_ota.se`, so the camera needs firmware and a romfs built by
openmv-ota. Open a script in OpenMV IDE and run it (the output is in the Serial Terminal), or
from this directory:

```
mpremote run read_keys.py
mpremote run sign_challenge.py
```

`sign_challenge.py` signs the hex string `CHALLENGE` at its top: put your verifier's nonce
there. Left empty, it signs 32 random bytes from the camera's RNG.

## On the host

`sign_challenge.py` ends by printing the command to run, from this directory:

```
python verify_challenge.py --public-key 04... --challenge ... --signature 30...
```

It needs `cryptography`, which `pip install openmv-ota` installs. A good signature proves only
which key made it, so also compare `--public-key` with the identity key you already trust for
that camera: the one `flash factory` printed, or what `read_keys.py` prints.

The signature covers SHA-256 of the context `omv-demo-challenge-v1\n` followed by the
challenge. The context keeps a signature from these demos from ever being one the camera makes
for anything else: a challenge built to look like a check-in still can't get a check-in signed.
An application that signs challenges for its own protocol should use its own context the same
way.
