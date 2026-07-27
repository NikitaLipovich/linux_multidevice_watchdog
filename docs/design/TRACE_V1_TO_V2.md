# TRACE_V1_TO_V2 — трассировочная матрица: каждое поведение v1.2 → место в v2

> Категории «куда в v2»:
> **КЛЮЧ** = ключ конфига v2 (путь) · **АДАПТЕР** = реализация type в adapters/ ·
> **CORE** = семантика ядра (не настройка; описана в DESIGN.md) ·
> **BOOTSTRAP** = нужно до чтения конфига · **ПРОТОКОЛ** = контракт формата/тест-хуков (PROTOCOL.md) ·
> **COMPILE-TIME** = согласованная ёмкость C · **СЕРВИС-СЛОЙ** = чужая подсистема (не вотчдог) ·
> **УПРАЗДНЕНО → чем** = поведение заменено структурой v2.
>
> Дыры схемы, найденные обходом (исправлены в CONFIG_V2_DRAFT в этом же заходе):
> 1. consume-timeout — сигнал о ПРОЦЕССЕ (супервизор мёртв), а не ступень лестницы сервиса
>    → новый процессный уровень `P2_request_stuck`; правило «действие провалилось → следующая
>    ступень» из семантики лестницы УБРАНО (лестницу двигает только tries-исчерпание).
> 2. fallback-пути runtime-лога были только в коде → ключ `supervisor.log.fallbacks`.

## 1. artifacts/watchdog-v1/src/svc_watchdog.c (демон)

| Поведение / константа | Где | Куда в v2 |
|---|---|---|
| путь конфига: argv[1], дефолт /etc/svc_watchdog.conf | main() | BOOTSTRAP (argv; py — env WD_CONFIG) |
| fail-closed парс: все missing key разом, отказ | req_*/load_config | CORE политика конфиг-слоя (Э1/Э4); сами ключи → новая схема |
| строка длиннее буфера = отказ, не усечение | req_strcpy | CORE валидатора C + COMPILE-TIME ёмкости |
| словари значений action/mode, отказ на чужое | parse_action/load_config | CORE валидатора: словари type/mode/from |
| валидатор min_free > floor | load_config | КЛЮЧ gates.min_free_mb > gates.alarm_mb (правило №6 валидатора) |
| валидатор timeout ≥ 3×beat | load_config | правило: watch.L1.dead_after_ms ≥ 3×signals.pulse.every_ms |
| валидатор after_attempts ≥ 1 | load_config | правило 15 (ladder[].tries ≥ 1 + достижимость шагов). ИСПРАВЛЕНО итер.8: TRACE утверждал перенос, но валидатора не было — ревью поймало |
| services[] непуст, > MAX_SERVICES = ошибка | load_config | CORE валидатора + COMPILE-TIME MAX_SERVICES=30 |
| разворачивание crash-шаблона {name}, переполнение = ошибка | load_config | АДАПТЕР request_file ({service}) + валидатор длины |
| MemAvailable из /proc/meminfo (+env WD_PROC_MEMINFO) | mem_available_mb() | АДАПТЕР ресурсов; /proc = системный путь; env = тест-хук (ПРОТОКОЛ) |
| load1 из /proc/loadavg (+env) | load1() | то же |
| floor-сигнал mem_below_floor (один раз до восстановления) | handle_tick | КЛЮЧ gates.alarm_mb; дедуп по состоянию = CORE |
| возраст процесса: pidfile → /proc/<pid>/stat + uptime | process_age_ms() | КЛЮЧ launch.pidfile; механика = CORE (fresh-гейт) |
| fresh-гейт В НАЧАЛЕ тика: молодой процесс → полный grace + сброс | handle_tick | КЛЮЧ launch.grace_ms; порядок = CORE |
| resource_gate: ждать, waiting_resources лог, force после max_wait | resource_gate() | КЛЮЧИ gates.recheck_ms / gates.force_after_ms |
| паузы попыток fixed/backoff | delay_for_attempt() | КЛЮЧ framework.pacing |
| рестарт процесса ТОЛЬКО fork/exec "/bin/sh <initd> restart" | restart_process() | АДАПТЕР init_script; /bin/sh + глагол restart = ПРОТОКОЛ действия |
| proc_attempts РОС, delay мог расти, но БЕЗ потолка/сдачи → вечный рестарт неизлечимого | restart_process/delay_for_attempt | **ДОПОЛНЕНО ресёрчем Р1 → actions.*.rate_limit** (v1.2-предок был неограничен; итер.7 кап N-за-T → cooldown+аларм, FR-30) |
| после рестарта: grace + полный сброс состояний всех сервисов | restart_process() | CORE (реакция на исполнение process-действия) |
| pause_file → только лог paused (rate-limit) | handle_tick | КЛЮЧИ observer.pause_file, observer.quiet.paused_ms |
| no_pulse_all: ВСЕ тихие → рестарт процесса (гейт observe) | handle_tick | КЛЮЧ processes.*.watch.P1_all_pulses_lost (гейт → mode + observer.mode) |
| per-service silent: now−last_seen > timeout (ref=grace для невиданных) | handle_tick | КЛЮЧ watch.L1_pulse_lost.dead_after_ms; ref-семантика = CORE |
| L2 stalled: episode ∧ state=="active" ∧ счётчик замер > stall_ms | handle_tick | КЛЮЧ watch.L2_activity_frozen.frozen_after_ms; "active" = ПРОТОКОЛ text_v1 |
| pulse_back / progress_back: лог + полный сброс попыток | handle_tick | CORE: выздоровление = лестница на ступень 0 |
| crash-файл создан → ждём поедания | handle_tick | АДАПТЕР request_file |
| съеден → окно max(delay, relaunch_ms) до следующего суда | handle_tick | КЛЮЧ actions.recreate.startup_ms (было process.service_relaunch_ms — переехало в действие) |
| stall-рестарт съеден → ПОЛНЫЙ сброс эпизода/state | handle_tick | CORE: новый инстанс = новый эпизод L2 |
| не съеден > consume_timeout → эскалация в процесс (гейт observe) | handle_tick | **ДЫРА №1 → КЛЮЧ processes.*.watch.P2_request_stuck** (порог = actions.recreate.eat_within_ms) |
| soft-fail: N подряд без pulse_back → процесс (3 гейта) | handle_tick | УПРАЗДНЕНО → ladder[].tries + наличие/отсутствие ступени @restart_* (+observer.mode) |
| датаграмма: parse name/state/counter | handle_datagram | ПРОТОКОЛ text_v1 (PROTOCOL.md) |
| смена state → лог (дедуп по значению) + закрытие эпизода | handle_datagram | CORE + ПРОТОКОЛ (idle закрывает эпизод) |
| counter изменился → last_progress=now | handle_datagram | CORE |
| unknown_name rate-limit | handle_datagram | КЛЮЧ observer.quiet.unknown_ms (0 = каждый — вырожденное значение той же семантики, не режим) |
| буфер датаграммы 128Б (формула 47+8+20=75) | handle_datagram | COMPILE-TIME (обоснование в PROTOCOL.md) |
| NAME 48 / STATE 24 / PATH 256 / MAX_SERVICES 30 | defines | COMPILE-TIME (валидатор длин при парсе) |
| **новые ёмкости v2 (итер.8): MAX_PROCESSES / MAX_ACTIONS / MAX_LADDERS / MAX_LADDER_STEPS / MAX_WATCH_LEVELS** | — | COMPILE-TIME: вложенная схема даёт новые коллекции; превышение = отказ, не усечение (валидатор правило 17). v1.2 каппил только MAX_SERVICES — урок был применён наполовину |
| SIGPIPE ignore; oom_score_adj самозащита | main() | CORE; КЛЮЧ observer.oom_adj |
| unlink+bind+chmod 0666 сокета | main() | АДАПТЕР unix_datagram; 0666 = ПРОТОКОЛ (не-root отправители) |
| epoll(sock+timerfd), один поток, ожидание = состояние | main() | CORE (реализация наблюдателя) |
| wd.log: wall-время + up=монотонные сек, ротация .1..N, fsync | wd_log()/rotate | КЛЮЧИ observer.log.*; формат строки = ПРОТОКОЛ лога |
| wd_start строка (services/socket/mode/grace) | main() | CORE (диагностика старта) |

## 2. external-storage-contents/wd_beat.py (отправитель)

| Поведение | Где | Куда в v2 |
|---|---|---|
| CONFIG_PATH: env WD_CONFIG → /etc/svc_watchdog.conf | модуль | BOOTSTRAP |
| config() строгий (без дефолтов, RuntimeError) | config() | Э1 config.py (единый строгий лоадер) |
| socket_path/interval_s/crash_file_template аксессоры | — | УПРАЗДНЕНО → config.py отдаёт валидированную модель |
| beat(name,state,counter): собрать датаграмму, sendto | beat() | АДАПТЕР transport unix_datagram (emit) |
| контракт never-raises на горячем пути | beat() | CORE контракт transport.emit (зафиксирован в contracts.py) |
| пересоздание сокета после ошибки | beat() | АДАПТЕР |
| мут тест-хук /tmp/wd_test_mute_<name> | beat() | ПРОТОКОЛ тест-хуков (PROTOCOL.md) |

## 3. external-storage-contents/wd_runtime.py (библиотека v1.2 → растворяется в svc_watch)

| Поведение | Где | Куда в v2 |
|---|---|---|
| load_config: все missing разом, WdConfigError | load_config() | Э1 config.py (новая схема) |
| resolve_entry ("module:function"; run_services/__main__ из globals) | resolve_entry() | АДАПТЕР start python_factory (спец-случай главного модуля сохранить) |
| setup_process_logging: primary + fallback-пути + ротация | setup_process_logging() | КЛЮЧИ supervisor.log{file,rotate_kb,keep} + **ДЫРА №2 → supervisor.log.fallbacks[]** |
| silence_propagate/warning_only адаптеры логгеров | — | СЕРВИС-СЛОЙ (аргументы composition root потребителя A) |
| beat_loop: probe-гейт → beat, период | WdRuntime.beat_loop | АДАПТЕР pulse from:"probe" (runtime собирает из signals.pulse) |
| tcp_probe замыкание (connect+close) | WdRuntime.tcp_probe | АДАПТЕР probe type:"tcp" |
| bounded_teardown (потолок, не raise) | _bounded_teardown | КЛЮЧ supervisor.stop_timeout_ms; контракт = CORE |
| supervise: factory → монитор crash-файла → teardown → backoff | supervise() | АДАПТЕР супервизора; КЛЮЧИ supervisor.poll_ms / supervisor.backoff |
| «файл существует → RuntimeError injected crash» (канал рестарта) | supervise() | АДАПТЕР request_file (сторона потребителя) |
| supervise_all: битый entry валит ТОЛЬКО этот сервис | supervise_all() | CORE runtime (изоляция сборки) |
| test_hang_watcher (/tmp/wd_test_hang, sleep 90) | test_hang_watcher() | ПРОТОКОЛ тест-хуков |

## 4. external-storage-contents/run_services.py (процесс unified — потребитель A)

| Поведение | Где | Куда в v2 |
|---|---|---|
| bootstrap sys.path (site-packages кандидаты) | _bootstrap_python_paths | BOOTSTRAP потребителя A |
| 4 фабрики сервисов (+атомарность фабрик при ошибке bind) | start_* | СЕРВИС-СЛОЙ: examples/unified_rut (фабрики остаются кодом сервисов) |
| порт config_server из конфига (bind+проба одним ключом) | start_config_server | КЛЮЧ services.config_server.port (@port в probe) |
| _teardown_service: разбор resource по имени | _teardown_service | СЕРВИС-СЛОЙ: teardown-адаптер потребителя A (замыкание в runtime) |
| main: fail-closed FATAL+exit(1) на битом конфиге | main() | CORE политика composition root |
| io_executor max_workers=2 (gzip offload) | main() | СЕРВИС-СЛОЙ (не вотчдог) |
| SIGINT/SIGTERM → teardown всех | main() | CORE runtime потребителя |
| ws_bridge порты 81/8765 из модуля run_ws_udp_bridge | start_ws_bridge | КЛЮЧ services.ws_bridge.port (проба); bind из модуля — унифицировать в Э5 (отмечено) |

## 5. flash.py `_WdActivity` + клиенты `_txrx` (сигнал activity)

| Поведение | Где | Куда в v2 |
|---|---|---|
| тик-счётчик общий, троттлинг по времени из конфига | _WdActivity.tick | КЛЮЧ services.config_server.signals.activity.tick_ms |
| beat("config_server","active",N) / "idle" в finally джоб | job_started/finished | ПРОТОКОЛ text_v1 (active/idle, эпизод) + АДАПТЕР activity-emitter |
| дев-ПК без конфига → activity выключен, лог | __init__ | CORE: вне вотчдог-среды сигнал не излучается |
| activity-колбэк в клиенты аргументом (клиенты конфиг НЕ читают) | вызовы run_flash | CORE границы: рабочий код получает tick параметром |
| нарезка recv min(rem, tick) + continue; ретрансмит только по дедлайну | _txrx | СЕРВИС-СЛОЙ (флеш-протокол); tick приходит из ключа activity.tick_ms |
| standalone-фолбэк 2.0с в клиентах | конструкторы | СЕРВИС-СЛОЙ (CLI вне роутера, конфига нет физически) |
| таймауты фаз START/DATA/END, retries, BLOCK_SIZE, порты 13600/8888 | клиенты/flash.py | СЕРВИС-СЛОЙ (флеш-протокол — вотчдога не касается) |

## 6. launcher + init.d (запуск)

| Поведение | Где | Куда в v2 |
|---|---|---|
| PATH/LD_LIBRARY_PATH, путь скрипта | launcher | BOOTSTRAP |
| ожидание SD (/dev/sda1) перед стартом | launcher | BOOTSTRAP (лог-каталоги живут на SD) |
| mkdir каталогов логов | launcher | CORE: каталог = dirname(log.file) из конфига (jsonfilter), не литерал (чинится в Э5) |
| pidfile: путь из конфига jsonfilter, пусто = exit 1 | launcher | КЛЮЧ launch.pidfile (уже соблюдено в v1.2) |
| exec python (pid сохраняется) | launcher | BOOTSTRAP (контракт pidfile-возраста) |
| procd respawn 3600/5/5 (unified) и 3600/5/0 (wd), stderr=1 | init.d оба | BOOTSTRAP (внешний слой перерождения; в конфиг не тянем — чужой формат procd) |
| START=98/99, STOP=10/11 (wd переживает стек) | init.d | BOOTSTRAP |

## 7. Ключи конфига v1.2 → v2 (полное покрытие CONFIG_REFERENCE)

| v1.2 | v2 |
|---|---|
| socket | framework.transport.socket |
| beat_interval_ms | services.*.signals.pulse.every_ms (per-service) |
| tick_ms | framework.observer.tick_ms |
| pause_file | framework.observer.pause_file |
| self_oom_adj | framework.observer.oom_adj |
| paused_log_every_ms / unknown_name_log_every_ms | framework.observer.quiet.{paused_ms,unknown_ms} |
| log{file,max_bytes,keep,fsync} | framework.observer.log{file,rotate_kb,keep,fsync} |
| python.teardown_timeout_ms | processes.*.supervisor.stop_timeout_ms |
| python.probe_timeout_ms → (v1.2 upd: per-service) | signals.pulse.probe.timeout_ms |
| python.http_shutdown_timeout_ms | СЕРВИС-СЛОЙ (константа фабрики config_server — уходит в код потребителя A; вотчдога не касается) |
| python.crash_poll_ms | processes.*.supervisor.poll_ms |
| python.activity_tick_ms | services.config_server.signals.activity.tick_ms |
| python.config_server_port | services.config_server.port |
| python.supervise_backoff | processes.*.supervisor.backoff{start_ms,factor,cap_ms} |
| python.log | processes.*.supervisor.log (+fallbacks[]) |
| resources{...} | framework.gates{min_free_mb,max_load1,recheck_ms,force_after_ms,alarm_mb} |
| restart_delay{mode,...} | framework.pacing{type,delay_ms}\|{type,backoff-params} — ТИПИЗИРОВАН (итер.8: было mode+оба блока = мёртвый блок, ревью поймало) |
| process.name | ключ карты processes (имя слева) |
| process.initd | processes.*.launch.{type:init_script, script}; глагол `restart_process` целит в процесс-владелец (итер.8: было отдельное действие restart_unified с @process — убрано, кросс-процессный промах) |
| process.pidfile | processes.*.launch.pidfile |
| process.crash_file_template | actions.recreate.file |
| process.consume_timeout_ms | actions.recreate.eat_within_ms (порог P2_request_stuck) |
| process.service_relaunch_ms | actions.recreate.startup_ms |
| process.start_grace_ms | processes.*.launch.grace_ms |
| process.soft_fail_escalation{enabled,after_attempts} | УПРАЗДНЕНО → ladder[].tries + состав лестницы |
| process.action | УПРАЗДНЕНО → observer.mode (главный) + watch.*.mode (точечный) |
| services[].name/entry | ключ карты services / start.python_factory.entry |
| services[].wait_pulse_timeout_ms | watch.L1_pulse_lost.dead_after_ms |
| services[].action | watch.*.mode (act/log) |
| services[].escalate_to_process | УПРАЗДНЕНО → наличие ступени @restart_* в лестнице |
| services[].progress_stall_ms (0=выкл) | signals.activity (наличие) + watch.L2_activity_frozen.frozen_after_ms |

## Семантические расхождения v1.2 → v2 (осознанные, на утверждение)

1. **Consume-эскалация** стала процессным уровнем `P2_request_stuck` (было: ветка эскалации внутри
   per-service; поведение то же — рестарт процесса при мёртвом супервизоре, — но теперь это явный
   уровень с СВОЕЙ лестницей и mode).
2. **beat_interval_ms** был глобальным — стал per-service (`every_ms`); боевые значения одинаковы (5000).
3. **probe_timeout_ms** был глобальным — стал per-probe (2000 у обоих).
4. **http_shutdown_timeout_ms** уходит из вотчдог-конфига в код потребителя A (это настройка
   aiohttp-фабрики, не наблюдения; вотчдог-конфиг перестаёт быть свалкой чужих калибровок).
5. **Тройной гейт эскалации** → состав лестницы + observer.mode (поведенчески эквивалентно
   боевым значениям v1.2: у всех esc=true сервисов ступень есть, у udp_logger нет).
