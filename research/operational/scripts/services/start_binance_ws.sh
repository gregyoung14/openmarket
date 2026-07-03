#!/bin/bash
# Start the Rust Binance WebSocket service

echo "Starting Rust Binance WebSocket service..."

# Change to project root
cd "$(dirname "$0")/../.."

# Ensure the binary exists
if [ ! -f "rust-services/binance-websocket/target/release/binance-websocket" ]; then
    echo "Error: Binary not found. Building..."
    cd rust-services/binance-websocket
    source $HOME/.cargo/env
    cargo build --release
    cd ../..
fi

# Kill any existing process on port 8001
lsof -ti :8001 | xargs kill -9 2>/dev/null || true
sleep 1

# Run the service in background
echo "Starting on 0.0.0.0:8001..."
nohup rust-services/binance-websocket/target/release/binance-websocket > /tmp/binance.log 2>&1 &
PID=$!
echo "✅ Started with PID: $PID"
echo $PID > /tmp/binance.pid

# Give it a moment to start
sleep 2

# Quick health check (with timeout)
if timeout 2 curl -s http://localhost:8001/health > /dev/null 2>&1; then
    echo "✅ Service responding on port 8001"
else
    echo "⚠️  Service started but not yet responding (may still be initializing)"
fi
