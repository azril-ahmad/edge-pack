# tui/cli.py — non-interactive CLI install path (no Textual UI)
#
# Lets automated/validation flows install a base profile (+ optional
# add-on profiles) directly from the command line, without stepping through
# the TUI wizard, e.g.:
#
#     sudo ./edgepack-installer install base-standard ipu
#     ./edgepack-installer list
#
# Reuses the same Processor/package_logic/install_logic building blocks as
# the TUI so both paths resolve packages and prerequisites identically.
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

import logging
import os
import re
import signal
import sys
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from edgepack_shared.host import (
    host_cpu_model, read_os_release, host_os_dot_version,
    host_kernel_release, host_kernel_dot_version, host_kernel_is_ubuntu,
    host_tsn_nics, host_tsn_summary,
    host_ipu_status, host_ipu_summary, host_ipu_warning,
)
from edgepack_shared.install_logic import run_install
from edgepack_shared.log import open_secure_log, rotate_logs, secure_log_path
from edgepack_shared.package_report import (
    apt_cache_policy_origins,
    apt_dependency_closure,
    parse_get_line,
    parse_resolved_line,
    write_package_summary,
)
from edgepack_shared.processor import Processor

from .package_logic import required_debconf_selections, required_prerequisites


def _make_cli_logger() -> logging.Logger:
    """Return a logger that writes to both the TUI log and stdout."""
    from edgepack_shared.log import ep_logger as _ep_log
    _log = _ep_log(name="cli_install", log_file="/var/log/edgepack/edgepack_tui.log")
    return _log.get_logger()


logger: logging.Logger | None = None  # initialised on first call to install_command


_CLI_LOG_DIR = Path("/var/log/edgepack")


class _CliStream:
    def __init__(self, stream, session):
        self.stream = stream
        self.session = session
        self.failed = False

    def write(self, text):
        with self.session.lock:
            self.session.write_log(text)
            if not self.failed:
                try:
                    self.stream.write(text)
                    self.stream.flush()
                except (OSError, ValueError):
                    self.failed = True
                    self.session.write_log("\nWarning: terminal output unavailable; installation remains managed.\n")
                    try:
                        descriptor = os.open(os.devnull, os.O_WRONLY)
                        try:
                            os.dup2(descriptor, self.stream.fileno())
                        finally:
                            os.close(descriptor)
                    except (OSError, ValueError, AttributeError):
                        pass
        return len(text)

    def flush(self):
        return None

    def __getattr__(self, name):
        return getattr(self.stream, name)


class _CliSession:
    def __init__(self):
        self.lock = threading.Lock()
        self.log_file = None
        self.log_path = None
        self.package_report_path = None
        self.saved_package_report_path = None
        self.log_error = False
        self.stdout = sys.stdout
        self.stderr = sys.stderr

    def __enter__(self):
        sys.stdout = _CliStream(self.stdout, self)
        sys.stderr = _CliStream(self.stderr, self)
        if os.geteuid() == 0:
            try:
                _CLI_LOG_DIR.mkdir(parents=True, exist_ok=True)
                secure_log_path(_CLI_LOG_DIR, is_dir=True)
                rotate_logs(str(_CLI_LOG_DIR), max_age_days=30, max_count=9)
                stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
                self.package_report_path = _CLI_LOG_DIR / f"edgepack-packages-{stamp}.log"
                path = _CLI_LOG_DIR / f"edgepack-installer-{stamp}.log"
                self.log_file = open_secure_log(str(path), buffering=1)
                self.log_path = path
                print(f"Installation log: {path}")
                print(f"Started: {datetime.now().isoformat(timespec='seconds')}")
            except OSError:
                self.warn_log_failure()
        return self

    def warn_log_failure(self):
        if not self.log_error:
            self.log_error = True
            try:
                self.stderr.write("warning: installation log unavailable; output may not be saved\n")
                self.stderr.flush()
            except (OSError, ValueError):
                pass

    def write_log(self, text):
        if self.log_file is not None:
            try:
                self.log_file.write(text)
            except (OSError, ValueError):
                self.close_log()
                self.warn_log_failure()

    def close_log(self):
        log_file, self.log_file = self.log_file, None
        if log_file is not None:
            try:
                log_file.close()
            except OSError:
                self.warn_log_failure()

    def record_package_report(self, path: str):
        self.saved_package_report_path = path

    def __exit__(self, exc_type, exc_value, traceback):
        if exc_type is not None:
            self.write_log(f"\nCLI exited before normal completion: {exc_type.__name__}\n")
        self.close_log()
        if self.log_path is not None and not self.log_error:
            print(f"Installation log: {self.log_path}")
        if self.saved_package_report_path is not None:
            print(f"Package report: {self.saved_package_report_path}")
        sys.stdout = self.stdout
        sys.stderr = self.stderr


def _dedupe(seq: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in seq:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _check_host_requirements(processor: Processor) -> dict:
    """Run the same platform/OS/kernel checks the TUI performs at startup (app.py __init__).

    Returns detected platform/os key+entry plus an 'issues' list of
    human-readable failure reasons — empty when the host qualifies, and a
    non-fatal 'warnings' list (e.g. missing TSN-capable NIC).
    """
    cpu_model = host_cpu_model()
    platform_key, platform_entry = processor.detect_platform(cpu_model)
    os_info = read_os_release()
    os_key, os_entry = processor.detect_os(os_info)

    issues: list[str] = []

    if platform_key is None:
        supported = ", ".join(processor.supported_platform_labels())
        issues.append(f"Unsupported platform — detected CPU '{cpu_model or 'Unknown'}' (supported: {supported})")
    if os_key is None:
        supported = ", ".join(processor.supported_os_labels())
        os_name = (
            os_info.get("PRETTY_NAME")
            or f"{os_info.get('NAME', '')} {os_info.get('VERSION_ID', '')}".strip()
            or "Unknown"
        )
        issues.append(f"Unsupported OS — detected '{os_name}' (supported: {supported})")

    if os_key:
        issues.extend(processor.os_version_issues(os_key, host_os_dot_version(os_info)))

    kernel_dot_version = host_kernel_dot_version(host_kernel_release())
    issues.extend(processor.kernel_version_issues(kernel_dot_version, os_key, host_kernel_is_ubuntu()))

    # TSN capability is informational — a host without an Intel I225-generation
    # (or later) NIC can still install, so this is a warning, never an issue.
    tsn_nics = host_tsn_nics(processor.tsn_nic_device_ids(), processor.tsn_nic_drivers())
    warnings: list[str] = []
    if not tsn_nics:
        warnings.append(
            "No TSN-capable NIC detected — Time-Sensitive Networking requires "
            "an Intel I225/I226 (or later) Ethernet controller"
        )

    # IPU health from the kernel log — also informational.
    ipu_status = host_ipu_status(device_ids=processor.ipu_device_ids())
    ipu_warning = host_ipu_warning(ipu_status)
    if ipu_warning:
        warnings.append(ipu_warning)

    return {
        "platform_key": platform_key,
        "platform_entry": platform_entry,
        "os_key": os_key,
        "os_entry": os_entry,
        "tsn_nics": tsn_nics,
        "tsn_description": host_tsn_summary(tsn_nics),
        "ipu_status": ipu_status,
        "ipu_description": host_ipu_summary(ipu_status),
        "issues": issues,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# 'list' command
# ---------------------------------------------------------------------------

def list_command(processor: Processor) -> None:
    """Print every base profile and add-on profile key defined in the template."""
    print("Base profiles (choose exactly one):\n")
    base_profiles = processor.section("base-profiles")
    key_width = max((len(k) for k in base_profiles), default=0)
    for key, entry in base_profiles.items():
        entry = entry or {}
        display = entry.get("display_name") or key
        platforms = ", ".join(entry.get("supported_platforms") or []) or "any"
        os_variants = ", ".join(entry.get("supported_os_variants") or []) or "any"
        print(f"  {key.ljust(key_width)}  {display}")
        print(f"  {' ' * key_width}  (platforms: {platforms} | os: {os_variants})")

    print("\nAdd-on profiles (optional, zero or more):\n")
    addon_profiles = processor.section("profiles")
    key_width = max((len(k) for k in addon_profiles), default=0)
    for key, entry in addon_profiles.items():
        entry = entry or {}
        display = entry.get("display_name") or key
        optional_with = ", ".join(entry.get("base-profiles") or []) or "any base profile"
        print(f"  {key.ljust(key_width)}  {display}")
        print(f"  {' ' * key_width}  Optional with: {optional_with}")
        included_in = entry.get("included_in_base_profiles") or []
        if included_in:
            print(f"  {' ' * key_width}  Already included in: {', '.join(included_in)}")

    print("\nUsage:")
    print("  edgepack-installer install <base-profile> [addon ...]")
    print("  e.g. sudo edgepack-installer install base-standard ipu")


# ---------------------------------------------------------------------------
# 'install' command
# ---------------------------------------------------------------------------

# Maximum allowed length for profile keys passed on the CLI. Prevents
# oversized or malformed arguments from reaching downstream processing.
_MAX_PROFILE_KEY_LEN = 128


def _validate_profile_key(key: str, label: str) -> int | None:
    """Return an exit code (non-zero) if *key* is invalid, else None."""
    if not key:
        print(f"error: {label} must not be empty", file=sys.stderr)
        return 2
    if len(key) > _MAX_PROFILE_KEY_LEN:
        print(
            f"error: {label} exceeds maximum length of {_MAX_PROFILE_KEY_LEN} characters",
            file=sys.stderr,
        )
        logger.warning("Validation failure (%s): argument exceeds maximum length of %d chars", label, _MAX_PROFILE_KEY_LEN)  # type: ignore[reportPossiblyUnboundVariable]
        return 2
    # Reject characters that could be interpreted as shell metacharacters or
    # path separators, since keys are embedded in shell commands.
    if re.search(r'[^a-zA-Z0-9_\-]', key):
        print(
            f"error: {label} contains invalid characters (only a-z, A-Z, 0-9, _, - allowed)",
            file=sys.stderr,
        )
        logger.warning("Validation failure (%s): argument contains disallowed characters", label)  # type: ignore[reportPossiblyUnboundVariable]
        return 2
    return None


def install_command(args) -> int:
    """Run an unattended installation with resilient output and a transaction log."""
    with _CliSession() as session:
        try:
            code = _install_command(
                args, package_report_path=session.package_report_path,
                package_report_callback=session.record_package_report,
            )
        except KeyboardInterrupt:
            session.write_log("\nCLI exit code: 130\n")
            raise
        except Exception:
            session.write_log("\nCLI exit code: 1 (unexpected error)\n")
            raise
        session.write_log(f"\nCLI exit code: {code}\n")
        return code


def _install_command(args, package_report_path: Path | None = None,
                     package_report_callback: Callable[[str], None] | None = None) -> int:
    """Resolve, validate, and install *args.base_profile* + *args.addons*.

    Mirrors the checks enforced by the TUI (host requirements at startup,
    then the compatibility rules behind Step 1/2's disabled radios) so the
    CLI can be trusted as a validation gate. Runs unattended — no
    confirmation prompt — since this path exists for scripted validation.
    Returns the process exit code.
    """
    global logger
    logger = _make_cli_logger()

    processor = Processor.load()

    base_profiles = processor.section("base-profiles")
    addon_profiles = processor.section("profiles")

    err = _validate_profile_key(args.base_profile, "base profile key")
    if err:
        return err

    for addon in args.addons:
        err = _validate_profile_key(addon, "add-on profile key")
        if err:
            return err

    if args.base_profile not in base_profiles:
        print(f"error: unknown base profile '{args.base_profile}'", file=sys.stderr)
        print(f"       valid base profiles: {', '.join(sorted(base_profiles)) or '(none)'}", file=sys.stderr)
        print("       run 'edgepack-installer list' to see all options", file=sys.stderr)
        logger.warning("Validation failure: unknown base profile '%s'", args.base_profile)
        return 2

    unknown_addons = [a for a in args.addons if a not in addon_profiles]
    if unknown_addons:
        print(f"error: unknown add-on profile(s): {', '.join(unknown_addons)}", file=sys.stderr)
        print(f"       valid add-on profiles: {', '.join(sorted(addon_profiles)) or '(none)'}", file=sys.stderr)
        print("       run 'edgepack-installer list' to see all options", file=sys.stderr)
        logger.warning("Validation failure: unknown add-on profile(s): %s", ", ".join(unknown_addons))
        return 2

    if os.geteuid() != 0:
        print(
            "error: installing packages requires root privileges — "
            "re-run with sudo, e.g. 'sudo edgepack-installer install ...'",
            file=sys.stderr,
        )
        return 1

    # ---- Host requirements (fatal, mirrors UnsupportedPlatformScreen in the TUI) ----
    print("Checking system requirements...")
    host = _check_host_requirements(processor)
    if host["issues"]:
        print("error: this machine does not meet the installation requirements:", file=sys.stderr)
        for issue in host["issues"]:
            print(f"       - {issue}", file=sys.stderr)
        logger.warning("Host does not meet installation requirements: %s", "; ".join(host["issues"]))
        return 4
    print("System requirements: OK")
    for warning in host["warnings"]:
        print(f"warning: {warning}")
        logger.warning("%s", warning)

    platform_key, platform_entry = host["platform_key"], host["platform_entry"]
    os_key, os_entry = host["os_key"], host["os_entry"]
    platform_display = (platform_entry or {}).get("display_name") or platform_key
    os_display = (os_entry or {}).get("display_name") or os_key

    # ---- Base profile compatibility (fatal, mirrors disabled radio in Step 1) ----
    base_rows = {
        key: (display, supported)
        for key, display, supported in processor.base_profiles_for_platform(platform_key, os_key)
    }
    base_display, base_supported = base_rows.get(args.base_profile, (args.base_profile, False))
    if not base_supported:
        entry = base_profiles.get(args.base_profile) or {}
        supported_platforms = ", ".join(entry.get("supported_platforms") or []) or "any"
        supported_os = ", ".join(entry.get("supported_os_variants") or []) or "any"
        print(
            f"error: base profile '{args.base_profile}' is not supported on this host "
            f"(detected platform: {platform_display}, os: {os_display})",
            file=sys.stderr,
        )
        print(f"       supported platforms: {supported_platforms} | supported os: {supported_os}", file=sys.stderr)
        logger.warning("Base profile '%s' is incompatible with detected host", args.base_profile)
        return 3

    # ---- Add-on compatibility (fatal, mirrors filtered add-on list in Step 2) ----
    compatible_addons = {key for key, _ in processor.compatible_profiles(args.base_profile, platform_key, os_key)}
    incompatible = [a for a in args.addons if a not in compatible_addons]
    if incompatible:
        for addon_key in incompatible:
            entry = addon_profiles.get(addon_key) or {}
            allowed_bases = entry.get("base-profiles") or []
            reasons = processor.addon_compatibility_issues(addon_key, platform_key, os_key)
            if allowed_bases and args.base_profile not in allowed_bases:
                reasons.insert(
                    0,
                    f"'{addon_key}' requires base profile {allowed_bases} "
                    f"(selected: '{args.base_profile}')",
                )
            print(f"error: add-on profile '{addon_key}' is not compatible with this selection:", file=sys.stderr)
            for reason in reasons or ["incompatible with detected host"]:
                print(f"       - {reason}", file=sys.stderr)
            logger.warning("Add-on '%s' is not compatible with the selected profile/host", addon_key)
        return 3

    # ---- Resolve package list ----
    packages = list(processor.profile_packages(args.base_profile, platform_key, os_key))
    for addon_key in args.addons:
        packages.extend(processor.addon_packages(addon_key, platform_key, os_key))
    packages = _dedupe(packages)

    if not packages:
        print("error: resolved package list is empty — nothing to install", file=sys.stderr)
        return 3

    prerequisites = required_prerequisites(processor, [args.base_profile, *args.addons], os_key)
    debconf_selections = required_debconf_selections(processor, [args.base_profile, *args.addons])
    repos = list((os_entry or {}).get("repositories", {}).values())

    hidden_packages: list[str] = []
    for _key in [args.base_profile, *args.addons]:
        hidden_packages.extend(processor.hidden_os_packages(_key, platform_key, os_key))

    # ---- Summary ----
    print(f"\nBase profile : {base_display} ({args.base_profile})")
    print(f"Add-ons      : {', '.join(args.addons) if args.addons else '(none)'}")
    print(f"Platform     : {platform_display}")
    print(f"OS           : {os_display}")
    print(f"TSN NIC      : {host['tsn_description']}")
    print(f"IPU          : {host['ipu_description']}")
    print("Packages to install:")
    for name in packages:
        print(f"  - {name}")
    if prerequisites.get("packages"):
        print("Prerequisite packages:")
        for name in prerequisites["packages"]:
            print(f"  - {name}")
    if debconf_selections:
        print("Preseeded configuration:")
        for package, question, _type, value in debconf_selections:
            print(f"  - {package}: {question} = {value}")

    # No confirmation prompt — this path is unattended for scripted validation.
    print("\nRequirements met — proceeding with installation...\n")
    code = _run_install_blocking(
        packages, repos, prerequisites, hidden_packages, debconf_selections,
        package_report_path=package_report_path,
        package_report_callback=package_report_callback,
    )

    if code == 0:
        print("\n\u2714 Installation completed successfully.")
        print("  A system restart is recommended to apply all changes.")
    elif code in (130, 143, 129):
        print(f"\nInstallation cancelled (exit code {code}); installation process has exited.", file=sys.stderr)
    else:
        print(f"\n\u2718 Installation failed (exit code {code}). See output above for details.", file=sys.stderr)
    return code


def _run_install_blocking(packages: list[str], repos: list[dict], prerequisites: dict,
                          force_packages: list[str] | None = None,
                          debconf_selections: list[tuple] | None = None,
                          package_report_path: Path | None = None,
                          package_report_callback: Callable[[str], None] | None = None) -> int:
    """Run edgepack_shared.install_logic.run_install synchronously and stream its output."""
    done = threading.Event()
    result = {"code": 1}
    downloaded: dict[str, tuple[str, str]] = {}
    resolved: set[str] = set()

    def _on_output(line: str) -> None:
        stripped = line.strip()
        parsed = parse_get_line(stripped)
        if parsed:
            name, version, origin = parsed
            downloaded[name] = (version, origin)
        if stripped.startswith("Resolved:"):
            resolved.update(parse_resolved_line(stripped))
        sys.stdout.write(line if line.endswith("\n") else line + "\n")
        sys.stdout.flush()

    def _on_finished(code: int = 0) -> None:
        result["code"] = code
        done.set()

    pending_signal = None
    cancelled_signal = None
    refusal_reported = False
    previous_handlers = {}

    def _on_signal(signum, frame):
        nonlocal pending_signal
        if pending_signal is None:
            pending_signal = signum

    try:
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            previous_handlers[signum] = signal.signal(signum, _on_signal)
        handle = run_install(
            [(name, "") for name in packages],
            repos,
            output_callback=_on_output,
            finished_callback=_on_finished,
            sudo_password=None,
            prerequisites=prerequisites,
            force_packages=force_packages,
            debconf_selections=debconf_selections,
        )
        while not done.wait(0.1):
            if pending_signal is not None:
                signum, pending_signal = pending_signal, None
                if cancelled_signal is None and not refusal_reported:
                    if handle.cancel():
                        cancelled_signal = signum
                        print("Cancellation requested; waiting for the installation process to exit...", file=sys.stderr)
                    elif handle.dpkg_started:
                        refusal_reported = True
                        print("Cannot cancel: package configuration has started. Waiting for installation to finish safely.", file=sys.stderr)
        if cancelled_signal is not None:
            return 128 + cancelled_signal
        code = result["code"]
        if code == 0 and package_report_path is not None:
            try:
                expected = resolved | {name.split("=")[0] for name in prerequisites.get("packages", [])}
                expected = expected | apt_dependency_closure(expected)
                cached = apt_cache_policy_origins(expected - set(downloaded))
                path = write_package_summary([str(package_report_path)], downloaded, cached)
                if path:
                    if package_report_callback:
                        package_report_callback(path)
                    print(f"Full Software BOM saved to: {path}")
            except Exception:
                print("warning: could not save the Software BOM; installation result is unchanged", file=sys.stderr)
        return 128 - code if code < 0 else code
    finally:
        for signum, previous in previous_handlers.items():
            signal.signal(signum, previous)
