"""Offline: render Minecraft-style 3D inventory icons from a jar's block models.
Reads models/textures the way the client does (elements + per-face UVs + the
gui display pose + fixed face shading + tint), rasterizes each block item to a
PNG, and writes them under an output dir. Run once; the dashboard just serves
the PNGs (no runtime image deps).

Usage: py build_icons.py <client.jar> <out_dir> [name1 name2 ...]
       (extra names = render just those, for quick visual checks)
"""
import json
import os
import sys
import zipfile
from pathlib import Path

YAW = float(os.environ.get("MC_YAW_OFFSET", "0"))
ZOOM = float(os.environ.get("MC_ZOOM", "1.0"))   # <1 leaves more padding around the block

import numpy as np
from PIL import Image

JAR = zipfile.ZipFile(sys.argv[1])
OUT = Path(sys.argv[2])
ONLY = sys.argv[3:]
NAMES = set(JAR.namelist())
SIZE = 48                      # output icon size in px
DEG = np.pi / 180.0

# fixed directional face shading used by gui_light:"side" (from MC LightUtil)
SHADE = {"up": 1.0, "down": 0.5, "north": 0.8, "south": 0.8, "east": 0.6, "west": 0.6}

_json_cache, _tex_cache = {}, {}


def load_model(path):
    path = path.split(":")[-1]
    if path not in _json_cache:
        e = f"assets/minecraft/models/{path}.json"
        _json_cache[path] = json.loads(JAR.read(e)) if e in NAMES else {}
    return _json_cache[path]


def item_model(name):
    try:
        d = json.loads(JAR.read(f"assets/minecraft/items/{name}.json"))
    except KeyError:
        return None
    out = []

    def walk(o):
        if isinstance(o, dict):
            if isinstance(o.get("model"), str):
                out.append(o["model"])
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(d)
    return out[0].split(":")[-1] if out else None


def resolve(model_path):
    """Merge the parent chain: textures (child wins), first elements, first
    gui display, and the root parent name (to tell blocks from flat items)."""
    tx, elements, gui, cur, seen, root = {}, None, None, model_path, set(), model_path
    while cur and cur not in seen:
        seen.add(cur)
        m = load_model(cur)
        for k, v in (m.get("textures") or {}).items():
            tx.setdefault(k, v)
        if elements is None and m.get("elements"):
            elements = m["elements"]
        if gui is None:
            gui = ((m.get("display") or {}).get("gui"))
        root = cur.split("/")[-1]
        cur = m.get("parent")
    return tx, elements, gui, root


def tex_ref(tx, ref):
    for _ in range(8):
        if isinstance(ref, str) and ref.startswith("#"):
            ref = tx.get(ref[1:])
        else:
            break
    return ref.split(":")[-1] if isinstance(ref, str) else None


def load_texture(name):
    if name not in _tex_cache:
        e = f"assets/minecraft/textures/{name}.png"
        if e not in NAMES:
            _tex_cache[name] = None
        else:
            im = Image.open(zipfile.ZipFile(sys.argv[1]).open(e)).convert("RGBA")
            if im.height > im.width:            # animated strip -> first frame
                im = im.crop((0, 0, im.width, im.width))
            _tex_cache[name] = im
    return _tex_cache[name]


def colormap_default(cm):
    im = load_texture(cm)                       # colormap/grass or foliage
    if im is None:
        return (255, 255, 255)
    t, d = 0.5, 0.5                             # temperate default (plains-ish)
    a = d * t
    x = int((1 - t) * 255)
    y = int((1 - a) * 255)
    return im.getpixel((min(x, 255), min(y, 255)))[:3]


# ---- geometry -------------------------------------------------------------
def rot_matrix(rx, ry, rz):
    cx, sx = np.cos(rx), np.sin(rx)
    cy, sy = np.cos(ry), np.sin(ry)
    cz, sz = np.cos(rz), np.sin(rz)
    Rx = np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]])
    Ry = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
    Rz = np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]])
    return Rx @ Ry @ Rz


def elem_rotation(el):
    r = el.get("rotation")
    if not r:
        return lambda p: p
    origin = np.array(r.get("origin", [8, 8, 8]), float)
    ang = r.get("angle", 0) * DEG
    axis = {"x": (ang, 0, 0), "y": (0, ang, 0), "z": (0, 0, ang)}[r["axis"]]
    M = rot_matrix(*axis)
    return lambda p: M @ (p - origin) + origin


FACES = {
    "down":  [(0, 0, 1), (1, 0, 1), (1, 0, 0), (0, 0, 0)],
    "up":    [(0, 1, 0), (1, 1, 0), (1, 1, 1), (0, 1, 1)],
    "north": [(1, 1, 0), (0, 1, 0), (0, 0, 0), (1, 0, 0)],
    "south": [(0, 1, 1), (1, 1, 1), (1, 0, 1), (0, 0, 1)],
    "west":  [(0, 1, 0), (0, 1, 1), (0, 0, 1), (0, 0, 0)],
    "east":  [(1, 1, 1), (1, 1, 0), (1, 0, 0), (1, 0, 1)],
}
# default uv (in 0..16) derived from element box per MC when "uv" omitted
UV_AXIS = {  # which model axes map to (u, v) for each face
    "down": ("x", "z"), "up": ("x", "z"),
    "north": ("x", "y"), "south": ("x", "y"),
    "west": ("z", "y"), "east": ("z", "y"),
}


def affine_coeffs(dst, src_w, src_h):
    """dst = [TL,TR,BL] output points; returns PIL AFFINE coeffs mapping
    output(x,y)->source(u,v) with source rect [0,src_w]x[0,src_h]."""
    (x0, y0), (x1, y1), (x2, y2) = dst
    A = np.array([[x0, y0, 1, 0, 0, 0],
                  [0, 0, 0, x0, y0, 1],
                  [x1, y1, 1, 0, 0, 0],
                  [0, 0, 0, x1, y1, 1],
                  [x2, y2, 1, 0, 0, 0],
                  [0, 0, 0, x2, y2, 1]], float)
    b = np.array([0, 0, src_w, 0, 0, src_h], float)
    return np.linalg.solve(A, b)


def render(name):
    model = item_model(name)
    if not model or not model.startswith("block/"):
        return None
    tx, elements, gui, root = resolve(model)
    if not elements:
        return None
    rx, ry, rz = (gui or {}).get("rotation", [30, 225, 0])
    R = rot_matrix(rx * DEG, (ry + YAW) * DEG, rz * DEG)  # our yaw is offset from MC's camera
    scale = (gui or {}).get("scale", [0.625, 0.625, 0.625])[0]

    def project(p):
        v = R @ (np.array(p, float) - 8.0)      # center then rotate
        k = (SIZE * scale / 16.0) * 1.28 * ZOOM
        return SIZE / 2 + v[0] * k, SIZE / 2 - v[1] * k, v[2]

    canvas = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    quads = []
    for el in elements:
        f = np.array(el["from"], float)
        t = np.array(el["to"], float)
        erot = elem_rotation(el)
        for fd, faced in (el.get("faces") or {}).items():
            texname = tex_ref(tx, faced.get("texture"))
            im = load_texture(f"block/{texname}") if texname and "/" not in texname else load_texture(texname)
            if im is None:
                continue
            # spatial corners
            pts3 = []
            for (ux, uy, uz) in FACES[fd]:
                p = np.array([f[0] if ux == 0 else t[0],
                              f[1] if uy == 0 else t[1],
                              f[2] if uz == 0 else t[2]], float)
                pts3.append(erot(p))
            proj = [project(p) for p in pts3]
            # backface cull by 2D winding (screen y is down)
            (ax, ay, _), (bx, by, _), (cx, cy, _), _ = proj
            area = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)
            if area <= 0:
                continue                          # cull faces pointing away from camera
            # uv rect (0..16); default from box extent
            uv = faced.get("uv")
            if uv is None:
                ua, va = UV_AXIS[fd]
                amap = {"x": (f[0], t[0]), "y": (f[1], t[1]), "z": (f[2], t[2])}
                uv = [amap[ua][0], amap[va][0], amap[ua][1], amap[va][1]]
            u0, v0, u1, v1 = uv
            W, H = im.size
            crop = im.crop((round(min(u0, u1) / 16 * W), round(min(v0, v1) / 16 * H),
                            round(max(u0, u1) / 16 * W), round(max(v0, v1) / 16 * H)))
            if crop.width < 1 or crop.height < 1:
                continue
            rot = faced.get("rotation", 0)
            if rot:
                crop = crop.rotate(-rot, expand=True)
            # shade + tint on the source
            arr = np.asarray(crop, float)
            sh = SHADE.get(fd, 1.0)
            arr[..., :3] *= sh
            if "tintindex" in faced:
                cm = "colormap/foliage" if (texname and "leaves" in texname) else "colormap/grass"
                tint = colormap_default(cm)
                arr[..., :3] *= np.array(tint) / 255.0
            crop = Image.fromarray(np.clip(arr, 0, 255).astype("uint8"), "RGBA")
            # map crop (TL,TR,BL) -> screen quad corners
            dst = [(proj[0][0], proj[0][1]), (proj[1][0], proj[1][1]), (proj[3][0], proj[3][1])]
            coeffs = affine_coeffs(dst, crop.width, crop.height)
            layer = crop.transform((SIZE, SIZE), Image.AFFINE, data=coeffs,
                                   resample=Image.NEAREST, fillcolor=(0, 0, 0, 0))
            depth = sum(p[2] for p in proj) / 4.0
            quads.append((depth, layer))
    for _, layer in sorted(quads, key=lambda q: q[0]):   # painter's: far -> near
        canvas.alpha_composite(layer)
    return canvas


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    items = ONLY or sorted(n.split("/")[-1][:-5] for n in NAMES
                           if n.startswith("assets/minecraft/items/") and n.endswith(".json"))
    made = 0
    for name in items:
        try:
            im = render(name)
        except Exception as e:
            if ONLY:
                print("ERR", name, e)
            continue
        if im and im.getbbox():
            im.save(OUT / f"{name}.png")
            made += 1
            if ONLY:
                print("rendered", name, im.getbbox())
    print(f"rendered {made} icons -> {OUT}")


main()
