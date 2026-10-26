#!/usr/bin/env python3
"""
Daily tire price fetcher for mizubikes.com/tire-prices/

Reads   data/tires.yaml              (the watchlist you edit)
Writes  data/tire_prices.json        (current price + stats per tire)
        data/tire_history.json       (one price point per tire per day)

Prices come from a Shopify store (Worldwide Cyclery) via the public
/products/<handle>.js endpoint. Amazon prices are never fetched or shown.

Run locally:   python scripts/tire_prices.py
"""

from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import statistics
import sys
import time
from zoneinfo import ZoneInfo

import requests
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
WATCHLIST = ROOT / "data" / "tires.yaml"
PRICES = ROOT / "data" / "tire_prices.json"
HISTORY = ROOT / "data" / "tire_history.json"

HISTORY_DAYS = 400          # how much daily history to keep
DEAL_DROP = 0.10            # 10% under the 90-day median counts as a deal
LIST_DROP = 0.15            # or 15%+ under the store's list price (WWC always shows a small "list price" discount)
MIN_PRICE, MAX_PRICE = 10.0, 300.0     # per-tire sanity bounds; outside = reject
TIMEOUT = 20
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; MizubikesPriceBot/1.0; "
                  "+https://mizubikes.com/tire-prices/)",
    "Accept": "application/json",
}


# ---------------------------------------------------------------- helpers

def load_json(path: pathlib.Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print(f"WARN: {path.name} is not valid JSON, starting fresh")
    return default


def write_json(path: pathlib.Path, data) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")


def fetch_shopify_product(base: str, handle: str) -> dict:
    url = f"{base.rstrip('/')}/products/{handle}.js"
    for attempt in range(3):
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(5 * (attempt + 1))
                continue
            raise RuntimeError(f"HTTP {r.status_code} for {url}")
        except requests.RequestException as e:
            if attempt == 2:
                raise RuntimeError(f"{type(e).__name__} for {url}") from e
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"gave up on {url}")


def pick_variant(product: dict, match: list[str], exclude: list[str]):
    """Cheapest in-stock variant whose title has every `match` word and no
    `exclude` word. Falls back to out-of-stock matches so a backordered
    spool still shows a price (flagged as unavailable)."""
    match = [m.lower() for m in match]
    exclude = [x.lower() for x in exclude]
    hits = []
    for v in product.get("variants", []):
        title = " ".join(filter(None, [v.get("title"), v.get("public_title")])).lower()
        if all(m in title for m in match) and not any(x in title for x in exclude):
            hits.append(v)
    if not hits:
        return None
    in_stock = [v for v in hits if v.get("available")]
    return min(in_stock or hits, key=lambda v: v["price"])


def cents(value) -> float | None:
    if value in (None, "", 0):
        return None
    return round(int(value) / 100.0, 2)


def stats_for(points: list[dict], today: dt.date) -> dict:
    """Low/high/median over trailing windows from history points."""
    out = {}
    for days in (30, 90):
        cutoff = (today - dt.timedelta(days=days)).isoformat()
        window = [p["p"] for p in points if p["d"] >= cutoff]
        if window:
            out[f"low_{days}"] = min(window)
            out[f"high_{days}"] = max(window)
            out[f"median_{days}"] = round(statistics.median(window), 2)
    out["days_tracked"] = len(points)
    return out


# ---------------------------------------------------------------- main

def main() -> int:
    cfg = yaml.safe_load(WATCHLIST.read_text(encoding="utf-8"))
    tires = cfg.get("tires", [])
    old = load_json(PRICES, {"items": {}})
    history = load_json(HISTORY, {})

    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    today = now.date()
    today_s = today.isoformat()
    cutoff_s = (today - dt.timedelta(days=HISTORY_DAYS)).isoformat()

    items, ok, failed, skipped = {}, 0, [], 0

    for f in tires:
        fid = f["id"]
        store = f.get("store")
        prev = old.get("items", {}).get(fid, {})

        if not store:
            items[fid] = {"id": fid, "source": "amazon-only"}
            skipped += 1
            continue

        rec = {
            "id": fid,
            "source": "store",
            "store_url": f"{store['base'].rstrip('/')}/products/{store['handle']}",
        }
        try:
            product = fetch_shopify_product(store["base"], store["handle"])
            v = pick_variant(product, store.get("match", []), store.get("exclude", []))
            if v is None:
                titles = [x.get("title") for x in product.get("variants", [])][:12]
                raise RuntimeError(f"no variant matched {store.get('match')} — saw {titles}")

            price = cents(v["price"])
            compare_at = cents(v.get("compare_at_price"))
            if not (MIN_PRICE <= price <= MAX_PRICE):
                raise RuntimeError(f"price ${price} outside sanity range")

            rec.update({
                "price": price,
                "compare_at": compare_at if compare_at and compare_at > price else None,
                "available": bool(v.get("available")),
                "variant": v.get("title"),
                "checked_at": now.isoformat(),
                "stale": False,
            })
            if v.get("id"):
                rec["store_url"] += f"?variant={v['id']}"

            pts = [p for p in history.get(fid, []) if p["d"] != today_s and p["d"] >= cutoff_s]
            pts.append({"d": today_s, "p": price})
            history[fid] = sorted(pts, key=lambda p: p["d"])
            ok += 1
            print(f"OK    {fid:40s} ${price:>7.2f}  {'in stock' if v.get('available') else 'OUT'}")
        except Exception as e:  # keep last known price, mark stale
            failed.append(fid)
            print(f"FAIL  {fid:40s} {e}")
            for k in ("price", "compare_at", "available", "variant", "checked_at"):
                if k in prev:
                    rec[k] = prev[k]
            if prev.get("store_url"):
                rec["store_url"] = prev["store_url"]
            rec["stale"] = "price" in rec
            rec["error"] = str(e)[:300]

        pts = history.get(fid, [])
        rec.update(stats_for(pts, today))
        if rec.get("price") is not None:
            med = rec.get("median_90")
            on_sale = bool(rec.get("compare_at") and rec["price"] <= rec["compare_at"] * (1 - LIST_DROP))
            below_typical = bool(med and rec["price"] <= med * (1 - DEAL_DROP))
            rec["deal"] = on_sale or below_typical
            rec["at_low"] = bool(rec.get("low_90") is not None
                                 and rec["price"] <= rec["low_90"]
                                 and rec.get("days_tracked", 0) >= 14)
        items[fid] = rec

    # drop history for tires removed from the watchlist
    live_ids = {f["id"] for f in tires}
    history = {k: v for k, v in history.items() if k in live_ids}

    write_json(PRICES, {"updated": now.astimezone(ZoneInfo("America/Los_Angeles")).isoformat(), "items": items})
    write_json(HISTORY, history)

    summary = f"{ok} updated, {len(failed)} failed, {skipped} Amazon-only"
    print(summary)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
            fh.write(f"### Tire prices\n{summary}\n")
            for fid in failed:
                fh.write(f"- ❌ `{fid}`: {items[fid].get('error', '')}\n")

    # fail the run only if nothing could be priced (keeps partial updates)
    return 1 if ok == 0 and failed else 0


if __name__ == "__main__":
    sys.exit(main())
