"""Exceptions raised by the crawl layer and handled by the pipeline."""
from __future__ import annotations


class ScrapeError(Exception):
    """Base class for page-level failures."""


class AccessDenied(ScrapeError):
    """The site refused access (401/403, CAPTCHA, bot challenge). Stop this site; never retried or bypassed."""


class RobotsDisallowed(ScrapeError):
    """robots.txt forbids this URL. Skip it."""


class NotFound(ScrapeError):
    """404 / 410, or the site redirected to its error page: the page no longer exists."""


class TemporaryError(ScrapeError):
    """Retries exhausted for a transient failure (429, 5xx, timeout, browser error)."""
