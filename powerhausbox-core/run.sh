#!/usr/bin/env bash
set -euo pipefail
umask 077

OPTIONS_FILE="/data/options.json"
TOKEN_FILE="/data/tunnel_token"
SECRETS_FILE="/data/pairing_secrets.json"
ADDON_INTERNAL_LOG="/data/powerhausbox.log"
HA_CONFIG_DIR="${HA_CONFIG_DIR:-/config}"
CORE_CONFIG_FILE="${HA_CONFIG_DIR}/.storage/core.config"

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

read_ssh_username() {
  if [ -f "${OPTIONS_FILE}" ]; then
    jq -r '.ssh.username // "hassio"' "${OPTIONS_FILE}" 2>/dev/null || echo "hassio"
  else
    echo "hassio"
  fi
}

manual_apply_debug_mode_enabled() {
  if [ -f "${OPTIONS_FILE}" ]; then
    jq -r '.debug_manual_apply_mode // false' "${OPTIONS_FILE}" 2>/dev/null || echo "false"
  else
    echo "false"
  fi
}

use_external_ssh_addon() {
  if [ -f "${OPTIONS_FILE}" ]; then
    jq -r '.use_external_ssh_addon // true' "${OPTIONS_FILE}" 2>/dev/null || echo "true"
  else
    echo "true"
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

# ---------------------------------------------------------------------------
# SSH Initialization
# ---------------------------------------------------------------------------

init_ssh() {
  local username
  username="$(read_ssh_username)"

  log "Initializing SSH..."

  # --- Generate host keys if they don't exist ---
  if [ ! -f "/data/ssh_host_rsa_key" ]; then
    log "Generating RSA host key..."
    ssh-keygen -t rsa -b 4096 -f /data/ssh_host_rsa_key -N "" || {
      log "Failed to generate RSA host key"
      return 1
    }
  fi

  if [ ! -f "/data/ssh_host_ed25519_key" ]; then
    log "Generating ED25519 host key..."
    ssh-keygen -t ed25519 -f /data/ssh_host_ed25519_key -N "" || {
      log "Failed to generate ED25519 host key"
      return 1
    }
  fi

  # --- Create/update user ---
  if id -u "${username}" > /dev/null 2>&1; then
    log "User '${username}' already exists."
  else
    log "Creating user '${username}'..."
  fi
  adduser -D -s /bin/bash "${username}" 2>/dev/null || true

  # --- Setup authorized keys directory ---
  mkdir -p "/home/${username}/.ssh"
  touch "/home/${username}/.ssh/authorized_keys"
  chmod 600 "/home/${username}/.ssh/authorized_keys"

  # Add locally-configured keys from add-on options
  if [ -f "${OPTIONS_FILE}" ]; then
    local keys_json
    keys_json="$(jq -r '.ssh.authorized_keys // [] | .[]' "${OPTIONS_FILE}" 2>/dev/null || true)"
    if [ -n "${keys_json}" ]; then
      log "Adding locally-configured authorized keys..."
      echo "${keys_json}" >> "/home/${username}/.ssh/authorized_keys"
    fi
  fi

  chown -R "${username}:${username}" "/home/${username}/.ssh"

  # --- Configure sshd ---
  sed -i "s|#HostKey /data/ssh_host_rsa_key|HostKey /data/ssh_host_rsa_key|" /etc/ssh/sshd_config
  sed -i "s|#HostKey /data/ssh_host_ed25519_key|HostKey /data/ssh_host_ed25519_key|" /etc/ssh/sshd_config
  sed -i "s/AllowUsers .*/AllowUsers ${username}/" /etc/ssh/sshd_config

  # Configure TCP forwarding
  local allow_tcp_forwarding
  allow_tcp_forwarding="$(jq -r '.ssh.allow_tcp_forwarding // false' "${OPTIONS_FILE}" 2>/dev/null || echo "false")"
  if [ "${allow_tcp_forwarding}" = "true" ]; then
    sed -i "s/AllowTcpForwarding.*/AllowTcpForwarding yes/" /etc/ssh/sshd_config
  else
    sed -i "s/AllowTcpForwarding.*/AllowTcpForwarding no/" /etc/ssh/sshd_config
  fi

  # Configure SFTP
  local sftp_enabled
  sftp_enabled="$(jq -r '.ssh.sftp // false' "${OPTIONS_FILE}" 2>/dev/null || echo "false")"
  if [ "${sftp_enabled}" = "true" ]; then
    sed -i "s|#Subsystem sftp|Subsystem sftp|" /etc/ssh/sshd_config
  fi

  # --- Generate ttyd internal credential ---
  if [ ! -f "/data/ttyd_credential" ]; then
    local ttyd_pass
    ttyd_pass="$(head -c 32 /dev/urandom | base64 | tr -dc 'a-zA-Z0-9' | head -c 32)"
    echo "${ttyd_pass}" > /data/ttyd_credential
    chmod 600 /data/ttyd_credential
    log "Generated internal ttyd credential."
  fi

  # --- Setup user environment ---
  mkdir -p /data/user_home

  # Link useful Home Assistant directories to user home
  for dir in config addons share backup media ssl; do
    if [ -d "/${dir}" ]; then
      ln -sf "/${dir}" "/home/${username}/${dir}" 2>/dev/null || true
    fi
  done

  # Persist shell history
  if [ ! -f "/data/user_home/.bash_history" ]; then
    touch /data/user_home/.bash_history
  fi
  ln -sf /data/user_home/.bash_history "/home/${username}/.bash_history"

  # Set SUPERVISOR_TOKEN in user environment for SSH sessions
  echo "SUPERVISOR_TOKEN=${SUPERVISOR_TOKEN}" > "/home/${username}/.ssh/environment" 2>/dev/null || true
  chmod 600 "/home/${username}/.ssh/environment" 2>/dev/null || true

  # Ensure correct ownership
  chown -R "${username}:${username}" "/home/${username}"

  log "SSH initialization complete."
}

# ---------------------------------------------------------------------------
# Integration auto-installer
# ---------------------------------------------------------------------------

install_integration() {
  local src="/opt/powerhausbox/integration/custom_components/powerhaus"
  local dst="${HA_CONFIG_DIR}/custom_components/powerhaus"

  if [ ! -d "${src}" ]; then
    log "No companion integration found at ${src}; skipping."
    return
  fi

  # Check if already installed with same version
  if [ -f "${dst}/manifest.json" ] && [ -f "${src}/manifest.json" ]; then
    local installed_version
    local source_version
    installed_version="$(jq -r '.version // ""' "${dst}/manifest.json" 2>/dev/null || true)"
    source_version="$(jq -r '.version // ""' "${src}/manifest.json" 2>/dev/null || true)"
    if [ "${installed_version}" = "${source_version}" ]; then
      log "PowerHaus integration v${installed_version} already installed."
      return
    fi
    log "Updating PowerHaus integration from v${installed_version} to v${source_version}..."
  else
    log "Installing PowerHaus backup integration..."
  fi

  mkdir -p "${HA_CONFIG_DIR}/custom_components"
  cp -r "${src}" "${dst}"
  log "PowerHaus integration installed to ${dst}."
  # Flag that a restart is needed (Flask UI will show a restart prompt)
  touch /data/.needs_ha_restart
}

# ---------------------------------------------------------------------------
# Process management: sshd
# ---------------------------------------------------------------------------

SSHD_PID=""

start_sshd() {
  log "Starting SSH daemon..."
  /usr/sbin/sshd -D -e &
  SSHD_PID=$!
}

stop_sshd() {
  if [ -n "${SSHD_PID}" ] && kill -0 "${SSHD_PID}" 2>/dev/null; then
    log "Stopping SSH daemon..."
    kill "${SSHD_PID}"
    wait "${SSHD_PID}" 2>/dev/null || true
  fi
  SSHD_PID=""
}

# ---------------------------------------------------------------------------
# Process management: ttyd (web terminal)
# ---------------------------------------------------------------------------

TTYD_PID=""

start_ttyd() {
  local username
  username="$(read_ssh_username)"

  local ttyd_pass=""
  if [ -f "/data/ttyd_credential" ]; then
    ttyd_pass="$(cat /data/ttyd_credential)"
  else
    log "No ttyd credential found, running terminal without auth!"
  fi

  # xterm.js theme (One Dark inspired, matching Studio aesthetic)
  local ttyd_theme='{"background":"#1e1e2e","foreground":"#cdd6f4","cursor":"#f5e0dc","selectionBackground":"#585b70","black":"#45475a","red":"#f38ba8","green":"#a6e3a1","yellow":"#f9e2af","blue":"#89b4fa","magenta":"#cba6f7","cyan":"#94e2d5","white":"#bac2de","brightBlack":"#585b70","brightRed":"#f38ba8","brightGreen":"#a6e3a1","brightYellow":"#f9e2af","brightBlue":"#89b4fa","brightMagenta":"#cba6f7","brightCyan":"#94e2d5","brightWhite":"#a6adc8"}'

  log "Starting web terminal (ttyd) on port 7681..."
  if [ -n "${ttyd_pass}" ]; then
    ttyd \
      --port 7681 \
      --interface 127.0.0.1 \
      --writable \
      --base-path /_powerhausbox/api/terminal \
      --credential "powerhaus:${ttyd_pass}" \
      --client-option fontFamily="'JetBrains Mono','Fira Code','Cascadia Code',Menlo,Monaco,'Courier New',monospace" \
      --client-option fontSize=14 \
      --client-option lineHeight=1.2 \
      --client-option cursorBlink=true \
      --client-option theme="${ttyd_theme}" \
      login -f "${username}" &
  else
    ttyd \
      --port 7681 \
      --interface 127.0.0.1 \
      --writable \
      --base-path /_powerhausbox/api/terminal \
      --client-option fontFamily="'JetBrains Mono','Fira Code','Cascadia Code',Menlo,Monaco,'Courier New',monospace" \
      --client-option fontSize=14 \
      --client-option lineHeight=1.2 \
      --client-option cursorBlink=true \
      --client-option theme="${ttyd_theme}" \
      login -f "${username}" &
  fi
  TTYD_PID=$!
}

stop_ttyd() {
  if [ -n "${TTYD_PID}" ] && kill -0 "${TTYD_PID}" 2>/dev/null; then
    log "Stopping web terminal..."
    kill "${TTYD_PID}"
    wait "${TTYD_PID}" 2>/dev/null || true
  fi
  TTYD_PID=""
}

# ---------------------------------------------------------------------------
# Process management: terminal proxy (aiohttp WebSocket relay on port 7682)
# ---------------------------------------------------------------------------

TERMINAL_PROXY_PID=""

start_terminal_proxy() {
  log "Starting terminal proxy on port 7682..."
  python3 /opt/powerhausbox/terminal_proxy.py &
  TERMINAL_PROXY_PID=$!
}

stop_terminal_proxy() {
  if [ -n "${TERMINAL_PROXY_PID}" ] && kill -0 "${TERMINAL_PROXY_PID}" 2>/dev/null; then
    log "Stopping terminal proxy..."
    kill "${TERMINAL_PROXY_PID}"
    wait "${TERMINAL_PROXY_PID}" 2>/dev/null || true
  fi
  TERMINAL_PROXY_PID=""
}

# ---------------------------------------------------------------------------
# Process management: nginx (reverse proxy on port 8099)
# ---------------------------------------------------------------------------

NGINX_PID=""

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

# ---------------------------------------------------------------------------
# Iframe configuration (deferred to first pairing, not on startup)
# ---------------------------------------------------------------------------
# iframe_configurator.py is no longer run automatically on boot.
# It runs during the pairing flow (inside _apply_all_config) when the
# user explicitly initiates pairing from the UI.

# ---------------------------------------------------------------------------
# Process management: Flask web UI
# ---------------------------------------------------------------------------

WEB_PID=""
WEB_FAILURE_COUNT=0
WEB_HEALTHCHECK_FAILURES=0
WEB_FAILURE_WINDOW_STARTED_AT=0
WEB_HEALTHCHECK_URL="http://127.0.0.1:${WEB_PORT}/_powerhausbox/api/livez"
CLOUDFLARED_FAILURE_COUNT=0
CLOUDFLARED_FAILURE_WINDOW_STARTED_AT=0
SSHD_FAILURE_COUNT=0
TTYD_FAILURE_COUNT=0
TERMINAL_PROXY_FAILURE_COUNT=0
NGINX_FAILURE_COUNT=0
SERVICE_FAILURE_WINDOW_STARTED_AT=0
SERVICE_MAX_FAILURES=5

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

stop_cloudflared() {
  if [ -n "${CLOUDFLARED_PID}" ] && kill -0 "${CLOUDFLARED_PID}" 2>/dev/null; then
    log "Stopping cloudflared tunnel process..."
    kill "${CLOUDFLARED_PID}"
    wait "${CLOUDFLARED_PID}" 2>/dev/null || true
  fi
  CLOUDFLARED_PID=""
}

# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

cleanup() {
  stop_nginx
  stop_cloudflared
  stop_web_server
  stop_terminal_proxy
  stop_sshd
  stop_ttyd
}

trap cleanup EXIT INT TERM HUP QUIT

# ---------------------------------------------------------------------------
# Startup sequence
# ---------------------------------------------------------------------------

# Clean stale pairing sync flag from previous run
rm -f /data/.pairing_sync_done

# Crash recovery: ensure HA Core is running (may have been left stopped
# if the add-on crashed during a run_with_core_stopped operation)
if [ -n "${SUPERVISOR_TOKEN}" ]; then
  core_state="$(supervisor_api GET "/core/info" 2>/dev/null | jq -r '.data.state // empty' 2>/dev/null || true)"
  if [ "${core_state}" = "stopped" ] || [ "${core_state}" = "error" ]; then
    log "Home Assistant Core is ${core_state} — attempting recovery start..."
    supervisor_api POST "/core/start" >/dev/null 2>&1 || true
    log "Sent /core/start to Supervisor."
  fi
fi

if [ "$(use_external_ssh_addon)" = "true" ]; then
  log "External SSH add-on mode is enabled; skipping built-in SSH and terminal stack."
else
  # Initialize SSH (host keys, user, sshd config, ttyd credential)
  # Non-fatal: SSH failure should not block tunnel/web startup
  if ! init_ssh; then
    log "SSH initialization failed; SSH and terminal will not be available."
  fi
fi

# Install companion HA integration (backup agent)
install_integration || log "Integration install failed; backups may not work."

# Detect cloudflared capabilities
detect_cloudflared_auth_mode

# Start all services (SSH/ttyd only if built-in SSH mode is active)
if [ "$(use_external_ssh_addon)" = "true" ]; then
  log "Managed external SSH backend is enabled; not starting built-in SSH, ttyd, or terminal proxy."
elif [ -f "/data/ssh_host_ed25519_key" ]; then
  start_sshd
  start_ttyd
  start_terminal_proxy
else
  log "Skipping SSH/terminal services (init_ssh did not complete)."
fi
start_web_server
start_nginx

log "Waiting for pairing credentials from ingress UI..."
while true; do
  NEW_TOKEN_FINGERPRINT="$(token_fingerprint)"
  if [ "${NEW_TOKEN_FINGERPRINT}" != "${ACTIVE_TOKEN_FINGERPRINT}" ]; then
    ACTIVE_TOKEN_FINGERPRINT="${NEW_TOKEN_FINGERPRINT}"
    stop_cloudflared
    if token_present; then
      # Check if pair_status() already synced everything
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

  # Reset failure window every 120 seconds
  _now="$(date +%s)"
  if [ "${SERVICE_FAILURE_WINDOW_STARTED_AT}" -eq 0 ] || [ $(( _now - SERVICE_FAILURE_WINDOW_STARTED_AT )) -gt 120 ]; then
    SERVICE_FAILURE_WINDOW_STARTED_AT="${_now}"
    SSHD_FAILURE_COUNT=0
    TTYD_FAILURE_COUNT=0
    TERMINAL_PROXY_FAILURE_COUNT=0
    NGINX_FAILURE_COUNT=0
  fi

  # Restart sshd if it died (only if it was started)
  if [ -n "${SSHD_PID}" ] && ! kill -0 "${SSHD_PID}" 2>/dev/null; then
    SSHD_FAILURE_COUNT=$((SSHD_FAILURE_COUNT + 1))
    if [ "${SSHD_FAILURE_COUNT}" -ge "${SERVICE_MAX_FAILURES}" ]; then
      log "SSH daemon failed ${SSHD_FAILURE_COUNT} times within 120s; giving up."
      SSHD_PID=""
    else
      log "SSH daemon died unexpectedly; restarting (attempt ${SSHD_FAILURE_COUNT}/${SERVICE_MAX_FAILURES})..."
      start_sshd
    fi
  fi

  # Restart ttyd if it died (only if it was started)
  if [ -n "${TTYD_PID}" ] && ! kill -0 "${TTYD_PID}" 2>/dev/null; then
    TTYD_FAILURE_COUNT=$((TTYD_FAILURE_COUNT + 1))
    if [ "${TTYD_FAILURE_COUNT}" -ge "${SERVICE_MAX_FAILURES}" ]; then
      log "Web terminal failed ${TTYD_FAILURE_COUNT} times within 120s; giving up."
      TTYD_PID=""
    else
      log "Web terminal died unexpectedly; restarting (attempt ${TTYD_FAILURE_COUNT}/${SERVICE_MAX_FAILURES})..."
      start_ttyd
    fi
  fi

  # Restart terminal proxy if it died (only if it was started)
  if [ -n "${TERMINAL_PROXY_PID}" ] && ! kill -0 "${TERMINAL_PROXY_PID}" 2>/dev/null; then
    TERMINAL_PROXY_FAILURE_COUNT=$((TERMINAL_PROXY_FAILURE_COUNT + 1))
    if [ "${TERMINAL_PROXY_FAILURE_COUNT}" -ge "${SERVICE_MAX_FAILURES}" ]; then
      log "Terminal proxy failed ${TERMINAL_PROXY_FAILURE_COUNT} times within 120s; giving up."
      TERMINAL_PROXY_PID=""
    else
      log "Terminal proxy died unexpectedly; restarting (attempt ${TERMINAL_PROXY_FAILURE_COUNT}/${SERVICE_MAX_FAILURES})..."
      start_terminal_proxy
    fi
  fi

  # Restart nginx if it died
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
