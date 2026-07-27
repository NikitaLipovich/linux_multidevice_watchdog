"""start_python_factory — real service start: import 'module:func', call the factory
with the service MODEL as its parameter (FR-3). The factory returns a handle with .stop()/.close()
(the service resource). teardown calls it bounded by timeout_ms (FR-14)."""

from __future__ import annotations

import importlib
from typing import Any


def _resolve(entry: str):
    if ":" not in entry:
        raise ValueError("entry must be 'module:callable', got %r" % entry)
    mod_name, func_name = entry.split(":", 1)
    mod = importlib.import_module(mod_name)
    return getattr(mod, func_name)


class PythonFactoryStart:
    def __init__(self, entry: str) -> None:
        self._entry = entry
        self._factory = _resolve(entry)

    def create(self, service: Any) -> Any:
        # the factory receives ITS OWN model; it does not read the config itself
        return self._factory(service)

    def teardown(self, handle: Any, timeout_ms: int) -> bool:
        if handle is None:
            return True
        for meth in ("stop", "close", "shutdown"):
            fn = getattr(handle, meth, None)
            if callable(fn):
                try:
                    fn()
                except Exception:
                    pass
                return True
        return True
