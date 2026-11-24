# tui/app.py — EdgePackTUI application class and shared installer state
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

import os
import sys

from textual.app import App, ComposeResult
from textual.binding import Binding
from .screens.base import BaseWizardScreen
from textual.theme import Theme
from textual.widgets import Footer, Header, Static

from edgepack_shared.processor import Processor
from edgepack_shared.package_status import query_package_status
from edgepack_shared.install_logic import (
    prepare_repositories, direct_dependencies, cancel_elevated_commands,
)
from edgepack_shared.host import (
    read_os_release, host_cpu_model, host_kernel_type, host_os_dot_version,
    host_kernel_release, host_kernel_dot_version, host_kernel_is_ubuntu,
    host_tsn_nics, host_tsn_summary, host_ipu_status, host_ipu_summary,
    host_ipu_warning,
)
from edgepack_shared.log import ep_logger
from tui.screens.about import PRODUCT_NAME
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

    ENABLE_COMMAND_PALETTE = False

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
        self.detected_tsn_nics: list[dict] = []
        self.detected_tsn_description: str = 'Not detected'
        self.tsn_supported: bool = False
        self.tsn_warning: str = ''
        self.detected_ipu_status: dict = {}
        self.detected_ipu_description: str = 'Not detected'
        self.ipu_healthy: bool = False
        self.ipu_warning: str = ''
        
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

            # TSN (Time-Sensitive Networking) capability — requires an Intel
            # I225-generation or later NIC. Informational only: a host without
            # one can still install, so this never blocks the wizard.
            self.detected_tsn_nics = host_tsn_nics(
                self.processor.tsn_nic_device_ids(), self.processor.tsn_nic_drivers()
            )
            self.detected_tsn_description = host_tsn_summary(self.detected_tsn_nics)
            self.tsn_supported = bool(self.detected_tsn_nics)
            self.tsn_warning = (
                '' if self.tsn_supported else
                'No TSN-capable NIC detected — Time-Sensitive Networking requires '
                'an Intel I225/I226 (or later) Ethernet controller'
            )
            self.logger.info(
                "TSN detection: supported=%s, nics=%s",
                self.tsn_supported, self.detected_tsn_description,
            )

            # IPU (camera Image Processing Unit) presence — detected on the PCI
            # bus. Informational only, like the TSN check.
            self.detected_ipu_status = host_ipu_status(
                device_ids=self.processor.ipu_device_ids()
            )
            self.detected_ipu_description = host_ipu_summary(self.detected_ipu_status)
            self.ipu_healthy = bool(self.detected_ipu_status.get('healthy'))
            self.ipu_warning = host_ipu_warning(self.detected_ipu_status)
            self.logger.info(
                "IPU detection: healthy=%s, status=%s",
                self.ipu_healthy, self.detected_ipu_description,
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
            if not self.tsn_supported:
                self.logger.warning("No TSN-capable NIC (Intel I225 or later) detected")
            if not self.ipu_healthy:
                self.logger.warning("IPU check did not pass: %s", self.detected_ipu_description)
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

        # ---------- Repository preparation -----
        # The apt repositories are registered and the index refreshed in the
        # background while the user works through Step 1/2, so Step 3 can show
        # the candidate version of every selected package.
        # repo_status: 'pending' until the worker finishes, then 'ready' or 'failed'.
        self.repo_status: str = "pending"
        self.repo_error: str = ""
        # {package_name: {'installed': str|None, 'candidate': str|None, 'state': str}}
        self.package_status: dict[str, dict] = {}

        # ---------- Base profile dependencies --
        # Resolved on demand when Step 3 opens (the closure depends on the
        # Step 1/2 selection). Add-on packages are deliberately not expanded.
        # dep_status: 'idle' | 'pending' | 'ready' | 'failed'
        self.dep_status: str = "idle"
        self.base_dependencies: list[str] = []
        self._dep_resolved_for: tuple[str, ...] = ()

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
        
        # Header title uses the static About info so version stays in one place.
        self.title = f"{PRODUCT_NAME} - v{__version__}"
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
        self.run_worker(self._prepare_environment, thread=True)
        self.logger.debug("Background worker started for repository setup and package scanning")

    async def action_quit(self) -> None:
        # The startup worker may be blocked in apt; the interpreter would wait
        # for it at exit, leaving a dead UI and a live process.
        cancel_elevated_commands()
        await super().action_quit()

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

    def _prepare_environment(self) -> None:
        """Background worker: register repositories, refresh the apt index, then scan versions.

        The version scan runs even when the refresh failed so the wizard can
        still report what is installed locally; those packages simply have no
        candidate version to compare against.
        """
        try:
            self._prepare_environment_inner()
        except Exception:
            # Never leave Step 3 spinning on "pending" because the worker died.
            self.logger.exception("Environment preparation failed")
            self.repo_error = (
                "Could not check package versions on this system. "
                "Installation can still proceed."
            )
            self.repo_status = "failed"
            self.call_from_thread(self._on_environment_ready)

    def _prepare_environment_inner(self) -> None:
        # Only the OS-level EdgePack repositories are registered here. Add-on
        # prerequisite repos (e.g. the graphics PPA) are deliberately left to
        # Step 4, so the host only ever gains the repos the user actually chose.
        repos = list((self.detected_os_entry or {}).get("repositories", {}).values())
        self.logger.debug(
            "Preparing %d repositories for OS %s", len(repos), self.detected_os_key,
        )

        ok, message = prepare_repositories(repos)

        names = list(self.processor.all_package_names())
        self.logger.debug("Scanning %d package names for installed/candidate versions", len(names))
        package_status = query_package_status(names)
        self.package_status = package_status
        self.installed_versions = {
            name: info["installed"]
            for name, info in package_status.items()
            if info.get("installed")
        }
        # Published last: Step 3 polls repo_status, and flipping it before the
        # scan lands would briefly render every package as "Unknown".
        self.repo_error = message
        self.repo_status = "ready" if ok else "failed"
        self.logger.info(
            "Package scan complete: repo_status=%s, %d packages already installed",
            self.repo_status, len(self.installed_versions),
        )
        self.call_from_thread(self._on_environment_ready)

    def _on_environment_ready(self) -> None:
        """Refresh whichever screen is showing once the background scan completes."""
        from .screens.step1 import Step1Screen
        from .screens.step3 import Step3Screen
        if isinstance(self.screen, Step1Screen):
            self.screen._update_installed_warnings()
        elif isinstance(self.screen, Step3Screen):
            self.screen.refresh_package_status()

    def request_base_dependencies(self, base_packages: list[str]) -> None:
        """Start resolving *base_packages*' dependency closure, unless already done.

        Called by Step 3. Results are cached against the package list so
        stepping back and forward does not re-run the resolver.
        """
        key = tuple(base_packages)
        if not key:
            self.dep_status = "ready"
            self.base_dependencies = []
            return
        if self._dep_resolved_for == key and self.dep_status in ("ready", "failed"):
            return
        if self.dep_status == "pending":
            return
        self._dep_resolved_for = key
        self.dep_status = "pending"
        self.run_worker(lambda: self._resolve_dependencies(list(key)), thread=True)

    def _resolve_dependencies(self, base_packages: list[str]) -> None:
        """Background worker: list the base profile's direct dependencies and scan their versions."""
        self.logger.debug("Resolving dependencies for %s", ", ".join(base_packages))
        try:
            ok, resolved = direct_dependencies(base_packages)
            if ok:
                names = [name for name in resolved if name not in self.package_status]
                if names:
                    self.package_status.update(query_package_status(names))
                # Published last, for the same reason as _prepare_environment.
                self.base_dependencies = resolved
                self.dep_status = "ready"
            else:
                self.base_dependencies = []
                self.dep_status = "failed"
        except Exception:
            self.logger.exception("Dependency lookup failed")
            self.base_dependencies = []
            self.dep_status = "failed"
        self.logger.info(
            "Dependency resolution %s (%d packages)",
            self.dep_status, len(self.base_dependencies),
        )
        self.call_from_thread(self._on_dependencies_ready)

    def _on_dependencies_ready(self) -> None:
        from .screens.step3 import Step3Screen
        if isinstance(self.screen, Step3Screen):
            self.screen.refresh_package_status()

    def action_show_about(self) -> None:
        """Push the About modal (F1)."""
        self.logger.debug("About screen requested")
        from .screens.about import AboutScreen
        self.push_screen(AboutScreen())
