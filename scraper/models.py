"""Plain data containers passed between the pipeline components."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Optional


@dataclass
class DiscoveredItem:
    """A product found by the URL Collector (from a category page or given directly)."""
    site: str
    url: str
    category: str                      # "Mobiles" or "TVs"
    key: str                           # stable identity used for checkpoint/de-duplication
    hints: Dict[str, Any] = field(default_factory=dict)   # data already seen on the listing page

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DiscoveredItem":
        return cls(**data)


@dataclass
class Offer:
    """One seller's offer for a product."""
    seller_name: str
    price: Optional[Decimal]
    currency: Optional[str]
    in_stock: Optional[bool] = None    # None = unknown (treated as available)
    seller_id: str = ""
    url: str = ""
    sku: str = ""
    source: str = ""                   # where the offer was read: listing / product / other_sellers

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["price"] = str(self.price) if self.price is not None else None
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Offer":
        data = dict(data)
        data["price"] = Decimal(data["price"]) if data.get("price") not in (None, "") else None
        return cls(**data)


@dataclass
class ScrapedProduct:
    """Everything the Product Scraper extracted for one unique product."""
    site: str
    url: str
    category: str
    name: str = ""
    sku: str = ""
    image_url: str = ""
    specs: Dict[str, str] = field(default_factory=dict)      # raw label -> value, priority order
    offers: List[Offer] = field(default_factory=list)
    extra: Dict[str, Any] = field(default_factory=dict)      # brand, condition, template_id ...
