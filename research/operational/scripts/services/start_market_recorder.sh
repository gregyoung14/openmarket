#!/bin/bash
# Start market-data-recorder service

echo "🚀 Starting Market Data Recorder service..."
echo "==========================================="

BINARY="$(dirname "$0")/../../rust-services/market-data-recorder/target/release/market-data-recorder"

if [ ! -f "$BINARY" ]; then
    echo "❌ Binary not found. Building..."
    cd "$(dirname "$0")/../../rust-services/market-data-recorder"
    cargo build --release
    if [ $? -ne 0 ]; then
        echo "❌ Build failed!"
        exit 1
    fi
fi

lsof -ti :8003 | xargs kill -9 2>/dev/null || true
sleep 1

echo "Starting on 0.0.0.0:8003..."
nohup "$BINARY" > /tmp/market_recorder.log 2>&1 &
PID=$!
echo "$PID" > /tmp/market_recorder.pid

echo "✅ Started with PID: $PID"
sleep 2

if timeout 2 curl -s http://localhost:8003/health > /dev/null 2>&1; then
    echo "✅ Service responding on port 8003"
    curl -s http://localhost:8003/health | jq .
else
    echo "⚠️  Service may still be warming up"
fi
