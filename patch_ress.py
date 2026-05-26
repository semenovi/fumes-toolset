#!/usr/bin/env python3
"""
patch_ress.py - Patch a Unity .resS file with a new RGB24 texture (with mipmaps).

Usage:
    python patch_ress.py <input.png> <ress_file> <offset> [--size 512] [--flip-y]

The PNG is resized to --size x --size, encoded as raw RGB24 with 10 mip levels
(total 1048575 bytes for 512x512), and written into the .resS file at <offset>.

Examples:
    python patch_ress.py AlbedoTexture4.png sharedassets0.assets.resS 16242104
    python patch_ress.py carpaint_mask_mu3.png sharedassets0.assets.resS 208870472
"""

import sys
import argparse
import struct
from pathlib import Path
from PIL import Image


def encode_rgb24_with_mipmaps(img, base_size):
    """
    Encode image as RGB24 with full mip chain (bottom mip = 1x1).
    Unity stores uncompressed textures bottom-to-top (OpenGL convention),
    so we flip vertically before encoding each mip.
    Returns raw bytes: mip0 (base_size) then mip1 ... mip9 (1x1).
    """
    img = img.convert('RGB')
    img = img.resize((base_size, base_size), Image.LANCZOS)

    data = bytearray()
    size = base_size
    current = img
    while size >= 1:
        flipped = current.transpose(Image.FLIP_TOP_BOTTOM)
        data.extend(flipped.tobytes())  # raw RGB bytes
        if size == 1:
            break
        size //= 2
        current = img.resize((size, size), Image.LANCZOS)

    return bytes(data)


def expected_size(base_size):
    total = 0
    s = base_size
    while s >= 1:
        total += s * s * 3
        s //= 2
    return total


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('png',    help='Input PNG file')
    ap.add_argument('ress',   help='Path to .resS file')
    ap.add_argument('offset', type=int, help='Byte offset in .resS')
    ap.add_argument('--size', type=int, default=512, help='Texture size (default 512)')
    ap.add_argument('--flip-y', action='store_true',
                    help='Additional vertical flip (try if texture appears upside-down in-game)')
    args = ap.parse_args()

    exp = expected_size(args.size)
    print(f'[patch_ress] PNG      : {args.png}')
    print(f'[patch_ress] resS     : {args.ress}')
    print(f'[patch_ress] offset   : {args.offset}')
    print(f'[patch_ress] expected : {exp} bytes ({args.size}x{args.size} RGB24, 10 mips)')

    img = Image.open(args.png)
    if args.flip_y:
        img = img.transpose(Image.FLIP_TOP_BOTTOM)
    print(f'[patch_ress] source   : {img.size[0]}x{img.size[1]} {img.mode}')

    data = encode_rgb24_with_mipmaps(img, args.size)
    if len(data) != exp:
        print(f'ERROR: encoded {len(data)} bytes, expected {exp}', file=sys.stderr)
        sys.exit(1)

    ress = Path(args.ress)
    if not ress.exists():
        print(f'ERROR: {ress} not found', file=sys.stderr)
        sys.exit(1)

    ress_size = ress.stat().st_size
    if args.offset + exp > ress_size:
        print(f'ERROR: offset {args.offset} + {exp} exceeds file size {ress_size}', file=sys.stderr)
        sys.exit(1)

    with open(ress, 'r+b') as f:
        f.seek(args.offset)
        f.write(data)

    print(f'[patch_ress] Written {len(data)} bytes at offset {args.offset}')
    print(f'[patch_ress] Done.')


if __name__ == '__main__':
    main()
