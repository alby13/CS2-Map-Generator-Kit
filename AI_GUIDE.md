# AI guide: building CS2 maps with mapgen

You are helping someone build a Counter-Strike 2 map. This repo lets you
write the map as Python, verify it with real measurements, and compile it with
Valve's own tools, without anyone clicking brushes in Hammer. Read this whole
file before writing code; most of it exists because something went wrong once.

What you produce is a **blockout**: correct scale, layout, routes, cover and
every gameplay entity, in dev textures. It compiles and is fully playable.
The art pass (materials, props) happens later in Hammer.

---

## 1. Ground rules

1. **Start with `python -m mapgen find`.** It confirms CS2, the Workshop Tools
   and the user's addons. If there is no addon, the user must create one in the
   *Counter-Strike 2 Workshop Tools* launcher ("Create New Addon"). You cannot
   do that step for them.
2. **Never overwrite the user's work.** Use a new map name for anything you
   generate. `install` refuses to replace an existing map unless given
   `--force`, and even then keeps a timestamped backup. Ask before using it.
3. **Regenerating discards Hammer edits.** Once the user has edited a map in
   Hammer, the Python script is no longer the source of truth. Ask before
   regenerating over it, and suggest they switch to Hammer for good once the
   layout has settled.
4. **Measure, don't eyeball.** `check`, `routes` and `plan` run in seconds on
   the generated text file. A compile takes minutes. Iterate on measurements
   and compile only when they look right.
5. **You cannot see the game.** "Compiled with 0 failed" is not "works". Report
   what you verified, then ask the user to playtest and tell you how it feels.
   A map that measures well can still play badly.
6. **Report honestly.** If `check` reports errors, the map is not done. If you
   skip a check, say so.

## 2. The pipeline

```
examples/your_map.py --> out/NAME.kv2.vmap     (KV2 text DMX, what Python writes)
                         out/NAME.meta.json    (rooms + named points, for the tools)
       |
       |  python -m mapgen check / routes / plan     <- seconds; iterate here
       v
python -m mapgen install  --> <CS2>/content/csgo_addons/ADDON/maps/NAME.vmap  (binary, via dmxconvert)
python -m mapgen compile  --> <CS2>/game/csgo_addons/ADDON/maps/NAME.vpk      (via resourcecompiler)
       |
       v
user plays it:  map NAME   (CS2 launched from the Workshop Tools with the addon)
```

`python -m mapgen build out/NAME.kv2.vmap --addon ADDON` runs check, install
and compile in one go and stops if `check` finds errors.

The `.vmap` format is binary DMX. Valve's `dmxconvert.exe` converts it to and
from a text form (keyvalues2) losslessly; that is what makes all of this work.

## 3. Coordinates and scale

- Units are roughly inches. **+Z is up. +Y is north.**
- Yaw: 0 = facing +X (east), 90 = +Y (north), 180 = west, 270 = south.
- Work on a **16-unit grid** (`s16()` snaps a value).

| Thing | Size |
|---|---|
| Player hull | 32 x 32 wide, 72 tall standing, 54 crouched |
| Step without jumping | 18 |
| Walkable slope | up to ~45 degrees |
| Doorway | 128+ wide, 128+ tall (kit default 256 x 224) |
| Corridor | 192-384 wide |
| Room height | 224-448 |

Cover heights, which decide how a fight plays:

| Height | Effect |
|---|---|
| 16 | a step: walked over |
| 64 | shoot over it standing, hide crouched |
| 96 | waist high: strong cover, peekable |
| 128 | full standing cover, no shooting over |
| 192+ | blocks everything; reads as a wall |

Timing targets at rifle speed (215 u/s), roughly what popular competitive maps
do. Use them as starting points, then trust playtests:

- T spawn to a bombsite: about **12-15 s**
- CT spawn to a bombsite: about **6-9 s**, so CT arrives ~5-8 s before T
- the two sites within ~1-2 s of each other for each team, unless deliberate

`routes` measures paths that keep the player's body 16 units off walls, so
its numbers run slightly longer than wall-hugging estimates.

## 4. The Builder API

```python
from mapgen import Builder, DOOR_Z, s16
from mapgen.builder import WALL, FLOOR, COVER   # proven dev materials

b = Builder("my_map")                     # files are named after this

# Rooms: interior x0..x1, y0..y1, floor at z, height h. Walls (32 thick),
# floor and ceiling are added around the interior.
# openings: side -> [(u0, u1, z0, z1)]; u along the wall in WORLD coords,
# z relative to this room's floor. Sides: south(-Y) north(+Y) west(-X) east(+X).
b.room("HALL", -128, 0, 128, 768, 224, {"south": [(-128, 128) + DOOR_Z]})
b.room_bounds("HALL")                     # -> (x0, y0, x1, y1, z, h)

b.box(mins, maxs, mat=COVER)              # one solid box
b.prism([(x, y), ...], z0, z1, mat)       # extruded CONVEX polygon (pillars)
b.ramp(mins, maxs, "+x")                  # wedge rising toward +x/-x/+y/-y
b.stairs(x0, y0, x1, y1, z_from, z_to, "-x", step=16)
b.panel(mins, maxs, axis, holes)          # free-standing wall with openings

b.spawns("T", cx, cy, yaw=90, count=5)    # also registers point t_spawn
b.spawns("CT", cx, cy, yaw=270, count=5)
b.bombsite("A", mins, maxs)               # func_bomb_target, point site_a
b.buyzone("T", mins, maxs)                # and "CT"
b.map_parameters()                        # info_map_parameters
b.nav_seeds_for_rooms(); b.nav_seed(x, y, z)
b.point("name", x, y, z=None)             # any spot routes/check should report

b.light_room("HALL")                      # grid of baked ceiling lamps
b.lamp(x, y, z, brightness_lumens=20000, color="255 240 210")
b.lightmap_volumes()                      # call after all rooms exist
b.environment()                           # sky, sun, probes, minimap bounds

b.save("out")                             # refuses layouts that break rules
```

`examples/test_map.py` uses all of it in ~80 lines. Copy it as a starting point.

## 5. Rules that bite

These all came from real failures. The tools enforce most of them.

**Side-by-side rooms must be exactly 64 apart.** Walls are 32 thick and sit
outside each interior; floor and ceiling slabs reach 32 past it. At 64 the
walls sit back to back and the floors meet exactly. Closer, and the floor
slabs overlap (z-fighting on a walkable floor). Further, and there is a strip
with **no floor at all**: the compile still succeeds, navgen still fills the
isolated room from its own seed, and the only symptom is the player being
unable to leave spawn. `save()` refuses gaps from 0 to 256 that are not 64.

**Doorways must line up.** Both rooms need an opening at the same `u` range,
and the corridor between them must be at least as wide. `check` finds sealed
rooms; `plan` draws them red.

**Stacked rooms need 96 units vertically** between the lower ceiling and the
upper floor (32 ceiling slab + 64 floor slab).

**One convex solid per mesh.** Each `box/prism/ramp` call makes its own
CMapMesh. Merging touching solids into one mesh welds their vertices into a
non-manifold mesh. Concave shapes must be split into convex pieces.

**Every enclosed area needs a `point_nav_walkable`**, or navgen produces an
empty nav mesh and bots cannot move. Put seeds on open floor, not inside cover.

**Lights are baked.** `lamp()` defaults to `directlight 1` (static). The FGD
default, stationary, shares a tiny pool of shadow slots: 26 stationary lights
left 21 without shadows. Static lights only appear after a full compile, which
`compile` always does. Enclosed rooms need `light_omni2` lamps;
`light_environment` (the sun) does not reach under ceilings.

**Add lightmap volumes.** Without a `toolslightmapres` box around the play
space the baker warns and uses a coarse default resolution.

**Only reference materials you know exist.** Proven in stock CS2:
`materials/dev/dev_measuregeneric01.vmat`, `materials/dev/reflectivity_30.vmat`,
`materials/tools/toolstrigger.vmat`, `materials/tools/toolslightmapres.vmat`,
`materials/tools/toolsskybox.vmat`. To use others, find them in a map the user
saved from Hammer (`python -m mapgen info that_map.vmap` lists materials).

## 6. The verification tools

| Command | What it tells you |
|---|---|
| `check MAP` | room rules, mesh topology, entity sanity, and walkability: every spawn, bombsite, buy zone, room and named point reachable from T spawn; sealed floors listed |
| `routes MAP` | walking time from each spawn to each site and named point, CT lead, site asymmetry, rotation time |
| `plan MAP` | SVG top-down view of what is *reachable*: floor, raised areas, sealed areas in red, cover heights, sites, spawns, lights |
| `info ANY.vmap` | entities (with every property) and materials of any map, binary or text |

`check` exits non-zero on errors. Warnings deserve a look; "raised surfaces
are unreachable" is normal (tops of cover).

How walkability is modelled: the map is sampled on a 16-unit grid; in each
column the vertical spans of solids are computed exactly from each mesh's face
planes; the top of each span with 72 units of headroom and solid above is a
standing spot. A player moves to a neighbouring column if its surface is at
most 18 higher (a step) or lower (a drop), the body fits, and diagonals do not
clip corners. `--jump N` also allows climbing N units.

Limits: 16-unit cells approximate the 32-unit hull; crouch-only gaps, ladders
and func_ brush entities are not modelled; meshes are assumed convex (Hammer
meshes that are not fall back to their bounding box, with a warning).

All tools work on Hammer-saved maps too (binary files are converted
automatically). Without a `.meta.json` they infer spawns from the spawn
entities' centroid and sites from `func_bomb_target`.

## 7. Compiling

`compile` (or `build`) runs a full compile: geometry, baked lighting, nav,
audio, VPK. Expect ~2 minutes for the example and 5-15 minutes for a full map.
The summary shows:

- `N compiled, 0 failed` - anything else is a failure
- nav areas - 0 means missing nav seeds
- shadow slots failed - should be 0
- whether the `.vpk` exists
- problem lines, with known-benign stock messages filtered out (e.g.
  `detail_prop_types.vdata_c`, `csgo_mega`, VPK signing, leak reports)

The full log goes to `out/NAME.compile.log`.

To play: open the map in Hammer and build/run it from there, or launch CS2
from the Workshop Tools launcher with the addon selected and type
`map NAME` in the console. Bombs plant and rounds play normally.

## 8. Going beyond the test map

A good way to grow a full map:

1. **Agree a structure with the user first.** A classic defusal layout is two
   bombsites, T spawn on one side, CT spawn opposite and closer to the sites,
   three lanes (one to each site plus a contested middle) and connectors that
   let CT rotate between sites. Ask about their theme and what they want it
   to feel like.
2. **Lay it out in bands.** Define each row of rooms as named coordinate
   constants at the top of the script, with exact 64 gaps between bands, and
   place cover and lights relative to room bounds (`room_bounds()`), so moving
   a band drags its contents along.
3. **Measure, adjust, repeat.** Generate, `check`, `routes`. If T takes too
   long to reach the sites, shorten the bands between T spawn and the sites
   rather than moving things at random. Show the user the timing table.
4. **Break up long sightlines** with chokes (`panel` with a doorway), columns
   and cover of mixed heights. A long straight corridor is the most common
   blockout problem.
5. **Compile and hand it over** for a playtest with specific questions (does
   mid feel too long, are rotations too easy) rather than "how is it".
6. **Move to Hammer** for the art pass once the layout plays well.

Multi-level spaces: give `room()` a `z`, connect levels with `stairs()`
(16-unit risers) or `ramp()`s of 45 degrees or less, and add a `point()` on
the upper level so `check` proves it is reachable.

Not supported yet, so verify carefully if you add it: props (`prop_static`),
custom materials, displacements, curved geometry, ladders, doors, 3D skybox.
The way to add a feature safely: have the user place one in Hammer and save,
run `python -m mapgen info` on that map to see exactly what Hammer wrote, emit
the same from code, then compile and playtest.

## 9. Format notes (for extending `mapgen/vmap.py`)

Verified against a Hammer-saved map (vmap format 40, editorbuild 9806) and by
compiling and playing generated maps.

- Root: `$prefix_element$` (asset references) then `CMapRootElement`, whose
  `world` (`CMapWorld`) has `children`: `CMapMesh` and `CMapEntity` elements,
  inline or referenced by id.
- Geometry is `CDmePolygonMesh`, a half-edge mesh. Half-edge twins are
  `(2k, 2k+1)`; `edgeVertexIndices[h]` is h's **destination**;
  `edgeNextIndices` walks a face loop; `vertexEdgeIndices[v]` is an
  **outgoing** half-edge; faces wind CCW seen from outside, normal =
  `cross(p1-p0, p2-p1)`.
- Vertex positions are local to the mesh `origin` (a pivot):
  world = origin + local.
- Brush entities (`func_bomb_target`, `func_buyzone`) are a `CMapEntity` with
  a `CMapMesh` child. The entity origin and mesh origin are the **same point**,
  not stacked transforms.
- Per-face texture data: `textureAxisU/V` (xyz + offset) and `textureScale`;
  `u = (dot(P_world, axisU) / scaleU + axisU.w) / texture_width`. Texcoords,
  normals and tangents are derived data; Hammer recomputes them.
- Entity keyvalues live in `entity_properties` (`EditGameClassProps`), all as
  strings. Unset keys take the FGD default. FGDs are in `<CS2>/game/core` and
  `<CS2>/game/csgo` if you need the full key list for an entity.

## 10. Troubleshooting

| Symptom | Cause |
|---|---|
| `Addon '...' does not exist` | create it in the Workshop Tools launcher |
| `dmxconvert.exe not found` | install the *Counter-Strike 2 Workshop Tools* DLC |
| player cannot leave a room | doorway misaligned or a room gap that is not 64; run `check`, look at `plan` |
| bots stand still, nav areas 0 | no `point_nav_walkable`, or seeds inside solids |
| map is dark | no `light_omni2` in enclosed rooms, or lights not baked yet (full compile) |
| `Light Shadow Slots ... N failed` | too many stationary lights; use `directlight 1` |
| `No lightmap resolution volumes` | call `b.lightmap_volumes()` |
| `face ... reuses directed edge` | two solids in one Mesh, or a duplicate face |
| `ModuleNotFoundError: numpy` | `pip install -r requirements.txt` |
