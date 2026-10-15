"""Full-field-of-view capture from the 200-degree IMX219 camera.

The Arduino ``Camera`` class asks libcamera for the output size directly, and
the soft ISP gets there by cropping, so small sizes lose most of the fisheye
image. Here we always capture the 2x2-binned mode (1632x1232 after the soft
ISP trims its 8 px border) and downscale in OpenCV. See docs/findings.md.

One thread owns the camera. Everyone else reads the latest frame from it:
the live view, the detector and the doorstep snapshots.
"""

from __future__ import annotations

import glob
import logging
import os
import threading
import time
from dataclasses import dataclass

import cv2
import numpy as np

log = logging.getLogger(__name__)

_ROTATIONS = {
    90: cv2.ROTATE_90_CLOCKWISE,
    180: cv2.ROTATE_180,
    270: cv2.ROTATE_90_COUNTERCLOCKWISE,
}


@dataclass
class Frame:
    image: np.ndarray  # full-resolution BGR, white balance applied
    seq: int
    t: float  # time.time() of capture


def crop_fraction(image: np.ndarray, zone: list[float]) -> tuple[np.ndarray, tuple[int, int]]:
    """Crop ``zone`` ([x0, y0, x1, y1] as fractions) and return (crop, (x_off, y_off))."""
    h, w = image.shape[:2]
    x0, y0, x1, y1 = zone
    ix0, iy0 = max(0, int(x0 * w)), max(0, int(y0 * h))
    ix1, iy1 = min(w, max(ix0 + 1, int(x1 * w))), min(h, max(iy0 + 1, int(y1 * h)))
    return image[iy0:iy1, ix0:ix1], (ix0, iy0)


def resize_area(image: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    """Downscale with INTER_AREA, which averages pixels instead of skipping them."""
    return cv2.resize(image, tuple(size), interpolation=cv2.INTER_AREA)


def colour_matrix(ccm_rgb) -> np.ndarray | None:
    """Config CCM (3x3, R,G,B rows, gamma domain) -> matrix for BGR images."""
    if not ccm_rgb:
        return None
    m = np.asarray(ccm_rgb, dtype=np.float32).reshape(3, 3)
    return m[::-1, ::-1].copy()


def display_size(image: np.ndarray, longest: int) -> tuple[int, int]:
    """(w, h) with the long side ``longest`` px and the image's own aspect, so a
    camera turned 90 degrees (camera.rotate) shows as portrait, not squashed."""
    h, w = image.shape[:2]
    scale = longest / max(w, h)
    return max(1, round(w * scale)), max(1, round(h * scale))


def encode_jpeg(image: np.ndarray, quality: int = 80) -> bytes:
    ok, buf = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, int(quality)])
    return buf.tobytes() if ok else b""


class GrayWorld:
    """Black-floor removal plus gray-world white balance, as one per-channel LUT.

    Black floor: the soft ISP leaves a magenta floor in the shadows (unlit
    pixels come out around R 47, G 10, B 59 instead of 0; raw data shows the
    sensor itself is neutral there), and the CCM amplifies it into purple
    shadows. The corners outside the fisheye circle never see the scene, so
    they're a built-in black reference: their level is subtracted per channel
    in linear light. That also removes neutral lens glare.

    White balance: measured on the centre of the circle and only on mid-tones.
    The lens turns the outer ring purple (colour shading), and averaging that
    in pushed the middle of the picture green.

    Both are measured on a small thumbnail every few frames and smoothed, so
    they don't flicker.
    """

    GAMMA = 2.2  # the soft ISP's output gamma

    def __init__(self, every: int = 10, smoothing: float = 0.2, radius: float = 0.6) -> None:
        self.every = every
        self.smoothing = smoothing
        self.gains = np.ones(3, dtype=np.float32)
        self.floor = np.zeros(3, dtype=np.float32)  # linear, 0..1
        self._n = 0
        self._lut = None
        yy, xx = np.mgrid[:60, :80]
        self._centre = (np.hypot((xx - 39.5) / 40, (yy - 29.5) / 30) < radius).reshape(-1)
        corners = np.zeros((60, 80), bool)
        for ys in (slice(0, 3), slice(57, 60)):
            for xs in (slice(0, 3), slice(77, 80)):
                corners[ys, xs] = True
        self._corners = corners.reshape(-1)
        self._to_lin = (np.arange(256, dtype=np.float32) / 255.0) ** self.GAMMA

    def __call__(self, image: np.ndarray) -> np.ndarray:
        if self._n % self.every == 0:
            thumb = cv2.resize(image, (80, 60), interpolation=cv2.INTER_AREA).reshape(-1, 3)
            lin = self._to_lin[thumb]  # (N, 3) linear BGR
            floor = np.clip(np.median(lin[self._corners], axis=0), 0, 0.1)
            self.floor = (1 - self.smoothing) * self.floor + self.smoothing * floor.astype(np.float32)
            clean = np.clip(lin - self.floor, 0, None) / (1 - self.floor)
            enc = clean ** (1 / self.GAMMA)
            use = self._centre & (thumb.mean(axis=1) > 25) & (thumb.max(axis=1) < 240)
            if use.sum() < 50:  # very dark or blown-out scene: fall back to the whole centre
                use = self._centre
            means = enc[use].mean(axis=0) + 1e-3
            target = np.clip(means.mean() / means, 0.5, 2.5).astype(np.float32)
            self.gains = (1 - self.smoothing) * self.gains + self.smoothing * target
            lin_ramp = np.clip(self._to_lin[:, None] - self.floor[None, :], 0, None) / (1 - self.floor[None, :])
            ramp = 255.0 * lin_ramp ** (1 / self.GAMMA) * self.gains[None, :]
            self._lut = np.clip(ramp + 0.5, 0, 255).astype(np.uint8).reshape(1, 256, 3)
        self._n += 1
        return cv2.LUT(image, self._lut)


class FrameSource:
    """Background capture thread. ``latest()`` returns the newest ``Frame``."""

    def __init__(self, cfg: dict, mock_path: str | None = None) -> None:
        self.cfg = cfg
        self.mock_path = mock_path
        self.capture_size = tuple(cfg.get("capture_size", [1632, 1232]))
        self.fps = float(cfg.get("fps", 10))
        self.rotate = int(cfg.get("rotate", 0))
        self._wb = GrayWorld() if cfg.get("white_balance", True) else None
        # Colour correction for pictures people look at (live view, snapshots,
        # HomeKit). Applied to downscaled frames only; detection and the doorstep
        # comparison use the uncorrected full frames. See camera.ccm in doorbell.yaml.
        self._ccm = colour_matrix(cfg.get("ccm"))
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._frame: Frame | None = None
        self._display: tuple[int, bytes] | None = None  # (seq, jpeg) cache
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.status = {"state": "stopped", "size": None, "fps": 0.0, "source": None, "error": None}

    # ---- lifecycle -------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="camera", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    # ---- readers ---------------------------------------------------------

    def latest(self) -> Frame | None:
        with self._lock:
            return self._frame

    def wait_newer(self, seq: int, timeout: float = 2.0) -> Frame | None:
        """Block until a frame newer than ``seq`` arrives (or timeout)."""
        with self._cond:
            self._cond.wait_for(lambda: self._frame is not None and self._frame.seq > seq, timeout)
            return self._frame

    def display_jpeg(self, frame: Frame | None = None) -> bytes:
        """The live-view JPEG for ``frame`` (default: latest), cached per frame."""
        frame = frame or self.latest()
        if frame is None:
            return b""
        cached = self._display
        if cached and cached[0] == frame.seq:
            return cached[1]
        jpeg = encode_jpeg(self.view(frame), self.cfg.get("jpeg_quality", 75))
        self._display = (frame.seq, jpeg)
        return jpeg

    def view(self, frame: Frame) -> np.ndarray:
        """``frame`` at display size with colour correction: what people see."""
        return self.correct(resize_area(frame.image, self.display_size(frame)))

    def display_size(self, frame: Frame) -> tuple[int, int]:
        return display_size(frame.image, max(self.cfg.get("display_size", [960, 724])))

    def correct(self, image: np.ndarray) -> np.ndarray:
        """Apply the colour matrix (cv2.transform saturates to 0..255)."""
        return image if self._ccm is None else cv2.transform(image, self._ccm)

    # ---- capture thread --------------------------------------------------

    def _open(self) -> cv2.VideoCapture:
        if self.mock_path:
            return _MockCapture(self.mock_path)
        from arduino.app_peripherals.camera import csi_camss_discovery as camss  # type: ignore

        camss.setup_gstreamer()
        source = camss.gstreamer_source(int(self.cfg.get("csi_index", 0)))
        w, h = self.capture_size
        pipeline = (
            f"{source} ! video/x-raw,width={w},height={h} ! "
            "videorate drop-only=true ! "
            f"video/x-raw,framerate={int(self.fps)}/1 ! "
            "videoconvert ! video/x-raw,format=BGR ! appsink drop=true max-buffers=1"
        )
        log.info("camera pipeline: %s", pipeline)
        self.status["source"] = source
        cap = cv2.VideoCapture(pipeline, cv2.CAP_GSTREAMER)
        if not cap.isOpened():
            raise RuntimeError("failed to open libcamera pipeline")
        return cap

    def _run(self) -> None:
        seq = 0
        backoff = 1.0
        while not self._stop.is_set():
            try:
                cap = self._open()
            except Exception as exc:  # camera missing, busy, pipeline error
                self.status.update(state="error", error=str(exc))
                log.error("camera open failed: %s (retry in %.0fs)", exc, backoff)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 30)
                continue
            backoff = 1.0
            self.status.update(state="streaming", error=None)
            window_start, window_n = time.monotonic(), 0
            try:
                while not self._stop.is_set():
                    ok, image = cap.read()
                    if not ok or image is None:
                        raise RuntimeError("camera read failed")
                    if self.rotate in _ROTATIONS:
                        image = cv2.rotate(image, _ROTATIONS[self.rotate])
                    if self._wb is not None:
                        image = self._wb(image)
                    seq += 1
                    with self._cond:
                        self._frame = Frame(image=image, seq=seq, t=time.time())
                        self._cond.notify_all()
                    window_n += 1
                    elapsed = time.monotonic() - window_start
                    if elapsed >= 2.0:
                        self.status["fps"] = round(window_n / elapsed, 1)
                        self.status["size"] = [image.shape[1], image.shape[0]]
                        window_start, window_n = time.monotonic(), 0
            except Exception as exc:
                self.status.update(state="error", error=str(exc))
                log.error("camera stream error: %s; reopening", exc)
                self._stop.wait(1.0)
            finally:
                cap.release()
        self.status["state"] = "stopped"


class _MockCapture:
    """Stands in for cv2.VideoCapture off the board: a video file or a folder of images."""

    def __init__(self, path: str, fps: float = 10) -> None:
        self.interval = 1.0 / fps
        self._video = None
        self._images: list[str] = []
        if os.path.isdir(path):
            self._images = sorted(
                p for ext in ("jpg", "jpeg", "png") for p in glob.glob(os.path.join(path, f"*.{ext}"))
            )
            if not self._images:
                raise RuntimeError(f"no images in {path}")
        else:
            self._video = cv2.VideoCapture(path)
        self._i = 0

    def read(self):
        time.sleep(self.interval)
        if self._video is not None:
            ok, frame = self._video.read()
            if not ok:
                self._video.set(cv2.CAP_PROP_POS_FRAMES, 0)
                ok, frame = self._video.read()
            return ok, frame
        path = self._images[self._i % len(self._images)]
        self._i += 1
        return True, cv2.imread(path)

    def release(self) -> None:
        if self._video is not None:
            self._video.release()
