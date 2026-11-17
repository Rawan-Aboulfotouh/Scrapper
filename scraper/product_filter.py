"""
Product-type filter: keeps real TVs in "TVs", real phones in "Mobiles" and real
tablets in "Tablets", and rejects accessories (remotes, stands, mounts, cables,
receivers, TV boxes, soundbars / cases, screen protectors, chargers, earbuds,
power banks, watches, keyboards, styluses ...) that stores often list in the
same categories.

A product is EXCLUDED when any rule matches, checked in this order:

 1. prefix      - the name STARTS with an accessory word: "حامل تليفزيون ...", "جراب ايفون ..."
 2. always      - the name contains a word/phrase that never appears in a real TV/phone
                  name, anywhere: "tv box", "متوافق مع", "للشاشات", "for iphone", "جراب", "power bank" ...
 3. conditional - the name contains an ambiguous word ("ريموت", "حامل", "شاحن", "سماعة" ...)
                  AND there is no STRONG sign of a real product. Strong signs:
                    TVs     : a screen size of 19-120 inches (name or specifications)
                    Mobiles : storage/RAM such as "128GB" (name) or RAM/storage in the specs
                    Tablets : storage/RAM as for Mobiles, or a screen size of 7-20 inches
                  -> "شاشه ال جى 65بوصه ... ريموت ماجيك" is kept (65 inch TV with a Magic Remote)
                  -> "شاحن سامسونج 25 وات" is excluded
 4. no signal   - nothing at all identifies a TV / phone (no strong sign, no TV/phone word,
                  no phone brand, no SIM/network/battery specs)
 5. min price   - the price is below the category minimum (checked later, after the price
                  is known): a "TV" for 450 EGP is an accessory.

Rules 1 and 2 need only the name, so they are applied BEFORE visiting the product page
(fewer requests). Every list can be extended in config.yaml (filters.product_rules).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Dict, Iterable, List, Optional, Tuple

from .specs import canonical_for
from .utils import normalize_arabic_key, normalize_digits

# Arabic words are often written with an attached prefix: "بحامل", "والشاحن", "للشاشات"
_AR_PREFIX = r"(?:و?(?:بال|لل|ال|ب|ل|ك)?)"

DEFAULT_RULES: Dict[str, Dict] = {
    "TVs": {
        "accessory_prefixes": [
            "حامل", "ستاند", "ريموت", "كابل", "وصله", "ريسيفر", "رسيفر", "انتينا", "دش", "محول",
            "ساوند بار", "مسرح منزلي", "بوكس", "شاشه حمايه", "منظم كهرباء", "مثبت",
            "tv wall", "tv stand", "tv mount", "tv bracket", "tv remote", "tv cabinet", "tv unit",
            "mount", "stand", "bracket", "remote", "cable", "receiver", "antenna", "soundbar",
            "sound bar", "home theater", "tv box", "box", "wall", "universal", "adapter",
            "hdmi", "converter", "dongle", "stick", "voltage", "stabilizer",
        ],
        "always_exclude": [
            "tv box", "android box", "smart box", "تي في بوكس", "اندرويد بوكس", "fire stick", "tv stick",
            "soundbar", "sound bar", "ساوند بار", "home theater", "هوم ثياتر", "مسرح منزلي",
            "wall mount", "tv mount", "tv stand", "tv bracket", "حامل حائط للشاشات",
            "remote control", "ريموت كنترول", "universal remote", "ريموت يونيفرسال",
            "hdmi cable", "كابل hdmi", "antenna", "انتينا",
            "for tv", "for tvs", "for smart tv", "compatible", "متوافق",
            "شاشات", "تليفزيونات", "تلفزيونات",      # plural "for screens/TVs" (with ل prefix below)
            "projector", "بروجيكتور", "داتا شو", "monitor", "مونيتور", "شاشه كمبيوتر",
        ],
        "conditional_exclude": [
            "حامل", "ستاند", "stand", "mount", "bracket", "ريموت", "remote", "كابل", "cable",
            "ريسيفر", "رسيفر", "receiver", "دش",
        ],
        "min_price": 3000,
    },
    "Mobiles": {
        "accessory_prefixes": [
            "جراب", "كفر", "اسكرينه", "سكرينه", "شاحن", "كابل", "سماعه", "سماعات", "ايربودز",
            "باور بانك", "ميموري", "كارت ميموري", "حامل", "ستاند", "ساعه", "عدسه", "لزقه", "وصله", "محول",
            "case", "cover", "charger", "cable", "adapter", "earbuds", "earphones", "earphone",
            "headphones", "headphone", "headset", "airpods", "buds", "power bank", "powerbank",
            "memory", "microsd", "screen protector", "protector", "glass", "tempered", "holder",
            "stand", "tripod", "selfie", "smart watch", "smartwatch", "watch", "band", "strap",
            "lens", "ring light", "car", "wireless charger", "magsafe", "speaker", "سبيكر",
        ],
        "always_exclude": [
            "جراب", "كفر", "اسكرينه", "سكرينه", "جلاس", "لزقه", "باور بانك", "power bank", "powerbank",
            "screen protector", "tempered glass", "microsd", "micro sd", "memory card", "ميموري",
            "smart watch", "smartwatch", "watch", "ساعه ذكيه", "ساعه سمارت", "smart band", "fit band",
            "سمارت باند", "selfie stick", "tripod", "ترايبود", "car holder", "car mount",
            "tablet", "tab", "تابلت", "ipad", "ايباد",
            "for iphone", "for samsung", "for xiaomi", "for redmi", "for oppo", "for realme",
            "for infinix", "for honor", "for huawei", "for vivo", "for tecno", "compatible", "متوافق",
            "موبايلات", "هواتف", "case", "cover",
        ],
        "conditional_exclude": [
            "شاحن", "charger", "كابل", "cable", "سماعه", "سماعات", "headphones", "earphones",
            "headset", "earbuds", "airpods", "ايربودز", "buds", "adapter", "محول", "حامل",
            "holder", "stand", "ستاند",
        ],
        "min_price": 300,
    },
    "Tablets": {
        "accessory_prefixes": [
            "جراب", "كفر", "اسكرينه", "سكرينه", "شاحن", "كابل", "قلم", "كيبورد", "لوحه مفاتيح", "حامل", "ستاند",
            "لزقه", "وصله", "محول", "ميموري", "كارت ميموري", "case", "cover", "folio", "sleeve", "pouch", "charger",
            "cable", "adapter", "keyboard", "stylus", "pencil", "pen", "screen protector", "protector", "glass",
            "tempered", "holder", "stand", "mount", "memory", "microsd", "hub", "dock",
        ],
        "always_exclude": [
            "جراب", "كفر", "اسكرينه", "سكرينه", "جلاس", "لزقه", "screen protector", "tempered glass", "folio",
            "keyboard case", "smart cover", "smart folio", "stylus", "apple pencil", "microsd", "micro sd",
            "memory card", "ميموري", "for ipad", "for tablet", "for galaxy tab", "for samsung", "for huawei",
            "for xiaomi", "for lenovo", "compatible", "متوافق", "case", "cover", "smart watch", "smartwatch",
        ],
        "conditional_exclude": [
            "شاحن", "charger", "كابل", "cable", "كيبورد", "keyboard", "قلم", "pen", "adapter", "محول",
            "حامل", "holder", "stand", "ستاند", "mount", "hub", "dock",
        ],
        "min_price": 1000,
    },
}

# --------------------------------------------------------------------------- signals
_TV_WORDS = ["شاشه", "تليفزيون", "تلفزيون", "تلفاز", "tv", "television", "smart tv", "led tv",
             "oled", "qled", "qned", "nanocell"]
_PHONE_WORDS = ["موبايل", "هاتف", "تليفون", "جوال", "phone", "smartphone", "mobile", "iphone",
                "galaxy", "redmi", "poco", "pixel", "sim", "dual sim", "شريحه", "شريحتين"]
_PHONE_BRANDS = ["samsung", "سامسونج", "apple", "ابل", "xiaomi", "شاومي", "oppo", "اوبو", "realme",
                 "ريلمي", "vivo", "فيفو", "infinix", "انفينكس", "tecno", "تكنو", "itel", "ايتل", "nokia",
                 "نوكيا", "honor", "هونر", "huawei", "هواوي", "motorola", "موتورولا", "oneplus", "zte",
                 "doogee", "دوجي", "blackview", "ulefone", "oukitel", "ace", "lava", "alcatel", "sony",
                 "nothing", "google", "hmd", "cubot", "umidigi", "tcl"]
_TABLET_WORDS = ["تابلت", "تاب", "ايباد", "tablet", "tab", "ipad", "pad", "matepad", "matepad se", "megapad",
                 "galaxy tab", "redmi pad", "xiaoxin", "yoga tab", "ideatab", "t-pad"]
_TABLET_BRANDS = ["samsung", "سامسونج", "apple", "ابل", "lenovo", "لينوفو", "huawei", "هواوي", "xiaomi", "شاومي",
                  "honor", "هونر", "oppo", "اوبو", "realme", "ريلمي", "infinix", "انفينكس", "tecno", "تكنو",
                  "itel", "ايتل", "alcatel", "tcl", "nokia", "نوكيا", "amazon", "teclast", "doogee", "blackview",
                  "oukitel", "acer", "asus", "microsoft", "surface", "dell", "hp"]
_CATEGORY_NOUN = {"TVs": "TV", "Mobiles": "phone", "Tablets": "tablet"}

_SIZE_IN_TEXT = re.compile(r"(?<![\d.])(\d{2,3})(?:\.\d)?\s*(?:-\s*)?(?:بوصه|بوصة|انش|إنش|inch|inches|in\b|\"|”|″|'')",
                           re.I)
_STORAGE_IN_TEXT = re.compile(r"(?<![\d.])\d{1,4}\s*(?:gb|tb|جيجا|جيجابايت|تيرا)\b", re.I)
# The same signals on normalize_arabic_key() text, where punctuation has become spaces.
_STORAGE_IN_KEY = re.compile(r"(?:^|\s)\d{1,4}\s*(?:gb|tb|جيجا|جيجابايت|تيرا)(?=\s|$)")
_SLASH_MEMORY_IN_KEY = re.compile(r"(?:^|\s)\d{1,2}\s+\d{2,4}(?=\s|$)")          # "(8/256)" becomes "8 256"
_SIZE_IN_KEY = re.compile(r"(?:^|\s)\d{2,3}\s*(?:بوصه|انش|inch|inches|in)(?=\s|$)")
_ADDON_CONNECTOR = re.compile(r"(?:^|\s)(?:with|free|gift|مع|هديه|plusmark)\s")
# A memory figure written as "(12/512)" or "(12+12/512)" is a real phone or tablet signal
_SLASH_MEMORY_RAW = re.compile(r"(?<![\d.])\d{1,2}(?:\s*\+\s*\d{1,2})?\s*/\s*\d{2,4}(?![\d.])")
_DEVICE_WORDS = {"TVs": _TV_WORDS, "Mobiles": _PHONE_WORDS + _PHONE_BRANDS, "Tablets": _TABLET_WORDS + _TABLET_BRANDS}


def _key(name: str) -> str:
    """Normalised name where a '+' (a bundle marker) survives as the word 'plusmark'."""
    return normalize_arabic_key(normalize_digits((name or "").replace("+", " plusmark ")))


def _first_position(key: str, words) -> Optional[int]:
    found = []
    for word in words:
        match = re.search(rf"(?:^|\s){_AR_PREFIX}{re.escape(normalize_arabic_key(word))}(?=\s|$)", key)
        if match:
            found.append(match.start())
    return min(found) if found else None


def _norm_list(words: Iterable[str]) -> List[str]:
    out = []
    for word in words or []:
        key = normalize_arabic_key(word)
        if key and key not in out:
            out.append(key)
    return out


def _contains(text: str, word: str) -> bool:
    """Whole-word / whole-phrase match, allowing attached Arabic prefixes (و ب ل ال لل ...)."""
    return re.search(rf"(?:^|\s){_AR_PREFIX}{re.escape(word)}(?:\s|$)", text) is not None


def _starts_with(text: str, word: str) -> bool:
    return re.match(rf"(?:ال)?{re.escape(word)}(?:\s|$)", text) is not None


def _screen_inches(text: str) -> List[int]:
    return [int(m.group(1)) for m in _SIZE_IN_TEXT.finditer(normalize_digits(text or "").lower())]


@dataclass
class Rules:
    prefixes: List[str]
    always: List[str]
    conditional: List[str]
    min_price: Decimal
    user_always: List[str]             # added in config.yaml: applied without the "sold with extras" allowance


class ProductTypeFilter:
    def __init__(self, filters_cfg: Optional[Dict] = None):
        filters_cfg = filters_cfg or {}
        configured = filters_cfg.get("product_rules") or {}
        legacy_prefixes = filters_cfg.get("exclude_name_prefixes") or {}   # older config.yaml files
        self.rules: Dict[str, Rules] = {}
        for category, defaults in DEFAULT_RULES.items():
            extra = configured.get(category) or {}
            self.rules[category] = Rules(
                prefixes=_norm_list(list(defaults["accessory_prefixes"]) + list(extra.get("accessory_prefixes") or [])
                                    + list(legacy_prefixes.get(category) or [])),
                always=_norm_list(list(defaults["always_exclude"]) + list(extra.get("always_exclude") or [])),
                conditional=_norm_list(list(defaults["conditional_exclude"]) + list(extra.get("conditional_exclude") or [])),
                min_price=Decimal(str(extra.get("min_price", defaults["min_price"]))),
                user_always=_norm_list(extra.get("always_exclude") or []),
            )
            # words the user explicitly wants to keep (e.g. a brand that looks like an accessory word)
            for word in _norm_list(extra.get("never_exclude") or []):
                for bucket in (self.rules[category].prefixes, self.rules[category].always,
                               self.rules[category].conditional):
                    if word in bucket:
                        bucket.remove(word)

    # ------------------------------------------------------------------ signals
    @staticmethod
    def _strong_signal(category: str, name: str, specs: Dict[str, str]) -> bool:
        if category == "TVs":
            sizes = _screen_inches(name)
            key = normalize_arabic_key(normalize_digits(name or ""))
            # "شاشه فريش 43 سمارت": a TV word plus a bare number is the screen size
            if any(_contains(key, normalize_arabic_key(w)) for w in _TV_WORDS):
                sizes += [int(n) for n in re.findall(r"(?:^|\s)(\d{2,3})(?=\s|$)", key)]
            for label, value in specs.items():
                if canonical_for(label) == "screen_size":
                    sizes += _screen_inches(value) or [int(n) for n in re.findall(r"\d{2,3}", normalize_digits(value))]
            return any(19 <= s <= 120 for s in sizes)
        if category in ("Mobiles", "Tablets"):
            if _STORAGE_IN_TEXT.search(normalize_digits(name or "")) or _SLASH_MEMORY_RAW.search(normalize_digits(name or "")):
                return True
            if any(canonical_for(label) in ("ram", "storage") and value for label, value in specs.items()):
                return True
            if category == "Tablets":
                sizes = _screen_inches(name)
                for label, value in specs.items():
                    if canonical_for(label) == "screen_size":
                        sizes += _screen_inches(value) or [
                            int(n) for n in re.findall(r"\d{1,2}", normalize_digits(value))]
                return any(7 <= s <= 20 for s in sizes)
            return False
        return True

    @staticmethod
    def _weak_signal(category: str, key: str, specs: Dict[str, str]) -> bool:
        if category == "TVs":
            return any(_contains(key, normalize_arabic_key(w)) for w in _TV_WORDS)
        if category == "Mobiles":
            words = _PHONE_WORDS + _PHONE_BRANDS
            if any(_contains(key, normalize_arabic_key(w)) for w in words):
                return True
            return any(canonical_for(label) in ("sim", "network", "battery", "operating_system", "main_camera")
                       for label in specs)
        if category == "Tablets":
            words = _TABLET_WORDS + _TABLET_BRANDS
            if any(_contains(key, normalize_arabic_key(w)) for w in words):
                return True
            return any(canonical_for(label) in ("operating_system", "battery", "processor") for label in specs)
        return True

    # ------------------------------------------------------------------ public
    @staticmethod
    def _device_position(category: str, key: str, name: str) -> Optional[int]:
        """Where the name first identifies a real device, or None when it never does."""
        if category == "TVs":
            sizes = [m.start() for m in _SIZE_IN_KEY.finditer(key)]
            words = [m.start() for w in _TV_WORDS
                     for m in re.finditer(rf"(?:^|\s){_AR_PREFIX}{re.escape(normalize_arabic_key(w))}(?=\s|$)", key)]
            # the raw name is checked too: 55" loses its quote mark in the normalised key
            has_size = bool(sizes) or any(19 <= s <= 120 for s in _screen_inches(name))
            return min(sizes + words) if has_size and words else None
        if category in ("Mobiles", "Tablets"):
            found = [m.start() for m in _STORAGE_IN_KEY.finditer(key)]
            found += [m.start() for m in _SLASH_MEMORY_IN_KEY.finditer(key)]
            return min(found) if found else None
        return None

    @staticmethod
    def _is_addon(key: str, word: str, device_at: Optional[int]) -> bool:
        """
        A real device sold with extras mentions the accessory after the device, or after "with":
        "Infinix Hot 60 16GB 256GB with Free Gift: Phone case", "Lenovo Tab One With Clear Case, 128GB".
        An accessory names itself first: "Case for iPhone 15 128GB".
        """
        if device_at is None:
            return False
        match = re.search(rf"(?:^|\s){_AR_PREFIX}{re.escape(word)}(?:\s|$)", key)
        if match is None:
            return False
        return match.start() > device_at or _ADDON_CONNECTOR.search(key[:match.start() + 1]) is not None

    @staticmethod
    def _device_anchor(category: str, key: str, name: str) -> Optional[int]:
        """Where the device is first named: its brand, a phone/tablet/TV word, a size or a memory figure."""
        found = [p for p in (ProductTypeFilter._device_position(category, key, name),
                             _first_position(key, _DEVICE_WORDS.get(category, []))) if p is not None]
        return min(found) if found else None

    @staticmethod
    def _is_bundled(category: str, key: str, word: str, name: str) -> bool:
        """
        An accessory that is part of a device listing: "VIVO V50 5G + Anker R50i Buds Bundle",
        "HONOR X9C Smart + Earbuds X6". It must come after the device and after a "+", "with" or "free".
        "Samsung Galaxy Buds2 Pro" has no such connector, so it stays excluded.
        """
        anchor = ProductTypeFilter._device_anchor(category, key, name)
        match = re.search(rf"(?:^|\s){_AR_PREFIX}{re.escape(word)}(?:\s|$)", key)
        if anchor is None or match is None or match.start() <= anchor:
            return False
        return _ADDON_CONNECTOR.search(key[:match.start() + 1]) is not None

    def check_name(self, name: str, category: str) -> Tuple[bool, str]:
        """Rules 1-2 (name only). Safe to apply before downloading the product page."""
        rules = self.rules.get(category)
        if rules is None:
            return False, ""
        key = _key(name)
        for word in rules.prefixes:
            if _starts_with(key, word):
                return True, f"accessory: name starts with '{word}'"
        device_at = self._device_position(category, key, name)
        for word in rules.always:
            if _contains(key, word) and (word in rules.user_always or not self._is_addon(key, word, device_at)):
                return True, f"accessory: name contains '{word}'"
        return False, ""

    def check_product(self, name: str, category: str, specs: Dict[str, str]) -> Tuple[bool, str]:
        """Rules 1-4 (name + specifications)."""
        excluded, reason = self.check_name(name, category)
        if excluded or category not in self.rules:
            return excluded, reason
        key = _key(name)
        strong = self._strong_signal(category, name, specs)
        for word in self.rules[category].conditional:
            if _contains(key, word) and not strong and not self._is_bundled(category, key, word, name):
                what = "screen size" if category == "TVs" else "storage/RAM"
                return True, f"accessory: name contains '{word}' and no {what} was found"
        if not strong and not self._weak_signal(category, key, specs):
            return True, f"not recognised as a {_CATEGORY_NOUN.get(category, category)}"
        return False, ""

    def check_price(self, category: str, price: Optional[Decimal]) -> Tuple[bool, str]:
        """Rule 5: applied once the lowest price is known."""
        rules = self.rules.get(category)
        if rules is None or price is None or rules.min_price <= 0:
            return False, ""
        if price < rules.min_price:
            return True, f"price {price} below the minimum {rules.min_price} for {category}"
        return False, ""
