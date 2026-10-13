"""Quick test of the Festool scraper."""
import sys
import logging
from scraper.festool_scraper import FestoolScraper

# Setup logging to see debug output
logging.basicConfig(
    level=logging.DEBUG,
    format='%(levelname)s - %(message)s'
)

def test_scraper():
    """Test the scraper and print results."""
    print("=" * 60)
    print("Testing Festool Recon Scraper")
    print("=" * 60)
    
    scraper = FestoolScraper()
    
    print("\n1. Testing connection...")
    if scraper.test_connection():
        print("✓ Connected to festoolrecon.com")
    else:
        print("✗ Connection failed")
        return False
    
    print("\n2. Fetching current product...")
    product = scraper.fetch_current_product()
    
    if product:
        print("\n✓ Product found!")
        print("=" * 60)
        print(f"Name: {product['name']}")
        print(f"Item Number: {product['item_number']}")
        print(f"Sale Price: ${product['sale_price']:.2f}")
        if product['regular_price']:
            print(f"Regular Price: ${product['regular_price']:.2f}")
        print(f"Image URL: {product['image_url'][:60] if product['image_url'] else 'None'}...")
        if product['description']:
            desc_preview = product['description'][:200]
            print(f"Description: {desc_preview}...")
        print("=" * 60)
        return True
    else:
        print("\n✗ No product found or scraping failed")
        print("\nCheck the debug output above for details.")
        return False

if __name__ == "__main__":
    success = test_scraper()
    sys.exit(0 if success else 1)