#!/bin/bash
# Quick Reference: Rust Binance WebSocket Service

echo "🦀 Rust Binance WebSocket Service - Quick Reference"
echo "=================================================="
echo ""

echo "📍 Service Details:"
echo "  Binary: research/operational/rust-services/binance-websocket/target/release/binance-websocket"
echo "  Port: 8001"
echo "  Protocol: HTTP/WebSocket"
echo ""

echo "🚀 Start Service:"
echo "  ./scripts/services/start_binance_ws.sh"
echo "  # Or directly:"
echo "  ./rust-services/binance-websocket/target/release/binance-websocket"
echo ""

echo "🛑 Stop Service:"
echo "  pkill binance-websocket"
echo ""

echo "✅ Test Service:"
echo "  ./scripts/monitoring/test_rust_service.sh"
echo "  # Or individual tests:"
echo "  curl http://localhost:8001/health | jq '.'"
echo ""

echo "📊 API Endpoints:"
echo "  GET  /              - Health check (same as /health)"
echo "  GET  /health        - Service health and stats"
echo "  GET  /ws            - WebSocket connection"
echo "  GET  /candles/:interval?limit=N  - Historical candles"
echo ""

echo "⚙️  Valid Intervals: 1s, 5s, 1m, 5m, 15m, 1h"
echo ""

echo "📝 View Logs:"
echo "  journalctl -u binance-websocket -f"
echo "  # Or if running manually, check terminal output"
echo ""

echo "🔧 Rebuild (if code changed):"
echo "  cd rust-services/binance-websocket"
echo "  cargo build --release"
echo ""

echo "📦 Service Status:"
if pgrep -f "binance-websocket" > /dev/null; then
    echo "  Status: ✅ RUNNING"
    HEALTH=$(curl -s http://localhost:8001/health 2>/dev/null)
    if [ $? -eq 0 ]; then
        echo "  Trades: $(echo "$HEALTH" | jq -r '.trades_stored' 2>/dev/null || echo 'N/A')"
        echo "  Port: 8001 (responding)"
    else
        echo "  Port: 8001 (not responding)"
    fi
else
    echo "  Status: ⛔ NOT RUNNING"
fi
echo ""

echo "🔗 Dependent Services:"
echo "  - trader-backend (port 8000) - proxies to this service"
echo "  - binance-dashboard - connects via WebSocket"
echo "  - Any client using localhost:8001"
echo ""

echo "📚 Documentation:"
echo "  README: ./rust-services/binance-websocket/README.md"
echo "  Migration: ./RUST_MIGRATION.md"
echo ""
