"""scrape_site(): retries with exponential backoff, block handling, robots.txt and wait logic."""
import asyncio
from types import SimpleNamespace

import pytest

from scraper import crawl_engine
from scraper.crawl_engine import (CrawlSession, CrawlSettings, ScrapeConfig, backoff_seconds, looks_like_challenge,
                                  parse_retry_after, scrape_site, wait_expression)
from scraper.errors import AccessDenied, NotFound, RobotsDisallowed, TemporaryError
from scraper.robots import RobotsRules

URL = "https://example.com/page"
CONFIG = ScrapeConfig()
BIG_PAGE = "<html>" + "x" * 70_000 + "</html>"


def _result(status=200, html=BIG_PAGE, success=True, error="", headers=None, extracted=None, final=URL):
    return SimpleNamespace(status_code=status, html=html, success=success, error_message=error,
                           response_headers=headers or {}, extracted_content=extracted, redirected_url=final)


def _session(results, **settings):
    """A session whose crawler replays ``results``; sleeps are recorded, not slept."""
    options = dict(respect_robots_txt=False, min_delay_seconds=0, max_delay_seconds=0, backoff_base_seconds=5)
    options.update(settings)
    session = CrawlSession(CrawlSettings(**options))
    calls = []

    async def fake_crawl(url, config):
        calls.append(url)
        item = results.pop(0)
        if isinstance(item, Exception):
            raise item
        return session._classify(url, config, item, crawl_engine._ActiveCapture(config))

    session._crawl = fake_crawl
    session._submit = asyncio.run
    session._stop_crawler = lambda: asyncio.sleep(0)
    return session, calls


@pytest.fixture
def sleeps(monkeypatch):
    recorded = []
    monkeypatch.setattr(crawl_engine.time, "sleep", recorded.append)
    return recorded


def test_success_needs_one_attempt(sleeps):
    session, calls = _session([_result(extracted='[{"name": "TV"}]')])
    result = scrape_site(URL, ScrapeConfig(schema={"name": "t", "baseSelector": "html", "fields": []}), session)
    assert result.items == [{"name": "TV"}] and result.item == {"name": "TV"} and len(calls) == 1 and sleeps == []


def test_temporary_error_is_retried_then_succeeds(sleeps):
    session, calls = _session([_result(503), _result(html="", success=False, error="Timeout"), _result()])
    assert scrape_site(URL, CONFIG, session).status == 200
    assert len(calls) == 3 and len(sleeps) == 2


def test_at_most_three_retries_with_growing_backoff(sleeps, monkeypatch):
    monkeypatch.setattr(crawl_engine.random, "uniform", lambda low, high: high)    # no jitter
    session, calls = _session([_result(500) for _ in range(4)])
    with pytest.raises(TemporaryError):
        scrape_site(URL, CONFIG, session)
    assert len(calls) == 4 and sleeps == [5, 10, 20]                               # 1 try + 3 retries


def test_retry_after_is_respected_and_capped(sleeps):
    session, _ = _session([_result(429, headers={"Retry-After": "42"}), _result()])
    scrape_site(URL, CONFIG, session)
    assert sleeps == [42]
    session, calls = _session([_result(429, headers={"retry-after": "9999"})])
    with pytest.raises(TemporaryError):
        scrape_site(URL, CONFIG, session)
    assert len(calls) == 1


@pytest.mark.parametrize("status", [401, 403])
def test_access_denied_is_never_retried(status, sleeps):
    session, calls = _session([_result(status)])
    with pytest.raises(AccessDenied):
        scrape_site(URL, CONFIG, session)
    assert len(calls) == 1 and sleeps == []


def test_akamai_block_page_is_not_mistaken_for_an_outage(sleeps):
    session, calls = _session([_result(503, html="<h1>Service Unavailable</h1>", success=False,
                                       error="Blocked by anti-bot protection: Akamai block (Reference #)")])
    with pytest.raises(AccessDenied):
        scrape_site(URL, CONFIG, session)
    assert len(calls) == 1


def test_challenge_page_stops_the_site(sleeps):
    html = '<div id="sec-if-cpt-container"><p class="scf-akamai-protected-by">x</p></div>'
    session, calls = _session([_result(200, html=html)])
    with pytest.raises(AccessDenied):
        scrape_site(URL, CONFIG, session)
    assert len(calls) == 1


def test_challenge_markers_on_a_large_page_are_ignored():
    assert not looks_like_challenge("<script>g-recaptcha</script>" + "x" * 70_000)
    assert looks_like_challenge("<script>g-recaptcha</script>")
    assert not looks_like_challenge("<html><body><h1>شاشه ال جى 43بوصة</h1></body></html>")


def test_short_xml_flagged_by_crawl4ai_is_accepted_as_a_document(sleeps):
    flagged = _result(200, html="<urlset></urlset>", success=False,
                      error="Blocked by anti-bot protection: Structural: no_content_elements")
    session, _ = _session([flagged])
    assert session.scrape(URL, ScrapeConfig(raw_document=True)).html == "<urlset></urlset>"
    session, _ = _session([flagged])
    with pytest.raises(AccessDenied):
        session.scrape(URL, CONFIG)                                    # the same result on a store page is a block


def test_not_found_status_and_error_page_redirect(sleeps):
    session, _ = _session([_result(404)])
    with pytest.raises(NotFound):
        scrape_site(URL, CONFIG, session)
    session, _ = _session([_result(final="https://example.com/en/error")])
    with pytest.raises(NotFound):
        scrape_site(URL, ScrapeConfig(not_found_paths=("/en/error",)), session)


def test_browser_crash_is_retried_with_a_new_browser(sleeps):
    session, calls = _session([RuntimeError("Target closed"), _result()])
    assert scrape_site(URL, CONFIG, session).status == 200 and len(calls) == 2


def test_robots_disallow_prevents_the_visit(sleeps):
    session, calls = _session([_result()], respect_robots_txt=True)
    session.robots._cache["https://example.com"] = RobotsRules("User-agent: *\nDisallow: /page\n")
    with pytest.raises(RobotsDisallowed):
        scrape_site(URL, CONFIG, session)
    assert calls == []


def test_robots_exception_only_for_hosts_with_written_permission(sleeps):
    session, calls = _session([_result()], respect_robots_txt=True)
    session.robots._cache["https://example.com"] = RobotsRules("User-agent: *\nDisallow: /\n")
    session.robots_permission["example.com"] = "Email from the site owner, 2026-11-15"
    assert scrape_site(URL, CONFIG, session).status == 200 and len(calls) == 1


def test_carrefour_robots_rule_blocks_paging_but_not_categories():
    rules = RobotsRules("User-agent: *\nDisallow: *?currentPage=*\nDisallow: /*.html\n")
    assert rules.can_fetch("PriceMonitorBot", "https://www.carrefouregypt.com/mafegy/en/c/NFEGY4080400")
    assert not rules.can_fetch("PriceMonitorBot", "https://www.carrefouregypt.com/mafegy/en/c/NFEGY4080400?currentPage=1")


def test_wait_expression_falls_through_after_the_timeout():
    expression = wait_expression('a[class*="x"]', 20_000)
    assert expression.startswith("js:") and "performance.now() > 20000" in expression
    assert '"a[class*=\\"x\\"]"' in expression
    assert wait_expression("", 1000) is None


def test_backoff_grows_and_is_capped():
    assert backoff_seconds(0, 5, 120) <= 5 and 5 < backoff_seconds(2, 5, 120) <= 20
    assert backoff_seconds(10, 5, 120) <= 120


def test_parse_retry_after():
    assert parse_retry_after("30") == 30 and parse_retry_after(None) is None
    assert parse_retry_after("not a date") is None
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 0
