import base64
import binascii
from functools import wraps
import hashlib
import hmac
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from flask import Flask, Response, flash, jsonify, redirect, render_template, request, session

_module_dir = str(Path(__file__).resolve().parent)
if _module_dir not in sys.path:
    sys.path.insert(0, _module_dir)

from exceptions import (  # noqa: E402
    AuthStorageError,
    PairingAPIError,
    StudioSyncError,
    SupervisorAPIError,
)
from utils import (  # noqa: E402
    CONTAINER_ENV_DIR,
    log,
    parse_bool,
    parse_iso_timestamp,
    read_container_env_value,
    read_interval_seconds,
    read_json_file,
    seconds_since,
    should_run_periodic,
    normalize_url,
    supervisor_request_raw,
    to_positive_int,
    utcnow_iso,
    write_json_file,
    write_secret_file,
)

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "powerhausbox-dev-secret")
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

# ---------------------------------------------------------------------------
# Constants and Configuration
# ---------------------------------------------------------------------------

TOKEN_FILE = Path(os.getenv("TOKEN_FILE", "/data/tunnel_token"))
SECRETS_FILE = Path(os.getenv("SECRETS_FILE", "/data/pairing_secrets.json"))
OPTIONS_FILE = Path(os.getenv("OPTIONS_FILE", "/data/options.json"))
MANAGED_SERVICE_USER_FILE = Path(os.getenv("MANAGED_SERVICE_USER_FILE", "/data/managed_service_user.json"))
IFRAME_CONFIGURATOR_SCRIPT = Path(os.getenv("IFRAME_CONFIGURATOR_SCRIPT", "/opt/powerhausbox/iframe_configurator.py"))
SYNC_STATE_FILE = Path(os.getenv("SYNC_STATE_FILE", "/data/sync_state.json"))

HA_CONFIG_DIR = Path(os.getenv("HA_CONFIG_DIR", "/config"))
AUTH_STORAGE_FILE = HA_CONFIG_DIR / ".storage" / "auth"
AUTH_PROVIDER_STORAGE_FILE = HA_CONFIG_DIR / ".storage" / "auth_provider.homeassistant"
CORE_CONFIG_STORAGE_FILE = HA_CONFIG_DIR / ".storage" / "core.config"

DEFAULT_UI_PASSWORD = os.getenv("UI_PASSWORD", "change-this-password")
DEFAULT_UI_AUTH_ENABLED = os.getenv("UI_AUTH_ENABLED", "false").strip().lower() == "true"
DEFAULT_STUDIO_BASE_URL = os.getenv("STUDIO_BASE_URL", "https://studio.powerhaus.ai")
DEFAULT_AUTO_ENABLE_IFRAME_EMBEDDING = os.getenv("AUTO_ENABLE_IFRAME_EMBEDDING", "true").strip().lower() != "false"

PAIR_INIT_PATH = "/api/addon/pair/init/"
PAIR_COMPLETE_PATH = "/api/addon/pair/complete/"
AUTH_SYNC_FULL_PATH = "/api/addon/auth-sync/full/"
CONFIG_SYNC_PATH = "/api/addon/config/sync/"
STATE_REPORT_PATH = "/api/addon/state/report/"
STUDIO_CONFIG_APPLY_PATH = "/_powerhausbox/api/studio/config/apply/"
STUDIO_CONFIG_PUSH_MAX_SKEW_SECONDS = 300
BACKUP_UPLOAD_PATH = "/api/addon/backup/upload/"
BACKUP_LIST_PATH = "/api/addon/backup/list/"
BACKUP_DOWNLOAD_PATH = "/api/addon/backup/download/"  # + backup_id + /
BACKUP_DETAIL_PATH = "/api/addon/backup/"  # + backup_id + /
BACKUP_CHUNK_SIZE = 262144  # 256 KB

GROUP_ID_USER = "system-users"
VALID_USERNAME_RE = re.compile(r"[a-z0-9._@-]{3,64}")
VALID_HOSTNAME_RE = re.compile(
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*"
)
VALID_BCRYPT_PREFIXES = (b"$2a$", b"$2b$", b"$2y$")
SERVICE_USER_WATCHDOG_ENABLED = os.getenv("SERVICE_USER_WATCHDOG_ENABLED", "true").strip().lower() != "false"
SERVICE_USER_WATCHDOG_INTERVAL_SECONDS = read_interval_seconds("SERVICE_USER_WATCHDOG_INTERVAL_SECONDS", 300, 60)
PERIODIC_AUTH_SYNC_ENABLED = os.getenv("PERIODIC_AUTH_SYNC_ENABLED", "true").strip().lower() != "false"
PERIODIC_AUTH_SYNC_INTERVAL_SECONDS = read_interval_seconds("PERIODIC_AUTH_SYNC_INTERVAL_SECONDS", 21600, 300)
CONFIG_RECONCILE_INTERVAL_SECONDS = read_interval_seconds("CONFIG_RECONCILE_INTERVAL_SECONDS", 60, 15)
CONFIG_PULL_INTERVAL_SECONDS = read_interval_seconds("CONFIG_PULL_INTERVAL_SECONDS", 300, 60)
AUTH_WATCH_INTERVAL_SECONDS = read_interval_seconds("AUTH_WATCH_INTERVAL_SECONDS", 5, 3)
HEALTH_PROBE_INTERVAL_SECONDS = read_interval_seconds("HEALTH_PROBE_INTERVAL_SECONDS", 60, 15)
HEARTBEAT_INTERVAL_SECONDS = read_interval_seconds("HEARTBEAT_INTERVAL_SECONDS", 3600, 300)
INVENTORY_INTERVAL_SECONDS = read_interval_seconds("INVENTORY_INTERVAL_SECONDS", 86400, 3600)
SYNC_STATE_REPORTS_ENABLED = os.getenv("SYNC_STATE_REPORTS_ENABLED", "true").strip().lower() != "false"
ADDON_VERSION = os.getenv("ADDON_VERSION", "unknown")


# ---------------------------------------------------------------------------
# Thread-safe State Management
# ---------------------------------------------------------------------------

_pairing_state_lock = threading.Lock()
_pairing_state: dict[str, Any] = {}
_watchdog_started = False
_watchdog_lock = threading.Lock()
_periodic_auth_sync_started = False
_periodic_auth_sync_lock = threading.Lock()
_sync_job_queue: queue.Queue[dict[str, str]] = queue.Queue()

_sync_pending_jobs_lock = threading.Lock()
_sync_pending_jobs: set[str] = set()
_sync_state_lock = threading.Lock()
_health_snapshot_lock = threading.Lock()
_latest_health_snapshot: dict[str, Any] = {}


SUPERVISOR_URL = (read_container_env_value("SUPERVISOR_URL") or "http://supervisor").rstrip("/")
SUPERVISOR_TOKEN = read_container_env_value("SUPERVISOR_TOKEN", "HASSIO_TOKEN")

# ---------------------------------------------------------------------------
# Ingress and Authentication Helpers
# ---------------------------------------------------------------------------


def is_authenticated() -> bool:
    if not is_ui_auth_enabled():
        return True
    return bool(session.get("authenticated"))


def _normalized_ingress_prefix() -> str:
    raw_prefix = str(request.headers.get("X-Ingress-Path", "") or "").strip()
    if not raw_prefix:
        raw_prefix = str(request.script_root or "").strip()
    if not raw_prefix:
        return ""
    if not raw_prefix.startswith("/"):
        raw_prefix = "/" + raw_prefix
    return raw_prefix.rstrip("/")


def ingress_url(path: str) -> str:
    normalized_path = "/" + path.lstrip("/")
    prefix = _normalized_ingress_prefix()
    if not prefix:
        return normalized_path
    return f"{prefix}{normalized_path}"


@app.context_processor
def inject_template_helpers() -> dict[str, Any]:
    return {
        "ingress_url": ingress_url,
        "ui_auth_enabled": is_ui_auth_enabled(),
        "persistent_apply_alert": build_apply_alert(),
    }



# ---------------------------------------------------------------------------
# Sync State Management
# ---------------------------------------------------------------------------

def _default_sync_state() -> dict[str, Any]:
    return {
        "desired_config_version": 0,
        "applied_config_version": 0,
        "last_apply_at": "",
        "last_apply_status": "",
        "last_apply_target": "",
        "last_apply_error": "",
        "last_apply_expected": {},
        "last_apply_observed": {},
        "last_config_sync_at": "",
        "last_config_sync_status": "",
        "last_config_sync_error": "",
        "last_config_reconcile_at": "",
        "last_config_reconcile_status": "",
        "last_config_reconcile_error": "",
        "last_auth_sync_at": "",
        "last_auth_sync_status": "",
        "last_auth_sync_error": "",
        "last_auth_snapshot_hash": "",
        "last_auth_observed_at": "",
        "last_health_probe_at": "",
        "last_health_status": "",
        "last_health_error": "",
        "last_heartbeat_at": "",
        "last_heartbeat_status": "",
        "last_heartbeat_error": "",
        "last_inventory_at": "",
        "last_inventory_status": "",
        "last_inventory_error": "",
        "studio_state_report_support": "unknown",
        "processed_command_ids": [],
    }


def read_sync_state() -> dict[str, Any]:
    raw_state = read_json_file(SYNC_STATE_FILE)
    state = _default_sync_state()
    if isinstance(raw_state, dict):
        state.update(raw_state)
    processed_ids = state.get("processed_command_ids")
    if not isinstance(processed_ids, list):
        processed_ids = []
    state["processed_command_ids"] = [str(item).strip() for item in processed_ids if str(item).strip()][-128:]
    support_state = str(state.get("studio_state_report_support", "unknown")).strip().lower()
    if support_state not in {"unknown", "supported", "unsupported"}:
        support_state = "unknown"
    state["studio_state_report_support"] = support_state
    return state


def mutate_sync_state(mutator: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    with _sync_state_lock:
        state = read_sync_state()
        mutator(state)
        processed_ids = state.get("processed_command_ids")
        if isinstance(processed_ids, list):
            state["processed_command_ids"] = [str(item).strip() for item in processed_ids if str(item).strip()][-128:]
        write_json_file(SYNC_STATE_FILE, state)
        return state


def update_sync_state(**updates: Any) -> dict[str, Any]:
    def _mutate(state: dict[str, Any]) -> None:
        state.update(updates)

    return mutate_sync_state(_mutate)


def read_processed_command_ids() -> set[str]:
    return set(read_sync_state().get("processed_command_ids", []))


def has_processed_command_id(command_id: str) -> bool:
    normalized = str(command_id).strip()
    if not normalized:
        return False
    return normalized in read_processed_command_ids()


def remember_processed_command_id(command_id: str) -> None:
    normalized = str(command_id).strip()
    if not normalized:
        return

    def _mutate(state: dict[str, Any]) -> None:
        processed = list(state.get("processed_command_ids", []))
        processed.append(normalized)
        state["processed_command_ids"] = processed[-128:]

    mutate_sync_state(_mutate)


def set_latest_health_snapshot(snapshot: dict[str, Any]) -> None:
    with _health_snapshot_lock:
        _latest_health_snapshot.clear()
        _latest_health_snapshot.update(snapshot)


def get_latest_health_snapshot() -> dict[str, Any]:
    with _health_snapshot_lock:
        return dict(_latest_health_snapshot)


def build_apply_alert() -> dict[str, str]:
    sync_state = read_sync_state()
    status = str(sync_state.get("last_apply_status", "")).strip().lower()
    if status in {"", "ok", "applied", "corrected", "unchanged"}:
        return {}

    target = str(sync_state.get("last_apply_target", "")).strip() or "Home Assistant config"
    error = str(sync_state.get("last_apply_error", "")).strip() or "Last apply attempt failed."
    return {
        "category": "error" if status == "error" else "warning",
        "message": f"{target} apply status is {status}. {error}",
    }



# ---------------------------------------------------------------------------
# API Error Extraction Helpers
# ---------------------------------------------------------------------------

def extract_api_error_code(payload: dict[str, Any]) -> str:
    return str(payload.get("error") or payload.get("code") or "").strip()


def extract_api_error_detail(payload: dict[str, Any]) -> str:
    return str(payload.get("detail") or payload.get("message") or "").strip()


def extract_api_request_id(payload: dict[str, Any], response_headers: dict[str, str] | None = None) -> str:
    request_id = str(payload.get("request_id") or payload.get("correlation_id") or "").strip()
    if request_id:
        return request_id
    if not response_headers:
        return ""
    return str(response_headers.get("x-request-id") or response_headers.get("x-correlation-id") or "").strip()


def extract_api_cf_ray(response_headers: dict[str, str] | None = None) -> str:
    if not response_headers:
        return ""
    return str(response_headers.get("cf-ray") or "").strip()


def pairing_error_detail_suffix(*, api_error: str, api_detail: str, request_id: str, cf_ray: str) -> str:
    parts: list[str] = []
    if api_error:
        parts.append(api_error)
    if api_detail:
        parts.append(api_detail)
    if request_id:
        parts.append(f"request_id={request_id}")
    if cf_ray:
        parts.append(f"cf_ray={cf_ray}")
    if not parts:
        return ""
    return f" Details: {' | '.join(parts)}"


def build_pair_start_error_message(
    *,
    status_code: int | None,
    api_error: str,
    api_detail: str,
    request_id: str = "",
    cf_ray: str = "",
    server_header: str = "",
) -> str:
    detail_suffix = pairing_error_detail_suffix(
        api_error=api_error,
        api_detail=api_detail,
        request_id=request_id,
        cf_ray=cf_ray,
    )

    if api_error == "invalid_code":
        return "Pair code is invalid." + detail_suffix
    if api_error == "code_expired":
        return "Pair code expired. Generate a fresh code in Studio." + detail_suffix
    if api_error == "code_used":
        return "Pair code was already used. Generate a fresh code in Studio." + detail_suffix
    if api_error == "tenant_mismatch":
        return (
            "Pair code belongs to a different Studio environment/account than studio_base_url."
            + detail_suffix
        )
    if api_error == "forbidden_source":
        return "Studio rejected this pairing source (forbidden_source)." + detail_suffix
    if api_error == "rate_limited" or status_code == 429:
        return "Too many attempts. Please wait and try again." + detail_suffix

    if status_code == 403:
        if "cloudflare" in server_header.lower() and not api_error:
            return (
                "Pairing request blocked before Studio app (HTTP 403 via Cloudflare/WAF). "
                "Check Cloudflare security policy for this Home Assistant egress IP."
                + detail_suffix
            )
        return "Studio denied pairing request (HTTP 403)." + detail_suffix

    if status_code:
        return f"Pairing init failed (HTTP {status_code})." + detail_suffix

    return "Pairing init failed: no HTTP status returned from Studio."



# ---------------------------------------------------------------------------
# Username, Hostname, and URL Normalization
# ---------------------------------------------------------------------------

def normalize_username(username: str) -> str:
    return username.strip().casefold()


def validate_username(username: str) -> str:
    normalized = normalize_username(username)
    if normalized != username:
        raise AuthStorageError("Username must already be normalized (lowercase and without surrounding spaces).")
    if not VALID_USERNAME_RE.fullmatch(normalized):
        raise AuthStorageError("Username must be 3-64 chars: lowercase letters, numbers, '.', '_', '@' or '-'.")
    return normalized


def validate_precomputed_password_hash(password_hash: str) -> None:
    if not password_hash:
        raise AuthStorageError("Password hash is required.")
    try:
        decoded_hash = base64.b64decode(password_hash, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise AuthStorageError("Password hash must be base64(bcrypt) from Home Assistant storage format.") from exc
    if not decoded_hash.startswith(VALID_BCRYPT_PREFIXES):
        raise AuthStorageError("Password hash does not look like a bcrypt hash encoded as base64.")


def require_dict(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AuthStorageError(f"{label} is invalid.")
    return value


def ensure_list(container: dict[str, Any], key: str, label: str) -> list[Any]:
    current = container.get(key)
    if current is None:
        container[key] = []
        return container[key]
    if not isinstance(current, list):
        raise AuthStorageError(f"{label} is invalid.")
    return current



# ---------------------------------------------------------------------------
# Credential Management
# ---------------------------------------------------------------------------

def read_saved_credentials() -> dict[str, str]:
    raw = read_json_file(SECRETS_FILE)
    return {
        "cloudflare_tunnel_token": str(raw.get("cloudflare_tunnel_token", "")),
        "tunnel_hostname": str(raw.get("tunnel_hostname", "")),
        "box_api_token": str(raw.get("box_api_token", "")),
        "internal_url": str(raw.get("internal_url", "")),
        "external_url": str(raw.get("external_url", "")),
        "hostname": str(raw.get("hostname", "")),
        "config_version": str(raw.get("config_version", "")),
    }


def read_live_core_urls() -> dict[str, str]:
    config_doc = read_core_config_document()
    config_data = config_doc.get("data", {})
    if not isinstance(config_data, dict):
        raise SupervisorAPIError("Home Assistant core config storage is invalid.")

    internal_url = str(config_data.get("internal_url") or "").strip()
    external_url = str(config_data.get("external_url") or "").strip()

    normalized_internal_url = ""
    normalized_external_url = ""
    if internal_url:
        try:
            normalized_internal_url = normalize_internal_url(internal_url)
        except AuthStorageError:
            normalized_internal_url = internal_url
    if external_url:
        try:
            normalized_external_url = normalize_external_url(external_url)
        except AuthStorageError:
            normalized_external_url = external_url

    return {
        "internal_url": normalized_internal_url,
        "external_url": normalized_external_url,
    }


def has_saved_pairing_credentials() -> bool:
    creds = read_saved_credentials()
    return all(
        creds.get(key)
        for key in ("cloudflare_tunnel_token", "tunnel_hostname", "box_api_token", "internal_url", "external_url")
    )


def normalize_external_url(external_url: str) -> str:
    try:
        return normalize_url(external_url, default_scheme="https", label="External URL")
    except ValueError as exc:
        raise AuthStorageError(str(exc)) from exc


def normalize_internal_url(internal_url: str) -> str:
    try:
        return normalize_url(internal_url, default_scheme="http", label="Internal URL")
    except ValueError as exc:
        raise AuthStorageError(str(exc)) from exc


def normalize_hostname(hostname: str) -> str:
    raw = hostname.strip().lower().rstrip(".")
    if not raw:
        raise AuthStorageError("Hostname is empty.")
    if len(raw) > 253:
        raise AuthStorageError("Hostname is too long.")
    if not VALID_HOSTNAME_RE.fullmatch(raw):
        raise AuthStorageError("Hostname is invalid.")
    return raw


def persist_credentials(
    cloudflare_tunnel_token: str,
    tunnel_hostname: str,
    box_api_token: str,
    internal_url: str,
    external_url: str,
    hostname: str = "",
    config_version: int | str = 0,
) -> None:
    payload = {
        "cloudflare_tunnel_token": cloudflare_tunnel_token,
        "tunnel_hostname": tunnel_hostname,
        "box_api_token": box_api_token,
        "internal_url": normalize_internal_url(internal_url),
        "external_url": normalize_external_url(external_url),
        "hostname": normalize_hostname(hostname) if hostname.strip() else "",
        "config_version": max(to_positive_int(config_version, 0), 0),
    }
    write_secret_file(SECRETS_FILE, json.dumps(payload, ensure_ascii=True) + "\n")
    write_secret_file(TOKEN_FILE, cloudflare_tunnel_token + "\n")


def clear_credentials() -> None:
    TOKEN_FILE.unlink(missing_ok=True)
    SECRETS_FILE.unlink(missing_ok=True)


def reset_sync_state() -> None:
    SYNC_STATE_FILE.unlink(missing_ok=True)
    set_latest_health_snapshot({})


def token_status_text() -> str:
    creds = read_saved_credentials()
    if has_saved_pairing_credentials():
        return f"Paired and ready. Tunnel hostname: {creds['tunnel_hostname']}"
    return "No pairing credentials configured yet."


def display_timestamp(raw_value: str) -> str:
    parsed = parse_iso_timestamp(raw_value)
    if parsed is None:
        return "Never"
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone().strftime("%Y-%m-%d %H:%M:%S")



# ---------------------------------------------------------------------------
# Addon Options
# ---------------------------------------------------------------------------

def read_addon_options() -> dict[str, Any]:
    options = read_json_file(OPTIONS_FILE)
    raw_studio_base_url = str(options.get("studio_base_url") or options.get("STUDIO_BASE_URL") or "").strip()
    raw_ui_password = str(options.get("ui_password") or options.get("UI_PASSWORD") or "").strip()
    return {
        "ui_auth_enabled": parse_bool(
            options.get("ui_auth_enabled", options.get("UI_AUTH_ENABLED")),
            DEFAULT_UI_AUTH_ENABLED,
        ),
        "ui_password": raw_ui_password or DEFAULT_UI_PASSWORD,
        "studio_base_url": (raw_studio_base_url or DEFAULT_STUDIO_BASE_URL).rstrip("/"),
        "auto_enable_iframe_embedding": parse_bool(
            options.get("auto_enable_iframe_embedding"),
            DEFAULT_AUTO_ENABLE_IFRAME_EMBEDDING,
        ),
    }


def get_studio_base_url() -> str:
    return str(read_addon_options()["studio_base_url"])


def is_ui_auth_enabled() -> bool:
    return bool(read_addon_options()["ui_auth_enabled"])


def get_ui_password() -> str:
    return str(read_addon_options()["ui_password"])


def normalize_redirect_path(raw_path: str, default: str = "/pairing") -> str:
    candidate = raw_path.strip()
    if not candidate.startswith("/") or candidate.startswith("//"):
        return default
    return candidate


def is_valid_https_url(base_url: str) -> bool:
    parsed = urllib.parse.urlparse(base_url)
    return parsed.scheme == "https" and bool(parsed.netloc)


def valid_pair_code(pair_code: str) -> bool:
    return bool(re.fullmatch(r"\d{6}", pair_code))


def compute_auth_snapshot_hash(rows: list[dict[str, Any]]) -> str:
    normalized_rows: list[dict[str, Any]] = []
    for row in rows:
        normalized_rows.append(
            {
                "user_id": str(row.get("user_id", "")),
                "credential_id": str(row.get("credential_id", "")),
                "name": str(row.get("name", "")),
                "username": str(row.get("username", "")),
                "password_hash": str(row.get("password_hash", "")),
                "is_owner": bool(row.get("is_owner", False)),
                "is_active": bool(row.get("is_active", False)),
                "system_generated": bool(row.get("system_generated", False)),
                "local_only": bool(row.get("local_only", False)),
                "group_ids": [str(group_id) for group_id in row.get("group_ids", []) if str(group_id)],
            }
        )
    payload = json.dumps(normalized_rows, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def extract_pair_code_from_form(form: Any) -> str:
    direct_pair_code = str(form.get("pair_code", "")).strip()
    if direct_pair_code:
        return direct_pair_code

    digits: list[str] = []
    for index in range(1, 7):
        digits.append(str(form.get(f"pair_code_{index}", "")).strip())
    return "".join(digits)


def post_json(url: str, payload: dict[str, Any], headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    req_headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if headers:
        req_headers.update(headers)

    req = urllib.request.Request(
        url=url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers=req_headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            body = response.read().decode("utf-8").strip()
            data = json.loads(body) if body else {}
            return response.status, data
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8").strip()
        payload_data: dict[str, Any] = {}
        response_headers = {
            str(key).strip().lower(): str(value).strip()
            for key, value in (exc.headers.items() if exc.headers else [])
        }
        if body:
            try:
                payload_data = json.loads(body)
            except json.JSONDecodeError:
                payload_data = {}
        raise PairingAPIError(
            message="API request failed.",
            status_code=exc.code,
            payload=payload_data,
            response_headers=response_headers,
            response_body=body,
        ) from exc
    except (TimeoutError, urllib.error.URLError) as exc:
        raise PairingAPIError("Could not reach API endpoint.") from exc


def current_config_version(credentials: dict[str, str] | None = None) -> int:
    source = credentials if credentials is not None else read_saved_credentials()
    return max(to_positive_int(source.get("config_version", 0), 0), 0)


def read_live_box_state(credentials: dict[str, str] | None = None) -> dict[str, str]:
    source = credentials if credentials is not None else read_saved_credentials()

    current_hostname = str(source.get("hostname", "")).strip()
    try:
        live_hostname = get_current_host_hostname()
        if live_hostname:
            current_hostname = live_hostname
    except SupervisorAPIError:
        pass

    current_internal_url = str(source.get("internal_url", "")).strip()
    current_external_url = str(source.get("external_url", "")).strip()
    try:
        live_urls = read_live_core_urls()
        if live_urls.get("internal_url"):
            current_internal_url = live_urls["internal_url"]
        if live_urls.get("external_url"):
            current_external_url = live_urls["external_url"]
    except SupervisorAPIError:
        pass

    if current_internal_url:
        try:
            current_internal_url = normalize_internal_url(current_internal_url)
        except AuthStorageError:
            current_internal_url = ""
    if current_external_url:
        try:
            current_external_url = normalize_external_url(current_external_url)
        except AuthStorageError:
            current_external_url = ""

    return {
        "tunnel_hostname": str(source.get("tunnel_hostname", "")).strip(),
        "internal_url": current_internal_url,
        "external_url": current_external_url,
        "hostname": current_hostname,
        "config_version": str(source.get("config_version", "")),
    }


def verify_applied_homeassistant_state(
    *,
    expected_hostname: str = "",
    expected_internal_url: str = "",
    expected_external_url: str = "",
    target: str = "homeassistant_config",
) -> dict[str, str]:
    expected: dict[str, str] = {}
    if str(expected_hostname).strip():
        expected["hostname"] = normalize_hostname(expected_hostname)
    if str(expected_internal_url).strip():
        expected["internal_url"] = normalize_internal_url(expected_internal_url)
    if str(expected_external_url).strip():
        expected["external_url"] = normalize_external_url(expected_external_url)
    if not expected:
        raise SupervisorAPIError("No expected Home Assistant values provided for verification.")

    live_state = read_live_box_state(read_saved_credentials())
    observed = {
        "hostname": str(live_state.get("hostname", "")).strip(),
        "internal_url": str(live_state.get("internal_url", "")).strip(),
        "external_url": str(live_state.get("external_url", "")).strip(),
    }

    mismatches: list[str] = []
    for field, expected_value in expected.items():
        observed_value = observed.get(field, "")
        if observed_value != expected_value:
            mismatches.append(f"{field}: expected {expected_value}, observed {observed_value or 'unset'}")

    if mismatches:
        error_message = "Home Assistant apply verification failed: " + "; ".join(mismatches)
        update_sync_state(
            last_apply_at=utcnow_iso(),
            last_apply_status="error",
            last_apply_target=target,
            last_apply_error=error_message,
            last_apply_expected=expected,
            last_apply_observed=observed,
        )
        raise SupervisorAPIError(error_message)

    update_sync_state(
        last_apply_at=utcnow_iso(),
        last_apply_status="applied",
        last_apply_target=target,
        last_apply_error="",
        last_apply_expected=expected,
        last_apply_observed=observed,
    )
    return observed


def _compute_config_hash(
    config_version: int, hostname: str, internal_url: str, external_url: str, tunnel_hostname: str,
) -> str:
    """Compute a deterministic hash of config state for change detection."""
    content = f"{config_version}:{hostname}:{internal_url}:{external_url}:{tunnel_hostname}"
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _compute_ssh_keys_hash(keys: list[str]) -> str:
    """Compute a hash of SSH authorized keys for change detection."""
    normalized = "\n".join(sorted(k.strip() for k in keys if k.strip()))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _read_studio_synced_ssh_keys() -> list[str]:
    """Read the Studio-synced SSH keys (excluding local keys) from sync state."""
    state = read_sync_state()
    return state.get("last_ssh_authorized_keys", [])


def build_config_sync_payload(
    *,
    credentials: dict[str, str],
    reported_config_version: int | None = None,
    reported_apply_status: str = "",
    reported_apply_error: str = "",
) -> dict[str, Any]:
    live_state = read_live_box_state(credentials)
    current_tunnel_hostname = live_state["tunnel_hostname"]
    current_internal_url = live_state["internal_url"]
    current_external_url = live_state["external_url"]
    current_hostname = live_state["hostname"]

    cv = (
        current_config_version(credentials)
        if reported_config_version is None
        else max(to_positive_int(reported_config_version, 0), 0)
    )

    payload: dict[str, Any] = {
        "requested_at": utcnow_iso(),
        "source": "home_assistant_addon",
        "addon_version": ADDON_VERSION,
        "current_tunnel_hostname": current_tunnel_hostname,
        "current_internal_url": current_internal_url,
        "current_external_url": current_external_url,
        "current_hostname": current_hostname,
        "reported_config_version": cv,
        "hash_version": 1,
        "config_hash": _compute_config_hash(cv, current_hostname, current_internal_url, current_external_url, current_tunnel_hostname),
        "ssh_keys_hash": _compute_ssh_keys_hash(_read_studio_synced_ssh_keys()),
    }
    if reported_apply_status:
        payload["reported_apply_status"] = str(reported_apply_status).strip().lower()
    if reported_apply_error:
        payload["reported_apply_error"] = str(reported_apply_error).strip()
    return payload


def build_studio_push_signature(secret: str, timestamp: str, payload_bytes: bytes) -> str:
    message = timestamp.encode("utf-8") + b"." + payload_bytes
    return hmac.new(secret.encode("utf-8"), message, "sha256").hexdigest()


def verify_studio_push_signature(*, secret: str, timestamp: str, signature: str, payload_bytes: bytes) -> bool:
    if not secret or not timestamp or not signature:
        return False
    try:
        parsed_timestamp = int(timestamp)
    except ValueError:
        return False
    if abs(int(time.time()) - parsed_timestamp) > STUDIO_CONFIG_PUSH_MAX_SKEW_SECONDS:
        return False
    expected = build_studio_push_signature(secret, timestamp, payload_bytes)
    return hmac.compare_digest(expected, signature)



# ---------------------------------------------------------------------------
# Pairing State Management
# ---------------------------------------------------------------------------

def set_pairing_state(
    *,
    session_token: str,
    verification_code: str,
    poll_after_seconds: int,
    expires_in_seconds: int,
    base_url: str,
) -> None:
    with _pairing_state_lock:
        _pairing_state.clear()
        _pairing_state.update(
            {
                "session_token": session_token,
                "verification_code": verification_code,
                "poll_after_seconds": poll_after_seconds,
                "expires_at": int(time.time()) + expires_in_seconds,
                "base_url": base_url,
            }
        )


def get_pairing_state() -> dict[str, Any]:
    with _pairing_state_lock:
        return dict(_pairing_state)


def clear_pairing_state() -> None:
    with _pairing_state_lock:
        _pairing_state.clear()



# ---------------------------------------------------------------------------
# Home Assistant Auth Storage Operations
# ---------------------------------------------------------------------------

def read_auth_storage_documents() -> tuple[dict[str, Any], dict[str, Any]]:
    if not AUTH_STORAGE_FILE.exists():
        raise AuthStorageError(f"Home Assistant auth file not found: {AUTH_STORAGE_FILE}")
    if not AUTH_PROVIDER_STORAGE_FILE.exists():
        raise AuthStorageError(f"Home Assistant auth provider file not found: {AUTH_PROVIDER_STORAGE_FILE}")

    auth_doc = read_json_file(AUTH_STORAGE_FILE)
    provider_doc = read_json_file(AUTH_PROVIDER_STORAGE_FILE)
    if not auth_doc:
        raise AuthStorageError("Could not read Home Assistant auth storage.")
    if not provider_doc:
        raise AuthStorageError("Could not read Home Assistant auth provider storage.")

    auth_data = require_dict(auth_doc.get("data"), "auth storage data")
    provider_data = require_dict(provider_doc.get("data"), "auth provider data")
    ensure_list(auth_data, "users", "auth storage users")
    ensure_list(auth_data, "groups", "auth storage groups")
    ensure_list(auth_data, "credentials", "auth storage credentials")
    ensure_list(auth_data, "refresh_tokens", "auth storage refresh_tokens")
    ensure_list(provider_data, "users", "auth provider users")
    return auth_doc, provider_doc


def read_core_config_document() -> dict[str, Any]:
    if not CORE_CONFIG_STORAGE_FILE.exists():
        raise SupervisorAPIError(f"Home Assistant core config file not found: {CORE_CONFIG_STORAGE_FILE}")

    config_doc = read_json_file(CORE_CONFIG_STORAGE_FILE)
    if not config_doc:
        raise SupervisorAPIError("Could not read Home Assistant core config storage.")

    config_data = config_doc.get("data")
    if not isinstance(config_data, dict):
        raise SupervisorAPIError("Home Assistant core config storage is invalid.")
    return config_doc


def list_homeassistant_hash_users() -> list[dict[str, Any]]:
    auth_doc, provider_doc = read_auth_storage_documents()
    auth_data = require_dict(auth_doc.get("data"), "auth storage data")
    provider_data = require_dict(provider_doc.get("data"), "auth provider data")

    users = ensure_list(auth_data, "users", "auth storage users")
    credentials = ensure_list(auth_data, "credentials", "auth storage credentials")
    provider_users = ensure_list(provider_data, "users", "auth provider users")

    users_by_id: dict[str, dict[str, Any]] = {}
    for user in users:
        if isinstance(user, dict):
            user_id = str(user.get("id", "")).strip()
            if user_id:
                users_by_id[user_id] = user

    provider_by_normalized_username: dict[str, dict[str, Any]] = {}
    for provider_user in provider_users:
        if not isinstance(provider_user, dict):
            continue
        username = str(provider_user.get("username", "")).strip()
        if not username:
            continue
        provider_by_normalized_username[normalize_username(username)] = provider_user

    rows: list[dict[str, Any]] = []
    linked_usernames: set[str] = set()

    for credential in credentials:
        if not isinstance(credential, dict):
            continue
        if credential.get("auth_provider_type") != "homeassistant":
            continue

        credential_data = credential.get("data")
        if not isinstance(credential_data, dict):
            continue

        username = str(credential_data.get("username", "")).strip()
        if not username:
            continue
        normalized = normalize_username(username)
        linked_usernames.add(normalized)

        provider_user = provider_by_normalized_username.get(normalized, {})
        user = users_by_id.get(str(credential.get("user_id", "")), {})
        raw_groups = user.get("group_ids") if isinstance(user, dict) else []
        group_ids = [str(group_id) for group_id in raw_groups] if isinstance(raw_groups, list) else []

        rows.append(
            {
                "user_id": str(credential.get("user_id", "")),
                "credential_id": str(credential.get("id", "")),
                "name": str(user.get("name", "")) if isinstance(user, dict) else "",
                "username": username,
                "password_hash": str(provider_user.get("password", "")),
                "is_owner": bool(user.get("is_owner", False)) if isinstance(user, dict) else False,
                "is_active": bool(user.get("is_active", False)) if isinstance(user, dict) else False,
                "system_generated": bool(user.get("system_generated", False)) if isinstance(user, dict) else False,
                "local_only": bool(user.get("local_only", False)) if isinstance(user, dict) else False,
                "group_ids": group_ids,
            }
        )

    for normalized_username, provider_user in provider_by_normalized_username.items():
        if normalized_username in linked_usernames:
            continue
        rows.append(
            {
                "user_id": "",
                "credential_id": "",
                "name": "",
                "username": str(provider_user.get("username", "")),
                "password_hash": str(provider_user.get("password", "")),
                "is_owner": False,
                "is_active": False,
                "system_generated": False,
                "local_only": False,
                "group_ids": [],
            }
        )

    rows.sort(key=lambda item: normalize_username(str(item.get("username", ""))))
    return rows



# ---------------------------------------------------------------------------
# Studio Synchronization
# ---------------------------------------------------------------------------

def sync_auth_hashes_to_studio(*, users: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    base_url = get_studio_base_url()
    if not is_valid_https_url(base_url):
        raise StudioSyncError("studio_base_url must use HTTPS.")

    credentials = read_saved_credentials()
    box_api_token = credentials.get("box_api_token", "").strip()
    if not box_api_token:
        raise StudioSyncError("No box_api_token available. Pair app with Studio first.")

    if users is None:
        users = list_homeassistant_hash_users()
    payload = {
        "synced_at": utcnow_iso(),
        "source": "home_assistant_addon",
        "addon_version": ADDON_VERSION,
        "replace_all": True,
        "users": users,
    }
    headers = {"Authorization": f"Bearer {box_api_token}"}

    try:
        status_code, response = post_json(f"{base_url}{AUTH_SYNC_FULL_PATH}", payload, headers=headers)
    except PairingAPIError as exc:
        if exc.status_code == 401:
            raise StudioSyncError("Studio rejected box_api_token (401). Re-pair app.") from exc
        if exc.status_code == 429:
            raise StudioSyncError("Studio auth sync rate limited (429). Try again shortly.") from exc
        if exc.status_code == 404:
            raise StudioSyncError("Studio auth sync endpoint not found (404).") from exc
        if exc.status_code:
            raise StudioSyncError(f"Studio auth sync failed (HTTP {exc.status_code}).") from exc
        raise StudioSyncError(exc.message) from exc

    if status_code != 200:
        raise StudioSyncError(f"Studio auth sync returned unexpected HTTP {status_code}.")

    response_status = str(response.get("status", "")).strip().lower()
    if response_status not in {"ok", "accepted", "queued"}:
        raise StudioSyncError("Studio auth sync returned unexpected status payload.")

    received_count = to_positive_int(response.get("received_count", len(users)), len(users))
    return {
        "synced_count": len(users),
        "received_count": received_count,
        "sync_id": str(response.get("sync_id", "")).strip(),
        "status": response_status,
    }


def sync_addon_configuration_from_studio() -> dict[str, Any]:
    base_url = get_studio_base_url()
    if not is_valid_https_url(base_url):
        raise StudioSyncError("studio_base_url must use HTTPS.")

    current_credentials = read_saved_credentials()
    box_api_token = current_credentials.get("box_api_token", "").strip()
    if not box_api_token:
        raise StudioSyncError("No box_api_token available. Pair app with Studio first.")
    headers = {"Authorization": f"Bearer {box_api_token}"}

    def request_config_sync(sync_payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        try:
            return post_json(f"{base_url}{CONFIG_SYNC_PATH}", sync_payload, headers=headers)
        except PairingAPIError as exc:
            if exc.status_code == 401:
                raise StudioSyncError("Studio rejected box_api_token (401). Re-pair app.") from exc
            if exc.status_code == 404:
                raise StudioSyncError(
                    f"Studio config sync endpoint not found (404 at {CONFIG_SYNC_PATH})."
                ) from exc
            if exc.status_code == 429:
                raise StudioSyncError("Studio config sync rate limited (429). Try again shortly.") from exc
            if exc.status_code:
                raise StudioSyncError(f"Studio config sync failed (HTTP {exc.status_code}).") from exc
            raise StudioSyncError(exc.message) from exc

    try:
        status_code, response = request_config_sync(
            build_config_sync_payload(credentials=current_credentials)
        )
    except AuthStorageError as exc:
        raise StudioSyncError(str(exc)) from exc

    if status_code != 200:
        raise StudioSyncError(f"Studio config sync returned unexpected HTTP {status_code}.")

    response_status = str(response.get("status", "")).strip().lower()
    if response_status and response_status not in {"ok", "accepted", "updated", "unchanged"}:
        raise StudioSyncError("Studio config sync returned unexpected status payload.")

    # If Studio confirms nothing changed, skip all merge/write operations
    if response_status == "unchanged" and "cloudflare_tunnel_token" not in response:
        return {
            "status": "unchanged",
            "changed": False,
            "internal_url": current_credentials.get("internal_url", ""),
            "external_url": current_credentials.get("external_url", ""),
            "tunnel_hostname": current_credentials.get("tunnel_hostname", ""),
            "hostname": current_credentials.get("hostname", ""),
            "config_version": current_config_version(current_credentials),
        }

    merged_cloudflare_tunnel_token = str(
        response.get("cloudflare_tunnel_token") or current_credentials.get("cloudflare_tunnel_token") or ""
    ).strip()
    merged_tunnel_hostname = str(response.get("tunnel_hostname") or current_credentials.get("tunnel_hostname") or "").strip()
    merged_box_api_token = str(response.get("box_api_token") or box_api_token).strip()
    merged_internal_url = str(response.get("internal_url") or current_credentials.get("internal_url") or "").strip()
    merged_external_url = str(response.get("external_url") or current_credentials.get("external_url") or "").strip()
    raw_hostname = str(response.get("hostname") or current_credentials.get("hostname") or "").strip()
    merged_config_version = max(
        to_positive_int(response.get("config_version", current_config_version(current_credentials)), 0),
        0,
    )

    if not merged_cloudflare_tunnel_token:
        raise StudioSyncError("Studio config sync did not yield a tunnel token.")
    if not merged_tunnel_hostname:
        raise StudioSyncError("Studio config sync did not yield a tunnel hostname.")
    if not merged_box_api_token:
        raise StudioSyncError("Studio config sync did not yield a box_api_token.")
    if not merged_internal_url:
        raise StudioSyncError("Studio config sync did not yield an internal_url.")
    if not merged_external_url:
        raise StudioSyncError("Studio config sync did not yield an external_url.")

    try:
        validated_hostname = normalize_hostname(raw_hostname) if raw_hostname else ""
        validated_internal_url = normalize_internal_url(merged_internal_url)
        validated_external_url = normalize_external_url(merged_external_url)
    except AuthStorageError as exc:
        raise StudioSyncError(str(exc)) from exc

    changed = any((
        merged_cloudflare_tunnel_token != current_credentials.get("cloudflare_tunnel_token", "").strip(),
        merged_tunnel_hostname != current_credentials.get("tunnel_hostname", "").strip(),
        merged_box_api_token != box_api_token,
        validated_internal_url != current_credentials.get("internal_url", "").strip(),
        validated_external_url != current_credentials.get("external_url", "").strip(),
        validated_hostname != current_credentials.get("hostname", "").strip(),
        merged_config_version != current_config_version(current_credentials),
    ))

    try:
        if validated_hostname:
            sync_homeassistant_hostname(validated_hostname)
        persist_credentials(
            merged_cloudflare_tunnel_token,
            merged_tunnel_hostname,
            merged_box_api_token,
            validated_internal_url,
            validated_external_url,
            hostname=validated_hostname,
            config_version=merged_config_version,
        )
        sync_homeassistant_urls(validated_internal_url, validated_external_url)
        verify_applied_homeassistant_state(
            expected_hostname=validated_hostname,
            expected_internal_url=validated_internal_url,
            expected_external_url=validated_external_url,
            target="studio_config_sync",
        )

        # Sync SSH authorized keys from Studio (only if returned and changed)
        ssh_keys = response.get("ssh_authorized_keys")
        if isinstance(ssh_keys, list):
            prev_keys = _read_studio_synced_ssh_keys()
            if sorted(k.strip() for k in ssh_keys if k.strip()) != sorted(k.strip() for k in prev_keys if k.strip()):
                write_authorized_keys(ssh_keys)
                update_sync_state(last_ssh_authorized_keys=ssh_keys)
                log(f"Updated authorized_keys with {len(ssh_keys)} Studio key(s).")

    except (AuthStorageError, SupervisorAPIError) as exc:
        try:
            request_config_sync(
                build_config_sync_payload(
                    credentials=read_saved_credentials(),
                    reported_config_version=merged_config_version,
                    reported_apply_status="error",
                    reported_apply_error=str(exc),
                )
            )
        except StudioSyncError:
            pass
        raise

    ack_status_code, ack_response = request_config_sync(
        build_config_sync_payload(
            credentials=read_saved_credentials(),
            reported_config_version=merged_config_version,
            reported_apply_status="applied" if changed else "unchanged",
        )
    )
    if ack_status_code != 200:
        raise StudioSyncError(f"Studio config sync acknowledgement returned unexpected HTTP {ack_status_code}.")

    ack_response_status = str(ack_response.get("status", "")).strip().lower()
    if ack_response_status and ack_response_status not in {"ok", "accepted", "updated", "unchanged"}:
        raise StudioSyncError("Studio config sync acknowledgement returned unexpected status payload.")

    return {
        "status": response_status or ("updated" if changed else "unchanged"),
        "changed": changed,
        "internal_url": validated_internal_url,
        "external_url": validated_external_url,
        "tunnel_hostname": merged_tunnel_hostname,
        "hostname": validated_hostname,
        "config_version": merged_config_version,
    }



# ---------------------------------------------------------------------------
# Supervisor & Core Management
# ---------------------------------------------------------------------------

def supervisor_request(method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    return supervisor_request_raw(
        method, path, payload,
        token=SUPERVISOR_TOKEN, base_url=SUPERVISOR_URL,
        error_class=SupervisorAPIError,
    )


def _apply_urls_to_config(config_doc: dict[str, Any], internal_url: str, external_url: str) -> dict[str, Any]:
    """Apply URL changes to a core config document (used inside run_with_core_stopped)."""
    config_data = config_doc.get("data")
    if not isinstance(config_data, dict):
        raise SupervisorAPIError("Home Assistant core config storage is invalid.")
    if internal_url:
        config_data["internal_url"] = internal_url
    if external_url:
        config_data["external_url"] = external_url
    return {
        "internal_url": str(config_data.get("internal_url") or "").strip(),
        "external_url": str(config_data.get("external_url") or "").strip(),
    }


def sync_homeassistant_core_urls(*, internal_url: str = "", external_url: str = "") -> dict[str, str]:
    normalized_internal_url = normalize_internal_url(internal_url) if str(internal_url).strip() else ""
    normalized_external_url = normalize_external_url(external_url) if str(external_url).strip() else ""
    if not normalized_internal_url and not normalized_external_url:
        raise AuthStorageError("At least one Home Assistant URL must be provided.")

    live_urls = read_live_core_urls()
    current_internal_url = str(live_urls.get("internal_url", "")).strip()
    current_external_url = str(live_urls.get("external_url", "")).strip()
    if (
        (not normalized_internal_url or normalized_internal_url == current_internal_url)
        and (not normalized_external_url or normalized_external_url == current_external_url)
    ):
        return {
            "internal_url": current_internal_url,
            "external_url": current_external_url,
        }

    def mutator(config_doc: dict[str, Any]) -> dict[str, Any]:
        config_data = config_doc.get("data")
        if not isinstance(config_data, dict):
            raise SupervisorAPIError("Home Assistant core config storage is invalid.")
        if normalized_internal_url:
            config_data["internal_url"] = normalized_internal_url
        if normalized_external_url:
            config_data["external_url"] = normalized_external_url
        return {
            "internal_url": str(config_data.get("internal_url") or "").strip(),
            "external_url": str(config_data.get("external_url") or "").strip(),
        }

    return run_with_core_stopped(lambda: mutate_core_config_storage(mutator))


def sync_homeassistant_urls(internal_url: str, external_url: str) -> str:
    result = sync_homeassistant_core_urls(internal_url=internal_url, external_url=external_url)
    normalized_external_url = normalize_external_url(result.get("external_url", ""))

    return normalized_external_url


def apply_saved_homeassistant_host_settings(*, target: str = "startup_saved_config") -> dict[str, str]:
    credentials = read_saved_credentials()
    hostname = str(credentials.get("hostname", "")).strip()
    internal_url = str(credentials.get("internal_url", "")).strip()
    external_url = str(credentials.get("external_url", "")).strip()

    if not hostname and not internal_url and not external_url:
        raise AuthStorageError("No stored Home Assistant hostname or URLs found.")

    normalized_hostname = normalize_hostname(hostname) if hostname else ""
    normalized_internal_url = normalize_internal_url(internal_url) if internal_url else ""
    normalized_external_url = normalize_external_url(external_url) if external_url else ""

    if normalized_hostname:
        sync_homeassistant_hostname(normalized_hostname)
    applied_urls = sync_homeassistant_core_urls(
        internal_url=normalized_internal_url,
        external_url=normalized_external_url,
    )
    observed = verify_applied_homeassistant_state(
        expected_hostname=normalized_hostname,
        expected_internal_url=normalized_internal_url,
        expected_external_url=normalized_external_url,
        target=target,
    )

    update_sync_state(
        desired_config_version=current_config_version(credentials),
        applied_config_version=current_config_version(credentials),
        last_config_reconcile_at=utcnow_iso(),
        last_config_reconcile_status="applied",
        last_config_reconcile_error="",
    )

    return {
        "hostname": observed.get("hostname", ""),
        "internal_url": observed.get("internal_url", applied_urls.get("internal_url", "")),
        "external_url": observed.get("external_url", applied_urls.get("external_url", "")),
    }


def get_current_host_hostname() -> str:
    info = supervisor_request("GET", "/host/info")
    data = info.get("data", {}) if isinstance(info, dict) else {}
    if not isinstance(data, dict):
        raise SupervisorAPIError("Invalid /host/info response from Supervisor.")
    raw_hostname = str(data.get("hostname", "") or "").strip()
    if not raw_hostname:
        return ""
    return normalize_hostname(raw_hostname)


def sync_homeassistant_hostname(hostname: str) -> str:
    normalized_hostname = normalize_hostname(hostname)
    try:
        current_hostname = get_current_host_hostname()
    except SupervisorAPIError:
        current_hostname = ""
    if current_hostname == normalized_hostname:
        return normalized_hostname
    supervisor_request("POST", "/host/options", {"hostname": normalized_hostname})
    return normalized_hostname


def apply_studio_configuration_locally(payload: dict[str, Any]) -> dict[str, Any]:
    current_credentials = read_saved_credentials()
    box_api_token = current_credentials.get("box_api_token", "").strip()
    if not box_api_token:
        raise StudioSyncError("No box_api_token available. Pair app with Studio first.")

    tunnel_token = str(
        payload.get("cloudflare_tunnel_token") or current_credentials.get("cloudflare_tunnel_token") or ""
    ).strip()
    tunnel_hostname = str(payload.get("tunnel_hostname") or current_credentials.get("tunnel_hostname") or "").strip()
    internal_url = str(payload.get("internal_url") or current_credentials.get("internal_url") or "").strip()
    external_url = str(payload.get("external_url") or current_credentials.get("external_url") or "").strip()
    raw_hostname = str(payload.get("hostname") or current_credentials.get("hostname") or "").strip()
    config_version = max(
        to_positive_int(payload.get("config_version", current_config_version(current_credentials)), 0),
        0,
    )

    if not tunnel_token:
        raise StudioSyncError("Studio config apply did not include a tunnel token.")
    if not tunnel_hostname:
        raise StudioSyncError("Studio config apply did not include a tunnel hostname.")
    if not internal_url:
        raise StudioSyncError("Studio config apply did not include an internal_url.")
    if not external_url:
        raise StudioSyncError("Studio config apply did not include an external_url.")

    validated_hostname = normalize_hostname(raw_hostname) if raw_hostname else ""
    validated_internal_url = normalize_internal_url(internal_url)
    validated_external_url = normalize_external_url(external_url)
    persist_credentials(
        tunnel_token,
        tunnel_hostname,
        box_api_token,
        validated_internal_url,
        validated_external_url,
        hostname=validated_hostname,
        config_version=config_version,
    )
    if validated_hostname:
        sync_homeassistant_hostname(validated_hostname)
    applied_external_url = sync_homeassistant_urls(validated_internal_url, validated_external_url)
    verify_applied_homeassistant_state(
        expected_hostname=validated_hostname,
        expected_internal_url=validated_internal_url,
        expected_external_url=validated_external_url,
        target="studio_push_apply",
    )

    # Sync SSH authorized keys from push payload
    ssh_keys = payload.get("ssh_authorized_keys", [])
    if isinstance(ssh_keys, list) and ssh_keys:
        write_authorized_keys(ssh_keys)
        log(f"Config push: updated authorized_keys with {len(ssh_keys)} Studio key(s).")

    return {
        "status": "applied",
        "config_version": config_version,
        "internal_url": validated_internal_url,
        "external_url": applied_external_url,
        "tunnel_hostname": tunnel_hostname,
        "hostname": validated_hostname,
    }


def get_core_state() -> str:
    info = supervisor_request("GET", "/core/info")
    data = info.get("data", {}) if isinstance(info, dict) else {}
    if not isinstance(data, dict):
        raise SupervisorAPIError("Invalid /core/info response from Supervisor.")
    state = str(data.get("state", "")).strip().lower()
    if not state:
        raise SupervisorAPIError("Could not determine Home Assistant Core state.")
    return state


def is_homeassistant_core_api_reachable() -> bool:
    try:
        supervisor_request("GET", "/core/api/config")
        return True
    except SupervisorAPIError:
        return False


def desired_configuration_from_credentials(credentials: dict[str, str] | None = None) -> dict[str, str]:
    source = credentials if credentials is not None else read_saved_credentials()
    desired_hostname = str(source.get("hostname", "")).strip()
    desired_internal_url = str(source.get("internal_url", "")).strip()
    desired_external_url = str(source.get("external_url", "")).strip()
    return {
        "hostname": normalize_hostname(desired_hostname) if desired_hostname else "",
        "internal_url": normalize_internal_url(desired_internal_url) if desired_internal_url else "",
        "external_url": normalize_external_url(desired_external_url) if desired_external_url else "",
        "config_version": str(source.get("config_version", "")).strip(),
    }


def detect_config_drift(credentials: dict[str, str] | None = None) -> dict[str, dict[str, str]]:
    desired = desired_configuration_from_credentials(credentials)
    live_state = read_live_box_state(credentials)
    drift: dict[str, dict[str, str]] = {}
    for field in ("hostname", "internal_url", "external_url"):
        desired_value = desired.get(field, "")
        live_value = str(live_state.get(field, "")).strip()
        if not desired_value:
            continue
        if desired_value != live_value:
            drift[field] = {"desired": desired_value, "live": live_value}
    return drift



# ---------------------------------------------------------------------------
# Health Monitoring
# ---------------------------------------------------------------------------

def is_cloudflared_running() -> bool:
    try:
        result = subprocess.run(
            ["pgrep", "-f", "cloudflared tunnel"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def read_storage_usage(path: Path) -> dict[str, int]:
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return {"total_bytes": 0, "used_bytes": 0, "free_bytes": 0}
    return {
        "total_bytes": int(usage.total),
        "used_bytes": int(usage.used),
        "free_bytes": int(usage.free),
    }


def collect_health_snapshot() -> dict[str, Any]:
    credentials = read_saved_credentials()
    sync_state = read_sync_state()
    live_state = read_live_box_state(credentials)
    desired_state = desired_configuration_from_credentials(credentials)
    drift = detect_config_drift(credentials)
    apply_status = str(sync_state.get("last_apply_status", "")).strip().lower()
    apply_error = str(sync_state.get("last_apply_error", "")).strip()

    core_state = ""
    homeassistant_version = ""
    homeassistant_reachable = False
    try:
        core_info = supervisor_request("GET", "/core/info")
        data = core_info.get("data", {}) if isinstance(core_info, dict) else {}
        if isinstance(data, dict):
            homeassistant_version = str(data.get("version", "")).strip()
            core_state = str(data.get("state", "")).strip().lower()
    except SupervisorAPIError:
        pass
    homeassistant_reachable = is_homeassistant_core_api_reachable()
    if not core_state and homeassistant_reachable:
        core_state = "running"

    auth_user_count = 0
    auth_storage_error = ""
    try:
        auth_user_count = len(list_homeassistant_hash_users())
    except AuthStorageError as exc:
        auth_storage_error = exc.message

    snapshot = {
        "reported_at": utcnow_iso(),
        "status": "ok",
        "paired": has_saved_pairing_credentials(),
        "addon_version": ADDON_VERSION,
        "homeassistant_version": homeassistant_version,
        "core_state": core_state,
        "homeassistant_reachable": homeassistant_reachable,
        "cloudflared_running": is_cloudflared_running(),
        "desired_config_version": current_config_version(credentials),
        "applied_config_version": max(to_positive_int(sync_state.get("applied_config_version", 0), 0), 0),
        "desired_state": desired_state,
        "live_state": {
            "hostname": live_state.get("hostname", ""),
            "internal_url": live_state.get("internal_url", ""),
            "external_url": live_state.get("external_url", ""),
        },
        "config_drift": drift,
        "last_syncs": {
            "config": str(sync_state.get("last_config_sync_at", "")).strip(),
            "config_reconcile": str(sync_state.get("last_config_reconcile_at", "")).strip(),
            "auth": str(sync_state.get("last_auth_sync_at", "")).strip(),
            "heartbeat": str(sync_state.get("last_heartbeat_at", "")).strip(),
            "inventory": str(sync_state.get("last_inventory_at", "")).strip(),
        },
        "last_apply": {
            "at": str(sync_state.get("last_apply_at", "")).strip(),
            "status": apply_status,
            "target": str(sync_state.get("last_apply_target", "")).strip(),
            "error": apply_error,
            "expected": sync_state.get("last_apply_expected", {}),
            "observed": sync_state.get("last_apply_observed", {}),
        },
        "storage": {
            "config": read_storage_usage(HA_CONFIG_DIR),
            "data": read_storage_usage(Path("/data")),
        },
        "auth_user_count": auth_user_count,
        "auth_storage_error": auth_storage_error,
    }
    if not homeassistant_reachable or drift or apply_status in {"error", "warning"}:
        snapshot["status"] = "degraded"
    return snapshot


def send_state_report(report_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    if not SYNC_STATE_REPORTS_ENABLED:
        return {"status": "disabled"}

    sync_state = read_sync_state()
    if sync_state.get("studio_state_report_support") == "unsupported":
        return {"status": "unsupported"}

    base_url = get_studio_base_url()
    if not is_valid_https_url(base_url):
        raise StudioSyncError("studio_base_url must use HTTPS.")

    credentials = read_saved_credentials()
    box_api_token = credentials.get("box_api_token", "").strip()
    if not box_api_token:
        raise StudioSyncError("No box_api_token available. Pair app with Studio first.")

    report_payload = {
        "report_type": str(report_type).strip().lower(),
        "reported_at": utcnow_iso(),
        "source": "home_assistant_addon",
        "addon_version": ADDON_VERSION,
    }
    report_payload.update(payload)

    try:
        status_code, response = post_json(
            f"{base_url}{STATE_REPORT_PATH}",
            report_payload,
            headers={"Authorization": f"Bearer {box_api_token}"},
        )
    except PairingAPIError as exc:
        if exc.status_code == 404:
            update_sync_state(studio_state_report_support="unsupported")
            return {"status": "unsupported"}
        if exc.status_code == 401:
            raise StudioSyncError("Studio rejected box_api_token for state report (401). Re-pair app.") from exc
        if exc.status_code == 429:
            raise StudioSyncError("Studio state report rate limited (429). Try again shortly.") from exc
        if exc.status_code:
            raise StudioSyncError(f"Studio state report failed (HTTP {exc.status_code}).") from exc
        raise StudioSyncError(exc.message) from exc

    if status_code != 200:
        raise StudioSyncError(f"Studio state report returned unexpected HTTP {status_code}.")

    update_sync_state(studio_state_report_support="supported")
    return {
        "status": str(response.get("status", "ok")).strip().lower() or "ok",
        "report_id": str(response.get("report_id", "")).strip(),
    }


def run_config_sync_once(*, trigger: str = "") -> dict[str, Any]:
    try:
        result = sync_addon_configuration_from_studio()
    except (StudioSyncError, AuthStorageError, SupervisorAPIError) as exc:
        update_sync_state(
            desired_config_version=current_config_version(),
            last_apply_status="error",
            last_apply_target="studio_config_sync",
            last_apply_error=str(exc),
            last_config_sync_at=utcnow_iso(),
            last_config_sync_status="error",
            last_config_sync_error=str(exc),
        )
        raise

    update_sync_state(
        desired_config_version=max(to_positive_int(result.get("config_version", 0), 0), current_config_version()),
        applied_config_version=max(to_positive_int(result.get("config_version", 0), 0), 0),
        last_config_sync_at=utcnow_iso(),
        last_config_sync_status=str(result.get("status", "ok")).strip().lower() or "ok",
        last_config_sync_error="",
    )
    if trigger:
        log(f"Config sync completed via {trigger}: status={result.get('status', 'ok')}")
    return result


def run_auth_sync_once(*, trigger: str = "") -> dict[str, Any]:
    try:
        users = list_homeassistant_hash_users()
        auth_snapshot_hash = compute_auth_snapshot_hash(users)
        result = sync_auth_hashes_to_studio(users=users)
    except (StudioSyncError, AuthStorageError) as exc:
        update_sync_state(
            last_auth_sync_at=utcnow_iso(),
            last_auth_sync_status="error",
            last_auth_sync_error=str(exc),
        )
        raise

    update_sync_state(
        last_auth_sync_at=utcnow_iso(),
        last_auth_sync_status=str(result.get("status", "ok")).strip().lower() or "ok",
        last_auth_sync_error="",
        last_auth_snapshot_hash=auth_snapshot_hash,
        last_auth_observed_at=utcnow_iso(),
    )
    if trigger:
        log(
            "Auth sync completed via "
            f"{trigger}: synced={result.get('synced_count', 0)} received={result.get('received_count', 0)}"
        )
    return result


def reconcile_desired_configuration(*, trigger: str = "") -> dict[str, Any]:
    if not has_saved_pairing_credentials():
        result = {"status": "skipped", "reason": "not_paired", "drift": {}}
        update_sync_state(
            desired_config_version=current_config_version(),
            last_config_reconcile_at=utcnow_iso(),
            last_config_reconcile_status="skipped",
            last_config_reconcile_error="",
        )
        return result

    credentials = read_saved_credentials()
    drift = detect_config_drift(credentials)
    if not drift:
        update_sync_state(
            desired_config_version=current_config_version(credentials),
            applied_config_version=current_config_version(credentials),
            last_config_reconcile_at=utcnow_iso(),
            last_config_reconcile_status="unchanged",
            last_config_reconcile_error="",
        )
        return {"status": "unchanged", "drift": {}}

    desired = desired_configuration_from_credentials(credentials)
    try:
        if desired.get("hostname"):
            sync_homeassistant_hostname(desired["hostname"])
        sync_homeassistant_core_urls(
            internal_url=desired.get("internal_url", ""),
            external_url=desired.get("external_url", ""),
        )
        verify_applied_homeassistant_state(
            expected_hostname=desired.get("hostname", ""),
            expected_internal_url=desired.get("internal_url", ""),
            expected_external_url=desired.get("external_url", ""),
            target="config_reconcile",
        )
    except (SupervisorAPIError, AuthStorageError) as exc:
        update_sync_state(
            desired_config_version=current_config_version(credentials),
            last_apply_status="error",
            last_apply_target="config_reconcile",
            last_apply_error=str(exc),
            last_config_reconcile_at=utcnow_iso(),
            last_config_reconcile_status="error",
            last_config_reconcile_error=str(exc),
        )
        raise

    update_sync_state(
        desired_config_version=current_config_version(credentials),
        applied_config_version=current_config_version(credentials),
        last_config_reconcile_at=utcnow_iso(),
        last_config_reconcile_status="corrected",
        last_config_reconcile_error="",
    )
    try:
        send_state_report(
            "event",
            {
                "event_type": "config_drift_corrected",
                "trigger": trigger or "scheduler",
                "config_version": current_config_version(credentials),
                "drift": drift,
            },
        )
    except StudioSyncError as exc:
        log(f"State report failed after config drift correction: {exc}")
    return {"status": "corrected", "drift": drift}


def run_health_probe_once(*, trigger: str = "") -> dict[str, Any]:
    snapshot = collect_health_snapshot()
    set_latest_health_snapshot(snapshot)
    update_sync_state(
        desired_config_version=max(
            current_config_version(),
            to_positive_int(snapshot.get("desired_config_version", 0), 0),
        ),
        applied_config_version=max(to_positive_int(snapshot.get("applied_config_version", 0), 0), 0),
        last_health_probe_at=snapshot["reported_at"],
        last_health_status=str(snapshot.get("status", "ok")).strip().lower() or "ok",
        last_health_error="",
    )
    if trigger:
        log(
            "Health probe completed via "
            f"{trigger}: status={snapshot.get('status', 'ok')} cloudflared_running={snapshot.get('cloudflared_running')}"
        )
    return snapshot


def run_heartbeat_once(*, trigger: str = "") -> dict[str, Any]:
    snapshot = run_health_probe_once(trigger=trigger or "heartbeat")
    payload = {
        "desired_config_version": snapshot.get("desired_config_version", 0),
        "applied_config_version": snapshot.get("applied_config_version", 0),
        "health": {
            "core_state": snapshot.get("core_state", ""),
            "homeassistant_reachable": snapshot.get("homeassistant_reachable", False),
            "cloudflared_running": snapshot.get("cloudflared_running", False),
            "config_drift_count": len(snapshot.get("config_drift", {})),
        },
        "last_syncs": snapshot.get("last_syncs", {}),
    }
    try:
        report_result = send_state_report("heartbeat", payload)
    except StudioSyncError as exc:
        update_sync_state(
            last_heartbeat_at=utcnow_iso(),
            last_heartbeat_status="error",
            last_heartbeat_error=str(exc),
        )
        raise

    update_sync_state(
        last_heartbeat_at=utcnow_iso(),
        last_heartbeat_status=str(report_result.get("status", "ok")).strip().lower() or "ok",
        last_heartbeat_error="",
    )
    return report_result


def run_inventory_once(*, trigger: str = "") -> dict[str, Any]:
    snapshot = run_health_probe_once(trigger=trigger or "inventory")
    payload = {
        "desired_config_version": snapshot.get("desired_config_version", 0),
        "applied_config_version": snapshot.get("applied_config_version", 0),
        "homeassistant_version": snapshot.get("homeassistant_version", ""),
        "core_state": snapshot.get("core_state", ""),
        "desired_state": snapshot.get("desired_state", {}),
        "live_state": snapshot.get("live_state", {}),
        "config_drift": snapshot.get("config_drift", {}),
        "storage": snapshot.get("storage", {}),
        "auth_user_count": snapshot.get("auth_user_count", 0),
    }
    try:
        report_result = send_state_report("inventory", payload)
    except StudioSyncError as exc:
        update_sync_state(
            last_inventory_at=utcnow_iso(),
            last_inventory_status="error",
            last_inventory_error=str(exc),
        )
        raise

    update_sync_state(
        last_inventory_at=utcnow_iso(),
        last_inventory_status=str(report_result.get("status", "ok")).strip().lower() or "ok",
        last_inventory_error="",
    )
    return report_result



# ---------------------------------------------------------------------------
# Background Sync Workers
# ---------------------------------------------------------------------------

def enqueue_sync_job(name: str, *, reason: str = "") -> None:
    normalized_name = str(name).strip().lower()
    if not normalized_name:
        return
    with _sync_pending_jobs_lock:
        if normalized_name in _sync_pending_jobs:
            return
        _sync_pending_jobs.add(normalized_name)
    _sync_job_queue.put({"name": normalized_name, "reason": reason})


def _mark_sync_job_done(name: str) -> None:
    with _sync_pending_jobs_lock:
        _sync_pending_jobs.discard(name)


def run_sync_job(name: str, *, reason: str = "") -> dict[str, Any]:
    if name == "config_pull":
        return run_config_sync_once(trigger=reason or "scheduler")
    if name == "config_reconcile":
        return reconcile_desired_configuration(trigger=reason or "scheduler")
    if name == "auth_sync":
        return run_auth_sync_once(trigger=reason or "scheduler")
    if name == "health_probe":
        return run_health_probe_once(trigger=reason or "scheduler")
    if name == "heartbeat":
        return run_heartbeat_once(trigger=reason or "scheduler")
    if name == "inventory":
        return run_inventory_once(trigger=reason or "scheduler")
    raise StudioSyncError(f"Unknown sync job: {name}")


def sync_worker_loop() -> None:
    while True:
        job = _sync_job_queue.get()
        job_name = str(job.get("name", "")).strip().lower()
        job_reason = str(job.get("reason", "")).strip()
        try:
            run_sync_job(job_name, reason=job_reason)
        except (StudioSyncError, AuthStorageError, SupervisorAPIError) as exc:
            log(f"Sync job failed: name={job_name} reason={job_reason!r} error={exc}")
        except Exception as exc:
            log(f"Sync job unexpected error: name={job_name} reason={job_reason!r} error={type(exc).__name__}: {exc}")
        finally:
            _mark_sync_job_done(job_name)
            _sync_job_queue.task_done()


def sync_scheduler_loop() -> None:
    last_auth_check_at = 0.0
    while True:
        now = time.time()
        state = read_sync_state()

        if should_run_periodic(str(state.get("last_health_probe_at", "")), HEALTH_PROBE_INTERVAL_SECONDS, now=now):
            enqueue_sync_job("health_probe", reason="scheduler:health")

        if has_saved_pairing_credentials():
            if should_run_periodic(str(state.get("last_config_reconcile_at", "")), CONFIG_RECONCILE_INTERVAL_SECONDS, now=now):
                enqueue_sync_job("config_reconcile", reason="scheduler:reconcile")

            pull_interval = CONFIG_PULL_INTERVAL_SECONDS
            if PERIODIC_AUTH_SYNC_ENABLED:
                pull_interval = min(CONFIG_PULL_INTERVAL_SECONDS, PERIODIC_AUTH_SYNC_INTERVAL_SECONDS)
            if should_run_periodic(str(state.get("last_config_sync_at", "")), pull_interval, now=now):
                enqueue_sync_job("config_pull", reason="scheduler:config-pull")

            if now - last_auth_check_at >= AUTH_WATCH_INTERVAL_SECONDS:
                last_auth_check_at = now
                try:
                    users = list_homeassistant_hash_users()
                    snapshot_hash = compute_auth_snapshot_hash(users)
                    update_sync_state(last_auth_observed_at=utcnow_iso())
                    if (
                        snapshot_hash != str(state.get("last_auth_snapshot_hash", "")).strip()
                        or not str(state.get("last_auth_sync_at", "")).strip()
                    ):
                        enqueue_sync_job("auth_sync", reason="scheduler:auth-change")
                except AuthStorageError as exc:
                    update_sync_state(
                        last_auth_observed_at=utcnow_iso(),
                        last_auth_sync_status="error",
                        last_auth_sync_error=str(exc),
                    )

            if should_run_periodic(str(state.get("last_heartbeat_at", "")), HEARTBEAT_INTERVAL_SECONDS, now=now):
                enqueue_sync_job("heartbeat", reason="scheduler:heartbeat")
            if should_run_periodic(str(state.get("last_inventory_at", "")), INVENTORY_INTERVAL_SECONDS, now=now):
                enqueue_sync_job("inventory", reason="scheduler:inventory")

        time.sleep(1)


def wait_for_core_state(target_states: set[str], timeout_seconds: int = 180) -> str:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        current = get_core_state()
        if current in target_states:
            return current
        time.sleep(2)
    expected = ", ".join(sorted(target_states))
    raise SupervisorAPIError(f"Timed out waiting for Home Assistant Core state: {expected}.")


def wait_for_homeassistant_api_reachability(desired_reachable: bool, timeout_seconds: int = 180) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if is_homeassistant_core_api_reachable() == desired_reachable:
            return
        time.sleep(2)
    if desired_reachable:
        raise SupervisorAPIError("Timed out waiting for Home Assistant Core API to become reachable.")
    raise SupervisorAPIError("Timed out waiting for Home Assistant Core API to stop responding.")


def _ensure_core_started() -> None:
    """Restart HA Core with retries. Raises on final failure."""
    max_attempts = 2
    timeout_per_attempt = 300  # 5 minutes per attempt
    for attempt in range(1, max_attempts + 1):
        try:
            if is_homeassistant_core_api_reachable():
                log("Core is reachable.")
                return
            log(f"Core not reachable, sending /core/start (attempt {attempt}/{max_attempts}, timeout {timeout_per_attempt}s)...")
            supervisor_request("POST", "/core/start")
            wait_for_homeassistant_api_reachability(True, timeout_seconds=timeout_per_attempt)
            log("Core started successfully.")
            return
        except (SupervisorAPIError, Exception) as exc:
            log(f"Core start attempt {attempt}/{max_attempts} failed: {exc}")
            if attempt < max_attempts:
                time.sleep(10)
    error_msg = f"CRITICAL: Failed to start Home Assistant Core after {max_attempts} attempts."
    log(error_msg)
    raise SupervisorAPIError(error_msg)


def run_with_core_stopped(operation: Callable[[], Any]) -> Any:
    core_was_running = False
    operation_error = None
    result = None
    try:
        core_was_running = is_homeassistant_core_api_reachable()
        if core_was_running:
            supervisor_request("POST", "/core/stop")
            wait_for_homeassistant_api_reachability(False)
        result = operation()
    except Exception as exc:
        operation_error = exc
    finally:
        if core_was_running:
            try:
                _ensure_core_started()
            except SupervisorAPIError as start_error:
                # If both operation AND start failed, log the start failure
                # but raise the original operation error
                if operation_error is not None:
                    log(f"Core restart also failed: {start_error}")
                else:
                    raise
    if operation_error is not None:
        raise operation_error
    return result


def mutate_auth_storage(mutator: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]) -> dict[str, Any]:
    def operation() -> dict[str, Any]:
        auth_doc, provider_doc = read_auth_storage_documents()
        result = mutator(auth_doc, provider_doc)
        write_json_file(AUTH_STORAGE_FILE, auth_doc)
        write_json_file(AUTH_PROVIDER_STORAGE_FILE, provider_doc)
        return result

    return run_with_core_stopped(operation)


def mutate_core_config_storage(mutator: Callable[[dict[str, Any]], dict[str, Any]]) -> dict[str, Any]:
    """Mutate core config storage on disk.

    IMPORTANT: Callers MUST wrap this in ``run_with_core_stopped()`` to
    prevent data corruption while Home Assistant Core is running.
    """
    if is_homeassistant_core_api_reachable():
        raise SupervisorAPIError(
            "Cannot mutate core config while Home Assistant Core is running. "
            "Wrap this call in run_with_core_stopped()."
        )
    config_doc = read_core_config_document()
    result = mutator(config_doc)
    write_json_file(CORE_CONFIG_STORAGE_FILE, config_doc)
    return result


def create_user_with_hash(
    *,
    username: str,
    password_hash: str,
    display_name: str,
    system_generated: bool,
    local_only: bool,
) -> dict[str, Any]:
    normalized_username = validate_username(username)
    validate_precomputed_password_hash(password_hash)

    if not display_name:
        display_name = username

    def mutator(auth_doc: dict[str, Any], provider_doc: dict[str, Any]) -> dict[str, Any]:
        auth_data = require_dict(auth_doc.get("data"), "auth storage data")
        provider_data = require_dict(provider_doc.get("data"), "auth provider data")

        users = ensure_list(auth_data, "users", "auth storage users")
        groups = ensure_list(auth_data, "groups", "auth storage groups")
        credentials = ensure_list(auth_data, "credentials", "auth storage credentials")
        provider_users = ensure_list(provider_data, "users", "auth provider users")

        for provider_user in provider_users:
            if not isinstance(provider_user, dict):
                continue
            existing_username = str(provider_user.get("username", "")).strip()
            if normalize_username(existing_username) == normalized_username:
                raise AuthStorageError("Username already exists in Home Assistant auth provider.")

        for credential in credentials:
            if not isinstance(credential, dict):
                continue
            if credential.get("auth_provider_type") != "homeassistant":
                continue
            credential_data = credential.get("data")
            if not isinstance(credential_data, dict):
                continue
            existing_username = str(credential_data.get("username", "")).strip()
            if normalize_username(existing_username) == normalized_username:
                raise AuthStorageError("Username already exists in Home Assistant credentials.")

        group_ids_available = {
            str(group.get("id", ""))
            for group in groups
            if isinstance(group, dict) and group.get("id") is not None
        }
        group_ids = [GROUP_ID_USER] if GROUP_ID_USER in group_ids_available else []

        user_id = uuid.uuid4().hex
        credential_id = uuid.uuid4().hex

        users.append(
            {
                "id": user_id,
                "group_ids": group_ids,
                "is_owner": False,
                "is_active": True,
                "name": display_name,
                "system_generated": system_generated,
                "local_only": local_only,
            }
        )
        credentials.append(
            {
                "id": credential_id,
                "user_id": user_id,
                "auth_provider_type": "homeassistant",
                "auth_provider_id": None,
                "data": {
                    "username": username,
                },
            }
        )
        provider_users.append(
            {
                "username": username,
                "password": password_hash,
            }
        )

        return {
            "user_id": user_id,
            "credential_id": credential_id,
            "username": username,
        }

    try:
        return mutate_auth_storage(mutator)
    except SupervisorAPIError as exc:
        raise AuthStorageError(exc.message) from exc


def read_managed_service_user_config() -> dict[str, str]:
    config = read_json_file(MANAGED_SERVICE_USER_FILE)
    if not config:
        return {}
    username = str(config.get("username", "")).strip()
    password_hash = str(config.get("password_hash", "")).strip()
    display_name = str(config.get("display_name", "")).strip()
    if not username or not password_hash:
        return {}
    return {
        "username": username,
        "password_hash": password_hash,
        "display_name": display_name or username,
    }


def write_managed_service_user_config(config: dict[str, str]) -> None:
    write_secret_file(MANAGED_SERVICE_USER_FILE, json.dumps(config, ensure_ascii=True, separators=(",", ":")) + "\n")


def managed_service_user_status(export_rows: list[dict[str, Any]]) -> str:
    config = read_managed_service_user_config()
    if not config:
        return "No managed internal service user configured."

    normalized = normalize_username(config["username"])
    for row in export_rows:
        if normalize_username(str(row.get("username", ""))) != normalized:
            continue
        if row.get("system_generated", False):
            return f"Managed service user '{config['username']}' is present."
        return f"Managed username '{config['username']}' exists but is not hidden (system_generated=false)."
    return f"Managed service user '{config['username']}' is configured but currently missing."


def ensure_managed_service_user() -> tuple[str, dict[str, Any] | None]:
    config = read_managed_service_user_config()
    if not config:
        raise AuthStorageError("No managed service user is configured yet.")

    rows = list_homeassistant_hash_users()
    normalized = normalize_username(config["username"])
    for row in rows:
        if normalize_username(str(row.get("username", ""))) != normalized:
            continue
        if row.get("system_generated", False):
            return "present", None
        raise AuthStorageError("Managed username exists but is not a hidden system-generated user.")

    created = create_user_with_hash(
        username=config["username"],
        password_hash=config["password_hash"],
        display_name=config["display_name"],
        system_generated=True,
        local_only=True,
    )
    return "created", created


def managed_service_user_watchdog_loop() -> None:
    while True:
        time.sleep(SERVICE_USER_WATCHDOG_INTERVAL_SECONDS)
        try:
            if not read_managed_service_user_config():
                continue
            status, _created = ensure_managed_service_user()
            if status == "created":
                try:
                    run_auth_sync_once(trigger="managed-watchdog")
                except (StudioSyncError, AuthStorageError) as exc:
                    log(f"Managed service auth sync failed: {exc}")
        except (AuthStorageError, SupervisorAPIError) as exc:
            log(f"Managed service user watchdog failed: {exc}")
            continue


def start_managed_service_user_watchdog() -> None:
    global _watchdog_started
    if not SERVICE_USER_WATCHDOG_ENABLED:
        return

    with _watchdog_lock:
        if _watchdog_started:
            return
        watchdog_thread = threading.Thread(target=managed_service_user_watchdog_loop, daemon=True)
        watchdog_thread.start()
        _watchdog_started = True


def start_periodic_auth_sync() -> None:
    global _periodic_auth_sync_started

    with _periodic_auth_sync_lock:
        if _periodic_auth_sync_started:
            return
        worker_thread = threading.Thread(target=sync_worker_loop, daemon=True)
        scheduler_thread = threading.Thread(target=sync_scheduler_loop, daemon=True)
        worker_thread.start()
        scheduler_thread.start()
        _periodic_auth_sync_started = True


def redirect_ingress_path(path: str):
    return redirect(ingress_url(path))


def redirect_to_login() -> Any:
    next_path = request.path or "/pairing"
    encoded_next = urllib.parse.quote(next_path, safe="/")
    return redirect_ingress_path(f"/login?next={encoded_next}")


def require_auth_or_redirect() -> Any:
    if is_authenticated():
        return None
    return redirect_to_login()


def require_completed_pairing_or_redirect() -> Any:
    if has_saved_pairing_credentials():
        return None
    return redirect_ingress_path("/pairing")


def load_pairing_context() -> dict[str, Any]:
    pairing_state = get_pairing_state()
    pending_verification_code = pairing_state.get("verification_code", "")
    poll_after_seconds = to_positive_int(pairing_state.get("poll_after_seconds", 2), 2)

    saved_credentials = read_saved_credentials()
    health_snapshot = collect_health_snapshot() if has_saved_pairing_credentials() else {}
    external_url = ""
    internal_url = ""
    desired_hostname = saved_credentials.get("hostname", "").strip()
    tunnel_hostname = saved_credentials.get("tunnel_hostname", "").strip()
    current_hostname = ""
    try:
        current_hostname = get_current_host_hostname()
    except SupervisorAPIError:
        current_hostname = ""
    if not current_hostname:
        current_hostname = desired_hostname
    raw_external_url = saved_credentials.get("external_url", "")
    if raw_external_url:
        try:
            external_url = normalize_external_url(raw_external_url)
        except AuthStorageError:
            external_url = ""
    raw_internal_url = saved_credentials.get("internal_url", "")
    if raw_internal_url:
        try:
            internal_url = normalize_internal_url(raw_internal_url)
        except AuthStorageError:
            internal_url = ""

    return {
        "status_text": token_status_text(),
        "studio_base_url": get_studio_base_url(),
        "pending_verification_code": pending_verification_code,
        "poll_after_seconds": poll_after_seconds,
        "current_hostname": current_hostname,
        "desired_hostname": desired_hostname,
        "current_internal_url": internal_url,
        "current_external_url": external_url,
        "tunnel_hostname": tunnel_hostname,
        "tunnel_status": "Connected" if health_snapshot.get("cloudflared_running") else "Disconnected",
        "tunnel_status_tone": "success" if health_snapshot.get("cloudflared_running") else "error",
        "system_status": str(health_snapshot.get("status", "unknown")).strip().capitalize() if health_snapshot else "Unknown",
        "system_status_tone": "success" if health_snapshot.get("status") == "ok" else "warning",
        "last_config_sync_display": display_timestamp(str(health_snapshot.get("last_syncs", {}).get("config", "")).strip()) if health_snapshot else "Never",
        "last_auth_sync_display": display_timestamp(str(health_snapshot.get("last_syncs", {}).get("auth", "")).strip()) if health_snapshot else "Never",
        "last_sync_target": str(health_snapshot.get("last_apply", {}).get("target", "")).strip() if health_snapshot else "",
    }


def load_auth_management_context() -> dict[str, Any]:
    auth_rows: list[dict[str, Any]] = []
    auth_error = ""
    managed_status = "No managed internal service user configured."
    try:
        auth_rows = list_homeassistant_hash_users()
        managed_status = managed_service_user_status(auth_rows)
    except AuthStorageError as exc:
        auth_error = exc.message

    return {
        "auth_user_count": len(auth_rows),
        "auth_storage_error": auth_error,
        "managed_service_status": managed_status,
        "auth_storage_path": str(HA_CONFIG_DIR / ".storage"),
        "managed_watchdog_enabled": SERVICE_USER_WATCHDOG_ENABLED,
        "managed_watchdog_interval_seconds": SERVICE_USER_WATCHDOG_INTERVAL_SECONDS,
        "periodic_auth_sync_enabled": PERIODIC_AUTH_SYNC_ENABLED,
        "periodic_auth_sync_interval_seconds": PERIODIC_AUTH_SYNC_INTERVAL_SECONDS,
    }


def persist_addon_options(options: dict[str, Any]) -> str:
    supervisor_error = ""
    try:
        supervisor_request("POST", "/addons/self/options", {"options": options})
    except SupervisorAPIError as exc:
        supervisor_error = exc.message

    write_json_file(OPTIONS_FILE, options)
    return supervisor_error


def ensure_iframe_embedding_on_initial_pairing() -> str:
    current_options = read_addon_options()
    updated_options = {
        "ui_auth_enabled": bool(current_options["ui_auth_enabled"]),
        "ui_password": str(current_options["ui_password"]),
        "studio_base_url": str(current_options["studio_base_url"]),
        "auto_enable_iframe_embedding": True,
    }

    option_warning = ""
    if not bool(current_options["auto_enable_iframe_embedding"]):
        supervisor_error = persist_addon_options(updated_options)
        if supervisor_error:
            option_warning = f"Enabled iframe option locally, but Supervisor option update failed: {supervisor_error}"

    try:
        completed = subprocess.run(
            ["python3", str(IFRAME_CONFIGURATOR_SCRIPT)],
            check=False,
            capture_output=True,
            text=True,
            timeout=180,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return str(exc)

    output_lines = [
        line.strip()
        for line in (completed.stdout.splitlines() + completed.stderr.splitlines())
        if line.strip()
    ]
    last_output_line = output_lines[-1] if output_lines else ""

    if completed.returncode != 0:
        if option_warning and last_output_line:
            return f"{option_warning} {last_output_line}"
        if option_warning:
            return option_warning
        return last_output_line or "Iframe configurator failed during initial pairing."

    return option_warning



# ---------------------------------------------------------------------------
# Route Decorators
# ---------------------------------------------------------------------------

def auth_required(func: Callable) -> Callable:
    @wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        guard = require_auth_or_redirect()
        if guard is not None:
            return guard
        return func(*args, **kwargs)
    return wrapper


def pairing_required(func: Callable) -> Callable:
    @wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        guard = require_completed_pairing_or_redirect()
        if guard is not None:
            return guard
        return func(*args, **kwargs)
    return wrapper


def flash_auth_sync_result(sync_result: dict[str, Any]) -> None:
    flash_auth_sync_result(sync_result)


# ---------------------------------------------------------------------------
# Route Handlers
# ---------------------------------------------------------------------------

@app.before_request
def ensure_background_tasks_started() -> None:
    start_managed_service_user_watchdog()
    start_periodic_auth_sync()


@app.post(STUDIO_CONFIG_APPLY_PATH)
def studio_config_apply():
    payload_bytes = request.get_data(cache=False)
    saved_credentials = read_saved_credentials()
    shared_secret = saved_credentials.get("cloudflare_tunnel_token", "").strip()
    if not shared_secret:
        return jsonify({"error": "unauthorized", "detail": "No shared tunnel secret configured yet."}), 401

    timestamp = str(request.headers.get("X-PowerHaus-Timestamp", "")).strip()
    signature = str(request.headers.get("X-PowerHaus-Signature", "")).strip()
    if not verify_studio_push_signature(
        secret=shared_secret,
        timestamp=timestamp,
        signature=signature,
        payload_bytes=payload_bytes,
    ):
        return jsonify({"error": "unauthorized", "detail": "Studio push signature is invalid."}), 401

    try:
        payload = json.loads(payload_bytes.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        return jsonify({"error": "invalid_json", "detail": "Request body must be valid JSON."}), 400

    if not isinstance(payload, dict):
        return jsonify({"error": "invalid_payload", "detail": "Request body must be a JSON object."}), 400

    command_id = str(payload.get("command_id", "")).strip()
    command_type = str(payload.get("command_type", "")).strip().lower()
    effective_payload = payload
    if command_type:
        if command_type != "apply_config":
            return jsonify({"error": "unsupported_command", "detail": f"Unsupported command_type: {command_type}"}), 400
        nested_payload = payload.get("payload")
        if not isinstance(nested_payload, dict):
            return jsonify({"error": "invalid_payload", "detail": "apply_config payload must be a JSON object."}), 400
        effective_payload = dict(nested_payload)
        if "config_version" in payload and "config_version" not in effective_payload:
            effective_payload["config_version"] = payload.get("config_version")
    if command_id and has_processed_command_id(command_id):
        return jsonify({"status": "duplicate", "command_id": command_id}), 200

    try:
        result = apply_studio_configuration_locally(effective_payload)
    except (StudioSyncError, AuthStorageError, SupervisorAPIError) as exc:
        update_sync_state(
            last_apply_at=utcnow_iso(),
            last_apply_status="error",
            last_apply_target="studio_push_apply",
            last_apply_error=str(exc),
        )
        return jsonify({"error": "config_apply_failed", "detail": str(exc)}), 409

    if command_id:
        remember_processed_command_id(command_id)
        result["command_id"] = command_id
    result["command_type"] = command_type or "apply_config"
    update_sync_state(
        desired_config_version=max(to_positive_int(result.get("config_version", 0), 0), current_config_version()),
        applied_config_version=max(to_positive_int(result.get("config_version", 0), 0), 0),
        last_config_sync_at=utcnow_iso(),
        last_config_sync_status="pushed",
        last_config_sync_error="",
    )
    enqueue_sync_job("health_probe", reason="studio-push")
    return jsonify(result), 200


@app.get("/_powerhausbox/api/healthz")
def healthz():
    snapshot = get_latest_health_snapshot()
    if not snapshot:
        snapshot = collect_health_snapshot()
        set_latest_health_snapshot(snapshot)
    status_code = 200
    if bool(snapshot.get("paired")) and not bool(snapshot.get("cloudflared_running")):
        status_code = 503
    return jsonify(snapshot), status_code


@app.get("/")
def index():
    return redirect_ingress_path("/pairing")


@app.get("/login")
def login_page():
    if not is_ui_auth_enabled():
        return redirect_ingress_path("/pairing")
    if bool(session.get("authenticated")):
        return redirect_ingress_path("/pairing")

    next_path = normalize_redirect_path(request.args.get("next", "/pairing"), "/pairing")
    return render_template("login.html", next_path=next_path)


@app.post("/login")
def login():
    if not is_ui_auth_enabled():
        return redirect_ingress_path("/pairing")

    password = request.form.get("password", "")
    ui_password = get_ui_password()
    if not ui_password:
        flash("UI authentication is enabled, but no password is configured.", "error")
        return redirect_ingress_path("/settings")

    if hmac.compare_digest(password, ui_password):
        session["authenticated"] = True
        flash("Login successful.", "success")
        next_path = normalize_redirect_path(request.form.get("next", "/pairing"), "/pairing")
        return redirect_ingress_path(next_path)
    flash("Invalid password.", "error")
    return redirect_ingress_path("/login")


@app.post("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "info")
    return redirect_ingress_path("/pairing")


NEEDS_RESTART_FLAG = Path("/data/.needs_ha_restart")


@app.get("/pairing")
@auth_required
def pairing_page():
    if NEEDS_RESTART_FLAG.exists():
        return render_template("needs_restart.html")
    if not has_saved_pairing_credentials():
        return render_template("pairing_onboarding.html", **load_pairing_context())
    return render_template("pairing.html", active_page="pairing", **load_pairing_context())


@app.post("/trigger-restart")
@auth_required
def trigger_ha_restart():
    """Trigger HA Core restart and remove the restart flag."""
    try:
        NEEDS_RESTART_FLAG.unlink(missing_ok=True)
        supervisor_request("POST", "/core/restart")
        flash("Home Assistant wird neu gestartet. Bitte warten...", "success")
    except SupervisorAPIError as exc:
        flash(f"Neustart fehlgeschlagen: {exc}", "error")
    return redirect_ingress_path("/pairing")


@app.get("/auth-management")
@auth_required
@pairing_required
def auth_management_page():
    return render_template("auth_management.html", active_page="auth_management", **load_auth_management_context())


@app.get("/settings")
@auth_required
@pairing_required
def settings_page():
    options = read_addon_options()
    health_snapshot = collect_health_snapshot()
    return render_template(
        "settings.html",
        active_page="settings",
        ui_auth_enabled=bool(options["ui_auth_enabled"]),
        studio_base_url=str(options["studio_base_url"]),
        auto_enable_iframe_embedding=bool(options["auto_enable_iframe_embedding"]),
        has_ui_password=bool(str(options["ui_password"]).strip()),
        sync_status=str(health_snapshot.get("status", "unknown")).strip().capitalize(),
        sync_status_tone="success" if health_snapshot.get("status") == "ok" else "warning",
        tunnel_status="Connected" if health_snapshot.get("cloudflared_running") else "Disconnected",
        tunnel_status_tone="success" if health_snapshot.get("cloudflared_running") else "error",
        last_config_sync_display=display_timestamp(str(health_snapshot.get("last_syncs", {}).get("config", "")).strip()),
        last_auth_sync_display=display_timestamp(str(health_snapshot.get("last_syncs", {}).get("auth", "")).strip()),
        last_sync_target=str(health_snapshot.get("last_apply", {}).get("target", "")).strip(),
    )


@app.post("/settings/security")
@auth_required
@pairing_required
def settings_security():
    current_options = read_addon_options()
    requested_ui_auth_enabled = request.form.get("ui_auth_enabled") == "on"
    requested_studio_base_url = request.form.get("studio_base_url", "").strip()
    requested_auto_iframe = request.form.get("auto_enable_iframe_embedding") == "on"
    requested_password = request.form.get("ui_password", "").strip()
    requested_password_confirm = request.form.get("ui_password_confirm", "").strip()

    if requested_password and requested_password != requested_password_confirm:
        flash("Password confirmation does not match.", "error")
        return redirect_ingress_path("/settings")

    effective_ui_password = requested_password or str(current_options["ui_password"]).strip()
    if requested_ui_auth_enabled and not effective_ui_password:
        flash("A UI password is required when UI authentication is enabled.", "error")
        return redirect_ingress_path("/settings")

    if not requested_studio_base_url:
        requested_studio_base_url = str(current_options["studio_base_url"]).strip()
    if not is_valid_https_url(requested_studio_base_url):
        flash("studio_base_url must use HTTPS.", "error")
        return redirect_ingress_path("/settings")

    updated_options = {
        "ui_auth_enabled": requested_ui_auth_enabled,
        "ui_password": effective_ui_password,
        "studio_base_url": requested_studio_base_url.rstrip("/"),
        "auto_enable_iframe_embedding": requested_auto_iframe,
    }
    supervisor_error = persist_addon_options(updated_options)

    if requested_ui_auth_enabled:
        session["authenticated"] = True
    else:
        session.pop("authenticated", None)

    if supervisor_error:
        flash(f"Settings saved locally, but Supervisor update failed: {supervisor_error}", "warning")
    else:
        flash("Settings saved.", "success")
    return redirect_ingress_path("/settings")


@app.post("/pair/start")
@auth_required
def pair_start():
    had_saved_credentials = has_saved_pairing_credentials()
    pair_code = extract_pair_code_from_form(request.form)
    if not valid_pair_code(pair_code):
        flash("Pair code must be exactly 6 digits.", "error")
        return redirect_ingress_path("/pairing")

    base_url = get_studio_base_url()
    if not is_valid_https_url(base_url):
        flash("studio_base_url must use HTTPS.", "error")
        return redirect_ingress_path("/pairing")

    outbound_request_id = f"phb-{uuid.uuid4().hex}"
    try:
        status_code, response = post_json(
            f"{base_url}{PAIR_INIT_PATH}",
            {"pair_code": pair_code},
            headers={"X-Request-ID": outbound_request_id},
        )
    except PairingAPIError as exc:
        api_error = extract_api_error_code(exc.payload)
        api_detail = extract_api_error_detail(exc.payload)
        request_id = extract_api_request_id(exc.payload, exc.response_headers) or outbound_request_id
        cf_ray = extract_api_cf_ray(exc.response_headers)
        server_header = str(exc.response_headers.get("server") or "").strip()

        flash(
            build_pair_start_error_message(
                status_code=exc.status_code,
                api_error=api_error,
                api_detail=api_detail,
                request_id=request_id,
                cf_ray=cf_ray,
                server_header=server_header,
            ),
            "error",
        )
        body_preview = str(exc.response_body or "").replace("\n", " ").replace("\r", " ")[:240]
        log(
            "pair/init failed "
            f"status={exc.status_code} error={api_error!r} detail={api_detail!r} "
            f"request_id={request_id!r} cf_ray={cf_ray!r} server={server_header!r} "
            f"payload={exc.payload!r} body_preview={body_preview!r}",
        )
        return redirect_ingress_path("/pairing")

    if status_code != 200 or response.get("status") != "pending_approval":
        flash("Unexpected response from Studio during pairing init.", "error")
        return redirect_ingress_path("/pairing")

    session_token = str(response.get("session_token", "")).strip()
    verification_code = str(response.get("verification_code", "")).strip()
    expires_in_seconds = to_positive_int(response.get("expires_in_seconds", 300), 300)
    poll_after_seconds = to_positive_int(response.get("poll_after_seconds", 2), 2)

    if not session_token:
        flash("Studio did not return a pairing session.", "error")
        return redirect_ingress_path("/pairing")
    if not re.fullmatch(r"\d{2}", verification_code):
        flash("Studio returned an invalid verification code.", "error")
        return redirect_ingress_path("/pairing")

    set_pairing_state(
        session_token=session_token,
        verification_code=verification_code,
        poll_after_seconds=poll_after_seconds,
        expires_in_seconds=expires_in_seconds,
        base_url=base_url,
    )
    if had_saved_credentials:
        flash("Pairing initialized. Approve the shown 2-digit code in Studio.", "success")
    return redirect_ingress_path("/pairing")


@app.get("/pair/status")
def pair_status():
    if not is_authenticated():
        return jsonify({"state": "unauthorized"}), 401

    state = get_pairing_state()
    if not state:
        return jsonify({"state": "idle"}), 200

    now = int(time.time())
    if now >= to_positive_int(state.get("expires_at", 0), 0):
        clear_pairing_state()
        return jsonify({"state": "expired", "message": "Pairing session expired. Start again with a new 6-digit code."}), 200

    base_url = str(state.get("base_url", "")).strip()
    session_token = str(state.get("session_token", "")).strip()

    try:
        status_code, response = post_json(
            f"{base_url}{PAIR_COMPLETE_PATH}",
            {"session_token": session_token},
        )
    except PairingAPIError as exc:
        api_error = extract_api_error_code(exc.payload)
        if exc.status_code in (400, 404) or api_error == "invalid_session":
            clear_pairing_state()
            return jsonify({"state": "error", "message": "Pairing session is no longer valid. Start pairing again."}), 200
        if exc.status_code == 429 or api_error == "rate_limited":
            return jsonify(
                {
                    "state": "pending",
                    "verification_code": state.get("verification_code", ""),
                    "poll_after_seconds": to_positive_int(state.get("poll_after_seconds", 2), 2),
                    "message": "Rate limited; retrying automatically.",
                }
            ), 200
        return jsonify(
            {
                "state": "pending",
                "verification_code": state.get("verification_code", ""),
                "poll_after_seconds": to_positive_int(state.get("poll_after_seconds", 2), 2),
                "message": "Still waiting for approval.",
            }
        ), 200

    if status_code == 202 and response.get("status") == "pending_approval":
        return jsonify(
            {
                "state": "pending",
                "verification_code": state.get("verification_code", ""),
                "poll_after_seconds": to_positive_int(state.get("poll_after_seconds", 2), 2),
            }
        ), 200

    if status_code == 200 and response.get("status") == "ready":
        was_initial_pairing = not has_saved_pairing_credentials()
        tunnel_hostname = str(response.get("tunnel_hostname", "")).strip()
        cloudflare_tunnel_token = str(response.get("cloudflare_tunnel_token", "")).strip()
        box_api_token = str(response.get("box_api_token", "")).strip()
        internal_url = str(response.get("internal_url", "")).strip()
        external_url = str(response.get("external_url", "")).strip()
        raw_hostname = str(response.get("hostname", "")).strip()
        config_version = max(to_positive_int(response.get("config_version", 0), 0), 0)
        if not tunnel_hostname or not cloudflare_tunnel_token or not box_api_token or not internal_url or not external_url:
            clear_pairing_state()
            return jsonify({"state": "error", "message": "Studio returned incomplete credentials."}), 200

        try:
            normalized_hostname = normalize_hostname(raw_hostname) if raw_hostname else ""
            normalized_internal_url = normalize_internal_url(internal_url)
            normalized_external_url = normalize_external_url(external_url)
        except AuthStorageError:
            clear_pairing_state()
            return jsonify({"state": "error", "message": "Studio returned invalid internal_url, external_url, or hostname."}), 200

        # Signal to run.sh BEFORE writing credentials, so the flag is always
        # present when run.sh detects the new token fingerprint.
        _PAIRING_SYNC_FLAG = Path("/data/.pairing_sync_done")
        _PAIRING_SYNC_FLAG.write_text(utcnow_iso(), encoding="utf-8")

        persist_credentials(
            cloudflare_tunnel_token,
            tunnel_hostname,
            box_api_token,
            normalized_internal_url,
            normalized_external_url,
            hostname=normalized_hostname,
            config_version=config_version,
        )
        clear_pairing_state()

        # --- Wait for Core to be stable before mutating config ---
        # The user may have just clicked "restart HA" from the setup page.
        # We must wait for that restart to finish before sending /core/stop,
        # otherwise conflicting lifecycle commands wedge the Supervisor.
        try:
            log("Waiting for Home Assistant Core to be stable before applying config...")
            wait_for_homeassistant_api_reachability(True, timeout_seconds=300)
            log("Core is reachable. Proceeding with config apply.")
        except SupervisorAPIError:
            log("Core not reachable after 5 minutes. Proceeding anyway (may already be stopped).")

        # --- Single Core stop/start for all config mutations ---
        url_sync_error = ""
        iframe_setup_error = ""
        applied_external_url = ""
        try:
            if normalized_hostname:
                sync_homeassistant_hostname(normalized_hostname)

            def _apply_all_config():
                """Batch URL sync + iframe config in one Core stop/start cycle."""
                mutate_core_config_storage(lambda doc: _apply_urls_to_config(
                    doc, normalized_internal_url, normalized_external_url,
                ))
                if was_initial_pairing:
                    # Run iframe configurator while Core is still stopped.
                    # POWERHAUS_CORE_STOPPED=1 tells it to skip its own restart.
                    try:
                        iframe_env = {**os.environ, "POWERHAUS_CORE_STOPPED": "1"}
                        completed = subprocess.run(
                            ["python3", str(IFRAME_CONFIGURATOR_SCRIPT)],
                            check=False, capture_output=True, text=True, timeout=120,
                            env=iframe_env,
                        )
                        if completed.returncode != 0:
                            return completed.stderr.strip() or "iframe configurator failed"
                    except (OSError, subprocess.SubprocessError) as exc:
                        return str(exc)
                return ""

            iframe_setup_error = run_with_core_stopped(_apply_all_config) or ""
            applied_external_url = normalized_external_url

            verify_applied_homeassistant_state(
                expected_hostname=normalized_hostname,
                expected_internal_url=normalized_internal_url,
                expected_external_url=normalized_external_url,
                target="pairing_apply",
            )
        except (SupervisorAPIError, AuthStorageError) as exc:
            url_sync_error = str(exc)

        update_sync_state(
            desired_config_version=config_version,
            applied_config_version=config_version if not url_sync_error else max(current_config_version(), 0),
            last_apply_status="applied" if not url_sync_error else "error",
            last_apply_target="pairing_apply",
            last_apply_error=url_sync_error,
            last_config_reconcile_at=utcnow_iso(),
            last_config_reconcile_status="applied" if not url_sync_error else "error",
            last_config_reconcile_error=url_sync_error,
        )

        # Report pairing result to Studio (success or failure)
        pairing_event = "pairing_completed" if not url_sync_error else "pairing_error"
        try:
            send_state_report("event", {
                "event_type": pairing_event,
                "config_version": config_version,
                "urls_synced": not bool(url_sync_error),
                "urls_sync_error": url_sync_error,
                "iframe_setup_error": iframe_setup_error,
                "hostname": normalized_hostname,
                "internal_url": normalized_internal_url,
                "external_url": normalized_external_url,
            })
        except (StudioSyncError, Exception) as exc:
            log(f"Failed to report pairing result to Studio: {exc}")

        # Auth sync (no Core restart needed, skip if Core is down)
        auth_sync_error = ""
        auth_sync_result: dict[str, Any] = {}
        if not url_sync_error:
            try:
                auth_sync_result = run_auth_sync_once(trigger="pairing")
            except (StudioSyncError, AuthStorageError) as exc:
                auth_sync_error = str(exc)

        pairing_state = "ready" if not url_sync_error else "error"
        pairing_message = url_sync_error if url_sync_error else ""

        return jsonify(
            {
                "state": pairing_state,
                "message": pairing_message,
                "tunnel_hostname": tunnel_hostname,
                "external_url": applied_external_url,
                "internal_url": normalized_internal_url,
                "hostname": normalized_hostname,
                "urls_synced": not bool(url_sync_error),
                "urls_sync_error": url_sync_error,
                "auth_synced": not bool(auth_sync_error),
                "auth_sync_error": auth_sync_error,
                "auth_synced_count": int(auth_sync_result.get("synced_count", 0)),
                "auth_sync_id": str(auth_sync_result.get("sync_id", "")).strip(),
                "iframe_setup_error": iframe_setup_error,
            }
        ), 200

    clear_pairing_state()
    return jsonify({"state": "error", "message": "Unexpected response from Studio."}), 200


@app.get("/auth/users/export")
def auth_users_export():
    if not is_authenticated():
        return jsonify({"error": "unauthorized"}), 401

    try:
        rows = list_homeassistant_hash_users()
    except AuthStorageError as exc:
        return jsonify({"error": exc.message}), 400

    payload = {"count": len(rows), "users": rows}
    body = json.dumps(payload, ensure_ascii=True, indent=2) + "\n"
    return Response(
        body,
        mimetype="application/json",
        headers={
            "Content-Disposition": "attachment; filename=ha_auth_users_export.json",
            "Cache-Control": "no-store",
        },
    )


@app.post("/auth/users/create-service")
@auth_required
def auth_create_service_user():
    username = request.form.get("service_username", "").strip()
    password_hash = request.form.get("service_password_hash", "").strip()
    display_name = request.form.get("service_display_name", "").strip() or "PowerHausBox Internal Service"

    try:
        created = create_user_with_hash(
            username=username,
            password_hash=password_hash,
            display_name=display_name,
            system_generated=True,
            local_only=True,
        )
        write_managed_service_user_config(
            {
                "username": username,
                "password_hash": password_hash,
                "display_name": display_name,
            }
        )
    except AuthStorageError as exc:
        flash(exc.message, "error")
        return redirect_ingress_path("/auth-management")

    flash(
        (
            "Hidden service user created and stored as managed user. "
            f"Username: {created['username']} (id: {created['user_id']})."
        ),
        "success",
    )
    try:
        sync_result = run_auth_sync_once(trigger="create-service-user")
        flash_auth_sync_result(sync_result)
    except (StudioSyncError, AuthStorageError) as exc:
        flash(f"Studio auth sync failed: {exc}", "warning")
    return redirect_ingress_path("/auth-management")


@app.post("/auth/users/ensure-service")
@auth_required
def auth_ensure_service_user():
    try:
        status, created = ensure_managed_service_user()
    except AuthStorageError as exc:
        flash(exc.message, "error")
        return redirect_ingress_path("/auth-management")

    if status == "present":
        flash("Managed service user already exists.", "info")
    elif created is None:
        flash("Managed service user check completed.", "info")
    else:
        flash(
            f"Managed service user was missing and has been recreated. "
            f"Username: {created['username']} (id: {created['user_id']}).",
            "success",
        )
    try:
        sync_result = run_auth_sync_once(trigger="ensure-service-user")
        flash_auth_sync_result(sync_result)
    except (StudioSyncError, AuthStorageError) as exc:
        flash(f"Studio auth sync failed: {exc}", "warning")
    return redirect_ingress_path("/auth-management")


@app.post("/auth/users/create-normal")
@auth_required
def auth_create_normal_user():
    username = request.form.get("normal_username", "").strip()
    password_hash = request.form.get("normal_password_hash", "").strip()
    display_name = request.form.get("normal_display_name", "").strip() or username

    try:
        created = create_user_with_hash(
            username=username,
            password_hash=password_hash,
            display_name=display_name,
            system_generated=False,
            local_only=False,
        )
    except AuthStorageError as exc:
        flash(exc.message, "error")
        return redirect_ingress_path("/auth-management")

    flash(
        f"Normal user created. Username: {created['username']} (id: {created['user_id']}).",
        "success",
    )
    try:
        sync_result = run_auth_sync_once(trigger="create-normal-user")
        flash_auth_sync_result(sync_result)
    except (StudioSyncError, AuthStorageError) as exc:
        flash(f"Studio auth sync failed: {exc}", "warning")
    return redirect_ingress_path("/auth-management")


@app.post("/studio/auth/sync")
@auth_required
def studio_auth_sync_now():
    try:
        sync_result = run_auth_sync_once(trigger="manual-auth-sync")
    except (StudioSyncError, AuthStorageError) as exc:
        flash(f"Studio auth sync failed: {exc}", "error")
        return redirect_ingress_path("/auth-management")

    flash_auth_sync_result(sync_result)
    return redirect_ingress_path("/auth-management")


@app.post("/studio/sync")
@auth_required
def studio_sync_now():
    next_path = normalize_redirect_path(request.form.get("next", "/pairing"), "/pairing")

    config_result: dict[str, Any] = {}
    config_error = ""
    try:
        config_result = run_config_sync_once(trigger="manual-full-sync")
    except (StudioSyncError, SupervisorAPIError, AuthStorageError) as exc:
        config_error = str(exc)

    auth_result: dict[str, Any] = {}
    auth_error = ""
    try:
        auth_result = run_auth_sync_once(trigger="manual-full-sync")
    except (StudioSyncError, AuthStorageError) as exc:
        auth_error = str(exc)

    if config_error and auth_error:
        flash(
            f"Studio sync failed. Config pull: {config_error} Auth push: {auth_error}",
            "error",
        )
        return redirect_ingress_path(next_path)

    if config_error:
        sync_id = str(auth_result.get("sync_id", "")).strip()
        sync_id_suffix = f" sync_id={sync_id}" if sync_id else ""
        flash(
            (
                f"Studio sync partial. Config pull failed: {config_error} "
                f"Auth push succeeded: synced={auth_result.get('synced_count', 0)} "
                f"received={auth_result.get('received_count', 0)}{sync_id_suffix}"
            ),
            "warning",
        )
        return redirect_ingress_path(next_path)

    if auth_error:
        flash(
            (
                "Studio sync partial. Config refreshed: "
                f"hostname={config_result.get('hostname', '')} "
                f"internal_url={config_result.get('internal_url', '')} "
                f"external_url={config_result.get('external_url', '')}. "
                f"Auth push failed: {auth_error}"
            ),
            "warning",
        )
        return redirect_ingress_path(next_path)

    sync_id = str(auth_result.get("sync_id", "")).strip()
    sync_id_suffix = f" sync_id={sync_id}" if sync_id else ""
    flash(
        (
            "Studio sync completed. "
            f"hostname={config_result.get('hostname', '')} "
            f"internal_url={config_result.get('internal_url', '')} "
            f"external_url={config_result.get('external_url', '')} "
            f"synced={auth_result.get('synced_count', 0)} "
            f"received={auth_result.get('received_count', 0)}{sync_id_suffix}"
        ),
        "success",
    )
    return redirect_ingress_path(next_path)


@app.post("/ha/urls/sync")
@auth_required
def sync_ha_urls_from_saved_credentials():
    studio_sync_error = ""
    try:
        run_config_sync_once(trigger="manual-ha-sync")
    except (StudioSyncError, AuthStorageError, SupervisorAPIError) as exc:
        studio_sync_error = str(exc)

    credentials = read_saved_credentials()
    tunnel_hostname = credentials.get("tunnel_hostname", "").strip()
    if not tunnel_hostname:
        flash("No stored tunnel hostname found. Pair first.", "error")
        return redirect_ingress_path("/pairing")
    if not (
        str(credentials.get("hostname", "")).strip()
        or str(credentials.get("internal_url", "")).strip()
        or str(credentials.get("external_url", "")).strip()
    ):
        flash("No stored Home Assistant hostname or URLs found. Re-pair or sync from Studio first.", "error")
        return redirect_ingress_path("/pairing")

    try:
        applied = apply_saved_homeassistant_host_settings(target="manual_ha_sync")
    except (SupervisorAPIError, AuthStorageError) as exc:
        flash(f"Failed to update Home Assistant host settings: {exc}", "error")
        return redirect_ingress_path("/pairing")

    if studio_sync_error:
        flash(f"Studio config refresh failed; applied local credentials instead: {studio_sync_error}", "warning")
    flash(
        (
            "Home Assistant host settings updated. "
            f"hostname={applied.get('hostname', '')} "
            f"internal_url={applied.get('internal_url', '')} "
            f"external_url={applied.get('external_url', '')}"
        ),
        "success",
    )
    enqueue_sync_job("health_probe", reason="manual-ha-sync")
    return redirect_ingress_path("/pairing")


@app.post("/token/delete")
@auth_required
@pairing_required
def delete_token():
    confirmation = str(request.form.get("confirmation", "")).strip().lower()
    if confirmation != "löschen":
        flash('Type "löschen" to confirm link removal.', "error")
        return redirect_ingress_path("/settings")

    disconnect_warning = ""
    credentials = read_saved_credentials()
    desired_state = desired_configuration_from_credentials(credentials)
    try:
        send_state_report(
            "event",
            {
                "event_type": "addon_disconnected",
                "reason": "user_requested_unlink",
                "config_version": current_config_version(credentials),
                "desired_state": desired_state,
            },
        )
    except StudioSyncError as exc:
        disconnect_warning = str(exc)

    clear_pairing_state()
    clear_credentials()
    reset_sync_state()
    if disconnect_warning:
        flash(f"Link removed locally, but Studio disconnect event failed: {disconnect_warning}", "warning")
    else:
        flash("Link removed. The app is reset and ready for a fresh pairing.", "warning")
    return redirect_ingress_path("/pairing")


# ---------------------------------------------------------------------------
# Studio API helpers for backup proxy
# ---------------------------------------------------------------------------


def _studio_headers() -> dict[str, str]:
    """Build authorization headers for Studio API calls."""
    credentials = read_saved_credentials()
    box_api_token = credentials.get("box_api_token", "").strip()
    return {"Authorization": f"Bearer {box_api_token}"}


def _studio_configured() -> bool:
    """Check if Studio API is configured and paired."""
    credentials = read_saved_credentials()
    return bool(credentials.get("box_api_token", "").strip())


# ---------------------------------------------------------------------------
# Terminal proxy
# Terminal HTTP and WebSocket traffic is routed by nginx directly to the
# aiohttp terminal proxy on port 7682 (terminal_proxy.py). Flask does not
# handle terminal traffic — nginx routes /_powerhausbox/api/terminal/*
# to port 7682 before it reaches Flask.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Backup proxy routes (HA integration → add-on → Studio)
# ---------------------------------------------------------------------------


@app.route("/api/backup/upload", methods=["POST"])
def backup_upload_proxy():
    """Receive backup from HA integration and stream-forward to Studio."""
    if not _studio_configured():
        return jsonify({"error": "Studio not configured. Pair app first."}), 503

    base_url = get_studio_base_url()
    headers = _studio_headers()

    try:
        import urllib.request as _urllib_request

        studio_url = f"{base_url}{BACKUP_UPLOAD_PATH}"
        content_type = request.content_type or "application/octet-stream"
        content_length = request.content_length

        req = _urllib_request.Request(
            studio_url,
            data=request.stream,
            method="POST",
            headers={
                **headers,
                "Content-Type": content_type,
                **({"Content-Length": str(content_length)} if content_length else {}),
            },
        )
        with _urllib_request.urlopen(req, timeout=7200) as resp:
            body = resp.read().decode("utf-8")
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                data = {"raw": body}
            return jsonify(data), resp.status
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        log(f"Backup upload to Studio failed: HTTP {exc.code}")
        return jsonify({"error": f"Studio returned {exc.code}", "detail": body}), exc.code
    except Exception as exc:
        log(f"Backup upload to Studio failed: {exc}")
        return jsonify({"error": "Upload to Studio failed"}), 502


@app.route("/api/backup/list", methods=["GET", "POST"])
def backup_list_proxy():
    """List backups stored on Studio."""
    if not _studio_configured():
        return jsonify({"backups": []})

    base_url = get_studio_base_url()
    headers = _studio_headers()

    try:
        status, data = post_json(f"{base_url}{BACKUP_LIST_PATH}", {}, headers=headers)
        if status == 200:
            return jsonify(data)
        return jsonify({"backups": []})
    except Exception as exc:
        log(f"Backup list from Studio failed: {exc}")
        return jsonify({"backups": []})


@app.route("/api/backup/download/<backup_id>", methods=["GET"])
def backup_download_proxy(backup_id):
    """Stream a backup file from Studio."""
    if not _studio_configured():
        return jsonify({"error": "Studio not configured"}), 503

    base_url = get_studio_base_url()
    headers = _studio_headers()

    try:
        import urllib.request as _urllib_request

        studio_url = f"{base_url}{BACKUP_DOWNLOAD_PATH}{backup_id}/"
        req = _urllib_request.Request(studio_url, headers=headers, method="GET")
        resp = _urllib_request.urlopen(req, timeout=7200)

        def generate():
            try:
                while True:
                    chunk = resp.read(BACKUP_CHUNK_SIZE)
                    if not chunk:
                        break
                    yield chunk
            finally:
                resp.close()

        return Response(
            generate(),
            status=200,
            content_type="application/octet-stream",
            headers={
                "Content-Disposition": f'attachment; filename="{backup_id}.tar"',
            },
        )
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return jsonify({"error": "Backup not found"}), 404
        body = exc.read().decode("utf-8")
        return jsonify({"error": f"Studio returned {exc.code}", "detail": body}), exc.code
    except Exception as exc:
        log(f"Backup download from Studio failed: {exc}")
        return jsonify({"error": "Download from Studio failed"}), 502


@app.route("/api/backup/<backup_id>", methods=["GET", "DELETE"])
def backup_detail_proxy(backup_id):
    """Get or delete a specific backup on Studio."""
    if not _studio_configured():
        return jsonify({"error": "Studio not configured"}), 503

    base_url = get_studio_base_url()
    headers = _studio_headers()

    try:
        import urllib.request as _urllib_request

        studio_url = f"{base_url}{BACKUP_DETAIL_PATH}{backup_id}/"
        req = _urllib_request.Request(studio_url, headers={**headers, "Accept": "application/json"}, method=request.method)
        with _urllib_request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                data = {"raw": body}
            return jsonify(data), resp.status
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return jsonify({"error": "Backup not found"}), 404
        body = exc.read().decode("utf-8")
        return jsonify({"error": f"Studio returned {exc.code}", "detail": body}), exc.code
    except Exception as exc:
        log(f"Backup operation failed: {exc}")
        return jsonify({"error": str(exc)}), 502


# ---------------------------------------------------------------------------
# SSH authorized keys management
# ---------------------------------------------------------------------------


def write_authorized_keys(studio_keys: list[str]) -> None:
    """Write SSH authorized keys merging local options and Studio-synced keys."""
    options = read_addon_options()
    username = options.get("ssh", {}).get("username", "hassio")
    local_keys = options.get("ssh", {}).get("authorized_keys", [])

    ssh_dir = Path(f"/home/{username}/.ssh")
    auth_keys_path = ssh_dir / "authorized_keys"

    try:
        ssh_dir.mkdir(parents=True, exist_ok=True)
        with open(auth_keys_path, "w") as f:
            for key in local_keys + studio_keys:
                key = key.strip()
                if key:
                    f.write(f"{key}\n")
        os.chmod(auth_keys_path, 0o600)
        os.chmod(ssh_dir, 0o700)
        shutil.chown(str(ssh_dir), user=username, group=username)
        shutil.chown(str(auth_keys_path), user=username, group=username)
    except Exception as exc:
        log(f"Failed to write authorized_keys: {exc}")


if __name__ == "__main__":
    if "--sync-config-from-studio" in sys.argv:
        try:
            result = run_config_sync_once(trigger="startup_preflight")
        except (StudioSyncError, AuthStorageError, SupervisorAPIError) as exc:
            log(f"Startup Studio config sync failed: {exc}")
            sys.exit(1)
        log(
            "Startup Studio config sync succeeded: "
            f"hostname={result.get('hostname', '')} "
            f"internal_url={result.get('internal_url', '')} "
            f"external_url={result.get('external_url', '')} "
            f"status={result.get('status', '')}"
        )
        sys.exit(0)

    if "--apply-saved-config" in sys.argv:
        try:
            result = apply_saved_homeassistant_host_settings(target="startup_saved_config")
        except (SupervisorAPIError, AuthStorageError) as exc:
            log(f"Startup saved-config apply failed: {exc}")
            sys.exit(1)
        log(
            "Startup saved-config apply succeeded: "
            f"hostname={result.get('hostname', '')} "
            f"internal_url={result.get('internal_url', '')} "
            f"external_url={result.get('external_url', '')}"
        )
        sys.exit(0)

    port = int(os.getenv("WEB_PORT", "8099"))
    start_managed_service_user_watchdog()
    start_periodic_auth_sync()
    app.run(host="0.0.0.0", port=port, debug=False)
