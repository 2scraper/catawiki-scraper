# Catawiki Scraper — Auction Lots, Bids and Estimates in Python

[![release](https://img.shields.io/github/v/release/2scraper/catawiki-scraper?sort=semver)](https://github.com/2scraper/catawiki-scraper/releases)
[![tests](https://github.com/2scraper/catawiki-scraper/actions/workflows/tests.yml/badge.svg)](https://github.com/2scraper/catawiki-scraper/actions/workflows/tests.yml)
[![canary](https://github.com/2scraper/catawiki-scraper/actions/workflows/canary.yml/badge.svg)](https://github.com/2scraper/catawiki-scraper/actions/workflows/canary.yml)
[![python](https://img.shields.io/badge/python-3.9%20%7C%203.13-blue)](pyproject.toml)
[![licence](https://img.shields.io/badge/licence-MIT-green)](LICENSE)
[![engines](https://img.shields.io/badge/engines-Playwright%20%7C%20Selenium%20%7C%20pyppeteer%20%7C%20CDP-informational)](#engines)
[![runs without an account](https://img.shields.io/badge/runs%20without-an%20account-brightgreen)](#do-you-need-any-of-the-paid-products)

**Catawiki scraper** — an open-source (MIT) Python tool that reads catawiki.com category listings, search results, whole auctions and single lot pages and writes auction lots — current or final bid, bid count, reserve status, estimate and seller — to JSON or CSV. Measured on 2026-09-10 against `/en/c/333-watches`, the site refused every headless browser whatever the exit address and served a headful one, so the scraper runs headful by default. Use it to build a dataset of lot prices and reserves, or compare two runs with `diff_runs.py`.

Scrapes [Catawiki](https://www.catawiki.com) auction lots — category listings,
search results, whole auctions and single lot pages — to JSON or CSV, with
four interchangeable browser back ends and one row schema shared with the rest
of the [2scraper](https://github.com/2scraper) family.

Everything below that states a number states when it was measured and on what.
Anything not measured is not claimed.

---

## The one thing to know first

**This site refuses a headless browser, and no proxy fixes that.** Measured
2026-09-10 against `/en/c/333-watches`:

| Client | Exit address | Result |
|---|---|---|
| `curl` with a full browser header set | local (datacentre-classified) | **403**, 394 bytes |
| `curl` with a full browser header set | residential NL | **403** |
| Chromium **headless** | local | **403**, 308 bytes |
| Chromium **headless** | residential NL (×3 exits) | **403** |
| Chrome **headless** | residential NL | **403** |
| Chrome **headful** | local (datacentre-classified) | **200**, 633 KB, 24 lots |
| Chrome **headful** | residential NL (×2 exits) | **200**, 24 lots |
| Scraping Browser API over CDP | residential NL | **200**, 637 KB, 24 lots |

Read it by column, not by row: the exit address changes nothing and the
headless flag changes everything. The "datacentre-classified" rows are refused
because of the **client** (`curl`, a headless browser), not the address: the
same address with a headful browser is served. So:

* **`--headful` is the default here.** A `--headless` default would be a scraper whose default cannot
  fetch the site.
* `--headless` is still accepted, because `--cdp-endpoint` ignores it and
  because a change on the site would make it work again. It will report
  exit 3 today.
* A red run is much more likely to mean "headless" than "bad IP". The block
  message says so rather than sending you to buy a proxy.

---

## Install

Core dependencies are `beautifulsoup4` and `requests`. **Install exactly one
engine**: their pins are mutually unsatisfiable (playwright and pyppeteer
disagree on `pyee`, pyppeteer and selenium on `urllib3`), and while all three
do run side by side in practice, `pip check` reports the conflict and pip may
resolve it by downgrading something you wanted. Use a virtualenv per engine if
you need more than one.

```bash
pip install -r requirements.txt -r requirements-playwright.txt
python -m playwright install chromium         # Playwright only
```

```bash
pip install -r requirements.txt -r requirements-selenium.txt    # or
pip install -r requirements.txt -r requirements-puppeteer.txt
```

Python 3.9 through 3.13; both ends are tested in CI.

---

## Usage

```bash
# A category listing, three pages, JSON and CSV
python3 playwright_scraper.py \
    --url 'https://www.catawiki.com/en/c/333-watches' \
    --pages 3 --format both --out watches

# A search
python3 playwright_scraper.py --url 'https://www.catawiki.com/en/s?q=rolex' --pages 2

# A whole auction — every lot at once, and see the note below on why this is
# the best source of the four
python3 playwright_scraper.py \
    --url 'https://www.catawiki.com/en/a/1243988-figures-figurines-auction'

# One lot, in full: estimate, seller, specifications, both absolute times
python3 playwright_scraper.py --mode lot \
    --url 'https://www.catawiki.com/en/l/106583855-omega-de-ville-prestige'

# The auctions index as a work list: one row per auction, feed the urls back in
python3 playwright_scraper.py --mode auctions --url 'https://www.catawiki.com/en/a'
```

`puppeteer_scraper.py` and `selenium_scraper.py` take the same flags. See
[engine differences](#engines).

### Prefer auction pages to category pages

An auction page (`/{locale}/a/{id}-{slug}`) carries **every one of its lots in
one response** — 130 of 130 on the page measured — and beside them the
auction's own `closeAt` and status. A category or search listing carries 24
lots per page and states **no absolute time anywhere**, only a relative timer
("3 days left"), which means nothing in a dataset read tomorrow. If you are
monitoring prices, walk the auctions index and then the auctions.

---

## What comes out

One row per lot, same field order in JSON and CSV, the family prefix first
(`source, scraped_at, url, sku, title, brand, price, currency,
original_price, discount_pct, rating, review_count, in_stock, image_url,
category, price_source, page, position`) and this site's own columns after it.
`sample_output.json` and `sample_output.csv` are cut from a real run.

In the CSV only, a text cell that starts with `=`, `+`, `-`, `@`, a tab or a
line break gets a leading apostrophe so a spreadsheet shows it as text rather
than running it as a formula; numbers are left alone and the JSON keeps the
site's text unchanged. The sidecar's `csv_cells_escaped` says how many cells
were changed.

The columns that need explaining, because an auction is not a shop:

**`price` means three different things, and `bid_kind` says which.**

| `bid_kind` | What `price` is |
|---|---|
| `current` | the live high bid |
| `final` | the last bid on a closed lot — a hammer price only if `sold` is also true |
| `starting` | **nobody has bid**. A floor, not a bid. |
| `null` | no price was published for this lot (see below) |

One captured lot reached €1,300 with `reserve_price_met` false and `sold`
false: it changed hands for nothing at all. Treating `final` as a sale price
without reading `sold` would put that €1,300 in a price history.

**A null price is normal and means a reserve.** Every blank price is a lot
whose reserve has not been met — 57 of 57 blanks across 13 captures carried
`reserve_price_set: true`, spread evenly through the page rather than
clustered at its end. Price coverage was 87–100% on 24-lot category and search
pages and 68% on a 130-lot auction with a heavier reserve mix, so **the useful
check is the invariant, not a percentage**: a null price always carries
`reserve_price_set`. The suite and the canary assert exactly that.

**`bid_count` is a floor.** The site returns the last ten bids and states no
total; two lots with very different activity both reported exactly ten.
`bid_count_is_floor` is what tells you which kind of number you have.

**`favorite_count` comes from the rendered card, not from the payload.** The
payload's own `favoriteCount` reads 0 on 288 of 288 lots across 12 captures
while the card shows the real figure on all 24 of each — a field that is
present, authoritative-looking and uniformly wrong. Reading it would have
shipped a column of zeros at 100% coverage.

**`brand` is null on listing rows.** A listing title reads "Cartier - Tank
Must de Cartier PM - No reserve price - ..."; splitting on the dash would be
a guess presented as a fact. `--mode lot` fills it from the lot's own
specification field.

**`price_source` records where the money came from**: `next_data+dom` on a
listing (the payload for everything else, the rendered card for the amount),
`next_data` in `--mode lot`, `dom` on the fallback path. `diff_runs.py`
reports a price difference that comes with a `price_source` difference as
`source_changed` rather than `changed`.

**Relative times stay relative.** `time_left_text` is the card's own phrase,
verbatim, in the page's language. It is not converted to a timestamp, because
a listing page states nothing absolute and the conversion would be your
clock dressed up as the site's fact. `bidding_start_at` / `bidding_end_at` are
absolute and come from a lot page; `auction_close_at` is absolute and comes
from an auction page.

`--mode auctions` writes a different, deliberately thin row: `sku` (the
auction id), `url`, `title`, `ends_text`, `slug`, `locale`. The index states
no absolute time, no reliable lot count (the badge in the card's corner reads
`+127` on one card and `18+` on the next), so those are not published from it.
The auction's own page is the authority, and it carries its lots too.

### Exit codes

`0` ok · `1` crash · `2` bad usage · `3` blocked · `4` zero lots · `5` the
pages were never obtained (a navigation timeout, a dead or unauthenticated
proxy, a refused Scraping Browser connection, a Scraper API error) and
nothing was gathered · `6` partial. Every run writes `<out>.meta.json` beside its output
with `status`, `stop_reason` and **which** pages failed by number. A failed
run writes no sidecar and leaves the previous good output in place — a run
that finds nothing does not overwrite last night's data unless you pass
`--allow-empty`.

---

## Locales

18 of them, from the site's own `hreflang` set, all on `www.catawiki.com`
under a path prefix: `en nl de fr it es pt da sv no pl el hu ro fi ja zh-Hans
zh-Hant`. Live-verified: `en`, `de`, `nl`, `pl`, `ja`, `zh-Hant`.

Three things about them that look like bugs and are not:

* **The category slug is translated** (`333-watches`, `333-horloges`,
  `333-armbanduhren`) while the numeric id is stable. The id is the key; the
  site canonicalises a foreign slug itself.
* **Two locale codes carry capitals** — `zh-Hans`, `zh-Hant` — and are served
  case-sensitively.
* **Prices are in euro on every locale**, Japanese and Chinese included. Only
  the written form changes: `€1,535` (en, ja, zh) · `€ 1.535` (nl, pl) ·
  `1.535 €` (de). All five forms are pinned in the test suite.

`total` differs slightly by locale (11,681 on en/nl/de/pl against 11,675 on
ja/zh-Hant, 2026-09-10), which is the site's own filtering, not a parse error.

---

## Pagination stops at 100 pages, and that is the site's limit

`?page=N`, and page 1's payload states `total` and `lotsPerPage` — so the page
count is arithmetic and workers can be handed independent pages without
following a chain.

But the site clamps: `?page=99999` on an 11,681-lot category returned HTTP
**200** with `currentPage: 100` and page 100's own 24 lots. It does not fail
and it does not empty. A planner that ignores the cap therefore re-fetches
page 100 forever, adds no new `sku`, and a data-based terminator reads
"listing exhausted" — a run reported **complete** holding 2,400 of 11,681
lots. This scraper caps page planning at 100, on both the URL it builds and
any link the site offers, and reports how many pages the catalogue has beyond
the cap.

**A run counts pages from where its URL starts.** `--pages 3` on a URL that
carries `?page=99` fetches site pages 99 and 100 and stops: it never asks for a
page the site will not address, keeps what it gathered, and records
`pages_capped_by_site: true` and `pages_requested_by_user` in the sidecar. The
`page` column holds the site's own page number (99, 100), not the run's
count. A start page past 100 is a usage error (exit 2), because the site would
answer it with page 100's lots.

A page that comes back empty although the listing's own count says it exists is
fetched once more. If it is empty again the run stops with `stop_reason`
`listing_ended_early`, `status: partial` and exit 6, rather than calling a
glitch the end of the listing and reporting a complete run.

A search that matches nothing is a related trap: HTTP 200, the words "No
results", **and 24 suggested lots** reported as `total: 24`. That state is
detected from the payload's own `extended_search_result` flag and is **not
parsed** — writing two dozen plausible rows for a query that matched nothing
is worse than writing none. A search with no matches stops at page 1 (page 2
of it is not a further answer) and ends as exit 4; with `--allow-empty` it
writes a sidecar with `status: complete` and `products: 0`. The site's own "Page
Not Found" for a withdrawn lot is read the same way: an answer about the
address, exit 4, not a block.

---

## Engines

Playwright is primary. All four back ends produce the same rows, the same
columns in the same order, the same exit codes and the same run status —
verified on a live two-page run: 48 rows each, identical `sku` sets, 44
identical columns (2026-09-11).

| | Playwright | pyppeteer | Selenium | `--cdp-endpoint` |
|---|---|---|---|---|
| Live-verified here | yes | yes | yes | yes |
| Authenticated remote CDP | yes | yes | **no** | — |
| Authenticated proxy | yes | yes | **no** | n/a |
| `--fingerprint`, `--fp-tags`, `--fp-country` | yes | no | yes | ignored |
| `--locale` | yes | no | no | — |
| `--chromium-path` | no | yes | no | — |

* **Selenium cannot use an authenticated remote CDP endpoint.** Playwright's
  `connect_over_cdp` and pyppeteer's `browserWSEndpoint` take a full
  `ws://user:pass@host:port` and authenticate on the WebSocket upgrade;
  chromedriver's `debuggerAddress` takes a bare `host:port` with nowhere to
  put a password. It is not a generic "connect to CDP" option.
* **Selenium's `--proxy-server` cannot authenticate at all.** A proxy with
  credentials is refused before a browser starts (exit 2), the same as a
  credentialed `--cdp-endpoint`; it used to run unauthenticated and end as
  "blocked". See the whitelist recipe below, which makes a proxy work in
  Selenium anyway.
* **pyppeteer is effectively unmaintained** and its own README points at
  Playwright. It is here for parity, not because it is a good idea.
* Credentials never reach a command line or a log in any engine — including
  exception messages, which are logs. They are passed through the driver's own
  fields.

---

## Do you need any of the paid products?

**No.** A local headful Chrome on an ordinary connection returned 24 of 24
lots with full price coverage, with no key, no proxy and no account
(2026-09-10). Start there.

Rechecked 2026-11-05 from a datacentre address (Hetzner, Finland), no key, no
proxy, Playwright headful under `xvfb-run`, one request per route:

| Route | Mode | Headful, no proxy | Headless, same address |
|---|---|---|---|
| Category listing `/en/c/333-watches` | `listing` | **served**, 24 of 24 lots priced | **refused**, 403, 308 bytes (exit 3) |
| Auctions index `/en/a` | `auctions` | **served**, 24 auctions | not tested |
| One auction `/en/a/{id}-{slug}` | `listing` | **served**, 350 lots (an auction closing that day; 59 still showed a bid) | not tested |
| One lot `/en/l/{id}-{slug}` | `lot` | **served**, 1 lot | not tested |

No route needed a paid product. What the site gates is the client: headless
was refused on the listing and a plain HTTP client (`curl`) got 403 on it from
the same address the same day, so run every route with a browser **with a
window** (a desktop session or `xvfb-run`). Where no display is available, the
Scraping Browser API below is the remote alternative (verified 2026-09 on all
three modes).

What the four separately billed [2Captcha](https://2captcha.com) products
actually buy on this site:

* **The Scraping Browser API** (`--cdp-endpoint`) — a remote browser, so you
  run no browser infrastructure and get an exit in a country you choose.
  Verified: 200 and the full catalogue from a residential Dutch exit, on
  `--mode listing`, `--mode lot` and `--mode auctions`. One live connection
  per `pid`, so `--concurrency` above 1 is ignored with a warning that says
  why; use several pids. Starting a run right after another has closed can meet
  the previous connection still releasing the profile (HTTP 500); the connect
  is retried four times, ten seconds apart, and the log says the profile is
  probably still held.
* **Proxies** (`--proxy`, `--proxy-file`) — volume from many addresses. They
  do **not** get a headless browser in.
* **Captcha solving** (`--solve-captcha`, default `when-blocked`) — nothing
  to solve here today. Across 19 captures and 6 locales this site rendered no
  reCAPTCHA, hCaptcha, Turnstile or PerimeterX challenge to an anonymous
  visitor, and ships no captcha mount point or site key in its pages either.
  The detectors are kept for the shapes a challenge would take, so an
  appearance is recognised rather than reported as an empty page. Its
  Content-Security-Policy does allow PerimeterX hosts, but nothing from them
  appears on any page measured — a CSP is a statement of policy, not of
  presence.
* **Fingerprints** (`--fingerprint`) — a consistent device identity. Pass
  **one** OS-family tag to `--fp-tags` (`Windows`, `Microsoft Windows` or
  `Android`); the API rejects a list, and `Chrome`, `Desktop` and `Mobile`
  are each rejected on their own with HTTP 400.

Never set a fingerprint, a user agent or a proxy together with
`--cdp-endpoint`: the remote browser brings its own, and stacking a second
creates a contradiction rather than better cover.

### The proxy recipe that works in every engine

Chromium cannot authenticate a SOCKS5 proxy (Playwright refuses at launch)
and Selenium cannot authenticate any proxy. 2Captcha's IP-whitelist mode side-
steps both: whitelist your address, then

```bash
curl "https://api.2captcha.com/proxy/generate_white_list_connections\
?key=$TWOCAPTCHA_KEY&ip=YOUR.IP.HERE&protocol=socks5&connection_count=3&country=nl"
```

returns one `host:port` per exit with **no credentials in them at all**. Save
them to a file, pass `--proxy-file`, and the credential never enters your
config. Measured 2026-09-11: three ports, three different residential Dutch
addresses, working in Playwright, Selenium and pyppeteer alike — and active
immediately, where the `protocol=http` connections generated the same way
never answered.

---

## Measured on this site

| | Value | When |
|---|---|---|
| Lots per listing page | 24 | 2026-09-10 |
| Lots on the auction page measured | 130, in one response | 2026-09-10 |
| Category `total` (`/en/c/333-watches`) | 11,638–11,681, and it moves | 2026-09-10/11 |
| Pagination cap the site enforces | 100 pages | 2026-09-10 |
| Price coverage, category and search | 87–100% | 2026-09-10 |
| Price coverage, the 130-lot auction | 68%, all blanks reserve lots | 2026-09-10 |
| Blank prices that were reserve lots | 57 of 57 | 2026-09-10 |
| Bid history the site returns | last 10, no total | 2026-09-10 |
| Hydration of the prices after commit | 1.9–2.4 s, all at once | 2026-09-10 |
| Scroll rounds needed | none — 3 scrolls added 0 cards | 2026-09-10 |
| JSON-LD blocks: category / search / lot | 1 (poor) / 0 / 0 | 2026-09-10 |
| Live three-engine agreement | 48 rows, identical sku sets, 44 columns | 2026-09-11 |

The rows and everything except the money come from the site's own SSR payload
(`__NEXT_DATA__`), which is present in the first response and needs no
JavaScript at all — a run whose readiness wait times out loses the prices, not
the rows. The amount, the bid state and the favourite count arrive with
hydration: the server sends 24 card shells with 24 **empty** price nodes, so
readiness here waits for a price node with something in it rather than for a
card count, which would be satisfied instantly while every price is blank.

---

## Traps that look like bugs

* A **null price** is a reserve lot, not a parse failure (see above).
* A **negative-looking discount** cannot happen: `discount_pct` is computed,
  and returns null rather than 0 or a negative when the two figures are not
  what they were taken for.
* **`rating` and `review_count` are null on every row.** Catawiki publishes no
  per-lot rating; the seller's feedback score is in `seller_score` and
  `seller_feedback_count` on a `--mode lot` row. The two family columns are
  kept so one schema works across the family.
* **A lot page's DOM is not read**, on purpose: it renders 20–40 *other* lots
  in a "similar lots" carousel using the same class a listing uses for its own
  price, so "the first euro amount on the page" is a neighbour's number.
* **`?page=2` on a lot or auction page does nothing** — neither has a second
  page. `--concurrency` above 1 is ignored there, with a warning that says why.
* **A `profile_locked` error from `--cdp-endpoint`** means another run still
  holds that `pid`. One live connection per profile.
* An **empty price node containing a zero-width space** is the site's own
  placeholder. It is treated as no price; a truthiness check on the node
  would report 100% coverage and write an invisible character into every row.

---

## Diffing two runs

```bash
python3 diff_runs.py --old old.json --new new.json
```

Compares by `sku` and refuses to compare runs that are not both `complete`,
because a partial run's un-fetched pages otherwise read as delisted lots. It
also refuses to compare two different `--mode` runs, and two runs gathered from
different listings (a category against a search, say): each sidecar records the
`start_url`, and two catalogues would otherwise read as every lot removed.

On an auction site, expect most differences between two runs to be auctions
running rather than data changing: a lot's `bid_kind` moving `current` →
`final` is a lifecycle transition, not a price change.

---

## Testing

```bash
python3 smoke_test.py        # 460+ offline checks, no engine library needed
pytest                       # the same checks, wrapped as one test
python3 env_config.py        # what config was picked up, without secrets
```

Every fixture in the suite is cut from a real capture and verified to parse
identically to the untrimmed original before being embedded, and personal
material is replaced with placeholders and guarded by patterns: a lot page's
payload carries a per-bidder token, and an auction card names the human who
curated it.

CI runs the offline suite on the oldest and newest supported Python, and builds
the Docker image, runs its entrypoint, launches Chromium (headless) inside it
and checks that no `.env`, test suite or fixture was baked in. The CI launch is
headless and fetches nothing; that the image can fetch with a window was
checked by hand on 2026-11-10 (the image starts its own virtual display with
`xvfb-run` under `tini`, so `docker run --init` is not needed).

**What the canary badge here means.** The workflow is a real three-page run
against a real category URL, with a floor on the rows, the reserve invariant,
page+position uniqueness and a dozen other assertions — and it runs **weekly (Mondays),
from a stock GitHub runner, with no secret**. Akamai refuses a headless browser
here, so the canary runs a real headful Chromium inside a virtual display
(`xvfb-run`). Its first run that way, on 2026-09-23, fetched 72 lots over 3
pages, 72 of them priced, `status=complete`. So a green badge means the site
was parsed that morning; a red one means something changed.

The Scraping Browser route is kept for a manual check, but it is not what the
schedule runs: the scheduled route needs no credential, and a secret-backed
route can go stale (an endpoint that is no longer valid answers 401):

```bash
gh secret set CATAWIKI_CDP_ENDPOINT --repo <your-org>/catawiki-scraper
gh workflow run canary.yml --repo <your-org>/catawiki-scraper -f route=cdp
```

Use a `pid` reserved for CI: a Scraping Browser profile allows one live
connection, so a canary sharing a pid with a person's run gets
`profile_locked` and reports a failure that has nothing to do with the site.

---

## Guides

- [How to run the 2scraper scrapers: setup, proxies, exit codes and access modes](https://2captcha.com/h/how-to-run-2scraper-open-source-scrapers) — installation, `.env`, `.meta.json` for the whole family.

## Related scrapers

Same CLI conventions, same output contract (one row per item, JSON or CSV, a `.meta.json` sidecar and shared exit codes):

- [Etsy scraper](https://github.com/2scraper/etsy-scraper) — Search, category, shop and listing pages as JSON or CSV…
- [Farfetch scraper](https://github.com/2scraper/farfetch-scraper) — Luxury fashion listings and per-size product data…
- [Givenchy Beauty scraper](https://github.com/2scraper/givenchy-scraper) — Products, prices and shades across 11 storefront locales
- [Home Depot scraper](https://github.com/2scraper/homedepot-scraper) — Per-store prices, stock, brands, models and ratings…
- [Indiegogo scraper](https://github.com/2scraper/indiegogo-scraper) — Project listings, campaign pages and reward tiers
- [Kohl's scraper](https://github.com/2scraper/kohls-scraper) — Prices, price ranges, sale and clearance labels, ratings…

All scrapers: [github.com/2scraper](https://github.com/2scraper) · Need another site? [Open a request](https://github.com/2scraper/.github/issues/new?title=%5BScraper%20request%5D%20)

## Contributing, security, licence

See [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md) and
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md). Issues have templates, including one
for "the site changed". MIT licensed.

This scraper reads public listing pages, obeys a `--delay` between requests
(default on), and is meant for price monitoring and research. It does not log
in, does not bid, and does not touch anything behind an account.
