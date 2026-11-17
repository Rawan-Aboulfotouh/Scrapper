"""End-to-end runs with an offline session: collector -> scraper -> price -> data -> storage, plus resume."""
import csv

import pytest

import scraper.pipeline as pipeline_module
from conftest import FakeSession, fixture_json, use_yalla_test_site
from scraper.errors import AccessDenied, TemporaryError
from scraper.pipeline import Pipeline

ROUTES = {
    "https://elsindbadstore.com/ar/tvs-and-audio-systems/smart-tvs.html": "elsindbad_listing_p1.html",
    "https://elsindbadstore.com/ar/tvs-and-audio-systems/smart-tvs.html?p=2": "elsindbad_listing_p2.html",
    "https://elsindbadstore.com/ar/lg-65-inch-oled-evo-ai-cs6-4k-144hz-smart-tv-oled65cs6va.html": "elsindbad_product.html",
    "https://www.awadserag.com/shop/category/13": "awadserag_listing.html",
    "https://www.awadserag.com/shop/category/13/page/2": "awadserag_listing_p2.html",
    "https://www.awadserag.com/shop/500020655-43-43mf423c-38794": "awadserag_product.html",
    "https://www.awadserag.com/shop/104555-55-55qned80-31200": "awadserag_product2.html",
    "https://uat.yallatager.com/ar/Smart-Phone-(-Madein-Egypt-).html": "yallatager_listing.html",
}
YALLA_CATEGORY = "https://uat.yallatager.com/ar/Smart-Phone-(-Madein-Egypt-).html"
YALLA_PRODUCT = ("https://uat.yallatager.com/ar/Smart-Phone-(-Madein-Egypt-)/Apple/Apple-iPhone-16-128GB-8GB-Pink-"
                 "(%D8%B6%D9%85%D8%A7%D9%86-24-%D8%B4%D9%87%D8%B1)-226.html")


def _configure(cfg):
    """Three offline sites; the sites with their own adapter tests are switched off here."""
    use_yalla_test_site(cfg)
    for name in ("noon", "carrefour", "vodafone"):
        cfg["sites"][name]["enabled"] = False
    cfg["sites"]["elsindbad"]["category_urls"] = [
        {"url": "https://elsindbadstore.com/ar/tvs-and-audio-systems/smart-tvs.html", "category": "TVs"}]
    cfg["sites"]["awadserag"]["category_urls"] = [{"url": "https://www.awadserag.com/shop/category/13", "category": "TVs"}]
    cfg["sites"]["yallatager"]["category_urls"] = [{"url": YALLA_CATEGORY, "category": "Mobiles"}]
    return cfg


def _install(monkeypatch, session):
    monkeypatch.setattr(pipeline_module, "CrawlSession", lambda settings: session)


def _yalla_captures():
    return {
        YALLA_PRODUCT: [("products/getItem", {}, fixture_json("yallatager_item.json")),
                        ("vendor/getVendorById", {}, fixture_json("yallatager_vendor.json")),
                        ("vendor/getproductsothersellers", {}, fixture_json("yallatager_others.json"))],
    }


def _read(path):
    with open(path, encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def test_full_run(base_cfg, monkeypatch):
    cfg = _configure(base_cfg)
    _install(monkeypatch, FakeSession(dict(ROUTES), _yalla_captures()))

    result = Pipeline(cfg).run()
    assert result["status"] == "completed"
    sites = result["sites"]
    assert sites["noon"]["status"] == "disabled"
    rows = {r["product_name"]: r for r in _read(cfg["output"]["products_file"])}

    lg = rows["شاشه ال جى 65بوصه OLED AI B4 4K سمارت ريموت ماجيك OLED65CS6VA"]
    assert (lg["sku"], lg["store_sku"], lg["lowest_price"], lg["currency"], lg["category"]) == (
        "OLED65CS6VA", "30408", "87999.00", "EGP", "TVs")
    assert lg["brand"] == "ال جي" and lg["screen_size"] == "65 بوصة" and lg["resolution"] == "UHD 4K"
    assert lg["operating_system"] == "WebOS" and lg["model"] == "OLED65CS6VA-SH5A"

    qned = rows["شاشه ال جي 55 بوصه QNED 4K 55QNED80"]
    assert (qned["availability"], qned["lowest_price"], qned["seller_name"]) == ("out_of_stock", "32990.00", "عوض سراج")
    assert sites["awadserag"]["out_of_stock"] == 1

    iphone = rows["Apple iPhone 16 - 128GB/8GB - Pink (ضمان 24 شهر)"]
    assert (iphone["seller_name"], iphone["lowest_price"], iphone["offers_count"]) == ("Mobile Zone", "54500.00", "2")
    assert iphone["storage"] == "128 GB" and iphone["ram"] == "8GB" and iphone["category"] == "Mobiles"
    assert iphone["sku"] == "195949822322"
    history = [h for h in _read(cfg["output"]["price_history_file"]) if h["sku"] == "195949822322"]
    assert sorted((h["price"], h["availability"]) for h in history) == [
        ("53000.00", "out_of_stock"), ("54500.00", "in_stock"), ("56000.00", "in_stock")]

    # second identical run: nothing new is added, history gets a new run
    _install(monkeypatch, FakeSession(dict(ROUTES), _yalla_captures()))
    second = Pipeline(cfg).run()
    assert second["status"] == "completed"
    assert all(s["new_products"] == 0 for s in second["sites"].values() if "new_products" in s)


def test_interrupted_run_resumes_without_refetching(base_cfg, monkeypatch):
    cfg = _configure(base_cfg)
    cfg["sites"]["yallatager"]["enabled"] = False
    calls = {"n": 0}

    class FlakySession(FakeSession):
        def scrape(self, url, config):
            if url.endswith("55qned80-31200"):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise KeyboardInterrupt          # simulate the PC being shut down mid-run
            return super().scrape(url, config)

    session = FlakySession(dict(ROUTES))
    _install(monkeypatch, session)
    assert Pipeline(cfg).run_site("awadserag")["status"] == "interrupted"

    session.requested.clear()
    assert Pipeline(cfg).run_site("awadserag")["status"] == "completed"
    # category pages and finished products were not requested again
    assert session.requested == ["https://www.awadserag.com/shop/104555-55-55qned80-31200"]


def test_failed_product_is_retried_limited_times(base_cfg, monkeypatch):
    cfg = _configure(base_cfg)

    class BrokenSession(FakeSession):
        def scrape(self, url, config):
            if url.endswith("43mf423c-38794"):
                raise TemporaryError("HTTP 503")
            return super().scrape(url, config)

    _install(monkeypatch, BrokenSession(dict(ROUTES)))
    result = Pipeline(cfg).run_site("awadserag")
    assert result["failed"] == 1 and result["status"] == "completed"


def test_blocked_site_stops_without_affecting_the_next_site(base_cfg, monkeypatch):
    cfg = _configure(base_cfg)
    cfg["sites"]["yallatager"]["enabled"] = False

    class BlockingSession(FakeSession):
        def scrape(self, url, config):
            if "elsindbadstore" in url:
                raise AccessDenied("HTTP 403")
            return super().scrape(url, config)

    session = BlockingSession(dict(ROUTES))
    _install(monkeypatch, session)
    result = Pipeline(cfg).run()
    assert result["sites"]["elsindbad"]["denied"] is True and result["sites"]["elsindbad"]["ok"] == 0
    assert result["sites"]["awadserag"]["ok"] == 1
    assert sum("elsindbadstore" in u for u in session.requested) == 0      # the fake raised before recording


def test_time_budget_pauses_the_batch_and_the_next_batch_finishes(base_cfg, monkeypatch):
    cfg = _configure(base_cfg)
    _install(monkeypatch, FakeSession(dict(ROUTES)))
    first = Pipeline(cfg).run_site("awadserag", time_budget_seconds=0)     # the batch's time is already up
    assert first["status"] == "paused"
    second = Pipeline(cfg).run_site("awadserag")                          # the next batch carries on
    assert second["status"] == "completed"
    assert len([r for r in _read(cfg["output"]["products_file"]) if r["site"] == "awadserag"]) == 2


def test_limit_processes_only_the_first_products_and_keeps_stock_flags(base_cfg, monkeypatch):
    cfg = _configure(base_cfg)
    session = FakeSession(dict(ROUTES))
    _install(monkeypatch, session)
    Pipeline(cfg).run_site("awadserag")
    Pipeline(cfg).run_site("awadserag", limit=1)
    rows = _read(cfg["output"]["products_file"])
    assert len(rows) == 2 and all(r["seen_in_last_run"] == "yes" for r in rows)   # a quick check is not a full pass


def test_site_requiring_permission_is_never_contacted(base_cfg, monkeypatch):
    session = FakeSession(captures=_yalla_captures())                 # shipped config: no permission recorded
    _install(monkeypatch, session)
    assert Pipeline(base_cfg).run_site("yallatager")["status"] == "not_permitted"
    assert Pipeline(base_cfg).run_site("yallatager", [{
        "site": "yallatager", "kind": "product", "category": "Mobiles",
        "url": "https://yallatager.com/en/x.html"}])["status"] == "not_permitted"
    assert session.requested == []


def test_permission_needs_a_reference(base_cfg, monkeypatch):
    use_yalla_test_site(base_cfg)
    base_cfg["sites"]["yallatager"]["permission_reference"] = ""      # flag set but nobody recorded
    session = FakeSession(captures=_yalla_captures())
    _install(monkeypatch, session)
    assert Pipeline(base_cfg).run_site("yallatager")["status"] == "not_permitted"
    assert session.requested == []


def test_pipeline_registers_robots_exception_and_logs_reference(base_cfg, monkeypatch, caplog):
    cfg = _configure(base_cfg)
    session = FakeSession(dict(ROUTES), _yalla_captures())
    _install(monkeypatch, session)
    with caplog.at_level("WARNING"):
        Pipeline(cfg).run_site("yallatager")
    assert session.robots_permission == {"uat.yallatager.com": "offline test fixtures"}
    assert "running under written permission: offline test fixtures" in caplog.text


def test_accessories_are_skipped_without_download_and_reported(base_cfg, monkeypatch):
    cfg = _configure(base_cfg)
    session = FakeSession(dict(ROUTES))
    _install(monkeypatch, session)
    result = Pipeline(cfg).run_site("elsindbad")
    assert result["excluded"] == 1
    assert "https://elsindbadstore.com/ar/tv-wall-mount-32-65.html" not in session.requested   # never downloaded
    excluded_file = cfg["output"]["products_file"].replace("products.csv", "excluded_products.csv")
    by_name = {r["product_name"]: r["reason"] for r in _read(excluded_file)}
    assert "starts with" in by_name["حامل شاشة حائط ثابت 32-65 بوصة"]
    assert sum(reason.startswith("not found") for reason in by_name.values()) == result["not_found"] == 2
    assert all("حامل" not in r["product_name"] for r in _read(cfg["output"]["products_file"]))


def test_each_site_keeps_its_own_excluded_rows(base_cfg, monkeypatch):
    cfg = _configure(base_cfg)
    _install(monkeypatch, FakeSession(dict(ROUTES)))
    Pipeline(cfg).run_site("elsindbad")
    Pipeline(cfg).run_site("awadserag")
    excluded_file = cfg["output"]["products_file"].replace("products.csv", "excluded_products.csv")
    assert "elsindbad" in {r["site"] for r in _read(excluded_file)}    # not wiped by the later awadserag run


def test_two_runs_of_one_site_cannot_overlap(base_cfg, monkeypatch):
    from scraper.state import RunLock
    from pathlib import Path
    cfg = _configure(base_cfg)
    _install(monkeypatch, FakeSession(dict(ROUTES)))
    lock_path = Path(cfg["paths"]["state_dir"]) / "sites" / "awadserag" / "run.lock"
    with RunLock(lock_path):
        assert Pipeline(cfg).run_site("awadserag")["status"] == "skipped"
        assert Pipeline(cfg).run_site("elsindbad")["status"] == "completed"   # other sites are independent


def test_out_of_stock_without_a_price_is_reported_as_out_of_stock():
    from decimal import Decimal
    from scraper.data_processor import DataProcessor
    from scraper.models import Offer, ScrapedProduct
    from scraper.price_processor import PriceDecision
    product = ScrapedProduct("carrefour", "https://x/p/1", "Mobiles", name="Ace Buzz 3 32GB",
                             offers=[Offer("Carrefour", None, "EGP", in_stock=False)])
    record = DataProcessor({"filters": {}}).build_record(product, PriceDecision(None, 0, 1), "2026-11-10T10:00:00+02:00")
    assert record["availability"] == "out_of_stock" and record["lowest_price"] == ""
    product.offers[0].in_stock = None
    assert DataProcessor({"filters": {}}).build_record(product, PriceDecision(None, 0, 1), "t")["availability"] == "no_price"
    assert Decimal("1") == 1
