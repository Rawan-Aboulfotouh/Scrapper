import csv
import json
import os
import random
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from scraper.data_processor import PRODUCT_COLUMNS
from scraper.scheduler import SiteScheduler, plan_batched_run, plan_interval_run, plan_next_run
from scraper.state import AlreadyRunning, Checkpoint, RunLock, WaitingFileLock
from scraper.storage import StorageManager

CAIRO = ZoneInfo("Africa/Cairo")


def _record(**values):
    row = {c: "" for c in PRODUCT_COLUMNS}
    row.update({"site": "elsindbad", "category": "TVs", "scraped_at": "2026-11-04T10:00:00+02:00"})
    row.update(values)
    row["last_seen"] = row["scraped_at"]          # as DataProcessor.build_record does
    return row


def _read(path):
    with open(path, encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


# ------------------------------------------------------------------ storage
def test_merge_appends_updates_and_never_blanks(tmp_path):
    storage = StorageManager({"products_file": tmp_path / "p.csv", "price_history_file": tmp_path / "h.csv"})
    storage.merge_products([_record(sku="1", product_name="TV A", lowest_price="100.00", seller_name="S", brand="LG",
                                    product_url="https://x/a")])
    result = storage.merge_products([
        _record(sku="1", product_name="TV A", lowest_price="90.00", seller_name="", brand="",
                availability="unavailable", product_url="https://x/a", scraped_at="2026-11-05T10:00:00+02:00"),
        _record(sku="2", product_name="TV B", lowest_price="50.00", product_url="https://x/b"),
    ], run_started_at="2026-11-05T09:00:00+02:00")
    assert result == {"added": 1, "updated": 1, "total": 2}
    rows = {r["sku"]: r for r in _read(tmp_path / "p.csv")}
    assert rows["1"]["lowest_price"] == "90.00"
    assert rows["1"]["seller_name"] == "S" and rows["1"]["brand"] == "LG"     # empty values did not overwrite
    assert rows["1"]["first_seen"] == "2026-11-04T10:00:00+02:00"
    assert rows["1"]["last_seen"] == "2026-11-05T10:00:00+02:00"
    assert (tmp_path / "p.csv.bak").exists()


def test_sku_fallback_to_url_and_not_seen_flag(tmp_path):
    storage = StorageManager({"products_file": tmp_path / "p.csv", "price_history_file": tmp_path / "h.csv"})
    storage.merge_products([_record(sku="", product_name="No SKU yet", product_url="https://x/a"),
                            _record(sku="9", product_name="Gone tomorrow", product_url="https://x/gone")])
    storage.merge_products([_record(sku="777", product_name="Now with SKU", product_url="https://x/a")],
                           run_started_at="2026-11-05T09:00:00+02:00")
    rows = _read(tmp_path / "p.csv")
    assert len(rows) == 2
    by_url = {r["product_url"]: r for r in rows}
    assert by_url["https://x/a"]["sku"] == "777"
    assert by_url["https://x/gone"]["seen_in_last_run"] == "no"


def test_history_append_is_idempotent_per_run(tmp_path):
    storage = StorageManager({"products_file": tmp_path / "p.csv", "price_history_file": tmp_path / "h.csv"})
    row = {"sku": "1", "product_name": "TV", "site": "s", "seller_name": "A", "price": "10.00",
           "currency": "EGP", "product_url": "u", "timestamp": "t", "run_id": "r1"}
    assert storage.append_history([row]) == 1
    assert storage.append_history([row]) == 0                 # resumed run does not duplicate
    assert storage.append_history([dict(row, run_id="r2")]) == 1
    assert len(_read(tmp_path / "h.csv")) == 2


# ------------------------------------------------------------------ lock & checkpoint
def test_lock_blocks_second_run_and_replaces_dead_lock(tmp_path):
    lock_path = tmp_path / "run.lock"
    first = RunLock(lock_path)
    first.acquire()
    with pytest.raises(AlreadyRunning):
        RunLock(lock_path).acquire()
    first.release()
    lock_path.write_text(json.dumps({"pid": 999999, "started": 0}))  # crashed process
    with RunLock(lock_path):
        assert json.loads(lock_path.read_text())["pid"] == os.getpid()
    assert not lock_path.exists()


def test_checkpoint_resume(tmp_path):
    t0 = datetime(2026, 11, 4, 10, 0, tzinfo=CAIRO)
    cp = Checkpoint(tmp_path)
    assert cp.start_or_resume(t0) is False
    run_id = cp.run_id
    cp.save_discovery("cat-1", [{"site": "s", "url": "u", "category": "TVs", "key": "k", "hints": {}}])
    cp.mark("s|k", "ok")
    cp.append_record({"record": {"sku": "1"}, "history": []})

    again = Checkpoint(tmp_path)                                     # process restarted
    assert again.start_or_resume(datetime(2026, 11, 4, 11, 0, tzinfo=CAIRO)) is True
    assert again.run_id == run_id and again.status_of("s|k") == "ok"
    assert again.discovery_done("cat-1") and again.discovered_items("cat-1")[0]["url"] == "u"
    assert list(again.iter_records()) == [{"record": {"sku": "1"}, "history": []}]

    again.finish("completed")
    fresh = Checkpoint(tmp_path)
    assert fresh.start_or_resume(datetime(2026, 11, 5, 10, 0, tzinfo=CAIRO)) is False
    assert fresh.run_id != run_id and list(fresh.iter_records()) == []


# ------------------------------------------------------------------ scheduler
def test_plan_inside_window_today():
    now = datetime(2026, 11, 4, 8, 0, tzinfo=CAIRO)
    for seed in range(50):
        when = plan_next_run(now, "09:00", "13:00", last_success=None, rng=random.Random(seed))
        assert when.date() == now.date() and 9 <= when.hour < 13


def test_plan_tomorrow_when_done_or_window_passed():
    now = datetime(2026, 11, 4, 10, 30, tzinfo=CAIRO)
    when = plan_next_run(now, "09:00", "13:00", last_success=date(2026, 11, 4), rng=random.Random(1))
    assert when.date() == date(2026, 11, 5) and 9 <= when.hour < 13
    late = datetime(2026, 11, 4, 18, 0, tzinfo=CAIRO)
    assert plan_next_run(late, "09:00", "13:00", None, random.Random(2)).date() == date(2026, 11, 5)


def test_plan_not_fixed_time():
    now = datetime(2026, 11, 4, 20, 0, tzinfo=CAIRO)
    times = {plan_next_run(now, "09:00", "13:00", None, random.Random(s)).time() for s in range(20)}
    assert len(times) > 15


def test_plan_mid_window_never_in_past():
    now = datetime(2026, 11, 4, 12, 30, tzinfo=CAIRO)
    when = plan_next_run(now, "09:00", "13:00", None, random.Random(3))
    assert now < when <= datetime(2026, 11, 4, 13, 0, tzinfo=CAIRO)


def test_interval_is_counted_from_the_previous_start():
    now = datetime(2026, 11, 4, 10, 0, tzinfo=CAIRO)
    started = datetime(2026, 11, 4, 8, 0, tzinfo=CAIRO)
    when = plan_interval_run(now, 6, started, random.Random(1))
    assert timedelta(hours=6) <= when - started <= timedelta(hours=6.3)      # 5 % jitter at most


def test_overdue_interval_runs_soon_and_never_in_the_past():
    now = datetime(2026, 11, 4, 20, 0, tzinfo=CAIRO)
    when = plan_interval_run(now, 6, datetime(2026, 11, 3, 8, 0, tzinfo=CAIRO), random.Random(2))
    assert now < when <= now + timedelta(minutes=2)


def test_first_interval_run_starts_within_minutes():
    now = datetime(2026, 11, 4, 20, 0, tzinfo=CAIRO)
    assert now < plan_interval_run(now, 6, None, random.Random(3)) <= now + timedelta(minutes=10)


def test_each_site_follows_its_own_schedule(base_cfg):
    base_cfg["sites"]["noon"]["schedule"].update(mode="daily_random", window_start="14:00", window_end="15:00")
    base_cfg["sites"]["vodafone"]["schedule"].update(mode="interval", every_hours=2)
    base_cfg["sites"]["carrefour"]["enabled"] = False
    scheduler = SiteScheduler(base_cfg)
    now = datetime(2026, 11, 4, 8, 0, tzinfo=CAIRO)
    assert 14 <= scheduler.next_run_for("noon", now).hour < 15
    scheduler._save_state("vodafone", last_started="2026-11-04T07:00:00+02:00")
    assert scheduler.next_run_for("vodafone", now).hour == 9
    assert "carrefour" not in scheduler.sites() and "noon" in scheduler.sites()
    assert SiteScheduler(base_cfg, only_sites=["noon"]).sites() == ["noon"]


def test_schedule_is_configured_per_site_without_code(base_cfg):
    assert base_cfg["sites"]["noon"]["schedule"]["mode"] == "batched"
    assert base_cfg["sites"]["vodafone"]["schedule"]["mode"] == "batched"            # inherited from the default
    assert base_cfg["sites"]["vodafone"]["schedule"]["batch_minutes"] == 20
    assert base_cfg["sites"]["vodafone"]["schedule"]["misfire_grace_minutes"] == 60


BATCH_WINDOW = dict(window_start="22:00", window_end="10:00")


def _batch_plan(now, state, seed=1):
    return plan_batched_run(now, BATCH_WINDOW["window_start"], BATCH_WINDOW["window_end"], 20, 5, 20, state,
                            random.Random(seed))


def test_batched_first_batch_is_random_inside_the_night():
    now = datetime(2026, 11, 10, 15, 0, tzinfo=CAIRO)                 # afternoon, before the window opens
    for seed in range(30):
        when = _batch_plan(now, {}, seed)
        assert datetime(2026, 11, 10, 22, 0, tzinfo=CAIRO) <= when <= datetime(2026, 11, 10, 23, 0, tzinfo=CAIRO)
    assert len({_batch_plan(now, {}, seed).time() for seed in range(30)}) > 20       # not a fixed time


def test_batched_starts_in_the_rest_of_an_open_window():
    now = datetime(2026, 11, 10, 23, 0, tzinfo=CAIRO)
    for seed in range(30):
        assert now < _batch_plan(now, {}, seed) <= datetime(2026, 11, 11, 9, 40, tzinfo=CAIRO)


def test_batched_next_batch_follows_after_a_gap_while_paused():
    now = datetime(2026, 11, 10, 23, 0, tzinfo=CAIRO)
    for seed in range(30):
        when = _batch_plan(now, {"paused": True}, seed)
        assert datetime(2026, 11, 10, 23, 5, tzinfo=CAIRO) <= when <= datetime(2026, 11, 10, 23, 20, tzinfo=CAIRO)


def test_batched_finished_window_waits_for_the_next_night():
    now = datetime(2026, 11, 10, 23, 0, tzinfo=CAIRO)
    for seed in range(30):
        when = _batch_plan(now, {"done_window": "2026-11-10"}, seed)
        assert datetime(2026, 11, 11, 22, 0, tzinfo=CAIRO) <= when <= datetime(2026, 11, 12, 9, 40, tzinfo=CAIRO)


def test_batched_paused_near_the_end_moves_to_the_next_night():
    now = datetime(2026, 11, 11, 9, 50, tzinfo=CAIRO)                  # the window opened on the 10th closes at 10:00
    when = _batch_plan(now, {"paused": True}, 3)
    assert when >= datetime(2026, 11, 11, 22, 0, tzinfo=CAIRO)


def test_batched_window_crossing_midnight_is_accepted(tmp_path):
    from scraper.config import load_config
    path = tmp_path / "c.yaml"
    path.write_text("schedule: {mode: batched, window_start: '22:00', window_end: '10:00'}\n"
                    "sites: {a: {base_url: 'https://a.com', allowed_hosts: [a.com]}}\n", encoding="utf-8")
    assert load_config(path)["schedule"]["window_end"] == "10:00"


def test_daily_window_cannot_cross_midnight(tmp_path):
    from scraper.config import ConfigError, load_config
    path = tmp_path / "c.yaml"
    path.write_text("schedule: {mode: daily_random, window_start: '22:00', window_end: '10:00'}\n"
                    "sites: {a: {base_url: 'https://a.com', allowed_hosts: [a.com]}}\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)


def test_invalid_schedule_is_rejected(tmp_path):
    from scraper.config import ConfigError, load_config
    path = tmp_path / "c.yaml"
    path.write_text("schedule: {mode: weekly}\nsites: {a: {base_url: 'https://a.com', allowed_hosts: [a.com]}}\n")
    with pytest.raises(ConfigError):
        load_config(path)


def test_storage_lock_waits_then_times_out(tmp_path):
    path = tmp_path / "storage.lock"
    with WaitingFileLock(path, timeout_seconds=1):
        with pytest.raises(AlreadyRunning):
            with WaitingFileLock(path, timeout_seconds=0.3):
                pass
    with WaitingFileLock(path, timeout_seconds=0.3):
        assert path.exists()


def test_existing_rows_are_rekeyed_when_sku_changes(tmp_path):
    """Upgrading: rows saved with the old internal SKU are matched by URL, not duplicated."""
    storage = StorageManager({"products_file": tmp_path / "p.csv", "price_history_file": tmp_path / "h.csv"})
    storage.merge_products([_record(sku="11691", product_name="TV", product_url="https://x/lg43", lowest_price="16999.00")])
    storage.merge_products([_record(sku="43LM6370PVA", store_sku="11691", product_name="TV",
                                    product_url="https://x/lg43", lowest_price="16999.00")])
    rows = _read(tmp_path / "p.csv")
    assert len(rows) == 1 and rows[0]["sku"] == "43LM6370PVA" and rows[0]["store_sku"] == "11691"


def test_old_price_history_file_gets_availability_column(tmp_path):
    history = tmp_path / "h.csv"
    history.write_text("sku,product_name,site,seller_name,price,currency,product_url,timestamp,run_id\n"
                       "1,TV,s,A,10.00,EGP,u,t,r1\n", encoding="utf-8-sig")
    storage = StorageManager({"products_file": tmp_path / "p.csv", "price_history_file": history})
    storage.append_history([{"sku": "1", "product_name": "TV", "site": "s", "seller_name": "A", "price": "12.00",
                             "currency": "EGP", "availability": "out_of_stock", "product_url": "u",
                             "timestamp": "t2", "run_id": "r2"}])
    rows = _read(history)
    assert [r["availability"] for r in rows] == ["", "out_of_stock"] and rows[0]["price"] == "10.00"


def test_missed_run_is_planned_again(base_cfg):
    """A run that missed its time (PC asleep) gets a new planned run, or the site stops for good."""
    class Jobs:
        def __init__(self):
            self.added = []

        def add_job(self, func, trigger=None, args=None, id=None, **options):
            self.added.append(id)

    class Missed:
        job_id = "scrape_vodafone"

    scheduler = SiteScheduler(base_cfg)
    scheduler.scheduler = Jobs()
    scheduler._on_missed(Missed())
    assert scheduler.scheduler.added == ["scrape_vodafone"]

