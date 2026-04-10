"""
Catawiki Scraper — Configuration & Shared Utilities
https://github.com/2scraper/catawiki-scraper

Shared settings for Playwright, Selenium, and Puppeteer scrapers.
Integrates with 2captcha.com (CAPTCHA solving) and 2prx.com (proxy).

Data extraction strategy (in priority order):
  1. Intercept internal API / GraphQL responses (Playwright & Puppeteer)
  2. Parse __NEXT_DATA__ JSON embedded in the HTML (all engines)
  3. DOM element parsing with broad selectors (fallback)
"""

import os
import csv
import json
import time
import random
import logging
import re
from pathlib import Path
from datetime import datetime

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("catawiki-scraper")

# ---------------------------------------------------------------------------
# 2captcha.com integration
# Docs: https://2captcha.com/api-docs
# ---------------------------------------------------------------------------
TWOCAPTCHA_API_KEY: str = os.getenv("TWOCAPTCHA_API_KEY", "")

# ---------------------------------------------------------------------------
# 2prx.com proxy  — format: http://user:pass@host:port
# ---------------------------------------------------------------------------
PROXY_URL: str = os.getenv("PROXY_URL", "")

# ---------------------------------------------------------------------------
# Anti-detect / fingerprint settings
# ---------------------------------------------------------------------------
FINGERPRINT_ENABLED: bool = os.getenv("FINGERPRINT_ENABLED", "true").lower() == "true"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.5; rv:126.0) Gecko/20100101 Firefox/126.0",
]

VIEWPORT_SIZES = [
    {"width": 1920, "height": 1080},
    {"width": 1366, "height": 768},
    {"width": 1536, "height": 864},
    {"width": 1440, "height": 900},
    {"width": 1280, "height": 720},
]

# ---------------------------------------------------------------------------
# Scraping settings
# ---------------------------------------------------------------------------
BASE_URL = "https://www.catawiki.com"
REQUEST_DELAY_MIN = 1.5
REQUEST_DELAY_MAX = 4.0
MAX_RETRIES = 3
PAGE_LOAD_TIMEOUT = 60_000       # ms
SELENIUM_TIMEOUT = 60            # seconds

OUTPUT_DIR = Path("output")
OUTPUT_DIR.mkdir(exist_ok=True)
DEBUG_DIR = Path("debug")

# ---------------------------------------------------------------------------
# Catawiki categories
#
# URL format: /en/c/<id>-<slug>
# The numeric IDs may shift; the slug is the stable part.
# If a page 404s, the scraper logs it and moves on.
# ---------------------------------------------------------------------------
CATEGORIES = {
    "art":                  "/en/c/501-art",
    "antiques":             "/en/c/505-antiques",
    "asian-art":            "/en/c/1463-asian-art",
    "books-comics":         "/en/c/513-books-and-comics",
    "cars-motorcycles":     "/en/c/585-cars-and-motorcycles",
    "ceramics":             "/en/c/545-ceramics",
    "coins-banknotes":      "/en/c/509-coins-and-banknotes",
    "collectibles":         "/en/c/587-collectibles",
    "diamonds-gemstones":   "/en/c/557-diamonds-and-gemstones",
    "dolls-bears":          "/en/c/593-dolls-and-bears",
    "fashion":              "/en/c/559-fashion",
    "furniture":            "/en/c/547-furniture",
    "interiors":            "/en/c/2099-interiors-and-decorations",
    "jewellery":            "/en/c/555-jewellery",
    "memorabilia":          "/en/c/595-memorabilia",
    "militaria":            "/en/c/597-militaria",
    "model-cars":           "/en/c/599-model-cars",
    "music":                "/en/c/515-music",
    "photography":          "/en/c/519-photography",
    "science-technology":   "/en/c/601-science-and-technology",
    "sculptures":           "/en/c/507-sculptures-and-carvings",
    "silver":               "/en/c/553-silver",
    "sports":               "/en/c/521-sports-memorabilia",
    "stamps":               "/en/c/511-stamps",
    "toys":                 "/en/c/603-toys-and-models",
    "vinyl-music":          "/en/c/517-vinyl-and-music-equipment",
    "watches":              "/en/c/563-watches",
    "wine-whisky":          "/en/c/583-wines-and-spirits",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def random_user_agent() -> str:
    return random.choice(USER_AGENTS)

def random_viewport() -> dict:
    return random.choice(VIEWPORT_SIZES)

def human_delay(minimum: float = REQUEST_DELAY_MIN, maximum: float = REQUEST_DELAY_MAX):
    time.sleep(random.uniform(minimum, maximum))

def timestamped_filename(prefix: str, ext: str) -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{prefix}_{ts}.{ext}"

def save_debug_html(html: str, label: str) -> Path:
    DEBUG_DIR.mkdir(exist_ok=True)
    fp = DEBUG_DIR / f"{label}_{datetime.now().strftime('%H%M%S')}.html"
    fp.write_text(html, encoding="utf-8")
    log.info("Debug HTML saved → %s (%d bytes)", fp, len(html))
    return fp


# ═══════════════════════════════════════════════════════════════════════════
# DATA EXTRACTION — three strategies
# ═══════════════════════════════════════════════════════════════════════════

def _jget(d: dict, *keys):
    """Return the first truthy value for the given keys."""
    for k in keys:
        v = d.get(k)
        if v is not None:
            return v
    return None


def _extract_lot(lot: dict, category: str = "") -> dict | None:
    """Normalise a single lot dict from any known API shape."""
    if not isinstance(lot, dict):
        return None

    item = {
        "id":           str(_jget(lot, "id", "lotId", "lot_id", "objectId") or ""),
        "title":        _jget(lot, "title", "name", "lotTitle", "displayTitle") or "",
        "url":          "",
        "price":        "",
        "currency":     _jget(lot, "currency", "currencyCode") or "EUR",
        "bids":         str(_jget(lot, "bidCount", "bid_count", "numberOfBids", "numBids") or ""),
        "closing_time": _jget(lot, "closingDate", "closing_date", "endDate", "endsAt", "closesAt") or "",
        "image_url":    "",
        "category":     category,
        "seller":       "",
        "description":  _jget(lot, "description", "subtitle", "lotSubtitle", "shortDescription") or "",
    }

    # --- price ---
    bid = _jget(lot, "currentBid", "current_bid", "currentBidAmount",
                "highestBid", "bidAmount", "price", "estimatedValue")
    if isinstance(bid, dict):
        amt = bid.get("amount") or bid.get("value") or bid.get("cents", 0)
        cur = bid.get("currency") or bid.get("currencyCode") or item["currency"]
        # cents → main unit
        if isinstance(amt, (int, float)) and amt > 10_000 and "cent" in str(list(bid.keys())).lower():
            amt = amt / 100
        item["price"] = f"{amt} {cur}".strip()
    elif bid is not None:
        item["price"] = str(bid)

    # --- url ---
    raw_url = _jget(lot, "url", "slug", "seoUrl", "path", "lotUrl", "href")
    if raw_url and str(raw_url).startswith("http"):
        item["url"] = raw_url
    elif raw_url and str(raw_url).startswith("/"):
        item["url"] = f"{BASE_URL}{raw_url}"
    elif raw_url:
        item["url"] = f"{BASE_URL}/en/l/{raw_url}"
    elif item["id"]:
        item["url"] = f"{BASE_URL}/en/l/{item['id']}"

    # --- image ---
    img = _jget(lot, "imageUrl", "image_url", "thumbnailUrl", "imageUri",
                "coverImageUrl", "primaryImageUrl", "image")
    if isinstance(img, dict):
        img = img.get("url") or img.get("src") or img.get("uri") or ""
    if not img:
        images = lot.get("images") or lot.get("photos") or lot.get("media") or []
        if images and isinstance(images[0], dict):
            img = images[0].get("url") or images[0].get("src") or ""
        elif images and isinstance(images[0], str):
            img = images[0]
    item["image_url"] = img or ""

    # --- seller ---
    seller = lot.get("seller") or lot.get("expert") or lot.get("sellerInfo") or {}
    if isinstance(seller, dict):
        item["seller"] = seller.get("name") or seller.get("nickname") or seller.get("displayName") or ""
    elif isinstance(seller, str):
        item["seller"] = seller

    if item["title"] or item["id"]:
        return item
    return None


# ── Strategy 1: API JSON response ──────────────────────────────────────────

def parse_lots_from_api_json(json_data, category: str = "") -> list[dict]:
    """
    Walk an arbitrary API/GraphQL JSON response and extract lots.
    Handles nested shapes: {lots:[…]}, {data:{lots:[…]}}, {props:{pageProps:{…}}}, etc.
    """
    found = _find_lot_lists(json_data)
    items = []
    for lst in found:
        for raw in lst:
            lot = _extract_lot(raw, category)
            if lot:
                items.append(lot)
    return items


def _find_lot_lists(obj, depth=0) -> list[list]:
    """Recursively find lists of dicts that look like lots (max depth 6)."""
    if depth > 6:
        return []
    results = []
    if isinstance(obj, list) and len(obj) > 0 and isinstance(obj[0], dict):
        # Check if items look like lots (have title/id/name/price-related keys)
        sample = obj[0]
        lot_keys = {"title", "name", "lotTitle", "id", "lotId", "currentBid",
                     "current_bid", "price", "imageUrl", "image_url", "url", "slug"}
        if lot_keys & set(sample.keys()):
            results.append(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            results.extend(_find_lot_lists(v, depth + 1))
    return results


# ── Strategy 2: __NEXT_DATA__ from HTML ────────────────────────────────────

def parse_next_data(html: str, category: str = "") -> list[dict]:
    """Extract lots from the __NEXT_DATA__ script tag (Next.js SSR)."""
    match = re.search(
        r'<script\s+id="__NEXT_DATA__"\s+type="application/json"[^>]*>(.*?)</script>',
        html, re.DOTALL,
    )
    if not match:
        # Try alternate patterns
        match = re.search(r'__NEXT_DATA__\s*=\s*({.*?});?\s*</script>', html, re.DOTALL)
    if not match:
        return []

    try:
        data = json.loads(match.group(1))
        log.info("Found __NEXT_DATA__ (%d chars)", len(match.group(1)))
        lots = parse_lots_from_api_json(data, category)
        if lots:
            log.info("Extracted %d lots from __NEXT_DATA__", len(lots))
        return lots
    except (json.JSONDecodeError, Exception) as exc:
        log.warning("Failed to parse __NEXT_DATA__: %s", exc)
        return []


# ── Strategy 3: JSON-LD structured data from HTML ──────────────────────────

def parse_jsonld(html: str, category: str = "") -> list[dict]:
    """Extract product/lot data from JSON-LD <script> tags."""
    items = []
    for match in re.finditer(
        r'<script\s+type="application/ld\+json"[^>]*>(.*?)</script>',
        html, re.DOTALL,
    ):
        try:
            data = json.loads(match.group(1))
            if isinstance(data, list):
                for entry in data:
                    lot = _extract_lot_from_jsonld(entry, category)
                    if lot:
                        items.append(lot)
            elif isinstance(data, dict):
                # ItemList
                if data.get("@type") == "ItemList" and "itemListElement" in data:
                    for elem in data["itemListElement"]:
                        item_data = elem.get("item") or elem
                        lot = _extract_lot_from_jsonld(item_data, category)
                        if lot:
                            items.append(lot)
                else:
                    lot = _extract_lot_from_jsonld(data, category)
                    if lot:
                        items.append(lot)
        except json.JSONDecodeError:
            continue

    if items:
        log.info("Extracted %d lots from JSON-LD", len(items))
    return items


def _extract_lot_from_jsonld(data: dict, category: str) -> dict | None:
    if not isinstance(data, dict):
        return None
    t = data.get("@type", "")
    if t not in ("Product", "Offer", "Thing", "IndividualProduct", ""):
        return None

    name = data.get("name") or data.get("title") or ""
    url = data.get("url") or ""
    img = data.get("image") or ""
    if isinstance(img, list):
        img = img[0] if img else ""
    if isinstance(img, dict):
        img = img.get("url") or ""

    price = ""
    offers = data.get("offers")
    if isinstance(offers, dict):
        price = f"{offers.get('price', '')} {offers.get('priceCurrency', '')}".strip()
    elif isinstance(offers, list) and offers:
        o = offers[0]
        price = f"{o.get('price', '')} {o.get('priceCurrency', '')}".strip()

    desc = data.get("description") or ""

    if not name:
        return None

    return {
        "id": "",
        "title": name,
        "url": url,
        "price": price,
        "currency": "",
        "bids": "",
        "closing_time": "",
        "image_url": img,
        "category": category,
        "seller": "",
        "description": desc[:300],
    }


# ---------------------------------------------------------------------------
# CAPTCHA solving via 2captcha.com
# ---------------------------------------------------------------------------

def detect_captcha_in_html(html: str) -> tuple[str, str] | None:
    """
    Scan HTML for known CAPTCHA challenges.
    Returns (captcha_type, sitekey) or None.
    """
    sitekey_match = re.search(r'data-sitekey=["\']([^"\']+)["\']', html)
    sitekey = sitekey_match.group(1) if sitekey_match else None

    if ("turnstile" in html.lower() or "cf-turnstile" in html.lower()) and sitekey:
        return ("turnstile", sitekey)
    if ("g-recaptcha" in html or "grecaptcha" in html) and sitekey:
        return ("recaptcha", sitekey)
    if ("hcaptcha" in html.lower() or "h-captcha" in html.lower()) and sitekey:
        return ("hcaptcha", sitekey)
    # Cloudflare challenge page (no sitekey visible)
    if "challenge-platform" in html.lower() or "cf-chl-widget" in html.lower():
        # Try extracting from Turnstile render call
        render_match = re.search(r'turnstile\.render\([^,]+,\s*\{[^}]*sitekey:\s*["\']([^"\']+)', html)
        if render_match:
            return ("turnstile", render_match.group(1))
    return None


def solve_captcha_2captcha(site_key: str, page_url: str, captcha_type: str = "turnstile") -> str | None:
    """Solve a CAPTCHA via 2captcha.com. Docs: https://2captcha.com/api-docs"""
    if not TWOCAPTCHA_API_KEY:
        log.warning("TWOCAPTCHA_API_KEY not set — cannot solve CAPTCHA")
        return None

    try:
        from twocaptcha import TwoCaptcha
    except ImportError:
        log.error("pip install 2captcha-python")
        return None

    solver = TwoCaptcha(TWOCAPTCHA_API_KEY)
    log.info("Sending %s CAPTCHA to 2captcha.com (sitekey=%s…)", captcha_type, site_key[:12])

    try:
        if captcha_type == "turnstile":
            result = solver.turnstile(sitekey=site_key, url=page_url)
        elif captcha_type == "recaptcha":
            result = solver.recaptcha(sitekey=site_key, url=page_url)
        elif captcha_type == "hcaptcha":
            result = solver.hcaptcha(sitekey=site_key, url=page_url)
        else:
            log.error("Unknown captcha_type: %s", captcha_type)
            return None
        token = result.get("code")
        log.info("CAPTCHA solved (token length: %d)", len(token) if token else 0)
        return token
    except Exception as exc:
        log.error("2captcha failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Data export
# ---------------------------------------------------------------------------

def save_json(data: list[dict], filepath: Path | str) -> None:
    filepath = Path(filepath)
    with open(filepath, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    log.info("Saved %d items → %s", len(data), filepath)


def save_csv(data: list[dict], filepath: Path | str) -> None:
    filepath = Path(filepath)
    if not data:
        log.warning("No data to save")
        return
    keys = list(data[0].keys())
    with open(filepath, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=keys)
        writer.writeheader()
        writer.writerows(data)
    log.info("Saved %d items → %s", len(data), filepath)


def export_results(data: list[dict], prefix: str = "catawiki", fmt: str = "json") -> Path:
    filename = timestamped_filename(prefix, fmt)
    filepath = OUTPUT_DIR / filename
    if fmt == "csv":
        save_csv(data, filepath)
    else:
        save_json(data, filepath)
    return filepath
