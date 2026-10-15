#!/usr/bin/env python3
"""HomeKit video doorbell for the UnoQ-Doorbell app.

Runs in its own container with host networking, because HomeKit needs
Bonjour/mDNS on the LAN and App Lab containers use bridge networking. It
talks to the App Lab app on http://127.0.0.1:7000:

  - /api/live.jpg       snapshots for the Home app and rich notifications
  - /stream.mjpg        source for live view (re-encoded to H.264 + SRTP by ffmpeg)
  - socket.io "bell"     ring the doorbell (HomePods chime and announce)
  - socket.io "delivery" pulse the "Delivery" motion sensor (Home app notification)
  - POST /api/homekit   report pairing state and setup code to the web page
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import threading
import time
import json
import urllib.request

import socketio
from pyhap import camera
from pyhap.accessory_driver import AccessoryDriver
from pyhap.const import CATEGORY_VIDEO_DOOR_BELL

log = logging.getLogger("hap-doorbell")

APP_URL = os.environ.get("APP_URL", "http://127.0.0.1:7000")
NAME = os.environ.get("ACCESSORY_NAME", "Front Door")
STATE_FILE = os.environ.get("STATE_FILE", "/state/accessory.state")
ADDRESS = os.environ.get("HOST_IP", "192.168.0.115")
PORT = int(os.environ.get("HAP_PORT", "51826"))
ENCODER = os.environ.get("H264_ENCODER", "libx264")
DELIVERY_PULSE_S = 30

if ENCODER == "libx264":
    ENCODER_ARGS = "-vcodec libx264 -preset ultrafast -tune zerolatency -profile:v baseline"
else:  # h264_v4l2m2m (Venus) works at 640x480 but segfaulted at 1280x720 (2026-09-25)
    ENCODER_ARGS = f"-vcodec {ENCODER}"

# Frames are 4:3 fisheye; pad rather than stretch when iOS asks for 16:9.
FFMPEG_CMD = (
    "ffmpeg -hide_banner -loglevel warning "
    # MJPEG over HTTP has no timestamps; without wall-clock ones ffmpeg assumes
    # 25 fps and falls behind the real ~10 fps feed.
    f"-use_wallclock_as_timestamps 1 -f mjpeg -i {APP_URL}/stream.mjpg "
    "-map 0:0 -an -sn -dn "
    f"{ENCODER_ARGS} -pix_fmt yuv420p -r {{fps}} "
    "-vf scale={width}:{height}:force_original_aspect_ratio=decrease,"
    "pad={width}:{height}:(ow-iw)/2:(oh-ih)/2 "
    "-b:v {v_max_bitrate}k -bufsize {v_max_bitrate}k -maxrate {v_max_bitrate}k "
    "-payload_type 99 -ssrc {v_ssrc} -f rtp "
    "-srtp_out_suite AES_CM_128_HMAC_SHA1_80 -srtp_out_params {v_srtp_key} "
    "srtp://{address}:{v_port}?rtcpport={v_port}&localrtcpport={v_port}&pkt_size=1316"
)

OPTIONS = {
    "video": {
        "codec": {
            "profiles": [
                camera.VIDEO_CODEC_PARAM_PROFILE_ID_TYPES["BASELINE"],
                camera.VIDEO_CODEC_PARAM_PROFILE_ID_TYPES["MAIN"],
                camera.VIDEO_CODEC_PARAM_PROFILE_ID_TYPES["HIGH"],
            ],
            "levels": [
                camera.VIDEO_CODEC_PARAM_LEVEL_TYPES["TYPE3_1"],
                camera.VIDEO_CODEC_PARAM_LEVEL_TYPES["TYPE3_2"],
                camera.VIDEO_CODEC_PARAM_LEVEL_TYPES["TYPE4_0"],
            ],
        },
        "resolutions": [
            [320, 240, 15],
            [320, 180, 15],
            [480, 360, 15],
            [480, 270, 15],
            [640, 480, 15],
            [640, 360, 15],
            [1024, 768, 15],
            [1280, 720, 15],
            [1920, 1080, 15],
        ],
    },
    "audio": {
        "codecs": [
            {"type": "OPUS", "samplerate": 24},
            {"type": "AAC-eld", "samplerate": 16},
        ],
    },
    "srtp": True,
    "address": ADDRESS,
    "start_stream_cmd": FFMPEG_CMD,
}


def http_get(path: str, timeout: float = 5) -> bytes:
    with urllib.request.urlopen(APP_URL + path, timeout=timeout) as resp:
        return resp.read()


def http_post_json(path: str, body: dict, timeout: float = 5) -> None:
    req = urllib.request.Request(
        APP_URL + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout):
        pass


class VideoDoorbell(camera.Camera):
    category = CATEGORY_VIDEO_DOOR_BELL

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.set_info_service(manufacturer="UnoQ-Doorbell", model="UNO Q + IMX219 200°", serial_number="valentina", firmware_revision="1.0")

        doorbell = self.add_preload_service("Doorbell")
        self.char_ring = doorbell.configure_char("ProgrammableSwitchEvent")

        # iOS treats a camera as a proper video doorbell only when it also
        # has speaker and microphone services. There's no audio hardware, so
        # both are just placeholders.
        self.add_preload_service("Speaker").configure_char("Mute", value=False)
        self.add_preload_service("Microphone").configure_char("Mute", value=False)

        delivery = self.add_preload_service("MotionSensor", chars=["Name"])
        delivery.configure_char("Name", value="Delivery")
        self.char_delivery = delivery.configure_char("MotionDetected", value=False)
        self._delivery_timer: threading.Timer | None = None

    def ring(self) -> None:
        log.info("ring")
        # 0 = single press. HAP-python sends it and then resets this
        # characteristic to null, so every ring counts as a change. Don't split
        # it into set_value(should_notify=False) + notify(): the reset happens
        # first and the hub receives null, which it ignores (no chime).
        self.char_ring.set_value(0)

    def delivery(self) -> None:
        log.info("delivery")
        if self._delivery_timer:
            self._delivery_timer.cancel()
        self.char_delivery.set_value(True)
        self._delivery_timer = threading.Timer(DELIVERY_PULSE_S, lambda: self.char_delivery.set_value(False))
        self._delivery_timer.daemon = True
        self._delivery_timer.start()

    def get_snapshot(self, image_size):  # called by HAP-python for the Home app
        try:
            return http_get("/api/live.jpg")
        except Exception as exc:
            log.warning("snapshot failed: %s", exc)
            return b""


def listen_to_app(acc: VideoDoorbell) -> None:
    sio = socketio.Client(reconnection=True, reconnection_delay=2, reconnection_delay_max=30)

    @sio.event
    def connect():
        log.info("connected to app socket.io")

    @sio.event
    def disconnect():
        log.warning("app socket.io disconnected")

    @sio.on("bell")
    def on_bell(data):
        acc.ring()

    @sio.on("delivery")
    def on_delivery(data):
        acc.delivery()

    failures = 0
    while True:
        try:
            sio.connect(APP_URL, transports=["websocket", "polling"], wait_timeout=10)
            failures = 0
            sio.wait()
        except Exception as exc:
            failures += 1
            # Log the first failure, then once every ~10 minutes, so a stopped
            # app doesn't flood (and rotate away) the log.
            if failures == 1 or failures % 120 == 0:
                log.warning("app not reachable (%s); retrying every 5s", exc)
        time.sleep(5)


def report_status(driver: AccessoryDriver, acc: VideoDoorbell) -> None:
    while True:
        paired = bool(driver.state.paired)
        body = {"paired": paired, "name": NAME}
        if not paired:
            body["setup_code"] = driver.state.pincode.decode()
            body["setup_uri"] = acc.xhm_uri()
        try:
            http_post_json("/api/homekit", body)
        except Exception as exc:
            log.debug("status report failed: %s", exc)
        time.sleep(20)


def setup_code() -> bytes:
    """A setup code kept next to the pairing state. HAP-python otherwise picks
    a new random one on every start, which is confusing before pairing."""
    path = os.path.join(os.path.dirname(STATE_FILE), "setup_code")
    if os.path.exists(path):
        with open(path, "rb") as fh:
            return fh.read().strip()
    from pyhap.util import generate_pincode

    code = generate_pincode()
    with open(path, "wb") as fh:
        fh.write(code)
    return code


def wait_for_address() -> None:
    """Block until ADDRESS is assigned to this host. After a power cut the
    container can start before the network is up, and HAP-python then fails
    to bind but keeps running, so Docker never restarts it."""
    logged = False
    while True:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind((ADDRESS, 0))
            return
        except OSError as exc:
            if not logged:
                log.warning("waiting for %s to come up (%s)", ADDRESS, exc)
                logged = True
            time.sleep(2)


def watchdog() -> None:
    """Exit if the HAP server stops accepting connections, so Docker's
    restart policy brings it back."""
    time.sleep(60)
    misses = 0
    while True:
        try:
            with socket.create_connection((ADDRESS, PORT), timeout=5):
                misses = 0
        except OSError as exc:
            misses += 1
            log.warning("HAP port %s:%s not answering (%s), miss %d/3", ADDRESS, PORT, exc, misses)
            if misses >= 3:
                log.error("HAP server is down; exiting so Docker restarts the container")
                os._exit(1)
        time.sleep(30)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if os.environ.get("HAP_DEBUG"):
        # Subscriptions and event sends: shows whether the home hub gets rings.
        logging.getLogger("pyhap.accessory_driver").setLevel(logging.DEBUG)
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    wait_for_address()
    driver = AccessoryDriver(port=PORT, persist_file=STATE_FILE, address=ADDRESS, pincode=setup_code())
    acc = VideoDoorbell(OPTIONS, driver, NAME)
    driver.add_accessory(accessory=acc)
    if not driver.state.paired:
        log.info("HomeKit setup code: %s", driver.state.pincode.decode())
    threading.Thread(target=listen_to_app, args=(acc,), daemon=True).start()
    threading.Thread(target=report_status, args=(driver, acc), daemon=True).start()
    threading.Thread(target=watchdog, name="watchdog", daemon=True).start()
    signal.signal(signal.SIGTERM, driver.signal_handler)
    driver.start()


if __name__ == "__main__":
    main()
