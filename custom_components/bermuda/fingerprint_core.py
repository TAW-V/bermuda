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
- The live vector is, per scanner, the median raw RSSI over the last few seconds (8 s by
  default, configurable together with the smoothing via presets).
- ``Matcher`` scores every spot over the same set of scanners (all known, online
  scanners), aggregates the spots of a room (count-normalised mixture by default),
  and turns room scores into a posterior via a softmax. Default method ``knn``:
  RMS dB difference to the spot means, a missing scanner counting as -100 dBm, no
  device offset. ``bayes`` (fp1-fp3) is still available. A live vector far from
  every spot is flagged as an outlier so the caller can fall back.
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
SIGMA_MIN = 4.0  # bayes: per-scanner std floor
SIGMA_MAX = 12.0
SEEN_MIN = 0.05
SEEN_MAX = 0.95
UNKNOWN_MEAN = -100.0  # bayes: model for a scanner never heard at a spot
UNKNOWN_SIGMA = 6.0
MISSING_RSSI = -100.0  # knn: a scanner not heard counts as this (and readings are floored here)
DISTANCE_FLOOR = -100.0  # the same for the outlier distance (fixed, so outlier_db keeps its meaning)
DEFAULT_METHOD = "knn"
OFFSET_MAX_DB = 0.0  # cap of the per-spot device offset; 0 disables it (tuned: it erases the level
# difference between adjacent rooms)
OFFSET_PRIOR_DB = 6.0  # bayes: gentle prior keeping the device offset near 0
OFFSET_MIN_SEEN = 0.5
OFFSET_MIN_SCANNERS = 4  # only estimate an offset when this many strong common scanners agree
PRESENCE_WEIGHT = 1.0  # bayes: weight of the seen / not-seen log terms
KNN_SCALE_DB = 4.0  # knn: score = -0.5 (rms_db / scale)^2
OUTLIER_DB = 10.0  # nearest spot farther than this (RMS dB) -> outlier, fall back; 0 disables
MIN_COMMON = 3  # Minimum common heard scanners for a spot to be scored
TEMPERATURE = 0.3  # knn softmax temperature (lowest leave-one-out log-loss)
TEMPERATURE_BAYES = 0.5  # bayes softmax temperature

# Room switching (live window + RoomSmoother). Tuned with scripts/fingerprint_eval.py --latency on
# the bedroom / walk-in closet recordings, readings every ~3.1 s per scanner (the Shelly limit, also
# at 10 Hz advertising). Window and tau set the lag; the flicker that remains is mostly genuine
# misclassification, so dwell and confirm cycles change little. Simulated, in-sample / leave-out:
#   fast      6 s, tau 2:  switch ~5 s,  flicker 0.9 / 1.6 per min
#   balanced  8 s, tau 3:  switch ~8 s,  flicker 0.6 / 1.2 per min (time in a wrong room as fp4)
#   stable   12 s, tau 6:  switch ~13 s, flicker 0.3 / 0.5 per min (the fp4 behaviour)
PRESET_FAST = "fast"
PRESET_BALANCED = "balanced"
PRESET_STABLE = "stable"
PRESET_CUSTOM = "custom"  # use the individual options
FP_PRESETS: dict[str, dict[str, float]] = {
    PRESET_FAST: {"live_window": 6.0, "tau": 2.0, "confirm_cycles": 1, "min_dwell": 3.0},
    PRESET_BALANCED: {"live_window": 8.0, "tau": 3.0, "confirm_cycles": 2, "min_dwell": 3.0},
    PRESET_STABLE: {"live_window": 12.0, "tau": 6.0, "confirm_cycles": 3, "min_dwell": 5.0},
}
PRESETS = [PRESET_FAST, PRESET_BALANCED, PRESET_STABLE, PRESET_CUSTOM]
DEFAULT_PRESET = PRESET_BALANCED
LIVE_WINDOW_S = FP_PRESETS[DEFAULT_PRESET]["live_window"]  # Live vector window (s)
SMOOTH_TAU_S = FP_PRESETS[DEFAULT_PRESET]["tau"]  # EMA time constant of the room posteriors (s)
CONFIRM_CYCLES = int(FP_PRESETS[DEFAULT_PRESET]["confirm_cycles"])  # cycles a challenger must lead by margin
MIN_DWELL_S = FP_PRESETS[DEFAULT_PRESET]["min_dwell"]  # minimum time between two room switches (s)
SWITCH_MARGIN = 0.15
INSTANT_P = 0.85
# Option ranges (options flow)
LIVE_WINDOW_RANGE = (3.0, 30.0)
SMOOTH_TAU_RANGE = (0.0, 30.0)
CONFIRM_CYCLES_RANGE = (1, 10)
MIN_DWELL_RANGE = (0.0, 60.0)


def resolve_switching(preset: str | None, custom: dict[str, Any] | None = None) -> dict[str, float]:
    """
    Live window and smoother settings for a preset; ``custom`` (or an unknown preset) uses the
    given values, each falling back to the default preset and clamped to the option ranges.
    """
    if preset in FP_PRESETS:
        return dict(FP_PRESETS[preset])
    base = FP_PRESETS[DEFAULT_PRESET]
    custom = custom or {}

    def val(key: str, rng: tuple[float, float]) -> float:
        try:
            v = float(custom.get(key, base[key]))
        except (TypeError, ValueError):
            v = float(base[key])
        if math.isnan(v):
            v = float(base[key])
        return clamp(v, rng[0], rng[1])

    return {
        "live_window": val("live_window", LIVE_WINDOW_RANGE),
        "tau": val("tau", SMOOTH_TAU_RANGE),
        "confirm_cycles": int(round(val("confirm_cycles", CONFIRM_CYCLES_RANGE))),
        "min_dwell": val("min_dwell", MIN_DWELL_RANGE),
    }


def smoother_kwargs(settings: dict[str, Any]) -> dict[str, Any]:
    """RoomSmoother kwargs from a preset / settings dict (ignores live_window)."""
    return {
        "tau": float(settings.get("tau", SMOOTH_TAU_S)),
        "confirm_cycles": int(settings.get("confirm_cycles", CONFIRM_CYCLES)),
        "min_dwell": float(settings.get("min_dwell", MIN_DWELL_S)),
    }


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
METHOD_KNN = "knn"  # nearest spot means, RMS dB distance (default)
METHOD_BAYES = "bayes"  # Gaussian naive Bayes with a detection term (fp1-fp3)
METHODS = [METHOD_KNN, METHOD_BAYES]
ROOM_AGG_MAX = "max"  # room score = best of its top-k spots
ROOM_AGG_MEAN = "mean"  # room score = mean of its top-k spots
ROOM_AGG_MIX = "mix"  # room score = log-mean-exp of its top-k spots (mixture)
ROOM_AGGS = [ROOM_AGG_MAX, ROOM_AGG_MEAN, ROOM_AGG_MIX]


@dataclass(slots=True)
class _ScannerModel:
    mean: float
    sigma: float
    log_seen: float
    log_unseen: float
    seen: float

    @classmethod
    def from_stats(cls, stats: dict[str, float], sigma_min: float = SIGMA_MIN) -> _ScannerModel:
        seen = clamp(float(stats.get("seen", SEEN_MIN)), SEEN_MIN, SEEN_MAX)
        sigma = clamp(float(stats.get("std", sigma_min)), sigma_min, max(sigma_min, SIGMA_MAX))
        return cls(float(stats["mean"]), sigma, math.log(seen), math.log(1 - seen), seen)


def _unknown_model(sigma_min: float) -> _ScannerModel:
    return _ScannerModel(
        UNKNOWN_MEAN, max(UNKNOWN_SIGMA, sigma_min), math.log(SEEN_MIN), math.log(1 - SEEN_MIN), SEEN_MIN
    )


@dataclass(slots=True)
class SpotScore:
    spot_id: str
    room: str
    score: float  # per-scanner score (tempered log-likelihood, or -0.5 (d / scale)^2 for knn)
    used: int  # scanners in the score (the same for every spot)
    common: int  # heard live and known at the spot
    offset: float  # estimated device offset in dB
    distance: float = 0.0  # dB: sqrt(sum of squared differences / heard scanners), missing = floor


@dataclass(slots=True)
class MatchResult:
    posterior: dict[str, float]  # room -> P
    best: SpotScore  # best spot of the best room
    second: SpotScore | None  # best spot of the runner-up room
    room_best: dict[str, SpotScore] = field(default_factory=dict)
    distance: float = 0.0  # distance to the nearest spot of any room (dB)
    outlier: bool = False  # nearest spot farther than outlier_db: position not covered by any spot


@dataclass(slots=True)
class _SpotModel:
    spot_id: str
    room: str
    scanners: dict[str, _ScannerModel]


class Matcher:
    """
    Scores a live vector against stored spots.

    Methods:
    - ``knn``: per spot the RMS difference in dB between the live vector and the spot means over the
      union of scanners, a scanner missing on one side counting as ``missing_rssi`` (and values are
      floored there). Score = -0.5 (d / knn_scale)^2. Absolute levels matter, presence only through
      the floor, and every spot weighs the same however many scanners heard it.
    - ``bayes``: the fp1-fp3 Gaussian naive Bayes with a detection term (``presence_weight`` scales
      the seen/unseen log terms) and a per-scanner std floor ``sigma_min``.

    Both can subtract a per-spot device offset (median live-minus-spot difference over the strong
    common scanners), capped at ``offset_max`` dB and only estimated when at least
    ``offset_min_scanners`` scanners agree. ``offset_max = 0`` disables it.

    A room's score aggregates its best ``room_k`` spots (0 = all) with ``room_agg``; the room
    posterior is a softmax over room scores with ``temperature``. If the nearest spot is farther
    than ``outlier_db`` the result is flagged as an outlier (``outlier_db = 0`` disables this).
    """

    def __init__(
        self,
        spots: list[dict[str, Any]],
        *,
        method: str = DEFAULT_METHOD,
        use_offset: bool = True,
        offset_max: float = OFFSET_MAX_DB,
        offset_min_scanners: int = OFFSET_MIN_SCANNERS,
        sigma_min: float = SIGMA_MIN,
        presence_weight: float = PRESENCE_WEIGHT,
        knn_scale: float = KNN_SCALE_DB,
        room_agg: str = ROOM_AGG_MIX,
        room_k: int = 0,
        temperature: float | None = None,
        min_common: int = MIN_COMMON,
        outlier_db: float = OUTLIER_DB,
        missing_rssi: float = MISSING_RSSI,
    ) -> None:
        if method not in METHODS:
            raise ValueError(f"unknown method {method!r}")
        if room_agg not in ROOM_AGGS:
            raise ValueError(f"unknown room_agg {room_agg!r}")
        self.method = method
        self.offset_max = float(offset_max) if use_offset else 0.0
        self.use_offset = self.offset_max > 0
        self.offset_min_scanners = max(1, int(offset_min_scanners))
        self.sigma_min = float(sigma_min)
        self.presence_weight = float(presence_weight)
        self.knn_scale = max(float(knn_scale), 0.1)
        self.room_agg = room_agg
        self.room_k = max(0, int(room_k))
        if temperature is None:
            temperature = TEMPERATURE if method == METHOD_KNN else TEMPERATURE_BAYES
        self.temperature = max(float(temperature), 1e-6)
        self.min_common = min_common
        self.outlier_db = max(0.0, float(outlier_db))
        self.floor = float(missing_rssi)
        self._unknown = _unknown_model(self.sigma_min)
        self.spots: list[_SpotModel] = []
        self.known_scanners: set[str] = set()
        for spot in spots:
            models = {s: _ScannerModel.from_stats(st, self.sigma_min) for s, st in spot.get("scanners", {}).items()}
            if not models or not spot.get("room"):
                continue
            self.spots.append(_SpotModel(str(spot.get("id")), str(spot["room"]), models))
            self.known_scanners.update(models)

    def __len__(self) -> int:
        return len(self.spots)

    @property
    def rooms(self) -> set[str]:
        return {s.room for s in self.spots}

    def _offset(self, spot: _SpotModel, heard: dict[str, float], common: list[str]) -> float:
        if not self.use_offset:
            return 0.0
        diffs = [heard[s] - spot.scanners[s].mean for s in common if spot.scanners[s].seen > OFFSET_MIN_SEEN]
        if len(diffs) < self.offset_min_scanners:
            return 0.0
        return clamp(median(diffs), -self.offset_max, self.offset_max)

    def score_spot(
        self,
        spot: _SpotModel,
        heard: dict[str, float],
        scanners: set[str],
        extra: tuple[float, int] = (0.0, 0),
    ) -> SpotScore | None:
        """
        Score one spot over `scanners`, the SAME set for every spot (all known, active
        scanners). Scoring each spot over its own scanners would dilute the misfit of
        spots that list many (weak) scanners and make them win (the fp3 bias).

        `heard` holds only known scanners; `extra` is (sum of squares, count) of heard
        scanners no spot knows, which only count towards the outlier distance.
        """
        common = [s for s in heard if s in spot.scanners]
        if len(common) < self.min_common:
            return None
        offset = self._offset(spot, heard, common)
        floor = self.floor
        sq = 0.0  # knn score, with the configurable floor
        dsq = 0.0  # outlier distance, always with DISTANCE_FLOOR
        loglik = 0.0
        bayes = self.method == METHOD_BAYES
        w = self.presence_weight
        for s in scanners:
            model = spot.scanners.get(s)
            x = heard.get(s)
            xo = None if x is None else x - offset
            m = None if model is None else model.mean
            a = floor if xo is None else max(xo, floor)
            b = floor if m is None else max(m, floor)
            sq += (a - b) ** 2
            a = DISTANCE_FLOOR if xo is None else max(xo, DISTANCE_FLOOR)
            b = DISTANCE_FLOOR if m is None else max(m, DISTANCE_FLOOR)
            dsq += (a - b) ** 2
            if bayes:
                if model is None:
                    model = self._unknown
                if x is None:
                    loglik += w * model.log_unseen
                else:
                    z = (x - offset - model.mean) / model.sigma
                    loglik += w * model.log_seen - 0.5 * z * z - math.log(model.sigma) - _LOG_SQRT_2PI
        # Normalise by the number of heard scanners: the same for every spot, so it only sets the scale.
        n_heard = max(1, len(heard) + extra[1])
        distance = math.sqrt((dsq + extra[0]) / n_heard)
        if bayes:
            if self.use_offset:
                loglik -= 0.5 * (offset / OFFSET_PRIOR_DB) ** 2
            score = loglik / max(1, len(heard))
        else:
            score = -0.5 * (sq / max(1, len(heard))) / self.knn_scale**2
        return SpotScore(spot.spot_id, spot.room, score, len(scanners), len(common), offset, distance)

    def _room_score(self, scores: list[float]) -> float:
        scores = sorted(scores, reverse=True)
        if self.room_k:
            scores = scores[: self.room_k]
        if self.room_agg == ROOM_AGG_MAX:
            return scores[0]
        if self.room_agg == ROOM_AGG_MEAN:
            return sum(scores) / len(scores)
        t = self.temperature
        top = scores[0]
        return top + t * math.log(sum(math.exp((s - top) / t) for s in scores) / len(scores))

    def match(self, live: dict[str, float], active: set[str] | None = None) -> MatchResult | None:
        """
        Match a live vector.

        `active` is the set of scanners currently online; scanners outside it are
        ignored on both sides. Scanners that no spot knows do not affect the room
        scores, but a strong one makes the outlier distance large.
        """
        live = {s: x for s, x in live.items() if active is None or s in active}
        heard = {s: x for s, x in live.items() if s in self.known_scanners}
        unknown = [max(x, DISTANCE_FLOOR) - DISTANCE_FLOOR for s, x in live.items() if s not in self.known_scanners]
        extra = (sum(v * v for v in unknown), len(unknown))
        scanners = self.known_scanners if active is None else active & self.known_scanners
        per_room: dict[str, list[SpotScore]] = {}
        for spot in self.spots:
            sc = self.score_spot(spot, heard, scanners, extra)
            if sc is not None:
                per_room.setdefault(sc.room, []).append(sc)
        if not per_room:
            return None
        room_score = {room: self._room_score([sc.score for sc in scs]) for room, scs in per_room.items()}
        room_best = {room: max(scs, key=lambda sc: sc.score) for room, scs in per_room.items()}
        temp = self.temperature
        top = max(room_score.values())
        exps = {room: math.exp((score - top) / temp) for room, score in room_score.items()}
        total = sum(exps.values())
        posterior = {room: v / total for room, v in exps.items()}
        ranked = sorted(room_score, key=room_score.get, reverse=True)
        distance = min(sc.distance for scs in per_room.values() for sc in scs)
        outlier = self.outlier_db > 0 and distance > self.outlier_db
        return MatchResult(
            posterior,
            room_best[ranked[0]],
            room_best[ranked[1]] if len(ranked) > 1 else None,
            room_best,
            distance,
            outlier,
        )


# ---------------------------------------------------------------------------
# Smoothing, hysteresis and fallback
# ---------------------------------------------------------------------------
REASON_MATCH = "match"
REASON_LOW_CONF = "fallback-low-confidence"
REASON_NO_DATA = "fallback-no-data"
REASON_NO_FP = "fallback-no-fingerprints"
REASON_OUTLIER = "fallback-outlier"  # far from every recorded spot (e.g. a room without spots)


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
        tau: float = SMOOTH_TAU_S,
        margin: float = SWITCH_MARGIN,
        confirm_cycles: int = CONFIRM_CYCLES,
        instant_p: float = INSTANT_P,
        min_dwell: float = MIN_DWELL_S,
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

    def update(
        self, posterior: dict[str, float] | None, nowstamp: float, reason: str = REASON_NO_DATA
    ) -> SmoothedResult:
        """
        Feed one cycle. `posterior` None means no usable match this cycle (`reason` says why:
        no data, or an outlier); the state is kept for `reset_after` seconds.
        """
        if posterior is None:
            if self.last_data is None or nowstamp - self.last_data > self.reset_after:
                self.reset()
            return self._result(reason)

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


def _balanced(confusion: dict[str, dict[str, int]]) -> tuple[float | None, dict[str, float]]:
    recall = {}
    for room, row in confusion.items():
        n = sum(row.values())
        if n:
            recall[room] = round(row.get(room, 0) / n, 3)
    return (round(sum(recall.values()) / len(recall), 3) if recall else None), recall


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    vals = sorted(values)
    idx = min(len(vals) - 1, max(0, math.ceil(q * len(vals)) - 1))
    return vals[idx]


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
    Returns overall, per-room (balanced) and per-spot accuracy, a confusion matrix
    (true -> predicted -> count), the log-loss and mean confidence of the room
    posterior, the held-out nearest-spot distances (to calibrate the outlier
    threshold) and, if `scanner_room` is given, the nearest-proxy baseline and the
    effective accuracy with fallback to it (outlier or low confidence).
    """
    total = correct = confident = confident_ok = outliers = eff_correct = 0
    log_loss = conf_sum = 0.0
    per_spot = []
    confusion: dict[str, dict[str, int]] = {}
    base_confusion: dict[str, dict[str, int]] = {}
    eff_confusion: dict[str, dict[str, int]] = {}
    distances: list[float] = []

    def bump(table: dict[str, dict[str, int]], true: str, pred: str | None) -> None:
        row = table.setdefault(true, {})
        key = pred or "none"
        row[key] = row.get(key, 0) + 1

    for i, spot in enumerate(spots):
        others = spots[:i] + spots[i + 1 :]
        matcher = Matcher(others, **matcher_kwargs)
        room = spot["room"]
        n = ok = base_ok = 0
        for vec in spot_windows(spot):
            res = matcher.match(vec)
            pred = max(res.posterior, key=res.posterior.get) if res else None
            n += 1
            ok += pred == room
            bump(confusion, room, pred)
            p_true = res.posterior.get(room, 0.0) if res else 0.0
            log_loss -= math.log(max(p_true, 0.01))
            accepted = res is not None and not res.outlier and res.posterior[pred] >= min_confidence
            if res is not None:
                conf_sum += res.posterior[pred]
                distances.append(res.distance)
                outliers += res.outlier
            if accepted:
                confident += 1
                confident_ok += pred == room
            if scanner_room is not None:
                base = nearest_proxy_room(vec, scanner_room, rssi_offsets)
                base_ok += base == room
                bump(base_confusion, room, base)
                eff = pred if accepted else base
                eff_correct += eff == room
                bump(eff_confusion, room, eff)
        total += n
        correct += ok
        entry = {"id": spot.get("id"), "room": room, "label": spot.get("label"), "windows": n}
        entry["accuracy"] = round(ok / n, 3) if n else None
        if scanner_room is not None:
            entry["baseline_accuracy"] = round(base_ok / n, 3) if n else None
        per_spot.append(entry)

    balanced, recall = _balanced(confusion)
    out: dict[str, Any] = {
        "spots": len(spots),
        "windows": total,
        "accuracy": round(correct / total, 3) if total else None,
        "balanced_accuracy": balanced,
        "room_recall": recall,
        "log_loss": round(log_loss / total, 3) if total else None,
        "mean_confidence": round(conf_sum / total, 3) if total else None,
        # Unsmoothed: share of windows that would be accepted (not an outlier, P >= min_confidence) and their accuracy.
        "confident_share": round(confident / total, 3) if total else None,
        "confident_accuracy": round(confident_ok / confident, 3) if confident else None,
        "outlier_share": round(outliers / total, 3) if total else None,
        "distance_p95": round(_percentile(distances, 0.95), 1) if distances else None,
        "distance_max": round(max(distances), 1) if distances else None,
        "confusion": confusion,
        "worst_spots": sorted((s for s in per_spot if s["accuracy"] is not None), key=lambda s: s["accuracy"])[:5],
        "per_spot": per_spot,
    }
    if scanner_room is not None:
        base_correct = sum(row.get(room, 0) for room, row in base_confusion.items())
        out["baseline_accuracy"] = round(base_correct / total, 3) if total else None
        out["baseline_balanced_accuracy"] = _balanced(base_confusion)[0]
        out["effective_accuracy"] = round(eff_correct / total, 3) if total else None
        out["effective_balanced_accuracy"] = _balanced(eff_confusion)[0]
    return out
