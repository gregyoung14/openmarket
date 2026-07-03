#!/bin/bash
# VPS Deployment Script for Polymarket BTC Scraper
# Usage: bash deploy_vps.sh <vps-ip> <repo-url>

set -e

if [ $# -lt 2 ]; then
    echo "Usage: bash deploy_vps.sh <vps-ip> <repo-url>"
    echo "Example: bash deploy_vps.sh 192.168.1.100 https://github.com/user/openmarket.git"
    exit 1
fi

VPS_IP=$1
REPO_URL=$2
VPS_USER="polymarket"
APP_DIR="/home/polymarket/openmarket"

echo "================================================================================"
echo "Polymarket BTC Scraper - VPS Deployment"
echo "================================================================================"
echo "VPS IP: $VPS_IP"
echo "Repository: $REPO_URL"
echo "App Directory: $APP_DIR"
echo ""

# Step 1: SSH setup
echo "Step 1: Verifying SSH access..."
ssh -q "root@$VPS_IP" "echo '✓ SSH access verified'" || {
    echo "✗ Cannot connect to VPS. Check IP and SSH setup."
    exit 1
}

# Step 2: System setup
echo ""
echo "Step 2: Setting up VPS system..."
ssh "root@$VPS_IP" << 'REMOTE_SCRIPT'
    set -e
    echo "  - Updating system packages..."
    apt-get update -qq
    apt-get upgrade -y -qq
    
    echo "  - Installing dependencies..."
    apt-get install -y -qq python3.11 python3.11-venv git curl wget supervisor
    
    echo "  - Creating application user..."
    if ! id -u $VPS_USER > /dev/null 2>&1; then
        useradd -m -s /bin/bash polymarket
        echo "  ✓ User 'polymarket' created"
    else
        echo "  ✓ User 'polymarket' already exists"
    fi
REMOTE_SCRIPT

# Step 3: Clone and setup application
echo ""
echo "Step 3: Cloning and setting up application..."
ssh "$VPS_USER@$VPS_IP" << REMOTE_SCRIPT
    set -e
    cd /tmp
    if [ ! -d openmarket ]; then
        git clone $REPO_URL openmarket
    else
        cd openmarket
        git pull origin main
    fi
    
    if [ ! -d $APP_DIR ]; then
        sudo mkdir -p $APP_DIR
        sudo chown polymarket:polymarket $APP_DIR
        mv /tmp/openmarket/* $APP_DIR/
    fi
    
    cd $APP_DIR
    echo "  ✓ Application cloned to $APP_DIR"
REMOTE_SCRIPT

# Step 4: Setup Python environment
echo ""
echo "Step 4: Setting up Python virtual environment..."
ssh "$VPS_USER@$VPS_IP" << REMOTE_SCRIPT
    set -e
    cd $APP_DIR
    python3.11 -m venv venv
    source venv/bin/activate
    pip install --upgrade pip setuptools wheel > /dev/null
    pip install -r requirements.txt > /dev/null
    echo "  ✓ Python environment ready"
REMOTE_SCRIPT

# Step 5: Configure environment
echo ""
echo "Step 5: Creating .env.local..."
echo "  ⚠️  You must manually add credentials to .env.local on the VPS:"
echo ""
echo "  SSH into VPS: ssh $VPS_USER@$VPS_IP"
echo "  Then edit: nano $APP_DIR/.env.local"
echo ""
echo "  Required credentials:"
echo "    - POLYGON_PRIVATE_KEY"
echo "    - POLYMARKET_API_KEY"
echo "    - POLYMARKET_SECRET"
echo "    - POLYMARKET_PASSPHRASE"
echo ""

# Create empty .env.local from .env.example
ssh "$VPS_USER@$VPS_IP" << REMOTE_SCRIPT
    cd $APP_DIR
    if [ ! -f .env.local ]; then
        cp .env.example .env.local
        chmod 600 .env.local
        echo "  ✓ .env.local created (placeholder)"
    fi
    mkdir -p logs
    chmod 755 logs
REMOTE_SCRIPT

# Step 6: Setup supervisor service
echo ""
echo "Step 6: Setting up supervisor service..."
ssh "root@$VPS_IP" << REMOTE_SCRIPT
    cat > /etc/supervisor/conf.d/polymarket-scraper.conf << 'EOF'
[program:polymarket-scraper]
command=/home/polymarket/openmarket/venv/bin/python3 -m services.scraper_daemon
directory=/home/polymarket/openmarket
user=polymarket
autostart=false
autorestart=true
redirect_stderr=true
stdout_logfile=/var/log/polymarket-scraper.log
stdout_logfile_maxbytes=50MB
stdout_logfile_backups=10
environment=PATH="/home/polymarket/openmarket/venv/bin"
EOF
    
    supervisorctl reread
    supervisorctl update
    echo "  ✓ Supervisor service configured"
REMOTE_SCRIPT

# Step 7: Setup backup cron
echo ""
echo "Step 7: Setting up backup cron job..."
ssh "$VPS_USER@$VPS_IP" << REMOTE_SCRIPT
    (crontab -l 2>/dev/null | grep -v "openmarket/scripts/backup"; \
     echo "0 2 * * * $APP_DIR/scripts/backup_data.sh") | crontab -
    echo "  ✓ Backup cron job scheduled (daily at 2 AM)"
REMOTE_SCRIPT

# Step 8: Verify installation
echo ""
echo "Step 8: Verifying installation..."
ssh "$VPS_USER@$VPS_IP" << REMOTE_SCRIPT
    set -e
    cd $APP_DIR
    source venv/bin/activate
    python3 -c "import py_clob_client; print('  ✓ py-clob-client installed')" 2>/dev/null || echo "  ✗ py-clob-client not found"
    python3 -c "import requests; print('  ✓ requests installed')" 2>/dev/null || echo "  ✗ requests not found"
    test -d logs && echo "  ✓ logs directory exists"
    test -f config.py && echo "  ✓ config.py found"
REMOTE_SCRIPT

# Summary
echo ""
echo "================================================================================"
echo "✓ Deployment Complete!"
echo "================================================================================"
echo ""
echo "Next Steps:"
echo ""
echo "1. SSH into VPS:"
echo "   ssh $VPS_USER@$VPS_IP"
echo ""
echo "2. Add credentials to .env.local:"
echo "   nano $APP_DIR/.env.local"
echo ""
echo "3. Start the service:"
echo "   sudo supervisorctl start polymarket-scraper"
echo ""
echo "4. Monitor the service:"
echo "   sudo supervisorctl status polymarket-scraper"
echo "   tail -f /var/log/polymarket-scraper.log"
echo ""
echo "5. Verify VPS location (must be outside US):"
echo "   curl -s ipinfo.io | grep country"
echo ""
echo "================================================================================"
