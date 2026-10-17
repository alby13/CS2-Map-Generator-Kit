"""Command line: python -m mapgen <command> ...

  check   MAP.kv2.vmap        verify a generated map before compiling
  routes  MAP.kv2.vmap        walking times between spawns and sites
  plan    MAP.kv2.vmap        top-down SVG of what is actually reachable
  info    ANY.vmap            entities/materials of any map (binary or text)
  find                        locate CS2, its tools, and your addons
  install MAP.kv2.vmap --addon NAME   convert into the addon's content folder
  compile --addon NAME --map NAME     full compile, summarised
  build   MAP.kv2.vmap --addon NAME   check + install + compile in one go
"""
import argparse
import json
import math
import os
import sys

from . import cs2
from .builder import check_rooms

try:                                    # numpy is only needed for analysis
    from . import analysis
    from .analysis import MapData, NavGrid, STEP, RIFLE_SPEED, site_points
except ImportError as _exc:
    analysis, _MISSING = None, _exc
    RIFLE_SPEED = 215.0


def meta_path(vmap):
    base = vmap
    for ext in (".kv2.vmap", ".vmap"):
        if base.endswith(ext):
            base = base[: -len(ext)]
            break
    return base + ".meta.json"


def load_meta(vmap):
    p = meta_path(vmap)
    if os.path.exists(p):
        with open(p) as f:
            return json.load(f)
    return {}


def load(vmap):
    """MapData for any vmap; binary files are converted to text first."""
    if analysis is None:
        raise SystemExit("This command needs numpy: pip install -r requirements.txt "
                         "(%s)" % _MISSING)
    if cs2.is_binary_vmap(vmap):
        vmap = cs2.to_text(cs2.find_cs2(), vmap)
    return MapData(vmap)


def _start(grid, data, meta):
    pts = site_points(data, meta)
    if "t_spawn" not in pts:
        return None, None
    p = pts["t_spawn"]
    return grid.node_at(p[0], p[1], p[2] if len(p) > 2 else None), pts


# ------------------------------------------------------------------ check

class Report:
    def __init__(self):
        self.errors = self.warns = 0

    def ok(self, msg):
        print("  ok     " + msg)

    def warn(self, msg):
        self.warns += 1
        print("  WARN   " + msg)

    def err(self, msg):
        self.errors += 1
        print("  ERROR  " + msg)


def cmd_check(a):
    data = load(a.map)
    meta = load_meta(a.map)
    rep = Report()

    print("Layout")
    if meta.get("rooms"):
        probs = check_rooms(meta["rooms"])
        for p in probs:
            rep.err(p)
        if not probs:
            rep.ok("%d rooms, separation rules hold" % len(meta["rooms"]))
    else:
        print("  (no .meta.json beside the map - room checks skipped)")
    for w in data.warnings:
        rep.warn(w)

    print("Geometry")
    topo, open_meshes = analysis.validate_topology(data)
    for p in topo[:20]:
        rep.err(p)
    if len(topo) > 20:
        rep.err("... and %d more topology problems" % (len(topo) - 20))
    if open_meshes:
        rep.warn("%d open (single-sided) meshes, e.g. %s"
                 % (len(open_meshes), open_meshes[0]))
    if not topo and not open_meshes:
        rep.ok("%d meshes are closed, manifold, planar, outward-facing"
               % len(data.meshes))

    print("Entities")
    lint_entities(data, rep)

    print("Walkability")
    grid = NavGrid(data, jump=a.jump)
    start, pts = _start(grid, data, meta)
    if start is None:
        rep.err("no T spawn found to start from")
    else:
        reached = grid.flood(start)
        ground = grid.z(start)
        rep.ok("%d standable cells reachable from T spawn" % len(reached))
        walk_targets(data, meta, grid, reached, pts, rep)
        orphans(grid, reached, ground, meta, rep, a.verbose)

    print("\n%d error(s), %d warning(s)" % (rep.errors, rep.warns))
    return 1 if rep.errors else 0


def lint_entities(data, rep):
    t = data.of_class("info_player_terrorist")
    ct = data.of_class("info_player_counterterrorist")
    for name, lst in (("T", t), ("CT", ct)):
        if not lst:
            rep.err("no %s spawns" % name)
        elif len(lst) < 5:
            rep.warn("only %d %s spawns (5 needed for 5v5, 10+ is safer)"
                     % (len(lst), name))
        else:
            rep.ok("%d %s spawns" % (len(lst), name))
    sp = t + ct
    for i in range(len(sp)):
        for k in range(i + 1, len(sp)):
            if math.dist(sp[i].origin[:2], sp[k].origin[:2]) < 40:
                rep.warn("spawns at %s and %s overlap (< 40 apart)"
                         % (sp[i].origin, sp[k].origin))
    bt = data.of_class("func_bomb_target")
    if not bt:
        rep.warn("no func_bomb_target - not a defusal map")
    else:
        rep.ok("%d bombsite(s)" % len(bt))
    teams = {e.props.get("TeamNum") for e in data.of_class("func_buyzone")}
    for num, name in (("2", "T"), ("3", "CT")):
        if num not in teams:
            rep.warn("no func_buyzone with TeamNum %s (%s)" % (num, name))
    if not data.of_class("info_map_parameters"):
        rep.warn("no info_map_parameters")
    if not data.of_class("point_nav_walkable"):
        rep.err("no point_nav_walkable - navgen will produce an empty nav "
                "mesh and bots cannot move")
    lights = [e for e in data.entities if e.classname.startswith("light_")
              and e.classname != "light_environment"]
    if not lights:
        rep.warn("no light entities except light_environment - enclosed rooms "
                 "get no direct light" if data.of_class("light_environment")
                 else "no light entities - the map will be dark")
    stationary = [e for e in lights
                  if e.props.get("directlight", "3") in ("2", "3")]
    if len(stationary) > 4:
        rep.warn("%d stationary/dynamic lights - they share a small pool of "
                 "shadow slots and most will lose shadows; use directlight 1 "
                 "(static) for fixed fixtures" % len(stationary))
    if not any("toolslightmapres" in m for m in data.materials):
        rep.warn("no toolslightmapres volume - lightmaps fall back to a "
                 "coarse default resolution")


def walk_targets(data, meta, grid, reached, pts, rep):
    def at(p):
        return grid.node_at(p[0], p[1], p[2] if len(p) > 2 else None)

    before = rep.errors
    bad = 0
    for e in data.of_class("info_player_terrorist", "info_player_counterterrorist"):
        n = at(e.origin)
        if n is None or n not in reached:
            bad += 1
            rep.err("%s at %s is %s" % (e.classname, [round(c) for c in e.origin],
                                        "not on walkable ground" if n is None
                                        else "unreachable from T spawn"))
    if not bad:
        rep.ok("every spawn is on reachable ground")
    for e in data.of_class("func_bomb_target", "func_buyzone"):
        if e.bounds and not grid.reached_in_box(reached, *e.bounds):
            (rep.err if e.classname == "func_bomb_target" else rep.warn)(
                "%s around %s cannot be reached"
                % (e.classname, [round(c) for c in e.bounds[0]]))
    for e in data.of_class("point_nav_walkable"):
        n = at(e.origin)
        if n is None or n not in reached:
            rep.warn("nav seed at %s is not on reachable ground"
                     % [round(c) for c in e.origin])
    for name, r in meta.get("rooms", {}).items():
        x0, y0, x1, y1 = r["bounds"]
        if not grid.reached_in_box(reached, (x0, y0, r["z"]),
                                   (x1, y1, r["z"] + r["h"])):
            rep.err("room %s cannot be reached from T spawn" % name)
    for name, p in pts.items():
        n = at(p)
        if n is None or n not in reached:
            rep.err("point %s %s cannot be reached" % (name, p))
    if meta.get("rooms") and rep.errors == before:
        rep.ok("all rooms and named points reachable")


def orphans(grid, reached, ground, meta, rep, verbose):
    floors = [r["z"] for r in meta.get("rooms", {}).values()] or [ground]
    comps = grid.components(grid.nodes - reached)
    sealed, raised = [], []
    for c in comps:
        z = min(grid.z(n) for n in c)
        (sealed if any(abs(z - f) <= STEP for f in floors) else raised).append(c)
    for c in sealed:
        x, y = grid.xy(c[0])
        rep.err("sealed-off floor: %d cells around (%d, %d) z=%d are "
                "standable but unreachable" % (len(c), x, y, grid.z(c[0])))
    if raised:
        print("  info   %d raised surfaces are unreachable (usually tops of "
              "cover - fine unless players should get up there)" % len(raised))
        if verbose:
            for c in raised:
                x, y = grid.xy(c[0])
                print("           %d cells near (%d, %d) z=%d"
                      % (len(c), x, y, grid.z(c[0])))


# ------------------------------------------------------------------ routes

def cmd_routes(a):
    data = load(a.map)
    meta = load_meta(a.map)
    grid = NavGrid(data, jump=a.jump)
    pts = site_points(data, meta)
    nodes = {}
    for k, p in pts.items():
        n = grid.node_at(p[0], p[1], p[2] if len(p) > 2 else None)
        if n is None:
            print("point %s %s is not on walkable ground" % (k, p))
        else:
            nodes[k] = n
    sites = sorted(k for k in nodes if k.startswith("site_"))
    froms = [k for k in ("t_spawn", "ct_spawn") if k in nodes]
    if not froms:
        print("need t_spawn / ct_spawn points or spawn entities")
        return 1
    dist = {k: grid.distances(nodes[k]) for k in froms + sites}

    def sec(src, dst):
        d = dist[src].get(nodes[dst])
        return None if d is None else d / a.speed

    def f(v):
        return "  unreach" if v is None else "%7.1f s" % v

    others = [k for k in nodes if k not in froms and k not in sites]
    cols = sites + others
    print("Walking time at %.0f u/s (8-connected shortest path on a %d-unit grid)\n"
          % (a.speed, grid.cell))
    print("%-12s" % "" + "".join("%11s" % c for c in cols))
    for s in froms:
        print("%-12s" % s + "".join("%11s" % f(sec(s, c)) for c in cols))
    if "t_spawn" in froms and "ct_spawn" in froms and sites:
        print("\nCT arrives ahead of T:")
        for s in sites:
            t, c = sec("t_spawn", s), sec("ct_spawn", s)
            if t is not None and c is not None:
                print("  %-10s %+.1f s" % (s, t - c))
    if len(sites) >= 2:
        print("\nSite asymmetry (first two sites):")
        for s in froms:
            x, y = sec(s, sites[0]), sec(s, sites[1])
            if x is not None and y is not None:
                print("  %-10s %+.1f s" % (s, x - y))
        r = sec(sites[0], sites[1])
        print("Rotation %s -> %s: %s" % (sites[0], sites[1], f(r).strip()))
    print("\nRough reference from popular maps: T to a site ~12-15 s, "
          "CT to a site ~6-9 s.")
    return 0


# ------------------------------------------------------------------ plan

def cmd_plan(a):
    from .plan import render
    data = load(a.map)
    meta = load_meta(a.map)
    grid = NavGrid(data, jump=a.jump)
    start, _ = _start(grid, data, meta)
    if start is None:
        reached, ground = set(), 0.0
    else:
        reached, ground = grid.flood(start), grid.z(start)
    out = a.output or (meta_path(a.map)[: -len(".meta.json")] + ".plan.svg")
    render(data, grid, reached, ground, out, meta)
    print("wrote", out)
    return 0


# ------------------------------------------------------------------ info

def cmd_info(a):
    data = load(a.map)
    print("meshes: %d   entities: %d" % (len(data.meshes), len(data.entities)))
    print("\nmaterials:")
    for m in data.materials:
        print("  " + m)
    by = {}
    for e in data.entities:
        by.setdefault(e.classname, []).append(e)
    print("\nentities:")
    for cls in sorted(by):
        lst = by[cls]
        print("\n  %s  x%d%s" % (cls, len(lst), "  (brush)" if lst[0].bounds else ""))
        for e in (lst if a.all else lst[:1]):
            print("    origin %s  angles %s" % (e.origin, e.angles))
            for k, v in e.props.items():
                if k != "classname" and v != "":
                    print("      %-32s %s" % (k, v))
    for w in data.warnings:
        print("\nwarning:", w)
    return 0


# ------------------------------------------------------------------ cs2

def cmd_find(a):
    c = cs2.find_cs2()
    print("CS2:", c)
    for t in ("dmxconvert.exe", "resourcecompiler.exe"):
        p = os.path.join(c, cs2.BIN, t)
        print("  %-22s %s" % (t, "found" if os.path.exists(p) else
                              "MISSING - install the CS2 Workshop Tools DLC"))
    addons = cs2.list_addons(c)
    print("addons:", ", ".join(addons) if addons else
          "(none - create one in the Workshop Tools launcher)")
    return 0


def cmd_install(a):
    name = a.map_name or load_meta(a.map).get("name")
    if not name:
        raise SystemExit("pass --map NAME")
    cs2.install(a.map, a.addon, name, a.force)
    return 0


def cmd_compile(a):
    s = cs2.compile_map(a.addon, a.map_name, a.log)
    print_compile(s)
    return 0 if s["ok"] and s["ok"][1] == 0 and s["vpk_exists"] else 1


def print_compile(s):
    print("\nCompile summary")
    print("  result      %s" % ("%d compiled, %d failed" % s["ok"] if s["ok"]
                                else "NO RESULT LINE - see log"))
    print("  nav areas   %s" % s["nav_areas"])
    if s["shadow"]:
        print("  shadows     %d slots assigned, %d failed%s"
              % (s["shadow"] + ("  <- too many stationary lights"
                                if s["shadow"][1] else "",)))
    print("  vpk         %s %s" % (s["vpk"], "" if s["vpk_exists"] else "(MISSING)"))
    if s["nav_areas"] == 0:
        print("  !! empty nav mesh: add point_nav_walkable seeds")
    for p in s["problems"]:
        print("  !! " + p)
    if s["benign"]:
        print("  (%d known-benign stock messages ignored)" % len(s["benign"]))


def cmd_build(a):
    print("== check ==")
    if cmd_check(a):
        print("\ncheck failed - not installing")
        return 1
    print("\n== install ==")
    cmd_install(a)
    name = a.map_name or load_meta(a.map).get("name")
    print("\n== compile ==")
    s = cs2.compile_map(a.addon, name, a.log)
    print_compile(s)
    print("\nPlay it: launch CS2 with the Workshop Tools, then in the console:"
          "  map %s" % name)
    return 0 if s["ok"] and s["ok"][1] == 0 else 1


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m mapgen", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def mapcmd(name, fn, jump=True):
        s = sub.add_parser(name)
        s.add_argument("map")
        if jump:
            s.add_argument("--jump", type=float, default=None,
                           help="also allow jumping up this many units")
        s.set_defaults(fn=fn)
        return s

    s = mapcmd("check", cmd_check)
    s.add_argument("-v", "--verbose", action="store_true")
    s = mapcmd("routes", cmd_routes)
    s.add_argument("--speed", type=float, default=RIFLE_SPEED)
    s = mapcmd("plan", cmd_plan)
    s.add_argument("-o", "--output")
    s = mapcmd("info", cmd_info, jump=False)
    s.add_argument("--all", action="store_true", help="every entity, not one per class")
    sub.add_parser("find").set_defaults(fn=cmd_find)
    for name, fn in (("install", cmd_install), ("build", cmd_build)):
        s = mapcmd(name, fn)
        s.add_argument("--addon", required=True)
        s.add_argument("--map", dest="map_name")
        s.add_argument("--force", action="store_true",
                       help="replace an existing map (a backup is kept)")
        s.add_argument("--log")
        s.add_argument("-v", "--verbose", action="store_true")
    s = sub.add_parser("compile")
    s.add_argument("--addon", required=True)
    s.add_argument("--map", dest="map_name", required=True)
    s.add_argument("--log")
    s.set_defaults(fn=cmd_compile)

    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
