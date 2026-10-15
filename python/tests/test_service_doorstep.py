import itertools

import numpy as np

from doorbell.camera import Frame
from doorbell.config import DEFAULTS_PATH, Config
from doorbell.events import EventStore
from doorbell.notify import Notifier
from doorbell.service import DoorbellService
from tests.test_doorstep_and_geometry import textured


class FakeSource:
    """Returns the scripted images in turn from latest()."""

    status = {}

    def __init__(self, images):
        self._images = itertools.chain(images, itertools.repeat(images[-1]))
        self._seq = 0

    def correct(self, image):
        return image

    def latest(self):
        self._seq += 1
        return Frame(image=next(self._images), seq=self._seq, t=float(self._seq))


def make_service(tmp_path, images):
    config = Config(DEFAULTS_PATH, tmp_path)
    config.update({"doorstep": {"stable_s": 0, "stable_tries": 3}})
    return DoorbellService(config, FakeSource(images), EventStore(tmp_path / "events"), Notifier(lambda: {}))


def full_frame(fill=None, seed=1):
    img = np.kron(textured(154, 204, seed), np.ones((8, 8, 1), dtype=np.uint8))[:1232, :1632]
    if fill is not None:
        img = img.copy()
        img[700:1200, 500:1100] = fill  # covers the default doorstep zone
    return img


def test_occluded_then_still_doorstep_settles(tmp_path):
    hand, empty = full_frame(fill=(40, 60, 90)), full_frame()
    # shot pairs: (hand, empty) moving -> (empty, empty) still
    svc = make_service(tmp_path, [hand, empty, empty, empty])
    shot, _ = svc._settled_doorstep()
    assert shot is not None
    assert not svc._changed(svc._doorstep(svc.source.latest())[0], shot[0]).changed


def test_never_still_doorstep_is_unknown(tmp_path):
    hand, empty = full_frame(fill=(40, 60, 90)), full_frame()
    svc = make_service(tmp_path, [hand, empty] * 10)
    shot, last_jpeg = svc._settled_doorstep()
    assert shot is None
    assert last_jpeg


def test_people_in_view_blocks_settling(tmp_path):
    svc = make_service(tmp_path, [full_frame()])
    svc._last_people = [{"label": "person"}]
    shot, _ = svc._settled_doorstep()
    assert shot is None


def test_reference_needs_two_matching_shots(tmp_path):
    hand, empty = full_frame(fill=(40, 60, 90)), full_frame()
    svc = make_service(tmp_path, [empty])
    svc._update_reference(Frame(hand, 1, 0.0))
    svc._update_reference(Frame(empty, 2, 3.0))
    assert svc._ref is None  # hand then empty: not stable yet
    svc._update_reference(Frame(empty, 3, 6.0))
    assert svc._ref is not None


def test_zone_overlap():
    from doorbell.service import zone_overlap

    zone = (0.4, 0.6, 0.5, 0.8)
    assert zone_overlap((0.0, 0.0, 1.0, 1.0), zone) == 1.0
    assert zone_overlap((0.0, 0.0, 0.3, 1.0), zone) == 0.0
    assert abs(zone_overlap((0.45, 0.0, 1.0, 1.0), zone) - 0.5) < 1e-9


FAR = (0.30, 0.45, 0.38, 0.72)    # small box out at the porch opening
NEAR = (0.13, 0.24, 0.48, 0.83)   # big box right by the camera / house door


def _finish(tmp_path, post_frames, parcel_change=False, bells=(), first=FAR, last=FAR):
    from doorbell.service import _VisitContext
    from doorbell.visits import Visit

    empty = full_frame()
    after = full_frame(fill=(40, 60, 90)) if parcel_change else empty
    svc = make_service(tmp_path, [after])
    svc.config.update({"zones": {"postbox": [0.43, 0.62, 0.54, 0.85]}})
    ctx = _VisitContext(svc._doorstep(Frame(empty, 1, 0.0)))
    ctx.post_frames = post_frames
    ctx.best_jpeg = b"jpg"
    ctx.first_box, ctx.last_box = first, last
    import time

    now = time.time()  # the event store prunes old events, so use real times
    visit = Visit(id=1, start=now - 10, last_seen=now, end=now, bells=[now - 5 for _ in bells])
    svc._finish_visit(visit, ctx)
    return svc.events.list()[0]


def test_reaching_into_postbox_is_post(tmp_path):
    assert _finish(tmp_path, post_frames=6)["kind"] == "post"


def test_brief_brush_past_postbox_is_not_post(tmp_path):
    assert _finish(tmp_path, post_frames=1)["kind"] == "caller"


def test_post_and_ringing_is_still_post(tmp_path):
    assert _finish(tmp_path, post_frames=6, bells=[1])["kind"] == "post"


def test_parcel_on_mat_beats_post(tmp_path):
    assert _finish(tmp_path, post_frames=6, parcel_change=True)["kind"] == "delivery"


def test_collecting_post_on_the_way_in_is_not_post(tmp_path):
    ev = _finish(tmp_path, post_frames=14, first=FAR, last=NEAR)
    assert ev["kind"] == "caller" and ev["into_house"] and not ev["from_house"]


def test_leaving_the_house_is_marked(tmp_path):
    ev = _finish(tmp_path, post_frames=0, first=NEAR, last=FAR)
    assert ev["from_house"] and not ev["into_house"]
