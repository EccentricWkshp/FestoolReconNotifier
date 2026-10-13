"""
Installation test script for Festool Recon Monitor
Run this after installation to verify everything is working correctly.
"""
import sys
import os
from datetime import datetime


def print_section(title):
    """Print a formatted section header."""
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}\n")


def test_python_version():
    """Test Python version."""
    print("Testing Python version...")
    version = sys.version_info
    
    if version.major >= 3 and version.minor >= 8:
        print(f"✓ Python {version.major}.{version.minor}.{version.micro} - OK")
        return True
    else:
        print(f"✗ Python {version.major}.{version.minor}.{version.micro} - TOO OLD")
        print("  Required: Python 3.8+")
        return False


def test_imports():
    """Test if all required modules can be imported."""
    print("\nTesting module imports...")
    
    modules = {
        'PyQt6': 'PyQt6',
        'SQLAlchemy': 'sqlalchemy',
        'Requests': 'requests',
        'BeautifulSoup': 'bs4',
        'APScheduler': 'apscheduler',
        'bcrypt': 'bcrypt',
        'cryptography': 'cryptography',
        'pandas': 'pandas',
        'numpy': 'numpy',
        'lxml': 'lxml'
    }
    
    optional_modules = {
        'Pushbullet': 'pushbullet',
        'Twilio': 'twilio'
    }
    
    failed = []
    optional_failed = []
    
    # Test required modules
    for name, module in modules.items():
        try:
            __import__(module)
            print(f"✓ {name}")
        except ImportError as e:
            print(f"✗ {name} - FAILED")
            failed.append(name)
    
    # Test optional modules
    print("\nOptional modules (for advanced features):")
    for name, module in optional_modules.items():
        try:
            __import__(module)
            print(f"✓ {name}")
        except ImportError:
            print(f"- {name} - Not installed (optional)")
            optional_failed.append(name)
    
    if failed:
        print(f"\n⚠ Required modules failed: {', '.join(failed)}")
        print("Run: pip install -r requirements.txt")
        return False
    
    return True


def test_database():
    """Test database creation and basic operations."""
    print("\nTesting database...")
    
    try:
        from database.manager import DatabaseManager
        from database.models import Base
        
        # Create test database
        test_db = "test_festool.db"
        if os.path.exists(test_db):
            os.remove(test_db)
        
        db = DatabaseManager(db_path=test_db)
        print("✓ Database created")
        
        # Test user creation
        user = db.create_user("test_user", "test_password")
        print("✓ User creation works")
        
        # Test authentication
        auth_user = db.authenticate_user("test_user", "test_password")
        if auth_user:
            print("✓ Authentication works")
        else:
            print("✗ Authentication failed")
            return False
        
        # Test product creation
        product = db.add_product(
            item_number="TEST123",
            name="Test Product",
            sale_price=999.99,
            regular_price=1299.99,
            description="Test description"
        )
        print("✓ Product creation works")
        
        # Test retrieval
        latest = db.get_latest_product()
        if latest and latest.item_number == "TEST123":
            print("✓ Product retrieval works")
        else:
            print("✗ Product retrieval failed")
            return False
        
        # Cleanup
        os.remove(test_db)
        print("✓ Database test cleanup complete")
        
        return True
        
    except Exception as e:
        print(f"✗ Database test failed: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_scraper():
    """Test web scraper."""
    print("\nTesting web scraper...")
    
    try:
        from scraper.festool_scraper import FestoolScraper
        
        scraper = FestoolScraper()
        print("✓ Scraper initialized")
        
        # Test connection (don't actually scrape to be respectful)
        if scraper.test_connection():
            print("✓ Connection to festoolrecon.com successful")
        else:
            print("⚠ Could not connect to festoolrecon.com")
            print("  This might be a network issue - check your internet connection")
            return True  # Don't fail the test for network issues
        
        return True
        
    except Exception as e:
        print(f"✗ Scraper test failed: {e}")
        return False


def test_notifications():
    """Test notification system."""
    print("\nTesting notification system...")
    
    try:
        from notifications.notifier import NotificationManager
        
        manager = NotificationManager()
        print("✓ Notification manager initialized")
        
        # Test encryption
        test_config = {'api_key': 'test_key_123', 'secret': 'test_secret'}
        encrypted = manager.encrypt_config(test_config)
        print("✓ Config encryption works")
        
        decrypted = manager.decrypt_config(encrypted)
        if decrypted == test_config:
            print("✓ Config decryption works")
        else:
            print("✗ Decryption mismatch")
            return False
        
        return True
        
    except Exception as e:
        print(f"✗ Notification test failed: {e}")
        return False


def test_gui():
    """Test if GUI can be initialized."""
    print("\nTesting GUI components...")
    
    try:
        from PyQt6.QtWidgets import QApplication
        import sys
        
        # Create QApplication (needed for Qt)
        app = QApplication.instance()
        if app is None:
            app = QApplication(sys.argv)
        
        print("✓ Qt application initialized")
        
        # Don't actually create the main window - just test imports
        from main import MainWindow
        print("✓ Main window class imports successfully")
        
        return True
        
    except Exception as e:
        print(f"✗ GUI test failed: {e}")
        
        # Provide helpful error message for Windows DLL issues
        error_str = str(e).lower()
        if 'dll' in error_str or 'procedure' in error_str:
            print("\n⚠ This appears to be a Windows DLL issue.")
            print("  Common fix: Install Visual C++ Redistributables")
            print("  See WINDOWS_GUI_FIX.md for detailed solutions")
            print("\n  Quick fix: Download and install:")
            print("  https://aka.ms/vs/17/release/vc_redist.x64.exe")
            print("\n  The application may still work - try running: python main.py")
        
        return False


def test_file_structure():
    """Test if all necessary files exist."""
    print("\nTesting file structure...")
    
    required_files = [
        'main.py',
        'requirements.txt',
        'README.md',
        'database/models.py',
        'database/manager.py',
        'scraper/festool_scraper.py',
        'scraper/monitor.py',
        'notifications/notifier.py',
        'analytics/sku_analytics.py'
    ]
    
    missing = []
    for filepath in required_files:
        if os.path.exists(filepath):
            print(f"✓ {filepath}")
        else:
            print(f"✗ {filepath} - MISSING")
            missing.append(filepath)
    
    if missing:
        print(f"\n⚠ Missing files: {', '.join(missing)}")
        return False
    
    return True


def test_directories():
    """Test if necessary directories exist or can be created."""
    print("\nTesting directories...")
    
    directories = ['backups', 'logs', 'exports']
    
    for directory in directories:
        if not os.path.exists(directory):
            try:
                os.makedirs(directory)
                print(f"✓ Created {directory}/")
            except Exception as e:
                print(f"✗ Could not create {directory}/: {e}")
                return False
        else:
            print(f"✓ {directory}/ exists")
    
    return True


def run_all_tests():
    """Run all tests."""
    print_section("Festool Recon Monitor - Installation Test")
    print("This will verify your installation is working correctly.\n")
    
    results = {
        'Python Version': test_python_version(),
        'File Structure': test_file_structure(),
        'Directories': test_directories(),
        'Module Imports': test_imports(),
        'Database': test_database(),
        'Web Scraper': test_scraper(),
        'Notifications': test_notifications(),
        'GUI': test_gui()
    }
    
    # Print summary
    print_section("Test Summary")
    
    passed = sum(1 for v in results.values() if v)
    total = len(results)
    
    for test_name, result in results.items():
        status = "✓ PASS" if result else "✗ FAIL"
        print(f"{status:10} {test_name}")
    
    print(f"\nPassed: {passed}/{total}")
    
    if passed == total:
        print("\n🎉 All tests passed! Your installation is ready.")
        print("\nNext steps:")
        print("1. Run: python main.py")
        print("2. Configure notifications in Settings")
        print("3. Click 'Start Monitoring'")
        return 0
    elif passed >= total - 1:  # Only 1 failure
        print(f"\n⚠ {total - passed} test failed, but core functionality should work.")
        if not results.get('GUI'):
            print("\n💡 The GUI test failed, but the application might still work.")
            print("   Try running: python main.py")
            print("   If it fails, see WINDOWS_GUI_FIX.md for solutions.")
        if not results.get('Database'):
            print("\n⚠ Database test failed - this needs to be fixed.")
            print("   Try: pip install --force-reinstall sqlalchemy")
        return 0
    else:
        print("\n⚠ Some tests failed. Please check the errors above.")
        print("\nCommon fixes:")
        print("1. Run: pip install -r requirements.txt")
        print("2. Ensure all files are present")
        print("3. Check Python version is 3.8+")
        return 1


if __name__ == "__main__":
    try:
        sys.exit(run_all_tests())
    except KeyboardInterrupt:
        print("\n\nTest interrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n\n✗ Unexpected error during testing: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)