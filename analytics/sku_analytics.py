"""
SKU-level history and price intelligence.

The products table is an event log: one row per time an item appeared on the
Recon page.  Items recur, so the interesting questions ("have I seen this
before?", "is this a good price?", "how long do these stay up?") need the log
rolled up per item number.  That is what this module does.

All timestamps handled here are naive UTC, as stored; formatting for display is
the caller's job (see utils.timezone_utils).
"""
import logging
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from database.models import Product
from analytics.product_types import summarize_types

logger = logging.getLogger(__name__)


# Prices are compared to the cent; anything closer than this is the same
# price wearing a float's rounding error, not a cheaper one.
PRICE_EPSILON = 0.005


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _money(value: Optional[float]) -> str:
    return f"${value:,.2f}" if value is not None else "-"


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _pct_suffix(pct: Optional[float]) -> str:
    """
    ' (12%)', or nothing at all when the percentage would round to zero.

    A 40-cent move on a $700 tool is real but rounds to "0%", and a claim of
    being 0% below the previous best reads as a contradiction.
    """
    if pct is None or abs(pct) < 0.5:
        return ""
    return f" ({abs(pct):.0f}%)"


def discount_pct(regular_price: Optional[float], sale_price: Optional[float]) -> Optional[float]:
    """Percent off MSRP, or None when there is no usable comparison."""
    if not regular_price or not sale_price or regular_price <= 0:
        return None
    if sale_price > regular_price:
        return None
    return (regular_price - sale_price) / regular_price * 100.0


@dataclass
class Listing:
    """A single appearance of an item on the Recon page."""
    product_id: int
    item_number: str
    name: str
    sale_price: float
    regular_price: Optional[float]
    posted_at: Optional[datetime]
    replaced_at: Optional[datetime]
    minutes_live: Optional[int]
    product_type: Optional[str] = None
    collection_handle: Optional[str] = None
    # True when this came from a collection the store was selling all at once.
    # Prices from those count everywhere - a price is a price - but anything
    # measuring the *rhythm* of the store has to leave them out: thirty items
    # posted together in one minute and left up for days is not thirty
    # rotations, and averaging it in describes neither.
    is_multi_item: bool = False

    @property
    def discount_pct(self) -> Optional[float]:
        return discount_pct(self.regular_price, self.sale_price)

    @property
    def is_current(self) -> bool:
        return self.replaced_at is None


@dataclass
class SkuSummary:
    """Rolled-up history for one item number."""
    item_number: str
    name: str
    times_listed: int
    first_seen: Optional[datetime]
    last_seen: Optional[datetime]
    first_price: float
    last_price: float
    lowest_price: float
    highest_price: float
    regular_price: Optional[float]
    best_discount_pct: Optional[float]
    last_discount_pct: Optional[float]
    avg_minutes_live: Optional[float]
    total_minutes_live: int
    is_current: bool
    years_seen: List[int] = field(default_factory=list)

    # Times the store lined this item up in the look-ahead queue and then
    # pulled it without ever fronting it.  Kept apart from times_listed on
    # purpose: being queued is not being for sale, and the two must never be
    # added together into a single "how often have I seen this" number.
    times_skipped: int = 0
    last_skipped_at: Optional[datetime] = None

    # True when the item has *only* ever been queued and pulled - no listing
    # of it has ever been recorded.  Every price on such a summary comes from
    # the queue feed, which is what the store was advertising, not a price it
    # was ever seen selling at.  Anything showing one of these has to say so.
    never_listed: bool = False

    @property
    def days_since_last_seen(self) -> Optional[int]:
        if not self.last_seen:
            return None
        return max(0, (_utcnow() - self.last_seen).days)

    @property
    def price_change(self) -> Optional[float]:
        """Absolute change from the first price seen to the most recent."""
        if self.times_listed < 2:
            return None
        return self.last_price - self.first_price

    @property
    def price_trend(self) -> str:
        """'down' / 'up' / 'flat' / 'n/a' across repeat listings."""
        change = self.price_change
        if change is None:
            return 'n/a'
        if abs(change) < 0.01:
            return 'flat'
        return 'down' if change < 0 else 'up'

    @property
    def is_repeat(self) -> bool:
        return self.times_listed > 1

    @property
    def was_skipped(self) -> bool:
        """The store queued this and pulled it at least once."""
        return self.times_skipped > 0

    @property
    def days_since_skipped(self) -> Optional[int]:
        if not self.last_skipped_at:
            return None
        return max(0, (_utcnow() - self.last_skipped_at).days)


@dataclass
class PriceVerdict:
    """
    How the current price compares with everything seen before.

    ``is_best_ever`` means nothing has ever been cheaper, which includes a
    price that merely equals the previous best.  ``is_new_low`` is the
    stricter claim: cheaper than anything on record.  Keeping them apart is
    the difference between "best price seen (previous best $719.20)" - which
    reads as nonsense when the two figures are the same number - and saying
    plainly that the item is back at the same price as last time.
    """
    item_number: str
    sale_price: float
    is_first_sighting: bool
    is_best_ever: bool
    is_new_low: bool
    previous_best: Optional[float]
    previous_worst: Optional[float]
    times_listed_before: int
    delta_vs_best: Optional[float]       # positive = more expensive than best
    delta_pct_vs_best: Optional[float]
    discount_pct: Optional[float]

    @property
    def headline(self) -> str:
        """Single-line summary suitable for the UI or a notification."""
        if self.is_first_sighting:
            if self.discount_pct:
                return f"First time seen - {self.discount_pct:.0f}% off MSRP"
            return "First time seen"

        prior = _plural(self.times_listed_before, "prior listing")

        if self.is_new_low:
            return (f"Best price yet - {_money(abs(self.delta_vs_best))}"
                    f"{_pct_suffix(self.delta_pct_vs_best)} below the previous "
                    f"best of {_money(self.previous_best)} in {prior}")

        if self.is_best_ever:
            return (f"Matches its best price, {_money(self.previous_best)}, "
                    f"already seen in {prior}")

        if self.delta_vs_best is not None:
            return (f"{_money(self.delta_vs_best)}"
                    f"{_pct_suffix(self.delta_pct_vs_best)} above the best seen "
                    f"({_money(self.previous_best)} in {prior})")

        return f"Seen in {prior}"

    @property
    def short_label(self) -> str:
        """Compact form for a table cell."""
        if self.is_first_sighting:
            return "Never seen before"
        if self.is_new_low:
            return f"New low (best was {_money(self.previous_best)})"
        if self.is_best_ever:
            return f"Matches best ({_money(self.previous_best)})"
        return (f"{_money(self.delta_vs_best)} above best "
                f"({_money(self.previous_best)})")

    @property
    def previous_range_text(self) -> Optional[str]:
        """
        What this item has cost before, in words.

        A single prior listing has no range, and neither does a run of
        listings that were all at one price - printing "between $719.20 and
        $719.20" for either invites the reader to look for a difference that
        is not there.  None when there is no history to describe.
        """
        if self.is_first_sighting or self.previous_best is None:
            return None

        times = _plural(self.times_listed_before, "time")

        if self.times_listed_before == 1:
            return f"Previously seen once, at {_money(self.previous_best)}"

        if (self.previous_worst is not None
                and abs(self.previous_worst - self.previous_best) < 0.01):
            return (f"Previously seen {times}, always at "
                    f"{_money(self.previous_best)}")

        return (f"Previously seen {times}, between "
                f"{_money(self.previous_best)} and {_money(self.previous_worst)}")


class SkuAnalytics:
    """Builds SKU rollups and catalog statistics from the product log."""

    def __init__(self, db_manager):
        self.db_manager = db_manager

    # ------------------------------------------------------------------
    # Raw access
    # ------------------------------------------------------------------
    def _multi_handles(self) -> set:
        """Collections the store is (or was last seen) selling all at once."""
        try:
            return self.db_manager.multi_item_handles()
        except Exception as e:
            logger.debug(f"Could not read collection modes: {e}")
            return set()

    def _to_listing(self, row, multi_handles: set) -> Listing:
        return Listing(
            product_id=row.id,
            item_number=row.item_number,
            name=row.name,
            sale_price=row.sale_price,
            regular_price=row.regular_price,
            posted_at=row.posted_at,
            replaced_at=row.replaced_at,
            minutes_live=row.minutes_live,
            product_type=row.product_type,
            collection_handle=row.collection_handle,
            is_multi_item=bool(row.collection_handle
                               and row.collection_handle in multi_handles),
        )

    def _all_listings(self) -> List[Listing]:
        """
        Every recorded listing, oldest first.

        Insertion order breaks ties on the timestamp: two listings recorded in
        the same check share a posted_at to the microsecond, and without a
        tiebreak "the first price" and "the most recent price" would be
        whichever row the database happened to hand back first.  A multi-item
        sale makes that tie the normal case rather than a rarity - thirty rows
        land on the same microsecond - so the tiebreak matters more than ever.
        """
        multi_handles = self._multi_handles()
        with self.db_manager.get_session() as session:
            rows = (session.query(Product)
                    .order_by(Product.posted_at.asc(), Product.id.asc())
                    .all())
            return [self._to_listing(r, multi_handles) for r in rows]

    def get_listings_for_sku(self, item_number: str) -> List[Listing]:
        """Every appearance of one item number, newest first."""
        multi_handles = self._multi_handles()
        with self.db_manager.get_session() as session:
            rows = (session.query(Product)
                    .filter(Product.item_number == item_number)
                    .order_by(Product.posted_at.desc(), Product.id.desc())
                    .all())
            return [self._to_listing(r, multi_handles) for r in rows]

    # ------------------------------------------------------------------
    # Rollups
    # ------------------------------------------------------------------
    def _skips_by_sku(self) -> Dict[str, List]:
        """Queue sightings that ended without the item ever going live."""
        grouped: Dict[str, List] = {}
        try:
            for row in self.db_manager.get_queue_skips():
                if row.item_number:
                    grouped.setdefault(row.item_number, []).append(row)
        except Exception as e:
            logger.debug(f"Could not read queue skips: {e}")
        return grouped

    def get_skips_for_sku(self, item_number: str) -> List:
        """Queue sightings of one item that ended without it going live."""
        return self._skips_by_sku().get(item_number, [])

    def get_sku_summaries(self, include_never_listed: bool = False) -> List[SkuSummary]:
        """
        One summary per item number, most recently seen first.

        ``include_never_listed`` adds items the store queued and then pulled
        without ever listing them.  Off by default, because every caller that
        counts SKUs or ranks deals means *things that were actually for sale* -
        counting a tool nobody could ever buy among the best discounts would
        be worse than not mentioning it at all.  The SKU History tab opts in
        and marks them.
        """
        grouped: Dict[str, List[Listing]] = {}
        for listing in self._all_listings():
            if not listing.item_number:
                continue
            grouped.setdefault(listing.item_number, []).append(listing)

        skips = self._skips_by_sku()
        summaries = []
        for item_number, listings in grouped.items():
            # _all_listings() is oldest-first, so this stays chronological.
            # Price fields all come from the priced listings, so "first price",
            # "last price" and "last discount" cannot end up describing
            # different listings from each other.
            priced = [l for l in listings if l.sale_price is not None]
            if not priced:
                continue
            prices = [l.sale_price for l in priced]

            live_times = [l.minutes_live for l in listings
                          if l.minutes_live is not None]
            discounts = [d for d in (l.discount_pct for l in listings) if d is not None]

            # Most recent known MSRP
            regular = next(
                (l.regular_price for l in reversed(listings) if l.regular_price),
                None
            )

            years = sorted({l.posted_at.year for l in listings if l.posted_at})

            sku_skips = skips.get(item_number, [])

            summaries.append(SkuSummary(
                item_number=item_number,
                name=listings[-1].name,
                times_listed=len(listings),
                first_seen=listings[0].posted_at,
                last_seen=listings[-1].posted_at,
                first_price=prices[0],
                last_price=prices[-1],
                lowest_price=min(prices),
                highest_price=max(prices),
                regular_price=regular,
                best_discount_pct=max(discounts) if discounts else None,
                last_discount_pct=priced[-1].discount_pct,
                avg_minutes_live=(sum(live_times) / len(live_times)) if live_times else None,
                total_minutes_live=sum(live_times) if live_times else 0,
                is_current=any(l.is_current for l in listings),
                years_seen=years,
                times_skipped=len(sku_skips),
                last_skipped_at=max((r.removed_at for r in sku_skips
                                     if r.removed_at), default=None),
            ))

        if include_never_listed:
            for item_number, sku_skips in skips.items():
                if item_number in grouped:
                    continue
                summary = self._never_listed_summary(item_number, sku_skips)
                if summary is not None:
                    summaries.append(summary)

        summaries.sort(key=lambda s: s.last_seen or datetime.min, reverse=True)
        return summaries

    @staticmethod
    def _never_listed_summary(item_number: str, skips: List) -> Optional[SkuSummary]:
        """
        Roll up an item that was only ever queued and pulled.

        Shaped like any other summary so the table can show it beside the
        rest, but every price on it came out of the queue feed - what the
        store was advertising while the item waited - and it was never seen
        selling at any of them.  times_listed is 0 and never_listed is True;
        between them nothing should be able to read this as a sale.
        """
        ordered = sorted(skips, key=lambda r: r.removed_at or datetime.min)
        priced = [r for r in ordered if r.sale_price is not None]
        if not priced:
            return None

        prices = [r.sale_price for r in priced]
        regular = next((r.regular_price for r in reversed(priced) if r.regular_price),
                       None)
        discounts = [d for d in (discount_pct(r.regular_price, r.sale_price)
                                 for r in priced) if d is not None]
        seen = [r.first_seen_at for r in ordered if r.first_seen_at]
        removed = [r.removed_at for r in ordered if r.removed_at]

        return SkuSummary(
            item_number=item_number,
            name=ordered[-1].name,
            times_listed=0,
            first_seen=min(seen) if seen else None,
            last_seen=max(removed) if removed else None,
            first_price=prices[0],
            last_price=prices[-1],
            lowest_price=min(prices),
            highest_price=max(prices),
            regular_price=regular,
            best_discount_pct=max(discounts) if discounts else None,
            last_discount_pct=discount_pct(priced[-1].regular_price,
                                           priced[-1].sale_price),
            avg_minutes_live=None,     # it was never live; there is no duration
            total_minutes_live=0,
            is_current=False,
            years_seen=sorted({d.year for d in removed}),
            times_skipped=len(skips),
            last_skipped_at=max(removed) if removed else None,
            never_listed=True,
        )

    def evaluate_price(self, item_number: str, sale_price: float,
                       regular_price: Optional[float] = None,
                       exclude_product_id: Optional[int] = None) -> PriceVerdict:
        """
        Compare a price against this SKU's own history.

        Pass exclude_product_id when the listing has already been written to
        the database, so the current listing is not compared against itself.
        """
        history = [
            l for l in self.get_listings_for_sku(item_number)
            if l.product_id != exclude_product_id and l.sale_price is not None
        ]

        if regular_price is None:
            regular_price = next((l.regular_price for l in history if l.regular_price), None)

        if not history:
            return PriceVerdict(
                item_number=item_number,
                sale_price=sale_price,
                is_first_sighting=True,
                is_best_ever=True,
                # Nothing to be lower than yet, so this is not a new low
                is_new_low=False,
                previous_best=None,
                previous_worst=None,
                times_listed_before=0,
                delta_vs_best=None,
                delta_pct_vs_best=None,
                discount_pct=discount_pct(regular_price, sale_price),
            )

        prices = [l.sale_price for l in history]
        best = min(prices)
        worst = max(prices)
        delta = sale_price - best

        return PriceVerdict(
            item_number=item_number,
            sale_price=sale_price,
            is_first_sighting=False,
            is_best_ever=sale_price <= best + PRICE_EPSILON,
            is_new_low=sale_price < best - PRICE_EPSILON,
            previous_best=best,
            previous_worst=worst,
            times_listed_before=len(history),
            delta_vs_best=delta,
            delta_pct_vs_best=(delta / best * 100.0) if best else None,
            discount_pct=discount_pct(regular_price, sale_price),
        )

    # ------------------------------------------------------------------
    # Catalog-wide statistics
    # ------------------------------------------------------------------
    def get_catalog_summary(self) -> dict:
        """Aggregate statistics across the whole tracked catalog."""
        listings = self._all_listings()
        summaries = self.get_sku_summaries()

        if not listings:
            return {
                'total_listings': 0,
                'rotation_listings': 0,
                'multi_item_listings': 0,
                'total_skus': 0,
                'repeat_skus': 0,
                'repeat_rate_pct': 0.0,
                'years_covered': [],
                'first_listing': None,
                'last_listing': None,
                'avg_discount_pct': None,
                'median_discount_pct': None,
                'max_discount_pct': None,
                'avg_minutes_live': None,
                'median_minutes_live': None,
                'listings_per_week': None,
                'price_bands': {},
                'type_counts': [],
                'busiest_weekday': None,
                'most_listed': [],
                'best_deals': [],
            }

        discounts = [d for d in (l.discount_pct for l in listings) if d is not None]
        dates = [l.posted_at for l in listings if l.posted_at]

        # Anything describing the store's rhythm - how long a listing stays up,
        # how often one appears, which day is busiest - is measured over the
        # one-at-a-time rotation only.  A sale that posts thirty items in one
        # minute and leaves them up for days would otherwise dominate all three
        # and make each of them describe the sale instead of the store.
        rotation = [l for l in listings if not l.is_multi_item]
        # `is not None`, never truthiness: minutes_live has three states and
        # 0 is one of them.  None means the duration is unknown - a listing
        # still running, or one closed out when the store stopped rotating.
        # 0 means known, and under a minute, which on this store is common:
        # items sell out that fast.  Dropping the 0s as falsy quietly deleted
        # the fastest sell-outs from the very average meant to describe them,
        # and left this figure disagreeing with the one on the Statistics tab.
        live_times = [l.minutes_live for l in rotation if l.minutes_live is not None]
        rotation_dates = [l.posted_at for l in rotation if l.posted_at]

        # Listing cadence
        listings_per_week = None
        if len(rotation_dates) >= 2:
            span_days = (max(rotation_dates) - min(rotation_dates)).total_seconds() / 86400
            if span_days >= 1:
                listings_per_week = len(rotation_dates) / (span_days / 7)

        # Price bands
        bands = {'Under $200': 0, '$200-500': 0, '$500-1000': 0, '$1000+': 0}
        for l in listings:
            price = l.sale_price or 0
            if price < 200:
                bands['Under $200'] += 1
            elif price < 500:
                bands['$200-500'] += 1
            elif price < 1000:
                bands['$500-1000'] += 1
            else:
                bands['$1000+'] += 1

        weekday_counts: Dict[int, int] = {}
        for d in rotation_dates:
            weekday_counts[d.weekday()] = weekday_counts.get(d.weekday(), 0) + 1
        busiest_weekday = None
        if weekday_counts:
            names = ['Monday', 'Tuesday', 'Wednesday', 'Thursday',
                     'Friday', 'Saturday', 'Sunday']
            busiest_weekday = names[max(weekday_counts, key=weekday_counts.get)]

        repeat_skus = [s for s in summaries if s.is_repeat]

        most_listed = sorted(summaries, key=lambda s: s.times_listed, reverse=True)[:5]
        best_deals = sorted(
            [s for s in summaries if s.best_discount_pct is not None],
            key=lambda s: s.best_discount_pct,
            reverse=True
        )[:5]

        return {
            'total_listings': len(listings),
            'rotation_listings': len(rotation),
            'multi_item_listings': len(listings) - len(rotation),
            'total_skus': len(summaries),
            'repeat_skus': len(repeat_skus),
            'repeat_rate_pct': (len(repeat_skus) / len(summaries) * 100.0) if summaries else 0.0,
            'years_covered': sorted({d.year for d in dates}),
            'first_listing': min(dates) if dates else None,
            'last_listing': max(dates) if dates else None,
            'avg_discount_pct': (sum(discounts) / len(discounts)) if discounts else None,
            'median_discount_pct': statistics.median(discounts) if discounts else None,
            'max_discount_pct': max(discounts) if discounts else None,
            'avg_minutes_live': (sum(live_times) / len(live_times)) if live_times else None,
            'median_minutes_live': statistics.median(live_times) if live_times else None,
            'listings_per_week': listings_per_week,
            'price_bands': bands,
            # What kinds of tool actually show up - counted over listings, so
            # a category that recurs weekly outweighs a one-off.
            'type_counts': summarize_types(listings),
            'busiest_weekday': busiest_weekday,
            'most_listed': most_listed,
            'best_deals': best_deals,
        }

    # ------------------------------------------------------------------
    # Look-ahead queue reliability
    # ------------------------------------------------------------------
    def get_queue_accuracy(self, collection_handle: str = 'oneanddone') -> dict:
        """
        Measure how well the look-ahead queue actually predicts what goes live.

        The queue is a plan: items get skipped, reordered, or sell out before
        they reach the front.  Rather than guessing at how much to trust it,
        this reports the observed record - how many queued items went live at
        all, and of those, how often the one at the front was next.

        Returns counts of zero everywhere until enough outcomes accumulate;
        'has_data' says whether the percentages mean anything yet.
        """
        try:
            outcomes = self.db_manager.get_queue_outcomes(collection_handle)
        except Exception as e:
            logger.error(f"Could not load queue outcomes: {e}")
            outcomes = []

        went_live = [o for o in outcomes if o.went_live_at is not None]
        skipped = [o for o in outcomes if o.was_skipped]
        resolved = len(went_live) + len(skipped)

        positions = [o.live_from_position for o in went_live
                     if o.live_from_position is not None]
        from_front = sum(1 for p in positions if p <= 1)

        return {
            'resolved': resolved,
            'went_live': len(went_live),
            'skipped': len(skipped),
            'skip_rate_pct': (len(skipped) / resolved * 100.0) if resolved else None,
            'front_of_queue_hits': from_front,
            'front_of_queue_pct': (from_front / len(positions) * 100.0) if positions else None,
            'median_position_before_live': statistics.median(positions) if positions else None,
            'max_position_before_live': max(positions) if positions else None,
            'has_data': resolved >= 5,
        }

    def describe_queue_confidence(self, collection_handle: str = 'oneanddone') -> str:
        """One-line, honest summary of how much the queue can be trusted."""
        stats = self.get_queue_accuracy(collection_handle)

        if not stats['resolved']:
            return ("No queue outcomes recorded yet - the ordering below is the store's "
                    "plan and has not been checked against reality on this machine.")

        if not stats['has_data']:
            return (f"Only {stats['resolved']} queue outcome(s) recorded so far - "
                    f"too few to judge how reliable the ordering is.")

        parts = []
        if stats['front_of_queue_pct'] is not None:
            parts.append(f"{stats['front_of_queue_pct']:.0f}% of items that went live "
                         f"came from the front of the queue")
        if stats['skip_rate_pct'] is not None:
            parts.append(f"{stats['skip_rate_pct']:.0f}% of queued items left "
                         f"without ever going live")

        return f"Based on {stats['resolved']} recorded outcomes: " + "; ".join(parts) + "."

    # Fewer past listings than this outlasting the current one, and a time
    # estimate would be a guess dressed up as a statistic.
    MIN_TIMING_SAMPLE = 5

    def predict_next(self, queue_rows: List, live_product=None,
                     collection_handle: str = 'oneanddone',
                     now: Optional[datetime] = None) -> Optional[dict]:
        """
        Which item goes live next, and roughly when - with the evidence for each.

        Replaces a predictor that only ever named the most common category in
        recent history.  The store publishes its own queue, so the front of it
        *is* the prediction; what is worth computing is how far to trust it:

          next / runner_up   the first two queued items behind the live one
          hit_rate_pct       how often the front of the queue really was next
          hits / sample      the counts behind that percentage
          minutes_live_so_far, remaining_low/mid/high
                             how much longer listings that had already
                             lasted this long went on for (25th / 50th / 75th
                             percentile) - None when the record is too thin
          outlasted_by       how many past listings lasted at least this long

        Timing uses rotation listings only: a sale leaves thirty items up for
        days, which says nothing about how long one rotation lasts.  Returns
        None when nothing is queued.
        """
        upcoming = sorted((r for r in queue_rows if (r.position or 0) > 0),
                          key=lambda r: r.position)
        if not upcoming:
            return None

        stats = self.get_queue_accuracy(collection_handle)
        sample = stats['went_live']
        result = {
            'next': upcoming[0],
            'runner_up': upcoming[1] if len(upcoming) > 1 else None,
            'hits': stats['front_of_queue_hits'],
            'sample': sample,
            'hit_rate_pct': stats['front_of_queue_pct'] if stats['has_data'] else None,
            'minutes_live_so_far': None,
            'outlasted_by': 0,
            'remaining_low': None,
            'remaining_mid': None,
            'remaining_high': None,
        }

        posted = getattr(live_product, 'posted_at', None)
        if live_product is None or posted is None or live_product.replaced_at is not None:
            return result

        elapsed = max(0.0, ((now or _utcnow()) - posted).total_seconds() / 60.0)
        result['minutes_live_so_far'] = elapsed

        durations = [l.minutes_live for l in self._all_listings()
                     if not l.is_multi_item and l.minutes_live is not None
                     and l.replaced_at is not None]
        remaining = sorted(d - elapsed for d in durations if d >= elapsed)
        result['outlasted_by'] = len(remaining)

        if len(remaining) >= self.MIN_TIMING_SAMPLE:
            low, mid, high = statistics.quantiles(remaining, n=4, method='inclusive')
            result.update(remaining_low=low, remaining_mid=mid, remaining_high=high)

        return result

    # ------------------------------------------------------------------
    # Printed catalog cross-reference
    # ------------------------------------------------------------------
    def describe_catalog_presence(self, item_number: str) -> Optional[str]:
        """
        One line on where an item sits in the printed catalogs.

        Recon sells refurbished stock, so an item that stopped being printed
        years ago is very likely a discontinued model - worth knowing before
        buying, and something the store itself does not tell you.  Returns None
        when no catalog index has been built, so callers can stay quiet rather
        than claim an item is absent when nothing was ever loaded.
        """
        try:
            history = self.db_manager.get_catalog_history(item_number)
            known_years = self.db_manager.get_catalog_years()
        except Exception as e:
            logger.error(f"Could not read catalog history for {item_number}: {e}")
            return None

        if not known_years:
            return None

        newest_edition = known_years[-1][0]

        if not history:
            return (f"Not found in any catalog {known_years[0][0]}-{newest_edition} "
                    f"- likely a discontinued model")

        years = [e.catalog_year for e in history]
        if years[-1] == newest_edition:
            return (f"In the current {newest_edition} catalog"
                    + (f" (and {len(years) - 1} earlier edition(s))" if len(years) > 1 else ""))

        return (f"Last printed in the {years[-1]} catalog "
                f"({len(years)} edition(s): {years[0]}-{years[-1]}) "
                f"- dropped from the current one")

    def compare_to_catalog(self, item_number: str,
                           sale_price: float) -> Optional[dict]:
        """
        Judge a price against Festool's own printed list price.

        The store sets its own "MSRP", so a discount measured against it is the
        store marking its own homework.  The catalog is an independent figure.
        Returns None when the item has no catalog price to compare with.
        """
        try:
            entry = self.db_manager.get_catalog_entry(item_number)
        except Exception as e:
            logger.error(f"Could not read catalog entry for {item_number}: {e}")
            return None

        if entry is None or entry.msrp_usd is None or not sale_price:
            return None

        off = entry.msrp_usd - sale_price
        return {
            'catalog_year': entry.catalog_year,
            'catalog_msrp': entry.msrp_usd,
            'confidence': entry.confidence,
            'savings': off,
            'discount_pct': (off / entry.msrp_usd * 100.0) if entry.msrp_usd else None,
        }

    def annotate_queue(self, queue_rows: List) -> List[dict]:
        """
        Attach price history context to queued items.

        This is the point of the look-ahead: knowing an item is coming is only
        useful alongside whether its price is any good.
        """
        annotated = []
        for row in queue_rows:
            entry = {'row': row, 'verdict': None, 'discount_pct': None}

            entry['discount_pct'] = discount_pct(row.regular_price, row.sale_price)

            if row.item_number and row.sale_price is not None:
                try:
                    entry['verdict'] = self.evaluate_price(
                        row.item_number, row.sale_price,
                        regular_price=row.regular_price
                    )
                except Exception as e:
                    logger.error(f"Could not evaluate queued item {row.item_number}: {e}")

            annotated.append(entry)
        return annotated

    def get_listings_by_month(self) -> List[tuple]:
        """[(YYYY-MM, count), ...] oldest first - listing activity over time."""
        counts: Dict[str, int] = {}
        for l in self._all_listings():
            if l.posted_at:
                key = l.posted_at.strftime('%Y-%m')
                counts[key] = counts.get(key, 0) + 1
        return sorted(counts.items())

    def export_sku_history(self, filepath: str) -> str:
        """Write the SKU rollup to a CSV file."""
        import csv

        # Never-listed items are included here - an export is a record, and
        # leaving out the near-misses would quietly answer a different
        # question than the one asked.  The two columns say which is which.
        summaries = self.get_sku_summaries(include_never_listed=True)
        with open(filepath, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow([
                'Item Number', 'Name', 'Times Listed', 'Times Queued And Pulled',
                'Ever Listed', 'First Seen', 'Last Seen',
                'First Price', 'Last Price', 'Lowest Price', 'Highest Price',
                'MSRP', 'Best Discount %', 'Last Discount %',
                'Avg Minutes Live', 'Currently Listed', 'Years Seen', 'Price Trend'
            ])
            for s in summaries:
                writer.writerow([
                    s.item_number, s.name, s.times_listed,
                    s.times_skipped, 'No' if s.never_listed else 'Yes',
                    s.first_seen.isoformat() if s.first_seen else '',
                    s.last_seen.isoformat() if s.last_seen else '',
                    f"{s.first_price:.2f}", f"{s.last_price:.2f}",
                    f"{s.lowest_price:.2f}", f"{s.highest_price:.2f}",
                    f"{s.regular_price:.2f}" if s.regular_price else '',
                    f"{s.best_discount_pct:.1f}" if s.best_discount_pct is not None else '',
                    f"{s.last_discount_pct:.1f}" if s.last_discount_pct is not None else '',
                    f"{s.avg_minutes_live:.0f}" if s.avg_minutes_live is not None else '',
                    'Yes' if s.is_current else 'No',
                    ' '.join(str(y) for y in s.years_seen),
                    s.price_trend,
                ])

        logger.info(f"Exported {len(summaries)} SKU summaries to {filepath}")
        return filepath
