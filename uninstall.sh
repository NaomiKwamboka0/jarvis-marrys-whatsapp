#!/bin/bash
# Stops the schedule and removes this project's files, including your settings.
# OpenJarvis itself is left in place.
set -euo pipefail

OJ_HOME="${OPENJARVIS_HOME:-$HOME/.openjarvis}"
LABEL="com.openjarvis.email-whatsapp"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
rm -rf "$OJ_HOME/email-whatsapp"
echo "Removed. To undo the spam change: git -C \"$OJ_HOME/src\" checkout src/openjarvis/connectors/gmail.py"
