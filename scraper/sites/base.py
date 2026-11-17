"""Common interface implemented by every site adapter."""
from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from typing import Dict, Iterator, List, Optional

from bs4 import BeautifulSoup

from ..crawl_engine import CrawlSession, ScrapeConfig, ScrapeResult, scrape_site
from ..models import DiscoveredItem, ScrapedProduct
from ..utils import clean_text, model_code_from_name, validate_url


class SiteAdapter(ABC):
    """
    URL Collector + Product Scraper for one website.

    * ``discover(category_url, category)`` walks a category and its pagination and
      yields DiscoveredItem objects (one per unique product).
    * ``scrape(item)`` visits the product page and returns a ScrapedProduct with all
      seller offers, or None when the page has no product.

    Each site module declares a ``LISTING`` and a ``PRODUCT`` :class:`ScrapeConfig`: the CSS
    selectors for its fields plus its Crawl4AI wait and scroll behaviour.
    """

    name: str = ""
    default_sku_source: str = "store"   # "store" or "model_from_name" (overridable in config: sku_source)

    def __init__(self, site_cfg: dict, session: CrawlSession):
        self.cfg = site_cfg
        self.session = session
        self.hosts: List[str] = site_cfg["allowed_hosts"]
        self.base_url: str = site_cfg["base_url"].rstrip("/")
        self.seller_name: str = site_cfg.get("seller_name", "")
        self.currency: str = site_cfg.get("default_currency", "EGP")
        self.max_pages: int = int(site_cfg.get("max_pages_per_category", 50))
        self.log = logging.getLogger(f"scraper.site.{self.name}")

    # ---------------------------------------------------------------- helpers
    def validate(self, url: str) -> str:
        return validate_url(url, self.hosts)

    def absolute(self, href: str) -> str:
        if not href:
            return ""
        if href.startswith(("http://", "https://")):
            return href
        return self.base_url + ("" if href.startswith("/") else "/") + href

    def load(self, url: str, config: ScrapeConfig) -> ScrapeResult:
        return scrape_site(url, config, self.session)

    @staticmethod
    def json_ld_products(soup: BeautifulSoup) -> List[Dict]:
        """All schema.org Product objects found in JSON-LD blocks (handles @graph and lists)."""
        found: List[Dict] = []

        def walk(node):
            if isinstance(node, list):
                for child in node:
                    walk(child)
            elif isinstance(node, dict):
                types = node.get("@type")
                types = types if isinstance(types, list) else [types]
                if "Product" in types:
                    found.append(node)
                for key in ("@graph", "mainEntity", "itemListElement"):
                    if key in node:
                        walk(node[key])

        for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
            try:
                walk(json.loads(script.string or script.get_text() or ""))
            except ValueError:
                continue
        return found

    @staticmethod
    def json_ld_offer(product: Dict) -> Dict:
        """The first schema.org Offer of a JSON-LD product, or an empty dict."""
        offers = product.get("offers") or {}
        if isinstance(offers, list):
            offers = offers[0] if offers else {}
        return offers if isinstance(offers, dict) else {}

    @staticmethod
    def json_ld_in_stock(offer: Dict) -> Optional[bool]:
        availability = clean_text(offer.get("availability")).lower().replace(" ", "")
        if not availability:
            return None
        return "instock" in availability or "limitedavailability" in availability

    @staticmethod
    def json_ld_condition(offer: Dict) -> str:
        condition = clean_text(offer.get("itemCondition")).lower()
        if "refurbished" in condition:
            return "renewed"
        if "used" in condition:
            return "used"
        return "new"

    @staticmethod
    def json_ld_image(product: Dict) -> str:
        image = product.get("image")
        if isinstance(image, list):
            image = image[0] if image else ""
        if isinstance(image, dict):
            image = image.get("url", "")
        return clean_text(image)

    @staticmethod
    def json_ld_brand(product: Dict) -> str:
        brand = product.get("brand")
        return clean_text(brand.get("name") if isinstance(brand, dict) else brand)

    @staticmethod
    def specs_from_rows(rows, label: str = "label", value: str = "value") -> Dict[str, str]:
        """Turn a schema ``list`` field of {label, value} rows into an ordered dictionary."""
        specs: Dict[str, str] = {}
        for row in rows or []:
            key, val = clean_text(row.get(label)), clean_text(row.get(value))
            if key and val:
                specs.setdefault(key, val)
        return specs

    def choose_sku(self, name: str, store_sku: str, specs: Dict[str, str], category: str = "TVs") -> str:
        """
        The store's own SKU is often an internal number (Elsindbad "11691", Noon "N70215105V").
        With sku_source = "model_from_name" a TV uses the manufacturer model code instead:
            1. model code written in the product name ("... 43LM6370PVA")
            2. a "Model" specification that looks like a code (not "A200 Plus")
            3. the store's SKU
        Phones and tablets always keep the store's SKU. One model code covers every colour and
        storage size, and each of those is a different product with its own price.
        The store's SKU is always kept in the store_sku column.
        """
        if self.cfg.get("sku_source", self.default_sku_source) != "model_from_name" or category != "TVs":
            return store_sku
        from ..specs import canonical_for          # local import avoids a cycle
        model = model_code_from_name(name)
        if not model:
            codes = (model_code_from_name(v) for k, v in specs.items() if canonical_for(k) == "model")
            model = next((code for code in codes if code), "")
        return model or store_sku

    # ---------------------------------------------------------------- interface
    @abstractmethod
    def canonical_url(self, url: str) -> str: ...

    @abstractmethod
    def discover(self, category_url: str, category: str) -> Iterator[DiscoveredItem]: ...

    @abstractmethod
    def scrape(self, item: DiscoveredItem) -> Optional[ScrapedProduct]: ...

    def item_for_product_url(self, url: str, category: str) -> DiscoveredItem:
        url = self.canonical_url(self.validate(url))
        return DiscoveredItem(site=self.name, url=url, category=category, key=self.item_key(url),
                              hints={"explicit": True})

    def item_key(self, url: str) -> str:
        """Identity of a product inside this site; overridden where the URL has several forms."""
        return f"url:{url}"

    def ready(self):
        """(True, "") when the adapter can run; (False, reason) otherwise."""
        return True, ""

    def close(self) -> None:
        pass
