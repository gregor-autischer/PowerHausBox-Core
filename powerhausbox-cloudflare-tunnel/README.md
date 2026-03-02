# PowerHausBox Cloudflare Tunnel Add-on

Home Assistant add-on that pairs with Studio API, manages Home Assistant auth storage, and runs `cloudflared`.

## What this add-on does
- Exposes an ingress web UI protected by `ui_password`.
- Uses a two-step pairing flow with Studio:
  1. User enters one-time 6-digit pairing code.
  2. Add-on calls `POST /api/addon/pair/init/`.
  3. Add-on shows Studio verification 2-digit code.
  4. Add-on polls `POST /api/addon/pair/complete/` until ready.
- Persists returned credentials in add-on storage:
  - `cloudflare_tunnel_token`
  - `tunnel_hostname`
  - `box_api_token`
- Starts Cloudflare tunnel with:
  - `cloudflared tunnel --no-autoupdate run --token-file /data/tunnel_token`
  - Fallback: `TUNNEL_TOKEN=<token> cloudflared tunnel --no-autoupdate run`
- Automatically updates Home Assistant core URLs after successful pairing and on tunnel runtime reconnect:
  - `internal_url` is always set to `http://powerhaus.local:8123`
  - `external_url` is set to `https://<tunnel_hostname>` from Studio pairing response
- Adds Home Assistant auth management capabilities:
  - Export all Home Assistant local usernames + hashes.
  - Create hidden internal service users (`system_generated=true`) from username + precomputed hash.
  - Create normal users from username + precomputed hash.
  - Reconcile a managed hidden service user if it was removed.
  - Sync full username/hash snapshot to Studio via `POST /api/addon/auth-sync/full/`.
  - Periodically re-sync full username/hash snapshot to Studio (default every 6 hours).

## Add-on options
- `ui_password`: password for ingress page login.
- `studio_base_url`: Studio base URL, must be HTTPS (for example `https://studio.powerhaus.ai`).

## Home Assistant auth architecture
- Storage files used:
  - `/config/.storage/auth`
  - `/config/.storage/auth_provider.homeassistant`
- Hash format expected for user creation:
  - Home Assistant storage format `base64(bcrypt_hash)`
- Write model:
  - Add-on stops Home Assistant Core via Supervisor API.
  - Mutates `.storage` auth files atomically.
  - Starts Home Assistant Core again.
- Internal hidden service user behavior:
  - Created as `system_generated=true` and `local_only=true`.
  - Hidden in normal HA frontend user list.
  - Stored in add-on-managed config (`/data/managed_service_user.json`) for reconcile/restore.
  - Watchdog loop can auto-recreate it if removed.

## Required add-on permissions
- `map: config:rw` for `.storage` read/write access.
- `hassio_api: true` to stop/start Home Assistant Core safely.
- `homeassistant_api: true` and `auth_api: true` enabled for HA integration scope.

## Optional watchdog env controls
- `SERVICE_USER_WATCHDOG_ENABLED` (default `true`)
- `SERVICE_USER_WATCHDOG_INTERVAL_SECONDS` (default `300`, minimum `60`)
- `PERIODIC_AUTH_SYNC_ENABLED` (default `true`)
- `PERIODIC_AUTH_SYNC_INTERVAL_SECONDS` (default `21600` = 6h, minimum `300`)

## Usage
1. Install this add-on from your local add-on repository.
2. Set `ui_password` and (optionally) `studio_base_url`.
3. Start the add-on and open the ingress page.
4. In Studio, generate a 6-digit code for the box.
5. Enter the 6-digit code in the add-on page.
6. Compare the displayed 2-digit verification code with Studio and click `Akzeptieren`.
7. The add-on polls until Studio returns `status=ready`, then saves credentials and restarts cloudflared automatically.
8. After pairing is ready, the add-on auto-updates Home Assistant `internal_url` and `external_url` through Supervisor API.
9. For auth tasks, use the "Home Assistant Auth Management" section in ingress UI:
   - Download usernames + hashes JSON export.
   - Create hidden service user (username + precomputed hash).
   - Ensure managed hidden service user exists.
   - Create normal user (username + precomputed hash).
   - Manually trigger "Sync hashes to Studio now".

## Security notes
- HTTPS is enforced for Studio API calls.
- Pair code is one-time input and is not persisted.
- Session token is held in-memory only and never logged.
- Credential files in `/data` are written with restrictive permissions.
- Tunnel token is not passed as a plain CLI argument during startup.
- Re-pairing overwrites previous credentials.
- Auth storage writes are performed while Core is stopped to avoid in-memory overwrite races.
- URL sync can be retriggered from ingress using "Sync Home Assistant URLs now".
- Auth sync to Studio is attempted automatically after pairing readiness, after add-on auth-user mutations, and periodically (default every 6 hours).

## Studio auth sync API contract used by add-on
- Endpoint: `POST {studio_base_url}/api/addon/auth-sync/full/`
- Authentication: `Authorization: Bearer <box_api_token>`
- Payload:
  - `synced_at`: UTC timestamp
  - `source`: `"home_assistant_addon"`
  - `addon_version`
  - `replace_all`: `true`
  - `users`: full snapshot of local HA users with `username`, `password_hash`, ids and flags
- Expected success response:
  - HTTP `200` and `status` in `ok | accepted | queued`
  - Optional: `received_count`, `sync_id`
