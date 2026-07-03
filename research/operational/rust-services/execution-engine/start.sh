#!/bin/bash
# Start the Polymarket Execution Engine
# Pure Rust — uses polymarket-client-sdk for direct CLOB access
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="$SCRIPT_DIR/../../logs"
BINARY="$(dirname "$0")/target/release/execution-engine"

mkdir -p "$LOG_DIR"

# Load private key from wallet
export POLYMARKET_PRIVATE_KEY="${POLYMARKET_PRIVATE_KEY:-}"

# Strategy: HOLD_TO_RESOLVE (default) or MOMENTUM
export EXIT_STRATEGY="${EXIT_STRATEGY:-HOLD_TO_RESOLVE}"

# Logging level
export RUST_LOG="${RUST_LOG:-execution_engine=info}"

echo "═══════════════════════════════════════════"
echo "  Starting Execution Engine"
echo "  Strategy: $EXIT_STRATEGY"
echo "  Binary: $BINARY"
echo "  Logs: $LOG_DIR/execution-engine.log"
echo "═══════════════════════════════════════════"

# Run with output to log file (stdout+stderr)
exec "$BINARY" >> "$LOG_DIR/execution-engine.log" 2>&1
