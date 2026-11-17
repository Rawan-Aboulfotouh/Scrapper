"""
Runs the Yalla Tager adapter through real Crawl4AI and Chromium against a local stand-in site.

The stand-in reproduces the behaviour the adapter depends on: cards added by infinite scroll, a
"Show more" button that adds the SKU and specifications, and JSON loaded by the page itself.
No request leaves this machine.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from scraper.crawl_engine import CrawlSession, CrawlSettings
from scraper.sites.yallatager import YallaTagerAdapter

FILLER = "".join(f"<p>Placeholder paragraph {i} so the page is a normal size for the block detector.</p>" for i in range(40))
PRODUCTS = [("Samsung Galaxy A55 256GB Black", 19000 + i, 100 + i) for i in range(5)]
PRODUCTS += [("Samsung Galaxy A55 256GB Black", 18500, 900), ("Lenovo Tab One 64GB Grey", 6500, 200)]

LISTING = """<html><body><div class="products-toolbar-counts"><strong>1-2</strong> of <strong>%d</strong></div>
<div id="grid"></div><div class="infiniteScrollWrap" style="height:40px"></div>%s
<script>
const all = %s; let shown = 0, busy = false;
function more() {
  const grid = document.getElementById('grid');
  for (const [title, price, seller] of all.slice(shown, shown + 2)) {
    const card = document.createElement('div');
    card.className = 'productItem_wrap'; card.style.height = '700px';
    card.innerHTML = `<h6 class="product_title"><a href="/en/item-${seller}.html">${title}</a></h6>
      <div class="product_price"><span class="price">${price.toLocaleString('en-US')} EGP</span></div>
      <div class="buyNowBtnWrap"><button>Buy Now</button></div>`;
    grid.appendChild(card);
  }
  shown += 2;
}
setTimeout(more, 200);
window.addEventListener('wheel', e => {
  if (e.deltaY > 0 && !busy) { busy = true; setTimeout(() => { more(); busy = false; }, 150); }
});
</script></body></html>"""

PRODUCT = """<html><body><h1>Product</h1>%s<button id="more">Show more</button><div id="extra"></div>
<script>
document.getElementById('more').addEventListener('click', () => {
  document.getElementById('extra').innerHTML = `<div>SKU: 6132368500281-EGY-900</div>
    <h3>Technical Specification</h3><table><tr><td>Brand</td><td>Samsung</td></tr><tr><td>RAM</td><td>8 GB</td></tr></table>`;
});
fetch('/restfulapi/core/vendor/getVendorById').then(r => r.json());
</script></body></html>"""


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/restfulapi/"):
            body, kind = json.dumps({"vendor_data": {"entity_id": "900", "vendor_name": "Mobile Zone"}}), "application/json"
        elif self.path.startswith("/en/search"):
            body, kind = LISTING % (len(PRODUCTS), FILLER, json.dumps(PRODUCTS)), "text/html"
        else:
            body, kind = PRODUCT % FILLER, "text/html"
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, *_args):
        pass


@pytest.fixture
def local_site():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_real_browser_scrolls_groups_and_expands(local_site, tmp_path):
    session = CrawlSession(CrawlSettings(respect_robots_txt=False, min_delay_seconds=0, max_delay_seconds=0,
                                         max_retries=0))
    site_cfg = {"base_url": local_site, "allowed_hosts": ["127.0.0.1"], "store_code": "en", "image_base_url": "none",
                "max_products_per_category": 1500}
    adapter = YallaTagerAdapter(site_cfg, session, state_dir=tmp_path)
    try:
        items = list(adapter.discover(f"{local_site}/en/search?query=mobile", "Mobiles"))
        by_name = {i.hints["name"]: i for i in items}
        assert set(by_name) == {"Samsung Galaxy A55 256GB Black", "Lenovo Tab One 64GB Grey"}   # every card loaded
        phone, tablet = by_name["Samsung Galaxy A55 256GB Black"], by_name["Lenovo Tab One 64GB Grey"]
        assert len(phone.hints["offers"]) == 6 and tablet.category == "Tablets"                 # sellers grouped
        assert phone.url.endswith("/en/item-900.html")                                          # cheapest seller

        product = adapter.scrape(phone)
        assert product.sku == "6132368500281-EGY"                       # only present after "Show more" is clicked
        assert product.specs == {"Brand": "Samsung", "RAM": "8 GB"}
        cheapest = min(product.offers, key=lambda o: o.price)
        assert (cheapest.seller_id, cheapest.seller_name, str(cheapest.price)) == ("900", "Mobile Zone", "18500")
    finally:
        session.close()
