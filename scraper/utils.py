"""
Small, dependency-free helpers shared by every component:

* Arabic/Persian digit normalisation and robust price parsing
* currency detection
* URL validation / canonicalisation
* key/value extraction from free-text specification blocks
* timestamps in the configured timezone
"""
from __future__ import annotations

import html
import re
import unicodedata
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Dict, Iterable, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

# --------------------------------------------------------------------------- #
# Digits & text
# --------------------------------------------------------------------------- #
# Arabic-Indic (U+0660..0669) and Extended/Persian (U+06F0..06F9) digits -> ASCII
_DIGIT_MAP = {ord(c): str(i) for i, c in enumerate("٠١٢٣٤٥٦٧٨٩")}
_DIGIT_MAP.update({ord(c): str(i) for i, c in enumerate("۰۱۲۳۴۵۶۷۸۹")})
# Arabic thousands separator (U+066C) and decimal separator (U+066B)
_DIGIT_MAP[ord("٬")] = ","
_DIGIT_MAP[ord("٫")] = "."

# Invisible direction marks that Magento/Odoo put around currency symbols
_INVISIBLES = dict.fromkeys(map(ord, "‎‏‪‫‬‭‮⁦⁧⁨⁩﻿"), None)


def normalize_digits(text: str) -> str:
    """Convert Arabic/Persian digits and separators to ASCII."""
    return text.translate(_DIGIT_MAP) if text else text


def clean_text(value) -> str:
    """Collapse whitespace, unescape entities, drop direction marks. Never returns None."""
    if value is None:
        return ""
    text = html.unescape(str(value)).translate(_INVISIBLES).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def normalize_arabic_key(text: str) -> str:
    """
    Normalise a specification label so that spelling variants compare equal:
    lower-case, Arabic letter variants unified (أ/إ/آ -> ا, ة -> ه, ى -> ي),
    tatweel and diacritics removed, punctuation collapsed.
    """
    text = clean_text(text).lower()
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"[ً-ْـ]", "", text)          # harakat + tatweel
    text = text.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    text = text.replace("ة", "ه").replace("ى", "ي")
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


# --------------------------------------------------------------------------- #
# Prices & currency
# --------------------------------------------------------------------------- #
_CURRENCY_PATTERNS = [
    ("EGP", re.compile(r"(EGP|E£|LE\b|L\.E|ج\.?\s?م|جنيه|جنية|جنيها)", re.I)),
    ("AED", re.compile(r"(AED|د\.?\s?إ|درهم)", re.I)),
    ("SAR", re.compile(r"(SAR|ر\.?\s?س|ريال)", re.I)),
    ("USD", re.compile(r"(USD|US\$|\$)", re.I)),
]


def detect_currency(text: str, default: Optional[str] = None) -> Optional[str]:
    """Return an ISO-4217 code for the currency mentioned in ``text``."""
    if not text:
        return default
    text = clean_text(text)
    for code, pattern in _CURRENCY_PATTERNS:
        if pattern.search(text):
            return code
    return default


def parse_price(value) -> Optional[Decimal]:
    """
    Parse a price that may be a number, "44,500.000 E£", "٨٧٬٩٩٩٫٠٠ ج.م.", "8890.0" ...
    Returns a Decimal or None when no valid positive number is found.
    Prices are compared numerically everywhere, never as strings.
    """
    if value is None:
        return None
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        try:
            number = Decimal(str(value))
        except InvalidOperation:
            return None
        return number if number.is_finite() and number > 0 else None

    text = normalize_digits(clean_text(value))
    match = re.search(r"\d[\d,.\s]*", text)
    if not match:
        return None
    raw = match.group(0).replace(" ", "").rstrip(".,")

    if "," in raw and "." in raw:
        # whichever separator comes last is the decimal separator
        if raw.rfind(",") > raw.rfind("."):
            raw = raw.replace(".", "").replace(",", ".")
        else:
            raw = raw.replace(",", "")
    elif "," in raw:
        # "44,500" -> thousands ; "12,5" -> decimal comma
        raw = raw.replace(",", "") if re.fullmatch(r"\d{1,3}(,\d{3})+", raw) else raw.replace(",", ".")
    try:
        number = Decimal(raw)
    except InvalidOperation:
        return None
    return number if number.is_finite() and number > 0 else None


def format_decimal(value: Optional[Decimal]) -> str:
    """Stable textual form for storage: 87999.00 -> '87999.00'."""
    if value is None:
        return ""
    return f"{value.quantize(Decimal('0.01'))}"


# --------------------------------------------------------------------------- #
# URLs
# --------------------------------------------------------------------------- #
TRACKING_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
                   "gclid", "fbclid", "___store", "___from_store"}


class InvalidURLError(ValueError):
    pass


def validate_url(url: str, allowed_hosts: Iterable[str]) -> str:
    """
    Validate an absolute http(s) URL whose host belongs to ``allowed_hosts``
    (exact host or a sub-domain of it). Returns the URL without fragment.
    """
    if not url or not isinstance(url, str):
        raise InvalidURLError("empty URL")
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https"):
        raise InvalidURLError(f"unsupported scheme in {url!r}")
    host = (parts.hostname or "").lower()
    if not host:
        raise InvalidURLError(f"missing host in {url!r}")
    allowed = [h.lower().lstrip(".") for h in allowed_hosts]
    if not any(host == h or host.endswith("." + h) for h in allowed):
        raise InvalidURLError(f"host {host!r} is not one of {allowed}")
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", parts.query, ""))


def strip_query_params(url: str, drop: Iterable[str] = (), keep_only: Optional[Iterable[str]] = None) -> str:
    """Remove tracking and selected query parameters (used for canonical product URLs)."""
    parts = urlsplit(url)
    drop_set = set(drop) | TRACKING_PARAMS
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    if keep_only is not None:
        keep = set(keep_only)
        pairs = [(k, v) for k, v in pairs if k in keep]
    else:
        pairs = [(k, v) for k, v in pairs if k not in drop_set]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(pairs), ""))


def set_query_param(url: str, key: str, value) -> str:
    parts = urlsplit(url)
    pairs = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != key]
    pairs.append((key, str(value)))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(pairs), parts.fragment))


# --------------------------------------------------------------------------- #
# Specifications in free text
# --------------------------------------------------------------------------- #
_KV_SPLIT = re.compile(r"\s*(?:\t+|:|：)\s*")


def extract_key_values(text: str, max_key_len: int = 45, max_value_len: int = 300) -> Dict[str, str]:
    """
    Pull "Key: Value" / "Key<TAB>Value" pairs out of a block of text (one pair per line).
    Lines that do not look like a pair are ignored. First occurrence of a key wins.
    """
    result: Dict[str, str] = {}
    if not text:
        return result
    for line in re.split(r"[\r\n]+", text):
        # split BEFORE collapsing whitespace, otherwise tab separators are lost
        line = html.unescape(line).translate(_INVISIBLES).replace("\xa0", " ").strip(" -•*·")
        if not line:
            continue
        pieces = _KV_SPLIT.split(line, maxsplit=1)
        if len(pieces) != 2:
            continue
        key, value = clean_text(pieces[0]), clean_text(pieces[1])
        if not key or not value or len(key) > max_key_len or len(value) > max_value_len:
            continue
        if re.fullmatch(r"https?|www", key, re.I):       # "https://..." is not a spec
            continue
        result.setdefault(key, value)
    return result


# --------------------------------------------------------------------------- #
# Model code (manufacturer SKU) from a product name
# --------------------------------------------------------------------------- #
# words that contain letters+digits but are specifications, not model codes
_NOT_A_MODEL = re.compile(
    r"^([\d.]+-?(GB|TB|MB|MP|HZ|MAH|W|K|KG|L|INCH|IN|CM|MM|HP|V|G|P|FPS)|[A-Z]?\d{1,2}|WEBOS\d+|ANDROID\d+|IOS\d+|"
    r"WI-?FI\d*|USB\d*|HDMI\d*|DVB\w*|HDR\d+|DDR\d+|LPDDR\w*|GEN\d+|AI\d*|[1-9]X\d+)$", re.I)
_MODEL_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-./]*[A-Za-z0-9]")


def model_code_from_name(name: str) -> str:
    """
    Extract the manufacturer model code from a product name:
      "شاشه ال جى 43بوصة سمارت 43LM6370PVA"              -> "43LM6370PVA"
      "شاشه جاك 65بوصة جوجل برسيفر NGLD65JE8135F-K4"    -> "NGLD65JE8135F-K4"
      "شاشه فريش 43بوصه سمارت جوجل 43MF435G كود 20977"  -> "43MF435G"
    A model code is a Latin token of 5+ characters mixing letters and digits. When several
    exist, the last one wins (model codes are written at the end of these names).
    Returns "" when nothing looks like a model code (e.g. "Apple iPhone 16 - 128GB").
    """
    candidates = []
    for token in _MODEL_TOKEN.findall(normalize_digits(name or "")):
        token = token.strip("-./")
        if (len(token) >= 5 and re.search(r"[A-Za-z]", token) and re.search(r"\d", token)
                and not _NOT_A_MODEL.match(token) and "/" not in token):
            candidates.append(token.upper())
    return candidates[-1] if candidates else ""


# --------------------------------------------------------------------------- #
# Time
# --------------------------------------------------------------------------- #
def now_in_tz(tz_name: str) -> datetime:
    return datetime.now(ZoneInfo(tz_name))


def iso_now(tz_name: str) -> str:
    return now_in_tz(tz_name).isoformat(timespec="seconds")
