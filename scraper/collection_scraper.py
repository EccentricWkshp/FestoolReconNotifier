"""
Look-ahead scraper for the Festool Recon collections.

The store front page shows one product at a time, but the collection behind it
(https://www.festoolrecon.com/collections/oneanddone) exposes the whole ordered
queue.  Position 0 of that queue is whatever is currently live on the home
page, so positions 1+ are the items lined up behind it.

Two important caveats, both of which the rest of the app has to respect:

1. The queue is a *plan*, not a schedule.  Items get skipped, swapped out, or
   sell out almost immediately, so a queued item may never appear as the live
   product.  Nothing here should be presented as a certainty - see
   QueueObservation accuracy tracking in the database layer for the empirical
   hit rate.

2. The store keeps a placeholder product ("Wow, that went fast!!!", SKU 999999,
   product_type "Placeholder") in the collection.  It is filler shown when
   stock is gone, not a real upcoming tool, and is flagged rather than listed.

3. Sold-out items linger in the feed.  Shopify serves these JSON endpoints
   from a server-side page cache whose membership can lag the real store by
   hours, so the front of the list is often several products that have already
   been and gone.  Each entry's ``available`` flag stays accurate though, so
   sold-out entries are filtered out *before* positions are assigned - that is
   what makes "position 0 is live" true rather than merely hoped for.

4. The store does not always sell one item at a time.  When it opens a sale
   collection it fronts the *collection* instead of a single product, and
   every available item in it is on offer simultaneously.  A collection is
   therefore read in one of two modes (MODE_SINGLE / MODE_MULTI), decided by
   what the front page is doing rather than by anything in the feed - see
   Storefront below.  Item counts cannot make that call: a one-at-a-time
   collection lists dozens of available items too, because that is its queue.

Shopify's JSON endpoints are used in preference to HTML scraping: they are
ordered identically to the rendered page, are far cheaper to parse, and give us
SKU, compare-at price, stock and product type directly.
"""
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import requests

from database.models import MODE_MULTI, MODE_SINGLE

logger = logging.getLogger(__name__)

# The collection the store ran on its own for years.  It is a starting point
# and a fallback for the queue APIs, never an assumption: the store is free to
# rename it or replace it, so which collection is live and how it is being run
# are both worked out from what the store is currently doing.
DEFAULT_COLLECTION = 'oneanddone'

# Store filler that stands in for sold-out stock
PLACEHOLDER_TYPE = 'placeholder'
PLACEHOLDER_SKUS = {'999999'}


@dataclass
class CollectionInfo:
    """A collection published on the store."""
    handle: str
    title: str
    products_count: Optional[int] = None
    published_at: Optional[str] = None
    updated_at: Optional[str] = None

    @property
    def is_default(self) -> bool:
        return self.handle == DEFAULT_COLLECTION

    @property
    def url(self) -> str:
        return f"https://www.festoolrecon.com/collections/{self.handle}"


@dataclass
class QueuedProduct:
    """One entry in a collection's ordered queue."""
    position: int
    item_number: Optional[str]
    name: str
    handle: str
    sale_price: Optional[float]
    regular_price: Optional[float]
    product_type: Optional[str]
    image_url: Optional[str]
    collection_handle: str = DEFAULT_COLLECTION
    available: bool = True

    @property
    def is_placeholder(self) -> bool:
        """True for the store's sold-out filler entry."""
        if (self.product_type or '').strip().lower() == PLACEHOLDER_TYPE:
            return True
        if self.item_number in PLACEHOLDER_SKUS:
            return True
        # The filler is listed at a nominal price
        return self.sale_price is not None and self.sale_price < 1.0

    @property
    def is_live(self) -> bool:
        """Position 0 is whatever the front page is currently showing."""
        return self.position == 0

    @property
    def discount_pct(self) -> Optional[float]:
        if not self.regular_price or not self.sale_price:
            return None
        if self.regular_price <= 0 or self.sale_price > self.regular_price:
            return None
        return (self.regular_price - self.sale_price) / self.regular_price * 100.0

    @property
    def url(self) -> str:
        return (f"https://www.festoolrecon.com/collections/"
                f"{self.collection_handle}/products/{self.handle}")


@dataclass
class Storefront:
    """
    What the store's front page is doing right now.

    This is the only thing that reliably separates the two selling modes, and
    the difference is visible in the markup rather than in any feed:

    * One at a time - the front page renders the product template for whatever
      item is up (``shopify-section-product-template``) and links to no
      collection at all.
    * Everything at once - there is no product template; the page is a set of
      collection cards linking to the collections being promoted.

    ``readable`` exists to keep "the front page says no single product is
    fronted" apart from "the front page could not be read".  Collapsing those
    two would be actively dangerous: a failed fetch would flip every published
    collection into multi-item mode and record its entire contents as brand
    new listings.  An unreadable front page yields no modes at all, and the
    caller keeps whatever it knew before.
    """
    readable: bool = False
    fronted_handle: Optional[str] = None
    fronted_item_number: Optional[str] = None
    featured_collections: List[str] = field(default_factory=list)

    @property
    def fronts_single_product(self) -> bool:
        """The store is showing one product on the front page."""
        return self.readable and bool(self.fronted_handle)

    def __repr__(self):
        if not self.readable:
            return "<Storefront(unreadable)>"
        if self.fronted_handle:
            return f"<Storefront(product={self.fronted_handle})>"
        return f"<Storefront(collections={self.featured_collections})>"


def assign_modes(storefront: Storefront,
                 feeds: Dict[str, List[QueuedProduct]],
                 previous_modes: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """
    Work out how the store is running each published collection.

    ``feeds`` maps a collection handle to its available items; ``previous_modes``
    is what was recorded last time, used only to hold a known answer steady
    when the evidence is momentarily missing.

    Returns handle -> MODE_SINGLE / MODE_MULTI, leaving out any collection the
    evidence does not cover.  A handle that is absent means "unknown", which
    callers must treat as "record nothing from it" rather than picking a side.

    Deliberately never keyed on a collection's name.  The store renames and
    replaces collections, and the one it had run for years went empty the day
    it opened a sale; anything that recognised a particular handle would have
    been wrong that same afternoon.
    """
    previous_modes = previous_modes or {}
    modes: Dict[str, str] = {}

    if not storefront.readable:
        logger.debug("Front page unreadable - leaving every collection mode as it was")
        return modes

    single_handle: Optional[str] = None

    if storefront.fronts_single_product:
        # Which collection is the fronted product coming out of?
        for handle, items in feeds.items():
            if any(item.handle == storefront.fronted_handle for item in items):
                single_handle = handle
                break

        if single_handle is None:
            # The feeds could not place it - most likely a stale copy that has
            # already dropped the item.  The store is plainly still selling one
            # at a time, so hold the last known single-item collection rather
            # than letting it fall through and be read as multi-item.
            single_handle = next(
                (h for h, mode in previous_modes.items() if mode == MODE_SINGLE), None
            )
            if single_handle is None and len(feeds) == 1:
                single_handle = next(iter(feeds))
            if single_handle is not None:
                logger.info(
                    f"Front page is fronting '{storefront.fronted_handle}', which is "
                    f"in no published feed - keeping '{single_handle}' as the "
                    f"one-at-a-time collection"
                )

    for handle in feeds:
        if handle == single_handle:
            modes[handle] = MODE_SINGLE
        elif handle in storefront.featured_collections:
            modes[handle] = MODE_MULTI
        elif storefront.fronts_single_product:
            # The store is fronting one product and is not promoting this
            # collection.  Nothing here says what it is, so say nothing.
            logger.debug(f"No evidence for how '{handle}' is being run - leaving unknown")
        elif previous_modes.get(handle):
            modes[handle] = previous_modes[handle]

    return modes


class CollectionScraper:
    """Reads the collection index and the ordered product queue."""

    BASE_URL = "https://www.festoolrecon.com"

    def __init__(self, timeout: int = 30):
        self.timeout = timeout

    def _headers(self) -> Dict[str, str]:
        return {
            'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                           'AppleWebKit/537.36 (KHTML, like Gecko) '
                           'Chrome/141.0.0.0 Safari/537.36'),
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'en-US,en;q=0.9',
            'Cache-Control': 'no-cache',
            'Pragma': 'no-cache',
            'Referer': f'{self.BASE_URL}/',
        }

    # ------------------------------------------------------------------
    def fetch_collections(self) -> List[CollectionInfo]:
        """
        List the published collections.

        Normally this is just 'oneanddone'; anything else is worth flagging as
        a possible special sale.  Returns an empty list on failure so callers
        do not mistake a network error for "the collection disappeared".
        """
        url = f"{self.BASE_URL}/collections.json"
        try:
            response = requests.get(url, headers=self._headers(), timeout=self.timeout)
            response.raise_for_status()
            payload = response.json()
        except requests.RequestException as e:
            logger.error(f"Could not fetch collection index: {e}")
            return []
        except ValueError as e:
            logger.error(f"Collection index was not valid JSON: {e}")
            return []

        collections = []
        for entry in payload.get('collections', []):
            handle = entry.get('handle')
            if not handle:
                continue
            collections.append(CollectionInfo(
                handle=handle,
                title=entry.get('title') or handle,
                products_count=entry.get('products_count'),
                published_at=entry.get('published_at'),
                updated_at=entry.get('updated_at'),
            ))

        logger.debug(f"Found {len(collections)} collection(s): "
                     f"{[c.handle for c in collections]}")
        return collections

    def fetch_queue(self, handle: str = DEFAULT_COLLECTION,
                    include_placeholders: bool = False,
                    include_sold_out: bool = False) -> List[QueuedProduct]:
        """
        Fetch a collection's ordered queue.

        Position 0 is the product currently on the front page.  Returns an
        empty list on failure.

        Two kinds of entry are dropped before positions are handed out, so
        that position 0 really is the live product:

        * the store's sold-out filler entry, and
        * items that have already sold out but are still listed.

        That second filter is what keeps this honest while Shopify is serving
        a stale copy of the feed.  Membership of the cached list can lag the
        real store by hours, but each entry's ``available`` flag stays
        accurate - so the first *available* item is the live one whichever
        copy we are handed.  Numbering before filtering (as this used to do)
        left the positions pointing at items that were already gone.
        """
        products = self._fetch_all_pages(handle)
        if not products:
            return []
        return self._build_queue(products, handle, include_placeholders, include_sold_out)

    def _build_queue(self, products: List[dict], handle: str,
                     include_placeholders: bool = False,
                     include_sold_out: bool = False) -> List[QueuedProduct]:
        """Turn raw feed entries into positioned items, fillers dropped first."""
        queue: List[QueuedProduct] = []
        for entry in products:
            variant = (entry.get('variants') or [{}])[0]

            sale_price = _to_float(variant.get('price'))
            regular_price = _to_float(variant.get('compare_at_price'))

            images = entry.get('images') or []
            image_url = images[0].get('src') if images else None

            item = QueuedProduct(
                position=len(queue),
                item_number=str(variant['sku']).strip() if variant.get('sku') else None,
                name=(entry.get('title') or '').strip(),
                handle=entry.get('handle') or '',
                sale_price=sale_price,
                regular_price=regular_price,
                product_type=entry.get('product_type'),
                image_url=image_url,
                collection_handle=handle,
                available=bool(variant.get('available', True)),
            )

            if item.is_placeholder and not include_placeholders:
                logger.debug(f"Skipping store placeholder entry: {item.name}")
                continue

            if not item.available and not include_sold_out:
                logger.debug(
                    f"Skipping sold-out entry still listed in '{handle}': "
                    f"{item.name} ({item.item_number})"
                )
                continue

            queue.append(item)

        logger.info(f"Collection '{handle}': {len(queue)} real product(s) listed")
        return queue

    def _fetch_all_pages(self, handle: str) -> Optional[List[dict]]:
        """
        Page through the collection feed until it runs dry.

        Returns None when the very first page could not be read, and a
        (possibly partial) list otherwise.  That distinction matters to
        fetch_feeds(): an unreadable collection has to stay out of the picture
        entirely, because an empty list there would be taken as "the sale is
        over" and retire every listing in it.
        """
        all_products: List[dict] = []
        page = 1

        while page <= 10:  # hard stop; the queue is nowhere near this long
            url = (f"{self.BASE_URL}/collections/{handle}/products.json"
                   f"?limit=250&page={page}")
            try:
                response = requests.get(url, headers=self._headers(), timeout=self.timeout)
                response.raise_for_status()
                products = response.json().get('products', [])
            except requests.RequestException as e:
                logger.error(f"Could not fetch queue for '{handle}' page {page}: {e}")
                return all_products or None
            except ValueError as e:
                logger.error(f"Queue feed for '{handle}' was not valid JSON: {e}")
                return all_products or None

            if not products:
                break

            all_products.extend(products)
            if len(products) < 250:
                break
            page += 1

        return all_products

    def fetch_upcoming(self, handle: str = DEFAULT_COLLECTION) -> List[QueuedProduct]:
        """
        Just the items lined up behind the live one (positions 1+).

        Only meaningful for a MODE_SINGLE collection: it is the queue behind
        the fronted product.  These are candidates, not guarantees - see the
        module docstring.
        """
        return [p for p in self.fetch_queue(handle) if not p.is_live]

    def fetch_live_items(self, handle: str) -> List[QueuedProduct]:
        """
        Everything on offer at once in a multi-item collection.

        Same feed and the same filtering as fetch_queue() - the difference is
        entirely in what the result means.  Here there is no queue and nothing
        is waiting its turn: every entry is buyable right now, and ``position``
        is only the order the store chose to display them in.  In particular
        ``QueuedProduct.is_live`` is about position 0 and says nothing useful
        in this mode; do not read it here.

        Returns an empty list on failure, which callers must not confuse with
        the sale having ended.
        """
        items = self.fetch_queue(handle)
        logger.info(f"Collection '{handle}': {len(items)} item(s) on offer at once")
        return items

    def fetch_feeds(self, handles: List[str]) -> Dict[str, List[QueuedProduct]]:
        """
        Available items for each of several collections, keyed by handle.

        A collection whose feed could not be read is left out entirely rather
        than mapped to an empty list, so that a network failure cannot be read
        as "this collection is empty now".
        """
        feeds: Dict[str, List[QueuedProduct]] = {}
        for handle in handles:
            products = self._fetch_all_pages(handle)
            if products is None:
                continue
            feeds[handle] = self._build_queue(products, handle)
        return feeds


def _to_float(value) -> Optional[float]:
    """Shopify returns prices as strings; missing values as None."""
    if value in (None, ''):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
