"""
Data Processor: turns a ScrapedProduct + the chosen offer into a clean, validated
row for the dataset. Responsible for normalisation and validation only; it does
not touch the network or the files.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Tuple

from .models import ScrapedProduct
from .price_processor import PriceDecision
from .product_filter import ProductTypeFilter
from .specs import CANONICAL_COLUMNS, normalize_specs, to_json
from .utils import clean_text, format_decimal

log = logging.getLogger("scraper.data")

ALLOWED_CATEGORIES = ("Mobiles", "TVs", "Tablets")

# Order of columns in products.csv
BASE_COLUMNS = [
    "sku", "store_sku", "product_name", "category", "seller_name", "lowest_price", "currency",
    "offers_count", "availability",
]
TAIL_COLUMNS = [
    "other_specs", "specs_raw", "product_url", "image_url", "site", "condition",
    "scraped_at", "first_seen", "last_seen", "seen_in_last_run",
]
PRODUCT_COLUMNS = BASE_COLUMNS + CANONICAL_COLUMNS + TAIL_COLUMNS

EXCLUDED_COLUMNS = ["site", "category", "product_name", "reason", "product_url", "timestamp", "run_id"]

HISTORY_COLUMNS = ["sku", "product_name", "site", "seller_name", "price", "currency",
                   "availability", "product_url", "timestamp", "run_id"]


class ValidationError(ValueError):
    pass


class DataProcessor:
    def __init__(self, cfg: dict):
        filters = cfg.get("filters", {}) or {}
        self.type_filter = ProductTypeFilter(filters)
        self.include_used = bool(filters.get("include_used_products", True))

    # ------------------------------------------------------------ filtering
    def is_excluded_by_name(self, name: str, category: str) -> Tuple[bool, str]:
        """Cheap check on the listing name, before the product page is downloaded."""
        return self.type_filter.check_name(name, category)

    def is_excluded(self, product: ScrapedProduct) -> Tuple[bool, str]:
        """Keeps only real TVs / phones (see product_filter.py for the rules)."""
        excluded, reason = self.type_filter.check_product(product.name, product.category, product.specs)
        if excluded:
            return True, reason
        if not self.include_used and product.extra.get("condition") in ("used", "renewed"):
            return True, "used product excluded by configuration"
        return False, ""

    def is_excluded_by_price(self, product: ScrapedProduct, decision: PriceDecision) -> Tuple[bool, str]:
        return self.type_filter.check_price(product.category, decision.offer.price if decision.offer else None)

    # ------------------------------------------------------------ building
    def build_record(self, product: ScrapedProduct, decision: PriceDecision, scraped_at: str) -> Dict[str, str]:
        name = clean_text(product.name)
        if not name:
            raise ValidationError(f"missing product name for {product.url}")
        if product.category not in ALLOWED_CATEGORIES:
            raise ValidationError(f"unexpected category {product.category!r} for {product.url}")
        if not product.url.startswith(("http://", "https://")):
            raise ValidationError(f"invalid product URL {product.url!r}")

        canonical, other = normalize_specs(product.specs)
        if not canonical.get("brand") and product.extra.get("brand"):
            canonical["brand"] = clean_text(product.extra["brand"])

        offer = decision.offer
        if offer is None:
            # some stores remove the price of a sold-out product
            sold_out = bool(product.offers) and all(o.in_stock is False for o in product.offers)
            availability = "out_of_stock" if sold_out else "no_price"
        elif offer.in_stock is False:
            availability = "out_of_stock"             # price shown is the last listed price
        elif offer.in_stock is True:
            availability = "in_stock"
        else:
            availability = "unknown"

        record = {column: "" for column in PRODUCT_COLUMNS}
        record.update({
            "sku": clean_text(product.sku),
            "store_sku": clean_text(product.extra.get("store_sku", "")),
            "product_name": name,
            "category": product.category,
            "seller_name": clean_text(offer.seller_name) if offer else "",
            "lowest_price": format_decimal(offer.price) if offer else "",
            "currency": (offer.currency or "") if offer else "",
            "offers_count": str(decision.valid_offers),
            "availability": availability,
            "other_specs": to_json(other),
            "specs_raw": to_json({clean_text(k): clean_text(v) for k, v in product.specs.items() if clean_text(v)}),
            "product_url": product.url,
            "image_url": clean_text(product.image_url),
            "site": product.site,
            "condition": clean_text(product.extra.get("condition", "")),
            "scraped_at": scraped_at,
            "last_seen": scraped_at,
            "seen_in_last_run": "yes",
        })
        for column in CANONICAL_COLUMNS:
            record[column] = canonical.get(column, "")
        return record

    @staticmethod
    def history_rows(product: ScrapedProduct, decision: PriceDecision, scraped_at: str,
                     run_id: str, sku: str) -> List[Dict[str, str]]:
        """
        Price history keeps every seller offer that has a price (not only the lowest),
        including out-of-stock ones, so price and stock changes can be analysed later.
        """
        rows = []
        seen = set()
        for offer in product.offers:
            if offer.price is None:
                continue
            key = (offer.seller_name, offer.seller_id, str(offer.price))
            if key in seen:
                continue
            seen.add(key)
            rows.append({
                "sku": sku,
                "product_name": clean_text(product.name),
                "site": product.site,
                "seller_name": clean_text(offer.seller_name),
                "price": format_decimal(offer.price),
                "currency": offer.currency or "",
                "availability": ("out_of_stock" if offer.in_stock is False
                                 else "in_stock" if offer.in_stock else "unknown"),
                "product_url": offer.url or product.url,
                "timestamp": scraped_at,
                "run_id": run_id,
            })
        return rows


def product_key(site: str, sku: str, url: str) -> str:
    """Primary identifier: site + SKU, falling back to site + product URL."""
    sku = clean_text(sku)
    return f"{site}|sku:{sku}" if sku else f"{site}|url:{url}"

