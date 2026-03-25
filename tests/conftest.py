"""Root conftest: add src/foil_serve to sys.path so bare imports work."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "foil_serve"))
