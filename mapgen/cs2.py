"""Locate CS2 and drive Valve's command-line tools (Windows only).

Needs the "Counter-Strike 2 Workshop Tools" DLC, which provides
game/bin/win64/dmxconvert.exe and resourcecompiler.exe.
"""
import datetime
import os
import re
import shutil
import subprocess
import tempfile

CS2_DIR = os.path.join("steamapps", "common", "Counter-Strike Global Offensive")
BIN = os.path.join("game", "bin", "win64")

# Lines stock CS2 prints on every compile, unrelated to the map being built.
BENIGN = {
    "detail_prop_types.vdata_c": "stock CS2 resource missing in addon mode",
    "csgo_mega": "stock sound-script warning",
    "Couldn't load public key": "VPK signing is Valve-internal",
    "Not signing; keys not set": "VPK signing is Valve-internal",
    "Leaking 20 elements": "compiler's own shutdown report",
    "Leaked KeyValues": "compiler's own shutdown report",
    "elements left in memory": "compiler's own shutdown report",
}


def _steam_roots():
    roots = []
    try:
        import winreg
        for hive, key in ((winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"),
                          (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam")):
            try:
                with winreg.OpenKey(hive, key) as k:
                    for name in ("SteamPath", "InstallPath"):
                        try:
                            roots.append(winreg.QueryValueEx(k, name)[0])
                        except OSError:
                            pass
            except OSError:
                pass
    except ImportError:
        pass
    roots.append(r"C:\Program Files (x86)\Steam")
    libs = []
    for r in roots:
        libs.append(r)
        vdf = os.path.join(r, "steamapps", "libraryfolders.vdf")
        if os.path.exists(vdf):
            with open(vdf, encoding="utf-8", errors="replace") as f:
                libs += [p.replace("\\\\", "\\")
                         for p in re.findall(r'"path"\s+"([^"]+)"', f.read())]
    seen, out = set(), []
    for p in libs:
        p = os.path.normpath(p)
        if p.lower() not in seen:
            seen.add(p.lower())
            out.append(p)
    return out


def find_cs2():
    """CS2 install folder. Honours the CS2_PATH environment variable."""
    env = os.environ.get("CS2_PATH")
    candidates = [env] if env else []
    candidates += [os.path.join(lib, CS2_DIR) for lib in _steam_roots()]
    for c in candidates:
        if c and os.path.exists(os.path.join(c, "game", "csgo")):
            return c
    raise SystemExit("Could not find CS2. Set the CS2_PATH environment variable "
                     "to the 'Counter-Strike Global Offensive' folder.")


def tool(cs2, name):
    p = os.path.join(cs2, BIN, name)
    if not os.path.exists(p):
        raise SystemExit("%s not found. Install the 'Counter-Strike 2 Workshop "
                         "Tools' DLC from the CS2 page in Steam." % p)
    return p


def addon_dirs(cs2, addon):
    content = os.path.join(cs2, "content", "csgo_addons", addon)
    game = os.path.join(cs2, "game", "csgo_addons", addon)
    if not os.path.isdir(content):
        raise SystemExit(
            "Addon '%s' does not exist (%s).\nCreate it first: launch "
            "'Counter-Strike 2 Workshop Tools' from Steam and use "
            "'Create New Addon'." % (addon, content))
    return content, game


def list_addons(cs2):
    root = os.path.join(cs2, "content", "csgo_addons")
    if not os.path.isdir(root):
        return []
    return sorted(d for d in os.listdir(root)
                  if os.path.isdir(os.path.join(root, d))
                  and d not in ("addon_template", "workshop_items"))


def is_binary_vmap(path):
    with open(path, "rb") as f:
        return b"encoding binary" in f.read(64)


def convert(cs2, src, dst, encoding):
    """dmxconvert src -> dst with encoding 'binary' or 'keyvalues2'."""
    r = subprocess.run([tool(cs2, "dmxconvert.exe"), "-i", os.path.abspath(src),
                        "-o", os.path.abspath(dst), "-oe", encoding],
                       capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(dst):
        raise SystemExit("dmxconvert failed:\n" + r.stdout + r.stderr)


def to_text(cs2, vmap):
    """Path to a KV2 text version of `vmap` (converting via a temp file)."""
    if not is_binary_vmap(vmap):
        return vmap
    out = os.path.join(tempfile.mkdtemp(prefix="mapgen_"),
                       os.path.basename(vmap))
    convert(cs2, vmap, out, "keyvalues2")
    return out


def install(text_vmap, addon, map_name, force=False):
    """Convert a generated KV2 map to binary inside the addon's content
    folder. Never overwrites silently: needs force=True, and backs up first."""
    cs2 = find_cs2()
    content, _ = addon_dirs(cs2, addon)
    maps = os.path.join(content, "maps")
    os.makedirs(maps, exist_ok=True)
    dst = os.path.join(maps, map_name + ".vmap")
    if os.path.exists(dst):
        if not force:
            raise SystemExit("%s already exists. Pass --force to replace it "
                             "(a timestamped backup is kept)." % dst)
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        bak = dst + ".bak-" + stamp
        shutil.copy2(dst, bak)
        print("backed up existing map to", bak)
    convert(cs2, text_vmap, dst, "binary")
    print("installed", dst)
    return dst


def compile_map(addon, map_name, log_path=None):
    """Full compile (geometry, lighting, nav, audio, vpk). Returns a summary
    dict and writes the full log to log_path."""
    cs2 = find_cs2()
    content, game = addon_dirs(cs2, addon)
    src = os.path.join(content, "maps", map_name + ".vmap")
    if not os.path.exists(src):
        raise SystemExit("%s not found - run `install` first." % src)
    log_path = log_path or os.path.abspath(os.path.join("out", map_name + ".compile.log"))
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    print("compiling %s (this takes minutes; full log: %s)" % (src, log_path))
    with open(log_path, "w", encoding="utf-8", errors="replace") as log:
        p = subprocess.run([tool(cs2, "resourcecompiler.exe"), "-game",
                            os.path.join(cs2, "game", "csgo"), "-i", src,
                            "-nop4", "-f"],
                           stdout=log, stderr=subprocess.STDOUT)
    with open(log_path, encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines()
    return summarize(lines, p.returncode,
                     os.path.join(game, "maps", map_name + ".vpk"))


def summarize(lines, returncode, vpk):
    s = {"returncode": returncode, "ok": None, "nav_areas": None,
         "shadow": None, "problems": [], "benign": [], "vpk": vpk}
    for ln in lines:
        t = ln.strip()
        m = re.search(r"OK:\s*(\d+) compiled,\s*(\d+) failed", t)
        if m:
            s["ok"] = (int(m.group(1)), int(m.group(2)))
        m = re.search(r"loaded with (\d+) nav areas", t)
        if m:
            s["nav_areas"] = int(m.group(1))
        m = re.search(r"Assigned (\d+) Light Shadow Slots, (\d+) failed", t)
        if m:
            s["shadow"] = (int(m.group(1)), int(m.group(2)))
        if not re.search(r"warning|error|failed|skipped", t, re.I):
            continue
        if "OK:" in t or "Shadow Slots" in t:
            continue
        why = next((v for k, v in BENIGN.items() if k in t), None)
        (s["benign"] if why else s["problems"]).append(
            t + ("   [benign: %s]" % why if why else ""))
    s["vpk_exists"] = os.path.exists(vpk)
    return s
