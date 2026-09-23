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

[Unreleased]: https://github.com/2scraper/catawiki-scraper/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/2scraper/catawiki-scraper/releases/tag/v0.1.0
