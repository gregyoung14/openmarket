#!/usr/bin/env bash
set -euo pipefail

cargo fmt --all --check
cargo check --workspace
python3 -m compileall -q research datasets
