"""
Simple diagnostic - what does the scraper see vs what's on the website?
Run this when you KNOW a new product is posted but the app doesn't show it.
"""
import sys
import time

def main():
    print("\n" + "=" * 70)
    print("SIMPLE DIAGNOSTIC")
    print("=" * 70)
    print("\nThis will fetch the product 3 times and show what the scraper sees.")
    print("\nBEFORE RUNNING:")
    print("1. Open festoolrecon.com in your browser")
    print("2. Note the CURRENT item number showing on the website")
    print("3. If a NEW product just posted, note that item number too")
    
    print("\n" + "=" * 70)
    browser_item = input("What item number do you see in your BROWSER right now? ")
    print("=" * 70)
    
    print("\nFetching with scraper 3 times (2 second delay)...\n")
    
    from scraper.festool_scraper import FestoolScraper
    
    results = []
    for i in range(3):
        print(f"--- Fetch #{i+1} ---")
        
        # Create fresh scraper
        scraper = FestoolScraper()
        product = scraper.fetch_current_product()
        
        if product:
            print(f"Item: {product['item_number']}")
            print(f"Name: {product['name']}")
            print(f"Price: ${product['sale_price']:.2f}")
            results.append(product['item_number'])
        else:
            print("✗ Failed to fetch")
            results.append(None)
        
        if i < 2:
            print("Waiting 2 seconds...\n")
            time.sleep(2)
    
    # Analysis
    print("\n" + "=" * 70)
    print("RESULTS")
    print("=" * 70)
    
    print(f"\nBrowser shows: Item #{browser_item}")
    print(f"Scraper found: {set(r for r in results if r)}")
    
    if all(r == browser_item for r in results if r):
        print("\n✓ SCRAPER MATCHES BROWSER - No stale data problem!")
        print("  If the app isn't showing the new product, the issue is elsewhere.")
        print("  Check the monitor/validation logic in monitor.py")
    else:
        print("\n✗ SCRAPER SEES DIFFERENT DATA THAN BROWSER")
        print(f"  Browser: #{browser_item}")
        print(f"  Scraper: {results}")
        print("\n  This means the scraper IS getting stale data.")
        print("  Possible causes:")
        print("  1. Server-side caching (CDN)")
        print("  2. DNS caching (hitting same stale edge node)")
        print("  3. Network/ISP caching")
    
    print("\n" + "=" * 70)

if __name__ == "__main__":
    main()