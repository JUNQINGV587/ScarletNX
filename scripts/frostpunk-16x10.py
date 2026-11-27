#!/usr/bin/env python3
"""Give Frostpunk's Switch build a 16:10 scene: 1280x800 instead of 1280x720.

The scene size is not in the renderer. NX64StandaloneGameMain writes the
handheld pair (1280, 720) into EngineConfig+0x2d0/+0x2d4 at start-up; every
render target follows from there. These two `mov esi, 720` become 800.

    scripts/frostpunk-16x10.py IN OUT
"""
import sys

PATCH = (0x1217a8f9, 0x1217a902)
OLD = b"\xd0\x02\x00\x00"          # mov esi, 720
NEW = b"\x20\x03\x00\x00"          # mov esi, 800


def main(argv):
    if len(argv) != 3:
        sys.exit(__doc__)
    data = bytearray(open(argv[1], "rb").read())
    for at in PATCH:
        if data[at:at + 4] != OLD:
            sys.exit("frostpunk-16x10: %#x holds %s, not the 720 this patch knows"
                     % (at, data[at:at + 4].hex()))
        data[at:at + 4] = NEW
    open(argv[2], "wb").write(data)
    print("frostpunk-16x10: %d sites, the scene is 1280x800" % len(PATCH))


if __name__ == "__main__":
    main(sys.argv)
