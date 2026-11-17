"""
Noon Egypt (noon.com/egypt-en): server-rendered React with hashed CSS-module class names.

Crawl4AI strategy: plain page load. The product grid is in the first HTML, so the crawler waits for
one product card. Class names look like ``_productBoxLink_1ts6x_5``; the middle part is stable and
the hash is not, so every selector uses ``[class*="productBoxLink"]`` style substring matching.

Pagination is ``?limit=50&page=N`` (allowed by robots.txt). There is no next link in the HTML, so
paging stops at the first page that adds no new products.

Product pages carry a complete schema.org JSON-LD block: name, sku, model, brand, image, offer
price, availability and every specification.

Seller limitation: the "More offers from other sellers" panel loads from ``/_svc/`` / ``/_vs/``
paths, which robots.txt disallows, so it is not opened. Noon rows hold the featured seller's offer.
"""
from __future__ import annotations

import re
from typing import Dict, Iterator, Optional

from ..crawl_engine import ScrapeConfig, ScrapeResult
from ..models import DiscoveredItem, Offer, ScrapedProduct
from ..utils import clean_text, detect_currency, parse_price, set_query_param, strip_query_params
from .base import SiteAdapter

LISTING = ScrapeConfig(
    wait_selector='a[class*="productBoxLink"]',
    settle_seconds=1.0,
    schema={
        "name": "noon_listing",
        "baseSelector": 'a[class*="productBoxLink"]',
        "fields": [
            {"name": "link", "type": "attribute", "attribute": "href"},
            {"name": "name", "selector": 'h2[data-qa="plp-product-box-name"]', "type": "text"},
            {"name": "price", "selector": 'strong[class*="amount"]', "type": "text"},
            {"name": "currency", "selector": 'span[class*="currency"]', "type": "text"},
            {"name": "image", "selector": 'img[src*="pzsku"]', "type": "attribute", "attribute": "src"},
            {"name": "sold_out", "selector": '[class*="outOfStock"], [class*="soldOut"]', "type": "text"},
        ],
    },
)

PRODUCT = ScrapeConfig(
    wait_selector='[class*="priceNowText"], h1',
    settle_seconds=1.0,
    schema={
        "name": "noon_product",
        "baseSelector": "html",
        "fields": [
            {"name": "name", "selector": "h1", "type": "text"},
            {"name": "price", "selector": '[class*="priceNowText"]', "type": "text"},
            {"name": "seller", "selector": 'strong[class*="soldBy"]', "type": "text"},
            {"name": "sold_out", "selector": '[class*="outOfStock"], [class*="soldOut"]', "type": "text"},
            {"name": "image", "selector": 'img[src*="pzsku"]', "type": "attribute", "attribute": "src"},
            {"name": "specs", "selector": "table tr", "type": "list", "fields": [
                {"name": "label", "selector": "td:nth-of-type(1)", "type": "text"},
                {"name": "value", "selector": "td:nth-of-type(2)", "type": "text"},
            ]},
        ],
    },
)

_PRODUCT_ID = re.compile(r"/(N[0-9A-Z]+[A-Z0-9])/p/?", re.I)


def product_id_from_url(url: str) -> str:
    match = _PRODUCT_ID.search(url or "")
    return match.group(1).upper() if match else ""


class NoonAdapter(SiteAdapter):
    name = "noon"
    default_sku_source = "model_from_name"     # Noon's own code is N70215105V; the model code matches other stores

    def canonical_url(self, url: str) -> str:
        return strip_query_params(url, keep_only=())    # "?o=<offer>" only selects the seller shown

    def item_key(self, url: str) -> str:
        product_id = product_id_from_url(url)
        return f"id:{product_id}" if product_id else f"url:{url}"

    # ------------------------------------------------------------------ listing
    def page_url(self, category_url: str, page: int) -> str:
        return set_query_param(set_query_param(category_url, "limit", 50), "page", page)

    def discover(self, category_url: str, category: str) -> Iterator[DiscoveredItem]:
        category_url = self.validate(category_url)
        seen = set()
        for page in range(1, self.max_pages + 1):
            result = self.load(self.validate(self.page_url(category_url, page)), LISTING)
            new_count = 0
            for raw in result.items:
                if not raw.get("link"):
                    continue
                product_url = self.canonical_url(self.absolute(raw["link"]))
                key = self.item_key(product_url)
                if key in seen:
                    continue
                seen.add(key)
                new_count += 1
                hints = {"url": product_url, "name": clean_text(raw.get("name")),
                         "price": clean_text(raw.get("price")), "currency": clean_text(raw.get("currency")),
                         "image": raw.get("image") or "",
                         "in_stock": False if raw.get("sold_out") is not None else None}
                yield DiscoveredItem(site=self.name, url=product_url, category=category, key=key, hints=hints)
            self.log.info("%s page %d: %d new products", category_url, page, new_count)
            if new_count == 0:
                break

    # ------------------------------------------------------------------ product
    def scrape(self, item: DiscoveredItem) -> Optional[ScrapedProduct]:
        return self.parse_product(self.load(self.validate(item.url), PRODUCT), item)

    def parse_product(self, result: ScrapeResult, item: DiscoveredItem) -> Optional[ScrapedProduct]:
        soup, css = result.soup(), result.item
        ld = (self.json_ld_products(soup) or [{}])[0]
        hints: Dict = item.hints or {}

        brand = self.json_ld_brand(ld)
        name = clean_text(ld.get("name")) or clean_text(css.get("name")) or hints.get("name", "")
        if not name:
            self.log.warning("no product found on %s", item.url)
            return None
        if brand and not name.lower().startswith(brand.lower()):    # Noon's title often omits the brand
            name = f"{brand} {name}"

        offer_ld = self.json_ld_offer(ld)
        price = parse_price(offer_ld.get("price")) or parse_price(css.get("price")) or parse_price(hints.get("price"))
        currency = clean_text(offer_ld.get("priceCurrency")).upper() or detect_currency(
            hints.get("currency") or "", self.currency)
        in_stock = self.json_ld_in_stock(offer_ld)
        if in_stock is None:
            in_stock = False if css.get("sold_out") is not None else hints.get("in_stock")

        specs: Dict[str, str] = {}
        for prop in ld.get("additionalProperty") or []:
            if isinstance(prop, dict) and clean_text(prop.get("name")) and clean_text(prop.get("value")):
                specs.setdefault(clean_text(prop["name"]), clean_text(prop["value"]))
        for key, value in self.specs_from_rows(css.get("specs")).items():
            specs.setdefault(key, value)
        if clean_text(ld.get("model")):
            specs.setdefault("Model Number", clean_text(ld["model"]))
        if clean_text(ld.get("color")):
            specs.setdefault("Color", clean_text(ld["color"]))

        store_sku = clean_text(ld.get("sku")) or product_id_from_url(item.url)
        sku = self.choose_sku(name, store_sku, specs, item.category)
        image = self.json_ld_image(ld) or clean_text(css.get("image")) or hints.get("image", "")
        offer = Offer(seller_name=clean_text(css.get("seller")), price=price, currency=currency or self.currency,
                      in_stock=in_stock, url=item.url, sku=store_sku, source="product")
        return ScrapedProduct(site=self.name, url=item.url, category=item.category, name=name, sku=sku,
                              image_url=image, specs=specs, offers=[offer],
                              extra={"brand": brand, "store_sku": store_sku,
                                     "condition": self.json_ld_condition(offer_ld)})
