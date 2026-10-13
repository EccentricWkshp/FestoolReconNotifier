"""Festool Recon website scraper."""
import re
import logging
from dataclasses import dataclass
from typing import Dict, List, Optional
import requests
from bs4 import BeautifulSoup

from .collection_scraper import (
    DEFAULT_COLLECTION, PLACEHOLDER_SKUS, PLACEHOLDER_TYPE, Storefront,
)

logger = logging.getLogger(__name__)


@dataclass
class FeedEntry:
    """One product in the collection feed, after the fillers are dropped."""
    position: int
    handle: str
    item_number: Optional[str]
    name: str
    sale_price: Optional[float]
    regular_price: Optional[float]
    product_type: Optional[str]
    image_url: Optional[str]
    body_html: Optional[str]
    available: bool


class FestoolScraper:
    """Scrapes festoolrecon.com for product information."""

    BASE_URL = "https://www.festoolrecon.com/"

    # The collection the front page rotates through
    COLLECTION = DEFAULT_COLLECTION

    # How many per-product stock checks to spend walking past sold-out
    # candidates before giving up on the feed
    MAX_STOCK_CHECKS = 3

    def __init__(self, timeout: int = 30, collection: Optional[str] = None):
        self.timeout = timeout
        # Which collection the front page is rotating through.  Overridable
        # because the store has renamed and replaced it before, and the name
        # must never be what the app depends on.
        if collection:
            self.COLLECTION = collection
        # The front page, read at most once per instance (see _fetch_soup)
        self._front_soup = None
        self._front_soup_read = False

    def fetch_current_product(self) -> Optional[Dict]:
        """
        Fetch the product that is actually live on the store right now.

        **The front page cannot be trusted to say which product is live.**
        Shopify serves it from a server-side full-page cache - the response
        carries an ``ETag: W/"page_cache:<shop>:IndexController:<hash>"`` - and
        that entry can lag the real store by hours, long enough for several
        products to come and go unnoticed.  Nothing sent from this end defeats
        it: ``Cache-Control: no-cache``, a cache-busting query string, a
        cookie-bearing session and browser-identical headers all come back
        byte-identical, same ETag.  The embedded ``ProductJson`` is part of
        that same cached page, so its ``available`` flag is stale too.

        The store's own collection feed is the authority on *which* product is
        live: the store sells one item at a time from ``oneanddone``, so the
        first entry of that collection is what the front page is supposed to
        be showing.  The front page is used only for the rich detail it
        carries - description, image - and only when it names that same item.

        An earlier version asked instead whether the front page's product was
        sold out, and trusted the page whenever it was not.  That works only
        while the store retires stock as it rotates; the moment a still-in-
        stock item is rotated *back into the queue*, a stale front page passes
        the stock check and the app sits on a product that has not been live
        for hours.  That is exactly what happened, so identity now comes from
        the feed and stock is only a sanity check on the feed's answer.

        Returns:
            Dict with product data or None if the live product could not be
            established.  None is deliberate: recording the wrong product is
            worse than recording nothing.
        """
        candidates = self._fetch_feed()
        front = self._scrape_product_page(self.BASE_URL)
        front_handle = (front or {}).get('handle')

        live = self._resolve_live(candidates)

        if live is None:
            # No usable feed: fall back to the old, weaker test rather than
            # record nothing at all.
            return self._front_page_only(front, front_handle)

        if front_handle and front_handle == live.handle:
            return self._merge(front, live)

        if front is None:
            logger.warning(
                f"Front page could not be read - taking the live product from "
                f"the collection feed: {live.name} ({live.item_number})"
            )
        else:
            seen_at = next((c.position for c in candidates
                            if c.handle == front_handle), None)
            logger.warning(
                f"Front page is stale: it is showing "
                f"{front.get('name')} ({front.get('item_number')}), "
                + (f"which the collection has at position {seen_at}. "
                   if seen_at is not None else
                   "which is not in the collection at all. ")
                + f"Live product is {live.name} ({live.item_number})."
            )

        product = self._scrape_product_page(f"{self.BASE_URL}products/{live.handle}")

        if product:
            logger.info(
                f"Live product resolved from the collection feed: "
                f"{live.name} ({live.item_number})"
            )
        else:
            # The product page failed, but the feed already carries everything
            # required to record a listing - losing the rotation entirely
            # would be the worse outcome.
            logger.warning(
                f"Could not scrape '{live.handle}' - recording it from the "
                f"collection feed instead"
            )

        resolved = self._merge(product, live)

        if not resolved.get('item_number') or not resolved.get('sale_price'):
            logger.error(
                f"Live product '{live.handle}' has no usable item number or "
                f"price - skipping this check"
            )
            return None

        return resolved

    def _front_page_only(self, front: Optional[Dict],
                         front_handle: Optional[str]) -> Optional[Dict]:
        """
        Last resort when the collection feed cannot be read.

        Without the feed there is nothing to establish which product is live,
        so the front page is accepted unless its own stock says otherwise.
        This is weaker than the normal path and says so in the log.
        """
        if not front:
            return None

        logger.warning(
            "No live product from the collection feed - falling back to the "
            "front page, which may be served from a stale cache"
        )

        if front_handle and self._is_available(front_handle) is False:
            logger.error(
                f"Front page is showing {front.get('name')} "
                f"({front.get('item_number')}), which is sold out, and the feed "
                f"offered nothing live - skipping this check"
            )
            return None

        return front

    def _merge(self, scraped: Optional[Dict], live: FeedEntry) -> Dict:
        """
        Combine a scraped product page with the feed entry for the same item.

        The page carries the description and image; the feed carries the SKU,
        product type and the more current price.  Where both have an answer
        the feed wins on the fields it owns - it is regenerated when the store
        changes something, while the page may be a cached copy of itself.
        """
        product = dict(scraped or {})

        product['handle'] = live.handle
        product.setdefault('raw_html', None)

        if not product.get('name'):
            product['name'] = live.name

        if live.item_number:
            scraped_number = product.get('item_number')
            if scraped_number and scraped_number != live.item_number:
                logger.warning(
                    f"Item number mismatch for '{live.handle}': page says "
                    f"{scraped_number}, collection feed says {live.item_number} "
                    f"- trusting the feed"
                )
            product['item_number'] = live.item_number

        for field, value in (('sale_price', live.sale_price),
                             ('regular_price', live.regular_price)):
            if value is None:
                continue
            scraped_value = product.get(field)
            if scraped_value is not None and abs(scraped_value - value) >= 0.01:
                logger.info(
                    f"{field} for {live.item_number}: page says ${scraped_value:,.2f}, "
                    f"feed says ${value:,.2f} - trusting the feed"
                )
            product[field] = value

        # The front page never exposes the store's own category; the feed does.
        if live.product_type:
            product['product_type'] = live.product_type

        if not product.get('image_url') and live.image_url:
            product['image_url'] = live.image_url

        if not product.get('description') and live.body_html:
            product['description'] = self._html_to_text(live.body_html)

        return product

    @staticmethod
    def _html_to_text(html: str) -> Optional[str]:
        """Flatten the feed's body_html the same way a page scrape reads it."""
        try:
            text = BeautifulSoup(html, 'lxml').get_text(strip=True)
        except Exception:
            return None
        return ' '.join(text.split()) or None

    def _is_available(self, handle: str) -> Optional[bool]:
        """
        Real stock for one product, straight from Shopify.

        ``/products/<handle>.js`` is keyed per product rather than per page, so
        it reflects reality even while the front page is being served stale.

        Returns True/False, or None when the check could not be made - callers
        must treat None as "unknown", never as "sold out".
        """
        url = f"{self.BASE_URL}products/{handle}.js"
        try:
            response = requests.get(url, headers=self._json_headers(), timeout=self.timeout)
            response.raise_for_status()
            return bool(response.json().get('available'))
        except (requests.RequestException, ValueError) as e:
            logger.warning(f"Could not confirm stock for '{handle}': {e}")
            return None

    def _fetch_feed(self) -> List[FeedEntry]:
        """
        The collection's ordered products, fillers removed and positions
        assigned afterwards, so position 0 is the item that should be live.

        Returns an empty list on failure - callers must not read that as "the
        collection is empty".
        """
        url = (f"{self.BASE_URL}collections/{self.COLLECTION}/products.json"
               f"?limit=250")
        try:
            response = requests.get(url, headers=self._json_headers(), timeout=self.timeout)
            response.raise_for_status()
            products = response.json().get('products', [])
        except (requests.RequestException, ValueError) as e:
            logger.error(f"Could not read the collection feed: {e}")
            return []

        entries: List[FeedEntry] = []
        for entry in products:
            variant = (entry.get('variants') or [{}])[0]
            sku = str(variant.get('sku') or '').strip() or None
            price = _to_float(variant.get('price'))

            if self._is_filler(entry, sku, price):
                continue

            images = entry.get('images') or []
            entries.append(FeedEntry(
                position=len(entries),
                handle=entry.get('handle') or '',
                item_number=sku,
                name=(entry.get('title') or '').strip(),
                sale_price=price,
                regular_price=_to_float(variant.get('compare_at_price')),
                product_type=(entry.get('product_type') or '').strip() or None,
                image_url=images[0].get('src') if images else None,
                body_html=entry.get('body_html'),
                available=bool(variant.get('available', True)),
            ))

        return entries

    @staticmethod
    def _is_filler(entry: dict, sku: Optional[str], price: Optional[float]) -> bool:
        """The store's "Wow, that went fast!!!" stand-in for sold-out stock."""
        if sku and sku in PLACEHOLDER_SKUS:
            return True
        if (entry.get('product_type') or '').strip().lower() == PLACEHOLDER_TYPE:
            return True
        return price is not None and price < 1.0

    def _resolve_live(self, candidates: List[FeedEntry]) -> Optional[FeedEntry]:
        """
        The first collection entry that is genuinely in stock.

        The feed's ``available`` flags survive its own caching, but a flag is
        cheap to be wrong about, so the leading candidate is confirmed against
        per-product stock before it is accepted.  A stock check that *fails*
        (rather than answering "sold out") is not evidence against the item -
        the feed order stands.
        """
        checks = 0
        for entry in candidates:
            if not entry.available:
                logger.debug(f"Feed says {entry.item_number} is sold out - skipping")
                continue

            if checks < self.MAX_STOCK_CHECKS:
                checks += 1
                if self._is_available(entry.handle) is False:
                    logger.info(
                        f"Collection lists {entry.name} ({entry.item_number}) as "
                        f"available but it is sold out - looking further down"
                    )
                    continue

            return entry

        if candidates:
            logger.error("No available product in the collection feed")
        return None

    def _json_headers(self) -> Dict[str, str]:
        return {
            'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                           'AppleWebKit/537.36 (KHTML, like Gecko) '
                           'Chrome/141.0.0.0 Safari/537.36'),
            'Accept': 'application/json, text/plain, */*',
            'Cache-Control': 'no-cache',
            'Pragma': 'no-cache',
            'Referer': self.BASE_URL,
        }

    def read_storefront(self) -> Storefront:
        """
        Work out what the store's front page is currently doing.

        Two shapes have been observed, and they are cleanly distinguishable in
        the markup rather than by guesswork:

        * one product at a time - the page renders
          ``shopify-section-product-template`` and links to no collection at
          all; and
        * a whole collection at once - no product template, just cards linking
          to the collections being promoted.

        Header and footer are excluded when looking for those links so that a
        navigation menu could never be mistaken for the store promoting a
        sale.  (This store's nav carries no collection links at all today, but
        that is not something to depend on.)

        A front page that cannot be read returns ``readable=False`` rather
        than an empty answer - see Storefront for why that distinction has to
        survive all the way to the caller.
        """
        soup = self._fetch_soup(self.BASE_URL)
        if soup is None:
            logger.warning("Could not read the front page - storefront mode unknown")
            return Storefront(readable=False)

        section = soup.find('div', id='shopify-section-product-template')
        if section is not None:
            fronted = Storefront(
                readable=True,
                fronted_handle=self._extract_handle(section),
                fronted_item_number=self._extract_item_number(section),
            )
            logger.debug(
                f"Front page is fronting a single product: {fronted.fronted_handle}"
            )
            return fronted

        featured = self._featured_collections(soup)
        logger.info(
            "Front page is not showing a single product; it is promoting "
            f"collection(s): {featured or 'none found'}"
        )
        return Storefront(readable=True, featured_collections=featured)

    # A link inside one of these is the shop's furniture, not a promotion
    _NAV_CLASS = re.compile(r'site-nav|mobile-nav|breadcrumb|site-footer')

    @classmethod
    def _featured_collections(cls, soup: BeautifulSoup) -> List[str]:
        """
        Collection handles the front page is actively pointing customers at.

        Navigation menus, breadcrumbs and the footer are skipped: those link
        to collections whether or not anything is on sale, so counting them
        would make every collection look promoted.  The announcement bar at
        the top of the page is deliberately *not* skipped even though it sits
        inside the header - that is where this store put the link to its sale.

        Order is the page's own, first occurrence wins, so the most prominent
        promotion comes first.
        """
        handles: List[str] = []
        for anchor in soup.find_all('a', href=True):
            if anchor.find_parent(['nav', 'footer']):
                continue
            if anchor.find_parent(class_=cls._NAV_CLASS):
                continue
            if anchor.find_parent(attrs={'role': 'navigation'}):
                continue
            if anchor.find_parent(id=re.compile(r'shopify-section-footer')):
                continue
            match = re.search(r'/collections/([A-Za-z0-9_-]+)', anchor['href'])
            if not match:
                continue
            handle = match.group(1)
            if handle not in handles:
                handles.append(handle)
        return handles

    def _fetch_soup(self, url: str) -> Optional[BeautifulSoup]:
        """
        Fetch and parse one page, browser-identical headers and all.

        The front page is cached for the life of the scraper: a check reads it
        once to see what the store is doing and possibly again for the fronted
        product's detail, and a second request would only risk being handed a
        different copy out of Shopify's page cache mid-check.  Callers make a
        fresh scraper per check, so nothing is cached across checks.
        """
        if url == self.BASE_URL and self._front_soup_read:
            return self._front_soup

        session = requests.Session()
        try:
            # Match EXACT headers from Chrome browser
            session.headers.update({
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
                'Accept-Language': 'en-US,en;q=0.9',
                # 'Accept-Encoding': 'gzip, deflate, br',
                'Cache-Control': 'no-cache',
                'Pragma': 'no-cache',
                'Priority': 'u=0, i',

                # Chrome-specific headers (critical for CDN)
                'sec-ch-ua': '"Google Chrome";v="141", "Not?A_Brand";v="8", "Chromium";v="141"',
                'sec-ch-ua-mobile': '?0',
                'sec-ch-ua-platform': '"Windows"',
                'sec-fetch-dest': 'document',
                'sec-fetch-mode': 'navigate',
                'sec-fetch-site': 'same-origin',  # Act like returning visitor
                'sec-fetch-user': '?1',
                'Upgrade-Insecure-Requests': '1',

                # CRITICAL: Add referrer to appear as returning visitor
                'Referer': 'https://www.festoolrecon.com/',
            })

            logger.debug(f"Fetching: {url}")

            response = session.get(url, timeout=self.timeout)
            response.raise_for_status()

            logger.debug(f"Response: {response.status_code}, {len(response.content)} bytes")

            soup = BeautifulSoup(response.text, 'lxml')

        except requests.RequestException as e:
            logger.error(f"Error fetching page: {e}")
            soup = None
        except Exception as e:
            logger.error(f"Error parsing page: {e}", exc_info=True)
            soup = None
        finally:
            try:
                session.close()
            except Exception:
                pass

        if url == self.BASE_URL:
            self._front_soup = soup
            self._front_soup_read = True

        return soup

    def _scrape_product_page(self, url: str) -> Optional[Dict]:
        """
        Scrape one page carrying the product template.

        The front page and ``/products/<handle>`` render the same
        ``shopify-section-product-template`` markup, so both are read here.
        Returns None when the page carries no product template at all, which
        is now a routine answer rather than an error: it is exactly what the
        front page looks like while the store is selling a whole collection at
        once.
        """
        soup = self._fetch_soup(url)
        if soup is None:
            return None

        try:
            # Target the main product section
            product_section = soup.find('div', id='shopify-section-product-template')

            if not product_section:
                logger.debug(f"No product template on {url} - not a single-product page")
                title = soup.find('title')
                if title:
                    logger.debug(f"Page title: {title.get_text(strip=True)}")
                return None

            # Extract product information
            product_data = {
                'name': self._extract_name(product_section),
                'regular_price': self._extract_regular_price(product_section),
                'sale_price': self._extract_sale_price(product_section),
                'description': self._extract_description(product_section),
                'item_number': self._extract_item_number(product_section),
                'image_url': self._extract_image_url(product_section),
                # Needed to confirm the listing against real stock
                'handle': self._extract_handle(product_section),
                'raw_html': str(soup)
            }

            # Log what was extracted
            logger.debug(f"Extracted - name: {product_data['name']}, "
                        f"price: ${product_data['sale_price']}, "
                        f"item: {product_data['item_number']}")

            # Validate we got critical fields
            if product_data['name'] and product_data['sale_price']:
                logger.info(f"Scraped product: {product_data['name']} - ${product_data['sale_price']}")
                return product_data
            else:
                logger.warning(f"Incomplete product data")
                return None

        except Exception as e:
            logger.error(f"Error parsing page: {e}", exc_info=True)
            return None
    
    def _extract_name(self, soup: BeautifulSoup) -> Optional[str]:
        """Extract product name from main product section."""
        element = soup.find('h1', itemprop='name', class_='product-single__title')
        
        if element and element.get_text(strip=True):
            return element.get_text(strip=True)
        
        element = soup.find('h1', class_='product-single__title')
        if element:
            return element.get_text(strip=True)
        
        logger.debug("Could not find product name")
        return None
    
    def _extract_regular_price(self, soup: BeautifulSoup) -> Optional[float]:
        """Extract regular price from main product section."""
        selectors = [
            's#ComparePrice-product-template',
            's[id*="ComparePrice"]',
            '.product-single__price s',
        ]
        
        for selector in selectors:
            element = soup.select_one(selector)
            if element:
                price_text = element.get_text(strip=True)
                price = self._parse_price(price_text)
                if price:
                    logger.debug(f"Found regular price: ${price}")
                    return price
        
        logger.debug("No regular price found (product may not be on sale)")
        return None
    
    def _extract_sale_price(self, soup: BeautifulSoup) -> Optional[float]:
        """Extract sale/current price from main product section."""
        element = soup.find('span', id=re.compile(r'ProductPrice-product-template'))
        
        if element:
            content = element.get('content')
            if content:
                try:
                    price = float(content)
                    logger.debug(f"Found sale price from content attribute: ${price}")
                    return price
                except ValueError:
                    pass
            
            price_elem = element.find(attrs={'itemprop': 'price', 'content': True})
            if price_elem:
                try:
                    price = float(price_elem['content'])
                    logger.debug(f"Found sale price from itemprop: ${price}")
                    return price
                except (ValueError, KeyError):
                    pass
            
            price_text = element.get_text(strip=True)
            price = self._parse_price(price_text)
            if price:
                logger.debug(f"Found sale price from text: ${price}")
                return price
        
        selectors = [
            'span[itemprop="price"]',
            '.product-single__price span:not(s)',
            '.product-price__price',
        ]
        
        for selector in selectors:
            element = soup.select_one(selector)
            if element:
                if element.parent and element.parent.name == 's':
                    continue
                
                price_text = element.get_text(strip=True)
                price = self._parse_price(price_text)
                if price:
                    logger.debug(f"Found sale price from fallback selector: ${price}")
                    return price
        
        logger.warning("Could not find sale price")
        return None
    
    def _extract_description(self, soup: BeautifulSoup) -> Optional[str]:
        """Extract product description from main product section."""
        element = soup.find('div', class_='product-single__description', itemprop='description')
        
        if not element:
            element = soup.find('div', class_='product-single__description')
        
        if element:
            desc = element.get_text(strip=True)
            desc = ' '.join(desc.split())
            logger.debug(f"Found description ({len(desc)} chars)")
            return desc
        
        logger.debug("No description found")
        return None
    
    def _extract_item_number(self, soup: BeautifulSoup) -> Optional[str]:
        """Extract item number from product description."""
        text = soup.get_text()
        
        patterns = [
            r'Product\s*number\s*[:\.]?\s*(\d+)',
            r'Product\s*No\s*[:\.]?\s*(\d+)',
            r'Item\s*No\s*[:\.]?\s*(\d+)',
            r'Item\s*Number\s*[:\.]?\s*(\d+)',
            r'SKU\s*[:\.]?\s*(\d+)',
            r'Model\s*[:\.]?\s*(\d+)',
            r'Item\s*#\s*[:\.]?\s*(\d+)',
            r'Product\s*#\s*[:\.]?\s*(\d+)',
        ]
        
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                item_no = match.group(1)
                logger.debug(f"Found item number: {item_no}")
                return item_no
        
        scripts = soup.find_all('script', type='application/json')
        for script in scripts:
            if not script.string:
                continue
            try:
                import json
                data = json.loads(script.string)
                
                if isinstance(data, dict):
                    if 'sku' in data and data['sku']:
                        logger.debug(f"Found item number in JSON sku: {data['sku']}")
                        return str(data['sku'])
                    
                    if 'variants' in data and isinstance(data['variants'], list):
                        if len(data['variants']) > 0 and 'sku' in data['variants'][0]:
                            sku = data['variants'][0]['sku']
                            if sku:
                                logger.debug(f"Found item number in JSON variant: {sku}")
                                return str(sku)
            except:
                pass
        
        logger.warning("Could not find item number")
        return None
    
    def _extract_handle(self, soup: BeautifulSoup) -> Optional[str]:
        """
        The product's URL handle, used to confirm it against real stock.

        Comes from the embedded ProductJson blob.  That blob is part of the
        cached page, so its prices and stock flag may be stale - but the handle
        identifies which product the page is talking about, which is exactly
        what needs checking.
        """
        import json

        for script in soup.find_all('script', type='application/json'):
            if not script.string:
                continue
            try:
                data = json.loads(script.string)
            except (ValueError, TypeError):
                continue

            if isinstance(data, dict) and data.get('handle'):
                return str(data['handle'])

        logger.debug("Could not find product handle")
        return None

    def _extract_image_url(self, soup: BeautifulSoup) -> Optional[str]:
        """Extract product image URL from main product section."""
        selectors = [
            'img[id*="FeaturedImage-product-template"]',
            'img.product-featured-img',
            'img.feature-row__image',
            '.product-single__photos img[itemprop="image"]',
        ]
        
        for selector in selectors:
            element = soup.select_one(selector)
            if element:
                # Try to get the original/full-size image first
                src = None
                
                # Check for srcset which often has larger sizes
                srcset = element.get('srcset')
                if srcset:
                    # Parse srcset - format is "url width, url width, ..."
                    # Get the largest one
                    entries = [s.strip() for s in srcset.split(',')]
                    if entries:
                        # Last entry is usually the largest
                        src = entries[-1].split()[0]
                        logger.debug(f"Using srcset image: {src[:60]}...")
                
                # Fallback to data-src or src
                if not src:
                    src = element.get('data-src') or element.get('src')
                
                if src:
                    # Skip placeholder/icon images
                    if any(x in src.lower() for x in ['icon', 'logo', 'no-image', 'placeholder']):
                        continue
                    
                    # Make absolute URL
                    if src.startswith('//'):
                        src = 'https:' + src
                    elif src.startswith('/'):
                        src = self.BASE_URL.rstrip('/') + src
                    
                    logger.debug(f"Found image URL: {src[:80]}...")
                    return src
        
        logger.debug("No product image found")
        return None
    
    def _parse_price(self, price_text: str) -> Optional[float]:
        """Parse price string to float."""
        if not price_text:
            return None
        
        price_text = re.sub(r'[^\d.,]', '', price_text)
        price_text = price_text.replace(',', '')
        
        try:
            return float(price_text)
        except ValueError:
            return None
    
    def test_connection(self) -> bool:
        """Test if we can connect to the website."""
        try:
            session = requests.Session()
            response = session.get(self.BASE_URL, timeout=self.timeout)
            session.close()
            return response.status_code == 200
        except Exception as e:
            logger.error(f"Connection test failed: {e}")
            return False


def _to_float(value) -> Optional[float]:
    """Shopify returns prices as strings; missing values as None."""
    if value in (None, ''):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None