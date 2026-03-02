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
- Adds Home Assistant auth management capabilities:
  - Export all Home Assistant local usernames + hashes.
  - Create hidden internal service users (`system_generated=true`) from username + precomputed hash.
  - Create normal users from username + precomputed hash.
  - Reconcile a managed hidden service user if it was removed.

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

## Usage
1. Install this add-on from your local add-on repository.
2. Set `ui_password` and (optionally) `studio_base_url`.
3. Start the add-on and open the ingress page.
4. In Studio, generate a 6-digit code for the box.
5. Enter the 6-digit code in the add-on page.
6. Compare the displayed 2-digit verification code with Studio and click `Akzeptieren`.
7. The add-on polls until Studio returns `status=ready`, then saves credentials and restarts cloudflared automatically.
8. For auth tasks, use the "Home Assistant Auth Management" section in ingress UI:
   - Download usernames + hashes JSON export.
   - Create hidden service user (username + precomputed hash).
   - Ensure managed hidden service user exists.
   - Create normal user (username + precomputed hash).

## Security notes
- HTTPS is enforced for Studio API calls.
- Pair code is one-time input and is not persisted.
- Session token is held in-memory only and never logged.
- Credential files in `/data` are written with restrictive permissions.
- Tunnel token is not passed as a plain CLI argument during startup.
- Re-pairing overwrites previous credentials.
- Auth storage writes are performed while Core is stopped to avoid in-memory overwrite races.
