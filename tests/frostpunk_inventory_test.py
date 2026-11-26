"""Tests for the `inventory` listing of scripts/frostpunk-assets.py.

The fixture is built here rather than committed: a real container is two
gigabytes of game data.  Each block kind is constructed from the evidence the
tool is allowed to use, and one block is deliberately unlabelled so the test
can check it is reported as unknown rather than guessed at.
"""
import contextlib
import importlib.util
import io
import json
import os
import struct
import sys
import tempfile
import unittest

try:
    import zstandard
except ImportError:  # pragma: no cover
    zstandard = None

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(os.path.dirname(HERE), "scripts", "frostpunk-assets.py")


def load_module():
    spec = importlib.util.spec_from_file_location("frostpunk_inventory", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def texture_payload(width, height, filler=1000):
    """A Switch texture block: magic at +0, width/height as u16 at +12/+14."""
    header = struct.pack("<IIIHH", 0x001C7865, 72, 1, width, height)
    header += b"\x00" * (28 - len(header))
    return header + b"\xaa" * filler


def descriptor_payload():
    return (b"<TextureArrayDescriptor>\r\n"
            b"  <Properties ClassName=\"TextureArrayDescriptor\"/>\r\n"
            b"</TextureArrayDescriptor>\r\n")


def tga_payload():
    """An 18-byte TGA header, pixels, and the TRUEVISION-XFILE footer."""
    header = struct.pack("<BBBHHBHHHHBB", 0, 0, 2, 0, 0, 0, 0, 0, 32, 32,
                         32, 8)
    footer = b"\x00" * 8 + b"TRUEVISION-XFILE.\x00"
    return header + b"\x01\x02\x03\x04" + b"\x00" * 100 + footer


def build_set(directory, entries, names=None):
    """Write a small .dat/.idx/.str set and return the .dat path.

    `entries` is a list of (id, payload, compress); a false `compress` stores
    the payload raw, as the texture packages do for their TGA blocks.
    """
    names = names or {}
    dat = os.path.join(directory, "set-s3.dat")
    idx = os.path.join(directory, "set-s3.idx")
    st = os.path.join(directory, "set-s3.str")
    blob = bytearray()
    records = []
    for rid, payload, compress in entries:
        stored = (zstandard.ZstdCompressor(level=3).compress(payload)
                  if compress else payload)
        records.append((rid, len(stored), len(payload), len(blob)))
        blob += stored
    with open(dat, "wb") as fh:
        fh.write(blob)
    with open(idx, "wb") as fh:
        fh.write(b"\x00" * 11)
        for rid, stored, payload_size, offset in records:
            fh.write(struct.pack("<IIIIII", rid, stored, 0, payload_size,
                                 0, offset))
            fh.write(struct.pack("<I", 0) + b"\x01")
    if names:
        with open(st, "wb") as fh:
            fh.write(b"\x00" * 11)
            for rid, _, _ in entries:
                name = names.get(rid)
                if name is None:
                    continue
                raw = name.encode()
                fh.write(struct.pack("<II", rid, len(raw)) + raw)
    return dat


def run(fa, argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = fa.main(argv)
    return code, out.getvalue(), err.getvalue()


def data_lines(text):
    return [line for line in text.splitlines() if line.strip()][1:]


@unittest.skipIf(zstandard is None, "needs the zstandard module")
class InventoryTest(unittest.TestCase):
    def setUp(self):
        self.fa = load_module()
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.entries = [
            (0x20, texture_payload(512, 256), True),
            (0x21, descriptor_payload(), True),
            (0x22, tga_payload(), False),
            # looks like a gzip header, which the PC build's blocks also are
            # not; the tool must leave it unknown rather than guess
            (0x23, b"\x1f\x8b\x08\x00" + b"\x00" * 40, True),
            (0x24, texture_payload(64, 64, filler=10), True),
        ]
        self.names = {
            0x20: "terrain/snow.texture",
            0x21: "terrain/snow.texarray",
            0x22: "cursors/arrow.tga",
            0x23: "misc/pc-block.bin",
        }
        self.dat = build_set(self.dir, self.entries, self.names)

    def tearDown(self):
        self.tmp.cleanup()

    def test_every_record_is_listed_and_kind_comes_from_evidence(self):
        code, text, err = run(self.fa, ["inventory", self.dat])
        self.assertEqual(code, 0, err)
        rows = data_lines(text)
        self.assertEqual(len(rows), len(self.entries))
        self.assertIn("5 rows (4 named, 1 unnamed)", err)
        self.assertIn("kinds descriptor=1, texture=2, tga=1, unknown=1", err)
        self.assertIn("2 with width/height", err)

        by_id = {row.split()[0]: row.split() for row in rows}
        self.assertEqual(by_id["0x00000020"][1], "texture")
        self.assertEqual(by_id["0x00000021"][1], "descriptor")
        self.assertEqual(by_id["0x00000022"][1], "tga")
        self.assertEqual(by_id["0x00000023"][1], "unknown")

    def test_width_and_height_only_for_texture_blocks(self):
        code, text, _ = run(self.fa, ["inventory", self.dat])
        self.assertEqual(code, 0)
        by_id = {row.split()[0]: row.split() for row in data_lines(text)}
        self.assertEqual(by_id["0x00000020"][2:4], ["512", "256"])
        self.assertEqual(by_id["0x00000024"][2:4], ["64", "64"])
        for rid in ("0x00000021", "0x00000022", "0x00000023"):
            self.assertEqual(by_id[rid][2:4], ["-", "-"])

    def test_unnamed_resource_is_reported_unnamed_never_invented(self):
        code, text, err = run(self.fa, ["inventory", self.dat])
        self.assertEqual(code, 0)
        rows = data_lines(text)
        unnamed = [row for row in rows if row.split()[0] == "0x00000024"]
        self.assertEqual(len(unnamed), 1)
        self.assertTrue(unnamed[0].split()[-1] == "-")
        self.assertIn("1 unnamed", err)
        # a fully named set is 100% covered with no made-up names
        full = os.path.join(self.dir, "full")
        os.makedirs(full)
        dat = build_set(full, self.entries,
                        {rid: "n/%08x" % rid for rid, _, _ in self.entries})
        code, _, err = run(self.fa, ["inventory", dat])
        self.assertEqual(code, 0)
        self.assertIn("5 rows (5 named, 0 unnamed)", err)

    def test_filters_by_name_prefix_kind_and_size(self):
        code, text, err = run(self.fa, ["inventory", self.dat, "--name", "snow"])
        self.assertEqual(code, 0)
        self.assertEqual([r.split()[0] for r in data_lines(text)],
                         ["0x00000020", "0x00000021"])
        self.assertIn("texture=1", err)

        code, text, _ = run(self.fa, ["inventory", self.dat,
                                      "--prefix", "cursors/"])
        self.assertEqual([r.split()[0] for r in data_lines(text)],
                         ["0x00000022"])

        code, text, _ = run(self.fa, ["inventory", self.dat, "--kind", "tga"])
        self.assertEqual([r.split()[0] for r in data_lines(text)],
                         ["0x00000022"])

        code, text, _ = run(self.fa, ["inventory", self.dat,
                                      "--min-width", "512"])
        self.assertEqual([r.split()[0] for r in data_lines(text)],
                         ["0x00000020"])

        code, text, _ = run(self.fa, ["inventory", self.dat,
                                      "--max-payload", "200"])
        shown = {r.split()[0] for r in data_lines(text)}
        self.assertNotIn("0x00000020", shown)   # 1028 bytes
        self.assertIn("0x00000022", shown)      # 144 bytes

    def test_sorted_by_payload_size(self):
        code, text, _ = run(self.fa, ["inventory", self.dat,
                                      "--sort", "size", "--reverse"])
        self.assertEqual(code, 0)
        rows = data_lines(text)
        self.assertEqual(rows[0].split()[0], "0x00000020")   # 1028 bytes
        sizes = [int(row.split()[5]) for row in rows]
        self.assertEqual(sizes, sorted(sizes, reverse=True))
        self.assertEqual(rows[-1].split()[0], "0x00000024")  # 38 bytes

    def test_json_export(self):
        out = os.path.join(self.dir, "rows.json")
        code, text, err = run(self.fa, ["inventory", self.dat,
                                        "--format", "json", "--out", out])
        self.assertEqual(code, 0, err)
        self.assertIn("wrote %s (5 rows)" % out, text)
        with open(out) as fh:
            rows = json.load(fh)
        self.assertEqual(len(rows), 5)
        by_id = {row["id"]: row for row in rows}
        self.assertEqual(by_id[0x20]["kind"], "texture")
        self.assertEqual((by_id[0x20]["width"], by_id[0x20]["height"]),
                         (512, 256))
        self.assertEqual(by_id[0x22]["kind"], "tga")
        self.assertIsNone(by_id[0x22]["width"])
        self.assertIsNone(by_id[0x24]["name"])
        self.assertEqual(by_id[0x23]["kind"], "unknown")

    def test_tsv_export(self):
        out = os.path.join(self.dir, "rows.tsv")
        code, _, err = run(self.fa, ["inventory", self.dat,
                                     "--format", "tsv", "--out", out])
        self.assertEqual(code, 0, err)
        with open(out) as fh:
            lines = fh.read().splitlines()
        self.assertEqual(lines[0], "id\tname\tkind\twidth\theight\toffset"
                                   "\tpayload\tstored")
        self.assertEqual(len(lines), 6)
        fields = {line.split("\t")[0]: line.split("\t") for line in lines[1:]}
        self.assertEqual(fields["0x00000020"][2:5],
                         ["texture", "512", "256"])
        self.assertEqual(fields["0x00000022"][2:5], ["tga", "", ""])
        self.assertEqual(fields["0x00000024"][1], "")


if __name__ == "__main__":
    unittest.main()
