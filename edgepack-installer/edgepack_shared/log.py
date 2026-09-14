import logging
import os
import stat
import sys
from pathlib import Path
from datetime import datetime

class ep_logger:
    # Minimum permissions for log directories: owner read/write/execute only.
    # This prevents other users from discovering or reading log files.
    _LOG_DIR_MODE = stat.S_IRWXU  # 0o700
    
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
            
            # Explicitly set minimal permissions on the log directory.
            # Do not rely on umask or parent directory permissions — these may
            # be insecure in production environments.
            try:
                os.chmod(dir_path, self._LOG_DIR_MODE)
            except OSError:
                pass  # Best-effort; non-root users may not have permission
            
            file_handler = logging.FileHandler(log_file, encoding="utf-8")
            file_handler.setFormatter(log_format)
            # Restrict log file permissions to owner-only (600). The installer runs
            # as root and the log may contain system inventory details.
            os.chmod(log_file, stat.S_IRUSR | stat.S_IWUSR)
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
