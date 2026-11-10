"""page_flow.py — what to do with the page Catawiki just gave us.

Catawiki answers a request five ways, and four of them want a different
response, which is why this module exists rather than the same triage being
written three times inside three engines and drifting apart:

    content    the site served its listing payload
    empty      a search that matched nothing -- and it comes with 24
               suggested lots in the grid, so this state must NOT be parsed
    shell      served, built from the site's own assets, payload not there
               yet. Wants a WAIT, not a refetch
    challenge  something solvable was rendered
    blocked    HTTP 403 from Akamai's edge with a 394-byte "Access Denied"

The policy lives in `STATE_POLICY` as DATA, so an engine cannot quietly
disagree with its twins about whether a page is worth retrying or worth
paying for.

Everything here is pure or driven through small callables, so each engine
passes its own driver's primitives and keeps its browser plumbing to itself:

    count(selector) -> int          how many elements match
    content() -> Optional[str]      current HTML, None if unavailable
    sleep(ms) -> None               wait

No JavaScript crosses that boundary in either direction: Selenium's
`execute_script` takes a function BODY with an explicit `return` where
Playwright and pyppeteer take `() => expr`, so the shared module names the
OPERATION and the engine spells it in its own driver's dialect.

This module has NO scroll loop, and that is measured rather than assumed
-----------------------------------------------------------------------
Three scrolls to `document.body.scrollHeight` added exactly zero cards and
did not change the page height on any of the three page kinds (24 -> 24 on a
category listing, 24 -> 24 on a search grid, 40 -> 40 on a lot page). Every
listing page holds its whole 24 lots and paginates by URL, so a scroll here
would be latency bought for nothing.
"""

from __future__ import annotations

import argparse
import logging
import re
import time
from typing import Callable, Dict, Iterable, List, Optional, Union
from urllib.parse import urljoin, urlsplit

from product_parser import (auction_links, detect_block_marker,
                            detect_bot_challenge, detect_page_state,
                            listing_kind, lots_payload, next_data, page_url,
                            page_number_from_url, paginates_by_url,
                            served_by_catawiki, strip_tracking, total_pages,
                            PAGE_CAP)

logger = logging.getLogger("page_flow")

# Below this a document with no reference to the site's assets is an empty
# DOM (a bare <html><head></head><body></body></html> is 39 bytes), not the
# refusal page, which is 300+ bytes.
EMPTY_DOM_BYTES = 100


# ---------------------------------------------------------------------------
# Readiness
# ---------------------------------------------------------------------------
# The trap this site sets, and the reason the anchor is not a card count.
#
# Both listing kinds are SERVER-RENDERED, but what the server renders is the
# card's SHELL: the first response carries 24 `c-lot-card__container` nodes
# and 24 `c-lot-card__price` nodes, and every one of those price nodes is
# EMPTY (measured on the raw response body, before any script ran: 0 of 24
# non-empty, no status labels, no favourite counts). The money, the bid state
# and the favourite count all arrive at hydration.
#
# So a readiness wait on the card count is satisfied instantly, at 24 out of
# 24, while every price on the page is still blank -- "a selector
# matching the WRONG element is worse than one matching nothing", in the
# shape it takes here. The anchor is therefore a price node WITH SOMETHING IN
# IT.
#
# Rows survive either way: title, url, sku, images, auction id and the
# reserve flag all come out of the SSR payload with no JavaScript at all (24
# rows parsed from the raw response, every one of them `price_source:
# next_data`). A timed-out wait costs the prices, not the run.
READY_SELECTOR_LISTING = ".c-lot-card__price:not(:empty)"

# A lot page needs NO wait: everything a row is built from -- the amount, the
# absolute open/close times, the bid history, the sold and reserve flags, the
# estimate, the seller, the specifications -- is in the first response's
# payload, and the DOM is deliberately not read there at all (a lot page
# renders 20-40 OTHER lots' prices in the same class a listing uses for its
# own). The selector is kept only so an engine has something to hand
# `wait_for_count` when it wants to know the page painted.
READY_SELECTOR_LOT = '[class*="LotBidStatusSection_container"], h1'
READY_SELECTOR_AUCTIONS = 'a[href*="/a/"]'

# Prices fill in ALL AT ONCE rather than trickling: 0 non-empty at 1.1s,
# 1.4s, 1.8s and 2.1s, then 24 of 24 at 2.4s (category), 1.9s (search) and
# 2.4s (category page 2). So the threshold does not need to chase a partial
# fill -- it needs to be above 1, because waiting for a single match
# resolves on an unrelated node.
MIN_CARD_MATCHES = 8
MIN_CARD_MATCHES_LOT = 1
MIN_CARD_MATCHES_AUCTIONS = 4

# Generous against the measured 2.4s, because a residential exit and a cold
# cache are both slower than this laptop, and the cost of waiting too long is
# latency where the cost of waiting too little is a run with no prices in it.
CONTENT_TIMEOUT_MS = 20_000
CONTENT_TIMEOUT_MS_LOT = 15_000

_READY = {
    "listing": READY_SELECTOR_LISTING,
    "lot": READY_SELECTOR_LOT,
    "auctions": READY_SELECTOR_AUCTIONS,
}
_MIN = {
    "listing": MIN_CARD_MATCHES,
    "lot": MIN_CARD_MATCHES_LOT,
    "auctions": MIN_CARD_MATCHES_AUCTIONS,
}


def ready_selector(mode: str) -> str:
    return _READY.get(mode, READY_SELECTOR_LISTING)


def min_matches(mode: str, expected: Optional[int] = None) -> int:
    """How many matches mean "painted".

    `expected` is the lot count the payload itself states, and passing it is
    what keeps a short last page from timing out: a category whose final page
    holds 5 lots can never reach 8, and waiting for 8 there would spend the
    whole timeout and then report the page unpainted when it was finished.
    """
    floor = _MIN.get(mode, MIN_CARD_MATCHES)
    if expected is None or expected <= 0:
        return floor
    return max(1, min(floor, expected))


def content_timeout_ms(mode: str) -> int:
    return CONTENT_TIMEOUT_MS_LOT if mode == "lot" else CONTENT_TIMEOUT_MS


def expected_lots(html: Optional[str]) -> Optional[int]:
    """How many lots this page's own payload says it holds."""
    _, payload = lots_payload(next_data(html or ""))
    lots = payload.get("lots")
    return len(lots) if isinstance(lots, list) else None


def wait_for_count(count: Callable[[str], int], sleep: Callable[[int], None],
                   selector: str, minimum: int, timeout_ms: int,
                   poll_ms: int = 250) -> int:
    """Poll `selector` until `minimum` elements match, or the budget runs out.

    Polls a COUNT rather than waiting on an evaluated string. Playwright's
    `wait_for_function` hands the browser a string to evaluate, which a site
    whose CSP lacks `unsafe-eval` refuses outright -- the run
    dies with `EvalError` and exit 1.
    Catawiki's CSP does allow `unsafe-eval` today (checked on all three page
    kinds: `script-src 'self' 'unsafe-eval' ...`), so this is not a bug being
    worked around here -- it is one not being invited back in when a header
    changes.

    Returns the last count seen, so a caller can tell "painted" from
    "timed out with three of them".
    """
    waited = 0
    seen = count(selector)
    while seen < minimum and waited < timeout_ms:
        sleep(poll_ms)
        waited += poll_ms
        seen = count(selector)
    if seen < minimum:
        logger.info("readiness wait ended at %d/%d matches for %s after %dms",
                    seen, minimum, selector, waited)
    return seen


# ---------------------------------------------------------------------------
# Classification and the policy that follows from it
# ---------------------------------------------------------------------------
def classify(html: Optional[str], status: Optional[int] = None,
             url: str = "") -> str:
    """Which of the five states this response is.

    `status` is positional and comes SECOND, matching `detect_page_state`.
    Getting that wrong is not a style question: an engine calling this as
    `classify(html, url=...)` crashes on its first fetch, and nothing short of
    a live run or a signature-binding check sees it.
    """
    if html is None:
        # A bare navigation failure. On this site that IS what a refusal
        # looks like from a headless browser, so it is reported rather than
        # raised.
        return "blocked"
    return detect_page_state(html, status, url)


# The retry/solve/blocked decision as DATA rather than as three copies of an
# if-chain in three engines.
#
#   parse    is there anything on this page worth writing down?
#   retry    would fetching it again, from a different exit, plausibly help?
#   solve    is there something to pay a solver for?
#   blocked  does this count towards exit 3?
STATE_POLICY: Dict[str, Dict[str, bool]] = {
    "content":   {"parse": True,  "retry": False, "solve": False, "blocked": False},
    # NOT parsed, and this row is deliberate. A Catawiki search that matches nothing answers HTTP 200,
    # prints "No results", and BACKFILLS the grid with 24 suggested lots that
    # its payload reports as `total: 24`. Parsing an `empty` page here would
    # write two dozen plausible, well-formed rows for a query that matched
    # nothing at all -- which is worse than writing none, because nothing
    # downstream can tell them from real ones.
    "empty":     {"parse": False, "retry": False, "solve": False, "blocked": False},
    # Served, and still painting. Wants the readiness wait, not another
    # fetch: refetching a shell just buys another shell.
    "shell":     {"parse": True,  "retry": False, "solve": False, "blocked": False},
    "challenge": {"parse": False, "retry": True,  "solve": True,  "blocked": False},
    "blocked":   {"parse": False, "retry": True,  "solve": False, "blocked": True},
}


def should_parse(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY["blocked"])["parse"]


def should_retry(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY["blocked"])["retry"]


def should_solve(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY["blocked"])["solve"]


def counts_as_blocked(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY["blocked"])["blocked"]


def is_unpainted(state: str, html: Optional[str]) -> bool:
    """Whether this page is served but has not filled its cards in yet.

    Distinguished from a refusal by the card SHELLS, which the server sends
    even when every price in them is blank.
    """
    if state != "shell":
        return False
    return served_by_catawiki(html or "")


# Rotating to a different exit does NOT clear a block on this site, and that
# is measured rather than assumed: a headless browser was refused with HTTP
# 403 from five different addresses -- four residential Dutch exits
# (84.25.177.161, 212.127.205.115, 82.168.162.97 and one more) and a
# hosting-classified one -- while a HEADFUL browser was served 200 with the
# full catalogue from the very same addresses. The discriminator is the
# client, not the address.
#
# So the retry budget for a block is spent on a change that cannot help. It
# is deliberately 0 here, and the engines
# read this constant rather than computing their own budget -- a policy
# constant nothing consults is the same defect as dead code.
RETRY_ON_BLOCKED = False
BLOCK_RETRIES_WITHOUT_POOL = 0
BLOCK_RETRIES_WITH_POOL = 0

# One solve per page. Detection is broad on purpose, but a second solve on
# the same page has never been the answer to the first one failing.
SOLVES_PER_PAGE = 1


class SolveBudget:
    """The one per-page allowance every paid solve goes through.

    Each engine runs its captcha handler twice per fetch attempt: once before
    the page is classified (so a challenge is cleared before anything is
    judged) and once after, for the state that says the page really is
    gated. Before this class only the second call was counted, so one page
    could buy two solves against a limit of one, and more on every block
    retry. Both call sites now hand the handler the SAME budget, and the
    handler asks it immediately before buying a token — so a page with no
    captcha on it spends nothing, and a page with one cannot buy more than
    `limit`, whichever call site reaches it.

    One budget per PAGE, created before the block-retry loop: it is not reset
    by a retry or by an exit rotation, because a fresh exit is a reason to
    re-fetch, not a fresh allowance to spend.

    Driver-free on purpose, so the three engines share one implementation
    rather than three counters that can drift.
    """

    def __init__(self, limit: int = SOLVES_PER_PAGE):
        self.limit = limit
        self.spent = 0

    def allows(self) -> bool:
        return self.spent < self.limit

    def take(self) -> bool:
        """Claim one purchase. Call immediately BEFORE buying a token; False
        means do not buy."""
        if self.spent >= self.limit:
            return False
        self.spent += 1
        return True


def block_advice(html: Optional[str], headless: bool,
                 has_pool: bool) -> str:
    """What a reader should actually DO about this block.

    Exists because the honest answer on this site is almost never "get a
    better proxy", and a message that says so saves someone an afternoon and
    a proxy bill.
    """
    marker = detect_block_marker(html or "") or "no marker"
    if headless:
        return (f"blocked ({marker}). Akamai refuses a HEADLESS browser here "
                f"regardless of the exit address -- measured 403 from four "
                f"residential exits and one datacentre one, and 200 from the "
                f"same addresses with a real window. Run --headful, or use "
                f"--cdp-endpoint (the Scraping Browser is served normally).")
    if not has_pool:
        return (f"blocked ({marker}) with a headful browser and no proxy "
                f"pool. This address may be scored; try --proxy or "
                f"--cdp-endpoint.")
    return (f"blocked ({marker}) with a headful browser and a proxy pool. "
            f"Rotation is unlikely to help on this site; --cdp-endpoint is "
            f"the path measured to work.")


def not_served_message(html: Optional[str], headless: bool, has_pool: bool,
                      tries: int, debug_html: str) -> str:
    """The log line for a page the site did not serve (exit 3).

    One text for all three engines, built from the same facts as
    `block_advice`. It states what was OBSERVED -- the byte count and whether
    the document references the site's own assets -- and only then what that
    usually means here. A document of a few dozen bytes is an EMPTY DOM, not
    Akamai's refusal (a 394-byte "Access Denied"), and no HTTP status
    reaches this line, so it must not claim the site refused anything.
    """
    n = len(html or "")
    served = served_by_catawiki(html or "")
    seen = (f"The site did not serve this request: {n} bytes, "
            f"{'which references' if served else 'with no reference to'} "
            f"the site's own asset host, saved to {debug_html}.")
    if n < EMPTY_DOM_BYTES and not served:
        what = (f" That is an empty document, not a refusal page (Akamai's "
                f"is a 394-byte \"Access Denied\"), so the navigation most "
                f"likely never completed: check the network and that "
                f"--retries is at least 1.")
        if headless:
            what += (" This run was HEADLESS, which this site refuses "
                     "whatever the address; --headful is the default.")
    elif headless:
        what = (" This run was HEADLESS, and Akamai refuses a headless "
                "browser here regardless of the exit address. Run "
                "--headful, or use --cdp-endpoint.")
    else:
        what = (" This run was headful, so the cause is not the headless "
                "flag; the exit address may be scored. "
                + ("Rotation is unlikely to help here; " if has_pool else "")
                + "--cdp-endpoint is the path measured to work.")
    return (f"{seen}{what} Tried {tries} time(s). This is exit 3, distinct "
            f"from a genuinely empty result (exit 4).")


def attempts(value: str) -> int:
    """argparse type for --retries: attempts per page load, so at least 1.

    The loop is `range(1, retries + 1)`. Zero ran it NEVER, `goto` was not
    called, and the engine judged about:blank -- an empty DOM reported as
    "blocked" (exit 3) for a site that was never asked.
    """
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not an integer")
    if n < 1:
        raise argparse.ArgumentTypeError(
            "must be at least 1: it counts attempts per page load, and 0 "
            "would attempt none and judge a page that was never fetched")
    return n


def positive_int(value: str) -> int:
    """argparse type for a count that must be at least 1 (--pages,
    --concurrency). Zero pages or zero workers would fetch nothing and then
    report on a run that never asked the site anything."""
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not an integer")
    if n < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1, got {n}")
    return n


def non_negative_int(value: str) -> int:
    """argparse type for a count of EXTRA tries (--proxy-block-retries,
    the Scraper API's --retries), where 0 is legal and a negative is not."""
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not an integer")
    if n < 0:
        raise argparse.ArgumentTypeError(f"must be 0 or more, got {n}")
    return n


def non_negative_float(value: str) -> float:
    """argparse type for a pause in seconds (--delay, --retry-delay). A
    negative pause makes `time.sleep` raise mid-run, after pages were
    already fetched."""
    try:
        x = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a number")
    if not x >= 0:  # also rejects nan
        raise argparse.ArgumentTypeError(f"must be 0 or more, got {value}")
    return x


def mode_url_mismatch(mode: str, url: str, kind: str) -> Optional[str]:
    """The usage error for a --mode that cannot read this URL, or None."""
    if mode == "lot" and kind != "lot":
        return (f"--mode lot expects a /{{loc}}/l/{{id}}-{{slug}} lot URL; "
                f"{url!r} is a {kind or 'unrecognised'} page.")
    if mode == "auctions" and kind != "auctions":
        return (f"--mode auctions reads the auctions INDEX (/{{loc}}/a), one "
                f"row per auction; {url!r} is a {kind or 'unrecognised'} "
                f"page. To read one auction's lots use the default "
                f"--mode listing on its /{{loc}}/a/{{id}}-{{slug}} URL.")
    if mode == "listing" and kind == "lot":
        return (f"{url!r} is a single lot page. Use --mode lot for it, or "
                f"pass a category (/{{loc}}/c/{{id}}-{{slug}}), a search "
                f"(/{{loc}}/s?q=...) or an auction (/{{loc}}/a/{{id}}-{{slug}}) "
                f"URL.")
    return None


URL_HELP = ("A Catawiki URL: a category /{loc}/c/{id}-{slug}, a search "
            "/{loc}/s?q=..., an auction /{loc}/a/{id}-{slug}, the auctions "
            "index /{loc}/a (--mode auctions), or one lot "
            "/{loc}/l/{id}-{slug} (--mode lot). Required, unless "
            "CATAWIKI_URL is set in the environment or in .env.")
MODE_HELP = ("listing (default): a category, a search or a whole auction, "
             "24 lots per page on the first two. lot: one lot page, adding "
             "the estimate, the seller and their feedback score, the "
             "specifications and the bid history. auctions: the auctions "
             "index as a work list, one row per auction, to feed back in "
             "as --url.")
def check_out_prefix(prefix: str) -> Optional[str]:
    """The usage error for an unwritable `--out`, or None.

    Checked BEFORE the browser starts: the folder is only touched at the very
    end of a run, so a missing one used to cost a whole run's rows.
    """
    import os
    folder = os.path.dirname(os.path.abspath(prefix))
    if not os.path.isdir(folder):
        return (f"--out {prefix!r}: the folder {folder!r} does not exist. "
                f"Create it first; nothing has been fetched.")
    if not os.access(folder, os.W_OK):
        return f"--out {prefix!r}: the folder {folder!r} is not writable."
    return None


PAGES_HELP = ("Listing pages to crawl (--mode listing on a category or a "
              "search; ?page=N, capped by the site at 100). Ignored in "
              "--mode lot and --mode auctions, and for a whole auction, "
              "which is one page.")
RETRIES_HELP = ("Attempts per page load before giving up (default 3, "
                "minimum 1). The pause between attempts doubles each time. "
                "A page that comes back EMPTY is not retried: an empty "
                "listing is a correct answer, not a fault.")


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------
# Three layers, weakest last, and on this site the strongest one is
# arithmetic rather than a link: page 1's payload states `total` and
# `lotsPerPage`, so every page's address is knowable at once and workers can
# be handed independent pages without following a chain.
#
# The site publishes NO `link[rel="next"]` at all (checked: 0 on every
# capture), so the selector layer here is genuinely the last resort it is
# supposed to be, and it is anchored on the page parameter rather than on a
# build-hash class.
NEXT_PAGE_SELECTOR = (
    'a[href*="page="][rel="next"], '
    'nav a[href*="page="], '
    'a[href*="page="]'
)


def next_page_selector(page_num: int = 1) -> str:
    return NEXT_PAGE_SELECTOR


def pagination_is_addressable(page1_url: str,
                              next_href: Optional[str] = None) -> bool:
    """Whether page N can be fetched without walking pages 2..N-1.

    True for both listing kinds: `?page=N` is the site's own convention, page
    1's payload states the total, and a fetched `?page=2` came back with
    `currentPage: 2` -- the convention and the site agree, which has to hold
    before planning page URLs up front.
    """
    if not paginates_by_url(page1_url):
        return False
    if next_href and "page=" not in next_href:
        # The site's own next link does not spell what the convention builds.
        # Chain link-to-link rather than guessing.
        return False
    return True


def _resolve(base_url: str, href: str) -> str:
    """A possibly-relative href as an absolute URL.

    Load-bearing, not tidiness: the site's own pagination links are RELATIVE
    (`/en/c/333-watches?page=2#filters`), so comparing one against a built
    absolute URL without resolving it first makes every comparison fail. That
    reads as "the site's link disagrees with the convention", which sends the
    caller down the link-chaining path and quietly costs it `--concurrency`
    -- while looking like a deliberate safety decision.
    """
    return urljoin(base_url, href) if href else href


def _as_hrefs(next_href: Union[str, Iterable[str], None]) -> List[str]:
    """One href or many, always as a list.

    Both spellings are real: an engine hands over EVERY advertised link so
    the filtering rule lives here instead of in three engines, while a test
    or a caller holding one link passes the string. Accepting only the string
    is what took the FIRST live run down -- `TypeError: Cannot mix str and
    non-str arguments`, from `urljoin` being handed a list -- and nothing
    short of running it saw that, because the offline checks called it the
    way its author was thinking. Run the code, not just the checks.
    """
    if next_href is None:
        return []
    if isinstance(next_href, str):
        return [next_href] if next_href else []
    return [h for h in next_href if isinstance(h, str) and h]


# A Scraping Browser profile allows ONE live connection. A run started right
# after another one has closed can reach the endpoint before the profile is
# released, and the service answers HTTP 500 for a few seconds (measured:
# every second back-to-back run failed without a pause, 3 of 3 succeeded with
# 15 s between them). Retrying the CONNECTION, with the reason in the log, is
# what the README promised for a held profile and the engines did not do.
CDP_CONNECT_ATTEMPTS = 4
CDP_CONNECT_PAUSE_S = 10.0
_PROFILE_BUSY_MARKERS = ("internal server error", "profile_locked",
                         "profile is locked")


def profile_busy(error: BaseException) -> bool:
    """Whether a CDP connect failure looks like a profile still in use."""
    text = str(error).lower()
    # A bare "500" would also match a port or a byte count; require it as a
    # whole word.
    return (any(marker in text for marker in _PROFILE_BUSY_MARKERS)
            or re.search(r"\b500\b", text) is not None)


def retry_while_profile_busy(connect: Callable, is_busy: Callable = profile_busy,
                             sleep: Callable = time.sleep,
                             attempts: int = CDP_CONNECT_ATTEMPTS,
                             pause_s: float = CDP_CONNECT_PAUSE_S):
    """Call `connect()`, retrying only a failure that looks like a held profile.

    Any other failure (bad credentials, a dead host) is raised at once: waiting
    cannot fix it. The last busy failure is raised after `attempts` tries.
    """
    for attempt in range(1, attempts + 1):
        try:
            return connect()
        except Exception as e:  # noqa: BLE001 - each engine raises its own types
            if attempt == attempts or not is_busy(e):
                raise
            logger.warning(
                "The remote browser refused the connection (%s) - the profile "
                "is most likely still held by the previous connection. "
                "Waiting %.0fs and trying again (%d/%d).",
                str(e).splitlines()[0][:80] if str(e) else type(e).__name__,
                pause_s, attempt, attempts - 1)
            sleep(pause_s)


def start_page(url: str) -> int:
    """The site page a run's URL starts on (1 when it names none)."""
    return page_number_from_url(url)


def site_page(start_url: str, ordinal: int) -> int:
    """The site's own page number of the run's `ordinal`-th page.

    A run counts its pages 1, 2, 3... from wherever the URL starts, and the
    site counts from 1 at the top of the listing. Every address, every
    "is this past the cap" test and the `page` column must use the SITE's
    number: using the run's own counter is what fetched page 3 after page 100
    when a run started on page 99.
    """
    return start_page(start_url) + ordinal - 1


def clamp_pages(url: str, pages: int):
    """(pages that can really be fetched from `url`, whether the site's cap cut it).

    The site addresses at most PAGE_CAP pages and answers a request past that
    with the capped page's own lots under HTTP 200, so a run that asks for more
    stops at the cap instead of asking. 0 means the start page is itself past
    the cap: nothing can be fetched.
    """
    if not paginates_by_url(url):
        return pages, False
    room = PAGE_CAP - start_page(url) + 1
    if room < 1:
        return 0, True
    return min(pages, room), pages > room


def planned_urls(base_url: str, start_url: str, pages: int) -> List[str]:
    """Addresses of the run's pages 2..`pages`, by the SITE's page number.

    `base_url` is the address page 1 actually loaded as (it may have been
    redirected), `start_url` is what the user typed and fixes the numbering.
    """
    return [page_url(base_url, site_page(start_url, n))
            for n in range(2, pages + 1)]


def apply_page_cap(args, parser) -> None:
    """Clamp `--pages` to what the site will address, once, at startup.

    A run that asked for more than the site serves used to plan page 101,
    get `None` for its address, crash, and lose every row it had gathered.
    It now stops at the cap, keeps them, and says so. A start page that is
    itself past the cap is a usage error: the site would answer it with page
    100's lots under HTTP 200.
    """
    args.pages_requested = args.pages
    args.pages_capped_by_site = False
    if args.mode != "listing":
        return
    pages, capped = clamp_pages(args.url, args.pages)
    if pages == 0:
        parser.error(
            f"--url starts on page {start_page(args.url)}, past the site's "
            f"cap of {PAGE_CAP}: a request that deep is answered with page "
            f"{PAGE_CAP}'s own lots. Start on page {PAGE_CAP} or earlier.")
    if capped:
        logger.warning(
            "--pages %d from page %d would run past the site's cap of %d "
            "pages; fetching pages %d-%d and stopping there. The run status "
            "records that the listing is longer than the site will address.",
            args.pages, start_page(args.url), PAGE_CAP,
            start_page(args.url), PAGE_CAP)
        args.pages = pages
        args.pages_capped_by_site = True


def finalize_args(args, parser) -> None:
    """Cross-flag checks every engine runs once, right after parsing."""
    error = check_out_prefix(args.out)
    if error:
        parser.error(error)
    apply_page_cap(args, parser)


def run_extra(args) -> dict:
    """Sidecar facts about the page window, shared by all three engines."""
    extra = {}
    if args.mode == "listing":
        extra["start_page"] = start_page(args.url)
        if getattr(args, "pages_capped_by_site", False):
            extra["pages_capped_by_site"] = True
            extra["pages_requested_by_user"] = args.pages_requested
    return extra


def next_url(start_url: str, current_url: str, ordinal: int) -> Optional[str]:
    """Address of the page after the run's `ordinal`-th page.

    Built from the run's start URL and the ordinal, not from whatever URL the
    browser ended up on: a redirect that drops `?page=` must not send the run
    back to page 2.
    """
    return page_url(current_url or start_url, site_page(start_url, ordinal + 1))


def empty_page_is_early(listing_pages: Optional[int], page_number: int) -> bool:
    """Whether an EMPTY page contradicts the listing's own stated size.

    `listing_pages` is the page count page 1's payload stated. A page inside
    it that came back with no rows is a glitch (one was served mid-run, on
    page 62 of 100, and the next page was full), not the end of the listing.
    Without a stated size there is nothing to contradict, so the empty page
    is taken as the end.
    """
    return listing_pages is not None and page_number <= listing_pages


def recheck_early_empty(args, page_num: int, outcome, listing_pages: Optional[int],
                        refetch: Callable):
    """(outcome, rechecked): fetch a suspiciously empty page once more.

    A page that comes back with no rows INSIDE the page count the listing
    itself stated is a site glitch, not the end of the results: one was served
    on page 62 of 100 with the next page full, and the run called that the end
    and reported itself complete with 1536 of 2400 rows. `refetch` asks for the
    page once more; if it is still empty the caller stops AND says so.
    """
    if listing_pages is None or not (outcome.ok and not outcome.products):
        return outcome, False
    site_n = site_page(args.url, page_num)
    if not empty_page_is_early(listing_pages, site_n):
        return outcome, False
    logger.warning("Page %d (site page %d) came back with no rows, but the "
                   "listing states it has %d page(s) — fetching it once more "
                   "before treating that as the end.", page_num, site_n,
                   listing_pages)
    return refetch(), True


def pagination_agrees(current_url: str, page_num: int,
                      next_href: Union[str, Iterable[str], None]) -> bool:
    """Whether the site's own next link matches what the convention builds.

    The page after `current_url` is read off the URL itself; `page_num` is
    kept for callers and no longer decides it (a run starting on page 99 has
    no page "1" to count from).

    True when ANY advertised link agrees. A listing page carries links to
    several of its own pages -- 2, 3 and the last -- and the question here is
    whether the CONVENTION is the site's, not whether every link happens to
    be page N+1.
    """
    hrefs = _as_hrefs(next_href)
    if not hrefs:
        return True
    want = page_url(current_url, page_number_from_url(current_url) + 1)
    if not want:
        return False
    target = comparable(want)
    return any(comparable(_resolve(current_url, h)) == target for h in hrefs)


def next_page_candidates(current_url: str,
                         next_href: Union[str, Iterable[str], None] = None
                         ) -> List[str]:
    """Addresses worth trying for the page after `current_url`, best first.

    Takes one href or the whole advertised set, and FILTERS it: a listing
    page links to plenty of other listings -- subcategories, related
    collections, the auctions index -- and chaining onto one of those returns
    rows from the wrong catalogue while reporting success.
    """
    page_num = page_number_from_url(current_url)
    out: List[str] = []
    built = page_url(current_url, page_num + 1)
    if built:
        out.append(built)
    for href in _as_hrefs(next_href):
        resolved = _resolve(current_url, href)
        known = {comparable(u) for u in out}
        # The cap is enforced on a link the SITE offered too, not only on one
        # this module built. Enforcing it in `page_url` alone left the
        # site-link path as a way around it -- and a request past the cap
        # does not fail, it returns page 100's own lots (measured:
        # `?page=99999` -> HTTP 200, `currentPage: 100`), so the run would
        # add no new sku, call the listing exhausted and report COMPLETE.
        # The number has to be the NEXT one, not merely a legal one. A
        # listing page advertises links to page 2, page 3 and the last page
        # at once, so accepting any of them as "the page after this" would
        # let page 100 be fetched as page 2 the moment the built URL failed.
        if (comparable(resolved) not in known
                and _same_listing(current_url, resolved)
                and page_number_from_url(resolved) == page_num + 1
                and page_num + 1 <= PAGE_CAP):
            out.append(resolved)
    return out


def _same_listing(current_url: str, candidate: str) -> bool:
    """Whether a candidate is another page of the SAME listing.

    A listing page links to plenty of other listings -- subcategories,
    related collections, the auctions index -- and following one of those
    would silently produce a run whose later pages are a different
    catalogue.
    """
    a, b = urlsplit(current_url), urlsplit(candidate)
    if b.netloc and a.netloc and a.netloc != b.netloc:
        return False
    return a.path == b.path


def pages_at_cap(html: Optional[str]) -> bool:
    """Whether this listing is deeper than the site will address.

    Reported rather than swallowed: a run that stops at page 100 is complete
    as far as the site is concerned and truncated as far as the catalogue is,
    and only saying so lets a consumer tell the two apart.
    """
    pages = total_pages(html or "")
    return pages is not None and pages >= PAGE_CAP


def comparable(url: str) -> str:
    """A URL reduced to what makes two spellings the same page.

    The site's own pagination links end in `#filters`, which is not part of
    the address; `strip_tracking` drops that along with campaign parameters.
    """
    parts = urlsplit(strip_tracking(url))
    host = (parts.hostname or "").lower()
    host = host[4:] if host.startswith("www.") else host
    return f"{host}{parts.path.rstrip('/')}?{parts.query}"


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------
def concurrency_limit(url: str) -> Optional[int]:
    """The highest `--concurrency` this URL can honestly support."""
    return None if paginates_by_url(url) else 1


def concurrency_refusal(url: str) -> Optional[str]:
    """Why concurrency above 1 is refused for this URL, or None.

    Refused WITH the reason: a lot page and the auctions index have no
    page 2 at all, so there is nothing for a second worker to fetch, and
    silently running one worker would look like the flag did something.
    """
    kind = listing_kind(url)
    if kind in ("lot", "auction"):
        return (f"a {kind} page is a single page; --concurrency above 1 has "
                f"nothing to fetch")
    if kind == "auctions":
        return ("the auctions index is a single page; --concurrency above 1 "
                "has nothing to fetch")
    if not paginates_by_url(url):
        return f"{url!r} is not a paginated listing"
    return None
