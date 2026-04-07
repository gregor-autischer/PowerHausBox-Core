#!/usr/bin/env bash
set -euo pipefail
umask 077

OPTIONS_FILE="/data/options.json"
TOKEN_FILE="/data/tunnel_token"
SECRETS_FILE="/data/pairing_secrets.json"
ADDON_INTERNAL_LOG="/data/powerhausbox.log"
HA_CONFIG_DIR="${HA_CONFIG_DIR:-/config}"

log() {
  local message="$*"
  local timestamp
  timestamp="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf '[powerhausbox-cloudflare] %s\n' "${message}"
  {
    printf '%s [powerhausbox-cloudflare] %s\n' "${timestamp}" "${message}" >> "${ADDON_INTERNAL_LOG}"
    if [ -f "${ADDON_INTERNAL_LOG}" ]; then
      local log_size
      log_size="$(wc -c < "${ADDON_INTERNAL_LOG}" 2>/dev/null || echo 0)"
      if [ "${log_size}" -gt 2097152 ]; then
        tail -c 1048576 "${ADDON_INTERNAL_LOG}" > "${ADDON_INTERNAL_LOG}.tmp" 2>/dev/null || true
        mv "${ADDON_INTERNAL_LOG}.tmp" "${ADDON_INTERNAL_LOG}" 2>/dev/null || true
      fi
    fi
  } 2>/dev/null || true
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

manual_apply_debug_mode_enabled() {
  if [ -f "${OPTIONS_FILE}" ]; then
    jq -r '.debug_manual_apply_mode // false' "${OPTIONS_FILE}" 2>/dev/null || echo "false"
  else
    echo "false"
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
export WEB_PORT=8100
export STUDIO_BASE_URL="${STUDIO_BASE_URL:-$(read_studio_base_url)}"
if [ -z "${STUDIO_BASE_URL}" ]; then
  export STUDIO_BASE_URL="https://studio.powerhaus.ai"
fi
export FLASK_SECRET_KEY="$(cat /proc/sys/kernel/random/uuid)"

install_integration() {
  local src="/opt/powerhausbox/integration/custom_components/powerhaus"
  local dst="${HA_CONFIG_DIR}/custom_components/powerhaus"

  if [ ! -d "${src}" ]; then
    log "No companion integration found at ${src}; skipping."
    return
  fi

  if [ -f "${dst}/manifest.json" ] && [ -f "${src}/manifest.json" ]; then
    local installed_version
    local source_version
    installed_version="$(jq -r '.version // ""' "${dst}/manifest.json" 2>/dev/null || true)"
    source_version="$(jq -r '.version // ""' "${src}/manifest.json" 2>/dev/null || true)"
    if [ "${installed_version}" = "${source_version}" ]; then
      log "PowerHaus integration v${installed_version} already installed."
      return
    fi
    log "PowerHaus integration v${installed_version} is installed; bundled v${source_version} is available. Skipping automatic overwrite until the user clicks update in the add-on UI."
    return
  elif [ -d "${dst}" ]; then
    log "PowerHaus integration directory already exists at ${dst}; skipping automatic overwrite until the user clicks update in the add-on UI."
    return
  else
    log "Installing PowerHaus backup integration..."
  fi

  mkdir -p "${HA_CONFIG_DIR}/custom_components"
  cp -r "${src}" "${dst}"
  log "PowerHaus integration installed to ${dst}."
  touch /data/.needs_ha_restart
}

WEB_PID=""
WEB_FAILURE_COUNT=0
WEB_HEALTHCHECK_FAILURES=0
WEB_FAILURE_WINDOW_STARTED_AT=0
WEB_HEALTHCHECK_URL="http://127.0.0.1:${WEB_PORT}/_powerhausbox/api/livez"

CLOUDFLARED_PID=""
CLOUDFLARED_FAILURE_COUNT=0
CLOUDFLARED_FAILURE_WINDOW_STARTED_AT=0
ACTIVE_TOKEN_FINGERPRINT=""
HAS_TOKEN_FILE_SUPPORT="false"

NGINX_PID=""
NGINX_FAILURE_COUNT=0
SERVICE_FAILURE_WINDOW_STARTED_AT=0
SERVICE_MAX_FAILURES=5

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
  if [ "$(manual_apply_debug_mode_enabled)" = "true" ]; then
    log "Manual apply debug mode is enabled; skipping automatic Home Assistant config apply."
    return
  fi

  if [ ! -f "${SECRETS_FILE}" ]; then
    return
  fi

  if python3 /opt/powerhausbox/server.py --sync-config-from-studio; then
    log "Refreshed saved Home Assistant host settings from Studio before startup apply."
  else
    log "Studio config refresh failed during startup; falling back to saved Home Assistant host settings."
  fi

  if python3 /opt/powerhausbox/server.py --apply-saved-config; then
    log "Home Assistant host settings synced from saved config."
    return
  fi

  log "Failed to apply saved Home Assistant host settings via verified startup sync."
}

start_web_server() {
  log "Starting ingress web UI on port ${WEB_PORT}..."
  python3 /opt/powerhausbox/server.py &
  WEB_PID=$!
}

stop_web_server() {
  if [ -n "${WEB_PID}" ] && kill -0 "${WEB_PID}" 2>/dev/null; then
    log "Stopping ingress web UI process..."
    kill "${WEB_PID}"
    wait "${WEB_PID}" 2>/dev/null || true
  fi
  WEB_PID=""
}

restart_web_server_or_exit() {
  local now=0
  now="$(date +%s)"
  if [ "${WEB_FAILURE_WINDOW_STARTED_AT}" -eq 0 ] || [ $(( now - WEB_FAILURE_WINDOW_STARTED_AT )) -gt 120 ]; then
    WEB_FAILURE_WINDOW_STARTED_AT="${now}"
    WEB_FAILURE_COUNT=0
  fi
  WEB_FAILURE_COUNT=$((WEB_FAILURE_COUNT + 1))
  stop_web_server
  if [ "${WEB_FAILURE_COUNT}" -ge 3 ]; then
    log "Ingress web UI failed ${WEB_FAILURE_COUNT} times within 120s. Exiting add-on so Supervisor can restart it."
    exit 1
  fi
  log "Ingress web UI failed unexpectedly; restarting (attempt ${WEB_FAILURE_COUNT}/3 within 120s)..."
  start_web_server
}

check_web_health() {
  if curl -fsS "${WEB_HEALTHCHECK_URL}" >/dev/null 2>&1; then
    WEB_HEALTHCHECK_FAILURES=0
    return 0
  fi
  WEB_HEALTHCHECK_FAILURES=$((WEB_HEALTHCHECK_FAILURES + 1))
  if [ "${WEB_HEALTHCHECK_FAILURES}" -ge 3 ]; then
    log "Ingress web UI health check failed ${WEB_HEALTHCHECK_FAILURES} times; forcing web process restart."
    WEB_HEALTHCHECK_FAILURES=0
    restart_web_server_or_exit
  fi
  return 1
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

restart_cloudflared_or_exit() {
  local now=0
  now="$(date +%s)"
  if [ "${CLOUDFLARED_FAILURE_WINDOW_STARTED_AT}" -eq 0 ] || [ $(( now - CLOUDFLARED_FAILURE_WINDOW_STARTED_AT )) -gt 120 ]; then
    CLOUDFLARED_FAILURE_WINDOW_STARTED_AT="${now}"
    CLOUDFLARED_FAILURE_COUNT=0
  fi
  CLOUDFLARED_FAILURE_COUNT=$((CLOUDFLARED_FAILURE_COUNT + 1))
  stop_cloudflared
  if [ "${CLOUDFLARED_FAILURE_COUNT}" -ge 5 ]; then
    log "cloudflared failed ${CLOUDFLARED_FAILURE_COUNT} times within 120s. Exiting add-on so Supervisor can restart it."
    exit 1
  fi
  log "cloudflared is not running; restarting (attempt ${CLOUDFLARED_FAILURE_COUNT}/5 within 120s)..."
  sync_homeassistant_urls_from_secrets
  start_cloudflared
}

start_nginx() {
  log "Starting nginx reverse proxy on port 8099..."
  nginx -c /etc/nginx/nginx.conf -g 'daemon off;' &
  NGINX_PID=$!
}

stop_nginx() {
  if [ -n "${NGINX_PID}" ] && kill -0 "${NGINX_PID}" 2>/dev/null; then
    log "Stopping nginx..."
    kill "${NGINX_PID}"
    wait "${NGINX_PID}" 2>/dev/null || true
  fi
  NGINX_PID=""
}

cleanup() {
  stop_nginx
  stop_cloudflared
  stop_web_server
}

trap cleanup EXIT INT TERM HUP QUIT

rm -f /data/.pairing_sync_done

if [ -n "${SUPERVISOR_TOKEN}" ]; then
  core_state="$(supervisor_api GET "/core/info" 2>/dev/null | jq -r '.data.state // empty' 2>/dev/null || true)"
  if [ "${core_state}" = "stopped" ] || [ "${core_state}" = "error" ]; then
    log "Home Assistant Core is ${core_state} — attempting recovery start..."
    supervisor_api POST "/core/start" >/dev/null 2>&1 || true
    log "Sent /core/start to Supervisor."
  fi
fi

install_integration || log "Integration install failed; backups may not work."
detect_cloudflared_auth_mode
start_web_server
start_nginx

log "Waiting for pairing credentials from ingress UI..."
while true; do
  NEW_TOKEN_FINGERPRINT="$(token_fingerprint)"
  if [ "${NEW_TOKEN_FINGERPRINT}" != "${ACTIVE_TOKEN_FINGERPRINT}" ]; then
    ACTIVE_TOKEN_FINGERPRINT="${NEW_TOKEN_FINGERPRINT}"
    stop_cloudflared
    if token_present; then
      if [ -f "/data/.pairing_sync_done" ]; then
        log "Pairing sync already completed by web UI; skipping redundant HA sync."
        rm -f "/data/.pairing_sync_done"
      else
        sync_homeassistant_urls_from_secrets
      fi
      start_cloudflared
    else
      log "No tunnel token configured."
    fi
  fi

  if token_present; then
    if [ -z "${CLOUDFLARED_PID}" ] || ! kill -0 "${CLOUDFLARED_PID}" 2>/dev/null; then
      restart_cloudflared_or_exit
    fi
  fi

  if ! kill -0 "${WEB_PID}" 2>/dev/null; then
    restart_web_server_or_exit
  fi

  _now="$(date +%s)"
  if [ "${SERVICE_FAILURE_WINDOW_STARTED_AT}" -eq 0 ] || [ $(( _now - SERVICE_FAILURE_WINDOW_STARTED_AT )) -gt 120 ]; then
    SERVICE_FAILURE_WINDOW_STARTED_AT="${_now}"
    NGINX_FAILURE_COUNT=0
  fi

  if [ -n "${NGINX_PID}" ] && ! kill -0 "${NGINX_PID}" 2>/dev/null; then
    NGINX_FAILURE_COUNT=$((NGINX_FAILURE_COUNT + 1))
    if [ "${NGINX_FAILURE_COUNT}" -ge "${SERVICE_MAX_FAILURES}" ]; then
      log "nginx failed ${NGINX_FAILURE_COUNT} times within 120s. Exiting add-on."
      exit 1
    else
      log "nginx died unexpectedly; restarting (attempt ${NGINX_FAILURE_COUNT}/${SERVICE_MAX_FAILURES})..."
      start_nginx
    fi
  fi

  check_web_health || true

  sleep 5
done
