"""
Unit tests for the pure-Python fingerprint matcher (TAW-V fork).

These need no Home Assistant install. Run with either:
    python3 -I -m unittest discover -s tests -p "test_fingerprint_core.py"
    pytest tests/test_fingerprint_core.py

All addresses and rooms below are made up.
"""

from __future__ import annotations

import importlib.util
import math
import random
import sys
import unittest
from pathlib import Path

_PATH = Path(__file__).resolve().parents[1] / "custom_components" / "bermuda" / "fingerprint_core.py"
_spec = importlib.util.spec_from_file_location("fingerprint_core", _PATH)
fc = importlib.util.module_from_spec(_spec)
sys.modules["fingerprint_core"] = fc
_spec.loader.exec_module(fc)

# Made-up scanners: address -> (x, y) position in metres and the room it is in.
SCANNERS = {
    "aa:00:00:00:00:01": ((0.0, 0.0), "kitchen"),
    "aa:00:00:00:00:02": ((4.0, 1.0), "kitchen"),
    "aa:00:00:00:00:03": ((9.0, 0.0), "living"),
    "aa:00:00:00:00:04": ((12.0, 3.0), "living"),
    "aa:00:00:00:00:05": ((6.0, 8.0), "bedroom"),
    "aa:00:00:00:00:06": ((1.0, 9.0), "bathroom"),
}
# Room centres. "attic" has no scanner of its own.
ROOMS = {
    "kitchen": (2.0, 1.0),
    "living": (10.0, 1.5),
    "bedroom": (7.0, 7.0),
    "bathroom": (1.5, 8.0),
    "attic": (5.0, 5.0),
}
SCANNER_ROOM = {s: room for s, (_, room) in SCANNERS.items()}


def expected_rssi(pos, scanner, offset=0.0):
    (sx, sy), _ = SCANNERS[scanner]
    d = max(0.5, math.hypot(pos[0] - sx, pos[1] - sy))
    return -45 - 25 * math.log10(d) + offset


def synth_spot(rng, room, spot_id, jitter=0.8, duration=120, offset=0.0, noise=3.0):
    """Record a synthetic spot near the room centre: one reading per scanner every ~3 s."""
    cx, cy = ROOMS[room]
    pos = (cx + rng.uniform(-jitter, jitter), cy + rng.uniform(-jitter, jitter))
    rec = fc.SpotRecorder(start=1000.0, duration=duration)
    for scanner in SCANNERS:
        mean = expected_rssi(pos, scanner, offset)
        t = 1000.0 + rng.uniform(0, 3)
        while t < 1000.0 + duration:
            rssi = round(mean + rng.gauss(0, noise))
            if rssi > -98:  # far scanners only hear the strong packets
                rec.add(scanner, t, rssi)
            t += rng.uniform(2.8, 3.6)
    spot = rec.build()
    spot.update({"id": spot_id, "room": room, "label": spot_id})
    return spot


def synth_live(rng, room, offset=0.0, noise=3.0, jitter=0.8, drop=()):
    cx, cy = ROOMS[room]
    pos = (cx + rng.uniform(-jitter, jitter), cy + rng.uniform(-jitter, jitter))
    vec = {}
    for scanner in SCANNERS:
        if scanner in drop:
            continue
        val = expected_rssi(pos, scanner, offset) + rng.gauss(0, noise / 2)  # median of a few readings
        if val > -98:
            vec[scanner] = val
    return vec


def make_house(seed=1, per_room=3):
    rng = random.Random(seed)
    spots = []
    for room in ROOMS:
        for i in range(per_room):
            spots.append(synth_spot(rng, room, f"{room}-{i}"))
    return spots


class TestHelpers(unittest.TestCase):
    def test_median(self):
        self.assertEqual(fc.median([3, 1, 2]), 2)
        self.assertEqual(fc.median([4, 1, 2, 3]), 2.5)

    def test_live_vector_window_dedupe_invalid(self):
        readings = {
            "s1": [(100.0, -70), (100.0, -70), (95.0, -60), (87.0, -50)],  # 87 is outside 12 s window
            "s2": [(99.0, -127)],  # invalid only
            "s3": [(80.0, -60)],  # stale only
        }
        vec = fc.live_vector(readings, nowstamp=100.0)
        self.assertEqual(vec, {"s1": -65.0})


class TestRecorder(unittest.TestCase):
    def test_build_stats_and_windows(self):
        rec = fc.SpotRecorder(start=0.0, duration=30)  # 3 windows
        for t, r in [(1, -60), (4, -62), (11, -64), (21, -66)]:
            self.assertTrue(rec.add("s1", t, r))
        self.assertFalse(rec.add("s1", 4, -62))  # duplicate
        self.assertFalse(rec.add("s1", -1, -60))  # before start
        self.assertFalse(rec.add("s1", 31, -60))  # after end
        self.assertFalse(rec.add("s1", 5, -127))  # invalid
        rec.add("s2", 2, -90)
        spot = rec.build()
        self.assertEqual(spot["samples"], 5)
        s1 = spot["scanners"]["s1"]
        self.assertEqual(s1["n"], 4)
        self.assertAlmostEqual(s1["mean"], -63.0)
        self.assertAlmostEqual(s1["seen"], 1.0)
        self.assertAlmostEqual(spot["scanners"]["s2"]["seen"], round(1 / 3, 3))
        self.assertEqual(spot["window_scanners"], ["s1", "s2"])
        self.assertEqual(spot["windows"], [[-61, -90], [-64, None], [-66, None]])
        wins = fc.spot_windows(spot)
        self.assertEqual(wins[1], {"s1": -64.0})

    def test_gap_tracking(self):
        rec = fc.SpotRecorder(start=0.0, duration=60)
        rec.add("s1", 2, -70)
        rec.finish_gap(10)
        rec.finish_gap(40)
        self.assertAlmostEqual(rec.max_gap, 38)


class TestMatcher(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.spots = make_house()
        cls.matcher = fc.Matcher(cls.spots)

    def accuracy(self, matcher, **live_kwargs):
        rng = random.Random(42)
        ok = n = 0
        for room in ROOMS:
            for _ in range(20):
                res = matcher.match(synth_live(rng, room, **live_kwargs.get(room, {})))
                n += 1
                ok += res is not None and max(res.posterior, key=res.posterior.get) == room
        return ok / n

    def test_classifies_rooms(self):
        self.assertGreaterEqual(self.accuracy(self.matcher), 0.9)

    def test_room_without_proxy(self):
        rng = random.Random(7)
        hits = 0
        for _ in range(20):
            res = self.matcher.match(synth_live(rng, "attic"))
            hits += max(res.posterior, key=res.posterior.get) == "attic"
        self.assertGreaterEqual(hits, 16)

    def test_posterior_sums_to_one(self):
        res = self.matcher.match(synth_live(random.Random(3), "kitchen"))
        self.assertAlmostEqual(sum(res.posterior.values()), 1.0)
        self.assertEqual(res.best.room, max(res.posterior, key=res.posterior.get))
        self.assertIsNotNone(res.second)
        self.assertNotEqual(res.second.room, res.best.room)

    def test_offset_invariance(self):
        rng = random.Random(5)
        live = synth_live(rng, "bedroom", noise=0.5)
        shifted = {s: v - 6 for s, v in live.items()}  # e.g. phone in a pocket
        base = self.matcher.match(live)
        res = self.matcher.match(shifted)
        self.assertEqual(max(res.posterior, key=res.posterior.get), "bedroom")
        self.assertAlmostEqual(res.best.offset - base.best.offset, -6, delta=1.5)

    def test_offset_is_clamped(self):
        live = synth_live(random.Random(5), "living", noise=0.5)
        res = self.matcher.match({s: v + 30 for s, v in live.items()})
        if res is not None:
            self.assertLessEqual(abs(res.best.offset), fc.OFFSET_MAX_DB)

    def test_offline_scanner_is_ignored(self):
        # The kitchen's strongest scanner goes offline: it is neither heard nor in `active`.
        dead = "aa:00:00:00:00:01"
        active = set(SCANNERS) - {dead}
        rng = random.Random(9)
        hits = 0
        for _ in range(20):
            res = self.matcher.match(synth_live(rng, "kitchen", drop={dead}), active)
            hits += max(res.posterior, key=res.posterior.get) == "kitchen"
        self.assertGreaterEqual(hits, 17)

    def test_unknown_scanner_is_ignored(self):
        live = synth_live(random.Random(11), "living")
        base = self.matcher.match(live)
        live["ff:ff:ff:ff:ff:01"] = -40.0  # a new proxy no spot knows
        res = self.matcher.match(live)
        self.assertAlmostEqual(res.best.score, base.best.score)

    def test_min_common(self):
        self.assertIsNone(self.matcher.match({"aa:00:00:00:00:01": -60.0, "aa:00:00:00:00:02": -70.0}))
        self.assertIsNone(fc.Matcher([]).match({"aa:00:00:00:00:01": -60.0}))

    def test_without_offset(self):
        m = fc.Matcher(self.spots, use_offset=False)
        self.assertGreaterEqual(self.accuracy(m), 0.85)
        res = m.match(synth_live(random.Random(1), "kitchen"))
        self.assertEqual(res.best.offset, 0.0)

    def test_temperature_sharpens(self):
        live = synth_live(random.Random(2), "living")
        hot = fc.Matcher(self.spots, temperature=4.0).match(live)
        cold = fc.Matcher(self.spots, temperature=0.25).match(live)
        self.assertGreater(max(cold.posterior.values()), max(hot.posterior.values()))


class TestSmoother(unittest.TestCase):
    def test_first_result_and_hysteresis(self):
        sm = fc.RoomSmoother(tau=0.001)  # tau ~0: EMA follows input, isolates the switch logic
        r = sm.update({"a": 0.7, "b": 0.3}, 0.0)
        self.assertEqual((r.room, r.reason), ("a", fc.REASON_MATCH))
        # b leads by 0.2 (>= margin) but below instant threshold: needs 3 cycles
        for t in (10.0, 11.0):
            r = sm.update({"a": 0.4, "b": 0.6}, t)
            self.assertEqual(r.incumbent, "a")
            self.assertTrue(r.holding)
        r = sm.update({"a": 0.4, "b": 0.6}, 12.0)
        self.assertEqual(r.room, "b")

    def test_instant_switch_respects_dwell(self):
        sm = fc.RoomSmoother(tau=0.001, min_dwell=5)
        sm.update({"a": 0.9, "b": 0.1}, 0.0)
        r = sm.update({"a": 0.05, "b": 0.95}, 2.0)  # within dwell
        self.assertEqual(r.incumbent, "a")
        r = sm.update({"a": 0.05, "b": 0.95}, 6.0)
        self.assertEqual(r.room, "b")

    def test_small_lead_does_not_switch(self):
        sm = fc.RoomSmoother(tau=0.001)
        sm.update({"a": 0.6, "b": 0.4}, 0.0)
        for t in range(10, 20):
            r = sm.update({"a": 0.47, "b": 0.53}, float(t))
        self.assertEqual(r.incumbent, "a")
        self.assertEqual(r.reason, fc.REASON_LOW_CONF)  # a's P < 0.5
        self.assertIsNone(r.room)

    def test_ema_smooths(self):
        sm = fc.RoomSmoother(tau=6.0)
        sm.update({"a": 1.0}, 0.0)
        r = sm.update({"b": 1.0}, 1.0)  # one cycle of contrary evidence
        self.assertEqual(r.incumbent, "a")
        self.assertGreater(sm.ema["a"], 0.8)

    def test_no_data_and_reset(self):
        sm = fc.RoomSmoother(tau=0.001, reset_after=30)
        sm.update({"a": 0.9, "b": 0.1}, 0.0)
        r = sm.update(None, 10.0)
        self.assertEqual(r.reason, fc.REASON_NO_DATA)
        self.assertEqual(sm.incumbent, "a")  # kept for a short gap
        sm.update(None, 40.0)
        self.assertIsNone(sm.incumbent)


class TestEvaluate(unittest.TestCase):
    def test_leave_one_out(self):
        spots = make_house(seed=3)
        out = fc.evaluate(spots, scanner_room=SCANNER_ROOM)
        self.assertEqual(out["spots"], len(spots))
        self.assertGreater(out["windows"], 0)
        self.assertGreaterEqual(out["accuracy"], 0.8)
        self.assertIn("baseline_accuracy", out)
        self.assertGreaterEqual(out["confident_share"], 0.5)
        self.assertGreaterEqual(out["confident_accuracy"], out["accuracy"] - 0.05)
        # The attic has no scanner, so the nearest-proxy baseline can never get it right.
        attic = [s for s in out["per_spot"] if s["room"] == "attic"]
        self.assertTrue(all(s["baseline_accuracy"] == 0 for s in attic))
        self.assertEqual(sum(sum(r.values()) for r in out["confusion"].values()), out["windows"])
        self.assertLessEqual(len(out["worst_spots"]), 5)

    def test_nearest_proxy_offsets(self):
        vec = {"aa:00:00:00:00:01": -70.0, "aa:00:00:00:00:03": -68.0}
        self.assertEqual(fc.nearest_proxy_room(vec, SCANNER_ROOM), "living")
        self.assertEqual(fc.nearest_proxy_room(vec, SCANNER_ROOM, {"aa:00:00:00:00:01": 5}), "kitchen")


if __name__ == "__main__":
    unittest.main()
