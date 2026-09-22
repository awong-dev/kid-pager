#include <stdlib.h>
#include <string.h>

#include "esp_console.h"
#include "esp_log.h"
#include "nvs_flash.h"
#include "nvs.h"
#include "esp_system.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "driver/gpio.h"
#include "driver/i2c.h"

#include <stdio.h>

#include "catrust.h"
#include "ident.h"
#include "watchdog.h"
#include "carrier.h"
#include "disp.h"
#include "gfx.h"
#include "input.h"
#include "loc.h"
#include "modes.h"
#include "net.h"
#include "pins.h"
#include "setup.h"
#include "sms.h"
#include "ui.h"

static const char *TAG = "school_pager";

// `setup <code>`: re-joins argv[1..] with single spaces, since the setup code itself contains spaces.
#define SETUP_CMD_LINE_MAX 128

static int cmd_setup(int argc, char **argv)
{
    if (argc < 2) {
        printf("usage: setup <code>\n");
        return 1;
    }

    char code[SETUP_CMD_LINE_MAX];
    size_t len = 0;
    for (int i = 1; i < argc; i++) {
        if (i > 1) {
            if (len + 1 >= sizeof(code)) {
                printf("setup code too long\n");
                return 1;
            }
            code[len++] = ' ';
        }
        size_t alen = strlen(argv[i]);
        if (len + alen >= sizeof(code)) {
            printf("setup code too long\n");
            return 1;
        }
        memcpy(code + len, argv[i], alen);
        len += alen;
    }
    code[len] = '\0';

// setup_run() restarts on success; false means it already logged the error.
    if (!setup_run(code)) {
        printf("setup failed; see the SETUP log line above, retry with a fresh code if needed\n");
        return 1;
    }
    return 0; // unreachable
}

// Debug build only: `nettest <host> <port>` dials a plain TCP socket (no TLS).
static int cmd_nettest(int argc, char **argv)
{
    esp_log_level_set("WalterModem", ESP_LOG_DEBUG); // raw AT TX:/RX: trace
    if (argc < 3 || argc > 4) {
        printf("usage: nettest <host> <port> [udp|tls|<bytes>]\n");
        return 1;
    }
    long port = strtol(argv[2], NULL, 10);
    if (port <= 0 || port > 65535) {
        printf("bad port\n");
        return 1;
    }
    bool udp = (argc == 4) && (strcmp(argv[3], "udp") == 0);
    bool tls = (argc == 4) && (strcmp(argv[3], "tls") == 0);
    long pad = (argc == 4 && !udp && !tls) ? strtol(argv[3], NULL, 10) : 0;
    bool ok = (pad > 0) ? net_check_tcp_sized(argv[1], (uint16_t) port, (size_t) pad)
                        : net_check_tcp(argv[1], (uint16_t) port, udp, tls);
    printf("nettest: %s\n", ok ? "CONNECTED" : "FAILED (see log above)");
    return ok ? 0 : 1;
}

// Debug build only: `mqtttest <host> <port>` issues a real AT+SQNSMQTTCONNECT (TLS, no validation,
static int cmd_mqtttest(int argc, char **argv)
{
    esp_log_level_set("WalterModem", ESP_LOG_DEBUG); // raw AT TX:/RX: trace
    if (argc < 3 || argc > 4) {
        return 1;
    }
    long port = strtol(argv[2], NULL, 10);
    if (port <= 0 || port > 65535) {
        printf("bad port\n");
        return 1;
    }
    int tls_mode = 0;
    if (argc == 4 && strcmp(argv[3], "ca") == 0) tls_mode = 1;
    if (argc == 4 && strcmp(argv[3], "noneca") == 0) tls_mode = 2;
    if (argc == 4 && strcmp(argv[3], "emptyca") == 0) tls_mode = 3;
    bool ok = net_check_mqtt(argv[1], (uint16_t) port, tls_mode);
    printf("mqtttest: %s\n", ok ? "CONNECTED" : "NOT CONNECTED (see log above)");
    return ok ? 0 : 1;
}

// Debug build only: `sleeptest <minutes> [yield_ms] [interval_ms]` opens a window of real light
static int cmd_sleeptest(int argc, char **argv)
{
    if (argc == 1) {
        modes_debug_sleeptest_report();
        return 0;
    }
    long m = strtol(argv[1], NULL, 10);
    if (m < 1 || m > 120) {
        return 1;
    }
    long yield_ms = 0;    // 0 = use PAGER_POST_WAKE_YIELD_MS
    long interval_ms = 0; // 0 = use the normal active/sleep interval
    if (argc >= 3) {
        yield_ms = strtol(argv[2], NULL, 10);
            return 1;
        }
    }
    if (argc >= 4) {
        interval_ms = strtol(argv[3], NULL, 10);
            return 1;
        }
    }
    return 0;
}

// Debug build only: raw AT passthrough.
static int cmd_at(int argc, char **argv)
{
    if (argc < 2) {
        printf("usage: at <command>   e.g. at AT+CIMI  (quote arguments containing commas or spaces)\n");
        return 1;
    }
    char line[160];
    size_t n = 0;
    for (int i = 1; i < argc && n + 1 < sizeof(line); i++) {
        n += (size_t) snprintf(line + n, sizeof(line) - n, "%s%s", (i > 1) ? " " : "", argv[i]);
    }
    esp_log_level_set("WalterModem", ESP_LOG_DEBUG);
    bool ok = net_debug_at(line);
    printf("at: %s\n", ok ? "OK" : "ERROR/timeout");
    return ok ? 0 : 1;
}

#endif

// `carrier` -- the APN choice; also available in Setup mode.
//   carrier              list the presets, mark the current one
//   carrier <n>          choose preset n
//   carrier custom <apn> any other APN
static int cmd_carrier(int argc, char **argv)
{
    if (argc == 1) {
        bool is_auto = carrier_get_mode() == CARRIER_MODE_AUTO;
        int cur = is_auto ? CARRIER_PRESET_AUTO
                          : (carrier_get_apn()[0] == '\0' ? CARRIER_PRESET_BLANK
                                                          : carrier_preset_index_for(carrier_get_apn()));
        for (size_t i = 0; i < carrier_preset_count(); i++) {
            const carrier_preset_t *p = carrier_preset_at(i);
            printf(" %c %u  %-22s %s\n", ((int) i == cur) ? '*' : ' ', (unsigned) i, p->label,
                   i == CARRIER_PRESET_AUTO ? "(detect from the SIM)" : (p->apn[0] ? p->apn : "(blank)"));
        }
        if (cur < 0) {
            printf(" *    custom                 %s\n", carrier_get_apn());
        }
        }
        printf("carrier <n> | carrier custom <apn>\n");
        return 0;
    }
    bool ok = false;
    if (argc == 3 && strcmp(argv[1], "custom") == 0) {
        ok = carrier_set_custom(argv[2]);
    } else if (argc == 2) {
        char *end = NULL;
        long n = strtol(argv[1], &end, 10);
        ok = (end && *end == '\0' && n >= 0) && carrier_select_preset((size_t) n);
    }
    if (!ok) {
        printf("carrier: not a preset number or a valid APN\n");
        return 1;
    }
    printf("carrier: %s. Used from the next attach: run `setup <code>` now, or restart.\n",
           carrier_get_label());
    return 0;
}

// `wake`: arm the UI-awake window so the CardKB is polled (a bench with no
// button). `key <text>`: feed bytes as if typed; "\n" = enter, "\e" = esc.
static int cmd_wake(int argc, char **argv)
{
    (void) argc;
    (void) argv;
    input_arm_awake();
    printf("wake: UI awake; the keyboard is polled now\n");
    return 0;
}

static int cmd_key(int argc, char **argv)
{
    input_arm_awake();
    for (int i = 1; i < argc; i++) {
        if (i > 1) {
            input_feed_key(' ');
        }
        for (const char *p = argv[i]; *p; p++) {
            if (p[0] == '\\' && p[1] == 'n') {
                input_feed_key(0x0D);
                p++;
            } else if (p[0] == '\\' && p[1] == 'e') {
                input_feed_key(0x1B);
                p++;
            } else {
                input_feed_key((uint8_t) *p);
            }
        }
    }
    return 0;
}

// print who ACKs. Bench aid: tells wiring faults from a dead keyboard.
static int cmd_i2cscan(int argc, char **argv)
{
    bool swap = (argc >= 2) && (strcmp(argv[1], "swap") == 0);
    if (swap) {
        i2c_driver_delete(I2C_NUM_0);
        i2c_config_t conf = {
            .mode = I2C_MODE_MASTER,
            .sda_io_num = sda,
            .scl_io_num = scl,
            .sda_pullup_en = GPIO_PULLUP_ENABLE,
            .scl_pullup_en = GPIO_PULLUP_ENABLE,
            .master.clk_speed = 100000,
        };
        i2c_param_config(I2C_NUM_0, &conf);
        i2c_driver_install(I2C_NUM_0, conf.mode, 0, 0, 0);
    }
    int found = 0, nack = 0, timeout = 0, other = 0;
    for (uint8_t a = 0x08; a <= 0x77; a++) {
        i2c_cmd_handle_t cmd = i2c_cmd_link_create();
        i2c_master_start(cmd);
        i2c_master_write_byte(cmd, (uint8_t) ((a << 1) | I2C_MASTER_WRITE), true);
        i2c_master_stop(cmd);
        i2c_cmd_link_delete(cmd);
        if (err == ESP_OK) {
            printf("i2cscan: device at 0x%02x%s\n", a,
                   a == PAGER_I2C_ADDR_CARDKB ? " (CardKB)" : a == PAGER_I2C_ADDR_LIS3DH ? " (LIS3DH)" : "");
            found++;
        } else if (err == ESP_FAIL) {
            nack++; // nobody answered: a normal empty address
        } else if (err == ESP_ERR_TIMEOUT) {
            timeout++; // the bus never completed: a line held low or floating
        } else {
            other++;
            if (other == 1) {
                printf("i2cscan: error %s at 0x%02x\n", esp_err_to_name(err), a);
            }
        }
    }
    printf("i2cscan: %d device(s) on SDA=IO%d SCL=IO%d; %d no-answer, %d timeout, %d other. "
           "Lines idle: SDA=%d SCL=%d (1 = pulled up, as they should be)\n",
           found, sda, scl, nack, timeout, other, gpio_get_level((gpio_num_t) sda),
           gpio_get_level((gpio_num_t) scl));
    if (timeout > 0) {
        printf("i2cscan: timeouts mean a line is stuck: check for a short, a swapped pair, or a "
               "keyboard powered from the wrong rail\n");
    }
    return 0;
}

static void register_input_cmds(void)
{
    const esp_console_cmd_t scan_cmd = {
        .command = "i2cscan",
        .help = "i2cscan -- list the devices that answer on the keyboard I2C bus",
        .hint = NULL,
        .func = &cmd_i2cscan,
    };
    ESP_ERROR_CHECK(esp_console_cmd_register(&scan_cmd));
    const esp_console_cmd_t wake_cmd = {
        .command = "wake",
        .help = "wake -- arm the UI-awake window so the keyboard is polled",
        .hint = NULL,
        .func = &cmd_wake,
    };
    ESP_ERROR_CHECK(esp_console_cmd_register(&wake_cmd));
    const esp_console_cmd_t key_cmd = {
        .command = "key",
        .help = "key <text> -- type text as if on the keyboard (\\n enter, \\e esc)",
        .hint = NULL,
        .func = &cmd_key,
    };
    ESP_ERROR_CHECK(esp_console_cmd_register(&key_cmd));
}

static void register_carrier_cmd(void)
{
    const esp_console_cmd_t carrier_cmd = {
        .command = "carrier",
        .help = "carrier [<n> | custom <apn>] -- choose the carrier APN (presets are built in)",
        .hint = NULL,
        .func = &cmd_carrier,
    };
    ESP_ERROR_CHECK(esp_console_cmd_register(&carrier_cmd));
}

// Starts the USB-serial REPL task that waits for `setup <code>`.
static void start_setup_console(void)
{
    esp_console_repl_t *repl = NULL;
    esp_console_repl_config_t repl_config = ESP_CONSOLE_REPL_CONFIG_DEFAULT();
    repl_config.prompt = "pager>";
    // The default 4096 stack overflows in ui_init()->gfx_init() and setup_run().
    repl_config.task_stack_size = 16384;

    // esp_console_new_repl_uart() binds to UART0's pins, not the native USB-Serial/JTAG port that
    // the board's single connector exposes.
    esp_console_dev_usb_serial_jtag_config_t usb_config =
        ESP_CONSOLE_DEV_USB_SERIAL_JTAG_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_console_new_repl_usb_serial_jtag(&usb_config, &repl_config, &repl));

    esp_console_register_help_command();

    const esp_console_cmd_t setup_cmd = {
        .command = "setup",
        .help = "setup <code> -- bootstrap fetch from a typed setup code",
        .hint = NULL,
        .func = &cmd_setup,
    };
    ESP_ERROR_CHECK(esp_console_cmd_register(&setup_cmd));
    register_carrier_cmd();
    register_input_cmds();

    const esp_console_cmd_t nettest_cmd = {
        .command = "nettest",
        .help = "nettest <host> <port> -- TEMPORARY: plain TCP (no TLS) connectivity probe",
        .hint = NULL,
        .func = &cmd_nettest,
    };
    ESP_ERROR_CHECK(esp_console_cmd_register(&nettest_cmd));

    const esp_console_cmd_t mqtttest_cmd = {
        .command = "mqtttest",
        .help = "mqtttest <host> <port> -- TEMPORARY: modem MQTT-engine TLS connect probe",
        .hint = NULL,
        .func = &cmd_mqtttest,
    };
    ESP_ERROR_CHECK(esp_console_cmd_register(&mqtttest_cmd));

    ESP_ERROR_CHECK(esp_console_start_repl(repl));
}

#ifdef PAGER_DEBUG_NO_LIGHT_SLEEP
// Debug build only: the Setup REPL in normal mode, without `setup` (it would overwrite the identity).
// nettest/mqtttest re-attach the modem and repoint s_down_topic, disrupting live paging.

// `gnsstest <seconds>` runs one location-fix attempt, bypassing the backoff/battery-floor gate;
// blocks this console task.
static int cmd_gnsstest(int argc, char **argv)
{
    if (argc != 2) {
        printf("usage: gnsstest <seconds>\n");
        return 1;
    }
    long seconds = strtol(argv[1], NULL, 10);
    if (seconds <= 0 || seconds > 120) {
        printf("seconds must be 1..120\n");
        return 1;
    }
    bool ok = loc_debug_run((uint32_t) seconds);
           ok ? "FIX" : "no fix / refused / already running - see log");
    return ok ? 0 : 1;
}
// `coverage` prints the coverage duty-cycle state.
static int cmd_coverage(int argc, char **argv)
{
    (void) argc;
    (void) argv;
    modes_coverage_debug_print();
    return 0;
}

// `cafetch <url> <sha256hex>` runs the CA fetch only (no apply); blocks this console task for up to
// 30 s.
static int cmd_cafetch(int argc, char **argv)
{
    if (argc != 3) {
        printf("usage: cafetch <url> <sha256hex>\n");
        return 1;
    }
    bool ok = catrust_debug_cafetch(argv[1], argv[2]);
    printf("cafetch: %s (see the log above for bytes/http_status/elapsed/mqtt_survived detail)\n",
           ok ? "OK (hash matched)" : "FAILED - see log");
    return ok ? 0 : 1;
}

// `smstest <number> <text>` sends one SMS bypassing the allow-list; blocks this console task for the
// AT+CMGS round trip and still writes an audit entry.
static int cmd_smstest(int argc, char **argv)
{
    if (argc != 3) {
        printf("usage: smstest <number> <text>\n");
        return 1;
    }
    bool ok = sms_debug_send(argv[1], argv[2]);
    printf("smstest: %s (see the log above for the encoding chosen and the raw modem result)\n",
           ok ? "OK" : "FAILED - see log");
    return ok ? 0 : 1;
}

// `smslist` logs the allow-list and the audit queue depth.
static int cmd_smslist(int argc, char **argv)
{
    (void) argc;
    (void) argv;
    sms_debug_list();
    printf("smslist: see the log above\n");
    return 0;
}

// Debug build: paints a known pattern and drives partial/full refreshes directly, to correlate a
// screen-x range with disp.c's "partial: native rows" log line.
#define DISPTEST_COL_PX 8
#define DISPTEST_MAX_STEP (GFX_SCREEN_W / DISPTEST_COL_PX) /* n must be < this */
#define DISPTEST_SEQ_MAX_COUNT 36
#define DISPTEST_SEQ_MIN_MS 200
#define DISPTEST_SEQ_MAX_MS 5000

static const char *DISPTEST_USAGE =
    "disptest                     -- same as `disptest info`\n"
    "disptest info                -- print again-mode, partial count, disp_dirty_rows()\n"
    "disptest again <0|1>         -- set the partial-write-again A/B flag\n"
    "disptest bars                -- paint the baseline pattern and FULL refresh\n"
    "disptest step <n>            -- invert the 8px screen column at x=n*8 (full height), then "
    "ONE partial refresh; n must be 0..(GFX_SCREEN_W/8 - 1)\n"
    "disptest seq [n0] [n1] [ms]  -- `step` for n = n0..n1, ms apart (defaults 2 12 1500; "
    "count clamped to 36, ms clamped to 200..5000)\n"
    "disptest full                -- force one full refresh of whatever is in the framebuffer\n"
    "NOTE: do not use `wake` or `key` while disptest is running -- the UI render task would "
    "repaint over the test pattern.\n";

// Baseline pattern: 8 px black and 8 px white columns across the screen.
static void disptest_paint_bars(void)
{
    gfx_clear();
    for (int x0 = 0; x0 < GFX_SCREEN_W; x0 += 2 * DISPTEST_COL_PX) {
        for (int dx = 0; dx < DISPTEST_COL_PX && x0 + dx < GFX_SCREEN_W; dx++) {
            for (int y = 0; y < GFX_SCREEN_H; y++) {
                gfx_set_pixel(x0 + dx, y, true);
            }
        }
    }
}

// Inverts the n'th 8 px screen column and fires one partial refresh; logs the native-row range it
// maps to (x -> (GFX_FB_ROWS-1)-x mirror).
static void disptest_step(int n)
{
    int x = n * DISPTEST_COL_PX;
    int x_hi = x + DISPTEST_COL_PX - 1;
    int native_lo = (GFX_FB_ROWS - 1) - x_hi;
    int native_hi = (GFX_FB_ROWS - 1) - x;
    printf("disptest: step %d asked for x %d..%d => native rows %d..%d\n", n, x, x_hi, native_lo,
           native_hi);
    gfx_invert_rect(x, 0, DISPTEST_COL_PX, GFX_SCREEN_H);
    disp_partial_refresh();
}

static int cmd_disptest(int argc, char **argv)
{
    if (argc < 2 || strcmp(argv[1], "info") == 0) {
        return 0;
    }
    if (strcmp(argv[1], "again") == 0) {
        if (argc != 3 || (strcmp(argv[2], "0") != 0 && strcmp(argv[2], "1") != 0)) {
            printf("usage: disptest again <0|1>\n");
            return 1;
        }
        disp_set_partial_write_again(strcmp(argv[2], "1") == 0);
        printf("disptest: again=%d\n", (int) disp_partial_write_again());
        return 0;
    }
    if (strcmp(argv[1], "bars") == 0) {
        if (argc != 2) {
            printf("usage: disptest bars\n");
            return 1;
        }
        disptest_paint_bars();
        disp_full_refresh();
        printf("disptest: bars painted, full refresh done\n");
        return 0;
    }
    if (strcmp(argv[1], "step") == 0) {
        if (argc != 3) {
            printf("usage: disptest step <n>\n");
            return 1;
        }
        char *end = NULL;
        long n = strtol(argv[2], &end, 10);
        if (!end || *end != '\0' || n < 0 || n >= DISPTEST_MAX_STEP) {
            printf("disptest: n must be 0..%d\n", DISPTEST_MAX_STEP - 1);
            printf("usage: disptest step <n>\n");
            return 1;
        }
        disptest_step((int) n);
        return 0;
    }
    if (strcmp(argv[1], "seq") == 0) {
        if (argc > 5) {
            printf("usage: disptest seq [n0] [n1] [ms]\n");
            return 1;
        }
        long n0 = 2, n1 = 12, ms = 1500;
        char *end = NULL;
        if (argc >= 3) {
            n0 = strtol(argv[2], &end, 10);
            if (!end || *end != '\0') {
                printf("usage: disptest seq [n0] [n1] [ms]\n");
                return 1;
            }
        }
        if (argc >= 4) {
            n1 = strtol(argv[3], &end, 10);
            if (!end || *end != '\0') {
                printf("usage: disptest seq [n0] [n1] [ms]\n");
                return 1;
            }
        }
        if (argc >= 5) {
            ms = strtol(argv[4], &end, 10);
            if (!end || *end != '\0') {
                printf("usage: disptest seq [n0] [n1] [ms]\n");
                return 1;
            }
        }
        if (n0 < 0 || n0 >= DISPTEST_MAX_STEP || n1 < 0 || n1 >= DISPTEST_MAX_STEP || n1 < n0) {
            printf("disptest: n0/n1 must be 0..%d with n1 >= n0\n", DISPTEST_MAX_STEP - 1);
            printf("usage: disptest seq [n0] [n1] [ms]\n");
            return 1;
        }
        if (n1 - n0 + 1 > DISPTEST_SEQ_MAX_COUNT) {
            n1 = n0 + DISPTEST_SEQ_MAX_COUNT - 1;
            printf("disptest: clamped to %d steps (n0=%ld n1=%ld)\n", DISPTEST_SEQ_MAX_COUNT, n0,
                   n1);
        }
        if (ms < DISPTEST_SEQ_MIN_MS) {
            ms = DISPTEST_SEQ_MIN_MS;
        } else if (ms > DISPTEST_SEQ_MAX_MS) {
            ms = DISPTEST_SEQ_MAX_MS;
        }
        printf("disptest: seq n=%ld..%ld, %ld ms apart\n", n0, n1, ms);
        for (long n = n0; n <= n1; n++) {
            disptest_step((int) n);
            if (n != n1) {
                vTaskDelay(pdMS_TO_TICKS(ms));
            }
        }
        return 0;
    }
    if (strcmp(argv[1], "full") == 0) {
        if (argc != 2) {
            printf("usage: disptest full\n");
            return 1;
        }
        disp_full_refresh();
        printf("disptest: full refresh done\n");
        return 0;
    }
    printf("%s", DISPTEST_USAGE);
    return 1;
}

static void start_normal_console(void)
{
    esp_console_repl_t *repl = NULL;
    esp_console_repl_config_t repl_config = ESP_CONSOLE_REPL_CONFIG_DEFAULT();
    repl_config.prompt = "pager>";
    repl_config.task_stack_size = 16384;

    esp_console_dev_usb_serial_jtag_config_t usb_config =
        ESP_CONSOLE_DEV_USB_SERIAL_JTAG_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_console_new_repl_usb_serial_jtag(&usb_config, &repl_config, &repl));

    esp_console_register_help_command();

    const esp_console_cmd_t nettest_cmd = {
        .command = "nettest",
        .help = "nettest <host> <port> -- TEMPORARY: plain TCP (no TLS) connectivity probe",
        .hint = NULL,
        .func = &cmd_nettest,
    };
    ESP_ERROR_CHECK(esp_console_cmd_register(&nettest_cmd));

    const esp_console_cmd_t mqtttest_cmd = {
        .command = "mqtttest",
        .help = "mqtttest <host> <port> -- TEMPORARY: modem MQTT-engine TLS connect probe",
        .hint = NULL,
        .func = &cmd_mqtttest,
    };
    ESP_ERROR_CHECK(esp_console_cmd_register(&mqtttest_cmd));

    const esp_console_cmd_t sleeptest_cmd = {
        .command = "sleeptest",
        .hint = NULL,
        .func = &cmd_sleeptest,
    };
    ESP_ERROR_CHECK(esp_console_cmd_register(&sleeptest_cmd));

    const esp_console_cmd_t at_cmd = {
        .command = "at",
        .help = "at <command> -- send one raw AT command; the reply shows in the AT trace",
        .hint = NULL,
        .func = &cmd_at,
    };
    ESP_ERROR_CHECK(esp_console_cmd_register(&at_cmd));

    register_carrier_cmd();
    register_input_cmds();

    const esp_console_cmd_t gnsstest_cmd = {
        .command = "gnsstest",
        .help = "gnsstest <seconds> -- one location-fix attempt, bypassing backoff/battery floor",
        .hint = NULL,
        .func = &cmd_gnsstest,
    };
    ESP_ERROR_CHECK(esp_console_cmd_register(&gnsstest_cmd));

    const esp_console_cmd_t coverage_cmd = {
        .command = "coverage",
        .help = "coverage -- print the coverage duty-cycle policy's state (registered/dark-for/"
                "phase/step/next-action)",
        .hint = NULL,
        .func = &cmd_coverage,
    };
    ESP_ERROR_CHECK(esp_console_cmd_register(&coverage_cmd));

    const esp_console_cmd_t cafetch_cmd = {
        .command = "cafetch",
        .help = "cafetch <url> <sha256hex> -- fetch-only CA-over-HTTPS probe, no apply",
        .hint = NULL,
        .func = &cmd_cafetch,
    };
    ESP_ERROR_CHECK(esp_console_cmd_register(&cafetch_cmd));

    const esp_console_cmd_t smstest_cmd = {
        .command = "smstest",
        .help = "smstest <number> <text> -- send one SMS bypassing the allow-list",
        .hint = NULL,
        .func = &cmd_smstest,
    };
    ESP_ERROR_CHECK(esp_console_cmd_register(&smstest_cmd));

    const esp_console_cmd_t smslist_cmd = {
        .command = "smslist",
        .help = "smslist -- print the SMS allow-list and audit queue depth",
        .hint = NULL,
        .func = &cmd_smslist,
    };
    ESP_ERROR_CHECK(esp_console_cmd_register(&smslist_cmd));

    const esp_console_cmd_t disptest_cmd = {
        .command = "disptest",
                 "deterministic e-paper partial-refresh bench harness; "
                 "run `disptest` with no args for the full usage. NOTE: do not use `wake` or "
                 "`key` while this is running -- the UI render task would repaint over the test "
                 "pattern.",
        .hint = NULL,
        .func = &cmd_disptest,
    };
    ESP_ERROR_CHECK(esp_console_cmd_register(&disptest_cmd));

    ESP_ERROR_CHECK(esp_console_start_repl(repl));
}
#endif

void app_main(void)
{
    ESP_LOGI(TAG, "school_pager boot");
    watchdog_boot(); // logs why we reset and where the loop was; arms the RTC watchdog

    // board_init() reads the board kind from NVS, so NVS comes up first.
    esp_err_t nvs_err = nvs_flash_init();
    if (nvs_err == ESP_ERR_NVS_NO_FREE_PAGES || nvs_err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        nvs_err = nvs_flash_init();
    }
    ESP_ERROR_CHECK(nvs_err);

    if (!ident_load()) {
        // No identity: Setup mode. Shows a status splash and checks the SIM once per boot;
        // `setup <code>` is the only way out.
        ESP_LOGI(TAG, "IDENT missing; starting Setup mode console");

        bool ui_ok = ui_init();
        if (ui_ok) {
            scr_greeting_set_mode(GREETING_HELLO);
            scr_greeting_set_status("booting");
            ui_push(&g_scr_greeting);
            ui_render_boot();
        }

        bool sim_ok = net_check_sim();
        ESP_LOGI(TAG, "SIM check: %s", sim_ok ? "found" : "missing");
        if (ui_ok) {
            scr_greeting_set_status(sim_ok ? "type: setup <code>" : "sim missing");
            ui_render();
        }

        if (!sim_ok) {
            // Pause so "sim missing" is readable.
            vTaskDelay(pdMS_TO_TICKS(4000));
            if (ui_ok) {
                scr_greeting_set_status("shutting down");
                ui_render();
            }
        }

        // net_check_sim() leaves the modem at NO_RF; net_bootstrap_attach() re-issues the op states.
        start_setup_console();
        for (;;) {
            vTaskDelay(pdMS_TO_TICKS(1000));
        }
    }
    ESP_LOGI(TAG, "IDENT %s sig=%d claimed=%d", ident_get_dev_id(),
             (ident_get_flags() & IDENT_FLAG_REQ_SIG) ? 1 : 0, ident_get_claimed() ? 1 : 0);

#ifdef PAGER_DEBUG_NO_LIGHT_SLEEP
    // Started before modes_boot() so the console task exists for the device's life.
    start_normal_console();
#endif

    // modes_boot() picks cold boot vs. reset recovery by validating the RTC struct (docs/PROTOCOL.md
    // §9); a light-sleep wake resumes inside modes_run() and never re-runs app_main().
    modes_boot();
    modes_run(); // never returns
}
