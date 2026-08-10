#!/bin/bash
# ASO Tracker Automation Setup
# Run once: ./setup.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== ASO Tracker Automation Setup ==="

# 1. Install Python dependencies
echo "Installing Python packages..."
pip3 install --user requests PyJWT cryptography

# 2. Create logs directory
mkdir -p "$SCRIPT_DIR/logs"

# 3. Create gp_data directory for Google Play CSVs
mkdir -p "$SCRIPT_DIR/gp_data"

# 4. Make update script executable
chmod +x "$SCRIPT_DIR/update_dashboard.py"

# 5. Install launchd agent (daily at 9 AM)
PLIST_SRC="$SCRIPT_DIR/com.aso-tracker.update.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.aso-tracker.update.plist"

# Unload if already loaded
launchctl unload "$PLIST_DST" 2>/dev/null || true

cp "$PLIST_SRC" "$PLIST_DST"
launchctl load "$PLIST_DST"

echo ""
echo "=== Setup Complete ==="
echo ""
echo "Schedule: Daily at 9:00 AM"
echo "  - Sign-ups/eFTDs/eFTTs updated from bydata every day"
echo "  - ASC downloads fetched on Monday and Wednesday"
echo "  - Google Play: drop CSVs into $SCRIPT_DIR/gp_data/"
echo ""
echo "Logs: $SCRIPT_DIR/logs/"
echo ""
echo "Manual run: python3 $SCRIPT_DIR/update_dashboard.py"
echo ""
echo "To stop: launchctl unload ~/Library/LaunchAgents/com.aso-tracker.update.plist"
