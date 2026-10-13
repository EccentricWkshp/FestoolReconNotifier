"""Database models for Festool Recon Monitor."""
from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import (
    Column, Integer, String, Float, DateTime, Boolean, Text, ForeignKey, JSON,
    UniqueConstraint
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


def utcnow() -> datetime:
    """
    Current UTC time as a naive datetime.

    All timestamps are stored naive-UTC; display conversion is handled by
    utils.timezone_utils.TimezoneManager.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Product(Base):
    """Product model tracking Festool Recon items."""
    __tablename__ = 'products'
    
    id = Column(Integer, primary_key=True)
    item_number = Column(String(50), nullable=False, index=True)
    name = Column(String(500), nullable=False)
    regular_price = Column(Float, nullable=True)
    sale_price = Column(Float, nullable=False)
    # Canonical category (analytics.product_types).  The front-page scraper
    # cannot see the store's product_type - it is only in the collection feed -
    # so this is usually inferred from the title.
    product_type = Column(String(100), nullable=True, index=True)
    description = Column(Text, nullable=True)
    image_url = Column(String(1000), nullable=True)
    # The store's own URL slug for this product.  The store builds every
    # product URL from it, and an item number will not substitute, so a row
    # without one cannot be linked to.
    handle = Column(String(500), nullable=True)
    # Which collection this listing was taken from.  NULL on every row written
    # before the store ran more than one collection - those are all the
    # one-at-a-time rotation, which is what get_latest_product() still means.
    collection_handle = Column(String(100), nullable=True, index=True)
    posted_at = Column(DateTime, nullable=False, default=utcnow)
    replaced_at = Column(DateTime, nullable=True)
    # Three states, and 0 is not one to be confused with NULL:
    #
    #   NULL + replaced_at NULL  - still on offer
    #   NULL + replaced_at set   - it ended, but we do not know when.  Written
    #                              when the store stopped selling this way and
    #                              the listing had to be closed without
    #                              evidence of how long it had run.
    #   0                        - known, and under a minute.  Routine here:
    #                              items on this store sell out that fast.
    #
    # Everything that reads this must test `is not None`, never truthiness.
    # Treating 0 as "no answer" drops the fastest sell-outs out of the very
    # averages meant to describe them, and once made two of those averages
    # disagree with each other.
    minutes_live = Column(Integer, nullable=True)
    raw_html = Column(Text, nullable=True)
    
    # Relationships
    notifications_sent = relationship("NotificationLog", back_populates="product")
    
    def __repr__(self):
        return f"<Product(item_number={self.item_number}, name={self.name[:30]})>"

    @property
    def url(self) -> Optional[str]:
        """
        Link to this exact listing on the store.

        None for rows written before handles were recorded: the store's URLs
        are built from its own slug, and guessing one from the item number or
        the title would produce a link that 404s.  Better no link than a
        broken one.
        """
        if not self.handle:
            return None
        if self.collection_handle:
            return (f"https://www.festoolrecon.com/collections/"
                    f"{self.collection_handle}/products/{self.handle}")
        return f"https://www.festoolrecon.com/products/{self.handle}"

    @property
    def is_live(self) -> bool:
        """Still on offer - nothing has retired this listing yet."""
        return self.replaced_at is None
    
    def calculate_minutes_live(self):
        """Calculate how long product was live."""
        if self.replaced_at and self.posted_at:
            delta = self.replaced_at - self.posted_at
            self.minutes_live = int(delta.total_seconds() / 60)
        return self.minutes_live


class User(Base):
    """User model for authentication."""
    __tablename__ = 'users'
    
    id = Column(Integer, primary_key=True)
    username = Column(String(100), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    email = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=utcnow)
    last_login = Column(DateTime, nullable=True)
    is_active = Column(Boolean, default=True)
    
    # Relationships
    settings = relationship("UserSettings", back_populates="user", uselist=False)
    watchlist = relationship("Watchlist", back_populates="user")
    exclusions = relationship("Exclusion", back_populates="user")
    
    def __repr__(self):
        return f"<User(username={self.username})>"


class UserSettings(Base):
    """User settings and preferences."""
    __tablename__ = 'user_settings'
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    
    # Monitoring settings
    check_interval_minutes = Column(Integer, default=5)
    is_monitoring = Column(Boolean, default=False)
    # Begin monitoring as soon as the window opens, without being asked.
    # Off by default: starting network activity unprompted should be a choice,
    # not what happens to someone opening the app to look something up.
    monitor_on_startup = Column(Boolean, default=False)
    
    # Timezone preference
    timezone_mode = Column(String(10), default='local')  # 'local' or 'utc'
    
    # Notification preferences
    notify_in_app = Column(Boolean, default=True)
    notify_email = Column(Boolean, default=False)
    notify_sms = Column(Boolean, default=False)
    notify_pushbullet = Column(Boolean, default=False)
    notify_webhook = Column(Boolean, default=False)
    
    # Include previous products in notification
    include_previous_count = Column(Integer, default=0)  # 0, 1, 3, or 5
    include_photos = Column(Boolean, default=True)

    # Look-ahead: alert when a watchlist item appears in the upcoming queue
    notify_queue_early_warning = Column(Boolean, default=True)
    
    # Keyword filtering
    keyword_filters = Column(JSON, default=list)  # List of keywords to match
    
    # Notification provider configs (encrypted)
    email_config = Column(Text, nullable=True)  # JSON: {smtp_server, port, username, password}
    sms_config = Column(Text, nullable=True)    # JSON: {provider, api_key, phone_number}
    pushbullet_config = Column(Text, nullable=True)  # JSON: {api_key}
    webhook_config = Column(Text, nullable=True)     # JSON: {url, method, headers}
    
    # Predictive model settings
    enable_basic_prediction = Column(Boolean, default=True)
    enable_advanced_prediction = Column(Boolean, default=False)
    
    user = relationship("User", back_populates="settings")
    
    def __repr__(self):
        return f"<UserSettings(user_id={self.user_id})>"


class Watchlist(Base):
    """Products or keywords user wants to watch for."""
    __tablename__ = 'watchlist'
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    keyword = Column(String(255), nullable=False)
    created_at = Column(DateTime, default=utcnow)
    
    user = relationship("User", back_populates="watchlist")
    
    def __repr__(self):
        return f"<Watchlist(keyword={self.keyword})>"


class Exclusion(Base):
    """Products or keywords to ignore."""
    __tablename__ = 'exclusions'
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    keyword = Column(String(255), nullable=True)
    item_number = Column(String(50), nullable=True)
    created_at = Column(DateTime, default=utcnow)
    
    user = relationship("User", back_populates="exclusions")
    
    def __repr__(self):
        return f"<Exclusion(keyword={self.keyword}, item_number={self.item_number})>"


class NotificationLog(Base):
    """Log of notifications sent."""
    __tablename__ = 'notification_logs'
    
    id = Column(Integer, primary_key=True)
    product_id = Column(Integer, ForeignKey('products.id'), nullable=False)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    notification_type = Column(String(50), nullable=False)  # email, sms, pushbullet, webhook
    sent_at = Column(DateTime, default=utcnow)
    success = Column(Boolean, default=True)
    error_message = Column(Text, nullable=True)
    
    product = relationship("Product", back_populates="notifications_sent")
    
    def __repr__(self):
        return f"<NotificationLog(type={self.notification_type}, success={self.success})>"


class Prediction(Base):
    """Predictions for next product."""
    __tablename__ = 'predictions'
    
    id = Column(Integer, primary_key=True)
    predicted_item_number = Column(String(50), nullable=True)
    predicted_category = Column(String(255), nullable=True)
    confidence_score = Column(Float, nullable=True)
    prediction_method = Column(String(50), nullable=False)  # basic or advanced
    created_at = Column(DateTime, default=utcnow)
    was_correct = Column(Boolean, nullable=True)
    actual_item_number = Column(String(50), nullable=True)
    
    def __repr__(self):
        return f"<Prediction(method={self.prediction_method}, confidence={self.confidence_score})>"


class QueueObservation(Base):
    """
    An item seen in a collection's look-ahead queue.

    One row per (collection, item number) rather than per sighting: the row is
    updated in place as the item moves up the queue.  This is what lets the app
    be honest about the queue being a plan rather than a schedule - when an
    item finally goes live we record the position it was holding, and when it
    vanishes without ever going live we record that instead.
    """
    __tablename__ = 'queue_observations'

    id = Column(Integer, primary_key=True)
    collection_handle = Column(String(100), nullable=False, index=True)
    item_number = Column(String(50), nullable=False, index=True)
    name = Column(String(500), nullable=False)
    handle = Column(String(500), nullable=True)
    sale_price = Column(Float, nullable=True)
    regular_price = Column(Float, nullable=True)
    product_type = Column(String(100), nullable=True)
    image_url = Column(String(1000), nullable=True)

    position = Column(Integer, nullable=False)          # most recent position
    best_position = Column(Integer, nullable=True)      # closest it ever got to live

    # Bookkeeping only: how many refreshes happened to catch this item.  It
    # tracks how often we looked, not anything about the item, so it must not
    # be presented to the user as a queue metric - see minutes_in_queue.
    times_observed = Column(Integer, default=1)

    first_seen_at = Column(DateTime, default=utcnow)
    last_seen_at = Column(DateTime, default=utcnow)

    # Outcome - exactly one of these is normally set
    went_live_at = Column(DateTime, nullable=True)
    live_from_position = Column(Integer, nullable=True)  # position held before going live
    removed_at = Column(DateTime, nullable=True)         # left the queue without going live

    notified_at = Column(DateTime, nullable=True)        # early-warning alert sent

    def __repr__(self):
        return (f"<QueueObservation(item={self.item_number}, pos={self.position}, "
                f"live={self.went_live_at is not None})>")

    @property
    def is_pending(self) -> bool:
        """Still queued: not yet live and not yet dropped."""
        return self.went_live_at is None and self.removed_at is None

    @property
    def was_skipped(self) -> bool:
        """Left the queue without ever becoming the live product."""
        return self.removed_at is not None and self.went_live_at is None

    @property
    def minutes_in_queue(self) -> Optional[int]:
        """
        How long the item has been waiting in the queue.

        Real elapsed time since it entered the queue, ending when it went live
        or dropped off.  This is what times_observed looks like it means but
        is not: that counter moves with the check interval and with every
        manual refresh, so it says how often we looked rather than how long
        the item has been waiting.
        """
        if not self.first_seen_at:
            return None
        end = self.went_live_at or self.removed_at or utcnow()
        return max(0, int((end - self.first_seen_at).total_seconds() // 60))


# How the store is presenting a collection.
#
# These are the two shapes the store has actually been seen to use.  They are
# a property of the *presentation*, not of the collection's contents: a
# one-at-a-time collection lists dozens of available items in its feed too -
# they are the queue - so item counts cannot tell the two apart.  See
# scraper.collection_scraper.Storefront for how they are told apart.
MODE_SINGLE = 'single'   # one item fronted at a time; the rest are a queue
MODE_MULTI = 'multi'     # every available item is on offer simultaneously


class CollectionSighting(Base):
    """
    A collection seen on the store.

    For a long time only 'oneanddone' existed, so a new handle showing up is a
    strong signal of a special sale and is worth alerting on.  It is also the
    point at which the store may stop selling one item at a time, so each
    sighting records the mode the store is running it in.
    """
    __tablename__ = 'collection_sightings'

    id = Column(Integer, primary_key=True)
    handle = Column(String(100), nullable=False, unique=True, index=True)
    title = Column(String(255), nullable=True)
    products_count = Column(Integer, nullable=True)
    first_seen_at = Column(DateTime, default=utcnow)
    last_seen_at = Column(DateTime, default=utcnow)
    is_active = Column(Boolean, default=True)
    notified_at = Column(DateTime, nullable=True)

    # MODE_SINGLE / MODE_MULTI, or None while it is genuinely unknown.
    #
    # None is a real state and must not be collapsed into either mode: it is
    # what a failed front-page read leaves behind, and acting on a guess there
    # would either record a whole collection as new listings or ignore a live
    # one.  Nothing is recorded from a collection whose mode is unknown.
    mode = Column(String(20), nullable=True)
    mode_seen_at = Column(DateTime, nullable=True)

    # Items on offer at the last check.  For MODE_SINGLE that is 1 (with the
    # rest queued behind); for MODE_MULTI it is the whole available feed.
    live_count = Column(Integer, nullable=True)

    def __repr__(self):
        return (f"<CollectionSighting(handle={self.handle}, "
                f"mode={self.mode}, active={self.is_active})>")

    @property
    def is_multi_item(self) -> bool:
        """The store is offering everything in here at once."""
        return self.mode == MODE_MULTI

    @property
    def is_single_item(self) -> bool:
        """The store is fronting one item at a time from here."""
        return self.mode == MODE_SINGLE

    @property
    def url(self) -> str:
        return f"https://www.festoolrecon.com/collections/{self.handle}"


class CatalogEntry(Base):
    """
    One item as printed in one year's Festool US catalog.

    Built offline from the catalog PDFs (see catalog.extractor), this is the
    only MSRP source in the app that the store does not control: the store's
    own compare_at_price is whatever it chooses to publish, whereas the
    catalog is Festool's list price for that year.  Two things fall out of it:

    * a real MSRP baseline to judge a "discount" against, and
    * catalog membership - an item that stopped appearing after 2019 is a
      discontinued model, which for a refurbished-tool store is a useful thing
      to know before buying.

    Extraction is heuristic, so `confidence` records how the price was found
    and callers can require 'table' when they need certainty.
    """
    __tablename__ = 'catalog_entries'

    id = Column(Integer, primary_key=True)
    catalog_year = Column(String(10), nullable=False, index=True)  # '2026', '2011-12'
    item_number = Column(String(50), nullable=False, index=True)
    msrp_usd = Column(Float, nullable=True)
    msrp_cad = Column(Float, nullable=True)
    page = Column(Integer, nullable=True)

    # 'table'      - SKU and price on the same printed row (highest confidence)
    # 'proximity'  - price found adjacent to the SKU in a product card
    # None         - the SKU appears in the catalog but no price was resolved
    confidence = Column(String(20), nullable=True)

    source_file = Column(String(255), nullable=True)
    extracted_at = Column(DateTime, default=utcnow)

    __table_args__ = (
        UniqueConstraint('catalog_year', 'item_number', name='uq_catalog_year_item'),
    )

    def __repr__(self):
        return (f"<CatalogEntry({self.catalog_year} {self.item_number} "
                f"${self.msrp_usd})>")

    @property
    def year_sort_key(self) -> int:
        """'2011-12' sorts as 2011."""
        try:
            return int(str(self.catalog_year)[:4])
        except (TypeError, ValueError):
            return 0


class DatabaseBackup(Base):
    """Track database backups."""
    __tablename__ = 'backups'
    
    id = Column(Integer, primary_key=True)
    backup_path = Column(String(1000), nullable=False)
    created_at = Column(DateTime, default=utcnow)
    file_size_bytes = Column(Integer, nullable=True)
    
    def __repr__(self):
        return f"<DatabaseBackup(created_at={self.created_at})>"