"""A small, playable defusal test map: T spawn -> hall -> arena with one
bombsite -> hall -> CT spawn. Good for checking the pipeline works and for
getting a feel for CS2 scale before designing a real layout.

    python examples/test_map.py                 # writes out/test_map.kv2.vmap
    python -m mapgen check out/test_map.kv2.vmap
    python -m mapgen build out/test_map.kv2.vmap --addon YOUR_ADDON

Plan (+Y is north):

          CT_SPAWN
             |
          CT_HALL
             |
    +--------+--------+
    | plat.   ARENA  A|     raised platform (stairs + ramp) in the west,
    |      [==]       |     bombsite A in the north-east
    +--------+--------+
             |
          T_HALL
             |
          T_SPAWN
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from mapgen import Builder, DOOR_Z  # noqa: E402
from mapgen.builder import FLOOR, WALL  # noqa: E402

b = Builder("test_map")

# ---------------------------------------------------------------- rooms
# Side-by-side rooms are exactly 64 apart (walls are 32 thick and sit outside
# each interior). Doorways in both rooms of a pair must line up.
DOOR = (-128, 128)            # 256 wide, the full width of the halls
D = DOOR + DOOR_Z             # (u0, u1, z0, z1)

b.room("T_SPAWN", -384, -1408, 384, -896, 256, {"north": [D]})
b.room("T_HALL", -128, -832, 128, -64, 224, {"south": [D], "north": [D]})
b.room("ARENA", -1024, 0, 1024, 1280, 320, {"south": [D], "north": [D]})
b.room("CT_HALL", -128, 1344, 128, 1856, 224, {"south": [D], "north": [D]})
b.room("CT_SPAWN", -384, 1920, 384, 2432, 256, {"south": [D]})

# ---------------------------------------------------------------- arena
# Cover heights: 64 shoot over, 96 waist high, 128 full standing cover.
b.box((-128, 448, 0), (128, 512, 128))        # blocks the door-to-door line
b.box((-640, 320, 0), (-512, 448, 64))
b.box((512, 256, 0), (640, 384, 96))

# Raised platform in the west, reachable two ways
b.box((-1024, 896, 0), (-832, 1280, 64), FLOOR)
b.stairs(-832, 960, -704, 1088, 0, 64, "-x")   # four 16-unit steps, climbing west
b.ramp((-1024, 768, 0), (-832, 896, 64), "+y")  # 64 rise over 128 = 27 degrees
b.point("platform", -928, 1088, 64)

# An octagonal pillar (any convex footprint works)
r, k = 64, 26                                   # k ~ r * tan(22.5)
cx, cy = -384, 832
b.prism([(cx + k, cy - r), (cx + r, cy - k), (cx + r, cy + k), (cx + k, cy + r),
         (cx - k, cy + r), (cx - r, cy + k), (cx - r, cy - k), (cx - k, cy - r)],
        0, 320, WALL)

# Bombsite A with cover inside it
b.bombsite("A", (256, 704, 0), (768, 1088, 192))
b.box((320, 960, 0), (448, 1088, 64))
b.box((640, 768, 0), (768, 896, 96))

# ---------------------------------------------------------------- gameplay
b.spawns("T", 0, -1152, yaw=90, count=5)        # yaw 90 = facing north
b.spawns("CT", 0, 2176, yaw=270, count=5)
b.buyzone("T", (-352, -1376, 0), (352, -928, 192))
b.buyzone("CT", (-352, 1952, 0), (352, 2400, 192))
b.map_parameters()

b.nav_seeds_for_rooms()
b.nav_seed(-928, 1088, 64)                      # the platform top too

# ---------------------------------------------------------------- lighting
for name in b.rooms:
    b.light_room(name)
b.lamp(512, 896, 272, brightness_lumens=20000, color="255 240 210")  # site
b.lightmap_volumes()
b.environment()

b.save(os.path.join(os.path.dirname(__file__), "..", "out"))
