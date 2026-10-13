"""Background monitoring service for festoolrecon.com product changes."""
import logging
import threading
import time
from typing import Optional, Callable
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED

from .collection_scraper import (
    DEFAULT_COLLECTION, CollectionScraper, assign_modes,
)
from .festool_scraper import FestoolScraper
from database.manager import DatabaseManager
from database.models import MODE_MULTI, MODE_SINGLE

logger = logging.getLogger(__name__)


class MonitorService:
    """Background service for monitoring product changes with validation."""
    
    def __init__(self, db_manager: DatabaseManager,
                 on_product_change: Optional[Callable] = None,
                 on_queue_change: Optional[Callable] = None,
                 on_new_collection: Optional[Callable] = None,
                 on_collection_change: Optional[Callable] = None,
                 on_mode_change: Optional[Callable] = None,
                 validation_checks: int = 3,
                 validation_delay: int = 2):
        """
        Initialize monitor with validation settings.

        Args:
            db_manager: Database manager instance
            on_product_change: Callback for product changes
            on_queue_change: Callback(sync_result) after the look-ahead queue is refreshed
            on_new_collection: Callback(list) when previously unseen collections appear
            on_collection_change: Callback(handle, sync) when a multi-item
                collection gains or loses items
            on_mode_change: Callback(changes) when the store switches between
                selling one item at a time and selling a whole collection
            validation_checks: Number of validation checks (default: 3)
            validation_delay: Seconds between validation checks (default: 2)
        """
        self.db_manager = db_manager
        # Don't store scraper - create fresh for each request
        self.on_product_change = on_product_change
        self.on_queue_change = on_queue_change
        self.on_new_collection = on_new_collection
        self.on_collection_change = on_collection_change
        self.on_mode_change = on_mode_change
        self.last_product_id = None
        self.last_item_number = None
        self.is_running = False
        self._stopping = False
        self._shutdown = False
        self._check_lock = threading.Lock()

        # Validation settings
        self.validation_checks = validation_checks
        self.validation_delay = validation_delay

        # Seed the "last seen" state from the database straight away.  If this
        # only happened in start(), a manual check before monitoring was ever
        # started would look like the very first product ever seen and skip
        # false-positive validation entirely.
        self._refresh_last_seen()

        self.scheduler = self._create_scheduler()

    def _create_scheduler(self) -> BackgroundScheduler:
        """Create a scheduler instance with the shared job defaults."""
        scheduler = BackgroundScheduler(
            job_defaults={
                'coalesce': True,
                'max_instances': 1,
                'misfire_grace_time': 60
            }
        )
        scheduler.add_listener(
            self._job_error_listener,
            EVENT_JOB_ERROR | EVENT_JOB_EXECUTED
        )
        return scheduler

    def survey_store(self, page_scraper: Optional[FestoolScraper] = None) -> dict:
        """
        One read of what the store is doing, and act on everything except the
        look-ahead queue.

        Establishes, in order:

        * which collections are published (and alerts on any new one);
        * what each of them is currently offering;
        * how the store is running each - one item at a time, or the whole
          collection at once; and
        * for the multi-item ones, which listings have appeared or ended.

        The queue sync is deliberately *not* done here.  It has to run after
        the rotation check, not before: syncing first would move the incoming
        item to position 0 and clamp its best_position, so the look-ahead
        accuracy figures would record every item as having gone live from the
        front of the queue.  refresh_look_ahead() is the version that does
        both, in that order.

        Never raises - a failure here must not stop the rotation check.
        """
        result = {
            'collections': [], 'new_collections': [], 'feeds': {},
            'modes': {}, 'mode_changes': {}, 'multi': {},
            'single_handle': None, 'storefront': None,
            'queue': [], 'sync': None, 'rotation_closed': [], 'retired': {},
        }

        try:
            scraper = CollectionScraper()
            page_scraper = page_scraper or FestoolScraper()

            # Watch for special-sale collections appearing alongside the usual one
            collections = scraper.fetch_collections()
            if collections:
                result['collections'] = collections
                new_collections = self.db_manager.sync_collections(collections)
                unexpected = [c for c in new_collections if c.handle != DEFAULT_COLLECTION]
                if unexpected:
                    result['new_collections'] = unexpected
                    for collection in unexpected:
                        logger.warning(
                            f"Unexpected collection published: '{collection.handle}' "
                            f"({collection.title}) - possible special sale"
                        )
                    if self.on_new_collection and not self._stopping:
                        try:
                            self.on_new_collection(unexpected)
                        except Exception as e:
                            logger.error(f"Error in new-collection callback: {e}", exc_info=True)
                handles = [c.handle for c in collections]

                # A sale can also end by the collection being withdrawn
                # outright.  Nothing else notices: an unpublished collection
                # is in no feed, so its listings are never reconciled again
                # and would go on claiming to be on offer indefinitely.  The
                # index was read successfully to get here, so a handle missing
                # from it really is gone rather than merely unreadable.
                for handle, mode in self.db_manager.get_collection_modes().items():
                    if mode == MODE_MULTI and handle not in handles:
                        retired = self.db_manager.close_collection_listings(handle)
                        if retired:
                            result['retired'][handle] = retired
            else:
                # The index failed rather than emptied - sync_collections was
                # skipped above for the same reason.  Carry on with the
                # collections already known to be active so one bad request
                # cannot blind the whole check.
                handles = [row.handle for row in
                           self.db_manager.get_collection_sightings(active_only=True)]
                logger.warning(
                    f"Collection index unavailable - falling back to the "
                    f"{len(handles)} collection(s) last known to be active"
                )

            if not handles:
                logger.error("No collections to read - nothing to monitor")
                return result

            # What each collection is offering, and what the front page is doing
            result['feeds'] = feeds = scraper.fetch_feeds(handles)
            result['storefront'] = storefront = page_scraper.read_storefront()

            previous_modes = self.db_manager.get_collection_modes()
            result['modes'] = modes = assign_modes(storefront, feeds, previous_modes)

            unknown = [h for h in feeds if h not in modes]
            if unknown:
                logger.info(
                    f"Cannot tell how the store is running {unknown} - recording "
                    f"nothing from them until it is clear"
                )

            live_counts = {
                handle: (1 if mode == MODE_SINGLE else len(feeds.get(handle, [])))
                for handle, mode in modes.items()
            }
            result['mode_changes'] = self.db_manager.record_collection_modes(
                modes, live_counts
            )
            if result['mode_changes'] and self.on_mode_change and not self._stopping:
                try:
                    self.on_mode_change(result['mode_changes'])
                except Exception as e:
                    logger.error(f"Error in mode-change callback: {e}", exc_info=True)

            # Collections where everything is on offer at once
            for handle, mode in modes.items():
                if mode != MODE_MULTI:
                    continue
                sync = self.db_manager.sync_live_collection(feeds.get(handle), handle)
                result['multi'][handle] = sync

                if (sync['added'] or sync['removed']) and not self._stopping:
                    if self.on_collection_change:
                        try:
                            self.on_collection_change(handle, sync)
                        except Exception as e:
                            logger.error(
                                f"Error in collection-change callback: {e}", exc_info=True
                            )

            result['single_handle'] = next(
                (h for h, mode in modes.items() if mode == MODE_SINGLE), None
            )

            # Positive evidence that nothing is rotating: this survey read the
            # front page and found the store selling a collection outright,
            # with no collection being rotated.  Whatever was last up has
            # ended, so stop calling it live.  An unreadable front page
            # produces no modes at all and so never reaches this.
            if result['single_handle'] is None and MODE_MULTI in modes.values():
                result['rotation_closed'] = self.db_manager.close_rotation_listing()
                if result['rotation_closed']:
                    self.last_item_number = None
                    self.last_product_id = None

        except Exception as e:
            logger.error(f"Store survey failed: {e}", exc_info=True)

        return result

    def sync_look_ahead_queue(self, survey: dict) -> dict:
        """
        Sync the look-ahead queue for whichever collection is running one item
        at a time, reusing the feed the survey already fetched.

        Does nothing when the store is not running a rotation at all: there is
        no queue to speak of when every item is on offer together.
        """
        handle = survey.get('single_handle')
        if not handle:
            logger.debug("No one-at-a-time collection right now - no queue to sync")
            return survey

        queue = survey.get('feeds', {}).get(handle) or []
        if not queue:
            logger.warning(
                f"Look-ahead queue for '{handle}' came back empty - skipping sync"
            )
            return survey

        try:
            survey['queue'] = queue
            survey['sync'] = self.db_manager.sync_queue(queue, handle)

            upcoming = [p for p in queue if not p.is_live]
            logger.info(
                f"Look-ahead: {len(upcoming)} item(s) queued behind the live product"
            )

            if self.on_queue_change and not self._stopping:
                try:
                    self.on_queue_change(survey)
                except Exception as e:
                    logger.error(f"Error in queue-change callback: {e}", exc_info=True)

        except Exception as e:
            logger.error(f"Look-ahead queue sync failed: {e}", exc_info=True)

        return survey

    def refresh_look_ahead(self, page_scraper: Optional[FestoolScraper] = None) -> dict:
        """
        Read the store and sync everything, look-ahead queue included.

        Safe to call on its own - this is what the UI's refresh button uses.
        Failures never propagate, so a check can call it without guarding.
        """
        return self.sync_look_ahead_queue(self.survey_store(page_scraper))

    def _refresh_last_seen(self):
        """Load the most recent product from the database into tracking state."""
        try:
            latest = self.db_manager.get_latest_product()
            if latest:
                self.last_product_id = latest.id
                self.last_item_number = latest.item_number
                logger.debug(f"Tracking last known product: {self.last_item_number}")
        except Exception as e:
            logger.error(f"Could not load last known product: {e}")

    def start(self, interval_minutes: int = 5):
        """Start monitoring with specified interval."""
        if self.is_running:
            logger.warning("Monitor already running")
            return
        
        try:
            # Get latest product to track changes
            self._refresh_last_seen()

            # A scheduler that has been shut down cannot be reused
            if self._shutdown:
                logger.info("Recreating scheduler after previous shutdown")
                self.scheduler = self._create_scheduler()
                self._shutdown = False

            # Remove any existing job
            if self.scheduler.get_job('product_monitor'):
                self.scheduler.remove_job('product_monitor')

            # Add the monitoring job
            self.scheduler.add_job(
                self._check_for_changes,
                trigger=IntervalTrigger(minutes=interval_minutes),
                id='product_monitor',
                replace_existing=True,
                name='Product Monitor Job'
            )
            
            # Start scheduler if not already started
            if not self.scheduler.running:
                self.scheduler.start()
            
            self._stopping = False
            self.is_running = True
            logger.info(f"Monitor started with {interval_minutes} minute interval (validation: {self.validation_checks} checks)")
            
        except Exception as e:
            logger.error(f"Error starting monitor: {e}", exc_info=True)
            raise
    
    def stop(self):
        """Stop monitoring."""
        if not self.is_running:
            return
        
        self._stopping = True
        logger.info("Stopping monitor...")
        
        try:
            # Pause the scheduler to prevent new job submissions
            if self.scheduler and self.scheduler.running:
                self.scheduler.pause()
                
                # Wait a tiny bit for any running jobs to finish
                time.sleep(0.2)
                
                # Remove the monitoring job
                try:
                    self.scheduler.remove_job('product_monitor')
                except:
                    pass  # Job might already be gone
                
                # Resume scheduler (but job is gone, so nothing will run)
                self.scheduler.resume()
            
            self.is_running = False
            logger.info("Monitor stopped")
            
        except Exception as e:
            logger.warning(f"Error during monitor stop: {e}")
            self.is_running = False
    
    def check_now(self) -> Optional[dict]:
        """Manually trigger a check."""
        return self._check_for_changes()
    
    def update_interval(self, interval_minutes: int):
        """Update check interval."""
        if self.is_running and self.scheduler and self.scheduler.running:
            try:
                # Reschedule the job with new interval
                self.scheduler.reschedule_job(
                    'product_monitor',
                    trigger=IntervalTrigger(minutes=interval_minutes)
                )
                logger.info(f"Interval updated to {interval_minutes} minutes")
            except Exception as e:
                logger.error(f"Error updating interval: {e}")
                # Fall back to restart
                self.stop()
                time.sleep(0.3)
                self.start(interval_minutes)
    
    def _job_error_listener(self, event):
        """Listen for job errors."""
        if event.exception:
            logger.error(f"Job error: {event.exception}")
    
    def _validate_product_change(self, new_item_number: str,
                                 force: bool = False) -> bool:
        """
        Validate that a product change is real by checking multiple times.
        Creates fresh scraper for each validation check to avoid caching.

        ``force`` says the caller already knows this is a new listing even
        though the item number has not moved - the store stopped rotating and
        has started again.  It skips the "same number, no change" shortcut but
        keeps the re-confirmations, which matter more than usual coming out of
        a sale with a front-page cache that has just been rewritten.

        Returns True if change is confirmed, False if it's a false positive.
        """
        if not self.last_item_number:
            logger.info("No previously known product - accepting without re-validation")
            return True  # Nothing to compare against

        if new_item_number == self.last_item_number and not force:
            return False  # No change

        # The initial scrape counts as the first observation; re-confirm the
        # rest to weed out CDN/cache-induced false positives.
        reconfirmations = max(0, self.validation_checks - 1)

        logger.info(f"Product change detected: {self.last_item_number} -> {new_item_number}")
        logger.info(
            f"Performing {reconfirmations} re-check(s) every {self.validation_delay}s "
            f"({self.validation_checks} observations total)..."
        )

        # Perform multiple validation checks with FRESH scrapers
        for check_num in range(1, reconfirmations + 1):
            time.sleep(self.validation_delay)

            try:
                # Create fresh scraper to avoid any caching
                validation_scraper = FestoolScraper()
                validation_product = validation_scraper.fetch_current_product()

                if not validation_product:
                    logger.warning(f"Validation check #{check_num} failed - no product found")
                    logger.warning("False positive: Scraping failed during validation")
                    return False

                validation_item = validation_product.get('item_number')

                if validation_item != new_item_number:
                    logger.warning(
                        f"Validation check #{check_num} FAILED: "
                        f"Returned {validation_item} instead of {new_item_number}"
                    )
                    logger.warning(
                        f"False positive detected and ignored. "
                        f"Keeping current product: {self.last_item_number}"
                    )
                    return False

                logger.info(f"Validation check #{check_num}: Confirmed {new_item_number} ✓")

            except Exception as e:
                logger.error(f"Error during validation check #{check_num}: {e}")
                logger.warning("False positive: Validation failed due to error")
                return False

        # All validation checks passed
        logger.info(
            f"✓ Product change VALIDATED: {self.validation_checks} observation(s) "
            f"confirmed {new_item_number}"
        )
        logger.info("This is a REAL product change - proceeding with notification")
        return True
    
    def _check_for_changes(self) -> Optional[dict]:
        """
        One monitoring pass over the store.

        Reads what the store is doing first, because the answer decides what
        the rest of the check even means.  When a collection is being sold all
        at once there is no rotation to watch and no queue behind anything -
        survey_store() has already recorded what came and went - so the
        single-product path is skipped rather than left to fail its way to
        None on every check.
        """
        # Don't run if we're stopping
        if self._stopping:
            logger.debug("Skipping check - monitor is stopping")
            return None

        # A scheduled run and a manual "Check Now" can overlap; without this
        # both could validate and insert the same product twice.
        if not self._check_lock.acquire(blocking=False):
            logger.info("A check is already in progress - skipping this one")
            return None

        try:
            # Create FRESH scraper instance to avoid any session/cache reuse.
            # It caches the front page for its own lifetime, so the storefront
            # read and the product scrape below see the same copy.
            scraper = FestoolScraper()

            survey = self.survey_store(scraper)
            single_handle = survey.get('single_handle')

            if not single_handle:
                multi = [h for h, m in survey.get('modes', {}).items()
                         if m == MODE_MULTI]
                if multi:
                    logger.info(
                        f"Store is selling {multi} all at once - no single live "
                        f"product to track this check"
                    )
                else:
                    logger.warning(
                        "Could not establish how the store is selling anything "
                        "right now - recording nothing this check"
                    )
                return None

            # Read the live product out of the collection the store is actually
            # rotating, whatever it is called this week.
            scraper.COLLECTION = single_handle
            current_product = scraper.fetch_current_product()

            if not current_product:
                logger.warning("No product found on page")
                self.sync_look_ahead_queue(survey)
                return None

            current_item_number = current_product.get('item_number')

            # Validate that we have required fields
            if not current_item_number:
                logger.error(f"Product missing item number: {current_product.get('name')}")
                logger.error("Cannot track product without item number - skipping this check")
                return None

            # Check if this is a new product
            latest = self.db_manager.get_latest_product()

            # Keep tracking state in step with the database, so validation
            # always compares against what we really saw last.
            if latest:
                self.last_product_id = latest.id
                self.last_item_number = latest.item_number

            # The store has started rotating again after a sale.  The last
            # rotation listing was closed out when it stopped, so this is a
            # fresh listing even if the store happens to resume with the same
            # item - and it must not be dismissed as "no change".
            resuming = latest is not None and latest.replaced_at is not None
            if resuming:
                logger.info(
                    f"Store is rotating again - {current_item_number} is a new "
                    f"listing, not a continuation of {latest.item_number}"
                )

            if not latest or resuming or latest.item_number != current_item_number:
                # Potential change detected - validate it
                if not self._validate_product_change(current_item_number,
                                                     force=resuming):
                    logger.info(f"False positive ignored. Current product remains: {self.last_item_number}")
                    return None

                # Change validated - proceed
                logger.info(f"CONFIRMED new product: {current_product['name']}")

                # Mark previous product as replaced
                if latest:
                    self.db_manager.mark_product_replaced(latest.id)

                # The front page does not carry the store's product type, but
                # the collection feed does - and the live product is resolved
                # from that feed, so it usually arrives with the scrape.  The
                # queue table is the fallback for a listing that was recorded
                # from the front page alone; inference from the title backs up
                # both.
                raw_type = current_product.get('product_type')
                if not raw_type:
                    try:
                        raw_type = self.db_manager.get_queue_product_type(
                            current_item_number, single_handle
                        )
                    except Exception as e:
                        logger.debug(f"Could not read queued product type: {e}")

                # Add new product
                new_product = self.db_manager.add_product(
                    item_number=current_product['item_number'],
                    name=current_product['name'],
                    sale_price=current_product['sale_price'],
                    regular_price=current_product.get('regular_price'),
                    description=current_product.get('description'),
                    image_url=current_product.get('image_url'),
                    raw_html=current_product.get('raw_html'),
                    product_type=raw_type,
                    collection_handle=single_handle,
                    handle=current_product.get('handle')
                )

                self.last_product_id = new_product.id
                self.last_item_number = new_product.item_number

                # Record which queue position this item was holding, so the
                # look-ahead's real hit rate can be measured rather than assumed
                try:
                    self.db_manager.mark_queue_item_live(
                        current_item_number, single_handle
                    )
                except Exception as e:
                    logger.error(f"Could not record queue outcome: {e}")

                # Refresh the queue now that the front of it has moved on.
                # Only the queue: re-surveying here would read the front page
                # a second time with a fresh cache, and being handed a
                # different copy of it mid-check could flip the collection's
                # mode on the strength of a stale page.
                try:
                    fresh_queue = CollectionScraper().fetch_queue(single_handle)
                    if fresh_queue:
                        survey.setdefault('feeds', {})[single_handle] = fresh_queue
                except Exception as e:
                    logger.error(f"Could not re-read the queue after rotation: {e}")

                self.sync_look_ahead_queue(survey)

                # Trigger callback if provided (don't run during shutdown)
                if self.on_product_change and not self._stopping:
                    try:
                        self.on_product_change(new_product)
                    except Exception as e:
                        logger.error(f"Error in product change callback: {e}", exc_info=True)

                return current_product
            else:
                logger.debug("No product change detected")
                # Still sync the look-ahead: the queue behind the live product
                # changes even when the live product does not.  The survey
                # already has the feed, so this costs no extra requests.
                self.sync_look_ahead_queue(survey)
                return None

        except Exception as e:
            logger.error(f"Error checking for changes: {e}", exc_info=True)
            return None
        finally:
            self._check_lock.release()
    
    def shutdown(self):
        """Completely shutdown the scheduler (called on app exit)."""
        if self._shutdown:
            return

        logger.info("Shutting down monitor service...")

        self._stopping = True
        self.is_running = False

        try:
            if self.scheduler:
                # Remove all jobs first
                if self.scheduler.running:
                    self.scheduler.remove_all_jobs()

                    # Wait a moment
                    time.sleep(0.2)

                    self.scheduler.shutdown(wait=False)

        except Exception as e:
            logger.warning(f"Error during scheduler shutdown: {e}")

        self._shutdown = True
        logger.info("Monitor service shut down")