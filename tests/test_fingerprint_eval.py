"""
Unit tests for the switching-latency simulation in scripts/fingerprint_eval.py (TAW-V fork).

No Home Assistant needed:
    python3 -I -m unittest discover -s tests -p "test_fingerprint_*.py"

All addresses and rooms are made up (see test_fingerprint_core.py).
"""

from __future__ import annotations

import importlib.util
import random
import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
import test_fingerprint_core as tc  # noqa: E402  (loads fingerprint_core as "fingerprint_core")

_PATH = _HERE.parent / "scripts" / "fingerprint_eval.py"
_spec = importlib.util.spec_from_file_location("fingerprint_eval", _PATH)
fe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fe)
fc = fe.fc


def two_rooms(seed=5, per_room=3):
    rng = random.Random(seed)
    rooms = ("kitchen", "living")
    return [tc.synth_spot(rng, room, f"{room}-{i}", duration=60) for room in rooms for i in range(per_room)]


class TestSampler(unittest.TestCase):
    def test_rate_and_levels(self):
        spot = two_rooms()[0]
        sampler = fe.SpotSampler(spot, random.Random(1), min_interval=3.1)
        readings = sampler.readings(0.0, 300.0)
        self.assertTrue(readings)
        for scanner, items in readings.items():
            stamps = [t for t, _ in items]
            self.assertTrue(all(0.0 <= t < 300.0 for t in stamps))
            gaps = [b - a for a, b in zip(stamps, stamps[1:], strict=False)]
            self.assertTrue(all(g >= 3.1 - 1e-9 for g in gaps))
            # roughly the recorded rate (n readings in duration_s)
            st = spot["scanners"][scanner]
            expected = 300.0 / max(3.1, spot["duration_s"] / st["n"])
            self.assertGreater(len(items), 0.5 * expected)
            self.assertLess(len(items), 1.5 * expected + 2)
            mean = sum(r for _, r in items) / len(items)
            self.assertLess(abs(mean - st["mean"]), 4.0)

    def test_deterministic_per_seed(self):
        spot = two_rooms()[0]
        a = fe.SpotSampler(spot, random.Random(7)).readings(0.0, 60.0)
        b = fe.SpotSampler(spot, random.Random(7)).readings(0.0, 60.0)
        self.assertEqual(a, b)


class TestLatency(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.spots = two_rooms()
        scen = fe.build_scenarios(cls.spots, None, seeds=1, steady_s=120.0)
        cls.scen = scen
        cls.pre = {w: fe.precompute(scen, cls.spots, tc.SCANNER_ROOM, {}, w) for w in (6.0, 12.0)}

    def test_scenarios(self):
        # 3 x 3 spot pairs in each direction, one steady run per spot
        self.assertEqual(len(self.scen["transitions"]), 18)
        self.assertEqual(len(self.scen["steady"]), 6)
        a, b, readings, _phase = self.scen["transitions"][0]
        self.assertNotEqual(a["room"], b["room"])
        stamps = [t for items in readings.values() for t, _ in items]
        self.assertLess(min(stamps), 0.0)
        self.assertGreater(max(stamps), 0.0)

    def test_fast_switches_sooner_than_stable(self):
        fast = fe.latency_flicker(self.pre[6.0], fc.smoother_kwargs(fc.FP_PRESETS[fc.PRESET_FAST]))
        stable = fe.latency_flicker(self.pre[12.0], fc.smoother_kwargs(fc.FP_PRESETS[fc.PRESET_STABLE]))
        for r in (fast, stable):
            self.assertEqual(r["missed"], 0.0)  # well separated rooms: every move is followed
            self.assertGreaterEqual(r["flicker_per_min"], 0.0)
            self.assertLess(r["wrong"], 0.1)
            self.assertLessEqual(r["switch_median"], r["switch_p90"])
            self.assertLessEqual(r["switch_median"], r["settled_median"] + 1e-9)
            self.assertEqual(set(r["by_pair"]), {"kitchen->living", "living->kitchen"})
            self.assertEqual(set(r["by_room"]), {"kitchen", "living"})
        self.assertLess(fast["switch_median"], stable["switch_median"])
        self.assertLess(fast["switch_median"], 8.0)

    def test_grid_has_presets(self):
        names = [name for name, _w, _kw in fe.latency_grid()]
        for preset in fc.FP_PRESETS:
            self.assertIn(f"preset {preset}", names)


if __name__ == "__main__":
    unittest.main()
