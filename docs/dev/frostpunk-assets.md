# Frostpunk's asset containers

Frostpunk keeps its assets in `.dat` files, one per kind of resource (textures,
scenes, sounds, videos), each paired with a `.idx` that says where every block
is. The Switch build adds a `.str` that maps a resource id to the name the
game's build tools gave it. The PC build uses the same family of files, so a
resource can be matched across the two by id.

Everything below was measured on the Switch package
(`Frostpunk/0100B1C0249B4000_data/romfs/textures-s3.*`, 4238 resources, 2 GB)
and cross-checked against the PC package (`textures-s3.*`, 4178 resources,
6.8 GB). `scripts/frostpunk-assets.py` reads and writes these files.

## Index

* [`.idx`](#idx)
* [`.dat`](#dat)
* [`.str`](#str)
* [Texture payloads](#texture-payloads)
* [Taking stock of a package](#taking-stock-of-a-package)
  * [Block kinds and their evidence](#block-kinds-and-their-evidence)
* [Replacing one resource](#replacing-one-resource)
* [Round-trip differences](#round-trip-differences)
* [What was proven](#what-was-proven)

## `.idx`

An 11-byte header, then one fixed **29-byte record** per resource:

| offset | type | meaning |
| --- | --- | --- |
| 0 | u32 | resource id |
| 4 | u32 | bytes the block occupies in `.dat` (stored size) |
| 8 | u32 | zero in every record seen |
| 12 | u32 | bytes the block decompresses to (payload size) |
| 16 | u32 | zero in every record seen |
| 20 | u32 | **byte offset of the block in `.dat`** |
| 24 | u32 | zero in every record seen |
| 28 | u8 | flags, `1` in every record seen |

Two earlier guesses were wrong and are worth recording so they are not made
again: offset 12 is the *payload* size, not a hash, and offset 20 is the block's
absolute offset, not an order key. With offset 20 read as an offset, all 4238
Switch records decode to exactly the payload size at offset 12, which is what
proves the reading.

The records are sorted by id in the shipped files, but the record order carries
no meaning: a reader must use the offsets.

## `.dat`

The blocks, in offset order. A block is either

* a zstd frame — it starts with `28 b5 2f fd` and decompresses to the payload
  size; or
* stored raw — the stored bytes are the payload.

Blocks do not tile the file exactly. On the Switch package 56 spans, 17 748 023
bytes in total, belong to no record; they look like data left behind by a patch.
A reader must therefore seek by offset rather than walk. When this tool rewrites
a set it drops the unowned spans and rewrites every offset, which keeps the
result smaller and still readable by the game.

## `.str`

An 11-byte header, then `u32 id`, `u32 name length`, and the UTF-8 name, for
each resource. The switch build's 4238 names cover all 4238 records. The PC
build ships no `.str`; its names live elsewhere in that package.

## Texture payloads

Most payloads are textures and start with `65 78 1c 00`. The 28-byte header is

| offset | type | meaning |
| --- | --- | --- |
| 0 | u32 | `0x001c7865` |
| 4 | u32 | format id: `72` on 3725 of 4172 texture blocks, `37` on 224, `1` on 172, `5` on 46 |
| 8 | u32 | 1 or 2; not yet pinned down |
| 12 | u16 | width |
| 14 | u16 | height |
| 16 | 12 bytes | format, block and mip fields; not yet pinned down |

Payload sizes then follow the format id. The 1973 blocks whose size is exactly
`28 + ceil(w/4)*ceil(h/4)*16` are one 4×4 ASTC block per tile with no mip chain.
The rest do not follow that rule at the stated width and height (a 2048×2048
atlas carries 5 596 160 payload bytes where 4×4 ASTC would need 4 194 304), so
the remaining format ids and their extra bytes are still open. Replacing a
texture means writing a payload whose own header describes it; the container
itself does not care what the bytes are.

Not every payload is a texture: 62 are Stingray XML `<TextureArrayDescriptor>`
documents naming the original PC `.dds` files, and 4 are plain 32-bit TGA
(cursors) ending in a `TRUEVISION-XFILE` footer.

## Taking stock of a package

`inventory` lists every record in one pass: resource id, name, block kind,
declared width and height, offset in the `.dat`, and payload and stored sizes.
It is the way to go from "that blurry thing on screen" to "this texture in the
package" without reading the tool's source:

```
scripts/frostpunk-assets.py inventory <dat>                          # all rows
scripts/frostpunk-assets.py inventory <dat> --name vignette          # substring
scripts/frostpunk-assets.py inventory <dat> --prefix ui/menu/        # prefix
scripts/frostpunk-assets.py inventory <dat> --min-width 1024 --kind texture
scripts/frostpunk-assets.py inventory <dat> --max-payload 5000       # small blocks
scripts/frostpunk-assets.py inventory <dat> --sort size --reverse --limit 20
scripts/frostpunk-assets.py inventory <dat> --format json --out rows.json
scripts/frostpunk-assets.py inventory <dat> --format tsv --out rows.tsv
```

Filters combine; a record must pass all of them. `--sort` takes `offset`
(default), `id`, `name`, `size` (payload bytes) and `stored`; `--reverse` turns
it around, so `--sort size --reverse` is the list of resources worth upgrading
first. Name and payload-size filters are applied before a block is read, so a
query does not pay for decompressing the rest of the package.

### Block kinds and their evidence

`kind` is decided from the payload bytes alone, in this order:

| kind | evidence |
| --- | --- |
| `texture` | the 28-byte Switch texture header: magic `0x001c7865` at +0, width and height as u16 at +12/+14 |
| `descriptor` | a Stingray XML document that begins with `<TextureArrayDescriptor` |
| `tga` | an uncompressed TGA, whose 26-byte footer carries `TRUEVISION-XFILE` (checked in the last 32 bytes) |
| `unknown` | none of the above. A block with any other header is not guessed at |

Width and height are printed only for `texture` blocks; every other kind prints
`-` (`null` in JSON, an empty field in TSV). A record with no `.str` entry is
reported as unnamed (`-`, empty, `null`); no name is ever made up.

On the Switch package the command prints 4238 rows — 4172 `texture`, 62
`descriptor`, 4 `tga`, 0 `unknown` — 4238 of them named and 4172 with a width
and height. The one-line summary after the rows states those counts:

```
inventory: 4238 rows (4238 named, 0 unnamed); kinds descriptor=62, texture=4172, tga=4; 4172 with width/height
```

`--out` writes the listing to a file and prints `wrote <path> (<n> rows)`
instead of the rows; `--format json` writes a list of objects with `id`,
`id_hex`, `name` (null when unnamed), `kind`, `width`/`height` (null when the
kind declares none), `offset`, `payload` and `stored`; `--format tsv` writes a
header line and one tab-separated row per resource, with empty fields where
JSON has null.

## Replacing one resource

```
scripts/frostpunk-assets.py info    <dat>                    # record, name, gap counts
scripts/frostpunk-assets.py list    <dat> --name ui/tiles    # offsets, payload sizes
scripts/frostpunk-assets.py inventory <dat> --name ui/tiles  # + kind, size, export
scripts/frostpunk-assets.py extract <dat> <name|id> --out f  # payload bytes
scripts/frostpunk-assets.py replace <dat> <name|id> <payload> --out DIR
scripts/frostpunk-assets.py digest  <dat> --out f.json       # per-resource payload hash
scripts/frostpunk-assets.py roundtrip <dat> --out DIR        # rewrite and compare every payload
scripts/frostpunk-assets.py verify  <dat>                    # every block reads back
```

`replace` refuses a payload of a different size unless `--allow-size-change` is
given. A malformed `.idx` (a record that runs past the end of the `.dat`, two
records overlapping, a size that does not multiply out) is reported with the id
of the record at fault.

## Round-trip differences

`roundtrip` is the self-consistency check: it reads every payload, writes the
set to a new directory, reads that set back and compares the id set, the names,
the payload size and the sha256 of every payload. A difference is printed with
the resource id and the command exits 1; a corrupt block is reported the same
way and nothing is written. A clean run prints
`roundtrip ok: <n> records, every payload hash matches`.

These differences are **allowed**, and none of them changes a resource:

* unowned spans between blocks are dropped, so `.dat` gets shorter and every
  offset after a dropped span moves;
* the `.idx` offset field (+20) is recomputed for every record, and the stored
  size (+4) only when a payload is replaced and recompressed;
* a replaced payload is recompressed, so its stored bytes and stored size, and
  the zstd frame boundaries, differ from the original;
* the `.idx` header, the bytes at +8, +16 and +24 (zero), and the flag byte are
  copied through, and `.str`, when the set has one, is copied byte for byte.

These differences are **not allowed**; any one of them is a failed round-trip:

* the set of resource ids, the record count, or a name;
* the decompressed payload bytes of any resource (compared by sha256), which
  includes a texture's width/height/format header;
* the decompressed payload size of any resource (`.idx` +12), so the size the
  game sees stays the same.

The `frostpunk_roundtrip` ctest entry builds a synthetic container in a
temporary directory and runs this check, so it needs no game data. Running it
against the real package is the same command with that package's `.dat`.

## What was proven

Swapping the payloads of two 2048×2048 HUD atlases
(`ui/tiles/varioushudpanelsatlas.texture` and
`ui/tiles/selectionpanelatlas.texture`, both 5 596 188 bytes with identical
headers) and repacking moved the game's generator panel to the other atlas's
artwork. The run stayed at 53.4 fps with no shader refused, the other 4236
resources were untouched byte for byte, and putting the original container back
restored the panel.

Two runs of the same 300 s script, one on the repacked set and one on the
original, differ over 36.6% of the panel's pixels, over 1.7% of the rest of the
frame (snowfall and animation) and over 0.0% of the quest panel in the bottom
left.
