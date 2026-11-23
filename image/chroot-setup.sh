#!/bin/bash
# Runs inside the arm64 root filesystem (chroot, under qemu-user) during image/build.sh.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive LC_ALL=C

printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d && chmod 755 /usr/sbin/policy-rc.d
trap 'rm -f /usr/sbin/policy-rc.d' EXIT

apt-get -q update
apt-get -q -y --no-install-recommends -o Dpkg::Options::=--force-unsafe-io \
    -o Dpkg::Options::=--force-confold install \
    bluez wpasupplicant iw rfkill network-manager dhcpcd-base avahi-daemon openssh-server \
    pipewire pipewire-bin wireplumber libspa-0.2-bluetooth dbus-user-session rtkit \
    python3 python3-pil python3-dbus-next fonts-dejavu-core tcpdump \
    python3-usb python3-av ffmpeg curl ca-certificates openssl
apt-get clean
rm -rf /var/lib/apt/lists/*

# A user whose only job is to run PipeWire + WirePlumber from boot (lingering session),
# so the A2DP / HFP profiles a phone has are there with nobody logged in.
id tftaudio >/dev/null 2>&1 || useradd --system --create-home --home-dir /var/lib/tftaudio \
    --shell /usr/sbin/nologin --groups audio,bluetooth tftaudio
mkdir -p /var/lib/systemd/linger && touch /var/lib/systemd/linger/tftaudio
systemctl --global enable pipewire.socket wireplumber.service

systemctl enable ssh.service bluetooth.service avahi-daemon.service \
    tftpush-bootconf.service tftpush.service tftpush-update.timer
chmod 755 /usr/local/sbin/tftpush-update /etc/NetworkManager/dispatcher.d/90-tftpush-update
# Not needed on a bike box, and they slow the boot or compete with it for the CPU and SD
# card: wait-for-network (nothing needs it), unattended apt (also holds dpkg locks while
# riding), man-db, console keymap, e2scrub.
systemctl disable NetworkManager-wait-online.service apt-daily.timer apt-daily-upgrade.timer \
    man-db.timer keyboard-setup.service e2scrub_reap.service 2>/dev/null || true

[ -n "${TIMEZONE:-}" ] && [ -e "/usr/share/zoneinfo/$TIMEZONE" ] && {
    ln -sf "/usr/share/zoneinfo/$TIMEZONE" /etc/localtime; echo "$TIMEZONE" > /etc/timezone; }

# --- checks: fail the build here rather than on a headless Pi -----------------------
PYTHONPATH=/opt/tftpush python3 -c 'import PIL, dbus_next, usb.core, av, tftpush.auto, tftpush.wifi, tftpush.carplay; tftpush.auto._make_agent(); print("python deps ok,", PIL.__version__, "av", av.__version__)'
cd /tmp
tftpush render --out /tmp/check.jpg >/dev/null && test -s /tmp/check.jpg
tftpush desk-demo --out /tmp/check-frames | tail -1
tftpush carplay-desk --out /tmp/check-carplay | tail -2
rm -rf /tmp/check.jpg /tmp/check-frames /tmp/check-carplay
for b in curl flock openssl hciconfig sdptool bluetoothctl wpa_cli wpa_supplicant nmcli iw rfkill dhcpcd wireplumber pipewire; do
    command -v "$b" >/dev/null || { echo "missing $b"; exit 1; }
done
systemd-analyze verify /etc/systemd/system/tftpush.service /etc/systemd/system/tftpush-bootconf.service \
    /etc/systemd/system/tftpush-update.service /etc/systemd/system/tftpush-update.timer 2>&1 \
    | grep -E "^/etc/systemd/system/tftpush" && { echo "unit file problem"; exit 1; } || true
for u in tftpush tftpush-bootconf ssh bluetooth; do
    systemctl is-enabled "$u.service" >/dev/null || { echo "$u not enabled"; exit 1; }
done
systemctl is-enabled tftpush-update.timer >/dev/null || { echo "tftpush-update.timer not enabled"; exit 1; }
[ -s /etc/tftpush/update-signing.pub ] || echo "WARNING: no update signing key: this image will not update itself"
bash -n /usr/local/sbin/tftpush-update
echo "chroot setup ok"
