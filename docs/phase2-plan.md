# Phase 2 plan: CarPlay from a USB Carlinkit dongle to the BMW TFT

Goal: the Pi takes the live CarPlay picture from a Carlinkit CPC200-CCPA plugged into its
USB port, pushes it to the BMW TFT over the mySPIN link that Phase 1 already built, and sends
the bike's handlebar controls back to the dongle so the rider can drive CarPlay.

```
iPhone ))) wireless CarPlay ))) Carlinkit ==USB==> Pi 5 ==Wi-Fi Direct / mySPIN==> BMW TFT
                                    ^                                                  |
                                    +---------- handlebar keys (CustomData) -----------+
```

## Status (2026-11-19)

All the code for steps P1 to P10 is written and desk-tested (57 tests, including a full fake
dongle -> Pi -> fake TFT run with keys going back). It's deployed on the Pi with pyusb, PyAV
and ffmpeg installed. What can't be done without the dongle and the bike is the testing in
section 8 below.

| Step | Code | Tested |
| --- | --- | --- |
| P0 bench setup | packages installed on the Pi | dongle and power test pending |
| P1 fps + size probe | `TFTPUSH_FPS_PROBE` mode, per-screen fps/size log | **done on the bike**, see below |
| P2 dongle bring-up | `tftpush dongle-probe` | with the dongle: pending |
| P3 decode benchmark | done on the Pi with a synthetic clip (1.3) | real stream: pending |
| P4 codec | `carlinkit/protocol.py` | unit tests |
| P5 driver | `carlinkit/dongle.py`, `fake_dongle.py` | lifecycle tests on the fake |
| P6 video into the session | `video.py`, `carplay.py`, `session.py` | desk end to end |
| P7 keys | `carplay_input.py` | unit tests; the real map is pending |
| P8 headless | `auto.py` settings, CarPlay task, LED, `tools/deploy.sh`, image build | on the Pi without a dongle |
| P9 audio | `carplay_audio.py` (off by default) | unit test with a file player |
| P10 docs | README, this file | |

### Bike results (2026-11-19, no dongle yet)

- **Frame size limit (P1), wide screen, real frame sizes:** 13-26 KB smooth at ~14-15
  frames/s; ~48 KB shows but stalls now and then (9-13 frames/s); ~98 KB corrupts the picture.
  The old ~19 KB cap was a side effect of the alpha4 wrong-size bug. Default is now
  `JPEG_MAX_BYTES=32000`. The TFT answers each frame and asks again within 1-9 ms.
- **Optimised Huffman tables** (`JPEG_OPTIMIZE=1`) work on the TFT.
- **CarPlay-like video through the whole service** (`TFTPUSH_CARPLAY_FAKE` with
  `tools/make_carplay_clip.py`'s day map, 1920x464): 8-9 frames/s at ~30 KB per frame, 43 ms
  from request to frame. The Pi's decode + JPEG work is now the limit, not the TFT; preparing
  the next JPEG ahead of the request would raise it.
- **Keys:** every handlebar key arrived and went to the (fake) dongle as the mapped CarPlay
  command. The real effect needs the iPhone.
- **Audio:** a test tone over the bike's A2DP link plays, but is **choppy while Wi-Fi Direct
  is up and clean when it's down**. Cause: Wi-Fi Direct runs on 2.4 GHz (channel 11) on the
  Pi 4's built-in chip, which also does Bluetooth. Fix: a **USB Wi-Fi adapter with P2P client
  support** for Wi-Fi Direct (`wifi.choose` already prefers one), leaving the built-in chip to
  Bluetooth. The bike's A2DP output also came up at volume 0; the audio code now raises it.
  CarPlay audio is on by default (`CARPLAY_AUDIO=on`) and runs `pw-play` in the audio user's
  PipeWire session (the root service can't reach it otherwise).
- **Bluetooth bond lost:** the service made the Pi non-pairable during a session, so a
  mid-session re-authentication by the bike came out non-bonding and the saved key was
  dropped; the bike then asked to pair on every connection. Fixed (alpha25): the Pi stays
  pairable and only hides. Reconnect after a restart is now ~27-29 s with no pairing.
- **Still open:** when the Bluetooth link to the TFT dies mid-handshake
  ("socket.send() raised exception"), the session waits the full 120 s credentials timeout
  before retrying. It should give up as soon as the link is gone.

Status legend: **[known]** = checked in our code, our bike logs, or the node-CarPlay source;
**[verify]** = believed but must be checked on real hardware before building on it.

---

## 1. What we already know

### 1.1 The BMW side (Phase 1, live on the bike)

- **[known]** The TFT only accepts JPEG frames, one per FrameRequest (type 25 -> type 26), at
  the size each screen asks for: 1920x464 (wide) or 640x640 (square). The reference
  `libclusterlink.so` also sends JPEG. The TFT reports `compression caps 5` (bit 0 = JPEG,
  bit 2 = unknown). There is no evidence it takes H.264, so plan on **decode, resize, JPEG**.
- **[known]** It's a pull model: the TFT asks, we answer, it asks again. Bike logs from
  2026-11-17 show requests ~150 ms apart while keys were pressed (fresh frame every time) and
  ~1.3 s apart when idle (we hold the request up to `stale_resend = 1.0 s` when nothing has
  changed). So the TFT re-asks ~150-300 ms after each frame: **about 3-7 fps today**, never
  measured properly.
- **[verify, critical]** **Frame size cap.** `FrameBuilder.jpeg()` lowers the JPEG quality
  until a frame is under `max_bytes = 18000`. It was added in alpha4 (2026-11-16) after the
  bike showed a ~19 KB frame and ignored a ~38 KB one, on the square screen. That was the same
  day we found we were answering at the wrong frame size, so the cap may be real or may be a
  side effect of that bug. A busy 1920x464 picture is ~150-220 KiB at normal quality. If the
  cap is real, CarPlay maps would need very low quality. P1 must find the largest frame each
  screen accepts.
- **[known]** Keys arrive as CustomData: OK 12, Back 14, wheel +/- 20/21, UP/DOWN/LEFT/RIGHT
  30-33, each with PRESS and RELEASE. Long-press timing is therefore possible.
- **[known]** Limitation: the TFT starts a screen's app once per mySPIN connection. Moving
  between the square and wide screens mid-session doesn't come back without reconnecting.

### 1.2 The Carlinkit side (from node-CarPlay `src/modules/*`)

- **[known]** USB ids `1314:1520` and `1314:1521`. Configuration 1, interface 0, one bulk IN
  and one bulk OUT endpoint.
- **[known]** 16-byte header on every message, little-endian:
  `magic 0x55AA55AA | u32 payload length | u32 type | u32 (type XOR 0xFFFFFFFF)`.
- **[known]** Message types: Open 0x01, Plugged 0x02, Phase 0x03, Unplugged 0x04, Touch 0x05,
  VideoData 0x06, AudioData 0x07, Command 0x08, LogoType 0x09, BluetoothAddress 0x0A,
  BluetoothPIN 0x0C, BluetoothDeviceName 0x0D, WifiDeviceName 0x0E, DisconnectPhone 0x0F,
  BluetoothPairedList 0x12, ManufacturerInfo 0x14, CloseDongle 0x15, MultiTouch 0x17,
  HiCarLink 0x18, BoxSettings 0x19, MediaData 0x2A, SendFile 0x99, HeartBeat 0xAA,
  SoftwareVersion 0xCC.
- **[known]** Open payload is 7 x u32: `width, height, fps, format(5), packetMax(49152),
  iBoxVersion(2), phoneWorkMode(2)`. **The phone renders CarPlay at the width/height we ask
  for**, so we can request roughly the BMW screen size and never decode a large picture.
- **[known]** VideoData payload: `u32 width, u32 height, u32 flags, u32 length, u32 unknown`,
  then the H.264 Annex-B stream starting at offset 20.
- **[known]** AudioData payload: `u32 decodeType, f32 volume, u32 audioType`, then PCM or a
  command byte. decodeType 1/2 = 44.1 kHz stereo, 3 = 8 kHz mono, 4 = 48 kHz stereo,
  5 = 16 kHz mono, 6 = 24 kHz mono, 7 = 16 kHz stereo (all 16-bit).
- **[known]** Startup used by node-CarPlay: open the device, USB reset, wait 1-3 s for it to
  come back, open again, claim the interface, then send:
  SendFile `/tmp/screen_dpi`, Open, SendFile `/tmp/night_mode`, `/tmp/hand_drive_mode`,
  `/tmp/charge_mode`, `/etc/box_name`, BoxSettings JSON
  (`mediaDelay, syncTime, androidAutoSizeW/H`), Command wifiEnable(1000), wifi5g(25) or
  wifi24g(24), mic(7) or boxMic(15), audioTransferOff(23); after 1 s, Command
  wifiConnect(1002). **HeartBeat (empty) every 2 s.** While CarPlay is connected, send
  Command `frame`(12) every 5 s to get a fresh keyframe.
  SendFile payload: `u32 name length | name + NUL | u32 content length | content`.
- **[known]** Button commands: left 100, right 101, selectDown 104, selectUp 105, back 106,
  up 113, down 114, home 200, play 201, pause 202, playOrPause 203, next 204, prev 205,
  acceptPhone 300, rejectPhone 301, siri 5, requestVideoFocus 500, releaseVideoFocus 501.
  Touch (0x05): `u32 action (Down 14, Move 15, Up 16) | u32 x | u32 y | u32 flags`, x and y
  scaled to 0..10000.
- **[verify]** Exactly what left/right (100/101) do in CarPlay: knob rotation or left/right
  nudge. That decides the key mapping in step P7.

### 1.3 The Pi (checked on the box itself, 2026-11-19)

- **[known]** The box is a **Raspberry Pi 4 Model B** (Cortex-A72, 4 cores, 1.5 GHz, 4 GB),
  running Debian 13 trixie, kernel 6.18. It is **not a Pi 5**; earlier notes were wrong.
- **[known]** The Pi 4 has a hardware H.264 decoder (`/dev/video10`, `h264_v4l2m2m`), but
  **we don't use it.** Measured on the Pi with a 1920x480 clip: the software decoder takes
  3.4 ms per frame and gives out each picture one frame late. The hardware decoder takes
  6.9 ms and holds back up to 22 frames before giving any out. CarPlay sends frames only
  when the screen changes, so the TFT would sit on an old picture. Software is the default
  (`CARPLAY_DECODER=sw`).
- **[known]** Turning a decoded frame into RGB costs 13 ms when it's already the screen's
  size and 35 ms when it also has to be rescaled. So the phone is asked for exactly
  **1920x464** by default (`CARPLAY_SIZE`). If CarPlay refuses that shape, fall back to
  1920x480.
- **[known]** JPEG at 1920x464 on a CarPlay-like map picture: q80 12.5 ms / 72 KB,
  q40 12 ms / 43 KB. With optimised Huffman tables it's +5 ms and 5-10% smaller, and on flat
  pictures much smaller (a flat frame goes from 14.5 KB to 5.5 KB). **Under an 18 KB cap that
  map picture only fits at quality 10.** So the real cap (P1) decides how CarPlay looks.
- **[known]** CPU cost measured on this Pi (Pillow 11.1 with libjpeg-turbo, a synthetic busy
  frame with stripes, so worse than a real CarPlay screen):

  | Output | Resize from 1920x480 | JPEG q50 / q70 / q85 | Size q70 |
  | --- | --- | --- | --- |
  | 1920x464 | ~14 ms | 15 / 14 / 14 ms | ~174 KiB |
  | 640x640 | ~16 ms | 6 ms | ~73 KiB |

  So about 30 ms of one core per wide frame including the resize, about 14 ms without it.
  The CPU is not the limit at the 3-10 fps the TFT is likely to allow.
- **[known]** Nothing missing for power so far: `get_throttled = 0x0` on the bench, 35 °C.
  The Pi 4 shares 1.2 A across its USB-A ports, enough for the dongle. Today's only USB
  device is a VIA hub (`2109:3431`); the dongle isn't connected yet.
- **[known]** Not installed: `pyusb`, `PyAV`, `ffmpeg`. The apt lists are empty (cleaned
  when the image was built), so `apt update` must run first (P0).
- **[verify]** The bike's USB socket must hold the Pi plus the dongle without brownouts, in
  the closed box.

## 2. The real risks: frame size and frames per second over mySPIN

How good CarPlay looks depends on two TFT limits, not on decoding: the largest JPEG it
accepts (the possible ~19 KB cap in 1.1) and how fast it asks for frames. The current 3-7 fps estimate is fine for maps and menus and choppy for scrolling.
Measure this first (step P1). Nothing else should be built until it's measured.

Ways to raise it, in order:

1. Answer every FrameRequest immediately with the newest frame. No `stale_resend` wait when
   new video is available.
2. Smaller JPEGs: lower quality (60-75), 4:2:0 subsampling, libjpeg-turbo through Pillow.
   A wide frame is ~150-220 KiB at q50-85 on a busy test picture (section 1.3); P1 shows
   whether size matters to the TFT or only the round trip does.
3. Have the dongle render at exactly the TFT's request size, so we never resize.
4. Only if still too slow: try the unknown compression bit 2 from `caps 5` (raw or another
   codec) with one probe frame. Research only, with no plan dependent on it.

## 3. Software design (all inside `tftpush/`, Python stays the language)

Python handles the plumbing. The heavy work runs in native code: libusb (pyusb), libavcodec
(PyAV) and libjpeg-turbo (Pillow).

| New / changed module | Job |
| --- | --- |
| `tftpush/carlinkit/protocol.py` | Pure codec, no I/O, the same pattern as `myspin.Protocol`: header pack/check, builders for Open, SendFile, BoxSettings, Command, Touch, HeartBeat; parsers for Plugged, Unplugged, Phase, VideoData, AudioData, Command, Opened, BoxSettings, SoftwareVersion, DisconnectPhone. Unknown types become an `unhandled` event, never a crash. |
| `tftpush/carlinkit/dongle.py` | USB transport and lifecycle. Find the device, reset it and wait for it to come back, claim, init sequence, 2 s heartbeat, keyframe every 5 s and on demand. A reader thread does blocking pyusb bulk reads (header, then payload) and hands messages to asyncio with `call_soon_threadsafe`. Watchdogs: USB errors, no message for N s (same lesson as alpha20), unplug: close, reset, start again with backoff. |
| `tftpush/carlinkit/fake_dongle.py` | Desk simulator that speaks the same protocol over an in-memory pipe and plays a short H.264 test clip from `tests/data/`. Lets every layer above run with no hardware. |
| `tftpush/video.py` | `LiveVideo` frame source. PyAV H.264 decoder in a worker thread (PyAV releases the GIL): software `h264` in low-delay mode by default; `h264_v4l2m2m` only by setting (see 1.3 for why). Every packet is decoded (H.264 needs its reference frames) but only the newest picture is kept, with a version counter. `jpeg(w, h)` does swscale resize and JPEG on demand, cached per `(version, w, h)`. Needs a keyframe to start: on attach or decoder error it asks the dongle for one. |
| `tftpush/session.py` | Swap the hard-wired `FrameBuilder` for a small frame-source interface (`version`, `jpeg(w, h)`, `add_key`). `FrameBuilder` already fits it. New `notify_new_frame()` answers waiting FrameRequests as soon as a new picture is decoded. Add a "waiting for iPhone" source used until the first video frame and whenever CarPlay drops. |
| `tftpush/carplay_input.py` | BMW `KeyInput` -> dongle Command or Touch, including long-press detection, configurable from the config file. |
| `tftpush/auto.py` | Start the dongle link at boot as its own task, in parallel with the BMW link and independent of it. Either side can drop and reconnect without taking the other down. |
| `tftpush/capture.py` | Also log dongle messages (video payload cut short) and optionally dump the raw `.h264`, so bugs can be replayed at the desk. |
| `image/chroot-setup.sh`, `pyproject.toml` | Add `python3-usb`, `python3-av`, `ffmpeg` (tools only). Add a `[carplay]` optional extra. |

New settings in `bmw-carplay.txt`: `CARPLAY=auto|off`, `CARPLAY_SIZE` (default: the wide
screen size), `CARPLAY_FPS`, `CARPLAY_DPI`, `JPEG_QUALITY`, `CARPLAY_WIFI=5ghz|24ghz`,
`KEYMAP` overrides.

**Which BMW screen shows CarPlay:** the phone renders one picture. Default: render at the wide
screen's size and send it there as is. The square screen gets a center-cropped or scaled
version from the same frame. Rendering a different size per screen would mean re-sending
Open and restarting the phone stream, so that waits until the basic version works.

## 4. Steps, in order, each with a gate

### P0. Bench setup (½ day)
- CPC200-CCPA, an iPhone with wireless CarPlay, a USB power meter.
- On the Pi: `apt update`, then `apt install python3-usb python3-av ffmpeg`. Check that
  `ffmpeg -decoders | grep v4l2m2m` lists `h264_v4l2m2m`.
- Note the dongle firmware version (SoftwareVersion 0xCC) in the capture log.
- **Gate:** `lsusb` shows `1314:1520/1521` on the Pi. Pi + dongle on the bike's USB socket
  shows no undervoltage (`vcgencmd get_throttled` = `0x0`) for 30 minutes.

### P1. Measure the mySPIN frame rate (needs the bike, not the dongle) (1 day)
- Add a `TFTPUSH_FPS_PROBE` mode. Answer every FrameRequest right away with a new frame (a
  counter plus a moving bar). Log fps per screen, JPEG bytes, and the gap between our Frame
  and the next FrameRequest. Try quality 50/70/85 on both screens.
- **Size test in the same mode:** step frame sizes 15, 20, 30, 50, 100 and 200 KB (busy test
  picture, chosen quality) on each screen. The rider says which numbers still show; the log
  says whether a FrameRequest still follows. The largest accepted size per screen sets
  `max_bytes` (section 6).
- **Gate:** written fps numbers and the largest accepted size per screen in this file.
  8 fps or more: carry on as planned. 4-8 fps: carry on, and accept CarPlay as a nav/menu
  display. Under 4 fps: stop and investigate section 2 item 4 before going further. If the
  cap really is ~19 KB on the wide screen: try dark-mode CarPlay (night mode, which
  compresses much better), a lower dongle render size scaled up by us, and stronger chroma
  subsampling, and pick the one that looks best on the bike.

### P2. Dongle bring-up spike on the Pi (1-2 days)
- `tools/dongle_probe.py` (standalone, not imported by the service): reset, open, init
  sequence, heartbeat; log every message with its type; write VideoData to `out.h264`.
- Pair the iPhone with the dongle; check Plugged (phoneType 3 = CarPlay) and that VideoData
  flows.
- Resolution test: ask Open for 1920x464, 1920x480, 1600x480, 1280x480, 800x480. Record what
  VideoData actually reports and how CarPlay lays out (`ffprobe` / `ffplay` on the desktop).
  CarPlay may refuse very wide ratios.
- Try DPI values for readability on a moving bike.
- Keep a few captures in `tests/data/` (cut short) for the unit tests.
- **Gate:** the iPhone connects without help and `out.h264` plays. A chosen CARPLAY_SIZE is
  written down.

### P3. Decode benchmark on the Pi 4 (½ day)
- JPEG and resize cost is already measured (section 1.3). What's left: on the captured
  stream, compare `h264_v4l2m2m` (hardware) with software `h264` decode. Measure time per
  frame, CPU %, and how long the first picture takes after a keyframe. Watch temperature
  (`vcgencmd measure_temp`) over 10 minutes inside the closed box.
- **Gate:** decode plus JPEG under 50 ms per frame on 1.5 cores or less, and no thermal
  throttling. If not met: lower the dongle fps (Open `fps`) or the render size.

### P4. Carlinkit protocol codec plus tests (1 day)
- `carlinkit/protocol.py` from section 1.2. Tests: build each outgoing message and compare
  bytes to known byte strings; parse the captures from P2; bad magic or type check raises a
  clear error; unknown type does not crash; a header split across two USB reads is put back
  together.
- **Gate:** `pytest` green, codec reads every message in the P2 captures.

### P5. Dongle driver, fake dongle, lifecycle tests (2 days)
- `carlinkit/dongle.py` and `fake_dongle.py`. Tests with the fake: init order matches
  section 1.2; a heartbeat goes out every 2 s; Unplugged and a USB error both lead to reset
  and start again; silence trips the watchdog; the BMW session keeps running while the dongle
  restarts.
- **Gate:** on the bench, unplug and replug the dongle, and turn the iPhone's Wi-Fi off and
  on, ten times each. It comes back every time without restarting the service.

### P6. Video into the BMW session (2 days)
- `video.py` plus the frame-source interface in `session.py`. Desk end-to-end test: fake
  dongle -> LiveVideo -> Session -> `FakeMySpinTft`; check that the frames reaching the fake
  TFT change and have the right sizes.
- On the bike: CarPlay home screen on the wide screen, then on the square one. Measure fps
  and glass-to-glass delay (film the phone and the TFT together).
- **Gate:** CarPlay visible on the bike at the P1 fps, under 500 ms delay, 30 minutes with no
  freeze.

### P7. Handlebar keys to CarPlay (1-2 days)
- First check, with `TFTPUSH` key logging, which BMW keys reach us while the CarPlay picture
  is showing. The TFT may keep the wheel or Back for its own menus.
- Starting map (adjust after testing what 100/101 do):

  | BMW key | Dongle command |
  | --- | --- |
  | Wheel + / - | right 101 / left 100 (knob turn) |
  | OK press / release | selectDown 104 / selectUp 105 |
  | Back (short) | back 106 |
  | Back (held > 0.8 s) | home 200 |
  | UP / DOWN | up 113 / down 114 |
  | LEFT / RIGHT | left 100 / right 101 if the wheel doesn't reach us, otherwise test a touch swipe |

- Unit tests for the map and long-press timing. Keep the debug overlay but turn it off by
  default when CarPlay is live.
- **Gate:** without touching the phone, the rider can open Maps, pick a recent destination,
  start guidance, and get back to the home screen.

### P8. Headless integration (1-2 days)
- `auto.py` wiring, config keys, image build dependencies, `systemd` unchanged (one service).
- Measure power-on to CarPlay-on-TFT time. The dongle boot and iPhone connect run alongside
  the BMW Bluetooth / Wi-Fi Direct link, so the total should be about whichever side is
  slower, not both added together.
- **Gate:** from ignition on, with no hand on the Pi or phone, CarPlay is on the TFT. Ten
  cold starts in a row, times written here.

### P9. Audio (its own milestone after P8)
- AudioData (media and nav prompts) -> PipeWire -> the A2DP link the Pi already has to the
  bike -> helmet. Use AudioCommand nav start/stop to duck music.
- Siri and phone calls need the microphone path (send AudioData to the dongle) and HFP
  hand-off. Kept out of Phase 2 on purpose, and noted.

### P10. Hardening and docs (1 day)
- Long run: 2 hours riding or bench soak. Check memory growth, CPU, temperature, and that
  every watchdog shows up in the logs.
- README "How it works" and Milestones; this file updated with the measured numbers.

## 5. Risks

| Risk | Effect | Action |
| --- | --- | --- |
| TFT drops frames over ~19 KB | Very blurry CarPlay on the wide screen | P1 size test; night mode, a lower render size, subsampling |
| mySPIN JPEG pull rate is low | Choppy CarPlay | P1 measures it first; section 2 options |
| CarPlay rejects ~4:1 sizes | Bars or a squashed picture on the wide screen | P2 resolution test; fall back to the closest accepted size and letterbox |
| Wheel/Back held by the TFT | Fewer controls | P7 check first; use UP/DOWN/LEFT/RIGHT and long-press |
| Bike USB power is too weak | Resets at start or while riding | P0 power test; powered USB hub or a better supply in the box |
| Dongle firmware differences | Init sequence not accepted | Log the firmware version; keep the init sequence in one place |
| 2.4/5 GHz radio clash (dongle Wi-Fi next to the Pi's Wi-Fi Direct) | Drops on one link or the other | `CARPLAY_WIFI` setting; check channels in P2 and P6 |
| Heat in a closed box | Throttling, then fps drop | P3 thermal test; fps cap |
| `h264_v4l2m2m` not in the trixie build of PyAV/ffmpeg, or flaky | Software decode on the A72 | P0 check; software fallback in `video.py`; keep the render size small |

## 6. Changes to the existing Python code, file by file

New modules are in section 3. These are the changes to code that exists today.

### `tftpush/session.py`
- `Session.__init__(make_builder, ...)`: take a **frame source** instead of a `FrameBuilder`.
  It's a small Protocol with `version`, `jpeg(w, h) -> bytes`, `add_key(key)`.
  `FrameBuilder` already has all three; `LiveVideo` and a status card will too.
- `_try_send_frame()` encodes JPEGs **inside the asyncio loop**, one per waiting screen.
  That's ~15-30 ms each on this Pi, which delays reading keys and pings. Move encoding to a
  worker (`asyncio.to_thread`), one encode in flight per screen, and send the result when it
  comes back if that screen's request is still open.
- `_sent_version` is a single number for all screens. Make it per `screen_id`, so a new video
  frame goes to every waiting screen, not just the first.
- Add `notify_new_frame()`. The video source calls it (through `loop.call_soon_threadsafe`)
  when a picture is decoded, so waiting FrameRequests are answered at once instead of on the
  next 0.2 s `_tick`.
- Keep `stale_resend` as the fallback when the picture hasn't changed (a still CarPlay
  screen).
- Log per screen every 10 s: fps sent, mean JPEG bytes, mean request-to-frame delay. That's
  the P1 numbers, and they stay useful afterwards.
- Leave the Phase 1 experiments (`TFTPUSH_PUSH_FRAMES`, `TFTPUSH_SCREEN_ANNOUNCE`,
  `TFTPUSH_REANNOUNCE`) off; don't build on them.

### `tftpush/frames.py`
- `FrameBuilder.jpeg()` can encode **up to 8 times** per frame (quality 80 -> 22 until under
  `max_bytes`). Fine for a still image, too slow for video (up to ~110 ms on the Pi 4). Move
  this into a shared helper `encode_jpeg(img, max_bytes, state)` that remembers the last
  good quality per `(w, h)`. It encodes once normally, steps down when a frame is too big,
  and creeps back up when there's room. `FrameBuilder` and `LiveVideo` both use it.
- `max_bytes` becomes a setting (`JPEG_MAX_BYTES`), set from the P1 result, per screen if the
  two screens differ.
- Add `status_card(width, height, text)` for "Waiting for iPhone...", "CarPlay disconnected"
  and "Dongle not found" (same style as `default_test_image`).
- The key overlay in `render()`: off by default on live video, kept for debugging behind a
  setting.

### `tftpush/myspin.py`
- No change needed for Phase 2. `send_frame()` already answers per screen at the requested
  size with the right compression byte. The only possible change is a probe for compression
  bit 2, and only if P1 fails (section 2 item 4).

### `tftpush/cli.py`
- `run_once(..., image=...)` builds the frame source inside itself (`make(scr)` closure).
  Change it to take `source_factory: Callable[[Screen], FrameSource]` and
  `on_key: Callable[[KeyInput], None]`, and pass both to `Session`. `image=` stays as the
  simple case for `tftpush run`.
- New subcommands:
  - `tftpush dongle-probe [--size WxH] [--fps N] [--dump out.h264]` (P2, bench, no bike)
  - `tftpush carplay-desk` (fake dongle -> LiveVideo -> fake TFT, the Phase 2 version of
    `desk-demo`)
- `TFTPUSH_FPS_PROBE` (P1): a frame source that draws a counter and moving bar, plus the
  byte-size steps.

### `tftpush/auto.py`
- `DEFAULTS`: add `CARPLAY`, `CARPLAY_SIZE`, `CARPLAY_FPS`, `CARPLAY_DPI`, `CARPLAY_WIFI`,
  `JPEG_MAX_BYTES`, `JPEG_QUALITY`, `KEYMAP`, `OVERLAY`. `parse_config` already accepts
  unknown keys, so only the defaults and their use are new.
- `Daemon`: create the `Dongle` and `LiveVideo` **once**, in `run()`, outside
  `_session_loop`. Each BMW reconnect (`run_once`) then reuses the live source instead of
  restarting CarPlay. Watch the dongle task the same way `_session` is watched now (restart
  it if it stops).
- `_session_loop` passes `source_factory` (live source with status-card fallback) and
  `on_key` (-> `carplay_input`) to `run_once`.
- When a new BMW session reaches streaming, ask the dongle for a keyframe (Command `frame` 12)
  so the first picture appears at once.
- `Led.show()`: add a state for "BMW up, waiting for iPhone" vs "CarPlay live", for checks
  without a laptop.

### `tftpush/capture.py`
- Add a `dongle` channel: every message type and length; VideoData and AudioData headers
  only, never the full payload.
- Optional raw dump: `session-*.h264` next to the log (setting `CAPTURE_H264=1`), for
  replaying video bugs at the desk.

### `tftpush/fake_tft.py`
- Option to enforce a `max_frame_bytes` (drop bigger frames like the bike may), and record
  the time of each frame, so the adaptive quality and the immediate-answer path can be tested
  on the desk.

### `pyproject.toml` and packaging
- `[tool.setuptools] packages = ["tftpush"]` is an explicit list: **add
  `"tftpush.carlinkit"`**, or the new subpackage won't install.
- `[project.optional-dependencies] carplay = ["pyusb>=1.2", "av>=12"]`.
- `image/chroot-setup.sh`: `apt install python3-usb python3-av ffmpeg`.
- The hand deploy we've been using (scp flat files into `/opt/tftpush/tftpush/`) must also
  copy the `carlinkit/` folder. Write it as `tools/deploy.sh` so a file isn't forgotten.

### `tests/test_all.py`
Split the Phase 2 tests into `tests/test_carlinkit.py` and `tests/test_video.py`. Cover:
- the codec: byte-exact messages, parsing captures, bad header, split reads
- dongle lifecycle with the fake dongle: init order, heartbeat, watchdog, unplug/replug
- `LiveVideo`: decoding the clip in `tests/data`, only the newest picture kept, keyframe
  request after a decoder error
- `encode_jpeg`: adapts under a byte cap
- `Session`: per-screen versions, immediate answer on a new frame, encoding off the event loop
- key mapping and long-press
- the `carplay-desk` end-to-end test

### `README.md`
- Code map rows for the new modules; "How it works" Phase 2 steps; Milestones updated; the
  measured fps and size cap.

## 7. Done means

CarPlay from the rider's iPhone shows on the BMW TFT wide screen within about a minute of
ignition with no one touching anything. Maps guidance and menus work from the handlebar
controls. If either the dongle or the bike link drops, it recovers on its own. Measured fps
and delay are written in this file. Audio follows as the next milestone.

## 8. Test runbook for the first dongle day

Run these in order; each one is short and narrows down where a problem is. The logs to send
back are `/var/lib/tftpush/captures/session-*.log` and `dongle-*.log`, plus the output of
`sudo journalctl -u tftpush --since today`.

Deploy from the laptop at any time with `tools/deploy.sh`.

### Step A. Dongle alone, on the bench (no bike needed)
1. Plug the dongle into the Pi. `lsusb` must show `1314:1520` or `1314:1521`.
2. Stop the service so it doesn't hold the dongle: `sudo systemctl stop tftpush`.
3. Run `cd /tmp && sudo PYTHONPATH=/opt/tftpush python3 -m tftpush dongle-probe --dump /tmp/cp.h264 --seconds 120`.
   On the iPhone, connect to the dongle (Settings > General > CarPlay, or pair with the
   dongle's Bluetooth name the first time).
4. Expected log: `dongle: waiting`, then `phone connected to the dongle: CARPLAY`, then
   `video 1920x464 (asked for 1920x464)`, with the video count climbing every 5 s.
5. If the phone never connects at 1920x464, try `--size 1920x480`, then `--size 1280x480`. Write
   down which sizes work and set `CARPLAY_SIZE` in `/boot/firmware/bmw-carplay.txt`
   (settings there are read at start-up: restart the service after editing).
6. Copy `/tmp/cp.h264` to the laptop and play it: `ffplay -f h264 cp.h264`. Check the CarPlay
   layout is sensible at that size; try `--dpi 120` and `--dpi 200` for icon size.
7. Start the service again: `sudo systemctl start tftpush`.

### Step B. Frame size and fps on the bike (P1; the dongle doesn't matter)
1. Turn the probe on:
   `printf '[Service]\nEnvironment=TFTPUSH_FPS_PROBE=1\n' | sudo tee /etc/systemd/system/tftpush.service.d/probe.conf && sudo systemctl daemon-reload && sudo systemctl restart tftpush`
2. On the bike, open the wide nav screen. Every 20 s the picture changes to
   `STEP n: XX KB` (15, 20, 30, 50, 100, 200 KB). Note the last step that still shows
   and moves. Then do the same on the square screen.
3. The log has the matching numbers every 10 s: `screen 2: 6.3 fps, 17.8 KB/frame, 45 ms from request to frame`.
4. If no step shows at all (not even 15 KB), add `Environment=TFTPUSH_FPS_PROBE_OPTIMIZE=0`
   to `probe.conf`, restart, and try again. If that helps, set `JPEG_OPTIMIZE=0`.
5. Turn the probe off: `sudo rm /etc/systemd/system/tftpush.service.d/probe.conf && sudo systemctl daemon-reload && sudo systemctl restart tftpush`.
6. Set `JPEG_MAX_BYTES` in `bmw-carplay.txt` to about 90% of the largest size that showed,
   then `sudo systemctl restart tftpush`.

### Step C. CarPlay on the bike
1. Dongle in the Pi, phone connected to the dongle, bike on. The TFT shows a status card
   ("Waiting for iPhone...") until the picture arrives, then CarPlay.
2. The log shows per-screen fps and frame sizes every 10 s; `dongle-*.log` shows the dongle
   side.
3. Try each handlebar key and write down what CarPlay does. The log says
   `key LEFT PRESS -> CarPlay LEFT` for each. If the wheel moves focus the wrong way, or
   LEFT/RIGHT nudge where the wheel should turn, change `KEYMAP` (for example
   `KEYMAP=WHEEL+=down,WHEEL-=up`).
4. Try `CARPLAY_NIGHT=1` if the picture is too blurry: dark maps compress much better.
5. Bike status bar (battery / signal). With the bike on, the battery should read full
   (100 %, the default while the iPhone's own level is unknown) instead of the red empty
   battery. Then look for the iPhone's values:
   `sudo journalctl -u tftpush --since today | grep -E "first seen|JSON keys|iPhone status"`.
   `iPhone status: battery NN%, signal NN%` means the dongle sends them and they go to the
   bike. Otherwise the `first seen` / `JSON keys` lines list every message and field the
   dongle sends that we don't know yet: send them back; if the battery or signal is in
   there under an unexpected name, it's a one-line change in `tftpush/phone_status.py`.

### Step D. Restarts
Unplug and replug the dongle; turn the iPhone's Wi-Fi off and on; turn the bike off and on.
Each time, CarPlay should come back on its own. If it doesn't, keep the logs from that moment.

