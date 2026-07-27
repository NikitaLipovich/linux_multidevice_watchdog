"""svc_watch — supervision library (watchdog v2).

The config = the single source of truth. The layers are separate:
  config   — schema + parser + validator (this package, E1); runs NOTHING.
  contracts/core/adapters/runtime — execution (E2+).
"""
