#!/bin/bash
# Run Rust Binance WebSocket service
cd "$(dirname "$0")"
./target/release/binance-websocket
