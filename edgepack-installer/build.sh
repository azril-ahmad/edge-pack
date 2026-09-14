#!/usr/bin/env bash
# build.sh — Build the edgepack-installer standalone executable
#
# Run this on Ubuntu 24.04 (Noble) to ensure the binary is compatible with
# target Ubuntu 24.04 / 26.04 machines.
#
# Usage:
#   chmod +x build.sh
#   ./build.sh
#
# Output:  dist/edgepack-installer  (single self-contained executable)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "==> Checking build environment..."
python3 --version
pip3 --version

if ! command -v objdump >/dev/null 2>&1; then
  echo "ERROR: 'objdump' not found. PyInstaller needs it to analyze shared library dependencies on Linux." >&2
  echo "        Install with: sudo apt-get install -y binutils" >&2
  exit 1
fi

echo "==> Installing / upgrading dependencies..."
pip3 install --upgrade pip
pip3 install --upgrade -r requirements.txt
pip3 install --upgrade pyinstaller

echo "==> Building edgepack-installer..."
# PYTHONOPTIMIZE=2 (-OO) strips docstrings and assert statements from the
# compiled bytecode that gets frozen into the binary.
PYTHONOPTIMIZE=2 pyinstaller edgepack-installer.spec --clean --noconfirm

echo "==> Verifying no GPL-licensed libraries were bundled..."
if find dist/ -iname 'libreadline*' | grep -q .; then
  echo "ERROR: libreadline (GPL) was bundled into the distributable. Refusing to ship." >&2
  find dist/ -iname 'libreadline*' >&2
  exit 1
fi
echo "    OK — no GPL-licensed libraries found."

echo "==> Generating SHA-256 checksum..."
(
  cd dist
  sha256sum edgepack-installer > edgepack-installer.sha256
)
echo "    OK — checksum written to dist/edgepack-installer.sha256"

echo ""
echo "==> Build complete."
echo "    Executable: dist/edgepack-installer"
echo "    Checksum:   dist/edgepack-installer.sha256"
echo ""
echo "    Run:        ./dist/edgepack-installer"
echo "    CLI install: ./dist/edgepack-installer install <base-profile> [addon ...]"
echo "    List profiles: ./dist/edgepack-installer list"
echo "    Verify:     (cd dist && sha256sum -c edgepack-installer.sha256)"
