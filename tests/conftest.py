from __future__ import annotations

import importlib.util

from tests_support import module_from_spec_with_registration


# Register dynamically loaded test modules before execution so dataclass and
# similar runtime-introspection code can resolve sys.modules[__name__] safely.
importlib.util.module_from_spec = module_from_spec_with_registration
