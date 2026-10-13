"""
output_writer.py
-----------------
Shared row models + JSON/CSV writers used by all three scrapers.

Three modes, two row shapes
---------------------------
    --mode listing   a category, a search or a whole auction -> Product
    --mode lot       one /{loc}/l/{id}-{slug} page -> Product, with the
                     lot-page-only columns populated
    --mode auctions  the auctions index as a work list -> Auction

A lot page is not a different kind of object from a listing card, it is the
same lot described more fully, so listing and lot rows are the SAME class and
`diff_runs.py` can compare a listing run against a lot run on the columns both
populate. An auction is a different kind of thing, so it gets its own class
rather than a Product with most columns null.

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


# The hostname a row came from. Catawiki is ONE storefront: the locale is a
# path segment on one host and prices are euro on every locale, so this column
# is `catawiki.com` on every row of every run. It is kept because the family's
# schema has it in this position and consumers read the columns by name
# across repos.
SOURCE_DEFAULT = "catawiki.com"


@dataclass
class Product:
    source: str = SOURCE_DEFAULT
    scraped_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    url: str = ""
    # The lot id: the number after `/l/` in the lot's URL, which is also the
    # id the page's own payload carries.
    sku: Optional[str] = None
    title: Optional[str] = None
    # A listing publishes no brand field, and splitting the title on its
    # dashes would be a guess presented as a fact, so this is null on a
    # listing row. A lot page's own specifications carry a real Brand, which
    # fills it in --mode lot.
    brand: Optional[str] = None
    # `bid_kind` below says WHICH quantity this is.
    price: Optional[float] = None
    # Euro on every locale. Null rather than defaulted when no price was
    # found: a row with no price has no currency either.
    currency: Optional[str] = None
    # Family columns that this site's parser never sets, so they are null on
    # every row: Catawiki has no was-price, no per-lot rating and no review
    # count. They stay for now so the family's column prefix is unchanged.
    original_price: Optional[float] = None
    discount_pct: Optional[float] = None
    rating: Optional[float] = None
    review_count: Optional[int] = None
    # Set on a lot page only, as "the lot is not closed" when a price or a
    # closed state is known. A listing card does not state it, so it is null
    # on a listing run rather than assumed.
    in_stock: Optional[bool] = None
    # From the listing payload's own image fields (originalImageUrl, else
    # thumbImageUrl) and the lot page's image list. Null when the site
    # published no image url for that lot.
    image_url: Optional[str] = None
    category: Optional[str] = None
    # Where `price` came from:
    #   "next_data+dom"  a listing row: the amount read off the hydrated
    #                    card, everything else from the page's payload.
    #   "next_data"      the payload alone: a lot page, or a listing row
    #                    whose card had not hydrated (so it has no price).
    #   "dom"            the fallback path, read off the card with no payload.
    # diff_runs.py reports a price change that comes with a price_source
    # change as `source_changed`, not `changed`: that says something about
    # our own two snapshots, not about the site.
    price_source: Optional[str] = None

    # ---- site-specific, appended so the family prefix stays stable ----
    # Which listing page this row came from (1-based) and its position in
    # that page as the site ordered it. Without `page`, `position` is
    # ambiguous — it restarts at 1 on every page.
    page: Optional[int] = None
    position: Optional[int] = None
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


# Row classes by --mode, so an engine maps its mode to a schema in one place.
ROW_CLASS_BY_MODE = {"listing": Product, "lot": Product, "auctions": Auction}

# Modes whose rows are one-per-sku, and therefore safe to dedupe on `sku` and
# to hand to diff_runs.py. A listing names each lot once and a lot page IS one
# lot. `auctions` rows are a different class and are refused by the diff.
UNIQUE_BY_SKU_MODES = ("listing", "lot")


def dedupe_by_key(rows: Sequence[Any], seen: Set[str], key: str = "sku") -> List[Any]:
    """Drop rows whose key already appeared earlier in this same run.

    `seen` is mutated in place, so callers thread the same set across pages —
    a stale or repeating next-page link then re-parses a page without
    duplicating its rows into the final output. A small non-zero drop count
    is expected on a healthy run, since a listing page can repeat a lot a
    neighbouring page already carried; a large one is not.

    A row with no key is always kept: there is nothing to check a duplicate
    against, and dropping it would be a silent data loss rather than a
    duplicate removal.

    The modes that use this are one row per `sku`, so `key` is not overridden
    here; the parameter exists because the rest of the family shares this
    function.
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
# This code does NOT cover the ways to get a real page with no products on
# it: a search whose query matches nothing, and one page past the end of a
# listing. Both are EXIT_NO_PRODUCTS: the request was served exactly as asked
# and simply has no products on it. Reporting either as blocked would send a
# user hunting for a proxy problem that does not exist.
#
# What EXIT_BLOCKED means here: Akamai answered with HTTP 403 and a short
# "Access Denied" page that carries nothing to solve, which this site does to
# a headless browser whatever its address. See the README.
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

    `extra` carries facts about the run that are not about any single row,
    such as a lot page's seller metadata.

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
# catalogue. The site publishes no `link[rel=next]`; both listing kinds are
# addressable by `?page=N` (see page_flow.pagination_is_addressable), and "no
# new products" is the termination condition that does not depend on markup.
#
# "single_page_mode" is complete by construction: --mode lot and --mode
# auctions read one page because one page is all there is.
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
    # Found by a third-party audit and measured by CALLING `finish_run`
    # rather than grepping for the fix. Same shape as the exit-code
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
