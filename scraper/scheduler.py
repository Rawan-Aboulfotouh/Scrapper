"""
Per-site scheduler (APScheduler).

Each enabled site has its own job and one of these schedules, set in config.yaml:

* ``batched`` (the default): a time window, e.g. 22:00 to 10:00, may cross midnight. A site is
  scraped in batches. Each batch starts at a RANDOM time inside the window and stops after
  ``batch_minutes``; progress is kept, and the next batch follows after a random pause of
  ``gap_minutes_min`` to ``gap_minutes_max``. When the site's full run finishes, its next run is
  planned for the next window.
* ``daily_random``: once a day at a random time inside a window that does not cross midnight.
* ``interval``: every N hours, counted from the start of the previous run.

Guarantees:

* the next run is planned when the scheduler starts and again after every batch or run
* overlapping executions of a site are impossible: APScheduler max_instances=1 plus the per-site
  run lock shared with manual runs
* an interrupted or paused run is resumed automatically by the next execution (checkpoint)
* at most ``max_parallel_sites`` sites crawl at the same time; a site that has to wait still runs
* a run whose start time passed while the PC was asleep is planned again instead of being dropped
"""
from __future__ import annotations

import json
import logging
import random
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from .pipeline import Pipeline

log = logging.getLogger("scraper.scheduler")

INTERVAL_JITTER_FRACTION = 0.05      # up to 5 % of the interval is added at random
FIRST_RUN_MAX_DELAY_MINUTES = 10     # interval sites with no history start within this time


def _parse_hhmm(value: str) -> dtime:
    hours, minutes = value.split(":")
    return dtime(int(hours), int(minutes))


def _random_between(low: datetime, high: datetime, rng) -> datetime:
    """A random moment between ``low`` and ``high``; ``low`` is used when ``high`` is not later."""
    if high <= low:
        return low.replace(microsecond=0)
    return (low + timedelta(seconds=rng.uniform(0, (high - low).total_seconds()))).replace(microsecond=0)


def _window_bounds(day: date, start_t: dtime, end_t: dtime, tz) -> Tuple[datetime, datetime]:
    start = datetime.combine(day, start_t, tzinfo=tz)
    end = datetime.combine(day, end_t, tzinfo=tz)
    if end <= start:                  # the window runs past midnight, e.g. 22:00 to 10:00
        end += timedelta(days=1)
    return start, end


def _windows(now: datetime, window_start: str, window_end: str) -> List[Tuple[datetime, datetime]]:
    start_t, end_t = _parse_hhmm(window_start), _parse_hhmm(window_end)
    return [_window_bounds(now.date() + timedelta(days=d), start_t, end_t, now.tzinfo) for d in (-1, 0, 1, 2)]


def current_window(now: datetime, window_start: str, window_end: str) -> Optional[Tuple[datetime, datetime]]:
    """The window that is open at ``now``, as (start, end), or None when none is open."""
    for start, end in _windows(now, window_start, window_end):
        if start <= now < end:
            return start, end
    return None


def plan_next_run(now: datetime, window_start: str, window_end: str,
                  last_success: Optional[date], rng: random.Random = random) -> datetime:
    """
    Pick a random start time inside the window.
    Today is used only if today's run has not succeeded yet and enough of the window remains.
    """
    tz = now.tzinfo
    start_t, end_t = _parse_hhmm(window_start), _parse_hhmm(window_end)

    def window(day: date):
        return (datetime.combine(day, start_t, tzinfo=tz), datetime.combine(day, end_t, tzinfo=tz))

    today_start, today_end = window(now.date())
    earliest_today = max(today_start, now + timedelta(minutes=1))
    if last_success != now.date() and earliest_today < today_end:
        low, high = earliest_today, today_end
    else:
        low, high = window(now.date() + timedelta(days=1))
    seconds = (high - low).total_seconds()
    return (low + timedelta(seconds=rng.uniform(0, seconds))).replace(microsecond=0)


def plan_interval_run(now: datetime, every_hours: float, last_started: Optional[datetime],
                      rng: random.Random = random) -> datetime:
    """The previous start plus the interval plus a little jitter; never in the past."""
    if last_started is None:
        when = now + timedelta(minutes=rng.uniform(1, FIRST_RUN_MAX_DELAY_MINUTES))
    else:
        jitter = rng.uniform(0, every_hours * 3600 * INTERVAL_JITTER_FRACTION)
        when = max(last_started + timedelta(hours=every_hours, seconds=jitter), now + timedelta(minutes=1))
    return when.replace(microsecond=0)


def plan_batched_run(now: datetime, window_start: str, window_end: str, batch_minutes: float,
                     gap_min: float, gap_max: float, state: Dict, rng: random.Random = random,
                     first_within: float = 60.0) -> datetime:
    """
    When the next batch of a site starts:
    1. after a paused batch, the next one follows after a random gap while the window is still open;
    2. otherwise, if this window has not finished the site yet, a random time in the first ``first_within``
       minutes of the window (right away when those minutes have passed);
    3. otherwise, the same kind of random time in the next window that has not finished the site yet.
    ``state["done_window"]`` is the date a window opened on, once the site finished (or was blocked) in it.
    Batches after the first follow each other with gaps, so a long run finishes inside the window.
    """
    now = now.replace(microsecond=0)
    batch = timedelta(minutes=batch_minutes)
    done_window = state.get("done_window") or ""
    active = current_window(now, window_start, window_end)
    if active:
        start, end = active
        if state.get("paused"):
            candidate = now + timedelta(minutes=rng.uniform(gap_min, gap_max))
            if candidate + batch <= end:
                return candidate.replace(microsecond=0)
        if str(start.date()) != done_window and now + timedelta(minutes=1) + batch <= end:
            high = min(end - batch, start + timedelta(minutes=first_within))
            return _random_between(now + timedelta(minutes=1), high, rng)
    for start, end in _windows(now, window_start, window_end):
        if start > now and str(start.date()) != done_window:
            high = min(end - batch, start + timedelta(minutes=first_within))
            return _random_between(start, high, rng)
    raise RuntimeError("no window found for the batched schedule")


class SiteScheduler:
    """Keeps one self-rescheduling APScheduler job per enabled site."""

    def __init__(self, cfg: dict, only_sites: Optional[List[str]] = None):
        self.cfg = cfg
        self.tz = ZoneInfo(cfg["timezone"])
        self.state_root = Path(cfg["paths"]["state_dir"]) / "sites"
        self.pipeline = Pipeline(cfg)
        self.only_sites = only_sites
        self.scheduler = None

    # ------------------------------------------------------------ state
    def _state_file(self, site: str) -> Path:
        return self.state_root / site / "scheduler_state.json"

    def _load_state(self, site: str) -> Dict:
        try:
            return json.loads(self._state_file(site).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_state(self, site: str, **values) -> None:
        path = self._state_file(site)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = self._load_state(site)
        data.update(values)
        path.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")

    def _last_success(self, site: str) -> Optional[date]:
        value = self._load_state(site).get("last_success_date")
        try:
            return date.fromisoformat(value) if value else None
        except ValueError:
            return None

    def _last_started(self, site: str) -> Optional[datetime]:
        value = self._load_state(site).get("last_started")
        try:
            return datetime.fromisoformat(value) if value else None
        except ValueError:
            return None

    def _window_key(self, site: str, when: datetime) -> str:
        """The date the batched window that contains ``when`` opened on, or "" outside any window."""
        schedule = self.cfg["sites"][site]["schedule"]
        if schedule["mode"] != "batched":
            return str(when.date())
        active = current_window(when, schedule["window_start"], schedule["window_end"])
        return str(active[0].date()) if active else ""

    # ------------------------------------------------------------ planning
    def sites(self) -> List[str]:
        names = [n for n, s in self.cfg["sites"].items() if s.get("enabled", True)]
        return [n for n in names if not self.only_sites or n in self.only_sites]

    def next_run_for(self, site: str, now: datetime) -> datetime:
        schedule = self.cfg["sites"][site]["schedule"]
        if schedule["mode"] == "batched":
            return plan_batched_run(now, schedule["window_start"], schedule["window_end"],
                                    float(schedule["batch_minutes"]), float(schedule["gap_minutes_min"]),
                                    float(schedule["gap_minutes_max"]), self._load_state(site),
                                    first_within=float(schedule.get("first_batch_within_minutes", 60)))
        if schedule["mode"] == "interval":
            return plan_interval_run(now, float(schedule["every_hours"]), self._last_started(site))
        return plan_next_run(now, schedule["window_start"], schedule["window_end"], self._last_success(site))

    def _schedule_next(self, site: str) -> None:
        from apscheduler.triggers.date import DateTrigger
        when = self.next_run_for(site, datetime.now(self.tz))
        grace = int(self.cfg["sites"][site]["schedule"]["misfire_grace_minutes"]) * 60
        self.scheduler.add_job(
            self._job, trigger=DateTrigger(run_date=when, timezone=self.tz), args=[site], id=f"scrape_{site}",
            replace_existing=True, max_instances=1, coalesce=True, misfire_grace_time=grace,
        )
        self._save_state(site, next_run=when.isoformat())
        log.info("%s: next run scheduled for %s", site, when.isoformat())

    # ------------------------------------------------------------ job
    def _job(self, site: str) -> None:
        started = datetime.now(self.tz)
        log.info("%s: scheduled run starting", site)
        schedule = self.cfg["sites"][site]["schedule"]
        self._save_state(site, last_started=started.isoformat(timespec="seconds"))
        budget = float(schedule["batch_minutes"]) * 60 if schedule["mode"] == "batched" else None
        try:
            result = self.pipeline.run_site(site, time_budget_seconds=budget)
            status = result.get("status")
            collected = sum(int(result.get(k, 0)) for k in ("ok", "out_of_stock", "no_price"))
            productive = collected > 0 or int(result.get("discovered", 0)) > 0   # a dead network is not "done"
            window = self._window_key(site, started)
            if status == "completed" and productive:
                self._save_state(site, last_success_date=started.date().isoformat(), done_window=window,
                                 paused=False, last_result=result)
            elif status == "denied":
                # the site blocked us: no more tries in this window; the next window tries again
                self._save_state(site, done_window=window, paused=False, last_result=result)
            elif status == "paused":
                self._save_state(site, paused=True, last_result=result)
            else:
                self._save_state(site, paused=False, last_result=result)
        except Exception:
            log.exception("%s: scheduled run crashed", site)
        finally:
            self._schedule_next(site)

    def _on_missed(self, event) -> None:
        """
        A run whose start time passed while the scheduler was not running (for example the PC was asleep).
        APScheduler drops that job, so plan the site's next run here; otherwise the site stops running.
        """
        site = event.job_id.removeprefix("scrape_")
        log.warning("%s: run time passed while the scheduler was not running - planning the next run", site)
        self._schedule_next(site)

    def start(self) -> None:
        from apscheduler.events import EVENT_JOB_MISSED
        from apscheduler.executors.pool import ThreadPoolExecutor
        from apscheduler.schedulers.blocking import BlockingScheduler
        sites = self.sites()
        if not sites:
            log.error("no enabled sites to schedule")
            return
        workers = int(self.cfg["schedule"]["max_parallel_sites"])
        self.scheduler = BlockingScheduler(timezone=self.tz, executors={"default": ThreadPoolExecutor(workers)})
        self.scheduler.add_listener(self._on_missed, EVENT_JOB_MISSED)
        for site in sites:
            self._schedule_next(site)
        log.info("scheduler running for %s (at most %d at a time). Press Ctrl+C to stop.", ", ".join(sites), workers)
        try:
            self.scheduler.start()
        except (KeyboardInterrupt, SystemExit):
            log.info("scheduler stopped")
