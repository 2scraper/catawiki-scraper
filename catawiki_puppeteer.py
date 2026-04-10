#!/usr/bin/env python3
"""
Catawiki Scraper — Puppeteer (Pyppeteer)
=========================================
Async scraper for catawiki.com using Pyppeteer (Python Puppeteer port).

Data extraction strategies (tried in order):
  1. Network interception — capture internal API / GraphQL responses
  2. __NEXT_DATA__ — parse embedded SSR JSON from the HTML
  3. JSON-LD — structured data
  4. DOM parsing — broad CSS selectors as last resort

Usage:
    python catawiki_puppeteer.py --categories art watches --pages 3 --format json
    python catawiki_puppeteer.py --all --format csv
    python catawiki_puppeteer.py --categories art --debug --headed

Requirements:
    pip install pyppeteer 2captcha-python

Repository : https://github.com/2scraper/catawiki-scraper
CAPTCHA    : https://2captcha.com
Proxy      : https://2prx.com
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from typing import Any

import pyppeteer
from pyppeteer import launch
from pyppeteer.page import Page

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

# Patterns for API responses carrying lots
API_PATTERNS = ["/api/", "/graphql", "/buyer/", "/_next/data/", "/lots", "/search", "/categories"]


# ── Browser factory ─────────────────────────────────────────────────────────

async def create_browser(headed: bool = False):
    args = [
        "--disable-blink-features=AutomationControlled",
        "--disable-dev-shm-usage",
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-infobars",
        "--lang=en-US,en",
    ]
    vp = random_viewport() if FINGERPRINT_ENABLED else {"width": 1920, "height": 1080}
    args.append(f"--window-size={vp['width']},{vp['height']}")

    if PROXY_URL:
        args.append(f"--proxy-server={PROXY_URL}")
        log.info("Proxy: %s", PROXY_URL.split("@")[-1] if "@" in PROXY_URL else PROXY_URL)

    browser = await launch(headless=not headed, args=args, ignoreHTTPSErrors=True, autoClose=False)
    return browser, vp


async def new_page(browser, vp: dict) -> Page:
    page = await browser.newPage()
    ua = random_user_agent() if FINGERPRINT_ENABLED else (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125.0.0.0 Safari/537.36"
    )
    await page.setUserAgent(ua)
    await page.setViewport(vp)
    await page.evaluateOnNewDocument("""
        () => {
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'plugins',   {get: () => [1,2,3,4,5]});
            Object.defineProperty(navigator, 'languages',  {get: () => ['en-US','en','nl']});
            window.chrome = { runtime: {} };
            const origQuery = window.navigator.permissions.query;
            window.navigator.permissions.query = (p) =>
                p.name === 'notifications'
                    ? Promise.resolve({state: Notification.permission})
                    : origQuery(p);
        }
    """)
    if FINGERPRINT_ENABLED:
        log.info("Fingerprint → UA=%s… viewport=%dx%d", ua[:40], vp["width"], vp["height"])
    return page


# ── CAPTCHA handling ────────────────────────────────────────────────────────

async def handle_captcha(page: Page) -> bool:
    html = await page.content()
    result = detect_captcha_in_html(html)
    if not result:
        return False

    captcha_type, sitekey = result
    log.info("CAPTCHA detected: %s (sitekey=%s…)", captcha_type, sitekey[:12])
    token = solve_captcha_2captcha(sitekey, page.url, captcha_type)
    if not token:
        return False

    if captcha_type == "turnstile":
        await page.evaluate(f"""
            () => {{
                const el = document.querySelector('[name="cf-turnstile-response"]');
                if (el) el.value = '{token}';
                if (typeof window.turnstileCallback === 'function') window.turnstileCallback('{token}');
                const form = document.querySelector('form');
                if (form) form.submit();
            }}
        """)
    elif captcha_type == "recaptcha":
        await page.evaluate(f"""
            () => {{
                const el = document.getElementById('g-recaptcha-response');
                if (el) el.innerHTML = '{token}';
            }}
        """)
    elif captcha_type == "hcaptcha":
        await page.evaluate(f"""
            () => {{
                const el = document.querySelector('[name="h-captcha-response"]');
                if (el) el.value = '{token}';
            }}
        """)

    await asyncio.sleep(3)
    return True


# ── DOM parsing (fallback) ──────────────────────────────────────────────────

async def parse_lots_from_dom(page: Page, category: str) -> list[dict]:
    items = await page.evaluate("""
        (baseUrl) => {
            const links = document.querySelectorAll('a[href*="/l/"]');
            const seen = {};
            const results = [];
            for (const a of links) {
                let href = a.getAttribute('href') || '';
                if (!/\\/l\\/\\d/.test(href)) continue;
                if (!href.startsWith('http')) href = baseUrl + href;
                if (seen[href]) continue;
                seen[href] = true;
                let card = a;
                for (let i = 0; i < 5; i++) {
                    if (card.parentElement && card.parentElement.tagName !== 'BODY') {
                        card = card.parentElement;
                    } else break;
                }
                const text = (sel) => { const e = card.querySelector(sel); return e ? e.innerText.trim() : ''; };
                const attr = (sel, a) => { const e = card.querySelector(sel); return e ? (e.getAttribute(a)||'') : ''; };
                results.push({
                    title: text('h3') || text('h2') || text('[class*="itle"]') || a.innerText.trim().split('\\n')[0],
                    url: href,
                    price: text('[class*="rice"]') || text('[class*="bid"]') || '',
                    bids: '', closing_time: text('time') || text('[class*="time"]') || '',
                    image_url: attr('img','src') || attr('img','data-src') || ''
                });
            }
            return results;
        }
    """, BASE_URL)

    result = []
    for item in (items or []):
        item.update({"category": category, "id": "", "currency": "EUR", "seller": "", "description": ""})
        if item.get("title") or item.get("url"):
            result.append(item)
    return result


# ── Category scraper ────────────────────────────────────────────────────────

async def scrape_category(
    browser, vp: dict,
    cat_name: str, cat_path: str,
    max_pages: int = 5,
    scrape_details: bool = False,
    debug: bool = False,
) -> list[dict]:
    page = await new_page(browser, vp)
    all_items: list[dict] = []

    # ── Network interception ───────────────────────────────────────────
    captured: list[dict] = []

    async def _on_response(resp):
        url = resp.url
        if resp.status != 200:
            return
        ct = resp.headers.get("content-type", "")
        if "json" not in ct:
            return
        if any(p in url.lower() for p in API_PATTERNS):
            try:
                text = await resp.text()
                body = json.loads(text)
                captured.append({"url": url, "body": body})
            except Exception:
                pass

    page.on("response", lambda r: asyncio.ensure_future(_on_response(r)))

    for page_num in range(1, max_pages + 1):
        url = f"{BASE_URL}{cat_path}?page={page_num}"
        log.info("[%s] Page %d → %s", cat_name, page_num, url)
        captured.clear()

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = await page.goto(url, {"timeout": PAGE_LOAD_TIMEOUT, "waitUntil": "domcontentloaded"})
                status = resp.status if resp else 0
                log.info("[%s] HTTP %d", cat_name, status)
                await asyncio.sleep(5)

                solved = await handle_captcha(page)
                if solved:
                    await page.goto(url, {"timeout": PAGE_LOAD_TIMEOUT, "waitUntil": "domcontentloaded"})
                    await asyncio.sleep(5)
                break
            except Exception as exc:
                log.warning("[%s] Attempt %d/%d: %s", cat_name, attempt, MAX_RETRIES, exc)
                if attempt == MAX_RETRIES:
                    await page.close()
                    return all_items
                human_delay(3, 6)

        # Scroll
        await page.evaluate("""
            async () => {
                for (let i = 0; i < 8; i++) {
                    window.scrollBy(0, window.innerHeight * 0.8);
                    await new Promise(r => setTimeout(r, 700));
                }
                window.scrollTo(0, 0);
            }
        """)
        await asyncio.sleep(2)

        html = await page.content()

        if debug:
            save_debug_html(html, f"{cat_name}_p{page_num}")
            if captured:
                from pathlib import Path
                Path("debug").mkdir(exist_ok=True)
                for i, cap in enumerate(captured):
                    fp = Path("debug") / f"{cat_name}_p{page_num}_api{i}.json"
                    fp.write_text(json.dumps(cap, indent=2, default=str), encoding="utf-8")
                log.info("[%s] Dumped %d API responses to debug/", cat_name, len(captured))

        # ── Extraction ─────────────────────────────────────────────────
        page_items: list[dict] = []

        # Strategy 1: API
        for cap in captured:
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

        # Strategy 4: DOM
        if not page_items:
            page_items = await parse_lots_from_dom(page, cat_name)
            if page_items:
                log.info("[%s] Strategy DOM: %d lots", cat_name, len(page_items))

        if not page_items:
            title = await page.evaluate("() => document.title")
            log.warning("[%s] No lots on page %d (title: %s, HTML: %d chars)",
                        cat_name, page_num, title, len(html))
            if len(html) < 5000:
                log.warning("[%s] Likely challenge page. Use PROXY_URL + TWOCAPTCHA_API_KEY.", cat_name)
            break

        seen = {it["url"] for it in all_items if it.get("url")}
        for it in page_items:
            if it.get("url") and it["url"] not in seen:
                all_items.append(it)
                seen.add(it["url"])
            elif not it.get("url"):
                all_items.append(it)

        log.info("[%s] Page %d: +%d (total: %d)", cat_name, page_num, len(page_items), len(all_items))
        human_delay()

    await page.close()
    log.info("[%s] Done — %d lots total", cat_name, len(all_items))
    return all_items


# ── Main ────────────────────────────────────────────────────────────────────

async def main(args: argparse.Namespace):
    if args.all:
        cats = CATEGORIES
    else:
        cats = {c: CATEGORIES[c] for c in args.categories if c in CATEGORIES}
        if not cats:
            log.error("No valid categories. Available: %s", ", ".join(sorted(CATEGORIES)))
            return

    browser, vp = await create_browser(headed=args.headed)
    try:
        all_results: list[dict] = []
        for name, path in cats.items():
            items = await scrape_category(
                browser, vp, name, path,
                max_pages=args.pages, scrape_details=args.details, debug=args.debug,
            )
            all_results.extend(items)
    finally:
        await browser.close()

    if all_results:
        outfile = export_results(all_results, prefix="catawiki", fmt=args.format)
        log.info("✓ Export: %s (%d items)", outfile, len(all_results))
    else:
        log.warning("No data scraped. Tips:")
        log.warning("  1. Run with --debug --headed to inspect the page")
        log.warning("  2. Set PROXY_URL from https://2prx.com")
        log.warning("  3. Set TWOCAPTCHA_API_KEY from https://2captcha.com")


def cli():
    p = argparse.ArgumentParser(
        description="Catawiki Scraper — Puppeteer (Pyppeteer)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python catawiki_puppeteer.py --all --pages 3 --format json
  python catawiki_puppeteer.py --categories art watches --format csv
  python catawiki_puppeteer.py --categories art --debug --headed

Environment variables:
  TWOCAPTCHA_API_KEY   Your 2captcha.com API key
  PROXY_URL            Proxy from 2prx.com
  FINGERPRINT_ENABLED  Enable fingerprint randomisation (default: true)
        """,
    )
    p.add_argument("--categories", nargs="+", default=[])
    p.add_argument("--all", action="store_true")
    p.add_argument("--pages", type=int, default=3)
    p.add_argument("--format", choices=["json", "csv"], default="json")
    p.add_argument("--details", action="store_true")
    p.add_argument("--headed", action="store_true")
    p.add_argument("--debug", action="store_true", help="Dump HTML & API to debug/")
    return p.parse_args()


if __name__ == "__main__":
    asyncio.run(main(cli()))
