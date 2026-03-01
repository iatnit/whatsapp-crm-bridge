#!/bin/bash
# Setup script for the Obsidian Chat Receiver on macOS
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo "=== Obsidian Chat Receiver Setup ==="

# 1. Create virtual environment
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
fi

echo "Installing dependencies..."
venv/bin/pip install -q -r requirements.txt

# 2. Create .env if not exists
if [ ! -f ".env" ]; then
    echo "Creating .env file..."
    cat > .env << 'EOF'
# Shared HMAC secret (must match server OBSIDIAN_SYNC_SECRET)
SYNC_SECRET=change-me-to-a-random-secret

# Obsidian CRM path (default is auto-detected)
# CRM_BASE_PATH=/Users/zhangyun/Nutstore Files/我的坚果云/LuckyOS/LOCA-Factory-Brain/05-Sales Library/CRM

# Server binding
HOST=127.0.0.1
PORT=8765
EOF
    echo "  -> Created .env — please set SYNC_SECRET before starting"
fi

# 3. Create data directory
mkdir -p data

echo ""
echo "Setup complete! To start:"
echo "  cd $SCRIPT_DIR"
echo "  venv/bin/python receiver.py"
echo ""
echo "Or install the LaunchAgent for auto-start (see com.loca.obsidian-receiver.plist)"
