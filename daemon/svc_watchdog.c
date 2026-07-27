/* svc_watchdog v2 — observer daemon (watchdog v2, svc_watch.conf schema).
 *
 * Same config as the py library (config.py), but with ITS OWN strict validator:
 *   - every readable key is mandatory, ALL problems reported at once, fail-closed;
 *   - UNKNOWN KEY = rejection (protection against typos);
 *   - type/mode/from/on_exceeded/grace/pacing dictionaries; @-references are resolved;
 *   - collection capacities (compile-time defines) — overflow = rejection, not truncation.
 *
 * Topology: processes ⊃ services ⊃ {signals, watch}. Levels L1/L2 (service),
 * P1/P2 (process). Ladders from the `ladders` catalog: a `@action` step (request_file)
 * or the built-in `restart_process` verb (targets the OWNER process). Terminal
 * actions are bounded by rate_limit → cooldown+alarm. observer.mode is the main
 * safety switch (act|log), watch.*.mode is the pinpoint one.
 *
 * Single thread, epoll(socket + tick timerfd). All waiting is STATE, not sleep.
 * Test hooks: env WD_PROC_MEMINFO / WD_PROC_LOADAVG (substitutes for /proc).
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

/* ── collection capacities (kept in sync with lib_py/svc_watch/config.py) ── */
#define MAX_PROCESSES 8
#define MAX_SERVICES 30
#define MAX_ACTIONS 16
#define MAX_LADDERS 16
#define MAX_LADDER_STEPS 8
#define MAX_FIRES 32                /* rate_limit window */
#define NAME_MAX_LEN 48
#define STATE_MAX_LEN 24
#define PATH_MAX_LEN 256

#define ACT 1
#define LOG 0

static int cfg_bad;

/* ── model ── */
typedef struct { int max; long per_ms; int on_exceeded; long cooldown_ms; } rl_t; /* on_exceeded: 0 cooldown, 1 stop */

typedef struct {
    char name[NAME_MAX_LEN];
    char file_tmpl[PATH_MAX_LEN];   /* request_file: template with {service} */
    long eat_within_ms, startup_ms;
    rl_t rl;
} action_t;

typedef struct { int is_verb; int action_idx; long tries; } step_t; /* verb → restart_process */
typedef struct { char name[NAME_MAX_LEN]; step_t steps[MAX_LADDER_STEPS]; int nsteps; } ladder_t;

typedef struct {
    int step, tries_used;
    long long last_action, firing_since, cooldown_until;
    long long fires[MAX_FIRES]; int nfires;
    long long wait_started, last_wait_log;
} lstate_t;

typedef struct {
    char name[NAME_MAX_LEN];
    long every_ms;
    int l1; int l1_mode; long l1_dead_ms; int l1_ladder;
    int l2; int l2_mode; long l2_frozen_ms; int l2_ladder;
    long activity_tick_ms;          /* 0 = none */
    /* runtime */
    long long last_seen; char state[STATE_MAX_LEN];
    int episode; long long last_counter, last_progress;
    long long next_judge;           /* suppression window (startup/relaunch) */
    long long crash_created; char crash_path[PATH_MAX_LEN]; long crash_eat_ms; int crash_stall;
    int silent_logged, stalled_logged;
    lstate_t l1_st, l2_st;
} svc_t;

typedef struct {
    char name[NAME_MAX_LEN];
    int launch_type;                /* 0 init_script, 2 external */
    char script[PATH_MAX_LEN], pidfile[PATH_MAX_LEN];
    long grace_ms;
    rl_t restart_rl;
    int p1; int p1_mode; int p1_ladder;
    int p2; int p2_mode; int p2_ladder;
    svc_t svc[MAX_SERVICES]; int nsvc;
    /* runtime */
    long long grace_until;
    lstate_t p1_st, p2_st;
    int all_silent_logged, p2_logged;
} proc_t;

typedef struct {
    int schema;
    char socket_path[PATH_MAX_LEN];
    int mode;                       /* observer.mode: act|log */
    long tick_ms;
    char pause_file[PATH_MAX_LEN];
    int oom_adj;
    char log_file[PATH_MAX_LEN]; long log_rotate_bytes; int log_keep, log_fsync;
    long quiet_paused_ms, quiet_unknown_ms;
    double min_free_mb, max_load1, alarm_mb;
    long recheck_ms, force_after_ms;
    int pacing_fixed; long pacing_delay_ms, pacing_start_ms, pacing_cap_ms; double pacing_factor;
    action_t actions[MAX_ACTIONS]; int naction;
    ladder_t ladders[MAX_LADDERS]; int nladder;
    proc_t procs[MAX_PROCESSES]; int nproc;
} cfg_t;

static cfg_t C;
static int log_fd = -1;
static long long paused_last_log = 0;
static int below_alarm = 0;
static long long last_unknown = 0;

/* ---------------------------------------------------------------- time/log */
static long long now_ms(void) {
    struct timespec ts; clock_gettime(CLOCK_MONOTONIC, &ts);
    return (long long)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}

static void rotate_if_needed(void) {
    struct stat st;
    if (log_fd < 0 || fstat(log_fd, &st) != 0 || st.st_size < C.log_rotate_bytes) return;
    close(log_fd); log_fd = -1;
    char from[PATH_MAX_LEN + 16], to[PATH_MAX_LEN + 16];
    for (int i = C.log_keep - 1; i >= 1; i--) {
        snprintf(from, sizeof from, "%s.%d", C.log_file, i);
        snprintf(to, sizeof to, "%s.%d", C.log_file, i + 1);
        rename(from, to);
    }
    snprintf(to, sizeof to, "%s.1", C.log_file);
    rename(C.log_file, to);
}

static void wd_log(const char *fmt, ...) {
    if (log_fd < 0) {
        log_fd = open(C.log_file, O_WRONLY | O_APPEND | O_CREAT, 0644);
        if (log_fd < 0) return;
    }
    char line[512]; time_t t = time(NULL); struct tm tm; localtime_r(&t, &tm);
    int n = (int)strftime(line, sizeof line, "%Y-%m-%dT%H:%M:%S ", &tm);
    n += snprintf(line + n, sizeof line - n, "up=%lld ", now_ms() / 1000);
    va_list ap; va_start(ap, fmt);
    n += vsnprintf(line + n, sizeof line - n - 2, fmt, ap);
    va_end(ap);
    line[n++] = '\n';
    if (write(log_fd, line, n) < 0) { /* ignore */ }
    if (C.log_fsync) fsync(log_fd);
    rotate_if_needed();
}

/* ---------------------------------------------------------------- validator */
static json_object *get_opt(json_object *o, const char *k) {
    json_object *v;
    if (o && json_object_object_get_ex(o, k, &v)) return v;
    return NULL;
}
static json_object *req_obj(json_object *o, const char *where, const char *k) {
    json_object *v = get_opt(o, k);
    if (!o) return NULL;
    if (!v) { fprintf(stderr, "svc_watchdog: missing key %s%s\n", where, k); cfg_bad = 1; }
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
static void req_strcpy(json_object *o, const char *where, const char *k, char *dst, size_t cap) {
    const char *val = req_str(o, where, k);
    size_t len = strlen(val);
    if (len >= cap) {
        fprintf(stderr, "svc_watchdog: %s%s too long (%zu, max %zu)\n", where, k, len, cap - 1);
        cfg_bad = 1; len = cap - 1;
    }
    memcpy(dst, val, len); dst[len] = 0;
}
/* UNKNOWN KEY = rejection */
static void check_keys(json_object *o, const char *where, const char *const *allowed, int n) {
    if (!o || json_object_get_type(o) != json_type_object) return;
    json_object_object_foreach(o, key, val) {
        (void)val; int ok = 0;
        for (int i = 0; i < n; i++) if (!strcmp(key, allowed[i])) { ok = 1; break; }
        if (!ok) { fprintf(stderr, "svc_watchdog: unknown key %s%s\n", where, key); cfg_bad = 1; }
    }
}
static int enum_idx(const char *val, const char *const *opts, int n) {
    for (int i = 0; i < n; i++) if (!strcmp(val, opts[i])) return i;
    return -1;
}
static int req_mode(json_object *o, const char *where) {
    const char *m = req_str(o, where, "mode");
    if (!strcmp(m, "act")) return ACT;
    if (!strcmp(m, "log")) return LOG;
    if (m[0]) { fprintf(stderr, "svc_watchdog: %smode must be act|log\n", where); cfg_bad = 1; }
    return LOG;
}

static void parse_rate_limit(json_object *o, const char *where, rl_t *rl, int allow_stop) {
    static const char *ALL[] = {"max", "per_ms", "on_exceeded", "cooldown_ms"};
    check_keys(o, where, ALL, 4);
    rl->max = (int)req_num(o, where, "max");
    rl->per_ms = (long)req_num(o, where, "per_ms");
    const char *oe = req_str(o, where, "on_exceeded");
    rl->on_exceeded = 0;
    if (!strcmp(oe, "cooldown")) rl->on_exceeded = 0;
    else if (!strcmp(oe, "stop")) {
        rl->on_exceeded = 1;
        if (!allow_stop) {
            fprintf(stderr, "svc_watchdog: %son_exceeded=stop forbidden on process restart\n", where);
            cfg_bad = 1;
        }
    } else if (oe[0]) {
        fprintf(stderr, "svc_watchdog: %son_exceeded must be cooldown|stop\n", where); cfg_bad = 1;
    }
    rl->cooldown_ms = 0;
    if (rl->on_exceeded == 0) rl->cooldown_ms = (long)req_num(o, where, "cooldown_ms");
    if (rl->max >= 1 && rl->per_ms >= 1 && rl->on_exceeded == 0
        && rl->cooldown_ms <= rl->per_ms / rl->max) {
        fprintf(stderr, "svc_watchdog: %scooldown_ms must be > per_ms/max\n", where); cfg_bad = 1;
    }
    if (rl->max > MAX_FIRES) {            /* otherwise the fires[] window overflows and the limit is inert */
        fprintf(stderr, "svc_watchdog: %smax %d exceeds MAX_FIRES=%d\n", where, rl->max, MAX_FIRES);
        cfg_bad = 1;
    }
}

static int find_action(const char *name) {
    for (int i = 0; i < C.naction; i++) if (!strcmp(C.actions[i].name, name)) return i;
    return -1;
}
static int find_ladder(const char *name) {
    for (int i = 0; i < C.nladder; i++) if (!strcmp(C.ladders[i].name, name)) return i;
    return -1;
}

/* service watch level: key = level name from a closed set */
static void parse_service_level(json_object *o, const char *where, const char *lname, svc_t *v) {
    int is_l1 = !strcmp(lname, "L1_pulse_lost");
    int is_l2 = !strcmp(lname, "L2_activity_frozen");
    if (!is_l1 && !is_l2) {
        fprintf(stderr, "svc_watchdog: %s unknown service watch level %s\n", where, lname);
        cfg_bad = 1; return;
    }
    static const char *L1K[] = {"mode", "ladder", "dead_after_ms"};
    static const char *L2K[] = {"mode", "ladder", "frozen_after_ms"};
    check_keys(o, where, is_l1 ? L1K : L2K, 3);
    int mode = req_mode(o, where);
    const char *lad = req_str(o, where, "ladder");
    int lidx = -1;
    if (lad[0] == '@') { lidx = find_ladder(lad + 1);
        if (lidx < 0) { fprintf(stderr, "svc_watchdog: %sladder %s not found\n", where, lad); cfg_bad = 1; } }
    else if (lad[0]) { fprintf(stderr, "svc_watchdog: %sladder must be @name\n", where); cfg_bad = 1; }
    if (is_l1) {
        v->l1 = 1; v->l1_mode = mode; v->l1_ladder = lidx;
        v->l1_dead_ms = (long)req_num(o, where, "dead_after_ms");
        if (v->l1_dead_ms < 3 * v->every_ms) {
            fprintf(stderr, "svc_watchdog: %sdead_after_ms < 3*every_ms\n", where); cfg_bad = 1; }
    } else {
        v->l2 = 1; v->l2_mode = mode; v->l2_ladder = lidx;
        v->l2_frozen_ms = (long)req_num(o, where, "frozen_after_ms");
    }
}

static void parse_service(json_object *s, const char *where, svc_t *v) {
    static const char *SK[] = {"start", "port", "signals", "watch", "enabled"};
    check_keys(s, where, SK, 5);   /* enabled: per-service toggle (skipped before parse when false) */
    memset(v, 0, sizeof *v);
    /* the service name comes from the map key (set by the caller) */
    /* start */
    json_object *st = req_obj(s, where, "start");
    static const char *STK[] = {"type", "entry"};
    check_keys(st, where, STK, 2);
    const char *stype = req_str(st, where, "type");
    static const char *STARTT[] = {"python", "inmemory"};
    if (enum_idx(stype, STARTT, 2) < 0 && stype[0]) {
        fprintf(stderr, "svc_watchdog: %sstart.type unknown %s\n", where, stype); cfg_bad = 1; }
    /* signals */
    json_object *sig = req_obj(s, where, "signals");
    static const char *SIGK[] = {"pulse", "activity"};
    check_keys(sig, where, SIGK, 2);
    json_object *pulse = req_obj(sig, where, "pulse");
    static const char *PK[] = {"from", "every_ms", "probe"};
    check_keys(pulse, where, PK, 3);
    const char *from = req_str(pulse, where, "from");
    v->every_ms = (long)req_num(pulse, where, "every_ms");
    json_object *probe = get_opt(pulse, "probe");
    if (!strcmp(from, "loop")) {
        if (probe) { fprintf(stderr, "svc_watchdog: %spulse from:loop must not carry probe\n", where); cfg_bad = 1; }
    } else if (!strcmp(from, "probe")) {
        if (!probe) { fprintf(stderr, "svc_watchdog: %spulse from:probe requires probe\n", where); cfg_bad = 1; }
    } else if (from[0]) { fprintf(stderr, "svc_watchdog: %spulse.from must be loop|probe\n", where); cfg_bad = 1; }
    json_object *act = get_opt(sig, "activity");
    if (act) {
        static const char *AK[] = {"tick_ms"};
        check_keys(act, where, AK, 1);
        v->activity_tick_ms = (long)req_num(act, where, "tick_ms");
    }
    /* watch */
    json_object *watch = req_obj(s, where, "watch");
    if (watch && json_object_get_type(watch) == json_type_object) {
        json_object_object_foreach(watch, lname, lobj) {
            char w2[96]; snprintf(w2, sizeof w2, "%s%s.", where, lname);
            parse_service_level(lobj, w2, lname, v);
        }
    }
    /* pairing (rule 5, C-readable): pulse ⇒ L1; activity ⇔ L2 */
    if (!v->l1) { fprintf(stderr, "svc_watchdog: %smissing watch.L1_pulse_lost\n", where); cfg_bad = 1; }
    if ((v->activity_tick_ms > 0) != (v->l2 != 0)) {
        fprintf(stderr, "svc_watchdog: %sactivity <-> L2_activity_frozen must pair\n", where); cfg_bad = 1; }
    if (v->l2 && v->activity_tick_ms > 0 && v->l2_frozen_ms < 20 * v->activity_tick_ms) {
        fprintf(stderr, "svc_watchdog: %sfrozen_after_ms < 20*tick_ms\n", where); cfg_bad = 1; }
    strcpy(v->state, "-");
}

static void parse_process_level(json_object *o, const char *where, const char *lname, proc_t *p) {
    int is_p1 = !strcmp(lname, "P1_all_pulses_lost");
    int is_p2 = !strcmp(lname, "P2_request_stuck");
    if (!is_p1 && !is_p2) {
        fprintf(stderr, "svc_watchdog: %s unknown process watch level %s\n", where, lname);
        cfg_bad = 1; return;
    }
    static const char *PK[] = {"mode", "ladder"};
    check_keys(o, where, PK, 2);
    int mode = req_mode(o, where);
    const char *lad = req_str(o, where, "ladder");
    int lidx = -1;
    if (lad[0] == '@') { lidx = find_ladder(lad + 1);
        if (lidx < 0) { fprintf(stderr, "svc_watchdog: %sladder %s not found\n", where, lad); cfg_bad = 1; } }
    else if (lad[0]) { fprintf(stderr, "svc_watchdog: %sladder must be @name\n", where); cfg_bad = 1; }
    /* P-level: the ladder must consist of verbs only (no {service} target) */
    if (lidx >= 0)
        for (int i = 0; i < C.ladders[lidx].nsteps; i++)
            if (!C.ladders[lidx].steps[i].is_verb) {
                fprintf(stderr, "svc_watchdog: %sprocess level ladder must be verb-only\n", where);
                cfg_bad = 1; break;
            }
    if (is_p1) { p->p1 = 1; p->p1_mode = mode; p->p1_ladder = lidx; }
    else { p->p2 = 1; p->p2_mode = mode; p->p2_ladder = lidx; }
}

static void parse_process(json_object *po, const char *pname, proc_t *p) {
    char where[64]; snprintf(where, sizeof where, "processes.%s.", pname);
    static const char *PPK[] = {"launch", "supervisor", "watch", "services"};
    check_keys(po, where, PPK, 4);
    memset(p, 0, sizeof *p);
    snprintf(p->name, sizeof p->name, "%s", pname);

    /* launch */
    json_object *L = req_obj(po, where, "launch");
    static const char *LK[] = {"type", "script", "pidfile", "grace", "restart_rate_limit"};
    check_keys(L, where, LK, 5);
    const char *lt = req_str(L, where, "type");
    if (!strcmp(lt, "init_script")) p->launch_type = 0;
    else if (!strcmp(lt, "external")) p->launch_type = 2;
    else if (!strcmp(lt, "inmemory")) p->launch_type = 1;
    else if (lt[0]) { fprintf(stderr, "svc_watchdog: %slaunch.type unknown %s\n", where, lt); cfg_bad = 1; }
    if (p->launch_type == 0) {
        req_strcpy(L, where, "script", p->script, sizeof p->script);
        req_strcpy(L, where, "pidfile", p->pidfile, sizeof p->pidfile);
    }
    json_object *g = req_obj(L, where, "grace");
    static const char *GK[] = {"type", "ms", "max_ms"};
    check_keys(g, where, GK, 3);
    const char *gt = req_str(g, where, "type");
    if (!strcmp(gt, "fixed")) p->grace_ms = (long)req_num(g, where, "ms");
    else if (!strcmp(gt, "until_ready")) {
        fprintf(stderr, "svc_watchdog: %sgrace.until_ready needs ready-signal (unsupported)\n", where);
        cfg_bad = 1;
    } else if (gt[0]) { fprintf(stderr, "svc_watchdog: %sgrace.type must be fixed|until_ready\n", where); cfg_bad = 1; }
    json_object *rrl = req_obj(L, where, "restart_rate_limit");
    char w2[96]; snprintf(w2, sizeof w2, "%srestart_rate_limit.", where);
    parse_rate_limit(rrl, w2, &p->restart_rl, 0);   /* stop is forbidden */

    /* supervisor — py side; C only validates presence/keys */
    json_object *sup = req_obj(po, where, "supervisor");
    static const char *SUK[] = {"poll_ms", "stop_timeout_ms", "min_stable_ms",
                                "max_consecutive_start_failures", "backoff", "log"};
    check_keys(sup, where, SUK, 6);

    /* watch (P-levels) */
    json_object *watch = get_opt(po, "watch");
    if (watch && json_object_get_type(watch) == json_type_object) {
        json_object_object_foreach(watch, lname, lobj) {
            char w3[96]; snprintf(w3, sizeof w3, "%swatch.%s.", where, lname);
            parse_process_level(lobj, w3, lname, p);
        }
    }

    /* services */
    json_object *svcs = req_obj(po, where, "services");
    if (svcs && json_object_get_type(svcs) == json_type_object) {
        int cnt = 0;
        json_object_object_foreach(svcs, sname, sobj) {
            if (cnt >= MAX_SERVICES) {
                fprintf(stderr, "svc_watchdog: %sservices exceed MAX_SERVICES=%d\n", where, MAX_SERVICES);
                cfg_bad = 1; break;
            }
            char w3[96]; snprintf(w3, sizeof w3, "%sservices.%s.", where, sname);
            /* config-native per-service toggle: enabled=false → not started (Python) and NOT watched
               here (skip entirely so the observer never expects a pulse from it). Default = on. */
            json_object *en = get_opt(sobj, "enabled");
            if (en) {
                if (!json_object_is_type(en, json_type_boolean)) {
                    fprintf(stderr, "svc_watchdog: %sservices.%s.enabled must be bool\n", where, sname);
                    cfg_bad = 1;
                } else if (!json_object_get_boolean(en)) {
                    continue;   /* disabled: not added to the watch set */
                }
            }
            svc_t *v = &p->svc[cnt];
            parse_service(sobj, w3, v);
            snprintf(v->name, sizeof v->name, "%s", sname);
            cnt++;
        }
        p->nsvc = cnt;
    }
    if (p->nsvc < 1) { fprintf(stderr, "svc_watchdog: %sservices must be non-empty\n", where); cfg_bad = 1; }
    if (p->p1 && p->p1_mode == ACT && p->nsvc < 2)
        wd_log("warn process=%s P1 inert (single service)", p->name);   /* rule 11: not fatal */
}

static int load_config(const char *path) {
    json_object *root = json_object_from_file(path);
    if (!root) { fprintf(stderr, "svc_watchdog: cannot parse %s\n", path); return -1; }
    cfg_bad = 0;
    static const char *ROOTK[] = {"schema", "framework", "actions", "ladders", "processes"};
    check_keys(root, "", ROOTK, 5);

    C.schema = (int)req_num(root, "", "schema");
    if (C.schema != 2) { fprintf(stderr, "svc_watchdog: schema must be 2\n"); cfg_bad = 1; }

    /* framework */
    json_object *fw = req_obj(root, "", "framework");
    static const char *FWK[] = {"transport", "observer", "gates", "pacing"};
    check_keys(fw, "framework.", FWK, 4);

    json_object *tr = req_obj(fw, "framework.", "transport");
    static const char *TRK[] = {"type", "unix_datagram", "inmemory"};
    check_keys(tr, "framework.transport.", TRK, 3);
    const char *trt = req_str(tr, "framework.transport.", "type");
    if (!strcmp(trt, "unix_datagram")) {
        json_object *ud = req_obj(tr, "framework.transport.", "unix_datagram");
        static const char *UDK[] = {"socket", "format"};
        check_keys(ud, "framework.transport.unix_datagram.", UDK, 2);
        req_strcpy(ud, "framework.transport.unix_datagram.", "socket", C.socket_path, sizeof C.socket_path);
        const char *fmt = req_str(ud, "framework.transport.unix_datagram.", "format");
        if (strcmp(fmt, "text_v1")) { fprintf(stderr, "svc_watchdog: transport.format must be text_v1\n"); cfg_bad = 1; }
    } else if (trt[0]) { fprintf(stderr, "svc_watchdog: transport.type unknown %s\n", trt); cfg_bad = 1; }

    json_object *ob = req_obj(fw, "framework.", "observer");
    static const char *OBK[] = {"mode", "tick_ms", "pause_file", "oom_adj", "log", "quiet", "enabled"};
    check_keys(ob, "framework.observer.", OBK, 7);   /* enabled: startup.sh gates the daemon; C just accepts the key */
    C.mode = req_mode(ob, "framework.observer.");
    C.tick_ms = (long)req_num(ob, "framework.observer.", "tick_ms");
    req_strcpy(ob, "framework.observer.", "pause_file", C.pause_file, sizeof C.pause_file);
    C.oom_adj = (int)req_num(ob, "framework.observer.", "oom_adj");
    json_object *lg = req_obj(ob, "framework.observer.", "log");
    static const char *LGK[] = {"file", "rotate_kb", "keep", "fsync"};
    check_keys(lg, "framework.observer.log.", LGK, 4);
    req_strcpy(lg, "framework.observer.log.", "file", C.log_file, sizeof C.log_file);
    C.log_rotate_bytes = (long)req_num(lg, "framework.observer.log.", "rotate_kb") * 1024;
    C.log_keep = (int)req_num(lg, "framework.observer.log.", "keep");
    C.log_fsync = json_object_get_boolean(req_obj(lg, "framework.observer.log.", "fsync"));
    json_object *qt = req_obj(ob, "framework.observer.", "quiet");
    static const char *QK[] = {"paused_ms", "unknown_ms"};
    check_keys(qt, "framework.observer.quiet.", QK, 2);
    C.quiet_paused_ms = (long)req_num(qt, "framework.observer.quiet.", "paused_ms");
    C.quiet_unknown_ms = (long)req_num(qt, "framework.observer.quiet.", "unknown_ms");

    json_object *ga = req_obj(fw, "framework.", "gates");
    static const char *GAK[] = {"min_free_mb", "max_load1", "recheck_ms", "force_after_ms", "alarm_mb"};
    check_keys(ga, "framework.gates.", GAK, 5);
    C.min_free_mb = req_num(ga, "framework.gates.", "min_free_mb");
    C.max_load1 = req_num(ga, "framework.gates.", "max_load1");
    C.recheck_ms = (long)req_num(ga, "framework.gates.", "recheck_ms");
    C.force_after_ms = (long)req_num(ga, "framework.gates.", "force_after_ms");
    C.alarm_mb = req_num(ga, "framework.gates.", "alarm_mb");
    if (ga && C.min_free_mb <= C.alarm_mb) { fprintf(stderr, "svc_watchdog: min_free_mb must be > alarm_mb\n"); cfg_bad = 1; }

    json_object *pc = req_obj(fw, "framework.", "pacing");
    static const char *PCF[] = {"type", "delay_ms"};
    static const char *PCB[] = {"type", "start_ms", "factor", "cap_ms"};
    const char *pct = req_str(pc, "framework.pacing.", "type");
    if (!strcmp(pct, "fixed")) {
        check_keys(pc, "framework.pacing.", PCF, 2);
        C.pacing_fixed = 1;
        C.pacing_delay_ms = (long)req_num(pc, "framework.pacing.", "delay_ms");
    } else if (!strcmp(pct, "backoff")) {
        check_keys(pc, "framework.pacing.", PCB, 4);
        C.pacing_fixed = 0;
        C.pacing_start_ms = (long)req_num(pc, "framework.pacing.", "start_ms");
        C.pacing_factor = req_num(pc, "framework.pacing.", "factor");
        C.pacing_cap_ms = (long)req_num(pc, "framework.pacing.", "cap_ms");
    } else if (pct[0]) { fprintf(stderr, "svc_watchdog: pacing.type must be fixed|backoff\n"); cfg_bad = 1; }

    /* actions */
    json_object *acts = req_obj(root, "", "actions");
    C.naction = 0;
    if (acts && json_object_get_type(acts) == json_type_object) {
        json_object_object_foreach(acts, aname, aobj) {
            if (C.naction >= MAX_ACTIONS) { fprintf(stderr, "svc_watchdog: actions exceed MAX_ACTIONS\n"); cfg_bad = 1; break; }
            action_t *a = &C.actions[C.naction];
            memset(a, 0, sizeof *a);
            snprintf(a->name, sizeof a->name, "%s", aname);
            char w[64]; snprintf(w, sizeof w, "actions.%s.", aname);
            const char *at = req_str(aobj, w, "type");
            if (!strcmp(at, "request_file")) {
                static const char *AK[] = {"type", "file", "eat_within_ms", "startup_ms", "rate_limit"};
                check_keys(aobj, w, AK, 5);
                req_strcpy(aobj, w, "file", a->file_tmpl, sizeof a->file_tmpl);
                if (!strstr(a->file_tmpl, "{service}")) { fprintf(stderr, "svc_watchdog: %sfile needs {service}\n", w); cfg_bad = 1; }
                a->eat_within_ms = (long)req_num(aobj, w, "eat_within_ms");
                a->startup_ms = (long)req_num(aobj, w, "startup_ms");
            } else if (at[0] && strcmp(at, "inmemory")) {
                fprintf(stderr, "svc_watchdog: %stype unknown %s\n", w, at); cfg_bad = 1;
            }
            json_object *rl = req_obj(aobj, w, "rate_limit");
            char w2[96]; snprintf(w2, sizeof w2, "%srate_limit.", w);
            parse_rate_limit(rl, w2, &a->rl, 1);
            C.naction++;
        }
    }

    /* ladders (after actions: resolving @actions) */
    json_object *lads = req_obj(root, "", "ladders");
    C.nladder = 0;
    if (lads && json_object_get_type(lads) == json_type_object) {
        json_object_object_foreach(lads, lname, larr) {
            if (C.nladder >= MAX_LADDERS) { fprintf(stderr, "svc_watchdog: ladders exceed MAX_LADDERS\n"); cfg_bad = 1; break; }
            ladder_t *L = &C.ladders[C.nladder];
            memset(L, 0, sizeof *L);
            snprintf(L->name, sizeof L->name, "%s", lname);
            if (json_object_get_type(larr) != json_type_array || json_object_array_length(larr) == 0) {
                fprintf(stderr, "svc_watchdog: ladders.%s must be non-empty array\n", lname); cfg_bad = 1; C.nladder++; continue;
            }
            size_t n = json_object_array_length(larr);
            if (n > MAX_LADDER_STEPS) { fprintf(stderr, "svc_watchdog: ladders.%s exceeds MAX_LADDER_STEPS\n", lname); cfg_bad = 1; n = MAX_LADDER_STEPS; }
            for (size_t i = 0; i < n; i++) {
                json_object *so = json_object_array_get_idx(larr, i);
                static const char *SK[] = {"do", "tries"};
                check_keys(so, "ladders.step.", SK, 2);
                const char *dostr = req_str(so, "ladders.step.", "do");
                step_t *stp = &L->steps[L->nsteps];
                stp->is_verb = 0; stp->action_idx = -1; stp->tries = -1;
                if (dostr[0] == '@') {
                    stp->action_idx = find_action(dostr + 1);
                    if (stp->action_idx < 0) { fprintf(stderr, "svc_watchdog: ladders.%s: @%s not in actions\n", lname, dostr + 1); cfg_bad = 1; }
                } else if (!strcmp(dostr, "restart_process")) {
                    stp->is_verb = 1;
                } else if (dostr[0]) {
                    fprintf(stderr, "svc_watchdog: ladders.%s: do %s not @action nor verb\n", lname, dostr); cfg_bad = 1;
                }
                json_object *tr = get_opt(so, "tries");
                if (tr) {
                    stp->tries = (long)json_object_get_int64(tr);
                    if (stp->tries < 1) { fprintf(stderr, "svc_watchdog: ladders.%s: tries must be >=1\n", lname); cfg_bad = 1; }
                    if (i + 1 == n) { /* last step may keep tries; fine */ }
                } else if (i + 1 != n) {
                    fprintf(stderr, "svc_watchdog: ladders.%s: non-last step without tries (step unreachable)\n", lname); cfg_bad = 1;
                }
                L->nsteps++;
            }
            C.nladder++;
        }
    }

    /* processes (after ladders: resolving @ladders) */
    json_object *procs = req_obj(root, "", "processes");
    C.nproc = 0;
    if (procs && json_object_get_type(procs) == json_type_object) {
        json_object_object_foreach(procs, pname, pobj) {
            if (C.nproc >= MAX_PROCESSES) { fprintf(stderr, "svc_watchdog: processes exceed MAX_PROCESSES\n"); cfg_bad = 1; break; }
            parse_process(pobj, pname, &C.procs[C.nproc]);
            C.nproc++;
        }
    }

    /* rule 7: global uniqueness of service names */
    for (int a = 0; a < C.nproc; a++)
        for (int i = 0; i < C.procs[a].nsvc; i++)
            for (int b = 0; b < C.nproc; b++)
                for (int j = 0; j < C.procs[b].nsvc; j++)
                    if (!(a == b && i == j) && !strcmp(C.procs[a].svc[i].name, C.procs[b].svc[j].name)
                        && (a < b || (a == b && i < j))) {
                        fprintf(stderr, "svc_watchdog: duplicate service name %s across processes\n", C.procs[a].svc[i].name);
                        cfg_bad = 1;
                    }

    json_object_put(root);
    if (cfg_bad) { fprintf(stderr, "svc_watchdog: config %s rejected (fail-closed)\n", path); return -1; }
    return 0;
}

/* ---------------------------------------------------------------- resources */
static double mem_available_mb(void) {
    const char *p = getenv("WD_PROC_MEMINFO"); if (!p) p = "/proc/meminfo";
    FILE *f = fopen(p, "r"); if (!f) return 1e9;
    char k[64]; double v = 1e9; char line[128];
    while (fgets(line, sizeof line, f))
        if (sscanf(line, "%63s %lf", k, &v) == 2 && !strcmp(k, "MemAvailable:")) break;
    fclose(f); return v / 1024.0;
}
static double load1(void) {
    const char *p = getenv("WD_PROC_LOADAVG"); if (!p) p = "/proc/loadavg";
    FILE *f = fopen(p, "r"); if (!f) return 0;
    double v = 0; if (fscanf(f, "%lf", &v) != 1) v = 0; fclose(f); return v;
}
static int resources_healthy(double *mem, double *load) {
    *mem = mem_available_mb(); *load = load1();
    return *mem >= C.min_free_mb && *load <= C.max_load1;
}
static long long process_age_ms(const char *pidfile) {
    if (!pidfile[0]) return -1;
    FILE *f = fopen(pidfile, "r"); if (!f) return -1;
    long pid = 0; int ok = fscanf(f, "%ld", &pid) == 1; fclose(f);
    if (!ok || pid <= 0) return -1;
    char p[64]; snprintf(p, sizeof p, "/proc/%ld/stat", pid);
    f = fopen(p, "r"); if (!f) return -1;
    char buf[512]; size_t n = fread(buf, 1, sizeof buf - 1, f); fclose(f); buf[n] = 0;
    char *rp = strrchr(buf, ')'); if (!rp) return -1;
    unsigned long long starttime = 0; int field = 2;
    for (char *tok = strtok(rp + 1, " "); tok; tok = strtok(NULL, " ")) {
        field++; if (field == 22) { starttime = strtoull(tok, NULL, 10); break; }
    }
    if (!starttime) return -1;
    f = fopen("/proc/uptime", "r"); if (!f) return -1;
    double up = 0; ok = fscanf(f, "%lf", &up) == 1; fclose(f); if (!ok) return -1;
    long hz = sysconf(_SC_CLK_TCK); if (hz <= 0) hz = 100;
    double age_s = up - (double)starttime / hz;
    return age_s < 0 ? -1 : (long long)(age_s * 1000.0);
}
static int resource_gate(long long now, lstate_t *ls, const char *target) {
    double mem, load;
    if (resources_healthy(&mem, &load)) { ls->wait_started = 0; return 1; }
    if (!ls->wait_started) { ls->wait_started = now; ls->last_wait_log = 0; }
    if (now - ls->wait_started >= C.force_after_ms) {
        wd_log("waiting_resources_expired target=%s mem_mb=%.1f load1=%.2f", target, mem, load);
        ls->wait_started = 0; return 1;
    }
    if (now - ls->last_wait_log >= C.recheck_ms) {
        wd_log("waiting_resources target=%s mem_mb=%.1f load1=%.2f", target, mem, load);
        ls->last_wait_log = now;
    }
    return 0;
}
static long pacing_delay(int attempt) {
    if (C.pacing_fixed) return C.pacing_delay_ms;
    double d = C.pacing_start_ms;
    for (int i = 1; i < attempt; i++) { d *= C.pacing_factor; if (d >= C.pacing_cap_ms) break; }
    return d > C.pacing_cap_ms ? C.pacing_cap_ms : (long)d;
}

/* ---------------------------------------------------------------- actions */
static void reset_service_state(svc_t *v) {
    v->last_seen = 0; v->silent_logged = 0; v->stalled_logged = 0;
    v->crash_created = 0; v->crash_stall = 0; v->next_judge = 0;
    v->episode = 0; v->last_counter = 0; v->last_progress = 0;
    memset(&v->l1_st, 0, sizeof v->l1_st); memset(&v->l2_st, 0, sizeof v->l2_st);
    strcpy(v->state, "-");
}
static void do_restart_process(proc_t *p, long long now, const char *reason) {
    wd_log("restart_process process=%s reason=%s script=%s", p->name, reason, p->script);
    if (p->launch_type == 0 && p->script[0]) {
        /* fire-and-forget: we do NOT wait for the script (SIGCHLD=SIG_IGN auto-reaps it).
         * Waiting = state (grace_until covers the restart window); a hung init script does
         * NOT freeze the observer's single thread (review E6). */
        pid_t pid = fork();
        if (pid == 0) { execl("/bin/sh", "sh", p->script, "restart", (char *)NULL); _exit(127); }
    }
    p->grace_until = now + p->grace_ms;
    p->all_silent_logged = 0; p->p2_logged = 0;
    memset(&p->p1_st, 0, sizeof p->p1_st); memset(&p->p2_st, 0, sizeof p->p2_st);
    for (int i = 0; i < p->nsvc; i++) reset_service_state(&p->svc[i]);
}

/* one ladder step. target_svc != NULL for service levels (request_file).
 * Return 1 => the process was restarted (finish the process tick). */
static int run_ladder(long long now, proc_t *p, svc_t *tv, lstate_t *ls, int ladder_idx,
                      int level_mode, const char *target, int is_stall) {
    if (!(C.mode == ACT && level_mode == ACT)) return 0;   /* safety switch */
    if (ladder_idx < 0) return 0;
    if (!ls->firing_since) ls->firing_since = now;
    ladder_t *L = &C.ladders[ladder_idx];
    if (ls->step >= L->nsteps) return 0;                   /* gave up (stop) */
    step_t *st = &L->steps[ls->step];
    /* cooldown */
    if (ls->cooldown_until && now < ls->cooldown_until) return 0;
    /* pacing */
    if (ls->last_action && now - ls->last_action < pacing_delay(ls->tries_used + 1)) return 0;
    /* resource gate (force after force_after_ms) */
    if (!resource_gate(now, ls, target)) return 0;
    /* rate limit (per target/level) */
    rl_t *rl = st->is_verb ? &p->restart_rl : &C.actions[st->action_idx].rl;
    long long ws = now - rl->per_ms; int k = 0;
    for (int i = 0; i < ls->nfires; i++) if (ls->fires[i] >= ws) ls->fires[k++] = ls->fires[i];
    ls->nfires = k;
    if (ls->nfires >= rl->max) {
        if (rl->on_exceeded == 0) {
            ls->cooldown_until = now + rl->cooldown_ms;
            wd_log("rate_limit_cooldown target=%s until_s=%lld", target, ls->cooldown_until / 1000);
        } else { wd_log("rate_limit_stop target=%s", target); ls->step = L->nsteps; }
        return 0;
    }
    /* execution */
    if (st->is_verb) {
        do_restart_process(p, now, "ladder");
        return 1;
    }
    action_t *a = &C.actions[st->action_idx];
    const char *br = strstr(a->file_tmpl, "{service}");
    if (br) snprintf(tv->crash_path, sizeof tv->crash_path, "%.*s%s%s",
                     (int)(br - a->file_tmpl), a->file_tmpl, tv->name, br + 9);
    else snprintf(tv->crash_path, sizeof tv->crash_path, "%s", a->file_tmpl);
    int fd = open(tv->crash_path, O_WRONLY | O_CREAT, 0644);
    if (fd >= 0) close(fd);
    tv->crash_created = now; tv->crash_eat_ms = a->eat_within_ms; tv->crash_stall = is_stall;
    tv->next_judge = now + a->startup_ms;
    ls->last_action = now;
    if (ls->nfires < MAX_FIRES) ls->fires[ls->nfires++] = now;
    ls->tries_used++;
    wd_log("action target=%s step=%d attempt=%d reason=%s", target, ls->step, ls->tries_used,
           is_stall ? "stalled" : "no_pulse");
    if (st->tries >= 1 && ls->tries_used >= st->tries) { ls->step++; ls->tries_used = 0; ls->last_action = 0; }
    return 0;
}

/* ---------------------------------------------------------------- tick */
static void reset_ladder_if_active(svc_t *v, lstate_t *ls, const char *kind) {
    if (ls->step || ls->tries_used || ls->firing_since) {
        wd_log("recovered name=%s level=%s", v->name, kind);
        memset(ls, 0, sizeof *ls);
    }
}

static void tick_process(long long now, proc_t *p) {
    /* fresh gate at the start */
    long long age = process_age_ms(p->pidfile);
    if (age >= 0 && age < p->grace_ms) {
        wd_log("fresh_process process=%s age_s=%lld", p->name, age / 1000);
        p->grace_until = now + (p->grace_ms - age);
        for (int i = 0; i < p->nsvc; i++) reset_service_state(&p->svc[i]);
        return;
    }
    if (now < p->grace_until) return;

    /* P1: all services are silent (>=2) */
    if (p->p1 && p->nsvc >= 2) {
        int nsilent = 0, seen = 0;
        for (int i = 0; i < p->nsvc; i++) {
            svc_t *v = &p->svc[i];
            long long ref = v->last_seen ? v->last_seen : p->grace_until;
            if (v->last_seen) seen = 1;
            if (v->l1 && now - ref > v->l1_dead_ms) nsilent++;
        }
        if (nsilent == p->nsvc && seen) {
            if (!p->all_silent_logged) { wd_log("no_pulse_all process=%s n=%d", p->name, p->nsvc); p->all_silent_logged = 1; }
            if (run_ladder(now, p, NULL, &p->p1_st, p->p1_ladder, p->p1_mode, "@process", 0)) return;
            if (C.mode == ACT && p->p1_mode == ACT) return;   /* judge the process only */
        } else p->all_silent_logged = 0;
    }

    /* P2: a file request has been stuck longer than eat_within_ms. Do NOT consume the file
     * before an actual restart — otherwise in log-mode / under gate starvation the signal would be lost (review E6). */
    if (p->p2) {
        int stuck = 0;
        for (int i = 0; i < p->nsvc; i++) {
            svc_t *v = &p->svc[i];
            if (v->crash_created && access(v->crash_path, F_OK) == 0
                && now - v->crash_created > v->crash_eat_ms) stuck = 1;
        }
        if (stuck) {
            if (!p->p2_logged) { wd_log("request_stuck process=%s", p->name); p->p2_logged = 1; }
            if (run_ladder(now, p, NULL, &p->p2_st, p->p2_ladder, p->p2_mode, "@process", 0)) {
                /* restart executed (states reset) — clean up stuck request files */
                for (int i = 0; i < p->nsvc; i++) unlink(p->svc[i].crash_path);
                return;
            }
            /* did not fire (log-mode / gate / cooldown): the file is kept, the signal lives on */
        } else p->p2_logged = 0;
    }

    /* L-levels per service */
    for (int i = 0; i < p->nsvc; i++) {
        svc_t *v = &p->svc[i];
        /* consuming/awaiting a pending crash */
        if (v->crash_created) {
            if (access(v->crash_path, F_OK) != 0) {          /* eaten */
                v->crash_created = 0;
                if (v->crash_stall) { v->episode = 0; v->stalled_logged = 0; strcpy(v->state, "-"); memset(&v->l2_st, 0, sizeof v->l2_st); }
            } else continue;                                 /* still there: P2 will handle it */
        }
        if (now < v->next_judge) continue;                   /* startup/relaunch window */
        long long ref = v->last_seen ? v->last_seen : p->grace_until;
        long long silence = now - ref;
        int silent = v->l1 && silence > v->l1_dead_ms;
        int stalled = v->l2 && !silent && v->episode && !strcmp(v->state, "active")
                      && now - v->last_progress > v->l2_frozen_ms;
        if (!silent && !stalled) {                           /* alive */
            if (v->silent_logged) { v->silent_logged = 0; }
            reset_ladder_if_active(v, &v->l1_st, "L1");
            if (v->l2) reset_ladder_if_active(v, &v->l2_st, "L2");
            if (v->stalled_logged) v->stalled_logged = 0;
            continue;
        }
        if (silent) {
            if (!v->silent_logged) { wd_log("no_pulse name=%s silence_s=%lld state=%s", v->name, silence / 1000, v->state); v->silent_logged = 1; }
            if (run_ladder(now, p, v, &v->l1_st, v->l1_ladder, v->l1_mode, v->name, 0)) return;
        } else {
            if (!v->stalled_logged) { wd_log("stalled name=%s counter=%lld", v->name, v->last_counter); v->stalled_logged = 1; }
            if (run_ladder(now, p, v, &v->l2_st, v->l2_ladder, v->l2_mode, v->name, 1)) return;
        }
    }
}

static void handle_tick(void) {
    long long now = now_ms();
    double mem = mem_available_mb();
    if (mem < C.alarm_mb && !below_alarm) { below_alarm = 1; wd_log("mem_below_alarm mem_mb=%.1f alarm_mb=%.1f", mem, C.alarm_mb); }
    else if (mem >= C.alarm_mb) below_alarm = 0;

    if (access(C.pause_file, F_OK) == 0) {
        if (now - paused_last_log >= C.quiet_paused_ms) { wd_log("paused file=%s", C.pause_file); paused_last_log = now; }
        return;
    }
    paused_last_log = 0;
    for (int i = 0; i < C.nproc; i++) tick_process(now, &C.procs[i]);
}

/* ---------------------------------------------------------------- datagram */
static void handle_datagram(int sfd) {
    char buf[128];
    for (;;) {
        ssize_t n = recv(sfd, buf, sizeof buf - 1, MSG_DONTWAIT);
        if (n <= 0) return;
        buf[n] = 0;
        char *sp = strchr(buf, ' ');
        const char *state = NULL, *cnt = NULL;
        if (sp) { *sp = 0; state = sp + 1; char *sp2 = strchr(sp + 1, ' '); if (sp2) { *sp2 = 0; cnt = sp2 + 1; } }
        int known = 0;
        for (int a = 0; a < C.nproc && !known; a++)
            for (int i = 0; i < C.procs[a].nsvc; i++) {
                svc_t *v = &C.procs[a].svc[i];
                if (strcmp(v->name, buf)) continue;
                known = 1; v->last_seen = now_ms();
                if (state && strncmp(v->state, state, sizeof v->state - 1)) {
                    wd_log("state name=%s state=%s", v->name, state);
                    snprintf(v->state, sizeof v->state, "%s", state);
                    v->episode = 0;
                }
                if (cnt) {
                    long long val = strtoll(cnt, NULL, 10);
                    if (!v->episode || val != v->last_counter) { v->episode = 1; v->last_counter = val; v->last_progress = v->last_seen; }
                }
                break;
            }
        if (!known) {
            long long nw = now_ms();
            if (nw - last_unknown >= C.quiet_unknown_ms) { wd_log("unknown_name name=%.47s", buf); last_unknown = nw; }
        }
    }
}

/* ---------------------------------------------------------------- main */
int main(int argc, char **argv) {
    const char *conf = argc > 1 ? argv[1] : "/etc/svc_watch.conf";
    if (load_config(conf) != 0) return 1;
    signal(SIGPIPE, SIG_IGN);
    signal(SIGCHLD, SIG_IGN);          /* auto-reap forked restart scripts (no zombie, no wait) */
    { FILE *f = fopen("/proc/self/oom_score_adj", "w"); if (f) { fprintf(f, "%d", C.oom_adj); fclose(f); } }

    unlink(C.socket_path);
    int sfd = socket(AF_UNIX, SOCK_DGRAM, 0);
    struct sockaddr_un addr; memset(&addr, 0, sizeof addr);
    addr.sun_family = AF_UNIX;
    snprintf(addr.sun_path, sizeof addr.sun_path, "%s", C.socket_path);
    if (bind(sfd, (struct sockaddr *)&addr, sizeof addr) != 0) {
        fprintf(stderr, "svc_watchdog: bind(%s): %s\n", C.socket_path, strerror(errno)); return 1;
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
    ev.data.fd = tfd; epoll_ctl(ep, EPOLL_CTL_ADD, tfd, &ev);

    long long start = now_ms();
    int total_svc = 0;
    for (int i = 0; i < C.nproc; i++) { C.procs[i].grace_until = start + C.procs[i].grace_ms; total_svc += C.procs[i].nsvc; }
    wd_log("wd_start processes=%d services=%d socket=%s mode=%s", C.nproc, total_svc, C.socket_path,
           C.mode == ACT ? "act" : "log");

    struct epoll_event events[4];
    for (;;) {
        int n = epoll_wait(ep, events, 4, -1);
        for (int i = 0; i < n; i++) {
            if (events[i].data.fd == sfd) handle_datagram(sfd);
            else if (events[i].data.fd == tfd) { unsigned long long e; ssize_t r = read(tfd, &e, sizeof e); (void)r; handle_tick(); }
        }
    }
}
