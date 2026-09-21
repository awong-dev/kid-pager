
#include "ui.h"

#include <stdio.h>
#include <string.h>

static char s_status[32] = "";


void scr_greeting_set_status(const char *status)
{
    if (!status) {
        s_status[0] = '\0';
        return;
    }
    snprintf(s_status, sizeof(s_status), "%s", status);
}

static void render(void)
{

}

static void on_key(input_key_t key)
{
    (void) key;
    ui_pop();
}

const ui_screen_t g_scr_greeting = {
    .name = "greeting",
    .render = render,
    .on_key = on_key,
};
