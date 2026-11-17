"""
Carrefour Egypt (carrefouregypt.com/mafegy/en): Next.js with Tailwind utility classes.

Crawl4AI strategy: plain page load, waiting for a product link, then the rendered card markup.

Discovery works around one robots.txt rule. Paging uses ``?currentPage=N``, and robots.txt says
``Disallow: *?currentPage=*``, so only the first page (60 products) of a category may be read.
Coverage comes from two permitted sources instead:

* the first page of several narrow categories (by type, screen size and price band), and
* the product sitemaps the site publishes for crawlers, filtered by the category slug that is the
  first path segment of each product URL (``/mafegy/en/uhd-tv/<name>/p/676576``).

The same product reached both ways is one item: identity is the numeric product id.

Product pages have schema.org JSON-LD for name, sku, image, price and availability. An out-of-stock
product has no price in it. The seller is in the page's embedded data next to the product's own
image (``"shopId":"0000","shopName":"Carrefour"``). Specifications are label/value rows; a
"View More" button is clicked first so that all of them are in the DOM.
"""
from __future__ import annotations

import re
from typing import Dict, Iterator, List, Optional
from urllib.parse import urlsplit

from ..crawl_engine import ScrapeConfig, ScrapeResult
from ..models import DiscoveredItem, Offer, ScrapedProduct
from ..utils import clean_text, detect_currency, parse_price, strip_query_params
from .base import SiteAdapter

EXPAND_SPECS = """
for (const button of document.querySelectorAll('button, div[role="button"]')) {
    if ((button.textContent || '').trim() === 'View More') { button.click(); }
}
"""

LISTING = ScrapeConfig(
    wait_selector='a[href*="/p/"]',
    settle_seconds=1.5,
    schema={
        "name": "carrefour_listing",
        "baseSelector": 'div.group:has(a[href*="/p/"])',
        "fields": [
            {"name": "link", "selector": 'a[href*="/p/"]', "type": "attribute", "attribute": "href"},
            {"name": "name", "selector": "span.line-clamp-2", "type": "text"},
            {"name": "price", "selector": "span.font-bold.text-lg:nth-of-type(2)", "type": "text"},
            {"name": "currency", "selector": "span.font-bold.text-lg:nth-of-type(1)", "type": "text"},
            {"name": "image", "selector": 'img[alt="Product image 1"]', "type": "attribute", "attribute": "src"},
        ],
    },
)

PRODUCT = ScrapeConfig(
    wait_selector="h1",
    settle_seconds=2.0,
    js_code=(EXPAND_SPECS,),
    schema={
        "name": "carrefour_product",
        "baseSelector": "html",
        "fields": [
            {"name": "name", "selector": "h1", "type": "text"},
            {"name": "specs", "selector": "div.flex.flex-row.gap-xl:has(> div.font-regular):has(> div.font-bold)",
             "type": "list", "fields": [
                 {"name": "label", "selector": "div.font-regular", "type": "text"},
                 {"name": "value", "selector": "div.font-bold", "type": "text"},
             ]},
        ],
    },
)

_PRODUCT_ID = re.compile(r"/p/(\d+)")
_SITEMAP_URL = re.compile(r"https://www\.carrefouregypt\.com/mafegy/en/[^\s<\"']+/p/\d+")
_BRAND_PREFIX = re.compile(r"^(?:non)?food_", re.I)


def product_id_from_url(url: str) -> str:
    match = _PRODUCT_ID.search(url or "")
    return match.group(1) if match else ""


def category_slug(url: str) -> str:
    """'/mafegy/en/uhd-tv/name/p/1' -> 'uhd-tv'."""
    parts = [p for p in urlsplit(url).path.split("/") if p]
    return parts[2] if len(parts) > 3 else ""


def seller_from_page(html: str, product_id: str) -> str:
    """The seller stored in the embedded page data beside this product's own image."""
    if not product_id:
        return ""
    match = re.search(re.escape(product_id) + r'_main[^"\\]*\\?",\s*\\?"shopId\\?":\s*\\?"[^"\\]*\\?",\s*'
                      r'\\?"shopName\\?":\s*\\?"([^"\\]+)', html or "")
    return clean_text(match.group(1)) if match else ""


class CarrefourAdapter(SiteAdapter):
    name = "carrefour"
    default_sku_source = "model_from_name"

    def __init__(self, site_cfg: dict, session):
        super().__init__(site_cfg, session)
        self.sitemap_slugs: Dict[str, List[str]] = site_cfg.get("sitemap_slugs") or {}
        self._sitemap_cache: Dict[str, List[str]] = {}

    def canonical_url(self, url: str) -> str:
        return strip_query_params(url, keep_only=())    # offer/sid/sellerId only pick the offer shown

    def item_key(self, url: str) -> str:
        product_id = product_id_from_url(url)
        return f"id:{product_id}" if product_id else f"url:{url}"

    # ------------------------------------------------------------------ discovery
    def discover(self, category_url: str, category: str) -> Iterator[DiscoveredItem]:
        if "/sitemaps/" in category_url:
            yield from self._discover_sitemap(self.validate(category_url), category)
        else:
            yield from self._discover_listing(self.validate(category_url), category)

    def _discover_listing(self, url: str, category: str) -> Iterator[DiscoveredItem]:
        result = self.load(url, LISTING)        # first page only: robots.txt disallows ?currentPage=
        seen = set()
        for raw in result.items:
            if not raw.get("link"):
                continue
            product_url = self.canonical_url(self.absolute(raw["link"]))
            key = self.item_key(product_url)
            if key in seen:
                continue
            seen.add(key)
            hints = {"url": product_url, "name": clean_text(raw.get("name")), "price": clean_text(raw.get("price")),
                     "currency": clean_text(raw.get("currency")), "image": raw.get("image") or ""}
            yield DiscoveredItem(site=self.name, url=product_url, category=category, key=key, hints=hints)

    def _sitemap_urls(self, sitemap_url: str) -> List[str]:
        if sitemap_url not in self._sitemap_cache:
            self._sitemap_cache[sitemap_url] = sorted(set(_SITEMAP_URL.findall(self.session.fetch_document(sitemap_url))))
        return self._sitemap_cache[sitemap_url]

    def _discover_sitemap(self, sitemap_url: str, category: str) -> Iterator[DiscoveredItem]:
        wanted = set(self.sitemap_slugs.get(category) or [])
        for product_url in self._sitemap_urls(sitemap_url):
            if category_slug(product_url) in wanted:
                product_url = self.canonical_url(product_url)
                yield DiscoveredItem(site=self.name, url=product_url, category=category,
                                     key=self.item_key(product_url), hints={"url": product_url})

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
        in_stock = self.json_ld_in_stock(offer_ld)
        price = parse_price(offer_ld.get("price"))
        if price is None and in_stock is not False:     # the listing price is only trusted for sellable items
            price = parse_price(hints.get("price"))
        currency = clean_text(offer_ld.get("priceCurrency")).upper() or detect_currency(
            hints.get("currency") or "", self.currency)

        store_sku = clean_text(ld.get("sku")) or product_id_from_url(item.url)
        seller = seller_from_page(result.html, store_sku) or self.seller_name
        specs = self.specs_from_rows(css.get("specs"))
        sku = self.choose_sku(name, store_sku, specs, item.category)
        # JSON-LD says "nonfood_samsung", and sometimes only "nonfood_"; names start with the brand
        brand = _BRAND_PREFIX.sub("", self.json_ld_brand(ld)).replace("_", " ").strip().title() or name.split()[0]
        image = self.json_ld_image(ld) or hints.get("image", "")
        offer = Offer(seller_name=seller, price=price, currency=currency or self.currency,
                      in_stock=in_stock, url=item.url, sku=store_sku, source="product")
        return ScrapedProduct(site=self.name, url=item.url, category=item.category, name=name, sku=sku,
                              image_url=image, specs=specs, offers=[offer],
                              extra={"brand": brand, "store_sku": store_sku,
                                     "condition": self.json_ld_condition(offer_ld)})
