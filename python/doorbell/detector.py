"""Person detection on the approach zone, via the object_detection brick (YOLOX-nano).

The approach zone is cropped from the full-resolution frame, then letterboxed
to the model's input size. Doing the resize ourselves means the returned boxes
are always in a known coordinate space, whatever the runner does internally.
Boxes come back as fractions of the full frame.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable

import cv2
import numpy as np

from doorbell.camera import Frame, FrameSource, crop_fraction, encode_jpeg
from doorbell.static import StaticFilter

log = logging.getLogger(__name__)


@dataclass
class Detection:
    label: str
    confidence: float
    box: tuple[float, float, float, float]  # x0, y0, x1, y1 as fractions of the full frame

    @property
    def feet(self) -> tuple[float, float]:
        x0, _, x1, y1 = self.box
        return ((x0 + x1) / 2, y1)

    def to_dict(self) -> dict:
        return {"label": self.label, "confidence": round(self.confidence, 2), "box": [round(v, 4) for v in self.box]}


def backlight(image: np.ndarray, clip: float) -> np.ndarray:
    """Local contrast enhancement (CLAHE on lightness) for the detector's input.

    In the backlit porch, the dark pillars beside the bright doorway looked like
    person silhouettes (~0.4–0.5 confidence) and backlit visitors were missed.
    On stored snapshots (2026-10-15) this removed the pillar ghosts and raised
    backlit people from 0.40/0.44 to 0.78/0.56, with well-lit people unchanged.
    """
    if not clip:
        return image
    lab = cv2.cvtColor(image, cv2.COLOR_BGR2LAB)
    lab[..., 0] = cv2.createCLAHE(clipLimit=float(clip), tileGridSize=(4, 4)).apply(lab[..., 0])
    return cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)


def letterbox(image: np.ndarray, size: tuple[int, int]) -> tuple[np.ndarray, float, tuple[int, int]]:
    """Fit ``image`` into ``size`` keeping aspect; returns (canvas, scale, (pad_x, pad_y))."""
    tw, th = size
    h, w = image.shape[:2]
    scale = min(tw / w, th / h)
    nw, nh = max(1, int(round(w * scale))), max(1, int(round(h * scale)))
    resized = cv2.resize(image, (nw, nh), interpolation=cv2.INTER_AREA)
    canvas = np.full((th, tw, 3), 114, dtype=np.uint8)
    px, py = (tw - nw) // 2, (th - nh) // 2
    canvas[py : py + nh, px : px + nw] = resized
    return canvas, scale, (px, py)


def unletterbox(
    box: list[float], scale: float, pad: tuple[int, int], offset: tuple[int, int], full: tuple[int, int]
) -> tuple[float, float, float, float]:
    """Map a box from letterboxed model space back to full-frame fractions."""
    fw, fh = full
    x0, y0, x1, y1 = box
    px, py = pad
    ox, oy = offset
    fx = lambda v: min(1.0, max(0.0, ((v - px) / scale + ox) / fw))  # noqa: E731
    fy = lambda v: min(1.0, max(0.0, ((v - py) / scale + oy) / fh))  # noqa: E731
    return (fx(x0), fy(y0), fx(x1), fy(y1))


class EIPersonModel:
    """Wrapper over arduino.app_bricks.object_detection.ObjectDetection."""

    def __init__(self, confidence: float) -> None:
        from arduino.app_bricks.object_detection import ObjectDetection  # type: ignore

        self._od = ObjectDetection(confidence=confidence)
        info = self._od.get_model_info()
        w = int(getattr(info, "image_input_width", -1) or -1)
        h = int(getattr(info, "image_input_height", -1) or -1)
        self.input_size = (w, h) if w > 0 and h > 0 else (320, 320)
        log.info("object detection model input %sx%s", *self.input_size)

    def detect(self, image: np.ndarray) -> list[tuple[str, float, list[float]]]:
        result = self._od.detect(encode_jpeg(image, 90), image_type="jpg") or {}
        out = []
        for d in result.get("detection", []):
            out.append((d["class_name"], float(d["confidence"]) / 100.0, d["bounding_box_xyxy"]))
        return out


class HogPersonModel:
    """Off-board stand-in using OpenCV's HOG people detector (mock mode only)."""

    input_size = (640, 480)

    def __init__(self, confidence: float) -> None:
        self._hog = cv2.HOGDescriptor()
        self._hog.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())

    def detect(self, image: np.ndarray) -> list[tuple[str, float, list[float]]]:
        rects, weights = self._hog.detectMultiScale(image, winStride=(8, 8))
        return [
            ("person", float(min(1.0, w)), [float(x), float(y), float(x + rw), float(y + rh)])
            for (x, y, rw, rh), w in zip(rects, np.ravel(weights) if len(rects) else [])
        ]


class Detector:
    """Runs the model on the latest frame every ``interval_s`` and reports people."""

    # The model reports everything above this, so the log shows near misses;
    # the configured ``confidence`` then decides what counts.
    MODEL_FLOOR = 0.3

    def __init__(
        self,
        source: FrameSource,
        cfg: Callable[[], dict] | dict,
        zones: Callable[[], dict],
        on_result: Callable[[Frame, list[Detection]], None],
        model=None,
        log_path=None,
    ) -> None:
        self.source = source
        self._cfg = cfg if callable(cfg) else (lambda: cfg)  # a getter, so web edits apply live
        self.zones = zones
        self.on_result = on_result
        self.model = model
        self.static = StaticFilter(self.cfg)
        self.log = DetectionLog(log_path) if log_path else None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.status = {"state": "starting", "fps": 0.0, "last_ms": None, "error": None, "people": 0, "ignored": 0}

    @property
    def cfg(self) -> dict:
        return self._cfg()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="detector", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def detect_frame(self, frame: Frame) -> list[Detection]:
        """All detections of the wanted labels above the model floor (no threshold, no filtering)."""
        cfg = self.cfg
        labels = set(cfg.get("labels", ["person"]))
        full_h, full_w = frame.image.shape[:2]
        crop, offset = crop_fraction(frame.image, self.zones()["approach"])
        canvas, scale, pad = letterbox(crop, self.model.input_size)
        canvas = backlight(canvas, cfg.get("backlight_clahe", 2.0))  # after downscaling: cheap
        people = []
        for label, conf, box in self.model.detect(canvas):
            if label in labels:
                people.append(Detection(label, conf, unletterbox(box, scale, pad, offset, (full_w, full_h))))
        return people

    def filter(self, t: float, raw: list[Detection]) -> tuple[list[Detection], list[Detection]]:
        """Apply the confidence threshold and the static-object filter: (people, ignored)."""
        cfg = self.cfg
        self.static.configure(cfg)
        threshold = float(cfg.get("confidence", 0.5))
        # Track every detection, weak ones too: a ghost that hovers around 0.3–0.5
        # and only now and then crosses the threshold still gets learned as static.
        moving, ignored = self.static.update(t, raw) if cfg.get("ignore_static", True) else (raw, [])
        return [d for d in moving if d.confidence >= threshold], ignored

    def _run(self) -> None:
        while self.model is None and not self._stop.is_set():
            try:
                self.model = EIPersonModel(self.MODEL_FLOOR)
            except Exception as exc:  # models runner container not up yet
                self.status.update(state="error", error=f"model: {exc}")
                log.warning("object detection not ready: %s", exc)
                self._stop.wait(5)
        self.status.update(state="running", error=None)
        seq = 0
        window_start, window_n = time.monotonic(), 0
        while not self._stop.is_set():
            started = time.monotonic()
            interval = float(self.cfg.get("interval_s", 0.4))
            frame = self.source.wait_newer(seq, timeout=2.0)
            if frame is None or frame.seq == seq:
                continue
            seq = frame.seq
            try:
                t0 = time.monotonic()
                raw = self.detect_frame(frame)
                people, ignored = self.filter(frame.t, raw)
                self.status["last_ms"] = int((time.monotonic() - t0) * 1000)
                self.status["people"] = len(people)
                self.status["ignored"] = len(ignored)
                self.status["error"] = None
                if self.log is not None:
                    self.log.write(frame.t, raw, people, ignored)
                self.on_result(frame, people)
            except Exception as exc:
                self.status["error"] = str(exc)
                log.exception("detection failed")
                self._stop.wait(1)
            window_n += 1
            if time.monotonic() - window_start >= 5:
                self.status["fps"] = round(window_n / (time.monotonic() - window_start), 1)
                window_start, window_n = time.monotonic(), 0
            self._stop.wait(max(0.0, interval - (time.monotonic() - started)))


class DetectionLog:
    """Every frame with a detection, as JSON lines (data/detections.jsonl), for tuning.

    status: kept (counted), static (ignored, didn't move), low (below threshold).
    Rotates to .1 at ``max_bytes``.
    """

    def __init__(self, path, max_bytes: int = 5_000_000) -> None:
        import pathlib

        self.path = pathlib.Path(path)
        self.max_bytes = max_bytes
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, t: float, raw, kept, ignored) -> None:
        if not raw:
            return
        import json

        kept_ids, ignored_ids = {id(d) for d in kept}, {id(d) for d in ignored}
        rows = []
        for d in raw:
            status = "kept" if id(d) in kept_ids else "static" if id(d) in ignored_ids else "low"
            x0, y0, x1, y1 = d.box
            rows.append({"c": round(d.confidence, 3), "box": [round(v, 3) for v in d.box],
                         "aspect": round((x1 - x0) / max(1e-6, y1 - y0), 2), "s": status})
        try:
            if self.path.exists() and self.path.stat().st_size > self.max_bytes:
                self.path.replace(self.path.with_suffix(".jsonl.1"))
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"t": round(t, 2), "d": rows}) + "\n")
        except OSError as exc:
            log.debug("detection log write failed: %s", exc)
