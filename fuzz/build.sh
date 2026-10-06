#!/bin/sh
# Build the ECDSA shim fuzzer: mbedtls and the shim both under libFuzzer + ASan + UBSan.
#   fuzz/build.sh <mbedtls source dir> <out dir>
# The mbedtls tree is the firmware's own (lib/micropython/lib/mbedtls in an openmv checkout,
# its generated sources present); it is copied, never built in place.
set -eu
MBED=$1
OUT=$2
HERE=$(cd "$(dirname "$0")" && pwd)
SAN="-fsanitize=address,undefined -fno-sanitize-recover=undefined -g -O1"
mkdir -p "$OUT"
rm -rf "$OUT/mbedtls"
cp -r "$MBED" "$OUT/mbedtls"
make -s -C "$OUT/mbedtls/library" clean >/dev/null 2>&1 || true
make -s -C "$OUT/mbedtls/library" CC=clang CFLAGS="$SAN -fsanitize=fuzzer-no-link -I../include" libmbedcrypto.a
clang $SAN -fsanitize=fuzzer -DOMV_ECDSA_VERIFY_HOST_TEST -I "$OUT/mbedtls/include" \
    "$HERE/../src/openmv_ota/build/device/ecdsa_verify.c" "$HERE/ecdsa_verify_fuzz.c" \
    "$OUT/mbedtls/library/libmbedcrypto.a" -o "$OUT/ecdsa_verify_fuzz"
echo "built $OUT/ecdsa_verify_fuzz"
