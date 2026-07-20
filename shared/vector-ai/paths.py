"""Install-root paths for vector-ai.

All durable files (memory.db, persona.txt, debug log, workday.db) live next to
service.py in the deployed tree. Modules that need those paths import ROOT from
here rather than using Path(__file__).parent themselves (which would break if
they ever nested under a subpackage).
"""
from pathlib import Path

# Flat siblings of service.py: this file lives in the vector-ai install dir.
ROOT: Path = Path(__file__).resolve().parent
