"""
RSSI fingerprinting for Bermuda (TAW-V fork): Home Assistant glue.

The maths lives in fingerprint_core.py (no HA imports). This module holds:
- the persistent store (``.storage/bermuda.fingerprints``),
- the services (record / cancel / delete / clear / list / evaluate / map_room),
- the per-cycle update called from the coordinator after Bermuda's own
  nearest-proxy area selection: it feeds the active recording, matches every
  tracked device and, in override mode, re-applies the area.

Modes (Bermuda global option ``fingerprint_mode``):
- off:      no matching and no per-device fingerprint sensors (recording still works).
- shadow:   separate "Fingerprint room" sensor; Bermuda's Area/Floor unchanged (default).
- override: Bermuda's Area/Floor show the fingerprint room when it is accepted and
            mapped to an HA area; otherwise Bermuda's own result stands.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from bluetooth_data_tools import monotonic_time_coarse
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.storage import Store
from homeassistant.util import slugify
from homeassistant.util.dt import now as dt_now

from .const import (
    _LOGGER,
    CONF_FINGERPRINT_MODE,
    CONF_FP_METHOD,
    CONF_FP_MIN_CONFIDENCE,
    CONF_FP_OFFSET_MAX,
    CONF_FP_OUTLIER_DB,
    CONF_RSSI_OFFSETS,
    DEFAULT_FINGERPRINT_MODE,
    DEFAULT_FP_METHOD,
    DEFAULT_FP_MIN_CONFIDENCE,
    DEFAULT_FP_OFFSET_MAX,
    DEFAULT_FP_OUTLIER_DB,
    DOMAIN,
    FINGERPRINT_MODE_OFF,
    FINGERPRINT_MODE_OVERRIDE,
)
from .fingerprint_core import (
    LIVE_WINDOW_S,
    METHODS,
    REASON_MATCH,
    REASON_NO_DATA,
    REASON_NO_FP,
    REASON_OUTLIER,
    Matcher,
    RoomSmoother,
    SpotRecorder,
    evaluate,
    live_vector,
)

if TYPE_CHECKING:
    from .bermuda_device import BermudaDevice
    from .coordinator import BermudaDataUpdateCoordinator

STORE_KEY = f"{DOMAIN}.fingerprints"
STORE_VERSION = 1
SAVE_DELAY = 2

SCANNER_ACTIVE_S = 60  # A scanner counts as online if it reported anything this recently
DEVICE_UNSEEN_S = 30  # Device unseen longer than this -> fallback-no-data
DURATION_DEFAULT = 120
DURATION_MIN = 30
DURATION_MAX = 600
WARN_MIN_SCANNERS = 4
WARN_MAX_GAP_S = 15
AUTO_LABEL = "Spot"
CARRY_OPTIONS = ["hand", "pocket", "table", "other"]

EVENT_RECORDED = f"{DOMAIN}_fingerprint_recorded"

SERVICE_RECORD = "record_fingerprint"
SERVICE_CANCEL = "cancel_fingerprint"
SERVICE_DELETE = "delete_fingerprint"
SERVICE_CLEAR = "clear_fingerprints"
SERVICE_LIST = "list_fingerprints"
SERVICE_EVALUATE = "evaluate_fingerprints"
SERVICE_MAP_ROOM = "map_room"
SERVICES = (
    SERVICE_RECORD,
    SERVICE_CANCEL,
    SERVICE_DELETE,
    SERVICE_CLEAR,
    SERVICE_LIST,
    SERVICE_EVALUATE,
    SERVICE_MAP_ROOM,
)


def _single(value: Any) -> Any:
    """Area selectors may hand us a one-element list."""
    if isinstance(value, list):
        if len(value) != 1:
            raise vol.Invalid("expected a single value")
        return value[0]
    return value


_ROOM_FIELDS = {
    vol.Optional("area_id"): vol.All(_single, cv.string),
    vol.Optional("room"): cv.string,
}


@dataclass
class FingerprintState:
    """Per-device fingerprint result, read by the sensor entities."""

    room: str | None = None  # accepted fingerprint room key (None = fallback)
    name: str | None = None  # displayed name (fingerprint room, or Bermuda's area on fallback)
    area_id: str | None = None  # HA area of the displayed result
    reason: str = REASON_NO_FP
    confidence: float = 0.0
    source: str = "nearest"  # "fingerprint" or "nearest"
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class RecordingSession:
    device_address: str
    room: str
    label: str
    spot_id: str | None  # existing spot to replace, if any
    carry: str | None
    recorder: SpotRecorder
    started: str  # ISO wall clock
    last_taken: dict[tuple[str, str], float] = field(default_factory=dict)
    waiters: list[asyncio.Future] = field(default_factory=list)


class FingerprintManager:
    """Owns fingerprint storage, services, recording and matching for one Bermuda entry."""

    def __init__(self, hass: HomeAssistant, coordinator: BermudaDataUpdateCoordinator) -> None:
        self.hass = hass
        self.coordinator = coordinator
        opts = coordinator.options
        self.mode: str = opts.get(CONF_FINGERPRINT_MODE, DEFAULT_FINGERPRINT_MODE)
        method = opts.get(CONF_FP_METHOD, DEFAULT_FP_METHOD)
        self.matcher_kwargs: dict[str, Any] = {
            "method": method if method in METHODS else DEFAULT_FP_METHOD,
            "offset_max": float(opts.get(CONF_FP_OFFSET_MAX, DEFAULT_FP_OFFSET_MAX)),
            "outlier_db": float(opts.get(CONF_FP_OUTLIER_DB, DEFAULT_FP_OUTLIER_DB)),
        }
        self.min_confidence = float(opts.get(CONF_FP_MIN_CONFIDENCE, DEFAULT_FP_MIN_CONFIDENCE))
        self._store: Store[dict[str, Any]] = Store(hass, STORE_VERSION, STORE_KEY)
        self.spots: list[dict[str, Any]] = []
        self.rooms: dict[str, dict[str, Any]] = {}
        self.loaded = False
        self.matcher: Matcher | None = None
        self.spots_version = 0  # bumped on every change, for entity refresh
        self.recording: RecordingSession | None = None
        self.status: dict[str, Any] = {"state": "idle"}
        self.status_version = 0
        self.states: dict[str, FingerprintState] = {}
        self._smoothers: dict[str, RoomSmoother] = {}

    @property
    def enabled(self) -> bool:
        return self.mode != FINGERPRINT_MODE_OFF

    # ------------------------------------------------------------------ storage
    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        self.spots = list(data.get("spots", []))
        self.rooms = dict(data.get("rooms", {}))
        self.loaded = True
        self._spots_changed(save=False)
        _LOGGER.debug("Loaded %d fingerprint spots in %d rooms", len(self.spots), len(self.rooms))

    def _data_to_save(self) -> dict[str, Any]:
        return {"spots": self.spots, "rooms": self.rooms}

    def _spots_changed(self, save: bool = True) -> None:
        # Drop label rooms without spots; area rooms are just a cache of the name.
        used = {s["room"] for s in self.spots}
        if self.recording is not None:
            used.add(self.recording.room)
        self.rooms = {k: v for k, v in self.rooms.items() if k in used}
        self.matcher = Matcher(self.spots, **self.matcher_kwargs) if self.spots else None
        self.spots_version += 1
        if save:
            self._store.async_delay_save(self._data_to_save, SAVE_DELAY)

    # ------------------------------------------------------------------ helpers
    def room_area_id(self, room: str | None) -> str | None:
        if room is None:
            return None
        return self.rooms.get(room, {}).get("area_id")

    def room_name(self, room: str | None) -> str | None:
        if room is None:
            return None
        area_id = self.room_area_id(room)
        if area_id and (area := self.coordinator.ar.async_get_area(area_id)):
            return area.name
        return self.rooms.get(room, {}).get("name", room)

    def _resolve_room(self, data: dict[str, Any], create: bool) -> str:
        """Return a room key from area_id / room fields, registering it if `create`."""
        area_id = data.get("area_id")
        text = (data.get("room") or "").strip()
        ar = self.coordinator.ar
        area = None
        if area_id:
            area = ar.async_get_area(area_id)
            if area is None:
                raise ServiceValidationError(f"Unknown area_id '{area_id}'")
        elif text:
            area = ar.async_get_area(text) or ar.async_get_area_by_name(text)
            if area is None:
                low = text.casefold()
                area = next(
                    (a for a in ar.async_list_areas() if low in {al.casefold() for al in (a.aliases or ())}), None
                )
        else:
            raise ServiceValidationError("Give an area_id or a room")

        if area is not None:
            key, entry = area.id, {"area_id": area.id, "name": area.name}
        else:
            key = next((k for k, v in self.rooms.items() if v.get("name", "").casefold() == text.casefold()), None)
            if key is None:
                key = slugify(text)
            entry = self.rooms.get(key, {"area_id": None, "name": text})
        if not create and key not in self.rooms:
            raise ServiceValidationError(f"No fingerprints for room '{area_id or text}'")
        if create:
            self.rooms[key] = entry
        return key

    def _resolve_device(self, value: str | None) -> BermudaDevice:
        tracked = [d for d in self.coordinator.devices.values() if d.create_sensor]
        if not value:
            if len(tracked) == 1:
                return tracked[0]
            raise ServiceValidationError(f"{len(tracked)} tracked devices; give the device to record with")
        low = value.strip().lower()
        # A device registry id (from a device selector)?
        if dev_entry := self.coordinator.dr.async_get(value):
            low_set = {ident.lower() for _domain, ident in dev_entry.identifiers}
        else:
            low_set = {low}
        for dev in tracked:
            names = {dev.address.lower(), (dev.unique_id or "").lower(), dev.name.lower()}
            if dev.name_by_user:
                names.add(dev.name_by_user.lower())
            if names & low_set:
                return dev
        raise ServiceValidationError(f"No tracked Bermuda device matches '{value}'")

    def _next_label(self, room: str) -> str:
        nums = [0]
        for spot in self.spots:
            if spot["room"] == room:
                m = re.fullmatch(rf"{AUTO_LABEL} (\d+)", spot.get("label") or "")
                nums.append(int(m.group(1)) if m else 0)
        count = sum(1 for s in self.spots if s["room"] == room)
        return f"{AUTO_LABEL} {max(max(nums), count) + 1}"

    def _find_spot(self, spot_id: str) -> dict[str, Any]:
        for spot in self.spots:
            if spot["id"] == spot_id:
                return spot
        raise ServiceValidationError(f"Unknown spot_id '{spot_id}'")

    def _scanner_name(self, address: str) -> str:
        dev = self.coordinator.devices.get(address)
        return dev.name if dev is not None else address

    def spot_summary(self, spot: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": spot["id"],
            "room": spot["room"],
            "label": spot.get("label"),
            "samples": spot.get("samples", 0),
            "scanners": len(spot.get("scanners", {})),
            "duration_s": spot.get("duration_s"),
            "created": spot.get("created"),
            "updated": spot.get("updated"),
        }

    def areas_overview(self) -> dict[str, dict[str, Any]]:
        """Per room (all HA areas plus label-only rooms): name, spot count and spot summaries."""
        out: dict[str, dict[str, Any]] = {}
        for area in sorted(self.coordinator.ar.async_list_areas(), key=lambda a: a.name.casefold()):
            out[area.id] = {"name": area.name, "area_id": area.id, "count": 0, "spots": []}
        for key, room in self.rooms.items():
            if key not in out:
                out[key] = {"name": room.get("name", key), "area_id": room.get("area_id"), "count": 0, "spots": []}
        for spot in self.spots:
            entry = out.get(spot["room"])
            if entry is not None:
                entry["count"] += 1
                entry["spots"].append(self.spot_summary(spot))
        return out

    # ------------------------------------------------------------------ services
    @callback
    def async_register_services(self) -> None:
        reg = self.hass.services.async_register
        reg(
            DOMAIN,
            SERVICE_RECORD,
            self._svc_record,
            vol.Schema(
                {
                    **_ROOM_FIELDS,
                    vol.Optional("label"): cv.string,
                    vol.Optional("spot_id"): cv.string,
                    vol.Optional("device"): vol.All(_single, cv.string),
                    vol.Optional("duration", default=DURATION_DEFAULT): vol.All(
                        vol.Coerce(int), vol.Range(min=DURATION_MIN, max=DURATION_MAX)
                    ),
                    vol.Optional("carry"): vol.In(CARRY_OPTIONS),
                    vol.Optional("replace", default=False): cv.boolean,
                }
            ),
            SupportsResponse.OPTIONAL,
        )
        reg(DOMAIN, SERVICE_CANCEL, self._svc_cancel, vol.Schema({}), SupportsResponse.NONE)
        reg(
            DOMAIN,
            SERVICE_DELETE,
            self._svc_delete,
            vol.Schema({vol.Optional("spot_id"): cv.string, **_ROOM_FIELDS, vol.Optional("label"): cv.string}),
            SupportsResponse.OPTIONAL,
        )
        reg(DOMAIN, SERVICE_CLEAR, self._svc_clear, vol.Schema(_ROOM_FIELDS), SupportsResponse.OPTIONAL)
        reg(DOMAIN, SERVICE_LIST, self._svc_list, vol.Schema({}), SupportsResponse.ONLY)
        reg(
            DOMAIN,
            SERVICE_EVALUATE,
            self._svc_evaluate,
            vol.Schema(
                {
                    vol.Optional("method"): vol.In(METHODS),
                    vol.Optional("offset_max"): vol.All(vol.Coerce(float), vol.Range(min=0, max=10)),
                    vol.Optional("outlier_db"): vol.All(vol.Coerce(float), vol.Range(min=0, max=40)),
                    vol.Optional("use_offset"): cv.boolean,
                    vol.Optional("temperature"): vol.All(vol.Coerce(float), vol.Range(min=0.05, max=20)),
                    vol.Optional("details", default=False): cv.boolean,
                }
            ),
            SupportsResponse.ONLY,
        )
        reg(
            DOMAIN,
            SERVICE_MAP_ROOM,
            self._svc_map_room,
            vol.Schema({vol.Required("room"): cv.string, vol.Required("area_id"): vol.All(_single, cv.string)}),
            SupportsResponse.NONE,
        )

    @callback
    def async_shutdown(self) -> None:
        """Called on entry unload."""
        for service in SERVICES:
            self.hass.services.async_remove(DOMAIN, service)
        if self.recording is not None:
            self._finish_recording(cancelled=True)

    async def _svc_record(self, call: ServiceCall) -> ServiceResponse:
        if not self.loaded:
            raise ServiceValidationError("Fingerprints are still loading, try again in a moment")
        if self.recording is not None:
            raise ServiceValidationError(
                f"Already recording '{self.room_name(self.recording.room)} {self.recording.label}'"
            )
        data = call.data
        device = self._resolve_device(data.get("device"))
        replace_id = None
        if spot_id := data.get("spot_id"):
            spot = self._find_spot(spot_id)
            room, label, replace_id = spot["room"], spot.get("label") or self._next_label(spot["room"]), spot_id
        else:
            room = self._resolve_room(data, create=True)
            label = (data.get("label") or "").strip() or self._next_label(room)
            if data.get("replace"):
                replace_id = next(
                    (s["id"] for s in self.spots if s["room"] == room and s.get("label") == label), None
                )
        session = RecordingSession(
            device_address=device.address,
            room=room,
            label=label,
            spot_id=replace_id,
            carry=data.get("carry"),
            recorder=SpotRecorder(monotonic_time_coarse(), data["duration"]),
            started=dt_now().isoformat(timespec="seconds"),
        )
        self.recording = session
        self._update_status(monotonic_time_coarse())
        _LOGGER.info("Fingerprint recording started: %s / %s (%ss)", self.room_name(room), label, data["duration"])
        if not call.return_response:
            return None
        fut: asyncio.Future = self.hass.loop.create_future()
        session.waiters.append(fut)
        return await fut

    async def _svc_cancel(self, call: ServiceCall) -> None:
        if self.recording is not None:
            self._finish_recording(cancelled=True)

    async def _svc_delete(self, call: ServiceCall) -> ServiceResponse:
        data = call.data
        if spot_id := data.get("spot_id"):
            ids = {self._find_spot(spot_id)["id"]}
        elif data.get("label") and (data.get("area_id") or data.get("room")):
            room = self._resolve_room(data, create=False)
            ids = {s["id"] for s in self.spots if s["room"] == room and s.get("label") == data["label"]}
            if not ids:
                raise ServiceValidationError(f"No spot '{data['label']}' in '{self.room_name(room)}'")
        else:
            raise ServiceValidationError("Give a spot_id, or a room/area_id plus label")
        return self._delete(ids)

    async def _svc_clear(self, call: ServiceCall) -> ServiceResponse:
        room = self._resolve_room(call.data, create=False)
        return self._delete({s["id"] for s in self.spots if s["room"] == room})

    def _delete(self, ids: set[str]) -> dict[str, Any]:
        before = len(self.spots)
        self.spots = [s for s in self.spots if s["id"] not in ids]
        deleted = before - len(self.spots)
        if deleted:
            self._spots_changed()
        return {"deleted": deleted, "remaining": len(self.spots)}

    async def _svc_list(self, call: ServiceCall) -> ServiceResponse:
        return {
            "mode": self.mode,
            "spots": len(self.spots),
            "rooms": {k: v for k, v in self.areas_overview().items() if v["count"]},
        }

    async def _svc_evaluate(self, call: ServiceCall) -> ServiceResponse:
        if len(self.spots) < 2:
            raise ServiceValidationError("Need at least two spots to evaluate")
        scanner_room = {}
        for scanner in self.coordinator.get_scanners:
            if scanner.area_id:
                key = next((k for k, v in self.rooms.items() if v.get("area_id") == scanner.area_id), scanner.area_id)
                scanner_room[scanner.address] = key
        spots = list(self.spots)
        offsets = dict(self.coordinator.options.get(CONF_RSSI_OFFSETS, {}))
        # Defaults: the configured matcher; service fields override for experiments.
        kwargs = dict(self.matcher_kwargs)
        for key in ("method", "offset_max", "outlier_db", "temperature"):
            if key in call.data:
                kwargs[key] = call.data[key]
        if call.data.get("use_offset") is False:
            kwargs["offset_max"] = 0.0
        result = await self.hass.async_add_executor_job(
            lambda: evaluate(
                spots,
                scanner_room=scanner_room,
                rssi_offsets=offsets,
                min_confidence=self.min_confidence,
                **kwargs,
            )
        )
        result["matcher"] = kwargs
        names = {k: self.room_name(k) for k in self.rooms}
        result["room_names"] = names
        if not call.data["details"]:
            result.pop("per_spot", None)
        return result

    async def _svc_map_room(self, call: ServiceCall) -> None:
        room = self._resolve_room({"room": call.data["room"]}, create=False)
        area = self.coordinator.ar.async_get_area(call.data["area_id"])
        if area is None:
            raise ServiceValidationError(f"Unknown area_id '{call.data['area_id']}'")
        if room == area.id:
            return
        self.rooms[area.id] = {"area_id": area.id, "name": area.name}
        for spot in self.spots:
            if spot["room"] == room:
                spot["room"] = area.id
        self._spots_changed()

    # ------------------------------------------------------------------ per-cycle
    @callback
    def update(self, nowstamp: float) -> None:
        """Called by the coordinator every cycle, after Bermuda's own area selection."""
        if not self.loaded:
            return
        if self.recording is not None:
            self._feed_recording(nowstamp)
        if not self.enabled:
            return
        active = {s.address for s in self.coordinator.get_scanners if nowstamp - s.last_seen < SCANNER_ACTIVE_S}
        for device in self.coordinator.devices.values():
            if device.create_sensor:
                self._update_device(device, nowstamp, active)

    def _feed_recording(self, nowstamp: float) -> None:
        rec = self.recording
        device = self.coordinator.devices.get(rec.device_address)
        if device is not None:
            for advert in device.adverts.values():
                key = (advert.device_address, advert.scanner_address)
                last = rec.last_taken.get(key, 0.0)
                for stamp, rssi in zip(advert.hist_stamp, advert.hist_rssi, strict=False):
                    if not stamp or stamp <= last:
                        break
                    rec.recorder.add(advert.scanner_address, stamp, rssi)
                if advert.hist_stamp and advert.hist_stamp[0]:
                    rec.last_taken[key] = max(last, advert.hist_stamp[0])
        rec.recorder.finish_gap(nowstamp)
        if nowstamp >= rec.recorder.end:
            self._finish_recording()
        else:
            self._update_status(nowstamp)

    def _update_status(self, nowstamp: float) -> None:
        rec = self.recording
        per_scanner = rec.recorder.samples_per_scanner()
        left = max(0, round(rec.recorder.end - nowstamp))
        self.status = {
            "state": "recording",
            "area_id": self.room_area_id(rec.room),
            "room": rec.room,
            "room_name": self.room_name(rec.room),
            "label": rec.label,
            "spot_id": rec.spot_id,
            "device": rec.device_address,
            "duration": int(rec.recorder.duration),
            "seconds_left": left,
            "progress": round(100 * (1 - left / rec.recorder.duration)),
            "samples": rec.recorder.sample_count,
            "scanners_heard": len(per_scanner),
            "per_scanner": {
                self._scanner_name(s): n for s, n in sorted(per_scanner.items(), key=lambda kv: -kv[1])
            },
            "message": f"Recording {self.room_name(rec.room)} {rec.label}: {left} s left",
            "warnings": [],
        }
        self.status_version += 1

    def _finish_recording(self, cancelled: bool = False) -> None:
        rec = self.recording
        self.recording = None
        room_name = self.room_name(rec.room)
        built = rec.recorder.build()
        warnings = []
        heard = len(built["scanners"])
        if heard < WARN_MIN_SCANNERS:
            warnings.append(f"only {heard} scanners heard the device")
        if rec.recorder.max_gap > WARN_MAX_GAP_S:
            warnings.append(f"device not seen for {round(rec.recorder.max_gap)} s")
        state, spot_id = "done", rec.spot_id
        if cancelled:
            state, message = "cancelled", f"Recording {room_name} {rec.label} cancelled"
        elif built["samples"] == 0:
            state, message = "failed", f"Recording {room_name} {rec.label} failed: no readings from the device"
        else:
            stamp = dt_now().isoformat(timespec="seconds")
            spot = {
                "id": rec.spot_id or uuid.uuid4().hex[:12],
                "label": rec.label,
                "room": rec.room,
                "created": stamp,
                "updated": stamp,
                "recorded_with": {"device": rec.device_address, "carry": rec.carry},
                **built,
            }
            if rec.spot_id:
                for i, old in enumerate(self.spots):
                    if old["id"] == rec.spot_id:
                        spot["created"] = old.get("created", stamp)
                        self.spots[i] = spot
                        break
                else:
                    self.spots.append(spot)
            else:
                self.spots.append(spot)
            spot_id = spot["id"]
            self._spots_changed()
            message = f"Recorded {room_name} {rec.label}: {built['samples']} samples, {heard} scanners"
            if warnings:
                message += " (" + "; ".join(warnings) + ")"
        result = {
            "state": state,
            "area_id": self.room_area_id(rec.room),
            "room": rec.room,
            "room_name": room_name,
            "label": rec.label,
            "spot_id": spot_id,
            "device": rec.device_address,
            "duration": int(rec.recorder.duration),
            "seconds_left": 0,
            "progress": 100 if state == "done" else 0,
            "samples": built["samples"],
            "scanners_heard": heard,
            "per_scanner": {
                self._scanner_name(s): st["n"]
                for s, st in sorted(built["scanners"].items(), key=lambda kv: -kv[1]["n"])
            },
            "message": message,
            "warnings": warnings,
        }
        self.status = result
        self.status_version += 1
        _LOGGER.info("Fingerprint: %s", message)
        self.hass.bus.async_fire(
            EVENT_RECORDED, {k: v for k, v in result.items() if k not in ("per_scanner", "seconds_left")}
        )
        for fut in rec.waiters:
            if not fut.done():
                fut.set_result(result)

    def _live_readings(self, device: BermudaDevice, nowstamp: float) -> dict[str, list[tuple[float, float]]]:
        cutoff = nowstamp - LIVE_WINDOW_S
        readings: dict[str, list[tuple[float, float]]] = {}
        for advert in device.adverts.values():
            for stamp, rssi in zip(advert.hist_stamp, advert.hist_rssi, strict=False):
                if not stamp or stamp < cutoff:
                    break
                readings.setdefault(advert.scanner_address, []).append((stamp, rssi))
        return readings

    def _update_device(self, device: BermudaDevice, nowstamp: float, active: set[str]) -> None:
        smoother = self._smoothers.get(device.address)
        if smoother is None:
            smoother = self._smoothers[device.address] = RoomSmoother(min_confidence=self.min_confidence)
        detail: dict[str, Any] = {}
        match = None
        if self.matcher is None:
            smoothed = None
            reason = REASON_NO_FP
        else:
            if nowstamp - device.last_seen <= DEVICE_UNSEEN_S:
                live = live_vector(self._live_readings(device, nowstamp), nowstamp)
                match = self.matcher.match(live, active)
                detail["heard_scanners"] = len(live)
            if match is None:
                smoothed = smoother.update(None, nowstamp)
            elif match.outlier:  # far from every spot: probably a room without spots
                smoothed = smoother.update(None, nowstamp, reason=REASON_OUTLIER)
            else:
                smoothed = smoother.update(match.posterior, nowstamp)
            reason = smoothed.reason

        if match is not None:
            detail["best"] = {
                "room": self.room_name(match.best.room),
                "spot": match.best.spot_id,
                "p": round(match.posterior[match.best.room], 2),
            }
            if match.second is not None:
                detail["second"] = {
                    "room": self.room_name(match.second.room),
                    "spot": match.second.spot_id,
                    "p": round(match.posterior[match.second.room], 2),
                }
                detail["margin"] = round(detail["best"]["p"] - detail["second"]["p"], 2)
            detail["common_scanners"] = match.best.common
            detail["offset_db"] = round(match.best.offset, 1)
            detail["distance_db"] = round(match.distance, 1)
            detail["outlier"] = match.outlier
        if smoothed is not None:
            detail["holding"] = smoothed.holding
            detail["smoothed_room"] = self.room_name(smoothed.incumbent)

        # Bermuda's own nearest-proxy result (before any override this cycle)
        nearest_area_id, nearest_name = device.area_id, device.area_name
        detail["nearest_area"] = nearest_name

        state = FingerprintState(reason=reason, detail=detail)
        # No fingerprint evidence this cycle (outlier / no data): confidence 0, not a stale EMA.
        state.confidence = (
            smoothed.confidence if smoothed is not None and reason not in (REASON_OUTLIER, REASON_NO_DATA) else 0.0
        )
        if smoothed is not None and smoothed.room is not None:
            state.room = smoothed.room
            state.name = self.room_name(smoothed.room)
            state.area_id = self.room_area_id(smoothed.room)
            state.source = "fingerprint"
        else:
            state.name, state.area_id = nearest_name, nearest_area_id
        self.states[device.address] = state

        if (
            self.mode == FINGERPRINT_MODE_OVERRIDE
            and reason == REASON_MATCH
            and state.area_id is not None
            and state.area_id != device.area_id
        ):
            device._update_area_and_floor(state.area_id)  # noqa: SLF001
            device.area_last_seen = device.area_name
            device.area_last_seen_id = device.area_id
            device.area_last_seen_icon = device.area_icon
