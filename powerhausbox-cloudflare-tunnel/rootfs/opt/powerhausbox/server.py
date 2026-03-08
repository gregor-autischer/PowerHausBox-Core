import base64
import binascii
from datetime import datetime, timezone
import hmac
import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable

from flask import Flask, Response, flash, jsonify, redirect, render_template, request, session


CONTAINER_ENV_DIR = Path("/run/s6/container_environment")


def _read_watchdog_interval_seconds() -> int:
    raw_value = os.getenv("SERVICE_USER_WATCHDOG_INTERVAL_SECONDS", "300").strip()
    try:
        parsed = int(raw_value)
    except ValueError:
        parsed = 300
    return parsed if parsed >= 60 else 60


def _read_periodic_auth_sync_interval_seconds() -> int:
    raw_value = os.getenv("PERIODIC_AUTH_SYNC_INTERVAL_SECONDS", "21600").strip()
    try:
        parsed = int(raw_value)
    except ValueError:
        parsed = 21600
    return parsed if parsed >= 300 else 300

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "powerhausbox-dev-secret")
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

TOKEN_FILE = Path(os.getenv("TOKEN_FILE", "/data/tunnel_token"))
SECRETS_FILE = Path(os.getenv("SECRETS_FILE", "/data/pairing_secrets.json"))
OPTIONS_FILE = Path(os.getenv("OPTIONS_FILE", "/data/options.json"))
MANAGED_SERVICE_USER_FILE = Path(os.getenv("MANAGED_SERVICE_USER_FILE", "/data/managed_service_user.json"))
IFRAME_CONFIGURATOR_SCRIPT = Path(os.getenv("IFRAME_CONFIGURATOR_SCRIPT", "/opt/powerhausbox/iframe_configurator.py"))

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
STUDIO_CONFIG_APPLY_PATH = "/_powerhausbox/api/studio/config/apply/"
STUDIO_CONFIG_PUSH_MAX_SKEW_SECONDS = 300

GROUP_ID_USER = "system-users"
VALID_USERNAME_RE = re.compile(r"[a-z0-9._@-]{3,64}")
VALID_HOSTNAME_RE = re.compile(
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*"
)
VALID_BCRYPT_PREFIXES = (b"$2a$", b"$2b$", b"$2y$")
SERVICE_USER_WATCHDOG_ENABLED = os.getenv("SERVICE_USER_WATCHDOG_ENABLED", "true").strip().lower() != "false"
SERVICE_USER_WATCHDOG_INTERVAL_SECONDS = _read_watchdog_interval_seconds()
PERIODIC_AUTH_SYNC_ENABLED = os.getenv("PERIODIC_AUTH_SYNC_ENABLED", "true").strip().lower() != "false"
PERIODIC_AUTH_SYNC_INTERVAL_SECONDS = _read_periodic_auth_sync_interval_seconds()
ADDON_VERSION = os.getenv("ADDON_VERSION", "unknown")

_pairing_state_lock = threading.Lock()
_pairing_state: dict[str, Any] = {}
_watchdog_started = False
_watchdog_lock = threading.Lock()
_periodic_auth_sync_started = False
_periodic_auth_sync_lock = threading.Lock()


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


SUPERVISOR_URL = read_container_env_value("SUPERVISOR_URL") or "http://supervisor"
SUPERVISOR_URL = SUPERVISOR_URL.rstrip("/")
SUPERVISOR_TOKEN = read_container_env_value("SUPERVISOR_TOKEN", "HASSIO_TOKEN")


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
    return {"ingress_url": ingress_url, "ui_auth_enabled": is_ui_auth_enabled()}


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
        os.chmod(path, 0o600)
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


def write_json_file(path: Path, payload: dict[str, Any]) -> None:
    write_secret_file(path, json.dumps(payload, ensure_ascii=True, separators=(",", ":")) + "\n")


def to_positive_int(raw_value: Any, default: int) -> int:
    try:
        parsed = int(raw_value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


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


def has_saved_pairing_credentials() -> bool:
    creds = read_saved_credentials()
    return all(
        [
            creds.get("cloudflare_tunnel_token"),
            creds.get("tunnel_hostname"),
            creds.get("box_api_token"),
            creds.get("internal_url"),
            creds.get("external_url"),
        ]
    )


def normalize_external_url(external_url: str) -> str:
    raw = external_url.strip()
    if not raw:
        raise AuthStorageError("External URL is empty.")

    candidate = raw if "://" in raw else f"https://{raw}"
    parsed = urllib.parse.urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise AuthStorageError("External URL is invalid.")
    if parsed.path not in {"", "/"} or parsed.params or parsed.query or parsed.fragment:
        raise AuthStorageError("External URL must not include path, query, or fragment.")
    return f"{parsed.scheme}://{parsed.netloc}"


def normalize_internal_url(internal_url: str) -> str:
    raw = internal_url.strip()
    if not raw:
        raise AuthStorageError("Internal URL is empty.")

    candidate = raw if "://" in raw else f"http://{raw}"
    parsed = urllib.parse.urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise AuthStorageError("Internal URL is invalid.")
    if parsed.path not in {"", "/"} or parsed.params or parsed.query or parsed.fragment:
        raise AuthStorageError("Internal URL must not include path, query, or fragment.")
    return f"{parsed.scheme}://{parsed.netloc}"


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


def token_status_text() -> str:
    creds = read_saved_credentials()
    if has_saved_pairing_credentials():
        return f"Paired and ready. Tunnel hostname: {creds['tunnel_hostname']}"
    return "No pairing credentials configured yet."


def parse_bool_option(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
    return default


def read_addon_options() -> dict[str, Any]:
    options = read_json_file(OPTIONS_FILE)
    raw_studio_base_url = str(options.get("studio_base_url") or options.get("STUDIO_BASE_URL") or "").strip()
    raw_ui_password = str(options.get("ui_password") or options.get("UI_PASSWORD") or "").strip()
    return {
        "ui_auth_enabled": parse_bool_option(
            options.get("ui_auth_enabled", options.get("UI_AUTH_ENABLED")),
            DEFAULT_UI_AUTH_ENABLED,
        ),
        "ui_password": raw_ui_password or DEFAULT_UI_PASSWORD,
        "studio_base_url": (raw_studio_base_url or DEFAULT_STUDIO_BASE_URL).rstrip("/"),
        "auto_enable_iframe_embedding": parse_bool_option(
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


def build_config_sync_payload(
    *,
    credentials: dict[str, str],
    reported_config_version: int | None = None,
    reported_apply_status: str = "",
    reported_apply_error: str = "",
) -> dict[str, Any]:
    current_tunnel_hostname = credentials.get("tunnel_hostname", "").strip()
    current_internal_url = credentials.get("internal_url", "").strip()
    current_external_url = credentials.get("external_url", "").strip()
    current_hostname = credentials.get("hostname", "").strip()
    if not current_hostname:
        try:
            current_hostname = get_current_host_hostname()
        except SupervisorAPIError:
            current_hostname = ""
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

    payload = {
        "requested_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "source": "home_assistant_addon",
        "addon_version": ADDON_VERSION,
        "current_tunnel_hostname": current_tunnel_hostname,
        "current_internal_url": current_internal_url,
        "current_external_url": current_external_url,
        "current_hostname": current_hostname,
        "reported_config_version": (
            current_config_version(credentials)
            if reported_config_version is None
            else max(to_positive_int(reported_config_version, 0), 0)
        ),
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


def sync_auth_hashes_to_studio() -> dict[str, Any]:
    base_url = get_studio_base_url()
    if not is_valid_https_url(base_url):
        raise StudioSyncError("studio_base_url must use HTTPS.")

    credentials = read_saved_credentials()
    box_api_token = credentials.get("box_api_token", "").strip()
    if not box_api_token:
        raise StudioSyncError("No box_api_token available. Pair add-on with Studio first.")

    users = list_homeassistant_hash_users()
    payload = {
        "synced_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
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
            raise StudioSyncError("Studio rejected box_api_token (401). Re-pair add-on.") from exc
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
        raise StudioSyncError("No box_api_token available. Pair add-on with Studio first.")
    headers = {"Authorization": f"Bearer {box_api_token}"}

    def request_config_sync(sync_payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        try:
            return post_json(f"{base_url}{CONFIG_SYNC_PATH}", sync_payload, headers=headers)
        except PairingAPIError as exc:
            if exc.status_code == 401:
                raise StudioSyncError("Studio rejected box_api_token (401). Re-pair add-on.") from exc
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

    changed = any(
        [
            merged_cloudflare_tunnel_token != current_credentials.get("cloudflare_tunnel_token", "").strip(),
            merged_tunnel_hostname != current_credentials.get("tunnel_hostname", "").strip(),
            merged_box_api_token != box_api_token,
            validated_internal_url != current_credentials.get("internal_url", "").strip(),
            validated_external_url != current_credentials.get("external_url", "").strip(),
            validated_hostname != current_credentials.get("hostname", "").strip(),
            merged_config_version != current_config_version(current_credentials),
        ]
    )

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


def supervisor_request(method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    if not SUPERVISOR_TOKEN:
        raise SupervisorAPIError("SUPERVISOR_TOKEN not available; enable hassio_api for this add-on.")

    url = f"{SUPERVISOR_URL}{path}"
    body = None
    headers = {
        "Authorization": f"Bearer {SUPERVISOR_TOKEN}",
        "Accept": "application/json",
    }
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url=url, method=method.upper(), data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            raw_body = response.read().decode("utf-8").strip()
            if not raw_body:
                return {}
            return json.loads(raw_body)
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8").strip()
        message = f"Supervisor API request failed with HTTP {exc.code}."
        if body_text:
            message = f"{message} Response: {body_text}"
        raise SupervisorAPIError(message) from exc
    except (TimeoutError, urllib.error.URLError) as exc:
        raise SupervisorAPIError("Supervisor API is unreachable.") from exc


def sync_homeassistant_urls(internal_url: str, external_url: str) -> str:
    normalized_internal_url = normalize_internal_url(internal_url)
    normalized_external_url = normalize_external_url(external_url)

    def mutator(config_doc: dict[str, Any]) -> dict[str, Any]:
        config_data = config_doc.get("data")
        if not isinstance(config_data, dict):
            raise SupervisorAPIError("Home Assistant core config storage is invalid.")
        config_data["internal_url"] = normalized_internal_url
        config_data["external_url"] = normalized_external_url
        return {"internal_url": normalized_internal_url, "external_url": normalized_external_url}

    mutate_core_config_storage(mutator)
    return normalized_external_url


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
    supervisor_request("POST", "/host/options", {"hostname": normalized_hostname})
    return normalized_hostname


def apply_studio_configuration_locally(payload: dict[str, Any]) -> dict[str, Any]:
    current_credentials = read_saved_credentials()
    box_api_token = current_credentials.get("box_api_token", "").strip()
    if not box_api_token:
        raise StudioSyncError("No box_api_token available. Pair add-on with Studio first.")

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
    if validated_hostname:
        sync_homeassistant_hostname(validated_hostname)
    applied_external_url = sync_homeassistant_urls(validated_internal_url, validated_external_url)
    persist_credentials(
        tunnel_token,
        tunnel_hostname,
        box_api_token,
        validated_internal_url,
        validated_external_url,
        hostname=validated_hostname,
        config_version=config_version,
    )
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


def wait_for_core_state(target_states: set[str], timeout_seconds: int = 180) -> str:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        current = get_core_state()
        if current in target_states:
            return current
        time.sleep(2)
    expected = ", ".join(sorted(target_states))
    raise SupervisorAPIError(f"Timed out waiting for Home Assistant Core state: {expected}.")


def run_with_core_stopped(operation: Callable[[], Any]) -> Any:
    core_was_running = False
    core_stopped = False
    try:
        current_state = get_core_state()
        core_was_running = current_state in {"running", "started"}
        if core_was_running:
            supervisor_request("POST", "/core/stop")
            wait_for_core_state({"stopped"})
            core_stopped = True
        return operation()
    finally:
        if core_was_running and core_stopped:
            supervisor_request("POST", "/core/start")
            wait_for_core_state({"running", "started"})


def mutate_auth_storage(mutator: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]) -> dict[str, Any]:
    def operation() -> dict[str, Any]:
        auth_doc, provider_doc = read_auth_storage_documents()
        result = mutator(auth_doc, provider_doc)
        write_json_file(AUTH_STORAGE_FILE, auth_doc)
        write_json_file(AUTH_PROVIDER_STORAGE_FILE, provider_doc)
        return result

    return run_with_core_stopped(operation)


def mutate_core_config_storage(mutator: Callable[[dict[str, Any]], dict[str, Any]]) -> dict[str, Any]:
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
                    sync_auth_hashes_to_studio()
                except (StudioSyncError, AuthStorageError):
                    pass
        except (AuthStorageError, SupervisorAPIError):
            continue


def periodic_auth_sync_loop() -> None:
    while True:
        try:
            sync_addon_configuration_from_studio()
        except (StudioSyncError, AuthStorageError):
            pass
        try:
            sync_auth_hashes_to_studio()
        except (StudioSyncError, AuthStorageError):
            pass
        time.sleep(PERIODIC_AUTH_SYNC_INTERVAL_SECONDS)


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
    if not PERIODIC_AUTH_SYNC_ENABLED:
        return

    with _periodic_auth_sync_lock:
        if _periodic_auth_sync_started:
            return
        sync_thread = threading.Thread(target=periodic_auth_sync_loop, daemon=True)
        sync_thread.start()
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
    external_url = ""
    internal_url = ""
    desired_hostname = saved_credentials.get("hostname", "").strip()
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

    try:
        result = apply_studio_configuration_locally(payload)
    except (StudioSyncError, AuthStorageError, SupervisorAPIError) as exc:
        return jsonify({"error": "config_apply_failed", "detail": str(exc)}), 409

    return jsonify(result), 200


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


@app.get("/pairing")
def pairing_page():
    auth_guard = require_auth_or_redirect()
    if auth_guard is not None:
        return auth_guard
    if not has_saved_pairing_credentials():
        return render_template("pairing_onboarding.html", **load_pairing_context())
    return render_template("pairing.html", active_page="pairing", **load_pairing_context())


@app.get("/auth-management")
def auth_management_page():
    auth_guard = require_auth_or_redirect()
    if auth_guard is not None:
        return auth_guard
    pairing_guard = require_completed_pairing_or_redirect()
    if pairing_guard is not None:
        return pairing_guard
    return render_template("auth_management.html", active_page="auth_management", **load_auth_management_context())


@app.get("/settings")
def settings_page():
    auth_guard = require_auth_or_redirect()
    if auth_guard is not None:
        return auth_guard
    pairing_guard = require_completed_pairing_or_redirect()
    if pairing_guard is not None:
        return pairing_guard
    options = read_addon_options()
    return render_template(
        "settings.html",
        active_page="settings",
        ui_auth_enabled=bool(options["ui_auth_enabled"]),
        studio_base_url=str(options["studio_base_url"]),
        auto_enable_iframe_embedding=bool(options["auto_enable_iframe_embedding"]),
        has_ui_password=bool(str(options["ui_password"]).strip()),
    )


@app.post("/settings/security")
def settings_security():
    auth_guard = require_auth_or_redirect()
    if auth_guard is not None:
        return auth_guard
    pairing_guard = require_completed_pairing_or_redirect()
    if pairing_guard is not None:
        return pairing_guard

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
def pair_start():
    auth_guard = require_auth_or_redirect()
    if auth_guard is not None:
        return auth_guard

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
        print(
            "[powerhausbox-server] pair/init failed "
            f"status={exc.status_code} error={api_error!r} detail={api_detail!r} "
            f"request_id={request_id!r} cf_ray={cf_ray!r} server={server_header!r} "
            f"payload={exc.payload!r} body_preview={body_preview!r}",
            flush=True,
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
        url_sync_error = ""
        applied_external_url = ""
        try:
            if normalized_hostname:
                sync_homeassistant_hostname(normalized_hostname)
            applied_external_url = sync_homeassistant_urls(normalized_internal_url, normalized_external_url)
        except (SupervisorAPIError, AuthStorageError) as exc:
            url_sync_error = str(exc)

        config_sync_error = ""
        config_synced = False
        try:
            sync_addon_configuration_from_studio()
            config_synced = True
        except (StudioSyncError, AuthStorageError, SupervisorAPIError) as exc:
            config_sync_error = str(exc)

        auth_sync_error = ""
        auth_sync_result: dict[str, Any] = {}
        try:
            auth_sync_result = sync_auth_hashes_to_studio()
        except (StudioSyncError, AuthStorageError) as exc:
            auth_sync_error = str(exc)

        iframe_setup_error = ""
        if was_initial_pairing:
            iframe_setup_error = ensure_iframe_embedding_on_initial_pairing()

        return jsonify(
            {
                "state": "ready",
                "tunnel_hostname": tunnel_hostname,
                "external_url": applied_external_url,
                "internal_url": normalized_internal_url,
                "hostname": normalized_hostname,
                "urls_synced": not bool(url_sync_error),
                "urls_sync_error": url_sync_error,
                "config_synced": config_synced,
                "config_sync_error": config_sync_error,
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
def auth_create_service_user():
    auth_guard = require_auth_or_redirect()
    if auth_guard is not None:
        return auth_guard

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
        sync_result = sync_auth_hashes_to_studio()
        flash(
            (
                "Studio auth sync completed. "
                f"synced={sync_result['synced_count']} received={sync_result['received_count']}"
            ),
            "info",
        )
    except (StudioSyncError, AuthStorageError) as exc:
        flash(f"Studio auth sync failed: {exc}", "warning")
    return redirect_ingress_path("/auth-management")


@app.post("/auth/users/ensure-service")
def auth_ensure_service_user():
    auth_guard = require_auth_or_redirect()
    if auth_guard is not None:
        return auth_guard

    try:
        status, created = ensure_managed_service_user()
    except AuthStorageError as exc:
        flash(exc.message, "error")
        return redirect_ingress_path("/auth-management")

    if status == "present":
        flash("Managed service user already exists.", "info")
        try:
            sync_result = sync_auth_hashes_to_studio()
            flash(
                (
                    "Studio auth sync completed. "
                    f"synced={sync_result['synced_count']} received={sync_result['received_count']}"
                ),
                "info",
            )
        except (StudioSyncError, AuthStorageError) as exc:
            flash(f"Studio auth sync failed: {exc}", "warning")
        return redirect_ingress_path("/auth-management")

    if created is None:
        flash("Managed service user check completed.", "info")
        return redirect_ingress_path("/auth-management")

    flash(
        (
            "Managed service user was missing and has been recreated. "
            f"Username: {created['username']} (id: {created['user_id']})."
        ),
        "success",
    )
    try:
        sync_result = sync_auth_hashes_to_studio()
        flash(
            (
                "Studio auth sync completed. "
                f"synced={sync_result['synced_count']} received={sync_result['received_count']}"
            ),
            "info",
        )
    except (StudioSyncError, AuthStorageError) as exc:
        flash(f"Studio auth sync failed: {exc}", "warning")
    return redirect_ingress_path("/auth-management")


@app.post("/auth/users/create-normal")
def auth_create_normal_user():
    auth_guard = require_auth_or_redirect()
    if auth_guard is not None:
        return auth_guard

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
        sync_result = sync_auth_hashes_to_studio()
        flash(
            (
                "Studio auth sync completed. "
                f"synced={sync_result['synced_count']} received={sync_result['received_count']}"
            ),
            "info",
        )
    except (StudioSyncError, AuthStorageError) as exc:
        flash(f"Studio auth sync failed: {exc}", "warning")
    return redirect_ingress_path("/auth-management")


@app.post("/studio/auth/sync")
def studio_auth_sync_now():
    auth_guard = require_auth_or_redirect()
    if auth_guard is not None:
        return auth_guard

    try:
        sync_result = sync_auth_hashes_to_studio()
    except (StudioSyncError, AuthStorageError) as exc:
        flash(f"Studio auth sync failed: {exc}", "error")
        return redirect_ingress_path("/auth-management")

    sync_id = str(sync_result.get("sync_id", "")).strip()
    sync_id_suffix = f" sync_id={sync_id}" if sync_id else ""
    flash(
        (
            "Studio auth sync completed. "
            f"synced={sync_result['synced_count']} received={sync_result['received_count']}{sync_id_suffix}"
        ),
        "success",
    )
    return redirect_ingress_path("/auth-management")


@app.post("/studio/sync")
def studio_sync_now():
    auth_guard = require_auth_or_redirect()
    if auth_guard is not None:
        return auth_guard

    next_path = normalize_redirect_path(request.form.get("next", "/pairing"), "/pairing")

    config_result: dict[str, Any] = {}
    config_error = ""
    try:
        config_result = sync_addon_configuration_from_studio()
    except (StudioSyncError, SupervisorAPIError, AuthStorageError) as exc:
        config_error = str(exc)

    auth_result: dict[str, Any] = {}
    auth_error = ""
    try:
        auth_result = sync_auth_hashes_to_studio()
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
def sync_ha_urls_from_saved_credentials():
    auth_guard = require_auth_or_redirect()
    if auth_guard is not None:
        return auth_guard

    studio_sync_error = ""
    try:
        sync_addon_configuration_from_studio()
    except (StudioSyncError, AuthStorageError, SupervisorAPIError) as exc:
        studio_sync_error = str(exc)

    credentials = read_saved_credentials()
    hostname = credentials.get("hostname", "").strip()
    tunnel_hostname = credentials.get("tunnel_hostname", "").strip()
    internal_url = credentials.get("internal_url", "").strip()
    external_url = credentials.get("external_url", "").strip()
    if not tunnel_hostname:
        flash("No stored tunnel hostname found. Pair first.", "error")
        return redirect_ingress_path("/pairing")
    if not internal_url:
        flash("No stored internal_url found. Re-pair to fetch it from Studio.", "error")
        return redirect_ingress_path("/pairing")
    if not external_url:
        flash("No stored external_url found. Re-pair or sync from Studio to fetch it.", "error")
        return redirect_ingress_path("/pairing")

    try:
        normalized_hostname = normalize_hostname(hostname) if hostname else ""
        normalized_internal_url = normalize_internal_url(internal_url)
        normalized_external_url = normalize_external_url(external_url)
        if normalized_hostname:
            sync_homeassistant_hostname(normalized_hostname)
        applied_external_url = sync_homeassistant_urls(normalized_internal_url, normalized_external_url)
    except (SupervisorAPIError, AuthStorageError) as exc:
        flash(f"Failed to update Home Assistant host settings: {exc}", "error")
        return redirect_ingress_path("/pairing")

    if studio_sync_error:
        flash(f"Studio config refresh failed; applied local credentials instead: {studio_sync_error}", "warning")
    flash(
        (
            "Home Assistant host settings updated. "
            f"hostname={normalized_hostname} internal_url={normalized_internal_url} external_url={applied_external_url}"
        ),
        "success",
    )
    return redirect_ingress_path("/pairing")


@app.post("/token/delete")
def delete_token():
    auth_guard = require_auth_or_redirect()
    if auth_guard is not None:
        return auth_guard

    clear_pairing_state()
    clear_credentials()
    flash("Pairing credentials removed and tunnel process stopped.", "warning")
    return redirect_ingress_path("/pairing")


if __name__ == "__main__":
    port = int(os.getenv("WEB_PORT", "8099"))
    start_managed_service_user_watchdog()
    start_periodic_auth_sync()
    app.run(host="0.0.0.0", port=port, debug=False)
