# tui/__main__.py — allows running the TUI with: python3 -m tui
#
# SPDX-License-Identifier: GPL-3.0-or-later

# Relative import works when run as a package (python3 -m tui).
# Falls back to absolute import when run as a frozen PyInstaller executable.
try:
    from .main import main
except ImportError:
    from tui.main import main

main()
