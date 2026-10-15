"""Settings: python/config/doorbell.yaml defaults plus data/config.yaml overrides.

The web page edits the overrides, so the checked-in defaults stay untouched.
"""

from __future__ import annotations

import copy
import logging
import threading
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

APP_DIR = Path(__file__).resolve().parents[2]
DEFAULTS_PATH = APP_DIR / "python" / "config" / "doorbell.yaml"
DATA_DIR = APP_DIR / "data"


def deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


class Config:
    def __init__(self, defaults_path: Path = DEFAULTS_PATH, data_dir: Path = DATA_DIR) -> None:
        self._defaults_path = Path(defaults_path)
        self._override_path = Path(data_dir) / "config.yaml"
        self._lock = threading.Lock()
        self._defaults: dict = {}
        self._overrides: dict = {}
        self.data: dict = {}
        self.load()

    def load(self) -> None:
        with open(self._defaults_path, "r", encoding="utf-8") as fh:
            self._defaults = yaml.safe_load(fh) or {}
        self._overrides = {}
        if self._override_path.exists():
            try:
                with open(self._override_path, "r", encoding="utf-8") as fh:
                    self._overrides = yaml.safe_load(fh) or {}
            except (OSError, yaml.YAMLError) as exc:
                log.warning("ignoring unreadable %s: %s", self._override_path, exc)
        self.data = deep_merge(self._defaults, self._overrides)

    def section(self, name: str) -> dict:
        return self.data.get(name, {})

    def update(self, changes: dict[str, Any]) -> dict:
        """Merge ``changes`` into the overrides, save them, and return the new config."""
        with self._lock:
            self._overrides = deep_merge(self._overrides, changes)
            self._override_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._override_path, "w", encoding="utf-8") as fh:
                yaml.safe_dump(self._overrides, fh, sort_keys=False)
            self.data = deep_merge(self._defaults, self._overrides)
            return self.data
