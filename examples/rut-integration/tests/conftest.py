"""RUT v1.2→v2 migration tests — NOT part of the framework `tests/` suite.

These verify that the RUT project's retired v1.2 runtime (`wd_runtime`/`wd_beat`, which read a
v1.2 *view* of the single `svc_watch.conf` via `svc_watch_compat`) reproduces the exact values the
old `svc_watchdog.conf` used to carry. They are RUT-specific (service names, socket path, entry
points) and need the glue co-located in this folder plus the framework package under `../../../src`.

Run them explicitly (they are outside the framework suite):
    python -m pytest examples/rut-integration/tests -q
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))                       # examples/rut-integration/tests
_RUT = os.path.dirname(_HERE)                                            # examples/rut-integration
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(_HERE)))         # repo root (svc-watch)
_SRC = os.path.join(_ROOT, "src")                                        # svc_watch package
for _p in (_SRC, _RUT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# The tests read `_LIB/svc_watch.conf` and import svc_watch_compat / wd_runtime / wd_beat from _LIB.
_LIB = _RUT
