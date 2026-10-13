"""
Database Migration Script for Timezone Feature
Run this ONCE before starting the app if you get timezone_mode errors.
"""
import sqlite3
import os
import sys

def migrate_database(db_path="festool_recon.db"):
    """Add timezone_mode column to existing database."""
    
    if not os.path.exists(db_path):
        print(f"❌ Database not found: {db_path}")
        print("   No migration needed (will be created on first run)")
        return True
    
    print("=" * 60)
    print("  Festool Recon Monitor - Database Migration")
    print("=" * 60)
    print()
    print(f"Database: {db_path}")
    print()
    
    try:
        # Connect to database
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # Check if timezone_mode column already exists
        cursor.execute("PRAGMA table_info(user_settings)")
        columns = [row[1] for row in cursor.fetchall()]
        
        if 'timezone_mode' in columns:
            print("✓ timezone_mode column already exists")
            print("  No migration needed!")
            conn.close()
            return True
        
        print("Adding timezone_mode column...")
        
        # Add the column
        cursor.execute("""
            ALTER TABLE user_settings 
            ADD COLUMN timezone_mode VARCHAR(10) DEFAULT 'local'
        """)
        
        # Verify it was added
        cursor.execute("PRAGMA table_info(user_settings)")
        columns = [row[1] for row in cursor.fetchall()]
        
        if 'timezone_mode' not in columns:
            print("❌ Failed to add column")
            conn.close()
            return False
        
        # Commit changes
        conn.commit()
        conn.close()
        
        print("✓ timezone_mode column added successfully!")
        print()
        print("=" * 60)
        print("  Migration Complete!")
        print("=" * 60)
        print()
        print("You can now run: python main.py")
        print()
        
        return True
        
    except sqlite3.OperationalError as e:
        print(f"❌ Database error: {e}")
        print()
        print("This might be because:")
        print("  1. The database is locked (close the app first)")
        print("  2. The database file is corrupted")
        print("  3. Permissions issue")
        return False
        
    except Exception as e:
        print(f"❌ Unexpected error: {e}")
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
    
    success = migrate_database(db_path)
    
    if success:
        sys.exit(0)
    else:
        print()
        print("Migration failed. Please check the errors above.")
        print()
        sys.exit(1)