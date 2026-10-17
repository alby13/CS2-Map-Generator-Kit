"""High-level map construction: rooms with doorways, cover, stairs, gameplay
entities and lighting, on top of the low-level emitter in vmap.py.

The one layout rule that bites: rooms that line up side by side must have
their interiors exactly GAP (64) apart. Walls are T (32) thick and sit outside
the interior, and floor/ceiling slabs reach T past it, so at 64 the two rooms'
walls sit back to back and their floors meet exactly. Less than 64 and slabs
overlap (z-fighting on a walkable floor); more and there is a strip with no
floor at all, which silently seals one room off. check_rooms() enforces this.
"""
import json
import math
import os

from .vmap import VMap, Mesh

T = 32           # wall thickness
FLOOR_D = 64     # floor slab depth
CEIL_D = 32      # ceiling slab depth
GAP = 2 * T      # required separation between side-by-side room interiors

# Materials proven to exist and compile in stock CS2 (seen in Valve's sample map).
WALL = "materials/dev/dev_measuregeneric01.vmat"
FLOOR = "materials/dev/reflectivity_30.vmat"
CEIL = "materials/dev/reflectivity_30.vmat"
COVER = "materials/dev/dev_measuregeneric01.vmat"
TRIG = "materials/tools/toolstrigger.vmat"
LMRES = "materials/tools/toolslightmapres.vmat"
SKY = "materials/tools/toolsskybox.vmat"

DOOR_Z = (0, 224)    # standard doorway, relative to the room floor
TEAMS = {"T": 2, "CT": 3}

LIGHT_DEFAULTS = {
    "brightness_units": "1",        # lumens
    "brightness_lumens": "14000",
    "color": "230 236 245",
    "range": "1100",
    "skirt": "0.35",
    "shape": "3",                   # point
    # 1 = static (fully baked). The default, 3 = stationary, burns a small
    # pool of runtime shadow slots: 26 stationary lights left 21 shadowless.
    "directlight": "1",
    "bouncelight": "1",
    "bouncescale": "1.0",
    "enabled": "1",
    "colormode": "0",
}


def s16(v):
    """Snap to the 16-unit grid."""
    return int(round(v / 16.0)) * 16


def _room_tuple(r):
    x0, y0, x1, y1 = r["bounds"]
    return x0, y0, x1, y1, r["z"], r["h"]


def check_rooms(rooms):
    """Problems with room placement, as human-readable strings.

    `rooms` maps name -> {"bounds": [x0, y0, x1, y1], "z": floor, "h": height}.
    """
    bad = []
    names = list(rooms)
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            na, nb = names[i], names[j]
            ax0, ay0, ax1, ay1, az, ah = _room_tuple(rooms[na])
            bx0, by0, bx1, by1, bz, bh = _room_tuple(rooms[nb])
            ox = min(ax1, bx1) - max(ax0, bx0)
            oy = min(ay1, by1) - max(ay0, by0)
            oz = min(az + ah, bz + bh) - max(az, bz)
            if ox > 0 and oy > 0:
                if oz > 0:
                    bad.append("%s and %s interiors overlap" % (na, nb))
                elif -oz < FLOOR_D + CEIL_D:
                    bad.append("%s and %s are stacked only %d apart vertically;"
                               " need >= %d so the ceiling and floor slabs do "
                               "not collide" % (na, nb, -oz, FLOOR_D + CEIL_D))
                continue
            if oz <= 0:
                continue
            for gap, other, ax in ((-ox, oy, "X"), (-oy, ox, "Y")):
                if other <= 0 or gap < 0 or gap == GAP:
                    continue
                if gap < GAP:
                    bad.append("%s/%s are %d apart in %s; side-by-side rooms "
                               "must be exactly %d apart (walls/floors overlap)"
                               % (na, nb, gap, ax, GAP))
                elif gap <= 4 * GAP:
                    bad.append("%s/%s are %d apart in %s; side-by-side rooms "
                               "must be exactly %d apart (this leaves a strip "
                               "with no floor)" % (na, nb, gap, ax, GAP))
    return bad


class Builder:
    """Collects geometry and entities for one map, then saves the KV2 text
    .vmap plus a .meta.json sidecar (rooms and named points) that the
    analysis tools read."""

    def __init__(self, name):
        self.name = name
        self.vm = VMap()
        self.solids = 0
        self.rooms = {}
        self.points = {}

    # ---- raw solids ------------------------------------------------------

    def _emit(self, mesh, mins, maxs):
        centre = tuple((mins[i] + maxs[i]) / 2.0 for i in range(3))
        self.vm.add_mesh(mesh, origin=centre)
        self.solids += 1

    def box(self, mins, maxs, mat=COVER, scale=0.25):
        """One solid box = one CMapMesh pivoted at its centre, like a Hammer
        brush. Zero-thickness boxes are skipped."""
        if any(maxs[i] - mins[i] < 0.5 for i in range(3)):
            return
        m = Mesh()
        m.add_box(mins, maxs, mat, scale=scale)
        self._emit(m, mins, maxs)

    def prism(self, polygon, z0, z1, mat=COVER, scale=0.25):
        """Vertical extrusion of a convex XY polygon (pillars, angled walls)."""
        m = Mesh()
        m.add_prism(polygon, z0, z1, mat, scale)
        xs = [p[0] for p in polygon]
        ys = [p[1] for p in polygon]
        self._emit(m, (min(xs), min(ys), z0), (max(xs), max(ys), z1))

    def ramp(self, mins, maxs, rise, mat=FLOOR, scale=0.25):
        """Wedge sloping up toward rise ('+x','-x','+y','-y'). Keep it at or
        under ~45 degrees or players slide off."""
        m = Mesh()
        m.add_ramp(mins, maxs, rise, mat, scale)
        self._emit(m, mins, maxs)

    def stairs(self, x0, y0, x1, y1, z_from, z_to, rise, step=16, mat=FLOOR):
        """Box steps climbing from z_from to z_to toward `rise`. Each riser is
        at most `step` (players walk up 18 without jumping)."""
        n = max(1, int(math.ceil((z_to - z_from) / float(step))))
        rise_h = (z_to - z_from) / float(n)
        along = 0 if rise[1] == "x" else 1
        lo, hi = (x0, x1) if along == 0 else (y0, y1)
        depth = (hi - lo) / float(n)
        if depth < 16:
            raise ValueError("stairs too steep: %.1f-unit treads (need >= 16)"
                             % depth)
        for k in range(n):
            a = lo + k * depth if rise[0] == "+" else hi - (k + 1) * depth
            b = a + depth
            top = z_from + (k + 1) * rise_h
            if along == 0:
                self.box((a, y0, z_from), (b, y1, top), mat, 0.5)
            else:
                self.box((x0, a, z_from), (x1, b, top), mat, 0.5)

    def panel(self, mins, maxs, axis, holes=(), mat=WALL):
        """A wall slab with rectangular openings cut through it.

        axis: 0 if the wall runs along X, 1 if along Y.
        holes: [(u0, u1, z0, z1)] in world coords along that axis.
        """
        lo, hi = mins[axis], maxs[axis]
        z0, z1 = mins[2], maxs[2]

        def seg(u0, u1, a, b):
            mn, mx = list(mins), list(maxs)
            mn[axis], mx[axis] = u0, u1
            mn[2], mx[2] = a, b
            self.box(mn, mx, mat)

        cur = lo
        for h0, h1, hz0, hz1 in sorted(holes):
            h0, h1 = max(h0, lo), min(h1, hi)
            seg(cur, h0, z0, z1)
            seg(h0, h1, z0, max(z0, hz0))      # sill under the opening
            seg(h0, h1, min(z1, hz1), z1)      # lintel above it
            cur = max(cur, h1)
        seg(cur, hi, z0, z1)

    def room(self, name, x0, y0, x1, y1, h, openings=None, z=0,
             walls=("south", "north", "west", "east"), floor=True,
             ceiling=True):
        """Enclose an interior box with floor, ceiling and four walls.

        Interior is x0..x1, y0..y1, from floor z to z+h. `openings` maps a side
        ('south' = -Y, 'north' = +Y, 'west' = -X, 'east' = +X) to a list of
        (u0, u1, z0, z1): u along the wall in world coords, z relative to this
        room's floor. Doorways in two adjoining rooms must line up.
        """
        o = openings or {}
        self.rooms[name] = {"bounds": [x0, y0, x1, y1], "z": z, "h": h}
        top = z + h
        if floor:
            self.box((x0 - T, y0 - T, z - FLOOR_D), (x1 + T, y1 + T, z), FLOOR, 0.5)
        if ceiling:
            self.box((x0 - T, y0 - T, top), (x1 + T, y1 + T, top + CEIL_D), CEIL, 0.5)

        def holes(side):
            return [(u0, u1, z + a, z + b) for u0, u1, a, b in o.get(side, [])]

        if "south" in walls:
            self.panel((x0 - T, y0 - T, z), (x1 + T, y0, top), 0, holes("south"))
        if "north" in walls:
            self.panel((x0 - T, y1, z), (x1 + T, y1 + T, top), 0, holes("north"))
        if "west" in walls:
            self.panel((x0 - T, y0, z), (x0, y1, top), 1, holes("west"))
        if "east" in walls:
            self.panel((x1, y0, z), (x1 + T, y1, top), 1, holes("east"))

    def room_bounds(self, name):
        """(x0, y0, x1, y1, z, h) of a room, for placing things relative to it."""
        return _room_tuple(self.rooms[name])

    # ---- gameplay entities ----------------------------------------------

    def point(self, name, x, y, z=None):
        """Named location the analysis tools report on (spawns, sites, any
        spot you want routed to). z defaults to the ground under it."""
        self.points[name] = [x, y] if z is None else [x, y, z]

    def spawns(self, team, cx, cy, yaw, count=5, spacing=128, z=0, per_row=5):
        """A grid of spawn points for 'T' or 'CT' centred on (cx, cy), facing
        `yaw` degrees (0 = +X/east, 90 = +Y/north)."""
        cls = {"T": "info_player_terrorist",
               "CT": "info_player_counterterrorist"}[team]
        rows = int(math.ceil(count / float(per_row)))
        for i in range(count):
            col, row = i % per_row, i // per_row
            ncol = min(per_row, count - row * per_row)
            x = cx + (col - (ncol - 1) / 2.0) * spacing
            y = cy + (row - (rows - 1) / 2.0) * spacing
            self.vm.add_entity(cls, (x, y, z + 16), (0, yaw, 0),
                               {"priority": "0", "enabled": "1"})
        self.points.setdefault(team.lower() + "_spawn", [cx, cy, z])

    def bombsite(self, letter, mins, maxs):
        """func_bomb_target. letter 'A' or 'B'. Registers point site_<letter>."""
        idx = "AB".index(letter.upper())
        self.vm.add_brush_entity("func_bomb_target", mins, maxs, TRIG, {
            "bomb_site_designation": str(idx), "heistbomb": "0",
            "useLocalOffset": "0"})
        self.points["site_" + letter.lower()] = [
            (mins[0] + maxs[0]) / 2.0, (mins[1] + maxs[1]) / 2.0, mins[2]]

    def buyzone(self, team, mins, maxs):
        self.vm.add_brush_entity("func_buyzone", mins, maxs, TRIG,
                                 {"TeamNum": str(TEAMS[team])})

    def map_parameters(self, **overrides):
        props = {"buying": "0", "bombradius": "500", "petpopulation": "0",
                 "usenormalspawnsfordm": "0",
                 "disableautogenerateddmspawns": "0",
                 "botmaxvisiondistance": "-1"}
        props.update({k: str(v) for k, v in overrides.items()})
        self.vm.add_entity("info_map_parameters", (0, 0, 32), props=props)

    def nav_seed(self, x, y, z=0):
        """point_nav_walkable. navgen floods outward from these; with none the
        nav mesh is empty and bots cannot move. One per enclosed area."""
        self.vm.add_entity("point_nav_walkable", (x, y, z + 16))

    def nav_seeds_for_rooms(self):
        for name in self.rooms:
            x0, y0, x1, y1, z, h = self.room_bounds(name)
            self.nav_seed(s16((x0 + x1) / 2.0), s16((y0 + y1) / 2.0), z)

    # ---- lighting ----------------------------------------------------------

    def lamp(self, x, y, z, **over):
        """light_omni2 with baked defaults. Override any key, e.g.
        brightness_lumens=20000, color='255 240 210', range=800."""
        p = dict(LIGHT_DEFAULTS)
        p.update({k: str(v) for k, v in over.items()})
        self.vm.add_entity("light_omni2", (x, y, z), (90, 0, 0), p)

    def light_room(self, name, spacing=640, drop=48, **over):
        """Grid of ceiling lamps across a room, `drop` below the ceiling."""
        x0, y0, x1, y1, z, h = self.room_bounds(name)
        nx = max(1, int(round((x1 - x0) / float(spacing))))
        ny = max(1, int(round((y1 - y0) / float(spacing))))
        for i in range(nx):
            for j in range(ny):
                self.lamp(s16(x0 + (x1 - x0) * (i + 0.5) / nx),
                          s16(y0 + (y1 - y0) * (j + 0.5) / ny), z + h - drop,
                          **over)

    def lightmap_volumes(self):
        """toolslightmapres boxes over every room. Without them the baker
        warns and falls back to a coarse lightmap texel density."""
        for name in self.rooms:
            x0, y0, x1, y1, z, h = self.room_bounds(name)
            self.box((x0, y0, z), (x1, y1, z + h), LMRES)

    def environment(self, sky="materials/editor/toolscene_lighting_sky_dust.vmat"):
        """Sky, sun, light-probe volume and minimap bounds, sized to the rooms.
        Call after all rooms exist."""
        xs = [v for r in self.rooms.values() for v in (r["bounds"][0], r["bounds"][2])]
        ys = [v for r in self.rooms.values() for v in (r["bounds"][1], r["bounds"][3])]
        zs = [r["z"] for r in self.rooms.values()]
        zt = [r["z"] + r["h"] for r in self.rooms.values()]
        lo = (min(xs) - 128, min(ys) - 128, min(zs) - 64)
        hi = (max(xs) + 128, max(ys) + 128, max(zt) + 64)
        c = tuple(s16((lo[i] + hi[i]) / 2.0) for i in range(3))
        vm = self.vm
        vm.add_entity("env_sky", (c[0], c[1], c[2]), props={
            "targetname": "sky", "StartDisabled": "0", "skyname": sky,
            "tint_color": "255 255 255", "brightnessscale": "1.0",
            "useLocalOffset": "0"})
        vm.add_entity("light_environment", (c[0], c[1], c[2] + 64), (75, 180, 0),
                      props={
            "enabled": "1", "color": "248 249 232", "brightness": "1.0",
            "range": "512", "castshadows": "1", "skycolor": "138 200 255",
            "skyintensity": "0.4", "skytexture": "sky", "skytexturescale": "1.0",
            "sunlightminbrightness": "32", "skydirectlight": "1",
            "skybouncescale": "1.0", "brightnessscale": "1.0",
            "bouncescale": "1.0", "directlight": "1", "indirectlight": "1",
            "angulardiameter": "0.5", "useLocalOffset": "0",
            "clientSideEntity": "1"})
        vm.add_entity("env_combined_light_probe_volume", c, props={
            "targetname": "sky", "useLocalOffset": "0", "StartDisabled": "0",
            "bakenearz": "2.0", "bakefarz": "4096.0", "moveable": "0",
            # box extents are relative to the entity origin
            "box_mins": "%g %g %g" % tuple(lo[i] - c[i] for i in range(3)),
            "box_maxs": "%g %g %g" % tuple(hi[i] - c[i] for i in range(3)),
            "voxel_size": "96.0", "flood_fill": "1", "voxelize": "1",
            "light_probe_volume_from_cubemap": "0", "storage": "-1",
            "indoor_outdoor_level": "0", "edge_fade_dists": "0 32 0",
            "clientSideEntity": "1"})
        vm.add_entity("cs_minimap_boundary", lo)
        vm.add_entity("cs_minimap_boundary", hi)

    # ---- output ----------------------------------------------------------

    def save(self, out_dir="out"):
        """Write <out_dir>/<name>.kv2.vmap and <name>.meta.json. Refuses to
        write a layout that breaks the room-separation rule."""
        problems = check_rooms(self.rooms)
        if problems:
            raise SystemExit("Room layout errors:\n  - " + "\n  - ".join(problems))
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, self.name + ".kv2.vmap")
        self.vm.save(path)
        with open(os.path.join(out_dir, self.name + ".meta.json"), "w") as f:
            json.dump({"name": self.name, "rooms": self.rooms,
                       "points": self.points}, f, indent=1)
        print("wrote %s  (%d solids, %d world nodes, %d rooms)"
              % (path, self.solids, len(self.vm.children), len(self.rooms)))
        return path
