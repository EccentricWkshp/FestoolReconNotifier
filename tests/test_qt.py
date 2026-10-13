# test_qt.py
import sys

try:
    from PyQt6.QtWidgets import QApplication, QLabel
    app = QApplication(sys.argv)
    label = QLabel("PyQt6 is working!")
    label.show()
    print("✓ PyQt6 GUI works!")
    sys.exit(0)
except Exception as e:
    print(f"✗ PyQt6 failed: {e}")
    sys.exit(1)