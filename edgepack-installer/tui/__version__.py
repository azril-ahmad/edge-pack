# tui/__version__.py — single source of truth for the TUI application version
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
# This is the only file that should be edited when bumping the TUI version.
# Everything else (tui_version in EdgePackTUI, the update-compatibility check
# in AutoUpdater, etc.) imports __version__ from here.
#
# Version scheme:  MAJOR.MINOR
#   MAJOR — incremented on breaking changes to the YAML manifest format
#   MINOR — incremented on feature/fix releases within the same manifest format

__version__ = "2026.2"
