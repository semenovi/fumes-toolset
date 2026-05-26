# fumes-toolset

Modding tools for **FUMES**. Lets you replace vehicle body meshes and regenerate the skin/paint system data (BakeData + SideMask) to match.

## Tools

### `uabea_mesh_converter.py`

Converts between UABEA text dumps and OBJ files.

```
python uabea_mesh_converter.py dump2obj <dump.txt> <output.obj>
python uabea_mesh_converter.py obj2dump [--no-v-flip] <input.obj> <template_dump.txt> <output_dump.txt>
python uabea_mesh_converter.py fix_submeshes <dump.txt> <output.txt>
python uabea_mesh_converter.py nullify <dump.txt>
```

**Commands:**

| Command | Description |
|---------|-------------|
| `dump2obj` | Export a UABEA mesh dump to OBJ for viewing/editing in Blender |
| `obj2dump` | Import an OBJ back into a UABEA dump (requires the original dump as channel layout template) |
| `fix_submeshes` | Keep submesh 0 geometry, collapse submeshes 1–N to degenerate triangles. Needed for Flakwagon-type bodies where Unity bakes all submeshes identically. |
| `nullify` | Replace a mesh with a single invisible vertex + degenerate triangles per submesh |

**`obj2dump` notes:**
- Use `--no-v-flip` when your OBJ was exported from Blender with standard UV (V=0 at top) — i.e., when you haven't manually flipped V in your UV layout.
- Blender's `usemtl` lines are treated as submesh group separators (same as `g` lines).
- If the OBJ has fewer groups than the template's submesh count, missing submeshes are automatically padded with degenerate triangles.

**Submeshes in Blender:**

Each material slot in Blender = one submesh in Unity. When exporting OBJ from Blender, each material boundary produces a `usemtl` line, which the converter reads as a submesh separator. If a material slot has no faces, Blender skips its `usemtl` — add a single zero-area triangle to force it through, or rely on automatic padding.

---

### `rebake.py`

Regenerates `BodyBakeData` and `SideMaskTexture` assets for a replaced body mesh. These drive the car paint / skin system.

```
python rebake.py <mesh_dump.txt> <bakedata_template.txt> <sidemask_template.txt> \
  --bakedata-out <output_bakedata.txt> \
  --sidemask-out <output_sidemask.txt> \
  --car-axis z \
  --preview sidemask_preview.png
```

**Options:**

| Flag | Description |
|------|-------------|
| `--car-axis x\|z` | Which axis the car nose faces. `x` = Flakwagon, `z` = most other vehicles (GAZ-66, Caro pickup, etc.) |
| `--classify uv\|normal` | How to assign pixels to sides. `normal` (default) works with any UV layout. `uv` requires multiplane unwrap. |
| `--no-position-filter` | Disable the geometric sanity filter that rejects triangles whose vertices contradict their side classification |
| `--preview <file.png>` | Save a colour-coded SideMask preview image (requires Pillow) |

**How the skin system works:**

1. `SideMaskTexture` (DXT1 512×512): each pixel is coloured by which side of the car it belongs to — green=top, red=right, blue=left, black=front/back.
2. `BodyBakeData`: maps each UV pixel index to its 3D position in mesh-local space.
3. At runtime, the game uses these to project paint decals onto the correct faces.

After replacing a body mesh, both files must be regenerated to match the new UV layout and geometry.

---

## Typical Blender → game pipeline

```powershell
# 1. Export from Blender with correct material slots (= submeshes)
#    Use standard OBJ export, no special settings needed.

# 2. Convert OBJ to UABEA dump
python uabea_mesh_converter.py obj2dump --no-v-flip "my_body.obj" `
  "all-original/CaroBody-original-sharedassets0.assets-2410.txt" `
  "out/CaroBody-sharedassets0.assets-2410.txt"

# 3. Regenerate skin data
python rebake.py "out/CaroBody-sharedassets0.assets-2410.txt" `
  "all-original/CaroBodyBakeData-original-sharedassets0.assets-116654.txt" `
  "all-original/CaroSideMaskTexture-original-sharedassets0.assets-1229.txt" `
  --bakedata-out "out/CaroBodyBakeData-sharedassets0.assets-116654.txt" `
  --sidemask-out "out/CaroSideMaskTexture-sharedassets0.assets-1229.txt" `
  --car-axis z --preview sidemask_preview.png

# 4. Import via UABEA into FUMES_Data/sharedassets0.assets
```

For non-body parts (doors, exhausts, chrome details) that don't participate in the skin system, only step 2 is needed — no rebake.

---

### `patch_ress.py`

Patches a Unity `.resS` file with a new texture image (RGB24 with full mip chain).

FUMES stores most vehicle textures (albedo, carpaint mask, skins) in `sharedassets0.assets.resS` as raw RGB24 data. UABEA can't edit `.resS` directly — use this script to write a new PNG into the correct offset.

```
python patch_ress.py <input.png> <ress_file> <offset> [--size 512] [--flip-y]
```

**Options:**

| Flag | Description |
|------|-------------|
| `--size N` | Base texture size in pixels (default 512). Must match what the asset expects. |
| `--flip-y` | Extra vertical flip — try if the texture appears upside-down in-game. |

**Example:**

```powershell
# Replace the Flakwagon albedo texture
python patch_ress.py "fw-textures\AlbedoTexture.png" `
  "FUMES_Data\sharedassets0.assets.resS" 16242104 --size 512
```

**Finding the offset:** Open the texture asset in UABEA, look at the `m_StreamData` field — it contains the offset into the `.resS` file and the expected byte size. The size for a 512×512 RGB24 texture with 10 mip levels is always 1 048 575 bytes.

---

## Caro submesh layout (reference)

**CaroBody** (asset 2410):

| Submesh | Material | Notes |
|---------|----------|-------|
| 0 | CaroBodyMaterial | Main body — paintable |
| 1 | CaroWindowMaterial | Window glass |
| 2 | Chrome/carpet | Interior/chrome details |
| 3 | CaroLampFrontMaterial | Front headlight lenses — glows when lights on |
| 4 | CaroLampRearMaterial | Rear taillight lenses — glows when lights on |

**CaroChromeDetails02** (asset 2455) — roof lamps (`PartRoofLamps`):

| Submesh | Material | Notes |
|---------|----------|-------|
| 0 | CaroChromeMaterial | Chrome housing |
| 1 | CaroLampFrontMaterial | Roof spotlight lenses — glows when lights on |

Lamp glow is driven by emission on the lamp materials. If a lamp submesh is degenerate (no real geometry), there is no glow effect.

---

## Requirements

- Python 3.8+
- `Pillow` — only for `rebake.py --preview` (PNG output)
- UABEA — to import/export the `.txt` dumps from `sharedassets0.assets`
