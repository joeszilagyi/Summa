from __future__ import annotations

import importlib.util
import subprocess

from tests_support import (
    module_from_spec_with_registration,
    subprocess_run_with_default_timeout,
)


# Register dynamically loaded test modules before execution so dataclass and
# similar runtime-introspection code can resolve sys.modules[__name__] safely.
importlib.util.module_from_spec = module_from_spec_with_registration

# Give test subprocesses a default timeout unless the call already sets one.
subprocess.run = subprocess_run_with_default_timeout
