#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""The block-linear (GOB) layout Frostpunk's texture blocks are stored in, and a BC3 codec for them.

The container's format-72 blocks are not linear: they are the bytes the GPU samples, in NVN's block-linear
layout with the block height the game's descriptor asks for (bh = 4 for this title).  A level's bytes are
gx GOB columns wide and whole blocks of 2^bh GOB rows high, and the block height shrinks for the small
levels.  This mirrors src/nx/gpu/texture/blocklinear.h and the bl_* helpers in src/nx/gpu/gpu.c and
src/nx/gpu/vk/texture.c: bl_level_size, bl_level_blocks, bl_offset.
"""

import struct

GOB_SIZE = 512
GOB_SIZE_X = 64
GOB_SIZE_Y = 8
GOB_RUN = 16


def gob_x_offset(x):
    return ((x & 32) << 3) | ((x & 16) << 1) | (x & 15)


def gob_y_offset(y):
    return (((y & 7) >> 1) << 6) | ((y & 1) << 4)


def bl_block_rows(height, bh):
    return (((height + GOB_SIZE_Y - 1) >> 3) + (1 << bh) - 1) >> bh


def bl_level_blocks(height, bh, level, bh_tex=4):
    """The block height (log2 GOBs) of a level: the hardware shrinks it for small levels."""
    lh = height >> level if height >> level else 1
    gy = ((lh + bh_tex - 1) // bh_tex + 7) // 8
    while bh > 0 and gy <= (1 << (bh - 1)):
        bh -= 1
    return bh


def level_bytes(width, height, bh, bw=4, bh_tex=4, bpp=16, tile_w=0):
    """The bytes one block-linear level of this size takes, given its own size and block height.

    The block height is the one the level is stored with, which for the small levels is not the texture's:
    `bl_level_blocks` says which, so a level's size never has to be re-derived from the level above it.
    """
    tw = (width + bw - 1) // bw
    th = (height + bh_tex - 1) // bh_tex
    lwb = tw * bpp
    gx = (lwb + GOB_SIZE_X - 1) // GOB_SIZE_X
    if tile_w and lwb >= 64 << tile_w and th >= 8 << bh:
        n = 1 << tile_w
        gx = (gx + n - 1) // n * n
    gy = (th + GOB_SIZE_Y - 1) // GOB_SIZE_Y
    gys = ((gy + (1 << bh) - 1) >> bh) << bh
    return gx * gys * GOB_SIZE


def bl_level_size(width, height, bh, level, bw=4, bh_tex=4, bpp=16, tile_w=0):
    """The bytes of one mip level of a 2D block-linear surface (texture.c's bl_level_size)."""
    lw = width >> level if width >> level else 1
    lh = height >> level if height >> level else 1
    return level_bytes(lw, lh, bl_level_blocks(height, bh, level, bh_tex), bw, bh_tex, bpp, tile_w)


def bl_offset(x_bytes, y, width_bytes, block_rows, bh):
    """A byte's offset in a 2D block-linear surface (gpu.c's bl_offset with z = 0).

    y counts block rows (4 texel rows each), as the port's bl_level_size counts them.
    """
    gobs_x = (width_bytes + GOB_SIZE_X - 1) >> 6
    gy = y >> 3
    rows = bl_block_rows(block_rows, bh)
    block = GOB_SIZE << bh
    in_block = (gy & ((1 << bh) - 1)) << 9
    return ((x_bytes >> 6) << (9 + bh)) + gob_x_offset(x_bytes) + ((gy >> bh) * gobs_x) * block + in_block + gob_y_offset(y)


def block_slots(width, height, bh):
    """(source block index, destination byte offset) for every 16-byte block of a level, in destination order."""
    tw = (width + 3) // 4
    th = (height + 3) // 4
    width_bytes = tw * GOB_RUN
    size = level_bytes(width, height, bh)
    out = []
    for by in range(th):
        for bx in range(tw):
            out.append(((by * tw + bx) * GOB_RUN, bl_offset(bx * GOB_RUN, by, width_bytes, th, bh), size))
    return out


def level_blocks(mips, bh, level, base_height=None):
    """The block height a level is stored with: the header's, shrunk for the small levels."""
    if base_height is None:
        return bh
    return bl_level_blocks(base_height, bh, level)


def unswizzle(data, width, height, bh):
    """The level's 16-byte blocks, in row-major block order, out of its block-linear bytes.

    `width` and `height` are the level's own size and `bh` the block height it is stored with, which
    `level_blocks(base_height, bh, level)` gives for a level of a mip chain.
    """
    tw = (width + 3) // 4
    th = (height + 3) // 4
    width_bytes = tw * GOB_RUN
    out = bytearray(tw * th * GOB_RUN)
    for by in range(th):
        for bx in range(tw):
            at = bl_offset(bx * GOB_RUN, by, width_bytes, th, bh)
            out[(by * tw + bx) * GOB_RUN:(by * tw + bx + 1) * GOB_RUN] = data[at:at + GOB_RUN]
    return bytes(out)


def swizzle(blocks, width, height, bh):
    """Block-linear bytes of a level from its 16-byte blocks in row-major block order.

    Takes the level's own size and block height, like `unswizzle`, so the buffer is exactly the level's
    bytes whatever level of a chain it is.
    """
    tw = (width + 3) // 4
    th = (height + 3) // 4
    width_bytes = tw * GOB_RUN
    out = bytearray(level_bytes(width, height, bh))
    for by in range(th):
        for bx in range(tw):
            at = bl_offset(bx * GOB_RUN, by, width_bytes, th, bh)
            out[at:at + GOB_RUN] = blocks[(by * tw + bx) * GOB_RUN:(by * tw + bx + 1) * GOB_RUN]
    return bytes(out)


def dds_bytes(pixels, width, height, fourcc=b"DXT5"):
    """A 128-byte DDS header around level-0 bytes, so Pillow can decode them."""
    if fourcc == b"DXT5":
        bpp, flags = 16, 0x4
    elif fourcc == b"DXT1":
        bpp, flags = 8, 0x4
    else:
        raise ValueError(fourcc)
    header = bytearray(128)
    header[0:4] = b"DDS "
    struct.pack_into("<7I", header, 4, 124, 0x1007, height, width, (width + 3) // 4 * bpp, 0, 1)
    struct.pack_into("<2I4s5I", header, 76, 32, flags, fourcc, 0, 0, 0, 0, 0)
    struct.pack_into("<5I", header, 108, 0x1000, 0, 0, 0, 0)
    return bytes(header) + pixels


def dxt5_decode(blocks, width, height):
    """RGBA bytes of a BC3 level, through Pillow's DDS reader."""
    from io import BytesIO

    from PIL import Image

    return Image.open(BytesIO(dds_bytes(blocks, width, height))).convert("RGBA").tobytes()


def dxt5_encode(rgba, width, height):
    """BC3 (DXT5) bytes of an RGBA image, one 16-byte block per 4x4 tile.

    Colour: the block's extremes as the two endpoints in four-colour mode; alpha: its extremes, eight
    values. Plain range fitting: no encoder to depend on, and fast enough to re-encode a package.
    """
    import numpy as np

    pixels = np.frombuffer(rgba, dtype=np.uint8).reshape(height, width, 4)
    th, tw = (height + 3) // 4, (width + 3) // 4
    padded = np.zeros((th * 4, tw * 4, 4), dtype=np.uint8)
    padded[:height, :width] = pixels
    tiles = padded.reshape(th, 4, tw, 4, 4).transpose(0, 2, 1, 3, 4).reshape(th * tw, 16, 4)

    colour_weights = (4 ** np.arange(16)).astype(np.uint32)
    alpha_weights = (8 ** np.arange(16)).astype(np.uint64)
    out = np.zeros((len(tiles), 16), dtype=np.uint8)
    # A chunk is as much as the per-block arrays below are built for at once.  They are several arrays of
    # chunk * 16 * 3 numbers, and a chunk of 32768 blocks of a 1024x1024 image is 1.5 MB of int16 that grows
    # to 25 MB of int32 in the distance step: on a machine whose memory is nearly full that allocation is what
    # the kernel spends its time reclaiming, and the encoder then takes minutes instead of a second.  Smaller
    # chunks keep the same output byte for byte and stay inside a cache.
    chunk = 4096
    for at in range(0, len(tiles), chunk):
        part = tiles[at:at + chunk]
        rgb = part[..., :3].astype(np.int16)
        alpha = part[..., 3].astype(np.int16)

        def q565(c):
            return (((c[:, 0] * 31 + 127) // 255) << 11 | ((c[:, 1] * 63 + 127) // 255) << 5 |
                    ((c[:, 2] * 31 + 127) // 255)).astype(np.uint32)

        def expand(c):
            return np.stack([((c >> 11) & 31) * 255 // 31, ((c >> 5) & 63) * 255 // 63,
                             (c & 31) * 255 // 31], axis=-1).astype(np.int16)

        c_hi = q565(rgb.max(axis=1).astype(np.int32))
        c_lo = q565(rgb.min(axis=1).astype(np.int32))
        c_hi = np.where(c_hi <= c_lo, np.minimum(c_lo + 1, 0xFFFF), c_hi).astype(np.uint32)
        a, b = expand(c_hi), expand(c_lo)
        ramp = np.stack([a, b, (2 * a + b) // 3, (a + 2 * b) // 3], axis=1).astype(np.int16)  # (n,4,3)
        dist = ((rgb[:, None, :, :] - ramp[:, :, None, :]).astype(np.int32) ** 2).sum(axis=-1)  # (n,4,16)
        bits = (dist.argmin(axis=1).astype(np.uint32) * colour_weights).sum(axis=1, dtype=np.uint32)

        a_lo = alpha.min(axis=1).astype(np.int32)
        a_hi = alpha.max(axis=1).astype(np.int32)
        swap = a_hi <= a_lo
        a_hi = np.where(swap, np.minimum(a_lo + 1, 255), a_hi)
        a_lo = np.where(swap, np.maximum(a_hi - 1, 0), a_lo)
        alphas = np.stack([a_hi, a_lo] + [((7 - k) * a_hi + k * a_lo) // 7 for k in range(1, 7)],
                          axis=1).astype(np.int32)  # (n,8)
        aidx = ((alpha[:, None, :] - alphas[:, :, None]).astype(np.int32) ** 2).argmin(axis=1)
        abits = (aidx.astype(np.uint64) * alpha_weights).sum(axis=1, dtype=np.uint64)

        n = len(part)
        block = out[at:at + n]
        block[:, 0] = a_hi.astype(np.uint8)
        block[:, 1] = a_lo.astype(np.uint8)
        block[:, 2:8] = abits.view(np.uint8).reshape(n, 8)[:, :6]
        colour = ((c_lo.astype(np.uint32) << 16) | c_hi).astype(np.uint32)
        block[:, 8:12] = colour.view(np.uint8).reshape(n, 4)
        block[:, 12:16] = bits.view(np.uint8).reshape(n, 4)
    return out.tobytes()


def resample(rgba, width, height, new_width, new_height):
    """Box-filter an RGBA image (Pillow when it is there, a plain 2x2 mean otherwise)."""
    from io import BytesIO

    from PIL import Image

    image = Image.frombytes("RGBA", (width, height), rgba)
    return image.resize((new_width, new_height), Image.BOX).tobytes()
