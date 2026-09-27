"""Allows `python -m Lab_nas ...` (useful when the lab_nas script isn't on PATH)."""
import sys

from .cli import main

sys.exit(main())
