#!/bin/sh
# Installed as /usr/local/bin/claude-usage. Runs the CLI as the service user so
# it can read /opt/claude-usage/.env and write usage.db.
APP=/opt/claude-usage
if [ "$(id -un)" = "claudeusage" ]; then
    exec "$APP/venv/bin/python" "$APP/app/claude_usage.py" "$@"
fi
exec sudo -u claudeusage "$APP/venv/bin/python" "$APP/app/claude_usage.py" "$@"
