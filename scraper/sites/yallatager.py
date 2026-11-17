"""
Yalla Tager (yallatager.com): multi-seller marketplace, single-page app.

Crawl4AI strategy
-----------------
Listing pages (category or search) render product cards with JavaScript and load more while the user
scrolls. ``load_all_cards`` runs before the HTML is read: it nudges the page up and down, brings the
``.infiniteScrollWrap`` loader into view and waits for the card count to grow, until the count reaches
the site's own total or the page stops growing. The CSS schema then reads every card.

The same product sold by several sellers appears as separate cards with the same title. Cards are
grouped by title into one product carrying every seller's offer, and only the cheapest available
card's page is opened. Each card's URL ends with "-<seller id>.html".

Product pages: "Show more" is clicked so the full details are in the DOM. The page's own JSON
(``products/getItem``, ``vendor/getVendorById``, ``vendor/getproductsothersellers``) is recorded
passively and supplies structured specifications and seller names. When it is absent, the SKU and the
"Technical Specification" section are read from the page text, and sellers are labelled by id.

SKU: marketplace SKUs end with "-<seller_id>"; that suffix is removed so one product has one SKU.
The "Show more" and "Technical Specification" labels are English, so ``store_code`` must stay ``en``.

Access: this site's robots.txt is ``Disallow: /`` for every crawler and its Terms (sections 8.9.9
and 8.9.10) forbid automated collection without written consent. The pipeline therefore refuses to
run this adapter until config.yaml records that permission (see ``permission_reference``).
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from pathlib import Path
from typing import Dict, Iterator, List, Optional
from urllib.parse import quote, unquote, urlsplit

from bs4 import BeautifulSoup

from ..crawl_engine import CapturedJson, ScrapeConfig, ScrapeResult
from ..models import DiscoveredItem, Offer, ScrapedProduct
from ..utils import clean_text, extract_key_values, normalize_arabic_key, parse_price, strip_query_params
from .base import SiteAdapter

ITEM_API = "products/getItem"
VENDOR_API = "vendor/getVendorById"
OTHERS_API = "vendor/getproductsothersellers"

CARD = "div.productItem_wrap"
SPECS_MARKER = "Technical Specification"
SPECS_MAX_CHARS = 3000

log = logging.getLogger("scraper.site.yallatager")

_TABLET_NAME = re.compile(r"(?:^|\s)(?:tablet|tab|ipad|matepad|pad|تابلت|ايباد)(?=\s|$)")
_OUT_OF_STOCK = re.compile(r"out\s*of\s*stock|sold\s*out|unavailable|notify|غير\s*متوفر|نفذ|نفدت", re.I)
_SELLER_SUFFIX = re.compile(r"-(\d+)\.html$")
_SKU_LABEL = re.compile(r"SKU\s*[:\-]?\s*([A-Za-z0-9][A-Za-z0-9\-_.]*)", re.I)
_SKU_IN_HTML = re.compile(r"\b\d{10,14}-[A-Za-z]+-\d+\b")
_SPECS_END = re.compile(r"^(?:reviews?|ratings?|related products?|similar products?|you may also like|"
                        r"customers? also|other sellers)\b", re.I)
_NOT_A_SPEC = ("show more", "show less")


# --------------------------------------------------------------------------- #
# Browser steps, run through Crawl4AI's before_retrieve_html hook
# --------------------------------------------------------------------------- #
async def site_total(page) -> Optional[int]:
    """The product count the page reports in its toolbar (the second <strong>)."""
    texts = await page.locator(".products-toolbar-counts strong").all_inner_texts()
    if len(texts) >= 2:
        digits = re.sub(r"\D", "", texts[1])
        if digits:
            return int(digits)
    return None


def make_scroll_driver(max_cards: int, min_delay: float, max_delay: float, stall_limit: int = 2,
                       growth_timeout_ms: int = 10_000):
    """Build the driver that scrolls until every card is loaded or ``max_cards`` is reached."""

    async def load_all_cards(page, _captured: List[CapturedJson]) -> None:
        total = await site_total(page)
        loaded = await page.locator(CARD).count()
        log.info("listing start: %d cards, site total: %s", loaded, total)
        await page.mouse.move(600, 400)
        stalls = 0
        while stalls < stall_limit and loaded < max_cards and not (total and loaded >= total):
            await asyncio.sleep(random.uniform(min_delay, max_delay))
            await page.mouse.wheel(0, -800)              # the loader only fires on a fresh downward scroll
            await page.wait_for_timeout(500)
            await page.mouse.wheel(0, 5000)
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            loader = page.locator(".infiniteScrollWrap")
            if await loader.count():
                try:
                    await loader.first.scroll_into_view_if_needed(timeout=3000)
                except Exception:                        # the loader can vanish once everything is shown
                    pass
            try:
                await page.wait_for_function("n => document.querySelectorAll('div.productItem_wrap').length > n",
                                             arg=loaded, timeout=growth_timeout_ms)
                stalls = 0
            except Exception:                            # Playwright timeout: the page did not grow
                stalls += 1
            loaded = await page.locator(CARD).count()
        log.info("listing finished: %d cards", loaded)

    return load_all_cards


async def expand_details(page, _captured: List[CapturedJson]) -> None:
    """Give the app time to render, then open every "Show more" section."""
    await page.wait_for_timeout(1500)
    pattern = re.compile(r"show more", re.I)
    locators = [page.get_by_role("button", name=pattern), page.get_by_role("link", name=pattern),
                page.get_by_text(re.compile(r"^\s*show more\s*$", re.I))]
    for locator in locators:
        for index in range(await locator.count()):
            try:
                element = locator.nth(index)
                if await element.is_visible():
                    await element.click(timeout=3000)
                    await page.wait_for_timeout(300)
            except Exception:                            # an element can disappear after the first click
                pass
    await page.wait_for_timeout(800)


PRODUCT = ScrapeConfig(
    before_html_driver=expand_details,
    capture_json=(ITEM_API, VENDOR_API, OTHERS_API),
    capture_optional=(ITEM_API, VENDOR_API, OTHERS_API),
    settle_seconds=0.2,
)


# --------------------------------------------------------------------------- #
# Pure parsing helpers (unit-tested without a browser)
# --------------------------------------------------------------------------- #
def flag(value) -> Optional[bool]:
    if value in (None, ""):
        return None
    return str(value).strip().lower() in ("1", "true", "yes")


def base_sku(sku: str, seller_id: str) -> str:
    sku, seller_id = clean_text(sku), clean_text(seller_id)
    if seller_id and sku.endswith("-" + seller_id):
        return sku[: -(len(seller_id) + 1)]
    return sku


def seller_id_from_url(url: str) -> str:
    match = _SELLER_SUFFIX.search(unquote(urlsplit(url or "").path))
    return match.group(1) if match else ""


def button_in_stock(button: Optional[str]) -> Optional[bool]:
    """"Buy Now" means available; a sold-out label means not; anything else is unknown."""
    text = clean_text(button)
    if text.lower() == "buy now":
        return True
    return False if _OUT_OF_STOCK.search(text) else None


def category_for(name: str, requested: str) -> Optional[str]:
    """
    Search pages mix phones and tablets, so a phone or tablet is filed by its name. The same name
    always lands in the same category, whichever page found it. Non-tablets found while searching for
    tablets are dropped; phones are collected by the Mobiles targets.
    """
    if requested == "TVs":
        return requested
    if _TABLET_NAME.search(normalize_arabic_key(name)):
        return "Tablets"
    return None if requested == "Tablets" else "Mobiles"


def page_text(html: str) -> str:
    """The page's visible text, one text node per line."""
    soup = BeautifulSoup(html or "", "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return soup.get_text("\n")


def sku_from_page(text: str, html: str) -> str:
    """The value after a "SKU" label, else a marketplace SKU anywhere in the HTML."""
    for match in _SKU_LABEL.finditer(text or ""):
        if any(ch.isdigit() for ch in match.group(1)):
            return match.group(1)
    match = _SKU_IN_HTML.search(html or "")
    return match.group(0) if match else ""


def specs_from_text(text: str) -> Dict[str, str]:
    """
    Parse the "Technical Specification" section: "Label: Value" lines, or a label and value on
    consecutive lines. When no structure is recognised the raw section is kept, so nothing is lost.
    """
    start = (text or "").find(SPECS_MARKER)
    if start < 0:
        return {}
    section = text[start + len(SPECS_MARKER):start + SPECS_MAX_CHARS]
    specs: Dict[str, str] = {}
    label: Optional[str] = None
    lines = []
    for raw in section.splitlines():
        line = clean_text(raw)
        if _SPECS_END.match(line):
            break
        if line and line.lower() not in _NOT_A_SPEC:
            lines.append(line)
    for line in lines:
        if line.startswith(":") and label:               # "<b>Brand</b>: Apple" gives two text nodes
            specs.setdefault(label, clean_text(line[1:]))
            label = None
            continue
        key, separator, value = line.partition(":")
        if separator and clean_text(value) and len(key) <= 45 and label is None:
            specs.setdefault(clean_text(key), clean_text(value))
        elif label is None:
            label = line
        else:
            specs.setdefault(label, line)
            label = None
    if len(specs) < 2 and lines:
        return {SPECS_MARKER: " ".join(lines)}
    return specs


def item_in_stock(item: Dict) -> bool:
    stock = item.get("stock_data") or {}
    checks = [flag(item.get("status")), flag(item.get("is_in_stock")), flag(stock.get("is_in_stock")),
              flag(item.get("is_salable"))]
    if any(c is False for c in checks):
        return False
    if flag(item.get("seller_disabled")) or flag(item.get("seller_deleted")):
        return False
    return True


def item_price(item: Dict):
    for key in ("final_price", "special_price", "price"):
        price = parse_price(item.get(key))
        if price is not None:
            return price
    return None


def product_url_for(item: Dict, site_base: str, store_code: str) -> str:
    path = item.get("url_path_category") or item.get("url_path") or ""
    if not path and item.get("url_key"):
        path = item["url_key"] + ".html"
    path = unquote(str(path)).lstrip("/")
    return f"{site_base}/{store_code}/{path}" if path else ""


def offer_from_item(item: Dict, site_base: str, store_code: str, currency: str,
                    seller_names: Dict[str, str], source: str) -> Offer:
    seller_id = clean_text(item.get("seller_id"))
    return Offer(
        seller_name=seller_names.get(seller_id, ""),
        seller_id=seller_id,
        price=item_price(item),
        currency=currency,
        in_stock=item_in_stock(item),
        url=product_url_for(item, site_base, store_code),
        sku=clean_text(item.get("sku")),
        source=source,
    )


def description_specs(description_html: str) -> Dict[str, str]:
    """'<li><strong>Brand</strong>: Apple</li>' ... -> {'Brand': 'Apple', ...}"""
    if not description_html:
        return {}
    soup = BeautifulSoup(description_html, "lxml")
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for row in soup.find_all("tr"):
        cells = row.find_all(["td", "th"])
        if len(cells) >= 2:
            cells[0].append("\t")
    for block in soup.find_all(["li", "p", "tr", "div", "h1", "h2", "h3", "h4"]):
        block.append("\n")
    return extract_key_values(soup.get_text(""))


def other_seller_offers(payload, site_base: str, store_code: str, currency: str,
                        seller_names: Dict[str, str]) -> List[Offer]:
    """Defensive parser for vendor/getproductsothersellers (``{"success":true,"data":[...]}``)."""
    rows = payload.get("data") if isinstance(payload, dict) else payload
    offers: List[Offer] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        product = row.get("product") if isinstance(row.get("product"), dict) else row
        seller_id = clean_text(row.get("seller_id") or row.get("vendor_id") or product.get("seller_id"))
        vendor = row.get("vendor") if isinstance(row.get("vendor"), dict) else {}
        name = clean_text(row.get("vendor_name") or row.get("seller_name") or row.get("shop_title")
                          or vendor.get("vendor_name"))
        if seller_id and name:
            seller_names.setdefault(seller_id, name)
        offer = offer_from_item(product, site_base, store_code, currency, seller_names, "other_sellers")
        offer.seller_id = seller_id or offer.seller_id
        offer.seller_name = name or seller_names.get(offer.seller_id, "")
        offers.append(offer)
    return offers


# --------------------------------------------------------------------------- #
# Adapter
# --------------------------------------------------------------------------- #
class YallaTagerAdapter(SiteAdapter):
    name = "yallatager"

    def __init__(self, site_cfg: dict, session, state_dir: Optional[Path] = None):
        super().__init__(site_cfg, session)
        self.store_code = site_cfg.get("store_code", "en")
        # empty = detected from the host the site's own API answers from
        self.image_base = site_cfg.get("image_base_url", "").rstrip("/")
        self._names_path = Path(state_dir or "state") / "yallatager_sellers.json"
        self.seller_names: Dict[str, str] = self._load_names()
        settings = session.settings
        self.listing = ScrapeConfig(
            wait_selector=CARD,
            wait_timeout_ms=45_000,
            before_html_driver=make_scroll_driver(int(site_cfg.get("max_products_per_category", 1500)),
                                                  settings.min_delay_seconds, settings.max_delay_seconds),
            schema={
                "name": "yallatager_listing",
                "baseSelector": CARD,
                "fields": [
                    {"name": "title", "selector": "h6.product_title a", "type": "text"},
                    {"name": "link", "selector": "h6.product_title a", "type": "attribute", "attribute": "href"},
                    {"name": "price", "selector": "div.product_price span.price", "type": "text"},
                    {"name": "button", "selector": "div.buyNowBtnWrap button", "type": "text"},
                    {"name": "image", "selector": "img", "type": "attribute", "attribute": "src"},
                ],
            },
        )

    # ---------------------------------------------------------------- seller cache
    def _load_names(self) -> Dict[str, str]:
        try:
            return json.loads(self._names_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_names(self) -> None:
        self._names_path.parent.mkdir(parents=True, exist_ok=True)
        self._names_path.write_text(json.dumps(self.seller_names, ensure_ascii=False, indent=1), encoding="utf-8")

    def canonical_url(self, url: str) -> str:
        parts = urlsplit(strip_query_params(url, keep_only=()))
        # keep Arabic characters readable but consistently encoded
        return f"{parts.scheme}://{parts.netloc}{quote(unquote(parts.path), safe='/()-_.,~')}"

    # ---------------------------------------------------------------- discovery
    def discover(self, category_url: str, category: str) -> Iterator[DiscoveredItem]:
        url = self.validate(category_url)
        result = self.load(url, self.listing)
        if not result.items:
            self.log.error("%s: no product cards found", url)
            return
        self.log.info("%s: %d cards", url, len(result.items))
        yield from self.group_cards(result.items, category)

    def group_cards(self, cards: List[Dict], requested: str) -> Iterator[DiscoveredItem]:
        """One DiscoveredItem per product title, carrying one offer per seller card."""
        groups: Dict[str, List[Dict]] = {}
        for card in cards:
            title = clean_text(card.get("title"))
            if title and card.get("link"):
                groups.setdefault(normalize_arabic_key(title), []).append(card)
        for key, members in groups.items():
            title = clean_text(members[0].get("title"))
            category = category_for(title, requested)
            if category is None:
                continue
            offers: Dict[str, Offer] = {}
            for card in members:
                product_url = self.canonical_url(self.validate(self.absolute(card["link"])))
                seller_id = seller_id_from_url(product_url)
                offers.setdefault(seller_id or product_url, Offer(
                    seller_name=self.seller_names.get(seller_id, ""), seller_id=seller_id,
                    price=parse_price(card.get("price")), currency=self.currency,
                    in_stock=button_in_stock(card.get("button")), url=product_url, source="listing"))
            available = [o for o in offers.values() if o.in_stock is not False and o.price is not None]
            representative = min(available, key=lambda o: o.price) if available else next(iter(offers.values()))
            image = clean_text(members[0].get("image"))
            yield DiscoveredItem(
                site=self.name, url=representative.url, category=category, key=f"title:{key}",
                hints={"name": title, "image": self.absolute(image) if image else "",
                       "offers": [o.to_dict() for o in offers.values()]},
            )

    # ---------------------------------------------------------------- product
    def _open_product(self, url: str) -> ScrapeResult:
        return self.load(self.validate(url), PRODUCT)

    def _learn_image_base(self, result: ScrapeResult) -> None:
        if self.image_base:
            return
        for captured in result.captured:
            parts = urlsplit(captured.url)
            if parts.scheme and parts.netloc and "/restfulapi/" in parts.path:
                self.image_base = f"{parts.scheme}://{parts.netloc}/media/catalog/product"
                self.log.info("product images served from %s", self.image_base)
                return

    def _remember_vendor(self, result: ScrapeResult) -> None:
        for captured in result.find(VENDOR_API):
            data = (captured.body or {}).get("vendor_data") if isinstance(captured.body, dict) else None
            if isinstance(data, dict) and data.get("entity_id") and data.get("vendor_name"):
                self.seller_names[str(data["entity_id"])] = clean_text(data["vendor_name"])

    @staticmethod
    def _item_from(result: ScrapeResult) -> Dict:
        captured = result.first(ITEM_API)
        body = captured.body if captured else None
        if isinstance(body, dict) and isinstance(body.get("data"), dict) and "entity_id" not in body:
            body = body["data"]
        return body if isinstance(body, dict) and body.get("entity_id") else {}

    def scrape(self, item: DiscoveredItem) -> Optional[ScrapedProduct]:
        result = self._open_product(item.url)
        self._learn_image_base(result)
        self._remember_vendor(result)
        detail = self._item_from(result)
        hints = item.hints or {}
        text = page_text(result.html)

        name = clean_text(detail.get("name")) or hints.get("name", "")
        if not name:
            title = result.soup().select_one("h1")
            name = clean_text(title.get_text()) if title else ""
        if not name:
            self.log.warning("no product data loaded for %s", item.url)
            return None

        # ---- offers: listing cards, refreshed by the product page, plus other sellers
        offers: Dict[str, Offer] = {}
        for raw in hints.get("offers", []):
            offer = Offer.from_dict(raw)
            offers[offer.seller_id or offer.url] = offer
        if detail:
            fresh = offer_from_item(detail, self.base_url, self.store_code, self.currency, self.seller_names, "product")
            offers[fresh.seller_id or fresh.url] = fresh
        for captured in result.find(OTHERS_API):
            for offer in other_seller_offers(captured.body, self.base_url, self.store_code,
                                             self.currency, self.seller_names):
                key = offer.seller_id or offer.url
                if key not in offers or offers[key].source == "listing":
                    offers[key] = offer

        # ---- resolve the cheapest seller's name if still unknown (one extra page at most)
        valid = [o for o in offers.values() if o.in_stock is not False and o.price is not None]
        cheapest = min(valid, key=lambda o: o.price) if valid else None
        if cheapest and cheapest.seller_id and cheapest.seller_id not in self.seller_names and cheapest.url \
                and self.canonical_url(cheapest.url) != self.canonical_url(item.url):
            try:
                self._remember_vendor(self._open_product(cheapest.url))
            except Exception as exc:                       # the name is nice-to-have; keep going
                self.log.warning("could not resolve seller %s: %s", cheapest.seller_id, exc)
        for offer in offers.values():
            if not offer.seller_name:
                offer.seller_name = self.seller_names.get(offer.seller_id, f"Seller #{offer.seller_id}" if offer.seller_id else "")
        self._save_names()

        # ---- specifications: structured attributes, description lines, then the page text
        specs: Dict[str, str] = {}
        for attribute in detail.get("attributes") or []:
            if isinstance(attribute, dict) and attribute.get("label"):
                specs.setdefault(clean_text(attribute["label"]), clean_text(attribute.get("value")))
        for key, value in description_specs(detail.get("description") or "").items():
            specs.setdefault(key, value)
        for key, value in specs_from_text(text).items():
            specs.setdefault(key, value)

        if detail:
            sku = base_sku(detail.get("sku", ""), detail.get("seller_id", ""))
        else:
            sku = base_sku(sku_from_page(text, result.html), seller_id_from_url(item.url))

        image = detail.get("image") or detail.get("small_image") or ""
        if image == "no_selection":
            image = ""
        image_url = (f"{self.image_base}{image}" if image and self.image_base else image) or hints.get("image", "")
        condition = ("used" if clean_text(detail.get("is_used")) == "1" else "new") if detail else ""
        return ScrapedProduct(
            site=self.name, url=item.url, category=item.category, name=name, sku=sku,
            image_url=image_url, specs=specs, offers=list(offers.values()),
            extra={"brand": clean_text(detail.get("manufacturer_value")), "store_sku": sku,
                   "template_id": clean_text(detail.get("template_id")), "condition": condition},
        )
