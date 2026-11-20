/* SSD1680 e-paper transport (docs/PROTOCOL.md §6). */
#include "disp.h"
#include "disp_flip.h"
#include "gfx.h"
#include "refreshpol.h"
#include "board.h"
#include "pins.h"
#include "wf_partial_2in9.h"

#include <stdio.h>
#include <string.h>

#include "driver/gpio.h"
#include "driver/spi_master.h"
#include "esp_log.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "nvs.h"

#include "flightrec.h"

static const char *TAG = "disp";

// Bound for BUSY seen asserted but never deasserting.
#define PAGER_UI_BUSY_TIMEOUT_US (6 * 1000000)

// Partial refresh windows are widened to a multiple of this many native rows.
#define PAGER_UI_PARTIAL_ROW_ALIGN 8

/* Shadow plane: what the panel was last told to show. */
static uint8_t s_fb_old[GFX_FB_ROWS][GFX_FB_ROW_BYTES];

// Bytes sent to 0x24 for the partial in flight; the 0x26 re-sync and s_fb_old commit use this,
// never a fresh framebuffer read.
static uint8_t s_fb_snap[GFX_FB_ROWS][GFX_FB_ROW_BYTES];

static spi_device_handle_t s_spi;
static bool s_spi_ready = false;
static bool s_display_dead = false; // logged once, then the device runs headless
static bool s_display_dead_logged = false;
static uint32_t s_partial_count = 0;

static uint32_t s_cnt_full = 0;
static uint32_t s_cnt_partial = 0;
static uint32_t s_cnt_forced_full = 0;

// Last refreshes, oldest first. kind: F full, P partial, U partial upgraded to full.
// tag: 1 full, 2 partial, 4 completed via the BUSY-timeout retry, 0 other.
#define DISP_REFRESH_RING_LEN 8
typedef struct {
    uint32_t ms;
    char kind;
    uint8_t tag;
} disp_refresh_event_t;
static disp_refresh_event_t s_refresh_ring[DISP_REFRESH_RING_LEN];
static uint8_t s_refresh_ring_head = 0;  // next slot to write
static uint8_t s_refresh_ring_count = 0; // valid entries, saturates at DISP_REFRESH_RING_LEN

static volatile bool s_power_loss_render_pending = false;
static int64_t s_power_loss_t0_us = 0;

static void disp_refresh_ring_push(char kind, uint8_t tag)
{
    s_refresh_ring[s_refresh_ring_head].ms = (uint32_t) (esp_timer_get_time() / 1000);
    s_refresh_ring[s_refresh_ring_head].kind = kind;
    s_refresh_ring[s_refresh_ring_head].tag = tag;
    s_refresh_ring_head = (uint8_t) ((s_refresh_ring_head + 1) % DISP_REFRESH_RING_LEN);
    if (s_refresh_ring_count < DISP_REFRESH_RING_LEN) {
        s_refresh_ring_count++;
    }
    if (s_power_loss_render_pending) {
        s_power_loss_render_pending = false;
        int64_t elapsed_ms = (esp_timer_get_time() - s_power_loss_t0_us) / 1000;
        ESP_LOGI(TAG, "disp: power-loss-to-render done in %lld ms", elapsed_ms);
        flightrec_event('L', (int32_t) elapsed_ms, 0);
    }
}

static uint32_t s_busy_timeout_count = 0;

// Makes the next disp_partial_refresh() a full refresh.
static bool s_force_full = false;

static bool s_partial_write_again = true;

// Host-written LUT mode: 0x12 wipes any loaded LUT, so it is sent before every partial.
#define DISP_NVS_KEY_LUT "lut"
static bool s_partial_use_lut = true;

void disp_set_partial_lut(bool on)
{
    s_partial_use_lut = on;
    nvs_handle_t h;
    if (nvs_open("disp", NVS_READWRITE, &h) != ESP_OK) {
        ESP_LOGI(TAG, "disp lut NVS open failed; not persisted this boot");
        return;
    }
    nvs_set_u8(h, DISP_NVS_KEY_LUT, (uint8_t) on);
    nvs_commit(h);
    nvs_close(h);
}

static void disp_load_lut(void)
{
    nvs_handle_t h;
    if (nvs_open("disp", NVS_READONLY, &h) != ESP_OK) {
        return; // never set; stays default true
    }
    uint8_t v = 1;
    if (nvs_get_u8(h, DISP_NVS_KEY_LUT, &v) == ESP_OK) {
        s_partial_use_lut = (v != 0);
    }
    nvs_close(h);
}

bool disp_get_partial_lut(void) { return s_partial_use_lut; }

// LUT byte 60 is group 0 TP0A (5 VS LUTs x 12 bytes, then group 0's first byte): changed-pixel
// drive frames. The table value is 10.
#define DISP_LUT_TP0A_IDX 60
#define DISP_NVS_KEY_TP0A "tp0a"
static uint8_t s_lut_tp0a = PAGER_LUT_TP0A;

static void disp_load_tp0a(void)
{
    nvs_handle_t h;
    if (nvs_open("disp", NVS_READONLY, &h) != ESP_OK) {
        return;
    }
    uint8_t v = 0;
    if (nvs_get_u8(h, DISP_NVS_KEY_TP0A, &v) == ESP_OK && v >= 1 && v <= 31) {
        s_lut_tp0a = v;
    }
    nvs_close(h);
}

uint8_t disp_get_lut_tp0a(void) { return s_lut_tp0a; }

bool disp_set_lut_tp0a(uint8_t n)
{
    if (n < 1 || n > 31) {
        return false;
    }
    s_lut_tp0a = n;
    nvs_handle_t h;
    if (nvs_open("disp", NVS_READWRITE, &h) != ESP_OK) {
        ESP_LOGI(TAG, "disp tp0a NVS open failed; not persisted this boot");
        return true;
    }
    nvs_set_u8(h, DISP_NVS_KEY_TP0A, n);
    nvs_commit(h);
    nvs_close(h);
    return true;
}

#define DISP_NVS_NAMESPACE "disp"
#define DISP_NVS_KEY_FLIP "flip"
static bool s_disp_flip = false;

static void disp_load_flip(void)
{
    nvs_handle_t h;
    if (nvs_open(DISP_NVS_NAMESPACE, NVS_READONLY, &h) != ESP_OK) {
        return; // never set yet; stays false (normal orientation)
    }
    uint8_t v = 0;
    if (nvs_get_u8(h, DISP_NVS_KEY_FLIP, &v) == ESP_OK) {
        s_disp_flip = (v != 0);
    }
    nvs_close(h);
}

bool disp_get_flip(void) { return s_disp_flip; }

/* Serializes every public refresh entry point. */
static SemaphoreHandle_t s_mutex;

static void disp_lock(void)
{
    if (s_mutex != NULL) {
        xSemaphoreTake(s_mutex, portMAX_DELAY);
    }
}

static void disp_unlock(void)
{
    if (s_mutex != NULL) {
        xSemaphoreGive(s_mutex);
    }
}

// Fixed waits used when BUSY never asserts; healthy refreshes take about 455 ms (partial) and
// 3426 ms (full).
#define PAGER_UI_BUSY_FALLBACK_GRACE_US (20 * 1000) // grace to see BUSY assert
#define PAGER_UI_BUSY_FALLBACK_PARTIAL_MS 700
#define PAGER_UI_BUSY_FALLBACK_FULL_MS 3500

static bool s_busy_fallback_logged = false;

// Call site of the BUSY wait in progress, for timeout logs (one wait at a time under the mutex).
static const char *s_busy_tag = "?";
static void disp_set_busy_tag(const char *tag) { s_busy_tag = tag; }

// Per-call BUSY timeout; shortened by disp_wait_busy_max().
static int64_t s_busy_timeout_us = PAGER_UI_BUSY_TIMEOUT_US;

// CONFIG_FREERTOS_HZ=100: vTaskDelay(1) blocks 0-10 ms, so add a tick to guarantee at least ms.
static void disp_delay_at_least_ms(uint32_t ms)
{
    vTaskDelay((ms + portTICK_PERIOD_MS - 1) / portTICK_PERIOD_MS + 1);
}

// Weak no-op; ui.c overrides it to wait for the LTE uplink to go quiet.
__attribute__((weak)) void disp_pre_write_gate_hook(void) {}

// fallback_ms != 0: an update is in flight; if BUSY never asserts within the grace period, wait
// fallback_ms instead of trusting an idle read.
static bool disp_wait_busy_fb(uint32_t fallback_ms)
{
    int64_t start = esp_timer_get_time();
    int entry_level = gpio_get_level(PAGER_PIN_DISP_BUSY);
    int iters = 0;

    if (fallback_ms != 0) {
        bool asserted = false;
        while (esp_timer_get_time() - start <= PAGER_UI_BUSY_FALLBACK_GRACE_US) {
            if (gpio_get_level(PAGER_PIN_DISP_BUSY) == 1) {
                asserted = true;
                break;
            }
            vTaskDelay(pdMS_TO_TICKS(2));
        }
        if (!asserted) {
            if (!s_busy_fallback_logged) {
                ESP_LOGI(TAG, "BUSY line never asserted; using fixed waits (check the IO18 wire)");
                s_busy_fallback_logged = true;
            }
            uint32_t waited_ms = 0;
            while (waited_ms < fallback_ms) {
                uint32_t step = (fallback_ms - waited_ms < 10) ? (fallback_ms - waited_ms) : 10;
                vTaskDelay(pdMS_TO_TICKS(step));
                waited_ms += step;
            }
            ESP_LOGD(TAG, "BUSY: entry=%d fixed-wait=%lu ms (fallback, no BUSY assert seen)",
                     entry_level, (unsigned long) fallback_ms);
            return true;
        }
    }

    // BUSY high = busy.
    while (gpio_get_level(PAGER_PIN_DISP_BUSY) == 1) {
        iters++;
        if (esp_timer_get_time() - start > s_busy_timeout_us) {
            int64_t elapsed_ms = (esp_timer_get_time() - start) / 1000;
            ESP_LOGI(TAG, "BUSY: %s entry=%d timed out after %d iters (~%lld ms)", s_busy_tag,
                     entry_level, iters, elapsed_ms);
            flightrec_event('D', entry_level, (int32_t) elapsed_ms);
            s_busy_timeout_count++;
            return false;
        }
        vTaskDelay(pdMS_TO_TICKS(10));
    }
    ESP_LOGI(TAG, "BUSY: entry=%d exit=%d iters=%d elapsed=%lld us", entry_level,
             gpio_get_level(PAGER_PIN_DISP_BUSY), iters, esp_timer_get_time() - start);
    return true;
}

// No update in flight at these call sites, so no fixed-wait fallback.
static bool disp_wait_busy(void) { return disp_wait_busy_fb(0); }

// disp_wait_busy() with a shorter timeout.
static bool disp_wait_busy_max(int64_t max_us)
{
    int64_t saved = s_busy_timeout_us;
    s_busy_timeout_us = max_us;
    bool ok = disp_wait_busy_fb(0);
    s_busy_timeout_us = saved;
    return ok;
}

// SW-reset BUSY takes about 10 ms.
#define PAGER_UI_BUSY_RESET_TIMEOUT_US (200 * 1000)

static void disp_send_cmd(uint8_t cmd)
{
    gpio_set_level(PAGER_PIN_DISP_DC, 0);
    spi_transaction_t t = { 0 };
    t.length = 8;
    t.tx_buffer = &cmd;
    spi_device_polling_transmit(s_spi, &t);
}

static void disp_send_data(const uint8_t *data, size_t len)
{
    if (len == 0) {
        return;
    }
    gpio_set_level(PAGER_PIN_DISP_DC, 1);
    spi_transaction_t t = { 0 };
    t.length = len * 8;
    t.tx_buffer = data;
    spi_device_polling_transmit(s_spi, &t);
}

static void disp_send_data1(uint8_t b) { disp_send_data(&b, 1); }

// Splits a write into GFX_FB_ROW_BYTES chunks (bus max_transfer_sz; the non-DMA FIFO limit is 64
// bytes). `data` may be in flash because the bus is non-DMA.
static void disp_send_data_chunked(const uint8_t *data, size_t len)
{
    while (len > 0) {
        size_t n = len < GFX_FB_ROW_BYTES ? len : GFX_FB_ROW_BYTES;
        disp_send_data(data, n);
        data += n;
        len -= n;
    }
}

// Persists the setting; does not refresh. The caller must full-refresh to avoid a torn frame.
void disp_set_flip(bool on)
{
    disp_lock();
    s_disp_flip = on;
    disp_unlock();
    nvs_handle_t h;
    if (nvs_open(DISP_NVS_NAMESPACE, NVS_READWRITE, &h) != ESP_OK) {
        ESP_LOGI(TAG, "disp_flip NVS open failed; new orientation not persisted this boot");
        return;
    }
    nvs_set_u8(h, DISP_NVS_KEY_FLIP, (uint8_t) on);
    nvs_commit(h);
    nvs_close(h);
}

// Sends native rows win_first..win_last of src (source space) to the current RAM window,
// reordered when flipped; the only place flip is applied.
static void disp_send_fb_rows(const uint8_t src[][GFX_FB_ROW_BYTES], int win_first, int win_last)
{
    for (int r = win_first; r <= win_last; r++) {
        if (!s_disp_flip) {
            disp_send_data(src[r], GFX_FB_ROW_BYTES);
        } else {
            uint8_t tmp[GFX_FB_ROW_BYTES];
            disp_flip_row(src[disp_flip_src_row(r, GFX_FB_ROWS)], tmp, GFX_FB_ROW_BYTES);
            disp_send_data(tmp, GFX_FB_ROW_BYTES);
        }
    }
}

static void disp_power_on(void)
{
    // Power effect: enables the panel regulator (ENA, active-high).
    if (board_disp_vcc_en_pin() >= 0) { // carrier: panel 3V3 is always on, IO12 is FL_PWM
        gpio_set_level(PAGER_PIN_DISP_VCC_EN, PAGER_DISP_VCC_EN_ON);
    }
    vTaskDelay(pdMS_TO_TICKS(10));
}

// RST low at least 10 ms, then wait for BUSY low before any command.
static void disp_hw_reset(void)
{
    gpio_set_level(PAGER_PIN_DISP_RST, 0);
    disp_delay_at_least_ms(10);
    gpio_set_level(PAGER_PIN_DISP_RST, 1);
    disp_delay_at_least_ms(1);
    const char *saved_tag = s_busy_tag;
    disp_set_busy_tag("post-hw-reset");
    (void) disp_wait_busy_max(PAGER_UI_BUSY_RESET_TIMEOUT_US);
    disp_set_busy_tag(saved_tag);
}

static void disp_set_ram_window(uint16_t y_start, uint16_t y_end)
{
    // X is always the full 16 bytes (128 px); only Y is windowed.
    uint8_t xr[2] = { 0x00, 0x0F };
    disp_send_cmd(0x44);
    disp_send_data(xr, 2);

    uint8_t yr[4] = { (uint8_t) (y_start & 0xFF), (uint8_t) (y_start >> 8),
                       (uint8_t) (y_end & 0xFF), (uint8_t) (y_end >> 8) };
    disp_send_cmd(0x45);
    disp_send_data(yr, 4);

    disp_send_cmd(0x4E);
    disp_send_data1(0x00);
    uint8_t yc[2] = { (uint8_t) (y_start & 0xFF), (uint8_t) (y_start >> 8) };
    disp_send_cmd(0x4F);
    disp_send_data(yc, 2);
}

// Registers set after the SW reset: MUX, data-entry mode, RAM window, border, update control,
// temperature source, RAM counters.
static void disp_send_init_registers(void)
{
    uint8_t mux[3] = { 0x27, 0x01, 0x00 };
    disp_send_cmd(0x01);
    disp_send_data(mux, 3);

    disp_send_cmd(0x11);
    disp_send_data1(0x03); // X increment, Y increment

    disp_set_ram_window(0, 295);

    disp_send_cmd(0x3C);
    disp_send_data1(0x05); // border follows LUT

    uint8_t duc1[2] = { 0x00, 0x80 };
    disp_send_cmd(0x21);
    disp_send_data(duc1, 2);

    disp_send_cmd(0x18);
    disp_send_data1(0x80); // internal temperature sensor

    disp_send_cmd(0x4E);
    disp_send_data1(0x00);
    uint8_t yc0[2] = { 0x00, 0x00 };
    disp_send_cmd(0x4F);
    disp_send_data(yc0, 2);
}

// Runs the SSD1680 init sequence (docs/PROTOCOL.md §6) after VCC on and a hardware reset.
static bool disp_run_init_sequence(void)
{
    disp_send_cmd(0x12); // SW reset
    if (!disp_wait_busy()) {
        return false;
    }
    disp_send_init_registers();
    return disp_wait_busy();
}

static void mark_display_dead(void)
{
    s_display_dead = true;
    if (!s_display_dead_logged) {
        ESP_LOGI(TAG, "display BUSY timeout after reset+re-init retry — marking dead for "
                      "this boot; device continues headless (network/replies/acks unaffected)");
        s_display_dead_logged = true;
    }
}

// Re-sends the init registers before each refresh; a register loss garbles every later frame.
// 0x12 resets registers only, not RAM, so the two-plane state survives.
static bool disp_pre_refresh_reset(const char *who)
{
    disp_set_busy_tag(who);
    if (disp_run_init_sequence()) {
        return true;
    }
    ESP_LOGI(TAG, "%s: pre-refresh re-init BUSY timeout; attempting one reset+re-init", who);
    disp_hw_reset();
    if (!disp_run_init_sequence()) {
        mark_display_dead();
        return false;
    }
    return true;
}

// Update-mode bytes: full 0xF7, partial 0xFF (reference 0xF7/0xFC plus bits 1+0 to power down).
// Rule 1: every RAM write is preceded by disp_set_ram_window(); the address counters run to the
// window end.
// Rule 2: after a partial, write the displayed image to both RAM planes (updates toggle them).

static void full_refresh_locked(uint8_t tag, bool upgraded)
{
    if (s_display_dead) {
        return;
    }

    // Wait for the LTE uplink to go quiet before any panel command.
    disp_pre_write_gate_hook();

    // Re-arm the SSD1680 registers before touching RAM.
    if (!disp_pre_refresh_reset("full refresh")) {
        return;
    }

    // Snapshot before blocking on BUSY so the commit reflects what was sent.
    for (int r = 0; r < GFX_FB_ROWS; r++) {
        memcpy(s_fb_snap[r], gfx_fb_native_row(r), GFX_FB_ROW_BYTES);
    }

    // disp_send_fb_rows() applies the flip; the full window (0..295) is a mirror fixed point.
    disp_set_ram_window(0, 295);
    disp_send_cmd(0x24);
    disp_send_fb_rows(s_fb_snap, 0, GFX_FB_ROWS - 1);
    disp_set_ram_window(0, 295); // rule 1
    disp_send_cmd(0x26);
    disp_send_fb_rows(s_fb_snap, 0, GFX_FB_ROWS - 1);
    disp_send_cmd(0x3C);
    disp_send_data1(0x05);
    disp_send_cmd(0x22);
    disp_send_data1(0xF7); // update mode, PROTOCOL.md §6
    disp_send_cmd(0x20);
    // Update in flight: fixed-wait fallback if BUSY never asserts.
    disp_set_busy_tag("full refresh update");
    bool recovered = false;
    if (!disp_wait_busy_fb(PAGER_UI_BUSY_FALLBACK_FULL_MS)) {
        ESP_LOGI(TAG, "full refresh BUSY timeout; attempting one reset+re-init");
        disp_hw_reset();
        if (!disp_run_init_sequence() || !disp_wait_busy()) {
            mark_display_dead();
            return;
        }
        recovered = true;
    }

    // A full update does not read the previous-image plane, so no re-sync is needed.
    ESP_LOGI(TAG, "full: %d rows", GFX_FB_ROWS);

    // Commit the snapshot, not a fresh read: the framebuffer may change during the BUSY wait.
    for (int r = 0; r < GFX_FB_ROWS; r++) {
        memcpy(s_fb_old[r], s_fb_snap[r], GFX_FB_ROW_BYTES);
    }
    s_partial_count = 0;
    refreshpol_note_full(); // logs reason + dirty, resets the policy's dirty counter
    // Any completed full refresh satisfies a pending forced full.
    s_force_full = false;

    uint8_t effective_tag = recovered ? 4 : tag;
    s_cnt_full++;
    if (upgraded) {
        s_cnt_forced_full++;
    }
    disp_refresh_ring_push(upgraded ? 'U' : 'F', effective_tag);
    ESP_LOGI(TAG, "disp: full refresh (tag=%u)", (unsigned) effective_tag);
}

// The 0x26 re-sync and the s_fb_old commit use s_fb_snap, never a fresh framebuffer read taken
// after the BUSY wait.
static void partial_refresh_locked(uint8_t tag)
{
    if (s_display_dead) {
        return;
    }

    // Diff first: this runs on every UI loop iteration, so the gate and register re-arm must only
    // run when there is something to write.
    int first = -1, last = -1;
    for (int r = 0; r < GFX_FB_ROWS; r++) {
        if (memcmp(gfx_fb_native_row(r), s_fb_old[r], GFX_FB_ROW_BYTES) != 0) {
            if (first < 0) {
                first = r;
            }
            last = r;
        }
    }
    if (first < 0) {
        return; // nothing changed, not worth a refresh or a cadence tick
    }

    // Wait for the LTE uplink to go quiet.
    disp_pre_write_gate_hook();

    // Re-arm registers; 0x12 leaves RAM intact.
    if (!disp_pre_refresh_reset("partial refresh")) {
        return;
    }

    // Widen to whole PAGER_UI_PARTIAL_ROW_ALIGN-row chunks; padding rows resend unchanged content.
    int pre_first = first, pre_last = last;
    first -= first % PAGER_UI_PARTIAL_ROW_ALIGN;
    last += (PAGER_UI_PARTIAL_ROW_ALIGN - 1) - (last % PAGER_UI_PARTIAL_ROW_ALIGN);
    if (last >= GFX_FB_ROWS) {
        last = GFX_FB_ROWS - 1;
    }

    // Screen x = 295 - native row, so the last native row is the lowest screen x.
    int x0 = (GFX_FB_ROWS - 1) - last;
    int x1 = (GFX_FB_ROWS - 1) - first;
    ESP_LOGI(TAG, "partial: pre-align rows %d..%d, native rows %d..%d (%d rows) = screen x %d..%d, again=%d",
             pre_first, pre_last, first, last, last - first + 1, x0, x1, (int) s_partial_write_again);

    // Snapshot in source space (the diff never knows about flip).
    for (int r = first; r <= last; r++) {
        memcpy(s_fb_snap[r], gfx_fb_native_row(r), GFX_FB_ROW_BYTES);
    }

    // With flip, the programmed window is the mirror of the source window; mirror after the
    // alignment widening so it stays 8-row aligned.
    int win_first = first, win_last = last;
    if (s_disp_flip) {
        disp_flip_mirror_window(first, last, GFX_FB_ROWS, &win_first, &win_last);
    }

    disp_set_ram_window((uint16_t) win_first, (uint16_t) win_last);
    disp_send_cmd(0x24);
    disp_send_fb_rows(s_fb_snap, win_first, win_last);
    disp_send_cmd(0x3C);
    disp_send_data1(0x80); // HiZ border for partial, PROTOCOL.md §6
    // The 0x12 in disp_pre_refresh_reset() wiped any loaded LUT, so it is re-sent every partial.
    if (s_partial_use_lut) {
        disp_send_cmd(0x32);
        uint8_t lut[WF_PARTIAL_2IN9_LUT_LEN];
        memcpy(lut, WF_PARTIAL_2IN9, sizeof lut);
        lut[DISP_LUT_TP0A_IDX] = s_lut_tp0a;
        disp_send_data_chunked(lut, sizeof lut);
        disp_send_cmd(0x22);
        disp_send_data1(0xCF); // load LUT from host write (vs. 0xFF = OTP/MCU default)
        ESP_LOGI(TAG, "partial: lut mode (host-written WF_PARTIAL_2IN9, 0x22=0xCF)");
    } else {
        disp_send_cmd(0x22);
        disp_send_data1(0xFF); // load LUT from OTP
        ESP_LOGI(TAG, "partial: otp mode (no host LUT)");
    }
    disp_send_cmd(0x20);
    // Update in flight: fixed-wait fallback if BUSY never asserts.
    disp_set_busy_tag("partial refresh update");
    if (!disp_wait_busy_fb(PAGER_UI_BUSY_FALLBACK_PARTIAL_MS)) {
        ESP_LOGI(TAG, "partial refresh BUSY timeout; attempting one reset+re-init");
        disp_hw_reset();
        if (!disp_run_init_sequence() || !disp_wait_busy()) {
            mark_display_dead();
            return;
        }
        // Re-init invalidates the shadow plane; force the next refresh to be full.
        s_force_full = true;
        return;
    }

    // Re-sync the old RAM plane and the shadow to exactly what was sent.
    disp_set_ram_window((uint16_t) win_first, (uint16_t) win_last);
    disp_send_cmd(0x26);
    disp_send_fb_rows(s_fb_snap, win_first, win_last);
    for (int r = first; r <= last; r++) {
        memcpy(s_fb_old[r], s_fb_snap[r], GFX_FB_ROW_BYTES);
    }

    // Updates toggle which RAM plane 0x24/0x26 address, so also write the image to the 0x24 plane
    // (after 0x26 above); otherwise adjacent-band partials garble.
    if (s_partial_write_again) {
        disp_set_ram_window((uint16_t) win_first, (uint16_t) win_last);
        disp_send_cmd(0x24);
        disp_send_fb_rows(s_fb_snap, win_first, win_last);
    }

    s_partial_count++;
    refreshpol_note_partial();

    s_cnt_partial++;
    disp_refresh_ring_push('P', tag);
}

// One-shot ~200 ms BUSY transition trace after rail-on; auto-disarms.
static volatile bool s_busy_trace_armed = false;

void disp_debug_arm_busy_trace(void) { s_busy_trace_armed = true; }

static void disp_log_busy_settle_once(void)
{
    s_busy_trace_armed = false;
    int64_t t0 = esp_timer_get_time();
    int last = gpio_get_level(PAGER_PIN_DISP_BUSY);
    ESP_LOGI(TAG, "disp: busytrace: BUSY=%d at +0 us (right after rail-on)", last);
    while (esp_timer_get_time() - t0 < 200000) {
        int lvl = gpio_get_level(PAGER_PIN_DISP_BUSY);
        if (lvl != last) {
            ESP_LOGI(TAG, "disp: busytrace: BUSY %d->%d at +%lld us", last, lvl,
                     esp_timer_get_time() - t0);
            last = lvl;
        }
        vTaskDelay(pdMS_TO_TICKS(1));
    }
    ESP_LOGI(TAG, "disp: busytrace: BUSY=%d at +200000 us (end of trace)", last);
}

// Reloads both RAM planes from s_fb_old after a rail cycle (no 0x20, so the glass does not move).
// False if the pre-refresh re-init fails BUSY.
static bool restore_ram_planes_locked(void)
{
    // Datasheet power-on sequence: RST low >= 10 ms, then BUSY low. A bare SW reset after a
    // light-sleep rail cycle leaves BUSY stuck.
    if (s_busy_trace_armed) {
        disp_log_busy_settle_once();
    }
    // Up to 3 hw-reset attempts with short BUSY waits, then the full-timeout path.
    bool ok = false;
    for (int attempt = 0; attempt < 3 && !ok; attempt++) {
        disp_hw_reset();
        disp_set_busy_tag("power-loss restore");
        disp_send_cmd(0x12); // SW reset
        ok = disp_wait_busy_max(PAGER_UI_BUSY_RESET_TIMEOUT_US);
        if (ok) {
            disp_send_init_registers();
            ok = disp_wait_busy_max(PAGER_UI_BUSY_RESET_TIMEOUT_US);
        }
        if (!ok) {
            ESP_LOGI(TAG, "power-loss restore: attempt %d BUSY timeout, hw reset + retry",
                     attempt + 1);
        }
    }
    if (!ok) {
        disp_hw_reset();
        if (!disp_pre_refresh_reset("power-loss restore")) {
            return false;
        }
    }

    // Restore both planes in the orientation the glass shows (same flip flag as the last refresh).
    disp_set_ram_window(0, 295);
    disp_send_cmd(0x24);
    disp_send_fb_rows(s_fb_old, 0, GFX_FB_ROWS - 1);
    disp_set_ram_window(0, 295); // rule 1
    disp_send_cmd(0x26);
    disp_send_fb_rows(s_fb_old, 0, GFX_FB_ROWS - 1);
    return true;
}

static void disp_gpio_init(void)
{
    // On the carrier IO12 (ENA) and IO38 (SRCS) are FL_PWM / CHG_STAT1; leave them alone.
    const bool breadboard_pins = !board_is_carrier();
    gpio_config_t out_cfg = {
        .pin_bit_mask = (1ULL << PAGER_PIN_DISP_RST) | (1ULL << PAGER_PIN_DISP_DC) |
                        (breadboard_pins ? ((1ULL << PAGER_PIN_DISP_VCC_EN) | (1ULL << PAGER_PIN_DISP_SRCS)) : 0ULL),
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    gpio_config(&out_cfg);

    // CONFIG_ESP_SLEEP_GPIO_RESET_WORKAROUND isolates GPIOs in light sleep; keep RST, DC, CS and
    // VCC_EN driven so they cannot float. SPI clock/data pads are peripheral-muxed and idle.
    gpio_sleep_sel_dis((gpio_num_t) PAGER_PIN_DISP_RST);
    gpio_sleep_sel_dis((gpio_num_t) PAGER_PIN_DISP_DC);
    gpio_sleep_sel_dis((gpio_num_t) PAGER_PIN_DISP_CS);
    if (breadboard_pins) {
        gpio_sleep_sel_dis((gpio_num_t) PAGER_PIN_DISP_VCC_EN);
    }

    gpio_config_t busy_cfg = {
        .pin_bit_mask = (1ULL << PAGER_PIN_DISP_BUSY),
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    gpio_config(&busy_cfg);
    // BUSY is also excluded from sleep isolation so its first read after wake is valid.
    gpio_sleep_sel_dis((gpio_num_t) PAGER_PIN_DISP_BUSY);

    // ENA is owned by rail.c. The eInk Friend SRAM shares SCK/MOSI; keep it deselected (CS high).
    if (breadboard_pins) {
        gpio_set_level(PAGER_PIN_DISP_SRCS, 1);
        gpio_sleep_sel_dis((gpio_num_t) PAGER_PIN_DISP_SRCS);
    }
}

// `disptest pins`: leaves SCK/MOSI/CS off the SPI matrix; the caller must reboot afterwards.
static void probe_busy(const char *what)
{
    printf("disptest pins:   %s: BUSY=%d\n", what, gpio_get_level(PAGER_PIN_DISP_BUSY));
}

void disp_pin_probe(uint32_t ms)
{
    static const struct { const char *name; int io; int j1; bool end_high; } pads[] = {
        { "RST", PAGER_PIN_DISP_RST, 10, true },  { "DC", PAGER_PIN_DISP_DC, 11, true },
        { "CS", PAGER_PIN_DISP_CS, 12, true },    { "SCK", PAGER_PIN_DISP_SCK, 13, false },
        { "MOSI", PAGER_PIN_DISP_MOSI, 14, false },
    };
    disp_lock(); // never unlocked; caller reboots
    gpio_num_t busy = (gpio_num_t) PAGER_PIN_DISP_BUSY;
    gpio_set_direction(busy, GPIO_MODE_INPUT);
    gpio_set_pull_mode(busy, GPIO_PULLUP_ONLY);
    vTaskDelay(pdMS_TO_TICKS(5));
    int up = gpio_get_level(busy);
    gpio_set_pull_mode(busy, GPIO_PULLDOWN_ONLY);
    vTaskDelay(pdMS_TO_TICKS(5));
    int down = gpio_get_level(busy);
    gpio_set_pull_mode(busy, GPIO_FLOATING);
    const char *verdict = (up == 1 && down == 0) ? "FLOATING (not driven / not connected)"
                          : (up == 0 && down == 0) ? "driven low"
                          : (up == 1 && down == 1) ? "driven high"
                                                   : "inconsistent";
    printf("BUSY (J1 pin 9): pullup->%d pulldown->%d => %s\n", up, down, verdict);

    for (size_t i = 0; i < sizeof(pads) / sizeof(pads[0]); i++) {
        gpio_num_t pin = (gpio_num_t) pads[i].io;
        gpio_reset_pin(pin); // detaches the SPI/GPIO-matrix route
        gpio_set_direction(pin, GPIO_MODE_OUTPUT);
        for (int lvl = 1; lvl >= 0; lvl--) {
            printf("disptest pins: %s %s now (panel J1 pin %d, ESP IO %d) for %u ms\n", pads[i].name,
                   lvl ? "HIGH" : "LOW", pads[i].j1, pads[i].io, (unsigned) ms);
            gpio_set_level(pin, lvl);
            vTaskDelay(pdMS_TO_TICKS(ms));
            probe_busy(lvl ? "high" : "low");
        }
        gpio_set_level(pin, pads[i].end_high);
    }
    printf("disptest pins: done, rebooting to restore SPI\n");
    fflush(stdout);
    vTaskDelay(pdMS_TO_TICKS(200));
    esp_restart();
}

static bool disp_spi_init(void)
{
    spi_bus_config_t buscfg = {
        .mosi_io_num = PAGER_PIN_DISP_MOSI,
        .miso_io_num = board_disp_miso_pin(), // -1 on the carrier (IO42 is CHG_STAT2)
        .sclk_io_num = PAGER_PIN_DISP_SCK,
        .quadwp_io_num = -1,
        .quadhd_io_num = -1,
        .max_transfer_sz = GFX_FB_ROW_BYTES,
    };
    esp_err_t err = spi_bus_initialize(SPI2_HOST, &buscfg, SPI_DMA_DISABLED);
    if (err != ESP_OK) {
        ESP_LOGI(TAG, "spi_bus_initialize failed: %d", (int) err);
        return false;
    }

    spi_device_interface_config_t devcfg = {
        .clock_speed_hz = 4 * 1000 * 1000,
        .mode = 0,
        .spics_io_num = PAGER_PIN_DISP_CS,
        .queue_size = 1,
    };
    err = spi_bus_add_device(SPI2_HOST, &devcfg, &s_spi);
    if (err != ESP_OK) {
        ESP_LOGI(TAG, "spi_bus_add_device failed: %d", (int) err);
        return false;
    }
    return true;
}

bool disp_init(void)
{
    if (s_mutex == NULL) {
        s_mutex = xSemaphoreCreateMutex();
    }
    disp_load_lut();
    disp_load_tp0a();
    disp_load_flip();
    disp_gpio_init();
    if (!s_spi_ready) {
        s_spi_ready = disp_spi_init();
    }

    if (!s_spi_ready) {
        mark_display_dead();
        return false;
    }

    ESP_LOGD(TAG, "BUSY raw level before power-on: %d", gpio_get_level(PAGER_PIN_DISP_BUSY));

    // Power effect: VCC on for the duration of reset+init (a few tens of ms).
    disp_power_on();
    ESP_LOGD(TAG, "BUSY raw level after power-on (pre-reset): %d",
             gpio_get_level(PAGER_PIN_DISP_BUSY));
    disp_hw_reset();
    ESP_LOGD(TAG, "BUSY raw level right after hw_reset (pre-command): %d",
             gpio_get_level(PAGER_PIN_DISP_BUSY));
    disp_set_busy_tag("boot init");
    if (!disp_run_init_sequence()) {
        ESP_LOGI(TAG, "init sequence BUSY timeout; retrying once");
        disp_hw_reset();
        if (!disp_run_init_sequence()) {
            mark_display_dead();
            return false;
        }
    }

    s_force_full = true;
    ESP_LOGI(TAG, "display init OK");
    return true;
}

bool disp_is_dead(void) { return s_display_dead; }

// Power effect: one SW-reset BUSY wait (~10 ms) plus one whole-panel RAM write; no glass movement.
// Falls back to a forced full refresh if the restore fails.
void disp_note_power_loss(void)
{
    disp_lock();
    if (s_display_dead) {
        disp_unlock();
        return;
    }
    // Start of the power-loss-to-render latency measurement.
    s_power_loss_render_pending = true;
    s_power_loss_t0_us = esp_timer_get_time();
    if (restore_ram_planes_locked()) {
        ESP_LOGI(TAG, "disp: RAM planes restored after rail power loss - next refresh is a partial");
    } else {
        ESP_LOGI(TAG, "disp: RAM restore failed after rail power loss - forcing a full refresh");
        s_force_full = true;
    }
    disp_unlock();
}

// SSD1680 deep-sleep mode 1 (0x10, 0x01): RAM kept, only a hardware reset wakes it.
// Power effect: panel controller to ~uA.
void disp_panel_deep_sleep(void)
{
    disp_lock();
    (void) disp_wait_busy_max(PAGER_UI_BUSY_RESET_TIMEOUT_US);
    disp_send_cmd(0x10);
    disp_send_data1(0x01);
    disp_unlock();
}

void disp_full_refresh(void)
{
    disp_lock();
    full_refresh_locked(1, false); // tag 1
    disp_unlock();
}

void disp_request_full(void)
{
    disp_lock();
    s_force_full = true;
    disp_unlock();
}

void disp_partial_refresh(void)
{
    disp_lock();
    if (s_force_full) {
        s_force_full = false;
        full_refresh_locked(2, true); // tag 2, upgraded
    } else {
        partial_refresh_locked(2); // tag 2
    }
    disp_unlock();
}

void disp_fault_inject_swreset(void)
{
    disp_lock();
    if (!s_spi_ready) {
        disp_unlock();
        return;
    }
    disp_send_cmd(0x12); // SW reset only
    disp_wait_busy();
    disp_unlock();
}

void disp_set_partial_write_again(bool on) { s_partial_write_again = on; }

bool disp_partial_write_again(void) { return s_partial_write_again; }

int disp_dirty_rows(void)
{
    disp_lock();
    int count = 0;
    for (int r = 0; r < GFX_FB_ROWS; r++) {
        if (memcmp(gfx_fb_native_row(r), s_fb_old[r], GFX_FB_ROW_BYTES) != 0) {
            count++;
        }
    }
    disp_unlock();
    return count;
}

uint32_t disp_partial_count(void) { return s_partial_count; }

uint32_t disp_busy_timeout_count(void) { return s_busy_timeout_count; }

void disp_get_refresh_stats(uint32_t *full, uint32_t *partial, uint32_t *upgraded)
{
    if (full != NULL) {
        *full = s_cnt_full;
    }
    if (partial != NULL) {
        *partial = s_cnt_partial;
    }
    if (upgraded != NULL) {
        *upgraded = s_cnt_forced_full;
    }
}

int disp_refresh_ring_format(char *buf, size_t cap)
{
    if (buf == NULL || cap == 0) {
        return 0;
    }
    buf[0] = '\0';
    size_t n = 0;
    uint8_t start = (s_refresh_ring_count < DISP_REFRESH_RING_LEN) ? 0 : s_refresh_ring_head;
    for (uint8_t i = 0; i < s_refresh_ring_count; i++) {
        uint8_t idx = (uint8_t) ((start + i) % DISP_REFRESH_RING_LEN);
        int w = snprintf(buf + n, cap - n, "%s%c@%u/%u", (i == 0) ? "" : " ", s_refresh_ring[idx].kind,
                          (unsigned) s_refresh_ring[idx].ms, (unsigned) s_refresh_ring[idx].tag);
        if (w < 0) {
            break;
        }
        if ((size_t) w >= cap - n) {
            n = cap - 1; // snprintf already truncated and NUL-terminated at cap
            break;
        }
        n += (size_t) w;
    }
    return (int) n;
}

void disp_reset_refresh_stats(void)
{
    s_cnt_full = 0;
    s_cnt_partial = 0;
    s_cnt_forced_full = 0;
    memset(s_refresh_ring, 0, sizeof(s_refresh_ring));
    s_refresh_ring_head = 0;
    s_refresh_ring_count = 0;
}
