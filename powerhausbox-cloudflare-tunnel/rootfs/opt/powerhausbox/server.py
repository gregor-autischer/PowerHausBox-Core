import hmac
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from flask import Flask, flash, jsonify, redirect, render_template, request, session, url_for

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "powerhausbox-dev-secret")
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

TOKEN_FILE = Path(os.getenv("TOKEN_FILE", "/data/tunnel_token"))
SECRETS_FILE = Path(os.getenv("SECRETS_FILE", "/data/pairing_secrets.json"))
OPTIONS_FILE = Path(os.getenv("OPTIONS_FILE", "/data/options.json"))
UI_PASSWORD = os.getenv("UI_PASSWORD", "change-this-password")
DEFAULT_STUDIO_BASE_URL = os.getenv("STUDIO_BASE_URL", "https://studio.powerhaus.ai")

PAIR_INIT_PATH = "/api/addon/pair/init/"
PAIR_COMPLETE_PATH = "/api/addon/pair/complete/"

_pairing_state_lock = threading.Lock()
_pairing_state: dict[str, Any] = {}


class PairingAPIError(Exception):
    def __init__(self, message: str, status_code: int | None = None, payload: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.payload = payload or {}


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


def to_positive_int(raw_value: Any, default: int) -> int:
    try:
        parsed = int(raw_value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def extract_api_error_code(payload: dict[str, Any]) -> str:
    return str(payload.get("error") or payload.get("code") or "").strip()


def post_json(url: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    req = urllib.request.Request(
        url=url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
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
            message="Studio API request failed.",
            status_code=exc.code,
            payload=payload_data,
        ) from exc
    except (TimeoutError, urllib.error.URLError) as exc:
        raise PairingAPIError("Could not reach Studio API.") from exc


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


@app.get("/")
def index():
    if not is_authenticated():
        return render_template("login.html")

    pairing_state = get_pairing_state()
    pending_verification_code = pairing_state.get("verification_code", "")
    poll_after_seconds = to_positive_int(pairing_state.get("poll_after_seconds", 2), 2)

    return render_template(
        "dashboard.html",
        status_text=token_status_text(),
        studio_base_url=get_studio_base_url(),
        pending_verification_code=pending_verification_code,
        poll_after_seconds=poll_after_seconds,
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
    app.run(host="0.0.0.0", port=port, debug=False)
