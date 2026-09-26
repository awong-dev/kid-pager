/* resub_verdict.h — decides what a re-SUBSCRIBE that got no SUBACK in 30 s means. Pure function. */
#ifndef RESUB_VERDICT_H
#define RESUB_VERDICT_H

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define RESUB_VERDICT_TIMEOUT_US ((int64_t) 30 * 1000000)

typedef enum {
    RESUB_VERDICT_NONE = 0, /* no re-SUBSCRIBE outstanding, or it has not timed out yet */
    RESUB_VERDICT_ALIVE,    /* a /down message ingested after `sent_us` proves the session alive */
    RESUB_VERDICT_RETRY,    /* first re-SUBSCRIBE unanswered, no downlink proof: send a second one */
    RESUB_VERDICT_DEAD,     /* the second re-SUBSCRIBE (second_try) also went unanswered */
} resub_verdict_t;

/* wait: a re-SUBSCRIBE is outstanding. sent_us: when it was sent. last_down_ingest_us: last ingested
 * /down message, 0 if none this boot. second_try: it is already a retry. All times esp_timer_get_time(). */
resub_verdict_t resub_verdict_check(bool wait, int64_t sent_us, int64_t last_down_ingest_us,
                                     bool second_try, int64_t now_us);

#ifdef __cplusplus
}
#endif

#endif /* RESUB_VERDICT_H */
