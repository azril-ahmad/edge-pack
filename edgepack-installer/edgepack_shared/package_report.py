"""Package-origin reporting shared by the wizard and unattended CLI."""

import subprocess  # nosec B404
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from .dep_resolver import valid_package_name
from .log import open_secure_log


def classify_origin(origin: str | None) -> str:
    if not origin:
        return "Unknown"
    if "intel" in origin.lower():
        return "Intel"
    host = (urlparse(origin).hostname or "").lower()
    if host == "ubuntu.com" or host.endswith(".ubuntu.com"):
        return "Ubuntu"
    return "Unknown"


def parse_get_line(line: str) -> tuple[str, str, str] | None:
    """Return package, version and URL for package fetches, excluding index fetches."""
    if not line.startswith("Get:"):
        return None
    tokens = line.split()
    if len(tokens) < 7:
        return None
    url, package, version = tokens[1], tokens[4], tokens[6]
    if package.startswith("["):
        return None
    return package, version, url


def parse_resolved_line(line: str) -> set[str]:
    payload = line[len("Resolved:"):].strip()
    return {entry.partition("=")[0] for entry in payload.split() if entry}


def apt_cache_policy_origins(package_names: set[str]) -> dict[str, tuple[str, str]]:
    """Read cached package versions and origins in one bounded apt-cache lookup."""
    names = sorted(name for name in package_names if valid_package_name(name))
    if not names:
        return {}
    try:
        result = subprocess.run(  # nosec B603
            ["/usr/bin/apt-cache", "policy", *names],
            capture_output=True, text=True, timeout=30,
        )
    except Exception:
        return {}

    origins: dict[str, tuple[str, str]] = {}
    name = None
    version = "unknown"
    origin = "unknown"
    lines = result.stdout.splitlines()
    for index, raw in enumerate(lines):
        if raw and not raw[0].isspace() and raw.endswith(":"):
            if name:
                origins[name] = (version, origin)
            name, version, origin = raw[:-1], "unknown", "unknown"
        elif raw.strip().startswith("Candidate:"):
            version = raw.split(":", 1)[1].strip()
        elif raw.strip().startswith("***") and index + 1 < len(lines):
            parts = lines[index + 1].split()
            if len(parts) >= 2:
                origin = parts[1]
    if name:
        origins[name] = (version, origin)
    return origins


def apt_dependency_closure(package_names: set[str]) -> set[str]:
    """Approximate installed hard dependencies, preserving the TUI's report scope."""
    names = sorted(name for name in package_names if valid_package_name(name))
    if not names:
        return set()
    try:
        result = subprocess.run(  # nosec B603
            ["/usr/bin/apt-cache", "depends", "--recurse", "--no-recommends", "--no-suggests",
             "--no-conflicts", "--no-breaks", "--no-replaces", "--no-enhances",
             "--installed", *names],
            capture_output=True, text=True, timeout=30,
        )
    except Exception:
        return set()
    return {raw.strip() for raw in result.stdout.splitlines() if raw and not raw[0].isspace()}


def write_package_summary(
    paths: list[str],
    downloaded: dict[str, tuple[str, str]],
    cached: dict[str, tuple[str, str]] | None = None,
) -> str | None:
    """Write the existing TUI Software BOM format, trying paths in priority order."""
    origins = {**(cached or {}), **downloaded}
    if not origins:
        return None

    buckets: dict[str, list[str]] = {"Intel": [], "Ubuntu": [], "Unknown": []}
    for name in sorted(origins):
        version, origin = origins[name]
        category = classify_origin(origin)
        tag = "" if name in downloaded else "  [cached]"
        buckets[category].append(f"  - {name} {version}  ({origin}){tag}")

    for path in paths:
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            with open_secure_log(path) as log_file:
                log_file.write(
                    "EdgePack Software BOM\n"
                    f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                    f"{'=' * 60}\n\n"
                )
                for category in ("Intel", "Ubuntu", "Unknown"):
                    entries = buckets[category]
                    log_file.write(f"{category} Packages ({len(entries)}):\n")
                    log_file.write(("\n".join(entries) if entries else "  (none)") + "\n\n")
            return path
        except OSError:
            continue
    raise OSError("Could not save the Software BOM")