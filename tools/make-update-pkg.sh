#!/bin/bash
# Build auto-update.pkg: the gzipped tar a Pi downloads to update itself (tftpush-update).
#   tools/make-update-pkg.sh [OUTDIR]     -> OUTDIR/auto-update.pkg (default image/build)
# The version is the one in the VERSION file at the repo root.
# Layout:
#   VERSION          copy of the repo's VERSION file (the Pi checks it against the release tag)
#   BUILD            same kind of info as /opt/tftpush/BUILD in the image
#   app/tftpush/     replaces /opt/tftpush/tftpush as a whole
#   rootfs/          image/rootfs, copied over / (bootfs settings are never touched)
#   update-hook.sh   image/update-hook.sh, run on the Pi after the copy
set -euo pipefail
REPO=$(cd "$(dirname "$0")/.." && pwd)
OUT=$(mkdir -p "${1:-$REPO/image/build}" && cd "${1:-$REPO/image/build}" && pwd)
VERSION=$(tr -d '[:space:]' < "$REPO/VERSION")
[ -n "$VERSION" ] || { echo "VERSION file is empty"; exit 1; }

STAGE=$(mktemp -d); trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$STAGE/app"
cp -r "$REPO/tftpush" "$STAGE/app/tftpush"
find "$STAGE/app" -name __pycache__ -prune -exec rm -rf {} +
find "$STAGE/app" -name '*.pyc' -delete
cp -r "$REPO/image/rootfs" "$STAGE/rootfs"
install -m 755 "$REPO/image/update-hook.sh" "$STAGE/update-hook.sh"
echo "$VERSION" > "$STAGE/VERSION"
REV=$(git -C "$REPO" describe --tags --always --dirty 2>/dev/null || echo unknown)
printf 'bmw-carplay update %s\nbuilt   %s\nsource  %s\n' "$VERSION" "$(date -u +%FT%TZ)" "$REV" > "$STAGE/BUILD"

MTIME=${SOURCE_DATE_EPOCH:-$(git -C "$REPO" log -1 --format=%ct 2>/dev/null || date +%s)}
tar -C "$STAGE" --sort=name --owner=0 --group=0 --numeric-owner --mtime="@$MTIME" -cf - . \
    | gzip -9n > "$OUT/auto-update.pkg"
echo "$OUT/auto-update.pkg ($(du -h --apparent-size "$OUT/auto-update.pkg" | cut -f1), version $VERSION)"
