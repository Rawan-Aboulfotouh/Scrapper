"""
Storage Manager: products dataset + price history.

products.csv
    one row per unique product, keyed by site + SKU (fallback: site + product URL).
    * new products are appended
    * existing products are updated field by field; an empty new value NEVER
      overwrites a non-empty stored value
    * products not seen in the current run are kept (seen_in_last_run = "no")
    * the previous file is copied to products.csv.bak before every write,
      and writes are atomic (temp file + rename)

price_history.csv
    append-only; one row per seller offer per run (re-running a resumed run
    does not duplicate rows thanks to the run_id check).

Files are written as UTF-8 with BOM so Excel on Windows shows Arabic correctly.
Optionally an .xlsx copy is produced as well.
"""
from __future__ import annotations

import csv
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from .data_processor import EXCLUDED_COLUMNS, HISTORY_COLUMNS, PRODUCT_COLUMNS, product_key

log = logging.getLogger("scraper.storage")
ENCODING = "utf-8-sig"


def _read_csv(path: Path) -> (List[str], List[Dict[str, str]]):
    if not path.exists() or path.stat().st_size == 0:
        return [], []
    with path.open("r", encoding=ENCODING, newline="") as handle:
        reader = csv.DictReader(handle)
        rows = [{k: (v or "") for k, v in row.items() if k is not None} for row in reader]
        return list(reader.fieldnames or []), rows


def _atomic_write_csv(path: Path, columns: List[str], rows: Iterable[Dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.stem + "_", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding=ENCODING, newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow({c: row.get(c, "") for c in columns})
        os.replace(tmp_name, path)
    except BaseException:
        if os.path.exists(tmp_name):
            os.remove(tmp_name)
        raise


class StorageManager:
    def __init__(self, output_cfg: dict):
        self.products_path = Path(output_cfg["products_file"])
        self.history_path = Path(output_cfg["price_history_file"])
        self.write_excel = str(output_cfg.get("format", "csv")).lower() in ("xlsx", "both")
        self.keep_backup = bool(output_cfg.get("keep_backup", True))
        self.excluded_path = Path(output_cfg.get("excluded_file")
                                  or self.products_path.with_name("excluded_products.csv"))

    # ------------------------------------------------------------------ products
    def existing_keys(self) -> set:
        """Identity keys (site + SKU, else site + URL) of the products already stored."""
        _, rows = _read_csv(self.products_path)
        return {product_key(r.get("site", ""), r.get("sku", ""), r.get("product_url", "")) for r in rows}

    def merge_products(self, records: List[Dict[str, str]], run_started_at: Optional[str] = None) -> Dict[str, int]:
        """Merge processed records into products.csv. Returns counters."""
        existing_columns, existing_rows = _read_csv(self.products_path)
        columns = list(PRODUCT_COLUMNS) + [c for c in existing_columns if c not in PRODUCT_COLUMNS]

        by_key: Dict[str, Dict[str, str]] = {}
        url_index: Dict[str, str] = {}
        for row in existing_rows:
            key = product_key(row.get("site", ""), row.get("sku", ""), row.get("product_url", ""))
            by_key[key] = row
            if row.get("product_url"):
                url_index[f"{row.get('site', '')}|{row['product_url']}"] = key

        # products that were part of this run are re-flagged below
        touched = set()
        added = updated = 0
        for record in records:
            key = product_key(record["site"], record["sku"], record["product_url"])
            if key not in by_key:
                # SKU fallback: the same URL may have been stored before without a SKU (or vice versa)
                url_key = url_index.get(f"{record['site']}|{record['product_url']}")
                if url_key and url_key in by_key:
                    by_key[key] = by_key.pop(url_key)
                    log.debug("matched %s to stored row %s by URL", key, url_key)
            if key in by_key:
                stored = by_key[key]
                for column, value in record.items():
                    if column == "first_seen":
                        continue
                    if value not in (None, ""):
                        stored[column] = value            # never overwrite with empty values
                if not stored.get("first_seen"):
                    stored["first_seen"] = record.get("scraped_at", "")
                updated += 1
            else:
                new_row = dict(record)
                new_row["first_seen"] = record.get("scraped_at", "")
                by_key[key] = new_row
                added += 1
            touched.add(key)
            url_index[f"{record['site']}|{record['product_url']}"] = key

        # at the end of a run, flag products of the scraped sites that were not found this time
        if run_started_at is not None:
            run_sites = {r["site"] for r in records}
            for key, row in by_key.items():
                if key not in touched and row.get("site") in run_sites:
                    row["seen_in_last_run"] = "no"

        if self.keep_backup and self.products_path.exists():
            shutil.copy2(self.products_path, self.products_path.with_suffix(self.products_path.suffix + ".bak"))
        ordered = sorted(by_key.values(), key=lambda r: (r.get("site", ""), r.get("category", ""), r.get("product_name", "")))
        _atomic_write_csv(self.products_path, columns, ordered)
        if self.write_excel:
            self._write_excel(self.products_path.with_suffix(".xlsx"), columns, ordered, "products")
        log.info("products saved to %s (%d added, %d updated, %d total)", self.products_path, added, updated, len(ordered))
        return {"added": added, "updated": updated, "total": len(ordered)}

    # ------------------------------------------------------------------ history
    def append_history(self, rows: List[Dict[str, str]]) -> int:
        if not rows:
            return 0
        existing_columns, existing_rows = _read_csv(self.history_path)
        run_ids = {r["run_id"] for r in rows}
        already = {
            (r.get("run_id"), r.get("site"), r.get("sku"), r.get("seller_name"), r.get("product_url"))
            for r in existing_rows if r.get("run_id") in run_ids
        }
        new_rows = [r for r in rows
                    if (r["run_id"], r["site"], r["sku"], r["seller_name"], r["product_url"]) not in already]
        if not new_rows:
            return 0
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        missing = [c for c in HISTORY_COLUMNS if existing_columns and c not in existing_columns]
        if missing:
            # file written by an older version: rewrite it once with the new columns (old rows keep blanks)
            columns = existing_columns + missing
            _atomic_write_csv(self.history_path, columns, existing_rows)
            log.info("price history upgraded with new column(s): %s", ", ".join(missing))
        write_header = not self.history_path.exists() or self.history_path.stat().st_size == 0
        columns = (existing_columns + missing) if existing_columns else HISTORY_COLUMNS
        with self.history_path.open("a", encoding=ENCODING if write_header else "utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
            if write_header:
                writer.writeheader()
            for row in new_rows:
                writer.writerow({c: row.get(c, "") for c in columns})
        if self.write_excel:
            _, all_rows = _read_csv(self.history_path)
            self._write_excel(self.history_path.with_suffix(".xlsx"), columns, all_rows, "price_history")
        log.info("price history: %d rows appended to %s", len(new_rows), self.history_path)
        return len(new_rows)

    # ------------------------------------------------------------------ excluded
    def write_excluded(self, rows: List[Dict[str, str]], site: Optional[str] = None) -> None:
        """
        Products skipped as accessories / not a TV, phone or tablet in the LAST run, with the reason.
        Review it to check that nothing real was filtered out.

        Sites run independently, so with ``site`` only that site's rows are replaced and the
        latest report of every other site is kept.
        """
        if site is not None:
            _, existing = _read_csv(self.excluded_path)
            rows = [r for r in existing if r.get("site") != site] + list(rows)
        rows = sorted(rows, key=lambda r: (r.get("site", ""), r.get("category", ""), r.get("product_name", "")))
        _atomic_write_csv(self.excluded_path, EXCLUDED_COLUMNS, rows)
        log.info("excluded products report: %d rows in %s", len(rows), self.excluded_path)

    # ------------------------------------------------------------------ excel
    @staticmethod
    def _write_excel(path: Path, columns: List[str], rows: List[Dict[str, str]], sheet: str) -> None:
        try:
            from openpyxl import Workbook
        except ImportError:                     # optional dependency
            log.warning("openpyxl not installed; skipping Excel export")
            return
        wb = Workbook()
        ws = wb.active
        ws.title = sheet
        ws.append(columns)
        for row in rows:
            ws.append([row.get(c, "") for c in columns])
        ws.freeze_panes = "A2"
        tmp = path.with_suffix(".tmp.xlsx")
        wb.save(tmp)
        os.replace(tmp, path)
