# PowerHausBox Cloudflare Tunnel Add-on

Home Assistant add-on that pairs with Studio API and runs `cloudflared`.

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

## Add-on options
- `ui_password`: password for ingress page login.
- `studio_base_url`: Studio base URL, must be HTTPS (for example `https://studio.powerhaus.ai`).

## Usage
1. Install this add-on from your local add-on repository.
2. Set `ui_password` and (optionally) `studio_base_url`.
3. Start the add-on and open the ingress page.
4. In Studio, generate a 6-digit code for the box.
5. Enter the 6-digit code in the add-on page.
6. Compare the displayed 2-digit verification code with Studio and click `Akzeptieren`.
7. The add-on polls until Studio returns `status=ready`, then saves credentials and restarts cloudflared automatically.

## Security notes
- HTTPS is enforced for Studio API calls.
- Pair code is one-time input and is not persisted.
- Session token is held in-memory only and never logged.
- Credential files in `/data` are written with restrictive permissions.
- Tunnel token is not passed as a plain CLI argument during startup.
- Re-pairing overwrites previous credentials.
