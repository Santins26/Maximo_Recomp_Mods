"""
Cross-world mesh porting for .PRS files, built on prs_mesh_parser.py.

*** MESH-TABLE PORTING IS VINDICATED. BLOB OFFSET 22 IS STILL DISCREDITED. ***

A BEF blob DOES name its meshes, just not at offset 22. The material section at
``[blob+0x20, blob+0x24)`` is framed ``[record_count u32]`` then per record
``[len u32][body]``, and each body opens with one or two 5-byte resource
references ``[rec_type u8][u32 = flag<<24 | index]`` (index = bits 0-23,
category = bits 24-30, is_global = bit 31). rec_type 0x05 carries a CATEGORY-1
reference, which IS a positional index into that world's .PRS MESH TABLE
(``max_local_cat1 == mesh_count-1`` in all five worlds); rec_type 0x04 carries
CATEGORY-2, an index into that world's .PRS ANIM TABLE. Proof for the mesh
half: Dark_Knight 0x53's CASTLE cat-1 refs are 25, 25, 26, and CASTLE.PRS
mesh[25] is the tag-6, 132,900-byte, 34-bone, 31-render-pass PSkeleton with
textures exactly {33, 34} -- the entry independently identified as his mesh.

So the extract / insert / gate machinery in this module is the RIGHT machinery;
it was merely being handed indices from the wrong field. Cross-world porting is
now driven by ``bef.parse_material_refs`` (see ``cross_world.WorldPortSession``).
What remains dead is offset 22 and everything that reads or writes it.

``MESH_INDEX_OFFSET = 22``, ``get_bef_mesh_index``, ``patch_bef_mesh_index``,
``port_enemy_mesh_and_reindex`` and the whole extract/insert porting pipeline
that consumes them assume a BEF blob's u16 at blob offset 22 is a POSITIONAL
index into a world's .PRS mesh table. MEASUREMENT DISPROVES THIS. Inside a
single GRAVE.BEF, seven native enemies all carry u16@22 = 25:

    Zombie_Crocodile 0x0D, Axe_Skeleton 0x2F, Raven 0x37, Basic_Skeleton 0x47,
    Guard_Skeleton 0x54, Torso_Zombie 0x6A, Ghost 0x6B

Basic_Skeleton and Guard_Skeleton both render CORRECTLY in vanilla and look
nothing alike, so 25 cannot be a shared model index. Three further natives share
the value 30. The neighbouring u16@20 is not an entry tag either (3 for 50 of 54
enemy entries, 6 for the other four, never 1). GLOBAL.PRS is not the missing
table: it has tag-1 props at 25 and 30 and cannot even be walked (32 resyncs,
the parse dies at entry 126 of 164, ending 965 KB short of its Anim anchor).

NOTHING DOWNSTREAM OF BLOB OFFSET 22 SHOULD BE TRUSTED. Every port performed
through this path extracted whatever object happened to occupy an unrelated
index, spliced it in byte-exactly, and rewrote a field that means something
else. In game the enemies spawn wearing arbitrary props. Those functions are
kept, not deleted, so the finding stays on record -- but nothing calls them any
more: ``cross_world`` drives porting from material refs instead.

PRECISELY WHAT IS DISCREDITED, AND WHAT IS NOT
  DISCREDITED: the MEANING of blob offset 22 (and hence any claim that a given
    enemy "is" mesh[N]), ``get_bef_mesh_index``, ``patch_bef_mesh_index``,
    ``port_enemy_mesh_and_reindex``, and the extract-splice-reindex pipeline as
    a way to give an enemy its model. Also the Session 5/6 "Dark_Knight is
    mesh[25]" identification below: 25 was read out of this same field.
  STILL SOUND: the .PRS container work, which never touches offset 22 --
    ``find_entry_bounds``, ``walk_mesh_table``, ``extract_mesh_entry`` /
    ``extract_mesh_entry_from_buffer``, ``insert_mesh_entry``,
    ``find_anim_table_anchor``, ``round_trip_entry`` and the trustworthiness
    gate (``assess_target_table`` / ``assess_source_entry``). Those are
    independently verified against the files themselves: the table walk, the
    mesh-count field, the entry byte ranges, the round-trip check and the
    agreement between the walk's end and the independent Anim-table anchor all
    still hold, and remain useful for any future approach.

Extracts a full top-level mesh-table entry (a PMeshNode or PSkeleton subtree,
including every nested bone/fragment/shader/render-pass byte) from a source
world's .PRS and inserts it into a target world's .PRS, growing the file.

Safety: the mesh table is immediately followed by the Anim table with no
gap and no offset directory anywhere in the file (confirmed: PTextureManager
and PResourceManager's loaders only ever call sequential file->Read, never
Seek). Inserting new bytes right at the mesh-table/anim-table boundary and
incrementing the mesh count field is therefore safe -- everything after the
insertion point simply shifts later in the file, and nothing references it
by absolute offset.

Verified end-to-end (see research/CROSSWORLD_TEXTURE_RESEARCH.md Session 6):
ported Dark_Knight's full mesh subtree (mesh[25], 132,900 bytes) from
CASTLE.PRS into GRAVE.PRS. Confirmed via full byte-level diff:
  - Only the mesh-count field (offset 0x20) and the newly-inserted region
    changed; every other byte, before and after the insertion point, is
    provably identical to the original file (just shifted after the
    insertion point).
  - The inserted bytes exactly match the extracted source entry.
  - The patched file re-parses cleanly in full (all original entries plus
    the new one), and the new entry's texture references (33, 34) are
    intact.
  - The corresponding BEF blob's mesh-index field (offset 22, a u16) was
    repatched to point at the new index and verified round-trip correct.
"""
from __future__ import annotations

import struct
from .prs_mesh_parser import Reader, PRSParseError, load_mesh_node, load_skeleton, _resync_toplevel_tag


def find_entry_bounds(data, index):
    """Return (start_offset, end_offset, tag) for mesh-table entry `index`,
    where start_offset is the position of that entry's own leading tag."""
    r = Reader(data, 0x20)
    count = r.u32()
    if index >= count:
        raise ValueError(f"entry {index} out of range (table has {count} entries)")
    for i in range(count):
        entry_start = r.off
        tag = r.u32()
        path = f"mesh[{i}]@0x{entry_start:X}"
        try:
            if tag == 1:
                load_mesh_node(r, path)
            elif tag == 6:
                load_skeleton(r, path)
            else:
                raise PRSParseError(f"unknown tag {tag}")
        except PRSParseError:
            r.off = _resync_toplevel_tag(r, path)
        if i == index:
            return entry_start, r.off, tag
    raise ValueError(f"never reached entry {index}")


def extract_mesh_entry_from_buffer(prs_bytes, index):
    """Return (entry_bytes, tag) for mesh-table entry `index` of an
    in-memory .PRS buffer, including the entry's leading tag.

    Buffer-taking twin of extract_mesh_entry: the porting flow needs to
    pull a texture-patched entry back out of a .PRS it is holding in
    memory, with no disk round trip and no temp file. Same walking logic,
    same return shape -- extract_mesh_entry is a thin wrapper over this.
    """
    start, end, tag = find_entry_bounds(prs_bytes, index)
    return prs_bytes[start:end], tag


def extract_mesh_entry(source_path, index):
    """Return the raw bytes of mesh-table entry `index` (including its
    leading tag) from a source world's .PRS file. Reads the file and
    delegates to extract_mesh_entry_from_buffer."""
    with open(source_path, 'rb') as f:
        data = f.read()
    return extract_mesh_entry_from_buffer(data, index)


def insert_mesh_entry(target_data, entry_bytes):
    """Insert `entry_bytes` (as returned by extract_mesh_entry) into a
    target world's .PRS bytes, right at the end of its mesh table (== the
    start of its Anim table). Increments the mesh count at file offset
    0x20. Returns (new_data, new_index) where new_index is the position
    the ported entry now occupies in the target's mesh table.
    """
    count = struct.unpack_from('<I', target_data, 0x20)[0]
    # Find where the target's own mesh table ends the same way we found
    # the source's entry bounds -- walk every existing entry.
    r = Reader(target_data, 0x20)
    r.u32()  # count, already have it
    for i in range(count):
        entry_start = r.off
        tag = r.u32()
        path = f"mesh[{i}]@0x{entry_start:X}"
        try:
            if tag == 1:
                load_mesh_node(r, path)
            elif tag == 6:
                load_skeleton(r, path)
            else:
                raise PRSParseError(f"unknown tag {tag}")
        except PRSParseError:
            r.off = _resync_toplevel_tag(r, path)
    insert_at = r.off  # end of mesh table == start of Anim table

    new_data = target_data[:insert_at] + entry_bytes + target_data[insert_at:]
    new_data = bytearray(new_data)
    struct.pack_into('<I', new_data, 0x20, count + 1)
    return bytes(new_data), count  # new entry lands at index == old count


def port_mesh_entry(source_path, source_index, target_path):
    """Full port: extract `source_index` from `source_path` and insert it
    into `target_path`'s bytes (read from disk). Returns (new_target_bytes,
    new_index, tag) -- caller writes new_target_bytes back to target_path
    (or a copy of it) and must update any BEF blob referencing the enemy to
    point at `new_index` instead of `source_index` (see
    patch_bef_mesh_index below).
    """
    entry_bytes, tag = extract_mesh_entry(source_path, source_index)
    target_data = open(target_path, 'rb').read()
    new_data, new_index = insert_mesh_entry(target_data, entry_bytes)
    return new_data, new_index, tag


# *** NOT A MESH-TABLE INDEX. *** See the module docstring: seven native
# GRAVE.BEF enemies all read 25 here, two of which render correctly in vanilla
# and look nothing alike. The real meaning of this u16 is UNKNOWN. Kept only so
# the parked machinery still imports and so the finding stays recorded.
MESH_INDEX_OFFSET = 22  # u16, within a BEF blob's fixed header


def get_bef_mesh_index(blob):
    """Read the u16 at BEF blob offset 22.

    *** UNFOUNDED PREMISE -- DO NOT TREAT THE RESULT AS A MESH INDEX. ***
    This was believed to be a positional .PRS mesh-table index, "confirmed" for
    Dark_Knight 0x53 in CASTLE.BEF because the field held 25 and CASTLE.PRS
    mesh[25] looked plausible. That identification is dead: in a single
    GRAVE.BEF, 0x0D, 0x2F, 0x37, 0x47, 0x54, 0x6A and 0x6B ALL hold 25, and
    Basic_Skeleton 0x47 / Guard_Skeleton 0x54 render correctly in vanilla while
    looking nothing alike. Three more natives share 30. Nothing downstream of
    this value is trustworthy. See the module docstring.
    """
    return struct.unpack_from('<H', blob, MESH_INDEX_OFFSET)[0]


def patch_bef_mesh_index(blob, new_index):
    """Return a copy of a BEF blob with the u16 at offset 22 rewritten to
    `new_index`. Fixed-size field, pure in-place overwrite -- blob length is
    unchanged.

    *** UNFOUNDED PREMISE. *** This does NOT repoint an enemy at a mesh: offset
    22 is not a mesh-table index (see the module docstring). Calling it
    overwrites a field of unknown meaning with a table position, which is how
    ported enemies ended up spawning as arbitrary props.
    """
    out = bytearray(blob)
    struct.pack_into('<H', out, MESH_INDEX_OFFSET, new_index)
    return bytes(out)


def port_enemy_mesh_and_reindex(source_prs_path, source_mesh_index,
                                 target_prs_path, bef_blob):
    """Full pipeline for one enemy: port their mesh subtree from the source
    world's .PRS into the target world's .PRS, and return a patched copy of
    their BEF blob pointing at the new mesh index.

    *** UNFOUNDED PREMISE -- DO NOT USE TO GIVE AN ENEMY A MODEL. *** Both ends
    of this pipeline rest on BEF blob offset 22 being a mesh-table index, which
    measurement disproves (module docstring). `source_mesh_index` selects an
    unrelated entry and the blob patch rewrites a field meaning something else.
    The .PRS container operations it calls are sound; the identification is not.

    Returns (new_target_prs_bytes, new_bef_blob, new_mesh_index).
    Caller is responsible for writing new_target_prs_bytes to the target
    world's .PRS (via the same patched-output-folder mechanism
    iso_patcher.py already uses for other grown assets) and using
    new_bef_blob in place of the original when injecting this enemy's BEF
    entry into the target world (see bef.py's inject_cross_world_enemies).

    NOTE on textures: this does NOT port textures. Confirmed for
    Dark_Knight (mesh[25] in CASTLE.PRS): his render-pass texture indices
    (33, 34) are small enough to near-certainly be GLOBAL.PRT indices
    (GLOBAL's ~160 textures are loaded first in the shared cache, before
    any world-local ones), meaning they're already present in every
    world's texture cache automatically -- no texture porting needed for
    him. This assumption should be re-checked per-enemy: if a ported
    enemy's texture ids turn out to be large (>= GLOBAL's texture count),
    they're world-local indices instead, and texture_port.py's PRT porting
    (or prs_mesh_parser.patch_texture_ids to remap them to existing target
    textures) is needed too.
    """
    entry_bytes, tag = extract_mesh_entry(source_prs_path, source_mesh_index)
    target_data = open(target_prs_path, 'rb').read()
    new_target_data, new_index = insert_mesh_entry(target_data, entry_bytes)
    new_blob = patch_bef_mesh_index(bef_blob, new_index)
    return new_target_data, new_blob, new_index

# ---------------------------------------------------------------------------
# Trustworthiness instrumentation
#
# WHY THIS EXISTS
#   find_entry_bounds / insert_mesh_entry locate entry boundaries by WALKING
#   the table with prs_mesh_parser. That walk USED TO fall back to a bounded
#   forward scan whenever a structure it did not understand showed up, and after
#   such a resync its idea of where something ends was a GUESS -- a wrong-length
#   entry spliced into a target mesh table desynchronises the engine's own
#   sequential read of that table for every entry after it.
#
#   THE RESYNC PATHS ARE GONE. The two structures behind every observed resync
#   are now MEASURED from the engine's loader chain (PSTAnim channel count
#   u16@+0, PVertexAnim element count u32@+4 -- see
#   research/PRS_MESH_TABLE_ENGINE_LAYOUTS.md), all 11 shipped .PRS files walk
#   with ZERO resyncs, and the `_resync_*` names now RAISE instead of scanning.
#   So a desynced walk REFUSES, and this instrumentation records that refusal
#   (as a `toplevel_fail` plus `resync_failed` event) rather than papering over
#   it. The gate below is unchanged and still demands a resync-free walk; it
#   simply has nothing to refuse on the retail files any more.
#
#   Nothing below changes prs_mesh_parser's behaviour. `resync_recorder` wraps
#   its four resync helpers with pass-through callables that only append to a
#   list, and restores the originals on exit. The parser's parsing logic is
#   untouched.
#
# WHAT "TRUSTWORTHY" MEANS HERE
#   * A resync INSIDE the entry being ported makes that entry's byte range a
#     guess -> untrustworthy.
#   * A TOP-LEVEL resync at or before the wanted index breaks the mapping from
#     table position to index, so "mesh[N]" may not be the engine's mesh[N]
#     -> untrustworthy. An inner resync in an EARLIER entry does not, provided
#     every following entry still starts on a valid top-level tag and parses
#     clean, which the walk verifies.
#   * The extracted slice must re-parse in isolation, consuming exactly its own
#     length with no leftover bytes and no resync.
#   * For the TARGET file the whole table must walk with zero resyncs, and the
#     walk-derived table end must agree with an INDEPENDENT anchor (the Anim
#     table's first record signature; see find_anim_table_anchor), because the
#     insertion point is that table end.
# ---------------------------------------------------------------------------
import contextlib
from dataclasses import dataclass, field

from . import prs_mesh_parser as _pmp

_RESYNC_HELPERS = (
    "_resync_toplevel_tag",
    "_resync_tag",
    "_resync_tag_bone",
    "_resync_any_tag",
)

# First record of the Anim table that follows the mesh table. Every retail
# .PRS holds these four bytes at the head of each anim record, and the two u32
# fields immediately before the FIRST one are the anim count and a constant
# 0x0A, so `first_signature - 12` is an anchor for the end of the mesh table
# that is completely independent of the mesh parser.
ANIM_RECORD_SIGNATURE = b"TFA\xff"
_ANIM_HEADER_BYTES = 12


@contextlib.contextmanager
def resync_recorder():
    """Record every resync prs_mesh_parser performs inside the block.

    Yields a list that receives ``(helper_name, path)`` per resync. Purely
    additive: each helper is replaced by a wrapper that appends and then calls
    the original, and the originals are restored on exit (including on error).
    """
    events: list[tuple[str, str]] = []
    saved = {name: getattr(_pmp, name) for name in _RESYNC_HELPERS}

    def wrap(name, func):
        def wrapper(r, path, *args, **kwargs):
            events.append((name, path))
            return func(r, path, *args, **kwargs)
        return wrapper

    for name, func in saved.items():
        setattr(_pmp, name, wrap(name, func))
    try:
        yield events
    finally:
        for name, func in saved.items():
            setattr(_pmp, name, func)


@dataclass
class MeshTableWalk:
    """One instrumented walk of a .PRS mesh table."""

    count: int
    #: (index, start, end, tag) per entry, in table order.
    bounds: list[tuple[int, int, int, int]] = field(default_factory=list)
    #: (index, kind, detail) -- kind is a helper name or "toplevel_fail".
    events: list[tuple[int, str, str]] = field(default_factory=list)
    end: int = 0

    def resyncs_for(self, index: int) -> list[tuple[int, str, str]]:
        return [e for e in self.events if e[0] == index]

    def toplevel_resyncs_upto(self, index: int) -> list[tuple[int, str, str]]:
        return [e for e in self.events
                if e[0] <= index
                and e[1] in ("toplevel_fail", "_resync_toplevel_tag")]

    @property
    def clean(self) -> bool:
        return not self.events


def walk_mesh_table(data, stop_after=None) -> MeshTableWalk:
    """Walk ``data``'s mesh table, recording every entry boundary and every
    resync, attributing each resync to the entry index it happened in.

    ``stop_after``: stop once that index has been walked (the source-side gate
    only cares about the prefix that determines the wanted entry's position).

    Uses exactly the same loop as ``find_entry_bounds`` / ``insert_mesh_entry``,
    so its boundaries are the ones the porter would use.
    """
    r = Reader(data, 0x20)
    count = r.u32()
    walk = MeshTableWalk(count=count)
    with resync_recorder() as events:
        for i in range(count):
            seen = len(events)
            entry_start = r.off
            tag = r.u32()
            path = f"mesh[{i}]@0x{entry_start:X}"
            try:
                if tag == 1:
                    load_mesh_node(r, path)
                elif tag == 6:
                    load_skeleton(r, path)
                else:
                    raise PRSParseError(f"unknown mesh-table tag {tag}")
            except PRSParseError as exc:
                walk.events.append((i, "toplevel_fail", str(exc)))
                try:
                    r.off = _resync_toplevel_tag(r, path)
                except PRSParseError as exc2:
                    # Even the bounded scan found nothing plausible. Record it
                    # and stop: there is no defensible position to continue
                    # from, and a gate caller only needs to know the walk is
                    # not trustworthy. Never raises out of a walk.
                    walk.events.append((i, "resync_failed", str(exc2)))
                    for name, epath in events[seen:]:
                        walk.events.append((i, name, epath))
                    walk.bounds.append((i, entry_start, r.off, tag))
                    break
            for name, epath in events[seen:]:
                walk.events.append((i, name, epath))
            walk.bounds.append((i, entry_start, r.off, tag))
            if stop_after is not None and i >= stop_after:
                break
    walk.end = r.off
    return walk


def find_first_anim_signature(data):
    """Offset of the first ``TFA\\xff`` in ``data``, or ``None``.

    ``TFA\\xff`` is the MEA (tag ``0x0A``) record magic and NOTHING ELSE. It is
    kept as a corroborating scan only; it is NOT an anchor. See
    ``find_anim_table_anchor``.
    """
    pos = data.find(ANIM_RECORD_SIGNATURE)
    return None if pos < 0 else pos


def find_anim_table_anchor(data):
    """Anchor for the anim table == the end of the mesh table, or ``None``.

    Derived from the MESH WALK'S END and corroborated by the format's own
    post-condition: the u32 there must be the next resource table's type id
    (``prs_mesh_parser.NEXT_RESOURCE_TABLE_TYPE`` == 2). Never raises; ``None``
    means "not locatable", which every caller already treats as a refusal.

    *** WHY THIS IS NO LONGER THE ``TFA\\xff`` SCAN. *** This used to return the
    first ``TFA\\xff`` occurrence minus 12. ``TFA\\xff`` is the MEA (tag ``0x0A``)
    record magic, and an SKA (tag ``0x0C``) record carries no magic at all, so in
    an archive whose table STARTS with SKA records the scan skips straight past
    them and lands in the middle of the table. MEASURED on all 12 shipped
    ``.PRS`` files: in the seven whose first record is MEA the scan's result and
    the mesh walk's end are byte-identical, and in the five whose first record is
    SKA the scan is 160-621 KB PAST the real table start:

        | file         | mesh walk end | old TFA anchor | first tag |
        |--------------|---------------|----------------|-----------|
        | GRAVE_B.PRS  |     1,808,536 |      2,378,397 |      0x0C |
        | ICE_B.PRS    |     1,297,252 |      1,918,743 |      0x0C |
        | SWAMP_B.PRS  |       924,740 |      1,411,513 |      0x0C |
        | UNDER_B.PRS  |     1,762,460 |      2,230,797 |      0x0C |
        | CASTLE_K.PRS |     1,153,184 |      1,313,136 |      0x0C |
        | CASTLE_Q.PRS |     1,035,956 |      1,322,232 |      0x0C |

    Those six are the FOUR BOSS ARENAS plus the king and queen arenas, and the
    wrong anchor made every one of them fail ``assess_target_table`` (the walk
    end "disagreed" with the anchor) and ``assess_anim_table`` (the region was
    located inside its own table), which refused EVERY cross-world arrival into
    EVERY arena at the trust gates. ``boss_inject.derive_anim_table`` already
    worked around this locally; the anchor itself is now correct, so no caller
    needs to.

    The mesh walk's end is not a self-confirming guess: it is the same cursor
    ``insert_mesh_entry`` writes at, and the type-id word at it is an
    independent format post-condition that a walk which absorbed or lost bytes
    cannot satisfy.
    """
    walk = walk_mesh_table(data)
    end = walk.end
    if end < 0 or end + _ANIM_HEADER_BYTES > len(data):
        return None
    type_word = struct.unpack_from("<I", data, end)[0]
    if type_word != _pmp.NEXT_RESOURCE_TABLE_TYPE:
        return None
    return end


def round_trip_entry(entry_bytes):
    """Re-parse ``entry_bytes`` in ISOLATION as a one-entry mesh table.

    Returns ``(consumed, resync_events)``. ``consumed == len(entry_bytes)`` with
    an empty event list is the only trustworthy outcome: any leftover byte means
    the slice's length is wrong, and any resync means the walk that produced the
    slice was guessing.
    """
    buf = bytes(0x20) + struct.pack("<I", 1) + bytes(entry_bytes)
    walk = walk_mesh_table(buf)
    return walk.end - 0x24, [(k, p) for _i, k, p in walk.events]


@dataclass
class TrustVerdict:
    """Result of a trustworthiness gate. ``ok`` False means REFUSE."""

    ok: bool
    reasons: list[str] = field(default_factory=list)
    details: list[str] = field(default_factory=list)
    walk: MeshTableWalk | None = None
    entry_bytes: bytes | None = None
    tag: int | None = None

    @property
    def reason(self) -> str:
        return "; ".join(self.reasons)


def assess_target_table(data, *, label="target") -> TrustVerdict:
    """Gate the TARGET .PRS: the insertion point is this walk's end, so the
    whole table must walk with zero resyncs AND the end must be corroborated
    independently of the mesh parser's own accounting.

    THE CORROBORATION IS THE FORMAT'S OWN POST-CONDITION, NOT A SIGNATURE SCAN:
    the u32 at the walk's end must be the next resource table's type id
    (``prs_mesh_parser.NEXT_RESOURCE_TABLE_TYPE`` == 2). MEASURED on all 12
    shipped ``.PRS`` files, the mesh table finishes exactly at a byte where that
    word reads 2; a walk that absorbed a byte, lost an entry or resynced does
    not land there. This is the same post-condition
    ``prs_mesh_parser.check_end_of_table`` states.

    *** THIS NO LONGER COMPARES AGAINST ``find_anim_table_anchor``. *** That
    comparison used to be the gate's only corroboration and it was a ``TFA\\xff``
    scan, which is MEA-record magic; SKA-first archives (all four boss arenas
    plus ``CASTLE_K`` / ``CASTLE_Q``) therefore "disagreed" with their own
    correct walk end by 160-621 KB and FAILED this gate, which refused every
    cross-world arrival into every arena. The anchor is now derived from this
    same walk end, so comparing the two would be tautological.

    WHAT THIS GATE DELIBERATELY DOES NOT DO is re-walk the anim record chain to
    check it tiles onto the archive trailer. That is ``anim_port`` territory --
    ``assess_anim_table`` already walks the chain, checks the tiling and
    round-trips every record, and ``cross_world`` consults it as ``anim_trust()``
    alongside this gate. Duplicating it here bought no extra safety and made the
    mesh gate unable to judge a mesh table that is not followed by a complete
    archive tail.
    """
    walk = walk_mesh_table(data)
    reasons: list[str] = []
    if walk.events:
        kinds = sorted({k for _i, k, _p in walk.events})
        indices = sorted({i for i, _k, _p in walk.events})
        reasons.append(
            f"{label} mesh-table walk needed {len(walk.events)} resync(s) "
            f"{kinds} at entr{'y' if len(indices) == 1 else 'ies'} {indices}, "
            f"so the end of the table (== the insertion point) is a guess")

    type_note = "not reached"
    if walk.end + 4 > len(data):
        reasons.append(
            f"{label} mesh-table walk ended at {walk.end}, which leaves no room "
            f"for the following resource table's type id in a {len(data)}-byte "
            f"buffer, so the walk ran off the end")
    else:
        type_word = struct.unpack_from("<I", data, walk.end)[0]
        type_note = f"type word {type_word}"
        if type_word != _pmp.NEXT_RESOURCE_TABLE_TYPE:
            reasons.append(
                f"{label} mesh-table walk ended at {walk.end} but the u32 "
                f"there reads {type_word} (0x{type_word:X}), not the following "
                f"resource table's type id "
                f"{_pmp.NEXT_RESOURCE_TABLE_TYPE} -- the walk did not end where "
                f"the next table begins, so the insertion point is wrong")

    details = [f"{label}: {walk.count} entries, walk end {walk.end}, "
               f"{len(walk.events)} resync(s), corroboration: {type_note}"]
    return TrustVerdict(ok=not reasons, reasons=reasons, details=details,
                        walk=walk)


def assess_source_entry(data, index, *, label="source") -> TrustVerdict:
    """Gate the SOURCE entry: index alignment intact, entry parsed with no
    resync, and the extracted slice round-trips in isolation.

    On success ``entry_bytes`` carries the extracted slice, so callers never
    need to walk the source table twice.
    """
    reasons: list[str] = []
    walk = walk_mesh_table(data, stop_after=index)
    if index >= walk.count:
        return TrustVerdict(
            ok=False, walk=walk,
            reasons=[f"{label} mesh[{index}] is out of range "
                     f"(table declares {walk.count} entries)"])

    top = walk.toplevel_resyncs_upto(index)
    if top:
        reasons.append(
            f"{label} mesh table needed a TOP-LEVEL resync at entr"
            f"{'y' if len(top) == 1 else 'ies'} "
            f"{sorted({i for i, _k, _p in top})} at or before {index}, so "
            f"table position no longer maps reliably to mesh index {index}")

    own = walk.resyncs_for(index)
    if own:
        reasons.append(
            f"{label} mesh[{index}] itself needed {len(own)} resync(s) "
            f"{sorted({k for _i, k, _p in own})}, so its byte range is a guess")

    wanted = [b for b in walk.bounds if b[0] == index]
    if not wanted:
        reasons.append(
            f"{label} mesh table walk never reached mesh[{index}] "
            f"({len(walk.bounds)} entr{'y' if len(walk.bounds) == 1 else 'ies'} "
            f"walked before it gave up)")
        return TrustVerdict(ok=False, reasons=reasons, walk=walk)
    _i, start, end, tag = wanted[0]
    entry_bytes = bytes(data[start:end])
    consumed, rt_events = round_trip_entry(entry_bytes)
    if consumed != len(entry_bytes) or rt_events:
        reasons.append(
            f"{label} mesh[{index}] does not round-trip in isolation: "
            f"{len(entry_bytes)} byte slice, {consumed} consumed, "
            f"{len(entry_bytes) - consumed} left over, {len(rt_events)} "
            f"resync(s)")

    details = [f"{label} mesh[{index}] tag={tag} [{start},{end}) "
               f"{len(entry_bytes)} B, round-trip consumed {consumed}, "
               f"{len(own)} own resync(s)"]
    return TrustVerdict(ok=not reasons, reasons=reasons, details=details,
                        walk=walk, entry_bytes=entry_bytes, tag=tag)


# ---------------------------------------------------------------------------
# Cross-world identity: the stable per-entry signature, and the anim count
# ---------------------------------------------------------------------------
#
# The SAME asset sits at a DIFFERENT mesh-table index in every world, and its
# bytes are NOT identical across worlds -- each world bakes its own
# texture-cache indices into the render passes. So a content hash cannot match
# an asset across worlds, while (length, tag, bone_count, render_pass_count) can
# and does. Use `mesh_entry_signature` for identity, never a digest.


@contextlib.contextmanager
def bone_counter():
    """Count every ``PBone`` the parser loads inside the block.

    Yields a one-element list holding the running count. Purely additive:
    ``prs_mesh_parser.load_bone`` is wrapped by a pass-through that increments
    and then delegates, and the original is restored on exit.
    """
    counter = [0]
    original = _pmp.load_bone

    def wrapper(r, path, *args, **kwargs):
        counter[0] += 1
        return original(r, path, *args, **kwargs)

    _pmp.load_bone = wrapper
    try:
        yield counter
    finally:
        _pmp.load_bone = original


def mesh_entry_signature(prs_bytes, index):
    """Return ``(length, tag, bone_count, render_pass_count)`` for mesh-table
    entry ``index`` of an in-memory .PRS.

    This tuple is THE cross-world key for "is this the same asset?". It is
    stable where the bytes are not: Dark_Knight's CASTLE mesh[25] and the same
    skeleton in another world agree on all four fields while differing in the
    baked texture-cache indices inside their render passes.

    The entry is parsed in ISOLATION (as a synthetic one-entry table), so the
    bone and render-pass counts belong to this entry only and cannot pick up
    anything from its neighbours.
    """
    entry_bytes, tag = extract_mesh_entry_from_buffer(prs_bytes, index)
    buf = bytes(0x20) + struct.pack("<I", 1) + entry_bytes
    with _pmp.parser_scope():
        with bone_counter() as bones:
            reader = Reader(buf, 0x20)
            _pmp.load_mesh_res_table(reader)
        passes = len(_pmp.render_passes)
    return (len(entry_bytes), tag, bones[0], passes)


@contextlib.contextmanager
def bone_name_collector():
    """Collect every ``PBone``'s NAME, in load order, inside the block.

    ``prs_mesh_parser.load_bone`` documents its first 96 bytes as
    "transform+name"; the name is the 32-byte NUL-padded field at the START of
    that block (offset 0), which is read here before delegating to the real
    loader. Verified against CASTLE.PRS mesh[25] -- Dark_Knight's skeleton --
    which yields exactly 34 names (``hip, l_thigh, l_shin, l_foot, ...``) for
    the 34 bones ``mesh_entry_signature`` counts, matching the bone naming
    convention `role-notes.md` measured in his own SKA anim records.

    Purely additive and restored on exit, exactly like ``bone_counter``.
    """
    names: list[str] = []
    original = _pmp.load_bone

    def wrapper(r, path, *args, **kwargs):
        raw = bytes(r.data[r.off:r.off + 32]).split(b"\x00", 1)[0]
        names.append(raw.decode("ascii", "replace"))
        return original(r, path, *args, **kwargs)

    _pmp.load_bone = wrapper
    try:
        yield names
    finally:
        _pmp.load_bone = original


@contextlib.contextmanager
def mesh_node_name_collector():
    """Collect every ``PMeshNode``'s NAME, in load order, inside the block.

    Same 32-byte NUL-padded field at the start of ``load_mesh_node``'s block as
    ``bone_name_collector`` reads out of ``load_bone``'s. These are the names a
    MEA (tag ``0x0A``) anim record addresses -- the engine's own local for that
    field is ``aszAnimTreeMeshNodeName`` (`role-notes.md`) -- so they are a
    DIFFERENT namespace from bone names and both are needed to answer "can this
    enemy's own records bind to its own ported assets?".

    Verified on CASTLE.PRS: mesh[26] (Dark_Knight's head group, a tag-1 entry
    with 0 bones) yields ``node#8, helmet, headgroup, nail, ...``, which is
    exactly the name set CASTLE anim[46] addresses.
    """
    names: list[str] = []
    original = _pmp.load_mesh_node

    def wrapper(r, path, *args, **kwargs):
        raw = bytes(r.data[r.off:r.off + 32]).split(b"\x00", 1)[0]
        names.append(raw.decode("ascii", "replace"))
        return original(r, path, *args, **kwargs)

    _pmp.load_mesh_node = wrapper
    try:
        yield names
    finally:
        _pmp.load_mesh_node = original


def mesh_entry_anim_target_names(prs_bytes, index):
    """Every name an anim record of this entry's own enemy could address.

    ``(bone_names, mesh_node_names)`` for mesh-table entry ``index``, parsed in
    isolation. SKA records name BONES, MEA records name MESH NODES, and an
    enemy commonly owns one skinned skeleton entry plus a tag-1 node group, so
    the porter's compatibility check needs the union of both.
    """
    entry_bytes, _tag = extract_mesh_entry_from_buffer(prs_bytes, index)
    buf = bytes(0x20) + struct.pack("<I", 1) + entry_bytes
    with _pmp.parser_scope():
        with bone_name_collector() as bones:
            with mesh_node_name_collector() as nodes:
                reader = Reader(buf, 0x20)
                _pmp.load_mesh_res_table(reader)
    return tuple(bones), tuple(nodes)


def mesh_entry_bone_names(prs_bytes, index):
    """Ordered bone names of mesh-table entry ``index`` of an in-memory .PRS.

    Returns a tuple; empty for an entry with no skeleton (a 0-bone prop).

    THIS IS WHY THE ANIM PORT CAN BE CHECKED RATHER THAN ASSUMED. An SKA anim
    record addresses bones BY NAME (`role-notes.md`, task 8.4), so "can this
    record drive that rig?" is a name-set predicate over the rig's own names --
    and those names are right here in the mesh entry. Without this the porter
    would have to be handed the names taken from the records themselves, which
    would make the compatibility check vacuous by construction.

    The entry is parsed in ISOLATION as a synthetic one-entry table, exactly as
    ``mesh_entry_signature`` does, so the names belong to this entry only.
    """
    entry_bytes, _tag = extract_mesh_entry_from_buffer(prs_bytes, index)
    buf = bytes(0x20) + struct.pack("<I", 1) + entry_bytes
    with _pmp.parser_scope():
        with bone_name_collector() as names:
            reader = Reader(buf, 0x20)
            _pmp.load_mesh_res_table(reader)
    return tuple(names)


def read_anim_count(data):
    """Number of entries in a .PRS Anim table, or ``None`` if unreadable.

    The anim header is ``[type id 2 u32][anim_count u32]`` at the end of the
    mesh table, which is exactly what ``find_anim_table_anchor`` locates, and
    the word after it is the FIRST RECORD'S TYPE TAG. Measured per world: GRAVE
    53, CASTLE 109, SWAMP 78, ICE 82, UNDER 90 -- and in every world the largest
    LOCAL CATEGORY-2 index found in any BEF blob is ``anim_count - 1`` (UNDER is
    the single exception: one unused entry). That is what identifies category 2
    as the anim table.

    *** THE WORD AT ``anchor + 8`` IS A RECORD TAG, NOT A CONSTANT. *** This
    used to require it to be ``0x0A`` and returned ``None`` otherwise. ``0x0A``
    is MEA and ``0x0C`` is SKA, both of which ``LoadAnimResTable`` accepts, so
    demanding ``0x0A`` rejected every SKA-first archive -- all four boss arenas
    and both castle arenas -- and a ``None`` count is a REFUSAL at
    ``_check_local_anim_refs`` (no bound means no way to check a CATEGORY-2
    ref). The tag is now validated against the accepted SET and otherwise left
    alone.
    """
    anchor = find_anim_table_anchor(data)
    if anchor is None or anchor + _ANIM_HEADER_BYTES > len(data):
        return None
    count, first_tag = struct.unpack_from("<II", data, anchor + 4)
    # Kept as literals rather than importing anim_port.ACCEPTED_TAGS: anim_port
    # imports THIS module, so the dependency only runs one way.
    if first_tag not in (0x0A, 0x0C) or count <= 0 or count > 0xFFFF:
        return None
    return count
