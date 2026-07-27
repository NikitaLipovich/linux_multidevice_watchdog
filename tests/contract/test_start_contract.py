"""Контракт StartMechanism ×2: фабрика получает МОДЕЛЬ сервиса (FR-3),
create→handle, teardown bounded."""
from dataclasses import dataclass

from svc_watch.adapters.inmemory import InMemoryStartMechanism
from svc_watch.adapters.start_python_factory import PythonFactoryStart


@dataclass
class FakeService:
    name: str


def test_inmemory_start_create_teardown():
    sm = InMemoryStartMechanism()
    h = sm.create(FakeService("udp_logger"))
    assert sm.created == ["udp_logger"]
    assert sm.teardown(h, 20000) is True
    assert sm.torn_down == ["udp_logger"]


def test_inmemory_start_factory_failure_propagates():
    sm = InMemoryStartMechanism(fail_on=lambda s: s.name == "bad")
    import pytest
    with pytest.raises(RuntimeError):
        sm.create(FakeService("bad"))


def test_python_factory_receives_service_model():
    sm = PythonFactoryStart("dummy_factory:make")
    h = sm.create(FakeService("config_server"))
    assert h.service_name == "config_server"     # фабрика ВИДЕЛА модель (FR-3)
    assert sm.teardown(h, 20000) is True
    assert h.stopped is True
