# Log Streaming: Studio ↔ Add-on

## Overview

Enable viewing Home Assistant logs (Core, Add-on, Supervisor, Host) directly in PowerHaus Studio's device detail page. Admin-only feature.

## Architecture: Pull via Cloudflare Tunnel

Studio fetches logs on-demand from the add-on through the existing Cloudflare tunnel. No persistent storage needed.

### Add-on Side

New Flask endpoint (admin-authenticated via Studio token):

```
GET /_powerhausbox/api/logs/<source>?lines=200
```

Sources: `core`, `addon`, `supervisor`, `host`

Implementation: Proxy to Supervisor API endpoints:
- `GET /core/logs` → Core logs
- `GET /addons/self/logs` → This add-on's logs
- `GET /supervisor/logs` → Supervisor logs  
- `GET /host/logs` → Host/OS logs

Each returns plain text log output. The add-on endpoint validates a Studio-issued token (same pattern as terminal token validation) and proxies the request to the Supervisor API.

### Studio Side

**New section on device detail page** (admin only, below terminal):

```
┌─────────────────────────────────────────────┐
│ Logs                                   [▼ Core] │
│                                                  │
│ 2026-04-06 12:00:01 INFO (MainThread) [core]    │
│ 2026-04-06 12:00:02 WARNING (MainThread) ...    │
│ ...                                              │
│                                    [Aktualisieren] │
└──────────────────────────────────────────────┘
```

- Tabs or dropdown for log source (Core, Add-on, Supervisor, Host)
- Fetches via `https://<tunnel_hostname>/_powerhausbox/api/logs/<source>`
- Auto-scroll to bottom
- Manual refresh button + optional auto-refresh (HTMX polling every 10s)
- Monospace font, dark background (matching terminal theme)
- Last 200 lines by default, configurable

### Authentication

Studio generates a short-lived token (same as terminal tokens) and passes it as a query parameter. The add-on validates it against Studio before returning logs.

### Token endpoint (Studio):
```
POST /devices/<pk>/logs-token/  → {"token": "...", "logs_url": "https://<tunnel>/_powerhausbox/api/logs/"}
```

### Security

- Admin-only in Studio UI
- Token-authenticated on the add-on side
- Logs never stored in Studio (fetched on demand, displayed in browser)
- Token expires after 5 minutes (same as terminal tokens)

## Future Enhancement: Push Approach

For historical debugging (when tunnel is disconnected):

- Add-on periodically pushes log snippets to `POST /api/addon/logs/push/`
- Studio stores in `PowerhouseBoxLogEntry` model with retention (e.g., 7 days)
- Viewable even when box is offline
- Structured: source, timestamp, level, message

This is more complex and can be implemented after the pull approach is validated.

## Implementation Steps

1. Add-on: Create `/_powerhausbox/api/logs/<source>` endpoint with token auth
2. Studio: Add logs token generation endpoint
3. Studio: Add logs section to device detail template (admin only)
4. Studio: JavaScript to fetch and display logs with source selector
