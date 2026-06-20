// ed25519_verify — MicroPython C module for the openmv firmware.
//
// Derived from pmvr/micropython-curve25519 (https://github.com/pmvr/micropython-curve25519):
// reuse its field-arithmetic kernel and add Edwards-curve point operations plus
// the EdDSA (RFC 8032) verify routine on top. Independent of mbedtls TLS config.
//
// Exposed to Python as a single function:
//   ed25519_verify(pubkey: bytes[32], signature: bytes[64], message: bytes) -> bool
//
// Hardening requirements (see concept plan, "Signing"):
//   - RFC 8032 known-answer vectors pass.
//   - Project Wycheproof ed25519 corpus rejected (low-order points, malleability,
//     non-canonical encodings, malleable S, S >= L, all-zero R/S, ...).
//   - Parser fuzzing: malformed inputs fail safely (no crash / OOB read).
//   - Constant-time scalar multiplication.
//   - No dynamic allocation on the verify path.
//
// TODO: implement. This is a placeholder so the module path exists.

// #include "py/runtime.h"
// #include "py/obj.h"
