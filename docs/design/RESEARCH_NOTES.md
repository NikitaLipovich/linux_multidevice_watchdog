# RESEARCH_NOTES — внешние аналоги супервизии: взято / адаптировано / отвергнуто

> Масштаб-контекст для всех решений: RUT956, ~128МБ RAM (свободно 10–29МБ), stdlib-only
> (внешних pip-пакетов на боксе нет), один asyncio event loop, наблюдатель — отдельный C-демон.
> Формат: механизм аналога → наш вердикт (BERËM / АДАПТ / ОТКАЗ) → куда (ключ конфига | правило FR).

## 1. Erlang/OTP supervisor

| Механизм | Вердикт | Куда / почему |
|---|---|---|
| `intensity`/`period`: >MaxR рестартов за MaxT сек → супервизор сдаётся (гасит детей и себя) | **BERËM (адапт)** | ГЛАВНАЯ находка. У нас терминальная ступень лестницы повторяется вечно → бесконечный рестарт неизлечимого жрёт RAM/CPU/лог. → новый `actions.*.rate_limit` (итер.7): N исполнений за T по одной цели → cooldown+аларм. У нас нет «up», куда эскалировать, поэтому не «умереть», а замедлиться и громко сигналить. FR-30. |
| `one_for_one` (рестарт только упавшего) | BERËM | это наш `recreate` (точечно). |
| `one_for_all` (упал один → рестарт всех) | ОТКАЗ (для сервисов) | не нужен на уровне сервисов; ближайший аналог — `restart_unified` (весь процесс), уже есть. |
| `restart: permanent/temporary/transient` | АДАПТ | permanent = обычный сервис; temporary («не рестартить, только следить») = `when_unhealthy mode:"log"`. transient (рестарт только при аномальном выходе) — нет понятия «нормальный выход» у вечного сервиса → ОТКАЗ. |
| `shutdown` timeout (worker 5000, sup infinity) | BERËM | это `supervisor.stop_timeout_ms`. |
| порядок старта детей + остановка в ОБРАТНОМ порядке | АДАПТ (боундэри) | наши сервисы независимы (стартуют конкурентно). Зависимости старта/остановки — известная граница (FR-41), добавится ключом `depends_on` при доказанной нужде. |

## 2. systemd `sd_notify`

| Механизм | Вердикт | Куда / почему |
|---|---|---|
| `WATCHDOG=1` keep-alive ping | BERËM (уже есть) | это наш pulse. |
| `READY=1`: готовность СОБЫТИЕМ (Type=notify ждёт READY до «active») | **BERËM (адапт)** | наш `grace_ms` — УГАДАННОЕ время слепоты после старта. Событие точнее: процесс сигналит «поднялся» → grace кончается сразу; время становится ВЕРХНЕЙ границей (fallback). → типизированный grace (итер.7): `{type:"fixed"}` \| `{type:"until_ready", max_ms}`. FR-31. |
| `STOPPING=1`: объявить плановую остановку | АДАПТ (боундэри) | у нас плановую остановку процесса ведёт procd (наблюдатель видит смерть+respawn+grace). Само-инициированная остановка сервиса — граница; сейчас достаточно pause_file для работ. |
| `NOTIFY_SOCKET` отсутствует вне systemd → всё no-op | BERËM (принцип) | ровно наш контракт: вне вотчдог-среды сигналы не излучаются, код сервиса работает. FR-11. |

## 3. supervisord

| Механизм | Вердикт | Куда / почему |
|---|---|---|
| `startsecs`: процесс должен прожить N сек, иначе не «RUNNING» → BACKOFF | **BERËM (адапт)** | «фабрика вернулась» ≠ «сервис поднялся»: сервис может умирать через 1с в цикле. → `supervisor.min_stable_ms` (итер.7): старт засчитан, только если сервис прожил столько; раньше — как упавшая фабрика (backoff). FR-32. |
| `startretries` → FATAL (сдаться) | BERËM (через rate_limit) | тот же смысл, что OTP intensity; покрыт `actions.*.rate_limit`. |
| `autorestart: false/true/unexpected` | АДАПТ | true=обычный; false=`mode:"log"`. unexpected (по кодам возврата) — нет у вечного сервиса → ОТКАЗ. |
| BACKOFF растущими паузами | BERËM (уже есть) | `supervisor.backoff` и `framework.pacing.backoff`. |
| event listeners (pub/sub состояний) | ОТКАЗ | избыточно; наблюдаемость через лог (event_log/runtime_log). |

## 4. Kubernetes probes + lifecycle

| Механизм | Вердикт | Куда / почему |
|---|---|---|
| `startupProbe` отключает liveness до успеха | BERËM (совпадает) | ровно смысл `until_ready`-grace (см. systemd READY). Подтверждает выбор. |
| `failureThreshold` (N подряд провалов → act) | АДАПТ | мы судим по ВРЕМЕНИ (`dead_after_ms`), не по счётчику проб — эквивалентно и проще (пульс-модель, не пулл-проба). ОТКАЗ от счётчика. |
| `terminationGracePeriodSeconds` + preStop (bounded graceful) | BERËM (есть) | = `stop_timeout_ms` + teardown-контракт. preStop-хук как «команда перед остановкой» — не нужен (teardown в коде фабрики). |
| liveness «is it stuck?» отдельно от readiness | BERËM (уже разделено) | наш L1 (жив) vs L2 (поток завис) — то же разделение, глубже. |

## 5. Structured concurrency (trio / anyio)

| Механизм | Вердикт | Куда / почему |
|---|---|---|
| nursery ВЛАДЕЕТ задачами; блок не выходит, пока все дети не завершены; падение → отмена братьев | **BERËM (принцип)** | v1.2 делал вручную (gather+cancel, ловил GC-сирот). Правило: супервизор владеет всеми задачами сервиса, teardown отменяет детерминированно, сирот нет. FR-20, FR-21. |
| детерминированная отмена, нет дрейфующих сирот | BERËM | прямо лечит GC-ловушку G3 (слабая ссылка на task). FR-20. |
| (сам trio/anyio как зависимость) | ОТКАЗ | stdlib-only; берём ПРИНЦИП (TaskGroup-владение), реализуем на asyncio 3.9-совместимо (не 3.11 TaskGroup — на боксе питон старше). FR-50. |

## 6. tenacity (модель retry)

| Механизм | Вердикт | Куда / почему |
|---|---|---|
| композиция stop-условий (`stop_after_attempt | stop_after_delay`) | АДАПТ | наша лестница = stop_after_attempt на ступень (`tries`); stop_after_delay покрыт `rate_limit.per_ms`. Композиция «\|» не нужна (одна ось). |
| `wait_exponential` (backoff) | BERËM (есть) | `pacing.backoff`. |
| jitter | ОТКАЗ | один процесс на одном боксе — «стада» нет; jitter только зашумил бы лог. |
| `retry_if_exception_type` (по типу ошибки) | ОТКАЗ | наблюдатель судит по СИГНАЛУ (пульс/счётчик), не по исключению; исключения фабрики → backoff супервизора (не настраивается по типу — не показана нужда). |

## Итог: что порождает изменения (итерация 7 конфига)

1. **`actions.*.rate_limit`** (OTP intensity + supervisord startretries + tenacity stop) —
   защита от бесконечного цикла рестартов; cooldown+аларм вместо вечного долбления.
2. **типизированный `launch.grace`** `fixed | until_ready` (systemd READY + k8s startupProbe) —
   готовность событием, время слепоты = верхняя граница.
3. **`supervisor.min_stable_ms`** (supervisord startsecs) — «поднялся» = прожил N, не «фабрика вернулась».

Что порождает ПРАВИЛА кода (FRAMEWORK_RULES.md), не ключи: владение задачами (trio),
clock-инъекция (trio + потребитель B), never-raise emit (systemd no-op), bounded teardown (k8s preStop).

## Источники
- OTP supervisor: https://www.erlang.org/doc/system/sup_princ.html · https://www.erlang.org/doc/apps/stdlib/supervisor.html
- sd_notify: https://www.freedesktop.org/software/systemd/man/latest/sd_notify.html
- supervisord subprocess: https://supervisord.org/subprocess.html
- k8s probes: https://kubernetes.io/docs/tasks/configure-pod-container/configure-liveness-readiness-startup-probes/ · lifecycle: https://kubernetes.io/docs/concepts/containers/container-lifecycle-hooks/
- structured concurrency (anyio): https://mattwestcott.org/blog/structured-concurrency-in-python-with-anyio
- tenacity: https://tenacity.readthedocs.io/ · https://github.com/jd/tenacity
