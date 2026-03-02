# Changelog

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
