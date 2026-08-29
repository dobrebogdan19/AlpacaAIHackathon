"""Make the repo root importable from tests (so `import engine` works)."""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
