# watchdog-v1 — /goal харнес (реализация WATCHDOG_MINIMAL)

> Спека (согласована 2026-07-24): `artifacts/svc-consolidation/WATCHDOG_MINIMAL.md` — ЧИТАТЬ ПЕРВОЙ.
> Разведка на RUT сделана (см. спеку §7 и RAM_HEADROOM_RESEARCH.md): procd/init.d ✓, libjson-c в RAM ✓ (28 процессов), oom_score_adj пишется ✓, MemAvailable ✓. RUT 192.168.1.1, ключ /tmp/rut_key, $RD=/usr/local/home/root/external-storage-contents, live arm R4=192.168.1.247.

## ОГРАНИЧЕНИЯ (наследуются всеми голами)
- **Коммитов НЕ делать.** Эмуляция устройств ЗАПРЕЩЕНА (реальный стенд; artifacts/_EMULATION_DO_NOT_USE).
- На RUT не спавнить лишний python; нагрузочные скрипты — с лаптопа (`load/bench_load.py`).
- RAM-монитор держать запущенным во время стендовых фаз; [CRITICAL-RAM]/[DOWN] → стоп + откат.
- Стенд не оставлять сломанным между голами: каждый гол заканчивается «сервисы здоровы» (health-чек).
- Физический шаг (ребут питанием, DFU) неизбежен ИЛИ первый флеш под новым watchdog → остановиться и позвать пользователя.
- Откат всегда доступен: `/etc/init.d/svc_watchdog stop; disable` + rc.local → 4-separate wrappers.

---

## G1 — Python-сторона (пульсы + init.d + lazy import)
**Делает:**
1. `$RD/wd_beat.py` (~20 строк): `beat(name, state=None)` → `sendto` в сокет **из конфига** (AF_UNIX, SOCK_DGRAM), все ошибки молча. Потокобезопасен (голый syscall). Конфиг `/etc/svc_watchdog.conf` кладётся В ЭТОМ голе (он нужен Python-стороне раньше, чем самому C-демону).
2. Вшить базовый пульс (период `beat_interval_ms` из конфига) в **собственный цикл каждого** из 4 сервисов: `data_uploader` — итерация его `_watchdog`; `udp_logger` — его периодический цикл (рядом с monitor_config_changes); `config_server`/`ws_bridge` — фоновая корутина внутри их supervise-фабрик (умирает вместе с task'ом). НЕ одной общей корутиной, НЕ независимой задачей.
3. `active`-пульсы: из flash progress-колбэков (`flash.py`), напрямую из executor-потока: `beat("config_server", "active")`.
4. Lazy import флеш-машинерии: `_client_module()` → `importlib.import_module` при первом запросе; на старте flash-модули НЕ импортируются; лог-строка «flash tools loaded» при первой загрузке.
5. `/etc/init.d/rut_services`: procd-скрипт для «Единого» (`procd_set_param command/respawn/term_timeout`), enable; rc.local → на него (вместо 4 wrappers). **Это переключение стенда на unified — постоянно** (решение A из RAM_HEADROOM_RESEARCH, подтверждено).
6. Лог run_services → `/mnt/sda1/crash_logs/unified.log` с ротацией (RotatingFileHandler 1МБ×7) вместо безротационного services_runtime.log; одна папка с будущим wd.log.
**Verify (лаптоп→RUT):**
- на RUT ровно 1 python (`run_services.py`), `/health` 200, ws :81 отвечает;
- слушатель на `/var/run/svc_wd.sock` (однострочник python на ЛАПТОПЕ нельзя — сокет локальный; допускается временный `socat`/mini-listener на RUT ЧЕРЕЗ ssh, убить после) видит 4 разных имени за ≤15с;
- `touch /tmp/svc_crash_udp_logger` → в логе `[supervisor] udp_logger up (start #2)`, остальные не перезапущены, python по-прежнему 1;
- `sys.modules` без odrive_fw/vesc_fw модулей до флеша (проверка через лог «flash tools loaded» отсутствует на старте);
- reboot-тест rc.local НЕ делаем (ребут роутера — только с пользователем); вместо него: `sh /etc/init.d/rut_services restart` поднимает unified.
- **единый источник имён:** run_services.py читает `/etc/svc_watchdog.conf` (тот же, что C-демон): `services[]` → что поднимать и ОТКУДА (`entry`="модуль:функция" через importlib — словаря в коде НЕТ; entry не импортируется → громкий отказ ЭТОГО сервиса, соседи живут), `beat_interval_ms`/`socket` → beat, `crash_file_template` → supervise; валидатор: `wait_pulse_timeout_ms ≥ 3×beat_interval_ms`; конфиг битый/отсутствует → поднять ВСЁ по встроенному дефолту + ошибка в лог. verify: подъём с конфигом, подъём без конфига (fail-open), битый entry → сервис не поднялся + лог, остальные работают.
**DoD:** `bash artifacts/watchdog-v1/verify_g1.sh` → «G1 OK» exit 0.

## G2 — C-демон в Docker (стенд НЕ трогаем)
**Делает:**
1. Docker-контейнер с OpenWrt SDK 21.02.0 **ramips/mt76x8** (mipsel_24kc, musl, **soft-float** — на устройстве /lib/ld-musl-mipsel-sf.so.1!). qemu-user (qemu-mipsel) в том же контейнере.
2. `artifacts/watchdog-v1/src/svc_watchdog.c` (~400 строк) + Makefile: epoll (heartbeat-сокет + timerfd 1с), таблица last_seen, конфиг через **libjson-c** (SDK staging), `pause_file`, resources-wait (meminfo/loadavg), `restart_delay` mode=fixed|backoff, `action` per-service/process (restart|log), crash-файл + `consume_timeout_ms`-эскалация, лог на SD + ротация max_bytes×keep + fsync, `self_oom_adj`, `start_grace_ms`, слово состояния в лог.
3. Юнит-тесты логики на ХОСТЕ (нативная сборка с libjson-c-dev; фейковые /proc и часы через #ifdef TEST hooks): таймаут пульса, no_pulse_all, log vs restart, оба режима delay, pause_file, эскалация, ротация.
4. Прогон mipsel-бинаря в qemu с тест-конфигом и фейк-пульсами.
**DoD:** `bash artifacts/watchdog-v1/verify_g2.sh` → «G2 OK»: хост-тесты зелёные; mipsel-бинарь собран, <150КБ, в qemu отрабатывает сценарий «пульс→тишина→crash-файл→эскалация» по логу.

## G3 — стендовая верификация (живой RUT)
**Делает:** деплой бинаря на SD + `/etc/svc_watchdog.conf` (JSON из спеки, ВСЕ action=log) + `/etc/init.d/svc_watchdog` (procd respawn, enable).
**Матрица (каждая строка → строка в wd.log на SD, читаемая по ssh):**
| # | Инъекция | Ожидание |
|---|---|---|
| 0 | observe-прогон: 10 мин под `bench_load.py` (боевая нагрузка с лаптопа) | НОЛЬ ложных `no_pulse` в wd.log → переключить action→restart (config_server оставить наблюдать отдельно при флеш-тесте №6) |
| 1 | стоп пульса одного сервиса (тест-хук: `touch /tmp/wd_test_mute_udp_logger`, beat-цикл его уважает) | `no_pulse` → crash-файл → supervise `start #N` → `pulse_back`; соседи нетронуты |
| 2 | вис loop: хук впрыскивает `time.sleep(90)` корутиной | `no_pulse_all` → `initd restart` → grace → все пульсы вернулись |
| 3 | `kill -9` unified | procd respawn; wd фиксирует тишину/восстановление без двойного рестарта (гонки нет: grace) |
| 4 | `touch /tmp/wd_pause` + инъекция №1 | wd пишет `paused`, НЕ действует; `rm` → действует |
| 5 | resources-wait БЕЗ реального голодания: временно `min_free_mb=25` (норма ~20 → «нездорово») + инъекция №1 | `waiting_resources` в логе; после возврата порога → рестарт |
| 6 | **реальный флеш R4 под watchdog в enforce** (первый флеш — ПОЗВАТЬ ПОЛЬЗОВАТЕЛЯ по конституции) | за весь флеш НОЛЬ рестартов от wd; `active`-пульсы видны в wd.log; флеш summary=ok; **lazy-путь**: до этого момента в unified.log НЕТ «flash tools loaded», строка появляется ровно при старте этого флеша (первая загрузка начинки), флеш при этом успешен |
| 7 | смерть самого watchdog: `kill -9` его | procd respawn wd; после подъёма state-таблица строится заново без ложных рестартов (grace) |
**DoD:** `bash artifacts/watchdog-v1/verify_all.sh` → «watchdog-v1: DONE» exit 0 (агрегат g1+g2+матрица по wd.log) + стенд здоров + запись в память.

## ПРОДОЛЖЕНИЕ ПОСЛЕ НАХОДОК G3 (согласовано 2026-07-24: делаем И «А», И «Б»; «Б» = митигация, включаемая конфигом)

> Статус на паузе: G1 ✅, G2 ✅, G3 частично (шаг 0 и 7 ✓, механика C доказана; findings: `G3_FINDINGS.md`).
> Стенд здоров, unified под procd, watchdog ОСТАНОВЛЕН (`/etc/init.d/svc_watchdog start` вернёт).

### G4 — «А»: контракт teardown для всех 4 сервисов (Python)
По §5a спеки: `GPSSystemOrchestrator.stop()` (гасит gps_server:1111 / mqtt / time_sync / realtime-listener / storage / http_forwarder; ВСЕ задачи оркестратора — в список с сильными ссылками, cancel в stop); аудит config_server (runner.cleanup достаточен? executor flash?) и ws_bridge; udp_logger уже чист.
**DoD (verify_g4.sh):** локально py_compile + НА СТЕНДЕ soak: для КАЖДОГО из 4 сервисов подряд 5 циклов `mute → service_restart → rm mute → pulse_back` без единого `address in use` в unified.log и без роста числа time_sync-циклов (grep 'NTP sync loop started' не растёт между циклами); стенд здоров.

### G5 — «Б»: soft_fail_escalation в C-демоне
Конфиг `process.soft_fail_escalation = {enabled, after_attempts}` (уже в спеке §3): `after_attempts` ПОДРЯД service_restart одного сервиса без pulse_back → `enabled=true` ? лог `soft_restart_failed name=S` + process_restart : только лог. Хост-тесты: оба положения глобального флага + per-service `escalate_to_process` (true→process_restart; false→только лог soft_restart_failed, процесс не тронут); сброс счётчика по pulse_back. + Довесок из findings №6: `up=<monotonic_s>` в каждую строку wd.log (wall-clock прыгает от time_sync).
**DoD:** verify_g2.sh расширен этими тестами → «G2 OK» (пересборка в том же docker-томе wdsdk).

### G6 — матрица v3 + финал
Матрица v2 → v3: тайминги с запасом (после каждого process_restart ждать health+grace+30с, бюджеты ожиданий ×2); шаг 1 прогнать для всех 4 сервисов (это же soak G4); шаг «Б»: на время теста enabled=true + сломанный сервис-симуляция (mute без rm) → после after_attempts попыток process_restart; шаги 2,3,4,5,7 как в v2 (офсетные грепы уже есть). Затем **шаг 6 — реальный флеш под watchdog — ПОЗВАТЬ ПОЛЬЗОВАТЕЛЯ**. Финал: `verify_all.sh` → «watchdog-v1: DONE» + память.

## Роллбек (в любом голе)
`ssh: /etc/init.d/svc_watchdog stop; /etc/init.d/svc_watchdog disable` — python-стек работает без wd (пульсы шлются в никуда, это штатно). Полный откат стенда: rc.local → 4 wrappers (команды в memory `svc-consolidation-done`).

## v1.1 — В РАБОТЕ (гол запущен 2026-07-25): G7 → G8 → G9

### Семантика (ФИНАЛЬНАЯ, уточнение пользователя 2026-07-25 — заменяет старую «counter=progress-событие»)
1. **Фикс дыры observe:** consume-timeout эскалация (crash-файл не съеден) обязана гейтиться `process.action=log` (сейчас рестартит процесс даже в observe) — 1 строка C + хост-тест.
2. **Progress-stall детект «L2»: счётчик = LIVENESS-ТИК протокольного ЦИКЛА, а НЕ progress-событие.**
   - Тик = каждая итерация низкоуровневого цикла флеш-клиента (send/recv/timeout-retry). Троттлинг ПО ВРЕМЕНИ: датаграмма со счётчиком не чаще 1 раза в 2с (тиков может быть тысячи/с — шлём срез).
   - Смысл: тиков нет = ПОТОК МЁРТВ (deadlock/вечная блокировка/замёрзший поток). Случай «поток жив, но устройство молчит» — НЕ дело вотчдога: его ловят собственные таймауты клиента (live_verify 600с и т.п.), клиент сам упадёт, finally пошлёт idle.
   - Следствие: немых окон нет (тики идут и сквозь erase — клиент крутится в ожидании ответа) → порог ужимается 180с → ~60с.
   - Пульс: `"<имя> <state> <counter>"` (3-е поле, тот же датаграм-канал; base-пульсы без слова эпизод НЕ трогают).
   - Эпизод: начинается первым counter-пульсом (state=active), заканчивается ЯВНЫМ пульсом `idle` (flash.py в finally всех джоб-веток). Клиент сдох без idle → счётчик замер при active → stalled → рестарт = ЖЕЛАЕМОЕ.
   - Правило C: state==active И counter не менялся дольше `progress_stall_ms(S)` → лог `stalled name=S counter=N stalled_s=...` → обычная цепочка действий сервиса (action/delay/ресурсы/митигация Б). Сброс эпизода: смена state, рестарты (service/process). C-демону ВСЁ РАВНО, что значит счётчик — только «менялся или нет».
   - Конфиг: `services[].progress_stall_ms`, 0=ВЫКЛючено (дефолт — и это же предохранитель: разработчик не нашёл куда воткнуть тик → сервис просто не включает L2); config_server=60000 (тики ≤2с → запас 30×; пересчитать, если аудит оставит неизменённое длинное ожидание); остальные 0.
   - Python: wd_beat.beat(name, state=None, counter=None) — счётчик только вместе со state; клиенты (odrive_fw/vesc_fw/odrive_config) wd_beat НЕ импортируют (живут и вне RUT) — опциональный activity-callback из flash.py по образцу progress-callback.
   - **ПРАВИЛО аудита ожиданий:** менять блокирующее ожидание (длинный recv → цикл коротких) ТОЛЬКО через стоп-и-позвать-пользователя (точка в коде / старое-новое поведение / есть ли ретрансмит — ретрансмит = смена протокола!). Не одобрено/нельзя → тик вокруг ожидания, порог пересчитать под самое длинное неизменённое окно.

### G7 — C-демон (Docker, стенд не трогать)
observe-фикс (а) + L2 (б) по семантике выше. Хост-тесты: stalled при active+замёрзшем счётчике; НЕ при progress_stall_ms=0; НЕ при растущем счётчике; idle закрывает эпизод; observe-гейт эскалации. **DoD:** verify_g2.sh «G2 OK», бинарь <150КБ.

### G8 — Python + конфиг + деплой
(а) АУДИТ точек ожидания флеш-пути — все блокирующие >5с, список сюда (раздел «Аудит ожиданий» ниже); (б) activity-callback → счётчик++ и beat("config_server","active",N) троттлинг 2с; idle в finally; (в) wd_beat 3-й аргумент; (г) конфиг progress_stall_ms (config_server=60000, остальные 0) + CONFIG_REFERENCE.md. Деплой на RUT, restart обоих, smoke: mute-цикл udp_logger + health 200, ноль ложных stalled за 5 мин.

### G9 — стенд
(1) временно progress_stall_ms=15000 у config_server → ssh-сендер шлёт `config_server active 7` раз в 5с с замороженным счётчиком → stalled → crash-файл → супервизор пересоздал → health; (2) растущий счётчик 20+ → НОЛЬ stalled; (3) idle → эпизод закрыт; (4) вернуть боевой порог, health 200 + 1 python; (5) регрессия — живой Bundle-флеш R4 под L2 (СТОП → позвать пользователя): summary=ok, НОЛЬ stalled, ноль рестартов, тики сквозь erase, idle закрыл эпизод. Финал: стенд здоров, память+GOALS обновить.

**✅ v1.1 DONE 2026-07-25 (все голы, живой стенд).** G7 ✅ (verify_g2 «G2 OK», бинарь 19900Б, тесты T15–T19). G8 ✅ (пере-взвод recv ОДОБРЕН пользователем; деплой; smoke чист, 5 мин — ноль строк). G9 ✅ все 5 шагов: (1) stalled за ровно 15с → restart(stalled) → consume → start #2 → health 200; (2) растущий 22× → 0 stalled; (3) idle → 0 stalled; (4) боевой порог 60000 возвращён; (5) живой Bundle-флеш R4: summary=ok 1/1, за окно 5м21с в wd.log ровно 2 строки `state=active`→`state=idle`, 0 stalled/0 рестартов = тики сквозь erase (эпизод 321с, порог 60с). Стенд здоров. Примечание: прекчек live_verify.sh ищет `run_config_server` — устарел (unified), шаги upload→run→stream гнать напрямую. Откат бинаря: $RD/svc_watchdog.bak_v1 (18988Б, v1).

### Аудит ожиданий флеш-пути (G8, 2026-07-25)
Все клиенты `_txrx` одной формы: внешний retry-цикл (ретрансмит), внутри — ОДИН блокирующий `recvfrom` до полного дедлайна фазы (`settimeout(rem)`). Пока ESP молчит (erase!), поток спит в одном syscall — итераций нет, тикать нечем без правки.

| Точка | Худшее одиночное ожидание | Вердикт |
|---|---|---|
| `odrive_fw/flash_job_client.py:_txrx` (recvfrom) | START/END: 15с × ts(=8) = **120с** (erase/verify); DATA 40с; retries 8 | НУЖЕН пере-взвод recv (см. ниже) — спросить пользователя |
| `vesc_fw/vesc_fw_client.py:_txrx` (recvfrom) | идентично: START/END **120с**, DATA 40с | НУЖЕН пере-взвод recv — спросить пользователя |
| `vesc_config/vesc_job_client.py:_txrx` | 2с/2с/5с — всё ≤5с | ниже порога, НЕ трогаем |
| `odrive_config/udp_can_bus.py:_recv_datagram_until` | `settimeout(min(rem, 0.05))` — уже 50мс-цикл; внешние дедлайны ≤5с | уже цикл, НЕ трогаем |
| flash.py async-сторона: `_bridge_enter` (≤55с, итерации 0.5–1с), `_verify_git_after` (≤35с, полл 1–3с) | циклы уже короткие | тик из итераций цикла, протокол не трогается |

**Предлагаемая правка (безопасный класс, ТОЛЬКО 2 файла odrive_fw/vesc_fw):** `settimeout(rem)` → `settimeout(min(rem, 2.0))`, на `socket.timeout` → `continue` (пере-взвод) вместо `break`; выход на ретрансмит — только по исчерпанию полного дедлайна (проверка `rem <= 0` в голове цикла, как и была). Ретрансмита НЕ добавляется, суммарный таймаут НЕ меняется. + опциональный `activity`-колбэк, зовётся на каждой итерации. Fallback при отказе: клиентов не трогаем, тики только вокруг ожиданий → порог config_server пересчитать на >120с×запас (≈300000).

## v1.2 — строгий конфиг (fail-closed) + вынос констант + библиотека wd_runtime (гол 2026-07-25)

### Заказ пользователя (дословно по смыслу)
1. Известные нарушения мета-правила — фиксить и в конфиг: троттлинг 2.0с (`_WdActivity.tick`), нарезка recv 2.0с (2 клиента), порт 8000 ×3 (run_services.py), pidfile-литерал в launcher при живом `process.pidfile`.
2. **Fail-open — «дерьмо полное»: ВСЁ только из конфига.** Битый/неполный конфиг = громкий отказ, никаких BUILTIN_SERVICES и дефолтов в коде. Compile-time ёмкости (MAX_SERVICES=30) — ок, согласовано.
3. Весь вотчдожий код из run_services.py — в БИБЛИОТЕКУ (адаптеры/замыкания), скрипт логики держать чистым.
4. Объяснить длины NAME/STATE/PATH_MAX_LEN, приёмный буфер, расположение конфига (если не флешка — плохо).

### G10 — Python: библиотека + строгость ✅
- **`wd_runtime.py` (новый)**: WdConfigError; `load_config()` FAIL-CLOSED — валидирует ВСЕ python-ключи, перечисляет все пропуски разом; `resolve_entry`; `setup_process_logging` (silence/warning-адаптеры); класс `WdRuntime` — beat_loop/beat_task, tcp_probe-замыкание, bounded_teardown, supervise, supervise_all, test_hang_watcher. run_services.py = фабрики 4 сервисов + `_teardown_service` (сервис-специфика, передаётся адаптером) + тонкий main. BUILTIN_SERVICES и `_pycfg` удалены.
- **wd_beat.py строгий**: дефолты удалены; `config()` кидает RuntimeError; `beat()` — единственное never-raise исключение (горячий путь; без конфига процесс всё равно не стартует).
- **Новые ключи**: `python.activity_tick_ms=2000` (троттлинг `_WdActivity` И нарезка recv — клиентам приходит аргументом `activity_tick_s`, конфиг они не читают; протокол не менялся — только литерал 2.0 → параметр), `python.config_server_port=8000` (bind + самопроба + лог). udp_logger/data_uploader: `except (ImportError, RuntimeError, KeyError)` — вне роутера пульсы просто выключены.
- **launcher**: `PIDFILE=$(jsonfilter -i $CONF -e '@.process.pidfile')`, пусто = exit 1.
- DoD: py_compile ✅; локальный тест лоадера ✅ (валидный → реестр 1:1 с services[]; битый JSON/нет файла/4 пропущенных ключа разом/пустой services[] → WdConfigError).

### G11 — C fail-closed + гигиена буферов ✅
- Все `jstr/jnum/jbool(dflt)` → `req_str/req_num/req_bool/req_strcpy`: КАЖДЫЙ ключ обязателен, `stderr "missing key <путь>"` (все разом) + exit 1; `req_strcpy` — длиннее буфера = ошибка, НЕ усечение; `parse_action` — только restart|log; mode — только fixed|backoff; `soft_fail_escalation` обязательна; >30 сервисов/пустой массив/`after_attempts<1`/`stall_ms<0`/переполнение crash_path = ошибка.
- Приёмный буфер 80→128 с формулой в комментарии (имя47 + " active "8 + счётчик20 = 75Б).
- Тесты: mkconf теперь полный (fail-closed), **T20** (7 ключей из всех секций по одному → отказ с "missing key"; 3 пропуска → 3 строки разом), **T21** (имя 60 символов → "too long"). T1–T19 зелёные.
- DoD: verify_g2.sh «G2 OK» ✅, бинарь 22924Б (<150КБ), qemu smoke OK.

### G12 — стенд ✅
- **Расположение конфига подтверждено фактом**: `/dev/sda1 on /overlay type ext4` + `overlayfs:/overlay on /` → /etc = overlay upperdir на SD (118ГБ ext4). Конфиг физически на флешке. Записано в CONFIG_REFERENCE.
- Деплой: бинарь 22924Б + конфиг (2114Б, +2 ключа) + run_services/wd_runtime/wd_beat/flash.py/udp_logger/data_uploader/launcher/2 клиента. Бэкапы: `$RD/svc_watchdog.bak_v1.1` (19900Б), `$RD/svc_watchdog.conf.bak_v1.1`, `$RD/unified-procd-launcher.sh.bak_v1.1`.
- Sanity на боксе: новый бинарь на боевом конфиге живёт; на конфиге без tick_ms → `missing key tick_ms` rc=1.
- **Негатив-тест живьём**: конфиг без activity_tick_ms/tick_ms → `FATAL run_services: missing key(s): python.activity_tick_ms` в logread, 0 python-процессов; wd → `missing key tick_ms` в logread. Откат конфига → health 200, все 4 up.
- Smoke: health 200, :81 LISTEN, 1 python, pidfile=PID python (launcher из конфига), mute-цикл udp_logger: no_pulse(15с) → service_restart → start #2 → pulse_back за 3с; **5 минут — НОЛЬ строк в wd.log**; RAM 29МБ.

**✅ v1.2 DONE 2026-07-25.** Откат: `/etc/init.d/svc_watchdog stop` + `$RD/svc_watchdog.bak_v1.1` + `.conf.bak_v1.1` + launcher-бэкап + git-версии python.
