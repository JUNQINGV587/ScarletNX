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
import json
import os
import struct
import sys
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

    def by_id(self, rid):
        for rec in self.records:
            if rec.rid == rid:
                return rec
        raise ContainerError(self.idx_path, "no record with id 0x%08x" % rid)

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
    def write(self, out_dir, replacements=None, compress_level=18):
        """Write the set to `out_dir`, optionally replacing payloads.

        `replacements` maps a record to its new payload bytes.  Blocks are
        rewritten in their original order; a block that grows or shrinks moves
        the blocks after it, so the index is rebuilt with the new offsets and
        the leftover bytes of the original file are kept out.
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

    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except ContainerError as err:
        print("frostpunk-assets: %s" % err, file=sys.stderr)
        return getattr(err, "exit_code", 2)


if __name__ == "__main__":
    sys.exit(main())
