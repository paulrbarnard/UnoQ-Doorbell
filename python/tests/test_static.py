from doorbell.detector import Detection
from doorbell.static import StaticFilter, iou

CFG = {"static_after_s": 60, "static_max_move": 0.02, "static_iou": 0.5, "static_forget_s": 600, "track_gap_s": 5}
PILLAR = (0.30, 0.20, 0.36, 0.80)


def det(box, c=0.5):
    return Detection("person", c, box)


def run(f, t0, t1, boxes_at, step=0.4):
    kept_n = ignored_n = 0
    t = t0
    while t < t1:
        kept, ignored = f.update(t, [det(b) for b in boxes_at(t)])
        kept_n += len(kept)
        ignored_n += len(ignored)
        t += step
    return kept_n, ignored_n


def test_iou():
    assert iou((0, 0, 1, 1), (0, 0, 1, 1)) == 1
    assert iou((0, 0, 1, 1), (2, 2, 3, 3)) == 0


def test_pillar_becomes_static_after_a_minute():
    f = StaticFilter(CFG)
    kept, _ = run(f, 0, 59, lambda t: [PILLAR])
    assert kept > 0  # counted at first: we can't know yet
    kept, ignored = run(f, 61, 300, lambda t: [PILLAR])
    assert kept == 0 and ignored > 0


def test_flickering_pillar_stays_static():
    f = StaticFilter(CFG)
    run(f, 0, 70, lambda t: [PILLAR])
    # detector misses it now and then (backlight changes)
    kept, ignored = run(f, 70, 400, lambda t: [PILLAR] if int(t) % 7 else [])
    assert kept == 0 and ignored > 0


def test_visitor_next_to_static_pillar_still_counts():
    f = StaticFilter(CFG)
    run(f, 0, 70, lambda t: [PILLAR])

    def scene(t):  # someone walks up from the right, separate from the pillar
        x = 0.8 - 0.002 * (t - 70)
        return [PILLAR, (x - 0.1, 0.3, x + 0.1, 0.95)]

    kept, ignored = run(f, 70, 130, scene)
    assert kept > 100  # the visitor, every detection


def test_person_waiting_and_shifting_is_not_suppressed():
    f = StaticFilter(CFG)

    def fidget(t):  # standing at the door, shifting weight every ~20 s
        dx = 0.03 if int(t // 20) % 2 else 0.0
        return [(0.5 + dx, 0.3, 0.7 + dx, 0.95)]

    kept, ignored = run(f, 0, 180, fidget)
    assert ignored == 0


def test_static_spot_is_forgotten_after_it_goes():
    f = StaticFilter(CFG)
    run(f, 0, 70, lambda t: [PILLAR])
    f.update(700, [])  # unseen for 10+ minutes
    kept, _ = f.update(701, [det(PILLAR)])
    assert kept  # counted again (and re-learned if it stays)


def test_weak_flickering_ghost_is_learned_from_low_confidence_hits():
    from doorbell.detector import Detector

    d = Detector.__new__(Detector)  # just the filtering part
    d._cfg = lambda: {**CFG, "confidence": 0.5, "ignore_static": True}
    d.static = StaticFilter(CFG)
    counted = 0
    t = 0.0
    while t < 300:
        c = 0.55 if int(t) % 15 == 0 else 0.35  # mostly below threshold, now and then above
        people, _ = d.filter(t, [det(PILLAR, c)])
        if t > 61:
            counted += len(people)
        t += 0.4
    assert counted == 0
