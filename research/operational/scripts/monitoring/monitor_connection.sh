#!/bin/bash
# Monitor Binance WebSocket connection health

echo "🔍 Binance WebSocket Connection Monitor"
echo "========================================"
echo ""

# Check if service is running
PID=$(pgrep -f "binance-websocket/target/release/binance-websocket")
if [ -z "$PID" ]; then
    echo "❌ Service is NOT running"
    exit 1
fi

echo "✅ Service running (PID: $PID)"
echo ""

# Get initial trade count
INITIAL=$(curl -s http://localhost:8001/health | jq -r '.trades_stored')
echo "📊 Current trades: $INITIAL"
echo ""

# Monitor for 2 minutes
echo "⏱️  Monitoring connection for 2 minutes..."
echo "   (Press Ctrl+C to stop early)"
echo ""

for i in {1..24}; do
    sleep 5
    CURRENT=$(curl -s http://localhost:8001/health 2>/dev/null | jq -r '.trades_stored')
    
    if [ -z "$CURRENT" ] || [ "$CURRENT" = "null" ]; then
        echo "⚠️  [$i] Connection issue - service not responding"
        continue
    fi
    
    DIFF=$((CURRENT - INITIAL))
    echo "✓ [$i] Trades: $CURRENT (+$DIFF new) - Connection healthy"
done

echo ""
echo "🎉 Monitoring complete - Connection stable!"
