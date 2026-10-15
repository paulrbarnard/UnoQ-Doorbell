"""Ignore "people" that never move: things in the scene the detector mistakes for a person.

In the porch, the stone pillars either side of the bright doorway turn into
person-shaped silhouettes and were detected at ~0.4–0.5 confidence all day
(2026-10-12..15, ~50 false visits a day). They never move. A real visitor
does, even when standing at the door.

Each detection is matched to a track by box overlap. A track whose box stays
put (centre moves less than ``max_move`` of the frame) for ``static_after_s``
is marked static, and its detections are dropped. A static track is forgotten
once it hasn't been seen for ``forget_s``.
"""

from __future__ import annotations

from dataclasses import dataclass


def iou(a, b) -> float:
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, x1 - x0) * max(0.0, y1 - y0)
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def _centre(box):
    return ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)


@dataclass
class _Track:
    anchor: tuple  # box where the track last "settled"; movement is measured from here
    box: tuple
    since: float  # when it settled at the anchor
    last_seen: float
    static: bool = False


class StaticFilter:
    def __init__(self, cfg: dict | None = None) -> None:
        self.tracks: list[_Track] = []
        self.configure(cfg or {})

    def configure(self, cfg: dict) -> None:
        self.static_after = float(cfg.get("static_after_s", 60))
        self.max_move = float(cfg.get("static_max_move", 0.02))
        self.match_iou = float(cfg.get("static_iou", 0.5))
        self.forget = float(cfg.get("static_forget_s", 600))
        self.gap = float(cfg.get("track_gap_s", 5))  # a moving track survives missed detections this long

    def update(self, t: float, detections: list) -> tuple[list, list]:
        """Return (kept, ignored) detections. Each must have a ``.box`` (x0, y0, x1, y1)."""
        kept, ignored = [], []
        used: set[int] = set()
        for det in detections:
            best, best_iou = None, self.match_iou
            for i, tr in enumerate(self.tracks):
                if i in used:
                    continue
                o = iou(det.box, tr.box)
                if o >= best_iou:
                    best, best_iou = i, o
            if best is None:
                self.tracks.append(_Track(anchor=tuple(det.box), box=tuple(det.box), since=t, last_seen=t))
                used.add(len(self.tracks) - 1)
                kept.append(det)
                continue
            used.add(best)
            tr = self.tracks[best]
            tr.box, tr.last_seen = tuple(det.box), t
            (ax, ay), (cx, cy) = _centre(tr.anchor), _centre(det.box)
            if abs(cx - ax) > self.max_move or abs(cy - ay) > self.max_move:
                tr.anchor, tr.since, tr.static = tuple(det.box), t, False  # it moved: start over
            elif t - tr.since >= self.static_after:
                tr.static = True
            (ignored if tr.static else kept).append(det)
        self.tracks = [
            tr for tr in self.tracks if t - tr.last_seen <= (self.forget if tr.static else self.gap)
        ]
        return kept, ignored

    @property
    def static_boxes(self) -> list[tuple]:
        return [tr.box for tr in self.tracks if tr.static]
