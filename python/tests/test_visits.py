from doorbell.visits import CALLER, DELIVERY, DEPARTED, IDLE, IGNORED, PRESENT, VISITOR, VisitTracker

CFG = {"arrive_hits": 3, "arrive_window": 5, "leave_after_s": 8, "settle_s": 3, "max_visit_s": 900, "min_dwell_s": 2}


def run(tracker, timeline, step=0.5, start=0.0):
    """Feed a list of booleans at ``step`` seconds apart; return all transitions with times."""
    out = []
    t = start
    for present in timeline:
        for tr in tracker.observe(t, present):
            out.append((t, tr))
        t += step
    return out, t


def test_single_false_positive_does_not_start_visit():
    tr = VisitTracker(CFG)
    out, _ = run(tr, [False, True, False, False, True, False, False])
    assert out == []
    assert tr.state == IDLE


def test_arrive_leave_close():
    tr = VisitTracker(CFG)
    out, t = run(tr, [False] * 4 + [True] * 20 + [False] * 30)
    names = [n for _, n in out]
    assert names == ["arrived", "left", "closed"]
    assert tr.state == IDLE
    visit = tr.visit
    assert visit.start == 2.0  # first sighting, not the confirming one
    assert abs(visit.dwell - 9.5) < 1e-6


def test_returning_within_leave_window_is_same_visit():
    tr = VisitTracker(CFG)
    out, _ = run(tr, [True] * 6 + [False] * 10 + [True] * 4 + [False] * 30)
    assert [n for _, n in out] == ["arrived", "left", "closed"]


def test_return_after_left_before_close():
    tr = VisitTracker(CFG)
    out, t = run(tr, [True] * 6 + [False] * 17)  # left at +8s absent
    assert tr.state == DEPARTED
    first = tr.visit.id
    assert tr.observe(t, True) == ["returned"]
    assert tr.state == PRESENT and tr.visit.id == first


def test_bell_without_detection_starts_visit():
    tr = VisitTracker(CFG)
    assert tr.bell(10.0) == ["arrived"]
    assert tr.state == PRESENT
    assert tr.visit.bells == [10.0]
    out = []
    for i in range(1, 40):
        out += tr.tick(10.0 + i * 0.5)
    assert out == ["left", "closed"]


def test_classification():
    tr = VisitTracker(CFG)
    run(tr, [True] * 10 + [False] * 30)
    v = tr.visit
    assert tr.classify(v, parcel_left=True) == DELIVERY
    assert tr.classify(v, parcel_left=False) == CALLER
    assert tr.classify(v, parcel_left=None) == CALLER
    v.bells.append(1.0)
    assert tr.classify(v, parcel_left=False) == VISITOR
    assert tr.classify(v, parcel_left=True) == DELIVERY


def test_brief_blip_is_ignored():
    tr = VisitTracker({**CFG, "min_dwell_s": 5})
    run(tr, [True] * 4 + [False] * 30, step=0.5)
    assert tr.classify(tr.visit, parcel_left=False) == IGNORED


def test_max_visit_closes_without_flapping():
    tr = VisitTracker({**CFG, "max_visit_s": 10})
    out, _ = run(tr, [True] * 60)  # someone stays for 30 s
    names = [n for _, n in out]
    assert "returned" not in names
    # 10 s visit, closed after settle, then a fresh visit starts.
    assert names[:4] == ["arrived", "left", "closed", "arrived"]
    assert names.count("left") <= 3


def test_configure_keeps_visit():
    tr = VisitTracker(CFG)
    run(tr, [True] * 6)
    visit = tr.visit
    tr.configure({**CFG, "leave_after_s": 2})
    assert tr.visit is visit and tr.state == PRESENT
    assert tr.leave_after == 2
