# shared/package_status.py — detect previously installed Intel Edge packages
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Query which packages are already installed on the host and compare versions.

Used at startup to scan for previously installed Intel Edge packages so that
the installer UI can warn the user that continuing will update or replace them.
"""

import logging
import os
import shutil
import subprocess  # nosec B404 - reviewed: only used with list-form args (no shell=True), fixed dpkg-query/flatpak-spawn commands, no untrusted input passed to a shell


def _host_prefix():
    """Return the command prefix needed to reach the host package database.

    Inside Flatpak the app is sandboxed, so dpkg-query has to run on the host
    via flatpak-spawn (mirroring how install_logic runs apt).
    """
    if shutil.which("flatpak-spawn") or os.path.exists("/usr/bin/flatpak-spawn"):
        return ["flatpak-spawn", "--host"]
    return []


def query_installed_versions(package_names):
    """Return {package: version} for the given names that are currently installed.

    Only packages whose dpkg status is fully installed ('ii') are returned;
    config-files-only or not-installed packages are omitted.

    Example:
        Input:  ['intel-edge-ai', 'intel-edge-media', 'not-here']
        Output: {'intel-edge-ai': '1.0~noble1'}
    """
    names = [n for n in package_names if n]
    if not names:
        return {}

    cmd = _host_prefix() + [
        "dpkg-query", "-W",
        "-f=${Package}\\t${Version}\\t${db:Status-Abbrev}\\n",
    ] + names
    try:
        proc = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )
    except Exception as e:
        # Not fatal (this only drives an informational "already installed" banner),
        # but must not fail silently — record it instead of pretending nothing is installed.
        logging.getLogger("tui_log").warning("query_installed_versions: dpkg-query failed to run: %s", e)
        return {}

    installed = {}
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        name, version, status = parts[0], parts[1], parts[2].strip()
        if status.startswith("ii") and version:
            installed[name] = version
    return installed


def compare_versions(a, b):
    """Compare two Debian version strings; return -1, 0 or 1.

    Delegates to ``dpkg --compare-versions`` on the host so we don't have to
    reimplement deb-version(7) ordering rules ourselves.
    """
    def _cmp(op):
        proc = subprocess.run(
            _host_prefix() + ["dpkg", "--compare-versions", a, op, b],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return proc.returncode == 0

    if _cmp("lt"):
        return -1
    if _cmp("gt"):
        return 1
    return 0
