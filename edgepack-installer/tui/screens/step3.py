# tui/screens/step3.py — Step 3: installation summary with compatibility warnings
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

from rich.table import Table
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, ScrollableContainer, Vertical

from .base import BaseWizardScreen, _ButtonNavMixin
from textual.widgets import Button, Footer, Header, Label, Rule, Static

from edgepack_shared.package_status import (
    STATE_CURRENT, STATE_DOWNGRADE, STATE_NEW, STATE_UPGRADE,
)

from ..package_logic import split_base_and_addons

_SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

# Glyph, label and colour per package state, keyed by package_status.STATE_*.
_STATE_DISPLAY = {
    STATE_NEW:       ("●", "New",       "green"),
    STATE_UPGRADE:   ("↑", "Update",    "yellow"),
    STATE_CURRENT:   ("✓", "Current",   "dim"),
    STATE_DOWNGRADE: ("↓", "Downgrade", "yellow"),
}
_STATE_UNKNOWN = ("✗", "Unknown", "red")

_STATUS_WIDTH = 13

# Inner width (of #step3-scroll) at which separate Installed/Available columns
# hold a long Debian version without folding. Measured against
# '1:2026.2.0-1ubuntu1~24.04.3~ppa1'; below this the single Version column fits
# the same data on fewer lines, so the table switches to the narrow layout.
_WIDE_LAYOUT_MIN_WIDTH = 116


class Step3Screen(_ButtonNavMixin, BaseWizardScreen):
    """Step 3 — read-only summary of what will be installed."""

    BINDINGS = [
        Binding("f1",     "app.show_about",    "About", show=True),
        Binding("tab",    "focus_next_smart",  "Next", show=True),
        Binding("space",  "press_focused",     "Select", show=True,  priority=True),
        Binding("left",   "focus_prev_button", "",     show=False),
        Binding("right",  "focus_next_button", "",     show=False),
        Binding("ctrl+q", "app.quit",          "Quit", show=True),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._spinner_timer = None
        self._spinner_index = 0

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Compose
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header()

        with Vertical(id="step3-outer"):
            yield Static("Step 3 of 4 — Installation Summary", id="step3-title")
            yield Rule()

            with ScrollableContainer(id="step3-scroll"):
                yield Vertical(id="summary-content")

            yield Rule()

            with Horizontal(id="step3-actions"):
                yield Button("← Back", id="btn-back")
                yield Button("Continue →", variant="primary", id="btn-install")

        yield Footer()

    # ------------------------------------------------------------------
    # Mount — render summary
    # ------------------------------------------------------------------

    def on_mount(self) -> None:
        self._render_summary()
        app = self.app  # type: ignore[attr-defined]
        if app.repo_status == "pending" or app.dep_status == "pending":
            self._spinner_timer = self.set_interval(0.12, self._tick_spinner)
        # Default focus: Continue button so Enter/Space triggers install immediately
        self.set_timer(0.05, lambda: self.query_one("#btn-install", Button).focus())

    def action_navigate_up(self) -> None:
        if not self._scroll_summary(-1):
            super().action_navigate_up()

    def action_navigate_down(self) -> None:
        if not self._scroll_summary(1):
            super().action_navigate_down()

    def _scroll_summary(self, delta: int) -> bool:
        panel = self.query_one("#step3-scroll", ScrollableContainer)
        if self.focused is not panel:
            return False
        offset = panel.scroll_offset.y
        if (delta > 0 and offset >= panel.max_scroll_y) or (delta < 0 and offset <= 0):
            return False
        panel.scroll_relative(y=delta, animate=False)
        return True

    def _render_summary(self) -> None:
        app = self.app  # type: ignore[attr-defined]
        proc = app.processor

        container = self.query_one("#summary-content", Vertical)

        # --- Installation candidate line ---
        profile_label = self._label_for(proc, "base-profiles", app.selected_profile)
        os_label = (app.detected_os_entry or {}).get("display_name") or app.detected_os_key or ""
        version_label = app.selected_version or "1.0"

        candidate_text = (
            f"[bold]Installation candidate:[/bold]\n"
            f"  {profile_label}, {os_label}, Edge Pack v{version_label}"
        )
        container.mount(Static(candidate_text, markup=True, id="candidate-label"))

        # --- Compatibility warnings ---
        comp_warnings: list[str] = []
        if not app.os_supported:
            comp_warnings.append(
                f"OS not in supported list — packages are built for Ubuntu 24.04 / 26.04 "
                f"(detected: {app.detected_os_name})"
            )
        if not app.platform_supported:
            comp_warnings.append(
                f"Platform not recognized — packages optimized for Panther Lake / Wildcat Lake "
                f"(detected: {app.detected_cpu_model})"
            )
        for addon_key in (app.selected_addon_profiles or []):
            comp_warnings.extend(
                proc.addon_compatibility_issues(addon_key, app.detected_platform_key, app.detected_os_key)
            )
        if comp_warnings:
            container.mount(Label(
                "[bold]⚠  Compatibility warnings[/bold]",
                markup=True, classes="summary-warn-header",
            ))
            for w in comp_warnings:
                container.mount(Static(f"  • {w}", classes="summary-warn"))

        container.mount(Rule())

        # --- Packages section ---
        container.mount(Label("[bold]Packages to be installed:[/bold]", markup=True, classes="summary-section"))

        # Mounted empty and refreshed in place by _update_packages(); remounting
        # instead would collide on these IDs, since remove_children() is deferred.
        container.mount(Static("", markup=True, id="pkg-error", classes="summary-warn"))
        container.mount(Static("", id="pkg-table"))

        self._update_packages()

    def _update_packages(self) -> None:
        """Refresh the package table and error banner from current app state."""
        app = self.app  # type: ignore[attr-defined]
        base_pkgs, addon_pkgs = split_base_and_addons(app.pkg_entries)
        # The resolver reads the apt index, so it can only run once the startup
        # refresh has finished.
        if app.repo_status == "ready":
            app.request_base_dependencies(base_pkgs)

        error = self.query_one("#pkg-error", Static)
        table = self.query_one("#pkg-table", Static)

        show_error = app.repo_status == "failed" and bool(app.repo_error)
        error.display = show_error
        if show_error:
            error.update(f"  ⚠  {app.repo_error}")

        if not base_pkgs and not addon_pkgs:
            table.update("  (none)")
            return

        table.update(self._build_table(app, base_pkgs, addon_pkgs))

    # ------------------------------------------------------------------
    # Package table
    # ------------------------------------------------------------------

    def _build_table(self, app, base_pkgs: list[str], addon_pkgs: list[str]) -> Table:
        """Compose the grouped package table shown on this screen.

        Below _WIDE_LAYOUT_MIN_WIDTH the Installed column is dropped and the
        previous version moves into the Version cell, which keeps long Debian
        version strings on one line instead of truncating them.
        """
        wide = self._is_wide()
        table = Table(
            box=None, expand=True, pad_edge=False,
            header_style="bold", show_edge=False,
        )
        # Package is content-sized, not a ratio column: as a ratio column it
        # absorbed all spare width and forced the version columns to truncate.
        table.add_column("Package", no_wrap=True, overflow="ellipsis")
        table.add_column("Status", width=_STATUS_WIDTH, no_wrap=True)
        if wide:
            table.add_column("Installed", overflow="fold")
            table.add_column("Available", overflow="fold")
        else:
            table.add_column("Version", overflow="fold")

        profile_label = self._label_for(app.processor, "base-profiles", app.selected_profile)
        self._add_header_row(table, f"[bold]Base profile — {profile_label}[/bold]")
        for name in base_pkgs:
            table.add_row(*self._row_for(app, name, indent=1, wide=wide))

        self._add_dependency_rows(app, table, wide, exclude=set(base_pkgs) | set(addon_pkgs))

        if addon_pkgs:
            self._add_header_row(table, "")
            self._add_header_row(table, "[bold]Optional add-ons[/bold]")
            for name in addon_pkgs:
                table.add_row(*self._row_for(app, name, indent=1, wide=wide))

        return table

    def _is_wide(self) -> bool:
        """True when there is room for separate Installed and Available columns."""
        try:
            width = self.query_one("#step3-scroll", ScrollableContainer).size.width
        except Exception:
            width = self.app.size.width
        return width >= _WIDE_LAYOUT_MIN_WIDTH

    def _add_header_row(self, table: Table, label: str) -> None:
        """Add a full-width group/spacer row matching the active column count."""
        table.add_row(label, *[""] * (len(table.columns) - 1))

    def _add_dependency_rows(self, app, table: Table, wide: bool, exclude: set[str]) -> None:
        """Append the base profile's direct dependencies, or its progress state."""
        if app.dep_status == "pending":
            frame = _SPINNER_FRAMES[self._spinner_index]
            self._add_header_row(table, f"  [dim]{frame}  Checking what else is included…[/dim]")
            return
        if app.dep_status == "failed":
            self._add_header_row(table, "  [red]Could not list the included packages[/red]")
            return
        # A dependency the user also picked as an add-on is already listed there.
        deps = [name for name in (app.base_dependencies or []) if name not in exclude]
        if not deps:
            return
        self._add_header_row(table, "")
        self._add_header_row(table, f"  [dim]Included with this profile ({len(deps)})[/dim]")
        for name in deps:
            table.add_row(*self._row_for(app, name, indent=2, wide=wide))

    def _row_for(self, app, name: str, indent: int, wide: bool) -> tuple:
        """Build one table row, shaped for the active wide/narrow column set."""
        info = (app.package_status or {}).get(name) or {}
        installed = info.get("installed")
        candidate = info.get("candidate")
        label = f"{'  ' * indent}{name}"

        if app.repo_status == "pending":
            frame = _SPINNER_FRAMES[self._spinner_index]
            status = f"[dim]{frame} Checking…[/dim]"
            if wide:
                return (label, status, installed or "—", "…")
            return (label, status, "…")

        glyph, state_label, colour = _STATE_DISPLAY.get(info.get("state"), _STATE_UNKNOWN)
        status = f"[{colour}]{glyph} {state_label}[/{colour}]"
        if wide:
            return (label, status, installed or "—", candidate or "—")

        version = candidate or "—"
        if installed and installed != candidate:
            version += f"\n[dim]was {installed}[/dim]"
        return (label, status, version)

    # ------------------------------------------------------------------
    # Refresh / spinner
    # ------------------------------------------------------------------

    def refresh_package_status(self) -> None:
        """Re-render the summary when the repository scan or dependency resolve finishes."""
        try:
            self._update_packages()
        except Exception:
            return  # screen was popped while the worker was still running
        # The refresh may have kicked off dependency resolution, so re-evaluate
        # whether the spinner is still needed rather than just stopping it.
        app = self.app  # type: ignore[attr-defined]
        if app.repo_status == "pending" or app.dep_status == "pending":
            if self._spinner_timer is None:
                self._spinner_timer = self.set_interval(0.12, self._tick_spinner)
        else:
            self._stop_spinner()

    def _stop_spinner(self) -> None:
        if self._spinner_timer is not None:
            self._spinner_timer.stop()
            self._spinner_timer = None

    def on_resize(self, event) -> None:
        """Re-lay out the table when the terminal crosses the wide/narrow threshold."""
        try:
            self._update_packages()
        except Exception:
            # Table not mounted yet.
            pass  # nosec B110

    def _tick_spinner(self) -> None:
        """Advance the spinner and repaint the rows that are still waiting on data."""
        app = self.app  # type: ignore[attr-defined]
        if app.repo_status != "pending" and app.dep_status != "pending":
            self._stop_spinner()
            return
        self._spinner_index = (self._spinner_index + 1) % len(_SPINNER_FRAMES)
        try:
            self._update_packages()
        except Exception:
            self._stop_spinner()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _label_for(proc, section: str, key: str | None) -> str:
        if not key:
            return ""
        pairs = dict(proc.display_pairs(section))
        return pairs.get(key, key)

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    @on(Button.Pressed, "#btn-back")
    def on_back(self) -> None:
        self.app.pop_screen()

    @on(Button.Pressed, "#btn-install")
    def on_install(self) -> None:
        from .step4 import Step4Screen
        self.app.push_screen(Step4Screen())
