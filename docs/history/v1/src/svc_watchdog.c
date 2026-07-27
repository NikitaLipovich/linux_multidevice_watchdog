/* svc_watchdog — WATCHDOG MINIMAL v1 (spec: artifacts/svc-consolidation/WATCHDOG_MINIMAL.md)
 *
 * Dumb restarter: services of the unified python process send text heartbeats
 * ("<name>", "<name> <state>" or "<name> <state> <counter>") to a unix datagram
 * socket. Silence longer than
 * wait_pulse_timeout_ms => create the service's crash file (its supervisor polls
 * it and recreates the service). Silence from ALL services => the loop is hung
 * or the process is dead => "/etc/init.d/<...> restart". Before any restart the
 * daemon checks RAM/CPU and waits (bounded) if the box is starving. Every event
 * goes to a rotated log on the SD card.
 *
 * Single thread, epoll(socket + 1s timerfd). All waiting is STATE, never sleep:
 * heartbeat reception must not stall while a target waits for resources.
 *
 * Config: JSON via libjson-c (resident on RutOS). Test hooks via env:
 *   WD_PROC_MEMINFO / WD_PROC_LOADAVG — fake /proc files for unit tests.
 */

#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/epoll.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/timerfd.h>
#include <sys/types.h>
#include <sys/un.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#include <json-c/json.h>

#define MAX_SERVICES 30   /* ~400Б/запись => ~12КБ статики; лимит согласован */
#define NAME_MAX_LEN 48
#define STATE_MAX_LEN 24
#define PATH_MAX_LEN 256

typedef struct {
    char name[NAME_MAX_LEN];
    long timeout_ms;                 /* wait_pulse_timeout_ms */
    long stall_ms;                   /* progress_stall_ms; 0 = L2 disabled */
    int action_restart;              /* 1 = restart, 0 = log only */
    int escalate_to_process;         /* mitigation "Б": may this service trigger a process restart */
    int softfail_logged;             /* soft_restart_failed logged this episode */
    /* runtime */
    long long last_seen;             /* monotonic ms; 0 = never */
    char state[STATE_MAX_LEN];
    int silent_logged;               /* no_pulse already logged this episode */
    /* L2 progress-stall: counter = liveness tick of the client's protocol loop.
     * No ticks while state stays "active" => the worker THREAD is dead
     * (deadlock / stuck syscall). "Device silent" is NOT our case — the client's
     * own timeouts handle that and its finally sends "idle". */
    int episode;                     /* counter-bearing pulses seen since last state change */
    long long last_counter;
    long long last_progress;         /* monotonic ms of last counter CHANGE */
    int stalled_logged;              /* stalled already logged this episode */
    int stall_restart;               /* pending crash file was stall-triggered */
    long long crash_created;         /* !=0: crash file created, awaiting consume */
    char crash_path[PATH_MAX_LEN];
    int attempts;                    /* consecutive restart attempts */
    long long next_attempt;          /* delay gate (monotonic ms) */
    long long wait_started;          /* resource-wait start; 0 = not waiting */
    long long last_wait_log;
} svc_t;

typedef struct {
    char socket_path[PATH_MAX_LEN];
    long beat_interval_ms, tick_ms;
    char pause_file[PATH_MAX_LEN];
    char log_file[PATH_MAX_LEN];
    long log_max_bytes; int log_keep; int log_fsync;
    int self_oom_adj;
    double floor_mb, min_free_mb, max_load1;
    long recheck_ms, max_wait_ms;
    int delay_fixed;                 /* 1=fixed, 0=backoff */
    long delay_interval_ms, delay_initial_ms, delay_max_ms; double delay_factor;
    char proc_name[NAME_MAX_LEN];
    char initd[PATH_MAX_LEN];
    char pidfile[PATH_MAX_LEN];
    char crash_template[PATH_MAX_LEN];
    long consume_timeout_ms, start_grace_ms, relaunch_ms;
    long paused_every_ms, unknown_every_ms;
    int proc_action_restart;
    int soft_enabled;                /* soft_fail_escalation.enabled (global master switch) */
    int soft_after;                  /* soft_fail_escalation.after_attempts */
    svc_t svc[MAX_SERVICES]; int nsvc;
} cfg_t;

static cfg_t C;
static int log_fd = -1;
static long long grace_until = 0;
static long long proc_next_attempt = 0, proc_wait_started = 0, proc_last_wait_log = 0;
static int proc_attempts = 0, all_silent_logged = 0;
static long long paused_last_log = 0;
static int below_floor = 0;

/* ---------------------------------------------------------------- time/log */

static long long now_ms(void) {
    struct timespec ts; clock_gettime(CLOCK_MONOTONIC, &ts);
    return (long long)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}

static void rotate_if_needed(void) {
    struct stat st;
    if (log_fd < 0 || fstat(log_fd, &st) != 0 || st.st_size < C.log_max_bytes) return;
    close(log_fd); log_fd = -1;
    char from[PATH_MAX_LEN + 16], to[PATH_MAX_LEN + 16];
    for (int i = C.log_keep - 1; i >= 1; i--) {
        snprintf(from, sizeof from, "%s.%d", C.log_file, i);
        snprintf(to, sizeof to, "%s.%d", C.log_file, i + 1);
        rename(from, to);                     /* best effort */
    }
    snprintf(to, sizeof to, "%s.1", C.log_file);
    rename(C.log_file, to);
}

static void wd_log(const char *fmt, ...) {
    if (log_fd < 0) {
        log_fd = open(C.log_file, O_WRONLY | O_APPEND | O_CREAT, 0644);
        if (log_fd < 0) return;
    }
    char line[512]; time_t t = time(NULL); struct tm tm;
    localtime_r(&t, &tm);
    int n = (int)strftime(line, sizeof line, "%Y-%m-%dT%H:%M:%S ", &tm);
    /* up= — монотонные секунды: wall-clock на боксе прыгает (time_sync/RTC),
     * без монотонной метки порядок событий в логе нечитаем (урок матрицы G3) */
    n += snprintf(line + n, sizeof line - n, "up=%lld ", now_ms() / 1000);
    va_list ap; va_start(ap, fmt);
    n += vsnprintf(line + n, sizeof line - n - 2, fmt, ap);
    va_end(ap);
    line[n++] = '\n';
    if (write(log_fd, line, n) < 0) { /* ignore */ }
    if (C.log_fsync) fsync(log_fd);
    rotate_if_needed();
}

/* ---------------------------------------------------------------- config
 * v1.2 FAIL-CLOSED: КАЖДЫЙ читаемый ключ ОБЯЗАТЕЛЕН. Нет ключа / кривое
 * значение / строка длиннее буфера => stderr "missing key ..." (ВСЕ проблемы
 * разом, чинится за один заход) и exit 1 из main. Дефолтов в коде НЕТ:
 * единственный источник истины — конфиг (решение пользователя, v1.2). */

static int cfg_bad;

static json_object *req_obj(json_object *o, const char *where, const char *k) {
    json_object *v;
    if (!o) return NULL;                     /* родительская секция уже отругана */
    if (!json_object_object_get_ex(o, k, &v)) {
        fprintf(stderr, "svc_watchdog: missing key %s%s\n", where, k);
        cfg_bad = 1;
        return NULL;
    }
    return v;
}
static const char *req_str(json_object *o, const char *where, const char *k) {
    json_object *v = req_obj(o, where, k);
    return v ? json_object_get_string(v) : "";
}
static double req_num(json_object *o, const char *where, const char *k) {
    json_object *v = req_obj(o, where, k);
    return v ? json_object_get_double(v) : 0;
}
static int req_bool(json_object *o, const char *where, const char *k) {
    json_object *v = req_obj(o, where, k);
    return v ? json_object_get_boolean(v) : 0;
}
/* копирование с проверкой ёмкости: длиннее буфера = ОШИБКА конфига, а не
 * молчаливое усечение (усечённое имя рассинхронизирует пульсы/crash-файлы) */
static void req_strcpy(json_object *o, const char *where, const char *k,
                       char *dst, size_t cap) {
    const char *val = req_str(o, where, k);
    size_t len = strlen(val);
    if (len >= cap) {
        fprintf(stderr, "svc_watchdog: %s%s too long (%zu chars, max %zu)\n",
                where, k, len, cap - 1);
        cfg_bad = 1;
        len = cap - 1;
    }
    memcpy(dst, val, len); dst[len] = 0;
}
/* "restart"|"log" -> 1|0; всё прочее = ошибка ("" = ключ уже отруган) */
static int parse_action(const char *val, const char *where) {
    if (!strcmp(val, "restart")) return 1;
    if (!strcmp(val, "log")) return 0;
    if (val[0]) {
        fprintf(stderr, "svc_watchdog: %saction must be restart|log\n", where);
        cfg_bad = 1;
    }
    return 0;
}

static int load_config(const char *path) {
    json_object *root = json_object_from_file(path);
    if (!root) { fprintf(stderr, "svc_watchdog: cannot parse %s\n", path); return -1; }
    cfg_bad = 0;

    req_strcpy(root, "", "socket", C.socket_path, sizeof C.socket_path);
    C.beat_interval_ms = (long)req_num(root, "", "beat_interval_ms");
    C.tick_ms = (long)req_num(root, "", "tick_ms");
    req_strcpy(root, "", "pause_file", C.pause_file, sizeof C.pause_file);
    C.self_oom_adj = (int)req_num(root, "", "self_oom_adj");
    C.paused_every_ms = (long)req_num(root, "", "paused_log_every_ms");
    C.unknown_every_ms = (long)req_num(root, "", "unknown_name_log_every_ms");

    json_object *o = req_obj(root, "", "log");
    req_strcpy(o, "log.", "file", C.log_file, sizeof C.log_file);
    C.log_max_bytes = (long)req_num(o, "log.", "max_bytes");
    C.log_keep = (int)req_num(o, "log.", "keep");
    C.log_fsync = req_bool(o, "log.", "fsync");

    o = req_obj(root, "", "resources");
    C.floor_mb = req_num(o, "resources.", "rut_floor_mb");
    C.min_free_mb = req_num(o, "resources.", "min_free_mb");
    C.max_load1 = req_num(o, "resources.", "max_load1");
    C.recheck_ms = (long)req_num(o, "resources.", "recheck_ms");
    C.max_wait_ms = (long)req_num(o, "resources.", "max_wait_ms");
    if (o && C.min_free_mb <= C.floor_mb) {
        fprintf(stderr, "svc_watchdog: min_free_mb must be > rut_floor_mb\n");
        cfg_bad = 1;
    }

    o = req_obj(root, "", "restart_delay");
    {
        const char *mode = req_str(o, "restart_delay.", "mode");
        if (!strcmp(mode, "fixed")) C.delay_fixed = 1;
        else if (!strcmp(mode, "backoff")) C.delay_fixed = 0;
        else if (mode[0]) {
            fprintf(stderr, "svc_watchdog: restart_delay.mode must be fixed|backoff\n");
            cfg_bad = 1;
        }
    }
    C.delay_interval_ms = (long)req_num(o, "restart_delay.", "interval_ms");
    C.delay_initial_ms = (long)req_num(o, "restart_delay.", "initial_ms");
    C.delay_factor = req_num(o, "restart_delay.", "factor");
    C.delay_max_ms = (long)req_num(o, "restart_delay.", "max_ms");

    o = req_obj(root, "", "process");
    req_strcpy(o, "process.", "name", C.proc_name, sizeof C.proc_name);
    req_strcpy(o, "process.", "initd", C.initd, sizeof C.initd);
    req_strcpy(o, "process.", "pidfile", C.pidfile, sizeof C.pidfile);
    req_strcpy(o, "process.", "crash_file_template", C.crash_template, sizeof C.crash_template);
    C.consume_timeout_ms = (long)req_num(o, "process.", "consume_timeout_ms");
    C.start_grace_ms = (long)req_num(o, "process.", "start_grace_ms");
    C.relaunch_ms = (long)req_num(o, "process.", "service_relaunch_ms");
    C.proc_action_restart = parse_action(req_str(o, "process.", "action"), "process.");
    {
        json_object *sfe = req_obj(o, "process.", "soft_fail_escalation");
        C.soft_enabled = req_bool(sfe, "process.soft_fail_escalation.", "enabled");
        C.soft_after = (int)req_num(sfe, "process.soft_fail_escalation.", "after_attempts");
        if (sfe && C.soft_after < 1) {
            fprintf(stderr, "svc_watchdog: soft_fail_escalation.after_attempts must be >= 1\n");
            cfg_bad = 1;
        }
    }

    json_object *arr = req_obj(root, "", "services");
    C.nsvc = 0;
    if (arr) {
        size_t len = json_object_get_type(arr) == json_type_array
                     ? json_object_array_length(arr) : 0;
        if (len == 0) {
            fprintf(stderr, "svc_watchdog: services must be a non-empty array\n");
            cfg_bad = 1;
        } else if (len > MAX_SERVICES) {
            fprintf(stderr, "svc_watchdog: services[] has %zu entries, compile-time max is %d\n",
                    len, MAX_SERVICES);
            cfg_bad = 1;
        } else for (size_t i = 0; i < len; i++) {
            json_object *s = json_object_array_get_idx(arr, i);
            char where[32]; snprintf(where, sizeof where, "services[%zu].", i);
            svc_t *v = &C.svc[C.nsvc];
            memset(v, 0, sizeof *v);
            req_strcpy(s, where, "name", v->name, sizeof v->name);
            v->timeout_ms = (long)req_num(s, where, "wait_pulse_timeout_ms");
            if (v->name[0] && v->timeout_ms < 3 * C.beat_interval_ms) {
                fprintf(stderr, "svc_watchdog: %s: wait_pulse_timeout_ms < 3*beat_interval_ms\n",
                        v->name);
                cfg_bad = 1;
            }
            v->stall_ms = (long)req_num(s, where, "progress_stall_ms");
            if (v->stall_ms < 0) {
                fprintf(stderr, "svc_watchdog: %sprogress_stall_ms must be >= 0\n", where);
                cfg_bad = 1;
            }
            v->action_restart = parse_action(req_str(s, where, "action"), where);
            v->escalate_to_process = req_bool(s, where, "escalate_to_process");
            /* expand crash_file_template's {name} once; переполнение = ошибка */
            const char *br = strstr(C.crash_template, "{name}");
            int r;
            if (br)
                r = snprintf(v->crash_path, sizeof v->crash_path, "%.*s%s%s",
                             (int)(br - C.crash_template), C.crash_template, v->name, br + 6);
            else
                r = snprintf(v->crash_path, sizeof v->crash_path, "%s", C.crash_template);
            if (r < 0 || (size_t)r >= sizeof v->crash_path) {
                fprintf(stderr, "svc_watchdog: %scrash path too long\n", where);
                cfg_bad = 1;
            }
            strcpy(v->state, "-");
            C.nsvc++;
        }
    }
    json_object_put(root);
    if (cfg_bad) {
        fprintf(stderr, "svc_watchdog: config %s rejected (fail-closed, no defaults)\n", path);
        return -1;
    }
    return 0;
}

/* ---------------------------------------------------------------- resources */

static double mem_available_mb(void) {
    const char *p = getenv("WD_PROC_MEMINFO"); if (!p) p = "/proc/meminfo";
    FILE *f = fopen(p, "r"); if (!f) return 1e9;
    char k[64]; double v = 1e9; char line[128];
    while (fgets(line, sizeof line, f))
        if (sscanf(line, "%63s %lf", k, &v) == 2 && !strcmp(k, "MemAvailable:")) break;
    fclose(f);
    return v / 1024.0;
}

static double load1(void) {
    const char *p = getenv("WD_PROC_LOADAVG"); if (!p) p = "/proc/loadavg";
    FILE *f = fopen(p, "r"); if (!f) return 0;
    double v = 0; if (fscanf(f, "%lf", &v) != 1) v = 0;
    fclose(f); return v;
}

static int resources_healthy(double *mem, double *load) {
    *mem = mem_available_mb(); *load = load1();
    return *mem >= C.min_free_mb && *load <= C.max_load1;
}

/* Age of the supervised process in ms via its pidfile (+/proc starttime).
 * <0 = unknown (no pidfile / process gone) — caller proceeds with restart. */
static long long process_age_ms(void) {
    if (!C.pidfile[0]) return -1;
    FILE *f = fopen(C.pidfile, "r"); if (!f) return -1;
    long pid = 0; int ok = fscanf(f, "%ld", &pid) == 1; fclose(f);
    if (!ok || pid <= 0) return -1;
    char p[64]; snprintf(p, sizeof p, "/proc/%ld/stat", pid);
    f = fopen(p, "r"); if (!f) return -1;
    char buf[512]; size_t n = fread(buf, 1, sizeof buf - 1, f); fclose(f);
    buf[n] = 0;
    char *rp = strrchr(buf, ')');           /* skip "pid (comm)" safely */
    if (!rp) return -1;
    unsigned long long starttime = 0; int field = 2;
    for (char *tok = strtok(rp + 1, " "); tok; tok = strtok(NULL, " ")) {
        field++;
        if (field == 22) { starttime = strtoull(tok, NULL, 10); break; }
    }
    if (!starttime) return -1;
    f = fopen("/proc/uptime", "r"); if (!f) return -1;
    double up = 0; ok = fscanf(f, "%lf", &up) == 1; fclose(f);
    if (!ok) return -1;
    long hz = sysconf(_SC_CLK_TCK); if (hz <= 0) hz = 100;
    double age_s = up - (double)starttime / hz;
    return age_s < 0 ? -1 : (long long)(age_s * 1000.0);
}

/* Returns 1 when the caller may act now; otherwise logs waiting_resources
 * (rate-limited) and keeps the wait state. max_wait exceeded => act anyway. */
static int resource_gate(long long now, long long *wait_started, long long *last_log,
                         const char *target) {
    double mem, load;
    if (resources_healthy(&mem, &load)) { *wait_started = 0; return 1; }
    if (!*wait_started) { *wait_started = now; *last_log = 0; }
    if (now - *wait_started >= C.max_wait_ms) {
        wd_log("waiting_resources_expired target=%s waited_s=%lld mem_mb=%.1f load1=%.2f",
               target, (now - *wait_started) / 1000, mem, load);
        *wait_started = 0;
        return 1;                    /* restart anyway: it usually FREES memory */
    }
    if (now - *last_log >= C.recheck_ms) {
        wd_log("waiting_resources target=%s mem_mb=%.1f load1=%.2f", target, mem, load);
        *last_log = now;
    }
    return 0;
}

static long delay_for_attempt(int attempts) {
    if (C.delay_fixed) return C.delay_interval_ms;
    double d = C.delay_initial_ms;
    for (int i = 1; i < attempts; i++) { d *= C.delay_factor; if (d >= C.delay_max_ms) break; }
    return d > C.delay_max_ms ? C.delay_max_ms : (long)d;
}

/* ---------------------------------------------------------------- actions */

static void restart_process(long long now, const char *reason) {
    proc_attempts++;
    wd_log("process_restart reason=%s attempt=%d initd=%s", reason, proc_attempts, C.initd);
    pid_t pid = fork();
    if (pid == 0) {
        execl("/bin/sh", "sh", C.initd, "restart", (char *)NULL);
        _exit(127);
    }
    if (pid > 0) { int st; waitpid(pid, &st, 0); }
    grace_until = now + C.start_grace_ms;
    proc_next_attempt = now + delay_for_attempt(proc_attempts);
    all_silent_logged = 0;
    for (int i = 0; i < C.nsvc; i++) {          /* fresh episode for everyone */
        C.svc[i].last_seen = 0; C.svc[i].silent_logged = 0;
        C.svc[i].crash_created = 0; C.svc[i].wait_started = 0;
        C.svc[i].attempts = 0; C.svc[i].softfail_logged = 0;
        C.svc[i].episode = 0; C.svc[i].stalled_logged = 0; C.svc[i].stall_restart = 0;
    }
}

/* ---------------------------------------------------------------- tick */

static void handle_tick(void) {
    long long now = now_ms();

    double mem = mem_available_mb();
    if (mem < C.floor_mb && !below_floor) {
        below_floor = 1;
        wd_log("mem_below_floor mem_mb=%.1f floor_mb=%.1f", mem, C.floor_mb);
    } else if (mem >= C.floor_mb) {
        below_floor = 0;
    }

    if (access(C.pause_file, F_OK) == 0) {
        if (now - paused_last_log >= C.paused_every_ms) {
            wd_log("paused file=%s", C.pause_file);
            paused_last_log = now;
        }
        return;
    }
    paused_last_log = 0;

    if (now < grace_until) return;

    /* FRESH-гейт — В НАЧАЛЕ тика, а не только в no_pulse_all: после kill -9
     * procd мгновенно перерождает процесс; пока тот бутится (~30с), и
     * per-service путь опасен — crash-файл ляжет под ещё не запущенный
     * супервизор → ложная «not consumed»-эскалация (пойман матрицей v3 шаг 3).
     * Процесс моложе start_grace_ms => полный grace на ВСЁ. */
    {
        long long age = process_age_ms();
        if (age >= 0 && age < C.start_grace_ms) {
            wd_log("fresh_process_detected age_s=%lld — grace instead of checks", age / 1000);
            grace_until = now + (C.start_grace_ms - age);
            all_silent_logged = 0;
            for (int i = 0; i < C.nsvc; i++) {
                C.svc[i].last_seen = 0; C.svc[i].silent_logged = 0;
                C.svc[i].crash_created = 0; C.svc[i].wait_started = 0;
                C.svc[i].attempts = 0; C.svc[i].softfail_logged = 0;
                C.svc[i].episode = 0; C.svc[i].stalled_logged = 0; C.svc[i].stall_restart = 0;
            }
            return;
        }
    }

    int nsilent = 0;
    for (int i = 0; i < C.nsvc; i++) {
        svc_t *v = &C.svc[i];
        long long ref = v->last_seen ? v->last_seen : grace_until;
        if (now - ref > v->timeout_ms) nsilent++;
    }

    if (nsilent == C.nsvc) {                     /* hung loop / dead process */
        if (!all_silent_logged) { wd_log("no_pulse_all n=%d", C.nsvc); all_silent_logged = 1; }
        if (!C.proc_action_restart) return;      /* observe mode */
        if (now < proc_next_attempt) return;
        /* fresh-procd-respawn case is handled by the top-of-tick fresh gate */
        if (!resource_gate(now, &proc_wait_started, &proc_last_wait_log, "process")) return;
        restart_process(now, "no_pulse_all");
        return;
    }
    all_silent_logged = 0;

    for (int i = 0; i < C.nsvc; i++) {
        svc_t *v = &C.svc[i];
        long long ref = v->last_seen ? v->last_seen : grace_until;
        long long silence = now - ref;
        int silent = silence > v->timeout_ms;
        /* L2 progress-stall: базовый пульс жив, эпизод открыт (counter-пульсы
         * шли), state=active, но счётчик (liveness-тик протокольного цикла
         * клиента) не менялся дольше progress_stall_ms => рабочий поток мёртв.
         * Демону всё равно, ЧТО значит счётчик — только «менялся или нет». */
        int stalled = !silent && v->stall_ms > 0 && v->episode &&
                      strcmp(v->state, "active") == 0 &&
                      now - v->last_progress > v->stall_ms;
        if (!silent && !stalled) {               /* fully alive */
            if (v->silent_logged) {
                wd_log("pulse_back name=%s", v->name);
                v->silent_logged = 0; v->attempts = 0; v->crash_created = 0;
                v->wait_started = 0; v->softfail_logged = 0;
            }
            if (v->stalled_logged) {             /* счётчик снова пошёл / эпизод закрыт */
                wd_log("progress_back name=%s counter=%lld", v->name, v->last_counter);
                v->stalled_logged = 0; v->stall_restart = 0; v->attempts = 0;
                v->crash_created = 0; v->wait_started = 0; v->softfail_logged = 0;
            }
            continue;
        }
        if (silent) {
            if (!v->silent_logged) {
                wd_log("no_pulse name=%s silence_s=%lld state=%s", v->name, silence / 1000, v->state);
                v->silent_logged = 1;
            }
        } else if (!v->stalled_logged) {
            wd_log("stalled name=%s counter=%lld stalled_s=%lld",
                   v->name, v->last_counter, (now - v->last_progress) / 1000);
            v->stalled_logged = 1;
        }
        if (!v->action_restart) continue;        /* log-only service */

        if (v->crash_created) {                  /* waiting for the supervisor */
            if (access(v->crash_path, F_OK) != 0) {
                /* consumed — supervisor restarts the service. Give its factory a
                 * RELAUNCH WINDOW before the next attempt: a slow factory (e.g.
                 * data_uploader ~34s: discovery timeouts) must not race a fresh
                 * crash file every delay-tick — that unconsumed file then looks
                 * like "supervisor dead" and falsely escalates (found by G4 soak). */
                long gap = delay_for_attempt(v->attempts);
                if (gap < C.relaunch_ms) gap = C.relaunch_ms;
                v->next_attempt = now + gap;
                v->crash_created = 0;
                if (v->stall_restart) {
                    /* stall-рестарт исполнен: новый инстанс сервиса = старый
                     * эпизод/state — призраки погибшего флеш-потока. Полный
                     * сброс; повторный stall возможен только с НОВЫМ флешем
                     * (новый эпизод), это новая проблема, а не «не вылечилось». */
                    v->episode = 0; v->stalled_logged = 0; v->stall_restart = 0;
                    v->attempts = 0; v->softfail_logged = 0;
                    strcpy(v->state, "-");
                }
                continue;
            }
            if (now - v->crash_created > C.consume_timeout_ms) {
                wd_log("escalation name=%s reason=crash_file_not_consumed", v->name);
                unlink(v->crash_path);
                /* observe-гейт (фикс v1.1): process.action=log => эскалация
                 * НЕ трогает процесс (раньше рестартила даже в observe) */
                if (C.proc_action_restart && now >= proc_next_attempt &&
                    resource_gate(now, &proc_wait_started, &proc_last_wait_log, "process"))
                    restart_process(now, "escalation");
                else
                    v->crash_created = 0;        /* retry escalation next round */
            }
            continue;
        }
        /* Митигация «Б»: after_attempts мягких попыток подряд БЕЗ pulse_back =
         * «мягкий рестарт не лечит» (teardown не освободил ресурсы). Дальше —
         * ТОЛЬКО при process.action=restart И глобальном enabled И per-service
         * escalate_to_process (observe не рестартит процесс никогда). */
        if (v->attempts >= C.soft_after) {
            int esc = C.proc_action_restart && C.soft_enabled && v->escalate_to_process;
            if (!v->softfail_logged) {
                wd_log("soft_restart_failed name=%s attempts=%d escalate=%s",
                       v->name, v->attempts, esc ? "yes" : "no");
                v->softfail_logged = 1;
            }
            if (esc) {
                if (now < proc_next_attempt) continue;
                if (!resource_gate(now, &proc_wait_started, &proc_last_wait_log, "process"))
                    continue;
                restart_process(now, "soft_restart_failed");
                return;              /* состояния сброшены — тик заканчиваем */
            }
            /* эскалация не разрешена — продолжаем мягкие попытки по delay-гейту */
        }
        if (now < v->next_attempt) continue;
        if (!resource_gate(now, &v->wait_started, &v->last_wait_log, v->name)) continue;
        int fd = open(v->crash_path, O_WRONLY | O_CREAT, 0644);
        if (fd >= 0) close(fd);
        v->attempts++;
        v->crash_created = now;
        v->stall_restart = stalled;
        v->next_attempt = now + delay_for_attempt(v->attempts);
        wd_log("service_restart name=%s attempt=%d reason=%s silence_s=%lld",
               v->name, v->attempts, stalled ? "stalled" : "no_pulse", silence / 1000);
    }
}

/* ---------------------------------------------------------------- datagram */

static void handle_datagram(int sfd) {
    /* Худшая легальная датаграмма: имя(<=NAME_MAX_LEN-1=47) + " active "(8) +
     * int64-счётчик(<=20 цифр со знаком) = 75Б. 128 = запас ~1.7x. Более
     * длинная датаграмма усекается ядром (SOCK_DGRAM) — хвост отбрасывается,
     * имя перестаёт матчиться и уходит в unknown_name (rate-limited). */
    char buf[128];
    for (;;) {
        ssize_t n = recv(sfd, buf, sizeof buf - 1, MSG_DONTWAIT);
        if (n <= 0) return;
        buf[n] = 0;
        char *sp = strchr(buf, ' ');
        const char *state = NULL, *cnt = NULL;
        if (sp) {
            *sp = 0; state = sp + 1;
            char *sp2 = strchr(sp + 1, ' ');
            if (sp2) { *sp2 = 0; cnt = sp2 + 1; }   /* 3-е поле = L2-счётчик */
        }
        int known = 0;
        for (int i = 0; i < C.nsvc; i++) {
            svc_t *v = &C.svc[i];
            if (strcmp(v->name, buf) != 0) continue;
            known = 1;
            v->last_seen = now_ms();
            if (state && strncmp(v->state, state, sizeof v->state - 1) != 0) {
                wd_log("state name=%s state=%s", v->name, state);
                snprintf(v->state, sizeof v->state, "%s", state);
                v->episode = 0;                  /* смена state закрывает L2-эпизод */
            }
            if (cnt) {                           /* base-пульсы без счётчика эпизод не трогают */
                long long val = strtoll(cnt, NULL, 10);
                if (!v->episode || val != v->last_counter) {
                    v->episode = 1;
                    v->last_counter = val;
                    v->last_progress = v->last_seen;
                }
            }
            break;
        }
        if (!known) {
            static long long last_unknown = 0;   /* rate-limit: лог-флуд от чужих имён */
            long long nw = now_ms();
            if (nw - last_unknown >= C.unknown_every_ms) {
                wd_log("unknown_name name=%.47s", buf);
                last_unknown = nw;
            }
        }
    }
}

/* ---------------------------------------------------------------- main */

int main(int argc, char **argv) {
    const char *conf = argc > 1 ? argv[1] : "/etc/svc_watchdog.conf";
    if (load_config(conf) != 0) return 1;

    signal(SIGPIPE, SIG_IGN);

    /* self-protection from the kernel OOM killer */
    { FILE *f = fopen("/proc/self/oom_score_adj", "w");
      if (f) { fprintf(f, "%d", C.self_oom_adj); fclose(f); } }

    unlink(C.socket_path);
    int sfd = socket(AF_UNIX, SOCK_DGRAM, 0);
    struct sockaddr_un addr; memset(&addr, 0, sizeof addr);
    addr.sun_family = AF_UNIX;
    snprintf(addr.sun_path, sizeof addr.sun_path, "%s", C.socket_path);
    if (bind(sfd, (struct sockaddr *)&addr, sizeof addr) != 0) {
        fprintf(stderr, "svc_watchdog: bind(%s): %s\n", C.socket_path, strerror(errno));
        return 1;
    }
    chmod(C.socket_path, 0666);

    int tfd = timerfd_create(CLOCK_MONOTONIC, 0);
    struct itimerspec its = {
        .it_interval = { C.tick_ms / 1000, (C.tick_ms % 1000) * 1000000L },
        .it_value    = { C.tick_ms / 1000, (C.tick_ms % 1000) * 1000000L },
    };
    timerfd_settime(tfd, 0, &its, NULL);

    int ep = epoll_create1(0);
    struct epoll_event ev = { .events = EPOLLIN, .data.fd = sfd };
    epoll_ctl(ep, EPOLL_CTL_ADD, sfd, &ev);
    ev.data.fd = tfd;
    epoll_ctl(ep, EPOLL_CTL_ADD, tfd, &ev);

    grace_until = now_ms() + C.start_grace_ms;   /* fresh start = grace for everyone */
    wd_log("wd_start services=%d socket=%s mode=%s grace_ms=%ld",
           C.nsvc, C.socket_path, C.proc_action_restart ? "enforce" : "observe",
           C.start_grace_ms);

    struct epoll_event events[4];
    for (;;) {
        int n = epoll_wait(ep, events, 4, -1);
        for (int i = 0; i < n; i++) {
            if (events[i].data.fd == sfd) handle_datagram(sfd);
            else if (events[i].data.fd == tfd) {
                unsigned long long exp; ssize_t r = read(tfd, &exp, sizeof exp); (void)r;
                handle_tick();
            }
        }
    }
}
