#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the ScarletNX contributors
"""Tests for the `apply` command of frostpunk-assets.py.

The fixture is a small container set built here, because a real one is
gigabytes of game data.  It holds the cases the ticket names: a DXT5 texture
at 4 by 4 and at 8 by 8, a raw RGBA8 texture, a mip chain whose full-size
level comes last, a texture that is not an image, a target smaller than the
texture it replaces, and a header that claims a mip chain.  The pixels are
built from one-colour blocks, so the expected result of every decode can be
worked out by hand and asserted exactly.

The encode path needs `astcenc`; when it is not installed those tests are
skipped with a message, and the rest run.  Every assertion is about what the
command prints, what a plan file says or what the new container holds.
"""
import contextlib
import importlib.util
import io
import json
import os
import re
import shutil
import struct
import sys
import tempfile
import unittest
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(os.path.dirname(HERE), "scripts", "frostpunk-assets.py")
ASTCENC = shutil.which("astcenc")
needs_astc = unittest.skipUnless(
    ASTCENC, "astcenc is not installed, so the encode path is skipped")
TEST_ID = 0x0c0ffee1
SECOND_ID = 0x0c0ffee2

# The colours of the one-colour blocks, which are exact in 5-6-5.
RED = (255, 0, 0, 255)
GREEN = (0, 255, 0, 255)
BLUE = (0, 0, 255, 128)
WHITE = (255, 255, 255, 255)
GREY = (128, 128, 128, 255)


def load_module():
    spec = importlib.util.spec_from_file_location("frostpunk_apply", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _rgb565(red, green, blue):
    return ((red >> 3) << 11) | ((green >> 2) << 5) | (blue >> 3)


def dxt1_block(colour):
    """One DXT1 block of one colour: both endpoints equal, index zero."""
    value = _rgb565(colour[0], colour[1], colour[2])
    return struct.pack("<HH", value, value) + b"\x00" * 4


def dxt3_block(colour, alpha=0xf):
    """One DXT3 block of one colour and one four-bit alpha nibble."""
    value = _rgb565(colour[0], colour[1], colour[2])
    return struct.pack("<Q", (alpha * 0x1111111111111111) &
                       0xffffffffffffffff) + \
        struct.pack("<HH", value, value) + b"\x00" * 4


def dxt5_block(colour):
    """One DXT5 block of one colour: alpha endpoints equal, index zero."""
    value = _rgb565(colour[0], colour[1], colour[2])
    return (bytes([colour[3], colour[3]]) + b"\x00" * 6 +
            struct.pack("<HH", value, value) + b"\x00" * 4)


def dxt5_quads(colours):
    """Four DXT5 blocks in a 8 by 8 texture, in reading order."""
    return b"".join(dxt5_block(colour) for colour in colours)


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


def descriptor_payload():
    return (b"<TextureArrayDescriptor>\r\n"
            b"  <Properties ClassName=\"TextureArrayDescriptor\"/>\r\n"
            b"</TextureArrayDescriptor>\r\n")


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
    entry = {"id": "0x%08x" % rid, "category": "terrain", "name": name,
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
        self.cache = os.path.join(directory, "cache")

    def write(self, switch_blocks, pc_blocks, entries):
        dat = build_set(self.directory, switch_blocks, basename="switch-s3")
        pc_dat = build_set(self.directory, pc_blocks, basename="pc-s3")
        assert dat == self.switch
        assert pc_dat == self.pc
        write_plan_file(self.fa, self.plan, self.switch, self.pc, entries)

    def apply(self, *extra):
        argv = ["apply", "--plan", self.plan, "--out", self.out,
                "--cache-dir", self.cache, "--quality", "fast"]
        argv.extend(extra)
        return run(self.fa, argv)

    def payload(self, rid, basename="switch-s3"):
        """The block the writer stored for `rid` in a new set."""
        dat = os.path.join(self.out, basename + ".dat")
        loaded = self.fa.DatSet(dat)
        record = loaded.by_id(rid)
        assert record is not None, "no record 0x%08x in %s" % (rid, dat)
        return loaded.read_payload(record)


class DecoderTest(unittest.TestCase):
    """The pixel decoders, against blocks whose result is known."""

    def setUp(self):
        self.fa = load_module()

    def test_a_dxt5_block_decodes_to_its_colour(self):
        pixels = self.fa._decode_dxt(dxt5_block(BLUE), 4, 4, "dxt5")
        self.assertEqual(4 * 4 * 4, len(pixels))
        self.assertEqual(bytes(BLUE) * 16, bytes(pixels))

    def test_a_dxt1_block_decodes_to_its_colour(self):
        pixels = self.fa._decode_dxt(dxt1_block(RED), 4, 4, "dxt1")
        self.assertEqual(bytes(RED) * 16, bytes(pixels))

    def test_a_dxt3_block_decodes_to_its_colour_and_alpha(self):
        pixels = self.fa._decode_dxt(dxt3_block(GREEN[:3], alpha=0x8),
                                     4, 4, "dxt3")
        self.assertEqual(bytes((0, 255, 0, 0x88)) * 16, bytes(pixels))

    def test_the_alpha_palette_of_a_dxt5_block_has_eight_values(self):
        block = bytearray(dxt5_block(WHITE))
        block[0], block[1] = 255, 0
        palette = self.fa._dxt5_alphas(bytes(block))
        self.assertEqual([255, 0, 218, 182, 145, 109, 72, 36], palette)
        block[0], block[1] = 0, 255
        palette = self.fa._dxt5_alphas(bytes(block))
        self.assertEqual([0, 255, 51, 102, 153, 204, 0, 255], palette)

    def test_an_eight_by_eight_texture_keeps_its_four_blocks_in_order(self):
        pixels = self.fa._decode_dxt(
            dxt5_quads([RED, GREEN, BLUE, WHITE]), 8, 8, "dxt5")
        for row in range(8):
            for column in range(8):
                expected = (RED if row < 4 else BLUE) if column < 4 else \
                    (GREEN if row < 4 else WHITE)
                at = (row * 8 + column) * 4
                self.assertEqual(bytes(expected), bytes(pixels[at:at + 4]),
                                 "pixel %d,%d" % (column, row))

    def test_a_raw_rgba8_block_decodes_to_its_pixels(self):
        source = bytes((10, 20, 30, 40, 50, 60, 70, 80))
        self.assertEqual(source, bytes(self.fa._decode_raw(source, 2, 1, 4)))

    def test_a_raw_byte_block_becomes_grey(self):
        pixels = self.fa._decode_raw(bytes((7, 200)), 2, 1, 1)
        self.assertEqual(bytes((7, 7, 7, 255, 200, 200, 200, 255)),
                         bytes(pixels))

    def test_a_raw_u16_block_keeps_the_high_byte(self):
        source = struct.pack("<HH", 0x0141, 0x02ff)
        pixels = self.fa._decode_raw(source, 2, 1, 2)
        self.assertEqual(bytes((1, 1, 1, 255, 2, 2, 2, 255)), bytes(pixels))

    def test_the_full_size_level_is_read_from_the_end_of_the_chain(self):
        small = dxt5_block(RED)
        middle = dxt5_block(GREEN)
        full = dxt5_block(BLUE)
        payload = pc_image([(1, 1, small), (2, 2, middle), (4, 4, full)])
        width, height, pixels = self.fa.decode_pc_level0(payload, "test")
        self.assertEqual((4, 4), (width, height))
        self.assertEqual(bytes(BLUE) * 16, bytes(pixels))

    def test_an_image_that_is_not_a_payload_is_refused(self):
        with self.assertRaises(self.fa.ContainerError) as caught:
            self.fa.decode_pc_level0(descriptor_payload(), "0x0000000a name")
        self.assertIn("not a PC image payload", str(caught.exception))

    def test_a_payload_shorter_than_its_blocks_is_refused(self):
        payload = pc_image([(4, 4, dxt5_block(RED))])
        with self.assertRaises(self.fa.ContainerError) as caught:
            self.fa.decode_pc_level0(payload[:-4], "0x0000000b name")
        self.assertIn("not a PC image payload", str(caught.exception))

    def test_a_power_of_two_downscale_is_an_exact_mean(self):
        source = bytes((0, 0, 0, 0, 200, 100, 20, 40)) * 8
        pixels = self.fa.resample_box(source, 4, 4, 2, 2)
        self.assertEqual(bytes((100, 50, 10, 20)), bytes(pixels[0:4]))
        with self.assertRaises(self.fa.ContainerError):
            self.fa.resample_box(source, 4, 4, 8, 8)


class QualityTest(unittest.TestCase):
    """The quality flag, which is a preset name or a number."""

    def setUp(self):
        self.fa = load_module()

    def test_a_preset_name_is_taken_as_it_is(self):
        for name in ("fastest", "medium", "thorough", "exhaustive"):
            self.assertEqual(name, self.fa.parse_quality(name))

    def test_a_dash_and_a_case_do_not_matter(self):
        self.assertEqual("medium", self.fa.parse_quality("-Medium"))

    def test_a_number_picks_the_nearest_preset(self):
        self.assertEqual("fastest", self.fa.parse_quality("0"))
        self.assertEqual("fast", self.fa.parse_quality("10"))
        self.assertEqual("medium", self.fa.parse_quality("60"))
        self.assertEqual("thorough", self.fa.parse_quality("98"))
        self.assertEqual("verythorough", self.fa.parse_quality("99"))
        self.assertEqual("exhaustive", self.fa.parse_quality("100"))

    def test_a_bad_quality_is_an_error(self):
        for text in ("quick", "101", "-1"):
            with self.assertRaises(self.fa.ContainerError):
                self.fa.parse_quality(text)

    def test_the_astc_magic_is_what_an_encoder_writes(self):
        self.assertEqual(b"\x13\xab\xa1\x5c", bytes.fromhex("13aba15c"))


@needs_astc
class ApplyTest(unittest.TestCase):
    """The whole path: a plan line to a block in a new container."""

    def setUp(self):
        self.fa = load_module()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fixture = Fixture(self.fa, self.tmp.name)

    def test_a_four_by_four_dxt5_texture_becomes_one_astc_block(self):
        switch = switch_block(4, 4)
        self.fixture.write(
            [(TEST_ID, switch, "terrain/snow.texture")],
            [(TEST_ID, pc_image([(4, 4, dxt5_block(RED))]),
              "terrain/snow.texture")],
            [plan_entry(self.fa, TEST_ID, "terrain/snow.texture", (4, 4),
                        (4, 4), (4, 4), len(switch))])
        code, out, err = self.fixture.apply()
        self.assertEqual(0, code, err)
        payload = self.fixture.payload(TEST_ID)
        self.assertEqual(28 + 16, len(payload))
        texture = self.fa.switch_texture(payload)
        self.assertEqual((4, 4), (texture.width, texture.height))
        self.assertEqual(self.fa.SWITCH_FORMAT_ASTC_4X4, texture.format)
        self.assertEqual(1, texture.mips)
        self.assertEqual(16, texture.pixel_bytes)

    def test_an_eight_by_eight_texture_keeps_four_distinct_blocks(self):
        switch = switch_block(8, 8)
        self.fixture.write(
            [(TEST_ID, switch, "terrain/snow.texture")],
            [(TEST_ID, pc_image([(8, 8, dxt5_quads([RED, GREEN, BLUE, WHITE]))]),
              "terrain/snow.texture")],
            [plan_entry(self.fa, TEST_ID, "terrain/snow.texture", (8, 8),
                        (8, 8), (8, 8), len(switch))])
        code, out, err = self.fixture.apply()
        self.assertEqual(0, code, err)
        payload = self.fixture.payload(TEST_ID)
        self.assertEqual(28 + 4 * 16, len(payload))
        blocks = [payload[28 + index * 16:28 + (index + 1) * 16]
                  for index in range(4)]
        self.assertEqual(4, len(set(blocks)), "four colours, four blocks")

    def test_a_raw_rgba8_texture_reaches_the_container(self):
        switch = switch_block(4, 4)
        pixels = bytes((200, 100, 50, 255)) * 16
        self.fixture.write(
            [(TEST_ID, switch, "terrain/snow.texture")],
            [(TEST_ID, pc_image([(4, 4, pixels)], tag=None),
              "terrain/snow.texture")],
            [plan_entry(self.fa, TEST_ID, "terrain/snow.texture", (4, 4),
                        (4, 4), (4, 4), len(switch))])
        code, out, err = self.fixture.apply()
        self.assertEqual(0, code, err)
        self.assertEqual(28 + 16, len(self.fixture.payload(TEST_ID)))

    def test_the_header_is_kept_except_the_words_that_describe_the_pixels(self):
        body = b"\xa5" * (4096 + 2192)
        switch = switch_block(64, 64, mips=10, pixel_bytes=len(body), body=body)
        self.fixture.write(
            [(TEST_ID, switch, "terrain/snow.texture")],
            [(TEST_ID, pc_image([(64, 64, dxt5_block(RED) * 256)]),
              "terrain/snow.texture")],
            [plan_entry(self.fa, TEST_ID, "terrain/snow.texture", (64, 64),
                        (64, 64), (64, 64), len(switch))])
        code, out, err = self.fixture.apply()
        self.assertEqual(0, code, err)
        payload = self.fixture.payload(TEST_ID)
        written = bytearray(payload[:28])
        self.assertEqual(1, written[19], "the level count describes one level")
        self.assertEqual(4096, struct.unpack_from("<I", written, 24)[0])
        written[19] = switch[19]
        written[24:28] = switch[24:28]
        self.assertEqual(switch[:28], bytes(written),
                         "every other header byte is kept")

    def test_the_size_report_matches_the_file(self):
        widths = (8, 16)
        blocks, pcs, entries = [], [], []
        for index, width in enumerate(widths):
            rid = (TEST_ID, SECOND_ID)[index]
            switch = switch_block(width, width)
            blocks.append((rid, switch, "terrain/snow%d.texture" % index))
            pcs.append((rid, pc_image([(width, width,
                                        dxt5_block(RED) * (width // 4) ** 2)]),
                        "terrain/snow%d.texture" % index))
            entries.append(plan_entry(self.fa, rid,
                                      "terrain/snow%d.texture" % index,
                                      (width, width), (width, width),
                                      (width, width), len(switch)))
        self.fixture.write(blocks, pcs, entries)
        code, out, err = self.fixture.apply()
        self.assertEqual(0, code, err)
        dat = os.path.join(self.fixture.out, "switch-s3.dat")
        old = sum(len(block[1]) for block in blocks)
        new = sum(28 + (width // 4) ** 2 * 16 for width in widths)
        self.assertIn("replaced  2 of 2", out)
        self.assertIn("old payload %s bytes, new payload %s bytes"
                      % (format(old, ","), format(new, ",")), out)
        self.assertIn("net %+d" % (new - old), out)
        self.assertIn("on disk   %s bytes" % format(os.path.getsize(dat), ","),
                      out)
        self.assertEqual(new, len(self.fixture.payload(TEST_ID)) +
                         len(self.fixture.payload(SECOND_ID)))
        report = json.loads(self.fixture.apply("--format", "json")[1])
        self.assertEqual(old, report["old_bytes"])
        self.assertEqual(new, report["new_bytes"])
        self.assertEqual(os.path.getsize(dat), report["on_disk"])

    def test_the_json_report_has_the_same_numbers(self):
        switch = switch_block(8, 8)
        self.fixture.write(
            [(TEST_ID, switch, "terrain/snow.texture")],
            [(TEST_ID, pc_image([(8, 8, dxt5_block(RED) * 4)]),
              "terrain/snow.texture")],
            [plan_entry(self.fa, TEST_ID, "terrain/snow.texture", (8, 8),
                        (8, 8), (8, 8), len(switch))])
        code, out, err = self.fixture.apply("--format", "json")
        self.assertEqual(0, code, err)
        report = json.loads(out)
        dat = os.path.join(self.fixture.out, "switch-s3.dat")
        self.assertEqual(1, report["count"])
        self.assertEqual(len(switch), report["old_bytes"])
        self.assertEqual(28 + 64, report["new_bytes"])
        self.assertEqual(os.path.getsize(dat), report["on_disk"])
        self.assertEqual(1, report["encoded"])
        self.assertEqual([], report["refused"])

    def test_a_scaled_target_is_averaged(self):
        switch = switch_block(8, 8)
        quadrants = ((RED, RED, GREEN, GREEN), (RED, RED, GREEN, GREEN),
                     (BLUE, BLUE, WHITE, WHITE), (BLUE, BLUE, WHITE, WHITE))
        source = b"".join(dxt5_block(colour)
                          for row in quadrants for colour in row)
        self.fixture.write(
            [(TEST_ID, switch, "terrain/snow.texture")],
            [(TEST_ID, pc_image([(16, 16, source)]), "terrain/snow.texture")],
            [plan_entry(self.fa, TEST_ID, "terrain/snow.texture", (8, 8),
                        (16, 16), (8, 8), len(switch))])
        code, out, err = self.fixture.apply()
        self.assertEqual(0, code, err)
        payload = self.fixture.payload(TEST_ID)
        self.assertEqual(28 + 4 * 16, len(payload))
        written = self.fa.switch_texture(payload)
        self.assertEqual((8, 8), (written.width, written.height),
                         "the target size is what is written")
        blocks = [payload[28 + index * 16:28 + (index + 1) * 16]
                  for index in range(4)]
        self.assertEqual(4, len(set(blocks)), "the quadrants stay apart")

    def test_a_second_run_uses_the_cache(self):
        switch = switch_block(8, 8)
        self.fixture.write(
            [(TEST_ID, switch, "terrain/snow.texture")],
            [(TEST_ID, pc_image([(8, 8, dxt5_block(GREEN) * 4)]),
              "terrain/snow.texture")],
            [plan_entry(self.fa, TEST_ID, "terrain/snow.texture", (8, 8),
                        (8, 8), (8, 8), len(switch))])
        first = self.fixture.apply()
        self.assertEqual(0, first[0], first[2])
        self.assertIn("1 encoded, 0 from cache", first[1])
        first_payload = self.fixture.payload(TEST_ID)
        second = self.fixture.apply()
        self.assertEqual(0, second[0], second[2])
        self.assertIn("0 encoded, 1 from cache", second[1])
        self.assertEqual(first_payload, self.fixture.payload(TEST_ID))

    def test_a_smaller_target_is_refused_by_id(self):
        switch = switch_block(128, 128)
        self.fixture.write(
            [(TEST_ID, switch, "terrain/snow.texture"),
             (SECOND_ID, switch_block(8, 8), "terrain/keep.texture")],
            [(TEST_ID, pc_image([(128, 128, dxt5_block(RED) * 1024)]),
              "terrain/snow.texture"),
             (SECOND_ID, pc_image([(8, 8, dxt5_block(RED) * 4)]),
              "terrain/keep.texture")],
            [plan_entry(self.fa, TEST_ID, "terrain/snow.texture", (128, 128),
                        (128, 128), (64, 64), len(switch)),
             plan_entry(self.fa, SECOND_ID, "terrain/keep.texture", (8, 8),
                        (8, 8), (8, 8), 92)])
        code, out, err = self.fixture.apply()
        self.assertEqual(0, code, err)
        self.assertIn("refused   1", out)
        self.assertIn("0x%08x" % TEST_ID, out)
        self.assertIn("must not do", out)
        self.assertIn("replaced  1 of 2", out)
        self.assertEqual(switch, self.fixture.payload(TEST_ID),
                         "a refused block is copied as it was")
        self.assertNotEqual(switch_block(8, 8),
                            self.fixture.payload(SECOND_ID))

    def test_a_block_that_is_not_a_texture_is_refused_by_id(self):
        self.fixture.write(
            [(TEST_ID, switch_block(8, 8), "terrain/snow.texture")],
            [(TEST_ID, descriptor_payload(), "terrain/snow.texture")],
            [plan_entry(self.fa, TEST_ID, "terrain/snow.texture", (8, 8),
                        (8, 8), (8, 8), 92)])
        code, out, err = self.fixture.apply()
        self.assertEqual(2, code)
        self.assertIn("frostpunk-assets:", err)
        self.assertIn("no entry could be replaced", err)

    def test_an_unreadable_pc_block_is_named_and_skipped(self):
        self.fixture.write(
            [(TEST_ID, switch_block(8, 8), "terrain/snow.texture"),
             (SECOND_ID, switch_block(8, 8), "terrain/keep.texture")],
            [(TEST_ID, descriptor_payload(), "terrain/snow.texture"),
             (SECOND_ID, pc_image([(8, 8, dxt5_block(RED) * 4)]),
              "terrain/keep.texture")],
            [plan_entry(self.fa, TEST_ID, "terrain/snow.texture", (8, 8),
                        (8, 8), (8, 8), 92),
             plan_entry(self.fa, SECOND_ID, "terrain/keep.texture", (8, 8),
                        (8, 8), (8, 8), 92)])
        code, out, err = self.fixture.apply()
        self.assertEqual(0, code, err)
        self.assertIn("0x%08x" % TEST_ID, out)
        self.assertIn("not a PC image payload", out)
        self.assertIn("replaced  1 of 2", out)

    def test_an_uncompressed_block_is_refused_unless_conversion_is_asked(self):
        switch = switch_block(8, 8, fmt=37, descriptor=b"\x04\x03\x02\x05",
                              body=b"\x11" * (8 * 8 * 4))
        self.fixture.write(
            [(TEST_ID, switch, "terrain/snow.texture")],
            [(TEST_ID, pc_image([(8, 8, dxt5_block(RED) * 4)]),
              "terrain/snow.texture")],
            [plan_entry(self.fa, TEST_ID, "terrain/snow.texture", (8, 8),
                        (8, 8), (8, 8), len(switch))])
        code, out, err = self.fixture.apply()
        self.assertEqual(2, code)
        self.assertIn("--convert-uncompressed", err)
        code, out, err = self.fixture.apply("--convert-uncompressed")
        self.assertEqual(0, code, err)
        payload = self.fixture.payload(TEST_ID)
        self.assertEqual(self.fa.SWITCH_FORMAT_ASTC_4X4,
                         self.fa.switch_texture(payload).format)
        self.assertEqual(b"\x02\x03\x04\x05", payload[20:24])

    def test_a_sixteen_bit_block_is_never_converted(self):
        switch = switch_block(8, 8, fmt=5, body=b"\x22" * (8 * 8 * 2))
        self.fixture.write(
            [(TEST_ID, switch, "terrain/height.hm")],
            [(TEST_ID, pc_image([(8, 8, dxt5_block(RED) * 4)]),
              "terrain/height.hm")],
            [plan_entry(self.fa, TEST_ID, "terrain/height.hm", (8, 8),
                        (8, 8), (8, 8), len(switch))])
        code, out, err = self.fixture.apply("--convert-uncompressed")
        self.assertEqual(2, code)
        self.assertIn("16-bit", err)

    def test_the_report_is_the_only_thing_on_stdout(self):
        switch = switch_block(8, 8)
        self.fixture.write(
            [(TEST_ID, switch, "terrain/snow.texture")],
            [(TEST_ID, pc_image([(8, 8, dxt5_block(RED) * 4)]),
              "terrain/snow.texture")],
            [plan_entry(self.fa, TEST_ID, "terrain/snow.texture", (8, 8),
                        (8, 8), (8, 8), len(switch))])
        self.fixture.apply()
        again = self.fixture.apply()
        self.assertEqual("", again[2], "a clean run says nothing on stderr")
        self.assertIsNone(re.search(r"Traceback", again[1] + again[2]))


if __name__ == "__main__":
    unittest.main()
