"""Config flow for the PowerHaus integration."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import ADDON_API_URL, ADDON_HEALTH_PATH, DOMAIN

_LOGGER = logging.getLogger(__name__)


class PowerHausConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for PowerHaus."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step.

        Checks if the PowerHaus add-on is reachable, then creates the entry.
        """
        errors: dict[str, str] = {}

        if user_input is not None:
            # Check if already configured
            await self.async_set_unique_id(DOMAIN)
            self._abort_if_unique_id_configured()

            # Verify the add-on is reachable
            session = async_get_clientsession(self.hass)
            try:
                async with session.get(
                    f"{ADDON_API_URL}{ADDON_HEALTH_PATH}", timeout=aiohttp.ClientTimeout(total=5)
                ) as resp:
                    if resp.status == 200:
                        return self.async_create_entry(
                            title="PowerHaus Cloud",
                            data={},
                        )
                    errors["base"] = "cannot_connect"
            except (aiohttp.ClientError, TimeoutError):
                errors["base"] = "cannot_connect"

        return self.async_show_form(
            step_id="user",
            errors=errors,
            description_placeholders={
                "addon_url": ADDON_API_URL,
            },
        )
