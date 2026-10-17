"""Generate Counter-Strike 2 maps (.vmap) from Python, and verify them.

    from mapgen import Builder
"""
from .builder import Builder, check_rooms, s16, DOOR_Z, GAP, T  # noqa: F401
from .vmap import Mesh, VMap  # noqa: F401