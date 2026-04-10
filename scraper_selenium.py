#!/usr/bin/env python3
"""
Tokopedia Scraper — Selenium
https://github.com/2scraper/tokopedia-scraper

Features:
  • Cloudflare challenge warmup & auto-detection
  • 2captcha.com integration for Turnstile / reCAPTCHA / hCaptcha
  • 2prx.com proxy support · Anti-detect browser support
  • Fingerprint evasion · Debug mode with screenshots

Usage:
  python scraper_selenium.py --categories electronics beauty --pages 3 --output json
  python scraper_selenium.py --all --output both
  python scraper_selenium.py --categories electronics --pages 1 --debug
  python scraper_selenium.py --categories electronics --pages 1 --debug --headed
"""

import argparse
import os
import random
import time
from datetime import datetime, timezone

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, WebDriverException

from config import (
    BASE_URL, CATEGORIES, PROXY_URL, ANTIDETECT_BROWSER_WS,
    FINGERPRINT_JS, CHROMIUM_ARGS, Product, CaptchaSolver,
    save_json, save_csv, random_delay, random_ua, log,
    MAX_RETRIES, RETRY_BACKOFF,
)

DEBUG = False


# ── Cloudflare challenge detection ───────────────────────────────────────────

def is_cloudflare_challenge(driver) -> bool:
    try:
        indicators = driver.execute_script("""
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
        """)
    except Exception:
        return False

    is_cf = indicators.get("hasChallenge") or indicators.get("hasBlock") or indicators.get("hasTurnstile")
    if is_cf:
        log.info("Cloudflare detected (title: '%s', turnstile: %s)", indicators["title"], indicators["hasTurnstile"])
    return is_cf


def wait_for_cloudflare(driver, solver: CaptchaSolver, timeout: int = 30) -> bool:
    captcha_attempted = False
    for elapsed in range(timeout):
        if not is_cloudflare_challenge(driver):
            log.info("Cloudflare cleared ✓")
            return True
        if not captcha_attempted and elapsed >= 5:
            captcha_attempted = True
            solved = detect_and_solve_captcha(driver, solver)
            if solved:
                time.sleep(3)
                continue
        time.sleep(1)
    log.warning("Cloudflare did NOT clear after %ds", timeout)
    return False


# ── CAPTCHA detection & solving ──────────────────────────────────────────────

def detect_and_solve_captcha(driver, solver: CaptchaSolver) -> bool:
    try:
        turnstile = driver.find_elements(By.CSS_SELECTOR,
            "iframe[src*='challenges.cloudflare.com'], #cf-turnstile-container, [data-sitekey]")
        if turnstile:
            log.info("Attempting Turnstile solve via 2captcha …")
            sitekey = driver.execute_script("""
                const el = document.querySelector('[data-sitekey]');
                if (el) return el.getAttribute('data-sitekey');
                const iframe = document.querySelector('iframe[src*="challenges.cloudflare.com"]');
                if (iframe) { const m = iframe.src.match(/[?&]k=([^&]+)/); if (m) return m[1]; }
                return '';
            """)
            if sitekey:
                token = solver.solve_turnstile(sitekey, driver.current_url)
                if token:
                    driver.execute_script(f"""
                        const cb = window.turnstileCallback || window.__turnstileCallback;
                        if (cb) {{ cb('{token}'); return; }}
                        const inp = document.querySelector('[name="cf-turnstile-response"]');
                        if (inp) inp.value = '{token}';
                        const form = document.querySelector('#challenge-form, form[action*="challenge"]');
                        if (form) form.submit();
                    """)
                    time.sleep(3)
                    return True

        recaptcha = driver.find_elements(By.CSS_SELECTOR, "iframe[src*='google.com/recaptcha']")
        if recaptcha:
            log.info("reCAPTCHA v2 detected")
            sitekey = driver.execute_script(
                "const el = document.querySelector('.g-recaptcha'); return el ? el.getAttribute('data-sitekey') : '';"
            )
            if sitekey:
                token = solver.solve_recaptcha_v2(sitekey, driver.current_url)
                if token:
                    driver.execute_script(f'document.getElementById("g-recaptcha-response").innerHTML="{token}";')
                    time.sleep(2)
                    return True

        hcaptcha = driver.find_elements(By.CSS_SELECTOR, "iframe[src*='hcaptcha.com']")
        if hcaptcha:
            log.info("hCaptcha detected")
            sitekey = driver.execute_script(
                "const el = document.querySelector('[data-sitekey]'); return el ? el.getAttribute('data-sitekey') : '';"
            )
            if sitekey:
                token = solver.solve_hcaptcha(sitekey, driver.current_url)
                if token:
                    driver.execute_script(f'document.querySelector("[name=h-captcha-response]").value = "{token}";')
                    time.sleep(2)
                    return True
    except Exception as exc:
        log.debug("CAPTCHA check error: %s", exc)
    return False


# ── Debug helpers ────────────────────────────────────────────────────────────

def debug_screenshot(driver, name: str) -> None:
    if not DEBUG:
        return
    os.makedirs("debug", exist_ok=True)
    ts = datetime.now().strftime("%H%M%S")
    path = f"debug/{ts}_{name}.png"
    try:
        driver.save_screenshot(path)
        log.info("📸 Screenshot → %s", path)
    except Exception as exc:
        log.debug("Screenshot failed: %s", exc)


def debug_dump_html(driver, name: str) -> None:
    if not DEBUG:
        return
    os.makedirs("debug", exist_ok=True)
    ts = datetime.now().strftime("%H%M%S")
    path = f"debug/{ts}_{name}.html"
    try:
        html = driver.page_source
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
        log.info("📄 HTML → %s (%d bytes)", path, len(html))
    except Exception as exc:
        log.debug("HTML dump failed: %s", exc)


# ── Page helpers ─────────────────────────────────────────────────────────────

def scroll_to_bottom(driver, pause: float = 1.5, max_scrolls: int = 30) -> None:
    prev_height = 0
    for _ in range(max_scrolls):
        curr_height = driver.execute_script("return document.body.scrollHeight")
        if curr_height == prev_height:
            break
        prev_height = curr_height
        driver.execute_script("window.scrollBy(0, window.innerHeight)")
        time.sleep(pause)


def extract_products(driver, category_name: str) -> list[Product]:
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
        cards = driver.find_elements(By.CSS_SELECTOR, sel)
        if cards:
            break

    log.info("Found %d product cards on page", len(cards))

    for card in cards:
        try:
            name = _text(card, "[data-testid='linkProductName'], .css-20kt3o, .prd_link-product-name")
            price = _text(card, "[data-testid='linkProductPrice'], .css-h66vau, .prd_link-product-price")
            original_price = _text(card, ".css-1bkbk97, .prd_label-product-slash-price")
            discount = _text(card, "[data-testid='linkProductDiscount'], .css-1ktbh56, .prd_label-product-discount")
            rating = _text(card, "[data-testid='linkProductRating'], .css-t70v7i")
            sold = _text(card, "[data-testid='linkProductSold'], .css-1agfcgp")
            shop = _text(card, "[data-testid='linkProductShopName'], .css-1rn0irl")
            location = _text(card, "[data-testid='linkProductShopLoc'], .css-1kdc32b")

            try:
                img_el = card.find_element(By.CSS_SELECTOR, "img")
                img_url = img_el.get_attribute("src") or ""
            except Exception:
                img_url = ""
            try:
                link_el = card.find_element(By.CSS_SELECTOR, "a[href*='/']")
                product_url = link_el.get_attribute("href") or ""
            except Exception:
                product_url = ""
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


def _text(parent, selector: str) -> str:
    try:
        el = parent.find_element(By.CSS_SELECTOR, selector)
        return el.text.strip()
    except Exception:
        return ""


# ── Browser launch ───────────────────────────────────────────────────────────

def create_driver(headed: bool = False) -> webdriver.Chrome:
    options = Options()
    for arg in CHROMIUM_ARGS:
        options.add_argument(arg)
    options.add_argument(f"--user-agent={random_ua()}")
    if not headed:
        options.add_argument("--headless=new")

    if PROXY_URL:
        log.info("Using proxy: %s", PROXY_URL.split("@")[-1] if "@" in PROXY_URL else PROXY_URL)
        options.add_argument(f"--proxy-server={PROXY_URL}")

    if ANTIDETECT_BROWSER_WS:
        log.info("Connecting to anti-detect browser at %s", ANTIDETECT_BROWSER_WS)
        options.debugger_address = ANTIDETECT_BROWSER_WS.replace("ws://", "").replace("wss://", "")

    driver = webdriver.Chrome(options=options)
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": FINGERPRINT_JS})
    driver.set_page_load_timeout(30)
    return driver


# ── Warmup ───────────────────────────────────────────────────────────────────

def warmup(driver, solver: CaptchaSolver) -> bool:
    log.info("🔥 Warming up — visiting homepage to pass Cloudflare …")
    try:
        driver.get(BASE_URL)
    except WebDriverException as exc:
        log.warning("Homepage nav error: %s", str(exc).split("\n")[0])
        debug_screenshot(driver, "warmup_nav_error")
        # Even on timeout, Selenium may have loaded the challenge page
        pass

    debug_screenshot(driver, "warmup_01_initial")

    if is_cloudflare_challenge(driver):
        log.info("Cloudflare challenge on homepage — waiting …")
        cleared = wait_for_cloudflare(driver, solver, timeout=45)
        debug_screenshot(driver, "warmup_02_after_cf")
        if not cleared:
            debug_dump_html(driver, "warmup_cf_stuck")
            return False
    else:
        log.info("No Cloudflare challenge on homepage ✓")

    time.sleep(3)
    title = driver.title
    log.info("Homepage title: '%s'", title)
    debug_screenshot(driver, "warmup_03_done")

    if "tokopedia" in title.lower() or "just a moment" not in title.lower():
        log.info("Warmup succeeded ✓")
        return True
    log.warning("Warmup may have failed — title: '%s'", title)
    return False


# ── Navigation with retry ────────────────────────────────────────────────────

def navigate_with_retry(driver, solver: CaptchaSolver, url: str, retries: int = 3) -> bool:
    wait = RETRY_BACKOFF
    for attempt in range(1, retries + 1):
        try:
            driver.get(url)
        except WebDriverException as exc:
            log.warning("Attempt %d/%d error: %s", attempt, retries, str(exc).split("\n")[0])
            debug_screenshot(driver, f"nav_err_{attempt}")
            # Even on timeout, page may be partially loaded — check CF
            pass

        debug_screenshot(driver, f"nav_ok_{attempt}")

        if is_cloudflare_challenge(driver):
            cleared = wait_for_cloudflare(driver, solver, timeout=30)
            if cleared:
                time.sleep(2)
                return True
            debug_screenshot(driver, f"nav_cf_stuck_{attempt}")
            if attempt < retries:
                jitter = random.uniform(0, wait * 0.3)
                log.info("CF stuck — retrying in %.1fs …", wait + jitter)
                time.sleep(wait + jitter)
                wait *= 2
            continue

        # No CF — page loaded
        return True

    return False


# ── Main scraping loop ───────────────────────────────────────────────────────

def scrape(
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
    driver = create_driver(headed=headed)

    try:
        # ── Warmup ───────────────────────────────────────────────────────
        warmup_ok = warmup(driver, solver)
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

                ok = navigate_with_retry(driver, solver, url)
                if not ok:
                    consecutive_errors += 1
                    log.error("All retries failed for %s", url)
                    if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                        log.warning("Too many errors — recycling browser …")
                        driver.quit()
                        random_delay(3.0, 6.0)
                        driver = create_driver(headed=headed)
                        warmup(driver, solver)
                        consecutive_errors = 0
                    continue

                consecutive_errors = 0

                try:
                    WebDriverWait(driver, 15).until(
                        EC.presence_of_element_located((By.CSS_SELECTOR,
                            "[data-testid='lstCL2ProductList'], .css-bk6tzz, "
                            "[data-testid='divProductWrapper'], [data-testid='master-product-card']"
                        ))
                    )
                except TimeoutException:
                    log.warning("Product grid not found")
                    debug_screenshot(driver, f"no_grid_{cat_key}_p{page_num}")
                    debug_dump_html(driver, f"no_grid_{cat_key}_p{page_num}")

                scroll_to_bottom(driver)
                products = extract_products(driver, cat_key)
                all_products.extend(products)
                log.info("Collected %d products (total: %d)", len(products), len(all_products))
                random_delay(2.0, 4.5)
    finally:
        driver.quit()

    # ── Save ─────────────────────────────────────────────────────────────
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    if "json" in output_format:
        save_json(all_products, f"{output_dir}/tokopedia_{ts}.json")
    if "csv" in output_format:
        save_csv(all_products, f"{output_dir}/tokopedia_{ts}.csv")

    return all_products


# ── CLI ──────────────────────────────────────────────────────────────────────

def cli():
    parser = argparse.ArgumentParser(description="Tokopedia Scraper — Selenium")
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

    scrape(cats, max_pages=args.pages, output_format=fmt,
           output_dir=args.output_dir, headed=args.headed)


if __name__ == "__main__":
    cli()
