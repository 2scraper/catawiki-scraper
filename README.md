# 🏛️ Catawiki Scraper

**Open-source web scraper for [Catawiki](https://www.catawiki.com) auction listings.**  
Extract lots, bids, prices, images, and seller data — across every category.

Three browser-automation engines. Four extraction strategies. One unified interface.

| Engine | Script | Async | Best For |
|--------|--------|-------|----------|
| **Playwright** ⭐ | `catawiki_playwright.py` | ✅ | Speed, reliability, network interception |
| **Selenium** | `catawiki_selenium.py` | ❌ | Legacy stacks, CI pipelines |
| **Puppeteer** (Pyppeteer) | `catawiki_puppeteer.py` | ✅ | Node.js ecosystem familiarity |

---

## ✨ Features

- **All categories** — art, watches, jewellery, coins, cars, and 25+ more
- **Smart extraction** — four strategies tried automatically (see below)
- **CAPTCHA bypass** — Turnstile / reCAPTCHA / hCaptcha solving via [2captcha.com](https://2captcha.com)
- **Proxy support** — residential & datacenter proxies via [2prx.com](https://2prx.com)
- **Fingerprint randomisation** — rotating User-Agent, viewport, timezone, locale
- **Anti-detection** — webdriver masking, `undetected-chromedriver`, stealth init scripts
- **Debug mode** — `--debug` dumps page HTML & intercepted API responses for inspection
- **Flexible output** — JSON or CSV with timestamped filenames
- **Pagination** — automatic multi-page traversal

---

## 🧠 How It Works — Four Extraction Strategies

Catawiki is a React SPA (Next.js) that loads data dynamically. Static HTML scraping alone won't cut it. The scraper tries **four strategies in order**, using the first one that returns data:

| # | Strategy | How | Reliability |
|---|----------|-----|-------------|
| 1 | **API Interception** | Captures internal API / GraphQL / `_next/data` responses via browser network events | ⭐⭐⭐ Highest |
| 2 | **`__NEXT_DATA__`** | Parses the JSON blob that Next.js embeds in `<script id="__NEXT_DATA__">` | ⭐⭐⭐ High |
| 3 | **JSON-LD** | Extracts structured data from `<script type="application/ld+json">` | ⭐⭐ Medium |
| 4 | **DOM Parsing** | Finds `<a href="/l/...">` elements and walks up to card containers | ⭐ Fallback |

> **Tip:** Run with `--debug` to see exactly what each strategy captures. HTML and API JSON are saved to `debug/`.

---

## 🚀 Quick Start

### 1. Clone the repo

```bash
git clone https://github.com/2scraper/catawiki-scraper.git
cd catawiki-scraper
```

### 2. Install dependencies

```bash
pip install -r requirements.txt

# For Playwright — install browser binaries:
playwright install chromium
```

### 3. Set your API keys (optional but recommended)

```bash
export TWOCAPTCHA_API_KEY="your_2captcha_api_key"   # https://2captcha.com
export PROXY_URL="http://user:pass@host:port"        # https://2prx.com
```

### 4. Run

```bash
# Playwright (recommended)
python catawiki_playwright.py --all --pages 3 --format json

# Selenium
python catawiki_selenium.py --categories art watches --pages 5 --format csv

# Puppeteer
python catawiki_puppeteer.py --categories jewellery --details --headed
```

### 5. Debug mode (when things don't work)

```bash
python catawiki_playwright.py --categories art --debug --headed
# Inspect debug/ folder for HTML dumps and intercepted API JSONs
```

---

## 📋 CLI Options

| Flag | Description | Default |
|------|-------------|---------|
| `--categories` | Space-separated list of categories to scrape | — |
| `--all` | Scrape ALL categories | `false` |
| `--pages` | Max listing pages per category | `3` |
| `--format` | Output format: `json` or `csv` | `json` |
| `--details` | Also visit each lot's detail page | `false` |
| `--headed` | Show the browser window | `false` |
| `--debug` | Save page HTML & API responses to `debug/` | `false` |

---

## 🗂️ Available Categories

```
antiques          art                asian-art          books-comics
cars-motorcycles  ceramics           coins-banknotes    collectibles
diamonds-gemstones  dolls-bears      fashion            furniture
interiors         jewellery          memorabilia        militaria
model-cars        music              photography        science-technology
sculptures        silver             sports             stamps
toys              vinyl-music        watches            wine-whisky
```

---

## 📦 Output Format

### JSON

```json
[
  {
    "id": "98765432",
    "title": "Pablo Picasso (1881-1973) - Femme assise",
    "url": "https://www.catawiki.com/en/l/98765432-picasso-femme-assise",
    "price": "2400 EUR",
    "currency": "EUR",
    "bids": "23",
    "closing_time": "2025-04-15T18:00:00Z",
    "image_url": "https://assets.catawiki.nl/...",
    "category": "art",
    "seller": "ArtGalleryAmsterdam",
    "description": "Original lithograph, signed in pencil…"
  }
]
```

### CSV

| id | title | url | price | bids | closing_time | image_url | category | seller |
|----|-------|-----|-------|------|--------------|-----------|----------|--------|
| 98765432 | Picasso … | https://… | 2400 EUR | 23 | 2025-04-15… | https://… | art | ArtGallery… |

---

## 🔐 CAPTCHA Solving with 2captcha.com

Catawiki may serve Cloudflare Turnstile or other CAPTCHA challenges. The scraper detects them automatically and solves via the [2captcha.com](https://2captcha.com) API.

1. Sign up at [2captcha.com](https://2captcha.com)
2. Copy your API key
3. Set the environment variable:

```bash
export TWOCAPTCHA_API_KEY="your_key_here"
```

Supported: Turnstile, reCAPTCHA v2/v3, hCaptcha. Average solve: 10–25s. Success rate: 99%+.

---

## 🌐 Proxy Support via 2prx.com

Route requests through [2prx.com](https://2prx.com) proxies to avoid IP bans.

```bash
export PROXY_URL="http://user:pass@gate.2prx.com:9090"
```

---

## 🕵️ Anti-Detection & Fingerprinting

Built-in stealth features:

- **User-Agent rotation** — random desktop UA per session
- **Viewport randomisation** — realistic screen sizes
- **Timezone & locale spoofing** (Playwright)
- **WebDriver flag masking** — `navigator.webdriver → undefined`
- **Chrome runtime emulation** — `window.chrome.runtime` stub
- **Plugin count spoofing**

For maximum stealth, consider the **2captcha Anti-Detect Browser** — premium Chromium with hardware-level fingerprint masking. [Learn more →](https://2captcha.com)

---

## 🏗️ Project Structure

```
catawiki-scraper/
├── catawiki_playwright.py   # ⭐ Primary scraper (async, network interception)
├── catawiki_selenium.py     # Selenium scraper (sync, perf log interception)
├── catawiki_puppeteer.py    # Pyppeteer scraper (async, network interception)
├── config.py                # Shared config, extraction strategies, 2captcha API
├── requirements.txt         # Python dependencies
├── output/                  # Scraped data (JSON/CSV)
├── debug/                   # HTML dumps & API captures (--debug mode)
└── README.md
```

---

## ⚙️ Environment Variables

| Variable | Description | Required |
|----------|-------------|----------|
| `TWOCAPTCHA_API_KEY` | API key from [2captcha.com](https://2captcha.com) | No (but recommended) |
| `PROXY_URL` | Proxy URL from [2prx.com](https://2prx.com) — `http://user:pass@host:port` | No |
| `FINGERPRINT_ENABLED` | Fingerprint randomisation (`true`/`false`) | No (default: `true`) |

---

## 🐛 Troubleshooting

**0 lots found?** This almost always means the page returned a challenge or block. Steps:

1. Run with `--debug --headed` to see what the browser is seeing
2. Check `debug/` for the HTML dump — if it's very short (<5KB), it's a Cloudflare challenge
3. Set `PROXY_URL` with a residential proxy from [2prx.com](https://2prx.com)
4. Set `TWOCAPTCHA_API_KEY` from [2captcha.com](https://2captcha.com)
5. If the page loads but selectors miss, open the HTML dump and look for the data structure, then adjust selectors in `config.py`

**Category 404?** Catawiki occasionally changes category URL slugs. Run with `--debug` to see the redirect, then update the path in `config.py → CATEGORIES`.

---

## 🤝 Contributing

Contributions are welcome! Please open an issue or submit a pull request.

---

## 📄 License

MIT License — see [LICENSE](LICENSE) for details.

---

## 🔗 Links

- **Repository**: [github.com/2scraper/catawiki-scraper](https://github.com/2scraper/catawiki-scraper)
- **CAPTCHA solving**: [2captcha.com](https://2captcha.com)
- **Proxy service**: [2prx.com](https://2prx.com)
- **Anti-Detect Browser**: [2captcha.com](https://2captcha.com)

---

<p align="center">
  Built with ❤️ by <a href="https://github.com/2scraper">2scraper</a> · Powered by <a href="https://2captcha.com">2captcha.com</a>
</p>
