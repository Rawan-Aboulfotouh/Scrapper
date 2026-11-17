"""Every site's selectors and parser, run against pages saved from the real websites."""
import asyncio
from decimal import Decimal

from conftest import FakeSession, fixture_json, use_yalla_test_site

from scraper.crawl_engine import CapturedJson
from scraper.models import DiscoveredItem
from scraper.sites.awadserag import AwadSeragAdapter, ref_from_text
from scraper.sites.carrefour import CarrefourAdapter, category_slug, seller_from_page
from scraper.sites.elsindbad import ElsindbadAdapter
from scraper.sites.noon import NoonAdapter, product_id_from_url
from scraper.sites.vodafone import (LIST_API, VodafoneAdapter, category_path, make_scroll_driver,
                                    specs_from_payload)
from scraper.sites.yallatager import (CARD, YallaTagerAdapter, base_sku, button_in_stock, category_for,
                                      description_specs, make_scroll_driver as make_card_driver, seller_id_from_url,
                                      sku_from_page, specs_from_text)

ELS_CAT = "https://elsindbadstore.com/ar/tvs-and-audio-systems/smart-tvs.html"
ELS_PRODUCT = "https://elsindbadstore.com/ar/lg-65-inch-oled-evo-ai-cs6-4k-144hz-smart-tv-oled65cs6va.html"
AWAD_CAT = "https://www.awadserag.com/shop/category/13"
AWAD_PRODUCT = "https://www.awadserag.com/shop/500020655-43-43mf423c-38794"
NOON_CAT = "https://www.noon.com/egypt-en/electronics-and-mobiles/television-and-video/televisions/"
NOON_PRODUCT = ("https://www.noon.com/egypt-en/43-full-hd-smart-led-tv-hdr-purcolor-ots-lite-sound-samsung-knox-"
                "security-tizen-os-2025-ua43f6000fuxeg-ua43f6000fuxeg-black/N70215105V/p/")
CARREFOUR_CAT = "https://www.carrefouregypt.com/mafegy/en/c/promo-tvs"
CARREFOUR_PRODUCT = "https://www.carrefouregypt.com/mafegy/en/uhd-tv/samsung-ledtvua65m70hauxeg/p/676576"
CARREFOUR_SITEMAP = "https://www.carrefouregypt.com/sitemaps/products2_en.xml"
VODAFONE_CAT = "https://eshop.vodafone.com.eg/en/list?categoryUrls=/tv"
VODAFONE_PRODUCT = "https://eshop.vodafone.com.eg/en/prod/hisense-smart-tv-32-inch-a4-hd-vidaa-32a4q"


# ------------------------------------------------------------------ Elsindbad
def test_elsindbad_discover_with_pagination(base_cfg):
    session = FakeSession({ELS_CAT: "elsindbad_listing_p1.html", ELS_CAT + "?p=2": "elsindbad_listing_p2.html"})
    adapter = ElsindbadAdapter(base_cfg["sites"]["elsindbad"], session)
    items = list(adapter.discover(ELS_CAT, "TVs"))
    assert [i.hints["sku"] for i in items] == ["30408", "29871", "", ""]   # last one is the wall mount (filtered later)
    assert items[0].url == ELS_PRODUCT and items[0].hints["price"] == "87999"
    assert items[2].hints["in_stock"] is False                  # "stock unavailable" on page 2
    assert session.requested == [ELS_CAT, ELS_CAT + "?p=2"]     # stops when there is no "next"


def test_elsindbad_product(base_cfg):
    adapter = ElsindbadAdapter(base_cfg["sites"]["elsindbad"], FakeSession({ELS_PRODUCT: "elsindbad_product.html"}))
    product = adapter.scrape(DiscoveredItem("elsindbad", ELS_PRODUCT, "TVs", "k"))
    assert product.sku == "OLED65CS6VA" and product.extra["store_sku"] == "30408"
    assert product.offers[0].price == Decimal("87999.00") and product.offers[0].currency == "EGP"
    assert product.offers[0].in_stock is True and product.offers[0].seller_name == "السندباد ستور"
    assert product.image_url.endswith("screenshot_2026-10-27_131742_1_.png")
    assert product.specs["مقاس الشاشه"] == "٦٥ بوصة"                 # table first
    assert product.specs["دقة الشاشة"] == "UHD 4K"                     # from description lines
    assert product.specs["نظام التشغيل"] == "WebOS"


# ------------------------------------------------------------------ Awad Serag
def test_ref_from_text():
    assert ref_from_text("[500011416] مبرد هواء") == "500011416"
    assert ref_from_text("arr[0] [abc] nothing") == ""


def test_awadserag_discover(base_cfg):
    session = FakeSession({AWAD_CAT: "awadserag_listing.html", AWAD_CAT + "/page/2": "awadserag_listing_p2.html"})
    adapter = AwadSeragAdapter(base_cfg["sites"]["awadserag"], session)
    items = list(adapter.discover(AWAD_CAT + "?order=list_price+asc", "TVs"))
    assert [i.url for i in items] == [AWAD_PRODUCT, "https://www.awadserag.com/shop/104555-55-55qned80-31200"]
    assert items[0].hints["sku"] == "500020655" and items[0].hints["in_stock"] is True
    assert items[1].hints["in_stock"] is False                          # OUT OF STOCK badge
    assert session.requested == [AWAD_CAT, AWAD_CAT + "/page/2"]


def test_awadserag_product_uses_hints_and_ignores_similar_products(base_cfg):
    adapter = AwadSeragAdapter(base_cfg["sites"]["awadserag"], FakeSession({AWAD_PRODUCT: "awadserag_product.html"}))
    product = adapter.scrape(DiscoveredItem("awadserag", AWAD_PRODUCT, "TVs", "k"))
    assert product.sku == "43MF423C"                # model code from the name
    assert product.extra["store_sku"] == "AS-TPL-38794"   # never "[999999]" from similar products
    with_hints = adapter.scrape(DiscoveredItem("awadserag", AWAD_PRODUCT, "TVs", "k", hints={"sku": "500020655"}))
    assert with_hints.sku == "43MF423C" and with_hints.extra["store_sku"] == "500020655"
    adapter.cfg = dict(adapter.cfg, sku_source="store")    # config can switch back to the store's SKU
    assert adapter.scrape(DiscoveredItem("awadserag", AWAD_PRODUCT, "TVs", "k", hints={"sku": "500020655"})).sku == "500020655"
    offer = with_hints.offers[0]
    assert offer.price == Decimal("11890.0") and offer.currency == "EGP" and offer.seller_name == "عوض سراج"
    assert with_hints.specs["مقاس الشاشة"] == "43 بوصه" and with_hints.specs["الماركات"] == "Fresh"
    assert with_hints.specs["دقة الشاشة"] == "FHD"
    assert with_hints.image_url.startswith("https://www.awadserag.com/web/image/product.template/38794")


# ------------------------------------------------------------------ Noon
def _noon_routes():
    base = NOON_CAT + "?limit=50&page="
    return {base + "1": "noon_listing_p1.html", base + "2": "noon_listing_p2.html", base + "3": "noon_listing_p3.html"}


def test_noon_discover_stops_when_a_page_adds_nothing(base_cfg):
    session = FakeSession(_noon_routes())
    items = list(NoonAdapter(base_cfg["sites"]["noon"], session).discover(NOON_CAT, "TVs"))
    assert len(items) == 4 and len(session.requested) == 3         # page 3 repeats page 2: paging stops
    assert items[0].key == "id:N70215105V" and "?" not in items[0].url
    assert items[0].hints["price"] == "14,659" and items[0].hints["currency"] == "EGP"
    assert items[0].hints["name"].startswith("Samsung 43″ Full HD")
    assert items[0].hints["image"].startswith("https://f.nooncdn.com/p/pzsku/")


def test_noon_product(base_cfg):
    adapter = NoonAdapter(base_cfg["sites"]["noon"], FakeSession({NOON_PRODUCT: "noon_product.html"}))
    product = adapter.scrape(DiscoveredItem("noon", NOON_PRODUCT, "TVs", "id:N70215105V"))
    assert product.name.startswith("Samsung 43″ Full HD")          # the page title omits the brand
    assert product.sku == "UA43F6000FUXEG" and product.extra["store_sku"] == "N70215105V"
    assert product.extra["brand"] == "Samsung" and product.extra["condition"] == "new"
    offer = product.offers[0]
    assert (offer.price, offer.currency, offer.in_stock) == (Decimal("14659"), "EGP", True)
    assert offer.seller_name == "E-TM Mega Home"
    assert product.specs["Screen Size"] == "43 in" and product.specs["Operating System"] == "Tizen"
    assert product.specs["Number of HDMI Ports"] == "2"
    assert product.image_url.startswith("https://f.nooncdn.com/p/pzsku/")


def test_noon_product_id():
    assert product_id_from_url("/egypt-en/x-black/N70215105V/p/?o=ce28") == "N70215105V"
    assert product_id_from_url("/egypt-en/televisions/") == ""


def test_phones_keep_the_store_sku_so_variants_stay_separate(base_cfg):
    adapter = NoonAdapter(base_cfg["sites"]["noon"], FakeSession())
    specs = {"Model Number": "SM-S938B"}
    assert adapter.choose_sku("Samsung Galaxy S25 Ultra 256GB Black", "N123", specs, "Mobiles") == "N123"
    assert adapter.choose_sku("Samsung Galaxy Tab S10 256GB", "N456", specs, "Tablets") == "N456"
    assert adapter.choose_sku("Samsung 55 inch TV", "N789", {"Model Number": "UA55U8000"}, "TVs") == "UA55U8000"
    assert adapter.choose_sku("Some 55 inch TV", "N789", {"Model Number": "Smart Plus"}, "TVs") == "N789"


# ------------------------------------------------------------------ Carrefour
def test_carrefour_listing_reads_first_page_only(base_cfg):
    session = FakeSession({CARREFOUR_CAT: "carrefour_listing.html"})
    items = list(CarrefourAdapter(base_cfg["sites"]["carrefour"], session).discover(CARREFOUR_CAT, "TVs"))
    assert items and session.requested == [CARREFOUR_CAT]          # robots.txt disallows ?currentPage=
    first = items[0]
    assert first.key.startswith("id:") and "?" not in first.url and "/p/" in first.url
    assert first.hints["name"] and first.hints["image"].startswith("https://cdn.mafrservices.com/")
    assert first.hints["currency"] == "EGP" and first.hints["price"].replace(",", "").replace(".", "").isdigit()


def test_carrefour_sitemap_is_filtered_by_category_slug(base_cfg):
    session = FakeSession({CARREFOUR_SITEMAP: "carrefour_sitemap.xml"})
    adapter = CarrefourAdapter(base_cfg["sites"]["carrefour"], session)
    assert [i.key for i in adapter.discover(CARREFOUR_SITEMAP, "TVs")] == ["id:676576"]    # the wall stand is left out
    assert [i.key for i in adapter.discover(CARREFOUR_SITEMAP, "Mobiles")] == ["id:613732"]
    assert [i.key for i in adapter.discover(CARREFOUR_SITEMAP, "Tablets")] == ["id:655640"]
    assert session.requested == [CARREFOUR_SITEMAP]                # the document is downloaded once


def test_carrefour_slug_and_seller_helpers():
    assert category_slug("https://www.carrefouregypt.com/mafegy/en/uhd-tv/name/p/1") == "uhd-tv"
    html = r'media/676576_main.jpg\",\"shopId\":\"0000\",\"shopName\":\"Radio Talaat\",\"discount\":\"1\"'
    assert seller_from_page(html, "676576") == "Radio Talaat"
    assert seller_from_page(html, "999") == ""


def test_carrefour_product(base_cfg):
    adapter = CarrefourAdapter(base_cfg["sites"]["carrefour"], FakeSession({CARREFOUR_PRODUCT: "carrefour_product.html"}))
    product = adapter.scrape(DiscoveredItem("carrefour", CARREFOUR_PRODUCT, "TVs", "id:676576"))
    assert product.sku == "UA65M70HAUXEG" and product.extra["store_sku"] == "676576"
    assert product.extra["brand"] == "Samsung"                     # JSON-LD says "nonfood_samsung"
    offer = product.offers[0]
    assert (offer.price, offer.currency, offer.in_stock, offer.seller_name) == (Decimal("29777.00"), "EGP", True, "Carrefour")
    assert product.specs["Display diagonal"] == "65 Inch" and product.specs["HDMI ports quantity"] == "3"


def test_carrefour_sold_out_product_has_no_price(base_cfg):
    adapter = CarrefourAdapter(base_cfg["sites"]["carrefour"], FakeSession({CARREFOUR_PRODUCT: "carrefour_product_oos.html"}))
    product = adapter.scrape(DiscoveredItem("carrefour", CARREFOUR_PRODUCT, "TVs", "id:676576", hints={"price": "1,000.00"}))
    assert product.offers[0].in_stock is False and product.offers[0].price is None   # no stale listing price


# ------------------------------------------------------------------ Vodafone
def _vodafone_session():
    return FakeSession(captures={VODAFONE_CAT: [
        (LIST_API, {}, fixture_json("vodafone_catalog_p1.json")),
        (LIST_API, {}, fixture_json("vodafone_catalog_p2.json"))]})


def test_vodafone_discover_builds_urls_from_captured_catalogue(base_cfg):
    adapter = VodafoneAdapter(base_cfg["sites"]["vodafone"], _vodafone_session())
    items = list(adapter.discover(VODAFONE_CAT, "TVs"))
    assert len(items) == 3                                         # 2 + 2 rows with one repeated
    assert items[0].url == "https://eshop.vodafone.com.eg/en/prod/samsung-smart-tv-50-inch-crystal-4k-uhd-50u8000h"
    assert items[0].hints["sku"] == "4444170415" and items[0].hints["price"] == 19999.0
    assert items[0].hints["brand"] == "Samsung"


def test_vodafone_ignores_rows_from_other_categories(base_cfg):
    adapter = VodafoneAdapter(base_cfg["sites"]["vodafone"], FakeSession())
    row = {"uri": "/some-case", "name": "Case", "categoryUrls": ["/accessories"], "bestPrice": {"amount": 5, "currency": "EGP"}}
    assert list(adapter.items_from_catalogue([{"content": [row]}], VODAFONE_CAT, "TVs")) == []
    assert category_path(VODAFONE_CAT) == "/tv"


def test_vodafone_product_with_captured_specs(base_cfg):
    session = FakeSession({VODAFONE_PRODUCT: "vodafone_product.html"},
                          {VODAFONE_PRODUCT: [("catalog/product-specifications/1", {}, fixture_json("vodafone_specs.json"))]})
    product = VodafoneAdapter(base_cfg["sites"]["vodafone"], session).scrape(
        DiscoveredItem("vodafone", VODAFONE_PRODUCT, "TVs", "k"))
    assert product.name == "Hisense Smart TV 32-inch A4 HD VIDAA - 32A4Q"
    assert product.sku == "32A4Q" and product.extra["store_sku"] == "4444170559"
    assert (product.offers[0].price, product.offers[0].currency, product.offers[0].seller_name) == (Decimal("7999"), "EGP", "Vodafone")
    assert product.specs and all(product.specs.values())


def test_vodafone_specs_payload_is_flattened():
    payload = {"specificationsGroup": [{"specifications": [
        {"specification": {"label": "RAM"}, "description": "8 GB"},
        {"specification": {"label": "Empty"}, "description": ""}]}]}
    assert specs_from_payload(payload) == {"RAM": "8 GB"}


class _ScrollingPage:
    """Each scroll makes the fake page deliver its next JSON response."""

    def __init__(self, captured, upcoming):
        self.captured, self.upcoming, self.scrolls = captured, list(upcoming), 0

    async def evaluate(self, _script):
        self.scrolls += 1
        if self.upcoming:
            self.captured.append(self.upcoming.pop(0))


def test_vodafone_driver_scrolls_until_last_page():
    capture = lambda body: CapturedJson(f"https://x/{LIST_API}?page=0", "", 200, body)
    pages = [{"content": [], "last": False}, {"content": [], "last": False}, {"content": [], "last": True}]
    captured = [capture(pages[0])]
    page = _ScrollingPage(captured, [capture(pages[1]), capture(pages[2])])
    asyncio.run(make_scroll_driver(20, 0, 0)(page, captured))
    assert page.scrolls == 2 and len(captured) == 3


def test_vodafone_driver_respects_page_limit():
    capture = lambda: CapturedJson(f"https://x/{LIST_API}", "", 200, {"content": [], "last": False})
    captured = [capture()]
    page = _ScrollingPage(captured, [capture() for _ in range(9)])
    asyncio.run(make_scroll_driver(3, 0, 0)(page, captured))
    assert len(captured) == 3


# ------------------------------------------------------------------ Yalla Tager
YT_CAT = "https://uat.yallatager.com/ar/Smart-Phone-(-Madein-Egypt-).html"


def test_base_sku_and_description_specs():
    assert base_sku("195949822322-1293", "1293") == "195949822322"
    assert base_sku("6132368500281-EGY-986", "986") == "6132368500281-EGY"
    assert base_sku("ABC", "12") == "ABC"
    specs = description_specs(fixture_json("yallatager_item.json")["description"])
    assert specs["Brand"] == "Apple" and specs["Storage Capacity"] == "128 GB" and specs["Color"] == "Pink"


def test_yallatager_helpers():
    assert seller_id_from_url("https://x/ar/Apple-iPhone-16-226.html?a=1") == "226"
    assert seller_id_from_url("https://x/ar/no-seller") == ""
    assert button_in_stock("Buy Now") is True and button_in_stock(" buy now ") is True
    assert button_in_stock("Out of Stock") is False and button_in_stock("Add to wishlist") is None
    assert button_in_stock(None) is None


def test_yallatager_files_phones_and_tablets_by_name():
    assert category_for("Samsung Galaxy Tab A9 64GB", "Mobiles") == "Tablets"
    assert category_for("Apple iPad 2025 128GB", "Tablets") == "Tablets"
    assert category_for("Samsung Galaxy A55 256GB", "Mobiles") == "Mobiles"
    assert category_for("Samsung Galaxy A55 256GB", "Tablets") is None       # noise from a tablet search
    assert category_for("Samsung TV Tab Stand", "TVs") == "TVs"


def test_yallatager_sku_label_needs_a_digit_and_ignores_scripts():
    assert sku_from_page("SKU: 6132368500281-EGY-986", "") == "6132368500281-EGY-986"
    assert sku_from_page("sku\n195949822322-226", "") == "195949822322-226"
    assert sku_from_page("SKU Number", "<p>6132368500281-EGY-986</p>") == "6132368500281-EGY-986"
    assert sku_from_page("nothing here", "<p>nothing</p>") == ""


def test_yallatager_specs_text_formats():
    assert specs_from_text("no section") == {}
    assert specs_from_text("Technical Specification\nBrand\nApple\nColor\nPink\nRelated Products\nJunk\nX") == {
        "Brand": "Apple", "Color": "Pink"}
    assert specs_from_text("Technical Specification\nBrand: Apple\nRAM: 8 GB") == {"Brand": "Apple", "RAM": "8 GB"}
    assert specs_from_text("Technical Specification\nBrand\n: Apple\nRAM\n: 8 GB") == {"Brand": "Apple", "RAM": "8 GB"}
    assert specs_from_text("Technical Specification\nJust one sentence") == {
        "Technical Specification": "Just one sentence"}


def test_yallatager_listing_reads_the_cards_with_the_scripts_selectors(base_cfg, tmp_path):
    site_cfg = use_yalla_test_site(base_cfg)["sites"]["yallatager"]
    session = FakeSession({YT_CAT: "yallatager_listing.html"})
    items = list(YallaTagerAdapter(site_cfg, session, state_dir=tmp_path).discover(YT_CAT, "Mobiles"))
    assert len(items) == 2 and {i.category for i in items} == {"Mobiles"}
    ace = next(i for i in items if "Ace Urban" in i.hints["name"])
    assert ace.hints["image"].endswith("card.png") and ace.hints["offers"][0]["price"] == "2595"
    assert ace.hints["offers"][0]["in_stock"] is True and ace.hints["offers"][0]["seller_id"] == "986"


def test_yallatager_product_without_page_json_uses_the_text(base_cfg, tmp_path):
    site_cfg = use_yalla_test_site(base_cfg)["sites"]["yallatager"]
    session = FakeSession({YT_CAT: "yallatager_listing.html"})
    adapter = YallaTagerAdapter(site_cfg, session, state_dir=tmp_path)
    ace = next(i for i in adapter.discover(YT_CAT, "Mobiles") if "Ace Urban" in i.hints["name"])
    session.routes[ace.url] = "yallatager_product_text.html"
    product = adapter.scrape(ace)
    assert product.sku == "6132368500281-EGY"                  # seller suffix removed; the script's value ignored
    assert product.specs == {"Brand": "Ace", "Storage Capacity": "32 GB", "RAM": "2 GB", "Color": "Black"}
    assert product.offers[0].seller_name == "Seller #986" and product.offers[0].price == Decimal("2595")
    assert product.image_url.endswith("card.png")


def test_yallatager_groups_sellers_and_picks_cheapest(base_cfg, tmp_path):
    site_cfg = use_yalla_test_site(base_cfg)["sites"]["yallatager"]
    session = FakeSession({YT_CAT: "yallatager_listing.html"})
    adapter = YallaTagerAdapter(site_cfg, session, state_dir=tmp_path)
    items = list(adapter.discover(YT_CAT, "Mobiles"))
    assert len(items) == 2                                   # 4 cards, 2 unique products
    iphone = next(i for i in items if "iPhone" in i.hints["name"])
    assert len(iphone.hints["offers"]) == 3
    assert iphone.url.endswith("-226.html")                  # cheapest IN-STOCK seller (53000 is out of stock)

    session.captures[iphone.url] = [
        ("products/getItem", {"product_id": "181990"}, fixture_json("yallatager_item.json")),
        ("vendor/getVendorById", {"seller_id": "226"}, fixture_json("yallatager_vendor.json")),
        ("vendor/getproductsothersellers", {"product_id": "181990"}, fixture_json("yallatager_others.json")),
    ]
    product = adapter.scrape(iphone)
    assert product.sku == "195949822322"
    assert product.extra["brand"] == "أبل" and product.extra["condition"] == "new"
    assert product.specs["الماركة"] == "أبل" and product.specs["RAM"] == "8 GB"
    by_seller = {o.seller_id: o for o in product.offers}
    assert by_seller["226"].seller_name == "Mobile Zone" and by_seller["226"].price == Decimal("54500")
    assert by_seller["500"].in_stock is False
    assert by_seller["1293"].seller_name == "Seller #1293"     # unknown name is labelled, not blank
    # image host detected from the site's own API host
    assert product.image_url == "https://uat-be.yallatager.com/media/catalog/product/j/g/jggjgggggggg-removebg-preview_6.png"


class _Locator:
    def __init__(self, page, selector):
        self.page, self.selector = page, selector

    @property
    def first(self):
        return self

    async def count(self):
        return self.page.cards if self.selector == CARD else 1

    async def all_inner_texts(self):
        return ["1-40", f"{self.page.total:,}"] if self.page.total else []

    async def scroll_into_view_if_needed(self, timeout=None):
        return None


class _Mouse:
    async def move(self, x, y):
        return None

    async def wheel(self, x, y):
        return None


class _CardPage:
    """Adds ``step`` cards per scroll until ``available`` cards exist; then stops growing."""

    def __init__(self, cards, step, available, total=None):
        self.cards, self.step, self.available, self.total, self.scrolls = cards, step, available, total, 0
        self.mouse = _Mouse()

    def locator(self, selector):
        return _Locator(self, selector)

    async def wait_for_timeout(self, _ms):
        return None

    async def evaluate(self, _script):
        self.scrolls += 1

    async def wait_for_function(self, _script, arg=None, timeout=None):
        if self.cards >= self.available:
            raise TimeoutError("no growth")
        self.cards = min(self.available, self.cards + self.step)


def test_yallatager_scrolls_until_the_site_total():
    page = _CardPage(40, 40, 500, total=120)
    asyncio.run(make_card_driver(1500, 0, 0)(page, []))
    assert page.cards == 120 and page.scrolls == 2


def test_yallatager_stops_after_two_stalls_without_a_total():
    page = _CardPage(40, 40, 80)
    asyncio.run(make_card_driver(1500, 0, 0)(page, []))
    assert page.cards == 80 and page.scrolls == 3              # 1 growth + 2 stalls


def test_yallatager_stops_at_the_configured_limit():
    page = _CardPage(40, 40, 5000, total=5000)
    asyncio.run(make_card_driver(100, 0, 0)(page, []))
    assert page.cards == 120 and page.scrolls == 2
