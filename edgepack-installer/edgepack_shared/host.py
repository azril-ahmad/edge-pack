# edgepack_shared/host.py — host OS detection helpers (UI-agnostic)
#
# SPDX-License-Identifier: MIT

"""Read /etc/os-release on the host.

The codename returned by ``host_codename()`` (e.g. ``noble``, ``resolute``)
is exactly the apt repository suite name on Debian/Ubuntu — used by the
installer to write the ``deb`` source line.
"""
import os
# Reviewed: list-form args only (no shell=True), fixed uname/lscpu commands, no untrusted input.
import subprocess  # nosec B404
import re

PCI_VENDOR_INTEL = 0x8086

# The TSN NIC and IPU PCI id tables live in data/edgepacks-template.yml under
# 'hardware_ids' so they can be extended without a code change; callers pass
# them in via Processor.tsn_nic_device_ids() / .ipu_device_ids(). This module
# stays free of any YAML dependency (processor.py imports host.py, not the
# other way round). The driver-name fallbacks below are structural rather than
# data, so they stay here and keep detection working even if the table is empty.

# igc only ever binds TSN-capable Intel NICs (I225 and later).
_TSN_NIC_DRIVERS_FALLBACK = ('igc',)

# Matches any generation of the IPU kernel driver: intel-ipu7, intel_ipu8, …
# so a device newer than the YAML table is still recognised once a driver binds.
_IPU_DRIVER_RE = re.compile(r'intel[-_]ipu\d*')


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
        except (OSError, UnicodeError):
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
    result = subprocess.run(['/usr/bin/uname', '-a'], capture_output=True, text=True)  # nosec B603
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
        result = subprocess.run(['/usr/bin/uname', '-r'], capture_output=True, text=True)  # nosec B603
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
    except OSError:
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
    except OSError:
        # Fall back to lscpu below when procfs is unavailable.
        pass

    try:
        result = subprocess.run(  # nosec B603
            ['/usr/bin/lscpu'],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                if line.lower().startswith('model name:'):
                    return line.split(':', 1)[1].strip()
    except (OSError, subprocess.SubprocessError):
        # lscpu is optional; an unreadable CPU model is handled by the caller.
        pass
    return ''


def _read_pci_id(path: str) -> int | None:
    """Read a sysfs PCI id file ('0x8086\\n') and return it as an int, or None."""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return int(f.read().strip(), 16)
    except Exception:
        return None


def host_tsn_nics(device_ids: dict | None = None,
                  drivers: list | None = None) -> list[dict]:
    """Return the TSN-capable network interfaces present on the host.

    Scans sysfs (not ``lspci``, which is not installed by default on minimal
    images) for Intel Ethernet controllers of the I225 generation or later —
    the first Intel discrete NICs with hardware Time-Sensitive Networking
    (802.1Qbv/Qbu/AS) offload.  A device qualifies when its PCI vendor is
    Intel and either its device id is in *device_ids* or it is bound to one of
    *drivers* (defaults to ``igc``, which only binds TSN-capable parts).

    *device_ids* comes from ``Processor.tsn_nic_device_ids()`` (YAML section
    ``hardware_ids.tsn_nic``); when omitted, only the driver check applies.

    Example:
        Input:  (no args)
        Output: [{'interface': 'enp1s0', 'model': 'I226-V',
                  'device_id': '0x125c', 'driver': 'igc'}]
    """
    device_ids = device_ids or {}
    drivers = drivers if drivers else list(_TSN_NIC_DRIVERS_FALLBACK)
    nics: list[dict] = []
    net_root = '/sys/class/net'
    try:
        interfaces = sorted(os.listdir(net_root))
    except OSError:
        return nics

    for iface in interfaces:
        device_dir = os.path.join(net_root, iface, 'device')
        vendor_id = _read_pci_id(os.path.join(device_dir, 'vendor'))
        if vendor_id != PCI_VENDOR_INTEL:
            continue
        device_id = _read_pci_id(os.path.join(device_dir, 'device'))
        try:
            driver = os.path.basename(os.readlink(os.path.join(device_dir, 'driver')))
        except OSError:
            driver = ''
        model = device_ids.get(device_id) if device_id is not None else None
        if model is None and driver not in drivers:
            continue
        nics.append({
            'interface': iface,
            'model': model or f'Intel 2.5G Ethernet ({driver})',
            'device_id': f'0x{device_id:04x}' if device_id is not None else '',
            'driver': driver,
        })
    return nics


def host_tsn_summary(nics: list[dict] | None = None) -> str:
    """One-line display string for the detected TSN NICs, or 'Not detected'.

    Pass *nics* (from a prior ``host_tsn_nics()`` call) to avoid rescanning.

    Example: 'I226-V (enp1s0), I225-LM (enp2s0)'
    """
    if nics is None:
        nics = host_tsn_nics()
    if not nics:
        return 'Not detected'
    return ', '.join(f"{nic['model']} ({nic['interface']})" for nic in nics)


def host_ipu_pci_devices(device_ids: dict | None = None) -> list[dict]:
    """Return the Intel IPU PCI devices present on the host.

    Reads sysfs rather than shelling out to ``lspci`` (not installed by default
    on minimal images) for the equivalent of::

        00:05.0 Multimedia controller [0480]: Intel Corporation Panther Lake IPU
                [8086:b05d] (rev 04)
                Kernel driver in use: intel-ipu7

    A device qualifies when its Intel PCI device id is in *device_ids* (which
    also catches an IPU whose driver failed to load) or when it is bound to an
    ``intel-ipuN`` driver of any generation.

    *device_ids* comes from ``Processor.ipu_device_ids()`` (YAML section
    ``hardware_ids.ipu``); when omitted, only the driver check applies.

    Example:
        Input:  (no args)
        Output: [{'slot': '0000:00:05.0', 'device_id': '0xb05d',
                  'model': 'IPU7P5 (Panther Lake)', 'driver': 'intel-ipu7'}]
    """
    devices: list[dict] = []
    pci_root = '/sys/bus/pci/devices'
    try:
        slots = sorted(os.listdir(pci_root))
    except OSError:
        return devices

    for slot in slots:
        device_dir = os.path.join(pci_root, slot)
        try:
            driver = os.path.basename(os.readlink(os.path.join(device_dir, 'driver')))
        except OSError:
            driver = ''
        device_id = _read_pci_id(os.path.join(device_dir, 'device'))
        model = None
        if _read_pci_id(os.path.join(device_dir, 'vendor')) == PCI_VENDOR_INTEL:
            model = (device_ids or {}).get(device_id)
        if model is None and not _IPU_DRIVER_RE.fullmatch(driver):
            continue
        devices.append({
            'slot': slot,
            'device_id': f'0x{device_id:04x}' if device_id is not None else '',
            'model': model or '',
            'driver': driver.replace('_', '-'),
        })
    return devices


def host_ipu_status(pci_devices: list[dict] | None = None,
                    device_ids: dict | None = None) -> dict:
    """Summarise Intel IPU presence from the PCI bus — version-agnostic.

    Nothing here is tied to a specific IPU generation: any ``intel-ipuN``
    driver (ipu7, ipu8, ipu9, …) counts and the PCI device id is looked up in
    the caller-supplied *device_ids* table (YAML ``hardware_ids.ipu``) purely
    to put a readable name on the device.

    Pass *pci_devices* (from a prior ``host_ipu_pci_devices()`` call) to avoid
    rescanning.

    Returned keys:
      present      : an IPU was found on the PCI bus.
      driver_bound : an ``intel-ipuN`` driver is bound to it.
      driver       : e.g. 'intel-ipu7', 'intel-ipu8'.
      model        : e.g. 'IPU7P5 (Panther Lake)', '' if the id is unknown.
      device_id    : PCI device id, e.g. '0xb05d'.
      slot         : PCI slot, e.g. '0000:00:05.0'.
      healthy      : present with a driver bound.
    """
    if pci_devices is None:
        pci_devices = host_ipu_pci_devices(device_ids)

    present = bool(pci_devices)
    driver_bound = any(dev.get('driver') for dev in pci_devices)
    return {
        'present': present,
        'driver_bound': driver_bound,
        'driver': next((dev['driver'] for dev in pci_devices if dev.get('driver')), ''),
        'model': next((dev['model'] for dev in pci_devices if dev.get('model')), ''),
        'device_id': next((dev['device_id'] for dev in pci_devices if dev.get('device_id')), ''),
        'slot': pci_devices[0]['slot'] if pci_devices else '',
        'healthy': present and driver_bound,
    }


def _ipu_label(status: dict) -> str:
    """'IPU7P5 (Panther Lake) (0xb05d)' — best available name for the device."""
    label = status.get('model') or status.get('driver') or 'Intel IPU'
    if status.get('device_id'):
        label += f" ({status['device_id']})"
    return label


def host_ipu_summary(status: dict | None = None) -> str:
    """One-line display string for the IPU state.

    Examples: 'IPU7P5 (Panther Lake) (0xb05d) — OK',
    'IPU7P5 (Panther Lake) (0xb05d) — no driver bound', 'Not detected'
    """
    if status is None:
        status = host_ipu_status()
    if not status.get('present'):
        return 'Not detected'
    label = _ipu_label(status)
    return f'{label} — OK' if status.get('driver_bound') else f'{label} — no driver bound'


def host_ipu_warning(status: dict | None = None) -> str:
    """Human-readable warning for a missing or driverless IPU, or '' when healthy."""
    if status is None:
        status = host_ipu_status()
    if not status.get('present'):
        return 'No Intel IPU detected — the camera stack will not work'
    if not status.get('driver_bound'):
        return (f'{_ipu_label(status)} has no kernel driver bound — '
                'the IPU driver is missing or failed to load')
    return ''




