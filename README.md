**English** | [Français](README.fr.md)

# pi-carplay-motorrad (100% Wireless)

Apple CarPlay on the 10.25" TFT of a BMW Motorrad bike, through a Raspberry Pi and a
Carlinkit wireless CarPlay dongle. The Pi passes itself off as a phone running the BMW
Connected app: the bike's own nav screens show CarPlay, and the handlebar controls drive it.
No cable to the bike or the iPhone: the iPhone talks to the dongle over wireless CarPlay, and
the Pi talks to the TFT over Bluetooth and Wi-Fi Direct. The program on the Pi is `tftpush`.

![Pi CarPlay screen on the wide nav screen, waiting for the iPhone](docs/screen-wide.jpg)

*The screen the bike shows until your iPhone is connected (wide nav screen). Once it is,
CarPlay replaces it.*

> Experimental hobby project, not affiliated with BMW or Apple. Set it up with the bike
> parked, and keep your eyes on the road.

## What works

Tested on a real bike (10.25" TFT, Pi 4, Carlinkit A15W, iPhone):

- CarPlay on **both** nav screens: the big wide one (1920x464) and the small square box
  (640x640), with switching between them while riding.
- Handlebar controls: UP/DOWN = next/previous item, RIGHT = select, LEFT = back
  (changeable with `KEYMAP`). The controls stay with CarPlay; they don't change the volume.
- CarPlay audio through the bike's Bluetooth audio.
- Hands-off: the Pi starts with the ignition, finds the bike and the iPhone by itself, and
  recovers on its own when Bluetooth or Wi-Fi drop (see [Recovery](#recovery)).
- Pairing locked to your bike: once paired, only that bike may pair again, except in the
  3 minutes after power-up (to pair a new TFT without a laptop).
- Multi-hour rides without a drop.

## What you need

- A **Raspberry Pi 4** (1 GB is plenty: the whole box uses about 265 MB while streaming).
  In Canada: [Raspberry Pi 4 Model B 1 GB at PiShop.ca](https://www.pishop.ca/product/raspberry-pi-4-model-b-1gb/).
  A Pi 5 should work with the same image but hasn't been tried yet; it needs a 5 V / 5 A
  supply, or it limits its USB ports and the dongle may drop out.
- A **Carlinkit wireless CarPlay USB dongle** (CPC200-CCPA family, USB `1314:1520/1521`;
  tested: A15W, which shows up as "AutoKit-xxxx"). For example:
  [CarPlay USB adapter on Amazon](https://a.co/d/0bSMkhVc).
- A fast microSD card of **32 GB or more** (A1/A2, U3), for a quick boot at every ignition.
  For example: [microSD cards on Amazon.ca](https://www.amazon.ca/Micro-SD-Cards/b?ie=UTF8&node=3381334011).
- Power for the Pi. So far it has been tested on a **10,000 mAh USB battery pack**: fine for
  more than 5 hours of riding. Powering it from one of the bike's internal 12 V wires (so it
  follows the ignition) will be tested this winter. Found a good solution? Please share it in
  this repo's [Discussions](https://github.com/macgaver/bmw-carplay/discussions).
- A BMW Motorrad bike with the 10.25" Connectivity TFT and navigation.

## Quick start

1. Build the SD-card image (`image/build.sh`, see [image/README.md](image/README.md)) and
   flash it.
2. Optional: open the card's `bootfs` partition on any computer and put your home Wi-Fi in
   `bmw-carplay.txt` (only for updates and SSH; the bike doesn't need it).
3. Plug the dongle into the Pi and power the Pi.
4. On the bike: Settings > Connections > Mobile device > pair new, pick **Pi CarPlay**, and
   confirm the code.
5. On the iPhone: Settings > Bluetooth, tap the dongle (AutoKit-xxxx), then "Use CarPlay".
   The TFT shows these steps too.
6. Open the bike's navigation. From then on, everything connects by itself at every ride.

Green LED on the Pi: slow blink = waiting to be paired, fast blink = connecting, solid =
picture on the TFT.

## Settings

All settings are in `bmw-carplay.txt` on the card's `bootfs` partition, read at every boot.
The most useful ones:

| Setting | Values | What it does |
| --- | --- | --- |
| `ZOOM` | `small`, `normal` (default), `large` | Size of CarPlay in the small square nav box |
| `SMALL_NAV_VIEW` | `center` (default), `left`, `right` | Which part of the wide picture the square box shows |
| `PAIRING` | `auto` (default), `always`, `off` | When the Pi accepts a new pairing |
| `PAIRING_WINDOW` | seconds, default `180` | How long after power-up another TFT may pair |
| `KEYMAP` | e.g. `UP=left,DOWN=right` | Handlebar key to CarPlay command |
| `CARPLAY_NIGHT` | `0`, `1` | CarPlay dark mode |
| `PHONE_BATTERY`, `PHONE_SIGNAL` | `auto`, `off`, `0`-`100` | What the bike's status bar shows (the dongle doesn't pass the iPhone's real values) |
| `AUTO_UPDATE` | `on` (default), `any`, `off` | At power-up on the home Wi-Fi (`any`: also a hotspot), download a new release and ask on the TFT: Update now / Not now |

The full list, with explanations, is in `tftpush/auto.py` (`DEFAULTS`).

For bigger text on the wide screen, use the iPhone itself: turn Display Zoom off and pick a
larger text size for CarPlay in the iPhone's Accessibility settings. (CarPlay sizes itself to
the screen height, so a different picture size from the Pi doesn't help.)

## Recovery

The Pi never gives up; every failure ends in a new attempt:

| Problem | What the Pi does |
| --- | --- |
| Bike off or out of range | Tries again every 6 s |
| Bluetooth drops while connecting | New attempt at once |
| The TFT takes the connection but ignores it (seen after a fuel stop) | After 3 tries, restarts its Bluetooth radio; after 6, all its Bluetooth software |
| Wi-Fi Direct fails, or drops while riding | Detected within 15 s, new attempt |
| The TFT shows the app but asks for no picture | Asks again after 2 s, new session after 6 s |
| Bluetooth service restarted or adapter lost | tftpush restarts and sets everything up again |
| iPhone not reconnecting to the dongle | Asks the dongle to reconnect every 15 s |
| Any crash | systemd restarts tftpush within 5 s |

## How it works

1. **Bluetooth.** The Pi looks like a phone: class of device `0x5a020c`, media (A2DP/HFP)
   connected, and the BMW SPP record advertised (UUID `c707050e-efae-1449-3913-c191e5bb32dc`).
2. **Ice.** It opens RFCOMM to the TFT's BMW service and speaks ZeroC Ice (protocol 1.0,
   encoding 1.1): `createMySpinICESession`, Hello, country code, then
   `getMySpinICEveh(WLAN_CREDENTIALS_P2P)` returns the TFT's Wi-Fi Direct name and PIN.
3. **Wi-Fi Direct.** It joins the TFT's group with that WPS PIN and reports "connected". A
   USB Wi-Fi adapter is used if present; otherwise the onboard radio is borrowed (the home
   Wi-Fi then drops while the bike link is up).
4. **mySPIN.** The TFT announces its endpoint as JSON on UDP 8158. The Pi connects over TCP,
   runs the Bosch mySPIN handshake and declares the BMW apps.
5. **Screens.** Each nav screen asks for frames on its own (`screen_id`, size). The Pi answers
   each FrameRequest with one JPEG at exactly that size, capped at about 30 KB (what the TFT
   accepts). When the rider switches screens, the Pi pings the other screens (type 168) and
   registers the new one again (type 164), which keeps both screens alive in one session.
6. **CarPlay.** The iPhone connects to the dongle over wireless CarPlay; the dongle sends
   H.264 video at 1920x464 over USB. The Pi decodes it in software (the Pi 4's hardware
   decoder holds back 22 frames) and keeps only the newest picture. The square box gets a
   square cut centred on the map.
7. **Controls.** Handlebar keys arrive as mySPIN CustomData and go to the dongle as CarPlay
   knob commands. The Pi sends `MMC_FOCUS` over Ice when its app is in front, so the TFT stops
   using the keys for volume.

Protocol layouts come from the public reverse-engineering in
[MOTO-HUB](https://github.com/vincenzobpt/MOTO-HUB) and
[open-connected-tft](https://github.com/vandelayautollc/open-connected-tft). This code is an
independent MIT implementation; no code was copied from either project.

## Code map

| Module | Job |
| --- | --- |
| `tftpush/auto.py` | Headless mode: settings, pairing agent and lock, session loop, recovery, LED |
| `tftpush/bt.py` | SDP record, RFCOMM connect with retry, Ice framing and heartbeat |
| `tftpush/ice.py`, `tftpush/ice_dialog.py` | Ice codec; Bluetooth dialogue up to the Wi-Fi Direct credentials |
| `tftpush/wifi.py`, `tftpush/p2p.py` | Wi-Fi interface choice, own `wpa_supplicant`, Wi-Fi Direct join |
| `tftpush/myspin.py`, `tftpush/session.py` | mySPIN packets, TCP session, per-screen frames, stuck-start watchdog |
| `tftpush/carlinkit/` | Carlinkit USB protocol, dongle driver, desk simulator |
| `tftpush/video.py` | H.264 decoding, newest picture, small-nav cut, JPEG per screen size |
| `tftpush/carplay.py` | Ties dongle, video, keys and audio together; status screens |
| `tftpush/cards.py` | The status screens (drawn in code, no image files) |
| `tftpush/carplay_input.py`, `tftpush/carplay_audio.py` | Keys to CarPlay; CarPlay audio to the bike |
| `tftpush/phone_status.py` | Battery and signal for the bike's status bar (hands-free) |
| `tftpush/fake_tft.py` | Desk simulator of the TFT |
| `image/` | Builds the ready-to-flash SD-card image |
| `tools/deploy.sh` | Copies the code to a Pi and restarts the service |

## Desk test (any Linux box, no bike)

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[test,carplay]'
pytest -q
tftpush carplay-desk --out frames   # fake dongle + fake TFT: the whole CarPlay path
```

The square nav box shows the same status screen, stacked:

![Pi CarPlay screen on the square nav box](docs/screen-square.jpg)
