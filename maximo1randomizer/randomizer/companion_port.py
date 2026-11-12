"""Companion-entity investigation and porting support.

Task 1.1 scope only: the CANDIDATE A probe of the companion investigation.

Candidate A asks a cheap question: is the object an enemy owns -- Bomb_Skeleton
``0x49``'s bomb, Dark_Knight ``0x53``'s purple wall -- a SEPARATE BEF entry with
its own type id? If it is, it must be a type id CASTLE declares and GRAVE does
not, because if GRAVE already declared the bomb the bomb would already appear.

CANDIDATE A ALONE GIVES A CANDIDATE SET, NOT AN EDGE. Knowing which type ids
exist in Castle and not in Grave does not prove that any of them is spawned by
``0x49`` or ``0x53``. The edge is what Task 1.3's blob scan tests for.

Everything here is READ-ONLY: it parses ``.BEF`` files (a few hundred KB each,
so several may be held at once) and writes nothing, anywhere. No ``.PRS`` is
opened by this module at all.

This module deliberately does NOT import ``cross_world``: the port session is
passed in by the caller so the dependency stays acyclic.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

import struct

from . import bef
from . import befscript as _befscript
from . import catalog

#: `SeqPlay`'s script-API slot. A clip selected by SLOT NUMBER carries no
#: material ref, so the slot range is the only evidence of the block's extent.
_SEQPLAY_API_INDEX = 0x29


def missed_local_refs(blob: bytes) -> list:
    """LOCAL cat-1/cat-2 resource refs that ``bef.parse_material_refs`` misses.

    THE GAP, MEASURED. ``parse_material_refs`` reads refs only from the START of
    each record body -- it walks ``while blob[p] in MATERIAL_REC_TYPES`` from
    ``body`` and stops at the first byte that is not ``0x04``/``0x05``. Records
    whose body does NOT begin with a ref therefore contribute NOTHING, even
    though a ref sits deeper inside them encoded identically.

    Goat_Devil ``0x13`` (UNDER.BEF) is the case that exposed it: three cat-2 refs
    at ``0x12E`` (anim 78), ``0x162`` (anim 78) and ``0x1F8`` (anim 89) are
    invisible to the parser, so their indices were never remapped and in the
    target world they addressed whatever native animation happened to occupy 78
    and 89. That is the "his attack animation is wrong/missing" report.

    The signature is deliberately NARROW so this cannot invent refs:
    ``[0x04|0x05][index u16][0x00][flag]`` with the flag REQUIRED to correlate
    with the record type (``0x05``->1 mesh, ``0x04``->2 anim). Across UNDER, ICE
    and SWAMP it yields exactly FIVE extra refs in THREE entities -- UNDER 0x13
    (three), ICE 0x7C (one), SWAMP 0x0B (one) -- which is a parser gap, not a
    false-positive spray.

    `bef.parse_material_refs` is NOT changed. It is used by the item randomizer,
    `cross_world` and `boss_inject`, and `anim_port` carries positional
    assumptions keyed to its ordering. These refs are returned SEPARATELY and
    merged only where remapping happens.
    """
    try:
        start, end = bef.material_section_bounds(blob)
    except Exception:
        return []
    try:
        known = {r.idx_offset for r in bef.parse_material_refs(blob)}
    except Exception:
        return []
    out = []
    for pos in range(start, max(start, end - 4)):
        rec_type = blob[pos]
        if rec_type not in (0x04, 0x05):
            continue
        if pos + 1 in known:
            continue
        raw = struct.unpack_from("<I", blob, pos + 1)[0]
        flag = (raw >> 24) & 0xFF
        index = raw & bef.MATERIAL_INDEX_MASK
        if index > 0xFFFF:
            continue
        if (rec_type == 0x05) != (flag == 1):
            continue
        if (rec_type == 0x04) != (flag == 2):
            continue
        out.append(bef.MaterialRef(record=-1, slot=0, rec_type=rec_type,
                                   idx_offset=pos + 1, flag=flag, index=index))
    return out
from .bef import (BefFile, MATERIAL_CATEGORY_ANIM, MATERIAL_CATEGORY_MESH,
                  MaterialSectionError)

#: The five world ``.BEF`` files, in the order the report tabulates them.
INVESTIGATION_WORLDS: tuple[str, ...] = ("GRAVE", "CASTLE", "SWAMP", "ICE",
                                         "UNDER")

#: The two enemies whose missing companions started this feature.
INVESTIGATION_ROOTS: tuple[int, ...] = (0x49, 0x53)

_LOG_PREFIX = "[cross-world]"


def world_only_type_ids(source_bef: BefFile, target_bef: BefFile) -> set[int]:
    """Type ids the source world defines and the target world does not.

    A pure set difference over ``BefFile.type_ids()``. The result is a subset of
    the source's ids and disjoint from the target's, which is exactly the
    property the investigation relies on when it calls the result a candidate
    set.
    """
    return source_bef.type_ids() - target_bef.type_ids()


def name_for_type(type_id: int) -> Optional[str]:
    """Best-effort human name for a BEF type id, or None when unknown.

    ``catalog`` names only the walking-melee pool, so most ids -- including any
    companion entity -- will not resolve. That is information, not a failure: an
    unnamed id is consistent with a non-enemy object such as a bomb or a wall.
    """
    getter = getattr(catalog, "name_for_type", None)
    if callable(getter):          # future-proofing if catalog grows one
        return getter(type_id)
    return catalog.WALKING_MELEE_POOL.get(type_id)


@dataclass(frozen=True)
class EntityRefReport:
    """The Candidate A facts for one BEF entry. Purely descriptive."""

    type_id: int
    name: Optional[str]
    blob_len: int
    local_mesh: tuple[int, ...]      # LOCAL CATEGORY-1 indices, in blob order
    local_anim: tuple[int, ...]      # LOCAL CATEGORY-2 indices, in blob order
    global_mesh: tuple[int, ...]
    global_anim: tuple[int, ...]
    tex_offsets: tuple[int, ...]     # from the deprecated find_texture_indices
    error: Optional[str] = None      # set when the material walk refused

    @property
    def distinct_local_mesh(self) -> int:
        return len(set(self.local_mesh))

    def describe(self) -> str:
        name = self.name or "-"
        if self.error:
            return f"0x{self.type_id:02X} {name:<18} len={self.blob_len:<7} " \
                   f"MATERIAL WALK REFUSED: {self.error}"
        return (f"0x{self.type_id:02X} {name:<18} len={self.blob_len:<7} "
                f"LOCAL cat1={_fmt(self.local_mesh)} "
                f"cat2={_fmt(self.local_anim)} | "
                f"GLOBAL cat1={_fmt(self.global_mesh)} "
                f"cat2={_fmt(self.global_anim)} | "
                f"tex_offsets={_fmt(self.tex_offsets)}")


def _fmt(values: Sequence[int]) -> str:
    return "[" + ",".join(str(v) for v in values) + "]"


def describe_entity(source_bef: BefFile, type_id: int) -> EntityRefReport:
    """Tabulate one entry's blob length, material refs and texture offsets.

    Read-only and non-raising: a blob whose material section will not walk is
    reported with ``error`` set rather than aborting the whole probe, because a
    refusing blob is itself a measurement.
    """
    entry = source_bef.get_entry(type_id)
    if entry is None:
        return EntityRefReport(type_id=type_id, name=name_for_type(type_id),
                               blob_len=0, local_mesh=(), local_anim=(),
                               global_mesh=(), global_anim=(), tex_offsets=(),
                               error="type id absent from this .BEF")
    blob = entry.data
    try:
        refs = bef.parse_material_refs(blob)
    except MaterialSectionError as exc:
        return EntityRefReport(type_id=type_id, name=name_for_type(type_id),
                               blob_len=len(blob), local_mesh=(),
                               local_anim=(), global_mesh=(), global_anim=(),
                               tex_offsets=(), error=str(exc))

    def pick(category: int, is_global: bool) -> tuple[int, ...]:
        return tuple(r.index for r in refs
                     if r.category == category and r.is_global is is_global)

    # find_texture_indices is DEPRECATED in bef.py and returns blob OFFSETS of
    # a byte pattern, not texture indices. Reported verbatim for continuity with
    # the design's wording; parse_material_refs above is the load-bearing part.
    tex = tuple(bef.find_texture_indices(blob))
    return EntityRefReport(
        type_id=type_id,
        name=name_for_type(type_id),
        blob_len=len(blob),
        local_mesh=pick(MATERIAL_CATEGORY_MESH, False),
        local_anim=pick(MATERIAL_CATEGORY_ANIM, False),
        global_mesh=pick(MATERIAL_CATEGORY_MESH, True),
        global_anim=pick(MATERIAL_CATEGORY_ANIM, True),
        tex_offsets=tex,
    )


def load_world_befs(game_files: Path,
                    worlds: Sequence[str] = INVESTIGATION_WORLDS,
                    ) -> dict[str, BefFile]:
    """Parse ``<world>.BEF`` for each world. Read-only; nothing is written."""
    out: dict[str, BefFile] = {}
    for world in worlds:
        path = Path(game_files) / f"{world}.BEF"
        out[world] = BefFile.parse(path)
    return out


def candidate_a_report(game_files: Path,
                       target_world: str = "GRAVE",
                       source_worlds: Sequence[str] = ("CASTLE", "SWAMP"),
                       ) -> str:
    """The Candidate A probe, rendered as text. Read-only, returns a string.

    Tabulates ``type_ids()`` for all five worlds, then for every source-only id
    (present in the source world, absent from the target) reports the id, its
    catalog name if any, the blob length, the LOCAL and GLOBAL CATEGORY-1 /
    CATEGORY-2 material refs, and the deprecated ``find_texture_indices``
    offsets.

    The caller prints this. Nothing is written to disk by this function or by
    anything it calls.
    """
    befs = load_world_befs(Path(game_files))
    lines: list[str] = [f"{_LOG_PREFIX} Candidate A probe (read-only)", ""]

    lines.append("Per-world type-id counts:")
    for world in INVESTIGATION_WORLDS:
        ids = befs[world].type_ids()
        lines.append(f"  {world:<7} entries={len(befs[world].entries):<4} "
                     f"distinct type ids={len(ids)}")
    lines.append("")

    for root in INVESTIGATION_ROOTS:
        rep = describe_entity(befs["CASTLE"], root)
        lines.append(f"Root enemy in CASTLE: {rep.describe()}")
    lines.append("")

    target = befs[target_world]
    for source in source_worlds:
        only = sorted(world_only_type_ids(befs[source], target))
        lines.append(f"{source}-only type ids vs {target_world}: "
                     f"{len(only)} -> "
                     + ", ".join(f"0x{i:02X}" for i in only))
        for type_id in only:
            lines.append("  " + describe_entity(befs[source], type_id)
                         .describe())
        lines.append("")

    lines.append("Candidate A yields a CANDIDATE SET, not an edge: membership "
                 "here does not prove any id is spawned by 0x49 or 0x53.")
    return "\n".join(lines)


# ============================================================================
# CANDIDATE B (task 1.3) -- is the edge a FIELD inside the enemy's own blob?
#
# Candidate A can only produce a candidate set. Candidate B asks the question
# that can actually NAME the edge: does the enemy's own blob contain a field, or
# a script operand, whose VALUE IS a companion's BEF type id?
#
# The scan is deliberately dumb. It decodes u8, u16 and u32 little-endian at
# every offset outside the material section -- which is the one region already
# fully interpreted, so a hit there would be a known mesh/anim index rather than
# a spawn reference -- and reports every offset whose value is a member of the
# candidate id set. It does NOT decode script opcodes: instruction lengths and
# operand encodings are the `bef-script-vm-disassembler` feature's job, and
# guessing them here would manufacture confidence the data does not support.
#
# ON `in_script`: NO SCRIPT-SECTION BOUNDARY IS ESTABLISHED ANYWHERE IN THIS
# CODEBASE. `bef.py` interprets the material section and nothing else; there is
# no header field known to hold a script offset, and inventing a boundary (say,
# "everything after the material section end") would silently convert a guess
# into a reported fact. So `script_start` is an EXPLICIT caller-supplied
# argument, and when it is None -- which is the current state of knowledge --
# every hit is reported with `in_script=False` and that flag carries NO claim
# either way. Head/tail clustering is reported separately, as a raw offset
# distribution, because that is a measurement rather than an interpretation.
# ============================================================================


@dataclass(frozen=True)
class FieldHit:
    """One offset in a blob whose decoded value is a candidate BEF type id.

    ``offset`` is relative to the blob start. ``width`` is 1, 2 or 4 and
    ``value`` is the little-endian decode at that width. ``in_script`` is True
    only when the caller supplied a script-section start and the offset is at or
    past it; with no boundary known it is False and means "unknown", not "not in
    the script".
    """

    offset: int
    width: int
    value: int
    in_script: bool = False

    def describe(self) -> str:
        tail = " SCRIPT" if self.in_script else ""
        return (f"@0x{self.offset:04X} u{self.width * 8:<2} "
                f"= 0x{self.value:02X}{tail}")


#: The widths the scan decodes, narrowest first.
FIELD_WIDTHS: tuple[int, ...] = (1, 2, 4)


def scan_blob_for_type_ids(blob: bytes,
                           ids,
                           *,
                           script_start: Optional[int] = None,
                           ) -> list[FieldHit]:
    """Every field outside the material section whose value is a known type id.

    Pure: no I/O, no mutation, no dependence on anything but its arguments.

    Offsets inside ``[mat_lo, mat_hi)`` as returned by
    ``bef.material_section_bounds`` are EXCLUDED, because that region is already
    fully interpreted -- a byte there holding a type-id-looking value is a mesh
    or anim index, not a spawn reference. A field is excluded if ANY of its bytes
    fall in that range, so a u32 straddling the boundary is not reported.

    Raises ``bef.MaterialSectionError`` when the bounds cannot be read, rather
    than falling back to scanning the whole blob: without the bounds the caller
    cannot know which hits are already-explained material indices, and a scan
    whose exclusions are unknown is not evidence.

    Results are ordered by offset, then by width.
    """
    wanted = frozenset(ids)
    mat_lo, mat_hi = bef.material_section_bounds(blob)
    if not wanted:
        return []
    hits: list[FieldHit] = []
    size = len(blob)
    for offset in range(size):
        for width in FIELD_WIDTHS:
            end = offset + width
            if end > size:
                continue
            if offset < mat_hi and end > mat_lo:   # any overlap at all
                continue
            if width == 1:
                value = blob[offset]
            elif width == 2:
                value = int.from_bytes(blob[offset:end], "little")
            else:
                value = int.from_bytes(blob[offset:end], "little")
            if value in wanted:
                hits.append(FieldHit(
                    offset=offset,
                    width=width,
                    value=value,
                    in_script=(script_start is not None
                               and offset >= script_start),
                ))
    return hits

# ============================================================================
# CANDIDATE C (task 1.5) -- is the edge in the LEVEL files rather than the BEF?
#
# Candidate A produced a candidate set and nothing more. Candidate B came back a
# rigorous NEGATIVE: the only real field found in an enemy's own blob was
# `blob+0x10` u32 = the entry's OWN type id (80/80 across CASTLE), which is a
# self id, not a spawn edge. So this probe is load-bearing: if it is also
# negative, the verdict is CANDIDATE_C-as-bytecode or UNRESOLVED, and tasks 3-9
# of this spec are the wrong next step -- which both the design and tasks.md say
# in as many words.
#
# What the probe asks: does any CASTLE `.PSX` record, or any 0x20-byte property
# entry inside one, hold the byte value 0x49 (or 0x53) TOGETHER WITH a second
# CASTLE BEF type id? A level-authored spawn parameter -- "this Bomb_Skeleton
# throws entity Y" -- would look exactly like that.
#
# MEASURED FACTS ABOUT `psx.py` THIS BUILDS ON (read from the source, not
# assumed):
#   * `PsxFile.parse(path)` takes a path (str or Path), reads the whole file and
#     returns records; there is no bytes-level entry point.
#   * A record is `raw` = [0x100 class-name buffer][0x100 instance-name buffer]
#     [0x28 entity header][prop_count * 0x20 property entries]. Name buffers are
#     ASCII + 0xCD padding, so they are TEXT and are excluded from every byte
#     scan here: a 0xCD or an ASCII byte that happens to equal a type id is not a
#     reference.
#   * Records DO carry a type id: `PsxRecord.type_id` = raw[0x204], i.e. the byte
#     at +0x04 of the entity header. `find_records_by_type` exists, so "is 0x49
#     placed in this map at all" is directly answerable.
#   * `prop_count` is raw[0x20C] u32 and `compute_record_size` = 0x228 +
#     count*0x20, but `_parse_records` RESYNCS to the next class buffer when the
#     formula lands wrong, so `len(raw)` can exceed the formula size. Entry
#     enumeration here is therefore clamped to what `raw` actually holds.
#   * CASTLE `.PSX` filenames: the design guessed C_HUB, C_INTRO, C_KING,
#     C_QUEEN, C_SUB1, C_SUB2. VERIFIED against game_files/ -- those six exist
#     and are the only `C_*.PSX` files. The guess was right; `castle_psx_paths`
#     globs rather than hardcodes so a wrong guess could not survive.
#
# Read-only. `write_with_replacements` is never called from this module. No
# bytecode is decoded: if the conclusion is that the edge lives in compiled
# script, this module says so and stops.
# ============================================================================

from . import psx as _psx  # noqa: E402  (kept next to the Candidate C section)

#: Every CASTLE level file, discovered by glob rather than hardcoded.
CASTLE_PSX_GLOB = "C_*.PSX"

#: The Candidate A result, MEASURED in task 1.1: type ids CASTLE.BEF declares and
#: GRAVE.BEF does not. Recorded here so the Candidate C probe can intersect it
#: with the set of ids actually PLACED by a level, which is the one narrowing
#: this probe can honestly perform.
CASTLE_ONLY_TYPE_IDS: tuple[int, ...] = (
    0x06, 0x08, 0x09, 0x0B, 0x15, 0x41, 0x44, 0x49, 0x4A, 0x4C, 0x4F, 0x50,
    0x53, 0x58, 0x5E, 0x63, 0x66, 0x71, 0x8C, 0xAA, 0xAD, 0xBC, 0xCB, 0xEA,
    0xEB, 0xF1, 0xF2,
)

#: A CASTLE enemy with no known companion, used as a control: a field that also
#: shows up in the control's placements is generic level data, not a spawn edge.
#: Same control Candidate B used, so the two probes stay comparable.
CONTROL_ENEMY_TYPE_ID = 0x54

#: Offset of the type-id byte inside a record, and the entity-header span.
RECORD_TYPE_ID_OFFSET = _psx.RECORD_HEADER_SIZE + 0x04
_ENTITY_HEADER_START = _psx.RECORD_HEADER_SIZE
_ENTRIES_START = _psx.RECORD_HEADER_SIZE + _psx.ENTITY_HEADER_SIZE


def castle_psx_paths(game_files: Path) -> list[Path]:
    """The CASTLE level files, sorted. Read-only; opens nothing."""
    return sorted(Path(game_files).glob(CASTLE_PSX_GLOB))


def byte_ids_present(chunk: bytes, ids) -> dict[int, tuple[int, ...]]:
    """Map each wanted id to the offsets in ``chunk`` holding that byte value.

    Pure. Byte-width only: PSX records store a type id as ONE byte
    (``raw[0x204]``), so a u16/u32 decode would invent a width the format does
    not use here.
    """
    wanted = frozenset(ids)
    out: dict[int, list[int]] = {}
    for offset, value in enumerate(chunk):
        if value in wanted:
            out.setdefault(value, []).append(offset)
    return {k: tuple(v) for k, v in sorted(out.items())}


@dataclass(frozen=True)
class CoReference:
    """One record-or-entry scope holding a root id AND a second type id.

    ``scope`` is ``"entity_header"`` or ``"entry[k]"``. ``root_offsets`` and
    ``others`` are offsets RELATIVE TO THE SCOPE, so they are directly checkable
    against a hex dump of that scope.
    """

    file_name: str
    record_offset: int
    class_name: str
    record_type_id: int
    scope: str
    root_id: int
    root_offsets: tuple[int, ...]
    others: tuple[tuple[int, tuple[int, ...]], ...]   # (id, offsets)

    def describe(self) -> str:
        others = " ".join(
            f"0x{i:02X}@{'+'.join(hex(o) for o in offs)}" for i, offs in self.others
        )
        return (f"{self.file_name} rec@0x{self.record_offset:X} "
                f"{self.class_name} (type 0x{self.record_type_id:02X}) "
                f"{self.scope}: root 0x{self.root_id:02X}"
                f"@{'+'.join(hex(o) for o in self.root_offsets)} "
                f"with {others}")


def record_scopes(record: "_psx.PsxRecord") -> list[tuple[str, bytes]]:
    """The scannable byte scopes of a record: entity header, then each entry.

    The two 0x100-byte NAME buffers are deliberately excluded (they are ASCII +
    0xCD padding, i.e. text). Entry enumeration is clamped to ``len(raw)``
    because `_parse_records` may resync a record to a size that disagrees with
    ``prop_count``.
    """
    raw = record.raw
    scopes: list[tuple[str, bytes]] = []
    if len(raw) >= _ENTRIES_START:
        scopes.append(("entity_header", raw[_ENTITY_HEADER_START:_ENTRIES_START]))
    for k in range(record.prop_count):
        lo = _ENTRIES_START + k * _psx.ENTRY_SIZE
        hi = lo + _psx.ENTRY_SIZE
        if hi > len(raw):
            break
        scopes.append((f"entry[{k}]", raw[lo:hi]))
    return scopes


def find_co_references(record: "_psx.PsxRecord",
                       roots,
                       ids,
                       *,
                       file_name: str = "") -> list[CoReference]:
    """Scopes of one record that hold a root id and at least one other type id.

    Pure: takes a record and two id sets, returns descriptions. A scope whose
    only root hit is the record's OWN type-id byte is NOT reported, for the same
    reason Candidate B discounted ``blob+0x10``: a self id is not a spawn edge.
    """
    root_set = frozenset(roots)
    id_set = frozenset(ids)
    out: list[CoReference] = []
    for scope, chunk in record_scopes(record):
        present = byte_ids_present(chunk, id_set | root_set)
        for root in sorted(root_set):
            offsets = present.get(root)
            if not offsets:
                continue
            if scope == "entity_header":
                offsets = tuple(o for o in offsets
                                if _ENTITY_HEADER_START + o != RECORD_TYPE_ID_OFFSET)
                if not offsets:
                    continue
            others = tuple((i, offs) for i, offs in present.items()
                           if i != root and i in id_set)
            if not others:
                continue
            out.append(CoReference(
                file_name=file_name,
                record_offset=record.offset,
                class_name=record.class_name,
                record_type_id=record.type_id,
                scope=scope,
                root_id=root,
                root_offsets=tuple(offsets),
                others=others,
            ))
    return out


#: The entity-header bytes that the format DEFINES as discrete integer fields:
#: +0x04 type id, +0x08..0x0B instance id, +0x0C..0x0F property count. From
#: +0x10 onward the header is the (X, Y, Z) position float triple and more
#: floats, so a byte there equalling a type id is float mantissa, not a
#: reference. Co-references are therefore reported in two buckets: those whose
#: root byte lands in an INTEGER field, and those that land in float payload.
INTEGER_FIELD_SPAN = range(0x00, 0x10)


def co_reference_base_rate(psx_file: "_psx.PsxFile", ids) -> tuple[int, int]:
    """(bytes scanned, bytes whose value is one of ``ids``) across all scopes.

    This is the number the co-reference count has to beat. With 80 of 256 byte
    values being CASTLE type ids, roughly a third of ALL bytes match something,
    so a 0x20-byte scope containing "a root and a second type id" is the DEFAULT
    outcome of scanning float data, not a discovery.
    """
    wanted = frozenset(ids)
    total = hits = 0
    for record in psx_file.records:
        for _scope, chunk in record_scopes(record):
            total += len(chunk)
            hits += sum(1 for b in chunk if b in wanted)
    return total, hits


def _placement_dump(records, label: str) -> list[str]:
    """Hex dump of the entity header of each placement, plus a constancy note."""
    lines: list[str] = []
    if not records:
        lines.append(f"  {label}: NOT PLACED in this file")
        return lines
    headers = [r.raw[_ENTITY_HEADER_START:_ENTRIES_START] for r in records]
    for r, h in zip(records, headers):
        lines.append(f"  {label} rec@0x{r.offset:X} {r.class_name} "
                     f"props={r.prop_count} header={h.hex()}")
    constant = [i for i in range(min(len(h) for h in headers))
                if len({h[i] for h in headers}) == 1]
    lines.append(f"  {label}: {len(records)} placement(s); entity-header bytes "
                 f"constant across all: {[hex(i) for i in constant]}")
    return lines


def candidate_c_report(game_files: Path,
                       roots: Sequence[int] = INVESTIGATION_ROOTS,
                       candidate_ids: Optional[Sequence[int]] = None,
                       ) -> str:
    """The Candidate C probe, rendered as text. Read-only, returns a string.

    ``candidate_ids`` defaults to ``CASTLE.BEF``'s own ``type_ids()`` so the
    probe needs no hardcoded candidate list. Only one `.PSX` is held at a time
    (they are small, and each is dropped before the next is parsed); no `.PRS` is
    opened at all.
    """
    game_files = Path(game_files)
    if candidate_ids is None:
        castle = BefFile.parse(game_files / "CASTLE.BEF")
        candidate_ids = sorted(castle.type_ids())
    ids = frozenset(candidate_ids)
    root_set = tuple(roots)

    paths = castle_psx_paths(game_files)
    lines = [f"{_LOG_PREFIX} Candidate C probe (read-only, no bytecode decoded)",
             f"CASTLE .PSX files found by glob {CASTLE_PSX_GLOB!r}: "
             + ", ".join(p.name for p in paths),
             f"CASTLE BEF type ids in play: {len(ids)}", ""]

    total_corefs = 0
    placed_ids: set[int] = set()
    for path in paths:
        psx_file = _psx.PsxFile.parse(path)
        lines.append(f"{path.name}: {len(psx_file.records)} records, "
                     f"records_start=0x{psx_file.records_start:X}, "
                     f"BEF '{psx_file.bef_path}'")
        placed_ids.update(r.type_id for r in psx_file.records)

        # (1) the task's question: co-reference of a root and a second type id.
        corefs: list[CoReference] = []
        for record in psx_file.records:
            corefs.extend(find_co_references(record, root_set, ids,
                                             file_name=path.name))
        total_corefs += len(corefs)
        in_int = [c for c in corefs
                  if any(o in INTEGER_FIELD_SPAN for o in c.root_offsets)]
        lines.append(f"  co-references (root + second CASTLE type id): "
                     f"{len(corefs)} total, {len(in_int)} with the root byte in "
                     f"an INTEGER field (offset < 0x10), "
                     f"{len(corefs) - len(in_int)} in FLOAT payload")
        for cr in corefs[:20]:
            lines.append("    " + cr.describe())
        if len(corefs) > 20:
            lines.append(f"    ... {len(corefs) - 20} more")

        # (1b) CONTROLS. Repeat the identical scan with the control enemy as the
        # root, and with a byte value that is NOT a CASTLE type id at all. If
        # those score like the real roots, the co-reference count measures the
        # base rate of byte collisions and nothing else.
        non_id = next((v for v in range(1, 256) if v not in ids), None)
        for label, probe in (("control 0x%02X" % CONTROL_ENEMY_TYPE_ID,
                              CONTROL_ENEMY_TYPE_ID),
                             ("non-type-id 0x%02X" % (non_id or 0), non_id)):
            if probe is None:
                continue
            n = sum(len(find_co_references(r, (probe,), ids))
                    for r in psx_file.records)
            lines.append(f"  same scan with root = {label}: {n}")
        scanned, matching = co_reference_base_rate(psx_file, ids)
        lines.append(f"  base rate: {matching}/{scanned} scanned bytes "
                     f"({100.0 * matching / max(scanned, 1):.1f}%) already equal "
                     f"SOME CASTLE type id")

        # (2) are the roots placed here at all, and how do they compare with the
        #     control enemy?
        for tid in root_set + (CONTROL_ENEMY_TYPE_ID,):
            lines.extend(_placement_dump(psx_file.find_records_by_type(tid),
                                         f"0x{tid:02X}"))
        lines.append("")
        del psx_file        # one .PSX in memory at a time

    lines.append(f"Distinct type ids PLACED across CASTLE .PSX records: "
                 f"{len(placed_ids)} -> "
                 + ", ".join(f"0x{i:02X}" for i in sorted(placed_ids)))
    absent = sorted(i for i in ids if i not in placed_ids)
    lines.append(f"CASTLE BEF type ids NEVER placed by any CASTLE level: "
                 f"{len(absent)} -> "
                 + ", ".join(f"0x{i:02X}" for i in absent))
    never_placed_candidates = tuple(i for i in CASTLE_ONLY_TYPE_IDS
                                    if i not in placed_ids)
    lines.append(f"Candidate A's {len(CASTLE_ONLY_TYPE_IDS)} CASTLE-only ids that "
                 f"NO CASTLE level ever places: "
                 f"{len(never_placed_candidates)} -> "
                 + ", ".join(f"0x{i:02X}" for i in never_placed_candidates))
    lines.append("An entity a level never places must be instantiated at "
                 "RUNTIME, which is what a companion is. That narrows the "
                 "candidate pool; it still does not name an edge.")
    lines.append("")
    lines.append(f"TOTAL co-references across all files: {total_corefs}")
    lines.append("A companion spawned at runtime would be ABSENT from every "
                 "map's record set; a level-placed one would appear above. "
                 "Neither outcome decodes bytecode, and this probe does not.")
    return "\n".join(lines)


# ============================================================================
# VERDICT (task 1.6) -- collapse the three probes into exactly one outcome.
#
# THE SUBTLETY THE DESIGN'S SKETCH DOES NOT CAPTURE. The design writes
# `if b_hits: return "CANDIDATE_B"` over the RAW output of
# `scan_blob_for_type_ids`, and over the RAW co-reference list from
# `find_co_references`. Both of those raw lists are NON-EMPTY for the real game
# files, and both were shown by CONTROLS to be pure noise:
#
#   * Candidate B: 0x49's blob yields 754 u8 / 94 u16 / 7 u32 raw hits, but a
#     control enemy with NO known companion (0x54) yields 1152 / 91 / 8, and the
#     u8 counts fall BELOW uniform chance because 80 of 256 byte values are
#     CASTLE type ids. The only survivor of cross-checking was blob+0x10 u32 =
#     the entry's OWN type id (80/80) -- a SELF id, not a spawn edge.
#   * Candidate C: 74 raw co-references across the six CASTLE levels, but the
#     control enemy scores 46 and a byte value that is NOT a type id at all
#     (0x01) scores 1,224 -- an order of magnitude MORE. 63 of the 74 have the
#     root byte at offset >= 0x10, i.e. inside position-float mantissa.
#
# So feeding the raw lists in returns CANDIDATE_B, which would be FALSE. This
# function therefore takes EDGE-BEARING evidence only, and says so in its
# signature names and its docstring, so that nobody later wires the raw scans
# straight in. Filtering is the CALLER's job because only the caller knows the
# controls it ran.
# ============================================================================

#: The FIVE permitted verdicts. Requirement 1.6 wants exactly one of these.
#:
#: ``CANDIDATE_D_BYTECODE`` was ADDED after the fact, and the addition is not a
#: convenience. It means: THE EDGE WAS RECOVERED BY DECODING THE Script_Section
#: WITH THE VERIFIED Opcode_Map AND TRACING THE API CALLING CONVENTION, and is
#: identified by opcode, call-site offset and API slot rather than by byte-value
#: coincidence.
#:
#: WHY IT IS NOT ``CANDIDATE_B``. Requirement 1.4 defines Candidate B as a
#: byte-value FIELD SCAN over the blob, and task 1.3 states in as many words "do
#: NOT decode opcodes". That method FAILED under controls: a no-companion control
#: enemy (0x54) produced the same hit rate, the u8 counts fell BELOW uniform
#: chance because 80 of 256 byte values are CASTLE type ids, and the only
#: survivor of cross-checking was ``blob+0x10`` u32 = the entry's OWN type id
#: (80/80 across CASTLE) -- a self id, not a spawn edge. Returning
#: ``CANDIDATE_B`` for a bytecode-recovered edge would credit a method that did
#: not work, and would make the notes unreadable to anyone auditing which
#: technique actually paid off.
VERDICTS: tuple[str, ...] = ("CANDIDATE_A", "CANDIDATE_B", "CANDIDATE_C",
                             "CANDIDATE_D_BYTECODE", "UNRESOLVED")


def decide_verdict(a_named_candidates, b_edge_hits, c_edge_coreferences,
                   d_bytecode_edges=()) -> str:
    """Collapse the three probes into exactly one member of ``VERDICTS``.

    ALL THREE ARGUMENTS MUST BE NOISE-FILTERED, EDGE-BEARING EVIDENCE -- NOT the
    raw probe output. Specifically:

    ``a_named_candidates``
        NOT the raw CASTLE-only set. The design is explicit: "Candidate A is
        CONFIRMED only if a CASTLE-only type id is also NAMED from 0x49's or
        0x53's data" -- the raw difference is a pool, and a pool is not an edge.
        So pass only those CASTLE-only ids that some measurement actually tied to
        a root enemy. Passing the raw 27-id (or narrowed 10-id) pool here yields
        a FALSE ``CANDIDATE_A``.
    ``b_edge_hits``
        ``FieldHit``s from ``scan_blob_for_type_ids`` that SURVIVED control
        comparison and name a DIFFERENT entity. Pass an empty sequence when the
        surviving hits are self ids (like ``blob+0x10``) or when a
        no-companion control enemy produces the same hit rate. Passing the raw
        scan output here yields a FALSE ``CANDIDATE_B``.
    ``c_edge_coreferences``
        ``CoReference``s that survived the same treatment: root byte in a field
        the ``.PSX`` format actually defines as holding a type id, and a hit
        rate above the base rate and above the control roots. Passing the raw
        list here yields a FALSE ``CANDIDATE_C``.
    ``d_bytecode_edges``
        Spawn edges recovered by DECODING the Script_Section -- in practice the
        ``literal_edges`` of the ``SpawnEdgeReport``s below. This evidence needs
        no control comparison, and that is the whole point of it: an edge
        identified by opcode, call-site offset and API slot cannot be a byte
        coincidence, which is precisely what sank Candidate B and Candidate C.
        Defaulted to empty so every pre-existing three-argument caller keeps its
        meaning exactly.

    Precedence: D FIRST -- a decoded call site is the strongest evidence
    available, strictly stronger than any of the three byte-pattern methods, so
    it wins over B, C and A. Then B, because a surviving blob field naming a
    companion would also be the edge. Then C, an edge recorded outside the blob.
    A last, because A only narrows the pool. ``UNRESOLVED`` if and only if ALL
    FOUR are empty (Requirement 1.8 as widened).

    Pure: no I/O, no mutation, no globals.
    """
    if d_bytecode_edges:
        return "CANDIDATE_D_BYTECODE"
    if b_edge_hits:
        return "CANDIDATE_B"
    if c_edge_coreferences:
        return "CANDIDATE_C"
    if a_named_candidates:
        return "CANDIDATE_A"
    return "UNRESOLVED"


#: The MEASURED verdict of this investigation, recorded in `companion-notes.md`.
#:
#: SUPERSEDED ONCE, deliberately and on the record. It read ``UNRESOLVED`` while
#: only Candidates A, B and C had been run: A produced a 27-id pool later
#: narrowed to 10, and B and C both came back negative under controls, so no edge
#: was named. Then `randomizer/befscript.py` landed -- 158 opcodes at `verified`
#: confidence, all 530 script sections decoding at 100% coverage and
#: round-tripping byte-exactly -- and the edge became directly readable:
#: `49 CScriptApi::Spawn` takes a literal BEF type id, pushed by the `*_push`
#: family and drained by `popapiparam`. 0 refusals across 359 entries in five
#: worlds, and 0 non-literal `Spawn` arguments anywhere in the game.
#:
#: So the verdict is CANDIDATE_D_BYTECODE, a value the original four did not
#: contain. The verdict unblocked the port layer; the separate size measurement
#: (config A, +1,538,265 .PRS / +565,584 .PRT, under a proven-good figure on both
#: axes) is what authorised shipping it, and
#: `cross_world.COMPANION_PORT_ENABLED` is now True.
MEASURED_VERDICT: str = "CANDIDATE_D_BYTECODE"


# ============================================================================
# CANDIDATE D (the fourth method) -- READ THE EDGE OUT OF THE BYTECODE.
#
# Candidates A, B and C all failed, and the notes above say why: A can only
# produce a pool, B was explicitly forbidden from decoding opcodes ("do NOT
# decode opcodes", because no decoder existed), and C's own criterion came back
# negative under controls.
#
# `randomizer/befscript.py` now exists and is fully verified: 158 opcodes all at
# `verified` confidence, measured from the engine's own handlers, and every one
# of the game's 530 script sections decodes at 100% coverage and round-trips
# byte-exactly. So the edge is directly readable: `49 CScriptApi::Spawn`
# (arity 1) takes a BEF type id, and the compiler these scripts came out of
# passes it as a literal push.
#
# THE CALLING CONVENTION (measured; it corrected a wrong first attempt).
# Arguments are NOT passed by the `*_apiparam` opcodes (0x8D-0x90). This
# compiler pushes onto the operand stack and drains into the API argument block:
#
#     by_c_push value=0x8C      <- the spawn type id, as a literal
#     popapiparam               <- or by_popsetapiparam count=N
#     by_callapi api_index=0x31 <- CScriptApi::Spawn, slot 49, arity 1
#
# `popapiparam` (0x15) moves one value; `popsetapiparam` / `by_popsetapiparam`
# (0x16/0x17) move `count`. A tracer that looks for `*_apiparam` finds zero
# arguments on every call -- that is the tracer being wrong, not the script
# being empty. So recovering a `Spawn` argument means running a small stack
# machine over the `*_push` family.
#
# WHAT THIS ORACLE WILL AND WILL NOT CLAIM.
#   * A LITERAL spawn argument (`c_push` / `by_c_push`) is recoverable and is
#     reported as an edge.
#   * A NON-LITERAL argument -- `by_m_push member=N`, a local, a register -- is
#     NOT recoverable statically and is reported as such, never guessed. This
#     case is real: Bomb_Skeleton's fuse comes from member field 12, so the
#     compiler demonstrably does pass member fields to API calls.
#   * `176 SpawnAbility` (arity 3) sites are reported SEPARATELY and marked
#     ARGUMENT ROLE UNDETERMINED. Which of its three arguments is a type id was
#     never established, so no presence verdict is claimed for it.
#
# Read-only, decode-only. Nothing here writes anything. `Encoding_Revision` is
# always an explicit argument and is never inferred from the bytes.
# ============================================================================

from . import befscript as _befscript  # noqa: E402  (kept with its section)

#: Script API slots this oracle cares about, from `befscript.API_SLOTS`.
SPAWN_API_INDEX = 49            # CScriptApi::Spawn, arity 1, handler 0x001154F0
SPAWN_ABILITY_API_INDEX = 176   # CScriptApi::SpawnAbility, arity 3

#: Value provenance kinds a traced argument can carry.
VALUE_LITERAL = "literal"
VALUE_MEMBER = "member"
VALUE_LOCAL = "local"
VALUE_REGISTER = "register"
VALUE_UNKNOWN = "unknown"

#: Push mnemonics whose pushed value the tracer can attribute. Everything else
#: (`r_pushall`, `l_pushset`, the arithmetic family, branches) is treated as
#: making the operand stack UNKNOWN rather than being modelled -- a linear walk
#: cannot honour control flow, so the tracer only trusts a push that sits in the
#: straight-line run immediately before the drain. That is exactly the shape the
#: measurements show, and it is the conservative direction: it can only turn a
#: recoverable literal into an `unknown`, never invent one.
_PUSH_KINDS = {
    "by_c_push": (VALUE_LITERAL, "value"),
    "c_push": (VALUE_LITERAL, "value"),
    "by_m_push": (VALUE_MEMBER, "member"),
    "m_push": (VALUE_MEMBER, "member"),
    "by_l_push": (VALUE_LOCAL, "local"),
    "l_push": (VALUE_LOCAL, "local"),
    "r_push": (VALUE_REGISTER, "reg"),
}

_DRAIN_ONE = frozenset({"popapiparam"})
_DRAIN_MANY = frozenset({"popsetapiparam", "by_popsetapiparam"})
_CALL_API = frozenset({"callapi", "by_callapi"})


@dataclass(frozen=True)
class ScriptValue:
    """One traced argument value handed to a script API call.

    ``kind`` is one of the ``VALUE_*`` constants. ``value`` is the literal for
    ``VALUE_LITERAL`` and the member/local/register INDEX for the others, and is
    ``None`` for ``VALUE_UNKNOWN``. ``push_offset`` is the offset of the
    instruction that produced it, or ``None`` when it could not be attributed.
    """

    kind: str
    value: Optional[int]
    push_offset: Optional[int]

    @property
    def is_literal(self) -> bool:
        return self.kind == VALUE_LITERAL

    def describe(self) -> str:
        if self.kind == VALUE_LITERAL:
            return f"{self.value} (0x{self.value:02X}) @{self.push_offset}"
        if self.kind == VALUE_UNKNOWN:
            return "UNKNOWN (not statically recoverable)"
        return f"{self.kind}[{self.value}] @{self.push_offset} " \
               f"(NOT statically recoverable)"


@dataclass(frozen=True)
class SpawnSite:
    """One `Spawn` or `SpawnAbility` call site inside a Script_Section."""

    api_index: int
    api_name: str
    call_offset: int
    args: tuple[ScriptValue, ...]
    #: True for `SpawnAbility`: which of its three arguments is a type id was
    #: never established, so no presence verdict may be claimed for it.
    role_undetermined: bool = False

    @property
    def literal_type_id(self) -> Optional[int]:
        """The spawned type id, for a single-argument literal `Spawn` only."""
        if self.role_undetermined or len(self.args) != 1:
            return None
        arg = self.args[0]
        return arg.value if arg.is_literal else None

    def describe(self) -> str:
        args = "; ".join(a.describe() for a in self.args) or "(no args traced)"
        tail = "  [ARGUMENT ROLE UNDETERMINED]" if self.role_undetermined else ""
        return (f"{self.api_name} @{self.call_offset} "
                f"args=[{args}]{tail}")


@dataclass(frozen=True)
class SpawnEdgeReport:
    """The spawn edges one BEF_Blob's Script_Section names. Never an exception.

    ``refused`` is True when the blob could not be decoded -- no script span, or
    not a Clean_Decode -- and ``reason`` then says why. A refusal reports NO
    edges; it does not report "no edges found", which is a different claim.
    """

    type_id: int
    revision: str
    refused: bool = False
    reason: Optional[str] = None
    section_len: int = 0
    coverage: float = 0.0
    clean: bool = False
    spawn_sites: tuple[SpawnSite, ...] = ()
    ability_sites: tuple[SpawnSite, ...] = ()

    @property
    def literal_edges(self) -> tuple[int, ...]:
        """Distinct spawned type ids recovered as LITERALS, sorted."""
        return tuple(sorted({s.literal_type_id for s in self.spawn_sites
                             if s.literal_type_id is not None}))

    @property
    def nonliteral_sites(self) -> tuple[SpawnSite, ...]:
        """`Spawn` sites whose argument is a member/local/register/unknown."""
        return tuple(s for s in self.spawn_sites if s.literal_type_id is None)

    def describe(self) -> str:
        if self.refused:
            return f"0x{self.type_id:02X} REFUSED: {self.reason}"
        edges = ", ".join(f"0x{i:02X}" for i in self.literal_edges) or "-"
        return (f"0x{self.type_id:02X} section={self.section_len}B "
                f"coverage={self.coverage * 100:.2f}% "
                f"clean={'yes' if self.clean else 'no'} "
                f"Spawn sites={len(self.spawn_sites)} "
                f"literal edges={edges} "
                f"nonliteral={len(self.nonliteral_sites)} "
                f"SpawnAbility sites={len(self.ability_sites)}")


def trace_api_arguments(disassembly) -> list[SpawnSite]:
    """Recover the arguments of every `Spawn` / `SpawnAbility` call site.

    Runs the measured calling convention as a small stack machine over a
    ``befscript.Disassembly``: the ``*_push`` family fills an operand stack,
    ``popapiparam`` / ``popsetapiparam`` / ``by_popsetapiparam`` drain it into
    the API argument block, and ``callapi`` / ``by_callapi`` consume the block.

    Arguments are reported in DRAIN order -- first popped first, i.e. last
    pushed first. Which of those the engine calls argument 0 is NOT determined
    and does not matter for ``Spawn``, which has arity 1.

    Any instruction outside the modelled families discards the operand stack, so
    a value that a branch or a computation produced is never mistaken for a
    literal. Pure; never raises.
    """
    sites: list[SpawnSite] = []
    stack: list[ScriptValue] = []
    block: list[ScriptValue] = []

    def drain(count: int) -> None:
        for _ in range(count):
            block.append(stack.pop() if stack
                         else ScriptValue(VALUE_UNKNOWN, None, None))

    for ins in disassembly.instructions:
        mnemonic = ins.mnemonic
        operands = {o.name: o.value for o in ins.operands}
        if mnemonic in _PUSH_KINDS:
            kind, name = _PUSH_KINDS[mnemonic]
            stack.append(ScriptValue(kind, operands.get(name), ins.offset))
            continue
        if mnemonic in _DRAIN_ONE:
            drain(1)
            continue
        if mnemonic in _DRAIN_MANY:
            drain(max(0, int(operands.get("count", 0))))
            continue
        if mnemonic in _CALL_API:
            index = operands.get(_befscript.API_INDEX_OPERAND_NAME)
            slot = _befscript.api_slot(index) if isinstance(index, int) else None
            if index in (SPAWN_API_INDEX, SPAWN_ABILITY_API_INDEX):
                sites.append(SpawnSite(
                    api_index=index,
                    api_name=slot.name if slot else f"api[{index}]",
                    call_offset=ins.offset,
                    args=tuple(block),
                    role_undetermined=(index == SPAWN_ABILITY_API_INDEX),
                ))
            block.clear()
            stack.clear()
            continue
        # anything else: the stack is no longer trustworthy.
        stack.clear()
    return sites


def spawn_edges_for_blob(blob: bytes, revision: str) -> tuple[
        bool, Optional[str], object]:
    """(refused, reason, disassembly) for one blob. Never raises.

    ``revision`` is REQUIRED and is never inferred; every target of this
    investigation is retail.
    """
    try:
        section = _befscript.script_section(blob)
    except ValueError as exc:
        return True, f"no usable Script_Section ({exc})", None
    except Exception as exc:                      # defensive: never raise
        return True, f"script span unreadable ({exc!r})", None
    try:
        dis = _befscript.disassemble(section, revision)
    except ValueError as exc:                     # bad revision: a real refusal
        return True, f"decode refused ({exc})", None
    except Exception as exc:                      # pragma: no cover - defensive
        return True, f"decode failed ({exc!r})", None
    if not dis.clean:
        return (True,
                f"not a Clean_Decode (coverage {dis.coverage * 100:.2f}%, "
                f"{len(dis.failures)} failure(s))", dis)
    return False, None, dis


def spawn_edge_report(blob: bytes, type_id: int, revision: str) -> SpawnEdgeReport:
    """The spawn edges one blob's Script_Section names. NEVER raises.

    A blob that does not Clean_Decode is a REFUSAL with a reason, in the same
    style as the rest of this module: it reports no edges, which is not the same
    claim as "this entity spawns nothing".
    """
    refused, reason, dis = spawn_edges_for_blob(blob, revision)
    if refused:
        return SpawnEdgeReport(
            type_id=type_id, revision=revision, refused=True, reason=reason,
            section_len=(dis.length if dis is not None else 0),
            coverage=(dis.coverage if dis is not None else 0.0),
            clean=False,
        )
    sites = trace_api_arguments(dis)
    return SpawnEdgeReport(
        type_id=type_id, revision=revision, refused=False, reason=None,
        section_len=dis.length, coverage=dis.coverage, clean=dis.clean,
        spawn_sites=tuple(s for s in sites if s.api_index == SPAWN_API_INDEX),
        ability_sites=tuple(s for s in sites
                            if s.api_index == SPAWN_ABILITY_API_INDEX),
    )


def spawn_edge_reports(source_bef: BefFile, revision: str,
                       ) -> dict[int, SpawnEdgeReport]:
    """One `SpawnEdgeReport` per entry of a parsed ``.BEF``. Never raises."""
    return {entry.type_id: spawn_edge_report(entry.data, entry.type_id, revision)
            for entry in source_bef.entries}


def make_spawn_edge_oracle(source_bef: BefFile, revision: str):
    """Return the EDGE ORACLE ``resolve_group`` takes: ``int -> frozenset[int]``.

    The returned callable maps a type id to the set of type ids its
    Script_Section names as LITERAL `Spawn` arguments. A type id absent from the
    ``.BEF``, a blob that will not Clean_Decode, and an entity that spawns
    nothing all map to the empty set -- so the oracle never raises and never
    guesses. The per-entity detail, including refusals and non-literal sites, is
    on the returned callable's ``reports`` attribute so a caller can tell those
    three cases apart.

    `SpawnAbility` is deliberately NOT an edge source: which of its three
    arguments is a type id is undetermined.
    """
    reports = spawn_edge_reports(source_bef, revision)

    def edges(type_id: int) -> frozenset:
        report = reports.get(type_id)
        if report is None or report.refused:
            return frozenset()
        return frozenset(report.literal_edges)

    edges.reports = reports          # type: ignore[attr-defined]
    edges.revision = revision        # type: ignore[attr-defined]
    return edges


# ============================================================================
# COMPANION GRAPH (task 3.3) -- the bounded transitive closure the oracle feeds.
#
# Pure: no buffers, no I/O. A depth or cycle failure therefore provably mutates
# nothing, which is what Requirement 3.6 asks for.
# ============================================================================

#: root -> companion -> companion's companion. Headroom, not a guess.
COMPANION_MAX_DEPTH = 2


@dataclass(frozen=True)
class CompanionNode:
    type_id: int
    source_world: str
    depth: int


@dataclass(frozen=True)
class CompanionGroup:
    root: CompanionNode
    #: excludes the root, deduped, depth-ordered.
    members: tuple[CompanionNode, ...]


class CompanionResolveError(Exception):
    """Depth exceeded, cycle found, or an edge naming an absent type id."""


def resolve_group(root_type_id: int, source_world: str, source_bef: BefFile,
                  edges, *, max_depth: int = COMPANION_MAX_DEPTH,
                  require_present: bool = True) -> CompanionGroup:
    """Breadth-first bounded closure of the companion graph. Pure.

    ``edges`` is the investigation's edge oracle -- ``make_spawn_edge_oracle``
    returns one in exactly this shape.

    CYCLES RESOLVE, THEY DO NOT REFUSE (Requirement 3.4 as AMENDED). The first
    implementation refused on a revisit, exactly as Requirement 3.4 was written,
    and that was MEASURED WRONG for this game: retail ``ICE.BEF`` has ``0x1E``
    and ``0xBA`` spawning EACH OTHER inside Snowman ``0x1D``'s group, so the old
    rule refused a route this feature exists to fix
    (``companion cycle: 0x1D -> 0x1E -> 0xBA -> 0x1E``). A revisit is now
    VISIT-ONCE DEDUPE -- the same rule Requirement 3.5 already demanded for the
    acyclic diamond -- and only expansion beyond ``max_depth`` refuses.

    THE ORDER OF THE TWO CHECKS IS LOAD-BEARING. ``target in seen`` is tested
    BEFORE the depth test, so a SECOND, DEEPER path to an already-recorded member
    is skipped rather than refused. Bomb_Skeleton is exactly that shape:
    ``0x49 -> 0x5E`` at depth 1, ``0x5E -> 0x44``/``0xEB`` at depth 2, while
    ``0x44``/``0xEB`` are ALSO direct depth-1 edges of ``0x49``. Testing depth
    first would refuse it at ``max_depth = 2`` if the limit were ever tightened,
    and would refuse it today for any deeper diamond. Because the walk is
    breadth-first (``queue.pop(0)``), every member is recorded at its SHALLOWEST
    depth.

    Raises ``CompanionResolveError`` when a NEW entity sits deeper than
    ``max_depth``, or -- with ``require_present`` -- when an edge names a type id
    absent from ``source_bef``. An entity reachable by several paths, cyclic or
    not, appears exactly once.
    """
    present = source_bef.type_ids()
    if require_present and root_type_id not in present:
        raise CompanionResolveError(
            f"root 0x{root_type_id:02X} absent from {source_world}.BEF")

    root = CompanionNode(root_type_id, source_world, 0)
    seen = {root_type_id: root}
    members: list[CompanionNode] = []
    queue = [(root_type_id, 0)]
    while queue:
        type_id, depth = queue.pop(0)
        for target in sorted(edges(type_id)):
            if require_present and target not in present:
                raise CompanionResolveError(
                    f"0x{type_id:02X} spawns 0x{target:02X}, which is absent "
                    f"from {source_world}.BEF")
            if target in seen:
                # VISIT-ONCE DEDUPE. Covers the acyclic diamond (Requirement
                # 3.5) and the retail cycle 0x1E <-> 0xBA identically, and is
                # tested BEFORE the depth bound so a deeper second path to an
                # already-recorded member never refuses.
                continue
            if depth + 1 > max_depth:
                raise CompanionResolveError(
                    f"0x{target:02X} sits at depth {depth + 1}, beyond "
                    f"COMPANION_MAX_DEPTH={max_depth}")
            node = CompanionNode(target, source_world, depth + 1)
            seen[target] = node
            members.append(node)
            queue.append((target, depth + 1))
    members.sort(key=lambda n: (n.depth, n.type_id))
    return CompanionGroup(root=root, members=tuple(members))

# ============================================================================
# THE PORT LAYER (tasks 4, 6, 7) -- resources, BEF injection, group atomicity.
#
# EVERYTHING BELOW IS BEHIND ``cross_world.COMPANION_PORT_ENABLED``, which is
# False and is NOT flipped by this work. The entry point ``port_group`` is
# reached only when the caller has already read that switch, so with the switch
# off not one byte of this section runs and the staged buffers are byte-identical
# to today's output.
#
# WHY THE DISCIPLINE HERE IS LOAD-BEARING RATHER THAN BELT-AND-BRACES. Measured
# in `MAXDEMOR.ELF`: `CVmEntityManager::LoadBoundEntityFile` (0x00108350) loops
# over EVERY entry in the world's `.BEF` -- `GetSofMemImageByIndex(i)` ->
# `ReadEntityID(i)` -> `CVmEntityManager::Load(parser, id)`, which parses the
# blob with `CVmMemSofParser::ReadEntityInfo` -- and IF `Load` FAILS FOR ANY
# ENTRY IT ABORTS THE WHOLE FILE. A malformed injected blob therefore does not
# cost one entity, it costs the level. Two consequences, both honoured below:
#
#   * INJECTION IS APPEND-ONLY. ``rebuild_with_injections(..., replace=False)``
#     only. No eviction path is used at all, so no progression anchor, no `end*`
#     kill group and no ``PROTECTED_TAGS`` entry can be touched -- that safety is
#     STRUCTURAL here, not a check that could be forgotten.
#   * ANYTHING WE CANNOT PROVE PARSES IS A REFUSAL. Every blob this module stages
#     is re-parsed through the same material walk the porter used, and a blob
#     that will not walk refuses the ROOT.
#
# This module still does NOT import ``cross_world``: the session is passed in, so
# the dependency stays acyclic and every failure here is convertible by the
# caller into exactly one ``session.refuse``.
# ============================================================================

import struct as _struct  # noqa: E402  (kept with the port layer)

from . import anim_port as _anim_port  # noqa: E402
from . import mesh_port as _mesh_port  # noqa: E402

#: A ``.PRS``'s mesh-table entry count lives in a u32 here. Same constant
#: ``mesh_port.find_entry_bounds`` reads (it opens ``Reader(data, 0x20)``).
MESH_COUNT_OFFSET = 0x20

#: A BEF blob records its OWN type id in a u32 at +0x10. Stated by `bef.py`'s
#: module docstring and VERIFIED 80/80 across CASTLE during Candidate B (it was
#: the one and only survivor of that scan -- a self id, which is exactly why it
#: was not an edge).
BLOB_SELF_TYPE_ID_OFFSET = 0x10

#: The BEF type-id space. Ids are stored as u32 in the TOC but the whole game
#: uses 8-bit values, and a `Spawn` literal is pushed by `by_c_push` as ONE byte,
#: so an id above 0xFF could not be named by the bytecode at all.
TYPE_ID_SPACE = range(0x00, 0x100)


class CompanionPortRefusal(Exception):
    """A companion could not be ported. The caller refuses the ROOT enemy.

    Deliberately NOT ``CompanionResolveError``: that one means the GRAPH could
    not be resolved (pure, nothing staged), this one means a RESOURCE could not
    be ported. Both end in one ``session.refuse``, but they are different
    failures and the message says which.
    """


@dataclass
class CompanionRecord:
    """What porting one Companion_Entity produced. Also the memo value.

    ``mesh_indices`` / ``anim_indices`` map SOURCE index -> TARGET index, in the
    same direction as ``cross_world``'s ``mesh_memo`` / ``anim_memo``.
    ``tex_remap`` maps SOURCE-world-local ``.PRT`` index -> target index.

    ``blob`` is the REWRITTEN blob, ready to inject; it is ``None`` when
    ``reused_target_entry`` is True, because in that case nothing is injected.
    """

    source_world: str
    source_type_id: int
    mesh_indices: dict = field(default_factory=dict)
    anim_indices: dict = field(default_factory=dict)
    tex_remap: dict = field(default_factory=dict)
    target_type_id: Optional[int] = None
    blob: Optional[bytes] = None
    injected: bool = False
    reused_target_entry: bool = False
    reused_memo: bool = False
    appended_anims: int = 0
    ported_meshes: int = 0
    ported_textures: int = 0
    blob_len: int = 0

    def describe(self) -> str:
        if self.reused_target_entry:
            how = "REUSED the existing target entry (nothing injected)"
        elif self.reused_memo:
            how = "served from the session companion memo (nothing injected)"
        else:
            how = (f"injected as type "
                   f"0x{(self.target_type_id or 0):02X} ({self.blob_len} B)")
        return (f"0x{self.source_type_id:02X} from "
                f"{self.source_world.upper()}: {how}, "
                f"mesh {sorted(self.mesh_indices.items())}, "
                f"anim {sorted(self.anim_indices.items())}, "
                f"{self.ported_textures} texture(s)")


@dataclass
class GroupOutcome:
    """The outcome of one Companion_Group. Never raises out of ``port_group``."""

    ok: bool
    root_type_id: int
    reason: Optional[str] = None
    records: tuple = ()
    prs_growth: int = 0
    prt_growth: int = 0
    bef_growth: int = 0

    @property
    def companion_count(self) -> int:
        return len(self.records)

    @property
    def injected_count(self) -> int:
        return sum(1 for r in self.records if r.injected)


# --- blob self id ----------------------------------------------------------

def blob_self_type_id(blob: bytes) -> Optional[int]:
    """The type id a blob records for ITSELF at +0x10, or None if unreadable."""
    if len(blob) < BLOB_SELF_TYPE_ID_OFFSET + 4:
        return None
    return _struct.unpack_from("<I", blob, BLOB_SELF_TYPE_ID_OFFSET)[0]


def set_blob_self_type_id(blob: bytes, type_id: int) -> bytes:
    """Return ``blob`` with its own-type-id field at +0x10 set to ``type_id``.

    WHY THIS EXISTS, AND THE HONEST LIMIT OF WHAT IS KNOWN. If
    ``allocate_type_id`` ever hands a companion an id DIFFERENT from its source
    id, the blob's +0x10 field still says the OLD id, and the entry is then
    internally inconsistent: the TOC says one thing, the blob says another.
    WHETHER THE ENGINE READS THIS FIELD IS NOT ESTABLISHED. What was read in the
    ELF is that ``LoadBoundEntityFile`` obtains the id from
    ``CVmBefReader::ReadEntityID(i)`` -- an index-driven read that the disassembly
    shows going through the reader, not through the blob body -- and passes it to
    ``CVmEntityManager::Load(parser, id)``, which is what ``CHashTable::Add``
    keys on. That is consistent with +0x10 being unread, but it does not prove
    ``ReadEntityInfo`` ignores it, and `LoadBoundEntityFile` aborts the WHOLE
    file if any `Load` fails.
    SO THE FIELD IS UPDATED ANYWAY. Making the blob agree with its TOC entry is
    correct under either reading and costs four bytes of write; leaving a stale
    id is only safe under the reading we cannot yet prove. In practice the field
    is rarely touched at all, because ``allocate_type_id`` prefers the source id
    and every measured route's companions are absent from GRAVE.
    """
    if len(blob) < BLOB_SELF_TYPE_ID_OFFSET + 4:
        raise CompanionPortRefusal(
            f"blob is {len(blob)} bytes, too small to hold its own type id at "
            f"+0x{BLOB_SELF_TYPE_ID_OFFSET:02X}")
    out = bytearray(blob)
    _struct.pack_into("<I", out, BLOB_SELF_TYPE_ID_OFFSET, int(type_id))
    return bytes(out)


# --- type-id allocation (task 6.1) -----------------------------------------

def allocate_type_id(source_type_id: int, used) -> int:
    """Prefer the SOURCE id when free; otherwise the lowest free id.

    PREFERRING THE SOURCE ID IS THE WHOLE LEVERAGE OF THIS FEATURE, and it is
    now justified rather than assumed: the spawn operand is a LITERAL BYTE inside
    a Script_Section that the port copies VERBATIM, so keeping the id means the
    edge needs no rewrite at all -- there is no instruction to patch, no operand
    to relocate, and no risk of desynchronising a script whose semantics we only
    partly understand.

    Raises ``CompanionResolveError`` when the id space is exhausted (Requirement
    5.5), which the caller turns into a refusal of the ROOT.
    """
    used = frozenset(int(u) for u in used)
    if source_type_id not in used:
        return int(source_type_id)
    for candidate in TYPE_ID_SPACE:
        if candidate not in used:
            return candidate
    raise CompanionResolveError(
        "no free BEF type id in the target world: all 256 are taken, so this "
        "companion cannot be injected")


# --- routing (task 9.1) ----------------------------------------------------

#: Sentinel telling "this route is not in the allowlist at all" apart from
#: "this route is in the allowlist with companions=None (uninvestigated)".
ROUTE_ABSENT = object()


def route_companions(enemy_id: int, source_world: str, target_world: str):
    """The ``companions`` field of one allowlist route.

    Returns ``ROUTE_ABSENT`` when no entry matches, ``None`` when the entry
    exists but is UNINVESTIGATED, an empty tuple when it was investigated and
    measured to have none, or the measured tuple. Reads
    ``catalog.CROSS_WORLD_ALLOWLIST`` directly -- the FULL list, not
    ``load_allowlist()``, because ``active`` is a build switch and says nothing
    about what was measured.
    """
    for entry in catalog.CROSS_WORLD_ALLOWLIST:
        if (entry.enemy_id == enemy_id
                and entry.source_world == source_world
                and entry.target_world == target_world):
            return entry.companions
    return ROUTE_ABSENT


# --- companion resource porting (task 4) -----------------------------------

def _log(session, message: str) -> None:
    """Log through the session, whose ``log`` already carries ``[cross-world]``.

    ``ELF patch:`` is reserved for executable patches and never appears here.
    """
    session.log(f"companion {message}")


class _CompanionPlan:
    """The two attributes ``WorldPortSession._port_one_mesh`` reads off a plan.

    A companion is not a PLACEMENT: nothing classifies it, nothing puts it in a
    map, and it has no source/target pool membership. Handing ``_port_one_mesh``
    a real ``PlacementPlan`` would therefore be a lie in every other field, and
    importing ``cross_world`` to build one would make the dependency cyclic. So
    this carries exactly the two fields that path uses, for its log labels.
    """

    __slots__ = ("name", "enemy_id")

    def __init__(self, root_type_id: int, type_id: int, source_world: str):
        self.name = (f"COMPANION_0x{type_id:02X}"
                     f"(of 0x{root_type_id:02X},{source_world.upper()})")
        self.enemy_id = type_id


def _target_rig_names(session, prs_bytes, mesh_indices) -> tuple:
    """The union of every name an anim record of this rig could address.

    SKA records name BONES and MEA records name MESH NODES, and
    ``mesh_port.mesh_entry_anim_target_names`` returns both for one entry, so the
    UNION across the companion's own ported entries is the correct comparison
    set. A single entry that will not yield names contributes none rather than
    aborting -- a missing name can only make the gate STRICTER, never laxer.

    THE RETURN SHAPE IS A PAIR, AND UNPACKING IT IS LOAD-BEARING.
    ``mesh_entry_anim_target_names`` returns ``(bone_names, mesh_node_names)`` --
    TWO tuples, not one flat sequence. An earlier version of this function
    iterated the pair directly, so the "rig" it handed the gate was a list
    holding two TUPLE OBJECTS and not one name string. ``missing_bone_names``
    compares by string equality, so every name of every record was reported
    missing and EVERY companion carrying an anim record refused -- measured on
    all three of `0x1E` (ICE), `0x44` (CASTLE) and `0x1A` (SWAMP), each of whose
    own mesh entries declares EXACTLY the names its own records address
    (`snow_debris#*`, `N1`..`N5`, `*_j`; missing count 0 once flattened).

    WHY THE CHECK IS STILL NON-VACUOUS. The names come from the MESH, parsed by
    ``prs_mesh_parser`` out of the staged ``.PRS``, and the addressed names come
    from the ANIM RECORD's own channel table. Those are two independent artefacts
    read by two independent parsers, so a record whose channels name something
    its own mesh does not declare is genuinely caught -- unlike deriving the
    target names from the anim bundle, which is true by construction.
    """
    names: list = []
    for index in sorted(set(mesh_indices)):
        try:
            bones, nodes = _mesh_port.mesh_entry_anim_target_names(prs_bytes,
                                                                  index)
        except Exception as exc:  # noqa: BLE001 - reported, never fatal
            _log(session, f"target mesh[{index}] bone/node names could not be "
                          f"read ({type(exc).__name__}: {exc}); it contributes "
                          f"no name to the rig")
            continue
        entry_names = tuple(bones) + tuple(nodes)
        _log(session, f"ported mesh[{index}] carries {len(bones)} bone name(s) "
                      f"and {len(nodes)} mesh-node name(s)")
        for name in entry_names:
            if name and name not in names:
                names.append(name)
    return tuple(names)


def port_companion(session, node: CompanionNode, source_bef: BefFile, *,
                   root_type_id: int) -> CompanionRecord:
    """Port ONE Companion_Entity's resources into ``session``. Task 4.

    Mirrors ``_port_enemy``'s steps without its placement concerns, and reuses
    the SAME code paths so a companion is held to the enemy's standard:
    ``session.target_trust`` (Requirement 4.2), ``mesh_port.assess_source_entry``
    plus ``session._port_one_mesh`` with ``mesh_memo`` dedupe (4.1),
    ``session.assign_tex_index`` (4.4), ``bef.set_material_ref_index`` (4.5),
    GLOBAL refs untouched (4.6), and the anim gate before the anim append (4.3,
    4.8).

    Raises ``CompanionPortRefusal`` for every recoverable problem. It COMMITS to
    ``session.prs`` / ``session.prt`` on success, which is safe precisely because
    the only caller is inside a ``GroupStage`` that restores the session wholesale
    on any later failure -- that is what makes per-group atomicity compose
    (Requirement 6.1-6.4). Companions are ported SEQUENTIALLY, so at most one
    staged ``.PRS`` exists beyond the transient buffer an insert or append
    unavoidably creates (Requirement 8.7).
    """
    sw = node.source_world.upper()
    memo_key = (sw, node.type_id)
    cached = session.companion_memo.get(memo_key)
    if cached is not None:
        # Requirements 7.1-7.3: ported once per session, and the SAME target
        # mesh / anim / texture indices are reused.
        record = CompanionRecord(
            source_world=sw, source_type_id=node.type_id,
            mesh_indices=dict(cached.mesh_indices),
            anim_indices=dict(cached.anim_indices),
            tex_remap=dict(cached.tex_remap),
            target_type_id=cached.target_type_id,
            blob=None, injected=False,
            reused_target_entry=cached.reused_target_entry,
            reused_memo=True, blob_len=cached.blob_len)
        _log(session, f"0x{node.type_id:02X} already ported this session -> "
                      f"reusing mesh {sorted(record.mesh_indices.items())}, "
                      f"anim {sorted(record.anim_indices.items())}, "
                      f"textures {sorted(record.tex_remap.items())}")
        return record

    entry = source_bef.get_entry(node.type_id)
    if entry is None:
        raise CompanionPortRefusal(
            f"companion 0x{node.type_id:02X} of root 0x{root_type_id:02X} is "
            f"absent from {sw}.BEF, so its blob cannot be read")
    blob = entry.data

    # --- the TARGET table gate (Requirement 4.2) --------------------------
    trust = session.target_trust()
    if not trust.ok:
        raise CompanionPortRefusal(
            f"companion 0x{node.type_id:02X}: the target "
            f"{session.target_world.upper()}.PRS mesh table is not trustworthy, "
            f"so nothing may be appended to it: {trust.reason}")

    try:
        refs = bef.parse_material_refs(blob)
    except MaterialSectionError as exc:
        raise CompanionPortRefusal(
            f"companion 0x{node.type_id:02X}: its material section does not "
            f"walk, so its resource references cannot be trusted: {exc}") \
            from exc

    global_refs = [r for r in refs if r.is_global]
    if global_refs:
        # Requirement 4.6: a GLOBAL asset exists in every world, so its index is
        # already correct and touching it would break it.
        _log(session, f"0x{node.type_id:02X}: {len(global_refs)} GLOBAL ref(s) "
                      f"left strictly alone")

    local_cat1 = [r for r in refs
                  if not r.is_global and r.category == MATERIAL_CATEGORY_MESH]
    distinct = sorted({r.index for r in local_cat1})
    _log(session, f"0x{node.type_id:02X} ({len(blob)} B): {len(refs)} material "
                  f"ref(s), {len(local_cat1)} LOCAL CATEGORY-1 naming "
                  f"{len(distinct)} distinct source mesh index(es) {distinct}")

    plan = _CompanionPlan(root_type_id, node.type_id, sw)
    source_prs = session._source_path(sw, "PRS")
    entries: dict = {}
    signatures: dict = {}
    if distinct:
        try:
            source_bytes = source_prs.read_bytes()
        except (FileNotFoundError, OSError) as exc:
            raise CompanionPortRefusal(
                f"companion 0x{node.type_id:02X}: source {source_prs.name} "
                f"could not be read: {type(exc).__name__}: {exc}") from exc
        try:
            # RANGE CHECK FIRST (Requirement 4.7). The source table's own count
            # word bounds every LOCAL index; an index at or past it names no
            # entry at all, and walking to it would either resync onto unrelated
            # bytes or run off the buffer.
            if len(source_bytes) < MESH_COUNT_OFFSET + 4:
                raise CompanionPortRefusal(
                    f"companion 0x{node.type_id:02X}: source "
                    f"{source_prs.name} is too small to hold a mesh count")
            source_count = _struct.unpack_from("<I", source_bytes,
                                               MESH_COUNT_OFFSET)[0]
            out_of_range = [i for i in distinct if not 0 <= i < source_count]
            if out_of_range:
                raise CompanionPortRefusal(
                    f"companion 0x{node.type_id:02X}: LOCAL CATEGORY-1 index"
                    f"(es) {out_of_range} fall outside {source_prs.name}'s mesh "
                    f"table, which declares {source_count} entr"
                    f"{'y' if source_count == 1 else 'ies'}")
            for index in distinct:
                if (sw, MATERIAL_CATEGORY_MESH, index) in session.mesh_memo:
                    continue
                try:
                    verdict = _mesh_port.assess_source_entry(
                        source_bytes, index, label=source_prs.name)
                except Exception as exc:  # noqa: BLE001
                    raise CompanionPortRefusal(
                        f"companion 0x{node.type_id:02X}: {source_prs.name} "
                        f"mesh table could not be walked as far as "
                        f"mesh[{index}]: {type(exc).__name__}: {exc}") from exc
                for line in verdict.details:
                    session.log(f"gate {line}")
                if not verdict.ok:
                    raise CompanionPortRefusal(
                        f"companion 0x{node.type_id:02X}: {verdict.reason}")
                entries[index] = verdict.entry_bytes
                signatures[index] = _mesh_port.mesh_entry_signature(
                    source_bytes, index)
                session.sig_memo[(sw, MATERIAL_CATEGORY_MESH, index)] = \
                    signatures[index]
        finally:
            # One .PRS at a time: the source buffer is dropped before the target
            # is grown (Requirement 8.7).
            del source_bytes

    # --- stage the mesh entries and their world-local textures ------------
    staged_prs = session.prs
    staged_prt = session.prt
    reserved: list = []
    new_indices: dict = {}
    remap_all: dict = {}
    ported_meshes = 0
    ported_tex = 0
    for index in distinct:
        memo_key_mesh = (sw, MATERIAL_CATEGORY_MESH, index)
        if memo_key_mesh in session.mesh_memo:
            new_indices[index] = session.mesh_memo[memo_key_mesh]
            _log(session, f"0x{node.type_id:02X}: source mesh[{index}] already "
                          f"ported this session -> target "
                          f"mesh[{new_indices[index]}] (deduped)")
            continue
        try:
            staged_prs, staged_prt, new_index, remap, ported = \
                session._port_one_mesh(plan, staged_prs, staged_prt, sw,
                                       index, entries[index], reserved)
        except Exception as exc:  # noqa: BLE001 - every porter failure refuses
            raise CompanionPortRefusal(
                f"companion 0x{node.type_id:02X}: {exc}") from exc
        new_indices[index] = new_index
        remap_all.update(remap)
        ported_meshes += 1
        ported_tex += ported

    # --- rewrite the LOCAL CATEGORY-1 refs, flag bytes preserved ----------
    patched_blob = blob
    for ref in local_cat1:
        patched_blob = bef.set_material_ref_index(patched_blob, ref,
                                                  new_indices[ref.index])
    # ``bef.remap_blob_textures`` is called for the reason the task names it: it
    # is the documented blob-level texture step. IT IS A NO-OP BY DESIGN, and its
    # own docstring says so at length -- a blob does not carry texture indices;
    # they live in the ``.PRS`` entry's RENDER PASSES, which is where
    # ``_port_one_mesh`` above actually remapped them via
    # ``prs_mesh_parser.patch_texture_ids`` using ids reserved from
    # ``session.assign_tex_index`` (Requirement 4.4). Calling it is honest about
    # the pipeline shape; relying on it to remap anything would not be.
    patched_blob = bef.remap_blob_textures(patched_blob, None)

    # --- anim: GATE FIRST, then append (Requirements 4.3, 4.8) ------------
    anim_indices: dict = {}
    appended_anims = 0
    anim_memo_update: dict = {}
    refreshed_count = None
    local_cat2 = [r for r in bef.parse_material_refs(patched_blob)
                  if not r.is_global and r.category == MATERIAL_CATEGORY_ANIM]
    if local_cat2:
        wanted = sorted({r.index for r in local_cat2})
        # --- REFS THE PARSER MISSED --------------------------------------
        # `bef.parse_material_refs` only reads refs at the START of a record
        # body, so refs sitting deeper are invisible and their indices were
        # never remapped. `missed_local_refs` recovers them with a narrow,
        # flag-correlated signature. This REPLACES an earlier heuristic that
        # guessed at "interior holes in a contiguous block": that guess got
        # Goat_Devil's anim 78 right by luck and 83 wrong, and a second guess
        # keyed on `SeqPlay` slot numbers was refuted outright -- `SeqPlay`
        # resolves its argument through `GetAppSeqDataPtr` @0x00117AB0, which
        # matches a sequence ID against a linked list, NOT an index into the
        # entity's anim block. These refs are MEASURED, not inferred.
        missed = missed_local_refs(patched_blob)
        missed_anim = sorted({r.index for r in missed
                              if r.category == MATERIAL_CATEGORY_ANIM})
        if missed:
            session.log(
                f"anim gate companion 0x{node.type_id:02X}: {sw} carries "
                f"{len(missed)} resource ref(s) that parse_material_refs does "
                f"NOT see, at "
                + ", ".join(f"0x{r.idx_offset:X}(cat{r.category} idx{r.index})"
                            for r in missed)
                + " -- their indices were previously left pointing at "
                  "SOURCE-world resources")
        if missed_anim:
            before_wanted = list(wanted)
            wanted = sorted(set(wanted) | set(missed_anim))
            added = [i for i in wanted if i not in before_wanted]
            if added:
                session.log(
                    f"anim gate companion 0x{node.type_id:02X}: {sw} anim set "
                    f"grows by {added} because those records ARE referenced -- "
                    f"by refs the parser missed")

        try:
            source_bytes = source_prs.read_bytes()
        except (FileNotFoundError, OSError) as exc:
            raise CompanionPortRefusal(
                f"companion 0x{node.type_id:02X}: source {source_prs.name} "
                f"could not be re-read for its anim records: "
                f"{type(exc).__name__}: {exc}") from exc
        try:
            bundle = _anim_port.collect_source_records(
                source_bytes, wanted, source_world=sw, label=source_prs.name)
        finally:
            del source_bytes
        for line in bundle.details:
            session.log(f"anim gate {line}")
        if not bundle.ok:
            raise CompanionPortRefusal(
                f"companion 0x{node.type_id:02X}: its own anim records cannot "
                f"be trusted: {'; '.join(bundle.reasons)}")

        # THE COMPATIBILITY GATE, RUN BEFORE ANYTHING IS APPENDED. The rig the
        # records will drive is the companion's OWN ported mesh, so the names
        # come from the STAGED buffer's new entries -- not from a target-world
        # native, whose naming convention is different and against which every
        # channel would fail to bind.
        target_names = _target_rig_names(session, staged_prs,
                                         new_indices.values())
        for index in sorted(bundle.records):
            record = bundle.records[index]
            # Same predicate `port_anim_records` uses, so this pre-check and the
            # porter can never disagree about what is portable. A MEA node whose
            # name the ported rig does not carry is TOLERATED and LOGGED -- the
            # engine skips it (`anim_port.MEA_UNMATCHED_NODE_IS_SKIPPED`, an ELF
            # measurement) -- while an SKA record still refuses.
            missing, tolerated, note = _anim_port.unmatched_name_verdict(
                record.data, target_names)
            if missing and not tolerated:
                raise CompanionPortRefusal(
                    f"companion 0x{node.type_id:02X}: {sw} anim[{index}] "
                    f"addresses {len(missing)} name(s) with no counterpart in "
                    f"the ported rig ({len(target_names)} name(s) read from the "
                    f"staged .PRS): {', '.join(map(str, missing[:6]))}"
                    + (" ..." if len(missing) > 6 else "")
                    + (f" -- {note}" if note else ""))
            if tolerated:
                session.log(
                    f"anim gate companion 0x{node.type_id:02X}: {sw} "
                    f"anim[{index}] binds WITH {len(missing)} UNMATCHED "
                    f"NODE(S) SKIPPED BY THE ENGINE: {note}")

        # The porter rewrites by `idx_offset`, so handing it the missed refs
        # alongside the parsed ones is purely additive and makes them remap.
        outcome = _anim_port.port_anim_records(
            staged_prs, patched_blob, bef.parse_material_refs(patched_blob),
            bundle, target_names, memo=session.anim_memo, source_world=sw,
            label=f"{session.target_world.upper()}.PRS",
            extra_refs=missed)
        for line in outcome.details:
            session.log(f"anim port {line}")
        if not outcome.ok:
            raise CompanionPortRefusal(
                f"companion 0x{node.type_id:02X}: the anim port refused it, "
                f"and a refused port is never downgraded: {outcome.reason}")
        staged_prs = outcome.prs
        patched_blob = outcome.blob
        anim_indices = {src: dst for (_w, src), dst
                        in outcome.new_indices.items()}
        appended_anims = outcome.appended_count
        anim_memo_update = dict(outcome.memo)
        refreshed_count = outcome.new_count

    # --- prove the rewrite before committing anything ---------------------
    # Same standard ``_port_enemy`` holds itself to: re-parse and compare ref for
    # ref, because a flag byte that moved would have changed the category or the
    # global bit and silently repointed the reference at another resource table.
    try:
        after = bef.parse_material_refs(patched_blob)
    except MaterialSectionError as exc:
        raise CompanionPortRefusal(
            f"companion 0x{node.type_id:02X}: the rewritten blob's material "
            f"section no longer walks: {exc}") from exc
    if len(after) != len(refs) or len(patched_blob) != len(blob):
        raise CompanionPortRefusal(
            f"companion 0x{node.type_id:02X}: the rewrite changed the blob's "
            f"shape ({len(refs)} ref(s)/{len(blob)} B before, {len(after)}/"
            f"{len(patched_blob)} after)")
    for before, now in zip(refs, after):
        if (now.flag, now.idx_offset, now.rec_type) != (
                before.flag, before.idx_offset, before.rec_type):
            raise CompanionPortRefusal(
                f"companion 0x{node.type_id:02X}: rewrite changed more than an "
                f"index: {before.describe()} became {now.describe()}")
        if not before.is_global and before.category == MATERIAL_CATEGORY_MESH:
            expected = new_indices[before.index]
        elif (not before.is_global
              and before.category == MATERIAL_CATEGORY_ANIM
              and before.index in anim_indices):
            expected = anim_indices[before.index]
        else:
            expected = before.index
        if now.index != expected:
            raise CompanionPortRefusal(
                f"companion 0x{node.type_id:02X}: ref at "
                f"0x{before.idx_offset:X} should now read index {expected} but "
                f"reads {now.index}")

    # --- commit this companion's resources into the session ---------------
    prs_growth = len(staged_prs) - len(session.prs)
    prt_growth = len(staged_prt) - len(session.prt)
    session.prs = staged_prs
    session.prt = staged_prt
    for index, new_index in new_indices.items():
        session.mesh_memo[(sw, MATERIAL_CATEGORY_MESH, index)] = new_index
    if anim_memo_update:
        session.anim_memo.update(anim_memo_update)
    if appended_anims:
        session.refresh_anim_count(expected=refreshed_count)

    record = CompanionRecord(
        source_world=sw, source_type_id=node.type_id,
        mesh_indices=dict(new_indices), anim_indices=dict(anim_indices),
        tex_remap=dict(remap_all), blob=patched_blob,
        appended_anims=appended_anims, ported_meshes=ported_meshes,
        ported_textures=ported_tex, blob_len=len(patched_blob))
    _log(session, f"0x{node.type_id:02X} resources staged: {ported_meshes} mesh "
                  f"entr{'y' if ported_meshes == 1 else 'ies'} (+{prs_growth} B "
                  f"PRS), {ported_tex} texture(s) (+{prt_growth} B PRT), "
                  f"{appended_anims} anim record(s)")
    return record


# --- BEF injection (task 6.1) ---------------------------------------------

def stage_injection(session, record: CompanionRecord, target_bef: BefFile,
                    used) -> CompanionRecord:
    """Allocate a type id for one ported companion and stage its blob.

    APPEND ONLY. The blob goes on ``session.companion_blobs``, which
    ``WorldPortSession.finish`` hands to
    ``bef.BefFile.rebuild_with_injections(injected, replace=False)`` in the ONE
    rebuild it already performs. No eviction path is used anywhere, so every
    pre-existing entry stays byte-identical and no Progression_Anchors entry and
    no ``PROTECTED_TAGS`` entry can be touched (Requirements 5.1, 5.6, 5.7).

    REUSE COMES FIRST (Requirement 5.8): a companion already present in the
    target ``.BEF`` needs nothing injected, and the measured `0x3A` route is
    exactly that case -- its `0x35` IS in `GRAVE.BEF`.
    """
    if record.reused_memo or record.reused_target_entry:
        return record
    if target_bef.has_type(record.source_type_id):
        record.reused_target_entry = True
        record.target_type_id = record.source_type_id
        record.blob = None
        record.injected = False
        _log(session, f"0x{record.source_type_id:02X} is ALREADY PRESENT in "
                      f"{session.target_world.upper()}.BEF: the existing entry "
                      f"is reused and NOTHING is injected")
        return record

    target_id = allocate_type_id(record.source_type_id, used)
    if target_id in used:                       # Requirement 5.4, belt on braces
        raise CompanionPortRefusal(
            f"companion 0x{record.source_type_id:02X}: allocated type id "
            f"0x{target_id:02X} is already in use in the target .BEF")
    blob = record.blob
    if blob is None:
        raise CompanionPortRefusal(
            f"companion 0x{record.source_type_id:02X}: no rewritten blob to "
            f"inject")
    if target_id != record.source_type_id:
        # The spawn operand is a literal byte in a Script_Section we copy
        # verbatim, so a DIFFERENT id would leave the edge naming the old one.
        # That is a correctness hole we will not paper over.
        raise CompanionPortRefusal(
            f"companion 0x{record.source_type_id:02X}: its source type id is "
            f"taken in {session.target_world.upper()}.BEF by an unrelated "
            f"entity, and the spawn operand naming it is a literal byte inside "
            f"a Script_Section this port copies verbatim, so allocating "
            f"0x{target_id:02X} instead would leave the edge dangling")
    # Even though the id is unchanged here, the self-id field is asserted rather
    # than assumed: a blob whose +0x10 disagrees with its TOC entry is
    # internally inconsistent, and `LoadBoundEntityFile` aborts the WHOLE file
    # if any entry fails to parse.
    if blob_self_type_id(blob) != target_id:
        _log(session, f"0x{record.source_type_id:02X}: blob self id at +0x10 "
                      f"reads {blob_self_type_id(blob)}, rewriting it to "
                      f"0x{target_id:02X} so the blob agrees with its TOC entry")
        blob = set_blob_self_type_id(blob, target_id)

    # PROVE IT PARSES, through the same material walk the porter used, before it
    # can reach a .BEF the engine will refuse as a whole file if one entry is
    # bad. Anything we cannot prove parses is a refusal, not a hope.
    try:
        bef.parse_material_refs(blob)
    except MaterialSectionError as exc:
        raise CompanionPortRefusal(
            f"companion 0x{record.source_type_id:02X}: the blob staged for "
            f"injection does not walk its own material section ({exc}), and a "
            f"single bad entry aborts the engine's load of the WHOLE .BEF") \
            from exc

    record.blob = blob
    record.target_type_id = target_id
    record.injected = True
    session.companion_blobs.append((target_id, blob, record.source_world,
                                    record.source_type_id))
    _log(session, f"0x{record.source_type_id:02X} staged for APPEND-ONLY "
                  f"injection into {session.target_world.upper()}.BEF as type "
                  f"0x{target_id:02X} ({len(blob)} B)")
    return record


# --- group-level all-or-nothing commit (task 7) ---------------------------

class GroupStage:
    """Snapshot the session on entry; commit on success, restore on failure.

    THE SNAPSHOT COSTS NO MEMORY FOR THE BIG BUFFERS. ``.PRS`` / ``.PRT`` /
    ``.BEF`` are immutable ``bytes``, so holding a REFERENCE is already a
    snapshot -- rebinding ``session.prs`` to it restores the old value exactly,
    with no copy and no second multi-megabyte buffer. The memos and the counters
    are small, so those ARE copied.

    Restoring counters ABSOLUTELY (to the snapshot value) rather than by
    decrementing is deliberate: a decrement has to agree with a count of what was
    reserved, and any disagreement leaves a gap or a collision in the texture
    index space. Requirement 6.5 asks for no gap, and an absolute restore cannot
    leave one.

    Requirement 7.4 falls out of taking the snapshot AT GROUP ENTRY: memo entries
    an EARLIER, COMMITTED group created are already in the snapshot, so a
    restore keeps them -- only entries this group added are dropped.
    """

    def __init__(self, session):
        self.session = session
        self.prs = session.prs
        self.prt = session.prt
        self.bef = session.bef
        self.local_tex_count = session.local_tex_count
        self.tex_memo = dict(session.tex_memo)
        self.mesh_memo = dict(session.mesh_memo)
        self.sig_memo = dict(session.sig_memo)
        self.anim_memo = dict(session.anim_memo)
        self.companion_memo = dict(session.companion_memo)
        self.pending_blobs = len(session.pending_blobs)
        self.companion_blobs = len(session.companion_blobs)
        self.results = len(session.results)
        self.anim_count_cache = getattr(session, "_target_anim_count", "unset")

    def restore(self) -> None:
        """Put the session back exactly as it was before the group was tried."""
        session = self.session
        session.prs = self.prs
        session.prt = self.prt
        session.bef = self.bef
        session.local_tex_count = self.local_tex_count
        session.tex_memo.clear()
        session.tex_memo.update(self.tex_memo)
        session.mesh_memo.clear()
        session.mesh_memo.update(self.mesh_memo)
        session.sig_memo.clear()
        session.sig_memo.update(self.sig_memo)
        session.anim_memo.clear()
        session.anim_memo.update(self.anim_memo)
        session.companion_memo.clear()
        session.companion_memo.update(self.companion_memo)
        del session.pending_blobs[self.pending_blobs:]
        del session.companion_blobs[self.companion_blobs:]
        del session.results[self.results:]
        session._target_anim_count = self.anim_count_cache

    def unchanged(self) -> bool:
        """True when the session's buffers are byte-identical to the snapshot."""
        session = self.session
        return (session.prs is self.prs or session.prs == self.prs) and \
               (session.prt is self.prt or session.prt == self.prt) and \
               (session.bef is self.bef or session.bef == self.bef)


def port_group(session, plan, blob: bytes, *, source_bef: BefFile,
               edges, companions=None, max_depth: int = COMPANION_MAX_DEPTH
               ) -> GroupOutcome:
    """Port one enemy AND every Companion_Entity it owns, or nothing at all.

    THE ENEMY PORT IS NOT REWRITTEN, IT IS WRAPPED. ``session.port_enemy`` is
    already atomic per enemy: it stages everything on locals and commits in one
    step. Wrapping it in a ``GroupStage`` composes that into atomicity per GROUP,
    which is the whole of Requirements 6.1-6.4 with no new staging machinery.

    Order of operations, and each step's reason:

      1. ROUTE CHECK. ``companions is None`` -- uninvestigated -- refuses
         (Requirement 9.2). Porting an enemy whose extra objects may be missing
         is the defect this feature exists to fix.
      2. RESOLVE THE GRAPH. Pure, no buffers touched, so a depth refusal
         provably mutates nothing (Requirement 3.6).
      3. PORT THE ENEMY through the unchanged path.
      4. PORT EACH COMPANION SEQUENTIALLY, so two staged ``.PRS`` buffers never
         coexist beyond the transient append buffer.
      5. STAGE THE INJECTIONS, append-only.
      6. COMMIT, or restore the snapshot and refuse the ROOT with EXACTLY ONE
         reason through ``session.refuse`` (Requirements 6.6, 6.7).

    Never raises: every failure comes back as a non-ok ``GroupOutcome`` after the
    session has been restored and one refusal recorded.
    """
    root_id = plan.enemy_id
    stage = GroupStage(session)

    def refuse(reason: str) -> GroupOutcome:
        stage.restore()
        session.refuse(plan, reason)
        return GroupOutcome(ok=False, root_type_id=root_id, reason=reason)

    if companions is None:
        return refuse(
            f"root 0x{root_id:02X}'s companion set is UNINVESTIGATED "
            f"(allowlist companions=None), and porting an enemy whose owned "
            f"objects may be missing is the exact defect this feature exists to "
            f"fix")

    # --- 2. resolve the group, before a single byte is staged --------------
    try:
        group = resolve_group(root_id, plan.source_world or "", source_bef,
                              edges, max_depth=max_depth)
    except CompanionResolveError as exc:
        return refuse(f"root 0x{root_id:02X}'s companion group cannot be "
                      f"resolved: {exc}")

    resolved = tuple(n.type_id for n in group.members)
    declared = tuple(companions)
    if set(resolved) != set(declared):
        # The allowlist is DATA and the oracle is MEASUREMENT; a disagreement
        # means one of them is stale, and guessing which would be exactly the
        # kind of quiet assumption that produced the u16@22 era.
        return refuse(
            f"root 0x{root_id:02X}: the allowlist declares companions "
            f"{tuple(f'0x{t:02X}' for t in declared)} but the bytecode oracle "
            f"resolves {tuple(f'0x{t:02X}' for t in resolved)}; one of them is "
            f"stale and the port will not guess which")
    _log(session, f"root 0x{root_id:02X} group resolved: "
                  f"{len(group.members)} companion(s) "
                  f"{[f'0x{n.type_id:02X}@d{n.depth}' for n in group.members]}")

    # --- 3. the enemy, through the UNCHANGED path -------------------------
    _blob, result = session.port_enemy(plan, blob)
    if not result.ok:
        # ``port_enemy`` already refused and logged; it mutated nothing, but the
        # snapshot restore also drops the refusal result so the group can report
        # exactly one reason of its own (Requirement 6.6).
        reason = (result.warnings[0] if result.warnings
                  else f"root 0x{root_id:02X} could not be ported")
        return refuse(f"root 0x{root_id:02X} refused, so its whole companion "
                      f"group is refused: {reason}")

    # --- 4. + 5. each companion, then its append-only injection ----------
    records: list = []
    try:
        target_bef = BefFile.parse(session.bef)
    except Exception as exc:  # noqa: BLE001
        return refuse(f"root 0x{root_id:02X}: the target "
                      f"{session.target_world.upper()}.BEF does not parse, so "
                      f"no companion may be injected into it "
                      f"({type(exc).__name__}: {exc})")
    used = set(target_bef.type_ids()) | {tid for tid, *_ in
                                         session.companion_blobs}
    for node in group.members:
        # REUSE BEFORE PORT (Requirement 5.8). A companion the target world
        # ALREADY declares has its own mesh, anim and texture data in that
        # world's own tables, correctly indexed for that world -- there is
        # nothing to port and nothing to inject, and porting it anyway would
        # duplicate the entity AND expose it to source-side gates that have no
        # bearing on an entity that already works. The measured case is `0x3A`'s
        # `0x35`, which IS in `GRAVE.BEF`.
        if target_bef.has_type(node.type_id):
            record = CompanionRecord(
                source_world=node.source_world.upper(),
                source_type_id=node.type_id,
                target_type_id=node.type_id,
                reused_target_entry=True)
            _log(session, f"0x{node.type_id:02X} is ALREADY PRESENT in "
                          f"{session.target_world.upper()}.BEF: the existing "
                          f"entry is reused, nothing is ported and nothing is "
                          f"injected")
            records.append(record)
            continue
        try:
            record = port_companion(session, node, source_bef,
                                    root_type_id=root_id)
            record = stage_injection(session, record, target_bef, used)
        except (CompanionPortRefusal, CompanionResolveError) as exc:
            return refuse(f"root 0x{root_id:02X}'s group is refused as a whole: "
                          f"{exc}")
        except Exception as exc:  # noqa: BLE001 - never let one escape
            return refuse(f"root 0x{root_id:02X}'s group is refused as a whole: "
                          f"unexpected {type(exc).__name__} porting companion "
                          f"0x{node.type_id:02X}: {exc}")
        if record.target_type_id is not None:
            used.add(record.target_type_id)
        if not record.reused_memo:
            session.companion_memo[(record.source_world,
                                    record.source_type_id)] = record
        records.append(record)

    # --- 6. commit --------------------------------------------------------
    prs_growth = len(session.prs) - len(stage.prs)
    prt_growth = len(session.prt) - len(stage.prt)
    bef_growth = sum(len(r.blob) + 8 for r in records
                     if r.injected and r.blob is not None)
    outcome = GroupOutcome(ok=True, root_type_id=root_id,
                           records=tuple(records), prs_growth=prs_growth,
                           prt_growth=prt_growth, bef_growth=bef_growth)
    result.companion_count = len(records)
    result.companion_type_ids = tuple(r.source_type_id for r in records)
    result.companion_injected = outcome.injected_count
    for record in records:
        _log(session, f"root 0x{root_id:02X} <- {record.describe()}")
    _log(session, f"root 0x{root_id:02X} group COMMITTED: "
                  f"{len(records)} companion(s), {outcome.injected_count} "
                  f"injected, +{prs_growth} B PRS, +{prt_growth} B PRT, "
                  f"+{bef_growth} B BEF (blob bytes plus one 8-byte TOC entry "
                  f"each)")
    return outcome
