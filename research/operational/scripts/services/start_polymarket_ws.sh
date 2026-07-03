#!/bin/bash
# Start the Polymarket WebSocket service

echo "🚀 Starting Polymarket WebSocket service..."
echo "==========================================="

BINARY="$(dirname "$0")/../../rust-services/polymarket-websocket/target/release/polymarket-websocket"

if [ ! -f "$BINARY" ]; then
    echo "❌ Binary not found. Building..."
    cd "$(dirname "$0")/../../rust-services/polymarket-websocket"
    cargo build --release
    if [ $? -ne 0 ]; then
        echo "❌ Build failed!"
        exit 1
    fi
fi

# Kill any existing process
pkill -f "polymarket-websocket/target" || true
sleep 1

echo "Starting on 0.0.0.0:8002..."
$BINARY &

echo "✅ Service started"
echo "   PID: $!"
sleep 2

# Check if it's running
if curl -s http://localhost:8002/health > /dev/null 2>&1; then
    echo "✅ Service is responding"
    curl -s http://localhost:8002/health | jq .
else
    echo "⚠️  Service may not be ready yet"
fi
