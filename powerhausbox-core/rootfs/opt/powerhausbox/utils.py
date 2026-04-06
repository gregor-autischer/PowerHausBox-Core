"""Shared utilities for the PowerHausBox add-on.

Functions in this module are used by both server.py and iframe_configurator.py.
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


CONTAINER_ENV_DIR = Path("/run/s6/container_environment")
ADDON_LOG_FILE = Path(os.getenv("POWERHAUSBOX_INTERNAL_LOG", "/data/powerhausbox.log"))
ADDON_LOG_MAX_BYTES = 2 * 1024 * 1024
ADDON_LOG_TRIM_TO_BYTES = 1024 * 1024


def log(message: str, prefix: str = "powerhausbox-server") -> None:
    line = f"{utcnow_iso()} [{prefix}] {message}"
    print(f"[{prefix}] {message}", flush=True)
    try:
        ADDON_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with ADDON_LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        _trim_addon_log_if_needed()
    except OSError:
        pass


def _trim_addon_log_if_needed() -> None:
    try:
        if not ADDON_LOG_FILE.exists() or ADDON_LOG_FILE.stat().st_size <= ADDON_LOG_MAX_BYTES:
            return
        with ADDON_LOG_FILE.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(-min(ADDON_LOG_TRIM_TO_BYTES, size), os.SEEK_END)
            data = handle.read()
        newline_index = data.find(b"\n")
        if newline_index != -1:
            data = data[newline_index + 1:]
        ADDON_LOG_FILE.write_bytes(data)
    except OSError:
        pass


def read_addon_log_tail(*, max_lines: int = 400) -> list[str]:
    if max_lines <= 0:
        return []
    try:
        lines = ADDON_LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return lines[-max_lines:]


def read_container_env_value(*names: str) -> str:
    for name in names:
        raw_value = os.getenv(name, "").strip()
        if raw_value:
            return raw_value
    for name in names:
        path = CONTAINER_ENV_DIR / name
        try:
            raw_value = path.read_text(encoding="utf-8").strip()
        except OSError:
            raw_value = ""
        if raw_value:
            return raw_value
    return ""


def read_json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def write_secret_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


def write_json_file(path: Path, payload: dict[str, Any]) -> None:
    write_secret_file(path, json.dumps(payload, ensure_ascii=True, separators=(",", ":")) + "\n")


def parse_bool(raw_value: Any, default: bool) -> bool:
    if isinstance(raw_value, bool):
        return raw_value
    if raw_value is None:
        return default
    if isinstance(raw_value, (int, float)):
        return bool(raw_value)
    if isinstance(raw_value, str):
        normalized = raw_value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
    return default


def read_interval_seconds(name: str, default: int, minimum: int) -> int:
    raw_value = os.getenv(name, str(default)).strip()
    try:
        parsed = int(raw_value)
    except ValueError:
        parsed = default
    return parsed if parsed >= minimum else minimum


def to_positive_int(raw_value: Any, default: int) -> int:
    try:
        parsed = int(raw_value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso_timestamp(raw_value: str) -> datetime | None:
    candidate = str(raw_value).strip()
    if not candidate:
        return None
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(candidate)
    except ValueError:
        return None


def seconds_since(raw_value: str, now: float | None = None) -> float | None:
    parsed = parse_iso_timestamp(raw_value)
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    reference = now if now is not None else __import__("time").time()
    return max(reference - parsed.timestamp(), 0.0)


def should_run_periodic(last_run_at: str, interval_seconds: int, now: float | None = None) -> bool:
    elapsed = seconds_since(last_run_at, now=now)
    if elapsed is None:
        return True
    return elapsed >= interval_seconds


def normalize_url(url: str, *, default_scheme: str, label: str) -> str:
    raw = url.strip()
    if not raw:
        raise ValueError(f"{label} is empty.")

    candidate = raw if "://" in raw else f"{default_scheme}://{raw}"
    parsed = urllib.parse.urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{label} is invalid.")
    if parsed.path not in {"", "/"} or parsed.params or parsed.query or parsed.fragment:
        raise ValueError(f"{label} must not include path, query, or fragment.")
    return f"{parsed.scheme}://{parsed.netloc}"


def supervisor_request_raw(
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    *,
    token: str = "",
    base_url: str = "",
    timeout: int = 30,
    error_class: type[Exception] = RuntimeError,
) -> dict[str, Any]:
    if not token:
        raise error_class("SUPERVISOR_TOKEN not available; enable hassio_api for this add-on.")

    url = f"{base_url.rstrip('/')}{path}"
    body = None
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url=url, method=method.upper(), data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw_body = response.read().decode("utf-8").strip()
            if not raw_body:
                return {}
            return json.loads(raw_body)
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8").strip()
        message = f"Supervisor API request failed with HTTP {exc.code}."
        if body_text:
            message = f"{message} Response: {body_text}"
        raise error_class(message) from exc
    except (TimeoutError, urllib.error.URLError) as exc:
        raise error_class("Supervisor API is unreachable.") from exc
