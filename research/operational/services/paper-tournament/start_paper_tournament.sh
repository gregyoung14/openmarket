#!/usr/bin/env bash
# start_paper_tournament.sh — Spin up multi-strategy paper-trade tournament
#
# Prerequisites:
#   - Shared data layer running (binance-websocket :8001, polymarket-websocket :8002)
#   - Signal engine binary built: cargo build --release -p signal-engine
#   - Paper executor binary built: cargo build --release -p paper-executor
#
# Usage:
#   ./start_paper_tournament.sh          # Start all strategies
#   ./start_paper_tournament.sh stop     # Stop all tournament processes

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
SIGNAL_BIN="$REPO_DIR/rust-services/signal-engine/target/release/signal-engine"
PAPER_BIN="$REPO_DIR/rust-services/paper-executor/target/release/paper-executor"
LOG_DIR="${PAPER_LOG_DIR:-$(dirname "$0")/logs}"
PID_DIR="$LOG_DIR/.pids"
CALIBRATED_MODEL_PATH="${PAPER_CALIBRATED_MODEL_PATH:-$REPO_DIR/data/ml_artifacts/latest_binary_model.json}"

# Starting paper bankroll for each strategy
BANKROLL="${PAPER_BANKROLL:-100.0}"

# ── Strategy definitions ────────────────────────────────────────────────────
# Format: "name|signal_port|executor_port|env_overrides"
# Signal ports: 8010-8019, Executor ports: 9010-9019
STRATEGIES=(
  "v14_baseline|8010|9010|"
  "v14.1_no_volgate|8011|9011|ENABLE_VOLUME_GATE=false"
  "v15_brier_cb|8012|9012|ENABLE_VOLUME_GATE=false"
  "v14_relaxed_conf|8013|9013|MIN_CONFIDENCE=0.56 MIN_EDGE_OVERRIDE=0.05"
  "v14_wide_confirm|8014|9014|"
  "v14_tight_regime|8015|9015|"
  "v14_canary_early_highcap|8016|9016|MIN_SECS_OVERRIDE=15 MAX_ENTRY_PRICE_OVERRIDE=0.75"
  "v16_calibrated_shadow|8017|9017|MIN_SECS_OVERRIDE=15 MAX_ENTRY_PRICE_OVERRIDE=0.75 CALIBRATED_SCORER_MODE=shadow CALIBRATED_MODEL_PATH=$CALIBRATED_MODEL_PATH"
  "v16_calibrated_active_paper|8018|9018|MIN_SECS_OVERRIDE=15 MAX_ENTRY_PRICE_OVERRIDE=0.75 CALIBRATED_SCORER_MODE=active CALIBRATED_MODEL_PATH=$CALIBRATED_MODEL_PATH CALIBRATED_MIN_EV=0.00"
  "v17_canary_priceband|8019|9019|MIN_SECS_OVERRIDE=15 MIN_ENTRY_PRICE_OVERRIDE=0.55 MAX_ENTRY_PRICE_OVERRIDE=0.75 MIN_EDGE_OVERRIDE=0.10 MAX_EDGE_OVERRIDE=0.35"
  "v17_canary_down_priceband|8020|9020|MIN_SECS_OVERRIDE=15 MIN_ENTRY_PRICE_OVERRIDE=0.55 MAX_ENTRY_PRICE_OVERRIDE=0.75 MIN_EDGE_OVERRIDE=0.10 MAX_EDGE_OVERRIDE=0.35 ALLOW_UP=false"
  "v17_calibrated_active_priceband|8021|9021|MIN_SECS_OVERRIDE=15 MIN_ENTRY_PRICE_OVERRIDE=0.55 MAX_ENTRY_PRICE_OVERRIDE=0.75 MAX_EDGE_OVERRIDE=0.35 CALIBRATED_SCORER_MODE=active CALIBRATED_MODEL_PATH=$CALIBRATED_MODEL_PATH CALIBRATED_MIN_EV=0.00"
)

# ── Functions ───────────────────────────────────────────────────────────────

stop_tournament() {
    echo "Stopping paper-trade tournament..."
    if [[ -d "$PID_DIR" ]]; then
        for pidfile in "$PID_DIR"/*.pid; do
            [[ -f "$pidfile" ]] || continue
            pid=$(cat "$pidfile")
            name=$(basename "$pidfile" .pid)
            if kill -0 "$pid" 2>/dev/null; then
                kill "$pid" 2>/dev/null || true
                echo "  Stopped $name (PID $pid)"
            fi
            rm -f "$pidfile"
        done
    fi
    echo "Tournament stopped."
}

start_tournament() {
    # Verify binaries exist
    if [[ ! -x "$SIGNAL_BIN" ]]; then
        echo "ERROR: Signal engine binary not found at $SIGNAL_BIN"
        echo "Build it: cd $REPO_DIR/rust-services && cargo build --release -p signal-engine"
        exit 1
    fi
    if [[ ! -x "$PAPER_BIN" ]]; then
        echo "ERROR: Paper executor binary not found at $PAPER_BIN"
        echo "Build it: cd $REPO_DIR/rust-services && cargo build --release -p paper-executor"
        exit 1
    fi

    mkdir -p "$LOG_DIR" "$PID_DIR"

    if [[ ! -f "$CALIBRATED_MODEL_PATH" ]]; then
        echo "WARNING: calibrated model artifact not found at $CALIBRATED_MODEL_PATH"
        echo "         Calibrated paper variants will fall back to disabled mode until the artifact exists."
    fi

    echo "═══════════════════════════════════════════════════════════"
    echo "  Paper-Trade Tournament"
    echo "  Strategies: ${#STRATEGIES[@]}"
    echo "  Bankroll: \$$BANKROLL per strategy"
    echo "  Log dir: $LOG_DIR"
    echo "  Calibrated artifact: $CALIBRATED_MODEL_PATH"
    echo "═══════════════════════════════════════════════════════════"

    for entry in "${STRATEGIES[@]}"; do
        IFS='|' read -r name sig_port exec_port env_vars <<< "$entry"

        echo ""
        echo "Starting $name (signal :$sig_port, executor :$exec_port)"
        [[ -n "$env_vars" ]] && echo "  Overrides: $env_vars"

        # Start signal engine with port override + any strategy-specific env vars
        # Use nohup + disown so processes survive terminal/SSH disconnects
        local sig_log="$LOG_DIR/signal_${name}.log"
        setsid nohup env RUST_LOG=info SIGNAL_PORT="$sig_port" $env_vars \
            "$SIGNAL_BIN" \
            > "$sig_log" 2>&1 &
        local sig_pid=$!
        disown "$sig_pid"
        echo "$sig_pid" > "$PID_DIR/signal_${name}.pid"
        echo "  Signal engine PID: $sig_pid"

        # Small delay for signal engine to start
        sleep 1

        # Start paper executor
        local csv_file="$LOG_DIR/paper_log_${name}.csv"
        local exec_log="$LOG_DIR/executor_${name}.log"
        setsid nohup "$PAPER_BIN" \
            --signal-url "ws://127.0.0.1:${sig_port}/ws" \
            --strategy "$name" \
            --log "$csv_file" \
            > "$exec_log" 2>&1 &
        local exec_pid=$!
        disown "$exec_pid"
        echo "$exec_pid" > "$PID_DIR/executor_${name}.pid"
        echo "  Paper executor PID: $exec_pid"
    done

    echo ""
    echo "═══════════════════════════════════════════════════════════"
    echo "  Tournament running. ${#STRATEGIES[@]} strategies active."
    echo "  CSV logs: $LOG_DIR/paper_log_*.csv"
    echo "  Stop with: $0 stop"
    echo "═══════════════════════════════════════════════════════════"
}

# ── Main ────────────────────────────────────────────────────────────────────

case "${1:-start}" in
    stop)
        stop_tournament
        ;;
    start)
        stop_tournament 2>/dev/null || true
        start_tournament
        ;;
    *)
        echo "Usage: $0 [start|stop]"
        exit 1
        ;;
esac
