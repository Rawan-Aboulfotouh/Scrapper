"""
Elsindbad Store (elsindbadstore.com): Magento 2, server-rendered HTML.

Crawl4AI strategy: plain page load, no scrolling. Category pages are complete in the first HTML
response, so waiting for the product grid is enough. Pagination follows the "next" link (``?p=N``,
allowed by robots.txt). Layered-navigation filters are never used because robots.txt disallows them.

Product pages carry schema.org JSON-LD, which is preferred; the CSS fields are the fallback.
Specifications come from the "More information" table and the "key<TAB>value" description lines.
Single-seller store: the seller is always the store itself. It has no mobiles or tablets category.
"""
from __future__ import annotations

from typing import Dict, Iterator, Optional

from ..crawl_engine import ScrapeConfig, ScrapeResult
from ..models import DiscoveredItem, Offer, ScrapedProduct
from ..utils import clean_text, detect_currency, extract_key_values, parse_price, strip_query_params
from .base import SiteAdapter

LISTING = ScrapeConfig(
    wait_selector=".products.wrapper, .message.empty",
    schema={
        "name": "elsindbad_listing",
        "baseSelector": ".products.wrapper li.product-item",
        "fields": [
            {"name": "link", "selector": "a.product-item-link, a.product-item-photo", "type": "attribute",
             "attribute": "href"},
            {"name": "name", "selector": "a.product-item-link", "type": "text"},
            {"name": "sku", "selector": "form[data-product-sku]", "type": "attribute",
             "attribute": "data-product-sku"},
            {"name": "price", "selector": '[data-price-type="finalPrice"]', "type": "attribute",
             "attribute": "data-price-amount"},
            {"name": "unavailable", "selector": ".stock.unavailable, .out-of-stock", "type": "text"},
            {"name": "add_to_cart", "selector": "button.tocart, .action.tocart", "type": "attribute",
             "attribute": "class"},
        ],
    },
)

PRODUCT = ScrapeConfig(
    wait_selector=".product-info-main, h1",
    schema={
        "name": "elsindbad_product",
        "baseSelector": "html",
        "fields": [
            {"name": "name", "selector": "h1.page-title, h1", "type": "text"},
            {"name": "sku", "selector": '.product.attribute.sku [itemprop="sku"], .product.attribute.sku .value',
             "type": "text"},
            {"name": "price", "selector": '.product-info-main [data-price-type="finalPrice"]', "type": "attribute",
             "attribute": "data-price-amount"},
            {"name": "price_text", "selector": ".product-info-main .price", "type": "text"},
            {"name": "currency", "selector": '.product-info-main meta[itemprop="priceCurrency"]', "type": "attribute",
             "attribute": "content"},
            {"name": "stock_out", "selector": ".product-info-main .stock.unavailable", "type": "text"},
            {"name": "stock_in", "selector": ".product-info-main .stock.available", "type": "text"},
            {"name": "image", "selector": 'meta[property="og:image"]', "type": "attribute", "attribute": "content"},
            {"name": "specs", "selector": "#product-attribute-specs-table tr", "type": "list", "fields": [
                {"name": "label", "selector": "th", "type": "text"},
                {"name": "value", "selector": "td", "type": "text"},
            ]},
        ],
    },
)


class ElsindbadAdapter(SiteAdapter):
    name = "elsindbad"
    default_sku_source = "model_from_name"     # store SKU is an internal number

    def canonical_url(self, url: str) -> str:
        return strip_query_params(url, keep_only=())

    # ------------------------------------------------------------------ listing
    @staticmethod
    def card_from_item(raw: Dict) -> Dict:
        if raw.get("unavailable") is not None:
            in_stock: Optional[bool] = False
        elif raw.get("add_to_cart") is not None:
            in_stock = True
        else:
            in_stock = None
        return {"url": raw.get("link") or "", "name": clean_text(raw.get("name")),
                "sku": clean_text(raw.get("sku")), "price": raw.get("price"), "in_stock": in_stock}

    @staticmethod
    def next_page_url(result: ScrapeResult) -> Optional[str]:
        link = result.soup().select_one(".pages-item-next a[href], a.action.next[href]")
        return link["href"] if link else None

    def discover(self, category_url: str, category: str) -> Iterator[DiscoveredItem]:
        category_url = self.validate(category_url)
        seen = set()
        url = category_url
        for page in range(1, self.max_pages + 1):
            result = self.load(url, LISTING)
            new_count = 0
            for raw in result.items:
                card = self.card_from_item(raw)
                if not card["url"]:
                    continue
                product_url = self.canonical_url(self.absolute(card["url"]))
                if product_url in seen:
                    continue
                seen.add(product_url)
                new_count += 1
                yield DiscoveredItem(site=self.name, url=product_url, category=category,
                                     key=f"url:{product_url}", hints=card)
            self.log.info("%s page %d: %d new products", category_url, page, new_count)
            next_url = self.next_page_url(result)
            # Magento serves the last page again for out-of-range ?p=, so "no new items" also stops us
            if not next_url or new_count == 0:
                break
            url = self.validate(self.absolute(next_url))

    # ------------------------------------------------------------------ product
    def scrape(self, item: DiscoveredItem) -> Optional[ScrapedProduct]:
        return self.parse_product(self.load(self.validate(item.url), PRODUCT), item)

    def parse_product(self, result: ScrapeResult, item: DiscoveredItem) -> Optional[ScrapedProduct]:
        soup, css = result.soup(), result.item
        ld = (self.json_ld_products(soup) or [{}])[0]
        hints: Dict = item.hints or {}

        name = clean_text(ld.get("name")) or clean_text(css.get("name"))
        if not name:
            self.log.warning("no product found on %s", item.url)
            return None

        sku = clean_text(ld.get("sku")) or clean_text(css.get("sku")) or hints.get("sku", "")

        # ---- price / currency / availability (JSON-LD first, CSS fallback)
        offer_ld = self.json_ld_offer(ld)
        price = parse_price(offer_ld.get("price") or offer_ld.get("lowPrice")) or parse_price(css.get("price"))
        currency = clean_text(offer_ld.get("priceCurrency")).upper() or clean_text(css.get("currency")).upper() \
            or detect_currency(css.get("price_text") or "", self.currency)
        in_stock = self.json_ld_in_stock(offer_ld)
        if in_stock is None:
            if css.get("stock_out") is not None:
                in_stock = False
            elif css.get("stock_in") is not None:
                in_stock = True
            else:
                in_stock = hints.get("in_stock")

        image = self.json_ld_image(ld) or clean_text(css.get("image"))

        # ---- specifications (table first, then description lines)
        specs = self.specs_from_rows(css.get("specs"))
        for block in soup.select(".product.attribute.description .value, .product.attribute.overview .value"):
            for key, value in extract_key_values(block.get_text("\n")).items():
                specs.setdefault(key, value)

        store_sku = sku
        sku = self.choose_sku(name, store_sku, specs, item.category)
        offer = Offer(seller_name=self.seller_name, price=price, currency=currency or self.currency,
                      in_stock=in_stock, url=item.url, sku=store_sku, source="product")
        return ScrapedProduct(site=self.name, url=item.url, category=item.category, name=name, sku=sku,
                              image_url=self.absolute(image) if image else "", specs=specs, offers=[offer],
                              extra={"brand": self.json_ld_brand(ld), "condition": "new", "store_sku": store_sku})
