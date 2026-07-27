# tests — проверяемые ворота (pytest)

- `invalid_config/` (Э1-ворот) — по кейсу на каждое нарушение правил 1–17: unknown type,
  битая @-ссылка, неизвестный глагол, пропуск ключа, ОПЕЧАТКА ключа, tries:0, не-последний шаг
  без tries, мёртвый pacing-блок, инертный rate_limit, дубль имени между процессами, stop на
  процесс-рестарте, until_ready без ready, from-форма пульса, activity↔L2 парность, ёмкости,
  числовые связки. Каждый = внятная ошибка с путём ключа.
- `contract/` (Э2-ворот) — ОДИН набор × 2 реализации на контракт (real + inmemory):
  transport×2, probe×2, start×2, action×2; core на FakeClock без реального сна.
- `extension/` (Э3-ворот) — новый type только файлами; `git diff --stat` core.py/runtime.py = пусто (FR-40).

Запуск фреймворк-сьюта: `python -m pytest tests -q` (из корня репы).

RUT v1.2→v2 миграционные тесты (`svc_watch_compat` + `wd_runtime`/`wd_beat`, RUT-специфика) вынесены
из фреймворк-сьюта в `examples/rut-integration/tests/` — запуск отдельно:
`python -m pytest examples/rut-integration/tests -q`.
