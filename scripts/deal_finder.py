#!/usr/bin/env python3
"""
Daily bike deal finder for mizubikes.com/bike-deals/

Reads   data/deal_stores.yaml     (the store list + thresholds you edit)
Writes  data/deals.json           (today's deals, grouped by category)
        static/deals-all.json     (every deal found, for the page's search box)
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
import html as html_lib
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
ALL = ROOT / "static" / "deals-all.json"    # every deal, for the page's search box

TIMEOUT = 30
PAGE_SIZE = 250                 # Shopify's maximum for products.json
HISTORY_KEEP_DAYS = 45          # forget deals not seen for this long
ENDED_DAYS = 7                  # "Recently ended" shows deals that left in the last week
ENDED_MAX = 30                  # ...and at most this many
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; MizubikesPriceBot/1.0; "
                  "+https://mizubikes.com/bike-deals/)",
    "Accept": "application/json",
}

DEFAULTS = {
    "min_discount": 0.20,       # at least 20% under the store's list price
    "max_discount": 0.80,       # above this the "list price" is usually bogus
    "min_price": 20.0,          # skip small parts and consumables
    "per_category": 20,
    "per_category_page": 120,   # deals listed on each category's own page         # rows shown per category
    "per_store_per_category": 8,  # stop one store from filling a category
    "top_count": 10,            # rows in "Best deals today"
    "max_pages": 100,           # 100 x 250 = 25,000 products per store
    "delay_seconds": 1.0,       # pause between requests to the same store
    "site_brands_only": True,   # only brands the site's own articles mention
    "brand_min_mentions": 2,    # how many times a brand must appear in the articles
    "brands_allow": [],         # always include these brands
    "brands_block": [],         # never include these brands
    "brands_boost": [],         # favorite brands, ranked higher
    "max_age_years": 2,         # skip items whose model year is older than this
    # not every deal has to be 20% off
    "rare_min_discount": 0.10,  # brands/parts that rarely go on sale count from here
    "rare_sale_rate": 0.05,     # a brand is "rarely on sale" when under 5% of its products are discounted
    "rare_sale_brands": [],     # always treated as rarely on sale
    "rare_sale_min_products": 25,  # ...measured over at least this many products
    "big_save_min_discount": 0.12,
    "big_save_min": 400,        # ...or at least $400 off, at 12%+ off
    "popular_terms": [],        # current-generation parts: ranked higher, count from rare_min_discount
    "older_gen_terms": [],      # previous generations: left off the main lists
    "wanted_terms": [],         # product lines people buy; the main lists need a match (see keep_main)
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

USED = re.compile(r"scratch (and|&) dent|cosmetic(ally)? (blem|damage)|pre-?owned|\bused\b|certified|second ?hand|\bdemo\b|open box|blemish|\bblem\b", re.I)

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
    ("drivetrain", re.compile(r"groupset|drivetrain|transmission|t-type|derailleur|cassette|crank|chainring|"
                              r"\bchains?\b|shifter|bottom bracket|\bpedals?\b|\baxs\b|\bdi2\b|"
                              r"upgrade kit")),
    ("cockpit", re.compile(r"handlebar|\bbars?\b|\bstems?\b|seatpost|seat post|saddle|\bgrips?\b|"
                           r"bar tape|headset|cockpit")),
]

def RIM_ONLY(title: str) -> bool:
    """A bare rim (not a wheel or wheelset): few readers build wheels."""
    return bool(re.search(r"\brims?\b(?!\s*(tape|strip|brake|liner))", title, re.I)
                and not re.search(r"wheel", title, re.I))


def HUB_ONLY(title: str) -> bool:
    """A bare hub or freehub body (not a built wheel): few readers build wheels."""
    if re.search(r"thru ?axle|\baxle\b|end ?caps?|hub ?shell|\bbearings?\b|\bspokes?\b|"
                 r"\bnipples?\b|\bpawls?\b|\bskewers?\b", title, re.I) \
            and not re.search(r"wheel|grease|\btool|\boil\b|\bpress\b", title, re.I):
        return True     # hub and wheel spares
    return bool(re.search(r"\b(free)?hubs?\b", title, re.I)
                and not re.search(r"wheel|grease|\btool|\boil\b", title, re.I))


COMPONENT_NOUN = re.compile(r"derailleur|cassette|crank|chainring|shifter|lever|groupset|group ?set|"
                            r"upgrade kit|\bkit\b|wheelset|\bwheels?\b|\brims?\b|\bhubs?\b|\bforks?\b|"
                            r"\bshocks?\b|power ?meter|\bbrakes?\b|caliper|\brotors?\b|seatpost|dropper|"
                            r"handlebar|\bstem\b|cockpit|saddle|\bmotor\b|battery|computer|trainer|"
                            r"\btires?\b|\bpedals?\b|\bchain\b", re.I)
BIKE_TITLE = re.compile(r"\b(road|mountain|gravel|cyclocross|triathlon|tt|fat|electric|e|"
                        r"enduro|trail|xc|commuter|hybrid|kids)[ -]?bike\b|\bbike\b ?[-|,] ?(19|20)\d\d")


# Words dropped from a store's vendor name before looking for it in the articles
BRAND_FILLER = re.compile(r"\b(bikes?|bicycles?|cycles?|cycling|components?|co|company|inc|"
                          r"usa|us|wheels?|tires?|racing|sports?|products?)\b\.?", re.I)
# Brand names that are also everyday words: these must appear capitalized more often
AMBIGUOUS = {"state", "evil", "giant", "hope", "pro", "title", "lake", "ride", "kind",
             "look", "time", "bell", "race", "industry", "union", "wild", "focus", "factor",
             "norco", "scott", "marin", "cube", "all", "one", "chromag", "smith", "thule"} - \
            {"norco", "scott", "marin", "chromag", "thule"}


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


# ---------------------------------------------------------------- BikeTiresDirect

BTD_CARD = re.compile(r"<a class='dpproduct[^']*'[^>]*data-mpc='([^']+)'[^>]*href='([^']+)'>(.*?)<!--end--></a>", re.S)
BTD_SALE_LINK = re.compile(r"""href=['"](/search\?(?:sale|list)=[A-Za-z0-9]+)['"]""", re.I)


def btd_cards(html: str) -> dict:
    """Product cards on a BikeTiresDirect listing -> {mpc: Shopify-like dict}."""
    out = {}
    for mpc, href, body in BTD_CARD.findall(html):
        head = re.search(r"class='dpheading2 dpheading'>(.*?)</div>", body, re.S)
        sale = re.search(r"dpsaleprice2'>\$([\d,.]+)", body)
        was = re.search(r"dpregprice2'>\$([\d,.]+)", body)
        if not (head and sale and was):
            continue
        brand_m = re.search(r"<span class='bolder'>(.*?)</span>", head.group(1))
        brand = html_lib.unescape(brand_m.group(1)).strip() if brand_m else ""
        title = html_lib.unescape(re.sub(r"<[^>]+>", "", head.group(1))).strip()
        title = " ".join(title.split())
        img = re.search(r"<img class='dpimg2 dpmainimg' src='([^']+)'", body)
        sale_v, was_v = sale.group(1).replace(",", ""), was.group(1).replace(",", "")
        out[mpc] = {
            "id": mpc,
            "title": title,
            "vendor": brand,
            "product_type": "",
            "tags": [],
            "url_path": href,
            "images": [{"src": img.group(1)}] if img else [],
            "options": [],
            "variants": [{"id": None, "title": "Default Title", "available": True,
                          "price": sale_v, "compare_at_price": was_v}],
        }
    return out


def fetch_btd(store: dict, cfg: dict) -> list[dict]:
    """Products on BikeTiresDirect's public sale pages. A sale page carries its
    first 32 cards plus the full list of product codes; the rest load in
    batches the same way the page's "See more" button does. robots.txt asks
    for 3 seconds between requests, so we wait at least that long."""
    base = store["base"].rstrip("/")
    wait = max(3.0, float(cfg["delay_seconds"]))
    listings = list(store.get("listings") or [])
    for page in store.get("discover") or []:          # e.g. the Black Friday landing page
        try:
            html = get_html(f"{base}{page}")
            listings += [m for m in BTD_SALE_LINK.findall(html) if m not in listings]
        except RuntimeError as e:
            print(f"      btd: couldn't read {page}: {e}")
        time.sleep(wait)
    cap = int(store.get("max_products_per_sale", 400))
    out, seen = [], set()
    for path in listings:
        try:
            html = get_html(f"{base}{path}")
        except RuntimeError as e:
            print(f"      btd: {path} failed: {e}")
            continue
        cards = btd_cards(html)
        m = re.search(r"var mpcSort\s*=\s*(\[.*?\]);", html, re.S)
        codes = json.loads(m.group(1)) if m else list(cards)
        codes = codes[:cap]
        todo = [c for c in codes if c not in cards]
        for i in range(0, len(todo), 32):
            time.sleep(wait)
            try:
                more = get_html(f"{base}/ajax/searchprod?mpc={'%20'.join(todo[i:i + 32])}&bstk=false")
                cards.update(btd_cards(more))
            except RuntimeError as e:
                print(f"      btd: batch failed on {path}: {e}")
                break
        n = 0
        for c in codes:
            if c in cards and c not in seen:
                seen.add(c)
                out.append(cards[c])
                n += 1
        print(f"      btd: {path}: {n} products read ({len(codes)} listed)")
        time.sleep(wait)
    if not out:
        raise RuntimeError("no products found on the sale pages")
    return out


# ---------------------------------------------------------------- Specialized

PAGE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/131.0 Safari/537.36 MizubikesPriceBot/1.0",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}
NEXT_PUSH = re.compile(r'self\.__next_f\.push\(\[1,("(?:[^"\\]|\\.)*")\]\)', re.S)


def get_html(url: str) -> str:
    for attempt in range(3):
        try:
            r = requests.get(url, headers=PAGE_HEADERS, timeout=TIMEOUT)
            if r.status_code == 200:
                return r.text
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(8 * (attempt + 1))
                continue
            raise RuntimeError(f"HTTP {r.status_code}")
        except requests.RequestException as e:
            if attempt == 2:
                raise RuntimeError(type(e).__name__) from e
            time.sleep(8 * (attempt + 1))
    raise RuntimeError("gave up after 3 tries (rate limited or server errors)")


def next_listing(html: str) -> tuple[list[dict], dict]:
    """Pull the product list and pagination out of a Next.js page. The data
    sits in self.__next_f.push([1,"..."]) chunks as JSON inside JS strings."""
    stream = "".join(json.loads(m.group(1)) for m in NEXT_PUSH.finditer(html))
    if not stream:
        stream = html
    dec = json.JSONDecoder()

    def value_after(key: str, start: int = 0, want: str = "{"):
        for m in re.finditer(r'"%s"\s*:\s*' % key, stream[start:]):
            pos = start + m.end()
            if stream[pos:pos + 1] != want:
                continue
            try:
                val, end = dec.raw_decode(stream, pos)
                return val, end
            except ValueError:
                continue
        return None, -1

    pagination, end = value_after("pagination")
    results, _ = value_after("results", max(end, 0), "[")
    if results is None:
        results, _ = value_after("results", 0, "[")
    results = results or []
    pagination = pagination or {}
    return [r for r in results if isinstance(r, dict)], pagination if isinstance(pagination, dict) else {}


def specialized_product(r: dict) -> dict | None:
    """One Specialized listing result -> the Shopify-like dict the rest of the
    script expects."""
    pid = str(r.get("uid") or (r.get("productData") or {}).get("id") or "")
    name = (r.get("name") or r.get("title") or "").strip()
    if not pid or not name:
        return None
    sale = was = None
    image = r.get("imageUrl") or ""
    for sw in r.get("swatchesJSON") or []:
        cp = sw.get("colorPrices") or {}
        d, f = cp.get("minDiscountPrice"), cp.get("minPrice")
        if d and f and f > d and (sale is None or d / f < sale / was):
            sale, was = d, f
            image = sw.get("plpImageId") or image
    if sale is None:
        return None
    path = (r.get("productData") or {}).get("list") or ""
    sub = (r.get("subhead") or "").strip()
    name = re.sub(r"(?i)\bframesetset\b", "Frameset", name)
    title = name
    if r.get("isBike") and sub and " - " not in name and sub.lower() not in name.lower() \
            and not re.search(r"\d+\s*(nm|w|wh)\b", sub, re.I):
        title = f"{name} - {sub}"
    year = str(r.get("modelYear") or "")
    if re.fullmatch(r"20[12]\d", year) and year not in title:
        title = f"{year} {title}"
    image = re.sub(r"([?&])w=\d+&h=\d+", r"\1w=400&h=400", image.replace("&amp;", "&"))
    return {
        "id": pid,
        "title": title,
        "vendor": "Specialized",
        "product_type": "Bikes" if r.get("isBike") else path.replace("|", " | "),
        "tags": path.split("|"),
        "url_path": r.get("url") or f"/us/en/p/{pid}",
        "images": [{"src": image}] if image else [],
        "options": [],
        "variants": [{"id": None, "title": "Default Title", "available": True,
                      "price": f"{sale:.2f}", "compare_at_price": f"{was:.2f}"}],
    }


def fetch_specialized(store: dict, cfg: dict) -> list[dict]:
    """Products shown on Specialized's public sale listing page(s). Each page
    carries its first 96 products in the HTML; we read only what a visitor's
    page shows, not the site's internal search service."""
    base = store["base"].rstrip("/")
    listings = store.get("listings") or [store.get("listing", "/us/en/shop/black-friday")]
    out, seen, total = [], set(), 0
    for path in listings:
        results, pag = next_listing(get_html(f"{base}{path}"))
        total += int(pag.get("totalResults") or 0)
        for r in results:
            prod = specialized_product(r)
            if prod and prod["id"] not in seen:
                seen.add(prod["id"])
                out.append(prod)
        time.sleep(float(cfg["delay_seconds"]) * 2)
    if not out:
        raise RuntimeError("no product data found on the listing page")
    print(f"      specialized: {len(out)} products read ({total} listed in total on the site)")
    return out


# brands that make parts, not bikes: their products are never "complete bikes"
PARTS_BRANDS = re.compile(r"^(sram|shimano|campagnolo|fox|fox racing|fox factory|rockshox|zipp|dt swiss|"
                          r"quarq|garbaruk|wolf tooth|oneup|chris king|industry nine|hunt|roval|"
                          r"reserve|race ?face|easton|fsa|e\*?thirteen|ohlins|cane creek|magura|hope|"
                          r"trp|hayes|praxis|rotor|power2max|4iiii|stages|garmin|wahoo|hammerhead|"
                          r"maxxis|continental|schwalbe|pirelli|vittoria|wtb|silca|park tool|"
                          r"renthal|deity|thomson|pnw|bikeyoke|ergon|fizik|crankbrothers)\b", re.I)


def categorize(product: dict, price: float) -> str:
    ptype = (product.get("product_type") or "").lower()
    title = (product.get("title") or "").lower()
    tags = product.get("tags") or []
    if isinstance(tags, str):
        tags = tags.split(",")
    text = " | ".join([ptype, title, " ".join(t.strip().lower() for t in tags[:25])])
    head = f"{ptype} | {title}"
    parts_brand = bool(PARTS_BRANDS.search((product.get("vendor") or "").strip())) or \
        bool(PARTS_BRANDS.search(title))

    is_bike = (re.search(r"\bbikes?\b|bicycles?", ptype) and price >= 500) or \
              (BIKE_TITLE.search(title) and price >= 800)
    # a frameset is a frameset even when the store files it under "Bikes"
    if re.search(r"frameset|frame set|frame only|\bframe\b(?!.*\bbike\b)|\b(disc|tt|frame) module\b", title) and price >= 300 \
            and not NOT_A_FRAME.search(head):
        return "framesets"
    if is_bike and not NOT_A_BIKE.search(head) and not parts_brand:
        return "bikes"
    if re.search(r"frameset|frame set|\bframes?\b", head) and price >= 300 \
            and not NOT_A_FRAME.search(head):
        return "framesets"
    # bike money and no component word in the name ("S-Works Stumpjumper 15
    # EVO Di2"): Di2/AXS/Transmission are the bike's spec, not the product
    if price >= 2500 and not parts_brand and not COMPONENT_NOUN.search(title) and not NOT_A_BIKE.search(head) \
            and not re.search(r"frameset|frame set|\bframe\b|\bmodule\b", title):
        return "bikes"
    # "Model - SRAM GX AXS, FOX ..." at bike money: the parts after the dash are
    # the bike's spec, not what is being sold
    model_part = re.split(r"\s[-–|]\s", title, maxsplit=1)[0]
    if price >= 2500 and not parts_brand and model_part != title and not NOT_A_BIKE.search(head) \
            and not any(rx.search(model_part) for _, rx in RULES) \
            and not re.search(r"groupset|wheelset|wheel|fork|frame|kit\b", model_part):
        return "bikes"
    for key, rx in RULES:
        if rx.search(head):
            return key
    # nothing in the name says "component": at this price it is a complete bike
    # (e.g. "Turbo Vado SL 2 4.0"), whatever the store's tags say
    if price >= 1500 and not parts_brand and not NOT_A_BIKE.search(head):
        return "bikes"
    for key, rx in RULES:
        if rx.search(text):
            return key
    return "accessories"


def floor_discount(cfg: dict) -> float:
    """Lowest markdown any rule accepts; the final cut happens after every
    store has been read (see keep_deal)."""
    return min(cfg["min_discount"], cfg["rare_min_discount"], cfg["big_save_min_discount"])


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
        if price < cfg["min_price"] or not (floor_discount(cfg) <= pct <= cfg["max_discount"]):
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


# Words too common to count as a model name when matching titles to articles
GENERIC = {"bike", "bikes", "road", "mountain", "gravel", "disc", "brake", "brakes", "carbon",
           "alloy", "black", "white", "speed", "tire", "tires", "wheel", "wheels", "wheelset",
           "frameset", "frame", "rear", "front", "set", "kit", "with", "and", "for", "the",
           "mens", "men's", "womens", "women's", "tubeless", "ready", "700c", "29", "27.5",
           "size", "small", "medium", "large", "new", "pro", "comp", "expert", "sport"}

# Older standards most riders aren't shopping for
DATED = re.compile(r"octalink|square taper|\bisis\b|\bes5[0-9]\b|\bun[0-9]{2}\b|v-?brake|"
                   r"cantilever|quill|threaded headset|\btriple\b|\b[123] ?x ?(7|8|9|10)\b|\b(7|8|9|10)[ -]?s\b|\b(7|8|9|10)[ -]?(spd|speed|sp|s)\b|\b26 ?(\"|in|inch|er)\b|"
                   r"\b26 ?x|quick release|\bqr\b|straight steerer|1[ -]1/8 straight|"
                   r"\b(pf30|bb30)\b|\b9x100|\b10x135|non-?boost", re.I)
HALF_SET = re.compile(r"[-–,(] ?(front|rear)\b(?! (and|&|\+))|\b(front|rear) (wheel|only)\b|"
                      r"\b(left|right) (only|side)\b|\bleft\b ?\(|lever only", re.I)
# Parts almost nobody is shopping for: left out completely
NOBODY_WANTS = re.compile(r"front derailleur|\bmetrea\b|"
                          r"(shock|coil|air|fork) springs?\b|\bsprings?\b.*\b(lb|lbs|n/mm)\b|"
                          r"spacer kit|volume spacer|bottomless token|\btokens?\b|band ?aid|"
                          r"\b(spacer|spacers)\b.*\b(kit|pack|set)\b|\bfd-\w+|display unit|junction ?box|"
                          r"\b(cable|housing) (set|kit)\b|\bferrules?\b|decals?\b(?! kit)|"
                          r"chain ?catcher|braze-?on|clamp adapter|direct mount adapter", re.I)
# Small spares: listed, but ranked well below real upgrades
SMALL_PARTS = re.compile(r"\b(chainrings?|spacers?|shims?|adapters?|hangers?|bearings?|bushings?|"
                         r"seal kit|service kit|rebuild kit|spare|replacement|end ?caps?|"
                         r"mounts?|top cap|valve stems?|cleats?|barrel adjuster)\b", re.I)
MODEL_YEAR = re.compile(r"\b(20[12]\d)\b")

# How much shoppers come looking for each category
CATEGORY_WEIGHT = {"bikes": 1.10, "framesets": 1.10, "wheels": 1.15, "drivetrain": 1.15,
                   "brakes": 1.05, "suspension": 1.10, "cockpit": 1.00, "tires": 1.10,
                   "electronics": 1.10, "apparel": 0.80, "tools": 0.90, "accessories": 0.75}


def words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", (text or "").lower()))


def term_lists(entries) -> list[list[str]]:
    """"SRAM E1" -> ["sram", "e1"]: an item matches when its brand + title
    contain every word of an entry, in any order. A word ending in * matches
    any word starting with it ("R92*" matches R9200, R9250, R9270)."""
    out = []
    for e in entries or []:
        toks = re.findall(r"[a-z0-9]+\*?", str(e).lower())
        if toks:
            out.append(toks)
    return out


def matches(item_words: set[str], terms: list[list[str]]) -> bool:
    for toks in terms:
        if all((any(w.startswith(t[:-1]) for w in item_words) if t.endswith("*") else t in item_words)
               for t in toks):
            return True
    return False


def relevance(item: dict, n_available: int, brands: "SiteBrands", this_year: int) -> float:
    """Rank by how likely a reader is to want it, not by discount alone."""
    pct, price, title = item["pct"], item["price"], item["title"]
    # discount: full credit up to 45% off, little extra beyond, and a penalty
    # past 60% where the list price is usually inflated or the item is obsolete.
    # Gear that rarely goes on sale gets double credit: 12% off ENVE is news.
    d = pct if pct <= 0.45 else 0.45 + (pct - 0.45) * 0.2
    if item.get("rare") or item.get("popular"):
        d = max(d, min(pct * 2, 0.45))
    if pct > 0.60:
        d -= (pct - 0.60) * 0.8
    s = d * (1 + math.log10(min(max(price, 10), 3000)) / 2)
    s *= brands.weight(item["brand"])                       # brands the site covers most
    s *= 1 + 0.15 * brands.topic_hits(item["brand"], title)  # models the site writes about
    s *= 1 + 0.10 * min(max(n_available - 1, 0), 3)          # several sizes/colors still in stock
    s *= CATEGORY_WEIGHT.get(item["category"], 1.0)
    if item.get("popular"):
        s *= 1.35                                           # current generation people are shopping for
    elif item.get("older_gen"):
        s *= 0.7                                            # previous generation
    if SMALL_PARTS.search(title):
        s *= 0.35
    if HALF_SET.search(title):
        s *= 0.8
    years = [int(y) for y in MODEL_YEAR.findall(title)]
    if years and max(years) <= this_year - 2:    # still allowed, but not current
        s *= 0.85
    return round(s, 4)


class SiteBrands:
    """Answers "does the site write about this brand?" from the article text."""

    def __init__(self, cfg: dict):
        self.on = bool(cfg.get("site_brands_only"))
        self.min = int(cfg.get("brand_min_mentions", 2))
        self.allow = {self.norm(b) for b in cfg.get("brands_allow") or []}
        self.block = {self.norm(b) for b in cfg.get("brands_block") or []}
        self.boost = {self.norm(b) for b in cfg.get("brands_boost") or []}
        self.cache: dict[str, bool] = {}
        self.mentions: dict[str, int] = {}
        self.topic_cache: dict[str, bool] = {}
        self.text = ""
        self.lower = ""
        if True:    # article text is also used for ranking, even with the filter off
            parts = []
            for f in sorted((ROOT / "content" / "posts").rglob("*.md")):
                try:
                    parts.append(f.read_text(encoding="utf-8", errors="ignore"))
                except OSError:
                    pass
            self.text = "\n".join(parts)
            self.lower = self.text.lower()
        if not self.text:               # no articles found: don't filter everything out
            self.on = False

    @staticmethod
    def norm(name: str) -> str:
        return " ".join(BRAND_FILLER.sub(" ", name or "").split()).lower().strip(" -.&")

    @staticmethod
    def listed(key: str, names: set) -> bool:
        """Exact match, match ignoring spaces/hyphens ("raceface" = "race face"),
        or the vendor starts with a listed name ("wahoo fitness" = "wahoo")."""
        squash = key.replace(" ", "").replace("-", "")
        return any(key == n or squash == n.replace(" ", "").replace("-", "")
                   or key.startswith(n + " ") for n in names if n)

    def ok(self, vendor: str) -> bool:
        key = self.norm(vendor)
        if key in self.cache:
            return self.cache[key]
        if not self.on:
            self.cache[key] = True
            self.mentions[key] = self.count(key)
            return True
        if self.listed(key, self.block) or len(key) < 2:
            res = False
        elif self.listed(key, self.allow):
            res = True
        else:
            # brand name as written in an article: must start with a capital letter
            hits = self.count(key)
            res = hits >= (max(self.min, 5) if key in AMBIGUOUS else self.min)
        self.cache[key] = res
        self.mentions.setdefault(key, self.count(key) if res else 0)
        return res

    def count(self, key: str) -> int:
        """Times the brand appears in the articles, written with a capital letter."""
        if not self.text or len(key) < 2:
            return 0
        rx = re.compile(r"(?<![\w-])" + re.escape(key).replace(r"\ ", r"[\s-]?") + r"(?![\w])", re.I)
        return sum(1 for m in rx.finditer(self.text) if m.group(0)[:1].isupper())

    def weight(self, vendor: str) -> float:
        """1.0 for a brand the site never mentions, up to 1.5 for its most-covered brands."""
        key = self.norm(vendor)
        if self.listed(key, self.boost):
            return 1.5
        n = self.mentions.get(key, 0)
        return 1 + min(math.log10(1 + n), 2.0) / 4

    def topic_hits(self, brand: str, title: str) -> int:
        """How many two-word model names from the title the articles also use
        (e.g. "GX Eagle", "SES 3.4"). Capped at 2."""
        words = re.findall(r"[a-z0-9][a-z0-9.\-]*", title.lower())
        skip = set(self.norm(brand).split())
        words = [w for w in words if w not in skip]
        hits = 0
        for a, b in zip(words, words[1:]):
            if len(a) < 2 or len(b) < 2 or (a.isdigit() and b.isdigit()) or a in GENERIC or b in GENERIC:
                continue
            pair = f"{a} {b}"
            if pair not in self.topic_cache:
                self.topic_cache[pair] = pair in self.lower
            if self.topic_cache[pair]:
                hits += 1
                if hits >= 2:
                    break
        return hits


def image_for(product: dict, width: int = 400) -> str:
    """First product photo, sized down when it comes from Shopify's image CDN."""
    imgs = product.get("images") or []
    src = (imgs[0].get("src") if imgs and isinstance(imgs[0], dict) else "") or ""
    if not src.startswith("http"):
        return ""
    if "cdn.shopify.com" in src or "/cdn/shop/" in src:
        src += ("&" if "?" in src else "?") + f"width={width}"
    return src


SIZE_OPTION = re.compile(r"size|length", re.I)


def sizes_on_sale(product: dict, cfg: dict) -> list[str]:
    """Sizes still in stock at a deal price, in the store's own order.
    Empty when the product has no size option (or only one size exists)."""
    opts = product.get("options") or []
    pos = None
    for i, o in enumerate(opts):
        name = o.get("name") if isinstance(o, dict) else str(o)
        if name and SIZE_OPTION.search(name):
            pos = (o.get("position") if isinstance(o, dict) else None) or i + 1
            order = [str(v) for v in (o.get("values") or [])] if isinstance(o, dict) else []
            break
    if pos is None:
        return []
    all_sizes, live = [], []
    for v in product.get("variants") or []:
        size = str(v.get(f"option{pos}") or "").strip()
        if not size:
            continue
        if size not in all_sizes:
            all_sizes.append(size)
        price, was = money(v.get("price")), money(v.get("compare_at_price"))
        if v.get("available") and price and was and was > price \
                and 1 - price / was >= cfg["min_discount"] and size not in live:
            live.append(size)
    if len(all_sizes) < 2:
        return []                       # one-size product: nothing useful to show
    rank = {s: i for i, s in enumerate(order or all_sizes)}
    return sorted(live, key=lambda s: rank.get(s, 999))


KEEP_CAPS = {"SRAM", "ENVE", "WTB", "BMC", "FSA", "TRP", "POC", "KMC", "DT", "MRP", "KS", "PNW",
             "HED", "MET", "SQ", "CADEX", "GT", "YT", "NS", "OPEN", "PRO", "ODI", "CEP", "USWE"}


def tidy_brand(brand: str) -> str:
    """SPECIALIZED -> Specialized, ROCKSHOX -> RockShox stays as the store wrote
    it only when it is a real acronym."""
    if not brand or not brand.isupper() or len(brand) <= 3:
        return brand
    fixed = {"ROCKSHOX": "RockShox", "ONEUP": "OneUp", "RACEFACE": "RaceFace",
             "CRANKBROTHERS": "Crankbrothers", "CAMELBAK": "CamelBak"}
    if brand in fixed:
        return fixed[brand]
    return " ".join(w if w in KEEP_CAPS else w.capitalize() for w in brand.split())


def display_name(brand: str, title: str) -> str:
    """Card title without the repeated brand or a leading model year, so the
    model name is what shows first: "2026 Specialized S-Works Crux Frameset"
    under brand Specialized becomes "S-Works Crux Frameset (2026)"."""
    t = re.sub(r"^\s*(\[[^\]]*\]\s*)+", "", title or "").strip()
    year = ""
    m = re.match(r"(20[12]\d)\s+(.*)", t)
    if m:
        year, t = m.group(1), m.group(2)
    for cut in filter(None, [brand, (brand or "").split()[0] if brand else ""]):
        if len(cut) >= 2 and t.lower().startswith(cut.lower() + " "):
            t = t[len(cut):].lstrip(" -:")
            break
    if not year:                                  # brand first, then the year
        m = re.match(r"(20[12]\d)\s+(.*)", t)
        if m:
            year, t = m.group(1), m.group(2)
    t = t.strip() or title
    if year and year not in t:
        t = f"{t} ({year})"
    return t


def family(item: dict) -> str:
    """Store + brand + first two words of the model, so "URS 01 ONE" and
    "URS 01 TWO" count as one family and don't stack up in a list."""
    brand = (item.get("brand") or "").lower()
    title = (item.get("title") or "").lower()
    if brand and title.startswith(brand):
        title = title[len(brand):]
    words = [w for w in re.findall(r"[a-z0-9]+", title) if not re.fullmatch(r"20[12]\d", w)]
    return f"{item.get('store')}|{brand}|{' '.join(words[:2])}"


def score(item: dict) -> float:
    """Relevance score set in scan_store (rows carried over from an older run fall back)."""
    return item.get("score") or item["pct"] * (1 + math.log10(max(item["price"], 10)) / 2) * 0.5


def scan_store(store: dict, cfg: dict, brands: "SiteBrands", seen: dict) -> tuple[list[dict], int]:
    base = store["base"].rstrip("/")
    kind = store.get("type")
    products = fetch_specialized(store, cfg) if kind == "specialized" \
        else fetch_btd(store, cfg) if kind == "btd" \
        else fetch_catalog(store, cfg)
    this_year = dt.date.today().year
    deals = []
    for p in products:
        title = (p.get("title") or "").strip()
        label = f"{title} {p.get('product_type') or ''}"
        if not title or SKIP.search(label) or USED.search(label):
            continue        # new gear only: used, demo and pre-owned are left out
        vkey = SiteBrands.norm((p.get("vendor") or "").strip())
        if vkey:                                     # how often does this brand go on sale?
            st = seen.setdefault(vkey, [0, 0])
            st[0] += 1
            if any((money(x.get("compare_at_price")) or 0) > (money(x.get("price")) or 0) > 0
                   for x in p.get("variants") or []):
                st[1] += 1
        hit = best_variant(p, cfg)
        if not hit:
            continue
        if not brands.ok((p.get("vendor") or "").strip() or store["name"]):
            continue        # brand the site doesn't write about
        # old standards, old model years and parts nobody shops for stay
        # searchable but never appear in the main lists
        years = [int(y) for y in MODEL_YEAR.findall(title)]
        on_main = not (DATED.search(title) or NOBODY_WANTS.search(title) or RIM_ONLY(title) or HUB_ONLY(title)
                       or (years and max(years) < this_year - int(cfg["max_age_years"])))
        price, was, pct, v = hit
        n_avail = sum(1 for x in p.get("variants") or []
                      if x.get("available") and (money(x.get("compare_at_price")) or 0)
                      > (money(x.get("price")) or 0) > 0)
        if p.get("url_path"):
            url = f"{base}{p['url_path']}"
        else:
            url = f"{base}/products/{p.get('handle')}"
            if v.get("id"):
                url += f"?variant={v['id']}"
        href, sponsored = link_for(store, url)
        vtitle = (v.get("title") or "").strip()
        brand = (p.get("vendor") or "").strip()
        if brand and title.lower().startswith(brand.lower()):
            brand = title[:len(brand)]      # use the title's spelling, e.g. SPECIALIZED -> Specialized
        elif brand and len(brand.split()[0]) >= 3 and \
                title.lower().startswith(brand.split()[0].lower() + " "):
            brand = title[:len(brand.split()[0])]   # "BMC Bikes" + "BMC URS 01" -> "BMC"
        brand = tidy_brand(brand)
        category = categorize(p, price)
        tag = (cfg.get("amazon_tag") or "").strip()
        deals.append({
            "key": f"{store['id']}:{p.get('id')}",
            "store": store["id"],
            "store_name": store["name"],
            "title": title,
            "name": display_name(brand, title),
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
            "image": image_for(p),
            "sizes": sizes_on_sale(p, {**cfg, "min_discount": floor_discount(cfg)}),
            "vendor_key": vkey,
            "stock": n_avail,
            "stale": False,
            "main": on_main,
        })
        deals[-1]["score"] = relevance(deals[-1], n_avail, brands, this_year)
    return deals, len(products)


def pick(pool: list[dict], limit: int, per_store: int, per_category: int | None = None,
         per_family: int = 1):
    """Top `limit` by score, capped per store, per category and per model
    family so one shop, category or product line can't dominate."""
    out, by_store, by_cat, by_fam = [], {}, {}, {}
    for it in sorted(pool, key=score, reverse=True):
        fam = family(it)
        if by_store.get(it["store"], 0) >= per_store:
            continue
        if per_category is not None and by_cat.get(it["category"], 0) >= per_category:
            continue
        if by_fam.get(fam, 0) >= per_family:
            continue
        out.append(it)
        by_store[it["store"]] = by_store.get(it["store"], 0) + 1
        by_cat[it["category"]] = by_cat.get(it["category"], 0) + 1
        by_fam[fam] = by_fam.get(fam, 0) + 1
        if len(out) >= limit:
            break
    return out


def brand_key(it: dict) -> str:
    """First word of the brand, so "Wolf Tooth Components" and "Wolf Tooth" match."""
    b = (it.get("brand") or it.get("store_name") or "").strip().lower()
    return b.split()[0] if b else ""


def mix_brands(items: list[dict], window: int = 3) -> list[dict]:
    """Keep the best-first order, but don't show the same brand again until
    `window` other cards have gone by (when there's anything else left to show).
    Stops one brand, like a brand's own store, filling a whole row."""
    left, out = list(items), []
    while left:
        recent = {brand_key(x) for x in out[-window:]}
        nxt = next((x for x in left if brand_key(x) not in recent), left[0])
        out.append(nxt)
        left.remove(nxt)
    return out


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print results, write nothing")
    ap.add_argument("--only", help="scan just these store ids (comma-separated), for testing")
    args = ap.parse_args()

    raw = yaml.safe_load(STORES.read_text(encoding="utf-8")) or {}
    cfg = {**DEFAULTS, **(raw.get("settings") or {})}
    # stores flagged `used: true` sell mostly second-hand gear and are skipped
    stores = [s for s in raw.get("stores", [])
              if s.get("enabled", True) and not s.get("used")]
    if args.only:
        wanted = {x.strip() for x in args.only.split(",")}
        stores = [s for s in raw.get("stores", []) if s["id"] in wanted]

    brands = SiteBrands(cfg)
    old = load_json(DEALS, {})
    history = load_json(HISTORY, {})
    first_run = not history

    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    today = now.astimezone(ZoneInfo("America/Los_Angeles")).date()
    today_s = today.isoformat()

    old_items = [it for c in old.get("categories", []) for it in c.get("items", [])
                 if not USED.search(it.get("title", "")) and brands.ok(it.get("brand", ""))
                 and not DATED.search(it.get("title", ""))
                 and not NOBODY_WANTS.search(it.get("title", ""))]
    pool, status, ok_count = [], [], 0
    seen: dict[str, list[int]] = {}          # brand -> [products, products on sale]

    for s in stores:
        rec = {"id": s["id"], "name": s["name"], "home": s["base"].rstrip("/")}
        try:
            deals, scanned = scan_store(s, cfg, brands, seen)
            pool.extend(deals)
            rec.update({"ok": True, "scanned": scanned, "deals": len(deals)})
            ok_count += 1
            print(f"OK    {s['id']:24s} {scanned:6d} products  {len(deals):5d} deals")
        except Exception as e:  # keep yesterday's rows for this store, flagged stale
            # keep yesterday's rows while a store is briefly down, but not for
            # more than two days (e.g. a sale page that has been taken down)
            stale_cutoff = (today - dt.timedelta(days=2)).isoformat()
            carried = [dict(it, stale=True) for it in old_items if it.get("store") == s["id"]
                       and history.get(it.get("key"), {}).get("last", "") >= stale_cutoff]
            pool.extend(carried)
            rec.update({"ok": False, "error": str(e)[:200], "deals": len(carried)})
            print(f"FAIL  {s['id']:24s} {e}  (kept {len(carried)} from last run)")
        status.append(rec)

    if ok_count == 0:
        print("No store could be scanned — leaving yesterday's data in place.")
        return 1

    # ---- which deals make the cut ----
    # 20%+ off as before, OR 10%+ off gear that rarely goes on sale (brands
    # that discount under 8% of their catalog, the rare_sale_brands list, or
    # current-generation parts on popular_terms), OR 12%+ off with $400+ saved.
    rare_brands = {SiteBrands.norm(b) for b in cfg.get("rare_sale_brands") or []}
    for k, (n, on_sale) in seen.items():
        if n >= int(cfg["rare_sale_min_products"]) and on_sale / n < float(cfg["rare_sale_rate"]):
            rare_brands.add(k)
    popular, older = term_lists(cfg.get("popular_terms")), term_lists(cfg.get("older_gen_terms"))
    wanted = term_lists(cfg.get("wanted_terms"))
    this_year = today.year
    kept = []
    for it in pool:
        if it.get("stale"):
            kept.append(it)
            continue
        w = words(f"{it.get('brand', '')} {it['title']}")
        bkey = it.get("vendor_key") or SiteBrands.norm(it.get("brand", ""))
        it["rare"] = bkey in rare_brands or SiteBrands.listed(bkey, rare_brands)
        it["popular"] = matches(w, popular)
        it["older_gen"] = not it["popular"] and matches(w, older)
        pct = it["pct"]
        ok = (pct >= cfg["min_discount"]
              or ((it["rare"] or it["popular"]) and pct >= cfg["rare_min_discount"])
              or (pct >= cfg["big_save_min_discount"] and it["save"] >= float(cfg["big_save_min"])))
        if ok:
            it["score"] = relevance(it, int(it.get("stock") or 1), brands, this_year)
            # main lists and homepage: only gear with a reason to be there.
            # Everything else stays in search.
            years = [int(y) for y in MODEL_YEAR.findall(it["title"])]
            current_bike = it["category"] in ("bikes", "framesets") and \
                (not years or max(years) >= this_year - 1)
            reason = it["popular"] or it["rare"] or matches(w, wanted) or current_bike
            it["main"] = bool(it.get("main", True) and not it["older_gen"] and reason)
            kept.append(it)
    print(f"Main lists: {sum(1 for i in kept if i.get('main'))} of {len(kept)} deals qualify "
          f"(current gen, wanted lines, rarely-on-sale brands, current bikes)")
    print(f"Deal cut: {len(kept)} of {len(pool)} candidates kept; "
          f"{sum(1 for i in kept if i.get('rare'))} rarely-on-sale, "
          f"{sum(1 for i in kept if i.get('popular'))} current-gen, "
          f"{sum(1 for i in kept if i.get('pct', 1) < cfg['min_discount'])} under {cfg['min_discount']:.0%} off")
    print("Rarely-on-sale brands: " + ", ".join(sorted(b for b in rare_brands if b in seen)[:80]))
    pool = kept

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

    main_pool = [it for it in pool if it.get("main", True)]
    categories = []
    for key, title in CATEGORIES:
        cand = [it for it in main_pool if it["category"] == key]
        # first per_category: the balanced top picks the main page shows;
        # then the rest, up to per_category_page, for the category's own page
        rows = pick(cand, int(cfg["per_category"]), int(cfg["per_store_per_category"]))
        chosen = {it["key"] for it in rows}
        more_n = max(int(cfg["per_category_page"]) - len(rows), 0)
        rest = pick([it for it in cand if it["key"] not in chosen], more_n,
                    max(int(cfg["per_category_page"]) // 3, 1))
        rows = mix_brands(rows) + mix_brands(rest)
        if rows:
            categories.append({"key": key, "title": title, "items": rows,
                               "main_total": len(cand),
                               "total": sum(1 for it in pool if it["category"] == key)})

    shown = [it for c in categories for it in c["items"][:int(cfg["per_category"])]]

    # remember what each listed deal looked like, so it can be shown as
    # "recently ended" once it drops out of the scan
    for it in shown:
        history[it["key"]]["info"] = {
            "title": it.get("name") or it["title"], "brand": it["brand"], "variant": it["variant"],
            "store": it["store"], "store_name": it["store_name"], "category": it["category"],
            "price": it["price"], "was": it["was"], "pct": it["pct"], "url": it["url"],
            "sponsored": it.get("sponsored", False),
        }
    live = {it["key"] for it in pool}
    ok_stores = {r["id"] for r in status if r["ok"]}
    ended_cutoff = (today - dt.timedelta(days=ENDED_DAYS)).isoformat()
    ended = []
    for key, h in history.items():
        info = h.get("info")
        if not info or key in live:
            continue
        if h["last"] < ended_cutoff:
            del h["info"]               # too long ago: stop carrying the details
            continue
        # only call it ended if its store was actually scanned today
        if h["last"] < today_s and info["store"] in ok_stores:
            ended.append({**info, "last_seen": h["last"], "since": h["first"]})
    ended.sort(key=lambda e: (e["last_seen"], e["pct"]), reverse=True)
    ended = ended[:ENDED_MAX]
    top = mix_brands(pick(shown, int(cfg["top_count"]), per_store=3, per_category=2))

    out = {
        "updated": now.astimezone(ZoneInfo("America/Los_Angeles")).isoformat(),
        "min_discount_pct": round(cfg["min_discount"] * 100),
        "total_found": len(pool),
        "stores": status,
        "top": top,
        "categories": categories,
        "ended": ended,
    }

    summary = (f"{ok_count}/{len(stores)} stores scanned, {len(pool)} deals found, "
               f"{len(shown)} shown, {len(ended)} recently ended")
    if brands.on:
        kept = sorted(k for k, v in brands.cache.items() if v)
        dropped = sorted(k for k, v in brands.cache.items() if not v)
        print(f"Brands kept ({len(kept)}): {', '.join(kept)}")
        print(f"Brands skipped ({len(dropped)}): {', '.join(dropped[:150])}")
    print(summary)
    if args.dry_run:
        for it in top:
            print(f"  {it['pct']*100:4.0f}%  ${it['price']:>8.2f}  {it['store_name']}: {it['title']}")
        for c in categories:
            print(f"  [{c['key']}] {c['total']} found")
            for it in c["items"][:5]:
                print(f"      {it['pct']*100:3.0f}%  ${it['price']:>8.2f}  {it.get('name') or it['title']}"
                      f"  {it['url']}  img={'y' if it.get('image') else 'n'}")
        return 0

    write_json(DEALS, out)
    write_json(HISTORY, history)

    # compact full list for the search box: one array per deal, best first
    rows = [[it.get("name") or it["title"], it["brand"], it["variant"], it["store_name"], it["price"], it["was"],
             round(it["pct"] * 100), it["category"], it["url"], it.get("amazon", ""),
             1 if it.get("sponsored") else 0, it.get("image", ""),
             " · ".join(it.get("sizes") or [])]
            for it in sorted(pool, key=score, reverse=True)]
    ALL.parent.mkdir(parents=True, exist_ok=True)
    ALL.write_text(json.dumps({"updated": out["updated"], "cats": dict(CATEGORIES), "items": rows},
                              ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")

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
