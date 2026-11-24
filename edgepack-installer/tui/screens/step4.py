# tui/screens/step4.py — Step 4: apt installation with live progress and log
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

import datetime
import itertools
import logging
import os
import shutil
# Reviewed: list-form args only (no shell=True); the `sh -c` scripts are built in install_logic.
import subprocess  # nosec B404
import sys
import threading
import time
from pathlib import Path

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.screen import ModalScreen

from .base import BaseWizardScreen, _ButtonNavMixin
from textual.widgets import Button, Footer, Header, Input, ProgressBar, RichLog, Rule, Static

from ..package_logic import (
    collect_selected_packages, required_debconf_selections, required_prerequisites,
    split_base_and_addons,
)
from edgepack_shared.package_status import STATE_CURRENT
from edgepack_shared.install_logic import run_install, allow_insecure_repos
from edgepack_shared.log import rotate_logs, open_secure_log as _open_secure_log, LOG_GROUP
from edgepack_shared.package_report import (
    apt_cache_policy_origins as _apt_cache_policy_origins,
    apt_dependency_closure as _apt_dependency_closure,
    parse_get_line as _parse_get_line,
    parse_resolved_line as _parse_resolved_line,
    write_package_summary,
)

LOG_DIR = "/var/log/edgepack"


def _candidate_log_paths(filename: str) -> list[str]:
    """Return the ordered [/var/log/edgepack/, XDG data dir] paths to try for *filename*."""
    return [
        os.path.join(LOG_DIR, filename),
        os.path.join(
            os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"),
            "edgepack", filename,
        ),
    ]


def _ensure_log_dir(sudo_password: str | None) -> bool:
    """Create /var/log/edgepack/ with elevated privileges if it does not exist yet.

    Mirrors the same auth strategy used by run_install: pkexec inside a
    Flatpak sandbox, sudo -S when a password is available, sudo -n otherwise.
    Returns True on success; False if the directory could not be created —
    callers should fall back gracefully but must log the failure so the
    user and operator are aware that persistent logging is unavailable.
    """
    log_dir = LOG_DIR
    if Path(log_dir).is_dir() and os.access(log_dir, os.W_OK):
        return True
    try:
        # root:adm 0750 (see edgepack_shared.log.secure_log_path) so every
        # administrator can read the logs, falling back to root-only where
        # there is no 'adm' group.
        script = (
            f"mkdir -p {log_dir} || exit 1; "
            f"if chgrp {LOG_GROUP} {log_dir} 2>/dev/null; then "
            f"chmod 0750 {log_dir}; else chmod 0700 {log_dir}; fi"
        )
        if shutil.which("flatpak-spawn") or os.path.exists("/usr/bin/flatpak-spawn"):
            # flatpak-spawn is resolved via PATH on purpose: its location is set by the sandbox.
            result = subprocess.run(  # nosec B603 B607
                ["flatpak-spawn", "--host", "pkexec", "sh", "-c", script],
                capture_output=True, timeout=10,
            )
        elif sudo_password is not None:
            result = subprocess.run(  # nosec B603
                ["/usr/bin/sudo", "-S", "-p", "", "/bin/sh", "-c", script],
                input=sudo_password + "\n",
                capture_output=True, text=True, timeout=10,
            )
        else:
            result = subprocess.run(  # nosec B603
                ["/usr/bin/sudo", "-n", "/bin/sh", "-c", script],
                capture_output=True, timeout=10,
            )
        if result.returncode != 0:
            logging.getLogger("tui_log").warning(
                "_ensure_log_dir: elevated command exited %d — persistent logging unavailable",
                result.returncode,
            )
            return False
        return True
    except Exception as e:
        logging.getLogger("tui_log").warning(
            "_ensure_log_dir: exception during directory creation (%s) — persistent logging unavailable",
            type(e).__name__,
        )
        return False


# ---------------------------------------------------------------------------
# In-TUI sudo authentication modal
# ---------------------------------------------------------------------------

class SudoAuthScreen(_ButtonNavMixin, ModalScreen):
    """Modal password dialog — collects sudo credentials without leaving the TUI.

    The password is pre-validated with ``sudo -S true`` in a background thread
    before the modal dismisses.  Wrong passwords show an inline error and
    re-focus the input for another attempt.  The user may retry as many times
    as needed and must press Cancel to abort.

    Dismissed with the verified password string on success, or None on cancel.
    """

    BINDINGS = [
        Binding("escape", "cancel",            "Cancel", show=False),
        Binding("space",  "press_focused",     "",       show=False),
        Binding("left",   "focus_prev_button", "",       show=False),
        Binding("right",  "focus_next_button", "",       show=False),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="auth-dialog"):
            yield Static("🔒  Authentication Required", id="auth-title", markup=False)
            yield Rule()
            yield Static(
                "A sudo password is required to install packages.",
                id="auth-msg",
            )
            yield Static("", id="auth-error")  # populated on wrong password
            yield Input(
                password=True,
                id="auth-password",
                placeholder="sudo password",
            )
            with Horizontal(id="auth-actions"):
                yield Button("Cancel", id="btn-auth-cancel")
                yield Button("Authenticate →", variant="success", id="btn-auth-confirm")

    def on_mount(self) -> None:
        self.query_one("#auth-password", Input).focus()

    # ------------------------------------------------------------------
    # Password submission & validation
    # ------------------------------------------------------------------

    def _submit(self, password: str) -> None:
        """Validate *password* via ``sudo -S true`` in a background thread."""
        if not password:
            self.query_one("#auth-error", Static).update("Please enter a password.")
            self.query_one("#auth-password", Input).focus()
            return

        # Show a "Verifying…" state so the user knows something is happening.
        confirm = self.query_one("#btn-auth-confirm", Button)
        confirm.disabled = True
        confirm.label = "Verifying…"
        self.query_one("#auth-error", Static).update("")

        def _validate() -> None:
            try:
                result = subprocess.run(  # nosec B603
                    ["/usr/bin/sudo", "-S", "-p", "", "true"],
                    input=password + "\n",
                    capture_output=True,
                    text=True,
                )
            except Exception as e:
                logging.getLogger("tui_log").warning("sudo validation failed to run: %s", e)
                self.app.call_from_thread(
                    self._on_wrong_password, "Could not run sudo — see log for details."
                )
                return
            if result.returncode == 0:
                self.app.call_from_thread(self.dismiss, password)
            else:
                self.app.call_from_thread(self._on_wrong_password)

        threading.Thread(target=_validate, daemon=True).start()

    def _on_wrong_password(self, message: str = "Incorrect password, please try again.") -> None:
        confirm = self.query_one("#btn-auth-confirm", Button)
        confirm.disabled = False
        confirm.label = "Authenticate →"
        self.query_one("#auth-error", Static).update(message)
        self.query_one("#auth-password", Input).clear()
        self.query_one("#auth-password", Input).focus()

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Input.Submitted, "#auth-password")
    def on_submitted(self, event: Input.Submitted) -> None:
        self._submit(event.value)

    @on(Button.Pressed, "#btn-auth-confirm")
    def on_confirm(self) -> None:
        self._submit(self.query_one("#auth-password", Input).value)

    @on(Button.Pressed, "#btn-auth-cancel")
    def on_cancel_btn(self) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------------------
# Cancel-installation confirmation modal
# ---------------------------------------------------------------------------

class CancelInstallScreen(_ButtonNavMixin, ModalScreen):
    """Confirmation modal for aborting an install that is still downloading.

    Only ever reachable before dpkg starts, so the wording can promise that
    the system is untouched. Dismissed with True to abort, False to continue.
    """

    BINDINGS = [
        Binding("escape", "keep_installing",   "Keep Installing", show=False),
        Binding("space",  "press_focused",     "",                show=False),
        Binding("left",   "focus_prev_button", "",                show=False),
        Binding("right",  "focus_next_button", "",                show=False),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="cancel-dialog"):
            yield Static("⚠  Cancel Installation?", id="cancel-title", markup=False)
            yield Rule()
            yield Static(
                "apt is still downloading packages — nothing has been\n"
                "installed yet, so stopping now is safe.\n\n"
                "You can start the installation again afterwards.",
                id="cancel-msg",
            )
            with Horizontal(id="cancel-actions"):
                yield Button("← Keep Installing", id="btn-keep-installing")
                yield Button("✕  Stop Installation", variant="error", id="btn-confirm-cancel")

    def on_mount(self) -> None:
        self.set_timer(0.05, lambda: self.query_one("#btn-keep-installing", Button).focus())

    def action_keep_installing(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed, "#btn-keep-installing")
    def on_keep(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed, "#btn-confirm-cancel")
    def on_confirm(self) -> None:
        self.dismiss(True)


# ---------------------------------------------------------------------------
# Installation failure modal
# ---------------------------------------------------------------------------

class InstallFailedScreen(ModalScreen):
    """Modal shown when apt exits with a non-zero status code."""

    BINDINGS = [Binding("space", "exit_app", "", show=False)]

    def __init__(self, exit_code: int, log_path: str | None) -> None:
        super().__init__()
        self._exit_code = exit_code
        self._log_path = log_path

    def compose(self) -> ComposeResult:
        with Vertical(id="error-dialog"):
            yield Static("✗  Installation Failed", id="error-title", markup=False)
            yield Rule()
            yield Static(
                f"Installation failed (exit code: {self._exit_code}).\n\n"
                "Possible causes:\n"
                "  \u2022 Network or repository access problem\n"
                "  \u2022 Package not found in the repository\n"
                "  \u2022 Package conflicts on this system\n"
                "  \u2022 Insufficient disk space\n\n"
                + (f"Full log saved to:\n  {self._log_path}" if self._log_path else "No log file was saved."),
                id="error-msg",
            )
            with Horizontal(id="error-actions"):
                yield Button("Exit Installer", variant="error", id="btn-exit-error")

    def on_mount(self) -> None:
        self.set_timer(0.05, lambda: self.query_one("#btn-exit-error", Button).focus())

    def action_exit_app(self) -> None:
        self.app.exit()

    @on(Button.Pressed, "#btn-exit-error")
    def on_exit_error(self) -> None:
        self.app.exit()


# ---------------------------------------------------------------------------
# Post-install restart prompt (modal overlay)
# ---------------------------------------------------------------------------

class RestartPromptScreen(_ButtonNavMixin, ModalScreen):
    """Modal shown after a successful install — offer restart or skip."""

    BINDINGS = [
        Binding("escape", "skip",             "Skip",    show=False),
        Binding("space",  "press_focused",    "",        show=False),
        Binding("left",   "focus_prev_button","",        show=False),
        Binding("right",  "focus_next_button","",        show=False),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="restart-dialog"):
            yield Static("✓  Installation Complete", id="restart-title", markup=False)
            yield Rule()
            yield Static(
                "A system restart is recommended to apply\n"
                "all changes from the installed packages.",
                id="restart-msg",
            )
            with Horizontal(id="restart-actions"):
                yield Button("↺  Restart Now", variant="warning", id="btn-restart")
                yield Button("Skip — Close Installer", id="btn-skip")

    def on_mount(self) -> None:
        self.set_timer(0.05, lambda: self.query_one("#btn-restart", Button).focus())

    def action_skip(self) -> None:
        self.app.exit()

    @on(Button.Pressed, "#btn-skip")
    def on_skip(self) -> None:
        self.app.exit()

    @on(Button.Pressed, "#btn-restart")
    def on_restart(self) -> None:
        # If the sudo cache from the install is still valid we can reboot
        # immediately without asking for a password again.
        try:
            cache_valid = subprocess.run(  # nosec B603
                ["/usr/bin/sudo", "-n", "true"],
                capture_output=True,
                timeout=10,
            ).returncode == 0
        except Exception as e:
            logging.getLogger("tui_log").warning("sudo cache check failed to run: %s", e)
            cache_valid = False  # fail closed — fall through to the password prompt

        if cache_valid:
            self._do_reboot(password=None, use_cache=True)
        else:
            self.app.push_screen(SudoAuthScreen(), self._do_reboot)

    def _do_reboot(self, password: str | None, use_cache: bool = False) -> None:
        """Run reboot via sudo. Uses cached credentials or an explicit password."""
        if not use_cache and password is None:
            return  # user cancelled the password dialog

        _REBOOT_DELAY = 4  # seconds between TUI exit and reboot

        def _run() -> None:
            # Close the TUI first so the terminal is restored before we print.
            self.app.call_from_thread(self.app.exit)
            # Give Textual a moment to finish its cleanup / restore the terminal.
            time.sleep(0.5)

            for remaining in range(_REBOOT_DELAY, 0, -1):
                sys.stdout.write(f"\rRebooting in {remaining} second{'s' if remaining != 1 else ''}...  ")
                sys.stdout.flush()
                time.sleep(1)
            sys.stdout.write("\rRebooting now...                \n")
            sys.stdout.flush()

            for base_cmd in (["/usr/bin/sudo", "systemctl", "reboot", "-i"], ["/usr/bin/sudo", "reboot"]):
                try:
                    if use_cache:
                        cmd = base_cmd
                        inp = None
                    else:
                        cmd = ["/usr/bin/sudo", "-S", "-p", ""] + base_cmd[1:]
                        inp = password + "\n"
                    result = subprocess.run(  # nosec B603
                        cmd,
                        input=inp,
                        capture_output=True,
                        text=True,
                        timeout=15,
                    )
                    if result.returncode == 0:
                        return
                except (OSError, subprocess.SubprocessError) as exc:
                    logging.getLogger("tui_log").warning("Reboot attempt failed: %s", exc)
                    continue
            sys.stdout.write("Reboot failed. Please restart your system manually.\n")
            sys.stdout.flush()

        # Non-daemon so the thread outlives the Textual app and the reboot runs.
        threading.Thread(target=_run, daemon=False).start()


_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def _pkg_id(name: str) -> str:
    """Sanitize a package name for use as a Textual widget ID."""
    return "pkg-row-" + name.replace(".", "-").replace("+", "-")


class Step4Screen(_ButtonNavMixin, BaseWizardScreen):
    """Step 4 — runs the apt installation and streams its output."""

    BINDINGS = [
        Binding("f1",     "app.show_about",    "About",   show=True),
        Binding("tab",    "focus_next_smart",  "Next",    show=True),
        Binding("enter",  "press_focused",     "Confirm", show=False),
        Binding("space",  "press_focused",     "Select",   show=True,  priority=True),
        Binding("left",   "focus_prev_button", "",        show=False),
        Binding("right",  "focus_next_button", "",        show=False),
        Binding("ctrl+q", "app.quit",           "Quit",   show=True),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._install_running = False
        self._install_complete = False
        self._install_failed_code: int | None = None
        self._install_handle = None   # InstallHandle for the running install
        self._cancel_requested = False
        # Latched once dpkg starts writing to the system; from that point on
        # cancelling could corrupt the package database, so it is disallowed.
        self._point_of_no_return = False
        self._spinner = itertools.cycle(_SPINNER)
        self._active_pkg: str | None = None
        self._pkg_names: list[str] = []
        self._log_file = None         # opened when install starts
        self._actual_log_path: str | None = None
        self._log_failure_reported = False
        self._install_ts: str | None = None
        self._pkg_get_origins: dict[str, tuple[str, str]] = {}
        self._dl_count = 0             # "Get:" lines seen in the current download phase
        self._in_install_phase = False
        # Full expected package roster (prereq names known upfront + main
        # names learned from the "Resolved:" line) — diffed against
        # _pkg_get_origins at the end to find cached packages with no "Get:".
        self._prereq_pkg_names: set[str] = set()
        self._resolved_pkg_names: set[str] = set()
        # Pre-auth via app.suspend()+sudo -v is needed unless we are already
        # root or running inside a Flatpak (where pkexec is used instead).
        # Deliberately NOT based on $DISPLAY: even with X11 forwarding active
        # there may be no graphical polkit agent, causing pkexec to fall back to
        # a text-mode prompt that writes directly to the TTY and corrupts the TUI.
        _in_flatpak = bool(
            shutil.which("flatpak-spawn") or os.path.exists("/usr/bin/flatpak-spawn")
        )
        self._needs_sudo: bool = not _in_flatpak and os.getuid() != 0

    # ------------------------------------------------------------------
    # Compose
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header()

        with ScrollableContainer(id="step4-outer"):
            yield Static("Step 4 of 4 — Installing", id="step4-title")
            yield Rule()

            # Package status list — populated in on_mount
            yield Static("Packages to be installed:", id="pkg-list-heading")
            yield ScrollableContainer(id="pkg-status-list")

            # Progress bar + status label (hidden until install starts)
            yield ProgressBar(total=100, id="install-progress", show_eta=False)
            yield Static("", id="install-status-label")

            yield Rule()

            # Live apt output
            yield RichLog(id="install-log", highlight=True, markup=True, wrap=True)

        yield Rule()

        with Horizontal(id="step4-actions"):
            yield Button("← Back", id="btn-back")
            yield Button("✕ Cancel", variant="error", id="btn-cancel-install")
            yield Button("▶ Install Now", variant="success", id="btn-install")

        yield Footer()

    # ------------------------------------------------------------------
    # Mount
    # ------------------------------------------------------------------

    def on_mount(self) -> None:
        app = self.app  # type: ignore[attr-defined]
        base_pkgs, addon_pkgs = split_base_and_addons(app.pkg_entries)
        status = getattr(app, "package_status", {}) or {}
        # A dependency the user also picked as an add-on is listed under add-ons,
        # matching Step 3; mounting it twice would also collide on the row ID.
        selected = set(base_pkgs) | set(addon_pkgs)
        deps = [n for n in (getattr(app, "base_dependencies", None) or []) if n not in selected]

        pkg_list = self.query_one("#pkg-status-list", ScrollableContainer)
        self._size_package_list()
        groups: list[tuple[str, list[str]]] = [("Base profile", base_pkgs)]
        if deps:
            groups.append((f"Included with this profile ({len(deps)})", deps))
        if addon_pkgs:
            groups.append(("Optional add-ons", addon_pkgs))

        # Mirrors Step 3's grouping so the same packages appear on both screens.
        self._pkg_names = []
        seen_ids: set[str] = set()
        for title, names in groups:
            names = [n for n in names if _pkg_id(n) not in seen_ids]
            if not names:
                continue
            pkg_list.mount(Static(f"[dim]{title}[/dim]", markup=True, classes="pkg-group-header"))
            for name in names:
                seen_ids.add(_pkg_id(name))
                self._pkg_names.append(name)
                # Packages already at the candidate version never emit a dpkg
                # pmstatus line, so mark them now instead of leaving them pending.
                if (status.get(name) or {}).get("state") == STATE_CURRENT:
                    label = f"  [green]✓[/green]  {name}  [dim](already up to date)[/dim]"
                else:
                    label = f"  ○  {name}"
                pkg_list.mount(Static(
                    label, markup=True, id=_pkg_id(name), classes="pkg-status-row",
                ))

        # Hide progress widgets until install starts
        self.query_one("#install-progress").display = False
        self.query_one("#install-status-label").display = False

        self._set_cancel_visible(False)

        # Spinner timer — updates the active package icon at 10 fps
        self.set_interval(0.1, self._tick_spinner)

        # Default focus: Install Now button
        self.set_timer(0.05, lambda: self.query_one("#btn-install", Button).focus())

    def on_resize(self, event) -> None:
        self._size_package_list()

    def _size_package_list(self) -> None:
        self.query_one("#pkg-status-list", ScrollableContainer).styles.max_height = max(
            6, self.app.size.height - 30
        )

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_focus_next_smart(self) -> None:
        prev = self.focused
        self.app.action_focus_next()
        if (
            not isinstance(prev, Button)
            and isinstance(self.focused, Button)
            and self.focused.id == "btn-back"
        ):
            self.app.action_focus_next()

    def action_navigate_up(self) -> None:
        if not self._scroll_focused_panel(-1):
            super().action_navigate_up()

    def action_navigate_down(self) -> None:
        if not self._scroll_focused_panel(1):
            super().action_navigate_down()

    def _scroll_focused_panel(self, delta: int) -> bool:
        focused = self.focused
        if focused not in (
            self.query_one("#pkg-status-list", ScrollableContainer),
            self.query_one("#step4-outer", ScrollableContainer),
        ):
            return False
        offset = focused.scroll_offset.y
        if (delta > 0 and offset >= focused.max_scroll_y) or (delta < 0 and offset <= 0):
            return False
        focused.scroll_relative(y=delta, animate=False)
        return True

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    @on(Button.Pressed, "#btn-back")
    def on_back(self) -> None:
        if not self._install_running:
            self.app.pop_screen()

    @on(Button.Pressed, "#btn-install")
    def on_install(self) -> None:
        if self._install_running:
            return

        # After completion the button becomes "Next →" — open restart prompt
        if self._install_complete:
            if self._install_failed_code is not None:
                self.app.push_screen(InstallFailedScreen(self._install_failed_code, self._actual_log_path))
            else:
                self.app.push_screen(RestartPromptScreen())
            return

        app = self.app  # type: ignore[attr-defined]
        packages = collect_selected_packages(app.pkg_entries)

        if not packages:
            self._append_log("[yellow]No packages selected to install.[/yellow]")
            return

        if self._needs_sudo:
            # push_screen with a callback is the correct Textual pattern for
            # receiving a dismissed value without requiring a worker.
            self.app.push_screen(SudoAuthScreen(), self._on_auth_result)
        else:
            self._start_install(sudo_password=None)

    def _on_auth_result(self, password: str | None) -> None:
        """Callback invoked when SudoAuthScreen is dismissed."""
        if password is None:
            return  # user cancelled — do nothing
        self._start_install(sudo_password=password)

    @on(Button.Pressed, "#btn-cancel-install")
    def on_cancel_install(self) -> None:
        if self._cancellable:
            self.app.push_screen(CancelInstallScreen(), self._on_cancel_confirmed)

    def _on_cancel_confirmed(self, confirmed: bool | None) -> None:
        """Abort the install, unless dpkg started while the dialog was open."""
        if not confirmed:
            return
        if not self._cancellable:
            self._append_log(
                "[yellow]Too late to cancel — packages are already being "
                "written to the system.[/yellow]"
            )
            return
        # cancel() also returns False when dpkg started between this screen's
        # last update and now — the handle's flag is the authoritative one.
        handle = self._install_handle
        if handle is None:
            return
        if not handle.cancel():
            if handle.dpkg_started:
                self._enter_point_of_no_return()
                self._append_log(
                    "[yellow]Too late to cancel — packages are already being "
                    "written to the system.[/yellow]"
                )
            return

        self._cancel_requested = True
        self._set_cancel_visible(False)
        try:
            self.query_one("#install-status-label", Static).update(
                "[yellow]Cancelling installation…[/yellow]"
            )
        except Exception:
            # UI/widget may be gone after install completes — silently ignore
            pass  # nosec B110
        self._append_log("\n[yellow]Cancelling installation at user request…[/yellow]")

    @property
    def _cancellable(self) -> bool:
        """True while apt is running but has not yet handed over to dpkg."""
        return (
            self._install_running
            and not self._point_of_no_return
            and not self._cancel_requested
        )

    def _set_cancel_visible(self, visible: bool) -> None:
        """Show or hide the Cancel button, moving focus off it when it disappears."""
        try:
            btn = self.query_one("#btn-cancel-install", Button)
        except Exception:
            # UI/widget may be gone after install completes — silently ignore
            return
        if not visible and btn.has_focus:
            self.set_focus(None)
        # Disabled as well as hidden so arrow-key button navigation skips it.
        btn.disabled = not visible
        btn.display = visible

    def _enter_point_of_no_return(self) -> None:
        """Latch the non-cancellable phase once dpkg starts unpacking/configuring."""
        if self._point_of_no_return:
            return
        self._point_of_no_return = True
        self._set_cancel_visible(False)
        self._append_log(
            "[dim]Applying packages — cancelling is no longer possible.[/dim]"
        )

    def _focus_cancel_button(self) -> None:
        """Deferred focus target; the install may already have ended by now."""
        try:
            self.query_one("#btn-cancel-install", Button).focus()
        except Exception:
            # UI/widget may be gone after install completes — silently ignore
            pass  # nosec B110

    def _start_install(self, sudo_password: str | None) -> None:
        """Lock the UI and launch the background install thread."""
        app = self.app  # type: ignore[attr-defined]
        packages = collect_selected_packages(app.pkg_entries)

        self._install_running = True
        self._cancel_requested = False
        self._point_of_no_return = False
        self._install_failed_code = None
        self.query_one("#btn-install", Button).disabled = True
        self.query_one("#btn-back", Button).disabled = True
        self._set_cancel_visible(True)
        # Disabling Install/Back leaves focus stranded on a disabled widget.
        self.set_timer(0.05, self._focus_cancel_button)

        # Show progress widgets — indeterminate (no % is meaningful) until
        # dpkg starts and _on_progress switches it to a determinate bar.
        self._dl_count = 0
        bar = self.query_one("#install-progress", ProgressBar)
        bar.display = True
        bar.update(total=None)
        self.query_one("#install-status-label").display = True
        self.query_one("#install-status-label", Static).update("Resolving and downloading packages…")

        self.query_one("#install-log", RichLog).clear()
        self._append_log("Starting installation…\n")

        repos = list((app.detected_os_entry or {}).get("repositories", {}).values())
        selected_profiles = [app.selected_profile, *app.selected_addon_profiles]
        prerequisites = required_prerequisites(
            app.processor,
            selected_profiles,
            app.detected_os_key,
        )
        debconf_selections = required_debconf_selections(app.processor, selected_profiles)
        hidden_packages: list[str] = []
        for _key in selected_profiles:
            if _key:
                hidden_packages.extend(
                    app.processor.hidden_os_packages(_key, app.detected_platform_key, app.detected_os_key)
                )

        # Ensure /var/log/edgepack/ is writable (requires root on first run).
        _log_dir_ok = _ensure_log_dir(sudo_password)
        if not _log_dir_ok:
            self._append_log(
                "[yellow]Warning: could not create persistent log directory; "
                "trying the fallback log directory.[/yellow]"
            )
        # Clean up stale log files before creating new ones.
        rotate_logs(LOG_DIR, max_age_days=30, max_count=10)

        # Open log file — fall back to XDG user dir if /var/log/edgepack/ is not yet writable
        _ts = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self._install_ts = _ts
        self._pkg_get_origins = {}
        self._in_install_phase = False
        self._prereq_pkg_names = {p.split("=")[0] for p in (prerequisites.get("packages") or [])}
        self._resolved_pkg_names = set()
        self._log_failure_reported = False
        self._log_file = None
        self._actual_log_path = None
        _log_filename = f"edgepack-installer-{_ts}.log"
        for _candidate in _candidate_log_paths(_log_filename):
            try:
                Path(_candidate).parent.mkdir(parents=True, exist_ok=True)
                log_file = _open_secure_log(_candidate, buffering=1)
            except OSError:
                continue
            self._log_file = log_file
            self._actual_log_path = _candidate
            break
        else:
            self._report_log_failure("Could not create a secure installation log")

        if self._log_file:
            try:
                self._log_file.write(
                    f"EdgePack Installer log\n"
                    f"Started: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                    f"Packages: {', '.join(self._pkg_names)}\n"
                    + "".join(
                        f"Preseed: {pkg} {question} {qtype} {value}\n"
                        for pkg, question, qtype, value in debconf_selections
                    )
                    + (
                        "WARNING: repository https enforcement disabled "
                        "(--allow-insecure-repos)\n"
                        if allow_insecure_repos() else ""
                    )
                    + f"{'=' * 60}\n"
                )
            except OSError as exc:
                self._disable_log(exc)

        kwargs = dict(
            output_callback=self._on_output,
            progress_callback=self._on_progress,
            finished_callback=self._on_finished,
            schedule=app.call_from_thread,
        )
        self._install_handle = run_install(
            sorted(packages.items()), repos, sudo_password=sudo_password,
            prerequisites=prerequisites, force_packages=hidden_packages,
            debconf_selections=debconf_selections, **kwargs
        )

    def _report_log_failure(self, error: object, kind: str = "installation log") -> None:
        if not self._log_failure_reported:
            self._log_failure_reported = True
            logging.getLogger("tui_log").warning("%s unavailable: %s", kind, error)
            self._append_log(f"[yellow]Warning: {kind} unavailable; output will not be saved.[/yellow]")

    def _disable_log(self, error: OSError) -> None:
        log_file, self._log_file = self._log_file, None
        if log_file:
            try:
                log_file.close()
            except OSError:
                # The original failure is reported below; a close error adds nothing.
                pass
        self._report_log_failure(error)

    # ------------------------------------------------------------------
    # Install callbacks  (invoked on Textual thread via call_from_thread)
    # ------------------------------------------------------------------

    def _on_output(self, line: str) -> None:
        """Append a regular apt output line to the log widget and log file."""
        stripped = line.strip()
        cleaned = line.rstrip("\n")
        self._append_log(cleaned)
        if self._log_file:
            try:
                self._log_file.write(line if line.endswith("\n") else line + "\n")
            except OSError as exc:
                self._disable_log(exc)
        # "Running: apt-get update" — echoed once before every apt-get update
        # call (prerequisite stage and main stage). Turns OFF Get:-line
        # tracking so index-file fetches (Packages/Translation-en/etc, which
        # can otherwise look like a real package line) are never captured.
        # "Running: apt-get ... install ..." — echoed once before every
        # apt-get install call (prerequisite stage and main stage). Turns ON
        # tracking so that stage's package fetches are captured.
        if stripped.startswith("Running: apt-get update"):
            self._in_install_phase = False
        elif stripped.startswith("Running: apt-get"):
            self._in_install_phase = True
        elif self._in_install_phase:
            parsed = _parse_get_line(stripped)
            if parsed:
                pkg, version, url = parsed
                self._pkg_get_origins[pkg] = (version, url)
                self._dl_count += 1
                # Buffered "Get:" lines keep arriving during the cancel grace
                # window; they must not overwrite the cancelling notice.
                if not self._point_of_no_return and not self._cancel_requested:
                    try:
                        self.query_one("#install-status-label", Static).update(
                            f"Downloading packages… ({self._dl_count} fetched)"
                        )
                    except Exception:
                        # UI/widget may be gone after install completes — silently ignore
                        pass  # nosec B110
        if stripped.startswith("Resolved:"):
            self._resolved_pkg_names |= _parse_resolved_line(stripped)
        # The reader thread latches this before scheduling us, so by now it
        # already reflects any dpkg activity in this very line.
        handle = self._install_handle
        if not self._point_of_no_return and handle is not None and handle.dpkg_started:
            self._enter_point_of_no_return()
        # "Setting up pkg (version) ..." — treat as active package
        if stripped.startswith("Setting up "):
            pkg = stripped[len("Setting up "):].split()[0].split(":")[0]
            self._mark_active(pkg)

    def _on_progress(self, percent: float, pkg: str, msg: str) -> None:
        """Handle a pmstatus progress update from APT::Status-Fd."""
        # pmstatus lines are only emitted once apt hands the transaction to
        # dpkg (dlstatus covers the download phase and is discarded upstream).
        was_downloading = not self._point_of_no_return
        self._enter_point_of_no_return()
        pkg = pkg.split(":")[0]  # strip :amd64 architecture suffix
        try:
            bar = self.query_one("#install-progress", ProgressBar)
            if was_downloading:
                # First pmstatus line: leave the indeterminate download phase
                # and switch to a real, determinate dpkg-driven percentage.
                bar.update(total=100, progress=percent)
            else:
                bar.update(progress=percent)
            self.query_one("#install-status-label", Static).update(msg)
        except Exception:
            # UI/widget may be gone after install completes — silently ignore
            pass  # nosec B110
        if percent >= 99.9:
            self._mark_done(pkg)
        else:
            self._mark_active(pkg)

    def _on_finished(self, exit_code: int = 0) -> None:
        """Called when apt exits. Branches on exit_code for success vs failure."""
        self._install_running = False
        self._install_complete = True
        self._active_pkg = None
        self._install_handle = None
        self._set_cancel_visible(False)

        # Close log file
        if self._log_file:
            log_file, self._log_file = self._log_file, None
            try:
                log_file.write(
                    f"{'=' * 60}\n"
                    + ("Cancelled by user.\n" if self._cancel_requested else "")
                    + f"Finished: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} "
                    f"(exit code: {exit_code})\n"
                )
            except OSError as exc:
                self._report_log_failure(exc)
            finally:
                try:
                    log_file.close()
                except OSError as exc:
                    self._report_log_failure(exc)

        if self._cancel_requested:
            self._finish_cancelled_ui()
        elif exit_code != 0:
            # Mark the active/in-progress package as failed; leave others as-is
            for name in self._pkg_names:
                try:
                    widget = self.query_one(f"#{_pkg_id(name)}", Static)
                    text = widget.renderable if hasattr(widget, 'renderable') else ""
                    # Only mark packages that weren't already ticked as done
                    if "\u2713" not in str(text):
                        widget.update(f"  [red]\u2717[/red]  {name}")
                except Exception:
                    # UI/widget may be gone after install completes — silently ignore
                    pass  # nosec B110
            try:
                self.query_one("#install-progress", ProgressBar).update(total=100, progress=0)
                self.query_one("#install-status-label", Static).update(
                    "[bold red]Installation failed[/bold red]"
                )
            except Exception:
                # UI/widget may be gone after install completes — silently ignore
                pass  # nosec B110
            self._append_log(f"\n[bold red]Installation failed (exit code: {exit_code}).[/bold red]")
            self._install_failed_code = exit_code
            try:
                btn = self.query_one("#btn-install", Button)
                btn.label = "Next \u2192"
                btn.variant = "error"
                btn.disabled = False
                self.set_timer(0.1, lambda: btn.focus())
            except Exception:
                # UI/widget may be gone after install completes — silently ignore
                pass  # nosec B110
        else:
            for name in self._pkg_names:
                self._mark_done(name)
            try:
                self.query_one("#install-progress", ProgressBar).update(total=100, progress=100)
                self.query_one("#install-status-label", Static).update(
                    "[bold green]Checking installed packages\u2026[/bold green]"
                )
            except Exception:
                # UI/widget may be gone after install completes — silently ignore
                pass  # nosec B110
            self._append_log("\n[bold green]Installation complete.[/bold green]")
            if self._actual_log_path:
                self._append_log(f"[dim]Full installation log saved to: {self._actual_log_path}[/dim]")
            # Keep the Next button disabled until the package summary log is
            # written — the lookup is a single batched, unprivileged
            # apt-cache call so this only adds a brief delay.
            threading.Thread(target=self._collect_and_write_package_summary, daemon=True).start()

    def _collect_and_write_package_summary(self) -> None:
        """Background-thread step: resolve cached packages' origins, then write the summary.

        Runs off the UI thread since "apt-cache policy"/"apt-cache depends"
        are subprocess calls; _write_package_summary_log() is then scheduled
        back onto the UI thread (it touches the RichLog widget).
        """
        expected = self._prereq_pkg_names | self._resolved_pkg_names
        expected |= _apt_dependency_closure(expected)
        leftover = expected - set(self._pkg_get_origins)
        cached_origins = _apt_cache_policy_origins(leftover)
        self.app.call_from_thread(self._write_package_summary_log, cached_origins)
        self.app.call_from_thread(self._finish_success_ui)

    def _write_package_summary_log(self, cached_origins: dict[str, tuple[str, str]] | None = None) -> None:
        """Write the categorized Intel/Ubuntu/Unknown package summary log.

        Merges packages seen via apt's "Get:" fetch lines (freshly
        downloaded) with *cached_origins* — packages already present in
        apt's local archive cache, resolved via a separate "apt-cache
        policy" lookup since they never produce a "Get:" line. No-op if
        nothing was found either way (e.g. mock installs).
        """
        _filename = f"edgepack-packages-{self._install_ts or 'unknown'}.log"
        try:
            path = write_package_summary(
                _candidate_log_paths(_filename), self._pkg_get_origins, cached_origins,
            )
        except OSError:
            self._report_log_failure("Could not save the Software BOM", kind="Software BOM")
            return
        if path:
            self._append_log(f"[dim]Full Software BOM saved to: {path}[/dim]")

    def _finish_cancelled_ui(self) -> None:
        """Reset the screen to its pre-install state so the user can retry."""
        self._install_complete = False
        self._install_failed_code = None
        for name in self._pkg_names:
            try:
                self.query_one(f"#{_pkg_id(name)}", Static).update(f"  ○  {name}")
            except Exception:
                # UI/widget may be gone after install completes — silently ignore
                pass  # nosec B110
        try:
            self.query_one("#install-progress", ProgressBar).update(total=100, progress=0)
            self.query_one("#install-status-label", Static).update(
                "[bold yellow]Installation cancelled[/bold yellow]"
            )
        except Exception:
            # UI/widget may be gone after install completes — silently ignore
            pass  # nosec B110
        self._append_log(
            "\n[bold yellow]Installation cancelled. No packages were installed.[/bold yellow]"
        )
        try:
            self.query_one("#btn-back", Button).disabled = False
            btn = self.query_one("#btn-install", Button)
            btn.label = "▶ Install Now"
            btn.variant = "success"
            btn.disabled = False
            self.set_timer(0.1, lambda: btn.focus())
        except Exception:
            # UI/widget may be gone after install completes — silently ignore
            pass  # nosec B110

    def _finish_success_ui(self) -> None:
        """Complete the success UI transition once the package summary log is done."""
        try:
            self.query_one("#install-status-label", Static).update(
                "[bold green]Installation complete[/bold green]"
            )
        except Exception:
            # UI/widget may be gone after install completes — silently ignore
            pass  # nosec B110
        try:
            btn = self.query_one("#btn-install", Button)
            btn.label = "Next \u2192"
            btn.variant = "success"
            btn.disabled = False
            self.set_timer(0.1, lambda: btn.focus())
        except Exception:
            # UI/widget may be gone after install completes — silently ignore
            pass  # nosec B110

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _tick_spinner(self) -> None:
        """Advance the spinner icon on the currently active package row."""
        if self._active_pkg is None or self._install_complete:
            return
        frame = next(self._spinner)
        try:
            self.query_one(f"#{_pkg_id(self._active_pkg)}", Static).update(
                f"  {frame}  {self._active_pkg}"
            )
        except Exception:
            # UI/widget may be gone after install completes — silently ignore
            pass  # nosec B110

    def _mark_active(self, pkg_name: str) -> None:
        """Mark a package as currently being installed (spinner)."""
        if pkg_name not in self._pkg_names:
            return
        if self._active_pkg and self._active_pkg != pkg_name:
            self._mark_done(self._active_pkg)
        self._active_pkg = pkg_name

    def _mark_done(self, pkg_name: str) -> None:
        """Mark a package as successfully installed (green ✓)."""
        if pkg_name not in self._pkg_names:
            return
        if self._active_pkg == pkg_name:
            self._active_pkg = None
        try:
            self.query_one(f"#{_pkg_id(pkg_name)}", Static).update(
                f"  [green]✓[/green]  {pkg_name}"
            )
        except Exception:
            # UI/widget may be gone after install completes — silently ignore
            pass  # nosec B110

    def _append_log(self, text: str) -> None:
        try:
            self.query_one("#install-log", RichLog).write(text)
        except Exception:
            # UI/widget may be gone after install completes — silently ignore
            pass  # nosec B110

