"""
Shared fakes: an offline crawl session, so tests never touch the real websites.

The fake still runs Crawl4AI's real JsonCssExtractionStrategy over saved pages, so the CSS selectors
in every site module are exercised exactly as they are in production.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from crawl4ai import JsonCssExtractionStrategy

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
FIXTURES = Path(__file__).resolve().parent / "fixtures"

from scraper.crawl_engine import CapturedJson, CrawlSettings, ScrapeResult  # noqa: E402
from scraper.errors import NotFound  # noqa: E402


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def fixture_json(name: str):
    return json.loads(fixture_text(name))


class FakeSession:
    """Maps URLs to fixture pages and recorded JSON; records every requested URL."""

    settings = CrawlSettings(min_delay_seconds=0, max_delay_seconds=0, max_retries=0)

    def __init__(self, routes: dict = None, captures: dict = None):
        self.routes = routes or {}
        self.captures = captures or {}             # url -> list[(api_fragment, post_dict, body)]
        self.requested = []
        self.robots_permission = {}

    def scrape(self, url, config):
        self.requested.append(url)
        if url not in self.routes and url not in self.captures:
            raise NotFound(f"no fixture for {url}")
        html = fixture_text(self.routes[url]) if url in self.routes else ""
        items = JsonCssExtractionStrategy(config.schema).extract(url, html) if config.schema and html else []
        captured = [CapturedJson(url=f"https://uat-be.yallatager.com/restfulapi/core/{fragment}",
                                 post_data=json.dumps(post), status=200, body=body)
                    for fragment, post, body in self.captures.get(url, [])]
        return ScrapeResult(url=url, final_url=url, status=200, html=html, items=items, captured=captured)

    def fetch_document(self, url):
        self.requested.append(url)
        if url not in self.routes:
            raise NotFound(f"no fixture for {url}")
        return fixture_text(self.routes[url])

    def close(self):
        pass


@pytest.fixture
def base_cfg(tmp_path):
    from scraper.config import load_config
    cfg = load_config(ROOT / "config.yaml")
    cfg["output"]["products_file"] = str(tmp_path / "output" / "products.csv")
    cfg["output"]["price_history_file"] = str(tmp_path / "output" / "price_history.csv")
    cfg["paths"]["state_dir"] = str(tmp_path / "state")
    cfg["paths"]["log_dir"] = str(tmp_path / "logs")
    cfg["crawl"]["min_delay_seconds"] = 0
    cfg["crawl"]["max_delay_seconds"] = 0
    return cfg


def use_yalla_test_site(cfg):
    """Offline tests run against fixtures captured from the UAT site (uat.yallatager.com, /ar/)."""
    cfg["sites"]["yallatager"].update(
        enabled=True, permission_obtained=True, permission_reference="offline test fixtures",
        base_url="https://uat.yallatager.com", allowed_hosts=["uat.yallatager.com"], store_code="ar",
        image_base_url="")
    return cfg
