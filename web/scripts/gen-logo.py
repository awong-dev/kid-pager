"""Draws the pager mark in three-quarter view and writes public/logo.svg, app/icon.svg and
components/Logo.tsx. Run from web/: python3 scripts/gen-logo.py . && node scripts/gen-icons.mjs"""
import math, sys
from pathlib import Path
WEB = Path(sys.argv[1])
NAVY, RIM, BASE, CREAM, INK = "#0b3c6f", "#062443", "#93a1b1", "#fff4d6", "#14181f"

PHI, TILT = math.radians(float(sys.argv[2]) if len(sys.argv) > 2 else 28), 0.60
W, D, R = 84.0, 56.0, 13.0          # lid width, depth, corner radius (in the lid's own plane)
LID_H, BASE_H = 7.0, 13.0           # visible lid rim and silver base heights on screen
A, B, C, Dm = math.cos(PHI), math.sin(PHI) * TILT, -math.sin(PHI), math.cos(PHI) * TILT

def proj(u, v, drop=0.0):
    return (A * u + C * v, B * u + Dm * v + drop)

def rrect(w, d, r, ox=0.0, oy=0.0, notch=False, n=7):
    """Outline points of a rounded (or notched-corner) rectangle centred on the lid."""
    pts = []
    corners = [(w / 2 - r, -d / 2 + r, -90), (w / 2 - r, d / 2 - r, 0), (-w / 2 + r, d / 2 - r, 90), (-w / 2 + r, -d / 2 + r, 180)]
    for cx, cy, a0 in corners:
        for i in range(n + 1):
            a = math.radians(a0 + 90 * i / n)
            if notch:  # concave quarter circle centred on the true corner
                kx = cx + (r if cx > 0 else -r); ky = cy + (r if cy > 0 else -r)
                b = math.radians(a0 + 180 + 90 - 90 * i / n)
                pts.append((kx + r * math.cos(b) + ox, ky + r * math.sin(b) + oy))
            else:
                pts.append((cx + r * math.cos(a) + ox, cy + r * math.sin(a) + oy))
    return pts

def hull(points):
    pts = sorted(set(points))
    def half(seq):
        out = []
        for p in seq:
            while len(out) >= 2 and (out[-1][0]-out[-2][0])*(p[1]-out[-2][1]) - (out[-1][1]-out[-2][1])*(p[0]-out[-2][0]) <= 0:
                out.pop()
            out.append(p)
        return out
    lo, hi = half(pts), half(reversed(pts))
    return lo[:-1] + hi[:-1]

top = [proj(u, v) for u, v in rrect(W, D, R)]
lid_side = hull(top + [(x, y + LID_H) for x, y in top])
base_side = hull([(x, y + LID_H) for x, y in top] + [(x, y + LID_H + BASE_H) for x, y in top])
label = [proj(u, v) for u, v in rrect(W - 18, D - 16, 7, notch=True, n=5)]
# chat bubble lying on the label, with a tail towards the front-left
bub = rrect(34, 20, 7, oy=-2, n=5)
tail = [(-4, 8), (-13, 16), (-11, 8)]
i = max(range(len(bub)), key=lambda k: (bub[k][1], -abs(bub[k][0] + 7)))
bubble = [proj(u, v) for u, v in bub]
tailp = [proj(u, v) for u, v in tail]
dots = [proj(u, -2) for u in (-9, 0, 9)]

# antenna: a short thin rod standing behind the tin's back-left corner, with a cream tip so it
# shows on the navy app bar; drawn first so the tin overlaps its foot
ax, ay = proj(-W / 2 + 9, -D / 2 + 5)
ROD_W, ROD_H = 5.5, 27.0
shapes = [
    ("rect", INK, (ax - ROD_W / 2, ay - ROD_H, ROD_W, ROD_H + 6, ROD_W / 2)),
    ("circle", CREAM, (ax, ay - ROD_H + ROD_W / 2, 1.3)),
    ("poly", BASE, base_side), ("poly", RIM, lid_side), ("poly", NAVY, top), ("poly", CREAM, label),
    ("poly", NAVY, bubble), ("poly", NAVY, tailp),
] + [("circle", CREAM, (x, y, 2.1)) for x, y in dots]
xs, ys = [], []
for kind, _, g in shapes:
    if kind == "poly": xs += [p[0] for p in g]; ys += [p[1] for p in g]
    elif kind == "circle": xs += [g[0]-g[2], g[0]+g[2]]; ys += [g[1]-g[2], g[1]+g[2]]
    else: xs += [g[0], g[0]+g[2]]; ys += [g[1], g[1]+g[3]]
MARGIN = 7.0
s = (128 - 2 * MARGIN) / max(max(xs) - min(xs), max(ys) - min(ys))
tinx = [p[0] for p in base_side + top]
tx = 64 - s * (min(tinx) + max(tinx)) / 2
ty = 64 - s * (min(ys) + max(ys)) / 2
f = lambda v: f"{v:.1f}".rstrip("0").rstrip(".")
els = []
for kind, fill, g in shapes:
    if kind == "poly":
        d = "M" + "L".join(f"{f(s*x+tx)} {f(s*y+ty)}" for x, y in g) + "Z"
        els.append(("path", {"d": d, "fill": fill}))
    elif kind == "circle":
        els.append(("circle", {"cx": f(s*g[0]+tx), "cy": f(s*g[1]+ty), "r": f(s*g[2]), "fill": fill}))
    else:
        els.append(("rect", {"x": f(s*g[0]+tx), "y": f(s*g[1]+ty), "width": f(s*g[2]), "height": f(s*g[3]), "rx": f(s*g[4]), "fill": fill}))
def tag(name, attrs, close): return f"<{name} " + " ".join(f'{k}="{v}"' for k, v in attrs.items()) + close
svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128" width="128" height="128" role="img" aria-label="Pager">\n'
       "  <title>Pager</title>\n"
       "  <!-- A mint-tin pager in three-quarter view: silver base, navy lid, cream label with a chat bubble, short antenna. -->\n"
       + "".join("  " + tag(n, a, "/>") + "\n" for n, a in els) + "</svg>\n")
(WEB / "public/logo.svg").write_text(svg)
(WEB / "app/icon.svg").write_text(svg)
tsx = '''/** The pager mark (a mint-tin pager in three-quarter view with a short antenna), inline. Same
 * drawing as `public/logo.svg`. The cream label and grey base keep it legible on the navy app bar
 * and on a white page. */

interface LogoProps {
  size?: number;
  /** Accessible name; omit for a decorative mark (hidden from assistive tech). */
  title?: string;
}

export default function Logo({ size = 32, title }: LogoProps) {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 128 128"
      width={size}
      height={size}
      role={title ? "img" : undefined}
      aria-label={title}
      aria-hidden={title ? undefined : true}
      focusable="false"
      style={{ display: "block", flexShrink: 0 }}
    >
      {title && <title>{title}</title>}
''' + "".join("      " + tag(n, a, " />") + "\n" for n, a in els) + '''    </svg>
  );
}
'''
(WEB / "components/Logo.tsx").write_text(tsx)
print("bbox", f(min(xs)), f(max(xs)), f(min(ys)), f(max(ys)), "scale", f(s), "bytes", len(svg))
