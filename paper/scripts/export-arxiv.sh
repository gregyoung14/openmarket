#!/usr/bin/env bash
# Build paper and pack an arXiv-ready source tarball.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

eval "$(/usr/libexec/path_helper)" 2>/dev/null || true
export PATH="/Library/TeX/texbin:$PATH"

# Ensure PDF + bibliography are current
"$(dirname "$0")/compile.sh"

STAMP="$(date +%Y%m%d)"
OUTDIR="$ROOT/arxiv-submit"
TARBALL="$ROOT/openmarket-paper-arxiv-${STAMP}.tar.gz"

rm -rf "$OUTDIR"
mkdir -p "$OUTDIR/sections" "$OUTDIR/assets/figures"

cp main.tex bibliography.bib main.bbl "$OUTDIR/"
mkdir -p "$OUTDIR/assets/stats"
cp assets/stats/characterization.tex "$OUTDIR/assets/stats/" 2>/dev/null || true
cp assets/stats/research_stats.tex "$OUTDIR/assets/stats/" 2>/dev/null || true
cp sections/*.tex "$OUTDIR/sections/"
# Only LaTeX-referenced data-driven matplotlib figures (pipeline diagrams are native TikZ)
for fig in lead-lag-hist.pdf daily-volume.pdf dataset-scale.pdf \
  feature-correlation.pdf calibration-curve.pdf walk-forward-metrics.pdf \
  throughput-bench.pdf lead-lag-vs-disagreement.pdf lead-lag-by-regime.pdf \
  spread-distribution.pdf forecast-benchmarks.pdf; do
  [[ -f assets/figures/$fig ]] && cp "assets/figures/$fig" "$OUTDIR/assets/figures/"
done

# arXiv manifest
cat > "$OUTDIR/00README.txt" <<'EOF'
openmarket-paper arXiv source bundle
====================================
Build locally:
  pdflatex main.tex && bibtex main && pdflatex main.tex && pdflatex main.tex
Or:
  latexmk -pdf main.tex

Main file: main.tex
Figures:   assets/figures/*.pdf
EOF

tar -czf "$TARBALL" -C "$OUTDIR" .
echo "arxiv tarball: $TARBALL"
echo "contents:"
tar -tzf "$TARBALL" | head -30
