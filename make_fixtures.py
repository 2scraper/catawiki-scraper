"""Cut the offline suite's fixtures out of real captures, and PROVE they parse
the same.

Its output is what lives in `smoke_test.py`, as string literals. This script
is shipped because two files point at it -- `smoke_test.py`'s own docstring
and TROUBLESHOOTING.md -- and an instruction pointing at a file that does not
exist is worse than no instruction.

WHAT YOU NEED TO RUN IT
-----------------------
Your own captures, in `../captures/` relative to the repo, named as the
`LISTINGS` table below expects. They are deliberately NOT in the repository:
a single capture of this site is 500-1,100 KB, and a real page dump carries
the session that fetched it.

Take them with a browser, not with curl -- this site refuses a headless
browser, so use a real window or `--cdp-endpoint`, and `--dump-html` writes
exactly the bytes the parser was given.

WHAT IT ENFORCES, and why each rule is here
-------------------------------------------
  * every fixture is CUT from a real capture, never hand-written;
  * each one is verified to parse IDENTICALLY to the untrimmed original for
    the lots it keeps -- price, bid kind, title, favourites, reserve, image.
    That check has already earned its keep: a first attempt kept only the
    first locale bundle of the page's own translation store, and the Japanese
    fixture came out with a null `bid_kind` on every row while the full
    capture resolved 21 of 24, because the `ja` page takes its card labels
    from an English fallback bundle;
  * personal material is replaced with obvious placeholders BEFORE anything
    is embedded, and guarded by PATTERNS rather than by the literals one
    capture happened to contain, so the next capture is caught too: a lot
    page's payload carries `highestBidderToken` and a `bidderToken` per bid,
    and an auction card names the human who curated it.
"""
import json, os, re, sys, pathlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import product_parser as P
from bs4 import BeautifulSoup

# Where the captures are. Defaults to `../captures` beside the repo and takes
# an override as argv[1], because the repository itself carries none: a single
# capture of this site is 500-1,100 KB, and a page dump carries the session
# that fetched it.
CAPTURES = pathlib.Path(sys.argv[1] if len(sys.argv) > 1
                        else os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                          "..", "captures"))
if not CAPTURES.is_dir():
    sys.exit("no captures at %s. Take some with --dump-html (a HEADFUL run or "
             "--cdp-endpoint; this site refuses a headless browser), or pass "
             "their directory as the first argument." % CAPTURES)

KEEP = ("lot_status_current_bid", "lot_status_final_bid", "lot_status_starting_bid")
ASSET = ('<img src="https://assets.catawiki.nl/assets/x/thumb2_x.jpg"/>'
         '<link rel="preconnect" href="https://assets.catawiki.nl">')
KEEP_CLASS = re.compile(r'^c-lot-card')

# Patterns, not literals, so the NEXT capture's values are caught too (§10).
SCRUB = [
    (re.compile(r'("(?:highestBidderToken|bidderToken)"\s*:\s*")[0-9a-f]{16,}(")'),
     r'\1PLACEHOLDER-BIDDER-TOKEN\2'),
    (re.compile(r'("(?:anti-csrftoken-a2z|csrfToken|sessionId)"\s*:\s*")[^"]+(")'),
     r'\1PLACEHOLDER\2'),
]


def scrub(text):
    for pattern, repl in SCRUB:
        text = pattern.sub(repl, text)
    return text


def slim(card):
    for tag in card.find_all(True):
        cls = [c for c in (tag.get('class') or []) if KEEP_CLASS.match(c)]
        attrs = {}
        if cls:
            attrs['class'] = cls
        if tag.get('href'):
            attrs['href'] = tag['href']
        if tag.name == 'img':
            attrs['src'] = 'https://assets.catawiki.nl/assets/x/thumb2_x.jpg'
        tag.attrs = attrs
    return str(card)


def listing(capture, url, pick=(0, 1, 2)):
    html = pathlib.Path(capture).read_text()
    props = P.next_data(html)
    key, payload = P.lots_payload(props)
    lots = [payload["lots"][i] for i in pick]
    ids = {l["id"] for l in lots}
    soup = BeautifulSoup(html, "html.parser")
    cards = []
    for card in soup.select(P.SELECTORS["lot_card"]):
        a = card.select_one("a[href]")
        m = re.search(r"/l/(\d+)", a.get("href") or "") if a else None
        if m and int(m.group(1)) in ids:
            cards.append(slim(card))
    store = props["_nextI18Next"]["initialI18nStore"]
    loc = list(store)[0]
    # EVERY bundle, not just the first. The `ja` page ships its own bundle
    # AND an English fallback, and the card's labels come from the fallback --
    # keeping only the first locale produced a fixture whose bid_kind was null
    # on every row while the untrimmed capture resolved 21 of 24. The identity
    # check caught it; a hand-written fixture would have shipped it.
    kept = {lname: {"translations": {k: v for k, v in (b or {}).get("translations", {}).items()
                                    if k in KEEP}}
            for lname, b in store.items()}
    pp = {"lotsPerPage": props.get("lotsPerPage"),
          "currentPage": props.get("currentPage"),
          "_nextI18Next": {"initialI18nStore": kept}}
    if key == "lots":
        pp["lots"] = lots
        a = props.get("auction") or {}
        pp["auction"] = {k: a.get(k) for k in
                         ("id", "title", "status", "startAt", "closeAt", "numberOfLots")}
    else:
        pp[key] = {"lots": lots, "total": payload.get("total"),
                   "meta": payload.get("meta") or {}}
    doc = ('<html lang="%s"><body>%s<script id="__NEXT_DATA__" type="application/json">%s</script>%s</body></html>'
           % (loc, ASSET, scrub(json.dumps({"props": {"pageProps": pp}, "locale": loc},
                                           ensure_ascii=False)), "".join(cards)))
    full = {r.sku: r for r in P.parse_products(html, url, 1)}
    cut = P.parse_products(doc, url, 1)
    same = len(cut) == len(lots) and all(
        (full[r.sku].price, full[r.sku].bid_kind, full[r.sku].title,
         full[r.sku].favorite_count, full[r.sku].reserve_price_set,
         full[r.sku].image_url)
        == (r.price, r.bid_kind, r.title, r.favorite_count,
            r.reserve_price_set, r.image_url)
        for r in cut if r.sku in full)
    return doc, cut, same


def lot(capture, url):
    html = pathlib.Path(capture).read_text()
    props = P.next_data(html)
    d = dict(props["lotDetailsData"])
    b = dict(props["biddingBlockResponse"])
    a = dict(props.get("auction") or {})
    # Keep only what the parser reads, so the fixture is small AND carries no
    # material nobody needs to see.
    d = {k: d.get(k) for k in ("lotId", "lotTitle", "lotSubtitle", "images",
                               "specifications", "expertsEstimate", "sellerInfo",
                               "favoriteCount", "isClosed", "open", "category",
                               "buyNow")}
    # Trim two long, unread subtrees: the estimate's explanatory prose and the
    # seller's badge icons, whose URLs carry 40-hex asset hashes that read as
    # credentials to any scanner looking for them.
    if isinstance(d.get("expertsEstimate"), dict):
        d["expertsEstimate"] = {k: v for k, v in d["expertsEstimate"].items()
                                if k in ("min", "max", "type", "label")}
    if isinstance(d.get("sellerInfo"), dict):
        d["sellerInfo"] = {k: v for k, v in d["sellerInfo"].items()
                           if k in ("id", "url", "address", "score")}
    if isinstance(d.get("images"), list):
        d["images"] = d["images"][:1]
    b = {k: b.get(k) for k in ("localizedCurrentBidAmount", "localizedMinBidAmount",
                               "closed", "sold", "reservePriceMet",
                               "biddingStartTime", "biddingEndTime",
                               "biddingHistory", "highestBidderToken")}
    hist = b.get("biddingHistory") or {}
    if isinstance(hist.get("bids"), list):
        b["biddingHistory"] = {"bids": [{"createdAt": x.get("createdAt"),
                                         "bidderToken": x.get("bidderToken")}
                                        for x in hist["bids"]]}
    a = {k: a.get(k) for k in ("id", "title", "status", "startAt", "closeAt",
                               "numberOfLots")}
    pp = {"lotDetailsData": d, "biddingBlockResponse": b, "auction": a}
    doc = ('<html lang="en"><body>%s<script id="__NEXT_DATA__" type="application/json">%s</script></body></html>'
           % (ASSET, scrub(json.dumps({"props": {"pageProps": pp}}, ensure_ascii=False))))
    before, after = P.parse_lot_page(html, url), P.parse_lot_page(doc, url)
    fields = ("sku", "title", "brand", "price", "bid_kind", "bid_count",
              "bid_count_is_floor", "estimate_min", "estimate_max", "sold",
              "reserve_price_set", "reserve_price_met", "bidding_end_at",
              "seller_country", "seller_score", "favorite_count")
    same = all(getattr(before, f) == getattr(after, f) for f in fields)
    # Two separate signals, deliberately not merged: a parse mismatch means
    # the trim broke the fixture, a leak means the fixture is not publishable.
    leaked = re.search(r'"(?:highestBidderToken|bidderToken)"\s*:\s*"[0-9a-f]{16,}"', doc)
    stray = re.search(r'\b[0-9a-f]{32,}\b', doc)
    return doc, after, same, (leaked.group(0)[:40] if leaked
                              else ("stray hex: " + stray.group(0)[:12] if stray else None))


def auctions(capture, url, n=3):
    html = pathlib.Path(capture).read_text()
    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select(P.AUCTION_CARD_SELECTOR)[:n]
    out = []
    for card in cards:
        for tag in card.find_all(True):
            attrs = {}
            if tag.get("href"):
                attrs["href"] = tag["href"]
            if tag.get("data-testid"):
                attrs["data-testid"] = tag["data-testid"]
            tag.attrs = attrs
        # The curator is a named person; the row does not carry it and the
        # fixture should not either.
        for p_tag in card.find_all("p"):
            if p_tag.string and "urated" in p_tag.get_text():
                p_tag.string = "Curated by PLACEHOLDER NAME"
        out.append(str(card))
    doc = ('<html lang="en"><body>%s%s</body></html>' % (ASSET, "".join(out)))
    rows = P.auction_rows(doc, url, 1)
    return doc, rows, len(rows) == len(cards)


def price_index(capture, want_price=True, count=2):
    """Indices of lots whose CARD has a price, or has none."""
    html = pathlib.Path(capture).read_text()
    props = P.next_data(html)
    _, payload = P.lots_payload(props)
    rows = P.parse_products(html, "https://www.catawiki.com/en/a/1-x", 1)
    by_sku = {r.sku: r for r in rows}
    out = []
    for i, l in enumerate(payload["lots"]):
        r = by_sku.get(str(l["id"]))
        if r and ((r.price is not None) == want_price):
            out.append(i)
        if len(out) >= count:
            break
    return out


fixtures, report = {}, []
LISTINGS = [
    ("LISTING_EN", str(CAPTURES / "cat_en_p1.html"), "https://www.catawiki.com/en/c/333-watches", (0, 1, 2)),
    ("LISTING_DE", str(CAPTURES / "cat_de_p1.html"), "https://www.catawiki.com/de/c/333-armbanduhren", (0, 1, 2)),
    ("LISTING_NL", str(CAPTURES / "cat_nl_p1.html"), "https://www.catawiki.com/nl/c/333-horloges", (0, 1, 2)),
    ("LISTING_PL", str(CAPTURES / "cat_pl_p1.html"), "https://www.catawiki.com/pl/c/333-zegarki", (0, 1, 2)),
    ("LISTING_ZH", str(CAPTURES / "cat_zhhant_p1.html"), "https://www.catawiki.com/zh-Hant/c/333-watches", (0, 1, 2)),
    ("LISTING_JA", str(CAPTURES / "cat_ja_p1.html"), "https://www.catawiki.com/ja/c/333-watches", (0, 1, 2)),
    ("SEARCH_EN", str(CAPTURES / "search_en.html"), "https://www.catawiki.com/en/s?q=rolex", (0, 1, 2)),
    ("SEARCH_NO_RESULTS", str(CAPTURES / "search_en_noresults.html"), "https://www.catawiki.com/en/s?q=zzz", (0, 1, 2)),
]
priced = price_index(str(CAPTURES / "auction_en.html"), True, 2)
unpriced = price_index(str(CAPTURES / "auction_en.html"), False, 1)
LISTINGS.append(("AUCTION_EN", str(CAPTURES / "auction_en.html"),
                 "https://www.catawiki.com/en/a/1243988-figures-figurines-auction",
                 tuple(priced + unpriced)))

for name, cap, url, pick in LISTINGS:
    doc, rows, same = listing(cap, url, pick)
    fixtures[name] = doc
    report.append((name, len(doc), len(rows), same,
                   [r.price for r in rows], [r.bid_kind for r in rows]))

for name, cap, url in [("LOT_CLOSED", str(CAPTURES / "lot_en_closed.html"),
                        "https://www.catawiki.com/en/l/106583855-omega-x"),
                       ("LOT_LIVE", str(CAPTURES / "lot_en_live.html"),
                        "https://www.catawiki.com/en/l/106506005-cartier-x")]:
    doc, row, same, leak = lot(cap, url)
    fixtures[name] = doc
    report.append((name, len(doc), 1, same,
                   [row.price], [row.bid_kind, "leak=%s" % (leak or "none")]))

doc, rows, same = auctions(str(CAPTURES / "auctions_en_scrolled.html"),
                           "https://www.catawiki.com/en/a")
fixtures["AUCTIONS_INDEX"] = doc
report.append(("AUCTIONS_INDEX", len(doc), len(rows), same,
               [r.sku for r in rows], [r.title for r in rows][:1]))

fixtures["BLOCKED_403"] = pathlib.Path(str(CAPTURES / "blocked_403.html")).read_text()
report.append(("BLOCKED_403", len(fixtures["BLOCKED_403"]), 0, True, [], []))

for name, size, n, ok, a, b in report:
    print("%-18s %6d bytes rows=%-3d %s  %s %s"
          % (name, size, n, "identical" if ok else "MISMATCH", a, b))
pathlib.Path(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fixtures_generated.json')).write_text(json.dumps(fixtures, ensure_ascii=False))
print("\n%d bytes in %d fixtures" % (sum(len(v) for v in fixtures.values()), len(fixtures)))
