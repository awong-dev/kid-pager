// Device info screen: battery, modem temperature, SIM identifiers and serving cell. Draws from
// caches, "--" for unknown values.

#include "ui.h"
#include "modes.h"
#include "net.h"
#include "bearer_try.h"
#include "devinfo_fmt.h"
#include "board.h"
#include "fuel_fmt.h"

#include <stdio.h>

#define DEVINFO_VISIBLE_ROWS 5

static int s_top = 0;

static void devinfo_on_event(ui_evt_t evt)
{
    if (evt == UI_EVT_ENTER) {
        s_top = 0;
        net_devinfo_request(); // filled after the first frame
    }
}

static int devinfo_build(char rows[][DEVINFO_ROW_LEN])
{
    net_devinfo_t d;
    net_devinfo_get(&d);
    devinfo_view_t v = {0};
    v.batt_known = modes_batt_mv_known();
    v.batt_mv = modes_get_batt_mv();
    v.have_pct = modes_get_batt_pct() >= 0;
    v.batt_pct = modes_get_batt_pct();
    v.charge = modes_get_charge_state() != 0 ? fuel_state_name((fuel_state_t) modes_get_charge_state()) : "";
    v.temp_known = modes_get_temp_dc(&v.temp_dc);
    v.imei = d.imei;
    v.imsi = d.imsi;
    v.iccid = d.iccid;
    v.msisdn = d.msisdn;
    v.opname = d.opname;
    v.have_cell = d.have_cell;
    v.mcc = d.mcc;
    v.mnc = d.mnc;
    v.tac = d.tac;
    v.ci = d.ci;
    v.band = d.band;
    v.have_rsrp = d.have_rsrp;
    v.rsrp = d.rsrp;
    v.have_rsrq = d.have_rsrq;
    v.rsrq_dtenths = d.rsrq_dtenths;
    v.carrier = net_carrier_label();
    v.apn = d.apn;
    v.bearer_known = d.apn[0] != '\0' || net_carrier_label()[0] != '\0';
    v.beam = net_bearer_beam();
    char br_note[24];
    brtry_rec_t br;
    br_note[0] = '\0';
    if (brtry_status_get(&br)) {
        brtry_ui_note(&br, br_note, sizeof(br_note));
    }
    v.bearer_note = br_note;
    v.board = board_name();
    return devinfo_rows(&v, rows, DEVINFO_MAX_ROWS);
}

static void devinfo_on_key(input_key_t key)
{
    switch (key.type) {
    case INPUT_KEY_UP:
        if (s_top > 0) {
            s_top--;
        }
        break;
    case INPUT_KEY_DOWN:
        s_top++; // clamped at render time
        break;
    case INPUT_KEY_ESC:
        ui_pop();
        break;
    default:
        break;
    }
}

static void devinfo_render(void)
{
    static char rows[DEVINFO_MAX_ROWS][DEVINFO_ROW_LEN];
    int n = devinfo_build(rows);
    int max_top = (n > DEVINFO_VISIBLE_ROWS) ? (n - DEVINFO_VISIBLE_ROWS) : 0;
    if (s_top > max_top) {
        s_top = max_top;
    }
    int y = UI_BODY_TOP + 2;
    for (int i = s_top; i < n && i < s_top + DEVINFO_VISIBLE_ROWS; i++) {
        gfx_text(0, y, GFX_FONT_NORMAL, rows[i]);
        y = ui_row_advance(y, false);
    }
    char foot[64];
    snprintf(foot, sizeof(foot), "up/down scroll  esc back  %d/%d", s_top + 1, max_top + 1);
    gfx_text(0, UI_FOOTER_Y, GFX_FONT_NORMAL, foot);
}

const ui_screen_t g_scr_devinfo = {
    .name = "devinfo",
    .render = devinfo_render,
    .on_key = devinfo_on_key,
    .on_event = devinfo_on_event,
};
