"""Backup platform for the PowerHaus integration."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Callable, Coroutine
from typing import Any

import aiohttp

from homeassistant.components.backup import (
    AgentBackup,
    BackupAgent,
    BackupAgentError,
    BackupNotFound,
    OnProgressCallback,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from . import DATA_BACKUP_AGENT_LISTENERS, PowerHausConfigEntry
from .const import ADDON_API_URL, BACKUP_STREAM_CHUNK_SIZE, DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_get_backup_agents(
    hass: HomeAssistant,
    **kwargs: Any,
) -> list[BackupAgent]:
    """Return the PowerHaus backup agent."""
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    return [PowerHausBackupAgent(hass=hass, entry=entry) for entry in entries]


@callback
def async_register_backup_agents_listener(
    hass: HomeAssistant,
    *,
    listener: Callable[[], None],
    **kwargs: Any,
) -> Callable[[], None]:
    """Register a listener to be called when agents are added or removed."""
    hass.data.setdefault(DATA_BACKUP_AGENT_LISTENERS, []).append(listener)

    @callback
    def remove_listener() -> None:
        hass.data[DATA_BACKUP_AGENT_LISTENERS].remove(listener)
        if not hass.data[DATA_BACKUP_AGENT_LISTENERS]:
            del hass.data[DATA_BACKUP_AGENT_LISTENERS]

    return remove_listener


class PowerHausBackupAgent(BackupAgent):
    """PowerHaus Cloud backup agent."""

    domain = DOMAIN

    def __init__(self, hass: HomeAssistant, entry: PowerHausConfigEntry) -> None:
        """Initialize the PowerHaus backup agent."""
        super().__init__()
        self.name = "PowerHaus Cloud"
        self.unique_id = DOMAIN
        self._hass = hass

    def _session(self) -> aiohttp.ClientSession:
        """Get aiohttp client session."""
        return async_get_clientsession(self._hass)

    async def async_upload_backup(
        self,
        *,
        open_stream: Callable[[], Coroutine[Any, Any, AsyncIterator[bytes]]],
        backup: AgentBackup,
        on_progress: OnProgressCallback,
        **kwargs: Any,
    ) -> None:
        """Upload a backup to PowerHaus Cloud via the add-on.

        The add-on proxies the upload to the Studio server through the
        Cloudflare tunnel.
        """
        stream = await open_stream()

        async def _progress_stream() -> AsyncIterator[bytes]:
            bytes_uploaded = 0
            async for chunk in stream:
                yield chunk
                bytes_uploaded += len(chunk)
                on_progress(bytes_uploaded=bytes_uploaded)

        # Build multipart: metadata JSON + backup file stream
        metadata = json.dumps(backup.as_dict(), default=str)

        with aiohttp.MultipartWriter("form-data") as mpwriter:
            # Part 1: JSON metadata
            meta_part = mpwriter.append(metadata)
            meta_part.set_content_disposition("form-data", name="metadata")
            meta_part.headers[aiohttp.hdrs.CONTENT_TYPE] = "application/json"

            # Part 2: Backup file stream
            file_part = mpwriter.append(_progress_stream())
            file_part.set_content_disposition(
                "form-data", name="backup_file", filename=f"{backup.backup_id}.tar"
            )
            file_part.headers[aiohttp.hdrs.CONTENT_TYPE] = "application/octet-stream"

            try:
                async with self._session().post(
                    f"{ADDON_API_URL}/api/backup/upload",
                    data=mpwriter,
                    timeout=aiohttp.ClientTimeout(total=7200),  # 2h for large backups
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        raise BackupAgentError(
                            f"Upload failed ({resp.status}): {body}"
                        )
            except aiohttp.ClientError as err:
                raise BackupAgentError(
                    f"Failed to upload backup to PowerHaus: {err}"
                ) from err

    async def async_download_backup(
        self,
        backup_id: str,
        **kwargs: Any,
    ) -> AsyncIterator[bytes]:
        """Download a backup from PowerHaus Cloud via the add-on."""
        try:
            resp = await self._session().get(
                f"{ADDON_API_URL}/api/backup/download/{backup_id}",
                timeout=aiohttp.ClientTimeout(total=7200),
            )
            if resp.status == 404:
                raise BackupNotFound(f"Backup {backup_id} not found on PowerHaus Cloud")
            if resp.status != 200:
                body = await resp.text()
                raise BackupAgentError(
                    f"Download failed ({resp.status}): {body}"
                )
        except aiohttp.ClientError as err:
            raise BackupAgentError(
                f"Failed to download backup from PowerHaus: {err}"
            ) from err

        return _response_stream(resp)

    async def async_delete_backup(
        self,
        backup_id: str,
        **kwargs: Any,
    ) -> None:
        """Delete a backup from PowerHaus Cloud."""
        try:
            async with self._session().delete(
                f"{ADDON_API_URL}/api/backup/{backup_id}",
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status == 404:
                    raise BackupNotFound(
                        f"Backup {backup_id} not found on PowerHaus Cloud"
                    )
                if resp.status != 200:
                    body = await resp.text()
                    raise BackupAgentError(
                        f"Delete failed ({resp.status}): {body}"
                    )
        except aiohttp.ClientError as err:
            raise BackupAgentError(
                f"Failed to delete backup from PowerHaus: {err}"
            ) from err

    async def async_list_backups(self, **kwargs: Any) -> list[AgentBackup]:
        """List backups stored on PowerHaus Cloud."""
        try:
            async with self._session().get(
                f"{ADDON_API_URL}/api/backup/list",
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise BackupAgentError(
                        f"List failed ({resp.status}): {body}"
                    )
                data = await resp.json()
        except aiohttp.ClientError as err:
            raise BackupAgentError(
                f"Failed to list backups from PowerHaus: {err}"
            ) from err

        return [AgentBackup.from_dict(b) for b in data.get("backups", [])]

    async def async_get_backup(
        self,
        backup_id: str,
        **kwargs: Any,
    ) -> AgentBackup:
        """Return a specific backup's metadata."""
        try:
            async with self._session().get(
                f"{ADDON_API_URL}/api/backup/{backup_id}",
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status == 404:
                    raise BackupNotFound(
                        f"Backup {backup_id} not found on PowerHaus Cloud"
                    )
                if resp.status != 200:
                    body = await resp.text()
                    raise BackupAgentError(
                        f"Get backup failed ({resp.status}): {body}"
                    )
                data = await resp.json()
        except aiohttp.ClientError as err:
            raise BackupAgentError(
                f"Failed to get backup from PowerHaus: {err}"
            ) from err

        return AgentBackup.from_dict(data)


async def _response_stream(resp: aiohttp.ClientResponse) -> AsyncIterator[bytes]:
    """Yield chunks from an aiohttp response."""
    async for chunk in resp.content.iter_chunked(BACKUP_STREAM_CHUNK_SIZE):
        yield chunk
