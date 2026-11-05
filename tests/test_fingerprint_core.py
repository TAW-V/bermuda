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
        vec = fc.live_vector(readings, nowstamp=100.0, window=12.0)
        self.assertEqual(vec, {"s1": -65.0})

    def test_live_vector_short_window(self):
        readings = {"s1": [(100.0, -70), (97.0, -60), (93.0, -50)]}
        self.assertEqual(fc.live_vector(readings, nowstamp=100.0, window=6.0), {"s1": -65.0})
        self.assertEqual(fc.live_vector(readings, nowstamp=100.0, window=2.5), {"s1": -70.0})


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
        matcher = fc.Matcher(self.spots, offset_max=8.0)
        base = matcher.match(live)
        res = matcher.match(shifted)
        self.assertEqual(max(res.posterior, key=res.posterior.get), "bedroom")
        self.assertAlmostEqual(res.best.offset - base.best.offset, -6, delta=1.5)

    def test_offset_is_clamped(self):
        live = synth_live(random.Random(5), "living", noise=0.5)
        for cap in (2.0, 8.0):
            res = fc.Matcher(self.spots, offset_max=cap).match({s: v + 30 for s, v in live.items()})
            if res is not None:
                self.assertLessEqual(abs(res.best.offset), cap)

    def test_offset_off_by_default_and_needs_agreement(self):
        live = synth_live(random.Random(5), "living", noise=0.5)
        shifted = {s: v - 6 for s, v in live.items()}
        self.assertEqual(fc.OFFSET_MAX_DB, 0.0)
        self.assertEqual(self.matcher.match(shifted).best.offset, 0.0)
        # Too few strong common scanners to agree: no offset.
        many = fc.Matcher(self.spots, offset_max=8.0, offset_min_scanners=50)
        self.assertEqual(many.match(shifted).best.offset, 0.0)

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
        m = fc.Matcher(self.spots, use_offset=False, offset_max=8.0)
        self.assertGreaterEqual(self.accuracy(m), 0.85)
        res = m.match(synth_live(random.Random(1), "kitchen"))
        self.assertEqual(res.best.offset, 0.0)

    def test_temperature_sharpens(self):
        live = synth_live(random.Random(2), "living")
        hot = fc.Matcher(self.spots, temperature=4.0).match(live)
        cold = fc.Matcher(self.spots, temperature=0.25).match(live)
        self.assertGreater(max(cold.posterior.values()), max(hot.posterior.values()))


    def test_bayes_method_classifies(self):
        self.assertGreaterEqual(self.accuracy(fc.Matcher(self.spots, method=fc.METHOD_BAYES)), 0.85)

    def test_invalid_options(self):
        with self.assertRaises(ValueError):
            fc.Matcher(self.spots, method="nope")
        with self.assertRaises(ValueError):
            fc.Matcher(self.spots, room_agg="nope")

    def test_outlier(self):
        # Spots only in kitchen and living; a live vector from the bathroom is far from all of them.
        spots = [s for s in self.spots if s["room"] in ("kitchen", "living")]
        matcher = fc.Matcher(spots, outlier_db=7.0)  # the synthetic house is small; real default is OUTLIER_DB
        rng = random.Random(4)
        inside = [matcher.match(synth_live(rng, "kitchen")) for _ in range(20)]
        self.assertFalse(any(r.outlier for r in inside))
        far = [matcher.match(synth_live(rng, "bathroom")) for _ in range(20)]
        self.assertTrue(all(r.outlier for r in far))
        self.assertTrue(all(r.distance > 7.0 for r in far))
        off = fc.Matcher(spots, outlier_db=0).match(synth_live(rng, "bathroom"))
        self.assertFalse(off.outlier)

    def test_unknown_strong_scanner_raises_distance_only(self):
        live = synth_live(random.Random(11), "living")
        base = self.matcher.match(live)
        res = self.matcher.match({**live, "ff:ff:ff:ff:ff:01": -45.0})
        self.assertAlmostEqual(res.best.score, base.best.score)
        self.assertGreater(res.distance, base.distance + 5)
        self.assertTrue(res.outlier)


def _spot(spot_id, room, means, std=4.0, seen=1.0):
    return {
        "id": spot_id,
        "room": room,
        "scanners": {s: {"mean": m, "std": std, "n": 15, "seen": seen if m > -93 else 0.5} for s, m in means.items()},
    }


class TestAdjacentRooms(unittest.TestCase):
    """
    Two adjacent rooms sharing their strongest scanners (made-up numbers modelled on a
    bedroom and its walk-in closet). The closet spots list many more weak far scanners.
    A bedroom window that hears fewer scanners must not drift to the closet (the fp3 bias:
    each spot was averaged over its own scanners, diluting the misfit of scanner-rich spots).
    """

    S = [f"bb:00:00:00:00:{i:02x}" for i in range(1, 13)]

    def spots(self):
        s = self.S
        bed = {s[0]: -79, s[1]: -74, s[2]: -79, s[3]: -87, s[4]: -95, s[5]: -97}
        closet_far = {s[i]: -96 for i in range(6, 12)}
        out = []
        for i, d in enumerate((0, 3, -3, 2)):
            out.append(_spot(f"bed{i}", "bed", {k: v + (d if j < 3 else 0) for j, (k, v) in enumerate(bed.items())}))
        closet = {s[0]: -69, s[1]: -73, s[2]: -80, s[3]: -86, s[4]: -91, s[5]: -94, **closet_far}
        out.append(_spot("closet0", "closet", closet))
        out.append(_spot("closet1", "closet", {**closet, s[0]: -67, s[1]: -76}))
        return out

    def bed_windows(self):
        s = self.S
        rng = random.Random(8)
        for _ in range(30):
            vec = {s[0]: -79, s[1]: -74, s[2]: -79, s[3]: -87, s[4]: -95}
            vec = {k: v + rng.gauss(0, 3) for k, v in vec.items()}
            if rng.random() < 0.5:
                vec[s[5]] = -97 + rng.gauss(0, 1)
            yield vec

    def test_bed_not_pulled_to_closet(self):
        for method in fc.METHODS:
            matcher = fc.Matcher(self.spots(), method=method)
            wrong = 0
            for vec in self.bed_windows():
                res = matcher.match(vec)
                wrong += max(res.posterior, key=res.posterior.get) != "bed"
            self.assertLessEqual(wrong, 3, method)

    def test_same_scanner_set_for_every_spot(self):
        matcher = fc.Matcher(self.spots())
        res = matcher.match(next(self.bed_windows()))
        self.assertEqual(len({sc.used for sc in res.room_best.values()}), 1)

    def test_closet_still_found(self):
        s = self.S
        live = {s[0]: -68, s[1]: -74, s[2]: -80, s[3]: -86, s[4]: -91, s[5]: -94, s[6]: -96, s[8]: -97}
        res = fc.Matcher(self.spots()).match(live)
        self.assertEqual(max(res.posterior, key=res.posterior.get), "closet")

    def test_room_count_does_not_win(self):
        # Many near-identical bed spots must not outvote one equally good closet spot.
        s = self.S
        base = {s[0]: -75, s[1]: -75, s[2]: -80, s[3]: -88}
        spots = [_spot(f"bed{i}", "bed", {**base, s[0]: -75 + i}) for i in range(8)]
        spots.append(_spot("closet", "closet", {**base, s[0]: -72}))
        res = fc.Matcher(spots).match({**base, s[0]: -72.0})
        self.assertEqual(max(res.posterior, key=res.posterior.get), "closet")


class TestSmoother(unittest.TestCase):
    def test_first_result_and_hysteresis(self):
        # tau ~0: EMA follows input, isolates the switch logic
        sm = fc.RoomSmoother(tau=0.001, confirm_cycles=3)
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

    def test_outlier_reason(self):
        sm = fc.RoomSmoother(tau=0.001)
        sm.update({"a": 0.9, "b": 0.1}, 0.0)
        r = sm.update(None, 2.0, reason=fc.REASON_OUTLIER)
        self.assertEqual((r.room, r.reason), (None, fc.REASON_OUTLIER))
        self.assertEqual(sm.incumbent, "a")

    def test_no_data_and_reset(self):
        sm = fc.RoomSmoother(tau=0.001, reset_after=30)
        sm.update({"a": 0.9, "b": 0.1}, 0.0)
        r = sm.update(None, 10.0)
        self.assertEqual(r.reason, fc.REASON_NO_DATA)
        self.assertEqual(sm.incumbent, "a")  # kept for a short gap
        sm.update(None, 40.0)
        self.assertIsNone(sm.incumbent)


    def test_confirm_cycles_one_switches_on_first_lead(self):
        sm = fc.RoomSmoother(tau=0.001, confirm_cycles=1, min_dwell=0)
        sm.update({"a": 0.7, "b": 0.3}, 0.0)
        r = sm.update({"a": 0.4, "b": 0.6}, 1.0)
        self.assertEqual(r.room, "b")

    def test_defaults_follow_balanced_preset(self):
        sm = fc.RoomSmoother()
        p = fc.FP_PRESETS[fc.DEFAULT_PRESET]
        self.assertEqual(fc.DEFAULT_PRESET, fc.PRESET_BALANCED)
        self.assertEqual((sm.tau, sm.confirm_cycles, sm.min_dwell), (p["tau"], p["confirm_cycles"], p["min_dwell"]))
        self.assertEqual(fc.LIVE_WINDOW_S, p["live_window"])

    def test_smaller_tau_switches_sooner(self):
        def switch_time(**kw):
            sm = fc.RoomSmoother(margin=0.15, **kw)
            t = 0.0
            for _ in range(20):
                sm.update({"a": 0.95, "b": 0.05}, t)
                t += 1.05
            t0 = t
            while t < t0 + 60:
                if sm.update({"a": 0.2, "b": 0.8}, t).room == "b":
                    return t - t0
                t += 1.05
            return None

        fast = switch_time(**fc.smoother_kwargs(fc.FP_PRESETS[fc.PRESET_FAST]))
        balanced = switch_time(**fc.smoother_kwargs(fc.FP_PRESETS[fc.PRESET_BALANCED]))
        stable = switch_time(**fc.smoother_kwargs(fc.FP_PRESETS[fc.PRESET_STABLE]))
        self.assertLess(fast, balanced)
        self.assertLess(balanced, stable)
        self.assertLess(balanced, 8.0)


class TestSwitchingSettings(unittest.TestCase):
    def test_presets_are_ordered(self):
        self.assertEqual(fc.PRESETS[-1], fc.PRESET_CUSTOM)
        f, b, s = (fc.FP_PRESETS[k] for k in (fc.PRESET_FAST, fc.PRESET_BALANCED, fc.PRESET_STABLE))
        for key in ("live_window", "tau"):
            self.assertLessEqual(f[key], b[key])
            self.assertLessEqual(b[key], s[key])

    def test_resolve_preset_ignores_custom_values(self):
        out = fc.resolve_switching(fc.PRESET_FAST, {"live_window": 25, "tau": 20})
        self.assertEqual(out, fc.FP_PRESETS[fc.PRESET_FAST])
        out["tau"] = 99  # a copy, the preset table is not changed
        self.assertNotEqual(fc.FP_PRESETS[fc.PRESET_FAST]["tau"], 99)

    def test_resolve_custom_clamps_and_defaults(self):
        out = fc.resolve_switching(
            fc.PRESET_CUSTOM, {"live_window": 1, "tau": "bad", "confirm_cycles": 2.6, "min_dwell": 999}
        )
        base = fc.FP_PRESETS[fc.DEFAULT_PRESET]
        self.assertEqual(out["live_window"], fc.LIVE_WINDOW_RANGE[0])
        self.assertEqual(out["tau"], base["tau"])
        self.assertEqual(out["confirm_cycles"], 3)
        self.assertIsInstance(out["confirm_cycles"], int)
        self.assertEqual(out["min_dwell"], fc.MIN_DWELL_RANGE[1])
        self.assertEqual(fc.resolve_switching(None), base)  # unknown / missing: default values
        self.assertEqual(fc.resolve_switching("nonsense", {}), base)

    def test_smoother_kwargs(self):
        kw = fc.smoother_kwargs({"live_window": 6, "tau": 2, "confirm_cycles": 1, "min_dwell": 3})
        self.assertEqual(kw, {"tau": 2.0, "confirm_cycles": 1, "min_dwell": 3.0})
        sm = fc.RoomSmoother(**kw, min_confidence=0.4)
        self.assertEqual(sm.min_confidence, 0.4)


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
        self.assertGreaterEqual(out["balanced_accuracy"], 0.8)
        self.assertEqual(set(out["room_recall"]), set(ROOMS))
        self.assertLess(out["mean_confidence"], 1.0)
        self.assertIsNotNone(out["log_loss"])
        self.assertLessEqual(out["outlier_share"], 0.05)
        self.assertGreaterEqual(out["effective_accuracy"], out["baseline_accuracy"])
        self.assertIn("baseline_balanced_accuracy", out)

    def test_nearest_proxy_offsets(self):
        vec = {"aa:00:00:00:00:01": -70.0, "aa:00:00:00:00:03": -68.0}
        self.assertEqual(fc.nearest_proxy_room(vec, SCANNER_ROOM), "living")
        self.assertEqual(fc.nearest_proxy_room(vec, SCANNER_ROOM, {"aa:00:00:00:00:01": 5}), "kitchen")


if __name__ == "__main__":
    unittest.main()
