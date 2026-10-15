"""Did something get left on the doorstep? Compare the doorstep ROI before and after a visit."""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

_WORK_WIDTH = 240  # compare at a fixed small size; noise and cost both drop


@dataclass
class DoorstepChange:
    changed: bool
    fraction: float  # share of the ROI that changed, 0..1
    mask: np.ndarray  # uint8 0/255 at work size, for debugging overlays


_CHROMA_GAIN = 2.0  # a/b channels span less range than L; weight them up


def _prep(image: np.ndarray) -> np.ndarray:
    """Small, blurred Lab float image."""
    h, w = image.shape[:2]
    size = (_WORK_WIDTH, max(1, int(round(h * _WORK_WIDTH / w))))
    small = cv2.GaussianBlur(cv2.resize(image, size, interpolation=cv2.INTER_AREA), (5, 5), 0)
    return cv2.cvtColor(small, cv2.COLOR_BGR2LAB).astype(np.float32)


def compare(
    before: np.ndarray,
    after: np.ndarray,
    diff_threshold: float = 28,
    changed_fraction: float = 0.06,
    highlight: float = 215,
) -> DoorstepChange:
    """Return how much of the doorstep changed between two BGR crops of the same ROI.

    Both lightness and colour are compared, because a brown box on a brown
    mat can match in brightness alone. The lightness of ``after`` is matched
    to ``before`` first (median ratio), so a cloud or the porch light switching
    on isn't read as a parcel.

    Areas that were already near-white *before* the visit (Lab L >= ``highlight``,
    e.g. the sunlit path beyond the porch) are ignored. They hold no detail,
    and a small exposure or sun change washes them out completely: that read as
    13.5% "changed" when nothing was left (2026-10-15). A bright parcel on a
    darker mat still counts, because only the *before* brightness is checked.
    """
    b = _prep(before)
    a = _prep(after)
    if a.shape != b.shape:
        a = cv2.resize(a, (b.shape[1], b.shape[0]), interpolation=cv2.INTER_AREA)
    usable = b[..., 0] < highlight
    if usable.mean() < 0.2:  # nearly all washed out: fall back to the whole zone
        usable[:] = True
    a[..., 0] *= (np.median(b[..., 0][usable]) + 1e-3) / (np.median(a[..., 0][usable]) + 1e-3)
    d = np.abs(a - b)
    diff = np.maximum(d[..., 0], _CHROMA_GAIN * np.maximum(d[..., 1], d[..., 2]))
    mask = ((diff > diff_threshold) & usable).astype(np.uint8) * 255
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
    fraction = float(np.count_nonzero(mask)) / mask.size
    return DoorstepChange(changed=fraction >= changed_fraction, fraction=fraction, mask=mask)
