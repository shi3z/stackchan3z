#!/bin/zsh
# Usage: ./provision_wifi.sh <SSID>     (password is read from the macOS keychain)
# Pulls the Wi-Fi password from the macOS keychain (a permission dialog appears; click "Allow")
# and sends "wifi <ssid> <password>" to the M5Stack over USB serial. The password is never printed.
set -e
SSID="${1:?usage: provision_wifi.sh <SSID>}"
PY="${PY:-python3}"   # needs pyserial (pip install pyserial)
cd "$(dirname "$0")"
PASS="$(security find-generic-password -wa "$SSID")" || { echo "keychain lookup failed for $SSID"; exit 1; }
echo "sending credentials for '$SSID' to the board ..."
"$PY" serial_cmd.py -t 25 "wifi $SSID $PASS" | tr -s '.' | grep -vE '^\.?$' | sed "s/$PASS/********/g"
