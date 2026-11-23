#!/bin/bash
# Shipped inside auto-update.pkg. Runs on the Pi as root during a self-update, after the
# new files are in place and before tftpush restarts. Put the one-off steps the NEXT
# release needs here: new apt packages, new units to enable, old files to remove.
# It must be safe to run more than once (a Pi may skip releases). A non-zero exit rolls
# the whole update back.   Environment: OLD_VERSION, NEW_VERSION.
# tftpush is stopped while this runs and the Wi-Fi was just handed back to NetworkManager:
# wait for it (nm-online -q -t 30) before anything that needs the internet.
set -euo pipefail

systemctl enable tftpush.service tftpush-bootconf.service >/dev/null 2>&1 || true
# Running from now on, not only from the next boot (a Pi updated from a build without it).
systemctl enable --now tftpush-update.timer >/dev/null 2>&1 || true

# Example, for a release that needs a new package:
#   dpkg -s foo >/dev/null 2>&1 || {
#       apt-get -q update && apt-get -q -y --no-install-recommends install foo; }
