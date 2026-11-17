"""
Specification normaliser.

Sites label the same specification in different ways ("مقاس الشاشه", "Display Size",
"الحجم" ...). This module maps known labels onto stable canonical columns and keeps
everything else in an "other specifications" dictionary so nothing is lost.
"""
from __future__ import annotations

import json
from typing import Dict, Tuple

from .utils import clean_text, normalize_arabic_key, normalize_digits

# canonical column name -> label variants (any language). Matching is done on
# normalize_arabic_key() of both sides, so spelling variants need not be listed.
CANONICAL_SPECS: Dict[str, Tuple[str, ...]] = {
    "brand": ("brand", "manufacturer", "الماركة", "الماركات", "ماركة", "الماركه", "العلامة التجارية", "البراند"),
    "model": ("model", "model name", "model number", "item model number", "الموديل", "موديل", "رقم الموديل", "اسم الموديل"),
    "storage": ("storage", "storage capacity", "memory internal", "internal memory", "internal storage",
                "internal storage capacity",
                "rom", "memory storage capacity", "hard disk size", "الذاكرة الداخلية", "سعة التخزين", "المساحة", "مساحة التخزين", "الذاكره الداخليه"),
    "ram": ("ram", "memory ram", "ram memory installed size", "ram size", "internal ram in gb", "رام", "الرام", "ذاكرة الوصول العشوائي", "الذاكرة العشوائية"),
    "screen_size": ("screen size", "display size", "display diagonal", "مقاس الشاشة", "مقاس الشاشه", "حجم الشاشة",
                    "الحجم", "المقاس", "size inch", "screen size inch"),
    "resolution": ("resolution", "display resolution", "screen resolution", "format", "دقة الشاشة", "دقة الشاشه",
                   "الدقة", "دقه الشاشه", "دقة العرض", "درجة الوضوح"),
    "display_technology": ("display type", "display technology", "screen type", "panel type", "display type 1", "نوع الشاشة",
                           "نوع الشاشه", "تقنية الشاشة", "تقنية العرض", "نوع العرض"),
    "operating_system": ("operating system", "operating system type", "operating system installed", "os",
                         "smartphone os", "نظام التشغيل",
                         "نظام التشغيل الذكي"),
    "processor": ("processor", "processor chipset", "processor model", "chipset", "cpu", "cpu model", "processor cpu",
                  "المعالج", "البروسيسور"),
    "dimensions": ("dimensions", "الابعاد", "الأبعاد", "ابعاد المنتج", "product dimensions",
                   "item dimensions", "item dimensions l x w x h", "package dimensions"),
    "color": ("color", "colour", "اللون"),
    "battery": ("battery", "battery capacity", "battery size", "type mah", "البطارية", "سعة البطارية"),
    "network": ("network", "الشبكة", "network type", "network tech", "cellular technology"),
    "refresh_rate": ("refresh rate", "refresh rates", "screen refresh rate", "معدل التحديث", "معدل تحديث الشاشة"),
    "smart_tv": ("smart", "smart tv", "smart function", "سمارت"),
    "built_in_receiver": ("built in receiver", "receiver", "ريسيفر داخلي", "رسيفر داخلي", "ريسيفر"),
    "hdmi_ports": ("hdmi", "hdmi ports", "number of hdmi ports", "hdmi ports quantity", "منافذ hdmi", "عدد منافذ hdmi"),
    "usb_ports": ("usb", "usb ports", "number of usb ports", "usb 2 0 ports quantity", "منافذ usb", "عدد منافذ usb"),
    "main_camera": ("main camera", "main camera resolution", "main camera resolution numeric",
                    "primary camera resolution", "rear camera", "الكاميرا الخلفية"),
    "front_camera": ("front camera", "selfie camera", "secondary camera resolution", "الكاميرا الامامية",
                     "الكاميرا الأمامية"),
    "sim": ("number of sim", "sim", "sim type", "sim card type", "sim card capability", "عدد الشرائح", "الشريحة"),
    "warranty": ("warranty", "الضمان", "مدة الضمان"),
    "country_of_origin": ("country of origin", "country of manufacture", "بلد الصنع", "بلد المنشأ"),
}

CANONICAL_COLUMNS = list(CANONICAL_SPECS.keys())

_LOOKUP: Dict[str, str] = {}
for _column, _labels in CANONICAL_SPECS.items():
    for _label in _labels:
        _LOOKUP.setdefault(normalize_arabic_key(_label), _column)


def canonical_for(label: str) -> str:
    """Return the canonical column for a raw label, or '' when unknown."""
    return _LOOKUP.get(normalize_arabic_key(label), "")


def normalize_specs(raw_specs: Dict[str, str]) -> Tuple[Dict[str, str], Dict[str, str]]:
    """
    Split raw specifications into (canonical, other).
    ``raw_specs`` must be ordered by source priority; the first non-empty value wins.
    """
    canonical: Dict[str, str] = {}
    other: Dict[str, str] = {}
    for label, value in raw_specs.items():
        # "٦٥ بوصة" -> "65 بوصة" so the columns can be filtered/sorted
        label, value = clean_text(label), normalize_digits(clean_text(value))
        if not label or not value:
            continue
        column = canonical_for(label)
        if column:
            canonical.setdefault(column, value)
        else:
            other.setdefault(label, value)
    return canonical, other


def to_json(data: Dict[str, str]) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=False) if data else ""
