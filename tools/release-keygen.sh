#!/bin/bash
# One time: make the update signing key pair.
#   tools/release-keygen.sh [PRIVATE_KEY_FILE]    (default ~/.config/pi-carplay-motorrad/update-signing.key)
# The public half goes to image/rootfs/etc/tftpush/update-signing.pub (commit it: every
# image and update carries it). The private half never goes in the repo: store it as the
# GitHub Actions secret UPDATE_SIGNING_KEY, and keep a backup. Lose it, and Pis already out
# there can't be updated by themselves any more (a release could ship a new public key, but
# it has to be signed by the old one).
#   gh secret set UPDATE_SIGNING_KEY < ~/.config/pi-carplay-motorrad/update-signing.key
set -euo pipefail
KEY=${1:-$HOME/.config/pi-carplay-motorrad/update-signing.key}
PUB=$(cd "$(dirname "$0")/.." && pwd)/image/rootfs/etc/tftpush/update-signing.pub
[ -e "$KEY" ] && { echo "$KEY already exists; not overwriting it"; exit 1; }
mkdir -p "$(dirname "$KEY")"
( umask 077; openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 -out "$KEY" )
openssl pkey -in "$KEY" -pubout -out "$PUB"
echo "private key: $KEY"
echo "public key:  $PUB"
echo "next: gh secret set UPDATE_SIGNING_KEY < \"$KEY\""
