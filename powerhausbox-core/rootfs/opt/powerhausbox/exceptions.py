from typing import Any


class PairingAPIError(Exception):
    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        payload: dict[str, Any] | None = None,
        response_headers: dict[str, str] | None = None,
        response_body: str = "",
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.payload = payload or {}
        self.response_headers = response_headers or {}
        self.response_body = response_body


class AuthStorageError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class SupervisorAPIError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class StudioSyncError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class IframeConfiguratorError(Exception):
    """Raised for iframe configurator errors."""
