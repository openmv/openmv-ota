# Tests

Mirrors the verification plan in the concept document ("Verification"):

- `wycheproof/` — Project Wycheproof negative ECDSA corpus for the
  ECDSA-over-mbedtls verify shim (invalid R/S, s > n, point-not-on-curve,
  non-canonical / malformed encodings, …).
- `boot_py_adversarial/` — boot.py trailer-parser + status state-machine tests
  (malformed trailers, every status combination, forged-confirm, replay, …).
- `integration/` — end-to-end build → flash → OTA → rollback.
