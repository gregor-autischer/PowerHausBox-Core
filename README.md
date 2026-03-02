# PowerHausBox Core

Home Assistant add-on repository for PowerHaus Box connectivity.

This repository currently contains one add-on:
- `powerhausbox-cloudflare-tunnel`: Studio-paired Cloudflare Tunnel add-on with auth sync tooling.

## What This Add-on Provides
- Two-step pairing with Studio (`pair/init` + `pair/complete`).
- Secure storage of returned tunnel and box credentials in `/data`.
- Automatic `cloudflared` startup and restart on credential changes.
- Home Assistant URL sync:
  - `internal_url = <from Studio pairing response>`
  - `external_url = https://<tunnel_hostname>`
- Optional startup automation for iframe embedding:
  - sets `http.use_x_frame_options: false` in `/config/configuration.yaml`
  - creates backup, validates config, rolls back on failure, restarts Core on success
- Home Assistant auth management:
  - export usernames + password hashes
  - create hidden service users with precomputed hash
  - create normal users with precomputed hash
  - periodic auth sync to Studio (default every 6 hours)

## Requirements
- Home Assistant installation with Supervisor (Home Assistant OS or Supervised).
- Access to add custom add-on repositories.
- Network access from Home Assistant to your Studio endpoint (HTTPS).

## Install In Home Assistant
1. Open Home Assistant.
2. Go to `Settings -> Add-ons -> Add-on Store`.
3. Open the menu (`⋮`) and select `Repositories`.
4. Add:
   - `https://github.com/gregor-autischer/PowerHausBox-Core`
5. Reload the Add-on Store.
6. Open `PowerHausBox Cloudflare Tunnel` and click `Install`.
7. Configure options, then `Start`.
8. Open the add-on ingress UI and complete pairing with Studio.

## Default Add-on Options
- `ui_password`: ingress UI password.
- `studio_base_url`: Studio API base URL (must be HTTPS).
- `auto_enable_iframe_embedding`: default `true`.

## Repository Structure
- `/repository.yaml`: Home Assistant add-on repository metadata.
- `/powerhausbox-cloudflare-tunnel/config.yaml`: add-on manifest.
- `/powerhausbox-cloudflare-tunnel/run.sh`: startup/runtime supervisor.
- `/powerhausbox-cloudflare-tunnel/rootfs/opt/powerhausbox/server.py`: ingress/API service.
- `/powerhausbox-cloudflare-tunnel/rootfs/opt/powerhausbox/iframe_configurator.py`: iframe config automation.

## Development Notes
- Add-on docs and API behavior are in:
  - `/powerhausbox-cloudflare-tunnel/README.md`
- Changelog is in:
  - `/powerhausbox-cloudflare-tunnel/CHANGELOG.md`
