from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
import urllib.error
import urllib.request

import yaml

STATUS_ALREADY_CONFIGURED = "already configured"
STATUS_UPDATED_AND_RESTARTED = "updated and restarted"
STATUS_FAILED_AND_ROLLED_BACK = "failed and rolled back"


class IframeConfiguratorError(Exception):
    """Raised for iframe configurator errors."""


@dataclass
class ConfigureResult:
    status: str
    backup_path: Path | None
    message: str
    changed: bool


def log(message: str) -> None:
    print(f"[powerhausbox-cloudflare] {message}", flush=True)


def read_json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def parse_bool(raw_value: Any, default: bool) -> bool:
    if isinstance(raw_value, bool):
        return raw_value
    if isinstance(raw_value, (int, float)):
        return bool(raw_value)
    if isinstance(raw_value, str):
        normalized = raw_value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return default


def read_auto_enable_flag(options_path: Path) -> bool:
    options = read_json_file(options_path)
    return parse_bool(options.get("auto_enable_iframe_embedding", True), True)


def create_timestamped_backup(config_path: Path) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup_path = config_path.with_name(f"{config_path.name}.powerhausbox-backup-{timestamp}")
    suffix = 1
    while backup_path.exists():
        backup_path = config_path.with_name(f"{config_path.name}.powerhausbox-backup-{timestamp}-{suffix}")
        suffix += 1
    shutil.copy2(config_path, backup_path)
    return backup_path


def parse_configuration_yaml(config_path: Path) -> dict[str, Any]:
    raw_text = config_path.read_text(encoding="utf-8")
    try:
        loaded = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise IframeConfiguratorError(
            "Malformed YAML in configuration.yaml. Please fix syntax and retry."
        ) from exc

    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise IframeConfiguratorError("configuration.yaml must be a YAML mapping at top level.")
    return loaded


def ensure_iframe_embedding_setting(configuration: dict[str, Any]) -> bool:
    http_block = configuration.get("http")
    if http_block is None:
        http_block = {}
        configuration["http"] = http_block
    elif not isinstance(http_block, dict):
        raise IframeConfiguratorError("'http' in configuration.yaml must be a mapping.")

    existing = http_block.get("use_x_frame_options")
    if existing is False:
        return False

    http_block["use_x_frame_options"] = False
    return True


def atomic_write_yaml(config_path: Path, configuration: dict[str, Any]) -> None:
    dumped = yaml.safe_dump(
        configuration,
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
    )
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(config_path.parent),
        prefix=f"{config_path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        temp_path = Path(handle.name)
        handle.write(dumped)
    os.chmod(temp_path, 0o600)
    os.replace(temp_path, config_path)


def restore_backup(config_path: Path, backup_path: Path) -> None:
    shutil.copy2(backup_path, config_path)


def supervisor_request(path: str, method: str = "POST", payload: dict[str, Any] | None = None) -> dict[str, Any]:
    token = os.getenv("SUPERVISOR_TOKEN", "").strip()
    if not token:
        raise IframeConfiguratorError("SUPERVISOR_TOKEN is missing; cannot run config check/restart.")

    supervisor_url = os.getenv("SUPERVISOR_URL", "http://supervisor").rstrip("/")
    url = f"{supervisor_url}{path}"
    data: bytes | None = None
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url=url, method=method.upper(), data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=45) as response:
            body = response.read().decode("utf-8").strip()
            if not body:
                return {}
            return json.loads(body)
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8").strip()
        msg = f"Supervisor API {path} failed with HTTP {exc.code}."
        if error_body:
            msg = f"{msg} Response: {error_body}"
        raise IframeConfiguratorError(msg) from exc
    except (TimeoutError, urllib.error.URLError) as exc:
        raise IframeConfiguratorError(f"Supervisor API {path} is unreachable.") from exc


def parse_check_config_response(response: dict[str, Any]) -> tuple[bool, str]:
    payload: Any = response
    if isinstance(response, dict) and response.get("result") in {"ok", "error"}:
        if response.get("result") == "error":
            return False, str(response.get("message") or "Supervisor returned error.")
        payload = response.get("data", {})

    if not isinstance(payload, dict):
        return True, ""

    result_value = str(payload.get("result", "")).strip().lower()
    if result_value in {"invalid", "error", "failed"}:
        detail = payload.get("errors") or payload.get("error") or payload.get("message") or "Invalid Home Assistant config."
        return False, str(detail)
    if result_value in {"valid", "ok", "passed"}:
        return True, ""

    errors_value = payload.get("errors")
    if errors_value not in (None, "", [], {}):
        return False, str(errors_value)

    return True, ""


def run_check_config() -> tuple[bool, str]:
    attempted_paths: list[str] = []
    failures: list[str] = []
    for path in ("/core/check", "/core/api/config/core/check_config"):
        attempted_paths.append(path)
        try:
            response = supervisor_request(path, "POST")
            valid, detail = parse_check_config_response(response)
            if valid:
                return True, ""
            return False, detail or f"Validation failed via {path}."
        except IframeConfiguratorError as exc:
            failures.append(str(exc))

    joined_paths = ", ".join(attempted_paths)
    details = " | ".join(failures) if failures else "unknown error"
    return False, f"Could not execute Home Assistant config validation ({joined_paths}). {details}"


def restart_home_assistant_core() -> tuple[bool, str]:
    try:
        supervisor_request("/core/restart", "POST")
        return True, ""
    except IframeConfiguratorError as restart_error:
        fallback_errors = [str(restart_error)]

    try:
        supervisor_request("/core/stop", "POST")
        supervisor_request("/core/start", "POST")
        return True, ""
    except IframeConfiguratorError as exc:
        fallback_errors.append(str(exc))
        return False, " ; ".join(fallback_errors)


def configure_iframe_embedding(
    config_path: Path,
    validate_fn: Callable[[], tuple[bool, str]],
    restart_fn: Callable[[], tuple[bool, str]],
) -> ConfigureResult:
    if not config_path.exists():
        raise IframeConfiguratorError(f"{config_path} does not exist.")
    if not os.access(config_path, os.R_OK):
        raise IframeConfiguratorError(f"{config_path} is not readable.")
    if not os.access(config_path, os.W_OK):
        raise IframeConfiguratorError(f"{config_path} is not writable.")

    try:
        backup_path = create_timestamped_backup(config_path)
    except (OSError, PermissionError) as exc:
        raise IframeConfiguratorError(f"Failed to create backup for {config_path}: {exc}") from exc

    try:
        configuration = parse_configuration_yaml(config_path)
        changed = ensure_iframe_embedding_setting(configuration)
    except (OSError, PermissionError, IframeConfiguratorError) as exc:
        return ConfigureResult(
            status=STATUS_FAILED_AND_ROLLED_BACK,
            backup_path=backup_path,
            message=str(exc),
            changed=False,
        )

    if not changed:
        return ConfigureResult(
            status=STATUS_ALREADY_CONFIGURED,
            backup_path=backup_path,
            message="http.use_x_frame_options is already false.",
            changed=False,
        )

    try:
        atomic_write_yaml(config_path, configuration)
    except (OSError, PermissionError) as exc:
        rollback_error = ""
        try:
            restore_backup(config_path, backup_path)
        except (OSError, PermissionError) as restore_exc:
            rollback_error = f" Rollback restore failed: {restore_exc}"
        return ConfigureResult(
            status=STATUS_FAILED_AND_ROLLED_BACK,
            backup_path=backup_path,
            message=f"Failed to write updated configuration.yaml: {exc}.{rollback_error}",
            changed=False,
        )

    valid, validation_message = validate_fn()
    if not valid:
        rollback_error = ""
        try:
            restore_backup(config_path, backup_path)
        except (OSError, PermissionError) as exc:
            rollback_error = f" Rollback restore failed: {exc}"
        return ConfigureResult(
            status=STATUS_FAILED_AND_ROLLED_BACK,
            backup_path=backup_path,
            message=f"Validation failed: {validation_message}.{rollback_error}",
            changed=True,
        )

    restarted, restart_message = restart_fn()
    if not restarted:
        rollback_error = ""
        try:
            restore_backup(config_path, backup_path)
        except (OSError, PermissionError) as exc:
            rollback_error = f" Rollback restore failed: {exc}"
        manual_instruction = "Please restart Home Assistant Core manually from Settings -> System -> Restart."
        return ConfigureResult(
            status=STATUS_FAILED_AND_ROLLED_BACK,
            backup_path=backup_path,
            message=(
                f"Restart trigger failed after valid config update: {restart_message}. "
                f"{manual_instruction} Rolled back to backup.{rollback_error}"
            ),
            changed=True,
        )

    return ConfigureResult(
        status=STATUS_UPDATED_AND_RESTARTED,
        backup_path=backup_path,
        message="http.use_x_frame_options set to false and Home Assistant Core restarted.",
        changed=True,
    )


def main() -> int:
    options_file = Path(os.getenv("OPTIONS_FILE", "/data/options.json"))
    config_path = Path(os.getenv("HA_CONFIGURATION_FILE", "/config/configuration.yaml"))

    auto_enable = read_auto_enable_flag(options_file)
    if not auto_enable:
        log("auto_enable_iframe_embedding is disabled; skipping iframe configuration.")
        return 0

    try:
        result = configure_iframe_embedding(config_path, run_check_config, restart_home_assistant_core)
    except IframeConfiguratorError as exc:
        log(f"{STATUS_FAILED_AND_ROLLED_BACK} (backup: n/a) - {exc}")
        return 1

    backup_display = str(result.backup_path) if result.backup_path else "n/a"
    log(f"{result.status} (backup: {backup_display}) - {result.message}")
    if result.status == STATUS_FAILED_AND_ROLLED_BACK:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
