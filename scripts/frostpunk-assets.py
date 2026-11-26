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
        +20 u32 offset of the block in .dat
        +24 u32 zero
        +28 u8  flag, 1 on every resource seen so far

  .dat  the blocks, in offset order; a block is a zstd frame when it starts
        with 28 b5 2f fd and stored raw otherwise, and decompresses to exactly
        the record's payload size.  Some bytes between blocks are not owned by
        any record (leftovers of a patch); this tool reproduces them as-is and
        keeps the offsets of the blocks that follow.

  .str  header 11 bytes, then records of u32 id, u32 name length, name bytes.
"""
import argparse
import hashlib
import json
import os
import struct
import sys

try:
    import zstandard
except ImportError:  # pragma: no cover - the tests skip without it
    zstandard = None

IDX_MAGIC_SIZE = 11
RECORD_SIZE = 29
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
TEXTURE_MAGIC = b"\x65\x78\x1c\x00"
TEXTURE_HEADER_SIZE = 28
DESCRIPTOR_TAG = b"<TextureArrayDescriptor"
TGA_FOOTER = b"TRUEVISION-XFILE"
KINDS = ("texture", "descriptor", "tga", "unknown")


def _u16(buf, off):
    return struct.unpack_from("<H", buf, off)[0]


def _u32(buf, off):
    return struct.unpack_from("<I", buf, off)[0]


def _p32(value):
    return struct.pack("<I", value)


class ContainerError(Exception):
    """A container that does not match the format, named precisely."""

    def __init__(self, where, why):
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
            self.records.append(Record(_u32(rec, 0), _u32(rec, 4),
                                       _u32(rec, 12), _u32(rec, 20), rec[28]))
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
            out += _p32(rec.rid) + _p32(rec.stored) + _p32(0) + \
                _p32(rec.payload) + _p32(0) + _p32(offsets[rec.rid]) + \
                _p32(0) + bytes([rec.flag])
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


def describe_payload(payload):
    """The block kind and, for a texture, its declared width and height.

    A kind is only reported when the bytes carry evidence for it:

      texture     the 28-byte header the Switch build writes, magic `0x001c7865`
                  at +0 and width/height as u16 at +12/+14;
      descriptor  a Stingray XML `<TextureArrayDescriptor>` document, which
                  starts with that root element;
      tga         an uncompressed TGA, whose 26-byte footer carries the
                  `TRUEVISION-XFILE` signature;
      unknown     anything else.  A block whose header is not one of the three
                  is not guessed at.

    Only texture blocks declare a size here; the other kinds return `None`.
    """
    if len(payload) >= TEXTURE_HEADER_SIZE and payload[:4] == TEXTURE_MAGIC:
        return "texture", _u16(payload, 12), _u16(payload, 14)
    if payload.startswith(DESCRIPTOR_TAG):
        return "descriptor", None, None
    if TGA_FOOTER in payload[-32:]:
        return "tga", None, None
    return "unknown", None, None


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

    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except ContainerError as err:
        print("frostpunk-assets: %s" % err, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
