"""
Cross-world enemy texture porting for .PRT files.

Background: see research/CROSSWORLD_TEXTURE_RESEARCH.md, Part 3, for the full
reverse-engineering trail (disassembly of PTextureManager::Create, confirmed
via real assert-condition strings pulled from the debug ELF).

Scope of what's SOLVED and supported here: indexed (PSMT8 / 8-bit paletted)
texture entries -- the format essentially every creature/enemy sprite uses,
confirmed by GRAVE.PRT (35/35 entries) and the first 54 of CASTLE.PRT's 55
entries. This case round-trips byte-perfectly and has been verified with a
real end-to-end port (Grave -> Castle) confirming: pixel data byte-identical,
new CLUT slot byte-identical to the source palette, all pre-existing CLUTs
and texture-stream bytes in the target file completely untouched.

SOLVED (previously the blocking unknown): direct-color (fmt=0x01) entries with
TEXTURE_FLAG_OPACITY_MAP set. Such an entry's on-disk body is

    declared_size (== w*h*FMT_BPP[fmt], the colour block)
  + w*h            (the opacity/alpha block: ONE byte per pixel)

so the total is size + (1<<log2_w)*(1<<log2_h). Two independent proofs:

  1. BYTES. Walking every one of the 11 world .PRT files with this rule
     consumes each file EXACTLY: entries walked == the header's count_b and
     the walk ends precisely at EOF with zero trailing bytes (CASTLE 56,
     CASTLE_K 25, CASTLE_Q 6, GLOBAL 164, GRAVE 35, ICE 46, ICE_B 39,
     SWAMP 40, SWAMP_B 22, UNDER 46, UNDER_B 33). The four entries that used
     to block cross-world routes measure: ICE.PRT 11 (256x128, declared
     98,304, +32,768), CASTLE.PRT 44 / SWAMP.PRT 27 / UNDER.PRT 3 (128x128,
     declared 49,152, +16,384). The earlier "continues well past where the
     formula says it should stop" observation was the missing w*h, nothing
     more.
  2. THE ENGINE'S OWN LOADER, MAXDEMOR.ELF. In the texture-create function
     (the one whose CLUT bound is the assert at vaddr 0x001A6178,
     ``slti $v0, $v1, 175``):
       0x001A62E8  lh   $v0, 6($v1)     ; flags
       0x001A62EC  andi $v0, $v0, 2     ; TEXTURE_FLAG_OPACITY_MAP
       0x001A62F0  beq  $v0, $zero, 0x001A63E4   ; no opacity -> skip entirely
       0x001A6300  bne  $v1, 19, ...    ; asserts fmt != PSMT8 on this path
       0x001A634C  lh   $v0, 0($s0)     ; log2_w
       0x001A6350  lh   $a0, 2($a1)     ; log2_h
       0x001A6358  sllv $v0, 1, log2_w
       0x001A635C  sllv $v1, 1, log2_h
       0x001A6360  mult $v0, $v1        ; = w*h
       0x001A6364  sw   $v0, 28($a1)    ; opacity byte count stored
       0x001A636C  jal  0x00193030      ; malloc(that many bytes)
       0x001A63B0  jalr file->Read(ptr, that many bytes)
       0x001A63BC  beq  $v0, $v1        ; asserts bytes read == w*h
     i.e. immediately after the colour block the loader reads exactly w*h
     more bytes, sequentially, with no seek. The ELF also confirms the flag
     is never legal on an indexed entry (the 0x001A6300 assert), which is why
     the rule below only adds the block for non-indexed entries.
  3. IN-GAME CORROBORATION, ON HARDWARE. The Snowman 0x1D ice -> grave route is
     the only cross-world route that must WALK PAST an opacity-map entry
     (ICE.PRT entry 11: 256x128, fmt=1, FLAG_OPACITY_MAP, declared 98,304 B plus
     a 32,768 B opacity block) to reach its own textures. The user built and ran
     the five-route set 0x0D / 0x16 / 0x1D / 0x49 / 0x53 (+1,663,193 .PRS /
     +609,504 .PRT into GRAVE) and GRAVE LOADED with no crash. That is the FIRST
     build containing this code path to load, so the size rule now has hardware
     corroboration on top of the byte proof and the ELF disassembly. A wrong
     opacity length would have desynchronised every later entry in the walk and
     the ported textures would not have resolved.
     Scope of the claim, deliberately narrow: the build LOADING confirms the
     walk produced coherent data. It is NOT a confirmation that Snowman renders
     correctly - nobody has watched the model yet - and no allowlist route was
     promoted to CONFIRMED on the strength of it.

HONESTY GUARD, still in force: this module never guesses a length. If an
opacity-map entry's computed body does not land either exactly at EOF or on a
valid next header, the walk still REFUSES with PRTPortError rather than
proceed on an unproven size (a wrong size desynchronises every later entry).

Key design insight that simplifies injection a lot: appending a new texture
to a WORLD's PRT does NOT require re-parsing every existing entry in that
file. The texture stream is a flat, back-to-back sequence with no directory,
so "append at the end" only needs:
  - the current file length (trivial)
  - the header's count_a / count_b fields (trivial, first 16 bytes)
Extraction (reading a specific entry OUT of a SOURCE world) is the part that
needs the sequential walk, since we don't know an entry's byte offset without
walking every entry before it.

Growing the file is safe (see research doc Part 3): the format is a flat
sequential stream with no absolute-offset directory read by
PTextureManager::Create (only sequential file->Read() calls -- no seeks).
The only other place that needs to know about the new size is the ISO 9660
directory record, which randomizer/iso_patcher.py's existing grow/relocate
machinery (write_patched_assets) already handles generically for any file
placed in the patched-output folder -- .PRT needs to be added to
ASSET_EXTENSIONS there (see bottom of this file's docstring / iso_patcher.py).
"""
from __future__ import annotations
import struct
from dataclasses import dataclass

MAGIC = 0x04030201
CLUT_TABLE_SIZE = 175
CLUT_ENTRY_SIZE = 1024  # 256 colors x RGBA
HEADER_SIZE = 16
FMT_BPP = {0x00: 4, 0x01: 3, 0x13: 1, 0x14: 1}
FMT_PSMT8 = 0x13
FLAG_OPACITY_MAP = 0x0002
RESYNC_WINDOW = 0x20000  # bounded forward scan, for benign padding gaps only


class PRTPortError(Exception):
    pass


@dataclass
class PortableTexture:
    """Everything needed to inject a texture into another world's PRT."""
    header_bytes: bytes    # 16-byte TEXTURE_HEADER, verbatim from the source
    pixel_data: bytes       # raw pixel bytes (nSourceImageSize long)
    clut_bytes: bytes | None  # 1024-byte palette, if this is a PSMT8 (indexed) texture
    source_world: str
    source_index: int
    #: The w*h opacity/alpha block that follows the colour block on a
    #: non-indexed entry with FLAG_OPACITY_MAP set; None for every other entry.
    #: It must be re-appended verbatim after ``pixel_data`` or the engine's
    #: sequential read of the following entry starts w*h bytes early.
    opacity_bytes: bytes | None = None

    @property
    def is_indexed(self) -> bool:
        return self.clut_bytes is not None


def has_opacity_block(fields: dict) -> bool:
    """True when this entry carries a trailing w*h opacity block.

    Only non-indexed entries can: the engine asserts fmt != PSMT8 on the
    opacity path (vaddr 0x001A6300), and no indexed entry in any shipped .PRT
    sets the flag.
    """
    return bool(fields['flags'] & FLAG_OPACITY_MAP) and fields['fmt'] != FMT_PSMT8


def opacity_size(fields: dict) -> int:
    """Byte length of the opacity block, 0 when there is none."""
    if not has_opacity_block(fields):
        return 0
    return (1 << fields['log2_w']) * (1 << fields['log2_h'])


def entry_body_size(fields: dict) -> int:
    """Total bytes after the 16-byte header before the NEXT entry's header:
    the declared colour size plus the opacity block when present."""
    return fields['size'] + opacity_size(fields)


def _read_header_fields(data: bytes, off: int) -> dict | None:
    if off + HEADER_SIZE > len(data):
        return None
    log2_w, log2_h = struct.unpack_from('<HH', data, off)
    if not (0 < log2_w <= 10 and 0 < log2_h <= 10):
        return None
    fmt = data[off + 4]
    if fmt not in FMT_BPP:
        return None
    size = struct.unpack_from('<I', data, off + 8)[0]
    if size != (1 << log2_w) * (1 << log2_h) * FMT_BPP[fmt]:
        return None
    flags = struct.unpack_from('<H', data, off + 6)[0]
    if flags & ~FLAG_OPACITY_MAP:
        return None
    clut_index = struct.unpack_from('<h', data, off + 14)[0]
    if fmt == FMT_PSMT8 and not (0 <= clut_index < CLUT_TABLE_SIZE):
        return None
    return dict(log2_w=log2_w, log2_h=log2_h, fmt=fmt, flags=flags,
                size=size, clut_index=clut_index)


def extract_texture(prt_path: str, index: int, world_name: str = "") -> PortableTexture:
    """Walk a world's PRT sequentially and pull out texture entry `index`
    (0-based, in file order) as a PortableTexture ready to inject elsewhere.

    Opacity-map (non-indexed) entries are walked past and extracted correctly
    now -- their body is colour size + w*h (see module docstring for the two
    proofs). Raises PRTPortError if entry `index` can't be reached: the file
    ran out, the walk desynced, or an opacity-map entry's body does not land on
    EOF or a valid next header (in which case its length is NOT proven and this
    refuses instead of guessing).
    """
    with open(prt_path, 'rb') as f:
        data = f.read()
    magic = struct.unpack_from('<I', data, 0)[0]
    if magic != MAGIC:
        raise PRTPortError(f"{prt_path}: bad magic 0x{magic:08X}")
    count_a = struct.unpack_from('<I', data, 0x08)[0]
    off = 0x20 + count_a * CLUT_ENTRY_SIZE
    clut_pool = data[0x20:off]

    next_clut_expected = 0
    for i in range(index + 1):
        fields = _read_header_fields(data, off)
        if fields is None:
            found = None
            for cand in range(off, min(off + RESYNC_WINDOW, len(data) - HEADER_SIZE)):
                f2 = _read_header_fields(data, cand)
                if f2 is not None:
                    found = cand
                    fields = f2
                    break
            if fields is None:
                raise PRTPortError(
                    f"{prt_path}: couldn't locate entry {index} -- lost track "
                    f"at entry {i} (offset 0x{off:X}). Likely hit an "
                    f"unsupported opacity-map/non-indexed entry; see "
                    f"research/CROSSWORLD_TEXTURE_RESEARCH.md Part 3."
                )
            off = found

        is_indexed = fields['fmt'] == FMT_PSMT8
        if is_indexed and fields['clut_index'] != next_clut_expected:
            raise PRTPortError(
                f"{prt_path}: entry {i} CLUT index {fields['clut_index']} != "
                f"expected {next_clut_expected} -- parse likely desynced."
            )
        header_bytes = bytes(data[off:off + HEADER_SIZE])
        body_start = off + HEADER_SIZE
        pixel_data = data[body_start: body_start + fields['size']]
        opacity_len = opacity_size(fields)
        opacity_bytes = (data[body_start + fields['size']:
                              body_start + fields['size'] + opacity_len]
                         if opacity_len else None)
        after = body_start + entry_body_size(fields)

        if opacity_len:
            # HONESTY GUARD: never advance on an unproven size. The rule is
            # accepted only where it lands on real structure -- exactly EOF, or
            # a byte range that parses as the next entry's header.
            if len(pixel_data) != fields['size'] or (
                    opacity_bytes is not None and len(opacity_bytes) != opacity_len):
                raise PRTPortError(
                    f"{prt_path}: entry {i} (opacity map, fmt={fields['fmt']}) "
                    f"claims {fields['size']}+{opacity_len} body bytes but the "
                    f"file ends first. Cannot safely determine where entry "
                    f"{i+1} starts; refusing rather than guessing a size."
                )
            if after != len(data) and _read_header_fields(data, after) is None:
                raise PRTPortError(
                    f"{prt_path}: entry {i} (opacity map, fmt={fields['fmt']}) "
                    f"body of {fields['size']}+{opacity_len} bytes ends at "
                    f"0x{after:X}, which is neither end-of-file nor a valid "
                    f"entry header. Cannot safely determine where entry {i+1} "
                    f"starts; refusing rather than guessing a size."
                )

        if i == index:
            clut_bytes = None
            if is_indexed:
                clut_start = fields['clut_index'] * CLUT_ENTRY_SIZE
                clut_bytes = clut_pool[clut_start:clut_start + CLUT_ENTRY_SIZE]
                if len(clut_bytes) != CLUT_ENTRY_SIZE:
                    raise PRTPortError(
                        f"{prt_path}: entry {i} CLUT index {fields['clut_index']} "
                        f"out of range for this file's {count_a} CLUTs."
                    )
            return PortableTexture(
                header_bytes=header_bytes, pixel_data=pixel_data,
                clut_bytes=clut_bytes, source_world=world_name, source_index=index,
                opacity_bytes=opacity_bytes,
            )

        if is_indexed:
            next_clut_expected = fields['clut_index'] + 1
        off = after

    raise PRTPortError(f"{prt_path}: file ended before reaching entry {index}")


def inject_texture(target_prt_bytes: bytes, tex: PortableTexture) -> bytes:
    """Append `tex` to a target world's PRT bytes, returning the new (grown)
    file. Growing the file is safe here (see research doc): the format is a
    flat sequential stream with no offset directory, and this function only
    ever appends -- it never touches any existing entry's bytes.

    If `tex` is indexed, its palette is appended as a NEW CLUT slot (this
    file's count_a is incremented) and the injected entry's header is patched
    to point at that new slot -- it does NOT try to reuse/guess an existing
    slot in the target world, to avoid silently corrupting an unrelated
    texture that already uses that slot.

    Raises PRTPortError if the target's CLUT_TABLE_SIZE (175) would be
    exceeded by adding this palette.
    """
    magic = struct.unpack_from('<I', target_prt_bytes, 0)[0]
    if magic != MAGIC:
        raise PRTPortError(f"target: bad magic 0x{magic:08X}")
    count_a, count_b = struct.unpack_from('<II', target_prt_bytes, 0x08)
    clut_pool_start = 0x20
    clut_pool_end = clut_pool_start + count_a * CLUT_ENTRY_SIZE
    clut_pool = bytearray(target_prt_bytes[clut_pool_start:clut_pool_end])
    stream = bytearray(target_prt_bytes[clut_pool_end:])

    header = bytearray(tex.header_bytes)
    new_count_a = count_a
    if tex.is_indexed:
        if count_a >= CLUT_TABLE_SIZE:
            raise PRTPortError(
                f"target world's CLUT table is full ({count_a}/{CLUT_TABLE_SIZE}); "
                f"cannot add another palette. Consider re-using an existing "
                f"slot with a visually close palette instead, or converting "
                f"the source texture to direct-color (loses palette sharing "
                f"but sidesteps the cap)."
            )
        new_clut_index = count_a
        clut_pool += tex.clut_bytes
        struct.pack_into('<h', header, 14, new_clut_index)
        new_count_a = count_a + 1

    stream += header
    stream += tex.pixel_data
    if tex.opacity_bytes:
        # A direct-colour opacity map has NO palette, so none of the CLUT-pool
        # arithmetic above ran for it (count_a is left alone and the header's
        # clut index field is not touched). Its only extra requirement is that
        # the w*h alpha block follows the colour block verbatim, because the
        # loader reads it with a second sequential file->Read.
        stream += tex.opacity_bytes

    out = bytearray(target_prt_bytes[0:0x20])
    struct.pack_into('<I', out, 0x08, new_count_a)
    struct.pack_into('<I', out, 0x0C, count_b + 1)
    out += clut_pool
    out += stream
    return bytes(out)


def port_texture(source_prt_path: str, source_index: int, source_world: str,
                  target_prt_path: str) -> bytes:
    """Convenience wrapper: extract entry `source_index` from
    `source_prt_path` and return the bytes of `target_prt_path` with it
    appended. Caller is responsible for writing the result to disk (typically
    into the randomizer's patched-output folder so iso_patcher.py's existing
    growth machinery picks it up)."""
    tex = extract_texture(source_prt_path, source_index, source_world)
    with open(target_prt_path, 'rb') as f:
        target_bytes = f.read()
    return inject_texture(target_bytes, tex)


def list_entries(prt_path: str, limit: int | None = None) -> list[dict]:
    """Diagnostic helper: walk a PRT and return a summary of each entry
    (index, dims, format, clut index, opacity block size) for up to
    `limit` entries (default: the file's whole declared count). Useful for figuring out which
    entry index corresponds to a given enemy (see research doc Part 4 --
    the enemy-name -> texture-index mapping isn't automated yet)."""
    with open(prt_path, 'rb') as f:
        data = f.read()
    count_a, count_b = struct.unpack_from('<II', data, 0x08)
    off = 0x20 + count_a * CLUT_ENTRY_SIZE
    out = []
    next_clut_expected = 0
    i = 0
    cap = count_b if limit is None else min(count_b, limit)
    while i < cap:
        fields = _read_header_fields(data, off)
        if fields is None:
            found = None
            for cand in range(off, min(off + RESYNC_WINDOW, len(data) - HEADER_SIZE)):
                f2 = _read_header_fields(data, cand)
                if f2 is not None:
                    found = cand
                    fields = f2
                    break
            if fields is None:
                out.append({"index": i, "error": f"lost track at offset 0x{off:X}"})
                break
            off = found
        is_indexed = fields['fmt'] == FMT_PSMT8
        out.append({
            "index": i, "offset": off,
            "width": 1 << fields['log2_w'], "height": 1 << fields['log2_h'],
            "format": fields['fmt'], "flags": fields['flags'],
            "size": fields['size'], "clut_index": fields['clut_index'] if is_indexed else None,
            "opacity_size": opacity_size(fields),
        })
        if is_indexed:
            next_clut_expected = fields['clut_index'] + 1
        off += HEADER_SIZE + entry_body_size(fields)
        i += 1
    return out
