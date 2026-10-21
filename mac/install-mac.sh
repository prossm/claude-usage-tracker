#!/usr/bin/env bash
# Install the Claude Code token pusher on a Mac: copies the app to ~/.claude-usage
# and adds a launchd job that runs `cc-push` every 30 minutes. Sends token counts
# only; no conversation content leaves the Mac. Idempotent; never writes secrets.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOME_DIR="$HOME/.claude-usage"
LABEL="com.claude-usage.cc-push"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

PY=""
for c in python3.13 python3.12 python3.11 python3.10 python3 /usr/bin/python3; do
    p=$(command -v "$c" 2>/dev/null || true)
    if [[ -n "$p" ]] && "$p" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
        PY=$(cd "$(dirname "$p")" && pwd)/$(basename "$p"); break
    fi
done
[[ -n "$PY" ]] || { echo "Python 3.10+ is required" >&2; exit 1; }

mkdir -p "$HOME_DIR" "$HOME/Library/LaunchAgents"
chmod 700 "$HOME_DIR"
rm -rf "$HOME_DIR/app"
mkdir -p "$HOME_DIR/app/cusage"
cp "$SRC"/cusage/*.py "$HOME_DIR/app/cusage/"
cp "$SRC/claude_usage.py" "$HOME_DIR/app/"

if [[ ! -f "$HOME_DIR/.env" ]]; then
    cp "$SRC/mac/env.mac.example" "$HOME_DIR/.env"
    echo "Created $HOME_DIR/.env; fill in CC_PUSH_URL and CC_PUSH_TOKEN, then re-run this script."
fi
chmod 600 "$HOME_DIR/.env"

cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PY</string>
    <string>$HOME_DIR/app/claude_usage.py</string>
    <string>--env-file</string><string>$HOME_DIR/.env</string>
    <string>cc-push</string>
  </array>
  <key>StartInterval</key><integer>1800</integer>
  <key>RunAtLoad</key><true/>
  <key>LowPriorityIO</key><true/>
  <key>Nice</key><integer>10</integer>
  <key>StandardOutPath</key><string>$HOME_DIR/push.log</string>
  <key>StandardErrorPath</key><string>$HOME_DIR/push.log</string>
</dict>
</plist>
PLIST

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
if grep -qE '^CC_PUSH_TOKEN=.+' "$HOME_DIR/.env" && grep -qE '^CC_PUSH_URL=.+' "$HOME_DIR/.env"; then
    echo "Sending full history once..."
    "$PY" "$HOME_DIR/app/claude_usage.py" --env-file "$HOME_DIR/.env" cc-push --all
    launchctl bootstrap "gui/$(id -u)" "$PLIST"
    echo "Installed. Pushes every 30 min; log: $HOME_DIR/push.log"
else
    echo "launchd job not loaded yet: set CC_PUSH_URL and CC_PUSH_TOKEN in $HOME_DIR/.env, then re-run."
fi
