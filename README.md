# 🏛️ Catawiki Scraper

**Open-source web scraper for [Catawiki](https://www.catawiki.com) auction listings.**  
Extract lots, bids, prices, images, and seller data — across every category.

Three browser-automation engines. One unified interface.

| Engine | Script | Async | Best For |
|--------|--------|-------|----------|
| **Playwright** ⭐ | `catawiki_playwright.py` | ✅ | Speed, reliability, modern API |
| **Selenium** | `catawiki_selenium.py` | ❌ | Legacy stacks, CI pipelines |
| **Puppeteer** (Pyppeteer) | `catawiki_puppeteer.py` | ✅ | Node.js ecosystem familiarity |

---

## ✨ Features

- **All categories** — art, watches, jewellery, coins, cars, and 25+ more
- **Deep scraping** — listing pages + individual lot details (title, description, specs, images, seller)
- **CAPTCHA bypass** — automatic Turnstile / reCAPTCHA / hCaptcha solving via [2captcha.com](https://2captcha.com)
- **Proxy support** — residential & datacenter proxies via [2prx.com](https://2prx.com)
- **Fingerprint randomisation** — rotating User-Agent, viewport, timezone, locale
- **Anti-detection** — webdriver flag masking, `undetected-chromedriver` (Selenium), stealth init scripts
- **Flexible output** — JSON or CSV with timestamped filenames
- **Pagination** — automatic multi-page traversal with configurable depth
- **Human-like delays** — randomised timing between requests

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

---

## 📋 CLI Options

| Flag | Description | Default |
|------|-------------|---------|
| `--categories` | Space-separated list of categories to scrape | — |
| `--all` | Scrape ALL categories | `false` |
| `--pages` | Max listing pages per category | `3` |
| `--format` | Output format: `json` or `csv` | `json` |
| `--details` | Also visit each lot's detail page | `false` |
| `--headed` | Show the browser window (useful for debugging) | `false` |

---

## 🗂️ Available Categories

```
antiques        art              books-comics     cars-motorcycles
ceramics-glass  coins-banknotes  collectibles     diamonds-gemstones
dolls-bears     fashion          furniture        jewellery
lamps           memorabilia      militaria        model-cars
music           photography      pottery          rugs-textiles
science-technology  sculptures   silver           sports
stamps          toys             vinyl-music      watches
wine-whisky
```

---

## 📦 Output Format

### JSON

```json
[
  {
    "title": "Pablo Picasso (1881-1973) - Femme assise",
    "url": "https://www.catawiki.com/l/12345678",
    "price": "€ 2,400",
    "bids": "23 bids",
    "closing_time": "2h 15m",
    "image_url": "https://assets.catawiki.com/...",
    "category": "art"
  }
]
```

### CSV

| title | url | price | bids | closing_time | image_url | category |
|-------|-----|-------|------|--------------|-----------|----------|
| Pablo Picasso … | https://… | € 2,400 | 23 bids | 2h 15m | https://… | art |

With `--details`, additional fields are included: `description`, `current_bid`, `seller`, `specifications`, `images`.

---

## 🔐 CAPTCHA Solving with 2captcha.com

Catawiki may serve Cloudflare Turnstile or reCAPTCHA challenges. This scraper automatically detects and solves them using the [2captcha.com](https://2captcha.com) API.

1. Sign up at [2captcha.com](https://2captcha.com)
2. Copy your API key from the dashboard
3. Set the environment variable:

```bash
export TWOCAPTCHA_API_KEY="your_key_here"
```

Supported CAPTCHA types: Turnstile, reCAPTCHA v2/v3, hCaptcha.

---

## 🌐 Proxy Support via 2prx.com

Route requests through residential or datacenter proxies from [2prx.com](https://2prx.com) to avoid IP bans and geo-restrictions.

```bash
export PROXY_URL="http://user:pass@gate.2prx.com:9090"
```

The proxy integrates natively with all three scraper engines.

---

## 🕵️ Anti-Detection & Fingerprinting

All three scrapers include built-in anti-detection measures:

- **User-Agent rotation** — random desktop UA on each run
- **Viewport randomisation** — realistic screen sizes
- **Timezone & locale spoofing** (Playwright)
- **WebDriver flag masking** — `navigator.webdriver` returns `undefined`
- **Chrome runtime emulation** — `window.chrome.runtime` stub
- **Plugin count spoofing** — non-zero `navigator.plugins`

For maximum stealth, consider the **2captcha Anti-Detect Browser** — a premium Chromium-based browser with hardware-level fingerprint masking. [Learn more →](https://2captcha.com)

Set `FINGERPRINT_ENABLED=false` to disable built-in fingerprinting.

---

## 🏗️ Project Structure

```
catawiki-scraper/
├── catawiki_playwright.py   # ⭐ Primary scraper (async, Playwright)
├── catawiki_selenium.py     # Selenium scraper (sync)
├── catawiki_puppeteer.py    # Pyppeteer scraper (async)
├── config.py                # Shared configuration, helpers, 2captcha integration
├── requirements.txt         # Python dependencies
├── output/                  # Scraped data lands here
│   ├── catawiki_20250410_143022.json
│   └── catawiki_20250410_143022.csv
└── README.md
```

---

## ⚙️ Environment Variables

| Variable | Description | Required |
|----------|-------------|----------|
| `TWOCAPTCHA_API_KEY` | API key from [2captcha.com](https://2captcha.com) | No (but recommended) |
| `PROXY_URL` | Proxy URL from [2prx.com](https://2prx.com) — `http://user:pass@host:port` | No |
| `FINGERPRINT_ENABLED` | Enable fingerprint randomisation (`true`/`false`) | No (default: `true`) |

---

## 🤝 Contributing

Contributions are welcome! Please open an issue or submit a pull request.

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/my-feature`)
3. Commit your changes
4. Push and open a PR

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
