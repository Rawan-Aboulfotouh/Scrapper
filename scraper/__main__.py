"""
Command line interface.

    python -m scraper run                         # scrape all enabled sites now (resumes if interrupted)
    python -m scraper run --site noon             # one site only (repeatable)
    python -m scraper run --site noon --limit 5   # quick check: only the first 5 products
    python -m scraper run --fresh                 # ignore an unfinished run and start over
    python -m scraper run --site carrefour --product-url URL --category TVs
    python -m scraper run --site elsindbad --category-url URL --category TVs
    python -m scraper schedule                    # stay running; every site on its own schedule
    python -m scraper schedule --site noon        # schedule only these sites (repeatable)
    python -m scraper sites                       # list sites, whether enabled, and their schedules
    python -m scraper status                      # last and next run of every site

Sites are enabled or disabled, and their schedules set, in config.yaml.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .config import ConfigError, load_config
from .data_processor import ALLOWED_CATEGORIES
from .logging_setup import setup_logging
from .utils import InvalidURLError, validate_url


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m scraper",
                                     description="Crawl4AI price scraper for TVs, mobiles and tablets")
    parser.add_argument("--config", default=str(Path(__file__).resolve().parent.parent / "config.yaml"),
                        help="path to config.yaml (default: project folder)")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run the scraper once, now")
    run.add_argument("--site", action="append", help="limit to this site (repeatable)")
    run.add_argument("--fresh", action="store_true", help="do not resume an unfinished run")
    run.add_argument("--limit", type=int, help="process only the first N products of each site (quick check)")
    run.add_argument("--product-url", action="append", default=[], help="scrape only this product URL (repeatable)")
    run.add_argument("--category-url", action="append", default=[], help="scrape only this category URL (repeatable)")
    run.add_argument("--category", choices=ALLOWED_CATEGORIES, help="category for --product-url/--category-url")

    schedule = sub.add_parser("schedule", help="keep running and scrape every site on its own schedule")
    schedule.add_argument("--site", action="append", help="schedule only this site (repeatable)")
    sub.add_parser("sites", help="list the configured sites and their schedules")
    sub.add_parser("status", help="show the last run and the next planned run of every site")
    return parser


def _describe_schedule(schedule: dict) -> str:
    if schedule["mode"] == "interval":
        return f"every {schedule['every_hours']} hours"
    if schedule["mode"] == "batched":
        return (f"{schedule['batch_minutes']}-minute batches between {schedule['window_start']} "
                f"and {schedule['window_end']}")
    return f"daily, random time {schedule['window_start']}-{schedule['window_end']}"


def _command_run(args, cfg) -> int:
    extra = []
    if args.product_url or args.category_url:
        if not args.site or len(args.site) != 1:
            print("--product-url/--category-url need exactly one --site", file=sys.stderr)
            return 2
        if not args.category:
            print(f"--product-url/--category-url need --category ({', '.join(ALLOWED_CATEGORIES)})", file=sys.stderr)
            return 2
        site_cfg = cfg["sites"].get(args.site[0])
        if site_cfg is None:
            print(f"unknown site {args.site[0]!r}; configured: {', '.join(cfg['sites'])}", file=sys.stderr)
            return 2
        try:
            for kind, urls in (("product", args.product_url), ("category", args.category_url)):
                for url in urls:
                    extra.append({"site": args.site[0], "kind": kind, "category": args.category,
                                  "url": validate_url(url, site_cfg["allowed_hosts"])})
        except InvalidURLError as exc:
            print(f"invalid URL: {exc}", file=sys.stderr)
            return 2
    for name in args.site or []:
        if name not in cfg["sites"]:
            print(f"unknown site {name!r}; configured: {', '.join(cfg['sites'])}", file=sys.stderr)
            return 2

    from .pipeline import Pipeline
    result = Pipeline(cfg).run(only_sites=args.site, extra_targets=extra, force_new=args.fresh, limit=args.limit)
    return 0 if result.get("status") == "completed" else 1


def _command_sites(cfg) -> int:
    for name, site in cfg["sites"].items():
        state = "enabled" if site.get("enabled", True) else "disabled"
        if site.get("terms_require_permission") and not str(site.get("permission_reference") or "").strip():
            state += ", waiting for written permission"
        print(f"{name:12} {state:40} {_describe_schedule(site['schedule'])}")
    return 0


def _command_status(cfg) -> int:
    root = Path(cfg["paths"]["state_dir"]) / "sites"
    for name in cfg["sites"]:
        print(f"--- {name}")
        for file_name in ("scheduler_state.json", "checkpoint.json"):
            path = root / name / file_name
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                data.pop("done", None)
                data.pop("discovered", None)
                print(f"{file_name}: {json.dumps(data, indent=1, ensure_ascii=False)}")
            else:
                print(f"{file_name}: none yet")
    return 0


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    setup_logging(cfg)

    if args.command == "run":
        return _command_run(args, cfg)
    if args.command == "schedule":
        from .scheduler import SiteScheduler
        SiteScheduler(cfg, only_sites=args.site).start()
        return 0
    if args.command == "sites":
        return _command_sites(cfg)
    if args.command == "status":
        return _command_status(cfg)
    logging.getLogger("scraper").error("unknown command")
    return 2


if __name__ == "__main__":
    sys.exit(main())
