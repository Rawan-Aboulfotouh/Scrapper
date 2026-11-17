"""
URL Collector: turns the configured category URLs (with pagination) and individual
product URLs into a de-duplicated list of DiscoveredItem objects.

Finished category discoveries are stored in the checkpoint, so an interrupted run
does not crawl the category pages again when it resumes.
"""
from __future__ import annotations

import logging
from typing import Dict, List

from .errors import AccessDenied, RobotsDisallowed, ScrapeError
from .models import DiscoveredItem
from .sites.base import SiteAdapter
from .state import Checkpoint
from .utils import InvalidURLError

log = logging.getLogger("scraper.collector")


class UrlCollector:
    def __init__(self, checkpoint: Checkpoint):
        self.checkpoint = checkpoint

    def collect(self, adapter: SiteAdapter, category_targets: List[Dict], product_targets: List[Dict]) -> List[DiscoveredItem]:
        """
        ``category_targets`` / ``product_targets``: [{"url": ..., "category": "Mobiles"|"TVs"|"Tablets"}, ...]
        Raises AccessDenied (the whole site must stop); other problems only skip one target.
        """
        unique: Dict[str, DiscoveredItem] = {}

        def add(item: DiscoveredItem) -> None:
            if item.key in unique:
                if unique[item.key].category != item.category:
                    log.info("%s: %s listed under %s and %s - keeping %s",
                             adapter.name, item.url, unique[item.key].category, item.category, unique[item.key].category)
                return
            unique[item.key] = item

        for target in category_targets:
            url, category = target["url"], target["category"]
            target_id = f"{adapter.name}|category|{category}|{url}"
            if self.checkpoint.discovery_done(target_id):
                items = [DiscoveredItem.from_dict(d) for d in self.checkpoint.discovered_items(target_id)]
                log.info("%s: %d products for %s taken from checkpoint", adapter.name, len(items), url)
            else:
                try:
                    items = list(adapter.discover(url, category))
                except AccessDenied:
                    raise
                except RobotsDisallowed as exc:
                    log.warning("%s: skipping category %s (%s)", adapter.name, url, exc)
                    continue
                except (ScrapeError, InvalidURLError) as exc:
                    log.error("%s: category %s failed: %s (will be retried on the next run)", adapter.name, url, exc)
                    continue
                self.checkpoint.save_discovery(target_id, [i.to_dict() for i in items])
                log.info("%s: discovered %d products in %s (%s)", adapter.name, len(items), url, category)
            for item in items:
                add(item)

        for target in product_targets:
            try:
                add(adapter.item_for_product_url(target["url"], target["category"]))
            except InvalidURLError as exc:
                log.error("%s: invalid product URL skipped: %s", adapter.name, exc)

        log.info("%s: %d unique products to process", adapter.name, len(unique))
        return list(unique.values())
