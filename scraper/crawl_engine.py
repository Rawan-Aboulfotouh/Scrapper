"""
Crawl4AI engine: the single route every site uses to load a page.

``scrape_site(url, config, session)`` is the one public entry point. It takes a URL and a
:class:`ScrapeConfig` (selectors, wait strategy, dynamic-content handling) and returns a
:class:`ScrapeResult` with the extracted data.

How Crawl4AI is used here
-------------------------
* ``AsyncWebCrawler`` drives one headless Chromium per site run. Crawl4AI is asynchronous and the
  rest of the project is synchronous, so the crawler lives on a private event loop in a background
  thread and :meth:`CrawlSession.scrape` waits for it.
* ``JsonCssExtractionStrategy`` applies each site's CSS schema to the rendered page.
* ``wait_for`` is sent as a JavaScript condition, "selector present OR N ms elapsed". A page that
  legitimately has no products (one past the last page) then returns quickly and empty instead of
  failing with a timeout, which is what Crawl4AI's plain ``css:`` wait would do.
* Crawl4AI hooks handle single-page apps. ``on_page_context_created`` passively records the JSON the
  page's own front-end receives. ``before_retrieve_html`` runs a site's ``before_html_driver`` while
  the DOM can still change, so cards loaded by scrolling are in the HTML that gets extracted.
  ``before_return_html`` runs after the HTML is read: it holds the page open until the expected
  JSON arrives and can run a ``page_driver`` that only needs captured responses.

Deliberately not used
---------------------
``magic``, ``simulate_user``, ``override_navigator``, ``enable_stealth``, proxies and
``fallback_fetch_function``. A block page, CAPTCHA, 401 or 403 stops that site for the run: it is
never retried or worked around. Crawl4AI's own ``check_robots_txt`` is off because its parser does
not implement the ``*`` and ``$`` wildcards these sites use; :mod:`scraper.robots` does.
"""
from __future__ import annotations

import asyncio
import email.utils
import json
import logging
import random
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from .errors import AccessDenied, NotFound, RobotsDisallowed, ScrapeError, TemporaryError
from .robots import RobotsCache

log = logging.getLogger("scraper.crawl")

CHALLENGE_MARKERS = (
    "g-recaptcha", "h-captcha", "hcaptcha.com", "cf-chl-", "/cdn-cgi/challenge", "captcha-delivery",
    "px-captcha", "_incapsula_resource", "incapsula incident", "distil_r_captcha", "sec-if-cpt-container",
    "scf-akamai-protected-by", "behavioral-content", "are you a robot", "pardon our interruption",
)
# A real store page is hundreds of KB. Challenge markers are only trusted on small pages, because
# large healthy pages can reference the same scripts.
CHALLENGE_MAX_PAGE_BYTES = 60_000


def looks_like_challenge(html: str) -> bool:
    """True when a small page carries the marks of a CAPTCHA or bot-challenge interstitial."""
    if not html or len(html) > CHALLENGE_MAX_PAGE_BYTES:
        return False
    sample = html.lower()
    return any(marker in sample for marker in CHALLENGE_MARKERS)


# --------------------------------------------------------------------------- #
# What a site asks for, and what it gets back
# --------------------------------------------------------------------------- #
PageDriver = Callable[[Any, List["CapturedJson"]], Awaitable[None]]


@dataclass(frozen=True)
class ScrapeConfig:
    """
    The Crawl4AI configuration of one kind of page (a site's listing page or product page).

    Each site module defines its own instances, so all selectors and browser behaviour for a
    site live in that site's file.
    """
    schema: Optional[Dict[str, Any]] = None        # JsonCssExtractionStrategy schema
    wait_selector: str = ""                        # wait until this CSS selector exists ...
    wait_timeout_ms: int = 20_000                  # ... or this much time has passed
    wait_until: str = "domcontentloaded"           # Playwright navigation event
    settle_seconds: float = 0.5                    # extra pause before the HTML is read
    scroll_full_page: bool = False                 # Crawl4AI scan_full_page, for lazy content
    scroll_delay_seconds: float = 0.4
    max_scroll_steps: int = 40
    js_code: Tuple[str, ...] = ()                  # JavaScript run after load (e.g. expand a panel)
    capture_json: Tuple[str, ...] = ()             # record JSON responses whose URL has one of these
    capture_wait_for: Tuple[str, ...] = ()         # hold the page until these have been captured
    capture_timeout_seconds: float = 25
    capture_optional: Tuple[str, ...] = ()         # also wait for these, briefly; absence is not an error
    capture_optional_timeout_seconds: float = 8
    before_html_driver: Optional[PageDriver] = None   # async (page, captured), runs before the HTML is read
    page_driver: Optional[PageDriver] = None       # async (page, captured), runs after the HTML is read
    not_found_paths: Tuple[str, ...] = ()          # a redirect to one of these paths means "no such page"
    raw_document: bool = False                     # robots.txt / XML sitemap, not an HTML store page


@dataclass
class CapturedJson:
    """One JSON response the page's own front-end received."""
    url: str
    post_data: str
    status: int
    body: Any

    def post_json(self) -> Dict[str, Any]:
        try:
            return json.loads(self.post_data or "{}")
        except ValueError:
            return {}


@dataclass
class ScrapeResult:
    """What :func:`scrape_site` returns."""
    url: str
    final_url: str
    status: int
    html: str
    items: List[Dict[str, Any]] = field(default_factory=list)       # one dict per schema base element
    captured: List[CapturedJson] = field(default_factory=list)
    _soup: Optional[BeautifulSoup] = None

    def soup(self) -> BeautifulSoup:
        if self._soup is None:
            self._soup = BeautifulSoup(self.html or "", "lxml")
        return self._soup

    @property
    def item(self) -> Dict[str, Any]:
        """The first extracted item (product pages have exactly one)."""
        return self.items[0] if self.items else {}

    def find(self, fragment: str) -> List[CapturedJson]:
        return [c for c in self.captured if fragment in c.url]

    def first(self, fragment: str) -> Optional[CapturedJson]:
        found = self.find(fragment)
        return found[0] if found else None


# --------------------------------------------------------------------------- #
# Settings, throttle, backoff
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class CrawlSettings:
    """Run-wide crawl behaviour, built from the ``crawl:`` section of config.yaml."""
    headless: bool = True
    user_agent: str = ""
    robots_agent: str = "PriceMonitorBot"
    respect_robots_txt: bool = True
    locale: str = ""
    block_heavy_resources: bool = True
    page_timeout_seconds: float = 60
    max_retries: int = 3
    backoff_base_seconds: float = 5
    backoff_max_seconds: float = 120
    max_retry_after_seconds: float = 300
    min_delay_seconds: float = 3
    max_delay_seconds: float = 7
    restart_browser_every_pages: int = 200

    @classmethod
    def from_config(cls, section: Dict[str, Any]) -> "CrawlSettings":
        known = {k: v for k, v in (section or {}).items() if k in cls.__dataclass_fields__}
        return cls(**known)


class HostThrottle:
    """Guarantees a random pause of [min_delay, max_delay] seconds between hits on the same host."""

    def __init__(self, min_delay: float, max_delay: float):
        self.min_delay = float(min_delay)
        self.max_delay = max(float(max_delay), float(min_delay))
        self._last: Dict[str, float] = {}

    def wait(self, host: str, minimum: Optional[float] = None) -> None:
        delay = random.uniform(self.min_delay, self.max_delay)
        if minimum:
            delay = max(delay, float(minimum))
        last = self._last.get(host)
        if last is not None:
            remaining = delay - (time.monotonic() - last)
            if remaining > 0:
                time.sleep(remaining)
        self._last[host] = time.monotonic()


def backoff_seconds(attempt: int, base: float, cap: float) -> float:
    """Exponential backoff with jitter: attempt 0 -> ~base, 1 -> ~2*base, 2 -> ~4*base ..."""
    return min(cap, base * (2 ** attempt)) * random.uniform(0.5, 1.0)


def parse_retry_after(value: Optional[str]) -> Optional[float]:
    """Retry-After may be a number of seconds or an HTTP date."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = email.utils.parsedate_to_datetime(value)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError):
        return None


def wait_expression(selector: str, timeout_ms: int) -> Optional[str]:
    """
    Crawl4AI ``wait_for`` condition: the selector exists, or the page has been open long enough.

    ``performance.now()`` counts from navigation, so no state is needed in the page.
    """
    if not selector:
        return None
    return f"js:() => !!document.querySelector({json.dumps(selector)}) || performance.now() > {int(timeout_ms)}"


class _Retry(Exception):
    """Internal: this attempt failed in a way that is worth retrying."""

    def __init__(self, reason: str, retry_after: Optional[float] = None):
        super().__init__(reason)
        self.retry_after = retry_after


# --------------------------------------------------------------------------- #
# Session: one browser for one site run
# --------------------------------------------------------------------------- #
@dataclass
class _ActiveCapture:
    config: ScrapeConfig
    captured: List[CapturedJson] = field(default_factory=list)
    tasks: List[asyncio.Future] = field(default_factory=list)
    denied: str = ""


class CrawlSession:
    """Owns the Crawl4AI browser, the robots.txt cache and the per-host throttle for one site run."""

    def __init__(self, settings: CrawlSettings):
        self.settings = settings
        self.throttle = HostThrottle(settings.min_delay_seconds, settings.max_delay_seconds)
        self.robots = RobotsCache(self._fetch_robots)
        # hosts whose owner gave WRITTEN permission to crawl despite robots.txt: host -> reference
        self.robots_permission: Dict[str, str] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._crawler = None
        self._active: Optional[_ActiveCapture] = None
        self._pages_since_start = 0

    # ------------------------------------------------------------ lifecycle
    def _ensure_loop(self) -> None:
        if self._loop is not None:
            return
        # asyncio.new_event_loop() is a ProactorEventLoop on Windows, which Playwright needs
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name="crawl4ai-loop", daemon=True)
        self._thread.start()

    def _submit(self, coroutine):
        self._ensure_loop()
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop).result()

    async def _start_crawler(self) -> None:
        from crawl4ai import AsyncWebCrawler, BrowserConfig

        options: Dict[str, Any] = {
            "headless": self.settings.headless,
            "verbose": False,
            "text_mode": self.settings.block_heavy_resources,   # no images/media: less load on the site
        }
        if self.settings.user_agent:
            options["user_agent"] = self.settings.user_agent
        self._crawler = AsyncWebCrawler(config=BrowserConfig(**options))
        await self._crawler.start()
        strategy = self._crawler.crawler_strategy
        strategy.set_hook("on_page_context_created", self._hook_page_created)
        strategy.set_hook("before_retrieve_html", self._hook_before_retrieve)
        strategy.set_hook("before_return_html", self._hook_before_return)
        self._pages_since_start = 0
        log.info("headless Chromium started through Crawl4AI")

    async def _stop_crawler(self) -> None:
        if self._crawler is not None:
            crawler, self._crawler = self._crawler, None
            try:
                await crawler.close()
            except Exception:                                  # closing must never hide the real error
                log.debug("error while closing the browser", exc_info=True)

    def close(self) -> None:
        if self._loop is None:
            return
        try:
            self._submit(self._stop_crawler())
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            if self._thread is not None:
                self._thread.join(timeout=10)
            self._loop = self._thread = None

    # ------------------------------------------------------------ Crawl4AI hooks
    async def _hook_page_created(self, page, context=None, **_kwargs):
        # A page can be seen more than once if Crawl4AI reuses it; attach the listener only once.
        if not getattr(page, "_price_capture_attached", False):
            page._price_capture_attached = True
            page.on("response", self._on_response)
        return page

    def _on_response(self, response) -> None:
        active = self._active
        if active is None or not any(fragment in response.url for fragment in active.config.capture_json):
            return
        active.tasks.append(asyncio.ensure_future(self._read_response(response, active)))

    @staticmethod
    async def _read_response(response, active: _ActiveCapture) -> None:
        try:
            body = await response.json()
        except Exception:                                      # not JSON, or the page navigated away
            body = None
        try:
            post = response.request.post_data or ""
        except Exception:
            post = ""
        active.captured.append(CapturedJson(response.url, post, response.status, body))
        if response.status in (401, 403):
            active.denied = f"data request refused with HTTP {response.status}: {response.url}"

    async def _hook_before_retrieve(self, page, context=None, config=None, **_kwargs):
        active = self._active
        if active is not None and active.config.before_html_driver is not None:
            await active.config.before_html_driver(page, active.captured)
        return page

    async def _hook_before_return(self, page, html=None, context=None, config=None, **_kwargs):
        active = self._active
        if active is None:
            return page

        async def wait_for(fragments: Sequence[str], timeout: float) -> None:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if all(any(f in c.url for c in active.captured) for f in fragments):
                    return
                await asyncio.sleep(0.25)

        scrape_config = active.config
        if scrape_config.capture_wait_for:
            await wait_for(scrape_config.capture_wait_for, scrape_config.capture_timeout_seconds)
        if scrape_config.capture_optional:
            await wait_for(scrape_config.capture_optional, scrape_config.capture_optional_timeout_seconds)
        if scrape_config.page_driver is not None:
            await scrape_config.page_driver(page, active.captured)
        if active.tasks:                                       # let in-flight body reads finish
            await asyncio.wait(active.tasks, timeout=10)
        return page

    # ------------------------------------------------------------ one attempt
    def _run_config(self, config: ScrapeConfig):
        from crawl4ai import CacheMode, CrawlerRunConfig, JsonCssExtractionStrategy

        options: Dict[str, Any] = dict(
            cache_mode=CacheMode.BYPASS,                       # prices must always be fresh
            page_timeout=int(self.settings.page_timeout_seconds * 1000),
            wait_until=config.wait_until,
            delay_before_return_html=config.settle_seconds,
            scan_full_page=config.scroll_full_page,
            scroll_delay=config.scroll_delay_seconds,
            max_scroll_steps=config.max_scroll_steps,
            verbose=False,
        )
        expression = wait_expression(config.wait_selector, config.wait_timeout_ms)
        if expression:
            options["wait_for"] = expression
        if config.js_code:
            options["js_code"] = list(config.js_code)
        if config.schema:
            options["extraction_strategy"] = JsonCssExtractionStrategy(config.schema, verbose=False)
        if self.settings.locale:
            options["locale"] = self.settings.locale
        return CrawlerRunConfig(**options)

    async def _crawl(self, url: str, config: ScrapeConfig) -> ScrapeResult:
        limit = self.settings.restart_browser_every_pages
        if self._crawler is not None and limit and self._pages_since_start >= limit:
            log.info("restarting the browser after %d pages to release memory", self._pages_since_start)
            await self._stop_crawler()
        if self._crawler is None:
            await self._start_crawler()
        self._pages_since_start += 1

        self._active = active = _ActiveCapture(config)
        try:
            result = await self._crawler.arun(url, config=self._run_config(config))
        finally:
            self._active = None
        if active.denied:
            raise AccessDenied(active.denied)
        return self._classify(url, config, result, active)

    def _classify(self, url: str, config: ScrapeConfig, result, active: _ActiveCapture) -> ScrapeResult:
        """Turn a Crawl4AI result into a ScrapeResult, or raise the error that describes it."""
        status = int(result.status_code or 0)
        html = result.html or ""
        message = (result.error_message or "").strip()
        blocked = "anti-bot" in message.lower()

        if status in (401, 403):
            raise AccessDenied(f"HTTP {status} for {url}")
        if status in (404, 410):
            raise NotFound(f"HTTP {status} for {url}")
        if status == 429 or 500 <= status < 600:
            if blocked:                                        # e.g. an Akamai 503 block page, not an outage
                raise AccessDenied(f"HTTP {status} block page for {url}: {message}")
            headers = {str(k).lower(): v for k, v in (result.response_headers or {}).items()}
            retry_after = parse_retry_after(headers.get("retry-after"))
            if retry_after is not None and retry_after > self.settings.max_retry_after_seconds:
                raise TemporaryError(f"HTTP {status}, Retry-After {retry_after:.0f}s too long for {url}")
            raise _Retry(f"HTTP {status}", retry_after)
        if not result.success:
            # Crawl4AI's detector misreads short XML documents as block pages
            if not (config.raw_document and status == 200 and html):
                if blocked:
                    raise AccessDenied(f"bot protection for {url}: {message}")
                raise _Retry(message.splitlines()[0] if message else "crawl failed")
        if looks_like_challenge(html) and not config.raw_document:
            raise AccessDenied(f"bot challenge served for {url}")

        final_url = result.redirected_url or url
        if any(path in urlsplit(final_url).path for path in config.not_found_paths):
            raise NotFound(f"{url} redirected to the site's error page")

        items: List[Dict[str, Any]] = []
        if config.schema and result.extracted_content:
            try:
                parsed = json.loads(result.extracted_content)
                items = parsed if isinstance(parsed, list) else []
            except ValueError:
                log.warning("unreadable extraction output for %s", url)
        return ScrapeResult(url=url, final_url=final_url, status=status, html=html, items=items,
                            captured=list(active.captured))

    # ------------------------------------------------------------ politeness
    def _permitted_host(self, url: str) -> bool:
        host = (urlsplit(url).hostname or "").lower()
        return any(host == h or host.endswith("." + h) for h in self.robots_permission)

    def check_allowed(self, url: str) -> None:
        if self._permitted_host(url):
            return                                             # written permission, logged at run start
        if self.settings.respect_robots_txt and not self.robots.can_fetch(self.settings.robots_agent, url):
            raise RobotsDisallowed(f"robots.txt disallows {url}")

    def polite_wait(self, url: str) -> None:
        minimum = None
        if self.settings.respect_robots_txt and not self._permitted_host(url):
            minimum = self.robots.crawl_delay(self.settings.robots_agent, url)
        self.throttle.wait(urlsplit(url).netloc, minimum=minimum)

    def _fetch_robots(self, url: str) -> Tuple[int, str]:
        """robots.txt is loaded through the browser: two of the sites do not answer plain HTTP clients."""
        self.throttle.wait(urlsplit(url).netloc)
        result = self._submit(self._crawl_raw(url))
        log.info("GET %s -> %s (robots)", url, result.status)
        return result.status, BeautifulSoup(result.html, "lxml").get_text("\n")

    async def _crawl_raw(self, url: str) -> ScrapeResult:
        return await self._crawl(url, ScrapeConfig(raw_document=True, settle_seconds=0))

    # ------------------------------------------------------------ public
    def scrape(self, url: str, config: ScrapeConfig) -> ScrapeResult:
        """Load one page: robots check, polite delay, up to ``max_retries`` retries with backoff."""
        self.check_allowed(url)
        retries = self.settings.max_retries
        last_problem = ""
        for attempt in range(retries + 1):
            self.polite_wait(url)
            started = time.monotonic()
            try:
                result = self._submit(self._crawl(url, config))
            except _Retry as problem:
                last_problem = str(problem)
                retry_after = problem.retry_after
            except ScrapeError:
                raise
            except Exception as exc:                           # browser crash, Playwright error
                last_problem = f"{type(exc).__name__}: {str(exc).splitlines()[0] if str(exc) else ''}"
                retry_after = None
                self._submit(self._stop_crawler())             # start from a clean browser
            else:
                log.info("CRAWL %s -> %s (%.1fs, %d items, %d captured)", url, result.status,
                         time.monotonic() - started, len(result.items), len(result.captured))
                return result
            if attempt < retries:
                wait = retry_after if retry_after is not None else backoff_seconds(
                    attempt, self.settings.backoff_base_seconds, self.settings.backoff_max_seconds)
                log.warning("CRAWL %s failed (%s) attempt %d/%d, retrying in %.0fs",
                            url, last_problem, attempt + 1, retries + 1, wait)
                time.sleep(wait)
        log.error("CRAWL %s failed (%s) after %d attempts", url, last_problem, retries + 1)
        raise TemporaryError(f"giving up on {url} after {retries + 1} attempts ({last_problem})")

    def fetch_document(self, url: str) -> str:
        """Load an XML document such as a sitemap and return its text (robots.txt still applies)."""
        result = self.scrape(url, ScrapeConfig(raw_document=True, settle_seconds=0))
        return result.html


def scrape_site(url: str, config: ScrapeConfig, session: Optional[CrawlSession] = None) -> ScrapeResult:
    """
    Load ``url`` with Crawl4AI according to ``config`` and return the extracted data.

    Pass a :class:`CrawlSession` to reuse one browser across many pages, which is what the
    pipeline does. Without one, a temporary session with default settings is used and closed.
    Raises AccessDenied, RobotsDisallowed, NotFound or TemporaryError.
    """
    if session is not None:
        return session.scrape(url, config)
    temporary = CrawlSession(CrawlSettings())
    try:
        return temporary.scrape(url, config)
    finally:
        temporary.close()
