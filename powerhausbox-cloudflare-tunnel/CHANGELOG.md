# Changelog

## 0.4.1
- Removed hardcoded Home Assistant internal URL (`powerhaus.local`).
- Pairing now requires Studio `pair/complete` ready payload to include `internal_url`.
- Persisted `internal_url` in add-on secrets storage and reused it for all URL sync operations.
- Updated runtime URL sync in `run.sh` to load `internal_url` from stored pairing secrets.
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
