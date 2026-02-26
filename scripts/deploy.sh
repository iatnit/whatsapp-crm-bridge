#!/bin/bash
# ============================================================
# WhatsApp CRM Bridge — One-command deploy to fresh Debian/Ubuntu
#
# Usage:
#   ./scripts/deploy.sh root@YOUR_SERVER_IP
#
# What it does:
#   1. Generate SSH key if needed & copy to server
#   2. Upload project files via rsync
#   3. Install Docker + Nginx + Certbot on server
#   4. Start the app with docker compose
#
# After running, you still need to:
#   - Point a domain A record to the server IP
#   - Run: ssh root@IP "certbot --nginx -d your-domain.com"
#   - Configure webhook URL in Meta Developer Console
# ============================================================

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

if [ $# -lt 1 ]; then
    echo -e "${RED}Usage: $0 root@SERVER_IP${NC}"
    exit 1
fi

SERVER="$1"
REMOTE_DIR="/opt/whatsapp-crm-bridge"
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

echo -e "${GREEN}=== WhatsApp CRM Bridge Deploy ===${NC}"
echo "Server: $SERVER"
echo "Local:  $SCRIPT_DIR"
echo ""

# ── Step 0: SSH key ──────────────────────────────────────────
if [ ! -f ~/.ssh/id_ed25519 ]; then
    echo -e "${YELLOW}Generating SSH key...${NC}"
    ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519 -N "" -q
fi

echo -e "${YELLOW}Copying SSH key to server (you may need to enter the password)...${NC}"
ssh-copy-id -i ~/.ssh/id_ed25519.pub "$SERVER" 2>/dev/null || true

# Test connection
echo -e "${YELLOW}Testing SSH connection...${NC}"
ssh -o ConnectTimeout=10 "$SERVER" "echo 'SSH OK'" || {
    echo -e "${RED}Cannot connect to $SERVER${NC}"
    exit 1
}

# ── Step 1: Upload project files ─────────────────────────────
echo -e "${GREEN}[1/4] Uploading project files...${NC}"
rsync -avz --exclude '.venv' --exclude '__pycache__' --exclude 'data/whatsapp.db' \
    --exclude 'data/media/*' --exclude '.git' --exclude '.env' \
    "$SCRIPT_DIR/" "$SERVER:$REMOTE_DIR/"

# Upload .env separately (contains secrets)
if [ -f "$SCRIPT_DIR/.env" ]; then
    echo "Uploading .env..."
    rsync -avz "$SCRIPT_DIR/.env" "$SERVER:$REMOTE_DIR/.env"
fi

# Ensure data directories exist
ssh "$SERVER" "mkdir -p $REMOTE_DIR/data/media"

# ── Step 2: Install Docker ───────────────────────────────────
echo -e "${GREEN}[2/4] Installing Docker on server...${NC}"
ssh "$SERVER" bash << 'REMOTE_DOCKER'
set -e
if command -v docker &>/dev/null; then
    echo "Docker already installed: $(docker --version)"
else
    echo "Installing Docker..."
    apt-get update -qq
    apt-get install -y -qq ca-certificates curl gnupg
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    chmod a+r /etc/apt/keyrings/docker.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian $(. /etc/os-release && echo "$VERSION_CODENAME") stable" > /etc/apt/sources.list.d/docker.list
    apt-get update -qq
    apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin
    systemctl enable docker
    echo "Docker installed: $(docker --version)"
fi
REMOTE_DOCKER

# ── Step 3: Install Nginx + Certbot ──────────────────────────
echo -e "${GREEN}[3/4] Installing Nginx + Certbot...${NC}"
ssh "$SERVER" bash << 'REMOTE_NGINX'
set -e
if command -v nginx &>/dev/null; then
    echo "Nginx already installed"
else
    apt-get install -y -qq nginx certbot python3-certbot-nginx
    systemctl enable nginx
    echo "Nginx installed"
fi

# Deploy nginx config (without SSL for now — certbot adds it later)
cat > /etc/nginx/sites-available/whatsapp-crm << 'NGINXCONF'
server {
    listen 80;
    server_name _;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        client_max_body_size 50m;
    }
}
NGINXCONF

ln -sf /etc/nginx/sites-available/whatsapp-crm /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx
echo "Nginx configured"
REMOTE_NGINX

# ── Step 4: Build & Start ────────────────────────────────────
echo -e "${GREEN}[4/4] Building and starting the app...${NC}"
ssh "$SERVER" bash << REMOTE_START
set -e
cd $REMOTE_DIR
docker compose down 2>/dev/null || true
docker compose up -d --build
sleep 3
echo ""
echo "=== Health check ==="
curl -s http://127.0.0.1:8000/health
echo ""
echo ""
echo "=== Container status ==="
docker compose ps
REMOTE_START

# ── Done ─────────────────────────────────────────────────────
SERVER_IP=$(echo "$SERVER" | cut -d@ -f2)
echo ""
echo -e "${GREEN}============================================${NC}"
echo -e "${GREEN}  Deploy complete!${NC}"
echo -e "${GREEN}============================================${NC}"
echo ""
echo "  Health: http://$SERVER_IP/health"
echo "  Stats:  http://$SERVER_IP/api/v1/stats"
echo ""
echo -e "${YELLOW}Next steps:${NC}"
echo "  1. Point a domain A record → $SERVER_IP"
echo "  2. SSH in and run: certbot --nginx -d YOUR_DOMAIN"
echo "  3. In Meta Developer Console, set webhook URL:"
echo "     https://YOUR_DOMAIN/api/v1/webhook"
echo "  4. Subscribe to 'messages' events"
echo ""
