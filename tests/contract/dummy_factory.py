"""Фабрика-пустышка для контрактного теста start_python_factory (entry 'dummy_factory:make').
Получает МОДЕЛЬ сервиса параметром (FR-3), возвращает handle со stop()."""


class Handle:
    def __init__(self, service):
        self.service_name = service.name
        self.stopped = False

    def stop(self):
        self.stopped = True


def make(service):
    return Handle(service)
