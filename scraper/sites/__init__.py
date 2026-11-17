"""Registry of site adapters. Add a new website by writing an adapter and listing it here."""
from .awadserag import AwadSeragAdapter
from .base import SiteAdapter
from .carrefour import CarrefourAdapter
from .elsindbad import ElsindbadAdapter
from .noon import NoonAdapter
from .vodafone import VodafoneAdapter
from .yallatager import YallaTagerAdapter

ADAPTERS = {
    ElsindbadAdapter.name: ElsindbadAdapter,
    AwadSeragAdapter.name: AwadSeragAdapter,
    YallaTagerAdapter.name: YallaTagerAdapter,
    NoonAdapter.name: NoonAdapter,
    VodafoneAdapter.name: VodafoneAdapter,
    CarrefourAdapter.name: CarrefourAdapter,
}

__all__ = ["ADAPTERS", "SiteAdapter"]
