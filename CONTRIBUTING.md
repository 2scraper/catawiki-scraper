# Contributing

Bug reports, site-change reports and pull requests are all welcome. This file
covers the few things specific to a scraper, which are not the usual ones.

## Before you open anything

Run the offline suite. It needs no network, no browser and no API key, and takes
about a second:

```bash
pip install -r requirements.txt
python3 smoke_test.py
```

It prints its own check count, and lists any group it had to skip because an
engine library is absent.

**The suite must pass with no engine installed at all.** CI installs only
`beautifulsoup4` and `requests`, so any import of `playwright_scraper`,
`puppeteer_scraper` or `selenium_scraper` in a test has to sit inside
`try/except ImportError` with the skip recorded. This is easy to get wrong
locally, where you almost certainly have an engine installed and an unguarded
import passes.

If the suite fails on a clean clone, that is itself the bug — say so.

## Never commit a credential

`.env` is in `.gitignore`. Keep it there.

The scrapers mask `user:pass@` in their own log lines, but three things are **not**
masked: raw HTML dumps, the Scraper API's `x-debug` response header, and your
shell history. Before pasting any output into an issue or a PR, replace keys,
proxy passwords and full `ws://user:pass@host:9222` endpoints with `***`.

CI fails the build if something that looks like a credential is committed. That
check is a backstop, not a review — a leaked key has to be rotated whether or
not the check caught it.

## Reporting a site change

Catawiki changing its markup is the normal way this stops working, and it
has its own issue template. The detail that saves the most time is WHICH
anchor broke, because on this site there is no structured data on a listing
page to fall back on — measured zero `application/ld+json`, zero
`__NEXT_DATA__` and zero Apollo state across six captures — so the DOM is
not the primary path by preference, it is the only one.

1. **The grid container.** `[data-testid="divSRPContentProducts"]` on a
   search page, `[data-ssr="productsCategoryL2/L3SSR"]` on a category
   listing. If one of these moves the run reports 0 rows and exit 4, which
   is loud.
2. **The tile marker.** `[data-testid="imgLeg-c"]` on a search page (one per
   tile), `[data-testid="divProductWrapper"]` inside
   `a[data-testid="lnkProductContainer"]` on a category listing.
3. **The reading ORDER inside the tile** — badge, title, price, was-price,
   rating, sold, shop, location. The field reads rest on it, deliberately,
   because the classes around each field are build hashes:
   `<span class="+tnoqZhn89+NHUA43BpiJg==">` is the title today. If Catawiki
   reorders a tile, `title` and the prices are what break.
4. **`span.flip`**, the shop name and the shop's city in that order, exactly
   two per search tile.

The one place structured data does exist is a DETAIL page's
`window.__cache` Apollo blob, which is where `--mode product` reads the real
product id, the exact sold count and the review count.

A third thing can break without any path failing: the **join** between the
tiles and the structured data. When it breaks, the row count and the prices
stay healthy while `in_stock` and part of `brand` quietly empty out — so
every run logs its structured-price confirmation share per page and warns
below a floor set PER PAGE KIND (search 8%, category 70%, shop 80%; the
achievable share differs by a factor of eight between them). If you are
reporting a change, that percentage and the page kind are the numbers to
include.

`--dump-html PATH` writes the exact bytes the parser was given, on success as
well as failure, and a run that finds nothing writes a dump and a screenshot
next to the output on its own.

## Before this repository goes public

One item cannot be undone later, so it belongs on a checklist rather than in
someone's head. **A commit on top cannot reach what a published tag and a
merged PR's refs already hold** — those stay attached to the PR and cannot be
deleted from it. Afterwards, only a fresh repository removes anything.

```bash
python3 .github/ci_checks.py --history-check
```

That applies the same credential rules CI enforces to **every blob that has
ever existed**, not just the working tree. It is deliberately not part of
`--all` and not run by CI: it shells out to git once per object, and a dirty
history needs a decision, not a red check on every push.

Then the rest of the presentation, in the order that matters:

1. `python3 smoke_test.py` green, and the canary dispatched at least once —
   including its SKIP branch, which is what runs when the
   `CATAWIKI_CDP_ENDPOINT` secret is absent. The canary has **no schedule**: a
   Scraping Browser credential on this account does not survive a day, so it
   runs on demand with a fresh secret rather than painting a badge that has
   tested nothing. Put a schedule back the day a long-lived credential
   exists.
2. The repo description, homepage and topics set (see the family notes on
   what those should say).
3. Only then the row in the org profile README — and check it with an
   ANONYMOUS request rather than your own logged-in browser. A row pointing
   at a private repo is a 404 for every visitor, which costs more trust than
   the missing row.

## Pull requests

**Add a test for the behaviour you are changing.** `smoke_test.py` is a single
file of plain functions with inline HTML/JSON fixtures — no pytest, no
conftest, no fixtures directory. Copy the nearest existing check and edit it.

Five properties in this repo exist because they were once absent and cost real
time. Tests pin all five, so a PR that breaks one will fail rather than
silently regress:

- **`price` means three different things, and `bid_kind` says which.**
  `current` is a live high bid, `final` is the last bid on a closed lot — a
  hammer price only when `sold` is also true — and `starting` is a floor
  nobody has bid. One captured lot reached €1,300 with its reserve unmet and
  sold for nothing at all. The kind is resolved through the page's OWN
  translation store (`lot_status_current_bid` and friends), not through a
  table of 18 languages: two keys read "Current bid" in English and they are
  different strings in Chinese, so a table built from the English page would
  have matched nothing there.
- **A null price is a reserve lot, not a failure.** 57 of 57 blank prices
  across 13 captures carried `reserve_price_set: true`, spread through the
  page rather than clustered at its end. So there is no price-coverage
  threshold worth setting, and the check that matters is the INVARIANT: a
  null price always carries the reserve flag. The canary asserts exactly
  that.
- **The empty-price placeholder is a ZERO-WIDTH SPACE.**
  `.c-lot-card__price` is present on 24 of 24 cards while 2–3 hold nothing,
  so a truthiness check on the node reports 100% coverage and writes an
  invisible character into every row. Anything read out of a card goes
  through the zero-width strip first.
- **`favorite_count` comes from the rendered card, never from the payload.**
  The payload's own `favoriteCount` reads 0 on 288 of 288 lots across 12
  captures while the card shows the real figure on all 24 of each — present,
  authoritative-looking and uniformly wrong.
- **`bid_count` is a floor.** The site returns the last ten bids and states
  no total; two lots with very different activity both reported exactly ten.
  `bid_count_is_floor` is what says which kind of number it is.
- **A lot page's DOM is not read.** It renders 20–40 OTHER lots in a
  "similar lots" carousel using the same class a listing uses for its own
  price, so "the first euro amount on the page" is a neighbour's number.
  Every lot-mode column comes from the payload.
- **Pagination is capped at 100 pages by the site**, and a request past the
  cap returns page 100's own lots under HTTP 200 rather than failing. The cap
  is enforced on the URL this repo builds AND on any link the site offers,
  because without the second half a run reports COMPLETE holding 2,400 of
  11,681 lots.
- **A search that matches nothing returns 24 suggested lots** reported as
  `total: 24`. That state is read off the payload's own
  `extended_search_result` flag and is NOT parsed: two dozen plausible rows
  for a query that matched nothing is worse than none.
- **A block here is a HEADLESS browser, not a bad address.** HTTP 403 and a
  394-byte "Access Denied" from four residential exits and one datacentre
  one, against HTTP 200 and the full catalogue from the same addresses with a
  real window. So `--headful` is the default, `RETRY_ON_BLOCKED` is False,
  and the block message says so rather than sending someone to buy a proxy.
  Block detection is INVERTED as well: a served page is recognised by the
  site's own asset host, because Chromium's own network-error page carries
  the site's hostname in its title and would pass any title check.
- **A run that finds nothing writes nothing.** It must not replace a good output
  file with `[]`. `--allow-empty` is the opt-out.
- **Exit codes are a contract**, not decoration: `0` ok, `1` crash, `2` bad
  usage, `3` blocked, `4` zero rows, `5` remote API error, `6` partial. A
  pipeline branches on these.
- **An EMPTY page is never retried and never counted as blocked.** One page
  past the end of a listing has no lots, and a no-results search has none of
  its own; both are correct answers to the question that was asked.
  `page_flow.STATE_POLICY` holds that for all three engines so they cannot
  disagree about it.
- **A challenge marker is only consulted for a state already counted as
  blocked**, and a marker that matches every page of the site is not a
  marker at all. This has bitten twice in this family, and the second time
  is why `akamai` is NOT in this repo's marker set: the string lives in the
  response header (`server: AkamaiGHost`), not in the body of a good page or
  a bad one — 0 occurrences in every capture. There is deliberately no
  extension-stripping guard either: the Scraping Browser's auto-solve
  extension does inject a recaptcha and a turnstile hunter into every page it
  loads, but none of this repo's markers matches them even without
  stripping, so the guard would be code that looks load-bearing and never
  runs. Broaden the set and add the guard together.
- **A sku already written by an earlier page of the same run is dropped, not
  duplicated.** Unlike its sibling repos this DOES fire on healthy runs
  here: page 1 and page 2 of one category listing shared exactly 3 products,
  all three from the "cheaper products" carousel that appears on every page.
  So a small non-zero drop count is expected and a large one is not. See
  `dedupe_by_key` in `output_writer.py`.

There is also a naming check: certain phrases are banned repo-wide and the suite
fails naming them. If it trips, read the message — the phrase is wrong for a
reason, not merely unfashionable.

### Style

- **Match the file you are editing.** No formatter is enforced.
- **Comments explain *why*.** What the code does is visible; why it does it that
  way, especially where the obvious version is wrong, is not.
- **A timeout on every remote call.** Every browser library used here has needed
  an explicit timeout its own API does not provide, and each has needed its own
  route out of the runtime — reporting a timeout is not the same as exiting on
  one. If you add a call to a remote browser or API, bound it.
- **Fail loudly.** A function that returns an empty list on error, or logs
  success without checking that the thing it wanted actually happened, is the
  single most common bug class in this codebase's history. A selector that
  matches the *wrong* element is worse than one that matches nothing, because
  the second one tells you.

### If your change needs a live run

Most do not — the suite covers the parser, the writers, the captcha classifier
and the CLI contract against inline fixtures. If yours genuinely needs
catawiki.com, say in the PR what you ran, which URL and page kind, from
which exit, and what you got — including the price and image coverage
percentages the run prints, and the scroll trace from the sidecar. Note that
a run from a datacentre address gets NO RESPONSE AT ALL, so "it returned
nothing" from a VPS is not a finding. Product counts differ by category, by
URL and by how far the scroll got, so a bare "worked for me" is not
reproducible.

**Run more than the primary engine.** "Mirror them exactly" is a design rule,
not a verification: the first live run of the pyppeteer engine crashed on its
FIRST fetch on a signature mismatch that four separate offline checks and 400
green assertions had not caught.

Do not add anything that submits the registration form. This project
deliberately never does, and a captcha token proved valid by creating a real
account is not a result worth having.

## Scope

This repo scrapes **public pages** on Catawiki: category listings, search
listings and product pages, exactly as an anonymous visitor is served them.
Out of scope: anything behind a login, anything that submits a form, and
anything that defeats a protection rather than passing it the way an ordinary
browser does.

## Licence

MIT. By opening a pull request you agree your contribution ships under it.
