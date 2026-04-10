# Tokopedia Scraper

Open-source web scraper for [Tokopedia](https://www.tokopedia.com) — Indonesia's largest e-commerce platform. Extract product data from **all 20+ categories** with built-in CAPTCHA solving, proxy rotation, fingerprint evasion, and anti-detect browser support.

Three implementations included: **Playwright** (recommended), **Selenium**, and **Pyppeteer** (Puppeteer for Python).

> **Part of the [2scraper](https://github.com/2scraper) collection** — production-ready scrapers powered by [2captcha.com](https://2captcha.com) and [2prx.com](https://2prx.com).

---

## Features

| Feature | Details |
|---|---|
| **20+ categories** | Electronics, Fashion, Beauty, Automotive, Gaming, and more |
| **3 browser engines** | Playwright (primary), Selenium, Pyppeteer |
| **CAPTCHA solving** | Cloudflare Turnstile, reCAPTCHA v2, hCaptcha via [2captcha.com](https://2captcha.com) |
| **Proxy support** | Residential & datacenter proxies via [2prx.com](https://2prx.com) |
| **Fingerprint evasion** | Stealth JS injection — hides webdriver flags, spoofs navigator properties |
| **Anti-detect browser** | Connect to any CDP-compatible anti-detect browser |
| **Output formats** | JSON, CSV, or both |
| **Pagination** | Infinite-scroll handling with configurable page limits |
| **Human-like behavior** | Random delays, user-agent rotation |

---

## Data Points Extracted

Each scraped product includes:

- **Product name**
- **Current price** & **original price**
- **Discount percentage**
- **Rating** & **units sold**
- **Shop name** & **shop location**
- **Product image URL**
- **Product page URL**
- **Category** & **timestamp**

### Sample Output (JSON)

```json
{
  "name": "Samsung Galaxy S24 Ultra 12/256GB",
  "price": "Rp17.999.000",
  "original_price": "Rp21.999.000",
  "discount": "18%",
  "rating": "4.9",
  "sold": "2rb+",
  "shop_name": "Samsung Official Store",
  "shop_location": "Jakarta Utara",
  "image_url": "https://images.tokopedia.net/...",
  "product_url": "https://www.tokopedia.com/...",
  "category": "electronics",
  "timestamp": "2025-06-15T12:30:00+00:00"
}
```

---

## Quick Start

### 1. Install

```bash
git clone https://github.com/2scraper/tokopedia-scraper.git
cd tokopedia-scraper
pip install -r requirements.txt

# Playwright only — install browser binaries:
playwright install chromium
```

### 2. Configure (optional)

Set environment variables for enhanced capabilities:

```bash
# CAPTCHA solving (get your key at https://2captcha.com)
export TWOCAPTCHA_API_KEY="your_api_key_here"

# Proxy rotation (get credentials at https://2prx.com)
export PROXY_URL="http://user:pass@gate.2prx.com:9999"

# Anti-detect browser (optional)
export ANTIDETECT_BROWSER_WS="ws://127.0.0.1:9222"
```

### 3. Run

**Playwright** (recommended):
```bash
# Scrape specific categories
python scraper_playwright.py --categories electronics fashion-men --pages 3 --output json

# Scrape ALL categories
python scraper_playwright.py --all --pages 5 --output both
```

**Selenium:**
```bash
python scraper_selenium.py --categories beauty health --pages 3 --output csv
```

**Pyppeteer:**
```bash
python scraper_puppeteer.py --all --pages 2 --output json
```

---

## Available Categories

| Key | Tokopedia Category |
|---|---|
| `electronics` | Elektronik |
| `mobile-tablets` | Handphone & Tablet |
| `laptops-computers` | Laptop & Aksesoris |
| `fashion-men` | Fashion Pria |
| `fashion-women` | Fashion Wanita |
| `beauty` | Kecantikan |
| `health` | Kesehatan |
| `home-living` | Rumah Tangga |
| `baby-kids` | Ibu & Bayi |
| `food-drinks` | Makanan & Minuman |
| `sports` | Olahraga |
| `automotive` | Otomotif |
| `books-stationery` | Buku |
| `toys-hobbies` | Mainan & Hobi |
| `office-industrial` | Office & Industrial |
| `cameras` | Kamera |
| `gaming` | Gaming |
| `pets` | Perawatan Hewan |
| `travel` | Travel & Aktivitas |
| `tickets-vouchers` | Tiket & Voucher |

---

## CLI Options

```
--categories KEY [KEY ...]   Category keys to scrape (see table above)
--all                        Scrape all categories
--pages N                    Number of pages per category (default: 5)
--output {json,csv,both}     Output format (default: json)
--output-dir PATH            Output directory (default: ./output)
```

---

## Architecture

```
tokopedia-scraper/
├── config.py                # Shared config, models, 2captcha integration
├── scraper_playwright.py    # Playwright implementation (primary)
├── scraper_selenium.py      # Selenium implementation
├── scraper_puppeteer.py     # Pyppeteer implementation
├── output/                  # Scraped data lands here
├── requirements.txt
└── README.md
```

All three scrapers share `config.py` which provides:

- **`Product`** data model with `.to_dict()` serialization
- **`CaptchaSolver`** class — unified 2captcha.com API client (Turnstile, reCAPTCHA, hCaptcha)
- **`CATEGORIES`** map of all Tokopedia categories
- **`FINGERPRINT_JS`** stealth injection script
- **Output helpers** — `save_json()`, `save_csv()`
- **Utility functions** — `random_delay()`, `random_ua()`

---

## Integrations

### CAPTCHA Solving — 2captcha.com

When Tokopedia or Cloudflare presents a challenge, the scraper automatically detects and solves it using the [2captcha.com](https://2captcha.com) API. Supported types:

- Cloudflare Turnstile
- reCAPTCHA v2
- hCaptcha

Set your API key: `export TWOCAPTCHA_API_KEY="your_key"`

### Proxy Rotation — 2prx.com

Avoid IP bans with residential and datacenter proxies from [2prx.com](https://2prx.com). The proxy URL format is:

```
http://username:password@gate.2prx.com:9999
```

Set via: `export PROXY_URL="http://user:pass@gate.2prx.com:9999"`

### Anti-Detect Browser

For maximum stealth, connect the scraper to a CDP-compatible anti-detect browser. This routes all scraping through a browser environment with unique fingerprints — canvas, WebGL, fonts, timezone, and more.

Set via: `export ANTIDETECT_BROWSER_WS="ws://127.0.0.1:9222"`

Contact us at [2captcha.com](https://2captcha.com) for our anti-detect browser solution.

---

## Tips for Reliable Scraping

1. **Always use proxies** — Tokopedia rate-limits aggressively. Residential proxies from [2prx.com](https://2prx.com) give the best success rate.
2. **Enable CAPTCHA solving** — Cloudflare Turnstile blocks are common. A [2captcha.com](https://2captcha.com) key ensures uninterrupted scraping.
3. **Start small** — Test with 1–2 categories and 2 pages before running a full scrape.
4. **Use Playwright** — It's the fastest and most reliable of the three implementations.
5. **Respect Tokopedia's terms of service** — This tool is intended for research and personal use.

---

## License

MIT — see [LICENSE](LICENSE).

---

## Links

- **Landing page**: [2captcha.com/tokopedia-scraper](https://2captcha.com/tokopedia-scraper)
- **CAPTCHA solving API**: [2captcha.com](https://2captcha.com)
- **Proxy service**: [2prx.com](https://2prx.com)
- **More scrapers**: [github.com/2scraper](https://github.com/2scraper)
