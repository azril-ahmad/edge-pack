# edgepack_shared/host.py — host OS detection helpers (UI-agnostic)
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Read /etc/os-release on the host.

The codename returned by ``host_codename()`` (e.g. ``noble``, ``resolute``)
is exactly the apt repository suite name on Debian/Ubuntu — used by the
installer to write the ``deb`` source line.
"""
import subprocess  # nosec B404 - reviewed: only used with list-form args (no shell=True), fixed uname commands, no untrusted input passed to a shell
import re

def _parse_os_release_text(text: str) -> dict:
    """Parse the contents of an os-release file into a dict. Pure/fuzzable."""
    info = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k, v = line.split('=', 1)
        info[k] = v.strip().strip('"')
    return info


def read_os_release():
    """Parse the host's os-release into a dict; return {} if unreadable.

    Prefers ``/run/host/os-release`` so the file reflects the host even when
    running inside a flatpak sandbox (whose ``/etc/os-release`` belongs to
    the runtime, e.g. org.gnome.Platform). Falls back to ``/etc/os-release``.

    Example:
        Input:  (no args)
        Output: {'NAME': 'Ubuntu', 'VERSION_ID': '24.04',
                 'VERSION_CODENAME': 'noble', ...}
    """
    for path in ('/run/host/os-release', '/etc/os-release'):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                info = _parse_os_release_text(f.read())
            if info:
                return info
        except Exception:
            continue
    return {}

def host_os_dot_version(info: dict | None = None) -> str:
    """Return the full OS version string including any dot release (e.g. '24.04.2').

    Ubuntu's /etc/os-release has two relevant fields:
      - VERSION_ID  e.g. '24.04'                        (major.minor only, never changes)
      - VERSION     e.g. '24.04.1 LTS (Noble Numbat)'   (includes the dot release)

    We extract the leading numeric portion of VERSION so we get '24.04.1' rather
    than just '24.04'.  Falls back to VERSION_ID if VERSION is absent or does not
    start with a parseable version number.

    Pass *info* (from a prior ``read_os_release()`` call) to avoid reading the
    file a second time.  When omitted the file is read here.

    Example return values: '24.04', '24.04.1', '24.04.5', '26.04.2'
    """
    if info is None:
        info = read_os_release()
    version_full = info.get('VERSION', '')
    match = re.match(r'([\d][\d.]*)', version_full)
    if match:
        return match.group(1).rstrip('.')
    # Fallback: VERSION_ID (no dot release component, e.g. '24.04')
    return info.get('VERSION_ID', '')


def host_kernel_type() -> str | None:
    """Return the detected kernel type, or None if uname failed.

    Error checks are performed before any processing of the command output —
    a non-zero return code means the command itself failed and the result
    must not be used (prevents silently acting on garbage output).
    """
    # Run the command and capture output; check return code BEFORE parsing.
    result = subprocess.run(['uname', '-a'], capture_output=True, text=True)
    if result.returncode != 0:
        return None  # command failed — do not process stale/partial output
    output = result.stdout

    # Check for specific kernel types
    if "PREEMPT_RT" in output:
        return "base-realtime"
    elif "PREEMPT_DYNAMIC" in output:
        return "base-standard"
    elif "PREEMPT" in output:
        return "base-realtime"

    return None


def host_kernel_release() -> str:
    """Return the raw kernel release string from ``uname -r``, or '' if unreadable.

    Example:
        Input:  (no args)
        Output: '6.8.0-1015-intel'
    """
    try:
        result = subprocess.run(['uname', '-r'], capture_output=True, text=True)
        # Check return code before using output — a failed command must not
        # produce a (possibly stale or empty) kernel release string.
        if result.returncode != 0:
            return ''
        return result.stdout.strip()
    except Exception:
        return ''


def host_kernel_dot_version(kernel_release: str | None = None) -> str:
    """Extract the leading dotted version number from a kernel release string.

    Pass *kernel_release* (from a prior ``host_kernel_release()`` call) to avoid
    invoking ``uname -r`` a second time.  When omitted, it is read here.

    Example: '6.8.0-1015-intel' -> '6.8.0'
    """
    if kernel_release is None:
        kernel_release = host_kernel_release()
    match = re.match(r'([\d]+(?:\.[\d]+)*)', kernel_release)
    return match.group(1) if match else ''


def host_kernel_is_ubuntu() -> bool:
    """Return True if the running kernel is confirmed to be Ubuntu-built.

    Ubuntu (and derivatives) patch their kernels to ship ``/proc/version_signature``,
    e.g. ``'Ubuntu 6.8.0-1015.16-generic 6.8.0'`` — a file that simply doesn't
    exist on non-Ubuntu-built kernels (mainline, other distros, locally-compiled).
    This is read directly from procfs, which reflects the real host kernel even
    inside a Flatpak sandbox (unlike /etc files, procfs isn't sandboxed per-container).

    Falls back to checking /proc/version's '-Ubuntu' build tag if the signature
    file is missing.
    """
    try:
        with open('/proc/version_signature', 'r', encoding='utf-8') as f:
            if f.read().strip().startswith('Ubuntu'):
                return True
    except Exception:
        # File may not exist on all systems (e.g., /proc/version_signature)
        pass
    try:
        with open('/proc/version', 'r', encoding='utf-8') as f:
            return 'Ubuntu' in f.read()
    except Exception:
        return False

def host_cpu_model():
    """Return the host CPU's "model name" string, or '' if unreadable.

    Reads /proc/cpuinfo's first ``model name`` line, falling back to the
    structured ``lscpu`` output when procfs is unavailable. The result is used
    to match the host against ``platforms[].model_names`` keywords in the YAML
    template.

    Example:
        Input:  (no args)  # running on a Panther Lake laptop
        Output: 'Intel(R) Core(TM) Ultra 7 388H'
    """
    try:
        with open('/proc/cpuinfo', 'r', encoding='utf-8') as f:
            for line in f:
                if line.lower().startswith('model name'):
                    return line.split(':', 1)[1].strip()
    except Exception:
        # Fall back to lscpu below when procfs is unavailable.
        pass

    try:
        result = subprocess.run(
            ['lscpu'],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                if line.lower().startswith('model name:'):
                    return line.split(':', 1)[1].strip()
    except Exception:
        # lscpu is optional; an unreadable CPU model is handled by the caller.
        pass
    return ''
