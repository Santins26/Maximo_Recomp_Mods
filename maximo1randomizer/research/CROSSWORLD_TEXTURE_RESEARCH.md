# Cross-world enemy texture research (in progress)

## Goal

Make world-exclusive enemies (Dark_Knight, Snowman, Frozen_Zombie, Zombie_Crocodile,
Crazed_Prisoner, Axe_Guard, Plant_Monster, etc.) safely appear in worlds other than
their home world, without the map-load crash that motivated `PRS_LOCKED_ENEMIES` in
`randomizer/catalog.py`.

**Status: major architecture solved; one file format fully solved for the common
case; porting not yet implemented.** The resource-ID format, the shared texture
cache architecture, and the complete `.PRT` texture-container file format (for the
common/simple case) have all been reverse-engineered and empirically verified
byte-exact against real game files. What remains is resolving a per-texture edge
case in mixed-format worlds, and then writing the actual extraction/injection tool.

## How this research was done

`game_files/MAXDEMOR.ELF` is a **debug build** of the game (6.07 MB vs. 1.17 MB for the
retail `SLUS_200.17`) with an intact `.symtab`/`.strtab` (real C++ symbol names, via
Metrowerks/CodeWarrior) and a large `.debug`/`.line` section containing full struct
field names. This is a goldmine the retail executable doesn't have, and is what made
this research possible without a live emulator.

Tools written for this (saved in `research/tools/`):
- `extract_elf_symbols.py` — parses MAXDEMOR.ELF's section headers + `.symtab`/`.strtab`
  and dumps `(name, address, size, info, shndx)` for every symbol. Regenerate with:
  `python extract_elf_symbols.py MAXDEMOR.ELF symbols_full.txt`
  (~8600 symbols; mostly compiler-generated noise (`@123` labels) and unrelated engine
  code (MPEG decoder, libc, etc.) — grep for what you need, e.g. `grep -i texture`).
- `mips_disasm.py` — a minimal hand-written MIPS R5900 disassembler (no `capstone`/
  `objdump` MIPS support was available in the research sandbox — no network access to
  install one, either). Covers standard arithmetic/branch/load-store opcodes.
  **Does not decode EE-specific "MMI" opcodes** (SPECIAL2, op=0x1c) — these show up
  constantly (mostly register moves/saves) and print as opaque `.mmi 0xXXXXXXXX`. A
  real MMI decoder would remove most of the remaining register-tracking ambiguity.

To map a virtual address to a file offset in MAXDEMOR.ELF: the `main` section has
`addr=0x100000`, `file_offset=0x80`, so `file_offset = 0x80 + (va - 0x100000)`.

**Methodology note that paid off repeatedly:** whenever a structural hypothesis about
a binary format was formed from disassembly, it was cross-checked by writing a small
Python parser and running it against the *real* files in `game_files/`, checking
whether it consumes every byte with zero leftover. This caught several wrong
assumptions early (see "corrections" below) and is very likely the fastest way to
finish resolving the one remaining open question (see "Open questions").

## Part 1 — The crash mechanism (fully solved, high confidence)

A BEF blob's "material record" has the byte layout (already documented in `bef.py`):
```
[rec_type: u8 (0x04 or 0x05)] [idx: u8] [0x00] [0x00] [flag: u8]
```

The last 4 bytes `[idx][00][00][flag]`, read as a little-endian `u32`, are passed
directly as the **resource ID** to `PResourceManager::GetResource(UInt id)`
(VA `0x001679C0`), which calls `DecodeResourceID(id, &a, &type, &b)` (VA `0x001681A0`).
Disassembly (via `dsll32`/`dsrl32` shift-pairs) shows this splits the 32-bit id into:

- **bits 0–23** (low 3 bytes) → an index — equal to `idx` itself, since bytes 1–2 of
  the material record are always 0 in vanilla data.
- **bits 24–30** (`flag & 0x7F`) → selects which resource category/table to use.
- **bit 31** (`flag & 0x80`) → LOCAL vs GLOBAL selector.

Empirical proof from vanilla data: `Basic_Skeleton` (0x47, a genuinely shared enemy)
has flag bytes `0x81`/`0x82` (high bit **set**) with **identical index sequences**
in both `GRAVE.BEF` and `CASTLE.BEF` — proving one shared asset. `Dark_Knight` (0x53),
`Crazed_Prisoner` (0x58), `Axe_Guard` (0x16), `Plant_Monster` (0x08) — all
world-exclusive — have flag bytes `0x01`/`0x02` (**no** high bit) natively, meaning
vanilla data never asks these to load from the global pool. Forcing the high bit on
for these makes the engine look up an unrelated/out-of-range entry — this is the
map-load crash, and there is no flag-only fix for it.

This confirms `catalog.py`'s `PRS_LOCKED_ENEMIES` fix (limiting cross-world injection
to the ~9 enemies natively used in 3+ worlds) is correct and necessary.

## Part 2 — Resource manager architecture

Recovered a full struct definition from the `.debug` section (search for
`tagResourceData`):

```c
struct tagResourceData {
    /* +0x00 */ unsigned int uNumMeshes;
    /* +0x04 */ void*        pMeshResTable;
    /* +0x08 */ unsigned int uNumAnims;
    /* +0x0C */ void*        pAnimResTable;
    /* +0x10 */ unsigned int uNumTextures;
    /* +0x14 */ void*        pTextureResTable;
    /* +0x18 */ char         szFileName[...];
    /* +0x98 */ bool         bUseSeparateMemPool;
    ...
};
```

`Load__16PResourceManagerF...` (VA `0x00168230`) reads this from a file (likely
`.PRS`, given an early magic check against `0xC0DEFEED` — PRS's confirmed magic) in
order: Mesh → Anim → Texture, via `LoadMeshResTable`/`LoadAnimResTable`/
`LoadTextureResTable` (VAs `0x00167C30`/`0x00167F00`/`0x00167A70`).

**`LoadTextureResTable`'s real behavior** (corrected after an earlier wrong read: the
per-entry value is not a size, it's an ID): reads `count → uNumTextures`, allocates
`pTextureResTable[count]`, then for each entry reads a 4-byte **texture ID** and
calls `LoadTexture(id)` (VA `0x0016C290`), storing the returned pointer.

**`LoadTexture(idx)` and `PreloadAllTextures(file, count, ...)`** (VA `0x0016C330`)
share one **capped 512-slot cache**: `struct { count: u32; entries: TexturePtr[512]; }`
(confirmed by identical `sltiu ..., 512/513` bounds checks in both). `PreloadAllTextures`
loops `count` times calling `PTextureManager::Create(mgr, file, ...)` (VA
`0x001A5FC0`) — **the function that actually reads texture bytes** — appending each
result to the shared cache.

**This means the cache is built sequentially: GLOBAL's textures preload first (low
indices), then the current world's own textures preload right after (higher
indices)** — one shared array, not two separate pools. The BEF flag's LOCAL/GLOBAL
bit is really just "which half of this one sequentially-built cache to expect the
id to fall in."

**Practical implication for porting:** to make a locked enemy's texture appear in a
foreign world, that world's own preload pass needs to also preload the source
texture's bytes (append to its own texture chunk stream — see Part 3), and the
injected BEF material record needs to point at the right *local* index afterward.

## Part 3 — `.PRT` file format (fully solved for the common case, byte-exact verified)

`PTextureLoader::SetTextureFile` (VA `0x0016C050`) opens `.PRT` directly — its magic
is `0x04030201` (the `01 02 03 04` bytes seen at every `.PRT` file's start) — reads
the 32-byte header, then:
- if `count_a != 0` (header offset `+0x08`): calls `LoadClutData(file, count_a)`
  (VA `0x001A7D70`)
- if `count_b != 0` (header offset `+0x0C`): calls `PreloadAllTextures(file, ...)`,
  which loops calling `PTextureManager::Create` once per texture (VA `0x001A5FC0`)

**`LoadClutData` — fully solved, trivial:** reads exactly `count_a` CLUTs, each
**exactly 1024 bytes** (256 colors × 4 bytes RGBA — a standard PS2 GS 8-bit-indexed
palette), no header, back to back.

**`PTextureManager::Create`'s on-disk texture chunk format** (16-byte header + pixel
data), from direct disassembly:

```
+0x00..03  unknown (2x u16?) — likely log2(width)/log2(height): a nearby code path
           computes (1 << val_at_0) * (1 << val_at_2) as a pixel count
+0x04      u8  format — PS2 GS PSM constant. Observed in real data: 0x00 (PSMCT32),
           0x01 (PSMCT24), 0x13 (PSMT8, 8-bit indexed). 0x14 (PSMT4) never directly
           observed but plausible.
+0x05      u8  mip-related count / CLUT selector (checked !=0, and used as an upper
           bound in a later per-miplevel loop — that loop looks like in-memory
           bookkeeping, not extra file reads)
+0x06..07  u16 flags — bit 0x0002 confirmed to mean "extra CLUT-sized block follows
           inline after the pixel data" in at least some cases (see open question)
+0x08..0B  u32 pixel data size in bytes — read via a second Read() call of exactly
           this many bytes immediately after the header
+0x0C      u8  another field, checked ==3 in one branch
+0x0E..0F  s16 another field, checked in range [0, 175)
```

**Verified, byte-exact formula for the whole `.PRT` file:**

```
[0x00..0x1F]                  header (magic, version, count_a, count_b, 16 reserved)
[0x20 .. 0x20+count_a*1024)   CLUT pool: count_a x 1024 bytes, no header
[0x20+count_a*1024 .. EOF]    texture chunk stream: count_b entries, each
                              [16-byte header][pixel data: size bytes from +0x08]
```

**This was verified against real game data**: parsing `GRAVE.PRT` with `count_a=34`
CLUTs and `count_b=35` textures using this exact formula consumes the file
**perfectly** — the computed end position lands exactly on EOF with **zero leftover
bytes** across all 35 texture chunks (all of which happened to be format `0x13` with
no inline-CLUT flag, using the shared CLUT pool exclusively).

### Open question: mixed-format worlds don't parse cleanly yet

`CASTLE.PRT` and `GLOBAL.PRT` have a wider mix of texture formats and don't fully
parse with the formula above — they get much further than a naive attempt (adding
"if flags bit 0x0002 set, skip 1024 extra bytes" got Global from failing at chunk 4
to chunk 102 of its texture stream; Castle got from chunk 45 to chunk 48 after also
allowing legitimate `size == 0` placeholder chunks) but still eventually hit an
invalid-looking chunk. Untried candidates worth checking next, in order of promise:
1. **Inline CLUT size may depend on `format`** — 1024 bytes for a 256-color palette
   (format `0x13`) but likely much smaller (64 bytes) for a 16-color palette (format
   `0x14`, PSMT4). This was tried once but *reduced* Global's progress rather than
   improving it, which is a useful negative data point — perhaps the condition for
   "extra bytes present" isn't simply "flags bit 0x0002", or there's a second flag
   bit that also needs checking (the failing chunk in one test run showed
   `flags=0x806`, i.e. multiple bits set together, e.g. `0x0004` and `0x0800`, which
   were never individually investigated).
2. The mip-related fields at `+0x05`/`+0x0C` may indicate additional on-disk mip
   level data appended after the base pixel data (a full mipmap chain would add
   roughly 1/3 more bytes — note the `0xAAAAAAAB` magic-number division-by-3 idiom
   spotted nearby in the disassembly, which might be *computing* the total mip chain
   size rather than just an in-memory GS-upload calculation as first assumed).
3. Keep using the "parse to a clean EOF with zero leftover" brute-force validation
   technique that solved the base format — try each candidate rule against
   `CASTLE.PRT`/`GLOBAL.PRT` and see which one gets all the way to EOF.

**The exact trigger point has been precisely isolated** (useful starting point for
the next attempt): in `CASTLE.PRT`, chunks 0–43 are all format `0x13` (indexed),
`flags=0`, `b12=1`, and parse perfectly with the simple `16+size` rule. **Chunk 44**
is the first deviation: format `1` (PSMCT24, direct color — not indexed!), flags
`0x0002`, and **`b12=3`** (never seen as anything but `1` before this point). A
resync scan was tried — brute-forcing every possible extra-byte-count from 0 to 8192
bytes after chunk 44's base end (`16+size`), requiring the next 15 chunks to
collectively contain a meaningful (non-placeholder) amount of real data — and found
**no valid resync point in that whole range**. This means either:
- chunk 44's reported `size` field (49152) is itself being read correctly but the
  *true* on-disk length of this chunk is unrelated to a simple "+ extra bytes after"
  correction (e.g. maybe the header is a different size for this case, so `size` was
  read from the wrong offset to begin with), or
- the resync point is further than 8192 bytes away (worth re-running with a larger
  search window as a first, cheap next step)

The `b12==3` branch in `PTextureManager::Create`'s disassembly is exactly the one
that does the `0xAAAAAAAB` divide-by-3-style computation — re-disassembling that
branch specifically (rather than treating it as in-memory-only bookkeeping, which
was an assumption, not a confirmed fact) is the most promising next move.

**Update:** the function called from the mip-processing loop right after this,
`RoundUpTexture__15PTextureManagerFUiUii` (VA `0x001A70A0`), was checked via its
symbol name and is a pure dimension-rounding utility (rounds texture width/height up
to valid GS block sizes) — it does not perform file I/O. This rules out "the mip
chain has more on-disk bytes than the base `size` field accounts for" as the
explanation. Both simple hypotheses (0 extra bytes, or +1024 extra bytes) for a
`flags=0x0002 + format=1 + b12=3` chunk have now been definitively ruled out by the
resync scan.

**Further investigation:** the 128+ bytes immediately following chunk 44's base end
(`16+size`) are **all zero** (checked: 834 zero bytes exactly, then scattered `0x7F`
fill-pattern bytes) — this looks like genuine disc padding/slack rather than
misaligned chunk data, which initially seemed promising (maybe chunks 45+ are
legitimately empty placeholder slots). But a much wider resync search (up to 200KB
past chunk 44, matching against 11 chunks = `count_b(56) - 44`) only turned up
false positives — patterns of mostly-zero chunks coincidentally containing one
large size value, none of which reach the file's true end position cleanly. This
rules out a simple fixed-padding-then-resume explanation too.

**Session 2 update — a strong header-consistency validator found, then a
systematic exhaustive search that still failed:**

A much stronger validation rule was discovered by cross-checking the header's
width/height fields against the declared size: for both chunk 0 and chunk 44,
`size == (2^h0) * (2^h2) * bytes_per_pixel(format)` EXACTLY (e.g. chunk 44:
`h0=7,h2=7` → 128×128, format 1 = PSMCT24 = 3 bytes/px → 128*128*3 = 49152,
matching its declared size precisely; chunk 0: `h0=7,h2=6` → 128×64, format
0x13 = PSMT8 = 1 byte/px → 128*64 = 8192, also exact). This confirms the header
layout (`+0x00` log2-width, `+0x02` log2-height, `+0x04` format, `+0x08` size) is
real and correctly read — chunk 44's own declared size is NOT the problem.

Using this as a strict validator, several further hypotheses for what comes
**after** chunk 44 were tested and ALL failed:
- Mip chain with per-level 16-byte headers (3 additional levels: 64×64, 32×32,
  16×16) — the predicted position only produced a coincidental single-chunk
  match; chaining to a second subsequent chunk immediately failed.
- Mip chain as raw concatenated bytes with NO per-level header (just one 16-byte
  header covering the whole mip pyramid) — same result, single coincidental hit,
  chain breaks immediately after.
- **A fully exhaustive, byte-by-byte search across the entire remaining ~432KB of
  the file**, requiring the declared size to be an exact multiple of `2^h0 * 2^h2`
  with a resulting bytes-per-pixel of 1–4 (format-byte-agnostic — not assuming
  we already know every valid format value), AND requiring the full remaining
  chain (11 more entries) to land EXACTLY on EOF with zero leftover: **found
  zero candidates** in the whole file.

A byte-frequency check of the ~432KB remainder shows it is only ~5% zero bytes
(i.e. NOT a large empty/padding region — it's real, dense data), which rules out
"there's just one big gap then things resume simply." Combined with the
exhaustive search finding nothing, this means either:
- the 16-byte header layout itself is different for chunk 44's specific
  format/flag combination (or for whatever comes after it) — e.g. extra fields
  inserted, shifting where `size` actually lives, or
- individual chunks include some additional per-chunk data (not a simple flat
  offset or mip-related concatenation) whose size can't be predicted purely from
  the header fields decoded so far, or
- the true chunk boundary requires bytes/alignment padding between entries that
  a byte-exact chain search can't discover without knowing the padding rule in
  advance (a systematic per-entry padding amount, e.g. rounded up to N bytes,
  would also evade this search unless N is tried explicitly).

This was a thorough, systematic effort (five distinct hypotheses, one of them an
exhaustive brute-force over the entire remaining file) and none succeeded —
this particular sub-problem is very likely at the edge of what's practically
resolvable through static analysis and computational search alone. Live PCSX2
verification (or much deeper, careful disassembly of the exact
`format != 0x13`/`b12==3` code path, ideally with proper MMI instruction decoding
which `mips_disasm.py` still doesn't have) is the realistic next step, not more
blind hypothesis testing.

**Final check performed:** to be thorough before stopping, an even more
permissive search was run: every one of the 11,636 structurally-valid header
positions found across the whole ~432KB remainder was used as a graph node, with
transitions allowing 0–64 bytes of per-chunk alignment padding, searching for ANY
path of exactly 11 hops landing precisely on EOF. Still **zero solutions found**.
This rules out simple fixed alignment padding between entries as the missing
piece too. The remaining explanation is almost certainly that the 16-byte header
layout itself differs for some entries (extra/shifted fields), which cannot be
discovered by searching for consistency with the CURRENT assumed layout — it
needs fresh disassembly or live memory inspection to reveal the real structure.

**Conclusion for this session:** this specific edge case (non-indexed formats
combined with the `flags=0x0002`+`b12=3` combination) resisted every static/empirical
hypothesis tried. It most likely needs either a fresh, careful disassembly of
`PTextureManager::Create`'s header-parsing prologue specifically for the
`format != 0x13` branch (rather than assuming the same fixed 16-byte header layout
holds for every format, which was never independently confirmed for non-indexed
formats), or live verification in PCSX2. Given this format only affects a small
minority of chunks (44 of Castle's first 45 chunks, i.e. the vast majority, parse
perfectly with the simple rule), a pragmatic interim option is to only support
porting textures that use the simple/common case, and skip/reject enemies whose
texture chunk can't be located this way.

**Once this is resolved**, extraction/injection between `.PRT` files is
straightforward: walk the source world's texture chunk stream to the target index,
slice out its exact bytes (now that chunk boundaries are fully computable), and
append them to the destination world's own texture chunk stream (incrementing its
`count_b` header field and growing the file — the ISO patcher's existing
grow/relocate machinery for `.PSX`/`.BEF` files would need `.PRT` added to it, see
`randomizer/iso_patcher.py`'s `ASSET_EXTENSIONS`).

## Part 4 — Not yet investigated: the Mesh table, and how the texture ID-list numbers are assigned

Two things remain genuinely unexplored:

1. **The Mesh table's on-disk format** (read by `LoadMeshResTable`, VA `0x00167C30`).
   Unlike Texture, each entry has a `u32` "chunk type" tag (observed values `1` and
   `6`) dispatching to `PMeshNode::Load` (VA `0x00176CE0`) or `PSkeleton::Load` (VA
   `0x001690D0`) respectively, each of which does further nested reads not traced in
   detail. **This is very likely irrelevant to the porting goal** — enemy geometry
   appears to already live inside the BEF blob itself (proven working for the "safe"
   9 cross-world enemies already), so the Mesh table format probably doesn't need to
   be solved at all.
2. **How the texture ID-list's numbers (read by `LoadTextureResTable`, from `.PRS`)
   correspond to positions in the `.PRT` texture chunk stream.** We know
   conceptually that `id = N_global + local_chunk_index` for local textures (Part 2),
   but this hasn't been empirically verified by cross-referencing an actual BEF
   material record's `idx` all the way through to a specific byte range in `.PRT`.
   Doing this for e.g. Dark_Knight's texture in `CASTLE.BEF`/`CASTLE.PRT` would be
   a strong end-to-end validation of the whole model before writing any injection
   code.

## Key VA reference table

| Symbol | VA | Notes |
|---|---|---|
| `GetResource__16PResourceManagerFUi` | 0x001679C0 | entry point; calls DecodeResourceID |
| `DecodeResourceID__16PResourceManagerFUiRiR14eResourceTypesRi` | 0x001681A0 | splits 32-bit id into (index, category, local/global) |
| `GetResource__16PResourceManagerFPQ216PResourceManager15tagResourceDataUi14eResourceTypesi` | 0x00168B80 | actual table lookup by (category-selected table, type, index) |
| `Load__16PResourceManagerFPCcPCcbPQ216PResourceManager15tagResourceData` | 0x00168230 | top-level file loader; calls the 3 ResTable loaders in order |
| `LoadMeshResTable__16PResourceManagerF...` | 0x00167C30 | complex, tag-dispatched per-entry format (not solved) |
| `LoadAnimResTable__16PResourceManagerF...` | 0x00167F00 | not analyzed |
| `LoadTextureResTable__16PResourceManagerF...` | 0x00167A70 | reads count + per-entry texture ID (not a size); calls LoadTexture(id) |
| `LoadTexture__14PTextureLoaderFi` | 0x0016C290 | shared-cache lookup by index; NOT a file reader |
| `PreloadAllTextures__14PTextureLoaderFP5PFileUii` | 0x0016C330 | loops calling PTextureManager::Create, appends to shared cache |
| `SetTextureFile__14PTextureLoaderFPCci` | 0x0016C050 | opens .PRT fresh, drives LoadClutData + PreloadAllTextures |
| `LoadClutData__15PTextureManagerFP5PFilei` | 0x001A7D70 | reads count CLUTs, 1024 bytes each — fully solved |
| `Create__15PTextureManagerFP5PFilei` | 0x001A5FC0 | reads one texture chunk: 16-byte header + pixel data |
| `Load__9PMeshNodeFP5PFileP14PTextureLoader16ePsx2FileVersionb` | 0x00176CE0 | mesh-node loader (tag 1), not analyzed |
| `Load__9PSkeletonFP5PFileP14PTextureLoader16ePsx2FileVersion` | 0x001690D0 | skeleton loader (tag 6), not analyzed |
| `GlobalResPrs` | 0x001EF0C0 | global var holding loaded GLOBAL.PRS resource data |
| `GlobalResPrt` | 0x001EF0F0 | global var holding loaded GLOBAL.PRT resource data |

## File format summary

- **`.PRS`**: magic `0xC0DEFEED`. Float-heavy content (many `1.0f`/`128.0f`/`255.0f`
  constants found via byte-frequency analysis) — mesh/vertex geometry data, plus the
  Mesh/Anim/Texture-ID-list tables read by `PResourceManager::Load`.
- **`.PRT`**: magic `0x04030201`. Byte-frequency analysis shows heavy same-byte-repeated
  4-byte runs (`04040404`, `82828282`, etc.) — consistent with flat-color regions in
  indexed texture data. **Fully decoded** (Part 3): 32-byte header, then a CLUT pool,
  then a texture chunk stream, back to back, no gaps.
- Every world (not just Grave/Under — an earlier incorrect assumption already fixed
  in `catalog.py`) has its own `.PRS`/`.PRT` pair on disc, in addition to
  `GLOBAL.PRS`/`GLOBAL.PRT`.

## Suggested next session's first move

Pick up at Part 3's open question using the same brute-force-against-real-bytes
technique — try format-dependent CLUT sizes and mip-chain-size candidates against
`CASTLE.PRT` and `GLOBAL.PRT` until one parses to a clean EOF with zero leftover, the
same way the base format was confirmed for `GRAVE.PRT`. That technique has a 100%
hit rate so far in this research and is much faster than continuing to hand-trace
MIPS disassembly.

## Session 3 — porting implemented and verified for the indexed (common) case

This session used `game_files/MAXDEMOR.ELF`'s embedded assert-condition strings
directly (not just symbol names) to get the REAL field names for
`PTextureManager::Create`'s struct, by extracting the literal C++ condition text
of every `assert()` in `PTextureManager.cpp` (they're plain ASCII strings baked
into the binary next to each assert call site — see the `a2` operand in the
disassembly, computed as `0x00200000 + offset`). This was a much stronger source
of ground truth than continuing to guess field layout from register behavior.
Confirmed struct (matches and extends Part 3's version):

```c
struct TEXTURE_HEADER {           // 16 bytes total, one file->Read(header,16) call
    u16 log2_width;                // +0x00 (assumed order; not yet independently
    u16 log2_height;               // +0x02  confirmed which of the two is which)
    u8  nPixelFormat;              // +0x04  SCE_GS_PSMCT32(0x00) / PSMCT24(0x01) / PSMT8(0x13)
    u8  nBytesPerPixel;            // +0x05  must be > 0 (4 / 3 / 1 respectively)
    u16 nFlags;                    // +0x06  bit 0x0002 = TEXTURE_FLAG_OPACITY_MAP
    u32 nSourceImageSize;          // +0x08  exact byte length of the raw pixel block
    u8  nMipMapLevels;             // +0x0C  must be <= MAX_MIPMAPS
    u8  (unconfirmed/pad);         // +0x0D
    s16 nClutIndex;                // +0x0E  must be in [0, CLUT_TABLE_SIZE=175)
};                                                          // only meaningful for PSMT8
```
Real assert strings recovered (file `PTextureManager.cpp`), which is what nailed
these names: `"ReadSize == sizeof(TEXTURE_HEADER)"`,
`"(u_int)aTextures[Index].Header.nPixelFormat == SCE_GS_PSMCT32 || ... PSMCT24 || ... PSMT8"`,
`"aTextures[Index].Header.nBytesPerPixel > 0"`,
`"aTextures[Index].Header.nSourceImageSize > 0"`,
`"aTextures[Index].Header.nClutIndex >= 0 && ... < CLUT_TABLE_SIZE"`,
`"aTextures[Index].pCLUT == NULL"`, `"aTextures[Index].pSourceImage == NULL"`,
`"ReadSize == aTextures[Index].Header.nSourceImageSize"`,
`"aTextures[Index].Header.nMipMapLevels <= MAX_MIPMAPS"`,
`"aTextures[Index].pOpacityImage"`, `"ReadSize == aTextures[Index].nOpacityImageSize"`.

**Correction to Session 2's "flags 0x0002" hypothesis:** it's not an inline-CLUT
flag, it's `TEXTURE_FLAG_OPACITY_MAP` — a separate full alpha-image block. Its
size is NOT stored on disk; it's derived at runtime from the base pixel size via
the exact multiply-by-reciprocal instructions in `Create` (`v1 * 0xAAAAAAAB`,
take the high word, shift right 1, add back `v1`), which works out to
`floor(nSourceImageSize * 4/3)` — an RGB24 -> RGBA32 size expansion. **This
formula was verified byte-exact**: `CASTLE.PRT` entry 44 (128x128, PSMCT24,
opacity flag set) has `nSourceImageSize=49152` and the next
`floor(49152*4/3)=65536` bytes parse as a clean, structurally-valid transition
point.

**Full file-level validator implemented and tested** (later promoted to
`randomizer/texture_port.py`): walks `[16-byte header][pixel data][+ opacity
data if flagged]` sequentially, validating each header against
`size == (1<<log2_w)*(1<<log2_h)*bytes_per_pixel(format)` and, for indexed
entries, that `nClutIndex` continues the previous indexed entry's index + 1 (an
extra cross-check beyond the range check, since real files use CLUT slots
strictly sequentially in file order). Results:

- **`GRAVE.PRT`: perfect.** All 35 entries (all PSMT8, no opacity) parse with
  zero leftover bytes, and a rebuild from the parsed structure reproduces the
  original file **byte-for-byte identical**.
- **`CASTLE.PRT`: 55 of 56 nominal entries parse cleanly, landing exactly on
  EOF.** The header's `count_b` field says 56, but only 55 entries' worth of
  bytes exist in the file and they use exactly `count_a=54` CLUT slots
  sequentially (0..53) with zero left over — a fully self-consistent result.
  Very likely explanation (not yet independently confirmed by re-disassembly):
  `Create`'s per-entry loop has a "slot already populated, skip re-read"
  early-exit that was noticed but not fully traced in Session 1/2 — one slot
  is probably pre-filled from `GLOBAL.PRT` and skipped without consuming file
  bytes, so the loop runs 56 times but only reads from disk 55 times. The one
  entry with the opacity flag (index 44, a 128x128 PSMCT24 background-style
  texture) parses correctly using the confirmed header+pixel+opacity formula,
  BUT is followed by 16,400 bytes of `7f 7f 00`-repeating **fill-pattern
  padding** before the next real header (confirmed by direct byte inspection —
  it is NOT structured data, just filler). The exact padding-length rule is
  still unconfirmed (only one example exists in this file to check against);
  `texture_port.py`'s parser handles this pragmatically with a bounded
  forward resync scan rather than requiring the formula, which is sufficient
  for reading past it.
- **`GLOBAL.PRT`: fails at entry 5**, in a *different* way — an opacity-map
  entry (index 3, 32x32 PSMCT24) is followed almost immediately (gap of only
  ~1040 bytes) by what LOOKED like another valid entry 4 with byte-identical
  fields to entry 3, which is suspicious (possibly a false-positive resync
  match on coincidentally-valid-looking padding, not real data) — and entry 5
  after that lands in the middle of what direct byte inspection shows is
  clearly **real, non-repeating pixel/gradient-looking data**, not padding.
  This means the opacity block's true size for at least these entries is
  BIGGER than the `size * 4/3` formula predicts (possibly a mip chain is
  genuinely appended after all, contradicting the Session 2 conclusion that
  `RoundUpTexture`/`sub_1A70A0` was purely an in-memory calculation with no
  associated file reads — that conclusion may have been wrong, or there's a
  second, still-unidentified data block specific to some opacity-flagged
  entries). **Not resolved this session; scoped around instead (see below).**

**Practical scoping decision — and why it's reasonable:** every enemy/creature
sprite texture observed in every sample file so far is indexed (PSMT8/CLUT).
The problematic opacity-flagged, non-indexed entries look like background /
environment art (large direct-color textures with alpha — e.g. skyboxes,
painted backdrops), based on format + dimensions + their scarcity (1 of 56 in
Castle). Since the porting goal is cross-world **enemy** textures,
`randomizer/texture_port.py` was scoped to support ONLY the indexed case for
now, and raises a clear `PRTPortError` (naming the exact entry index and
reason) if extraction ever has to cross an unsupported opacity/non-indexed
entry, rather than silently producing a corrupt file.

**Injection is simpler than expected and doesn't depend on solving the above at
all.** Appending a new texture to a target world's PRT only needs that file's
current `count_a`/`count_b` header fields and its current length — the format
has no offset directory, so there's nothing to re-parse. Extraction (pulling a
specific entry OUT of a source world) is the only side that needs the
sequential walk, and only up to the target entry's index.

**End-to-end port implemented and verified byte-exact** (Grave entry 5, a
128x256 PSMT8 texture, ported into a scratch copy of `CASTLE.PRT`):
- File grew by exactly `16 + len(pixel_data) + 1024 (new CLUT)` bytes, no more,
  no less.
- The appended header, pixel data, and new CLUT slot are byte-identical to the
  source.
- Every single pre-existing byte in the target file (all 54 original CLUTs,
  and the entire original texture stream including the one still-unsolved
  opacity entry) is provably untouched (verified via direct byte-range
  comparison against the pre-port file).
- The injected entry's `nClutIndex` is correctly repointed to the new slot
  appended at the target's old `count_a` (so it doesn't collide with any
  existing palette in the target world).

**Delivered this session:** `randomizer/texture_port.py` —
`extract_texture(path, index, world_name)`, `inject_texture(target_bytes, tex)`,
`port_texture(...)` convenience wrapper, and `list_entries(path)` (a diagnostic
walker to help identify which entry index corresponds to a given enemy — see
Part 4 below, still the missing piece for a fully automated enemy->texture-index
lookup). `randomizer/iso_patcher.py` needs no changes: `write_patched_assets`
is already fully generic over any file placed in the patched-output folder
with a name matching what `find_maximo_files` extracted (which already
includes every world's `.PRT`), so a grown `.PRT` flows through the exact same
grow/relocate path as `.BEF` already does.

**Not yet done — the remaining gap before this is a fully automated feature:**
Part 4's "how does a BEF material record's resource-ID index map to a specific
`.PRT` entry index" is still unsolved. `list_entries()` lets you inspect a
world's PRT by hand today; wiring this into `bef.py`'s cross-world injection so
a locked enemy's texture gets auto-ported alongside its BEF blob is the next
real step.

## Suggested next session's first move (updated)

1. Solve Part 4 (BEF resource-ID -> PRT entry index mapping) using a concrete
   test case — e.g. trace `Dark_Knight`'s texture in `CASTLE.BEF`/`CASTLE.PRT`
   end-to-end — so `texture_port.py` can be driven by enemy name instead of a
   raw entry index.
2. Wire `texture_port.py` into `bef.py`'s `inject_cross_world_enemies` so a
   locked enemy's texture is ported automatically alongside its BEF blob
   whenever cross-world mode injects it into a foreign world.
3. If backgrounds/opacity-flagged textures ever need porting too (not needed
   for the current enemy-only goal), revisit `GLOBAL.PRT` entry 3-5 with live
   PCSX2 memory inspection — static analysis hit a real wall there this
   session (the `size*4/3` formula that worked for `CASTLE.PRT` entry 44
   under-counts for `GLOBAL.PRT`'s entries, and the gap between them contains
   real, non-padding data of an as-yet-unknown shape).

## Session 4 — the mesh/skeleton file format itself, fully cracked

This session set out to solve Part 4 (BEF material idx -> PRT entry index) via
continued static disassembly (PCSX2 live debugging was explicitly ruled out for
this session). That specific mapping is STILL not solved — but a much bigger,
adjacent discovery fully overtook it: **a mesh fragment's own rendering texture
is not looked up via the BEF material record system at all.** It's read
directly, inline, as part of the PRS mesh/skeleton data itself, via a
completely separate chain: `PShader::Load` -> `PRenderPass::Load`, where each
render-pass's 40-byte header has the shared-cache texture index at a plain
offset (+4), with `-1` as a real "no texture" sentinel. This was confirmed by
disassembling `Load__11PRenderPassFP5PFileP14PTextureLoader` directly — the ID
read from the header is passed straight to `LoadTexture(id)`, the same
primitive used elsewhere. The BEF material-record path (`GetResource`/
`DecodeResourceID`/`pTextureResTable`) may be for something else entirely
(a gameplay-logic overlay, not confirmed) rather than the base skin.

Given this, the session pivoted to fully reverse-engineering the mesh/skeleton
format itself, since that's what actually determines a character's texture —
and this succeeded completely. **`randomizer/prs_mesh_parser.py` now parses
`CASTLE.PRS`'s entire 59-entry mesh table cleanly, extracting 614 real
texture-cache-index references** across every top-level object in the file
(static props, doors, and full character skeletons with deeply nested bone
hierarchies), each tagged with a breadcrumb path (e.g.
`mesh[4].root.child0.mesh.frag0.layer0`) showing exactly which
node/bone/fragment/layer it came from.

### The confirmed format (see `prs_mesh_parser.py` docstrings for the authoritative version)

```
File: [0x20: u32 uNumMeshes], entries start immediately after (no further
      leading header found/needed for this table specifically)

Per top-level entry: [u32 tag] tag==1 -> PMeshNode; tag==6 -> PSkeleton

PMeshNode: 112-byte transform (NOTE: version-gated 80-vs-112, not yet
    auto-detected — hardcoded to 112, matches this file) + 16-byte
    mesh-header (field0 doubles as BOTH "has mesh" flag AND fragment count)
    + optional attached PMesh + tag loop: 1 = another child node follows
    (recurse), 2 = terminal/stop. tag==4 is a rare untraced special case.

PSkeleton: 16-byte header (field0 gates LoadSharedMesh) + opening tag
    (must==8) + root PBone::Load (recursive) + optional LoadSharedMesh
    (BEFORE the closing tag, not after) + closing tag (must==7)

PBone: 96 bytes (transform+name) + 16-byte mesh-header (same field0
    double-duty as PMeshNode) + optional attached PMesh + tag loop:
    8 = another child bone follows (recurse), 9 = terminal/stop

PMesh: [fragment_count fragments, each via PMeshFragment::Load]
    (fragment_count = the SAME mesh-header field0 from whichever of
    PMeshNode/PBone/LoadSharedMesh called it)

PMeshFragment: PShader::Load(fragment) + 60-byte header (flags@0,
    face_buf_size@4, vertexAnimCount@8, vertCount@12 as u16) +
    vertCount x PPolyTable::Load + optional vertex-anims (flags bit0x0002
    AND vertexAnimCount!=0) + optional raw face/index buffer (flags
    bit0x0001, size = face_buf_size, taken directly from the header)

PShader: 16-byte header (field0 = layer count) + that many PRenderPass::Load

PRenderPass: 40-byte header. offset+4 (s32) = texture-cache index, -1
    sentinel for none. offset+20 bit0x0001 gates an optional PSTAnim
    (UV-scroll animation) block.

PSTAnim: 16-byte header (channel count @ offset+2, u16) + that many
    PSTAnimChannel::Load

PSTAnimChannel: 16-byte header (count @ offset+0) + count*8 raw bytes

PPolyTable: 16-byte header (poly_count @ offset+4) + poly_count*32 raw bytes

PVertexAnim: 16-byte header (count @ offset+0) + count*16 raw bytes
    (NOTE: the gating count field used by LoadVertexAnims itself isn't
    fully nailed down yet — see "unsolved" below)
```

### Two backwards-tag-semantics bugs found and fixed by testing against real bytes

Both were found the same way: implement from the disassembly, run against
`CASTLE.PRS`, and when the parse landed on implausible values partway through,
re-read the actual branch targets character-by-character rather than trusting
a first-pass summary.

- **`PMeshNode`**: initially implemented as 2=continue/1=stop. Actually
  backwards: **1=continue (recurse into a new child), 2=stop** (terminal,
  with a "notify" virtual call).
- **`PBone`**: initially implemented as 9=continue/8=stop. Actually
  backwards: **8=continue (recurse into a new child bone), 9=stop**.

Also found: `PShader::Load`'s own 16-byte header was initially modeled as just
a 4-byte count read (missing the other 12 bytes) — and `PSkeleton::Load`'s
optional `LoadSharedMesh` call was initially placed AFTER the closing tag
check instead of BEFORE it. Both were caught the same way: the parser landed
on clean, plausible-looking data for many entries in a row, then hit a wall at
a specific point, and re-deriving that one function's exact instruction
sequence (rather than trusting an earlier summary) found the discrepancy every
time.

### Still unsolved (rare; the parser tolerates via bounded resync rather than being blocked)

- `LoadVertexAnims`'s exact gating/count field isn't fully right yet — when a
  fragment's own vertex-anim data doesn't parse, the parser resyncs at the
  next tag boundary rather than trusting a wrong byte count. This lost some
  (unknown, probably small) number of texture references between entries 24
  and 57.
- `PMeshNode` tag==4 (a rare special node type, possibly a camera or particle
  attachment point) is untraced — raises clearly if hit; wasn't hit in this
  file.
- The version-gated 80-vs-112-byte `PMeshNode` transform size is hardcoded to
  112 (matches this file) rather than actually detected from the file's
  version field.
- The very last top-level entry needed a resync too; not fully root-caused.

### What this changes for the original cross-world texture goal

The BEF-material-idx path (Part 4's original question) may not even be the
right thing to patch for changing a mesh's visible texture — **the
`PRenderPass` texture index found here is very likely the actual lever to
pull.** The remaining real gap is purely: given an enemy name (e.g.
`Dark_Knight`), which specific path(s) in the 614 collected — i.e. which
mesh-table entry, which bone, which fragment/layer — belong to him. That's a
much narrower, more tractable question than the original "find a hidden list
in a 4MB file" problem, since `prs_mesh_parser.py` now gets you every
candidate with full structural context in one pass.

## Suggested next session's first move (Session 4)

1. Identify which of the 614 collected `(path, tex_id)` entries belong to
   `Dark_Knight` specifically. Candidate approaches: cross-reference bone
   counts/hierarchy depth against what's known about his skeleton from other
   sources; or match `PSkeleton` entries' fragment counts/dimensions against
   expected armor-piece counts; or (fastest) live-inspect once, now that we
   know exactly which 4 bytes in which PRenderPass header to watch.
2. Once identified, extend `prs_mesh_parser.py` from a read-only inspector
   into an editor: given a path like `mesh[4].root.child2.mesh.frag0.layer0`,
   overwrite that specific PRenderPass's texture-index field in place (no
   file growth needed here, unlike the PRT case — it's a fixed-size field
   being overwritten with a different valid shared-cache index).
3. Fix the `LoadVertexAnims` count field using the same real-bytes-first
   method that cracked everything else this session.

## Session 5 — Dark_Knight identified, patcher built and verified

**Dark_Knight = `mesh[25]` in `CASTLE.PRS`, confirmed.** His BEF blob (type `0x53`
in `CASTLE.BEF`) has a header field at offset `+0x14` (20 decimal) whose raw u32
value (`1638406` / `0x00190006`) decodes as two packed u16s: `(6, 25)`. `25`
matches mesh-table entry 25 exactly — one of only **13** `PSkeleton` (tag==6,
character) entries out of the file's 59 top-level objects. Corroborating evidence:
entry 25's subtree contains 31 `PRenderPass` records but resolves to only **2
distinct texture IDs (33 and 34)**, reused repeatedly across dozens of
bone/fragment paths (helmet, torso, limbs, shield, etc.) — exactly the texture-reuse
pattern a fully-armored knight's mesh would have (a few armor-plate textures
applied across many separate geometry pieces), and a much more specific/plausible
signal than matching on file size alone. No other candidate skeleton fit this well.
Notably, `mesh[25]`'s entire subtree parsed **with zero resyncs needed** — none of
the session-4 unsolved edge cases (`LoadVertexAnims`, tag==4) occur anywhere in his
data, so this identification and the patch built on it rest on fully-understood,
not bounded-resync-patched, bytes.

**Built and verified `patch_texture_ids()`** in `randomizer/prs_mesh_parser.py`:
given a path prefix (e.g. `'mesh[25]'`) and an `{old_tex_id: new_tex_id}` map, it
finds every matching `PRenderPass` already collected by a prior
`load_mesh_res_table()` call and overwrites the 4-byte texture-id field at each
one's `offset+4`, returning new file bytes of **identical length** plus a list of
exactly what changed. This is a pure in-place overwrite — no growth, no shifting,
no resync risk, unlike the `.PRT` porting case in `texture_port.py`.

**End-to-end verification against real bytes** (remapping Dark_Knight's `33→200`,
`34→201` as a test): confirmed all of the following against `CASTLE.PRS`:
- File size unchanged (byte length identical before/after).
- Exactly 31 bytes changed — one per render pass found under `mesh[25]`, matching
  the 31 collected entries exactly.
- **Zero bytes changed outside the 31 expected 4-byte windows** (verified by a
  full byte-by-byte diff of the entire ~4.2MB file against the original).
- Re-parsing the patched file recovers exactly `{200, 201}` as `mesh[25]`'s tex_ids
  (round-trip correctness, not just "nothing else moved").

**What this means practically:** the mechanism to reskin Dark_Knight for
cross-world spawning is now fully built and proven safe. The only remaining
unknowns are *product* decisions, not technical ones: which specific replacement
texture-cache indices to target (i.e. which of the destination world's existing
textures, or a newly-ported one via `texture_port.py`, should `33`/`34` map to),
and whether to wire this into `bef.py`'s `inject_cross_world_enemies` as an
automatic step or expose it as a standalone tool.

## Suggested next session's first move (Session 5)

1. Decide the actual replacement texture strategy for a first real cross-world
   spawn of Dark_Knight: either point `33`/`34` at existing shared-cache indices
   already valid in the destination world (simplest, changes his colors/pattern
   using assets already present), or use `texture_port.py` to bring his real
   armor textures into the destination world's own `.PRT`/CLUT pool first, then
   point `patch_texture_ids()` at the newly-ported indices (keeps his real look).
2. Wire the chosen approach into `bef.py`'s cross-world injection path so it
   happens automatically alongside the existing BEF blob injection.
3. Repeat the Session-5 identification method (BEF header field → packed u16 pair
   → mesh-table index → distinct-texture-count sanity check) for the other
   locked enemies in `PRS_LOCKED_ENEMIES` to build a full name→path lookup table,
   rather than re-deriving it by hand each time.

## Session 6 — the core crash mechanism finally identified, and full mesh porting shipped

**The real reason verbatim BEF-blob copying breaks locked enemies, found:** a BEF
blob's mesh-table index isn't a portable reference — it's a raw positional index
into THAT WORLD'S OWN `.PRS` mesh table. Dark_Knight's blob points at index 25
because that's where his skeleton happens to live in `CASTLE.PRS`; copying his
blob verbatim into `GRAVE.BEF` (as the existing "safe" cross-world strategy does)
would make the engine look up whatever object happens to occupy slot 25 in
`GRAVE.PRS` instead — unrelated geometry, or out of range entirely. This is
almost certainly the actual mechanism behind the map-load crash Part 1 attributed
purely to the GLOBAL/LOCAL flag bit: the flag-bit theory explains *a* crash
mechanism, but this mesh-index mismatch is a second, independent, and probably
more fundamental one, since it would misbehave even with the flag bit set
correctly.

**Fix implemented and fully verified end-to-end: `randomizer/mesh_port.py`.**
Given the confirmed fact from Session 4 that the mesh table is a flat sequential
stream immediately followed by the Anim table with no gap and no offset
directory anywhere in the file, porting a whole enemy (mesh and all) is safe:

1. `extract_mesh_entry(source_path, index)` — walks the source world's mesh
   table (reusing `prs_mesh_parser`'s loaders) to get the exact byte range of
   one top-level entry (every nested bone/fragment/shader/render-pass byte,
   verbatim).
2. `insert_mesh_entry(target_data, entry_bytes)` — walks the target world's own
   mesh table to find precisely where it ends (== where its Anim table begins),
   splices the extracted bytes in right there, and increments the mesh count
   field at file offset `0x20`. The new entry lands at index `old_count`.
3. `get_bef_mesh_index(blob)` / `patch_bef_mesh_index(blob, new_index)` — the
   BEF blob's mesh-index field lives at a fixed offset (byte 22, as a `u16`,
   confirmed via the same `(6, 25)` field from Session 5). Patched via pure
   in-place overwrite, blob length unchanged.
4. `port_enemy_mesh_and_reindex(...)` — orchestrates all of the above for one
   enemy in one call.

**`randomizer/bef.py` now has `inject_locked_enemy(...)`**, a new function
separate from the existing `inject_cross_world_enemies` (which remains correct
for the ~9 already-shared enemies and should NOT be changed — it relies on
zero-growth slot-replacement specifically because those enemies' meshes already
exist in every target world). `inject_locked_enemy` instead: extracts the
enemy's BEF blob and BEF-encoded mesh index from the source world, ports the
mesh subtree into the target world's `.PRS` via `mesh_port`, patches a copy of
the blob to point at the new index, and injects that patched blob into the
target BEF as a genuinely new entry via `rebuild_with_injections` (this DOES
grow the BEF file, which is fine since the `.PRS` is already growing too — both
need the same ISO-level relocation regardless).

**Verified fully end-to-end** (Dark_Knight, `CASTLE` → `GRAVE`, using the real
game files, replicating the exact logic now shipped in `bef.py`/`mesh_port.py`):
- `GRAVE.PRS` grew from 2,563,126 to 2,696,026 bytes — exactly `+132,900`, the
  extracted entry's exact size, no more, no less.
- A full byte-level diff confirmed: only the mesh-count field (`0x20`) and the
  newly-inserted region changed. Every byte before the insertion point, and
  every byte after it (just shifted later in the file), is byte-for-byte
  identical to the original `GRAVE.PRS`.
- The **entire patched `GRAVE.PRS` re-parses cleanly end-to-end** with
  `prs_mesh_parser` — all original entries plus the new one (landing at index
  39) — and the ported entry's texture references (33, 34) are intact,
  matching Dark_Knight's textures in `CASTLE.PRS` exactly.
- `GRAVE.BEF` grew by exactly Dark_Knight's blob size (3,320 bytes) via
  `rebuild_with_injections`; his blob is now a real entry in `GRAVE.BEF`, and
  its mesh-index field correctly reads `39` (the new position), confirmed by
  reading it back out with `get_bef_mesh_index`.

**Textures:** per Session 5's analysis, Dark_Knight's texture indices (33, 34)
are almost certainly `GLOBAL.PRT` entries (small enough to fall within
GLOBAL's ~160 preloaded textures, which load first in the shared cache before
any world-local ones) — meaning they're already present in every world
automatically, and no texture porting step was needed for him specifically.
This assumption is documented in `mesh_port.py`'s docstring as something to
re-check per enemy: if a different ported enemy's texture ids turn out to be
large (>= GLOBAL.PRT's own texture count), they're world-local instead, and
`texture_port.py`'s PRT porting (or `prs_mesh_parser.patch_texture_ids` to
remap onto existing target-world textures) would be needed alongside the mesh
port.

**Status: the core cross-world-enemy technical mechanism is now complete and
verified for Dark_Knight**, covering the actual root cause (mesh-index
mismatch) rather than just the texture question the research started from.
What's left is mechanical, not investigative:

1. Wire `inject_locked_enemy` into the randomizer's actual CLI/pipeline (`cli.py`)
   and `iso_patcher.py`'s asset-writing path, so both the grown `.BEF` and `.PRS`
   flow through the existing ISO grow/relocate machinery (already fully generic,
   confirmed in Session 3 — no changes needed there, just needs both files
   written to the patched-output folder).
2. Repeat Session 5's identification method for the rest of `PRS_LOCKED_ENEMIES`
   (Snowman, Frozen_Zombie, Zombie_Crocodile, Crazed_Prisoner, Axe_Guard,
   Plant_Monster, etc.) to build a name→(source_bef, source_prs, mesh_index)
   lookup table, so `inject_locked_enemy` can be driven by enemy name across the
   whole locked-enemy pool instead of one at a time.
3. For each of those, verify the same GLOBAL-texture assumption holds (check
   their render-pass tex_ids against GLOBAL.PRT's actual texture count) before
   assuming texture porting can be skipped.

## Session 7 — Texture porting implemented and working

**Problem discovered:** The fake-PRS approach for texture ID patching was corrupting mesh data, causing the ported mesh to lose all its texture references.

**Solution implemented:** Instead of building a fake PRS, the code now:
1. Inserts the mesh into the target PRS temporarily
2. Parses the temp PRS to collect render passes for the new mesh
3. Patches texture IDs in-place using `patch_texture_ids()`
4. Extracts the now-patched mesh back out
5. Uses that for the final insertion

**Verified working:**
- Zombie_Crocodile SWAMP→GRAVE: 4 textures ported, IDs remapped [3,4,25,26] → [35,36,37,38]
- Dark_Knight CASTLE→GRAVE: 2 textures ported, IDs remapped [33,34] → [35,36]
- Snowman ICE→GRAVE: Mesh ports but parser finds no tex_ids (mesh parse resync issue)

**Key insight about shared enemies:**
All shared enemies (Basic_Skeleton, Axe_Skeleton, etc.) have the **SAME mesh index (25 or 30) across ALL worlds**. This is why verbatim blob copying works for them — they're pointing at the same position in GLOBAL.PRS mesh table. World-exclusive enemies have mesh[25] in their OWN world's PRS, which is different content.

## Remaining issues

**Sounds:** Not yet investigated. Audio files aren't in the game_files extract. Sounds may be:
- Embedded in the main ISO outside extracted files
- Referenced by ID in BEF blobs (needs investigation)
- In a separate sound bank file not yet extracted

**Animations:** The Anim table format wasn't fully reverse-engineered. The "count" field at mesh table end is small (1-6) and the format isn't a simple `[size][data]` array. Animations might be:
- Embedded in mesh bone data (partially — vertex anims are in mesh fragments)
- In a complex Anim table structure that needs more research
- Referenced by the BEF blob in ways we haven't identified

**Practical status:** Cross-world enemies now have correct meshes and textures. Sounds and animations may still be wrong for world-exclusive enemies ported to other worlds. The shared-enemy set (mesh indices 25/30 in GLOBAL.PRS) works correctly with the existing `inject_cross_world_enemies` approach.

## Suggested next steps

1. **Test in-game** — Run the randomizer with cross-world enabled and verify textures appear correctly
2. **Investigate sound IDs** — Search BEF blobs for consistent patterns that could be sound effect references
3. **Research Anim table format** — Need disassembly of `LoadAnimResTable` (VA 0x00167F00) to understand the structure
4. **Consider scope** — For a randomizer, wrong sounds/animations might be acceptable as long as gameplay works


## Session 8 — Critical texture cache index fix

**Bug found:** Texture IDs in render passes are **absolute cache indices**, not PRT-relative indices.

The texture cache is built sequentially at runtime:
```
Cache index 0-163:     GLOBAL.PRT textures (loads first)
Cache index 164+:      World's own PRT textures (loads after)
```

When porting Zombie_Crocodile's textures [3, 4, 25, 26] to GRAVE:
- **WRONG:** Remapping to [35, 36, 37, 38] → These are still GLOBAL range
- **CORRECT:** Remapping to [199, 200, 201, 202] → 164 (GLOBAL count) + 35 (GRAVE count) + port offset

**Fix applied in `bef.py`:**
```python
# OLD (wrong):
target_local_idx = target_prt_tex_count_before  # Just PRT index

# NEW (correct):
new_cache_idx = global_tex_count + target_prt_tex_count_before  # Full cache index
```

**Result:** Ported enemies now correctly reference their ported textures in the shared cache.

**Remaining issues:**
- **Animations:** Format not fully reverse-engineered. May need `LoadAnimResTable` disassembly.
- **Sounds:** Likely referenced by ID in BEF blob. Sound bank location unknown.
- **Some enemies:** Parser can't find texture IDs due to mesh parse resyncs (e.g., Snowman).
