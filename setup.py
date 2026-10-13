"""
Setup script for Festool Recon Monitor
Helps with installation and initial configuration
"""
import os
import sys
import subprocess
from pathlib import Path


def print_header(text):
    """Print formatted header."""
    print("\n" + "=" * 60)
    print(f"  {text}")
    print("=" * 60 + "\n")


def check_python_version():
    """Check if Python version is compatible."""
    print_header("Checking Python Version")
    
    version = sys.version_info
    print(f"Current Python version: {version.major}.{version.minor}.{version.micro}")
    
    if version.major < 3 or (version.major == 3 and version.minor < 8):
        print("❌ Error: Python 3.8 or higher is required!")
        print("Please install Python 3.8+ from https://www.python.org/downloads/")
        return False
    
    print("✓ Python version is compatible")
    return True


def create_virtual_environment():
    """Create a virtual environment."""
    print_header("Creating Virtual Environment")
    
    venv_path = Path("venv")
    if venv_path.exists():
        print("⚠ Virtual environment already exists")
        response = input("Do you want to recreate it? (y/N): ").strip().lower()
        if response != 'y':
            print("Skipping virtual environment creation")
            return True
    
    try:
        print("Creating virtual environment...")
        subprocess.check_call([sys.executable, "-m", "venv", "venv"])
        print("✓ Virtual environment created successfully")
        return True
    except Exception as e:
        print(f"❌ Error creating virtual environment: {e}")
        return False


def get_pip_command():
    """Get the appropriate pip command for the platform."""
    if sys.platform == "win32":
        return os.path.join("venv", "Scripts", "pip.exe")
    else:
        return os.path.join("venv", "bin", "pip")


def install_dependencies():
    """Install required dependencies."""
    print_header("Installing Dependencies")
    
    pip_cmd = get_pip_command()
    
    if not os.path.exists(pip_cmd):
        print("⚠ Using system pip (virtual environment not found)")
        pip_cmd = "pip"
    
    try:
        print("Upgrading pip...")
        subprocess.check_call([pip_cmd, "install", "--upgrade", "pip"])
        
        print("\nInstalling dependencies from requirements.txt...")
        subprocess.check_call([pip_cmd, "install", "-r", "requirements.txt"])
        
        print("\n✓ Dependencies installed successfully")
        return True
    except Exception as e:
        print(f"❌ Error installing dependencies: {e}")
        return False


def create_directories():
    """Create necessary directories."""
    print_header("Creating Directories")
    
    directories = ["backups", "logs", "exports"]
    
    for directory in directories:
        path = Path(directory)
        if not path.exists():
            path.mkdir(parents=True)
            print(f"✓ Created directory: {directory}")
        else:
            print(f"⚠ Directory already exists: {directory}")
    
    return True


def test_installation():
    """Test if key modules can be imported."""
    print_header("Testing Installation")
    
    required_modules = [
        "PyQt6",
        "sqlalchemy",
        "requests",
        "bs4",
        "apscheduler",
        "bcrypt",
        "cryptography"
    ]
    
    failed = []
    
    for module_name in required_modules:
        try:
            __import__(module_name)
            print(f"✓ {module_name}")
        except ImportError:
            print(f"❌ {module_name} - FAILED")
            failed.append(module_name)
    
    if failed:
        print(f"\n⚠ Failed to import: {', '.join(failed)}")
        print("Try running: pip install -r requirements.txt")
        return False
    
    print("\n✓ All modules imported successfully")
    return True


def create_sample_config():
    """Create a sample configuration file."""
    print_header("Creating Sample Configuration")
    
    config_content = """# Festool Recon Monitor Configuration
# Copy this to config.ini and customize for your setup

[Monitoring]
# Check interval in minutes (minimum: 1)
check_interval = 5

# Enable automatic daily backup
auto_backup = True

[Notifications]
# Enable in-app notifications
in_app = True

# Include previous products (0, 1, 3, or 5)
include_previous = 1

# Include photos in notifications
include_photos = True

[Email]
# Enable email notifications
enabled = False

smtp_server = smtp.gmail.com
smtp_port = 587
username = your.email@gmail.com
password = your-app-password
from_email = your.email@gmail.com
to_email = recipient@example.com

[SMS]
# Enable SMS notifications
enabled = False

provider = Twilio
api_key = your-account-sid
api_secret = your-auth-token
from_number = +1234567890
to_number = +1234567890

[Pushbullet]
# Enable Pushbullet notifications
enabled = False

api_key = your-pushbullet-api-key

[Webhook]
# Enable webhook notifications
enabled = False

url = https://discord.com/api/webhooks/your-webhook-url
method = POST

[Filters]
# Keywords to watch for (comma-separated)
watchlist = KAPEX, domino, sander

# Keywords to exclude (comma-separated)
exclusions = cordless, battery

"""
    
    config_path = Path("config.sample.ini")
    if config_path.exists():
        print("⚠ config.sample.ini already exists")
    else:
        with open(config_path, 'w') as f:
            f.write(config_content)
        print("✓ Created config.sample.ini")
        print("  Copy to config.ini and customize for your setup")
    
    return True


def print_next_steps():
    """Print next steps for the user."""
    print_header("Setup Complete!")
    
    print("Next steps:")
    print("\n1. Activate the virtual environment:")
    
    if sys.platform == "win32":
        print("   venv\\Scripts\\activate")
    else:
        print("   source venv/bin/activate")
    
    print("\n2. (Optional) Copy config.sample.ini to config.ini and customize")
    print("   cp config.sample.ini config.ini")
    
    print("\n3. Run the application:")
    print("   python main.py")
    
    print("\n4. Configure notifications in Settings")
    
    print("\nFor help and documentation, see README.md")
    print("\n" + "=" * 60 + "\n")


def main():
    """Main setup function."""
    print("\n" + "=" * 60)
    print("  Festool Recon Monitor - Setup")
    print("=" * 60)
    
    # Check Python version
    if not check_python_version():
        return 1
    
    # Create virtual environment
    if not create_virtual_environment():
        print("\n⚠ Virtual environment creation failed, but you can continue")
        response = input("Continue with setup? (y/N): ").strip().lower()
        if response != 'y':
            return 1
    
    # Install dependencies
    if not install_dependencies():
        print("\n❌ Dependency installation failed")
        print("Please resolve the errors and run setup again")
        return 1
    
    # Create directories
    create_directories()
    
    # Test installation
    if not test_installation():
        print("\n⚠ Some modules failed to import")
        print("The application may not work correctly")
    
    # Create sample config
    create_sample_config()
    
    # Print next steps
    print_next_steps()
    
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n\nSetup interrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n\n❌ Unexpected error during setup: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)