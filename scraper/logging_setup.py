"""
Logging configuration.

* logs/scraper.log   - everything (rotating)
* logs/requests.log  - one line per HTTP/browser request: successes and failures (rotating)
* logs/errors.log    - warnings and errors only (rotating)
* console            - INFO and above
"""
from __future__ import annotations

import logging
import logging.config
from pathlib import Path


def setup_logging(cfg: dict) -> None:
    log_dir = Path(cfg["paths"]["log_dir"])
    log_dir.mkdir(parents=True, exist_ok=True)
    level = str(cfg["logging"].get("level", "INFO")).upper()
    max_bytes = int(cfg["logging"].get("max_bytes", 5_000_000))
    backups = int(cfg["logging"].get("backup_count", 10))

    def rotating(filename: str, file_level: str, filters=None) -> dict:
        handler = {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": str(log_dir / filename),
            "maxBytes": max_bytes,
            "backupCount": backups,
            "encoding": "utf-8",
            "formatter": "detailed",
            "level": file_level,
        }
        if filters:
            handler["filters"] = filters
        return handler

    logging.config.dictConfig({
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "detailed": {"format": "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"},
            "console": {"format": "%(asctime)s %(levelname)-7s %(message)s", "datefmt": "%H:%M:%S"},
        },
        "filters": {
            "requests_only": {"()": "scraper.logging_setup.RequestFilter"},
        },
        "handlers": {
            "console": {"class": "logging.StreamHandler", "formatter": "console", "level": level},
            "main_file": rotating("scraper.log", "DEBUG" if level == "DEBUG" else "INFO"),
            "requests_file": rotating("requests.log", "INFO", ["requests_only"]),
            "error_file": rotating("errors.log", "WARNING"),
        },
        "loggers": {
            # third-party noise
            "urllib3": {"level": "WARNING"},
            "apscheduler": {"level": "INFO"},
            "asyncio": {"level": "WARNING"},
            "playwright": {"level": "WARNING"},
        },
        "root": {"level": "DEBUG" if level == "DEBUG" else "INFO",
                 "handlers": ["console", "main_file", "requests_file", "error_file"]},
    })


class RequestFilter(logging.Filter):
    """Keeps only records from the HTTP, robots and browser loggers."""

    def filter(self, record: logging.LogRecord) -> bool:
        return record.name in ("scraper.crawl", "scraper.robots")
