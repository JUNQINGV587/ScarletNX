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
  * [The PC build's payloads](#the-pc-builds-payloads)
* [Taking stock of a package](#taking-stock-of-a-package)
  * [Block kinds and their evidence](#block-kinds-and-their-evidence)
* [Aligning the two packages](#aligning-the-two-packages)
* [Planning the replacements](#planning-the-replacements)
  * [The whitelist](#the-whitelist)
  * [The target size](#the-target-size)
  * [The budget](#the-budget)
  * [The plan file](#the-plan-file)
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
  size;
* a gzip stream — it starts with `1f 8b 08` and decompresses to the payload
  size. Every block of the PC package is one; the tool picks the branch from
  the first bytes, so one reader handles both builds; or
* stored raw — the stored bytes are the payload (the 4 TGA cursors on Switch).

Blocks do not tile the file exactly. On the Switch package 56 spans, 17 748 023
bytes in total, belong to no record; they look like data left behind by a patch.
A reader must therefore seek by offset rather than walk. When this tool rewrites
a set it drops the unowned spans and rewrites every offset, which keeps the
result smaller and still readable by the game.

## `.str`

An 11-byte header, then `u32 id`, `u32 name length`, and the UTF-8 name, for
each resource. The switch build's 4238 names cover all 4238 records.

The PC build ships no `.str`, and its names are not lost either: the same
resource id names the same asset in both builds, so the Switch `.str` names the
PC package too. 139 of the 4178 PC ids have no Switch record and stay unnamed;
the alignment report groups them under `(unnamed)`.

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

### The PC build's payloads

A PC texture payload has its own header, and its levels are listed smallest
first:

| offset | type | meaning |
| --- | --- | --- |
| 0 | u32 | width |
| 4 | u32 | height, or `0xffffffff` for a texture array or cube map |
| 8 | 4 bytes | block format: `DXT1`, `DXT3`, `DXT5`, or a numeric format id |
| 12 | u32 | level count, 1 to 16 |
| 16 | 16 × 8 bytes | one slot per level: u16 width, u16 height, u32 bytes |
| 144 | | the pixel blocks of every level, one slice after another |

The payload is exactly `144 + sum(level bytes) * slices` long, and the level
table is the authority rather than the width and height at +0: a mip chain
starts at 1x1 and ends at the full size, so the last slot must agree with +0/+4
— unless the texture is an array or cube map, where +4 is `0xffffffff` and the
size comes from the last slot alone. A block format holds `ceil(w/4)*ceil(h/4)`
blocks of 8 (`DXT1`) or 16 bytes (`DXT3`, `DXT5`); any other format id holds raw
pixels of 1, 2, 4 or 8 bytes each (`raw4`, the most common of them, is the
numeric format id 21).

Every one of the 4178 PC blocks decompresses to a payload that satisfies this
rule, and 4112 of them are images: `dxt5` 3181, `raw4` 857, `raw2` 46, `raw1`
24, `dxt3` 3, `dxt1` 1. The other 66 are 62 `<TextureArrayDescriptor>` documents
and 4 TGAs, as on Switch. A payload that breaks one of the rules is reported as
`unknown` and never guessed at.

The level count is the reason an estimate must not be taken from the PC payload
size: a 1024x1024 `dxt5` payload is 1 398 272 bytes because it carries 11
levels, while a replacement in the Switch format holds no mip chain.

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
| `pc-texture` | a PC image payload whose level table multiplies out to the bytes after +144 and whose last level agrees with +0/+4 |
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

## Aligning the two packages

`align` answers "which resources exist on both sides" before any work is
planned, and checks every PC block while it is there:

```
scripts/frostpunk-assets.py align --switch sw/textures-s3.dat --pc pc/textures-s3.dat
scripts/frostpunk-assets.py align --switch ... --pc ... --format json --out align.json
```

Records are matched by resource id. The report gives three counts, then one row
per name group — the PC blocks, the ids in both, only on Switch, only on PC, and
for the PC blocks how many are readable, image, readable but not an image, and
unreadable. A block is readable when it decompresses to exactly the payload size
at +12; it is an image when its bytes prove a payload of the layout above.
Reading every PC block is the point of the command: an offset or a stream type
the reader gets wrong shows up here as an unreadable block, not as a corrupt
package later.

Measured on the real packages:

```
switch  .../romfs/textures-s3.dat           4,238 records, 2,022,116,141 bytes
pc      /data/pcfrostpunk/textures-s3.dat   4,178 records, 6,782,072,018 bytes

alignment by id: 4,039 in both, 199 only on Switch, 139 only on PC

group                      pc   both  sw-only  pc-only  readable   image  other  unreadable
ui                       2448   2448       56        0      2448    2448      0           0
buildings                 457    457        2        0       457     457      0           0
environment               336    336        3        0       336     336      0           0
graphics                  252    252       71        0       252     194     58           0
effects                   178    178        5        0       178     178      0           0
(unnamed)                 139      0        0      139       139     135      4           0
...
(total)                  4178   4039      199      139      4178    4112     66           0

pc blocks by kind: dxt5 3,181, raw4 857, descriptor 62, raw2 46, raw1 24, tga 4, dxt3 3, dxt1 1
images: 4,112, 1,054 of them not a power of two on both sides
```

The `graphics` group is where most of the non-image blocks sit: 58 of them are
`<TextureArrayDescriptor>` documents, the same kind the Switch package carries,
and the 4 TGAs are the `cursors` group. An unreadable count of zero over the
whole package is the check that the 64-bit offset above is right.

An id that only one package has is listed with its kind, size and name and is
left alone: with no counterpart there is nothing to copy from or to, and the
replacement pipeline does not invent one.

## Planning the replacements

`plan` is the step between "which resources align" and "write a package":

```
scripts/frostpunk-assets.py plan --switch sw/textures-s3.dat --pc pc/textures-s3.dat \
    --out plan.jsonl --budget 8G
scripts/frostpunk-assets.py plan --read plan.jsonl
scripts/frostpunk-assets.py plan --read plan.jsonl --budget 4G
```

It writes the plan file, prints the same summary `--read` prints, and refuses to
write anything when the plan would exceed the budget. `--read` recomputes every
total from the lines that are in the file, so a plan edited by hand is measured
as it now is.

### The whitelist

The category of a resource is decided by its name, and the first rule that
matches decides:

| category | the name starts with |
| --- | --- |
| `terrain` | `environment/terrain/`, `environment/builders/`, `environment/snow system/`, `graphics/environment/`, `citydetail/` |
| `buildings` | `buildings/` |
| `rocks` | `environment/rocks/`, `environment/endless/`, or any other name under `environment/` whose last path segment contains `rock` |

`--categories` takes a comma list or `all`; the default is
`terrain,buildings,rocks`. `--include PREFIX` adds a name the table does not
cover and `--exclude PREFIX` removes one; exclude wins over include and over the
table, so a folder can be carved out of a category.

A category takes every channel map of the textures in its folders — `_col`,
`_alb`, `_dif`, `_hgt`, `_nor`, `_nm`, `_pbr`, `_rgh`, `_alp`, `_mask` — because
the rule matches a folder prefix, not a file name: a detail map sits next to its
colour map and follows it with no second list to keep in step. A name that
matches no category is not in the plan at all, which is what keeps the roughly
8 GB of a full 2x upgrade out of the package.

### The target size

`--target-size` is either a scale, `2x` by default, applied to the size the
Switch texture declares, or a pixel count that caps the longest side.
`--max-size` (2048 by default) then caps each side, and the PC source caps it
once more: a replacement never asks for more pixels than the PC payload holds. A
result smaller than the Switch texture on either side is skipped with its
reason instead of being downscaled, and every skip is printed and counted, never
silent.

Nothing is rounded to a power of two. A 1010x534 source keeps those sides, and
the ASTC cost rounds up per block, so a non-power-of-two texture is a normal
case and not an error. The estimate for a replacement is
`28 + ceil(w/4)*ceil(h/4)*16` bytes — the header the Switch payloads carry and
no mip chain — because that is what the replacement will cost, not what the PC
payload happens to weigh.

### The budget

`--budget` takes bytes with an optional `K`, `M`, `G` or `T` suffix and is
checked before the plan is written. The projected package is the Switch `.dat`
size, less the bytes no record owns, plus the sum of every entry's growth;
`write` keeps the unowned spans out and copies untouched blocks byte for byte,
so the projection is an upper bound rather than a guess the other way. Over
budget, nothing is written and the command exits 3, saying by how many bytes it
went over:

```
frostpunk-assets: plan: over budget by 427,436,429 bytes: the replacement package
would be 2,574,920,077 bytes and the budget is 2,147,483,648 bytes; nothing
written.  Raise --budget, narrow --categories, or lower --target-size.
```

### The plan file

The file is JSON Lines: one object per line, so `diff` shows one changed entry
per line and a person can delete a line to drop a replacement. Line 1 is the
header: `plan` (`frostpunk-assets/replace-plan`), `version`, `switch`, `pc`,
`switch_records`, `pc_records`, `switch_dat_bytes`, `switch_gap_bytes`,
`target_size`, `max_size`, `categories`, `include`, `exclude`, `budget`, and
`astc` (`block`, `header`, `bytes_per_block`).

Every other line is one entry: `id` (`0x` and eight hex digits), `category`,
`name`, `switch` (`[w, h]` as the Switch package declares it), `pc` (`[w, h]` of
the PC payload), `target` (`[w, h]` of the replacement), `switch_payload`,
`switch_stored`, `pc_payload`, `estimated` (the ASTC cost) and `delta`
(`estimated - switch_stored`).

No total is stored: every count and every byte total is recomputed from the
lines that are present, which is why deleting an entry line shrinks the estimate
by that entry's `delta` and nothing else. Blank lines and lines starting with
`#` are skipped, so a note can be added. A line that is not JSON, an entry with
a missing field, and a header with another tag or version are reported with
their line number instead of being half-read.

On the real packages `plan` with the default whitelist and `--budget 8G` gives:

```
entries   818 replaced
switch payload 1,113,843,064 bytes, replacement 895,778,424 bytes, growth 570,551,959 bytes
projected package 2,574,920,077 bytes, 6,015,014,515 bytes under the budget of 8,589,934,592
skipped 58: the Switch block is descriptor, not a texture
skipped 58: would be 2048x2048, smaller than the Switch texture 4096x4096
```

The skipped 4096x4096 textures are the `--max-size` default doing its job: at
2048 they would be a downgrade, so they stay as they are. Growth is the sum of
every entry's `delta`, and a `delta` can be negative: a Switch payload that
carries a mip chain is larger than the ASTC replacement that carries none, as
`0x06bc77aa` shows at -1,988,085 bytes.

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
* the `.idx` offset (+20 and +24, the two halves) is recomputed for every
  record, and the stored size (+4) only when a payload is replaced and
  recompressed;
* a replaced payload is recompressed, so its stored bytes and stored size, and
  the zstd frame boundaries, differ from the original;
* the `.idx` header, the bytes at +8 and +16 (zero), and the flag byte are
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
