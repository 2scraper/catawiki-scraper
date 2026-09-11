"""product_parser.py — Catawiki lot extraction. This module IS the site.

Everything Catawiki-specific lives here: how a lot is recognised, where its
data actually is, how pagination is addressed, and how a served page is told
apart from a refusal. The engines carry a handful of named constants and
nothing else.

Where the data is, and why it is not where you would expect
-----------------------------------------------------------
Catawiki is a Next.js site, and the primary path is its own SSR payload,
`<script id="__NEXT_DATA__">`. Not JSON-LD: a category page publishes exactly
one `ItemList` block carrying only `name`, `url` and `image` — no price, no
bid, no id — and a search page and a lot page publish **none at all**
(measured on 17 captures, 2026-09-10). A JSON-LD-primary parser here would
have produced a title-and-image scraper with no prices in it.

    page kind                     SSR key                      JSON-LD
    /{loc}/c/{id}-{slug}          pageProps.categoryLots       1 x ItemList
    /{loc}/s?q=                   pageProps.searchLots         0
    /{loc}/l/{id}-{slug}          pageProps.lotDetailsData     0
                                  + pageProps.biddingBlockResponse
                                  + pageProps.auction
    /{loc}/a                      -- (client-side)             0

A listing's payload has NO BID in it. Bids arrive client-side over PubNub
(`pubnubChannel`), so on a LISTING the money is only in the hydrated DOM and
the SSR supplies everything else. On a LOT PAGE the opposite is true:
`biddingBlockResponse` carries the amount, the absolute open/close times, the
bid history and the sold/reserve flags, so the DOM is not needed and must not
be used — a lot page renders a "similar lots" carousel of 30+ OTHER lots'
prices in the same `c-lot-card__price` class the listing uses for its own
(§4's junk-link data theft, in a new costume).

What `price` means on an auction site
------------------------------------
Three different quantities share one node, told apart only by a label in the
page's language:

    lot_status_current_bid     a live high bid
    lot_status_final_bid       the last bid on a closed lot
    lot_status_starting_bid    nobody has bid at all — a floor, not a bid

`bid_kind` records which one `price` is. Conflating them would put "what
someone paid" and "what nobody has offered" in one column, and would make
every diff between two runs read as a price change when all that happened is
that an auction closed.
"""

from __future__ import annotations

import json
import logging
import math
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from output_writer import Auction, Product

logger = logging.getLogger("product_parser")


# ---------------------------------------------------------------------------
# Hosts, locales, currency
# ---------------------------------------------------------------------------
HOSTS = ("catawiki.com", "www.catawiki.com")

# Taken from the site's OWN `<link rel="alternate" hreflang=...>` set rather
# than guessed (§5). One host, 18 locales as a path prefix -- and two of them
# carry CAPITALS, so a lowercasing normaliser would break Chinese.
LOCALES = ("en", "nl", "de", "fr", "it", "es", "pt", "da", "sv", "no",
           "pl", "el", "hu", "ro", "fi", "ja", "zh-Hans", "zh-Hant")
_LOCALE_BY_LOWER = {loc.lower(): loc for loc in LOCALES}

# EUR is a fact here rather than a guess, and it does not follow the exit or
# the language: € on all 18 locales including ja and zh-Hant, from an NL exit
# and from a hosting-classified one. The lot page's own `live.lot.bid` map
# proves the point in the other direction -- it lists GBP and USD, both
# holding the placeholder `1`, which is why only the EUR figure is ever read.
CURRENCY = "EUR"


def site_host(url: str) -> Optional[str]:
    """The recognised host, or None."""
    host = (urlsplit(url).hostname or "").lower()
    return host if host in HOSTS else None


def unsupported_reason(url: str) -> Optional[str]:
    """Why this URL cannot be scraped, in the reader's terms, or None.

    Says WHAT is wrong rather than just refusing (§5): "is not a Catawiki
    URL" sends someone hunting for a typo when the real problem is that they
    passed a lot page to listing mode.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return f"{url!r} is not an http(s) URL"
    host = (parts.hostname or "").lower()
    if not host:
        return f"{url!r} has no host"
    if host not in HOSTS:
        return (f"{host} is not a Catawiki host; this scraper reads "
                f"{' or '.join(HOSTS)}")
    if locale_of(url) is None:
        return (f"{parts.path!r} does not start with a Catawiki locale "
                f"prefix such as /en/ or /nl/")
    if not listing_kind(url):
        return (f"{parts.path!r} is not a category (/c/), search (/s), lot "
                f"(/l/) or auction (/a) path")
    return None


def is_supported_host(url: str) -> bool:
    return site_host(url) is not None


def locale_of(url: str) -> Optional[str]:
    """The locale segment in its OWN casing, or None.

    `zh-Hans` and `zh-Hant` are served case-sensitively, so the value is
    returned as the site spells it and never lowercased.
    """
    seg = urlsplit(url).path.strip("/").split("/", 1)[0]
    return _LOCALE_BY_LOWER.get(seg.lower())


def host_currency(url: str) -> str:
    """EUR on every locale. Kept for family-shaped call sites."""
    return CURRENCY


# ---------------------------------------------------------------------------
# Paths, page kinds, pagination
# ---------------------------------------------------------------------------
# A lot's own id is in its URL, and the slug beside it is decorative: the
# site translates it per locale (`333-watches` / `333-horloges` /
# `333-armbanduhren`) and canonicalises a foreign one itself -- a /nl/l/ URL
# built with the English slug redirected to the Dutch spelling. So the id is
# the key and the slug is never parsed for meaning.
_LOT_PATH_RE = re.compile(r"^/(?P<loc>[A-Za-z-]{2,7})/l/(?P<id>\d+)(?:-(?P<slug>[^/?#]*))?/?$")
_CATEGORY_PATH_RE = re.compile(r"^/(?P<loc>[A-Za-z-]{2,7})/c/(?P<id>\d+)(?:-(?P<slug>[^/?#]*))?/?$")
_AUCTION_PATH_RE = re.compile(r"^/(?P<loc>[A-Za-z-]{2,7})/a/(?P<id>\d+)(?:-(?P<slug>[^/?#]*))?/?$")
_AUCTIONS_PATH_RE = re.compile(r"^/(?P<loc>[A-Za-z-]{2,7})/a/?$")
_SEARCH_PATH_RE = re.compile(r"^/(?P<loc>[A-Za-z-]{2,7})/s/?$")

# Specification ids are stable across locales where their names are not.
_SPEC_BRAND = 909

SELECTORS = {
    # A lot link, anchored on the URL PATTERN and not on a class: Catawiki's
    # own component classes carry build hashes (`LotBidStatusSection_bid-
    # amount__bWWF4`), and a URL is a contract with search engines.
    "item_link": 'a[href*="/l/"]',
    # The listing card. `article.c-lot-card__container` is the outermost node
    # covering exactly ONE lot, which is what §4 asks for -- a card links to
    # its lot more than once (image and title), so a scope that stopped at
    # "more than one lot link" would never leave the anchor.
    "lot_card": "article.c-lot-card__container",
    "card_price": ".c-lot-card__price",
    "card_status": ".c-lot-card__status-text",
    "card_timer": ".c-lot-card__timer",
    "card_favorites": ".c-lot-card__top-left",
    # Lot page. Matched by SUBSTRING because the tail is a build hash that
    # changes on the next deploy (§4).
    "lot_bid_amount": '[class*="LotBidStatusSection_bid-amount"]',
    "lot_bid_status": '[class*="LotBidStatusSection_subtitle-content"]',
}

PAGE_PARAM = "page"

# The site's own footer offers at most `?page=100`, and it does not fail past
# that -- it CLAMPS. `?page=99999` on an 11,681-lot category returned HTTP
# 200 with `currentPage: 100` and page 100's own 24 lots (measured
# 2026-09-10). So a planner that ignores the cap re-fetches page 100 for
# every further page, adds no new sku, and a data-based terminator then reads
# "listing exhausted" -- a COMPLETE run holding 2,400 of 11,681 lots. Every
# page plan is capped here, and the cap is reported as its own stop reason
# rather than looking like the end of the catalogue.
PAGE_CAP = 100

# Both listing kinds take `?page=N` and the payload confirms it
# (`currentPage` 2 on a search page 2). The cap above is measured on category
# and applied to search too: a search of 681 lots is 29 pages and cannot
# reach it, so this is deliberately the conservative direction.
PAGINATED_KINDS = ("category", "search")

TRACKING_PARAMS = frozenset("""
utm_source utm_medium utm_campaign utm_term utm_content utm_id
gclid fbclid msclkid ttclid twclid igshid
_ga _gl mc_cid mc_eid ref referrer
""".split())


def listing_kind(url: str) -> str:
    """Which kind of page this URL is: category, search, lot, auction,
    auctions, or "" when it is none of them."""
    path = urlsplit(url).path
    if _CATEGORY_PATH_RE.match(path):
        return "category"
    if _SEARCH_PATH_RE.match(path):
        return "search"
    if _LOT_PATH_RE.match(path):
        return "lot"
    if _AUCTION_PATH_RE.match(path):
        return "auction"
    if _AUCTIONS_PATH_RE.match(path):
        return "auctions"
    return ""


def strip_tracking(url: str) -> str:
    """The URL without tracking parameters and without a fragment.

    The site's own pagination links end in `#filters`, which is not part of
    the address and would otherwise make two spellings of one page look like
    two pages.
    """
    parts = urlsplit(url)
    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if k not in TRACKING_PARAMS]
    return urlunsplit((parts.scheme, parts.netloc, parts.path,
                       urlencode(kept), ""))


def paginates_by_url(url: str) -> bool:
    """Whether page N of this listing has an address of its own."""
    return listing_kind(url) in PAGINATED_KINDS


def page_url(url: str, page_num: int) -> Optional[str]:
    """The address of page `page_num`, or None if there cannot be one.

    None means "do not fetch this": either the kind does not paginate by URL,
    or the number is past the site's own cap, where a request would silently
    return the capped page's contents instead of failing.
    """
    if page_num < 1 or not paginates_by_url(url):
        return None
    if page_num > PAGE_CAP:
        return None
    parts = urlsplit(strip_tracking(url))
    params = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
              if k != PAGE_PARAM]
    if page_num > 1:
        params.append((PAGE_PARAM, str(page_num)))
    return urlunsplit((parts.scheme, parts.netloc, parts.path,
                       urlencode(params), ""))


def page_number_from_url(url: str) -> int:
    """The page this URL addresses; 1 when it says nothing."""
    for key, value in parse_qsl(urlsplit(url).query):
        if key == PAGE_PARAM:
            try:
                return max(1, int(value))
            except ValueError:
                return 1
    return 1


_NOT_A_CATEGORY = frozenset("""
c s l a u v e f help pages accounts feed stories press highlights
livestreams lp veiling
""".split())


def category_from_url(url: str) -> Optional[str]:
    """The category slug a listing URL names, or None.

    The numeric id is the stable key and is returned with it, because the
    slug alone is a different string in each of the 18 locales.
    """
    match = _CATEGORY_PATH_RE.match(urlsplit(url).path)
    if not match:
        return None
    slug = match.group("slug") or ""
    if slug in _NOT_A_CATEGORY:
        return None
    return f"{match.group('id')}-{slug}" if slug else match.group("id")


def sku_from_url(url: str) -> Optional[str]:
    """The lot id from a lot URL. The site's own id, not a derived one."""
    match = _LOT_PATH_RE.match(urlsplit(url).path)
    return match.group("id") if match else None


def auction_id_from_url(url: str) -> Optional[str]:
    match = _AUCTION_PATH_RE.match(urlsplit(url).path)
    return match.group("id") if match else None


def _lot_id_from_href(href: str) -> Optional[int]:
    match = re.search(r"/l/(\d+)", href or "")
    return int(match.group(1)) if match else None


# ---------------------------------------------------------------------------
# The SSR payload
# ---------------------------------------------------------------------------
_NEXT_DATA_RE = re.compile(
    r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


def next_data(html: str) -> Optional[dict]:
    """`pageProps` out of the Next.js payload, or None if it is not there."""
    match = _NEXT_DATA_RE.search(html or "")
    if not match:
        return None
    try:
        return (json.loads(match.group(1)) or {}).get("props", {}).get("pageProps")
    except (ValueError, AttributeError):
        logger.warning("__NEXT_DATA__ present but not parseable as JSON")
        return None


_LOTS_KEYS = ("categoryLots", "searchLots")


def lots_payload(page_props: Optional[dict]) -> Tuple[Optional[str], dict]:
    """The listing payload and which key held it.

    Three shapes, because an AUCTION page is a listing too and spells itself
    differently: a category and a search publish
    `{total, lots[], filters, meta}` under their own key, while an auction
    page publishes a bare `lots` LIST beside an `auction` object -- 130 lots
    on one page, no `total` of its own and no pagination at all. Normalising
    it here rather than in the caller is what lets one `parse_products` read
    all three.
    """
    props = page_props or {}
    for key in _LOTS_KEYS:
        value = props.get(key)
        if isinstance(value, dict) and isinstance(value.get("lots"), list):
            return key, value
    bare = props.get("lots")
    if isinstance(bare, list):
        auction = props.get("auction") or {}
        total = auction.get("numberOfLots") or auction.get("lotCount")
        return "lots", {"lots": bare,
                        "total": total if isinstance(total, int) else len(bare),
                        "meta": {}}
    return None, {}


def total_results(html: str) -> Optional[int]:
    """How many lots the listing says it holds, in total."""
    _, payload = lots_payload(next_data(html))
    total = payload.get("total")
    return total if isinstance(total, int) else None


def lots_per_page(html: str) -> int:
    props = next_data(html) or {}
    value = props.get("lotsPerPage")
    return value if isinstance(value, int) and value > 0 else 24


def total_pages(html: str, url: str = "") -> Optional[int]:
    """Pages this listing has, from ARITHMETIC and capped by the site's own
    limit -- no selector, no link-chaining.

    This is the strongest form §7 asks for: page 1's payload states `total`
    and `lotsPerPage`, so every page's address is knowable at once and
    workers can be handed independent pages.
    """
    if url and listing_kind(url) == "auction":
        # An auction page carries every one of its lots at once -- 130 of 130
        # on the captured one -- and publishes no page links. Dividing its
        # total by `lotsPerPage` would invent pages that do not exist.
        return 1
    total = total_results(html)
    if total is None:
        return None
    pages = max(1, math.ceil(total / lots_per_page(html)))
    return min(pages, PAGE_CAP)


def pages_beyond_cap(html: str) -> int:
    """How many pages the catalogue has that the site will not address.

    Reported rather than swallowed: a run that stops at the cap is complete
    as far as the site is concerned and truncated as far as the catalogue is,
    and only saying so lets a consumer tell the two apart.
    """
    total = total_results(html)
    if total is None:
        return 0
    pages = max(1, math.ceil(total / lots_per_page(html)))
    return max(0, pages - PAGE_CAP)


def search_header(html: str) -> Optional[str]:
    """The query a search page ran, from the payload rather than the heading."""
    props = next_data(html) or {}
    query = (props.get("query") or {}) if isinstance(props.get("query"), dict) else {}
    term = query.get("q")
    return term if isinstance(term, str) and term else None


# ---------------------------------------------------------------------------
# Bid state, resolved through the page's own dictionary
# ---------------------------------------------------------------------------
# The three states are identical in markup -- no modifier class, no data
# attribute (checked: `modifiers=()` on all 24 cards of every capture) -- and
# differ only by a label in the page's language.
#
# Rather than a hand-written table for 18 languages, the label is mapped back
# through the dictionary the page already ships:
# `_nextI18Next.initialI18nStore.{locale}.translations`, ~1035 entries keyed
# by stable English names. It is locale-proof and it self-heals if the site
# retranslates.
#
# The key choice is load-bearing, not cosmetic. Two keys read "Current bid"
# in English and the card uses `lot_status_current_bid`; in zh-Hant they
# differ -- `lot_status_current_bid` is the card's 現時出價 while
# `auction_current_bid` is 當前出價. A table built from the English page would
# have looked right and matched nothing in Chinese.
_BID_KIND_BY_KEY = {
    "lot_status_current_bid": "current",
    "lot_status_final_bid": "final",
    "lot_status_starting_bid": "starting",
}

# Zero-width characters are the site's own empty-price placeholder, and they
# are the reason `.c-lot-card__price` is present on 24 of 24 cards while 2-3
# of them hold no price at all. A truthiness check on the node reports 100%
# coverage and writes an invisible character into every row (§10).
_ZERO_WIDTH = "​‌‍⁠﻿"


def _clean(text: Optional[str]) -> str:
    if not text:
        return ""
    stripped = text.strip()
    for char in _ZERO_WIDTH:
        stripped = stripped.replace(char, "")
    return stripped.strip()


def bid_kind_labels(page_props: Optional[dict]) -> Dict[str, str]:
    """`{rendered label: bid kind}` for the page's own locale."""
    i18n = (page_props or {}).get("_nextI18Next") or {}
    store = i18n.get("initialI18nStore") or {}
    labels: Dict[str, str] = {}
    for bundle in store.values():
        translations = (bundle or {}).get("translations") or {}
        for key, kind in _BID_KIND_BY_KEY.items():
            label = _clean(translations.get(key))
            if label:
                labels[label] = kind
    return labels


# ---------------------------------------------------------------------------
# Money
# ---------------------------------------------------------------------------
# One currency, five written forms for it, all seen on the same lots:
#   €1,535   en, ja, zh-*      symbol first, comma grouping
#   € 1.535  nl, pl            symbol first, space, dot grouping
#   1.535 €  de                symbol last
#   €27      any               no grouping
# and the German timer beside it uses NBSP, so no-break variants have to be
# part of the grouping class or `1 535 €` parses as 535 (§4).
_GROUP_SPACES = "    "
_AMOUNT = (r"\d{1,3}(?:[.," + _GROUP_SPACES + r"]\d{3})+(?:[.,]\d{1,2})?"
           r"|\d+(?:[.,]\d{1,2})?")
_PRICE_RE = re.compile(r"€\s*(" + _AMOUNT + r")|(" + _AMOUNT + r")\s*€")
_PCT_RE = re.compile(r"-?\s*\d{1,3}(?:[.,]\d+)?\s*%|-?\s*%\s*\d{1,3}(?:[.,]\d+)?")


def _normalize_amount(raw: str) -> Optional[float]:
    """A written amount as a number, honouring all three groupings.

    `1,234.56` / `1.234,56` / `1 234,56`, with the space form allowing NBSP,
    narrow NBSP and thin space. Whichever of dot and comma comes LAST is the
    decimal point; where only one appears, exactly three trailing digits is a
    thousands grouping, because no currency has a three-digit subunit.
    """
    text = (raw or "").strip()
    for space in _GROUP_SPACES:
        if space != " ":
            text = text.replace(space, " ")
    text = text.replace(" ", ".")
    has_dot, has_comma = "." in text, "," in text
    if has_dot and has_comma:
        decimal = "," if text.rfind(",") > text.rfind(".") else "."
        text = text.replace("." if decimal == "," else ",", "").replace(decimal, ".")
    elif has_comma:
        head, _, tail = text.rpartition(",")
        text = (head + tail) if len(tail) == 3 else text.replace(",", ".")
    elif has_dot:
        head, _, tail = text.rpartition(".")
        if len(tail) == 3:
            text = text.replace(".", "")
    try:
        return float(text)
    except ValueError:
        return None


def prices_in(text: str) -> List[float]:
    """Every euro amount in reading order, percentages removed FIRST.

    Removed first rather than rejected afterwards: a rejected match has
    already consumed the currency symbol, so skipping it loses the real price
    too (§4).
    """
    out: List[float] = []
    for match in _PRICE_RE.finditer(_PCT_RE.sub(" ", text or "")):
        value = _normalize_amount(match.group(1) or match.group(2))
        if value is not None:
            out.append(value)
    return out


def price_in(text: str) -> Optional[float]:
    """The first euro amount in `text`, or None."""
    found = prices_in(text)
    return found[0] if found else None


# ---------------------------------------------------------------------------
# Detection: what kind of answer did we get?
# ---------------------------------------------------------------------------
# Positive detection, inverted from the usual marker hunt, because on this
# site the refusal carries no vendor marker worth matching: Akamai answers a
# refused request with a 394-byte "Access Denied" page whose only clue is an
# `errors.edgesuite.net` reference id. Chromium's own network-error page is
# the same problem one step worse -- it carries `<title>www.catawiki.com</title>`
# and would pass a title check (§18).
#
# What every page the site actually serves has, and neither refusal does, is
# a reference to Catawiki's own asset hosts.
_ASSET_MARKER = re.compile(r"assets\.catawiki\.nl|cdn\.catawiki\.net"
                           r"|assets\.catawiki\.com", re.I)
_ASSET_MIN_MATCHES = 2

# Vendor markers are deliberately SHORT of `akamai`. Catawiki IS fronted by
# Akamai and its refusal is an Akamai page -- but the string appears in the
# response HEADER (`server: AkamaiGHost`), not in the body of a good page or
# a bad one: 0 occurrences in all six captures checked. §18's rule is that a
# marker matching a good page is worse than no marker; the corollary is that
# a marker matching NEITHER is not a marker at all.
#
# Two sets, because §8's "detected is not blocking" begins with not calling
# them the same thing:
#
#   BLOCK_MARKERS          the site refused. Nothing to solve and nothing to
#                          pay for; a real browser or another exit is the
#                          answer.
#   BOT_CHALLENGE_MARKERS  something solvable was rendered. This is the only
#                          set `--solve-captcha` acts on.
#
# The refusal is 394 bytes of "Access Denied" carrying a reference id and no
# vendor name, so the reference host is the one durable string in it.
BLOCK_MARKERS = (
    "errors.edgesuite.net",
    "Access Denied",
)

# Catawiki rendered none of these to an anonymous visitor across 19 captures
# and 6 locales, and -- unlike a sibling repo in this family -- it ships no
# captcha mount point and no site key in its page config either
# (`<captcha-widgets>` and `CAPTCHA_SITE_KEY`: 0 occurrences on every
# capture). Those are another site's findings and were checked before being
# believed here. This set is therefore forward-looking: the shapes a
# challenge WOULD take, kept so that an appearance is recognised instead of
# being reported as an empty page.
BOT_CHALLENGE_MARKERS = (
    "recaptcha/api2/anchor",
    "recaptcha/api2/bframe",
    "recaptcha/api.js",
    "data-sitekey",
    "hcaptcha.com/captcha",
    "challenges.cloudflare.com",
    "px-captcha",
)

# There is NO extension-tag stripping here, and that is a measurement rather
# than an oversight. The Scraping Browser's auto-solve extension does inject
# its own hunters into every page it loads: a CDP capture of a perfectly good
# listing page carries 16 `chrome-extension://` scripts, among them
# `recaptcha/hunter.js`, plus a `data-ts-input="cf-turnstile-response"`
# attribute. But none of the markers above matches that capture even WITHOUT
# stripping -- measured: `detect_bot_challenge` returns None and
# `detect_page_state` returns "content" on it -- so the guard §8 describes
# would be code that looks load-bearing and never runs (§17).
#
# Add it back together WITH any broadening of the set above. A bare
# `recaptcha`, a bare `cf-turnstile` or a hunter path are exactly the strings
# the extension puts on good pages.


def served_by_catawiki(html: str) -> bool:
    """Whether this page was built out of Catawiki's own assets.

    The primary signal, and the only one that answers correctly for a
    browser-generated error page: Chromium's own network-error page carries
    `<title>www.catawiki.com</title>`, so a title check calls it a real page,
    and it holds no vendor marker of any kind (§18).
    """
    return len(_ASSET_MARKER.findall(html or "")) >= _ASSET_MIN_MATCHES


def detect_block_marker(html: str) -> Optional[str]:
    """The refusal marker this page carries, or None."""
    lowered = (html or "").lower()
    for marker in BLOCK_MARKERS:
        if marker.lower() in lowered:
            return marker
    return None


def detect_bot_challenge(html: str, url: str = "") -> Optional[str]:
    """The SOLVABLE challenge this page rendered, or None.

    Deliberately silent about a refusal: a solve must not be attempted, or
    charged for, on a page that carries nothing to solve.
    """
    lowered = (html or "").lower()
    for marker in BOT_CHALLENGE_MARKERS:
        if marker.lower() in lowered:
            return marker
    return None


def is_no_results(html: str) -> bool:
    """Whether a search genuinely matched nothing.

    This needs the payload, not a phrase. A search that matches nothing
    returns HTTP 200, prints "No results", AND backfills the grid with 24
    suggested lots reported as `total: 24` -- so a parser trusting the count
    returns two dozen plausible rows for a query that matched nothing.

    The site states the truth itself: `meta.extended_search_result` is true
    exactly when the results shown are not the query's own.
    """
    _, payload = lots_payload(next_data(html))
    meta = payload.get("meta") or {}
    return bool(meta.get("extended_search_result"))


def detect_page_state(html: str, status: Optional[int] = None,
                      url: str = "") -> str:
    """What kind of answer this is: content, empty, blocked, challenge or
    shell.

    Ordered by how much each signal PROVES, not by what is cheapest to check
    (§17). An unambiguous positive -- the payload holding lots -- outranks
    the asset-reference threshold, so a leanly-built real page cannot be
    reported as blocked; and the vendor marker scan only ever refines a state
    that has already gone wrong.
    """
    html = html or ""
    props = next_data(html)
    key, payload = lots_payload(props)

    # 1. Unambiguous positive: the site served its own listing payload.
    if payload.get("lots"):
        if is_no_results(html):
            return "empty"
        return "content"
    if props and (props.get("lotDetailsData") or props.get("auction")):
        return "content"

    # 2. Its own empty answer, stated by the payload rather than by a phrase.
    if key and payload.get("total") == 0:
        return "empty"

    # 3. A challenge it rendered on purpose.
    if detect_bot_challenge(html):
        return "challenge"

    # 4. A refusal. The status is the primary signal where there is one: this
    #    site answers a refused request with 403 and a page carrying no
    #    vendor marker worth matching.
    if status is not None and status in (401, 403, 429) or (
            status is not None and status >= 500):
        return "blocked"
    if detect_block_marker(html):
        return "blocked"
    if not served_by_catawiki(html):
        return "blocked"

    # 5. An auctions index: no lot payload by design, so its own links are
    #    the positive signal. Without this it falls through to `shell` and an
    #    engine waits for a grid that is never coming.
    if listing_kind(url) == "auctions" and auction_links(html):
        return "content"

    # 6. Served, built from the site's assets, and holding no payload yet.
    #    This is a page that is still painting, and it wants a WAIT rather
    #    than a retry -- retrying a shell just fetches another shell (§18).
    return "shell"


# ---------------------------------------------------------------------------
# Listing rows
# ---------------------------------------------------------------------------
def _cards_by_lot_id(soup: BeautifulSoup,
                     labels: Dict[str, str]) -> Dict[int, dict]:
    """What the hydrated DOM adds to each lot, keyed by the site's lot id.

    Joined on the id in the card's own anchor, which is the same id the SSR
    payload uses -- 24 cards against 24 payload lots on every capture -- so
    the two views cannot be misaligned by rendering order.
    """
    out: Dict[int, dict] = {}
    for card in soup.select(SELECTORS["lot_card"]):
        anchor = card.select_one("a[href]")
        lot_id = _lot_id_from_href(anchor.get("href")) if anchor else None
        if lot_id is None:
            continue
        price_node = card.select_one(SELECTORS["card_price"])
        status_node = card.select_one(SELECTORS["card_status"])
        timer_node = card.select_one(SELECTORS["card_timer"])
        favorites_node = card.select_one(SELECTORS["card_favorites"])
        status = _clean(status_node.get_text(" ", strip=True)) if status_node else ""
        favorites = _clean(favorites_node.get_text(" ", strip=True)) if favorites_node else ""
        out[lot_id] = {
            "price": price_in(_clean(price_node.get_text(" ", strip=True))) if price_node else None,
            "bid_kind": labels.get(status),
            "bid_status_text": status or None,
            "time_left_text": _clean(timer_node.get_text(" ", strip=True)) or None if timer_node else None,
            "favorite_count": int(favorites) if favorites.isdigit() else None,
        }
    return out


def _buy_now_amount(value: Any) -> Optional[float]:
    """`buyNow` is an object, not a number: `{"price_eur": 771}`."""
    if isinstance(value, dict):
        amount = value.get("price_eur")
        if isinstance(amount, (int, float)):
            return float(amount)
    elif isinstance(value, (int, float)):
        return float(value)
    return None


def parse_products(html: str, url: str, page: int = 1,
                   position_offset: int = 0,
                   category: Optional[str] = None) -> List[Product]:
    """Rows for one listing page, in the payload's own order.

    Payload order is page order, which is what §8 requires: a dedupe that
    mutates a running set inside a fetch loop makes the output depend on
    which page finished first.
    """
    props = next_data(html)
    key, payload = lots_payload(props)
    lots = payload.get("lots") or []
    soup = BeautifulSoup(html or "", "html.parser")
    overlay = _cards_by_lot_id(soup, bid_kind_labels(props))
    auction = (props or {}).get("auction") or {}
    auction_title = _clean(auction.get("title")) or None
    auction_close_at = _clean(auction.get("closeAt")) or None
    auction_status = _clean(auction.get("status")) or None

    if not lots:
        return _dom_only_rows(soup, url, page, position_offset, props)

    rows: List[Product] = []
    for index, lot in enumerate(lots, start=1):
        if not isinstance(lot, dict):
            continue
        lot_id = lot.get("id")
        card = overlay.get(lot_id, {})
        price = card.get("price")
        rows.append(Product(
            url=lot.get("url") or "",
            sku=str(lot_id) if lot_id is not None else None,
            title=_clean(lot.get("title")) or None,
            # No brand field anywhere on a listing. A title here reads
            # "Cartier - Tank Must de Cartier PM - ..." and splitting on the
            # dash would be a guess presented as a fact (§8); the lot page's
            # own `specifications` carry a real Brand and fill this in
            # --mode lot.
            brand=None,
            price=price,
            currency=CURRENCY if price is not None else None,
            image_url=lot.get("originalImageUrl") or lot.get("thumbImageUrl") or None,
            category=category or category_from_url(url),
            # Provenance, and on this site it is doing real work: the amount
            # comes from the DOM and everything else from the payload, so a
            # row whose card had not hydrated is `next_data` and is missing
            # its price for a reason a consumer can see.
            price_source="next_data+dom" if price is not None else "next_data",
            page=page,
            position=position_offset + index,
            bid_kind=card.get("bid_kind"),
            bid_status_text=card.get("bid_status_text"),
            time_left_text=card.get("time_left_text"),
            # From the DOM, NOT from the payload. The payload's own
            # `favoriteCount` is 0 on 288 of 288 lots across 12 listing
            # captures while the hydrated card shows the real figure on every
            # one of them -- a field that is present, authoritative-looking
            # and uniformly wrong.
            favorite_count=card.get("favorite_count"),
            subtitle=_clean(lot.get("subtitle")) or None,
            auction_id=str(lot["auctionId"]) if lot.get("auctionId") else None,
            reserve_price_set=lot.get("reservePriceSet"),
            buy_now=_buy_now_amount(lot.get("buyNow")),
            has_free_shipping=lot.get("hasFreeShipping"),
            bidding_start_at=lot.get("biddingStartTime") or None,
            listing_kind=key and {"categoryLots": "category",
                                  "searchLots": "search",
                                  "lots": "auction"}.get(key),
            # Only an auction page states these, and they are the columns a
            # category listing cannot give at all: an absolute close time and
            # the auction's own status. A row scraped by auction is therefore
            # strictly better than the same row scraped by category -- worth
            # saying in the README rather than leaving to be discovered.
            auction_title=auction_title,
            auction_close_at=auction_close_at,
            auction_status=auction_status,
        ))
    return rows


def _dom_only_rows(soup: BeautifulSoup, url: str, page: int,
                   position_offset: int, props: Optional[dict]) -> List[Product]:
    """The fallback path: cards without a payload behind them.

    Kept because the payload is one script tag away from a redesign, and a
    URL pattern is not. It cannot recover the payload-only columns, and says
    so through `price_source` rather than leaving a caller to guess.
    """
    labels = bid_kind_labels(props)
    rows: List[Product] = []
    for index, card in enumerate(soup.select(SELECTORS["lot_card"]), start=1):
        anchor = card.select_one("a[href]")
        href = anchor.get("href") if anchor else ""
        lot_id = _lot_id_from_href(href)
        if lot_id is None:
            continue
        price_node = card.select_one(SELECTORS["card_price"])
        status_node = card.select_one(SELECTORS["card_status"])
        title_node = card.select_one(".c-lot-card__title")
        price = price_in(_clean(price_node.get_text(" ", strip=True))) if price_node else None
        rows.append(Product(
            url=href,
            sku=str(lot_id),
            title=_clean(title_node.get_text(" ", strip=True)) or None if title_node else None,
            price=price,
            currency=CURRENCY if price is not None else None,
            category=category_from_url(url),
            price_source="dom",
            page=page,
            position=position_offset + index,
            bid_kind=labels.get(_clean(status_node.get_text(" ", strip=True))) if status_node else None,
        ))
    if rows:
        logger.warning("no SSR payload on %s: %d rows recovered from the DOM "
                       "alone, without the payload-only columns", url, len(rows))
    return rows


# ---------------------------------------------------------------------------
# A lot page
# ---------------------------------------------------------------------------
def parse_lot_page(html: str, url: str,
                   category: Optional[str] = None) -> Optional[Product]:
    """One row for one lot, from the payload only.

    Deliberately not from the DOM. A lot page renders a "similar lots"
    carousel of 30+ other lots in the very same `c-lot-card__price` class a
    listing uses for its own price, and its own bid node is absent once
    bidding has closed -- so "the first euro amount on the page" is a
    neighbour's price or an estimate. The payload has the amount, both
    absolute times, the bid history and the sold/reserve flags, and needs
    none of that guessing.
    """
    props = next_data(html)
    if not props:
        return None
    details = props.get("lotDetailsData") or {}
    bidding = props.get("biddingBlockResponse") or {}
    auction = props.get("auction") or {}
    if not details:
        return None

    lot_id = details.get("lotId") or sku_from_url(url)
    # Only the EUR figure is real: `live.lot.bid` lists GBP and USD holding
    # the placeholder 1 on every lot seen.
    amount = bidding.get("localizedCurrentBidAmount")
    price = float(amount) if isinstance(amount, (int, float)) else None
    closed = bool(bidding.get("closed") or details.get("isClosed"))
    # A closed lot's amount is the LAST bid, which is a hammer price only if
    # it sold: this one reached EUR 1,300 with `reservePriceMet: false`, so
    # it changed hands for nothing at all.
    if price is None:
        kind = None
    elif closed:
        kind = "final"
    elif bidding.get("biddingHistory", {}).get("bids"):
        kind = "current"
    else:
        kind = "starting"

    estimate = details.get("expertsEstimate") or {}
    # Keyed by the site's own numeric `specificationId`, NOT by `name`: the
    # names are translated (909 is "Brand" on /en and "Merk" on /nl, and the
    # id is 909 on both), so a name-keyed lookup returns a brand on English
    # pages and null on the other seventeen locales -- which is exactly how
    # it read before a second locale was run (§15).
    specs = {s.get("specificationId"): s.get("value")
             for s in (details.get("specifications") or [])
             if isinstance(s, dict)}
    seller = details.get("sellerInfo") or {}
    score = (seller.get("score") or {}) if isinstance(seller.get("score"), dict) else {}
    images = [i.get("url") or i.get("originalUrl")
              for i in (details.get("images") or []) if isinstance(i, dict)]

    return Product(
        url=strip_tracking(url),
        sku=str(lot_id) if lot_id is not None else None,
        title=_clean(details.get("lotTitle")) or None,
        # The lot page has a real Brand in its own specifications, so this
        # column is a fact here where it is null on a listing row.
        brand=_clean(specs.get(_SPEC_BRAND)) or None,
        price=price,
        currency=CURRENCY if price is not None else None,
        in_stock=(not closed) if price is not None or closed else None,
        image_url=next((i for i in images if i), None),
        category=category or _clean(
            (details.get("category") or {}).get("name")
            if isinstance(details.get("category"), dict) else None) or None,
        price_source="next_data",
        bid_kind=kind,
        subtitle=_clean(details.get("lotSubtitle")) or None,
        favorite_count=details.get("favoriteCount"),
        auction_id=str(auction.get("id")) if auction.get("id") else None,
        auction_title=_clean(auction.get("title")) or None,
        # `reservePriceMet` is null exactly when the lot has no reserve, and
        # a bool when it has one. Checked against the listing payload's own
        # `reservePriceSet` for both captured lots: False/null and
        # True/false. Two lots is thin, so this is stated rather than
        # assumed -- but it beats reporting "unknown" for a fact the payload
        # does carry.
        reserve_price_set=bidding.get("reservePriceMet") is not None,
        reserve_price_met=bidding.get("reservePriceMet"),
        sold=bidding.get("sold"),
        # Absolute, per LOT, and epoch millis in the payload. A listing page
        # has only a relative timer ("3 days left"), which means nothing in a
        # dataset read tomorrow.
        bidding_start_at=_epoch_iso(bidding.get("biddingStartTime")),
        bidding_end_at=_epoch_iso(bidding.get("biddingEndTime")),
        bid_count=_bid_count(bidding),
        bid_count_is_floor=_bid_count_is_floor(bidding),
        next_min_bid=_number(bidding.get("localizedMinBidAmount")),
        buy_now=_buy_now_amount(details.get("buyNow")
                                or (bidding.get("live") or {}).get("lot", {}).get("buyNow")),
        # `min`/`max` are per currency and the non-EUR entries hold 0 -- which
        # means "not provided" and not "free" (§4's null-versus-zero).
        estimate_min=_number((estimate.get("min") or {}).get("EUR")) or None,
        estimate_max=_number((estimate.get("max") or {}).get("EUR")) or None,
        seller_id=str(seller.get("id")) if seller.get("id") else None,
        seller_country=_clean(((seller.get("address") or {}).get("country") or {}).get("name")) or None,
        seller_score=_number(score.get("score")),
        seller_feedback_count=score.get("lifetimeCount"),
        listing_kind="lot",
    )


# The site returns the last TEN bids and no total: two lots with very
# different activity both reported exactly 10, and `biddingHistory` carries
# no count field. So the length is a FLOOR once it reaches the cap, and the
# column says which kind of number it is -- without that flag one column
# would silently mean two things (the family's `sold_is_floor` lesson).
_BID_HISTORY_CAP = 10


def _bid_history(bidding: dict) -> List[dict]:
    bids = (bidding.get("biddingHistory") or {}).get("bids")
    return [b for b in bids if isinstance(b, dict)] if isinstance(bids, list) else []


def _bid_count(bidding: dict) -> Optional[int]:
    count = len(_bid_history(bidding))
    return count or None


def _bid_count_is_floor(bidding: dict) -> Optional[bool]:
    count = len(_bid_history(bidding))
    return count >= _BID_HISTORY_CAP if count else None


def _number(value: Any) -> Optional[float]:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _epoch_iso(value: Any) -> Optional[str]:
    """Epoch milliseconds as an ISO-8601 UTC string."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    from datetime import datetime, timezone
    return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat()


_AUCTION_HREF_RE = re.compile(r"/(?:[A-Za-z-]{2,7})/a/(\d+)-([^\"?#]*)")


def auction_links(html: str) -> List[Tuple[str, str]]:
    """`(id, slug)` for every auction an index page lists.

    The auctions index carries no lot payload and no lot cards at all -- its
    24 auctions arrive client-side -- so it is read from its own links. This
    is the URL-pattern path doing the job structured data cannot.
    """
    seen: Dict[str, str] = {}
    for auction_id, slug in _AUCTION_HREF_RE.findall(html or ""):
        seen.setdefault(auction_id, slug)
    return sorted(seen.items())


AUCTION_CARD_SELECTOR = '[data-testid="all-auctions-card"]'


def auction_rows(html: str, url: str, page: int = 1) -> List[Auction]:
    """One row per auction on the index page.

    Anchored on the site's own `data-testid`, which is a semantic name rather
    than a build hash -- the card's inner classes are Tailwind utilities and
    its title lives in an `<h6>` with no id of its own, so the title is the
    card's FIRST `<h6>` and the relative end phrase is the second.

    Two fields the index appears to offer are deliberately NOT read. The
    badge in the image corner holds `+127` on one card and `18+` on the next
    -- a further-lots hint and an age warning in the same position -- so
    reading it as a lot count would publish an age flag as a number on some
    rows. And "Curated by {name}" is a person; the auction's own page states
    the same expert in a structured field for anyone who wants it.
    """
    soup = BeautifulSoup(html or "", "html.parser")
    rows: List[Auction] = []
    seen = set()
    for index, card in enumerate(soup.select(AUCTION_CARD_SELECTOR), start=1):
        anchor = card.select_one('a[href*="/a/"]')
        if not anchor:
            continue
        href = anchor.get("href") or ""
        match = _AUCTION_HREF_RE.search(href)
        if not match:
            continue
        auction_id, slug = match.group(1), match.group(2)
        if auction_id in seen:
            continue
        seen.add(auction_id)
        headings = card.find_all("h6")
        rows.append(Auction(
            url=href if href.startswith("http") else f"https://www.catawiki.com{href}",
            sku=auction_id,
            title=_clean(headings[0].get_text(" ", strip=True)) if headings else None,
            ends_text=(_clean(headings[1].get_text(" ", strip=True))
                       if len(headings) > 1 else None),
            slug=slug or None,
            locale=locale_of(url),
            page=page,
            position=index,
        ))
    if not rows:
        # The links are there even when the cards are not: `auction_links`
        # reads the raw hrefs, which is the fallback that keeps a redesign of
        # the card from emptying the mode entirely.
        for index, (auction_id, slug) in enumerate(auction_links(html), start=1):
            rows.append(Auction(
                url=f"https://www.catawiki.com/{locale_of(url) or 'en'}/a/{auction_id}-{slug}",
                sku=auction_id, slug=slug or None, locale=locale_of(url),
                page=page, position=index))
        if rows:
            logger.warning("auctions index: no cards matched %s, %d rows "
                           "recovered from links alone",
                           AUCTION_CARD_SELECTOR, len(rows))
    return rows


def auction_metadata(html: str, url: str) -> Dict[str, Optional[str]]:
    """What a page says about the auction its lots belong to."""
    props = next_data(html) or {}
    auction = props.get("auction") or {}
    return {
        "auction_id": str(auction.get("id")) if auction.get("id") else auction_id_from_url(url),
        "auction_title": _clean(auction.get("title")) or None,
        "auction_status": _clean(auction.get("status")) or None,
        "auction_start_at": _clean(auction.get("startAt")) or None,
        "auction_close_at": _clean(auction.get("closeAt")) or None,
        "auction_lot_count": auction.get("numberOfLots") or auction.get("lotCount"),
        "locale": locale_of(url),
    }
