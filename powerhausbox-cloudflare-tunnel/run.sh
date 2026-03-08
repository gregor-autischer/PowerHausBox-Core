#!/usr/bin/env bash
set -euo pipefail
umask 077

OPTIONS_FILE="/data/options.json"
TOKEN_FILE="/data/tunnel_token"
SECRETS_FILE="/data/pairing_secrets.json"
HA_CONFIG_DIR="${HA_CONFIG_DIR:-/config}"
CORE_CONFIG_FILE="${HA_CONFIG_DIR}/.storage/core.config"

log() {
  printf '[powerhausbox-cloudflare] %s\n' "$*"
}

supervisor_api() {
  local method="$1"
  local path="$2"
  local data="${3:-}"
  local url="${SUPERVISOR_URL}${path}"

  if [ -n "${data}" ]; then
    curl -fsS -X "${method}" \
      -H "Authorization: Bearer ${SUPERVISOR_TOKEN}" \
      -H "Content-Type: application/json" \
      -d "${data}" \
      "${url}"
    return
  fi

  curl -fsS -X "${method}" \
    -H "Authorization: Bearer ${SUPERVISOR_TOKEN}" \
    "${url}"
}

read_container_env() {
  local name=""
  for name in "$@"; do
    if [ -n "${!name:-}" ]; then
      printf '%s' "${!name}"
      return
    fi

    local path="/run/s6/container_environment/${name}"
    if [ -f "${path}" ]; then
      tr -d '\r\n' < "${path}"
      return
    fi
  done
}

wait_for_core_state() {
  local target_state="$1"
  local timeout_seconds="${2:-180}"
  local deadline=$(( $(date +%s) + timeout_seconds ))

  while [ "$(date +%s)" -lt "${deadline}" ]; do
    local current_state=""
    current_state="$(supervisor_api GET "/core/info" | jq -r '.data.state // empty' 2>/dev/null || true)"
    if [ "${current_state}" = "${target_state}" ]; then
      return 0
    fi
    sleep 2
  done

  return 1
}

read_ui_password() {
  if [ -f "${OPTIONS_FILE}" ]; then
    jq -r '.ui_password // empty' "${OPTIONS_FILE}" 2>/dev/null || true
  fi
}

read_ui_auth_enabled() {
  if [ -f "${OPTIONS_FILE}" ]; then
    jq -r '.ui_auth_enabled // .UI_AUTH_ENABLED // false' "${OPTIONS_FILE}" 2>/dev/null || true
  fi
}

read_studio_base_url() {
  if [ -f "${OPTIONS_FILE}" ]; then
    jq -r '.studio_base_url // .STUDIO_BASE_URL // empty' "${OPTIONS_FILE}" 2>/dev/null || true
  fi
}

UI_AUTH_ENABLED="$(read_ui_auth_enabled)"
if [ -z "${UI_AUTH_ENABLED}" ]; then
  UI_AUTH_ENABLED="false"
fi

UI_PASSWORD="$(read_ui_password)"
if [ -z "${UI_PASSWORD}" ]; then
  UI_PASSWORD="change-this-password"
  if [ "${UI_AUTH_ENABLED}" = "true" ]; then
    log "ui_auth_enabled is true but ui_password is empty, using fallback value."
  fi
fi

SUPERVISOR_TOKEN="${SUPERVISOR_TOKEN:-$(read_container_env SUPERVISOR_TOKEN HASSIO_TOKEN)}"
SUPERVISOR_URL="${SUPERVISOR_URL:-$(read_container_env SUPERVISOR_URL)}"
if [ -z "${SUPERVISOR_URL}" ]; then
  SUPERVISOR_URL="http://supervisor"
fi

export UI_PASSWORD
export UI_AUTH_ENABLED
export TOKEN_FILE
export OPTIONS_FILE
export SUPERVISOR_TOKEN
export SUPERVISOR_URL
export WEB_PORT=8099
export STUDIO_BASE_URL="${STUDIO_BASE_URL:-$(read_studio_base_url)}"
if [ -z "${STUDIO_BASE_URL}" ]; then
  export STUDIO_BASE_URL="https://studio.powerhaus.ai"
fi
export FLASK_SECRET_KEY="$(cat /proc/sys/kernel/random/uuid)"

log "Applying iframe embedding configuration if enabled..."
if ! python3 /opt/powerhausbox/iframe_configurator.py; then
  log "Iframe embedding configuration encountered issues; continuing startup."
fi

log "Starting ingress web UI on port ${WEB_PORT}..."
python3 /opt/powerhausbox/server.py &
WEB_PID=$!

CLOUDFLARED_PID=""
ACTIVE_TOKEN_FINGERPRINT=""
HAS_TOKEN_FILE_SUPPORT="false"

detect_cloudflared_auth_mode() {
  if cloudflared tunnel run --help 2>&1 | grep -q -- "--token-file"; then
    HAS_TOKEN_FILE_SUPPORT="true"
    log "Using cloudflared token-file authentication."
  else
    HAS_TOKEN_FILE_SUPPORT="false"
    log "cloudflared lacks --token-file; using TUNNEL_TOKEN environment variable."
  fi
}

token_fingerprint() {
  if [ ! -f "${TOKEN_FILE}" ]; then
    printf ''
    return
  fi
  cksum "${TOKEN_FILE}" | awk '{print $1 ":" $2}'
}

token_present() {
  if [ ! -f "${TOKEN_FILE}" ]; then
    return 1
  fi
  [ -n "$(tr -d '\r\n' < "${TOKEN_FILE}")" ]
}

sync_homeassistant_urls_from_secrets() {
  if [ -z "${SUPERVISOR_TOKEN:-}" ]; then
    log "SUPERVISOR_TOKEN unavailable; skipping Home Assistant host setting sync."
    return
  fi

  if [ ! -f "${SECRETS_FILE}" ]; then
    return
  fi

  local hostname=""
  hostname="$(jq -r '.hostname // empty' "${SECRETS_FILE}" 2>/dev/null || true)"
  if [ -n "${hostname}" ]; then
    if ! supervisor_api POST "/host/options" "{\"hostname\":\"${hostname}\"}" >/dev/null; then
      log "Failed to apply stored Home Assistant hostname."
    else
      log "Home Assistant hostname synced to ${hostname}."
    fi
  fi

  local internal_url=""
  internal_url="$(jq -r '.internal_url // empty' "${SECRETS_FILE}" 2>/dev/null || true)"
  if [ -z "${internal_url}" ]; then
    log "Stored internal_url missing; re-pair to receive it from Studio."
    return
  fi

  local external_url=""
  external_url="$(jq -r '.external_url // empty' "${SECRETS_FILE}" 2>/dev/null || true)"
  if [ -z "${external_url}" ]; then
    log "Stored external_url missing; re-pair or sync from Studio to receive it."
    return
  fi

  case "${internal_url}" in
    http://*|https://*) ;;
    *) internal_url="http://${internal_url}" ;;
  esac
  internal_url="${internal_url%/}"
  case "${external_url}" in
    http://*|https://*) ;;
    *) external_url="https://${external_url}" ;;
  esac
  external_url="${external_url%/}"
  if [ ! -f "${CORE_CONFIG_FILE}" ]; then
    log "Home Assistant core config storage not found at ${CORE_CONFIG_FILE}; skipping URL sync."
    return
  fi

  local current_state=""
  current_state="$(supervisor_api GET "/core/info" | jq -r '.data.state // empty' 2>/dev/null || true)"
  local core_was_running="false"
  if [ "${current_state}" = "running" ] || [ "${current_state}" = "started" ]; then
    core_was_running="true"
    if ! supervisor_api POST "/core/stop" >/dev/null; then
      log "Failed to stop Home Assistant Core before URL sync."
      return
    fi
    if ! wait_for_core_state "stopped" 180; then
      log "Timed out waiting for Home Assistant Core to stop before URL sync."
      return
    fi
  fi

  local tmp_file=""
  tmp_file="$(mktemp "${CORE_CONFIG_FILE}.XXXXXX")"
  if ! jq \
    --arg internal_url "${internal_url}" \
    --arg external_url "${external_url}" \
    '.data.internal_url = $internal_url | .data.external_url = $external_url' \
    "${CORE_CONFIG_FILE}" > "${tmp_file}"; then
    rm -f "${tmp_file}"
    log "Failed to write updated Home Assistant core config storage."
    if [ "${core_was_running}" = "true" ]; then
      supervisor_api POST "/core/start" >/dev/null || true
    fi
    return
  fi

  chmod 600 "${tmp_file}"
  mv "${tmp_file}" "${CORE_CONFIG_FILE}"

  if [ "${core_was_running}" = "true" ]; then
    if ! supervisor_api POST "/core/start" >/dev/null; then
      log "Updated Home Assistant URLs, but failed to restart Home Assistant Core."
      return
    fi
    if ! wait_for_core_state "running" 180 && ! wait_for_core_state "started" 180; then
      log "Updated Home Assistant URLs, but Home Assistant Core did not reach a running state."
      return
    fi
  fi

  log "Home Assistant URLs synced."
}

start_cloudflared() {
  log "Starting cloudflared tunnel process..."
  if [ "${HAS_TOKEN_FILE_SUPPORT}" = "true" ]; then
    cloudflared tunnel --no-autoupdate run --token-file "${TOKEN_FILE}" &
  else
    local token=""
    token="$(tr -d '\r\n' < "${TOKEN_FILE}")"
    if [ -z "${token}" ]; then
      log "Token file is empty; not starting cloudflared."
      return
    fi
    TUNNEL_TOKEN="${token}" cloudflared tunnel --no-autoupdate run &
  fi
  CLOUDFLARED_PID=$!
}

stop_cloudflared() {
  if [ -n "${CLOUDFLARED_PID}" ] && kill -0 "${CLOUDFLARED_PID}" 2>/dev/null; then
    log "Stopping cloudflared tunnel process..."
    kill "${CLOUDFLARED_PID}"
    wait "${CLOUDFLARED_PID}" 2>/dev/null || true
  fi
  CLOUDFLARED_PID=""
}

cleanup() {
  stop_cloudflared
  if kill -0 "${WEB_PID}" 2>/dev/null; then
    kill "${WEB_PID}"
    wait "${WEB_PID}" 2>/dev/null || true
  fi
}

trap cleanup EXIT INT TERM

detect_cloudflared_auth_mode

log "Waiting for pairing credentials from ingress UI..."
while true; do
  NEW_TOKEN_FINGERPRINT="$(token_fingerprint)"
  if [ "${NEW_TOKEN_FINGERPRINT}" != "${ACTIVE_TOKEN_FINGERPRINT}" ]; then
    ACTIVE_TOKEN_FINGERPRINT="${NEW_TOKEN_FINGERPRINT}"
    stop_cloudflared
    if token_present; then
      sync_homeassistant_urls_from_secrets
      start_cloudflared
    else
      log "No tunnel token configured."
    fi
  fi

  if token_present; then
    if [ -z "${CLOUDFLARED_PID}" ] || ! kill -0 "${CLOUDFLARED_PID}" 2>/dev/null; then
      log "cloudflared is not running; restarting..."
      sync_homeassistant_urls_from_secrets
      start_cloudflared
    fi
  fi

  if ! kill -0 "${WEB_PID}" 2>/dev/null; then
    log "Ingress UI stopped unexpectedly. Exiting add-on."
    exit 1
  fi

  sleep 5
done
