"""Minimal reader/writer for Valve DMX keyvalues2 text encoding.

CS2 saves .vmap files as *binary* DMX. Valve's dmxconvert.exe (shipped with
the CS2 Workshop Tools) converts losslessly between binary and this text form,
which is what makes generating maps from Python possible:

    dmxconvert -i map.vmap -o map_text.vmap -oe keyvalues2   # binary -> text
    dmxconvert -i map_text.vmap -o map.vmap -oe binary       # text -> binary

Elements can appear inline, or at the top level and be referenced by id
("element" "<uuid>"); Hammer-saved files use both. Element-array items that
are references are kept as plain id strings - resolve them via DmxFile.by_id.
"""
import re
import uuid


class Element:
    """A DMX element: a typed bag of ordered attributes."""

    def __init__(self, etype, eid=None):
        self.etype = etype
        self.id = eid or str(uuid.uuid4())
        self.attrs = []  # list of (name, type, value)

    def get(self, name, default=None):
        for n, t, v in self.attrs:
            if n == name:
                return v
        return default

    def gettype(self, name):
        for n, t, v in self.attrs:
            if n == name:
                return t
        return None

    def set(self, name, atype, value):
        for i, (n, t, v) in enumerate(self.attrs):
            if n == name:
                self.attrs[i] = (name, atype, value)
                return
        self.attrs.append((name, atype, value))

    def keys(self):
        return [n for n, t, v in self.attrs]

    def __repr__(self):
        return "<%s %s>" % (self.etype, self.id[:8])


ARRAY_TYPES = {
    "element_array", "int_array", "float_array", "bool_array", "string_array",
    "vector2_array", "vector3_array", "vector4_array", "color_array",
    "qangle_array", "quaternion_array", "matrix_array", "uint64_array",
    "time_array", "binary_array", "uint8_array",
}

SCALAR_TYPES = {
    "elementid", "int", "float", "bool", "string", "vector2", "vector3",
    "vector4", "color", "qangle", "quaternion", "matrix", "uint64", "time",
    "binary", "element", "uint8",
}


class Tokenizer:
    def __init__(self, text):
        self.text = text
        self.pos = 0
        self.n = len(text)

    def _skip_ws(self):
        t, n = self.text, self.n
        while self.pos < n:
            c = t[self.pos]
            if c in " \t\r\n":
                self.pos += 1
            elif c == "/" and self.pos + 1 < n and t[self.pos + 1] == "/":
                while self.pos < n and t[self.pos] != "\n":
                    self.pos += 1
            else:
                break

    def peek(self):
        self._skip_ws()
        if self.pos >= self.n:
            return None
        return self.text[self.pos]

    def next_token(self):
        """Returns (kind, value) where kind is 'str', 'punct', or None at EOF."""
        self._skip_ws()
        if self.pos >= self.n:
            return (None, None)
        c = self.text[self.pos]
        if c in "{}[],":
            self.pos += 1
            return ("punct", c)
        if c == '"':
            return ("str", self._read_string())
        raise ValueError("Unexpected char %r at %d: %r"
                         % (c, self.pos, self.text[self.pos:self.pos + 60]))

    def _read_string(self):
        # assumes text[pos] == '"'
        self.pos += 1
        out = []
        t, n = self.text, self.n
        while self.pos < n:
            c = t[self.pos]
            if c == "\\":
                nxt = t[self.pos + 1]
                out.append({"n": "\n", "t": "\t", '"': '"', "\\": "\\"}.get(nxt, nxt))
                self.pos += 2
            elif c == '"':
                self.pos += 1
                return "".join(out)
            else:
                out.append(c)
                self.pos += 1
        raise ValueError("Unterminated string")


class DmxFile:
    def __init__(self):
        self.header = ("keyvalues2", 4, "vmap", 40)
        self.elements = []      # top-level elements in file order
        self.by_id = {}

    # ---------- parsing ----------

    @classmethod
    def load(cls, path):
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
        self = cls()
        m = re.match(r"\s*<!--\s*dmx encoding (\S+) (\d+) format (\S+) (\d+)\s*-->", text)
        if not m:
            raise ValueError("Not a keyvalues2 dmx text file")
        self.header = (m.group(1), int(m.group(2)), m.group(3), int(m.group(4)))
        tk = Tokenizer(text[m.end():])
        while True:
            kind, val = tk.next_token()
            if kind is None:
                break
            if kind != "str":
                raise ValueError("Expected element type, got %r" % (val,))
            el = self._parse_element(tk, val)
            self.elements.append(el)
        return self

    def _parse_element(self, tk, etype):
        kind, val = tk.next_token()
        if val != "{":
            raise ValueError("Expected { after element type %s" % etype)
        el = Element(etype)
        while True:
            kind, val = tk.next_token()
            if kind == "punct" and val == "}":
                break
            if kind != "str":
                raise ValueError("Expected attr name, got %r" % (val,))
            name = val
            kind2, atype = tk.next_token()
            if kind2 != "str":
                raise ValueError("Expected attr type for %s" % name)
            if atype == "elementid":
                _, v = tk.next_token()
                el.id = v
                continue
            value = self._parse_value(tk, atype)
            el.attrs.append((name, atype, value))
        self.by_id[el.id] = el
        return el

    def _parse_value(self, tk, atype):
        if atype in ARRAY_TYPES:
            kind, val = tk.next_token()
            if val != "[":
                raise ValueError("Expected [ for %s" % atype)
            items = []
            while True:
                c = tk.peek()
                if c == "]":
                    tk.next_token()
                    break
                kind, val = tk.next_token()
                if kind == "punct" and val == ",":
                    continue
                if kind == "punct" and val == "]":
                    break
                if atype == "element_array":
                    # either  "element" "<uuid>"   or  "CType" { ... }
                    if val == "element":
                        _, uid = tk.next_token()
                        items.append(uid)  # reference by id string
                    else:
                        items.append(self._parse_element(tk, val))
                else:
                    items.append(val)
            return items
        # scalars
        if atype == "element":
            _, v = tk.next_token()
            return v
        if atype not in SCALAR_TYPES:
            # An unknown type name means an inline sub-element, e.g.
            #   "defaultcamera" "CStoredCamera" { ... }
            return self._parse_element(tk, atype)
        kind, val = tk.next_token()
        if kind == "punct" and val == "{":
            raise ValueError("unexpected inline element for scalar type %s" % atype)
        return val

    # ---------- writing ----------

    def dumps(self):
        out = ["<!-- dmx encoding %s %d format %s %d -->\n" % self.header]
        for el in self.elements:
            self._write_element(out, el, 0, top=True)
        return "".join(out)

    def save(self, path):
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(self.dumps())

    def _write_element(self, out, el, depth, top=False):
        ind = "\t" * depth
        out.append('%s"%s"\n%s{\n' % (ind, el.etype, ind))
        ind2 = "\t" * (depth + 1)
        out.append('%s"id" "elementid" "%s"\n' % (ind2, el.id))
        for name, atype, value in el.attrs:
            self._write_attr(out, name, atype, value, depth + 1)
        out.append("%s}\n" % ind)
        if top:
            out.append("\n")

    def _write_attr(self, out, name, atype, value, depth):
        ind = "\t" * depth
        if atype in ARRAY_TYPES:
            out.append('%s"%s" "%s" \n%s[\n' % (ind, name, atype, ind))
            ind2 = "\t" * (depth + 1)
            n = len(value)
            for i, item in enumerate(value):
                comma = "," if i < n - 1 else ""
                if atype == "element_array":
                    if isinstance(item, Element):
                        self._write_element(out, item, depth + 1)
                        # replace trailing newline with comma form
                        if comma:
                            out[-1] = out[-1].rstrip("\n") + ",\n"
                    else:
                        out.append('%s"element" "%s"%s\n' % (ind2, item, comma))
                else:
                    out.append('%s"%s"%s\n' % (ind2, _esc(item), comma))
            out.append("%s]\n" % ind)
        elif atype == "element":
            out.append('%s"%s" "element" "%s"\n' % (ind, name, value or ""))
        elif isinstance(value, Element):
            out.append('%s"%s" ' % (ind, name))
            # inline named element: type line then body
            out.append('"%s"\n%s{\n' % (value.etype, ind))
            ind2 = "\t" * (depth + 1)
            out.append('%s"id" "elementid" "%s"\n' % (ind2, value.id))
            for n2, t2, v2 in value.attrs:
                self._write_attr(out, n2, t2, v2, depth + 1)
            out.append("%s}\n\n" % ind)
        else:
            out.append('%s"%s" "%s" "%s"\n' % (ind, name, atype, _esc(value)))


def _esc(s):
    s = str(s)
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\t", "\\t")


def new_id():
    return str(uuid.uuid4())
