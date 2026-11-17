"""
robots.txt support (RFC 9309) including the ``*`` and ``$`` wildcards that
Python's urllib.robotparser does not understand (Elsindbad's robots.txt uses them).

Rules:
* the most specific user-agent group wins (exact token match, else ``*``)
* the longest matching rule wins; on equal length ``Allow`` wins
* robots.txt 4xx  -> everything allowed
* robots.txt 5xx / unreachable -> everything disallowed (we do not crawl)
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from urllib.parse import unquote, urlsplit

log = logging.getLogger("scraper.robots")


@dataclass
class _Group:
    agents: List[str] = field(default_factory=list)
    rules: List[Tuple[bool, str]] = field(default_factory=list)   # (allow?, pattern)
    crawl_delay: Optional[float] = None


class RobotsRules:
    def __init__(self, text: str = "", allow_all: bool = False, disallow_all: bool = False):
        self.allow_all = allow_all
        self.disallow_all = disallow_all
        self.groups: List[_Group] = []
        self.sitemaps: List[str] = []
        if text:
            self._parse(text)

    # ------------------------------------------------------------------ parse
    def _parse(self, text: str) -> None:
        current: Optional[_Group] = None
        last_was_agent = False
        for raw_line in text.splitlines():
            line = raw_line.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            field_name, value = (part.strip() for part in line.split(":", 1))
            field_name = field_name.lower()
            if field_name == "user-agent":
                if current is None or not last_was_agent:
                    current = _Group()
                    self.groups.append(current)
                current.agents.append(value.lower())
                last_was_agent = True
                continue
            last_was_agent = False
            if field_name == "sitemap":
                self.sitemaps.append(value)
            elif current is None:
                continue
            elif field_name in ("allow", "disallow"):
                if value:                      # "Disallow:" (empty) means allow everything
                    current.rules.append((field_name == "allow", value))
            elif field_name == "crawl-delay":
                try:
                    current.crawl_delay = float(value)
                except ValueError:
                    pass

    def _group_for(self, user_agent: str) -> Optional[_Group]:
        token = user_agent.split("/", 1)[0].strip().lower()
        specific = [g for g in self.groups if any(a != "*" and a in token for a in g.agents)]
        if specific:
            return specific[0]
        star = [g for g in self.groups if "*" in g.agents]
        return star[0] if star else None

    # ------------------------------------------------------------------ match
    @staticmethod
    def _pattern_to_regex(pattern: str) -> re.Pattern:
        anchored = pattern.endswith("$")
        body = pattern[:-1] if anchored else pattern
        regex = "".join(".*" if ch == "*" else re.escape(ch) for ch in body)
        return re.compile("^" + regex + ("$" if anchored else ""))

    def can_fetch(self, user_agent: str, url: str) -> bool:
        if self.disallow_all:
            return False
        if self.allow_all:
            return True
        group = self._group_for(user_agent)
        if group is None or not group.rules:
            return True
        parts = urlsplit(url)
        target = unquote(parts.path or "/") + (("?" + unquote(parts.query)) if parts.query else "")
        best_len, best_allow = -1, True
        for allow, pattern in group.rules:
            if self._pattern_to_regex(unquote(pattern)).match(target):
                length = len(pattern)
                if length > best_len or (length == best_len and allow):
                    best_len, best_allow = length, allow
        return best_allow

    def crawl_delay(self, user_agent: str) -> Optional[float]:
        group = self._group_for(user_agent)
        return group.crawl_delay if group else None


class RobotsCache:
    """Fetches and caches robots.txt once per host per run."""

    def __init__(self, fetch_func):
        """``fetch_func(url) -> (status_code, text)``; raises on network failure."""
        self._fetch = fetch_func
        self._cache: Dict[str, RobotsRules] = {}

    def rules_for(self, url: str) -> RobotsRules:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._cache:
            robots_url = origin + "/robots.txt"
            try:
                status, text = self._fetch(robots_url)
            except Exception as exc:                      # unreachable -> do not crawl
                log.error("robots.txt unreachable for %s (%s) - treating site as disallowed", origin, exc)
                self._cache[origin] = RobotsRules(disallow_all=True)
                return self._cache[origin]
            if 200 <= status < 300:
                rules = RobotsRules(text)
                log.info("robots.txt loaded for %s (%d groups)", origin, len(rules.groups))
            elif 400 <= status < 500:
                rules = RobotsRules(allow_all=True)
                log.info("robots.txt returned %s for %s - no restrictions", status, origin)
            else:
                rules = RobotsRules(disallow_all=True)
                log.error("robots.txt returned %s for %s - treating site as disallowed", status, origin)
            self._cache[origin] = rules
        return self._cache[origin]

    def can_fetch(self, user_agent: str, url: str) -> bool:
        return self.rules_for(url).can_fetch(user_agent, url)

    def crawl_delay(self, user_agent: str, url: str) -> Optional[float]:
        return self.rules_for(url).crawl_delay(user_agent)
