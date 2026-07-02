#!/usr/bin/env bash
# Fix interrupted MacTeX install (brew says "installed" but latexmk missing).
set -euo pipefail

PKG="/opt/homebrew/Caskroom/mactex-no-gui/2026.0324/mactex-20260324.pkg"
ALT_PKG="$(brew --prefix 2>/dev/null)/Caskroom/mactex-no-gui/2026.0324/mactex-20260324.pkg"

eval "$(/usr/libexec/path_helper)"

if command -v latexmk >/dev/null 2>&1; then
  echo "MacTeX already OK: $(which latexmk)"
  latexmk --version | head -1
  exit 0
fi

if [[ ! -f "$PKG" && -f "$ALT_PKG" ]]; then
  PKG="$ALT_PKG"
fi

if [[ ! -f "$PKG" ]]; then
  echo "MacTeX .pkg not found. Downloading via Homebrew..."
  brew install --cask mactex-no-gui
  PKG="/opt/homebrew/Caskroom/mactex-no-gui/2026.0324/mactex-20260324.pkg"
fi

echo "MacTeX package found but CLI tools are missing."
echo "Running the installer (requires your password; do NOT press Ctrl+C):"
echo "  sudo installer -pkg \"$PKG\" -target /"
echo ""

sudo installer -pkg "$PKG" -target /

eval "$(/usr/libexec/path_helper)"

if ! command -v latexmk >/dev/null 2>&1; then
  export PATH="/Library/TeX/texbin:$PATH"
fi

if command -v latexmk >/dev/null 2>&1; then
  echo ""
  echo "Success: $(which latexmk)"
  latexmk --version | head -1
  echo ""
  echo "Add to ~/.zshrc if needed:"
  echo '  eval "$(/usr/libexec/path_helper)"'
else
  echo "error: installer finished but latexmk still not found." >&2
  echo "Restart Terminal, then run: eval \"\$(/usr/libexec/path_helper)\"" >&2
  exit 1
fi