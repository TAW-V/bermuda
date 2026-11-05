"""Sensor entities for Bermuda RSSI fingerprinting (TAW-V fork)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from bluetooth_data_tools import monotonic_time_coarse
from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.core import Event, callback
from homeassistant.helpers.area_registry import EVENT_AREA_REGISTRY_UPDATED

from .entity import BermudaEntity, BermudaGlobalEntity

if TYPE_CHECKING:
    from . import BermudaConfigEntry
    from .coordinator import BermudaDataUpdateCoordinator
    from .fingerprint import FingerprintManager

# Minimum seconds between state writes, to keep the recorder small.
ROOM_MIN_INTERVAL = 5  # reason/source changes
CONF_MIN_INTERVAL = 30  # confidence bucket changes
STATUS_MIN_INTERVAL = 2  # progress updates while recording


def fingerprint_global_entities(coordinator: BermudaDataUpdateCoordinator, entry: BermudaConfigEntry) -> list:
    """Global entities: recording status and spot overview."""
    return [BermudaFingerprintStatus(coordinator, entry), BermudaFingerprintSpots(coordinator, entry)]


def fingerprint_device_entities(
    coordinator: BermudaDataUpdateCoordinator, entry: BermudaConfigEntry, address: str
) -> list:
    """Per tracked device entities (none when fingerprinting is off)."""
    if not coordinator.fingerprints.enabled:
        return []
    return [
        BermudaSensorFingerprintRoom(coordinator, entry, address),
        BermudaSensorFingerprintConfidence(coordinator, entry, address),
    ]


class _FingerprintGlobalSensor(BermudaGlobalEntity, SensorEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, coordinator: BermudaDataUpdateCoordinator, config_entry: BermudaConfigEntry) -> None:
        super().__init__(coordinator, config_entry)
        self._last_write: float = 0

    @property
    def fp(self) -> FingerprintManager:
        return self.coordinator.fingerprints


class BermudaFingerprintStatus(_FingerprintGlobalSensor):
    """Recording status and live progress."""

    _attr_name = "Fingerprint status"
    _attr_unique_id = "BERMUDA_GLOBAL_FINGERPRINT_STATUS"
    _attr_icon = "mdi:fingerprint"
    _unrecorded_attributes = frozenset(
        {"room", "spot_id", "device", "duration", "seconds_left", "progress", "samples", "scanners_heard",
         "per_scanner", "warnings"}  # fmt: skip
    )

    def __init__(self, coordinator: BermudaDataUpdateCoordinator, config_entry: BermudaConfigEntry) -> None:
        super().__init__(coordinator, config_entry)
        self._version = -1
        self._state: str | None = None

    @property
    def native_value(self) -> str:
        return self.fp.status.get("state", "idle")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {k: v for k, v in self.fp.status.items() if k != "state"}

    @callback
    def _handle_coordinator_update(self) -> None:
        if self.fp.status_version == self._version:
            return
        nowstamp = monotonic_time_coarse()
        state = self.fp.status.get("state")
        if state != self._state or nowstamp - self._last_write >= STATUS_MIN_INTERVAL:
            self._version = self.fp.status_version
            self._state = state
            self._last_write = nowstamp
            self.async_write_ha_state()


class BermudaFingerprintSpots(_FingerprintGlobalSensor):
    """Number of recorded spots, with a per-room overview for dashboards."""

    _attr_name = "Fingerprint spots"
    _attr_unique_id = "BERMUDA_GLOBAL_FINGERPRINT_SPOTS"
    _attr_icon = "mdi:map-marker-multiple"
    _unrecorded_attributes = frozenset({"areas"})

    def __init__(self, coordinator: BermudaDataUpdateCoordinator, config_entry: BermudaConfigEntry) -> None:
        super().__init__(coordinator, config_entry)
        self._version = -1

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        @callback
        def _areas_changed(_event: Event) -> None:
            self.async_write_ha_state()

        self.async_on_remove(self.hass.bus.async_listen(EVENT_AREA_REGISTRY_UPDATED, _areas_changed))

    @property
    def native_value(self) -> int:
        return len(self.fp.spots)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        areas = self.fp.areas_overview()
        return {
            "mode": self.fp.mode,
            "rooms_with_spots": sum(1 for a in areas.values() if a["count"]),
            "areas": areas,
        }

    @callback
    def _handle_coordinator_update(self) -> None:
        if self.fp.spots_version != self._version:
            self._version = self.fp.spots_version
            self.async_write_ha_state()


class _FingerprintDeviceSensor(BermudaEntity, SensorEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, coordinator: BermudaDataUpdateCoordinator, config_entry: BermudaConfigEntry, address: str):
        super().__init__(coordinator, config_entry, address)
        self._last_write: float = 0
        self._written: tuple | None = None

    @property
    def fp_state(self):
        return self.coordinator.fingerprints.states.get(self.address)


class BermudaSensorFingerprintRoom(_FingerprintDeviceSensor):
    """Room from RSSI fingerprinting, falling back to Bermuda's own area."""

    _attr_name = "Fingerprint room"
    _attr_icon = "mdi:fingerprint"
    _unrecorded_attributes = frozenset(
        {"best", "second", "margin", "offset_db", "common_scanners", "heard_scanners", "nearest_area", "holding",
         "smoothed_room", "recording"}  # fmt: skip
    )

    @property
    def unique_id(self):
        return f"{self._device.unique_id}_fingerprint_room"

    @property
    def native_value(self) -> str | None:
        st = self.fp_state
        return st.name if st is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        st = self.fp_state
        if st is None:
            return {}
        attrs = {
            "area_id": st.area_id,
            "room": st.room,
            "confidence": round(st.confidence, 1),
            "reason": st.reason,
            "source": st.source,
            "mode": self.coordinator.fingerprints.mode,
        }
        attrs.update(st.detail)
        rec = self.coordinator.fingerprints.recording
        attrs["recording"] = rec is not None and rec.device_address == self.address
        return attrs

    @callback
    def _handle_coordinator_update(self) -> None:
        st = self.fp_state
        if st is None:
            return
        nowstamp = monotonic_time_coarse()
        key = (st.name, st.area_id, st.room, st.reason, st.source, round(st.confidence, 1))
        old = self._written
        since = nowstamp - self._last_write
        if (
            old is None
            or key[:3] != old[:3]
            or (key[3:5] != old[3:5] and since >= ROOM_MIN_INTERVAL)
            or (key[5] != old[5] and since >= CONF_MIN_INTERVAL)
        ):
            self._written = key
            self._last_write = nowstamp
            self.async_write_ha_state()


class BermudaSensorFingerprintConfidence(_FingerprintDeviceSensor):
    """Smoothed confidence of the fingerprint room (disabled by default)."""

    _attr_name = "Fingerprint confidence"
    _attr_icon = "mdi:percent-circle-outline"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    @property
    def unique_id(self):
        return f"{self._device.unique_id}_fingerprint_confidence"

    @property
    def native_value(self) -> int | None:
        st = self.fp_state
        return None if st is None else int(round(st.confidence * 20) * 5)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        return None

    @callback
    def _handle_coordinator_update(self) -> None:
        value = self.native_value
        if value is None:
            return
        nowstamp = monotonic_time_coarse()
        old = self._written[0] if self._written else None
        if old is None or (
            value != old and (abs(value - old) >= 20 or nowstamp - self._last_write >= CONF_MIN_INTERVAL)
        ):
            self._written = (value,)
            self._last_write = nowstamp
            self.async_write_ha_state()
