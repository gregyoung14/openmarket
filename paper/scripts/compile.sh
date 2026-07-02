#!/usr/bin/env bash
# Build OpenMarket paper PDF: figures -> latexmk (or tectonic fallback)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# Figure dependencies in paper/.venv
if [[ ! -x "$ROOT/.venv/bin/python" ]]; then
  python3 -m venv "$ROOT/.venv"
  "$ROOT/.venv/bin/pip" install -q matplotlib pyarrow pandas
fi
"$ROOT/.venv/bin/python" scripts/generate_figures.py
"$ROOT/.venv/bin/python" scripts/paper/analyze_unified.py --root "$ROOT/../data/hf_release/unified_parquet"
"$ROOT/.venv/bin/python" scripts/paper/analyze_research.py
"$ROOT/.venv/bin/python" scripts/paper/generate_ml_figures.py

# Prefer latexmk (MacTeX); fall back to tectonic (no sudo)
if [[ -x /Library/TeX/texbin/latexmk ]]; then
  export PATH="/Library/TeX/texbin:$PATH"
elif command -v latexmk >/dev/null 2>&1; then
  :
elif command -v tectonic >/dev/null 2>&1; then
  echo "latexmk not found; using tectonic"
  tectonic main.tex
  echo "built: $ROOT/main.pdf"
  exit 0
else
  echo "error: neither latexmk nor tectonic found." >&2
  if [[ -f /opt/homebrew/Caskroom/mactex-no-gui/2026.0324/mactex-20260324.pkg ]]; then
    echo "  MacTeX pkg downloaded but not installed (interrupted brew install?)." >&2
    echo "  Fix:  ./scripts/install-mactex.sh" >&2
  else
    echo "  Option A:  brew install --cask mactex-no-gui && ./scripts/install-mactex.sh" >&2
    echo "  Option B:  brew install tectonic" >&2
  fi
  exit 1
fi

latexmk -pdf -interaction=nonstopmode main.tex
echo "built: $ROOT/main.pdf"