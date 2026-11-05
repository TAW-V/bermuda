#!/usr/bin/env python3
"""
Offline evaluation harness for the fingerprint matcher (TAW-V fork).

Pure Python, no Home Assistant needed. It loads the fingerprint store and the
scanner areas / RSSI offsets from a Home Assistant ``.storage`` directory and
runs leave-one-spot-out evaluations of ``fingerprint_core.Matcher`` variants:

    python3 -I scripts/fingerprint_eval.py --storage /config/.storage
    python3 -I scripts/fingerprint_eval.py --storage /config/.storage --json out.json
    python3 -I scripts/fingerprint_eval.py --storage /config/.storage --only knn

For every variant it prints the window accuracy, the balanced accuracy (mean
per-room recall, so a room with more spots does not dominate), the log-loss and
mean confidence of the posterior, the outlier share, and a smoothed accuracy
(RoomSmoother over the held-out windows, with fallback to nearest-proxy).
``--confusion A B`` adds the A -> B confusion count.

The recordings stay where they are; nothing is written unless --json is given.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
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
