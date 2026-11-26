"""The container round-trip check: rewrite a set, read it back, compare hashes.

A round-trip may drop unowned spans and recompute block offsets; it may not
change ids, names, payload bytes or payload sizes.  The fixture is built here
rather than committed, because a real container is two gigabytes of game data.
"""
import contextlib
import importlib.util
import io
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
    spec = importlib.util.spec_from_file_location("frostpunk_assets_rt", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build_set(directory, entries, names=None, gap=0):
    """Write a small .dat/.idx/.str set and return the .dat path.

    `entries` is a list of (id, payload, compress); a false `compress` stores
    the payload raw, as the texture packages do for TGA blocks.
    """
    names = names or {}
    dat = os.path.join(directory, "set-s3.dat")
    idx = os.path.join(directory, "set-s3.idx")
    st = os.path.join(directory, "set-s3.str")
    blob = bytearray()
    records = []
    for i, (rid, payload, compress) in enumerate(entries):
        stored = (zstandard.ZstdCompressor(level=3).compress(payload)
                  if compress else payload)
        records.append((rid, len(stored), len(payload), len(blob)))
        blob += stored
        if gap and i != len(entries) - 1:
            blob += b"\x00" * gap
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
                name = names.get(rid, "").encode()
                fh.write(struct.pack("<II", rid, len(name)) + name)
    return dat


def run(main, argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = main(argv)
    return code, out.getvalue()


@unittest.skipIf(zstandard is None, "needs the zstandard module")
class RoundTripTest(unittest.TestCase):
    def setUp(self):
        self.fa = load_module()
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.entries = [(0x10, b"A" * 300, True),
                        (0x11, b"B" * 120, True),
                        (0x12, b"C" * 40, False)]
        self.names = {0x10: "ui/one.texture", 0x11: "ui/two.texture",
                      0x12: "buildings/raw.tga"}

    def tearDown(self):
        self.tmp.cleanup()

    def test_roundtrip_keeps_ids_names_payloads_and_sizes(self):
        dat = build_set(self.dir, self.entries, self.names, gap=9)
        out = os.path.join(self.dir, "out")
        code, text = run(self.fa.main, ["roundtrip", dat, "--out", out])
        self.assertEqual(code, 0, text)
        self.assertIn("roundtrip ok: 3 records", text)

        source = self.fa.DatSet(dat)
        again = self.fa.DatSet(os.path.join(out, "set-s3.dat"))
        self.assertEqual(len(again.records), len(source.records))
        for rid, payload, _ in self.entries:
            rec = again.by_id(rid)
            self.assertEqual(rec.name, self.names[rid])
            self.assertEqual(again.read_payload(rec), payload)
        # the unowned spans are dropped, so the rewritten .dat is smaller
        self.assertLess(os.path.getsize(os.path.join(out, "set-s3.dat")),
                        os.path.getsize(dat))
        self.assertEqual(again.gaps, [])

    def test_corrupt_block_is_named_by_resource_id(self):
        dat = build_set(self.dir, self.entries, self.names)
        ds = self.fa.DatSet(dat)
        rec = ds.by_id(0x11)
        self.assertNotEqual(rec.stored, rec.payload)
        with open(dat, "r+b") as fh:          # destroy the zstd frame magic
            fh.seek(rec.offset)
            fh.write(b"\x00\x00\x00\x00")
        out = os.path.join(self.dir, "out")
        code, text = run(self.fa.main, ["roundtrip", dat, "--out", out])
        self.assertEqual(code, 1, text)
        self.assertIn("00000011", text)
        self.assertIn("do not read back", text)
        self.assertFalse(os.path.exists(out))


if __name__ == "__main__":
    unittest.main()
