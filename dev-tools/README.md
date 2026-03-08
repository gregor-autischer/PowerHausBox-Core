# Development Tools

`deploy_ha_addon.sh` automates local development deploys to Home Assistant:
- SSH connect
- Copy repository to HA add-on folder
- Reload add-on store
- Install add-on if missing
- Rebuild add-on image from local sources
- Apply options (`ui_auth_enabled`, `ui_password`, `studio_base_url`, `auto_enable_iframe_embedding`)
- Restart and verify add-on state
- Show final status summary

## Usage

1. Create a local env file (kept out of git):
   - `cp dev-tools/.env.example dev-tools/.env.local`
   - Edit `dev-tools/.env.local`
2. Run deploy:
   - `./dev-tools/deploy_ha_addon.sh --env-file dev-tools/.env.local`

Or run with one-shot environment variables:

```bash
HA_SSH_HOST=192.168.1.201 \
HA_SSH_USER=root \
HA_SSH_KEY_FILE="$HOME/.ssh/1Password/YOUR_KEY.pub" \
HA_SSH_AUTH_SOCK="$HOME/Library/Group Containers/2BUA8C4S2C.com.1password/t/agent.sock" \
./dev-tools/deploy_ha_addon.sh
```
