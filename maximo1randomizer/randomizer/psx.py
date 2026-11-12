"""
PSX (level instance) file parser/writer for Maximo: Ghosts to Glory.

The header layout has different sizes for different worlds. The BEF path is
in a 256-byte buffer and records start 0x40 bytes later. Header layout:

  GRAVE-world layout:
    0x000-0x430: World metadata (1072 bytes)
    0x430-0x530: BEF path buffer (256 bytes)
    0x530:       u32 record count
    0x534:       u32 records-section SIZE (the "post-records pointer")
    0x538-0x570: 56 bytes manager metadata
    0x570+:      records back-to-back

  UNDER-world layout (header is 0x200 smaller):
    0x000-0x230: World metadata (560 bytes)
    0x230-0x330: BEF path buffer
    0x330:       u32 record count
    0x334:       u32 records-section size
    0x338-0x370: manager metadata
    0x370+:      records

Each record:
  +0x000: 256 bytes class name buffer (debug)
  +0x100: 256 bytes instance name buffer
  +0x204: 1 byte = entity TYPE ID
  +0x208: u32 instance ID
  +0x20C: u32 property count (size = 0x228 + count*0x20)
  +0x210: 12 bytes (X, Y, Z position floats)
  +0x228+: property entries (32 bytes each)

Post-records field semantics (MEASURED, not inferred): the u32 at
`post_records_ptr_offset` is a SIZE, not an absolute file offset. Across the 33
vanilla game_files/*.PSX files, 32 have that field exactly equal to the summed
byte length of the parsed records, and NONE equal `records_start + that sum`.
(The exception is U_SKY.PSX, which the layout detector fails to resolve and
which parses to zero records - a pre-existing, unrelated condition.)
Consequence: because the field is a size, no other absolute offset in the file
needs relocating when the records section grows or shrinks.

`total_shift` accumulates `r.size - nr.size`, so it is POSITIVE on shrink.
Subtracting it therefore INCREASES the field when the records section grows and
DECREASES it when the section shrinks, which is correct in both directions.

The writer emits exactly header + records + preserved tail: the output length
reflects the records actually written, with no padding to and no truncation to
the input length. The returned `file_size` is therefore the ACTUAL output
length (it previously reported the input length, because the output was padded
back to match).
"""
from __future__ import annotations
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple


class BuiltPsxBytes(NamedTuple):
    """Result of assembling a rewritten PSX file in memory (nothing written)."""
    data: bytearray
    total_shift: int   # sum of (old.size - new.size); POSITIVE on shrink
    replaced: int
    tail_start: int    # index in `data` where the preserved tail begins


NAME_BUFFER_SIZE = 0x100
RECORD_HEADER_SIZE = 0x200  # = 2 * NAME_BUFFER_SIZE
ENTITY_HEADER_SIZE = 0x28  # bytes from +0x200 to +0x228
ENTRY_SIZE = 0x20  # bytes per property entry

# Player spawn / level-entry coordinate. Stored in the header's manager-
# metadata block as 3 little-endian floats (X, Y, Z), located 0x38 bytes
# before the records section starts. Verified across all worlds: in HUB maps
# it matches the Level_Column the player stands on; in levels it sits in the
# start area. (The 3 floats that follow look like a facing/orientation vector
# and are left untouched.)
PLAYER_SPAWN_REL = 0x38

# Size_Ceiling revert point. `None` means UNLIMITED, which is the default: the
# records section may grow freely. Setting this to the string "input" restores
# the historical never-grow behaviour (the writer refuses any output longer than
# the input file). Setting it to an int imposes that explicit byte limit.
#
# This is the ONLY place in the pipeline that bounds records-section growth, so
# restoring the bound is the single edit
#     RECORDS_SECTION_SIZE_CEILING = None  ->  = "input"
# Note that restoring the ceiling restores the REFUSAL only; the byte-rationing
# machinery that used to keep the pipeline under it is gone, so a bounded run is
# expected to fail loudly on maps that grow.
RECORDS_SECTION_SIZE_CEILING: int | str | None = None


def _size_ceiling_for(input_len: int) -> int | None:
    """Resolve the configured ceiling to a concrete byte limit, or None."""
    if RECORDS_SECTION_SIZE_CEILING is None:
        return None
    if RECORDS_SECTION_SIZE_CEILING == "input":
        return input_len          # the historical never-grow behaviour
    return int(RECORDS_SECTION_SIZE_CEILING)


def compute_record_size(prop_count: int) -> int:
    return RECORD_HEADER_SIZE + ENTITY_HEADER_SIZE + prop_count * ENTRY_SIZE


@dataclass
class PsxRecord:
    offset: int
    class_name: str
    instance_name: str
    type_id: int
    instance_id: int
    prop_count: int
    pos_x: float
    pos_y: float
    pos_z: float
    raw: bytes

    @property
    def size(self) -> int:
        return len(self.raw)


@dataclass
class PsxFile:
    path: Path
    raw: bytes
    records: list[PsxRecord]
    bef_path: str
    bef_offset: int
    record_count_field: int
    record_count_offset: int
    post_records_ptr: int
    post_records_ptr_offset: int
    records_start: int

    @classmethod
    def parse(cls, path: Path | str) -> "PsxFile":
        path = Path(path)
        data = path.read_bytes()
        layout = _detect_header_layout(data)
        records = _parse_records(data, layout["records_start"])

        rec_count = struct.unpack_from("<I", data, layout["count_offset"])[0]
        post_ptr = struct.unpack_from("<I", data, layout["ptr_offset"])[0]

        return cls(
            path=path,
            raw=data,
            records=records,
            bef_path=layout["bef_path"],
            bef_offset=layout["bef_offset"],
            record_count_field=rec_count,
            record_count_offset=layout["count_offset"],
            post_records_ptr=post_ptr,
            post_records_ptr_offset=layout["ptr_offset"],
            records_start=layout["records_start"],
        )

    def find_records_by_type(self, type_id: int) -> list[PsxRecord]:
        return [r for r in self.records if r.type_id == type_id]

    def find_records_by_class_name(self, name: str) -> list[PsxRecord]:
        return [r for r in self.records if r.class_name == name]

    @property
    def player_spawn_offset(self) -> int:
        """Byte offset of the player-spawn (X,Y,Z) float triple in the header."""
        return self.records_start - PLAYER_SPAWN_REL

    def get_player_spawn(self) -> tuple[float, float, float] | None:
        """Read the current player spawn coordinate, or None if out of range."""
        off = self.player_spawn_offset
        if off < 0 or off + 12 > len(self.raw):
            return None
        return struct.unpack_from("<fff", self.raw, off)

    def _build_replacement_bytes(
        self,
        replacements: dict[int, "PsxRecord"],
        header_patches: dict[int, bytes] | None = None,
    ) -> "BuiltPsxBytes":
        """Assemble the output bytes in memory. Writes NOTHING to disk.

        Returns a `BuiltPsxBytes` named tuple:
          data        - the assembled bytearray (header + records + preserved tail)
          total_shift - sum of `old.size - new.size`, POSITIVE on shrink
          replaced    - number of records replaced
          tail_start  - index in `data` where the preserved tail begins

        Split out of `write_with_replacements` so verification can build and
        inspect the bytes without writing any file.
        """
        new_data = bytearray()

        sorted_records = sorted(self.records, key=lambda r: r.offset)
        prev_end = 0
        total_shift = 0
        replaced = 0

        for r in sorted_records:
            new_data.extend(self.raw[prev_end:r.offset])
            if r.offset in replacements:
                nr = replacements[r.offset]
                new_data.extend(nr.raw)
                total_shift += r.size - nr.size
                replaced += 1
            else:
                new_data.extend(self.raw[r.offset:r.offset + r.size])
            prev_end = r.offset + r.size

        # TAIL - preserved byte-for-byte. This is CRITICAL: the tail is the
        # MAJORITY of every PSX file (C_KING is 0xD16F0 bytes total of which
        # 0xCEB20 is tail), so computing the output size from the records alone
        # and dropping or overwriting this is the single easiest way to destroy
        # every file. There is no padding and no truncation to the input length:
        # the output is exactly header + records + tail.
        tail_start = len(new_data)
        new_data.extend(self.raw[prev_end:])

        ceiling = _size_ceiling_for(len(self.raw))
        if ceiling is not None and len(new_data) > ceiling:
            raise RuntimeError(
                f"New file would exceed the records-section size ceiling "
                f"({len(new_data)} vs {ceiling})"
            )

        # Post-records field is a SIZE (measured across 32 of 33 vanilla files),
        # and total_shift is positive on shrink, so subtracting it grows the
        # field on growth and shrinks it on shrink.
        new_post_ptr = self.post_records_ptr - total_shift
        struct.pack_into("<I", new_data, self.post_records_ptr_offset, new_post_ptr)

        # record_count_offset is deliberately left unwritten, and that is safe:
        # records are only ever RETYPED IN PLACE. `replacements` is keyed by
        # source offset, so there is at most one replacement per record; the loop
        # above iterates the EXISTING record list; records are never added and
        # never removed. The count therefore cannot change. If a future change
        # ever adds or removes records, this field becomes load-bearing and needs
        # a matching struct.pack_into here.

        # Apply raw header patches (e.g. randomized player-spawn coordinate).
        # These offsets live before the records section, which is copied
        # verbatim, so they map directly into new_data. Applied AFTER the
        # post-records fixup, so a header patch could in principle overwrite it
        # - existing behaviour, preserved deliberately.
        if header_patches:
            for off, payload in header_patches.items():
                if 0 <= off and off + len(payload) <= len(new_data):
                    new_data[off:off + len(payload)] = payload

        return BuiltPsxBytes(new_data, total_shift, replaced, tail_start)

    def write_with_replacements(
        self,
        replacements: dict[int, "PsxRecord"],
        out_path: Path | str,
        header_patches: dict[int, bytes] | None = None,
    ) -> dict:
        out_path = Path(out_path)
        built = self._build_replacement_bytes(replacements, header_patches)
        new_data = built.data
        out_path.write_bytes(bytes(new_data))
        return {
            "replaced": built.replaced,
            "total_shift": built.total_shift,
            "old_post_ptr": self.post_records_ptr,
            "new_post_ptr": self.post_records_ptr - built.total_shift,
            # ACTUAL output length (previously the input length).
            "file_size": len(new_data),
            # Positive means the records section GREW (total_shift is positive
            # on shrink).
            "records_growth": -built.total_shift,
            "tail_length": len(new_data) - built.tail_start,
        }


def make_record_with_template(
    template: PsxRecord,
    keep_position_from: PsxRecord,
    new_offset: int,
) -> PsxRecord:
    """Build a new record with template's type/data but keep position+ID from `keep_position_from`.

    Also translates any 0x0C "trigger volume" properties by the delta between the
    template's position and the new position. Without this fix, entities like
    Bone_Tower would have their detection range trigger at the wrong world coordinates.
    """
    raw = bytearray(template.raw)
    src_prop = RECORD_HEADER_SIZE
    raw[src_prop + 0x08:src_prop + 0x0C] = keep_position_from.raw[src_prop + 0x08:src_prop + 0x0C]
    raw[src_prop + 0x10:src_prop + 0x1C] = keep_position_from.raw[src_prop + 0x10:src_prop + 0x1C]
    raw[NAME_BUFFER_SIZE:RECORD_HEADER_SIZE] = keep_position_from.raw[NAME_BUFFER_SIZE:RECORD_HEADER_SIZE]

    # Translate trigger-volume properties (type 0x0C) by the position delta.
    # Property entry layout (32 bytes): [type:1][3 pad][idx:4][6 floats: cx,cy,cz,sx,sy,sz][2 pad]
    # The trigger center (cx,cy,cz) is in world coordinates; we offset it by
    # (new_pos - template_pos) so it stays at the same RELATIVE offset from the entity.
    dx = keep_position_from.pos_x - template.pos_x
    dy = keep_position_from.pos_y - template.pos_y
    dz = keep_position_from.pos_z - template.pos_z
    if dx or dy or dz:
        for k in range(template.prop_count):
            eo = RECORD_HEADER_SIZE + ENTITY_HEADER_SIZE + k * ENTRY_SIZE
            if eo + ENTRY_SIZE > len(raw):
                break
            type_b = raw[eo]
            if type_b == 0x0C:
                cx, cy, cz = struct.unpack_from("<3f", raw, eo + 8)
                struct.pack_into("<3f", raw, eo + 8, cx + dx, cy + dy, cz + dz)

    return PsxRecord(
        offset=new_offset,
        class_name=template.class_name,
        instance_name=keep_position_from.instance_name,
        type_id=template.type_id,
        instance_id=keep_position_from.instance_id,
        prop_count=template.prop_count,
        pos_x=keep_position_from.pos_x,
        pos_y=keep_position_from.pos_y,
        pos_z=keep_position_from.pos_z,
        raw=bytes(raw),
    )


def _read_string(data: bytes, offset: int, max_len: int) -> str:
    end = offset
    while end < offset + max_len and end < len(data) and data[end] != 0:
        end += 1
    return data[offset:end].decode("ascii", errors="replace")


def _find_bef_path(data: bytes) -> tuple[int, str]:
    """Locate the .bef path in the header by scanning."""
    bef_marker = b".bef"
    # Some maps (e.g. C_HUB) have extra metadata pushing the BEF buffer past 0x800.
    # Scan the first 0x1200 bytes which covers all known layouts.
    for off in range(0, min(0x1200, len(data) - 5)):
        if data[off:off+4].lower() == bef_marker:
            # Walk backwards to find start of path
            start = off
            while start > 0 and start > off - 256:
                c = data[start - 1]
                if c == 0:
                    break
                if not (0x20 <= c < 127):
                    break
                start -= 1
            try:
                path = data[start:off + 4].decode("ascii", errors="replace")
                return start, path
            except UnicodeDecodeError:
                continue
    return 0, ""


def _detect_header_layout(data: bytes) -> dict:
    """Detect header offsets from bef_path location."""
    bef_off, bef_path = _find_bef_path(data)
    if bef_off == 0:
        # Fallback: assume GRAVE-style
        return {
            "bef_offset": 0x430,
            "bef_path": "",
            "count_offset": 0x530,
            "ptr_offset": 0x534,
            "records_start": 0x570,
        }
    # The BEF path lives in a 256-byte buffer at bef_off.
    # After it: u32 count at +0x100, u32 ptr at +0x104, then 0x3C bytes manager,
    # then records start at bef_off + 0x140.
    layout = {
        "bef_offset": bef_off,
        "bef_path": bef_path,
        "count_offset": bef_off + 0x100,
        "ptr_offset": bef_off + 0x104,
        "records_start": bef_off + 0x140,
    }
    # Self-correct off-by-N detection. A few files (e.g. I_BOSS / U_BOSS) have a
    # stray printable byte immediately before the BEF-path buffer, so the
    # backward path scan starts one byte early and every derived offset is
    # shifted. The records section MUST begin on a class-name buffer; if it
    # doesn't, nudge all derived offsets to the nearest valid one. Files that
    # already parse correctly land on a class buffer, so this is a no-op for
    # them.
    rs = layout["records_start"]
    if not _is_class_buffer(data, rs):
        for delta in (1, -1, 2, -2, 3, -3, 4, -4):
            if _is_class_buffer(data, rs + delta):
                layout["bef_offset"] += delta
                layout["count_offset"] += delta
                layout["ptr_offset"] += delta
                layout["records_start"] += delta
                break
    return layout


def _is_class_buffer(data: bytes, offset: int) -> bool:
    if offset >= len(data):
        return False
    b = data[offset]
    if not (0x41 <= b <= 0x5A):
        return False
    end = offset
    while end < offset + 64 and end < len(data) and 32 <= data[end] < 127:
        end += 1
    if end - offset < 3 or end >= len(data) or data[end] != 0:
        return False
    cd = 0
    for j in range(end + 1, min(offset + NAME_BUFFER_SIZE, len(data))):
        if data[j] != 0xCD:
            break
        cd += 1
    return cd >= 200


def _parse_records(data: bytes, records_start: int) -> list[PsxRecord]:
    records = []
    i = records_start
    while i < len(data) - RECORD_HEADER_SIZE - ENTITY_HEADER_SIZE:
        if not _is_class_buffer(data, i):
            i += 4
            continue

        end = i
        while end < i + 64 and 32 <= data[end] < 127:
            end += 1
        class_name = data[i:end].decode("ascii")
        instance_name = _read_string(data, i + NAME_BUFFER_SIZE, 256)

        prop_start = i + RECORD_HEADER_SIZE
        type_id = data[prop_start + 0x04]
        instance_id = struct.unpack_from("<I", data, prop_start + 0x08)[0]
        prop_count = struct.unpack_from("<I", data, prop_start + 0x0C)[0]
        pos_x, pos_y, pos_z = struct.unpack_from("<fff", data, prop_start + 0x10)

        formula_size = compute_record_size(prop_count)
        size = formula_size

        if not _is_class_buffer(data, i + size):
            test_i = i + max(formula_size, 0x100)
            found = None
            # Use a step of 4 (since records are 4-aligned) but limit how far we'll search
            search_limit = i + 0x10000  # don't search more than 64KB ahead
            while test_i < min(len(data) - 0x100, search_limit):
                if _is_class_buffer(data, test_i):
                    found = test_i
                    break
                test_i += 4
            if found is not None:
                size = found - i

        raw = data[i:i + size]
        records.append(PsxRecord(
            offset=i,
            class_name=class_name,
            instance_name=instance_name,
            type_id=type_id,
            instance_id=instance_id,
            prop_count=prop_count,
            pos_x=pos_x,
            pos_y=pos_y,
            pos_z=pos_z,
            raw=raw,
        ))
        i += size

    return records


if __name__ == "__main__":
    import sys
    from collections import Counter
    if len(sys.argv) < 2:
        print("Usage: python -m randomizer.psx <file.psx>")
        sys.exit(1)
    psx = PsxFile.parse(sys.argv[1])
    print(f"{psx.path.name}: {len(psx.records)} records, BEF '{psx.bef_path}'")
    types = Counter(r.type_id for r in psx.records)
    for tid, count in sorted(types.items()):
        sample = psx.find_records_by_type(tid)[0]
        print(f"  0x{tid:02X}  {count:3} x {sample.class_name} (size 0x{sample.size:X})")
