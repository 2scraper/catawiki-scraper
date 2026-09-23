#!/usr/bin/env python3
"""smoke_test.py - the offline suite for catawiki-scraper.

One file, plain functions, fixtures inline. No pytest, no conftest, no
fixtures directory; `tests/test_smoke.py` wraps this as a single pytest test
so `pytest` works as an entry point without a second copy of the checks.

It must pass with NO engine library installed at all, so every engine import
is guarded and the skip is reported at the end - a suite that silently skips
part of itself and still says "all passed" is the same defect as code that
reports success without checking what it wanted actually happened.

Every fixture below was cut from a real capture by `make_fixtures.py`, which
is shipped beside this file and verifies that the trim parses IDENTICALLY to the
untrimmed original -- price, bid kind, title, favourites, reserve flag and
image url -- before it is embedded here. That check has already earned its
keep: a first attempt kept only the first locale bundle of the page's own
translation store, and the Japanese fixture came out with a null `bid_kind`
on every row while the full capture resolved 21 of 24, because the `ja` page
takes its card labels from an English fallback bundle.

Personal material is replaced with obvious placeholders and guarded by
PATTERNS rather than by the old literals, so the next capture's values are
caught too: a lot page's payload carries `highestBidderToken` and a
`bidderToken` per bid, and an auction card names the human who curated it.
"""
import ast
import builtins
import csv
import inspect
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import threading
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import asdict as dataclasses_asdict, fields

import captcha_solver
from captcha_solver import (CaptchaChallenge, detect_recaptcha_v3,
                            reconcile_detections, _v2_task_for, _redact)
from diff_runs import diff_products
import env_config

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from output_writer import (Product, Auction, save, finish_run, write_csv,
                           dedupe_by_key, dedupe_by_sku, run_meta,
                           ROW_CLASS_BY_MODE, UNIQUE_BY_SKU_MODES,
                           EXIT_BLOCKED, EXIT_NO_PRODUCTS, EXIT_PARTIAL,
                           EXIT_API_ERROR, COMPLETE_STOP_REASONS,
                           LIST_CSV_SEPARATOR)
import product_parser
from product_parser import (parse_products, parse_lot_page, auction_metadata,
                            auction_rows, auction_links, page_url,
                            paginates_by_url, category_from_url, listing_kind,
                            site_host, is_supported_host, host_currency,
                            locale_of, LOCALES, HOSTS, CURRENCY, PAGE_CAP,
                            detect_page_state, detect_bot_challenge,
                            detect_block_marker, served_by_catawiki,
                            is_no_results, page_number_from_url, total_results,
                            total_pages, pages_beyond_cap, search_header,
                            sku_from_url, unsupported_reason, strip_tracking,
                            prices_in, price_in, bid_kind_labels, next_data,
                            lots_payload, SELECTORS, BLOCK_MARKERS,
                            BOT_CHALLENGE_MARKERS)
import page_flow
from proxy_pool import (ProxyPool, mask, to_playwright, split_credentials,
                        parse_proxy_line)

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))

_failures = []


def check(label, condition):
    """Print and record one check. Returns the condition, so callers can
    accumulate with `ok &= check(...)`."""
    if condition:
        print("  PASS  %s" % label)
    else:
        print("  FAIL  %s" % label)
        _failures.append(label)
    return bool(condition)


def group(title):
    print("\n== %s" % title)


def _raises(fn):
    """True if `fn()` raises. Used where refusing is the correct behaviour."""
    try:
        fn()
    except Exception:
        return True
    return False


BANNED_PHRASES = (
    "cloud browser",
    "antidetect browser",
    "anti-detect browser",
    "2scraper Antidetect Browser",
    "gate.2prx.com",
    "2prx.com",
)

# Flags that were removed and must stay removed. Scoped to the ENGINES:
# `--country` is banned on a scraper here -- the locale is a path segment, so
# a flag could only disagree with the URL -- and legitimate on
# fingerprint_client.py, where it picks a fingerprint locale.
REMOVED_ENGINE_FLAGS = ("--antidetect", "--country")
ENGINE_FILES = ("playwright_scraper.py", "puppeteer_scraper.py",
                "selenium_scraper.py")

# A fingerprint as the 2Captcha API really returns one, with this site's
# region. Kept as a fixture because three of the six defects §16 lists were
# one wrong key each in exactly this structure -- `userAgent.value` where the
# API says `userAgent.userAgent`, a locale built as `en-{country}`, a
# timezone never applied at all.
FIX_FINGERPRINT = {
    "id": 1000000,
    "country": "NL",
    "userAgent": {
        "userAgent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/146.0.0.0 Safari/537.36"),
        "platform": "Windows",
        "mobile": False,
    },
    "intl": {
        "contentLocale": "nl-NL",
        "languages": ["nl-NL", "nl", "en-US", "en"],
        "timeZone": "Europe/Amsterdam",
    },
    "screen": {"width": 1920, "height": 1080,
               "outerWidth": 1920, "outerHeight": 992,
               "deviceScaleFactor": 1},
}

CAT_URL = "https://www.catawiki.com/en/c/333-watches"
SEARCH_URL = "https://www.catawiki.com/en/s?q=rolex"
AUCTION_URL = "https://www.catawiki.com/en/a/1243988-figures-figurines-auction"
AUCTIONS_URL = "https://www.catawiki.com/en/a"
LOT_URL = "https://www.catawiki.com/en/l/106583855-omega-x"

# The exact tags the 2Captcha Scraping Browser's auto-solve extension injects
# into every page it loads, copied from a CDP capture of a page the site
# plainly served. Kept verbatim because the question they answer is whether
# our own marker set mistakes them for the site's challenge.
EXTENSION_TAGS = (
    '<script src="chrome-extension://kjmkgkdkpedkejedfhmfcenooemhbpbo/'
    'content/captcha/recaptcha/hunter.js"></script>'
    '<script src="chrome-extension://kjmkgkdkpedkejedfhmfcenooemhbpbo/'
    'content/captcha/turnstile/hunter.js" '
    'data-ts-input="cf-turnstile-response"></script>'
)

# ---------------------------------------------------------------------------
# FIXTURES - cut from real captures, verified identical, personal data scrubbed
# ---------------------------------------------------------------------------
LISTING_EN = """<html lang="en"><body><img src="https://assets.catawiki.nl/assets/x/thumb2_x.jpg"/><link rel="preconnect" href="https://assets.catawiki.nl"><script id="__NEXT_DATA__" type="application/json">{"props": {"pageProps": {"lotsPerPage": 24, "currentPage": 1, "_nextI18Next": {"initialI18nStore": {"en": {"translations": {"lot_status_starting_bid": "Starting bid", "lot_status_current_bid": "Current bid", "lot_status_final_bid": "Final bid"}}}}, "categoryLots": {"lots": [{"id": 106506005, "title": "Cartier - Tank Must de Cartier PM - No reserve price - 5057001 - Unisex - 1990-1999 ", "subtitle": "Quartz - Gold-plated, Silver", "thumbImageUrl": "https://assets.catawiki.nl/assets/2026/8/13/8/9/4/thumb2_89445a08-8c49-46b9-ab5b-d58af7b204d9.jpg", "originalImageUrl": "https://assets.catawiki.nl/assets/2026/8/13/8/9/4/89445a08-8c49-46b9-ab5b-d58af7b204d9.jpg", "favoriteCount": 0, "url": "https://www.catawiki.com/en/l/106506005-cartier-tank-must-de-cartier-pm-no-reserve-price-5057001-unisex-1990-1999", "localized": true, "translatedTitle": null, "translatedSubtitle": null, "auctionId": 1264941, "pubnubChannel": "CWAUCTION-production-1264941", "useRealtimeMessageFallback": false, "use_realtime_message_fallback": false, "isContentExplicit": false, "reservePriceSet": false, "biddingStartTime": "2026-09-04T10:00:00+00:00", "bidding_start_time": "2026-09-04T10:00:00+00:00", "buyNow": null, "hasFreeShipping": false, "isVectorSearchResult": null, "description": null, "sellerId": null, "sellerShopName": null, "live": null}, {"id": 106583855, "title": "Omega - De Ville Prestige Co-Axial \\"Orbis Edition\\" - 424.13.40.20.03.003 - Men - 2010-2020 ", "subtitle": "Automatic - Stainless steel", "thumbImageUrl": "https://assets.catawiki.nl/assets/2026/7/7/c/d/7/thumb2_cd7e70f9-1b50-4287-872b-426c1895538e.jpg", "originalImageUrl": "https://assets.catawiki.nl/assets/2026/7/7/c/d/7/cd7e70f9-1b50-4287-872b-426c1895538e.jpg", "favoriteCount": 0, "url": "https://www.catawiki.com/en/l/106583855-omega-de-ville-prestige-co-axial-orbis-edition-424-13-40-20-03-003-men-2010-2020", "localized": true, "translatedTitle": null, "translatedSubtitle": null, "auctionId": 1263376, "pubnubChannel": "CWAUCTION-production-1263376", "useRealtimeMessageFallback": false, "use_realtime_message_fallback": false, "isContentExplicit": false, "reservePriceSet": true, "biddingStartTime": "2026-09-03T16:00:00+00:00", "bidding_start_time": "2026-09-03T16:00:00+00:00", "buyNow": null, "hasFreeShipping": false, "isVectorSearchResult": null, "description": null, "sellerId": null, "sellerShopName": null, "live": null}, {"id": 106468216, "title": "Seiko - Chronograph - No reserve price - 7T32-6L70 - Men - 2010-2020 ", "subtitle": "Quartz - Stainless steel", "thumbImageUrl": "https://assets.catawiki.nl/assets/2026/8/30/3/3/f/thumb2_33f46f83-dd10-4b0f-8cf6-09e82cc4cd9c.jpg", "originalImageUrl": "https://assets.catawiki.nl/assets/2026/8/30/3/3/f/33f46f83-dd10-4b0f-8cf6-09e82cc4cd9c.jpg", "favoriteCount": 0, "url": "https://www.catawiki.com/en/l/106468216-seiko-chronograph-no-reserve-price-7t32-6l70-men-2010-2020", "localized": true, "translatedTitle": null, "translatedSubtitle": null, "auctionId": 1265196, "pubnubChannel": "CWAUCTION-production-1265196", "useRealtimeMessageFallback": false, "use_realtime_message_fallback": false, "isContentExplicit": false, "reservePriceSet": false, "biddingStartTime": "2026-09-04T10:00:00+00:00", "bidding_start_time": "2026-09-04T10:00:00+00:00", "buyNow": null, "hasFreeShipping": false, "isVectorSearchResult": null, "description": null, "sellerId": null, "sellerShopName": null, "live": null}], "total": 11681, "meta": {"extended_search_result": false}}}}, "locale": "en"}</script><article class="c-lot-card__container"><a class="c-lot-card" href="https://www.catawiki.com/en/l/106506005-cartier-tank-must-de-cartier-pm-no-reserve-price-5057001-unisex-1990-1999"><div class="c-lot-card__image"><img class="c-lot-card__image-element" src="https://assets.catawiki.nl/assets/x/thumb2_x.jpg"/><div class="c-lot-card__image-bottom-left"></div></div><div class="c-lot-card__content"><p class="c-lot-card__title">Cartier - Tank Must de Cartier PM - No reserve price - 5057001 - Unisex - 1990-1999 </p><p class="c-lot-card__status-text">Current bid</p><p class="c-lot-card__price">€1,535</p><div class="c-lot-card__timer"><div><time>1 min left</time><span><strong>+90s</strong></span></div></div></div></a><div class="c-lot-card__top-left"><button><div><svg><defs><clippath><rect></rect></clippath></defs><g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g></g></svg></div><span>144</span></button></div></article><article class="c-lot-card__container"><a class="c-lot-card" href="https://www.catawiki.com/en/l/106583855-omega-de-ville-prestige-co-axial-orbis-edition-424-13-40-20-03-003-men-2010-2020"><div class="c-lot-card__image"><img class="c-lot-card__image-element" src="https://assets.catawiki.nl/assets/x/thumb2_x.jpg"/><div class="c-lot-card__image-bottom-left"></div></div><div class="c-lot-card__content"><p class="c-lot-card__title">Omega - De Ville Prestige Co-Axial "Orbis Edition" - 424.13.40.20.03.003 - Men - 2010-2020 </p><p class="c-lot-card__status-text">​</p><p class="c-lot-card__price">​</p><div class="c-lot-card__timer"><div>Closed for bidding</div></div></div></a><div class="c-lot-card__top-left"><button><div><svg><defs><clippath><rect></rect></clippath></defs><g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g></g></svg></div><span>45</span></button></div></article><article class="c-lot-card__container"><a class="c-lot-card" href="https://www.catawiki.com/en/l/106468216-seiko-chronograph-no-reserve-price-7t32-6l70-men-2010-2020"><div class="c-lot-card__image"><img class="c-lot-card__image-element" src="https://assets.catawiki.nl/assets/x/thumb2_x.jpg"/><div class="c-lot-card__image-bottom-left"></div></div><div class="c-lot-card__content"><p class="c-lot-card__title">Seiko - Chronograph - No reserve price - 7T32-6L70 - Men - 2010-2020 </p><p class="c-lot-card__status-text">Final bid</p><p class="c-lot-card__price">€75</p><div class="c-lot-card__timer"><div>Closed for bidding</div></div></div></a><div class="c-lot-card__top-left"><button><div><svg><defs><clippath><rect></rect></clippath></defs><g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g></g></svg></div><span>6</span></button></div></article></body></html>"""

LISTING_DE = """<html lang="de"><body><img src="https://assets.catawiki.nl/assets/x/thumb2_x.jpg"/><link rel="preconnect" href="https://assets.catawiki.nl"><script id="__NEXT_DATA__" type="application/json">{"props": {"pageProps": {"lotsPerPage": 24, "currentPage": 1, "_nextI18Next": {"initialI18nStore": {"de": {"translations": {"lot_status_starting_bid": "Startpreis ", "lot_status_current_bid": "Aktuelles Gebot", "lot_status_final_bid": "Endgebot"}}}}, "categoryLots": {"lots": [{"id": 106582114, "title": "Omega - De Ville - Ohne mindestpreis - 1450 - Damen - 1990-1999 ", "subtitle": "Quarz - Edelstahl", "thumbImageUrl": "https://assets.catawiki.nl/assets/2026/9/3/0/9/3/thumb2_09308ca7-3291-4647-a197-49308aae5222.jpg", "originalImageUrl": "https://assets.catawiki.nl/assets/2026/9/3/0/9/3/09308ca7-3291-4647-a197-49308aae5222.jpg", "favoriteCount": 0, "url": "https://www.catawiki.com/de/l/106582114-omega-de-ville-ohne-mindestpreis-1450-damen-1990-1999", "localized": true, "translatedTitle": null, "translatedSubtitle": null, "auctionId": 1263430, "pubnubChannel": "CWAUCTION-production-1263430", "useRealtimeMessageFallback": false, "use_realtime_message_fallback": false, "isContentExplicit": false, "reservePriceSet": false, "biddingStartTime": "2026-09-04T10:00:00+00:00", "bidding_start_time": "2026-09-04T10:00:00+00:00", "buyNow": {"price_eur": 771}, "hasFreeShipping": false, "isVectorSearchResult": null, "description": null, "sellerId": null, "sellerShopName": null, "live": null}, {"id": 106605067, "title": "Seiko - Quartz Day-Date  Black Dial - Ohne mindestpreis - 7N43-0BR0 - Herren - 2010–2020 ", "subtitle": "Quarz - Edelstahl", "thumbImageUrl": "https://assets.catawiki.nl/assets/2026/7/2/9/8/e/thumb2_98ec9fd4-8b3a-4116-9157-95c46de465bb.jpg", "originalImageUrl": "https://assets.catawiki.nl/assets/2026/7/2/9/8/e/98ec9fd4-8b3a-4116-9157-95c46de465bb.jpg", "favoriteCount": 0, "url": "https://www.catawiki.com/de/l/106605067-seiko-quartz-day-date-black-dial-ohne-mindestpreis-7n43-0br0-herren-2010-2020", "localized": true, "translatedTitle": null, "translatedSubtitle": null, "auctionId": 1265196, "pubnubChannel": "CWAUCTION-production-1265196", "useRealtimeMessageFallback": false, "use_realtime_message_fallback": false, "isContentExplicit": false, "reservePriceSet": false, "biddingStartTime": "2026-09-04T10:00:00+00:00", "bidding_start_time": "2026-09-04T10:00:00+00:00", "buyNow": null, "hasFreeShipping": false, "isVectorSearchResult": null, "description": null, "sellerId": null, "sellerShopName": null, "live": null}, {"id": 106566224, "title": "Citizen - Citizen - Citizen Promaster Aqualand JP2007-17X - Ohne mindestpreis - JP2007-17X - Herren - 2026", "subtitle": "Quarz - Stahl", "thumbImageUrl": "https://assets.catawiki.nl/assets/2026/8/24/4/c/0/thumb2_4c04f0ed-18fd-4342-9ba3-5ca8c13a4f33.jpg", "originalImageUrl": "https://assets.catawiki.nl/assets/2026/8/24/4/c/0/4c04f0ed-18fd-4342-9ba3-5ca8c13a4f33.jpg", "favoriteCount": 0, "url": "https://www.catawiki.com/de/l/106566224-citizen-citizen-citizen-promaster-aqualand-jp2007-17x-ohne-mindestpreis-jp2007-17x-herren-2026", "localized": true, "translatedTitle": null, "translatedSubtitle": null, "auctionId": 1264557, "pubnubChannel": "CWAUCTION-production-1264557", "useRealtimeMessageFallback": false, "use_realtime_message_fallback": false, "isContentExplicit": false, "reservePriceSet": false, "biddingStartTime": "2026-09-03T16:00:00+00:00", "bidding_start_time": "2026-09-03T16:00:00+00:00", "buyNow": null, "hasFreeShipping": false, "isVectorSearchResult": null, "description": null, "sellerId": null, "sellerShopName": null, "live": null}], "total": 11681, "meta": {"extended_search_result": false}}}}, "locale": "de"}</script><article class="c-lot-card__container"><a class="c-lot-card" href="https://www.catawiki.com/de/l/106582114-omega-de-ville-ohne-mindestpreis-1450-damen-1990-1999"><div class="c-lot-card__image"><img class="c-lot-card__image-element" src="https://assets.catawiki.nl/assets/x/thumb2_x.jpg"/><div class="c-lot-card__image-bottom-left"></div></div><div class="c-lot-card__content"><p class="c-lot-card__title">Omega - De Ville - Ohne mindestpreis - 1450 - Damen - 1990-1999 </p><p class="c-lot-card__status-text">Aktuelles Gebot</p><p class="c-lot-card__price">150 €</p><div class="c-lot-card__timer"><div><time>Noch 1 Sekunde</time></div></div></div></a><div class="c-lot-card__top-left"><button><div><svg><defs><clippath><rect></rect></clippath></defs><g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g></g></svg></div><span>24</span></button></div></article><article class="c-lot-card__container"><a class="c-lot-card" href="https://www.catawiki.com/de/l/106605067-seiko-quartz-day-date-black-dial-ohne-mindestpreis-7n43-0br0-herren-2010-2020"><div class="c-lot-card__image"><img class="c-lot-card__image-element" src="https://assets.catawiki.nl/assets/x/thumb2_x.jpg"/><div class="c-lot-card__image-bottom-left"></div></div><div class="c-lot-card__content"><p class="c-lot-card__title">Seiko - Quartz Day-Date  Black Dial - Ohne mindestpreis - 7N43-0BR0 - Herren - 2010–2020 </p><p class="c-lot-card__status-text">Endgebot</p><p class="c-lot-card__price">35 €</p><div class="c-lot-card__timer"><div>Beendet</div></div></div></a><div class="c-lot-card__top-left"><button><div><svg><defs><clippath><rect></rect></clippath></defs><g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g></g></svg></div><span>14</span></button></div></article><article class="c-lot-card__container"><a class="c-lot-card" href="https://www.catawiki.com/de/l/106566224-citizen-citizen-citizen-promaster-aqualand-jp2007-17x-ohne-mindestpreis-jp2007-17x-herren-2026"><div class="c-lot-card__image"><img class="c-lot-card__image-element" src="https://assets.catawiki.nl/assets/x/thumb2_x.jpg"/><div class="c-lot-card__image-bottom-left"></div></div><div class="c-lot-card__content"><p class="c-lot-card__title">Citizen - Citizen - Citizen Promaster Aqualand JP2007-17X - Ohne mindestpreis - JP2007-17X - Herren - 2026</p><p class="c-lot-card__status-text">Endgebot</p><p class="c-lot-card__price">315 €</p><div class="c-lot-card__timer"><div>Beendet</div></div></div></a><div class="c-lot-card__top-left"><button><div><svg><defs><clippath><rect></rect></clippath></defs><g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g></g></svg></div><span>50</span></button></div></article></body></html>"""

LISTING_NL = """<html lang="nl"><body><img src="https://assets.catawiki.nl/assets/x/thumb2_x.jpg"/><link rel="preconnect" href="https://assets.catawiki.nl"><script id="__NEXT_DATA__" type="application/json">{"props": {"pageProps": {"lotsPerPage": 24, "currentPage": 1, "_nextI18Next": {"initialI18nStore": {"nl": {"translations": {"lot_status_starting_bid": "Openingsbod", "lot_status_current_bid": "Huidig bod", "lot_status_final_bid": "Eindbod"}}}}, "categoryLots": {"lots": [{"id": 106582114, "title": "Omega - De Ville - Zonder minimumprijs - 1450 - Dames - 1990-1999 ", "subtitle": "Quartz - Roestvrij staal", "thumbImageUrl": "https://assets.catawiki.nl/assets/2026/9/3/0/9/3/thumb2_09308ca7-3291-4647-a197-49308aae5222.jpg", "originalImageUrl": "https://assets.catawiki.nl/assets/2026/9/3/0/9/3/09308ca7-3291-4647-a197-49308aae5222.jpg", "favoriteCount": 0, "url": "https://www.catawiki.com/nl/l/106582114-omega-de-ville-zonder-minimumprijs-1450-dames-1990-1999", "localized": true, "translatedTitle": null, "translatedSubtitle": null, "auctionId": 1263430, "pubnubChannel": "CWAUCTION-production-1263430", "useRealtimeMessageFallback": false, "use_realtime_message_fallback": false, "isContentExplicit": false, "reservePriceSet": false, "biddingStartTime": "2026-09-04T10:00:00+00:00", "bidding_start_time": "2026-09-04T10:00:00+00:00", "buyNow": {"price_eur": 771}, "hasFreeShipping": false, "isVectorSearchResult": null, "description": null, "sellerId": null, "sellerShopName": null, "live": null}, {"id": 106605067, "title": "Seiko - Quartz Day-Date  Black Dial - Zonder minimumprijs - 7N43-0BR0 - Heren - 2010-2020 ", "subtitle": "Quartz - Roestvrij staal", "thumbImageUrl": "https://assets.catawiki.nl/assets/2026/7/2/9/8/e/thumb2_98ec9fd4-8b3a-4116-9157-95c46de465bb.jpg", "originalImageUrl": "https://assets.catawiki.nl/assets/2026/7/2/9/8/e/98ec9fd4-8b3a-4116-9157-95c46de465bb.jpg", "favoriteCount": 0, "url": "https://www.catawiki.com/nl/l/106605067-seiko-quartz-day-date-black-dial-zonder-minimumprijs-7n43-0br0-heren-2010-2020", "localized": true, "translatedTitle": null, "translatedSubtitle": null, "auctionId": 1265196, "pubnubChannel": "CWAUCTION-production-1265196", "useRealtimeMessageFallback": false, "use_realtime_message_fallback": false, "isContentExplicit": false, "reservePriceSet": false, "biddingStartTime": "2026-09-04T10:00:00+00:00", "bidding_start_time": "2026-09-04T10:00:00+00:00", "buyNow": null, "hasFreeShipping": false, "isVectorSearchResult": null, "description": null, "sellerId": null, "sellerShopName": null, "live": null}, {"id": 106566224, "title": "Citizen - Citizen - Citizen Promaster Aqualand JP2007-17X - Zonder minimumprijs - JP2007-17X - Heren - 2026", "subtitle": "Quartz - Staal", "thumbImageUrl": "https://assets.catawiki.nl/assets/2026/8/24/4/c/0/thumb2_4c04f0ed-18fd-4342-9ba3-5ca8c13a4f33.jpg", "originalImageUrl": "https://assets.catawiki.nl/assets/2026/8/24/4/c/0/4c04f0ed-18fd-4342-9ba3-5ca8c13a4f33.jpg", "favoriteCount": 0, "url": "https://www.catawiki.com/nl/l/106566224-citizen-citizen-citizen-promaster-aqualand-jp2007-17x-zonder-minimumprijs-jp2007-17x-heren-2026", "localized": true, "translatedTitle": null, "translatedSubtitle": null, "auctionId": 1264557, "pubnubChannel": "CWAUCTION-production-1264557", "useRealtimeMessageFallback": false, "use_realtime_message_fallback": false, "isContentExplicit": false, "reservePriceSet": false, "biddingStartTime": "2026-09-03T16:00:00+00:00", "bidding_start_time": "2026-09-03T16:00:00+00:00", "buyNow": null, "hasFreeShipping": false, "isVectorSearchResult": null, "description": null, "sellerId": null, "sellerShopName": null, "live": null}], "total": 11681, "meta": {"extended_search_result": false}}}}, "locale": "nl"}</script><article class="c-lot-card__container"><a class="c-lot-card" href="https://www.catawiki.com/nl/l/106582114-omega-de-ville-zonder-minimumprijs-1450-dames-1990-1999"><div class="c-lot-card__image"><img class="c-lot-card__image-element" src="https://assets.catawiki.nl/assets/x/thumb2_x.jpg"/><div class="c-lot-card__image-bottom-left"></div></div><div class="c-lot-card__content"><p class="c-lot-card__title">Omega - De Ville - Zonder minimumprijs - 1450 - Dames - 1990-1999 </p><p class="c-lot-card__status-text">Huidig bod</p><p class="c-lot-card__price">€ 150</p><div class="c-lot-card__timer"><div><time>Nog 2 seconden</time></div></div></div></a><div class="c-lot-card__top-left"><button><div><svg><defs><clippath><rect></rect></clippath></defs><g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g></g></svg></div><span>24</span></button></div></article><article class="c-lot-card__container"><a class="c-lot-card" href="https://www.catawiki.com/nl/l/106605067-seiko-quartz-day-date-black-dial-zonder-minimumprijs-7n43-0br0-heren-2010-2020"><div class="c-lot-card__image"><img class="c-lot-card__image-element" src="https://assets.catawiki.nl/assets/x/thumb2_x.jpg"/><div class="c-lot-card__image-bottom-left"></div></div><div class="c-lot-card__content"><p class="c-lot-card__title">Seiko - Quartz Day-Date  Black Dial - Zonder minimumprijs - 7N43-0BR0 - Heren - 2010-2020 </p><p class="c-lot-card__status-text">Huidig bod</p><p class="c-lot-card__price">€ 35</p><div class="c-lot-card__timer"><div><time>Nog 4 seconden</time></div></div></div></a><div class="c-lot-card__top-left"><button><div><svg><defs><clippath><rect></rect></clippath></defs><g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g></g></svg></div><span>14</span></button></div></article><article class="c-lot-card__container"><a class="c-lot-card" href="https://www.catawiki.com/nl/l/106566224-citizen-citizen-citizen-promaster-aqualand-jp2007-17x-zonder-minimumprijs-jp2007-17x-heren-2026"><div class="c-lot-card__image"><img class="c-lot-card__image-element" src="https://assets.catawiki.nl/assets/x/thumb2_x.jpg"/><div class="c-lot-card__image-bottom-left"></div></div><div class="c-lot-card__content"><p class="c-lot-card__title">Citizen - Citizen - Citizen Promaster Aqualand JP2007-17X - Zonder minimumprijs - JP2007-17X - Heren - 2026</p><p class="c-lot-card__status-text">Huidig bod</p><p class="c-lot-card__price">€ 315</p><div class="c-lot-card__timer"><div><time>Nog 4 seconden</time></div></div></div></a><div class="c-lot-card__top-left"><button><div><svg><defs><clippath><rect></rect></clippath></defs><g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g></g></svg></div><span>50</span></button></div></article></body></html>"""

LISTING_PL = """<html lang="pl"><body><img src="https://assets.catawiki.nl/assets/x/thumb2_x.jpg"/><link rel="preconnect" href="https://assets.catawiki.nl"><script id="__NEXT_DATA__" type="application/json">{"props": {"pageProps": {"lotsPerPage": 24, "currentPage": 1, "_nextI18Next": {"initialI18nStore": {"pl": {"translations": {"lot_status_starting_bid": "Cena wywoławcza", "lot_status_current_bid": "Aktualna oferta", "lot_status_final_bid": "Ostateczna oferta"}}}}, "categoryLots": {"lots": [{"id": 106605067, "title": "Seiko - Quartz Day-Date  Black Dial - Bez ceny minimalnej\\r\\n - 7N43-0BR0 - Mężczyzna - 2010-2020 ", "subtitle": "kwarcowy - Stal nierdzewna", "thumbImageUrl": "https://assets.catawiki.nl/assets/2026/7/2/9/8/e/thumb2_98ec9fd4-8b3a-4116-9157-95c46de465bb.jpg", "originalImageUrl": "https://assets.catawiki.nl/assets/2026/7/2/9/8/e/98ec9fd4-8b3a-4116-9157-95c46de465bb.jpg", "favoriteCount": 0, "url": "https://www.catawiki.com/pl/l/106605067-seiko-quartz-day-date-black-dial-bez-ceny-minimalnej-7n43-0br0-mezczyzna-2010-2020", "localized": true, "translatedTitle": null, "translatedSubtitle": null, "auctionId": 1265196, "pubnubChannel": "CWAUCTION-production-1265196", "useRealtimeMessageFallback": false, "use_realtime_message_fallback": false, "isContentExplicit": false, "reservePriceSet": false, "biddingStartTime": "2026-09-04T10:00:00+00:00", "bidding_start_time": "2026-09-04T10:00:00+00:00", "buyNow": null, "hasFreeShipping": false, "isVectorSearchResult": null, "description": null, "sellerId": null, "sellerShopName": null, "live": null}, {"id": 106566224, "title": "Citizen - Citizen - Citizen Promaster Aqualand JP2007-17X - Bez ceny minimalnej\\r\\n - JP2007-17X - Mężczyzna - 2026", "subtitle": "kwarcowy - Stal", "thumbImageUrl": "https://assets.catawiki.nl/assets/2026/8/24/4/c/0/thumb2_4c04f0ed-18fd-4342-9ba3-5ca8c13a4f33.jpg", "originalImageUrl": "https://assets.catawiki.nl/assets/2026/8/24/4/c/0/4c04f0ed-18fd-4342-9ba3-5ca8c13a4f33.jpg", "favoriteCount": 0, "url": "https://www.catawiki.com/pl/l/106566224-citizen-citizen-citizen-promaster-aqualand-jp2007-17x-bez-ceny-minimalnej-jp2007-17x-mezczyzna-2026", "localized": true, "translatedTitle": null, "translatedSubtitle": null, "auctionId": 1264557, "pubnubChannel": "CWAUCTION-production-1264557", "useRealtimeMessageFallback": false, "use_realtime_message_fallback": false, "isContentExplicit": false, "reservePriceSet": false, "biddingStartTime": "2026-09-03T16:00:00+00:00", "bidding_start_time": "2026-09-03T16:00:00+00:00", "buyNow": null, "hasFreeShipping": false, "isVectorSearchResult": null, "description": null, "sellerId": null, "sellerShopName": null, "live": null}, {"id": 106535661, "title": "Franck Muller - Casablanca Cintrée Curvex Chronograph Cherry - 6850 CC AT - Mężczyzna - 1990-1999 ", "subtitle": "Automatyczna - Białe złoto", "thumbImageUrl": "https://assets.catawiki.nl/assets/2026/7/19/3/a/5/thumb2_3a5219a4-4e93-4ce3-b666-bd38740a2ae8.jpg", "originalImageUrl": "https://assets.catawiki.nl/assets/2026/7/19/3/a/5/3a5219a4-4e93-4ce3-b666-bd38740a2ae8.jpg", "favoriteCount": 0, "url": "https://www.catawiki.com/pl/l/106535661-franck-muller-casablanca-cintree-curvex-chronograph-cherry-6850-cc-at-mezczyzna-1990-1999", "localized": true, "translatedTitle": null, "translatedSubtitle": null, "auctionId": 1262955, "pubnubChannel": "CWAUCTION-production-1262955", "useRealtimeMessageFallback": false, "use_realtime_message_fallback": false, "isContentExplicit": false, "reservePriceSet": true, "biddingStartTime": "2026-09-03T10:00:00+00:00", "bidding_start_time": "2026-09-03T10:00:00+00:00", "buyNow": null, "hasFreeShipping": false, "isVectorSearchResult": null, "description": null, "sellerId": null, "sellerShopName": null, "live": null}], "total": 11681, "meta": {"extended_search_result": false}}}}, "locale": "pl"}</script><article class="c-lot-card__container"><a class="c-lot-card" href="https://www.catawiki.com/pl/l/106605067-seiko-quartz-day-date-black-dial-bez-ceny-minimalnej-7n43-0br0-mezczyzna-2010-2020"><div class="c-lot-card__image"><img class="c-lot-card__image-element" src="https://assets.catawiki.nl/assets/x/thumb2_x.jpg"/><div class="c-lot-card__image-bottom-left"></div></div><div class="c-lot-card__content"><p class="c-lot-card__title">Seiko - Quartz Day-Date  Black Dial - Bez ceny minimalnej
 - 7N43-0BR0 - Mężczyzna - 2010-2020 </p><p class="c-lot-card__status-text">Ostateczna oferta</p><p class="c-lot-card__price">€ 35</p><div class="c-lot-card__timer"><div>Licytacja zakończona</div></div></div></a><div class="c-lot-card__top-left"><button><div><svg><defs><clippath><rect></rect></clippath></defs><g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g></g></svg></div><span>14</span></button></div></article><article class="c-lot-card__container"><a class="c-lot-card" href="https://www.catawiki.com/pl/l/106566224-citizen-citizen-citizen-promaster-aqualand-jp2007-17x-bez-ceny-minimalnej-jp2007-17x-mezczyzna-2026"><div class="c-lot-card__image"><img class="c-lot-card__image-element" src="https://assets.catawiki.nl/assets/x/thumb2_x.jpg"/><div class="c-lot-card__image-bottom-left"></div></div><div class="c-lot-card__content"><p class="c-lot-card__title">Citizen - Citizen - Citizen Promaster Aqualand JP2007-17X - Bez ceny minimalnej
 - JP2007-17X - Mężczyzna - 2026</p><p class="c-lot-card__status-text">Ostateczna oferta</p><p class="c-lot-card__price">€ 315</p><div class="c-lot-card__timer"><div>Licytacja zakończona</div></div></div></a><div class="c-lot-card__top-left"><button><div><svg><defs><clippath><rect></rect></clippath></defs><g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g></g></svg></div><span>50</span></button></div></article><article class="c-lot-card__container"><a class="c-lot-card" href="https://www.catawiki.com/pl/l/106535661-franck-muller-casablanca-cintree-curvex-chronograph-cherry-6850-cc-at-mezczyzna-1990-1999"><div class="c-lot-card__image"><img class="c-lot-card__image-element" src="https://assets.catawiki.nl/assets/x/thumb2_x.jpg"/><div class="c-lot-card__image-bottom-left"></div></div><div class="c-lot-card__content"><p class="c-lot-card__title">Franck Muller - Casablanca Cintrée Curvex Chronograph Cherry - 6850 CC AT - Mężczyzna - 1990-1999 </p><p class="c-lot-card__status-text">​</p><p class="c-lot-card__price">​</p><div class="c-lot-card__timer"><div>Licytacja zakończona</div></div></div></a><div class="c-lot-card__top-left"><button><div><svg><defs><clippath><rect></rect></clippath></defs><g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g></g></svg></div><span>37</span></button></div></article></body></html>"""

LISTING_ZH = """<html lang="zh-Hant"><body><img src="https://assets.catawiki.nl/assets/x/thumb2_x.jpg"/><link rel="preconnect" href="https://assets.catawiki.nl"><script id="__NEXT_DATA__" type="application/json">{"props": {"pageProps": {"lotsPerPage": 24, "currentPage": 1, "_nextI18Next": {"initialI18nStore": {"zh-Hant": {"translations": {"lot_status_starting_bid": "開拍價", "lot_status_current_bid": "現時出價", "lot_status_final_bid": "最終出價"}}}}, "categoryLots": {"lots": [{"id": 106573097, "title": "Seiko - ALBA AQPK411 - 沒有保留價 - Seiko ALBA VJ21-KNF0 Quartz Blue Dial Men's Watch - Made in Japan - 男士 - 2010-2020 ", "subtitle": "石英表 - 不銹鋼", "thumbImageUrl": "https://assets.catawiki.nl/assets/2026/7/23/8/0/6/thumb2_80636e8b-1619-442c-96a9-4cf2a4693fac.jpg", "originalImageUrl": "https://assets.catawiki.nl/assets/2026/7/23/8/0/6/80636e8b-1619-442c-96a9-4cf2a4693fac.jpg", "favoriteCount": 0, "url": "https://www.catawiki.com/zh-Hant/l/106573097-seiko-alba-aqpk411-seiko-alba-vj21-knf0-quartz-blue-dial-men-s-watch-made-in-japan-2010-2020", "localized": true, "translatedTitle": null, "translatedSubtitle": null, "auctionId": 1265195, "pubnubChannel": "CWAUCTION-production-1265195", "useRealtimeMessageFallback": false, "use_realtime_message_fallback": false, "isContentExplicit": false, "reservePriceSet": false, "biddingStartTime": "2026-09-03T16:00:00+00:00", "bidding_start_time": "2026-09-03T16:00:00+00:00", "buyNow": null, "hasFreeShipping": false, "isVectorSearchResult": null, "description": null, "sellerId": null, "sellerShopName": null, "live": null}, {"id": 106535661, "title": "Franck Muller - Casablanca Cintrée Curvex Chronograph Cherry - 6850 CC AT - 男士 - 1990-1999 ", "subtitle": "全自動 - 白金", "thumbImageUrl": "https://assets.catawiki.nl/assets/2026/7/19/3/a/5/thumb2_3a5219a4-4e93-4ce3-b666-bd38740a2ae8.jpg", "originalImageUrl": "https://assets.catawiki.nl/assets/2026/7/19/3/a/5/3a5219a4-4e93-4ce3-b666-bd38740a2ae8.jpg", "favoriteCount": 0, "url": "https://www.catawiki.com/zh-Hant/l/106535661-franck-muller-casablanca-cintree-curvex-chronograph-cherry-6850-cc-at-1990-1999", "localized": true, "translatedTitle": null, "translatedSubtitle": null, "auctionId": 1262955, "pubnubChannel": "CWAUCTION-production-1262955", "useRealtimeMessageFallback": false, "use_realtime_message_fallback": false, "isContentExplicit": false, "reservePriceSet": true, "biddingStartTime": "2026-09-03T10:00:00+00:00", "bidding_start_time": "2026-09-03T10:00:00+00:00", "buyNow": null, "hasFreeShipping": false, "isVectorSearchResult": null, "description": null, "sellerId": null, "sellerShopName": null, "live": null}, {"id": 106571580, "title": "Omega - De Ville - Prestige - Co-Axial - Orbis Edition - Automatic - Date - 424.13.40.20.03.003 - 男士 - 2010-2020 ", "subtitle": "全自動 - 鋼", "thumbImageUrl": "https://assets.catawiki.nl/assets/2026/5/28/3/7/a/thumb2_37a876be-18c7-40bd-acd7-2d91d036fb7b.jpg", "originalImageUrl": "https://assets.catawiki.nl/assets/2026/5/28/3/7/a/37a876be-18c7-40bd-acd7-2d91d036fb7b.jpg", "favoriteCount": 0, "url": "https://www.catawiki.com/zh-Hant/l/106571580-omega-de-ville-prestige-co-axial-orbis-edition-automatic-date-424-13-40-20-03-003-2010-2020", "localized": true, "translatedTitle": null, "translatedSubtitle": null, "auctionId": 1263376, "pubnubChannel": "CWAUCTION-production-1263376", "useRealtimeMessageFallback": false, "use_realtime_message_fallback": false, "isContentExplicit": false, "reservePriceSet": true, "biddingStartTime": "2026-09-03T16:00:00+00:00", "bidding_start_time": "2026-09-03T16:00:00+00:00", "buyNow": null, "hasFreeShipping": false, "isVectorSearchResult": null, "description": null, "sellerId": null, "sellerShopName": null, "live": null}], "total": 11675, "meta": {"extended_search_result": false}}}}, "locale": "zh-Hant"}</script><article class="c-lot-card__container"><a class="c-lot-card" href="https://www.catawiki.com/zh-Hant/l/106573097-seiko-alba-aqpk411-seiko-alba-vj21-knf0-quartz-blue-dial-men-s-watch-made-in-japan-2010-2020"><div class="c-lot-card__image"><img class="c-lot-card__image-element" src="https://assets.catawiki.nl/assets/x/thumb2_x.jpg"/><div class="c-lot-card__image-bottom-left"></div></div><div class="c-lot-card__content"><p class="c-lot-card__title">Seiko - ALBA AQPK411 - 沒有保留價 - Seiko ALBA VJ21-KNF0 Quartz Blue Dial Men's Watch - Made in Japan - 男士 - 2010-2020 </p><p class="c-lot-card__status-text">現時出價</p><p class="c-lot-card__price">€27</p><div class="c-lot-card__timer"><div><time>剩餘2秒</time></div></div></div></a><div class="c-lot-card__top-left"><button><div><svg><defs><clippath><rect></rect></clippath></defs><g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g></g></svg></div><span>2</span></button></div></article><article class="c-lot-card__container"><a class="c-lot-card" href="https://www.catawiki.com/zh-Hant/l/106535661-franck-muller-casablanca-cintree-curvex-chronograph-cherry-6850-cc-at-1990-1999"><div class="c-lot-card__image"><img class="c-lot-card__image-element" src="https://assets.catawiki.nl/assets/x/thumb2_x.jpg"/><div class="c-lot-card__image-bottom-left"></div></div><div class="c-lot-card__content"><p class="c-lot-card__title">Franck Muller - Casablanca Cintrée Curvex Chronograph Cherry - 6850 CC AT - 男士 - 1990-1999 </p><p class="c-lot-card__status-text">​</p><p class="c-lot-card__price">​</p><div class="c-lot-card__timer"><div>競投結束</div></div></div></a><div class="c-lot-card__top-left"><button><div><svg><defs><clippath><rect></rect></clippath></defs><g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g></g></svg></div><span>37</span></button></div></article><article class="c-lot-card__container"><a class="c-lot-card" href="https://www.catawiki.com/zh-Hant/l/106571580-omega-de-ville-prestige-co-axial-orbis-edition-automatic-date-424-13-40-20-03-003-2010-2020"><div class="c-lot-card__image"><img class="c-lot-card__image-element" src="https://assets.catawiki.nl/assets/x/thumb2_x.jpg"/><div class="c-lot-card__image-bottom-left"></div></div><div class="c-lot-card__content"><p class="c-lot-card__title">Omega - De Ville - Prestige - Co-Axial - Orbis Edition - Automatic - Date - 424.13.40.20.03.003 - 男士 - 2010-2020 </p><p class="c-lot-card__status-text">現時出價</p><p class="c-lot-card__price">€1,400</p><div class="c-lot-card__timer"><div><time>剩餘4秒</time></div></div></div></a><div class="c-lot-card__top-left"><button><div><svg><defs><clippath><rect></rect></clippath></defs><g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g></g></svg></div><span>33</span></button></div></article></body></html>"""

LISTING_JA = """<html lang="ja"><body><img src="https://assets.catawiki.nl/assets/x/thumb2_x.jpg"/><link rel="preconnect" href="https://assets.catawiki.nl"><script id="__NEXT_DATA__" type="application/json">{"props": {"pageProps": {"lotsPerPage": 24, "currentPage": 1, "_nextI18Next": {"initialI18nStore": {"ja": {"translations": {}}, "en": {"translations": {"lot_status_starting_bid": "Starting bid", "lot_status_current_bid": "Current bid", "lot_status_final_bid": "Final bid"}}}}, "categoryLots": {"lots": [{"id": 106535661, "title": "Franck Muller - Casablanca Cintrée Curvex Chronograph Cherry - 6850 CC AT - Men - 1990-1999 ", "subtitle": "Automatic - White gold", "thumbImageUrl": "https://assets.catawiki.nl/assets/2026/7/19/3/a/5/thumb2_3a5219a4-4e93-4ce3-b666-bd38740a2ae8.jpg", "originalImageUrl": "https://assets.catawiki.nl/assets/2026/7/19/3/a/5/3a5219a4-4e93-4ce3-b666-bd38740a2ae8.jpg", "favoriteCount": 0, "url": "https://www.catawiki.com/ja/l/106535661-franck-muller-casablanca-cintree-curvex-chronograph-cherry-6850-cc-at-men-1990-1999", "localized": true, "translatedTitle": null, "translatedSubtitle": null, "auctionId": 1262955, "pubnubChannel": "CWAUCTION-production-1262955", "useRealtimeMessageFallback": false, "use_realtime_message_fallback": false, "isContentExplicit": false, "reservePriceSet": true, "biddingStartTime": "2026-09-03T10:00:00+00:00", "bidding_start_time": "2026-09-03T10:00:00+00:00", "buyNow": null, "hasFreeShipping": false, "isVectorSearchResult": null, "description": null, "sellerId": null, "sellerShopName": null, "live": null}, {"id": 106605067, "title": "Seiko - Quartz Day-Date  Black Dial - No reserve price - 7N43-0BR0 - Men - 2010-2020 ", "subtitle": "Quartz - Stainless steel", "thumbImageUrl": "https://assets.catawiki.nl/assets/2026/7/2/9/8/e/thumb2_98ec9fd4-8b3a-4116-9157-95c46de465bb.jpg", "originalImageUrl": "https://assets.catawiki.nl/assets/2026/7/2/9/8/e/98ec9fd4-8b3a-4116-9157-95c46de465bb.jpg", "favoriteCount": 0, "url": "https://www.catawiki.com/ja/l/106605067-seiko-quartz-day-date-black-dial-no-reserve-price-7n43-0br0-men-2010-2020", "localized": true, "translatedTitle": null, "translatedSubtitle": null, "auctionId": 1265196, "pubnubChannel": "CWAUCTION-production-1265196", "useRealtimeMessageFallback": false, "use_realtime_message_fallback": false, "isContentExplicit": false, "reservePriceSet": false, "biddingStartTime": "2026-09-04T10:00:00+00:00", "bidding_start_time": "2026-09-04T10:00:00+00:00", "buyNow": null, "hasFreeShipping": false, "isVectorSearchResult": null, "description": null, "sellerId": null, "sellerShopName": null, "live": null}, {"id": 106566224, "title": "Citizen - Citizen - Citizen Promaster Aqualand JP2007-17X - No reserve price - JP2007-17X - Men - 2026", "subtitle": "Quartz - Steel", "thumbImageUrl": "https://assets.catawiki.nl/assets/2026/8/24/4/c/0/thumb2_4c04f0ed-18fd-4342-9ba3-5ca8c13a4f33.jpg", "originalImageUrl": "https://assets.catawiki.nl/assets/2026/8/24/4/c/0/4c04f0ed-18fd-4342-9ba3-5ca8c13a4f33.jpg", "favoriteCount": 0, "url": "https://www.catawiki.com/ja/l/106566224-citizen-citizen-citizen-promaster-aqualand-jp2007-17x-no-reserve-price-jp2007-17x-men-2026", "localized": true, "translatedTitle": null, "translatedSubtitle": null, "auctionId": 1264557, "pubnubChannel": "CWAUCTION-production-1264557", "useRealtimeMessageFallback": false, "use_realtime_message_fallback": false, "isContentExplicit": false, "reservePriceSet": false, "biddingStartTime": "2026-09-03T16:00:00+00:00", "bidding_start_time": "2026-09-03T16:00:00+00:00", "buyNow": null, "hasFreeShipping": false, "isVectorSearchResult": null, "description": null, "sellerId": null, "sellerShopName": null, "live": null}], "total": 11675, "meta": {"extended_search_result": false}}}}, "locale": "ja"}</script><article class="c-lot-card__container"><a class="c-lot-card" href="https://www.catawiki.com/ja/l/106535661-franck-muller-casablanca-cintree-curvex-chronograph-cherry-6850-cc-at-men-1990-1999"><div class="c-lot-card__image"><img class="c-lot-card__image-element" src="https://assets.catawiki.nl/assets/x/thumb2_x.jpg"/><div class="c-lot-card__image-bottom-left"></div></div><div class="c-lot-card__content"><p class="c-lot-card__title">Franck Muller - Casablanca Cintrée Curvex Chronograph Cherry - 6850 CC AT - Men - 1990-1999 </p><p class="c-lot-card__status-text">​</p><p class="c-lot-card__price">​</p><div class="c-lot-card__timer"><div>Closed for bidding</div></div></div></a><div class="c-lot-card__top-left"><button><div><svg><defs><clippath><rect></rect></clippath></defs><g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g></g></svg></div><span>37</span></button></div></article><article class="c-lot-card__container"><a class="c-lot-card" href="https://www.catawiki.com/ja/l/106605067-seiko-quartz-day-date-black-dial-no-reserve-price-7n43-0br0-men-2010-2020"><div class="c-lot-card__image"><img class="c-lot-card__image-element" src="https://assets.catawiki.nl/assets/x/thumb2_x.jpg"/><div class="c-lot-card__image-bottom-left"></div></div><div class="c-lot-card__content"><p class="c-lot-card__title">Seiko - Quartz Day-Date  Black Dial - No reserve price - 7N43-0BR0 - Men - 2010-2020 </p><p class="c-lot-card__status-text">Final bid</p><p class="c-lot-card__price">€35</p><div class="c-lot-card__timer"><div>Closed for bidding</div></div></div></a><div class="c-lot-card__top-left"><button><div><svg><defs><clippath><rect></rect></clippath></defs><g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g></g></svg></div><span>14</span></button></div></article><article class="c-lot-card__container"><a class="c-lot-card" href="https://www.catawiki.com/ja/l/106566224-citizen-citizen-citizen-promaster-aqualand-jp2007-17x-no-reserve-price-jp2007-17x-men-2026"><div class="c-lot-card__image"><img class="c-lot-card__image-element" src="https://assets.catawiki.nl/assets/x/thumb2_x.jpg"/><div class="c-lot-card__image-bottom-left"></div></div><div class="c-lot-card__content"><p class="c-lot-card__title">Citizen - Citizen - Citizen Promaster Aqualand JP2007-17X - No reserve price - JP2007-17X - Men - 2026</p><p class="c-lot-card__status-text">Final bid</p><p class="c-lot-card__price">€315</p><div class="c-lot-card__timer"><div>Closed for bidding</div></div></div></a><div class="c-lot-card__top-left"><button><div><svg><defs><clippath><rect></rect></clippath></defs><g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g></g></svg></div><span>50</span></button></div></article></body></html>"""

SEARCH_EN = """<html lang="en"><body><img src="https://assets.catawiki.nl/assets/x/thumb2_x.jpg"/><link rel="preconnect" href="https://assets.catawiki.nl"><script id="__NEXT_DATA__" type="application/json">{"props": {"pageProps": {"lotsPerPage": 24, "currentPage": 1, "_nextI18Next": {"initialI18nStore": {"en": {"translations": {"lot_status_starting_bid": "Starting bid", "lot_status_current_bid": "Current bid", "lot_status_final_bid": "Final bid"}}}}, "searchLots": {"lots": [{"id": 106667597, "title": "Rolex - Datejust - Serviced by Rolex - 1603 - Men - 1972", "subtitle": "Automatic - Steel", "thumbImageUrl": "https://assets.catawiki.nl/assets/2026/9/7/f/4/6/thumb2_f46a7028-4dc0-488a-ba53-33e893cf09fe.jpg", "originalImageUrl": "https://assets.catawiki.nl/assets/2026/9/7/f/4/6/f46a7028-4dc0-488a-ba53-33e893cf09fe.jpg", "favoriteCount": 0, "url": "https://www.catawiki.com/en/l/106667597-rolex-datejust-serviced-by-rolex-1603-men-1972", "localized": true, "translatedTitle": null, "translatedSubtitle": null, "auctionId": 1263166, "pubnubChannel": "CWAUCTION-production-1263166", "useRealtimeMessageFallback": false, "use_realtime_message_fallback": false, "isContentExplicit": false, "reservePriceSet": true, "biddingStartTime": "2026-09-08T12:00:00+00:00", "bidding_start_time": "2026-09-08T12:00:00+00:00", "buyNow": null, "hasFreeShipping": null, "isVectorSearchResult": false, "description": null, "sellerId": null, "sellerShopName": null, "live": null}, {"id": 106652319, "title": "Rolex - Oyster Perpetual - 126000 - Unisex - 2026", "subtitle": "Automatic - Stainless steel", "thumbImageUrl": "https://assets.catawiki.nl/assets/2026/9/6/9/9/1/thumb2_991abf4a-772c-410d-b965-c8399e3f53ca.jpg", "originalImageUrl": "https://assets.catawiki.nl/assets/2026/9/6/9/9/1/991abf4a-772c-410d-b965-c8399e3f53ca.jpg", "favoriteCount": 0, "url": "https://www.catawiki.com/en/l/106652319-rolex-oyster-perpetual-126000-unisex-2026", "localized": true, "translatedTitle": null, "translatedSubtitle": null, "auctionId": 1263104, "pubnubChannel": "CWAUCTION-production-1263104", "useRealtimeMessageFallback": false, "use_realtime_message_fallback": false, "isContentExplicit": false, "reservePriceSet": true, "biddingStartTime": "2026-09-07T12:00:00+00:00", "bidding_start_time": "2026-09-07T12:00:00+00:00", "buyNow": null, "hasFreeShipping": null, "isVectorSearchResult": false, "description": null, "sellerId": null, "sellerShopName": null, "live": null}, {"id": 106597095, "title": "Rolex - Oyster Perpetual - 6085 - Men - 1953", "subtitle": "Automatic - Stainless steel", "thumbImageUrl": "https://assets.catawiki.nl/assets/2026/9/3/c/f/1/thumb2_cf15fb79-88fe-4268-a928-43e953b77ef8.jpg", "originalImageUrl": "https://assets.catawiki.nl/assets/2026/9/3/c/f/1/cf15fb79-88fe-4268-a928-43e953b77ef8.jpg", "favoriteCount": 0, "url": "https://www.catawiki.com/en/l/106597095-rolex-oyster-perpetual-6085-men-1953", "localized": true, "translatedTitle": null, "translatedSubtitle": null, "auctionId": 1263146, "pubnubChannel": "CWAUCTION-production-1263146", "useRealtimeMessageFallback": false, "use_realtime_message_fallback": false, "isContentExplicit": false, "reservePriceSet": true, "biddingStartTime": "2026-09-07T10:00:00+00:00", "bidding_start_time": "2026-09-07T10:00:00+00:00", "buyNow": null, "hasFreeShipping": null, "isVectorSearchResult": false, "description": null, "sellerId": null, "sellerShopName": null, "live": null}], "total": 681, "meta": {"extended_search_result": false}}}}, "locale": "en"}</script><article class="c-lot-card__container"><a class="c-lot-card" href="https://www.catawiki.com/en/l/106667597-rolex-datejust-serviced-by-rolex-1603-men-1972?po=search&amp;poq=rolex"><div class="c-lot-card__image"><img class="c-lot-card__image-element" src="https://assets.catawiki.nl/assets/x/thumb2_x.jpg"/><div class="c-lot-card__image-bottom-left"></div></div><div class="c-lot-card__content"><p class="c-lot-card__title">Rolex - Datejust - Serviced by Rolex - 1603 - Men - 1972</p><p class="c-lot-card__status-text">Current bid</p><p class="c-lot-card__price">€4,200</p><div class="c-lot-card__timer"><div><time>2 days left</time></div></div></div></a><div class="c-lot-card__top-left"><button><div><svg><defs><clippath><rect></rect></clippath></defs><g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g></g></svg></div><span>20</span></button></div></article><article class="c-lot-card__container"><a class="c-lot-card" href="https://www.catawiki.com/en/l/106652319-rolex-oyster-perpetual-126000-unisex-2026?po=search&amp;poq=rolex"><div class="c-lot-card__image"><img class="c-lot-card__image-element" src="https://assets.catawiki.nl/assets/x/thumb2_x.jpg"/><div class="c-lot-card__image-bottom-left"></div></div><div class="c-lot-card__content"><p class="c-lot-card__title">Rolex - Oyster Perpetual - 126000 - Unisex - 2026</p><p class="c-lot-card__status-text">Current bid</p><p class="c-lot-card__price">€12,500</p><div class="c-lot-card__timer"><div><time>20 hours left</time></div></div></div></a><div class="c-lot-card__top-left"><button><div><svg><defs><clippath><rect></rect></clippath></defs><g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g></g></svg></div><span>57</span></button></div></article><article class="c-lot-card__container"><a class="c-lot-card" href="https://www.catawiki.com/en/l/106597095-rolex-oyster-perpetual-6085-men-1953?po=search&amp;poq=rolex"><div class="c-lot-card__image"><img class="c-lot-card__image-element" src="https://assets.catawiki.nl/assets/x/thumb2_x.jpg"/><div class="c-lot-card__image-bottom-left"></div></div><div class="c-lot-card__content"><p class="c-lot-card__title">Rolex - Oyster Perpetual - 6085 - Men - 1953</p><p class="c-lot-card__status-text">Current bid</p><p class="c-lot-card__price">€1,950</p><div class="c-lot-card__timer"><div><time>1 day left</time></div></div></div></a><div class="c-lot-card__top-left"><button><div><svg><defs><clippath><rect></rect></clippath></defs><g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g></g></svg></div><span>18</span></button></div></article></body></html>"""

SEARCH_NO_RESULTS = """<html lang="en"><body><img src="https://assets.catawiki.nl/assets/x/thumb2_x.jpg"/><link rel="preconnect" href="https://assets.catawiki.nl"><script id="__NEXT_DATA__" type="application/json">{"props": {"pageProps": {"lotsPerPage": 24, "currentPage": 1, "_nextI18Next": {"initialI18nStore": {"en": {"translations": {"lot_status_starting_bid": "Starting bid", "lot_status_current_bid": "Current bid", "lot_status_final_bid": "Final bid"}}}}, "searchLots": {"lots": [{"id": 106350649, "title": "Cartier - Bi-fold wallet", "subtitle": "Bordeaux - Leather, Gold-plated metal", "thumbImageUrl": "https://assets.catawiki.nl/assets/2026/8/26/d/5/b/thumb2_d5b17d39-bb41-41d9-890b-d566d654ba9c.jpg", "originalImageUrl": "https://assets.catawiki.nl/assets/2026/8/26/d/5/b/d5b17d39-bb41-41d9-890b-d566d654ba9c.jpg", "favoriteCount": 0, "url": "https://www.catawiki.com/en/l/106350649-cartier-bi-fold-wallet", "localized": true, "translatedTitle": null, "translatedSubtitle": null, "auctionId": 1261971, "pubnubChannel": "CWAUCTION-production-1261971", "useRealtimeMessageFallback": false, "use_realtime_message_fallback": false, "isContentExplicit": false, "reservePriceSet": false, "biddingStartTime": "2026-09-04T10:00:00+00:00", "bidding_start_time": "2026-09-04T10:00:00+00:00", "buyNow": null, "hasFreeShipping": false, "isVectorSearchResult": true, "description": null, "sellerId": null, "sellerShopName": null, "live": null}, {"id": 106659405, "title": "LEGO Minifigure - sw0131 - Star Wars - Star Wars", "subtitle": "Used - unboxed - Tested and working", "thumbImageUrl": "https://assets.catawiki.nl/assets/2026/9/6/c/4/0/thumb2_c400d9e4-58b8-4b8b-9c60-d8a6e656169d.jpg", "originalImageUrl": "https://assets.catawiki.nl/assets/2026/9/6/c/4/0/c400d9e4-58b8-4b8b-9c60-d8a6e656169d.jpg", "favoriteCount": 0, "url": "https://www.catawiki.com/en/l/106659405-lego-minifigure-sw0131-star-wars-star-wars", "localized": true, "translatedTitle": null, "translatedSubtitle": null, "auctionId": 1256217, "pubnubChannel": "CWAUCTION-production-1256217", "useRealtimeMessageFallback": false, "use_realtime_message_fallback": false, "isContentExplicit": false, "reservePriceSet": true, "biddingStartTime": "2026-09-09T12:00:00+00:00", "bidding_start_time": "2026-09-09T12:00:00+00:00", "buyNow": null, "hasFreeShipping": false, "isVectorSearchResult": true, "description": null, "sellerId": null, "sellerShopName": null, "live": null}, {"id": 106441181, "title": "Star Wars - Master Yoda - jakks pacific - big fig - 50cm - mint condition - Jakks Pacific", "subtitle": "Mint", "thumbImageUrl": "https://assets.catawiki.nl/assets/2024/9/6/b/c/b/thumb2_bcbabbe7-9536-4ffa-b738-6e20572960a8.jpg", "originalImageUrl": "https://assets.catawiki.nl/assets/2024/9/6/b/c/b/bcbabbe7-9536-4ffa-b738-6e20572960a8.jpg", "favoriteCount": 0, "url": "https://www.catawiki.com/en/l/106441181-star-wars-master-yoda-jakks-pacific-big-fig-50cm-mint-condition-jakks-pacific", "localized": true, "translatedTitle": null, "translatedSubtitle": null, "auctionId": 1251559, "pubnubChannel": "CWAUCTION-production-1251559", "useRealtimeMessageFallback": false, "use_realtime_message_fallback": false, "isContentExplicit": false, "reservePriceSet": true, "biddingStartTime": "2026-09-07T10:00:00+00:00", "bidding_start_time": "2026-09-07T10:00:00+00:00", "buyNow": null, "hasFreeShipping": false, "isVectorSearchResult": true, "description": null, "sellerId": null, "sellerShopName": null, "live": null}], "total": 24, "meta": {"extended_search_result": true}}}}, "locale": "en"}</script><article class="c-lot-card__container"><a class="c-lot-card" href="https://www.catawiki.com/en/l/106350649-cartier-bi-fold-wallet?po=search&amp;poq=zzzqxwv-nothing-here"><div class="c-lot-card__image"><img class="c-lot-card__image-element" src="https://assets.catawiki.nl/assets/x/thumb2_x.jpg"/><div class="c-lot-card__image-bottom-left"></div></div><div class="c-lot-card__content"><p class="c-lot-card__title">Cartier - Bi-fold wallet</p><p class="c-lot-card__status-text">Current bid</p><p class="c-lot-card__price">€11</p><div class="c-lot-card__timer"><div><time>3 days left</time></div></div></div></a><div class="c-lot-card__top-left"><button><div><svg><defs><clippath><rect></rect></clippath></defs><g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g></g></svg></div><span>22</span></button></div></article><article class="c-lot-card__container"><a class="c-lot-card" href="https://www.catawiki.com/en/l/106659405-lego-minifigure-sw0131-star-wars-star-wars?po=search&amp;poq=zzzqxwv-nothing-here"><div class="c-lot-card__image"><img class="c-lot-card__image-element" src="https://assets.catawiki.nl/assets/x/thumb2_x.jpg"/><div class="c-lot-card__image-bottom-left"></div></div><div class="c-lot-card__content"><p class="c-lot-card__title">LEGO Minifigure - sw0131 - Star Wars - Star Wars</p><p class="c-lot-card__status-text">Current bid</p><p class="c-lot-card__price">€28</p><div class="c-lot-card__timer"><div><time>7 days left</time></div></div></div></a><div class="c-lot-card__top-left"><button><div><svg><defs><clippath><rect></rect></clippath></defs><g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g></g></svg></div><span>6</span></button></div></article><article class="c-lot-card__container"><a class="c-lot-card" href="https://www.catawiki.com/en/l/106441181-star-wars-master-yoda-jakks-pacific-big-fig-50cm-mint-condition-jakks-pacific?po=search&amp;poq=zzzqxwv-nothing-here"><div class="c-lot-card__image"><img class="c-lot-card__image-element" src="https://assets.catawiki.nl/assets/x/thumb2_x.jpg"/><div class="c-lot-card__image-bottom-left"></div></div><div class="c-lot-card__content"><p class="c-lot-card__title">Star Wars - Master Yoda - jakks pacific - big fig - 50cm - mint condition - Jakks Pacific</p><p class="c-lot-card__status-text">Current bid</p><p class="c-lot-card__price">€155</p><div class="c-lot-card__timer"><div><time>3 days left</time></div></div></div></a><div class="c-lot-card__top-left"><button><div><svg><defs><clippath><rect></rect></clippath></defs><g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g></g></svg></div><span>19</span></button></div></article></body></html>"""

AUCTION_EN = """<html lang="en"><body><img src="https://assets.catawiki.nl/assets/x/thumb2_x.jpg"/><link rel="preconnect" href="https://assets.catawiki.nl"><script id="__NEXT_DATA__" type="application/json">{"props": {"pageProps": {"lotsPerPage": null, "currentPage": null, "_nextI18Next": {"initialI18nStore": {"en": {"translations": {"lot_status_starting_bid": "Starting bid", "lot_status_current_bid": "Current bid", "lot_status_final_bid": "Final bid"}}}}, "lots": [{"id": 106411829, "title": "Statuette, Petit Prince - 22 cm - Bronze", "subtitle": "France", "thumbImageUrl": "https://assets.catawiki.nl/assets/2026/8/27/3/9/1/thumb2_391c779f-ea3a-4c42-8c9e-fb7c698dfb1a.jpg", "originalImageUrl": "https://assets.catawiki.nl/assets/2026/8/27/3/9/1/391c779f-ea3a-4c42-8c9e-fb7c698dfb1a.jpg", "url": "https://www.catawiki.com/en/l/106411829-statuette-petit-prince-22-cm-bronze", "pubnubChannel": "CWAUCTION-production-1243988", "useRealtimeMessageFallback": false, "reservePriceSet": true, "auctionId": 1243988, "isContentExplicit": false, "favoriteCount": 0, "biddingStartTime": null, "bidding_start_time": null, "localized": null, "translatedTitle": null, "translatedSubtitle": null, "hasFreeShipping": null, "isVectorSearchResult": null, "description": null, "sellerId": null, "sellerShopName": null, "buyNow": null, "live": null}, {"id": 106567427, "title": "Sculpture, Naar Johan Coenraad Altorf – Art Deco bronzen uil – Coenrad – JB Déposée Bronze Garanti Paris – - 32.7 cm - Bronze, Marble", "subtitle": "Europe", "thumbImageUrl": "https://assets.catawiki.nl/assets/2026/8/27/d/5/b/thumb2_d5b9df98-b1f6-475d-bae7-312ba96b97d0.jpg", "originalImageUrl": "https://assets.catawiki.nl/assets/2026/8/27/d/5/b/d5b9df98-b1f6-475d-bae7-312ba96b97d0.jpg", "url": "https://www.catawiki.com/en/l/106567427-sculpture-naar-johan-coenraad-altorf-art-deco-bronzen-uil-coenrad-jb-deposee-bronze-garanti-paris-32-7-cm-bronze-marble", "pubnubChannel": "CWAUCTION-production-1243988", "useRealtimeMessageFallback": false, "reservePriceSet": true, "auctionId": 1243988, "isContentExplicit": false, "favoriteCount": 0, "biddingStartTime": null, "bidding_start_time": null, "localized": null, "translatedTitle": null, "translatedSubtitle": null, "hasFreeShipping": null, "isVectorSearchResult": null, "description": null, "sellerId": null, "sellerShopName": null, "buyNow": null, "live": null}, {"id": 106541729, "title": "Statue, Paardenhoofd 43 cm - 43 cm - Resin", "subtitle": "Europe", "thumbImageUrl": "https://assets.catawiki.nl/assets/2026/5/7/4/4/3/thumb2_4433aa26-b58a-43a9-b359-7d2d87bcecf2.jpg", "originalImageUrl": "https://assets.catawiki.nl/assets/2026/5/7/4/4/3/4433aa26-b58a-43a9-b359-7d2d87bcecf2.jpg", "url": "https://www.catawiki.com/en/l/106541729-statue-paardenhoofd-43-cm-43-cm-resin", "pubnubChannel": "CWAUCTION-production-1243988", "useRealtimeMessageFallback": false, "reservePriceSet": true, "auctionId": 1243988, "isContentExplicit": false, "favoriteCount": 0, "biddingStartTime": null, "bidding_start_time": null, "localized": null, "translatedTitle": null, "translatedSubtitle": null, "hasFreeShipping": null, "isVectorSearchResult": null, "description": null, "sellerId": null, "sellerShopName": null, "buyNow": null, "live": null}], "auction": {"id": 1243988, "title": "Figures & Figurines Auction", "status": "closed", "startAt": "2026-09-04T10:00:00Z", "closeAt": "2026-09-10T18:00:00Z", "numberOfLots": 130}}}, "locale": "en"}</script><article class="c-lot-card__container"><a class="c-lot-card" href="https://www.catawiki.com/en/l/106541729-statue-paardenhoofd-43-cm-43-cm-resin"><div class="c-lot-card__image"><img class="c-lot-card__image-element" src="https://assets.catawiki.nl/assets/x/thumb2_x.jpg"/><div class="c-lot-card__image-bottom-left"></div></div><div class="c-lot-card__content"><p class="c-lot-card__title">Statue, Paardenhoofd 43 cm - 43 cm - Resin</p><p class="c-lot-card__status-text">​</p><p class="c-lot-card__price">​</p><div class="c-lot-card__timer"><div>Closed for bidding</div></div></div></a><div class="c-lot-card__top-left"><button><div><svg><defs><clippath><rect></rect></clippath></defs><g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g></g></svg></div><span>3</span></button></div></article><article class="c-lot-card__container"><a class="c-lot-card" href="https://www.catawiki.com/en/l/106411829-statuette-petit-prince-22-cm-bronze"><div class="c-lot-card__image"><img class="c-lot-card__image-element" src="https://assets.catawiki.nl/assets/x/thumb2_x.jpg"/><div class="c-lot-card__image-bottom-left"></div></div><div class="c-lot-card__content"><p class="c-lot-card__title">Statuette, Petit Prince - 22 cm - Bronze</p><p class="c-lot-card__status-text">Final bid</p><p class="c-lot-card__price">€225</p><div class="c-lot-card__timer"><div>Closed for bidding</div></div></div></a><div class="c-lot-card__top-left"><button><div><svg><defs><clippath><rect></rect></clippath></defs><g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g></g></svg></div><span>9</span></button></div></article><article class="c-lot-card__container"><a class="c-lot-card" href="https://www.catawiki.com/en/l/106567427-sculpture-naar-johan-coenraad-altorf-art-deco-bronzen-uil-coenrad-jb-deposee-bronze-garanti-paris-32-7-cm-bronze-marble"><div class="c-lot-card__image"><img class="c-lot-card__image-element" src="https://assets.catawiki.nl/assets/x/thumb2_x.jpg"/><div class="c-lot-card__image-bottom-left"></div></div><div class="c-lot-card__content"><p class="c-lot-card__title">Sculpture, Naar Johan Coenraad Altorf – Art Deco bronzen uil – Coenrad – JB Déposée Bronze Garanti Paris – - 32.7 cm - Bronze, Marble</p><p class="c-lot-card__status-text">Final bid</p><p class="c-lot-card__price">€140</p><div class="c-lot-card__timer"><div>Closed for bidding</div></div></div></a><div class="c-lot-card__top-left"><button><div><svg><defs><clippath><rect></rect></clippath></defs><g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g><g><path></path></g></g></svg></div><span>19</span></button></div></article></body></html>"""

LOT_CLOSED = """<html lang="en"><body><img src="https://assets.catawiki.nl/assets/x/thumb2_x.jpg"/><link rel="preconnect" href="https://assets.catawiki.nl"><script id="__NEXT_DATA__" type="application/json">{"props": {"pageProps": {"lotDetailsData": {"lotId": 106583855, "lotTitle": "Omega - De Ville Prestige Co-Axial \\"Orbis Edition\\" - 424.13.40.20.03.003 - Men - 2010-2020 ", "lotSubtitle": "Automatic - Stainless steel", "images": [{"title": "Omega - De Ville Prestige Co-Axial \\"Orbis Edition\\" - 424.13.40.20.03.003 - Men - 2010-2020 ", "id": "https://assets.catawiki.nl/assets/2026/7/7/c/d/7/cd7e70f9-1b50-4287-872b-426c1895538e.jpg", "large": "https://assets.catawiki.nl/assets/2026/7/7/c/d/7/cd7e70f9-1b50-4287-872b-426c1895538e.jpg", "medium": "https://assets.catawiki.nl/assets/2026/7/7/c/d/7/cd7e70f9-1b50-4287-872b-426c1895538e.jpg", "thumbnail": "https://assets.catawiki.nl/assets/2026/7/7/c/d/7/thumb5_cd7e70f9-1b50-4287-872b-426c1895538e.jpg", "w": 547, "h": 700}], "specifications": [{"collectionUrl": null, "specificationId": 909, "valueId": 60796, "name": "Brand", "value": "Omega", "url": "https://www.catawiki.com/en/c/333-watches?filters=909[]=60796"}, {"collectionUrl": null, "specificationId": 926, "valueId": 60038, "name": "Gender", "value": "Men", "url": "https://www.catawiki.com/en/c/333-watches?filters=926[]=60038"}, {"collectionUrl": null, "specificationId": 966, "valueId": null, "name": "Model", "value": "De Ville Prestige Co-Axial \\"Orbis Edition\\"", "url": null}, {"collectionUrl": null, "specificationId": 1364, "valueId": 165398, "name": "Band material", "value": "Leather", "url": "https://www.catawiki.com/en/c/333-watches?filters=1364[]=165398"}, {"collectionUrl": null, "specificationId": 992, "valueId": null, "name": "Reference number", "value": "424.13.40.20.03.003", "url": null}, {"collectionUrl": null, "specificationId": 1365, "valueId": 165402, "name": "Band length", "value": "Long (over 200 mm)", "url": "https://www.catawiki.com/en/c/333-watches?filters=1365[]=165402"}, {"collectionUrl": null, "specificationId": 1010, "valueId": null, "name": "Shipped insured", "value": "Yes", "url": null}, {"collectionUrl": null, "specificationId": 1366, "valueId": null, "name": "Repainted dial", "value": "No", "url": null}, {"collectionUrl": null, "specificationId": 996, "valueId": 154439, "name": "Period", "value": "2010-2020", "url": "https://www.catawiki.com/en/c/333-watches?filters=996[]=154439"}, {"collectionUrl": null, "specificationId": 998, "valueId": 61725, "name": "Movement", "value": "Automatic", "url": "https://www.catawiki.com/en/c/333-watches?filters=998[]=61725"}, {"collectionUrl": null, "specificationId": 913, "valueId": 61892, "name": "Dial colour", "value": "Blue", "url": "https://www.catawiki.com/en/c/333-watches?filters=913[]=61892"}, {"collectionUrl": null, "specificationId": 932, "valueId": 99821, "name": "Case material", "value": "Stainless steel", "url": "https://www.catawiki.com/en/c/333-watches?filters=932[]=99821"}, {"collectionUrl": null, "specificationId": 1367, "valueId": null, "name": "Original box included", "value": "No", "url": null}, {"collectionUrl": null, "specificationId": 1368, "valueId": null, "name": "Original papers included", "value": "No", "url": null}, {"collectionUrl": null, "specificationId": 1363, "valueId": 165384, "name": "Case diameter", "value": "40 mm", "url": "https://www.catawiki.com/en/c/333-watches?filters=1363[]=165384"}, {"collectionUrl": null, "specificationId": 1369, "valueId": null, "name": "Original warranty included", "value": "No", "url": null}, {"collectionUrl": null, "specificationId": 914, "valueId": 165438, "name": "Condition", "value": "Very good - minor signs of wear", "url": "https://www.catawiki.com/en/c/333-watches?filters=914[]=165438"}], "expertsEstimate": {"label": "Estimate", "min": {"USD": 0, "GBP": 0, "EUR": 2100}, "max": {"USD": 0, "GBP": 0, "EUR": 2400}, "type": "expert"}, "sellerInfo": {"id": 30124450, "url": "https://www.catawiki.com/en/u/30124450-galatawatch", "address": {"country": {"name": "Türkiye", "shortCode": "tr"}}, "score": {"score": 100, "positiveCount": 19, "negativeCount": 0, "neutralCount": 2, "lifetimeCount": 21}}, "favoriteCount": 44, "isClosed": true, "open": false, "category": {"id": 697, "url": "https://www.catawiki.com/en/c/697-omega-watches", "level": 2, "is_messaging_supported": false}, "buyNow": null}, "biddingBlockResponse": {"localizedCurrentBidAmount": 1300, "localizedMinBidAmount": 1400, "closed": true, "sold": false, "reservePriceMet": false, "biddingStartTime": 1788451200000, "biddingEndTime": 1789068236000, "biddingHistory": {"bids": [{"createdAt": "2026-09-10T17:35:39Z", "bidderToken": "PLACEHOLDER-BIDDER-TOKEN"}, {"createdAt": "2026-09-09T19:58:15Z", "bidderToken": "PLACEHOLDER-BIDDER-TOKEN"}, {"createdAt": "2026-09-09T19:58:08Z", "bidderToken": "PLACEHOLDER-BIDDER-TOKEN"}, {"createdAt": "2026-09-09T09:40:57Z", "bidderToken": "PLACEHOLDER-BIDDER-TOKEN"}, {"createdAt": "2026-09-09T07:04:46Z", "bidderToken": "PLACEHOLDER-BIDDER-TOKEN"}, {"createdAt": "2026-09-07T23:46:06Z", "bidderToken": "PLACEHOLDER-BIDDER-TOKEN"}, {"createdAt": "2026-09-07T20:45:37Z", "bidderToken": "PLACEHOLDER-BIDDER-TOKEN"}, {"createdAt": "2026-09-07T17:53:09Z", "bidderToken": "PLACEHOLDER-BIDDER-TOKEN"}, {"createdAt": "2026-09-07T13:00:16Z", "bidderToken": "PLACEHOLDER-BIDDER-TOKEN"}, {"createdAt": "2026-09-06T05:19:03Z", "bidderToken": "PLACEHOLDER-BIDDER-TOKEN"}]}, "highestBidderToken": "PLACEHOLDER-BIDDER-TOKEN"}, "auction": {"id": 1263376, "title": "Omega Watches Auction", "status": "closing_now", "startAt": "2026-09-03T16:00:00Z", "closeAt": "2026-09-10T19:00:00Z", "numberOfLots": 42}}}}</script></body></html>"""

LOT_LIVE = """<html lang="en"><body><img src="https://assets.catawiki.nl/assets/x/thumb2_x.jpg"/><link rel="preconnect" href="https://assets.catawiki.nl"><script id="__NEXT_DATA__" type="application/json">{"props": {"pageProps": {"lotDetailsData": {"lotId": 106506005, "lotTitle": "Cartier - Tank Must de Cartier PM - No reserve price - 5057001 - Unisex - 1990-1999 ", "lotSubtitle": "Quartz - Gold-plated, Silver", "images": [{"title": "Cartier - Tank Must de Cartier PM - No reserve price - 5057001 - Unisex - 1990-1999 ", "id": "https://assets.catawiki.nl/assets/2026/8/13/8/9/4/89445a08-8c49-46b9-ab5b-d58af7b204d9.jpg", "large": "https://assets.catawiki.nl/assets/2026/8/13/8/9/4/89445a08-8c49-46b9-ab5b-d58af7b204d9.jpg", "medium": "https://assets.catawiki.nl/assets/2026/8/13/8/9/4/89445a08-8c49-46b9-ab5b-d58af7b204d9.jpg", "thumbnail": "https://assets.catawiki.nl/assets/2026/8/13/8/9/4/thumb5_89445a08-8c49-46b9-ab5b-d58af7b204d9.jpg", "w": 700, "h": 526}], "specifications": [{"collectionUrl": null, "specificationId": 909, "valueId": 60226, "name": "Brand", "value": "Cartier", "url": "https://www.catawiki.com/en/c/333-watches?filters=909[]=60226"}, {"collectionUrl": null, "specificationId": 926, "valueId": 60041, "name": "Gender", "value": "Unisex", "url": "https://www.catawiki.com/en/c/333-watches?filters=926[]=60041"}, {"collectionUrl": null, "specificationId": 966, "valueId": null, "name": "Model", "value": "Tank Must de Cartier PM", "url": null}, {"collectionUrl": null, "specificationId": 1364, "valueId": 165398, "name": "Band material", "value": "Leather", "url": "https://www.catawiki.com/en/c/333-watches?filters=1364[]=165398"}, {"collectionUrl": null, "specificationId": 992, "valueId": null, "name": "Reference number", "value": "5057001", "url": null}, {"collectionUrl": null, "specificationId": 1365, "valueId": 165400, "name": "Band length", "value": "Short (160–180 mm)", "url": "https://www.catawiki.com/en/c/333-watches?filters=1365[]=165400"}, {"collectionUrl": null, "specificationId": 1010, "valueId": null, "name": "Shipped insured", "value": "Yes", "url": null}, {"collectionUrl": null, "specificationId": 1366, "valueId": null, "name": "Repainted dial", "value": "No", "url": null}, {"collectionUrl": null, "specificationId": 996, "valueId": 70506, "name": "Period", "value": "1990-1999", "url": "https://www.catawiki.com/en/c/333-watches?filters=996[]=70506"}, {"collectionUrl": null, "specificationId": 998, "valueId": 61727, "name": "Movement", "value": "Quartz", "url": "https://www.catawiki.com/en/c/333-watches?filters=998[]=61727"}, {"collectionUrl": null, "specificationId": 913, "valueId": 165409, "name": "Dial colour", "value": "Champagne", "url": "https://www.catawiki.com/en/c/333-watches?filters=913[]=165409"}, {"collectionUrl": null, "specificationId": 932, "valueId": 60012, "name": "Case material", "value": "Gold-plated, Silver", "url": "https://www.catawiki.com/en/c/333-watches?filters=932[]=60012"}, {"collectionUrl": null, "specificationId": 1367, "valueId": null, "name": "Original box included", "value": "No", "url": null}, {"collectionUrl": null, "specificationId": 1368, "valueId": null, "name": "Original papers included", "value": "No", "url": null}, {"collectionUrl": null, "specificationId": 1363, "valueId": 165357, "name": "Case diameter", "value": "20 mm", "url": "https://www.catawiki.com/en/c/333-watches?filters=1363[]=165357"}, {"collectionUrl": null, "specificationId": 1369, "valueId": null, "name": "Original warranty included", "value": "No", "url": null}, {"collectionUrl": null, "specificationId": 914, "valueId": 165438, "name": "Condition", "value": "Very good - minor signs of wear", "url": "https://www.catawiki.com/en/c/333-watches?filters=914[]=165438"}], "expertsEstimate": {"label": "Estimate", "min": {"USD": 0, "GBP": 0, "EUR": 2400}, "max": {"USD": 0, "GBP": 0, "EUR": 2700}, "type": "expert"}, "sellerInfo": {"id": 9740215, "url": "https://www.catawiki.com/en/u/9740215-user-6dc1249", "address": {"country": {"name": "France", "shortCode": "fr"}}, "score": {"score": 99.86, "positiveCount": 716, "negativeCount": 1, "neutralCount": 31, "lifetimeCount": 3622}}, "favoriteCount": 144, "isClosed": true, "open": false, "category": {"id": 855, "url": "https://www.catawiki.com/en/c/855-cartier-watches", "level": 2, "is_messaging_supported": false}, "buyNow": null}, "biddingBlockResponse": {"localizedCurrentBidAmount": 1635, "localizedMinBidAmount": 1735, "closed": true, "sold": true, "reservePriceMet": null, "biddingStartTime": 1788516000000, "biddingEndTime": 1789068434000, "biddingHistory": {"bids": [{"createdAt": "2026-09-10T19:25:41Z", "bidderToken": "PLACEHOLDER-BIDDER-TOKEN"}, {"createdAt": "2026-09-10T19:24:11Z", "bidderToken": "PLACEHOLDER-BIDDER-TOKEN"}, {"createdAt": "2026-09-10T13:58:58Z", "bidderToken": "PLACEHOLDER-BIDDER-TOKEN"}, {"createdAt": "2026-09-10T13:51:45Z", "bidderToken": "PLACEHOLDER-BIDDER-TOKEN"}, {"createdAt": "2026-09-08T08:48:51Z", "bidderToken": "PLACEHOLDER-BIDDER-TOKEN"}, {"createdAt": "2026-09-05T15:58:47Z", "bidderToken": "PLACEHOLDER-BIDDER-TOKEN"}, {"createdAt": "2026-09-05T12:40:05Z", "bidderToken": "PLACEHOLDER-BIDDER-TOKEN"}, {"createdAt": "2026-09-05T12:39:55Z", "bidderToken": "PLACEHOLDER-BIDDER-TOKEN"}, {"createdAt": "2026-09-05T12:39:55Z", "bidderToken": "PLACEHOLDER-BIDDER-TOKEN"}, {"createdAt": "2026-09-05T12:37:51Z", "bidderToken": "PLACEHOLDER-BIDDER-TOKEN"}]}, "highestBidderToken": "PLACEHOLDER-BIDDER-TOKEN"}, "auction": {"id": 1264941, "title": "Cartier Watches Auction • No Reserve", "status": "closing_now", "startAt": "2026-09-04T10:00:00Z", "closeAt": "2026-09-10T19:00:00Z", "numberOfLots": 43}}}}</script></body></html>"""

AUCTIONS_INDEX = """<html lang="en"><body><img src="https://assets.catawiki.nl/assets/x/thumb2_x.jpg"/><link rel="preconnect" href="https://assets.catawiki.nl"><article data-sentry-component="AllAuctionsAuctionCard" data-sentry-source-file="AllAuctionsAuctionCard.tsx" data-testid="all-auctions-card"><article><a href="https://www.catawiki.com/en/a/1243988-figures-figurines-auction"><div><div><img/></div><div><div><img/></div><div><img/></div><div><img/><div><span>+127</span></div></div></div></div><div><p>Curated by PLACEHOLDER NAME</p><h6>Figures &amp; Figurines Auction</h6><div><div><h6>Ending now!</h6></div></div></div></a></article></article><article data-sentry-component="AllAuctionsAuctionCard" data-sentry-source-file="AllAuctionsAuctionCard.tsx" data-testid="all-auctions-card"><article><a href="https://www.catawiki.com/en/a/1249604-risque-art-photography-books-auction-japanese-erotica"><div><div>18+</div><div></div><div><div></div><div></div><div></div></div></div><div><p>Curated by PLACEHOLDER NAME</p><h6>Risqué Art &amp; Photography Books Auction (Japanese Erotica)</h6><div><div><h6>Ending now!</h6></div></div></div></a></article></article><article data-sentry-component="AllAuctionsAuctionCard" data-sentry-source-file="AllAuctionsAuctionCard.tsx" data-testid="all-auctions-card"><article><a href="https://www.catawiki.com/en/a/1249714-modern-contemporary-art-books-auction"><div><div><img/></div><div><div><img/></div><div><img/></div><div><img/><div><span>+120</span></div></div></div></div><div><p>Curated by PLACEHOLDER NAME</p><h6>Modern &amp; Contemporary Art Books Auction</h6><div><div><h6>Ending now!</h6></div></div></div></a></article></article></body></html>"""

BLOCKED_403 = """<HTML><HEAD>
<TITLE>Access Denied</TITLE>
</HEAD><BODY>
<H1>Access Denied</H1>
 
You don't have permission to access "http&#58;&#47;&#47;www&#46;catawiki&#46;com&#47;en&#47;c&#47;333&#45;watches" on this server.<P>
Reference&#32;&#35;18&#46;1dff4817&#46;1789067250&#46;962670
<P>https&#58;&#47;&#47;errors&#46;edgesuite&#46;net&#47;18&#46;1dff4817&#46;1789067250&#46;962670</P>
</BODY>
</HTML>
"""

# The privacy checks collect by the `_FIXTURE` suffix, so both spellings
# exist on purpose: the short name reads better in an assertion, and the
# suffixed one is what test_no_capture_leaks scans. A corpus check that
# can silently scan nothing is worse than no corpus check at all.
LISTING_EN_FIXTURE = LISTING_EN
LISTING_DE_FIXTURE = LISTING_DE
LISTING_NL_FIXTURE = LISTING_NL
LISTING_PL_FIXTURE = LISTING_PL
LISTING_ZH_FIXTURE = LISTING_ZH
LISTING_JA_FIXTURE = LISTING_JA
SEARCH_EN_FIXTURE = SEARCH_EN
SEARCH_NO_RESULTS_FIXTURE = SEARCH_NO_RESULTS
AUCTION_EN_FIXTURE = AUCTION_EN
LOT_CLOSED_FIXTURE = LOT_CLOSED
LOT_LIVE_FIXTURE = LOT_LIVE
AUCTIONS_INDEX_FIXTURE = AUCTIONS_INDEX
BLOCKED_403_FIXTURE = BLOCKED_403


# ---------------------------------------------------------------------------
# Money
# ---------------------------------------------------------------------------
def test_price_parsing():
    group("prices: one currency, five written forms of it")
    ok = True
    # All five seen on the SAME lots across the locale captures, which is why
    # every one of them is pinned rather than sampled.
    cases = [
        ("€1,535", 1535.0, "en: symbol first, comma grouping"),
        ("€ 1.535", 1535.0, "nl/pl: symbol, space, dot grouping"),
        ("1.535 €", 1535.0, "de: symbol last"),
        ("€27", 27.0, "no grouping"),
        ("€1,400", 1400.0, "ja/zh-Hant: still euro, still comma"),
        ("€10,950", 10950.0, "five figures"),
        ("€ 1.234,56", 1234.56, "dot grouping with real cents"),
        ("€1,234.56", 1234.56, "comma grouping with real cents"),
    ]
    for text, want, why in cases:
        got = price_in(text)
        ok &= check("%r -> %s (%s)" % (text, want, why), got == want)

    # A no-break space is what a rendered page actually uses so the number
    # does not wrap, and missing it parses "1 535" as 535.
    for space, name in ((" ", "plain"), (" ", "NBSP"),
                        (" ", "narrow NBSP"), (" ", "thin")):
        ok &= check("1%s535 € with a %s space -> 1535" % (space, name),
                    price_in("1%s535 €" % space) == 1535.0)

    # Percentages come out BEFORE prices are matched, not after: a rejected
    # match has already consumed the symbol, so filtering afterwards loses
    # the real price too.
    ok &= check("a -16% badge beside €150 does not become the price",
                price_in("-16% €150") == 150.0)
    ok &= check("a -%10,34 badge (Turkish word order) is skipped too",
                price_in("-%10,34 €25.999") == 25999.0)
    ok &= check("prices_in keeps reading order",
                prices_in("€10 and €20") == [10.0, 20.0])
    ok &= check("no euro amount -> no price", price_in("Current bid") is None)

    # The site's own empty-price placeholder is a ZERO-WIDTH SPACE, not an
    # empty node: `.c-lot-card__price` is present on 24 of 24 cards while 2-3
    # of them hold nothing at all. A truthiness check on the node reports
    # 100% coverage and writes an invisible character into every row.
    for char, name in (("​", "U+200B"), ("‌", "U+200C"),
                       ("﻿", "U+FEFF")):
        ok &= check("a bare %s reads as no price, not as a price" % name,
                    price_in(char) is None)
    return ok


# ---------------------------------------------------------------------------
# What `price` MEANS on an auction site
# ---------------------------------------------------------------------------
def test_bid_state():
    group("bid state resolved through the page's own dictionary")
    ok = True
    # Same three keys in every locale, and the labels come from the page
    # rather than from a table written here - which is the whole point, since
    # a table would need 18 languages and would go stale on a retranslation.
    expected = {
        "LISTING_EN": {"Current bid": "current", "Final bid": "final",
                       "Starting bid": "starting"},
        "LISTING_DE": {"Aktuelles Gebot": "current", "Endgebot": "final"},
        "LISTING_NL": {"Huidig bod": "current", "Eindbod": "final"},
        "LISTING_PL": {"Aktualna oferta": "current",
                       "Ostateczna oferta": "final"},
        "LISTING_ZH": {"現時出價": "current",
                       "最終出價": "final"},
    }
    for name, want in expected.items():
        labels = bid_kind_labels(next_data(globals()[name]))
        for label, kind in want.items():
            ok &= check("%s: %r -> %s" % (name, label, kind),
                        labels.get(label) == kind)

    # The disambiguation that makes the approach necessary rather than merely
    # tidy: two keys read "Current bid" in English, and in zh-Hant they are
    # different strings. A table built from the English page would have
    # matched nothing in Chinese and left bid_kind null on every row.
    zh = bid_kind_labels(next_data(LISTING_ZH))
    ok &= check("zh-Hant uses the CARD's key (現時出價), "
                "not auction_current_bid (當前出價)",
                zh.get("現時出價") == "current"
                and "當前出價" not in zh)

    # Three quantities, one column, and the label is the only thing telling
    # them apart. `starting` is a FLOOR nobody has bid.
    rows = parse_products(LISTING_EN, CAT_URL, 1)
    kinds = {r.bid_kind for r in rows}
    ok &= check("a listing row carries bid_kind or nothing, never a guess",
                kinds <= {"current", "final", "starting", None})
    ok &= check("a row with no price has no bid_kind either",
                all(r.bid_kind is None for r in rows if r.price is None))
    return ok


# ---------------------------------------------------------------------------
# Listing rows, by value
# ---------------------------------------------------------------------------
def test_listing_values():
    group("listing rows: pinned VALUES, not coverage")
    ok = True
    rows = parse_products(LISTING_EN, CAT_URL, 1)
    ok &= check("3 rows from the 3-lot English fixture", len(rows) == 3)
    first = rows[0]
    ok &= check("sku is the site's own lot id", first.sku == "106506005")
    ok &= check("price 1535.0 from the card", first.price == 1535.0)
    ok &= check("currency EUR", first.currency == "EUR")
    ok &= check("bid_kind current", first.bid_kind == "current")
    ok &= check("title from the payload",
                first.title.startswith("Cartier - Tank Must de Cartier PM"))
    ok &= check("price_source records BOTH sources",
                first.price_source == "next_data+dom")
    ok &= check("auction_id from the payload", first.auction_id == "1264941")
    ok &= check("url is the lot's own absolute url",
                first.url.startswith("https://www.catawiki.com/en/l/106506005-"))
    ok &= check("image url from the payload, not an <img> tag",
                (first.image_url or "").startswith("https://assets.catawiki"))

    # From the hydrated CARD, never from the payload: the payload's own
    # favoriteCount is 0 on 288 of 288 lots across 12 captures while the card
    # shows the real figure on all 24 of each. A field that is present,
    # authoritative-looking and uniformly wrong.
    ok &= check("favorite_count comes from the card (144, not the payload's 0)",
                first.favorite_count == 144)
    payload_favs = {l.get("favoriteCount")
                    for l in lots_payload(next_data(LISTING_EN))[1]["lots"]}
    ok &= check("...and the payload really does say 0", payload_favs == {0})

    ok &= check("page and position are threaded through",
                [(r.page, r.position) for r in rows] == [(1, 1), (2, 2), (3, 3)]
                or [(r.page, r.position) for r in rows] == [(1, 1), (1, 2), (1, 3)])
    ok &= check("page+position unique across a two-page merge",
                len({(r.page, r.position) for r in
                     parse_products(LISTING_EN, CAT_URL, 1)
                     + parse_products(LISTING_EN, CAT_URL, 2)}) == 6)
    ok &= check("source is this site on every row",
                {r.source for r in rows} == {"catawiki.com"})
    ok &= check("listing_kind says which page kind produced the row",
                {r.listing_kind for r in rows} == {"category"})

    # No brand on a listing, and that is deliberate: the title reads
    # "Cartier - Tank Must ..." and splitting on the dash would be a guess
    # presented as a fact. The lot page has a real Brand field.
    ok &= check("brand is null on a listing row rather than split off a title",
                all(r.brand is None for r in rows))

    # Locale coverage, by value in each language.
    per_locale = {
        "LISTING_DE": ("https://www.catawiki.com/de/c/333-armbanduhren", 150.0),
        "LISTING_NL": ("https://www.catawiki.com/nl/c/333-horloges", 150.0),
        "LISTING_PL": ("https://www.catawiki.com/pl/c/333-zegarki", 35.0),
        "LISTING_ZH": ("https://www.catawiki.com/zh-Hant/c/333-watches", 27.0),
        "LISTING_JA": ("https://www.catawiki.com/ja/c/333-watches", None),
    }
    for name, (url, want_first_price) in per_locale.items():
        got = parse_products(globals()[name], url, 1)
        ok &= check("%s: 3 rows, currency EUR everywhere" % name,
                    len(got) == 3
                    and {r.currency for r in got if r.price is not None} <= {"EUR"})
        ok &= check("%s: first row's price is %s" % (name, want_first_price),
                    got[0].price == want_first_price)

    search = parse_products(SEARCH_EN, SEARCH_URL, 1)
    ok &= check("a search page yields rows with listing_kind=search",
                {r.listing_kind for r in search} == {"search"})
    ok &= check("search values pinned (4200, 12500, 1950)",
                [r.price for r in search] == [4200.0, 12500.0, 1950.0])
    return ok


def test_reserve_invariant():
    group("the invariant behind every null price on this site")
    ok = True
    # 57 of 57 blank prices across 13 captures carried reservePriceSet: True,
    # spread through the page rather than clustered at its end - so a null
    # price here is the site's behaviour and not a hydration failure. This is
    # the check the canary runs instead of a coverage threshold, because a
    # threshold cannot tell a reserve-heavy auction from a broken run.
    for name, url in (("LISTING_EN", CAT_URL), ("LISTING_PL",
                      "https://www.catawiki.com/pl/c/333-zegarki"),
                      ("LISTING_ZH", "https://www.catawiki.com/zh-Hant/c/333-watches"),
                      ("AUCTION_EN", AUCTION_URL)):
        rows = parse_products(globals()[name], url, 1)
        blank = [r for r in rows if r.price is None]
        ok &= check("%s: every blank price is a reserve lot (%d of them)"
                    % (name, len(blank)),
                    all(r.reserve_price_set for r in blank))
        ok &= check("%s: a priced row still reports its reserve flag" % name,
                    all(r.reserve_price_set is not None for r in rows))
    return ok


def test_lot_page():
    group("a lot page: read from the payload, never from the DOM")
    ok = True
    row = parse_lot_page(LOT_CLOSED, LOT_URL)
    ok &= check("one row", row is not None)
    ok &= check("price 1300.0 - the last bid, from the payload",
                row.price == 1300.0)
    ok &= check("bid_kind final on a closed lot", row.bid_kind == "final")
    # Final is not the same as sold: this lot reached EUR 1,300 with its
    # reserve unmet and changed hands for nothing at all.
    ok &= check("sold False even though a final bid exists", row.sold is False)
    ok &= check("reserve_price_met False", row.reserve_price_met is False)
    ok &= check("reserve_price_set True", row.reserve_price_set is True)
    ok &= check("estimate 2100-2400, euro only",
                (row.estimate_min, row.estimate_max) == (2100.0, 2400.0))
    ok &= check("absolute close time, per LOT, from epoch millis",
                row.bidding_end_at == "2026-09-10T19:23:56+00:00")
    ok &= check("seller country and score",
                row.seller_country and row.seller_score is not None)
    ok &= check("price_source is the payload alone",
                row.price_source == "next_data")

    # The bid count is a FLOOR: the site returns the last ten bids and states
    # no total, and two lots with very different activity both reported
    # exactly 10.
    ok &= check("bid_count 10 with bid_count_is_floor True",
                row.bid_count == 10 and row.bid_count_is_floor is True)

    live = parse_lot_page(LOT_LIVE, "https://www.catawiki.com/en/l/106506005-x")
    ok &= check("the live-lot fixture parses too", live is not None)
    ok &= check("brand from the specification id, not its name",
                live.brand == "Cartier")

    # A lot page renders 20-40 OTHER lots in a "similar lots" carousel, using
    # the very same class a listing uses for its own price. Reading the DOM
    # here would return a neighbour's number.
    ok &= check("the lot fixture has no c-lot-card price nodes at all, so a "
                "DOM read could only have found a neighbour's",
                "c-lot-card__price" not in LOT_CLOSED)
    ok &= check("no starting_bid column: it was the same number as "
                "next_min_bid under a name that claimed otherwise",
                "starting_bid" not in [f.name for f in fields(Product)])
    return ok


def test_auction_page_is_a_listing():
    group("an auction page is a listing, and a better one")
    ok = True
    ok &= check("listing_kind auction", listing_kind(AUCTION_URL) == "auction")
    rows = parse_products(AUCTION_EN, AUCTION_URL, 1)
    ok &= check("rows come out of its bare `lots` list", len(rows) == 3)
    ok &= check("listing_kind on the row says auction",
                {r.listing_kind for r in rows} == {"auction"})
    # The columns a category listing cannot give at all.
    ok &= check("every row carries the auction's ABSOLUTE close time",
                all(r.auction_close_at == "2026-09-10T18:00:00Z" for r in rows))
    ok &= check("...and its status", {r.auction_status for r in rows} == {"closed"})
    ok &= check("a category row has neither",
                all(r.auction_close_at is None and r.auction_status is None
                    for r in parse_products(LISTING_EN, CAT_URL, 1)))
    ok &= check("total comes from the auction's own lot count",
                total_results(AUCTION_EN) == 130)
    # 130 lots on one page and no page links at all: dividing the total by
    # lotsPerPage would invent pages that do not exist.
    ok &= check("total_pages is 1, not 130/24", total_pages(AUCTION_EN, AUCTION_URL) == 1)
    ok &= check("an auction page does not paginate by URL",
                not paginates_by_url(AUCTION_URL))
    ok &= check("page_url refuses to build one", page_url(AUCTION_URL, 2) is None)
    return ok


def test_auctions_index():
    group("the auctions index: a work list, honestly thin")
    ok = True
    rows = auction_rows(AUCTIONS_INDEX, AUCTIONS_URL, 1)
    ok &= check("3 auctions from the 3-card fixture", len(rows) == 3)
    ok &= check("sku is the auction id", rows[0].sku == "1243988")
    ok &= check("title from the card's first h6",
                rows[0].title == "Figures & Figurines Auction")
    ok &= check("the relative end phrase is kept verbatim, not converted",
                rows[0].ends_text is not None)
    ok &= check("url is absolute and points at the auction",
                "/en/a/1243988-" in rows[0].url)
    ok &= check("rows are Auction, not Product",
                all(isinstance(r, Auction) for r in rows))
    ok &= check("ROW_CLASS_BY_MODE maps the mode to it",
                ROW_CLASS_BY_MODE["auctions"] is Auction)
    # The badge in the image corner is `+127` on one card and `18+` on the
    # next - a further-lots hint and an age warning in the same place - so no
    # lot-count column is published from it at all.
    ok &= check("no lot-count column read off an ambiguous badge",
                not any(f.name.startswith("lot_count") for f in fields(Auction)))
    ok &= check("no curator column: the index names a person",
                not any("curator" in f.name for f in fields(Auction)))
    # The links survive a redesign of the card.
    broken = AUCTIONS_INDEX.replace('data-testid="all-auctions-card"',
                                    'data-testid="renamed"')
    with redirect_stderr(io.StringIO()):
        fallback = auction_rows(broken, AUCTIONS_URL, 1)
    ok &= check("cards renamed -> rows recovered from the hrefs alone",
                len(fallback) == 3 and fallback[0].sku == "1243988")
    ok &= check("auction_links reads ids and slugs",
                dict(auction_links(AUCTIONS_INDEX)).get("1243988")
                == "figures-figurines-auction")
    return ok


# ---------------------------------------------------------------------------
# URLs
# ---------------------------------------------------------------------------
def test_urls():
    group("hosts, locales and lot ids")
    ok = True
    ok &= check("18 locales, from the site's own hreflang set", len(LOCALES) == 18)
    ok &= check("two of them carry capitals",
                "zh-Hans" in LOCALES and "zh-Hant" in LOCALES)
    # A lowercasing normaliser would break Chinese, which is served
    # case-sensitively.
    ok &= check("zh-Hant survives in its own casing",
                locale_of("https://www.catawiki.com/zh-Hant/c/333-watches") == "zh-Hant")
    ok &= check("a lowercase spelling still resolves to the site's casing",
                locale_of("https://www.catawiki.com/zh-hant/c/1-x") == "zh-Hant")
    ok &= check("an unknown prefix is not a locale",
                locale_of("https://www.catawiki.com/xx/c/1-x") is None)
    ok &= check("both hosts recognised",
                all(is_supported_host("https://%s/en/c/1-x" % h) for h in HOSTS))
    ok &= check("another catawiki TLD is refused WITH the reason",
                "is not a Catawiki host"
                in (unsupported_reason("https://www.catawiki.de/en/c/1-x") or ""))
    ok &= check("a missing locale prefix says so",
                "locale prefix"
                in (unsupported_reason("https://www.catawiki.com/xx/c/1-x") or ""))
    ok &= check("a good URL has no reason", unsupported_reason(CAT_URL) is None)

    ok &= check("page kinds", [listing_kind(u) for u in (
        CAT_URL, SEARCH_URL, LOT_URL, AUCTION_URL, AUCTIONS_URL,
        "https://www.catawiki.com/en/help")]
        == ["category", "search", "lot", "auction", "auctions", ""])

    ok &= check("the lot id comes out of the URL",
                sku_from_url("https://www.catawiki.com/en/l/106583855-omega-x")
                == "106583855")
    # The slug is decorative: the site translates it per locale and
    # canonicalises a foreign one itself, so the id is the key.
    ok &= check("a foreign slug does not change the id",
                sku_from_url("https://www.catawiki.com/nl/l/106583855-anything")
                == "106583855")
    ok &= check("a category keeps its stable id beside the slug",
                category_from_url(CAT_URL) == "333-watches")
    ok &= check("the same category in Dutch resolves to its own slug",
                category_from_url("https://www.catawiki.com/nl/c/333-horloges")
                == "333-horloges")
    ok &= check("tracking parameters and the #filters fragment come off",
                strip_tracking(CAT_URL + "?utm_source=x&page=2#filters")
                == CAT_URL + "?page=2")
    ok &= check("EUR on every locale, and it is a fact rather than a default",
                {host_currency("https://www.catawiki.com/%s/c/1-x" % loc)
                 for loc in LOCALES} == {CURRENCY} == {"EUR"})
    return ok


def test_pagination():
    group("pagination: arithmetic first, and a cap the site enforces")
    ok = True
    ok &= check("both listing kinds paginate by URL",
                paginates_by_url(CAT_URL) and paginates_by_url(SEARCH_URL))
    ok &= check("a lot page does not", not paginates_by_url(LOT_URL))
    ok &= check("page 1 has no page parameter", page_url(CAT_URL, 1) == CAT_URL)
    ok &= check("page 2 is built, not followed",
                page_url(CAT_URL, 2) == CAT_URL + "?page=2")
    ok &= check("a search keeps its query", page_url(SEARCH_URL, 2)
                == "https://www.catawiki.com/en/s?q=rolex&page=2")
    ok &= check("an existing page parameter is replaced, not duplicated",
                page_url(CAT_URL + "?page=7", 2) == CAT_URL + "?page=2")
    ok &= check("page numbers read back", page_number_from_url(CAT_URL + "?page=42") == 42)
    ok &= check("no page parameter means page 1", page_number_from_url(CAT_URL) == 1)

    # The cap is the whole point. `?page=99999` returned HTTP 200 with
    # `currentPage: 100` and page 100's own lots, so a planner that ignores
    # it re-fetches page 100 for every further page, adds no new sku, and a
    # data-based terminator reads "listing exhausted" - a COMPLETE run
    # holding 2,400 of 11,681 lots.
    ok &= check("the cap is the site's own 100", PAGE_CAP == 100)
    ok &= check("page 100 is buildable", page_url(CAT_URL, 100) is not None)
    ok &= check("page 101 is refused", page_url(CAT_URL, 101) is None)
    ok &= check("total_pages is capped, never the raw division",
                total_pages(LISTING_EN, CAT_URL) == 100)
    ok &= check("...and the pages beyond it are REPORTED, not swallowed",
                pages_beyond_cap(LISTING_EN) == 387)
    ok &= check("a listing inside the cap reports nothing beyond it",
                pages_beyond_cap(SEARCH_EN) == 0
                and total_pages(SEARCH_EN, SEARCH_URL) == 29)
    ok &= check("total comes from the payload", total_results(LISTING_EN) == 11681)
    ok &= check("a search page's own query is read from the payload, not a heading",
                search_header(SEARCH_EN) in (None, "rolex"))
    return ok


# ---------------------------------------------------------------------------
# What kind of answer was that?
# ---------------------------------------------------------------------------
def test_page_state():
    group("classification: ordered by what each signal proves")
    ok = True
    ok &= check("a listing with lots is content",
                detect_page_state(LISTING_EN, 200, CAT_URL) == "content")
    ok &= check("a lot page is content",
                detect_page_state(LOT_CLOSED, 200, LOT_URL) == "content")
    ok &= check("an auction page is content",
                detect_page_state(AUCTION_EN, 200, AUCTION_URL) == "content")
    ok &= check("the auctions index is content on its own links",
                detect_page_state(AUCTIONS_INDEX, 200, AUCTIONS_URL) == "content")

    # The trap: a search that matches nothing answers 200, prints "No
    # results", AND backfills the grid with 24 suggested lots reported as
    # `total: 24`. A parser that trusts the count returns two dozen
    # plausible, well-formed rows for a query that matched nothing.
    ok &= check("a no-results search is EMPTY, not content",
                detect_page_state(SEARCH_NO_RESULTS, 200, SEARCH_URL) == "empty")
    ok &= check("...and the signal is the payload's own flag, not a phrase",
                is_no_results(SEARCH_NO_RESULTS) and not is_no_results(SEARCH_EN))
    ok &= check("...and page_flow refuses to parse that state",
                not page_flow.should_parse("empty"))

    ok &= check("the real 403 refusal is blocked",
                detect_page_state(BLOCKED_403, 403, CAT_URL) == "blocked")
    ok &= check("its marker is a refusal marker, not a challenge",
                detect_block_marker(BLOCKED_403)
                and detect_bot_challenge(BLOCKED_403) is None)
    ok &= check("a 403 on an otherwise fine page is still blocked",
                detect_page_state("<html></html>", 403, CAT_URL) == "blocked")
    ok &= check("no response at all reports blocked rather than raising",
                page_flow.classify(None, None, CAT_URL) == "blocked")

    # Positive detection, inverted: the refusal carries no vendor name and
    # Chromium's own error page carries the site's hostname in its title.
    ok &= check("a page built from the site's assets is recognised",
                served_by_catawiki(LISTING_EN))
    ok &= check("the refusal is not", not served_by_catawiki(BLOCKED_403))
    chromium_error = ('<html><head><title>www.catawiki.com</title></head>'
                      '<body><div>ERR_PROXY_CONNECTION_FAILED</div></body></html>')
    ok &= check("Chromium's own error page, whose TITLE is the site's host, "
                "is not mistaken for a served page",
                not served_by_catawiki(chromium_error)
                and detect_page_state(chromium_error, None, CAT_URL) == "blocked")

    # `akamai` is deliberately not a marker: the string lives in the response
    # HEADER, not in the body of either page - 0 occurrences in all captures.
    ok &= check("no marker matches a page the site served",
                detect_bot_challenge(LISTING_EN) is None
                and detect_block_marker(LISTING_EN) is None)
    ok &= check("'akamai' is not in either marker set",
                not any("akamai" in m.lower()
                        for m in BLOCK_MARKERS + BOT_CHALLENGE_MARKERS))

    # The Scraping Browser's extension injects its own hunters into every
    # page it loads. The question is whether OUR markers mistake them for the
    # site's challenge - measured on a real CDP capture, they do not, which
    # is why there is no strip guard.
    with_extension = LISTING_EN.replace("</body>", EXTENSION_TAGS + "</body>")
    ok &= check("a good page carrying the extension's injected hunters is "
                "still content",
                detect_page_state(with_extension, 200, CAT_URL) == "content")
    ok &= check("...and reports no challenge",
                detect_bot_challenge(with_extension) is None)

    # Served, from the site's assets, with no payload yet: wants a WAIT, not
    # a refetch.
    shell = ('<html><body><img src="https://assets.catawiki.nl/a.jpg">'
             '<link href="https://assets.catawiki.nl/b.css">'
             '<article class="c-lot-card__container">'
             '<p class="c-lot-card__price"></p></article></body></html>')
    ok &= check("a served page with empty cards is a shell",
                detect_page_state(shell, 200, CAT_URL) == "shell")
    ok &= check("...which page_flow calls unpainted rather than blocked",
                page_flow.is_unpainted("shell", shell)
                and not page_flow.should_retry("shell"))
    return ok


def test_page_flow():
    group("policy: as data, so three engines cannot disagree")
    ok = True
    ok &= check("five states covered", set(page_flow.STATE_POLICY) ==
                {"content", "empty", "shell", "challenge", "blocked"})
    for state in page_flow.STATE_POLICY:
        ok &= check("%s: every decision is spelled out" % state,
                    set(page_flow.STATE_POLICY[state])
                    == {"parse", "retry", "solve", "blocked"})
    ok &= check("content parses and is not retried",
                page_flow.should_parse("content")
                and not page_flow.should_retry("content")
                and not page_flow.should_solve("content"))
    ok &= check("empty is NOT parsed on this site (the 24 suggestions)",
                not page_flow.should_parse("empty")
                and not page_flow.counts_as_blocked("empty"))
    ok &= check("blocked counts as blocked and never asks for a solve",
                page_flow.counts_as_blocked("blocked")
                and not page_flow.should_solve("blocked"))
    ok &= check("challenge asks for a solve and is not counted as blocked",
                page_flow.should_solve("challenge")
                and not page_flow.counts_as_blocked("challenge"))
    ok &= check("an unknown state is treated as blocked, not as content",
                not page_flow.should_parse("nonsense")
                and page_flow.counts_as_blocked("nonsense"))

    # Rotating an exit does not clear a block here: a headless browser was
    # refused from four residential exits and one datacentre address, while a
    # headful one was served from the same addresses. The budget is 0 on
    # purpose, and the engines read these constants rather than computing
    # their own.
    ok &= check("RETRY_ON_BLOCKED is False, against True in the siblings",
                page_flow.RETRY_ON_BLOCKED is False)
    ok &= check("and the block-retry budgets are 0",
                page_flow.BLOCK_RETRIES_WITHOUT_POOL == 0
                and page_flow.BLOCK_RETRIES_WITH_POOL == 0)
    advice = page_flow.block_advice(BLOCKED_403, headless=True, has_pool=False)
    ok &= check("the block advice names the real cause (headless), not the proxy",
                "HEADLESS" in advice and "--headful" in advice)

    # Readiness is a POPULATED price node, not a card count: the server sends
    # 24 card shells with 24 empty price nodes, so a count-based wait is
    # satisfied instantly while every price is still blank.
    ok &= check("the readiness anchor requires content in the node",
                ":not(:empty)" in page_flow.ready_selector("listing"))
    ok &= check("the threshold is above 1", page_flow.MIN_CARD_MATCHES > 1)
    ok &= check("a short last page lowers the threshold instead of timing out",
                page_flow.min_matches("listing", 5) == 5
                and page_flow.min_matches("listing", 24) == page_flow.MIN_CARD_MATCHES)
    ok &= check("expected_lots reads the payload", page_flow.expected_lots(LISTING_EN) == 3)

    # No scroll subsystem at all, and that is measured: three scrolls added
    # zero cards on every page kind.
    ok &= check("page_flow exposes no scroll API",
                not hasattr(page_flow, "scroll_until_settled")
                and not hasattr(page_flow, "SCROLL_ROUNDS_MAX"))

    # A poll, not an evaluated string: `wait_for_function` hands the browser
    # a string, which a CSP without `unsafe-eval` refuses outright. This
    # site's CSP does allow it today; the poll is what keeps a header change
    # from taking the run down.
    calls = 0
    def count(_sel):
        nonlocal calls
        calls += 1
        return 0 if calls < 3 else 9
    got = page_flow.wait_for_count(count, lambda ms: None, ".x", 8, 5000)
    ok &= check("wait_for_count polls until the threshold is reached", got == 9)
    ok &= check("...and returns the last count when the budget runs out",
                page_flow.wait_for_count(lambda s: 2, lambda ms: None,
                                         ".x", 8, 500) == 2)

    # Pagination candidates: the site's links are RELATIVE, several of its own
    # pages are advertised at once, and it links to other listings too.
    ok &= check("a relative link of its own agrees with the convention",
                page_flow.pagination_agrees(CAT_URL, 1,
                                            ["/en/c/333-watches?page=2#filters"]))
    ok &= check("a cursor link does not",
                not page_flow.pagination_agrees(CAT_URL, 1,
                                                ["/en/c/333-watches?cursor=abc"]))
    ok &= check("only the NEXT page number is accepted as a candidate",
                page_flow.next_page_candidates(
                    CAT_URL, ["/en/c/333-watches?page=2",
                              "/en/c/333-watches?page=100"])
                == [CAT_URL + "?page=2"])
    ok &= check("another listing's page 2 is dropped",
                page_flow.next_page_candidates(CAT_URL, ["/en/c/999-other?page=2"])
                == [CAT_URL + "?page=2"])
    ok &= check("past the cap there are no candidates, site link or not",
                page_flow.next_page_candidates(CAT_URL + "?page=100",
                                               ["/en/c/333-watches?page=101"]) == [])
    ok &= check("a single string is accepted as well as a list - the shape "
                "that took the first live run down",
                page_flow.next_page_candidates(CAT_URL, "/en/c/333-watches?page=2")
                == [CAT_URL + "?page=2"])
    ok &= check("the site publishes no link[rel=next], so the selector layer "
                "is genuinely last",
                'rel="next"' not in LISTING_EN)

    # Concurrency: refused WITH the reason where there is nothing to fetch.
    ok &= check("a paginated listing has no concurrency limit",
                page_flow.concurrency_limit(CAT_URL) is None
                and page_flow.concurrency_refusal(CAT_URL) is None)
    for url, word in ((LOT_URL, "lot"), (AUCTIONS_URL, "auctions index"),
                      (AUCTION_URL, "auction")):
        reason = page_flow.concurrency_refusal(url)
        ok &= check("concurrency on %s is refused with a reason" % word,
                    page_flow.concurrency_limit(url) == 1
                    and reason and "nothing to fetch" in reason)
    ok &= check("pages_at_cap flags a listing deeper than the site addresses",
                page_flow.pages_at_cap(LISTING_EN)
                and not page_flow.pages_at_cap(SEARCH_EN))
    return ok

# ---------------------------------------------------------------------------
# The output contract
# ---------------------------------------------------------------------------
def test_output_contract():
    group("the output contract shared across this scraper family")
    ok = True
    names = [f.name for f in fields(Product)]
    # The family prefix, byte-identical and in order, so a consumer written
    # against another repo in this family reads the first sixteen columns
    # unchanged. Site-specific columns go AFTER it.
    family_prefix = ["source", "scraped_at", "url", "sku", "title", "brand",
                     "price", "currency", "original_price", "discount_pct",
                     "rating", "review_count", "in_stock", "image_url",
                     "category", "price_source"]
    ok &= check("the family field prefix is present and in order",
                names[:len(family_prefix)] == family_prefix)
    ok &= check("this site's own columns come after it, in order",
                names[len(family_prefix):] ==
                ["page", "position", "bid_kind", "bid_status_text",
                 "bid_count", "bid_count_is_floor", "next_min_bid",
                 "time_left_text", "bidding_start_at", "bidding_end_at",
                 "reserve_price_set", "reserve_price_met", "sold", "buy_now",
                 "has_free_shipping", "favorite_count", "subtitle",
                 "estimate_min", "estimate_max", "auction_id",
                 "auction_title", "auction_close_at", "auction_status",
                 "seller_id", "seller_country", "seller_score",
                 "seller_feedback_count", "listing_kind"])
    # Three modes, and the third reads a different KIND of thing, so it gets
    # its own dataclass rather than a Product with most columns null (§9).
    ok &= check("three modes map to a row class, auctions to its own",
                ROW_CLASS_BY_MODE == {"listing": Product, "lot": Product,
                                      "auctions": Auction})
    ok &= check("the Auction row reuses `sku` for the id, like every other "
                "schema in the family",
                "sku" in [f.name for f in fields(Auction)])
    # A column that is null on every row of every run should not exist (§9).
    # These four are null on every LISTING row and populated in --mode
    # product, which is a different thing and is why they are kept — pinned
    # so that removing them needs a measurement rather than a hunch.
    # Null on every LISTING row and populated in --mode lot, which is a
    # different thing and is why they are kept -- pinned so that removing one
    # needs a measurement rather than a hunch.
    ok &= check("the lot-only columns are declared",
                {"bid_count", "bid_count_is_floor", "next_min_bid",
                 "estimate_min", "estimate_max", "seller_id",
                 "seller_country", "seller_score", "seller_feedback_count",
                 "bidding_end_at", "reserve_price_met", "sold"} <= set(names))
    # Null on every LOT row and populated on a listing: the card's own
    # relative timer.
    ok &= check("the listing-only columns are declared too",
                {"time_left_text", "bid_status_text"} <= set(names))
    ok &= check("both modes are one row per sku",
                set(UNIQUE_BY_SKU_MODES) == {"listing", "product"})

    ok &= check("the exit codes are the family's",
                (EXIT_BLOCKED, EXIT_NO_PRODUCTS, EXIT_PARTIAL) == (3, 4, 6))
    ok &= check("an exhausted listing counts as complete",
                "no_new_products" in COMPLETE_STOP_REASONS
                and "pagination_exhausted" in COMPLETE_STOP_REASONS)
    ok &= check("a single-page mode is complete by construction",
                "single_page_mode" in COMPLETE_STOP_REASONS)

    # No defaulted currency anywhere: a row that could not establish one says
    # None rather than claiming EUR, which would be wrong for the four
    # non-euro country sites.
    ok &= check("Product defaults currency to None, not a guess",
                Product().currency is None)
    ok &= check("Product defaults price_source to None",
                Product().price_source is None)
    return ok


def test_writers():
    group("writers, dedupe and the refusal to overwrite good data")
    ok = True
    rows = [Product(sku="1", url="u1", price=1.0),
            Product(sku="2", url="u2", price=2.0)]
    with tempfile.TemporaryDirectory() as d:
        prefix = os.path.join(d, "out")

        # A run that finds nothing writes NOTHING: a consumer cannot tell an
        # empty category from a failed run, and the failure destroys the last
        # known good data.
        save(rows, prefix, "json", allow_empty=False)
        ok &= check("a good run writes its output",
                    os.path.exists(prefix + ".json"))
        before = open(prefix + ".json").read()
        save([], prefix, "json", allow_empty=False)
        ok &= check("an empty run does NOT overwrite the previous good output",
                    open(prefix + ".json").read() == before)
        save([], prefix, "json", allow_empty=True)
        ok &= check("--allow-empty is the opt-out and does overwrite",
                    json.load(open(prefix + ".json")) == [])

        # An empty CSV still carries its header, so a consumer reads a table
        # with no rows instead of failing on a zero-byte file.
        csv_path = os.path.join(d, "empty.csv")
        write_csv([], csv_path, row_cls=Product)
        header = open(csv_path).read().strip().split("\n")[0]
        ok &= check("an empty CSV still carries its header",
                    header.split(",")[:4] == ["source", "scraped_at", "url", "sku"])

        # A list column has to survive CSV without becoming a Python repr.
        #
        # NO column here is a list — a listing row carries one image url
        # and no attribute list this repo reads — so this is checked with a
        # local row class rather than with Product. The joining is kept in
        # write_csv because it is generic and because a future column may
        # need it; pinning the CURRENT behaviour is what stops it being
        # deleted as dead or reappearing as a repr() by accident.
        ok &= check("no Product column is a list today",
                    not [f for f in fields(Product)
                         if "List" in str(f.type)])

        from dataclasses import dataclass as _dataclass
        from typing import List as _List, Optional as _Optional

        @_dataclass
        class _WithList:
            sku: _Optional[str] = None
            things: _Optional[_List[str]] = None

        csv_path = os.path.join(d, "list.csv")
        write_csv([_WithList(sku="1", things=["a", "b"])], csv_path,
                  row_cls=_WithList)
        body = open(csv_path).read()
        ok &= check("a list column is joined, not repr()d in CSV",
                    ("a" + LIST_CSV_SEPARATOR + "b") in body and "['a'" not in body)

    seen = set()
    ok &= check("dedupe drops a repeated sku",
                len(dedupe_by_sku([Product(sku="a"), Product(sku="a")], seen)) == 1)
    # A row with no key is always KEPT: there is nothing to check a duplicate
    # against, and dropping it is a silent data loss rather than a dedupe.
    ok &= check("a row with no sku is kept, not dropped",
                len(dedupe_by_key([Product(sku=None), Product(sku=None)],
                                  set())) == 2)

    meta = run_meta("complete", "completed", 3, 3, "u", "u", 36,
                    pages_failed=[], mode="listing", source="catawiki.com")
    ok &= check("the sidecar records status, mode and source",
                meta["status"] == "complete" and meta["mode"] == "listing"
                and meta["source"] == "catawiki.com")
    # A count stops being a description once a page can fail while later ones
    # succeed, so the sidecar names WHICH pages failed.
    meta = run_meta("partial", "blocked", 5, 3, "u", "u", 12,
                    pages_failed=[2, 4], mode="listing", source="catawiki.com")
    ok &= check("the sidecar names which pages failed, by number",
                meta["pages_failed"] == [2, 4])
    return ok


def test_finish_run():
    group("finish_run: the exit codes all three engines must agree on")
    ok = True
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "run")
        rows = [Product(sku="1", url="u")]

        code = finish_run(rows, p, "json", False, blocked=False,
                          stop_reason="completed", pages_requested=1,
                          pages_completed=1, pages_failed=[], mode="listing",
                          source="catawiki.com", start_url="u", final_url="u")
        ok &= check("a complete run exits 0", code == 0)

        code = finish_run([], p + "b", "json", False, blocked=True,
                          stop_reason="blocked_no-response",
                          pages_requested=1, pages_completed=0,
                          pages_failed=[1], mode="listing",
                          source="catawiki.com", start_url="u", final_url="u")
        ok &= check("a blocked run exits 3, not 4", code == EXIT_BLOCKED)
        # A FAILED run writes no sidecar: `save` leaves the previous good
        # output in place, and a "failed" sidecar beside good data would
        # contradict it.
        ok &= check("a failed run writes no sidecar beside older good data",
                    not os.path.exists(p + "b.meta.json"))

        code = finish_run([], p + "c", "json", False, blocked=False,
                          stop_reason="completed", pages_requested=1,
                          pages_completed=1, pages_failed=[], mode="listing",
                          source="catawiki.com", start_url="u", final_url="u")
        ok &= check("a genuinely empty result exits 4, not 3",
                    code == EXIT_NO_PRODUCTS)

        code = finish_run(rows, p + "d", "json", False, blocked=False,
                          stop_reason="page_load_timeout", pages_requested=5,
                          pages_completed=2, pages_failed=[3], mode="listing",
                          source="catawiki.com", start_url="u", final_url="u")
        ok &= check("a run with data that stopped early exits 6 (partial)",
                    code == EXIT_PARTIAL)
        ok &= check("a partial run still writes what it got",
                    os.path.exists(p + "d.json"))
    return ok


def test_diff():
    group("diff_runs")
    ok = True
    # `price_source` on this site is "dom" for a listing row and
    # "meta+apollo" for a detail row — there is no structured price on a
    # listing page to confirm against — so the source-change case uses those
    # two values rather than the sibling repos' jsonld pair.
    old = [{"sku": "1", "price": 10.0, "price_source": "dom"},
           {"sku": "2", "price": 20.0, "price_source": "dom"},
           {"sku": "3", "price": 30.0, "price_source": "dom"}]
    new = [{"sku": "1", "price": 11.0, "price_source": "dom"},
           {"sku": "3", "price": 30.5, "price_source": "meta+apollo"},
           {"sku": "4", "price": 40.0, "price_source": "dom"}]
    d = diff_products(old, new)
    ok &= check("a real price move is reported as changed",
                any(c["sku"] == "1" for c in d["changed"]))
    ok &= check("a delisted product is reported as removed",
                [r["sku"] for r in d["removed"]] == ["2"])
    ok &= check("a new product is reported as added",
                [r["sku"] for r in d["added"]] == ["4"])
    # A price difference that comes with a price_source difference says
    # something about OUR two snapshots, not about the shop.
    ok &= check("a price move with a source change is not 'changed'",
                not any(c["sku"] == "3" for c in d["changed"]))
    ok &= check("...it is reported separately as source_changed",
                any(c["sku"] == "3" for c in d.get("source_changed", [])))

    # The auction columns are tracked, and `bid_kind` beside `price` is the
    # point: without it a price difference is unreadable, because the same
    # number means a live bid in one run and a closed lot's last bid in the
    # next.
    tracked = __import__("diff_runs").TRACKED_FIELDS
    ok &= check("price and bid_kind are tracked together",
                "price" in tracked and "bid_kind" in tracked)
    ok &= check("the auction outcome columns are tracked",
                {"sold", "reserve_price_met", "auction_status"} <= set(tracked))
    # A closed auction is a LIFECYCLE transition, not a repricing: bid_kind
    # moves current -> final and the amount moves with it, and reporting that
    # as a price change would make every overnight diff of a live auction
    # look like a repricing.
    import diff_runs as _dr
    closed = _dr.diff_products(
        [{"sku": "1", "price": 100.0, "bid_kind": "current",
          "price_source": "next_data+dom"}],
        [{"sku": "1", "price": 140.0, "bid_kind": "final",
          "price_source": "next_data+dom"}])
    ok &= check("current -> final lands in `lifecycle`, not in `changed`",
                len(closed["lifecycle"]) == 1 and not closed["changed"])
    # The other direction is an anomaly: a closed lot does not reopen.
    reopened = _dr.diff_products(
        [{"sku": "1", "price": 140.0, "bid_kind": "final",
          "price_source": "next_data+dom"}],
        [{"sku": "1", "price": 100.0, "bid_kind": "current",
          "price_source": "next_data+dom"}])
    ok &= check("final -> current is reported as a real change",
                len(reopened["changed"]) == 1 and not reopened["lifecycle"])
    # And a plain price move with no lifecycle change is still a change.
    outbid = _dr.diff_products(
        [{"sku": "1", "price": 100.0, "bid_kind": "current",
          "price_source": "next_data+dom"}],
        [{"sku": "1", "price": 120.0, "bid_kind": "current",
          "price_source": "next_data+dom"}])
    ok &= check("a new bid on an open lot is still a change",
                len(outbid["changed"]) == 1)
    # The sibling repo's from-price columns are NOT tracked, because they do
    # not exist here: a card prints one amount, not a range.
    ok &= check("no from-price columns are tracked (this site has none)",
                not {"price_is_from", "price_max"} & set(tracked))
    ok &= check("...and Product does not declare them either",
                not {"price_is_from", "price_max"}
                & {f.name for f in fields(Product)})
    return ok


def test_pyppeteer_teardown_noise():
    group("pyppeteer teardown noise is suppressed, and its limit is pinned")
    ok = True
    try:
        import puppeteer_scraper as pyp
    except ImportError:
        return check("pyppeteer engine present (skipped: library absent)", True)

    handler = pyp._AsyncBridge._on_loop_exception.__func__ if hasattr(
        pyp._AsyncBridge._on_loop_exception, "__func__") else pyp._AsyncBridge._on_loop_exception

    class _Loop:
        def __init__(self): self.passed_through = []
        def default_exception_handler(self, context):
            self.passed_through.append(context)

    # Each of these arrives on a run that SUCCEEDED, after the output is
    # written, and four tracebacks under a healthy run is how a reader learns
    # to ignore the log.
    swallowed = [
        {"message": "Task was destroyed but it is pending"},
        {"message": "Future exception was never retrieved",
         "exception": RuntimeError("Protocol error (Target.sendMessageToTarget): "
                                   "No session with given id")},
        {"exception": RuntimeError("Target closed")},
        {"exception": RuntimeError("Connection closed")},
        {"message": "Event loop is closed"},
    ]
    for context in swallowed:
        loop = _Loop()
        handler(loop, context)
        label = (context.get("message") or str(context.get("exception")))[:44]
        ok &= check("teardown noise suppressed: %s" % label,
                    not loop.passed_through)

    # A REAL error must still get through, or the suppression has become a
    # blindfold.
    loop = _Loop()
    handler(loop, {"exception": ValueError("something actually went wrong")})
    ok &= check("a real exception is NOT swallowed", len(loop.passed_through) == 1)

    # The handler reads BOTH fields. It used to read `exception or message`,
    # which meant a context carrying both never had its message inspected —
    # so the asyncio-worded ones kept printing after they were "handled".
    src = inspect.getsource(handler)
    ok &= check("the handler inspects the message as well as the exception",
                'for k in ("exception", "message")' in src)

    # PINNED LIMITATION, not a guard: `Exception ignored in: <coroutine
    # object Connection._recv_loop>` is printed by CPython's garbage
    # collector at interpreter shutdown, after the loop is gone and after the
    # exit code is decided. No loop handler can reach it, and catching it
    # would mean a global unraisable hook that swallows real bugs too. It is
    # documented in TROUBLESHOOTING.md instead; this check makes sure that
    # documentation stays there.
    doc = open(os.path.join(REPO_ROOT, "TROUBLESHOOTING.md"),
               encoding="utf-8").read()
    ok &= check("the shutdown-time traceback is documented rather than hidden",
                "Exception ignored in" in doc and "The run succeeded" in doc)
    return ok


def test_canary_separates_access_from_defect():
    group("the canary fails on defects and only WARNS on access conditions")
    ok = True
    wf_path = os.path.join(REPO_ROOT, ".github", "workflows", "canary.yml")
    wf = open(wf_path, encoding="utf-8").read()

    # The data checks must not run on a blocked or refused run: there is no
    # output file, and a missing file would fail for the wrong reason.
    ok &= check("the data checks are gated on the run having got in",
                "steps.verdict.outputs.tested == 'true'" in wf)

    # Extract the real interpret-the-exit-code script and run it under bash
    # for every code, rather than asserting on the YAML text. What matters is
    # whether the JOB FAILS, and only running it answers that.
    try:
        start = wf.index('          set -e\n          code=')
        end = wf.index('          echo "tested=$tested" >> "$GITHUB_OUTPUT"')
        end += len('          echo "tested=$tested" >> "$GITHUB_OUTPUT"')
    except ValueError:
        return check("the canary's exit-code script could be located", False)
    script = "\n".join(line[10:] if line.startswith(" " * 10) else line
                        for line in wf[start:end].splitlines())

    # WHY EACH CODE LANDS WHERE IT DOES:
    #   0  got in and parsed         -> pass, and the assertions then run
    #   3  blocked before parsing    -> ACCESS. Measured intermittent per
    #      profile on this site, so a daily red badge would be noise.
    #   5  endpoint refused          -> ACCESS, and the commonest cause is an
    #      EXPIRED SECRET. A credential is not forever; failing on it paints
    #      the badge red every day until someone notices.
    #   6  partial                   -> ACCESS, usually a mid-run block.
    #   1  crashed                   -> DEFECT.
    #   2  bad arguments             -> DEFECT (in the workflow itself).
    #   4  served a page, ZERO rows  -> DEFECT, and precisely the regression
    #      this canary exists to catch: the tile anchor moved.
    expected = {0: "pass", 3: "warn", 5: "warn", 6: "warn",
                1: "fail", 2: "fail", 4: "fail", 99: "fail"}
    for code, want in sorted(expected.items()):
        body = script.replace('code="${{ steps.run.outputs.exit_code }}"',
                              'code="%d"' % code)
        with tempfile.TemporaryDirectory() as td:
            out_file = os.path.join(td, "gh_output")
            summary = os.path.join(td, "gh_summary")
            open(out_file, "w").close()
            open(summary, "w").close()
            done = subprocess.run(
                ["bash", "-c", body], capture_output=True, text=True,
                env=dict(os.environ, GITHUB_OUTPUT=out_file,
                         GITHUB_STEP_SUMMARY=summary))
            failed = done.returncode != 0
            warned = "::warning::" in done.stdout
            errored = "::error::" in done.stdout
            wrote_summary = bool(open(summary, encoding="utf-8").read().strip())
            tested = "tested=true" in open(out_file, encoding="utf-8").read()

        if want == "pass":
            got = not failed and not warned and not errored and tested
        elif want == "warn":
            # A warning must NOT read as a pass: it also has to say in the
            # step summary that nothing was actually tested, and it must not
            # claim `tested`.
            got = not failed and warned and wrote_summary and not tested
        else:
            got = failed and errored
        ok &= check("exit %-2d is treated as %s" % (code, want), got)

    ok &= check("the reason access is not a defect is written down",
                "ACCESS CONDITIONS ARE NOT DEFECTS" in wf)

    # NO SCHEDULE, and the reason has to travel with the decision. A daily
    # cron against a credential that does not survive a day gives either a
    # permanently red badge or a permanently green one that tested nothing —
    # and the green is worse, because it reads as "the parser still works".
    # Restoring the cron is a legitimate change the day a long-lived
    # credential exists; this check makes it a decision rather than a habit.
    ok &= check("the canary has no cron schedule",
                not re.search(r"^\s*-\s*cron:", wf, re.M))
    ok &= check("it is dispatchable by hand", "workflow_dispatch:" in wf)
    ok &= check("and the reason the schedule is off is written down",
                "does not survive a day" in wf)
    ok &= check("the expired-secret case is named",
                "expired" in wf.lower() and "refresh" in wf.lower())
    return ok


def test_ci_checks_is_actually_wired_up():
    group("the repo's own checks are RUN, and still catch a real secret")
    ok = True
    script = os.path.join(REPO_ROOT, ".github", "ci_checks.py")
    ok &= check("ci_checks.py exists", os.path.exists(script))
    if not os.path.exists(script):
        return ok

    # IT HAS TO BE INVOKED BY A WORKFLOW. It was not — for the whole of
    # v0.1.0 it sat there implementing three checks that nothing ran, while a
    # second, LOOSER copy of one of them lived inline in tests.yml. Dead code
    # that looks load-bearing is worse than no code, and this is the check
    # that keeps it alive.
    wf_dir = os.path.join(REPO_ROOT, ".github", "workflows")
    workflows = "\n".join(
        open(os.path.join(wf_dir, f), encoding="utf-8").read()
        for f in sorted(os.listdir(wf_dir)) if f.endswith((".yml", ".yaml")))
    ok &= check("a workflow runs ci_checks.py", "ci_checks.py" in workflows)
    ok &= check("the secret check specifically is run",
                "--secret-check" in workflows or "--all" in workflows)

    # AND IT PASSES ON THIS REPO. A check that is always red teaches everyone
    # to ignore checks; this one WAS red, on six documented placeholders.
    done = subprocess.run([sys.executable, script, "--all"],
                          cwd=REPO_ROOT, capture_output=True, text=True)
    ok &= check("ci_checks.py --all passes on this repo (exit %d)" % done.returncode,
                done.returncode == 0)
    if done.returncode != 0:
        print("        " + (done.stdout or done.stderr).strip()[-400:])

    # AND IT STILL CATCHES A REAL ONE. Loosening an allowlist until the check
    # passes is the failure mode here, so both directions are asserted: a
    # planted CDP endpoint, a planted 32-hex key and a planted http proxy URL
    # must all be found. The http one matters most — the inline grep this
    # replaced covered only ws:// and would have missed a committed proxy.
    planted = os.path.join(REPO_ROOT, "_secret_probe_delete_me.py")
    # The key is ASSEMBLED rather than written as a literal, because a
    # 32-character hex string sitting in this file is exactly what the check
    # under test flags — and it did, on the first run of this test. The file
    # it writes still gets the whole thing, which is what the probe needs.
    planted_key = "3f8a1c9e4b7d2065" + "af13ce88b409d752"
    try:
        with open(planted, "w", encoding="utf-8") as f:
            f.write(
                'CDP = "ws://acct-zone-scraping_browser-pid-x:'
                'S3cretPassw0rd@cb.2captcha.com:9222"\n'
                'KEY = "%s"\n'
                'PROXY = "http://acct-zone-custom:S3cretPassw0rd'
                '@na.proxy.2captcha.com:2334"\n' % planted_key)
        caught = subprocess.run([sys.executable, script, "--secret-check"],
                                cwd=REPO_ROOT, capture_output=True, text=True)
        out = caught.stdout + caught.stderr
        ok &= check("a planted secret fails the check", caught.returncode != 0)
        ok &= check("the planted ws:// CDP endpoint is named",
                    "_secret_probe_delete_me.py:1" in out)
        ok &= check("the planted 32-hex key is named",
                    "_secret_probe_delete_me.py:2" in out)
        ok &= check("the planted http:// PROXY url is named (the grep this "
                    "replaced missed those)",
                    "_secret_probe_delete_me.py:3" in out)
    finally:
        # Never leave it behind: a test that mutates the working tree is its
        # own defect, and this one would plant a fake secret.
        if os.path.exists(planted):
            os.remove(planted)
    ok &= check("the probe file is cleaned up", not os.path.exists(planted))

    # The pre-publication scan: the same rules over every blob that has EVER
    # existed. A later commit cannot remove what a published tag and a merged
    # PR's refs already hold, so this has to be runnable BEFORE the repo goes
    # public — and it has to be findable, which a check makes it.
    hist = subprocess.run([sys.executable, script, "--history-check"],
                          cwd=REPO_ROOT, capture_output=True, text=True)
    ok &= check("--history-check runs and this history is clean",
                hist.returncode == 0)
    ok &= check("it says how many objects it looked at",
                "ever existed" in hist.stdout)
    # NOT in --all, on purpose: it shells out to git once per object, and a
    # dirty history needs a decision rather than a red check on every push.
    every = subprocess.run([sys.executable, script, "--all"],
                           cwd=REPO_ROOT, capture_output=True, text=True)
    ok &= check("--all deliberately excludes the history scan",
                "history check" not in every.stdout)
    return ok


def test_no_capture_leaks():
    group("no credentials or personal data in the committed fixtures")
    ok = True
    # Collected by SUFFIX, which is how this file names its fixtures. An
    # earlier version asked for a "FIX_" PREFIX, matched nothing, and every
    # check below passed against an empty string — 150 KB of committed real
    # captures went unexamined while twelve checks reported green. The
    # non-empty assertion underneath is the actual fix: a corpus check that
    # can silently scan nothing is worse than no corpus check at all.
    names = [k for k, v in sorted(globals().items())
             if k.endswith("_FIXTURE") and isinstance(v, str)]
    fixtures = "\n".join(globals()[k] for k in names)
    ok &= check("the privacy checks below have fixtures to scan "
                "(%d fixtures, %d chars)" % (len(names), len(fixtures)),
                len(names) >= 3 and len(fixtures) > 30000)
    # Guarded with PATTERNS rather than with the literals a previous capture
    # happened to contain, so the NEXT capture is checked too. On a sibling
    # site (mediamarkt-scraper) the pages embed a front-end configuration blob — a Sentry DSN, a Woosmap
    # public key, a store-code JWT — none of which is needed to test a
    # parser, and none of which belongs in a public repository.
    patterns = {
        "a JWT": r"eyJ[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{10,}",
        "an access token": r"(?:access|auth|bearer)[_\-]?[Tt]oken\"?\s*[:=]\s*\"?[A-Za-z0-9._\-]{12,}",
        "an API key": r"(?:api|public|secret|private)[_\-]?[Kk]ey\"?\s*[:=]\s*\"?[A-Za-z0-9._\-]{12,}",
        "a Sentry DSN": r"https://[0-9a-f]{16,}@[\w.]*ingest",
        "a session id": r"session[_\-]?[Ii]d\"?\s*[:=]\s*\"?[A-Za-z0-9._\-]{8,}",
        "an email address": r"[\w.+-]+@[\w-]+\.[a-z]{2,}",
        "a proxy credential": r"://[^\s/@\"]+:[^\s/@\"]+@",
    }
    for label, pattern in patterns.items():
        hits = re.findall(pattern, fixtures)
        ok &= check("no %s in the fixtures" % label, not hits)

    # The SITE's OWN per-impression material, which a fresh capture brings with
    # it: a click-tracking key, its checksum, and the logging key that ties an
    # impression to a session. Anonymous and expired, and still not something
    # to commit — and a 40-character hex-ish blob in a public repo reads as a
    # credential to every scanner that looks, including this repo's own CI
    # grep. Matched as PATTERNS rather than as the values one capture
    # happened to hold, so the NEXT capture is checked too.
    site_session = {
        "a click-tracking key": r"click_key=(?!PLACEHOLDER)[A-Za-z0-9%.-]{12,}",
        "a click checksum": r"click_sum=(?!PLACEHOLDER)[A-Za-z0-9]{6,}",
        "an impression logging key":
            r'data-logging-key="(?!PLACEHOLDER)[A-Za-z0-9:-]{12,}"',
        "a content-source token":
            r"content_source=(?!PLACEHOLDER)[A-Za-z0-9%.-]{12,}",
        "a DataDome session blob": r"'(?:cid|hsh|e|cookie)':'(?!PLACEHOLDER)[^']{16,}'",
    }
    for label, pattern in site_session.items():
        hits = re.findall(pattern, fixtures)
        ok &= check("no %s in the fixtures (scrub a new capture before "
                    "committing it)" % label, not hits)

    # The repo-wide grep CI runs, applied here too so a failure is local.
    # Asked of GIT, not of the filesystem. A developer's own `.env` beside
    # the scripts is EXPECTED — it is how the local runs get their key — and
    # `.gitignore` is what keeps it out of the repo. Checking for the file's
    # existence made this red on every machine that had ever run the scraper
    # for real, which is the machine most likely to be running the suite.
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", ".env"],
        cwd=REPO_ROOT, capture_output=True, text=True).returncode == 0
    ok &= check("no .env file is tracked by git", not tracked)
    return ok


def test_wording():
    group("wording and removed flags")
    ok = True
    # Asked of GIT, so the scan reaches the workflows and the issue
    # templates under .github/ — eight shipped files that an os.listdir of
    # the repo ROOT silently missed, including the four a contributor is
    # most likely to paste marketing wording into. Untracked scratch files
    # and .pytest_cache/ are excluded for free by asking git.
    listed = subprocess.run(["git", "ls-files"], cwd=REPO_ROOT,
                            capture_output=True, text=True)
    if listed.returncode == 0 and listed.stdout.strip():
        shipped = [f for f in listed.stdout.split("\n")
                   if f.endswith((".py", ".md", ".txt", ".toml", ".yml", ".yaml"))
                   and os.path.basename(f) != os.path.basename(__file__)]
    else:  # not a git checkout (a release tarball): fall back to the root
        shipped = [f for f in os.listdir(REPO_ROOT)
                   if f.endswith((".py", ".md", ".txt", ".toml", ".yml", ".yaml"))
                   and f != os.path.basename(__file__)]
    ok &= check("the wording scan reaches beyond the repo root",
                any(os.sep in f or "/" in f for f in shipped))
    for phrase in BANNED_PHRASES:
        offenders = []
        for f in shipped:
            try:
                text = open(os.path.join(REPO_ROOT, f), encoding="utf-8").read()
            except (OSError, UnicodeDecodeError):
                continue
            if phrase.lower() in text.lower():
                offenders.append(f)
        ok &= check("no shipped file says %r" % phrase, not offenders)

    for flag in REMOVED_ENGINE_FLAGS:
        offenders = []
        for f in ENGINE_FILES:
            path = os.path.join(REPO_ROOT, f)
            if not os.path.exists(path):
                continue
            text = open(path, encoding="utf-8").read()
            # A prose mention explaining why the flag does NOT exist is fine
            # and is worth keeping; an argparse registration is not.
            if ('add_argument("%s"' % flag) in text or \
                    ("add_argument('%s'" % flag) in text:
                offenders.append(f)
        ok &= check("no engine registers the removed flag %s" % flag,
                    not offenders)

    # The product this repo integrates with, named correctly.
    readme = os.path.join(REPO_ROOT, "README.md")
    if os.path.exists(readme):
        text = open(readme, encoding="utf-8").read()
        ok &= check("the README names the Scraping Browser API",
                    "Scraping Browser API" in text)
        ok &= check("the README does not name a competitor",
                    not re.search(r"brightdata|oxylabs|smartproxy|zyte|scraperapi\.com",
                                  text, re.IGNORECASE))
    return ok


def test_fingerprint_client_reads_env():
    group("fingerprint_client resolves its key the way the docs promise")
    ok = True
    import fingerprint_client as fpc

    # THE DEFECT THIS PINS, found the first time --fingerprint was run live
    # here and confirmed present in five sibling repos: `--key` defaulted to
    # `os.environ.get("TWOCAPTCHA_KEY")` alone. So a key put in `.env` —
    # which is exactly what §3, the README and .env.example instruct — worked
    # for every engine and failed HERE with "No API key". A documented
    # mechanism not applied on one path, which is the shape of half the
    # defects §16 lists.
    src = inspect.getsource(fpc.main)
    ok &= check("it loads .env itself, rather than hoping an engine did",
                "env_config.load_env()" in src)
    ok &= check("...and reads the key through the family's loader",
                'env_config.env_value("TWOCAPTCHA_KEY")' in src)
    # Through `env_value` and NOT `os.environ.get`, because only the former
    # applies the placeholder rule. Measured both ways with
    # TWOCAPTCHA_KEY=your_2captcha_api_key_here exported: os.environ.get
    # sends the placeholder to the API and the run reports "Fingerprint API
    # rejected the key (401) — note this is a separate subscription", which
    # sends the reader to check a subscription they never needed.
    ok &= check("...not straight from os.environ, which skips the "
                "placeholder rule",
                'os.environ.get("TWOCAPTCHA_KEY")' not in src)
    # Behaviourally, not just by reading the source — and written
    # self-contained so this check is byte-identical in every repo of the
    # family rather than depending on a local helper.
    saved = os.environ.get("TWOCAPTCHA_KEY")
    try:
        os.environ["TWOCAPTCHA_KEY"] = "your_2captcha_api_key_here"
        read_back = env_config.env_value("TWOCAPTCHA_KEY")
    finally:
        if saved is None:
            os.environ.pop("TWOCAPTCHA_KEY", None)
        else:
            os.environ["TWOCAPTCHA_KEY"] = saved
    ok &= check("a placeholder still reads as unset on this path",
                read_back is None)

    # The default must never reach `--help`. argparse prints a default only
    # when the help string asks for it, so this is one substring away from
    # printing a live credential to anyone who types --help.
    ok &= check("the --key help text does not interpolate its default",
                "%(default)s" not in src)
    return ok


def test_fingerprint_application():
    group("a fingerprint is applied as the fingerprint describes it")
    ok = True
    import fingerprint_client as fpc

    ua = fpc.fingerprint_user_agent(FIX_FINGERPRINT)
    # The UA used to be read from `userAgent.value`, a key the API returns in
    # NEITHER format. So --fingerprint silently set no user agent at all and
    # the browser kept its own: a German fingerprint's screen and locale
    # wearing a local Chromium's UA, which is precisely the identity mismatch
    # the flag exists to avoid.
    ok &= check("the user agent is found in the shape the API returns",
                ua and ua.startswith("Mozilla/5.0 (Windows NT 10.0"))
    ok &= check("the `raw` format's ua key is understood too",
                fpc.fingerprint_user_agent({"data": {"ua": "UA/1.0"}}) == "UA/1.0")
    ok &= check("a fingerprint with no user agent yields None, not a crash",
                fpc.fingerprint_user_agent({"country": "DE"}) is None)

    kw = fpc.playwright_context_kwargs(FIX_FINGERPRINT)
    ok &= check("the context carries the fingerprint's user agent",
                kw.get("user_agent") == ua)
    # `locale` used to be built as f"en-{country}", giving "en-NL" for a
    # Dutch fingerprint. An English-speaking visitor in the Netherlands is
    # possible, but it is not what this fingerprint describes, and a locale
    # that contradicts the rest of the identity is the mismatch again. This
    # was one of the six defects a sibling repo inherited from copied core
    # and never ran (§16).
    ok &= check("the locale is the fingerprint's own, not en-<country>",
                kw.get("locale") == "nl-NL")
    ok &= check("the timezone is carried, so the browser cannot contradict it",
                kw.get("timezone_id") == "Europe/Amsterdam")
    # The device pixel ratio, which Playwright takes as its own option and
    # which was dropped on the floor until a live browser was compared
    # against the fingerprint: a fingerprint stating 1.25 produced a browser
    # reporting 1, so the identity contradicted itself on an axis a
    # fingerprinter reads for free.
    ok &= check("the device scale factor is carried",
                kw.get("device_scale_factor") == 1)
    # A viewport exactly equal to the screen is itself a signal, and the
    # fingerprint states its own window size rather than needing one guessed.
    ok &= check("the viewport is the fingerprint's window, not its screen",
                kw.get("viewport") == {"width": 1920, "height": 992}
                and kw.get("screen") == {"width": 1920, "height": 1080})

    # Falling back sensibly when a field is absent, rather than dropping it.
    bare = fpc.playwright_context_kwargs({"country": "FR", "screen":
                                          {"width": 1280, "height": 800}})
    ok &= check("a fingerprint with no intl block still gets a locale",
                bare.get("locale") == "en-FR")
    ok &= check("...and a window smaller than the screen",
                bare["viewport"]["height"] < bare["screen"]["height"])
    ok &= check("a fingerprint with nothing usable yields no kwargs",
                fpc.playwright_context_kwargs({}) == {})

    # Every key this produces must be one Playwright's new_context accepts;
    # an unknown one is a TypeError at launch, on the paid path, at runtime.
    accepted = {"user_agent", "viewport", "screen", "locale", "timezone_id",
                "geolocation", "permissions", "extra_http_headers",
                "device_scale_factor", "is_mobile", "has_touch", "color_scheme"}
    ok &= check("every context kwarg is one Playwright accepts",
                set(kw) <= accepted)
    return ok


def test_credentials_never_reach_a_log():
    group("an API key never reaches a log or an exception message")
    ok = True
    import fingerprint_client as fpc
    import captcha_solver as cs

    # requests puts the FULL URL — query string included — into the text of
    # HTTPError and of every connection error. Both of these modules have an
    # endpoint that takes the key as a query parameter, so an error there
    # echoed a live key to the terminal. It did, once, on a real call.
    # An obviously fake key, and NOT a real one even a revoked one: a
    # 32-hex string in a public repo reads as a live credential to every
    # scanner that looks, including this repo's own CI grep. The word
    # "example" in the name is what tells that grep this line is a fixture.
    example_key = "0123456789abcdef0123456789abcdef"
    for name, module in (("fingerprint_client", fpc), ("captcha_solver", cs)):
        redacted = module._redact(
            "400 Client Error: Bad Request for url: "
            "https://api.2captcha.com/fingerprint/random?format=chromium&"
            "key=%s" % example_key)
        ok &= check("%s redacts a key out of an error message" % name,
                    example_key not in redacted)
        ok &= check("...and keeps the endpoint, which is the useful half",
                    "api.2captcha.com/fingerprint/random" in redacted)
        ok &= check("%s redacts clientKey too" % name,
                    example_key not in module._redact("clientKey=%s" % example_key))
        ok &= check("%s leaves ordinary text alone" % name,
                    module._redact("upstream status 403") == "upstream status 403")
    return ok


def test_concurrent_dispatch(skips):
    group("concurrent page dispatch (threads, stop event, accounting)")
    ok = True
    try:
        import playwright_scraper as eng
    except ImportError as e:
        skips.append("concurrent dispatch (%s)" % e)
        return ok

    # The thread fan-out is the one part of --concurrency that the rest of
    # this suite does not reach, and it is not reachable from a live run in
    # every environment either: page 1 is always fetched alone and decides
    # whether the rest may be addressed, so a blocked page 1 means the
    # workers never start. Driven here with the browser stubbed out, which
    # leaves exactly the concurrency logic under test.
    original = (eng.sync_playwright, eng._BrowserSession, eng._fetch_one_page)

    class Args:
        delay = 0
        mode = "listing"
        out = "x"

    def run(specs, concurrency, rows_for_page, die_on=()):
        fetched, lock = [], threading.Lock()

        def fake_fetch(session, args, pool, page_num, url):
            with lock:
                fetched.append(page_num)
            if page_num in die_on:
                raise RuntimeError("worker blew up on page %d" % page_num)
            outcome = eng.PageOutcome(page_num=page_num, url=url)
            outcome.products = rows_for_page(page_num)
            return outcome

        eng.sync_playwright = lambda: _FakePlaywright()
        eng._BrowserSession = lambda pw, args, pool, **kw: _FakeSession(pool)
        eng._fetch_one_page = fake_fetch
        try:
            results, unattempted, exhausted = eng._fetch_pages_concurrently(
                Args(), None, specs, concurrency)
        finally:
            (eng.sync_playwright, eng._BrowserSession,
             eng._fetch_one_page) = original
        return fetched, results, unattempted, exhausted

    # 1. Every page fetched exactly once, whatever the worker count.
    specs = [(n, "u%d" % n) for n in range(2, 12)]
    fetched, results, unattempted, exhausted = run(
        specs, 4, lambda n: ["row"])
    ok &= check("every queued page is fetched exactly once",
                sorted(fetched) == [n for n, _ in specs])
    ok &= check("every page produces an outcome",
                sorted(o.page_num for o in results) == [n for n, _ in specs])
    ok &= check("nothing is left unattempted when the listing does not end",
                unattempted == [] and not exhausted)

    # 2. Results arrive in whatever order the threads finish, which is
    #    exactly why the caller merges by page number instead of by arrival.
    #    Sorting them must reconstruct the page order.
    ok &= check("outcomes can be put back into page order",
                [o.page_num for o in sorted(results, key=lambda o: o.page_num)]
                == [n for n, _ in specs])

    # 3. The stop event. Asking for 50 pages of a listing that ends at page 5
    #    must not fetch 45 empty ones: workers check the event before taking
    #    more work, so at most (concurrency - 1) extra are already in flight.
    specs = [(n, "u%d" % n) for n in range(2, 51)]
    fetched, results, unattempted, exhausted = run(
        specs, 3, lambda n: [] if n >= 5 else ["row"])
    ok &= check("the end of the listing stops dispatch", exhausted)
    ok &= check("an exhausted listing costs at most (concurrency-1) extra "
                "fetches (%d fetched of 49 queued)" % len(fetched),
                len(fetched) <= 4 + 3)
    ok &= check("the pages never tried are reported, not counted as failed",
                unattempted and all(o.ok for o in results))
    ok &= check("unattempted pages are reported in order",
                unattempted == sorted(unattempted))

    # 4. A worker that dies must not hang the run, and must not swallow the
    #    pages its siblings did fetch.
    specs = [(n, "u%d" % n) for n in range(2, 8)]
    fetched, results, unattempted, exhausted = run(
        specs, 3, lambda n: ["row"], die_on={3})
    ok &= check("a worker that raises does not hang the run",
                len(results) + len(unattempted) + 1 >= len(specs))
    ok &= check("the pages other workers fetched still come back",
                any(o.page_num != 3 for o in results))
    return ok


def test_no_undefined_names():
    group("no engine references a name that does not exist")
    ok = True
    # This exists because of a bug that got all the way to a live run.
    # puppeteer_scraper.py called `detect_page_state(...)` on a line reached
    # only while fetching a page, after the import of that name had been
    # removed. The module imported fine, `--help` worked, `compileall`
    # passed, the whole offline suite passed and CI was green — and the
    # engine died with NameError on its first real page.
    #
    # Byte-compiling proves a file PARSES. It says nothing about whether the
    # names in it resolve, and the paths where they do not are exactly the
    # ones an offline suite cannot execute.
    for name in sorted(f for f in os.listdir(REPO_ROOT) if f.endswith(".py")):
        missing = _undefined_names(os.path.join(REPO_ROOT, name))
        detail = ", ".join("%s (line %d)" % (k, v[0])
                           for k, v in sorted(missing.items()))
        ok &= check("%s references no undefined name%s"
                    % (name, ": " + detail if missing else ""), not missing)

    # And a statement that can never RUN — see `_unreachable_statements`.
    for name in sorted(f for f in os.listdir(REPO_ROOT) if f.endswith(".py")):
        dead = _unreachable_statements(os.path.join(REPO_ROOT, name))
        ok &= check("%s has no statement the control flow can never reach%s"
                    % (name, "" if not dead else ": line %d" % dead[0]),
                    not dead)

    return ok


def test_dockerfile_copies_what_it_runs():
    group("the Docker image contains every module its entrypoint imports")
    ok = True
    path = os.path.join(REPO_ROOT, "Dockerfile")
    if not os.path.exists(path):
        return check("Dockerfile exists", False)

    # The Dockerfile COPYs an explicit list rather than the whole directory,
    # which is right — the image should not carry the test suite, the
    # fixtures or a stray .env. The cost is that the list can fall behind the
    # imports, and NOTHING else in this repo would notice: CI never builds
    # the image, so a missing module ships and the container dies with
    # ModuleNotFoundError on every invocation, `--help` included.
    #
    # That is not hypothetical. `proxy_pool.py` was missing from this list,
    # and playwright_scraper.py imports it at module level.
    raw = open(path, encoding="utf-8").read()
    joined = re.sub(r"\\\n\s*", " ", raw)          # fold line continuations
    copied = set()
    for line in joined.splitlines():
        if line.startswith("COPY "):
            copied.update(tok for tok in line.split() if tok.endswith(".py"))

    entrypoint = None
    m = re.search(r'ENTRYPOINT\s*\[([^\]]*)\]', joined)
    if m:
        parts = [x.strip().strip('"\'') for x in m.group(1).split(",")]
        entrypoint = next((x for x in parts if x.endswith(".py")), None)
    ok &= check("the Dockerfile names a Python entrypoint", bool(entrypoint))
    if not entrypoint:
        return False
    ok &= check("the entrypoint itself is copied into the image",
                entrypoint in copied)

    # Every LOCAL module the entrypoint reaches, transitively.
    local = {f[:-3] for f in os.listdir(REPO_ROOT) if f.endswith(".py")}

    def reached(module, seen=None):
        seen = seen if seen is not None else set()
        if module in seen:
            return seen
        seen.add(module)
        tree = ast.parse(open(os.path.join(REPO_ROOT, module + ".py"),
                              encoding="utf-8").read())
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module.split(".")[0]]
            for name in names:
                if name in local:
                    reached(name, seen)
        return seen

    needed = reached(entrypoint[:-3])
    missing = sorted(m + ".py" for m in needed if (m + ".py") not in copied)
    ok &= check("every module the entrypoint imports is COPYed (%s)"
                % (", ".join(missing) if missing else "none missing"),
                not missing)

    # The other direction is a warning, not a failure: diff_runs.py is copied
    # deliberately as a companion tool even though the engine never imports
    # it. But anything copied must at least still EXIST.
    gone = sorted(f for f in copied
                  if not os.path.exists(os.path.join(REPO_ROOT, f)))
    ok &= check("the Dockerfile copies no file that has been deleted (%s)"
                % (", ".join(gone) if gone else "none"), not gone)
    return ok


def test_sample_output():
    group("sample_output is cut from a real run")
    ok = True
    path = os.path.join(REPO_ROOT, "sample_output.json")
    if not os.path.exists(path):
        return check("sample_output.json exists", False)
    rows = json.load(open(path, encoding="utf-8"))
    ok &= check("the sample has rows", len(rows) > 0)
    names = [f.name for f in fields(Product)]
    ok &= check("its columns match the Product schema exactly",
                all(set(r) == set(names) for r in rows))
    text = json.dumps(rows, ensure_ascii=False)
    ok &= check("the sample carries no fabrication markers",
                not re.search(r"example\.com|lorem ipsum|FIXME|TODO|XXXX",
                              text, re.IGNORECASE))
    ok &= check("every sample row's sku is the site's own numeric lot id",
                all(re.fullmatch(r"\d{6,}", r.get("sku") or "") for r in rows))
    ok &= check("every sample row names the storefront it came from",
                all((r.get("source") or "") in ("catawiki.com",) + HOSTS
                    for r in rows))
    ok &= check("every sample row's URL is a lot on the storefront",
                all(re.match(r"https://www\.catawiki\.com/[A-Za-z-]{2,7}/l/\d+",
                             r.get("url") or "") for r in rows))
    ok &= check("no sample URL carries a tracking tail",
                not [r for r in rows if "utm_" in (r.get("url") or "")])
    # A sample cut from ONE mode would hide half the schema: a listing row
    # has the card's relative timer and no seller, a lot row has the seller,
    # the estimate and both absolute times and no timer at all. So the sample
    # spans them, or a reader judges the output by its sparsest rows alone.
    ok &= check("the sample spans both modes",
                any(r.get("listing_kind") == "lot" for r in rows)
                and any(r.get("listing_kind") in ("category", "search",
                                                  "auction") for r in rows))
    ok &= check("the sample shows a lot row's seller and estimate",
                any(r.get("seller_id") and r.get("estimate_min") for r in rows))
    ok &= check("the sample shows the bid kinds it can carry",
                {r.get("bid_kind") for r in rows} & {"current", "final",
                                                     "starting"})
    # The invariant, in the shipped sample too: a null price is a reserve lot.
    ok &= check("no sample row has a null price without its reserve flag",
                all(r.get("reserve_price_set")
                    for r in rows if r.get("price") is None))
    ok &= check("the sample shows a real price_source",
                {r.get("price_source") for r in rows}
                <= {"next_data+dom", "next_data", "dom"}
                and any(r.get("price_source") for r in rows))

    csv_path = os.path.join(REPO_ROOT, "sample_output.csv")
    if os.path.exists(csv_path):
        header = open(csv_path, encoding="utf-8").read().split("\n")[0]
        ok &= check("the sample CSV header matches the schema",
                    header.strip().split(",") == names)
    return ok


def test_captcha():
    group("captcha detection and reconciliation")
    ok = True
    from captcha_solver import CaptchaChallenge

    # Format 2: the site's own wrapper element carries the config as
    # attributes, with the execute() call inside a bundled file that never
    # appears as readable inline script.
    widget = ('<captcha-widget data-captcha-type="recaptcha" data-version="v3" '
              'data-sitekey="6LcABCDEFGHIJKLMNOPQRSTUVWXYZ0123" '
              'data-action="submit"></captcha-widget>')
    c = detect_recaptcha_v3(widget, "https://www.catawiki.com/")
    ok &= check("a captcha-widget declaring v3 is detected",
                c is not None and c.kind == "recaptcha_v3")

    # A sitekey is at least 20 characters; a short string next to
    # data-sitekey is not one, and treating it as one would send a malformed
    # task to the API and bill for the answer.
    ok &= check("a too-short sitekey is not accepted as a challenge",
                detect_recaptcha_v3('<div data-sitekey="short" '
                                    'class="g-recaptcha"></div>',
                                    "https://www.catawiki.com/") is None)
    ok &= check("a page with no reCAPTCHA at all is not a challenge",
                detect_recaptcha_v3(SEARCH_EN, SEARCH_URL) is None)

    # THE LOADER WINS. A site's own wrapper can declare v3 while the Google
    # loader it actually ships is the v2-invisible signature
    # (render=explicit, size=invisible, a bframe challenge iframe). v3
    # parameters sent for a v2-invisible widget buy a token the site
    # rejects — so the runtime reading is authoritative and the two
    # detectors are reconciled rather than short-circuited.
    static_v3 = CaptchaChallenge(kind="recaptcha_v3", sitekey="6LcABC" + "X" * 20,
                                 action="submit", source="html")
    runtime_v2 = CaptchaChallenge(kind="recaptcha_v2_invisible",
                                  sitekey="6LcABC" + "X" * 20,
                                  source="runtime", size="invisible")
    merged = reconcile_detections(static_v3, runtime_v2)
    ok &= check("when the detectors disagree, the live loader wins",
                merged is not None and merged.kind == "recaptcha_v2_invisible")
    ok &= check("...and the real action from the static markup is kept",
                merged.action == "submit")
    ok &= check("one detector alone is still used when only it fires",
                reconcile_detections(static_v3, None) is static_v3
                and reconcile_detections(None, runtime_v2) is runtime_v2)
    ok &= check("neither firing means no challenge",
                reconcile_detections(None, None) is None)

    # Deliberately absent: no solver for a first-party image captcha. This
    # site has no such page — measured 2026-09-10, its refusal is not a page
    # at all: the HTTP/2 stream is reset and nothing arrives — so a solver
    # for one would be dead code that looks load-bearing. Pinned so that
    # reintroducing it is a decision rather than a drift.
    import captcha_solver
    ok &= check("no first-party image-captcha solver was ported",
                not [n for n in dir(captcha_solver)
                     if "image" in n.lower() and "captcha" in n.lower()])
    # ...while the DETECTORS stay broad, which is the family's standing
    # policy: which challenge a visitor meets depends on the exit country and
    # on what the address has been doing.
    # The marker set is deliberately SHORT on this site, and shorter than
    # the sibling repos'. None of these has ever been observed here —
    # a refused request is not a page at all — so every entry is a guess
    # about a bot manager that might be switched on between deploys, and a
    # long list of guesses is not better than a short one.
    #
    # `cf-turnstile` is specifically EXCLUDED: the Scraping Browser's
    # auto-solve extension injects a cf-turnstile hunter into every page it
    # loads, so it would fire on good pages fetched over --cdp-endpoint.
    # Pinned in both directions so a broadening edit is a decision.
    markers = {m.lower() for m in product_parser.BOT_CHALLENGE_MARKERS}
    ok &= check("detection covers the shapes a challenge here would take",
                {"recaptcha/api2/anchor", "recaptcha/api.js", "data-sitekey",
                 "hcaptcha.com/captcha"} <= markers)
    # `cf-turnstile` is specifically EXCLUDED as a bare marker: the Scraping
    # Browser's auto-solve extension injects a turnstile hunter into every
    # page it loads, so a bare match would fire on good pages fetched over
    # --cdp-endpoint. The full challenge host is fine, since the extension
    # does not reference it.
    ok &= check("...and no bare cf-turnstile marker, which our own extension "
                "would trip",
                "cf-turnstile" not in markers)
    # Refusals are a SEPARATE set: `--solve-captcha` must not pay for a page
    # that carries nothing to solve.
    block = {m.lower() for m in product_parser.BLOCK_MARKERS}
    ok &= check("the refusal markers are their own set and do not overlap",
                block and not (block & markers))
    return ok


def _placeholder_reads_unset(raw):
    """Whether env_config would treat `raw` as "not configured".

    Goes through the real rule — `env_config.env_value`, which is where the
    placeholder logic lives — rather than reimplementing it, because a
    reimplementation is what drifts. The variable is set in os.environ
    directly and restored afterwards: `load_env` only fills variables that
    are not already set, so writing a temporary .env would be shadowed by
    whatever the suite has already loaded.
    """
    name = "CATAWIKI_CDP_ENDPOINT"
    saved = os.environ.get(name)
    try:
        os.environ[name] = raw
        with io.StringIO() as buf, redirect_stdout(buf):
            value = env_config.env_value(name)
    finally:
        if saved is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = saved
    return value is None


def test_env_config():
    group("env_config")
    ok = True
    ok &= check("the env keys are this site's, not another repo's",
                set(env_config.ENV_KEYS) ==
                {"TWOCAPTCHA_KEY", "CATAWIKI_CDP_ENDPOINT",
                 "CATAWIKI_PROXY", "CATAWIKI_URL"})

    # .env.example must document exactly the variables the code reads, in
    # both directions. It drifts otherwise, and a documented-but-unread
    # variable is worse than an undocumented one.
    example = os.path.join(REPO_ROOT, ".env.example")
    documented = set()
    if os.path.exists(example):
        for line in open(example, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                documented.add(line.split("=", 1)[0].strip())
    ok &= check(".env.example documents exactly the variables the code reads",
                documented == set(env_config.ENV_KEYS))

    # A variable mapped onto a flag with a non-empty default would be
    # silently inert, because the loader only fills UNSET values: a setting
    # that looks configurable and is not.
    ok &= check("no env variable is mapped onto --out (it has a default)",
                "out" not in env_config.ENV_KEYS.values())

    # A COPIED .env.example MUST READ AS UNSET, and a literal-only check is
    # not enough to make that true. This repo documents its two credentialled
    # URLs the way the vendor does, with the parts you fill in written in
    # braces:
    #
    #     ws://{login}-zone-scraping_browser-…-pid-{profileId}:{password}@…
    #     http://{user}:{password}@ap.proxy.2captcha.com:2334
    #
    # Before the brace check existed the loader reported both of those as
    # CONFIGURED, so `cp .env.example .env` and a run connected to
    # cb.2captcha.com with the string `{login}-zone-…` as its username and
    # got a 401 — a confusing failure a long way from its cause, which is
    # what §3's rule exists to prevent.
    for raw in ('ws://{login}-zone-scraping_browser-country-id-pid-'
                '{profileId}:{password}@cb.2captcha.com:9222',
                'http://{user}:{password}@ap.proxy.2captcha.com:2334',
                'your_2captcha_api_key_here'):
            ok &= check("a placeholder value reads as unset: %s..." % raw[:34],
                        _placeholder_reads_unset(raw))
    # ...and a REAL value still reads as set, or the guard has eaten the
    # feature it was protecting.
    ok &= check("a real value is not mistaken for a placeholder",
                _placeholder_reads_unset(
                    "ws://acct1-zone-scraping_browser-country-id-pid-p1:"
                    "secret@cb.2captcha.com:9222") is False)
    # The one variable a copied example leaves USABLE is the target URL,
    # which carries no credential and is a working default.
    ok &= check("the example's default URL is usable as-is",
                _placeholder_reads_unset(
                    "https://www.catawiki.com/en/c/333-watches"
                    "kopi-bubuk") is False)

    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, ".env")
        with open(path, "w", encoding="utf-8") as f:
            f.write("TWOCAPTCHA_KEY=fromfile\n")
            f.write("CATAWIKI_URL=https://www.catawiki.com/en/s?q=x\n")
            f.write("NOT_A_REAL_KEY=1\n")

        class A:
            twocaptcha_key = None
            url = None
            cdp_endpoint = None
            proxy = None

        a = A()
        env_config.load_env(path)
        env_config.apply(a, quiet=True)
        ok &= check("a value in .env fills an unset flag",
                    a.twocaptcha_key == "fromfile")

        b = A()
        b.twocaptcha_key = "fromflag"
        env_config.apply(b, quiet=True)
        # A .env must never override something the caller typed.
        ok &= check("an explicit flag beats .env", b.twocaptcha_key == "fromflag")
        # A typo is REPORTED rather than silently ignored.
        ok &= check("an unrecognised variable in .env is reported",
                    "NOT_A_REAL_KEY" in env_config.unknown_keys(path))
    return ok


def test_proxy_pool():
    group("proxy_pool: credentials never reach argv or logs")
    ok = True
    url = "http://user:secret@eu.proxy.2captcha.com:2334"
    masked = mask(url)
    ok &= check("credentials are masked in logs", "secret" not in masked)
    # The host and port are KEPT: which exit a run used is the point of the
    # log and is not the secret.
    ok &= check("...but the host and port survive masking",
                "eu.proxy.2captcha.com:2334" in masked)

    pw = to_playwright(url)
    # A `--proxy-server=` value becomes part of the browser's command line,
    # readable by anything that can run `ps`. The credentials go through the
    # driver's own fields instead.
    ok &= check("the server string handed to the browser has no credentials",
                "secret" not in pw["server"])
    ok &= check("credentials go through the driver's own fields",
                pw["username"] == "user" and pw["password"] == "secret")

    scrubbed, creds = split_credentials(url)
    ok &= check("split_credentials separates the two",
                scrubbed == "http://eu.proxy.2captcha.com:2334"
                and creds == ("user", "secret"))

    pool = ProxyPool(["http://a:1", "http://b:2", "http://c:3"])
    ok &= check("a pool reports its size", len(pool) == 3)
    first = pool.current
    pool.advance("test")
    ok &= check("advancing moves to another exit", pool.current != first)
    # `.proxies` hands back a COPY, so a worker building its own pool from it
    # cannot mutate the parent's list. Two threads sharing one mutable list
    # is the bug that makes concurrency stop being worth it.
    copy = pool.proxies
    copy.append("http://d:4")
    ok &= check("the pool hands out a copy of its exits, not the list itself",
                len(pool) == 3)

    # Workers start on DIFFERENT exits, each with its own pool object, so no
    # thread needs a lock: the concurrency is safe by construction rather
    # than by discipline. Tested through the engine's own helper, because
    # that is where the offset actually lives.
    try:
        import playwright_scraper
    except ImportError:
        playwright_scraper = None
    if playwright_scraper is not None:
        exits = [playwright_scraper._worker_pool(pool, i).current
                 for i in range(3)]
        ok &= check("three workers start on three different exits",
                    len(set(exits)) == 3)
        ok &= check("a worker with no pool gets none",
                    playwright_scraper._worker_pool(None, 0) is None)

    # A pool of one is legal and must not rotate itself into an index error.
    one = ProxyPool(["http://only:1"])
    one.advance("nowhere else to go")
    ok &= check("a single-exit pool survives a rotation",
                one.current == "http://only:1")
    ok &= check("an empty pool is refused rather than silently accepted",
                _raises(lambda: ProxyPool([])))

    # This used to assert that "http://host:port:login:pass" — a line from a
    # proxy LIST FILE — "is understood", checking only that parse_proxy_line
    # did not reject it. It returned the string unchanged, so the check
    # passed; the value was never usable, and it blew up several calls later.
    # A test that asserts a function did not complain is not a test that its
    # answer was right.
    #
    # A proxy LIST FILE line pasted where a proxy URL belongs. This is the
    # mistake a new user makes — the file format is
    # scheme://host:port:login:password and the flag wants
    # http://login:password@host:port — and it reached a real CI run.
    #
    # It used to sail through parse_proxy_line (which never looked at the
    # port) and blow up much later inside to_playwright as an uncaught
    # ValueError: exit 1, a crash, where it should be exit 2, bad usage. And
    # the traceback printed the login AND the password into a public CI log.
    from proxy_pool import ProxyError
    pasted = ("http://eu.proxy.2captcha.com:2334:"
              "SOMELOGIN-zone-custom-region-de:SOMEPASSWORD")
    raised = None
    try:
        parse_proxy_line(pasted, source="CATAWIKI_PROXY")
    except ProxyError as exc:
        raised = str(exc)
    ok &= check("a proxy-list line pasted as a URL is refused, not crashed on",
                raised is not None)
    ok &= check("...and the refusal says what the value should look like",
                raised is not None and "login:password@host:port" in raised)
    ok &= check("...and neither the login nor the password is in the message",
                raised is not None
                and "SOMEPASSWORD" not in raised and "SOMELOGIN" not in raised)

    # mask() is the last thing standing between a password and a log, and it
    # is called precisely when the value is already wrong. It read
    # `parsed.port`, which urlparse computes lazily and which RAISES on a
    # malformed authority — so the masker blew up on exactly the input that
    # most needed masking. A masker that raises is worse than a vague one.
    ok &= check("mask() does not raise on a malformed URL",
                "SOMEPASSWORD" not in mask(pasted))
    for junk in ("::::", "not a url", "http://", "://x", ""):
        try:
            mask(junk)
            raised_here = False
        except Exception:
            raised_here = True
        ok &= check("mask(%r) does not raise" % junk, not raised_here)
    ok &= check("mask() still keeps host and port on a good URL",
                mask("http://u:p@h.example:8080") == "http://***:***@h.example:8080")

    # PINNED LIMITATION, not a defence. mask() takes a bare URL; given a
    # SENTENCE containing one it returns "?://?" — the password is gone,
    # which is the property that matters, but so is the host and port the log
    # was written to show. Every caller here passes the URL as its own `%s`
    # argument for that reason, and `_mask_credentials()` is what handles
    # arbitrary text. Asserting the CURRENT behaviour makes a future swap a
    # failing check rather than an unreadable log (§10).
    sentence = "a http://u:supersecret@h.example:8080 b"
    ok &= check("mask() on a sentence loses the host — the documented limit",
                mask(sentence) == "?://?")
    ok &= check("...but never the password", "supersecret" not in mask(sentence))
    ok &= check("every mask() call site passes a bare URL, not a sentence",
                not [ln for f in ("playwright_scraper.py", "puppeteer_scraper.py",
                                  "selenium_scraper.py", "proxy_pool.py")
                     for ln in open(os.path.join(REPO_ROOT, f),
                                    encoding="utf-8").read().split("\n")
                     if re.search(r'[^_]mask\(f?["\']', ln)])
    # ...and the engines' own masker handles a sentence, globally. A masker
    # that fixes the first occurrence and prints the password the other four
    # times looks exactly like one that works.
    for name in _ENGINE_MODULES:
        try:
            mod = __import__(name)
        except ImportError:
            continue
        many = ("x ws://u:supersecret@h:1 y ws://u:supersecret@h:1 "
                "z http://u:supersecret@h:2")
        out = mod._mask_credentials(many)
        ok &= check("%s._mask_credentials masks EVERY occurrence" % name,
                    "supersecret" not in out)
        ok &= check("%s._mask_credentials keeps the surrounding text" % name,
                    out.startswith("x ") and out.endswith(":2")
                    and "h:1" in out)
    return ok


# The three engines. Playwright is primary; the other two exist for parity
# and are demoted in priority, not in correctness — all three must agree on
# exit codes, run status, and whether a run crashes or spends money.
_ENGINE_MODULES = ("playwright_scraper", "puppeteer_scraper",
                   "selenium_scraper")


# Stand-ins for a real browser session, so the concurrency machinery can be
# driven with the browser stubbed out. A live run cannot always reach it:
# page 1 is fetched alone and decides whether the rest may be addressed, so a
# blocked page 1 means the workers never start.
class _FakeSession:
    """Stands in for a _BrowserSession: opened, closed, carries a pool."""

    def __init__(self, pool=None):
        self.pool = pool
        self.closed = False

    def open(self):
        return self

    def close(self):
        self.closed = True


class _FakePlaywright:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


STATE_POLICY_NAMES = ("content", "empty", "blocked", "challenge", "unknown")


def test_engines(skips):
    group("engines: all three must behave identically")
    ok = True
    loaded = {}
    for name in _ENGINE_MODULES:
        try:
            loaded[name] = __import__(name)
        except ImportError as e:
            # Reported, never swallowed: "skipped, engine absent" reads
            # exactly like a passing run, and CI's engine-smoke job fails if
            # this list is non-empty.
            skips.append("%s (%s)" % (name, e))

    for name, mod in loaded.items():
        ok &= check("%s exposes scrape() and parse_args()" % name,
                    hasattr(mod, "scrape") and hasattr(mod, "parse_args"))
        # The engines must reach the shared policy rather than carry copies.
        src = inspect.getsource(mod)
        ok &= check("%s takes its readiness policy from page_flow" % name,
                    "page_flow.ready_selector" in src)
        ok &= check("%s takes its state policy from page_flow" % name,
                    "page_flow.should_retry" in src or "page_flow.classify" in src)
        # NO scroll anywhere, and it is asserted rather than merely absent:
        # three scrolls to the document's own bottom added zero cards and left
        # the page height unchanged on all three page kinds, so an engine that
        # grew a loop back would be buying latency for nothing -- and would
        # disagree with its twins about what a settled page is. A sibling repo
        # cannot see one product without the scroll, which is exactly why this
        # was measured here instead of ported.
        ok &= check("%s has no scroll loop of its own" % name,
                    "scroll_until_settled" not in src
                    and "scroll_to_bottom" not in src)
        ok &= check("%s passes no JS across the page_flow boundary" % name,
                    "page_flow.wait_for_count" in src)
        # "Not painted yet" is not a fault, and every engine has to make that
        # distinction the same way — the first live search run of the
        # Playwright engine reported 0 rows and exit 4 because it did not.
        # §17's rule, applied to this repo's own additions: a policy
        # constant or a helper that nothing consults is the same defect as
        # dead code, and harder to see because the prose reads like
        # enforcement. All three of these were written, documented in the
        # README, and called by nothing until this check was added.
        ok &= check("%s shows the block advice rather than only computing it"
                    % name, "page_flow.block_advice" in src)
        ok &= check("%s reports the pages beyond the site's own cap" % name,
                    "parser_page_cap" in src and "deeper than the site will" in src)
        ok &= check("%s passes the page's own lot count to the readiness "
                    "threshold, so a short last page does not time out" % name,
                    "page_flow.expected_lots" in src)
        ok &= check("%s waits for an unpainted page instead of retrying it"
                    % name, "page_flow.is_unpainted" in src)
        # Credentials never reach a log, in any engine.
        ok &= check("%s masks credentials globally, not just once" % name,
                    "pass@" not in mod._mask_credentials(
                        "a ws://user:pass@h:1/ b ws://user:pass@h:1/"))
        ok &= check("%s refuses a host that is not Catawiki" % name,
                    "is_supported_host" in src)
        # The same modes in every engine — a mode one engine offers and
        # another does not is the drift page_flow.py and finish_run() exist
        # to prevent, one level up. There are exactly two here, and no shop
        # mode: a seller's own page is a different application shell whose
        # markup has not been measured, and a mode that ships untested is
        # worse than one that is absent.
        ok &= check("%s offers exactly listing, lot and auctions" % name,
                    '"listing", "lot", "auctions"]' in src)
        ok &= check("%s has no shop mode" % name, '"shop"' not in src)
        # HEADFUL is the default here, against headless in every sibling: a
        # headless browser is refused with HTTP 403 from every address tried,
        # residential included, while a real window is served from the same
        # ones. A --headless default would be a scraper whose default cannot
        # fetch the site.
        ok &= check("%s defaults to headful" % name,
                    'dest="headless", action="store_false",' in src
                    and "default=False" in src)

    # THE FLAG CONTRACT, and the exact ways the engines differ from it.
    #
    # §9 lists the flags every engine must offer. Checked rather than
    # trusted, because a flag one engine has and another does not is the
    # drift page_flow.py and finish_run() exist to prevent, one level up —
    # and because the README documents these differences by name, so a new
    # divergence has to update the README or fail here.
    contract = ("--url --pages --category --format --out --delay --retries "
                "--retry-delay --concurrency --proxy --proxy-file "
                "--proxy-rotate --proxy-shuffle --proxy-block-retries "
                "--twocaptcha-key --captcha-api --solve-captcha --min-score "
                "--cdp-endpoint --allow-empty --dump-html").split()
    flags = {}
    for name in _ENGINE_MODULES:
        path = os.path.join(REPO_ROOT, name + ".py")
        if not os.path.exists(path):
            continue
        src = open(path, encoding="utf-8").read()
        flags[name] = set(re.findall(r'add_argument\(\s*"(--[a-z-]+)"', src))
        missing = [f for f in contract if f not in flags[name]]
        ok &= check("%s offers every flag in the family contract" % name,
                    not missing)
        if missing:
            print("        missing: %s" % missing)
        ok &= check("%s offers --headless and --headful" % name,
                    "--headless" in flags[name] and "--headful" in flags[name])
    if len(flags) == 3:
        pw = flags["playwright_scraper"]
        # The differences the README states, pinned in both directions: a NEW
        # divergence fails here, and closing one of these also fails here, so
        # the README cannot quietly go stale either way.
        ok &= check("pyppeteer differs from playwright by exactly the "
                    "documented four flags",
                    sorted(pw - flags["puppeteer_scraper"])
                    == ["--fingerprint", "--fp-country", "--fp-tags",
                        "--locale"])
        ok &= check("selenium differs from playwright by exactly --locale",
                    sorted(pw - flags["selenium_scraper"]) == ["--locale"])

    # `--fp-tags` MUST DEFAULT TO ONE OS-FAMILY TAG. It shipped in this
    # family as "Windows,Chrome,Desktop", which the fingerprint API rejects
    # with HTTP 400 — so --fingerprint failed on every invocation, which is
    # one of the six defects §16 of the family notes lists. Measured
    # 2026-09-10 against the live API: `Windows` succeeds;
    # `Windows,Chrome,Desktop`, `Chrome` and `Desktop` each 400.
    for name in _ENGINE_MODULES:
        path = os.path.join(REPO_ROOT, name + ".py")
        if not os.path.exists(path):
            continue
        src = open(path, encoding="utf-8").read()
        m = re.search(r'--fp-tags"\s*,\s*default="([^"]*)"', src)
        if m is None:
            continue          # pyppeteer has no fingerprint flags
        ok &= check("%s's --fp-tags default is ONE tag the API accepts"
                    % name,
                    "," not in m.group(1)
                    and m.group(1) in ("Windows", "Microsoft Windows",
                                       "Android"))

    # EVERY page_flow CALL IN EVERY ENGINE, CHECKED AGAINST THE REAL
    # SIGNATURE. This is the general form of a bug the first live run of the
    # pyppeteer engine found: `classify(html, status, url)` took `status`
    # positionally, and two of the three engines called it as
    # `classify(html, url=…)` because they have no response object to read a
    # status from. Both crashed with TypeError on their FIRST fetch — and
    # that was invisible to import, to --help, to compileall, to the AST
    # undefined-name walk and to 400+ green offline checks, because none of
    # those calls a function the way a live run does.
    #
    # An offline suite cannot execute a fetch. It CAN bind every call's
    # arguments to the callee's signature, which is the same check the
    # interpreter does at the moment of the call, minus the browser.
    import inspect as _inspect
    for name in _ENGINE_MODULES:
        path = os.path.join(REPO_ROOT, name + ".py")
        if not os.path.exists(path):
            continue
        tree = ast.parse(open(path, encoding="utf-8").read())
        bad = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if not (isinstance(fn, ast.Attribute)
                    and isinstance(fn.value, ast.Name)
                    and fn.value.id == "page_flow"):
                continue
            target = getattr(page_flow, fn.attr, None)
            if not callable(target):
                bad.append("%s: page_flow has no %s()" % (name, fn.attr))
                continue
            try:
                sig = _inspect.signature(target)
            except (TypeError, ValueError):
                continue
            # Bind PLACEHOLDERS, not values: this checks arity and keyword
            # names, which is what drifts. `*args` in the call (none today)
            # would make the binding unknowable, so it is skipped rather
            # than guessed at.
            if any(isinstance(a, ast.Starred) for a in node.args) or \
                    any(k.arg is None for k in node.keywords):
                continue
            try:
                sig.bind(*[object()] * len(node.args),
                         **{k.arg: object() for k in node.keywords})
            except TypeError as exc:
                bad.append("%s:%d page_flow.%s(...) — %s"
                           % (name, node.lineno, fn.attr, exc))
        ok &= check("every page_flow call in %s matches its signature" % name,
                    not bad)
        for line in bad:
            print("        %s" % line)

    # The same check for product_parser, which the engines call as often.
    for name in _ENGINE_MODULES:
        path = os.path.join(REPO_ROOT, name + ".py")
        if not os.path.exists(path):
            continue
        tree = ast.parse(open(path, encoding="utf-8").read())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "product_parser":
                imported.update(a.asname or a.name for a in node.names)
        bad = []
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id in imported):
                continue
            target = getattr(product_parser, node.func.id, None)
            if not callable(target):
                continue
            if any(isinstance(a, ast.Starred) for a in node.args) or \
                    any(k.arg is None for k in node.keywords):
                continue
            try:
                _inspect.signature(target).bind(
                    *[object()] * len(node.args),
                    **{k.arg: object() for k in node.keywords})
            except TypeError as exc:
                bad.append("%s:%d %s(...) — %s"
                           % (name, node.lineno, node.func.id, exc))
        ok &= check("every product_parser call in %s matches its signature"
                    % name, not bad)
        for line in bad:
            print("        %s" % line)

    # And the two-argument call itself, pinned: `status` must stay optional,
    # because two of the three engines have no status to pass.
    ok &= check("page_flow.classify works with no status, as two engines "
                "call it",
                page_flow.classify("<html>x</html>",
                                   url=SEARCH_URL) in STATE_POLICY_NAMES)

    # For "it must pass with no engine installed" to mean anything, each
    # engine has to import its driver at MODULE level — otherwise the module
    # imports cleanly with the library absent, the group never skips, and the
    # CI job that exists to catch that cannot. This drifts back silently, so
    # it is asserted rather than trusted.
    driver_imports = {"playwright_scraper": "playwright",
                      "puppeteer_scraper": "pyppeteer",
                      "selenium_scraper": "selenium"}
    for name, lib in driver_imports.items():
        path = os.path.join(REPO_ROOT, name + ".py")
        if not os.path.exists(path):
            continue
        tree = ast.parse(open(path, encoding="utf-8").read())
        top_level = set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                top_level.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                top_level.add(node.module.split(".")[0])
        ok &= check("%s imports %s at module level, so an absent library skips"
                    % (name, lib), lib in top_level)
    return ok


def _raises_type(fn, exc_type) -> bool:
    """True if `fn()` raises exactly `exc_type` (or a subclass)."""
    try:
        fn()
    except exc_type:
        return True
    except Exception:  # noqa: BLE001 — a different type is a failed check
        return False
    return False


def _no_secret_in(fn, secret: str) -> bool:
    """True if `fn()` raises and the secret is absent from the message."""
    try:
        fn()
    except Exception as e:  # noqa: BLE001
        return secret not in str(e)
    return False


# Names Python provides that are not imports and not assignments.
_MODULE_DUNDERS = {"__file__", "__name__", "__doc__", "__package__",
                   "__spec__", "__loader__", "__builtins__", "__debug__"}


def _undefined_names(path):
    """Names loaded in `path` that are never imported, defined or assigned.

    A deliberately coarse approximation — it pools every binding in the file
    rather than tracking scopes, so it under-reports and never invents a
    problem. That is the right trade here: this exists to catch a name that
    is nowhere at all, and a false positive would be worse than a miss.
    """
    tree = ast.parse(open(path, encoding="utf-8").read())
    bound = set(dir(builtins)) | _MODULE_DUNDERS
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            bound |= {(a.asname or a.name.split(".")[0]) for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            bound |= {(a.asname or a.name) for a in node.names}
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                               ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            bound.add(node.id)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, ast.Global):
            bound |= set(node.names)
    missing = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) \
                and node.id not in bound:
            missing.setdefault(node.id, []).append(node.lineno)
    return missing



def _unreachable_statements(path):
    """Line numbers of statements that can never run.

    A statement sitting after a `return`/`raise`/`break`/`continue` in the
    SAME block. Deliberately narrow: it makes no claim about conditions or
    reachability in general, only about a block whose control flow has
    already left. Measured across the eighteen repos in this family on
    2026-09-16 it reported six problems and zero false positives.

    `_undefined_names` above cannot see this class at all, by design — it
    pools every binding in the file rather than tracking scopes, so a name
    used inside dead code passes as long as anything else in the module
    binds it. What was hiding there: a function whose `def` line had been
    lost, leaving its docstring and body absorbed into the end of the
    function above it. Identical in six repos, present since each one's
    first commit, invisible to import, `--help`, `compileall` and every
    green run of this suite.
    """
    tree = ast.parse(open(path, encoding="utf-8").read())
    dead = []
    for node in ast.walk(tree):
        for field in ("body", "orelse", "finalbody"):
            block = getattr(node, field, None)
            if not isinstance(block, list):
                continue
            for i, stmt in enumerate(block[:-1]):
                if isinstance(stmt, (ast.Return, ast.Raise,
                                     ast.Continue, ast.Break)):
                    dead.append(block[i + 1].lineno)
                    break
    return sorted(dead)

def test_x_debug_header_is_redacted():
    """SECURITY.md names the Scraper API's x-debug header as a place
    credentials reach a log unmasked. It was then logged verbatim.

    The fixtures are assembled from pieces rather than written out whole,
    because this file is scanned by the credential check like every other
    and a fixture that LOOKS like a live key fails it. They are the SHAPES a
    credential takes, not the literals this repo happens to contain today.
    """
    try:
        import scraper_api_client as sac
    except ImportError:
        return False

    pw = "SeCr" + "EtPw"
    key = "abcdef01" * 4
    raw = ("cdpurl=ws://acct-zone-scraping_browser-pid-7:" + pw
           + "@cb.2captcha.com:9222 cost=0.00145 key=" + key + " status=200")
    out = sac._redact_debug_header(raw)
    ok = True
    ok &= check("x-debug: the credential and the key are gone",
                      pw not in out and key not in out)
    ok &= check("x-debug: the cost, host and status survive",
                      "cost=0.00145" in out and "cb.2captcha.com:9222" in out
                      and "status=200" in out)

    s1, s2 = "secret" + "one", "secret" + "two"
    two = sac._redact_debug_header(
        "a=http://u1:" + s1 + "@h1:1 b=http://u2:" + s2 + "@h2:2")
    ok &= check("x-debug: both credentials are masked, not just the first",
                      s1 not in two and s2 not in two)

    src = inspect.getsource(sac)
    ok &= check("x-debug: the log line calls the redactor",
                      'logger.info("x-debug: %s", _redact_debug_header(debug))' in src)
    return ok


def test_parse_is_gated_on_the_policy():
    group("the engines read STATE_POLICY's parse column")
    ok = True
    # `should_parse` existed, was correct, and had NO consumer: every
    # engine parsed whatever reached the parse line, so the `parse` column
    # of STATE_POLICY decided nothing and an engine could disagree with the
    # table — and with its twins — without anything noticing. That is the
    # defect CLAUDE.md §17 names for constants, with a function instead.
    #
    # Measured across the family on 2026-09-23 by counting definitions
    # against readers: 7 of 24 repos defined it and none called it.
    import glob as _glob
    engines = sorted(_glob.glob(os.path.join(REPO_ROOT, "*_scraper.py")))
    ok &= check("there are engines to check (%d)" % len(engines), engines)
    for path in engines:
        name = os.path.basename(path)
        text = open(path, encoding="utf-8").read()
        if "_parse_for_mode" not in text:
            continue
        ok &= check("%s reads the parse decision from the policy" % name,
                    "should_parse(" in text)
        # And nothing parses unconditionally any more: a bare
        # `products = _parse_for_mode(` is the shape that ignored the table.
        ok &= check("%s does not parse unconditionally" % name,
                    not re.search(r"products = _parse_for_mode\(", text))
    # Every state the policy names must be answerable — a typo'd state name
    # would make should_parse fall through to its default for ever.
    for state in page_flow.STATE_POLICY:
        ok &= check("should_parse answers for %r" % state,
                    isinstance(page_flow.should_parse(state), bool))

    # The live bug this repo had, pinned as a value rather than a shape: a
    # no-results search page carries an "other lots you might like" rail,
    # and reading it wrote three real lots as if they were hits.
    import ast as _ast
    _tree = _ast.parse(open(os.path.join(REPO_ROOT, "smoke_test.py"),
                            encoding="utf-8").read())
    _html = next((n.value.value for n in _ast.walk(_tree)
                  if isinstance(n, _ast.Assign)
                  and getattr(n.targets[0], "id", "") == "SEARCH_NO_RESULTS"),
                 None)
    if _html:
        _state = detect_page_state(_html, 200, "")
        ok &= check("a no-results page still classifies as empty",
                    _state == "empty")
        ok &= check("the policy says do not parse it",
                    page_flow.should_parse(_state) is False)
        ok &= check("...and the parser alone WOULD have yielded rows "
                    "(so the gate is load-bearing)",
                    len(parse_products(_html, "")) > 0)
    return ok


def main() -> int:
    ok = True
    # Checks that could not run because an optional engine library is absent.
    # Reported at the end: a suite that silently skips part of itself and
    # still says "all passed" is the same defect as code that reports success
    # without checking that what it wanted actually happened.
    skips = []

    ok &= test_parse_is_gated_on_the_policy()
    ok &= test_price_parsing()
    ok &= test_bid_state()
    ok &= test_listing_values()
    ok &= test_reserve_invariant()
    ok &= test_lot_page()
    ok &= test_auction_page_is_a_listing()
    ok &= test_auctions_index()
    ok &= test_urls()
    ok &= test_pagination()
    ok &= test_page_state()
    ok &= test_page_flow()
    ok &= test_output_contract()
    ok &= test_writers()
    ok &= test_finish_run()
    ok &= test_diff()
    ok &= test_captcha()
    ok &= test_env_config()
    ok &= test_proxy_pool()
    ok &= test_engines(skips)
    ok &= test_pyppeteer_teardown_noise()
    ok &= test_canary_separates_access_from_defect()
    ok &= test_ci_checks_is_actually_wired_up()
    ok &= test_no_capture_leaks()
    ok &= test_wording()
    ok &= test_fingerprint_client_reads_env()
    ok &= test_fingerprint_application()
    ok &= test_credentials_never_reach_a_log()
    ok &= test_concurrent_dispatch(skips)
    ok &= test_no_undefined_names()
    ok &= test_dockerfile_copies_what_it_runs()
    ok &= test_sample_output()
    ok &= test_x_debug_header_is_redacted()

    print()
    if _failures:
        print("%d check(s) FAILED:" % len(_failures))
        for f in _failures:
            print("  - %s" % f)
    if skips:
        print("%d engine group(s) SKIPPED — an optional engine library is "
              "absent. CI's engine-smoke job installs all three and fails if "
              "this list is non-empty, because a skip reads exactly like a "
              "passing run:" % len(skips))
        for s in skips:
            print("  - %s" % s)
    print("smoke_test: %s" % ("OK" if ok else "FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
