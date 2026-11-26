#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the ScarletNX contributors
"""The code this fork has and origin/main does not, held to the checkout, each miss a line that names its file:

The port branch carries patches that upstream has no reason to keep: the pointer table windows of the legacy shader
translator (a register-indexed LDC names a constant bank's table, and the translator turns each window of that table
into a global buffer of its own), the tests that guard those windows, and the packer that turns a port folder into an
AppImage. A merge that resolves a conflict in these files by taking the upstream side loses a patch with no compile
error and no failing test. The merge 5a6ccccfe did that: ctest -R shader stayed green and one game drew no models.

This check reads a marker of each patch. It cannot tell whether the code still works. Run the shader tests and one
game's affected scene for that.

    scripts/check-port-fork.py [--root DIR]

Exit 0, 1 for a missing patch, 2 for a file it cannot read (ctest's failure, not a skip: the file is tracked).
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# (file, marker, what the marker is): the file must hold each marker, else the patch is gone
PATCHES = (
    ('src/nx/gpu/shader/spirv_translate.c', 'SH_GBUF_TABLE_KEY', "the key of a constant bank's table window"),
    ('src/nx/gpu/shader/spirv_translate.c', 'needs_mbase', 'the declaration of SNX_VKB_MBASE'),
    ('src/nx/gpu/shader/translate.h', 'mbase_pointer', "the window binding's pointer of the emitter's context"),
    ('src/nx/gpu/shader/emit_memory.c', 'SNX_VKB_MBASE', "a table window's base in a dynamic access"),
    ('src/nx/gpu/vk/pipeline.c', 'if (i == SNX_VKB_MBASEFS) return VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER;',
     "a fragment program's own window binding, bound as a uniform buffer"),
    ('tests/shader_direct_regressions.c', 'indexed_fragment', 'the tests of the indexed LDC tables'),
    ('scripts/make-port-appimage.sh', 'SNX_PORT_RESOLUTION', "the port packer's render resolution"),
)


def check(root):
    """(the misses as (file, marker, what), the file it cannot read as an error line)"""
    misses, unreadable = [], None
    for name, marker, what in PATCHES:
        try:
            text = (root / name).read_text(encoding='utf-8', errors='replace')
        except OSError as error:
            unreadable = f'check-port-fork: {name}: {error.strerror}'
            break
        if marker not in text:
            misses.append((name, marker, what))
    return misses, unreadable


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--root', default=str(ROOT), help='the checkout to read (the default is this one)')
    args = parser.parse_args()
    root = Path(args.root)
    if not root.is_dir():
        print(f'check-port-fork: {root} is not a directory')
        return 2
    misses, unreadable = check(root)
    if unreadable:
        print(unreadable)
        return 2
    for name, marker, what in misses:
        print(f'{name}: no {marker!r}, so the fork patch is gone: {what}')
    if misses:
        print(f'check-port-fork: {len(misses)} of {len(PATCHES)} fork patches are missing')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
