openmarket-paper arXiv source bundle
====================================
Build locally:
  pdflatex main.tex && bibtex main && pdflatex main.tex && pdflatex main.tex
Or:
  latexmk -pdf main.tex

Main file: main.tex
Figures:   assets/figures/*.pdf
