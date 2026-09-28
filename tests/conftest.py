"""让 tests 直接 import 包（src/jobforge），无需先设 PYTHONPATH。"""
import os
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
