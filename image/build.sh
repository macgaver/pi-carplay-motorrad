#!/bin/bash
# Build the Raspberry Pi 5 SD-card image: Raspberry Pi OS Lite (arm64) with tftpush and
# everything it needs baked in, set up to run headless.
#
#   image/build.sh            -> image/build/bmw-carplay-pi5.img.xz
#
# Works on an x86-64 Linux host, also inside an unprivileged container (no loop devices):
# the root filesystem is mounted with fuse2fs and entered with qemu-user.
# Needs: sudo, and the packages  qemu-user-static fuse2fs mtools fdisk xz-utils curl openssl
set -euo pipefail

# Everything below runs in its own mount namespace that only RECEIVES mount events from
# the host ("slave"). Whatever this script mounts or unmounts, the host's mount table
# cannot change. bind_slave() further down is a second guard on top of this one.
if [ -z "${BMW_BUILD_NS:-}" ]; then
    exec sudo unshare -m --propagation slave -- \
        sudo -u "#$(id -u)" BMW_BUILD_NS=1 "$(readlink -f "$0")" "$@"
fi
if [ "$(readlink /proc/self/ns/mnt)" = "$(sudo readlink /proc/1/ns/mnt)" ]; then
    echo "refusing to run in the host mount namespace"; exit 1
fi

HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(dirname "$HERE")
OUT=$HERE/build
WORK=$OUT/work
MNT=$OUT/mnt
CACHE=$OUT/cache

BASE_URL=https://downloads.raspberrypi.com/raspios_lite_arm64/images/raspios_lite_arm64-2026-10-06/2026-10-06-raspios-trixie-arm64-lite.img.xz
BASE_SHA256=483db18a48da399b5b7022ffae9a07bc6d99daab30e593aedac09b69ff422843

# --- settings ------------------------------------------------------------------------
WIFI_COUNTRY=CA WIFI_SSID= WIFI_PASSWORD= HOSTNAME=bmw-carplay PI_USER=pi PI_PASSWORD=
SSH_PUBKEY_FILES= TIMEZONE= EXTRA_MB=768
# shellcheck disable=SC1091
[ -f "$HERE/config.env" ] && . "$HERE/config.env"
[ -n "$TIMEZONE" ] || TIMEZONE=$(cat /etc/timezone 2>/dev/null || echo Etc/UTC)
[ -n "$SSH_PUBKEY_FILES" ] || SSH_PUBKEY_FILES=$(ls "$HOME"/.ssh/id_*.pub 2>/dev/null | tr '\n' ' ' || true)

step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

unmount_all() {
    for m in dev proc sys run tmp; do
        if mountpoint -q "$MNT/$m" 2>/dev/null; then
            sudo mount --make-rslave "$MNT/$m" || true   # never propagate unmounts to the host
            sudo umount -R -l "$MNT/$m" || true
        fi
    done
    if mountpoint -q "$MNT" 2>/dev/null; then
        sudo umount "$MNT" || sudo umount -l "$MNT"
    fi
    # fuse2fs writes the last blocks when it exits: wait for it before touching the file.
    for _ in $(seq 1 120); do pgrep -f "fuse2fs .*$WORK/root.ext4" >/dev/null || break; sleep 1; done
}
trap unmount_all EXIT

for tool in qemu-aarch64-static fuse2fs mcopy sfdisk xz curl openssl e2fsck resize2fs; do
    command -v $tool >/dev/null || { echo "missing tool: $tool (see the header of this script)"; exit 1; }
done

mkdir -p "$WORK" "$MNT" "$CACHE"
unmount_all

# --- base image ----------------------------------------------------------------------
step "Base image"
if [ ! -f "$CACHE/base.img" ]; then
    [ -f "$CACHE/base.img.xz" ] || curl -fL --progress-bar -o "$CACHE/base.img.xz" "$BASE_URL"
    echo "$BASE_SHA256  $CACHE/base.img.xz" | sha256sum -c -
    xz -dk "$CACHE/base.img.xz"
fi
read -r BOOT_START BOOT_SECTORS ROOT_START < <(sfdisk -d "$CACHE/base.img" | awk -F'[=, ]+' '
    /img1/ {bs=$4; bn=$6} /img2/ {rs=$4} END {print bs, bn, rs}')
echo "boot: start $BOOT_START, $BOOT_SECTORS sectors; root: start $ROOT_START"
dd if="$CACHE/base.img" of="$WORK/boot.fat" bs=512 skip="$BOOT_START" count="$BOOT_SECTORS" status=none
dd if="$CACHE/base.img" of="$WORK/root.ext4" bs=1M iflag=skip_bytes skip=$((ROOT_START * 512)) status=none
truncate -s +"${EXTRA_MB}M" "$WORK/root.ext4"
e2fsck -fy "$WORK/root.ext4" >/dev/null || [ $? -le 1 ]
resize2fs "$WORK/root.ext4" >/dev/null

# --- root filesystem -----------------------------------------------------------------
step "Root filesystem: packages and tftpush (arm64 under qemu, takes a few minutes)"
if [ ! -e /proc/sys/fs/binfmt_misc/register ]; then
    sudo mount -t binfmt_misc binfmt_misc /proc/sys/fs/binfmt_misc
fi
if [ ! -e /proc/sys/fs/binfmt_misc/qemu-aarch64 ]; then
    echo ':qemu-aarch64:M::\x7fELF\x02\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x02\x00\xb7\x00:\xff\xff\xff\xff\xff\xff\xff\x00\xff\xff\xff\xff\xff\xff\xff\xff\xfe\xff\xff\xff:/usr/bin/qemu-aarch64-static:F' \
        | sudo tee /proc/sys/fs/binfmt_misc/register >/dev/null
fi
sudo fuse2fs -o rw,allow_other,fakeroot "$WORK/root.ext4" "$MNT"
# Each recursive bind is made a SLAVE of the host tree straight away. Without that, the
# host's / is shared, so unmounting these copies later would also unmount the host's own
# /dev/null, /dev/pts, /sys/fs/cgroup, ... (this happened once: it takes a reboot to undo).
bind_slave() {
    sudo mount --rbind "$1" "$2"
    sudo mount --make-rslave "$2"
}
bind_slave /dev "$MNT/dev"
bind_slave /proc "$MNT/proc"
bind_slave /sys "$MNT/sys"
sudo mount -t tmpfs tmpfs "$MNT/run"
sudo mount -t tmpfs tmpfs "$MNT/tmp"

# overlay + the Python package
[ -f "$MNT/etc/bluetooth/main.conf.stock" ] || sudo cp -a "$MNT/etc/bluetooth/main.conf" "$MNT/etc/bluetooth/main.conf.stock"
sudo cp -r --no-preserve=ownership "$HERE/rootfs/." "$MNT/"
sudo mkdir -p "$MNT/opt/tftpush" "$MNT/var/lib/tftpush"
sudo rm -rf "$MNT/opt/tftpush/tftpush"
sudo cp -r --no-preserve=ownership "$REPO/tftpush" "$MNT/opt/tftpush/tftpush"
sudo find "$MNT/opt/tftpush" -name __pycache__ -prune -exec rm -rf {} +
REV=$(git -C "$REPO" describe --always --dirty 2>/dev/null || echo unknown)
printf 'bmw-carplay image\nbuilt   %s\nsource  %s\nbase    %s\n' "$(date -u +%FT%TZ)" "$REV" "$(basename "$BASE_URL")" \
    | sudo tee "$MNT/opt/tftpush/BUILD" >/dev/null

# DNS for apt inside the chroot (resolv.conf may be a symlink into /run)
sudo mkdir -p "$MNT/run/systemd/resolve" "$MNT/run/NetworkManager"
RESOLV=$(sudo chroot "$MNT" readlink -m /etc/resolv.conf)
sudo mkdir -p "$MNT$(dirname "$RESOLV")"
if [ -f "$MNT$RESOLV" ] && [ ! -L "$MNT/etc/resolv.conf" ]; then sudo cp "$MNT$RESOLV" "$WORK/resolv.conf.orig"; fi
grep -v '^nameserver 127\.' /etc/resolv.conf | sudo tee "$MNT$RESOLV" >/dev/null
grep -q nameserver "$MNT$RESOLV" || echo "nameserver 1.1.1.1" | sudo tee -a "$MNT$RESOLV" >/dev/null

sudo cp "$HERE/chroot-setup.sh" "$MNT/tmp/chroot-setup.sh"
sudo chroot "$MNT" /usr/bin/env TIMEZONE="$TIMEZONE" /bin/bash /tmp/chroot-setup.sh
GROUPS_OK=$(for g in adm dialout cdrom audio users sudo video games plugdev input gpio spi i2c netdev render bluetooth; do
    grep -q "^$g:" "$MNT/etc/group" && printf '%s, ' "$g"; done | sed 's/, $//')
if [ -f "$WORK/resolv.conf.orig" ]; then sudo cp "$WORK/resolv.conf.orig" "$MNT$RESOLV"; rm -f "$WORK/resolv.conf.orig"
elif [ ! -L "$MNT/etc/resolv.conf" ]; then sudo truncate -s 0 "$MNT$RESOLV"; fi
unmount_all
e2fsck -fy "$WORK/root.ext4" >/dev/null || [ $? -le 1 ]

# --- boot partition ------------------------------------------------------------------
step "Boot partition: bmw-carplay.txt and first-boot user-data"
esc() { printf '%s' "$1" | sed -e 's/[\\&|]/\\&/g'; }
sed -e "s|@WIFI_COUNTRY@|$(esc "$WIFI_COUNTRY")|" -e "s|@WIFI_SSID@|$(esc "$WIFI_SSID")|" \
    -e "s|@WIFI_PASSWORD@|$(esc "$WIFI_PASSWORD")|" "$HERE/bootfs/bmw-carplay.txt" | sed 's/$/\r/' > "$WORK/bmw-carplay.txt"

KEYS=
for f in $SSH_PUBKEY_FILES; do [ -f "$f" ] && KEYS+="  - $(head -n1 "$f")"$'\n'; done
GENERATED=
if [ -z "$PI_PASSWORD" ] && [ -z "$KEYS" ]; then
    PI_PASSWORD=$(openssl rand -base64 9); GENERATED=1
fi
{
    echo "#cloud-config"
    echo "# First-boot setup written by image/build.sh (cloud-init reads it once)."
    echo "hostname: $HOSTNAME"
    echo "manage_etc_hosts: true"
    echo "timezone: $TIMEZONE"
    echo "users:"
    echo "- name: $PI_USER"
    echo "  gecos: BMW CarPlay Pi"
    echo "  groups: [$GROUPS_OK]"
    echo "  shell: /bin/bash"
    echo "  sudo: ALL=(ALL) NOPASSWD:ALL"
    if [ -n "$PI_PASSWORD" ]; then
        echo "  lock_passwd: false"
        echo "  passwd: '$(openssl passwd -6 "$PI_PASSWORD")'"
    else
        echo "  lock_passwd: true"
    fi
    if [ -n "$KEYS" ]; then echo "  ssh_authorized_keys:"; printf '%s' "$KEYS"; fi
    echo "ssh_pwauth: $([ -n "$PI_PASSWORD" ] && echo true || echo false)"
    echo "# First boot done: no cloud-init on later boots (it costs ~3 s of every boot)."
    echo "runcmd:"
    echo "- [touch, /etc/cloud/cloud-init.disabled]"
} > "$WORK/user-data"
mcopy -o -i "$WORK/boot.fat" "$WORK/bmw-carplay.txt" ::bmw-carplay.txt
mcopy -o -i "$WORK/boot.fat" "$WORK/user-data" ::user-data
# Faster boot: quieter kernel console; no camera/display probing, boot delay or splash.
mtype -i "$WORK/boot.fat" ::cmdline.txt | tr -d '\r' > "$WORK/cmdline.txt"
if [[ "$WIFI_COUNTRY" =~ ^[A-Za-z]{2}$ ]]; then
    sed -i -e 's/ *cfg80211\.ieee80211_regdom=[A-Za-z]*//g' \
        -e "1s/\$/ cfg80211.ieee80211_regdom=${WIFI_COUNTRY^^}/" "$WORK/cmdline.txt"
fi
grep -qw quiet "$WORK/cmdline.txt" || sed -i '1s/$/ quiet/' "$WORK/cmdline.txt"
mcopy -o -i "$WORK/boot.fat" "$WORK/cmdline.txt" ::cmdline.txt
mtype -i "$WORK/boot.fat" ::config.txt | tr -d '\r' \
    | sed -e 's/^camera_auto_detect=1/camera_auto_detect=0/' -e 's/^display_auto_detect=1/display_auto_detect=0/' > "$WORK/config.txt"
grep -q '^boot_delay=' "$WORK/config.txt" || printf '[all]\nboot_delay=0\ndisable_splash=1\n' >> "$WORK/config.txt"
mcopy -o -i "$WORK/boot.fat" "$WORK/config.txt" ::config.txt

# --- assemble ------------------------------------------------------------------------
step "Assemble and compress"
IMG=$OUT/bmw-carplay-pi5.img
rm -f "$IMG" "$IMG.xz"
dd if="$CACHE/base.img" of="$IMG" bs=512 count="$BOOT_START" status=none
dd if="$WORK/boot.fat" of="$IMG" bs=1M oflag=seek_bytes seek=$((BOOT_START * 512)) conv=notrunc status=none
dd if="$WORK/root.ext4" of="$IMG" bs=1M oflag=seek_bytes seek=$((ROOT_START * 512)) conv=notrunc status=none
echo "$ROOT_START,+" | sfdisk -q --no-reread -N 2 "$IMG"
sfdisk -l "$IMG" | tail -3
xz -T0 -6 -k "$IMG"
( cd "$OUT" && sha256sum "$(basename "$IMG").xz" > "$(basename "$IMG").xz.sha256" )

step "Done"
ls -lh "$IMG.xz"
echo "user: $PI_USER   host: $HOSTNAME.local   Wi-Fi: ${WIFI_SSID:-not set (edit bmw-carplay.txt on the card)}"
[ -n "$KEYS" ] && echo "SSH keys: $SSH_PUBKEY_FILES"
[ -n "$GENERATED" ] && echo "generated password for $PI_USER: $PI_PASSWORD" && echo "$PI_PASSWORD" > "$OUT/generated-password.txt"
exit 0
