#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the ScarletNX contributors
"""Tests for the `apply-bc3` command of frostpunk-assets.py.

The fixture is a small container set built here, because a real one is
gigabytes of game data.  It holds the cases the ticket names: an 8 by 8
format-72 texture whose pixels are a BC3 chain in the block-linear layout, a
texture of another format, a texture whose pixel count does not match that
layout, a PC image of the same size, a PC image smaller than the Switch
texture, and a PC image that is raw pixels rather than blocks.  The pixels are
built from flat 4 by 4 blocks, so the expected result of every decode can be
worked out by hand and asserted exactly.

The layout is checked against the level bytes of a real 1024 by 1024 texture,
which the ticket measured: 1,048,576 + 262,144 + 65,536 + 16,384 + 4,096 +
1,024 + 512 + 512 + 512 + 512 + 512 = 1,400,320 bytes, with the block height
shrinking to 3, 2, 1 and 0 as the levels get small.  Every assertion is about
what the command prints, what the new container holds, or what the layout
gives back.
"""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import struct
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(os.path.dirname(HERE), "scripts", "frostpunk-assets.py")
TEST_ID = 0x0c0ffee1
SECOND_ID = 0x0c0ffee2

# Flat colours of one 4 by 4 block each, which are exact in 5-6-5 up to the
# rounding of that format, so a decode differs from them by at most 4.
RED = (255, 0, 0, 255)
GREEN = (0, 255, 0, 255)
BLUE = (0, 0, 255, 255)
WHITE = (255, 255, 255, 255)
GREY = (128, 128, 128, 255)


def load_module():
    spec = importlib.util.spec_from_file_location("frostpunk_apply_bc3", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _rgb565(red, green, blue):
    return ((red >> 3) << 11) | ((green >> 2) << 5) | (blue >> 3)


def dxt5_block(colour):
    """One DXT5 block of one colour: alpha endpoints equal, index zero."""
    value = _rgb565(colour[0], colour[1], colour[2])
    return (bytes([colour[3], colour[3]]) + b"\x00" * 6 +
            struct.pack("<HH", value, value) + b"\x00" * 4)


def flat_image(width, height, colour):
    """An RGBA image of one colour, in reading order."""
    return bytes(colour) * (width * height)


def quad_image():
    """An 8 by 8 RGBA image of four flat 4 by 4 blocks, in reading order."""
    pixels = bytearray()
    for row in range(8):
        for column in range(8):
            if row < 4:
                colour = RED if column < 4 else GREEN
            else:
                colour = BLUE if column < 4 else WHITE
            pixels += bytes(colour)
    return bytes(pixels)


def pc_image(levels, tag=b"DXT5"):
    """A PC image payload from (width, height, pixels) levels, smallest first.

    `tag` is a four-byte block format, or `None` for raw pixels, which the PC
    build marks with the numeric format id 21 and stores at four bytes each.
    """
    header = struct.pack("<II", levels[-1][0], levels[-1][1])
    header += struct.pack("<I", 21) if tag is None else tag
    header += struct.pack("<I", len(levels))
    for width, height, pixels in levels:
        header += struct.pack("<HHI", width, height, len(pixels))
    header += b"\x00" * (8 * (16 - len(levels)))
    return header + b"".join(pixels for _, _, pixels in levels)


def pc_dxt5(width, height, colours=None):
    """A one-level PC image of DXT5 blocks, one colour per 4 by 4 block."""
    blocks = ((width + 3) // 4) * ((height + 3) // 4)
    if colours is None:
        colours = [WHITE] * blocks
    body = b"".join(dxt5_block(colour) for colour in colours)
    return pc_image([(width, height, body)])


def switch_block(width, height, fmt=72, mips=1, descriptor=b"\x02\x03\x04\x05",
                 pixel_bytes=None, body=None):
    """A Switch texture block: the 28-byte header and the pixels it describes."""
    if body is None:
        blocks = ((width + 3) // 4) * ((height + 3) // 4)
        body = b"\x5c" * (blocks * 16)
    header = struct.pack("<IIIHH", 0x001C7865, fmt, 1, width, height)
    header += struct.pack("<HBB", 1, 1, mips) + descriptor
    header += struct.pack("<I", len(body) if pixel_bytes is None else pixel_bytes)
    return header + body


def bc3_switch_block(fa, width, height, fill=0x5C):
    """A format-72 block whose pixel bytes are the chain that layout takes."""
    body = bytes([fill]) * fa.bc3_chain_bytes(width, height)
    return switch_block(width, height, fmt=72,
                        mips=fa.bc3_levels(width, height), body=body)


def build_set(directory, blocks, basename="set-s3"):
    """Write a set of stored payloads with its index and names, and return it.

    The blocks are stored as they are, which the reader takes as a block with
    no compression, so the fixture needs nothing but the standard library.
    """
    os.makedirs(directory, exist_ok=True)
    dat = os.path.join(directory, basename + ".dat")
    idx = os.path.join(directory, basename + ".idx")
    names = os.path.join(directory, basename + ".str")
    offsets = []
    with open(dat, "wb") as fh:
        for _rid, payload, _name in blocks:
            offsets.append(fh.tell())
            fh.write(payload)
    with open(idx, "wb") as fh:
        fh.write(b"\x00" * 11)
        for (rid, payload, _name), offset in zip(blocks, offsets):
            fh.write(struct.pack("<IIIIII", rid, len(payload), 0, len(payload),
                                 0, offset & 0xffffffff))
            fh.write(struct.pack("<I", offset >> 32) + b"\x01")
    with open(names, "wb") as fh:
        fh.write(b"\x00" * 11)
        for rid, _payload, name in sorted(blocks, key=lambda row: row[0]):
            raw = name.encode()
            fh.write(struct.pack("<II", rid, len(raw)) + raw)
    return dat


def plan_entry(fa, rid, name, switch, pc, target, switch_bytes=0, estimated=0):
    """One plan line, with every key a plan line has to carry."""
    entry = {"id": "0x%08x" % rid, "category": "buildings", "name": name,
             "switch": list(switch), "pc": list(pc), "target": list(target),
             "switch_payload": switch_bytes, "switch_stored": switch_bytes,
             "pc_payload": 0, "estimated": estimated,
             "delta": estimated - switch_bytes}
    assert set(entry) == set(fa.PLAN_ENTRY_KEYS)
    return entry


def write_plan_file(fa, path, switch_dat, pc_dat, entries):
    """Write a plan file the way the `plan` command does."""
    header = {"plan": fa.PLAN_TAG, "version": fa.PLAN_VERSION,
              "switch": switch_dat, "pc": pc_dat,
              "switch_records": 0, "pc_records": 0,
              "switch_dat_bytes": os.path.getsize(switch_dat),
              "switch_gap_bytes": 0, "target_size": "2x", "max_size": 2048,
              "categories": list(fa.CATEGORY_NAMES), "include": [],
              "exclude": [], "budget": None}
    fa.write_plan(path, header, entries)


def run(fa, argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = fa.main(argv)
    return code, out.getvalue(), err.getvalue()


class Fixture:
    """One Switch set, one PC set and a plan file, in a temp directory."""

    def __init__(self, fa, directory):
        self.fa = fa
        self.directory = directory
        self.switch = os.path.join(directory, "switch-s3.dat")
        self.pc = os.path.join(directory, "pc-s3.dat")
        self.plan = os.path.join(directory, "plan.jsonl")
        self.out = os.path.join(directory, "out")

    def write(self, switch_blocks, pc_blocks, entries):
        dat = build_set(self.directory, switch_blocks, basename="switch-s3")
        pc_dat = build_set(self.directory, pc_blocks, basename="pc-s3")
        assert dat == self.switch
        assert pc_dat == self.pc
        write_plan_file(self.fa, self.plan, self.switch, self.pc, entries)

    def apply(self, *extra):
        argv = ["apply-bc3", "--plan", self.plan, "--out", self.out]
        argv.extend(extra)
        return run(self.fa, argv)

    def payload(self, rid, name="switch-s3"):
        """The payload a written block of the new set holds."""
        dat = os.path.join(self.out, name + ".dat")
        ds = self.fa.DatSet(dat)
        return ds.read_payload(ds.by_id(rid))


class LevelTableTest(unittest.TestCase):
    """The byte count of every level of a BC3 chain, against the measured one."""

    @classmethod
    def setUpClass(cls):
        cls.fa = load_module()

    def test_levels_of_a_1024_texture(self):
        sizes = [self.fa.bc3_level_bytes(1024, 1024, level) for level in range(11)]
        self.assertEqual(sizes, [1048576, 262144, 65536, 16384, 4096, 1024,
                                 512, 512, 512, 512, 512])
        self.assertEqual(sum(sizes), 1400320)
        self.assertEqual(self.fa.bc3_chain_bytes(1024, 1024), 1400320)
        self.assertEqual(self.fa.bc3_levels(1024, 1024), 11)

    def test_block_height_shrinks_for_small_levels(self):
        heights = [self.fa.bc3_level_bh(1024, level) for level in range(11)]
        self.assertEqual(heights, [4, 4, 3, 2, 1, 0, 0, 0, 0, 0, 0])

    def test_chain_stops_at_one_texel(self):
        for width, height in ((1, 1), (8, 8), (8, 4), (10, 6), (2048, 2048)):
            levels = self.fa.bc3_levels(width, height)
            last = self.fa.bc3_level_dims(width, height, levels - 1)
            self.assertEqual(last, (1, 1),
                             "%dx%d ends at %r" % (width, height, last))
        self.assertEqual(self.fa.bc3_levels(8, 4), 4)
        self.assertEqual([self.fa.bc3_level_dims(8, 4, level) for level in range(4)],
                         [(8, 4), (4, 2), (2, 1), (1, 1)])

    def test_every_level_is_whole_gobs(self):
        for width, height in ((8, 8), (10, 6), (64, 64), (128, 64), (1024, 1024)):
            for level in range(self.fa.bc3_levels(width, height)):
                size = self.fa.bc3_level_bytes(width, height, level)
                self.assertEqual(size % 512, 0,
                                 "%dx%d level %d is %d bytes"
                                 % (width, height, level, size))


class LayoutTest(unittest.TestCase):
    """Writing blocks into the layout and reading them back."""

    @classmethod
    def setUpClass(cls):
        cls.fa = load_module()
        cls.layout = cls.fa.bc3_layout()

    @staticmethod
    def blocks(width, height):
        """One 16-byte block per 4 by 4 texel tile, each filled with its index."""
        count = ((width + 3) // 4) * ((height + 3) // 4)
        return b"".join(bytes([index % 256]) * 16 for index in range(count))

    def test_swizzle_round_trip(self):
        for width, height in ((8, 8), (10, 6), (64, 64), (128, 64), (1024, 1024)):
            blocks = self.blocks(width, height)
            block_height = self.fa.bc3_level_bh(height, 0)
            written = self.layout.swizzle(blocks, width, height, block_height)
            self.assertEqual(len(written),
                             self.fa.bc3_level_bytes(width, height, 0))
            back = self.layout.unswizzle(written, width, height, block_height)
            self.assertEqual(back, blocks, "%dx%d does not round trip"
                             % (width, height))

    def test_swizzle_permutes_the_blocks(self):
        # 64 by 64 is 4,096 bytes of blocks and 4,096 bytes of level, so a
        # difference here is the order the blocks are stored in and nothing else.
        blocks = self.blocks(64, 64)
        written = self.layout.swizzle(blocks, 64, 64, self.fa.bc3_level_bh(64, 0))
        self.assertEqual(len(written), len(blocks))
        self.assertNotEqual(written, blocks)

    def test_swizzle_pads_to_whole_gobs(self):
        # 8 by 8 is four blocks, 64 bytes, and a level is one whole 512-byte GOB.
        blocks = self.blocks(8, 8)
        written = self.layout.swizzle(blocks, 8, 8, self.fa.bc3_level_bh(8, 0))
        self.assertEqual(len(written), 512)
        self.assertEqual(self.layout.unswizzle(written, 8, 8, 0), blocks)


class PayloadTest(unittest.TestCase):
    """A payload built from a known image, read back through the layout."""

    @classmethod
    def setUpClass(cls):
        cls.fa = load_module()
        cls.layout = cls.fa.bc3_layout()

    def decode_level0(self, payload):
        """The first level of a payload, decoded: (width, height, pixels)."""
        header = self.fa.switch_texture(payload)
        self.assertIsNotNone(header)
        level_w, level_h = header.width, header.height
        block_height = self.fa.bc3_level_bh(level_h, 0)
        size = self.fa.bc3_level_bytes(level_w, level_h, 0)
        blocks = self.layout.unswizzle(payload[self.fa.TEXTURE_HEADER_SIZE:]
                                       [:size], level_w, level_h, block_height)
        return level_w, level_h, self.layout.dxt5_decode(blocks, level_w, level_h)

    def test_eight_by_eight_round_trip(self):
        original = bc3_switch_block(self.fa, 8, 8)
        payload = self.fa.build_bc3_payload(original, 8, 8, quad_image())
        width, height, pixels = self.decode_level0(payload)
        self.assertEqual((width, height), (8, 8))
        for index, expected in enumerate(quad_image()):
            self.assertLessEqual(
                abs(pixels[index] - expected), 4,
                "byte %d decoded to %d, the image has %d"
                % (index, pixels[index], expected))

    def test_each_block_keeps_its_colour(self):
        original = bc3_switch_block(self.fa, 8, 8)
        payload = self.fa.build_bc3_payload(original, 8, 8, quad_image())
        _, _, pixels = self.decode_level0(payload)
        for row, column, expected in ((1, 1, RED), (1, 5, GREEN),
                                      (5, 1, BLUE), (5, 5, WHITE)):
            offset = (row * 8 + column) * 4
            for channel in range(3):
                self.assertLessEqual(abs(pixels[offset + channel] - expected[channel]),
                                     4, "pixel %d,%d channel %d"
                                     % (row, column, channel))
            self.assertEqual(pixels[offset + 3], 255)

    def test_header_fields_are_kept(self):
        original = switch_block(8, 8, fmt=72, mips=4,
                                descriptor=b"\x05\x04\x03\x02",
                                pixel_bytes=self.fa.bc3_chain_bytes(8, 8),
                                body=b"\x5c" * self.fa.bc3_chain_bytes(8, 8))
        payload = self.fa.build_bc3_payload(original, 8, 8, quad_image())
        self.assertEqual(payload[:4], original[:4])
        self.assertEqual(struct.unpack_from("<I", payload, 4)[0], 72)
        self.assertEqual(payload[8:12], original[8:12])
        self.assertEqual(payload[16:20], original[16:20])
        self.assertEqual(payload[20:24], b"\x05\x04\x03\x02")
        self.assertEqual(struct.unpack_from("<HH", payload, 12), (8, 8))
        self.assertEqual(payload[19], 4)
        self.assertEqual(struct.unpack_from("<I", payload, 24)[0],
                         self.fa.bc3_chain_bytes(8, 8))
        self.assertEqual(len(payload) - self.fa.TEXTURE_HEADER_SIZE,
                         self.fa.bc3_chain_bytes(8, 8))

    def test_a_grown_payload_is_a_longer_chain(self):
        original = bc3_switch_block(self.fa, 8, 8)
        payload = self.fa.build_bc3_payload(original, 16, 16,
                                            flat_image(16, 16, GREY))
        self.assertEqual(struct.unpack_from("<HH", payload, 12), (16, 16))
        self.assertEqual(payload[19], 5)
        self.assertEqual(struct.unpack_from("<I", payload, 24)[0],
                         self.fa.bc3_chain_bytes(16, 16))
        width, height, pixels = self.decode_level0(payload)
        self.assertEqual((width, height), (16, 16))
        for channel in range(3):
            self.assertLessEqual(abs(pixels[channel] - GREY[channel]), 4)

    def test_an_odd_size_chain_is_built(self):
        # 10 by 6 halves to 5 by 3, 2 by 1 and 1 by 1, none of them square and
        # none of them a multiple of the block size.
        original = bc3_switch_block(self.fa, 10, 6)
        payload = self.fa.build_bc3_payload(original, 10, 6,
                                            flat_image(10, 6, BLUE))
        self.assertEqual(payload[19], 4)
        self.assertEqual(len(payload) - self.fa.TEXTURE_HEADER_SIZE,
                         self.fa.bc3_chain_bytes(10, 6))
        width, height, _ = self.decode_level0(payload)
        self.assertEqual((width, height), (10, 6))

    def test_target_dims_caps_the_pc_size(self):
        self.assertEqual(self.fa.bc3_target_dims(2048, (1024, 1024), (4096, 4096)),
                         ((2048, 2048), None))
        self.assertEqual(self.fa.bc3_target_dims(2048, (64, 64), (256, 128)),
                         ((256, 128), None))
        self.assertEqual(self.fa.bc3_target_dims(512, (64, 64), (4096, 2048)),
                         ((512, 256), None))
        self.assertEqual(self.fa.bc3_target_dims(64, (64, 64), (4096, 4096)),
                         ((64, 64), None))

    def test_target_dims_refuses_to_shrink(self):
        target, why = self.fa.bc3_target_dims(512, (1024, 1024), (4096, 4096))
        self.assertIsNone(target)
        self.assertIn("smaller than the Switch texture 1024x1024", why)
        self.assertIn("--max-size", why)


class ApplyTest(unittest.TestCase):
    """What `apply-bc3` writes, and what it refuses to write."""

    @classmethod
    def setUpClass(cls):
        cls.fa = load_module()

    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="frostpunk-bc3-")
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.fixture = Fixture(self.fa, self.directory)

    def test_replaces_a_format_72_texture(self):
        switch = [bc3_switch_block(self.fa, 8, 8)]
        pc = [(TEST_ID, pc_dxt5(8, 8, [RED, GREEN, BLUE, WHITE]),
               "of/albedo.texture")]
        self.fixture.write([(TEST_ID, switch[0], "of/albedo.texture")], pc,
                           [plan_entry(self.fa, TEST_ID, "of/albedo.texture",
                                       (8, 8), (8, 8), (8, 8))])
        code, out, err = self.fixture.apply()
        self.assertEqual(code, 0, err)
        self.assertEqual(err, "")
        self.assertIn("replaced  1 of 1", out)
        self.assertIn("0x%08x" % TEST_ID, out)
        payload = self.fixture.payload(TEST_ID)
        header = self.fa.switch_texture(payload)
        self.assertEqual(header.format, self.fa.SWITCH_FORMAT_ASTC_4X4)
        self.assertEqual((header.width, header.height), (8, 8))
        self.assertEqual(len(payload) - self.fa.TEXTURE_HEADER_SIZE,
                         self.fa.bc3_chain_bytes(8, 8))

    def test_report_names_the_sizes(self):
        self.fixture.write(
            [(TEST_ID, bc3_switch_block(self.fa, 8, 8), "of/albedo.texture")],
            [(TEST_ID, pc_dxt5(8, 8), "of/albedo.texture")],
            [plan_entry(self.fa, TEST_ID, "of/albedo.texture", (8, 8), (8, 8),
                        (8, 8), switch_bytes=2048)])
        code, out, _ = self.fixture.apply()
        self.assertEqual(code, 0)
        self.assertIn("8x8", out)
        self.assertIn("4 lv", out)
        self.assertIn("old payload", out)
        self.assertIn("on disk", out)
        self.assertIn("time", out)

    def test_json_report(self):
        self.fixture.write(
            [(TEST_ID, bc3_switch_block(self.fa, 8, 8), "of/albedo.texture")],
            [(TEST_ID, pc_dxt5(8, 8), "of/albedo.texture")],
            [plan_entry(self.fa, TEST_ID, "of/albedo.texture", (8, 8), (8, 8),
                        (8, 8))])
        code, out, _ = self.fixture.apply("--format", "json")
        self.assertEqual(code, 0)
        report = json.loads(out)
        for key in ("switch", "pc", "plan", "count", "attempted", "old_bytes",
                    "new_bytes", "on_disk", "padding", "replaced_size", "beyond",
                    "beyond_bytes", "encoded", "write", "elapsed", "entries",
                    "refused", "dat", "idx"):
            self.assertIn(key, report)
        self.assertEqual(report["count"], 1)
        self.assertEqual(report["entries"][0]["levels"], 4)
        self.assertEqual(report["entries"][0]["pc_format"], "dxt5")

    def test_accepts_a_block_that_holds_one_level(self):
        # 2629 of the shipped format-72 blocks hold level 0 alone, and the
        # replacement still gets the whole chain.
        one_level = bc3_switch_block(self.fa, 8, 8)[:28 + 512]
        one_level = bytearray(one_level)
        one_level[19] = 1
        struct.pack_into("<I", one_level, 24, 512)
        self.fixture.write(
            [(TEST_ID, bytes(one_level), "of/albedo.texture")],
            [(TEST_ID, pc_dxt5(8, 8), "of/albedo.texture")],
            [plan_entry(self.fa, TEST_ID, "of/albedo.texture", (8, 8), (8, 8),
                        (8, 8))])
        code, out, err = self.fixture.apply()
        self.assertEqual(code, 0, err)
        self.assertIn("replaced  1 of 1", out)
        payload = self.fixture.payload(TEST_ID)
        self.assertEqual(payload[19], 4)
        self.assertEqual(len(payload) - self.fa.TEXTURE_HEADER_SIZE,
                         self.fa.bc3_chain_bytes(8, 8))

    def test_refuses_a_cube_map(self):
        # Six faces, each the chain of an 8 by 8 image.
        face = self.fa.bc3_chain_bytes(8, 8)
        body = bytes(face) * 6
        self.fixture.write(
            [(TEST_ID, switch_block(8, 8, fmt=72, mips=4, pixel_bytes=len(body),
                                    body=body), "cubemaps/stone.texture")],
            [(TEST_ID, pc_dxt5(8, 8), "cubemaps/stone.texture")],
            [plan_entry(self.fa, TEST_ID, "cubemaps/stone.texture", (8, 8),
                        (8, 8), (8, 8))])
        code, _, err = self.fixture.apply()
        self.assertEqual(code, 2)
        self.assertIn("cube map", err)
        self.assertIn("6 copies", err)

    def test_refuses_another_format(self):
        self.fixture.write(
            [(TEST_ID, switch_block(8, 8, fmt=37, mips=1,
                                    pixel_bytes=8 * 8 * 4), "of/albedo.texture")],
            [(TEST_ID, pc_dxt5(8, 8), "of/albedo.texture")],
            [plan_entry(self.fa, TEST_ID, "of/albedo.texture", (8, 8), (8, 8),
                        (8, 8))])
        code, _, err = self.fixture.apply()
        self.assertEqual(code, 2)
        self.assertIn("not format 72", err)
        self.assertIn("0x%08x" % TEST_ID, err)

    def test_refuses_pixels_that_are_not_the_layout(self):
        # The header says 8 by 8 with four levels but the block holds 64 bytes,
        # which is one row-major level and not a block-linear chain.
        self.fixture.write(
            [(TEST_ID, switch_block(8, 8, fmt=72, mips=1, pixel_bytes=64),
              "of/albedo.texture")],
            [(TEST_ID, pc_dxt5(8, 8), "of/albedo.texture")],
            [plan_entry(self.fa, TEST_ID, "of/albedo.texture", (8, 8), (8, 8),
                        (8, 8))])
        code, _, err = self.fixture.apply()
        self.assertEqual(code, 2)
        self.assertIn("not that layout", err)

    def test_refuses_a_missing_pc_entry(self):
        self.fixture.write(
            [(TEST_ID, bc3_switch_block(self.fa, 8, 8), "of/albedo.texture")],
            [(SECOND_ID, pc_dxt5(8, 8), "of/other.texture")],
            [plan_entry(self.fa, TEST_ID, "of/albedo.texture", (8, 8), (8, 8),
                        (8, 8))])
        code, _, err = self.fixture.apply()
        self.assertEqual(code, 2)
        self.assertIn("has no record in the PC package", err)

    def test_refuses_a_pc_payload_that_is_not_an_image(self):
        self.fixture.write(
            [(TEST_ID, bc3_switch_block(self.fa, 8, 8), "of/albedo.texture")],
            [(TEST_ID, b"<TextureArrayDescriptor/>\n", "of/albedo.texture")],
            [plan_entry(self.fa, TEST_ID, "of/albedo.texture", (8, 8), (8, 8),
                        (8, 8))])
        code, _, err = self.fixture.apply()
        self.assertEqual(code, 2)
        self.assertIn("not an image payload on the PC side", err)

    def test_refuses_raw_pc_pixels(self):
        self.fixture.write(
            [(TEST_ID, bc3_switch_block(self.fa, 8, 8), "of/albedo.texture")],
            [(TEST_ID, pc_image([(8, 8, flat_image(8, 8, WHITE))], tag=None),
              "of/albedo.texture")],
            [plan_entry(self.fa, TEST_ID, "of/albedo.texture", (8, 8), (8, 8),
                        (8, 8))])
        code, _, err = self.fixture.apply()
        self.assertEqual(code, 2)
        self.assertIn("not BC1, BC2 or BC3", err)

    def test_refuses_a_pc_image_smaller_than_the_switch_texture(self):
        self.fixture.write(
            [(TEST_ID, bc3_switch_block(self.fa, 64, 64), "of/albedo.texture")],
            [(TEST_ID, pc_dxt5(32, 32), "of/albedo.texture")],
            [plan_entry(self.fa, TEST_ID, "of/albedo.texture", (64, 64),
                        (32, 32), (32, 32))])
        code, _, err = self.fixture.apply()
        self.assertEqual(code, 2)
        self.assertIn("smaller than the Switch texture", err)

    def test_refuses_a_cap_below_the_switch_texture(self):
        self.fixture.write(
            [(TEST_ID, bc3_switch_block(self.fa, 64, 64), "of/albedo.texture")],
            [(TEST_ID, pc_dxt5(128, 128), "of/albedo.texture")],
            [plan_entry(self.fa, TEST_ID, "of/albedo.texture", (64, 64),
                        (128, 128), (128, 128))])
        code, _, err = self.fixture.apply("--max-size", "32")
        self.assertEqual(code, 2)
        self.assertIn("smaller than the Switch texture", err)
        self.assertIn("--max-size (it is 32)", err)

    def test_one_refusal_does_not_stop_the_others(self):
        self.fixture.write(
            [(TEST_ID, bc3_switch_block(self.fa, 8, 8), "of/albedo.texture"),
             (SECOND_ID, switch_block(8, 8, fmt=37, mips=1,
                                      pixel_bytes=8 * 8 * 4), "of/mask.texture")],
            [(TEST_ID, pc_dxt5(8, 8), "of/albedo.texture"),
             (SECOND_ID, pc_dxt5(8, 8), "of/mask.texture")],
            [plan_entry(self.fa, TEST_ID, "of/albedo.texture", (8, 8), (8, 8),
                        (8, 8)),
             plan_entry(self.fa, SECOND_ID, "of/mask.texture", (8, 8), (8, 8),
                        (8, 8))])
        code, out, _ = self.fixture.apply()
        self.assertEqual(code, 0)
        self.assertIn("replaced  1 of 2", out)
        self.assertIn("refused   1", out)
        self.assertIn("not format 72", out)

    def test_fail_fast_stops_at_the_first_refusal(self):
        self.fixture.write(
            [(TEST_ID, switch_block(8, 8, fmt=37, mips=1,
                                    pixel_bytes=8 * 8 * 4), "of/mask.texture")],
            [(TEST_ID, pc_dxt5(8, 8), "of/mask.texture")],
            [plan_entry(self.fa, TEST_ID, "of/mask.texture", (8, 8), (8, 8),
                        (8, 8))])
        code, _, err = self.fixture.apply("--fail-fast")
        self.assertEqual(code, 2)
        self.assertIn("not format 72", err)
        self.assertFalse(os.path.exists(os.path.join(self.fixture.out, "switch-s3.dat")))

    def test_limit_takes_the_first_entries(self):
        self.fixture.write(
            [(TEST_ID, bc3_switch_block(self.fa, 8, 8), "of/albedo.texture"),
             (SECOND_ID, bc3_switch_block(self.fa, 8, 8), "of/normal.texture")],
            [(TEST_ID, pc_dxt5(8, 8), "of/albedo.texture"),
             (SECOND_ID, pc_dxt5(8, 8), "of/normal.texture")],
            [plan_entry(self.fa, TEST_ID, "of/albedo.texture", (8, 8), (8, 8),
                        (8, 8)),
             plan_entry(self.fa, SECOND_ID, "of/normal.texture", (8, 8), (8, 8),
                        (8, 8))])
        code, out, _ = self.fixture.apply("--limit", "1")
        self.assertEqual(code, 0)
        self.assertIn("replaced  1 of 1", out)
        self.assertIn("2 entries", out)


class PaddingTest(unittest.TestCase):
    """The file length the loader insists on."""

    @classmethod
    def setUpClass(cls):
        cls.fa = load_module()

    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="frostpunk-bc3-pad-")
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.fixture = Fixture(self.fa, self.directory)
        self.fixture.write(
            [(TEST_ID, bc3_switch_block(self.fa, 8, 8), "of/albedo.texture")],
            [(TEST_ID, pc_dxt5(8, 8), "of/albedo.texture")],
            [plan_entry(self.fa, TEST_ID, "of/albedo.texture", (8, 8), (8, 8),
                        (8, 8))])

    def written(self):
        return os.path.getsize(os.path.join(self.fixture.out, "switch-s3.dat"))

    def test_pads_to_the_replaced_file_by_default(self):
        before = os.path.getsize(self.fixture.switch)
        code, out, _ = self.fixture.apply()
        self.assertEqual(code, 0)
        self.assertEqual(self.written(), before)
        self.assertIn("of padding", out)
        self.assertIn("0x177a02", out)

    def test_padding_is_zeros_after_the_last_block(self):
        self.fixture.apply()
        ds = self.fa.DatSet(os.path.join(self.fixture.out, "switch-s3.dat"))
        accounted = sum(record.stored for record in ds.records)
        size = self.written()
        self.assertLess(accounted, size)
        with open(ds.dat_path, "rb") as fh:
            fh.seek(accounted)
            self.assertEqual(fh.read(), bytes(size - accounted))

    def test_no_pad_leaves_a_shorter_file(self):
        before = os.path.getsize(self.fixture.switch)
        code, out, _ = self.fixture.apply("--no-pad")
        self.assertEqual(code, 0)
        self.assertLess(self.written(), before)
        self.assertNotIn("of padding", out)

    def test_pad_to_a_named_size(self):
        before = os.path.getsize(self.fixture.switch)
        code, _, _ = self.fixture.apply("--pad-to", str(before + 4096))
        self.assertEqual(code, 0)
        self.assertEqual(self.written(), before + 4096)

    def report(self, beyond, beyond_bytes):
        return {
            "switch": "switch-s3.dat", "pc": "pc-s3.dat", "plan": "plan.jsonl",
            "planned": 1, "attempted": 1, "count": 1, "max_size": 2048,
            "levels": 4, "old_bytes": 2076, "new_bytes": 2076,
            "on_disk": 4096, "pad_to": 4096, "padding": 2048,
            "replaced_size": 1024, "beyond": beyond,
            "beyond_bytes": beyond_bytes, "encoded": 0.1, "write": 0.1,
            "elapsed": 0.2, "entries": [], "refused": [],
            "dat": "out/switch-s3.dat", "idx": "out/switch-s3.idx",
        }

    def test_notes_a_tail_past_the_replaced_file(self):
        # Padding fixes a file that is too short; a replacement that grows the
        # blocks moves them past the length of the file they replace, which the
        # report names instead of leaving the reader to compare two numbers.
        out = self.fa.render_apply_bc3(self.report(2, 4096), "table")
        self.assertIn("end past the", out)
        self.assertIn("2 record(s)", out)
        self.assertIn("4,096 bytes in all", out)
        clean = self.fa.render_apply_bc3(self.report(0, 0), "table")
        self.assertNotIn("end past the", clean)

    def test_the_index_still_describes_the_blocks(self):
        self.fixture.apply()
        dat = os.path.join(self.fixture.out, "switch-s3.dat")
        ds = self.fa.DatSet(dat)
        accounted = sum(record.stored for record in ds.records)
        self.assertLessEqual(accounted, os.path.getsize(dat))
        payload = ds.read_payload(ds.by_id(TEST_ID))
        self.assertEqual(len(payload) - self.fa.TEXTURE_HEADER_SIZE,
                         self.fa.bc3_chain_bytes(8, 8))


if __name__ == "__main__":
    unittest.main()
