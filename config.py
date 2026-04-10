"""
Tokopedia Scraper — Shared Configuration & Utilities
https://github.com/2scraper/tokopedia-scraper

Integrations:
  • CAPTCHA solving  → 2captcha.com
  • Proxy rotation   → 2prx.com
"""

import os
import json
import csv
import time
import random
import logging
from dataclasses import dataclass, field, asdict
from typing import Optional

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("tokopedia-scraper")

# ---------------------------------------------------------------------------
# Environment-based settings
# ---------------------------------------------------------------------------
TWOCAPTCHA_API_KEY: str = os.getenv("TWOCAPTCHA_API_KEY", "")
PROXY_URL: str = os.getenv("PROXY_URL", "")  # e.g. http://user:pass@gate.2prx.com:9999
ANTIDETECT_BROWSER_WS: str = os.getenv("ANTIDETECT_BROWSER_WS", "")  # ws://...

BASE_URL = "https://www.tokopedia.com"
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.7; rv:133.0) Gecko/20100101 Firefox/133.0",
]

# ---------------------------------------------------------------------------
# Shared Chromium launch arguments
# ---------------------------------------------------------------------------
CHROMIUM_ARGS: list[str] = [
    "--disable-http2",                         # ← fixes ERR_HTTP2_PROTOCOL_ERROR
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-blink-features=AutomationControlled",
    "--disable-features=IsolateOrigins,site-per-process",
    "--disable-infobars",
    "--disable-background-networking",
    "--disable-breakpad",
    "--disable-component-update",
    "--disable-default-apps",
    "--disable-extensions",
    "--metrics-recording-only",
    "--no-first-run",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-ipc-flooding-protection",
    "--password-store=basic",
    "--use-mock-keychain",
    "--window-size=1920,1080",
]

MAX_RETRIES: int = 3
RETRY_BACKOFF: float = 5.0  # seconds, doubled on each retry

# ---------------------------------------------------------------------------
# Tokopedia category map
# ---------------------------------------------------------------------------
CATEGORIES: dict[str, str] = {
    "electronics":          "/p/elektronik",
    "mobile-tablets":       "/p/handphone-tablet",
    "laptops-computers":    "/p/laptop-aksesoris",
    "fashion-men":          "/p/fashion-pria",
    "fashion-women":        "/p/fashion-wanita",
    "beauty":               "/p/kecantikan",
    "health":               "/p/kesehatan",
    "home-living":          "/p/rumah-tangga",
    "baby-kids":            "/p/ibu-bayi",
    "food-drinks":          "/p/makanan-minuman",
    "sports":               "/p/olahraga",
    "automotive":           "/p/otomotif",
    "books-stationery":     "/p/buku",
    "toys-hobbies":         "/p/mainan-hobi",
    "office-industrial":    "/p/office-industrial",
    "cameras":              "/p/kamera",
    "gaming":               "/p/gaming",
    "pets":                 "/p/perawatan-hewan",
    "travel":               "/p/travel-aktivitas",
    "tickets-vouchers":     "/p/tiket-voucher",
}

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
@dataclass
class Product:
    name: str = ""
    price: str = ""
    original_price: str = ""
    discount: str = ""
    rating: str = ""
    sold: str = ""
    shop_name: str = ""
    shop_location: str = ""
    image_url: str = ""
    product_url: str = ""
    category: str = ""
    timestamp: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

# ---------------------------------------------------------------------------
# 2captcha integration helpers
# ---------------------------------------------------------------------------
class CaptchaSolver:
    """Solve CAPTCHAs via 2captcha.com API."""

    API_BASE = "https://api.2captcha.com"

    def __init__(self, api_key: str = ""):
        self.api_key = api_key or TWOCAPTCHA_API_KEY
        if not self.api_key:
            log.warning("TWOCAPTCHA_API_KEY not set — CAPTCHA solving disabled")

    def solve_turnstile(self, sitekey: str, page_url: str) -> Optional[str]:
        """Solve Cloudflare Turnstile challenge."""
        if not self.api_key:
            return None
        return self._solve({
            "key": self.api_key,
            "method": "turnstile",
            "sitekey": sitekey,
            "pageurl": page_url,
            "json": 1,
        })

    def solve_recaptcha_v2(self, sitekey: str, page_url: str) -> Optional[str]:
        """Solve reCAPTCHA v2."""
        if not self.api_key:
            return None
        return self._solve({
            "key": self.api_key,
            "method": "userrecaptcha",
            "googlekey": sitekey,
            "pageurl": page_url,
            "json": 1,
        })

    def solve_hcaptcha(self, sitekey: str, page_url: str) -> Optional[str]:
        """Solve hCaptcha."""
        if not self.api_key:
            return None
        return self._solve({
            "key": self.api_key,
            "method": "hcaptcha",
            "sitekey": sitekey,
            "pageurl": page_url,
            "json": 1,
        })

    # internal -----------------------------------------------------------------
    def _solve(self, payload: dict) -> Optional[str]:
        import requests

        log.info("Submitting CAPTCHA to 2captcha.com …")
        resp = requests.post(f"{self.API_BASE}/in.php", data=payload)
        data = resp.json()
        if data.get("status") != 1:
            log.error("2captcha submission failed: %s", data)
            return None

        task_id = data["request"]
        log.info("Task %s created — polling for result …", task_id)

        for attempt in range(60):
            time.sleep(5)
            result = requests.get(
                f"{self.API_BASE}/res.php",
                params={"key": self.api_key, "action": "get", "id": task_id, "json": 1},
            ).json()
            if result.get("status") == 1:
                log.info("CAPTCHA solved ✓")
                return result["request"]
            if result.get("request") == "ERROR_CAPTCHA_UNSOLVABLE":
                log.error("CAPTCHA unsolvable")
                return None

        log.error("CAPTCHA solve timed out")
        return None

# ---------------------------------------------------------------------------
# Fingerprint evasion helpers
# ---------------------------------------------------------------------------
FINGERPRINT_JS = """
// ── Stealth: hide automation markers ──────────────────────────────────────

// 1. Remove webdriver flag
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
delete navigator.__proto__.webdriver;

// 2. Fake plugins array (real Chrome has ≥3)
Object.defineProperty(navigator, 'plugins', {
    get: () => {
        const arr = [
            {name:'Chrome PDF Plugin', filename:'internal-pdf-viewer', description:'Portable Document Format'},
            {name:'Chrome PDF Viewer', filename:'mhjfbmdgcfjbbpaeojofohoefgiehjai', description:''},
            {name:'Native Client', filename:'internal-nacl-plugin', description:''},
        ];
        arr.item = (i) => arr[i];
        arr.namedItem = (n) => arr.find(p => p.name === n);
        arr.refresh = () => {};
        return arr;
    }
});

// 3. Languages
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en', 'id']});
Object.defineProperty(navigator, 'language', {get: () => 'en-US'});

// 4. Chrome runtime object
window.chrome = {
    runtime: {
        connect: () => {},
        sendMessage: () => {},
        onMessage: {addListener: () => {}, removeListener: () => {}},
    },
    loadTimes: () => ({
        requestTime: Date.now() / 1000 - 1,
        startLoadTime: Date.now() / 1000 - 0.8,
        commitLoadTime: Date.now() / 1000 - 0.5,
        finishDocumentLoadTime: Date.now() / 1000 - 0.1,
        finishLoadTime: Date.now() / 1000,
        firstPaintTime: Date.now() / 1000 - 0.3,
        firstPaintAfterLoadTime: 0,
        navigationType: 'Other',
        wasFetchedViaSpdy: false,
        wasNpnNegotiated: true,
        npnNegotiatedProtocol: 'h2',
        wasAlternateProtocolAvailable: false,
        connectionInfo: 'h2',
    }),
    csi: () => ({
        startE: Date.now(),
        onloadT: Date.now(),
        pageT: Date.now() - performance.timing.navigationStart,
        tran: 15,
    }),
};

// 5. Patch permissions.query
const origQuery = window.navigator.permissions.query.bind(navigator.permissions);
window.navigator.permissions.query = (params) =>
    params.name === 'notifications'
        ? Promise.resolve({state: Notification.permission})
        : origQuery(params);

// 6. Fake connection info (non-headless)
Object.defineProperty(navigator, 'connection', {
    get: () => ({effectiveType: '4g', rtt: 50, downlink: 10, saveData: false}),
});

// 7. Hardware concurrency & device memory (headless defaults are suspicious)
Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8});
Object.defineProperty(navigator, 'deviceMemory', {get: () => 8});

// 8. Platform
Object.defineProperty(navigator, 'platform', {get: () => 'Win32'});

// 9. Hide automation-related window properties
['__webdriver_evaluate','__selenium_evaluate','__fxdriver_evaluate',
 '__driver_unwrapped','__webdriver_unwrapped','__driver_evaluate',
 '__selenium_unwrapped','__fxdriver_unwrapped','_Selenium_IDE_Recorder',
 '_selenium','calledSelenium','_WEBDRIVER_ELEM_CACHE',
 'ChromeDriverw','driver-evaluate','webdriver-evaluate',
 'selenium-evaluate','webdriverCommand','webdriver-evaluate-response',
 '__webdriverFunc','__webdriver_script_fn','__$webdriverAsyncExecutor',
 '__lastWatirAlert','__lastWatirConfirm','__lastWatirPrompt',
 '_phantom','__nightmare','domAutomation','domAutomationController',
].forEach(prop => { delete window[prop]; });

// 10. Patch iframe contentWindow to also hide webdriver
const origAttach = Element.prototype.attachShadow;
Element.prototype.attachShadow = function() { return origAttach.apply(this, arguments); };
"""

# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------
def save_json(products: list[Product], filepath: str) -> None:
    """Save list of Products as JSON."""
    os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump([p.to_dict() for p in products], f, ensure_ascii=False, indent=2)
    log.info("Saved %d products → %s", len(products), filepath)


def save_csv(products: list[Product], filepath: str) -> None:
    """Save list of Products as CSV."""
    if not products:
        return
    os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)
    keys = list(products[0].to_dict().keys())
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=keys)
        writer.writeheader()
        for p in products:
            writer.writerow(p.to_dict())
    log.info("Saved %d products → %s", len(products), filepath)


def random_delay(lo: float = 1.0, hi: float = 3.0) -> None:
    """Human-like random delay."""
    time.sleep(random.uniform(lo, hi))


def random_ua() -> str:
    return random.choice(USER_AGENTS)


async def retry_goto(page, url: str, retries: int = MAX_RETRIES, **kwargs) -> bool:
    """Navigate to URL with retry + exponential backoff. Returns True on success."""
    wait = RETRY_BACKOFF
    for attempt in range(1, retries + 1):
        try:
            await page.goto(url, **kwargs)
            return True
        except Exception as exc:
            err = str(exc)
            log.warning("Attempt %d/%d failed: %s", attempt, retries, err.split("\n")[0])
            if attempt < retries:
                jitter = random.uniform(0, wait * 0.3)
                log.info("Retrying in %.1fs …", wait + jitter)
                import asyncio as _aio
                await _aio.sleep(wait + jitter)
                wait *= 2
    return False
