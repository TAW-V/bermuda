"""
RSSI fingerprinting core for Bermuda (TAW-V fork).

Pure Python, no Home Assistant imports, so it can be unit-tested and reused
outside HA (offline evaluation scripts etc).

Concepts
--------
- A *spot* is one recording at one position: per scanner the mean/std of the raw
  RSSI, and ``seen`` = fraction of 10 s windows in which that scanner heard the
  device. The per-window medians are kept for leave-one-out evaluation.
- A *room* is a key (an HA area_id, or a slug for a label-only room). Each spot
  belongs to one room.
- The live vector is, per scanner, the median raw RSSI over the last ~12 s.
- ``Matcher`` scores every spot with a tempered Gaussian naive-Bayes model with a
  detection term and a per-spot device offset, takes the best spot per room,
  and turns room scores into a posterior via a softmax.
- ``RoomSmoother`` applies an EMA, hysteresis and minimum dwell time, and
  decides when to fall back to Bermuda's own nearest-proxy area.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Tunables (defaults; the Matcher / RoomSmoother take overrides)
# ---------------------------------------------------------------------------
RSSI_INVALID = -127  # Readings at or below this are ignored
WINDOW_S = 10.0  # Recording window length for "seen" and window medians
LIVE_WINDOW_S = 12.0  # Live vector window
SIGMA_MIN = 4.0
SIGMA_MAX = 12.0
SEEN_MIN = 0.05
SEEN_MAX = 0.95
UNKNOWN_MEAN = -100.0  # Model for a scanner never heard at a spot
UNKNOWN_SIGMA = 6.0
OFFSET_MAX_DB = 8.0
OFFSET_PRIOR_DB = 6.0  # Gentle prior keeping the device offset near 0
OFFSET_MIN_SEEN = 0.5
MIN_COMMON = 3  # Minimum common heard scanners for a spot to be scored
TEMPERATURE = 0.5  # softmax temperature on per-scanner scores; 0.5 calibrated on synthetic data

_LOG_SQRT_2PI = 0.5 * math.log(2 * math.pi)


def median(values: list[float]) -> float:
    """Median of a non-empty list."""
    vals = sorted(values)
    n = len(vals)
    mid = n // 2
    if n % 2:
        return float(vals[mid])
    return (vals[mid - 1] + vals[mid]) / 2.0


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def valid_rssi(rssi: float | None) -> bool:
    return rssi is not None and rssi > RSSI_INVALID


def live_vector(
    readings: dict[str, list[tuple[float, float]]], nowstamp: float, window: float = LIVE_WINDOW_S
) -> dict[str, float]:
    """
    Build the live vector: per scanner the median RSSI of readings in the last `window` seconds.

    `readings` maps scanner -> list of (stamp, rssi). Duplicates (same stamp and rssi,
    e.g. the same advert seen via two metadevice sources) are removed.
    """
    out: dict[str, float] = {}
    cutoff = nowstamp - window
    for scanner, items in readings.items():
        vals = [rssi for stamp, rssi in set(items) if stamp >= cutoff and valid_rssi(rssi)]
        if vals:
            out[scanner] = median(vals)
    return out


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------
class SpotRecorder:
    """Accumulates readings for one spot and builds the stored spot dict."""

    def __init__(self, start: float, duration: float, window: float = WINDOW_S) -> None:
        self.start = start
        self.duration = float(duration)
        self.window = window
        self._seen_keys: set[tuple[str, float, float]] = set()
        self.readings: dict[str, list[tuple[float, float]]] = {}
        self.last_reading: float | None = None
        self.max_gap: float = 0.0

    @property
    def end(self) -> float:
        return self.start + self.duration

    @property
    def sample_count(self) -> int:
        return len(self._seen_keys)

    def samples_per_scanner(self) -> dict[str, int]:
        return {s: len(v) for s, v in self.readings.items()}

    def add(self, scanner: str, stamp: float, rssi: float | None) -> bool:
        """Add one reading. Returns True if it was new and accepted."""
        if not valid_rssi(rssi) or stamp < self.start or stamp > self.end:
            return False
        key = (scanner, stamp, rssi)
        if key in self._seen_keys:
            return False
        self._seen_keys.add(key)
        self.readings.setdefault(scanner, []).append((stamp, float(rssi)))
        if self.last_reading is None or stamp > self.last_reading:
            self.last_reading = stamp
        return True

    def finish_gap(self, nowstamp: float) -> None:
        """Track the longest period without any reading (call once per cycle)."""
        ref = self.last_reading if self.last_reading is not None else self.start
        self.max_gap = max(self.max_gap, min(nowstamp, self.end) - ref)

    def build(self) -> dict[str, Any]:
        """Build the spot statistics and windows (no id / room / label metadata)."""
        n_windows = max(1, math.ceil(self.duration / self.window))
        scanners: dict[str, dict[str, float]] = {}
        window_vals: dict[str, dict[int, list[float]]] = {}
        for scanner, items in self.readings.items():
            vals = [r for _, r in items]
            mean = sum(vals) / len(vals)
            var = sum((v - mean) ** 2 for v in vals) / len(vals)
            per_window: dict[int, list[float]] = {}
            for stamp, rssi in items:
                idx = min(n_windows - 1, int((stamp - self.start) // self.window))
                per_window.setdefault(idx, []).append(rssi)
            window_vals[scanner] = per_window
            scanners[scanner] = {
                "mean": round(mean, 1),
                "std": round(math.sqrt(var), 1),
                "n": len(vals),
                "seen": round(len(per_window) / n_windows, 3),
            }
        order = sorted(scanners)
        windows = []
        for idx in range(n_windows):
            row = []
            for scanner in order:
                w = window_vals[scanner].get(idx)
                row.append(round(median(w)) if w else None)
            if any(v is not None for v in row):
                windows.append(row)
        return {
            "duration_s": int(self.duration),
            "samples": self.sample_count,
            "scanners": scanners,
            "window_scanners": order,
            "windows": windows,
        }


def spot_windows(spot: dict[str, Any]) -> list[dict[str, float]]:
    """Expand a stored spot's windows into live-vector style dicts."""
    order = spot.get("window_scanners", [])
    out = []
    for row in spot.get("windows", []):
        vec = {s: float(v) for s, v in zip(order, row, strict=False) if v is not None}
        if vec:
            out.append(vec)
    return out


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class _ScannerModel:
    mean: float
    sigma: float
    log_seen: float
    log_unseen: float
    seen: float

    @classmethod
    def from_stats(cls, stats: dict[str, float]) -> _ScannerModel:
        seen = clamp(float(stats.get("seen", SEEN_MIN)), SEEN_MIN, SEEN_MAX)
        sigma = clamp(float(stats.get("std", SIGMA_MIN)), SIGMA_MIN, SIGMA_MAX)
        return cls(float(stats["mean"]), sigma, math.log(seen), math.log(1 - seen), seen)


_UNKNOWN_MODEL = _ScannerModel(UNKNOWN_MEAN, UNKNOWN_SIGMA, math.log(SEEN_MIN), math.log(1 - SEEN_MIN), SEEN_MIN)


@dataclass(slots=True)
class SpotScore:
    spot_id: str
    room: str
    score: float  # tempered log-likelihood (per scanner)
    used: int  # scanners in the likelihood
    common: int  # heard live and known at the spot
    offset: float  # estimated device offset in dB


@dataclass(slots=True)
class MatchResult:
    posterior: dict[str, float]  # room -> P
    best: SpotScore
    second: SpotScore | None  # best spot of the runner-up room
    room_best: dict[str, SpotScore] = field(default_factory=dict)


@dataclass(slots=True)
class _SpotModel:
    spot_id: str
    room: str
    scanners: dict[str, _ScannerModel]


class Matcher:
    """Scores a live vector against stored spots."""

    def __init__(
        self,
        spots: list[dict[str, Any]],
        *,
        use_offset: bool = True,
        temperature: float = TEMPERATURE,
        min_common: int = MIN_COMMON,
    ) -> None:
        self.use_offset = use_offset
        self.temperature = temperature
        self.min_common = min_common
        self.spots: list[_SpotModel] = []
        self.known_scanners: set[str] = set()
        for spot in spots:
            models = {s: _ScannerModel.from_stats(st) for s, st in spot.get("scanners", {}).items()}
            if not models or not spot.get("room"):
                continue
            self.spots.append(_SpotModel(str(spot.get("id")), str(spot["room"]), models))
            self.known_scanners.update(models)

    def __len__(self) -> int:
        return len(self.spots)

    @property
    def rooms(self) -> set[str]:
        return {s.room for s in self.spots}

    def score_spot(self, spot: _SpotModel, heard: dict[str, float], active: set[str] | None) -> SpotScore | None:
        common = [s for s in heard if s in spot.scanners]
        if len(common) < self.min_common:
            return None

        offset = 0.0
        if self.use_offset:
            diffs = [heard[s] - spot.scanners[s].mean for s in common if spot.scanners[s].seen > OFFSET_MIN_SEEN]
            if diffs:
                offset = clamp(median(diffs), -OFFSET_MAX_DB, OFFSET_MAX_DB)

        scanners = set(spot.scanners) | set(heard)
        if active is not None:
            scanners &= active
        loglik = 0.0
        used = 0
        for s in scanners:
            model = spot.scanners.get(s, _UNKNOWN_MODEL)
            x = heard.get(s)
            if x is None:
                loglik += model.log_unseen
            else:
                z = (x - offset - model.mean) / model.sigma
                loglik += model.log_seen - 0.5 * z * z - math.log(model.sigma) - _LOG_SQRT_2PI
            used += 1
        if used == 0:
            return None
        if self.use_offset:
            loglik -= 0.5 * (offset / OFFSET_PRIOR_DB) ** 2
        return SpotScore(spot.spot_id, spot.room, loglik / used, used, len(common), offset)

    def match(self, live: dict[str, float], active: set[str] | None = None) -> MatchResult | None:
        """
        Match a live vector.

        `active` is the set of scanners currently online; scanners outside it are
        ignored on both sides. Scanners that no spot knows are always ignored.
        """
        heard = {s: x for s, x in live.items() if s in self.known_scanners and (active is None or s in active)}
        if active is not None:
            active = active & self.known_scanners
        room_best: dict[str, SpotScore] = {}
        for spot in self.spots:
            sc = self.score_spot(spot, heard, active)
            if sc is None:
                continue
            cur = room_best.get(sc.room)
            if cur is None or sc.score > cur.score:
                room_best[sc.room] = sc
        if not room_best:
            return None
        temp = max(self.temperature, 1e-6)
        top = max(sc.score for sc in room_best.values())
        exps = {room: math.exp((sc.score - top) / temp) for room, sc in room_best.items()}
        total = sum(exps.values())
        posterior = {room: v / total for room, v in exps.items()}
        ranked = sorted(room_best.values(), key=lambda sc: sc.score, reverse=True)
        return MatchResult(posterior, ranked[0], ranked[1] if len(ranked) > 1 else None, room_best)


# ---------------------------------------------------------------------------
# Smoothing, hysteresis and fallback
# ---------------------------------------------------------------------------
REASON_MATCH = "match"
REASON_LOW_CONF = "fallback-low-confidence"
REASON_NO_DATA = "fallback-no-data"
REASON_NO_FP = "fallback-no-fingerprints"


@dataclass(slots=True)
class SmoothedResult:
    room: str | None  # accepted fingerprint room, None means "use the fallback"
    reason: str
    confidence: float  # smoothed P of the incumbent room (0 if none)
    incumbent: str | None
    challenger: str | None
    challenger_p: float
    holding: bool  # a challenger is waiting for confirmation


class RoomSmoother:
    """EMA over room posteriors, plus hysteresis, dwell time and confidence fallback."""

    def __init__(
        self,
        *,
        tau: float = 6.0,
        margin: float = 0.15,
        confirm_cycles: int = 3,
        instant_p: float = 0.85,
        min_dwell: float = 5.0,
        min_confidence: float = 0.5,
        reset_after: float = 30.0,
    ) -> None:
        self.tau = tau
        self.margin = margin
        self.confirm_cycles = confirm_cycles
        self.instant_p = instant_p
        self.min_dwell = min_dwell
        self.min_confidence = min_confidence
        self.reset_after = reset_after
        self.ema: dict[str, float] = {}
        self.incumbent: str | None = None
        self.last_switch: float = -math.inf
        self.last_data: float | None = None
        self.last_update: float | None = None
        self._streak_room: str | None = None
        self._streak = 0

    def reset(self) -> None:
        self.ema.clear()
        self.incumbent = None
        self.last_switch = -math.inf
        self.last_data = None
        self.last_update = None
        self._streak_room = None
        self._streak = 0

    def _result(self, reason: str, challenger: str | None = None, holding: bool = False) -> SmoothedResult:
        conf = self.ema.get(self.incumbent, 0.0) if self.incumbent else 0.0
        room = self.incumbent if reason == REASON_MATCH else None
        challenger_p = self.ema.get(challenger, 0.0) if challenger else 0.0
        return SmoothedResult(room, reason, conf, self.incumbent, challenger, challenger_p, holding)

    def update(self, posterior: dict[str, float] | None, nowstamp: float) -> SmoothedResult:
        if posterior is None:
            if self.last_data is None or nowstamp - self.last_data > self.reset_after:
                self.reset()
            return self._result(REASON_NO_DATA)

        dt = 0.0 if self.last_update is None else max(0.0, nowstamp - self.last_update)
        self.last_update = nowstamp
        self.last_data = nowstamp
        if not self.ema:
            self.ema = dict(posterior)
        else:
            alpha = 1 - math.exp(-dt / self.tau) if self.tau > 0 else 1.0
            for room in set(self.ema) | set(posterior):
                old = self.ema.get(room, 0.0)
                self.ema[room] = old + alpha * (posterior.get(room, 0.0) - old)
            for room in [r for r, p in self.ema.items() if p < 1e-3 and r != self.incumbent]:
                del self.ema[room]

        ranked = sorted(self.ema.items(), key=lambda kv: kv[1], reverse=True)
        if self.incumbent is None or self.incumbent not in self.ema:
            self.incumbent = ranked[0][0]
            self.last_switch = nowstamp
            self._streak = 0
            self._streak_room = None
        challenger = next((r for r, _ in ranked if r != self.incumbent), None)
        holding = False
        if challenger is not None:
            p_c = self.ema[challenger]
            p_i = self.ema.get(self.incumbent, 0.0)
            dwell_ok = nowstamp - self.last_switch >= self.min_dwell
            if p_c >= p_i + self.margin:
                self._streak = self._streak + 1 if self._streak_room == challenger else 1
                self._streak_room = challenger
            else:
                self._streak = 0
                self._streak_room = None
            if (p_c > p_i and p_c >= self.instant_p) or self._streak >= self.confirm_cycles:
                if dwell_ok:
                    self.incumbent = challenger
                    self.last_switch = nowstamp
                    self._streak = 0
                    self._streak_room = None
                    challenger = next((r for r, _ in ranked if r != self.incumbent), None)
                else:
                    holding = True
            elif self._streak > 0:
                holding = True

        if self.ema.get(self.incumbent, 0.0) < self.min_confidence:
            return self._result(REASON_LOW_CONF, challenger, holding)
        return self._result(REASON_MATCH, challenger, holding)


# ---------------------------------------------------------------------------
# Offline evaluation
# ---------------------------------------------------------------------------
def nearest_proxy_room(
    vec: dict[str, float], scanner_room: dict[str, str], rssi_offsets: dict[str, float] | None = None
) -> str | None:
    """Baseline: room of the strongest (offset-corrected) scanner."""
    offsets = rssi_offsets or {}
    best = None
    best_rssi = -math.inf
    for s, x in vec.items():
        room = scanner_room.get(s)
        if room is None:
            continue
        val = x + offsets.get(s, 0)
        if val > best_rssi:
            best, best_rssi = room, val
    return best


def evaluate(
    spots: list[dict[str, Any]],
    *,
    scanner_room: dict[str, str] | None = None,
    rssi_offsets: dict[str, float] | None = None,
    min_confidence: float = 0.5,
    **matcher_kwargs: Any,
) -> dict[str, Any]:
    """
    Leave-one-spot-out evaluation on the stored window medians.

    Each spot's windows are classified by a Matcher trained on all other spots.
    Returns overall and per-spot accuracy, a confusion matrix (true -> predicted
    -> count) and, if `scanner_room` is given, the nearest-proxy baseline.
    """
    total = correct = base_correct = confident = confident_ok = 0
    per_spot = []
    confusion: dict[str, dict[str, int]] = {}
    for i, spot in enumerate(spots):
        others = spots[:i] + spots[i + 1 :]
        matcher = Matcher(others, **matcher_kwargs)
        n = ok = base_ok = 0
        for vec in spot_windows(spot):
            res = matcher.match(vec)
            pred = max(res.posterior, key=res.posterior.get) if res else None
            n += 1
            ok += pred == spot["room"]
            if res is not None and res.posterior[pred] >= min_confidence:
                confident += 1
                confident_ok += pred == spot["room"]
            row = confusion.setdefault(spot["room"], {})
            key = pred or "none"
            row[key] = row.get(key, 0) + 1
            if scanner_room is not None:
                base_ok += nearest_proxy_room(vec, scanner_room, rssi_offsets) == spot["room"]
        total += n
        correct += ok
        base_correct += base_ok
        entry = {"id": spot.get("id"), "room": spot["room"], "label": spot.get("label"), "windows": n}
        entry["accuracy"] = round(ok / n, 3) if n else None
        if scanner_room is not None:
            entry["baseline_accuracy"] = round(base_ok / n, 3) if n else None
        per_spot.append(entry)
    out: dict[str, Any] = {
        "spots": len(spots),
        "windows": total,
        "accuracy": round(correct / total, 3) if total else None,
        # Unsmoothed: share of windows that would pass the confidence threshold, and their accuracy.
        "confident_share": round(confident / total, 3) if total else None,
        "confident_accuracy": round(confident_ok / confident, 3) if confident else None,
        "confusion": confusion,
        "worst_spots": sorted(
            (s for s in per_spot if s["accuracy"] is not None), key=lambda s: s["accuracy"]
        )[:5],
        "per_spot": per_spot,
    }
    if scanner_room is not None:
        out["baseline_accuracy"] = round(base_correct / total, 3) if total else None
    return out
