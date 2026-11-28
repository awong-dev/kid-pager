/* `cfg.br`: a pager that left Soracom Beam for a server move tries Beam again when its server asks, and
 * falls back to the direct bearer unless a signed proof arrives over Beam (docs/PROTOCOL.md §3.2,
 * docs/MOVE_FIRMWARE_DESIGN.md §5.10).
 *
 * bearer_try.c is the part that runs on the host too: the parser, the request record in NVS, the boot
 * repair, the decisions and the /status keys. bearer_try_svc.c (ESP only) drives the modem from them.
 *
 * The stored bearer choice (carrier.h, NVS carrier/bearer) stays `direct` for the whole try; an attempt
 * selects Beam in RAM only (net_bearer_select()). Beam becomes the stored choice only in brtry_commit(),
 * on the proof, after the record says so. So a reset at any point boots on the direct bearer unless the
 * proof was taken, and the attempts, counted in NVS before each starts, never exceed BRTRY_ATTEMPTS. */
#ifndef BEARER_TRY_H
#define BEARER_TRY_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "cbor.h"

#ifdef __cplusplus
extern "C" {
#endif

#define BRTRY_ID_MAX 17 /* 16 chars + NUL */
#define BRTRY_ATTEMPTS 3

#define PAGER_BRTRY_SESSION_WAIT_S 180u   /* a usable Beam session after the connect is issued */
#define PAGER_BRTRY_PROOF_WAIT_S 180u     /* the proof after the first /status on Beam */
#define PAGER_BRTRY_ACK_WAIT_S 30u        /* the request's ack leaves on the direct session first */
#define PAGER_BRTRY_RETRY_S 60u           /* on the direct session between two attempts */
#define PAGER_BRTRY_DISCONNECT_WAIT_S 5u

typedef enum {
    BRTRY_ST_NONE = 0,
    BRTRY_ST_TRY,
    BRTRY_ST_OK,
    BRTRY_ST_FAIL,
    BRTRY_ST_NA,
    BRTRY_ST_PROVED, /* the proof was taken and the bearer choice is being given back to Beam; never
                        reported: `ok` follows in the same pass, or at the next boot */
} brtry_st_t;

typedef enum {
    BRTRY_RSN_NONE = 0,
    BRTRY_RSN_CONN,
    BRTRY_RSN_AUTH,
    BRTRY_RSN_PROOF,
} brtry_rsn_t;

const char *brtry_st_str(brtry_st_t st);   /* "try" "ok" "fail" "na"; "" for none */
const char *brtry_rsn_str(brtry_rsn_t rsn); /* "" for none */

/* The record under NVS "brtry"/"st". The last settled request is kept. */
typedef struct {
    char id[BRTRY_ID_MAX];
    uint8_t st;  /* brtry_st_t */
    uint8_t rsn; /* brtry_rsn_t: with `fail`; while `try`, the last failed attempt's */
    uint8_t at;  /* attempts started, each counted before it starts */
} brtry_rec_t;

/* `cfg.br` (CFG key 8): try `{id, b:"beam"}` or proof `{id, ok:true}`. */
typedef enum { BRTRY_SHAPE_NONE = 0, BRTRY_SHAPE_TRY, BRTRY_SHAPE_PROOF } brtry_shape_t;
typedef struct {
    brtry_shape_t shape;
    char id[BRTRY_ID_MAX];
} brtry_cfg_t;

/* False for anything not exactly a try or a proof; unknown sub-keys are ignored. */
bool brtry_parse_cfg_submap(const uint8_t *buf, uint16_t len, brtry_cfg_t *out);

/* The service's cross-task lock (modes.c's mutex); without a binding the calls do nothing, as on the host. */
void brtry_bind(void (*lock)(void), void (*unlock)(void));
void brtry_lock(void);
void brtry_unlock(void);

/* Record cache and NVS. The cache is written under the lock. */
void brtry_rec_get(brtry_rec_t *out); /* a copy taken under the lock, for any task */
const brtry_rec_t *brtry_rec(void);   /* the loop task's view */

typedef enum {
    BRTRY_BOOT_NOTHING,
    BRTRY_BOOT_RESUME,    /* `try` with attempts left: the loop makes the next one */
    BRTRY_BOOT_COMMITTED, /* the proof had been taken: the bearer choice is given back to Beam, `ok` */
    BRTRY_BOOT_EXHAUSTED, /* `try` with every attempt started: `fail` */
} brtry_boot_t;

/* Loads the record and repairs it. A `try` whose proof was not taken boots on the direct bearer: the
 * stored choice is checked and written again if it is anything else. */
brtry_boot_t brtry_boot_repair(void);

/* A `try` without its proof while the stored bearer choice is not `direct` (its write failed): the caller
 * keeps the bearer direct in RAM and makes no attempt. */
bool brtry_direct_owed(void);

/* A try: what the pager does with it. */
typedef struct {
    bool beam_offered;  /* the carrier preset in force names the Beam bearer */
    bool on_beam;       /* the session in use runs through Beam */
    bool direct_stored; /* the stored bearer choice is `direct`: what a failed try falls back to */
    bool busy;          /* a server move is unsettled or its trial runs, or an OTA or font download, a CA
                           fetch or a CA apply runs */
} brtry_ctx_t;

typedef enum {
    BRTRY_REQ_ACK_IGNORE, /* the stored id: ack, change nothing */
    BRTRY_REQ_DEFER,      /* no ack; looked at again on a later pass */
    BRTRY_REQ_NA,         /* settle `na`, ack */
    BRTRY_REQ_OK,         /* already on Beam: settle `ok`, ack */
    BRTRY_REQ_START,      /* store `try`, ack, then attempt */
} brtry_req_act_t;
brtry_req_act_t brtry_decide_try(const brtry_rec_t *rec, const char *id, const brtry_ctx_t *c);

typedef enum {
    BRTRY_PROOF_DROP,   /* not acked: off Beam, another id, or nothing waiting */
    BRTRY_PROOF_ACK,    /* the request already settled `ok`: ack only */
    BRTRY_PROOF_COMMIT, /* the awaited proof */
} brtry_proof_act_t;
/* `awaiting`: an attempt has a usable Beam session and waits for the proof. `on_beam`: the session the
 * proof came on runs through Beam. */
brtry_proof_act_t brtry_decide_proof(const brtry_rec_t *rec, const char *id, bool awaiting, bool on_beam);

/* Record transitions; each returns false, with nothing changed, when its NVS write fails. */
bool brtry_settle(const char *id, brtry_st_t st); /* `na`, or `ok` at once */
bool brtry_start(const char *id);                 /* `try`, no attempt yet */

/* Counts an attempt before it starts. False when the record is not `try`, every attempt is used, or the
 * count could not be stored: the caller then makes no attempt. Power effect: one NVS write per attempt. */
bool brtry_attempt_begin(void);

/* A failed attempt: the reason is stored, and after the last one the stored bearer choice is made sure to
 * be `direct` and the record says `fail`. True when that was the last attempt. */
bool brtry_attempt_failed(brtry_rsn_t rsn);

/* A `try` that cannot go on (every attempt started, or the stored choice is not `direct`): as the last
 * failed attempt. False while the bearer choice or the record cannot be written; called again later. */
bool brtry_give_up(void);

/* The proof was taken: the record says so (the commit point), the stored bearer choice goes back to
 * automatic, then the record says `ok`. False, with Beam not kept, if the first write fails. */
bool brtry_commit(void);

/* Drops the record: the request came from a server the pager has left. */
void brtry_clear(void);

/* What the loop does with a `try` record while no attempt runs. */
typedef struct {
    uint8_t at;
    bool ack_out;           /* the request's ack has left, or was never queued in this boot */
    int64_t store_age_us;   /* since the request was stored in this boot; -1 = a boot ago */
    int64_t since_last_us;  /* since the last attempt of this boot ended; -1 = none */
    bool session_usable;    /* on the direct bearer */
    bool registered, airplane, coverage_owns_radio;
    bool busy;              /* as brtry_ctx_t.busy */
    bool direct_stored;     /* as brtry_ctx_t.direct_stored */
} brtry_plan_in_t;
typedef enum { BRTRY_ACT_NONE, BRTRY_ACT_WAIT, BRTRY_ACT_ATTEMPT, BRTRY_ACT_GIVE_UP } brtry_act_t;
brtry_act_t brtry_plan(const brtry_plan_in_t *in);

/* One step of a running attempt. */
typedef enum { BRTRY_PH_IDLE = 0, BRTRY_PH_DISCONNECTING, BRTRY_PH_CONNECTING, BRTRY_PH_PROOF } brtry_phase_t;
typedef struct {
    brtry_phase_t phase;
    int64_t phase_age_us;
    bool usable;          /* the session is up and subscribed */
    bool disconnect_edge;
    bool refused;         /* the edge's class is a refusal (login or ACL) */
    bool radio_ok;        /* registered, not airplane, the coverage policy does not own the radio */
} brtry_step_in_t;
typedef enum {
    BRTRY_STEP_STAY,
    BRTRY_STEP_CONNECT,  /* the direct session has ended: select Beam and connect */
    BRTRY_STEP_TO_PROOF, /* usable on Beam: the proof wait starts */
    BRTRY_STEP_FAIL_CONN,
    BRTRY_STEP_FAIL_AUTH,
    BRTRY_STEP_FAIL_PROOF,
} brtry_step_t;
brtry_step_t brtry_step(const brtry_step_in_t *in);

/* /status keys 79 `brc` (uint 1) and 80 `br` (map {id=0, st=1, rsn=2 only with `fail`}), docs/PROTOCOL.md
 * §5.1 and §10. `br` is NULL or has st NONE when no request is held. `_fields` counts the root-map entries
 * `_write` adds. */
#define BRTRY_STK_BRC 79
#define BRTRY_STK_BR 80
uint32_t brtry_status_cbor_fields(bool brc, const brtry_rec_t *br);
bool brtry_status_cbor_write(cbor_w_t *w, bool brc, const brtry_rec_t *br);

/* The bearer note of the Device info screen's carrier row: "beam try 2/3" while a request is tried,
 * "beam failed" after one failed, "" otherwise (`ok` and `na` are what the bearer itself says). */
void brtry_ui_note(const brtry_rec_t *rec, char *out, size_t cap);

#ifdef ESP_PLATFORM

#include "move.h"
#include "net.h"

/* ---- bearer_try_svc.c ---- */

/* After move_init(): repairs and loads the record. */
void brtry_init(void);

/* `cfg.br` intercept (MQTT event task): a malformed map is dropped and counted, no ack. A build without
 * CONFIG_PAGER_MOVE_TRIALS advertises no `brc` and leaves the map alone, as an unknown `cfg` key. */
void brtry_apply_cfg_submap(const uint8_t *buf, uint16_t len, const char *cfg_id);

/* One non-blocking step per loop iteration, after move_service(), on the same snapshot. */
void brtry_service(const net_mqtt_status_t *st, const move_env_t *env);

/* True from an attempt's disconnect to its commit or its return to the direct bearer: the reconnect loop,
 * the registered-edge reconnect and the modem health reset stand aside, as for a move trial. */
bool brtry_owns_session(void);

/* A request is `try`: a move order and a cfg.ca wait for it to settle. */
bool brtry_busy(void);

/* True once after a failed attempt: the reconnect loop connects on the direct bearer at once. */
bool brtry_take_reconnect_request(void);

/* /status: `brc` goes with `mvc`; the record, if a request is held. False without CONFIG_PAGER_MOVE_TRIALS. */
bool brtry_status_get(brtry_rec_t *out);

#endif /* ESP_PLATFORM */

#ifdef __cplusplus
}
#endif

#endif /* BEARER_TRY_H */
