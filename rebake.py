#!/usr/bin/env python3
"""
rebake.py - Regenerate FlakwagonBodyBakeData and SideMaskTexture for a new mesh.

Usage:
  python rebake.py <mesh_dump.txt> <bakedata_dump.txt> <sidemask_dump.txt>

Modifies bakedata and sidemask dumps in-place.
"""

import re, struct, sys, math
from pathlib import Path
from collections import defaultdict

TEX_W = TEX_H = 512

# Side direction vectors (Unity: Y=up)
# CAR_AXIS: 'x' = car nose faces +X, sides face ±Z  (Flakwagon)
#           'z' = car nose faces +Z, sides face ±X  (GAZ-66 / most Unity vehicles)
SIDES    = ['top', 'front', 'back', 'right', 'left']
CAR_AXIS = 'z'   # set via --car-axis CLI flag before running

# RGB colors in SideMaskTexture per side
SIDE_COLOR = {
    'top':   (0,   255,   0),
    'right': (255,   0,   0),
    'left':  (0,     0, 255),
    'front': (0,     0,   0),  # not encoded in mask → black
    'back':  (0,     0,   0),  # not encoded in mask → black
}

# ── Format helpers (reused from uabea_mesh_converter) ──────────────────────────

FORMAT_BYTES = {0:4, 1:2, 2:1, 3:1, 4:2, 5:2, 6:1, 7:2, 8:4, 11:2}

def fmtsize(f): return FORMAT_BYTES.get(f, 4)

def _half_to_float(h):
    s = (h >> 15) & 1; e = (h >> 10) & 0x1f; m = h & 0x3ff
    if e == 0:   val = (m/1024.0)*(2**-14) if m else 0.0
    elif e == 31: val = float('inf') if not m else float('nan')
    else:         val = (1.0+m/1024.0)*(2**(e-15))
    return -val if s else val

def decode_val(data, off, fmt):
    if fmt == 0:        return struct.unpack_from('<f', data, off)[0]
    if fmt in (1, 11):  return _half_to_float(struct.unpack_from('<H', data, off)[0])
    if fmt == 2:        return data[off] / 255.0
    if fmt == 3:
        v = data[off]; return max(-1.0, (v-256 if v>127 else v)/127.0)
    if fmt == 4:        return struct.unpack_from('<H', data, off)[0] / 65535.0
    if fmt == 5:        return struct.unpack_from('<h', data, off)[0] / 32767.0
    return 0.0

def compute_stride(channels):
    max_end = 0
    for ch in channels:
        if ch['dimension'] == 0: continue
        end = ch['offset'] + ch['dimension'] * fmtsize(ch['format'])
        if end > max_end: max_end = end
    return (max_end + 3) & ~3

# ── Mesh parser ────────────────────────────────────────────────────────────────

def _read_n_bytes(lines, start, count):
    data = bytearray(); i = start
    while i < len(lines) and len(data) < count:
        m = re.search(r'UInt8 data = (\d+)', lines[i])
        if m: data.append(int(m.group(1)))
        i += 1
    return data

def parse_mesh(path):
    lines = Path(path).read_text(encoding='utf-8').splitlines()
    mesh = {'channels': [], 'vertex_count': 0,
            'index_buffer': bytearray(), 'vertex_data': bytearray(),
            'index_format': 0, 'submeshes': []}
    i = 0
    while i < len(lines):
        s = lines[i].strip()
        m = re.search(r'm_IndexFormat = (\d+)', s)
        if m: mesh['index_format'] = int(m.group(1))
        m = re.search(r'm_VertexCount = (\d+)', s)
        if m: mesh['vertex_count'] = int(m.group(1))
        if 'vector m_IndexBuffer' in s:
            j = i+1
            while j < len(lines):
                ms = re.search(r'int size = (\d+)', lines[j].strip())
                if ms:
                    mesh['index_buffer'] = _read_n_bytes(lines, j+1, int(ms.group(1)))
                    break
                j += 1
        if 'TypelessData m_DataSize' in s:
            j = i+1
            while j < len(lines):
                ms = re.search(r'int size = (\d+)', lines[j].strip())
                if ms:
                    mesh['vertex_data'] = _read_n_bytes(lines, j+1, int(ms.group(1)))
                    break
                j += 1
        if 'ChannelInfo data' in s:
            ch = {}; j = i+1
            while j < len(lines) and len(ch) < 4:
                sl = lines[j].strip()
                for field in ('stream', 'offset', 'format', 'dimension'):
                    mm = re.search(rf'UInt8 {field} = (\d+)', sl)
                    if mm: ch[field] = int(mm.group(1))
                j += 1
            if len(ch) == 4: mesh['channels'].append(ch)
        if 'SubMesh data' in s:
            sm = {}; j = i+1
            while j < len(lines):
                sl = lines[j].strip()
                if 'SubMesh data' in sl and j > i+1: break
                for field in ('firstByte', 'indexCount'):
                    mm = re.search(rf'unsigned int {field} = (\d+)', sl)
                    if mm: sm[field] = int(mm.group(1))
                j += 1
            if sm: mesh['submeshes'].append(sm)
        i += 1
    return mesh

def decode_mesh_geo(mesh):
    """Return positions (list of (x,y,z)), normals, uvs, triangles (list of (i0,i1,i2))."""
    channels = mesh['channels']
    stride   = compute_stride(channels)
    data     = mesh['vertex_data']
    vc       = mesh['vertex_count']
    idx_fmt  = mesh['index_format']
    idx_sz   = 2 if idx_fmt == 0 else 4
    idx_pack = '<H' if idx_fmt == 0 else '<I'

    CH_POS  = 0
    CH_NORM = 1
    CH_UV0  = 4

    def get_ch(idx):
        return channels[idx] if idx < len(channels) and channels[idx]['dimension'] > 0 else None

    pos_ch  = get_ch(CH_POS)
    norm_ch = get_ch(CH_NORM)
    uv_ch   = get_ch(CH_UV0)

    positions, normals, uvs = [], [], []
    for v in range(vc):
        base = v * stride
        if pos_ch:
            o = base + pos_ch['offset']; f = pos_ch['format']; fs = fmtsize(f)
            positions.append((decode_val(data,o,f), decode_val(data,o+fs,f), decode_val(data,o+2*fs,f)))
        if norm_ch:
            o = base + norm_ch['offset']; f = norm_ch['format']; fs = fmtsize(f)
            normals.append((decode_val(data,o,f), decode_val(data,o+fs,f), decode_val(data,o+2*fs,f)))
        if uv_ch:
            o = base + uv_ch['offset']; f = uv_ch['format']; fs = fmtsize(f)
            # Unity UV: (0,0)=bottom-left; keep as-is for pixel index calc
            uvs.append((decode_val(data,o,f), decode_val(data,o+fs,f)))

    raw_idx = mesh['index_buffer']
    triangles = []
    for sm in mesh['submeshes']:
        first = sm.get('firstByte', 0) // idx_sz
        count = sm.get('indexCount', 0)
        for t in range(count // 3):
            b = first + t*3
            if b+2 >= len(raw_idx)//idx_sz: continue
            i0 = struct.unpack_from(idx_pack, raw_idx, (first+t*3)*idx_sz)[0]
            i1 = struct.unpack_from(idx_pack, raw_idx, (first+t*3+1)*idx_sz)[0]
            i2 = struct.unpack_from(idx_pack, raw_idx, (first+t*3+2)*idx_sz)[0]
            if max(i0,i1,i2) < vc:
                triangles.append((i0, i1, i2))

    return positions, normals, uvs, triangles

# ── Triangle rasterizer in UV space ───────────────────────────────────────────

# UV region boundaries (tex_v=0 at top, matching multiplane_unwrap REGIONS)
_UV_REGIONS = [
    ('right',  0.00, 0.50, 0.50, 1.00),
    ('left',   0.50, 1.00, 0.50, 1.00),
    ('front',  0.00, 0.33, 0.00, 0.50),   # includes overflow_front
    ('back',   0.33, 0.67, 0.00, 0.50),   # includes overflow_back
    ('top',    0.67, 1.00, 0.00, 0.50),
]

def classify_by_uv(u, v):
    """Classify a pixel by its UV position (tex_v=0=top). Consistent with unwrap.py."""
    for name, u0, u1, v0, v1 in _UV_REGIONS:
        if u0 <= u < u1 and v0 <= v < v1:
            return name
    return None

def classify_by_normal(nx, ny, nz, car_axis='z'):
    """Classify a face by its averaged surface normal. Works with any UV layout."""
    ax, ay, az = abs(nx), abs(ny), abs(nz)
    if car_axis == 'z':
        if ay > ax and ay > az and ay > 0.35:
            return 'top'
        if az > ax * 0.7:
            return 'front' if nz > 0 else 'back'
        return 'right' if nx >= 0 else 'left'
    else:
        if ay > az and ay > ax and ay > 0.35:
            return 'top'
        if ax > az * 0.7:
            return 'front' if nx > 0 else 'back'
        return 'right' if nz >= 0 else 'left'

def cross(a, b):
    return (a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0])

def normalize(v):
    l = math.sqrt(v[0]**2+v[1]**2+v[2]**2)
    return (v[0]/l, v[1]/l, v[2]/l) if l > 1e-9 else (0,1,0)

def compute_position_thresholds(positions, car_axis='z'):
    """
    Compute per-side vertex validation thresholds from the mesh AABB.
    A triangle is rejected if any of its vertices contradicts its UV side.
    MARGIN controls how far from the mesh centre a vertex may deviate before
    the triangle is considered geometrically inconsistent with its UV region.
    """
    if not positions:
        return {}
    xs = [p[0] for p in positions]
    ys = [p[1] for p in positions]
    zs = [p[2] for p in positions]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    min_z, max_z = min(zs), max(zs)
    cx = (min_x + max_x) / 2
    cz = (min_z + max_z) / 2
    wx = max_x - min_x
    wz = max_z - min_z
    wy = max_y - min_y

    MARGIN_SIDE = 0.25   # fraction of span allowed on the "wrong" side of centre
    MARGIN_TOP  = 0.30   # fraction of height below which top-faces are rejected

    return {
        'right': {'x_min': cx - MARGIN_SIDE * wx},
        'left':  {'x_max': cx + MARGIN_SIDE * wx},
        'front': {'z_min': cz - MARGIN_SIDE * wz},
        'back':  {'z_max': cz + MARGIN_SIDE * wz},
        'top':   {'y_min': min_y + MARGIN_TOP * wy},
    }


def _tri_passes_threshold(p0, p1, p2, side, thresholds):
    """Return False if any vertex of this triangle contradicts the expected side."""
    if not thresholds or side not in thresholds:
        return True
    t = thresholds[side]
    verts = (p0, p1, p2)
    if 'x_min' in t and any(p[0] < t['x_min'] for p in verts): return False
    if 'x_max' in t and any(p[0] > t['x_max'] for p in verts): return False
    if 'z_min' in t and any(p[2] < t['z_min'] for p in verts): return False
    if 'z_max' in t and any(p[2] > t['z_max'] for p in verts): return False
    if 'y_min' in t and any(p[1] < t['y_min'] for p in verts): return False
    return True


def rasterize(positions, normals, uvs, triangles, tex_w, tex_h, thresholds=None,
              classify_mode='uv', car_axis='z'):
    """
    For each pixel covered by a triangle in UV space, record:
      pixel_index → (side, interp_3d_position)
    Returns dict: pixel_index → (side, (x,y,z))

    classify_mode: 'uv'    — classify by UV region (requires multiplane UV layout)
                   'normal' — classify by averaged vertex normal (works with any UV)
    thresholds: optional dict from compute_position_thresholds(); triangles whose
    vertices contradict their side classification are skipped.
    """
    pixel_map = {}   # pixel_index → (side, pos)

    skipped = 0
    for i0, i1, i2 in triangles:
        p0, p1, p2 = positions[i0], positions[i1], positions[i2]
        u0, v0 = uvs[i0]; u1, v1 = uvs[i1]; u2, v2 = uvs[i2]

        # Determine side for this triangle
        if classify_mode == 'normal':
            n0, n1, n2 = normals[i0], normals[i1], normals[i2]
            avg_nx = (n0[0]+n1[0]+n2[0]) / 3
            avg_ny = (n0[1]+n1[1]+n2[1]) / 3
            avg_nz = (n0[2]+n1[2]+n2[2]) / 3
            l = math.sqrt(avg_nx**2 + avg_ny**2 + avg_nz**2) or 1.0
            tri_side = classify_by_normal(avg_nx/l, avg_ny/l, avg_nz/l, car_axis)
        else:
            cu = (u0 + u1 + u2) / 3
            cv = (v0 + v1 + v2) / 3
            tri_side = classify_by_uv(cu, cv)

        if tri_side and not _tri_passes_threshold(p0, p1, p2, tri_side, thresholds):
            skipped += 1
            continue

        # UV bounding box in pixel space (dump_v=0 is top, same as tex_v)
        px0 = int(u0 * tex_w); py0 = int(v0 * tex_h)
        px1 = int(u1 * tex_w); py1 = int(v1 * tex_h)
        px2 = int(u2 * tex_w); py2 = int(v2 * tex_h)

        min_px = max(0, min(px0,px1,px2))
        max_px = min(tex_w-1, max(px0,px1,px2))
        min_py = max(0, min(py0,py1,py2))
        max_py = min(tex_h-1, max(py0,py1,py2))

        # Precompute edge equations for barycentric test
        denom = (v1-v2)*(u0-u2) + (u2-u1)*(v0-v2)
        if abs(denom) < 1e-10:
            continue

        for py in range(min_py, max_py+1):
            vf = (py + 0.5) / tex_h
            for px in range(min_px, max_px+1):
                uf = (px + 0.5) / tex_w

                w0 = ((v1-v2)*(uf-u2) + (u2-u1)*(vf-v2)) / denom
                w1 = ((v2-v0)*(uf-u2) + (u0-u2)*(vf-v2)) / denom
                w2 = 1.0 - w0 - w1

                if w0 < -0.01 or w1 < -0.01 or w2 < -0.01:
                    continue

                if classify_mode == 'normal':
                    side = tri_side  # same for all pixels in this triangle
                else:
                    side = classify_by_uv(uf, vf)
                    if side is None:
                        continue

                # Interpolated 3D position
                ix = w0*p0[0] + w1*p1[0] + w2*p2[0]
                iy = w0*p0[1] + w1*p1[1] + w2*p2[1]
                iz = w0*p0[2] + w1*p1[2] + w2*p2[2]

                # Pixel index: DirectX/Unity convention, row 0 = TOP of texture
                idx = py * tex_w + px
                if idx not in pixel_map:
                    pixel_map[idx] = (side, (ix, iy, iz))

    if skipped:
        print(f'      [pos-filter] skipped {skipped} geometrically inconsistent triangles')
    return pixel_map

# ── BodyBakeData dump writer ───────────────────────────────────────────────────

def _format_pixel_array(side, pixels_list, indent='  '):
    """Generate UABEA dump lines for one side's pixel array."""
    lines = []
    lines.append(f' 0 Pixel {side}Pixels')
    lines.append(f'  1 Array Array ({len(pixels_list)} items)')
    lines.append(f'   0 int size = {len(pixels_list)}')
    for i, (idx, pos) in enumerate(pixels_list):
        lines.append(f'   [{i}]')
        lines.append(f'    0 Pixel data')
        lines.append(f'     0 int index = {idx}')
        lines.append(f'     0 Vector3 position')
        lines.append(f'      0 float x = {pos[0]:.7g}')
        lines.append(f'      0 float y = {pos[1]:.7g}')
        lines.append(f'      0 float z = {pos[2]:.7g}')
    return lines

def write_bakedata(template_path, out_path, pixel_map):
    """Replace all pixel arrays in the template dump with new data."""
    template = Path(template_path).read_text(encoding='utf-8').splitlines()

    # Group pixels by side
    side_pixels = defaultdict(list)
    for idx, (side, pos) in sorted(pixel_map.items()):
        side_pixels[side].append((idx, pos))

    # Build output: copy header, replace each pixel section
    out = []
    i = 0
    while i < len(template):
        line = template[i]
        matched = False
        for side in SIDES:
            if re.match(rf'\s*0 Pixel {side}Pixels', line):
                out.extend(_format_pixel_array(side, side_pixels[side]))
                # Skip old array lines until next top-level field
                i += 1
                depth = 0
                while i < len(template):
                    l = template[i]
                    if re.match(r'\s*\d+ (Pixel \w+Pixels|MonoBehaviour)', l) and not re.match(r'\s+', l[:2]):
                        break
                    # Count until we exit the array block
                    if re.match(r'\s*0 Pixel \w+Pixels', l):
                        break
                    i += 1
                matched = True
                break
        if not matched:
            out.append(line)
            i += 1

    Path(out_path).write_text('\n'.join(out), encoding='utf-8')
    for side in SIDES:
        print(f'  {side:6s}: {len(side_pixels[side])} pixels')

# ── DXT1 encoder ──────────────────────────────────────────────────────────────

def _rgb_to_565(r, g, b):
    return ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)

def encode_dxt1(rgba_image, w, h):
    """Encode an RGB image (bytearray, 3 bytes/pixel, bottom-up) to DXT1."""
    out = bytearray()
    for by in range((h + 3) // 4):
        for bx in range((w + 3) // 4):
            # Collect 16 pixels of this block
            block_colors = []
            for py in range(4):
                for px in range(4):
                    x = bx*4+px; y = by*4+py
                    if x >= w or y >= h:
                        block_colors.append((0,0,0))
                    else:
                        o = (y*w+x)*3
                        block_colors.append((rgba_image[o], rgba_image[o+1], rgba_image[o+2]))

            unique = list(set(block_colors))
            if len(unique) == 1:
                c565 = _rgb_to_565(*unique[0])
                out += struct.pack('<HHI', c565, c565, 0)
                continue

            # Find two most different colors
            max_dist = -1; c0_rgb = c1_rgb = unique[0]
            for a in unique:
                for b in unique:
                    if a == b: continue
                    d = (a[0]-b[0])**2+(a[1]-b[1])**2+(a[2]-b[2])**2
                    if d > max_dist:
                        max_dist = d; c0_rgb = a; c1_rgb = b

            c0 = _rgb_to_565(*c0_rgb)
            c1 = _rgb_to_565(*c1_rgb)
            # Ensure c0 > c1 for 4-color mode (no transparency)
            if c0 < c1:
                c0, c1 = c1, c0; c0_rgb, c1_rgb = c1_rgb, c0_rgb

            # Build palette
            r0,g0,b0 = c0_rgb; r1,g1,b1 = c1_rgb
            palette = [
                c0_rgb,
                c1_rgb,
                ((2*r0+r1)//3, (2*g0+g1)//3, (2*b0+b1)//3),
                ((r0+2*r1)//3, (g0+2*g1)//3, (b0+2*b1)//3),
            ]

            bits = 0
            for pi, col in enumerate(block_colors):
                best = min(range(4), key=lambda k:
                    (col[0]-palette[k][0])**2+(col[1]-palette[k][1])**2+(col[2]-palette[k][2])**2)
                bits |= (best << (pi*2))

            out += struct.pack('<HHI', c0, c1, bits)
    return out

def build_sidemask_rgb(pixel_map, w, h):
    """Build a w×h RGB image (bottom-up) with side colors per pixel."""
    img = bytearray(w * h * 3)   # all black
    for idx, (side, _) in pixel_map.items():
        col = SIDE_COLOR[side]
        o = idx * 3
        if 0 <= o+2 < len(img):
            img[o:o+3] = col
    return img

def _dxt1_mip(rgb_img, w, h):
    """Encode one mip level; downsample by 2x for next."""
    encoded = encode_dxt1(rgb_img, w, h)
    if w == 1 and h == 1:
        return encoded, rgb_img, 1, 1
    nw = max(1, w//2); nh = max(1, h//2)
    small = bytearray(nw*nh*3)
    for y in range(nh):
        for x in range(nw):
            sy = y*2; sx = x*2
            r = g = b = 0; cnt = 0
            for dy in range(2):
                for dx in range(2):
                    cx=sx+dx; cy=sy+dy
                    if cx < w and cy < h:
                        o=(cy*w+cx)*3; r+=rgb_img[o]; g+=rgb_img[o+1]; b+=rgb_img[o+2]; cnt+=1
            o2=(y*nw+x)*3; small[o2]=(r//cnt); small[o2+1]=(g//cnt); small[o2+2]=(b//cnt)
    return encoded, small, nw, nh

def encode_dxt1_with_mips(rgb_img, w, h, num_mips=10):
    all_data = bytearray()
    cur_img = rgb_img; cw = w; ch = h
    for _ in range(num_mips):
        mip_data, cur_img, cw, ch = _dxt1_mip(cur_img, cw, ch)
        all_data += mip_data
        if cw == 1 and ch == 1 and _ < num_mips-1:
            # Pad remaining mips with 8-byte black DXT1 blocks
            for _r in range(num_mips - _ - 1):
                all_data += b'\x00' * 8
            break
    return all_data

# ── SideMask dump writer ───────────────────────────────────────────────────────

def write_sidemask(template_path, out_path, dxt1_data):
    """Replace embedded image data bytes in the SideMask dump."""
    template = Path(template_path).read_text(encoding='utf-8').splitlines()
    out = []; i = 0
    while i < len(template):
        line = template[i]
        if 'TypelessData image data' in line:
            # Update item count
            line = re.sub(r'\(\d+ items\)', f'({len(dxt1_data)} items)', line)
            out.append(line); i += 1
            # Copy 'int size' line and update
            while i < len(template):
                l = template[i]
                l = re.sub(r'(int size = )\d+', rf'\g<1>{len(dxt1_data)}', l)
                out.append(l); i += 1
                if re.match(r'\s+\[\d+\]', l): break
            # Skip old byte lines
            while i < len(template):
                if re.match(r'\s+(\[\d+\]|\d+ UInt8 data = \d+)', template[i]):
                    i += 1
                else:
                    break
            # Write new byte lines
            for bi, b in enumerate(dxt1_data):
                out.append(f'   [{bi}]')
                out.append(f'    0 UInt8 data = {b}')
        else:
            out.append(line); i += 1

    Path(out_path).write_text('\n'.join(out), encoding='utf-8')

# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    import argparse
    ap = argparse.ArgumentParser(description='Regenerate BodyBakeData + SideMaskTexture from mesh dump.')
    ap.add_argument('mesh',     help='UABEA mesh dump (.txt)')
    ap.add_argument('bakedata', help='BakeData dump used as template AND output')
    ap.add_argument('sidemask', help='SideMaskTexture dump used as template AND output')
    ap.add_argument('--bakedata-out', default=None,
                    help='Output path for BakeData dump (default: overwrite template)')
    ap.add_argument('--sidemask-out', default=None,
                    help='Output path for SideMaskTexture dump (default: overwrite template)')
    ap.add_argument('--car-axis', choices=['x', 'z'], default='z',
                    help='Axis car nose faces: x=Flakwagon, z=GAZ-66/most vehicles (default: z)')
    ap.add_argument('--preview', default=None,
                    help='Save SideMask as PNG for visual verification')
    ap.add_argument('--no-position-filter', action='store_true',
                    help='Disable geometric position filter (keep all triangles regardless of vertex positions)')
    ap.add_argument('--classify', choices=['uv', 'normal'], default='normal',
                    help='Pixel classification: normal=by vertex normal, works with any UV layout (default); '
                         'uv=by UV region, requires multiplane UV layout (unwrap.py --mode multiplane)')
    args = ap.parse_args()

    global CAR_AXIS
    CAR_AXIS = args.car_axis

    bakedata_out = args.bakedata_out or args.bakedata
    sidemask_out = args.sidemask_out or args.sidemask

    print(f'[1/4] Parsing mesh: {Path(args.mesh).name}  (car-axis={CAR_AXIS})')
    mesh = parse_mesh(args.mesh)
    print(f'      {mesh["vertex_count"]} vertices, {len(mesh["submeshes"])} submesh(es)')

    print('[2/4] Decoding geometry...')
    positions, normals, uvs, triangles = decode_mesh_geo(mesh)
    print(f'      {len(positions)} verts, {len(triangles)} triangles')

    if not uvs:
        sys.exit('Mesh has no UV0 channel - cannot rebake.')

    thresholds = None
    if not args.no_position_filter:
        thresholds = compute_position_thresholds(positions, CAR_AXIS)
        print(f'      [pos-filter] thresholds: ' +
              ', '.join(f'{s}={list(v.values())[0]:.3f}' for s, v in thresholds.items()))

    print(f'[3/4] Rasterizing {len(triangles)} triangles into {TEX_W}x{TEX_H} UV space...')
    print(f'      classify={args.classify}')
    pixel_map = rasterize(positions, normals, uvs, triangles, TEX_W, TEX_H, thresholds,
                          classify_mode=args.classify, car_axis=CAR_AXIS)
    print(f'      {len(pixel_map)} pixels covered')

    print('[4/4] Writing outputs...')
    print('  BodyBakeData:')
    write_bakedata(args.bakedata, bakedata_out, pixel_map)
    print(f'  -> {bakedata_out}')

    rgb_img   = build_sidemask_rgb(pixel_map, TEX_W, TEX_H)
    dxt1_data = encode_dxt1_with_mips(rgb_img, TEX_W, TEX_H, num_mips=10)
    print(f'  SideMask DXT1: {len(dxt1_data)} bytes (expected 174776)')
    write_sidemask(args.sidemask, sidemask_out, dxt1_data)
    print(f'  -> {sidemask_out}')

    if args.preview:
        from PIL import Image
        import numpy as np
        arr = np.frombuffer(bytes(rgb_img), dtype=np.uint8).reshape(TEX_H, TEX_W, 3)
        Image.fromarray(arr, 'RGB').save(args.preview)
        print(f'  Preview -> {args.preview}')

    sides = {}
    for side, pos in pixel_map.values():
        sides[side] = sides.get(side, 0) + 1
    for s in ['top', 'front', 'back', 'right', 'left']:
        print(f'    {s:6s}: {sides.get(s, 0)} pixels')
    print('Done.')

if __name__ == '__main__':
    main()

