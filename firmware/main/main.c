#include "esp_log.h"
#include "modes.h"

static const char *TAG = "school_pager";

void app_main(void)
{
    ESP_LOGI(TAG, "school_pager boot");

    // modes_boot() picks cold boot vs. reset recovery by validating the RTC struct (docs/PROTOCOL.md
    // §9); a light-sleep wake resumes inside modes_run() and never re-runs app_main().
    modes_boot();
    modes_run(); // never returns
}
