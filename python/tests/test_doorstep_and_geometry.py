import numpy as np

from doorbell import doorstep
from doorbell.camera import crop_fraction
from doorbell.detector import letterbox, unletterbox
from doorbell.events import EventStore


def textured(h=240, w=320, seed=1):
    rng = np.random.default_rng(seed)
    base = rng.integers(60, 120, size=(h // 8, w // 8, 3), dtype=np.uint8)
    return np.kron(base, np.ones((8, 8, 1), dtype=np.uint8))


def test_parcel_is_detected():
    before = textured()
    after = before.copy()
    after[80:180, 100:220] = (30, 90, 160)  # a brown box
    change = doorstep.compare(before, after)
    assert change.changed
    assert change.fraction > 0.1


def test_same_scene_with_noise_is_not_a_parcel():
    before = textured()
    rng = np.random.default_rng(2)
    after = np.clip(before.astype(int) + rng.integers(-8, 9, before.shape), 0, 255).astype(np.uint8)
    assert not doorstep.compare(before, after).changed


def test_global_brightness_change_is_not_a_parcel():
    before = textured()
    after = np.clip(before.astype(float) * 1.5, 0, 255).astype(np.uint8)
    assert not doorstep.compare(before, after).changed


def test_crop_fraction_offsets():
    img = np.zeros((1232, 1632, 3), np.uint8)
    crop, (ox, oy) = crop_fraction(img, [0.25, 0.5, 0.75, 1.0])
    assert (ox, oy) == (408, 616)
    assert crop.shape[:2] == (616, 816)


def test_letterbox_roundtrip():
    full = (1632, 1232)
    img = np.zeros((1232, 1632, 3), np.uint8)
    crop, offset = crop_fraction(img, [0.2, 0.3, 0.8, 1.0])
    canvas, scale, pad = letterbox(crop, (320, 320))
    assert canvas.shape == (320, 320, 3)
    # A box covering the whole crop maps back to the zone.
    ch, cw = crop.shape[:2]
    box = [pad[0], pad[1], pad[0] + cw * scale, pad[1] + ch * scale]
    x0, y0, x1, y1 = unletterbox(box, scale, pad, offset, full)
    assert abs(x0 - 0.2) < 0.01 and abs(y0 - 0.3) < 0.01
    assert abs(x1 - 0.8) < 0.01 and abs(y1 - 1.0) < 0.01


def test_event_store_add_list_prune(tmp_path):
    store = EventStore(tmp_path, keep_days=14, keep_max=3)
    for i in range(5):
        store.add("bell", {"n": i}, {"snapshot": b"\xff\xd8jpeg"}, t=1_900_000_000 + i)
    events = store.list()
    assert [e["n"] for e in events] == [4, 3, 2]
    assert store.image_path(events[0]["id"], "snapshot").read_bytes() == b"\xff\xd8jpeg"
    assert store.image_path("../etc", "passwd") is None
    assert len(list(tmp_path.iterdir())) == 3
    # Reload from disk.
    assert [e["n"] for e in EventStore(tmp_path, keep_max=3).list()] == [4, 3, 2]


def test_washed_out_highlights_are_not_a_parcel():
    # Upper part bright (sunlit path), then exposure rises and it washes out completely.
    before = textured()
    before[:90] = np.clip(before[:90].astype(int) + 130, 0, 255).astype(np.uint8)
    after = before.copy()
    after[:90] = 255
    after = np.clip(after.astype(float) * 1.1, 0, 255).astype(np.uint8)
    assert not doorstep.compare(before, after).changed


def test_white_parcel_on_dark_mat_still_counts():
    before = textured()
    after = before.copy()
    after[110:200, 90:230] = (245, 245, 245)  # white box
    change = doorstep.compare(before, after)
    assert change.changed and change.fraction > 0.1
