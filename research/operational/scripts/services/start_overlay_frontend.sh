#!/bin/bash
# Start the Market Overlay Frontend (SolidJS/Vite)

echo "🚀 Starting Market Overlay Frontend..."
echo "======================================"

FRONTEND_DIR="$(dirname "$0")/../../market-overlay-frontend"
LOG_FILE="/tmp/market_frontend.log"

if [ ! -d "$FRONTEND_DIR" ]; then
    echo "❌ Frontend directory not found at $FRONTEND_DIR"
    exit 1
fi

cd "$FRONTEND_DIR"

# Check if port 5173 is already in use
PORT_PID=$(lsof -t -i:5173)
if [ -n "$PORT_PID" ]; then
    echo "⚠️  Port 5173 is already in use by PID: $PORT_PID. Stopping it..."
    kill $PORT_PID
    sleep 1
fi

echo "Starting Vite dev server on port 5173..."
# Using --host to allow external access if needed (similar to previous script)
nohup npm run dev -- --host 0.0.0.0 --port 5173 > "$LOG_FILE" 2>&1 &

# Save PID
PID=$!
echo $PID > /tmp/market_frontend.pid

echo "✅ Frontend started in background"
echo "   URL: http://localhost:5173"
echo "   Logs: tail -f $LOG_FILE"
echo "   PID: $PID"
