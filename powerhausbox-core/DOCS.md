# PowerHausBox Core

Connects Home Assistant to PowerHaus Studio for pairing, remote access, cloud backups, diagnostics, and auth sync.

## Features

- **Cloudflare Tunnel** — Secure remote access to your Home Assistant instance via a Studio-managed Cloudflare tunnel.
- **Cloud Backups** — Back up and restore your Home Assistant through the native backup system, stored in PowerHaus Studio.
- **Studio Pairing** — Two-step pairing flow with 6-digit code and 2-digit verification for secure setup.
- **Auth Sync** — Periodic synchronization of Home Assistant user credentials to Studio.
- **URL Sync** — Automatic configuration of internal and external URLs for Home Assistant.
- **Diagnostics** — Ingress pages for current state, apply status, rollback status, and internal logs.

## Setup

1. Install this add-on and start it.
2. Open the add-on UI from the Home Assistant sidebar.
3. In PowerHaus Studio, generate a 6-digit pairing code.
4. Enter the code in the add-on UI and confirm the 2-digit verification code in Studio.
5. The add-on will automatically set up the Cloudflare tunnel and sync your configuration.

## Configuration

### General Options

| Option | Default | Description |
|--------|---------|-------------|
| `ui_auth_enabled` | `false` | Require password to access the add-on UI |
| `ui_password` | (set on first use) | Password for the add-on UI |
| `studio_base_url` | `https://studio.powerhaus.ai` | PowerHaus Studio API endpoint |
| `auto_enable_iframe_embedding` | `true` | Auto-configure Home Assistant for iframe embedding |

## Network Ports

| Port | Protocol | Description |
|------|----------|-------------|
| 8099 | TCP | Web UI (accessed via Home Assistant ingress) |

## Support

For issues and feature requests, visit the [PowerHausBox GitHub repository](https://github.com/gregor-autischer/PowerHausBox-Core/issues).
