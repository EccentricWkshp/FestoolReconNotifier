"""Diagnostic tool to test if we're getting cached data."""
import sys
import time
from scraper.festool_scraper import FestoolScraper

def main():
    print("=" * 60)
    print("Cache Diagnostic Test")
    print("=" * 60)
    print("\nThis will fetch the product 5 times with 2-second delays.")
    print("If the item number stays the same across all checks,")
    print("we're likely getting cached data.\n")
    
    results = []
    
    for i in range(1, 6):
        print(f"\n--- Check #{i} ---")
        
        # Create fresh scraper instance
        scraper = FestoolScraper()
        
        # Fetch product
        product = scraper.fetch_current_product()
        
        if product:
            item = product['item_number']
            name = product['name'][:50]
            price = product['sale_price']
            
            print(f"✓ Found: {name}")
            print(f"  Item #: {item}")
            print(f"  Price: ${price}")
            
            results.append({
                'check': i,
                'item_number': item,
                'name': name,
                'price': price
            })
        else:
            print("✗ No product found")
            results.append({
                'check': i,
                'item_number': None,
                'name': None,
                'price': None
            })
        
        if i < 5:
            print("Waiting 2 seconds...")
            time.sleep(2)
    
    # Analysis
    print("\n" + "=" * 60)
    print("ANALYSIS")
    print("=" * 60)
    
    item_numbers = [r['item_number'] for r in results if r['item_number']]
    
    if not item_numbers:
        print("\n✗ FAILED: Could not fetch any products")
        return 1
    
    if len(set(item_numbers)) == 1:
        print(f"\n⚠️  All checks returned same item: {item_numbers[0]}")
        print("This is EXPECTED if the product hasn't actually changed.")
        print("\nTo test for caching issues:")
        print("1. Note the current item number")
        print("2. Wait for the product to change on festoolrecon.com")
        print("3. Run this test again immediately")
        print("4. If it still shows the OLD item number, we have caching")
    else:
        print(f"\n✓ Got different items: {set(item_numbers)}")
        print("This suggests either:")
        print("  - Product changed during our test (timing coincidence)")
        print("  - OR we have an inconsistent caching issue")
    
    # Show all results
    print("\n--- All Results ---")
    for r in results:
        if r['item_number']:
            print(f"Check {r['check']}: Item #{r['item_number']} - ${r['price']}")
        else:
            print(f"Check {r['check']}: Failed to fetch")
    
    print("\n" + "=" * 60)
    return 0

if __name__ == "__main__":
    sys.exit(main())