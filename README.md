# PowerHausBox Home Assistant Add-on Repository

[![Home Assistant Add-on](https://img.shields.io/badge/Home%20Assistant-Add--on-blue.svg)](https://www.home-assistant.io/hassio/)

Secure remote access, cloud backups, SSH, and a web terminal for Home Assistant — powered by PowerHaus Studio.

## Installation

1. Open your Home Assistant instance.
2. Navigate to **Settings** > **Add-ons** > **Add-on Store**.
3. Click the menu icon (three dots, top right) and select **Repositories**.
4. Add this repository URL:
   ```
   https://github.com/gregor-autischer/PowerHausBox-Core
   ```
5. Find **PowerHausBox Cloudflare Tunnel** in the add-on store and click **Install**.
6. Configure options, start the add-on, and complete pairing with Studio.

## Add-ons in this repository

### [PowerHausBox Cloudflare Tunnel](./powerhausbox-cloudflare-tunnel)

All-in-one Home Assistant connectivity add-on that pairs with PowerHaus Studio.

**Features:**
- Cloudflare Tunnel for secure remote access
- Cloud backups via Home Assistant's native backup system
- SSH access with public key authentication
- Web terminal with Studio token authentication
- Two-step pairing with 6-digit code
- Periodic auth sync to Studio
- Automatic URL and iframe configuration

## Development

See the [dev-tools/](./dev-tools) directory for deployment scripts.

```bash
cd dev-tools
./deploy_ha_addon.sh
```
