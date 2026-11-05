"""Constants for Bermuda BLE Trilateration tests."""

from __future__ import annotations

import custom_components.bermuda.const

# from custom_components.bermuda.const import CONF_DEVICES
# from custom_components.bermuda.const import CONF_MAX_RADIUS


MOCK_OPTIONS = {
    custom_components.bermuda.const.CONF_MAX_RADIUS: 20.0,
    custom_components.bermuda.const.CONF_MAX_VELOCITY: 3.0,
    custom_components.bermuda.const.CONF_DEVTRACK_TIMEOUT: 30,
    custom_components.bermuda.const.CONF_UPDATE_INTERVAL: 10.0,
    custom_components.bermuda.const.CONF_SMOOTHING_SAMPLES: 20,
    custom_components.bermuda.const.CONF_ATTENUATION: 3.0,
    custom_components.bermuda.const.CONF_REF_POWER: -55.0,
    custom_components.bermuda.const.CONF_DEVICES: [],  # ["EE:E8:37:9F:6B:54"],
}

MOCK_OPTIONS_GLOBALS = {
    custom_components.bermuda.const.CONF_MAX_RADIUS: 20.0,
    custom_components.bermuda.const.CONF_MAX_VELOCITY: 3.0,
    custom_components.bermuda.const.CONF_DEVTRACK_TIMEOUT: 30,
    custom_components.bermuda.const.CONF_UPDATE_INTERVAL: 10.0,
    custom_components.bermuda.const.CONF_SMOOTHING_SAMPLES: 20,
    custom_components.bermuda.const.CONF_ATTENUATION: 3.0,
    custom_components.bermuda.const.CONF_REF_POWER: -55.0,
    # TAW-V fingerprint options (all present in the global options form)
    custom_components.bermuda.const.CONF_FINGERPRINT_MODE: "shadow",
    custom_components.bermuda.const.CONF_FP_METHOD: "knn",
    custom_components.bermuda.const.CONF_FP_OFFSET_MAX: 0.0,
    custom_components.bermuda.const.CONF_FP_OUTLIER_DB: 10.0,
    custom_components.bermuda.const.CONF_FP_MIN_CONFIDENCE: 0.5,
    custom_components.bermuda.const.CONF_FP_PRESET: "custom",
    custom_components.bermuda.const.CONF_FP_LIVE_WINDOW: 10.0,
    custom_components.bermuda.const.CONF_FP_TAU: 4.0,
    custom_components.bermuda.const.CONF_FP_CONFIRM_CYCLES: 2,
    custom_components.bermuda.const.CONF_FP_MIN_DWELL: 3.0,
}

MOCK_OPTIONS_DEVICES = {
    custom_components.bermuda.const.CONF_DEVICES: [],  # ["EE:E8:37:9F:6B:54"],
}

MOCK_CONFIG = {"source": "user"}


SERVICE_INFOS = [
    {
        "name": "test device",
        "advertisement": {"local_name": "test local name"},
        "device": {"name": "test device name"},
        "address": "EE:E8:37:9F:6B:54",
    },
    {
        "name": "test device2",
        "advertisement": {"local_name": "test local name2"},
        "device": {"name": "test device name2"},
        "address": "EE:E8:37:9F:6B:56",
    },
]
