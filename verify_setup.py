"""
Verification script - checks if everything is set up correctly
Run this before starting the app to verify your installation.
"""
import os
import sys
import sqlite3

def check_files():
    """Check if all required files exist."""
    print("Checking files...")
    
    required_files = [
        'main.py',
        'requirements.txt',
        'database/models.py',
        'database/manager.py',
        'scraper/festool_scraper.py',
        'scraper/monitor.py',
        'notifications/notifier.py',
        'analytics/sku_analytics.py',
        'utils/timezone_utils.py',
        'utils/export.py'
    ]
    
    missing = []
    for filepath in required_files:
        if os.path.exists(filepath):
            print(f"  ✓ {filepath}")
        else:
            print(f"  ✗ {filepath} - MISSING")
            missing.append(filepath)
    
    if missing:
        print(f"\n❌ Missing files: {len(missing)}")
        return False
    
    print(f"\n✓ All required files present\n")
    return True


def check_database():
    """Check database structure."""
    print("Checking database...")
    
    db_path = "festool_recon.db"
    
    if not os.path.exists(db_path):
        print("  ⚠ Database doesn't exist yet (will be created on first run)")
        return True
    
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # Check user_settings table
        cursor.execute("PRAGMA table_info(user_settings)")
        columns = [row[1] for row in cursor.fetchall()]
        
        required_columns = [
            'id', 'user_id', 'check_interval_minutes', 
            'timezone_mode', 'notify_in_app'
        ]
        
        for col in required_columns:
            if col in columns:
                print(f"  ✓ Column: {col}")
            else:
                print(f"  ✗ Column: {col} - MISSING")
        
        if 'timezone_mode' not in columns:
            print("\n  ❌ timezone_mode column missing!")
            print("  Run: python migrate_database.py")
            conn.close()
            return False
        
        # Count products
        cursor.execute("SELECT COUNT(*) FROM products")
        product_count = cursor.fetchone()[0]
        print(f"\n  ✓ Products in database: {product_count}")
        
        conn.close()
        print("✓ Database structure OK\n")
        return True
        
    except Exception as e:
        print(f"  ❌ Database error: {e}")
        return False


def check_imports():
    """Check if key modules can be imported."""
    print("Checking Python imports...")
    
    modules = [
        ('PyQt6', 'PyQt6.QtWidgets'),
        ('SQLAlchemy', 'sqlalchemy'),
        ('Requests', 'requests'),
        ('BeautifulSoup', 'bs4'),
        ('APScheduler', 'apscheduler'),
    ]
    
    failed = []
    for name, module in modules:
        try:
            __import__(module)
            print(f"  ✓ {name}")
        except ImportError:
            print(f"  ✗ {name} - NOT INSTALLED")
            failed.append(name)
    
    if failed:
        print(f"\n❌ Missing modules: {', '.join(failed)}")
        print("Run: pip install -r requirements.txt")
        return False
    
    print("✓ All modules available\n")
    return True


def check_utils_package():
    """Check if utils package is set up correctly."""
    print("Checking utils package...")
    
    # Check directory exists
    if not os.path.isdir('utils'):
        print("  ❌ utils/ directory doesn't exist")
        print("  Create it: mkdir utils")
        return False
    
    print("  ✓ utils/ directory exists")
    
    # Check __init__.py exists
    init_file = 'utils/__init__.py'
    if not os.path.exists(init_file):
        print(f"  ❌ {init_file} doesn't exist")
        print("  This file is needed to make utils a Python package")
        return False
    
    print(f"  ✓ {init_file} exists")
    
    # Try importing
    try:
        from utils.timezone_utils import TimezoneManager
        print("  ✓ Can import TimezoneManager")
    except ImportError as e:
        print(f"  ❌ Cannot import from utils: {e}")
        return False
    
    print("✓ Utils package OK\n")
    return True


def main():
    """Run all checks."""
    print("=" * 60)
    print("  Festool Recon Monitor - Setup Verification")
    print("=" * 60)
    print()
    
    checks = [
        ("Files", check_files),
        ("Python Imports", check_imports),
        ("Utils Package", check_utils_package),
        ("Database", check_database),
    ]
    
    results = []
    for name, check_func in checks:
        try:
            result = check_func()
            results.append((name, result))
        except Exception as e:
            print(f"❌ Error checking {name}: {e}")
            results.append((name, False))
    
    # Summary
    print("=" * 60)
    print("  Summary")
    print("=" * 60)
    print()
    
    for name, result in results:
        status = "✓ PASS" if result else "✗ FAIL"
        print(f"{status:10} {name}")
    
    passed = sum(1 for _, r in results if r)
    total = len(results)
    
    print()
    if passed == total:
        print("🎉 All checks passed! You're ready to run: python main.py")
        return 0
    else:
        print(f"⚠ {total - passed} check(s) failed. Fix the issues above before running the app.")
        return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n\nVerification interrupted")
        sys.exit(1)
    except Exception as e:
        print(f"\n\nUnexpected error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)