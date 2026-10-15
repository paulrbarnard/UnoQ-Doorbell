"""iPhone push notifications through Pushover (https://pushover.net).

Pushover lets each alert choose its sound, including your own uploaded sounds
(pushover.net → Your Custom Sounds), so the bell can play a real chime on the
iPhone. It needs two keys, both kept only in data/config.yaml on the board:
  pushover_token  the "Doorbell" application's API token
  pushover_user   your user key
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
import uuid

log = logging.getLogger(__name__)

API_URL = "https://api.pushover.net/1/messages.json"

# Pushover priority: 1 = high (shown in red, bypasses Pushover quiet hours).
# 2 (emergency) would repeat until acknowledged, too much for a doorbell.
PRIORITY = {"bell": 1, "delivery": 0, "post": 0, "visit": -1, "test": 0}


def _multipart(fields: dict[str, str], image: bytes | None) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    parts = []
    for name, value in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    if image:
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="attachment"; filename="snapshot.jpg"\r\n'
            "Content-Type: image/jpeg\r\n\r\n".encode()
            + image
            + b"\r\n"
        )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


class Notifier:
    def __init__(self, cfg_getter) -> None:
        self._cfg = cfg_getter  # returns the current notify config section
        self.last = {"time": None, "ok": None, "detail": "not sent yet"}

    def configured(self) -> bool:
        cfg = self._cfg()
        return bool(cfg.get("pushover_token")) and bool(cfg.get("pushover_user"))

    def enabled(self, kind: str) -> bool:
        return self.configured() and bool(self._cfg().get(f"on_{kind}", False))

    def send(self, kind: str, title: str, message: str, image: bytes | None = None) -> None:
        """Send in the background so a slow network never holds up the doorbell."""
        threading.Thread(target=self._send, args=(kind, title, message, image), daemon=True).start()

    def _send(self, kind: str, title: str, message: str, image: bytes | None) -> None:
        cfg = self._cfg()
        if not self.configured():
            self.last = {"time": time.time(), "ok": False, "detail": "Pushover token/user key not set"}
            return
        fields = {
            "token": cfg["pushover_token"],
            "user": cfg["pushover_user"],
            "title": title,
            "message": message,
            "priority": str(PRIORITY.get(kind, 0)),
        }
        sound = cfg.get(f"{kind}_sound") or cfg.get("default_sound")
        if sound:
            fields["sound"] = sound
        if image and len(image) > 5_000_000:  # Pushover's attachment limit
            image = None
        body, content_type = _multipart(fields, image)
        req = urllib.request.Request(API_URL, data=body, headers={"Content-Type": content_type}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                self.last = {"time": time.time(), "ok": True, "detail": f"HTTP {resp.status}"}
        except urllib.error.HTTPError as exc:
            # Pushover explains rejections (bad token, unknown sound...) in JSON.
            try:
                detail = "; ".join(json.loads(exc.read()).get("errors", [])) or str(exc)
            except ValueError:
                detail = str(exc)
            self.last = {"time": time.time(), "ok": False, "detail": detail}
            log.warning("Pushover push failed: %s", detail)
        except Exception as exc:
            self.last = {"time": time.time(), "ok": False, "detail": str(exc)}
            log.warning("Pushover push failed: %s", exc)
