from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

DEFAULT_SUBPROCESS_TIMEOUT_SECONDS = 300.0
_ORIGINAL_MODULE_FROM_SPEC = importlib.util.module_from_spec
_ORIGINAL_SUBPROCESS_RUN = subprocess.run


def module_from_spec_with_registration(spec: importlib.machinery.ModuleSpec) -> ModuleType:
    module = _ORIGINAL_MODULE_FROM_SPEC(spec)
    sys.modules[spec.name] = module
    return module


def load_module_from_path(module_name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load module spec from {path}")
    module = module_from_spec_with_registration(spec)
    spec.loader.exec_module(module)
    return module


def subprocess_run_with_default_timeout(*popenargs: Any, timeout: float | None = None, **kwargs: Any):
    if timeout is None:
        timeout = DEFAULT_SUBPROCESS_TIMEOUT_SECONDS
    return _ORIGINAL_SUBPROCESS_RUN(*popenargs, timeout=timeout, **kwargs)
