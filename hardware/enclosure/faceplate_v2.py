"""Kid-pager faceplate (display cassette top plate) for the Altoids tin build, v2.

Parametric CadQuery model for the cost-reduced carrier layout: USB-C, SW1 and SW2 in one row along the
hinge edge, board end at kx 110.55. The board's only locating hole H4 sits under the glass where the plate
cannot reach; h4_spacer uses it from below, on the battery.
Frame: the Fusion assembly frame used by hardware/carrier/Assembled*.step
(tin long axis = y, hinge wall = -x, z up from the tin floor). KiCad board frame
maps as kx = world_y + 2.03, ky = world_x + 101.27.

Assumptions that must be checked against hardware:
  * display = bare GDEY029T94(-FL03) glass 79.0 x 36.7, glass top at GLASS_TOP,
    FPC leaving the +y (battery-end) short edge, centred;
  * carrier top at CARRIER_TOP, part positions from pager-carrier.kicad_pcb:
    J6 ShouHan TYPE-C 16PLT-H6.5 at (92.71, 61.09), SW1 TS-1187A-S16 at (82.42, 61.09),
    SW2 MSS12C02LS-HB2.0 at (105.28, 61.09);
  * H4 (1.10 mm NPTH at board (102.743, 72.009)) over the battery; carrier underside to battery top = H4_GAP;
  * tin interior from "Assembled V0.2.step" (Altoids bottom lid v4).
Run:  <cadquery python> faceplate_v2.py
"""
import cadquery as cq
import os

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out", "v2")

TIN_X0, TIN_X1 = -45.37, 12.28
TIN_Y0, TIN_Y1 = 66.21, 159.01
TIN_R = 13.45
CLEAR = 0.30                     # plate-to-wall clearance per side (magnets hold)

CARRIER_TOP = 10.91              # carrier PCB top, from the assembly STEP (socketed headers)
BATTERY_TOP = 8.75
GLASS_TOP = 16.00                # SH header tops (13.9) must clear the glass back (14.2)
LIP = 1.00                       # bezel thickness over the glass border
PLATE_T = 1.60                   # plate thickness elsewhere
TOP = GLASS_TOP + LIP            # planar top face: prints face-down
UNDER = TOP - PLATE_T            # plate underside away from the glass
GLASS_STACK = 1.80               # glass + front light; pocket depth below the lip
RIM_T = 1.20                     # pocket rim wall thickness

GLASS_W, GLASS_L = 36.7, 79.0
GLASS_X0 = -34.81
GLASS_Y0 = 70.81
AA_W, AA_L = 29.05, 66.89        # GDEY029T94 active area
AA_FAR_MARGIN = 3.5              # from the non-FPC (-y) glass edge; verify on part
WIN_CLEAR = 0.30
FPC_W = 24.0                     # opening in the +y rim wall for the FPC fold

def kw(kx, ky):
    return (ky - 101.27, kx - 2.03)

# board edges in world: x -43.90..5.06, y 72.24..111.14 (1.0 mm thick, no holes)
BOARD_X1 = kw(0, 106.33)[0]      # far-side edge (5.06)
BOARD_Y1 = kw(113.17, 0)[1]      # battery-end edge (111.14)
BOARD_T = 1.0
# USB-C J6: ShouHan TYPE-C 16PLT-H6.5 (C3151748) vertical receptacle, shell 8.94 (along kx) x 3.16 (along ky),
# 6.50 +-0.15 above the PCB (vendor drawing sheet 1). The shell stands 0.4 proud of the plate top; its
# opening is snug across the shell (world x) and loose along it (world y).
J6 = kw(92.71, 61.087)           # (-40.18, 90.68)
J6_SHELL = (3.16, 8.94)          # (world x, world y)
J6_H = 6.50
J6_OPEN = (J6_SHELL[0] + 2 * 0.15, J6_SHELL[1] + 2 * 0.40)
# SW1 front-light tactile: XKB TS-1187A-S16 (C42420254), 5.15 sq body, H 1.5, round button dia 2.0,
# travel 0.2 mm. The cap rests on the button by gravity; a flange under the band keeps it captive.
SW1 = kw(82.423, 61.087)         # (-40.18, 80.39)
SW1_H = 1.5
SW1_BUTTON_D = 2.0
FL_SHAFT_D = 3.6                 # shaft through the plate
SW1_HOLE = FL_SHAFT_D + 0.4
FL_NUB_D, FL_NUB_H = 1.8, 0.5    # contact nub: the shaft never lands on the switch body (0.2 travel)
FL_CAP_TOP = 18.4                # CardKB key bottoms at 19.26 with the lid closed
FL_FLANGE_D = 5.6                # under the plate band: keeps the cap captive
FL_FLANGE_T = 0.6
FL_FLANGE_GAP = 0.6              # flange top to plate underside: CARRIER_TOP may be up to 0.6 high before the
                                 # flange lifts the plate (which would hold the button down)
# SW2 power slide: ShouHan MSS12C02LS-HB2.0 (C3008584): body 6.65 (kx) x 2.75 (ky) x 1.9, knob 1.63 (kx)
# x 0.68 (ky), 2.0 above the body, travel 1.5 total along kx (= world y). Footprint origin = knob at mid travel.
SW2_KNOB = kw(105.283, 61.087)   # (-40.18, 103.25)
SW2_BODY_H = 1.9
SW2_KNOB_WH = (0.68, 1.63, 2.0)  # (world x, world y, height above the body)
SW2_TRAVEL = 0.75                # each side of mid travel
SW2_STEM = (2.6, 3.6)            # cap stem (world x, y)
SW2_SLOT = (SW2_STEM[0] + 0.3, SW2_STEM[1] + 2 * SW2_TRAVEL + 0.3)

# Stability pin at H4, a 1.10 mm NPTH at board (102.743, 72.009) = world (-29.26, 100.71): under the glass
# and past the Walter end (y 97.07), where the carrier overhangs the battery. A 1.0 mm steel pin glued into
# H4 flush with the board top drops into a blind hole in a printed disc fixed to the battery top. The disc
# props the overhang and the pin stops it sliding. Walter PCB ends at world y 97.07 (disc edge 98.71).
H4_K = (102.743, 72.009)         # board frame
H4 = kw(*H4_K)                   # (-29.26, 100.71)
BATTERY_TOP = 8.75               # Assembled V0.2.step
H4_GAP = CARRIER_TOP - BOARD_T - BATTERY_TOP   # 1.16 with CARRIER_TOP 10.91: measure on the soldered unit
H4_SPACER_D = 4.0
PIN_D = 1.0
PIN_HOLE_D = 1.05                # printed; ream with a 1.0 mm drill
H4_FLOOR = 0.3                   # disc floor under the pin hole
PIN_LEN = BOARD_T + H4_GAP - H4_FLOOR - 0.1   # cut the pin to this: flush on top, 0.1 short of the hole floor

# height datums resting on bare solder mask (the board has no mounting holes)
FOOT = kw(77.6, 59.6)            # hinge / Walter-end corner (-41.67, 75.57): inside the board's R5.45 corner arc, clear of C6
FOOT_D = 3.0
RAIL_X = (3.4, 4.9)              # far-side edge strip outside the glass rim, inside the board edge
RAIL_Y = [(73.5, 79.0), (90.2, 101.0)]   # two segments: SIM notch at y 79.4..89.7; J2 courtyard starts at y 101.45
BAT_FOOT = (-39.5, 114.0)        # 3 x 3 foot on the battery at the hinge / battery-end corner (wires pass at y >= 116)
BAT_FOOT_W = 3.0
LEG_D = 4.4                      # standoff on the 4.5 mm copper-free annulus
SCREW_HOLE = 2.4                 # M2 clearance
CBORE_D, CBORE_DEPTH = 4.0, 1.0  # M2 pan head

MAG_D, MAG_T = 6.3, 1.2          # pocket for a 6 x 1 mm disc magnet
TAB_W, TAB_T, TAB_BOTTOM = 9.0, 2.0, 8.0
MAG_Z = 11.5
WIRE_CH = (116.0, 124.0, 4.0)    # keyboard wire channel: y0, y1, depth into x
HINGE_NOTCH = [(88.0, 97.2), (128.0, 137.2)]   # lid hinge tabs, z 11.1..15.8
HINGE_NOTCH_DEPTH = 1.5
CRUSH_H, CRUSH_W = 0.45, 1.0     # crush ribs on the tab faces: 0.15 mm interference per side (sand to fit)

# The 40x20 panel board slides in from +y into grooves in two posts hanging from the plate outside the
# glass rim, over the battery. Board rotated 180 deg in plane: its FPC edge (J1/J3/J4) faces +y, SH-8 (J2)
# faces -y toward the carrier's J1, SH-2 (J5) sits at the far end -> far post opened there for the plug.
PANEL_W, PANEL_L = 40.1, 20.1
PANEL_X0 = -36.5                 # centred on the glass (-16.45)
PANEL_Y0 = 128.0                 # -y edge; FPC fold between 148 and the +y rim wall at 150
PANEL_PCB_Z = 9.6                # PCB bottom: parts up to 12.57+1 = 13.57 < glass back 14.2; battery 8.75
PANEL_T = 1.0
POST_T, POST_LIP = 2.6, 0.8      # post thickness / how far the groove reaches over the board end
GROOVE_CLEAR = 0.3               # x clearance at the board end
POST_BOTTOM = PANEL_PCB_Z - 0.5  # groove floor 0.5 thick
DETENT_H = 0.3


def box(x0, x1, y0, y1, z0, z1):
    return (cq.Workplane("XY").box(x1 - x0, y1 - y0, z1 - z0, centered=False)
            .translate((x0, y0, z0)))


def cyl_z(cx, cy, d, z0, z1):
    return (cq.Workplane("XY").circle(d / 2).extrude(z1 - z0)
            .translate((cx, cy, z0)))


def rrect(x0, x1, y0, y1, r, z0, z1):
    w, l = x1 - x0, y1 - y0
    return (cq.Workplane("XY").center((x0 + x1) / 2, (y0 + y1) / 2)
            .rect(w, l).extrude(z1 - z0).edges("|Z").fillet(r)
            .translate((0, 0, z0)))


def build_plate():
    # 1. plate body
    px0, px1 = TIN_X0 + CLEAR, TIN_X1 - CLEAR
    py0, py1 = TIN_Y0 + CLEAR, TIN_Y1 - CLEAR
    plate = rrect(px0, px1, py0, py1, TIN_R - CLEAR, UNDER, TOP)

    # 2. glass pocket + rim
    gx0, gx1 = GLASS_X0 - 0.2, GLASS_X0 + GLASS_W + 0.2
    gy0, gy1 = GLASS_Y0 - 0.2, GLASS_Y0 + GLASS_L + 0.2
    rim_bottom = GLASS_TOP - GLASS_STACK
    rim = box(gx0 - RIM_T, gx1 + RIM_T, gy0 - RIM_T, gy1 + RIM_T, rim_bottom, UNDER)
    plate = plate.union(rim)
    plate = plate.cut(box(gx0, gx1, gy0, gy1, rim_bottom - 0.1, GLASS_TOP))
    # far-side rim opened over J2's plug (world y 101.45..108.1)
    plate = plate.cut(box(gx1 - 0.01, gx1 + RIM_T + 0.1, 101.0, 108.5, rim_bottom - 0.1, UNDER + 0.01))
    # +y rim wall opened for the FPC fold
    gcx = (gx0 + gx1) / 2
    plate = plate.cut(box(gcx - FPC_W / 2, gcx + FPC_W / 2, gy1 - 0.01, gy1 + RIM_T + 0.1,
                          rim_bottom - 0.1, UNDER + 0.01))

    # 3. window over the active area
    wx0 = GLASS_X0 + (GLASS_W - AA_W) / 2 - WIN_CLEAR
    wx1 = wx0 + AA_W + 2 * WIN_CLEAR
    wy0 = GLASS_Y0 + AA_FAR_MARGIN - WIN_CLEAR
    wy1 = wy0 + AA_L + 2 * WIN_CLEAR
    plate = plate.cut(box(wx0, wx1, wy0, wy1, GLASS_TOP - 1, TOP + 1))

    # 4. hinge-band openings
    plate = plate.cut(box(J6[0] - J6_OPEN[0] / 2, J6[0] + J6_OPEN[0] / 2,
                          J6[1] - J6_OPEN[1] / 2, J6[1] + J6_OPEN[1] / 2, UNDER - 1, TOP + 1))
    plate = plate.cut(cyl_z(SW1[0], SW1[1], SW1_HOLE, UNDER - 1, TOP + 1))
    plate = plate.cut(box(SW2_KNOB[0] - SW2_SLOT[0] / 2, SW2_KNOB[0] + SW2_SLOT[0] / 2,
                          SW2_KNOB[1] - SW2_SLOT[1] / 2, SW2_KNOB[1] + SW2_SLOT[1] / 2, UNDER - 1, TOP + 1))

    # 5. height datums
    plate = plate.union(cyl_z(FOOT[0], FOOT[1], FOOT_D, CARRIER_TOP, UNDER + 0.01))
    for (ry0, ry1) in RAIL_Y:
        plate = plate.union(box(RAIL_X[0], RAIL_X[1], ry0, ry1, CARRIER_TOP, UNDER + 0.01))
    plate = plate.union(box(BAT_FOOT[0] - BAT_FOOT_W / 2, BAT_FOOT[0] + BAT_FOOT_W / 2,
                            BAT_FOOT[1] - BAT_FOOT_W / 2, BAT_FOOT[1] + BAT_FOOT_W / 2, POST_BOTTOM, UNDER + 0.01))

    # 6. panel board holder: two grooved posts over the battery, open toward +y
    x0, x1 = PANEL_X0, PANEL_X0 + PANEL_W
    y0, y1 = PANEL_Y0, PANEL_Y0 + PANEL_L
    gz0, gz1 = PANEL_PCB_Z, PANEL_PCB_Z + PANEL_T + 0.2
    for (px, sign) in ((x0, -1), (x1, +1)):
        inner = px + sign * (-POST_LIP)            # groove reaches this far over the board
        outer = px + sign * (POST_T - POST_LIP)
        plate = plate.union(box(min(inner, outer), max(inner, outer), y0 - 1.5, y1 + 0.5,
                                POST_BOTTOM, UNDER + 0.01))
        gx_in, gx_out = inner - sign * 0.01, px + sign * GROOVE_CLEAR
        plate = plate.cut(box(min(gx_in, gx_out), max(gx_in, gx_out), y0 - 0.3, y1 + 1.0, gz0, gz1))
        plate = plate.union(box(min(gx_in, gx_out), max(gx_in, gx_out), y1 + 0.3, y1 + 1.0,
                                gz0 - 0.01, gz0 + DETENT_H))
    # far post opened above the groove floor where the panel board's SH-2 (J5) plug exits (+x)
    plate = plate.cut(box(x1 - POST_LIP - 0.01, x1 + POST_T, y0 + 13.0, y1 + 1.1, gz0 + 0.01, UNDER - 0.01))

    # 7. magnet tabs: far long wall (+x) and both end walls
    for ty in (90.0, 135.0):
        tab = box(px1 - TAB_T, px1, ty - TAB_W / 2, ty + TAB_W / 2, TAB_BOTTOM, UNDER + 0.01)
        plate = plate.union(tab)
        for ry in (ty - 2.5, ty + 2.5):
            plate = plate.union(box(px1 - 0.01, px1 + CRUSH_H, ry - CRUSH_W / 2, ry + CRUSH_W / 2,
                                    TAB_BOTTOM, UNDER + 0.01))
        pocket = (cq.Workplane("YZ").center(ty, MAG_Z).circle(MAG_D / 2).extrude(MAG_T + 0.5)
                  .translate((px1 - MAG_T, 0, 0)))
        plate = plate.cut(pocket)
    for (ty0, ty1, dirn) in ((py0, py0 + TAB_T, -1), (py1 - TAB_T, py1, +1)):
        tx = -17.5
        plate = plate.union(box(tx - TAB_W / 2, tx + TAB_W / 2, ty0, ty1, TAB_BOTTOM, UNDER + 0.01))
        for rx in (tx - 2.5, tx + 2.5):
            ry0, ry1 = (ty0 - CRUSH_H, ty0 + 0.01) if dirn < 0 else (ty1 - 0.01, ty1 + CRUSH_H)
            plate = plate.union(box(rx - CRUSH_W / 2, rx + CRUSH_W / 2, ry0, ry1, TAB_BOTTOM, UNDER + 0.01))
        pocket = cq.Workplane("XZ").center(tx, MAG_Z).circle(MAG_D / 2).extrude(MAG_T + 0.5)
        # XZ workplane extrudes toward -y; place so the pocket opens on the outer face
        if dirn < 0:
            pocket = pocket.translate((0, ty0 + MAG_T, 0))
        else:
            pocket = pocket.translate((0, ty1 + 0.5, 0))
        plate = plate.cut(pocket)

    # 8. hinge-side notches for the lid's hinge tabs
    for (ny0, ny1) in HINGE_NOTCH:
        plate = plate.cut(box(px0 - 0.5, px0 + HINGE_NOTCH_DEPTH, ny0, ny1, UNDER - 1, TOP + 1))

    # 9. keyboard wire channel (rounded inner corners)
    wy0, wy1, wd = WIRE_CH
    ch = (cq.Workplane("XY").center(px0 - 2 + (wd + 2) / 2, (wy0 + wy1) / 2)
          .rect(wd + 2, wy1 - wy0).extrude(PLATE_T + 2).edges("|Z").fillet(1.5)
          .translate((0, 0, UNDER - 1)))
    plate = plate.cut(ch)
    return plate


def build_cap():
    # Slider cap for SW2 (MSS12C02LS). Head 7 (y) x 4 (x) x 1.2 rides on the plate top and covers the slot at
    # both ends of the +-0.75 travel; the stem slides in the slot; the pocket takes the 0.68 x 1.63 knob with
    # 0.125 per side (PLA shrink makes it a light press fit). Stem bottom 0.3 above the switch body.
    body_top = CARRIER_TOP + SW2_BODY_H
    knob_top = body_top + SW2_KNOB_WH[2]
    stem_len = TOP - (body_top + 0.3)
    kx_, ky_, _ = SW2_KNOB_WH
    sx, sy = SW2_STEM
    head = box(-2.0, 2.0, -3.5, 3.5, 0, 1.2)
    stem = box(-sx / 2, sx / 2, -sy / 2, sy / 2, -stem_len, 0)
    cap = head.union(stem)
    cap = cap.cut(box(-kx_ / 2 - 0.125, kx_ / 2 + 0.125, -ky_ / 2 - 0.125, ky_ / 2 + 0.125,
                      -stem_len - 0.1, knob_top - TOP + 0.15))
    return cap.translate((SW2_KNOB[0], SW2_KNOB[1], TOP))   # world frame, like the other STEPs


def build_fl_cap():
    # Button cap for SW1 (TS-1187A-S16, 1.5 mm). Drop it into the plate hole from below before the plate goes
    # on; it rests on the button through a 1.8 mm nub and the flange stops it falling out of the top.
    x, y = SW1
    button_top = CARRIER_TOP + SW1_H
    fz1 = UNDER - FL_FLANGE_GAP
    cap = cyl_z(x, y, FL_NUB_D, button_top, button_top + FL_NUB_H)
    cap = cap.union(cyl_z(x, y, FL_SHAFT_D, button_top + FL_NUB_H, FL_CAP_TOP))
    cap = cap.union(cyl_z(x, y, FL_FLANGE_D, fz1 - FL_FLANGE_T, fz1))
    cap = cap.faces(">Z").edges().chamfer(0.4)
    return cap


def build_h4_spacer():
    # Disc on the battery top under H4; the pin glued in H4 drops into its blind hole.
    x, y = H4
    z1 = CARRIER_TOP - BOARD_T
    disc = cyl_z(x, y, H4_SPACER_D, BATTERY_TOP, z1)
    return disc.cut(cyl_z(x, y, PIN_HOLE_D, BATTERY_TOP + H4_FLOOR, z1 + 0.1))


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    plate = build_plate()
    cap = build_cap()
    cq.exporters.export(plate, os.path.join(OUT, "faceplate.step"))
    cq.exporters.export(cap, os.path.join(OUT, "slider_cap.step"))
    fl = build_fl_cap()
    cq.exporters.export(fl, os.path.join(OUT, "fl_cap.step"))
    bbf = fl.val().BoundingBox()
    flp = fl.rotate((0, 0, 0), (1, 0, 0), 180)   # chamfered top on the bed; the 1 mm flange ring is a short overhang
    bbp = flp.val().BoundingBox()
    flp = flp.translate((-bbp.xmin, -bbp.ymin, -bbp.zmin))
    cq.exporters.export(flp, os.path.join(OUT, "fl_cap_print.stl"), tolerance=0.02, angularTolerance=0.1)
    print("fl cap bbox z %.2f..%.2f  print %.2f x %.2f x %.2f" % (bbf.zmin, bbf.zmax, bbf.xlen, bbf.ylen, bbf.zlen))
    # print orientation: top face on the bed
    bb = plate.val().BoundingBox()
    pp = plate.rotate((0, 0, 0), (1, 0, 0), 180)
    bb2 = pp.val().BoundingBox()
    pp = pp.translate((-bb2.xmin, -bb2.ymin, -bb2.zmin))
    cq.exporters.export(pp, os.path.join(OUT, "faceplate_print.stl"), tolerance=0.02, angularTolerance=0.1)
    cp = cap.rotate((0, 0, 0), (1, 0, 0), 180)   # head down on the bed
    bbc = cp.val().BoundingBox()
    cp = cp.translate((-bbc.xmin, -bbc.ymin, -bbc.zmin))
    cq.exporters.export(cp, os.path.join(OUT, "slider_cap_print.stl"), tolerance=0.02, angularTolerance=0.1)
    sp = build_h4_spacer()
    cq.exporters.export(sp, os.path.join(OUT, "h4_spacer.step"))
    bbs = sp.val().BoundingBox()
    cq.exporters.export(sp.translate((-bbs.xmin, -bbs.ymin, -bbs.zmin)), os.path.join(OUT, "h4_spacer_print.stl"),
                        tolerance=0.01, angularTolerance=0.1)
    print("h4 spacer: gap %.2f, disc %.1f x %.2f, pin length %.2f" % (H4_GAP, H4_SPACER_D, bbs.zlen, PIN_LEN))
    v = plate.val()
    print("plate bbox x %.2f..%.2f y %.2f..%.2f z %.2f..%.2f  volume %.0f mm3  solids %d" %
          (bb.xmin, bb.xmax, bb.ymin, bb.ymax, bb.zmin, bb.zmax, v.Volume(), len(plate.solids().vals())))
    print("print bbox", pp.val().BoundingBox().xlen, pp.val().BoundingBox().ylen, pp.val().BoundingBox().zlen)
