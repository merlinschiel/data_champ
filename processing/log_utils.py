"""
Logging setup shared by all processing scripts.

Each script gets its own logger with its own log file (processing/meta/logs/<name>.log)
that is rewritten on every run, both when the script runs standalone and when it is
imported by run_pipeline.py. Messages also propagate to the root logger, so the
pipeline runner sees them in pipeline.log and on the console.
"""

import logging
import sys
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parent / "meta" / "logs"
LOG_FORMAT = "%(asctime)s - %(levelname)s - %(message)s"


def get_logger(name: str, log_filename: str) -> logging.Logger:
    """Return a logger writing to processing/meta/logs/<log_filename>.

    A console handler is only added to the root logger when nothing else
    (e.g. run_pipeline.py) has configured logging yet.
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / log_filename

    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        root.addHandler(handler)
        root.setLevel(logging.INFO)

    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    if not any(getattr(h, "_crns_log_path", None) == str(log_path) for h in logger.handlers):
        file_handler = logging.FileHandler(log_path, mode="w", encoding="utf-8")
        file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
        file_handler._crns_log_path = str(log_path)
        logger.addHandler(file_handler)
    return logger


def configure_utf8_console() -> None:
    """Make Windows consoles accept the unicode characters neptoon/rich print."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


# fontTools logs every glyph-subsetting step at INFO when neptoon writes PDF reports
logging.getLogger("fontTools").setLevel(logging.WARNING)
