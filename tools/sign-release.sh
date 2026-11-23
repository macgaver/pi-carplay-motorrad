#!/bin/bash
# Sign release files with the update signing key: FILE -> FILE.sig (ECDSA P-256 over SHA-256,
# DER). Each signature is checked against image/rootfs/etc/tftpush/update-signing.pub,
# the public key the Pi uses, so a key that doesn't match the image fails here.
#   tools/sign-release.sh KEY.pem FILE...
# Check one by hand:
#   openssl dgst -sha256 -verify image/rootfs/etc/tftpush/update-signing.pub -signature FILE.sig FILE
set -euo pipefail
KEY=${1:?usage: tools/sign-release.sh KEY.pem FILE...}; shift
PUB=$(cd "$(dirname "$0")/.." && pwd)/image/rootfs/etc/tftpush/update-signing.pub
[ -f "$PUB" ] || { echo "missing $PUB (run tools/release-keygen.sh)"; exit 1; }
for f in "$@"; do
    openssl dgst -sha256 -sign "$KEY" -out "$f.sig" "$f"
    openssl dgst -sha256 -verify "$PUB" -signature "$f.sig" "$f" >/dev/null \
        || { echo "signature of $f does not match $PUB: wrong key?"; exit 1; }
    echo "signed $f"
done
