# tui/main.py — entry point for the EdgePack TUI
#
# Run from the ui/ directory:  python -m tui
#
# SPDX-License-Identifier: MIT

import argparse
import os
import shutil
import sys

# Ensure ui/ (the parent of both tui/ and edgepack_shared/) is on sys.path
# when this module is executed directly, so edgepack_shared is importable.
_UI_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _UI_DIR not in sys.path:
    sys.path.insert(0, _UI_DIR)


def _in_flatpak() -> bool:
    return bool(shutil.which("flatpak-spawn") or os.path.exists("/usr/bin/flatpak-spawn"))


def _require_root(reason: str | None = None) -> None:
    """Abort unless the TUI was launched with root privileges.

    The wizard configures the EdgePack apt repositories and refreshes the
    package index while the user is still on Step 1, so privileges must be
    held from process start rather than collected at install time. Inside a
    Flatpak sandbox elevation goes through pkexec instead, so the check is
    skipped there.
    """
    if _in_flatpak() or os.geteuid() == 0:
        return
    detail = reason or (
        "Root access is required to configure the Intel EdgePack repositories,\n"
        "       check the installed and available package versions, and install packages."
    )
    print(
        f"error: edgepack-installer must be run as root.\n"
        f"       {detail}\n"
        f"\n"
        f"       Re-run with:  sudo ./edgepack-installer",
        file=sys.stderr,
    )
    sys.exit(1)


def _use_remote_template(url: str) -> None:
    """Download the template from *url* and make it replace the bundled one.

    Developer-only path behind the hidden --template-url flag. A failure is
    fatal rather than a silent fall back to the bundled template, so a typo in
    the URL can never be mistaken for a successful override.
    """
    from edgepack_shared.processor import fetch_template, set_template_path
    try:
        path = fetch_template(url)
    except Exception as e:
        print(f"error: could not use the template at {url}: {e}", file=sys.stderr)
        sys.exit(1)
    set_template_path(path)
    print(f"Using downloaded template: {path}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description="Intel EdgePack TUI Installer", allow_abbrev=False)
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

    parser.add_argument(
        "--template-url",
        metavar="URL",
        default=None,
        help=argparse.SUPPRESS,  # dev-only flag, hidden from customer help output
    )
    parser.add_argument(
        "--allow-insecure-repos",
        action="store_true",
        help=argparse.SUPPRESS,  # dev-only flag, hidden from customer help output
    )

    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser(
        "list", help="List available base profiles and add-on profiles, then exit",
    )

    install_parser = subparsers.add_parser(
        "install",
        help="Install a base profile (+ optional add-ons) without the TUI wizard",
        description="Install immediately after validation, without a confirmation prompt.",
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

    if args.template_url:
        # Gate the download itself: nothing is fetched from the network until
        # we know this run is privileged enough to use the result.
        _require_root("--template-url downloads a replacement template and requires root.")
        _use_remote_template(args.template_url)

    if args.allow_insecure_repos:
        from edgepack_shared.install_logic import set_allow_insecure_repos
        set_allow_insecure_repos(True)
        print(
            "warning: https enforcement for repository URLs is DISABLED for this run "
            "(--allow-insecure-repos)",
            file=sys.stderr,
        )

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
        _require_root()
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
