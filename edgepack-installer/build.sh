#!/usr/bin/env bash
# build.sh — Build the edgepack-installer standalone executable
#
# Run this on Ubuntu 24.04 (Noble) to ensure the binary is compatible with
# target Ubuntu 24.04 / 26.04 machines.
#
# Usage:
#   chmod +x build.sh
#   ./build.sh --template prod|test
#
# Output:  dist/edgepack-installer  (both variants build to the same name)

set -euo pipefail

if [[ $# != 2 || $1 != --template || ( $2 != prod && $2 != test ) ]]; then
  echo "Usage: $0 --template prod|test" >&2
  exit 2
fi
variant="$2"
binary="edgepack-installer"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [[ $variant == prod ]]; then
  template=data/edgepacks-template.yml
else
  template=data/edgepacks-test-template.yml
fi
if [[ ! -f $template ]]; then
  echo "ERROR: missing $template" >&2
  exit 1
fi

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

echo "==> Building $binary with $template..."
# Bundle exactly one template under the filename Processor.load() expects.
stage_dir="$(mktemp -d)"
trap 'rm -rf -- "$stage_dir"' EXIT
cp "$template" "$stage_dir/edgepacks-template.yml"
# PYTHONOPTIMIZE=2 (-OO) strips docstrings and assert statements from the
# compiled bytecode that gets frozen into the binary.
EDGEPACK_TEMPLATE_PATH="$stage_dir/edgepacks-template.yml" \
EDGEPACK_BINARY_NAME="$binary" \
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
  sha256sum "$binary" > "$binary.sha256"
)
echo "    OK — checksum written to dist/$binary.sha256"

# Older builds produced suffixed names; a leftover one is easy to run by mistake.
for stale in edgepack-installer-prod edgepack-installer-test; do
  rm -f "dist/$stale" "dist/$stale.sha256"
done

echo ""
echo "==> Build complete."
echo "    Template:   $template ($variant)"
echo "    Executable: dist/$binary"
echo "    Checksum:   dist/$binary.sha256"
echo ""
echo "    Run:        ./dist/$binary"
echo "    CLI install: ./dist/$binary install <base-profile> [addon ...]"
echo "    List profiles: ./dist/$binary list"
echo "    Verify:     (cd dist && sha256sum -c $binary.sha256)"
