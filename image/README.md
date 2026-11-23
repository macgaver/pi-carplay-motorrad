# Raspberry Pi 5 image (headless)

`image/build.sh` turns Raspberry Pi OS Lite (64-bit, Trixie) into an SD-card image with
tftpush and everything it needs already installed. The Pi needs no keyboard or screen.

```sh
cp image/config.example.env image/config.env   # optional: Wi-Fi, user, SSH key
image/build.sh                                 # -> image/build/bmw-carplay-pi5.img.xz
```

Build host: x86-64 Linux with `sudo` and
`qemu-user-static fuse2fs mtools fdisk xz-utils curl openssl`. It also works inside an
unprivileged container (no loop devices needed). About 6 minutes.

## Use

1. Flash `bmw-carplay-pi5.img.xz` to a card (Raspberry Pi Imager > "Use custom", or
   `xz -dc bmw-carplay-pi5.img.xz | sudo dd of=/dev/sdX bs=4M status=progress`).
2. Optional, for SSH at home: open the card's `bootfs` partition on any computer and put
   your Wi-Fi name and password in `bmw-carplay.txt`. Ethernet works with no setup.
3. Power the Pi near the bike. The first boot takes a minute or two longer.
4. On the bike: Settings > Connections > Mobile device > pair new, pick **Pi CarPlay**, and
   confirm the code on the bike. The Pi confirms its side by itself.
5. Nothing else to do. The Pi remembers the TFT, asks it for its Wi-Fi Direct name and
   PIN over Bluetooth, joins, and pushes the picture. It does the same at every power-up.

Green ACT LED: slow blink = waiting to be paired, fast blink = connecting, solid = picture
on the TFT.

## What is in the image

| Piece | Where | Job |
| --- | --- | --- |
| `tftpush auto` | `tftpush.service` | Pairing agent, remembers the TFT, runs the session forever |
| Boot settings | `tftpush-bootconf.service` | Applies `bmw-carplay.txt` (Wi-Fi, country) at every boot |
| Phone look | `/etc/bluetooth/main.conf`, `bluetoothd --compat` | Class `0x5a020c`, always pairable, `sdptool` works |
| Media profiles | user `tftaudio` (lingering PipeWire + WirePlumber) | A2DP / HFP with nobody logged in |
| Wi-Fi Direct | `/etc/tftpush/wpa_p2p.conf` | Own `wpa_supplicant` on the P2P interface, outside NetworkManager |
| First boot | `user-data` on `bootfs` (cloud-init) | Hostname `bmw-carplay`, user, SSH key; then turns cloud-init off |
| Bike status bar | `/etc/dbus-1/system.d/tftpush-phone-status.conf`, WirePlumber `hfphsp-backend-native-modem` | tftpush stands in for UPower / ModemManager so hands-free tells the bike a battery level and signal (`PHONE_BATTERY`, `PHONE_SIGNAL`) |
| Logs | `/etc/systemd/journald.conf.d/tftpush.conf` | Journal kept on disk (200 MB cap), so logs survive the bike switching off |

### Fast boot

The image already starts tftpush right after Bluetooth, turns cloud-init off after the
first boot, skips camera/display probing, boot delay and splash, uses a quiet kernel console,
and disables services a bike box doesn't need (wait-for-network, automatic apt updates,
man-db, keyboard setup, e2scrub).

The biggest gain can't be set from the SD card. By default the Pi 4 bootloader tries USB
boot **before** the SD card (`BOOT_ORDER=0xf14`). With the dongle and a hub plugged in, that
cost about 27 s on every power-up. Set it once on each Pi:

```sh
sudo rpi-eeprom-config > /tmp/eeprom.conf
sudo sed -i 's/^BOOT_ORDER=.*/BOOT_ORDER=0xf41/' /tmp/eeprom.conf   # SD card first, then USB
grep -q '^DISABLE_HDMI=' /tmp/eeprom.conf || echo 'DISABLE_HDMI=1' | sudo tee -a /tmp/eeprom.conf
sudo rpi-eeprom-config --apply /tmp/eeprom.conf && sudo reboot
```

Measured on the Pi 4 (2026-11-19), reboot to tftpush ready: 54.6 s before, 19.7 s after.

Wi-Fi Direct uses a USB Wi-Fi adapter if one is plugged in. Without one it borrows the
onboard radio, so the home Wi-Fi (and SSH over it) drops while the TFT link is up.

## Releases and self-update

The version lives in one place: the `VERSION` file at the root of the repo (e.g. `0.9.704`).
The TFT shows it on the status screens (bottom left on the wide screen, top right on the
square one), and `tftpush-status` prints it.

To publish a version:

```sh
echo 0.9.705 > VERSION && git commit -am "Version 0.9.705"
git tag v$(cat VERSION) && git push origin main v$(cat VERSION)
```

`.github/workflows/release.yml` then runs the tests, builds the image, and attaches to the
GitHub release of that tag:

| File | What it is |
| --- | --- |
| `pi-carplay-motorrad-<version>.img.xz` (+ `.sig`, `.sha256`) | The SD-card image to flash |
| `auto-update.pkg` (+ `.sig`) | What Pis already out there download to update themselves |
| `SHA256SUMS` | Checksums of all of the above |

The build stops if the tag doesn't match `VERSION`. A tag can be built again from
Actions > Release > Run workflow. Pis only take the **latest** release, so tick
"pre-release" on a release to keep it away from them while you test it.

`auto-update.pkg` is a `.tar.gz` (see `tools/make-update-pkg.sh`): the `tftpush` folder,
everything in `image/rootfs`, and `image/update-hook.sh`. That hook runs on the Pi after
the copy; put whatever the next version needs there (a new apt package, a unit to
enable). It never touches `bmw-carplay.txt`, so the rider's settings stay as they are.

On the Pi, nothing is installed without the rider saying so:

1. At power-up, if the home Wi-Fi (`WIFI_SSID`) is in range, tftpush looks for a newer
   release and downloads it. The BMW link does its Bluetooth part meanwhile; only its
   Wi-Fi Direct step (which takes the same radio on a Pi without a USB adapter) waits.
   The wait ends as soon as the home Wi-Fi connects, fails to connect (wrong password,
   no DHCP), or the Wi-Fi scan shows it isn't around (a few seconds away from home); 25 s
   at most. With no Wi-Fi set up there is no wait.
2. Downloaded (signature checked)? The TFT shows **New version ready** with
   **Update now / Not now**, over CarPlay. UP/DOWN choose, RIGHT confirms, LEFT = not now.
   No answer in 90 s = not now. "Not now" asks again at the next power-up at home.
3. **Update now**: the TFT shows "Updating", tftpush stops, the files are installed, and
   tftpush starts again on the new version (about a minute). If it doesn't stay up, the
   old files are put back and that version isn't offered again.
4. The download failed (rode off, Wi-Fi lost)? CarPlay as usual; a stalled download gives
   up after 20 s.

`tftpush-update.timer` (3 minutes after boot, then every 6 hours) and NetworkManager (a
connection came up) also download a new release when parked at home with no TFT link, so
the next power-up can ask straight away. They never install.

```sh
sudo tftpush-update --check          # is there a newer release?
sudo tftpush-update --now            # download and install now (SSH)
sudo tftpush-update --now --force    # same, off the home Wi-Fi or after a rollback
journalctl -u tftpush-update -u 'tftpush-install-*'   # what it did
```

The release images have no Wi-Fi, SSH key or password in them. For SSH, edit `user-data`
on the card's `bootfs` partition before the first boot and add your key under
`ssh_authorized_keys:`. For your own Wi-Fi and key built in, use `image/build.sh` with a
`config.env`.

### Signing key (one time)

```sh
tools/release-keygen.sh      # private key in ~/.config/pi-carplay-motorrad/, public key in image/rootfs
gh secret set UPDATE_SIGNING_KEY < ~/.config/pi-carplay-motorrad/update-signing.key
```

Commit `image/rootfs/etc/tftpush/update-signing.pub`. Keep a backup of the private key
somewhere safe: without it, Pis already out there can't update themselves any more.
Check a file by hand:
`openssl dgst -sha256 -verify image/rootfs/etc/tftpush/update-signing.pub -signature auto-update.pkg.sig auto-update.pkg`

## Looking inside

```sh
ssh pi@bmw-carplay.local
tftpush-status                 # one screen: services, TFT address, radios, last log lines
journalctl -u tftpush -f       # live
ls /var/lib/tftpush/captures   # hex log per session: send these back after a bike test
```

To pair with a different bike: `sudo rm /var/lib/tftpush/tft-address`, remove the old
device with `bluetoothctl remove <addr>`, reboot.

## Status

Checked at build time, inside the image under emulation: Python dependencies, `tftpush
render`, the full `desk-demo`, unit files, and that every needed tool is present.
Not checked: anything that needs the real hardware. No Pi has booted this image yet, and
the Bluetooth and Wi-Fi Direct steps have never met a real TFT.
