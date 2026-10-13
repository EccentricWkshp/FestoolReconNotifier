"""Notification system for multiple delivery methods."""
import json
import logging
import smtplib
import requests
from abc import ABC, abstractmethod
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from html import escape
from typing import List, Optional, Dict
from datetime import date, datetime

from utils.crypto import get_cipher

logger = logging.getLogger(__name__)


def to_jsonable(value):
    """
    Recursively convert a value into something json.dumps() can handle.

    Product dicts carry real datetime objects for posted_at, which requests
    cannot serialise - without this every webhook send fails.
    """
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v) for v in value]
    return value


STORE_URL = "https://www.festoolrecon.com/"


def digest_headline(digest: dict) -> str:
    """One line saying what just happened, used as subject / push title."""
    added = len(digest.get('items') or [])
    watched = len(digest.get('watched') or [])
    title = digest.get('collection_title') or digest.get('collection_handle') or 'sale'

    if watched:
        return f"{watched} watched item(s) in {title} ({added} new listing(s))"
    if added == 1:
        only = digest['items'][0]
        return f"New in {title}: {only.get('name')}"
    return f"{added} new listing(s) in {title}"


def digest_line(item: dict) -> str:
    """`Name - $1,234.00 (25% off)`, the one-line form every channel shares."""
    line = f"{item.get('name')} - ${item.get('sale_price') or 0:,.2f}"
    if item.get('discount_pct'):
        line += f" ({item['discount_pct']:.0f}% off)"
    return line


class NotificationProvider(ABC):
    """
    Base class for notification providers.

    Every provider here talks to somebody else's server, so every one of them
    gets a deadline.  Without one a send does not fail, it simply never
    returns: the sender waits forever on a socket, and whatever was waiting on
    the sender waits with it.  That is not hypothetical - it is what the
    Pushbullet client used to do.
    """

    DEFAULT_TIMEOUT = 30

    def __init__(self, tz_manager=None, timeout: Optional[int] = None):
        """Initialize with optional timezone manager."""
        self.tz_manager = tz_manager
        self.timeout = timeout or self.DEFAULT_TIMEOUT

    @abstractmethod
    def send(self, product: dict, previous_products: List[dict] = None,
             include_photo: bool = True) -> bool:
        """Send notification. Returns True if successful."""
        pass

    def _format_time(self, time_value) -> str:
        """Format a timestamp using the timezone manager when available."""
        if self.tz_manager:
            return self.tz_manager.to_short_display(time_value)
        if not time_value:
            return "N/A"
        return str(time_value)

    def send_digest(self, digest: dict) -> bool:
        """
        Send one message covering a batch of listings that appeared together.

        A multi-item collection can put thirty items up in a single check, and
        sending thirty separate notifications would cost thirty SMS and train
        the reader to ignore all of them.  Providers that can do better than
        this override it; the default folds the batch into the ordinary
        single-product shape so a provider never silently drops a digest.
        """
        items = digest.get('items') or []
        if not items:
            return True

        summary = dict(items[0])
        summary['name'] = digest_headline(digest)
        summary['item_number'] = digest.get('collection_handle') or ''
        summary['description'] = '\n'.join(digest_line(i) for i in items)
        summary['price_check'] = digest.get('price_check') or ''
        return self.send(summary, [], False)


class EmailNotifier(NotificationProvider):
    """Email notification via SMTP."""
    
    def __init__(self, smtp_server: str, port: int, username: str, 
                 password: str, from_email: str, to_email: str, tz_manager=None):
        super().__init__(tz_manager)
        self.smtp_server = smtp_server
        self.port = port
        self.username = username
        self.password = password
        self.from_email = from_email
        self.to_email = to_email
    
    def send(self, product: dict, previous_products: List[dict] = None,
             include_photo: bool = True) -> bool:
        """Send email notification."""
        try:
            msg = MIMEMultipart('alternative')
            msg['Subject'] = f"New Festool Product: {product['name']}"
            msg['From'] = self.from_email
            msg['To'] = self.to_email
            
            # Create email body
            html = self._create_html_body(product, previous_products, include_photo)
            text = self._create_text_body(product, previous_products)
            
            msg.attach(MIMEText(text, 'plain'))
            msg.attach(MIMEText(html, 'html'))
            
            if int(self.port) == 465:
                # Implicit TLS - starttls() would fail on this port
                with smtplib.SMTP_SSL(self.smtp_server, self.port, timeout=self.timeout) as server:
                    server.login(self.username, self.password)
                    server.send_message(msg)
            else:
                with smtplib.SMTP(self.smtp_server, self.port, timeout=self.timeout) as server:
                    server.starttls()
                    server.login(self.username, self.password)
                    server.send_message(msg)

            logger.info(f"Email sent to {self.to_email}")
            return True
            
        except Exception as e:
            logger.error(f"Email send failed: {e}")
            return False
    
    def _create_text_body(self, product: dict, previous_products: List[dict] = None) -> str:
        """Create plain text email body."""
        posted_time = self._format_time(product.get('posted_at'))
        
        body = f"""New Festool Recon Product Available!

Product: {product['name']}
Item Number: {product['item_number']}
Price: ${product['sale_price']:.2f}"""
        
        if product.get('regular_price'):
            body += f" (Regular: ${product['regular_price']:.2f}"
            if product.get('discount_pct'):
                body += f", {product['discount_pct']:.0f}% off"
            body += ")"

        if product.get('price_check'):
            body += f"\n\nPrice check: {product['price_check']}"

        body += f"\n\nPosted: {posted_time}"
        body += f"\n\nView: https://www.festoolrecon.com/"
        
        if previous_products:
            body += "\n\n--- Previous Products ---"
            for prev in previous_products:
                body += f"\n{prev['name']} - ${prev['sale_price']:.2f}"
                if prev.get('minutes_live') is not None:
                    body += f" (Live for {prev['minutes_live']} minutes)"
        
        return body
    
    def _create_html_body(self, product: dict, previous_products: List[dict] = None,
                         include_photo: bool = True) -> str:
        """Create HTML email body."""
        posted_time = self._format_time(product.get('posted_at'))
        
        html = f"""
        <html>
        <body style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;">
            <h2 style="color: #009a44;">New Festool Recon Product!</h2>
            <div style="border: 1px solid #ddd; padding: 20px; border-radius: 5px;">
        """
        
        if include_photo and product.get('image_url'):
            html += (f'<img src="{escape(str(product["image_url"]), quote=True)}" '
                     f'alt="{escape(str(product["name"]), quote=True)}" '
                     f'style="max-width: 100%; height: auto;"><br><br>')

        html += f"""
                <h3>{escape(str(product['name']))}</h3>
                <p><strong>Item Number:</strong> {escape(str(product['item_number']))}</p>
                <p><strong>Price:</strong> ${product['sale_price']:.2f}"""

        if product.get('regular_price'):
            html += f' <span style="text-decoration: line-through; color: #999;">${product["regular_price"]:.2f}</span>'
            if product.get('discount_pct'):
                html += f' <strong style="color: #009a44;">({product["discount_pct"]:.0f}% off)</strong>'

        html += "</p>"

        if product.get('price_check'):
            highlight = ('#e6f5ec' if product.get('is_new_low')
                         else '#f1f7f3' if product.get('is_best_ever')
                         else '#f4f4f4')
            html += (f'<p style="background-color: {highlight}; padding: 10px; '
                     f'border-radius: 4px;"><strong>Price check:</strong> '
                     f'{escape(str(product["price_check"]))}</p>')

        html += f"""
                <p><strong>Posted:</strong> {escape(posted_time)}</p>
                <p><a href="https://www.festoolrecon.com/" style="background-color: #009a44; color: white; padding: 10px 20px; text-decoration: none; border-radius: 3px;">View Product</a></p>
            </div>
        """

        if previous_products:
            html += '<h3 style="margin-top: 30px;">Previous Products</h3><ul>'
            for prev in previous_products:
                html += f'<li>{escape(str(prev["name"]))} - ${prev["sale_price"]:.2f}'
                if prev.get('minutes_live') is not None:
                    html += f' (Live for {prev["minutes_live"]} min)'
                html += '</li>'
            html += '</ul>'
        
        html += '</body></html>'
        return html


    def send_digest(self, digest: dict) -> bool:
        """One email covering everything that appeared in a collection at once."""
        try:
            msg = MIMEMultipart('alternative')
            msg['Subject'] = f"Festool Recon: {digest_headline(digest)}"
            msg['From'] = self.from_email
            msg['To'] = self.to_email

            msg.attach(MIMEText(self._digest_text(digest), 'plain'))
            msg.attach(MIMEText(self._digest_html(digest), 'html'))

            if int(self.port) == 465:
                with smtplib.SMTP_SSL(self.smtp_server, self.port, timeout=self.timeout) as server:
                    server.login(self.username, self.password)
                    server.send_message(msg)
            else:
                with smtplib.SMTP(self.smtp_server, self.port, timeout=self.timeout) as server:
                    server.starttls()
                    server.login(self.username, self.password)
                    server.send_message(msg)

            logger.info(f"Collection digest emailed to {self.to_email}")
            return True

        except Exception as e:
            logger.error(f"Digest email send failed: {e}")
            return False

    def _digest_text(self, digest: dict) -> str:
        url = digest.get('collection_url') or STORE_URL
        title = digest.get('collection_title') or digest.get('collection_handle')

        body = f"{digest_headline(digest)}\n\n"
        body += (f"The store is offering {digest.get('total_live', 0)} item(s) at once "
                 f"in '{title}'.\n")

        watched = digest.get('watched') or []
        if watched:
            body += "\n--- On your watchlist ---\n"
            for item in watched:
                body += f"* {digest_line(item)}  [{item.get('item_number')}]\n"
                if item.get('price_check'):
                    body += f"    {item['price_check']}\n"

        items = digest.get('items') or []
        rest = [i for i in items if i not in watched]
        if rest:
            body += "\n--- Newly listed ---\n"
            for item in rest:
                body += f"* {digest_line(item)}  [{item.get('item_number')}]\n"

        ended = digest.get('ended') or []
        if ended:
            body += "\n--- No longer listed ---\n"
            for item in ended:
                body += f"* {item.get('name')} [{item.get('item_number')}]\n"

        body += f"\nView: {url}\n"
        return body

    def _digest_html(self, digest: dict) -> str:
        url = escape(str(digest.get('collection_url') or STORE_URL), quote=True)
        title = escape(str(digest.get('collection_title')
                           or digest.get('collection_handle') or ''))

        html = f"""
        <html>
        <body style="font-family: Arial, sans-serif; max-width: 700px; margin: 0 auto;">
            <h2 style="color: #009a44;">{escape(digest_headline(digest))}</h2>
            <p style="color: #555;">The store is offering
               <strong>{digest.get('total_live', 0)}</strong> item(s) at once in
               &ldquo;{title}&rdquo;.</p>
        """

        watched = digest.get('watched') or []
        if watched:
            html += '<h3 style="margin-top: 24px;">On your watchlist</h3>'
            html += self._digest_table(watched, highlight=True)

        items = digest.get('items') or []
        rest = [i for i in items if i not in watched]
        if rest:
            html += '<h3 style="margin-top: 24px;">Newly listed</h3>'
            html += self._digest_table(rest)

        ended = digest.get('ended') or []
        if ended:
            html += '<h3 style="margin-top: 24px;">No longer listed</h3><ul>'
            for item in ended:
                html += (f'<li>{escape(str(item.get("name")))} '
                         f'({escape(str(item.get("item_number")))})</li>')
            html += '</ul>'

        html += (f'<p style="margin-top: 24px;"><a href="{url}" '
                 f'style="background-color: #009a44; color: white; padding: 10px 20px; '
                 f'text-decoration: none; border-radius: 3px;">View the collection</a></p>')
        html += '</body></html>'
        return html

    @staticmethod
    def _digest_table(items: List[dict], highlight: bool = False) -> str:
        rows = ('<table style="border-collapse: collapse; width: 100%;">'
                '<tr style="text-align: left; border-bottom: 2px solid #ddd;">'
                '<th style="padding: 6px;">Item</th>'
                '<th style="padding: 6px;">Number</th>'
                '<th style="padding: 6px;">Price</th>'
                '<th style="padding: 6px;">MSRP</th>'
                '<th style="padding: 6px;">Off</th></tr>')

        for item in items:
            shade = ''
            if item.get('is_new_low'):
                shade = ' background-color: #e6f5ec;'
            elif highlight:
                shade = ' background-color: #f7f7f7;'

            regular = (f'${item["regular_price"]:,.2f}'
                       if item.get('regular_price') else '-')
            discount = (f'{item["discount_pct"]:.0f}%'
                        if item.get('discount_pct') else '-')

            rows += (
                f'<tr style="border-bottom: 1px solid #eee;{shade}">'
                f'<td style="padding: 6px;">{escape(str(item.get("name")))}</td>'
                f'<td style="padding: 6px;">{escape(str(item.get("item_number")))}</td>'
                f'<td style="padding: 6px;"><strong>'
                f'${item.get("sale_price") or 0:,.2f}</strong></td>'
                f'<td style="padding: 6px; color: #999; text-decoration: line-through;">'
                f'{regular}</td>'
                f'<td style="padding: 6px; color: #009a44;">{discount}</td>'
                f'</tr>'
            )

        return rows + '</table>'


class SMSNotifier(NotificationProvider):
    """SMS notification (supports Twilio and others)."""
    
    def __init__(self, provider: str, api_key: str, api_secret: str,
                 from_number: str, to_number: str, tz_manager=None,
                 timeout: Optional[int] = None):
        super().__init__(tz_manager, timeout)
        self.provider = provider.lower()
        self.api_key = api_key
        self.api_secret = api_secret
        self.from_number = from_number
        self.to_number = to_number
    
    def send(self, product: dict, previous_products: List[dict] = None,
             include_photo: bool = True) -> bool:
        """Send SMS notification."""
        if self.provider == 'twilio':
            return self._send_twilio(product)
        else:
            logger.error(f"Unsupported SMS provider: {self.provider}")
            return False
    
    def _twilio_client(self, client_class):
        """
        A Twilio client that gives up rather than hanging.

        Twilio's default HTTP client has no timeout.  Wiring one in is done
        defensively: if a future version moves TwilioHttpClient or changes its
        signature, an untimed send still beats no send at all, so we fall back
        rather than raise.
        """
        try:
            from twilio.http.http_client import TwilioHttpClient
            return client_class(self.api_key, self.api_secret,
                                http_client=TwilioHttpClient(timeout=self.timeout))
        except Exception as e:
            logger.warning(
                f"Could not give the Twilio client a {self.timeout}s timeout "
                f"({e}); sending without one"
            )
            return client_class(self.api_key, self.api_secret)

    def _send_twilio(self, product: dict) -> bool:
        """Send SMS via Twilio."""
        try:
            try:
                from twilio.rest import Client
            except ImportError:
                logger.error(
                    "SMS notification requires the 'twilio' package. "
                    "Install it with: pip install twilio"
                )
                return False

            client = self._twilio_client(Client)
            message = f"New Festool: {product['name']}\n"
            message += f"Price: ${product['sale_price']:.2f}"
            if product.get('discount_pct'):
                message += f" ({product['discount_pct']:.0f}% off)"
            message += "\n"
            message += f"Item: {product['item_number']}\n"
            if product.get('times_listed_before') and product.get('is_best_ever'):
                message += ("Lowest price yet for this item\n"
                            if product.get('is_new_low')
                            else "Matches the lowest price seen for this item\n")
            message += "View: https://www.festoolrecon.com/"
            
            client.messages.create(
                body=message,
                from_=self.from_number,
                to=self.to_number
            )
            
            logger.info(f"SMS sent to {self.to_number}")
            return True
            
        except Exception as e:
            logger.error(f"SMS send failed: {e}")
            return False


    # A digest has to fit in a message someone will actually read, and every
    # extra segment is billed - so it names a few items and counts the rest.
    DIGEST_ITEM_LIMIT = 4

    def send_digest(self, digest: dict) -> bool:
        """One SMS for the whole batch, watchlist matches first."""
        if self.provider != 'twilio':
            logger.error(f"Unsupported SMS provider: {self.provider}")
            return False

        try:
            try:
                from twilio.rest import Client
            except ImportError:
                logger.error(
                    "SMS notification requires the 'twilio' package. "
                    "Install it with: pip install twilio"
                )
                return False

            watched = digest.get('watched') or []
            items = digest.get('items') or []
            # Watchlist matches earn their place in the message ahead of the rest
            ordered = watched + [i for i in items if i not in watched]

            message = digest_headline(digest) + "\n"
            for item in ordered[:self.DIGEST_ITEM_LIMIT]:
                message += f"- {digest_line(item)}\n"

            remaining = len(ordered) - self.DIGEST_ITEM_LIMIT
            if remaining > 0:
                message += f"...and {remaining} more\n"

            message += digest.get('collection_url') or STORE_URL

            self._twilio_client(Client).messages.create(
                body=message,
                from_=self.from_number,
                to=self.to_number
            )

            logger.info(f"Collection digest SMS sent to {self.to_number}")
            return True

        except Exception as e:
            logger.error(f"Digest SMS send failed: {e}")
            return False


class PushbulletNotifier(NotificationProvider):
    """
    Pushbullet push notifications.

    Posts to the documented v2 API directly rather than through the
    `pushbullet.py` client.  That client builds a bare requests.Session with no
    timeout and then calls refresh() from its own constructor, so merely
    *constructing* it makes four network calls that can block forever -
    loading devices, chats, channels and user info we never use, because a
    push with no named recipient goes to all of them anyway.  One timed POST
    does the same job and can be given a deadline.
    """

    PUSH_URL = "https://api.pushbullet.com/v2/pushes"

    def __init__(self, api_key: str, tz_manager=None, timeout: Optional[int] = None):
        super().__init__(tz_manager, timeout)
        self.api_key = api_key

    def _push_link(self, title: str, url: str, body: str) -> bool:
        """One push. Raises on transport failure; callers log and return False."""
        response = requests.post(
            self.PUSH_URL,
            json={'type': 'link', 'title': title, 'url': url, 'body': body},
            headers={
                'Access-Token': self.api_key,
                'Content-Type': 'application/json',
            },
            timeout=self.timeout,
        )
        response.raise_for_status()
        return True
    
    def send(self, product: dict, previous_products: List[dict] = None,
             include_photo: bool = True) -> bool:
        """Send Pushbullet notification."""
        try:
            # Lead with the strongest signalso it lands in the preview - but
            # only claim a best price when the item is genuinely cheaper than
            # it has ever been, not when it has merely returned at the same
            # price as last time.
            if product.get('times_listed_before') and product.get('is_best_ever'):
                prefix = "BEST PRICE" if product.get('is_new_low') else "TIES BEST PRICE"
                title = f"{prefix} - {product['name']}"
            else:
                title = f"New Festool: {product['name']}"
            
            # Format the posted time using timezone manager
            posted_time = self._format_time(product.get('posted_at'))
            
            # Build body without duplicate URL
            body = f"💰 Price: ${product['sale_price']:.2f}"
            if product.get('discount_pct'):
                body += f" ({product['discount_pct']:.0f}% off)"
            body += "\n"
            body += f"🔢 Item: {product['item_number']}\n"
            body += f"📅 Posted: {posted_time}\n"

            if product.get('price_check'):
                body += f"📊 {product['price_check']}\n"
            
            if previous_products:
                body += "\n🕐 Previous:\n"
                for prev in previous_products[:3]:
                    body += f"• {prev['name']} (${prev.get('sale_price') or 0:.2f})\n"
            
            # A link push - the URL appears on its own, no need to repeat it
            self._push_link(title, STORE_URL, body)

            logger.info("Pushbullet notification sent")
            return True

        except Exception as e:
            logger.error(f"Pushbullet send failed: {e}")
            return False

    def _format_time(self, time_value) -> str:
        """Format timestamp using timezone manager if available."""
        if self.tz_manager:
            # Use timezone manager's formatting which respects user settings
            return self.tz_manager.to_short_display(time_value)

        # Fallback if no timezone manager
        if not time_value:
            return "Just now"

        try:
            if isinstance(time_value, str):
                dt = datetime.fromisoformat(time_value.replace('Z', '+00:00'))
            else:
                dt = time_value

            if dt.tzinfo is not None:
                dt = dt.astimezone()

            return dt.strftime('%b %d %Y, %I:%M %p')

        except Exception as e:
            logger.debug(f"Error formatting time: {e}")
            return str(time_value)


    DIGEST_ITEM_LIMIT = 10

    def send_digest(self, digest: dict) -> bool:
        """One push for the whole batch."""
        try:
            watched = digest.get('watched') or []
            items = digest.get('items') or []
            ordered = watched + [i for i in items if i not in watched]

            body = (f"{digest.get('total_live', 0)} item(s) on offer at once\n\n")

            if watched:
                body += "On your watchlist:\n"
                for item in watched[:self.DIGEST_ITEM_LIMIT]:
                    body += f"* {digest_line(item)}\n"
                body += "\n"

            rest = [i for i in ordered if i not in watched]
            if rest:
                body += "Newly listed:\n"
                for item in rest[:self.DIGEST_ITEM_LIMIT]:
                    body += f"* {digest_line(item)}\n"
                remaining = len(rest) - self.DIGEST_ITEM_LIMIT
                if remaining > 0:
                    body += f"...and {remaining} more\n"

            ended = digest.get('ended') or []
            if ended:
                body += f"\n{len(ended)} listing(s) ended\n"

            self._push_link(
                digest_headline(digest),
                digest.get('collection_url') or STORE_URL,
                body
            )

            logger.info("Collection digest pushed")
            return True

        except Exception as e:
            logger.error(f"Digest push failed: {e}")
            return False


class WebhookNotifier(NotificationProvider):
    """Generic webhook notifications."""

    # Shorter than the shared default, which is what this has always waited.
    # A webhook is usually a chat service that answers at once or not at all.
    DEFAULT_TIMEOUT = 10
    
    def __init__(self, url: str, method: str = 'POST', headers: Dict = None,
                 tz_manager=None, timeout: Optional[int] = None):
        super().__init__(tz_manager, timeout)
        self.url = url
        self.method = method.upper()
        self.headers = headers or {'Content-Type': 'application/json'}
    
    def send(self, product: dict, previous_products: List[dict] = None,
             include_photo: bool = True) -> bool:
        """Send webhook notification."""
        try:
            # Product dicts contain datetime objects; convert the whole payload
            # to JSON-safe primitives before handing it to requests.
            payload = to_jsonable({
                'product': product,
                'previous_products': previous_products or [],
                'timestamp': product.get('posted_at') or datetime.now()
            })

            if self.method == 'POST':
                response = requests.post(self.url, json=payload, headers=self.headers, timeout=self.timeout)
            elif self.method == 'PUT':
                response = requests.put(self.url, json=payload, headers=self.headers, timeout=self.timeout)
            else:
                # Query strings cannot carry nested structures - send a flat summary
                params = {
                    'name': payload['product'].get('name'),
                    'item_number': payload['product'].get('item_number'),
                    'sale_price': payload['product'].get('sale_price'),
                    'timestamp': payload['timestamp'],
                }
                response = requests.get(self.url, params=params, headers=self.headers, timeout=self.timeout)

            response.raise_for_status()
            logger.info(f"Webhook sent to {self.url}")
            return True

        except Exception as e:
            logger.error(f"Webhook send failed: {e}")
            return False


    def send_digest(self, digest: dict) -> bool:
        """POST the whole batch as one structured payload."""
        try:
            payload = to_jsonable({
                'event': 'collection_digest',
                'collection': {
                    'handle': digest.get('collection_handle'),
                    'title': digest.get('collection_title'),
                    'url': digest.get('collection_url'),
                    'live_count': digest.get('total_live'),
                },
                'headline': digest_headline(digest),
                'added': digest.get('items') or [],
                'watched': digest.get('watched') or [],
                'ended': digest.get('ended') or [],
                'timestamp': digest.get('posted_at') or datetime.now(),
            })

            if self.method == 'PUT':
                response = requests.put(self.url, json=payload,
                                        headers=self.headers, timeout=self.timeout)
            elif self.method == 'GET':
                # Query strings cannot carry nested structures - send a summary
                response = requests.get(self.url, headers=self.headers, timeout=self.timeout,
                                        params={
                                            'event': 'collection_digest',
                                            'collection': digest.get('collection_handle'),
                                            'added': len(digest.get('items') or []),
                                            'watched': len(digest.get('watched') or []),
                                            'timestamp': payload['timestamp'],
                                        })
            else:
                response = requests.post(self.url, json=payload,
                                         headers=self.headers, timeout=self.timeout)

            response.raise_for_status()
            logger.info(f"Collection digest webhook sent to {self.url}")
            return True

        except Exception as e:
            logger.error(f"Digest webhook send failed: {e}")
            return False


class NotificationManager:
    """Manages multiple notification providers."""
    
    def __init__(self, tz_manager=None):
        self.providers = {}
        self.tz_manager = tz_manager
        # Persistent key - a key generated per process could never decrypt
        # anything written by a previous run.
        self.cipher = get_cipher()
    
    def add_provider(self, name: str, provider: NotificationProvider):
        """Add a notification provider."""
        self.providers[name] = provider
    
    def remove_provider(self, name: str):
        """Remove a notification provider."""
        self.providers.pop(name, None)
    
    def send_notification(self, product: dict, previous_products: List[dict] = None,
                         include_photo: bool = True) -> Dict[str, bool]:
        """Send notifications via all providers. Returns dict of results."""
        results = {}
        for name, provider in self.providers.items():
            try:
                success = provider.send(product, previous_products, include_photo)
                results[name] = success
            except Exception as e:
                logger.error(f"Provider {name} failed: {e}")
                results[name] = False
        
        return results
    
    def send_digest(self, digest: dict) -> Dict[str, bool]:
        """Send one digest through every configured provider."""
        results = {}
        for name, provider in self.providers.items():
            try:
                results[name] = provider.send_digest(digest)
            except Exception as e:
                logger.error(f"Provider {name} failed on digest: {e}")
                results[name] = False

        return results

    def encrypt_config(self, config: Dict) -> str:
        """Encrypt configuration data."""
        json_data = json.dumps(config)
        encrypted = self.cipher.encrypt(json_data.encode())
        return encrypted.decode()
    
    def decrypt_config(self, encrypted_config: str) -> Dict:
        """Decrypt configuration data."""
        decrypted = self.cipher.decrypt(encrypted_config.encode())
        return json.loads(decrypted.decode())