#!/bin/sh
# Install the HDHR facade as a root LaunchDaemon (survives reboot). macOS only.
# Run as: sudo ops/install-facade-launchd.sh
set -e
SELF=$(cd "$(dirname "$0")" && pwd)
if [ -f "$SELF/hdhr_emulator.py" ]; then HERE=$SELF; else HERE=$(cd "$SELF/.." && pwd); fi
PY=${PYTHON:-}
[ -z "$PY" ] && [ -x "$HERE/venv/bin/python" ] && PY="$HERE/venv/bin/python"
[ -z "$PY" ] && PY=$(command -v python3)
sleep 1
launchctl unload -w /Library/LaunchDaemons/com.zapperbridge.facade.plist 2>/dev/null || true
TPL=$HERE/ops/com.zapperbridge.facade.plist
[ -f "$TPL" ] || TPL=$HERE/com.zapperbridge.facade.plist
sed -e "s|@INSTALL_DIR@|$HERE|g" -e "s|@PYTHON@|$PY|g" \
  "$TPL" > /Library/LaunchDaemons/com.zapperbridge.facade.plist
chown root:wheel /Library/LaunchDaemons/com.zapperbridge.facade.plist
chmod 644 /Library/LaunchDaemons/com.zapperbridge.facade.plist
launchctl load -w /Library/LaunchDaemons/com.zapperbridge.facade.plist
sleep 3
curl -s -m 5 http://127.0.0.1:80/lineup.json | head -c 120
