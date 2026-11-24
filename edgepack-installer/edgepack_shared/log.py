import grp
import logging
import os
import stat
import sys
from pathlib import Path
from datetime import datetime

# Debian/Ubuntu convention for /var/log: root-owned, readable by the 'adm'
# group. Chosen over chowning to $SUDO_USER so ownership does not flip to
# whoever most recently ran sudo, which would lock earlier admins out of
# their own logs on a multi-admin machine.
LOG_GROUP = "adm"


def _log_group_gid() -> int | None:
    """gid of the log-reader group, or None if this system has no such group."""
    try:
        return grp.getgrnam(LOG_GROUP).gr_gid
    except KeyError:
        return None


def secure_log_path(path, is_dir: bool = False) -> None:
    """Restrict *path* to root plus the log-reader group.

    Directories become 0750 and files 0640, owned ``root:adm``. Falls back to
    owner-only (0700/0600) when the group is missing or we are not root, so the
    logs are never more exposed than before.
    """
    gid = _log_group_gid()
    shared = gid is not None and os.geteuid() == 0
    if is_dir:
        mode = stat.S_IRWXU | (stat.S_IRGRP | stat.S_IXGRP if shared else 0)
    else:
        mode = stat.S_IRUSR | stat.S_IWUSR | (stat.S_IRGRP if shared else 0)
    os.chmod(path, mode)
    if not shared:
        return
    try:
        os.chown(path, 0, gid)
    except OSError:
        pass  # Best-effort; the mode above already protects the contents.


def open_secure_log(path: str, buffering: int = -1):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        log_file = os.fdopen(descriptor, "w", encoding="utf-8", buffering=buffering)
    except BaseException:
        os.close(descriptor)
        raise
    try:
        secure_log_path(log_file.fileno())
    except OSError:
        log_file.close()
        raise
    return log_file


class ep_logger:
    def __init__(self, name: str = "log", log_file: str = "/var/log/edgepack/edgepack.log", level: str = "MUTE"):
        # 1. Get or create the logger instance
        self.logger = logging.getLogger(name)
        # 2. Translate user-set level to the library's definition
        match level:
            case "MUTE":
                logging.disable()
                return  # No handlers or log file created when muted
            case "CRITICAL":
                self.logger.setLevel("CRITICAL")
            case "ERROR":
                self.logger.setLevel("ERROR")
            case "WARNING":
                self.logger.setLevel("WARNING")
            case "INFO":
                self.logger.setLevel("INFO")
            case "DEBUG":
                self.logger.setLevel("DEBUG")
            case "NOTSET":
                self.logger.setLevel("NOTSET")
        
        # 3. Prevent adding handlers multiple times if the instance already exists
        if not self.logger.handlers:
            # Create a shared formatting style
            log_format = logging.Formatter(
                fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(filename)s:%(lineno)d | %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S"
            )
            
            # Setup Console Output Handler
            console_handler = logging.StreamHandler(sys.stdout)
            console_handler.setFormatter(log_format)
            self.logger.addHandler(console_handler)
            
            # Setup File Output Handler
            log_path = Path(log_file)
            dir_path = log_path.parent
            dir_path.mkdir(parents=True, exist_ok=True)  # Ensure directory exists

            # Do not rely on umask or parent directory permissions — these may
            # be insecure in production environments.
            try:
                secure_log_path(dir_path, is_dir=True)
                file_handler = logging.FileHandler(log_file, encoding="utf-8")
                file_handler.setFormatter(log_format)
                try:
                    secure_log_path(file_handler.stream.fileno())
                except OSError:
                    file_handler.close()
                    raise
            except OSError as exc:
                # Console logging continues; never write logs to a path we could not restrict.
                self.logger.warning("File logging disabled, %s could not be secured: %s", log_file, exc)
            else:
                self.logger.addHandler(file_handler)

    def get_logger(self) -> logging.Logger:
        """Returns the configured standard logging.Logger instance."""
        return self.logger


def rotate_logs(log_dir: str, max_age_days: int = 30, max_count: int = 10) -> None:
    """Clean up old log files in *log_dir*.

    Removes log files older than *max_age_days* or when the count exceeds
    *max_count*, whichever comes first. This prevents excessive disk usage
    on space-constrained systems and avoids information leakage from stale logs.

    Args:
        log_dir: Directory containing log files to clean up.
        max_age_days: Remove files older than this many days (default 30).
        max_count: Keep at most this many recent log files (default 10).
    """
    dir_path = Path(log_dir)
    if not dir_path.is_dir():
        return

    cutoff_time = datetime.now().timestamp() - (max_age_days * 86400)
    
    # Collect candidate log files with their modification times
    candidates = []
    try:
        for entry in dir_path.iterdir():
            if entry.is_file() and entry.suffix == '.log':
                mtime = entry.stat().st_mtime
                candidates.append((entry, mtime))
    except OSError:
        return

    # Remove files exceeding age limit
    to_remove = [path for path, mtime in candidates if mtime < cutoff_time]
    
    # If within age limit but too many files, remove oldest first
    remaining = [(path, mtime) for path, mtime in candidates if path not in to_remove]
    if len(remaining) > max_count:
        remaining.sort(key=lambda x: x[1])  # sort by mtime ascending (oldest first)
        excess_count = len(remaining) - max_count
        to_remove.extend(path for path, _ in remaining[:excess_count])

    # Perform removals
    for log_file in to_remove:
        try:
            os.remove(log_file)
        except OSError:
            pass  # Best-effort cleanup — do not raise on permission errors
