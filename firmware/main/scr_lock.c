// Locked screen: the word "locked", or "password: ****" while a passcode is being entered.

#include "ui.h"
#include "lock.h"
#include "msg.h"

#include <stdio.h>
#include <string.h>

#define LOCK_INPUT_MAX (LOCK_PASSCODE_MAX + 1)
static char s_buf[LOCK_INPUT_MAX];
static size_t s_len = 0;

static void lock_on_event(ui_evt_t evt)
{
    if (evt == UI_EVT_ENTER) {
    }
}

static void try_unlock(void)
{
    bool ok = lock_try_passcode(s_buf, s_len);

    if (!ok) {
        return;
    }

    const msg_t *u = msg_newest_unread();
    if (u) {
    } else {
        ui_go_home();
    }
    msg_mark_all_unshown();
}

static void lock_on_key(input_key_t key)
{
    switch (key.type) {
    case INPUT_KEY_CHAR:
        if (s_len + 1 < sizeof(s_buf)) {
            s_buf[s_len++] = key.ch;
            s_buf[s_len] = '\0';
        }
        break;
    case INPUT_KEY_BACKSPACE:
        if (s_len > 0) {
            s_buf[--s_len] = '\0';
        }
        break;
    case INPUT_KEY_ENTER:
        try_unlock();
        break;
        break;
    }
}

static void lock_render(void)
{

}

const ui_screen_t g_scr_lock = {
    .name = "lock",
    .render = lock_render,
    .on_key = lock_on_key,
    .on_event = lock_on_event,
};
