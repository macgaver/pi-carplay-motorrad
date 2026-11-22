"""Writes tftpush/carlinkit/testclip.h264: a short H.264 Annex-B clip (320x80, a moving bar
and a frame counter, keyframe every 10 frames, one slice per picture) that the fake dongle
streams in desk tests and `tftpush carplay-desk`. Needs PyAV with libx264:

    .venv/bin/python tools/make_test_clip.py
"""
from pathlib import Path

import av
from PIL import Image, ImageDraw

W, H, N = 320, 80, 30
out = Path(__file__).resolve().parents[1] / "tftpush" / "carlinkit" / "testclip.h264"

codec = av.CodecContext.create("libx264", "w")
codec.width, codec.height, codec.pix_fmt = W, H, "yuv420p"
codec.framerate = 20
codec.gop_size = 10
codec.options = {"preset": "veryfast", "tune": "zerolatency", "bf": "0", "threads": "1",
                 "x264-params": "keyint=10:min-keyint=10:scenecut=0:repeat-headers=1:sliced-threads=0"}
data = bytearray()
for i in range(N):
    img = Image.new("RGB", (W, H), (20, 30, 60))
    d = ImageDraw.Draw(img)
    x = int(i * (W - 40) / (N - 1))
    d.rectangle([x, 10, x + 40, H - 10], fill=(240, 200, 40))
    d.text((6, 6), f"CarPlay test {i:02d}", fill=(255, 255, 255))
    frame = av.VideoFrame.from_image(img).reformat(format="yuv420p")
    frame.pts = i
    for pkt in codec.encode(frame):
        data += bytes(pkt)
for pkt in codec.encode(None):
    data += bytes(pkt)
out.write_bytes(bytes(data))
print(f"wrote {out} ({len(data)} bytes, {N} frames)")
