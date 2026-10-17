"""Load a .vmap (KV2 text) and analyse it: mesh topology, walkability,
route distances. Works on builder output and on Hammer-saved maps (convert
those to text first; `python -m mapgen info` does it for you).

Walkability model (2.5D, multi-level):
  The map is sampled on a CELL-unit grid. In each grid column the vertical
  spans occupied by solids are collected - exactly, from each convex mesh's
  face planes - and merged. The top of each span is a candidate standing
  surface. It becomes a node if
    * something solid is above it (bare roof tops are ignored),
    * there is PLAYER_H of headroom, and
    * the four neighbouring columns are clear from STEP to PLAYER_H above it
      (an approximation of the 32-unit-wide player body).
  From a node a player can move to a neighbouring column's highest surface
  at or below z + STEP: walk up a step, or walk off a ledge and drop.
  Optional `jump` adds edges up to z + jump.

Known limits: 16-unit cells approximate the player hull; meshes are assumed
convex (true for everything the builder makes; non-convex ones fall back to
their bounding box with a warning); brush entities (func_*) are treated as
non-solid; crouch-only gaps and ladders are not modelled.
"""
import heapq
import math
from collections import defaultdict

import numpy as np

from .dmx import DmxFile, Element

CELL = 16
PLAYER_H = 72        # standing hull height (54 crouched)
STEP = 18            # walkable step-up without jumping
RIFLE_SPEED = 215.0  # u/s carrying an AK-47; M4 is 225, knife 250
NONSOLID = ("toolstrigger", "toolslightmapres")

_DIAG = 2 ** 0.5
_NBR8 = [(1, 0, 1.0), (-1, 0, 1.0), (0, 1, 1.0), (0, -1, 1.0),
         (1, 1, _DIAG), (1, -1, _DIAG), (-1, 1, _DIAG), (-1, -1, _DIAG)]
_NBR4 = [(1, 0), (-1, 0), (0, 1), (0, -1)]


def _v(s):
    return [float(c) for c in str(s).split()]


def angle_matrix(pitch, yaw, roll):
    """Source's AngleMatrix (degrees). Columns are forward, left, up."""
    p, y, r = (math.radians(a) for a in (pitch, yaw, roll))
    sp, cp = math.sin(p), math.cos(p)
    sy, cy = math.sin(y), math.cos(y)
    sr, cr = math.sin(r), math.cos(r)
    return [[cp * cy, sp * sr * cy - cr * sy, sp * cr * cy + sr * sy],
            [cp * sy, sp * sr * sy + cr * cy, sp * cr * sy - sr * cy],
            [-sp, sr * cp, cr * cp]]


def _newell(pts):
    nx = ny = nz = 0.0
    n = len(pts)
    for i in range(n):
        a, b = pts[i], pts[(i + 1) % n]
        nx += (a[1] - b[1]) * (a[2] + b[2])
        ny += (a[2] - b[2]) * (a[0] + b[0])
        nz += (a[0] - b[0]) * (a[1] + b[1])
    m = (nx * nx + ny * ny + nz * nz) ** 0.5 or 1.0
    return (nx / m, ny / m, nz / m)


# ------------------------------------------------------------------ loading

class Solid:
    def __init__(self, faces_world, materials, solid):
        self.materials = materials
        self.solid = solid
        pts = [p for f in faces_world for p in f]
        self.mins = [min(p[i] for p in pts) for i in range(3)]
        self.maxs = [max(p[i] for p in pts) for i in range(3)]
        cen = [sum(p[i] for p in pts) / len(pts) for i in range(3)]
        N, D = [], []
        for f in faces_world:
            n = _newell(f)
            c = [sum(p[i] for p in f) / len(f) for i in range(3)]
            if sum(n[i] * (c[i] - cen[i]) for i in range(3)) < 0:
                n = (-n[0], -n[1], -n[2])          # tolerate inward winding
            N.append(n)
            D.append(n[0] * c[0] + n[1] * c[1] + n[2] * c[2])
        self.N, self.D = np.array(N), np.array(D)
        P = np.array(pts)
        # convex (and outward-facing) iff every vertex is inside every plane
        self.convex = bool(np.all(P @ self.N.T <= self.D + 0.05))
        if not self.convex:
            mn, mx = self.mins, self.maxs
            self.N = np.array([[1, 0, 0], [-1, 0, 0], [0, 1, 0], [0, -1, 0],
                               [0, 0, 1], [0, 0, -1]], dtype=float)
            self.D = np.array([mx[0], -mn[0], mx[1], -mn[1], mx[2], -mn[2]])


class Entity:
    def __init__(self, classname, origin, angles, props, bounds):
        self.classname = classname
        self.origin = origin
        self.angles = angles
        self.props = props
        self.bounds = bounds        # (mins, maxs) for brush entities, else None

    def __repr__(self):
        return "<%s @ %s>" % (self.classname, self.origin)


class MapData:
    """Solids, entities and topology problems of one KV2 text .vmap."""

    def __init__(self, path):
        self.path = path
        self.dmx = DmxFile.load(path)
        self.solids, self.entities, self.warnings = [], [], []
        self.meshes = []                 # raw CMapMesh elements (for validate)
        self._ids = {}
        for e in self.dmx.elements:
            self._index(e)
        roots = [e for e in self.dmx.elements if e.etype == "CMapRootElement"]
        if not roots:
            raise ValueError("%s has no CMapRootElement - not a map?" % path)
        self.root = roots[0]
        for c in self.root.get("world").get("children") or []:
            self._visit(self._res(c))
        self.materials = sorted({m for s in self.solids for m in s.materials})

    def _index(self, el):
        if isinstance(el, Element):
            self._ids[el.id] = el
            for n, t, v in el.attrs:
                if isinstance(v, Element):
                    self._index(v)
                elif t == "element_array":
                    for it in v:
                        self._index(it)

    def _res(self, x):
        return self._ids.get(x) if isinstance(x, str) else x

    def _visit(self, el):
        if not isinstance(el, Element):
            return
        if el.etype == "CMapMesh":
            self.solids += self._mesh(el)
        elif el.etype == "CMapEntity":
            ep = el.get("entity_properties")
            props = {n: v for n, t, v in ep.attrs} if ep else {}
            bounds = None
            for k in el.get("children") or []:
                km = self._res(k)
                if isinstance(km, Element) and km.etype == "CMapMesh":
                    for s in self._mesh(km):
                        if bounds is None:
                            bounds = (list(s.mins), list(s.maxs))
                        else:
                            bounds = ([min(bounds[0][i], s.mins[i]) for i in range(3)],
                                      [max(bounds[1][i], s.maxs[i]) for i in range(3)])
            self.entities.append(Entity(props.get("classname", "?"),
                                        _v(el.get("origin", "0 0 0")),
                                        _v(el.get("angles", "0 0 0")),
                                        props, bounds))
        elif el.get("children") is not None:
            o, a = el.get("origin"), el.get("angles")
            if (o and any(abs(c) > 1e-3 for c in _v(o))) or \
                    (a and any(abs(c) > 1e-3 for c in _v(a))):
                self.warnings.append(
                    "%s has a non-identity transform; its children are read "
                    "as world-space (group transforms are not applied)" % el.etype)
            for c in el.get("children"):
                self._visit(self._res(c))

    def _mesh(self, m):
        md = m.get("meshData")
        if md is None:
            return []
        self.meshes.append(m)
        streams = {s.get("name"): s for s in
                   (self._res(x) for x in md.get("vertexData").get("streams"))}
        pos = [_v(p) for p in streams["position:0"].get("data")]
        vdi = [int(x) for x in md.get("vertexDataIndices")]
        eV = [int(x) for x in md.get("edgeVertexIndices")]
        eN = [int(x) for x in md.get("edgeNextIndices")]
        fE = [int(x) for x in md.get("faceEdgeIndices")]
        mats_all = md.get("materials") or []
        fstreams = {s.get("name"): s for s in
                    (self._res(x) for x in md.get("faceData").get("streams"))}
        mi = fstreams.get("materialindex:0")
        face_mat = [int(x) for x in mi.get("data")] if mi else [0] * len(fE)

        o = _v(m.get("origin", "0 0 0"))
        R = angle_matrix(*_v(m.get("angles", "0 0 0")))
        sc = _v(m.get("scales", "1 1 1"))

        def world(p):
            q = (p[0] * sc[0], p[1] * sc[1], p[2] * sc[2])
            return tuple(o[r] + R[r][0] * q[0] + R[r][1] * q[1] + R[r][2] * q[2]
                         for r in range(3))

        faces = []
        for f in fE:
            loop, h = [], f
            for _ in range(len(eN) + 1):
                loop.append(world(pos[vdi[eV[h]]]))
                h = eN[h]
                if h == f:
                    break
            faces.append(loop)
        def mat(fi):
            i = face_mat[fi] if fi < len(face_mat) else 0
            return mats_all[i] if 0 <= i < len(mats_all) else ""

        def is_solid(ms):
            return not all(any(t in u for t in NONSOLID) for u in ms)

        if any(int(x) == -1 for x in md.get("edgeFaceIndices")):
            # Open mesh (Hammer allows single-sided planes, e.g. a floor that
            # is just a quad): give each face a 2-unit slab behind it.
            out = []
            for fi, f in enumerate(faces):
                n = _newell(f)
                back = [tuple(p[i] - 2.0 * n[i] for i in range(3)) for p in f]
                k = len(f)
                polys = [f, back[::-1]] + [[f[i], back[i], back[(i + 1) % k],
                                             f[(i + 1) % k]] for i in range(k)]
                out.append(Solid(polys, [mat(fi)], is_solid([mat(fi)])))
            return out
        used = sorted({mat(fi) for fi in range(len(faces))})
        s = Solid(faces, used, is_solid(used))
        if s.solid and not s.convex:
            self.warnings.append("non-convex mesh near %s treated as its "
                                 "bounding box" % [round(c) for c in s.mins])
        return [s]

    def of_class(self, *classes):
        return [e for e in self.entities if e.classname in classes]


# ------------------------------------------------------------------ topology

def validate_topology(data):
    """Half-edge consistency, closure, outward orientation and planarity of
    every mesh. Returns (errors, warnings) as lists of strings. Open meshes
    are only a warning: Hammer allows them, the builder never makes them."""
    errs, warns = [], []
    for mi, m in enumerate(data.meshes):
        md = m.get("meshData")
        tag = "mesh %d (origin %s)" % (mi, m.get("origin"))
        eV = [int(x) for x in md.get("edgeVertexIndices")]
        eO = [int(x) for x in md.get("edgeOppositeIndices")]
        eN = [int(x) for x in md.get("edgeNextIndices")]
        eF = [int(x) for x in md.get("edgeFaceIndices")]
        fE = [int(x) for x in md.get("faceEdgeIndices")]
        vE = [int(x) for x in md.get("vertexEdgeIndices")]
        pstream = [data._res(s) for s in md.get("vertexData").get("streams")]
        raw = [_v(p) for p in [s for s in pstream
                               if s.get("name") == "position:0"][0].get("data")]
        vdi = [int(x) for x in md.get("vertexDataIndices")]
        pos = [raw[i] for i in vdi]
        nhe, nv, nf = len(eV), len(pos), len(fE)
        if not (len(eO) == len(eN) == len(eF) == nhe):
            errs.append("%s: half-edge array lengths differ" % tag)
            continue
        bad = False
        for h in range(nhe):
            if not (0 <= eO[h] < nhe) or eO[eO[h]] != h:
                errs.append("%s: opposite of half-edge %d is inconsistent" % (tag, h))
                bad = True
                break
        if bad:
            continue
        seen = set()
        vol = 0.0
        for fi in range(nf):
            h = start = fE[fi]
            loop = []
            for _ in range(nhe + 1):
                if eF[h] != fi or h in seen:
                    errs.append("%s: face %d loop is broken" % (tag, fi))
                    break
                seen.add(h)
                loop.append(pos[eV[h]])
                h = eN[h]
                if h == start:
                    break
            if len(loop) < 3:
                errs.append("%s: face %d has %d vertices" % (tag, fi, len(loop)))
                continue
            n = _newell(loop)
            d = sum(n[i] * loop[0][i] for i in range(3))
            if any(abs(sum(n[i] * p[i] for i in range(3)) - d) > 0.05 for p in loop):
                errs.append("%s: face %d is not planar" % (tag, fi))
            p0 = loop[0]
            for k in range(1, len(loop) - 1):
                a, b = loop[k], loop[k + 1]
                vol += (p0[0] * (a[1] * b[2] - a[2] * b[1])
                        - p0[1] * (a[0] * b[2] - a[2] * b[0])
                        + p0[2] * (a[0] * b[1] - a[1] * b[0])) / 6.0
        if any(f == -1 for f in eF):
            warns.append("%s: open mesh (%d boundary half-edges) - fine in "
                         "Hammer, but builder solids should be closed"
                         % (tag, sum(1 for f in eF if f == -1)))
        elif nv - nhe // 2 + nf != 2:
            errs.append("%s: Euler characteristic %d, expected 2"
                        % (tag, nv - nhe // 2 + nf))
        elif vol <= 0:
            errs.append("%s: faces point inward (signed volume %.0f)" % (tag, vol))
        for v in range(nv):
            if not (0 <= vE[v] < nhe) or eV[eO[vE[v]]] != v:
                errs.append("%s: vertexEdgeIndices[%d] is not outgoing" % (tag, v))
                break
    return errs, warns


# ------------------------------------------------------------------ walkability

class NavGrid:
    def __init__(self, data, cell=CELL, height=PLAYER_H, step=STEP, jump=None):
        self.cell, self.height, self.step = cell, height, step
        self.climbs = (step,) if not jump or jump <= step else (step, jump)
        solids = [s for s in data.solids if s.solid]
        if not solids:
            raise ValueError("map has no solid geometry")
        self.X0 = min(s.mins[0] for s in solids)
        self.Y0 = min(s.mins[1] for s in solids)
        self.nx = int((max(s.maxs[0] for s in solids) - self.X0) / cell) + 1
        self.ny = int((max(s.maxs[1] for s in solids) - self.Y0) / cell) + 1

        cols = defaultdict(list)
        for s in solids:
            i0 = int(math.ceil((s.mins[0] - self.X0) / cell - 0.5))
            i1 = int(math.floor((s.maxs[0] - self.X0) / cell - 0.5))
            j0 = int(math.ceil((s.mins[1] - self.Y0) / cell - 0.5))
            j1 = int(math.floor((s.maxs[1] - self.Y0) / cell - 0.5))
            if i1 < i0 or j1 < j0:
                continue
            I, J = np.meshgrid(np.arange(i0, i1 + 1), np.arange(j0, j1 + 1),
                               indexing="ij")
            PX = self.X0 + (I + 0.5) * cell
            PY = self.Y0 + (J + 0.5) * cell
            lo = np.full(PX.shape, -np.inf)
            hi = np.full(PX.shape, np.inf)
            ok = np.ones(PX.shape, dtype=bool)
            for n, d in zip(s.N, s.D):
                if n[2] > 1e-6:
                    hi = np.minimum(hi, (d - n[0] * PX - n[1] * PY) / n[2])
                elif n[2] < -1e-6:
                    lo = np.maximum(lo, (d - n[0] * PX - n[1] * PY) / n[2])
                else:
                    ok &= n[0] * PX + n[1] * PY <= d + 1e-3
            ok &= np.isfinite(lo) & np.isfinite(hi) & (hi > lo + 1e-3)
            for a, b, zl, zh in zip(I[ok], J[ok], lo[ok], hi[ok]):
                cols[(int(a), int(b))].append((float(zl), float(zh)))

        self.iv = {}
        for key, spans in cols.items():
            spans.sort()
            merged = [list(spans[0])]
            for zl, zh in spans[1:]:
                if zl <= merged[-1][1] + 0.5:
                    merged[-1][1] = max(merged[-1][1], zh)
                else:
                    merged.append([zl, zh])
            self.iv[key] = merged

        self.nodes = set()
        for (i, j), spans in self.iv.items():
            for k in range(len(spans) - 1):        # top span: nothing above it
                z = spans[k][1]
                if spans[k + 1][0] - z < height:
                    continue
                if all(self._free((i + di, j + dj), z + step, z + height)
                       for di, dj in _NBR4):
                    self.nodes.add((i, j, k))

    # -- helpers -------------------------------------------------------------

    def _free(self, col, a, b):
        for zl, zh in self.iv.get(col, ()):
            if zl < b - 0.5 and zh > a + 0.5:
                return False
        return True

    def z(self, node):
        return self.iv[(node[0], node[1])][node[2]][1]

    def xy(self, node):
        return (self.X0 + (node[0] + 0.5) * self.cell,
                self.Y0 + (node[1] + 0.5) * self.cell)

    def col_of(self, x, y):
        return (int(math.floor((x - self.X0) / self.cell)),
                int(math.floor((y - self.Y0) / self.cell)))

    def edges(self, node):
        i, j, k = node
        spans = self.iv[(i, j)]
        z = spans[k][1]
        own_above = spans[k + 1][0]
        seen = set()
        for di, dj, w in _NBR8:
            col = (i + di, j + dj)
            spans2 = self.iv.get(col)
            if not spans2:
                continue
            for climb in self.climbs:
                k2 = None
                for kk in range(len(spans2) - 1, -1, -1):
                    if spans2[kk][1] <= z + climb + 1e-3:
                        k2 = kk
                        break
                if k2 is None or (col[0], col[1], k2) not in self.nodes:
                    continue
                t = spans2[k2][1]
                need = max(z, t) + self.height
                if spans2[k2 + 1][0] < need - 0.5:
                    continue                      # body does not fit through
                if t > z + self.step and own_above < t + self.height - 0.5:
                    continue                      # no headroom to jump
                if di and dj:
                    lo = max(z, t) + self.step
                    if not (self._free((i + di, j), lo, need)
                            and self._free((i, j + dj), lo, need)):
                        continue
                n2 = (col[0], col[1], k2)
                if n2 not in seen:
                    seen.add(n2)
                    yield n2, w * self.cell

    def node_at(self, x, y, z=None, radius=4):
        """Nearest node to a world point. With z, the highest surface at or
        just below it (entity origins sit ~16 above the floor); without z,
        the lowest surface (ground level)."""
        ci, cj = self.col_of(x, y)
        best = None
        for r in range(radius + 1):
            for i in range(ci - r, ci + r + 1):
                for j in range(cj - r, cj + r + 1):
                    if max(abs(i - ci), abs(j - cj)) != r:
                        continue
                    ks = [k for k in range(len(self.iv.get((i, j), ())))
                          if (i, j, k) in self.nodes]
                    if z is not None:
                        ks = [k for k in ks
                              if self.iv[(i, j)][k][1] <= z + self.step + 1e-3]
                        cand = max(ks, key=lambda k: self.iv[(i, j)][k][1]) if ks else None
                    else:
                        cand = min(ks, key=lambda k: self.iv[(i, j)][k][1]) if ks else None
                    if cand is not None:
                        d = (i - ci) ** 2 + (j - cj) ** 2
                        if best is None or d < best[0]:
                            best = (d, (i, j, cand))
            if best is not None:
                return best[1]
        return None

    def flood(self, start):
        seen = {start}
        stack = [start]
        while stack:
            n = stack.pop()
            for m, _ in self.edges(n):
                if m not in seen:
                    seen.add(m)
                    stack.append(m)
        return seen

    def distances(self, start):
        dist = {start: 0.0}
        pq = [(0.0, start)]
        while pq:
            d, n = heapq.heappop(pq)
            if d > dist.get(n, math.inf):
                continue
            for m, w in self.edges(n):
                nd = d + w
                if nd < dist.get(m, math.inf):
                    dist[m] = nd
                    heapq.heappush(pq, (nd, m))
        return dist

    def components(self, nodes):
        """Group a set of nodes into connected pockets (largest first)."""
        left = set(nodes)
        out = []
        while left:
            seed = left.pop()
            comp, stack = [seed], [seed]
            while stack:
                n = stack.pop()
                for m, _ in self.edges(n):
                    if m in left:
                        left.discard(m)
                        comp.append(m)
                        stack.append(m)
            out.append(comp)
        out.sort(key=len, reverse=True)
        return out

    def reached_in_box(self, reached, mins, maxs, zpad=STEP):
        """Any reached node whose column lies inside the XY box, standing
        between mins.z - zpad and maxs.z."""
        i0, j0 = self.col_of(mins[0], mins[1])
        i1, j1 = self.col_of(maxs[0] - 1e-3, maxs[1] - 1e-3)
        for i in range(i0, i1 + 1):
            for j in range(j0, j1 + 1):
                for k in range(len(self.iv.get((i, j), ()))):
                    if (i, j, k) in reached and \
                            mins[2] - zpad <= self.iv[(i, j)][k][1] <= maxs[2]:
                        return True
        return False


def site_points(data, meta=None):
    """Named points to route between: meta points first, then whatever can be
    inferred from entities (centroid of each team's spawns, bomb target
    centres)."""
    pts = dict((meta or {}).get("points", {}))
    for key, cls in (("t_spawn", "info_player_terrorist"),
                     ("ct_spawn", "info_player_counterterrorist")):
        sp = data.of_class(cls)
        if key not in pts and sp:
            # centroid of the team's spawns: the first one can sit far off-centre
            pts[key] = [sum(e.origin[i] for e in sp) / len(sp) for i in range(3)]
    for e in data.of_class("func_bomb_target"):
        if not e.bounds:
            continue
        letter = "ab"[int(e.props.get("bomb_site_designation", "0") or 0) % 2]
        mn, mx = e.bounds
        pts.setdefault("site_" + letter, [(mn[0] + mx[0]) / 2.0,
                                          (mn[1] + mx[1]) / 2.0, mn[2]])
    return pts
