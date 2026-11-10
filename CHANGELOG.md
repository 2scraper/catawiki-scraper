# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and versions follow [Semantic Versioning](https://semver.org) as closely as a
CLI toolkit can. Read "patch" as **fixes**, not as "no flag or default ever
moves": a default that was measured wrong is a fix, and it will be called out
at the top of the release notes rather than left to be discovered from a bill
or an empty column.

## [Unreleased]

### Fixed

Found by a third-party audit on 2026-11-10 and reproduced against `main` the same day; each fix below was re-run live afterwards.

- **A run asking for more than 100 pages no longer loses everything.** It used to plan page 101, get no address for it, crash and write nothing. It now stops at the site's cap, keeps what it gathered and records `pages_capped_by_site` and `pages_requested_by_user` in the sidecar.
- **A run that starts deep in a listing no longer fetches the wrong pages.** `--pages 3` from `?page=99` fetched 99, 100 and then page **3**, and reported success. Every address, the cap and the `page` column now use the site's own page number. A start page past 100 is a usage error (exit 2).
- **"Nothing found" and "Page Not Found" are no longer read as a block.** A search that matched nothing stops after page 1, and a withdrawn lot exits 4 instead of 3 with advice to buy a Scraping Browser. A page the site built but that is too small to name its asset host twice is now recognised by its own `__NEXT_DATA__` envelope; Akamai's refusal and Chromium's error page still read as blocked.
- **An empty page inside the listing's stated size no longer ends the run as complete.** It is fetched once more; if it is empty again the run stops with `stop_reason: listing_ended_early`, `status: partial` and exit 6.
- **`--mode lot` fills `image_url`.** The lot payload names its sizes `large`/`medium`/`thumbnail`; the code read the listing card's `url`/`originalUrl`.
- **`--proxy-block-retries` now rotates away from a dead exit** (refused connection, rejected tunnel, bad credentials) in all three engines. It still does not retry a site refusal, which is decided by the client, not the address. "Gave up after N attempts" now counts the attempts made.
- **A Scraping Browser profile still held by the previous run is retried** (four tries, ten seconds apart, with the reason in the log) instead of failing every second back-to-back run with a bare HTTP 500. pyppeteer now ends that case as one masked line and exit 5, as Playwright does, instead of a 30-second hang, two tracebacks and exit 1.
- **The Docker image builds and fetches.** Playwright 1.65's driver needs `libatomic1`, which `python:3.12-slim` lacks, so the build died before installing a browser. The image now also carries `xvfb` and `tini` and runs the scraper under `xvfb-run`: the site refuses a headless browser and a headful one needs a display. No `docker run --init` is needed.
- **`diff_runs.py` refuses two runs of different listings** (a category against a search) using the `start_url` in each sidecar, and no longer advises re-running "the incomplete side" when both are complete. The README example now uses `--old`/`--new`.
- **Search rows carry their query in `category`**, as the help says.
- **`--mode auctions` refuses a URL that is not the auctions index.**
- **An unwritable `--out` folder and a missing `--proxy-file` are usage errors (exit 2)** raised before any browser starts, not a traceback after a whole run.
- **An empty search that ran to its end writes `status: complete`** (with `products: 0`) when `--allow-empty` is given; it used to write `failed` beside `stop_reason: completed`.
- **Selenium with a credentialed proxy is refused before it starts (exit 2)**; it used to run unauthenticated and end as "blocked" (exit 3).
- **The Fingerprint API's own reason reaches the user** for any non-success status, and a failure ends as one line and exit 5 instead of a traceback.
- **The Scraper API client reads a lot page and an auctions index** (`--mode lot`/`auctions`), accepts `--twocaptcha-key` and `--cdp-endpoint` like the browser engines, and its `--help` no longer says it needs `--cdp-url` to reach the site or returns about 5 products.
- `--min-score` accepts only 0.3, 0.7 or 0.9; the default `--locale` is `nl-NL` (it was `id-ID`); the `--solve-captcha` and `--proxy-block-retries` help texts now agree with the README; the README says `--concurrency` with a Scraping Browser is ignored with a warning, as it is.
- `sample_output.json`/`.csv` were retaken on 2026-11-10 with the current version: a filled `time_left_text` on the listing rows and an `image_url` on the lot rows.

### Documentation

- README: states per route (category listing, auctions index, one auction, one lot) what access each needs, rechecked 2026-11-05 from a datacentre address. The 403 shown for a datacentre address is a refusal of the client (`curl`, headless), not of the address: a headful browser from that address is served with no key or proxy.

### Changed

- CI only: the scheduled canary now starts at 06:27 UTC (Mondays) instead of 06:23, so canaries that share one Scraping Browser profile no longer start at the same time and lock each other out.
- CI only: the canary now runs weekly (Mondays 06:23 UTC) instead of daily, and on a manual dispatch.

## [0.2.0] — 2026-10-15

> **Behaviour changes.**
> - CSV cells that begin with `=`, `+`, `-`, `@`, a tab, CR or LF now get a
>   leading apostrophe in the CSV output only (JSON is unchanged), and the
>   sidecar records how many as `csv_cells_escaped`.
> - `--pages 0`, `--concurrency 0`, a negative `--delay`, `--retry-delay` or
>   `--proxy-block-retries` (and a negative `--retries`/`--retry-delay` on
>   `scraper_api_client.py`) are now usage errors (exit 2).
> - Output files are written atomically. A new file gets the mode `open()`
>   would have given it under your umask; an existing file keeps its mode.

### Fixed

- **One page could buy two captcha solves against a limit of one.** Each
  engine runs its captcha handler before and after classifying a page, and
  only the second call was counted, so the first repeated on every block
  retry (2 solver calls per page at the default settings and 4 with block
  retries enabled, reproduced with the solver stubbed). Both call sites now
  share one per-page `page_flow.SolveBudget`, checked immediately before a
  token is bought and not reset by a retry or a rotation. A page with no
  captcha spends nothing from it. No challenge has been observed on
  catawiki.com; this bounds the paid path for the day one appears.
- **A failed write destroyed the previous good output.** `write_json`,
  `write_csv` and the `.meta.json` sidecar truncated the file before writing,
  so an exception half-way left invalid JSON where a good run had been. They
  now write to a temporary file in the same directory, fsync it and rename it
  over the target.
- **CSV output could carry spreadsheet formulas** written by whoever listed a
  lot. See the behaviour note above.
- **Selenium and pyppeteer crashed with no `--proxy` once block retries were
  enabled** (`AttributeError` on `pool.current`). Their block and load-failure
  paths now rotate only when there is a pool and otherwise re-fetch through
  the same path, as the Playwright engine does. Latent at the shipped
  settings, where block retries are off.
- **The credential scan read nothing when the clone lived under a directory
  named `venv`, `.venv`, `.git` or `__pycache__`**, and still printed "ok".
  Exclusions are now judged relative to the repo root, and a scan that read
  0 files fails.
- **A foreign `--url` produced `error: None www.ebay.com is not a Catawiki
  host`.** The message no longer starts with "None", and the unreachable
  fallback no longer describes a site with one language and no locale
  prefix.
- `.gitignore` covers `--dump-html X --pages N` dumps of any suffix
  (`X.pageN`), a root-level `.html`, and scratch directories such as `live/`.
- The README now describes exit 5 as "the pages were never obtained", which
  is what the code returns it for, not only a remote API error. The canary
  and the docs no longer claim a Scraping Browser endpoint lasts less than a
  day: that was never measured, and a 401 is the signal that one is no
  longer valid. The canary's exit-4 message names Catawiki's own payload and
  lot card instead of another site's selectors.
- Comments no longer cite numbered sections of private notes; each rule is
  stated in place. A suite check keeps such citations out of every tracked
  file. `tests.yml` runs the sample check from `ci_checks.py` instead of a
  narrower inline copy.

## [0.1.1] — 2026-10-13

> **Behaviour change:** `--retries 0` is now refused as a usage error (exit 2).
> It used to run no navigation at all and report a block (exit 3). Use
> `--retries 1` for a single attempt.

### Fixed

- **`--retries 0` skipped navigation and reported a block.** The load loop
  is `range(1, retries + 1)`, so 0 ran it never, `goto` was not called, and
  the engine judged `about:blank`: a 39-byte empty DOM, exit 3, for a site that
  was never asked. Reproduced 2026-10-09 (Playwright, `--retries 0`: exit 3 in
  ~50 ms in both modes; the same command with `--retries 1` fetched 72 lots,
  `complete`, exit 0). `--retries` now takes an integer of at least 1 in all
  three engines and refuses 0 as a usage error (exit 2).
- **The not-served message contradicted the README.** It said a headless
  browser was the cause, then recommended a residential exit, and said it for a
  headful run too. One shared `page_flow.not_served_message` now states what
  was observed (bytes, whether the site's own assets appear) and only then the
  likely cause: a document under 100 bytes with no site assets is called an
  empty document and a navigation that did not complete; headless is blamed
  only when the run was headless.
- **`--help` and usage errors described a different site** (its modes, URL
  kinds, currency and page structure). They now describe the real URL kinds
  and the `listing|lot|auctions` modes, from one set of strings in `page_flow`.
  The `--mode lot` usage errors no longer point at a mode that does not exist, and the
  discovery-hub warning, which could never fire, is removed. A suite check walks
  every string in the three engines for the old wording.

- **`diff_runs.py` refused every `--mode lot` run.** The list of diffable modes
  named a mode this repo does not have (`product`) instead of `lot`, so a lot
  run's sidecar was rejected. `listing` and `lot` are diffable; `auctions` rows
  are a different class and still are not.

### Changed

- The listing sidecar's `extra` no longer carries `scroll` and
  `pages_still_growing`. No engine ever filled them (this site has no scroll
  loop), so they were always empty; `result_header` is unchanged. The
  `--mode lot` branch that read never-set seller facts is removed too.
- Comments, docstrings and `CONTRIBUTING.md` no longer describe another
  site's markup, ids, measurements or modes; they describe this site's
  payload and card, and the real URL kinds. No column or flag changed.
- `SECURITY.md` no longer says a raw HTML dump contains session cookies; it
  holds the page markup, not the cookie jar.

- **The Scraper API path (`scraper_api_client.py`) failed whenever a wait flag
  was given, and never saw the target's status.** Measured 2026-09-23 against
  the live `/tasks/sync` endpoint: `waitFor` sent as a JSON-encoded string
  (what this client sent) is answered HTTP 422 "params.waitFor must be an
  object" and is still billed ($0.0005); sent as an object it is answered
  HTTP 200. It is now an object. And the response's `status` is the API's own
  verdict ("success"), while the target site's HTTP code is `http_code` — the
  client handed `status` onward, so a target 403/503 never reached the page
  classifier. It now reads `http_code`, falling back to `status` only if that
  is an integer. After the fix, one live call (`--wait-text Watches` on the README's watches category) answered HTTP 200, upstream 200, 24 rows.
- **The canary runs daily again, with no secret.** It was dispatch-only,
  gated on a Scraping Browser endpoint that does not survive a day, and the
  suite pinned "no cron" — so it ran once, on 2026-09-11, and never again
  while every sibling's canary ran daily. The xvfb route that the workflow
  called "plausible but unverified" has now been run on a stock GitHub
  runner: headful Chromium under `xvfb-run`, no credential, 72 lots over 3
  pages, every assertion green. That route is now the scheduled one, with no
  skip path; `route=cdp` on a manual dispatch still uses the secret.

- **Fifteen lines of unreachable code removed from `playwright_scraper.py`.**
  A function's `def` line had been lost at some point before this repo's
  first commit, leaving its docstring and its `try: return page.content()`
  body indented into the end of `_mask_credentials`, where the control flow
  can never arrive. Nothing called it — `_content_when_settled` below it
  does the job — so no behaviour changes. The same fifteen lines, byte for
  byte, were in six repos of this family.

- **Donor leftovers removed.** The bug-report template's example wrote
  `--out grills` and expected Farfetch's "96 products, as the README says a
  category page yields"; it now uses `--out watches` and this README's 24
  lots per category page. `captcha_solver.py`'s docstring pointed at a "No
  DataDome solver" section that does not exist in this repo (Catawiki is
  fronted by Akamai, and no DataDome path was ever here). A comment in
  `output_writer.py` used `shop_rating`, a column this repo does not have,
  as its example sidecar field; it now names `scroll`, which the engines
  really write. A smoke-test comment described "MediaMarkt's pages" without
  saying it was a sibling site; it now does. CONTRIBUTING's "the
  registration form" is generalised.

### Added

- **A check for a statement the control flow can never reach.** The
  undefined-name walk beside it cannot see this class by design: it pools
  every binding in a file rather than tracking scopes, so a name used inside
  dead code passes as long as anything else in the module binds it. The new
  one is narrow — a statement after a `return`/`raise`/`break`/`continue` in
  the SAME block — and measured across the eighteen repos of this family it
  found six real problems and zero false positives. Verified by control:
  appending `return 1` followed by a statement turns the suite red.

- `SECURITY.md` said this project has no releases or version tags; it has
  both. "Supported versions" now names the latest release and `main`.


## [0.1.0] — 2026-09-11

First release. Rewritten from the original generated scripts as a member of
the [2scraper](https://github.com/2scraper) family: one row schema, one set of
exit codes, one `page_flow` policy shared by three engines, and the family's
offline suite.

### Added

- **Four back ends behind one row schema** — `playwright_scraper.py`
  (primary), `puppeteer_scraper.py`, `selenium_scraper.py` and
  `scraper_api_client.py`. Verified live on the same URL: 48 rows each,
  identical `sku` sets, 44 identical columns in the same order.
- **Three modes.** `--mode listing` reads a category, a search or a whole
  auction; `--mode lot` reads one lot page in full (estimate, seller and
  their feedback score, specifications, bid-history depth, both absolute
  bidding times); `--mode auctions` reads the auctions index as a work list
  and writes its own `Auction` row.
- **Auction-shaped columns**: `bid_kind`, `bid_count` with
  `bid_count_is_floor`, `next_min_bid`, `reserve_price_set`,
  `reserve_price_met`, `sold`, `buy_now`, `estimate_min`/`estimate_max`,
  `bidding_start_at`/`bidding_end_at`, `auction_close_at`, `auction_status`,
  the seller's country and score, and `favorite_count`.
- **`diff_runs.py` understands auctions.** A lot whose `bid_kind` moves
  `current` → `final` lands in a `lifecycle` bucket rather than in `changed`,
  and `--fail-on-change` ignores it: the auction ran, the site did not
  reprice. The reverse direction stays a real change, because a closed lot
  does not reopen.
- **451 offline checks**, every fixture cut from a real capture and verified
  to parse identically to the untrimmed original, with per-bidder tokens and
  a named curator replaced by placeholders and guarded by patterns.
- A daily canary against a real three-page category URL that asserts the
  reserve invariant rather than a price-coverage percentage, and skips with a
  notice when its secret is absent.

### Notable behaviour, decided by measurement

- **`--headful` is the default**, against headless in every sibling repo.
  Akamai refuses a headless browser here with HTTP 403 and a 394-byte
  "Access Denied" from four residential exits and one datacentre address,
  while a real window is served HTTP 200 and the full catalogue from the very
  same addresses. `--headless` is kept, and will report exit 3 today.
- **No scroll loop anywhere.** Three scrolls to the document's own bottom
  added zero cards and left the page height unchanged on all three page
  kinds.
- **`page_flow.RETRY_ON_BLOCKED` is False** and the block budgets are 0:
  rotating an exit cannot clear a block whose cause is the client.
- **A no-results search is not parsed.** It answers HTTP 200 with the words
  "No results" and 24 suggested lots reported as `total: 24`; the state is
  read off the payload's own `extended_search_result` flag.
- **Pagination is capped at 100 pages**, on the URL this repo builds and on
  any link the site offers. Past the cap the site returns page 100's own lots
  under HTTP 200, so an uncapped planner reports a complete run holding 2,400
  of 11,681 lots.
- **`env_config.apply()` now honours an explicitly empty flag.** It used to
  fill any falsy destination, so `--cdp-endpoint ''` was overwritten by the
  value in `.env` and the run went to the remote browser it had just been
  told not to use — there was no way to switch a `.env` setting off from the
  command line at all.

[Unreleased]: https://github.com/2scraper/catawiki-scraper/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/2scraper/catawiki-scraper/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/2scraper/catawiki-scraper/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/2scraper/catawiki-scraper/releases/tag/v0.1.0
