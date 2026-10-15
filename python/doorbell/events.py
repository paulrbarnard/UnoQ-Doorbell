"""Event log with snapshots, under data/events/<id>/ (event.json + JPEGs)."""

from __future__ import annotations

import json
import logging
import re
import shutil
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

_SAFE = re.compile(r"^[A-Za-z0-9_-]+$")


class EventStore:
    def __init__(self, root: Path, keep_days: float = 14, keep_max: int = 300) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.keep_days = keep_days
        self.keep_max = keep_max
        self._lock = threading.Lock()
        self._events: list[dict] = self._load()

    def _load(self) -> list[dict]:
        events = []
        for path in self.root.glob("*/event.json"):
            try:
                events.append(json.loads(path.read_text()))
            except (OSError, ValueError) as exc:
                log.warning("skipping bad event %s: %s", path, exc)
        events.sort(key=lambda e: e["time"], reverse=True)
        return events

    def add(self, kind: str, data: dict, images: dict[str, bytes], t: float | None = None) -> dict:
        """Save an event with its JPEG snapshots and return its record."""
        t = t or time.time()
        event_id = time.strftime("%Y%m%d-%H%M%S", time.localtime(t)) + f"-{kind}"
        with self._lock:
            n, base = 1, event_id
            while (self.root / event_id).exists():
                n += 1
                event_id = f"{base}-{n}"
            folder = self.root / event_id
            folder.mkdir(parents=True)
            names = []
            for name, jpeg in images.items():
                if jpeg:
                    (folder / f"{name}.jpg").write_bytes(jpeg)
                    names.append(name)
            record = {"id": event_id, "kind": kind, "time": t, "images": names, **data}
            (folder / "event.json").write_text(json.dumps(record, indent=1))
            self._events.insert(0, record)
            self._prune()
        return record

    def list(self, limit: int = 50) -> list[dict]:
        with self._lock:
            return self._events[:limit]

    def image_path(self, event_id: str, name: str) -> Path | None:
        if not (_SAFE.match(event_id) and _SAFE.match(name)):
            return None
        path = self.root / event_id / f"{name}.jpg"
        return path if path.is_file() else None

    def _prune(self) -> None:
        cutoff = time.time() - self.keep_days * 86400
        keep, drop = [], []
        for i, event in enumerate(self._events):
            (keep if i < self.keep_max and event["time"] >= cutoff else drop).append(event)
        for event in drop:
            shutil.rmtree(self.root / event["id"], ignore_errors=True)
        self._events = keep
