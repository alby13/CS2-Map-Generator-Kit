"""Emit CS2 .vmap content (Hammer's DMX schema) from Python.

Geometry is written as CDmePolygonMesh half-edge meshes, the same structure
Hammer saves. The conventions below were reverse-engineered from a map saved
by Hammer (vmap format 40, editorbuild 9806) and confirmed by compiling and
playing generated maps:

  * half-edge twins are (2k, 2k+1)
  * edgeVertexIndices[h] is h's DESTINATION vertex
  * a face loop is walked via edgeNextIndices, collecting destinations
  * faces wind CCW seen from outside; normal = cross(p1-p0, p2-p1)
  * vertexEdgeIndices[v] is an OUTGOING half-edge of v
  * world position = mesh origin + local vertex (the origin is a pivot)
  * u = (dot(P_world, axisU) / scaleU + axisU.w) / texture_width
"""
import random

from .dmx import DmxFile, Element

TEX_SIZE = 512.0   # dev textures are 512 wide; Hammer regenerates UVs anyway


# ---------------------------------------------------------------- vectors

def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _norm(v):
    m = (v[0] ** 2 + v[1] ** 2 + v[2] ** 2) ** 0.5
    if m < 1e-9:
        return (0.0, 0.0, 1.0)
    return (v[0] / m, v[1] / m, v[2] / m)


def newell_normal(pts):
    """Robust polygon normal (works even if consecutive points are collinear)."""
    nx = ny = nz = 0.0
    n = len(pts)
    for i in range(n):
        a, b = pts[i], pts[(i + 1) % n]
        nx += (a[1] - b[1]) * (a[2] + b[2])
        ny += (a[2] - b[2]) * (a[0] + b[0])
        nz += (a[0] - b[0]) * (a[1] + b[1])
    return _norm((nx, ny, nz))


def _centroid(pts):
    n = float(len(pts))
    return (sum(p[0] for p in pts) / n, sum(p[1] for p in pts) / n,
            sum(p[2] for p in pts) / n)


def _fmt(*vals):
    return " ".join(("%g" % v) for v in vals)


def _ref_id():
    return "0x%016x" % random.getrandbits(64)


def tex_axes(n):
    """Source-style world-aligned texture axes for a face normal."""
    ax, ay, az = abs(n[0]), abs(n[1]), abs(n[2])
    if az >= ax and az >= ay:
        return (1.0, 0.0, 0.0), (0.0, -1.0, 0.0)
    if ax >= ay:
        return (0.0, 1.0, 0.0), (0.0, 0.0, -1.0)
    return (1.0, 0.0, 0.0), (0.0, 0.0, -1.0)


def _stream(name, attr, dtype, data, flags, vbloc=0):
    e = Element("CDmePolygonMeshDataStream")
    e.set("name", "string", name)
    e.set("standardAttributeName", "string", attr)
    e.set("semanticName", "string", name.split(":")[0])
    e.set("semanticIndex", "int", "0")
    e.set("vertexBufferLocation", "int", str(vbloc))
    e.set("dataStateFlags", "int", str(flags))
    e.set("subdivisionBinding", "element", "")
    e.set("data", dtype, data)
    return e


def _data_array(size, streams):
    e = Element("CDmePolygonMeshDataArray")
    e.set("size", "int", str(size))
    e.set("streams", "element_array", streams)
    return e


# ---------------------------------------------------------------- mesh

class Mesh:
    """One solid under construction: shared vertices + polygon faces.

    Keep ONE convex solid per Mesh. Putting several touching solids in one
    mesh welds their coincident vertices into a non-manifold mesh.
    """

    def __init__(self):
        self.verts = []
        self.faces = []          # (vertex index list, material, texture scale)
        self._vmap = {}

    def vert(self, p):
        key = (round(p[0], 3), round(p[1], 3), round(p[2], 3))
        if key not in self._vmap:
            self._vmap[key] = len(self.verts)
            self.verts.append((float(p[0]), float(p[1]), float(p[2])))
        return self._vmap[key]

    def face(self, points, material, scale=0.25):
        """Add a polygon. Points must already wind CCW seen from outside."""
        idx = [self.vert(p) for p in points]
        self.faces.append((idx, material, scale))

    def translated(self, off):
        m = Mesh()
        m.verts = [(p[0] + off[0], p[1] + off[1], p[2] + off[2])
                   for p in self.verts]
        m._vmap = {(round(p[0], 3), round(p[1], 3), round(p[2], 3)): i
                   for i, p in enumerate(m.verts)}
        m.faces = list(self.faces)
        return m

    # -- shapes ------------------------------------------------------------

    def add_convex(self, points, faces, material, scale=0.25, materials=None):
        """Any convex polyhedron: `points` are xyz, `faces` index lists in
        cyclic order. Each face is flipped if needed so it faces outward,
        so callers never have to think about winding."""
        c = _centroid(points)
        for fi, f in enumerate(faces):
            pts = [points[i] for i in f]
            if _dot(newell_normal(pts), _sub(_centroid(pts), c)) < 0:
                pts.reverse()
            self.face(pts, materials[fi] if materials else material, scale)

    def add_box(self, mins, maxs, material, scale=0.25, materials=None):
        """Axis-aligned box. `materials` optionally maps a side
        ('bottom','top','south','north','west','east') to a material."""
        x0, y0, z0 = mins
        x1, y1, z1 = maxs
        v = [(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
             (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)]
        sides = [("bottom", (0, 3, 2, 1)), ("top", (4, 5, 6, 7)),
                 ("south", (0, 1, 5, 4)), ("east", (1, 2, 6, 5)),
                 ("north", (2, 3, 7, 6)), ("west", (3, 0, 4, 7))]
        mats = [(materials or {}).get(k, material) for k, _ in sides]
        self.add_convex(v, [list(q) for _, q in sides], material, scale, mats)

    def add_prism(self, polygon, z0, z1, material, scale=0.25):
        """Vertical extrusion of a convex XY polygon (pillars, angled walls)."""
        n = len(polygon)
        if n < 3:
            raise ValueError("prism needs at least 3 points")
        sign = 0
        for i in range(n):
            a, b, c = polygon[i], polygon[(i + 1) % n], polygon[(i + 2) % n]
            cr = (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0])
            if abs(cr) > 1e-6:
                s = 1 if cr > 0 else -1
                if sign and s != sign:
                    raise ValueError("prism polygon must be convex; split it")
                sign = s
        pts = [(x, y, z0) for x, y in polygon] + [(x, y, z1) for x, y in polygon]
        faces = [list(range(n)), list(range(n, 2 * n))]
        faces += [[i, (i + 1) % n, n + (i + 1) % n, n + i] for i in range(n)]
        self.add_convex(pts, faces, material, scale)

    def add_ramp(self, mins, maxs, rise, material, scale=0.25):
        """Wedge filling the box, sloping up toward `rise` ('+x','-x','+y','-y').
        Source treats slopes up to ~45 degrees as walkable."""
        x0, y0, z0 = mins
        x1, y1, z1 = maxs
        base = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
        axis = 0 if rise[1] == "x" else 1
        hi_val = (x1 if axis == 0 else y1) if rise[0] == "+" else \
                 (x0 if axis == 0 else y0)
        high = [i for i, p in enumerate(base) if p[axis] == hi_val]
        low = [i for i in range(4) if i not in high]
        across = 1 - axis
        pts = [(x, y, z0) for x, y in base]
        pts += [(base[high[0]][0], base[high[0]][1], z1),
                (base[high[1]][0], base[high[1]][1], z1)]
        lo0 = [i for i in low if base[i][across] == base[high[0]][across]][0]
        lo1 = [i for i in low if i != lo0][0]
        faces = [[0, 1, 2, 3],
                 [high[0], high[1], 5, 4],
                 [lo0, 4, 5, lo1],
                 [lo0, high[0], 4],
                 [lo1, high[1], 5]]
        self.add_convex(pts, faces, material, scale)

    # -- emit --------------------------------------------------------------

    def build(self):
        """Produce the CDmePolygonMesh element and the material list."""
        materials, mat_index = [], {}
        for _, mat, _ in self.faces:
            if mat not in mat_index:
                mat_index[mat] = len(materials)
                materials.append(mat)

        # undirected edge (lo,hi) -> k ; half-edge 2k goes lo->hi, 2k+1 hi->lo
        edge_key, he_of = {}, {}
        for idx, _, _ in self.faces:
            n = len(idx)
            for i in range(n):
                a, b = idx[i], idx[(i + 1) % n]
                lo, hi = (a, b) if a < b else (b, a)
                if (lo, hi) not in edge_key:
                    k = len(edge_key)
                    edge_key[(lo, hi)] = k
                    he_of[(lo, hi)] = 2 * k
                    he_of[(hi, lo)] = 2 * k + 1
        nhe = 2 * len(edge_key)

        edgeVertex = [0] * nhe
        edgeOpposite = [0] * nhe
        edgeNext = [-1] * nhe
        edgeFace = [-1] * nhe
        edgeData = [0] * nhe
        edgeVertexData = [0] * nhe
        for (lo, hi), k in edge_key.items():
            edgeVertex[2 * k], edgeVertex[2 * k + 1] = hi, lo
            edgeOpposite[2 * k], edgeOpposite[2 * k + 1] = 2 * k + 1, 2 * k
            edgeData[2 * k] = edgeData[2 * k + 1] = k

        vertexEdge = [-1] * len(self.verts)
        faceEdge, face_norm = [], []
        fv_count = 0
        for fi, (idx, mat, scale) in enumerate(self.faces):
            n = len(idx)
            hs = []
            for i in range(n):
                a, b = idx[i], idx[(i + 1) % n]
                h = he_of[(a, b)]
                if edgeFace[h] != -1:
                    raise ValueError(
                        "face %d reuses directed edge %d->%d: bad winding, a "
                        "duplicate face, or two solids in one mesh" % (fi, a, b))
                hs.append(h)
                if vertexEdge[a] == -1:
                    vertexEdge[a] = h
            for i in range(n):
                edgeNext[hs[i]] = hs[(i + 1) % n]
                edgeFace[hs[i]] = fi
                edgeVertexData[hs[i]] = fv_count
                fv_count += 1
            faceEdge.append(hs[0])
            face_norm.append(newell_normal([self.verts[i] for i in idx]))

        open_edges = [h for h in range(nhe) if edgeFace[h] == -1]
        if open_edges:
            raise ValueError("mesh is not closed (%d boundary half-edges); "
                             "every solid must be watertight" % len(open_edges))

        texcoord, normal, tangent, pvl = [], [], [], []
        fd_scale, fd_axu, fd_axv, fd_mat = [], [], [], []
        for fi, (idx, mat, scale) in enumerate(self.faces):
            nrm = face_norm[fi]
            U, V = tex_axes(nrm)
            fd_scale.append(_fmt(scale, scale))
            fd_axu.append(_fmt(U[0], U[1], U[2], 0.0))
            fd_axv.append(_fmt(V[0], V[1], V[2], 0.0))
            fd_mat.append(str(mat_index[mat]))
            t = _norm(_sub(U, tuple(c * _dot(U, nrm) for c in nrm)))
            h = start = faceEdge[fi]
            while True:
                p = self.verts[edgeVertex[h]]
                texcoord.append(_fmt(_dot(p, U) / scale / TEX_SIZE,
                                     _dot(p, V) / scale / TEX_SIZE))
                normal.append(_fmt(*nrm))
                tangent.append(_fmt(t[0], t[1], t[2], 1.0))
                pvl.append("1 1 1 0")
                h = edgeNext[h]
                if h == start:
                    break

        nf = len(self.faces)
        md = Element("CDmePolygonMesh")
        md.set("name", "string", "meshData")
        md.set("vertexEdgeIndices", "int_array", [str(x) for x in vertexEdge])
        md.set("vertexDataIndices", "int_array",
               [str(i) for i in range(len(self.verts))])
        md.set("edgeVertexIndices", "int_array", [str(x) for x in edgeVertex])
        md.set("edgeOppositeIndices", "int_array", [str(x) for x in edgeOpposite])
        md.set("edgeNextIndices", "int_array", [str(x) for x in edgeNext])
        md.set("edgeFaceIndices", "int_array", [str(x) for x in edgeFace])
        md.set("edgeDataIndices", "int_array", [str(x) for x in edgeData])
        md.set("edgeVertexDataIndices", "int_array",
               [str(x) for x in edgeVertexData])
        md.set("faceEdgeIndices", "int_array", [str(x) for x in faceEdge])
        md.set("faceDataIndices", "int_array", [str(i) for i in range(nf)])
        md.set("materials", "string_array", materials)
        md.set("vertexData", "CDmePolygonMeshDataArray", _data_array(
            len(self.verts),
            [_stream("position:0", "position", "vector3_array",
                     [_fmt(*p) for p in self.verts], 3)]))
        md.set("faceVertexData", "CDmePolygonMeshDataArray", _data_array(
            len(texcoord), [
                _stream("texcoord:0", "texcoord", "vector2_array", texcoord, 1),
                _stream("normal:0", "normal", "vector3_array", normal, 1),
                _stream("tangent:0", "tangent", "vector4_array", tangent, 1),
                _stream("PerVertexLighting:0", "", "vector4_array", pvl, 1, 1),
            ]))
        md.set("edgeData", "CDmePolygonMeshDataArray", _data_array(
            len(edge_key),
            [_stream("flags:0", "flags", "int_array", ["0"] * len(edge_key), 3)]))
        md.set("faceData", "CDmePolygonMeshDataArray", _data_array(nf, [
            _stream("textureScale:0", "textureScale", "vector2_array", fd_scale, 0),
            _stream("textureAxisU:0", "textureAxisU", "vector4_array", fd_axu, 0),
            _stream("textureAxisV:0", "textureAxisV", "vector4_array", fd_axv, 0),
            _stream("materialindex:0", "materialindex", "int_array", fd_mat, 8),
            _stream("flags:0", "flags", "int_array", ["0"] * nf, 3),
            _stream("lightmapScaleBias:0", "lightmapScaleBias", "int_array",
                    ["0"] * nf, 1),
        ]))
        sub = Element("CDmePolygonMeshSubdivisionData")
        sub.set("subdivisionLevels", "int_array", ["0"] * nhe)
        sub.set("streams", "element_array", [])
        md.set("subdivisionData", "CDmePolygonMeshSubdivisionData", sub)
        return md, materials


# ---------------------------------------------------------------- map

class VMap:
    """A map document: world meshes and entities, saved as KV2 text."""

    def __init__(self):
        self._node = 2
        self.children = []
        self.materials = set()

    def _base(self, el):
        self._node += 1
        el.set("nodeID", "int", str(self._node))
        el.set("referenceID", "uint64", _ref_id())
        el.set("children", "element_array", [])
        el.set("variableTargetKeys", "string_array", [])
        el.set("variableNames", "string_array", [])
        return el

    @staticmethod
    def _tail(el, origin=(0, 0, 0), angles=(0, 0, 0)):
        el.set("origin", "vector3", _fmt(*origin))
        el.set("angles", "qangle", _fmt(*angles))
        el.set("scales", "vector3", "1 1 1")
        el.set("transformLocked", "bool", "0")
        el.set("force_hidden", "bool", "0")
        el.set("editorOnly", "bool", "0")
        return el

    def add_mesh(self, mesh, add_to_world=True, origin=(0.0, 0.0, 0.0)):
        """Emit a CMapMesh. `origin` becomes the pivot and is subtracted from
        the absolute vertex positions (Hammer stores vertices local to it)."""
        origin = tuple(float(c) for c in origin)
        if origin != (0.0, 0.0, 0.0):
            mesh = mesh.translated((-origin[0], -origin[1], -origin[2]))
        md, mats = mesh.build()
        self.materials.update(mats)
        m = Element("CMapMesh")
        self._base(m)
        m.set("cubeMapName", "string", "")
        m.set("lightGroup", "string", "")
        for k in ("visexclude", "disablemerging", "renderwithdynamic",
                  "disableHeightDisplacement"):
            m.set(k, "bool", "0")
        m.set("fademindist", "float", "-1")
        m.set("fademaxdist", "float", "0")
        for k in ("bakelighting", "precomputelightprobes", "renderToCubemaps",
                  "emissiveLightingEnabled"):
            m.set(k, "bool", "1")
        m.set("emissiveLightingBoost", "float", "1")
        m.set("disableShadows", "int", "0")
        m.set("lightingDummy", "bool", "0")
        m.set("smoothingAngle", "float", "40")
        m.set("tintColor", "color", "255 255 255 255")
        m.set("renderAmt", "int", "255")
        m.set("physicsType", "string", "default")
        for k in ("physicsGroup", "physicsInteractsAs", "physicsInteractsWith",
                  "physicsInteractsExclude"):
            m.set(k, "string", "")
        m.set("meshData", "CDmePolygonMesh", md)
        m.set("physicsSimplificationOverride", "bool", "0")
        m.set("physicsSimplificationError", "float", "0")
        self._tail(m, origin)
        if add_to_world:
            self.children.append(m)
        return m

    def add_entity(self, classname, origin=(0, 0, 0), angles=(0, 0, 0),
                   props=None, meshes=None):
        """Point entity, or a brush entity if `meshes` is given. A brush
        entity's mesh pivot is the entity origin (they are the same point,
        not stacked transforms)."""
        e = Element("CMapEntity")
        self._base(e)
        plug = Element("DmePlugList")
        plug.set("names", "string_array", [])
        plug.set("dataTypes", "int_array", [])
        plug.set("plugTypes", "int_array", [])
        plug.set("descriptions", "string_array", [])
        e.set("relayPlugData", "DmePlugList", plug)
        e.set("connectionsData", "element_array", [])
        ep = Element("EditGameClassProps")
        ep.set("classname", "string", classname)
        for k, v in (props or {}).items():
            ep.set(k, "string", str(v))
        e.set("entity_properties", "EditGameClassProps", ep)
        e.set("hitNormal", "vector3", "0 0 1")
        e.set("isProceduralEntity", "bool", "0")
        self._tail(e, origin, angles)
        if meshes:
            e.set("children", "element_array",
                  [self.add_mesh(m, add_to_world=False, origin=origin)
                   for m in meshes])
        self.children.append(e)
        return e

    def add_brush_entity(self, classname, mins, maxs,
                         material="materials/tools/toolstrigger.vmat",
                         props=None):
        """Brush entity from one box (triggers, bomb targets, buy zones)."""
        mesh = Mesh()
        mesh.add_box(mins, maxs, material)
        origin = tuple((mins[i] + maxs[i]) / 2.0 for i in range(3))
        return self.add_entity(classname, origin, props=props, meshes=[mesh])

    def save(self, path, camera=((0, -640, 320), (0, 0, 128))):
        f = DmxFile()
        f.header = ("keyvalues2", 4, "vmap", 40)

        prefix = Element("$prefix_element$")
        prefix.set("map_asset_references", "string_array", sorted(self.materials))
        f.elements.append(prefix)

        root = Element("CMapRootElement")
        root.set("isprefab", "bool", "0")
        root.set("editorbuild", "int", "9806")
        root.set("editorversion", "int", "400")
        root.set("itemFile", "string", "")
        cam = Element("CStoredCamera")
        cam.set("position", "vector3", _fmt(*camera[0]))
        cam.set("lookat", "vector3", _fmt(*camera[1]))
        root.set("defaultcamera", "CStoredCamera", cam)
        cams = Element("CStoredCameras")
        cams.set("activecamera", "int", "-1")
        cams.set("cameras", "element_array", [])
        root.set("3dcameras", "CStoredCameras", cams)

        world = Element("CMapWorld")
        world.set("nodeID", "int", "1")
        world.set("referenceID", "uint64", "0x0")
        world.set("children", "element_array", self.children)
        world.set("variableTargetKeys", "string_array", [])
        world.set("variableNames", "string_array", [])
        plug = Element("DmePlugList")
        plug.set("names", "string_array", [])
        plug.set("dataTypes", "int_array", [])
        plug.set("plugTypes", "int_array", [])
        plug.set("descriptions", "string_array", [])
        world.set("relayPlugData", "DmePlugList", plug)
        world.set("connectionsData", "element_array", [])
        ep = Element("EditGameClassProps")
        ep.set("classname", "string", "worldspawn")
        world.set("entity_properties", "EditGameClassProps", ep)
        world.set("nextDecalID", "int", "0")
        world.set("fixupEntityNames", "bool", "1")
        world.set("mapUsageType", "string", "standard")
        self._tail(world)
        root.set("world", "CMapWorld", world)

        vis = Element("CVisibilityMgr")
        vis.set("nodes", "element_array", [])
        vis.set("hiddenFlags", "int_array", [])
        root.set("visbility", "CVisibilityMgr", vis)     # sic: Valve's spelling
        mv = Element("CMapVariableSet")
        for k in ("variableNames", "variableValues", "variableTypeNames",
                  "variableTypeParameters"):
            mv.set(k, "string_array", [])
        mv.set("m_ChoiceGroups", "element_array", [])
        root.set("mapVariables", "CMapVariableSet", mv)
        sel = Element("CMapSelectionSet")
        sel.set("children", "element_array", [])
        sel.set("selectionSetName", "string", "")
        sel.set("selectionSetData", "element", "")
        root.set("rootSelectionSet", "CMapSelectionSet", sel)
        root.set("m_ReferencedMeshSnapshots", "element_array", [])
        root.set("m_bIsCordoning", "bool", "0")
        root.set("m_bCordonsVisible", "bool", "0")
        root.set("nodeInstanceData", "element_array", [])
        f.elements.append(root)
        f.save(path)
        return f
