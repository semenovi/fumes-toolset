#!/usr/bin/env python3
"""
UABEA Unity mesh text dump <-> OBJ converter

Usage:
  python uabea_mesh_converter.py dump2obj <dump.txt> <output.obj>
  python uabea_mesh_converter.py obj2dump <input.obj> <template_dump.txt> <output_dump.txt>

Notes:
  - OBJ groups (g lines) map 1-to-1 to Unity submeshes in order.
    The leading "o <name>" object line is treated as the mesh name, not a group.
  - UV V-axis is flipped automatically (Unity <-> OBJ convention).
  - obj2dump requires the original dump as a template for channel layout
    and all other mesh metadata (compression flags, blendshapes, etc.).
  - Some Unity meshes contain index-buffer values that exceed vertex_count
    (game-engine internal use). dump2obj skips those faces with a warning;
    Blender-generated replacements won't have this issue.
"""

import struct, re, sys, math, argparse
from pathlib import Path

# ── Format helpers ─────────────────────────────────────────────────────────────

FORMAT_BYTES = {0: 4, 1: 2, 2: 1, 3: 1, 4: 2, 5: 2, 6: 1, 7: 2, 8: 4, 11: 2}

def fmtsize(f):
    return FORMAT_BYTES.get(f, 4)

def _half_to_float(h):
    s = (h >> 15) & 1
    e = (h >> 10) & 0x1f
    m = h & 0x3ff
    if e == 0:
        val = (m / 1024.0) * (2 ** -14) if m else 0.0
    elif e == 31:
        val = float('inf') if not m else float('nan')
    else:
        val = (1.0 + m / 1024.0) * (2 ** (e - 15))
    return -val if s else val

def _float_to_half(f):
    if math.isnan(f):  return 0x7FFF
    if math.isinf(f):  return 0xFC00 if f < 0 else 0x7C00
    v = struct.unpack('<I', struct.pack('<f', f))[0]
    s = v >> 31
    e = ((v >> 23) & 0xFF) - 127 + 15
    m = (v >> 13) & 0x3FF
    if e <= 0:  return s << 15
    if e >= 31: return (s << 15) | 0x7C00
    return (s << 15) | (e << 10) | m

def decode_val(data, off, fmt):
    if fmt == 0:       return struct.unpack_from('<f', data, off)[0]
    if fmt in (1, 11): return _half_to_float(struct.unpack_from('<H', data, off)[0])
    if fmt == 2:       return data[off] / 255.0
    if fmt == 3:
        v = data[off]; return max(-1.0, (v - 256 if v > 127 else v) / 127.0)
    if fmt == 4:       return struct.unpack_from('<H', data, off)[0] / 65535.0
    if fmt == 5:       return struct.unpack_from('<h', data, off)[0] / 32767.0
    return 0.0

def encode_val(v, fmt):
    if fmt == 0:       return struct.pack('<f', float(v))
    if fmt in (1, 11): return struct.pack('<H', _float_to_half(float(v)))
    if fmt == 2:       return bytes([max(0, min(255, round(v * 255)))])
    if fmt == 3:       return bytes([max(-128, min(127, round(v * 127))) & 0xFF])
    if fmt == 4:       return struct.pack('<H', max(0, min(65535, round(v * 65535))))
    if fmt == 5:       return struct.pack('<h', max(-32768, min(32767, round(v * 32767))))
    return b'\x00' * fmtsize(fmt)

# ── Stride ─────────────────────────────────────────────────────────────────────

def compute_stride(channels):
    """Byte stride for one vertex (single stream, 4-byte aligned)."""
    max_end = 0
    for ch in channels:
        if ch['dimension'] == 0: continue
        end = ch['offset'] + ch['dimension'] * fmtsize(ch['format'])
        if end > max_end:
            max_end = end
    return (max_end + 3) & ~3

# ── Dump parser ────────────────────────────────────────────────────────────────

def _read_n_bytes(lines, start, count):
    data = bytearray()
    i = start
    while i < len(lines) and len(data) < count:
        m = re.search(r'UInt8 data = (\d+)', lines[i])
        if m:
            data.append(int(m.group(1)))
        i += 1
    return data

def parse_dump(path):
    lines = Path(path).read_text(encoding='utf-8').splitlines()

    mesh = {
        'name':         'Mesh',
        'index_format': 0,
        'submeshes':    [],
        'channels':     [],
        'vertex_count': 0,
        'index_buffer': bytearray(),
        'vertex_data':  bytearray(),
    }

    i = 0
    while i < len(lines):
        s = lines[i].strip()

        m = re.search(r'm_Name = "([^"]*)"', s)
        if m:
            mesh['name'] = m.group(1)

        m = re.search(r'm_IndexFormat = (\d+)', s)
        if m:
            mesh['index_format'] = int(m.group(1))

        m = re.search(r'm_VertexCount = (\d+)', s)
        if m:
            mesh['vertex_count'] = int(m.group(1))

        if 'vector m_IndexBuffer' in s:
            j = i + 1
            while j < len(lines):
                ms = re.search(r'int size = (\d+)', lines[j].strip())
                if ms:
                    mesh['index_buffer'] = _read_n_bytes(lines, j + 1, int(ms.group(1)))
                    break
                j += 1

        if 'TypelessData m_DataSize' in s:
            j = i + 1
            while j < len(lines):
                ms = re.search(r'int size = (\d+)', lines[j].strip())
                if ms:
                    mesh['vertex_data'] = _read_n_bytes(lines, j + 1, int(ms.group(1)))
                    break
                j += 1

        # SubMesh: stop look-ahead at the NEXT 'SubMesh data' marker so we
        # don't accidentally read the next submesh's fields into this one.
        if 'SubMesh data' in s:
            sm = {}
            j = i + 1
            pv_state = None
            pv_axes  = {}
            while j < len(lines):
                sl = lines[j].strip()
                if 'SubMesh data' in sl and j > i + 1:
                    break   # reached next submesh
                if re.match(r'^[01] (BlendShapeData|vector m_BindPose|UInt8 m_MeshCompression|bool m_Is)', sl):
                    break   # reached unrelated section

                for field in ('firstByte', 'indexCount', 'baseVertex', 'firstVertex', 'vertexCount'):
                    mm = re.search(rf'unsigned int {field} = (\d+)', sl)
                    if mm:
                        sm[field] = int(mm.group(1))

                if 'Vector3f m_Center' in sl:
                    pv_state = 'center'; pv_axes = {}
                elif 'Vector3f m_Extent' in sl:
                    pv_state = 'extent'; pv_axes = {}
                elif pv_state:
                    for axis in 'xyz':
                        mm = re.search(rf'float {axis} = ([+-]?[\d.eE+-]+)', sl)
                        if mm:
                            pv_axes[axis] = float(mm.group(1))
                    if len(pv_axes) == 3:
                        sm[pv_state] = (pv_axes['x'], pv_axes['y'], pv_axes['z'])
                        pv_state = None; pv_axes = {}
                j += 1

            if sm:
                mesh['submeshes'].append(sm)

        if 'ChannelInfo data' in s:
            ch = {}
            j = i + 1
            while j < len(lines) and len(ch) < 4:
                sl = lines[j].strip()
                for field in ('stream', 'offset', 'format', 'dimension'):
                    mm = re.search(rf'UInt8 {field} = (\d+)', sl)
                    if mm:
                        ch[field] = int(mm.group(1))
                j += 1
            if len(ch) == 4:
                mesh['channels'].append(ch)

        i += 1

    return mesh, lines

# ── Vertex encoder / decoder ───────────────────────────────────────────────────

CH_POS  = 0   # position  (float32 x3)
CH_NORM = 1   # normal    (float32 x3)
CH_UV0  = 4   # uv0       (float32 x2)

def decode_vertices(data, channels, vertex_count):
    stride = compute_stride(channels)

    def ch(idx):
        return channels[idx] if idx < len(channels) and channels[idx]['dimension'] > 0 else None

    pos_ch  = ch(CH_POS)
    norm_ch = ch(CH_NORM)
    uv_ch   = ch(CH_UV0)
    positions, normals, uvs = [], [], []

    for v in range(vertex_count):
        base = v * stride
        if pos_ch:
            o = base + pos_ch['offset']; f = pos_ch['format']; fs = fmtsize(f)
            positions.append((decode_val(data, o,       f),
                               decode_val(data, o +  fs, f),
                               decode_val(data, o + 2*fs, f)))
        if norm_ch:
            o = base + norm_ch['offset']; f = norm_ch['format']; fs = fmtsize(f)
            normals.append((decode_val(data, o,       f),
                             decode_val(data, o +  fs, f),
                             decode_val(data, o + 2*fs, f)))
        if uv_ch:
            o = base + uv_ch['offset']; f = uv_ch['format']; fs = fmtsize(f)
            uvs.append((decode_val(data, o,      f),
                         decode_val(data, o + fs, f)))

    return positions, normals, uvs

def encode_vertices(positions, normals, uvs, channels):
    stride = compute_stride(channels)
    data   = bytearray(stride * len(positions))

    def write_ch(ch_idx, values, n_comp):
        if ch_idx >= len(channels): return
        ch = channels[ch_idx]
        if ch['dimension'] == 0: return
        fmt = ch['format']; fs = fmtsize(fmt)
        for vi, vals in enumerate(values):
            base = vi * stride + ch['offset']
            for c in range(n_comp):
                enc = encode_val(vals[c], fmt)
                data[base + c*fs: base + c*fs + fs] = enc

    write_ch(CH_POS,  positions, 3)
    if normals: write_ch(CH_NORM, normals, 3)
    if uvs:     write_ch(CH_UV0, uvs,     2)
    return data

def decode_indices(data, index_format):
    fmt = '<H' if index_format == 0 else '<I'
    sz  = 2    if index_format == 0 else 4
    return [struct.unpack_from(fmt, data, i * sz)[0] for i in range(len(data) // sz)]

def encode_indices(submesh_index_lists, index_format):
    fmt = '<H' if index_format == 0 else '<I'
    buf = bytearray()
    for idxs in submesh_index_lists:
        for idx in idxs:
            buf += struct.pack(fmt, idx)
    return buf

# ── AABB ───────────────────────────────────────────────────────────────────────

def calc_aabb(positions):
    if not positions:
        return (0.0, 0.0, 0.0), (0.0, 0.0, 0.0)
    xs = [p[0] for p in positions]
    ys = [p[1] for p in positions]
    zs = [p[2] for p in positions]
    mn = (min(xs), min(ys), min(zs))
    mx = (max(xs), max(ys), max(zs))
    return ((mn[0]+mx[0])/2, (mn[1]+mx[1])/2, (mn[2]+mx[2])/2), \
           ((mx[0]-mn[0])/2, (mx[1]-mn[1])/2, (mx[2]-mn[2])/2)

# ── dump2obj ───────────────────────────────────────────────────────────────────

def dump2obj(dump_path, obj_path):
    mesh, _ = parse_dump(dump_path)

    if not mesh['channels']:
        sys.exit('[dump2obj] No channel info – is this a valid UABEA mesh dump?')
    if not mesh['vertex_data']:
        sys.exit('[dump2obj] No vertex data found.')

    vc       = mesh['vertex_count']
    positions, normals, uvs = decode_vertices(mesh['vertex_data'], mesh['channels'], vc)
    indices  = decode_indices(mesh['index_buffer'], mesh['index_format'])
    idx_size = 2 if mesh['index_format'] == 0 else 4

    out = [f'# Unity mesh: {mesh["name"]}', f'o {mesh["name"]}', '']

    for p in positions:
        out.append(f'v {p[0]:.7f} {p[1]:.7f} {p[2]:.7f}')
    if normals:
        out.append('')
        for n in normals:
            out.append(f'vn {n[0]:.7f} {n[1]:.7f} {n[2]:.7f}')
    if uvs:
        out.append('')
        for u in uvs:
            out.append(f'vt {u[0]:.7f} {1.0 - u[1]:.7f}')  # flip V for OBJ

    out.append('')
    total_tris = 0
    skipped    = 0

    for si, sm in enumerate(mesh['submeshes']):
        out.append(f'g submesh_{si}')
        first = sm.get('firstByte', 0) // idx_size
        count = sm.get('indexCount', 0)

        for tri in range(count // 3):
            b = first + tri * 3
            if b + 2 >= len(indices):
                skipped += 1; continue
            i0, i1, i2 = indices[b], indices[b+1], indices[b+2]
            # Skip faces whose indices exceed the vertex buffer
            # (can happen with some GPU-side mesh optimisations Unity applies)
            if i0 >= vc or i1 >= vc or i2 >= vc:
                skipped += 1; continue
            total_tris += 1
            if normals and uvs:
                out.append(f'f {i0+1}/{i0+1}/{i0+1} {i1+1}/{i1+1}/{i1+1} {i2+1}/{i2+1}/{i2+1}')
            elif normals:
                out.append(f'f {i0+1}//{i0+1} {i1+1}//{i1+1} {i2+1}//{i2+1}')
            elif uvs:
                out.append(f'f {i0+1}/{i0+1} {i1+1}/{i1+1} {i2+1}/{i2+1}')
            else:
                out.append(f'f {i0+1} {i1+1} {i2+1}')

    Path(obj_path).write_text('\n'.join(out), encoding='utf-8')
    warn = f'  ({skipped} faces skipped – out-of-range indices)' if skipped else ''
    print(f'[dump2obj] {vc} verts  {total_tris} tris  '
          f'{len(mesh["submeshes"])} submesh(es)  ->  {obj_path}{warn}')

# ── OBJ parser ─────────────────────────────────────────────────────────────────

def parse_obj(path):
    raw_pos, raw_norm, raw_uv = [], [], []
    groups = {}
    cur    = None   # no group until 'g' is seen

    for line in Path(path).read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#'): continue
        parts = line.split()
        cmd   = parts[0]

        if cmd == 'v':
            raw_pos.append(tuple(float(x) for x in parts[1:4]))
        elif cmd == 'vn':
            raw_norm.append(tuple(float(x) for x in parts[1:4]))
        elif cmd == 'vt':
            raw_uv.append(tuple(float(x) for x in parts[1:3]))
        elif cmd == 'o':
            pass   # object name – NOT a submesh group, ignore for grouping
        elif cmd == 'g':
            cur = parts[1] if len(parts) > 1 else '__default__'
            groups.setdefault(cur, [])
        elif cmd == 'usemtl':
            # Treat usemtl as group separator when Blender exports without g lines
            cur = parts[1] if len(parts) > 1 else '__default__'
            groups.setdefault(cur, [])
        elif cmd == 'f':
            if cur is None:
                cur = '__default__'     # faces before any 'g' line
            groups.setdefault(cur, [])
            verts = []
            for tok in parts[1:]:
                c   = tok.split('/')
                vi  = int(c[0]) - 1
                vti = int(c[1]) - 1 if len(c) > 1 and c[1] else None
                vni = int(c[2]) - 1 if len(c) > 2 and c[2] else None
                verts.append((vi, vti, vni))
            for k in range(1, len(verts) - 1):   # fan triangulation
                groups[cur].append((verts[0], verts[k], verts[k+1]))

    return raw_pos, raw_norm, raw_uv, groups

def expand_vertices(raw_pos, raw_norm, raw_uv, groups, flip_v=True):
    """
    OBJ uses per-corner v/vt/vn indices; Unity needs a unified vertex buffer.
    Out-of-range indices are clamped to the nearest valid vertex.
    Returns positions, normals, uvs, submesh_index_lists (one per group).

    flip_v=True  (default): V = 1-v_obj — correct for standard OBJ (V=0 at bottom)
                             and for OBJs produced by dump2obj.
    flip_v=False: V = v_obj as-is — use when the OBJ was exported with Unity/DirectX
                  convention already applied (V=0 at top), e.g. Blender exports where
                  the artist manually matched Unity UV space.
    """
    n_pos  = len(raw_pos)
    n_norm = len(raw_norm)
    n_uv   = len(raw_uv)
    has_norm = n_norm > 0
    has_uv   = n_uv   > 0

    vertex_map = {}
    positions, normals, uvs = [], [], []

    def get_or_create(vi, vti, vni):
        vi  = max(0, min(vi,  n_pos  - 1)) if vi  is not None else 0
        vti = max(0, min(vti, n_uv   - 1)) if vti is not None else 0
        vni = max(0, min(vni, n_norm - 1)) if vni is not None else 0
        key = (vi, vti if has_uv else None, vni if has_norm else None)
        if key not in vertex_map:
            idx = len(positions)
            vertex_map[key] = idx
            positions.append(raw_pos[vi])
            if has_norm: normals.append(raw_norm[vni])
            if has_uv:
                u, v_obj = raw_uv[vti]
                uvs.append((u, 1.0 - v_obj if flip_v else v_obj))
        return vertex_map[key]

    submesh_index_lists = []
    for group_faces in groups.values():
        idxs = []
        for tri in group_faces:
            for vert in tri:
                idxs.append(get_or_create(*vert))
        submesh_index_lists.append(idxs)

    return positions, normals, uvs, submesh_index_lists

# ── Dump writer helpers ────────────────────────────────────────────────────────

def _replace_byte_section(lines, marker, new_data):
    """
    Find a byte-array section by marker, update all count fields, replace bytes.
    Handles both:
      'TypelessData m_DataSize (N items)' – count is on the marker line
      'vector m_IndexBuffer'              – count is on the 'Array Array' line below
    """
    out = []
    i   = 0
    while i < len(lines):
        line = lines[i]
        if marker in line:
            # Update (N items) on this line if present (TypelessData case)
            line = re.sub(r'\(\d+ items\)', f'({len(new_data)} items)', line)
            out.append(line); i += 1

            # Walk through intermediate header lines (Array Array, int size)
            # until we reach the first '[N]' byte-index line
            while i < len(lines):
                l = lines[i]
                if re.match(r'\s+\[\d+\]\s*$', l):  # start of byte data
                    break
                l = re.sub(r'\(\d+ items\)', f'({len(new_data)} items)', l)
                l = re.sub(r'(int size = )\d+', rf'\g<1>{len(new_data)}', l)
                out.append(l); i += 1

            # Skip all old byte lines ([N] and UInt8 data = V)
            while i < len(lines) and re.match(r'\s+(\[\d+\]|\d+ UInt8 data = \d+)', lines[i]):
                i += 1

            # Emit new byte lines
            for bi, b in enumerate(new_data):
                out.append(f'   [{bi}]')
                out.append(f'    0 UInt8 data = {b}')
        else:
            out.append(line); i += 1
    return out

def _update_submeshes(lines, submesh_index_lists, positions, index_format):
    """Patch firstByte/indexCount/firstVertex/vertexCount and AABBs per submesh."""
    idx_size = 2 if index_format == 0 else 4
    n        = len(submesh_index_lists)

    # Pre-compute submesh metadata and AABBs
    meta  = []
    aabbs = []
    cur_byte = 0
    for idxs in submesh_index_lists:
        used    = sorted(set(idxs))
        first_v = used[0] if used else 0
        vert_c  = (max(used) - first_v + 1) if used else 0
        meta.append({'firstByte':   cur_byte,
                     'indexCount':  len(idxs),
                     'firstVertex': first_v,
                     'vertexCount': vert_c})
        cur_byte += len(idxs) * idx_size
        aabbs.append(calc_aabb([positions[v] for v in used if v < len(positions)]))

    out      = []
    sm_idx   = -1
    in_sm    = False
    pv_state = None
    pv_axes  = {}

    for line in lines:
        s = line.strip()

        if 'SubMesh data' in s:
            sm_idx  += 1
            in_sm   = True
            pv_state = None; pv_axes = {}

        if in_sm and 0 <= sm_idx < n:
            m              = meta[sm_idx]
            center, extent = aabbs[sm_idx]

            for field in ('firstByte', 'indexCount', 'baseVertex', 'firstVertex', 'vertexCount'):
                if f'unsigned int {field} = ' in s:
                    if field in m:
                        line = re.sub(r'(unsigned int ' + field + r' = )\d+',
                                      rf'\g<1>{m[field]}', line)
                    break

            if 'Vector3f m_Center' in s:
                pv_state = 'center'; pv_axes = {}
            elif 'Vector3f m_Extent' in s:
                pv_state = 'extent'; pv_axes = {}
            elif pv_state:
                vec = center if pv_state == 'center' else extent
                for ai, axis in enumerate('xyz'):
                    if re.match(rf'\s+\d+ float {axis} = ', line):
                        line = re.sub(r'(float ' + axis + r' = )[^\s]+',
                                      rf'\g<1>{vec[ai]:.7g}', line)
                        pv_axes[axis] = True
                        if len(pv_axes) == 3:
                            pv_state = None
                        break

        out.append(line)
    return out

# ── obj2dump ───────────────────────────────────────────────────────────────────

def obj2dump(obj_path, template_path, out_path, flip_v=True):
    mesh, template_lines = parse_dump(template_path)

    if not mesh['channels']:
        sys.exit('[obj2dump] Template has no channel info.')

    raw_pos, raw_norm, raw_uv, groups = parse_obj(obj_path)
    if not groups:
        sys.exit('[obj2dump] No geometry (g groups) found in OBJ.')

    positions, normals, uvs, submesh_index_lists = expand_vertices(
        raw_pos, raw_norm, raw_uv, groups, flip_v=flip_v)

    # Pad to template submesh count with degenerate triangles
    n_template = len(mesh['submeshes'])
    if len(submesh_index_lists) < n_template:
        pad = n_template - len(submesh_index_lists)
        submesh_index_lists += [[0, 0, 0]] * pad
        print(f'[obj2dump] Padding {pad} missing submesh(es) with degenerate triangles')

    n_tris = sum(len(g) // 3 for g in submesh_index_lists)
    print(f'[obj2dump] {len(positions)} unified verts  {n_tris} tris  '
          f'{len(submesh_index_lists)} group(s)')

    vert_bytes  = encode_vertices(positions, normals, uvs, mesh['channels'])
    index_bytes = encode_indices(submesh_index_lists, mesh['index_format'])

    lines = list(template_lines)
    lines = _replace_byte_section(lines, 'vector m_IndexBuffer', index_bytes)
    lines = _replace_byte_section(lines, 'TypelessData m_DataSize', vert_bytes)

    # Update m_VertexCount
    lines = [re.sub(r'(unsigned int m_VertexCount = )\d+', rf'\g<1>{len(positions)}', l)
             for l in lines]

    lines = _update_submeshes(lines, submesh_index_lists, positions, mesh['index_format'])

    Path(out_path).write_text('\n'.join(lines), encoding='utf-8')
    print(f'[obj2dump] Saved  ->  {out_path}')


# ── nullify ────────────────────────────────────────────────────────────────────

def nullify(dump_path):
    """
    Modify a UABEA mesh dump in-place so the mesh is valid but invisible.
    Replaces geometry with a single vertex at the origin and one degenerate
    triangle (indices 0,0,0) per submesh – zero screen area, never drawn.
    """
    mesh, template_lines = parse_dump(dump_path)

    if not mesh['channels']:
        sys.exit('[nullify] No channel info – is this a valid UABEA mesh dump?')

    n_submeshes = max(len(mesh['submeshes']), 1)

    # One vertex: position (0,0,0), normal (0,1,0), uv (0,0)
    positions = [(0.0, 0.0, 0.0)]
    normals   = [(0.0, 1.0, 0.0)] if any(
        c['dimension'] > 0 for c in mesh['channels'][1:2]) else []
    uvs       = [(0.0, 0.0)] if any(
        c['dimension'] > 0 for c in mesh['channels'][4:5]) else []

    # One degenerate triangle per submesh (all three indices point to vertex 0)
    submesh_index_lists = [[0, 0, 0] for _ in range(n_submeshes)]

    vert_bytes  = encode_vertices(positions, normals, uvs, mesh['channels'])
    index_bytes = encode_indices(submesh_index_lists, mesh['index_format'])

    lines = list(template_lines)
    lines = _replace_byte_section(lines, 'vector m_IndexBuffer', index_bytes)
    lines = _replace_byte_section(lines, 'TypelessData m_DataSize', vert_bytes)

    lines = [re.sub(r'(unsigned int m_VertexCount = )\d+', r'\g<1>1', l)
             for l in lines]

    lines = _update_submeshes(lines, submesh_index_lists, positions,
                              mesh['index_format'])

    Path(dump_path).write_text('\n'.join(lines), encoding='utf-8')
    print(f'[nullify] {dump_path}  ->  1 vert, {n_submeshes} degenerate submesh(es) (invisible)')

# ── fix_submeshes ──────────────────────────────────────────────────────────────

def fix_submeshes(dump_path, out_path):
    """
    Fix a body mesh dump where all submeshes share identical geometry.
    Submesh 0 keeps its full geometry; submeshes 1..N get one degenerate
    triangle each (indices 0,0,0) so their materials render nothing.
    Vertex data is preserved exactly as-is.
    """
    mesh, template_lines = parse_dump(dump_path)

    if not mesh['channels']:
        sys.exit('[fix_submeshes] No channel info – is this a valid UABEA mesh dump?')

    n_submeshes = len(mesh['submeshes'])
    if n_submeshes < 2:
        print(f'[fix_submeshes] Only {n_submeshes} submesh(es), nothing to fix.')
        return

    # Extract submesh 0 indices
    sm0       = mesh['submeshes'][0]
    idx_size  = 2 if mesh['index_format'] == 0 else 4
    first     = sm0.get('firstByte', 0) // idx_size
    count     = sm0.get('indexCount', 0)
    all_idxs  = decode_indices(mesh['index_buffer'], mesh['index_format'])
    sm0_idxs  = list(all_idxs[first:first + count])

    # Submeshes 1..N become degenerate (single invisible triangle at vertex 0)
    submesh_index_lists = [sm0_idxs] + [[0, 0, 0]] * (n_submeshes - 1)

    # Decode positions only for AABB computation (vertex data bytes unchanged)
    positions, _, _ = decode_vertices(mesh['vertex_data'], mesh['channels'],
                                      mesh['vertex_count'])

    index_bytes = encode_indices(submesh_index_lists, mesh['index_format'])

    lines = list(template_lines)
    lines = _replace_byte_section(lines, 'vector m_IndexBuffer', index_bytes)
    # Vertex data stays unchanged – no _replace_byte_section for m_DataSize
    lines = _update_submeshes(lines, submesh_index_lists, positions,
                              mesh['index_format'])

    Path(out_path).write_text('\n'.join(lines), encoding='utf-8')
    print(f'[fix_submeshes] Submesh 0 kept ({count} indices, {mesh["vertex_count"]} verts), '
          f'submeshes 1-{n_submeshes - 1} made degenerate.')
    print(f'[fix_submeshes] Saved -> {out_path}')
# ── CLI ────────────────────────────────────────────────────────────────────────

def main():
    p   = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest='cmd', required=True)

    p1 = sub.add_parser('dump2obj', help='UABEA dump  ->  OBJ')
    p1.add_argument('dump', help='UABEA text dump (.txt)')
    p1.add_argument('obj',  help='Output OBJ')

    p2 = sub.add_parser('obj2dump', help='OBJ  ->  UABEA dump  (requires template)')
    p2.add_argument('obj',      help='Input OBJ')
    p2.add_argument('template', help='Original UABEA dump (channel layout + metadata source)')
    p2.add_argument('output',   help='Output UABEA dump')
    p2.add_argument('--no-v-flip', action='store_true',
                    help='Do not flip V coordinate (use when OBJ was exported with Unity/DirectX '
                         'V convention: V=0 at top). Default flips V assuming standard OBJ convention.')

    p4 = sub.add_parser('fix_submeshes',
                        help='Keep submesh 0 geometry, make submeshes 1-N degenerate (fixes Z-fighting body meshes)')
    p4.add_argument('dump',   help='UABEA mesh dump to fix')
    p4.add_argument('output', help='Output fixed dump')

    p3 = sub.add_parser('nullify',
                        help='Make mesh invisible in-place: 1 vertex + degenerate triangles')
    p3.add_argument('dump', help='UABEA text dump to modify in-place')

    args = p.parse_args()
    if args.cmd == 'dump2obj':
        dump2obj(args.dump, args.obj)
    elif args.cmd == 'obj2dump':
        obj2dump(args.obj, args.template, args.output, flip_v=not args.no_v_flip)
    elif args.cmd == 'fix_submeshes':
        fix_submeshes(args.dump, args.output)
    else:
        nullify(args.dump)

if __name__ == '__main__':
    main()
