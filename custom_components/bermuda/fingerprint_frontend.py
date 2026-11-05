"""Serve the bundled Lovelace card (custom:bermuda-fingerprint-card) from the integration (TAW-V fork)."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .const import _LOGGER, DOMAIN

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

URL_BASE = f"/{DOMAIN}_frontend"
CARD_FILE = "bermuda-fingerprint-card.js"
CARD_PATH = f"{URL_BASE}/{CARD_FILE}"
_DATA_KEY = f"{DOMAIN}_frontend_registered"
_FRONTEND_DIR = Path(__file__).parent / "frontend"


def _file_digest(path: Path) -> str:
    return hashlib.sha1(path.read_bytes(), usedforsecurity=False).hexdigest()[:10]


async def _async_ensure_lovelace_resource(hass: HomeAssistant, url: str) -> bool:
    """
    Keep one Lovelace module resource for the card, pointing at the current ?v= hash.

    Lovelace resources load after the websocket is connected. add_extra_js_url modules are imported
    while the page boots; when that import fails (the Android app reloading while the phone wakes up,
    before the network is back) the browser never retries it and the card stays a "Configuration error".
    Returns False when resources are YAML-managed or unavailable, so the caller can fall back.
    """
    lovelace: Any = hass.data.get("lovelace")
    resources = getattr(lovelace, "resources", None)
    if getattr(lovelace, "resource_mode", None) != "storage" or not hasattr(resources, "async_create_item"):
        return False
    await resources.async_get_info()  # loads the collection from storage
    ours = [item for item in resources.async_items() if str(item.get("url", "")).split("?")[0] == CARD_PATH]
    if not ours:
        await resources.async_create_item({"res_type": "module", "url": url})
    elif ours[0].get("url") != url or ours[0].get("type") != "module":
        await resources.async_update_item(ours[0]["id"], {"res_type": "module", "url": url})
    return True


async def async_register_frontend(hass: HomeAssistant) -> None:
    """Register the static path and auto-load the card once per HA run."""
    if hass.data.get(_DATA_KEY) or getattr(hass, "http", None) is None:
        return
    hass.data[_DATA_KEY] = True
    try:
        from homeassistant.components.http import StaticPathConfig  # noqa: PLC0415

        digest = await hass.async_add_executor_job(_file_digest, _FRONTEND_DIR / CARD_FILE)
        await hass.http.async_register_static_paths([StaticPathConfig(URL_BASE, str(_FRONTEND_DIR), True)])
        # The ?v= digest changes with the file, so browsers fetch a new card after an update.
        url = f"{CARD_PATH}?v={digest}"
        try:
            if await _async_ensure_lovelace_resource(hass, url):
                return
        except Exception:  # noqa: BLE001
            _LOGGER.warning("Could not manage the Lovelace resource for the fingerprint card", exc_info=True)
        # YAML-managed resources: fall back to loading the card on every page.
        from homeassistant.components.frontend import add_extra_js_url  # noqa: PLC0415

        add_extra_js_url(hass, url)
    except Exception:  # noqa: BLE001 - the card is optional, never block Bermuda setup
        _LOGGER.exception("Could not register the Bermuda fingerprint card")
