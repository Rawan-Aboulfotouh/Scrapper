from decimal import Decimal

import pytest

from scraper.models import Offer
from scraper.price_processor import PriceProcessor
from scraper.robots import RobotsCache, RobotsRules
from scraper.specs import normalize_specs
from scraper.utils import (InvalidURLError, detect_currency, extract_key_values, parse_price,
                           strip_query_params, validate_url)


# ------------------------------------------------------------------ prices
@pytest.mark.parametrize("raw,expected", [
    ("٨٧٬٩٩٩٫٠٠ ج.م.‏", Decimal("87999.00")),
    ("44,500.000 E£", Decimal("44500.000")),
    ("8890.0", Decimal("8890.0")),
    ("56,000 جنية", Decimal("56000")),
    ("1.299,50 EGP", Decimal("1299.50")),
    (2595, Decimal("2595")),
    ("0", None),
    ("", None),
    ("call for price", None),
    (None, None),
])
def test_parse_price(raw, expected):
    assert parse_price(raw) == expected


def test_detect_currency():
    assert detect_currency("٨٧٬٩٩٩ ج.م.") == "EGP"
    assert detect_currency("11,890.000 E£") == "EGP"
    assert detect_currency("1,349.00 AED") == "AED"
    assert detect_currency("no currency", "EGP") == "EGP"


def test_extract_key_values_tabs_and_colons():
    text = "الموديل\tOLED65CS6VA-SH5A\nالحجم\tبوصة 65\nBrand: Apple\nnot a pair\nhttps://x.y/z"
    assert extract_key_values(text) == {"الموديل": "OLED65CS6VA-SH5A", "الحجم": "بوصة 65", "Brand": "Apple"}


def test_validate_url():
    assert validate_url("https://www.awadserag.com/shop/x#frag", ["awadserag.com"]) == "https://www.awadserag.com/shop/x"
    with pytest.raises(InvalidURLError):
        validate_url("https://evil.com/shop", ["awadserag.com"])
    with pytest.raises(InvalidURLError):
        validate_url("ftp://awadserag.com/x", ["awadserag.com"])
    with pytest.raises(InvalidURLError):
        validate_url("not a url", ["awadserag.com"])


def test_strip_query_params():
    assert strip_query_params("https://a.com/p?category=13&utm_source=x", keep_only=()) == "https://a.com/p"


def test_specs_normalisation_arabic_variants():
    canonical, other = normalize_specs({"مقاس الشاشه": "65 بوصة", "مقاس الشاشة": "ignored duplicate",
                                        "Storage Capacity": "128 GB", "الماركات": "Fresh", "Face ID": "Yes"})
    assert canonical == {"screen_size": "65 بوصة", "storage": "128 GB", "brand": "Fresh"}
    assert other == {"Face ID": "Yes"}


# ------------------------------------------------------------------ robots
ELSINDBAD_ROBOTS = """
User-agent: *
Disallow: /checkout/
Disallow: /catalogsearch/
Disallow: /*?*brand=
Disallow: /*?*screen_size=
Disallow: /*?*isAjax=
Sitemap: https://elsindbadstore.com/sitemap.xml
"""


def test_robots_wildcards():
    rules = RobotsRules(ELSINDBAD_ROBOTS)
    ua = "PriceMonitorBot/1.0"
    assert rules.can_fetch(ua, "https://elsindbadstore.com/ar/tvs-and-audio-systems/smart-tvs.html")
    assert rules.can_fetch(ua, "https://elsindbadstore.com/ar/tvs-and-audio-systems.html?p=2")
    assert not rules.can_fetch(ua, "https://elsindbadstore.com/ar/tvs.html?p=2&brand=5")
    assert not rules.can_fetch(ua, "https://elsindbadstore.com/checkout/cart/")
    assert rules.sitemaps == ["https://elsindbadstore.com/sitemap.xml"]


def test_robots_allow_beats_disallow_and_empty_disallow():
    rules = RobotsRules("User-agent: *\nDisallow: /shop/\nAllow: /shop/category/\n")
    assert rules.can_fetch("x", "https://a.com/shop/category/13")
    assert not rules.can_fetch("x", "https://a.com/shop/cart")
    assert RobotsRules("User-agent: *\nDisallow:\n").can_fetch("x", "https://a.com/anything")


def test_robots_specific_agent_and_crawl_delay():
    rules = RobotsRules("User-agent: PriceMonitorBot\nDisallow: /\nCrawl-delay: 9\n\nUser-agent: *\nDisallow:\n")
    assert not rules.can_fetch("PriceMonitorBot/1.0 (+contact)", "https://a.com/x")
    assert rules.crawl_delay("PriceMonitorBot/1.0") == 9
    assert rules.can_fetch("OtherBot", "https://a.com/x")


def test_robots_cache_status_codes():
    assert RobotsCache(lambda u: (404, "")).can_fetch("x", "https://a.com/p")
    assert not RobotsCache(lambda u: (503, "")).can_fetch("x", "https://a.com/p")

    def boom(url):
        raise ConnectionError("down")
    assert not RobotsCache(boom).can_fetch("x", "https://a.com/p")


# ------------------------------------------------------------------ lowest price
def _offer(seller, price, stock=True, currency="EGP", sid=""):
    return Offer(seller_name=seller, price=parse_price(price), currency=currency, in_stock=stock, seller_id=sid)


def test_lowest_price_numeric_not_string():
    pp = PriceProcessor({"preferred_currency": "EGP"})
    decision = pp.select_lowest([_offer("A", "9,999"), _offer("B", "10,000"), _offer("C", "100,000")])
    assert decision.offer.seller_name == "A" and decision.valid_offers == 3


def test_lowest_price_excludes_unavailable_and_invalid():
    pp = PriceProcessor({"preferred_currency": "EGP"})
    decision = pp.select_lowest([_offer("Cheap but gone", "100", stock=False), _offer("Zero", "0"),
                                 _offer("Valid", "500"), _offer("Unknown stock", "700", stock=None)])
    assert decision.offer.seller_name == "Valid" and decision.valid_offers == 2   # cheaper but gone is ignored
    # everything out of stock: still reported, with the lowest listed price
    gone = pp.select_lowest([_offer("X", "150", stock=False), _offer("Y", "100", stock=False)])
    assert gone.offer.seller_name == "Y" and gone.offer.in_stock is False and gone.valid_offers == 0
    assert pp.select_lowest([_offer("Z", "0", stock=False)]).offer is None             # no valid price at all


def test_tie_break_rule():
    pp = PriceProcessor({"preferred_currency": "EGP"})
    decision = pp.select_lowest([_offer("Zeta", "500", stock=None), _offer("beta", "500"), _offer("Alpha", "500")])
    # confirmed in stock first, then alphabetical (case-insensitive)
    assert decision.offer.seller_name == "Alpha"


def test_no_cross_currency_comparison():
    pp = PriceProcessor({"preferred_currency": "EGP"})
    decision = pp.select_lowest([_offer("AED seller", "100", currency="AED"), _offer("EGP seller", "1500")])
    assert decision.offer.seller_name == "EGP seller" and decision.offer.currency == "EGP"


# --------------------------------------------------------------------- TV / phone filter
KEEP_TVS = [
    "شاشه ال جى 65بوصه OLED AI B4 4K سمارت ريموت ماجيك OLED65CS6VA",      # "ريموت" but a 65" TV
    "شاشه فريش 43 سمارت كوليتا بحامل 43MF423C",                            # "بحامل" but a 43" TV
    "شاشه جاك 43بوصه اندرويد ريسيفر بدون فريم NGLD143JE8121F",
    "شاشه شارب 32بوصة سمارت برسيفر داخلى بدون فريم 2TC32FG6EX",
    "Samsung 55 Inch Crystal UHD 4K Smart TV UA55DU7000",
    "LG 43\" Full HD Smart LED TV 43LM6370PVA",
]
DROP_TVS = [
    "حامل تليفزيون للشاشات المسطحة، متوافق مع LED / LCD / PDP مقاس 26-63 بوصة, SU002 - فضي",
    "ريموت شاشة سامسونج سمارت اصلي",
    "Universal remote control for LG smart TV",
    "Xiaomi TV Box S 2nd Gen 4K",
    "Mi TV Stick FHD",
    "ساوند بار سامسونج HW-B450",
    "كابل HDMI 4K طول 3 متر",
    "ريسيفر ستار سات SR-2090",
    "Wall Mount Bracket 32-65 inch",
    "ستاند شاشة ارضي متحرك",
    "شاشه حمايه لشاشات التليفزيون 55 بوصه",
]
KEEP_PHONES = [
    "Apple iPhone 16 - 128GB/8GB - Pink (ضمان 24 شهر)",
    "Ace Urban 3 - 32GB/2GB - Black (صنع في مصر)",
    "Itel A100C - 64GB/2GB - Titanium Gold (صنع في مصر)",
    "Nokia 105 - Dual SIM - Black",                                         # feature phone
    "Samsung Galaxy A15 128GB 6GB RAM + شاحن هدية",                         # "شاحن" but a phone
    "Doogee S41 Max Rugged 256GB/6GB",                                      # rugged phone
]
DROP_PHONES = [
    "جراب ايفون 16 شفاف",
    "Screen Protector for Galaxy A15",
    "اسكرينة جلاس سامسونج A55",
    "شاحن سامسونج 25 وات اصلي",
    "Samsung 25W Travel Charger",
    "Anker Power Bank 20000mAh",
    "Galaxy Buds FE",
    "Apple AirPods Pro 2",
    "Samsung Galaxy Watch 6 - 16GB",                                       # GB, but a watch
    "Samsung Galaxy Tab A9 64GB",                                          # tablet, not a phone
    "SanDisk Ultra microSD 128GB",                                         # GB, but a memory card
    "Xiaomi Mi Car Holder",
    "كابل تايب سي 1 متر",
]


@pytest.mark.parametrize("name", KEEP_TVS)
def test_real_tvs_are_kept(name):
    from scraper.product_filter import ProductTypeFilter
    assert ProductTypeFilter().check_product(name, "TVs", {}) == (False, "")


@pytest.mark.parametrize("name", DROP_TVS)
def test_tv_accessories_are_excluded(name):
    from scraper.product_filter import ProductTypeFilter
    excluded, reason = ProductTypeFilter().check_product(name, "TVs", {})
    assert excluded, name


@pytest.mark.parametrize("name", KEEP_PHONES)
def test_real_phones_are_kept(name):
    from scraper.product_filter import ProductTypeFilter
    assert ProductTypeFilter().check_product(name, "Mobiles", {}) == (False, "")


@pytest.mark.parametrize("name", DROP_PHONES)
def test_phone_accessories_are_excluded(name):
    from scraper.product_filter import ProductTypeFilter
    excluded, reason = ProductTypeFilter().check_product(name, "Mobiles", {})
    assert excluded, name


def test_specs_and_price_rules():
    from decimal import Decimal
    from scraper.product_filter import ProductTypeFilter
    f = ProductTypeFilter()
    # no size in the name, but the specifications say 43": still a TV
    assert f.check_product("شاشه فريش كوليتا بحامل", "TVs", {"مقاس الشاشة": "43 بوصه"}) == (False, "")
    assert f.check_product("شاشه فريش كوليتا بحامل", "TVs", {})[0]
    assert f.check_price("TVs", Decimal("450"))[0] and not f.check_price("TVs", Decimal("9850"))[0]
    # config can extend the lists and protect a word
    custom = ProductTypeFilter({"product_rules": {"TVs": {"always_exclude": ["مستعمل"], "min_price": 0},
                                                  "Mobiles": {"never_exclude": ["tab"]}}})
    assert custom.check_product("شاشه سامسونج 43 بوصه مستعمل", "TVs", {})[0]
    assert not custom.check_price("TVs", Decimal("450"))[0]
    assert not custom.check_product("Samsung Galaxy Tab A9 64GB", "Mobiles", {})[0]


@pytest.mark.parametrize("name,expected", [
    ("شاشه ال جى 43بوصة سمارت 43LM6370PVA", "43LM6370PVA"),
    ("شاشه ال جى 65بوصه OLED AI B4 4K سمارت ريموت ماجيك OLED65CS6VA", "OLED65CS6VA"),
    ("شاشه جاك 65بوصة جوجل برسيفر بدون فريم NGLD65JE8135F-K4", "NGLD65JE8135F-K4"),
    ("شاشه فريش 43بوصه سمارت جوجل 43MF435G كود 20977", "43MF435G"),
    ("شاشه تى سى ال 43بوصه QLED سمارت بدون فريم 43S5K", "43S5K"),
    ("Apple iPhone 16 - 128GB/8GB - Pink (ضمان 24 شهر)", ""),
    ("Samsung Galaxy A15 128GB 6GB RAM 5000mAh", ""),
])
def test_model_code_from_name(name, expected):
    from scraper.utils import model_code_from_name
    assert model_code_from_name(name) == expected


def test_size_with_a_hyphen_is_not_a_model_code():
    from scraper.utils import model_code_from_name
    assert model_code_from_name("INOI Tab 2 Pro - 11-inch 256GB/6GB Dual SIM 4G Tablet") == ""
    assert model_code_from_name("Honor X5c Plus - 6.74-inch 128GB/4GB Dual SIM 4G") == ""


# ------------------------------------------------------------------ names seen on the live sites
@pytest.mark.parametrize("category,name", [
    ("Mobiles", "Infinix Hot 60 Pro Dual SIM Coral Tides 16GB(8+8)RAM 256GB 4G with Free Gift: Phone Case"),
    ("Mobiles", "Xiaomi Redmi 17 Dual SIM Black 6GB RAM 256GB 4G With Free Xiaomi Power Bank Lite"),
    ("Mobiles", "Infinix GT50 Pro 5G Smart Phone (12+12/512) With Cooling Fan + Back Cover"),
    ("Mobiles", "HONOR Magic V3 (12/512) + X7lite Buds + Watch 2i"),
    ("TVs", "Pluto 65-inch 4K Smart LED TV - Built-in Receiver - Shatterproof-Wall Mount"),
    ("TVs", "Fresh 40-Inch Full HD Smart TV Monitor - 40MF435G"),
    ("TVs", "Fresh Smart Google TV Monitor 43-inch FHD (Model 43MF435G)"),
    ("TVs", 'Xiaomi TV A Pro 55" 2026 | Google TV | 4K QLED Display | With Voice Remote Control'),
    ("Tablets", "Lenovo Tab One With Clear Case, 8.7\" ,MediaTek Helio G85 processor, 4GB RAM, 128GB"),
    ("Tablets", "Lenovo Idea Tab 5G (8/128) 11 Folio Case / Pen - Luna Grey"),
    ("Tablets", "Honor Pad 10 Grey 8GB RAM 256GB Wi-Fi, With Free Keyboard - Middle East Version"),
])
def test_devices_sold_with_extras_are_kept(category, name):
    from scraper.product_filter import ProductTypeFilter
    assert ProductTypeFilter().check_name(name, category) == (False, "")


@pytest.mark.parametrize("category,name", [
    ("Mobiles", "Samsung Galaxy Tab S10 FE Gray 12GB RAM 256GB Wifi - Middle East Version"),
    ("Mobiles", "Samsung Galaxy Watch 8 44mm 32GB Graphite"),
    ("Mobiles", "Silicone Case For iPhone 15 128GB"),
    ("TVs", "TV Wall Mount Bracket For 32-65 Inch Screens"),
    ("Tablets", "Apple Magic Keyboard for iPad Pro 13 inch M4"),
    ("Tablets", "Screen Protector for Huawei Matepad Air 11.5 Tablet"),
    ("Tablets", "Majentik MP-001 Pen Capacitive Universal Tablet Mobile Stylus"),
    ("Tablets", "Fintie Hybrid Case for Samsung Galaxy Tab A9 Plus 11 Inch 2023"),
])
def test_accessories_are_still_excluded(category, name):
    from scraper.product_filter import ProductTypeFilter
    assert ProductTypeFilter().check_name(name, category)[0] is True


def test_tablet_signals_and_minimum_price():
    from decimal import Decimal
    from scraper.product_filter import ProductTypeFilter
    f = ProductTypeFilter()
    assert f.check_product("Lenovo Tab M11", "Tablets", {"Screen Size": "11 in"}) == (False, "")
    assert f.check_product("Keyboard stand", "Tablets", {})[0] is True
    assert f.check_price("Tablets", Decimal("500"))[0] is True
    assert f.check_price("Tablets", Decimal("6000")) == (False, "")


@pytest.mark.parametrize("name", [
    "VIVO V50 5G + Anker R50i Buds Bundle",                       # Vodafone, excluded wrongly before
    "HONOR X9C Smart + Earbuds X6 - White",                       # Vodafone
    "HONOR Magic7 Pro (12/512)+ Honor Earbuds Clip Black gift",   # Vodafone
    "Realme 14 5G Smart Phone + Realme T01 Buds",                 # Vodafone
    "Realme 14T 5G Smart Phone + Realme T01 Buds",                # Vodafone
])
def test_phones_sold_with_earbuds_are_kept(name):
    from scraper.product_filter import ProductTypeFilter
    assert ProductTypeFilter().check_product(name, "Mobiles", {}) == (False, "")


@pytest.mark.parametrize("name", [
    "Samsung Galaxy Buds2 Pro Wireless Earbuds",
    "Realme Buds Air 5 Pro",
    "Earbuds Bluetooth Wireless Headset",
])
def test_earbuds_sold_alone_are_excluded(name):
    from scraper.product_filter import ProductTypeFilter
    assert ProductTypeFilter().check_product(name, "Mobiles", {})[0] is True
