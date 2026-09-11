# Troubleshooting

Symptoms first, in the order people actually hit them. Every number here was
measured; where a thing is a property of the site rather than a bug, it says
so, because half of what looks broken on an auction site is the site working.

---

## Exit 3, a 308-byte or 394-byte dump, `Access Denied`

**You are running headless.** This is the first thing to check and it is not
a proxy problem.

Measured 2026-09-10 on the same URL: HTTP 403 and a 394-byte Akamai "Access
Denied" from four residential exits and one datacentre address, against HTTP
200 and the full catalogue from those very same addresses with a real browser
window. The exit address changes nothing; the headless flag changes
everything.

```bash
# The default. Do not pass --headless.
python3 playwright_scraper.py --url "https://www.catawiki.com/en/c/333-watches"

# Or use the remote browser, which is also measured working:
python3 playwright_scraper.py --cdp-endpoint "ws://…@cb.2captcha.com:9222" --url …
```

A proxy will not fix it, and `page_flow.RETRY_ON_BLOCKED` is `False` for that
reason — spending the retry budget on a different exit spends it on a change
that cannot help. The block message says the same thing rather than sending
you shopping.

---

## `price` is null on some rows

**That is a reserve lot, and it is correct.** Every blank price is a lot whose
reserve has not been met: 57 of 57 blanks across 13 captures carried
`reserve_price_set: true`, and they are spread through the page rather than
clustered at its end, which is what tells reserve behaviour from a hydration
failure.

Coverage was 87–100% on 24-lot category and search pages and 68% on a 130-lot
auction with a heavier reserve mix. So there is no percentage worth alarming
on. The check that means something is the invariant:

```bash
python3 - <<'EOF'
import json
rows = json.load(open("out.json"))
bad = [r["sku"] for r in rows if r["price"] is None and not r["reserve_price_set"]]
print("rows with no price and no reserve flag:", bad or "none — healthy")
EOF
```

If that list is non-empty, the price node has moved and the rows are quietly
empty. That is a real bug; open an issue with the URL and a `--dump-html`
snapshot.

---

## Every price is null, and `price_source` says `next_data` everywhere

**Nothing hydrated.** The rows come from the site's SSR payload, which is in
the first response and needs no JavaScript; the money comes from the rendered
card. A healthy listing row says `next_data+dom`.

Causes, in order of likelihood: the readiness wait timed out on a slow exit
(the prices fill in 1.9–2.4 s after commit on a local run, all at once); you
used `scraper_api_client.py`, which is browserless by design and can never
get the money; or the card's price class changed, which is an issue worth
filing.

---

## `bid_kind` is null but `price` is not

The status label is what `bid_kind` is resolved through, and it is resolved
through the page's OWN translation store rather than a table written here. If
this happens on a locale that used to work, the site has renamed its
`lot_status_*` keys. File it with the locale in the title.

---

## `--mode lot` returns a price for a closed lot. Is that a sale?

Only if `sold` is true. `bid_kind: final` means "the last bid", and a lot can
close with bids and still not sell: one captured lot reached €1,300 with
`reserve_price_met: false`. Treating `final` as a hammer price without reading
`sold` puts money in a price history that nobody ever paid.

---

## `bid_count` is 10 on every lot

The site returns the **last ten** bids and states no total. Two lots with very
different activity both reported exactly ten. `bid_count_is_floor` is true in
that case, and it is what stops one column meaning two things.

---

## The run stops at page 100 and reports `complete`

**The site caps pagination at 100 pages** and does not fail past it: `?page=99999`
returned HTTP 200 with `currentPage: 100` and page 100's own 24 lots. This
scraper caps its planning at 100 and reports how many pages the catalogue has
beyond that, so a run of an 11,681-lot category legitimately holds 2,400 lots.

To go deeper, narrow the listing — the site's own filters are in the URL — or
walk the auctions index and scrape each auction, which has no cap and carries
every one of its lots in one response.

---

## A search returns 24 rows for a query that matched nothing

It should return **zero**, and it does — check the sidecar: `status` will not
be `complete` with rows, and the run exits 4.

What happens on the site is that a no-results search answers HTTP 200, prints
"No results", and backfills the grid with 24 suggested lots reported as
`total: 24`. That state is detected from the payload's own
`extended_search_result` flag and deliberately not parsed. If you see 24 rows
from such a query, the flag has changed name and that is a bug worth filing
immediately, because the rows look completely plausible.

---

## Exit 4 and zero rows — broken, or empty?

Check `<out>.meta.json`. `status: "empty"` with `stop_reason` naming the empty
state is a correct answer to the question asked; `status: "blocked"` is not.

Legitimately empty: one page past the end of a listing, a no-results search,
an auction with every lot withdrawn. Not legitimate: a category URL that a
browser shows lots on. For the second case, run with `--dump-html` and look
at whether the payload is in the file at all.

---

## `500 Internal Server Error` on connecting, exit 5, `profile_locked`

A Scraping Browser profile allows **one live connection**. Another run still
holds that `pid` — wait for it, or use a different `pid`. `--concurrency`
above 1 is refused with `--cdp-endpoint` for the same reason.

---

## `Browser does not support socks5 proxy authentication`

Chromium cannot authenticate a SOCKS5 proxy, and Playwright refuses at launch
rather than connecting without the credentials. Selenium cannot authenticate
**any** proxy.

The way round both is 2Captcha's IP-whitelist mode, which hands out
credential-free connections:

```bash
curl "https://api.2captcha.com/proxy/generate_white_list_connections\
?key=$TWOCAPTCHA_KEY&ip=YOUR.IP&protocol=socks5&connection_count=3&country=nl"
```

One `host:port` per exit, no user, no password — works in all three engines
and drops straight into `--proxy-file`. Measured 2026-09-11: three ports,
three different residential Dutch addresses, active immediately. The
`protocol=http` connections generated the same way never answered.

---

## `--proxy` is set and every navigation times out

A residential exit is slower than your own line, and one timeout followed by
a successful retry is normal — that happened on the first page of a measured
run. If **every** attempt times out, check the proxy directly before blaming
the scraper:

```bash
curl -sS --max-time 30 --proxy "socks5h://host:port" http://ip-api.com/json/
```

Note that a transport-level probe can lie: in a sandboxed environment a TCP
connect to any port may appear to succeed instantly. Trust an HTTP response,
not an open port.

---

## The run says "shell … has not filled in" and then works

That is the intended path. The server sends the card **shells** — 24
containers with 24 empty price nodes — and fills the money in at hydration.
The readiness wait is for a price node with something in it, because a card
count is satisfied instantly while every price on the page is still blank.

A shell is waited out, not refetched: refetching one just buys another shell.

---

## `rating`, `review_count`, `original_price` and `discount_pct` are null on every row

Catawiki publishes none of them on a lot. There is no per-lot rating (the
seller's feedback is `seller_score` and `seller_feedback_count`, on a
`--mode lot` row) and no was-price. The four columns are kept so one schema
works across this scraper family.

---

## `brand` is null on listing rows and populated in `--mode lot`

Deliberate. A listing title reads "Cartier - Tank Must de Cartier PM - No
reserve price - …" and splitting on the dash would be a guess presented as a
fact. A lot page states the brand in its own specification field — read by
the site's numeric `specificationId`, not by the field's NAME, because the
names are translated (`Brand` on /en, `Merk` on /nl).

---

## Two runs differ on almost every row

On an auction site that is usually time passing, not the site changing.
`diff_runs.py` separates it: a lot whose `bid_kind` moved `current` → `final`
goes into the `lifecycle` bucket, and `--fail-on-change` ignores that bucket.
What is left in `changed` is worth reading.

---

## `--concurrency 4` is refused

Three reasons, and the message says which: the URL is a lot page or the
auctions index (no page 2 exists), or `--cdp-endpoint` is set (one live
connection per profile).

---

## `pytest` or `python3 smoke_test.py` fails after an edit

The suite pins values, not coverage, so a failure names the value. Two
classes worth knowing:

* **A fixture check fails after you regenerate a fixture.** The fixtures are
  cut from real captures and verified to parse identically to the untrimmed
  original. Regenerate with `make_fixtures.py` in the working directory
  above the repo rather than editing the literals by hand — an early hand
  attempt kept only the first locale bundle of the translation store and
  produced a Japanese fixture whose `bid_kind` was null on every row.
* **A signature or flag-parity check fails.** Those exist because two of
  three engines once called a shared function with the wrong argument shape
  and both crashed on their first live fetch, invisible to import, `--help`
  and the whole offline suite.

---

## pyppeteer prints a wall of asyncio tracebacks after a successful run

You will see lines beginning `Exception ignored in:` from
`asyncio.base_subprocess` or `asyncio.proactor_events`, after the rows have
already been written.

**The run succeeded.** This is teardown noise from an unmaintained library:
pyppeteer's transports are finalised by the garbage collector after the event
loop has closed and after the exit code has been decided, so no loop handler
can reach it. Catching it would mean installing a global unraisable hook that
swallows real bugs too, which is a worse trade. Check the exit code and the
output file; both are correct.

The offline suite pins this as a known limitation rather than half-guarding
it, so a future change to it becomes a decision instead of a surprise.

---

## Selenium cannot reach the site over `--cdp-endpoint`

It cannot, and it says so rather than silently dropping the credentials:
chromedriver's `debuggerAddress` takes a bare `host:port` with nowhere to put
a password, while Playwright and pyppeteer authenticate on the WebSocket
upgrade. Use one of those two for a credentialled endpoint.

---

## Where to look next

* `python3 env_config.py` — what configuration was picked up, without
  printing secrets.
* `--dump-html` — the exact bytes the parser was given, written on success as
  well as failure. A run can return the right count with a column silently
  empty, and then only the bytes tell a parsing bug from a too-early
  snapshot.
* `<out>.meta.json` — `status`, `stop_reason`, and which pages failed by
  number.
* `python3 .github/ci_checks.py --all` — the same checks CI runs.
