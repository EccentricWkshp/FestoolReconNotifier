"""Festool Recon website scraper - fixed version."""
import re
import logging
import random
import time
from typing import Optional, Dict
import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


class FestoolScraper:
    """Scrapes festoolrecon.com for product information."""
    
    BASE_URL = "https://www.festoolrecon.com/"
    
    def __init__(self, timeout: int = 30):
        self.timeout = timeout
    
    def fetch_current_product(self) -> Optional[Dict]:
        """
        Fetch the current product from festoolrecon.com.
        
        Returns:
            Dict with product data or None if no product found
        """
        try:
            # Create fresh session each time
            session = requests.Session()
            
            # Disable connection pooling completely to prevent caching
            adapter = requests.adapters.HTTPAdapter(
                pool_connections=1,
                pool_maxsize=1,
                max_retries=0,
                pool_block=False
            )
            session.mount('https://', adapter)
            session.mount('http://', adapter)
            
            # Simple, realistic headers
            session.headers.update({
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.9',
                'Cache-Control': 'no-cache',
                'Pragma': 'no-cache',
            })
            
            # Add timestamp to URL to prevent caching
            timestamp = int(time.time() * 1000)
            url = f"{self.BASE_URL}?_={timestamp}"
            
            logger.debug(f"Fetching: {url}")
            
            response = session.get(url, timeout=self.timeout)
            response.raise_for_status()
            
            logger.debug(f"Response status: {response.status_code}, Length: {len(response.content)} bytes")
            
            # Parse HTML
            soup = BeautifulSoup(response.text, 'lxml')
            
            # Close session
            session.close()
            
            # Target the main product section
            product_section = soup.find('div', id='shopify-section-product-template')
            
            if not product_section:
                logger.warning("Could not find main product section")
                # Debug: log what we got instead
                title = soup.find('title')
                if title:
                    logger.debug(f"Page title: {title.get_text(strip=True)}")
                logger.debug(f"First 500 chars of page: {response.text[:500]}")
                return None
            
            # Extract product information
            product_data = {
                'name': self._extract_name(product_section),
                'regular_price': self._extract_regular_price(product_section),
                'sale_price': self._extract_sale_price(product_section),
                'description': self._extract_description(product_section),
                'item_number': self._extract_item_number(product_section),
                'image_url': self._extract_image_url(product_section),
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
                
        except requests.RequestException as e:
            logger.error(f"Error fetching page: {e}")
            return None
        except Exception as e:
            logger.error(f"Error parsing page: {e}", exc_info=True)
            return None
        finally:
            try:
                session.close()
            except:
                pass
    
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
                    
                    # For Shopify CDN URLs with {width} template, request original size
                    # Use a large width (2048px) to get near-original quality
                    '''if '{width}' in src:
                        src = src.replace('{width}', '2048')
                        logger.debug("Requesting high-res version for Shopify CDN")'''
                    
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