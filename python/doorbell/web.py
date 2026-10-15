"""HTTP routes on the web_ui brick (port 7000). Also used by the HomeKit companion.

    GET  /stream.mjpg                 live view (full FOV, downscaled)
    GET  /api/live.jpg                one live frame (HomeKit snapshots use this); ?ccm=0 = no colour correction
    GET  /api/status                  camera/detector/visit/bridge/push/HomeKit status
    GET  /api/events                  recent events
    GET  /api/events/{event_id}/{name}  an event snapshot (name without .jpg)
    GET  /api/config                  current settings
    POST /api/config                  merge settings (zones, visit, doorstep, detector, notify)
    POST /api/test/bell               simulate the IO2 button
    POST /api/test/delivery           fake a delivery (HomeKit sensor + push)
    POST /api/test/push               send a test Pushover push (bell sound)
    POST /api/homekit                 HomeKit companion reports its state here
"""

from __future__ import annotations

import time

from doorbell.camera import encode_jpeg, resize_area
from doorbell.service import DoorbellService

EDITABLE_SECTIONS = {"zones", "visit", "doorstep", "detector", "notify", "post"}


def _check_zone(zone) -> list[float]:
    x0, y0, x1, y1 = (float(v) for v in zone)
    if not (0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1):
        raise ValueError(f"bad zone {zone}")
    return [round(v, 4) for v in (x0, y0, x1, y1)]


def register_routes(ui, svc: DoorbellService) -> None:
    from fastapi import HTTPException
    from fastapi.responses import FileResponse, Response, StreamingResponse

    source = svc.source

    def stream():
        def frames():
            seq = 0
            while True:
                frame = source.wait_newer(seq, timeout=5.0)
                if frame is None or frame.seq == seq:
                    continue
                seq = frame.seq
                jpeg = source.display_jpeg(frame)
                yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpeg + b"\r\n"

        return StreamingResponse(frames(), media_type="multipart/x-mixed-replace; boundary=frame")

    def live(ccm: int = 1):
        if not ccm:  # uncorrected picture, for colour calibration
            frame = source.latest()
            jpeg = encode_jpeg(resize_area(frame.image, source.display_size(frame)), 95) if frame else b""
        else:
            jpeg = source.display_jpeg()
        if not jpeg:
            raise HTTPException(503, "no frame yet")
        return Response(jpeg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    def status():
        return svc.status()

    def events(limit: int = 50):
        return svc.events.list(limit)

    def event_image(event_id: str, name: str):
        path = svc.events.image_path(event_id, name)
        if path is None:
            raise HTTPException(404, "no such image")
        return FileResponse(path, media_type="image/jpeg")

    def get_config():
        return svc.config.data

    def set_config(body: dict):
        changes = {k: v for k, v in body.items() if k in EDITABLE_SECTIONS and isinstance(v, dict)}
        try:
            if "zones" in changes:
                changes["zones"] = {k: _check_zone(v) for k, v in changes["zones"].items() if k in ("approach", "doorstep", "postbox")}
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, str(exc))
        data = svc.config.update(changes)
        svc.apply_config()
        return data

    def test_bell():
        svc.bell(source="web")
        return {"ok": True}

    def test_delivery():
        return svc.simulate_delivery()

    def test_push():
        # Uses the bell's sound, so this is how to hear what a ring will sound like.
        svc.notifier.send("bell", "Doorbell test", time.strftime("Test push at %H:%M:%S"), source.display_jpeg() or None)
        return {"ok": True, "enabled": svc.notifier.configured()}

    def homekit(body: dict):
        svc.homekit = {**body, "seen": time.time()}
        return {"ok": True}

    ui.expose_api("GET", "/stream.mjpg", stream)
    ui.expose_api("GET", "/api/live.jpg", live)
    ui.expose_api("GET", "/api/status", status)
    ui.expose_api("GET", "/api/events", events)
    ui.expose_api("GET", "/api/events/{event_id}/{name}", event_image)
    ui.expose_api("GET", "/api/config", get_config)
    ui.expose_api("POST", "/api/config", set_config)
    ui.expose_api("POST", "/api/test/bell", test_bell)
    ui.expose_api("POST", "/api/test/delivery", test_delivery)
    ui.expose_api("POST", "/api/test/push", test_push)
    ui.expose_api("POST", "/api/homekit", homekit)
