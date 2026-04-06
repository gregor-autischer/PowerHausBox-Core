"""Helper to discover the PowerHausBox add-on URL via Supervisor API."""

from __future__ import annotations

import logging
import os

import aiohttp

from .const import ADDON_PORT

_LOGGER = logging.getLogger(__name__)

# The Supervisor API is available inside HA Core at this URL
_SUPERVISOR_URL = "http://supervisor"
_SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")

# Add-on slug suffix (the part after the repo hash)
_ADDON_SLUG_SUFFIX = "powerhausbox_cloudflare_tunnel"


async def get_addon_api_url() -> str:
    """Discover the add-on's internal API URL via the Supervisor API.

    The Supervisor assigns each add-on a Docker hostname based on its
    full slug (repo_hash + addon_slug). We query the Supervisor to find
    all installed add-ons and match by slug suffix.

    Falls back to localhost if discovery fails (e.g., during development).
    """
    cached = _get_cached_url()
    if cached:
        return cached

    if not _SUPERVISOR_TOKEN:
        _LOGGER.debug("No SUPERVISOR_TOKEN; using localhost fallback")
        return f"http://localhost:{ADDON_PORT}"

    try:
        headers = {
            "Authorization": f"Bearer {_SUPERVISOR_TOKEN}",
            "Content-Type": "application/json",
        }
        async with aiohttp.ClientSession() as session:
            # Try to find the add-on by listing all installed add-ons
            async with session.get(
                f"{_SUPERVISOR_URL}/addons",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    _LOGGER.warning("Supervisor /addons returned %s", resp.status)
                    return f"http://localhost:{ADDON_PORT}"

                data = await resp.json()
                addons = data.get("data", {}).get("addons", [])

                for addon in addons:
                    slug = addon.get("slug", "")
                    if slug.endswith(_ADDON_SLUG_SUFFIX):
                        # The Docker hostname is the slug with underscores replaced by hyphens
                        hostname = slug.replace("_", "-")
                        url = f"http://{hostname}:{ADDON_PORT}"
                        _set_cached_url(url)
                        _LOGGER.debug("Discovered add-on URL: %s", url)
                        return url

                _LOGGER.warning("PowerHausBox add-on not found in installed add-ons")
                return f"http://localhost:{ADDON_PORT}"

    except Exception:
        _LOGGER.warning("Failed to discover add-on URL via Supervisor", exc_info=True)
        return f"http://localhost:{ADDON_PORT}"


# Simple cache to avoid repeated Supervisor API calls
_cached_addon_url: str = ""


def _get_cached_url() -> str:
    return _cached_addon_url


def _set_cached_url(url: str) -> None:
    global _cached_addon_url
    _cached_addon_url = url
