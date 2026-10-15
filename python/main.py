#!/usr/bin/env python3
"""App Lab entry point for the doorbell (Linux side).

On the board App Lab runs this with no arguments. Off the board, for
development (needs fastapi, uvicorn and opencv-python-headless):

    python main.py --mock-camera path/to/images_or_video --mock-bridge
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from doorbell.camera import FrameSource  # noqa: E402
from doorbell.config import DATA_DIR, Config  # noqa: E402
from doorbell.detector import Detector, HogPersonModel  # noqa: E402
from doorbell.events import EventStore  # noqa: E402
from doorbell.notify import Notifier  # noqa: E402
from doorbell.service import DoorbellService  # noqa: E402
from doorbell.web import register_routes  # noqa: E402

ASSETS_DIR = Path(__file__).resolve().parents[1] / "assets"

# Our soft ISP tuning (python/config/libcamera/simple/imx219.yaml: black level and
# colour matrix). libcamera reads this when the camera opens.
os.environ.setdefault("LIBCAMERA_IPA_CONFIG_PATH", str(Path(__file__).resolve().parent / "config" / "libcamera"))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mock-camera", help="folder of images or a video file instead of the CSI camera")
    parser.add_argument("--mock-bridge", action="store_true", help="run without the MCU sketch")
    parser.add_argument("--port", type=int, default=7000, help="port for the off-board dev server")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args()


class DevUI:
    """Minimal stand-in for the web_ui brick when running off the board (no socket.io)."""

    def __init__(self) -> None:
        from fastapi import FastAPI

        self.app = FastAPI()

    def expose_api(self, method: str, path: str, function) -> None:
        self.app.add_api_route(path, function, methods=[method])

    def send_message(self, message_type: str, message) -> None:
        logging.getLogger("devui").info("socket %s: %s", message_type, str(message)[:200])

    def run(self, port: int) -> None:
        import uvicorn
        from fastapi.staticfiles import StaticFiles

        self.app.mount("/", StaticFiles(directory=ASSETS_DIR, html=True), name="assets")
        uvicorn.run(self.app, host="0.0.0.0", port=port)


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    on_board = not args.mock_camera

    config = Config()
    source = FrameSource(config.section("camera"), mock_path=args.mock_camera)
    events_cfg = config.section("events")
    events = EventStore(DATA_DIR / "events", events_cfg.get("keep_days", 14), events_cfg.get("keep_max", 300))
    notifier = Notifier(lambda: config.section("notify"))
    svc = DoorbellService(config, source, events, notifier)

    detector = Detector(
        source,
        lambda: config.section("detector"),
        svc.zones,
        svc.on_detections,
        model=None if on_board else HogPersonModel(0.0),
        log_path=DATA_DIR / "detections.jsonl",
    )
    svc.detector_status = detector.status

    if on_board:
        from arduino.app_bricks.web_ui import WebUI  # type: ignore
        from arduino.app_utils import App  # type: ignore

        ui = WebUI()
    else:
        ui = DevUI()
    svc.broadcast = ui.send_message

    if not args.mock_bridge:
        from doorbell.bridge import DoorBridge

        bridge = DoorBridge(on_bell=lambda: svc.bell("button"))
        svc.bridge_ok = lambda: bridge.ok

    register_routes(ui, svc)
    source.start()
    detector.start()
    svc.start()

    if on_board:
        App.run()  # blocks until App Lab stops the app
    else:
        ui.run(args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
