# Changelog

## 0.9.0
- Switched PowerHausBox to prefer a managed external SSH backend using the Home Assistant Community add-on `a0d7b954_ssh` instead of the custom embedded terminal path.
- Added add-on option `use_external_ssh_addon` and made it enabled by default.
- Added Supervisor-driven SSH backend install/configure/start actions in the UI and a dedicated SSH backend status page under `Terminal`.
- Reused configured local and Studio-synced authorized keys to configure the external SSH add-on automatically.
- Disabled the built-in SSH/ttyd/terminal-proxy runtime stack when external SSH backend mode is enabled.

## 0.8.1
- Fixed local add-on terminal session authentication so ttyd asset and WebSocket requests stay authorized inside the Home Assistant ingress UI instead of rendering a blank black terminal frame.

## 0.8.0
- Raised PowerHausBox Core to a host-capable SSH-style super add-on security model with `host_network`, `host_pid`, `host_uts`, `host_dbus`, `docker_api`, `full_access`, and AppArmor disabled.
- Added host-level helper commands `host-shell` and `ha-host` so the terminal can jump into the Home Assistant host namespaces and invoke the host `ha` CLI.
- Expanded the image with host-oriented troubleshooting tools such as `nsenter`, Docker CLI, networking utilities, and common shell tooling.
- Bound the internal Flask web process to `127.0.0.1` so moving to host networking does not expose the raw backend on the LAN.

## 0.7.4
- Added a new in-app `Terminal` page in the add-on UI that opens the existing ttyd shell directly inside Home Assistant ingress.
- Added short-lived locally issued terminal session tokens so the add-on UI can open the terminal without depending on a Studio-issued terminal token.
- Kept Studio terminal validation intact for remote use while allowing the local add-on UI to reuse the same terminal backend.
- Added regression tests for local terminal token issuance and validation.

## 0.7.3
- Split add-on liveness from operational health by adding `/_powerhausbox/api/livez` and pointing both the Supervisor watchdog and internal web watchdog to it, so degraded tunnel status no longer restarts the add-on web UI.
- Fixed the manual debug overview page so `Refresh from Studio` updates the displayed desired hostname and URL summary values immediately instead of leaving stale values on screen.
- Added regression coverage for the dedicated liveness endpoint.

## 0.7.2
- Added add-on option `debug_manual_apply_mode` to disable automatic Home Assistant config mutation after Studio pairing and config refresh.
- Added a manual debug apply panel in the add-on overview with one-click steps for Studio refresh, Core URLs, hostname, iframe/HTTP config, and SSH authorized keys.
- Surfaced per-step status, timestamp, error, details, and recent internal log output directly inside the add-on UI.
- Disabled automatic startup and background Home Assistant config apply paths while manual debug mode is enabled.
- Preserved `ssh` and `debug_manual_apply_mode` add-on options when settings are updated from inside the add-on UI.

## 0.7.1
- Made initial pairing config apply transactional: back up HA config files, roll back automatically if Core fails to come back, and fail closed when Core never stabilizes.
- Moved hostname sync out of the fragile pairing stop/start window.
- Refused unsafe automatic rewrites when `configuration.yaml` already contains a top-level `http:` block.
- Added in-app diagnostics and log pages to show desired state, applied state, rollback state, restored files, and internal add-on logs.
- Mirrored Python and `run.sh` log output into `/data/powerhausbox.log` while keeping stdout logging for the normal Home Assistant add-on log viewer.
- Added regression tests for transactional rollback behavior.

## 0.6.0
- Added SSH daemon with hardened configuration (public key only, strong ciphers).
- Added web terminal (ttyd) with Studio token-based authentication.
- Added cloud backup agent via Home Assistant's native backup system.
- Added companion HA integration auto-installer for backup agent registration.
- Added SSH authorized key sync from Studio during config sync.
- Added SSH configuration options: username, authorized_keys, sftp, tcp forwarding.
- Added process management for sshd and ttyd in run.sh main loop.
- Exposed port 22 for SSH access.
- Added volume maps for backup, media, share, and ssl directories.

## 0.5.13
- Refactored shared modules and added comprehensive tests.
- Base functionality fully implemented and tested.

## 0.4.1
- Removed hardcoded Home Assistant internal URL (`powerhaus.local`).
- Pairing now requires Studio `pair/complete` ready payload to include `internal_url`.
- Pairing and config sync now require Studio to include `external_url`; the add-on no longer derives it from `tunnel_hostname`.
- Persisted `internal_url` in add-on secrets storage and reused it for all URL sync operations.
- Persisted `external_url` in add-on secrets storage and reused it for all URL sync operations.
- Updated runtime URL sync in `run.sh` to load `internal_url` and `external_url` from stored pairing secrets.
- Updated ingress UI to show current internal URL from stored credentials.

## 0.4.0
- Added startup automation for iframe embedding config:
  - option `auto_enable_iframe_embedding` (default `true`)
  - enforces `http.use_x_frame_options: false` in `/config/configuration.yaml`.
- Added timestamped configuration backups before changes.
- Added Home Assistant config validation (`check_config`) before applying restart.
- Added automatic rollback to backup when validation fails.
- Added automatic Core restart after successful update and validation.
- Added rollback to backup when restart trigger fails, with explicit manual restart instruction in logs.
- Added explicit startup logs for:
  - `already configured`
  - `updated and restarted`
  - `failed and rolled back`
- Added unit tests for iframe configurator scenarios.

## 0.3.3
- Added periodic Studio auth sync background loop (default every 6 hours).
- Added env controls `PERIODIC_AUTH_SYNC_ENABLED` and `PERIODIC_AUTH_SYNC_INTERVAL_SECONDS`.
- Added ingress status display for periodic auth sync configuration.

## 0.3.2
- Added Studio auth sync integration: full HA username/hash snapshot push to `POST /api/addon/auth-sync/full/`.
- Added manual ingress action "Sync hashes to Studio now".
- Added automatic Studio auth sync after pairing readiness and after auth-user mutations.
- Added watchdog-triggered Studio auth sync when managed service user is recreated.
- Added pairing-ready response fields for auth sync status (`auth_synced`, `auth_sync_error`, `auth_synced_count`, `auth_sync_id`).

## 0.3.1
- Added automatic Home Assistant URL sync on successful Studio tunnel pairing:
  - internal URL sourced by add-on (superseded in `0.4.1` by Studio-provided `internal_url`)
  - external URL set from paired tunnel hostname.
- Added runtime URL sync in `run.sh` when tunnel token changes or cloudflared reconnects.
- Added manual "Sync Home Assistant URLs now" action in ingress UI.
- Added Home Assistant auth export endpoint and UI action to download all local usernames + hashes.
- Added creation of hidden internal service users from precomputed base64(bcrypt) hashes.
- Added creation of normal users from precomputed base64(bcrypt) hashes.
- Added managed hidden service-user reconciliation workflow.
- Added background watchdog to auto-recreate managed hidden service user if removed.
- Added safe auth storage mutation flow: stop Core, write `.storage` auth files, start Core.
- Enabled required add-on permissions (`hassio_api`, `homeassistant_api`, `auth_api`, `map: config:rw`).

## 0.2.1
- Hardened cloudflared startup to avoid passing tunnel token as a plain CLI argument.
- Added automatic auth mode detection: prefer `--token-file`, fallback to `TUNNEL_TOKEN` env mode.

## 0.2.0
- Added Studio API two-step pairing flow with 6-digit code input.
- Added polling of `/api/addon/pair/complete/` until approval.
- Added secure persistence of `cloudflare_tunnel_token`, `tunnel_hostname`, and `box_api_token` in `/data`.
- Added HTTPS enforcement for `studio_base_url`.
- Updated ingress dashboard to display 2-digit verification code and pairing state.

## 0.1.0
- Initial release.
- Added token-only Cloudflare tunnel setup.
- Added ingress UI with daisyUI login screen.
