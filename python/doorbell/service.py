"""Connects the parts together: camera -> detector -> visit tracker -> events, push, HomeKit.

HomeKit is a separate container (homekit/). It listens for the socket.io
messages ``bell`` and ``delivery`` that ``broadcast`` sends to every client.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

import cv2
import numpy as np

from doorbell import doorstep
from doorbell.camera import Frame, FrameSource, crop_fraction, encode_jpeg
from doorbell.config import Config
from doorbell.detector import Detection
from doorbell.events import EventStore
from doorbell.notify import Notifier
from doorbell.visits import DELIVERY, IDLE, VisitTracker

POST = "post"

log = logging.getLogger(__name__)

REF_EVERY_S = 3.0  # how often to refresh the empty-doorstep reference
BELL_DEBOUNCE_S = 3.0


def in_zone(point: tuple[float, float], zone: list[float]) -> bool:
    x, y = point
    x0, y0, x1, y1 = zone
    return x0 <= x <= x1 and y0 <= y <= y1


def zone_overlap(box, zone) -> float:
    """Share of ``zone``'s area covered by ``box`` (both x0, y0, x1, y1 fractions)."""
    w = min(box[2], zone[2]) - max(box[0], zone[0])
    h = min(box[3], zone[3]) - max(box[1], zone[1])
    area = (zone[2] - zone[0]) * (zone[3] - zone[1])
    return max(0.0, w) * max(0.0, h) / area if area > 0 else 0.0


def box_area(box) -> float:
    return max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])


def annotate(frame: Frame, img: np.ndarray, people: list[Detection], zones: dict, quality: int) -> bytes:
    """JPEG of ``img`` (``frame`` at display size) with the zones and person boxes drawn on it."""
    img = img.copy()
    h, w = img.shape[:2]
    colours = {"approach": (255, 200, 0), "doorstep": (0, 200, 255), "postbox": (200, 80, 255)}
    for name, zone in zones.items():
        x0, y0, x1, y1 = zone
        cv2.rectangle(img, (int(x0 * w), int(y0 * h)), (int(x1 * w), int(y1 * h)), colours.get(name, (200, 200, 200)), 1)
    for p in people:
        x0, y0, x1, y1 = p.box
        colour = (0, 255, 0) if in_zone(p.feet, zones["approach"]) else (0, 140, 255)
        cv2.rectangle(img, (int(x0 * w), int(y0 * h)), (int(x1 * w), int(y1 * h)), colour, 2)
        cv2.putText(img, f"{p.confidence:.0%}", (int(x0 * w), max(12, int(y0 * h) - 4)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, colour, 1)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(frame.t))
    cv2.putText(img, stamp, (8, h - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
    return encode_jpeg(img, quality)


class _VisitContext:
    def __init__(self, before: tuple[np.ndarray, bytes] | None) -> None:
        self.before = before  # (doorstep crop, doorstep jpeg) from before arrival
        self.best_area = 0.0
        self.best_jpeg: bytes = b""
        self.max_people = 0
        self.post_frames = 0  # detections with someone reaching into the post box
        self.post_jpeg: bytes = b""
        # Biggest at-door box in the first and latest detections: big = right by the
        # camera (the house door), small = out at the porch opening.
        self.first_box: tuple | None = None
        self.last_box: tuple | None = None


class DoorbellService:
    def __init__(self, config: Config, source: FrameSource, events: EventStore, notifier: Notifier) -> None:
        self.config = config
        self.source = source
        self.events = events
        self.notifier = notifier
        self.broadcast: Callable[[str, dict], None] = lambda kind, data: None
        self.bridge_ok: Callable[[], bool] = lambda: False
        self.detector_status: dict = {}
        self.homekit: dict = {"seen": None}
        self._lock = threading.RLock()
        self.tracker = VisitTracker(config.section("visit"))
        self._ctx: _VisitContext | None = None
        self._ref: tuple[np.ndarray, bytes] | None = None
        self._ref_t = 0.0
        self._ref_candidate: tuple[np.ndarray, bytes] | None = None
        self._last_bell = 0.0
        self._last_people: list[dict] = []
        self._stop = threading.Event()

    # ---- config ----------------------------------------------------------

    def zones(self) -> dict:
        return self.config.section("zones")

    def apply_config(self) -> None:
        with self._lock:
            self.tracker.configure(self.config.section("visit"))

    # ---- inputs ----------------------------------------------------------

    def start(self) -> None:
        threading.Thread(target=self._tick_loop, name="visit-tick", daemon=True).start()

    def on_detections(self, frame: Frame, people: list[Detection]) -> None:
        zones = self.zones()
        at_door = [p for p in people if in_zone(p.feet, zones["approach"])]
        self._last_people = [dict(p.to_dict(), at_door=p in at_door) for p in people]
        with self._lock:
            transitions = self.tracker.observe(frame.t, bool(at_door))
            if self.tracker.state == IDLE and not people and frame.t - self._ref_t >= REF_EVERY_S:
                self._update_reference(frame)
            if self._ctx is not None or "arrived" in transitions:
                self._track_best(frame, people, at_door, transitions)
            self._handle(transitions, frame)

    def bell(self, source: str = "button") -> None:
        now = time.time()
        if now - self._last_bell < BELL_DEBOUNCE_S:
            return
        self._last_bell = now
        log.info("bell pressed (%s)", source)
        frame = self.source.latest()
        snapshot = self._annotated(frame) if frame else b""
        with self._lock:
            transitions = self.tracker.bell(now)
            if "arrived" in transitions:
                self._ctx = _VisitContext(self._ref)
                self._ctx.best_jpeg = snapshot
        record = self.events.add("bell", {"source": source}, {"snapshot": snapshot}, t=now)
        self.broadcast("bell", record)
        self.broadcast("event", record)
        if self.notifier.enabled("bell"):
            self.notifier.send("bell", "Doorbell", time.strftime("Someone rang at %H:%M"), snapshot)

    def simulate_delivery(self) -> dict:
        """A fake delivery event for testing HomeKit and push without a real drop-off."""
        frame = self.source.latest()
        snapshot = self._annotated(frame) if frame else b""
        record = self.events.add(DELIVERY, {"test": True, "parcel_left": True, "dwell_s": 0, "bells": 0}, {"arrival": snapshot})
        self.broadcast("event", record)
        self.broadcast("delivery", record)
        if self.notifier.enabled("delivery"):
            self.notifier.send("delivery", "Delivery (test)", time.strftime("Test delivery at %H:%M"), snapshot)
        return record

    # ---- visit handling --------------------------------------------------

    def _tick_loop(self) -> None:
        while not self._stop.wait(1.0):
            with self._lock:
                self._handle(self.tracker.tick(time.time()), self.source.latest())

    def _track_best(self, frame: Frame, people, at_door, transitions) -> None:
        if "arrived" in transitions and self._ctx is None:
            self._ctx = _VisitContext(self._ref)
        ctx = self._ctx
        ctx.max_people = max(ctx.max_people, len(at_door))
        if at_door:
            biggest = max(at_door, key=lambda p: box_area(p.box)).box
            if ctx.first_box is None:
                ctx.first_box = biggest
            ctx.last_box = biggest
        postbox = self.zones().get("postbox")
        if postbox:
            cfg = self.config.section("post")
            reach = max((zone_overlap(p.box, postbox) for p in people), default=0.0)
            if reach >= float(cfg.get("min_overlap", 0.15)):
                ctx.post_frames += 1
                if not ctx.post_jpeg or ctx.post_frames == 2:  # an early frame shows the post going in
                    ctx.post_jpeg = self._annotated(frame, people)
        for p in at_door:
            x0, y0, x1, y1 = p.box
            area = (x1 - x0) * (y1 - y0)
            if area > ctx.best_area:  # the biggest box is usually the clearest face
                ctx.best_area = area
                ctx.best_jpeg = self._annotated(frame, people)

    def _handle(self, transitions: list[str], frame: Frame | None) -> None:
        for tr in transitions:
            log.info("visit %s: %s", self.tracker.visit.id if self.tracker.visit else "-", tr)
            if tr == "arrived":
                self.broadcast("visit", {"state": "arrived", "time": time.time()})
            elif tr == "closed":
                self._close_visit(frame)

    def _close_visit(self, frame: Frame | None) -> None:
        visit, ctx = self.tracker.visit, self._ctx or _VisitContext(None)
        self._ctx = None
        # Waiting for a still doorstep takes seconds; don't hold the tracker lock.
        threading.Thread(target=self._finish_visit, args=(visit, ctx), name="visit-close", daemon=True).start()

    def _changed(self, before: np.ndarray, after: np.ndarray) -> doorstep.DoorstepChange:
        cfg = self.config.section("doorstep")
        return doorstep.compare(
            before, after, cfg.get("diff_threshold", 28), cfg.get("changed_fraction", 0.06), cfg.get("highlight", 215)
        )

    def _settled_doorstep(self) -> tuple[tuple[np.ndarray, bytes] | None, bytes]:
        """Wait until the doorstep looks the same in two shots ``stable_s`` apart
        with nobody detected, and return that shot. A hand over the lens or
        someone the detector missed would otherwise look like a parcel.
        Returns (shot or None if it never settled, last jpeg for the record)."""
        cfg = self.config.section("doorstep")
        wait, tries = float(cfg.get("stable_s", 2)), int(cfg.get("stable_tries", 6))
        last_jpeg = b""
        for _ in range(tries):
            first = self.source.latest()
            if first is None:
                return None, last_jpeg
            a = self._doorstep(first)
            if self._stop.wait(wait):
                return None, last_jpeg
            second = self.source.latest()
            b = self._doorstep(second)
            last_jpeg = b[1]
            if not self._last_people and not self._changed(a[0], b[0]).changed:
                return b, last_jpeg
        return None, last_jpeg

    def _finish_visit(self, visit, ctx: _VisitContext) -> None:
        parcel, fraction, after_jpeg, unsettled = None, None, b"", False
        if ctx.before is not None:
            after, after_jpeg = self._settled_doorstep()
            if after is None:
                unsettled = True
                log.info("visit %s: doorstep never settled; parcel unknown", visit.id)
            else:
                change = self._changed(ctx.before[0], after[0])
                parcel, fraction = change.changed, round(change.fraction, 3)
        kind = self.tracker.classify(visit, parcel)
        post_cfg = self.config.section("post")
        near = float(post_cfg.get("near_area", 0.10))
        from_house = ctx.first_box is not None and box_area(ctx.first_box) >= near
        into_house = ctx.last_box is not None and box_area(ctx.last_box) >= near
        # Reaching into the post box is "post" only if they then left the way they
        # came. Ending the visit by the house door means a resident collecting the
        # post on the way in (2026-10-15 13:15).
        posted = ctx.post_frames >= int(post_cfg.get("min_frames", 4)) and not into_house
        if posted and kind != DELIVERY:  # a parcel on the mat wins; ringing first doesn't matter
            kind = POST
        data = {
            "visit_start": visit.start,
            "visit_end": visit.end,
            "dwell_s": round(visit.dwell, 1),
            "bells": len(visit.bells),
            "people": ctx.max_people,
            "parcel_left": parcel,
            "doorstep_change": fraction,
            "doorstep_unsettled": unsettled,
            "post_frames": ctx.post_frames,
            "from_house": from_house,
            "into_house": into_house,
        }
        images = {"arrival": ctx.best_jpeg}
        if ctx.post_jpeg:
            images["postbox"] = ctx.post_jpeg
        if ctx.before is not None:
            images["doorstep_before"] = ctx.before[1]
        if after_jpeg:
            images["doorstep_after"] = after_jpeg
        if kind == "ignored":
            log.info("visit %s ignored (dwell %.1fs)", visit.id, visit.dwell)
            return
        record = self.events.add(kind, data, images, t=visit.start)
        self.broadcast("event", record)
        if kind in (DELIVERY, POST):
            self.broadcast("delivery", record)  # HomeKit pulses its "Delivery" sensor for both
        # Bell presses were already pushed as they happened; this is the summary.
        alert = {DELIVERY: "delivery", POST: "post"}.get(kind, "visit")
        if self.notifier.enabled(alert):
            title = {"delivery": "Delivery", "post": "Post", "visitor": "Visitor", "caller": "Someone came to the door"}[kind]
            when = time.strftime("%H:%M", time.localtime(visit.start))
            text = f"{title} at {when}, at the door for {visit.dwell:.0f}s"
            image = after_jpeg or ctx.best_jpeg
            if kind == DELIVERY:
                text = f"Delivery at {when}: parcel left on the doorstep"
            elif kind == POST:
                text = f"Post at {when}: something went in the post box"
                image = ctx.post_jpeg or ctx.best_jpeg
            self.notifier.send(alert, title, text, image)

    # ---- snapshots -------------------------------------------------------

    def _update_reference(self, frame: Frame) -> None:
        """Take a new empty-doorstep reference, but only once two shots in a
        row agree, so a passing shadow or an undetected person isn't kept."""
        shot = self._doorstep(frame)
        candidate, self._ref_candidate = self._ref_candidate, shot
        self._ref_t = frame.t
        if candidate is not None and not self._changed(candidate[0], shot[0]).changed:
            self._ref = shot

    def _doorstep(self, frame: Frame) -> tuple[np.ndarray, bytes]:
        crop, _ = crop_fraction(frame.image, self.zones()["doorstep"])
        crop = crop.copy()
        return crop, encode_jpeg(self.source.correct(crop), 85)

    def _annotated(self, frame: Frame, people: list[Detection] | None = None) -> bytes:
        return annotate(frame, self.source.view(frame), people or [], self.zones(), 85)

    # ---- status ----------------------------------------------------------

    def status(self) -> dict:
        visit = self.tracker.visit
        return {
            "time": time.time(),
            "camera": self.source.status,
            "detector": self.detector_status,
            "people": self._last_people,
            "visit": {
                "state": self.tracker.state,
                "id": visit.id if visit and self.tracker.state != IDLE else None,
                "since": visit.start if visit and self.tracker.state != IDLE else None,
                "bells": len(visit.bells) if visit and self.tracker.state != IDLE else 0,
            },
            "reference_age_s": round(time.time() - self._ref_t, 1) if self._ref is not None else None,
            "bridge_ok": self.bridge_ok(),
            "push": self.notifier.last,
            "push_enabled": self.notifier.configured(),
            "homekit": self.homekit,
        }
