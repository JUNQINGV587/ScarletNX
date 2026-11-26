#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the ScarletNX contributors
"""Tests for the `align` report and the `plan` file of frostpunk-assets.py.

The fixture is built here rather than committed, because a real container is
gigabytes of game data.  It holds every case the ticket names: an id in both
packages, an id only on Switch, an id only on PC, a PC block that does not
decompress to its declared size, a texture the whitelist leaves out, a PC
block that is not an image, a PC source that is smaller than the Switch
texture, and a non-power-of-two source.  The sizes are chosen so the expected
ASTC estimate can be worked out by hand and asserted exactly.

Every assertion is about what the command prints or what a plan file holds;
nothing reads the tool's internals except to parse a plan back, which is the
contract `--read` promises to another program.
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
import zlib

try:
    import zstandard
except ImportError:  # pragma: no cover
    zstandard = None

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(os.path.dirname(HERE), "scripts", "frostpunk-assets.py")


def load_module():
    spec = importlib.util.spec_from_file_location("frostpunk_plan", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def texture_payload(width, height, filler=200):
    """A Switch texture block: magic at +0, width/height as u16 at +12/+14."""
    header = struct.pack("<IIIHH", 0x001C7865, 72, 1, width, height)
    header += b"\x00" * (28 - len(header))
    return header + b"\xaa" * filler


def descriptor_payload():
    return (b"<TextureArrayDescriptor>\r\n"
            b"  <Properties ClassName=\"TextureArrayDescriptor\"/>\r\n"
            b"</TextureArrayDescriptor>\r\n")


def pc_image_payload(width, height, tag=b"DXT5"):
    """A single-level PC image payload: header, one level slot, pixels.

    `tag` is a block format, or `None` for raw pixels, which the PC build
    marks with the numeric format id 21 and stores at 4 bytes per pixel.
    """
    if tag is None:
        tag = struct.pack("<I", 21)
        nbytes = width * height * 4
    else:
        blocks = 8 if tag == b"DXT1" else 16
        nbytes = ((width + 3) // 4) * ((height + 3) // 4) * blocks
    table = struct.pack("<HHI", width, height, nbytes)
    table += b"\x00" * (8 * 15)
    return (struct.pack("<II", width, height) + tag + struct.pack("<I", 1)
            + table + b"\x5a" * nbytes)


def gzip_bytes(payload):
    obj = zlib.compressobj(6, zlib.DEFLATED, 31)
    return obj.compress(payload) + obj.flush()


def stored_block(payload, compress):
    """The bytes a block takes: `gzip`, `zstd`, or `None` for raw."""
    if compress == "gzip":
        return gzip_bytes(payload)
    if compress == "zstd":
        return zstandard.ZstdCompressor(level=3).compress(payload)
    return payload


def build_set(directory, blocks, names=None, basename="set-s3"):
    """Write a .dat/.idx/.str set and return the .dat path.

    `blocks` is a list of (id, payload, compress, declared), where `declared`
    overrides the payload size the index claims, which is how an unreadable
    block is made without touching the file.
    """
    names = names or {}
    os.makedirs(directory, exist_ok=True)
    dat = os.path.join(directory, basename + ".dat")
    idx = os.path.join(directory, basename + ".idx")
    st = os.path.join(directory, basename + ".str")
    blob = bytearray()
    records = []
    for rid, payload, compress, declared in blocks:
        stored = stored_block(payload, compress)
        records.append((rid, len(stored),
                        len(payload) if declared is None else declared,
                        len(blob)))
        blob += stored
    with open(dat, "wb") as fh:
        fh.write(blob)
    with open(idx, "wb") as fh:
        fh.write(b"\x00" * 11)
        for rid, stored, payload_size, offset in records:
            fh.write(struct.pack("<IIIIII", rid, stored, 0, payload_size,
                                 0, offset & 0xffffffff))
            fh.write(struct.pack("<I", offset >> 32) + b"\x01")
    if names:
        with open(st, "wb") as fh:
            fh.write(b"\x00" * 11)
            for rid in sorted(names):
                raw = names[rid].encode()
                fh.write(struct.pack("<II", rid, len(raw)) + raw)
    return dat


def run(fa, argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = fa.main(argv)
    return code, out.getvalue(), err.getvalue()


def group_row(text, name):
    """The numbers of one group line in an alignment table."""
    for line in text.splitlines():
        if line.startswith(name + " "):
            return line.split()
    raise AssertionError("no group row starts with %r in:\n%s" % (name, text))


class Fixture(unittest.TestCase):
    """One Switch set and one PC set, built from evidence for every case."""

    def setUp(self):
        self.fa = load_module()
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        switch = [
            (0x100, texture_payload(256, 128), "zstd", None),
            (0x101, texture_payload(512, 512), "zstd", None),
            (0x102, texture_payload(128, 128), "zstd", None),
            (0x103, texture_payload(64, 64), "zstd", None),
            (0x104, texture_payload(256, 128), "zstd", None),
            (0x106, texture_payload(64, 64), "zstd", None),
            (0x107, texture_payload(64, 64), "zstd", None),
            # only on Switch: it is listed and left alone
            (0x200, texture_payload(128, 64), "zstd", None),
        ]
        pc = [
            (0x100, pc_image_payload(500, 250, None), "gzip", None),
            (0x101, pc_image_payload(1024, 1024), "gzip", None),
            (0x102, pc_image_payload(500, 500, None), "gzip", None),
            (0x103, pc_image_payload(512, 512), "gzip", None),
            # smaller than the Switch texture, so a replacement is refused
            (0x104, pc_image_payload(128, 64, None), "gzip", None),
            # the index says 4096 bytes and the block yields 64
            (0x106, b"\x00" * 64, "gzip", 4096),
            (0x107, descriptor_payload(), "gzip", None),
            # only on PC: it has no name and is listed and left alone
            (0x300, pc_image_payload(64, 64), "gzip", None),
        ]
        names = {
            0x100: "environment/terrain/snow_ground_col.texture",
            0x101: "environment/rocks/granite_nor.texture",
            0x102: "buildings/town_hall_col.texture",
            0x103: "ui/hud_button.texture",
            0x104: "environment/terrain/flat_col.texture",
            0x106: "environment/terrain/broken_col.texture",
            0x107: "environment/terrain/array.texarray",
            0x200: "environment/terrain/lost_col.texture",
        }
        self.switch_dat = build_set(os.path.join(self.dir, "switch"),
                                    switch, names, "textures-s3")
        self.pc_dat = build_set(os.path.join(self.dir, "pc"), pc, {},
                                "textures-s3")
        self.plan_path = os.path.join(self.dir, "plan.jsonl")

    def tearDown(self):
        self.tmp.cleanup()

    def align(self, *extra):
        return run(self.fa, ["align", "--switch", self.switch_dat,
                             "--pc", self.pc_dat] + list(extra))

@unittest.skipIf(zstandard is None, "needs the zstandard module")
class AlignmentTest(Fixture):
    def test_alignment_counts_every_class(self):

        code, out, err = self.align()
        self.assertEqual(0, code, err)
        self.assertIn("7 in both, 1 only on Switch, 1 only on PC", out)
        self.assertIn("only on Switch, left alone (1)", out)
        self.assertIn("0x00000200", out)
        self.assertIn("environment/terrain/lost_col.texture", out)
        self.assertIn("only on PC, left alone (1)", out)
        self.assertIn("0x00000300", out)

    def test_alignment_table_counts_readable_blocks_per_group(self):
        code, out, err = self.align()
        self.assertEqual(0, code, err)
        # environment: 5 PC blocks, of which 3 images, 1 descriptor and 1
        # that does not decompress to its declared size
        self.assertEqual(
            ["environment", "5", "5", "1", "0", "4", "3", "1", "1"],
            group_row(out, "environment"))
        self.assertEqual(
            ["buildings", "1", "1", "0", "0", "1", "1", "0", "0"],
            group_row(out, "buildings"))
        self.assertEqual(
            ["ui", "1", "1", "0", "0", "1", "1", "0", "0"],
            group_row(out, "ui"))
        self.assertEqual(
            ["(unnamed)", "1", "0", "0", "1", "1", "1", "0", "0"],
            group_row(out, "(unnamed)"))
        self.assertEqual(
            ["(total)", "8", "7", "1", "1", "7", "6", "1", "1"],
            group_row(out, "(total)"))
        self.assertIn("pc blocks by kind: dxt5 3, raw4 3, descriptor 1", out)
        self.assertIn("images: 6, 2 of them not a power of two", out)

    def test_alignment_json_is_machine_readable(self):
        code, out, err = self.align("--format", "json")
        self.assertEqual(0, code, err)
        report = json.loads(out)
        self.assertEqual(7, report["both"])
        self.assertEqual(1, report["switch_only"])
        self.assertEqual(1, report["pc_only"])
        self.assertEqual(8, report["pc"]["records"])
        self.assertEqual(2, report["non_power_of_two"])

    def test_alignment_can_be_written_to_a_file(self):
        report = os.path.join(self.dir, "align.txt")
        code, out, err = self.align("--out", report)
        self.assertEqual(0, code, err)
        self.assertEqual("", out)
        with open(report) as fh:
            self.assertIn("7 in both", fh.read())


@unittest.skipIf(zstandard is None, "needs the zstandard module")
class PlanTest(Fixture):
    def plan(self, *extra):
        return run(self.fa, ["plan", "--switch", self.switch_dat,
                             "--pc", self.pc_dat, "--out", self.plan_path]
                   + list(extra))

    def test_plan_replaces_only_the_whitelisted_image_textures(self):
        code, out, err = self.plan()
        self.assertEqual(0, code, err)
        self.assertIn("entries   3 replaced", out)
        with open(self.plan_path) as fh:
            header = json.loads(fh.readline())
        self.assertEqual("frostpunk-assets/replace-plan", header["plan"])
        self.assertEqual(1, header["version"])
        self.assertEqual(["terrain", "buildings", "rocks"],
                         header["categories"])
        _, entries = self.fa.read_plan(self.plan_path)
        by_id = {entry["id"]: entry for entry in entries}
        self.assertEqual(["terrain", "rocks", "buildings"],
                         [by_id["0x00000100"]["category"],
                          by_id["0x00000101"]["category"],
                          by_id["0x00000102"]["category"]])
        # the ui texture is not in the whitelist: no entry, not even a skip
        self.assertNotIn("0x00000103", by_id)
        self.assertIn("skipped 1: would be 128x64, smaller than the Switch "
                      "texture 256x128", err)
        self.assertIn("skipped 1: the PC block is unreadable", err)
        self.assertIn("skipped 1: the PC block does not prove an image "
                      "payload", err)

    def test_estimate_uses_the_astc_cost_of_the_target(self):
        code, out, err = self.plan()
        self.assertEqual(0, code, err)
        _, entries = self.fa.read_plan(self.plan_path)
        by_id = {entry["id"]: entry for entry in entries}
        # 2x of 256x128 is 512x256, capped by the 500x250 PC source; the ASTC
        # cost rounds up per 4x4 block: 28 + 125*63*16
        self.assertEqual([500, 250], by_id["0x00000100"]["target"])
        self.assertEqual(126028, by_id["0x00000100"]["estimated"])
        # a full power of two: 28 + 256*256*16
        self.assertEqual([1024, 1024], by_id["0x00000101"]["target"])
        self.assertEqual(1048604, by_id["0x00000101"]["estimated"])
        # 2x of 128x128
        self.assertEqual(28 + 64 * 64 * 16,
                         by_id["0x00000102"]["estimated"])

    def test_a_non_power_of_two_source_is_not_an_error(self):
        code, out, err = self.plan()
        self.assertEqual(0, code, err)
        _, entries = self.fa.read_plan(self.plan_path)
        entry = [e for e in entries if e["id"] == "0x00000100"][0]
        self.assertEqual([500, 250], entry["target"])
        self.assertEqual(126028, entry["estimated"])

    def test_target_size_policy_is_configurable(self):
        code, out, err = self.plan("--target-size", "1x", "--max-size",
                                   "1024")
        self.assertEqual(0, code, err)
        _, entries = self.fa.read_plan(self.plan_path)
        entry = [e for e in entries if e["id"] == "0x00000101"][0]
        self.assertEqual([512, 512], entry["target"])
        self.assertEqual(28 + 128 * 128 * 16, entry["estimated"])

    def test_a_plan_reads_back_and_a_deleted_line_shrinks_the_estimate(self):
        code, out, err = self.plan("--budget", "8M")
        self.assertEqual(0, code, err)
        header, entries = self.fa.read_plan(self.plan_path)
        before = self.fa.plan_summary(header, entries)
        victim = max(entries, key=lambda entry: entry["estimated"])
        with open(self.plan_path) as fh:
            lines = fh.readlines()
        kept = [lines[0], "# a note a person added\n"]
        kept += [line for line in lines[1:]
                 if json.loads(line)["id"] != victim["id"]]
        with open(self.plan_path, "w") as fh:
            fh.writelines(kept)
        header, entries = self.fa.read_plan(self.plan_path)
        after = self.fa.plan_summary(header, entries)
        self.assertEqual(2, after["count"])
        self.assertEqual(before["projected"] - victim["delta"],
                         after["projected"])
        code, out, err = run(self.fa, ["plan", "--read", self.plan_path])
        self.assertEqual(0, code, err)
        self.assertIn("entries   2 replaced", out)

    def test_reading_a_plan_rechecks_the_budget(self):
        code, out, err = self.plan("--budget", "8M")
        self.assertEqual(0, code, err)
        code, out, err = run(self.fa, ["plan", "--read", self.plan_path])
        self.assertEqual(0, code, err)
        self.assertIn("under the budget of 8,388,608", out)

    def test_an_over_budget_plan_is_refused_and_writes_nothing(self):
        code, out, err = self.plan("--budget", "8M")
        self.assertEqual(0, code, err)
        header, entries = self.fa.read_plan(self.plan_path)
        projected = self.fa.plan_summary(header, entries)["projected"]
        os.remove(self.plan_path)
        budget = 1000000
        code, out, err = self.plan("--budget", str(budget))
        self.assertEqual(3, code)
        self.assertIn("over budget by %s bytes" % format(projected - budget,
                                                         ","), err)
        self.assertIn("nothing written", err)
        self.assertFalse(os.path.exists(self.plan_path))

    def test_a_plan_needs_both_packages(self):
        code, out, err = run(self.fa, ["plan", "--switch", self.switch_dat,
                                       "--out", self.plan_path])
        self.assertEqual(2, code)
        self.assertIn("needs --switch and --pc", err)


@unittest.skipIf(zstandard is None, "needs the zstandard module")
class HighOffsetTest(unittest.TestCase):
    """A block above 4 GiB is addressed by the two 32-bit halves of its offset.

    The PC package is one of these, and reading only the low half made two
    thousand blocks look unreadable, so the fixture is a sparse file with one
    record past 4 GiB.
    """

    def test_a_block_above_four_gibabytes_is_found(self):
        fa = load_module()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        dat = os.path.join(tmp.name, "high-s3.dat")
        payload = texture_payload(64, 64, filler=16)
        stored = zstandard.ZstdCompressor(level=3).compress(payload)
        base = 0x100000000 + 4096
        try:
            with open(dat, "wb") as fh:
                fh.seek(base)
                fh.write(stored)
        except OSError as err:
            self.skipTest("this file system has no sparse file: %s" % err)
        idx = os.path.join(tmp.name, "high-s3.idx")
        with open(idx, "wb") as fh:
            fh.write(b"\x00" * 11)
            fh.write(struct.pack("<IIIIII", 0x55, len(stored), 0,
                                 len(payload), 0, base & 0xffffffff))
            fh.write(struct.pack("<I", base >> 32) + b"\x01")
        ds = fa.DatSet(dat)
        self.assertEqual([0x55], [rec.rid for rec in ds.records])
        self.assertEqual(base, ds.records[0].offset)
        self.assertEqual(payload, ds.read_payload(ds.records[0]))
        out = os.path.join(tmp.name, "out")
        ds.write(out)
        back = fa.DatSet(os.path.join(out, "high-s3.dat"))
        self.assertEqual(payload, back.read_payload(back.records[0]))


if __name__ == "__main__":
    unittest.main()
