"""
Awad Serag (awadserag.com): Odoo eCommerce, server-rendered HTML.

Crawl4AI strategy: plain page load, no scrolling. Category pages are ``/shop/category/<id>`` with
pagination ``/shop/category/<id>/page/N``; cards are ``.oe_product`` with schema.org microdata.
The listing shows an "OUT OF STOCK" badge which is not on the product page, so it is carried to
the product scraper as a hint.

SKU: Odoo's internal reference shown as "[500020655] Product name" (image alt text); fallback
``AS-TPL-<template id>`` from the URL. Single-seller store: the seller is the store itself.
"""
from __future__ import annotations

import re
from typing import Dict, Iterator, Optional
from urllib.parse import urlsplit

from ..crawl_engine import ScrapeConfig, ScrapeResult
from ..models import DiscoveredItem, Offer, ScrapedProduct
from ..utils import clean_text, detect_currency, extract_key_values, parse_price, strip_query_params
from .base import SiteAdapter

REF_PATTERN = re.compile(r"\[([A-Za-z0-9][A-Za-z0-9\-_./]{2,40})\]")
OUT_OF_STOCK = re.compile(r"out\s*of\s*stock|غير\s*متوفر|نفذ|نفدت|نفذت", re.I)
NEXT_LABELS = ("التالي", "next", "›", "»")

LISTING = ScrapeConfig(
    wait_selector=".oe_product, #products_grid",
    schema={
        "name": "awadserag_listing",
        "baseSelector": ".oe_product",
        "fields": [
            {"name": "link", "selector": 'a[itemprop="url"], a[href^="/shop/"]', "type": "attribute",
             "attribute": "href"},
            {"name": "name_attr", "selector": '[itemprop="name"]', "type": "attribute", "attribute": "content"},
            {"name": "name", "selector": '[itemprop="name"]', "type": "text"},
            {"name": "price", "selector": '[itemprop="price"], .oe_currency_value', "type": "text"},
            {"name": "currency", "selector": '[itemprop="priceCurrency"]', "type": "text"},
            {"name": "image_alt", "selector": "img[alt]", "type": "attribute", "attribute": "alt"},
            {"name": "badge", "selector": ".tp-product-stock-label, .o_wsale_product_out_of_stock, .badge",
             "type": "text"},
        ],
    },
)

PRODUCT = ScrapeConfig(
    wait_selector="#product_details, h1",
    schema={
        "name": "awadserag_product",
        "baseSelector": "html",
        "fields": [
            {"name": "name", "selector": 'h1[itemprop="name"], #product_details h1, h1', "type": "text"},
            {"name": "price", "selector": '.product_price [itemprop="price"], [itemprop="price"], '
                                          '.oe_price .oe_currency_value', "type": "text"},
            {"name": "currency", "selector": '[itemprop="priceCurrency"]', "type": "text"},
            {"name": "price_text", "selector": ".oe_price", "type": "text"},
            {"name": "badge", "selector": ".tp-product-stock-label, #out_of_stock_message", "type": "text"},
            {"name": "image", "selector": 'meta[property="og:image"]', "type": "attribute", "attribute": "content"},
            {"name": "specs", "selector": "#product_specifications tr", "type": "list", "fields": [
                {"name": "label", "selector": "td:nth-of-type(1)", "type": "text"},
                {"name": "value", "selector": "td:nth-of-type(2)", "type": "text"},
            ]},
        ],
    },
)


def ref_from_text(text: str) -> str:
    """'[500011416] مبرد هواء' -> '500011416' (must contain a digit)."""
    for match in REF_PATTERN.finditer(text or ""):
        if any(ch.isdigit() for ch in match.group(1)):
            return match.group(1)
    return ""


def template_id_from_url(url: str) -> str:
    match = re.search(r"-(\d+)/?$", urlsplit(url).path)
    return match.group(1) if match else ""


class AwadSeragAdapter(SiteAdapter):
    name = "awadserag"
    default_sku_source = "model_from_name"     # store SKU is an internal reference

    def canonical_url(self, url: str) -> str:
        # "?category=13" only tells which menu was used; the product is the same
        return strip_query_params(url, keep_only=())

    # ------------------------------------------------------------------ listing
    @staticmethod
    def card_from_item(raw: Dict) -> Optional[Dict]:
        link = raw.get("link") or ""
        if not link or "/shop/category/" in link:
            return None
        # the store shows an "OUT OF STOCK" badge on the card; no badge = available
        in_stock = not (raw.get("badge") and OUT_OF_STOCK.search(raw["badge"]))
        return {"url": link, "name": clean_text(raw.get("name_attr") or raw.get("name")),
                "sku": ref_from_text(raw.get("image_alt") or ""),
                "price": clean_text(raw.get("price")) or None,
                "currency": clean_text(raw.get("currency")) or None,
                "in_stock": in_stock}

    @staticmethod
    def next_page_url(result: ScrapeResult) -> Optional[str]:
        for link in result.soup().select("ul.pagination a[href]"):
            if link["href"] and clean_text(link.get_text()).lower() in NEXT_LABELS:
                return link["href"]
        return None

    def discover(self, category_url: str, category: str) -> Iterator[DiscoveredItem]:
        url = self.validate(strip_query_params(category_url, keep_only=()))
        seen = set()
        visited_pages = set()
        for page in range(1, self.max_pages + 1):
            visited_pages.add(url)
            result = self.load(url, LISTING)
            new_count = 0
            for raw in result.items:
                card = self.card_from_item(raw)
                if card is None:
                    continue
                product_url = self.canonical_url(self.absolute(card["url"]))
                if product_url in seen:
                    continue
                seen.add(product_url)
                new_count += 1
                yield DiscoveredItem(site=self.name, url=product_url, category=category,
                                     key=f"url:{product_url}", hints=card)
            self.log.info("%s page %d: %d new products", url, page, new_count)
            next_url = self.next_page_url(result)
            if not next_url or new_count == 0:
                break
            next_url = self.validate(self.absolute(next_url))
            if next_url in visited_pages:
                break
            url = next_url

    # ------------------------------------------------------------------ product
    def scrape(self, item: DiscoveredItem) -> Optional[ScrapedProduct]:
        return self.parse_product(self.load(self.validate(item.url), PRODUCT), item)

    def parse_product(self, result: ScrapeResult, item: DiscoveredItem) -> Optional[ScrapedProduct]:
        soup, css = result.soup(), result.item
        hints: Dict = item.hints or {}
        name = clean_text(css.get("name")) or hints.get("name", "")
        if not name:
            self.log.warning("no product found on %s", item.url)
            return None

        price = parse_price(css.get("price"))
        if price is None:
            price = parse_price(hints.get("price"))
        currency = clean_text(css.get("currency")).upper() or detect_currency(
            css.get("price_text") or "", detect_currency(hints.get("currency") or "", self.currency))

        # the listing card carries the reference; on the product page look only inside the
        # main product block so references of "similar products" are never picked up
        main_block = soup.select_one("#product_detail, #product_details, .oe_website_sale")
        sku = hints.get("sku") or (ref_from_text(str(main_block)) if main_block else "")
        if not sku:
            template_id = template_id_from_url(item.url)
            sku = f"AS-TPL-{template_id}" if template_id else ""

        in_stock = hints.get("in_stock")
        if css.get("badge") and OUT_OF_STOCK.search(css["badge"]):
            in_stock = False

        image = clean_text(css.get("image"))
        if not image:
            template_id = template_id_from_url(item.url)
            image = f"{self.base_url}/web/image/product.template/{template_id}/image_1024" if template_id else ""

        specs = {}
        for row in css.get("specs") or []:
            label, value = clean_text(row.get("label")), clean_text(row.get("value"))
            if label and value:
                specs.setdefault(label, value)
        description = soup.select_one("#tp-product-description-tab, #product_full_description, .o_product_description")
        if description:
            for key, value in extract_key_values(description.get_text("\n")).items():
                specs.setdefault(key, value)

        store_sku = sku
        sku = self.choose_sku(name, store_sku, specs, item.category)
        offer = Offer(seller_name=self.seller_name, price=price, currency=currency or self.currency,
                      in_stock=in_stock, url=item.url, sku=store_sku, source="product")
        return ScrapedProduct(site=self.name, url=item.url, category=item.category, name=name, sku=sku,
                              image_url=self.absolute(image), specs=specs, offers=[offer],
                              extra={"template_id": template_id_from_url(item.url), "condition": "new",
                                     "store_sku": store_sku})
