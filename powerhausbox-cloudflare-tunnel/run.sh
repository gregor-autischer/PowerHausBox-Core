#!/usr/bin/env bash
set -euo pipefail
umask 077

OPTIONS_FILE="/data/options.json"
TOKEN_FILE="/data/tunnel_token"
SECRETS_FILE="/data/pairing_secrets.json"

log() {
  printf '[powerhausbox-cloudflare] %s\n' "$*"
}

read_ui_password() {
  if [ -f "${OPTIONS_FILE}" ]; then
    jq -r '.ui_password // empty' "${OPTIONS_FILE}" 2>/dev/null || true
  fi
}

read_studio_base_url() {
  if [ -f "${OPTIONS_FILE}" ]; then
    jq -r '.studio_base_url // .STUDIO_BASE_URL // empty' "${OPTIONS_FILE}" 2>/dev/null || true
  fi
}

UI_PASSWORD="$(read_ui_password)"
if [ -z "${UI_PASSWORD}" ]; then
  UI_PASSWORD="change-this-password"
  log "No ui_password found in options, using default value."
fi

export UI_PASSWORD
export TOKEN_FILE
export OPTIONS_FILE
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
    log "SUPERVISOR_TOKEN unavailable; skipping Home Assistant URL sync."
    return
  fi

  if [ ! -f "${SECRETS_FILE}" ]; then
    return
  fi

  local tunnel_hostname=""
  tunnel_hostname="$(jq -r '.tunnel_hostname // empty' "${SECRETS_FILE}" 2>/dev/null || true)"
  if [ -z "${tunnel_hostname}" ]; then
    return
  fi

  local internal_url=""
  internal_url="$(jq -r '.internal_url // empty' "${SECRETS_FILE}" 2>/dev/null || true)"
  if [ -z "${internal_url}" ]; then
    log "Stored internal_url missing; re-pair to receive it from Studio."
    return
  fi

  case "${internal_url}" in
    http://*|https://*) ;;
    *) internal_url="http://${internal_url}" ;;
  esac
  internal_url="${internal_url%/}"

  local raw_host="${tunnel_hostname#http://}"
  raw_host="${raw_host#https://}"
  raw_host="${raw_host%%/*}"
  if [ -z "${raw_host}" ]; then
    log "Stored tunnel hostname is invalid; skipping Home Assistant URL sync."
    return
  fi

  local external_url="https://${raw_host}"
  local payload=""
  payload="$(jq -cn --arg internal_url "${internal_url}" --arg external_url "${external_url}" '{internal_url:$internal_url,external_url:$external_url}')"

  if ! curl -fsS -X POST \
    -H "Authorization: Bearer ${SUPERVISOR_TOKEN}" \
    -H "Content-Type: application/json" \
    -d "${payload}" \
    "http://supervisor/core/api/config/core/update" >/dev/null; then
    log "Failed to sync Home Assistant internal/external URLs."
    return
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
