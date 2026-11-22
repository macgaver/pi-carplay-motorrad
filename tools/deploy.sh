#!/bin/bash
# Copy the tftpush package to the Pi and restart the service. Copies the whole folder
# (including tftpush/carlinkit/ and the test clip), so no file is forgotten.
#   tools/deploy.sh [pi@192.168.2.167] [--no-restart]
set -euo pipefail
HOST="${1:-pi@192.168.2.167}"
RESTART=1
[ "${2:-}" = "--no-restart" ] && RESTART=0
cd "$(dirname "$0")/.."
tar -C . -cz --exclude=__pycache__ --exclude='*.pyc' tftpush | ssh "$HOST" "
  set -e
  sudo mkdir -p /opt/tftpush
  sudo tar -C /opt/tftpush -xzf -
  sudo chown -R root:root /opt/tftpush/tftpush
  sudo python3 -m compileall -q /opt/tftpush/tftpush
  echo deployed: \$(cd /opt/tftpush && find tftpush -name '*.py' | wc -l) python files
  if [ $RESTART = 1 ]; then sudo systemctl restart tftpush && sleep 2 && echo service: \$(systemctl is-active tftpush); fi
"
