/* Server move over the air: pager side (docs/MOVE_FIRMWARE_DESIGN.md, wire in docs/PROTOCOL.md §3.2).
 *
 * move.c is the part that runs on the host too: the `cfg.mv` parser, the key derivation, the move
 * record and its ledger in NVS, the boot-time repair, the order and plan decisions and the reconnect
 * back-off. move_svc.c (ESP only) drives the modem and the messages from them. */
#ifndef MOVE_H
#define MOVE_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "cbor.h"
#include "ident.h"
#include "net.h"

#ifdef __cplusplus
extern "C" {
#endif

/* The service's cross-task lock (modes.c's mutex). Without a binding the calls do nothing, as on the host. */
typedef void (*move_lock_fn)(void);
typedef void (*move_unlock_fn)(void);
void move_bind(move_lock_fn lock, move_unlock_fn unlock);
void move_lock(void);
void move_unlock(void);

#define MOVE_ID_MAX 17      /* 16 chars + NUL */
#define MOVE_HOST_MAX 128   /* 127 chars + NUL, IDENT_HOST_MAX */
#define MOVE_CA_URL_MAX 201 /* 200 bytes + NUL */
#define MOVE_LEDGER_N 4
#define MOVE_NO_SLOT 0xFF
#define MOVE_SCHED_STEPS 4  /* scheduled trials before `fail/budget` */

/* Timers, docs/MOVE_FIRMWARE_DESIGN.md §5.6. */
#define PAGER_MOVE_BG_TRIAL_S 86400u        /* after the budget */
#define PAGER_MOVE_ACK_WAIT_S 30u
#define PAGER_MOVE_DRAIN_S 30u
#define PAGER_MOVE_DRAIN_MANUAL_S 5u
#define PAGER_MOVE_PROOF_WAIT_S 180u
#define PAGER_MOVE_GUARD_S 60u               /* SUBACK on the new broker */
#define PAGER_MOVE_DISCONNECT_WAIT_S 5u
#define PAGER_MOVE_QUIET_S 60u              /* no key this long before a background trial */
#define PAGER_MOVE_MOVING_HOLD_S 86400u     /* a `moving` receipt without an order */
#define PAGER_MOVE_CA_TRIES 3
#define PAGER_MOVE_CA_RETRY_S 60u

typedef enum {
    MOVE_ST_NONE = 0,
    MOVE_ST_RECV,
    MOVE_ST_PEND,
    MOVE_ST_TRY, /* never stored; the live phase reports it */
    MOVE_ST_OK,
    MOVE_ST_FAIL,
    MOVE_ST_CXL,
} move_st_t;

typedef enum {
    MOVE_RSN_NONE = 0,
    MOVE_RSN_CA,
    MOVE_RSN_TLS,
    MOVE_RSN_AUTH,
    MOVE_RSN_CONN,
    MOVE_RSN_PROOF,
    MOVE_RSN_BUDGET,
    MOVE_RSN_NVS,
    MOVE_RSN_REJ,
    MOVE_RSN_DOWN,
    MOVE_RSN_BUSY,
} move_rsn_t;

/* `bearer` of the record. An unsettled order taken on Beam says BEAM, and its commit stores the bearer
 * choice `direct`. The `ok` record says 0 once that is stored and OWED while the write is still to be made;
 * a BEAM left in an `ok` record by firmware that moved through Beam's group asks for nothing. */
#define MOVE_BEARER_BEAM 1
#define MOVE_BEARER_OWED 2

/* Ledger outcomes. */
#define MOVE_OUT_NONE 0
#define MOVE_OUT_OK 1
#define MOVE_OUT_CXL 2

const char *move_st_str(move_st_t st);   /* "pend" "try" "ok" "fail" "cxl"; "" for none/recv */
const char *move_rsn_str(move_rsn_t rsn); /* "" for none */

/* The record under NVS "move"/"st" (§4). */
typedef struct {
    char id[MOVE_ID_MAX];
    uint8_t st;       /* move_st_t */
    uint8_t rsn;      /* move_rsn_t reported with `st` */
    uint8_t lrsn;     /* move_rsn_t of the last failed trial, for the Device screen */
    uint8_t at;       /* trials and attempts started for this id, saturating */
    uint8_t sched;    /* scheduled trials started */
    uint8_t slot;     /* the pending identity slot, MOVE_NO_SLOT if there is none */
    uint8_t bearer;   /* MOVE_BEARER_*: what the commit owes the bearer choice (§5.3) */
    uint8_t acked;
    uint8_t tb;       /* trial-by-restart armed (§5.4, not used by the in-boot trial) */
    uint32_t next_try_ts; /* epoch s; 0 = due, or no clock */
    uint32_t ack_ts;      /* epoch s of the ack; 0 = unknown */
    uint8_t ca_sha[32];   /* all zero = the target is unpinned */
    char ledger_id[MOVE_LEDGER_N][MOVE_ID_MAX]; /* newest first */
    uint8_t ledger_out[MOVE_LEDGER_N];
} move_rec_t;

/* `cfg.mv` (§3). Ids and shapes are checked here; host syntax and port 0 are not (move_decide_order()). */
typedef enum {
    MOVE_SHAPE_NONE = 0,
    MOVE_SHAPE_ORDER,
    MOVE_SHAPE_CANCEL,
    MOVE_SHAPE_PROOF,
} move_shape_t;

typedef struct {
    move_shape_t shape;
    char id[MOVE_ID_MAX];
    char host[MOVE_HOST_MAX];
    uint16_t port;
    bool have_ca;
    char ca_url[MOVE_CA_URL_MAX];
    uint8_t ca_sha[32];
    uint64_t ok_n;
} move_cfg_t;

/* False for anything not exactly an order, a cancel or a proof; unknown sub-keys are ignored. */
bool move_parse_cfg_submap(const uint8_t *buf, uint16_t len, move_cfg_t *out);

/* Hostname (letters, digits, '.', '-', no empty label) or an IP literal, 1-127 bytes. */
bool move_host_ok(const char *host);

/* §2 M2 / PROTOCOL.md §14.8. `pw_out` is 43 base64url characters and a NUL. False on a bad argument. */
#define MOVE_PW_LEN 43
bool move_derive(const uint8_t k[32], const char *move_id, const char *dev_id, uint8_t k_out[32],
                 char pw_out[MOVE_PW_LEN + 1]);

/* Ledger helpers on the in-memory record. */
uint8_t move_ledger_find(const move_rec_t *r, const char *id); /* MOVE_OUT_NONE if absent */
void move_ledger_add(move_rec_t *r, const char *id, uint8_t out);

/* An order or cancel the move holds: a pending slot and a state that is neither final nor refused. */
bool move_rec_unsettled(const move_rec_t *r);

/* §5.1 and §5.5 for an order. */
typedef struct {
    bool signing;         /* IDENT_FLAG_REQ_SIG */
    bool image_confirmed; /* not pending verify */
    bool active_pinned;
    bool busy;            /* a catrust apply, an OTA or font download, or a cafetch runs */
    bool trial_running;   /* a trial, or the processing of another order, is in flight */
    bool trials_built;    /* CONFIG_PAGER_MOVE_TRIALS: this image can finish a move */
    bool beam;            /* the session runs through Beam, which validates the broker for the pager */
} move_order_ctx_t;

typedef enum {
    MOVE_ORD_ACK_IGNORE, /* the current or a finished id: ack, change nothing */
    MOVE_ORD_REJECT,     /* ack, report fail with *rsn */
    MOVE_ORD_DEFER,      /* no ack; the relay re-publishes */
    MOVE_ORD_ACCEPT,     /* a move starts */
    MOVE_ORD_REPLACE,    /* an unsettled move of another id is replaced */
} move_order_act_t;

move_order_act_t move_decide_order(const move_rec_t *rec, const move_cfg_t *o, const move_order_ctx_t *c,
                                   move_rsn_t *rsn);

/* §5.5 for a cancel and a proof; only the id (and the counter of a proof) is looked at. */
typedef enum {
    MOVE_CXL_ACK_IGNORE, /* ok, finished or unknown id */
    MOVE_CXL_DEFER,      /* a trial runs */
    MOVE_CXL_APPLY,
} move_cancel_act_t;
move_cancel_act_t move_decide_cancel(const move_rec_t *rec, const char *id, bool trial_running);

typedef enum {
    MOVE_PROOF_DROP,   /* not acked, counted */
    MOVE_PROOF_ACK,    /* the committed move's proof again: ack only */
    MOVE_PROOF_COMMIT, /* the awaited proof */
} move_proof_act_t;
move_proof_act_t move_decide_proof(const move_rec_t *rec, const char *id, uint64_t ok_n, bool awaiting,
                                   uint64_t n_first);

/* Record cache and NVS. */
void move_rec_init_empty(move_rec_t *r);
const move_rec_t *move_rec(void); /* the loop task's view; other tasks use move_rec_get() */
void move_rec_get(move_rec_t *out); /* a copy taken under the service lock, for any task */
bool move_rec_update(const move_rec_t *r); /* NVS first, then the cache under the lock; false leaves both as they were */

/* The target's CA URL, which does not fit the record (§5.8 re-fetch). */
bool move_url_load(char *out, size_t cap);

/* What a stored order carries. `pem` is "" when there is none. */
typedef struct {
    const move_cfg_t *o;
    const char *pem;
    bool ca_failed;   /* the fetch failed three times; the slot is stored without a PEM */
    bool beam;        /* the pager is on Beam: recorded, so the commit leaves that bearer (§5.3) */
    uint32_t now_epoch;
} move_accept_t;

/* §4 write order: record `recv`, the pending slot and its seal, record `pend`. A failure leaves
 * `fail`/`nvs`. True when the order is stored (`pend`, or `fail`/`ca`). */
bool move_store_order(const move_accept_t *a);

/* Refused at parse (§5.1 item 3): the record shows `fail`/`rsn` with no slot. A move that holds a
 * pending slot is left alone and false is returned. */
bool move_note_reject(const char *id, move_rsn_t rsn);

/* §5.5 cancel: ledger entry and `cxl`, then the pending slot is dropped. */
bool move_cancel_apply(void);

/* §4 commit point and after: writes `idsel/act`, then the bearer choice `direct` when the order was taken
 * on Beam, then record `ok` and the ledger entry. False, with nothing committed, if the slot cannot be
 * committed. */
bool move_commit(void);

/* The committed move left Beam and the bearer choice `direct` is not stored yet (an NVS write failed): the
 * caller keeps the bearer direct in RAM, and every boot tries the write again (move_boot_repair()). */
bool move_bearer_owed(void);

typedef enum {
    MOVE_BOOT_NOTHING,
    MOVE_BOOT_CLEARED,   /* `recv`: the half-written order is dropped */
    MOVE_BOOT_DEMOTED,   /* an interrupted trial goes back to `pend` */
    MOVE_BOOT_COMMITTED, /* `act` was written, the record (and, from Beam, the bearer choice) was not */
    MOVE_BOOT_DROPPED,   /* the record named a slot that is not there */
    MOVE_BOOT_BUDGET,    /* `pend` with the scheduled trials used up */
} move_boot_t;

/* After ident_load(): loads the record and repairs it against the slots (§4 power-loss table). */
move_boot_t move_boot_repair(void);

/* An image that cannot run trials: an order stored by one that could is reported `fail`/`rej`, its
 * pending slot and CA URL are dropped, and no hold remains. True if there was one. After move_boot_repair(). */
bool move_boot_refuse_unsettled(void);

/* Replies and sms_log entries are held while a move is unsettled, and after a `moving` receipt. */
bool move_hold_up_at(int64_t now_us);
void move_hold_note_moving(int64_t now_us);
void move_hold_clear_moving(void);

/* State the UI and /status read. */
typedef struct {
    bool have;          /* a record exists (the last terminal one is kept) */
    char id[MOVE_ID_MAX];
    move_st_t st;       /* TRY while a trial waits for the proof */
    move_rsn_t rsn;
    uint8_t at;
} move_status_t;
void move_status_from_rec(const move_rec_t *r, bool trying, move_status_t *out);

/* /status keys 77 `mvc` (uint 1) and 78 `mv` (map {id=0, st=1, rsn=2 only when set, at=3}), docs/PROTOCOL.md
 * §5.1 and §10. `mv` is NULL or has !have when no record exists. `_fields` counts the root-map entries
 * `_write` adds, so the caller can size the map header first. */
#define MOVE_STK_MVC 77
#define MOVE_STK_MV 78
uint32_t move_status_cbor_fields(bool mvc, const move_status_t *mv);
bool move_status_cbor_write(cbor_w_t *w, bool mvc, const move_status_t *mv);

/* ---- plan: what the loop does next (docs/MOVE_FIRMWARE_DESIGN.md §5.2, §5.7) ---- */

typedef enum {
    MOVE_ACT_NONE,
    MOVE_ACT_WAIT_ACK,   /* the order's ack is not out yet */
    MOVE_ACT_WAIT_REPLY, /* a reply awaits its PUBACK */
    MOVE_ACT_TRIAL_SCHED,
    MOVE_ACT_TRIAL_BG,
    MOVE_ACT_TRIAL_MANUAL,
    MOVE_ACT_REFUSE_AIRPLANE,
    MOVE_ACT_REFUSE_NO_NETWORK,
    MOVE_ACT_REFUSE_BUSY,
} move_act_t;

typedef struct {
    move_st_t st;       /* the stored state */
    uint8_t sched;
    bool acked;
    uint32_t now_epoch; /* 0 = no clock */
    uint32_t next_try_ts;
    bool mono_gate_open; /* RAM spacing so a missing clock cannot spin the schedule */
    int64_t store_age_us; /* since the order was stored in this boot; -1 = a boot ago */
    bool session_usable;
    bool registered, airplane, coverage_owns_radio;
    bool downloading;    /* OTA or font download, a cafetch or a catrust apply */
    bool quiet;          /* sleep mode and no key for PAGER_MOVE_QUIET_S */
    bool reply_awaiting; /* a reply awaits its PUBACK */
    int64_t reply_wait_us;
    bool manual;
    uint8_t attempts;    /* trials and attempts started for this order (the record's `at`) */
} move_plan_in_t;

/* Without network time a trial that follows an earlier one waits: only a manual one runs. */
move_act_t move_plan(const move_plan_in_t *in);

/* The epoch at which the spacing after the last trial ends, for a record whose trial started with no
 * clock (next_try_ts 0, attempts > 0), computed when the clock first shows. `remaining_us` is the RAM
 * spacing left in this boot, or -1 when a reset lost it (the whole step then applies). 0 = nothing to fill. */
uint32_t move_next_try_fill(const move_rec_t *r, uint32_t now_epoch, int64_t remaining_us);

/* Delay after a scheduled trial that made `sched` trials: 10 min, 1 h, 6 h, then 24 h. */
uint32_t move_sched_delay_s(uint8_t sched);

/* §5.7: 5, 15, 60, 300 s, then 300 s until an hour without a session, 15 min until a day, then 30 min. */
uint32_t move_backoff_delay_s(uint32_t idx, uint32_t no_session_s);

/* §5.7: which identity the next connect attempt uses. */
typedef struct {
    bool next_pending;      /* the next attempt goes to the pending login */
    bool usable_since_issue; /* the session was usable since the last attempt was issued */
} move_alt_t;

/* A loss. A session that was never usable since the attempt was issued failed the attempt on the
 * login in use, so the next attempt uses the other one. `on_pending`: the attempt was on the pending login. */
void move_alt_on_loss(move_alt_t *a, bool on_pending);
void move_alt_on_usable(move_alt_t *a);
void move_alt_on_issue(move_alt_t *a);

/* ---- Device screen (docs/MOVE_FIRMWARE_DESIGN.md §5.8) ---- */

typedef enum {
    MOVE_REQ_OK = 0,
    MOVE_REQ_NOT_BUILT,   /* built without CONFIG_PAGER_MOVE_TRIALS */
    MOVE_REQ_NO_ORDER,
    MOVE_REQ_RUNNING,
    MOVE_REQ_AIRPLANE,
    MOVE_REQ_NO_NETWORK,
    MOVE_REQ_BUSY,
} move_req_t;
typedef enum {
    MOVE_PHASE_IDLE = 0,
    MOVE_PHASE_CONNECTING, /* "Connecting to <host>..." */
    MOVE_PHASE_WAITING,    /* "Waiting for <host>...": the proof wait */
} move_phase_t;

typedef struct {
    bool have;
    char id[MOVE_ID_MAX];
    char host[MOVE_HOST_MAX]; /* the order's host, from the pending slot; "" when none */
    move_st_t st;
    move_rsn_t rsn;       /* stored: what /status carries */
    move_rsn_t last_rsn;  /* the last trial's reason, for the outcome line */
    uint8_t at;
    bool action_available; /* a move is held and not committed: show `Switch to <host> now` */
    bool running;
    move_phase_t phase;
    uint32_t outcome_seq; /* bumps when a run ends, so the screen paints once per outcome */
    bool outcome_committed;
    bool back_on_active;  /* the active identity has a session again after a failed run */
} move_ui_state_t;
#ifdef ESP_PLATFORM

/* ---- move_svc.c ---- */

/* After ident_load() and the RTC restore: repairs the record against the slots, then loads it. */
void move_init(void);

/* `cfg.mv` intercept (MQTT event task): a malformed map is dropped and counted, no ack. */
void move_apply_cfg_submap(const uint8_t *buf, uint16_t len, const char *cfg_id);

typedef struct {
    int64_t now_us;
    uint32_t now_epoch;
    bool registered;
    bool airplane;
    bool coverage_owns_radio;
    bool sleep_mode;
    uint32_t input_age_ms;
    bool downloading; /* OTA or font download, cafetch, catrust apply */
} move_env_t;

/* One non-blocking step per loop iteration; `st` is this iteration's MQTT snapshot, as for
 * catrust_service(). Cancels and proofs are handled here; orders are stored and trials run only when built
 * with CONFIG_PAGER_MOVE_TRIALS, otherwise an order is refused (`fail`/`rej`) and acked. */
void move_service(const net_mqtt_status_t *st, const move_env_t *env);

/* Replies and sms_log entries are held back (§6.2). Any task. */
bool move_hold_up(void);

/* /status keys 77 and 78 (docs/PROTOCOL.md §5.1). `mvc` is true only in a trial build, when the
 * message is signed, the image is confirmed and the rollback bootloader is confirmed. */
bool move_status_mvc(bool signed_env, bool image_confirmed, bool rollback_ok);
bool move_status_get(move_status_t *out);

/* A move holds an unsettled order. */
bool move_unsettled(void);

/* A move with an unsettled order in a build that runs trials: the reconnect loop then follows §5.7
 * (the long-run schedule, the other identity after a refusal). Always false without CONFIG_PAGER_MOVE_TRIALS. */
bool move_trials_active(void);

/* A cfg.ca apply waits while a move is `pend` or a trial runs; never without CONFIG_PAGER_MOVE_TRIALS. */
bool move_blocks_cfg_ca(void);

/* A `moving` receipt re-queued a reply (§6.2): hold replies for a day if no order is stored. */
void move_note_moving_receipt(void);

/* True from the first step of a trial to its commit or revert; the reconnect loop, registered-edge reconnect,
 * no-network modem reset and TLS-fail fallback stand aside. False without CONFIG_PAGER_MOVE_TRIALS. */
bool move_owns_session(void);

/* modes_run() notes, for the unsettled-move reconnect schedule (§5.7). */
void move_note_usable(int64_t now_us);
void move_note_loss(int64_t now_us, const net_mqtt_status_t *st);
void move_note_connect_issued(void);
void move_note_backoff_reset(int64_t now_us);
uint32_t move_no_session_s(int64_t now_us);

/* A disconnect a trial, its revert or a cfg.br attempt just issued: the next DISCONNECTED within
 * PAGER_MOVE_DISCONNECT_WAIT_S + 1 s is its own. */
void move_note_own_disconnect(int64_t now_us);

/* True once, right after such a disconnect: its DISCONNECTED is not a lost session. */
bool move_swallow_own_disconnect(int64_t now_us);

/* The reconnect loop asks, when it is about to connect and nothing is up: should this attempt use the
 * pending identity? If so move_begin_pending_attempt() runs it as a trial. */
bool move_attempt_pending_next(const move_env_t *env);
void move_begin_pending_attempt(const move_env_t *env);

/* What the reconnect loop does after a trial ended: nothing; drop the session and connect on the active
 * identity at once (back-off index 0); or take the next back-off step. */
typedef enum { MOVE_RECONNECT_NONE = 0, MOVE_RECONNECT_NOW, MOVE_RECONNECT_BACKOFF } move_reconnect_t;
move_reconnect_t move_take_reconnect_request(void);

/* Device-screen entry point (§5.8): MOVE_REQ_OK means move_service() runs a trial on its next pass. */
move_req_t move_request_now(void);

/* The state the Device screen draws; the screen compares `phase` and `outcome_seq` between renders. */
void move_get_ui_state(move_ui_state_t *out);

#endif /* ESP_PLATFORM */

#ifdef __cplusplus
}
#endif

#endif /* MOVE_H */
