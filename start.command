#!/bin/bash
# Ranchers Finest — Pricing app launcher.
# Double-click this file in Finder, or run it from Terminal.
cd "$(dirname "$0")" || exit 1

echo "Preparing the Ranchers Finest pricing app..."

# The hardened app refuses to boot without a SECRET_KEY (by design).
# This launcher is for a server where SECRET_KEY is set in the environment.
# For local testing on a Mac, use start_local.command instead.
if [ -z "$SECRET_KEY" ]; then
  echo ""
  echo "  STOP: no SECRET_KEY is set, so the app would refuse to start."
  echo "  For local testing, close this window and double-click"
  echo "  start_local.command instead (it sets a dev-only key)."
  echo ""
  read -r -p "Press Enter to close..."
  exit 1
fi

python3 -m pip install -q -r requirements.txt 2>/dev/null

# First run: create the database, a default admin, and load the pricelists.
if [ ! -s instance/pricing.db ]; then
  echo "First run — setting up the database and loading pricelists..."
  RF_ADMIN_USER=admin RF_ADMIN_PASS=ranchers2026 RF_ADMIN_NAME="Administrator" python3 setup.py
  python3 importer.py
  LOGIN_LINE="Login:  admin   /   ranchers2026   (change it under Admin once you are in)"
else
  LOGIN_LINE="Login:  use the username and password you set up earlier"
fi

echo ""
echo "============================================================"
echo "  Ranchers Finest Pricing is starting."
echo "  Open this in your browser:   http://127.0.0.1:8000"
echo "  $LOGIN_LINE"
echo "  To stop the app: come back here and press  Control + C"
echo "============================================================"
echo ""
python3 -m flask --app app run --port 8000
