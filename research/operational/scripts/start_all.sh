#!/bin/bash
# ============================================================
# Master Startup Script for Polymarket BTC Trader
# ============================================================
#
# All services are managed via systemd user services.
# This script starts/restarts them and shows status.
#
# Services:
#   1. Binance WebSocket     (port 8001)
#   2. Polymarket WebSocket  (port 8002)
#   3. Signal Engine         (port 8003)
#   4. Execution Engine      (port 8004)
#   5. Market Data Recorder  (port 8005)
#   6. Redeem Positions      (port 8006)
#
# Monitoring:
#   Uptime Kuma dashboard:   http://<host>:3001
#   Public status page:      http://<host>:3001/status/btc-trading
#
# ============================================================

set -e

# Colors
GREEN='\033[0;32m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

SERVICES=(
    binance-websocket
    polymarket-websocket
    signal-engine
    execution-engine
    market-data-recorder
    redeem-positions
)

LOG_DIR="$(dirname "$0")/../logs"

echo -e "${CYAN}═══════════════════════════════════════════════════${NC}"
echo -e "${CYAN}  Polymarket BTC Trader — System Startup (systemd)${NC}"
echo -e "${CYAN}═══════════════════════════════════════════════════${NC}"

# Start all services
for i in "${!SERVICES[@]}"; do
    svc="${SERVICES[$i]}"
    step=$((i + 1))
    echo -e "${BLUE}[${step}/${#SERVICES[@]}] Starting ${svc}...${NC}"
    systemctl --user restart "$svc"
    echo -e "  ${GREEN}✓${NC} Requested"
done

# Wait for them to come up
echo ""
echo -e "${BLUE}Waiting 5s for services to bind ports...${NC}"
sleep 5

# Health check
echo ""
declare -A PORT_MAP=(
    [binance-websocket]=8001
    [polymarket-websocket]=8002
    [signal-engine]=8003
    [execution-engine]=8004
    [market-data-recorder]=8005
    [redeem-positions]=8006
)

for svc in "${SERVICES[@]}"; do
    state=$(systemctl --user is-active "$svc" 2>/dev/null || echo "inactive")
    port="${PORT_MAP[$svc]:-}"
    health=""
    if [[ -n "$port" ]]; then
        if curl -sf "http://127.0.0.1:${port}/health" >/dev/null 2>&1; then
            health=" — HTTP OK"
        else
            health=" — HTTP not responding yet"
        fi
    fi
    if [[ "$state" == "active" ]]; then
        echo -e "  ${GREEN}●${NC} ${svc}: ${GREEN}${state}${NC}${health}"
    else
        echo -e "  ${RED}●${NC} ${svc}: ${RED}${state}${NC}${health}"
    fi
done

echo ""
echo -e "${CYAN}═══════════════════════════════════════════════════${NC}"
echo -e "${CYAN}  System is Running${NC}"
echo -e "${CYAN}═══════════════════════════════════════════════════${NC}"
echo -e "  ${BLUE}Useful commands:${NC}"
echo -e "    systemctl --user status <service>     # check one service"
echo -e "    systemctl --user restart <service>     # restart one service"
echo -e "    systemctl --user stop <service>        # stop one service"
echo -e "    journalctl --user -u <service> -f      # follow logs"
echo -e ""
echo -e "  ${BLUE}Log files:${NC}"
echo -e "    tail -f $LOG_DIR/*.log"
echo -e ""
echo -e "  ${BLUE}Monitoring:${NC}"
echo -e "    Uptime Kuma:   http://localhost:3001"
echo -e "    Status Page:   http://localhost:3001/status/btc-trading"
