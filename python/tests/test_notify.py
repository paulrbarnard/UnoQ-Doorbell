import time

from doorbell import notify


class FakeResp:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_pushover_request(monkeypatch):
    sent = {}

    def fake_urlopen(req, timeout):
        sent["url"] = req.full_url
        sent["body"] = req.data
        sent["type"] = req.headers["Content-type"]
        return FakeResp()

    monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)
    cfg = {"pushover_token": "tok", "pushover_user": "usr", "on_bell": True, "bell_sound": "doorbell"}
    n = notify.Notifier(lambda: cfg)
    assert n.enabled("bell") and not n.enabled("delivery")
    n._send("bell", "Doorbell", "Someone rang", b"\xff\xd8jpeg")
    assert sent["url"] == notify.API_URL
    assert sent["type"].startswith("multipart/form-data; boundary=")
    body = sent["body"]
    for field, value in [("token", b"tok"), ("user", b"usr"), ("sound", b"doorbell"), ("priority", b"1")]:
        assert f'name="{field}"\r\n\r\n'.encode() + value in body
    assert b'filename="snapshot.jpg"' in body and b"\xff\xd8jpeg" in body
    assert n.last["ok"] is True


def test_not_configured():
    n = notify.Notifier(lambda: {"on_bell": True})
    assert not n.enabled("bell")
    n._send("bell", "t", "m", None)
    assert n.last["ok"] is False
