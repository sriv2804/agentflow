# tests/unit/test_agent.py stubs src.core.flow and src.tools.common with
# sys.modules.setdefault(...) at import time. Without the real modules loaded
# first, every test file collected after it imports MagicMocks instead of the
# runtime. Importing them here (conftest loads before collection) makes that
# setdefault a no-op for the rest of the suite.
import src.core.flow  # noqa: F401
import src.tools.common  # noqa: F401
