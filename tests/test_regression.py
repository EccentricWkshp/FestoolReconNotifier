"""
Headless regression tests for Festool Recon Monitor.

Covers the previously-broken behaviours (webhook serialisation, backup/restore,
filter matching, credential encryption) and the SKU analytics.

Run from the project root:
    python tests/test_regression.py
"""
import csv
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import warnings
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Project code must be free of deprecation warnings; third-party noise is
# not our problem.
warnings.filterwarnings(
    'error', category=DeprecationWarning,
    module=r'(database|scraper|notifications|utils|analytics)\..*'
)

from analytics.sku_analytics import SkuAnalytics, discount_pct
from analytics.product_types import normalize_type, type_of
from catalog.extractor import CatalogItem, extract_page, year_from_filename
from database.manager import DatabaseManager
from database.models import Exclusion, Product
from scraper.collection_scraper import (
    CollectionInfo, QueuedProduct, Storefront, assign_modes,
)
from database.models import MODE_MULTI, MODE_SINGLE
from notifications.notifier import (
    EmailNotifier, NotificationManager, NotificationProvider, PushbulletNotifier,
    SMSNotifier, WebhookNotifier, to_jsonable,
)
from utils.crypto import decrypt_str, encrypt_str
from utils.export import DataExporter
from utils.timezone_utils import TimezoneManager

WORK = tempfile.mkdtemp(prefix='frn_tests_')
FAILURES = []


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def check(name, fn):
    try:
        fn()
        print(f"  PASS  {name}")
    except Exception as e:
        print(f"  FAIL  {name}: {type(e).__name__}: {e}")
        FAILURES.append(name)


def section(title):
    print(f"\n== {title} ==")


# ---------------------------------------------------------------------------
section("credential encryption")


def crypto_round_trip():
    assert decrypt_str(encrypt_str("hunter2")) == "hunter2"


def crypto_legacy_plaintext():
    # Rows written before encryption existed must still be readable
    assert decrypt_str('{"smtp_server": "x"}') == '{"smtp_server": "x"}'


def crypto_key_is_persistent():
    # A key generated per process could never decrypt a previous run's output
    blob = NotificationManager().encrypt_config({'api_key': 'abc'})
    assert NotificationManager().decrypt_config(blob) == {'api_key': 'abc'}


check("encrypt/decrypt round-trips", crypto_round_trip)
check("legacy plaintext still readable", crypto_legacy_plaintext)
check("key persists across manager instances", crypto_key_is_persistent)

# ---------------------------------------------------------------------------
section("webhook payload")


def webhook_serialisable():
    product = {'name': 'Test', 'item_number': '1', 'sale_price': 1.0,
               'posted_at': utcnow(), 'minutes_live': 5}
    payload = to_jsonable({'product': product,
                           'previous_products': [dict(product)],
                           'timestamp': product['posted_at']})
    json.dumps(payload)  # would raise TypeError before the fix
    assert isinstance(payload['product']['posted_at'], str)


def webhook_fails_gracefully():
    notifier = WebhookNotifier(url='http://127.0.0.1:9/none', method='POST')
    assert notifier.send({'name': 'x', 'item_number': '1', 'sale_price': 1.0,
                          'posted_at': utcnow()}) is False


check("datetime payload is JSON-serialisable", webhook_serialisable)
check("unreachable webhook returns False, not TypeError", webhook_fails_gracefully)

# ---------------------------------------------------------------------------
section("database")

db = DatabaseManager(db_path=os.path.join(WORK, 'test.db'),
                     backup_dir=os.path.join(WORK, 'backups'))


def db_basics():
    uid = db.get_or_create_default_user()
    assert uid == db.get_or_create_default_user(), "user should be reused"
    db.ensure_user_settings(uid)
    p1 = db.add_product('111', 'Track Saw TS 60', 677.55, 900.0, 'desc', None, None)
    db.mark_product_replaced(p1.id)
    db.add_product('222', 'Cordless Drill TXS', 199.0, 249.0, 'desc', None, None)
    assert len(db.get_product_history()) == 2
    assert db.get_latest_product().item_number == '222'


def db_filters():
    uid = db.get_or_create_default_user()
    product = db.get_latest_product()      # Cordless Drill TXS, item 222

    assert db.should_notify_user(uid, product) is True, "no filters = notify"

    db.add_to_exclusions(uid, keyword='cordless')
    assert db.should_notify_user(uid, product) is False, "keyword exclusion"

    with db.get_session() as s:
        s.query(Exclusion).delete()

    db.add_to_exclusions(uid, item_number='222')
    assert db.should_notify_user(uid, product) is False, "item-number exclusion"

    with db.get_session() as s:
        s.query(Exclusion).delete()

    db.add_to_watchlist(uid, 'kapex')
    assert db.should_notify_user(uid, product) is False, "non-matching watchlist"
    db.add_to_watchlist(uid, 'drill')
    assert db.should_notify_user(uid, product) is True, "matching watchlist"


def db_backup_restore():
    path = db.backup_database()
    assert os.path.exists(path)

    db.add_product('333', 'Domino Joiner DF 500', 999.0, None, None, None, None)
    assert len(db.get_product_history()) == 3

    db.restore_from_backup(path)
    assert len(db.get_product_history()) == 2, "restore should roll back"

    # The rebuilt engine must still allow access from another thread
    result = {}

    def worker():
        try:
            result['count'] = len(db.get_product_history())
        except Exception as e:
            result['error'] = e

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert 'error' not in result, f"cross-thread access broke: {result.get('error')}"
    assert result['count'] == 2


check("add / replace / history / latest", db_basics)
check("watchlist and exclusion filtering", db_filters)
check("backup, restore, cross-thread access after restore", db_backup_restore)

# ---------------------------------------------------------------------------
section("SKU analytics")

analytics = SkuAnalytics(db)


def analytics_rollup():
    db.add_product('111', 'Track Saw TS 60', 599.00, 900.0, 'd', None, None)
    db.add_product('111', 'Track Saw TS 60', 649.00, 900.0, 'd', None, None)

    summaries = {s.item_number: s for s in analytics.get_sku_summaries()}
    ts = summaries['111']
    assert ts.times_listed == 3, ts.times_listed
    assert ts.lowest_price == 599.00
    assert ts.highest_price == 677.55
    assert ts.first_price == 677.55 and ts.last_price == 649.00
    assert ts.price_trend == 'down', ts.price_trend
    assert ts.is_repeat is True
    assert summaries['222'].is_repeat is False


def analytics_discounts():
    assert discount_pct(100.0, 75.0) == 25.0
    assert discount_pct(None, 75.0) is None
    assert discount_pct(0, 75.0) is None
    # A sale price above MSRP is bad data, not a negative discount
    assert discount_pct(100.0, 125.0) is None


def analytics_verdicts():
    # History for 111 is 677.55, 599.00, 649.00
    best = analytics.evaluate_price('111', 550.00)
    assert best.is_best_ever and best.is_new_low and not best.is_first_sighting
    assert best.previous_best == 599.00
    assert best.times_listed_before == 3
    assert 'Best price' in best.headline

    high = analytics.evaluate_price('111', 700.00)
    assert high.is_best_ever is False and high.is_new_low is False
    assert abs(high.delta_vs_best - 101.0) < 0.01, high.delta_vs_best
    assert 'above the best seen' in high.headline

    unseen = analytics.evaluate_price('nope-404', 100.0, regular_price=200.0)
    assert unseen.is_first_sighting and unseen.is_best_ever
    assert unseen.is_new_low is False, "nothing to be a new low against"
    assert 'First time seen' in unseen.headline
    assert unseen.previous_range_text is None

    # A stored listing must not be compared against itself
    latest = db.get_latest_product()
    own = analytics.evaluate_price(latest.item_number, latest.sale_price,
                                   exclude_product_id=latest.id)
    assert own.times_listed_before == 2, own.times_listed_before


def analytics_matching_the_best_price_is_not_a_new_low():
    """
    An item back at the price it last sold for is not "the best price seen,
    previous best <the same number>" - which is what the old wording produced,
    a claim that contradicted itself in the same sentence.
    """
    tie = analytics.evaluate_price('111', 599.00)
    assert tie.is_best_ever is True, "nothing has been cheaper"
    assert tie.is_new_low is False, "but it is not cheaper either"
    assert 'Matches its best price' in tie.headline, tie.headline
    assert 'previous best' not in tie.headline, tie.headline
    assert 'Matches best' in tie.short_label

    # Sub-cent noise is the same price, not a saving
    noise = analytics.evaluate_price('111', 599.00 - 0.001)
    assert noise.is_new_low is False, noise.headline

    # ...and a real cut of a few cents still counts
    cut = analytics.evaluate_price('111', 598.90)
    assert cut.is_new_low is True
    assert '%' not in cut.headline, \
        f"a 0.02% cut should not be reported as a percentage: {cut.headline}"


def analytics_price_range_text():
    """No item was ever priced 'between $X and $X'."""
    single = analytics.evaluate_price('222', 199.0)
    assert single.times_listed_before == 1
    assert single.previous_range_text == "Previously seen once, at $199.00", \
        single.previous_range_text

    spread = analytics.evaluate_price('111', 600.0)
    assert spread.previous_range_text == \
        "Previously seen 3 times, between $599.00 and $677.55", \
        spread.previous_range_text

    # Same item, same price, twice over: a range would be two identical numbers
    flat = DatabaseManager(db_path=os.path.join(WORK, 'flat.db'),
                           backup_dir=os.path.join(WORK, 'b3'))
    for _ in range(2):
        flat.add_product('444', 'Dust Extractor CT 26', 719.20, 899.0, None, None, None)
    verdict = SkuAnalytics(flat).evaluate_price('444', 719.20)
    assert verdict.previous_range_text == \
        "Previously seen 2 times, always at $719.20", verdict.previous_range_text


def analytics_catalog():
    c = analytics.get_catalog_summary()
    assert c['total_listings'] == 4, c['total_listings']
    assert c['total_skus'] == 2, c['total_skus']
    assert c['repeat_skus'] == 1
    assert c['median_discount_pct'] is not None
    assert sum(c['price_bands'].values()) == c['total_listings']
    assert len(analytics.get_listings_by_month()) >= 1
    assert len(analytics.get_listings_for_sku('111')) == 3


def analytics_export():
    out = os.path.join(WORK, 'sku.csv')
    analytics.export_sku_history(out)
    text = open(out, encoding='utf-8').read()
    assert 'Item Number' in text and '111' in text


def analytics_empty_db():
    fresh = DatabaseManager(db_path=os.path.join(WORK, 'empty.db'),
                            backup_dir=os.path.join(WORK, 'b2'))
    a = SkuAnalytics(fresh)
    c = a.get_catalog_summary()
    assert c['total_listings'] == 0 and c['total_skus'] == 0
    assert a.get_sku_summaries() == []
    assert a.evaluate_price('x', 1.0).is_first_sighting is True


check("per-SKU rollup (count, low/high, trend)", analytics_rollup)
check("discount percentage maths", analytics_discounts)
check("price verdicts", analytics_verdicts)
check("matching the best price is not a new low",
      analytics_matching_the_best_price_is_not_a_new_low)
check("price history described without empty ranges", analytics_price_range_text)
check("catalog summary and monthly cadence", analytics_catalog)
check("SKU history CSV export", analytics_export)
check("analytics on an empty database", analytics_empty_db)

# ---------------------------------------------------------------------------
section("look-ahead queue")

queue_db = DatabaseManager(db_path=os.path.join(WORK, 'queue.db'),
                           backup_dir=os.path.join(WORK, 'qb'))
queue_analytics = SkuAnalytics(queue_db)


def make_queue(*specs):
    """Build a queue from (position, sku, name, price) tuples."""
    return [QueuedProduct(position=p, item_number=s, name=n, handle=f'h-{s}',
                          sale_price=price, regular_price=price * 1.25,
                          product_type='Drill', image_url=None)
            for p, s, n, price in specs]


def queue_placeholder_detection():
    """The store's sold-out filler must not be listed as an upcoming tool."""
    filler = QueuedProduct(position=3, item_number='999999',
                           name='Wow, that went fast!!!', handle='wow-that-went-fast',
                           sale_price=0.01, regular_price=0.0,
                           product_type='Placeholder', image_url=None)
    real = make_queue((1, '200', 'Real Tool', 400.0))[0]
    assert filler.is_placeholder is True
    assert real.is_placeholder is False
    assert real.is_live is False
    assert make_queue((0, '100', 'Live', 500.0))[0].is_live is True
    assert abs(real.discount_pct - 20.0) < 0.01


def queue_sync_and_skips():
    queue_db.sync_queue(make_queue(
        (0, '100', 'Live Item', 500.0), (1, '200', 'Next Up', 400.0),
        (2, '300', 'After That', 300.0), (3, '400', 'Later', 200.0)))
    assert len(queue_db.get_queue()) == 4

    assert queue_db.mark_queue_item_live('200') == 1

    # 300 vanishes without ever going live; 100 leaves from the live slot
    result = queue_db.sync_queue(make_queue(
        (0, '200', 'Next Up', 400.0), (1, '400', 'Later', 200.0),
        (2, '500', 'Brand New', 150.0)))
    assert '500' in result['added']
    assert '300' in result['removed']
    # An item leaving from position 0 ran its course - counting it as skipped
    # would badly inflate the skip rate
    assert '100' not in result['removed']

    outcomes = queue_db.get_queue_outcomes()
    skipped = {o.item_number for o in outcomes if o.was_skipped}
    live = {o.item_number for o in outcomes if o.went_live_at}
    assert '300' in skipped and '100' not in skipped
    assert {'100', '200'} <= live


def queue_relisting():
    # A skipped item returning becomes pending again with a fresh price
    queue_db.sync_queue(make_queue(
        (0, '200', 'Next Up', 400.0), (1, '300', 'After That', 295.0)))
    pending = {r.item_number: r for r in queue_db.get_queue()}
    assert '300' in pending
    assert pending['300'].removed_at is None
    assert pending['300'].sale_price == 295.0


def queue_best_position():
    queue_db.sync_queue(make_queue(
        (0, '200', 'Next Up', 400.0), (1, '300', 'After That', 295.0),
        (2, '700', 'Seven', 88.0)))
    queue_db.sync_queue(make_queue(
        (0, '200', 'Next Up', 400.0), (1, '700', 'Seven', 88.0),
        (2, '300', 'After That', 295.0)))
    rows = {r.item_number: r for r in queue_db.get_queue()}
    assert rows['300'].best_position == 1, rows['300'].best_position
    assert rows['300'].position == 2
    assert rows['300'].times_observed >= 3


def queue_wait_is_elapsed_time():
    """
    The queue tab must report waiting time, not sighting count.

    times_observed increments once per refresh, so it tracked how often the
    app had looked rather than anything about the item; refreshing twice in a
    row would 'age' a queued item that had not moved.
    """
    # Its own database: syncing a short queue against the shared fixture would
    # mark the items missing from it as skipped.
    from database.models import QueueObservation
    wait_db = DatabaseManager(db_path=os.path.join(WORK, 'q3.db'),
                              backup_dir=os.path.join(WORK, 'qb3'))
    pending = make_queue((0, '200', 'Next Up', 400.0), (1, '900', 'Fresh Arrival', 120.0))

    wait_db.sync_queue(pending)
    row = {r.item_number: r for r in wait_db.get_queue()}['900']
    assert row.minutes_in_queue == 0, row.minutes_in_queue

    before = row.times_observed
    wait_db.sync_queue(pending)
    row = {r.item_number: r for r in wait_db.get_queue()}['900']
    assert row.times_observed > before, "refresh should still be counted internally"
    assert row.minutes_in_queue == 0, "an extra refresh must not age the item"

    # A row that has been queued a while reports that wait...
    with wait_db.get_session() as session:
        stored = (session.query(QueueObservation)
                  .filter_by(item_number='900').first())
        stored.first_seen_at = utcnow() - timedelta(hours=3)

    aged = {r.item_number: r for r in wait_db.get_queue()}['900']
    assert 178 <= aged.minutes_in_queue <= 182, aged.minutes_in_queue

    # ...and a resolved row stops the clock at its outcome rather than growing
    # forever after it has left the queue.
    wait_db.mark_queue_item_live('900')
    done = next(o for o in wait_db.get_queue_outcomes() if o.item_number == '900')
    assert 178 <= done.minutes_in_queue <= 182, done.minutes_in_queue


def queue_empty_fetch_is_ignored():
    """A failed fetch must not look like 'everything was skipped'."""
    before = len(queue_db.get_queue())
    outcomes_before = len(queue_db.get_queue_outcomes())
    queue_db.sync_queue([])
    assert len(queue_db.get_queue()) == before, "empty fetch emptied the queue"
    assert len(queue_db.get_queue_outcomes()) == outcomes_before, "false skips recorded"


def queue_accuracy_stats():
    stats = queue_analytics.get_queue_accuracy()
    assert stats['went_live'] >= 2
    assert stats['skipped'] >= 1
    assert 0 <= stats['skip_rate_pct'] <= 100
    text = queue_analytics.describe_queue_confidence()
    assert isinstance(text, str) and text

    # A database with no outcomes must say so rather than imply confidence
    fresh = DatabaseManager(db_path=os.path.join(WORK, 'q2.db'),
                            backup_dir=os.path.join(WORK, 'qb2'))
    blank = SkuAnalytics(fresh).get_queue_accuracy()
    assert blank['resolved'] == 0 and blank['has_data'] is False
    assert 'No queue outcomes' in SkuAnalytics(fresh).describe_queue_confidence()


def queue_price_annotation():
    queue_db.add_product('300', 'After That', 350.0, 400.0, None, None, None)
    annotated = queue_analytics.annotate_queue(queue_db.get_queue())
    verdict = next(e['verdict'] for e in annotated if e['row'].item_number == '300')
    assert verdict.is_best_ever, "queued at 295 against 350 history should be a new best"
    assert all(e['discount_pct'] is not None for e in annotated)


def collection_sync():
    usual = CollectionInfo(handle='oneanddone', title='OneAndDone', products_count=7)
    new = queue_db.sync_collections([usual])
    assert [c.handle for c in new] == ['oneanddone']

    # Re-syncing the same collection is not a new sighting
    assert queue_db.sync_collections([usual]) == []

    # A special sale appearing alongside the usual collection
    special = CollectionInfo(handle='blackfriday', title='Black Friday', products_count=3)
    new = queue_db.sync_collections([usual, special])
    assert [c.handle for c in new] == ['blackfriday']

    # ...and going away again marks it inactive without deleting the record
    queue_db.sync_collections([usual])
    sightings = {s.handle: s for s in queue_db.get_collection_sightings()}
    assert sightings['blackfriday'].is_active is False
    assert sightings['oneanddone'].is_active is True
    assert len(queue_db.get_collection_sightings(active_only=True)) == 1


check("placeholder and live-position detection", queue_placeholder_detection)
check("queue sync records skips and completions separately", queue_sync_and_skips)
check("re-listed items become pending again", queue_relisting)
check("best position and observation count tracked", queue_best_position)
check("queue wait is elapsed time, not a refresh count", queue_wait_is_elapsed_time)
check("failed fetch does not wipe the queue", queue_empty_fetch_is_ignored)
check("queue accuracy statistics", queue_accuracy_stats)
check("queued items annotated with price verdicts", queue_price_annotation)


# ---------------------------------------------------------------------------
section("next-item prediction")

from types import SimpleNamespace
from analytics.sku_analytics import Listing


def _forecaster(accuracy, durations, multi_durations=()):
    """SkuAnalytics with its two data sources replaced by fixed records."""
    fc = SkuAnalytics(queue_db)
    now = utcnow()
    listings = [Listing(product_id=i, item_number=str(i), name='x', sale_price=1.0,
                        regular_price=None, posted_at=now, replaced_at=now,
                        minutes_live=m)
                for i, m in enumerate(durations)]
    listings += [Listing(product_id=900 + i, item_number='m', name='sale', sale_price=1.0,
                         regular_price=None, posted_at=now, replaced_at=now,
                         minutes_live=m, is_multi_item=True)
                 for i, m in enumerate(multi_durations)]
    # Still live: its duration is not known yet and must not be counted
    listings.append(Listing(product_id=999, item_number='live', name='live',
                            sale_price=1.0, regular_price=None, posted_at=now,
                            replaced_at=None, minutes_live=5000))
    fc.get_queue_accuracy = lambda handle='oneanddone': accuracy
    fc._all_listings = lambda: listings
    return fc


_ROWS = [SimpleNamespace(position=p, item_number=s, name=n, sale_price=1.0)
         for p, s, n in [(2, '300', 'Third'), (0, '100', 'Live'), (1, '200', 'Second')]]
_GOOD_RECORD = {'went_live': 59, 'front_of_queue_hits': 52,
                'front_of_queue_pct': 52 / 59 * 100, 'has_data': True}


def next_item_is_the_front_of_the_queue():
    fc = _forecaster(_GOOD_RECORD, [])
    result = fc.predict_next(_ROWS)
    assert result['next'].item_number == '200', "position 0 is already live"
    assert result['runner_up'].item_number == '300'
    assert round(result['hit_rate_pct']) == 88
    assert (result['hits'], result['sample']) == (52, 59)
    assert result['minutes_live_so_far'] is None, "no live product, no timing"


def a_thin_record_claims_no_hit_rate():
    thin = {'went_live': 2, 'front_of_queue_hits': 2,
            'front_of_queue_pct': 100.0, 'has_data': False}
    result = _forecaster(thin, []).predict_next(_ROWS)
    assert result['hit_rate_pct'] is None, "2 of 2 is not '100% reliable'"


def timing_comes_from_listings_that_lasted_as_long():
    """
    Remaining time is measured over the listings that had already survived
    as long as the current one - not the overall average, which a listing
    already up for hours has plainly beaten.
    """
    fc = _forecaster(_GOOD_RECORD, [10, 20, 30, 60, 90, 120, 240],
                     multi_durations=[4000, 5000, 6000])
    live = SimpleNamespace(posted_at=utcnow() - timedelta(minutes=25), replaced_at=None)
    result = fc.predict_next(_ROWS, live, now=live.posted_at + timedelta(minutes=25))

    assert result['minutes_live_so_far'] == 25
    # Survivors 30, 60, 90, 120, 240 -> another 5, 35, 65, 95, 215; sale
    # listings and the still-live row are left out
    assert result['outlasted_by'] == 5, result['outlasted_by']
    assert (result['remaining_low'], result['remaining_mid'],
            result['remaining_high']) == (35, 65, 95), result


def no_estimate_beyond_the_record():
    fc = _forecaster(_GOOD_RECORD, [10, 20, 30, 60, 90, 120, 240])
    live = SimpleNamespace(posted_at=utcnow() - timedelta(minutes=200), replaced_at=None)
    result = fc.predict_next(_ROWS, live, now=live.posted_at + timedelta(minutes=200))
    assert result['outlasted_by'] == 1
    assert result['remaining_mid'] is None, "one survivor is not a statistic"


def nothing_queued_means_no_prediction():
    fc = _forecaster(_GOOD_RECORD, [])
    assert fc.predict_next([]) is None
    assert fc.predict_next([_ROWS[1]]) is None, "only the live item"


check("the next item is the front of the queue", next_item_is_the_front_of_the_queue)
check("a thin record claims no hit rate", a_thin_record_claims_no_hit_rate)
check("timing comes from listings that lasted as long",
      timing_comes_from_listings_that_lasted_as_long)
check("no estimate beyond the record", no_estimate_beyond_the_record)
check("nothing queued means no prediction", nothing_queued_means_no_prediction)
check("collection sightings and special-sale detection", collection_sync)

# ---------------------------------------------------------------------------
section("product categories")


def types_inferred_from_title():
    # The front-page scraper never sees the store's product_type, so the title
    # has to carry every category on its own.
    cases = {
        'Router OF 2200 EB-F-Plus': 'Router',
        'MFK 700 EQ/B-Plus': 'Router',            # router without the word
        'Multi-Mode Sander ROTEX RO 150 FEQ-Plus': 'Sander',
        'ETSC 2 150-Basic': 'Sander',             # cordless naming
        'Random Orbital Sander ETS 125 REQ-Plus': 'Sander',
        'Domino Joiner DF 500 Q Set': 'Joiner',
        'Track Saw TS 75 EQ-F-Plus': 'Saw',
        'Cordless Drill T 18+3': 'Drill',
        'Dust Extractor CT 26 E': 'Vac',
        'PLANEX LHS 225 Drywall Sander': 'Planex',
    }
    for title, expected in cases.items():
        got = type_of(None, None, title)
        assert got == expected, f"{title!r} -> {got!r}, expected {expected!r}"


def types_normalize_store_values():
    # The store pluralises unpredictably and files a PLANEX as a sander
    assert normalize_type('Sanders', 'ETS 125 REQ') == 'Sander'
    assert normalize_type('Sander', 'PLANEX LHS 225') == 'Planex'
    assert normalize_type('Work Table', 'MFT/3') == 'MFT'
    # Placeholder values must fall through to the title, not be stored as-is
    assert normalize_type('Unknown', 'Track Saw TS 75') == 'Saw'
    assert normalize_type(None, 'Track Saw TS 75') == 'Saw'
    assert normalize_type('', None) == 'Unknown'


def types_persisted_and_backfilled():
    tdb = DatabaseManager(db_path=os.path.join(WORK, 'types.db'),
                          backup_dir=os.path.join(WORK, 'tb'))
    # Store value wins when there is one, and is normalised on the way in
    p = tdb.add_product('576028', 'ROTEX RO 150 FEQ-Plus', 592.75, 740.94,
                        None, None, None, product_type='Sanders')
    assert p.product_type == 'Sander', p.product_type

    # No store value: inferred from the title
    p2 = tdb.add_product('574689', 'Router OF 2200 EB-F-Plus', 771.75, 1029.0,
                         None, None, None)
    assert p2.product_type == 'Router', p2.product_type

    # A row written before the column existed must not be left out of rollups
    with tdb.get_session() as s:
        from database.models import Product as P
        s.query(P).filter(P.item_number == '574689').update({'product_type': None})
    assert tdb.backfill_product_types() == 1
    assert tdb.backfill_product_types() == 0, "backfill must be idempotent"

    counts = dict(SkuAnalytics(tdb).get_catalog_summary()['type_counts'])
    assert counts.get('Router') == 1 and counts.get('Sander') == 1, counts


check("categories inferred from product titles", types_inferred_from_title)
check("store product types normalised", types_normalize_store_values)
check("categories persisted, backfilled, and rolled up", types_persisted_and_backfilled)

# ---------------------------------------------------------------------------
section("printed catalog index")


class FakePage:
    """Stands in for a pdfplumber page - extract_page only needs the words."""

    def __init__(self, words):
        self._words = words

    def extract_words(self, **kwargs):
        return self._words


def word(text, x0, top, width=28, height=9):
    return {'text': text, 'x0': x0, 'x1': x0 + width,
            'top': top, 'bottom': top + height}


def catalog_card_pairing():
    """
    A product card prints the order number above its price.

    Measured on the 2026 pages: the USD price sits ~9pt right and ~10pt below
    the order number, with the CAD price directly beneath it.  Reading the
    page line by line would pair the CAD figure - or the neighbouring card's -
    with the tool.
    """
    words = [
        word('577079', 627, 587, width=25),
        word('USD', 605, 597, width=28), word('$269.00', 635, 597),
        word('CAD', 605, 606, width=28), word('$375.00', 635, 606),
    ]
    items = {i.item_number: i for i in extract_page(FakePage(words), page_number=39)}
    got = items['577079']
    assert got.msrp_usd == 269.00, got.msrp_usd
    assert got.msrp_cad == 375.00, got.msrp_cad
    assert got.confidence == 'proximity', got.confidence
    assert got.page == 39


def catalog_table_row_wins():
    """The accessory tables print one item per row - unambiguous."""
    words = [
        word('P150', 100, 300), word('10', 150, 300), word('Units', 175, 300),
        word('201261', 230, 300), word('$11.00', 300, 300), word('$15.00', 360, 300),
    ]
    items = {i.item_number: i for i in extract_page(FakePage(words))}
    got = items['201261']
    # Unlabelled columns run USD then CAD
    assert got.msrp_usd == 11.00 and got.msrp_cad == 15.00, (got.msrp_usd, got.msrp_cad)
    assert got.confidence == 'table'


def catalog_never_records_cad_as_usd():
    """A Canadian price must never be stored as the US list price."""
    words = [
        word('577385', 627, 400, width=25),
        word('CAD', 605, 410, width=28), word('$97.00', 635, 410),
    ]
    items = {i.item_number: i for i in extract_page(FakePage(words))}
    got = items['577385']
    assert got.msrp_usd is None, got.msrp_usd
    assert got.msrp_cad == 97.00
    # Still indexed: membership is useful even without a price
    assert got.confidence is None


def catalog_distant_price_not_claimed():
    """A price belonging to another card must not be stolen."""
    words = [
        word('576954', 627, 100, width=25),
        word('USD', 605, 300, width=28), word('$99.00', 635, 300),
    ]
    items = {i.item_number: i for i in extract_page(FakePage(words))}
    assert items['576954'].msrp_usd is None, "claimed a price 200pt away"


def catalog_old_format_index_rows():
    """
    The pre-2016 editions print no currency symbol.

    Their index rows read 'order-no  description  page-ref  price', two items
    to a printed row, so each order number takes the first bare decimal after
    it.  The page reference is a plain integer and must not be read as money.
    """
    words = [
        word('490161', 100, 300), word('Flat', 150, 300), word('sanding', 180, 300),
        word('pad', 225, 300), word('LS130', 250, 300),
        word('153', 300, 300), word('30.00', 330, 300),
        word('492128', 390, 300), word('Polishing', 440, 300), word('pad', 490, 300),
        word('159', 530, 300), word('28.00', 560, 300),
    ]
    items = {i.item_number: i for i in
             extract_page(FakePage(words), page_number=196, bare_prices=True)}

    assert items['490161'].msrp_usd == 30.00, items['490161'].msrp_usd
    assert items['492128'].msrp_usd == 28.00, items['492128'].msrp_usd
    assert items['490161'].confidence == 'table'

    # The same words must NOT yield prices when read as a modern page: with no
    # '$' anywhere there is nothing to pair, and guessing would be worse.
    modern = {i.item_number: i for i in extract_page(FakePage(words))}
    assert modern['490161'].msrp_usd is None


def catalog_bare_prices_not_used_on_modern_pages():
    """
    Weights and dimensions must never be read as prices.

    "1.76 lbs" is a bare decimal in exactly the shape of an old-format price,
    which is why the old format is detected per catalog rather than per page.
    """
    words = [
        word('577079', 627, 587, width=25),
        word('USD', 605, 597, width=28), word('$269.00', 635, 597),
        word('1.76', 700, 597), word('lbs', 730, 597),
    ]
    items = {i.item_number: i for i in extract_page(FakePage(words))}
    assert items['577079'].msrp_usd == 269.00, items['577079'].msrp_usd


def catalog_split_order_numbers():
    """
    Some editions print the order number in two groups: '561 556'.

    Confirmed on page 40 of the 2013 catalog, where '$585.00' sits at y=168.6
    and the halves '561'/'556' at y=177.4 with a 1.5pt gap between them.  Read
    as separate tokens the item is invisible, which is why that whole edition
    yielded almost nothing before.
    """
    words = [
        word('$585.00', 130.1, 168.6, width=27.8),
        word('561', 133.9, 177.4, width=9.4), word('556', 144.7, 177.4, width=9.4),
    ]
    items = {i.item_number: i for i in extract_page(FakePage(words), page_number=40)}
    assert '561556' in items, list(items)
    assert items['561556'].msrp_usd == 585.00, items['561556'].msrp_usd

    # A normal gap between two unrelated numbers must NOT be glued together
    spaced = [word('230', 100, 300, width=9.4), word('240', 130, 300, width=9.4)]
    assert not any(i.item_number == '230240'
                   for i in extract_page(FakePage(spaced)))


def catalog_doubled_text_rejected():
    """
    Pages that render their text twice turn '1002' into '110022'.

    The result is six digits and would otherwise be indexed as an order
    number - 149 of them were.
    """
    words = [
        word('110022', 100, 300), word('$40.00', 200, 300),
        word('576028', 100, 400), word('$50.00', 200, 400),
    ]
    items = {i.item_number: i for i in extract_page(FakePage(words))}
    assert '110022' not in items, "indexed a doubled-text artifact"
    assert '576028' in items, "rejected a legitimate order number"


def catalog_repeated_sku_keeps_priced_occurrence():
    """
    An order number is often printed twice on one page.

    Real case (577619, page 94 of the 2026 catalog): once as a bare mention
    with nothing near it, and again in the spec block with "USD $555.00" 13pt
    below.  Recording the first occurrence and skipping the rest threw the
    price away whenever the bare mention came first.
    """
    words = [
        word('577619', 863, 349, width=23),          # bare mention, no price
        word('577619', 356, 545, width=27),          # spec block
        word('USD', 336, 558, width=20), word('$555.00', 390, 558),
        word('CAD', 336, 567, width=20), word('$769.00', 390, 567),
    ]
    items = {i.item_number: i for i in extract_page(FakePage(words), page_number=94)}
    assert items['577619'].msrp_usd == 555.00, items['577619'].msrp_usd
    assert items['577619'].msrp_cad == 769.00, items['577619'].msrp_cad

    # A priced occurrence must not be downgraded by a later bare one
    reordered = [words[1], words[2], words[3], words[4], words[5], words[0]]
    again = {i.item_number: i for i in extract_page(FakePage(reordered))}
    assert again['577619'].msrp_usd == 555.00, again['577619'].msrp_usd


def catalog_several_items_per_row():
    """
    A printed row can carry a whole product family side by side.

    The 2024 pages set it out as '576451 $329.00   576991 $555.00' with no
    currency labels at all.  Requiring one order number per row skipped those
    rows, and letting the first number take every price on the row would give
    the Basic model the Set price.
    """
    words = [
        word('576451', 100, 300, width=27), word('$329.00', 140, 300),
        word('576991', 250, 300, width=27), word('$555.00', 290, 300),
    ]
    items = {i.item_number: i for i in extract_page(FakePage(words))}

    assert items['576451'].msrp_usd == 329.00, items['576451'].msrp_usd
    assert items['576991'].msrp_usd == 555.00, items['576991'].msrp_usd
    assert items['576451'].confidence == 'table'
    # The cheaper item must not have swallowed the row's second price as CAD
    assert items['576451'].msrp_cad is None, items['576451'].msrp_cad


def catalog_storage_and_lookup():
    cdb = DatabaseManager(db_path=os.path.join(WORK, 'cat.db'),
                          backup_dir=os.path.join(WORK, 'cb'))

    def entry(sku, usd, conf='table'):
        return CatalogItem(item_number=sku, msrp_usd=usd, confidence=conf)

    cdb.replace_catalog_year('2019', [entry('576028', 700.0), entry('999111', 50.0)])
    cdb.replace_catalog_year('2026', [entry('576028', 795.0)])

    history = cdb.get_catalog_history('576028')
    assert [e.catalog_year for e in history] == ['2019', '2026']

    # Newest priced edition by default
    assert cdb.get_catalog_entry('576028').msrp_usd == 795.0
    assert cdb.get_catalog_entry('576028', '2019').msrp_usd == 700.0
    assert cdb.get_catalog_entry('000000') is None

    # Re-running an edition replaces it rather than duplicating
    cdb.replace_catalog_year('2026', [entry('576028', 810.0)])
    assert len(cdb.get_catalog_history('576028')) == 2
    assert cdb.get_catalog_entry('576028').msrp_usd == 810.0

    years = dict((y, (t, p)) for y, t, p in cdb.get_catalog_years())
    assert years['2019'] == (2, 2) and years['2026'] == (1, 1), years

    analytics = SkuAnalytics(cdb)

    # Still printed in the newest edition
    assert 'current 2026 catalog' in analytics.describe_catalog_presence('576028')

    # Printed once, then dropped - the wording has to distinguish this from
    # never having been catalogued at all
    dropped = analytics.describe_catalog_presence('999111')
    assert 'dropped from the current one' in dropped, dropped
    assert '2019' in dropped

    # Never in any edition at all
    absent = analytics.describe_catalog_presence('123456')
    assert 'discontinued' in absent, absent

    verdict = analytics.compare_to_catalog('576028', 648.0)
    assert verdict['catalog_msrp'] == 810.0
    assert abs(verdict['discount_pct'] - 20.0) < 0.01, verdict['discount_pct']

    # Silent on a database with no catalog index, rather than claiming absence
    blank = SkuAnalytics(DatabaseManager(db_path=os.path.join(WORK, 'cat2.db'),
                                         backup_dir=os.path.join(WORK, 'cb2')))
    assert blank.describe_catalog_presence('576028') is None
    assert blank.compare_to_catalog('576028', 648.0) is None


def catalog_year_naming():
    # The 2011-12 edition spans two years; its filename must not become '2011'
    assert year_from_filename('2011-12-US.pdf') == '2011-12'
    assert year_from_filename('2026-US.pdf') == '2026'
    assert year_from_filename(r'c:\x\2019-US.pdf') == '2019'


check("catalog edition naming", catalog_year_naming)
check("product card SKU/price pairing", catalog_card_pairing)
check("tabular rows beat proximity", catalog_table_row_wins)
check("CAD price never stored as USD", catalog_never_records_cad_as_usd)
check("distant price not mis-assigned", catalog_distant_price_not_claimed)
check("split order numbers ('561 556')", catalog_split_order_numbers)
check("doubled-text artifacts rejected", catalog_doubled_text_rejected)
check("repeated SKU keeps the priced occurrence",
      catalog_repeated_sku_keeps_priced_occurrence)
check("several items on one printed row", catalog_several_items_per_row)
check("pre-2016 index rows (no currency symbol)", catalog_old_format_index_rows)
check("weights never read as prices", catalog_bare_prices_not_used_on_modern_pages)
def catalog_leads_with_newest_edition():
    """
    An older year's price must never be presented as if it were current.

    Real case (SKU 577619): indexed in 2024, 2025 and 2026, but only the 2025
    edition yielded a price.  Leading with "2025 catalog list price" while
    also saying the item is in the 2026 catalog reads as though $555 were the
    current list price.  The newest edition has to come first and say plainly
    that it carried no price.
    """
    ndb = DatabaseManager(db_path=os.path.join(WORK, 'newest.db'),
                          backup_dir=os.path.join(WORK, 'nb'))

    def entry(sku, usd, conf='proximity'):
        return CatalogItem(item_number=sku, msrp_usd=usd, confidence=conf)

    ndb.replace_catalog_year('2024', [entry('577619', None)])
    ndb.replace_catalog_year('2025', [entry('577619', 555.0)])
    ndb.replace_catalog_year('2026', [entry('577619', None)])

    history = ndb.get_catalog_history('577619')
    newest = list(reversed(history))[0]
    assert newest.catalog_year == '2026', newest.catalog_year
    assert newest.msrp_usd is None

    # The comparison still has to use the most recent edition that has a price
    verdict = SkuAnalytics(ndb).compare_to_catalog('577619', 444.0)
    assert verdict['catalog_year'] == '2025', verdict['catalog_year']
    assert abs(verdict['discount_pct'] - 20.0) < 0.01, verdict['discount_pct']

    # ...and when the newest edition does carry a price, that one wins
    ndb.replace_catalog_year('2026', [entry('577619', 575.0)])
    verdict = SkuAnalytics(ndb).compare_to_catalog('577619', 444.0)
    assert verdict['catalog_year'] == '2026', verdict['catalog_year']
    assert verdict['catalog_msrp'] == 575.0


check("catalog storage, lookup, and verdicts", catalog_storage_and_lookup)
check("newest edition leads, older price never passed off as current",
      catalog_leads_with_newest_edition)

# ---------------------------------------------------------------------------
section("statistics, export")


def stats():
    assert 'total_products' in db.get_statistics()


def exports():
    exporter = DataExporter(export_dir=os.path.join(WORK, 'exports'))
    products = db.get_product_history()
    for fn in (exporter.export_to_csv, exporter.export_to_json, exporter.export_to_excel):
        assert os.path.getsize(fn(products)) > 0


check("statistics", stats)
check("CSV / JSON / Excel export", exports)

# ---------------------------------------------------------------------------
section("date and duration formatting")


def dates_include_year():
    sample = datetime(2025, 10, 17, 20, 13, 47)
    for mode in ('local', 'utc'):
        m = TimezoneManager(mode)
        for fn in (m.to_display, m.to_short_display, m.to_date_display):
            out = fn(sample)
            assert re.search(r'\b20\d{2}\b', out), f"{mode} {fn.__name__}: {out!r}"

    u = TimezoneManager('utc')
    assert u.to_short_display(sample) == 'Oct 17 2025, 20:13', u.to_short_display(sample)
    assert u.to_date_display(sample) == 'Oct 17 2025', u.to_date_display(sample)
    assert u.to_display(sample) == '2025-10-17 20:13:47'


def durations():
    m = TimezoneManager('local')
    assert m.format_duration(45) == "45m"
    assert m.format_duration(135) == "2h 15m"
    assert m.format_duration(120) == "2h"
    # Long gaps used to read as nonsense ("6920h 4m")
    assert m.format_duration(415204) == "288d 8h", m.format_duration(415204)
    assert m.format_duration(None) == "N/A"
    assert "ago" in m.get_relative_time(utcnow() - timedelta(hours=3))


check("every date format carries the year", dates_include_year)
check("duration formatting", durations)

# ---------------------------------------------------------------------------
section("monitor state")


def monitor_seeds_last_seen():
    from scraper.monitor import MonitorService
    m = MonitorService(db, on_product_change=None)
    # Must be seeded at construction: a manual check before start() used to
    # look like the first product ever seen and skipped validation.
    assert m.last_item_number is not None, "last seen product not seeded"
    assert m._validate_product_change(m.last_item_number) is False


check("last-seen product seeded before start()", monitor_seeds_last_seen)

# ---------------------------------------------------------------------------
section("stale front page")

# Shopify serves the front page from a server-side full-page cache that can lag
# the real store by hours - long enough for several products to come and go
# while the home page still advertises the first of them.  No request header
# defeats it, so which product is live is taken from the collection feed, and
# the front page is used only for detail and only when it agrees.
#
# Stock alone is not enough to catch this, which an earlier version of these
# tests took for granted: an item rotated out of the front of the queue but
# still on sale further down passes every stock check there is.

import requests as _real_requests  # noqa: E402  (exception types only)


def _product_html(handle, sku, title, price, compare):
    """A page carrying the product template, as both / and /products/x do."""
    return f'''<html><body><div id="shopify-section-product-template">
      <h1 itemprop="name" class="product-single__title">{title}</h1>
      <span id="ProductPrice-product-template" itemprop="price"
            content="{price}">${price}</span>
      <s id="ComparePrice-product-template">${compare}</s>
      <div class="product-single__description" itemprop="description">About {title}</div>
      <script type="application/json" id="ProductJson-product-template">
        {{"handle": "{handle}", "title": "{title}", "available": true,
          "variants": [{{"sku": "{sku}", "price": {int(price * 100)},
                         "available": true}}]}}
      </script>
    </div></body></html>'''


class _FakeResponse:
    def __init__(self, text='', payload=None, status=200):
        self.text, self._payload, self.status_code = text, payload, status
        self.content = text.encode()

    def raise_for_status(self):
        if self.status_code >= 400:
            raise _real_requests.RequestException(f'HTTP {self.status_code}')

    def json(self):
        if self._payload is None:
            raise ValueError('not json')
        return self._payload


class _FakeSession:
    def __init__(self, router):
        self._router, self.headers = router, {}

    def update(self, *_a, **_k):
        pass

    def get(self, url, timeout=None):
        return self._router(url)

    def close(self):
        pass


class _FakeRequests:
    """Stands in for the `requests` module inside a scraper."""
    RequestException = _real_requests.RequestException

    def __init__(self, router):
        self._router = router
        self.calls = []

    def get(self, url, headers=None, timeout=None):
        self.calls.append(url)
        return self._router(url)

    def Session(self):
        session = _FakeSession(self._router)
        session.headers = dict()
        return session


BASE = 'https://www.festoolrecon.com/'
SOLD = ('sold-out-table-saw', '576821', 'Cordless Table Saw CSC SYS 50', 1199.20, 1499.00)
LIVE = ('live-recip-saw', '576951', 'Cordless reciprocating saw RSC 18', 399.20, 499.00)
# In stock and still in the collection, but no longer at the front of it
QUEUED = ('queued-jigsaw', '576527', 'Cordless Jigsaw CARVEX PSC 420', 479.20, 599.00)
PAGES = (SOLD, LIVE, QUEUED)


def _feed(*entries):
    """A collection feed whose membership is stale but whose stock flags are not."""
    return {'products': [
        {'handle': h, 'title': t, 'product_type': 'Saw',
         'images': [{'src': f'https://cdn/{sku}.jpg'}],
         'variants': [{'sku': sku, 'price': f'{price:.2f}',
                       'compare_at_price': f'{cmp:.2f}', 'available': avail}]}
        for (h, sku, t, price, cmp), avail in entries]}


def _router(front, availability, feed):
    def route(url):
        path = url.split('?')[0]
        if path.rstrip('/') == BASE.rstrip('/'):
            return _FakeResponse(text=front)
        if path.endswith('.js'):
            handle = path.rsplit('/', 1)[-1][:-3]
            if availability.get(handle) == 'error':
                raise _real_requests.RequestException('boom')
            return _FakeResponse(payload={'available': availability.get(handle, False)})
        if path.endswith('products.json'):
            if feed is None:
                raise _real_requests.RequestException('feed down')
            return _FakeResponse(payload=feed)
        handle = path.rsplit('/', 1)[-1]
        for spec in PAGES:
            if spec[0] == handle:
                return _FakeResponse(text=_product_html(*spec))
        return _FakeResponse(status=404)
    return route


def _scraper_with(router):
    from scraper import festool_scraper as fs
    scraper = fs.FestoolScraper(timeout=1)
    fake = _FakeRequests(router)
    scraper._patched = fake
    return scraper, fs, fake


def stale_front_page_falls_back_to_real_stock():
    """The sold-out item on a cached front page must not be recorded."""
    router = _router(_product_html(*SOLD),
                     {SOLD[0]: False, LIVE[0]: True},
                     _feed((SOLD, False), (LIVE, True)))
    scraper, fs, fake = _scraper_with(router)
    original = fs.requests
    try:
        fs.requests = fake
        product = scraper.fetch_current_product()
    finally:
        fs.requests = original

    assert product is not None, "fell back to nothing"
    assert product['item_number'] == LIVE[1], \
        f"recorded {product['item_number']}, expected the live {LIVE[1]}"
    assert product['name'] == LIVE[2]
    assert product['sale_price'] == LIVE[3]
    assert product['handle'] == LIVE[0]


def _fetch_with(router):
    """Run one fetch_current_product() against a fake store."""
    scraper, fs, fake = _scraper_with(router)
    original = fs.requests
    try:
        fs.requests = fake
        return scraper.fetch_current_product(), fake
    finally:
        fs.requests = original


def agreeing_front_page_supplies_the_detail():
    """
    When the front page names the live product, its richer content is kept.

    The feed has no description, so throwing the page away whenever the feed
    is consulted would quietly empty a field the app records.
    """
    product, fake = _fetch_with(
        _router(_product_html(*LIVE), {LIVE[0]: True}, _feed((LIVE, True))))

    assert product['item_number'] == LIVE[1]
    assert product['description'] == f'About {LIVE[2]}', product.get('description')
    assert product['product_type'] == 'Saw', "store's own category comes from the feed"
    # The feed decides which product is live, so it is always consulted
    assert any('products.json' in u for u in fake.calls)


def in_stock_but_no_longer_live_is_not_recorded():
    """
    The failure the stock check cannot see.

    The front page is serving a cached copy naming an item that is still on
    sale - just no longer at the front of the queue.  Every stock check says
    "available", so an app that only asks "is this sold out?" sits on it
    indefinitely.  The collection order is what settles it.
    """
    product, _ = _fetch_with(
        _router(_product_html(*QUEUED),
                {QUEUED[0]: True, LIVE[0]: True},
                _feed((LIVE, True), (QUEUED, True))))

    assert product is not None
    assert product['item_number'] == LIVE[1], \
        f"recorded {product['item_number']}, which the store had already moved past"
    assert product['handle'] == LIVE[0]


def feed_price_beats_a_cached_page_price():
    """The feed is regenerated on change; the page may be a cached copy."""
    stale_page = _product_html(LIVE[0], LIVE[1], LIVE[2], 449.20, 499.00)
    product, _ = _fetch_with(
        _router(stale_page, {LIVE[0]: True}, _feed((LIVE, True))))

    assert product['sale_price'] == LIVE[3], product['sale_price']
    assert product['regular_price'] == LIVE[4]


def sold_out_head_of_the_queue_is_stepped_over():
    """A feed entry flagged available that is really gone must not be recorded."""
    product, _ = _fetch_with(
        _router(_product_html(*SOLD),
                {SOLD[0]: False, LIVE[0]: True},
                _feed((SOLD, True), (LIVE, True))))

    assert product['item_number'] == LIVE[1], product['item_number']


def feed_outage_falls_back_to_the_front_page():
    """Without the feed there is no better answer than the front page."""
    product, _ = _fetch_with(
        _router(_product_html(*LIVE), {LIVE[0]: True}, None))

    assert product is not None, "discarded a usable scrape over a feed outage"
    assert product['item_number'] == LIVE[1]


def unverifiable_stock_keeps_the_scrape():
    """A failed stock check is 'unknown', never 'sold out'."""
    router = _router(_product_html(*LIVE), {LIVE[0]: 'error'}, _feed((LIVE, True)))
    scraper, fs, fake = _scraper_with(router)
    original = fs.requests
    try:
        fs.requests = fake
        product = scraper.fetch_current_product()
    finally:
        fs.requests = original

    assert product is not None, "threw away a good scrape over a failed check"
    assert product['item_number'] == LIVE[1]


def stale_front_page_with_no_fallback_records_nothing():
    """Better to skip a check than to record a sold-out product."""
    router = _router(_product_html(*SOLD), {SOLD[0]: False}, None)
    scraper, fs, fake = _scraper_with(router)
    original = fs.requests
    try:
        fs.requests = fake
        product = scraper.fetch_current_product()
    finally:
        fs.requests = original

    assert product is None, f"recorded {product and product['item_number']} anyway"


def queue_positions_skip_sold_out_items():
    """Position 0 must be the live product, not a sold-out leftover."""
    from scraper import collection_scraper as cs
    filler = ('wow-that-went-fast', '999999', 'Wow, that went fast!!!', 0.01, 0.0)
    feed = _feed((SOLD, False), (LIVE, True),
                 (('next-up', '571935', 'Drywall Sander', 399.50, 799.00), True),
                 (filler, True))

    fake = _FakeRequests(_router('', {}, feed))
    original = cs.requests
    try:
        cs.requests = fake
        queue = cs.CollectionScraper(timeout=1).fetch_queue()
    finally:
        cs.requests = original

    assert [p.item_number for p in queue] == [LIVE[1], '571935'], \
        f"queue was {[p.item_number for p in queue]}"
    # Numbering happens after filtering, so these are contiguous from zero
    assert [p.position for p in queue] == [0, 1]
    assert queue[0].is_live is True
    assert queue[1].is_live is False


check("stale front page falls back to real stock", stale_front_page_falls_back_to_real_stock)
check("agreeing front page supplies the detail", agreeing_front_page_supplies_the_detail)
check("in-stock item that is no longer live is not recorded",
      in_stock_but_no_longer_live_is_not_recorded)
check("feed price beats a cached page price", feed_price_beats_a_cached_page_price)
check("sold-out head of the queue stepped over", sold_out_head_of_the_queue_is_stepped_over)
check("feed outage falls back to the front page", feed_outage_falls_back_to_the_front_page)
check("failed stock check does not discard scrape", unverifiable_stock_keeps_the_scrape)
check("no fallback records nothing", stale_front_page_with_no_fallback_records_nothing)
check("queue positions skip sold-out items", queue_positions_skip_sold_out_items)


# ---------------------------------------------------------------------------
section("multi-item collections")

# The store has run two shapes of front page.  These are the real markers:
# a product template and no collection links at all, versus collection cards
# and no product template.
SALE_A = ('sale-vac', '577083', 'Dust Extractor CT 26 E HEPA', 599.25, 799.00)
SALE_B = ('sale-saw', '576708', 'Track Saw TS 55 FEQ-F-Plus', 501.75, 669.00)
SALE_C = ('sale-drill', '576758', 'Cordless Drill T18+3-E Basic', 139.30, 199.00)


def _collection_front_page(*handles):
    """The front page as it looks when the store is fronting collections."""
    cards = ''.join(
        f'<div class="collection-card"><a href="/collections/{h}">{h}</a></div>'
        for h in handles
    )
    return (f'<html><body>'
            f'<div id="shopify-section-header"><nav class="site-nav">'
            f'<a href="/collections/always-in-the-menu">Shop</a></nav></div>'
            f'<div id="shopify-section-1542311855670">{cards}</div>'
            f'<div id="shopify-section-footer">'
            f'<a href="/collections/footer-junk">Footer</a></div>'
            f'</body></html>')


def _queued(spec, position=0, collection='sale'):
    handle, sku, title, price, compare = spec
    return QueuedProduct(
        position=position, item_number=sku, name=title, handle=handle,
        sale_price=price, regular_price=compare, product_type='Saw',
        image_url=f'https://cdn/{sku}.jpg', collection_handle=collection,
    )


def _storefront_from(html):
    from scraper import festool_scraper as fs
    from bs4 import BeautifulSoup
    scraper = fs.FestoolScraper(timeout=1)
    scraper._front_soup = BeautifulSoup(html, 'lxml')
    scraper._front_soup_read = True
    return scraper.read_storefront()


def single_product_front_page_reads_as_one_at_a_time():
    """The classic shape: a product template and no collection links."""
    front = _storefront_from(_product_html(*LIVE))
    assert front.readable is True
    assert front.fronts_single_product is True
    assert front.fronted_handle == LIVE[0], front.fronted_handle
    assert front.featured_collections == [], front.featured_collections


def collection_front_page_reads_as_all_at_once():
    """No product template, cards linking to what is being promoted."""
    front = _storefront_from(_collection_front_page('winter-sale', 'clearance'))
    assert front.readable is True
    assert front.fronts_single_product is False
    # Nav and footer links are furniture, not a promotion
    assert front.featured_collections == ['winter-sale', 'clearance'], \
        front.featured_collections


def unreadable_front_page_leaves_every_mode_alone():
    """A failed fetch must never be read as 'the store changed how it sells'."""
    feeds = {'winter-sale': [_queued(SALE_A), _queued(SALE_B, 1)]}
    modes = assign_modes(Storefront(readable=False), feeds,
                         previous_modes={'winter-sale': MODE_SINGLE})
    assert modes == {}, modes


def promoted_collection_is_multi_and_the_fronted_one_is_single():
    """Both shapes can be live at once; each collection is judged separately."""
    front = Storefront(readable=True, fronted_handle=LIVE[0],
                       featured_collections=['winter-sale'])
    feeds = {
        'rotation': [_queued(LIVE, 0, 'rotation')],
        'winter-sale': [_queued(SALE_A, 0, 'winter-sale')],
    }
    modes = assign_modes(front, feeds)
    assert modes == {'rotation': MODE_SINGLE, 'winter-sale': MODE_MULTI}, modes


def mode_detection_never_keys_on_a_collection_name():
    """The store renames collections; nothing may recognise a particular one."""
    front = _storefront_from(_collection_front_page('a-name-nobody-has-seen'))
    feeds = {'a-name-nobody-has-seen': [_queued(SALE_A, 0, 'a-name-nobody-has-seen')]}
    assert assign_modes(front, feeds) == {'a-name-nobody-has-seen': MODE_MULTI}

    # ...and the historical handle gets no special treatment either
    front = _storefront_from(_collection_front_page('oneanddone'))
    feeds = {'oneanddone': [_queued(SALE_A, 0, 'oneanddone')]}
    assert assign_modes(front, feeds) == {'oneanddone': MODE_MULTI}


def fronted_product_missing_from_the_feed_holds_the_known_mode():
    """A stale feed must not tip a rotating collection into multi-item mode."""
    front = Storefront(readable=True, fronted_handle='gone-already')
    feeds = {'rotation': [_queued(SALE_A, 0, 'rotation')]}
    modes = assign_modes(front, feeds, previous_modes={'rotation': MODE_SINGLE})
    assert modes == {'rotation': MODE_SINGLE}, modes


def unreadable_collection_is_left_out_of_the_feeds():
    """fetch_feeds() must not map a failed request to an empty collection."""
    from scraper import collection_scraper as cs

    def route(url):
        if 'good' in url:
            return _FakeResponse(payload=_feed((SALE_A, True)))
        raise _real_requests.RequestException('feed down')

    fake = _FakeRequests(route)
    original = cs.requests
    try:
        cs.requests = fake
        feeds = cs.CollectionScraper(timeout=1).fetch_feeds(['good', 'broken'])
    finally:
        cs.requests = original

    assert 'good' in feeds and len(feeds['good']) == 1
    assert 'broken' not in feeds, \
        "an unreadable collection came back as empty, which retires its listings"


# --- storage -------------------------------------------------------------
mdb = DatabaseManager(db_path=os.path.join(WORK, 'multi.db'),
                      backup_dir=os.path.join(WORK, 'mbackups'))
muser = mdb.get_or_create_default_user()


def multi_collection_records_every_item_at_once():
    items = [_queued(SALE_A, 0), _queued(SALE_B, 1), _queued(SALE_C, 2)]
    result = mdb.sync_live_collection(items, 'sale')

    assert len(result['added']) == 3, result
    assert result['removed'] == [] and result['skipped'] is False
    assert {p.item_number for p in result['added']} == {SALE_A[1], SALE_B[1], SALE_C[1]}
    assert all(p.collection_handle == 'sale' for p in result['added'])

    # Re-reading the same feed changes nothing
    again = mdb.sync_live_collection(items, 'sale')
    assert again['added'] == [] and again['removed'] == []
    assert again['unchanged'] == 3, again


def sold_out_item_leaves_the_window_and_gets_a_time_live():
    """Dropping out of the feed is how an item ends, same as the rotation."""
    remaining = [_queued(SALE_A, 0), _queued(SALE_B, 1)]
    result = mdb.sync_live_collection(remaining, 'sale')

    assert [p.item_number for p in result['removed']] == [SALE_C[1]], result['removed']
    assert result['removed'][0].replaced_at is not None, \
        "retirement was expunged before it was flushed"

    open_now = mdb.get_open_listings('sale')
    assert {p.item_number for p in open_now} == {SALE_A[1], SALE_B[1]}


def an_unreadable_feed_retires_nothing():
    """None means 'could not read', and must not empty the shop window."""
    before = {p.item_number for p in mdb.get_open_listings('sale')}
    result = mdb.sync_live_collection(None, 'sale')

    assert result['skipped'] is True
    assert result['added'] == [] and result['removed'] == []
    assert {p.item_number for p in mdb.get_open_listings('sale')} == before


def a_genuinely_empty_feed_ends_the_sale():
    """[] means 'read it, there is nothing there' - which does retire them."""
    result = mdb.sync_live_collection([], 'sale')

    assert len(result['removed']) == 2, result['removed']
    assert mdb.get_open_listings('sale') == []


def a_feed_entry_with_no_price_is_skipped_not_crashed():
    """sale_price is NOT NULL; a priceless entry must be dropped, not inserted."""
    priceless = _queued(SALE_A, 0)
    priceless.sale_price = None
    result = mdb.sync_live_collection([priceless, _queued(SALE_B, 1)], 'priced')

    assert [p.item_number for p in result['added']] == [SALE_B[1]], result['added']


def the_rotation_pointer_ignores_a_multi_item_sale():
    """get_latest_product() is 'what is up now', and a sale is not that."""
    rot = DatabaseManager(db_path=os.path.join(WORK, 'rot.db'),
                          backup_dir=os.path.join(WORK, 'rbackups'))
    rot.get_or_create_default_user()

    rotation_item = rot.add_product(
        item_number=LIVE[1], name=LIVE[2], sale_price=LIVE[3],
        regular_price=LIVE[4], collection_handle='rotation',
    )
    rot.sync_collections([CollectionInfo(handle='rotation', title='Rotation'),
                          CollectionInfo(handle='sale', title='Sale')])
    rot.record_collection_modes({'rotation': MODE_SINGLE, 'sale': MODE_MULTI})

    # A sale lands thirty listings, all newer than the rotation's
    rot.sync_live_collection([_queued(SALE_A, 0, 'sale'),
                              _queued(SALE_B, 1, 'sale')], 'sale')

    latest = rot.get_latest_product()
    assert latest is not None and latest.item_number == rotation_item.item_number, \
        f"rotation pointer moved to {latest and latest.item_number}"

    stats = rot.get_statistics()
    assert stats['multi_item_listings'] == 2, stats
    assert stats['rotation_listings'] == 1, stats

    # ...and the cadence figures stay about the rotation
    summary = SkuAnalytics(rot).get_catalog_summary()
    assert summary['multi_item_listings'] == 2, summary['multi_item_listings']
    assert summary['rotation_listings'] == 1, summary['rotation_listings']
    # but the sale's prices are still part of every SKU's history
    assert any(s.item_number == SALE_A[1] for s in SkuAnalytics(rot).get_sku_summaries())


def a_mode_switch_is_reported_once():
    """Worth an alert when it happens, and silent every check afterwards."""
    sdb = DatabaseManager(db_path=os.path.join(WORK, 'modes.db'),
                          backup_dir=os.path.join(WORK, 'sbackups'))
    sdb.sync_collections([CollectionInfo(handle='shop', title='Shop')])

    first = sdb.record_collection_modes({'shop': MODE_SINGLE}, {'shop': 1})
    assert first == {'shop': (None, MODE_SINGLE)}, first

    assert sdb.record_collection_modes({'shop': MODE_SINGLE}, {'shop': 1}) == {}

    switched = sdb.record_collection_modes({'shop': MODE_MULTI}, {'shop': 30})
    assert switched == {'shop': (MODE_SINGLE, MODE_MULTI)}, switched
    assert sdb.get_collection_modes() == {'shop': MODE_MULTI}
    assert sdb.multi_item_handles() == {'shop'}


def exclusions_still_hold_inside_a_batch():
    """An excluded item must not reach the user as one line of a digest."""
    edb = DatabaseManager(db_path=os.path.join(WORK, 'excl.db'),
                          backup_dir=os.path.join(WORK, 'ebackups'))
    user_id = edb.get_or_create_default_user()
    edb.add_to_exclusions(user_id, keyword='Track Saw')

    added = edb.sync_live_collection(
        [_queued(SALE_A, 0), _queued(SALE_B, 1)], 'sale')['added']

    allowed = [p for p in added if not edb.is_excluded(user_id, p)]
    assert [p.item_number for p in allowed] == [SALE_A[1]], \
        [p.item_number for p in allowed]

    # An empty watchlist means "no specific interest", so nothing gets a
    # second, individual alert on top of the digest.
    assert not any(edb.matches_watchlist(user_id, p) for p in added)


def a_digest_never_lists_a_watched_item_twice():
    """The watched section and the rest must not overlap."""
    from notifications.notifier import EmailNotifier, digest_headline

    watched_item = {'name': SALE_A[2], 'item_number': SALE_A[1],
                    'sale_price': SALE_A[3], 'regular_price': SALE_A[4],
                    'discount_pct': 25.0}
    other = {'name': SALE_B[2], 'item_number': SALE_B[1],
             'sale_price': SALE_B[3], 'regular_price': SALE_B[4]}

    digest = {'collection_handle': 'sale', 'collection_title': 'Sale',
              'collection_url': 'https://example/', 'total_live': 2,
              'items': [watched_item, other], 'watched': [watched_item],
              'ended': []}

    text = EmailNotifier('s', 587, 'u', 'p', 'f@x', 't@x')._digest_text(digest)
    assert text.count(SALE_A[2]) == 1, text
    assert SALE_B[2] in text
    assert 'watched' in digest_headline(digest).lower(), digest_headline(digest)


def one_digest_covers_the_whole_batch():
    """Thirty new listings must produce one message per channel, not thirty."""
    sent = []

    class _Recorder(WebhookNotifier):
        def send_digest(self, digest):
            sent.append(digest)
            return True

    manager = NotificationManager()
    manager.add_provider('webhook', _Recorder('https://example/hook'))

    items = [{'name': f'Tool {i}', 'item_number': str(i), 'sale_price': 100.0 + i}
             for i in range(30)]
    results = manager.send_digest({'collection_handle': 'sale', 'items': items,
                                   'watched': [], 'ended': []})

    assert results == {'webhook': True}, results
    assert len(sent) == 1, f"{len(sent)} messages sent for one batch"
    assert len(sent[0]['items']) == 30


def a_stopped_rotation_is_closed_out():
    """Nothing may go on calling the last rotated item live."""
    cdb = DatabaseManager(db_path=os.path.join(WORK, 'close.db'),
                          backup_dir=os.path.join(WORK, 'cbackups'))
    cdb.get_or_create_default_user()

    live = cdb.add_product(item_number=LIVE[1], name=LIVE[2], sale_price=LIVE[3],
                           regular_price=LIVE[4], collection_handle='rotation',
                           handle=LIVE[0])
    assert live.replaced_at is None and live.is_live

    cdb.sync_collections([CollectionInfo(handle='rotation', title='Rotation'),
                          CollectionInfo(handle='sale', title='Sale')])
    cdb.record_collection_modes({'sale': MODE_MULTI})
    cdb.sync_live_collection([_queued(SALE_A, 0, 'sale')], 'sale')

    closed = cdb.close_rotation_listing()
    assert [p.item_number for p in closed] == [LIVE[1]], closed

    latest = cdb.get_latest_product()
    assert latest.replaced_at is not None, "listing still claims to be live"
    assert latest.is_live is False
    # We know it ended; we do not know when, and must not invent a duration
    assert latest.minutes_live is None, latest.minutes_live

    # The sale's own listings are untouched - they really are on offer
    assert len(cdb.get_open_listings('sale')) == 1

    # ...and running again changes nothing
    assert cdb.close_rotation_listing() == []


def an_invented_duration_never_reaches_the_averages():
    """A closed-with-unknown-duration row must not move 'average time live'."""
    adb = DatabaseManager(db_path=os.path.join(WORK, 'avg.db'),
                          backup_dir=os.path.join(WORK, 'abackups'))
    adb.get_or_create_default_user()

    done = adb.add_product(item_number='111', name='Finished', sale_price=100.0,
                           collection_handle='rotation')
    # Back-date it so it has a real duration.  A zero-minute listing would not
    # do: the two averages disagree about those (the database counts a 0, the
    # analytics rollup drops it as falsy), which is a pre-existing quirk and
    # not what this test is about.
    from database.models import Product as _P
    with adb.get_session() as session:
        session.get(_P, done.id).posted_at = utcnow() - timedelta(minutes=90)
    adb.mark_product_replaced(done.id)

    adb.add_product(item_number='222', name='Interrupted', sale_price=200.0,
                    collection_handle='rotation')

    before = adb.get_statistics()['average_minutes_live']
    adb.close_rotation_listing()
    after = adb.get_statistics()['average_minutes_live']

    assert before == after, f"average moved from {before} to {after}"
    assert SkuAnalytics(adb).get_catalog_summary()['avg_minutes_live'] == before


def resuming_the_rotation_opens_a_new_listing():
    """Same item back up after a sale is a new listing, not 'no change'."""
    from scraper.monitor import MonitorService

    rdb = DatabaseManager(db_path=os.path.join(WORK, 'resume.db'),
                          backup_dir=os.path.join(WORK, 'rsbackups'))
    rdb.get_or_create_default_user()
    rdb.add_product(item_number=LIVE[1], name=LIVE[2], sale_price=LIVE[3],
                    collection_handle='rotation')
    rdb.close_rotation_listing()

    monitor = MonitorService(rdb, validation_checks=1, validation_delay=0)
    monitor._refresh_last_seen()

    # Without force, an unchanged item number is dismissed as no change...
    assert monitor._validate_product_change(LIVE[1]) is False
    # ...but the caller knows the rotation restarted, so it is a new listing
    assert monitor._validate_product_change(LIVE[1], force=True) is True

    monitor.shutdown()


def a_listing_links_to_its_own_page():
    """The Collection tab links each row; a guessed URL would 404."""
    ldb = DatabaseManager(db_path=os.path.join(WORK, 'links.db'),
                          backup_dir=os.path.join(WORK, 'lbackups'))

    added = ldb.sync_live_collection([_queued(SALE_A, 0, 'sale')], 'sale')['added']
    assert added[0].url == (
        'https://www.festoolrecon.com/collections/sale/products/' + SALE_A[0]
    ), added[0].url

    # A row recorded before handles were stored has no slug to build one from,
    # and gets no link rather than a broken one
    bare = ldb.add_product(item_number='999', name='Legacy row', sale_price=10.0)
    assert bare.url is None


def a_relisted_item_picks_up_its_current_slug():
    """The store re-slugs products; a stored link that 404s is worse than none."""
    sdb = DatabaseManager(db_path=os.path.join(WORK, 'slug.db'),
                          backup_dir=os.path.join(WORK, 'slbackups'))

    sdb.sync_live_collection([_queued(SALE_A, 0, 'sale')], 'sale')

    moved = _queued(SALE_A, 0, 'sale')
    moved.handle = 'renamed-by-the-store'
    sdb.sync_live_collection([moved], 'sale')

    row = sdb.get_open_listings('sale')[0]
    assert row.handle == 'renamed-by-the-store', row.handle
    assert row.url.endswith('/renamed-by-the-store')


check("a stopped rotation is closed out", a_stopped_rotation_is_closed_out)
check("no invented duration reaches the averages",
      an_invented_duration_never_reaches_the_averages)
check("resuming the rotation opens a new listing",
      resuming_the_rotation_opens_a_new_listing)
check("a listing links to its own page", a_listing_links_to_its_own_page)
check("a relisted item picks up its current slug",
      a_relisted_item_picks_up_its_current_slug)
check("front page with a product template reads as one-at-a-time",
      single_product_front_page_reads_as_one_at_a_time)
check("front page of collection cards reads as all-at-once",
      collection_front_page_reads_as_all_at_once)
check("unreadable front page leaves every mode alone",
      unreadable_front_page_leaves_every_mode_alone)
check("fronted and promoted collections are judged separately",
      promoted_collection_is_multi_and_the_fronted_one_is_single)
check("mode detection never keys on a collection name",
      mode_detection_never_keys_on_a_collection_name)
check("stale feed cannot tip a rotation into multi-item mode",
      fronted_product_missing_from_the_feed_holds_the_known_mode)
check("unreadable collection is left out of the feeds",
      unreadable_collection_is_left_out_of_the_feeds)
check("multi-item collection records every item at once",
      multi_collection_records_every_item_at_once)
check("sold-out item leaves the window with a time live",
      sold_out_item_leaves_the_window_and_gets_a_time_live)
check("an unreadable feed retires nothing", an_unreadable_feed_retires_nothing)
check("a genuinely empty feed ends the sale", a_genuinely_empty_feed_ends_the_sale)
check("feed entry with no price is skipped", a_feed_entry_with_no_price_is_skipped_not_crashed)
check("rotation pointer ignores a multi-item sale",
      the_rotation_pointer_ignores_a_multi_item_sale)
check("a mode switch is reported once", a_mode_switch_is_reported_once)
check("exclusions still hold inside a batch", exclusions_still_hold_inside_a_batch)
check("a digest never lists a watched item twice", a_digest_never_lists_a_watched_item_twice)
check("one digest covers the whole batch", one_digest_covers_the_whole_batch)



# ---------------------------------------------------------------------------
section("time live, and the three states of minutes_live")


def _timed_db(name):
    tdb = DatabaseManager(db_path=os.path.join(WORK, name),
                          backup_dir=os.path.join(WORK, name + '.backups'))
    tdb.get_or_create_default_user()
    return tdb


def _listing_lasting(db, item_number, minutes, collection='rotation'):
    """Record a listing that ran for exactly `minutes`."""
    from database.models import Product as _P
    row = db.add_product(item_number=item_number, name=f'Tool {item_number}',
                         sale_price=100.0, collection_handle=collection)
    with db.get_session() as session:
        session.get(_P, row.id).posted_at = utcnow() - timedelta(minutes=minutes)
    db.mark_product_replaced(row.id)
    return row


def a_sub_minute_listing_is_counted_not_dropped():
    """0 means 'gone in under a minute', not 'no idea' - it is real data."""
    db0 = _timed_db('sub_minute.db')
    _listing_lasting(db0, '111', 0)
    _listing_lasting(db0, '222', 60)

    stats = db0.get_statistics()
    summary = SkuAnalytics(db0).get_catalog_summary()

    # Both averages must see both listings: (0 + 60) / 2
    assert stats['average_minutes_live'] == 30.0, stats['average_minutes_live']
    assert summary['avg_minutes_live'] == 30.0, summary['avg_minutes_live']


def the_two_averages_agree():
    """They disagreed: one filtered on NULL, the other on truthiness."""
    db1 = _timed_db('agree.db')
    for i, minutes in enumerate((0, 0, 5, 90, 1440)):
        _listing_lasting(db1, str(500 + i), minutes)

    from_db = db1.get_statistics()['average_minutes_live']
    from_analytics = SkuAnalytics(db1).get_catalog_summary()['avg_minutes_live']

    assert abs(from_db - from_analytics) < 0.01, (from_db, from_analytics)
    assert from_db == round(sum((0, 0, 5, 90, 1440)) / 5, 2), from_db


def an_unknown_duration_is_still_excluded():
    """NULL is the genuine 'we do not know' and must stay out of the averages."""
    db2 = _timed_db('unknown.db')
    _listing_lasting(db2, '111', 10)
    db2.add_product(item_number='222', name='Interrupted', sale_price=50.0,
                    collection_handle='rotation')

    before = db2.get_statistics()['average_minutes_live']
    db2.close_rotation_listing()

    assert db2.get_statistics()['average_minutes_live'] == before
    assert SkuAnalytics(db2).get_catalog_summary()['avg_minutes_live'] == before


def a_sub_minute_listing_is_never_reported_as_current():
    """The HTML report read minutes_live == 0 as falsy and said "Current"."""
    db3 = _timed_db('report.db')
    _listing_lasting(db3, '111', 0)

    from utils.export import ReportGenerator
    path = ReportGenerator(export_dir=os.path.join(WORK, 'reports')).generate_html_report(
        db3.get_product_history(), db3.get_statistics()
    )
    with open(path, encoding='utf-8') as report:
        body = report.read()

    assert 'Current' not in body, "a finished listing was reported as still live"
    assert '0 min' in body, body[:200]


def a_withdrawn_collection_stops_claiming_to_be_on_offer():
    """A sale can end by the collection being pulled, not just by emptying."""
    wdb = DatabaseManager(db_path=os.path.join(WORK, 'withdrawn.db'),
                          backup_dir=os.path.join(WORK, 'wbackups'))
    wdb.sync_collections([CollectionInfo(handle='sale', title='Sale')])
    wdb.record_collection_modes({'sale': MODE_MULTI})
    wdb.sync_live_collection([_queued(SALE_A, 0, 'sale'),
                              _queued(SALE_B, 1, 'sale')], 'sale')

    assert len(wdb.get_open_listings('sale')) == 2

    closed = wdb.close_collection_listings('sale')
    assert len(closed) == 2, closed
    assert all(row.replaced_at is not None for row in closed)
    # How long they were on offer is unknown, and stays unknown
    assert all(row.minutes_live is None for row in closed)
    assert wdb.get_open_listings('sale') == []

    # Idempotent
    assert wdb.close_collection_listings('sale') == []


check("a sub-minute listing is counted, not dropped",
      a_sub_minute_listing_is_counted_not_dropped)
check("the two time-live averages agree", the_two_averages_agree)
check("an unknown duration is still excluded", an_unknown_duration_is_still_excluded)
check("a sub-minute listing is never reported as current",
      a_sub_minute_listing_is_never_reported_as_current)
check("a withdrawn collection stops claiming to be on offer",
      a_withdrawn_collection_stops_claiming_to_be_on_offer)


# ---------------------------------------------------------------------------
section("per-item watch / exclude filters")

fdb = DatabaseManager(db_path=os.path.join(WORK, 'filters.db'),
                      backup_dir=os.path.join(WORK, 'fbackups'))
fuser = fdb.get_or_create_default_user()


def _state(item_number, name=''):
    return fdb.classify_item(fdb.get_filter_rules(fuser), item_number, name)


def watching_an_item_makes_it_notify():
    assert fdb.set_item_filter(fuser, '577083', 'watch') == 'watch'
    assert _state('577083')['rule'] == 'watch'

    candidate = Product(item_number='577083', name='Dust Extractor CT 26',
                        sale_price=599.25)
    assert fdb.matches_watchlist(fuser, candidate) is True
    assert fdb.is_excluded(fuser, candidate) is False


def excluding_replaces_watching_rather_than_joining_it():
    """Both at once has no meaning: an excluded item can never notify."""
    assert fdb.set_item_filter(fuser, '577083', 'exclude') == 'exclude'
    assert _state('577083')['rule'] == 'exclude'

    candidate = Product(item_number='577083', name='Dust Extractor CT 26',
                        sale_price=599.25)
    assert fdb.is_excluded(fuser, candidate) is True
    assert fdb.matches_watchlist(fuser, candidate) is False
    assert fdb.should_notify_user(fuser, candidate) is False

    # ...and no stale watch rule was left lying underneath it
    assert not [w for w in fdb.get_user_watchlist(fuser)
                if (w.keyword or '') == '577083']


def clearing_removes_both_rules():
    assert fdb.set_item_filter(fuser, '577083', None) is None
    assert _state('577083')['rule'] is None
    assert not [e for e in fdb.get_user_exclusions(fuser)
                if (e.item_number or '') == '577083']


def clearing_one_item_never_deletes_a_keyword():
    """A keyword covers many listings; an item menu must not reach it."""
    fdb.add_to_watchlist(fuser, 'sander')
    fdb.set_item_filter(fuser, '576032', 'watch')

    state = _state('576032', 'Multi-Mode Sander ROTEX RO 125')
    assert state['rule'] == 'watch'
    assert state['watched_by'] == ['sander'], state['watched_by']

    fdb.set_item_filter(fuser, '576032', None)

    # The item rule is gone; the keyword is untouched and still matching
    after = _state('576032', 'Multi-Mode Sander ROTEX RO 125')
    assert after['rule'] is None
    assert after['watched_by'] == ['sander'], after['watched_by']
    assert 'sander' in [(w.keyword or '') for w in fdb.get_user_watchlist(fuser)]


def a_rule_is_never_added_twice():
    """Duplicates made a removal look as though it had not worked."""
    ddb = DatabaseManager(db_path=os.path.join(WORK, 'dupes.db'),
                          backup_dir=os.path.join(WORK, 'dbackups'))
    duser = ddb.get_or_create_default_user()

    assert ddb.add_to_watchlist(duser, 'kapex') is True
    assert ddb.add_to_watchlist(duser, 'kapex') is False
    assert ddb.add_to_watchlist(duser, 'KAPEX') is False
    assert len(ddb.get_user_watchlist(duser)) == 1

    assert ddb.add_to_exclusions(duser, item_number='999') is True
    assert ddb.add_to_exclusions(duser, item_number='999') is False
    assert len(ddb.get_user_exclusions(duser)) == 1

    # And one removal really is enough
    assert ddb.remove_from_watchlist(duser, 'kapex') == 1
    assert ddb.get_user_watchlist(duser) == []


def setting_the_same_filter_twice_is_harmless():
    fdb.set_item_filter(fuser, '111222', 'watch')
    fdb.set_item_filter(fuser, '111222', 'watch')
    matching = [w for w in fdb.get_user_watchlist(fuser)
                if (w.keyword or '') == '111222']
    assert len(matching) == 1, matching


check("watching an item makes it notify", watching_an_item_makes_it_notify)
check("excluding replaces watching", excluding_replaces_watching_rather_than_joining_it)
check("clearing removes both rules", clearing_removes_both_rules)
check("clearing one item never deletes a keyword", clearing_one_item_never_deletes_a_keyword)
check("a rule is never added twice", a_rule_is_never_added_twice)
check("setting the same filter twice is harmless", setting_the_same_filter_twice_is_harmless)


# ---------------------------------------------------------------------------
section("what the Filters tab says will notify")


def _fresh_filter_db(name):
    xdb = DatabaseManager(db_path=os.path.join(WORK, f'{name}.db'),
                          backup_dir=os.path.join(WORK, f'{name}_backups'))
    return xdb, xdb.get_or_create_default_user()


def the_preview_and_the_notifier_always_agree():
    """
    The preview is only worth having if it cannot drift from the real check.

    Every combination of rule here is run both ways: through explain_item()
    on the rules, as the Filters tab does, and through the per-product
    methods the notification paths call.
    """
    products = [
        Product(item_number='577083', name='Dust Extractor CT 26', sale_price=1),
        Product(item_number='576541', name='Cordless Drill TXS 18', sale_price=1),
        Product(item_number='575321', name='Domino Joiner DF 500', sale_price=1),
        Product(item_number='', name='Mystery Sander', sale_price=1),
    ]
    setups = [
        [],
        [('watch', 'domino')],
        [('exclude_kw', 'cordless')],
        [('exclude_item', '577083')],
        [('watch', '577083'), ('exclude_kw', 'extractor')],
        [('watch', 'sander'), ('exclude_item', '576541'), ('watch', 'drill')],
        [('exclude_kw', '575321')],
    ]
    for n, setup in enumerate(setups):
        xdb, xuser = _fresh_filter_db(f'agree{n}')
        for kind, value in setup:
            if kind == 'watch':
                xdb.add_to_watchlist(xuser, value)
            elif kind == 'exclude_kw':
                xdb.add_to_exclusions(xuser, keyword=value)
            else:
                xdb.add_to_exclusions(xuser, item_number=value)

        rules = xdb.get_filter_rules(xuser)
        for p in products:
            v = xdb.explain_item(rules, p.item_number, p.name)
            where = f"{setup} / {p.name}"
            assert v['live'] == xdb.should_notify_user(xuser, p), where
            assert v['excluded'] == xdb.is_excluded(xuser, p), where
            assert v['watched'] == xdb.matches_watchlist(xuser, p), where
            assert v['digest'] == ('omitted' if v['excluded'] else
                                   'alert' if v['watched'] else 'listed'), where


def one_watch_rule_silences_everything_else():
    """The switch the confirmation dialog warns about."""
    xdb, xuser = _fresh_filter_db('switch')
    drill = Product(item_number='576541', name='Cordless Drill TXS 18', sale_price=1)

    rules = xdb.get_filter_rules(xuser)
    before = xdb.explain_item(rules, drill.item_number, drill.name)
    assert before['live'] and not before['watchlist_active']

    xdb.set_item_filter(xuser, '577083', 'watch')
    rules = xdb.get_filter_rules(xuser)
    after = xdb.explain_item(rules, drill.item_number, drill.name)
    assert after['watchlist_active']
    assert not after['live'], "an unwatched item goes quiet in watchlist mode"
    assert after['digest'] == 'listed', "but it is still in a sale digest"
    assert after['reason'] == "Not on your watchlist"


def saving_settings_keeps_right_click_exclusions():
    """
    Saving the Settings dialog used to rebuild the exclusion table from a
    keyword-only text box, deleting every item excluded by right-click.
    """
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from PyQt6.QtWidgets import QApplication, QMessageBox
    import main as app_main

    # Held in a global: an unreferenced QApplication is collected at once,
    # and Qt aborts the whole process when the dialog is then built.
    global _QT_APP
    _QT_APP = QApplication.instance() or QApplication([])
    xdb, xuser = _fresh_filter_db('settings_save')
    xdb.ensure_user_settings(xuser)
    xdb.set_item_filter(xuser, '577083', 'exclude')
    xdb.add_to_exclusions(xuser, keyword='cordless')
    xdb.add_to_watchlist(xuser, 'domino')
    before = xdb.get_filter_rules(xuser)

    original = QMessageBox.information
    QMessageBox.information = staticmethod(lambda *a, **k: None)
    try:
        dialog = app_main.SettingsDialog(xdb, xuser)
        dialog.save_settings()
    finally:
        QMessageBox.information = original

    assert xdb.get_filter_rules(xuser) == before, xdb.get_filter_rules(xuser)
    excluded = Product(item_number='577083', name='Dust Extractor CT 26', sale_price=1)
    assert xdb.is_excluded(xuser, excluded)


check("the preview and the notifier always agree", the_preview_and_the_notifier_always_agree)
check("one watch rule silences everything else", one_watch_rule_silences_everything_else)
check("saving settings keeps right-click exclusions", saving_settings_keeps_right_click_exclusions)



# ---------------------------------------------------------------------------
section("notification deadlines")


class _RecordingPost:
    """Stands in for requests.post, remembering how it was called."""

    def __init__(self, status=200):
        self.calls = []
        self.status = status

    def __call__(self, url, **kwargs):
        self.calls.append({'url': url, **kwargs})
        return _FakeResponse(payload={'ok': True}, status=self.status)


def every_provider_carries_a_deadline():
    """
    A send that cannot time out does not fail - it never returns.

    That is not theoretical: the Pushbullet client used to build a bare
    session with no timeout, so a push could block until the process died.
    """
    providers = [
        EmailNotifier('smtp.example', 587, 'u', 'p', 'f@x', 't@x'),
        SMSNotifier('twilio', 'sid', 'token', '+1', '+2'),
        PushbulletNotifier('key'),
        WebhookNotifier('https://example/hook'),
    ]
    for provider in providers:
        assert isinstance(provider.timeout, int), type(provider.timeout)
        assert provider.timeout > 0, provider.timeout

    # The webhook keeps the shorter deadline it has always had
    assert WebhookNotifier('https://example/hook').timeout == 10
    assert PushbulletNotifier('key').timeout == NotificationProvider.DEFAULT_TIMEOUT
    # ...and an explicit one still wins
    assert PushbulletNotifier('key', timeout=3).timeout == 3


def pushbullet_posts_directly_with_a_timeout():
    """No client library: it makes four blocking calls before pushing anything."""
    from notifications import notifier as nf

    post = _RecordingPost()
    original = nf.requests.post
    try:
        nf.requests.post = post
        sent = PushbulletNotifier('my-key', timeout=7).send({
            'name': 'Track Saw TS 55', 'item_number': '576708',
            'sale_price': 501.75, 'regular_price': 669.00,
            'posted_at': utcnow(),
        })
    finally:
        nf.requests.post = original

    assert sent is True
    assert len(post.calls) == 1, f"{len(post.calls)} calls for one push"

    call = post.calls[0]
    assert call['url'] == 'https://api.pushbullet.com/v2/pushes', call['url']
    assert call['timeout'] == 7, call['timeout']
    assert call['headers']['Access-Token'] == 'my-key'
    assert call['json']['type'] == 'link'
    assert 'Track Saw TS 55' in call['json']['title']


def a_pushbullet_failure_is_reported_not_raised():
    """A dead push service must not take the notification manager with it."""
    from notifications import notifier as nf

    def explode(*_a, **_k):
        raise _real_requests.RequestException('connection reset')

    original = nf.requests.post
    try:
        nf.requests.post = explode
        assert PushbulletNotifier('key').send({
            'name': 'x', 'item_number': '1', 'sale_price': 1.0,
            'posted_at': utcnow(),
        }) is False
    finally:
        nf.requests.post = original


def one_bad_provider_does_not_stop_the_others():
    """Results come back per channel; a failure is a False, not an exception."""
    class _Exploding(WebhookNotifier):
        def send(self, *_a, **_k):
            raise RuntimeError('provider is broken')

    class _Working(WebhookNotifier):
        def send(self, *_a, **_k):
            return True

    manager = NotificationManager()
    manager.add_provider('broken', _Exploding('https://example/a'))
    manager.add_provider('working', _Working('https://example/b'))

    results = manager.send_notification({'name': 'x', 'item_number': '1',
                                         'sale_price': 1.0}, [], False)
    assert results == {'broken': False, 'working': True}, results


check("every provider carries a deadline", every_provider_carries_a_deadline)
check("pushbullet posts directly with a timeout", pushbullet_posts_directly_with_a_timeout)
check("a pushbullet failure is reported, not raised",
      a_pushbullet_failure_is_reported_not_raised)
check("one bad provider does not stop the others", one_bad_provider_does_not_stop_the_others)



# ---------------------------------------------------------------------------
section("items that were queued and pulled")

NEARLY = ('nearly-sander', '580001', 'Rotex RO 90 DX FEQ-Plus', 463.20, 579.00)
STAYED = ('stayed-put', '580002', 'Cordless Drill CXS 12', 260.00, 325.00)
BOTH = ('both-ways', '580003', 'Track Saw TS 55 FEQ-F-Plus', 501.75, 669.00)


def _skip_db(name):
    return DatabaseManager(db_path=os.path.join(WORK, name),
                           backup_dir=os.path.join(WORK, name + '.backups'))


def _pull_from_queue(db, spec, collection='rotation'):
    """Queue an item behind another, then take it away without it going live."""
    keeper = _queued(STAYED, 0, collection)
    db.sync_queue([keeper, _queued(spec, 1, collection)], collection)
    db.sync_queue([keeper], collection)


def only_pulled_items_count_as_skipped():
    """Live and still-waiting items are not near-misses."""
    db = _skip_db('skips.db')
    _pull_from_queue(db, NEARLY)

    skipped = db.get_queue_skips()
    assert [r.item_number for r in skipped] == [NEARLY[1]], \
        [r.item_number for r in skipped]
    assert skipped[0].removed_at is not None
    assert skipped[0].went_live_at is None

    # The one that stayed is still pending, not skipped
    assert STAYED[1] not in [r.item_number for r in skipped]


def skips_are_found_whatever_the_collection_was_called():
    """The store renames the collection it rotates; the item is the same item."""
    db = _skip_db('skips_renamed.db')
    _pull_from_queue(db, NEARLY, collection='oneanddone')
    _pull_from_queue(db, BOTH, collection='a-new-name')

    across = {r.item_number for r in db.get_queue_skips()}
    assert across == {NEARLY[1], BOTH[1]}, across

    just_one = {r.item_number for r in db.get_queue_skips('a-new-name')}
    assert just_one == {BOTH[1]}, just_one


def a_never_listed_item_is_opt_in_and_marked():
    """It has to be unmistakable that nothing here was ever for sale."""
    db = _skip_db('never_listed.db')
    db.get_or_create_default_user()
    _pull_from_queue(db, NEARLY)

    analytics = SkuAnalytics(db)

    # Off by default - callers that count SKUs mean things that were for sale
    assert analytics.get_sku_summaries() == []

    summaries = analytics.get_sku_summaries(include_never_listed=True)
    assert len(summaries) == 1, summaries

    only = summaries[0]
    assert only.item_number == NEARLY[1]
    assert only.never_listed is True
    assert only.times_listed == 0, only.times_listed
    assert only.times_skipped == 1
    assert only.is_current is False
    # It was never live, so there is no duration to report
    assert only.avg_minutes_live is None
    assert only.total_minutes_live == 0
    # The price is the one it was advertised at in the queue
    assert only.lowest_price == NEARLY[3]


def a_listed_item_also_records_its_near_misses():
    """Being queued and pulled must never be added into times_listed."""
    db = _skip_db('both.db')
    db.get_or_create_default_user()

    db.add_product(item_number=BOTH[1], name=BOTH[2], sale_price=BOTH[3],
                   regular_price=BOTH[4], collection_handle='rotation')
    _pull_from_queue(db, BOTH)

    summary = next(s for s in SkuAnalytics(db).get_sku_summaries()
                   if s.item_number == BOTH[1])

    assert summary.times_listed == 1, summary.times_listed
    assert summary.times_skipped == 1, summary.times_skipped
    assert summary.never_listed is False
    assert summary.was_skipped is True
    assert summary.last_skipped_at is not None


def near_misses_stay_out_of_the_catalog_figures():
    """A tool nobody could buy must not turn up among the best deals."""
    db = _skip_db('catalog_skips.db')
    db.get_or_create_default_user()
    db.add_product(item_number=STAYED[1], name=STAYED[2], sale_price=STAYED[3],
                   regular_price=STAYED[4], collection_handle='rotation')
    _pull_from_queue(db, NEARLY)

    analytics = SkuAnalytics(db)
    catalog = analytics.get_catalog_summary()

    assert catalog['total_skus'] == 1, catalog['total_skus']
    assert all(not s.never_listed for s in catalog['best_deals'])
    assert all(not s.never_listed for s in catalog['most_listed'])

    # ...but the tab that opts in does see it
    assert any(s.never_listed for s in
               analytics.get_sku_summaries(include_never_listed=True))


def a_queued_item_with_no_price_is_dropped_not_crashed():
    """The feed occasionally carries an entry with no price at all."""
    db = _skip_db('priceless_skip.db')
    keeper = _queued(STAYED, 0, 'rotation')
    priceless = _queued(NEARLY, 1, 'rotation')
    priceless.sale_price = None

    db.sync_queue([keeper, priceless], 'rotation')
    db.sync_queue([keeper], 'rotation')

    assert [r.item_number for r in db.get_queue_skips()] == [NEARLY[1]]
    # No price means nothing to summarise, so it is left out rather than
    # invented
    assert SkuAnalytics(db).get_sku_summaries(include_never_listed=True) == []


def the_export_says_which_rows_were_never_listed():
    db = _skip_db('skip_export.db')
    db.get_or_create_default_user()
    db.add_product(item_number=STAYED[1], name=STAYED[2], sale_price=STAYED[3],
                   collection_handle='rotation')
    _pull_from_queue(db, NEARLY)

    path = os.path.join(WORK, 'sku_skips.csv')
    SkuAnalytics(db).export_sku_history(path)

    with open(path, encoding='utf-8') as f:
        rows = list(csv.DictReader(f))

    by_number = {r['Item Number']: r for r in rows}
    assert by_number[NEARLY[1]]['Ever Listed'] == 'No'
    assert by_number[NEARLY[1]]['Times Listed'] == '0'
    assert by_number[NEARLY[1]]['Times Queued And Pulled'] == '1'
    assert by_number[STAYED[1]]['Ever Listed'] == 'Yes'


check("only pulled items count as skipped", only_pulled_items_count_as_skipped)
check("skips are found whatever the collection was called",
      skips_are_found_whatever_the_collection_was_called)
check("a never-listed item is opt-in and marked", a_never_listed_item_is_opt_in_and_marked)
check("a listed item also records its near misses",
      a_listed_item_also_records_its_near_misses)
check("near misses stay out of the catalog figures",
      near_misses_stay_out_of_the_catalog_figures)
check("a queued item with no price is dropped, not crashed",
      a_queued_item_with_no_price_is_dropped_not_crashed)
check("the export says which rows were never listed",
      the_export_says_which_rows_were_never_listed)


# ---------------------------------------------------------------------------
shutil.rmtree(WORK, ignore_errors=True)

print("\n" + "=" * 60)
if FAILURES:
    print(f"FAILED ({len(FAILURES)}): {', '.join(FAILURES)}")
    sys.exit(1)
print("ALL TESTS PASSED")
sys.exit(0)
