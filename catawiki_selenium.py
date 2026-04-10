#!/usr/bin/env python3
"""
Catawiki Scraper — Selenium
============================
Synchronous scraper for catawiki.com using Selenium WebDriver.

Data extraction strategies (tried in order):
  1. __NEXT_DATA__ — parse embedded SSR JSON from the HTML
  2. JSON-LD — structured data from <script type="application/ld+json">
  3. Performance log API interception (Chrome DevTools Protocol)
  4. DOM parsing — broad CSS selectors as last resort

Usage:
    python catawiki_selenium.py --categories art watches --pages 3 --format json
    python catawiki_selenium.py --all --format csv
    python catawiki_selenium.py --categories art --debug --headed

Requirements:
    pip install selenium undetected-chromedriver 2captcha-python

Repository : https://github.com/2scraper/catawiki-scraper
CAPTCHA    : https://2captcha.com
Proxy      : https://2prx.com
"""

from __future__ import annotations

import argparse
import json
import re
import time
from typing import Any
from urllib.parse import urlparse

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By

try:
    import undetected_chromedriver as uc
    HAS_UC = True
except ImportError:
    HAS_UC = False

from config import (
    BASE_URL,
    CATEGORIES,
    FINGERPRINT_ENABLED,
    MAX_RETRIES,
    PROXY_URL,
    SELENIUM_TIMEOUT,
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


# ── Driver factory ──────────────────────────────────────────────────────────

def create_driver(headed: bool = False) -> webdriver.Chrome:
    ua = random_user_agent() if FINGERPRINT_ENABLED else None
    vp = random_viewport() if FINGERPRINT_ENABLED else {"width": 1920, "height": 1080}

    if HAS_UC:
        options = uc.ChromeOptions()
    else:
        options = Options()

    if not headed:
        options.add_argument("--headless=new")

    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--no-sandbox")
    options.add_argument(f"--window-size={vp['width']},{vp['height']}")

    if ua:
        options.add_argument(f"--user-agent={ua}")

    if PROXY_URL:
        parsed = urlparse(PROXY_URL)
        proxy_hp = f"{parsed.hostname}:{parsed.port}"
        options.add_argument(f"--proxy-server={parsed.scheme}://{proxy_hp}")
        log.info("Proxy: %s", proxy_hp)

    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

    # Enable performance logging for API interception
    options.set_capability("goog:loggingPrefs", {"performance": "ALL"})

    prefs = {"credentials_enable_service": False, "profile.password_manager_enabled": False}
    options.add_experimental_option("prefs", prefs)

    if HAS_UC:
        driver = uc.Chrome(options=options)
        log.info("Driver: undetected-chromedriver")
    else:
        driver = webdriver.Chrome(options=options)
        log.info("Driver: Selenium ChromeDriver")

    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": """
        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        Object.defineProperty(navigator, 'plugins',   {get: () => [1,2,3,4,5]});
        Object.defineProperty(navigator, 'languages',  {get: () => ['en-US','en','nl']});
        window.chrome = { runtime: {} };
    """})

    driver.set_page_load_timeout(SELENIUM_TIMEOUT)
    driver.implicitly_wait(3)

    if FINGERPRINT_ENABLED:
        log.info("Fingerprint → UA=%s… viewport=%dx%d", (ua or "")[:40], vp["width"], vp["height"])
    return driver


# ── CAPTCHA handling ────────────────────────────────────────────────────────

def handle_captcha(driver: webdriver.Chrome) -> bool:
    html = driver.page_source
    result = detect_captcha_in_html(html)
    if not result:
        return False

    captcha_type, sitekey = result
    log.info("CAPTCHA detected: %s (sitekey=%s…)", captcha_type, sitekey[:12])
    token = solve_captcha_2captcha(sitekey, driver.current_url, captcha_type)
    if not token:
        return False

    if captcha_type == "turnstile":
        driver.execute_script(f"""
            var el = document.querySelector('[name="cf-turnstile-response"]')
                  || document.querySelector('input[name*="turnstile"]');
            if (el) el.value = '{token}';
            if (typeof window.turnstileCallback === 'function') window.turnstileCallback('{token}');
            var form = document.querySelector('form');
            if (form) form.submit();
        """)
    elif captcha_type == "recaptcha":
        driver.execute_script(f"""
            var el = document.getElementById('g-recaptcha-response');
            if (el) el.innerHTML = '{token}';
        """)
    elif captcha_type == "hcaptcha":
        driver.execute_script(f"""
            var el = document.querySelector('[name="h-captcha-response"]');
            if (el) el.value = '{token}';
        """)

    time.sleep(3)
    return True


# ── API interception via performance log ────────────────────────────────────

def extract_api_responses(driver: webdriver.Chrome, category: str) -> list[dict]:
    """Parse Chrome performance logs to find API JSON responses."""
    items = []
    try:
        logs = driver.get_log("performance")
    except Exception:
        return items

    for entry in logs:
        try:
            msg = json.loads(entry["message"])["message"]
            if msg.get("method") != "Network.responseReceived":
                continue
            resp = msg.get("params", {}).get("response", {})
            url = resp.get("url", "")
            ct = resp.get("mimeType", "")
            if "json" not in ct:
                continue
            if not any(p in url.lower() for p in ("/api/", "/graphql", "/buyer/", "/_next/data/", "/lots", "/search")):
                continue

            request_id = msg["params"].get("requestId")
            if request_id:
                body_resp = driver.execute_cdp_cmd("Network.getResponseBody", {"requestId": request_id})
                body_str = body_resp.get("body", "")
                if body_str:
                    data = json.loads(body_str)
                    lots = parse_lots_from_api_json(data, category)
                    if lots:
                        log.info("Strategy PerfLog: %d lots from %s", len(lots), url[:80])
                        items.extend(lots)
        except Exception:
            continue

    return items


# ── DOM parsing (fallback) ──────────────────────────────────────────────────

def parse_lots_from_dom(driver: webdriver.Chrome, category: str) -> list[dict]:
    items = driver.execute_script("""
        var baseUrl = arguments[0];
        var links = document.querySelectorAll('a[href*="/l/"]');
        var seen = {};
        var results = [];
        for (var a of links) {
            var href = a.getAttribute('href') || '';
            if (!/\\/l\\/\\d/.test(href)) continue;
            if (!href.startsWith('http')) href = baseUrl + href;
            if (seen[href]) continue;
            seen[href] = true;

            var card = a;
            for (var i = 0; i < 5; i++) {
                if (card.parentElement && card.parentElement.tagName !== 'BODY') {
                    card = card.parentElement;
                } else break;
            }

            var text = function(sel) {
                var el = card.querySelector(sel);
                return el ? el.innerText.trim() : '';
            };
            var attr = function(sel, a) {
                var el = card.querySelector(sel);
                return el ? (el.getAttribute(a) || '') : '';
            };

            results.push({
                title: text('h3') || text('h2') || text('[class*="itle"]')
                       || a.innerText.trim().split('\\n')[0] || '',
                url: href,
                price: text('[class*="rice"]') || text('[class*="bid"]') || '',
                bids: '',
                closing_time: text('time') || text('[class*="time"]') || '',
                image_url: attr('img', 'src') || attr('img', 'data-src') || ''
            });
        }
        return results;
    """, BASE_URL)

    result = []
    for item in (items or []):
        item.update({"category": category, "id": "", "currency": "EUR", "seller": "", "description": ""})
        if item.get("title") or item.get("url"):
            result.append(item)
    return result


# ── Category scraper ────────────────────────────────────────────────────────

def scrape_category(
    driver: webdriver.Chrome,
    cat_name: str,
    cat_path: str,
    max_pages: int = 5,
    scrape_details: bool = False,
    debug: bool = False,
) -> list[dict]:
    all_items: list[dict] = []

    for page_num in range(1, max_pages + 1):
        url = f"{BASE_URL}{cat_path}?page={page_num}"
        log.info("[%s] Page %d → %s", cat_name, page_num, url)

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                driver.get(url)
                time.sleep(5)

                solved = handle_captcha(driver)
                if solved:
                    driver.get(url)
                    time.sleep(5)
                break
            except Exception as exc:
                log.warning("[%s] Attempt %d/%d: %s", cat_name, attempt, MAX_RETRIES, exc)
                if attempt == MAX_RETRIES:
                    return all_items
                human_delay(3, 6)

        # Scroll
        for _ in range(8):
            driver.execute_script("window.scrollBy(0, window.innerHeight * 0.8);")
            time.sleep(0.7)
        driver.execute_script("window.scrollTo(0, 0);")
        time.sleep(2)

        html = driver.page_source

        if debug:
            save_debug_html(html, f"{cat_name}_p{page_num}")

        # ── Extraction strategies ──────────────────────────────────────
        page_items: list[dict] = []

        # Strategy 1: __NEXT_DATA__
        page_items = parse_next_data(html, cat_name)
        if page_items:
            log.info("[%s] Strategy __NEXT_DATA__: %d lots", cat_name, len(page_items))

        # Strategy 2: JSON-LD
        if not page_items:
            page_items = parse_jsonld(html, cat_name)
            if page_items:
                log.info("[%s] Strategy JSON-LD: %d lots", cat_name, len(page_items))

        # Strategy 3: Performance log API
        if not page_items:
            page_items = extract_api_responses(driver, cat_name)
            if page_items:
                log.info("[%s] Strategy PerfLog: %d lots", cat_name, len(page_items))

        # Strategy 4: DOM
        if not page_items:
            page_items = parse_lots_from_dom(driver, cat_name)
            if page_items:
                log.info("[%s] Strategy DOM: %d lots", cat_name, len(page_items))

        if not page_items:
            title = driver.title
            log.warning("[%s] No lots on page %d (title: %s, HTML: %d chars)",
                        cat_name, page_num, title, len(html))
            if len(html) < 5000:
                log.warning("[%s] Likely a challenge page. Use PROXY_URL + TWOCAPTCHA_API_KEY.", cat_name)
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

    log.info("[%s] Done — %d lots total", cat_name, len(all_items))
    return all_items


# ── Main ────────────────────────────────────────────────────────────────────

def main(args: argparse.Namespace):
    if args.all:
        cats = CATEGORIES
    else:
        cats = {c: CATEGORIES[c] for c in args.categories if c in CATEGORIES}
        if not cats:
            log.error("No valid categories. Available: %s", ", ".join(sorted(CATEGORIES)))
            return

    driver = create_driver(headed=args.headed)
    try:
        all_results: list[dict] = []
        for name, path in cats.items():
            items = scrape_category(driver, name, path,
                                    max_pages=args.pages, scrape_details=args.details,
                                    debug=args.debug)
            all_results.extend(items)
    finally:
        driver.quit()

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
        description="Catawiki Scraper — Selenium",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python catawiki_selenium.py --all --pages 3 --format json
  python catawiki_selenium.py --categories art watches --format csv
  python catawiki_selenium.py --categories art --debug --headed

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
    p.add_argument("--debug", action="store_true", help="Dump HTML to debug/ folder")
    return p.parse_args()


if __name__ == "__main__":
    main(cli())
