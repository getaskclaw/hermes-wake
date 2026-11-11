import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _load():
    spec = importlib.util.spec_from_file_location(
        "hermes_wake", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules["hermes_wake"] = mod
    spec.loader.exec_module(mod)
    return mod


hermes_wake = _load()
