"""Visit state machine: someone arrives at the door, maybe rings, then leaves.

Pure logic with no camera, clock or I/O, so it can be unit-tested with
scripted timelines. The caller feeds it detections and bell presses and acts
on the transitions it returns.

    IDLE --(person in approach zone in >= hits of last window)--> PRESENT   "arrived"
    IDLE --(bell)--> PRESENT                                                "arrived"
    (max_visit_s reached: "left", then "closed" even if still present; a new visit follows)
    PRESENT --(nobody for leave_after_s, or max_visit_s)--> DEPARTED        "left"
    DEPARTED --(person again)--> PRESENT  (same visit)                      "returned"
    DEPARTED --(settle_s passed)--> IDLE                                    "closed"
"""

from __future__ import annotations

import itertools
from collections import deque
from dataclasses import dataclass, field

IDLE, PRESENT, DEPARTED = "idle", "present", "departed"

DELIVERY, VISITOR, CALLER, IGNORED = "delivery", "visitor", "caller", "ignored"

_ids = itertools.count(1)


@dataclass
class Visit:
    id: int
    start: float
    last_seen: float
    end: float | None = None
    bells: list[float] = field(default_factory=list)

    @property
    def dwell(self) -> float:
        return (self.end if self.end is not None else self.last_seen) - self.start


class VisitTracker:
    def __init__(self, cfg: dict) -> None:
        self.state = IDLE
        self.visit: Visit | None = None
        self._history: deque[tuple[float, bool]] = deque()
        self._left_at: float | None = None
        self._capped = False  # departure was forced by max_visit_s
        self.configure(cfg)

    def configure(self, cfg: dict) -> None:
        """Apply new thresholds without losing the visit in progress."""
        self.hits = int(cfg.get("arrive_hits", 3))
        self.window = int(cfg.get("arrive_window", 5))
        self.leave_after = float(cfg.get("leave_after_s", 8))
        self.settle = float(cfg.get("settle_s", 3))
        self.max_visit = float(cfg.get("max_visit_s", 900))
        self.min_dwell = float(cfg.get("min_dwell_s", 2))
        self._history = deque(self._history, maxlen=self.window)

    def observe(self, t: float, present: bool) -> list[str]:
        """Feed one detection result. Returns the transitions it caused."""
        self._history.append((t, present))
        if self.state == IDLE:
            if present and sum(p for _, p in self._history) >= self.hits:
                first = next(ts for ts, p in self._history if p)
                self._start(first, t)
                return ["arrived"]
            return []
        if self.state == PRESENT:
            assert self.visit is not None
            if present:
                self.visit.last_seen = t
            if t - self.visit.last_seen >= self.leave_after:
                return self._depart(t)
            if t - self.visit.start >= self.max_visit:
                # Close this visit even though someone is still there. Don't
                # let them "return" to it, or it flips left/returned on every
                # detection; a new visit starts once this one has closed.
                self._capped = True
                return self._depart(t)
            return []
        # DEPARTED
        assert self.visit is not None
        if present and not self._capped:
            self.state = PRESENT
            self.visit.last_seen = t
            self.visit.end = None
            self._left_at = None
            return ["returned"]
        return self.tick(t)

    def tick(self, t: float) -> list[str]:
        """Advance time with no new detection (e.g. detector stalled)."""
        if self.state == PRESENT and self.visit and t - self.visit.last_seen >= self.leave_after:
            return self._depart(t)
        if self.state == DEPARTED and self._left_at is not None and t - self._left_at >= self.settle:
            self.state = IDLE
            self._history.clear()
            self._capped = False
            return ["closed"]
        return []

    def bell(self, t: float) -> list[str]:
        if self.state == IDLE:
            self._start(t, t)
            self.visit.bells.append(t)
            return ["arrived"]
        assert self.visit is not None
        self.visit.bells.append(t)
        self.visit.last_seen = max(self.visit.last_seen, t)
        if self.state == DEPARTED:
            self.state = PRESENT
            self.visit.end = None
            self._left_at = None
            return ["returned"]
        return []

    def _start(self, start: float, now: float) -> None:
        self.state = PRESENT
        self.visit = Visit(id=next(_ids), start=start, last_seen=now)

    def _depart(self, t: float) -> list[str]:
        assert self.visit is not None
        self.state = DEPARTED
        self.visit.end = self.visit.last_seen
        self._left_at = t
        return ["left"]

    def classify(self, visit: Visit, parcel_left: bool | None) -> str:
        """delivery: a parcel appeared. visitor: rang, left nothing.
        caller: came and went without ringing. ignored: too brief to matter."""
        if parcel_left:
            return DELIVERY
        if visit.bells:
            return VISITOR
        if visit.dwell < self.min_dwell:
            return IGNORED
        return CALLER
