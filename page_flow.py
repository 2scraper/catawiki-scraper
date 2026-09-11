"""page_flow.py — what to do with the page Catawiki just gave us.

Catawiki answers a request five ways, and four of them want a different
response, which is why this module exists rather than the same triage being
written three times inside three engines and drifting apart (§1):

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

No JavaScript crosses that boundary in either direction (§1): Selenium's
`execute_script` takes a function BODY with an explicit `return` where
Playwright and pyppeteer take `() => expr`, so the shared module names the
OPERATION and the engine spells it in its own driver's dialect.

This module has NO scroll loop, and that is measured rather than assumed
-----------------------------------------------------------------------
Three scrolls to `document.body.scrollHeight` added exactly zero cards and
did not change the page height on any of the three page kinds (24 -> 24 on a
category listing, 24 -> 24 on a search grid, 40 -> 40 on a lot page). Every
listing page holds its whole 24 lots and paginates by URL, so a scroll here
would be latency bought for nothing. The opposite of a sibling repo, where
the scroll is the only way any product is ever seen -- which is exactly why
this was checked instead of ported.
"""

from __future__ import annotations

import logging
from typing import Callable, Dict, Iterable, List, Optional, Union
from urllib.parse import urljoin, urlsplit

from product_parser import (auction_links, detect_block_marker,
                            detect_bot_challenge, detect_page_state,
                            listing_kind, lots_payload, next_data, page_url,
                            page_number_from_url, paginates_by_url,
                            served_by_catawiki, strip_tracking, total_pages,
                            PAGE_CAP)

logger = logging.getLogger("page_flow")


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
# 24, while every price on the page is still blank -- §8's "a selector
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
# fill -- it needs to be above 1, per §5, because waiting for a single match
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
    whose CSP lacks `unsafe-eval` refuses outright -- it took a sibling repo's
    run down with `EvalError` and exit 1 on the site's most obvious URL.
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
    Getting that wrong is not a style question: a sibling repo shipped two of
    three engines calling this as `classify(html, url=...)`, both crashed on
    their first fetch, and nothing short of a live run or a signature-binding
    check saw it (§17).
    """
    if html is None:
        # A bare navigation failure. On this site that IS what a refusal
        # looks like from a headless browser, so it is reported rather than
        # raised.
        return "blocked"
    return detect_page_state(html, status, url)


# The retry/solve/blocked decision as DATA rather than as three copies of an
# if-chain in three engines (§1).
#
#   parse    is there anything on this page worth writing down?
#   retry    would fetching it again, from a different exit, plausibly help?
#   solve    is there something to pay a solver for?
#   blocked  does this count towards exit 3?
STATE_POLICY: Dict[str, Dict[str, bool]] = {
    "content":   {"parse": True,  "retry": False, "solve": False, "blocked": False},
    # NOT parsed, and this is the one policy row that differs from every
    # sibling repo. A Catawiki search that matches nothing answers HTTP 200,
    # prints "No results", and BACKFILLS the grid with 24 suggested lots that
    # its payload reports as `total: 24`. Parsing an `empty` page here would
    # write two dozen plausible, well-formed rows for a query that matched
    # nothing at all -- which is worse than writing none, because nothing
    # downstream can tell them from real ones.
    "empty":     {"parse": False, "retry": False, "solve": False, "blocked": False},
    # Served, and still painting. Wants the readiness wait, not another
    # fetch: refetching a shell just buys another shell (§18).
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
# is deliberately 0 here, against `True` in the siblings, and the engines
# read this constant rather than computing their own budget -- a policy
# constant nothing consults is the same defect as dead code (§17).
RETRY_ON_BLOCKED = False
BLOCK_RETRIES_WITHOUT_POOL = 0
BLOCK_RETRIES_WITH_POOL = 0

# One solve per page. Detection is broad on purpose, but a second solve on
# the same page has never been the answer to the first one failing.
SOLVES_PER_PAGE = 1


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


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------
# Three layers, weakest last (§7), and on this site the strongest one is
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
    `currentPage: 2` -- the convention and the site agree, which is what §7
    requires before planning page URLs up front.
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
    (§7) -- while looking like a deliberate safety decision.
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
    way its author was thinking. §17's first lesson, earned again.
    """
    if next_href is None:
        return []
    if isinstance(next_href, str):
        return [next_href] if next_href else []
    return [h for h in next_href if isinstance(h, str) and h]


def pagination_agrees(current_url: str, page_num: int,
                      next_href: Union[str, Iterable[str], None]) -> bool:
    """Whether the site's own next link matches what the convention builds.

    True when ANY advertised link agrees. A listing page carries links to
    several of its own pages -- 2, 3 and the last -- and the question here is
    whether the CONVENTION is the site's, not whether every link happens to
    be page N+1.
    """
    hrefs = _as_hrefs(next_href)
    if not hrefs:
        return True
    want = page_url(current_url, page_num + 1)
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

    Refused WITH the reason (§18): a lot page and the auctions index have no
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
