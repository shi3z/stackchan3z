#!/bin/zsh
# Render the launchd templates for this Mac and (re)start the services.
#   BOARD_IP=192.168.1.50 ./install_services.sh                # forward + tts (macOS say) + brain (local Ollama)
# Optional env: TTS_API (Tsukasa-Speech URL -> tsukasa backend), VLM_URL, VLM_MODEL, PY_ML (python with insightface),
#               OLLAMA_SSH_HOST (a host whose localhost-only Ollama gets tunnelled to 127.0.0.1:11435),
#               SERVICES="tts brain forward" (subset to install)
set -e
REPO="$(cd "$(dirname "$0")/.." && pwd)"
BOARD_IP="${BOARD_IP:?set BOARD_IP=<stack-chan LAN ip>}"
PY="${PY:-/usr/bin/python3}"
PY_ML="${PY_ML:-$(command -v python3)}"
TTS_API="${TTS_API:-}"; TTS_BACKEND="${TTS_BACKEND:-$([ -n "$TTS_API" ] && echo tsukasa || echo say)}"
VLM_URL="${VLM_URL:-http://127.0.0.1:11434/api/chat}"; VLM_MODEL="${VLM_MODEL:-qwen2.5vl:7b}"
TTS_PITCH="${TTS_PITCH:--4}"   # for the optional second voice (SERVICES="... tts2", port 9003)
SERVICES="${SERVICES:-tts brain forward}"; [ -n "$OLLAMA_SSH_HOST" ] && SERVICES="$SERVICES ollama-tunnel"
mkdir -p ~/Library/LaunchAgents
for s in ${=SERVICES}; do
  label="com.stackchan.$s"; dst=~/Library/LaunchAgents/$label.plist
  sed -e "s|__REPO__|$REPO|g" -e "s|__PY__|$PY|g" -e "s|__PY_ML__|$PY_ML|g" -e "s|__BOARD_IP__|$BOARD_IP|g" \
      -e "s|__TTS_API__|$TTS_API|g" -e "s|__TTS_BACKEND__|$TTS_BACKEND|g" -e "s|__VLM_URL__|$VLM_URL|g" -e "s|__VLM_MODEL__|$VLM_MODEL|g" \
      -e "s|__OLLAMA_SSH_HOST__|$OLLAMA_SSH_HOST|g" -e "s|__TTS_PITCH__|$TTS_PITCH|g" -e "s|<string>stackchan-$s</string>|<string>$label</string>|" -e "s|/tmp/stackchan-$s.log|/tmp/$label.log|g" \
      "$REPO/server/launchd/stackchan-$s.plist" > "$dst"
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
  sleep 2   # give launchd time to tear the old instance down, otherwise bootstrap fails with an I/O error
  launchctl bootstrap "gui/$(id -u)" "$dst" && echo "started $label"
done
