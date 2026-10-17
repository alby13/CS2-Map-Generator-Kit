"""Top-down SVG plan rendered from the walkability analysis, so it shows what
a player can actually reach - not what the build script intended.

  dark floor   reachable ground
  teal shades  reachable raised surfaces (stairs, platforms), lighter = higher
  red          standable floor that cannot be reached from T spawn (sealed!)
  background   walls, cover, void
"""
from .analysis import STEP

BG = "#0f1115"


def _shade(dz):
    """Colour for a reachable surface dz units above ground."""
    if dz <= STEP:
        return "#222a35"
    t = min(1.0, dz / 256.0)
    r, g, b = (int(40 + 40 * t), int(90 + 110 * t), int(90 + 80 * t))
    return "#%02x%02x%02x" % (r, g, b)


def render(data, grid, reached, ground_z, out_path, meta=None):
    meta = meta or {}
    cell = grid.cell
    S = min(0.32, 1000.0 / max(1, grid.nx * cell))
    PAD = 70
    W = grid.nx * cell * S + 2 * PAD
    H = grid.ny * cell * S + 2 * PAD
    ytop = grid.Y0 + grid.ny * cell

    def px(x):
        return PAD + (x - grid.X0) * S

    def py(y):
        return PAD + (ytop - y) * S

    # colour per column: highest reachable surface; red if only unreachable
    # standable floor exists there
    colour = {}
    for (i, j), spans in grid.iv.items():
        ks = [k for k in range(len(spans)) if (i, j, k) in grid.nodes]
        if not ks:
            continue
        rk = [k for k in ks if (i, j, k) in reached]
        if rk:
            colour[(i, j)] = _shade(spans[max(rk)][1] - ground_z)
        elif any(abs(spans[k][1] - ground_z) <= STEP for k in ks):
            colour[(i, j)] = "#8a2b2b"

    svg = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" '
           'width="%d" height="%d" font-family="Segoe UI,Arial,sans-serif">'
           % (W, H, W, H),
           '<rect width="100%%" height="100%%" fill="%s"/>' % BG]
    cw = cell * S
    for j in range(grid.ny):          # run-length merge each row
        i = 0
        while i < grid.nx:
            c = colour.get((i, j))
            if c is None:
                i += 1
                continue
            k = i
            while k + 1 < grid.nx and colour.get((k + 1, j)) == c:
                k += 1
            svg.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" '
                       'fill="%s"/>' % (PAD + i * cw, PAD + (grid.ny - 1 - j) * cw,
                                        (k - i + 1) * cw + 0.3, cw + 0.3, c))
            i = k + 1

    # cover heights: solid blocks standing on a floor, below wall height
    # (ceiling slabs are thin too, but they do not start at a floor level)
    floors = {ground_z} | {r["z"] for r in meta.get("rooms", {}).values()}
    for s in data.solids:
        if not s.solid:
            continue
        h = s.maxs[2] - s.mins[2]
        wx, wy = s.maxs[0] - s.mins[0], s.maxs[1] - s.mins[1]
        on_floor = any(abs(s.mins[2] - fz) <= STEP for fz in floors)
        if 16 < h <= 200 and min(wx, wy) > 40 and on_floor:
            svg.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" '
                       'fill="none" stroke="#4dbfa0" stroke-width="1"/>'
                       % (px(s.mins[0]), py(s.maxs[1]), wx * S, wy * S))
            if wx * S > 22 and wy * S > 10:
                svg.append('<text x="%.1f" y="%.1f" fill="#8fe8d0" font-size="9" '
                           'text-anchor="middle" dominant-baseline="middle">%d</text>'
                           % (px((s.mins[0] + s.maxs[0]) / 2),
                              py((s.mins[1] + s.maxs[1]) / 2), round(h)))

    trig = {"func_bomb_target": "#ff5c5c", "func_buyzone": "#4da3ff"}
    for e in data.entities:
        if e.classname in trig and e.bounds:
            mn, mx = e.bounds
            col = trig[e.classname]
            if e.classname == "func_bomb_target":
                lab = "SITE " + "AB"[int(e.props.get("bomb_site_designation", "0") or 0) % 2]
            else:
                lab = "BUY " + {"2": "T", "3": "CT"}.get(e.props.get("TeamNum", ""), "?")
            svg.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" '
                       'fill="%s" fill-opacity="0.08" stroke="%s" stroke-width="2" '
                       'stroke-dasharray="7 5"/>'
                       % (px(mn[0]), py(mx[1]), (mx[0] - mn[0]) * S,
                          (mx[1] - mn[1]) * S, col, col))
            svg.append('<text x="%.1f" y="%.1f" fill="%s" font-size="11" '
                       'font-weight="600" text-anchor="middle">%s</text>'
                       % (px((mn[0] + mx[0]) / 2), py(mx[1]) + 13, col, lab))

    dots = {"info_player_terrorist": ("#ffb64d", 3.5),
            "info_player_counterterrorist": ("#6fd3ff", 3.5),
            "light_omni2": ("#fff2a8", 1.8),
            "point_nav_walkable": ("#9aa3b2", 1.8)}
    for e in data.entities:
        if e.classname in dots:
            col, r = dots[e.classname]
            svg.append('<circle cx="%.1f" cy="%.1f" r="%.1f" fill="%s"/>'
                       % (px(e.origin[0]), py(e.origin[1]), r, col))

    for name, r in meta.get("rooms", {}).items():
        x0, y0, x1, y1 = r["bounds"]
        svg.append('<text x="%.1f" y="%.1f" fill="#8793a5" font-size="11" '
                   'font-weight="600" text-anchor="middle">%s</text>'
                   % (px((x0 + x1) / 2), py((y0 + y1) / 2), name))

    legend = [("reachable floor", "#222a35"), ("raised, reachable", _shade(128)),
              ("UNREACHABLE floor", "#8a2b2b"), ("cover (height)", "#4dbfa0"),
              ("bombsite", "#ff5c5c"), ("buy zone", "#4da3ff"),
              ("T spawn", "#ffb64d"), ("CT spawn", "#6fd3ff"), ("light", "#fff2a8")]
    for n, (lab, col) in enumerate(legend):
        svg.append('<rect x="10" y="%d" width="10" height="10" fill="%s"/>'
                   '<text x="25" y="%d" fill="#c9d1da" font-size="10">%s</text>'
                   % (10 + n * 15, col, 19 + n * 15, lab))
    svg.append('<text x="%d" y="%d" fill="#5c6675" font-size="10" '
               'text-anchor="end">%d x %d units</text>'
               % (W - 10, H - 10, grid.nx * cell, grid.ny * cell))
    svg.append("</svg>")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(svg))
    return out_path
