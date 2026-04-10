#!/usr/bin/env python3
"""
Catawiki Scraper — Playwright (Primary)
========================================
High-performance async scraper for catawiki.com auction listings.

Data extraction strategies (tried in order):
  1. Network interception — capture internal API / GraphQL responses
  2. __NEXT_DATA__ — parse embedded SSR JSON from the HTML
  3. JSON-LD — structured data from <script type="application/ld+json">
  4. DOM parsing — broad CSS selectors as last resort

Features:
  • All categories or custom selection
  • Fingerprint randomisation
  • 2captcha.com CAPTCHA solving
  • 2prx.com proxy support
  • JSON / CSV export
  • --debug flag to dump page HTML for inspection

Usage:
    python catawiki_playwright.py --all --pages 3 --format json
    python catawiki_playwright.py --categories art watches --pages 5 --format csv
    python catawiki_playwright.py --categories art --debug --headed

Requirements:
    pip install playwright 2captcha-python
    playwright install chromium

Repository : https://github.com/2scraper/catawiki-scraper
CAPTCHA    : https://2captcha.com
Proxy      : https://2prx.com
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import random
from typing import Any

from playwright.async_api import async_playwright, Page, BrowserContext, Response

from config import (
    BASE_URL,
    CATEGORIES,
    FINGERPRINT_ENABLED,
    MAX_RETRIES,
    PAGE_LOAD_TIMEOUT,
    PROXY_URL,
    detect_captcha_in_html,
    export_results,
    human_delay,
    log,
    parse_jsonld,
    parse_lots_from_api_json,
    parse_next_data,
    random_user_agent,
    random_viewport,
    save_debug_html,
    solve_captcha_2captcha,
)

# ── Fingerprint pools ───────────────────────────────────────────────────────

TIMEZONES = ["America/New_York", "Europe/London", "Europe/Amsterdam", "Europe/Berlin", "Asia/Tokyo"]
LOCALES   = ["en-US", "en-GB", "nl-NL", "de-DE", "fr-FR"]

# Patterns that indicate an API response carrying lot data
API_LOT_PATTERNS = [
    re.compile(r"/api/", re.I),
    re.compile(r"/graphql", re.I),
    re.compile(r"/buyer/", re.I),
    re.compile(r"/_next/data/", re.I),
    re.compile(r"/search", re.I),
    re.compile(r"/lots", re.I),
    re.compile(r"/categories", re.I),
]


# ── Browser context factory ────────────────────────────────────────────────

async def _new_context(pw, headless: bool = True) -> BrowserContext:
    launch_args: dict[str, Any] = {
        "headless": headless,
        "args": [
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--no-sandbox",
        ],
    }

    if PROXY_URL:
        launch_args["proxy"] = {"server": PROXY_URL}
        log.info("Proxy: %s", PROXY_URL.split("@")[-1] if "@" in PROXY_URL else PROXY_URL)

    browser = await pw.chromium.launch(**launch_args)

    ctx_opts: dict[str, Any] = {"bypass_csp": True, "java_script_enabled": True}

    if FINGERPRINT_ENABLED:
        vp = random_viewport()
        ctx_opts.update({
            "user_agent": random_user_agent(),
            "viewport": vp,
            "screen": vp,
            "locale": random.choice(LOCALES),
            "timezone_id": random.choice(TIMEZONES),
            "color_scheme": random.choice(["light", "dark"]),
        })
        log.info("Fingerprint → UA=%s… viewport=%dx%d",
                 ctx_opts["user_agent"][:40], vp["width"], vp["height"])

    context = await browser.new_context(**ctx_opts)

    await context.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        Object.defineProperty(navigator, 'plugins',   {get: () => [1,2,3,4,5]});
        Object.defineProperty(navigator, 'languages',  {get: () => ['en-US','en','nl']});
        window.chrome = { runtime: {} };
    """)

    return context


# ── CAPTCHA handling ────────────────────────────────────────────────────────

CAPTCHA_INJECT_JS = {
    "turnstile": """(token) => {
        const el = document.querySelector('[name="cf-turnstile-response"]')
                || document.querySelector('input[name*="turnstile"]');
        if (el) el.value = token;
        if (typeof window.turnstileCallback === 'function') window.turnstileCallback(token);
        const form = document.querySelector('form');
        if (form) form.submit();
    }""",
    "recaptcha": """(token) => {
        const el = document.getElementById('g-recaptcha-response');
        if (el) el.innerHTML = token;
        if (typeof ___grecaptcha_cfg !== 'undefined') {
            Object.entries(___grecaptcha_cfg.clients).forEach(([_,v]) => {
                const cb = v?.S?.S?.callback || v?.S?.callback;
                if (typeof cb === 'function') cb(token);
            });
        }
    }""",
    "hcaptcha": """(token) => {
        const el = document.querySelector('[name="h-captcha-response"]');
        if (el) el.value = token;
        const el2 = document.querySelector('[name="g-recaptcha-response"]');
        if (el2) el2.value = token;
    }""",
}


async def _handle_captcha(page: Page) -> bool:
    """Detect & solve CAPTCHA on the current page."""
    html = await page.content()
    result = detect_captcha_in_html(html)
    if not result:
        return False

    captcha_type, sitekey = result
    log.info("CAPTCHA detected: %s (sitekey=%s…)", captcha_type, sitekey[:12])
    token = solve_captcha_2captcha(sitekey, page.url, captcha_type)
    if not token:
        return False

    js = CAPTCHA_INJECT_JS.get(captcha_type)
    if js:
        await page.evaluate(js, token)
        await page.wait_for_load_state("networkidle", timeout=15_000).catch(lambda _: None)
    return True


# ── DOM-based lot extraction (fallback) ─────────────────────────────────────

async def _parse_lots_from_dom(page: Page, category: str) -> list[dict]:
    """
    Extract lots by looking for <a> elements whose href points to lot pages.
    This is the least reliable strategy but works as a catch-all.
    """
    items = await page.evaluate("""
        (baseUrl) => {
            // Collect all links that point to individual lot pages
            const links = document.querySelectorAll('a[href*="/l/"]');
            const seen = new Set();
            const results = [];
            for (const a of links) {
                let href = a.getAttribute('href') || '';
                if (!href.match(/\\/l\\/\\d/)) continue;  // must have /l/<id>
                if (!href.startsWith('http')) href = baseUrl + href;
                if (seen.has(href)) continue;
                seen.add(href);

                // Walk up to find a card-like container (max 5 levels)
                let card = a;
                for (let i = 0; i < 5; i++) {
                    if (card.parentElement && card.parentElement.tagName !== 'BODY'
                        && card.parentElement.tagName !== 'MAIN'
                        && card.parentElement.tagName !== 'HTML') {
                        card = card.parentElement;
                    } else break;
                }

                const text = (sel) => {
                    const el = card.querySelector(sel);
                    return el ? el.innerText.trim() : '';
                };
                const attr = (sel, a) => {
                    const el = card.querySelector(sel);
                    return el ? (el.getAttribute(a) || '') : '';
                };

                results.push({
                    title: text('h3') || text('h2') || text('[class*="itle"]')
                           || a.innerText.trim().split('\\n')[0] || '',
                    url: href,
                    price: text('[class*="rice"]') || text('[class*="bid"]') || '',
                    bids: '',
                    closing_time: text('time') || text('[class*="time"]') || '',
                    image_url: attr('img', 'src') || attr('img', 'data-src') || '',
                });
            }
            return results;
        }
    """, BASE_URL)

    result = []
    for item in items:
        item["category"] = category
        item["id"] = ""
        item["currency"] = "EUR"
        item["seller"] = ""
        item["description"] = ""
        if item.get("title") or item.get("url"):
            result.append(item)

    return result


# ── Category page scraper ──────────────────────────────────────────────────

async def scrape_category(
    context: BrowserContext,
    cat_name: str,
    cat_path: str,
    max_pages: int = 5,
    scrape_details: bool = False,
    debug: bool = False,
) -> list[dict]:
    page = await context.new_page()
    all_items: list[dict] = []

    # ── Network interception: collect API JSONs ────────────────────────
    captured_api_data: list[dict] = []

    async def _on_response(resp: Response):
        url = resp.url
        if resp.status != 200:
            return
        ct = resp.headers.get("content-type", "")
        if "json" not in ct and "javascript" not in ct:
            return
        if any(p.search(url) for p in API_LOT_PATTERNS):
            try:
                body = await resp.json()
                captured_api_data.append({"url": url, "body": body})
            except Exception:
                pass

    page.on("response", _on_response)

    for page_num in range(1, max_pages + 1):
        url = f"{BASE_URL}{cat_path}?page={page_num}"
        log.info("[%s] Page %d → %s", cat_name, page_num, url)
        captured_api_data.clear()

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = await page.goto(url, timeout=PAGE_LOAD_TIMEOUT, wait_until="domcontentloaded")
                status = resp.status if resp else 0
                log.info("[%s] HTTP %d  (final URL: %s)", cat_name, status, page.url)

                if status in (403, 503):
                    log.warning("[%s] Blocked (%d) — checking for CAPTCHA", cat_name, status)

                # Wait for JS to render + fire API calls
                await page.wait_for_timeout(5000)
                try:
                    await page.wait_for_load_state("networkidle", timeout=10_000)
                except Exception:
                    pass

                # Handle CAPTCHA
                solved = await _handle_captcha(page)
                if solved:
                    log.info("[%s] CAPTCHA solved — reloading", cat_name)
                    await page.goto(url, timeout=PAGE_LOAD_TIMEOUT, wait_until="domcontentloaded")
                    await page.wait_for_timeout(5000)

                break
            except Exception as exc:
                log.warning("[%s] Attempt %d/%d: %s", cat_name, attempt, MAX_RETRIES, exc)
                if attempt == MAX_RETRIES:
                    await page.close()
                    return all_items
                human_delay(3, 6)

        # Scroll to trigger lazy-loading
        await page.evaluate("""
            async () => {
                for (let i = 0; i < 8; i++) {
                    window.scrollBy(0, window.innerHeight * 0.8);
                    await new Promise(r => setTimeout(r, 700));
                }
                window.scrollTo(0, 0);
            }
        """)
        await page.wait_for_timeout(2000)

        html = await page.content()

        # Debug: dump everything
        if debug:
            save_debug_html(html, f"{cat_name}_p{page_num}")
            if captured_api_data:
                from pathlib import Path
                Path("debug").mkdir(exist_ok=True)
                for i, cap in enumerate(captured_api_data):
                    fp = Path("debug") / f"{cat_name}_p{page_num}_api{i}.json"
                    fp.write_text(json.dumps(cap, indent=2, default=str), encoding="utf-8")
                log.info("[%s] Dumped %d API responses to debug/", cat_name, len(captured_api_data))

        # ── Try extraction strategies in order ─────────────────────────
        page_items: list[dict] = []

        # Strategy 1: intercepted API responses
        for cap in captured_api_data:
            lots = parse_lots_from_api_json(cap["body"], cat_name)
            if lots:
                log.info("[%s] Strategy API: %d lots from %s", cat_name, len(lots), cap["url"][:80])
                page_items.extend(lots)

        # Strategy 2: __NEXT_DATA__
        if not page_items:
            page_items = parse_next_data(html, cat_name)
            if page_items:
                log.info("[%s] Strategy __NEXT_DATA__: %d lots", cat_name, len(page_items))

        # Strategy 3: JSON-LD
        if not page_items:
            page_items = parse_jsonld(html, cat_name)
            if page_items:
                log.info("[%s] Strategy JSON-LD: %d lots", cat_name, len(page_items))

        # Strategy 4: DOM parsing
        if not page_items:
            page_items = await _parse_lots_from_dom(page, cat_name)
            if page_items:
                log.info("[%s] Strategy DOM: %d lots", cat_name, len(page_items))

        if not page_items:
            log.warning("[%s] No lots found on page %d (HTML length: %d chars)", cat_name, page_num, len(html))
            # Show a preview so the user knows what we're getting
            title = await page.title()
            log.info("[%s] Page title: %s", cat_name, title)
            # Check if it looks like a block/challenge page
            if len(html) < 5000:
                log.warning("[%s] Very short HTML — likely a challenge/block page. "
                            "Set TWOCAPTCHA_API_KEY and PROXY_URL for best results.", cat_name)
            break

        # Deduplicate by URL
        seen = {it["url"] for it in all_items if it.get("url")}
        for it in page_items:
            if it.get("url") and it["url"] not in seen:
                all_items.append(it)
                seen.add(it["url"])
            elif not it.get("url"):
                all_items.append(it)

        log.info("[%s] Page %d: +%d lots (total: %d)", cat_name, page_num, len(page_items), len(all_items))
        human_delay()

    await page.close()
    log.info("[%s] Done — %d lots total", cat_name, len(all_items))
    return all_items


# ── Lot detail scraper ─────────────────────────────────────────────────────

async def scrape_lot_detail(page: Page, url: str) -> dict:
    detail: dict[str, Any] = {"url": url}
    try:
        await page.goto(url, timeout=PAGE_LOAD_TIMEOUT, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)
        await _handle_captcha(page)

        html = await page.content()

        # Try __NEXT_DATA__ for detail page
        match = re.search(
            r'<script\s+id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL
        )
        if match:
            try:
                nd = json.loads(match.group(1))
                pp = nd.get("props", {}).get("pageProps", {})
                lot = pp.get("lot") or pp.get("item") or pp.get("product") or {}
                if lot:
                    detail["title"] = lot.get("title") or lot.get("name") or ""
                    detail["description"] = lot.get("description") or ""
                    detail["current_bid"] = str(
                        lot.get("currentBid", {}).get("amount", "")
                        if isinstance(lot.get("currentBid"), dict) else lot.get("currentBid", "")
                    )
                    seller = lot.get("seller") or lot.get("expert") or {}
                    detail["seller"] = seller.get("name", "") if isinstance(seller, dict) else str(seller)
                    detail["specifications"] = lot.get("specifications") or lot.get("attributes") or {}
                    imgs = lot.get("images") or lot.get("photos") or lot.get("media") or []
                    detail["images"] = [
                        (i.get("url") or i.get("src") or i) if isinstance(i, dict) else i
                        for i in imgs
                    ]
                    return detail
            except Exception:
                pass

        # Fallback: evaluate DOM
        detail.update(await page.evaluate("""
            () => {
                const q = s => { const e = document.querySelector(s); return e ? e.innerText.trim() : ''; };
                return {
                    title: q('h1'),
                    description: q('[class*="escription"]') || q('article'),
                    current_bid: q('[class*="rice"]') || q('[class*="bid"]'),
                };
            }
        """))

    except Exception as exc:
        log.error("Detail scrape failed %s: %s", url, exc)
    return detail


# ── Main ────────────────────────────────────────────────────────────────────

async def main(args: argparse.Namespace):
    if args.all:
        cats = CATEGORIES
    else:
        cats = {c: CATEGORIES[c] for c in args.categories if c in CATEGORIES}
        if not cats:
            log.error("No valid categories. Available: %s", ", ".join(sorted(CATEGORIES)))
            return

    async with async_playwright() as pw:
        context = await _new_context(pw, headless=not args.headed)
        all_results: list[dict] = []

        for name, path in cats.items():
            items = await scrape_category(
                context, name, path,
                max_pages=args.pages,
                scrape_details=args.details,
                debug=args.debug,
            )
            all_results.extend(items)

        await context.browser.close()

    if all_results:
        outfile = export_results(all_results, prefix="catawiki", fmt=args.format)
        log.info("✓ Export: %s (%d items)", outfile, len(all_results))
    else:
        log.warning("No data scraped. Tips:")
        log.warning("  1. Run with --debug --headed to see what the page looks like")
        log.warning("  2. Set PROXY_URL from https://2prx.com to avoid IP blocks")
        log.warning("  3. Set TWOCAPTCHA_API_KEY from https://2captcha.com for CAPTCHA solving")


# ── CLI ─────────────────────────────────────────────────────────────────────

def cli():
    p = argparse.ArgumentParser(
        description="Catawiki Scraper — Playwright",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python catawiki_playwright.py --all --pages 3 --format json
  python catawiki_playwright.py --categories art watches --pages 5 --format csv
  python catawiki_playwright.py --categories art --debug --headed

Environment variables:
  TWOCAPTCHA_API_KEY   Your 2captcha.com API key
  PROXY_URL            Proxy from 2prx.com  (http://user:pass@host:port)
  FINGERPRINT_ENABLED  Enable fingerprint randomisation (default: true)
        """,
    )
    p.add_argument("--categories", nargs="+", default=[],
                   help=f"Categories: {', '.join(sorted(CATEGORIES))}")
    p.add_argument("--all", action="store_true", help="Scrape ALL categories")
    p.add_argument("--pages", type=int, default=3, help="Max pages per category")
    p.add_argument("--format", choices=["json", "csv"], default="json")
    p.add_argument("--details", action="store_true", help="Also scrape lot detail pages")
    p.add_argument("--headed", action="store_true", help="Show browser window")
    p.add_argument("--debug", action="store_true",
                   help="Save page HTML & API responses to debug/ folder")
    return p.parse_args()


if __name__ == "__main__":
    asyncio.run(main(cli()))
