#!/usr/bin/env python3
"""mkassets.py -- rasterises a Latin face, Noto Sans CJK and optional emoji into the `assets`
image gfx.c mmaps at boot. The byte layout is documented in firmware/main/gfx.h.

Requires FreeType (`pip install freetype-py`). Rendering is monochrome, hinted
(FT_LOAD_TARGET_MONO). Font files are inputs (--sans-font / --cjk-font / --emoji-font); none is
fetched or embedded. The Noto Sans CJK file is not in the repo; pass a local copy.

Default image: DejaVu Sans for Latin/Greek/Cyrillic plus Noto Sans CJK, both pinned to the same
per-size baseline (Noto's ascender values):
  python3 tools/mkassets.py --lang sc \
      --sans-font tools/fonts/dejavu/DejaVuSans.ttf \
      --cjk-font <a local Noto Sans CJK .ttc> \
      --baseline "12=13,16=18" \
      -o build/assets.bin

Emoji coverage is enabled by --emoji-font; without it the output is identical to the default:
  python3 tools/mkassets.py --lang sc \
      --sans-font tools/fonts/dejavu/DejaVuSans.ttf \
      --cjk-font <a local Noto Sans CJK .ttc> \
      --emoji-font tools/fonts/notoemoji/NotoEmoji-wght.ttf \
      --emoji-axes "wght=600" --emoji-px "12=10,16=13" \
      --baseline "12=13,16=18" \
      -o build/images/assets-dejavu-emoji.bin
Sources, in priority order (a codepoint is taken from the first that has it):
  (a) DejaVu Sans's own symbol/emoji glyphs (_DEJAVU_EXTRA_RANGES);
  (b) _EMOJI_ALIASES: common emoji DejaVu lacks, drawn with the bitmap of a DejaVu symbol;
  (c) Noto Emoji (monochrome outline font, OFL), every single-codepoint emoji not covered above;
  (d) zero-width blanks (w=0, adv=0, rows=0) for joiner/selector/skin-tone components.
--emoji-px maps text size to the pixel size Noto Emoji is rasterised at (it is ~1 em tall). The
glyph keeps its bearing_y relative to the text baseline and its advance is tightened to ink
width plus side bearings (Noto's own advance is 1.3 em).

--sans-axes "opsz=7,wght=500" sets variable-font design coordinates on the text face; it has no
effect on a static font. --baseline "12=13,16=18" pins the per-size baseline, which by default is
the text face's own ascender and positions every glyph, CJK included.

CJK repertoire: `sc` is all 6763 GB2312 hanzi plus Big5 level 1's 5401 (0xA440-0xC67E), merged by
codepoint into 8836, plus 365 Cantonese characters from tools/fonts/cantonese_extra.txt (some
outside the BMP) = 9201; `tc` is Big5 level 1 alone. Both are decoded from Python's gb2312/big5
codecs. `jp`/`kr` take the first N codepoints of their Unicode blocks that the CJK font has a
glyph for (an approximation, not JIS/KS).
"""

import argparse
import struct
import sys
from pathlib import Path

import freetype

MAGIC = b"PGFA"
VERSION = 1
SIZES = (12, 16)

# (start, end, font) inclusive codepoint ranges shared by every language.
# 'sans' = the Latin face, 'cjk' = Noto Sans CJK's regional face (CJK punctuation, fullwidth).
_COMMON_RANGES = (
    (0x0020, 0x007E, "sans"),  # Basic Latin (printable)
    (0x00A0, 0x00FF, "sans"),  # Latin-1 Supplement
    (0x0100, 0x017F, "sans"),  # Latin Extended-A
    (0x0370, 0x03FF, "sans"),  # Greek and Coptic
    (0x0400, 0x04FF, "sans"),  # Cyrillic
    (0x2000, 0x206F, "sans"),  # General Punctuation
    (0x3000, 0x303F, "cjk"),  # CJK Symbols and Punctuation
    (0xFF00, 0xFFEF, "cjk"),  # Halfwidth and Fullwidth Forms
)

# jp/kr: Unicode ranges + target count; sc/tc use _legacy_hanzi() instead.
_CJK_RANGES = {
    "jp": ((0x3040, 0x309F), (0x30A0, 0x30FF), (0x4E00, 0x9FFF)),  # kana + stand-in for JIS X 0208
    "kr": ((0xAC00, 0xD7A3), (0x4E00, 0x9FFF)),  # Hangul syllables + stand-in for KS X 1001 hanja
}
_CJK_TARGET_COUNT = {"jp": 6879, "kr": 7238}


def _gb2312_hanzi():
    """All 6763 GB2312 hanzi (rows 16-87) as Unicode codepoints."""
    out = set()
    for row in range(16, 88):
        for cell in range(0xA1, 0xFF):
            try:
                out.add(ord(bytes([row + 0xA0, cell]).decode("gb2312")))
            except UnicodeDecodeError:
                pass
    return out


def _big5_level1_hanzi():
    """Big5 level 1, the 5401 common hanzi at 0xA440-0xC67E."""
    out = set()
    for lead in range(0xA4, 0xC7):
        for trail in list(range(0x40, 0x7F)) + list(range(0xA1, 0xFF)):
            if (lead << 8 | trail) > 0xC67E:
                break
            try:
                out.add(ord(bytes([lead, trail]).decode("big5")))
            except UnicodeDecodeError:
                pass
    return out


_CANTONESE_EXTRA = Path(__file__).resolve().parent / "fonts" / "cantonese_extra.txt"


def _cantonese_extra():
    """The frequent Cantonese characters outside GB2312 + Big5 level 1."""
    out = set()
    for line in _CANTONESE_EXTRA.read_text(encoding="utf-8").splitlines():
        if not line.startswith("#"):
            out.update(ord(ch) for ch in line if not ch.isspace())
    return out


def _legacy_hanzi(lang):
    """The real legacy repertoire for sc/tc, else None (jp/kr use ranges)."""
    if lang == "sc":
        return _gb2312_hanzi() | _big5_level1_hanzi() | _cantonese_extra()
    if lang == "tc":
        return _big5_level1_hanzi()
    return None

_CJK_FACE_NAME_HINT = {"sc": "SC", "tc": "TC", "jp": "JP", "kr": "KR"}


def _open_cjk_face(path, lang, face_index):
    """Opens `path` as a FreeType face; for a .ttc/.otc with no `face_index`, picks the Regular
    face whose family name matches the language's region tag."""
    if face_index is not None:
        return freetype.Face(str(path), face_index)

    probe = freetype.Face(str(path), 0)
    num_faces = probe.num_faces
    if num_faces <= 1:
        return probe

    hint = _CJK_FACE_NAME_HINT[lang]
    best = None
    for i in range(num_faces):
        f = freetype.Face(str(path), i)
        family = f.family_name.decode("utf-8", "replace") if f.family_name else ""
        style = f.style_name.decode("utf-8", "replace") if f.style_name else ""
        if hint in family and style == "Regular" and "Mono" not in family:
            best = f
            break
    if best is None:
        raise SystemExit(
            f"mkassets: could not find a '{hint}' Regular face in font collection "
            f"{path} ({num_faces} faces) — pass --cjk-face-index explicitly"
        )
    return best


# DejaVu Sans's own glyphs, taken before anything else (inclusive ranges).
_DEJAVU_EXTRA_RANGES = (
    (0x2100, 0x214F),  # Letterlike (TM, info)
    (0x2190, 0x21FF),  # Arrows
    (0x2300, 0x23FF),  # Misc Technical
    (0x25A0, 0x25FF),  # Geometric Shapes
    (0x2600, 0x27BF),  # Misc Symbols + Dingbats
    (0x2B00, 0x2BFF),  # Misc Symbols and Arrows (stars, arrows)
    (0x1F300, 0x1F64F),  # Misc Symbols and Pictographs + Emoticons
)
# emoji codepoint -> DejaVu symbol whose bitmap it reuses (only where DejaVu
# lacks the emoji codepoint itself).
_EMOJI_ALIASES = {
    0x2705: 0x2714,  # white heavy check mark -> heavy check mark
    0x274C: 0x2718,  # cross mark -> heavy ballot x
    0x2B50: 0x2605,  # white medium star -> black star
}
# Noto Emoji candidates (inclusive ranges / singles), minus _EMOJI_COMPONENTS.
_NOTO_EMOJI_RANGES = (
    (0x00A9, 0x00A9), (0x00AE, 0x00AE), (0x203C, 0x203C), (0x2049, 0x2049),
    (0x2122, 0x2122), (0x2139, 0x2139), (0x2190, 0x2BFF),
    (0x3030, 0x3030), (0x303D, 0x303D), (0x3297, 0x3297), (0x3299, 0x3299),
    (0x1F000, 0x1FAFF),
)
# Not drawn: skin tones, regional indicators, ZWJ, variation selectors,
# keycap, tags. Of these, _EMOJI_BLANKS become zero-width records.
_EMOJI_BLANKS = (0x200D, 0xFE0E, 0xFE0F, 0x20E3) + tuple(range(0x1F3FB, 0x1F400))
_EMOJI_COMPONENTS = (set(_EMOJI_BLANKS) | set(range(0x1F1E6, 0x1F200))
                     | set(range(0xE0020, 0xE0080)))


def _collect_emoji(chosen, sans_face, emoji_face):
    """Adds emoji entries to `chosen` ({cp: kind}); kind is 'dejavu' (DejaVu extras),
    ('alias', target_cp), 'emoji' or 'blank'. Returns nothing."""
    for start, end in _DEJAVU_EXTRA_RANGES:
        for cp in range(start, end + 1):
            if cp not in chosen and _glyph_coverage(sans_face, cp):
                chosen[cp] = "dejavu"
    for cp, target in sorted(_EMOJI_ALIASES.items()):
        if cp not in chosen and chosen.get(target) in ("sans", "dejavu"):
            chosen[cp] = ("alias", target)
    for start, end in _NOTO_EMOJI_RANGES:
        for cp in range(start, end + 1):
            if (cp not in chosen and cp not in _EMOJI_COMPONENTS
                    and _glyph_coverage(emoji_face, cp)):
                chosen[cp] = "emoji"
    for cp in _EMOJI_BLANKS:
        chosen[cp] = "blank"  # overrides: components must never draw ink


def _glyph_coverage(face, cp):
    return face.get_char_index(cp) != 0


def _collect_codepoints(lang, sans_face, cjk_face, emoji_face=None):
    """Returns an ordered list of (codepoint, font) pairs, font in
    {'sans','cjk'}, deduplicated and finally sorted by codepoint (gfx.c's
    binary search requires strictly ascending, unique codepoints — if a
    codepoint were reachable from both fonts we keep the first one seen,
    'sans' before 'cjk' per _COMMON_RANGES' order)."""
    chosen = {}  # codepoint -> font
    for start, end, font in _COMMON_RANGES:
        face = sans_face if font == "sans" else cjk_face
        for cp in range(start, end + 1):
            if cp in chosen:
                continue
            if _glyph_coverage(face, cp):
                chosen[cp] = font

    legacy = _legacy_hanzi(lang)
    if legacy is not None:
        missing = []
        for cp in sorted(legacy):
            if cp in chosen:
                continue
            if _glyph_coverage(cjk_face, cp):
                chosen[cp] = "cjk"
            else:
                missing.append(cp)
        if missing:
            print(f"mkassets: {len(missing)} {lang} hanzi have no glyph in the CJK face, "
                  f"e.g. {''.join(chr(c) for c in missing[:20])}", file=sys.stderr)
    else:
        target = _CJK_TARGET_COUNT[lang]
        cjk_count = sum(1 for f in chosen.values() if f == "cjk")
        for start, end in _CJK_RANGES[lang]:
            if cjk_count >= target:
                break
            for cp in range(start, end + 1):
                if cjk_count >= target:
                    break
                if cp in chosen:
                    continue
                if _glyph_coverage(cjk_face, cp):
                    chosen[cp] = "cjk"
                    cjk_count += 1

    if emoji_face is not None:
        _collect_emoji(chosen, sans_face, emoji_face)
    return sorted(chosen.items())


def _blank_glyph():
    return {"w": 0, "adv": 0, "bearing_x": 0, "bearing_y": 0, "rows": 0, "bitmap": b""}


def _render_glyph(face, size_px, cp):
    """Renders codepoint `cp` at `size_px` with FreeType, monochrome,
    hinted (FT_LOAD_TARGET_MONO). Returns a dict with the fields gfx.c's
    glyph record needs, plus 'bitmap' (tightly packed rows, ceil(w/8)
    bytes/row, MSB-first, 1=ink) already re-packed from FreeType's own
    (possibly differently padded/oriented) buffer."""
    face.set_pixel_sizes(0, size_px)
    flags = freetype.FT_LOAD_RENDER | freetype.FT_LOAD_TARGET_MONO
    face.load_char(cp, flags)
    g = face.glyph
    bmp = g.bitmap
    w = bmp.width
    rows = bmp.rows
    adv = round(g.advance.x / 64)
    bearing_x = g.bitmap_left
    bearing_y = g.bitmap_top

    row_bytes = (w + 7) // 8
    packed = bytearray(row_bytes * rows)
    pitch = bmp.pitch
    src_row_bytes = abs(pitch)
    buf = bytes(bmp.buffer)
    for r in range(rows):
        src_row_index = r if pitch >= 0 else (rows - 1 - r)
        src_off = src_row_index * src_row_bytes
        src_row = buf[src_off:src_off + src_row_bytes]
        packed[r * row_bytes:(r + 1) * row_bytes] = src_row[:row_bytes].ljust(row_bytes, b"\x00")

    for v, name in ((w, "w"), (adv, "adv"), (rows, "rows")):
        if not (0 <= v <= 255):
            raise SystemExit(f"mkassets: U+{cp:04X} {name}={v} does not fit in a byte")
    for v, name in ((bearing_x, "bearing_x"), (bearing_y, "bearing_y")):
        if not (-128 <= v <= 127):
            raise SystemExit(f"mkassets: U+{cp:04X} {name}={v} does not fit in a signed byte")

    return {
        "w": w,
        "adv": adv,
        "bearing_x": bearing_x,
        "bearing_y": bearing_y,
        "rows": rows,
        "bitmap": bytes(packed),
    }


def _apply_axes(face, axes):
    """Set variable-font design coordinates from {"opsz": 7, "wght": 500}.
    Axes not named keep the font's default. No-op for a static font."""
    if not axes:
        return
    info = face.get_variation_info()
    tags = {"OpticalSize": "opsz", "Weight": "wght", "Width": "wdth", "Italic": "ital",
            "Slant": "slnt"}
    coords = []
    for axis in info.axes:
        tag = tags.get(axis.name, axis.name)
        value = axes.get(tag, axis.default)
        coords.append(max(axis.minimum, min(axis.maximum, value)))
    face.set_var_design_coords(coords)


def _build_size_block(size_px, entries, sans_face, cjk_face, baseline_override=None,
                      emoji_face=None, emoji_px=None, stats=None):
    """entries: sorted [(codepoint, font), ...]. Returns the block's bytes
    (sub-header + codepoint table + record table + bitmap blob), per
    gfx.h's format doc."""
    sans_face.set_pixel_sizes(0, size_px)
    # Defaults to the text face's ascender, which differs per font; --baseline pins it.
    baseline = baseline_override if baseline_override is not None else round(
        sans_face.size.ascender / 64)
    baseline = max(0, min(255, baseline))

    codepoints_bytes = bytearray()
    records_bytes = bytearray()
    bitmap_blob = bytearray()

    for cp, font in entries:
        rec_start = len(bitmap_blob)
        if font == "blank":
            g = _blank_glyph()
        elif font == "emoji":
            g = _render_glyph(emoji_face, (emoji_px or {}).get(size_px, size_px), cp)
            # Noto's advance is ~1.3 em; tighten to ink + side bearings.
            g["adv"] = g["bearing_x"] + g["w"] + max(g["bearing_x"], 1)
        elif isinstance(font, tuple):  # ("alias", target_cp)
            g = _render_glyph(sans_face, size_px, font[1])
        else:
            g = _render_glyph(sans_face if font in ("sans", "dejavu") else cjk_face, size_px, cp)
        if stats is not None:
            key = font[0] if isinstance(font, tuple) else font
            stats[(size_px, key)] = stats.get((size_px, key), 0) + 14 + len(g["bitmap"])
        codepoints_bytes += struct.pack("<I", cp)
        offset = len(bitmap_blob)
        records_bytes += struct.pack(
            "<BBbbBBI",
            g["w"],
            g["adv"],
            g["bearing_x"],
            g["bearing_y"],
            g["rows"],
            0,
            offset,
        )
        bitmap_blob += g["bitmap"]

    sub_header_len = 12
    block_bytes = sub_header_len + len(codepoints_bytes) + len(records_bytes) + len(bitmap_blob)
    sub_header = struct.pack("<BBHII", size_px, baseline, 0, len(entries), block_bytes)
    return bytes(sub_header) + bytes(codepoints_bytes) + bytes(records_bytes) + bytes(bitmap_blob)


def build_assets(lang, sans_font, cjk_font, cjk_face_index, sans_axes=None, baselines=None,
                emoji_font=None, emoji_axes=None, emoji_px=None, stats=None):
    sans_face = freetype.Face(str(sans_font))
    _apply_axes(sans_face, sans_axes)
    cjk_face = _open_cjk_face(cjk_font, lang, cjk_face_index)
    emoji_face = None
    if emoji_font:
        emoji_face = freetype.Face(str(emoji_font))
        _apply_axes(emoji_face, emoji_axes)

    entries = _collect_codepoints(lang, sans_face, cjk_face, emoji_face)
    cjk_count = sum(1 for _, f in entries if f == "cjk")
    sans_count = sum(1 for _, f in entries if f in ("sans", "dejavu"))
    if emoji_face is not None:
        kinds = {}
        for _, f in entries:
            k = f[0] if isinstance(f, tuple) else f
            kinds[k] = kinds.get(k, 0) + 1
        print(f"mkassets: kinds {kinds}", file=sys.stderr)
    print(
        f"mkassets: lang={lang} codepoints={len(entries)} (sans={sans_count} cjk={cjk_count}, "
        f"target cjk={_CJK_TARGET_COUNT.get(lang, 'legacy table')})",
        file=sys.stderr,
    )

    blocks = [
        _build_size_block(size_px, entries, sans_face, cjk_face,
                          (baselines or {}).get(size_px), emoji_face, emoji_px, stats)
        for size_px in SIZES
    ]

    header_len = 16
    total = header_len + sum(len(b) for b in blocks)
    lang_bytes = lang.encode("ascii")[:3].ljust(3, b"\x00")
    header = struct.pack("<4sB3sIB3s", MAGIC, VERSION, lang_bytes, total, len(SIZES), b"\x00\x00\x00")
    return header + b"".join(blocks)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--lang", choices=("jp", "kr", "sc", "tc"), default="sc",
                     help="CJK repertoire (default: sc, docs/DEVICE_PLAN.md §9)")
    ap.add_argument("-o", "--out", default="build/assets.bin", help="output assets.bin path")
    ap.add_argument("--sans-font", required=True, help="Noto Sans (or substitute) font file")
    ap.add_argument("--cjk-font", required=True,
                     help="Noto Sans CJK font file (a .ttc collection, or a single-face font "
                          "already specific to one region)")
    ap.add_argument("--cjk-face-index", type=int, default=None,
                     help="face index within --cjk-font; default auto-selects by --lang "
                          "(see _open_cjk_face)")
    ap.add_argument("--sans-axes", default="",
                     help='variable-font axes for --sans-font, e.g. "opsz=7,wght=500"')
    ap.add_argument("--baseline", default="",
                     help='baseline in px per size, e.g. "12=13,16=18" (default: the text '
                          "face's own ascender). Pin it when changing fonts so the UI's rows "
                          "do not move.")
    ap.add_argument("--emoji-font", default="",
                     help="Noto Emoji (monochrome) font file; enables emoji coverage (see "
                          "docstring). Absent = output byte-identical to the plain build.")
    ap.add_argument("--emoji-axes", default="", help='axes for --emoji-font, e.g. "wght=500"')
    ap.add_argument("--emoji-px", default="12=10,16=13",
                     help="pixel size Noto Emoji is rasterised at per text size")
    args = ap.parse_args()

    axes = {k.strip(): float(v) for k, v in
            (kv.split("=") for kv in args.sans_axes.split(",") if kv.strip())}
    baselines = {int(k): int(v) for k, v in
                 (kv.split("=") for kv in args.baseline.split(",") if kv.strip())}
    def _kv(text, conv):
        return {k.strip(): conv(v) for k, v in
                (kv.split("=") for kv in text.split(",") if kv.strip())}
    stats = {}
    data = build_assets(args.lang, Path(args.sans_font), Path(args.cjk_font), args.cjk_face_index,
                        axes, baselines,
                        args.emoji_font or None, _kv(args.emoji_axes, float),
                        {int(k): v for k, v in _kv(args.emoji_px, int).items()}, stats)
    for (sz, kind), n in sorted(stats.items()):
        if kind not in ("cjk",):
            print(f"mkassets: bytes {sz}px {kind}: {n} (14 B/glyph table+record, plus bitmap)",
                  file=sys.stderr)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(data)

    print(f"mkassets: wrote {out_path} ({len(data)} bytes, {len(data) / 1024:.1f} KiB)")
    if len(data) >= 1024 * 1024:
        print("mkassets: WARNING — exceeds the 1 MiB `assets` partition", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
