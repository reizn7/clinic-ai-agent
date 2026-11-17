#!/usr/bin/env bash
# Run the agent locally and expose it on a fixed public HTTPS URL via ngrok.
#
# Needs: `ngrok config add-authtoken <token>` done once, and NGROK_DOMAIN set
# (in .env or the shell) to your free static domain, e.g. foo-bar.ngrok-free.app.
# Ctrl+C stops both the app and the tunnel.
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ -z "${NGROK_DOMAIN:-}" && -f .env ]]; then
  NGROK_DOMAIN="$(grep -E '^NGROK_DOMAIN=' .env | tail -1 | cut -d= -f2- | tr -d '"'"'"' ')"
fi
if [[ -z "${NGROK_DOMAIN:-}" ]]; then
  echo "NGROK_DOMAIN is not set (add NGROK_DOMAIN=<your>.ngrok-free.app to .env)" >&2
  exit 1
fi
PORT="${PORT:-3000}"

uv run medi-desk-ai &
APP_PID=$!
trap 'kill "$APP_PID" 2>/dev/null || true' EXIT

echo "Webhook:   https://${NGROK_DOMAIN}/webhook"
echo "Call page: https://${NGROK_DOMAIN}/voice/call?token=<VOICE_WS_TOKEN>"
ngrok http --url="$NGROK_DOMAIN" "$PORT" --log=stdout --log-level=warn
