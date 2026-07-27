"""Контракт ActionExecutor ×2: execute(target) применяет действие к сервису target."""
import os

from svc_watch.adapters.inmemory import InMemoryActionExecutor
from svc_watch.adapters.action_request_file import RequestFileAction


def test_inmemory_action_records_target():
    a = InMemoryActionExecutor()
    a.execute("udp_logger")
    a.execute("config_server")
    assert a.calls == ["udp_logger", "config_server"]


def test_request_file_creates_file_with_service(tmp_path):
    tmpl = os.path.join(str(tmp_path), "svc_crash_{service}")
    a = RequestFileAction(tmpl)
    a.execute("config_server")
    assert os.path.exists(os.path.join(str(tmp_path), "svc_crash_config_server"))
    # разные цели → разные файлы
    a.execute("ws_bridge")
    assert os.path.exists(os.path.join(str(tmp_path), "svc_crash_ws_bridge"))


def test_request_file_requires_placeholder():
    import pytest
    with pytest.raises(ValueError):
        RequestFileAction("/tmp/no_placeholder")
