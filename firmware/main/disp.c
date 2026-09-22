/* SSD1680 e-paper transport (docs/PROTOCOL.md §6). */
#include "disp.h"
#include "gfx.h"
#include "pins.h"

#include <string.h>

#include "driver/gpio.h"
#include "driver/spi_master.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

static const char *TAG = "disp";


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

static bool s_partial_write_again = true;

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

static void disp_power_on(void)
{
    vTaskDelay(pdMS_TO_TICKS(10));
}

static void disp_hw_reset(void)
{
    gpio_set_level(PAGER_PIN_DISP_RST, 0);
    gpio_set_level(PAGER_PIN_DISP_RST, 1);
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

// Update-mode bytes: full 0xF7, partial 0xFF (reference 0xF7/0xFC plus bits 1+0 to power down).
// Rule 1: every RAM write is preceded by disp_set_ram_window(); the address counters run to the
// window end.
// Rule 2: after a partial, write the displayed image to both RAM planes (updates toggle them).

{
    if (s_display_dead) {
        return;
    }

    // Snapshot before blocking on BUSY so the commit reflects what was sent.
    for (int r = 0; r < GFX_FB_ROWS; r++) {
        memcpy(s_fb_snap[r], gfx_fb_native_row(r), GFX_FB_ROW_BYTES);
    }

    disp_set_ram_window(0, 295);
    disp_send_cmd(0x24);
    disp_set_ram_window(0, 295); // rule 1
    disp_send_cmd(0x26);
    disp_send_cmd(0x3C);
    disp_send_data1(0x05);
    disp_send_cmd(0x22);
    disp_send_data1(0xF7); // update mode, PROTOCOL.md §6
    disp_send_cmd(0x20);
    // Update in flight: fixed-wait fallback if BUSY never asserts.
    if (!disp_wait_busy_fb(PAGER_UI_BUSY_FALLBACK_FULL_MS)) {
        ESP_LOGI(TAG, "full refresh BUSY timeout; attempting one reset+re-init");
        disp_hw_reset();
        if (!disp_run_init_sequence() || !disp_wait_busy()) {
            mark_display_dead();
            return;
        }
    }

    // A full update does not read the previous-image plane, so no re-sync is needed.
    ESP_LOGI(TAG, "full: %d rows", GFX_FB_ROWS);

    // Commit the snapshot, not a fresh read: the framebuffer may change during the BUSY wait.
    for (int r = 0; r < GFX_FB_ROWS; r++) {
        memcpy(s_fb_old[r], s_fb_snap[r], GFX_FB_ROW_BYTES);
    }
    s_partial_count = 0;
}

// The 0x26 re-sync and the s_fb_old commit use s_fb_snap, never a fresh framebuffer read taken
// after the BUSY wait.
{
    if (s_display_dead) {
        return;
    }
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

    }
    disp_send_cmd(0x3C);
    disp_send_data1(0x80); // HiZ border for partial, PROTOCOL.md §6
    disp_send_cmd(0x20);
    // Update in flight: fixed-wait fallback if BUSY never asserts.
    if (!disp_wait_busy_fb(PAGER_UI_BUSY_FALLBACK_PARTIAL_MS)) {
        ESP_LOGI(TAG, "partial refresh BUSY timeout; attempting one reset+re-init");
        disp_hw_reset();
        if (!disp_run_init_sequence() || !disp_wait_busy()) {
            mark_display_dead();
            return;
        }
        // Re-init invalidates the shadow plane; force the next refresh to be full.
        return;
    }

    // Re-sync the old RAM plane and the shadow to exactly what was sent.
    disp_send_cmd(0x26);
    for (int r = first; r <= last; r++) {
        memcpy(s_fb_old[r], s_fb_snap[r], GFX_FB_ROW_BYTES);
    }

    // Updates toggle which RAM plane 0x24/0x26 address, so also write the image to the 0x24 plane
    // (after 0x26 above); otherwise adjacent-band partials garble.
    if (s_partial_write_again) {
        disp_send_cmd(0x24);
    }

    s_partial_count++;
}

static void disp_gpio_init(void)
{
    gpio_config_t out_cfg = {
        .pin_bit_mask = (1ULL << PAGER_PIN_DISP_RST) | (1ULL << PAGER_PIN_DISP_DC) |
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    gpio_config(&out_cfg);

    gpio_config_t busy_cfg = {
        .pin_bit_mask = (1ULL << PAGER_PIN_DISP_BUSY),
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    gpio_config(&busy_cfg);

}

static bool disp_spi_init(void)
{
    spi_bus_config_t buscfg = {
        .mosi_io_num = PAGER_PIN_DISP_MOSI,
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
    disp_gpio_init();
    if (!s_spi_ready) {
        s_spi_ready = disp_spi_init();
    }

    if (!s_spi_ready) {
        mark_display_dead();
        return false;
    }


    // Power effect: VCC on for the duration of reset+init (a few tens of ms).
    disp_power_on();
             gpio_get_level(PAGER_PIN_DISP_BUSY));
    disp_hw_reset();
             gpio_get_level(PAGER_PIN_DISP_BUSY));
    if (!disp_run_init_sequence()) {
        ESP_LOGI(TAG, "init sequence BUSY timeout; retrying once");
        disp_hw_reset();
        if (!disp_run_init_sequence()) {
            mark_display_dead();
            return false;
        }
    }

    ESP_LOGI(TAG, "display init OK");
    return true;
}

bool disp_is_dead(void) { return s_display_dead; }

void disp_full_refresh(void)
{
    disp_lock();
    disp_unlock();
}

void disp_partial_refresh(void)
{
    disp_lock();
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
