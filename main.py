"""
Festool Recon Monitor - Main Application
Cross-platform desktop application for monitoring festoolrecon.com
"""
import argparse
import sys
import os
import json
import logging
import queue
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Optional
import urllib.parse
import webbrowser

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QTableWidget, QTableWidgetItem, QTabWidget,
    QDialog, QLineEdit, QSpinBox, QCheckBox, QTextEdit, QComboBox,
    QMessageBox, QFileDialog, QGroupBox, QFormLayout, QAbstractItemView,
    QHeaderView, QScrollArea, QSystemTrayIcon, QMenu, QSplitter
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QObject, QThread, QByteArray
from PyQt6.QtGui import QIcon, QAction, QColor, QPixmap

from database.manager import DatabaseManager
from database.models import (
    MODE_MULTI, MODE_SINGLE, Product, UserSettings, utcnow
)
from scraper.monitor import MonitorService
from scraper.collection_scraper import DEFAULT_COLLECTION
from scraper.festool_scraper import FestoolScraper
from notifications.notifier import (
    NotificationManager, EmailNotifier, SMSNotifier,
    PushbulletNotifier, WebhookNotifier
)
from analytics.sku_analytics import SkuAnalytics, discount_pct
from analytics.product_types import resolve_type, summarize_types
from utils.crypto import decrypt_str, encrypt_str
from utils.export import DataExporter
from utils.timezone_utils import TimezoneManager, get_timezone_manager, set_timezone_mode

APP_DIR = Path(__file__).resolve().parent
LOG_DIR = APP_DIR / 'logs'
LOG_DIR.mkdir(parents=True, exist_ok=True)

# Rotating handler - the log previously grew without bound (it had reached
# several MB and had to be trimmed by hand).
_file_handler = RotatingFileHandler(
    LOG_DIR / 'festool_monitor.log',
    maxBytes=2 * 1024 * 1024,
    backupCount=5,
    encoding='utf-8'
)

if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except AttributeError:
        # Python < 3.7
        import codecs
        sys.stdout = codecs.getwriter('utf-8')(sys.stdout.buffer, 'strict')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        _file_handler,
        logging.StreamHandler(sys.stdout)
    ]
)

logger = logging.getLogger(__name__)

# Qt.ItemDataRole.UserRole holds a sort key so that clicking a column header
# orders by value, not by the displayed string ("$1,000.00" < "$99.00").
SORT_ROLE = Qt.ItemDataRole.UserRole


class SortableTableWidgetItem(QTableWidgetItem):
    """Table item that sorts on its stored sort key when it has one."""

    def __lt__(self, other):
        mine = self.data(SORT_ROLE)
        theirs = other.data(SORT_ROLE) if isinstance(other, QTableWidgetItem) else None
        if mine is not None and theirs is not None:
            return mine < theirs
        return super().__lt__(other)


# The store URL a row points at, carried on the cell itself so sorting cannot
# separate a link from the item it belongs to.
LINK_ROLE = Qt.ItemDataRole.UserRole + 1

# The item number and title a row stands for.  Read back by the context menu
# rather than re-parsing cell text, which would break the moment a column
# moved or a marker was appended to it.
ITEM_ROLE = Qt.ItemDataRole.UserRole + 2
NAME_ROLE = Qt.ItemDataRole.UserRole + 3

# What the per-item menu shows against each state
FILTER_MARKERS = {'watch': '\u2605', 'exclude': '\u2205'}


def _cell(text: str, sort_value=None) -> QTableWidgetItem:
    """Build a table cell that sorts by value when given a sort key."""
    item = SortableTableWidgetItem(text)
    if sort_value is not None:
        item.setData(SORT_ROLE, float(sort_value))
    return item


def _link_cell(text: str, url: Optional[str], sort_value=None) -> QTableWidgetItem:
    """
    A cell that opens a store page when double-clicked.

    Falls back to a plain cell when there is no URL rather than showing a link
    that goes nowhere - rows recorded before product handles were stored have
    no slug to build one from.
    """
    item = _cell(text, sort_value)
    if not url:
        return item

    item.setData(LINK_ROLE, url)
    item.setForeground(QColor('#1b4b7a'))
    font = item.font()
    font.setUnderline(True)
    item.setFont(font)
    item.setToolTip(f"Double-click to open:\n{url}")
    return item


class SettingsDialog(QDialog):
    """Settings and configuration dialog."""
    
    def __init__(self, db_manager: DatabaseManager, user_id: int, parent=None):
        super().__init__(parent)
        self.db_manager = db_manager
        self.user_id = user_id
        self.setWindowTitle("Settings")
        self.setMinimumWidth(600)
        self.setup_ui()
        self.load_settings()
        self._fit_to_screen()

    @staticmethod
    def _scrollable(widget: QWidget) -> QScrollArea:
        """
        Wrap a tab page in a scroll area.

        The notification page in particular is taller than many screens; it
        used to push the Save/Cancel buttons off the bottom of the display.
        """
        area = QScrollArea()
        area.setWidget(widget)
        area.setWidgetResizable(True)
        area.setFrameShape(QScrollArea.Shape.NoFrame)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        return area

    def _fit_to_screen(self):
        """Keep the dialog inside the available screen area."""
        screen = self.screen() or QApplication.primaryScreen()
        if not screen:
            return

        available = screen.availableGeometry()
        # Leave room for the taskbar and window decorations
        max_height = int(available.height() * 0.85)
        max_width = int(available.width() * 0.9)

        self.setMaximumHeight(max_height)
        self.resize(
            min(max(self.width(), 640), max_width),
            min(self.sizeHint().height(), max_height)
        )

        # Centre on the parent window when there is one
        parent = self.parentWidget()
        if parent:
            geo = self.frameGeometry()
            geo.moveCenter(parent.frameGeometry().center())
            # ...but never off the top/left of the screen
            geo.moveTop(max(geo.top(), available.top()))
            geo.moveLeft(max(geo.left(), available.left()))
            self.move(geo.topLeft())

    def setup_ui(self):
        """Setup the UI components."""
        layout = QVBoxLayout()

        # Tabs for different settings
        tabs = QTabWidget()

        # Monitoring settings
        tabs.addTab(self._scrollable(self.create_monitor_tab()), "Monitoring")

        # Notification settings
        tabs.addTab(self._scrollable(self.create_notification_tab()), "Notifications")


        layout.addWidget(tabs)

        # Buttons
        button_layout = QHBoxLayout()
        save_btn = QPushButton("Save")
        save_btn.setDefault(True)
        save_btn.clicked.connect(self.save_settings)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)

        button_layout.addStretch()
        button_layout.addWidget(save_btn)
        button_layout.addWidget(cancel_btn)
        layout.addLayout(button_layout)

        self.setLayout(layout)
    
    def create_monitor_tab(self) -> QWidget:
        """Create monitoring settings tab."""
        widget = QWidget()
        layout = QFormLayout()
        
        self.check_interval = QSpinBox()
        self.check_interval.setMinimum(1)
        self.check_interval.setMaximum(60)
        self.check_interval.setValue(5)
        self.check_interval.setSuffix(" minutes")
        layout.addRow("Check Interval:", self.check_interval)
        
        self.auto_backup = QCheckBox("Auto-backup database daily")
        self.auto_backup.setChecked(True)
        layout.addRow("", self.auto_backup)

        self.monitor_on_startup = QCheckBox("Start monitoring when the app opens")
        self.monitor_on_startup.setToolTip(
            "Begin monitoring automatically on launch, without pressing Start.\n"
            "The --monitor and --no-monitor command line flags override this "
            "for a single run."
        )
        layout.addRow("", self.monitor_on_startup)
        
        # Timezone preference
        layout.addRow("", QLabel(""))  # Spacer
        tz_label = QLabel("Time Display Mode:")
        tz_label.setStyleSheet("font-weight: bold;")
        layout.addRow(tz_label)
        
        self.tz_mode = QComboBox()
        self.tz_mode.addItems(["Local System Time", "UTC (Universal Time)"])
        self.tz_mode.setToolTip("Choose how to display dates and times")
        layout.addRow("Timezone:", self.tz_mode)
        
        tz_info = QLabel("Note: Changing this will update all displayed times")
        tz_info.setWordWrap(True)
        tz_info.setStyleSheet("color: gray; font-size: 10px;")
        layout.addRow("", tz_info)
        
        widget.setLayout(layout)
        return widget
    
    def create_notification_tab(self) -> QWidget:
        """Create notification settings tab."""
        widget = QWidget()
        layout = QVBoxLayout()
        
        # In-app notifications
        self.notify_inapp = QCheckBox("In-App Notifications")
        self.notify_inapp.setChecked(True)
        layout.addWidget(self.notify_inapp)
        
        # Which products notify - watchlist, exclusions, look-ahead warnings -
        # lives on the main window's Filters tab, next to the preview of what
        # those rules actually do.  It used to be split across here, a
        # Filters page in this dialog, and the right-click menus, and saving
        # this dialog quietly threw away every item excluded by right-click.
        filters_note = QLabel(
            "Which products notify you - watchlist, exclusions and look-ahead "
            "warnings - is set on the Filters tab of the main window."
        )
        filters_note.setWordWrap(True)
        filters_note.setStyleSheet("color: gray; font-size: 10px;")
        layout.addWidget(filters_note)

        # Include previous products
        prev_group = QGroupBox("Include Previous Products")
        prev_layout = QFormLayout()
        self.prev_count = QComboBox()
        self.prev_count.addItems(["0", "1", "3", "5"])
        prev_layout.addRow("Count:", self.prev_count)
        prev_group.setLayout(prev_layout)
        layout.addWidget(prev_group)
        
        # Email settings
        email_group = QGroupBox("Email Notifications")
        email_layout = QFormLayout()
        
        self.notify_email = QCheckBox("Enable Email")
        email_layout.addRow("", self.notify_email)
        
        self.email_smtp = QLineEdit()
        self.email_smtp.setPlaceholderText("smtp.gmail.com")
        email_layout.addRow("SMTP Server:", self.email_smtp)
        
        self.email_port = QSpinBox()
        self.email_port.setMaximum(65535)
        self.email_port.setValue(587)
        email_layout.addRow("Port:", self.email_port)
        
        self.email_user = QLineEdit()
        email_layout.addRow("Username:", self.email_user)
        
        self.email_pass = QLineEdit()
        self.email_pass.setEchoMode(QLineEdit.EchoMode.Password)
        email_layout.addRow("Password:", self.email_pass)
        
        self.email_to = QLineEdit()
        email_layout.addRow("To Address:", self.email_to)
        
        self.email_photos = QCheckBox("Include Photos")
        self.email_photos.setChecked(True)
        email_layout.addRow("", self.email_photos)
        
        email_test_btn = QPushButton("Test Email")
        email_test_btn.clicked.connect(self.test_email)
        email_layout.addRow("", email_test_btn)
        
        email_group.setLayout(email_layout)
        layout.addWidget(email_group)
        
        # SMS settings
        sms_group = QGroupBox("SMS Notifications")
        sms_layout = QFormLayout()
        
        self.notify_sms = QCheckBox("Enable SMS")
        sms_layout.addRow("", self.notify_sms)
        
        self.sms_provider = QComboBox()
        self.sms_provider.addItems(["Twilio"])
        sms_layout.addRow("Provider:", self.sms_provider)
        
        self.sms_key = QLineEdit()
        sms_layout.addRow("API Key:", self.sms_key)
        
        self.sms_secret = QLineEdit()
        self.sms_secret.setEchoMode(QLineEdit.EchoMode.Password)
        sms_layout.addRow("API Secret:", self.sms_secret)
        
        self.sms_from = QLineEdit()
        sms_layout.addRow("From Number:", self.sms_from)
        
        self.sms_to = QLineEdit()
        sms_layout.addRow("To Number:", self.sms_to)
        
        sms_group.setLayout(sms_layout)
        layout.addWidget(sms_group)
        
        # Pushbullet settings
        pb_group = QGroupBox("Pushbullet")
        pb_layout = QFormLayout()
        
        self.notify_pb = QCheckBox("Enable Pushbullet")
        pb_layout.addRow("", self.notify_pb)
        
        self.pb_key = QLineEdit()
        self.pb_key.setPlaceholderText("Your Pushbullet API key")
        pb_layout.addRow("API Key:", self.pb_key)
        
        pb_test_btn = QPushButton("Test Pushbullet")
        pb_test_btn.clicked.connect(self.test_pushbullet)
        pb_layout.addRow("", pb_test_btn)
        
        pb_group.setLayout(pb_layout)
        layout.addWidget(pb_group)
        
        # Webhook settings
        wh_group = QGroupBox("Webhook")
        wh_layout = QFormLayout()
        
        self.notify_webhook = QCheckBox("Enable Webhook")
        wh_layout.addRow("", self.notify_webhook)
        
        self.webhook_url = QLineEdit()
        wh_layout.addRow("URL:", self.webhook_url)
        
        self.webhook_method = QComboBox()
        self.webhook_method.addItems(["POST", "PUT", "GET"])
        wh_layout.addRow("Method:", self.webhook_method)
        
        wh_test_btn = QPushButton("Test Webhook")
        wh_test_btn.clicked.connect(self.test_webhook)
        wh_layout.addRow("", wh_test_btn)
        
        wh_group.setLayout(wh_layout)
        layout.addWidget(wh_group)
        
        layout.addStretch()
        widget.setLayout(layout)
        return widget
    
    @staticmethod
    def _dump_config(has_value: bool, config: dict) -> Optional[str]:
        """Encrypt a provider config for storage, or clear it when emptied."""
        if not has_value:
            return None
        return encrypt_str(json.dumps(config))

    @staticmethod
    def _load_config(raw: Optional[str]) -> dict:
        """Decrypt and parse a stored provider config, tolerating bad data."""
        if not raw:
            return {}
        try:
            return json.loads(decrypt_str(raw))
        except Exception as e:
            logger.warning(f"Could not read stored notification config: {e}")
            return {}

    def load_settings(self):
        """Load current settings from database."""
        with self.db_manager.get_session() as session:
            settings = session.query(UserSettings).filter_by(user_id=self.user_id).first()
            if settings:
                self.check_interval.setValue(settings.check_interval_minutes)
                self.monitor_on_startup.setChecked(
                    getattr(settings, 'monitor_on_startup', False) is True
                )
                self.notify_inapp.setChecked(settings.notify_in_app)
                self.notify_email.setChecked(settings.notify_email)
                self.notify_sms.setChecked(settings.notify_sms)
                self.notify_pb.setChecked(settings.notify_pushbullet)
                self.notify_webhook.setChecked(settings.notify_webhook)
                self.prev_count.setCurrentText(str(settings.include_previous_count))
                self.email_photos.setChecked(settings.include_photos)
                
                # Load timezone mode (handle missing column gracefully)
                try:
                    tz_mode = getattr(settings, 'timezone_mode', 'local')
                    if tz_mode:
                        self.tz_mode.setCurrentIndex(0 if tz_mode == 'local' else 1)
                except:
                    self.tz_mode.setCurrentIndex(0)  # Default to local
                
                # Load encrypted configs
                email_conf = self._load_config(settings.email_config)
                self.email_smtp.setText(email_conf.get('smtp_server', ''))
                self.email_port.setValue(email_conf.get('port', 587))
                self.email_user.setText(email_conf.get('username', ''))
                self.email_pass.setText(email_conf.get('password', ''))
                self.email_to.setText(email_conf.get('to_email', ''))

                sms_conf = self._load_config(settings.sms_config)
                self.sms_provider.setCurrentText(sms_conf.get('provider', 'Twilio'))
                self.sms_key.setText(sms_conf.get('api_key', ''))
                self.sms_secret.setText(sms_conf.get('api_secret', ''))
                self.sms_from.setText(sms_conf.get('from_number', ''))
                self.sms_to.setText(sms_conf.get('to_number', ''))

                pb_conf = self._load_config(settings.pushbullet_config)
                self.pb_key.setText(pb_conf.get('api_key', ''))

                wh_conf = self._load_config(settings.webhook_config)
                self.webhook_url.setText(wh_conf.get('url', ''))
                self.webhook_method.setCurrentText(wh_conf.get('method', 'POST'))

    def test_webhook(self):
        """Test webhook notification."""
        url = self.webhook_url.text().strip()
        method = self.webhook_method.currentText()
        
        if not url:
            QMessageBox.warning(self, "Missing URL", "Please enter a webhook URL first.")
            return
        
        try:
            from notifications.notifier import WebhookNotifier
            from utils.timezone_utils import get_timezone_manager
            
            # Get timezone manager based on current settings
            tz_mode = 'local' if self.tz_mode.currentIndex() == 0 else 'utc'
            tz_manager = get_timezone_manager(tz_mode)
            
            notifier = WebhookNotifier(url=url, method=method, tz_manager=tz_manager)
            
            # Send test notification
            test_product = {
                'name': 'Test Product - Webhook Working',
                'item_number': 'TEST123',
                'sale_price': 999.99,
                'regular_price': 1299.99,
                'posted_at': datetime.now().isoformat(),
                'image_url': None,
                'description': 'This is a test notification from Festool Recon Monitor'
            }
            
            success = notifier.send(test_product)
            
            if success:
                QMessageBox.information(
                    self, 
                    "Success!", 
                    f"Test webhook sent to {url}\n\nCheck your webhook endpoint."
                )
            else:
                QMessageBox.warning(
                    self,
                    "Send Failed",
                    "Webhook reported failure. Check the URL and try again."
                )
            
        except Exception as e:
            QMessageBox.critical(
                self,
                "Test Failed",
                f"Failed to send test webhook:\n{str(e)}"
            )
    
    def test_email(self):
        """Test email notification."""
        smtp_server = self.email_smtp.text().strip()
        port = self.email_port.value()
        username = self.email_user.text().strip()
        password = self.email_pass.text().strip()
        to_email = self.email_to.text().strip()
        
        if not all([smtp_server, username, password, to_email]):
            QMessageBox.warning(self, "Missing Information", 
                            "Please fill in all email fields first.")
            return
        
        try:
            from notifications.notifier import EmailNotifier
            from utils.timezone_utils import get_timezone_manager
            
            # Get timezone manager based on current settings
            tz_mode = 'local' if self.tz_mode.currentIndex() == 0 else 'utc'
            tz_manager = get_timezone_manager(tz_mode)
            
            notifier = EmailNotifier(
                smtp_server=smtp_server,
                port=port,
                username=username,
                password=password,
                from_email=username,
                to_email=to_email,
                tz_manager=tz_manager  # Pass timezone manager
            )
            
            # Send test notification
            test_product = {
                'name': 'Test Product - Email Working',
                'item_number': 'TEST123',
                'sale_price': 999.99,
                'regular_price': 1299.99,
                'posted_at': datetime.now(),
                'image_url': None,
                'description': 'This is a test notification from Festool Recon Monitor'
            }
            
            success = notifier.send(test_product, include_photo=False)
            
            if success:
                QMessageBox.information(
                    self, 
                    "Success!", 
                    f"Test email sent to {to_email}!\n\nCheck your inbox."
                )
            else:
                QMessageBox.warning(
                    self,
                    "Send Failed",
                    "Email sending reported failure. Check the log file for details."
                )
            
        except Exception as e:
            QMessageBox.critical(
                self,
                "Test Failed",
                f"Failed to send test email:\n{str(e)}"
            )
    
    def test_pushbullet(self):
        """Test Pushbullet notification."""
        api_key = self.pb_key.text().strip()
        
        if not api_key:
            QMessageBox.warning(self, "Missing API Key", "Please enter your Pushbullet API key first.")
            return
        
        try:
            from notifications.notifier import PushbulletNotifier
            from utils.timezone_utils import get_timezone_manager
            
            # Get timezone manager based on current settings
            tz_mode = 'local' if self.tz_mode.currentIndex() == 0 else 'utc'
            tz_manager = get_timezone_manager(tz_mode)
            
            # Create notifier with timezone manager
            notifier = PushbulletNotifier(api_key=api_key, tz_manager=tz_manager)
            
            # Send test notification
            test_product = {
                'name': 'Test Product - Pushbullet Working',
                'item_number': 'TEST123',
                'sale_price': 999.99,
                'regular_price': 1299.99,
                'posted_at': datetime.now(),
                'image_url': None,
                'description': 'This is a test notification from Festool Recon Monitor'
            }
            
            success = notifier.send(test_product)
            
            if success:
                QMessageBox.information(
                    self, 
                    "Success!", 
                    "Test notification sent! Check your Pushbullet devices."
                )
            else:
                QMessageBox.warning(
                    self,
                    "Send Failed",
                    "Notification reported failure. Check the log file for details."
                )
            
        except Exception as e:
            QMessageBox.critical(
                self,
                "Test Failed",
                f"Failed to send test notification:\n{str(e)}\n\nCheck your API key is correct."
            )
    
    def save_settings(self):
        """Save settings to database."""
        try:
            new_tz_mode = 'local' if self.tz_mode.currentIndex() == 0 else 'utc'

            # A user with no settings row would otherwise silently lose every
            # notification and monitoring preference on save.
            self.db_manager.ensure_user_settings(self.user_id)

            with self.db_manager.get_session() as session:
                settings = session.query(UserSettings).filter_by(user_id=self.user_id).first()
                if settings:
                    settings.check_interval_minutes = self.check_interval.value()
                    settings.monitor_on_startup = self.monitor_on_startup.isChecked()
                    settings.notify_in_app = self.notify_inapp.isChecked()
                    settings.notify_email = self.notify_email.isChecked()
                    settings.notify_sms = self.notify_sms.isChecked()
                    settings.notify_pushbullet = self.notify_pb.isChecked()
                    settings.notify_webhook = self.notify_webhook.isChecked()
                    settings.include_previous_count = int(self.prev_count.currentText())
                    settings.include_photos = self.email_photos.isChecked()
                    settings.timezone_mode = new_tz_mode

                    # Credentials are encrypted at rest; clearing the key field
                    # clears the stored config rather than silently keeping it.
                    settings.email_config = self._dump_config(
                        bool(self.email_smtp.text().strip()),
                        {
                            'smtp_server': self.email_smtp.text().strip(),
                            'port': self.email_port.value(),
                            'username': self.email_user.text().strip(),
                            'password': self.email_pass.text().strip(),
                            'from_email': self.email_user.text().strip(),
                            'to_email': self.email_to.text().strip()
                        }
                    )

                    settings.sms_config = self._dump_config(
                        bool(self.sms_key.text().strip()),
                        {
                            'provider': self.sms_provider.currentText(),
                            'api_key': self.sms_key.text().strip(),
                            'api_secret': self.sms_secret.text().strip(),
                            'from_number': self.sms_from.text().strip(),
                            'to_number': self.sms_to.text().strip()
                        }
                    )

                    settings.pushbullet_config = self._dump_config(
                        bool(self.pb_key.text().strip()),
                        {'api_key': self.pb_key.text().strip()}
                    )

                    settings.webhook_config = self._dump_config(
                        bool(self.webhook_url.text().strip()),
                        {
                            'url': self.webhook_url.text().strip(),
                            'method': self.webhook_method.currentText()
                        }
                    )

            # Update global timezone manager
            set_timezone_mode(new_tz_mode)
            logger.info(f"Timezone display mode set to: {new_tz_mode}")

            QMessageBox.information(self, "Success", "Settings saved successfully!")
            self.accept()
            
        except Exception as e:
            logger.error(f"Error saving settings: {e}", exc_info=True)
            QMessageBox.critical(self, "Error", f"Failed to save settings: {e}")


class LookAheadWorker(QThread):
    """
    Refreshes the look-ahead queue off the GUI thread.

    Separate from CheckWorker so a manual queue refresh does not collide with
    (or wait behind) a full product check.
    """

    finished_refresh = pyqtSignal()

    def __init__(self, monitor: MonitorService, parent=None):
        super().__init__(parent)
        self.monitor = monitor

    def run(self):
        try:
            self.monitor.refresh_look_ahead()
        except Exception as e:
            logger.error(f"Look-ahead refresh failed: {e}", exc_info=True)
        self.finished_refresh.emit()


@dataclass
class NotificationJob:
    """
    One notification, ready to send, with nothing left to look up.

    Everything is resolved on the GUI thread before the job is queued -
    settings read, credentials decrypted, providers built, payload formatted -
    so the worker only does the part that talks to the network.  That also
    means a job cannot be changed out from under the worker by a later
    rebuild, which a shared NotificationManager used to allow.
    """
    kind: str                      # 'product' | 'digest'
    manager: object                # a NotificationManager of its own
    payload: dict
    label: str                     # what to call it in a log line
    previous: list = field(default_factory=list)
    include_photo: bool = False
    product_id: Optional[int] = None   # row to log the result against

    def send(self) -> dict:
        if self.kind == 'digest':
            return self.manager.send_digest(self.payload)
        return self.manager.send_notification(
            self.payload, self.previous, self.include_photo
        )


class NotificationWorker(QThread):
    """
    Sends notifications off the GUI thread, one at a time.

    Every provider is someone else's server on the far end of a socket, and
    these sends used to happen inline on the GUI thread: an unreachable push
    service or a slow SMTP host froze the entire window for as long as the
    call took, with no way to tell the difference between that and a crash.

    One serialised queue rather than a thread per send.  The order carries
    meaning - a digest should land before the individual alerts it summarises -
    SMTP servers dislike being opened several times at once, and a send that
    is merely slow must not be able to pile up threads behind it.
    """

    # (job, {channel: succeeded})
    finished_send = pyqtSignal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._jobs = queue.Queue()
        self._stopping = False

    def submit(self, job: NotificationJob):
        """Queue a notification and return at once."""
        if self._stopping:
            logger.warning(f"Shutting down - dropping {job.label}")
            return
        self._jobs.put(job)

    def run(self):
        while True:
            job = self._jobs.get()
            if job is None:          # the stop sentinel
                break

            try:
                results = job.send()
            except Exception as e:
                # A provider that raises must not take the worker with it, or
                # every later notification dies with the first bad one.
                logger.error(f"Sending {job.label} failed: {e}", exc_info=True)
                results = {}

            if self._stopping:
                break

            self.finished_send.emit((job, results))

    def stop(self, wait_ms: int = 5000):
        """
        Ask the worker to finish and wait briefly for it.

        The wait is bounded because the thing being waited on is a network
        call.  Providers all carry their own deadline now, so this should
        never be reached - but "should" is not a reason to make quitting the
        app depend on a remote server answering.
        """
        if not self.isRunning():
            return

        self._stopping = True
        self._jobs.put(None)

        if not self.wait(wait_ms):
            logger.warning(
                "A notification was still in flight at shutdown; leaving it "
                "rather than waiting any longer"
            )


class CheckWorker(QThread):
    """
    Runs a monitor check off the GUI thread.

    The check performs network requests and sleeps between validation
    re-checks; running it inline froze the whole window for several seconds
    on startup and on every "Check Now".
    """

    finished_check = pyqtSignal(bool)

    def __init__(self, monitor: MonitorService, parent=None):
        super().__init__(parent)
        self.monitor = monitor

    def run(self):
        found = False
        try:
            found = bool(self.monitor.check_now())
        except Exception as e:
            logger.error(f"Check failed: {e}", exc_info=True)
        self.finished_check.emit(found)


class MainWindow(QMainWindow):
    """Main application window."""

    # Emitted from the monitor's background thread; Qt queues delivery so the
    # slot always runs on the GUI thread.  Touching widgets directly from the
    # scheduler thread is undefined behaviour and can crash the app.
    product_changed = pyqtSignal(object)
    queue_updated = pyqtSignal()
    new_collections_found = pyqtSignal(object)
    # (handle, sync) for a multi-item collection, and the mode-change map.
    # Both cross from the monitor thread, so they go through signals like
    # everything else that ends up touching widgets.
    collection_changed = pyqtSignal(object)
    collection_mode_changed = pyqtSignal(object)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Festool Recon Monitor")
        self.setMinimumSize(1000, 700)

        # Set window icon
        icon = self._load_icon()
        if not icon.isNull():
            self.setWindowIcon(icon)
            # Also set as application icon (shows in taskbar on Windows)
            QApplication.setWindowIcon(icon)

        # Initialize components
        self.tray_icon = None
        self.check_worker = None
        self.look_ahead_worker = None
        self._force_quit = False
        self._tray_hint_shown = False

        self.db_manager = DatabaseManager()
        self.current_user_id = self.setup_user()

        # Initialize timezone manager
        self.tz_manager = self._init_timezone_manager()

        # Initialize image cache EARLY - before UI setup
        self.image_cache = {}
        self.image_cache_max_size = 20

        # Notifications are built fresh per send (see
        # _build_notification_providers) and delivered on their own thread, so
        # there is no shared manager to keep here - only the worker.
        self.notification_worker = None
        self.analytics = SkuAnalytics(self.db_manager)

        self.notification_worker = NotificationWorker(self)
        self.notification_worker.finished_send.connect(self._on_notification_sent)
        self.notification_worker.start()

        # Marshal background detections onto the GUI thread
        self.product_changed.connect(self.handle_product_change)
        self.queue_updated.connect(self.redraw_queue_table)
        self.queue_updated.connect(self.redraw_collection_table)
        self.new_collections_found.connect(self.handle_new_collections)
        self.collection_changed.connect(self.handle_collection_change)
        self.collection_mode_changed.connect(self.handle_mode_change)

        # Setup monitor - these callbacks fire on the scheduler thread and do
        # nothing but emit signals.
        self.monitor = MonitorService(
            self.db_manager,
            on_product_change=self.product_changed.emit,
            on_queue_change=lambda result: self.queue_updated.emit(),
            on_new_collection=self.new_collections_found.emit,
            on_collection_change=lambda handle, sync: self.collection_changed.emit(
                (handle, sync)
            ),
            on_mode_change=self.collection_mode_changed.emit,
        )

        # Setup UI
        self.setup_ui()
        self.setup_system_tray()
        self.setup_timers()

        # Load initial data
        self.refresh_product_list()
        self.refresh_sku_history()
        self.update_stats()
        self.redraw_queue_table()      # from storage; the network refresh follows
        self.redraw_collection_table()

        # Check for current product and refresh the queue (in the background)
        self.check_now()
        self.refresh_look_ahead()

        logger.info("Application started")
    
    def _init_timezone_manager(self) -> TimezoneManager:
        """
        Initialize timezone manager based on user settings.
        
        Returns: TimezoneManager instance configured for user's preference
        """
        try:
            with self.db_manager.get_session() as session:
                settings = session.query(UserSettings).filter_by(user_id=self.current_user_id).first()
                
                if settings:
                    # Try to get timezone_mode (may not exist in old databases)
                    try:
                        tz_mode = settings.timezone_mode if hasattr(settings, 'timezone_mode') else 'local'
                    except AttributeError:
                        tz_mode = 'local'
                    
                    logger.info(f"Initializing timezone manager in {tz_mode} mode")
                    set_timezone_mode(tz_mode)
                    return get_timezone_manager(tz_mode)
                
        except Exception as e:
            logger.warning(f"Error loading timezone setting: {e}")
            logger.info("Defaulting to local timezone mode")
        
        # Default to local if anything goes wrong
        return get_timezone_manager('local')

    def setup_user(self) -> int:
        """Setup default user or login."""
        user_id = self.db_manager.get_or_create_default_user()
        self.db_manager.ensure_user_settings(user_id)
        return user_id
    
    def setup_ui(self):
        """Setup the main UI."""
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        
        layout = QVBoxLayout()
        
        # Top controls
        control_layout = QHBoxLayout()
        
        self.start_btn = QPushButton("Start Monitoring")
        self.start_btn.clicked.connect(self.toggle_monitoring)
        control_layout.addWidget(self.start_btn)
        
        self.check_now_btn = QPushButton("Check Now")
        self.check_now_btn.clicked.connect(self.check_now)
        control_layout.addWidget(self.check_now_btn)
        
        self.settings_btn = QPushButton("Settings")
        self.settings_btn.clicked.connect(self.show_settings)
        control_layout.addWidget(self.settings_btn)
        
        control_layout.addStretch()
        
        self.status_label = QLabel("Status: Stopped")
        control_layout.addWidget(self.status_label)
        
        layout.addLayout(control_layout)
        
        # Tabs
        tabs = QTabWidget()
        
        # Current product tab
        current_tab = self.create_current_product_tab()
        tabs.addTab(current_tab, "Current Product")
        
        # History tab
        history_tab = self.create_history_tab()
        tabs.addTab(history_tab, "History")

        # Look-ahead queue tab
        look_ahead_tab = self.create_look_ahead_tab()
        tabs.addTab(look_ahead_tab, "Look Ahead")

        # Everything on offer at once, when the store is running a sale
        collection_tab = self.create_collection_tab()
        tabs.addTab(collection_tab, "Collection")

        # SKU history tab
        sku_tab = self.create_sku_history_tab()
        tabs.addTab(sku_tab, "SKU History")

        # Every rule deciding what notifies, and a preview of what it does
        filters_tab = self.create_filters_tab()
        tabs.addTab(filters_tab, "Filters")

        # Stats tab
        stats_tab = self.create_stats_tab()
        tabs.addTab(stats_tab, "Statistics")
        
        layout.addWidget(tabs)
        
        # Menu bar
        self.setup_menu()
        
        central_widget.setLayout(layout)
    
    # The photo is scaled into this box; the facts sit beside it rather than
    # underneath, which is what left a band of empty space either side.
    PRODUCT_IMAGE_SIZE = (420, 340)

    # Individual alerts sent for watchlist matches inside one batch.  A broad
    # keyword ("cordless") can match most of a thirty-item sale, and the point
    # of the digest is that such a sale arrives as one message, not thirty.
    WATCHLIST_ALERT_LIMIT = 5

    def create_current_product_tab(self) -> QWidget:
        """
        Current product: photo on the left, the facts that decide a purchase
        beside it, and the full description underneath.

        The whole page scrolls.  It previously did not, so a long description
        was clipped at the bottom of the tab with no way to reach the rest of
        it - and the text was truncated to 300 characters on top of that.
        """
        page = QWidget()
        page_layout = QVBoxLayout()

        top = QHBoxLayout()

        self.current_product_image = QLabel()
        self.current_product_image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.current_product_image.setFixedSize(*self.PRODUCT_IMAGE_SIZE)
        self.current_product_image.setScaledContents(False)
        self.current_product_image.setStyleSheet("""
            QLabel {
                border: 2px solid #ccc;
                border-radius: 5px;
                background-color: #f9f9f9;
                padding: 10px;
            }
        """)
        top.addWidget(self.current_product_image, 0, Qt.AlignmentFlag.AlignTop)

        self.current_product_label = QLabel("No product currently loaded")
        self.current_product_label.setWordWrap(True)
        self.current_product_label.setAlignment(Qt.AlignmentFlag.AlignTop)
        # Item numbers and prices are worth copying out of the panel
        self.current_product_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextBrowserInteraction
        )
        self.current_product_label.setStyleSheet("""
            QLabel {
                padding: 16px;
                border: 2px solid #ccc;
                border-radius: 5px;
                font-size: 14px;
            }
        """)
        top.addWidget(self.current_product_label, 1)

        page_layout.addLayout(top)

        self.current_product_description = QLabel("")
        self.current_product_description.setWordWrap(True)
        self.current_product_description.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.current_product_description.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextBrowserInteraction
        )
        self.current_product_description.setStyleSheet("""
            QLabel {
                padding: 16px;
                border: 2px solid #ccc;
                border-radius: 5px;
                font-size: 13px;
            }
        """)
        self.current_product_description.hide()     # nothing to show yet
        page_layout.addWidget(self.current_product_description)

        page_layout.addStretch()
        page.setLayout(page_layout)

        area = QScrollArea()
        area.setWidget(page)
        area.setWidgetResizable(True)
        area.setFrameShape(QScrollArea.Shape.NoFrame)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        return area
    
    def create_history_tab(self) -> QWidget:
        """Create product history tab."""
        widget = QWidget()
        layout = QVBoxLayout()
        
        # Export button
        export_btn = QPushButton("Export to CSV")
        export_btn.clicked.connect(self.export_history)
        layout.addWidget(export_btn)
        
        # Table
        self.history_table = QTableWidget()
        self.history_table.setColumnCount(7)
        # "From" matters now that history holds two different kinds of
        # listing: the one-at-a-time rotation, and sales where thirty items
        # were posted together.  Without it a sale looks like the store
        # rotating thirty products in one minute.
        self.history_table.setHorizontalHeaderLabels([
            "Item Number", "Name", "Price", "Posted", "Time Live", "Replaced", "From"
        ])
        # History is a record, not an editor - cells were editable by default
        self.history_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.history_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.history_table.verticalHeader().setVisible(False)

        header = self.history_table.horizontalHeader()
        header.setStretchLastSection(True)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.history_table)
        
        widget.setLayout(layout)
        return widget
    
    def create_look_ahead_tab(self) -> QWidget:
        """Create the look-ahead queue tab."""
        widget = QWidget()
        layout = QVBoxLayout()

        # The prediction: what comes next, how often that has held, and when
        self.next_up_label = QLabel("")
        self.next_up_label.setWordWrap(True)
        self.next_up_label.setTextFormat(Qt.TextFormat.RichText)
        self.next_up_label.setStyleSheet(
            "background-color: #e8f0fb; color: #1d3f73; padding: 8px; border-radius: 4px;"
        )
        layout.addWidget(self.next_up_label)

        # The queue is a plan, not a schedule - say so up front rather than
        # letting the table imply more certainty than it has.
        caveat = QLabel(
            "The store lists these behind the current product, but the order is not a "
            "promise: items get skipped, swapped, or sell out almost immediately, so an "
            "item here may never appear as the live product."
        )
        caveat.setWordWrap(True)
        caveat.setStyleSheet(
            "background-color: #fdf1e2; color: #8a4b00;"
            "padding: 8px; border-radius: 4px;"
        )
        layout.addWidget(caveat)

        hint = QLabel(
            "Right-click an item to watch it, exclude it, or clear its filter."
        )
        hint.setStyleSheet("color: gray; font-size: 11px;")
        layout.addWidget(hint)

        controls = QHBoxLayout()
        refresh_btn = QPushButton("Refresh Look Ahead")
        refresh_btn.clicked.connect(self.refresh_look_ahead)
        controls.addWidget(refresh_btn)
        controls.addStretch()
        layout.addLayout(controls)

        self.look_ahead_status = QLabel("")
        self.look_ahead_status.setWordWrap(True)
        self.look_ahead_status.setStyleSheet("color: gray;")
        layout.addWidget(self.look_ahead_status)

        self.queue_table = QTableWidget()
        self.queue_columns = [
            "Up Next", "Item Number", "Name", "Type", "Price",
            "MSRP", "Discount", "Price Check", "In Queue"
        ]
        self.queue_table.setColumnCount(len(self.queue_columns))
        self.queue_table.setHorizontalHeaderLabels(self.queue_columns)

        # This column used to show a sighting count, which only reflected how
        # many times the app had refreshed - say plainly that it is elapsed
        # time so it cannot be read that way again.
        in_queue_header = self.queue_table.horizontalHeaderItem(
            self.queue_columns.index("In Queue")
        )
        if in_queue_header:
            in_queue_header.setToolTip(
                "How long the item has been waiting in the queue.\n"
                "Elapsed time since it first appeared - not a count of checks."
            )

        self.queue_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.queue_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.queue_table.verticalHeader().setVisible(False)
        self.queue_table.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu
        )
        self.queue_table.customContextMenuRequested.connect(
            lambda pos: self.show_filter_menu(self.queue_table, pos)
        )
        self.queue_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.queue_table)

        self.queue_confidence = QLabel("")
        self.queue_confidence.setWordWrap(True)
        self.queue_confidence.setStyleSheet("color: #444; font-size: 11px; padding-top: 4px;")
        layout.addWidget(self.queue_confidence)

        self.collections_label = QLabel("")
        self.collections_label.setWordWrap(True)
        self.collections_label.setStyleSheet("font-size: 11px; padding-top: 2px;")
        layout.addWidget(self.collections_label)

        widget.setLayout(layout)
        return widget

    def create_collection_tab(self) -> QWidget:
        """
        Everything the store is offering at once.

        Separate from Look Ahead on purpose.  That tab is a queue - an ordered
        plan that may not hold - and its whole presentation says so.  This one
        is the opposite: nothing here is waiting its turn, every row is on sale
        right now, and there is no ordering to be wrong about.
        """
        widget = QWidget()
        layout = QVBoxLayout()

        self.collection_banner = QLabel("")
        self.collection_banner.setWordWrap(True)
        self.collection_banner.setStyleSheet(
            "background-color: #eef4fb; color: #1b4b7a;"
            "padding: 8px; border-radius: 4px;"
        )
        layout.addWidget(self.collection_banner)

        controls = QHBoxLayout()
        refresh_btn = QPushButton("Refresh Collection")
        refresh_btn.clicked.connect(self.refresh_look_ahead)
        controls.addWidget(refresh_btn)

        self.collection_open_btn = QPushButton("Open in Browser")
        self.collection_open_btn.clicked.connect(self.open_active_collection)
        self.collection_open_btn.setEnabled(False)
        controls.addWidget(self.collection_open_btn)

        controls.addStretch()
        layout.addLayout(controls)

        self.collection_status = QLabel("")
        self.collection_status.setWordWrap(True)
        self.collection_status.setStyleSheet("color: gray;")
        layout.addWidget(self.collection_status)

        self.collection_table = QTableWidget()
        self.collection_columns = [
            "Collection", "Item Number", "Name", "Type", "Price",
            "MSRP", "Discount", "Price Check", "Listed"
        ]
        self.collection_hint = (
            "Double-click an item's name to open it on the store."
        )
        self.collection_table.setColumnCount(len(self.collection_columns))
        self.collection_table.setHorizontalHeaderLabels(self.collection_columns)
        self.collection_table.setSortingEnabled(True)
        self.collection_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.collection_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.collection_table.verticalHeader().setVisible(False)
        self.collection_table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.Stretch
        )
        self.collection_table.itemDoubleClicked.connect(self.open_listing)

        listed_header = self.collection_table.horizontalHeaderItem(
            self.collection_columns.index("Listed")
        )
        if listed_header:
            listed_header.setToolTip(
                "How long this item has been on offer in the collection."
            )

        layout.addWidget(self.collection_table)

        widget.setLayout(layout)
        return widget

    def active_multi_collections(self) -> list:
        """Published collections the store is currently selling all at once."""
        try:
            return [row for row in self.db_manager.get_collection_sightings(active_only=True)
                    if row.mode == MODE_MULTI]
        except Exception as e:
            logger.error(f"Could not load collection modes: {e}")
            return []

    def open_active_collection(self):
        """Open the multi-item collection in the default browser."""
        collections = self.active_multi_collections()
        if not collections:
            return
        webbrowser.open(collections[0].url)

    def open_listing(self, item: QTableWidgetItem):
        """Open the store page for a double-clicked row, if it has one."""
        url = item.data(LINK_ROLE) if item is not None else None
        if not url:
            return
        logger.info(f"Opening listing: {url}")
        webbrowser.open(url)

    def redraw_collection_table(self):
        """Redraw the live-collection table from what is stored (GUI thread only)."""
        collections = self.active_multi_collections()

        if not collections:
            self.collection_table.setRowCount(0)
            self.collection_open_btn.setEnabled(False)
            self.collection_banner.setText(
                "The store is selling one item at a time right now. This tab fills "
                "in when it opens a collection with everything on offer together - "
                "see Current Product and Look Ahead in the meantime."
            )
            self.collection_status.setText("")
            return

        self.collection_open_btn.setEnabled(True)
        names = ', '.join(f"{c.title} ({c.handle})" for c in collections)
        self.collection_banner.setText(
            f"<strong>Everything below is on sale at the same time.</strong> "
            f"Nothing here is queued or waiting its turn: {names}."
        )

        rows = []
        for collection in collections:
            try:
                rows.extend((collection, row)
                            for row in self.db_manager.get_open_listings(collection.handle))
            except Exception as e:
                logger.error(f"Could not load listings for '{collection.handle}': {e}")

        # Sorting is switched off while the table is repopulated: with it on,
        # Qt re-sorts after every setItem() and the half-built row moves out
        # from under the column index being written.
        self.collection_table.setSortingEnabled(False)
        self.collection_table.setRowCount(len(rows))

        for index, (collection, row) in enumerate(rows):
            discount = discount_pct(row.regular_price, row.sale_price)

            verdict = None
            try:
                verdict = self.analytics.evaluate_price(
                    row.item_number, row.sale_price,
                    regular_price=row.regular_price,
                    exclude_product_id=row.id,
                )
            except Exception as e:
                logger.debug(f"No price verdict for {row.item_number}: {e}")

            category, superseded = resolve_type(
                row.item_number, row.product_type, row.name
            )
            type_cell = _cell(category or "-")
            if superseded and superseded != category:
                type_cell.setToolTip(f"Store lists this as: {superseded}")

            listed_minutes = None
            if row.posted_at:
                listed_minutes = max(
                    0, int((utcnow() - row.posted_at).total_seconds() // 60)
                )
            listed_cell = _cell(
                self.tz_manager.format_duration(listed_minutes)
                if listed_minutes is not None else "-",
                sort_value=listed_minutes or 0
            )
            if row.posted_at:
                listed_cell.setToolTip(
                    f"First seen in this collection "
                    f"{self.tz_manager.to_short_display(row.posted_at)}"
                )

            cells = [
                _cell(collection.handle),
                _cell(row.item_number or "-"),
                _link_cell(row.name, row.url),
                type_cell,
                _cell(f"${row.sale_price:,.2f}" if row.sale_price else "-",
                      sort_value=row.sale_price or 0),
                _cell(f"${row.regular_price:,.2f}" if row.regular_price else "-",
                      sort_value=row.regular_price or 0),
                _cell(f"{discount:.0f}%" if discount is not None else "-",
                      sort_value=discount or 0),
                _cell(verdict.short_label if verdict is not None else "-"),
                listed_cell,
            ]

            if verdict is not None and not verdict.is_first_sighting:
                shade = ('#e6f5ec' if verdict.is_new_low
                         else '#f1f7f3' if verdict.is_best_ever else None)
                if shade:
                    for cell in cells:
                        cell.setBackground(QColor(shade))

            for column, cell in enumerate(cells):
                self.collection_table.setItem(index, column, cell)

        self.collection_table.setSortingEnabled(True)
        self.collection_table.resizeColumnsToContents()
        self.collection_table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.Stretch
        )

        total = sum(row.sale_price or 0 for _, row in rows)
        saving = sum((row.regular_price or row.sale_price or 0) - (row.sale_price or 0)
                     for _, row in rows)
        self.collection_status.setText(
            f"{len(rows)} item(s) on offer, ${total:,.2f} in total "
            f"(${saving:,.2f} off MSRP across the collection). "
            f"{self.collection_hint}"
        )

    def create_sku_history_tab(self) -> QWidget:
        """Create the per-SKU rollup tab."""
        widget = QWidget()
        layout = QVBoxLayout()

        # Controls
        controls = QHBoxLayout()

        self.sku_search = QLineEdit()
        self.sku_search.setPlaceholderText("Filter by name or item number...")
        self.sku_search.textChanged.connect(self.filter_sku_table)
        controls.addWidget(self.sku_search)

        self.sku_repeats_only = QCheckBox("Repeats only")
        self.sku_repeats_only.setToolTip("Show only items that have been listed more than once")
        self.sku_repeats_only.stateChanged.connect(self.filter_sku_table)
        controls.addWidget(self.sku_repeats_only)

        self.sku_never_listed_only = QCheckBox("Never listed only")
        self.sku_never_listed_only.setToolTip(
            "Show only items the store queued and then pulled without ever\n"
            "putting them up for sale. Their prices come from the queue feed."
        )
        self.sku_never_listed_only.stateChanged.connect(self.filter_sku_table)
        controls.addWidget(self.sku_never_listed_only)

        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self.refresh_sku_history)
        controls.addWidget(refresh_btn)

        export_btn = QPushButton("Export SKU History")
        export_btn.clicked.connect(self.export_sku_history)
        controls.addWidget(export_btn)

        layout.addLayout(controls)

        self.sku_count_label = QLabel("")
        self.sku_count_label.setStyleSheet("color: gray;")
        layout.addWidget(self.sku_count_label)

        # Table
        self.sku_table = QTableWidget()
        self.sku_columns = [
            "Item Number", "Name", "Times Listed", "Queued, Pulled",
            "First Seen", "Last Seen",
            "Last Price", "Lowest", "Highest", "MSRP", "Best Discount",
            "Avg Time Live", "Trend", "Status"
        ]
        self.sku_table.setColumnCount(len(self.sku_columns))
        self.sku_table.setHorizontalHeaderLabels(self.sku_columns)
        self.sku_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.sku_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.sku_table.setSortingEnabled(True)
        self.sku_table.verticalHeader().setVisible(False)
        self.sku_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.sku_table.itemDoubleClicked.connect(self.show_sku_detail)
        self.sku_table.setToolTip("Double-click a row to see every listing for that item")
        self.sku_table.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu
        )
        self.sku_table.customContextMenuRequested.connect(
            lambda pos: self.show_filter_menu(self.sku_table, pos)
        )

        skipped_header = self.sku_table.horizontalHeaderItem(
            self.sku_columns.index("Queued, Pulled")
        )
        if skipped_header:
            skipped_header.setToolTip(
                "Times the store lined this item up in the queue and then\n"
                "pulled it without ever putting it up for sale.\n"
                "Not the same as being listed - it was never for sale."
            )

        layout.addWidget(self.sku_table)

        hint = QLabel(
            "Double-click any row for that item's full history. Right-click to "
            "watch it, exclude it, or clear its filter."
        )
        hint.setStyleSheet("color: gray; font-size: 11px;")
        layout.addWidget(hint)

        widget.setLayout(layout)
        return widget

    # The Filters preview's "Show" choices, as (label, row test)
    FILTER_PREVIEW_VIEWS = [
        ("All items", lambda v: True),
        ("Notify when live", lambda v: v['live']),
        ("Silent when live", lambda v: not v['live']),
        ("Excluded", lambda v: v['excluded']),
        ("Watched", lambda v: v['watched']),
    ]

    DIGEST_LABELS = {
        'alert': "Own alert + digest",
        'listed': "In digest",
        'omitted': "Left out",
    }

    def create_filters_tab(self) -> QWidget:
        """
        Every rule deciding what notifies, in one place.

        Watch and exclude rules used to be split between a Settings page that
        knew only keywords and right-click menus that set item rules - and
        saving Settings rebuilt the exclusion table from its keyword box,
        silently deleting every item excluded by right-click.  Here both kinds
        sit together, and the preview underneath runs every item ever seen
        through the same check the notifier uses, so "will this notify?" is
        answered rather than inferred.
        """
        widget = QWidget()
        layout = QVBoxLayout()

        self.filters_summary = QLabel("")
        self.filters_summary.setWordWrap(True)
        self.filters_summary.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self.filters_summary)

        # Rules above, preview below, with a handle between them.  In one
        # plain layout the preview took every spare pixel and squeezed the
        # rule lists down to a single row on an ordinary-sized window.
        rules_widget = QWidget()
        rules_layout = QVBoxLayout()
        rules_layout.setContentsMargins(0, 0, 0, 0)

        rules_row = QHBoxLayout()
        self.watch_rules_table, self.watch_rule_input, self.watch_rules_box =             self._build_rule_box('watch', "Keyword or item number, e.g. domino or 577083")
        self.exclude_rules_table, self.exclude_rule_input, self.exclude_rules_box =             self._build_rule_box('exclude', "Keyword or item number, e.g. cordless or 576541")
        rules_row.addWidget(self.watch_rules_box)
        rules_row.addWidget(self.exclude_rules_box)
        rules_layout.addLayout(rules_row, stretch=1)

        rule_hint = QLabel(
            "A keyword matches any product whose name contains it. An item number "
            "matches that one item. Exclusions always win over the watchlist."
        )
        rule_hint.setWordWrap(True)
        rule_hint.setStyleSheet("color: gray; font-size: 11px;")
        rules_layout.addWidget(rule_hint)

        self.queue_warning_check = QCheckBox(
            "Also warn me when a watched item shows up in the look-ahead queue"
        )
        self.queue_warning_check.setToolTip(
            "Notifies you while a watched item is still queued behind the live "
            "product.\nOnly watched items - without a watch rule this never fires.\n"
            "Queued items are not guaranteed to go live: they can be skipped, "
            "swapped, or sell out immediately."
        )
        self.queue_warning_check.toggled.connect(self._set_queue_warnings)
        rules_layout.addWidget(self.queue_warning_check)
        rules_widget.setLayout(rules_layout)

        # The preview: every item ever seen, run through the real filter
        preview_box = QGroupBox("What would notify you")
        preview_layout = QVBoxLayout()

        controls = QHBoxLayout()
        self.filter_preview_search = QLineEdit()
        self.filter_preview_search.setPlaceholderText("Find an item by name or number...")
        self.filter_preview_search.textChanged.connect(self._apply_filter_preview)
        controls.addWidget(self.filter_preview_search)

        self.filter_preview_view = QComboBox()
        self.filter_preview_view.addItems([label for label, _ in self.FILTER_PREVIEW_VIEWS])
        self.filter_preview_view.currentIndexChanged.connect(self._apply_filter_preview)
        controls.addWidget(self.filter_preview_view)
        preview_layout.addLayout(controls)

        self.filter_preview_count = QLabel("")
        self.filter_preview_count.setStyleSheet("color: gray;")
        preview_layout.addWidget(self.filter_preview_count)

        self.filter_preview_table = QTableWidget()
        self.filter_preview_columns = [
            "Item Number", "Name", "When It Goes Live", "In A Sale",
            "Queue Warning", "Why"
        ]
        self.filter_preview_table.setColumnCount(len(self.filter_preview_columns))
        self.filter_preview_table.setHorizontalHeaderLabels(self.filter_preview_columns)
        self.filter_preview_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.filter_preview_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.filter_preview_table.setSortingEnabled(True)
        self.filter_preview_table.verticalHeader().setVisible(False)
        self._compact_rows(self.filter_preview_table)
        self.filter_preview_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.filter_preview_table.customContextMenuRequested.connect(
            lambda pos: self.show_filter_menu(self.filter_preview_table, pos)
        )

        for column, tip in (
            ("When It Goes Live",
             "When the store is selling one item at a time and this item comes up."),
            ("In A Sale",
             "When the store puts many items up at once. One digest lists every\n"
             "item that is not excluded; watched items also get their own alert."),
            ("Queue Warning",
             "An early warning while the item is still queued behind the live\n"
             "product. Watched items only, and only while the option above is on."),
        ):
            header = self.filter_preview_table.horizontalHeaderItem(
                self.filter_preview_columns.index(column)
            )
            if header:
                header.setToolTip(tip)

        preview_layout.addWidget(self.filter_preview_table)

        preview_hint = QLabel(
            "Every item seen so far. Right-click one to watch it, exclude it, or clear "
            "its rule - the same menu as on Look Ahead and SKU History."
        )
        preview_hint.setWordWrap(True)
        preview_hint.setStyleSheet("color: gray; font-size: 11px;")
        preview_layout.addWidget(preview_hint)

        preview_box.setLayout(preview_layout)

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(rules_widget)
        splitter.addWidget(preview_box)
        splitter.setChildrenCollapsible(False)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setHandleWidth(8)
        splitter.setStyleSheet(
            "QSplitter::handle { background-color: #d8d8d8; margin: 2px 40%;"
            " border-radius: 2px; }"
        )
        splitter.handle(1).setToolTip("Drag to give the rules or the preview more room")
        layout.addWidget(splitter, stretch=1)

        widget.setLayout(layout)
        return widget

    # Rows each rule list shows before it has to scroll
    RULE_ROWS_VISIBLE = 4

    @staticmethod
    def _compact_rows(table: QTableWidget):
        """Rows sized to the text, not Qt's roomy default."""
        table.verticalHeader().setDefaultSectionSize(table.fontMetrics().height() + 10)

    def _build_rule_box(self, kind: str, placeholder: str):
        """One side of the rules row: the list, an add box, a remove button."""
        box = QGroupBox()   # titled with its rule count by refresh_filters_tab()
        box_layout = QVBoxLayout()

        table = QTableWidget()
        table.setColumnCount(3)
        table.setHorizontalHeaderLabels(["Rule", "Kind", "Catches"])
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._compact_rows(table)
        table.setMinimumHeight(
            table.horizontalHeader().sizeHint().height()
            + self.RULE_ROWS_VISIBLE * table.verticalHeader().defaultSectionSize()
            + 2 * table.frameWidth() + 2
        )
        table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        table.customContextMenuRequested.connect(
            lambda pos: self._show_rule_menu(kind, table, pos)
        )
        box_layout.addWidget(table)

        add_row = QHBoxLayout()
        entry = QLineEdit()
        entry.setPlaceholderText(placeholder)
        entry.returnPressed.connect(lambda: self._add_filter_rule(kind))
        add_row.addWidget(entry)

        add_btn = QPushButton("Add")
        add_btn.clicked.connect(lambda: self._add_filter_rule(kind))
        add_row.addWidget(add_btn)

        remove_btn = QPushButton("Remove Selected")
        remove_btn.clicked.connect(lambda: self._remove_filter_rules(kind, table))
        add_row.addWidget(remove_btn)

        box_layout.addLayout(add_row)
        box.setLayout(box_layout)
        return table, entry, box

    def _known_items(self) -> dict:
        """Item number (lower-cased) -> name, for everything seen so far."""
        return {(s.item_number or '').strip().lower(): s.name
                for s in getattr(self, 'sku_summaries', None) or []
                if s.item_number}

    @staticmethod
    def _looks_like_item_number(text: str, known: dict) -> bool:
        """
        Is this an item number rather than a keyword?

        Festool item numbers are six digits; anything shorter is more likely
        part of a name ("18" for 18V) and is safer treated as a keyword.
        """
        text = (text or '').strip().lower()
        return text in known or (text.isdigit() and len(text) >= 5)

    def refresh_filters_tab(self):
        """Redraw the rule lists, the summary, and the preview."""
        if not hasattr(self, 'filter_preview_table'):
            return

        try:
            rules = self.db_manager.get_filter_rules(self.current_user_id)
        except Exception as e:
            logger.error(f"Could not read the filter rules: {e}", exc_info=True)
            self.filters_summary.setText("Could not read your filter rules.")
            return

        known = self._known_items()
        summaries = getattr(self, 'sku_summaries', None) or []

        watch_rows = [(k, 'item' if self._looks_like_item_number(k, known) else 'keyword')
                      for k in rules['watch_keywords']]
        exclude_rows = ([(i, 'item') for i in rules['exclude_items']]
                        + [(k, 'item' if self._looks_like_item_number(k, known) else 'keyword')
                           for k in rules['exclude_keywords']])
        self._fill_rule_table(self.watch_rules_table, watch_rows, known, summaries)
        self._fill_rule_table(self.exclude_rules_table, exclude_rows, known, summaries)
        # The count in the title says there is more to scroll to
        self.watch_rules_box.setTitle(
            f"Watch - notify me about these ({len(watch_rows)} rule(s))")
        self.exclude_rules_box.setTitle(
            f"Exclude - never notify me about these ({len(exclude_rows)} rule(s))")

        queue_on = self.queue_alerts_enabled
        self.queue_warning_check.blockSignals(True)
        self.queue_warning_check.setChecked(queue_on)
        self.queue_warning_check.blockSignals(False)

        # The preview
        self.filter_preview_table.setSortingEnabled(False)
        self.filter_preview_table.setRowCount(len(summaries))
        verdicts = []
        for row, s in enumerate(summaries):
            verdict = self.db_manager.explain_item(rules, s.item_number, s.name)
            verdicts.append(verdict)

            if verdict['early_warning']:
                queue_text = "Yes" if queue_on else "Off"
            else:
                queue_text = "No"

            cells = [
                _cell(s.item_number or "-"),
                _cell(s.name),
                _cell("Notifies" if verdict['live'] else "Silent"),
                _cell(self.DIGEST_LABELS[verdict['digest']]),
                _cell(queue_text),
                _cell(verdict['reason']),
            ]

            if verdict['excluded']:
                shade, ink = QColor('#fbeaea'), QColor('#8a1f1f')
            elif verdict['live']:
                shade, ink = QColor('#e6f5ec'), None
            else:
                shade, ink = None, QColor('#777777')
            for cell in cells:
                if shade:
                    cell.setBackground(shade)
                if ink:
                    cell.setForeground(ink)

            cells[1].setToolTip(s.name)
            cells[5].setToolTip(verdict['reason'])
            cells[0].setData(ITEM_ROLE, s.item_number)
            cells[0].setData(NAME_ROLE, s.name)
            for col, cell in enumerate(cells):
                self.filter_preview_table.setItem(row, col, cell)

        self.filter_preview_table.setSortingEnabled(True)
        # Every column fits its contents except Name, which takes what is
        # left.  "Why" is the column that has to stay readable; a long name
        # can elide, and its full text is on the tooltip.
        self.filter_preview_table.resizeColumnsToContents()
        self.filter_preview_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )

        self._filter_verdicts = {(s.item_number or ''): v
                                 for s, v in zip(summaries, verdicts)}
        self.filters_summary.setText(self._filters_summary_html(rules, verdicts, queue_on))
        self._apply_filter_preview()

    def _fill_rule_table(self, table: QTableWidget, rows: list,
                         known: dict, summaries: list):
        """Fill one rule list, saying what each rule actually catches."""
        table.setRowCount(len(rows))
        for index, (value, kind) in enumerate(rows):
            needle = value.strip().lower()
            if kind == 'item':
                name = known.get(needle)
                catches = _cell(name or "Not seen yet")
                if not name:
                    catches.setForeground(QColor('#777777'))
            else:
                hits = [s.name for s in summaries
                        if needle in (s.name or '').lower()
                        or needle == (s.item_number or '').strip().lower()]
                catches = _cell(f"{len(hits)} item(s) seen so far", sort_value=len(hits))
                if hits:
                    catches.setToolTip('\n'.join(hits[:15])
                                       + (f"\n...and {len(hits) - 15} more"
                                          if len(hits) > 15 else ""))

            value_cell = _cell(value)
            value_cell.setData(ITEM_ROLE, value)
            value_cell.setData(NAME_ROLE, kind)
            table.setItem(index, 0, value_cell)
            table.setItem(index, 1, _cell("Item" if kind == 'item' else "Keyword"))
            table.setItem(index, 2, catches)

        table.resizeColumnsToContents()
        table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)

    def _filters_summary_html(self, rules: dict, verdicts: list, queue_on: bool) -> str:
        """The plain-English statement of what currently notifies."""
        watch_count = len(rules['watch_keywords'])
        exclude_count = len(rules['exclude_keywords']) + len(rules['exclude_items'])
        live_count = sum(1 for v in verdicts if v['live'])

        if watch_count:
            headline = (f"Watchlist mode: only products matching one of your "
                        f"{watch_count} watch rule(s) notify you when they go live. "
                        f"Everything else is silent.")
            background, ink = '#e8f0fb', '#1d3f73'
            queue_line = ("watched items also warn you while still queued."
                          if queue_on else "off.")
        else:
            headline = ("Everything mode: your watchlist is empty, so every new product "
                        "notifies you when it goes live"
                        + (f" - except the {exclude_count} exclusion(s)." if exclude_count
                           else "."))
            background, ink = '#fdf1e2', '#8a4b00'
            queue_line = ("needs at least one watch rule, so it is not firing."
                          if queue_on else "off.")

        lines = [
            f"<b>Sales with many items at once:</b> one digest listing every item that "
            f"is not excluded; watched items also get their own alert.",
            f"<b>Look-ahead warnings:</b> {queue_line}",
            "<b>Always sent, whatever the filters:</b> a new sale collection opening.",
        ]

        channels = self._notification_channels()
        sent_to = ("<b>Sent to:</b> " + (", ".join(channels) if channels else
                                         "no channels are switched on in Settings - "
                                         "only desktop pop-ups will appear"))
        if verdicts:
            sent_to += (f" &nbsp;&middot;&nbsp; <b>{live_count}</b> of the {len(verdicts)} "
                        f"items seen so far would notify when they go live.")
        lines.append(sent_to)

        return (
            f"<div style='background-color:{background}; color:{ink}; padding:8px;'>"
            f"<b>{escape(headline)}</b><br>" + "<br>".join(lines) + "</div>"
        )

    def _notification_channels(self) -> list:
        """The channels switched on in Settings, by name."""
        try:
            with self.db_manager.get_session() as session:
                settings = session.query(UserSettings).filter_by(
                    user_id=self.current_user_id
                ).first()
                if not settings:
                    return []
                return [label for label, on in (
                    ("Email", settings.notify_email),
                    ("SMS", settings.notify_sms),
                    ("Pushbullet", settings.notify_pushbullet),
                    ("Webhook", settings.notify_webhook),
                ) if on]
        except Exception as e:
            logger.error(f"Could not read the notification channels: {e}")
            return []

    def _apply_filter_preview(self):
        """Apply the search box and the Show choice to the preview."""
        if not hasattr(self, 'filter_preview_table'):
            return

        needle = self.filter_preview_search.text().strip().lower()
        _, wanted = self.FILTER_PREVIEW_VIEWS[max(0, self.filter_preview_view.currentIndex())]
        verdicts = getattr(self, '_filter_verdicts', {})

        shown = 0
        table = self.filter_preview_table
        for row in range(table.rowCount()):
            anchor = table.item(row, 0)
            if anchor is None:
                continue
            number = anchor.data(ITEM_ROLE) or ''
            name = anchor.data(NAME_ROLE) or ''
            verdict = verdicts.get(number)

            visible = (verdict is not None and wanted(verdict)
                       and (not needle or needle in name.lower()
                            or needle in number.lower()))
            table.setRowHidden(row, not visible)
            shown += visible

        self.filter_preview_count.setText(f"Showing {shown} of {table.rowCount()} item(s)")

    def _confirm_watchlist_switch(self, rules: dict, what: str) -> bool:
        """
        Say so before the first watch rule turns everything else off.

        An empty watchlist means "notify me about everything"; one rule means
        "notify me only about this".  Watching one item by right-click is easy
        to read as "and also this", so the switch is spelled out.
        """
        if rules['watch_keywords']:
            return True

        answer = QMessageBox.question(
            self, "Switch to watchlist mode?",
            f"Your watchlist is empty, so right now every new product notifies you "
            f"(apart from exclusions).\n\n"
            f"Watching {what} switches to watchlist mode: from then on, only watched "
            f"products notify you when they go live. Everything else goes quiet.\n\n"
            f"Sale digests still list every item that is not excluded.\n\nContinue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _add_filter_rule(self, kind: str):
        """Add what is typed in a rule box as a watch or exclude rule."""
        entry = self.watch_rule_input if kind == 'watch' else self.exclude_rule_input
        value = entry.text().strip()
        if not value:
            return

        try:
            rules = self.db_manager.get_filter_rules(self.current_user_id)
            known = self._known_items()
            is_item = self._looks_like_item_number(value, known)

            if kind == 'watch' and not self._confirm_watchlist_switch(rules, f"'{value}'"):
                return

            if is_item:
                # Through the per-item path, so watching an item clears an
                # exclusion of it and vice versa, exactly as right-click does.
                self.db_manager.set_item_filter(self.current_user_id, value, kind)
            elif kind == 'watch':
                self.db_manager.add_to_watchlist(self.current_user_id, value)
            else:
                self.db_manager.add_to_exclusions(self.current_user_id, keyword=value)
        except Exception as e:
            logger.error(f"Could not add filter rule '{value}': {e}", exc_info=True)
            QMessageBox.warning(self, "Rule not added", f"Could not add '{value}':\n{e}")
            return

        if kind == 'watch' and is_item:
            self._suppress_echo_warning(value)

        entry.clear()
        self.status_label.setText(
            f"Status: '{value}' added to the "
            + ("watchlist" if kind == 'watch' else "exclusion list")
        )
        self._filters_changed()

    def _remove_filter_rules(self, kind: str, table: QTableWidget):
        """Remove the selected rules from one list."""
        rows = sorted({index.row() for index in table.selectedIndexes()})
        if not rows:
            return

        removed = []
        try:
            for row in rows:
                cell = table.item(row, 0)
                if cell is None:
                    continue
                value = cell.data(ITEM_ROLE)
                rule_kind = cell.data(NAME_ROLE)
                if kind == 'watch':
                    self.db_manager.remove_from_watchlist(self.current_user_id, value)
                elif rule_kind == 'item':
                    self.db_manager.remove_from_exclusions(
                        self.current_user_id, item_number=value, keyword=value
                    )
                else:
                    self.db_manager.remove_from_exclusions(
                        self.current_user_id, keyword=value
                    )
                removed.append(value)
        except Exception as e:
            logger.error(f"Could not remove filter rules: {e}", exc_info=True)
            QMessageBox.warning(self, "Rules not removed", f"Could not remove rules:\n{e}")

        if not removed:
            return

        message = f"Status: removed {', '.join(removed[:3])}" + (
            f" and {len(removed) - 3} more" if len(removed) > 3 else "")
        if kind == 'watch' and not self.db_manager.get_filter_rules(
                self.current_user_id)['watch_keywords']:
            message += " - watchlist now empty, so every product not excluded notifies"
        self.status_label.setText(message)
        self._filters_changed()

    def _show_rule_menu(self, kind: str, table: QTableWidget, pos):
        """Right-click on a rule list: remove."""
        if table.rowAt(pos.y()) < 0:
            return
        menu = QMenu(self)
        remove = menu.addAction("Remove")
        if menu.exec(table.viewport().mapToGlobal(pos)) is remove:
            self._remove_filter_rules(kind, table)

    def _set_queue_warnings(self, enabled: bool):
        """Save the look-ahead warning switch straight away."""
        try:
            self.db_manager.ensure_user_settings(self.current_user_id)
            with self.db_manager.get_session() as session:
                settings = session.query(UserSettings).filter_by(
                    user_id=self.current_user_id
                ).first()
                settings.notify_queue_early_warning = enabled
        except Exception as e:
            logger.error(f"Could not save the look-ahead warning setting: {e}",
                         exc_info=True)
            QMessageBox.warning(self, "Setting not saved",
                                f"Could not save the look-ahead warning setting:\n{e}")
        self.refresh_filters_tab()

    def _filters_changed(self):
        """
        Redraw everything that shows filter state.

        The queue, the SKU history and the Filters tab all mark the same
        rules; one moving without the others would be worse than no marker.
        refresh_sku_history() redraws the Filters tab once it has the items.
        """
        self.redraw_queue_table()
        self.refresh_sku_history()

    def create_stats_tab(self) -> QWidget:
        """Create statistics tab."""
        widget = QWidget()
        layout = QVBoxLayout()
        
        self.stats_text = QTextEdit()
        self.stats_text.setReadOnly(True)
        layout.addWidget(self.stats_text)
        
        refresh_btn = QPushButton("Refresh Statistics")
        refresh_btn.clicked.connect(self.update_stats)
        layout.addWidget(refresh_btn)
        
        widget.setLayout(layout)
        return widget
    
    def setup_menu(self):
        """Setup menu bar."""
        menubar = self.menuBar()
        
        # File menu
        file_menu = menubar.addMenu("File")

        # Minimize to tray option if tray icon is available
        #if hasattr(self, 'tray_icon') and self.tray_icon:
        minimize_action = QAction("Minimize to Tray", self)
        minimize_action.triggered.connect(self.hide_window)
        minimize_action.setShortcut("Ctrl+H")
        file_menu.addAction(minimize_action)
        file_menu.addSeparator()
        
        backup_action = QAction("Backup Database", self)
        backup_action.triggered.connect(self.backup_database)
        file_menu.addAction(backup_action)
        
        restore_action = QAction("Restore Database", self)
        restore_action.triggered.connect(self.restore_database)
        file_menu.addAction(restore_action)
        
        file_menu.addSeparator()
        
        exit_action = QAction("Exit", self)
        exit_action.triggered.connect(self.quit_application)
        exit_action.setShortcut("Ctrl+Q")
        file_menu.addAction(exit_action)
        
        # Help menu
        help_menu = menubar.addMenu("Help")
        
        about_action = QAction("About", self)
        about_action.triggered.connect(self.show_about)
        help_menu.addAction(about_action)
    
    def show_window(self):
        """Show and activate the main window."""
        self.show()
        self.setWindowState(self.windowState() & ~Qt.WindowState.WindowMinimized | Qt.WindowState.WindowActive)
        self.raise_()
        self.activateWindow()
        logger.debug("Window shown and activated")

    def hide_window(self):
        """Hide the window to system tray."""
        self.hide()
        logger.debug("Window hidden to tray")

    def tray_icon_activated(self, reason):
        """Handle tray icon activation (clicks)."""
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            # Double-click shows window
            if self.isVisible():
                self.hide_window()
            else:
                self.show_window()
        elif reason == QSystemTrayIcon.ActivationReason.Trigger:
            # Single click on some platforms (Linux/Windows)
            # On macOS this doesn't trigger - menu is shown instead
            pass

    def _load_icon(self) -> QIcon:
        """Load application icon from assets directory."""
        icon_path = Path(__file__).parent / 'assets' / 'icon.png'
        
        if icon_path.exists():
            icon = QIcon(str(icon_path))
            if not icon.isNull():
                logger.info(f"Loaded icon from {icon_path}")
                return icon
            else:
                logger.warning(f"Icon file exists but failed to load: {icon_path}")
        else:
            logger.warning(f"Icon file not found: {icon_path}")
        
        # Return default icon if custom icon fails
        return QIcon()

    def setup_system_tray(self):
        """Setup system tray icon."""
        if not QSystemTrayIcon.isSystemTrayAvailable():
            self.tray_icon = None
            logger.warning("System tray not available - running without tray icon")
            return

        icon = self._load_icon()
        self.tray_icon = QSystemTrayIcon(icon, self)

        # Keep a reference: a menu owned only by a local would be garbage
        # collected and the tray menu would come up empty.
        self.tray_menu = QMenu()
        for label, handler, separator_after in (
            ("Show", self.show_window, False),
            ("Settings", self.show_settings, True),
            ("Monitor", self.toggle_monitoring, True),
            ("Check Now", self.check_now, True),
            ("Quit", self.quit_application, False),
        ):
            action = QAction(label, self)
            action.triggered.connect(handler)
            self.tray_menu.addAction(action)
            if separator_after:
                self.tray_menu.addSeparator()

        self.tray_icon.activated.connect(self.tray_icon_activated)

        self.tray_icon.setContextMenu(self.tray_menu)
        self.tray_icon.setToolTip("Festool Recon Monitor")
        self.tray_icon.show()

        logger.info("System tray icon initialized")

    def _set_tray_tooltip(self, text: str):
        """Update the tray tooltip when a tray icon exists."""
        if self.tray_icon:
            self.tray_icon.setToolTip(text)

    def quit_application(self):
        """Exit the application for real (bypasses close-to-tray)."""
        self._force_quit = True
        self.close()
        QApplication.quit()
    
    def setup_timers(self):
        """Setup auto-refresh timers."""
        self.refresh_timer = QTimer()
        self.refresh_timer.timeout.connect(self.update_stats)
        # The Look Ahead time estimate moves with the clock, not with checks
        self.refresh_timer.timeout.connect(self._refresh_next_up)
        self.refresh_timer.start(60000)  # Update every minute
        
        self.backup_timer = QTimer()
        self.backup_timer.timeout.connect(self.auto_backup)
        self.backup_timer.start(24 * 60 * 60 * 1000)  # Daily backup
    
    def toggle_monitoring(self):
        """Start/stop monitoring from the button."""
        if self.monitor.is_running:
            self.stop_monitoring()
        else:
            self.start_monitoring()

    def start_monitoring(self, announce_failure: bool = True) -> bool:
        """
        Begin monitoring at the configured interval.

        Split out of toggle_monitoring() so startup can ask for monitoring
        directly.  Toggling would have been the wrong thing to call there: if
        anything had already started it, the toggle would switch it back off.
        """
        if self.monitor.is_running:
            return True

        try:
            with self.db_manager.get_session() as session:
                settings = session.query(UserSettings).filter_by(
                    user_id=self.current_user_id).first()
                interval = settings.check_interval_minutes if settings else 5

            self.monitor.start(interval)
            self.start_btn.setText("Stop Monitoring")
            self.status_label.setText(f"Status: Monitoring (every {interval} min)")
            self._set_tray_tooltip(f"Festool Recon Monitor - Monitoring ({interval} min)")
            logger.info("Monitoring started")
            return True

        except Exception as e:
            logger.error(f"Error starting monitoring: {e}", exc_info=True)
            if announce_failure:
                QMessageBox.critical(self, "Error", f"Failed to start monitoring: {e}")
            self.start_btn.setText(
                "Stop Monitoring" if self.monitor.is_running else "Start Monitoring")
            return False

    def stop_monitoring(self) -> bool:
        """Stop monitoring, leaving the window and its data alone."""
        if not self.monitor.is_running:
            return True

        try:
            self.monitor.stop()
            self.start_btn.setText("Start Monitoring")
            self.status_label.setText("Status: Stopped")
            self._set_tray_tooltip("Festool Recon Monitor - Not Monitoring")
            logger.info("Monitoring stopped")
            return True

        except Exception as e:
            logger.error(f"Error stopping monitoring: {e}", exc_info=True)
            QMessageBox.critical(self, "Error", f"Failed to stop monitoring: {e}")
            self.start_btn.setText(
                "Stop Monitoring" if self.monitor.is_running else "Start Monitoring")
            return False

    def apply_startup_monitoring(self, override: Optional[bool] = None):
        """
        Start monitoring on launch if asked to.

        ``override`` is the command line's answer (--monitor / --no-monitor)
        and holds for this run only; None means no flag was given, so the
        saved preference decides.  A flag is deliberately not written back to
        settings: a one-off launch should not silently rewrite what the app
        does every other time it opens.
        """
        if override is None:
            try:
                with self.db_manager.get_session() as session:
                    settings = session.query(UserSettings).filter_by(
                        user_id=self.current_user_id).first()
                    wanted = bool(getattr(settings, 'monitor_on_startup', False))
            except Exception as e:
                logger.error(f"Could not read the startup preference: {e}")
                return
            source = "the saved preference"
        else:
            wanted = override
            source = "the command line"

        if not wanted:
            logger.debug(f"Not starting monitoring on launch (per {source})")
            return

        logger.info(f"Starting monitoring on launch, per {source}")
        self.start_monitoring()
    
    def check_now(self):
        """Manually check for product changes (runs off the GUI thread)."""
        if self.check_worker and self.check_worker.isRunning():
            logger.info("A check is already running")
            return

        self.status_label.setText("Status: Checking...")
        self.check_now_btn.setEnabled(False)
        logger.info("Checking for product updates...")

        self.check_worker = CheckWorker(self.monitor, self)
        self.check_worker.finished_check.connect(self._on_check_finished)
        self.check_worker.start()

    def _on_check_finished(self, found: bool):
        """Update the UI once a background check completes."""
        self.check_now_btn.setEnabled(True)

        if found:
            # handle_product_change has already refreshed the views
            self.status_label.setText("Status: New product found!")
            logger.info("New product detected during manual check")
        else:
            self.status_label.setText("Status: No changes")
            logger.info("No new product detected during manual check")

    def handle_product_change(self, product: Product):
        """Handle new product detected (always runs on the GUI thread)."""
        logger.info(f"Product change detected: {product.name}")

        # The desktop pop-up obeys the same filters as every other channel;
        # it used to fire for excluded items too, which looked exactly like
        # the exclusion being ignored.
        notify = self.should_notify_product(product)
        if notify:
            self.send_notifications(product, filters_checked=True)

        # Update UI - refresh_product_list() also refreshes the current
        # product panel, so don't do that work twice.
        self.refresh_product_list()
        self.refresh_sku_history()
        self.update_stats()

        # Show system notification
        if self.tray_icon and notify:
            self.tray_icon.showMessage(
                "New Product!",
                f"{product.name}\n${product.sale_price:.2f}",
                QSystemTrayIcon.MessageIcon.Information,
                5000
            )
    
    def should_notify_product(self, product: Product) -> bool:
        """
        Check if product matches filter criteria.

        Returns True if notifications should be sent, False otherwise.

        Logic:
            1. If product matches any exclusion (keyword or item number) -> False
            2. If watchlist is empty -> True (notify all non-excluded products)
            3. If product matches any watchlist keyword -> True (notify)
            4. Otherwise -> False (not in watchlist)
        """
        try:
            # Single implementation lives on the database manager; the copy
            # that used to live here ignored item-number exclusions entirely.
            allowed = self.db_manager.should_notify_user(self.current_user_id, product)
            if allowed:
                logger.info(f"Product '{product.name}' passed notification filters")
            else:
                logger.info(f"Product '{product.name}' filtered out - notification suppressed")
            return allowed

        except Exception as e:
            logger.error(f"Error checking filters: {e}", exc_info=True)
            # On error, default to sending notification (fail open)
            return True

    def _build_notification_providers(self) -> Optional[NotificationManager]:
        """
        Build the configured notification providers from saved settings.

        Shared by the product, queue early-warning, special-sale and digest
        paths so they cannot drift apart.  Returns the manager, or None if
        nothing is configured.

        A *new* manager each time, rather than clearing and refilling one
        shared instance.  Sends happen on a worker thread now, so a queued job
        holds on to the manager it was built with; refilling a shared one
        would swap the providers out from under a send already in progress.
        Building costs a settings read and some decryption - no network - so
        doing it per job is cheap.
        """
        manager = NotificationManager(tz_manager=self.tz_manager)

        with self.db_manager.get_session() as session:
            settings = session.query(UserSettings).filter_by(
                user_id=self.current_user_id
            ).first()

            if not settings:
                return None

            if settings.notify_email and settings.email_config:
                try:
                    config = json.loads(decrypt_str(settings.email_config))
                    manager.add_provider('email', EmailNotifier(
                        smtp_server=config['smtp_server'],
                        port=config['port'],
                        username=config['username'],
                        password=config['password'],
                        from_email=config['from_email'],
                        to_email=config['to_email'],
                        tz_manager=self.tz_manager
                    ))
                except Exception as e:
                    logger.error(f"Error configuring email: {e}")

            if settings.notify_sms and settings.sms_config:
                try:
                    config = json.loads(decrypt_str(settings.sms_config))
                    manager.add_provider('sms', SMSNotifier(
                        provider=config['provider'],
                        api_key=config['api_key'],
                        api_secret=config['api_secret'],
                        from_number=config['from_number'],
                        to_number=config['to_number'],
                        tz_manager=self.tz_manager
                    ))
                except Exception as e:
                    logger.error(f"Error configuring SMS: {e}")

            if settings.notify_pushbullet and settings.pushbullet_config:
                try:
                    config = json.loads(decrypt_str(settings.pushbullet_config))
                    manager.add_provider('pushbullet', PushbulletNotifier(
                        api_key=config['api_key'],
                        tz_manager=self.tz_manager
                    ))
                except Exception as e:
                    logger.error(f"Error configuring pushbullet: {e}")

            if settings.notify_webhook and settings.webhook_config:
                try:
                    config = json.loads(decrypt_str(settings.webhook_config))
                    manager.add_provider('webhook', WebhookNotifier(
                        url=config['url'],
                        method=config['method'],
                        tz_manager=self.tz_manager
                    ))
                except Exception as e:
                    logger.error(f"Error configuring webhook: {e}")

        return manager if manager.providers else None

    def send_notifications(self, product: Product, filters_checked: bool = False):
        """Queue notifications for a new listing on every configured channel."""
        try:
            # Check filters before sending notifications
            if not filters_checked and not self.should_notify_product(product):
                logger.info(f"Skipping notifications for '{product.name}' due to filters")
                return

            with self.db_manager.get_session() as session:
                settings = session.query(UserSettings).filter_by(user_id=self.current_user_id).first()

                if not settings:
                    return

                # Get previous products if configured
                prev_products = []
                if settings.include_previous_count > 0:
                    prev = self.db_manager.get_previous_products(settings.include_previous_count + 1)
                    prev_products = [self._product_to_dict(p) for p in prev[1:]]  # Exclude current

                product_dict = self._product_to_dict(product, with_price_check=True)
                include_photos = settings.include_photos

            providers = self._build_notification_providers()
            if not providers:
                return

            self._queue_notification(NotificationJob(
                kind='product',
                manager=providers,
                payload=product_dict,
                previous=prev_products,
                include_photo=include_photos,
                product_id=product.id,
                label=f"alert for {product.item_number}",
            ))

        except Exception as e:
            logger.error(f"Error sending notifications: {e}", exc_info=True)

    def _queue_notification(self, job: NotificationJob):
        """
        Hand a ready-made notification to the worker thread.

        Nothing after this point runs on the GUI thread, which is the whole
        point: these calls reach other people's servers, and doing that inline
        is what used to freeze the window.
        """
        if not self.notification_worker:
            logger.warning(f"No notification worker - dropping {job.label}")
            return
        logger.info(f"Queued {job.label} for sending")
        self.notification_worker.submit(job)

    def _on_notification_sent(self, outcome):
        """Record and report a send, back on the GUI thread."""
        job, results = outcome

        if not results:
            logger.warning(f"No channel accepted {job.label}")
            return

        sent = [channel for channel, ok in results.items() if ok]
        failed = [channel for channel, ok in results.items() if not ok]

        for channel, ok in results.items():
            logger.info(f"{job.label} via {channel}: {'sent' if ok else 'failed'}")
            if job.product_id is None:
                continue
            try:
                self.db_manager.log_notification(
                    job.product_id, self.current_user_id,
                    channel if job.kind != 'digest' else f"{channel}:digest",
                    ok
                )
            except Exception as e:
                logger.debug(f"Could not log the {channel} notification: {e}")

        if failed:
            self.status_label.setText(
                f"Status: {job.label} failed on {', '.join(failed)}"
            )
        elif sent:
            self.status_label.setText(
                f"Status: {job.label} sent via {', '.join(sent)}"
            )
    
    @property
    def queue_alerts_enabled(self) -> bool:
        """Whether look-ahead early warnings are turned on."""
        try:
            with self.db_manager.get_session() as session:
                settings = session.query(UserSettings).filter_by(
                    user_id=self.current_user_id
                ).first()
                if not settings:
                    return False
                return getattr(settings, 'notify_queue_early_warning', True) is not False
        except Exception as e:
            logger.error(f"Could not read queue alert setting: {e}")
            return False

    def send_collection_notification(self, collection):
        """Alert through the configured channels that a new collection appeared."""
        payload = {
            'name': f"Special sale collection: {collection.title}",
            'item_number': collection.handle,
            'sale_price': 0.0,
            'regular_price': None,
            'description': (f"A collection other than the usual '{DEFAULT_COLLECTION}' "
                            f"is now published on festoolrecon.com."),
            'image_url': None,
            'posted_at': datetime.now(),
            'price_check': f"New collection '{collection.handle}' - check for a special sale",
        }

        try:
            providers = self._build_notification_providers()
            if providers:
                self._queue_notification(NotificationJob(
                    kind='product',
                    manager=providers,
                    payload=payload,
                    label=f"special-sale alert for '{collection.handle}'",
                ))
        except Exception as e:
            logger.error(f"Special-sale notification failed: {e}", exc_info=True)

    def check_queue_for_watchlist(self):
        """
        Warn early when a watched item turns up in the look-ahead queue.

        This is what the look-ahead is for: being told an item is coming while
        it is still a few places back, rather than after it is already live
        (and possibly already gone).

        Requires an explicit watchlist match. Alerting on everything that is
        merely not excluded would fire for most of the queue on every run,
        which would train you to ignore the alerts.
        """
        if not self.queue_alerts_enabled:
            return

        try:
            watchlist = self.db_manager.get_user_watchlist(self.current_user_id)
            if not [w for w in watchlist if w.keyword]:
                logger.debug("No watchlist keywords - skipping queue early warnings")
                return

            rotating = self.active_single_collection()
            if rotating is None and self.active_multi_collections():
                # Warning that something is "coming up" out of a queue the
                # store has closed would be worse than saying nothing.
                logger.debug("No rotation in progress - skipping queue early warnings")
                return

            queue_handle = rotating.handle if rotating else DEFAULT_COLLECTION
            queued = self.db_manager.get_queue(queue_handle)
        except Exception as e:
            logger.error(f"Could not check queue against watchlist: {e}")
            return

        for row in queued:
            # Position 0 is already live; it is handled by the normal flow
            if row.position == 0 or row.notified_at is not None:
                continue

            candidate = Product(
                item_number=row.item_number,
                name=row.name,
                sale_price=row.sale_price or 0.0,
                regular_price=row.regular_price,
                description=None,
            )

            if not self.db_manager.matches_watchlist(self.current_user_id, candidate):
                continue

            logger.info(
                f"Watched item queued at position {row.position}: "
                f"{row.name} (#{row.item_number})"
            )

            if self.tray_icon:
                message = str(row.name)
                if row.sale_price:
                    message += f"\n${row.sale_price:,.2f}"
                self.tray_icon.showMessage(
                    f"Coming up (#{row.position} in queue)",
                    message,
                    QSystemTrayIcon.MessageIcon.Information,
                    8000
                )

            try:
                self.send_queue_notification(row)
            except Exception as e:
                logger.error(f"Queue early-warning notification failed: {e}", exc_info=True)

            self.db_manager.mark_queue_item_notified(row.item_number, queue_handle)

    def send_queue_notification(self, row):
        """Send an early-warning notification for a queued watchlist item."""
        verdict = None
        try:
            verdict = self.analytics.evaluate_price(
                row.item_number, row.sale_price, regular_price=row.regular_price
            )
        except Exception as e:
            logger.error(f"Could not evaluate queued item price: {e}")

        note = (f"Queued at position {row.position} - not live yet, and it may be "
                f"skipped or sell out immediately.")
        if verdict is not None:
            note = f"{verdict.headline}. {note}"

        payload = {
            'name': f"Coming up: {row.name}",
            'item_number': row.item_number,
            'sale_price': row.sale_price or 0.0,
            'regular_price': row.regular_price,
            'description': None,
            'image_url': row.image_url,
            'posted_at': datetime.now(),
            'discount_pct': discount_pct(row.regular_price, row.sale_price),
            'price_check': note,
            'is_best_ever': bool(verdict and verdict.is_best_ever
                                 and not verdict.is_first_sighting),
            'is_new_low': bool(verdict and verdict.is_new_low),
            'times_listed_before': verdict.times_listed_before if verdict else 0,
        }

        providers = self._build_notification_providers()
        if not providers:
            return

        self._queue_notification(NotificationJob(
            kind='product',
            manager=providers,
            payload=payload,
            label=f"early warning for {row.item_number}",
        ))

    def _product_to_dict(self, product: Product, with_price_check: bool = False) -> dict:
        """Convert Product model to dict."""
        data = {
            'name': product.name,
            'item_number': product.item_number,
            'sale_price': product.sale_price,
            'regular_price': product.regular_price,
            'description': product.description,
            'image_url': product.image_url,
            'posted_at': product.posted_at,
            'minutes_live': product.minutes_live,
            'discount_pct': discount_pct(product.regular_price, product.sale_price),
        }

        if with_price_check:
            try:
                verdict = self.analytics.evaluate_price(
                    product.item_number,
                    product.sale_price,
                    regular_price=product.regular_price,
                    exclude_product_id=product.id,
                )
                data['price_check'] = verdict.headline
                data['is_best_ever'] = verdict.is_best_ever
                data['is_new_low'] = verdict.is_new_low
                data['times_listed_before'] = verdict.times_listed_before
                data['previous_best_price'] = verdict.previous_best
            except Exception as e:
                logger.error(f"Could not add price check to notification: {e}")

        return data
    
    def update_current_product(self, product: Product):
        """Update current product display with cached or downloaded image."""
        # Check image cache first
        if product.item_number in self.image_cache:
            logger.info(f"Using cached image for item #{product.item_number}")
            self.current_product_image.setPixmap(self.image_cache[product.item_number])
            self.current_product_image.setStyleSheet("""
                QLabel {
                    border: 2px solid #ccc;
                    border-radius: 5px;
                    background-color: #f9f9f9;
                    padding: 10px;
                }
            """)
        elif product.image_url:
            # Download and cache new image
            self._load_product_image(product.image_url, product.item_number)
        else:
            # Show placeholder if no image
            self.current_product_image.clear()
            self.current_product_image.setText("No image available")
            self.current_product_image.setStyleSheet("""
                QLabel {
                    border: 2px solid #ccc;
                    border-radius: 5px;
                    background-color: #f0f0f0;
                    color: #999;
                    font-size: 16px;
                }
            """)
        
        # Build product details text
        text = f"""<h2>{product.name}</h2>
        <p><strong>Item Number:</strong> {product.item_number}</p>
        <p><strong>Price:</strong> ${product.sale_price:,.2f}"""

        if product.regular_price:
            text += (f' <span style="text-decoration: line-through;">'
                     f'${product.regular_price:,.2f}</span>')
            saving = discount_pct(product.regular_price, product.sale_price)
            if saving:
                text += f' <span style="color: #009a44;"><strong>({saving:.0f}% off)</strong></span>'

        # Use current timezone manager for display
        posted_str = self.tz_manager.to_display(product.posted_at)
        relative_time = self.tz_manager.get_relative_time(product.posted_at)

        text += f"""</p>
        <p><strong>Posted:</strong> {posted_str} <em>({relative_time})</em></p>
        """

        text += self._price_verdict_html(product)
        text += self._catalog_html(product)

        self.current_product_label.setText(text)

        # The description gets its own pane below the photo, where it has the
        # full width to itself.  It is no longer cut at 300 characters - the
        # page scrolls, so there is room for all of it.
        if product.description:
            self.current_product_description.setText(
                f"<strong>Description</strong><br>{product.description}"
            )
            self.current_product_description.show()
        else:
            self.current_product_description.clear()
            self.current_product_description.hide()
    
    @staticmethod
    def _catalog_block(body: str, warn: bool = False) -> str:
        """Wrap catalog findings in the panel's callout styling."""
        colour, background = (("#8a4b00", "#fdf1e2") if warn
                              else ("#31708f", "#eaf3f8"))
        return (f'<p style="color: {colour}; background-color: {background}; '
                f'padding: 8px; border-radius: 4px;">{body}</p>')

    @staticmethod
    def _catalog_price_text(entry) -> str:
        """A catalog price, flagged when it was inferred rather than tabulated."""
        qualifier = "" if entry.confidence == 'table' else " <em>(approx.)</em>"
        return f"${entry.msrp_usd:,.2f}{qualifier}"

    def _catalog_html(self, product: Product) -> str:
        """
        Cross-reference the listing against Festool's printed catalogs.

        Two things the store will not tell you: what Festool's own list price
        was, and whether the model is still in the current catalog at all.

        Ordered newest edition first, because the decision being made is a
        purchase at today's price.  The newest edition an item appears in does
        not always carry a recovered price - coverage runs 81-96% - so when it
        does not, that is said plainly and the most recent edition that *does*
        is offered underneath, rather than quietly showing an older year's
        price as though it were current.

        Stays silent when no catalog index has been built (it is an optional
        offline step - see build_catalog_index.py).
        """
        try:
            history = self.db_manager.get_catalog_history(product.item_number)
            editions = self.db_manager.get_catalog_years()
        except Exception as e:
            logger.error(f"Could not read catalog data: {e}", exc_info=True)
            return ""

        if not editions:
            return ""

        oldest_indexed, newest_indexed = editions[0][0], editions[-1][0]

        if not history:
            return self._catalog_block(
                f"Not found in any catalog {oldest_indexed}&ndash;{newest_indexed} "
                f"&mdash; likely a discontinued model",
                warn=True
            )

        ordered = list(reversed(history))          # newest edition first
        latest = ordered[0]
        priced = [e for e in ordered if e.msrp_usd is not None]

        def below(entry) -> str:
            if not product.sale_price or not entry.msrp_usd:
                return ""
            pct = (entry.msrp_usd - product.sale_price) / entry.msrp_usd * 100.0
            return f" &mdash; this listing is {pct:.0f}% below it"

        lines = []
        skip = None

        if latest.msrp_usd is not None:
            lines.append(
                f"<strong>{latest.catalog_year} catalog list price:</strong> "
                f"{self._catalog_price_text(latest)}{below(latest)}"
            )
        else:
            lines.append(
                f"<strong>Listed in the {latest.catalog_year} catalog</strong>, "
                f"but no list price was recovered from that edition"
            )
            if priced:
                recent = priced[0]
                skip = recent
                lines.append(
                    f"Most recent list price: <strong>{recent.catalog_year}</strong> "
                    f"{self._catalog_price_text(recent)}{below(recent)}"
                )

        earlier = [e for e in ordered[1:] if e is not skip]
        if earlier:
            shown = ' &middot; '.join(
                f"{e.catalog_year} ${e.msrp_usd:,.0f}" if e.msrp_usd is not None
                else f"{e.catalog_year} (no price)"
                for e in earlier[:5]
            )
            more = f" &hellip; +{len(earlier) - 5} more" if len(earlier) > 5 else ""
            lines.append(f"Earlier editions: {shown}{more}")

        # Absent from the newest edition indexed: probably no longer sold new,
        # which is the thing worth knowing before buying a refurbished one.
        dropped = latest.catalog_year != newest_indexed
        if dropped:
            lines.append(f"Not in the current {newest_indexed} catalog "
                         f"&mdash; likely no longer sold new")

        return self._catalog_block("<br>".join(lines), warn=dropped)

    def _price_verdict_html(self, product: Product) -> str:
        """
        Compare this listing's price against the item's own history.

        This is the question the raw log cannot answer on its own: the same
        item recurs at different prices, so "is this a good price?" needs the
        previous listings for that item number.
        """
        try:
            verdict = self.analytics.evaluate_price(
                product.item_number,
                product.sale_price,
                regular_price=product.regular_price,
                exclude_product_id=product.id,
            )
        except Exception as e:
            logger.error(f"Could not evaluate price history: {e}", exc_info=True)
            return ""

        if verdict.is_first_sighting:
            colour, background = "#555", "#f0f0f0"
        elif verdict.is_new_low:
            colour, background = "#0a6b31", "#e6f5ec"
        elif verdict.is_best_ever:
            # As cheap as it has ever been, but not cheaper - worth noting,
            # not worth the same shout as an actual new low.
            colour, background = "#0a6b31", "#f1f7f3"
        elif verdict.delta_pct_vs_best is not None and verdict.delta_pct_vs_best > 10:
            colour, background = "#8a4b00", "#fdf1e2"
        else:
            colour, background = "#555", "#f0f0f0"

        detail = ""
        if verdict.previous_range_text:
            detail = (
                f"<br><span style='font-size: 12px; color: #666;'>"
                f"{verdict.previous_range_text}"
                f"</span>"
            )

        return (
            f"<p style='background-color: {background}; color: {colour}; "
            f"padding: 10px; border-radius: 4px;'>"
            f"<strong>Price check:</strong> {verdict.headline}{detail}</p>"
        )

    def _load_product_image(self, image_url: str, item_number: str):
        """Load product image from URL, cache it, and display."""
        try:
            import requests
            
            # Fix URL-encoded {width} placeholder
            # Handle both {width} and %7Bwidth%7D (URL-encoded version)
            if '%7Bwidth%7D' in image_url or '%7bwidth%7d' in image_url:
                # URL-decode first
                image_url = urllib.parse.unquote(image_url)
                logger.debug(f"URL-decoded image URL: {image_url[:100]}...")
            
            # Now replace {width} with actual size
            if '{width}' in image_url:
                image_url = image_url.replace('{width}', '2048')
                logger.debug(f"Replaced {{width}} with 2048: {image_url[:100]}...")
            
            logger.info(f"Downloading image for item #{item_number}...")
            logger.debug(f"Final URL: {image_url[:100]}...")
            
            # Download the image
            response = requests.get(image_url, timeout=10)
            response.raise_for_status()
            
            logger.debug(f"Downloaded {len(response.content)} bytes")
            
            # Load into QPixmap
            image_data = QByteArray(response.content)
            pixmap = QPixmap()
            pixmap.loadFromData(image_data)
            
            if pixmap.isNull():
                logger.warning("Failed to load image from data")
                self.current_product_image.clear()
                self.current_product_image.setText("Invalid image format")
                return
            
            logger.debug(f"Original image size: {pixmap.width()}x{pixmap.height()}")
            
            # Scale into the photo box.  The padding in the label's stylesheet
            # would otherwise clip a pixmap scaled to the box's full size.
            box_width, box_height = self.PRODUCT_IMAGE_SIZE
            scaled_pixmap = pixmap.scaled(
                box_width - 24, box_height - 24,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation
            )
            
            logger.info(f"Displaying image scaled to: {scaled_pixmap.width()}x{scaled_pixmap.height()}")
            
            # Cache the scaled pixmap
            self._cache_image(item_number, scaled_pixmap)
            
            # Display the image
            self.current_product_image.setPixmap(scaled_pixmap)
            self.current_product_image.setStyleSheet("""
                QLabel {
                    border: 2px solid #ccc;
                    border-radius: 5px;
                    background-color: #f9f9f9;
                    padding: 10px;
                }
            """)
            
        except requests.RequestException as e:
            logger.error(f"Failed to download image: {e}")
            self.current_product_image.clear()
            self.current_product_image.setText("Failed to download image")
            self.current_product_image.setStyleSheet("""
                QLabel {
                    border: 2px solid #ccc;
                    border-radius: 5px;
                    background-color: #f0f0f0;
                    color: #999;
                    font-size: 14px;
                }
            """)
        except Exception as e:
            logger.error(f"Error loading image: {e}", exc_info=True)
            self.current_product_image.clear()
            self.current_product_image.setText("Error loading image")
            self.current_product_image.setStyleSheet("""
                QLabel {
                    border: 2px solid #ccc;
                    border-radius: 5px;
                    background-color: #f0f0f0;
                    color: #999;
                    font-size: 14px;
                }
            """)
    
    def _cache_image(self, item_number: str, pixmap):
        """Cache a product image with size limit."""
        # Add to cache
        self.image_cache[item_number] = pixmap
        
        # Enforce cache size limit (simple FIFO)
        if len(self.image_cache) > self.image_cache_max_size:
            # Remove oldest entry (first item in dict)
            oldest_key = next(iter(self.image_cache))
            del self.image_cache[oldest_key]
            logger.debug(f"Removed image from cache: {oldest_key} (cache size: {len(self.image_cache)})")
        
        logger.debug(f"Cached image for item #{item_number} (cache size: {len(self.image_cache)})")
    
    def refresh_product_list(self):
        """Refresh product history table."""
        try:
            products = self.db_manager.get_product_history(limit=100)
        except Exception as e:
            logger.error(f"Could not load product history: {e}", exc_info=True)
            return

        self.history_table.setRowCount(len(products))
        for i, product in enumerate(products):
            self.history_table.setItem(i, 0, QTableWidgetItem(product.item_number))
            self.history_table.setItem(i, 1, QTableWidgetItem(product.name))
            self.history_table.setItem(i, 2, QTableWidgetItem(f"${product.sale_price:.2f}"))
            
            # Use timezone manager for time display
            posted_str = self.tz_manager.to_short_display(product.posted_at)
            self.history_table.setItem(i, 3, QTableWidgetItem(posted_str))
            
            # Format minutes live with duration formatter.  0 is a real
            # answer ("gone in under a minute") and must not read as "N/A",
            # which means we do not know - see Product.minutes_live.
            if product.minutes_live is not None:
                live_str = self.tz_manager.format_duration(product.minutes_live)
                self.history_table.setItem(i, 4, QTableWidgetItem(live_str))
            else:
                self.history_table.setItem(i, 4, QTableWidgetItem("N/A"))
            
            # Replaced time
            if product.replaced_at:
                replaced_str = self.tz_manager.to_short_display(product.replaced_at)
                self.history_table.setItem(i, 5, QTableWidgetItem(replaced_str))
            else:
                self.history_table.setItem(i, 5, QTableWidgetItem("Current"))

            # NULL predates collection tracking, and everything from back then
            # was the one-at-a-time rotation.
            self.history_table.setItem(
                i, 6, QTableWidgetItem(product.collection_handle or "rotation")
            )

        # The Current Product panel follows the rotation, not simply the newest
        # row: a sale posts thirty listings at once, and the newest of those is
        # not "the product currently up" in any sense this panel means.
        current = None
        try:
            current = self.db_manager.get_latest_product()
        except Exception as e:
            logger.error(f"Could not load the current rotation product: {e}")

        # A closed-out listing is not "current".  When the store stops
        # rotating, the last listing is retired (see
        # DatabaseManager.close_rotation_listing) and this panel must go with
        # it - leaving the product on screen would present something that
        # ended hours ago as the item currently for sale.
        if current is not None and current.is_live:
            self.update_current_product(current)
        else:
            self._clear_current_product(current)

    def _clear_current_product(self, last: Optional[Product] = None):
        """
        Empty the Current Product panel, saying why rather than going blank.

        ``last`` is the most recent rotation listing, already closed out, when
        there is one - worth naming so the panel reads as "that run is over"
        instead of as the app having lost track of the store.
        """
        self.current_product_image.clear()
        self.current_product_image.setText("No image available")
        self.current_product_description.clear()
        self.current_product_description.hide()

        if last is None:
            self.current_product_label.setText("No product currently loaded")
            return

        ended = (self.tz_manager.to_short_display(last.replaced_at)
                 if last.replaced_at else "at an unknown time")

        if self.active_multi_collections():
            reason = (
                "The store is selling a whole collection at once rather than "
                "rotating one item at a time - every item on offer is on the "
                "<strong>Collection</strong> tab."
            )
        else:
            reason = "The store is not showing a single product right now."

        self.current_product_label.setText(
            "<h3>Nothing is up on its own right now</h3>"
            f"<p>{reason}</p>"
            f"<p style='color: #666;'>Last item rotated: "
            f"{escape(str(last.name))} (#{escape(str(last.item_number))}), "
            f"closed {escape(str(ended))}. How long it stayed live is not "
            f"known, so it is not counted in the time-live figures.</p>"
        )
    
    def refresh_look_ahead(self):
        """Kick off a look-ahead refresh on a worker thread."""
        if self.look_ahead_worker and self.look_ahead_worker.isRunning():
            logger.debug("Look-ahead refresh already running")
            return

        self.look_ahead_status.setText("Refreshing look-ahead queue...")
        self.look_ahead_worker = LookAheadWorker(self.monitor, self)
        self.look_ahead_worker.finished_refresh.connect(self.redraw_queue_table)
        self.look_ahead_worker.finished_refresh.connect(self.redraw_collection_table)
        self.look_ahead_worker.start()

    def active_single_collection(self):
        """
        The collection the store is currently rotating one item at a time.

        Returns None when it is not rotating anything - which is a real state,
        not a failure, and the look-ahead says so rather than showing an empty
        queue as though the store had gone quiet.
        """
        try:
            for row in self.db_manager.get_collection_sightings(active_only=True):
                if row.mode == MODE_SINGLE:
                    return row
        except Exception as e:
            logger.error(f"Could not load collection modes: {e}")
        return None

    def redraw_queue_table(self):
        """Redraw the queue table from what is stored (GUI thread only)."""
        rotating = self.active_single_collection()

        if rotating is None and self.active_multi_collections():
            # The store has stopped rotating.  The last queue it published is
            # still in the database - correctly, as a record of what was
            # pending when the collection closed - but showing it here would
            # present a dead queue as the live one, which is exactly the
            # mistake this tab exists to avoid.
            self.queue_table.setRowCount(0)
            self.look_ahead_status.setText(
                "The store is not rotating one item at a time right now - it is "
                "selling a whole collection at once, so there is no queue behind "
                "anything. See the Collection tab."
            )
            self.queue_confidence.setText("")
            self.next_up_label.hide()
            self._redraw_collections_label()
            return

        # Fall back to the historical handle so an established queue still
        # renders before the first survey of a fresh install has run.
        queue_handle = rotating.handle if rotating else DEFAULT_COLLECTION

        try:
            rows = self.db_manager.get_queue(queue_handle)
            annotated = self.analytics.annotate_queue(rows)
        except Exception as e:
            logger.error(f"Could not load look-ahead queue: {e}", exc_info=True)
            self.look_ahead_status.setText("Could not load the look-ahead queue.")
            return

        # Position 0 is the live product; the look-ahead is everything behind it
        upcoming = [a for a in annotated if a['row'].position > 0]

        # One read for the whole table; classify_item() is pure from here on.
        try:
            filter_rules = self.db_manager.get_filter_rules(self.current_user_id)
        except Exception as e:
            logger.error(f"Could not read the filter rules: {e}")
            filter_rules = {'watch_keywords': [], 'exclude_keywords': [],
                            'exclude_items': []}

        self._redraw_next_up(rows, queue_handle, filter_rules)

        self.queue_table.setRowCount(len(upcoming))
        for row_index, entry in enumerate(upcoming):
            row = entry['row']
            verdict = entry['verdict']

            check_text = verdict.short_label if verdict is not None else "-"

            # Elapsed wait, not a sighting count: the old "12x" moved with the
            # check interval and with every manual refresh, so it measured how
            # often the app had looked rather than anything about the item.
            waited = row.minutes_in_queue
            in_queue_cell = _cell(
                self.tz_manager.format_duration(waited) if waited is not None else "-",
                sort_value=waited or 0
            )
            in_queue_cell.setToolTip(
                f"In the queue since {self.tz_manager.to_short_display(row.first_seen_at)}\n"
                f"(caught by {row.times_observed or 0} look-ahead refresh(es))"
            )

            # The store's own type is inconsistent ("Sanders" / "Sander", a
            # PLANEX filed as a sander); show the canonical category so the
            # column groups and sorts sensibly.
            category, superseded = resolve_type(
                row.item_number, row.product_type, row.name
            )
            type_cell = _cell(category or "-")
            if superseded and superseded != category:
                type_cell.setToolTip(f"Store lists this as: {superseded}")
            elif row.product_type and row.product_type != category:
                type_cell.setToolTip(f"Store lists this as: {row.product_type}")

            number_cell = _cell(row.item_number or "-")
            filter_state = self._filter_state_for(
                filter_rules, row.item_number, row.name
            )
            self._mark_filter_state(number_cell, filter_state)

            cells = [
                _cell(f"#{row.position}", sort_value=row.position),
                number_cell,
                _cell(row.name),
                type_cell,
                _cell(f"${row.sale_price:,.2f}" if row.sale_price else "-",
                      sort_value=row.sale_price or 0),
                _cell(f"${row.regular_price:,.2f}" if row.regular_price else "-",
                      sort_value=row.regular_price or 0),
                _cell(f"{entry['discount_pct']:.0f}%" if entry['discount_pct'] is not None else "-",
                      sort_value=entry['discount_pct'] or 0),
                _cell(check_text),
                in_queue_cell,
            ]

            # Highlight a genuine new low; a price that only matches the best
            # already seen gets the paler shade, because it is not news in the
            # same way.
            if verdict is not None and not verdict.is_first_sighting:
                shade = ('#e6f5ec' if verdict.is_new_low
                         else '#f1f7f3' if verdict.is_best_ever else None)
                if shade:
                    for cell in cells:
                        cell.setBackground(QColor(shade))

            # The context menu reads the row's identity back off column 0
            # rather than re-parsing cell text, which a marker or a moved
            # column would break.
            cells[0].setData(ITEM_ROLE, row.item_number)
            cells[0].setData(NAME_ROLE, row.name)

            for col, cell in enumerate(cells):
                self.queue_table.setItem(row_index, col, cell)

        self.queue_table.resizeColumnsToContents()
        self.queue_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)

        if upcoming:
            self.look_ahead_status.setText(
                f"{len(upcoming)} item(s) queued behind the current product. "
                f"Nearest first."
            )
        else:
            self.look_ahead_status.setText(
                "Nothing queued behind the current product right now."
            )

        # Honest statement of how well the queue has actually predicted things
        try:
            self.queue_confidence.setText(
                self.analytics.describe_queue_confidence(queue_handle)
            )
        except Exception as e:
            logger.error(f"Could not summarise queue confidence: {e}")

        self._redraw_collections_label()

        # Early warning for watched items still a few places back
        self.check_queue_for_watchlist()

    def _redraw_next_up(self, rows: list, queue_handle: str, filter_rules: dict):
        """
        The prediction at the top of Look Ahead.

        This replaced a Predictions tab that named the most common category in
        recent history and called its share "confidence".  The store's queue
        already says what is next; the useful part is how often that has held
        on this machine, and how long the current item is likely to last.
        """
        self._next_up_args = (rows, queue_handle, filter_rules)
        try:
            live = self.db_manager.get_latest_product()
            forecast = self.analytics.predict_next(rows, live, queue_handle)
        except Exception as e:
            logger.error(f"Could not predict the next item: {e}", exc_info=True)
            forecast = None

        if forecast is None:
            self.next_up_label.hide()
            return

        fmt = self.tz_manager.format_duration
        nxt = forecast['next']
        price = f" - ${nxt.sale_price:,.2f}" if nxt.sale_price else ""
        verdict = self._filter_state_for(filter_rules, nxt.item_number, nxt.name)
        if 'live' in verdict:
            alert = ("you will be notified" if verdict['live']
                     else f"you will not be notified ({verdict['reason'].lower()})")
        else:
            alert = ""

        lines = [
            f"<span style='font-size:13px'><b>Next up:</b> {escape(nxt.name)} "
            f"(#{escape(nxt.item_number or '-')}){price}</span>"
            + (f" &nbsp;&middot;&nbsp; {escape(alert)}" if alert else "")
        ]

        runner_up = forecast['runner_up']
        backup = (f" If not, the likeliest is #2: {escape(runner_up.name)}."
                  if runner_up else "")
        if forecast['hit_rate_pct'] is not None:
            lines.append(
                f"<b>How sure:</b> the front of the queue went live next "
                f"<b>{forecast['hit_rate_pct']:.0f}%</b> of the time "
                f"({forecast['hits']} of {forecast['sample']} recorded rotations).{backup}"
            )
        else:
            lines.append(
                f"<b>How sure:</b> only {forecast['sample']} rotation(s) recorded so far - "
                f"too few to say how often the front of the queue goes next.{backup}"
            )

        so_far = forecast['minutes_live_so_far']
        if so_far is not None:
            if forecast['remaining_mid'] is not None:
                lines.append(
                    f"<b>When:</b> the current item has been up {fmt(so_far)}. Of the "
                    f"{forecast['outlasted_by']} past listings that lasted that long, half "
                    f"changed within another <b>{fmt(forecast['remaining_mid'])}</b>; the "
                    f"middle half between {fmt(forecast['remaining_low'])} and "
                    f"{fmt(forecast['remaining_high'])}."
                )
            else:
                lines.append(
                    f"<b>When:</b> the current item has been up {fmt(so_far)} - longer than "
                    f"all but {forecast['outlasted_by']} past listing(s), so there is no "
                    f"useful estimate. It could change at any time."
                )

        self.next_up_label.setText("<br>".join(lines))
        self.next_up_label.show()

    def _refresh_next_up(self):
        """Re-run the prediction on the last queue drawn, for the clock's sake."""
        args = getattr(self, '_next_up_args', None)
        if args and not self.next_up_label.isHidden():
            self._redraw_next_up(*args)

    def show_filter_menu(self, table: QTableWidget, pos):
        """
        Per-item watch / exclude / clear, on right-click.

        Acts only on rules keyed to this exact item number.  Keywords that
        happen to catch the same listing are reported but never touched -
        clearing one sander must not delete a "sander" keyword that was
        covering a dozen other listings.
        """
        row = table.rowAt(pos.y())
        if row < 0:
            return

        anchor_cell = table.item(row, 0)
        if anchor_cell is None:
            return

        item_number = anchor_cell.data(ITEM_ROLE)
        name = anchor_cell.data(NAME_ROLE) or ''
        if not item_number:
            return

        try:
            rules = self.db_manager.get_filter_rules(self.current_user_id)
            state = self.db_manager.explain_item(rules, item_number, name)
        except Exception as e:
            logger.error(f"Could not read the filter rules: {e}", exc_info=True)
            return

        menu = QMenu(self)

        heading = menu.addAction(f"{name[:48]}  (#{item_number})")
        heading.setEnabled(False)
        verdict = menu.addAction(
            ("Notifies when it goes live" if state['live'] else "Silent when it goes live")
            + f" - {state['reason']}"
        )
        verdict.setEnabled(False)
        menu.addSeparator()

        watch = menu.addAction("Watch this item")
        watch.setEnabled(state['rule'] != 'watch')
        watch.setToolTip("Alert me when this item comes up")

        exclude = menu.addAction("Exclude this item")
        exclude.setEnabled(state['rule'] != 'exclude')
        exclude.setToolTip("Never alert me about this item")

        clear = menu.addAction("Clear filter for this item")
        clear.setEnabled(state['rule'] is not None)

        # Say what else is catching this row, so a cleared item that is still
        # being filtered does not look like the menu failed to work.
        for label, keywords in (("watchlist", state['watched_by']),
                                ("exclusion", state['excluded_by'])):
            if not keywords:
                continue
            menu.addSeparator()
            note = menu.addAction(
                f"Also matched by {label} keyword: {', '.join(keywords[:3])}"
            )
            note.setEnabled(False)

        chosen = menu.exec(table.viewport().mapToGlobal(pos))
        if chosen is None:
            return

        if chosen is watch:
            wanted = 'watch'
        elif chosen is exclude:
            wanted = 'exclude'
        elif chosen is clear:
            wanted = None
        else:
            return  # one of the disabled information lines

        if wanted == 'watch' and not self._confirm_watchlist_switch(rules, name or item_number):
            return

        try:
            self.db_manager.set_item_filter(
                self.current_user_id, item_number, wanted
            )
        except Exception as e:
            logger.error(f"Could not change the filter for {item_number}: {e}",
                         exc_info=True)
            QMessageBox.warning(self, "Filter not changed",
                                f"Could not update the filter for {item_number}:\n{e}")
            return

        if wanted == 'watch':
            self._suppress_echo_warning(item_number)

        message = (
            f"Status: {item_number} "
            + {'watch': 'added to the watchlist',
               'exclude': 'added to the exclusion list'}.get(wanted, 'filter cleared')
        )
        if state['rule'] == 'watch' and wanted != 'watch' and not \
                self.db_manager.get_filter_rules(self.current_user_id)['watch_keywords']:
            message += " - watchlist now empty, so every product not excluded notifies"
        self.status_label.setText(message)

        # Redraw every table that carries these markers, not just the one the
        # menu was opened on.
        self._filters_changed()

    def _suppress_echo_warning(self, item_number: str):
        """
        Do not fire a look-ahead warning for the row that was just watched.

        The early warning exists to catch an item you would otherwise miss.
        Announcing one you are looking at, a moment after you right-clicked it,
        is not that - it is an echo, and it arrives as a push on your phone.
        Marking this sighting as already-warned keeps the feature honest: the
        item still raises the full notification when it actually goes live,
        and it will warn again normally if it drops out of the queue and comes
        back later.
        """
        rotating = self.active_single_collection()
        handle = rotating.handle if rotating else DEFAULT_COLLECTION

        try:
            self.db_manager.mark_queue_item_notified(item_number, handle)
        except Exception as e:
            logger.debug(f"Could not suppress the echo warning for {item_number}: {e}")

    def _filter_state_for(self, rules: dict, item_number, name) -> dict:
        """explain_item(), but never raising into the middle of a redraw."""
        try:
            return self.db_manager.explain_item(rules, item_number, name)
        except Exception as e:
            logger.debug(f"Could not classify {item_number}: {e}")
            return {'rule': None, 'watched_by': [], 'excluded_by': []}

    @staticmethod
    def _mark_filter_state(cell: QTableWidgetItem, state: dict):
        """Show a row's filter rule on its cell, with a tooltip to explain."""
        marker = FILTER_MARKERS.get(state['rule'])
        notes = []

        if 'live' in state:
            notes.append(("Notifies" if state['live'] else "Silent")
                         + f" when it goes live - {state['reason']}")

        if state['rule'] == 'watch':
            notes.append("On your watchlist")
        elif state['rule'] == 'exclude':
            notes.append("On your exclusion list")

        if state['watched_by']:
            notes.append("Matched by watchlist keyword: "
                         + ', '.join(state['watched_by'][:3]))
        if state['excluded_by']:
            notes.append("Matched by exclusion keyword: "
                         + ', '.join(state['excluded_by'][:3]))

        if marker:
            cell.setText(f"{cell.text()} {marker}")
        if notes:
            notes.append("Right-click to change.")
            cell.setToolTip('\n'.join(notes))

    def _redraw_collections_label(self):
        """
        Show which collections are published and how each is being sold.

        The mode is the part worth reading: a collection selling everything at
        once means this tab's queue is not where the action is, and saying so
        here is cheaper than letting an empty Look Ahead imply the store has
        gone quiet.
        """
        try:
            sightings = self.db_manager.get_collection_sightings()
        except Exception as e:
            logger.error(f"Could not load collection sightings: {e}")
            return

        active = [s for s in sightings if s.is_active]

        def describe(sighting) -> str:
            if sighting.mode == MODE_MULTI:
                count = sighting.live_count
                detail = f"{count} item(s) at once" if count else "all at once"
                return f"{sighting.handle} - {detail}"
            if sighting.mode == MODE_SINGLE:
                return f"{sighting.handle} - one at a time"
            return f"{sighting.handle} - not yet established"

        if not active:
            self.collections_label.setText(
                "<span style='color: gray;'>No collections published.</span>"
            )
            return

        summary = '; '.join(describe(s) for s in active)
        multi = [s for s in active if s.mode == MODE_MULTI]
        special = [s for s in active if s.handle != DEFAULT_COLLECTION]

        if multi:
            self.collections_label.setText(
                f"<span style='color: #8a4b00;'><strong>The store is selling a whole "
                f"collection at once</strong> - see the Collection tab. {summary}</span>"
            )
        elif special:
            self.collections_label.setText(
                f"<span style='color: #8a4b00;'><strong>Special sale collection "
                f"detected:</strong> {summary}</span>"
            )
        else:
            self.collections_label.setText(
                f"<span style='color: gray;'>Collections published: {summary}</span>"
            )

    def handle_new_collections(self, collections: list):
        """A collection other than the usual one appeared - tell the user."""
        if not collections:
            return

        names = ', '.join(f"{c.title} ({c.handle})" for c in collections)
        logger.warning(f"Special sale collection detected: {names}")

        self._redraw_collections_label()

        if self.tray_icon:
            self.tray_icon.showMessage(
                "Special sale collection!",
                f"A new collection appeared on festoolrecon.com:\n{names}",
                QSystemTrayIcon.MessageIcon.Warning,
                10000
            )

        for collection in collections:
            try:
                self.send_collection_notification(collection)
                self.db_manager.mark_collection_notified(collection.handle)
            except Exception as e:
                logger.error(f"Could not send special-sale notification: {e}", exc_info=True)

    def handle_collection_change(self, payload):
        """
        A multi-item collection gained or lost listings.

        One digest covers the batch, because a sale can put thirty items up in
        a single check and thirty separate alerts would cost thirty SMS and
        teach the reader to ignore all of them.  Items that explicitly match
        the watchlist also get their own full notification on top - that is
        the case where the detail is worth the extra message.
        """
        handle, sync = payload
        added = sync.get('added') or []
        removed = sync.get('removed') or []

        logger.info(
            f"Collection '{handle}': {len(added)} new listing(s), "
            f"{len(removed)} ended"
        )

        self.redraw_collection_table()
        self.refresh_product_list()
        self.refresh_sku_history()
        self.update_stats()

        if not added:
            return

        watched = []
        try:
            watched = [p for p in added
                       if self.db_manager.matches_watchlist(self.current_user_id, p)]
        except Exception as e:
            logger.error(f"Could not match the batch against the watchlist: {e}")

        self.send_collection_digest(handle, added, removed, watched)

        for product in watched[:self.WATCHLIST_ALERT_LIMIT]:
            self.send_notifications(product)

        if len(watched) > self.WATCHLIST_ALERT_LIMIT:
            logger.warning(
                f"{len(watched)} watchlist matches in one batch - sent individual "
                f"alerts for the first {self.WATCHLIST_ALERT_LIMIT}; the rest are "
                f"in the digest"
            )

        # The pop-up counts what the digest counts: excluded items are not news
        try:
            shown = sum(1 for p in added
                        if not self.db_manager.is_excluded(self.current_user_id, p))
        except Exception as e:
            logger.error(f"Could not apply exclusions to the pop-up: {e}")
            shown = len(added)

        if self.tray_icon and shown:
            headline = (f"{len(watched)} watched item(s) among {shown} new listing(s)"
                        if watched else f"{shown} new listing(s)")
            self.tray_icon.showMessage(
                "Collection updated!",
                f"{headline} in {handle}",
                QSystemTrayIcon.MessageIcon.Information,
                8000
            )

    def handle_mode_change(self, changes: dict):
        """
        The store switched between selling one item at a time and all at once.

        Worth saying out loud rather than leaving to be inferred from a tab
        quietly filling up: it changes what every other tab means.
        """
        if not changes:
            return

        lines = []
        for handle, (previous, current) in changes.items():
            if current == MODE_MULTI:
                lines.append(f"{handle} is now selling everything at once")
            elif current == MODE_SINGLE:
                lines.append(f"{handle} is now selling one item at a time")
            logger.warning(f"Selling mode for '{handle}': {previous} -> {current}")

        self._redraw_collections_label()
        self.redraw_collection_table()

        if self.tray_icon and lines:
            self.tray_icon.showMessage(
                "The store changed how it is selling",
                "\n".join(lines),
                QSystemTrayIcon.MessageIcon.Warning,
                10000
            )

    def send_collection_digest(self, handle: str, added: list,
                               removed: list, watched: list):
        """
        One notification covering everything that just appeared in a collection.

        Exclusions are applied here and nowhere downstream: they mean "never
        tell me about this", and a digest line is still being told.  The
        watchlist is deliberately *not* applied - an empty watchlist means "no
        specific interest", not "send me nothing", and the digest is the
        overview the user asked for either way.
        """
        try:
            allowed = [p for p in added
                       if not self.db_manager.is_excluded(self.current_user_id, p)]
        except Exception as e:
            logger.error(f"Could not apply exclusions to the digest: {e}")
            allowed = list(added)

        if not allowed:
            logger.info(f"Every new listing in '{handle}' is excluded - no digest sent")
            return

        collection = next(
            (c for c in self.active_multi_collections() if c.handle == handle), None
        )

        # The same dict objects go into both lists so the digest formatters can
        # tell "watched" from "the rest" by identity and not list an item twice.
        item_dicts = {p.id: self._product_to_dict(p, with_price_check=True)
                      for p in allowed}

        digest = {
            'collection_handle': handle,
            'collection_title': collection.title if collection else handle,
            'collection_url': (collection.url if collection else
                               f"https://www.festoolrecon.com/collections/{handle}"),
            'total_live': collection.live_count if collection else len(allowed),
            'items': list(item_dicts.values()),
            'watched': [item_dicts[p.id] for p in watched if p.id in item_dicts],
            'ended': [{'name': p.name, 'item_number': p.item_number} for p in removed],
            'posted_at': datetime.now(),
        }

        try:
            providers = self._build_notification_providers()
            if not providers:
                logger.info("No notification providers configured - digest not sent")
                return

            self._queue_notification(NotificationJob(
                kind='digest',
                manager=providers,
                payload=digest,
                # Logged against one listing from the batch so the digest shows
                # up in the notification history rather than vanishing.
                product_id=allowed[0].id,
                label=f"digest for '{handle}' ({len(allowed)} item(s))",
            ))

        except Exception as e:
            logger.error(f"Collection digest failed: {e}", exc_info=True)

    def refresh_sku_history(self):
        """Rebuild the per-SKU rollup table."""
        try:
            # Near-misses included here and nowhere else - see
            # SkuAnalytics.get_sku_summaries for why this is opt-in.
            self.sku_summaries = self.analytics.get_sku_summaries(
                include_never_listed=True
            )
            filter_rules = self.db_manager.get_filter_rules(self.current_user_id)
        except Exception as e:
            logger.error(f"Could not build SKU history: {e}", exc_info=True)
            return

        # Sorting must be off while populating or rows get shuffled mid-fill
        self.sku_table.setSortingEnabled(False)
        self.sku_table.setRowCount(len(self.sku_summaries))

        for row, s in enumerate(self.sku_summaries):
            number_cell = _cell(s.item_number)
            self._mark_filter_state(
                number_cell,
                self._filter_state_for(filter_rules, s.item_number, s.name)
            )

            cells = [
                number_cell,
                _cell(s.name),
                _cell(f"{s.times_listed}", sort_value=s.times_listed),
                _cell(f"{s.times_skipped}" if s.times_skipped else "-",
                      sort_value=s.times_skipped),
                _cell(self.tz_manager.to_date_display(s.first_seen),
                      sort_value=s.first_seen.timestamp() if s.first_seen else 0),
                _cell(self.tz_manager.to_date_display(s.last_seen),
                      sort_value=s.last_seen.timestamp() if s.last_seen else 0),
                _cell(f"${s.last_price:,.2f}", sort_value=s.last_price),
                _cell(f"${s.lowest_price:,.2f}", sort_value=s.lowest_price),
                _cell(f"${s.highest_price:,.2f}", sort_value=s.highest_price),
                _cell(f"${s.regular_price:,.2f}" if s.regular_price else "-",
                      sort_value=s.regular_price or 0),
                _cell(f"{s.best_discount_pct:.0f}%" if s.best_discount_pct is not None else "-",
                      sort_value=s.best_discount_pct or 0),
                _cell(self.tz_manager.format_duration(int(s.avg_minutes_live))
                      if s.avg_minutes_live is not None else "-",
                      sort_value=s.avg_minutes_live or 0),
                _cell({'down': "↓ lower", 'up': "↑ higher",
                       'flat': "= same", 'n/a': "-"}[s.price_trend]),
                _cell("Never listed" if s.never_listed
                      else "Listed now" if s.is_current
                      else f"{s.days_since_last_seen}d ago"
                      if s.days_since_last_seen is not None else "-",
                      sort_value=-1 if s.is_current else (s.days_since_last_seen or 0)),
            ]

            if s.never_listed:
                # This item was never for sale, and every price on the row is
                # what the store was advertising while it sat in the queue.
                # Colour alone would not carry that, so the Status column says
                # it in words and the tooltip says the rest.
                note = (
                    f"Never listed. The store queued this {s.times_skipped} "
                    f"time(s) and pulled it without ever putting it up for "
                    f"sale.\nThe prices shown are what it was advertised at "
                    f"in the queue, not prices it was ever sold at."
                )
                for cell in cells:
                    cell.setBackground(QColor('#f2f2f2'))
                    cell.setForeground(QColor('#555'))
                    font = cell.font()
                    font.setItalic(True)
                    cell.setFont(font)
                    cell.setToolTip(note)

            # The filter menu reads identity off column 0 rather than the
            # cell text, which now carries a filter marker.
            cells[0].setData(ITEM_ROLE, s.item_number)
            cells[0].setData(NAME_ROLE, s.name)

            for col, item in enumerate(cells):
                self.sku_table.setItem(row, col, item)

        self.sku_table.setSortingEnabled(True)
        self.sku_table.resizeColumnsToContents()
        self.sku_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.filter_sku_table()

        # The Filters preview runs over these same items
        self.refresh_filters_tab()

    def filter_sku_table(self):
        """Apply the search box and 'repeats only' filter to the SKU table."""
        if not hasattr(self, 'sku_summaries'):
            return

        needle = self.sku_search.text().strip().lower()
        repeats_only = self.sku_repeats_only.isChecked()
        never_listed_only = self.sku_never_listed_only.isChecked()
        visible = 0

        for row in range(self.sku_table.rowCount()):
            number_item = self.sku_table.item(row, 0)
            name_item = self.sku_table.item(row, 1)
            times_item = self.sku_table.item(row, 2)
            if not (number_item and name_item and times_item):
                continue

            haystack = f"{number_item.text()} {name_item.text()}".lower()
            matches = needle in haystack if needle else True

            try:
                times_listed = int(times_item.text())
            except ValueError:
                times_listed = None

            if repeats_only and times_listed is not None:
                matches = matches and times_listed > 1

            if never_listed_only and times_listed is not None:
                matches = matches and times_listed == 0

            self.sku_table.setRowHidden(row, not matches)
            if matches:
                visible += 1

        total = len(self.sku_summaries)
        repeat_count = sum(1 for s in self.sku_summaries if s.is_repeat)
        never_listed = sum(1 for s in self.sku_summaries if s.never_listed)

        label = (f"Showing {visible} of {total} items  |  "
                 f"{repeat_count} listed more than once")
        if never_listed:
            label += (f"  |  {never_listed} queued and pulled without ever "
                      f"being listed")
        self.sku_count_label.setText(label)

    def _catalog_history_html(self, item_number: str) -> str:
        """
        List-price history for an item across the printed catalogs.

        Empty when no catalog index has been built, or when the item never
        appeared in one - in which case describe_catalog_presence() has
        already said so in a single line.
        """
        try:
            history = self.db_manager.get_catalog_history(item_number)
            presence = self.analytics.describe_catalog_presence(item_number)
        except Exception as e:
            logger.error(f"Could not load catalog history: {e}")
            return ""

        if not presence:
            return ""

        html = f"<p><em>{presence}</em></p>"

        priced = [e for e in history if e.msrp_usd is not None]
        if len(priced) < 2:
            return html

        # Newest edition first: the current list price is the one being judged
        # against, and older ones are context behind it.
        cells = " ".join(
            f"<td align='right'>{e.catalog_year}<br>"
            f"<strong>${e.msrp_usd:,.2f}</strong></td>"
            for e in reversed(priced)
        )

        # ...but the trend is still measured oldest to newest, and names both
        # years so the direction cannot be misread against the reversed table.
        oldest, newest = priced[0], priced[-1]
        change = newest.msrp_usd - oldest.msrp_usd
        arrow = "&uarr;" if change > 0 else ("&darr;" if change < 0 else "")
        movement = (f" &nbsp; list price {arrow} ${abs(change):,.2f} "
                    f"from {oldest.catalog_year} to {newest.catalog_year}"
                    if change else "")

        return (html + f"<p><strong>Catalog list price</strong> "
                f"(newest first){movement}</p>"
                f"<table cellpadding='4'><tr>{cells}</tr></table>")

    def show_sku_detail(self, item: QTableWidgetItem):
        """Show everything recorded for the double-clicked item number."""
        number_item = self.sku_table.item(item.row(), 0)
        if not number_item:
            return

        # From the role, not the cell text: the text carries a filter marker.
        item_number = number_item.data(ITEM_ROLE) or number_item.text()

        try:
            listings = self.analytics.get_listings_for_sku(item_number)
            skips = self.analytics.get_skips_for_sku(item_number)
        except Exception as e:
            logger.error(f"Could not load history for {item_number}: {e}", exc_info=True)
            QMessageBox.critical(self, "Error", f"Could not load listing history: {e}")
            return

        # An item that was only ever queued and pulled has no listings at all,
        # which used to close this dialog before it opened.  Its queue history
        # is the whole story worth telling about it.
        if not listings and not skips:
            return

        summary = next(
            (s for s in getattr(self, 'sku_summaries', []) if s.item_number == item_number),
            None
        )

        rows = ""
        for l in listings:
            disc = l.discount_pct
            rows += (
                "<tr>"
                f"<td>{self.tz_manager.to_short_display(l.posted_at)}</td>"
                f"<td>{self.tz_manager.to_short_display(l.replaced_at) if l.replaced_at else '<i>current</i>'}</td>"
                f"<td align='right'>${l.sale_price:,.2f}</td>"
                f"<td align='right'>{f'${l.regular_price:,.2f}' if l.regular_price else '-'}</td>"
                f"<td align='right'>{f'{disc:.0f}%' if disc is not None else '-'}</td>"
                f"<td align='right'>{self.tz_manager.format_duration(l.minutes_live) if l.minutes_live is not None else '-'}</td>"
                "</tr>"
            )

        title = (listings[0].name if listings
                 else skips[0].name if skips else item_number)
        header = f"<h3>{escape(str(title))}</h3><p><strong>Item #{item_number}</strong>"

        if summary is not None and summary.never_listed:
            header += "</p>" + self._never_listed_block(summary)
        elif summary:
            # One listing has no spread, and neither does a run of listings all
            # at the same price; printing "low $X | high $X" for either implies
            # a movement that never happened.
            spread = summary.highest_price - summary.lowest_price
            if summary.times_listed == 1:
                prices = f"at ${summary.lowest_price:,.2f}"
            elif abs(spread) < 0.01:
                prices = f"always ${summary.lowest_price:,.2f}"
            else:
                prices = (f"low ${summary.lowest_price:,.2f}"
                          f" &nbsp;|&nbsp; high ${summary.highest_price:,.2f}")
            header += (
                f" &nbsp;|&nbsp; listed {summary.times_listed}x"
                f" &nbsp;|&nbsp; {prices}"
            )
            if summary.years_seen:
                header += f" &nbsp;|&nbsp; seen in {', '.join(str(y) for y in summary.years_seen)}"
            header += "</p>"
        else:
            header += "</p>"

        # Where the item sits in Festool's printed catalogs - the one price
        # reference on this screen the store does not control.
        header += self._catalog_history_html(item_number)

        dialog = QDialog(self)
        dialog.setWindowTitle(f"Listing History - {item_number}")
        dialog.setMinimumSize(760, 420)

        listing_table = ""
        if listings:
            listing_table = (
                "<table width='100%' cellpadding='6' cellspacing='0' border='1' "
                "style='border-collapse: collapse;'>"
                "<tr><th align='left'>Posted</th><th align='left'>Replaced</th>"
                "<th align='right'>Price</th><th align='right'>MSRP</th>"
                "<th align='right'>Discount</th><th align='right'>Time Live</th></tr>"
                f"{rows}</table>"
            )

        layout = QVBoxLayout()
        body = QTextEdit()
        body.setReadOnly(True)
        body.setHtml(header + listing_table + self._queue_skips_html(skips))
        layout.addWidget(body)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(dialog.accept)
        button_row = QHBoxLayout()
        button_row.addStretch()
        button_row.addWidget(close_btn)
        layout.addLayout(button_row)

        dialog.setLayout(layout)
        dialog.exec()

    @staticmethod
    def _never_listed_block(summary) -> str:
        """Say plainly that nothing below was ever for sale."""
        return (
            "<p style='background-color: #fdf1e2; color: #8a4b00; padding: 8px; "
            "border-radius: 4px;'><strong>Never listed.</strong> The store put "
            f"this in the queue {summary.times_skipped} time(s) and pulled it "
            "without ever putting it up for sale. The prices below are what it "
            "was advertised at while it waited - it has never been seen selling "
            "at any of them.</p>"
        )

    def _queue_skips_html(self, skips: list) -> str:
        """
        Queue sightings that ended without the item ever going live.

        Shown for any item that has them, listed or not: for something with
        real listings this is context (the store lined it up again and changed
        its mind), and for something with none it is the entire record.
        """
        if not skips:
            return ""

        rows = ""
        for row in sorted(skips, key=lambda r: r.removed_at or datetime.min,
                          reverse=True):
            closest = (row.best_position if row.best_position is not None
                       else row.position)
            rows += (
                "<tr>"
                f"<td>{self.tz_manager.to_short_display(row.first_seen_at)}</td>"
                f"<td>{self.tz_manager.to_short_display(row.removed_at)}</td>"
                f"<td align='right'>"
                f"{f'${row.sale_price:,.2f}' if row.sale_price else '-'}</td>"
                f"<td align='right'>#{closest}</td>"
                f"<td align='right'>"
                f"{self.tz_manager.format_duration(row.minutes_in_queue)}</td>"
                "</tr>"
            )

        return (
            "<h4 style='margin-top: 20px;'>Queued and pulled</h4>"
            "<p style='color: #666; font-size: 11px;'>Times the store lined "
            "this up and then took it away again without ever fronting it. "
            "These were never for sale.</p>"
            "<table width='100%' cellpadding='6' cellspacing='0' border='1' "
            "style='border-collapse: collapse;'>"
            "<tr><th align='left'>Entered queue</th><th align='left'>Pulled</th>"
            "<th align='right'>Queued at</th><th align='right'>Closest</th>"
            "<th align='right'>Waited</th></tr>"
            f"{rows}</table>"
        )

    def export_sku_history(self):
        """Export the SKU rollup to CSV."""
        filename, _ = QFileDialog.getSaveFileName(
            self,
            "Export SKU History",
            f"festool_sku_history_{datetime.now().strftime('%Y%m%d')}.csv",
            "CSV Files (*.csv)"
        )

        if not filename:
            return

        try:
            if not Path(filename).suffix:
                filename += '.csv'
            self.analytics.export_sku_history(filename)
            count = len(self.analytics.get_sku_summaries())
            QMessageBox.information(
                self, "Success", f"Exported {count} unique items to\n{filename}"
            )
        except Exception as e:
            logger.error(f"SKU export failed: {e}", exc_info=True)
            QMessageBox.critical(self, "Error", f"Export failed: {e}")

    def update_stats(self):
        """Update statistics display."""
        try:
            stats = self.db_manager.get_statistics()
            catalog = self.analytics.get_catalog_summary()
        except Exception as e:
            logger.error(f"Could not load statistics: {e}", exc_info=True)
            return

        def pct(value):
            return f"{value:.1f}%" if value is not None else "-"

        def mins(value):
            return self.tz_manager.format_duration(int(value)) if value else "-"

        years = catalog['years_covered']
        year_span = f"{years[0]}-{years[-1]}" if len(years) > 1 else (str(years[0]) if years else "-")

        text = f"""
        <h3>Catalog Overview</h3>
        <table cellpadding="4">
        <tr><td><strong>Unique items tracked</strong></td><td>{catalog['total_skus']}</td></tr>
        <tr><td><strong>Total listings recorded</strong></td><td>{catalog['total_listings']}</td></tr>
        <tr><td><strong>From the one-at-a-time rotation</strong></td>
            <td>{catalog.get('rotation_listings', catalog['total_listings'])}</td></tr>
        <tr><td><strong>From multi-item sale collections</strong></td>
            <td>{catalog.get('multi_item_listings', 0)}</td></tr>
        <tr><td><strong>Items listed more than once</strong></td>
            <td>{catalog['repeat_skus']} ({catalog['repeat_rate_pct']:.0f}%)</td></tr>
        <tr><td><strong>Years covered</strong></td><td>{year_span}</td></tr>
        <tr><td><strong>Tracking since</strong></td>
            <td>{self.tz_manager.to_date_display(catalog['first_listing'])}</td></tr>
        <tr><td><strong>Listing rate</strong></td>
            <td>{f"{catalog['listings_per_week']:.1f} per week" if catalog['listings_per_week'] else '-'}</td></tr>
        <tr><td><strong>Busiest day</strong></td><td>{catalog['busiest_weekday'] or '-'}</td></tr>
        </table>

        <h3>Discounts off MSRP</h3>
        <table cellpadding="4">
        <tr><td><strong>Median discount</strong></td><td>{pct(catalog['median_discount_pct'])}</td></tr>
        <tr><td><strong>Average discount</strong></td><td>{pct(catalog['avg_discount_pct'])}</td></tr>
        <tr><td><strong>Deepest discount seen</strong></td><td>{pct(catalog['max_discount_pct'])}</td></tr>
        </table>

        <h3>How long listings last</h3>
        <table cellpadding="4">
        <tr><td><strong>Median time live</strong></td><td>{mins(catalog['median_minutes_live'])}</td></tr>
        <tr><td><strong>Average time live</strong> <span style="color:#888;">(rotation only)</span></td>
            <td>{mins(catalog['avg_minutes_live'])}</td></tr>
        </table>

        <h3>Price mix</h3>
        <table cellpadding="4">
        """
        total_listings = max(catalog['total_listings'], 1)
        for band, count in catalog['price_bands'].items():
            text += (f"<tr><td><strong>{band}</strong></td>"
                     f"<td>{count} ({count / total_listings * 100:.0f}%)</td></tr>")
        text += "</table>"

        # Optional: only present once build_catalog_index.py has been run
        try:
            editions = self.db_manager.get_catalog_years()
        except Exception as e:
            logger.error(f"Could not read catalog index: {e}")
            editions = []

        if editions:
            indexed = sum(total for _, total, _ in editions)
            text += (f"<h3>Printed catalogs</h3>"
                     f"<p>{len(editions)} edition(s) indexed "
                     f"({editions[0][0]}-{editions[-1][0]}), "
                     f"{indexed:,} catalogued item(s).</p>"
                     f"<table cellpadding=\"4\">")
            for year, total, priced in editions:
                text += (f"<tr><td><strong>{year}</strong></td>"
                         f"<td>{total:,} items</td>"
                         f"<td>{priced:,} with a list price</td></tr>")
            text += "</table>"

        if catalog['type_counts']:
            text += "<h3>Tool categories</h3><table cellpadding=\"4\">"
            for category, count in catalog['type_counts']:
                text += (f"<tr><td><strong>{category}</strong></td>"
                         f"<td>{count} ({count / total_listings * 100:.0f}%)</td></tr>")
            text += "</table>"

        if catalog['most_listed']:
            text += "<h3>Most frequently listed</h3><ol>"
            for s in catalog['most_listed']:
                text += (f"<li>{s.name} (#{s.item_number}) - "
                         f"{s.times_listed}x, low ${s.lowest_price:,.2f}</li>")
            text += "</ol>"

        if catalog['best_deals']:
            text += "<h3>Deepest discounts seen</h3><ol>"
            for s in catalog['best_deals']:
                text += (f"<li>{s.name} (#{s.item_number}) - "
                         f"{s.best_discount_pct:.0f}% off, ${s.lowest_price:,.2f}</li>")
            text += "</ol>"

        text += f"""
        <h3>Application</h3>
        <table cellpadding="4">
        <tr><td><strong>Notifications sent</strong></td><td>{stats['total_notifications']}</td></tr>
        <tr><td><strong>Monitoring</strong></td>
            <td>{'Running' if self.monitor.is_running else 'Stopped'}</td></tr>
        </table>
        """

        self.stats_text.setHtml(text)
    
    def show_settings(self):
        """Show settings dialog with proper timezone handling."""
        was_monitoring = self.monitor.is_running
        old_interval = 5
        old_tz_mode = 'local'
        
        # STEP 1: Capture CURRENT state before opening dialog
        with self.db_manager.get_session() as session:
            settings = session.query(UserSettings).filter_by(user_id=self.current_user_id).first()
            if settings:
                old_interval = settings.check_interval_minutes
                # Safely get timezone_mode (might not exist in old databases)
                try:
                    old_tz_mode = getattr(settings, 'timezone_mode', 'local')
                except AttributeError:
                    old_tz_mode = 'local'
        
        logger.info(f"Opening settings dialog - Current timezone: {old_tz_mode}")
        
        # STEP 2: Show settings dialog
        dialog = SettingsDialog(self.db_manager, self.current_user_id, self)
        
        # STEP 3: Process changes if user clicked Save
        if dialog.exec() == QDialog.DialogCode.Accepted:
            logger.info("Settings saved - processing changes...")
            
            # Get NEW settings from database (already saved by dialog)
            new_tz_mode = 'local'
            new_interval = 5
            
            with self.db_manager.get_session() as session:
                settings = session.query(UserSettings).filter_by(user_id=self.current_user_id).first()
                if settings:
                    new_interval = settings.check_interval_minutes
                    try:
                        new_tz_mode = getattr(settings, 'timezone_mode', 'local')
                    except AttributeError:
                        new_tz_mode = 'local'
            
            # STEP 4: Detect what changed
            tz_changed = (old_tz_mode != new_tz_mode)
            interval_changed = (old_interval != new_interval)
            
            logger.info(f"Timezone changed: {tz_changed} ({old_tz_mode} -> {new_tz_mode})")
            logger.info(f"Interval changed: {interval_changed} ({old_interval} -> {new_interval})")
            
            # STEP 5: Handle timezone change
            if tz_changed:
                logger.info("Processing timezone change...")
                
                # Update global timezone setting
                set_timezone_mode(new_tz_mode)
                logger.info(f"Global timezone mode set to: {new_tz_mode}")
                
                # Reload our timezone manager instance
                self.tz_manager = get_timezone_manager(new_tz_mode)
                logger.info(f"Timezone manager reloaded - mode: {self.tz_manager.mode}")
                
                # Nothing to rebuild for notifications: each send builds its
                # own providers from self.tz_manager, which has just been
                # replaced above, so the next one picks the new mode up.

                # Refresh ALL displays
                logger.info("Refreshing UI with new timezone...")
                
                self.refresh_product_list()
                self.update_stats()
                
                latest = self.db_manager.get_latest_product()
                if latest:
                    self.update_current_product(latest)
                    logger.info("Current product display refreshed")
                
                # Show confirmation
                tz_name = "Local System Time" if new_tz_mode == 'local' else "UTC (Universal Time)"
                QMessageBox.information(
                    self,
                    "Timezone Updated",
                    f"✓ Display timezone changed to {tz_name}\n\n"
                    f"All times have been updated."
                )
                
                logger.info("Timezone change complete")
            
            # STEP 6: Handle interval change (only if monitoring is running)
            if interval_changed and was_monitoring:
                logger.info(f"Updating monitoring interval to {new_interval} minutes...")
                
                try:
                    self.monitor.update_interval(new_interval)
                    self.status_label.setText(f"Status: Monitoring (every {new_interval} min)")
                    logger.info("Interval updated successfully")
                    
                except Exception as e:
                    logger.error(f"Error updating interval: {e}", exc_info=True)
                    QMessageBox.warning(
                        self,
                        "Interval Update Failed",
                        f"Failed to update check interval: {e}\n\n"
                        f"Please stop and restart monitoring."
                    )
            
            logger.info(f"Settings applied - Timezone: {new_tz_mode}, Interval: {new_interval}")
        
        else:
            logger.info("Settings dialog cancelled - no changes applied")
    
    def export_history(self):
        """Export product history to CSV, Excel or JSON."""
        filename, selected_filter = QFileDialog.getSaveFileName(
            self,
            "Export Product History",
            f"festool_history_{datetime.now().strftime('%Y%m%d')}.csv",
            "CSV Files (*.csv);;Excel Files (*.xlsx);;JSON Files (*.json)"
        )

        if not filename:
            return

        # Honour the chosen filter when the user did not type an extension
        suffix = Path(filename).suffix.lower()
        if not suffix:
            suffix = {'Excel': '.xlsx', 'JSON': '.json'}.get(
                selected_filter.split(' ')[0], '.csv'
            )
            filename += suffix

        try:
            products = self.db_manager.get_product_history()
            target = Path(filename)
            exporter = DataExporter(export_dir=str(target.parent))

            if suffix == '.xlsx':
                exporter.export_to_excel(products, target.name)
            elif suffix == '.json':
                exporter.export_to_json(products, target.name)
            else:
                exporter.export_to_csv(products, target.name)

            QMessageBox.information(
                self, "Success",
                f"Exported {len(products)} products to\n{filename}"
            )

        except Exception as e:
            logger.error(f"Export failed: {e}", exc_info=True)
            QMessageBox.critical(self, "Error", f"Export failed: {e}")
    
    def backup_database(self):
        """Manually backup database."""
        try:
            backup_path = self.db_manager.backup_database()
            QMessageBox.information(self, "Success", f"Database backed up to:\n{backup_path}")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Backup failed: {e}")
    
    def auto_backup(self):
        """Automatic daily backup."""
        try:
            self.db_manager.backup_database()
            logger.info("Automatic backup completed")
        except Exception as e:
            logger.error(f"Auto-backup failed: {e}")
    
    def restore_database(self):
        """Restore database from backup."""
        filename, _ = QFileDialog.getOpenFileName(
            self,
            "Select Backup File",
            "backups/",
            "Database Files (*.db)"
        )
        
        if filename:
            reply = QMessageBox.question(
                self,
                "Confirm Restore",
                "This will replace your current database. Continue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            
            if reply == QMessageBox.StandardButton.Yes:
                was_monitoring = self.monitor.is_running
                try:
                    if was_monitoring:
                        self.monitor.stop()
                        self.start_btn.setText("Start Monitoring")
                        self.status_label.setText("Status: Stopped")

                    self.db_manager.restore_from_backup(filename)

                    # The restored database has a different product history;
                    # drop cached state that referred to the old one.
                    self.image_cache.clear()
                    self.monitor._refresh_last_seen()

                    self.refresh_product_list()
                    self.update_stats()

                    QMessageBox.information(
                        self, "Success",
                        "Database restored successfully!\n\n"
                        "A copy of the previous database was saved in the backups folder."
                    )

                except Exception as e:
                    logger.error(f"Restore failed: {e}", exc_info=True)
                    QMessageBox.critical(self, "Error", f"Restore failed: {e}")
    
    def show_about(self):
        """Show about dialog."""
        QMessageBox.about(
            self,
            "About Festool Recon Monitor",
            """<h3>Festool Recon Monitor v1.0</h3>
            <p>Monitor festoolrecon.com for product changes and receive notifications.</p>
            <p><strong>Features:</strong></p>
            <ul>
            <li>Real-time monitoring</li>
            <li>Multi-channel notifications</li>
            <li>Product history tracking</li>
            <li>Look-ahead: what goes live next, and roughly when</li>
            </ul>
            """
        )
    
    def closeEvent(self, event):
        """
        Handle window close.

        With a system tray available, closing hides to the tray and monitoring
        continues (as documented); use File -> Exit or the tray's Quit to
        actually leave.
        """
        if self.tray_icon and not self._force_quit:
            event.ignore()
            self.hide_window()

            if not self._tray_hint_shown:
                self.tray_icon.showMessage(
                    "Still running",
                    "Festool Recon Monitor is still running in the tray. "
                    "Use Quit from the tray menu to exit.",
                    QSystemTrayIcon.MessageIcon.Information,
                    4000
                )
                self._tray_hint_shown = True
            return

        logger.info("Application closing...")

        # Stop timers first so nothing new is scheduled during teardown
        if hasattr(self, 'refresh_timer'):
            self.refresh_timer.stop()
        if hasattr(self, 'backup_timer'):
            self.backup_timer.stop()

        # Let in-flight workers finish before tearing things down
        for worker, label in ((self.check_worker, "check"),
                              (self.look_ahead_worker, "look-ahead refresh")):
            if worker and worker.isRunning():
                logger.info(f"Waiting for in-progress {label} to finish...")
                worker.wait(10000)

        # The notification worker gets a shorter leash than the others: what it
        # is waiting on is a remote server, and quitting must not depend on one
        # answering.  Anything still queued is dropped rather than delivered
        # late to a process that is going away.
        if self.notification_worker:
            logger.info("Stopping the notification worker...")
            self.notification_worker.stop()

        # Properly shutdown monitoring
        if self.monitor:
            logger.info("Shutting down monitor...")
            self.monitor.shutdown()
            QThread.msleep(300)

        if self.tray_icon:
            self.tray_icon.hide()

        logger.info("Application closed")
        event.accept()


def parse_args(argv):
    """
    Read our own flags, leaving Qt's for Qt.

    parse_known_args, not parse_args: Qt takes arguments of its own
    (-platform, -style and friends) and argparse would reject them outright.
    The leftovers are handed straight to QApplication.
    """
    parser = argparse.ArgumentParser(
        prog='main.py',
        description='Monitor festoolrecon.com for product changes.',
    )

    monitoring = parser.add_mutually_exclusive_group()
    monitoring.add_argument(
        '--monitor', dest='monitor', action='store_true', default=None,
        help='start monitoring as soon as the window opens, whatever the '
             'saved preference says',
    )
    monitoring.add_argument(
        '--no-monitor', dest='monitor', action='store_false',
        help='open without starting monitoring, whatever the saved preference '
             'says. Affects this launch only - it does not reach an instance '
             'that is already running',
    )

    args, qt_args = parser.parse_known_args(argv[1:])
    return args, [argv[0]] + qt_args


def main(argv=None):
    """Application entry point."""
    args, qt_args = parse_args(list(sys.argv if argv is None else argv))

    app = QApplication(qt_args)
    app.setApplicationName("Festool Recon Monitor")

    # Closing the window hides to the tray; without this Qt would quit the
    # process as soon as the last window closed.
    app.setQuitOnLastWindowClosed(False)

    try:
        window = MainWindow()
    except Exception as e:
        logger.critical(f"Failed to start: {e}", exc_info=True)
        QMessageBox.critical(
            None,
            "Festool Recon Monitor - Startup Failed",
            f"The application could not start:\n\n{e}\n\n"
            f"See logs/festool_monitor.log for details."
        )
        return 1

    window.show()

    # After show(), so a failure to start monitoring has a window to report
    # itself against rather than a message box floating on its own.
    window.apply_startup_monitoring(args.monitor)

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())