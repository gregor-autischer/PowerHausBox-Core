"""The PowerHaus integration."""

from __future__ import annotations

from collections.abc import Callable

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.util.hass_dict import HassKey

from .const import DOMAIN, INTEGRATION_NAME

type PowerHausConfigEntry = ConfigEntry

DATA_BACKUP_AGENT_LISTENERS: HassKey[list[Callable[[], None]]] = HassKey(
    f"{DOMAIN}.backup_agent_listeners"
)


async def async_setup_entry(
    hass: HomeAssistant, entry: PowerHausConfigEntry
) -> bool:
    """Set up PowerHaus from a config entry."""

    if entry.title != INTEGRATION_NAME:
        hass.config_entries.async_update_entry(entry, title=INTEGRATION_NAME)

    def async_notify_backup_listeners() -> None:
        for listener in hass.data.get(DATA_BACKUP_AGENT_LISTENERS, []):
            listener()

    entry.async_on_unload(entry.async_on_state_change(async_notify_backup_listeners))

    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: PowerHausConfigEntry
) -> bool:
    """Unload a PowerHaus config entry."""
    return True
