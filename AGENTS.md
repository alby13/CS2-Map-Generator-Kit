# Instructions for AI assistants

This repository generates Counter-Strike 2 maps from Python.

**Before doing anything, read [AI_GUIDE.md](AI_GUIDE.md) in full.** It has the
ground rules (never overwrite the user's maps, measure before compiling, you
cannot see the game), the scale references, the Builder API, the layout rules
that cause silent failures, and the format notes.

Start every session with `python -m mapgen find`, and start new maps by
copying `examples/test_map.py`.