// CA-over-HTTPS fetch: HTTP/1.1 parser, URL parser, single-PEM check, and the device socket driver.
#ifndef CAFETCH_H
#define CAFETCH_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define CAFETCH_HOST_MAX 128   /* matches IDENT_HOST_MAX */
#define CAFETCH_PATH_MAX 256   /* "/ca/<64 hex>.pem" is 68 bytes */
#define CAFETCH_BODY_MAX 4096  /* body capped at 4096 bytes */
#define CAFETCH_LINE_MAX 512   /* one status/header line */
#define CAFETCH_SHA_LEN 32
#define CAFETCH_PEM_MAX (CAFETCH_BODY_MAX + 1)

// `https://host[:port]/path` only; port defaults to 443; `path_out` includes the leading '/'.
bool cafetch_parse_url(const char *url, char *host_out, size_t host_cap, uint16_t *port_out,
                       char *path_out, size_t path_cap);

bool cafetch_build_request(const char *host, const char *path, char *out, size_t cap,
                           size_t *out_len);

// Stream-mode body sink; return false to abort.
typedef enum {
    CAFETCH_PHASE_STATUS_LINE = 0,
    CAFETCH_PHASE_HEADER_LINE,
    CAFETCH_PHASE_BODY_LENGTH,        /* Content-Length known */
    CAFETCH_PHASE_BODY_UNTIL_CLOSE,   /* neither Content-Length nor chunked */
    CAFETCH_PHASE_CHUNK_SIZE_LINE,
    CAFETCH_PHASE_CHUNK_DATA,
    CAFETCH_PHASE_CHUNK_CRLF,         /* CRLF after each chunk's data */
    CAFETCH_PHASE_CHUNK_TRAILER,      /* trailer lines after the 0-length chunk */
    CAFETCH_PHASE_DONE,
    CAFETCH_PHASE_ERROR,
} cafetch_phase_t;

typedef struct {
    cafetch_phase_t phase;

    char line[CAFETCH_LINE_MAX];
    size_t line_len;

    int status_code;
    bool have_content_length;
    size_t content_length;
    bool chunked;

    uint8_t body[CAFETCH_BODY_MAX];
    size_t body_len;

    size_t chunk_remaining; /* bytes left in the current chunk */

    // Sticky failure reasons; only the first one hit is set.
    bool err_oversize;   /* body or declared Content-Length > CAFETCH_BODY_MAX */
    bool err_malformed;  /* bad status line, headers or chunk framing */
    bool err_non200;     /* status != 200 (redirects included) */
} cafetch_parser_t;

void cafetch_parser_init(cafetch_parser_t *p);

// Feeds more bytes in any split; false once the phase is ERROR.
bool cafetch_parser_feed(cafetch_parser_t *p, const uint8_t *data, size_t len);

// Socket closed: ends an until-close body, else a truncated response.
void cafetch_parser_closed(cafetch_parser_t *p);

// Hashes `body` exactly as delivered; copies its single PEM block to `pem_out`. False on mismatch.
bool cafetch_validate_body(const uint8_t *body, size_t body_len, const uint8_t expected_sha[CAFETCH_SHA_LEN],
                          char *pem_out, size_t pem_cap, size_t *pem_len);

#ifdef ESP_PLATFORM

bool cafetch_begin(const char *url);

typedef enum {
    CAFETCH_PENDING = 0, /* waiting for data or the 30 s timeout */
    CAFETCH_OK,          /* full body received: call cafetch_result() */
    CAFETCH_FAILED,      /* already logged */
} cafetch_status_t;

// One non-blocking step; call every modes_run() iteration. Never blocks.
cafetch_status_t cafetch_poll(int64_t now_us);

// After CAFETCH_OK: validates the body; the out-params are filled regardless.
bool cafetch_result(const uint8_t expected_sha[CAFETCH_SHA_LEN], char *pem_out, size_t pem_cap,
                    size_t *pem_len, int *out_http_status, size_t *out_bytes);

// Tears down the socket; call after CAFETCH_OK/FAILED before the next begin.
void cafetch_end(void);

bool cafetch_in_progress(void);

// Blocking wrapper for callers that own a task; `out_mqtt_survived` = MQTT stayed up across it.
bool cafetch_run_blocking(const char *url, const uint8_t expected_sha[CAFETCH_SHA_LEN], char *pem_out,
                         size_t pem_cap, size_t *pem_len, int *out_http_status, size_t *out_bytes,
                         uint32_t *out_elapsed_ms, bool *out_mqtt_survived);

#endif

#ifdef __cplusplus
}
#endif

#endif
