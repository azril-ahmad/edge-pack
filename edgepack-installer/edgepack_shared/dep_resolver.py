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
#   2. Recursively expands intel-edge* dependencies (full transitive closure).
#   3. For each intel-edge* package found, also records its direct
#      version-constrained non-intel-edge deps (e.g. rpc-go) — pinned to
#      the exact version from the Depends line, or to the currently available
#      version when only a range constraint is given.  Transitive deps of
#      those non-intel-edge packages are left for apt to resolve on its own.
#
# Output: one "name=version" (or bare "name") per line, consumed by $PKGS
# in the install shell script.
#
# SPDX-License-Identifier: GPL-3.0-or-later

import re
import subprocess  # nosec B404 - reviewed: only used with list-form args (no shell=True), fixed apt/dpkg commands, no untrusted input passed to a shell
import sys


def _apt_show(pkg):
    # A non-zero return code (e.g. "package not found") still yields stdout to
    # parse normally. Only a failure to *run* apt at all (missing binary, hung
    # process past the timeout, etc.) is treated as fatal — that must abort
    # loudly rather than silently resolve to an empty/incomplete package list.
    try:
        r = subprocess.run(
            ["apt", "show", pkg],
            capture_output=True, text=True, timeout=30
        )
        return r.stdout
    except Exception as e:
        print(f"dep_resolver: 'apt show {pkg}' failed to run: {e}", file=sys.stderr)
        sys.exit(1)


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

    - intel-edge* deps: always recursed (full transitive closure pinned),
      and their direct version-constrained deps are also added to pkgs.
    - Non-intel-edge deps of intel-edge* packages: added to pkgs with their
      pinned version but NOT recursed into — transitive deps of those packages
      are left for apt to resolve on its own.
    - Unconstrained deps (no version spec at all): left for apt to resolve.
    """
    if not pkg or not isinstance(pkg, str):
        return  # guard against empty or malformed package names
    if pkg in seen:
        return
    seen.add(pkg)
    out = _apt_show(pkg)
    pkgs[pkg] = forced_version if forced_version else _get_version(out)
    for dep_name, dep_op, dep_ver in _get_depends(out):
        if dep_name.startswith("intel-edge"):
            resolve(dep_name)
        elif dep_op == "=" and dep_name not in seen:
            # Exact pin from Depends line — record it, no further recursion.
            seen.add(dep_name)
            pkgs[dep_name] = dep_ver
        elif dep_op and dep_name not in seen:
            # Range-constrained dep — pin to available version, no further recursion.
            seen.add(dep_name)
            pkgs[dep_name] = _get_version(_apt_show(dep_name))
        # No constraint: let apt resolve freely; do not add to pkgs.


if __name__ == "__main__":  # guard needed so this module is importable (e.g. for fuzzing/tests)
    for p in sys.argv[1:]:
        resolve(p)

    for name, ver in pkgs.items():
        print(name + ("=" + ver if ver else ""))
