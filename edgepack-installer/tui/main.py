# tui/main.py — entry point for the EdgePack TUI
#
# Run from the ui/ directory:  python -m tui
#
# SPDX-License-Identifier: GPL-3.0-or-later

import argparse
import os
import sys

# Ensure ui/ (the parent of both tui/ and edgepack_shared/) is on sys.path
# when this module is executed directly, so edgepack_shared is importable.
_UI_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _UI_DIR not in sys.path:
    sys.path.insert(0, _UI_DIR)


def main():
    parser = argparse.ArgumentParser(description="Intel EdgePack TUI Installer")
    parser.add_argument(
        "--debug-level",
        type=str,
        choices=["NOTSET", "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL", "MUTE"],
        default="MUTE",
        help=argparse.SUPPRESS,  # dev-only flag, hidden from customer help output
    )
    # Debug level 0 : show all events
    # Debug level 1 : show debug level and higher level events (correspond to level 10 in )
    # Debug level 2 : show info and higher level level events (20)

    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser(
        "list", help="List available base profiles and add-on profiles, then exit",
    )

    install_parser = subparsers.add_parser(
        "install",
        help="Install a base profile (+ optional add-ons) without the TUI wizard",
    )
    install_parser.add_argument(
        "base_profile", help="Base profile key to install, e.g. base-standard (see 'list')",
    )
    install_parser.add_argument(
        "addons", nargs="*", help="Optional add-on profile keys to install alongside the base profile",
    )

    args = parser.parse_args()

    # Debug mode is disabled by default (MUTE). To enable it, the caller must
    # explicitly set EDGEPACK_DEBUG=1 — prevents accidental or accidental-by-path
    # information leakage from debug-level log output.
    if args.debug_level != "MUTE" and os.environ.get("EDGEPACK_DEBUG") != "1":
        print(
            "error: debug mode requires EDGEPACK_DEBUG=1 environment variable",
            file=sys.stderr,
        )
        sys.exit(1)

    # Global handler: any unhandled exception below is reported as a single
    # generic line (no traceback/paths) instead of a raw Python crash dump.
    try:
        if args.command == "list":
            from edgepack_shared.processor import Processor
            from .cli import list_command
            list_command(Processor.load())
            return

        if args.command == "install":
            from .cli import install_command
            sys.exit(install_command(args))

        # Deferred: textual (and its dependencies) is only required for the TUI itself,
        # not for the 'list'/'install' CLI paths above — keeps those usable under sudo,
        # which drops the invoking user's site-packages (no user-local textual install).
        from .app import EdgePackTUI
        EdgePackTUI(debug_level=args.debug_level).run()
    except SystemExit:
        raise
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception:
        # Log the full traceback internally (with paths and stack) but show the
        # user a generic message to avoid leaking internal paths or module names.
        import logging
        logging.getLogger("tui_log").exception("Unexpected error during installer run")
        print(
            "error: edgepack-installer encountered an unexpected error — "
            "check logs for details",
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
