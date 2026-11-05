#!/usr/bin/env python3
"""
Offline evaluation harness for the fingerprint matcher (TAW-V fork).

Pure Python, no Home Assistant needed. It loads the fingerprint store and the
scanner areas / RSSI offsets from a Home Assistant ``.storage`` directory and
runs leave-one-spot-out evaluations of ``fingerprint_core.Matcher`` variants:

    python3 -I scripts/fingerprint_eval.py --storage /config/.storage
    python3 -I scripts/fingerprint_eval.py --storage /config/.storage --json out.json
    python3 -I scripts/fingerprint_eval.py --storage /config/.storage --only knn
    python3 -I scripts/fingerprint_eval.py --storage /config/.storage --latency --rooms bedroom closet

For every variant it prints the window accuracy, the balanced accuracy (mean
per-room recall, so a room with more spots does not dominate), the log-loss and
mean confidence of the posterior, the outlier share, and a smoothed accuracy
(RoomSmoother over the held-out windows, with fallback to nearest-proxy).
``--confusion A B`` adds the A -> B confusion count.

``--latency`` replays room changes instead: the recorded raw readings of spot A then spot B of
another room (from ``.storage/bermuda_fingerprints_raw/<spot_id>.json``, fp6+, looped from a
random phase when the run is longer than the recording), or, for a spot without a raw file or
with ``--no-raw``, synthetic readings regenerated from the stored summaries at the measured
Shelly rate (one reading per scanner every >= 3.1 s), fed through live_vector + Matcher + RoomSmoother every 1.05 s. It reports the
time until the shown room switches (median / p90), until it stays switched, and the flicker
(room changes per minute, share of time in a wrong room) while standing still at one spot, for
the presets and a grid of live window / tau / confirm cycles / dwell. ``--in-sample`` keeps the
replayed spots in the matcher, ``--detail`` splits per direction and room.

The recordings stay where they are; nothing is written unless --json is given.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import random
import sys
from pathlib import Path

_CORE = Path(__file__).resolve().parents[1] / "custom_components" / "bermuda" / "fingerprint_core.py"
_spec = importlib.util.spec_from_file_location("fingerprint_core", _CORE)
fc = importlib.util.module_from_spec(_spec)
sys.modules["fingerprint_core"] = fc
_spec.loader.exec_module(fc)


def load(storage: Path) -> tuple[list[dict], dict[str, str], dict[str, float]]:
    """Spots, scanner -> room key, and Bermuda's RSSI offsets."""
    store = json.loads((storage / "bermuda.fingerprints").read_text())["data"]
    spots = store["spots"]
    rooms = store.get("rooms", {})
    area_to_room = {v.get("area_id"): k for k, v in rooms.items() if v.get("area_id")}
    scanner_room: dict[str, str] = {}
    known = {s for spot in spots for s in spot.get("scanners", {})}
    dev_file = storage / "core.device_registry"
    if dev_file.exists():
        for dev in json.loads(dev_file.read_text())["data"]["devices"]:
            for kind, value in dev.get("connections", []):
                mac = str(value).lower()
                if kind in ("mac", "bluetooth") and mac in known and dev.get("area_id"):
                    scanner_room[mac] = area_to_room.get(dev["area_id"], dev["area_id"])
    offsets: dict[str, float] = {}
    ce_file = storage / "core.config_entries"
    if ce_file.exists():
        for entry in json.loads(ce_file.read_text())["data"]["entries"]:
            if entry.get("domain") == "bermuda":
                offsets = dict(entry.get("options", {}).get("rssi_offsets", {}))
    return spots, scanner_room, offsets


RAW_DIR_NAME = "bermuda_fingerprints_raw"


def load_raw(storage: Path, spots: list[dict]) -> dict[str, dict[str, list[tuple[float, float]]]]:
    """spot id -> per scanner (t, rssi) raw readings, for the spots that have a readable raw file."""
    out = {}
    raw_dir = storage / RAW_DIR_NAME
    for spot in spots:
        name = (spot.get("raw") or {}).get("file") or fc.raw_file_name(spot["id"])
        path = raw_dir / Path(name).name
        if not path.exists():
            continue
        try:
            readings = fc.raw_readings(fc.read_raw_file(path))
        except (OSError, ValueError) as err:
            print(f"skipping raw file {path}: {err}", file=sys.stderr)
            continue
        if readings:
            out[spot["id"]] = readings
    return out


def smoothed_accuracy(
    spots: list[dict],
    scanner_room: dict[str, str],
    offsets: dict[str, float],
    window_s: float = 10.0,
    cycle_s: float = 1.05,
    smoother_kwargs: dict | None = None,
    **matcher_kwargs,
) -> tuple[float, float]:
    """
    Leave-one-spot-out through the RoomSmoother, as the integration runs it.

    Each held-out window is held for `window_s` seconds and fed every `cycle_s`; the
    smoother starts cold per spot. A cycle counts as correct if the shown room
    (fingerprint room, or nearest-proxy on fallback) is the true room. Returns
    (accuracy, balanced accuracy).
    """
    per_room: dict[str, list[int]] = {}
    for i, spot in enumerate(spots):
        matcher = fc.Matcher(spots[:i] + spots[i + 1 :], **matcher_kwargs)
        sm = fc.RoomSmoother(**(smoother_kwargs or {}))
        t = 0.0
        for vec in fc.spot_windows(spot):
            res = matcher.match(vec)
            base = fc.nearest_proxy_room(vec, scanner_room, offsets)
            end = t + window_s
            while t < end:
                if res is None:
                    out = sm.update(None, t)
                elif res.outlier:
                    out = sm.update(None, t, reason=fc.REASON_OUTLIER)
                else:
                    out = sm.update(res.posterior, t)
                shown = out.room if out.room is not None else base
                stats = per_room.setdefault(spot["room"], [0, 0])
                stats[0] += shown == spot["room"]
                stats[1] += 1
                t += cycle_s
    ok = sum(v[0] for v in per_room.values())
    n = sum(v[1] for v in per_room.values())
    bal = sum(v[0] / v[1] for v in per_room.values()) / len(per_room)
    return ok / n, bal


def _perturb(vec: dict[str, float], kind: str, rng: random.Random) -> dict[str, float]:
    """Live-like perturbations of a held-out 10 s window."""
    if kind == "weak<-92 dropped":  # short live window: far scanners not heard
        return {s: x for s, x in vec.items() if x >= -92} or vec
    if kind == "50% weak dropped":
        return {s: x for s, x in vec.items() if x >= -90 or rng.random() < 0.5} or vec
    if kind == "only top 4":
        return dict(sorted(vec.items(), key=lambda kv: -kv[1])[:4])
    if kind.startswith("shift"):
        delta = float(kind.split()[1])
        return {s: x + delta for s, x in vec.items()}
    if kind == "+2 weak heard":  # more far scanners than usual
        out = dict(vec)
        extra = [s for s in rng.sample(sorted(_ALL_SCANNERS), len(_ALL_SCANNERS)) if s not in out][:2]
        for s in extra:
            out[s] = -97.0
        return out
    return vec


PERTURBATIONS = ["weak<-92 dropped", "50% weak dropped", "only top 4", "shift -5", "shift +5", "+2 weak heard"]
_ALL_SCANNERS: set[str] = set()


def stress(spots: list[dict], pair: tuple[str, str], **matcher_kwargs) -> dict[str, tuple[int, int, int, int]]:
    """Per perturbation: (A->B errors, A windows, B->A errors, B windows), leave-one-spot-out."""
    out = {}
    for kind in PERTURBATIONS:
        rng = random.Random(1)
        counts = [0, 0, 0, 0]
        for i, spot in enumerate(spots):
            if spot["room"] not in pair:
                continue
            matcher = fc.Matcher(spots[:i] + spots[i + 1 :], **matcher_kwargs)
            j = 0 if spot["room"] == pair[0] else 2
            other = pair[1] if j == 0 else pair[0]
            for vec in fc.spot_windows(spot):
                res = matcher.match(_perturb(vec, kind, rng))
                pred = max(res.posterior, key=res.posterior.get) if res else None
                counts[j] += pred == other
                counts[j + 1] += 1
        out[kind] = tuple(counts)
    return out


# ---------------------------------------------------------------------------
# Switching latency / flicker simulation
# ---------------------------------------------------------------------------
MIN_INTERVAL_S = 3.1  # measured: a Shelly forwards at most one reading per device every ~3.1 s
CYCLE_S = 1.05  # Bermuda's update cycle


class SpotSampler:
    """
    Synthetic raw readings for one recorded spot.

    The stored spot keeps per scanner the mean/std/count and the 10 s window medians, not the raw
    samples. Readings are regenerated per scanner at the recorded rate (gaps of at least
    `min_interval`, plus an exponential extra so the mean matches duration / n). The level follows
    the window medians (windows replayed in a random order, so the slow drift is kept) plus
    per-sample noise with the remaining spread. A scanner absent from a window stays silent then.
    """

    def __init__(self, spot: dict, rng: random.Random, min_interval: float = MIN_INTERVAL_S) -> None:
        self.rng = rng
        self.window = fc.WINDOW_S
        order = spot.get("window_scanners", [])
        rows = spot.get("windows", [])
        self.rows = [{s: v for s, v in zip(order, row, strict=False) if v is not None} for row in rows]
        duration = float(spot.get("duration_s", 60))
        self.scanners: dict[str, tuple[float, float]] = {}  # scanner -> (mean extra gap, noise sd)
        for s, st in spot.get("scanners", {}).items():
            meds = [r[s] for r in self.rows if s in r]
            if not meds:
                continue
            mean_m = sum(meds) / len(meds)
            var_w = sum((m - mean_m) ** 2 for m in meds) / len(meds)
            sd = max(1.0, (max(0.0, float(st.get("std", 3.0)) ** 2 - var_w)) ** 0.5)
            gap = max(min_interval, duration / max(1, int(st.get("n", 1))))
            self.scanners[s] = (gap - min_interval, sd)
        self.min_interval = min_interval

    def readings(self, start: float, end: float) -> dict[str, list[tuple[float, float]]]:
        """Readings with stamps in [start, end)."""
        rng = self.rng
        n_win = max(1, math.ceil((end - start) / self.window))
        seq: list[dict[str, float]] = []
        while len(seq) < n_win:
            block = list(self.rows)
            rng.shuffle(block)
            seq.extend(block)
        out: dict[str, list[tuple[float, float]]] = {}
        for s, (extra, sd) in self.scanners.items():
            t = start + rng.random() * (self.min_interval + extra)
            while t < end:
                level = seq[int((t - start) // self.window)].get(s)
                if level is not None:
                    out.setdefault(s, []).append((t, float(round(level + rng.gauss(0.0, sd)))))
                t += self.min_interval + (rng.expovariate(1.0 / extra) if extra > 0 else 0.0)
        return out


class RawSampler:
    """
    The recorded raw readings of one spot, replayed on the simulation clock.

    Same interface as SpotSampler. The recording (length `duration`) is looped from a random phase,
    so runs longer than the recording and different seeds see different stretches of it.
    """

    def __init__(self, readings: dict[str, list[tuple[float, float]]], duration: float, rng: random.Random) -> None:
        self.items = sorted((t, s, r) for s, v in readings.items() for t, r in v)
        last = self.items[-1][0] if self.items else 0.0
        self.duration = max(float(duration), last + 1e-3, 1.0)
        self.phase = rng.random() * self.duration

    def readings(self, start: float, end: float) -> dict[str, list[tuple[float, float]]]:
        """Readings with stamps in [start, end)."""
        out: dict[str, list[tuple[float, float]]] = {}
        base = start - self.phase
        while base < end:
            for t, s, r in self.items:
                stamp = base + t
                if start <= stamp < end:
                    out.setdefault(s, []).append((stamp, r))
            base += self.duration
        for v in out.values():
            v.sort()
        return out


def make_sampler(spot: dict, rng: random.Random, min_interval: float, raw: dict | None):
    """RawSampler if the spot has raw readings in `raw`, else the synthetic SpotSampler."""
    if raw and spot["id"] in raw:
        return RawSampler(raw[spot["id"]], float(spot.get("duration_s", 60)), rng)
    return SpotSampler(spot, rng, min_interval)


def _merge(*parts: dict[str, list[tuple[float, float]]]) -> dict[str, list[tuple[float, float]]]:
    out: dict[str, list[tuple[float, float]]] = {}
    for part in parts:
        for s, items in part.items():
            out.setdefault(s, []).extend(items)
    for items in out.values():
        items.sort()
    return out


def _posteriors(matcher, readings, scanner_room, offsets, times, window):
    """Per cycle: (posterior or None, reason, nearest-proxy room)."""
    out = []
    for t in times:
        live = fc.live_vector({s: [x for x in v if x[0] <= t] for s, v in readings.items()}, t, window)
        res = matcher.match(live) if live else None
        base = fc.nearest_proxy_room(live, scanner_room, offsets)
        if res is None:
            out.append((None, fc.REASON_NO_DATA, base))
        elif res.outlier:
            out.append((None, fc.REASON_OUTLIER, base))
        else:
            out.append((res.posterior, fc.REASON_MATCH, base))
    return out


def _shown(seq, times, smoother_kwargs):
    sm = fc.RoomSmoother(**smoother_kwargs)
    shown = []
    for (post, reason, base), t in zip(seq, times, strict=True):
        out = sm.update(post, t, reason=reason)
        shown.append(out.room if out.room is not None else base)
    return shown


def build_scenarios(
    spots: list[dict],
    rooms: tuple[str, str] | None,
    *,
    seeds: int = 4,
    pre_s: float = 40.0,
    post_s: float = 60.0,
    steady_s: float = 300.0,
    min_interval: float = MIN_INTERVAL_S,
    raw: dict | None = None,
) -> dict:
    """
    Raw reading scenarios (independent of the settings, so every setting sees the same data).

    - transitions: for every ordered pair of spots in different rooms (restricted to `rooms` if
      given), `pre_s` seconds at spot A then `post_s` at spot B, the switch at t = 0.
    - steady: every spot (in `rooms`) for `steady_s` seconds.
    The matcher for a scenario is trained on all spots except the replayed ones (leave-out).
    Spots with raw readings in `raw` (spot id -> per scanner (t, rssi), see load_raw) replay them;
    the others use synthetic readings from the summaries.
    """
    sel = [s for s in spots if rooms is None or s["room"] in rooms]
    trans, steady = [], []
    for seed in range(seeds):
        for a in sel:
            for b in sel:
                if a["room"] == b["room"]:
                    continue
                rng = random.Random(f"{seed}/{a['id']}/{b['id']}")
                ra = make_sampler(a, rng, min_interval, raw).readings(-pre_s - 30.0, 0.0)
                rb = make_sampler(b, rng, min_interval, raw).readings(0.0, post_s)
                trans.append((a, b, _merge(ra, rb), rng.random() * CYCLE_S))
        for a in sel:
            rng = random.Random(f"{seed}/{a['id']}/steady")
            sampler = make_sampler(a, rng, min_interval, raw)
            steady.append((a, sampler.readings(-30.0, steady_s), rng.random() * CYCLE_S))
    n_raw = sum(1 for a in sel if raw and a["id"] in raw)
    return {
        "transitions": trans,
        "steady": steady,
        "pre_s": pre_s,
        "post_s": post_s,
        "steady_s": steady_s,
        "raw_spots": n_raw,
        "synthetic_spots": len(sel) - n_raw,
    }


def precompute(
    scenarios: dict, spots: list[dict], scanner_room, offsets, window: float, leave_out: bool = True, **matcher_kwargs
) -> dict:
    """
    Posterior sequences for one live window (the expensive part); reused for all smoother settings.

    With `leave_out` (default) the replayed spots are not in the matcher (pessimistic: a position
    between recorded spots); without it all spots are (optimistic: the phone at a recorded spot).
    """
    cache: dict[frozenset, object] = {}

    def matcher_without(*ids):
        key = frozenset(ids) if leave_out else frozenset()
        if key not in cache:
            cache[key] = fc.Matcher([s for s in spots if s["id"] not in key], **matcher_kwargs)
        return cache[key]

    pre, post, steady_s = scenarios["pre_s"], scenarios["post_s"], scenarios["steady_s"]
    trans = []
    for a, b, readings, phase in scenarios["transitions"]:
        times = [t * CYCLE_S + phase - pre for t in range(int((pre + post) / CYCLE_S))]
        matcher = matcher_without(a["id"], b["id"])
        trans.append((a, b, times, _posteriors(matcher, readings, scanner_room, offsets, times, window)))
    steady = []
    for a, readings, phase in scenarios["steady"]:
        times = [t * CYCLE_S + phase for t in range(int(steady_s / CYCLE_S))]
        matcher = matcher_without(a["id"])
        steady.append((a, times, _posteriors(matcher, readings, scanner_room, offsets, times, window)))
    return {"transitions": trans, "steady": steady}


def _switch_stats(runs, smoother_kwargs) -> dict:
    first, settled, missed = [], [], 0
    for _a, b, times, seq in runs:
        shown = _shown(seq, times, smoother_kwargs)
        after = [(t, r) for t, r in zip(times, shown, strict=True) if t >= 0]
        hit = next((t for t, r in after if r == b["room"]), None)
        if hit is None:
            missed += 1
            continue
        first.append(hit)
        last_wrong = max((t for t, r in after if r != b["room"]), default=None)
        settled.append(0.0 if last_wrong is None else last_wrong + CYCLE_S)
    n = len(runs)
    return {
        "switch_median": fc.median(first) if first else None,
        "switch_p90": fc._percentile(first, 0.9),
        "settled_median": fc.median(settled) if settled else None,
        "settled_p90": fc._percentile(settled, 0.9),
        "missed": round(missed / n, 3) if n else None,
    }


def _steady_stats(runs, smoother_kwargs, warmup_s) -> dict:
    changes = minutes = wrong = cycles = 0.0
    for a, times, seq in runs:
        shown = _shown(seq, times, smoother_kwargs)
        kept = [r for t, r in zip(times, shown, strict=True) if t >= warmup_s]
        changes += sum(1 for x, y in zip(kept, kept[1:], strict=False) if x != y)
        minutes += len(kept) * CYCLE_S / 60.0
        wrong += sum(1 for r in kept if r != a["room"])
        cycles += len(kept)
    return {
        "flicker_per_min": round(changes / minutes, 3) if minutes else None,
        "wrong": round(wrong / cycles, 3) if cycles else None,
    }


def latency_flicker(pre: dict, smoother_kwargs: dict, warmup_s: float = 20.0) -> dict:
    """
    Switching time and flicker for one smoother setting.

    - switch: seconds from the move until the shown room first equals B (median, p90), and
      `settled`: until it stays B for the rest of the run; `missed` = share that never showed B.
    - flicker: changes of the shown room per minute while the phone stays at one spot (after
      `warmup_s`), and the share of time a wrong room is shown (`wrong`).
    `by_pair` / `by_room` give the same per direction and per room.
    """
    out = {
        **_switch_stats(pre["transitions"], smoother_kwargs),
        **_steady_stats(pre["steady"], smoother_kwargs, warmup_s),
    }
    pairs: dict[str, list] = {}
    for run in pre["transitions"]:
        pairs.setdefault(f"{run[0]['room']}->{run[1]['room']}", []).append(run)
    rooms: dict[str, list] = {}
    for run in pre["steady"]:
        rooms.setdefault(run[0]["room"], []).append(run)
    out["by_pair"] = {k: _switch_stats(v, smoother_kwargs) for k, v in sorted(pairs.items())}
    out["by_room"] = {k: _steady_stats(v, smoother_kwargs, warmup_s) for k, v in sorted(rooms.items())}
    return out


def latency_grid() -> list[tuple[str, float, dict]]:
    """(name, live window, smoother kwargs) for --latency."""
    out = []
    for name, p in fc.FP_PRESETS.items():
        out.append((f"preset {name}", p["live_window"], fc.smoother_kwargs(p)))
    out.append(("fp4 (12 s, tau 6, 3 cyc, dwell 5)", 12.0, {"tau": 6.0, "confirm_cycles": 3, "min_dwell": 5.0}))
    for window in (6.0, 8.0, 10.0, 12.0):
        for tau in (1.0, 2.0, 3.0, 4.0, 6.0):
            for confirm in (1, 2, 3):
                for dwell in (0.0, 3.0, 5.0, 10.0):
                    out.append(
                        (
                            f"w{window:g} tau{tau:g} c{confirm} d{dwell:g}",
                            window,
                            {"tau": tau, "confirm_cycles": confirm, "min_dwell": dwell},
                        )
                    )
    return out


def run_latency(spots, scanner_room, offsets, rooms, args, matcher_kwargs, raw=None) -> list[dict]:
    scen = build_scenarios(spots, rooms, seeds=args.seeds, min_interval=args.min_interval, raw=raw)
    print(
        f"latency: {len(scen['transitions'])} transitions, {len(scen['steady'])} steady runs "
        f"({scen['steady_s']:.0f} s); {scen['raw_spots']} spots replay recorded raw readings, "
        f"{scen['synthetic_spots']} synthetic (>= {args.min_interval} s apart); "
        + ("in-sample (all spots in the matcher)" if args.in_sample else "replayed spots left out of the matcher")
    )
    print(
        f"{'setting':36} {'sw.med':>6} {'sw.p90':>6} {'set.med':>7} {'set.p90':>7}"
        f" {'miss':>5} {'flick/min':>9} {'wrong':>5}"
    )
    pre_cache: dict[float, dict] = {}
    out = []
    for name, window, skw in latency_grid():
        if args.only and args.only not in name:
            continue
        if window not in pre_cache:
            pre_cache[window] = precompute(
                scen, spots, scanner_room, offsets, window, leave_out=not args.in_sample, **matcher_kwargs
            )
        skw = {"min_confidence": args.min_confidence, **skw}
        r = latency_flicker(pre_cache[window], skw)

        def f(v):
            return f"{v:6.1f}" if v is not None else "   n/a"

        print(
            f"{name:36} {f(r['switch_median'])} {f(r['switch_p90'])}  {f(r['settled_median'])}  {f(r['settled_p90'])}"
            f" {r['missed']:5.2f} {r['flicker_per_min']:9.3f} {r['wrong']:5.3f}"
        )
        if args.detail:
            for k, v in r["by_pair"].items():
                print(
                    f"    {k:30} first {f(v['switch_median'])} p90 {f(v['switch_p90'])}"
                    f"  settled {f(v['settled_median'])} p90 {f(v['settled_p90'])} miss {v['missed']:.2f}"
                )
            for k, v in r["by_room"].items():
                print(f"    steady {k:23} flicker/min {v['flicker_per_min']:.3f} wrong {v['wrong']:.3f}")
        out.append({"name": name, "live_window": window, "smoother": skw, **r})
    return out


def variants() -> list[tuple[str, dict]]:
    """(name, Matcher kwargs). Edit freely; the first one is the fp3 behaviour."""
    out: list[tuple[str, dict]] = [("DEFAULTS (fp4)", {})]
    bayes_fp3 = {
        "method": "bayes",
        "offset_max": 8.0,
        "offset_min_scanners": 1,
        "temperature": 0.5,
        "outlier_db": 0,
        "room_agg": "max",
        "room_k": 1,
    }
    out.append(("bayes fp3 (offset 8, any)", bayes_fp3))
    for cap in (0, 2, 3):
        out.append((f"bayes offset cap {cap}", {**bayes_fp3, "offset_max": cap}))
    out.append(("bayes offset 3, >=4 agree", {**bayes_fp3, "offset_max": 3, "offset_min_scanners": 4}))
    out.append(("bayes offset 0, room mean", {**bayes_fp3, "offset_max": 0, "room_agg": "mean", "room_k": 0}))
    out.append(("bayes offset 0, room mix", {**bayes_fp3, "offset_max": 0, "room_agg": "mix", "room_k": 0}))
    for floor in (3, 6):
        out.append((f"bayes offset 0, sigma floor {floor}", {**bayes_fp3, "offset_max": 0, "sigma_min": floor}))
    for w in (0.5, 0.0):
        out.append((f"bayes offset 0, presence w {w}", {**bayes_fp3, "offset_max": 0, "presence_weight": w}))
    out.append(
        (
            "bayes offset 0, w 0, sigma 6",
            {**bayes_fp3, "offset_max": 0, "presence_weight": 0.0, "sigma_min": 6},
        )
    )
    for t in (1.0, 2.0):
        out.append((f"bayes offset 0, T {t}", {**bayes_fp3, "offset_max": 0, "temperature": t}))
    knn = {"method": "knn", "offset_max": 0, "temperature": 1.0, "outlier_db": 0, "room_agg": "max", "room_k": 1}
    for k in (1, 2, 3):
        out.append((f"knn k={k} (room mean of top-k)", {**knn, "room_k": k, "room_agg": "mean"}))
    out.append(("knn all spots, room mean", {**knn, "room_k": 0, "room_agg": "mean"}))
    out.append(("knn all spots, room mix", {**knn, "room_k": 0, "room_agg": "mix"}))
    out.append(("knn all spots, room mix, T 0.3", {**knn, "room_k": 0, "room_agg": "mix", "temperature": 0.3}))
    for cap in (2, 3, 8):
        out.append((f"knn k=1, offset cap {cap} (>=4)", {**knn, "offset_max": cap, "offset_min_scanners": 4}))
    out.append(("knn k=1, offset cap 8 (any)", {**knn, "offset_max": 8, "offset_min_scanners": 1}))
    for floor in (-95, -105):
        out.append((f"knn k=1, missing {floor}", {**knn, "missing_rssi": floor}))
    for t in (0.5, 2.0, 4.0):
        out.append((f"knn k=1, T {t}", {**knn, "temperature": t}))
    mix = {**knn, "room_k": 0, "room_agg": "mix"}
    for floor in (-90, -92, -95, -105):
        out.append((f"knn room mix, floor {floor}", {**mix, "missing_rssi": floor}))
    for cap in (3, 8):
        out.append((f"knn room mix, offset {cap} (>=4)", {**mix, "offset_max": cap, "offset_min_scanners": 4}))
    out.append(("knn room mix, floor -92, offset 3", {**mix, "missing_rssi": -92, "offset_max": 3}))
    for t in (0.5, 2.0):
        out.append((f"knn room mix, floor -92, T {t}", {**mix, "missing_rssi": -92, "temperature": t}))
    out.append(("knn room mean k=2, floor -92", {**knn, "room_k": 2, "room_agg": "mean", "missing_rssi": -92}))
    bfix = {**bayes_fp3, "offset_max": 0, "room_agg": "mix", "room_k": 0}
    out.append(("bayes room mix, presence w 0.25", {**bfix, "presence_weight": 0.25}))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--storage", type=Path, default=Path("/config/.storage"))
    ap.add_argument("--only", help="only variants whose name contains this text")
    ap.add_argument("--confusion", nargs=2, metavar=("TRUE", "PRED"), help="report TRUE -> PRED counts")
    ap.add_argument("--json", type=Path, help="write all results to this file")
    ap.add_argument("--exclude", action="append", default=[], help="leave out spots (id, or room/label)")
    ap.add_argument("--min-confidence", type=float, default=0.5, help="fallback threshold (effective / smoothed)")
    ap.add_argument("--outlier-db", type=float, help="override outlier_db for all variants")
    ap.add_argument("--stress", action="store_true", help="also run live-like perturbation tests")
    ap.add_argument("--no-smooth", action="store_true", help="skip the (slower) smoothed evaluation")
    ap.add_argument(
        "--latency", action="store_true", help="simulate room switching time and flicker for smoother settings"
    )
    ap.add_argument("--rooms", nargs=2, metavar=("A", "B"), help="--latency: only transitions between these rooms")
    ap.add_argument("--in-sample", action="store_true", help="--latency: keep the replayed spots in the matcher")
    ap.add_argument("--detail", action="store_true", help="--latency: per direction and per room")
    ap.add_argument("--seeds", type=int, default=4, help="--latency: repetitions per scenario")
    ap.add_argument(
        "--min-interval", type=float, default=MIN_INTERVAL_S, help="--latency: minimum seconds between readings"
    )
    ap.add_argument(
        "--no-raw", action="store_true", help="--latency: ignore raw recording files, use synthetic readings"
    )
    args = ap.parse_args()

    spots, scanner_room, offsets = load(args.storage)
    spots = [
        s for s in spots if s.get("id") not in args.exclude and f"{s['room']}/{s.get('label')}" not in args.exclude
    ]
    _ALL_SCANNERS.update(x for spot in spots for x in spot.get("scanners", {}))
    rooms: dict[str, int] = {}
    for s in spots:
        rooms[s["room"]] = rooms.get(s["room"], 0) + 1
    pair = args.confusion
    if pair is None and len(rooms) == 2:
        big, small = sorted(rooms, key=rooms.get, reverse=True)
        pair = (big, small)
    print(f"{len(spots)} spots: {rooms}; {len(scanner_room)} scanners with an area")

    if args.latency:
        lat_rooms = tuple(args.rooms) if args.rooms else None
        raw = {} if args.no_raw else load_raw(args.storage, spots)
        results = run_latency(spots, scanner_room, offsets, lat_rooms, args, {}, raw=raw)
        if args.json:
            args.json.write_text(json.dumps(results, indent=1))
        return 0

    results = []
    header = f"{'variant':38} {'acc':>5} {'bal':>5} {'conf':>5} {'logl':>5} {'outl':>5} {'d95':>5} {'s.acc':>5} {'s.bal':>5} {'e.acc':>5} {'e.bal':>5} {'dmax':>5}"
    if pair:
        header += f"  {pair[0][:6]}->{pair[1][:6]}"
    print(header)
    first = True
    for name, kwargs in variants():
        if args.only and args.only not in name:
            continue
        if args.outlier_db is not None:
            kwargs = {**kwargs, "outlier_db": args.outlier_db}
        res = fc.evaluate(
            spots, scanner_room=scanner_room, rssi_offsets=offsets, min_confidence=args.min_confidence, **kwargs
        )
        if first:
            print(
                f"{'nearest-proxy baseline':38} {res['baseline_accuracy']:5.3f} {res['baseline_balanced_accuracy']:5.3f}"
            )
            first = False
        s_acc = s_bal = float("nan")
        if not args.no_smooth:
            s_acc, s_bal = smoothed_accuracy(
                spots, scanner_room, offsets, smoother_kwargs={"min_confidence": args.min_confidence}, **kwargs
            )
        line = (
            f"{name:38} {res['accuracy']:5.3f} {res['balanced_accuracy']:5.3f} {res['mean_confidence']:5.2f}"
            f" {res['log_loss']:5.2f} {res['outlier_share']:5.2f} {res['distance_p95']:5.1f} {s_acc:5.3f} {s_bal:5.3f}"
            f" {res['effective_accuracy']:5.3f} {res['effective_balanced_accuracy']:5.3f} {res['distance_max']:5.1f}"
        )
        if pair:
            row = res["confusion"].get(pair[0], {})
            line += f"  {row.get(pair[1], 0)}/{sum(row.values())}"
            back = res["confusion"].get(pair[1], {})
            line += f"  rev {back.get(pair[0], 0)}/{sum(back.values())}"
        print(line)
        if args.stress and pair:
            st = stress(spots, pair, **kwargs)
            print("      " + "  ".join(f"{k}: {v[0]}/{v[1]} rev {v[2]}/{v[3]}" for k, v in st.items()))
        results.append({"name": name, "kwargs": kwargs, "result": res, "smoothed": [s_acc, s_bal]})
    if args.json:
        args.json.write_text(json.dumps(results, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
