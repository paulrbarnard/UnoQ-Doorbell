#!/usr/bin/env python3
"""Probe which libcamera output sizes keep the camera's full field of view.

Runs inside the App Lab container (python-apps-base), using the same
libcamerasrc -> appsink pipeline shape as arduino's CSICamera. For each
requested size it saves one JPEG and prints the delivered size and fps, so
the frames can be compared by eye for crop vs. scale.

    python3 probe_camera.py [--out /app/data/probe] [WxH ...]

Set LIBCAMERA_LOG_LEVELS=SimplePipeline:0 to see which sensor mode libcamera
picks for each request.
"""

from __future__ import annotations

import argparse
import os
import time

import cv2

from arduino.app_peripherals.camera import csi_camss_discovery as camss

DEFAULT_SIZES = ["3280x2464", "1640x1232", "1632x1232", "1600x1200", "820x616", "640x480"]


def probe(source: str, width: int, height: int, out_dir: str, frames: int = 20) -> None:
    pipeline = (
        f"{source} ! video/x-raw,width={width},height={height} ! "
        "videoconvert ! video/x-raw,format=BGR ! appsink drop=true max-buffers=1"
    )
    cap = cv2.VideoCapture(pipeline, cv2.CAP_GSTREAMER)
    if not cap.isOpened():
        print(f"{width}x{height}: FAILED to open")
        return
    try:
        ok, frame = cap.read()
        if not ok or frame is None:
            print(f"{width}x{height}: FAILED first read")
            return
        start = time.monotonic()
        count = 0
        for _ in range(frames):
            ok, f = cap.read()
            if ok and f is not None:
                frame = f
                count += 1
        fps = count / (time.monotonic() - start)
        h, w = frame.shape[:2]
        path = os.path.join(out_dir, f"probe_{width}x{height}.jpg")
        cv2.imwrite(path, frame)
        print(f"{width}x{height}: got {w}x{h} at {fps:.1f} fps -> {path}")
    finally:
        cap.release()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="/app/data/probe")
    parser.add_argument("--camera", type=int, default=0, help="CSIPHY index")
    parser.add_argument("sizes", nargs="*", default=DEFAULT_SIZES)
    args = parser.parse_args()

    os.makedirs(args.out, exist_ok=True)
    camss.setup_gstreamer()
    source = camss.gstreamer_source(args.camera)
    print(f"source: {source}")
    for size in args.sizes:
        w, h = (int(v) for v in size.lower().split("x"))
        probe(source, w, h, args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
