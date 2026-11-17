"""Loads config.yaml, fills defaults, resolves paths and validates everything up-front."""
from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any, Dict
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from .data_processor import ALLOWED_CATEGORIES
from .utils import InvalidURLError, validate_url

DEFAULTS: Dict[str, Any] = {
    "timezone": "Africa/Cairo",
    "crawl": {
        "headless": True,
        "user_agent": "",
        "robots_agent": "PriceMonitorBot",
        "respect_robots_txt": True,
        "locale": "",
        "block_heavy_resources": True,
        "page_timeout_seconds": 60,
        "max_retries": 3,
        "backoff_base_seconds": 5,
        "backoff_max_seconds": 120,
        "max_retry_after_seconds": 300,
        "min_delay_seconds": 3,
        "max_delay_seconds": 7,
        "restart_browser_every_pages": 200,
    },
    "schedule": {
        "mode": "batched",
        "window_start": "22:00",
        "window_end": "10:00",
        "batch_minutes": 20,
        "gap_minutes_min": 5,
        "gap_minutes_max": 20,
        "first_batch_within_minutes": 60,
        "every_hours": 6,
        "misfire_grace_minutes": 60,
        "max_parallel_sites": 2,
    },
    "run": {
        "resume_max_age_hours": 20,
        "max_failures_per_product": 3,
        "flush_every": 25,
        "lock_stale_hours": 12,
    },
    "price": {
        "preferred_currency": "EGP",
        "min_valid_price": 1,
        "max_valid_price": 10000000,
    },
    "filters": {
        "include_used_products": True,
        "exclude_name_prefixes": {},
    },
    "output": {
        "products_file": "output/products.csv",
        "price_history_file": "output/price_history.csv",
        "format": "csv",
        "keep_backup": True,
    },
    "paths": {
        "state_dir": "state",
        "log_dir": "logs",
    },
    "logging": {
        "level": "INFO",
        "max_bytes": 5_000_000,
        "backup_count": 10,
    },
    "sites": {},
}

_TIME = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
SCHEDULE_MODES = ("batched", "daily_random", "interval")


class ConfigError(ValueError):
    pass


def _merge(base: Dict, override: Dict) -> Dict:
    result = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


def _validate_schedule(where: str, schedule: Dict[str, Any]) -> None:
    mode = schedule.get("mode")
    if mode not in SCHEDULE_MODES:
        raise ConfigError(f"{where}.mode must be one of {SCHEDULE_MODES}")
    if mode == "interval":
        if float(schedule.get("every_hours", 0)) <= 0:
            raise ConfigError(f"{where}.every_hours must be greater than 0")
        return
    start, end = schedule.get("window_start"), schedule.get("window_end")
    if not _TIME.match(str(start)) or not _TIME.match(str(end)):
        raise ConfigError(f"{where}.window_start/window_end must be HH:MM (24h)")
    if str(start) == str(end):
        raise ConfigError(f"{where}.window_start and window_end must differ")
    if mode == "daily_random" and str(start) > str(end):
        raise ConfigError(f"{where}: a daily_random window must not cross midnight; use mode: batched")
    if mode == "batched":
        if float(schedule.get("batch_minutes", 0)) <= 0:
            raise ConfigError(f"{where}.batch_minutes must be greater than 0")
        low, high = float(schedule.get("gap_minutes_min", 0)), float(schedule.get("gap_minutes_max", 0))
        if not 0 <= low <= high:
            raise ConfigError(f"{where}: gap_minutes_min must be between 0 and gap_minutes_max")
        if float(schedule.get("first_batch_within_minutes", 0)) < 0:
            raise ConfigError(f"{where}.first_batch_within_minutes must be 0 or more")


def site_crawl_settings(cfg: Dict[str, Any], site_name: str) -> Dict[str, Any]:
    """The global ``crawl:`` section with this site's own ``crawl:`` overrides applied."""
    return _merge(cfg["crawl"], cfg["sites"][site_name].get("crawl") or {})


def load_config(path: str | Path) -> Dict[str, Any]:
    path = Path(path).resolve()
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")
    with path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    cfg = _merge(DEFAULTS, raw)
    base_dir = path.parent
    cfg["_base_dir"] = str(base_dir)

    # ---- paths relative to the config file
    for section, keys in (("output", ("products_file", "price_history_file")), ("paths", ("state_dir", "log_dir"))):
        for key in keys:
            value = Path(cfg[section][key])
            cfg[section][key] = str(value if value.is_absolute() else (base_dir / value).resolve())

    # ---- timezone & global schedule
    try:
        ZoneInfo(cfg["timezone"])
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"unknown timezone {cfg['timezone']!r} (on Windows: pip install tzdata)") from exc
    _validate_schedule("schedule", cfg["schedule"])
    if int(cfg["schedule"]["max_parallel_sites"]) < 1:
        raise ConfigError("schedule.max_parallel_sites must be at least 1")

    crawl = cfg["crawl"]
    if float(crawl["min_delay_seconds"]) < 0 or float(crawl["max_delay_seconds"]) < float(crawl["min_delay_seconds"]):
        raise ConfigError("crawl.min_delay_seconds must be >= 0 and <= max_delay_seconds")
    if not 0 <= int(crawl["max_retries"]) <= 10:
        raise ConfigError("crawl.max_retries must be between 0 and 10")

    # ---- sites
    if not cfg["sites"]:
        raise ConfigError("no sites configured")
    for name, site in cfg["sites"].items():
        for required in ("base_url", "allowed_hosts"):
            if not site.get(required):
                raise ConfigError(f"sites.{name}.{required} is required")
        site.setdefault("enabled", True)
        site.setdefault("category_urls", [])
        site.setdefault("product_urls", [])
        # each site's schedule starts from the global one and overrides what it names
        site["schedule"] = _merge(cfg["schedule"], site.get("schedule") or {})
        _validate_schedule(f"sites.{name}.schedule", site["schedule"])
        for kind in ("category_urls", "product_urls"):
            for entry in site[kind]:
                if entry.get("category") not in ALLOWED_CATEGORIES:
                    raise ConfigError(f"sites.{name}.{kind}: category must be one of {ALLOWED_CATEGORIES}, "
                                      f"got {entry.get('category')!r}")
                try:
                    entry["url"] = validate_url(entry.get("url", ""), site["allowed_hosts"])
                except InvalidURLError as exc:
                    raise ConfigError(f"sites.{name}.{kind}: {exc}") from exc
    return cfg
