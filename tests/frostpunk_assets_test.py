"""Tests for scripts/frostpunk-assets.py.

The fixtures are built here rather than committed, because a real container is
two gigabytes of game data.
"""
import importlib.util
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
    spec = importlib.util.spec_from_file_location("frostpunk_assets", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build_set(directory, payloads, names=None, gap=0, compress=True):
    """Write a small .dat/.idx/.str set and return the .dat path."""
    names = names or {}
    dat = os.path.join(directory, "set-s3.dat")
    idx = os.path.join(directory, "set-s3.idx")
    st = os.path.join(directory, "set-s3.str")
    blob = bytearray()
    records = []
    for rid, payload in payloads:
        stored = (zstandard.ZstdCompressor(level=3).compress(payload)
                  if compress else payload)
        records.append((rid, len(stored), len(payload), len(blob)))
        blob += stored
        if gap and rid != payloads[-1][0]:
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
            for rid, payload in payloads:
                name = names.get(rid, "").encode()
                fh.write(struct.pack("<II", rid, len(name)) + name)
    return dat


@unittest.skipIf(zstandard is None, "needs the zstandard module")
class ContainerTest(unittest.TestCase):
    def setUp(self):
        self.fa = load_module()
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.payloads = [(0x10, b"A" * 100), (0x11, b"B" * 50),
                         (0x12, b"C" * 10)]
        self.names = {0x10: "ui/one.texture", 0x11: "ui/two.texture",
                      0x12: "buildings/three.texture"}

    def tearDown(self):
        self.tmp.cleanup()

    def test_reads_offsets_and_payloads(self):
        dat = build_set(self.dir, self.payloads, self.names)
        ds = self.fa.DatSet(dat)
        self.assertEqual(len(ds.records), 3)
        self.assertEqual(ds.by_name("ui/two.texture").rid, 0x11)
        for rid, payload in self.payloads:
            self.assertEqual(ds.read_payload(ds.by_id(rid)), payload)

    def test_unowned_gap_is_reported_not_read(self):
        dat = build_set(self.dir, self.payloads, self.names, gap=7)
        ds = self.fa.DatSet(dat)
        self.assertEqual(len(ds.gaps), 2)
        for _, length in ds.gaps:
            self.assertGreaterEqual(length, 7)
        self.assertEqual(ds.read_payload(ds.by_id(0x12)), b"C" * 10)

    def test_rewrite_keeps_every_payload(self):
        dat = build_set(self.dir, self.payloads, self.names, gap=5)
        ds = self.fa.DatSet(dat)
        out = os.path.join(self.dir, "out")
        new_dat, _ = ds.write(out)
        again = self.fa.DatSet(new_dat)
        self.assertEqual(len(again.records), 3)
        self.assertEqual(again.gaps, [])
        for rid, payload in self.payloads:
            self.assertEqual(again.read_payload(again.by_id(rid)), payload)

    def test_replace_refuses_a_different_size(self):
        dat = build_set(self.dir, self.payloads, self.names)
        blob = os.path.join(self.dir, "new.bin")
        with open(blob, "wb") as fh:
            fh.write(b"Z" * 99)
        out = os.path.join(self.dir, "out")
        code = self.fa.main(["replace", dat, "0x10", blob, "--out", out])
        self.assertEqual(code, 2)
        code = self.fa.main(["replace", dat, "0x10", blob, "--out", out,
                             "--allow-size-change"])
        self.assertEqual(code, 0)
        again = self.fa.DatSet(os.path.join(out, "set-s3.dat"))
        self.assertEqual(again.read_payload(again.by_id(0x10)), b"Z" * 99)
        self.assertEqual(again.read_payload(again.by_id(0x11)), b"B" * 50)

    def test_record_running_past_the_data_names_the_id(self):
        dat = build_set(self.dir, self.payloads, self.names)
        idx = dat[:-4] + ".idx"
        with open(idx, "r+b") as fh:                 # third record too long
            fh.seek(11 + 2 * 29 + 4)
            fh.write(struct.pack("<I", 1 << 20))
        with self.assertRaises(self.fa.ContainerError) as caught:
            self.fa.DatSet(dat)
        self.assertIn("00000012", str(caught.exception))

    def test_index_that_does_not_multiply_out(self):
        dat = build_set(self.dir, self.payloads, self.names)
        idx = dat[:-4] + ".idx"
        with open(idx, "ab") as fh:
            fh.write(b"\x00" * 3)
        with self.assertRaises(self.fa.ContainerError) as caught:
            self.fa.DatSet(dat)
        self.assertIn("multiple of 29", str(caught.exception))

    def test_missing_name_is_named(self):
        dat = build_set(self.dir, self.payloads, self.names)
        ds = self.fa.DatSet(dat)
        with self.assertRaises(self.fa.ContainerError) as caught:
            ds.by_name("ui/nope.texture")
        self.assertIn("ui/nope.texture", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
