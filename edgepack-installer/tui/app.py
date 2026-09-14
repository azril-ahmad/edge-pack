# tui/app.py — EdgePackTUI application class and shared installer state
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import os
import sys

from textual.app import App, ComposeResult
from textual.binding import Binding
from .screens.base import BaseWizardScreen
from textual.theme import Theme
from textual.widgets import Footer, Header, Static

from edgepack_shared.processor import Processor
from edgepack_shared.package_status import query_installed_versions
from edgepack_shared.host import (
    read_os_release, host_cpu_model, host_kernel_type, host_os_dot_version,
    host_kernel_release, host_kernel_dot_version, host_kernel_is_ubuntu,
)
from edgepack_shared.log import ep_logger
from tui.__version__ import __version__

# Resolve the CSS file path for both normal execution and PyInstaller bundles.
# In a frozen bundle sys._MEIPASS is the extraction root; the CSS is placed at
# _MEIPASS/tui/app.tcss by the spec's datas entry.
_CSS_FILE = (
    os.path.join(sys._MEIPASS, 'tui', 'app.tcss')
    if getattr(sys, 'frozen', False)
    else os.path.join(os.path.dirname(os.path.abspath(__file__)), 'app.tcss')
)

# Detect whether the terminal supports 24-bit (true) colour.
# MobaXterm and many SSH sessions report 256-colour only; in that mode
# Textual's default theme maps colours to ANSI slots 0-15 which are
# terminal-configurable and inconsistent.  When not in true-colour mode
# we register and apply a custom greyscale theme built entirely from
# 256-colour extended-cube values (consistent across every terminal).
_TRUECOLOR = os.environ.get('COLORTERM', '').lower() in ('truecolor', '24bit')

# All hex values below fall exactly on the 6x6x6 cube (R/G/B each in
# {0,95,135,175,215,255}) or the greyscale ramp (232-255) so they render
# identically on every xterm-256color terminal.
_GREY_THEME = Theme(
    name="edgepack-grey",
    primary="#585858",       # 240 — medium grey  (borders, header bg)
    secondary="#3a3a3a",     # 237 — darker grey
    accent="#eeeeee",        # 255 — near-white   (selected / highlighted)
    foreground="#d0d0d0",    # 252 — light grey   (main text)
    background="#1c1c1c",    # 234 — very dark    (screen background)
    surface="#262626",       # 235 — panel backgrounds
    panel="#303030",         # 236 — widget interiors
    warning="#d7af00",       # 178 — gold-yellow  (warnings)
    error="#d75f5f",         # 167 — red          (errors)
    success="#87af87",       # 114 — muted green  (success)
    dark=True,
    variables={
        # Fix the grey-box-on-focus that covers text in the default theme
        "block-cursor-background": "#3a3a3a",   # 237
        "block-cursor-foreground": "#eeeeee",   # 255
        "block-cursor-text-style": "bold",
        "border":         "#585858",            # 240
        "border-blurred": "#3a3a3a",            # 237
    },
)

_256_CSS = "" if _TRUECOLOR else """
Header  { background: #000080; }   /* ANSI blue — classic navy header */
Footer  { background: #000080; }
RadioButton { background: transparent; }
RadioButton:focus { background: transparent; }
RadioButton > .toggle--button { background: transparent; }
RadioButton:focus > .toggle--button { background: transparent; }
RadioButton:focus > .toggle--label { background: transparent; }
Checkbox { background: transparent; }
Checkbox:focus { background: transparent; }
Checkbox > .toggle--button { background: transparent; }
Checkbox:focus > .toggle--button { background: transparent; }
Checkbox:focus > .toggle--label { background: #3a3a3a; color: #eeeeee; text-style: bold; }
"""


MIN_WIDTH = 100
MIN_HEIGHT = 30


class TooSmallScreen(BaseWizardScreen):
    """Overlay screen shown when the terminal is below the minimum supported size.

    Pushed on top of the wizard stack automatically; popped when the terminal
    is resized back to an acceptable size.
    """

    BINDINGS = [
        Binding("f1",     "app.show_about", "About", show=True),
        ("ctrl+q", "app.quit", "Quit"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="too-small-message")
        yield Footer()

    def on_mount(self) -> None:
        self._refresh_message(self.app.size.width, self.app.size.height)

    def on_resize(self, event) -> None:
        self._refresh_message(event.size.width, event.size.height)

    def _refresh_message(self, w: int, h: int) -> None:
        self.query_one("#too-small-message", Static).update(
            f"Terminal too small  ({w} \u00d7 {h})\n\n"
            f"Please resize to at least {MIN_WIDTH} \u00d7 {MIN_HEIGHT} characters."
        )


class EdgePackTUI(App[None]):
    """Root Textual application.

    Holds all shared state that flows across the 4-step wizard:
      - logger         : log info, errors and warnings
      - processor      : parsed YAML template
      - detected platform/OS : auto-detected at startup
      - step-1 selections: profile, platform, usages, kvariant, type, version
      - step-2 entries : pkg_entries list (plain dicts, no Gtk)
      - installed_versions: packages already on the host
    """

    CSS_PATH = _CSS_FILE
    CSS = _256_CSS
    TITLE = "Intel Edgepack Installer"

    BINDINGS = [
        ("ctrl+q", "quit",       "Quit"),
        ("f1",     "show_about", "About"),
    ]

    def __init__(self, debug_level: str = "MUTE") -> None:
        super().__init__()

        tui_logger = ep_logger(name="tui_log", log_file = "/var/log/edgepack/edgepack_tui.log", level=debug_level)
        self.tui_version = __version__
        self.logger = tui_logger.get_logger()
        self.logger.debug("Initializing EdgePackTUI (debug_level=%s)", debug_level)

        # Detection defaults — overridden inside the try block when detection succeeds.
        # Keeping them here ensures every attribute always exists even if detection throws.
        self.kernel_variant: str | None = None
        self.detected_platform_key: str | None = None
        self.detected_platform_entry: dict = {}
        self.detected_os_key: str | None = None
        self.detected_os_entry: dict = {}
        self.platform_supported: bool = False
        self.os_supported: bool = False
        self.detected_os_dot_version: str = ''
        self.os_version_met: bool = False
        self.os_version_warning: str = ''
        self.detected_os_name: str = 'Unknown'
        self.detected_cpu_model: str = 'Unknown'
        self.detected_kernel_version: str = 'Unknown'
        self.detected_kernel_release: str = 'Unknown'
        self.kernel_version_met: bool = False
        self.kernel_version_warning: str = ''
        self.detected_kernel_is_ubuntu: bool = False
        
        try:
            self.logger.debug("Loading YAML template...")
            self.processor = Processor.load()
            self.logger.debug("YAML template loaded successfully")
        except Exception as e:
            self.logger.exception("Failed to load YAML template: %s", e)
            raise

        # Auto-detect host platform + OS at startup.  Either may be None if
        # the host isn't listed in the template — Step 1 renders a warning.
        try:
            # Read CPU model once — reused for both platform matching and display.
            # Pass the raw string (may be empty) to detect_platform so it can
            # correctly short-circuit when the model is unreadable.
            _cpu_model_raw = host_cpu_model()
            self.detected_cpu_model = _cpu_model_raw or 'Unknown'
            self.detected_platform_key, self.detected_platform_entry = self.processor.detect_platform(_cpu_model_raw)
            # Read /etc/os-release once and share the result across all OS detection calls.
            _os_info = read_os_release()
            self.detected_os_key, self.detected_os_entry = self.processor.detect_os(_os_info)
            self.logger.info("Platform detection: key=%s, supported=%s", self.detected_platform_key, self.detected_platform_key is not None)
            self.logger.info("OS detection: key=%s, supported=%s", self.detected_os_key, self.detected_os_key is not None)
            self.platform_supported: bool = self.detected_platform_key is not None
            self.os_supported: bool = self.detected_os_key is not None
            self.kernel_variant = host_kernel_type()
            self.logger.info("Kernel variant: %s", self.kernel_variant)

            # Dot-release version check (e.g. '24.04.2' vs min_version in template).
            self.detected_os_dot_version: str = host_os_dot_version(_os_info)
            _ver_issues = self.processor.os_version_issues(
                self.detected_os_key or '', self.detected_os_dot_version
            ) if self.detected_os_key else []
            self.os_version_met: bool = not _ver_issues
            self.os_version_warning: str = _ver_issues[0] if _ver_issues else ''
            self.logger.info(
                "OS version check: dot_version=%s, met=%s",
                self.detected_os_dot_version, self.os_version_met,
            )

            # Kernel version + provenance check — both scoped per-OS-variant
            # e.g. os_variant.ubuntu_noble.min_kernel_version
            # and .require_ubuntu_kernel ("7.0.0" + Ubuntu-built required for that OS).
            _kernel_release_raw = host_kernel_release()
            _kernel_dot_version_raw = host_kernel_dot_version(_kernel_release_raw)
            self.detected_kernel_version = _kernel_dot_version_raw or 'Unknown'
            self.detected_kernel_release = _kernel_release_raw or 'Unknown'
            self.detected_kernel_is_ubuntu = host_kernel_is_ubuntu()
            _kernel_issues = self.processor.kernel_version_issues(
                _kernel_dot_version_raw, self.detected_os_key, self.detected_kernel_is_ubuntu,
            )
            self.kernel_version_met: bool = not _kernel_issues
            self.kernel_version_warning: str = _kernel_issues[0] if _kernel_issues else ''
            self.logger.info(
                "Kernel version/provenance check: version=%s, is_ubuntu=%s, met=%s",
                self.detected_kernel_version, self.detected_kernel_is_ubuntu,
                self.kernel_version_met,
            )

            # Raw host strings for display when YAML detection doesn't match.
            self.detected_os_name: str = (
                _os_info.get('PRETTY_NAME')
                or f"{_os_info.get('NAME', '')} {_os_info.get('VERSION_ID', '')}".strip()
                or 'Unknown'
            )
            self.logger.info("CPU info: cpu_model=%s", self.detected_cpu_model)

            # Warning cases
            if self.detected_cpu_model == "Unknown":
                self.logger.warning("CPU model not found")
            if self.detected_platform_key is None:
                self.logger.warning("Platform is not supported")
            if self.detected_os_key is None:
                self.logger.warning("OS is not supported")
            if self.kernel_variant is None:
                self.logger.warning("Unknown kernel variant detected")
            if not self.kernel_version_met:
                self.logger.warning("Kernel version below minimum requirement")
        except Exception as e:
            self.logger.exception(
                "Failed to detect system specification — continuing with limited functionality. "
                "Install may fail or produce incorrect results on this host."
            )
            # Continue with defaults so the user gets a usable (restricted) UI
            # instead of a hard failure, but log clearly that detection was skipped.

        # ---------- Step 1 selections ----------
        # selected_profile is the new profile radio (base-standard/base-realtime).
        # selected_platform is auto-detected (no user choice anymore).
        # The other fields remain for Step 2/3/4 compatibility — Step 1
        # populates them with defaults derived from the profile.
        self.selected_profile: str | None = self.kernel_variant
        self.selected_addon_profiles: list[str] = []
        self.selected_platform: str | None = self.detected_platform_key
        self.selected_usages: list[str] = []
        self.selected_kvariant: str | None = None
        self.selected_version: str | None = None

        # ---------- Step 2 package tree --------
        # List of plain-dict entries built by package_logic.build_pkg_entries().
        # Shared between Step 2 (editing), Step 3 (summary) and Step 4 (install).
        self.pkg_entries: list[dict] = []

        # ---------- Pre-existing packages ------
        # {package_name: installed_version} populated at startup.
        self.installed_versions: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def on_mount(self) -> None:
        """Push Step 1 as the first screen after app mounts."""
        # Clean up stale temporary log files from previous runs (excluding /var/log/edgepack/edgepack_tui.log
        # because it is already opened by the logger in __init__ — removing an open file would
        # silently lose subsequent logs on POSIX). This prevents information leakage and frees
        # disk space in constrained environments.
        for _tmp_log in ("/var/log/edgepack/edgepack.log", "/var/log/edgepack/update.log"):
            try:
                os.remove(_tmp_log)
            except OSError:
                pass  # file may not exist — ignore
        
        # Set header title from template so version is maintained in one place.
        _about = self.processor._data.get("about") or {}
        _name = _about.get("product_name") or "Intel Edgepack Installer"
        self.title = f"{_name} - v{__version__}"
        self.logger.info("App mounted: title=%s", self.title)

        if not _TRUECOLOR:
            # Register and apply the greyscale theme for 256-colour terminals.
            # All colours use extended-cube palette entries so they render
            # identically on MobaXterm, PuTTY, and SSH sessions.
            self.register_theme(_GREY_THEME)
            self.theme = "edgepack-grey"
            self.logger.debug("Using greyscale theme for 256-color terminal")
        else:
            self.logger.debug("Using true-color theme")

        from .screens.step1 import Step1Screen
        self.push_screen(Step1Screen())
        self.logger.debug("Step1Screen pushed")

        # Scan installed packages in background — non-blocking
        self.run_worker(self._scan_installed, thread=True)
        self.logger.debug("Background worker started for package scanning")

    def on_resize(self, event) -> None:
        """Push/pop TooSmallScreen as the terminal crosses the minimum size threshold."""
        too_small = event.size.width < MIN_WIDTH or event.size.height < MIN_HEIGHT
        is_too_small_screen = isinstance(self.screen, TooSmallScreen)
        if too_small and not is_too_small_screen:
            self.logger.warning("Terminal too small: %dx%d (minimum=%dx%d)", event.size.width, event.size.height, MIN_WIDTH, MIN_HEIGHT)
            self.push_screen(TooSmallScreen())
        elif not too_small and is_too_small_screen:
            self.logger.debug("Terminal resized to acceptable size: %dx%d", event.size.width, event.size.height)
            self.pop_screen()

    def compose(self) -> ComposeResult:  # pragma: no cover
        yield Header()
        yield Footer()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _scan_installed(self) -> None:
        """Background worker: detect pre-installed Intel Edge packages."""
        names = list(self.processor.all_package_names())
        self.logger.debug("Scanning %d package names for installed versions", len(names))
        self.installed_versions = query_installed_versions(names)
        self.logger.info("Package scan complete: %d packages already installed", len(self.installed_versions))
        if self.installed_versions:
            self.logger.debug("Installed packages: %s", self.installed_versions)
        # Refresh Step 1 installed-profile warnings now that results are available.
        self.call_from_thread(self._refresh_installed_warnings)

    def _refresh_installed_warnings(self) -> None:
        """Update Step1Screen's per-profile warnings after the background scan completes."""
        from .screens.step1 import Step1Screen
        if isinstance(self.screen, Step1Screen):
            self.screen._update_installed_warnings()

    def action_show_about(self) -> None:
        """Push the About modal (F1)."""
        self.logger.debug("About screen requested")
        from .screens.about import AboutScreen
        self.push_screen(AboutScreen())
