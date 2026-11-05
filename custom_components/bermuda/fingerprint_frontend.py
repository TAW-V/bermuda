"""Serve the bundled Lovelace card (custom:bermuda-fingerprint-card) from the integration (TAW-V fork)."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING

from .const import _LOGGER, DOMAIN

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

URL_BASE = f"/{DOMAIN}_frontend"
CARD_FILE = "bermuda-fingerprint-card.js"
_DATA_KEY = f"{DOMAIN}_frontend_registered"
_FRONTEND_DIR = Path(__file__).parent / "frontend"


def _file_digest(path: Path) -> str:
    return hashlib.sha1(path.read_bytes(), usedforsecurity=False).hexdigest()[:10]


async def async_register_frontend(hass: HomeAssistant) -> None:
    """Register the static path and auto-load the card once per HA run."""
    if hass.data.get(_DATA_KEY) or getattr(hass, "http", None) is None:
        return
    hass.data[_DATA_KEY] = True
    try:
        from homeassistant.components.frontend import add_extra_js_url  # noqa: PLC0415
        from homeassistant.components.http import StaticPathConfig  # noqa: PLC0415

        digest = await hass.async_add_executor_job(_file_digest, _FRONTEND_DIR / CARD_FILE)
        await hass.http.async_register_static_paths([StaticPathConfig(URL_BASE, str(_FRONTEND_DIR), True)])
        # The ?v= digest changes with the file, so browsers fetch a new card after an update.
        add_extra_js_url(hass, f"{URL_BASE}/{CARD_FILE}?v={digest}")
    except Exception:  # noqa: BLE001 - the card is optional, never block Bermuda setup
        _LOGGER.exception("Could not register the Bermuda fingerprint card")
