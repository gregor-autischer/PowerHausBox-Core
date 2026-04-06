"""Helper to discover the PowerHausBox add-on URL via Supervisor API."""

from __future__ import annotations

import logging
import os

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import ADDON_PORT

_LOGGER = logging.getLogger(__name__)

# The Supervisor API is available inside HA Core at this URL
_SUPERVISOR_URL = "http://supervisor"
_SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")

# Add-on slug suffix (the part after the repo hash)
_ADDON_SLUG_SUFFIX = "powerhausbox_core"

# Cache (populated once per HA session)
_cached_addon_url: str = ""


async def get_addon_api_url(hass: HomeAssistant | None = None) -> str:
    """Discover the add-on's internal API URL via the Supervisor API.

    The Supervisor assigns each add-on a Docker hostname based on its
    full slug (repo_hash + addon_slug). We query the Supervisor to find
    all installed add-ons and match by slug suffix.

    Falls back to localhost if discovery fails (e.g., during development).
    """
    global _cached_addon_url
    if _cached_addon_url:
        return _cached_addon_url

    if not _SUPERVISOR_TOKEN:
        _LOGGER.debug("No SUPERVISOR_TOKEN; using localhost fallback")
        return f"http://localhost:{ADDON_PORT}"

    try:
        headers = {
            "Authorization": f"Bearer {_SUPERVISOR_TOKEN}",
            "Content-Type": "application/json",
        }

        if hass is not None:
            session = async_get_clientsession(hass)
        else:
            # Fallback: import aiohttp only when needed (no hass available)
            import aiohttp
            session = aiohttp.ClientSession()

        try:
            async with session.get(
                f"{_SUPERVISOR_URL}/addons",
                headers=headers,
                timeout=10,
            ) as resp:
                if resp.status != 200:
                    _LOGGER.warning("Supervisor /addons returned %s", resp.status)
                    return f"http://localhost:{ADDON_PORT}"

                data = await resp.json()
                addons = data.get("data", {}).get("addons", [])

                for addon in addons:
                    slug = addon.get("slug", "")
                    if slug.endswith(_ADDON_SLUG_SUFFIX):
                        hostname = slug.replace("_", "-")
                        url = f"http://{hostname}:{ADDON_PORT}"
                        _cached_addon_url = url
                        _LOGGER.debug("Discovered add-on URL: %s", url)
                        return url

                _LOGGER.warning("PowerHausBox add-on not found in installed add-ons")
                return f"http://localhost:{ADDON_PORT}"
        finally:
            # Close standalone session if we created one (not HA's shared session)
            if hass is None and hasattr(session, "close"):
                await session.close()

    except Exception:
        _LOGGER.warning("Failed to discover add-on URL via Supervisor", exc_info=True)
        return f"http://localhost:{ADDON_PORT}"
