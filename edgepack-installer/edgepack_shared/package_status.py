# shared/package_status.py — detect previously installed Intel Edge packages
#
# SPDX-License-Identifier: MIT

"""Query which packages are already installed on the host and compare versions.

Used at startup to scan for previously installed Intel Edge packages so that
the installer UI can warn the user that continuing will update or replace them.
"""

import logging
import os
import shutil
# Reviewed: list-form args only (no shell=True), fixed dpkg/apt-cache commands, names validated.
import subprocess  # nosec B404

from .dep_resolver import valid_package_name


def _host_prefix():
    """Return the command prefix needed to reach the host package database.

    Inside Flatpak the app is sandboxed, so dpkg-query has to run on the host
    via flatpak-spawn (mirroring how install_logic runs apt).
    """
    if shutil.which("flatpak-spawn") or os.path.exists("/usr/bin/flatpak-spawn"):
        return ["flatpak-spawn", "--host"]
    return []


def compare_versions(a, b):
    """Compare two Debian version strings; return -1, 0 or 1.

    Delegates to ``dpkg --compare-versions`` on the host so we don't have to
    reimplement deb-version(7) ordering rules ourselves.
    """
    def _cmp(op):
        proc = subprocess.run(  # nosec B603
            _host_prefix() + ["dpkg", "--compare-versions", a, op, b],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15,
        )
        return proc.returncode == 0

    if _cmp("lt"):
        return -1
    if _cmp("gt"):
        return 1
    return 0


# ---------------------------------------------------------------------------
# Candidate-version reporting (requires a refreshed apt index)
# ---------------------------------------------------------------------------

# Package states reported to the wizard. UNAVAILABLE means apt knows nothing
# about the package, which normally indicates the index refresh failed.
STATE_NEW = "new"
STATE_CURRENT = "current"
STATE_UPGRADE = "upgrade"
STATE_DOWNGRADE = "downgrade"
STATE_UNAVAILABLE = "unavailable"


def _parse_apt_cache_policy(text):
    """Parse ``apt-cache policy`` output into {package: (installed, candidate)}.

    Versions reported by apt as ``(none)`` come back as None.
    """
    result = {}
    current = None
    for line in text.splitlines():
        if not line.strip():
            continue
        if not line[0].isspace() and line.rstrip().endswith(":"):
            current = line.strip().rstrip(":")
            result[current] = [None, None]
            continue
        if current is None:
            continue
        stripped = line.strip()
        for label, index in (("Installed:", 0), ("Candidate:", 1)):
            if stripped.startswith(label):
                value = stripped[len(label):].strip()
                if value and value != "(none)":
                    result[current][index] = value
                break
    return {name: tuple(pair) for name, pair in result.items()}


def query_package_status(package_names, timeout=60):
    """Return {package: {installed, candidate, state}} for the given names.

    ``apt-cache policy`` reports both the installed and the repository
    candidate version in one call, so this supersedes a separate dpkg-query
    scan. The apt index must have been refreshed first (see
    ``install_logic.prepare_repositories``) or every package comes back as
    ``STATE_UNAVAILABLE``.
    """
    names = [n for n in package_names if n]
    if not names:
        return {}
    # A name apt could mistake for an option is reported unavailable, not passed through.
    rejected = [n for n in names if not valid_package_name(n)]
    if rejected:
        logging.getLogger("tui_log").warning(
            "query_package_status: skipping %d invalid package name(s)", len(rejected)
        )
        names = [n for n in names if valid_package_name(n)]
    unavailable = {n: {"installed": None, "candidate": None, "state": STATE_UNAVAILABLE}
                   for n in rejected if isinstance(n, str)}
    if not names:
        return unavailable

    try:
        proc = subprocess.run(  # nosec B603
            _host_prefix() + ["apt-cache", "policy"] + names,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            timeout=timeout,
        )
    except Exception as e:
        # A hang or failure here must not leave the wizard spinning forever;
        # report every package as unavailable and let the caller carry on.
        logging.getLogger("tui_log").warning(
            "query_package_status: apt-cache policy failed to run: %s", e
        )
        return {**unavailable,
                **{n: {"installed": None, "candidate": None, "state": STATE_UNAVAILABLE} for n in names}}

    parsed = _parse_apt_cache_policy(proc.stdout or "")

    status = dict(unavailable)
    for name in names:
        installed, candidate = parsed.get(name, (None, None))
        status[name] = {
            "installed": installed,
            "candidate": candidate,
            "state": _classify(installed, candidate),
        }
    return status


def _classify(installed, candidate):
    """Derive the reported state from an installed/candidate version pair."""
    if not candidate:
        return STATE_UNAVAILABLE
    if not installed:
        return STATE_NEW
    try:
        result = compare_versions(installed, candidate)
    except Exception as e:
        # dpkg is the only authority on deb version ordering; if it cannot be
        # run we must not guess an ordering, and must not kill the caller.
        logging.getLogger("tui_log").warning("_classify: dpkg compare failed: %s", e)
        return STATE_UNAVAILABLE
    if result < 0:
        return STATE_UPGRADE
    if result > 0:
        return STATE_DOWNGRADE
    return STATE_CURRENT
