"""
Database Time Repair Script
Run this ONCE to convert all times back to UTC if they were corrupted.

This script converts local times back to UTC (naive).
"""
import shutil
import sqlite3
import os
import sys
from datetime import datetime, timezone

def repair_database_times(db_path="festool_recon.db"):
    """Convert all local times back to UTC."""
    
    if not os.path.exists(db_path):
        print(f"❌ Database not found: {db_path}")
        return False
    
    print("=" * 60)
    print("  Database Time Repair Script")
    print("=" * 60)
    print()
    print(f"Database: {db_path}")
    print()
    print("This will convert all times from LOCAL back to UTC.")
    print("This is needed if times were incorrectly converted.")
    print()
    
    response = input("Continue? (yes/no): ").strip().lower()
    if response != 'yes':
        print("Cancelled.")
        return False
    
    # Take a copy first - this script rewrites timestamps in place and is not
    # idempotent; running it twice shifts every time by two offsets.
    backup_path = f"{db_path}.pre_repair_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    shutil.copy2(db_path, backup_path)
    print(f"Backed up current database to: {backup_path}\n")

    # Compute the local UTC offset once, up front.  It used to be assigned
    # inside the per-product loop, so an empty product table left it undefined
    # and the user/notification updates below raised NameError.
    offset = datetime.now() - datetime.now(timezone.utc).replace(tzinfo=None)

    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        # Get all products with timestamps
        cursor.execute("""
            SELECT id, posted_at, replaced_at
            FROM products
            WHERE posted_at IS NOT NULL
        """)

        products = cursor.fetchall()
        print(f"\nFound {len(products)} products to repair...")

        repaired = 0
        for prod_id, posted_at, replaced_at in products:
            try:
                # Parse the datetime string
                if posted_at:
                    # Parse as local time, convert to UTC
                    dt_local = datetime.fromisoformat(posted_at.replace(' ', 'T'))
                    # Assume it's local, make it aware
                    dt_local = dt_local.replace(tzinfo=None)

                    # Subtract the offset to get UTC
                    dt_utc = dt_local - offset
                    posted_utc = dt_utc.strftime('%Y-%m-%d %H:%M:%S')
                else:
                    posted_utc = None
                
                if replaced_at:
                    dt_local = datetime.fromisoformat(replaced_at.replace(' ', 'T'))
                    dt_local = dt_local.replace(tzinfo=None)
                    dt_utc = dt_local - offset
                    replaced_utc = dt_utc.strftime('%Y-%m-%d %H:%M:%S')
                else:
                    replaced_utc = None
                
                # Update the product
                cursor.execute("""
                    UPDATE products 
                    SET posted_at = ?, replaced_at = ?
                    WHERE id = ?
                """, (posted_utc, replaced_utc, prod_id))
                
                repaired += 1
                
                if repaired % 10 == 0:
                    print(f"  Repaired {repaired}/{len(products)} products...")
                    
            except Exception as e:
                print(f"  Warning: Could not repair product {prod_id}: {e}")
        
        # Also fix user login times
        cursor.execute("UPDATE users SET last_login = datetime(last_login, '-' || ? || ' hours') WHERE last_login IS NOT NULL", 
                      (offset.total_seconds() / 3600,))
        
        # Fix notification log times
        cursor.execute("UPDATE notification_logs SET sent_at = datetime(sent_at, '-' || ? || ' hours') WHERE sent_at IS NOT NULL",
                      (offset.total_seconds() / 3600,))
        
        conn.commit()
        conn.close()
        
        print(f"\n✓ Successfully repaired {repaired} products!")
        print()
        print("All times are now back to UTC (naive).")
        print("Display timezone is controlled by your settings.")
        print()
        print("=" * 60)
        
        return True
        
    except Exception as e:
        print(f"\n❌ Error repairing database: {e}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == "__main__":
    print()
    
    # Check for custom database path
    if len(sys.argv) > 1:
        db_path = sys.argv[1]
    else:
        db_path = "festool_recon.db"
    
    success = repair_database_times(db_path)
    
    if success:
        print()
        print("✓ Repair complete!")
        print()
        print("Next steps:")
        print("1. Restart the application: python main.py")
        print("2. Check that times display correctly")
        print("3. Switch between Local/UTC to verify both work")
        print()
        sys.exit(0)
    else:
        print()
        print("Repair failed. Please check the errors above.")
        print()
        sys.exit(1)