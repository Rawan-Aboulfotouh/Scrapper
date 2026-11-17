"""
Run orchestration: lock -> checkpoint -> collect -> scrape -> lowest price ->
normalise/validate -> save. Used by manual runs and by the scheduler.

Every site runs independently, with its own lock, checkpoint and browser, so one site being
blocked, slow or interrupted never affects another. All sites merge into the same output files;
a short file lock serialises those writes.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Dict, List, Optional

from .collector import UrlCollector
from .config import site_crawl_settings
from .crawl_engine import CrawlSession, CrawlSettings
from .data_processor import DataProcessor, ValidationError, product_key
from .errors import AccessDenied, NotFound, RobotsDisallowed, ScrapeError
from .price_processor import PriceProcessor
from .sites import ADAPTERS
from .sites.yallatager import YallaTagerAdapter
from .state import AlreadyRunning, Checkpoint, RunLock, WaitingFileLock
from .storage import StorageManager
from .utils import InvalidURLError, iso_now, now_in_tz

log = logging.getLogger("scraper.pipeline")

DONE_STATES = ("ok", "skipped", "excluded", "duplicate", "unavailable", "out_of_stock", "no_price")
FINISHED_STATUSES = ("completed", "skipped", "disabled", "not_permitted")


class Pipeline:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.tz = cfg["timezone"]
        self.state_dir = Path(cfg["paths"]["state_dir"])
        self.price = PriceProcessor(cfg["price"])
        self.data = DataProcessor(cfg)
        self.storage = StorageManager(cfg["output"])
        self.storage_lock = self.state_dir / "storage.lock"

    # ------------------------------------------------------------------ public
    def run(self, only_sites: Optional[List[str]] = None, extra_targets: Optional[List[Dict]] = None,
            force_new: bool = False, limit: Optional[int] = None) -> Dict:
        """Run every enabled site (or ``only_sites``) one after the other."""
        results: Dict[str, Dict] = {}
        extra_targets = extra_targets or []
        for site_name in self.cfg["sites"]:
            if only_sites and site_name not in only_sites:
                continue
            site_extra = [t for t in extra_targets if t["site"] == site_name]
            results[site_name] = self.run_site(site_name, site_extra, force_new, limit, requested=bool(only_sites))
            if results[site_name].get("status") == "interrupted":
                break
        statuses = [r.get("status") for r in results.values()]
        overall = "completed" if all(s in FINISHED_STATUSES for s in statuses) else "failed"
        if "interrupted" in statuses:
            overall = "interrupted"
        return {"status": overall, "sites": results}

    def run_site(self, site_name: str, extra_targets: Optional[List[Dict]] = None, force_new: bool = False,
                 limit: Optional[int] = None, requested: bool = False,
                 time_budget_seconds: Optional[float] = None) -> Dict:
        """
        Scrape one site. ``limit`` caps the products processed (for a quick check).
        ``time_budget_seconds`` makes this one batch: when the time is up the run stops with status
        "paused", its progress is saved, and the next batch resumes it.
        """
        extra_targets = extra_targets or []
        site_cfg = self.cfg["sites"].get(site_name)
        if site_cfg is None:
            log.error("no site named %r in the configuration", site_name)
            return {"status": "failed", "reason": "unknown site"}
        if not site_cfg.get("enabled", True) and not extra_targets:
            if requested:
                log.warning("%s is disabled in config.yaml (sites.%s.enabled: false) - skipped", site_name, site_name)
            return {"status": "disabled"}
        if site_name not in ADAPTERS:
            log.error("no adapter for site %r - skipped", site_name)
            return {"status": "failed", "reason": "no adapter"}
        if not self._permitted(site_name, site_cfg):
            return {"status": "not_permitted"}

        site_state = self.state_dir / "sites" / site_name
        lock = RunLock(site_state / "run.lock", float(self.cfg["run"]["lock_stale_hours"]))
        try:
            lock.acquire()
        except AlreadyRunning as exc:
            log.warning("%s: run skipped: %s", site_name, exc)
            return {"status": "skipped", "reason": str(exc)}
        try:
            partial = bool(extra_targets) or limit is not None
            return self._run_locked(site_name, site_cfg, site_state, extra_targets, force_new or partial, limit, partial,
                                    time_budget_seconds)
        finally:
            lock.release()

    # ------------------------------------------------------------------ internals
    @staticmethod
    def _permitted(site_name: str, site_cfg: Dict) -> bool:
        """Sites whose terms require written consent stay off until that consent is recorded."""
        if not site_cfg.get("terms_require_permission"):
            return True
        reference = str(site_cfg.get("permission_reference") or "").strip()
        if not site_cfg.get("permission_obtained") or not reference:
            log.error("%s: the site's terms forbid automated collection without written permission. After "
                      "obtaining it, set sites.%s.permission_obtained: true AND permission_reference (who "
                      "approved it and when). Skipped.", site_name, site_name)
            return False
        log.warning("%s: running under written permission: %s", site_name, reference)
        return True

    def _run_locked(self, site_name, site_cfg, site_state, extra_targets, force_new, limit, partial,
                    time_budget_seconds=None) -> Dict:
        started = now_in_tz(self.tz)
        t0 = time.monotonic()
        deadline = t0 + time_budget_seconds if time_budget_seconds is not None else None
        # partial runs (single URLs, --limit) keep their own checkpoint so they never disturb
        # an interrupted scheduled run that is waiting to be resumed
        checkpoint_dir = site_state / "adhoc" if partial else site_state
        checkpoint = Checkpoint(checkpoint_dir, float(self.cfg["run"]["resume_max_age_hours"]))
        resumed = checkpoint.start_or_resume(started, force_new=force_new)
        log.info("=== %s run %s %s ===", site_name, checkpoint.run_id, "RESUMED" if resumed else "started")

        session = CrawlSession(CrawlSettings.from_config(site_crawl_settings(self.cfg, site_name)))
        if site_cfg.get("terms_require_permission") and site_cfg.get("permission_overrides_robots"):
            reference = str(site_cfg.get("permission_reference") or "").strip()
            for host in site_cfg["allowed_hosts"]:
                session.robots_permission[host.lower()] = reference
            log.warning("%s: robots.txt is NOT applied to %s - covered by the written permission above",
                        site_name, ", ".join(site_cfg["allowed_hosts"]))

        collector = UrlCollector(checkpoint)
        counters = {"site": site_name, "discovered": 0, "ok": 0, "skipped": 0, "excluded": 0, "duplicate": 0,
                    "failed": 0, "out_of_stock": 0, "no_price": 0, "not_found": 0, "denied": False}
        known_before = self.storage.existing_keys()

        identities = set()
        for payload in checkpoint.iter_records():          # products processed before an interruption
            if "record" in payload:
                rec = payload["record"]
                identities.add(product_key(rec["site"], rec["sku"], rec["product_url"]))

        status = "completed"
        paused = False
        adapter = None
        try:
            adapter_cls = ADAPTERS[site_name]
            kwargs = {"state_dir": site_state} if adapter_cls is YallaTagerAdapter else {}
            adapter = adapter_cls(site_cfg, session, **kwargs)
            ok, why = adapter.ready()
            if not ok:
                log.error("%s: not run - %s", site_name, why)
            elif checkpoint.site_denied(site_name):
                log.warning("%s denied access earlier in this run - skipped", site_name)
                counters["denied"] = True
            else:
                categories = [] if extra_targets else list(site_cfg["category_urls"])
                products = [] if extra_targets else list(site_cfg["product_urls"])
                categories += [t for t in extra_targets if t["kind"] == "category"]
                products += [t for t in extra_targets if t["kind"] == "product"]
                try:
                    items = collector.collect(adapter, categories, products)
                except AccessDenied as exc:
                    log.error("%s: access denied during discovery (%s) - site stopped", site_name, exc)
                    checkpoint.deny_site(site_name)
                    counters["denied"] = True
                    items = []
                if limit is not None:
                    items = items[:limit]
                counters["discovered"] = len(items)
                paused = self._process_items(site_name, adapter, items, checkpoint, identities, counters, deadline)
        except KeyboardInterrupt:
            status = "interrupted"
            log.warning("%s: interrupted by user - progress saved, the next run will resume", site_name)
        except Exception:
            status = "failed"
            log.exception("%s: run failed unexpectedly - progress saved, the next run will resume", site_name)
        finally:
            if adapter is not None:
                try:
                    adapter.close()
                except Exception:                          # closing must never hide the real error
                    log.debug("error while closing %s", site_name, exc_info=True)
            session.close()

        if paused and status == "completed":
            status = "paused"
        if status == "completed" and counters["denied"]:
            status = "denied"
        finished = status == "completed"
        saved = self._flush(checkpoint, site_name, final=finished and not partial)
        run_keys = {product_key(p["record"]["site"], p["record"]["sku"], p["record"]["product_url"])
                    for p in checkpoint.iter_records() if "record" in p}
        counters["new_products"] = len(run_keys - known_before)
        counters["updated_products"] = len(run_keys & known_before)
        counters["total_in_file"] = saved.get("total", len(known_before))
        counters["price_history_rows"] = sum(len(p.get("history", [])) for p in checkpoint.iter_records()
                                             if "record" in p)
        counters["duration_minutes"] = round((time.monotonic() - t0) / 60, 1)
        counters["status"] = status
        counters["run_id"] = checkpoint.run_id
        if status in ("completed", "denied"):
            checkpoint.finish(status, counters)
        log.info("=== %s run %s %s: %s ===", site_name, checkpoint.run_id, status, counters)
        return counters

    def _process_items(self, site_name, adapter, items, checkpoint: Checkpoint, identities, counters,
                       deadline=None) -> bool:
        """Returns True when the batch's time ran out before every item was processed."""
        max_failures = int(self.cfg["run"]["max_failures_per_product"])
        flush_every = int(self.cfg["run"]["flush_every"])
        since_flush = 0
        for index, item in enumerate(items, 1):
            if deadline is not None and time.monotonic() >= deadline:
                log.info("%s: batch time is up - progress saved, the next batch continues", site_name)
                return True
            key = f"{site_name}|{item.key}"
            state = checkpoint.status_of(key)
            if state in DONE_STATES:
                continue
            if checkpoint.failures_of(key) >= max_failures:
                log.warning("%s: %s failed %d times - not retried in this run", site_name, item.url, max_failures)
                continue
            listing_name = (item.hints or {}).get("name", "")
            if listing_name and not (item.hints or {}).get("explicit"):
                excluded, why = self.data.is_excluded_by_name(listing_name, item.category)
                if excluded:      # accessory recognised from the listing: no need to open its page
                    self._exclude(checkpoint, counters, key, site_name, item.category, listing_name, item.url, why)
                    continue
            log.info("%s [%d/%d] %s", site_name, index, len(items), item.url)
            try:
                product = adapter.scrape(item)
            except NotFound as exc:
                # reported in excluded_products.csv so a removed page is visible
                self._exclude(checkpoint, counters, key, site_name, item.category, listing_name,
                              item.url, f"not found: {exc}", counter="not_found")
                continue
            except (RobotsDisallowed, InvalidURLError) as exc:
                log.warning("%s: skipped %s (%s)", site_name, item.url, exc)
                checkpoint.mark(key, "skipped")
                counters["skipped"] += 1
                continue
            except AccessDenied as exc:
                log.error("%s: access denied (%s) - stopping this site", site_name, exc)
                checkpoint.deny_site(site_name)
                counters["denied"] = True
                break
            except ScrapeError as exc:
                attempts = checkpoint.mark_failed(key)
                counters["failed"] += 1
                log.error("%s: failed %s (attempt %d): %s", site_name, item.url, attempts, exc)
                continue
            except Exception:                                   # parsing bug, unexpected page...
                attempts = checkpoint.mark_failed(key)
                counters["failed"] += 1
                log.exception("%s: unexpected error on %s (attempt %d)", site_name, item.url, attempts)
                continue

            if product is None:
                checkpoint.mark(key, "skipped")
                counters["skipped"] += 1
                continue
            explicit = bool((item.hints or {}).get("explicit"))   # asked for by URL: never filtered out
            excluded, why = (False, "") if explicit else self.data.is_excluded(product)
            if excluded:
                self._exclude(checkpoint, counters, key, site_name, product.category, product.name, product.url, why)
                continue

            decision = self.price.select_lowest(product.offers, context=product.url)
            excluded, why = (False, "") if explicit else self.data.is_excluded_by_price(product, decision)
            if excluded:
                self._exclude(checkpoint, counters, key, site_name, product.category, product.name, product.url, why)
                continue
            scraped_at = iso_now(self.tz)
            try:
                record = self.data.build_record(product, decision, scraped_at)
            except ValidationError as exc:
                log.warning("%s: invalid data skipped: %s", site_name, exc)
                checkpoint.mark(key, "skipped")
                counters["skipped"] += 1
                continue

            identity = product_key(record["site"], record["sku"], record["product_url"])
            if identity in identities:
                log.info("%s: duplicate product %s (%s) skipped", site_name, record["sku"], item.url)
                checkpoint.mark(key, "duplicate")
                counters["duplicate"] += 1
                continue
            identities.add(identity)

            history = self.data.history_rows(product, decision, scraped_at, checkpoint.run_id, record["sku"])
            checkpoint.append_record({"record": record, "history": history})
            if record["availability"] == "out_of_stock":
                log.info("%s: %s -> OUT OF STOCK (last listed price %s %s)", site_name, record["product_name"],
                         record["lowest_price"], record["currency"])
                checkpoint.mark(key, "out_of_stock")
                counters["out_of_stock"] += 1
            elif decision.offer is None:
                log.info("%s: %s has no price on the site (%s)", site_name, product.name, decision.reason)
                checkpoint.mark(key, "no_price")
                counters["no_price"] += 1
            else:
                log.info("%s: %s -> %s %s by %s (%s)", site_name, record["product_name"], record["lowest_price"],
                         record["currency"], record["seller_name"], decision.reason)
                checkpoint.mark(key, "ok")
                counters["ok"] += 1

            since_flush += 1
            if since_flush >= flush_every:
                self._flush(checkpoint, site_name, final=False)
                since_flush = 0
        return False

    def _exclude(self, checkpoint: Checkpoint, counters: Dict, key: str, site: str, category: str,
                 name: str, url: str, reason: str, counter: str = "excluded") -> None:
        log.info("%s: excluded '%s' - %s", site, name, reason)
        checkpoint.append_record({"excluded": {
            "site": site, "category": category, "product_name": name, "reason": reason,
            "product_url": url, "timestamp": iso_now(self.tz), "run_id": checkpoint.run_id}})
        checkpoint.mark(key, "excluded")
        counters[counter] = counters.get(counter, 0) + 1

    def _flush(self, checkpoint: Checkpoint, site_name: str, final: bool) -> Dict:
        records, history, excluded = [], [], []
        for payload in checkpoint.iter_records():
            if "excluded" in payload:
                excluded.append(payload["excluded"])
                continue
            records.append(payload["record"])
            history.extend(payload.get("history", []))
        result: Dict = {"added": 0, "updated": 0, "history_rows": 0}
        with WaitingFileLock(self.storage_lock):
            if records:
                result = self.storage.merge_products(records, run_started_at=checkpoint.started_at if final else None)
                result["history_rows"] = self.storage.append_history(history)
            if final:
                self.storage.write_excluded(excluded, site=site_name)
        return result
