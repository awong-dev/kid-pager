"""Kid-pager faceplate (display cassette top plate) for the Altoids tin build, v1.

Parametric CadQuery model.
Frame: the Fusion assembly frame used by hardware/carrier/Assembled*.step
(tin long axis = y, hinge wall = -x, z up from the tin floor). KiCad board frame
maps as kx = world_y + 2.03, ky = world_x + 101.27.

Assumptions that must be checked against hardware:
  * display = bare GDEY029T94(-FL03) glass 79.0 x 36.7, glass top at GLASS_TOP,
    FPC leaving the +y (battery-end) short edge, centred;
  * carrier top at CARRIER_TOP, part positions from pager-carrier.kicad_pcb
  * tin interior from "Assembled V0.2.step" (Altoids bottom lid v4).
Run:  <cadquery python> faceplate.py
"""
import cadquery as cq
import os

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")

TIN_X0, TIN_X1 = -45.37, 12.28
TIN_Y0, TIN_Y1 = 66.21, 159.01
TIN_R = 13.45
CLEAR = 0.30                     # plate-to-wall clearance per side (magnets hold)

CARRIER_TOP = 10.91              # carrier PCB top, from the assembly STEP (socketed headers)
BATTERY_TOP = 8.75
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

J6_OPEN = (3.9, 10.9)            # (x, y) opening for the 3.2 x 10.3 receptacle

LEG_D = 4.4                      # standoff on the 4.5 mm copper-free annulus
SCREW_HOLE = 2.4                 # M2 clearance
CBORE_D, CBORE_DEPTH = 4.0, 1.0  # M2 pan head

MAG_D, MAG_T = 6.3, 1.2          # pocket for a 6 x 1 mm disc magnet
TAB_W, TAB_T, TAB_BOTTOM = 9.0, 2.0, 8.0
MAG_Z = 11.5
WIRE_CH = (116.0, 124.0, 4.0)    # keyboard wire channel: y0, y1, depth into x
HINGE_NOTCH = [(88.0, 97.2), (128.0, 137.2)]   # lid hinge tabs, z 11.1..15.8
HINGE_NOTCH_DEPTH = 1.5


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

    # 7. magnet tabs: far long wall (+x) and both end walls
    for ty in (90.0, 135.0):
        tab = box(px1 - TAB_T, px1, ty - TAB_W / 2, ty + TAB_W / 2, TAB_BOTTOM, UNDER + 0.01)
        plate = plate.union(tab)
        pocket = (cq.Workplane("YZ").center(ty, MAG_Z).circle(MAG_D / 2).extrude(MAG_T + 0.5)
                  .translate((px1 - MAG_T, 0, 0)))
        plate = plate.cut(pocket)
    for (ty0, ty1, dirn) in ((py0, py0 + TAB_T, -1), (py1 - TAB_T, py1, +1)):
        tx = -17.5
        plate = plate.union(box(tx - TAB_W / 2, tx + TAB_W / 2, ty0, ty1, TAB_BOTTOM, UNDER + 0.01))
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
    cap = head.union(stem)


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    plate = build_plate()
    cap = build_cap()
    cq.exporters.export(plate, os.path.join(OUT, "faceplate.step"))
    cq.exporters.export(cap, os.path.join(OUT, "slider_cap.step"))
    # print orientation: top face on the bed
    bb = plate.val().BoundingBox()
    pp = plate.rotate((0, 0, 0), (1, 0, 0), 180)
    bb2 = pp.val().BoundingBox()
    pp = pp.translate((-bb2.xmin, -bb2.ymin, -bb2.zmin))
    cq.exporters.export(pp, os.path.join(OUT, "faceplate_print.stl"), tolerance=0.02, angularTolerance=0.1)
    bbc = cp.val().BoundingBox()
    cp = cp.translate((-bbc.xmin, -bbc.ymin, -bbc.zmin))
    cq.exporters.export(cp, os.path.join(OUT, "slider_cap_print.stl"), tolerance=0.02, angularTolerance=0.1)
    v = plate.val()
    print("plate bbox x %.2f..%.2f y %.2f..%.2f z %.2f..%.2f  volume %.0f mm3  solids %d" %
          (bb.xmin, bb.xmax, bb.ymin, bb.ymax, bb.zmin, bb.zmax, v.Volume(), len(plate.solids().vals())))
    print("print bbox", pp.val().BoundingBox().xlen, pp.val().BoundingBox().ylen, pp.val().BoundingBox().zlen)
