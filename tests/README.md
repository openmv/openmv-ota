# Tests

Mirrors the verification plan in the concept document ("Verification"):

- `ed25519_kat/` — RFC 8032 known-answer vectors for the `ed25519_verify` C module.
- `wycheproof/` — Project Wycheproof negative corpus (low-order points,
  malleability, non-canonical encodings, malleable S, …).
- `boot_py_adversarial/` — boot.py trailer-parser + status state-machine tests
  (malformed trailers, every status combination, forged-confirm, replay, …).
- `integration/` — end-to-end build → flash → OTA → rollback.
