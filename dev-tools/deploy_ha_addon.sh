#!/usr/bin/env bash
set -euo pipefail

print_help() {
  cat <<'EOF'
Deploy PowerHausBox add-on to Home Assistant via SSH.

Usage:
  ./dev-tools/deploy_ha_addon.sh [--env-file <path>]

Options:
  --env-file <path>   Load environment variables from this file.
  -h, --help          Show this help.

Required environment:
  HA_SSH_HOST         Home Assistant host or IP (example: 192.168.1.201).

Optional environment:
  HA_SSH_PORT                         Default: 22
  HA_SSH_USER                         Default: root
  HA_SSH_KEY_FILE                     SSH key/public-key file used with -i
  HA_SSH_AUTH_SOCK                    SSH agent socket (for example 1Password agent)
  HA_REMOTE_REPO_PATH                 Default: /addons/local/powerhausbox
  HA_ADDON_SLUG                       Default: local_powerhausbox_cloudflare_tunnel
  PHB_STUDIO_BASE_URL                 Default: https://studio.powerhaus.ai
  PHB_AUTO_ENABLE_IFRAME_EMBEDDING    Default: true
  PHB_UI_AUTH_ENABLED                 Default: false
  PHB_UI_PASSWORD                     If empty, a random password is generated.
EOF
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

ENV_FILE=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --env-file)
      [[ $# -ge 2 ]] || { echo "Missing value for --env-file" >&2; exit 1; }
      ENV_FILE="$2"
      shift 2
      ;;
    -h|--help)
      print_help
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      print_help >&2
      exit 1
      ;;
  esac
done

if [[ -n "${ENV_FILE}" ]]; then
  if [[ ! -f "${ENV_FILE}" ]]; then
    echo "Env file not found: ${ENV_FILE}" >&2
    exit 1
  fi
  set -a
  # shellcheck disable=SC1090
  source "${ENV_FILE}"
  set +a
fi

for required_cmd in ssh tar curl base64; do
  command -v "${required_cmd}" >/dev/null 2>&1 || {
    echo "Missing required command: ${required_cmd}" >&2
    exit 1
  }
done

HA_SSH_HOST="${HA_SSH_HOST:-}"
HA_SSH_PORT="${HA_SSH_PORT:-22}"
HA_SSH_USER="${HA_SSH_USER:-root}"
HA_SSH_KEY_FILE="${HA_SSH_KEY_FILE:-}"
HA_SSH_AUTH_SOCK="${HA_SSH_AUTH_SOCK:-${SSH_AUTH_SOCK:-}}"
HA_REMOTE_REPO_PATH="${HA_REMOTE_REPO_PATH:-/addons/local/powerhausbox}"
HA_ADDON_SLUG="${HA_ADDON_SLUG:-local_powerhausbox_cloudflare_tunnel}"
PHB_STUDIO_BASE_URL="${PHB_STUDIO_BASE_URL:-https://studio.powerhaus.ai}"
PHB_AUTO_ENABLE_IFRAME_EMBEDDING="${PHB_AUTO_ENABLE_IFRAME_EMBEDDING:-true}"
PHB_UI_AUTH_ENABLED="${PHB_UI_AUTH_ENABLED:-false}"
PHB_UI_PASSWORD="${PHB_UI_PASSWORD:-}"

if [[ -z "${HA_SSH_HOST}" ]]; then
  echo "HA_SSH_HOST is required." >&2
  exit 1
fi

normalize_bool() {
  local raw
  raw="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')"
  case "${raw}" in
    true|1|yes|on) echo "true" ;;
    false|0|no|off) echo "false" ;;
    *) return 1 ;;
  esac
}

if ! PHB_AUTO_ENABLE_IFRAME_EMBEDDING="$(normalize_bool "${PHB_AUTO_ENABLE_IFRAME_EMBEDDING}")"; then
  echo "PHB_AUTO_ENABLE_IFRAME_EMBEDDING must be true/false (or yes/no, 1/0)." >&2
  exit 1
fi

if ! PHB_UI_AUTH_ENABLED="$(normalize_bool "${PHB_UI_AUTH_ENABLED}")"; then
  echo "PHB_UI_AUTH_ENABLED must be true/false (or yes/no, 1/0)." >&2
  exit 1
fi

GENERATED_PASSWORD=false
if [[ -z "${PHB_UI_PASSWORD}" ]]; then
  if [[ "${PHB_UI_AUTH_ENABLED}" == "true" ]]; then
    if command -v openssl >/dev/null 2>&1; then
      PHB_UI_PASSWORD="$(openssl rand -hex 12)"
    else
      PHB_UI_PASSWORD="$(LC_ALL=C dd if=/dev/urandom bs=48 count=1 2>/dev/null | base64 | tr -dc 'A-Za-z0-9' | cut -c 1-24)"
    fi
    GENERATED_PASSWORD=true
  else
    PHB_UI_PASSWORD="change-this-password"
  fi
fi

SSH_TARGET="${HA_SSH_USER}@${HA_SSH_HOST}"
SSH_OPTS=(
  -o BatchMode=yes
  -o StrictHostKeyChecking=accept-new
  -o ConnectTimeout=12
  -o IdentitiesOnly=yes
  -p "${HA_SSH_PORT}"
)
if [[ -n "${HA_SSH_KEY_FILE}" ]]; then
  SSH_OPTS+=(-i "${HA_SSH_KEY_FILE}")
fi

ssh_exec() {
  local remote_cmd="$1"
  if [[ -n "${HA_SSH_AUTH_SOCK}" ]]; then
    SSH_AUTH_SOCK="${HA_SSH_AUTH_SOCK}" ssh "${SSH_OPTS[@]}" "${SSH_TARGET}" "${remote_cmd}"
  else
    ssh "${SSH_OPTS[@]}" "${SSH_TARGET}" "${remote_cmd}"
  fi
}

ssh_with_stdin() {
  local remote_cmd="$1"
  if [[ -n "${HA_SSH_AUTH_SOCK}" ]]; then
    SSH_AUTH_SOCK="${HA_SSH_AUTH_SOCK}" ssh "${SSH_OPTS[@]}" "${SSH_TARGET}" "${remote_cmd}"
  else
    ssh "${SSH_OPTS[@]}" "${SSH_TARGET}" "${remote_cmd}"
  fi
}

json_escape() {
  local value="$1"
  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  value="${value//$'\n'/\\n}"
  value="${value//$'\r'/\\r}"
  value="${value//$'\t'/\\t}"
  printf '%s' "${value}"
}

render_bar() {
  local percent="$1"
  local width=30
  local filled=$((percent * width / 100))
  local bar=""
  local i
  for ((i = 0; i < filled; i++)); do
    bar+="#"
  done
  for ((i = filled; i < width; i++)); do
    bar+="-"
  done
  printf '%s' "${bar}"
}

TOTAL_STEPS=10
CURRENT_STEP=0

step() {
  local message="$1"
  CURRENT_STEP=$((CURRENT_STEP + 1))
  local percent=$((CURRENT_STEP * 100 / TOTAL_STEPS))
  printf '\n[%3d%%] [%s] %s\n' "${percent}" "$(render_bar "${percent}")" "${message}"
}

step "Checking SSH connectivity to ${SSH_TARGET}:${HA_SSH_PORT}"
ssh_exec "echo connected >/dev/null"

step "Preparing remote repository path (${HA_REMOTE_REPO_PATH})"
ssh_exec "mkdir -p '${HA_REMOTE_REPO_PATH}' && rm -rf '${HA_REMOTE_REPO_PATH}'/*"

step "Copying repository files to Home Assistant"
tar \
  --exclude='.git' \
  --exclude='.DS_Store' \
  --exclude='._*' \
  --exclude='*.pyc' \
  -czf - \
  -C "${REPO_ROOT}" \
  . | ssh_with_stdin "tar -xzf - -C '${HA_REMOTE_REPO_PATH}'"

step "Validating copied repository layout"
ssh_exec "find '${HA_REMOTE_REPO_PATH}' -name '._*' -type f -delete; test -f '${HA_REMOTE_REPO_PATH}/repository.yaml'; test -f '${HA_REMOTE_REPO_PATH}/powerhausbox-cloudflare-tunnel/config.yaml'"

step "Reloading Home Assistant add-on store"
ssh_exec "ha store reload >/dev/null"

step "Installing add-on if missing (${HA_ADDON_SLUG})"
if ssh_exec "ha apps info '${HA_ADDON_SLUG}' >/dev/null 2>&1"; then
  echo "Add-on already installed. Continuing with update/restart flow."
else
  ssh_exec "ha apps install '${HA_ADDON_SLUG}' >/dev/null"
fi

step "Rebuilding add-on image from local sources"
if ! ssh_exec "ha apps rebuild '${HA_ADDON_SLUG}' >/dev/null 2>&1"; then
  ssh_exec "ha apps update '${HA_ADDON_SLUG}' >/dev/null"
fi

step "Applying add-on options"
ESCAPED_PASSWORD="$(json_escape "${PHB_UI_PASSWORD}")"
ESCAPED_STUDIO_BASE_URL="$(json_escape "${PHB_STUDIO_BASE_URL}")"
OPTIONS_PAYLOAD="{\"options\":{\"ui_auth_enabled\":${PHB_UI_AUTH_ENABLED},\"ui_password\":\"${ESCAPED_PASSWORD}\",\"studio_base_url\":\"${ESCAPED_STUDIO_BASE_URL}\",\"auto_enable_iframe_embedding\":${PHB_AUTO_ENABLE_IFRAME_EMBEDDING}}}"
OPTIONS_PAYLOAD_B64="$(printf '%s' "${OPTIONS_PAYLOAD}" | base64 | tr -d '\n')"
ssh_exec "PAYLOAD=\$(printf '%s' '${OPTIONS_PAYLOAD_B64}' | base64 -d); curl -fsS -X POST -H \"Authorization: Bearer \$SUPERVISOR_TOKEN\" -H \"Content-Type: application/json\" -d \"\$PAYLOAD\" \"http://supervisor/addons/${HA_ADDON_SLUG}/options\" >/dev/null"

step "Restarting add-on"
if ! ssh_exec "ha apps restart '${HA_ADDON_SLUG}' >/dev/null"; then
  ssh_exec "ha apps start '${HA_ADDON_SLUG}' >/dev/null"
fi

step "Waiting for add-on to report state=started"
APP_STATE=""
for _attempt in {1..30}; do
  APP_STATE="$(ssh_exec "ha apps info '${HA_ADDON_SLUG}' | sed -n 's/^state: //p' | head -n1")"
  if [[ "${APP_STATE}" == "started" ]]; then
    break
  fi
  sleep 2
done

if [[ "${APP_STATE}" != "started" ]]; then
  echo
  echo "Deployment failed: add-on did not reach state=started." >&2
  echo "Recent logs:" >&2
  ssh_exec "ha apps logs '${HA_ADDON_SLUG}' --lines 80" >&2 || true
  exit 1
fi

INGRESS_URL="$(ssh_exec "ha apps info '${HA_ADDON_SLUG}' | sed -n 's/^ingress_url: //p' | head -n1")"

echo
echo "Deployment finished successfully."
echo "Host: ${HA_SSH_HOST}"
echo "Add-on slug: ${HA_ADDON_SLUG}"
echo "Add-on state: ${APP_STATE}"
echo "Ingress path: ${INGRESS_URL}"
echo "Studio base URL: ${PHB_STUDIO_BASE_URL}"
echo "Auto iframe embedding: ${PHB_AUTO_ENABLE_IFRAME_EMBEDDING}"
echo "UI auth enabled: ${PHB_UI_AUTH_ENABLED}"
echo "UI password: ${PHB_UI_PASSWORD}"
if [[ "${GENERATED_PASSWORD}" == "true" ]]; then
  echo "Note: UI password was generated for this run."
fi
