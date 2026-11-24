# edgepack_shared/dep_resolver.py — apt dependency resolver
#
# Runs as a standalone script inside the privileged (sudo/pkexec) shell,
# AFTER "apt-get update" has refreshed the package index for the EdgePack
# repository.  Called by install_logic._build_install_script() via:
#
#   python3 /path/to/_dep_resolver.py <pkg1> [<pkg2> ...]
#
# For each top-level package it:
#   1. Calls "apt show <pkg>" to get the available version and Depends line.
#   2. Recursively expands intel-edge* dependencies (full transitive closure),
#      pinning each to the version the EdgePack repo offers.
#   3. Records non-intel-edge deps only when the Depends line pins them with
#      an exact '=' (e.g. rpc-go), since apt would otherwise pick a newer
#      incompatible version from another source. Range constraints (>=, <=)
#      are deliberately left unpinned so apt can still find a solution that
#      satisfies every package in the transaction.
#
# Output: one "name=version" (or bare "name") per line, consumed by $PKGS
# in the install shell script.
#
# SPDX-License-Identifier: MIT

import os
import re
# Reviewed: list-form args only (no shell=True), fixed apt command, names validated below.
import subprocess  # nosec B404
import sys


_PACKAGE_NAME = re.compile(r"[a-z0-9][a-z0-9+.-]+(?::[a-z0-9]+)?\Z")


def valid_package_name(name):
    return isinstance(name, str) and _PACKAGE_NAME.fullmatch(name) is not None


def _apt_show(pkg):
    # A non-zero return code (e.g. "package not found") still yields stdout to
    # parse normally. Only a failure to *run* apt at all (missing binary, hung
    # process past the timeout, etc.) is treated as fatal — that must abort
    # loudly rather than silently resolve to an empty/incomplete package list.
    if not valid_package_name(pkg):
        raise ValueError(f"Invalid package name: {pkg!r}")
    try:
        r = subprocess.run(  # nosec B603
            ["/usr/bin/apt", "show", pkg],
            capture_output=True, text=True, timeout=30
        )
    except Exception as e:
        raise RuntimeError(f"'apt show {pkg}' failed to run: {e}") from e
    return r.stdout


def _get_version(out):
    m = re.search(r"^Version:\s+(.+)", out, re.M)
    return m.group(1).strip() if m else ""


def _get_depends(out):
    """Return list of (name, operator, version) from an apt show Depends line.

    operator/version are empty strings when no constraint is present, e.g.:
      'rpc-go (= 2.49.2-1ppa1~noble3)' -> ('rpc-go', '=',  '2.49.2-1ppa1~noble3')
      'libssl3 (>= 1.1.1)'             -> ('libssl3',  '>=', '1.1.1')
      'libc6'                          -> ('libc6',    '',   '')
    """
    m = re.search(r"^Depends:\s+(.+)", out, re.M)
    if not m:
        return []
    result = []
    for entry in m.group(1).split(","):
        entry = entry.split("|")[0].strip()  # ignore alternatives after '|'
        nm = re.match(r"^(\S+)(?:\s*\(\s*([><=!]+)\s*([^)]+)\))?", entry)
        if nm:
            name = nm.group(1)
            op  = (nm.group(2) or "").strip()
            ver = (nm.group(3) or "").strip()
            result.append((name, op, ver))
    return result


seen = set()  # packages already visited via apt show
pkgs = {}     # name -> version string (empty means unversioned)


def resolve(pkg, forced_version=""):
    """Add pkg to pkgs and recurse into dependencies.

    - intel-edge* deps: always recursed, pinned to the repo's version.
    - Non-intel-edge deps pinned with an exact '=': recorded at that version,
      not recursed into — their own deps are left for apt.
    - Everything else (range-constrained or unconstrained): left for apt, so
      pinning one package's '>=' dep cannot block another package's exact
      requirement for a different version of that same dep.
    """
    if not valid_package_name(pkg):
        raise ValueError(f"Invalid package name: {pkg!r}")
    if pkg in seen:
        return
    seen.add(pkg)
    out = _apt_show(pkg)
    pkgs[pkg] = forced_version if forced_version else _get_version(out)
    for dep_name, dep_op, dep_ver in _get_depends(out):
        if dep_name.startswith("intel-edge"):
            resolve(dep_name)
        elif dep_op == "=" and dep_name not in seen:
            seen.add(dep_name)
            pkgs[dep_name] = dep_ver


if __name__ == "__main__":  # guard needed so this module is importable (e.g. for fuzzing/tests)
    try:
        for p in sys.argv[1:]:
            resolve(p)
    except (ValueError, RuntimeError) as e:
        print(f"dep_resolver: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        for name, ver in pkgs.items():
            print(name + ("=" + ver if ver else ""))
        sys.stdout.flush()
    except BrokenPipeError:
        # The shell owning our command-substitution pipe went away (e.g. the
        # user cancelled the install). Point stdout at devnull so the
        # interpreter's shutdown flush cannot fail and print a traceback.
        with open(os.devnull, "w") as devnull:
            os.dup2(devnull.fileno(), sys.stdout.fileno())
        sys.exit(1)
