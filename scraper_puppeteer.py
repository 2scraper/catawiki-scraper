#!/usr/bin/env python3
"""
Tokopedia Scraper — Pyppeteer (Puppeteer for Python)
https://github.com/2scraper/tokopedia-scraper

Features:
  • Cloudflare challenge warmup & auto-detection
  • 2captcha.com integration for Turnstile / reCAPTCHA / hCaptcha
  • 2prx.com proxy support · Anti-detect browser support
  • Fingerprint evasion · Debug mode with screenshots

Usage:
  python scraper_puppeteer.py --categories electronics gaming --pages 3 --output json
  python scraper_puppeteer.py --all --output both
  python scraper_puppeteer.py --categories electronics --pages 1 --debug
  python scraper_puppeteer.py --categories electronics --pages 1 --debug --headed
"""

import argparse
import asyncio
import os
import random
from datetime import datetime, timezone

from pyppeteer import launch, connect

from config import (
    BASE_URL, CATEGORIES, PROXY_URL, ANTIDETECT_BROWSER_WS,
    FINGERPRINT_JS, CHROMIUM_ARGS, Product, CaptchaSolver,
    save_json, save_csv, random_delay, random_ua, log,
)

DEBUG = False


# ── Cloudflare challenge detection ───────────────────────────────────────────

async def is_cloudflare_challenge(page) -> bool:
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


async def wait_for_cloudflare(page, solver: CaptchaSolver, timeout: int = 30) -> bool:
    captcha_attempted = False
    for elapsed in range(timeout):
        if not await is_cloudflare_challenge(page):
            log.info("Cloudflare cleared ✓")
            return True
        if not captcha_attempted and elapsed >= 5:
            captcha_attempted = True
            solved = await detect_and_solve_captcha(page, solver)
            if solved:
                await asyncio.sleep(3)
                continue
        await asyncio.sleep(1)
    log.warning("Cloudflare did NOT clear after %ds", timeout)
    return False


# ── CAPTCHA detection & solving ──────────────────────────────────────────────

async def detect_and_solve_captcha(page, solver: CaptchaSolver) -> bool:
    turnstile = await page.querySelector(
        "iframe[src*='challenges.cloudflare.com'], #cf-turnstile-container, [data-sitekey]"
    )
    if turnstile:
        log.info("Attempting Turnstile solve via 2captcha …")
        sitekey = await page.evaluate("""() => {
            const el = document.querySelector('[data-sitekey]');
            if (el) return el.getAttribute('data-sitekey');
            const iframe = document.querySelector('iframe[src*="challenges.cloudflare.com"]');
            if (iframe) { const m = iframe.src.match(/[?&]k=([^&]+)/); if (m) return m[1]; }
            return '';
        }""")
        if sitekey:
            token = solver.solve_turnstile(sitekey, page.url)
            if token:
                await page.evaluate(f"""() => {{
                    const cb = window.turnstileCallback || window.__turnstileCallback;
                    if (cb) {{ cb('{token}'); return; }}
                    const inp = document.querySelector('[name="cf-turnstile-response"]');
                    if (inp) inp.value = '{token}';
                    const form = document.querySelector('#challenge-form, form[action*="challenge"]');
                    if (form) form.submit();
                }}""")
                await asyncio.sleep(3)
                return True

    recaptcha = await page.querySelector("iframe[src*='google.com/recaptcha']")
    if recaptcha:
        log.info("reCAPTCHA v2 detected")
        sitekey = await page.evaluate(
            "() => { const el = document.querySelector('.g-recaptcha'); return el ? el.getAttribute('data-sitekey') : ''; }"
        )
        if sitekey:
            token = solver.solve_recaptcha_v2(sitekey, page.url)
            if token:
                await page.evaluate(f'document.getElementById("g-recaptcha-response").innerHTML="{token}";')
                await asyncio.sleep(2)
                return True

    hcaptcha = await page.querySelector("iframe[src*='hcaptcha.com']")
    if hcaptcha:
        log.info("hCaptcha detected")
        sitekey = await page.evaluate(
            "() => { const el = document.querySelector('[data-sitekey]'); return el ? el.getAttribute('data-sitekey') : ''; }"
        )
        if sitekey:
            token = solver.solve_hcaptcha(sitekey, page.url)
            if token:
                await page.evaluate(f'document.querySelector("[name=h-captcha-response]").value = "{token}";')
                await asyncio.sleep(2)
                return True

    return False


# ── Debug helpers ────────────────────────────────────────────────────────────

async def debug_screenshot(page, name: str) -> None:
    if not DEBUG:
        return
    os.makedirs("debug", exist_ok=True)
    ts = datetime.now().strftime("%H%M%S")
    path = f"debug/{ts}_{name}.png"
    try:
        await page.screenshot({"path": path})
        log.info("📸 Screenshot → %s", path)
    except Exception as exc:
        log.debug("Screenshot failed: %s", exc)


async def debug_dump_html(page, name: str) -> None:
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

async def scroll_to_bottom(page, pause: float = 1.5, max_scrolls: int = 30) -> None:
    prev_height = 0
    for _ in range(max_scrolls):
        curr_height = await page.evaluate("document.body.scrollHeight")
        if curr_height == prev_height:
            break
        prev_height = curr_height
        await page.evaluate("window.scrollBy(0, window.innerHeight)")
        await asyncio.sleep(pause)


async def extract_products(page, category_name: str) -> list[Product]:
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
        cards = await page.querySelectorAll(sel)
        if cards:
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

            img_el = await card.querySelector("img")
            img_url = (await (await img_el.getProperty("src")).jsonValue()) if img_el else ""
            link_el = await card.querySelector("a[href*='/']")
            product_url = (await (await link_el.getProperty("href")).jsonValue()) if link_el else ""
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
    el = await parent.querySelector(selector)
    if not el:
        return ""
    return (await (await el.getProperty("innerText")).jsonValue()).strip()


# ── Browser launch ───────────────────────────────────────────────────────────

async def create_browser_and_page(headed: bool = False):
    if ANTIDETECT_BROWSER_WS:
        log.info("Connecting to anti-detect browser at %s", ANTIDETECT_BROWSER_WS)
        browser = await connect(browserWSEndpoint=ANTIDETECT_BROWSER_WS)
        pages = await browser.pages()
        page = pages[0] if pages else await browser.newPage()
        return browser, page

    ua = random_ua()
    launch_opts = {
        "headless": not headed,
        "args": CHROMIUM_ARGS + [f"--user-agent={ua}"],
    }

    if PROXY_URL:
        log.info("Using proxy: %s", PROXY_URL.split("@")[-1] if "@" in PROXY_URL else PROXY_URL)
        launch_opts["args"].append(f"--proxy-server={PROXY_URL}")

    browser = await launch(**launch_opts)
    page = await browser.newPage()
    await page.setViewport({"width": 1920, "height": 1080})
    await page.setExtraHTTPHeaders({
        "Accept-Language": "en-US,en;q=0.9,id;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    })
    await page.evaluateOnNewDocument(FINGERPRINT_JS)

    return browser, page


# ── Warmup ───────────────────────────────────────────────────────────────────

async def warmup(page, solver: CaptchaSolver) -> bool:
    log.info("🔥 Warming up — visiting homepage to pass Cloudflare …")
    try:
        await page.goto(BASE_URL, {"waitUntil": "domcontentloaded", "timeout": 30_000})
    except Exception as exc:
        log.warning("Homepage nav error: %s", str(exc).split("\n")[0])
        await debug_screenshot(page, "warmup_nav_error")
        # Pyppeteer may still have loaded the challenge page

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

    await asyncio.sleep(3)
    title = await page.evaluate("document.title")
    log.info("Homepage title: '%s'", title)
    await debug_screenshot(page, "warmup_03_done")

    if "tokopedia" in title.lower() or "just a moment" not in title.lower():
        log.info("Warmup succeeded ✓")
        return True
    log.warning("Warmup may have failed — title: '%s'", title)
    return False


# ── Navigation with retry ────────────────────────────────────────────────────

async def navigate_with_retry(page, solver: CaptchaSolver, url: str, retries: int = 3) -> bool:
    wait = 5.0
    for attempt in range(1, retries + 1):
        try:
            resp = await page.goto(url, {"waitUntil": "domcontentloaded", "timeout": 30_000})
            status = resp.status if resp else 0
            log.info("Status: %d", status)
        except Exception as exc:
            log.warning("Attempt %d/%d error: %s", attempt, retries, str(exc).split("\n")[0])
            await debug_screenshot(page, f"nav_err_{attempt}")
            # Pyppeteer may have partial page — check CF below

        await debug_screenshot(page, f"nav_ok_{attempt}")

        if await is_cloudflare_challenge(page):
            cleared = await wait_for_cloudflare(page, solver, timeout=30)
            if cleared:
                await asyncio.sleep(2)
                return True
            await debug_screenshot(page, f"nav_cf_stuck_{attempt}")
            if attempt < retries:
                jitter = random.uniform(0, wait * 0.3)
                log.info("CF stuck — retrying in %.1fs …", wait + jitter)
                await asyncio.sleep(wait + jitter)
                wait *= 2
            continue

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
    browser, page = await create_browser_and_page(headed=headed)

    try:
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
                        await browser.close()
                        random_delay(3.0, 6.0)
                        browser, page = await create_browser_and_page(headed=headed)
                        await warmup(page, solver)
                        consecutive_errors = 0
                    continue

                consecutive_errors = 0

                try:
                    await page.waitForSelector(
                        "[data-testid='lstCL2ProductList'], .css-bk6tzz, "
                        "[data-testid='divProductWrapper'], [data-testid='master-product-card']",
                        {"timeout": 15_000},
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
    finally:
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
    parser = argparse.ArgumentParser(description="Tokopedia Scraper — Pyppeteer (Puppeteer)")
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
