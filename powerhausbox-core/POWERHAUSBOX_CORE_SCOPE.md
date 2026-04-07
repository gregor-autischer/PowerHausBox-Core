# PowerHausBox Core Scope

## Purpose

PowerHausBox Core is the Home Assistant core add-on for PowerHaus Box.

It is responsible for:

- pairing a Home Assistant box with `https://studio.powerhaus.ai`
- receiving desired hostname and Home Assistant URLs from Studio
- applying those Home Assistant settings safely
- running the Studio-managed `cloudflared` tunnel so the box is reachable remotely
- sending heartbeat and state reports back to Studio
- syncing Home Assistant users and credentials to Studio
- exposing the PowerHaus backup integration so Studio-backed backups can appear in normal Home Assistant backup settings
- providing diagnostics and logs for the above

It is not responsible for built-in SSH, ttyd, or an embedded terminal.

SSH should be handled separately through the Home Assistant SSH add-on:

- `https://github.com/hassio-addons/app-ssh`

## Current Status

### Done

- Studio pairing flow is implemented.
- Pairing persists tunnel token, box API token, hostname, internal URL, and external URL.
- `cloudflared` startup and reconnect handling are implemented.
- Home Assistant hostname and URL apply logic is implemented.
- Home Assistant apply is transactional and rollback-aware if Core fails to come back.
- Iframe / proxy HTTP configuration is implemented with a safer text-based path.
- Diagnostics and internal log pages are implemented.
- Heartbeat and state reporting to Studio are implemented.
- Full Home Assistant auth sync to Studio is implemented.
- The custom PowerHaus backup integration is auto-installed into Home Assistant config.
- Built-in SSH, ttyd, terminal proxy, and SSH-related UI/config have been removed from this add-on.

### Partially Done / Needs Confirmation

- End-to-end pairing on a real Home Assistant box still needs to be re-verified after the SSH removal cleanup.
- The backup integration exists and is auto-installed, but it still needs explicit end-to-end confirmation that the PowerHaus backup target appears and works in normal Home Assistant backup settings without extra manual steps.
- The overall Studio product still contains legacy terminal assumptions outside this add-on. That follow-up belongs on the Studio side, not in Core.

### Still To Implement

- If needed, a smoother end-to-end onboarding for the Home Assistant backup agent after installation or update.
- Any Studio-side work required to stop assuming PowerHausBox Core exposes a terminal endpoint.
- Any explicit integration with the external SSH add-on, if later required. At the moment the intended direction is that Core does not own SSH functionality.

## Notes

- The add-on should stay focused on pairing, remote access, auth sync, backups, diagnostics, and safe Home Assistant configuration changes.
- SSH should remain out of scope for this add-on unless a future requirement makes a minimal integration unavoidable.
