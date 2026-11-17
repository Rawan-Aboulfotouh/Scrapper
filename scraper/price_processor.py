"""
Price Processor: chooses the lowest valid offer for one product.

Rules
-----
1. Drop offers without a valid price: missing, non-numeric, <= 0, or outside
   [min_valid_price, max_valid_price] from the configuration.
2. Prefer offers that can be bought: if at least one offer is in stock (or its
   stock is unknown), the lowest of THOSE is selected, and out-of-stock offers
   are ignored even when cheaper.
3. If every offer is out of stock, the product is still reported: the lowest
   listed out-of-stock price is selected and the product is marked out_of_stock.
4. Never compare across currencies. If offers use several currencies, only the
   ``preferred_currency`` (default EGP) is compared; if it is absent, the currency
   with the most offers is used. The other offers are logged and ignored.
5. Compare prices numerically (Decimal).
6. Tie-breaking when several sellers share the lowest price, in this order:
     a. an offer confirmed in stock beats one whose stock is unknown
     b. seller name, case-insensitive alphabetical order
     c. seller id / URL, alphabetical order (fully deterministic)
"""
from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from typing import List, Optional

from .models import Offer

log = logging.getLogger("scraper.price")

TIE_BREAK_RULE = ("in-stock offers first; lowest price; then confirmed in-stock before unknown stock; "
                  "then seller name A-Z; then seller id/URL A-Z")


@dataclass
class PriceDecision:
    offer: Optional[Offer]
    valid_offers: int
    total_offers: int
    reason: str = ""


class PriceProcessor:
    def __init__(self, cfg: dict):
        self.preferred_currency = (cfg.get("preferred_currency") or "EGP").upper()
        self.min_valid = Decimal(str(cfg.get("min_valid_price", 1)))
        self.max_valid = Decimal(str(cfg.get("max_valid_price", 10_000_000)))

    def _has_valid_price(self, offer: Offer) -> bool:
        if offer.price is None or not isinstance(offer.price, Decimal) or not offer.price.is_finite():
            return False
        return self.min_valid <= offer.price <= self.max_valid

    def _lowest(self, offers: List[Offer], context: str) -> Offer:
        currencies = Counter((o.currency or self.preferred_currency).upper() for o in offers)
        if len(currencies) > 1:
            chosen = self.preferred_currency if self.preferred_currency in currencies \
                else currencies.most_common(1)[0][0]
            log.warning("%s: offers in several currencies %s; comparing only %s (no conversion source)",
                        context, dict(currencies), chosen)
            offers = [o for o in offers if (o.currency or self.preferred_currency).upper() == chosen]

        def sort_key(o: Offer):
            return (o.price,
                    0 if o.in_stock is True else 1,
                    (o.seller_name or "").casefold(),
                    str(o.seller_id or ""),
                    o.url or "")

        return min(offers, key=sort_key)

    def select_lowest(self, offers: List[Offer], context: str = "") -> PriceDecision:
        total = len(offers)
        priced = [o for o in offers if self._has_valid_price(o)]
        buyable = [o for o in priced if o.in_stock is not False]
        if buyable:
            best = self._lowest(buyable, context)
            ties = [o for o in buyable if o.price == best.price]
            reason = "single valid offer" if len(buyable) == 1 else (
                f"lowest of {len(buyable)} valid offers"
                + (f" (tie between {len(ties)}, rule applied)" if len(ties) > 1 else ""))
            return PriceDecision(best, len(buyable), total, reason)
        if priced:                                   # everything is out of stock: still report it
            best = self._lowest(priced, context)
            return PriceDecision(best, 0, total, f"out of stock - lowest listed price of {len(priced)} offer(s)")
        return PriceDecision(None, 0, total, "no offer with a valid price")
