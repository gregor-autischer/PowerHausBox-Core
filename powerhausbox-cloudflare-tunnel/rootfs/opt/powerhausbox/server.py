import base64
import binascii
import hmac
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable

from flask import Flask, Response, flash, jsonify, redirect, render_template, request, session, url_for


def _read_watchdog_interval_seconds() -> int:
    raw_value = os.getenv("SERVICE_USER_WATCHDOG_INTERVAL_SECONDS", "300").strip()
    try:
        parsed = int(raw_value)
    except ValueError:
        parsed = 300
    return parsed if parsed >= 60 else 60

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "powerhausbox-dev-secret")
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

TOKEN_FILE = Path(os.getenv("TOKEN_FILE", "/data/tunnel_token"))
SECRETS_FILE = Path(os.getenv("SECRETS_FILE", "/data/pairing_secrets.json"))
OPTIONS_FILE = Path(os.getenv("OPTIONS_FILE", "/data/options.json"))
MANAGED_SERVICE_USER_FILE = Path(os.getenv("MANAGED_SERVICE_USER_FILE", "/data/managed_service_user.json"))

HA_CONFIG_DIR = Path(os.getenv("HA_CONFIG_DIR", "/config"))
AUTH_STORAGE_FILE = HA_CONFIG_DIR / ".storage" / "auth"
AUTH_PROVIDER_STORAGE_FILE = HA_CONFIG_DIR / ".storage" / "auth_provider.homeassistant"

SUPERVISOR_URL = os.getenv("SUPERVISOR_URL", "http://supervisor").rstrip("/")
SUPERVISOR_TOKEN = os.getenv("SUPERVISOR_TOKEN", "")

UI_PASSWORD = os.getenv("UI_PASSWORD", "change-this-password")
DEFAULT_STUDIO_BASE_URL = os.getenv("STUDIO_BASE_URL", "https://studio.powerhaus.ai")

PAIR_INIT_PATH = "/api/addon/pair/init/"
PAIR_COMPLETE_PATH = "/api/addon/pair/complete/"

GROUP_ID_USER = "system-users"
VALID_USERNAME_RE = re.compile(r"[a-z0-9._@-]{3,64}")
VALID_BCRYPT_PREFIXES = (b"$2a$", b"$2b$", b"$2y$")
SERVICE_USER_WATCHDOG_ENABLED = os.getenv("SERVICE_USER_WATCHDOG_ENABLED", "true").strip().lower() != "false"
SERVICE_USER_WATCHDOG_INTERVAL_SECONDS = _read_watchdog_interval_seconds()

_pairing_state_lock = threading.Lock()
_pairing_state: dict[str, Any] = {}
_watchdog_started = False
_watchdog_lock = threading.Lock()


class PairingAPIError(Exception):
    def __init__(self, message: str, status_code: int | None = None, payload: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.payload = payload or {}


class AuthStorageError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class SupervisorAPIError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def is_authenticated() -> bool:
    return bool(session.get("authenticated"))


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
    }


def persist_credentials(cloudflare_tunnel_token: str, tunnel_hostname: str, box_api_token: str) -> None:
    payload = {
        "cloudflare_tunnel_token": cloudflare_tunnel_token,
        "tunnel_hostname": tunnel_hostname,
        "box_api_token": box_api_token,
    }
    write_secret_file(SECRETS_FILE, json.dumps(payload, ensure_ascii=True) + "\n")
    write_secret_file(TOKEN_FILE, cloudflare_tunnel_token + "\n")


def clear_credentials() -> None:
    TOKEN_FILE.unlink(missing_ok=True)
    SECRETS_FILE.unlink(missing_ok=True)


def token_status_text() -> str:
    creds = read_saved_credentials()
    if all(
        [
            creds.get("cloudflare_tunnel_token"),
            creds.get("tunnel_hostname"),
            creds.get("box_api_token"),
        ]
    ):
        return f"Paired and ready. Tunnel hostname: {creds['tunnel_hostname']}"
    return "No pairing credentials configured yet."


def get_studio_base_url() -> str:
    options = read_json_file(OPTIONS_FILE)
    base_url = str(options.get("studio_base_url") or options.get("STUDIO_BASE_URL") or "").strip()
    if not base_url:
        base_url = DEFAULT_STUDIO_BASE_URL
    return base_url.rstrip("/")


def is_valid_https_url(base_url: str) -> bool:
    parsed = urllib.parse.urlparse(base_url)
    return parsed.scheme == "https" and bool(parsed.netloc)


def valid_pair_code(pair_code: str) -> bool:
    return bool(re.fullmatch(r"\d{6}", pair_code))


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
        if body:
            try:
                payload_data = json.loads(body)
            except json.JSONDecodeError:
                payload_data = {}
        raise PairingAPIError(
            message="API request failed.",
            status_code=exc.code,
            payload=payload_data,
        ) from exc
    except (TimeoutError, urllib.error.URLError) as exc:
        raise PairingAPIError("Could not reach API endpoint.") from exc


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


def mutate_auth_storage(mutator: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]) -> dict[str, Any]:
    core_was_running = False
    core_stopped = False
    try:
        current_state = get_core_state()
        core_was_running = current_state in {"running", "started"}
        if core_was_running:
            supervisor_request("POST", "/core/stop")
            wait_for_core_state({"stopped"})
            core_stopped = True

        auth_doc, provider_doc = read_auth_storage_documents()
        result = mutator(auth_doc, provider_doc)
        write_json_file(AUTH_STORAGE_FILE, auth_doc)
        write_json_file(AUTH_PROVIDER_STORAGE_FILE, provider_doc)
        return result
    finally:
        if core_was_running and core_stopped:
            supervisor_request("POST", "/core/start")
            wait_for_core_state({"running", "started"})


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
            ensure_managed_service_user()
        except (AuthStorageError, SupervisorAPIError):
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


@app.before_request
def ensure_watchdog_started() -> None:
    start_managed_service_user_watchdog()


@app.get("/")
def index():
    if not is_authenticated():
        return render_template("login.html")

    pairing_state = get_pairing_state()
    pending_verification_code = pairing_state.get("verification_code", "")
    poll_after_seconds = to_positive_int(pairing_state.get("poll_after_seconds", 2), 2)

    auth_rows: list[dict[str, Any]] = []
    auth_error = ""
    managed_status = "No managed internal service user configured."
    try:
        auth_rows = list_homeassistant_hash_users()
        managed_status = managed_service_user_status(auth_rows)
    except AuthStorageError as exc:
        auth_error = exc.message

    return render_template(
        "dashboard.html",
        status_text=token_status_text(),
        studio_base_url=get_studio_base_url(),
        pending_verification_code=pending_verification_code,
        poll_after_seconds=poll_after_seconds,
        auth_user_count=len(auth_rows),
        auth_storage_error=auth_error,
        managed_service_status=managed_status,
        auth_storage_path=str(HA_CONFIG_DIR / ".storage"),
        managed_watchdog_enabled=SERVICE_USER_WATCHDOG_ENABLED,
        managed_watchdog_interval_seconds=SERVICE_USER_WATCHDOG_INTERVAL_SECONDS,
    )


@app.post("/login")
def login():
    password = request.form.get("password", "")
    if hmac.compare_digest(password, UI_PASSWORD):
        session["authenticated"] = True
        flash("Login successful.", "success")
        return redirect(url_for("index"))
    flash("Invalid password.", "error")
    return redirect(url_for("index"))


@app.post("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("index"))


@app.post("/pair/start")
def pair_start():
    if not is_authenticated():
        return redirect(url_for("index"))

    pair_code = request.form.get("pair_code", "").strip()
    if not valid_pair_code(pair_code):
        flash("Pair code must be exactly 6 digits.", "error")
        return redirect(url_for("index"))

    base_url = get_studio_base_url()
    if not is_valid_https_url(base_url):
        flash("studio_base_url must use HTTPS.", "error")
        return redirect(url_for("index"))

    try:
        status_code, response = post_json(
            f"{base_url}{PAIR_INIT_PATH}",
            {"pair_code": pair_code},
        )
    except PairingAPIError as exc:
        api_error = extract_api_error_code(exc.payload)
        if api_error == "invalid_code":
            flash("Pair code is invalid or expired.", "error")
        elif api_error == "rate_limited":
            flash("Too many attempts. Please wait and try again.", "error")
        elif exc.status_code:
            flash(f"Pairing init failed (HTTP {exc.status_code}).", "error")
        else:
            flash(exc.message, "error")
        return redirect(url_for("index"))

    if status_code != 200 or response.get("status") != "pending_approval":
        flash("Unexpected response from Studio during pairing init.", "error")
        return redirect(url_for("index"))

    session_token = str(response.get("session_token", "")).strip()
    verification_code = str(response.get("verification_code", "")).strip()
    expires_in_seconds = to_positive_int(response.get("expires_in_seconds", 300), 300)
    poll_after_seconds = to_positive_int(response.get("poll_after_seconds", 2), 2)

    if not session_token:
        flash("Studio did not return a pairing session.", "error")
        return redirect(url_for("index"))
    if not re.fullmatch(r"\d{2}", verification_code):
        flash("Studio returned an invalid verification code.", "error")
        return redirect(url_for("index"))

    set_pairing_state(
        session_token=session_token,
        verification_code=verification_code,
        poll_after_seconds=poll_after_seconds,
        expires_in_seconds=expires_in_seconds,
        base_url=base_url,
    )
    flash("Pairing initialized. Approve the shown 2-digit code in Studio.", "success")
    return redirect(url_for("index"))


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
        tunnel_hostname = str(response.get("tunnel_hostname", "")).strip()
        cloudflare_tunnel_token = str(response.get("cloudflare_tunnel_token", "")).strip()
        box_api_token = str(response.get("box_api_token", "")).strip()
        if not tunnel_hostname or not cloudflare_tunnel_token or not box_api_token:
            clear_pairing_state()
            return jsonify({"state": "error", "message": "Studio returned incomplete credentials."}), 200

        persist_credentials(cloudflare_tunnel_token, tunnel_hostname, box_api_token)
        clear_pairing_state()
        return jsonify(
            {
                "state": "ready",
                "tunnel_hostname": tunnel_hostname,
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
    if not is_authenticated():
        return redirect(url_for("index"))

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
        return redirect(url_for("index"))

    flash(
        (
            "Hidden service user created and stored as managed user. "
            f"Username: {created['username']} (id: {created['user_id']})."
        ),
        "success",
    )
    return redirect(url_for("index"))


@app.post("/auth/users/ensure-service")
def auth_ensure_service_user():
    if not is_authenticated():
        return redirect(url_for("index"))

    try:
        status, created = ensure_managed_service_user()
    except AuthStorageError as exc:
        flash(exc.message, "error")
        return redirect(url_for("index"))

    if status == "present":
        flash("Managed service user already exists.", "info")
        return redirect(url_for("index"))

    if created is None:
        flash("Managed service user check completed.", "info")
        return redirect(url_for("index"))

    flash(
        (
            "Managed service user was missing and has been recreated. "
            f"Username: {created['username']} (id: {created['user_id']})."
        ),
        "success",
    )
    return redirect(url_for("index"))


@app.post("/auth/users/create-normal")
def auth_create_normal_user():
    if not is_authenticated():
        return redirect(url_for("index"))

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
        return redirect(url_for("index"))

    flash(
        f"Normal user created. Username: {created['username']} (id: {created['user_id']}).",
        "success",
    )
    return redirect(url_for("index"))


@app.post("/token/delete")
def delete_token():
    if not is_authenticated():
        return redirect(url_for("index"))

    clear_pairing_state()
    clear_credentials()
    flash("Pairing credentials removed and tunnel process stopped.", "warning")
    return redirect(url_for("index"))


if __name__ == "__main__":
    port = int(os.getenv("WEB_PORT", "8099"))
    start_managed_service_user_watchdog()
    app.run(host="0.0.0.0", port=port, debug=False)
