#!/usr/bin/env python3
"""catawiki-scraper — Playwright edition (primary engine)

Scrapes Catawiki auction lots: category listings, search results, whole
auctions and single lot pages.

    --mode listing  (default)  a category (/{loc}/c/{id}-{slug}), a search
                               (/{loc}/s?q=...) or a whole auction
                               (/{loc}/a/{id}-{slug}) — 24 lots per page on
                               the first two, every lot at once on the third
    --mode lot                 one /{loc}/l/{id}-{slug} page: the estimate,
                               the seller and their feedback score, the
                               specifications, the bid history depth and BOTH
                               absolute bidding times, none of which a
                               listing states
    --mode auctions            the auctions index as a work list: one row per
                               auction, to feed back in as --url

There is deliberately no `--country` flag. The locale is a path segment on a
single host, so a flag could only disagree with the URL it was given.

WHAT IS DIFFERENT ABOUT THIS SITE
---------------------------------
* **It refuses a HEADLESS browser, and no proxy fixes that.** Measured
  2026-09-10: HTTP 403 and a 394-byte Akamai "Access Denied" from four
  residential exits and one datacentre address, against HTTP 200 and the full
  catalogue from those very same addresses with a real window. So `--headful`
  is the DEFAULT here, against headless in every sibling repo, and
  `page_flow.RETRY_ON_BLOCKED` is False: rotating an exit spends the budget
  on a change that cannot help. `--cdp-endpoint` is the other measured-
  working path.

* **The rows come from the site's own SSR payload, not from JSON-LD and not
  from the DOM.** `__NEXT_DATA__` is in the first response and needs no
  JavaScript, so a readiness wait that times out costs the prices, not the
  rows. JSON-LD is one poor `ItemList` on a category page and absent
  altogether on a search page and a lot page.

* **The money is the one thing the payload does NOT carry.** Bids arrive
  client-side, so the amount, the bid state and the favourite count come from
  the hydrated card. The server sends 24 card shells with 24 EMPTY price
  nodes, which is why readiness waits for a price node with something in it
  rather than for a card count — the count is satisfied instantly while every
  price on the page is still blank.

* **There is no scroll, and that is measured.** Three scrolls to the
  document's own bottom added zero cards and left the page height unchanged
  on all three page kinds. A sibling repo cannot see one product without its
  scroll loop, which is exactly why this was checked here rather than ported.

* **Pagination is arithmetic, and the site caps it at 100 pages.** Page 1's
  payload states `total` and `lotsPerPage`, so every page's address is
  knowable at once. But `?page=99999` returns HTTP 200 with `currentPage:
  100` and page 100's own lots — it clamps rather than failing — so a planner
  that ignores the cap re-fetches page 100 forever and reports a COMPLETE run
  holding 2,400 of 11,681 lots.

* **A search that matches nothing returns 24 suggested lots** and reports
  them as `total: 24`. That state is read off the payload's own
  `extended_search_result` flag and is not parsed.

Examples
--------
    python3 playwright_scraper.py \\
        --url "https://www.catawiki.com/en/c/333-watches" --pages 3

    python3 playwright_scraper.py \\
        --url "https://www.catawiki.com/en/s?q=rolex" --pages 2

    python3 playwright_scraper.py \\
        --url "https://www.catawiki.com/en/a/1243988-figures-figurines-auction"

    python3 playwright_scraper.py --mode lot \\
        --url "https://www.catawiki.com/en/l/106583855-omega-de-ville-prestige"

The site does not geo-redirect: the locale is whatever the path says, and the
currency is euro on all 18 of them.
"""

import argparse
import logging
import queue
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import List, Optional
from urllib.parse import urlparse, urljoin, parse_qsl

from playwright.sync_api import (sync_playwright, Error as PWError,
                                 TimeoutError as PWTimeout)

from captcha_solver import (detect_recaptcha_v3, detect_recaptcha_in_page,
                            reconcile_detections, solve_recaptcha,
                            CaptchaUnsolvable, INJECT_TOKEN_JS,
                            RECAPTCHA_DISCOVERY_JS)
from product_parser import (parse_products, parse_lot_page,
                            pages_beyond_cap as parser_pages_beyond_cap,
                            PAGE_CAP as parser_page_cap,
                            auction_metadata, auction_links, auction_rows,
                            SELECTORS,
                            detect_bot_challenge, detect_block_marker,
                            page_url, paginates_by_url, listing_kind,
                            site_host, is_supported_host, total_results,
                            total_pages, search_header, unsupported_reason,
                            CURRENCY, served_by_catawiki)
from output_writer import dedupe_by_key, finish_run, EXIT_API_ERROR
import page_flow
from page_flow import MIN_CARD_MATCHES
from proxy_pool import (from_args as proxy_pool_from_args, to_playwright, mask,
                        ROTATE_MODES, ProxyError, ProxyPool)
import env_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("playwright_scraper")


def _chrome_ua(chromium_version: str) -> str:
    """Build a desktop-Chrome UA naming the browser's OWN real version.

    Not a hardcoded version number: that drifts the moment a newer Chromium
    ships, and a UA claiming an older Chrome than what the JS engine, WebGL
    strings and TLS ClientHello all actually report is itself a mismatch a
    fingerprinter can key on.
    """
    return (f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{chromium_version} Safari/537.36")


@dataclass
class PageOutcome:
    """What one page produced.

    Collected per page and merged afterwards rather than folded into shared
    state as the loop goes. Two reasons, and the second is the point:
    dedupe that mutates a running set inside the loop makes the OUTPUT depend
    on the order pages happen to arrive in — fine while that order is fixed,
    wrong the moment pages are fetched concurrently, because which page
    "claims" a duplicate sku (and so which `scraped_at` the row carries)
    would vary between runs of the same command. Merging afterwards in page
    order is deterministic regardless of arrival order.
    """
    page_num: int
    url: str
    final_url: Optional[str] = None
    products: List = field(default_factory=list)
    blocked_by: Optional[str] = None
    load_failed: bool = False
    # The page_flow state this page came back as ("content", "blocked",
    # "challenge", "empty", "unknown"). Carried so the caller can tell an
    # EMPTY page — a /p/<slug> hub, a no-match query, or one page past the
    # end of a listing — from a page that failed. Both produce zero rows and
    # they mean opposite things.
    state: Optional[str] = None
    # What the listing itself says its catalogue size is. Populated here,
    # unlike in a sibling repo: the SSR payload states `total` and
    # `lotsPerPage` on page 1 (11,681 and 24 on the category measured), which
    # is what makes the page count arithmetic rather than a link to follow.
    total_available: Optional[int] = None
    # The raw result-count header, verbatim, for the sidecar. Worth recording
    # and worth nothing as a count — after one scroll it claimed 61-180 while
    # the DOM held 95 tiles.
    header: Optional[str] = None
    # What the lazy-load scroll did: rounds spent, cards reached, and whether
    # it SETTLED. That last flag is the important one — a page whose grid was
    # still growing when the round budget ran out is partial, and a run that
    # reported it as complete would read as a shrinking catalogue.
    scroll: Optional[dict] = None
    # In --mode product, the seller's own id/name/slug read off the page's
    # Apollo cache. Stored as the small dict rather than by keeping the
    # page's HTML around: a detail page is 285 KB and a scrolled listing
    # nearly 1 MB.
    shop_facts: Optional[dict] = None

    @property
    def ok(self) -> bool:
        return not self.load_failed and self.blocked_by is None


ITEM_LINK_SELECTOR = page_flow.READY_SELECTOR_LISTING

# The lowest PRICE coverage that is still healthy, per page kind. Not a
# structured-price confirmation share, because the two sources here are not
# two views of one number: the SSR payload carries everything EXCEPT the
# money and the hydrated card carries only the money, so `price_source` is
# "next_data+dom" on a healthy listing row and there is nothing for a
# confirmation
# threshold to describe.
#
# What IS worth a floor is the plain share of rows that got a price at all:
# 95/95 on both search captures and 60/60 on both category captures, so
# anything below 95% means the read broke rather than the page being unusual.
# A price-coverage floor, and deliberately a low one, because on an auction
# site a missing price is usually CORRECT. Every lot with no price is a lot
# with a reserve that has not been met: 57 of 57 blank prices across 13
# captures carried `reservePriceSet: True`, spread evenly through the page
# rather than clustered at its end, so this is the site's behaviour and not a
# hydration failure. Measured coverage: 87-100% on 24-lot category and search
# pages, 68% on a 130-lot auction page whose reserve mix is heavier.
#
# The real check is the INVARIANT, not the share -- a null price must come
# with `reserve_price_set` true -- and it lives in the smoke suite and the
# canary. This floor only catches a page that hydrated not at all.
PRICE_FLOOR = {"search": 50, "category": 50, "auction": 40, "lots": 40}

# A page holding less than this share of the fullest page in the same run is
# reported as thin. This site's page size is steady — 24 lots per category
# and search page on every capture and every live run — so the bar can sit
# closer than it does on the sibling repos, but not
# tight: the last page of a listing is legitimately short.
THIN_PAGE_SHARE = 0.6


# ---------------------------------------------------------------------------
# page_flow, bound to Playwright
# ---------------------------------------------------------------------------
# Every decision about WHAT to do with a page — how long to wait, when to
# scroll, when a fresh session is the only fix — lives in page_flow.py so all
# three engines make it identically. What lives here is only HOW to ask this
# particular driver. See page_flow's docstring for why that split exists.
def _driver(page):
    # The scroll primitives are NAMED OPERATIONS rather than JavaScript, and
    # that is the point of the split. Selenium's execute_script takes a
    # function BODY with an explicit `return` while Playwright and pyppeteer
    # take `() => expr`, so a shared module handing JS across this boundary
    # would quietly acquire one driver's dialect.
    return {
        "count": lambda selector: len(page.query_selector_all(selector)),
        "sleep": page.wait_for_timeout,
        "content": lambda: _content_when_settled(page),
        "current_url": lambda: page.url,
    }


# No scroll primitives and no `page_height` here, and that is measured
# rather than omitted: three scrolls to the document's own bottom added zero
# cards and left the page height unchanged on all three page kinds. Every
# listing page holds its whole 24 lots and paginates by URL. A sibling repo
# cannot see a single product without the scroll, which is exactly why this
# was checked here instead of ported (see page_flow's docstring).


def _ready_selector(args) -> str:
    return page_flow.ready_selector(args.mode)


def _min_matches(args, html: str = "") -> int:
    """The readiness threshold, lowered to what THIS page actually holds.

    Passing the payload's own lot count is what keeps a short last page from
    spending the whole timeout and then reporting itself unpainted: a
    category whose final page holds 5 lots can never reach the default 8.
    `page_flow.expected_lots` reads it out of the first response, which is
    available before anything has hydrated.
    """
    return page_flow.min_matches(args.mode, page_flow.expected_lots(html))


def _classify(page, html: str, status=None) -> str:
    return page_flow.classify(html, status=status, url=page.url)

# Every readiness constant, every pagination selector and every state policy
# lives in page_flow.py, with its measurement beside it. Nothing about WHAT
# to do with a page is duplicated here — this file only knows HOW to ask
# Playwright.


def _advertised_next_hrefs(page, page_num: int) -> List[str]:
    """Every href on the page that could be the link to page `page_num` + 1.

    ALL of them, not the first, and the full set is handed to
    `page_flow.next_page_candidates` to filter — so the filtering rule lives
    in one place for all three engines.

    On Catawiki the site DOES advertise its own page links, and they are
    relative (`/en/c/333-watches?page=2#filters`) — resolving them before
    comparing is what stops a relative link reading as a disagreement with
    the convention. It publishes no `link[rel=next]` anywhere, and a category
    listing prints none either. The function is here so that if the site
    ever grows real pagination markup the engines pick it up instead of
    quietly relying on the URL convention forever.
    """
    selector = page_flow.next_page_selector(page_num)
    return [el.get_attribute("href") for el in page.query_selector_all(selector)]


def _plan_page_urls(page, args, page_one_url: str) -> Optional[List[str]]:
    """URLs for pages 2..N, decided once from page 1, or None to chain.

    Following the site's own next-link one page at a time is correct but
    strictly sequential: the address of page 5 is not knowable until page 4
    has been fetched. Constructing the page parameter up front removes that
    chain — which is what makes fetching pages independently (and later,
    concurrently) possible at all.

    It is only safe when the site's own link AGREES with the convention, so
    that is checked rather than assumed: if page 1's next-link is not what
    `page_url()` would build for page 2, pagination is carrying something the
    convention cannot reproduce (a cursor, a token, a filter id) and the
    caller must keep chaining link to link. Returns None in that case.

    On Catawiki both listing kinds are addressable and the site's own links
    agree with the convention — but the site CAPS it at 100 pages, and a
    request past the cap returns page 100's lots under HTTP 200 rather than
    failing. A category listing is addressable: `?page=2` on
    /p/makanan-minuman/minuman/kopi-bubuk returned 61 products against page
    1's 66, overlapping by 3 (the "cheaper products" carousel that appears
    on both). A SEARCH is not: `?page=2` on a search URL does not advance
    it, it EMPTIES the result set — "Oops, produk nggak ditemukan", no grid,
    no prices. A planner that built `?page=N` regardless would fetch that,
    find no new sku, and report a COMPLETE run holding page 1.
    """
    if args.pages < 2:
        return None

    hrefs = _advertised_next_hrefs(page, 1)
    candidates = page_flow.next_page_candidates(page_one_url, hrefs)
    dropped = len([h for h in hrefs if h]) - len(candidates)
    if dropped > 0:
        logger.info("Ignored %d advertised page-2 link(s) that paginate "
                    "something other than this listing (a shop front's "
                    "review pages are the known case).", dropped)

    constructed = page_url(page_one_url, 2)
    if candidates:
        if page_flow.pagination_agrees(page_one_url, 1, hrefs):
            logger.info("Pagination follows the ?page=N convention (a page 2 "
                        "link matches the constructed URL) — planning pages "
                        "2-%d up front.", args.pages)
        else:
            logger.info("The site's own next-page link (%s) is not what the "
                        "page convention would build (%s) — following its "
                        "links one page at a time instead. Pages cannot be "
                        "fetched independently for this listing, so "
                        "--concurrency will not help here.",
                        candidates[0], constructed)
            return None
    else:
        logger.info(
            "No pagination link for page 2 was found on page 1 — using the "
            "URL convention. That is not alarming on this site: it "
            "publishes no rel=next anywhere (measured: 0 matches on every "
            "capture), so the numbered links are all there is, and the "
            "convention is backed by the payload's own total (measured on a "
            "category listing), and on a CATEGORY URL `?page=N` addresses "
            "every page correctly — verified against a real page 2. On a "
            "SEARCH URL it does not, which is why this planner is only "
            "reached for a listing page_flow reports as addressable.")

    return [page_url(page_one_url, n) for n in range(2, args.pages + 1)]


def _next_url_from_page(page, args, page_num: int) -> str:
    """Next page's URL from the site's own link, falling back to ?page=N.

    Only used when pagination could not be planned up front. A missing link
    must not end the run: pagination resting entirely on DOM selectors is a
    silent-success failure waiting to happen, so the convention backs it up
    and the DATA decides when to stop.

    The candidate filter is not optional here either — chaining onto a shop
    front's review pagination would return rows from the wrong listing while
    reporting success.
    """
    candidates = page_flow.next_page_candidates(
        page.url, _advertised_next_hrefs(page, page_num))
    if candidates:
        return candidates[0]
    return page_url(page.url, page_num + 1)


def _same_url(a: str, b: str) -> bool:
    """Whether two URLs address the same page.

    Delegates to page_flow rather than reimplementing the comparison, so all
    three engines cannot drift on it. An engine that carried its own copy of
    this in a sibling repo went stale and silently fell back to sequential
    fetching — the exact divergence page_flow.py exists to prevent,
    reproduced inside one engine.

    On this site the comparison has to strip a long tracking tail: a listing
    anchor arrives with `?extParam=…keyword=kopi&search_id=…&src=search` and
    a detail page's own canonical arrives with a UTM triple, so two views of
    one page never match unless both sides are cleaned.
    """
    return page_flow.comparable(a) == page_flow.comparable(b)


# Chromium's own names for "the proxy is the problem, not the site". Matched
# on the error text because Playwright surfaces them as a generic Error.
_PROXY_ERROR_MARKERS = (
    "ERR_PROXY_CONNECTION_FAILED",     # nothing listening / refused
    "ERR_TUNNEL_CONNECTION_FAILED",    # CONNECT rejected by the proxy
    "ERR_PROXY_AUTH_UNSUPPORTED",      # auth scheme we cannot satisfy
    "ERR_PROXY_AUTH_REQUESTED",        # credentials missing or wrong
    "ERR_UNEXPECTED_PROXY_AUTH",
    "ERR_PROXY_CERTIFICATE_INVALID",
)


def _proxy_failure(exc) -> str:
    """The Chromium proxy-error name in `exc`, or "" if it is not one.

    Distinguishing this from an ordinary timeout matters because the two want
    opposite responses: a timeout deserves a retry from the same exit, while
    an unusable exit deserves a different exit — retrying it unchanged just
    spends the retry budget on a proxy that is not going to answer.
    """
    text = str(exc)
    for marker in _PROXY_ERROR_MARKERS:
        if marker in text:
            return marker
    return ""


def _launch_local(pw, args, pool):
    """Launch our own Chromium on `pool`'s current exit; return (browser, context, page).

    Factored out of scrape() so a proxy rotation can tear the whole browser
    down and call this again. Swapping the proxy under a live session would
    be cheaper and wrong: cookies a bot manager issued against one exit,
    replayed from another, are a stronger signal than either address alone.
    A rotation therefore means a genuinely fresh browser — new cookie jar,
    new storage — which is what an ordinary user on a different network
    looks like.
    """
    launch_kwargs = {"headless": args.headless}
    proxy = to_playwright(pool.current) if pool else None
    if proxy:
        launch_kwargs["proxy"] = proxy
        logger.info("Using proxy exit %s", mask(pool.current))

    browser = pw.chromium.launch(**launch_kwargs)
    # Only override the UA when we launched our own bundled Chromium.
    # Forcing a UA on a page reached via --cdp-endpoint mismatches the remote
    # browser's real TLS/JS fingerprint on purpose-matched values.
    ctx_kwargs = {"user_agent": _chrome_ua(browser.version), "locale": args.locale}
    init_script = None
    if args.fingerprint:
        # Only meaningful on this branch. Over --cdp-endpoint the Scraping
        # Browser already has its own fingerprint, and layering a second one
        # on top produces a mismatch rather than better cover.
        from fingerprint_client import (get_fingerprint,
                                        playwright_context_kwargs,
                                        playwright_init_script)
        fp = get_fingerprint(args.twocaptcha_key,
                             tags=args.fp_tags, country=args.fp_country)
        ctx_kwargs.update(playwright_context_kwargs(fp))
        init_script = playwright_init_script(fp)
        logger.info("Using 2captcha fingerprint %s (%s)", fp.get("id"), fp.get("country"))

    context = browser.new_context(**ctx_kwargs)
    if init_script:
        # Must be installed on the context, before any page script runs.
        context.add_init_script(init_script)
    return browser, context, context.new_page()


class _BrowserSession:
    """One browser + context + page, relaunchable onto a different exit.

    Exists because a rotation replaces all three handles at once, and passing
    three mutable locals through every helper is how one of them ends up
    stale. It also gives a worker thread a single object to own: with
    Playwright's sync API, a browser and everything reachable from it belong
    to the thread that created them, so each worker builds its own.
    """

    def __init__(self, pw, args, pool, remote: bool = False):
        self.pw, self.args, self.pool, self.remote = pw, args, pool, remote
        self.browser = self.context = self.page = None

    def open(self):
        if self.remote:
            self.browser, self.context, self.page = _connect_remote(self.pw, self.args)
        else:
            self.browser, self.context, self.page = _launch_local(
                self.pw, self.args, self.pool)
        return self

    def relaunch(self):
        """Tear the browser down and come back on the pool's current exit.

        On a remote browser this is a no-op — its exit is not ours to change.
        """
        if self.remote:
            return
        try:
            self.browser.close()
        except Exception as e:  # noqa: BLE001 — teardown must not mask the reason we're here
            logger.debug("Ignoring error while closing browser for rotation: %s", e)
        self.open()

    def close(self):
        try:
            if self.remote:
                self.page.close()  # leave the remote browser app running
            else:
                self.browser.close()
        except Exception as e:  # noqa: BLE001
            logger.debug("Ignoring error during browser teardown: %s", e)


def _connect_remote(pw, args):
    """Attach to an already-running browser over CDP; return (browser, context, page)."""
    logger.info("Connecting to existing browser over CDP: %s",
                _mask_credentials(args.cdp_endpoint))
    # Explicit timeout. Playwright defaults to 30s here, but stating it makes
    # the contract visible next to the pyppeteer twin, which has no connect
    # timeout at all. A Scraping Browser session that is still held answers
    # with HTTP 500 rather than stalling, so this mostly guards against the
    # endpoint going quiet.
    try:
        browser = pw.chromium.connect_over_cdp(args.cdp_endpoint, timeout=30000)
    except (PWError, PWTimeout) as e:
        # Playwright puts the endpoint it tried into the exception text, and
        # the endpoint is a URL with the password in it. Unmasked, that
        # password lands in the terminal, in CI output and in any log the run
        # is piped to — which is the one thing this project promises does not
        # happen ("credentials never reach argv or logs"). The message is
        # rewritten with the credentials masked and the host and port kept,
        # because WHICH endpoint failed is the useful half and is not the
        # secret.
        raise PWError(
            f"could not connect to --cdp-endpoint "
            f"{_mask_credentials(args.cdp_endpoint)}: "
            f"{_mask_credentials(str(e))}\n"
            f"A Scraping Browser profile allows ONE live connection at a "
            f"time, so a 500 here usually means another run still holds this "
            f"`pid`. Wait for it to finish, or use a different pid."
        ) from None
    # Reuse the remote browser's existing context so its
    # fingerprint/session/proxy settings stay intact.
    context = browser.contexts[0] if browser.contexts else browser.new_context()
    page = context.new_page()

    # The Scraping Browser API exposes a documented CDP domain
    # (`Captcha.setAutoSolve` / `Captcha.solve`) that clears supported
    # challenges inside the browser: https://2captcha.com/scraper/browser-api/api
    # Tried first when --cdp-endpoint is set; this script's own detect+solve
    # logic still runs as a fallback if the endpoint does not support it.
    # Note it does NOT cover this site's refusal, which is not a challenge:
    # Akamai answers a headless browser with a 394-byte "Access Denied" that
    # has nothing to solve on it, and a real window rather than a better
    # address is the answer. No challenge has ever been observed here; this is
    # wired
    # up because one can appear between deploys.
    try:
        cdp_session = context.new_cdp_session(page)
        cdp_session.send("Captcha.setAutoSolve", {"autoSolve": True, "options": [{"type": "*"}]})
        cdp_session.on("Captcha.detected", lambda *_: logger.info("[Scraping Browser] CAPTCHA detected on page."))
        cdp_session.on("Captcha.waitForSolve", lambda *_: logger.info("[Scraping Browser] CAPTCHA sent to 2captcha for solving."))
        cdp_session.on("Captcha.solveFinished", lambda *_: logger.info("[Scraping Browser] CAPTCHA solved automatically."))
        cdp_session.on("Captcha.solveFailed", lambda *_: logger.warning("[Scraping Browser] CAPTCHA auto-solve failed."))
        logger.info("Scraping Browser API Captcha.setAutoSolve enabled — supported "
                    "challenge types will be solved automatically if this "
                    "--cdp-endpoint is a Scraping Browser API session.")
    except Exception as e:
        logger.info("Captcha.setAutoSolve not available on this --cdp-endpoint (%s) — "
                    "relying on this script's own detect+solve logic instead.", e)
    return browser, context, page


def _resolve_pagination_url(base_url: str, href: str) -> str:
    """Resolve a pagination link's raw href against the page it came from.

    Playwright's get_attribute("href") returns the raw HTML attribute,
    unresolved — unlike the DOM .href property Puppeteer/Selenium read for
    the same purpose in this project, which the browser resolves for you.
    urljoin handles every shape correctly — absolute, protocol-relative,
    absolute-path, and page-relative hrefs alike.
    """
    return urljoin(base_url, href)


# Every `scheme://user:pass@` in a string, however many times it occurs.
# Matching globally rather than once is the point: a Playwright connection
# error repeats the endpoint five times (the message plus a four-line call
# log), so a masker that handled only the first occurrence would print the
# password four times and look like it was working.
_CREDENTIALS_IN_URL_RE = re.compile(r"([a-z][a-z0-9+.\-]*://)[^\s/@]+:[^\s/@]+@",
                                    re.IGNORECASE)


def _mask_credentials(text: str) -> str:
    """`text` with any username:password in an embedded URL replaced.

    Takes arbitrary text, not just a URL, because the strings that most need
    this are exception messages with a URL inside them. The host and port are
    KEPT — which endpoint or exit a run used is the useful half of the line
    and is not the secret.
    """
    return _CREDENTIALS_IN_URL_RE.sub(r"\1***:***@", text or "")


def _content_when_settled(page, attempts: int = 4, pause_ms: int = 700):
    """page.content() that tolerates a page mid-navigation.

    Playwright raises `Page.content: Unable to retrieve content because the
    page is navigating and changing the content` if the document swaps under
    it. Catawiki does not geo-redirect: the locale is a path segment, and euro
    from two exit countries — but its shell navigates on its own during
    hydration, and the remote Scraping Browser target was observed closing
    mid-scroll on three of six captures, so a snapshot taken right after
    goto() can land exactly on a swap.

    Retries briefly and returns None if the page won't hold still, so the
    caller can skip a check instead of failing the run.
    """
    for attempt in range(1, attempts + 1):
        try:
            return page.content()
        except PWError as e:
            if "navigating" not in str(e).lower():
                raise
            if attempt == attempts:
                logger.warning("Page kept navigating through %d attempts — "
                               "continuing without a snapshot.", attempts)
                return None
            logger.info("Page is navigating (a geo-redirect or the consent "
                        "layer?) — retrying content() in %dms (%d/%d).",
                        pause_ms, attempt, attempts)
            page.wait_for_timeout(pause_ms)
    return None


def handle_captcha_if_present(page, args) -> bool:
    """Detect and solve a challenge. True if something was solved.

    Runs after EVERY navigation, for ANY page — not scoped to one URL. The
    static-HTML and runtime reCAPTCHA detectors are run and reconciled
    against each other rather than short-circuited, because they can disagree
    about the variant and the parameters for one are rejected for the other.

    NOTE what this cannot help with. This site's refusal carries nothing to
    solve — a 394-byte "Access Denied" — and it is triggered by the browser
    being headless rather than by the address, so a 2Captcha key does nothing
    about the state a blocked run is most likely to meet, and
    `detect_page_state` reports that as "blocked" rather than "challenge"
    precisely so no solve is attempted or billed. No challenge has ever been
    observed on this site. This path exists because a bot manager can be
    switched on between deploys, and because the family's rule is that
    detection stays broad:
    different geos and scenarios surface different challenges.
    """
    html = _content_when_settled(page)
    if html is None:
        # Couldn't get a stable snapshot — skip detection for this navigation
        # rather than taking the whole run down. The next navigation gets
        # another chance, and the parse below reads its own copy of the DOM.
        return False

    # Detected is not the same as blocking. A challenge on a page whose
    # products are already rendered guards nothing, and counting the anchors
    # is instant — no wait_for_function, no 20s — which is why this check
    # sits here rather than after the readiness wait. Doing it the other way
    # round would cost 20 wasted seconds on a page the captcha genuinely
    # gates, where solving FIRST is what makes the content appear.
    already_rendered = len(page.query_selector_all(_ready_selector(args)))
    when_blocked = getattr(args, "solve_captcha", "when-blocked") == "when-blocked"

    html_challenge = detect_recaptcha_v3(html, page.url)
    runtime_challenge = detect_recaptcha_in_page(
        lambda js: page.evaluate(js), page_url=page.url)
    challenge = reconcile_detections(html_challenge, runtime_challenge)
    if not challenge:
        return False

    if when_blocked and already_rendered > MIN_CARD_MATCHES:
        logger.info("%s detected via %s, but %d anchors are already on the "
                    "page — not solving it. Pass --solve-captcha always to "
                    "solve it anyway.", challenge.kind, challenge.source,
                    already_rendered)
        return False

    logger.warning("%s detected via %s (sitekey=%s, action=%s) — attempting to solve.",
                   challenge.kind, challenge.source, challenge.sitekey, challenge.action)
    if not args.twocaptcha_key:
        logger.warning("No 2captcha API key, so this challenge cannot be "
                       "solved — continuing with whatever the page already "
                       "holds.")
        return False
    try:
        token = solve_recaptcha(challenge, args.twocaptcha_key,
                               api_version=args.captcha_api,
                               min_score=args.min_score)
    except Exception as e:  # noqa: BLE001 — a solver failure is not a crash
        logger.error("Solving the challenge failed (%s) — continuing with "
                     "whatever the page holds.", e)
        return False

    page.evaluate(INJECT_TOKEN_JS, token)
    logger.info("Token injected. Reloading page to continue.")
    page.wait_for_timeout(1500)
    page.reload(wait_until="domcontentloaded", timeout=60000)
    return True


def _parse_for_mode(html: str, url: str, args, page_num: int = 1) -> List:
    """Rows for this mode, always as a list even when the mode yields one.

    `parse_lot_page` returns a single row or None; wrapping it here keeps
    every caller downstream — dedupe, merge, coverage logging, the writers —
    working on one shape instead of branching on the mode again.

    `page_num` is threaded through rather than defaulted, because `position`
    restarts at 1 on every page: without the page number beside it, a row
    from page 2 claims the same position as one from page 1 and the two are
    indistinguishable in the output. The first live run of this engine
    wrote 120 rows all labelled page 1 for exactly that reason.
    """
    if args.mode == "lot":
        row = parse_lot_page(html, url, category=args.category)
        return [row] if row is not None else []
    if args.mode == "auctions":
        return auction_rows(html, url, page=page_num)
    return parse_products(html, url, page=page_num, category=args.category)


def _fetch_one_page(session, args, pool, page_num: int, url: str) -> PageOutcome:
    """Fetch and parse one page. Retries, rotations and debug dumps live here.

    Returns a PageOutcome and never raises for an EXPECTED failure — a
    timeout, a 403 refusal, a captcha page, a dead exit are all recorded on the
    outcome instead. What the run should do about them differs between the
    sequential and concurrent paths, so that decision belongs to the caller
    rather than to a raised exception unwinding through it.

    Always goes through `session.page`, never a captured local: a rotation
    replaces the browser, context and page together, and a stale handle is
    exactly the bug _BrowserSession exists to prevent.
    """
    outcome = PageOutcome(page_num=page_num, url=url)

    # How many times a blocked page may be retried.
    #
    # With a pool, each retry moves to a DIFFERENT exit and the budget is the
    # user's `--proxy-block-retries`. WITHOUT one — the ordinary case here,
    # because `--cdp-endpoint` brings its own exit — the retry re-fetches
    # through the same access path, and that is worth doing on this site
    # rather than giving up: a Scraping Browser profile was measured refusing
    # two requests and serving the third. Zero was the family default and it
    # made the first live run of this engine abandon page 1 on its first
    # block without retrying once.
    has_pool = bool(pool and len(pool) > 1)
    # `RETRY_ON_BLOCKED` is CONSULTED, not just documented. It was a
    # constant with a paragraph of justification that no engine read — a
    # policy statement nothing enforced, which is the same defect as dead
    # code that looks load-bearing. Setting it False now really does stop
    # the retry loop.
    block_retries = 0 if not page_flow.RETRY_ON_BLOCKED else (
        args.proxy_block_retries if has_pool
        else page_flow.BLOCK_RETRIES_WITHOUT_POOL)
    # Counted across the whole block-retry loop, not per attempt: a page that
    # keeps coming back as a challenge would otherwise buy one solve per
    # rotation, which is how a run quietly turns into a bill.
    solves_bought = 0
    html, state, load_failed = None, "ok", False

    for block_attempt in range(block_retries + 1):
        logger.info("Fetching page %d/%d: %s", page_num, args.pages, url)
        # Retry a navigation timeout rather than ending the run on it. One
        # network flap on page 12 of 50 should not break the loop.
        load_failed, exit_failed = False, None
        for attempt in range(1, args.retries + 1):
            try:
                session.page.goto(url, wait_until="domcontentloaded", timeout=60000)
                load_failed = False
                break
            except (PWTimeout, PWError) as e:
                # A dead or misconfigured proxy raises PWError
                # (net::ERR_PROXY_CONNECTION_FAILED), not PWTimeout —
                # catching only the latter lets it escape as a traceback,
                # which is the likeliest failure the first time anyone points
                # --proxy-file at a real list.
                reason = _proxy_failure(e)
                if reason:
                    exit_failed = reason
                    load_failed = True
                    break  # a different exit is the only thing that helps
                load_failed = True
                if attempt < args.retries:
                    pause = args.retry_delay * (2 ** (attempt - 1))
                    logger.warning("Timeout loading %s (attempt %d/%d) — "
                                   "retrying in %.1fs.", url, attempt,
                                   args.retries, pause)
                    time.sleep(pause)

        if exit_failed and has_pool and block_attempt < block_retries:
            logger.warning("Exit %s is unusable (%s) — rotating to another "
                           "one (%d/%d).", mask(pool.current), exit_failed,
                           block_attempt + 1, block_retries)
            pool.advance(f"unusable exit: {exit_failed}")
            session.relaunch()
            continue
        if load_failed:
            break

        if handle_captcha_if_present(session.page, args):
            # A solve navigated the page. Give the destination a moment
            # before judging what came back.
            session.page.wait_for_timeout(1000)

        html = _content_when_settled(session.page) or ""
        state = _classify(session.page, html)

        # "Not painted yet" is not a fault, and telling it apart from one is
        # what the first live search run of this engine got wrong. A CATEGORY
        # listing server-renders its grid container, so it classifies as
        # content at domcontentloaded; a SEARCH grid arrives with the
        # client-side GraphQL response, so at that moment the page is a
        # 608 KB shell with no grid in it. Classified naively that is
        # "unknown", "unknown" retries, and the run fetched the page twice,
        # scrolled not at all and reported 0 rows with exit 4.
        #
        # So wait for the anchor and re-classify BEFORE the retry decision.
        # See page_flow.is_unpainted.
        if page_flow.is_unpainted(state, html):
            wait_timeout = page_flow.content_timeout_ms(args.mode)
            logger.info("Page %d is a shell the site served but has not "
                        "filled in (%d bytes, empty cards) — waiting up to "
                        "%.0fs for the prices rather than spending a retry.",
                        page_num, len(html), wait_timeout / 1000)
            found = page_flow.wait_for_count(
                lambda sel: len(session.page.query_selector_all(sel)),
                session.page.wait_for_timeout,
                _ready_selector(args), _min_matches(args, html), wait_timeout)
            if found <= _min_matches(args, html):
                logger.info("The grid still had not painted after %.0fs "
                            "(%d match(es)).", wait_timeout / 1000, found)
            html = _content_when_settled(session.page) or html
            state = _classify(session.page, html)

        # No interstitial-settling step here, and its absence is measured
        # rather than an omission. This site has no interstitial to settle: a
        # refused request gets a 394-byte "Access Denied" with no markup at
        # all, so there is nothing to wait out and nothing to reclassify. See
        # page_flow's "There is no block page".
        #
        # The paid path is reached only for state "challenge", which NO
        # capture of this site has ever produced. It is wired up because a
        # bot manager can be switched on between deploys and a scraper that
        # cannot name what stopped it is much harder to fix — and bounded by
        # SOLVES_PER_PAGE so a speculative path cannot become a bill.
        if (page_flow.should_solve(state)
                and solves_bought < page_flow.SOLVES_PER_PAGE):
            solves_bought += 1
            if handle_captcha_if_present(session.page, args):
                session.page.wait_for_timeout(1000)
                html = _content_when_settled(session.page) or html
                state = _classify(session.page, html)
                # The VERIFIED outcome, and the only one worth reporting: a
                # "ready" task result is not evidence the token works. This
                # line is what says whether the money bought anything.
                if state == "content":
                    logger.info("The solve was accepted — page %d is content "
                                "now.", page_num)
                else:
                    logger.warning(
                        "The solve was NOT accepted: page %d is still %s. The "
                        "purchase is spent.", page_num, state)

        if not page_flow.should_retry(state):
            # "content" and "empty" are both final answers. An empty page is
            # a CORRECT one — a hub category has no grid, and one page past
            # the end of a listing has no products — so retrying it would
            # spend the user's budget re-confirming the same right answer,
            # and rotating the exit would blame an address for the URL it was
            # given.
            break

        # Blocked or challenged. A different exit is the one thing that
        # plausibly changes the outcome: the ADDRESS is what was scored, not
        # the URL, so retrying it unchanged would only confirm it. Measured
        # 2026-09-09 — the same URL that answers 403 from a datacentre exit
        # answers 200 from a residential one.
        if block_attempt < block_retries:
            if has_pool:
                logger.warning("Page %d came back as %s from %s — retrying "
                               "from another exit (%d/%d).", page_num, state,
                               mask(pool.current), block_attempt + 1,
                               block_retries)
                pool.advance(f"{state} on page {page_num}")
                session.relaunch()
            else:
                # No pool, so nowhere else to go — but a plain re-fetch is
                # what clears this on a Scraping Browser profile. The browser
                # is NOT relaunched: over `--cdp-endpoint` a profile allows
                # one live connection, so tearing the session down and
                # reconnecting risks `profile_locked` and would lose the very
                # cookies the retry is meant to build on.
                pause = args.retry_delay * (block_attempt + 1)
                logger.warning("Page %d came back as %s — re-fetching through "
                               "the same access path in %.1fs (%d/%d). On this "
                               "site that is often what clears it.",
                               page_num, state, pause, block_attempt + 1,
                               block_retries)
                time.sleep(pause)

    if load_failed:
        logger.error("Gave up loading %s after %d attempt(s).", url, args.retries)
        outcome.load_failed = True
        return outcome

    outcome.state = state

    if state == "blocked":
        # What a caller needs to know here is that there is nothing to solve.
        # This site does not send a challenge or a captcha to a request it
        # refuses: Akamai answers with HTTP 403 and a 394-byte "Access
        # Denied" page carrying a reference id and nothing to solve. So a
        # 2Captcha key does nothing about the state a blocked run is most
        # likely to meet, and saying what DOES clear it is more use than a
        # captcha hint that would cost money.
        #
        # The dump is written even when it is empty, because "0 bytes" is
        # itself the diagnosis on this site and a reader who finds no file at
        # all cannot tell that from a run that never got here.
        debug_html = f"{args.out}_page{page_num}_debug.html"
        with open(debug_html, "w", encoding="utf-8") as f:
            f.write(html or "")
        served = served_by_catawiki(html or "")
        logger.error(

            "The site did not serve this request — %d bytes, %s the site's "
            "own asset host, saved to %s. On this site a refusal is a 394-byte "
            "\"Access Denied\" with no vendor name and nothing to solve, and "
            "it is triggered by the browser being headless rather than by the "
            "address: no interstitial, no "
            "vendor marker, so there is no challenge to solve and no key "
            "would help. What clears it, measured 2026-09-10: a residential "
            "exit. The country does not matter — an Indonesian and a US "
            "residential exit returned identical pages — but a datacentre "
            "address gets nothing. Use --cdp-endpoint, or a residential "
            "--proxy. This is exit 3, distinct from a genuinely empty result "
            "(exit 4).%s",
            len(html or ""), "which references" if served else "with no "
            "reference to", debug_html,
            (f" Tried {block_retries + 1} exit(s)." if has_pool
             else f" Re-fetched {block_retries + 1} time(s)."))
        outcome.blocked_by = "no-response" if not html else "not-served"
        outcome.final_url = session.page.url
        return outcome

    if state == "content":
        # Don't wait for network idle (retail sites never go fully quiet) and
        # don't accept a single selector match as "ready".
        #
        # NO scroll follows, and that is measured: three scrolls to the
        # document's own bottom added zero cards and left the page height
        # unchanged on all three page kinds. The whole page is served at once,
        # so without the scroll below a listing run returns whatever the
        # first batch happened to have painted. Measured on one search URL:
        # 11 tiles at round 0, 95 after one scroll.
        selector, threshold = _ready_selector(args), _min_matches(args, html)
        content_timeout = page_flow.content_timeout_ms(args.mode)
        # A POLL, not wait_for_function. This site's CSP does allow
        # `unsafe-eval` today, so an evaluated string would work — but
        # wait_for_function hands the browser a STRING, and on a site whose
        # CSP forbids it that raises EvalError and takes the run down with exit
        # 1. See page_flow.wait_for_count.
        found = page_flow.wait_for_count(
            lambda sel: len(session.page.query_selector_all(sel)),
            session.page.wait_for_timeout, selector, threshold, content_timeout)
        session.page.wait_for_timeout(500)
        if found <= threshold:
            # Not an error on its own, and what it MEANS depends on the
            # mode — which is why the message does too. A listing page with
            # no grid is a correct answer (a taxonomy hub, or one page past
            # the end); a detail page whose buy box never painted is a
            # different thing entirely, and on this site it is usually just
            # slow rather than absent, because the row is parsed out of the
            # page's JSON-LD and not out of the buy box.
            if args.mode == "lot":
                logger.info("The product name did not paint within %.0fs. "
                            "That is not fatal: a detail row is read from the "
                            "page's own Apollo cache and its price meta, both "
                            "of which are in the first response, so the parse "
                            "below decides.", content_timeout / 1000)
            else:
                logger.info("No listing tiles appeared within %.0fs. If this "
                            "URL is a /p/<slug> discovery hub or one page "
                            "past the end of a listing, that is the expected "
                            "answer and the run will report 0 rows (exit 4).",
                            content_timeout / 1000)

        html = _content_when_settled(session.page) or html

    # Dumping on success, not only on failure: a run can return the right
    # NUMBER of rows with a field silently unpopulated, and then the only way
    # to tell a parsing bug from a too-early snapshot is to inspect the exact
    # bytes the parser was given.
    if args.dump_html:
        dump_path = (args.dump_html if args.pages == 1
                     else f"{args.dump_html}.page{page_num}")
        with open(dump_path, "w", encoding="utf-8") as f:
            f.write(html)
        logger.info("Saved the snapshot the parser sees to %s (%d bytes).",
                    dump_path, len(html))

    # Only for a state page_flow already counts as BLOCKED, and that
    # narrowing was earned twice.
    #
    # A marker on a page whose products have rendered guards nothing — that
    # is the "detected is not blocking" rule the captcha default follows,
    # applied to the blocking decision instead of the spending one. But
    # `state != "content"` is still too wide: an EMPTY page is a correct
    # answer, and a live run of a /p/<slug> hub reported exit 3 on a 191 KB
    # page the site had plainly served, because the hub's own performance
    # script names `akamaihd.net` and "akamai" was in the marker list. Both
    # halves were wrong; the marker is gone (see
    # product_parser.BOT_CHALLENGE_MARKERS) and this now only refines the
    # REASON for a page the policy had already given up on.
    vendor = (detect_bot_challenge(html, url=session.page.url)
              if page_flow.counts_as_blocked(state) else None)
    if vendor:
        debug_html = f"{args.out}_page{page_num}_debug.html"
        with open(debug_html, "w", encoding="utf-8") as f:
            f.write(html)
        try:
            session.page.screenshot(path=f"{args.out}_page{page_num}_debug.png",
                                    full_page=True)
        except Exception as e:
            logger.warning("Could not capture screenshot: %s", e)
        logger.error("Blocked by %s before parsing (%d bytes) — saved to %s%s. "
                     "This is exit 3, distinct from a genuinely empty result "
                     "(exit 4).", vendor, len(html), debug_html,
                     (f" (tried {block_retries + 1} exit(s))" if has_pool
                      else f" (re-fetched {block_retries + 1} time(s))"))
        outcome.blocked_by = vendor
        logger.error("%s", page_flow.block_advice(
            html, headless=bool(getattr(args, "headless", False)),
            has_pool=has_pool))
        return outcome

    products = _parse_for_mode(html, session.page.url, args, page_num)
    logger.info("Parsed %d row(s) from page %d.", len(products), page_num)

    if args.mode == "lot":
        outcome.auction_facts = auction_metadata(html, session.page.url)

    # This site DOES publish a result total, in its own payload, so the
    # arithmetic completeness check is available here. What it does not have
    # is a printed header worth reading — the old note about
    # untuk "kopi"" — and the total is empty on every capture; the range it
    # does print is not a count of loaded rows either (it claimed 61-180
    # while the DOM held 95). So the header is recorded verbatim and
    # `total_available` stays None rather than carrying a number nobody can
    # act on.
    if args.mode == "listing" and page_num == 1:
        outcome.total_available = total_results(html)
        outcome.header = search_header(html)
        if outcome.header:
            logger.info("The page's own result header says: %s", outcome.header)

    if products and args.mode == "listing":
        priced = sum(1 for p in products if p.price is not None)
        share = 100.0 * priced / len(products)
        kind = listing_kind(session.page.url)
        floor = PRICE_FLOOR.get(kind, 0)
        # Reported every time, not only when it looks wrong, so a consumer
        # gets the number rather than a threshold someone guessed. On
        # this site the healthy figure is 87-100% on a category or search
        # page and can be 68% on a reserve-heavy auction: a blank price is a
        # lot whose reserve has not been met, which is why the floor is low
        # and the real check is the reserve invariant.
        logger.info("Price coverage on page %d (%s page): %d/%d (%.0f%%); the "
                    "floor for this page kind is %d%%.",
                    page_num, kind, priced, len(products), share, floor)
        if share < floor:
            logger.warning(
                "Only %.0f%% of page %d carries a price, against a measured "
                "floor of %d%% for a %s page. Every row of every captured "
                "page had one, so this is the read breaking rather than the "
                "page being unusual — re-run with --dump-html.",
                share, page_num, floor, kind)

        # There is deliberately NO structured-price confirmation share here,
        # and its absence is measured rather than an omission: the two sources
        # on this site are not two views of one number. The payload carries
        # everything except the money and the card carries only the money, so
        # `price_source`
        # is "dom" on every listing row and a confirmation threshold would
        # describe nothing. Porting the sibling repo's would be dead code
        # that looks load-bearing.
        #
        # What IS worth reporting is how much of the grid had painted its
        # images, because that is the one column this site makes sparse on
        # purpose.
        with_image = sum(1 for p in products if p.image_url)
        logger.info("Lot images on page %d: %d/%d (%.0f%%). Read from the "
                    "payload rather than from an <img> tag, so this is not a "
                    "rendering measure: a null here means the site published "
                    "no image url for that lot.",
                    page_num, with_image, len(products),
                    100.0 * with_image / len(products))

    if not products:
        debug_html = f"{args.out}_page{page_num}_debug.html"
        debug_png = f"{args.out}_page{page_num}_debug.png"
        with open(debug_html, "w", encoding="utf-8") as f:
            f.write(html)
        try:
            session.page.screenshot(path=debug_png, full_page=True)
        except Exception as e:
            logger.warning("Could not capture screenshot: %s", e)
        logger.warning("0 rows parsed — saved what the browser actually saw to "
                       "%s and %s. Open the .png to see it.", debug_html, debug_png)

    outcome.products = products
    outcome.final_url = session.page.url
    return outcome


def _worker_pool(pool, worker_index: int):
    """A private ProxyPool for one worker, starting at a different exit.

    Each worker gets its OWN pool object holding the same exits rotated to a
    different offset. Two things fall out of that, both wanted:

      * Workers start on distinct exits, which is the point of running
        several — N workers all leaving from one address is just a faster way
        to burn that address.
      * No shared mutable state between threads, so rotation needs no lock.
        A worker that gets blocked can still walk the rest of the pool on its
        own.

    Its exit stays put for the worker's lifetime otherwise: a SESSION must
    not change address mid-flight, and a worker is one session.
    """
    if not pool:
        return None
    proxies = pool.proxies
    offset = worker_index % len(proxies)
    return ProxyPool(proxies[offset:] + proxies[:offset], rotate="per-run")


def _fetch_pages_concurrently(args, pool, specs, concurrency: int):
    """Fetch `specs` [(page_num, url), ...] across `concurrency` workers.

    Each worker owns its own Playwright instance, browser and exit: with the
    sync API a browser belongs to the thread that made it, so sharing one
    across threads is not an option even if it were desirable.
    """
    work = queue.Queue()
    for spec in specs:
        work.put(spec)

    results = []
    results_lock = threading.Lock()
    # Set when a page comes back with no rows at all — the end of the
    # listing. Without it, asking for 50 pages of a 5-page result would fetch
    # 45 empty ones. Workers check it before taking more work, so at most
    # (concurrency - 1) extra pages are in flight when it trips.
    exhausted = threading.Event()

    def worker(index: int):
        name = f"worker-{index + 1}"
        try:
            with sync_playwright() as pw:
                session = _BrowserSession(pw, args, _worker_pool(pool, index)).open()
                try:
                    first = True
                    while not exhausted.is_set():
                        try:
                            page_num, url = work.get_nowait()
                        except queue.Empty:
                            break
                        if not first:
                            time.sleep(args.delay)
                        first = False
                        outcome = _fetch_one_page(session, args, session.pool,
                                                  page_num, url)
                        with results_lock:
                            results.append(outcome)
                        if outcome.ok and not outcome.products:
                            logger.info("[%s] page %d returned no rows — "
                                        "treating that as the end of the listing "
                                        "and stopping dispatch.", name, page_num)
                            exhausted.set()
                finally:
                    session.close()
        except Exception:  # noqa: BLE001 — a dead worker must not hang the run
            logger.exception("[%s] died; its pages will be reported as failed.", name)

    threads = [threading.Thread(target=worker, args=(i,), name=f"page-worker-{i + 1}")
               for i in range(concurrency)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Anything still queued was never attempted (a worker died, or dispatch
    # stopped at the end of the listing). Not reported as failed pages: they
    # were not tried, and claiming otherwise would overstate the damage.
    unattempted = []
    while True:
        try:
            unattempted.append(work.get_nowait()[0])
        except queue.Empty:
            break
    return results, sorted(unattempted), exhausted.is_set()


def scrape(args) -> int:
    # One entry per page attempted, merged after the loop rather than folded
    # into shared state during it — see PageOutcome for why that ordering
    # matters more than it looks.
    outcomes: List[PageOutcome] = []
    seen_keys = set()
    blocked = False
    # Both modes are one row per product, so `sku` is the key for both.
    dedupe_key = "sku"
    # Why the loop ended. "completed" means every requested page was fetched;
    # "no_new_products" means the listing itself ran out (also a complete
    # result). "single_page_mode" is complete by construction — a detail page
    # has no page 2. Anything else is an early stop, and the run is only a
    # partial view.
    # Only --mode product is single-page. A SHOP FRONT paginates exactly like
    # a category listing — ?page=N, the same tiles — and treating it as
    # single-page made `--mode shop --pages 2` fetch one page and report
    # "complete", which is the silent-success failure this family exists to
    # avoid. Found on the first live shop run.
    stop_reason = ("single_page_mode" if args.mode in ("lot", "auctions")
                   else "completed")

    pool = proxy_pool_from_args(args)
    if pool and args.cdp_endpoint:
        logger.warning("Ignoring --proxy/--proxy-file: with --cdp-endpoint the "
                       "remote browser has its own exit, and layering a second "
                       "proxy on top would contradict it.")
        pool = None

    concurrency = max(1, args.concurrency)
    if concurrency > 1:
        if args.mode == "lot":
            logger.info("--concurrency is ignored in --mode product: there is "
                        "one page to fetch.")
            concurrency = 1
        elif args.cdp_endpoint:
            logger.warning("--concurrency is ignored with --cdp-endpoint: the "
                           "Scraping Browser API allows one live connection per "
                           "profile, and several workers would collide on it "
                           "(profile_locked). Use several pids instead, one run "
                           "each.")
            concurrency = 1
        elif not pool:
            logger.warning("--concurrency %d with no proxy pool: every worker "
                           "leaves from the SAME address, which is a faster way "
                           "to get that address scored than to gather data. "
                           "Catawiki already refuses a headless browser whatever the address it has "
                           "scored by answering NOTHING at all, so an address "
                           "that works is one worth not burning. Pass "
                           "--proxy-file to spread the load.", concurrency)
        if pool and pool.rotates_per_page():
            logger.info("--proxy-rotate per-page is redundant under "
                        "--concurrency: each worker already holds its own exit "
                        "for its lifetime, which is the same spread without a "
                        "browser relaunch per page.")
        if concurrency > 8:
            logger.warning("--concurrency %d means %d browsers at once "
                           "(~150-300MB each). Make sure the machine has the "
                           "memory for it.", concurrency, concurrency)

    with sync_playwright() as pw:
        session = _BrowserSession(pw, args, pool,
                                  remote=bool(args.cdp_endpoint)).open()
        try:
            # Page 1 is always fetched on its own: its content is what decides
            # whether pages 2..N can be addressed independently at all.
            first = _fetch_one_page(session, args, pool, 1, args.url)
            outcomes.append(first)

            if not first.ok:
                stop_reason = ("page_load_timeout" if first.load_failed
                               else f"blocked_{first.blocked_by}")
                blocked = first.blocked_by is not None
            elif args.mode == "lot":
                pass  # one page is the whole run
            else:
                seen_keys.update(p.sku for p in first.products if p.sku is not None)
                planned = _plan_page_urls(session.page, args, first.final_url)

                if args.pages > 1 and concurrency > 1 and planned is None:
                    logger.warning("--concurrency %d requested, but this "
                                   "listing's pagination cannot be addressed "
                                   "independently (see above) — falling back to "
                                   "one page at a time.", concurrency)
                    concurrency = 1

                if args.pages > 1 and concurrency > 1:
                    # Close the page-1 browser before starting workers: it has
                    # done its job, and holding it open would cost one more
                    # browser than asked for.
                    session.close()
                    specs = [(n, planned[n - 2]) for n in range(2, args.pages + 1)]
                    logger.info("Fetching pages 2-%d across %d workers%s.",
                                args.pages, concurrency,
                                f" over {len(pool)} exit(s)" if pool else "")
                    rest, unattempted, exhausted = _fetch_pages_concurrently(
                        args, pool, specs, concurrency)
                    outcomes.extend(rest)

                    failed = [o for o in rest if not o.ok]
                    if failed:
                        worst = min(failed, key=lambda o: o.page_num)
                        stop_reason = ("page_load_timeout" if worst.load_failed
                                       else f"blocked_{worst.blocked_by}")
                        blocked = any(o.blocked_by for o in rest)
                    elif exhausted:
                        stop_reason = "no_new_products"
                    elif unattempted:
                        # Should not happen without a failure or exhaustion,
                        # but say so rather than reporting a complete run.
                        stop_reason = "pages_unattempted"
                    session = None  # already closed
                else:
                    url = (planned[0] if planned else
                           _next_url_from_page(session.page, args, 1))
                    for page_num in range(2, args.pages + 1):
                        # A new exit per page is what actually spreads a run's
                        # volume, and it costs a browser relaunch: carrying the
                        # session across exits would defeat the point.
                        if pool and pool.rotates_per_page():
                            pool.advance(f"per-page rotation, page {page_num}")
                            session.relaunch()

                        outcome = _fetch_one_page(session, args, pool, page_num, url)
                        outcomes.append(outcome)
                        if not outcome.ok:
                            stop_reason = ("page_load_timeout" if outcome.load_failed
                                           else f"blocked_{outcome.blocked_by}")
                            blocked = outcome.blocked_by is not None
                            break

                        # Whether this page contributed anything not already
                        # seen. Kept as a running check because the condition is
                        # inherently sequential — "new" only means anything
                        # relative to the pages before it. The authoritative
                        # dedupe happens once, after the loop, in page order.
                        fresh_count = sum(1 for p in outcome.products
                                          if p.sku is None or p.sku not in seen_keys)
                        seen_keys.update(p.sku for p in outcome.products
                                         if p.sku is not None)

                        # A page past the first that contributes nothing new
                        # means the end of the results — or that pagination is
                        # looping back on itself. Either way there is nothing
                        # further to fetch, and this is the honest terminating
                        # condition: a property of the DATA, not of a CSS
                        # selector that may have been renamed.
                        if not fresh_count:
                            logger.info("Page %d added no rows not already seen "
                                        "— treating that as the end of the "
                                        "listing.", page_num)
                            stop_reason = "no_new_products"
                            break

                        if page_num < args.pages:
                            url = (planned[page_num - 1] if planned else
                                   _next_url_from_page(session.page, args, page_num))
                            time.sleep(args.delay)
        finally:
            if session is not None:
                session.close()

    # Merge once, in PAGE order — not in the order pages happened to finish.
    # At one page at a time the two are identical, which is the point: this is
    # what keeps the output byte-for-byte the same while removing the
    # dependency on arrival order that concurrency would otherwise introduce.
    all_rows = []
    merged_seen = set()
    for oc in sorted(outcomes, key=lambda o: o.page_num):
        fresh = dedupe_by_key(oc.products, merged_seen, key=dedupe_key)
        if len(fresh) < len(oc.products):
            # Not necessarily "on an earlier page" — a duplicate can be on
            # this page. This site's pagination was measured NOT repeating —
            # 72 rows across three pages, 72 distinct sku — but a live auction
            # listing reorders as lots close, so a
            # small non-zero count here is expected and a large one is not.
            logger.info("Page %d: dropped %d duplicate row(s).",
                        oc.page_num, len(oc.products) - len(fresh))
        all_rows.extend(fresh)

    # Completeness, checked over the MERGED result rather than per page — a
    # per-page check cannot see a gap BETWEEN two pages, which is exactly
    # where a short page hides.
    #
    # NOT "pages x rows-per-page". This site's category page size is steady
    # at 60, but a search page's row count depends on how far the scroll got
    # before it settled, and the LAST page of any listing is legitimately
    # short — so multiplying the fullest page by the page count would warn
    # on healthy runs, and a threshold that fires on every healthy run
    # teaches the reader to ignore it.
    #
    # What is worth warning about is a page that came back materially THIN
    # against its siblings — that is what a truncated response or a
    # half-painted grid looks like. A page holding less than 60% of the
    # fullest page is well outside the +-3% spread that the varying page size
    # accounts for.
    total_available = next((o.total_available for o in outcomes
                            if o.total_available is not None), None)
    if args.mode == "listing" and all_rows:
        counts = [(o.page_num, len(o.products)) for o in outcomes if o.ok]
        fullest = max((n for _, n in counts), default=0)
        thin = [(p, n) for p, n in counts
                if fullest and n < THIN_PAGE_SHARE * fullest]
        # The LAST page of a listing is legitimately short — the catalogue
        # simply ran out — so it is excluded unless there are pages after it.
        last_page = max((p for p, _ in counts), default=0)
        thin = [(p, n) for p, n in thin if p != last_page]
        if thin:
            logger.warning(
                "Page(s) %s came back much thinner than the fullest page "
                "(%d rows): %s. A truncated response or a half-painted grid "
                "looks like this — re-run with --dump-html to check the "
                "snapshot for those pages.",
                ", ".join(str(p) for p, _ in thin), fullest,
                ", ".join("page %d: %d" % (p, n) for p, n in thin))
        if total_available:
            logger.info("This listing holds %d product(s) in total; this run "
                        "took %d (%.1f%%).", total_available, len(all_rows),
                        100.0 * len(all_rows) / total_available)
            # The pages the CATALOGUE has that the site will NOT address. A
            # run that stops at the cap is complete as far as the site is
            # concerned and truncated as far as the catalogue is, and only
            # saying so lets a consumer tell the two apart. Computed from the
            # total the payload already stated rather than from another fetch.
            # `fullest` is this run's own observed page size, which is what
            # the site actually served rather than a number hardcoded here.
            beyond = (max(0, -(-total_available // fullest) - parser_page_cap)
                      if fullest else 0)
            if beyond:
                logger.warning(
                    "This listing is %d page(s) deeper than the site will "
                    "address: it caps ?page= at %d, and a request past that "
                    "returns the capped page's own lots under HTTP 200 rather "
                    "than failing. Narrow the listing with the site's own "
                    "filters, or walk the auctions index instead - an auction "
                    "page carries every one of its lots at once.",
                    beyond, parser_page_cap)

    ok_pages = [o for o in outcomes if o.ok]
    failed_pages = [o.page_num for o in outcomes if not o.ok]
    final_url = (max(ok_pages, key=lambda o: o.page_num).final_url
                 if ok_pages else args.url)

    # One-per-run context, in the sidecar rather than repeated down a column.
    #
    # In --mode product that is the seller's own id and name, off the page's
    # Apollo cache. In --mode listing it is the scroll trace and the page's
    # own result header: both say how much of the listing this run actually
    # saw, which is the question a consumer of an infinitely-scrolling site
    # most needs answered and which no column can carry.
    extra = None
    if args.mode == "lot":
        first = next((o for o in outcomes if o.ok and o.shop_facts), None)
        if first is not None and first.shop_facts:
            extra = dict(first.shop_facts)
            logger.info("Shop: %s (id %s, /%s).",
                        extra.get("shop_name") or "?",
                        extra.get("shop_id") or "?",
                        extra.get("shop_slug") or "?")
    else:
        scrolls = {o.page_num: o.scroll for o in outcomes if o.scroll}
        headers = {o.page_num: o.header for o in outcomes if o.header}
        unsettled = sorted(n for n, s in scrolls.items()
                           if s and not s.get("settled"))
        if scrolls or headers:
            extra = {"scroll": scrolls, "result_header": headers,
                     "pages_still_growing": unsettled}
        if unsettled:
            logger.warning(
                "Page(s) %s were still loading more products when the scroll "
                "budget ran out, so their row counts are floors rather than "
                "the listing. Raise the budget or narrow the URL.",
                ", ".join(str(n) for n in unsettled))

    return finish_run(all_rows, args.out, args.format, args.allow_empty,
                      blocked=blocked, stop_reason=stop_reason,
                      pages_requested=args.pages, pages_completed=len(ok_pages),
                      pages_failed=failed_pages, mode=args.mode,
                      source=site_host(final_url),
                      start_url=args.url, final_url=final_url,
                      extra=extra)


def parse_args():
    p = argparse.ArgumentParser(
        description="Catawiki auction scraper (Playwright edition)")
    p.add_argument("--url", default=None,
                   help="A Catawiki URL: a category listing "
                        "(/search?st=product&q=...), a category listing "
                        "(/p/<cat>/<sub>[/<subsub>]) or one product "
                        "(/{shop}/{slug}) with --mode product. One "
                        "storefront, one language, one currency — there is "
                        "no locale prefix and no per-country host. "
                        "Required, unless CATAWIKI_URL is set in the environment "
                        "or in .env.")
    p.add_argument("--mode", choices=["listing", "lot", "auctions"],
                   default="listing",
                   help="listing (default): a search grid "
                        "(/search?st=product&q=...) or a category listing "
                        "(/p/<cat>/<sub>/<subsub>) — 60 products per batch. "
                        "product: one /{shop}/{slug} page, read from the "
                        "page's own Apollo cache, which adds the site's real "
                        "product id, the EXACT sold count, the review count, "
                        "the condition, the shipping weight and the "
                        "category's own listing URL. --pages applies to "
                        "listing only; there is one page to read in product "
                        "mode. There is deliberately no shop mode: a "
                        "shop front is a different application "
                        "shell whose markup has not been measured, and a mode "
                        "that ships untested is worse than one that is "
                        "absent.")
    p.add_argument("--category", default=None,
                   help="Label to tag output rows with. Defaults to the "
                        "category leaf from a /c/ URL, the search query from a "
                        "/search URL, and the breadcrumb in --mode product, so "
                        "the column is rarely empty just because the flag was "
                        "omitted. A shop front spans categories and gets none, "
                        "so pass one if you want that column filled.")
    p.add_argument("--pages", type=int, default=1,
                   help="Number of listing pages to crawl. Applies to "
                        "--mode listing; ignored in --mode product, which "
                        "has one page. Note that only a CATEGORY URL has "
                        "per-page addresses: a search is one infinitely "
                        "scrolling page, so --pages above 1 there is honoured "
                        "by scrolling further rather than by fetching more "
                        "URLs.")
    p.add_argument("--delay", type=float, default=2.0, help="Delay between pages, seconds")
    p.add_argument("--concurrency", type=int, default=1, metavar="N",
                   help="Fetch pages through N parallel workers (default 1 — "
                        "unchanged sequential behaviour). Each worker runs its "
                        "own browser and holds its own proxy exit, so N>1 "
                        "without --proxy-file just sends N times the traffic "
                        "from one address. Ignored with --cdp-endpoint.")
    p.add_argument("--retries", type=int, default=3,
                   help="Attempts per page load before giving up (default 3). "
                        "The pause between attempts doubles each time. A page "
                        "that comes back EMPTY is not retried — see "
                        "page_flow.STATE_POLICY — because an empty hub "
                        "category is a correct answer, not a fault.")
    p.add_argument("--retry-delay", type=float, default=2.0,
                   help="Seconds before the first page-load retry, doubling "
                        "thereafter (default 2.0)")
    p.add_argument("--format", choices=["json", "csv", "both"], default="both")
    p.add_argument("--out", default="catawiki_products", help="Output file prefix")
    p.add_argument("--locale", default="id-ID",
                   help="Browser locale (default id-ID, the site's only "
                        "one). It does NOT decide the language or the "
                        "currency: this site serves euro on all 18 locales and "
                        "IDR prices to an Indonesian and a US exit alike, so "
                        "this only affects what the browser claims about "
                        "itself.")
    p.add_argument("--proxy", default=None,
                   help="Proxy URL, e.g. http://ACCOUNT:PASSWORD@HOST:9999 "
                        "(2captcha.com/proxy)")
    p.add_argument("--proxy-file", default=None,
                   help="File with one proxy URL per line (# comments and blank "
                        "lines skipped) to rotate across. Wins over --proxy.")
    p.add_argument("--proxy-rotate", choices=list(ROTATE_MODES), default="per-run",
                   help="per-run (default): one exit for the whole run. per-page: "
                        "a new exit for every page — this is what spreads volume, "
                        "and it relaunches the browser each time so the session "
                        "does not follow the IP around.")
    p.add_argument("--proxy-shuffle", action="store_true",
                   help="Shuffle the pool at startup, so concurrent runs do not "
                        "all begin on the first exit in the file.")
    p.add_argument("--proxy-block-retries", type=int, default=2,
                   help="When a page comes back refused (HTTP 403) or behind "
                        "a captcha, retry it from this many OTHER exits before "
                        "giving up (default 2). Needs a pool of more than one; "
                        "ignored otherwise. This is the flag that matters most "
                        "on this site: the refusal is a property of the "
                        "ADDRESS, and a different exit is what clears it.")
    p.add_argument("--twocaptcha-key", default=None, help="2captcha.com API key")
    p.add_argument("--allow-empty", action="store_true",
                   help="Write output files even when 0 rows were found. Off by "
                        "default so a failed run can't overwrite a good result "
                        "with an empty one; exit code is 4 either way.")
    p.add_argument("--fingerprint", action="store_true",
                   help="Fetch a browser fingerprint from 2captcha's Fingerprint "
                        "API and apply it to the launched browser. Needs "
                        "--twocaptcha-key. Ignored with --cdp-endpoint, where the "
                        "Scraping Browser supplies its own.")
    # ONE OS-family tag, not a list — and the default is what makes
    # --fingerprint work at all. It shipped as "Windows,Chrome,Desktop" in
    # this family, which the API rejects with HTTP 400 ("Request parameters
    # are invalid"), so --fingerprint failed on every invocation. Measured
    # 2026-09-10: `Windows` succeeds, and `Windows,Chrome,Desktop`, `Chrome`
    # and `Desktop` each 400. fingerprint_client.py's own --tags help has
    # said so all along; the engines' default contradicted it.
    p.add_argument("--fp-tags", default="Windows",
                   help="ONE OS-family tag for the fingerprint filter: "
                        "Windows, Microsoft Windows or Android. NOT a list — "
                        "Chrome, Desktop and Mobile are each rejected by the "
                        "API with 400, and no combination is accepted. Use "
                        "--fp-country to narrow further. (default: Windows)")
    p.add_argument("--fp-country", default=None,
                   help="Fingerprint country, ISO 3166-1 alpha-2. Match it to "
                        "your proxy's exit country — a US fingerprint on a "
                        "German IP is a contradiction.")
    p.add_argument("--captcha-api", choices=["v2", "v1"], default="v2",
                   help="Which 2captcha solver API to use. v2 is the current "
                        "JSON API (api.2captcha.com/createTask); v1 is the "
                        "legacy in.php/res.php pair. Applies to both the image "
                        "captcha and reCAPTCHA.")
    p.add_argument("--solve-captcha", choices=["when-blocked", "always"],
                   default="when-blocked",
                   help="when-blocked (default): only pay to solve a "
                        "reCAPTCHA if the content is not already readable. "
                        "always: solve whenever one is detected. Neither "
                        "setting touches this site's refusal, which is not a "
                        "page at all — the HTTP/2 stream is reset and nothing "
                        "arrives — so no solve helps there and none is "
                        "attempted or billed. In fact NO challenge has ever "
                        "been observed on this site; the path is wired up "
                        "because a bot manager can be switched on between "
                        "deploys.")
    p.add_argument("--min-score", type=float, default=0.7,
                   help="reCAPTCHA v3 minimum score to request (0.3, 0.7 or 0.9 "
                        "— the API only accepts these three). Ignored for v2 "
                        "widgets.")
    p.add_argument("--cdp-endpoint", default=None,
                   help="Connect to an already-running browser over CDP instead "
                        "of launching Playwright's bundled Chromium, e.g. "
                        "ws://user:pass@host:port — the Scraping Browser API "
                        "endpoint, or any browser that exposes a CDP URL. "
                        "--proxy and --headless/--headful are ignored when this "
                        "is set.")
    p.add_argument("--dump-html", default=None, metavar="PATH",
                   help="Save the exact HTML the parser is given, on success as "
                        "well as failure. Useful when the row count is right but "
                        "a column comes back empty — see TROUBLESHOOTING.md.")
    # HEADFUL by default, a departure from every sibling repo and a measured
    # one: Akamai refuses a headless browser here whatever the exit -- HTTP
    # 403 and a 394-byte "Access Denied" from four residential exits and one
    # datacentre address, against 200 and the full catalogue from those very
    # same addresses with a real window. A --headless default would be a
    # scraper whose default cannot fetch the site.
    p.add_argument("--headful", dest="headless", action="store_false",
                   default=False,
                   help="Run with a real browser window. THE DEFAULT here, "
                        "because this site refuses a headless browser "
                        "regardless of the proxy - see README.")
    p.add_argument("--headless", dest="headless", action="store_true",
                   help="Run headless. Measured to be refused with HTTP 403 "
                        "from every address tried, residential included. Kept "
                        "because --cdp-endpoint ignores it and because a "
                        "change on the site would make it work again.")
    args = p.parse_args()
    # Fill --twocaptcha-key / --cdp-endpoint / --proxy / --url from the
    # environment or .env when the flag was not given. An explicit flag wins.
    env_config.apply(args)
    if not args.url:
        p.error("no --url given, and CATAWIKI_URL is not set in the "
                "environment or in .env.")
    if not is_supported_host(args.url):
        # Refused rather than attempted. The parser's grid containers, its
        # product-path pattern and its pagination convention are all
        # this site's, so pointing this at another marketplace would not fail
        # loudly — it would return zero rows and look like an empty category.
        why = unsupported_reason(args.url)
        if why:
            p.error(f"{site_host(args.url)} {why}.")
        p.error(f"{site_host(args.url) or args.url!r} is not Catawiki. This "
                f"scraper reads catawiki.com, which is the site's only "
                f"storefront — one language, one currency, no per-country "
                f"hostname and no locale prefix.")
    kind = listing_kind(args.url)
    if args.mode == "lot" and kind != "lot":
        p.error(f"--mode product expects a /{{shop}}/{{slug}} product URL; "
                f"{args.url!r} is a {kind} page.")
    if args.mode == "listing" and kind == "lot":
        p.error(f"{args.url!r} is a single product page. Use --mode product "
                f"for it, or pass a /search?st=product&q=... or "
                f"/p/<cat>/<sub>/<subsub> URL.")
    if args.mode == "listing" and kind == "hub":
        # Said as a warning rather than an error: it IS a Catawiki URL and
        # the run will honestly report zero rows (exit 4). But a reader who
        # tries the obvious category URL first would otherwise conclude the
        # tool is broken, so name what happened.
        logger.warning(
            "%s is a /p/<slug> DISCOVERY HUB, not a listing. It has no "
            "product grid on it — only banners and recommendation carousels "
            "— so this run will return 0 rows and exit 4. The real listings "
            "are one or two levels down: /p/<cat>/<sub>[/<subsub>], e.g. "
            "/p/makanan-minuman/minuman/kopi-bubuk. A product page's own "
            "breadcrumb names its category's listing URL.", args.url)
    if args.mode in ("lot", "auctions") and args.pages != 1:
        # Said out loud rather than silently ignored: a user who passed
        # --pages 5 expects five pages of something.
        logger.warning("--pages %d is ignored in --mode %s: there is one page "
                       "to read. The run status will say single_page_mode.",
                       args.pages, args.mode)
        args.pages = 1
    return args


if __name__ == "__main__":
    args = parse_args()
    if args.fingerprint and not args.twocaptcha_key:
        logger.error("--fingerprint needs --twocaptcha-key (the Fingerprint API "
                     "uses the same key, though it's a separate subscription "
                     "from solving).")
        sys.exit(2)
    if args.fingerprint and args.cdp_endpoint:
        logger.warning("--fingerprint is ignored with --cdp-endpoint: the "
                       "Scraping Browser supplies its own fingerprint, and "
                       "stacking a second one on top creates a mismatch rather "
                       "than better cover.")
    try:
        sys.exit(scrape(args))
    except ProxyError as e:
        # Bad usage, not a crash: a typo in a proxy list would otherwise
        # surface as a connection failure on page 1 with nothing naming it.
        logger.error("%s", e)
        sys.exit(2)
    except PWError as e:
        # A remote browser that will not accept the connection is a REMOTE
        # API failure (exit 5), not a crash in this code (exit 1) and not bad
        # usage (exit 2). The distinction earns its keep on the commonest one:
        # `profile_locked` means another run still holds this `pid`, and a
        # harness that sees exit 1 goes looking for a bug in the scraper
        # instead of waiting or passing a different pid.
        text = _mask_credentials(str(e))
        if "profile_locked" in text or "connect to --cdp-endpoint" in text:
            logger.error("%s", text)
            sys.exit(EXIT_API_ERROR)
        raise
