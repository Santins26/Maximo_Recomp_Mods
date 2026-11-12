"""
BEF (Binary Entity File) parser and injector for Maximo: Ghosts to Glory.

Format:
  Header (0x18 bytes):
    0x00: u32  magic (0xDEADBEEF)
    0x04: u32  version (0x00010001)
    0x08: u32  header_size (always 24)
    0x0C: u32  unk (always 0)
    0x10: u32  unk (always 0)
    0x14: u32  toc_count

  TOC (toc_count * 8 bytes, starts at 0x18):
    Each entry: u32 type_id, u32 data_offset

  Blobs follow the TOC, packed back-to-back.
  Each blob: DEADC0DE magic at +0x00, type_id at +0x10, texture section 
  between offsets stored at blob+0x20 and blob+0x24.
"""
from __future__ import annotations
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

BEF_MAGIC = 0xDEADBEEF
HEADER_SIZE = 0x18
TOC_ENTRY_SIZE = 8


@dataclass
class BefEntry:
    type_id: int
    data_offset: int
    data: bytes


@dataclass
class BefFile:
    path: Optional[Path]
    raw: bytes
    magic: int
    version: int
    toc_count: int
    entries: list[BefEntry] = field(default_factory=list)

    @classmethod
    def parse(cls, path_or_data) -> "BefFile":
        if isinstance(path_or_data, (str, Path)):
            path = Path(path_or_data)
            data = path.read_bytes()
        else:
            path = None
            data = bytes(path_or_data)

        if len(data) < HEADER_SIZE:
            raise ValueError(f"BEF too small: {len(data)} bytes")
        magic = struct.unpack_from("<I", data, 0x00)[0]
        if magic != BEF_MAGIC:
            raise ValueError(f"Bad BEF magic: 0x{magic:08X}")
        version = struct.unpack_from("<I", data, 0x04)[0]
        toc_count = struct.unpack_from("<I", data, 0x14)[0]

        toc_entries_raw = []
        for i in range(toc_count):
            off = HEADER_SIZE + i * TOC_ENTRY_SIZE
            type_id = struct.unpack_from("<I", data, off)[0]
            data_offset = struct.unpack_from("<I", data, off + 4)[0]
            toc_entries_raw.append((type_id, data_offset))

        sorted_by_offset = sorted(toc_entries_raw, key=lambda x: x[1])
        entries = []
        for idx, (type_id, data_offset) in enumerate(sorted_by_offset):
            if idx + 1 < len(sorted_by_offset):
                next_offset = sorted_by_offset[idx + 1][1]
            else:
                next_offset = len(data)
            blob = data[data_offset:next_offset]
            entries.append(BefEntry(type_id=type_id, data_offset=data_offset, data=blob))
        entries.sort(key=lambda e: e.type_id)

        return cls(path=path, raw=data, magic=magic, version=version,
                   toc_count=toc_count, entries=entries)

    def get_entry(self, type_id: int) -> Optional[BefEntry]:
        for e in self.entries:
            if e.type_id == type_id:
                return e
        return None

    def has_type(self, type_id: int) -> bool:
        return any(e.type_id == type_id for e in self.entries)

    def type_ids(self) -> set[int]:
        return {e.type_id for e in self.entries}

    def rebuild_with_slot_replacement(self, replacement_map: dict[int, BefEntry]) -> bytes:
        """Replace specific type_id slots with new entries, maintaining same file size.
        
        This mimics the working GRAVE_CROSSWORLD.BEF approach: replace unused slots
        with crossworld enemy blobs without growing the file.
        
        Parameters
        ----------
        replacement_map : dict[int, BefEntry]
            Map of {old_type_id: new_entry} for slot replacement
            
        Returns
        -------
        bytes
            Rebuilt BEF file data with same size as original
        """
        if not replacement_map:
            return self.raw
            
        # Build new TOC and data section by replacing specified slots
        toc_data = bytearray()
        data_section = bytearray()
        current_offset = HEADER_SIZE + self.toc_count * TOC_ENTRY_SIZE
        
        for i in range(self.toc_count):
            off = HEADER_SIZE + i * TOC_ENTRY_SIZE
            old_type_id = struct.unpack_from("<I", self.raw, off)[0]
            old_offset = struct.unpack_from("<I", self.raw, off + 4)[0]
            
            if old_type_id in replacement_map:
                # Replace this slot
                new_entry = replacement_map[old_type_id]
                toc_data += struct.pack("<II", new_entry.type_id, current_offset)
                data_section += new_entry.data
                current_offset += len(new_entry.data)
            else:
                # Keep original entry
                if i + 1 < self.toc_count:
                    next_offset = struct.unpack_from("<I", self.raw, HEADER_SIZE + (i + 1) * TOC_ENTRY_SIZE + 4)[0]
                else:
                    next_offset = len(self.raw)
                blob = self.raw[old_offset:next_offset]
                
                toc_data += struct.pack("<II", old_type_id, current_offset)
                data_section += blob
                current_offset += len(blob)
        
        # Build header (same TOC count)
        header = bytearray(HEADER_SIZE)
        struct.pack_into("<I", header, 0x00, self.magic)
        struct.pack_into("<I", header, 0x04, self.version)
        struct.pack_into("<I", header, 0x08, 1)
        struct.pack_into("<I", header, 0x0C, 0)
        struct.pack_into("<I", header, 0x10, 0)
        struct.pack_into("<I", header, 0x14, self.toc_count)
        
        new_data = bytes(header) + bytes(toc_data) + bytes(data_section)
        
        # Pad to original size to match working reference behavior
        if len(new_data) < len(self.raw):
            new_data += b"\x00" * (len(self.raw) - len(new_data))
        
        return new_data

    def rebuild_with_injections(self, injected: list[BefEntry], replace: bool = False) -> bytes:
        """Rebuild BEF with additional entries appended or replaced.
        
        Parameters
        ----------
        injected : list[BefEntry]
            Entries to inject
        replace : bool
            If True, replace existing entries with same type_id.
            If False (default), only append new entries (skip if already exists).
        
        Returns
        -------
        bytes
            Rebuilt BEF file data
        """
        if not injected:
            return self.raw
        
        # Separate into entries to add and entries to replace
        existing_types = self.type_ids()
        new_entries = [e for e in injected if e.type_id not in existing_types]
        replace_entries = [e for e in injected if e.type_id in existing_types]
        
        # If we're not replacing and there are no new entries, return unchanged
        if not replace and not new_entries:
            return self.raw
        
        # If we ARE replacing, handle those first
        if replace and replace_entries:
            # Build a map of type_id -> new blob for quick lookup
            replacement_map = {e.type_id: e.data for e in replace_entries}
            
            # Replace existing entries in place
            # This requires rebuilding the entire data section
            #
            # The data section MUST start after the NEW TOC. This line used to
            # use ``self.toc_count`` (the OLD count) while the header/TOC below
            # were written with ``new_toc_count``, so every offset recorded was
            # 8 bytes per appended entry (8*N) too small and EVERY pre-existing
            # entry pointed 8*N bytes before its real blob -- silently
            # corrupting the whole file (invisible enemies / wrong meshes).
            # Keep deriving offsets from the actual write position below.
            new_toc_count = self.toc_count + len(new_entries)
            toc_data = bytearray()
            data_section = bytearray()
            data_start = HEADER_SIZE + new_toc_count * TOC_ENTRY_SIZE
            
            # Read original TOC and rebuild with replacements
            for i in range(self.toc_count):
                off = HEADER_SIZE + i * TOC_ENTRY_SIZE
                type_id = struct.unpack_from("<I", self.raw, off)[0]
                old_offset = struct.unpack_from("<I", self.raw, off + 4)[0]
                
                # Determine which blob to use
                if type_id in replacement_map:
                    blob = replacement_map[type_id]
                else:
                    # Keep original blob
                    # Find the end of this blob by reading the next entry's offset (or EOF)
                    if i + 1 < self.toc_count:
                        next_offset = struct.unpack_from("<I", self.raw, HEADER_SIZE + (i + 1) * TOC_ENTRY_SIZE + 4)[0]
                    else:
                        next_offset = len(self.raw)
                    blob = self.raw[old_offset:next_offset]
                
                # Offset = where this blob actually lands in the new file.
                toc_data += struct.pack("<II", type_id, data_start + len(data_section))
                data_section += blob
            
            # Append new entries after replacements, in original TOC order.
            for entry in new_entries:
                toc_data += struct.pack("<II", entry.type_id, data_start + len(data_section))
                data_section += entry.data
            
            # Build header with updated count
            header = bytearray(HEADER_SIZE)
            struct.pack_into("<I", header, 0x00, self.magic)
            struct.pack_into("<I", header, 0x04, self.version)
            struct.pack_into("<I", header, 0x08, 1)
            struct.pack_into("<I", header, 0x0C, 0)
            struct.pack_into("<I", header, 0x10, 0)
            struct.pack_into("<I", header, 0x14, new_toc_count)
            
            return bytes(header) + bytes(toc_data) + bytes(data_section)
        
        # Original behavior: only append new entries
        if not new_entries:
            return self.raw
        
        new_toc_count = self.toc_count + len(new_entries)
        old_toc_size = self.toc_count * TOC_ENTRY_SIZE
        new_toc_size = new_toc_count * TOC_ENTRY_SIZE
        old_data_start = HEADER_SIZE + old_toc_size
        new_data_start = HEADER_SIZE + new_toc_size
        toc_growth = new_data_start - old_data_start

        existing_data = self.raw[old_data_start:]
        new_blobs = b""
        new_blob_offsets = []
        append_offset = new_data_start + len(existing_data)
        for entry in new_entries:
            new_blob_offsets.append(append_offset)
            new_blobs += entry.data
            append_offset += len(entry.data)

        toc_data = bytearray()
        for i in range(self.toc_count):
            off = HEADER_SIZE + i * TOC_ENTRY_SIZE
            type_id = struct.unpack_from("<I", self.raw, off)[0]
            old_offset = struct.unpack_from("<I", self.raw, off + 4)[0]
            toc_data += struct.pack("<II", type_id, old_offset + toc_growth)
        for entry, blob_offset in zip(new_entries, new_blob_offsets):
            toc_data += struct.pack("<II", entry.type_id, blob_offset)

        header = bytearray(HEADER_SIZE)
        struct.pack_into("<I", header, 0x00, self.magic)
        struct.pack_into("<I", header, 0x04, self.version)
        struct.pack_into("<I", header, 0x08, 1)
        struct.pack_into("<I", header, 0x0C, 0)
        struct.pack_into("<I", header, 0x10, 0)
        struct.pack_into("<I", header, 0x14, new_toc_count)

        return bytes(header) + bytes(toc_data) + existing_data + new_blobs
MATERIAL_SECTION_START_OFFSET = 0x20
MATERIAL_SECTION_END_OFFSET = 0x24

#: rec_type byte -> resource category it introduces. Measured across all 359
#: blobs of all five worlds: 0x05 always carries a CATEGORY-1 reference and
#: comes first when both are present; 0x04 always carries CATEGORY-2 and comes
#: second.
MATERIAL_REC_TYPES: dict[int, int] = {0x05: 1, 0x04: 2}

#: A resource reference is 1 byte of rec_type plus a u32 LE whose top byte is
#: a flag (category in bits 24-30, is_global in bit 31) and whose low 24 bits
#: are the index.
MATERIAL_REF_SIZE = 5
MATERIAL_INDEX_MASK = 0x00FFFFFF
MATERIAL_CATEGORY_MASK = 0x7F
MATERIAL_GLOBAL_BIT = 0x80

#: CATEGORY-1 = an index into the world's .PRS MESH TABLE.
MATERIAL_CATEGORY_MESH = 1
#: CATEGORY-2 = an index into the world's .PRS ANIM TABLE.
MATERIAL_CATEGORY_ANIM = 2


class MaterialSectionError(Exception):
    """A blob's material section could not be walked with its own framing.

    Framing is ``[record_count u32]`` then per record ``[len u32][body...]``,
    and a correct walk consumes exactly ``record_count`` records and lands
    exactly on the section end. Anything else means the bytes are not what we
    think they are, so callers must refuse rather than guess.
    """


@dataclass(frozen=True)
class MaterialRef:
    """One resource reference at the head of a material record's body.

    ``idx_offset`` is the byte offset, WITHIN THE BLOB, of the u32 whose low 24
    bits are the index and whose top byte is the flag. Rewriting must preserve
    that flag byte exactly -- it carries both the category and the is_global
    bit, and forging either repoints the reference at a different resource
    table.
    """

    record: int          # record ordinal within the material section
    slot: int            # 0 = first ref of the record, 1 = second
    rec_type: int        # 0x05 (category 1) or 0x04 (category 2)
    idx_offset: int      # blob offset of the u32 index/flag field
    flag: int            # bits 24-31 of that u32
    index: int           # bits 0-23 of that u32

    @property
    def category(self) -> int:
        """Resource category, bits 24-30 of the u32."""
        return self.flag & MATERIAL_CATEGORY_MASK

    @property
    def is_global(self) -> bool:
        """Bit 31: the reference resolves against GLOBAL.PRS, not the world's."""
        return bool(self.flag & MATERIAL_GLOBAL_BIT)

    def describe(self) -> str:
        scope = "GLOBAL" if self.is_global else "LOCAL"
        return (f"rec{self.record}.{self.slot} @0x{self.idx_offset:X} "
                f"type=0x{self.rec_type:02X} cat={self.category} "
                f"{scope} index={self.index}")


def material_section_bounds(blob: bytes) -> tuple[int, int]:
    """Return ``(section_start, section_end)`` from blob+0x20 / blob+0x24."""
    if len(blob) < MATERIAL_SECTION_END_OFFSET + 4:
        raise MaterialSectionError(
            f"blob is {len(blob)} bytes, too small to hold material section "
            f"bounds")
    start = struct.unpack_from("<I", blob, MATERIAL_SECTION_START_OFFSET)[0]
    end = struct.unpack_from("<I", blob, MATERIAL_SECTION_END_OFFSET)[0]
    if not (0 < start < end <= len(blob)):
        raise MaterialSectionError(
            f"material section bounds [{start}, {end}) are not inside the "
            f"{len(blob)}-byte blob")
    return start, end


def parse_material_refs(blob: bytes) -> list[MaterialRef]:
    """Parse every resource reference in a BEF blob's material section.

    The section at ``[blob+0x20, blob+0x24)`` is framed as::

        [record_count u32]
        per record: [len u32][body ... len bytes]

    and each record's BODY begins with one or two 5-byte resource references
    ``[rec_type u8][u32 LE = flag<<24 | index]``. rec_type 0x05 introduces a
    CATEGORY-1 reference (an index into the world's .PRS MESH TABLE) and comes
    first when present; rec_type 0x04 introduces CATEGORY-2 (an index into that
    world's .PRS ANIM TABLE) and comes second.

    Walking with this framing parses all 359 blobs across all five worlds with
    the record count and the section end both landing exactly, which is why the
    walk is strict: a mismatch raises ``MaterialSectionError`` instead of
    returning a partial, guessed list.
    """
    start, end = material_section_bounds(blob)
    if start + 4 > end:
        raise MaterialSectionError(
            f"material section [{start}, {end}) has no room for a record count")
    count = struct.unpack_from("<I", blob, start)[0]
    refs: list[MaterialRef] = []
    pos = start + 4
    for k in range(count):
        if pos + 4 > end:
            raise MaterialSectionError(
                f"record {k} of {count}: length field at {pos} runs past the "
                f"section end {end}")
        length = struct.unpack_from("<I", blob, pos)[0]
        body = pos + 4
        body_end = body + length
        if body_end > end:
            raise MaterialSectionError(
                f"record {k} of {count}: body [{body}, {body_end}) runs past "
                f"the section end {end}")
        p = body
        slot = 0
        while (p + MATERIAL_REF_SIZE <= body_end
               and blob[p] in MATERIAL_REC_TYPES):
            raw = struct.unpack_from("<I", blob, p + 1)[0]
            refs.append(MaterialRef(
                record=k,
                slot=slot,
                rec_type=blob[p],
                idx_offset=p + 1,
                flag=(raw >> 24) & 0xFF,
                index=raw & MATERIAL_INDEX_MASK,
            ))
            p += MATERIAL_REF_SIZE
            slot += 1
        pos = body_end
    if pos != end:
        raise MaterialSectionError(
            f"material walk consumed {count} record(s) and stopped at {pos}, "
            f"but the section ends at {end} ({end - pos:+d} bytes)")
    return refs


def set_material_ref_index(blob: bytes, ref: MaterialRef,
                           new_index: int) -> bytes:
    """Return a copy of ``blob`` with ``ref``'s index rewritten.

    ONLY the 24 index bits change. The top byte -- and therefore both the
    resource category (bits 24-30) and the is_global bit (31) -- is written back
    byte-identical, read from the buffer rather than from ``ref``, so a stale
    ref object can never forge a flag.

    At current table sizes every index fits in one byte, but the field is 24
    bits wide and the full width is written: the u32 at ``ref.idx_offset``
    becomes ``(flag << 24) | new_index`` in little-endian, so indices above 255
    correctly spill into the second and third bytes and never touch the flag.
    """
    if not isinstance(new_index, int) or new_index < 0:
        raise ValueError(f"new_index must be a non-negative int, got {new_index!r}")
    if new_index > MATERIAL_INDEX_MASK:
        raise ValueError(
            f"new_index {new_index} does not fit in the 24-bit index field "
            f"(max {MATERIAL_INDEX_MASK})")
    if ref.idx_offset + 4 > len(blob):
        raise ValueError(
            f"ref index field at {ref.idx_offset} is outside the "
            f"{len(blob)}-byte blob")
    out = bytearray(blob)
    flag = out[ref.idx_offset + 3]          # LE: top byte is the flag byte
    struct.pack_into("<I", out, ref.idx_offset, (flag << 24) | new_index)
    assert out[ref.idx_offset + 3] == flag, "flag byte must survive a rewrite"
    return bytes(out)


def find_texture_indices(blob: bytes) -> list[int]:
    """DEPRECATED -- use ``parse_material_refs`` instead.

    This looked for the byte pattern ``00 00 00 [04|05] [IDX] 00 00
    [01|02|03|04]`` in a flat scan of the material section. That pattern
    demands three zero bytes IMMEDIATELY BEFORE the rec_type byte, which only
    holds when the reference is preceded by the zero-padded tail of something
    else. When two references sit adjacent -- the normal case, category 1
    (rec_type 0x05) followed directly by category 2 (rec_type 0x04) -- the
    second one's rec_type is preceded by the first one's index/flag bytes, so
    it never matches. The scan therefore reports at most the FIRST reference of
    an adjacent pair and misses every category-2 reference, and it has no idea
    which category, index, or global bit it is looking at.

    Kept only so stale callers keep importing. Do not build anything new on it:
    ``parse_material_refs`` walks the real ``[count][len][body]`` framing and
    returns typed refs with category, index and is_global.
    """
    if len(blob) < 0x28:
        return []
    sec_start = struct.unpack_from("<I", blob, 0x20)[0]
    sec_end = struct.unpack_from("<I", blob, 0x24)[0]
    if sec_start >= len(blob) or sec_end > len(blob) or sec_start >= sec_end:
        return []

    results = []
    for off in range(sec_start, min(sec_end, len(blob) - 7)):
        if (blob[off] == 0x00 and blob[off+1] == 0x00 and blob[off+2] == 0x00
            and blob[off+3] in (0x04, 0x05)
            and blob[off+5] == 0x00 and blob[off+6] == 0x00
            and blob[off+7] in (0x01, 0x02, 0x03, 0x04)):
            if blob[off+4] > 0:
                results.append(off + 4)
    return results


def remap_blob_textures(blob: bytes, target_bef: "BefFile") -> bytes:
    """Return the blob unchanged -- and that is NOT good enough for a foreign world.

    THE OLD DOCSTRING'S CLAIM IS DISPROVEN. It said "the engine resolves each
    enemy type's textures by TYPE_ID lookup against the PRS/PRT resource
    tables, not by indices embedded in the BEF blob". A blob's material section
    (``[blob+0x20, blob+0x24)``) demonstrably DOES embed per-world resource
    indices: each material record's body opens with one or two 5-byte
    references ``[rec_type u8][u32 = flag<<24 | index]`` where a CATEGORY-1
    reference is a positional index into THAT WORLD's .PRS mesh table and a
    CATEGORY-2 reference indexes that world's .PRS anim table (bit 31 marks the
    handful that resolve against GLOBAL instead). Proof: Dark_Knight 0x53's
    CASTLE cat-1 refs are 25, 25, 26, and CASTLE.PRS mesh[25] is the tag-6,
    132,900-byte, 34-bone, 31-render-pass PSkeleton independently identified as
    his mesh. The same asset sits at a DIFFERENT index in every world.

    Setting bit 7 on the flags (the even older behaviour) was also wrong: it
    claims the resource lives in GLOBAL, which for an enemy mesh it does not.

    So this function stays a no-op ON PURPOSE, but it is NOT a substitute for
    porting. Copying a foreign blob verbatim leaves its indices pointing at
    whatever occupies those slots in the target world. Correct cross-world
    placement goes through ``cross_world.WorldPortSession.port_enemy``, which
    ports the referenced mesh entries into the target .PRS and rewrites each
    local cat-1 reference with ``set_material_ref_index``.
    ``inject_cross_world_enemies`` below, which calls this, is the LEGACY
    verbatim-copy path and is not index-correct.
    """
    return blob


# Type IDs that must NEVER be evicted from a BEF, regardless of whether
# they appear in that world's PSX files. Walking-melee enemies and item
# types are all potentially placed by the randomizer and must stay.
_NEVER_EVICT: frozenset[int] = frozenset({
    # Walking-melee enemies (all worlds)
    0x01, 0x08, 0x0D, 0x10, 0x13, 0x19, 0x1D, 0x27, 0x2F,
    0x37, 0x39, 0x47, 0x48, 0x49, 0x53, 0x54, 0x58, 0x5D, 0x6A, 0x6B,
    # Item / interactable pool
    0x20, 0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x2A, 0x2B,
    0x3F, 0x41, 0x45, 0x46, 0x4E, 0x51, 0x52, 0x59, 0x5B, 0x65,
})


def _evictable_entries(
    bef: "BefFile",
    psx_used_types: set[int],
) -> list["BefEntry"]:
    """Return BEF entries safe to replace with foreign content.

    Safe = not in _NEVER_EVICT AND not referenced by any entity record in
    the world's own PSX files. Sorted largest-first to free most bytes.
    """
    return sorted(
        [e for e in bef.entries
         if e.type_id not in _NEVER_EVICT
         and e.type_id not in psx_used_types],
        key=lambda e: len(e.data),
        reverse=True,
    )


def inject_locked_enemy(
    enemy_type_id: int,
    source_bef_path: Path,
    source_prs_path: Path,
    target_bef_path: Path,
    target_prs_path: Path,
    source_prt_path: Optional[Path] = None,
    target_prt_path: Optional[Path] = None,
    source_mesh_index: Optional[int] = None,
) -> tuple[bytes, bytes, Optional[bytes], int]:
    """Port a world-exclusive enemy (mesh + textures) into a foreign world and
    inject its BEF blob with corrected indices.

    *** WARNING: UNFOUNDED PREMISE -- DO NOT CALL. ***
    This uses the same false field as ``mesh_port``: the u16 at BEF blob offset
    22, read here as a positional .PRS mesh-table index. It is not one. In a
    single GRAVE.BEF, seven native enemies all carry 25 there (Zombie_Crocodile
    0x0D, Axe_Skeleton 0x2F, Raven 0x37, Basic_Skeleton 0x47, Guard_Skeleton
    0x54, Torso_Zombie 0x6A, Ghost 0x6B); Basic_Skeleton and Guard_Skeleton both
    render correctly in vanilla and look nothing alike, so 25 cannot be a shared
    model index. Three more natives share 30. So "extract mesh[N], splice it in,
    rewrite offset 22" splices an unrelated object and overwrites a field of
    unknown meaning: the enemy spawns wearing an arbitrary prop. The whole mesh
    splice path is parked at ``cross_world.MESH_PORT_ENABLED = False``; nothing
    downstream of offset 22 is trustworthy. The ``global_tex_count = 164``
    hardcoded fallback below is a separate, independently bad idea (see
    ``cross_world.read_global_tex_count``).

    World-exclusive enemies can't be injected via inject_cross_world_enemies
    alone, because:
      1. Their BEF blob contains a raw positional mesh-table index into their
         HOME WORLD's .PRS — copying verbatim makes the engine look up the
         wrong object in the target world, causing a crash or wrong geometry.
      2. Their mesh's texture IDs are world-relative indices (e.g. texture 25
         = source_world.PRT[25], not GLOBAL.PRT[25]). In the target world,
         the same cache index points at a completely different texture.

    This function ports BOTH the mesh geometry AND its textures:
      1. Parses the source mesh to extract which texture IDs it references.
      2. Ports those textures from source_world.PRT into target_world.PRT
         (each texture grows the target PRT by ~1-8KB + 1024-byte CLUT).
      3. Builds a remap (old_tex_id → new_tex_id) and patches the mesh's
         render-pass texture fields in-place before inserting it.
      4. Inserts the patched mesh into the target .PRS, patches the BEF
         blob's mesh-index field, and injects the blob into the target BEF.

    Parameters
    ----------
    enemy_type_id    : the entity type_id (e.g. 0x53 for Dark_Knight).
    source_bef_path  : path to source world's .BEF (e.g. CASTLE.BEF).
    source_prs_path  : path to source world's .PRS (e.g. CASTLE.PRS).
    target_bef_path  : path to target world's current .BEF (read-only).
    target_prs_path  : path to target world's current .PRS (read-only).
    source_prt_path  : path to source world's .PRT. If None (default),
                       texture porting is skipped (use only if the enemy's
                       textures are confirmed GLOBAL, like Dark_Knight).
    target_prt_path  : path to target world's .PRT. Required if
                       source_prt_path is supplied.
    source_mesh_index: override the mesh index read from the BEF blob.
                       Leave as None to read it automatically from offset 22.

    Returns
    -------
    (new_bef_bytes, new_prs_bytes, new_prt_bytes_or_none, new_mesh_index):
      new_bef_bytes         — target BEF with the enemy's patched blob.
      new_prs_bytes         — target PRS with the ported (texture-remapped) mesh.
      new_prt_bytes_or_none — target PRT with ported textures, or None if
                              source_prt_path was None (texture port skipped).
      new_mesh_index        — the index the ported mesh now occupies in the
                              target world's mesh table (= old mesh-count).

    Raises ValueError if the enemy type_id is not found in the source BEF,
    or if only one of source_prt_path/target_prt_path is supplied (both or
    neither required). Any parse/IO error from mesh_port or texture_port is
    propagated to the caller.
    """
    from .mesh_port import extract_mesh_entry, insert_mesh_entry, patch_bef_mesh_index, get_bef_mesh_index
    from . import prs_mesh_parser as pmp
    from .prs_mesh_parser import find_render_passes_under, patch_texture_ids
    
    if (source_prt_path is None) != (target_prt_path is None):
        raise ValueError(
            "inject_locked_enemy: source_prt_path and target_prt_path must "
            "both be supplied or both be None"
        )

    # --- 1. Read source BEF blob -------------------------------------------
    src_bef = BefFile.parse(source_bef_path)
    entry = src_bef.get_entry(enemy_type_id)
    if entry is None:
        raise ValueError(
            f"inject_locked_enemy: type_id 0x{enemy_type_id:02X} not found "
            f"in {Path(source_bef_path).name}"
        )
    blob = entry.data

    # --- 2. Determine the mesh-table index to extract ---------------------
    if source_mesh_index is None:
        source_mesh_index = get_bef_mesh_index(blob)

    # --- 3. Extract the mesh subtree from the source .PRS -----------------
    entry_bytes, _tag = extract_mesh_entry(source_prs_path, source_mesh_index)

    # --- 4. TEXTURE PORTING (if requested) ---------------------------------
    new_prt_bytes = None
    tex_id_remap = {}
    
    if source_prt_path is not None:
        from .texture_port import extract_texture, inject_texture, PRTPortError
        import struct
        
        # Read the target PRT to start with
        new_prt_bytes = Path(target_prt_path).read_bytes()
        
        # Parse the source mesh to find which texture IDs it uses
        pmp.render_passes = []
        source_prs_data = Path(source_prs_path).read_bytes()
        r = pmp.Reader(source_prs_data, 0x20)
        pmp.load_mesh_res_table(r)
        passes = find_render_passes_under(f'mesh[{source_mesh_index}]')
        source_tex_ids = sorted(set(tid for _, tid, _ in passes if tid >= 0))
        
        if source_tex_ids:
            # Read GLOBAL.PRT texture count to determine which IDs are world-local
            global_prt_path = Path(source_prt_path).parent / "GLOBAL.PRT"
            if global_prt_path.exists():
                global_tex_count = struct.unpack_from('<I', global_prt_path.read_bytes(), 0x0C)[0]
            else:
                global_tex_count = 164  # fallback from known data
            
            # Filter to ONLY world-local texture IDs (>= global_tex_count)
            # Texture IDs < global_tex_count are GLOBAL textures, already present everywhere
            world_local_tex_ids = [tid for tid in source_tex_ids if tid >= global_tex_count]
            
            if world_local_tex_ids:
                # Convert cache indices to PRT indices: cache_idx - global_tex_count = PRT_idx
                source_prt_indices = [tid - global_tex_count for tid in world_local_tex_ids]
                
                # Port each world-local texture from source PRT to target PRT
                target_prt_data = new_prt_bytes
                target_prt_tex_count_before = struct.unpack_from('<I', target_prt_data, 0x0C)[0]
                
                for source_cache_idx, source_prt_idx in zip(world_local_tex_ids, source_prt_indices):
                    try:
                        tex = extract_texture(str(source_prt_path), source_prt_idx, "source")
                        target_prt_data = inject_texture(target_prt_data, tex)
                        
                        # The new cache index = GLOBAL + target's new PRT count
                        new_cache_idx = global_tex_count + target_prt_tex_count_before
                        tex_id_remap[source_cache_idx] = new_cache_idx
                        target_prt_tex_count_before += 1
                    except (PRTPortError, FileNotFoundError, IndexError) as e:
                        # If texture porting fails, don't add to remap
                        # The mesh will reference a missing texture, which will
                        # likely cause a game crash when this mesh is rendered.
                        # We log this but continue, since stopping would be worse.
                        pass
                
                new_prt_bytes = target_prt_data

    # --- 5. Patch texture IDs in the extracted mesh (if we ported textures) -
    if tex_id_remap:
        # Patch render-pass texture IDs directly in the extracted mesh bytes.
        # prs_mesh_parser.patch_texture_ids expects full PRS bytes, so we'll
        # insert the mesh FIRST, then patch it in-place, then extract it back.
        # This avoids the fake-PRS approach which was breaking the mesh structure.
        
        # Insert unpatched mesh into target PRS temporarily
        target_prs_data = Path(target_prs_path).read_bytes()
        temp_prs_with_mesh, temp_mesh_index = insert_mesh_entry(target_prs_data, entry_bytes)
        
        # Parse this temp PRS to collect render passes
        pmp.render_passes = []
        r_temp = pmp.Reader(temp_prs_with_mesh, 0x20)
        pmp.load_mesh_res_table(r_temp)
        
        # Find render passes in our newly-inserted mesh
        temp_passes = find_render_passes_under(f'mesh[{temp_mesh_index}]')
        
        if temp_passes:
            # Patch the texture IDs in-place
            patched_prs, patch_log = patch_texture_ids(
                temp_prs_with_mesh, 
                f'mesh[{temp_mesh_index}]', 
                tex_id_remap
            )
            
            # Extract the now-patched mesh back out
            # Find its bounds in the patched PRS
            from .mesh_port import find_entry_bounds
            patched_start, patched_end, _ = find_entry_bounds(
                patched_prs, 
                temp_mesh_index
            )
            entry_bytes = patched_prs[patched_start:patched_end]

    # --- 6. Insert the (possibly texture-remapped) mesh into target PRS ----
    target_prs_data = Path(target_prs_path).read_bytes()
    new_prs_data, new_mesh_index = insert_mesh_entry(target_prs_data, entry_bytes)

    # --- 7. Patch the blob's mesh-index field and inject into target BEF ---
    patched_blob = patch_bef_mesh_index(blob, new_mesh_index)
    new_entry = BefEntry(type_id=enemy_type_id, data_offset=0, data=patched_blob)

    target_bef = BefFile.parse(target_bef_path)
    # Check if this enemy type already exists in the target BEF.
    # If it does, we should replace it (to update the mesh index).
    # If it doesn't, we should append it.
    # For cross-world porting, the enemy should NEVER already exist (because
    # we check bef_native_types before calling this function), but use replace=True
    # anyway to handle edge cases where the file on disk is out of sync with memory.
    replace = target_bef.has_type(enemy_type_id)
    new_bef_data = target_bef.rebuild_with_injections([new_entry], replace=replace)

    print(f"[DEBUG] inject_locked_enemy(0x{enemy_type_id:02X}):")
    print(f"  Mesh index: {source_mesh_index} -> {new_mesh_index}")
    print(f"  BEF grew by {len(new_bef_data) - len(Path(target_bef_path).read_bytes())} bytes")
    print(f"  PRS grew by {len(new_prs_data) - len(Path(target_prs_path).read_bytes())} bytes")
    if new_prt_bytes:
        print(f"  PRT grew by {len(new_prt_bytes) - len(Path(target_prt_path).read_bytes())} bytes")
        print(f"  Texture IDs remapped: {len(tex_id_remap)} entries")
    else:
        print(f"  PRT: NO CHANGES (using GLOBAL textures only)")
    print(f"  [WARNING] Animations may need porting (format not yet reverse-engineered)")
    return new_bef_data, new_prs_data, new_prt_bytes, new_mesh_index


def inject_cross_world_enemies(
    target_bef_path: Path,
    source_bef_paths: list[Path],
    enemy_types_to_inject: set[int],
    psx_used_types: set[int] | None = None,
    output_path: Optional[Path] = None,
) -> tuple[bytes, list[int]]:
    """Inject foreign enemy blobs into the target BEF via TOC-slot replacement.

    Strategy (zero file growth, no ISO relocation required):
      1. Identify BEF entries safe to evict (not used by this world's PSX
         files, not in the item/enemy pool the randomizer places).
      2. Sort evictable slots by size descending (free most bytes first).
      3. Sort foreign enemy blobs by size ascending (pack smallest first).
      4. Greedily assign each foreign blob to the smallest evictable slot
         that is >= the blob size, replacing that slot's type_id and data.
      5. Rebuild the BEF with the SAME toc_count. File stays the same size
         (padded with zeros), so no ISO sector relocation is required.

    This mirrors what GRAVE_CROSSWORLD.BEF does: same file size, same
    toc_count, replaced blobs. Avoids the crash caused by growing the BEF
    and relocating it on disc (the PS2 CDVD driver may use a cached LBA).

    Foreign blobs are remapped (flag |= 0x80) to force GLOBAL resource
    loading so they render with GLOBAL.PRS/PRT textures instead of the
    home-world's local ones (correct mesh, potentially different colours).
    """
    target = BefFile.parse(target_bef_path)
    have = target.type_ids()
    used = psx_used_types or set()

    # Collect foreign enemy blobs not already in target, sorted smallest first.
    foreign: list[BefEntry] = []
    for source_path in source_bef_paths:
        if Path(source_path) == Path(target_bef_path):
            continue
        try:
            source = BefFile.parse(source_path)
        except Exception:
            continue
        for type_id in enemy_types_to_inject:
            if type_id in have:
                continue
            entry = source.get_entry(type_id)
            if entry is not None:
                remapped = remap_blob_textures(entry.data, target)
                foreign.append(BefEntry(type_id=type_id, data_offset=0, data=remapped))
                have.add(type_id)
    foreign.sort(key=lambda e: len(e.data))  # smallest first

    if not foreign:
        return target.raw, []

    evictable = _evictable_entries(target, used)

    # Greedy assignment: each foreign blob takes the smallest available slot >= it.
    available = list(evictable)  # sorted large -> small
    assignments: list[tuple[BefEntry, BefEntry]] = []  # (evicted, foreign)
    for fe in foreign:
        best = None
        best_idx = -1
        for i, slot in enumerate(available):
            if len(slot.data) >= len(fe.data):
                if best is None or len(slot.data) < len(best.data):
                    best = slot
                    best_idx = i
        if best is not None:
            assignments.append((best, fe))
            available.pop(best_idx)

    if not assignments:
        return target.raw, []

    replace_map: dict[int, BefEntry] = {
        evicted.type_id: fe for evicted, fe in assignments
    }

    # Rebuild: walk original TOC order, replace matched slots, keep rest.
    toc_count = target.toc_count
    data_start = HEADER_SIZE + toc_count * TOC_ENTRY_SIZE
    new_toc = bytearray()
    new_blobs = bytearray()
    for i in range(toc_count):
        off = HEADER_SIZE + i * TOC_ENTRY_SIZE
        type_id = struct.unpack_from("<I", target.raw, off)[0]
        if type_id in replace_map:
            fe = replace_map[type_id]
            blob = fe.data
            new_type_id = fe.type_id
        else:
            entry = target.get_entry(type_id)
            blob = entry.data if entry else b""
            new_type_id = type_id
        blob_offset = data_start + len(new_blobs)
        new_toc += struct.pack("<II", new_type_id, blob_offset)
        new_blobs += blob

    header = bytearray(HEADER_SIZE)
    struct.pack_into("<I", header, 0x00, target.magic)
    struct.pack_into("<I", header, 0x04, target.version)
    struct.pack_into("<I", header, 0x08, 1)
    struct.pack_into("<I", header, 0x0C, 0)
    struct.pack_into("<I", header, 0x10, 0)
    struct.pack_into("<I", header, 0x14, toc_count)

    new_data = bytes(header) + bytes(new_toc) + bytes(new_blobs)

    # Pad to original size — zero file growth, no sector relocation needed.
    if len(new_data) < len(target.raw):
        new_data += b"\x00" * (len(target.raw) - len(new_data))
    elif len(new_data) > len(target.raw):
        import warnings
        warnings.warn(
            f"{Path(target_bef_path).name}: grew by "
            f"{len(new_data)-len(target.raw)} bytes despite slot-replacement strategy "
            f"(ISO relocation may be required)."
        )

    injected_ids = [fe.type_id for _, fe in assignments]
    if output_path:
        Path(output_path).write_bytes(new_data)
    return new_data, injected_ids
