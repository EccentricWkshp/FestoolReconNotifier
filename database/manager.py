"""Database manager with connection pooling and backup functionality."""
import os
import shutil
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
from typing import List, Optional
from sqlalchemy import create_engine, event, or_
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.pool import StaticPool
import bcrypt
import logging

from .models import (
    Base, Product, User, UserSettings, Watchlist, Exclusion,
    NotificationLog, Prediction, DatabaseBackup, QueueObservation,
    CollectionSighting, CatalogEntry, MODE_MULTI, MODE_SINGLE, utcnow
)
from analytics.product_types import type_of

logger = logging.getLogger(__name__)


def _price_moved(old: Optional[float], new: Optional[float]) -> bool:
    """True when two prices differ by a real amount rather than rounding."""
    if new is None or old is None:
        return False
    return abs(old - new) >= 0.01


class DatabaseManager:
    """Manages database operations with proper connection handling."""

    def __init__(self, db_path: str = "festool_recon.db", backup_dir: str = "backups"):
        self.db_path = db_path
        self.backup_dir = backup_dir
        os.makedirs(backup_dir, exist_ok=True)

        # A single connection is shared across threads (StaticPool), so writes
        # from the monitor thread and the GUI thread must be serialised.
        self._write_lock = threading.RLock()

        self._build_engine()
        self._init_database()

    def _build_engine(self):
        """
        Create the engine and session factory.

        Kept in one place so restore_from_backup() cannot rebuild the engine
        with different (unsafe) settings - dropping check_same_thread=False
        would break every database call made from the monitor thread.
        """
        self.engine = create_engine(
            f'sqlite:///{self.db_path}',
            connect_args={
                'check_same_thread': False,
                'timeout': 30,
            },
            poolclass=StaticPool,
            echo=False
        )

        # Enable WAL mode and foreign keys
        @event.listens_for(self.engine, "connect")
        def set_sqlite_pragma(dbapi_conn, connection_record):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.close()

        self.SessionLocal = sessionmaker(bind=self.engine)
    
    # Columns added after the original schema shipped.  create_all() only
    # creates missing *tables*, so columns added to an existing table have to
    # be patched in by hand.
    _ADDED_COLUMNS = [
        ('user_settings', 'timezone_mode', "VARCHAR(10) DEFAULT 'local'"),
        ('user_settings', 'notify_queue_early_warning', "BOOLEAN DEFAULT 1"),
        ('user_settings', 'monitor_on_startup', "BOOLEAN DEFAULT 0"),
        ('products', 'product_type', "VARCHAR(100)"),
        # Left NULL on every existing row on purpose: those all predate the
        # store running more than one collection, so they are the one-at-a-time
        # rotation, which is exactly what NULL is read as everywhere below.
        ('products', 'collection_handle', "VARCHAR(100)"),
        ('products', 'handle', "VARCHAR(500)"),
        ('collection_sightings', 'mode', "VARCHAR(20)"),
        ('collection_sightings', 'mode_seen_at', "DATETIME"),
        ('collection_sightings', 'live_count', "INTEGER"),
    ]

    def _init_database(self):
        """Initialize database tables and apply lightweight migrations."""
        Base.metadata.create_all(bind=self.engine)
        self._apply_column_migrations()
        self.backfill_product_types()

    def _apply_column_migrations(self):
        """Add any columns missing from an older database."""
        from sqlalchemy import text

        added = []
        for table, column, definition in self._ADDED_COLUMNS:
            try:
                with self.engine.begin() as conn:
                    result = conn.execute(text(f"PRAGMA table_info({table})"))
                    existing = [row[1] for row in result.fetchall()]

                    if not existing:
                        continue  # table does not exist yet

                    if column in existing:
                        continue

                    logger.info(f"Adding {column} column to {table}")
                    conn.execute(text(
                        f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
                    ))
                    added.append(f"{table}.{column}")

            except Exception as e:
                # Not fatal - the app degrades to defaults for that setting
                logger.warning(f"Could not add {table}.{column}: {e}")

        if added:
            logger.info(f"Schema migrations applied: {', '.join(added)}")
        return added

    def backfill_product_types(self) -> int:
        """
        Give older listings a category.

        Rows written before product_type existed have none, which would leave
        them out of every per-category rollup.  The category is derived from
        the title, so this is deterministic and safe to re-run; rows that
        already have one are left alone.
        """
        try:
            with self.get_session() as session:
                rows = (session.query(Product)
                        .filter(Product.product_type.is_(None))
                        .all())
                if not rows:
                    return 0

                for row in rows:
                    row.product_type = type_of(row.item_number, None, row.name)

            logger.info(f"Backfilled product_type on {len(rows)} listing(s)")
            return len(rows)

        except Exception as e:
            # A missing category is a cosmetic gap, never a reason not to start
            logger.warning(f"Could not backfill product types: {e}")
            return 0
    
    @contextmanager
    def get_session(self) -> Session:
        """
        Context manager for database sessions.

        The lock serialises access because StaticPool hands the same SQLite
        connection to the GUI thread and the monitor thread; two overlapping
        sessions would otherwise share (and prematurely commit) one another's
        transaction.  It is reentrant, so nested use on one thread is safe.
        """
        with self._write_lock:
            session = self.SessionLocal()
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()
    
    def create_user(self, username: str, password: str, email: Optional[str] = None) -> User:
        """Create a new user with hashed password."""
        password_hash = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt())
        
        with self.get_session() as session:
            user = User(
                username=username,
                password_hash=password_hash.decode('utf-8'),
                email=email
            )
            session.add(user)
            session.flush()
            
            # Create default settings
            # Create default settings with LOCAL mode (more user-friendly)
            settings = UserSettings(
                user_id=user.id,
                timezone_mode='local'  # Default to local display
            )
            session.add(settings)
            session.flush()

            # Expunge to keep the loaded attributes usable after the session closes
            session.expunge(user)

            return user

    def get_or_create_default_user(self) -> int:
        """
        Return the id of the first user, creating the default one if needed.

        Done in a single session so the caller does not have to nest sessions
        on the shared SQLite connection.
        """
        with self.get_session() as session:
            user = session.query(User).first()
            if user:
                return user.id

            password_hash = bcrypt.hashpw(b"default", bcrypt.gensalt()).decode('utf-8')
            user = User(username="default", password_hash=password_hash)
            session.add(user)
            session.flush()

            session.add(UserSettings(user_id=user.id, timezone_mode='local'))
            session.flush()

            logger.info("Created default user")
            return user.id

    def ensure_user_settings(self, user_id: int) -> None:
        """Make sure a settings row exists for the user."""
        with self.get_session() as session:
            existing = session.query(UserSettings).filter_by(user_id=user_id).first()
            if not existing:
                session.add(UserSettings(user_id=user_id, timezone_mode='local'))
                logger.info(f"Created missing settings row for user {user_id}")


    def authenticate_user(self, username: str, password: str) -> Optional[User]:
        """Authenticate user and return user object if successful."""
        with self.get_session() as session:
            user = session.query(User).filter_by(username=username, is_active=True).first()
            if user and bcrypt.checkpw(password.encode('utf-8'), user.password_hash.encode('utf-8')):
                user.last_login = utcnow()
                session.flush()
                session.expunge(user)
                return user
            return None
    
    def add_product(self, item_number: str, name: str, sale_price: float,
               regular_price: Optional[float] = None, description: Optional[str] = None,
               image_url: Optional[str] = None, raw_html: Optional[str] = None,
               product_type: Optional[str] = None,
               collection_handle: Optional[str] = None,
               handle: Optional[str] = None) -> Product:
        """
        Add a new product to the database.

        product_type is the store's raw value when the caller has one; it is
        normalised here (and inferred from the name when absent) so every row
        lands in a canonical category.

        collection_handle records which collection the listing came out of.
        It is what keeps a multi-item sale - where thirty items are on offer
        at once - out of statistics that only make sense for the one-at-a-time
        rotation, while still letting the SKU rollups and price checks see
        every price the item has ever been offered at.
        """
        with self.get_session() as session:
            product = Product(
                item_number=item_number,
                name=name,
                regular_price=regular_price,
                sale_price=sale_price,
                product_type=type_of(item_number, product_type, name),
                description=description,
                image_url=image_url,
                raw_html=raw_html,
                collection_handle=collection_handle,
                handle=handle,
                posted_at=utcnow()  # ALWAYS store as naive UTC
            )
            session.add(product)
            session.flush()
            session.expunge(product)
            return product
    
    def multi_item_handles(self) -> set:
        """Handles of every collection the store is running as a multi-item sale."""
        with self.get_session() as session:
            return self._multi_item_handles(session)

    @staticmethod
    def _multi_item_handles(session) -> set:
        rows = (session.query(CollectionSighting.handle)
                .filter(CollectionSighting.mode == MODE_MULTI)
                .all())
        return {row[0] for row in rows}

    def get_latest_product(self) -> Optional[Product]:
        """
        The most recent listing from the one-at-a-time rotation.

        Deliberately blind to multi-item collections.  This is the app's "what
        is up right now" pointer, and the whole rotation-change machinery -
        false-positive validation, mark_product_replaced, minutes_live - is
        built on there being exactly one.  A sale that puts thirty items up
        together would otherwise land here and read as thirty rotations in a
        single check.
        """
        with self.get_session() as session:
            query = session.query(Product).order_by(Product.posted_at.desc())

            multi = self._multi_item_handles(session)
            if multi:
                # NULL is every row written before collections were tracked,
                # all of which are rotation listings.
                query = query.filter(or_(
                    Product.collection_handle.is_(None),
                    Product.collection_handle.notin_(multi),
                ))

            product = query.first()
            if product:
                # Eager load all attributes before session closes
                session.refresh(product)
                # Make transient to keep loaded data after session closes
                session.expunge(product)
            return product
    
    def mark_product_replaced(self, product_id: int):
        """Mark a product as replaced and calculate time live."""
        with self.get_session() as session:
            product = session.get(Product, product_id)
            if product and not product.replaced_at:
                product.replaced_at = utcnow()
                product.calculate_minutes_live()
    
    def get_product_history(self, limit: Optional[int] = None, offset: int = 0) -> List[Product]:
        """Get product history with optional pagination."""
        with self.get_session() as session:
            query = session.query(Product).order_by(Product.posted_at.desc())
            if limit:
                query = query.limit(limit).offset(offset)
            products = query.all()
            # Expunge all products to keep them after session closes
            for product in products:
                session.expunge(product)
            return products
    
    def get_previous_products(self, count: int = 1) -> List[Product]:
        """Get the previous N products."""
        with self.get_session() as session:
            products = session.query(Product).order_by(
                Product.posted_at.desc()
            ).limit(count).all()
            for product in products:
                session.expunge(product)
            return products
    
    def search_products(self, keywords: List[str]) -> List[Product]:
        """Search products by keywords."""
        with self.get_session() as session:
            query = session.query(Product)
            for keyword in keywords:
                query = query.filter(
                    Product.name.ilike(f'%{keyword}%') |
                    Product.description.ilike(f'%{keyword}%')
                )
            products = query.order_by(Product.posted_at.desc()).all()
            for product in products:
                session.expunge(product)
            return products
    
    def add_to_watchlist(self, user_id: int, keyword: str) -> bool:
        """
        Add keyword to user's watchlist.

        Returns False if it was already there.  Duplicates were previously
        allowed to pile up, which made removing a keyword look like it had not
        worked - the second copy went on matching.
        """
        keyword = (keyword or '').strip()
        if not keyword:
            return False

        with self.get_session() as session:
            existing = [row.keyword for row in
                        session.query(Watchlist).filter_by(user_id=user_id).all()]
            if any((k or '').strip().lower() == keyword.lower() for k in existing):
                return False

            session.add(Watchlist(user_id=user_id, keyword=keyword))
            return True

    def remove_from_watchlist(self, user_id: int, keyword: str) -> int:
        """Drop a watchlist keyword. Returns how many rows went."""
        keyword = (keyword or '').strip().lower()
        if not keyword:
            return 0

        with self.get_session() as session:
            rows = [row for row in
                    session.query(Watchlist).filter_by(user_id=user_id).all()
                    if (row.keyword or '').strip().lower() == keyword]
            for row in rows:
                session.delete(row)
            return len(rows)

    def remove_from_exclusions(self, user_id: int, keyword: Optional[str] = None,
                               item_number: Optional[str] = None) -> int:
        """Drop matching exclusion rules. Returns how many rows went."""
        keyword = (keyword or '').strip().lower() or None
        item_number = (item_number or '').strip().lower() or None
        if keyword is None and item_number is None:
            return 0

        with self.get_session() as session:
            rows = []
            for row in session.query(Exclusion).filter_by(user_id=user_id).all():
                if keyword and (row.keyword or '').strip().lower() == keyword:
                    rows.append(row)
                elif item_number and (row.item_number or '').strip().lower() == item_number:
                    rows.append(row)
            for row in rows:
                session.delete(row)
            return len(rows)

    # ------------------------------------------------------------------
    # Per-item filter rules
    #
    # Watch and exclude are stored differently, for historical reasons that
    # the matchers already depend on: an exclusion has its own item_number
    # column, while the watchlist has only keywords, so watching one item
    # means a keyword that happens to equal its item number.  Both work with
    # matches_watchlist() and is_excluded() as they stand; the asymmetry is
    # confined to this section.
    # ------------------------------------------------------------------
    def get_filter_rules(self, user_id: int) -> dict:
        """
        Every rule affecting notifications, read in one go.

        Item rules and keyword rules are kept apart on purpose.  A menu that
        acts per item must only ever add and remove item rules: clearing one
        sander should not delete a "sander" keyword that was covering a dozen
        other listings too.
        """
        with self.get_session() as session:
            watch = [(row.keyword or '').strip()
                     for row in session.query(Watchlist).filter_by(user_id=user_id).all()]
            exclusions = session.query(Exclusion).filter_by(user_id=user_id).all()
            exclude_keywords = [(row.keyword or '').strip() for row in exclusions]
            exclude_items = [(row.item_number or '').strip() for row in exclusions]

        return {
            'watch_keywords': [k for k in watch if k],
            'exclude_keywords': [k for k in exclude_keywords if k],
            'exclude_items': [i for i in exclude_items if i],
        }

    @staticmethod
    def classify_item(rules: dict, item_number: Optional[str],
                      name: Optional[str]) -> dict:
        """
        How the stored rules currently treat one listing.

        ``rule`` is the item's own rule - the only thing a per-item menu may
        change.  ``matched_by`` names the broader keywords that also catch it,
        so the menu can say so rather than leaving someone to wonder why an
        item they just cleared is still being filtered.
        """
        number = (item_number or '').strip().lower()
        title = (name or '').lower()

        def keyword_hits(keywords):
            return [k for k in keywords
                    if (k.strip().lower() in title
                        or k.strip().lower() == number) and k.strip().lower() != number]

        rule = None
        if number and any(i.strip().lower() == number for i in rules['exclude_items']):
            rule = 'exclude'
        elif number and any(k.strip().lower() == number for k in rules['exclude_keywords']):
            rule = 'exclude'
        elif number and any(k.strip().lower() == number for k in rules['watch_keywords']):
            rule = 'watch'

        return {
            'rule': rule,
            'watched_by': keyword_hits(rules['watch_keywords']),
            'excluded_by': keyword_hits(rules['exclude_keywords']),
        }

    @classmethod
    def explain_item(cls, rules: dict, item_number: Optional[str],
                     name: Optional[str]) -> dict:
        """
        Whether one listing would notify, in each way a listing can arrive.

        The single source of truth for notification filtering.  is_excluded(),
        should_notify_user() and matches_watchlist() all answer from here, and
        so does the Filters tab preview - so what the preview says will notify
        is, by construction, what does.

        Adds to classify_item():
          excluded          - an exclusion catches it; nothing else matters
          watched           - a watch rule catches it (and it is not excluded)
          watchlist_active  - at least one watch rule exists, which switches
                              live alerts from "everything" to "watched only"
          live              - alerts when it goes live in a rotation
          digest            - in a sale: 'alert' (own alert plus digest line),
                              'listed' (digest line only) or 'omitted'
          early_warning     - eligible for a look-ahead warning while queued
                              (the on/off setting is applied by the caller)
          reason            - one line saying why, for people
        """
        state = cls.classify_item(rules, item_number, name)
        watchlist_active = bool(rules['watch_keywords'])

        excluded = state['rule'] == 'exclude' or bool(state['excluded_by'])
        watched = not excluded and (state['rule'] == 'watch' or bool(state['watched_by']))

        if state['rule'] == 'exclude':
            reason = "Excluded item"
        elif state['excluded_by']:
            reason = f"Excluded by keyword '{state['excluded_by'][0]}'"
        elif state['rule'] == 'watch':
            reason = "Watched item"
        elif state['watched_by']:
            reason = f"Matches watch keyword '{state['watched_by'][0]}'"
        elif watchlist_active:
            reason = "Not on your watchlist"
        else:
            reason = "No watchlist, so everything not excluded notifies"

        state.update({
            'excluded': excluded,
            'watched': watched,
            'watchlist_active': watchlist_active,
            'live': not excluded and (watched or not watchlist_active),
            'digest': 'omitted' if excluded else 'alert' if watched else 'listed',
            'early_warning': watched,
            'reason': reason,
        })
        return state

    def set_item_filter(self, user_id: int, item_number: str,
                        state: Optional[str]) -> Optional[str]:
        """
        Put one item on the watchlist, on the exclusion list, or on neither.

        ``state`` is 'watch', 'exclude', or None to clear.  The three are
        exclusive by nature rather than by decree: an excluded item can never
        notify, so holding both rules at once has no meaning beyond being
        something to be puzzled by later.  Setting one therefore clears the
        other.

        Only ever touches rules keyed on this exact item number.  Returns the
        state the item is left in.
        """
        item_number = (item_number or '').strip()
        if not item_number:
            return None
        if state not in (None, 'watch', 'exclude'):
            raise ValueError(f"Unknown filter state: {state!r}")

        removed = self.remove_from_watchlist(user_id, item_number)
        removed += self.remove_from_exclusions(user_id, item_number=item_number,
                                               keyword=item_number)

        if state == 'watch':
            self.add_to_watchlist(user_id, item_number)
        elif state == 'exclude':
            self.add_to_exclusions(user_id, item_number=item_number)

        logger.info(
            f"Filter for {item_number}: "
            + (f"set to {state}" if state else "cleared")
            + (f" (removed {removed} previous rule(s))" if removed else "")
        )
        return state
    
    def add_to_exclusions(self, user_id: int, keyword: Optional[str] = None,
                          item_number: Optional[str] = None) -> bool:
        """
        Add to exclusion list.

        Returns False if an identical rule was already present - see
        add_to_watchlist() for why duplicates are worth refusing.
        """
        keyword = (keyword or '').strip() or None
        item_number = (item_number or '').strip() or None
        if keyword is None and item_number is None:
            return False

        with self.get_session() as session:
            for row in session.query(Exclusion).filter_by(user_id=user_id).all():
                same_keyword = ((row.keyword or '').strip().lower()
                                == (keyword or '').lower())
                same_item = ((row.item_number or '').strip().lower()
                             == (item_number or '').lower())
                if same_keyword and same_item:
                    return False

            session.add(Exclusion(
                user_id=user_id,
                keyword=keyword,
                item_number=item_number
            ))
            return True
    
    def get_user_watchlist(self, user_id: int) -> List[Watchlist]:
        """Get user's watchlist."""
        with self.get_session() as session:
            watchlist = session.query(Watchlist).filter_by(user_id=user_id).all()
            for item in watchlist:
                session.expunge(item)
            return watchlist
    
    def get_user_exclusions(self, user_id: int) -> List[Exclusion]:
        """Get user's exclusion list."""
        with self.get_session() as session:
            exclusions = session.query(Exclusion).filter_by(user_id=user_id).all()
            for item in exclusions:
                session.expunge(item)
            return exclusions
    
    def is_excluded(self, user_id: int, product: Product) -> bool:
        """
        Has the user asked never to hear about this item?

        Exclusions are the one filter that holds no matter how a listing
        reaches the user - single rotation, watchlist hit, or one line in a
        thirty-item sale digest - so they live in their own method rather than
        being re-implemented at each of those call sites.
        """
        verdict = self._explain_product(user_id, product)
        if verdict['excluded']:
            logger.info(f"{verdict['reason']}: {product.name} (#{product.item_number})")
        return verdict['excluded']

    def _explain_product(self, user_id: int, product: Product) -> dict:
        """explain_item() for a stored or candidate product."""
        return self.explain_item(self.get_filter_rules(user_id),
                                 product.item_number, product.name)

    def should_notify_user(self, user_id: int, product: Product) -> bool:
        """
        Check if user should be notified about this product.

        Keywords are matched against the product name and item number only.
        Matching the description too was far too broad - most listings mention
        words like "battery" or "cordless" somewhere in their marketing copy,
        which silently suppressed almost everything.
        """
        verdict = self._explain_product(user_id, product)
        logger.info(f"{verdict['reason']}: {product.name} (#{product.item_number})")
        return verdict['live']
    
    # ------------------------------------------------------------------
    # Look-ahead queue tracking
    # ------------------------------------------------------------------
    def sync_queue(self, queued_products: List, collection_handle: str = 'oneanddone') -> dict:
        """
        Reconcile an observed queue against what is already stored.

        Items still present are updated in place; items that were pending but
        are no longer listed - and never went live - are marked as removed.
        That distinction is the whole point: the queue is a plan, and this is
        how we find out how often the plan holds.

        Returns a dict describing what changed.
        """
        added, updated, removed = [], [], []

        # A failed fetch looks exactly like "the collection is empty".  Treating
        # it as the latter would mark every queued item as skipped and poison
        # the accuracy statistics, so refuse to reconcile against nothing.
        if not queued_products:
            logger.warning("Empty queue passed to sync_queue - ignoring rather than "
                           "marking every item as skipped")
            return {'added': added, 'updated': updated, 'removed': removed}

        seen_numbers = {p.item_number for p in queued_products if p.item_number}
        now = utcnow()

        with self.get_session() as session:
            existing = {
                row.item_number: row
                for row in session.query(QueueObservation)
                .filter_by(collection_handle=collection_handle)
                .all()
            }

            for product in queued_products:
                if not product.item_number:
                    continue

                row = existing.get(product.item_number)

                if row is None:
                    session.add(QueueObservation(
                        collection_handle=collection_handle,
                        item_number=product.item_number,
                        name=product.name,
                        handle=getattr(product, 'handle', None),
                        sale_price=product.sale_price,
                        regular_price=product.regular_price,
                        product_type=product.product_type,
                        image_url=product.image_url,
                        position=product.position,
                        best_position=product.position,
                        times_observed=1,
                        first_seen_at=now,
                        last_seen_at=now,
                    ))
                    added.append(product.item_number)
                    continue

                # An item can be re-listed after having gone live or dropped
                # off.  An item that just went live is still sitting at
                # position 0, though, so that alone is not a re-listing -
                # only treat it as one if it has genuinely left and returned.
                returned_after_removal = row.removed_at is not None
                relisted_after_going_live = (row.went_live_at is not None
                                             and product.position > 0)

                if returned_after_removal or relisted_after_going_live:
                    row.went_live_at = None
                    row.live_from_position = None
                    row.removed_at = None
                    row.first_seen_at = now
                    row.best_position = product.position
                    row.times_observed = 0
                    row.notified_at = None

                row.name = product.name
                row.handle = getattr(product, 'handle', None)
                row.sale_price = product.sale_price
                row.regular_price = product.regular_price
                row.product_type = product.product_type
                row.image_url = product.image_url
                row.position = product.position
                row.best_position = (product.position if row.best_position is None
                                     else min(row.best_position, product.position))
                row.times_observed = (row.times_observed or 0) + 1
                row.last_seen_at = now
                updated.append(product.item_number)

            # Anything pending that has dropped off the feed
            for item_number, row in existing.items():
                if item_number not in seen_numbers and row.is_pending:
                    if row.position == 0:
                        # It was the live product when it left, so it ran its
                        # course rather than being skipped.  This covers items
                        # already live before tracking started.
                        row.went_live_at = now
                        row.live_from_position = (row.best_position
                                                  if row.best_position is not None else 0)
                        logger.debug(
                            f"Live item {item_number} ({row.name}) left the queue "
                            f"after its run"
                        )
                    else:
                        row.removed_at = now
                        removed.append(item_number)
                        logger.info(
                            f"Queued item {item_number} ({row.name}) left the queue "
                            f"from position {row.position} without going live"
                        )

        return {'added': added, 'updated': updated, 'removed': removed}

    def mark_queue_item_live(self, item_number: str,
                             collection_handle: str = 'oneanddone') -> Optional[int]:
        """
        Record that a queued item became the live product.

        Returns the position it was holding when it went live, which is what
        the accuracy statistics are built from.
        """
        with self.get_session() as session:
            row = (session.query(QueueObservation)
                   .filter_by(collection_handle=collection_handle,
                              item_number=item_number)
                   .first())
            if not row:
                logger.info(f"Item {item_number} went live without having been queued")
                return None

            position = row.best_position if row.best_position is not None else row.position
            row.went_live_at = utcnow()
            row.live_from_position = position
            row.removed_at = None
            logger.info(f"Queued item {item_number} went live from position {position}")
            return position

    def get_queue(self, collection_handle: str = 'oneanddone',
                  pending_only: bool = True) -> List[QueueObservation]:
        """Current queue contents, nearest to going live first."""
        with self.get_session() as session:
            query = session.query(QueueObservation).filter_by(
                collection_handle=collection_handle
            )
            if pending_only:
                query = query.filter(QueueObservation.went_live_at.is_(None),
                                     QueueObservation.removed_at.is_(None))
            rows = query.order_by(QueueObservation.position.asc()).all()
            for row in rows:
                session.expunge(row)
            return rows

    def get_queue_outcomes(self, collection_handle: str = 'oneanddone') -> List[QueueObservation]:
        """Every queued item that has since gone live or been dropped."""
        with self.get_session() as session:
            rows = (session.query(QueueObservation)
                    .filter_by(collection_handle=collection_handle)
                    .filter((QueueObservation.went_live_at.isnot(None)) |
                            (QueueObservation.removed_at.isnot(None)))
                    .order_by(QueueObservation.last_seen_at.desc())
                    .all())
            for row in rows:
                session.expunge(row)
            return rows

    def get_queue_skips(self,
                        collection_handle: Optional[str] = None) -> List[QueueObservation]:
        """
        Every queued item that left without ever going live.

        Across all collections by default, not just the one being rotated
        today: the store renames and replaces that collection, and an item
        lined up under the old name is the same item.

        These are the near-misses - things the store put in the queue and then
        pulled. Worth keeping, because they are the part of the look-ahead's
        record that says how often the plan does not hold, and because an item
        that was queued and pulled may well come round again.
        """
        with self.get_session() as session:
            query = session.query(QueueObservation).filter(
                QueueObservation.removed_at.isnot(None),
                QueueObservation.went_live_at.is_(None),
            )
            if collection_handle:
                query = query.filter_by(collection_handle=collection_handle)

            rows = query.order_by(QueueObservation.removed_at.desc()).all()
            for row in rows:
                session.expunge(row)
            return rows

    def get_queue_product_type(self, item_number: str,
                               collection_handle: str = 'oneanddone') -> Optional[str]:
        """
        The store's raw product type for an item, if it was seen in the queue.

        The front page does not expose the type, but the collection feed does,
        and everything that goes live passed through the queue - so this
        recovers the store's own classification instead of inferring one.
        """
        with self.get_session() as session:
            row = (session.query(QueueObservation)
                   .filter_by(collection_handle=collection_handle,
                              item_number=item_number)
                   .first())
            return row.product_type if row else None

    def mark_queue_item_notified(self, item_number: str,
                                 collection_handle: str = 'oneanddone'):
        """Record that an early-warning alert was sent, so it only goes once."""
        with self.get_session() as session:
            row = (session.query(QueueObservation)
                   .filter_by(collection_handle=collection_handle,
                              item_number=item_number)
                   .first())
            if row:
                row.notified_at = utcnow()

    # ------------------------------------------------------------------
    # Printed catalog index (built offline - see build_catalog_index.py)
    # ------------------------------------------------------------------
    def replace_catalog_year(self, catalog_year: str, items: List,
                             source_file: Optional[str] = None) -> int:
        """
        Store one catalog edition, replacing any previous extraction of it.

        Replacing rather than merging keeps a re-run after an extractor fix
        from leaving stale rows behind.
        """
        now = utcnow()
        with self.get_session() as session:
            session.query(CatalogEntry).filter_by(catalog_year=catalog_year).delete()

            for item in items:
                if not item.item_number:
                    continue
                session.add(CatalogEntry(
                    catalog_year=catalog_year,
                    item_number=item.item_number,
                    msrp_usd=item.msrp_usd,
                    msrp_cad=item.msrp_cad,
                    page=item.page,
                    confidence=item.confidence,
                    source_file=source_file,
                    extracted_at=now,
                ))

        logger.info(f"Catalog {catalog_year}: stored {len(items)} item(s)")
        return len(items)

    def get_catalog_years(self) -> List[tuple]:
        """[(year, items, priced_items), ...] oldest edition first."""
        with self.get_session() as session:
            rows = session.query(CatalogEntry).all()
            by_year: dict = {}
            for row in rows:
                total, priced = by_year.get(row.catalog_year, (0, 0))
                by_year[row.catalog_year] = (
                    total + 1,
                    priced + (1 if row.msrp_usd is not None else 0)
                )
        return sorted((year, t, p) for year, (t, p) in by_year.items())

    def get_catalog_history(self, item_number: str) -> List[CatalogEntry]:
        """Every catalog edition an item was printed in, oldest first."""
        if not item_number:
            return []
        with self.get_session() as session:
            rows = (session.query(CatalogEntry)
                    .filter_by(item_number=item_number)
                    .order_by(CatalogEntry.catalog_year.asc())
                    .all())
            for row in rows:
                session.expunge(row)
            return rows

    def get_catalog_entry(self, item_number: str,
                          catalog_year: Optional[str] = None) -> Optional[CatalogEntry]:
        """
        One catalog record for an item - the newest edition carrying a US
        price unless a specific year is asked for.
        """
        history = self.get_catalog_history(item_number)
        if not history:
            return None

        if catalog_year:
            return next((e for e in history if e.catalog_year == catalog_year), None)

        priced = [e for e in history if e.msrp_usd is not None]
        return priced[-1] if priced else history[-1]

    def get_catalog_item_numbers(self, catalog_year: Optional[str] = None) -> set:
        """Every item number known to the catalog index."""
        with self.get_session() as session:
            query = session.query(CatalogEntry.item_number)
            if catalog_year:
                query = query.filter(CatalogEntry.catalog_year == catalog_year)
            return {row[0] for row in query.all()}

    def sync_collections(self, collections: List) -> List[CollectionSighting]:
        """
        Record the collections currently published.

        Returns collections seen for the first time - normally none, since only
        'oneanddone' exists; anything else suggests a special sale.
        """
        now = utcnow()
        seen_handles = {c.handle for c in collections}
        new_sightings = []

        with self.get_session() as session:
            existing = {row.handle: row for row in session.query(CollectionSighting).all()}

            for collection in collections:
                row = existing.get(collection.handle)
                if row is None:
                    row = CollectionSighting(
                        handle=collection.handle,
                        title=collection.title,
                        products_count=collection.products_count,
                        first_seen_at=now,
                        last_seen_at=now,
                        is_active=True,
                    )
                    session.add(row)
                    session.flush()
                    session.expunge(row)
                    new_sightings.append(row)
                    logger.info(f"New collection seen: {collection.handle} ({collection.title})")
                else:
                    row.title = collection.title
                    row.products_count = collection.products_count
                    row.last_seen_at = now
                    row.is_active = True

            # Collections that have gone away
            for handle, row in existing.items():
                if handle not in seen_handles and row.is_active:
                    row.is_active = False
                    logger.info(f"Collection no longer published: {handle}")

        return new_sightings

    def get_collection_sightings(self, active_only: bool = False) -> List[CollectionSighting]:
        """All collections ever seen."""
        with self.get_session() as session:
            query = session.query(CollectionSighting)
            if active_only:
                query = query.filter_by(is_active=True)
            rows = query.order_by(CollectionSighting.first_seen_at.asc()).all()
            for row in rows:
                session.expunge(row)
            return rows

    def mark_collection_notified(self, handle: str):
        """Record that a special-sale alert was sent for this collection."""
        with self.get_session() as session:
            row = session.query(CollectionSighting).filter_by(handle=handle).first()
            if row:
                row.notified_at = utcnow()

    # ------------------------------------------------------------------
    # Multi-item collections
    # ------------------------------------------------------------------
    def get_collection_modes(self) -> dict:
        """handle -> MODE_SINGLE / MODE_MULTI for every collection with a known mode."""
        with self.get_session() as session:
            rows = (session.query(CollectionSighting.handle, CollectionSighting.mode)
                    .filter(CollectionSighting.mode.isnot(None))
                    .all())
            return {handle: mode for handle, mode in rows}

    def record_collection_modes(self, modes: dict,
                                live_counts: Optional[dict] = None) -> dict:
        """
        Store how the store is currently running each collection.

        Only handles present in ``modes`` are touched.  A collection left out
        keeps whatever was recorded before, which is what makes an unreadable
        front page harmless: it produces no modes, so nothing changes and the
        app carries on with what it already knew.

        Returns {handle: (previous_mode, new_mode)} for the collections whose
        mode actually changed.  That is worth surfacing on its own - a store
        switching from selling one item at a time to selling everything at
        once is the most consequential thing it can do to this app.
        """
        live_counts = live_counts or {}
        changed = {}
        now = utcnow()

        with self.get_session() as session:
            rows = {row.handle: row for row in session.query(CollectionSighting).all()}

            for handle, mode in modes.items():
                row = rows.get(handle)
                if row is None:
                    logger.debug(f"No sighting recorded yet for '{handle}' - skipping mode")
                    continue

                previous = row.mode
                if previous != mode:
                    changed[handle] = (previous, mode)
                    logger.warning(
                        f"Collection '{handle}' is now being sold "
                        f"{'one item at a time' if mode == MODE_SINGLE else 'all at once'}"
                        + (f" (was {previous})" if previous else " (first reading)")
                    )

                row.mode = mode
                row.mode_seen_at = now
                if handle in live_counts:
                    row.live_count = live_counts[handle]

        return changed

    @staticmethod
    def _retire(session, rows: List[Product], now) -> List[Product]:
        """
        Close listings whose end time we know but whose duration we do not.

        ``minutes_live`` is left NULL on purpose - see Product.minutes_live.
        Flush before detaching, or the update goes out with the identity map.
        """
        for row in rows:
            row.replaced_at = now

        session.flush()
        for row in rows:
            session.expunge(row)
        return rows

    def close_collection_listings(self, collection_handle: str) -> List[Product]:
        """
        Retire what a multi-item collection was offering once it is gone.

        The mirror of close_rotation_listing(), for the other way a sale can
        end: the collection stops being published at all.  An unpublished
        collection never appears in a feed, so sync_live_collection() is never
        called for it and its listings would otherwise stay open forever -
        thirty items still counted as "currently listed" months after the sale
        finished.

        Only call this on positive evidence that the collection is gone: the
        collection index was read and this handle was not in it.  "Not in the
        feeds" alone will not do - that is also what an unreadable collection
        looks like.
        """
        now = utcnow()

        with self.get_session() as session:
            rows = self._retire(session, session.query(Product).filter(
                Product.collection_handle == collection_handle,
                Product.replaced_at.is_(None),
            ).all(), now)

        if rows:
            logger.warning(
                f"Collection '{collection_handle}' is no longer published - "
                f"closing its {len(rows)} remaining listing(s); how long they "
                f"stayed on offer is unknown and is recorded as such"
            )

        return rows

    def close_rotation_listing(self) -> List[Product]:
        """
        Retire the open one-at-a-time listing once the store stops rotating.

        Whatever was last up is not up any more, and an open row goes on
        claiming otherwise everywhere at once: History shows "Current", the
        SKU rollup counts the item as currently listed, and the Current
        Product panel presents a listing that ended hours ago as live.

        ``replaced_at`` is stamped with the moment we noticed.  ``minutes_live``
        is deliberately left NULL: we know the listing ended, but not when -
        the store could have switched at any point since the last check, or
        while the app was closed - and a duration measured from "now" would be
        invented, then quietly absorbed into every average in the app.  NULL
        is already the "unknown" those averages skip.

        Idempotent: rows that are already closed are left alone.
        """
        now = utcnow()

        with self.get_session() as session:
            multi = self._multi_item_handles(session)

            query = session.query(Product).filter(Product.replaced_at.is_(None))
            if multi:
                query = query.filter(or_(
                    Product.collection_handle.is_(None),
                    Product.collection_handle.notin_(multi),
                ))

            rows = self._retire(session, query.all(), now)

        for row in rows:
            logger.warning(
                f"Store has stopped rotating - closing the open listing for "
                f"{row.item_number} ({row.name}); how long it stayed live is "
                f"unknown and is recorded as such"
            )

        return rows

    def get_open_listings(self, collection_handle: str) -> List[Product]:
        """
        Everything currently on offer from one collection, dearest last.

        "Open" means recorded and not yet retired - for a multi-item sale that
        is the whole shop window.
        """
        with self.get_session() as session:
            rows = (session.query(Product)
                    .filter(Product.collection_handle == collection_handle,
                            Product.replaced_at.is_(None))
                    .order_by(Product.sale_price.asc(), Product.id.asc())
                    .all())
            for row in rows:
                session.expunge(row)
            return rows

    def sync_live_collection(self, items: Optional[List],
                             collection_handle: str) -> dict:
        """
        Reconcile a multi-item collection's shop window against what is stored.

        ``items`` is what the store is offering right now - already filtered to
        the available, non-placeholder entries - or **None** when the feed
        could not be read.  Those two are not the same thing and are not
        treated the same:

        * ``None``  - unreadable.  Nothing is added and nothing is retired.
        * ``[]``    - read, and genuinely empty.  Every open listing is
                      retired, because the sale really has ended.

        Collapsing them would be the expensive mistake here: one bad request
        would retire an entire live sale and then re-add all of it on the next
        check, firing a notification for thirty items nobody has stopped
        selling.  CollectionScraper.fetch_feeds() is built to preserve the
        distinction - an unreadable collection is left out of its result
        entirely rather than mapped to an empty list.

        An item that sells out drops out of the feed and is retired here, so
        minutes_live ends up meaning "how long it was on offer" - the same
        thing it means for the rotation.

        Returns {'added': [Product], 'removed': [Product], 'unchanged': int,
                 'repriced': [Product], 'skipped': bool}.
        """
        result = {'added': [], 'removed': [], 'unchanged': 0,
                  'repriced': [], 'skipped': False}

        if items is None:
            logger.warning(
                f"Feed for '{collection_handle}' could not be read - leaving its "
                f"listings alone rather than retiring them"
            )
            result['skipped'] = True
            return result

        seen = {}
        for item in items:
            if not item.item_number:
                logger.debug(f"Ignoring feed entry with no SKU: {item.name}")
                continue
            if item.sale_price is None:
                logger.warning(
                    f"Ignoring '{item.name}' ({item.item_number}) in "
                    f"'{collection_handle}': the feed carried no price"
                )
                continue
            # A collection can list the same SKU twice; first occurrence wins.
            seen.setdefault(item.item_number, item)

        now = utcnow()

        with self.get_session() as session:
            open_rows = {
                row.item_number: row
                for row in session.query(Product).filter(
                    Product.collection_handle == collection_handle,
                    Product.replaced_at.is_(None),
                ).all()
            }

            # New to the window
            for item_number, item in seen.items():
                row = open_rows.get(item_number)
                if row is None:
                    row = Product(
                        item_number=item_number,
                        name=item.name,
                        sale_price=item.sale_price,
                        regular_price=item.regular_price,
                        product_type=type_of(item_number, item.product_type, item.name),
                        image_url=item.image_url,
                        handle=item.handle,
                        collection_handle=collection_handle,
                        posted_at=now,
                    )
                    session.add(row)
                    session.flush()
                    session.expunge(row)
                    result['added'].append(row)
                    continue

                # Already on offer.  A price move during a sale is updated in
                # place rather than opened as a fresh listing: the store's two
                # cache copies can disagree on price for a while, and churning
                # a row per disagreement would invent listings and fire alerts
                # for a sale nobody changed.  The cost is that an intermediate
                # dip is not kept as its own row.
                if _price_moved(row.sale_price, item.sale_price):
                    logger.info(
                        f"Price change while on offer: {item_number} "
                        f"${row.sale_price:,.2f} -> ${item.sale_price:,.2f}"
                    )
                    row.sale_price = item.sale_price
                    result['repriced'].append(item_number)
                if item.regular_price is not None:
                    row.regular_price = item.regular_price
                if item.handle and row.handle != item.handle:
                    # The store re-slugs products from time to time; a stored
                    # link that no longer resolves is worse than none.
                    row.handle = item.handle
                result['unchanged'] += 1

            # Gone from the window - sold out, pulled, or the sale ended
            retired = [row for item_number, row in open_rows.items()
                       if item_number not in seen]
            for row in retired:
                row.replaced_at = now
                row.calculate_minutes_live()

            # Flush before detaching: expunging a modified row first would
            # drop the update along with the identity map entry.
            session.flush()
            for row in retired:
                session.expunge(row)
            result['removed'] = retired

        if result['added'] or result['removed']:
            logger.info(
                f"Collection '{collection_handle}': {len(result['added'])} added, "
                f"{len(result['removed'])} retired, {result['unchanged']} unchanged"
            )
        if not seen and open_rows:
            logger.warning(
                f"Collection '{collection_handle}' is now empty - retired "
                f"{len(result['removed'])} listing(s)"
            )

        return result

    def matches_watchlist(self, user_id: int, product: Product) -> bool:
        """
        Does this product explicitly match a watchlist keyword?

        Deliberately stricter than should_notify_user(): an empty watchlist
        means "no specific interest", not "interested in everything".  Used for
        look-ahead early warnings, where alerting on every queued item would be
        pure noise.  Exclusions still win.
        """
        return self._explain_product(user_id, product)['watched']

    def log_notification(self, product_id: int, user_id: int, notification_type: str,
                        success: bool = True, error_message: Optional[str] = None):
        """Log a notification attempt."""
        with self.get_session() as session:
            log = NotificationLog(
                product_id=product_id,
                user_id=user_id,
                notification_type=notification_type,
                success=success,
                error_message=error_message
            )
            session.add(log)
    
    def get_statistics(self) -> dict:
        """
        Get database statistics.

        ``average_minutes_live`` counts rotation listings only.  A multi-item
        sale leaves thirty items up together for days, and averaging that in
        with a rotation that turns over in minutes would describe neither: the
        headline number would quietly become "how long the last sale ran".
        Those listings are counted separately as ``multi_item_listings``.
        """
        with self.get_session() as session:
            total_products = session.query(Product).count()

            multi = self._multi_item_handles(session)

            rotation = session.query(Product).filter(Product.minutes_live.isnot(None))
            if multi:
                rotation = rotation.filter(or_(
                    Product.collection_handle.is_(None),
                    Product.collection_handle.notin_(multi),
                ))
            times = [row[0] for row in rotation.with_entities(Product.minutes_live).all()]

            avg_time = sum(times) / len(times) if times else 0

            multi_listings = 0
            if multi:
                multi_listings = (session.query(Product)
                                  .filter(Product.collection_handle.in_(multi))
                                  .count())

            return {
                'total_products': total_products,
                'average_minutes_live': round(avg_time, 2),
                'total_notifications': session.query(NotificationLog).count(),
                'multi_item_listings': multi_listings,
                'rotation_listings': total_products - multi_listings,
            }
    
    def backup_database(self) -> str:
        """
        Create a backup of the database.

        Uses SQLite's online backup API rather than a file copy: the database
        runs in WAL mode, so recent commits may still live in the -wal file and
        a plain copy of the .db can be stale or torn.
        """
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        backup_path = os.path.join(self.backup_dir, f'backup_{timestamp}.db')

        with self._write_lock:
            source = sqlite3.connect(self.db_path, timeout=30)
            try:
                dest = sqlite3.connect(backup_path)
                try:
                    source.backup(dest)
                    dest.execute("VACUUM")
                    dest.commit()
                finally:
                    dest.close()
            finally:
                source.close()

        # Log backup
        file_size = os.path.getsize(backup_path)
        with self.get_session() as session:
            backup = DatabaseBackup(
                backup_path=backup_path,
                file_size_bytes=file_size
            )
            session.add(backup)

        # Clean old backups (keep last 10)
        self._cleanup_old_backups()

        logger.info(f"Database backed up to {backup_path} ({file_size} bytes)")
        return backup_path
    
    def _cleanup_old_backups(self, keep_count: int = 10):
        """Remove old backups keeping only the most recent ones."""
        with self.get_session() as session:
            backups = session.query(DatabaseBackup).order_by(
                DatabaseBackup.created_at.desc()
            ).all()

            if len(backups) > keep_count:
                for backup in backups[keep_count:]:
                    try:
                        if os.path.exists(backup.backup_path):
                            os.remove(backup.backup_path)
                    except OSError as e:
                        logger.warning(f"Could not delete old backup {backup.backup_path}: {e}")
                    session.delete(backup)

    def restore_from_backup(self, backup_path: str):
        """Restore database from backup."""
        if not os.path.exists(backup_path):
            raise FileNotFoundError(f"Backup file not found: {backup_path}")

        with self._write_lock:
            # Safety net in case the restore turns out to be the wrong backup
            if os.path.exists(self.db_path):
                pre_restore = os.path.join(
                    self.backup_dir,
                    f"pre_restore_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
                )
                shutil.copy2(self.db_path, pre_restore)
                logger.info(f"Saved pre-restore copy to {pre_restore}")

            self.engine.dispose()

            # WAL/journal side files belong to the old database - leaving them
            # behind would corrupt the restored one.
            for suffix in ('-wal', '-shm', '-journal'):
                side_file = self.db_path + suffix
                if os.path.exists(side_file):
                    os.remove(side_file)

            shutil.copy2(backup_path, self.db_path)

            # Rebuild with the same connect args and pragmas as the original
            # engine; a bare create_engine() here would re-enable
            # check_same_thread and break the background monitor.
            self._build_engine()
            self._init_database()

        logger.info(f"Database restored from {backup_path}")