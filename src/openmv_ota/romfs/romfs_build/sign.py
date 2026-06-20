"""Image signing (stub).

Signs the 128-byte signed prefix of the trailer with an ed25519 private key.
The customer owns all keys; we never see or store private keys.

TODO (see concept plan, "Signing"):
  - Software keyfile backend (development).
  - HSM backends (YubiHSM, AWS CloudHSM, PKCS#11) for production keys.
  - key_id selection from trusted_keys.json.
"""
