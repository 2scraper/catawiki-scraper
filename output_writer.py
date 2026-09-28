"""
output_writer.py
-----------------
Shared row models + JSON/CSV writers used by all three scrapers.

Two modes, one row shape
------------------------
    --mode listing   a search grid or a category listing -> Product
    --mode product   one /{shop}/{slug} detail page -> Product, with the
                     trailing detail-only fields populated

The listing and lot modes yield the SAME class, because here a lot page is not a
different kind of object from a tile — it is the same product described more
fully. So there is no second dataclass here (a sibling repo needs one for
reviews; this one does not), and `diff_runs.py` can compare a listing run
against a product run on the columns both populate.

There is deliberately no `--mode seller`. A seller's own page is its own
application shell with its own markup, none of which has been captured or
measured, and a mode that ships untested would be worse than a mode that is
absent. `product_parser.shop_metadata` reads the seller's facts off a detail
page for the sidecar, which is what a run covering one product can honestly
say about its seller.

`Product` keeps the family's first sixteen columns in the family's order,
with this site's own ones appended after `position`, so a consumer
written against another repo in this family still reads the prefix unchanged.

Everything below is row-class-agnostic: pass `row_cls` so an empty CSV still
gets the right header for the mode that produced it.
"""

import csv
import json
from dataclasses import dataclass, asdict, field, fields
from datetime import datetime, timezone
from typing import Optional, List, Set, Sequence, Any, Type


# The hostname a row came from. Catawiki is ONE storefront — 18 locales as a
# path prefix on one host — measured by
# fetching the same URL from an Indonesian and a US exit and getting
# identical markup, identical `<html lang="id">` and identical IDR prices —
# so this column is `catawiki.com` on every row of every run. It is kept
# because the family's schema has it in this position and consumers read the
# columns by name across repos.
SOURCE_DEFAULT = "catawiki.com"


@dataclass
class Product:
    source: str = SOURCE_DEFAULT
    scraped_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    url: str = ""
    # The product's `/{shop-slug}/{product-slug}` path, and NOT the 19-digit
    # tail most slugs end in.
    #
    # That tail is tempting and wrong. The site's own id for this product is
    # `pdpBasicInfo.productID` in a detail page's Apollo cache —
    # 103490518624 where the URL tail is 1731177319241910164 — and 4 of 40
    # listing URLs carry no tail at all (two carry a short hex suffix
    # instead), all four ordinary organic products with identical markup. So
    # a tail-derived sku would have been a different number than the site's,
    # null on a tenth of every run, and nobody would have noticed either.
    #
    # The path is always present, is what the site's own canonical uses, and
    # is what a listing row and a detail row join on. `product_id` below
    # carries the real numeric id where a page states it.
    sku: Optional[str] = None
    title: Optional[str] = None
    # The SHOP. On a marketplace of small sellers the shop IS the brand, and
    # A listing publishes no manufacturer field anywhere, so this column
    # carries the seller's display name instead of being null on every row.
    # Read from the tile on a search page (95/95), from the badge image's alt
    # on a category tile, and from `pdpBasicInfo.shopName` on a detail page;
    # falls back to the slug in the URL, which names the same seller.
    brand: Optional[str] = None
    price: Optional[float] = None
    # IDR, and on this site that is a fact rather than a guess: one
    # storefront, one currency, and the two exit countries tested returned
    # zero price differences across the 68 products both saw. Still null
    # rather than defaulted when no price was found — a row with no price has
    # no currency either.
    currency: Optional[str] = None
    # The was-price, from the tile's strike node. 88 of 95 search tiles carry
    # one; a CATEGORY tile carries none at all (0 of 60), which is a property
    # of that page kind and not a parsing failure. Null in --mode product: a
    # detail page has no strikethrough of its own and the "similar products"
    # carousel's strikes belong to other products.
    original_price: Optional[float] = None
    # Computed from the two prices, never read off the printed badge.
    # A sibling site only prints its badge above some threshold — two tiles carry
    # a strike at 7% and 8% off with no badge — so computing recovers 88 rows
    # where reading recovers 86, and where both exist they agreed on all 86.
    discount_pct: Optional[float] = None
    # A per-item rating, which this site does not publish at all — worth
    # stating because a sibling repo's tile stars are the SHOP's, and folding
    # the two together would make one column mean different things per site.
    # 95/95 on a search page, 0/60 on a category page (that tile kind prints
    # none at all), and `pdpBasicInfo.stats.rating` on a detail page.
    rating: Optional[float] = None
    # Null on every listing row and populated in --mode product, from
    # `stats.countReview`. A tile prints the sold count, never the review
    # count.
    review_count: Optional[int] = None
    # Only a detail page says: `status == "ACTIVE"` with a non-zero
    # `maxOrder`. A tile does not state availability at all, so this is null
    # on a listing run rather than assumed true.
    in_stock: Optional[bool] = None
    # Sparse ON PURPOSE, and this is the trap on this site.
    #
    # A sibling site lazy-loads its tile images, so a tile below the fold carries a
    # PLACEHOLDER in `src`: 55 of 95 search tiles held an SVG under
    # `/zeus_v2/` and 30 of 60 category tiles held a `data:` URI. Reading
    # `src` blindly gives a column that is 100% populated and half wrong, so
    # a real image is recognised positively by its host and everything else
    # is null. Scroll further and more rows fill in.
    image_url: Optional[str] = None
    category: Optional[str] = None
    # Where `price` came from:
    #   "dom"          the rendered tile. The ONLY case on a listing page —
    #                  there is no structured data on one to confirm against,
    #                  which is measured rather than assumed (0 JSON-LD, 0
    #                  __NEXT_DATA__, 0 Apollo state on six captures).
    #   "meta+apollo"  --mode product: the price from the page's own
    #                  `product:price:amount` meta, everything else from its
    #                  Apollo cache.
    #   "meta"         --mode product where the Apollo cache was absent.
    # diff_runs.py reports a price change that comes with a price_source
    # change as `source_changed`, not `changed`: that says something about
    # our own two snapshots, not about the site.
    price_source: Optional[str] = None

    # ---- site-specific, appended so the family prefix stays stable ----
    # Which listing page this row came from (1-based) and its position in
    # that page as the site ordered it. Without `page`, `position` is
    # ambiguous — it restarts at 1 on every page. Both null in --mode
    # product, where there is no page.
    page: Optional[int] = None
    position: Optional[int] = None
    # The seller's URL slug, from the row's own path. Always present, and
    # kept beside `brand` because a shop's display name can change and its
    # slug is what the URL commits to.
    # ---- Catawiki-specific, appended after the family prefix (§9) ----
    # WHICH quantity `price` is. Three states share one node on a listing
    # card and are told apart only by a label in the page's language, which
    # `product_parser` resolves through the dictionary the page itself ships:
    #
    #   current    a live high bid
    #   final      the last bid on a closed lot -- a hammer price only if it
    #              also `sold`; one measured lot reached EUR 1,300 with
    #              `reserve_price_met` false and changed hands for nothing
    #   starting   nobody has bid at all. A FLOOR, not a bid.
    #
    # Without this column one number would mean three things, and every diff
    # between two runs would report a price change when all that happened is
    # that an auction closed.
    bid_kind: Optional[str] = None
    # The label as the page printed it, kept verbatim so a locale whose
    # wording the dictionary lookup did not cover is visible rather than
    # silently null.
    bid_status_text: Optional[str] = None
    # ---- from a lot page; null on a listing row ----
    # The site returns only the last ten bids and states no total, so this
    # is a FLOOR once it reaches ten -- `bid_count_is_floor` is what says
    # which kind of number it is. Two lots with very different activity both
    # reported exactly 10.
    bid_count: Optional[int] = None
    bid_count_is_floor: Optional[bool] = None
    # What the site will accept as the NEXT bid. There is deliberately no
    # `starting_bid` column beside it: the payload's `localizedStartBidAmount`
    # is the same number as `localizedMinBidAmount` on every lot measured
    # (1735/1735, 1400/1400, 1301/1301), so a column named for the OPENING
    # bid would have been a duplicate of this one wearing a misleading name --
    # and on a closed lot it read 1301 against a final price of 1201, an
    # opening bid above the hammer price, which is impossible. On a lot with
    # no bids yet this figure IS the opening bid, and `bid_kind` says so.
    next_min_bid: Optional[float] = None
    # ---- from a listing card; null in --mode lot ----
    # The relative timer, verbatim ("3 days left", "Noch 3\xa0Sekunden").
    # Deliberately NOT parsed into a timestamp: a listing page states no
    # absolute time anywhere, so any conversion would be this machine's clock
    # dressed up as the site's fact. The absolute pair below comes from a lot
    # page, which does state it.
    time_left_text: Optional[str] = None
    bidding_start_at: Optional[str] = None
    bidding_end_at: Optional[str] = None
    reserve_price_set: Optional[bool] = None
    reserve_price_met: Optional[bool] = None
    sold: Optional[bool] = None
    # `buyNow` is an object in the payload (`{"price_eur": 771}`), not a
    # number; the column holds the euro figure.
    buy_now: Optional[float] = None
    has_free_shipping: Optional[bool] = None
    # From the hydrated CARD on a listing, never from the listing payload:
    # the payload's own `favoriteCount` is 0 on 288 of 288 lots across 12
    # captures while the card shows the real number on all 24 of every one.
    # A field that is present, authoritative-looking and uniformly wrong.
    favorite_count: Optional[int] = None
    subtitle: Optional[str] = None
    # The expert's estimate range, euro only. The payload gives min/max per
    # currency with 0 in the entries it does not provide, and 0 there means
    # "not provided" rather than "free".
    estimate_min: Optional[float] = None
    estimate_max: Optional[float] = None
    auction_id: Optional[str] = None
    auction_title: Optional[str] = None
    # Only an auction page states these. A category or search listing gives a
    # relative timer and nothing else, so these stay null there rather than
    # being computed from this machine's clock.
    auction_close_at: Optional[str] = None
    auction_status: Optional[str] = None
    seller_id: Optional[str] = None
    seller_country: Optional[str] = None
    seller_score: Optional[float] = None
    seller_feedback_count: Optional[int] = None
    # Which kind of page this row came off: category, search or lot. Recorded
    # because the repo reads more than one kind and the mode is no longer
    # implied by the source (§9).
    listing_kind: Optional[str] = None

# Row classes by --mode, so an engine maps its mode to a schema in one place.
# Both modes are Product here; the mapping exists so adding a mode later is a
# one-line change rather than a search for every place that assumed Product.
@dataclass
class Auction:
    """One row per AUCTION, for --mode auctions.

    A different KIND of thing from a lot, so it gets its own dataclass rather
    than a Product with most columns null (§9) -- and `sku` carries the id
    here too, so one column name works across the family.

    Deliberately thin. The auctions index publishes an id, a title, a
    curator and a RELATIVE end phrase ("Ending now!", "Ends tomorrow"), and
    nothing absolute; the authority for an auction's status, its absolute
    close time and its lot count is the auction's own page, which also
    carries every one of its lots. So this mode is a WORK LIST -- feed these
    URLs back in as --url and the rows that come out have an absolute
    `auction_close_at` on every one of them.
    """
    source: str = SOURCE_DEFAULT
    scraped_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    url: str = ""
    sku: Optional[str] = None
    title: Optional[str] = None
    # Verbatim and relative, as the index prints it. Not converted to a
    # timestamp: the index states nothing absolute, so any conversion would
    # be this machine's clock presented as the site's fact.
    ends_text: Optional[str] = None
    slug: Optional[str] = None
    locale: Optional[str] = None
    page: Optional[int] = None
    position: Optional[int] = None


ROW_CLASS_BY_MODE = {"listing": Product, "lot": Product, "auctions": Auction}

# Modes whose rows are one-per-sku, and therefore safe to dedupe on `sku` and
# to hand to diff_runs.py. Both of this repo's modes qualify: a listing page
# names each product once, and a product page IS one product.
UNIQUE_BY_SKU_MODES = ("listing", "product")


def dedupe_by_key(rows: Sequence[Any], seen: Set[str], key: str = "sku") -> List[Any]:
    """Drop rows whose key already appeared earlier in this same run.

    `seen` is mutated in place, so callers thread the same set across pages —
    a stale or repeating next-page link then re-parses a page without
    duplicating its rows into the final output. On this site this DOES fire
    on healthy runs: page 1 and page 2 of one category listing shared
    exactly 3 products, all three from the "cheaper products" carousel that
    appears on every page of a listing. So a small non-zero drop count here
    is expected and a large one is not.

    A row with no key is always kept: there is nothing to check a duplicate
    against, and dropping it would be a silent data loss rather than a
    duplicate removal.

    Both of this repo's modes are one row per `sku`, so `key` is never
    overridden here — the parameter exists because the rest of the family
    shares this function and one of them needs it.
    """
    fresh = []
    for r in rows:
        val = getattr(r, key, None)
        if val is None or val not in seen:
            if val is not None:
                seen.add(val)
            fresh.append(r)
    return fresh


# Kept under its old name: the engines and smoke tests in this family all
# call it, and a listing run does dedupe by sku.
def dedupe_by_sku(rows: Sequence[Any], seen: Set[str]) -> List[Any]:
    return dedupe_by_key(rows, seen, key="sku")


# CSV cannot hold a list. Joining with " | " keeps the cell readable in a
# spreadsheet and round-trippable by splitting on the same separator; the
# JSON output keeps the real list, so nothing is lost for a consumer that
# wants structure. `repr()` of a Python list (the default if this is not
# handled) is neither readable nor parseable by anything but Python.
LIST_CSV_SEPARATOR = " | "


def _csv_value(v: Any) -> Any:
    if isinstance(v, (list, tuple)):
        return LIST_CSV_SEPARATOR.join(str(x) for x in v)
    return v


def write_json(rows: Sequence[Any], path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump([asdict(r) for r in rows], f, ensure_ascii=False, indent=2)


def write_csv(rows: Sequence[Any], path: str, row_cls: Type = Product) -> None:
    # An empty result still gets the header row. A zero-byte file makes a
    # consumer fail on read (no columns to parse) instead of reading a valid
    # table with zero rows — and "an empty result is still a well-formed
    # result" is the same principle as `save` refusing to overwrite good data.
    #
    # The header comes from `row_cls`, not from the first row, so an empty
    # run still writes the columns of the mode that produced it.
    fieldnames = [f.name for f in fields(row_cls)]
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: _csv_value(v) for k, v in asdict(r).items()})


# Exit code used when a run completes but produced nothing. Distinct from 1
# (crash) so a caller can tell "ran, found nothing" from "blew up".
EXIT_NO_PRODUCTS = 4

# Exit code for a run blocked by a bot-check/challenge page before parsing
# even started — distinct from EXIT_NO_PRODUCTS so a caller can tell "the
# search genuinely matched nothing" from "something stood between us and the
# content". See product_parser.detect_bot_challenge.
#
# On this site this code specifically does NOT cover the three ways to get a
# real page with no products on it: a `/p/<slug>` discovery hub, which
# answers 200 with banners and carousels and no grid; a search whose query
# matches nothing ("Oops, produk nggak ditemukan"); and one page past the
# end of a category listing. All three are EXIT_NO_PRODUCTS — the request
# was served exactly as asked and simply has no products on it. Reporting
# any of them as blocked would send a user hunting for a proxy problem that
# does not exist.
#
# What EXIT_BLOCKED means here is unusually literal: this site refuses a
# address it has scored NOTHING at all. No status code, no interstitial, no
# vendor marker — the HTTP/2 stream is reset and the run sees a connection
# error rather than a page.
EXIT_BLOCKED = 3

# Exit code for a run that gathered SOME rows and then stopped early — a
# page-load timeout, a 503 throttle, or a challenge on page 3 of 10. The
# output file is still written (throwing away three good pages would be
# worse), but it is not a complete picture, and a consumer that cannot tell
# the difference will read the pages that were never fetched as products that
# disappeared from the catalogue. See write_run_meta.
# A REMOTE service failed — the Scraping Browser refusing the connection
# (`profile_locked` is the common one: a profile allows a single live
# connection), or the Scraper API answering an error. Distinct from 1 (a
# crash in this code) and from 2 (bad usage) because it means "try again, or
# use a different profile", not "there is a bug here". Defined once, here,
# because the browser engines and scraper_api_client.py both return it and
# two definitions of the same code is exactly how a family's exit contract
# drifts.
EXIT_API_ERROR = 5

EXIT_PARTIAL = 6


# Exit code for a run that never GOT its pages: a navigation timeout, a dead
# or unauthenticated proxy, a DNS failure, or an edge answering with
# something that is not the page that was asked for.
#
# Distinct from EXIT_NO_PRODUCTS because those are opposite facts. Exit 4 is
# a statement about the CATALOGUE — "we asked, and the answer was nothing" —
# so handing it to a run that never reached the site tells a pipeline the
# listing is empty when nothing was read at all.
#
# 5 rather than a new number, and 5 rather than EXIT_PARTIAL:
#
#   * this family's contract already reserves 5 for a transport failure
#     (scraper_api_client has used it for a remote API error since it was
#     written), so this needs no new code and no per-repo table for a caller
#     driving more than one of these scrapers;
#   * EXIT_PARTIAL (6) means "some rows were gathered and the output is
#     incomplete". A run holding nothing writes no output at all, so a
#     consumer that reads the file on a 6 finds either nothing or the
#     PREVIOUS run's good data, which `save` deliberately does not
#     overwrite. Exit 5 promises no file.
#
# Deliberately NOT applied when rows WERE gathered: a timeout on page 7 of
# 10 is a partial run (exit 6, output written), which is already right. This
# decides only what a run holding nothing reports.
EXIT_FETCH_FAILED = 5


def write_run_meta(out_prefix: str, meta: dict) -> str:
    """Write a run-metadata sidecar next to the output, return its path.

    Deliberately a separate `<out>.meta.json` rather than columns on every
    row: this describes the RUN, not the product, and repeating it across
    every row would both bloat the output and change the schema every
    consumer of this project already parses.

    diff_runs.py reads it to refuse a comparison between runs that are not
    both complete, and between runs of different `mode`.
    """
    path = f"{out_prefix}.meta.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print(f"[+] Wrote run metadata -> {path} (status={meta.get('status')})")
    return path


def run_meta(status: str, stop_reason: str, pages_requested: int,
             pages_completed: int, start_url: str, final_url: str,
             products: int, pages_failed: Optional[List[int]] = None,
             mode: str = "listing", source: str = SOURCE_DEFAULT,
             extra: Optional[dict] = None) -> dict:
    """Build the metadata dict for a finished run.

    `status` is the field a consumer branches on:
      complete — every requested page was fetched, or the site's own
                 pagination genuinely ran out (nothing more existed to get)
      partial  — rows were gathered, then the run stopped early
      failed   — nothing was gathered at all

    `mode` and `source` are recorded because `mode` is not implied by the
    repo: the same output prefix can hold a listing run or a product run,
    and those populate different columns — `sold` is a FLOOR on a listing
    row and exact on a product row, so diffing one against the other would
    report every row as changed. diff_runs.py refuses a pair whose modes or
    sources differ. `source` is `catawiki.com` on every row of every run
    here, since the site has one storefront and one currency; it is kept
    because consumers read these columns by name across the family.

    `extra` carries facts about the run that are not about any single row.
    `--mode shop` uses it for the SELLER's own name, location, rating and
    review count: a run covers exactly one shop, so those belong to the run
    rather than repeated down a column, and the shop's review count (16679
    on the captured seller) is a different number from its listings' own
    (827 on one of them) — putting them in one column would make the schema
    lie.

    `pages_failed` lists the pages that did not yield data, by number.
    `pages_completed` alone was enough only while pages were fetched strictly
    in order, where "3 of 10 completed" could only mean 1-2-3: a count is not
    a description once pages can be fetched independently and page 3 can fail
    while 4 and 5 succeed. Recording the numbers keeps the sidecar honest
    about WHICH part of the catalogue is missing, not just how much.
    """
    meta = {
        "source": source,
        "mode": mode,
        "status": status,
        "stop_reason": stop_reason,
        "pages_requested": pages_requested,
        "pages_completed": pages_completed,
        "pages_failed": pages_failed or [],
        "products": products,
        "start_url": start_url,
        "final_url": final_url,
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }
    if extra:
        # Merged rather than nested under a key, so a consumer reads
        # `scroll` at the top level beside `products`. Run fields win a
        # name collision: a caller cannot accidentally overwrite `status`.
        meta.update({k: v for k, v in extra.items() if k not in meta})
    return meta


def save(rows: Sequence[Any], out_prefix: str, fmt: str,
         allow_empty: bool = False, row_cls: Type = Product) -> int:
    """Write JSON/CSV and return a process exit code.

    Returns 0 when rows were written, EXIT_NO_PRODUCTS when there were none.
    Callers are expected to exit with it.

    On zero rows, nothing is written at all unless `allow_empty`. Two reasons,
    and a live run demonstrated both. A page-load timeout produced
    `Saved 0 products -> out.json` and exit 0: a two-byte `[]` that a
    consuming pipeline reads as a successful run with no stock. Worse, if the
    file already held a good result from an earlier run, that result is now
    gone — the failure destroyed the last known good data. So an empty result
    leaves the previous file intact and says why.

    `allow_empty=True` is for the legitimate case: a filter that genuinely
    matches nothing, where an empty file is the answer.
    """
    if not rows and not allow_empty:
        print(f"[!] 0 products — refusing to write {out_prefix}.json/.csv, so an "
              f"earlier good result isn't overwritten with an empty one. "
              f"Pass --allow-empty if an empty result is the expected answer.")
        return EXIT_NO_PRODUCTS

    if fmt in ("json", "both"):
        write_json(rows, f"{out_prefix}.json")
        print(f"[+] Saved {len(rows)} products -> {out_prefix}.json")
    if fmt in ("csv", "both"):
        write_csv(rows, f"{out_prefix}.csv", row_cls=row_cls)
        print(f"[+] Saved {len(rows)} products -> {out_prefix}.csv")
    return 0 if rows else EXIT_NO_PRODUCTS


# Stop reasons that mean the run saw everything there was to see. Anything
# else ended the page loop early, so the result is only a partial view.
#
# "no_new_products" belongs here and "pagination_exhausted" is kept for the
# engines that still stop on a missing next-link: the first is a property of
# the DATA (a page contributed nothing not already seen, so the listing is
# over), while the second is a property of a CSS SELECTOR and is therefore
# the weaker signal — a renamed attribute looks identical to a short
# catalogue. On this site that ordering is not a preference, it is the only
# thing that works: the site publishes NO `link[rel=next]` and no numbered
# anchors anywhere, a CATEGORY listing is addressable by `?page=N`, and a
# SEARCH is not addressable at all — `?page=2` there returns an empty result
# set rather than page 2. So "no new products" is the one termination
# condition available on a search. See page_flow.pagination_is_addressable.
#
# "single_page_mode" is complete by construction: --mode product reads one
# page because one page is all there is.
COMPLETE_STOP_REASONS = ("completed", "pagination_exhausted", "no_new_products",
                         "single_page_mode")


def finish_run(rows: Sequence[Any], out_prefix: str, fmt: str,
               allow_empty: bool, *, blocked: bool, stop_reason: str,
               pages_requested: int, pages_completed: int,
               start_url: str, final_url: str,
               pages_failed: Optional[List[int]] = None,
               mode: str = "listing", source: str = SOURCE_DEFAULT,
               extra: Optional[dict] = None) -> int:
    """Write output + the run-metadata sidecar; return the exit code.

    Shared by all three browser engines so the status/exit-code mapping
    cannot drift between them.

    The metadata sidecar is written ONLY when the row file was written.
    Otherwise a failed run would leave a "status": "failed" sidecar next to
    the previous run's still-intact good output (which `save` deliberately
    does not overwrite) — the two files would contradict each other, and
    diff_runs.py would refuse to compare data that is in fact fine.
    """
    # Completeness is decided by the reason AND by the evidence. A named
    # list of stop reasons cannot cover a failure recorded somewhere else,
    # and `pages_failed` is somewhere else: a run whose loop ended for a
    # COMPLETE reason while individual pages failed reported exit 0 and
    # `status: complete` with a non-empty `pages_failed` in the same
    # sidecar — a file that contradicts itself, and a pipeline branching
    # on `status` reading a short run as a whole one.
    #
    # Found by a third-party audit of a sibling repo and measured across
    # the family by CALLING each `finish_run` rather than grepping for the
    # fix: 28 of 32 repos behaved this way. Same shape as the exit-code
    # unification this file already carries — a rule keyed on a list of
    # names has a hole for every name nobody added to it.
    complete = stop_reason in COMPLETE_STOP_REASONS and not pages_failed
    row_cls = ROW_CLASS_BY_MODE.get(mode, Product)
    rc = save(rows, out_prefix, fmt, allow_empty=allow_empty, row_cls=row_cls)
    wrote_output = bool(rows) or allow_empty

    if wrote_output:
        status = "complete" if (rows and complete) else (
            "partial" if rows else "failed")
        write_run_meta(out_prefix, run_meta(
            status=status, stop_reason=stop_reason,
            pages_requested=pages_requested, pages_completed=pages_completed,
            pages_failed=pages_failed, mode=mode, source=source,
            start_url=start_url, final_url=final_url, products=len(rows),
            extra=extra))

    if not rows:
        # Nothing gathered at all, and WHY decides the code. The three
        # outcomes are different facts and a pipeline branches on them
        # (blocked is not empty is not "never reached"):
        #
        #   blocked            something stood between the run and the content
        #   did not complete   we never got the pages — a dead proxy, a load
        #                      timeout, an edge serving something else
        #   completed          we asked, and the answer was nothing
        #
        # Keyed on `not complete` rather than on a list of stop reasons, on
        # purpose: a list cannot cover a reason nobody has added to it yet,
        # so a new one falls silently through to "the catalogue is empty" —
        # which is the defect this branch exists to prevent.
        if blocked:
            return EXIT_BLOCKED
        if not complete:
            print(f"[!] Nothing was gathered and the run did not finish "
                  f"({stop_reason}) — exit {EXIT_FETCH_FAILED}, NOT an empty "
                  f"result (exit {EXIT_NO_PRODUCTS}). Nothing can be "
                  f"concluded about the catalogue from this run.")
            return EXIT_FETCH_FAILED
        return rc
    if not complete:
        print(f"[!] Partial run: stopped after {pages_completed} of "
              f"{pages_requested} page(s) ({stop_reason}). The output holds "
              f"what was gathered, but it is NOT a complete view — see "
              f"{out_prefix}.meta.json.")
        return EXIT_PARTIAL
    return rc
