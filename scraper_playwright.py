#!/usr/bin/env python3
"""
Tokopedia Scraper — Playwright (Primary)
https://github.com/2scraper/tokopedia-scraper

Features:
  • Scrapes ALL Tokopedia categories (or a user-specified subset)
  • Cloudflare challenge warmup & auto-detection
  • Infinite-scroll pagination
  • Fingerprint evasion (stealth JS injection)
  • 2captcha.com integration for Turnstile / reCAPTCHA / hCaptcha
  • 2prx.com proxy support
  • Anti-detect browser endpoint support
  • JSON & CSV output
  • Debug mode with screenshots on every step

Usage:
  python scraper_playwright.py --categories electronics fashion-men --pages 3 --output json
  python scraper_playwright.py --all --output csv
  python scraper_playwright.py --categories electronics --pages 1 --debug          # saves screenshots
  python scraper_playwright.py --categories electronics --pages 1 --debug --headed  # visible browser
"""

import argparse
import asyncio
import os
import random
import re
import time
from datetime import datetime, timezone

from playwright.async_api import async_playwright, Page, BrowserContext

from config import (
    BASE_URL, CATEGORIES, PROXY_URL, ANTIDETECT_BROWSER_WS,
    FINGERPRINT_JS, CHROMIUM_ARGS, Product, CaptchaSolver,
    save_json, save_csv, random_delay, random_ua, log,
)

DEBUG = False  # set via --debug flag


# ── Cloudflare challenge detection ───────────────────────────────────────────

async def is_cloudflare_challenge(page: Page) -> bool:
    """Detect if the current page is a Cloudflare challenge/block."""
    try:
        indicators = await page.evaluate("""() => {
            const html = document.documentElement.innerHTML || '';
            const title = document.title || '';
            return {
                hasChallenge: html.includes('challenge-platform')
                           || html.includes('cf-browser-verification')
                           || html.includes('cf-challenge-running')
                           || html.includes('Checking if the site connection is secure')
                           || html.includes('challenges.cloudflare.com'),
                hasBlock: title.includes('Just a moment')
                       || title.includes('Attention Required')
                       || title.includes('Access denied'),
                hasTurnstile: !!document.querySelector('iframe[src*="challenges.cloudflare.com"]')
                           || !!document.querySelector('#cf-turnstile-container')
                           || !!document.querySelector('[data-sitekey]'),
                title: title,
            };
        }""")
    except Exception:
        return False

    is_cf = indicators.get("hasChallenge") or indicators.get("hasBlock") or indicators.get("hasTurnstile")
    if is_cf:
        log.info("Cloudflare detected (title: '%s', turnstile: %s)", indicators["title"], indicators["hasTurnstile"])
    return is_cf


async def wait_for_cloudflare(page: Page, solver: CaptchaSolver, timeout: int = 30) -> bool:
    """Wait for Cloudflare challenge to auto-resolve, or solve with 2captcha.
    Returns True if page is now clear."""

    captcha_attempted = False
    for elapsed in range(timeout):
        if not await is_cloudflare_challenge(page):
            log.info("Cloudflare cleared ✓")
            return True

        # Try solving Turnstile with 2captcha after 5s of waiting
        if not captcha_attempted and elapsed >= 5:
            captcha_attempted = True
            solved = await detect_and_solve_captcha(page, solver)
            if solved:
                await page.wait_for_timeout(3000)
                continue

        await page.wait_for_timeout(1000)

    log.warning("Cloudflare did NOT clear after %ds", timeout)
    return False


# ── CAPTCHA detection & solving ──────────────────────────────────────────────

async def detect_and_solve_captcha(page: Page, solver: CaptchaSolver) -> bool:
    """Return True if a CAPTCHA was detected AND solved."""

    # Cloudflare Turnstile
    turnstile = await page.query_selector(
        "iframe[src*='challenges.cloudflare.com'], #cf-turnstile-container, [data-sitekey]"
    )
    if turnstile:
        log.info("Attempting Turnstile solve via 2captcha …")
        sitekey = await _extract_sitekey(page)
        if sitekey:
            token = solver.solve_turnstile(sitekey, page.url)
            if token:
                await page.evaluate(
                    """(token) => {
                        const cb = window.turnstileCallback || window.__turnstileCallback;
                        if (cb) { cb(token); return; }
                        const inp = document.querySelector('[name="cf-turnstile-response"]');
                        if (inp) { inp.value = token; }
                        const form = document.querySelector('#challenge-form, form[action*="challenge"]');
                        if (form) form.submit();
                    }""",
                    token,
                )
                await page.wait_for_timeout(3000)
                return True

    # reCAPTCHA v2
    recaptcha = await page.query_selector("iframe[src*='google.com/recaptcha']")
    if recaptcha:
        log.info("reCAPTCHA v2 detected")
        sitekey = await page.evaluate(
            "() => { const el = document.querySelector('.g-recaptcha'); return el ? el.getAttribute('data-sitekey') : ''; }"
        )
        if sitekey:
            token = solver.solve_recaptcha_v2(sitekey, page.url)
            if token:
                await page.evaluate(
                    f'document.getElementById("g-recaptcha-response").innerHTML="{token}";'
                )
                await page.wait_for_timeout(2000)
                return True

    # hCaptcha
    hcaptcha = await page.query_selector("iframe[src*='hcaptcha.com']")
    if hcaptcha:
        log.info("hCaptcha detected")
        sitekey = await page.evaluate(
            "() => { const el = document.querySelector('[data-sitekey]'); return el ? el.getAttribute('data-sitekey') : ''; }"
        )
        if sitekey:
            token = solver.solve_hcaptcha(sitekey, page.url)
            if token:
                await page.evaluate(
                    f'document.querySelector("[name=h-captcha-response]").value = "{token}";'
                )
                await page.wait_for_timeout(2000)
                return True

    return False


async def _extract_sitekey(page: Page) -> str:
    return await page.evaluate("""() => {
        const el = document.querySelector('[data-sitekey]');
        if (el) return el.getAttribute('data-sitekey');
        const iframe = document.querySelector('iframe[src*="challenges.cloudflare.com"]');
        if (iframe) {
            const m = iframe.src.match(/[?&]k=([^&]+)/);
            if (m) return m[1];
        }
        const scripts = document.querySelectorAll('script');
        for (const s of scripts) {
            const m = s.textContent.match(/sitekey['":\\s]+['"]([0-9a-zA-Z_-]{30,})['"]/);
            if (m) return m[1];
        }
        return '';
    }""") or ""


# ── Debug helpers ────────────────────────────────────────────────────────────

async def debug_screenshot(page: Page, name: str) -> None:
    if not DEBUG:
        return
    os.makedirs("debug", exist_ok=True)
    ts = datetime.now().strftime("%H%M%S")
    path = f"debug/{ts}_{name}.png"
    try:
        await page.screenshot(path=path, full_page=False)
        log.info("📸 Screenshot → %s", path)
    except Exception as exc:
        log.debug("Screenshot failed: %s", exc)


async def debug_dump_html(page: Page, name: str) -> None:
    if not DEBUG:
        return
    os.makedirs("debug", exist_ok=True)
    ts = datetime.now().strftime("%H%M%S")
    path = f"debug/{ts}_{name}.html"
    try:
        html = await page.content()
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
        log.info("📄 HTML → %s (%d bytes)", path, len(html))
    except Exception as exc:
        log.debug("HTML dump failed: %s", exc)


# ── Page helpers ─────────────────────────────────────────────────────────────

async def scroll_to_bottom(page: Page, pause: float = 1.5, max_scrolls: int = 30) -> None:
    prev_height = 0
    for _ in range(max_scrolls):
        curr_height = await page.evaluate("document.body.scrollHeight")
        if curr_height == prev_height:
            break
        prev_height = curr_height
        await page.evaluate("window.scrollBy(0, window.innerHeight)")
        await page.wait_for_timeout(int(pause * 1000))


async def extract_products(page: Page, category_name: str) -> list[Product]:
    products: list[Product] = []
    now = datetime.now(timezone.utc).isoformat()

    selectors = [
        "[data-testid='lstCL2ProductList'] > div",
        ".css-bk6tzz",
        "[data-testid='divProductWrapper']",
        "[data-testid='master-product-card']",
        ".pcv3__container",
    ]

    cards = []
    for sel in selectors:
        cards = await page.query_selector_all(sel)
        if cards:
            log.debug("Matched selector: %s", sel)
            break

    log.info("Found %d product cards on page", len(cards))

    for card in cards:
        try:
            name = await _text(card, "[data-testid='linkProductName'], .css-20kt3o, .prd_link-product-name")
            price = await _text(card, "[data-testid='linkProductPrice'], .css-h66vau, .prd_link-product-price")
            original_price = await _text(card, ".css-1bkbk97, .prd_label-product-slash-price")
            discount = await _text(card, "[data-testid='linkProductDiscount'], .css-1ktbh56, .prd_label-product-discount")
            rating = await _text(card, "[data-testid='linkProductRating'], .css-t70v7i")
            sold = await _text(card, "[data-testid='linkProductSold'], .css-1agfcgp")
            shop = await _text(card, "[data-testid='linkProductShopName'], .css-1rn0irl")
            location = await _text(card, "[data-testid='linkProductShopLoc'], .css-1kdc32b")
            img_el = await card.query_selector("img")
            img_url = (await img_el.get_attribute("src")) if img_el else ""
            link_el = await card.query_selector("a[href*='/']")
            product_url = (await link_el.get_attribute("href")) if link_el else ""
            if product_url and not product_url.startswith("http"):
                product_url = BASE_URL + product_url

            if name:
                products.append(Product(
                    name=name, price=price, original_price=original_price,
                    discount=discount, rating=rating, sold=sold,
                    shop_name=shop, shop_location=location,
                    image_url=img_url, product_url=product_url,
                    category=category_name, timestamp=now,
                ))
        except Exception as exc:
            log.debug("Card parse error: %s", exc)

    return products


async def _text(parent, selector: str) -> str:
    el = await parent.query_selector(selector)
    return ((await el.inner_text()) if el else "").strip()


# ── Browser launch ───────────────────────────────────────────────────────────

async def create_context(pw, headed: bool = False) -> tuple:
    if ANTIDETECT_BROWSER_WS:
        log.info("Connecting to anti-detect browser at %s", ANTIDETECT_BROWSER_WS)
        browser = await pw.chromium.connect_over_cdp(ANTIDETECT_BROWSER_WS)
        ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
        return browser, ctx

    ua = random_ua()
    launch_opts: dict = {
        "headless": not headed,
        "args": CHROMIUM_ARGS + [f"--user-agent={ua}"],
    }
    ctx_opts: dict = {
        "user_agent": ua,
        "viewport": {"width": 1920, "height": 1080},
        "locale": "en-US",
        "timezone_id": "Asia/Jakarta",
        "extra_http_headers": {
            "Accept-Language": "en-US,en;q=0.9,id;q=0.8",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
        },
    }

    if PROXY_URL:
        log.info("Using proxy: %s", PROXY_URL.split("@")[-1] if "@" in PROXY_URL else PROXY_URL)
        ctx_opts["proxy"] = {"server": PROXY_URL}

    browser = await pw.chromium.launch(**launch_opts)
    ctx = await browser.new_context(**ctx_opts)
    await ctx.add_init_script(FINGERPRINT_JS)
    return browser, ctx


# ── Warmup ───────────────────────────────────────────────────────────────────

async def warmup(page: Page, solver: CaptchaSolver) -> bool:
    """Visit Tokopedia homepage to establish Cloudflare cookies/clearance."""

    log.info("🔥 Warming up — visiting homepage to pass Cloudflare …")

    try:
        await page.goto(BASE_URL, wait_until="commit", timeout=30_000)
    except Exception as exc:
        log.warning("Homepage nav error: %s", str(exc).split("\n")[0])
        await debug_screenshot(page, "warmup_nav_error")
        return False

    await debug_screenshot(page, "warmup_01_initial")

    if await is_cloudflare_challenge(page):
        log.info("Cloudflare challenge on homepage — waiting …")
        cleared = await wait_for_cloudflare(page, solver, timeout=45)
        await debug_screenshot(page, "warmup_02_after_cf")
        if not cleared:
            await debug_dump_html(page, "warmup_cf_stuck")
            return False
    else:
        log.info("No Cloudflare challenge on homepage ✓")

    await page.wait_for_timeout(3000)

    title = await page.title()
    log.info("Homepage title: '%s'", title)
    await debug_screenshot(page, "warmup_03_done")

    if "tokopedia" in title.lower() or "just a moment" not in title.lower():
        log.info("Warmup succeeded ✓")
        return True

    log.warning("Warmup may have failed — title: '%s'", title)
    return False


# ── Navigation with retry ────────────────────────────────────────────────────

async def navigate_with_retry(page: Page, solver: CaptchaSolver, url: str, retries: int = 3) -> bool:
    wait = 5.0
    for attempt in range(1, retries + 1):
        try:
            resp = await page.goto(url, wait_until="commit", timeout=30_000)
            status = resp.status if resp else 0
            log.info("Status: %d", status)
        except Exception as exc:
            log.warning("Attempt %d/%d error: %s", attempt, retries, str(exc).split("\n")[0])
            await debug_screenshot(page, f"nav_err_{attempt}")
            if attempt < retries:
                jitter = random.uniform(0, wait * 0.3)
                log.info("Retrying in %.1fs …", wait + jitter)
                await asyncio.sleep(wait + jitter)
                wait *= 2
            continue

        await debug_screenshot(page, f"nav_ok_{attempt}_s{status}")

        if await is_cloudflare_challenge(page):
            cleared = await wait_for_cloudflare(page, solver, timeout=30)
            if cleared:
                await page.wait_for_timeout(2000)
                return True
            await debug_screenshot(page, f"nav_cf_stuck_{attempt}")
            if attempt < retries:
                jitter = random.uniform(0, wait * 0.3)
                log.info("CF stuck — retrying in %.1fs …", wait + jitter)
                await asyncio.sleep(wait + jitter)
                wait *= 2
            continue

        # No CF — wait for DOM
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=15_000)
        except Exception:
            pass
        return True

    return False


# ── Main scraping loop ───────────────────────────────────────────────────────

async def scrape(
    categories: list[str],
    max_pages: int = 5,
    output_format: str = "json",
    output_dir: str = "output",
    headed: bool = False,
) -> list[Product]:
    solver = CaptchaSolver()
    all_products: list[Product] = []
    consecutive_errors = 0
    MAX_CONSECUTIVE_ERRORS = 5

    async with async_playwright() as pw:
        browser, ctx = await create_context(pw, headed=headed)
        page = await ctx.new_page()

        # ── Warmup ───────────────────────────────────────────────────────
        warmup_ok = await warmup(page, solver)
        if not warmup_ok:
            log.error(
                "❌ Warmup failed — Cloudflare is blocking this browser.\n"
                "   Recommendations:\n"
                "   1. Set PROXY_URL with residential proxy from https://2prx.com\n"
                "   2. Set TWOCAPTCHA_API_KEY to solve Turnstile via https://2captcha.com\n"
                "   3. Set ANTIDETECT_BROWSER_WS for anti-detect browser\n"
                "   4. Run with --debug --headed to see what's happening"
            )
            log.info("Attempting to continue despite failed warmup …")

        # ── Scrape ───────────────────────────────────────────────────────
        for cat_key in categories:
            cat_path = CATEGORIES.get(cat_key)
            if not cat_path:
                log.warning("Unknown category '%s' — skipping", cat_key)
                continue

            log.info("━━━ Scraping category: %s ━━━", cat_key)

            for page_num in range(1, max_pages + 1):
                url = f"{BASE_URL}{cat_path}?page={page_num}"
                log.info("Page %d → %s", page_num, url)

                ok = await navigate_with_retry(page, solver, url)
                if not ok:
                    consecutive_errors += 1
                    log.error("All retries failed for %s", url)
                    if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                        log.warning("Too many errors — recycling browser …")
                        await ctx.close()
                        await browser.close()
                        random_delay(3.0, 6.0)
                        browser, ctx = await create_context(pw, headed=headed)
                        page = await ctx.new_page()
                        await warmup(page, solver)
                        consecutive_errors = 0
                    continue

                consecutive_errors = 0

                # Wait for product grid
                try:
                    await page.wait_for_selector(
                        "[data-testid='lstCL2ProductList'], .css-bk6tzz, "
                        "[data-testid='divProductWrapper'], [data-testid='master-product-card']",
                        timeout=15_000,
                    )
                except Exception:
                    log.warning("Product grid not found")
                    await debug_screenshot(page, f"no_grid_{cat_key}_p{page_num}")
                    await debug_dump_html(page, f"no_grid_{cat_key}_p{page_num}")

                await scroll_to_bottom(page)
                products = await extract_products(page, cat_key)
                all_products.extend(products)
                log.info("Collected %d products (total: %d)", len(products), len(all_products))
                random_delay(2.0, 4.5)

        await ctx.close()
        await browser.close()

    # ── Save ─────────────────────────────────────────────────────────────
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    if "json" in output_format:
        save_json(all_products, f"{output_dir}/tokopedia_{ts}.json")
    if "csv" in output_format:
        save_csv(all_products, f"{output_dir}/tokopedia_{ts}.csv")

    return all_products


# ── CLI ──────────────────────────────────────────────────────────────────────

def cli():
    parser = argparse.ArgumentParser(description="Tokopedia Scraper — Playwright")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--categories", nargs="+", help="Category keys to scrape")
    group.add_argument("--all", action="store_true", help="Scrape ALL categories")
    parser.add_argument("--pages", type=int, default=5, help="Pages per category (default: 5)")
    parser.add_argument("--output", default="json", choices=["json", "csv", "both"], help="Output format")
    parser.add_argument("--output-dir", default="output", help="Output directory")
    parser.add_argument("--debug", action="store_true", help="Save screenshots & HTML to debug/")
    parser.add_argument("--headed", action="store_true", help="Run visible browser (not headless)")
    args = parser.parse_args()

    global DEBUG
    DEBUG = args.debug
    if DEBUG:
        log.setLevel("DEBUG")
        log.info("Debug mode ON — screenshots → debug/")

    cats = list(CATEGORIES.keys()) if args.all else args.categories
    fmt = "json,csv" if args.output == "both" else args.output

    asyncio.run(scrape(
        cats, max_pages=args.pages, output_format=fmt,
        output_dir=args.output_dir, headed=args.headed,
    ))


if __name__ == "__main__":
    cli()
