"""Общая обвязка тестов watchdog-v2: путь к библиотеке + валидная база конфига.

База берётся из БОЕВОГО svc_watch.conf (deepcopy на каждый вызов) — гарантирует,
что «валидное» в тестах не разъедется со схемой. Тесты мутируют базу точечно.
"""
import copy
import json
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))                 # <repo>/tests
_ROOT = os.path.dirname(_HERE)                                     # <repo> root (svc-watch)
_LIB = os.path.join(_ROOT, "src")                                  # svc_watch package + svc_watch_compat module
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

# The one full valid config lives with the RUT integration reference (see examples/rut-integration).
_CONF = os.path.join(_ROOT, "examples", "rut-integration", "svc_watch.conf")
with open(_CONF, "r", encoding="utf-8") as _f:
    _BASE_RAW = json.load(_f)


def base():
    """Свежая копия валидного боевого конфига (мутируй как хочешь)."""
    return copy.deepcopy(_BASE_RAW)


@pytest.fixture
def cfg():
    return base()
