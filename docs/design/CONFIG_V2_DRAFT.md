# CONFIG_V2_DRAFT — итерация 8 (после адверсариального ревью дизайна)

> Правила чтения (те же, что в итерации 3):
> 1. **Имя сущности = ключ слева** (`processes`, `services`, `actions`, `signals` — карты).
> 2. **`@` = связь** на сущность из другого места; каждая проверяется валидатором.
> 3. **Смысл несёт путь**: ключи короткие, контекст даёт вложенность.
>
> Новое в итерации 4 — закрыты две структурные дыры:
> 4. **Сервис = `signals` + `watch`, связанные именем сигнала.**
>    `signals` — что сервис ИЗЛУЧАЕТ и как (python-сторона, эмиссия).
>    `watch` — как наблюдатель СУДИТ каждый сигнал (C-сторона, суждение).
>    Ключ уровня называет свой сигнал: `signals.pulse` → `watch.L1_pulse_lost`,
>    `signals.activity` → `watch.L2_activity_frozen`. Видно без вчитывания, кто что судит.
> 5. **Заполнителей нет.** Ни `type: "none"`, ни `mode: "off"` с мёртвыми параметрами:
>    - пульс из своего цикла (`from: "loop"`) — доказательство по построению, поля probe
>      у него НЕ СУЩЕСТВУЕТ; пульс от библиотеки (`from: "probe"`) без probe невыразим;
>    - сервис без рабочего потока просто НЕ ИМЕЕТ сигнала `activity` и уровня L2 —
>      отсутствие блока = «не инструментирован» (валидатор требует парности);
>    - `mode: "log"` остаётся для обкатки (сигнал есть, судим, но только пишем).
>
> Семантика лестницы (`ladder`, одна на все уровни): ступень `{ "do": "@действие", "tries": N }` —
> до N исполнений (паузы `pacing`, гейт `gates`); не вылечило → следующая ступень. Последняя —
> без `tries` (повторяется до выздоровления). Выздоровление → ступень 0. Право «дёрнуть
> процесс» = наличие такой ступени в лестнице уровня. Флагов-гейтов нет.
> «Файл-запрос не съеден» — НЕ провал ступени сервиса, а сигнал о ПРОЦЕССЕ (супервизор мёртв):
> его судит процессный уровень `P2_request_stuck` (итерация 5, найдено обходом v1.2).
>
> Итерация 6: **лестницы — переиспользуемые политики в каталоге `ladders`**. Значение `ladder`:
> строка `"@имя"` = выбор из каталога (обычный путь) | массив ступеней = одноразовая инлайн-политика.
> Смена политики = правка одного места; «выбирать из них» — буквально.
>
> Итерация 7 (ресёрч Р1, RESEARCH_NOTES + FRAMEWORK_RULES) — три находки из аналогов:
> - **`actions.*.rate_limit`** (OTP intensity, supervisord startretries): терминальное действие
>   не долбит вечно — N исполнений по одной цели за T → cooldown + аларм. Закрывает FR-30.
> - **типизированный `launch.grace`** `{fixed|until_ready}` (systemd READY, k8s startupProbe):
>   время слепоты после старта может кончиться РАНЬШЕ по событию готовности. FR-31.
> - **`supervisor.min_stable_ms`** (supervisord startsecs): «сервис поднялся» = прожил N, а не
>   «фабрика вернулась» — ловит старт-и-сразу-смерть. FR-32.
>
> Итерация 8 (адверсариальное ревью дизайна, 24 находки) — главные:
> - **Рестарт процесса — встроенный глагол `restart_process`** (без `@ref`): резолвится в
>   «перезапусти процесс-ВЛАДЕЛЕЦ этого сервиса». Убирает `actions.restart_unified` и делает
>   `escalate_std` реально переиспользуемым — второй процесс берёт ту же лестницу, глагол целит
>   в НЕГО. Кросс-процессный промах структурно невыразим (было: 3 линзы нашли эту дыру).
>   `launch.restart_rate_limit` + `launch.type` живут на процессе.
> - **Типизирован `pacing`** (`{fixed|backoff}`) как grace — убит мёртвый блок.
> - **Типизирован `transport`** (params под вариантом `type`) — для `inmemory` нет мёртвых socket/format.
> - **Валидатор ужесточён** (правила 14–18): `tries≥1`+достижимость шагов, закрытое множество
>   watch-уровней, ёмкости коллекций C, `stop` запрещён на процесс-рестарте, глобальная
>   уникальность имён, `until_ready` требует ready-сигнала. `supervisor.max_consecutive_start_failures`
>   (py-сдача, supervisord startretries→FATAL). Полный разбор — DESIGN.md §Итерация 8.

```jsonc
{
  "schema": 2,                                    // версия схемы; незнакомая → отказ

  // ═══ FRAMEWORK — механика ══════════════════════════════════════════════════
  "framework": {

    "transport": {                                // как ВСЕ сигналы доставляются наблюдателю
      "type": "unix_datagram",                    //   реализация (будущие: udp, inmemory)
      "unix_datagram": {                          //   params ПОД вариантом type (итер.8): для type:"inmemory"
        "socket": "/var/run/svc_wd.sock",         //   этих полей нет — мёртвых socket/format у B не будет
        "format": "text_v1"                       //   "<svc>[ <state>[ <counter>]]" → PROTOCOL.md
      }                                           //   pulse="svc"; activity="svc active N"; idle закрывает эпизод
    },

    "observer": {                                 // C-демон: читает сигналы, исполняет лестницы
      "mode": "act",                              //   act | log — ГЛАВНЫЙ предохранитель
      "tick_ms": 1000,                            //   период суждения. 200..5000
      "pause_file": "/tmp/wd_pause",              //   файл есть → полная пауза (touch/rm руками)
      "oom_adj": -1000,                           //   не убиваем ядерным OOM-killer
      "log": {
        "file": "/mnt/sda1/crash_logs/wd.log",    //   на SD, НЕ /tmp(=RAM)
        "rotate_kb": 1024, "keep": 7,             //   ротация: порог и сколько хранить
        "fsync": true                             //   строка сразу на диск (переживает kill -9)
      },
      "quiet": { "paused_ms": 60000,              //   напоминание «на паузе». ≥1
                 "unknown_ms": 60000 }            //   чужие имена в сигналах. ≥1 (0 запрещён, правило 9; тестам 1)
    },

    "gates": {                                    // ресурсный гейт перед КАЖДЫМ действием
      "min_free_mb": 8,                           //   меньше памяти → ждём (> alarm_mb)
      "max_load1": 3.0,                           //   loadavg(1m) выше → ждём
      "recheck_ms": 5000,                         //   шаг перепроверки
      "force_after_ms": 300000,                   //   ждали дольше → действуем всё равно
      "alarm_mb": 5                               //   сигнальная линия в лог
    },

    "pacing": {                                   // паузы между повторами действия (типизировано, итер.8)
      "type": "fixed",                            //   "fixed" → только delay_ms | "backoff" → только start/factor/cap
      "delay_ms": 5000                            //   [type=fixed] ровный период. 2000..60000
      // "type":"backoff" → { "start_ms":2000, "factor":2, "cap_ms":60000 } — только активные params, без мёртвого блока
    }
  },

  // ═══ ACTIONS — каталог СЕРВИСНЫХ воздействий (лестницы выбирают по @имени) ═══
  // Только действия НАД СЕРВИСОМ (параметризуются {service}). Рестарт процесса —
  // НЕ действие каталога, а встроенный глагол "restart_process" (см. ladders).
  "actions": {
    "recreate": {                                 // пересоздать ОДИН сервис на месте
      "type": "request_file",                     //   наблюдатель кладёт файл → супервизор ест
      "file": "/tmp/svc_crash_{service}",         //   {service} подставляется; tmpfs ок (0 байт)
      "eat_within_ms": 10000,                     //   не съеден → сигнал P2_request_stuck (не ступень!)
      "startup_ms": 60000,                        //   съеден → столько не судим (фабрика ~34с)
      "rate_limit": { "max": 6, "per_ms": 600000,       //   >6 пересозданий сервиса за 10мин (итер.8:
                      "on_exceeded": "cooldown", "cooldown_ms": 300000 }
          //   max=6 ДОСТИЖИМ: мин.цикл = startup_ms(60с)+dead_after(15с)=75с, 600000/6=100000≥75000
          //   правило 6). Было 20 — недостижимо, инертный лимит. cooldown+аларм; счёт per-сервис. FR-30.
    }
  },

  // ═══ LADDERS — каталог лестниц (переиспользуемые политики ответа) ═══════════
  // Уровень пишет "ladder": "@имя" (выбор отсюда) или инлайн-массив (одноразовая).
  // Шаг "do": "@имя" = СЕРВИСНОЕ действие из actions | "restart_process" = ВСТРОЕННЫЙ ГЛАГОЛ
  // (без @): «перезапусти процесс-ВЛАДЕЛЕЦ этого сервиса» — резолвится из контекста вложенности,
  // промахнуться в чужой процесс НЕЛЬЗЯ (итер.8). Механика/лимит рестарта — на процессе (launch).
  "ladders": {
    "soft_only":    [ { "do": "@recreate" } ],
        // только пересоздание, без права на процесс (доказанно надёжный teardown)
    "escalate_std": [ { "do": "@recreate", "tries": 3 },
                      { "do": "restart_process" } ],
        // 3 пересоздания без выздоровления → рестарт СВОЕГО процесса. РЕАЛЬНО переиспользуема:
        // сервис любого процесса берёт эту лестницу, глагол целит в его процесс.
    "process_only": [ { "do": "restart_process" } ]
        // сразу процесс (для P-уровней: точечно лечить нечего)
  },

  // ═══ PROCESSES — топология: процесс ⊃ сервисы ⊃ {signals, watch} ════════════
  "processes": {

    "unified": {
      "launch": {                                 // как процесс СТАРТУЕТ и как его РЕСТАРТИТ глагол restart_process
        "type": "init_script",                    //   механизм: "init_script" | "inmemory" (B) | "external".
                                                  //   restart_process исполняет sh <script> restart через ЭТОТ тип
        "script": "/etc/init.d/rut_services",     //   procd-скрипт; его же зовёт restart_process
        "pidfile": "/tmp/rut_services.pid",       //   пишет launcher (путь отсюда); наблюдателю —
                                                  //   отличать «procd только что переродил» → grace
        "grace": {                                //   время слепоты после (ре)старта процесса (типизировано)
          "type": "fixed",                        //   "fixed" = ровно ms | "until_ready" = кончить по ready-сигналу
          "ms": 90000                             //   процесса (max_ms как потолок). until_ready ТРЕБУЕТ
        },                                        //   объявленного ready-сигнала (правило 13) → Э5; пока fixed=v1.2
        "restart_rate_limit": { "max": 5, "per_ms": 600000,  //   >5 рестартов процесса за 10мин = crash-loop
                                "on_exceeded": "cooldown", "cooldown_ms": 300000 }
            //   → cooldown + аларм (транзиентная причина может уйти; жечь RAM/CPU/лог перестаём).
            //   "stop" ЗАПРЕЩЁН здесь (правило 12): единственный путь спасения бокса без оператора.
      },
      "supervisor": {                             // библиотечный супервизор ВНУТРИ процесса (py)
        "poll_ms": 1000,                          //   опрос файлов-запросов. ≤ min(eat_within_ms)/5 (правило 6)
        "stop_timeout_ms": 20000,                 //   потолок остановки сервиса при пересоздании
        "min_stable_ms": 5000,                    //   «сервис поднялся» = прожил столько; смерть раньше =
                                                  //   как упавшая фабрика (backoff), не «успешный старт». FR-32
        "max_consecutive_start_failures": 5,      //   итер.8: N падений фабрики подряд → супервизор БРОСАЕТ
                                                  //   пересоздавать (лог); сервис молчит → L1-лестница
                                                  //   эскалирует в процесс. Py-сдача (supervisord startretries). FR-37
        "backoff": { "start_ms": 1000, "factor": 2, "cap_ms": 10000 },
        "log": { "file": "/mnt/sda1/crash_logs/unified.log", "rotate_kb": 1024, "keep": 7,
                 "fallbacks": ["/usr/local/home/root/logs/processes/services_runtime.log",
                               "/tmp/services_runtime.log"] }
                                                  //   каталог file недоступен (SD не смонтирована) →
                                                  //   первый рабочий из fallbacks; [] = без запасных
      },
      "watch": {
        "P1_all_pulses_lost": {                   // судит pulse-сигналы ВСЕХ сервисов разом:
          "mode": "act",                          //   все молчат = завис общий event loop.
          "ladder": "@process_only"               //   Правило ядра: судится только при ≥2 сервисах
        },                                        //   (с одним неотличим от его L1 — валидатор предупредит)
        "P2_request_stuck": {                     // судит ПОЕДАНИЕ файлов-запросов моих сервисов:
          "mode": "act",                          //   запрос лежит дольше eat_within_ms своего
          "ladder": "@process_only"               //   действия = супервизор мёртв (сервисы могут
        }                                         //   ещё пульсировать — лечит только рестарт процесса)
      },

      "services": {

        "udp_logger": {
          "start": { "type": "python", "entry": "run_services:start_udp_logger" },
          "signals": {
            "pulse": { "from": "loop", "every_ms": 5000 }
                //  loop = пульс шлёт СОБСТВЕННЫЙ рабочий цикл сервиса — сам факт отправки
                //  и есть доказательство жизни; полю probe тут неоткуда взяться.
          },                                      //  сигнала activity НЕТ = поток не инструментирован → и L2 нет
          "watch": {
            "L1_pulse_lost": {                    // ← судит signals.pulse
              "mode": "act",
              "dead_after_ms": 15000,             //   ≥ 3× every_ms (валидатор)
              "ladder": "@soft_only"              //   без права на процесс: пересоздание доказано (G4)
            }
          }
        },

        "data_uploader": {
          "start": { "type": "python", "entry": "run_services:start_data_uploader" },
          "signals": {
            "pulse": { "from": "loop", "every_ms": 5000 }
          },
          "watch": {
            "L1_pulse_lost": {
              "mode": "act",
              "dead_after_ms": 15000,
              "ladder": "@escalate_std"           //   тяжёлый (:1111/MQTT) — имеет право на процесс
            }
          }
        },

        "config_server": {
          "start": { "type": "python", "entry": "run_services:start_config_server" },
          "port": 8000,                           //   ЕДИНАЯ истина: bind фабрики И probe ниже
          "signals": {
            "pulse": {
              "from": "probe",                    //   нет своего цикла → пульс шлёт библиотека,
              "every_ms": 5000,                   //   и КАЖДОМУ пульсу предшествует probe:
              "probe": { "type": "tcp",           //   connect на свой порт; принял → пульс уходит,
                         "port": "@port",         //   нет → пульс НЕ шлётся → это увидит L1.
                         "timeout_ms": 2000 }     //   probe без from:"probe" невыразим — и наоборот.
            },
            "activity": {                         //   счётчик «поток крутится» от флеш-логики:
              "tick_ms": 2000                     //   не чаще этого шлётся "active N", и такими же
            }                                     //   отрезками рабочий код нарезает ожидания
          },                                      //   (передаётся ему параметром); idle закрывает эпизод
          "watch": {
            "L1_pulse_lost": {                    // ← судит signals.pulse
              "mode": "act",
              "dead_after_ms": 30000,             //   щедрее: event loop занят при прошивке
              "ladder": "@escalate_std"
            },
            "L2_activity_frozen": {               // ← судит signals.activity: счётчик замер при
              "mode": "act",                      //   state=active дольше порога = поток завис.
              "frozen_after_ms": 60000,           //   ≥ 20× tick_ms (тики идут даже сквозь erase →
              "ladder": "@escalate_std"           //   запас 30×). «Устройство молчит» — не сюда: это
            }                                     //   ловят таймауты самого кода (пошлёт idle).
          }
        },

        "ws_bridge": {
          "start": { "type": "python", "entry": "run_services:start_ws_bridge" },
          "port": 81,
          "signals": {
            "pulse": { "from": "probe", "every_ms": 5000,
                       "probe": { "type": "tcp", "port": "@port", "timeout_ms": 2000 } }
          },
          "watch": {
            "L1_pulse_lost": {
              "mode": "act",
              "dead_after_ms": 15000,
              "ladder": "@escalate_std"
            }
          }
        }
      }
    }
  }
}
```

## Сигналы ↔ уровни (парность, обе стороны одного имени)

| Сигнал (эмиссия, py) | Уровень (суждение, C) | Что значит срабатывание |
|---|---|---|
| `signals.pulse` | `watch.L1_pulse_lost` | пульсов нет дольше `dead_after_ms` → цикл/листенер мёртв |
| `signals.activity` | `watch.L2_activity_frozen` | счётчик замер при active дольше `frozen_after_ms` → поток завис |
| pulse всех сервисов | `process.watch.P1_all_pulses_lost` | молчат ВСЕ разом → завис event loop процесса |
| поедание файлов-запросов | `process.watch.P2_request_stuck` | запрос лежит > `eat_within_ms` → супервизор процесса мёртв |

Нет сигнала — нет уровня (и наоборот): парность требует валидатор. Новый глубокий уровень =
новая пара «сигнал + watch-блок» (аддитивно; ключ называет сигнал).

## Связи одним взглядом

```
processes.unified ──содержит──▶ services.{udp_logger, data_uploader, config_server, ws_bridge}
services.*.signals ──датаграммы──▶ framework.transport ──▶ observer (судит по watch.*)
watch.*.ladder ──@──▶ ladders.{soft_only, escalate_std, process_only}
ladders.*[].do ──@──▶ actions.{recreate}   |   "restart_process" = ВСТРОЕННЫЙ ГЛАГОЛ (без @)
restart_process ──контекст──▶ процесс-ВЛАДЕЛЕЦ сервиса → его launch (type+script+restart_rate_limit)
signals.pulse.probe.port ──@──▶ этот же service.port
```

## Подключение нового (по образцу на каждую ось)

```jsonc
// сервис   = блок в services его процесса; минимум: start + signals.pulse + watch.L1_pulse_lost
// процесс  = блок в processes (launch несёт СВОЙ script+restart_rate_limit); НИЧЕГО в actions
//            добавлять НЕ нужно — глагол restart_process целит в этот процесс сам:
"processes": { "vision": { "launch": { "type":"init_script", "script":"/etc/init.d/vision",
                                        "pidfile":"/tmp/vision.pid", "grace":{...},
                                        "restart_rate_limit":{...} },
                           "supervisor": {...},
                           "watch": { "P2_request_stuck": { "mode":"act", "ladder":"@process_only" } },
                           //  ОДИН сервис → P1 НЕ ставим (правило 11: инертен); покрытие = L1+P2
                           "services": { "camera_feed": {
                              "start": {...}, "signals": {...},
                              "watch": { "L1_pulse_lost": { "mode":"act", "dead_after_ms":15000,
                                                            "ladder":"@escalate_std" } } } } } }
//  escalate_std ПЕРЕИСПОЛЬЗУЕТСЯ как есть: её restart_process целит в vision, не в unified.
// действие = блок в actions (СЕРВИСНОЕ, {service}-параметр; + адаптер если новый type)
// probe    = новый type (напр. "http") — только для pulse c from:"probe"
// транспорт= новый type у framework.transport ("inmemory" — потребитель B)
// уровень  = новая пара signals.X + watch.LN_X_ (+ поддержка в core; ключ из закрытого множества, правило 16)
```

## Валидатор (fail-closed, все проблемы разом, ДО запуска)

1. `schema` известна; все обязательные ключи на месте (ВСЕ пропуски одним списком).
2. Все `type` — из словарей адаптеров; неизвестный → отказ.
3. `@`-ссылки резолвятся: `ladder`-строка→ladders; шаг `do` с `@`→actions; `port`→ключ своего
   сервиса. Шаг `do` БЕЗ `@` = встроенный глагол из закрытого множества `{restart_process}`; иначе отказ.
4. Форма пульса: `from:"loop"` ⇒ probe ОТСУТСТВУЕТ; `from:"probe"` ⇒ probe ОБЯЗАТЕЛЕН.
5. Парность: у каждого сервиса `signals.pulse` + `watch.L1_pulse_lost`;
   `signals.activity` ⟺ `watch.L2_activity_frozen`.
6. Числовые связки: `dead_after_ms ≥ 3×every_ms` · `frozen_after_ms ≥ 20×tick_ms` ·
   `probe.timeout_ms < every_ms` · `min_free_mb > alarm_mb` · `poll_ms ≤ min(eat_within_ms)/5` ·
   `grace.until_ready ⇒ max_ms` · `rate_limit.cooldown_ms > per_ms/max` ·
   **ДОСТИЖИМОСТЬ rate_limit: для request_file-действия `per_ms/max ≥ startup_ms + min(dead_after_ms
   сервисов, использующих его)`** (иначе лимит инертен — итер.8).
7. Имена уникальны; **имена сервисов уникальны ГЛОБАЛЬНО по ВСЕМ процессам** (датаграмма
   `text_v1` не несёт квалификатора процесса — коллизия слепит `last_seen`); длины строк по ёмкостям C.
8. **НЕИЗВЕСТНЫЙ ключ = отказ** (защита от опечаток: `"trys": 3` не молча-игнор — итер.6).
9. Rate-limit'ы и лог-интервалы ≥ 1мс: вырожденный `0` запрещён (тестам хватает 1).
10. Шаблоны `file` request_file-действий уникальны (иначе неоднозначное поедание).
11. Предупреждение: процесс с ОДНИМ сервисом и `P1...mode:"act"` — P1 инертен (правило ядра ≥2);
    покрытие одно-сервисного процесса = L1-лестница + P2.
12. Каждое действие/`restart_rate_limit` несёт `rate_limit`; `on_exceeded ∈ {cooldown, stop}`;
    **`stop` ЗАПРЕЩЁН на `launch.restart_rate_limit`** — единственный путь спасения бокса без
    оператора не имеет права сдаться навсегда (итер.8).
13. `grace.type ∈ {fixed, until_ready}`; `fixed ⇒ ms`; **`until_ready ⇒ max_ms И процесс объявляет
    ready-сигнал** (иначе accept-but-cannot-honor; пока ready-сигнала нет — легален только fixed, итер.8).
14. **`pacing.type ∈ {fixed, backoff}`**; `fixed ⇒ delay_ms`; `backoff ⇒ {start_ms,factor,cap_ms}` —
    типизированный вариант, только активные params (мёртвого блока нет, итер.8).
15. **Лестница: каждый `tries` (если задан) ≥ 1; ТОЛЬКО последний шаг может опустить `tries`;
    не-последний шаг без `tries` = ОТКАЗ** (следующий шаг недостижим — итер.8, перенос v1.2-урока).
16. **watch-уровни из закрытого множества**: сервис `{L1_pulse_lost, L2_activity_frozen}`,
    процесс `{P1_all_pulses_lost, P2_request_stuck}`; неизвестный ключ уровня = отказ (итер.8).
17. **Ёмкости коллекций (compile-time C)**: ≤MAX_PROCESSES · процесс 1..MAX_SERVICES · ≤MAX_ACTIONS ·
    ≤MAX_LADDERS · лестница ≤MAX_LADDER_STEPS; превышение = ОТКАЗ, не усечение (итер.8; см. PROTOCOL).
```

## Что нашло ревью дизайна (итерация 8) — сводка

| Тема (нашли N линз) | Правка |
|---|---|
| кросс-процессный промах лестницы (3 линзы, топ) | глагол `restart_process` = процесс-владелец; `restart_unified` убран из actions; промах невыразим |
| `pacing` — мёртвый блок (mode+оба варианта) | типизирован как grace (правило 14) |
| валидация `tries` без дома (TRACE врал) | правило 15 (`tries≥1`, достижимость шагов) |
| глобальная уникальность имён сервисов | правило 7 (датаграмма без квалификатора процесса) |
| `transport.socket/format` мёртвы для inmemory | params под вариантом `type` |
| `recreate.rate_limit max=20` недостижим (инертен) | max=6 + правило достижимости (правило 6) |
| py-супервизор без сдачи | `supervisor.max_consecutive_start_failures` (FR-37) |
| `on_exceeded:"stop"` мог обездвижить бокс | запрещён на процесс-рестарте (правило 12) |
| `until_ready` accept-but-cannot-honor | требует ready-сигнала (правило 13) |
| watch-уровни / ёмкости C не закрыты | правила 16, 17 |
| `unknown_ms:0` противоречие | 0 запрещён везде (правило 9) |
| emit не оговаривал thread-safety | КОНТРАКТ Transport (FR-11 расширен) — Э2 |
| activity обновляет L1 минуя probe | by design (работа = жизнь) — задокументировано в PROTOCOL |

## Чего не хватало (ресёрч Р1 → итерация 7)

| Дыра | Откуда | Закрыта |
|---|---|---|
| терминальное действие долбит ВЕЧНО | OTP intensity, supervisord startretries | `actions.*.rate_limit` / `launch.restart_rate_limit` |
| время слепоты УГАДАНО | systemd READY, k8s startupProbe | типизированный `grace` (fixed \| until_ready) |
| «старт успешен» = фабрика вернулась | supervisord startsecs | `supervisor.min_stable_ms` |
| clock прибит к `time.monotonic` | потребитель B, trio | КОНТРАКТ core (clock-инъекция, FR-22) — Э2 |
| задачи-сироты (GC-ловушка) | v1.2 G3, trio nursery | ПРАВИЛА FR-20/21 — Э2 |

Принципы не сломаны: всё типизировано (`type`+активные params), без заполнителей и магий-нулей,
связи через `@` либо контекст-глагол, инварианты — в валидаторе, а не в комментариях.
