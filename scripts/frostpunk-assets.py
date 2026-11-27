#!/usr/bin/env python3
"""Read and rewrite a Frostpunk container set (`.dat` + `.idx` + `.str`).

The Switch build and the PC build ship the same container family: a `.dat`
holding every resource as one compressed block, an `.idx` of fixed 29-byte
records telling the runtime where each block is, and (Switch only) a `.str`
mapping a resource id to its name.  This tool reads and writes that pair so a
resource can be replaced with a different one and the game still loads it.

Layout, as measured on the Switch package (see docs/user/frostpunk-assets.md):

  .idx  header 11 bytes, then one 29-byte record per resource:
        +0  u32 resource id
        +4  u32 stored size in .dat
        +8  u32 zero
        +12 u32 payload size after decompression
        +16 u32 zero
        +20 u32 low 32 bits of the offset of the block in .dat
        +24 u32 high 32 bits of that offset
        +28 u8  flag, 1 on every resource seen so far

  .dat  the blocks, in offset order; a block is a zstd frame when it starts
        with 28 b5 2f fd, a gzip stream when it starts with 1f 8b, and stored
        raw otherwise, and decompresses to exactly the record's payload size.
        Some bytes between blocks are not owned by any record (leftovers of a
        patch); this tool reproduces them as-is and keeps the offsets of the
        blocks that follow.

The PC build writes the same two files without a `.str` and with a gzip block
for every resource, so the same reader takes both packages; `align` compares
them by id and `plan` writes the replacement list.  See
docs/dev/frostpunk-assets.md for the measured evidence.

  .str  header 11 bytes, then records of u32 id, u32 name length, name bytes.
"""
import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import zlib

try:
    import zstandard
except ImportError:  # pragma: no cover - the tests skip without it
    zstandard = None

IDX_MAGIC_SIZE = 11
RECORD_SIZE = 29
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
GZIP_MAGIC = b"\x1f\x8b\x08"
TEXTURE_MAGIC = b"\x65\x78\x1c\x00"
TEXTURE_HEADER_SIZE = 28
DESCRIPTOR_TAG = b"<TextureArrayDescriptor"
TGA_FOOTER = b"TRUEVISION-XFILE"
KINDS = ("texture", "pc-texture", "descriptor", "tga", "unknown")

# A PC image payload: 8 bytes of size, 4 bytes of format, 4 bytes of level
# count, 16 level slots of 8 bytes, then the pixel blocks.
PC_IMAGE_HEADER_SIZE = 144
PC_IMAGE_SLOTS = 16
PC_IMAGE_SLOT_SIZE = 8
PC_BLOCK_FORMATS = {b"DXT1": 8, b"DXT3": 16, b"DXT5": 16}
PC_RAW_BPP = (1, 2, 4, 8)
PC_MAX_DIM = 16384
PC_ARRAY_HEIGHT = 0xffffffff

# The Switch package holds ASTC 4x4 blocks after a 28-byte header, with no
# mip chain; a replacement costs exactly this much.
ASTC_BLOCK = 4
ASTC_HEADER_SIZE = 28
ASTC_BYTES_PER_BLOCK = 16

PLAN_TAG = "frostpunk-assets/replace-plan"
PLAN_VERSION = 1
PLAN_ENTRY_KEYS = ("id", "category", "name", "switch", "pc", "target",
                   "switch_payload", "switch_stored", "pc_payload",
                   "estimated", "delta")

# The categories the replacement plan can select, by resource name.  The first
# rule that matches decides; each category takes every channel map of the
# textures under it, which is what makes the detail maps follow their colour
# maps without a second list.
CATEGORIES = (
    ("terrain", ("environment/terrain/", "environment/builders/",
                 "environment/snow system/", "graphics/environment/",
                 "citydetail/")),
    ("buildings", ("buildings/",)),
    ("rocks", ("environment/rocks/", "environment/endless/")),
)
ROCK_WORD = "rock"
ROCK_SCOPE = "environment/"
CATEGORY_NAMES = tuple(name for name, _ in CATEGORIES)


def _u16(buf, off):
    return struct.unpack_from("<H", buf, off)[0]


def _u32(buf, off):
    return struct.unpack_from("<I", buf, off)[0]


def _p32(value):
    return struct.pack("<I", value)


class ContainerError(Exception):
    """A container that does not match the format, named precisely."""

    def __init__(self, where, why):
        self.where = where
        self.why = why
        super().__init__("%s: %s" % (where, why))


class Record:
    __slots__ = ("rid", "stored", "payload", "offset", "flag", "name")

    def __init__(self, rid, stored, payload, offset, flag):
        self.rid = rid
        self.stored = stored
        self.payload = payload
        self.offset = offset
        self.flag = flag
        self.name = None

    def __repr__(self):
        return "Record(0x%08x %s stored=%d payload=%d offset=%d)" % (
            self.rid, self.name or "?", self.stored, self.payload, self.offset)


class DatSet:
    """A `.dat`/`.idx`/`.str` set, read into memory record by record."""

    def __init__(self, dat_path, idx_path=None, str_path=None):
        self.dat_path = dat_path
        self.idx_path = idx_path or os.path.splitext(dat_path)[0] + ".idx"
        base = os.path.splitext(dat_path)[0]
        self.str_path = str_path or base + ".str"
        self.records = []
        self.names = {}
        self.header = b""
        self.gaps = []          # (offset, length) bytes no record owns
        self._by_id = None
        self.load()

    # -- reading ---------------------------------------------------------
    def load(self):
        if not os.path.exists(self.dat_path):
            raise ContainerError(self.dat_path, "no such data file")
        if not os.path.exists(self.idx_path):
            raise ContainerError(self.idx_path, "no such index file")
        with open(self.idx_path, "rb") as fh:
            idx = fh.read()
        if len(idx) < IDX_MAGIC_SIZE:
            raise ContainerError(self.idx_path,
                                 "shorter than the 11-byte header")
        body = len(idx) - IDX_MAGIC_SIZE
        if body % RECORD_SIZE:
            raise ContainerError(
                self.idx_path,
                "has %d bytes of records, which is not a multiple of %d"
                % (body, RECORD_SIZE))
        self.header = idx[:IDX_MAGIC_SIZE]
        self.records = []
        self._by_id = None
        for i in range(body // RECORD_SIZE):
            off = IDX_MAGIC_SIZE + i * RECORD_SIZE
            rec = idx[off:off + RECORD_SIZE]
            offset = _u32(rec, 20) | (_u32(rec, 24) << 32)
            self.records.append(Record(_u32(rec, 0), _u32(rec, 4),
                                       _u32(rec, 12), offset, rec[28]))
        if os.path.exists(self.str_path):
            self.names = load_str(self.str_path)
        for rec in self.records:
            rec.name = self.names.get(rec.rid)
        self._check_layout()

    def _check_layout(self):
        size = os.path.getsize(self.dat_path)
        spans = sorted((r.offset, r.stored, r) for r in self.records)
        end = 0
        self.gaps = []
        for offset, stored, rec in spans:
            if offset < end:
                raise ContainerError(
                    self.idx_path,
                    "record 0x%08x starts at %d, inside the block that ends at "
                    "%d" % (rec.rid, offset, end))
            if offset > end:
                self.gaps.append((end, offset - end))
            if offset + stored > size:
                raise ContainerError(
                    self.idx_path,
                    "record 0x%08x runs to %d but %s holds %d bytes"
                    % (rec.rid, offset + stored, self.dat_path, size))
            end = offset + stored
        if end < size:
            self.gaps.append((end, size - end))

    def record_map(self):
        """The records by id, for a caller that looks up many of them."""
        if self._by_id is None:
            self._by_id = {rec.rid: rec for rec in self.records}
        return self._by_id

    def by_id(self, rid):
        rec = self.record_map().get(rid)
        if rec is None:
            raise ContainerError(self.idx_path,
                                 "no record with id 0x%08x" % rid)
        return rec

    def by_name(self, name):
        for rec in self.records:
            if rec.name == name:
                return rec
        raise ContainerError(self.str_path or self.idx_path,
                             "no resource named %r" % name)

    def resolve(self, which):
        """A record from `name`, `0xID`, or a decimal id."""
        try:
            return self.by_name(which)
        except ContainerError:
            pass
        try:
            rid = int(which, 0)
        except ValueError:
            raise ContainerError(which, "is neither a resource name nor a number")
        return self.by_id(rid)

    def read_payload(self, rec):
        with open(self.dat_path, "rb") as fh:
            fh.seek(rec.offset)
            block = fh.read(rec.stored)
        if len(block) != rec.stored:
            raise ContainerError(
                self.dat_path,
                "holds %d bytes for record 0x%08x, the index says %d"
                % (len(block), rec.rid, rec.stored))
        if block[:4] == ZSTD_MAGIC:
            if zstandard is None:
                raise ContainerError(self.dat_path,
                                     "needs the zstandard module to read a "
                                     "compressed block")
            obj = zstandard.ZstdDecompressor().decompressobj()
            try:
                payload = obj.decompress(block)
            except zstandard.ZstdError as err:
                raise ContainerError(
                    self.dat_path,
                    "record 0x%08x does not decompress: %s" % (rec.rid, err))
        elif block[:3] == GZIP_MAGIC:
            obj = zlib.decompressobj(31)
            try:
                payload = obj.decompress(block)
            except zlib.error as err:
                raise ContainerError(
                    self.dat_path,
                    "record 0x%08x does not decompress: %s" % (rec.rid, err))
        else:
            payload = block
        if len(payload) != rec.payload:
            raise ContainerError(
                self.dat_path,
                "record 0x%08x yields %d bytes, the index says %d"
                % (rec.rid, len(payload), rec.payload))
        return payload

    # -- writing ---------------------------------------------------------
    def write(self, out_dir, replacements=None, compress_level=18, pad_to=None):
        """Write the set to `out_dir`, optionally replacing payloads.

        `replacements` maps a record to its new payload bytes.  Blocks are
        rewritten in their original order; a block that grows or shrinks moves
        the blocks after it, so the index is rebuilt with the new offsets and
        the leftover bytes of the original file are kept out.

        `pad_to` appends zero bytes until the `.dat` is that long, because a
        file shorter than the one it replaces is what the game refuses to read
        (see `cmd_apply_bc3`).  The index does not describe the padding, which
        is why a caller that pads measures the block bytes apart from the file
        size.
        """
        replacements = replacements or {}
        os.makedirs(out_dir, exist_ok=True)
        dat_path = os.path.join(out_dir, os.path.basename(self.dat_path))
        idx_path = os.path.join(out_dir, os.path.basename(self.idx_path))
        order = sorted(self.records, key=lambda r: r.offset)
        blob = bytearray()
        offsets = {}
        for rec in order:
            offsets[rec.rid] = len(blob)
            payload = replacements.get(rec.rid)
            if payload is None:
                with open(self.dat_path, "rb") as fh:
                    fh.seek(rec.offset)
                    blob += fh.read(rec.stored)
                continue
            block = _compress(payload, compress_level)
            rec.stored = len(block)
            rec.payload = len(payload)
            blob += block
        if pad_to is not None and len(blob) < pad_to:
            blob += bytes(pad_to - len(blob))
        with open(dat_path, "wb") as fh:
            fh.write(blob)
        out = bytearray(self.header)
        for rec in self.records:
            offset = offsets[rec.rid]
            out += _p32(rec.rid) + _p32(rec.stored) + _p32(0) + \
                _p32(rec.payload) + _p32(0) + _p32(offset & 0xffffffff) + \
                _p32(offset >> 32) + bytes([rec.flag])
        with open(idx_path, "wb") as fh:
            fh.write(out)
        if os.path.exists(self.str_path):
            out_str = os.path.join(out_dir, os.path.basename(self.str_path))
            with open(self.str_path, "rb") as src, open(out_str, "wb") as dst:
                dst.write(src.read())
        return dat_path, idx_path

    # -- reporting -------------------------------------------------------
    def describe(self):
        return {
            "dat": self.dat_path,
            "records": len(self.records),
            "names": len(self.names),
            "gaps": len(self.gaps),
            "gap_bytes": sum(length for _, length in self.gaps),
            "size": os.path.getsize(self.dat_path),
        }


def _compress(payload, level):
    if zstandard is None:
        raise ContainerError("payload", "needs the zstandard module to compress")
    return zstandard.ZstdCompressor(level=level).compress(payload)


def load_str(path):
    with open(path, "rb") as fh:
        blob = fh.read()
    names = {}
    pos = IDX_MAGIC_SIZE
    while pos + 8 <= len(blob):
        rid = _u32(blob, pos)
        length = _u32(blob, pos + 4)
        if pos + 8 + length > len(blob):
            raise ContainerError(
                path, "record 0x%08x names %d bytes past the end of the file"
                % (rid, length))
        names[rid] = blob[pos + 8:pos + 8 + length].decode("utf-8", "replace")
        pos += 8 + length
    if pos != len(blob):
        raise ContainerError(path, "%d trailing bytes after the last name"
                             % (len(blob) - pos))
    return names


class PcImage:
    """A PC image payload, described from its own bytes."""

    __slots__ = ("fmt", "width", "height", "levels", "slices", "bpp", "tag")

    def __init__(self, fmt, width, height, levels, slices, bpp, tag):
        self.fmt = fmt
        self.width = width
        self.height = height
        self.levels = levels
        self.slices = slices
        self.bpp = bpp
        self.tag = tag

    @property
    def label(self):
        return self.fmt if self.bpp is None else "raw%d" % self.bpp

    def dims(self):
        return [self.width, self.height]

    def __repr__(self):
        return "PcImage(%s %dx%d levels=%d slices=%d)" % (
            self.label, self.width, self.height, self.levels, self.slices)


def describe_pc_image(payload):
    """A PC texture payload, or `None` when its bytes do not prove one.

    A PC image payload is

      +0    u32 width
      +4    u32 height, or `0xffffffff` for a texture array or cube map
      +8    4-byte format: `DXT1`, `DXT3`, `DXT5`, or a numeric format id
      +12   u32 level count, 1 to 16
      +16   sixteen level slots of u16 width, u16 height, u32 bytes, the
            smallest level first
      +144  the pixel blocks of every level, one slice after another

    and its length is exactly `144 + sum(level bytes) * slices`.  A block
    format holds `ceil(w/4)*ceil(h/4)` blocks of 8 or 16 bytes; any other
    format holds raw pixels of 1, 2, 4 or 8 bytes each.  A shape that breaks
    one of these rules is not reported as an image: it is not guessed at.
    """
    if len(payload) < PC_IMAGE_HEADER_SIZE:
        return None
    width, height = struct.unpack_from("<II", payload, 0)
    count = _u32(payload, 12)
    if not 1 <= count <= PC_IMAGE_SLOTS:
        return None
    levels = [struct.unpack_from("<HHI", payload,
                                 16 + i * PC_IMAGE_SLOT_SIZE)
              for i in range(count)]
    total = sum(level[2] for level in levels)
    body = len(payload) - PC_IMAGE_HEADER_SIZE
    if total == 0 or body <= 0 or body % total:
        return None
    slices = body // total
    top_w, top_h = levels[-1][0], levels[-1][1]
    if not (1 <= top_w <= PC_MAX_DIM and 1 <= top_h <= PC_MAX_DIM):
        return None
    if height == PC_ARRAY_HEIGHT:
        width, height = top_w, top_h
    elif (width, height) != (top_w, top_h):
        return None
    tag = payload[8:12]
    if tag in PC_BLOCK_FORMATS:
        blocks = PC_BLOCK_FORMATS[tag]
        for level_w, level_h, nbytes in levels:
            want = ((level_w + 3) // 4) * ((level_h + 3) // 4) * blocks
            if nbytes != want:
                return None
        return PcImage(tag.decode("ascii").lower(), width, height, count,
                       slices, None, _u32(payload, 8))
    if levels[-1][2] % (top_w * top_h):
        return None
    bpp = levels[-1][2] // (top_w * top_h)
    if bpp not in PC_RAW_BPP:
        return None
    for level_w, level_h, nbytes in levels:
        if nbytes != level_w * level_h * bpp:
            return None
    return PcImage("raw", width, height, count, slices, bpp,
                   _u32(payload, 8))


def describe_payload(payload):
    """The block kind and, for a texture, its declared width and height.

    A kind is only reported when the bytes carry evidence for it:

      texture     the 28-byte header the Switch build writes, magic `0x001c7865`
                  at +0 and width/height as u16 at +12/+14;
      descriptor  a Stingray XML `<TextureArrayDescriptor>` document, which
                  starts with that root element;
      tga         an uncompressed TGA, whose 26-byte footer carries the
                  `TRUEVISION-XFILE` signature;
      pc-texture  the PC build's image payload, as `describe_pc_image` reads it;
      unknown     anything else.  A block with any other header is not guessed
                  at.

    Only texture blocks declare a size here; the other kinds return `None`.
    """
    if len(payload) >= TEXTURE_HEADER_SIZE and payload[:4] == TEXTURE_MAGIC:
        return "texture", _u16(payload, 12), _u16(payload, 14)
    if payload.startswith(DESCRIPTOR_TAG):
        return "descriptor", None, None
    if TGA_FOOTER in payload[-32:]:
        return "tga", None, None
    image = describe_pc_image(payload)
    if image is not None:
        return "pc-texture", image.width, image.height
    return "unknown", None, None


def astc_bytes(width, height):
    """Bytes a Switch texture payload of this size takes: header, no mips."""
    return ASTC_HEADER_SIZE + \
        ((width + ASTC_BLOCK - 1) // ASTC_BLOCK) * \
        ((height + ASTC_BLOCK - 1) // ASTC_BLOCK) * ASTC_BYTES_PER_BLOCK


def category_of(name):
    """The replacement category a resource name belongs to, `None` if none.

    The rules are matched in order and the first match decides, so a rock in
    `environment/builders/` is terrain: the folder is the stronger statement.
    A category takes every channel map of the textures under its folders
    (`_col`, `_alb`, `_nor`, `_nm`, `_pbr`, `_rgh`, `_hgt`, `_alp`, `_mask`),
    so a detail map follows its colour map with no second list.
    """
    if not name:
        return None
    for category, prefixes in CATEGORIES:
        if name.startswith(prefixes):
            return category
    if name.startswith(ROCK_SCOPE) and \
            ROCK_WORD in name.rsplit("/", 1)[-1].lower():
        return "rocks"
    return None


# -- alignment -------------------------------------------------------------


def side_summary(ds):
    """What one package holds, in numbers a report can print."""
    return {"dat": ds.dat_path,
            "records": len(ds.records),
            "dat_bytes": os.path.getsize(ds.dat_path),
            "idx_bytes": os.path.getsize(ds.idx_path),
            "gap_bytes": sum(length for _, length in ds.gaps),
            "names": len(ds.names)}


def block_group(name):
    """The name group a resource belongs to: its first path segment."""
    if not name:
        return "(unnamed)"
    return name.split("/", 1)[0]


def align_sets(switch, pc):
    """Compare two packages by resource id.

    The three counts are the ids present in both packages, only on Switch and
    only on PC.  For every group of names the report also counts the PC
    blocks and how many of them are readable: a block is readable when it
    decompresses to exactly the payload size its index entry declares, image
    when those bytes prove an image payload, and unreadable when the block
    does not come back at that size.  A block that is readable but not an
    image is listed as other; no block is called an image on assumption.

    Every PC block is read, so the call costs one pass over the PC package.
    """
    sw = {rec.rid: rec for rec in switch.records}
    pcs = {rec.rid: rec for rec in pc.records}
    both = sorted(set(sw) & set(pcs))
    switch_only = sorted(set(sw) - set(pcs))
    pc_only = sorted(set(pcs) - set(sw))

    groups = {}

    def group(name):
        key = block_group(name)
        slot = groups.get(key)
        if slot is None:
            slot = groups[key] = {
                "group": key, "switch": 0, "pc": 0, "both": 0,
                "switch_only": 0, "pc_only": 0, "pc_blocks": 0,
                "readable": 0, "image": 0, "other": 0, "unreadable": 0}
        return slot

    for rec in sw.values():
        group(rec.name)["switch"] += 1
    for rid, rec in pcs.items():
        slot = group((sw[rid] if rid in sw else rec).name)
        slot["pc"] += 1
        slot["pc_blocks"] += 1
    for rid in both:
        group(sw[rid].name)["both"] += 1
    for rid in switch_only:
        group(sw[rid].name)["switch_only"] += 1
    for rid in pc_only:
        group(None)["pc_only"] += 1

    kinds = {}
    non_power_of_two = 0
    images = 0
    pc_rows = []
    for rid in sorted(pcs):
        rec = pcs[rid]
        slot = group((sw[rid] if rid in sw else rec).name)
        row = {"id": rid, "name": sw[rid].name if rid in sw else None,
               "kind": None, "width": None, "height": None,
               "payload": rec.payload, "stored": rec.stored}
        try:
            payload = pc.read_payload(rec)
        except ContainerError as err:
            slot["unreadable"] += 1
            row["reason"] = err.why
            if rid not in sw:
                pc_rows.append(row)
            continue
        slot["readable"] += 1
        kind, width, height = describe_payload(payload)
        row.update(kind=kind, width=width, height=height)
        if kind == "pc-texture":
            image = describe_pc_image(payload)
            slot["image"] += 1
            images += 1
            kinds[image.label] = kinds.get(image.label, 0) + 1
            row["label"] = image.label
            if image.width & (image.width - 1) or \
                    image.height & (image.height - 1):
                non_power_of_two += 1
        else:
            slot["other"] += 1
            kinds[kind] = kinds.get(kind, 0) + 1
        if rid not in sw:
            pc_rows.append(row)

    only_switch = []
    for rid in switch_only:
        rec = sw[rid]
        row = {"id": rid, "name": rec.name, "kind": "unknown",
               "width": None, "height": None, "payload": rec.payload,
               "stored": rec.stored}
        try:
            row["kind"], row["width"], row["height"] = \
                describe_payload(switch.read_payload(rec))
        except ContainerError as err:
            row["reason"] = err.why
        only_switch.append(row)

    rows = sorted(groups.values(), key=lambda r: (-r["pc"], r["group"]))
    return {"switch": side_summary(switch), "pc": side_summary(pc),
            "both": len(both), "switch_only": len(switch_only),
            "pc_only": len(pc_only), "groups": rows,
            "kinds": sorted(kinds.items(), key=lambda kv: (-kv[1], kv[0])),
            "images": images, "non_power_of_two": non_power_of_two,
            "only_switch": only_switch, "only_pc": pc_rows}


def _size_cell(row):
    """`dxt5 1024x1024` or a kind, for the lists at the end of the report."""
    if row.get("width"):
        return "%s %dx%d" % (row.get("label") or row["kind"], row["width"],
                             row["height"])
    return row["kind"] or "?"


def render_align(report, fmt):
    """The alignment report as text; `fmt` is `table` or `json`."""
    if fmt == "json":
        return json.dumps(report, indent=2, sort_keys=False) + "\n"
    out = []
    out.append("switch  %s" % report["switch"]["dat"])
    out.append("        %s records, %s bytes, %s names"
               % (format(report["switch"]["records"], ","),
                  format(report["switch"]["dat_bytes"], ","),
                  format(report["switch"]["names"], ",")))
    out.append("pc      %s" % report["pc"]["dat"])
    out.append("        %s records, %s bytes, %s names"
               % (format(report["pc"]["records"], ","),
                  format(report["pc"]["dat_bytes"], ","),
                  format(report["pc"]["names"], ",")))
    out.append("")
    out.append("alignment by id: %s in both, %s only on Switch, "
               "%s only on PC"
               % (format(report["both"], ","),
                  format(report["switch_only"], ","),
                  format(report["pc_only"], ",")))
    out.append("")
    out.append("%-26s %6s %6s %8s %8s %9s %6s %6s %10s"
               % ("group", "pc", "both", "sw-only", "pc-only", "readable",
                  "image", "other", "unreadable"))
    totals = {"pc": 0, "both": 0, "switch_only": 0, "pc_only": 0,
              "readable": 0, "image": 0, "other": 0, "unreadable": 0}
    for row in report["groups"]:
        out.append("%-26s %6d %6d %8d %8d %9d %6d %6d %10d"
                   % (row["group"], row["pc"], row["both"],
                      row["switch_only"], row["pc_only"], row["readable"],
                      row["image"], row["other"], row["unreadable"]))
        for key in totals:
            totals[key] += row[key]
    out.append("%-26s %6d %6d %8d %8d %9d %6d %6d %10d"
               % ("(total)", totals["pc"], totals["both"],
                  totals["switch_only"], totals["pc_only"], totals["readable"],
                  totals["image"], totals["other"], totals["unreadable"]))
    out.append("")
    out.append("pc blocks by kind: %s" % (
        ", ".join("%s %s" % (kind, format(count, ","))
                  for kind, count in report["kinds"]) or "none"))
    out.append("images: %s, %s of them not a power of two on both sides"
               % (format(report["images"], ","),
                  format(report["non_power_of_two"], ",")))
    out.append("")
    out.append("only on Switch, left alone (%s):"
               % format(report["switch_only"], ","))
    for row in report["only_switch"]:
        out.append("  0x%08x  %-18s %10s bytes  %s"
                   % (row["id"], _size_cell(row),
                      format(row["payload"], ","), row["name"]))
    out.append("only on PC, left alone (%s):"
               % format(report["pc_only"], ","))
    for row in report["only_pc"]:
        out.append("  0x%08x  %-18s %10s bytes  %s"
                   % (row["id"], _size_cell(row),
                      format(row["payload"], ","),
                      row["name"] or "(no name)"))
    return "\n".join(out) + "\n"


def cmd_align(args):
    switch = DatSet(args.switch)
    pc = DatSet(args.pc)
    text = render_align(align_sets(switch, pc), args.format)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
        print("align: wrote %s (%d bytes)" % (args.out, len(text)),
              file=sys.stderr)
    else:
        sys.stdout.write(text)
    return 0


def cmd_info(args):
    ds = DatSet(args.dat)
    info = ds.describe()
    for key, value in info.items():
        print("%-10s %s" % (key, value))
    return 0


def cmd_list(args):
    ds = DatSet(args.dat)
    rows = sorted(ds.records, key=lambda r: r.offset)
    shown = 0
    for rec in rows:
        if args.name and args.name not in (rec.name or ""):
            continue
        if args.min_payload and rec.payload < args.min_payload:
            continue
        print("%08x %10d %10d %s" % (rec.rid, rec.offset, rec.payload,
                                     rec.name or "-"))
        shown += 1
        if args.limit and shown >= args.limit:
            break
    return 0


def cmd_inventory(args):
    """Every resource with its block kind, declared size, offset and payload.

    The rows are filtered before a block is read, so a name or payload-size
    filter does not pay for decompressing the whole package.
    """
    ds = DatSet(args.dat)
    rows = []
    for rec in ds.records:
        if args.name and args.name not in (rec.name or ""):
            continue
        if args.prefix and not (rec.name or "").startswith(args.prefix):
            continue
        if rec.payload < args.min_payload:
            continue
        if args.max_payload is not None and rec.payload > args.max_payload:
            continue
        payload = ds.read_payload(rec)
        kind, width, height = describe_payload(payload)
        if args.kind and kind not in args.kind:
            continue
        if width is not None:
            if args.min_width is not None and width < args.min_width:
                continue
            if args.max_width is not None and width > args.max_width:
                continue
            if args.min_height is not None and height < args.min_height:
                continue
            if args.max_height is not None and height > args.max_height:
                continue
        elif (args.min_width is not None or args.max_width is not None or
              args.min_height is not None or args.max_height is not None):
            continue
        rows.append({
            "id": rec.rid,
            "id_hex": "0x%08x" % rec.rid,
            "name": rec.name,
            "kind": kind,
            "width": width,
            "height": height,
            "offset": rec.offset,
            "payload": rec.payload,
            "stored": rec.stored,
        })

    keys = {
        "offset": lambda r: r["offset"],
        "id": lambda r: r["id"],
        "name": lambda r: r["name"] or "",
        "size": lambda r: r["payload"],
        "stored": lambda r: r["stored"],
    }
    rows.sort(key=keys[args.sort], reverse=args.reverse)

    matched = len(rows)
    if args.limit:
        rows = rows[:args.limit]
    text = render_inventory(rows, args.format)

    if args.out:
        with open(args.out, "w") as fh:
            fh.write(text)
        print("wrote %s (%d rows)" % (args.out, len(rows)))
    else:
        sys.stdout.write(text)

    named = sum(1 for r in rows if r["name"] is not None)
    unnamed = len(rows) - named
    counts = {}
    for row in rows:
        counts[row["kind"]] = counts.get(row["kind"], 0) + 1
    kinds = ", ".join("%s=%d" % (k, counts[k]) for k in sorted(counts)) \
        or "none"
    dims = sum(1 for r in rows if r["width"] is not None)
    print("inventory: %d rows (%d named, %d unnamed); kinds %s; "
          "%d with width/height" % (len(rows), named, unnamed, kinds, dims),
          file=sys.stderr)
    if args.limit and matched > len(rows):
        print("inventory: showing %d of %d matching rows"
              % (len(rows), matched), file=sys.stderr)
    return 0


def render_inventory(rows, fmt):
    if fmt == "json":
        return json.dumps(rows, indent=1) + "\n"
    if fmt == "tsv":
        lines = ["id\tname\tkind\twidth\theight\toffset\tpayload\tstored"]
        for row in rows:
            lines.append("\t".join([
                row["id_hex"],
                row["name"] if row["name"] is not None else "",
                row["kind"],
                "" if row["width"] is None else str(row["width"]),
                "" if row["height"] is None else str(row["height"]),
                str(row["offset"]),
                str(row["payload"]),
                str(row["stored"]),
            ]))
        return "\n".join(lines) + "\n"
    lines = ["%-10s %-10s %5s %5s %12s %10s %9s  %s"
             % ("id", "kind", "width", "height", "offset", "payload",
                "stored", "name")]
    for row in rows:
        lines.append("%-10s %-10s %5s %5s %12d %10d %9d  %s"
                     % (row["id_hex"], row["kind"],
                        "-" if row["width"] is None else row["width"],
                        "-" if row["height"] is None else row["height"],
                        row["offset"], row["payload"], row["stored"],
                        row["name"] if row["name"] is not None else "-"))
    return "\n".join(lines) + "\n"


# -- replacement plan ------------------------------------------------------


class BudgetError(ContainerError):
    """A plan whose projected package is larger than the budget."""

    exit_code = 3

    def __init__(self, over, budget, projected):
        self.over = over
        self.budget = budget
        self.projected = projected
        super().__init__(
            "plan",
            "over budget by %s bytes: the replacement package would be %s "
            "bytes and the budget is %s bytes; nothing written.  Raise "
            "--budget, narrow --categories, or lower --target-size."
            % (format(over, ","), format(projected, ","),
               format(budget, ",")))


def parse_bytes(text):
    """A byte count, as digits or with a `K`, `M`, `G` or `T` suffix."""
    value = str(text).strip().lower()
    if not value:
        raise ContainerError("--budget", "is empty")
    scale = 1
    for suffix, factor in (("k", 1024), ("m", 1024 ** 2), ("g", 1024 ** 3),
                           ("t", 1024 ** 4)):
        if value.endswith(suffix + "b") or value.endswith(suffix + "ib"):
            value, scale = value[:-2], factor
            break
        if value.endswith(suffix):
            value, scale = value[:-1], factor
            break
    try:
        number = int(float(value) * scale)
    except ValueError:
        raise ContainerError("--budget",
                             "%r is not a byte count" % text)
    if number < 0:
        raise ContainerError("--budget", "%r is negative" % text)
    return number


def parse_target_size(text):
    """A target size policy: `Nx` scales the Switch size, `N` caps a side."""
    value = str(text or "2x").strip().lower()
    if value.endswith("x"):
        factor = value[:-1]
        if not factor.isdigit() or int(factor) < 1:
            raise ContainerError("--target-size",
                                 "%r is not a scale like 2x" % text)
        return ("scale", int(factor))
    if not value.isdigit() or int(value) < 1:
        raise ContainerError(
            "--target-size",
            "%r is neither a scale like 2x nor a pixel count" % text)
    return ("cap", int(value))


def target_dims(policy, max_size, switch_size, pc_size):
    """The size a replacement is made at, or `None` and the reason.

    The policy scales the size the Switch package declares, or caps its
    longest side.  `max_size` then caps each side, and the PC source caps it
    once more: a replacement never asks for more pixels than the PC source
    actually holds.  A result smaller than the Switch texture on either side
    is refused, so nothing is ever downgraded to fit a limit.

    Nothing is rounded to a power of two.  A 1010x534 source keeps those
    sides and the ASTC cost rounds up per 4x4 block instead, which is why a
    non-power-of-two texture is a normal case and not an error.
    """
    mode, value = policy
    width, height = switch_size
    if mode == "scale":
        width, height = width * value, height * value
    else:
        longest = max(width, height)
        if longest > value:
            width = max(1, (width * value) // longest)
            height = max(1, (height * value) // longest)
    width = min(width, pc_size[0], max_size)
    height = min(height, pc_size[1], max_size)
    if width < switch_size[0] or height < switch_size[1]:
        return None, ("would be %dx%d, smaller than the Switch texture %dx%d"
                      % (width, height, switch_size[0], switch_size[1]))
    return (width, height), None


def chosen_category(name, categories, include, exclude):
    """The category a selected name belongs to, `None` when it is not picked.

    `--exclude` wins over every other rule, so a folder can be carved out
    even when `--include` names it.  `--include` adds a name whose folder the
    category table does not cover.  A picked name that matches none of these
    is not replaced.
    """
    if not name:
        return None
    for prefix in exclude:
        if name.startswith(prefix):
            return None
    for prefix in include:
        if name.startswith(prefix):
            return "included"
    category = category_of(name)
    if category is not None and category in categories:
        return category
    return None


def plan_header(switch, pc, categories, target_size, max_size, include,
                exclude, budget):
    """The first line of a plan file: what it was made from and how."""
    return {"plan": PLAN_TAG, "version": PLAN_VERSION,
            "switch": switch.dat_path, "pc": pc.dat_path,
            "switch_records": len(switch.records),
            "pc_records": len(pc.records),
            "switch_dat_bytes": os.path.getsize(switch.dat_path),
            "switch_gap_bytes": sum(length for _, length in switch.gaps),
            "target_size": target_size, "max_size": max_size,
            "categories": list(categories), "include": list(include),
            "exclude": list(exclude), "budget": budget,
            "astc": {"block": ASTC_BLOCK, "header": ASTC_HEADER_SIZE,
                     "bytes_per_block": ASTC_BYTES_PER_BLOCK}}


def build_plan(switch, pc, categories, policy, max_size, include, exclude):
    """The replacement entries for the selected categories, and the skips.

    An entry is only made for an id that both packages hold, whose Switch
    block is a texture, whose PC block proves an image payload, and whose
    target size is not smaller than the Switch texture.  Everything else that
    the whitelist selected is returned as a skip with the reason, so a plan
    never hides a resource it decided not to touch.
    """
    sw = {rec.rid: rec for rec in switch.records}
    pcs = {rec.rid: rec for rec in pc.records}
    entries = []
    skipped = []
    for rid in sorted(set(sw) & set(pcs)):
        srec, prec = sw[rid], pcs[rid]
        name = srec.name
        category = chosen_category(name, categories, include, exclude)
        if category is None:
            continue
        if not name:
            skipped.append({"id": rid, "name": None, "reason": "no name"})
            continue
        kind, switch_w, switch_h = describe_payload(switch.read_payload(srec))
        if kind != "texture":
            skipped.append({"id": rid, "name": name,
                            "reason": "the Switch block is %s, not a texture"
                                      % kind})
            continue
        try:
            pc_payload = pc.read_payload(prec)
        except ContainerError as err:
            skipped.append({"id": rid, "name": name,
                            "reason": "the PC block is unreadable: %s"
                                      % err.why})
            continue
        image = describe_pc_image(pc_payload)
        if image is None:
            skipped.append({"id": rid, "name": name,
                            "reason": "the PC block does not prove an image "
                                      "payload"})
            continue
        target, reason = target_dims(policy, max_size, (switch_w, switch_h),
                                     (image.width, image.height))
        if target is None:
            skipped.append({"id": rid, "name": name, "reason": reason})
            continue
        estimated = astc_bytes(target[0], target[1])
        entries.append({"id": "0x%08x" % rid, "category": category,
                        "name": name,
                        "switch": [switch_w, switch_h],
                        "pc": [image.width, image.height],
                        "target": [target[0], target[1]],
                        "switch_payload": srec.payload,
                        "switch_stored": srec.stored,
                        "pc_payload": prec.payload,
                        "estimated": estimated,
                        "delta": estimated - srec.stored})
    return entries, skipped


def write_plan(path, header, entries):
    """Write a plan as one JSON object per line, entries in a fixed order."""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(header, ensure_ascii=True,
                            separators=(",", ":")) + "\n")
        for entry in entries:
            line = {key: entry[key] for key in PLAN_ENTRY_KEYS}
            fh.write(json.dumps(line, ensure_ascii=True,
                                separators=(",", ":")) + "\n")


def read_plan(path):
    """Read a plan back into its header and entries.

    Blank lines and lines starting with `#` are skipped, so a person can add
    a note; deleting an entry line removes it from the plan, and every total
    is recomputed from the lines that are left.
    """
    header = None
    entries = []
    with open(path, "r", encoding="utf-8") as fh:
        for number, line in enumerate(fh, 1):
            text = line.strip()
            if not text or text.startswith("#"):
                continue
            try:
                obj = json.loads(text)
            except ValueError as err:
                raise ContainerError(path, "line %d is not JSON: %s"
                                     % (number, err))
            if not isinstance(obj, dict):
                raise ContainerError(path, "line %d is not an object"
                                     % number)
            if header is None:
                if obj.get("plan") != PLAN_TAG:
                    raise ContainerError(path, "line %d is not a %s header"
                                         % (number, PLAN_TAG))
                if obj.get("version") != PLAN_VERSION:
                    raise ContainerError(
                        path, "line %d says version %r, this tool writes %d"
                        % (number, obj.get("version"), PLAN_VERSION))
                for key in ("switch_dat_bytes", "switch_gap_bytes"):
                    if not isinstance(obj.get(key), int):
                        raise ContainerError(path, "line %d has no %s"
                                             % (number, key))
                header = obj
                continue
            missing = [key for key in PLAN_ENTRY_KEYS if key not in obj]
            if missing:
                raise ContainerError(path, "line %d is missing %s"
                                     % (number, ", ".join(missing)))
            for key in ("switch_payload", "switch_stored", "pc_payload",
                        "estimated", "delta"):
                if not isinstance(obj[key], int):
                    raise ContainerError(path, "line %d: %s is not an integer"
                                         % (number, key))
            entries.append(obj)
    if header is None:
        raise ContainerError(path, "holds no plan header")
    return header, entries


def plan_summary(header, entries, budget=None):
    """Recompute what a plan would do, from the lines it is made of.

    The projected package is the Switch `.dat` size, less the bytes no record
    owns (the writer keeps them out), plus the growth of every entry.  The
    estimate uses the ASTC cost of the replacement and not the size the PC
    payload happens to have, because the payload is decoded and re-encoded
    before it is stored.
    """
    if budget is None:
        budget = header.get("budget")
    grown = sum(entry["delta"] for entry in entries)
    base = header["switch_dat_bytes"] - header["switch_gap_bytes"]
    projected = base + grown
    summary = {
        "switch": header["switch"], "pc": header["pc"],
        "target_size": header.get("target_size"),
        "max_size": header.get("max_size"),
        "categories": header.get("categories"),
        "astc": header.get("astc"),
        "count": len(entries),
        "switch_payload": sum(entry["switch_payload"] for entry in entries),
        "estimated": sum(entry["estimated"] for entry in entries),
        "growth": grown,
        "switch_dat_bytes": header["switch_dat_bytes"],
        "switch_gap_bytes": header["switch_gap_bytes"],
        "projected": projected, "budget": budget,
        "largest": sorted(entries, key=lambda e: (-e["estimated"], e["id"]))[:5],
    }
    if budget is not None:
        summary["over"] = projected - budget
    return summary


def render_plan(summary, fmt):
    """The plan summary as text; `fmt` is `table` or `json`."""
    if fmt == "json":
        return json.dumps(summary, indent=2, sort_keys=False) + "\n"
    out = []
    out.append("switch    %s" % summary["switch"])
    out.append("pc        %s" % summary["pc"])
    out.append("policy    target %s, max %s, ASTC %dx%d block, %d-byte header"
               % (summary["target_size"], format(summary["max_size"], ","),
                  summary["astc"]["block"], summary["astc"]["block"],
                  summary["astc"]["header"]))
    out.append("categories %s" % ", ".join(summary["categories"] or []))
    out.append("entries   %s replaced"
               % format(summary["count"], ","))
    out.append("switch payload %s bytes, replacement %s bytes, growth %s bytes"
               % (format(summary["switch_payload"], ","),
                  format(summary["estimated"], ","),
                  format(summary["growth"], ",")))
    if summary["budget"] is None:
        out.append("projected package %s bytes (no budget)"
                   % format(summary["projected"], ","))
    elif summary["over"] > 0:
        out.append("projected package %s bytes, over budget by %s bytes"
                   % (format(summary["projected"], ","),
                      format(summary["over"], ",")))
    else:
        out.append("projected package %s bytes, %s bytes under the budget "
                   "of %s"
                   % (format(summary["projected"], ","),
                      format(-summary["over"], ","),
                      format(summary["budget"], ",")))
    out.append("largest entries:")
    for entry in summary["largest"]:
        out.append("  %s %-12s %-46s %4dx%-4d %12s %+13s"
                   % (entry["id"], entry["category"], entry["name"],
                      entry["target"][0], entry["target"][1],
                      format(entry["estimated"], ","),
                      format(entry["delta"], ",")))
    return "\n".join(out) + "\n"


def cmd_plan(args):
    budget = parse_bytes(args.budget) if args.budget is not None else None
    if args.read:
        header, entries = read_plan(args.read)
        summary = plan_summary(header, entries,
                               budget if budget is not None
                               else header.get("budget"))
        if summary.get("over", 0) > 0:
            raise BudgetError(summary["over"], summary["budget"],
                              summary["projected"])
        sys.stdout.write(render_plan(summary, args.format))
        return 0
    if not (args.switch and args.pc):
        raise ContainerError("plan", "needs --switch and --pc to make a plan")
    if not args.out:
        raise ContainerError("plan", "needs --out to write the plan")
    categories = parse_categories(args.categories)
    policy = parse_target_size(args.target_size)
    switch = DatSet(args.switch)
    pc = DatSet(args.pc)
    header = plan_header(switch, pc, categories, args.target_size,
                         args.max_size, args.include, args.exclude,
                         budget)
    entries, skipped = build_plan(switch, pc, categories, policy,
                                  args.max_size, args.include, args.exclude)
    summary = plan_summary(header, entries, budget)
    if summary.get("over", 0) > 0:
        raise BudgetError(summary["over"], summary["budget"],
                          summary["projected"])
    write_plan(args.out, header, entries)
    sys.stdout.write(render_plan(summary, args.format))
    if skipped:
        reasons = {}
        for row in skipped:
            reasons[row["reason"]] = reasons.get(row["reason"], 0) + 1
        for reason in sorted(reasons):
            print("skipped %s: %s" % (format(reasons[reason], ","), reason),
                  file=sys.stderr)
    print("plan: wrote %s (%s entries)"
          % (args.out, format(len(entries), ",")), file=sys.stderr)
    return 0


def parse_categories(text):
    """The selected categories, from a comma list or `all`."""
    if not text:
        return list(CATEGORY_NAMES)
    names = [name.strip() for name in text.split(",") if name.strip()]
    if names == ["all"]:
        return list(CATEGORY_NAMES)
    unknown = [name for name in names if name not in CATEGORY_NAMES]
    if unknown:
        raise ContainerError(
            "--categories",
            "%s is not one of %s" % (", ".join(unknown),
                                     ", ".join(CATEGORY_NAMES)))
    return names


# -- applying a plan -----------------------------------------------------
#
# A Switch texture block is a 28-byte header and the pixels it describes.  The
# header, read from all 4172 texture blocks of the shipped package, is
#
#   +0   magic `0x001c7865`
#   +4   u32 format id: 72 ASTC 4x4, 37 RGBA8, 1 R8, 5 R16
#   +8   u32 1
#   +12  u16 width, +14 u16 height
#   +16  u16 1, +18 u8 1
#   +19  u8 mip level count
#   +20  4-byte format descriptor, `02 03 04 05` for ASTC 4x4
#   +24  u32 pixel bytes, every mip, which is the payload length less 28
#
# The last field holds on every block in the package and the level count
# matches the bytes the mips take, so a block written with one level has to
# say so in both fields: a header that kept `10` levels and the old byte count
# would send the loader past the end of the pixels it was given.  `apply`
# therefore writes level 0 and the two fields that describe the pixels, and
# changes no other header byte.
SWITCH_FORMAT_ASTC_4X4 = 72
SWITCH_FORMAT_RGBA8 = 37
SWITCH_FORMAT_R8 = 1
SWITCH_FORMAT_R16 = 5
ASTC_FORMAT_DESCRIPTOR = b"\x02\x03\x04\x05"
RGBA8_FORMAT_DESCRIPTOR = b"\x04\x03\x02\x05"
QUALITY_PRESETS = ("fastest", "fast", "medium", "thorough",
                   "verythorough", "exhaustive")
QUALITY_SCORES = {"fastest": 0, "fast": 10, "medium": 60, "thorough": 98,
                  "verythorough": 99, "exhaustive": 100}
DEFAULT_QUALITY = "medium"
SWITCH_FORMAT_LABELS = {SWITCH_FORMAT_ASTC_4X4: "astc4x4",
                        SWITCH_FORMAT_RGBA8: "rgba8",
                        SWITCH_FORMAT_R8: "r8",
                        SWITCH_FORMAT_R16: "r16"}


class SwitchTexture:
    """The header of a Switch texture block, and how many bytes its mips take."""

    __slots__ = ("width", "height", "format", "mips", "pixel_bytes",
                 "descriptor")

    def __init__(self, width, height, fmt, mips, pixel_bytes, descriptor):
        self.width = width
        self.height = height
        self.format = fmt
        self.mips = mips
        self.pixel_bytes = pixel_bytes
        self.descriptor = descriptor

    @property
    def label(self):
        return SWITCH_FORMAT_LABELS.get(self.format,
                                        "format-%d" % self.format)

    @property
    def level0_bytes(self):
        """Bytes an ASTC 4x4 full-size level of this texture takes."""
        return _astc_pixels(self.width, self.height)


def _astc_pixels(width, height):
    blocks = ((width + ASTC_BLOCK - 1) // ASTC_BLOCK) * \
        ((height + ASTC_BLOCK - 1) // ASTC_BLOCK)
    return blocks * ASTC_BYTES_PER_BLOCK


def switch_texture(payload):
    """The Switch texture header of `payload`, or `None` when it is not one."""
    if len(payload) < TEXTURE_HEADER_SIZE or payload[:4] != TEXTURE_MAGIC:
        return None
    width, height = _u16(payload, 12), _u16(payload, 14)
    if width < 1 or height < 1:
        return None
    return SwitchTexture(width, height, _u32(payload, 4), payload[19],
                         _u32(payload, 24), bytes(payload[20:24]))


_RGB565 = None


def _rgb565():
    """A 65536-entry table of the three bytes a 5-6-5 colour expands to."""
    global _RGB565
    if _RGB565 is None:
        table = bytearray(65536 * 3)
        for value in range(65536):
            red = (value >> 11) & 0x1f
            green = (value >> 5) & 0x3f
            blue = value & 0x1f
            table[3 * value] = (red << 3) | (red >> 2)
            table[3 * value + 1] = (green << 2) | (green >> 4)
            table[3 * value + 2] = (blue << 3) | (blue >> 2)
        _RGB565 = table
    return _RGB565


def _dxt1_colours(block, table, four_colour):
    """The four RGBA colours of a DXT1/DXT3/DXT5 colour block."""
    first = block[0] | (block[1] << 8)
    second = block[2] | (block[3] << 8)
    bits = block[4] | (block[5] << 8) | (block[6] << 16) | (block[7] << 24)
    r0, g0, b0 = table[3 * first:3 * first + 3]
    r1, g1, b1 = table[3 * second:3 * second + 3]
    colours = [(r0, g0, b0, 255), (r1, g1, b1, 255)]
    if first > second or four_colour:
        colours.append(((2 * r0 + r1) // 3, (2 * g0 + g1) // 3,
                        (2 * b0 + b1) // 3, 255))
        colours.append(((r0 + 2 * r1) // 3, (g0 + 2 * g1) // 3,
                        (b0 + 2 * b1) // 3, 255))
    else:
        colours.append(((r0 + r1) // 2, (g0 + g1) // 2, (b0 + b1) // 2, 255))
        colours.append((0, 0, 0, 0))
    return colours, bits


def _dxt5_alphas(block):
    """The eight alpha values of a DXT5 alpha block."""
    first, second = block[0], block[1]
    if first > second:
        return [first, second] + \
            [((7 - i) * first + i * second) // 7 for i in range(1, 7)]
    return [first, second] + \
        [((5 - i) * first + i * second) // 5 for i in range(1, 5)] + [0, 255]


def _pillow_image():
    """Pillow's Image module, or None when it is not installed."""
    try:
        from PIL import Image
    except Exception:
        return None
    return Image


def _dds_bytes(pixels, width, height, kind):
    """Wrap BC1/BC2/BC3 level 0 pixels in a DDS header, for Pillow to read."""
    block = {"dxt1": (b"DXT1", 8), "dxt3": (b"DXT3", 16),
             "dxt5": (b"DXT5", 16)}[kind]
    fourcc, block_bytes = block
    linear = max(1, (width + 3) // 4) * max(1, (height + 3) // 4) * block_bytes
    header = b"DDS " + struct.pack("<7I", 124, 0x1007, height, width,
                                   linear, 0, 1)
    header += struct.pack("<11I", *([0] * 11))
    header += struct.pack("<2I4s5I", 32, 0x4, fourcc, 0, 0, 0, 0, 0)
    header += struct.pack("<5I", 0x1000, 0, 0, 0, 0)
    return header + pixels


def _decode_dxt(payload, width, height, kind):
    """Decode DXT1, DXT3 or DXT5 blocks to RGBA8.

    Blocks are laid out left to right and top to bottom, and a block that
    hangs over the edge of the image is cut off rather than wrapped.  This is
    the plain decoder of the format, in pure Python, with the 565 table built
    once: the tool has to read these textures on a machine that may not have
    anything else installed.  A block inside the image is written as four
    rows of packed pixels, which is much faster than 16 pixel stores.
    """
    Image = _pillow_image()
    if Image is not None:
        try:
            from io import BytesIO
            picture = Image.open(BytesIO(_dds_bytes(payload, width, height,
                                                    kind)))
            return bytearray(picture.convert("RGBA").tobytes())
        except Exception:
            pass
    table = _rgb565()
    block_bytes = 8 if kind == "dxt1" else 16
    width_blocks = (width + 3) // 4
    height_blocks = (height + 3) // 4
    want = width_blocks * height_blocks * block_bytes
    if len(payload) < want:
        raise ContainerError(
            "pixels", "%s needs %d bytes for %dx%d, the payload holds %d"
            % (kind, want, width, height, len(payload)))
    out = bytearray(width * height * 4)
    stride = width * 4
    offset = 0
    pack = struct.pack
    for block_y in range(height_blocks):
        top = block_y * 4
        rows = min(4, height - top)
        for block_x in range(width_blocks):
            block = payload[offset:offset + block_bytes]
            offset += block_bytes
            left = block_x * 4
            columns = min(4, width - left)
            if kind == "dxt3":
                alpha_bits = int.from_bytes(block[0:8], "little")
                colours, bits = _dxt1_colours(block[8:16], table, True)
                alphas = None
            elif kind == "dxt5":
                alphas = _dxt5_alphas(block)
                alpha_bits = int.from_bytes(block[2:8], "little")
                colours, bits = _dxt1_colours(block[8:16], table, True)
            else:
                alphas = None
                alpha_bits = 0
                colours, bits = _dxt1_colours(block, table, False)
            if rows == 4 and columns == 4:
                base = top * stride + left * 4
                if kind == "dxt1":
                    p0, p1, p2, p3 = [
                        colour[0] | colour[1] << 8 | colour[2] << 16
                        | colour[3] << 24 for colour in colours]
                else:
                    p0, p1, p2, p3 = [
                        colour[0] | colour[1] << 8 | colour[2] << 16
                        for colour in colours]
                palette = (p0, p1, p2, p3)
                for row in range(4):
                    here = base + row * stride
                    index = (bits >> (8 * row)) & 0xff
                    red = palette[index & 3]
                    green = palette[(index >> 2) & 3]
                    blue = palette[(index >> 6) & 3]
                    alpha = palette[(index >> 14) & 3]
                    if kind == "dxt3":
                        nibbles = (alpha_bits >> (16 * row)) & 0xffff
                        out[here:here + 16] = pack(
                            "<4I",
                            red | ((nibbles & 0xf) * 17) << 24,
                            green | (((nibbles >> 4) & 0xf) * 17) << 24,
                            blue | (((nibbles >> 8) & 0xf) * 17) << 24,
                            alpha | (((nibbles >> 12) & 0xf) * 17) << 24)
                    elif kind == "dxt5":
                        three = (alpha_bits >> (12 * row)) & 0xfff
                        out[here:here + 16] = pack(
                            "<4I",
                            red | alphas[three & 7] << 24,
                            green | alphas[(three >> 3) & 7] << 24,
                            blue | alphas[(three >> 6) & 7] << 24,
                            alpha | alphas[(three >> 9) & 7] << 24)
                    else:
                        out[here:here + 16] = pack(
                            "<4I", red, green, blue, alpha)
                continue
            for row in range(rows):
                line = (top + row) * stride + left * 4
                pixel = row * 4
                for column in range(columns):
                    red, green, blue, alpha = colours[(bits >> (2 * pixel)) & 3]
                    if kind == "dxt3":
                        alpha = ((alpha_bits >> (4 * pixel)) & 0xf) * 17
                    elif kind == "dxt5":
                        alpha = alphas[(alpha_bits >> (3 * pixel)) & 7]
                    at = line + column * 4
                    out[at] = red
                    out[at + 1] = green
                    out[at + 2] = blue
                    out[at + 3] = alpha
                    pixel += 1
    return out


def _decode_raw(payload, width, height, bpp):
    """Decode raw 1, 2, 4 or 8 bytes-a-pixel data to RGBA8.

    The one-byte form is a single channel, which a colour map reads as grey.
    The two-byte form is a little-endian 16-bit channel, and its high byte is
    the value the height-map data carries, so the low byte is dropped.  The
    four-byte form is RGBA8 and is copied.  The eight-byte form, two bytes a
    channel, does not occur in either package: it is read as four little-
    endian 16-bit channels with the low byte dropped, which that layout would
    make correct, and the tool reports it as unproven.
    """
    want = width * height * bpp
    if len(payload) < want:
        raise ContainerError(
            "pixels", "raw%d needs %d bytes for %dx%d, the payload holds %d"
            % (bpp * 8, want, width, height, len(payload)))
    count = width * height
    out = bytearray(count * 4)
    if bpp == 4:
        out[:] = payload[:want]
        return out
    if bpp == 1:
        for index in range(count):
            value = payload[index]
            at = index * 4
            out[at] = value
            out[at + 1] = value
            out[at + 2] = value
            out[at + 3] = 255
    elif bpp == 2:
        for index in range(count):
            value = payload[2 * index + 1]
            at = index * 4
            out[at] = value
            out[at + 1] = value
            out[at + 2] = value
            out[at + 3] = 255
    else:
        for index in range(count):
            at = 8 * index
            out[4 * index] = payload[at + 1]
            out[4 * index + 1] = payload[at + 3]
            out[4 * index + 2] = payload[at + 5]
            out[4 * index + 3] = payload[at + 7]
    return out


def decode_pc_level0(payload, where):
    """The full-size level of slice 0 of a PC image payload, as RGBA8.

    The level table puts the smallest level first, so the full-size level is
    the last slot and starts at the end of the level bytes of slice 0.  A
    payload that is not an image, or whose format has no decoder, is an error
    that names the resource: a wrongly decoded texture is worse than none.
    """
    image = describe_pc_image(payload)
    if image is None:
        raise ContainerError(
            where, "is not a PC image payload, so its pixels cannot be read")
    levels = [struct.unpack_from("<HHI", payload, 16 + index * PC_IMAGE_SLOT_SIZE)
              for index in range(image.levels)]
    level_w, level_h, level_bytes = levels[-1]
    start = PC_IMAGE_HEADER_SIZE + sum(level[2] for level in levels) - level_bytes
    pixels = payload[start:start + level_bytes]
    if image.bpp is None:
        if image.fmt not in ("dxt1", "dxt3", "dxt5"):
            raise ContainerError(
                where, "is stored as %r blocks, which has no decoder"
                % image.fmt)
        pixels = _decode_dxt(pixels, level_w, level_h, image.fmt)
    else:
        pixels = _decode_raw(pixels, level_w, level_h, image.bpp)
    return level_w, level_h, pixels


def png_rgba(width, height, pixels):
    """A PNG file of RGBA8 pixels, using only the standard library."""
    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data +
                struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff))

    stride = width * 4
    raw = bytearray((stride + 1) * height)
    for row in range(height):
        at = row * (stride + 1)
        raw[at] = 0
        raw[at + 1:at + 1 + stride] = pixels[row * stride:(row + 1) * stride]
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6,
                                         0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
            + chunk(b"IEND", b""))


def _box_python(pixels, width, height, out_width, out_height):
    """Area-average down to a smaller size, in pure Python."""
    scale_x = width / out_width
    scale_y = height / out_height
    out = bytearray(out_width * out_height * 4)
    for out_y in range(out_height):
        top = out_y * scale_y
        bottom = top + scale_y
        first_row = int(top)
        last_row = min(height, max(first_row + 1, int(bottom + 0.999999)))
        for out_x in range(out_width):
            left = out_x * scale_x
            right = left + scale_x
            first = int(left)
            last = min(width, max(first + 1, int(right + 0.999999)))
            red = green = blue = alpha = 0
            count = 0
            for row in range(first_row, last_row):
                base = row * width * 4
                for column in range(first, last):
                    at = base + column * 4
                    red += pixels[at]
                    green += pixels[at + 1]
                    blue += pixels[at + 2]
                    alpha += pixels[at + 3]
                    count += 1
            at = (out_y * out_width + out_x) * 4
            out[at] = red // count
            out[at + 1] = green // count
            out[at + 2] = blue // count
            out[at + 3] = alpha // count
    return out


def _box_two(pixels, width, height):
    """The exact 2x2 mean of an RGBA8 image, in one pass of plain Python.

    The plan halves most textures, so this path carries nearly every
    replacement.  numpy is not used: on a memory-tight machine its large
    arrays cost more than this loop does.
    """
    out_width = width // 2
    out_height = height // 2
    out = bytearray(out_width * out_height * 4)
    stride = width * 4
    out_stride = out_width * 4
    source = pixels
    for row in range(out_height):
        top = 2 * row * stride
        bottom = top + stride
        at = row * out_stride
        left = 0
        for _ in range(out_width):
            a = top + left
            b = bottom + left
            ap = a + 4
            bp = b + 4
            out[at] = (source[a] + source[ap] + source[b]
                       + source[bp]) >> 2
            out[at + 1] = (source[a + 1] + source[ap + 1] + source[b + 1]
                           + source[bp + 1]) >> 2
            out[at + 2] = (source[a + 2] + source[ap + 2] + source[b + 2]
                           + source[bp + 2]) >> 2
            out[at + 3] = (source[a + 3] + source[ap + 3] + source[b + 3]
                           + source[bp + 3]) >> 2
            at += 4
            left += 8
    return out


def resample_box(pixels, width, height, out_width, out_height):
    """Area-average RGBA8 pixels down to `out_width` x `out_height`.

    Every output pixel is the mean of the source it covers, so a 2048 texture
    that the plan gives a 1024 target is averaged 2x2.  numpy is not used:
    on a machine that is short of memory its large arrays cost seconds, so
    the exact factor 2 case has its own pass and anything else uses the slow
    general path.
    """
    if (out_width, out_height) == (width, height):
        return pixels
    if out_width < 1 or out_height < 1:
        raise ContainerError("resample", "%dx%d is not a size"
                             % (out_width, out_height))
    if out_width <= width and out_height <= height:
        if width == 2 * out_width and height == 2 * out_height:
            # The exact mean keeps every bit of the average, so the halving
            # path stays on it rather than on a rounding resampler.
            return _box_two(pixels, width, height)
        Image = _pillow_image()
        if Image is not None:
            try:
                picture = Image.frombuffer("RGBA", (width, height),
                                           bytes(pixels), "raw", "RGBA", 0, 1)
                small = picture.resize((out_width, out_height), Image.BOX)
                return bytearray(small.tobytes())
            except Exception:
                pass
        return _box_python(pixels, width, height, out_width, out_height)
    raise ContainerError("resample", "%dx%d is larger than %dx%d; scaling up "
                         "is not part of a replacement"
                         % (out_width, out_height, width, height))


def parse_quality(text):
    """The astcenc quality preset a `--quality` value names.

    A preset name is taken as it is.  A number from 0 to 100 is the value
    astcenc gives the six presets, so it picks the closest one: 0 to 5
    `fastest`, 6 to 30 `fast`, 31 to 80 `medium`, 81 to 98 `thorough`, 99
    `verythorough`, 100 `exhaustive`.
    """
    raw = (text or DEFAULT_QUALITY).strip().lower()
    name = raw[1:] if raw.startswith("-") else raw
    if name in QUALITY_PRESETS:
        return name
    if raw.startswith("-") and name.isdigit():
        raise ContainerError("--quality", "%s is not a preset name; write a "
                             "number from 0 to 100 without the dash" % text)
    try:
        score = int(name)
    except (TypeError, ValueError):
        raise ContainerError(
            "--quality", "%s is not one of %s or a number from 0 to 100"
            % (text, ", ".join(QUALITY_PRESETS)))
    if not 0 <= score <= 100:
        raise ContainerError("--quality", "%d is not between 0 and 100" % score)
    if score <= 5:
        return "fastest"
    if score <= 30:
        return "fast"
    if score <= 80:
        return "medium"
    if score <= 98:
        return "thorough"
    if score <= 99:
        return "verythorough"
    return "exhaustive"


def astc_encode(pixels, width, height, quality, astcenc, colorspace="linear"):
    """Encode RGBA8 to ASTC 4x4 blocks with `astcenc`.

    The encoder writes a 16-byte header (its magic, the 4x4 block, and the
    image size) and then the blocks, and the Switch header carries the blocks
    only, so the header is checked and dropped here.  `colorspace` is `linear`
    (`-cl`, the values are encoded as they are) or `srgb` (`-cs`).
    """
    if astcenc is None:
        raise ContainerError(
            "astcenc", "is not on PATH; install it, or pass --astcenc")
    work = tempfile.mkdtemp(prefix="frostpunk-astc-")
    try:
        source = os.path.join(work, "source.png")
        target = os.path.join(work, "target.astc")
        with open(source, "wb") as fh:
            fh.write(png_rgba(width, height, pixels))
        command = [astcenc, "-cs" if colorspace == "srgb" else "-cl",
                   source, target, "4x4", "-" + quality, "-silent"]
        result = subprocess.run(command, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)
        if result.returncode != 0:
            raise ContainerError(
                "astcenc", "failed on %dx%d at %s: %s"
                % (width, height, quality,
                   result.stderr.decode("utf-8", "replace").strip()[-400:]))
        with open(target, "rb") as fh:
            blob = fh.read()
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if len(blob) < 16 or blob[:4] != b"\x13\xab\xa1\x5c":
        raise ContainerError("astcenc", "did not write an ASTC file")
    block_x, block_y = blob[4], blob[5]
    size_x = int.from_bytes(blob[7:10], "little")
    size_y = int.from_bytes(blob[10:13], "little")
    depth = int.from_bytes(blob[13:16], "little")
    if (block_x, block_y, size_x, size_y, depth) != (4, 4, width, height, 1):
        raise ContainerError(
            "astcenc", "wrote a %dx%d texture in %dx%d blocks, not %dx%d in "
            "4x4" % (size_x, size_y, block_x, block_y, width, height))
    blocks = blob[16:]
    if len(blocks) != _astc_pixels(width, height):
        raise ContainerError(
            "astcenc", "wrote %d block bytes for %dx%d, expected %d"
            % (len(blocks), width, height, _astc_pixels(width, height)))
    return blocks


def astc_cached(plan_id, pixels, width, height, quality, astcenc, cache_dir,
                colorspace="linear"):
    """Encode, or read the cached encoding for this id, size and quality.

    The cache holds the whole `.astc` file the encoder wrote, so it can be
    decoded again with astcenc for a look, and it is keyed by the resource id,
    the size, the quality and the colour space.
    """
    name = "%08x-%dx%d-%s-%s.astc" % (plan_id, width, height, quality,
                                      "srgb" if colorspace == "srgb" else "lin")
    path = os.path.join(cache_dir, name)
    if os.path.exists(path):
        with open(path, "rb") as fh:
            blob = fh.read()
        if len(blob) == 16 + _astc_pixels(width, height):
            return blob[16:], True
    blocks = astc_encode(pixels, width, height, quality, astcenc, colorspace)
    with open(path + ".part", "wb") as fh:
        fh.write(bytes.fromhex("13aba15c") + bytes([4, 4, 1]) +
                 width.to_bytes(3, "little") + height.to_bytes(3, "little") +
                 (1).to_bytes(3, "little") + blocks)
    os.replace(path + ".part", path)
    return blocks, False


def build_switch_payload(original, width, height, blocks, convert=False):
    """A Switch texture payload that holds one ASTC 4x4 level.

    Every header byte of the texture being replaced is kept, except the two
    words that describe the pixels, which are set to the level this runs
    wrote: the level count at +19 and the pixel bytes at +24.  With `convert`
    the format id and its descriptor are set to the ASTC 4x4 pair as well,
    which is what turns an uncompressed block into an ASTC one.
    """
    header = bytearray(original[:TEXTURE_HEADER_SIZE])
    struct.pack_into("<HH", header, 12, width, height)
    header[19] = 1
    struct.pack_into("<I", header, 24, len(blocks))
    if convert:
        struct.pack_into("<I", header, 4, SWITCH_FORMAT_ASTC_4X4)
        header[20:24] = ASTC_FORMAT_DESCRIPTOR
    return bytes(header) + blocks


def default_cache_dir():
    """Where encoded replacements are cached between runs."""
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(
        os.path.expanduser("~"), ".cache")
    return os.path.join(base, "frostpunk-assets", "astc")


def find_astcenc(path=None):
    """The astcenc program to use, or `None` when there is none."""
    if path:
        return path if os.path.exists(path) else None
    return shutil.which("astcenc")


def _short_name(name, width=44):
    """A resource name that fits in a table cell, cut at the front."""
    if len(name) <= width:
        return name
    return "..." + name[-(width - 3):]


def render_apply(report, fmt):
    """The report of one `apply` run, as text or as JSON."""
    if fmt == "json":
        return json.dumps(report, indent=2, sort_keys=False) + "\n"
    out = ["switch    %s" % report["switch"],
           "plan      %s (%s entries)"
           % (report["plan"], format(report["planned"], ",")),
           "replaced  %s of %s" % (format(report["count"], ","),
                                   format(report["planned"], ",")),
           "old payload %s bytes, new payload %s bytes, net %+d"
           % (format(report["old_bytes"], ","),
              format(report["new_bytes"], ","),
              report["new_bytes"] - report["old_bytes"]),
           "on disk   %s bytes" % format(report["on_disk"], ","),
           "time      %.1f s (%s encoded, %s from cache)"
           % (report["elapsed"], format(report["encoded"], ","),
              format(report["cached"], ","))]
    if report["refused"]:
        groups = {}
        for row in report["refused"]:
            groups.setdefault(row["reason"], []).append(row["id"])
        out.append("refused   %s"
                   % format(len(report["refused"]), ","))
        for reason in sorted(groups):
            wanted = " ".join(sorted(groups[reason]))
            out.append("  %s  %s" % (format(len(groups[reason]), ","), reason))
            out.append("           ids: %s" % wanted)
    if report["entries"]:
        out.append("replacements:")
        for row in report["entries"]:
            out.append("  %s %-44s %4dx%-4d -> %4dx%-4d %12s -> %12s %+13s"
                       % (row["id"], _short_name(row["name"]), row["switch"][0],
                          row["switch"][1], row["target"][0], row["target"][1],
                          format(row["old"], ","), format(row["new"], ","),
                          format(row["new"] - row["old"], ",")))
        largest = sorted(report["entries"],
                         key=lambda row: (-row["new"], row["id"]))[:3]
        out.append("largest replacements:")
        for row in largest:
            out.append("  %s %-44s %4dx%-4d %12s"
                       % (row["id"], _short_name(row["name"]), row["target"][0],
                          row["target"][1], format(row["new"], ",")))
    return "\n".join(out) + "\n"


def apply_entry(entry, switch, pc, quality, astcenc, cache_dir, colorspace,
                convert_uncompressed):
    """One plan entry to its replacement payload, or a `ContainerError`."""
    plan_id = int(entry["id"], 16)
    where = "%s %s" % (entry["id"], entry["name"])
    record = switch.by_id(plan_id)
    original = switch.read_payload(record)
    texture = switch_texture(original)
    if texture is None:
        raise ContainerError(where, "is not a Switch texture block")
    convert = False
    if texture.format != SWITCH_FORMAT_ASTC_4X4:
        if texture.format == SWITCH_FORMAT_R16:
            raise ContainerError(
                where,
                "is stored uncompressed as 16-bit data (format 5); an ASTC "
                "4x4 block would keep 8 bits a channel, so it is left alone")
        if not convert_uncompressed:
            raise ContainerError(
                where,
                "is stored uncompressed as %s (format %d), not ASTC 4x4; pass "
                "--convert-uncompressed to rewrite the format fields, which "
                "has not been tried on a device"
                % (texture.label, texture.format))
        convert = True
    target_w, target_h = entry["target"]
    if target_w * target_h < texture.width * texture.height:
        raise ContainerError(
            where,
            "would shrink the texture from %dx%d to %dx%d, which a "
            "replacement must not do"
            % (texture.width, texture.height, target_w, target_h))
    source = pc.by_id(plan_id)
    payload = pc.read_payload(source)
    width, height, pixels = decode_pc_level0(payload, where)
    pixels = resample_box(pixels, width, height, target_w, target_h)
    blocks, cached = astc_cached(plan_id, pixels, target_w, target_h, quality,
                                 astcenc, cache_dir, colorspace)
    replacement = build_switch_payload(original, target_w, target_h, blocks,
                                       convert)
    return replacement, {
        "id": entry["id"], "name": entry["name"] or "",
        "switch": [texture.width, texture.height],
        "target": [target_w, target_h],
        "old": len(original), "new": len(replacement),
    }, cached


def cmd_apply(args):
    """Write a replacement container set from a plan file."""
    header, entries = read_plan(args.plan)
    switch_path = args.switch or header.get("switch")
    pc_path = args.pc or header.get("pc")
    if not switch_path or not pc_path:
        raise ContainerError(
            args.plan, "does not name the two packages; pass --switch and --pc")
    switch = DatSet(switch_path)
    pc = DatSet(pc_path)
    quality = parse_quality(args.quality)
    astcenc = find_astcenc(args.astcenc)
    if astcenc is None:
        raise ContainerError("astcenc", "is not on PATH; install it, or pass "
                             "--astcenc")
    cache_dir = args.cache_dir or default_cache_dir()
    os.makedirs(cache_dir, exist_ok=True)
    planned = len(entries)
    entries = entries[:args.limit] if args.limit else entries
    colorspace = "srgb" if args.srgb else "linear"
    started = time.time()
    replacements = {}
    rows, refused, cached_count = [], [], 0
    for entry in entries:
        try:
            replacement, row, cached = apply_entry(
                entry, switch, pc, quality, astcenc, cache_dir, colorspace,
                args.convert_uncompressed)
        except ContainerError as err:
            if args.fail_fast:
                raise
            refused.append({"id": entry["id"],
                            "name": entry.get("name") or "",
                            "reason": err.why})
            continue
        replacements[int(entry["id"], 16)] = replacement
        rows.append(row)
        cached_count += 1 if cached else 0
    elapsed = time.time() - started
    if not replacements:
        said = "; ".join("%s: %s" % (row["id"], row["reason"])
                         for row in refused[:5])
        raise ContainerError(args.plan, "no entry could be replaced: %s" % said)
    dat_path, idx_path = switch.write(args.out, replacements)
    on_disk = os.path.getsize(dat_path)
    written = DatSet(dat_path)
    accounted = sum(record.stored for record in written.records)
    if accounted != on_disk:
        raise ContainerError(
            dat_path, "holds %d bytes but its index accounts for %d"
            % (on_disk, accounted))
    report = {
        "switch": switch_path,
        "plan": args.plan,
        "planned": planned,
        "attempted": len(entries),
        "count": len(rows),
        "old_bytes": sum(row["old"] for row in rows),
        "new_bytes": sum(row["new"] for row in rows),
        "on_disk": on_disk,
        "elapsed": round(elapsed, 2),
        "quality": quality,
        "encoded": len(rows) - cached_count,
        "cached": cached_count,
        "entries": rows,
        "refused": refused,
        "dat": dat_path, "idx": idx_path,
    }
    sys.stdout.write(render_apply(report, args.format))
    return 0


# -- writing format 72 as BC3 in the block-linear layout -------------------
#
# Format 72 is not ASTC 4x4: a level's bytes are BC3 (DXT5) blocks in NVN's
# block-linear layout, the layout src/nx/gpu/texture/blocklinear.h describes
# and `frostpunk_bl.py` ports.  A level of `w` by `h` texels takes
# `bl_level_size(w, h, 4, level)` bytes, which is the 16-byte blocks of
# ceil(w/4) by ceil(h/4) laid out in GOBs of 512 bytes, whole blocks of 2^bh
# GOB rows high, with the block height shrunk for the small levels of a chain.
#
# On `buildings/generator/bld_generator_000_lv1_alb_01.texture` (1024x1024, 11
# levels, format 72) the levels take 1,048,576 + 262,144 + 65,536 + 16,384 +
# 4,096 + 1,024 + 512 + 512 + 512 + 512 + 512 = 1,400,320 bytes, which is the
# payload length less its 28-byte header and exactly the header's +24 field.
# Reading those levels back with `unswizzle` and writing them out again with
# `swizzle` reproduces the payload byte for byte, which is what proves the
# layout.
#
# A replacement is therefore the PC image, decoded, cut down to a size the
# package can carry, encoded to BC3 and written back in that layout, level
# after level.  `apply` (above) writes ASTC blocks instead, which is what
# format 72 was read as before the layout was measured; it stays for the
# textures whose pixels are not format 72 and for the record of that reading.
BC3_BLOCK = 4
BC3_BYTES_PER_BLOCK = 16
BC3_BLOCK_HEIGHT = 4
BC3_PC_FORMATS = ("dxt1", "dxt3", "dxt5")
BC3_PC_LABELS = {"dxt1": "BC1/DXT1", "dxt3": "BC2/DXT3", "dxt5": "BC3/DXT5"}
DEFAULT_BC3_MAX_SIZE = 2048
BC3_PAD_REASON = (
    "a .dat shorter than the one it replaces is what the game refuses to read: "
    "nn::fs::ReadFile fails with 0x177a02 at load")

_BC3 = None


def bc3_layout():
    """The layout and codec module that sits next to this script."""
    global _BC3
    if _BC3 is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "frostpunk_bl.py")
        spec = importlib.util.spec_from_file_location("frostpunk_bl", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _BC3 = module
    return _BC3


def bc3_levels(width, height):
    """The levels a BC3 chain of this size has: one per halving, down to 1x1."""
    levels = 1
    while max(width >> levels, height >> levels) >= 1:
        levels += 1
    return levels


def bc3_level_dims(width, height, level):
    """The texel size of one level of the chain."""
    return max(width >> level, 1), max(height >> level, 1)


def bc3_level_bh(height, level):
    """The block height a level is stored with: the texture's, shrunk for small levels."""
    return bc3_layout().level_blocks(0, BC3_BLOCK_HEIGHT, level, height)


def bc3_level_bytes(width, height, level):
    """The bytes one level of a `width` by `height` chain takes."""
    level_w, level_h = bc3_level_dims(width, height, level)
    return bc3_layout().level_bytes(level_w, level_h,
                                    bc3_level_bh(height, level))


def bc3_chain_bytes(width, height, levels=None):
    """The pixel bytes a `width` by `height` block takes, every level or the first `levels`.

    A block may declare fewer levels than the chain could hold: 2629 of the
    shipped format-72 blocks hold level 0 alone.  The bytes of a shorter chain
    are the bytes of the same levels in the same order.
    """
    if levels is None:
        levels = bc3_levels(width, height)
    return sum(bc3_level_bytes(width, height, level)
               for level in range(levels))


def build_bc3_payload(original, width, height, pixels):
    """A format-72 payload: every level of `pixels` as BC3, in the block-linear layout.

    The header of the block being replaced is kept except the three fields
    that describe the pixels: the size at +12/+14, the level count at +19 and
    the pixel bytes at +24.  The magic, the format id, the descriptor and the
    fields the package does not document keep their old values.
    """
    layout = bc3_layout()
    levels = bc3_levels(width, height)
    level_w, level_h, image = width, height, pixels
    body = bytearray()
    for level in range(levels):
        if level:
            new_w, new_h = bc3_level_dims(width, height, level)
            image = layout.resample(image, level_w, level_h, new_w, new_h)
            level_w, level_h = new_w, new_h
        blocks = layout.dxt5_encode(image, level_w, level_h)
        written = layout.swizzle(blocks, level_w, level_h,
                                 bc3_level_bh(height, level))
        want = bc3_level_bytes(width, height, level)
        if len(written) != want:
            raise ContainerError(
                "bc3", "level %d of %dx%d takes %d bytes, the layout gave %d"
                % (level, width, height, want, len(written)))
        body += written
    header = bytearray(original[:TEXTURE_HEADER_SIZE])
    struct.pack_into("<HH", header, 12, width, height)
    header[19] = levels
    struct.pack_into("<I", header, 24, len(body))
    return bytes(header) + bytes(body)


def bc3_target_dims(max_size, switch_size, pc_size):
    """The size a BC3 replacement is written at, or `None` and why it cannot be.

    The PC image is the source, so the target is the PC size with its longest
    side capped at `max_size`; a replacement never comes out smaller than the
    Switch texture it stands in for, because that would lose detail the
    package already has.  Nothing is rounded to a power of two: the layout
    rounds up per block, so a 1010x534 image is a normal case.
    """
    pc_w, pc_h = pc_size
    longest = max(pc_w, pc_h)
    if longest <= max_size:
        width, height = pc_w, pc_h
    else:
        width = max(1, min(max_size, (pc_w * max_size + longest // 2) // longest))
        height = max(1, min(max_size, (pc_h * max_size + longest // 2) // longest))
    switch_w, switch_h = switch_size
    if width < switch_w or height < switch_h:
        return None, ("would be %dx%d, smaller than the Switch texture %dx%d; "
                      "raise --max-size (it is %d) or leave this texture alone"
                      % (width, height, switch_w, switch_h, max_size))
    return (width, height), None


def apply_bc3_entry(entry, switch, pc, max_size):
    """One plan entry to its replacement payload, or a `ContainerError`."""
    plan_id = int(entry["id"], 16)
    where = "%s %s" % (entry["id"], entry["name"])
    record = switch.record_map().get(plan_id)
    if record is None:
        raise ContainerError(where, "is not in the Switch package")
    original = switch.read_payload(record)
    texture = switch_texture(original)
    if texture is None:
        raise ContainerError(where, "is not a Switch texture block")
    if texture.format != SWITCH_FORMAT_ASTC_4X4:
        raise ContainerError(
            where,
            "is stored as %s (format %d), not format 72, so it does not hold "
            "the BC3 blocks this command writes and is left alone"
            % (texture.label, texture.format))
    chain = bc3_chain_bytes(texture.width, texture.height)
    levels = bc3_levels(texture.width, texture.height)
    if texture.mips > levels:
        raise ContainerError(
            where,
            "declares %d level(s); %dx%d has a chain of %d, so its pixels are "
            "not that layout and are left alone"
            % (texture.mips, texture.width, texture.height, levels))
    # A block holds its declared levels or a whole number of copies of them:
    # a cube map or a texture array carries one copy a face, and this command
    # encodes one image, so those are refused rather than guessed at.
    own = bc3_chain_bytes(texture.width, texture.height, texture.mips)
    if own and texture.pixel_bytes != own and not texture.pixel_bytes % own:
        raise ContainerError(
            where,
            "holds %d copies of its %d-level chain (%s bytes each), a cube map "
            "or a texture array, which this command does not encode"
            % (texture.pixel_bytes // own, texture.mips, format(own, ",")))
    if texture.pixel_bytes != own:
        raise ContainerError(
            where,
            "declares %d level(s) and %s pixel bytes; %dx%d in the block-linear "
            "layout at block height %d is %s bytes, so its pixels are not that "
            "layout and are left alone"
            % (texture.mips, format(texture.pixel_bytes, ","), texture.width,
               texture.height, BC3_BLOCK_HEIGHT, format(own, ",")))
    source = pc.record_map().get(plan_id)
    if source is None:
        raise ContainerError(where, "has no record in the PC package")
    payload = pc.read_payload(source)
    image = describe_pc_image(payload)
    if image is None:
        raise ContainerError(
            where, "is not an image payload on the PC side, so there is "
                   "nothing to encode")
    if image.fmt not in BC3_PC_FORMATS:
        raise ContainerError(
            where,
            "is %s on the PC side, not BC1, BC2 or BC3 blocks, so it is not "
            "an image to encode again" % image.label)
    width, height, pixels = decode_pc_level0(payload, where)
    target, why = bc3_target_dims(max_size, (texture.width, texture.height),
                                  (width, height))
    if target is None:
        raise ContainerError(where, why)
    replacement = build_bc3_payload(original, target[0], target[1], pixels)
    return replacement, {
        "id": entry["id"], "name": entry["name"] or "",
        "switch": [texture.width, texture.height],
        "pc": [width, height],
        "target": [target[0], target[1]],
        "levels": bc3_levels(target[0], target[1]),
        "pc_format": image.label,
        "old": len(original), "new": len(replacement),
    }


def render_apply_bc3(report, fmt):
    """The report of one `apply-bc3` run, as text or as JSON."""
    if fmt == "json":
        return json.dumps(report, indent=2, sort_keys=False) + "\n"
    out = ["switch    %s" % report["switch"],
           "pc        %s" % report["pc"],
           "plan      %s (%s entries)"
           % (report["plan"], format(report["planned"], ",")),
           "replaced  %s of %s" % (format(report["count"], ","),
                                   format(report["attempted"], ",")),
           "levels    %s" % format(report["levels"], ","),
           "old payload %s bytes, new payload %s bytes, net %+d"
           % (format(report["old_bytes"], ","),
              format(report["new_bytes"], ","),
              report["new_bytes"] - report["old_bytes"]),
           "on disk   %s bytes%s"
           % (format(report["on_disk"], ","),
              "" if not report["padding"]
              else ", %s of padding to reach %s"
                   % (format(report["padding"], ","),
                      format(report["pad_to"], ","))),
           "time      %.1f s (encode %.1f s, write %.1f s)"
           % (report["elapsed"], report["encoded"], report["write"])]
    if report["padding"]:
        out.append("padding   %s" % BC3_PAD_REASON)
    if report["beyond"]:
        out.append(
            "note      %s record(s) end past the %s bytes of the file they "
            "replace, by %s bytes in all; a loader that bounds its reads by "
            "the size it knows would not reach them"
            % (format(report["beyond"], ","),
               format(report["replaced_size"], ","),
               format(report["beyond_bytes"], ",")))
    if report["refused"]:
        groups = {}
        for row in report["refused"]:
            groups.setdefault(row["reason"], []).append(row["id"])
        out.append("refused   %s" % format(len(report["refused"]), ","))
        for reason in sorted(groups):
            out.append("  %s  %s" % (format(len(groups[reason]), ","), reason))
            out.append("           ids: %s" % " ".join(sorted(groups[reason])))
    if report["entries"]:
        out.append("replacements:")
        for row in report["entries"]:
            out.append("  %s %-38s %4dx%-4d -> %4dx%-4d %2d lv %12s -> "
                       "%12s %+13s"
                       % (row["id"], _short_name(row["name"]), row["switch"][0],
                          row["switch"][1], row["target"][0],
                          row["target"][1], row["levels"],
                          format(row["old"], ","), format(row["new"], ","),
                          format(row["new"] - row["old"], ",")))
    return "\n".join(out) + "\n"


def cmd_apply_bc3(args):
    """Write a replacement container set from a plan, format 72 as BC3 blocks.

    The `.dat` is padded to the length of the one it replaces by default: the
    loader asks for the file's length from the RomFS and fails the load with
    `nn::fs::ReadFile` 0x177a02 when the rewritten file is shorter, even
    though every offset in the index is right.  The padding is not described
    by the index, so the block bytes and the file size are reported apart.
    """
    header, entries = read_plan(args.plan)
    switch_path = args.switch or header.get("switch")
    pc_path = args.pc or header.get("pc")
    if not switch_path or not pc_path:
        raise ContainerError(
            args.plan, "does not name the two packages; pass --switch and --pc")
    switch = DatSet(switch_path)
    pc = DatSet(pc_path)
    planned = len(entries)
    entries = entries[:args.limit] if args.limit else entries
    started = time.time()
    replacements, rows, refused = {}, [], []
    for entry in entries:
        try:
            replacement, row = apply_bc3_entry(entry, switch, pc,
                                               args.max_size)
        except ContainerError as err:
            if args.fail_fast:
                raise
            refused.append({"id": entry["id"],
                            "name": entry.get("name") or "",
                            "reason": err.why})
            continue
        replacements[int(entry["id"], 16)] = replacement
        rows.append(row)
    encoded = time.time() - started
    if not replacements:
        said = "; ".join("%s: %s" % (row["id"], row["reason"])
                         for row in refused[:5])
        raise ContainerError(args.plan, "no entry could be replaced: %s" % said)
    pad_to = None if args.no_pad else (
        args.pad_to if args.pad_to is not None
        else os.path.getsize(switch.dat_path))
    dat_path, idx_path = switch.write(args.out, replacements, pad_to=pad_to)
    elapsed = time.time() - started
    on_disk = os.path.getsize(dat_path)
    written = DatSet(dat_path)
    accounted = sum(record.stored for record in written.records)
    if accounted > on_disk:
        raise ContainerError(
            dat_path, "holds %d bytes but its index accounts for %d"
            % (on_disk, accounted))
    # Padding fixes a file that is too short, but a replacement that grows the
    # blocks moves the tail past the length of the file it replaces.  Whether
    # that is reachable depends on how the loader sizes the file, which this
    # command cannot know, so the report counts the blocks that sit past it.
    replaced_size = os.path.getsize(switch.dat_path)
    beyond = [record for record in written.records
              if record.offset + record.stored > replaced_size]
    report = {
        "switch": switch_path,
        "pc": pc_path,
        "plan": args.plan,
        "planned": planned,
        "attempted": len(entries),
        "count": len(rows),
        "max_size": args.max_size,
        "levels": sum(row["levels"] for row in rows),
        "old_bytes": sum(row["old"] for row in rows),
        "new_bytes": sum(row["new"] for row in rows),
        "on_disk": on_disk,
        "pad_to": pad_to,
        "padding": on_disk - accounted,
        "replaced_size": replaced_size,
        "beyond": len(beyond),
        "beyond_bytes": sum(record.offset + record.stored - replaced_size
                            for record in beyond),
        "encoded": round(encoded, 2),
        "write": round(elapsed - encoded, 2),
        "elapsed": round(elapsed, 2),
        "entries": rows,
        "refused": refused,
        "dat": dat_path, "idx": idx_path,
    }
    sys.stdout.write(render_apply_bc3(report, args.format))
    return 0


def cmd_extract(args):
    ds = DatSet(args.dat)
    rec = ds.resolve(args.what)
    payload = ds.read_payload(rec)
    out = args.out or "%08x.bin" % rec.rid
    with open(out, "wb") as fh:
        fh.write(payload)
    print("%s -> %s (%d bytes, payload of 0x%08x)"
          % (rec.name or "record", out, len(payload), rec.rid))
    return 0


def cmd_replace(args):
    """Write a new container set with one resource's payload swapped."""
    ds = DatSet(args.dat)
    rec = ds.resolve(args.what)
    with open(args.payload, "rb") as fh:
        payload = fh.read()
    if not args.allow_size_change and len(payload) != rec.payload:
        raise ContainerError(
            args.payload,
            "holds %d bytes but 0x%08x takes %d; pass --allow-size-change to "
            "take a different size" % (len(payload), rec.rid, rec.payload))
    dat, idx = ds.write(args.out, {rec.rid: payload})
    print("replaced %s (0x%08x) with %s, %d bytes"
          % (rec.name or "record", rec.rid, args.payload, len(payload)))
    print("wrote %s and %s" % (dat, idx))
    return 0


def cmd_roundtrip(args):
    """Read every payload, rewrite the set, read it back and compare hashes.

    A round-trip may legitimately change where a block sits (unowned spans are
    dropped, the offsets are recomputed) and, for a replaced resource, how the
    block is compressed.  It may not change which ids and names exist, nor the
    payload bytes or payload size of any resource.  Any such change is reported
    with the resource id and exits 1.
    """
    ds = DatSet(args.dat)
    source = {}
    bad = 0
    for rec in ds.records:
        label = "%08x" % rec.rid
        try:
            payload = ds.read_payload(rec)
        except ContainerError as err:
            print("FAIL %s: %s" % (label, err))
            bad += 1
            continue
        source[rec.rid] = (rec.name, len(payload),
                           hashlib.sha256(payload).hexdigest())
    if bad:
        print("%d of %d records do not read back; nothing written"
              % (bad, len(ds.records)))
        return 1

    out_dat, _ = ds.write(args.out)
    try:
        again = DatSet(out_dat)
    except ContainerError as err:
        print("FAIL rewritten set: %s" % err)
        return 1
    reread = {}
    for rec in again.records:
        label = "%08x" % rec.rid
        try:
            payload = again.read_payload(rec)
        except ContainerError as err:
            print("FAIL %s: rewritten set: %s" % (label, err))
            bad += 1
            continue
        reread[rec.rid] = (rec.name, len(payload),
                           hashlib.sha256(payload).hexdigest())

    for rid in sorted(set(source) | set(reread)):
        label = "%08x" % rid
        if rid not in reread:
            print("FAIL %s: missing from the rewritten set" % label)
            bad += 1
            continue
        if rid not in source:
            print("FAIL %s: not in the original set" % label)
            bad += 1
            continue
        src_name, src_size, src_hash = source[rid]
        dst_name, dst_size, dst_hash = reread[rid]
        if src_name != dst_name:
            print("FAIL %s: name %r became %r"
                  % (label, src_name, dst_name))
            bad += 1
        if src_size != dst_size:
            print("FAIL %s: payload %d bytes became %d"
                  % (label, src_size, dst_size))
            bad += 1
        elif src_hash != dst_hash:
            print("FAIL %s: payload bytes changed (sha256 %s -> %s)"
                  % (label, src_hash, dst_hash))
            bad += 1

    if bad:
        print("%d difference(s) across %d records"
              % (bad, len(source)))
        return 1

    src_size = os.path.getsize(ds.dat_path)
    dst_size = os.path.getsize(out_dat)
    moved = sum(1 for rec in ds.records
                if rec.offset != again.by_id(rec.rid).offset)
    resized = sum(1 for rec in ds.records
                  if rec.stored != again.by_id(rec.rid).stored)
    print("roundtrip ok: %d records, every payload hash matches" % len(source))
    print("  dat %d -> %d bytes (%d unowned spans, %d bytes dropped)"
          % (src_size, dst_size, len(ds.gaps),
             sum(length for _, length in ds.gaps)))
    print("  %d offsets moved, %d stored sizes changed"
          % (moved, resized))
    return 0


def cmd_verify(args):
    ds = DatSet(args.dat)
    bad = 0
    for rec in ds.records:
        try:
            ds.read_payload(rec)
        except ContainerError as err:
            print("FAIL %s" % err)
            bad += 1
    print("%d of %d records read back to their declared payload size"
          % (len(ds.records) - bad, len(ds.records)))
    return 1 if bad else 0


def cmd_digest(args):
    """Print id, name and payload hash for every record, for comparisons."""
    ds = DatSet(args.dat)
    out = {}
    for rec in sorted(ds.records, key=lambda r: r.rid):
        payload = ds.read_payload(rec)
        out["%08x" % rec.rid] = {
            "name": rec.name,
            "payload": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
    text = json.dumps(out, indent=1, sort_keys=True)
    if args.out:
        with open(args.out, "w") as fh:
            fh.write(text + "\n")
        print("wrote %s (%d resources)" % (args.out, len(out)))
    else:
        print(text)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("info", help="summarise a container set")
    p.add_argument("dat")
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("list", help="list records by offset with names")
    p.add_argument("dat")
    p.add_argument("--name", help="only names containing this text")
    p.add_argument("--min-payload", type=int, default=0)
    p.add_argument("--limit", type=int, default=0)
    p.set_defaults(func=cmd_list)

    p = sub.add_parser(
        "inventory",
        help="list every resource with its block kind, size and name")
    p.add_argument("dat")
    p.add_argument("--name", help="only names containing this text")
    p.add_argument("--prefix", help="only names starting with this text")
    p.add_argument("--kind", action="append", choices=KINDS,
                   help="only this block kind (repeatable)")
    p.add_argument("--min-width", type=int)
    p.add_argument("--max-width", type=int)
    p.add_argument("--min-height", type=int)
    p.add_argument("--max-height", type=int)
    p.add_argument("--min-payload", type=int, default=0,
                   help="smallest payload in bytes")
    p.add_argument("--max-payload", type=int,
                   help="largest payload in bytes")
    p.add_argument("--sort", choices=("offset", "id", "name", "size",
                                      "stored"),
                   default="offset", help="size sorts by payload bytes")
    p.add_argument("--reverse", action="store_true",
                   help="sort largest/last first")
    p.add_argument("--format", choices=("table", "json", "tsv"),
                   default="table")
    p.add_argument("--out", help="write the listing here instead of stdout")
    p.add_argument("--limit", type=int, default=0)
    p.set_defaults(func=cmd_inventory)

    p = sub.add_parser("extract", help="write one resource's payload")
    p.add_argument("dat")
    p.add_argument("what", help="resource name or id")
    p.add_argument("--out")
    p.set_defaults(func=cmd_extract)

    p = sub.add_parser("replace", help="write a set with one payload swapped")
    p.add_argument("dat")
    p.add_argument("what", help="resource name or id")
    p.add_argument("payload")
    p.add_argument("--out", required=True)
    p.add_argument("--allow-size-change", action="store_true")
    p.set_defaults(func=cmd_replace)

    p = sub.add_parser("roundtrip",
                       help="rewrite the set and compare every payload hash")
    p.add_argument("dat")
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_roundtrip)

    p = sub.add_parser("verify", help="read every block back")
    p.add_argument("dat")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("digest", help="hash every payload into JSON")
    p.add_argument("dat")
    p.add_argument("--out")
    p.set_defaults(func=cmd_digest)

    p = sub.add_parser(
        "align",
        help="compare the Switch and PC packages by id, and check every PC "
             "block")
    p.add_argument("--switch", required=True, help="the Switch .dat")
    p.add_argument("--pc", required=True, help="the PC .dat")
    p.add_argument("--format", choices=("table", "json"), default="table")
    p.add_argument("--out", help="write the report here instead of stdout")
    p.set_defaults(func=cmd_align)

    p = sub.add_parser(
        "plan",
        help="plan the replacements of the selected category whitelist, or "
             "read a plan back")
    p.add_argument("--switch", help="the Switch .dat, to make a plan")
    p.add_argument("--pc", help="the PC .dat, to make a plan")
    p.add_argument("--read", metavar="PLAN",
                   help="summarise an existing plan instead of making one")
    p.add_argument("--categories",
                   default=",".join(CATEGORY_NAMES),
                   help="comma list of categories to replace, or all "
                        "(default: %(default)s)")
    p.add_argument("--include", action="append", default=[],
                   metavar="PREFIX",
                   help="also replace names starting with this text "
                        "(repeatable)")
    p.add_argument("--exclude", action="append", default=[],
                   metavar="PREFIX",
                   help="never replace names starting with this text "
                        "(repeatable)")
    p.add_argument("--target-size", default="2x",
                   help="2x scales the Switch size, a number caps the longest "
                        "side (default: %(default)s)")
    p.add_argument("--max-size", type=int, default=2048,
                   help="largest side of a replacement (default: "
                        "%(default)s)")
    p.add_argument("--budget",
                   help="largest projected .dat in bytes, with an optional "
                        "K, M, G or T suffix")
    p.add_argument("--out", metavar="PLAN",
                   help="write the plan here when making one")
    p.add_argument("--format", choices=("table", "json"), default="table")
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser(
        "apply",
        help="write the replacement container set a plan describes")
    p.add_argument("--plan", required=True,
                   help="the plan file made by the plan command")
    p.add_argument("--out", required=True,
                   help="directory for the new .dat and .idx")
    p.add_argument("--switch", help="the Switch .dat (default: from the plan)")
    p.add_argument("--pc", help="the PC .dat (default: from the plan)")
    p.add_argument("--quality", default=DEFAULT_QUALITY,
                   help="astcenc quality preset or a number from 0 to 100 "
                        "(default: %(default)s)")
    p.add_argument("--astcenc", help="the astcenc program (default: found on "
                                     "PATH)")
    p.add_argument("--cache-dir",
                   help="where encoded replacements are cached (default: "
                        "%s)" % default_cache_dir())
    p.add_argument("--srgb", action="store_true",
                   help="encode as sRGB instead of linear")
    p.add_argument("--convert-uncompressed", action="store_true",
                   help="also replace blocks the game stores uncompressed "
                        "(RGBA8 and R8); this rewrites the format fields")
    p.add_argument("--fail-fast", action="store_true",
                   help="stop at the first entry that cannot be replaced")
    p.add_argument("--limit", type=int, default=0,
                   help="only the first entries of the plan")
    p.add_argument("--format", choices=("table", "json"), default="table")
    p.set_defaults(func=cmd_apply)

    p = sub.add_parser(
        "apply-bc3",
        help="write the replacement container set a plan describes, "
             "format 72 as BC3 blocks in the block-linear layout")
    p.add_argument("--plan", required=True,
                   help="the plan file made by the plan command")
    p.add_argument("--out", required=True,
                   help="directory for the new .dat and .idx")
    p.add_argument("--switch", help="the Switch .dat (default: from the plan)")
    p.add_argument("--pc", help="the PC .dat (default: from the plan)")
    p.add_argument("--max-size", type=int, default=DEFAULT_BC3_MAX_SIZE,
                   help="largest side of a replacement, which is the PC size "
                        "when it fits and never below the Switch size "
                        "(default: %(default)s)")
    p.add_argument("--pad-to", type=int, metavar="BYTES",
                   help="pad the .dat to this many bytes (default: the size of "
                        "the .dat it replaces, which the loader needs)")
    p.add_argument("--no-pad", action="store_true",
                   help="leave the .dat at its written length; the game will "
                        "refuse a file shorter than the one it replaces")
    p.add_argument("--fail-fast", action="store_true",
                   help="stop at the first entry that cannot be replaced")
    p.add_argument("--limit", type=int, default=0,
                   help="only the first entries of the plan")
    p.add_argument("--format", choices=("table", "json"), default="table")
    p.set_defaults(func=cmd_apply_bc3)

    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except ContainerError as err:
        print("frostpunk-assets: %s" % err, file=sys.stderr)
        return getattr(err, "exit_code", 2)


if __name__ == "__main__":
    sys.exit(main())
