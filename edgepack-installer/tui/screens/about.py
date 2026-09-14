# tui/screens/about.py — About modal for the EdgePack TUI installer
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Rule, Static

from tui.__version__ import __version__


class AboutScreen(ModalScreen):
    """Modal overlay showing product information and licence."""

    BINDINGS = [
        Binding("escape", "dismiss", "Close", show=False),
        Binding("f1",     "dismiss", "Close", show=False),
        Binding("space",  "dismiss", "Close", show=False),
        Binding("enter",  "dismiss", "Close", show=False),
    ]

    def compose(self) -> ComposeResult:
        raw = self.app.processor._data  # type: ignore[attr-defined]
        about = raw.get("about") or {}

        product_name = about.get("product_name") or "Intel EdgePack Installer"
        description  = (about.get("description") or "").strip()
        license_txt  = about.get("license") or ""
        built_with   = about.get("built_with") or ""
        version      = __version__
        updated_raw  = raw.get("updated_datetime") or ""
        # Format "2026-06-26T05:20:00Z" → "2026-06-26 05:20:00 UTC"
        updated = updated_raw.replace("T", " ").rstrip("Z") + (" UTC" if updated_raw.endswith("Z") else "")

        footer_lines = []
        if version:     footer_lines.append(f"Version  : {version}")
        if updated:     footer_lines.append(f"Updated  : {updated}")
        if license_txt: footer_lines.append(f"License  : {license_txt}")
        if built_with:  footer_lines.append(f"Built with: {built_with}")

        with Vertical(id="about-dialog"):
            yield Static(product_name, id="about-title", markup=False)
            yield Rule()
            if description:
                yield Static(description, id="about-body", markup=False)
                yield Rule()
            yield Static("\n".join(footer_lines), id="about-footer-text", markup=False)
            with Horizontal(id="about-actions"):
                yield Button("Close", variant="primary", id="btn-about-close")

    def on_mount(self) -> None:
        self.query_one("#btn-about-close", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss()
