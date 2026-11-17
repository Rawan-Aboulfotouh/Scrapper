"""
Vodafone Egypt eShop (eshop.vodafone.com.eg): Angular single-page app.

Crawl4AI strategy: the shell HTML is empty, so the crawler waits for rendered ``vf-product-card``
elements. The cards' links are ``href="#"`` (navigation happens in JavaScript), so the product URL
cannot come from the DOM. The page's own catalogue responses are captured passively instead; they
hold the URI, SKU, price and brand of every card. No API is called by this scraper.

Pagination is infinite scroll: the listing shows 16 products and requests the next page when the
user scrolls. ``scroll_until_complete`` keeps scrolling, with polite pauses, until the catalogue
response says it was the last page.

Product pages carry JSON-LD inside ``@graph``. Specifications are read from the page's own
``product-specifications`` response. Single-seller store. The catalogue only lists in-stock items.
"""
from __future__ import annotations

import asyncio
import random
import re
from typing import Dict, Iterator, List, Optional
from urllib.parse import parse_qs, urlsplit

from ..crawl_engine import CapturedJson, ScrapeConfig, ScrapeResult
from ..models import DiscoveredItem, Offer, ScrapedProduct
from ..utils import clean_text, parse_price, strip_query_params
from .base import SiteAdapter

LIST_API = "catalog-browse/search/catalog"
SPECS_API = "catalog/product-specifications/"
SCROLL_JS = "window.scrollTo(0, document.body.scrollHeight);"


def list_responses(captured: List[CapturedJson]) -> List[Dict]:
    """Catalogue responses that belong to the product grid."""
    return [c.body for c in captured
            if LIST_API in c.url and isinstance(c.body, dict) and isinstance(c.body.get("content"), list)]


def make_scroll_driver(max_pages: int, min_delay: float, max_delay: float, response_timeout: float = 15):
    """Build the page driver that scrolls until the catalogue is exhausted or ``max_pages`` is reached."""

    async def scroll_until_complete(page, captured: List[CapturedJson]) -> None:
        stale_rounds = 0
        while stale_rounds < 3:
            bodies = list_responses(captured)
            if not bodies:
                return
            if any(b.get("last") for b in bodies) or len(bodies) >= max_pages:
                return
            before = len(bodies)
            await asyncio.sleep(random.uniform(min_delay, max_delay))
            await page.evaluate(SCROLL_JS)
            waited = 0.0
            while waited < response_timeout and len(list_responses(captured)) == before:
                await asyncio.sleep(0.5)
                waited += 0.5
            stale_rounds = stale_rounds + 1 if len(list_responses(captured)) == before else 0

    return scroll_until_complete


PRODUCT = ScrapeConfig(
    wait_selector="h1",
    wait_timeout_ms=25_000,
    settle_seconds=2.0,
    capture_json=(SPECS_API,),
    capture_optional=(SPECS_API,),
    capture_optional_timeout_seconds=20,
    not_found_paths=("/en/error", "/ar/error"),
    schema={
        "name": "vodafone_product",
        "baseSelector": "html",
        "fields": [
            {"name": "name", "selector": "h1", "type": "text"},
            {"name": "add_to_cart", "selector": 'button[class*="add-to-cart" i], button[id*="add-to-cart" i]',
             "type": "text"},
        ],
    },
)


def category_path(category_url: str) -> str:
    """'.../en/list?categoryUrls=/tv' -> '/tv'."""
    values = parse_qs(urlsplit(category_url).query).get("categoryUrls") or [""]
    return values[0]


def specs_from_payload(payload) -> Dict[str, str]:
    """Flatten ``specificationsGroup[].specifications[]`` into label -> value."""
    specs: Dict[str, str] = {}

    def walk(node):
        if isinstance(node, list):
            for child in node:
                walk(child)
        elif isinstance(node, dict):
            definition = node.get("specification")
            if isinstance(definition, dict) and definition.get("label") and "description" in node:
                label, value = clean_text(definition["label"]), clean_text(node.get("description"))
                if label and value:
                    specs.setdefault(label, value)
                return
            for child in node.values():
                if isinstance(child, (list, dict)):
                    walk(child)

    walk(payload)
    return specs


class VodafoneAdapter(SiteAdapter):
    name = "vodafone"
    default_sku_source = "model_from_name"

    def __init__(self, site_cfg: dict, session):
        super().__init__(site_cfg, session)
        settings = session.settings
        self.listing = ScrapeConfig(
            wait_selector="vf-product-card",
            wait_timeout_ms=25_000,
            settle_seconds=1.0,
            capture_json=(LIST_API,),
            capture_wait_for=(LIST_API,),
            page_driver=make_scroll_driver(self.max_pages, settings.min_delay_seconds, settings.max_delay_seconds),
            not_found_paths=("/en/error", "/ar/error"),
            schema={
                "name": "vodafone_listing",
                "baseSelector": "vf-product-card",
                "fields": [
                    {"name": "name", "selector": ".product-title", "type": "text"},
                    {"name": "price", "selector": ".sale-price", "type": "text"},
                    {"name": "image", "selector": ".product-img img", "type": "attribute", "attribute": "src"},
                ],
            },
        )

    def canonical_url(self, url: str) -> str:
        return strip_query_params(url, keep_only=())

    def product_url(self, uri: str) -> str:
        return f"{self.base_url}/en/prod/{uri.lstrip('/')}"

    # ------------------------------------------------------------------ listing
    def items_from_catalogue(self, bodies: List[Dict], category_url: str, category: str) -> Iterator[DiscoveredItem]:
        wanted = category_path(category_url)
        seen = set()
        for body in bodies:
            for row in body.get("content") or []:
                uri, categories = clean_text(row.get("uri")), row.get("categoryUrls") or []
                # the same response type also serves the header's suggestions: keep this category only
                if not uri or uri in seen or (wanted and categories and wanted not in categories):
                    continue
                seen.add(uri)
                product_url = self.canonical_url(self.product_url(uri))
                price = row.get("bestPrice") or row.get("salePrice") or row.get("price") or {}
                # bundles have no SKU in the catalogue, only the catalogue's own id
                hints = {"url": product_url, "name": clean_text(row.get("name")),
                         "sku": clean_text(row.get("sku")) or clean_text(row.get("id")),
                         "price": price.get("amount"), "currency": clean_text(price.get("currency")),
                         "image": row.get("primaryAssetContentUrl") or "",
                         "brand": clean_text(row.get("brandDisplayValue")),
                         "condition": clean_text(row.get("productCondition")).lower()}
                yield DiscoveredItem(site=self.name, url=product_url, category=category,
                                     key=f"url:{product_url}", hints=hints)

    def discover(self, category_url: str, category: str) -> Iterator[DiscoveredItem]:
        category_url = self.validate(category_url)
        result = self.load(category_url, self.listing)
        bodies = list_responses(result.captured)
        if not bodies:
            self.log.error("%s: the page did not load a product list", category_url)
            return
        yield from self.items_from_catalogue(bodies, category_url, category)

    # ------------------------------------------------------------------ product
    def scrape(self, item: DiscoveredItem) -> Optional[ScrapedProduct]:
        return self.parse_product(self.load(self.validate(item.url), PRODUCT), item)

    def parse_product(self, result: ScrapeResult, item: DiscoveredItem) -> Optional[ScrapedProduct]:
        soup, css = result.soup(), result.item
        ld = (self.json_ld_products(soup) or [{}])[0]
        hints: Dict = item.hints or {}

        name = clean_text(ld.get("name")) or clean_text(css.get("name")) or hints.get("name", "")
        if not name:
            self.log.warning("no product found on %s", item.url)
            return None

        offer_ld = self.json_ld_offer(ld)
        price = parse_price(offer_ld.get("price")) or parse_price(hints.get("price"))
        currency = clean_text(offer_ld.get("priceCurrency")).upper() or hints.get("currency") or self.currency
        in_stock = self.json_ld_in_stock(offer_ld)
        if in_stock is None:
            in_stock = True if css.get("add_to_cart") is not None or hints.get("price") else None

        specs: Dict[str, str] = {}
        for captured in result.find(SPECS_API):
            for key, value in specs_from_payload(captured.body).items():
                specs.setdefault(key, value)

        store_sku = clean_text(ld.get("sku")) or hints.get("sku", "")
        sku = self.choose_sku(name, store_sku, specs, item.category)
        brand = self.json_ld_brand(ld) or hints.get("brand", "")
        image = self.json_ld_image(ld) or hints.get("image", "")
        condition = self.json_ld_condition(offer_ld) if offer_ld else "new"
        if re.search(r"refurb|renew", hints.get("condition") or ""):
            condition = "renewed"
        offer = Offer(seller_name=self.seller_name, price=price, currency=currency,
                      in_stock=in_stock, url=item.url, sku=store_sku, source="product")
        return ScrapedProduct(site=self.name, url=item.url, category=item.category, name=name, sku=sku,
                              image_url=image, specs=specs, offers=[offer],
                              extra={"brand": brand, "store_sku": store_sku, "condition": condition})
