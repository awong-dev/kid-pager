#!/usr/bin/env python3
"""Carrier PCB: 85 x 52 mm, Walter crosswise under the left end, battery under
the rest. Run with KiCad's python:

  /Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3 carrier_pcb.py [--route]
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import pcbnew  # noqa: E402

from build import build_carrier, build_panel  # noqa: E402
from pcb import Board  # noqa: E402

W, H = 85.0, 52.0
NOTCH = (2.5, 14.5, 6.0)  # x0, x1, depth at the top edge for the antenna pigtails
WALTER_X, WALTER_Y = 13.5, 24.73  # header centre; module spans x 1.1..25.9, y -1.5..53.5
UFL = [(WALTER_X - 3.0, WALTER_Y - 21.93), (WALTER_X - 7.3, WALTER_Y - 21.93)]  # LTE, GNSS jacks (mirrored X)
GLASS = (0.5, 8.0, 79.5, 44.7)  # 2.9" glass footprint on the carrier top (79 x 36.7)
PANEL_ZONE = (62.0, 8.0, 79.5, 44.7)  # the panel board lies here, under the glass


def groups_by_tag(prj):
    """Passives carry a .group tag set in carrier.py (grp())."""
    groups = {}
    seen = set()
    for sh in prj.sheets:
        for i in sh.instances:
            g = getattr(i, "group", None)
            if g and i.ref not in seen:
                groups.setdefault(g, []).append(i.ref)
                seen.add(i.ref)
    return groups


def main(route=False):
    prj, _ = build_carrier(cli=False)
    d = os.path.normpath(os.path.join(HERE, ".."))
    b = Board(prj, d, "pager-carrier", thickness=1.0)
    b.load_all()

    nx0, nx1, nd = NOTCH
    pts = [(0, 0), (nx0, 0), (nx0, nd), (nx1, nd), (nx1, 0), (W, 0), (W, H), (0, H)]
    b.outline_polyline(pts, radius=2.0)

    P = {
        "M1": (WALTER_X, WALTER_Y, 0),
        "U1": (13.5, 31.0, 0),
        "Q1": (9.0, 44.0, 0),
        "U3": (33.0, 16.0, 0), "L2": (37.5, 16.0, 0),
        "U4": (32.0, 26.0, 0),
        "U10": (45.0, 27.0, 0),
        "U9": (56.0, 11.5, 0),
    }
    for ref, (x, y, r) in P.items():
        b.place(ref, x, y, r)

    groups = groups_by_tag(prj)
    for a, refs in groups.items():
        refs[:] = [r for r in refs if r in b.fps and r not in P and not r.startswith("TP")]
    walter_caps = sorted(groups.pop("M1", []))
    rects = {
        "U5": (6.6, 15.3, 20.6, 20.5, 90),
        "U1": (6.6, 33.6, 20.6, 41.5, 90),
        "Q1": (11.5, 42.6, 20.6, 47.0, 90),
        "U3": (27.5, 18.5, 46.0, 22.5, 90),
        "U10": (42.0, 29.5, 48.0, 31.5, 90),
        "U9": (53.0, 14.0, 61.0, 17.5, 90),
    }
    for a, refs in groups.items():
        if not refs:
            continue
        if a not in rects:
            print("unplaced group:", a, refs)
            continue
        x0, y0, x1, y1, rot = rects[a]
        b.pack(sorted(refs), x0, y0, x1, y1, rot=rot)

    for cx, cy in UFL:
    gx0, gy0, gx1, gy1 = GLASS
    for a, c in (((gx0, gy0), (gx1, gy0)), ((gx1, gy0), (gx1, gy1)), ((gx1, gy1), (gx0, gy1)), ((gx0, gy1), (gx0, gy0))):
        b.seg(a, c, layer=pcbnew.Cmts_User, w=0.15)
    px0, py0, px1, py1 = PANEL_ZONE
    for a, c in (((px0, py0), (px1, py0)), ((px1, py0), (px1, py1)), ((px1, py1), (px0, py1)), ((px0, py1), (px0, py0))):
        b.seg(a, c, layer=pcbnew.Cmts_User, w=0.15)
    b.text("glass 79 x 36.7 (GDEY029T94)", 20, 43.5, pcbnew.Cmts_User, 1.0)
    b.text("panel board here", 70.5, 26, pcbnew.Cmts_User, 1.0)
    b.text("antenna pigtails", 8.5, 7.2, pcbnew.Cmts_User, 0.8)
    b.text("SIM loads from this edge (Walter beneath)", 13.5, 51.2, pcbnew.Cmts_User, 0.8)
    b.text("pager-carrier rev A", 44, 46.0, pcbnew.F_SilkS, 1.0)
    b.text("WALTER UNDERNEATH, header side up", 13.5, 6.9, pcbnew.B_SilkS, 0.8)

    if route:
        b.route(passes=int(os.environ.get("PASSES", "30")))
    outline = [(0, 0), (W, 0), (W, H), (0, H)]
    b.zone("GND", outline, pcbnew.F_Cu, "GND top")
    b.zone("GND", outline, pcbnew.B_Cu, "GND bottom")
    b.fill()
    path = b.save()
    print("saved", path)
    return b


if __name__ == "__main__":
    main(route="--route" in sys.argv)
