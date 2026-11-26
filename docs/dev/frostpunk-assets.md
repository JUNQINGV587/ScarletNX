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

## Replacing one resource

```
scripts/frostpunk-assets.py info    <dat>                    # record, name, gap counts
scripts/frostpunk-assets.py list    <dat> --name ui/tiles    # offsets, payload sizes
scripts/frostpunk-assets.py extract <dat> <name|id> --out f  # payload bytes
scripts/frostpunk-assets.py replace <dat> <name|id> <payload> --out DIR
scripts/frostpunk-assets.py digest  <dat> --out f.json       # per-resource payload hash
scripts/frostpunk-assets.py verify  <dat>                    # every block reads back
```

`replace` refuses a payload of a different size unless `--allow-size-change` is
given. A malformed `.idx` (a record that runs past the end of the `.dat`, two
records overlapping, a size that does not multiply out) is reported with the id
of the record at fault.

## What was proven

Swapping the payloads of two 2048×2048 HUD atlases
(`ui/tiles/varioushudpanelsatlas.texture` and
`ui/tiles/selectionpanelatlas.texture`, both 5 596 188 bytes with identical
headers) and repacking moved the game's generator panel to the other atlas's
artwork. The run stayed at 53.4 fps with no shader refused, the other 4236
resources were untouched byte for byte, and putting the original container back
restored the panel.
