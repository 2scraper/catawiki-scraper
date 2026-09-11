#!/usr/bin/env python3
"""
diff_runs.py
-------------
Compares two output files from this project (JSON, as written by
output_writer.save) and reports what changed between them, keyed on `sku` —
the identifier the README already tells people to diff on for price
monitoring and assortment tracking, but that nothing in this repo actually
computed.

    python3 diff_runs.py --old watches.2026-09-01.json \\
                          --new watches.2026-09-07.json

Typical use is a scheduled re-run of one of the four scraper engines, kept
under a dated filename, diffed against the previous one:

    python3 playwright_scraper.py --url "$URL" --out "girls_$(date +%F)"
    python3 diff_runs.py --old "girls_$(ls -t girls_*.json | sed -n 2p)" \\
                          --new "girls_$(date +%F).json" --out diff.json

Four buckets, each keyed on sku:

  added          — sku present in --new, absent from --old
  removed        — sku present in --old, absent from --new (delisted, or just
                   off this particular page/category run)
  changed        — sku present in both, with a different price,
                   original_price, discount_pct, currency or in_stock
  source_changed — sku present in both with a different price, but also a
                   different price_source: one run got the DOM-corrected
                   figure and the other the raw JSON-LD one, so the two are
                   not comparable on price. Reported separately because this
                   says something about our own two snapshots, not about the
                   site — and --fail-on-change deliberately ignores it.

A product this project's parser could not recover a sku for (None) cannot be
matched across runs at all, so it is counted and reported separately rather
than silently folded into "added"/"removed", which would be wrong on its face.
"""

import argparse
import json
import pathlib
import re
import sys
from typing import Dict, List, Optional, Tuple

from output_writer import UNIQUE_BY_SKU_MODES

# `sold` is tracked alongside the price, and `sold_is_floor` with it, because
# without the flag a `sold` change is unreadable: a tile's figure is a floor
# the site rounded down ("100rb+ terjual" = 100_000) while a product page's
# is exact (207785 for that same product). A monitor watching `sold` alone
# would report a jump of 107,785 the moment someone diffed a listing run
# against a product run, and none of it would be a sale.
#
# No `price_is_from` / `price_max` here: a Catawiki card prints one amount,
# not a range. If variant pricing ever appears on a listing page, this is
# where it goes.
TRACKED_FIELDS = ("price", "original_price", "discount_pct", "currency",
                  "in_stock", "bid_kind", "sold", "reserve_price_met",
                  "buy_now", "auction_status")

# The subset of TRACKED_FIELDS whose comparability depends on price_source
# matching between the two runs — see diff_products.
PRICE_FIELDS = ("price", "original_price", "discount_pct")


def _load(path: str) -> List[dict]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _by_sku(products: List[dict]) -> Tuple[Dict[str, dict], int]:
    indexed = {}
    unmatchable = 0
    for p in products:
        sku = p.get("sku")
        if sku is None:
            unmatchable += 1
            continue
        # A run's own output can already hold a duplicate sku (two rows in the
        # same category, or a rerun of dedupe_by_sku's job on older output
        # written before it existed) — keep the first and count the rest as
        # unmatchable rather than letting one clobber the other silently.
        if sku in indexed:
            unmatchable += 1
            continue
        indexed[sku] = p
    return indexed, unmatchable


def _within_tolerance(before: dict, after: dict, changes: dict,
                      tolerance_pct: float) -> bool:
    """True if every differing price field moved by less than `tolerance_pct`.

    Inherited from this family rather than earned here, and said plainly
    because the alternative is a comment inventing a reason. A sibling repo
    needs it: that site converts prices for a cross-border visitor and the
    exchange rate ticks between two runs of the same command. NO EQUIVALENT
    CATAWIKI BEHAVIOUR WAS MEASURED — this site quotes euro to every
    visitor, on all 18 locales including Japanese and Chinese and from every
    exit tried, so a run holds no conversion and every cent of a difference
    is a real move.

    What DOES move here is the auction itself, and a tolerance is the wrong
    instrument for that: a bid is not a small drift but a jump to whatever
    the next bidder offered. The `lifecycle` bucket in `diff_products` is
    the right one, and it is exact rather than approximate.

    So the flag stays available and DEFAULTS TO ZERO, which makes it inert
    unless someone deliberately asks for it. Set it to something non-zero
    only with a reason you can state; a price monitor that silently swallows
    small moves is worse than one that cries wolf.

    A move is judged on the LARGEST relative change among the price fields,
    so a genuine 0.5% cut is not hidden by a 0.04% tolerance applied
    field-by-field.
    """
    if tolerance_pct <= 0:
        return False
    for field in PRICE_FIELDS:
        if field not in changes:
            continue
        was, now = before.get(field), after.get(field)
        if not isinstance(was, (int, float)) or not isinstance(now, (int, float)):
            return False  # a None appearing or disappearing is a real change
        if was == 0:
            return False
        if abs(now - was) / abs(was) * 100.0 > tolerance_pct:
            return False
    return True


def diff_products(old: List[dict], new: List[dict],
                  price_tolerance_pct: float = 0.0) -> dict:
    old_by_sku, old_unmatchable = _by_sku(old)
    new_by_sku, new_unmatchable = _by_sku(new)

    added = [new_by_sku[sku] for sku in new_by_sku.keys() - old_by_sku.keys()]
    removed = [old_by_sku[sku] for sku in old_by_sku.keys() - new_by_sku.keys()]

    changed, source_changed, within_tolerance, lifecycle = [], [], [], []
    for sku in old_by_sku.keys() & new_by_sku.keys():
        before, after = old_by_sku[sku], new_by_sku[sku]
        field_changes = {
            field: {"old": before.get(field), "new": after.get(field)}
            for field in TRACKED_FIELDS
            if before.get(field) != after.get(field)
        }
        if not field_changes:
            continue

        # A row whose price_source differs between runs is not comparable on
        # price: here that means one run had its structured price confirmed
        # against a rendered tile ("jsonld+dom") while the other did not
        # ("jsonld"), or fell back to reading the DOM alone ("dom"). The
        # figures should agree, and when they do not, the difference is in
        # how OUR two snapshots rendered, not in what the shop charges.
        # Reporting it as a price change would be a false alarm about the
        # site. Non-price fields still compare fine.
        sources = (before.get("price_source"), after.get("price_source"))
        if sources[0] != sources[1] and any(f in field_changes for f in PRICE_FIELDS):
            price_part = {f: v for f, v in field_changes.items() if f in PRICE_FIELDS}
            other_part = {f: v for f, v in field_changes.items() if f not in PRICE_FIELDS}
            source_changed.append({
                "sku": sku, "title": after.get("title"),
                "price_source": {"old": sources[0], "new": sources[1]},
                "changes": price_part,
            })
            field_changes = other_part
            if not field_changes:
                continue

        # AN AUCTION CLOSED, which is not a price change.
        #
        # This bucket exists because the thing being monitored here is not a
        # shop's price list. A lot's `bid_kind` moves `current` -> `final`
        # when bidding ends, and the amount beside it moves with it -- the
        # last bid is simply higher than whatever the previous run saw. Both
        # differences are one event, and reporting them as a price change
        # would make every overnight diff of a live auction look like a
        # repricing. `--fail-on-change` ignores this bucket for the same
        # reason it ignores `source_changed`: it says something about time
        # passing, not about the site.
        #
        # A move in the other direction (`final` -> `current`) is NOT put
        # here: a closed lot does not reopen, so that is a real anomaly and
        # belongs in `changed` where someone will look at it.
        kinds = (before.get("bid_kind"), after.get("bid_kind"))
        if kinds == ("current", "final") or (kinds[0] == "starting"
                                             and kinds[1] in ("current", "final")):
            lifecycle.append({
                "sku": sku, "title": after.get("title"),
                "bid_kind": {"old": kinds[0], "new": kinds[1]},
                "changes": field_changes,
            })
            continue

        # An FX tick rather than a price change — see _within_tolerance. Only
        # when the ONLY differences are price fields: a currency or stock
        # change alongside is a real change whatever the size of the move.
        if (all(f in PRICE_FIELDS for f in field_changes)
                and _within_tolerance(before, after, field_changes,
                                      price_tolerance_pct)):
            within_tolerance.append({"sku": sku, "title": after.get("title"),
                                     "changes": field_changes})
            continue

        changed.append({"sku": sku, "title": after.get("title"),
                        "changes": field_changes})

    return {
        "added": added,
        "removed": removed,
        "changed": changed,
        "source_changed": source_changed,
        "within_tolerance": within_tolerance,
        "lifecycle": lifecycle,
        "unmatchable_old": old_unmatchable,
        "unmatchable_new": new_unmatchable,
    }


def _print_summary(result: dict) -> None:
    print(f"[+] {len(result['added'])} added, {len(result['removed'])} removed, "
          f"{len(result['changed'])} changed, "
          f"{len(result['source_changed'])} not comparable on price, "
          f"{len(result.get('within_tolerance', []))} within the price "
          f"tolerance, {len(result.get('lifecycle', []))} auction(s) closed.")
    for p in result["added"]:
        print(f"  + {p.get('sku')}  {p.get('title')}  {p.get('price')} {p.get('currency')}")
    for p in result["removed"]:
        print(f"  - {p.get('sku')}  {p.get('title')}  {p.get('price')} {p.get('currency')}")
    for c in result["changed"]:
        deltas = ", ".join(f"{f}: {v['old']!r} -> {v['new']!r}" for f, v in c["changes"].items())
        print(f"  ~ {c['sku']}  {c['title']}  {deltas}")
    for c in result.get("within_tolerance", []):
        moves = ", ".join(
            f"{f}: {v['old']} -> {v['new']}" for f, v in c["changes"].items())
        print(f"  ~ {c['sku']}  {c['title']}  {moves}  [within --price-"
              f"tolerance-pct: an exchange-rate tick, not a price change]")
    for c in result.get("lifecycle", []):
        kind = c["bid_kind"]
        deltas = ", ".join(f"{f}: {v['old']!r} -> {v['new']!r}"
                           for f, v in c["changes"].items())
        print(f"  * {c['sku']}  {c['title']}  {deltas}  "
              f"[bid_kind {kind['old']!r} -> {kind['new']!r}: the auction ran, "
              f"so this is a lifecycle transition rather than a price change]")
    for c in result["source_changed"]:
        src = c["price_source"]
        deltas = ", ".join(f"{f}: {v['old']!r} -> {v['new']!r}" for f, v in c["changes"].items())
        print(f"  ? {c['sku']}  {c['title']}  {deltas}  "
              f"[price_source {src['old']!r} -> {src['new']!r}: the two runs "
              f"rendered differently, so this is not a site-side price change]")
    unmatchable = result["unmatchable_old"] + result["unmatchable_new"]
    if unmatchable:
        print(f"[!] {unmatchable} row(s) across both files had no sku or a "
              f"duplicate sku, and could not be matched across runs.")


def _run_status(path: str) -> Tuple[Optional[str], Optional[dict]]:
    """Read the `<out>.meta.json` sidecar beside a run's JSON output.

    Returns (status, meta), or (None, None) when there is no sidecar — which
    is the normal case for output written before run metadata existed, or by
    `scraper_api_client.py` (single fetch, no pagination to cut short).
    """
    meta_path = re.sub(r"\.json$", "", path) + ".meta.json"
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None, None
    return meta.get("status"), meta


def _check_comparable(args) -> bool:
    """Refuse an assortment diff between runs that are not both complete.

    This is the failure mode the sidecar exists for: a run cut short on page
    3 of 10 is missing every product on pages 4-10, and diffing it against
    yesterday's full run reports all of them as `removed` — reading as "these
    products were delisted" when in fact they were simply never fetched.
    Prices of the SKUs both runs DID see are still comparable, which is why
    this is a refusal with a --force escape hatch rather than a hard error.
    """
    problems = []
    modes = {}
    for label, path in (("--old", args.old), ("--new", args.new)):
        status, meta = _run_status(path)
        if status is None:
            continue  # no sidecar: nothing to check, see _run_status
        mode = (meta or {}).get("mode")
        if mode:
            modes[label] = mode
        if mode and mode not in UNIQUE_BY_SKU_MODES:
            # This tool's whole premise is one row per `sku`, diffed on
            # price. A mode that produces many rows per sku would give a diff
            # whose every line is an artefact of two rows sharing an id, so
            # it is refused outright rather than answered. Both of this
            # repo's current modes qualify; the check is here so that adding
            # one that does not is caught rather than discovered.
            problems.append(
                f"{label} ({path}) is a {mode!r} run, which is not one row "
                f"per sku. This tool diffs one row per sku on price, so there "
                f"is nothing here it can compare.")
        if status != "complete":
            problems.append(
                f"{label} ({path}) was a {status!r} run — stopped after "
                f"{meta.get('pages_completed')} of {meta.get('pages_requested')} "
                f"page(s), reason {meta.get('stop_reason')!r}")
    if len(set(modes.values())) > 1:
        problems.append(
            f"the two runs are different modes ({modes}). A listing row and a "
            f"detail row carry different fields, so `added`/`removed` would "
            f"describe the mode change rather than the catalogue.")

    # A CURRENCY MISMATCH, which on this site should be impossible — and is
    # checked anyway.
    #
    # The sibling repos guard cross-storefront diffs with `source`: eleven
    # country hostnames, so a run of one against another is refused on the
    # hostname alone. Catawiki is ONE host with ONE currency — 18 locales as
    # a path prefix, euro on every one of them, measured 2026-09-10 — so
    # `source` is "catawiki.com" on both sides and there is no storefront
    # split for it to catch.
    #
    # This check is therefore expected never to fire, and it is kept for one
    # reason: if it EVER does, it means either the site has grown a second
    # currency or something in this repo is inventing them, and both of those
    # make every row's price incomparable. A guard that costs nothing and
    # fails loudly beats discovering it from a diff.
    currencies = {}
    for label, path in (("--old", args.old), ("--new", args.new)):
        try:
            rows = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        seen = {r.get("currency") for r in rows if r.get("currency")}
        if len(seen) == 1:
            currencies[label] = seen.pop()
        elif len(seen) > 1:
            problems.append(
                f"{label} ({path}) holds more than one currency ({sorted(seen)}) "
                f"— that run was redirected mid-way and its own prices are not "
                f"comparable with each other, let alone with another run's.")
    if len(set(currencies.values())) > 1:
        problems.append(
            f"the two runs quote different currencies ({currencies}). "
            f"Catawiki quotes euro to every visitor — verified on 18 locales "
            f"and every exit tried — so this should be impossible: either the "
            f"site has grown a second currency or one of these runs invented "
            f"one, and either way every row's price is incomparable. "
            f"`source` cannot catch it: it is 'catawiki.com' on both sides.")

    if not problems:
        return True

    # A generic headline, because the reasons below are no longer only about
    # completeness: a mode mismatch and a reviews run are refused too, and a
    # message naming the wrong reason sends the reader looking in the wrong
    # place.
    print("[!] Refusing to diff these two runs:")
    for line in problems:
        print(f"      {line}")
    print("    Re-run the incomplete side, or pass --force to compare anyway "
          "(added/removed will include products that were simply never "
          "fetched).")
    return False


def parse_args():
    p = argparse.ArgumentParser(
        description="Diff two catawiki-scraper JSON outputs by sku.")
    p.add_argument("--old", required=True, help="Earlier run's JSON output.")
    p.add_argument("--new", required=True, help="Later run's JSON output.")
    p.add_argument("--out", default=None,
                   help="Write the full diff as JSON to this path too.")
    p.add_argument("--price-tolerance-pct", type=float, default=0.0,
                   metavar="PCT",
                   help="Treat a price move smaller than PCT%% as an exchange-"
                        "rate tick rather than a price change: reported "
                        "separately and ignored by --fail-on-change. Default 0 "
                        "(report every cent), which is what a Catawiki "
                        "run wants: the site quotes IDR to every visitor, so "
                        "there is no conversion drift to absorb. The flag is "
                        "inherited from this scraper family; set it non-zero "
                        "only with a reason you can state.")
    p.add_argument("--fail-on-change", action="store_true",
                   help="Exit 1 if anything was added, removed or changed — "
                        "for a cron job that should only notify on a real diff.")
    p.add_argument("--force", action="store_true",
                   help="Diff even when a run's .meta.json says it was partial "
                        "or failed. Products never fetched by the short run will "
                        "appear as added/removed.")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if not args.force and not _check_comparable(args):
        return 2

    try:
        old = _load(args.old)
        new = _load(args.new)
    except (OSError, json.JSONDecodeError) as e:
        print(f"[!] Could not read one of the input files: {e}")
        return 2

    result = diff_products(old, new, price_tolerance_pct=args.price_tolerance_pct)
    _print_summary(result)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"[+] Full diff written to {args.out}")

    # Neither `source_changed` nor `within_tolerance` is a reason to fail.
    # The first means our own two snapshots rendered differently; the second
    # means an exchange rate moved. Neither says anything about the site, and
    # alerting on either would train whoever reads the alert to ignore it.
    if args.fail_on_change and (result["added"] or result["removed"] or result["changed"]):
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(1)
