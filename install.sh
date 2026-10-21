#!/usr/bin/env bash
# Install or upgrade the Claude usage tracker on a Raspberry Pi (Raspberry Pi OS 64-bit).
# Idempotent. Creates the service user, /opt/claude-usage, the venv, and systemd units.
# Never writes secrets: .env is created from .env.example (empty values) only if absent.
set -euo pipefail

PREFIX=/opt/claude-usage
SVC_USER=claudeusage
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_DIR=/etc/systemd/system

if [[ $EUID -ne 0 ]]; then
    echo "Run as root: sudo ./install.sh" >&2
    exit 1
fi

PY=$(command -v python3 || true)
if [[ -z "$PY" ]] || ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
    echo "Python 3.11+ is required (Raspberry Pi OS Bookworm ships 3.11)." >&2
    exit 1
fi
if ! "$PY" -c 'import venv, ensurepip' 2>/dev/null; then
    echo "Installing python3-venv..."
    apt-get update -qq && apt-get install -y -qq python3-venv
fi

echo "==> service user"
if ! id "$SVC_USER" >/dev/null 2>&1; then
    useradd --system --home-dir "$PREFIX" --no-create-home --shell /usr/sbin/nologin "$SVC_USER"
fi

echo "==> directories"
install -d -m 750 -o "$SVC_USER" -g "$SVC_USER" "$PREFIX"
rm -rf "$PREFIX/app"
install -d -m 755 -o root -g root "$PREFIX/app" "$PREFIX/app/cusage"
install -m 644 -o root -g root "$SRC"/cusage/*.py "$PREFIX/app/cusage/"
install -m 755 -o root -g root "$SRC/claude_usage.py" "$PREFIX/app/"
install -m 644 -o root -g root "$SRC/requirements.txt" "$SRC/.env.example" "$PREFIX/app/"

echo "==> virtualenv"
if [[ ! -x "$PREFIX/venv/bin/python" ]]; then
    "$PY" -m venv "$PREFIX/venv"
fi
"$PREFIX/venv/bin/pip" install --quiet --upgrade pip
"$PREFIX/venv/bin/pip" install --quiet -r "$PREFIX/app/requirements.txt"
chown -R root:root "$PREFIX/venv"

echo "==> .env"
if [[ ! -f "$PREFIX/.env" ]]; then
    install -m 600 -o "$SVC_USER" -g "$SVC_USER" "$SRC/.env.example" "$PREFIX/.env"
    echo "    created $PREFIX/.env from template (no values). Fill it in."
fi
chown "$SVC_USER:$SVC_USER" "$PREFIX/.env"
chmod 600 "$PREFIX/.env"

env_value() { grep -E "^[[:space:]]*$1=" "$PREFIX/.env" | tail -1 | cut -d= -f2- | tr -d "\"' " || true; }
TZ_NAME=$(env_value TZ_NAME)
TZ_NAME=${TZ_NAME:-America/New_York}
if [[ ! -f "/usr/share/zoneinfo/$TZ_NAME" ]]; then
    echo "TZ_NAME '$TZ_NAME' is not a known timezone" >&2
    exit 1
fi

echo "==> systemd units (timers in $TZ_NAME)"
for f in "$SRC"/systemd/*.service "$SRC"/systemd/*.timer; do
    sed "s|@TZ@|$TZ_NAME|g" "$f" > "$UNIT_DIR/$(basename "$f")"
    chmod 644 "$UNIT_DIR/$(basename "$f")"
done
install -m 755 -o root -g root "$SRC/claude-usage.sh" /usr/local/bin/claude-usage
systemctl daemon-reload
systemctl enable --quiet claude-usage-poller.service
systemctl enable --quiet --now claude-usage-cc-tokens.timer claude-usage-rollup.timer claude-usage-sync.timer

AUTH_MODE=$(env_value CLAUDE_AUTH_MODE)
if [[ -n "$(env_value CLAUDE_SESSION_KEY)" || "$AUTH_MODE" == "oauth" ]]; then
    systemctl restart claude-usage-poller.service
    echo "==> poller (re)started"
else
    echo
    echo "Next steps:"
    echo "  1. sudo nano $PREFIX/.env                     # fill in the values (see README)"
    echo "  2. claude-usage test-alert                    # check the Slack webhook"
    echo "  3. claude-usage poll                          # one manual poll"
    echo "  4. claude-usage sync --check                  # check the Notion database"
    echo "  5. sudo systemctl start claude-usage-poller"
fi
echo "Done. Check with: claude-usage status"
