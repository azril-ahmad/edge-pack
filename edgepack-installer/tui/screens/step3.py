# tui/screens/step3.py — Step 3: installation summary with compatibility warnings
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, ScrollableContainer, Vertical

from .base import BaseWizardScreen, _ButtonNavMixin
from textual.widgets import Button, Footer, Header, Label, Rule, Static

from ..package_logic import collect_selected_packages


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
        # Prevent the ScrollableContainer from grabbing an extra Tab stop.
        self.query_one("#step3-scroll", ScrollableContainer).can_focus = False
        # Default focus: Continue button so Enter/Space triggers install immediately
        self.set_timer(0.05, lambda: self.query_one("#btn-install", Button).focus())

    def _render_summary(self) -> None:
        app = self.app  # type: ignore[attr-defined]
        proc = app.processor
        entries = app.pkg_entries

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

        # --- Collect packages ---
        packages = collect_selected_packages(entries)

        all_pkgs = [e for e in entries if e["level"] == 2 and e.get("checked")]

        # --- Packages section ---
        container.mount(Label("[bold]Packages to install:[/bold]", markup=True, classes="summary-section"))
        if all_pkgs:
            for pkg in all_pkgs:
                ver_str = f" (v{pkg['version']})" if pkg.get("version") else ""
                container.mount(Static(f"  - {pkg['name']}{ver_str}", classes="summary-pkg"))
        else:
            container.mount(Static("  (none)", classes="summary-indent"))

        # --- Already installed / will be updated ---
        installed = app.installed_versions
        if installed:
            upgrades = []
            for name, new_ver in packages.items():
                if name in installed:
                    upgrades.append((name, installed[name]))
            if upgrades:
                container.mount(Rule())
                container.mount(Label("[bold]Already installed:[/bold]", markup=True, classes="summary-section"))
                for name, old_ver in upgrades:
                    container.mount(Static(
                        f"  {name}  (installed: v{old_ver})",
                        classes="summary-indent",
                    ))

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
