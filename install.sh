#!/bin/bash
# Installs the 10:00, 13:00 and 23:00 email summaries on a Mac.
# Safe to run again: your settings are kept.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OJ_HOME="${OPENJARVIS_HOME:-$HOME/.openjarvis}"
APP_DIR="$OJ_HOME/email-whatsapp"
ENV_FILE="$APP_DIR/settings.env"
LABEL="com.openjarvis.email-whatsapp"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PY="$OJ_HOME/.venv/bin/python"
GMAIL_PY="$OJ_HOME/src/src/openjarvis/connectors/gmail.py"

if [ ! -x "$PY" ]; then
  echo "OpenJarvis was not found at $OJ_HOME. Install it first (see README.md)." >&2
  exit 1
fi
if ! command -v launchctl >/dev/null 2>&1; then
  echo "This installer is for macOS, where launchctl runs the schedule." >&2
  exit 1
fi

mkdir -p "$APP_DIR" "$HOME/Library/LaunchAgents"
cp "$REPO_DIR/src/email_whatsapp.py" "$APP_DIR/email_whatsapp.py"

if [ ! -f "$ENV_FILE" ]; then
  cp "$REPO_DIR/settings.example.env" "$ENV_FILE"
fi
chmod 600 "$ENV_FILE"

# OpenJarvis skips spam by default, so ask Gmail to include it.
if [ ! -f "$GMAIL_PY" ]; then
  echo "WARNING: $GMAIL_PY was not found, so spam may not be included." >&2
elif ! grep -q includeSpamTrash "$GMAIL_PY"; then
  perl -0pi -e 's/(\n    resp = _gmail_get\(f"\{_GMAIL_API_BASE\}\/messages", token, params\))/\n    params["includeSpamTrash"] = "true"$1/' "$GMAIL_PY"
  if grep -q includeSpamTrash "$GMAIL_PY"; then
    echo "Spam access turned on."
  else
    echo "WARNING: OpenJarvis has changed, so spam could not be turned on." >&2
  fi
fi

cat > "$PLIST" <<PLISTXML
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>$PY</string><string>$APP_DIR/email_whatsapp.py</string></array>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string></dict>
  <key>StartCalendarInterval</key>
  <array>
    <dict><key>Hour</key><integer>10</integer><key>Minute</key><integer>0</integer></dict>
    <dict><key>Hour</key><integer>13</integer><key>Minute</key><integer>0</integer></dict>
    <dict><key>Hour</key><integer>23</integer><key>Minute</key><integer>0</integer></dict>
  </array>
  <key>StandardOutPath</key><string>$APP_DIR/run.log</string>
  <key>StandardErrorPath</key><string>$APP_DIR/run.log</string>
</dict>
</plist>
PLISTXML

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

echo "Schedule installed: 10:00, 13:00 and 23:00 Mac time."
echo "Add your WhatsApp details to: $ENV_FILE"
