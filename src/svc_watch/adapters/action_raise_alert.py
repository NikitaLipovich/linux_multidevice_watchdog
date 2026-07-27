"""action_raise_alert — extension EXAMPLE (Layer 3): a new action type in ONE file.

Demonstrates FR-40/41: a new kind of action = a new file adapter + `type` in the config;
core.py and runtime.py are NOT edited. The type is parameterless (only type+rate_limit), so
config.py does not need touching either — the validator accepts any registered type.

execute(target) sends an alert (here — to the passed-in sink; in production — syslog/notification).
"""

from __future__ import annotations

from typing import Callable, List, Optional


class RaiseAlertExecutor:
    def __init__(self, sink: Optional[Callable[[str], None]] = None) -> None:
        self.alerts: List[str] = []
        self._sink = sink

    def execute(self, target: str) -> None:
        self.alerts.append(target)
        if self._sink is not None:
            self._sink(target)


# ─── self-registration into BOTH registries: validator (type) + builder (runtime) ───
def _register() -> None:
    from .. import config as cfgmod
    from ..runtime import register_action_builder
    cfgmod.register_type("action", "raise_alert")
    register_action_builder("raise_alert", lambda a: RaiseAlertExecutor())


_register()
