# Crawl4AI price scraper

Collects TVs, mobile phones and tablets from six Egyptian stores into one dataset, using
[Crawl4AI](https://github.com/unclecode/crawl4ai) as the only scraping route. Each site runs
independently on its own schedule.

| Site | Crawl4AI approach | TVs | Mobiles | Tablets |
|---|---|---|---|---|
| Noon | Rendered page + CSS, JSON-LD on product pages | yes | yes | yes |
| Carrefour | Rendered page + CSS, published sitemaps, JSON-LD | yes | yes | yes |
| Vodafone eShop | Angular app: infinite scroll, page's own JSON recorded | yes | yes | yes |
| Elsindbad | Server-rendered page + CSS, JSON-LD | yes | store has none | store has none |
| Awad Serag | Server-rendered page + CSS | yes | listed, currently accessories only | store has none |
| Yalla Tager | JavaScript app: infinite scroll, rendered cards + CSS, page text and JSON on product pages | yes, once permission is recorded | yes, once permission is recorded | yes, once permission is recorded |

## Install (Windows)

Needs Python 3.10 or newer. From this folder:

```bash
python -m venv .venv
```

```bash
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

```bash
.venv\Scripts\python.exe -m playwright install chromium
```

## Run manually

```bash
.venv\Scripts\python.exe -m scraper run
```

Useful variations:

```bash
.venv\Scripts\python.exe -m scraper run --site noon --limit 5
```

```bash
.venv\Scripts\python.exe -m scraper run --site carrefour --product-url https://www.carrefouregypt.com/mafegy/en/p/676576 --category TVs
```

```bash
.venv\Scripts\python.exe -m scraper sites
```

```bash
.venv\Scripts\python.exe -m scraper status
```

`--limit` is a quick check: it saves what it scrapes but does not count as a full run.
`scripts\run_scraper.bat` does the same as `scraper run` and can be double-clicked.

## Run automatically

Keep this running; every enabled site follows its own schedule:

```bash
.venv\Scripts\python.exe -m scraper schedule
```

To start it automatically when you log on to Windows:

```bash
powershell -ExecutionPolicy Bypass -File scripts\install_windows_task.ps1
```

## Enable sites and set schedules

Everything is in `config.yaml`; no code changes are needed.

```yaml
sites:
  noon:
    enabled: true                     # false turns the site off
    schedule: {mode: interval, every_hours: 6}
  carrefour:
    schedule: {mode: daily_random, window_start: "09:00", window_end: "13:00"}
```

* `daily_random` runs once a day at a random time inside the window (the original project's schedule).
* `interval` runs every N hours, counted from the previous start.
* A site with no `schedule:` uses the defaults at the top of the file.
* `schedule.max_parallel_sites` limits how many sites crawl at the same moment.

As shipped, Noon, Carrefour and Yalla Tager use `daily_random`, because a full run takes hours.
Vodafone, Elsindbad and Awad Serag run every 6 hours.

## Output

| File | Contents |
|---|---|
| `output/products.csv` | One row per product with a `site` column. New products are appended, existing ones updated, and an empty value never overwrites a stored one. |
| `output/price_history.csv` | Append-only: every seller offer with price, currency and timestamp. |
| `output/excluded_products.csv` | Everything filtered out as an accessory, with the reason, for the latest run of each site. |
| `logs/` | `scraper.log`, `requests.log` (one line per page) and `errors.log`. |

Set `output.format: both` for Excel copies as well. A sample is in `sample_output/`.

Columns: SKU, store SKU, product name, category, seller, lowest price, currency, offers count,
availability, one column per common specification (brand, model, storage, RAM, screen size,
resolution ...), `other_specs` and `specs_raw` (JSON, nothing is lost), product URL, image URL,
site, condition, and the timestamps `scraped_at`, `first_seen`, `last_seen`.

**SKU.** A TV uses the manufacturer's model code, so the same TV can be matched across stores. Phones
and tablets use the store's own SKU, because one model code covers several colours and storage sizes
with different prices. The store's SKU is always in `store_sku`.

**Lowest price.** Prices are compared numerically. Offers without a valid price are dropped, in-stock
offers win over out-of-stock ones, and currencies are never converted or mixed. Ties go to a confirmed
in-stock offer, then the seller name A-Z, then the seller id.

## How it is organised

```
config.yaml              sites, schedules, delays, output paths
scraper/
  crawl_engine.py        scrape_site(url, config): Crawl4AI, retries, robots.txt, block handling
  sites/                 one file per store: its selectors, wait strategy and parser
  collector.py           URL collector (categories and pagination)
  price_processor.py     lowest-price selection
  data_processor.py      validation and normalisation
  product_filter.py      keeps real TVs, phones and tablets; drops accessories
  storage.py             products.csv and price history
  pipeline.py            runs one site end to end
  scheduler.py           per-site schedules
  state.py               locks and resumable checkpoints
tests/                   run offline against pages saved from the real sites
```

To add a store, write a module in `scraper/sites/` with its `ScrapeConfig` objects and parser, register
it in `scraper/sites/__init__.py`, and add it to `config.yaml`.

## Reliability

* Temporary failures (429, 5xx, timeouts, browser errors): at most 3 retries with exponential backoff,
  honouring `Retry-After`.
* 401, 403, CAPTCHA or a block page: the site stops for that run and is never retried or worked around.
  The other sites carry on.
* An interrupted run resumes where it stopped. Overlapping runs of the same site are prevented.
* `robots.txt` is checked before every page. Delays between pages are set per site.

## Known limits

* **Yalla Tager is off until permission is recorded.** Its `robots.txt` is `Disallow: /` and its Terms
  forbid automated collection without written consent. After getting that consent, write who approved
  it and when in `sites.yallatager.permission_reference`. Its selectors come from a working Playwright
  script and have only been tested against a local copy of that page behaviour, so check the first run
  with `--limit 5`. Tablets come from search, because no tablets category is known.
* **Noon** stores the featured seller only. The other-sellers panel loads from paths that `robots.txt`
  disallows.
* **Noon** reads the first 10 pages (500 products) of each category, most popular first. Raise
  `max_pages_per_category` for more; each extra page adds about 12 minutes.
* **Carrefour** cannot be paged because `robots.txt` disallows `?currentPage=`. Coverage comes from
  narrower categories and the site's sitemaps, so a few products may be missed.
* **Noon and Carrefour** did not load when the browser identified itself as a bot, so `crawl.user_agent`
  is left empty, which uses Crawl4AI's default browser identity.
* Selectors follow each site's current markup. If a redesign changes it, that site reports zero products
  or empty fields in `logs/errors.log`, and its `LISTING` / `PRODUCT` selectors need updating.

## Tests

```bash
.venv\Scripts\python.exe -m pytest
```
