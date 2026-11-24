# edgepack_shared/install_logic.py — apt installation logic (UI-agnostic)
#
# SPDX-License-Identifier: MIT

import logging
import os
import hashlib
import re
import shlex
import shutil
import signal
# Reviewed: list-form args only (no shell=True); the one `sh -c` script is built with shlex.quote.
import subprocess  # nosec B404
import sys
import threading
from urllib.parse import urlparse

# Shared with the install-time resolver so both parse Depends lines identically.
# The spec also ships dep_resolver.py as a data file for the subprocess path.
from .dep_resolver import _get_depends, valid_package_name

SOURCES_LIST_PATH = "/etc/apt/sources.list.d/edgepack.list"
PREFERENCES_PATH = "/etc/apt/preferences.d/edgepack.pref"
DEFAULT_PIN_PRIORITY = 1001

# First appearance of any of these in apt's output means dpkg has started
# modifying the system. Everything before this is index refresh / dependency
# resolution / .deb downloads, none of which touch installed package state.
DPKG_ACTIVITY_MARKERS = (
    "(Reading database",
    "Selecting previously unselected package",
    "Preparing to unpack",
    "Unpacking ",
    "Setting up ",
    "Processing triggers for ",
    "dpkg: ",
)


def _proxy_env_exports() -> str:
    """Preserve user-supplied network configuration across privilege elevation."""
    exports = []
    for variable in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "no_proxy", "NO_PROXY"):
        value = os.environ.get(variable)
        if value is not None:
            exports.append(f"export {variable}={shlex.quote(value)}\n")
    return "".join(exports)


# ---------------------------------------------------------------------------
# Path resolver for _dep_resolver.py
#
# The resolver runs as a standalone script inside the privileged shell after
# "apt-get update".  In a PyInstaller bundle it is extracted alongside the
# other data files under sys._MEIPASS; in a normal source checkout it lives
# next to this module in edgepack_shared/.
# ---------------------------------------------------------------------------

def _resolver_path() -> str:
    """Return the absolute path to dep_resolver.py for both frozen and source runs."""
    if getattr(sys, 'frozen', False):
        # PyInstaller bundle: data files are under sys._MEIPASS
        return os.path.join(sys._MEIPASS, 'edgepack_shared', 'dep_resolver.py')
    # Normal source checkout: file is in the same directory as this module
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), 'dep_resolver.py')


PREREQ_KEYRING_DIR = "/etc/apt/keyrings"
PREREQ_SOURCES_PATH = "/etc/apt/sources.list.d/edgepack-prereq.list"


# Developer-only opt-out from the https-only repository policy
# (--allow-insecure-repos). A template alone can never turn this on.
_ALLOW_INSECURE_REPOS = False

# GPG v4 fingerprints are 40 hex chars, v5 are 64.
_FINGERPRINT_RE = re.compile(r"\A(?:[0-9A-F]{40}|[0-9A-F]{64})\Z")


def set_allow_insecure_repos(enabled: bool) -> None:
    """Permit plain-http repository URLs for this run (developer flag)."""
    global _ALLOW_INSECURE_REPOS
    _ALLOW_INSECURE_REPOS = bool(enabled)


def allow_insecure_repos() -> bool:
    """True when the https-only repository policy has been relaxed for this run."""
    return _ALLOW_INSECURE_REPOS


def _keyring_path_for(url):
    """Return a stable keyring path for *url*.

    Derived from a digest rather than ``hash()`` so the same repository maps to
    the same file across processes — the startup repo-setup stage and the later
    install stage must not each leave their own copy behind.
    """
    digest = hashlib.sha256(str(url).encode("utf-8")).hexdigest()[:16]
    return f"{PREREQ_KEYRING_DIR}/edgepack-{digest}.gpg"


# wget's default read timeout is 900s and it retries 20 times, so an
# unreachable host looks like a hang rather than a failure.
_WGET_TIMEOUT_ARGS = "--timeout=30 --tries=2"


def _gpg_available_cmd():
    """Shell snippet that installs gnupg when the host does not already have it."""
    return (
        "command -v gpg >/dev/null 2>&1 || "
        "{ apt-get update && apt-get install -y gnupg; } || exit $?"
    )


def _key_download_cmd(key_url, keyring_path, fingerprint=""):
    """Shell command fetching *key_url* and dearmoring it into *keyring_path*.

    With *fingerprint* set the key is staged in a temp file and only moved into
    place once it matches, so an unverified key never reaches the keyring
    directory where apt would pick it up. The fingerprint check also catches a
    failed download that POSIX sh would otherwise hide (no ``pipefail``: wget
    can fail while gpg still exits 0 on the empty stream).
    """
    url_q = shlex.quote(key_url)
    path_q = shlex.quote(keyring_path)
    if not fingerprint:
        return (
            f"wget {_WGET_TIMEOUT_ARGS} -qO- {url_q} | "
            f"gpg --batch --yes --dearmor -o {path_q} && "
            f"chmod 0644 {path_q} || exit $?"
        )
    fpr_q = shlex.quote(fingerprint)
    return "\n".join([
        "ep_key_tmp=$(mktemp) || exit $?",
        f'wget {_WGET_TIMEOUT_ARGS} -qO- {url_q} | gpg --batch --yes --dearmor -o "$ep_key_tmp"' +
        ' || { rm -f "$ep_key_tmp"; exit 1; }',
        'ep_key_fpr=$(gpg --batch --with-colons --show-keys --fingerprint "$ep_key_tmp"' +
        """ | awk -F: '$1=="fpr"{print $10; exit}')""",
        f'[ "$ep_key_fpr" = {fpr_q} ] || {{ ' +
        "printf 'error: signing key fingerprint mismatch for %s " +
        "(expected %s, got %s)\\n' " +
        f'{url_q} {fpr_q} "${{ep_key_fpr:-none}}" >&2; ' +
        'rm -f "$ep_key_tmp"; exit 1; }',
        f'install -m 0644 "$ep_key_tmp" {path_q}' +
        ' || { rm -f "$ep_key_tmp"; exit 1; }',
        'rm -f "$ep_key_tmp"',
    ])


def _normalize_fingerprint(value):
    """Normalize a YAML ``repository_key_fingerprint`` to bare uppercase hex.

    Accepts the spaced and ``0x``-prefixed forms gpg and the Ubuntu keyserver
    print. Returns "" when unset; raises ValueError for anything that is not a
    valid fingerprint, so a malformed value can never reach the comparison and
    silently match nothing.
    """
    if not value:
        return ""
    text = "".join(str(value).split()).upper()
    if text.startswith("0X"):
        text = text[2:]
    if not _FINGERPRINT_RE.match(text):
        raise ValueError(f"Invalid repository_key_fingerprint: {value!r}")
    return text


def _is_https(url):
    """True when *url* is set and uses https. Single source of truth for the TLS check."""
    return bool(url) and str(url).strip().lower().startswith("https://")


def _require_https(url, label):
    """Raise ValueError if a YAML-sourced url is set but not https:// (TLS-only network policy)."""
    if url and not _is_https(url):
        raise ValueError(f"Insecure {label} rejected (must use https://): {url!r}")


def _require_repo_url_transport(url):
    """Enforce https on a repository URL unless the developer opt-out is active.

    Relaxing this costs transport integrity and privacy but not package
    authenticity: ``repository_key`` stays mandatory, so apt still rejects
    anything that is not signed by the pinned key.
    """
    if _ALLOW_INSECURE_REPOS:
        return
    _require_https(url, "repository_url")


def _require_key_transport(key_url, fingerprint):
    """Enforce https on a signing key URL unless it is pinned to a fingerprint.

    A key fetched over http with no fingerprint is the one combination that
    breaks the whole trust chain — an on-path attacker could swap the key and
    then serve arbitrary "signed" packages to a root install — so it is
    rejected even when the opt-out is active.
    """
    if _is_https(key_url):
        return
    if not _ALLOW_INSECURE_REPOS:
        _require_https(key_url, "repository_key")
    if not fingerprint:
        raise ValueError(
            f"repository_key over http requires repository_key_fingerprint: {key_url!r}"
        )


def _require_single_line(value, label):
    """Reject newlines/control characters in a value destined for an apt sources line.

    These land inside a quoted heredoc, so they cannot reach the shell, but an
    embedded newline could still forge an extra ``deb`` entry or close the
    heredoc early by emitting a bare ``EOF`` line.
    """
    text = str(value)
    if any(ch == "\n" or ch == "\r" or (ord(ch) < 32) for ch in text):
        raise ValueError(f"Invalid {label}: control characters are not allowed")
    return text


# debconf preseed. DEBIAN_FRONTEND=noninteractive (see _script_preamble) only
# suppresses the prompt -- debconf then takes the template's Default:, so the
# answer would be whatever the package maintainer happened to pick. Selections
# come from the YAML ``debconf_selections`` block of the selected profiles (see
# tui.package_logic.required_debconf_selections) as (owner, question, type,
# value) tuples, the debconf-set-selections line format.
#
# Admins change an answer afterwards with: sudo dpkg-reconfigure <package>
_DEBCONF_TOKEN_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9+._-]*\Z")
_DEBCONF_QUESTION_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9+._-]*(?:/[A-Za-z0-9+._-]+)+\Z")
# "password" is deliberately absent: answers are echoed into the install log
# header and the CLI summary, so a secret must never be preseedable from YAML.
_DEBCONF_TYPES = frozenset(
    {"select", "multiselect", "string", "boolean", "note", "text"}
)


def _debconf_entry(selection):
    """Validate one (owner, question, type, value) tuple into a set-selections line."""
    try:
        owner, question, qtype, value = selection
    except (TypeError, ValueError):
        raise ValueError("Invalid debconf selection: expected 4 fields")
    if not _DEBCONF_TOKEN_RE.match(str(owner)):
        raise ValueError(f"Invalid debconf owner: {owner!r}")
    if not _DEBCONF_QUESTION_RE.match(str(question)):
        raise ValueError(f"Invalid debconf question: {question!r}")
    if qtype not in _DEBCONF_TYPES:
        raise ValueError(f"Invalid debconf type: {qtype!r}")
    # The value is the remainder of the line, so spaces are legal but a newline
    # would forge an extra selection.
    return f"{owner} {question} {qtype} {_require_single_line(value, 'debconf value')}"


def _build_debconf_preseed_lines(selections):
    """Shell lines writing the preseeded answers into the debconf database.

    Must run before the packages are unpacked: a DKMS package reads its answer
    at configure time to pick the driver's compile options.
    """
    entries = [_debconf_entry(sel) for sel in selections or ()]
    if not entries:
        return []
    content = "\n".join(entries)
    return [
        'echo "Running: debconf-set-selections (EdgePack preseed)"',
        f"printf '%s\\n' {shlex.quote(content)} | debconf-set-selections || exit $?",
    ]


def _build_debconf_verify_lines(selections):
    """Shell lines echoing the post-install debconf state for each preseeded package.

    A preseed aimed at a question name the package does not define is a silent
    no-op, so the effective values are logged rather than assumed.
    """
    owners = sorted({str(sel[0]) for sel in selections or ()})
    lines = []
    for owner in owners:
        if not _DEBCONF_TOKEN_RE.match(owner):
            continue
        lines.append(f'echo "debconf state for {owner}:"')
        lines.append(f"debconf-show {shlex.quote(owner)} 2>&1 || true")
    return lines


def _build_prerequisite_package_lines(prerequisites):
    """Shell lines installing the gating packages the selected profiles require.

    Repository registration for these packages is handled by
    :func:`_build_repo_registration_lines`; only the install remains here.
    Gated with "|| exit $?" so a failure aborts before the main install runs.
    Returns [] when no profile declares a prerequisite package.
    """
    packages = (prerequisites or {}).get("packages") or []
    if not packages:
        return []
    # Quote each package name individually so a malicious/malformed name can't
    # inject shell metacharacters, while still passing as separate apt args.
    pkgs = " ".join(shlex.quote(p) for p in packages)
    return [
        f'echo "Running: apt-get -o Dpkg::Options::=--force-confnew install -y {pkgs}"',
        f"apt-get -o Dpkg::Options::=--force-confnew install -y {pkgs} || exit $?",
    ]


def _build_apt_preferences(repos):
    """Compose an apt preferences file pinning each edgepack repo's origin host.

    Each entry in *repos* may set ``pin_priority`` (an int) to control how
    strongly apt prefers that repo's origin over other sources; entries that
    omit it fall back to DEFAULT_PIN_PRIORITY. Pins are scoped by
    "Pin: origin <host>" (not the full URL/path) since apt preferences only
    matches on origin hostname, dist/release, or component. Returns "" when no
    repo has a resolvable host, so callers can skip writing an empty file.
    """
    seen = set()
    blocks = []
    for repo in repos:
        url = (repo.get("repository_url") or "").rstrip("/")
        dist = repo.get("repository_dist") or ""
        if not url or not dist:
            continue
        host = urlparse(url).hostname
        if not host:
            continue
        priority = repo.get("pin_priority", DEFAULT_PIN_PRIORITY)
        key = (host, priority)
        if key in seen:
            continue
        seen.add(key)
        blocks.append(f"Package: *\nPin: origin {host}\nPin-Priority: {priority}\n")

    return "\n".join(blocks)


# ---------------------------------------------------------------------------
# Repository registration — single source of truth
#
# Both the startup stage (prepare_repositories, which only refreshes the index
# so the wizard can report versions) and the install stage
# (_build_install_script) register repositories through the helpers below, so
# the index the wizard reports against is exactly the one the install later
# resolves against.
# ---------------------------------------------------------------------------

_APT_UPDATE_LINES = (
    'echo "Running: apt-get update"',
    # Bounded acquire timeouts: without them an unreachable repo (no proxy, DNS
    # blackhole) leaves apt waiting long enough to look like a hang.
    "apt-get -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 update || exit $?",
)


def _write_file_cmd(path, content):
    """Heredoc write; the quoted delimiter keeps *content* free of shell interpretation."""
    if not content.endswith("\n"):
        content += "\n"
    return f"cat > {path} <<'EOF'\n{content}EOF"


def _repo_source_lines(repos, key_cmds, require_dist):
    """Validate *repos*, append their key-download commands to *key_cmds*, return deb lines.

    Raises ValueError for a non-https URL or a repo with no ``repository_key``
    (unsigned/trusted=yes repos are not allowed). Entries with no URL — or no
    dist when *require_dist* — are skipped rather than rejected.
    """
    source_lines = []
    for repo in repos or []:
        url = str(repo.get("repository_url") or "").strip()
        dist = repo.get("repository_dist") or ""
        if not url or (require_dist and not dist):
            continue
        components = " ".join(repo.get("repository_components") or ["main"])
        key_url = repo.get("repository_key")
        if not key_url:
            raise ValueError(
                f"repository_key is required for {url!r} "
                "(unsigned/trusted=yes repos are no longer allowed)"
            )
        fingerprint = _normalize_fingerprint(repo.get("repository_key_fingerprint"))
        _require_repo_url_transport(url)
        _require_key_transport(key_url, fingerprint)
        url = _require_single_line(url, "repository_url")
        dist = _require_single_line(dist, "repository_dist")
        components = _require_single_line(components, "repository_components")
        keyring_path = _keyring_path_for(url)
        # key_url/keyring_path land in a shell command (not just file content) -- must be quoted.
        key_cmds.append(_key_download_cmd(key_url, keyring_path, fingerprint))
        line = f"deb [signed-by={keyring_path}] {url}"
        if dist:
            line += f" {dist} {components}"
        source_lines.append(line)
    return source_lines


def _build_repo_registration_lines(repos, prereq_repos=None, write_empty_prereq=False):
    """Shell lines that install signing keys and write the apt sources + preferences files.

    *repos* are the OS-level EdgePack repositories (a ``repository_dist`` is
    required); *prereq_repos* are add-on prerequisite repositories such as the
    graphics PPA. No ``apt-get update`` is emitted — callers decide when to
    refresh. Returns [] when there is nothing to register.

    *write_empty_prereq* makes the install stage remove a stale prerequisite
    sources file when the current selection needs no prerequisite repo.
    """
    key_cmds = []
    main_sources = _repo_source_lines(repos, key_cmds, require_dist=True)
    prereq_sources = _repo_source_lines(prereq_repos, key_cmds, require_dist=False)
    if not main_sources and not prereq_sources:
        return []

    # APT verifies repository metadata as an unprivileged user, so it must be
    # able to traverse the keyring directory and read each referenced key.
    lines = [f"install -d -m 0755 {PREREQ_KEYRING_DIR}", _gpg_available_cmd()]
    # Keys must land before the sources files so apt-get update can verify them.
    lines.extend(key_cmds)

    if main_sources:
        lines.append(_write_file_cmd(SOURCES_LIST_PATH, "\n".join(main_sources)))
        preferences = _build_apt_preferences(repos)
        if preferences:
            lines.append(_write_file_cmd(PREFERENCES_PATH, preferences))
    if prereq_sources:
        lines.append(_write_file_cmd(PREREQ_SOURCES_PATH, "\n".join(prereq_sources)))
    elif write_empty_prereq:
        # Truncate rather than skip: a prerequisite repo registered for an
        # earlier selection (or by an older build) must not stay enabled when
        # the current selection no longer needs it.
        lines.append(f"rm -f {PREREQ_SOURCES_PATH}")
    return lines


def _script_preamble():
    """Proxy exports plus the non-interactive apt environment every stage needs."""
    return [
        _proxy_env_exports().rstrip("\n"),
        "export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a",
    ]


def _build_install_script(packages, repos, prerequisites=None, debconf_selections=None):
    """Compose the shell script that registers the repositories, refreshes the index, then installs.

    Each entry in *repos* is a repository config dict from the YAML ``repositories`` section,
    containing ``repository_url``, ``repository_dist``, ``repository_components``, and a required
    ``repository_key`` URL. The key is downloaded and used with [signed-by=...]; a repo with a URL
    and dist but no key raises ``ValueError`` (unsigned/trusted=yes repos are no longer allowed).

    *prerequisites* (optional) is a ``{repositories, packages}`` dict. Its repositories are
    registered alongside the main ones by the shared
    :func:`_build_repo_registration_lines`, and its packages are installed
    immediately after the index refresh, before the main install.

    *debconf_selections* (optional) is a list of (owner, question, type, value)
    tuples preseeded into the debconf database before anything is unpacked.

    Registration is idempotent, so this stays correct whether or not
    :func:`prepare_repositories` already ran at startup.
    """
    prerequisites = prerequisites or {}
    prereq_repos = [
        r for r in (prerequisites.get("repositories") or []) if r.get("repository_url")
    ]
    main_repos = [
        r for r in (repos or []) if r.get("repository_url") and r.get("repository_dist")
    ]

    if not main_repos:
        # Never install gating packages against a half-configured apt state, and
        # never write an empty sources file — abort before the shell touches apt.
        print(
            "error: install aborted — repository configuration incomplete",
            file=sys.stderr,
        )
        return ""

    registration = _build_repo_registration_lines(
        main_repos, prereq_repos, write_empty_prereq=True,
    )

    # Quote each package name individually (see _build_prerequisite_package_lines) --
    # this is the exact command-substitution boundary Bandit's B404 flags.
    pkg_names = " ".join(shlex.quote(name) for name, _ in packages)
    resolver = shlex.quote(_resolver_path())

    # Notes on script structure:
    #   - No global "set -e": we need the diagnostic block to run after a
    #     failed apt-get install, then re-exit with the original code.
    #   - "apt-get update || exit $?" still aborts early on update failure.
    #   - _dep_resolver.py is bundled as a data file (PyInstaller) or lives
    #     next to this module (source run).  _resolver_path() returns the
    #     correct path for both cases — no temp file writing needed.
    #   - "--allow-downgrades" lets apt pin a version lower than whatever is
    #     currently installed when the Depends line requires it.
    #   - "-V" (--verbose-versions) prints the exact version being installed /
    #     upgraded / removed for every package.
    install_lines = [
        # Disable glob expansion and split the resolver output on newlines only,
        # so the unquoted $PKGS below (run as root) cannot glob-expand against /
        # or word-split on whitespace in a version string (review Issue 1).
        "set -f; IFS='\n'",
        f"PKGS=$(python3 {resolver} {pkg_names}) || exit $?",
        'PKGS_INLINE=$(echo "$PKGS" | tr "\\n" " ")',
        'echo "Resolved: $PKGS_INLINE"',
        'echo "Running: apt-get -o Dpkg::Options::=--force-confnew install --allow-downgrades -y -V $PKGS_INLINE"',
        "apt-get -o APT::Status-Fd=1 -o Dpkg::Options::=--force-confnew install --allow-downgrades -y -V $PKGS",
        "_APT_EXIT=$?",
        *_build_debconf_verify_lines(debconf_selections),
        "exit $_APT_EXIT",
    ]

    return "\n".join([
        *_script_preamble(),
        *registration,
        *_APT_UPDATE_LINES,
        *_build_debconf_preseed_lines(debconf_selections),
        *_build_prerequisite_package_lines(prerequisites),
        *install_lines,
    ]) + "\n"


def _call_direct(fn, *args):
    fn(*args)


# ---------------------------------------------------------------------------
# Startup repository setup
#
# Runs before the user has selected anything so Step 3 can report, per package,
# whether it is new, already current, or has a newer candidate available. Only
# registers repositories and refreshes the apt index — nothing is installed,
# upgraded or removed here.
# ---------------------------------------------------------------------------

# Matched against apt output to turn a bare exit code into an actionable message.
_APT_ERROR_HINTS = (
    (("Temporary failure resolving", "Could not resolve", "Name or service not known"),
     "Failed to fetch package information — the repository host could not be resolved. "
     "Check your network connection or proxy settings."),
    (("Connection timed out", "Connection failed", "Unable to connect", "Connection refused"),
     "Failed to fetch package information — could not reach the Intel EdgePack repositories. "
     "Check your network connection or proxy settings."),
    (("407 Proxy", "Proxy Authentication"),
     "Failed to fetch package information — the proxy rejected the request. "
     "Check your http_proxy / https_proxy settings."),
    (("NO_PUBKEY", "GPG error", "not signed", "no longer signed"),
     "Failed to verify the Intel EdgePack repositories — the signing key could not be "
     "validated."),
    (("404  Not Found", "404 Not Found"),
     "Failed to fetch package information — a configured repository path does not exist."),
    (("Conflicting values set for option", "The list of sources could not be read"),
     "Failed to read the apt sources — a repository required by Edge Pack is already "
     "configured elsewhere with a different signing key. Remove the duplicate entry "
     "from /etc/apt/sources.list or /etc/apt/sources.list.d/ and try again."),
)

_GENERIC_APT_ERROR = (
    "Failed to fetch package information from the Intel EdgePack repositories. "
    "Check your network connection or proxy settings."
)


def _classify_apt_failure(output):
    """Map apt output to a user-facing explanation of why the refresh failed."""
    for markers, message in _APT_ERROR_HINTS:
        if any(marker in output for marker in markers):
            return message
    return _GENERIC_APT_ERROR


def _build_repo_setup_script(repos, prereq_repos=None):
    """Compose the startup script: register every repository and refresh the apt index.

    Same registration as the install stage (see
    :func:`_build_repo_registration_lines`) minus the package installs, so the
    candidate versions the wizard reports come from the exact index the install
    will later resolve against.
    """
    registration = _build_repo_registration_lines(repos, prereq_repos)
    if not registration:
        return ""
    return "\n".join([
        *_script_preamble(),
        *registration,
        *_APT_UPDATE_LINES,
    ]) + "\n"


_running_elevated: set = set()
_running_elevated_lock = threading.Lock()


def cancel_elevated_commands() -> None:
    """Terminate any in-flight elevated helper command (the apt index refresh).

    Called on quit. Without this the interpreter blocks at exit waiting for the
    worker thread, which is itself blocked on apt — so the TUI closes but the
    process never dies. Safe to kill: these stages only refresh the index.
    """
    with _running_elevated_lock:
        procs = list(_running_elevated)
    for proc in procs:
        _kill_process_group(proc)


def _kill_process_group(proc) -> None:
    """Signal the whole process group, so apt/wget children die with the shell."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.terminate()
        except OSError:
            return


def _elevated_shell(script, timeout):
    """Run *script* in a privileged shell, returning (returncode, combined_output).

    The TUI is launched under sudo (enforced in ``tui/main.py``) so no password
    is needed here; inside a Flatpak sandbox pkexec elevates on the host.

    Runs in its own process group and is registered in *_running_elevated* so
    :func:`cancel_elevated_commands` can stop it on quit.
    """
    if shutil.which("flatpak-spawn") or os.path.exists("/usr/bin/flatpak-spawn"):
        cmd = ["flatpak-spawn", "--host", "pkexec", "sh", "-c", script]
    elif os.geteuid() == 0:
        cmd = ["/bin/sh", "-c", script]
    else:
        cmd = ["/usr/bin/sudo", "-n", "/bin/sh", "-c", script]
    proc = subprocess.Popen(  # nosec B603
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=True,
        start_new_session=True,
    )
    with _running_elevated_lock:
        _running_elevated.add(proc)
    try:
        output, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_process_group(proc)
        proc.communicate()
        raise
    finally:
        with _running_elevated_lock:
            _running_elevated.discard(proc)
    return proc.returncode, output or ""


def prepare_repositories(repos, prereq_repos=None, timeout=180):
    """Register the EdgePack repositories and refresh the apt index.

    Returns ``(ok, message)``. On failure *message* is a user-facing
    explanation (network, proxy, signing key, ...) suitable for display in the
    wizard; on success it is empty.
    """
    log = logging.getLogger("tui_log")
    try:
        script = _build_repo_setup_script(repos, prereq_repos)
    except ValueError as e:
        log.warning("Repository setup rejected an invalid repository definition: %s", e)
        return False, (
            "The repository configuration is invalid, so package versions could not "
            "be checked."
        )
    if not script:
        log.warning("Repository setup skipped: no repositories defined for this OS")
        return False, "No Intel EdgePack repositories are defined for this OS."

    try:
        returncode, output = _elevated_shell(script, timeout)
    except subprocess.TimeoutExpired:
        log.warning("Repository setup timed out after %ss", timeout)
        return False, (
            "Timed out while fetching package information from the Intel EdgePack "
            "repositories. Check your network connection or proxy settings."
        )
    except Exception as e:
        log.warning("Repository setup failed to run: %s", type(e).__name__)
        return False, _GENERIC_APT_ERROR

    if returncode != 0:
        log.warning("Repository setup exited %d", returncode)
        log.debug("Repository setup output: %s", output)
        return False, _classify_apt_failure(output)

    log.info("Repository setup complete; apt index refreshed")
    return True, ""


def direct_dependencies(package_names, timeout=60):
    """Return ``(ok, [name, ...])`` — the first-level ``Depends:`` of *package_names*.

    Mirrors what ``apt show <pkg>`` prints, so the wizard lists exactly the
    dependencies a user would see by hand. Deliberately unlike the install-time
    ``dep_resolver.py``, which recurses and keeps only ``intel-edge*`` plus
    exactly-pinned names — that filtering is right for building the apt command
    but would hide real dependencies here. Requires a refreshed apt index.
    """
    log = logging.getLogger("tui_log")
    names = [n for n in (package_names or []) if n]
    # A name apt could mistake for an option is dropped, not passed through.
    rejected = [n for n in names if not valid_package_name(n)]
    if rejected:
        log.warning("direct_dependencies: skipping %d invalid package name(s)", len(rejected))
        names = [n for n in names if valid_package_name(n)]
    if not names:
        return True, []

    requested = set(names)
    seen = set()
    deps = []
    for name in names:
        try:
            proc = subprocess.run(  # nosec B603
                ["/usr/bin/apt", "show", name],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL, text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            log.warning("Dependency lookup for %s timed out after %ss", name, timeout)
            return False, []
        except Exception as e:
            log.warning("Dependency lookup for %s failed to run: %s", name, type(e).__name__)
            return False, []

        for dep_name, _op, _ver in _get_depends(proc.stdout or ""):
            if dep_name in requested or dep_name in seen:
                continue
            seen.add(dep_name)
            deps.append(dep_name)

    log.info("Direct dependencies resolved: %d", len(deps))
    return True, deps


def _elevated_kill(pgid, sudo_password, sig):
    """Signal the root-owned process group *pgid* with elevated privileges.

    The negative-pid form targets the whole process group, so the wrapping
    shell *and* its apt/dpkg children are signalled. Signalling only the
    ``sudo`` parent would leave the real apt process running orphaned, which
    is more dangerous than not cancelling at all.
    """
    args = ["/usr/bin/kill", "-s", sig, "--", f"-{int(pgid)}"]
    try:
        if shutil.which("flatpak-spawn") or os.path.exists("/usr/bin/flatpak-spawn"):
            # flatpak-spawn is resolved via PATH on purpose: its location is set by the sandbox.
            subprocess.run(  # nosec B603 B607
                ["flatpak-spawn", "--host", "pkexec", *args],
                capture_output=True, timeout=15,
            )
            return
        # The running install just refreshed sudo's credential cache, so -n
        # normally succeeds and the password never has to be re-sent.
        result = subprocess.run(["/usr/bin/sudo", "-n", *args], capture_output=True, timeout=15)  # nosec B603
        if result.returncode != 0 and sudo_password is not None:
            subprocess.run(  # nosec B603
                ["/usr/bin/sudo", "-S", "-p", "", *args],
                input=sudo_password + "\n", text=True,
                capture_output=True, timeout=15,
            )
    except Exception:
        logging.getLogger("tui_log").warning(
            "elevated kill (SIG%s) failed for process group %s", sig, pgid
        )


class InstallHandle:
    """Cancellation handle for an in-flight install returned by :func:`run_install`.

    Cancelling is only permitted while apt is still downloading. Once dpkg
    starts, killing it can leave the package database half-configured, so
    :meth:`cancel` refuses and returns False. The deciding flag is set by the
    output-reader thread (see :meth:`_mark_dpkg_started`) rather than by the
    UI, so there is no window where a queued UI update lags behind reality.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._process = None
        self._sudo_password = None
        self._cancelled = False
        self._done = False
        self._dpkg_started = False

    @property
    def cancelled(self):
        with self._lock:
            return self._cancelled

    @property
    def dpkg_started(self):
        with self._lock:
            return self._dpkg_started

    def _mark_dpkg_started(self):
        """Latch the non-cancellable phase; called from the reader thread only."""
        with self._lock:
            self._dpkg_started = True

    def cancel(self):
        """Request cancellation; False if already finished or past the dpkg handover."""
        with self._lock:
            if self._done or self._dpkg_started:
                return False
            self._cancelled = True
            process, password = self._process, self._sudo_password
        if process is not None:
            self._start_terminate(process, password)
        return True

    def _attach(self, process, sudo_password):
        """Register the started process; True means cancel arrived before it existed."""
        with self._lock:
            self._process = process
            self._sudo_password = sudo_password
            return self._cancelled

    def _release(self):
        with self._lock:
            self._process = None
            self._sudo_password = None
            self._done = True

    def _start_terminate(self, process, password):
        threading.Thread(
            target=self._terminate, args=(process, password), daemon=True
        ).start()

    def _terminate(self, process, password):
        # process_group=0 made the child its own group leader, so pid == pgid.
        # SIGKILL rather than SIGTERM: apt survives SIGTERM and runs the
        # install to completion, and because SIGTERM does reap the sudo/sh
        # parent, any "wait for exit, then escalate" loop sees the parent gone
        # and never escalates — leaving apt orphaned but still installing.
        pgid = process.pid
        _elevated_kill(pgid, password, "KILL")


def run_install(packages, repos, output_callback, finished_callback,
                progress_callback=None, schedule=_call_direct, sudo_password=None,
                prerequisites=None, force_packages=None, debconf_selections=None):
    """Start a background thread that runs the apt install script.

    Args:
        packages: list of (name, version) tuples.
        force_packages: optional list of package names appended to the apt
            command unversioned, without surfacing in the UI/summary (YAML
            ``meta_packages_by_os``). Names already in ``packages`` are skipped
            so they aren't listed twice.
        repos: list of repository config dicts from the YAML ``repositories``
            section (each has ``repository_url``, ``repository_dist``,
            ``repository_components``).
        prerequisites: optional dict ``{repositories: [...], packages: [...]}``
            (see ``tui.package_logic.required_prerequisites``). Each entry in
            ``repositories`` has ``repository_url``, ``repository_dist``,
            ``repository_components``, and a required ``repository_key``.
            When non-empty, every repo is added (signed with its key) and all
            ``packages`` installed *before* the normal sources file is written
            and the normal install runs; a failure here aborts before anything
            else is touched.
        debconf_selections: optional list of (owner, question, type, value)
            tuples from the YAML ``debconf_selections`` block of the selected
            profiles (see ``tui.package_logic.required_debconf_selections``).
            Preseeded before any package is unpacked so packages that ask a
            configuration question use these answers instead of their own
            defaults.
        output_callback: called with each regular apt output line.
        finished_callback: called with no arguments when the install exits.
        progress_callback: optional callable(percent, pkg, msg) driven by
            APT::Status-Fd pmstatus lines.  Not called for dlstatus lines.
        schedule: callable used to dispatch callbacks onto the UI thread.
        sudo_password: password string collected from the in-TUI auth modal.
            When provided, ``sudo -S`` reads it from stdin.
            When None (already root, or Flatpak path), ``sudo -n`` is used
            (no prompt; relies on cached credentials or root privileges).

    Auth strategy:
        Flatpak sandbox:
            ``flatpak-spawn --host pkexec`` elevates via the graphical polkit
            agent guaranteed to be present in the host desktop session.
        Everything else (direct terminal, SSH, headless):
            The caller collects the password via an in-TUI modal and passes it
            here as ``sudo_password``.  ``sudo -S -p ""`` reads from stdin so
            the subprocess never needs to touch Textual's terminal.

    Returns:
        An :class:`InstallHandle` whose ``cancel()`` aborts the run. The apt
        process tree is started in its own process group so cancellation can
        signal all of it, not just the ``sudo`` wrapper.
    """

    handle = InstallHandle()

    def target():
        process = None
        try:
            existing = {name for name, _ in packages}
            all_packages = list(packages) + [
                (name, "") for name in (force_packages or []) if name not in existing
            ]
            script = _build_install_script(
                all_packages, repos, prerequisites, debconf_selections
            )

            if not script:
                # _build_install_script returns "" when a repository is missing
                # its signing key — this is a fatal configuration error.
                schedule(output_callback, "\nError: repository configuration incomplete\n")
                schedule(finished_callback, 1)
                return

            if shutil.which("flatpak-spawn") or os.path.exists("/usr/bin/flatpak-spawn"):
                # Flatpak sandbox: delegate to host so pkexec can reach the
                # graphical polkit agent.
                cmd = ["flatpak-spawn", "--host", "pkexec", "sh", "-c", script]
                stdin_data = None
            elif sudo_password is not None:
                # Password collected from the TUI modal.
                # -S  : read password from stdin
                # -p "": suppress the prompt so it doesn't pollute the output stream
                cmd = ["/usr/bin/sudo", "-S", "-p", "", "/bin/sh", "-c", script]
                # ITEP-95741
                # Mutable bytearray container which can be explicitly cleared.
                # Deterministic memory wiping plus reducing memory forensics window
                stdin_data = bytearray((sudo_password + "\n").encode('utf-8'))
            else:
                # Already root, or caller used another pre-auth mechanism.
                cmd = ["/usr/bin/sudo", "-n", "/bin/sh", "-c", script]
                stdin_data = None

            # The subprocess must never inherit Textual's terminal fd.
            # stdin=PIPE is used when we feed the password; DEVNULL otherwise.
            # process_group=0 isolates the whole apt/dpkg tree in its own
            # process group so cancellation can signal it as a unit.
            process = subprocess.Popen(  # nosec B603
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
                text=True,
                process_group=0,
            )
            if handle._attach(process, sudo_password):
                # cancel() arrived between the handle being handed out and the
                # process existing, so there was nothing for it to signal.
                handle._start_terminate(process, sudo_password)
            # ITEP-95741
            # Write password as string (bytearray converted on-the-fly by subprocess)
            # and securely wipe the password bytearray from memory
            if stdin_data is not None:
                process.stdin.write(stdin_data.decode('utf-8'))
                process.stdin.close()
                stdin_data.clear()
            for line in process.stdout:
                stripped = line.rstrip('\n')
                if stripped.startswith('pmstatus:'):
                    # pmstatus is only emitted once apt hands over to dpkg.
                    handle._mark_dpkg_started()
                    if progress_callback:
                        parts = stripped.split(':', 3)
                        if len(parts) == 4:
                            _, pkg, pct_str, msg = parts
                            try:
                                schedule(progress_callback, float(pct_str), pkg, msg)
                            except ValueError:
                                pass  # malformed percentage string — skip this progress update
                elif stripped.startswith('dlstatus:'):
                    pass  # skip download status lines from the log
                else:
                    if stripped.startswith(DPKG_ACTIVITY_MARKERS):
                        handle._mark_dpkg_started()
                    schedule(output_callback, line)
            process.wait()
        except Exception as e:
            # Sanitize the message so internal details (paths, module names,
            # stack frames) are not exposed to the user or captured in logs.
            err_msg = str(e).strip()
            if len(err_msg) > 120:
                err_msg = err_msg[:120] + "..."
            schedule(output_callback, f"\nError: {err_msg}\n")
            schedule(finished_callback, 1)
            return
        finally:
            handle._release()
        schedule(finished_callback, process.returncode)

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return handle
