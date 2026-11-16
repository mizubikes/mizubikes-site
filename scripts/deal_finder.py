#!/usr/bin/env python3
"""
Daily bike deal finder for mizubikes.com/bike-deals/

Reads   data/deal_stores.yaml     (the store list + thresholds you edit)
Writes  data/deals.json           (today's deals, grouped by category)
        data/deal_history.json    (first-seen date + lowest price per deal)

Each store is a Shopify shop. The script pages through the public
/products.json catalog (or specific collections), keeps new, in-stock items
marked down from the store's own list price (used gear is left out), sorts them into categories,
and ranks them. Each row can carry an Amazon search link with your Associates
tag; Amazon prices are never fetched or shown.

Run locally:   python scripts/deal_finder.py
Dry run:       python scripts/deal_finder.py --dry-run   (prints, writes nothing)
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import pathlib
import re
import sys
import time
import urllib.parse
from zoneinfo import ZoneInfo

import requests
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
STORES = ROOT / "data" / "deal_stores.yaml"
DEALS = ROOT / "data" / "deals.json"
HISTORY = ROOT / "data" / "deal_history.json"

TIMEOUT = 30
PAGE_SIZE = 250                 # Shopify's maximum for products.json
HISTORY_KEEP_DAYS = 45          # forget deals not seen for this long
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; MizubikesPriceBot/1.0; "
                  "+https://mizubikes.com/bike-deals/)",
    "Accept": "application/json",
}

DEFAULTS = {
    "min_discount": 0.20,       # at least 20% under the store's list price
    "max_discount": 0.80,       # above this the "list price" is usually bogus
    "min_price": 20.0,          # skip small parts and consumables
    "per_category": 20,         # rows shown per category
    "per_store_per_category": 8,  # stop one store from filling a category
    "top_count": 10,            # rows in "Best deals today"
    "max_pages": 100,           # 100 x 250 = 25,000 products per store
    "delay_seconds": 1.0,       # pause between requests to the same store
    "amazon_tag": "",           # Amazon Associates tag; empty = no Amazon buttons
    "amazon_skip": ["bikes", "framesets"],   # categories Amazon doesn't really sell
}

# Display order on the page. Keys are used as anchors.
CATEGORIES = [
    ("bikes", "Complete bikes"),
    ("framesets", "Framesets"),
    ("wheels", "Wheels"),
    ("drivetrain", "Drivetrain"),
    ("brakes", "Brakes"),
    ("suspension", "Suspension and droppers"),
    ("cockpit", "Bars, stems, posts and saddles"),
    ("tires", "Tires and tubes"),
    ("electronics", "Computers, power meters and lights"),
    ("apparel", "Helmets, shoes and clothing"),
    ("tools", "Tools and maintenance"),
    ("accessories", "Accessories"),
]

USED = re.compile(r"pre-?owned|\bused\b|certified|second ?hand|\bdemo\b|open box|blemish|\bblem\b", re.I)

SKIP = re.compile(r"gift ?card|e-?gift|warranty|protection plan|shipping protection|"
                  r"route package|donation|labor|service fee|deposit", re.I)

NOT_A_BIKE = re.compile(r"\b(racks?|trainers?|computers?|stands?|bags?|cases?|lights?|helmets?|"
                        r"shoes?|locks?|pumps?|fenders?|trailers?|covers?|mounts?|bells?|"
                        r"baskets?|kickstands?|carriers?)\b", re.I)
NOT_A_FRAME = re.compile(r"frame (bag|pump|protect|guard|strap|pack|tape|saver|mount)|"
                         r"frame-?bag|frame hardware|bearing kit", re.I)

# First match wins. Checked against "product_type | title", then tags.
RULES = [
    ("electronics", re.compile(r"power ?meter|bike computer|cycling computer|\bgps\b|head ?unit|"
                               r"\bradar\b|heart rate|\bsensor\b|\blights\b|head ?light|"
                               r"tail ?light|light set|lumens?|action cam|\btrainers?\b")),
    ("apparel", re.compile(r"helmet|\bshoes?\b|jersey|\bbibs?\b|\bshorts\b|jacket|gloves?|"
                           r"\bsocks?\b|\bvest\b|\btights\b|apparel|clothing|sunglasses|"
                           r"eyewear|goggles?|knee pads?|elbow pads?|body armor|base ?layer|"
                           r"hoodie|t-shirt|\btee\b|\bpants\b")),
    ("tires", re.compile(r"\btires?\b|\btyres?\b|inner tubes?|\bpresta\b|\bschrader\b|"
                         r"sealant|rim tape")),
    ("tools", re.compile(r"\btools?\b|\blube\b|lubricant|degreaser|cleaner|repair stand|"
                         r"work ?stand|torque|\bwrench|bleed kit|grease|\bpumps?\b|"
                         r"chain ?wax")),
    ("suspension", re.compile(r"\bforks?\b|\bshocks?\b|suspension|dropper|air spring|damper")),
    ("wheels", re.compile(r"wheelset|\bwheels?\b|\bhubs?\b|\brims?\b(?! brake)|\bspokes?\b|"
                          r"freehub")),
    ("brakes", re.compile(r"\bbrakes?\b|\brotors?\b|brake pads?|caliper")),
    ("drivetrain", re.compile(r"groupset|drivetrain|derailleur|cassette|crank|chainring|"
                              r"\bchains?\b|shifter|bottom bracket|\bpedals?\b|\baxs\b|\bdi2\b|"
                              r"upgrade kit")),
    ("cockpit", re.compile(r"handlebar|\bbars?\b|\bstems?\b|seatpost|seat post|saddle|\bgrips?\b|"
                           r"bar tape|headset|cockpit")),
]

BIKE_TITLE = re.compile(r"\b(road|mountain|gravel|cyclocross|triathlon|tt|fat|electric|e|"
                        r"enduro|trail|xc|commuter|hybrid|kids)[ -]?bike\b|\bbike\b ?[-|,] ?(19|20)\d\d")


# ---------------------------------------------------------------- helpers

def load_json(path: pathlib.Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print(f"WARN: {path.name} is not valid JSON, starting fresh")
    return default


def write_json(path: pathlib.Path, data) -> None:
    path.write_text(json.dumps(data, indent=1, sort_keys=True, ensure_ascii=False) + "\n",
                    encoding="utf-8")


def money(value) -> float | None:
    """products.json gives "129.99" (string dollars); product .js gives cents."""
    if value in (None, "", 0, "0", "0.00"):
        return None
    try:
        if isinstance(value, int):
            return round(value / 100.0, 2)
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def get_json(url: str) -> dict:
    for attempt in range(3):
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(8 * (attempt + 1))
                continue
            raise RuntimeError(f"HTTP {r.status_code}")
        except (requests.RequestException, ValueError) as e:
            if attempt == 2:
                raise RuntimeError(type(e).__name__) from e
            time.sleep(8 * (attempt + 1))
    raise RuntimeError("gave up after 3 tries (rate limited or server errors)")


def fetch_catalog(store: dict, cfg: dict) -> list[dict]:
    """All products for a store, de-duplicated across collections."""
    base = store["base"].rstrip("/")
    collections = store.get("collections") or [None]
    max_pages = int(store.get("max_pages", cfg["max_pages"]))
    seen, out = set(), []
    for coll in collections:
        path = f"/collections/{coll}/products.json" if coll else "/products.json"
        for page in range(1, max_pages + 1):
            try:
                data = get_json(f"{base}{path}?limit={PAGE_SIZE}&page={page}")
            except RuntimeError:
                if page == 1:
                    raise
                break       # store stops serving deep pages: keep what we have
            products = data.get("products") or []
            for p in products:
                if p.get("id") not in seen:
                    seen.add(p.get("id"))
                    out.append(p)
            if len(products) < PAGE_SIZE:
                break
            time.sleep(float(cfg["delay_seconds"]))
    return out


def categorize(product: dict, price: float) -> str:
    ptype = (product.get("product_type") or "").lower()
    title = (product.get("title") or "").lower()
    tags = product.get("tags") or []
    if isinstance(tags, str):
        tags = tags.split(",")
    text = " | ".join([ptype, title, " ".join(t.strip().lower() for t in tags[:25])])
    head = f"{ptype} | {title}"

    is_bike = (re.search(r"\bbikes?\b|bicycles?", ptype) and price >= 500) or \
              (BIKE_TITLE.search(title) and price >= 800)
    if is_bike and not NOT_A_BIKE.search(head):
        return "bikes"
    if re.search(r"frameset|frame set|\bframes?\b", head) and price >= 300 \
            and not NOT_A_FRAME.search(head):
        return "framesets"
    for key, rx in RULES:
        if rx.search(head):
            return key
    for key, rx in RULES:
        if rx.search(text):
            return key
    return "accessories"


def best_variant(product: dict, cfg: dict):
    """In-stock variant with the biggest real markdown, or None."""
    best = None
    for v in product.get("variants") or []:
        if not v.get("available"):
            continue
        price, was = money(v.get("price")), money(v.get("compare_at_price"))
        if not price or not was or was <= price:
            continue
        pct = 1 - price / was
        if price < cfg["min_price"] or not (cfg["min_discount"] <= pct <= cfg["max_discount"]):
            continue
        if best is None or pct > best[2] or (pct == best[2] and price < best[0]):
            best = (price, was, pct, v)
    return best


def link_for(store: dict, url: str) -> tuple[str, bool]:
    """Wrap the product URL in the store's affiliate template, if it has one."""
    tpl = (store.get("affiliate") or "").strip()
    if not tpl:
        return url, False
    return (tpl.replace("{url_encoded}", urllib.parse.quote(url, safe=""))
               .replace("{url}", url)), True


def amazon_link(brand: str, title: str, tag: str) -> str:
    """Amazon search link carrying the Associates tag. Never a price: Amazon
    only allows prices pulled from its own API, so this is a "check it" link."""
    words = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", title)       # drop [notes] and (notes)
    words = re.sub(r"[^\w\s.\-/+]", " ", words)
    if brand and not words.lower().lstrip().startswith(brand.lower()):
        words = f"{brand} {words}"
    query = " ".join(words.split()[:10])
    return f"https://www.amazon.com/s?k={urllib.parse.quote_plus(query)}&tag={tag}"


def score(item: dict) -> float:
    """Bigger discount first, with a nudge toward higher-ticket items."""
    return item["pct"] * (1 + math.log10(max(item["price"], 10)) / 2)


def scan_store(store: dict, cfg: dict) -> tuple[list[dict], int]:
    base = store["base"].rstrip("/")
    products = fetch_catalog(store, cfg)
    deals = []
    for p in products:
        title = (p.get("title") or "").strip()
        label = f"{title} {p.get('product_type') or ''}"
        if not title or SKIP.search(label) or USED.search(label):
            continue        # new gear only: used, demo and pre-owned are left out
        hit = best_variant(p, cfg)
        if not hit:
            continue
        price, was, pct, v = hit
        url = f"{base}/products/{p.get('handle')}"
        if v.get("id"):
            url += f"?variant={v['id']}"
        href, sponsored = link_for(store, url)
        vtitle = (v.get("title") or "").strip()
        brand = (p.get("vendor") or "").strip()
        if brand and title.lower().startswith(brand.lower()):
            brand = title[:len(brand)]      # use the title's spelling, e.g. SPECIALIZED -> Specialized
        category = categorize(p, price)
        tag = (cfg.get("amazon_tag") or "").strip()
        deals.append({
            "key": f"{store['id']}:{p.get('id')}",
            "store": store["id"],
            "store_name": store["name"],
            "title": title,
            "brand": brand,
            "variant": "" if vtitle.lower() == "default title" else vtitle,
            "price": price,
            "was": was,
            "pct": round(pct, 3),
            "save": round(was - price, 2),
            "category": category,
            "amazon": amazon_link(brand, title, tag)
                      if tag and category not in (cfg.get("amazon_skip") or []) else "",
            "url": href,
            "sponsored": sponsored,
            "stale": False,
        })
    return deals, len(products)


def pick(pool: list[dict], limit: int, per_store: int, per_category: int | None = None):
    """Top `limit` by score with caps so one store or category can't dominate."""
    out, by_store, by_cat = [], {}, {}
    for it in sorted(pool, key=score, reverse=True):
        if by_store.get(it["store"], 0) >= per_store:
            continue
        if per_category is not None and by_cat.get(it["category"], 0) >= per_category:
            continue
        out.append(it)
        by_store[it["store"]] = by_store.get(it["store"], 0) + 1
        by_cat[it["category"]] = by_cat.get(it["category"], 0) + 1
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print results, write nothing")
    args = ap.parse_args()

    raw = yaml.safe_load(STORES.read_text(encoding="utf-8")) or {}
    cfg = {**DEFAULTS, **(raw.get("settings") or {})}
    # stores flagged `used: true` sell mostly second-hand gear and are skipped
    stores = [s for s in raw.get("stores", [])
              if s.get("enabled", True) and not s.get("used")]

    old = load_json(DEALS, {})
    history = load_json(HISTORY, {})
    first_run = not history

    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    today = now.astimezone(ZoneInfo("America/Los_Angeles")).date()
    today_s = today.isoformat()

    old_items = [it for c in old.get("categories", []) for it in c.get("items", [])
                 if not USED.search(it.get("title", ""))]
    pool, status, ok_count = [], [], 0

    for s in stores:
        rec = {"id": s["id"], "name": s["name"], "home": s["base"].rstrip("/")}
        try:
            deals, scanned = scan_store(s, cfg)
            pool.extend(deals)
            rec.update({"ok": True, "scanned": scanned, "deals": len(deals)})
            ok_count += 1
            print(f"OK    {s['id']:24s} {scanned:6d} products  {len(deals):5d} deals")
        except Exception as e:  # keep yesterday's rows for this store, flagged stale
            carried = [dict(it, stale=True) for it in old_items if it.get("store") == s["id"]]
            pool.extend(carried)
            rec.update({"ok": False, "error": str(e)[:200], "deals": len(carried)})
            print(f"FAIL  {s['id']:24s} {e}  (kept {len(carried)} from last run)")
        status.append(rec)

    if ok_count == 0:
        print("No store could be scanned — leaving yesterday's data in place.")
        return 1

    # history: first-seen date and lowest price per deal
    for it in pool:
        h = history.get(it["key"])
        if h is None:
            # deals found on the very first run are a baseline, not "new today"
            h = history[it["key"]] = {"first": today_s, "last": today_s, "low": it["price"]}
            if first_run:
                h["seed"] = True
            it["lower"] = False
        else:
            it["lower"] = it["price"] < h["low"] - 0.005
            h["low"] = min(h["low"], it["price"])
            h["last"] = today_s
        it["new"] = h["first"] == today_s and not h.get("seed")
        it["since"] = history[it["key"]]["first"]
    cutoff = (today - dt.timedelta(days=HISTORY_KEEP_DAYS)).isoformat()
    history = {k: v for k, v in history.items() if v["last"] >= cutoff}

    categories = []
    for key, title in CATEGORIES:
        rows = pick([it for it in pool if it["category"] == key],
                    int(cfg["per_category"]), int(cfg["per_store_per_category"]))
        if rows:
            categories.append({"key": key, "title": title, "items": rows,
                               "total": sum(1 for it in pool if it["category"] == key)})

    shown = [it for c in categories for it in c["items"]]
    top = pick(shown, int(cfg["top_count"]), per_store=3, per_category=2)

    out = {
        "updated": now.astimezone(ZoneInfo("America/Los_Angeles")).isoformat(),
        "min_discount_pct": round(cfg["min_discount"] * 100),
        "total_found": len(pool),
        "stores": status,
        "top": top,
        "categories": categories,
    }

    summary = (f"{ok_count}/{len(stores)} stores scanned, {len(pool)} deals found, "
               f"{len(shown)} shown")
    print(summary)
    if args.dry_run:
        for it in top:
            print(f"  {it['pct']*100:4.0f}%  ${it['price']:>8.2f}  {it['store_name']}: {it['title']}")
        return 0

    write_json(DEALS, out)
    write_json(HISTORY, history)

    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
            fh.write(f"### Bike deals\n{summary}\n\n| Store | Result |\n|---|---|\n")
            for r in status:
                res = (f"{r['scanned']} products, {r['deals']} deals" if r["ok"]
                       else f"❌ {r['error']}")
                fh.write(f"| {r['name']} | {res} |\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
