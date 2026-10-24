"""Small timestamped file logger for standalone services."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import threading

_LOCK = threading.Lock()
_RUN_FILES: dict[tuple[str, str], Path] = {}
_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


def log(
    log_name: str,
    log_dir: str | Path,
    log_message: str,
    log_level: str = "INFO",
) -> Path:
    """Append one line to a per-process, timestamped log file."""
    level = str(log_level).strip().upper()
    if level not in _LEVELS:
        raise ValueError(f"Unsupported log level: {log_level!r}")
    directory = Path(log_dir).expanduser()
    directory.mkdir(parents=True, exist_ok=True)
    key = (str(directory.resolve()), str(log_name))
    with _LOCK:
        path = _RUN_FILES.get(key)
        if path is None:
            stamp = datetime.now().strftime("%Y%m%d%H%M%S")
            path = directory / f"{stamp}_{log_name}.log"
            _RUN_FILES[key] = path
        line = (
            f"{datetime.now().isoformat(timespec='seconds')} "
            f"{level} {log_message.rstrip()}\n"
        )
        with path.open("a", encoding="utf-8") as output:
            output.write(line)
    return path
