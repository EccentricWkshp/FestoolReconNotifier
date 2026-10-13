"""
Test if the validation logic is working correctly.
This simulates what happens when the monitor detects a change.
"""
import time
from scraper.festool_scraper import FestoolScraper
from database.manager import DatabaseManager

def test_validation():
    print("\n" + "=" * 70)
    print("VALIDATION LOGIC TEST")
    print("=" * 70)
    
    # Get current database state
    db = DatabaseManager()
    latest = db.get_latest_product()
    
    if latest:
        print(f"\nCurrent product in database:")
        print(f"  Item: {latest.item_number}")
        print(f"  Name: {latest.name}")
        last_item = latest.item_number
    else:
        print("\nNo products in database yet")
        last_item = None
    
    # Fetch current product
    print(f"\nFetching current product from website...")
    scraper = FestoolScraper()
    current = scraper.fetch_current_product()
    
    if not current:
        print("✗ Failed to fetch current product")
        return
    
    print(f"\nCurrent product on website:")
    print(f"  Item: {current['item_number']}")
    print(f"  Name: {current['name']}")
    print(f"  Price: ${current['sale_price']:.2f}")
    
    # Check if it's different
    if last_item and current['item_number'] == last_item:
        print(f"\n✓ Product hasn't changed (still #{current['item_number']})")
        print("  This is normal if no new product has been posted.")
        return
    
    # Product is different - test validation
    print(f"\n⚠️  PRODUCT CHANGE DETECTED!")
    print(f"  Database has: #{last_item}")
    print(f"  Website has: #{current['item_number']}")
    
    print(f"\nRunning validation (3 checks, 2 seconds apart)...")
    print("This simulates what the monitor does...")
    
    validation_results = []
    for i in range(3):
        print(f"\n  Validation check #{i+1}:")
        time.sleep(2)
        
        validation_scraper = FestoolScraper()
        validation_product = validation_scraper.fetch_current_product()
        
        if not validation_product:
            print(f"    ✗ Failed to fetch")
            validation_results.append(None)
            continue
        
        validation_item = validation_product['item_number']
        print(f"    Item: {validation_item}")
        
        if validation_item != current['item_number']:
            print(f"    ✗ MISMATCH! Expected {current['item_number']}, got {validation_item}")
            validation_results.append(False)
        else:
            print(f"    ✓ Confirmed {validation_item}")
            validation_results.append(True)
    
    # Analyze validation
    print("\n" + "=" * 70)
    print("VALIDATION RESULTS")
    print("=" * 70)
    
    if all(validation_results):
        print("\n✓ ALL VALIDATIONS PASSED")
        print("  The change would be accepted and saved to database.")
        print("  If this isn't happening in the app, check monitor.py logic.")
    elif any(r == False for r in validation_results):
        print("\n✗ VALIDATION FAILED - Inconsistent data")
        print("  The scraper is returning different item numbers!")
        print(f"  Results: {validation_results}")
        print("\n  This explains why the app isn't updating:")
        print("  The monitor rejects changes that don't validate consistently.")
        print("\n  Possible solutions:")
        print("  1. Reduce validation checks from 3 to 1")
        print("  2. Increase delay between checks from 2s to 5s")
        print("  3. Disable validation entirely (not recommended)")
    elif None in validation_results:
        print("\n✗ SOME VALIDATIONS FAILED TO FETCH")
        print("  Network issues or scraping errors occurred.")
        print("  Check logs for details.")
    
    print("\n" + "=" * 70)

if __name__ == "__main__":
    test_validation()