"""MCU link: the sketch notifies ``bell_pressed`` when the IO2 button goes down.

On the Python side ``Bridge.call`` is synchronous and returns the value
directly, with no ``.result()`` (see GlassKilnController docs/architecture.md).
"""

from __future__ import annotations

import logging
import threading
from typing import Callable

log = logging.getLogger(__name__)


class DoorBridge:
    def __init__(self, on_bell: Callable[[], None], mock: bool = False) -> None:
        self.mock = mock
        self.ok = mock
        self._bridge = None
        if mock:
            return
        from arduino.app_utils import Bridge  # type: ignore

        self._bridge = Bridge
        Bridge.provide("bell_pressed", lambda *args: on_bell())
        threading.Thread(target=self._monitor, name="bridge-ping", daemon=True).start()

    def _monitor(self) -> None:
        stop = threading.Event()
        while True:
            self.ok = self.ping()
            stop.wait(10)

    def ping(self) -> bool:
        if self._bridge is None:
            return self.mock
        try:
            return bool(self._bridge.call("ping", timeout=2))
        except Exception as exc:
            log.debug("sketch ping failed: %s", exc)
            return False
