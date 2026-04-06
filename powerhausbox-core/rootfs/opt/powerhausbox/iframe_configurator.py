from __future__ import annotations

import ipaddress
import os
import shutil
import socket
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import sys

import yaml

_module_dir = str(Path(__file__).resolve().parent)
if _module_dir not in sys.path:
    sys.path.insert(0, _module_dir)

from exceptions import IframeConfiguratorError  # noqa: E402
from utils import (  # noqa: E402
    log as _log,
    parse_bool,
    read_container_env_value,
    read_json_file,
    supervisor_request_raw,
)

STATUS_ALREADY_CONFIGURED = "already configured"
STATUS_UPDATED_AND_RESTARTED = "updated and restarted"
STATUS_UPDATED_RESTART_REQUIRED = "updated, restart required"
STATUS_FAILED_AND_ROLLED_BACK = "failed and rolled back"


@dataclass
class ConfigureResult:
    status: str
    backup_path: Path | None
    message: str
    changed: bool


@dataclass
class TaggedYAMLValue:
    tag: str
    value: Any


def log(message: str) -> None:
    _log(message, prefix="powerhausbox-cloudflare")


class PowerHausBoxLoader(yaml.SafeLoader):
    pass


class PowerHausBoxDumper(yaml.SafeDumper):
    pass


def construct_tagged_yaml_value(loader: PowerHausBoxLoader, tag_suffix: str, node: yaml.Node) -> TaggedYAMLValue:
    tag_name = f"!{tag_suffix}" if tag_suffix else node.tag
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    elif isinstance(node, yaml.MappingNode):
        value = loader.construct_mapping(node, deep=True)
    else:  # pragma: no cover - PyYAML currently exposes only scalar/sequence/mapping nodes here.
        value = loader.construct_object(node, deep=True)
    return TaggedYAMLValue(tag=tag_name, value=value)


def represent_tagged_yaml_value(dumper: PowerHausBoxDumper, value: TaggedYAMLValue) -> yaml.Node:
    payload = value.value
    if isinstance(payload, dict):
        return dumper.represent_mapping(value.tag, payload)
    if isinstance(payload, list):
        return dumper.represent_sequence(value.tag, payload)
    if payload is None:
        return dumper.represent_scalar(value.tag, "")
    return dumper.represent_scalar(value.tag, str(payload))


PowerHausBoxLoader.add_multi_constructor("!", construct_tagged_yaml_value)
PowerHausBoxDumper.add_representer(TaggedYAMLValue, represent_tagged_yaml_value)


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
        loaded = yaml.load(raw_text, Loader=PowerHausBoxLoader)
    except yaml.YAMLError as exc:
        raise IframeConfiguratorError(
            "Malformed YAML in configuration.yaml. Please fix syntax and retry."
        ) from exc

    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise IframeConfiguratorError("configuration.yaml must be a YAML mapping at top level.")
    return loaded


def normalize_proxy_entry(raw_value: str) -> str:
    value = raw_value.strip()
    if not value:
        raise IframeConfiguratorError("trusted_proxies contains an empty value.")
    try:
        if "/" in value:
            return str(ipaddress.ip_network(value, strict=False))
        return str(ipaddress.ip_address(value))
    except ValueError as exc:
        raise IframeConfiguratorError(f"Invalid trusted proxy entry: {value}") from exc


def normalize_trusted_proxies(values: list[str]) -> list[str]:
    normalized: list[str] = []
    for raw_value in values:
        candidate = normalize_proxy_entry(raw_value)
        if candidate not in normalized:
            normalized.append(candidate)
    if not normalized:
        raise IframeConfiguratorError("No trusted proxy addresses are available.")
    return normalized


def discover_trusted_proxies() -> list[str]:
    configured_value = read_container_env_value("POWERHAUS_TRUSTED_PROXIES")
    if configured_value:
        raw_parts = configured_value.replace("\n", ",").split(",")
        return normalize_trusted_proxies([part.strip() for part in raw_parts if part.strip()])

    discovered: list[str] = []

    def add_candidate(candidate: str) -> None:
        if not candidate:
            return
        try:
            parsed = ipaddress.ip_address(candidate)
        except ValueError:
            return
        if parsed.is_loopback or parsed.is_unspecified:
            return
        normalized = str(parsed)
        if normalized not in discovered:
            discovered.append(normalized)

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            add_candidate(sock.getsockname()[0])
    except OSError:
        pass

    try:
        for family, _, _, _, sockaddr in socket.getaddrinfo(socket.gethostname(), None):
            if family not in {socket.AF_INET, socket.AF_INET6}:
                continue
            add_candidate(sockaddr[0])
    except socket.gaierror:
        pass

    if discovered:
        return normalize_trusted_proxies(discovered)
    return ["172.30.33.1"]


def ensure_http_integration_settings(configuration: dict[str, Any], trusted_proxies: list[str]) -> bool:
    http_block = configuration.get("http")
    if http_block is None:
        http_block = {}
        configuration["http"] = http_block
    elif not isinstance(http_block, dict):
        raise IframeConfiguratorError("'http' in configuration.yaml must be a mapping.")

    changed = False

    if http_block.get("use_x_frame_options") is not False:
        http_block["use_x_frame_options"] = False
        changed = True

    if http_block.get("use_x_forwarded_for") is not True:
        http_block["use_x_forwarded_for"] = True
        changed = True

    normalized_trusted_proxies = normalize_trusted_proxies(trusted_proxies)
    existing_trusted_proxies = http_block.get("trusted_proxies")
    if existing_trusted_proxies is None:
        http_block["trusted_proxies"] = list(normalized_trusted_proxies)
        changed = True
        return changed
    if not isinstance(existing_trusted_proxies, list):
        raise IframeConfiguratorError("'http.trusted_proxies' in configuration.yaml must be a list.")

    for proxy_entry in normalized_trusted_proxies:
        if proxy_entry not in existing_trusted_proxies:
            existing_trusted_proxies.append(proxy_entry)
            changed = True

    return changed


def atomic_write_yaml(config_path: Path, configuration: dict[str, Any]) -> None:
    dumped = yaml.dump(
        configuration,
        Dumper=PowerHausBoxDumper,
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
    token = read_container_env_value("SUPERVISOR_TOKEN", "HASSIO_TOKEN")
    base_url = (read_container_env_value("SUPERVISOR_URL") or "http://supervisor").rstrip("/")
    return supervisor_request_raw(
        method, path, payload,
        token=token, base_url=base_url, timeout=45,
        error_class=IframeConfiguratorError,
    )


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
    # If Core is already stopped by run_with_core_stopped (pairing flow),
    # skip the restart — the caller will handle it.
    if os.environ.get("POWERHAUS_CORE_STOPPED") == "1":
        return True, ""

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


def _build_http_block(trusted_proxies: list[str]) -> str:
    """Build the http: YAML block as text."""
    lines = [
        "",
        "# PowerHausBox: iframe embedding and reverse proxy settings",
        "http:",
        "  use_x_frame_options: false",
        "  use_x_forwarded_for: true",
        "  trusted_proxies:",
    ]
    for proxy in trusted_proxies:
        lines.append(f"    - {proxy}")
    lines.append("")
    return "\n".join(lines)


def _text_has_http_settings(content: str) -> bool:
    """Check if the file already has our http settings (text-based check)."""
    return (
        "use_x_frame_options: false" in content
        and "use_x_forwarded_for: true" in content
        and "trusted_proxies:" in content
    )


def configure_iframe_embedding(
    config_path: Path,
    validate_fn: Callable[[], tuple[bool, str]],
    restart_fn: Callable[[], tuple[bool, str]],
    trusted_proxies: list[str] | None = None,
) -> ConfigureResult:
    """Configure iframe embedding by appending http: block to configuration.yaml.

    Uses TEXT-BASED insertion to preserve comments, !include directives, and
    all existing formatting. Never parses and re-dumps the entire YAML file.
    """
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
        content = config_path.read_text(encoding="utf-8")
    except (OSError, PermissionError) as exc:
        return ConfigureResult(
            status=STATUS_FAILED_AND_ROLLED_BACK,
            backup_path=backup_path,
            message=str(exc),
            changed=False,
        )

    # Check if already configured
    if _text_has_http_settings(content):
        return ConfigureResult(
            status=STATUS_ALREADY_CONFIGURED,
            backup_path=backup_path,
            message="HTTP iframe and reverse proxy settings are already configured.",
            changed=False,
        )

    # Check if there's an existing http: block we'd conflict with.
    # We refuse to rewrite such files until a truly surgical merger exists.
    import re
    has_existing_http = bool(re.search(r"^http:", content, re.MULTILINE))
    if has_existing_http:
        return ConfigureResult(
            status=STATUS_FAILED_AND_ROLLED_BACK,
            backup_path=backup_path,
            message=(
                "configuration.yaml already contains an http: block. "
                "Refusing unsafe automatic merge; update http settings manually."
            ),
            changed=False,
        )
    else:
        # No existing http: block — safe to append as text (preserves all formatting)
        try:
            parse_configuration_yaml(config_path)
        except (OSError, PermissionError, IframeConfiguratorError) as exc:
            return ConfigureResult(
                status=STATUS_FAILED_AND_ROLLED_BACK,
                backup_path=backup_path,
                message=str(exc),
                changed=False,
            )
        proxy_list = discover_trusted_proxies() if trusted_proxies is None else trusted_proxies
        http_block = _build_http_block(normalize_trusted_proxies(proxy_list))

        try:
            new_content = content.rstrip() + "\n" + http_block
            config_path.write_text(new_content, encoding="utf-8")
        except (OSError, PermissionError) as exc:
            try:
                restore_backup(config_path, backup_path)
            except (OSError, PermissionError):
                pass
            return ConfigureResult(
                status=STATUS_FAILED_AND_ROLLED_BACK,
                backup_path=backup_path,
                message=f"Failed to write configuration.yaml: {exc}",
                changed=False,
            )

    # Validate if possible
    valid, validation_message = validate_fn()
    if not valid:
        try:
            restore_backup(config_path, backup_path)
        except (OSError, PermissionError) as exc:
            pass
        return ConfigureResult(
            status=STATUS_FAILED_AND_ROLLED_BACK,
            backup_path=backup_path,
            message=f"Validation failed: {validation_message}.",
            changed=True,
        )

    restarted, restart_message = restart_fn()
    if not restarted:
        return ConfigureResult(
            status=STATUS_UPDATED_RESTART_REQUIRED,
            backup_path=backup_path,
            message=f"Config updated but restart failed: {restart_message}. Restart HA manually.",
            changed=True,
        )

    return ConfigureResult(
        status=STATUS_UPDATED_AND_RESTARTED,
        backup_path=backup_path,
        message="HTTP iframe and reverse proxy settings added to configuration.yaml.",
        changed=True,
    )


def main() -> int:
    options_file = Path(os.getenv("OPTIONS_FILE", "/data/options.json"))
    config_path = Path(os.getenv("HA_CONFIGURATION_FILE", "/config/configuration.yaml"))

    auto_enable = read_auto_enable_flag(options_file)
    if not auto_enable:
        log("auto_enable_iframe_embedding is disabled; skipping iframe configuration.")
        return 0

    # When Core is already stopped (pairing flow), skip validation since Core
    # can't validate config while stopped. We trust our own YAML generation.
    core_stopped = os.environ.get("POWERHAUS_CORE_STOPPED") == "1"
    if core_stopped:
        validate_fn = lambda: (True, "")
        log("Core is stopped; skipping config validation (trusted write).")
    else:
        validate_fn = run_check_config

    try:
        result = configure_iframe_embedding(config_path, validate_fn, restart_home_assistant_core)
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
