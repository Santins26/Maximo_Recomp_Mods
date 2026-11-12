# PRS mesh-table structure layouts recovered from the debug build

Research only. No module was modified. Everything labelled MEASURED was read
out of `game_files/MAXDEMOR.ELF` (debug build, full `.symtab`) or out of real
`.PRS` bytes; everything labelled INFERRED is a conclusion drawn from those
measurements.

Method: same one that worked for the sound driver and the texture research --
read the engine's own loader chain instead of guessing at the data. Symbol
extraction via `research/tools/extract_elf_symbols.py`, disassembly via
`research/tools/mips_disasm.py`. The disassembler decoded every function
below cleanly: prologue/epilogue `sq`/`lq` pairs balanced, `paddub`-as-`move`
rendered sensibly, all `jal` targets resolved to named symbols, no `.word`
placeholders in any control-flow position (the only undecoded words were
COP1 float ops in code paths irrelevant to file layout). Listings are
trustworthy for this purpose.

## Verdict

**FIXABLE -- structures recovered.**

There is **no offset table**; the mechanism is two field-offset corrections
plus one new tag case. With those three changes all **11** `.PRS` files in
`game_files/` walk end-to-end with **zero resyncs**, and in every one of them
the mesh table finishes exactly at a byte where the next word is the next
resource-table type id (`2` = anim table). That end-of-table alignment is an
independent check that nothing was silently absorbed.

| file | uNumMeshes | entries parsed | resyncs |
|---|---|---|---|
| CASTLE.PRS | 59 | 59 | 0 |
| CASTLE_K.PRS | 22 | 22 | 0 |
| CASTLE_Q.PRS | 4 | 4 | 0 |
| UNDER.PRS | 40 | 40 | 0 |
| UNDER_B.PRS | 30 | 30 | 0 |
| ICE.PRS | 41 | 41 | 0 |
| ICE_B.PRS | 29 | 29 | 0 |
| SWAMP.PRS | 42 | 42 | 0 |
| SWAMP_B.PRS | 14 | 14 | 0 |
| GRAVE.PRS | 39 | 39 | 0 |
| GLOBAL.PRS | 164 | 164 | 0 |

(MEASURED.)

## Symbols found

Addresses are virtual; file offset = `0x80 + (va - 0x100000)` in the `main`
section.

| symbol | addr | size |
|---|---|---|
| `Load__16PResourceManagerFPCcPCcbPQ216PResourceManager15tagResourceData` | 0x00168230 | 1468 |
| `LoadMeshResTable__16PResourceManagerFP5PFileP14PTextureLoader16ePsx2FileVersionPQ2...` | 0x00167C30 | 708 |
| `Load__9PMeshNodeFP5PFileP14PTextureLoader16ePsx2FileVersionb` | 0x00176CE0 | 1216 |
| `Load__9PSkeletonFP5PFileP14PTextureLoader16ePsx2FileVersion` | 0x001690D0 | 600 |
| `Load__5PBoneFP5PFileP14PTextureLoader16ePsx2FileVersion` | 0x0016A830 | 704 |
| `LoadSharedMesh__9PSkeletonFP5PFileP14PTextureLoader16ePsx2FileVersion` | 0x00169C20 | 276 |
| `Load__5PMeshFP5PFileP14PTextureLoaderUiUi16ePsx2FileVersionb` | 0x00173A20 | 424 |
| `Load__13PMeshFragmentFP5PFileP14PTextureLoader16ePsx2FileVersion` | 0x00172F50 | 636 |
| `Load__7PShaderFP5PFileP14PTextureLoader` | 0x001960A0 | 400 |
| `Load__11PRenderPassFP5PFileP14PTextureLoader` | 0x00195390 | 492 |
| `Load__7PSTAnimFP5PFile` | 0x00195BE0 | 448 |
| `Load__14PSTAnimChannelFP5PFile` | 0x00196390 | 244 |
| `LoadPolyTables__13PMeshFragmentFP5PFile` | 0x001734F0 | 140 |
| `Load__10PPolyTableFP5PFile` | 0x00167510 | 352 |
| `LoadVertexAnims__13PMeshFragmentFP5PFileUi` | 0x00173320 | 456 |
| `Load__11PVertexAnimFP5PFile` | 0x0016C4E0 | 416 |
| `Load__5PAnimFP5PFile` | 0x001740F0 | 784 |
| `LoadChannel__8PChannelFP5PFile` | 0x001660C0 | 584 |
| `LoadAnimResTable__16PResourceManagerFP5PFilePQ2...` | 0x00167F00 | 668 |
| `LoadTextureResTable__16PResourceManagerFP5PFileP14PTextureLoaderPQ2...` | 0x00167A70 | 444 |

(MEASURED.)

## The two field-offset bugs (this is the whole fix)

### 1. `PSTAnim` channel count is a u16 at header **+0**, not +2

`Load__7PSTAnimFP5PFile` @0x00195BE0:

```
00195C18: lw   $t9, 0($s2)          ; PFile vtable
00195C20: addiu $a2, $zero, 16      ; read 16 bytes -> sp+80
00195C44: lhu  $v0, 80($sp)         ; <-- channel count = u16 at hdr+0
00195C48: sh   $v0, 2($s3)          ; stored to this->numChannels
00195C58: jal  __nwa__              ; alloc count*4 channel pointers
00195D28: lhu  $v0, 2($s3)          ; loop bound
00195D2C: sltu $v0, $s0, $v0
00195D30: bne  $v0, $zero, 0x00195C94
```

`prs_mesh_parser.load_st_anim` reads the count from `hdr+2`. The `sh $v0,
2($s3)` is the *in-memory* field offset, which is what the `+2` looks like
if you read the store instead of the load.

`Load__14PSTAnimChannelFP5PFile` @0x00196390 is what the parser already
had: 16-byte header, `n = u32 @hdr+0` (must be non-zero, else Load fails),
then `n * 8` bytes.

So: **PSTAnim = 16 + sum over `u16@+0` channels of (16 + `u32@+0` * 8).**

### 2. `PVertexAnim` count is a u32 at header **+4**, not +0

`Load__11PVertexAnimFP5PFile` @0x0016C4E0:

```
0016C520: addiu $a2, $zero, 16      ; read 16 bytes -> sp+80
0016C544: lw   $v0, 80($sp)         ; hdr+0
0016C548: beq  $v0, $zero, ...      ; hdr+0 == 0  -> Load FAILS
0016C550: addiu $s1, $sp, 84        ; s1 = &hdr[4]
0016C554: lw   $v0, 0($s1)          ; <-- element count = u32 at hdr+4
0016C558: bne  $v0, $zero, 0x0016C570
0016C55C: sll  $a0, $v0, 4          ; count * 16
0016C570: jal  __nwa__              ; alloc count*16
0016C5A0: sll  $s2, $v0, 4
0016C5A4: jalr $t9                  ; File::Read(buf, count*16)
```

`prs_mesh_parser.load_vertex_anim` reads the count from `hdr+0` -- which is
the *keyframe-count / validity* word, not the element count.

So: **PVertexAnim = 16 + (`u32@+4`) * 16 bytes.** Total on-disk size is
exactly that; there is no trailing tag. `hdr+0` and `hdr+4` must both be
non-zero or the engine's Load returns failure, which makes them a good
sanity gate. The number of `PVertexAnim` records is *not* in the
`PVertexAnim` itself -- it is passed in by
`LoadVertexAnims__13PMeshFragmentFP5PFileUi` @0x00173320, whose `uNum`
argument comes from `PMeshFragment` header `u32@+8`, gated by `flags & 2`
(`Load__13PMeshFragmentFP5PFile` @0x001730F4-0x00173118). The parser
already had that part right.

For the record, the ICE `LoadVertexAnims` chunk named in the parser
docstring is a red herring for the mesh table: MEASURED, **ICE.PRS contains
zero `PVertexAnim` records**. Only CASTLE.PRS (19) and SWAMP.PRS (19) have
any. The Snowman `0x1D` walk problem lives in the *anim* resource table, not
here.

### Attribution: which bug breaks which enemy

CASTLE.PRS ablation, strict walk, first failing top-level entry (MEASURED):

| PSTAnim count offset | PVertexAnim count offset | entries parsed | first failure |
|---|---|---|---|
| +2 (current) | +0 (current) | 24/59 | entry[24] |
| **+0 (correct)** | +0 (current) | 49/59 | **entry[49]** |
| +2 (current) | **+4 (correct)** | 24/59 | entry[24] |
| **+0** | **+4** | **59/59** | none |

The `PVertexAnim` bug's first casualty in CASTLE is **mesh[49] itself** --
the Plant_Monster companion mesh. The `PSTAnim` bug fires earlier (entry 24)
and masks it.

UNDER.PRS has zero `PVertexAnim` records and 3 `PSTAnim` blocks, so UNDER is
**entirely** the `PSTAnim` bug (MEASURED).

## `PMeshNode` tag == 4: it is a `PAnim` block

`Load__9PMeshNodeFP5PFile...` @0x00176CE0, tag dispatch:

```
00176F34: lw   $v0, 332($sp)        ; tag just read
00176F38: bne  $v0, $v1, 0x00177048 ; v1 == 4 ; not 4 -> skip
00176F40: jal  DestroyAnim__9PMeshNodeFv
00176F48: jal  __nw__FUi            ; a0 = 32  (sizeof PAnim)
00176F58: jal  __ct__5PAnimFv
00176F84: jal  Load__5PAnimFP5PFile
0017701C: (read the NEXT 4-byte tag)
00177048: beq tag, 2 -> terminal (vtable+44 notify), return 1
00177168: beq tag, 1 -> allocate child PMeshNode(512), recurse
          anything else -> return 0 (whole file load fails)
```

So the node tag protocol is: **4 = a `PAnim` follows, then read another
tag; 1 = a child `PMeshNode` follows; 2 = end of children.** It is not a
camera or particle attachment. `tag == 4` is only accepted at the *first*
tag read of a node -- inside the child loop only 1 and 2 are accepted.

`Load__5PAnimFP5PFile` @0x001740F0 on-disk layout (MEASURED):

```
16-byte header
  u32 @+4  = channel count, must be < 5 (sltiu 5) else Load fails
  u16 @+12 = size in bytes of a raw block that follows the channels
  u16 @+14 = count of 32-byte records that follow that block
channel_count * PChannel
if u16@+12: that many raw bytes
if u16@+14: that many * 32 bytes
u32 trailing tag, must == 5
```

`LoadChannel__8PChannelFP5PFile` @0x001660C0 (MEASURED):

```
8-byte header
  u32 @+0 = anim type, must be 1..4
  u8  @+4 = eval type, must be 1..2
  u8  @+5, @+6, @+7 = per-sub-channel key counts
if eval type == 1:  read (b5) * 16 bytes
if eval type == 2:  read (b5 + b6 + b7) * 16 bytes   (as three chunks)
```

**Does `tag == 4` appear in the data?** MEASURED: **no.** Across all 11
`.PRS` files -- 484 mesh-table entries total -- the strict walk encountered
`tag == 4` **zero** times. It does not appear in `CASTLE.PRS` or
`UNDER.PRS` near the failing entries or anywhere else. It is dead weight in
these files; implementing it costs nothing and removes a `raise`, but it is
not part of any of the three enemies' failures.

## Mesh-table framing: strictly sequential, no offset table

This was the most valuable question, and the answer is the unhelpful one.

`Load__16PResourceManager...` @0x00168230 -- the `.PRS` container header:

```
read 28 (0x1C) bytes
  u32 @+0  magic, must == 0xC0DEFEED
  u32 @+4  version, must be >= 0x20003 (else printf + fail)
  u32 @+8  number of resource tables
then, that many times:
  u32 table type id:  1 -> LoadMeshResTable, 2 -> LoadAnimResTable,
                      3 -> LoadTextureResTable, then a u32 that must be 0
```

MEASURED on all 11 files: magic `0xC0DEFEED`, version `0x20003`, 4 tables,
table 0 type `1`. So `Reader(data, 0x20)` (0x1C header + the 4-byte type id)
is the correct mesh-table start, as the existing parser assumes.

Note the version handed to the mesh loader is **hardcoded**, not the file's:

```
00168560: lui  $v0, 0x0001
00168570: ori  $a3, $v0, 0x000b     ; ePsx2FileVersion = 0x1000B
00168574: jal  LoadMeshResTable
```

which is why `PMeshNode::Load`'s `slt $s1, 0x1000B` branch always takes the
112-byte transform, never the 80-byte one (`0x00176D2C`-`0x00176D6C`). The
parser's hardcoded 112 is correct for every file loaded through this path.
(INFERRED for other entry points -- `LoadMeshNodeHierarchy__6PWorldF...`
@0x001793B0 takes a version argument and was not traced.)

`LoadMeshResTable` @0x00167C30:

```
read u32 uNumMeshes -> allocate uNumMeshes * 4 POINTERS
for i in 0..uNumMeshes-1:
    read u32 tag
    tag == 1 -> new PMeshNode(512 bytes); PMeshNode::Load
    tag == 6 -> new PSkeleton(576 bytes); PSkeleton::Load
    else     -> CdsAssert, return 0
    on Load failure: printf(i), delete, return 0   (no recovery)
    store pointer at table[i]
```

Every read goes through `PFile::Read` (vtable +0x18) on a `PCDStFile`. There
is **no seek anywhere in the chain**, no per-entry size field, no per-entry
offset array, and no length prefix. The only thing allocated per index is a
pointer array filled in as the walk proceeds. Table position -> mesh index
is therefore recoverable **only** by walking every node correctly.
(MEASURED, and this is what kills the offset-table escape hatch.)

Also note: the engine does not tolerate a bad entry at all. It aborts the
whole resource load. Any resync the parser performs is, by definition,
already off the rails relative to the engine.

## Layouts confirmed unchanged (parser was already right)

- `PMeshNode`: 112-byte transform, 16-byte mesh header, `u32@+0` doubles as
  has-mesh flag and fragment count, then the tag loop. @0x00176CE0.
- `PSkeleton`: 16-byte header, opening tag must be 8, root `PBone`,
  `LoadSharedMesh` gated by header `u32@+0` **before** the closing tag,
  closing tag must be 7. @0x001690D0.
- `PBone`: 96 bytes, 16-byte mesh header (`u32@+0` = fragment count),
  optional `PMesh`, tag loop 8 = child follows / 9 = end. No `PAnim` case.
  @0x0016A830.
- `LoadSharedMesh`: 16-byte header; if `u32@+0` is zero it returns success
  having consumed only those 16 bytes. @0x00169C20.
- `PMesh`: no header of its own; `n` fragments where `n` came from the
  caller's mesh header. @0x00173A20.
- `PMeshFragment`: `PShader::Load` first, then 60-byte header --
  `flags u32@+0`, `face_buf_size u32@+4` (must be non-zero),
  `vanim_count u32@+8`, `poly_table_count u16@+12`; `poly_table_count`
  `PPolyTable`s; `PVertexAnim`s if `flags & 2` and `vanim_count`; then
  `face_buf_size` raw bytes. Worth knowing: if `flags & 1` is clear the
  engine **fails the load** (0x00173150-0x0017315C), so bit 0 is always set
  in valid data -- that makes it a strong desync detector, and it is exactly
  the check that caught the `PSTAnim` bug here. @0x00172F50.
- `PPolyTable`: 16-byte header, `n = u32@+4` (must be non-zero), `n * 32`
  bytes. @0x00167510.
- `PShader`: 16-byte header, `layer_count = u32@+0`, that many
  `PRenderPass`. @0x001960A0.
- `PRenderPass`: 40-byte header, texture cache index is `s32@+4`, `PSTAnim`
  gated by `u32@+20 & 1`. @0x00195390. The texture-id offset the porter
  depends on is confirmed by `0x0019543C`/`0x00195490`.

## The specific failures

Measured by instrumenting the *current* `prs_mesh_parser` (wrapping its four
`_resync_*` helpers) and comparing against the corrected strict walk.

### CASTLE.PRS mesh[49] and mesh[54]

Current parser: 14 resync events. First divergence is at `0xF7884`, inside
`mesh[24]...child0.child0` -- a `PRenderPass` at `0xF780C` has `flags20`
bit 0 set, so a `PSTAnim` starts at `0xF7834`; the parser read its channel
count from `+2` (value 0) instead of `+0`, consumed only the 16-byte header,
and landed 16 bytes short. From there the walk is offset and every
subsequent "success" is luck. It then hits `_resync_any_tag` at `0x262804`
inside `mesh[49]`, a `_resync_tag` at `0x262810`, and a **top-level**
`_resync_toplevel_tag` at `0x262840` for `mesh[50]` -- which is precisely
the top-level resync at entry [50] that makes table position stop mapping to
mesh index, so `mesh[54]` (Dark_Knight's companion `0x8C`) can no longer be
addressed. `mesh[50]` and `mesh[58]` end up with zero render passes. The
whole table is reported as consumed to `0x270304`; the true end is
`0x2B40C4`, i.e. the current parser loses ~277 KB of table.

Cause: `PSTAnim` count offset, then `PVertexAnim` count offset. With only
the `PSTAnim` fix the walk reaches entry **49** and fails there on the first
`PVertexAnim` in the file -- so `mesh[49]` (Plant_Monster companion `0x0B`)
is directly the `PVertexAnim` bug, and `mesh[54]` is collateral from the
resync that `mesh[49]`'s failure triggers. The reported "724 B slice, 680
consumed, 44 left over" for `mesh[49]` in isolation is consistent with a
mis-sized `PVertexAnim` record: recovered structures explain it.

**Both entries explained. Both fixed** -- with both offsets corrected,
CASTLE.PRS is 59/59 with zero resyncs.

### UNDER.PRS mesh[33]-mesh[38]

Current parser: 14 resync events, first divergence at `0x1C35C` in
`mesh[3]`. `mesh[33]` takes an inner `_resync_any_tag` at `0x1A9270` plus
two more, then top-level resyncs fire for `mesh[33]`, `[34]`, `[35]`,
`[36]`, `[37]`, `[38]` and `[39]`. Entries 34-39 all end with zero render
passes. Table reported consumed to `0x1B2350`; true end `0x2664DC`.

UNDER.PRS contains **zero** `PVertexAnim` records and **3** `PSTAnim`
blocks, so this range is entirely the `PSTAnim` count-offset bug. With that
one fix UNDER.PRS is 40/40 with zero resyncs, and mesh[38] (Goat_Devil
`0x13`) round-trips.

**Explained and fixed.**

### The "last entry sometimes needs a resync" note

Root-caused: it was not a distinct phenomenon. CASTLE `[58]` and UNDER
`[39]` are the tail of an already-desynced walk. With the offsets corrected
no last entry needs anything. (MEASURED.)

### Frozen_Zombie 0x10

Out of scope and nothing found that bears on it. It is an anim-resource-table
bone-name problem; the mesh table walk in ICE.PRS is clean (41/41, zero
resyncs, zero `PVertexAnim`s), which does at least rule the mesh table out
as a contributing cause.

## What an implementation task would need to change

In `randomizer/prs_mesh_parser.py`, three edits, all in loaders:

1. `load_st_anim`: channel count is `struct.unpack_from('<H', hdr, 0)`, not
   offset 2. The plausibility bound can stay.
2. `load_vertex_anim`: element count is `struct.unpack_from('<I', hdr, 4)`,
   not offset 0. Reject `hdr[0] == 0` and `count == 0` as the engine does.
3. `load_mesh_node`: `tag == 4` means load a `PAnim` (formula above) and
   then read the next tag, rather than raising. Optional -- it never fires
   in any shipped `.PRS` -- but it removes a known unknown.

Worth adding as a correctness gate rather than a heuristic: `PMeshFragment`
`flags & 1` must be set, `PPolyTable` count must be non-zero, `PAnim`
channel count must be < 5, `PChannel` eval type must be 1..2 and anim type
1..4. Each of those is a hard failure in the engine, so any of them tripping
means the walk is already desynced.

And the strong post-condition, which is what makes it safe to delete the
resync paths: after the mesh table, the next `u32` must be the next resource
table's type id (`2` for all 11 files). If it isn't, the walk is wrong and
the porter should refuse rather than resync -- because the engine would
refuse too.
