"""Self-test helpers: swap a sibling module for a fake inside this package.

``from . import importer`` returns the package attribute when it exists and
otherwise imports ``<package>.importer``, so a fake must replace both.
"""

from __future__ import annotations

import sys
from typing import Any, Optional


def sibling_get(package_name: str, name: str) -> Optional[Any]:
    """Return the module ``from . import name`` would give right now, or None."""
    package = sys.modules[package_name]
    return getattr(package, name, None) or sys.modules.get("{}.{}".format(package_name, name))


def sibling_set(package_name: str, name: str, module: Optional[Any]) -> None:
    """Make ``from . import name`` return ``module`` (None makes the import fail)."""
    package = sys.modules[package_name]
    sys.modules["{}.{}".format(package_name, name)] = module
    if module is None:
        if hasattr(package, name):
            delattr(package, name)
    else:
        setattr(package, name, module)


def sibling_pop(package_name: str, name: str) -> None:
    """Forget a swapped module so the next import loads the real one."""
    package = sys.modules[package_name]
    sys.modules.pop("{}.{}".format(package_name, name), None)
    if hasattr(package, name):
        delattr(package, name)
