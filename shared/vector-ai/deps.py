"""Process-wide singletons set by service.py at import time.

Leaf modules (prompt_assembly, response_cleanup, routes, etc.) import from here
instead of importing service, which would create cycles. service.py constructs
MEMORY / BEHAVIOR_RUNTIME / configs and assigns them onto this module.
"""
from typing import Any, Optional

# Populated by service.py immediately after construction.
MEMORY: Any = None
BEHAVIOR_RUNTIME: Any = None
_runtime_cfg: Any = None
_workday_cfg: Any = None
JOKE_CFG: Any = None
_continuity: Any = None
