#!/usr/bin/env bash

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
UNIT_SRC_DIR="$REPO_DIR/systemd/user"
UNIT_DST_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
SERVICE_NAME="storage-guardrails.service"
TIMER_NAME="storage-guardrails.timer"

mkdir -p "$UNIT_DST_DIR"

install -m 0644 "$UNIT_SRC_DIR/$SERVICE_NAME" "$UNIT_DST_DIR/$SERVICE_NAME"
install -m 0644 "$UNIT_SRC_DIR/$TIMER_NAME" "$UNIT_DST_DIR/$TIMER_NAME"

systemctl --user daemon-reload
systemctl --user enable --now "$TIMER_NAME"

if ! systemctl --user start "$SERVICE_NAME"; then
    echo "Initial storage guardrail run returned non-zero. Inspect journalctl --user -u $SERVICE_NAME for details." >&2
fi

echo
systemctl --user list-timers "$TIMER_NAME" --no-pager

echo
systemctl --user status "$SERVICE_NAME" --no-pager || true