"""Anim-table walker for world ``.PRS`` archives (task 5).

Mirrors ``mesh_port.py`` in shape and naming, and reuses its anchor helpers
rather than re-deriving them: ``find_anim_table_anchor``, ``read_anim_count``
and ``ANIM_RECORD_SIGNATURE`` all come from there. ``find_anim_table_anchor`` is
the END OF THE MESH TABLE (corroborated by the type-id word), not a ``TFA\xff``
scan -- ``TFA\xff`` is MEA magic and an SKA-first table has none.

THIS TASK (5.1) IS REGION LOCATION ONLY. What lands later:

* ``record_length`` -- the per-TYPE-TAG advance rules from the ELF: task 5.4.
* ``walk_anim_table`` / ``find_record_bounds`` / ``extract_record_from_buffer``:
  task 5.5.
* ``round_trip_record``: task 6.1.
* ``assess_anim_table`` / ``assess_source_record`` (the trust gate): task 6.3,
  landed. It passes for ALL FIVE worlds, where ``mesh_port``'s
  ``assess_target_table`` passes for GRAVE alone -- which is precisely why anim
  porting is viable into worlds mesh porting cannot target.
* ``append_anim_records`` and everything about porting: tasks 8-9. That is the
  consumer of ``AnimRegion.trailer`` -- see below.

Layout, as MEASURED and VALIDATED against all five worlds in task 3.3
(``.kiro/specs/anim-table-format/elf-notes.md`` is authoritative):

    anchor + 0   u32   2            -- THIS table's resource type id
    anchor + 4   u32   record count -- read by mesh_port.read_anim_count
    anchor + 8   u32   TYPE TAG of the FIRST RECORD

So **records start at anchor + 8**, not anchor + 12. The word at ``anchor + 8``
is the first record's tag, which the engine's own loader reads inside its
per-record loop (``LoadAnimResTable`` `0x00167FE4`), NOT a header constant.
``AnimRegion`` therefore reports it as ``first_tag`` and this module never
requires it to be ``0x0A`` (Requirement 1.4). It is ``0x0A`` (MEA) in the five
main worlds and ``0x0C`` (SKA) in all six ARENA archives, which is exactly why
neither this module nor ``mesh_port.read_anim_count`` may demand ``0x0A``.

The record region ends at ``len(data) - 16``. The final 16 bytes are a uniform
archive TRAILER, byte-identical in all five worlds::

    03 00 00 00 00 00 00 00 04 00 00 00 00 00 00 00

``AnimRegion`` exposes it verbatim because task 9's ``append_anim_records`` has
to reproduce it after the appended records (Requirement 8.4).

Measured ground truth (task 3.3, no tuning applied):

    world   size       anchor     count  records_start  records_end
    GRAVE   2,563,126  1,984,784     53      1,984,792    2,563,110
    CASTLE  4,199,541  2,834,628    109      2,834,636    4,199,525
    SWAMP   3,442,632  2,356,884     78      2,356,892    3,442,616
    ICE     3,082,301  2,143,444     82      2,143,452    3,082,285
    UNDER   3,641,513  2,516,188     90      2,516,196    3,641,497

UNDER declares **90**, not the 89 recorded elsewhere in the project: the file
says 90 and 90 records walk cleanly to the trailer, while 89 would leave a whole
20,656-byte record unconsumed.

No function here scans for markers or resyncs. A signature scan is exactly what
failed before (57% of records carry no magic), so every failure is a signal --
``AnimParseError`` or ``None`` -- never a guess.
"""
from __future__ import annotations

import enum
import struct
from dataclasses import dataclass, field

from .mesh_port import (
    ANIM_RECORD_SIGNATURE,
    find_anim_table_anchor,
    find_first_anim_signature,
    read_anim_count,
)

__all__ = [
    "ANIM_RECORD_SIGNATURE",
    "ANIM_HEADER_BYTES",
    "ANIM_TRAILER_BYTES",
    "ANIM_TRAILER",
    "TAG_MEA",
    "TAG_SKA",
    "ACCEPTED_TAGS",
    "AnimParseError",
    "AnimRegion",
    "find_anim_region",
    "AnimRecordInfo",
    "measure_record",
    "record_length",
    "MEA_MAGIC_WORD",
    "MEA_TFA_HEADER_BYTES",
    "MEA_RECORD_TERMINATOR",
    "MEA_TRACK_TERMINATOR",
    "MEA_NODE_MARKER",
    "SKA_RECORD_TERMINATOR",
]

#: Bytes between the anchor and the first record: ``[2][count]``. The third
#: word of the old 12-byte reading is the first record's tag, so it belongs to
#: the records, not the header.
ANIM_HEADER_BYTES = 8

#: Distance from the region anchor to the first ``TFA\xff`` magic, used ONLY by
#: the fallback in ``find_anim_region``. The magic sits 4 bytes into record 0
#: (after its tag word), and record 0 starts at ``anchor + ANIM_HEADER_BYTES``.
_TFA_HEADER_BYTES = ANIM_HEADER_BYTES + 4

#: Uniform archive trailer after the last record, identical in all five worlds.
ANIM_TRAILER_BYTES = 16
ANIM_TRAILER = bytes.fromhex("03000000000000000400000000000000")

#: The complete accepted TYPE TAG set. ``LoadAnimResTable`` compares the tag
#: against exactly these two immediates and hard-asserts on anything else
#: (elf-notes task 3.1), so task 5.4's ``record_length`` must raise
#: ``AnimParseError`` for any other value rather than skipping the record.
TAG_MEA = 0x0A  # PMeshNodeAnimDataTable, the TFA\xff form
TAG_SKA = 0x0C  # PSkeletonAnim, no magic at all
ACCEPTED_TAGS = frozenset((TAG_MEA, TAG_SKA))


class AnimParseError(Exception):
    """An anim table or record could not be parsed from its own bytes.

    Raised by region location for a structurally impossible buffer, and (from
    task 5.4) by ``record_length`` for an unknown TYPE TAG or an implausible
    length. ``walk_anim_table`` (task 5.5) catches it and turns it into a walk
    event, so no public walker function raises for malformed data.
    """


@dataclass(frozen=True)
class AnimRegion:
    """Where a world's anim records live, and what frames them.

    Attributes
    ----------
    anchor:
        ``find_anim_table_anchor(data)`` -- the END OF THE MESH TABLE, where the
        u32 is this table's resource type id (2). NOT a ``TFA\\xff`` scan: that
        magic belongs to MEA records only, so it misses an SKA-first table
        entirely (see ``mesh_port.find_anim_table_anchor``).
    count:
        Declared record count, the u32 at ``anchor + 4``.
    first_tag:
        The u32 at ``anchor + 8``. This is the FIRST RECORD'S TYPE TAG, reported
        as observed and never validated against ``0x0A`` (Requirement 1.4).
    records_start:
        First byte of record 0 == ``anchor + ANIM_HEADER_BYTES``.
    records_end:
        One past the last record's last byte == ``len(data) - ANIM_TRAILER_BYTES``
        in every shipped world. This is where task 9 appends.
    trailer:
        The ``ANIM_TRAILER_BYTES`` bytes from ``records_end`` to end of buffer,
        verbatim. ``append_anim_records`` must reproduce these after the
        appended records.
    """

    anchor: int
    count: int
    first_tag: int
    records_start: int
    records_end: int
    trailer: bytes

    @property
    def records_length(self) -> int:
        """Byte span the records occupy."""
        return self.records_end - self.records_start

    @property
    def trailer_is_standard(self) -> bool:
        """Whether the trailer matches the one shared by all five worlds.

        Reported, not enforced: a differing trailer is information for the gate
        (task 6.3), and the append path preserves whatever is actually there.
        """
        return self.trailer == ANIM_TRAILER


def find_anim_region(data) -> AnimRegion | None:
    """Locate the anim region in ``data``. Pure read; ``data`` is untouched.

    Returns ``None`` when the anchor is not locatable, i.e. when the table is
    unreadable (Requirement 1.3). Raises ``AnimParseError`` when an anchor exists
    but the buffer cannot hold the region it implies -- that is a corrupt
    archive, and reporting it as "no table" would hide it.

    Takes a ``bytes`` buffer and opens nothing, so callers keep control of the
    one-``.PRS``-at-a-time rule (Requirement 10.2).

    TWO WAYS TO GET THE ANCHOR, IN A FIXED ORDER OF PREFERENCE:

    1. ``mesh_port.find_anim_table_anchor`` -- the MESH WALK'S END, corroborated
       by the type-id word. This is the only one that is right for a real
       archive, and it is tried FIRST, so every shipped ``.PRS`` takes it. It is
       what makes an SKA-first table (all six arena archives) locate correctly;
       the old ``TFA\\xff``-only anchor put the "region" 160-621 KB INSIDE such a
       table and reported a truncated count.
    2. the ``TFA\\xff`` scan, and ONLY when (1) found nothing. For a real archive
       that cannot happen -- a ``.PRS`` always has a walkable mesh table at 0x20
       with the type-id word after it. What reaches the fallback is an ANIM TABLE
       HELD ON ITS OWN, with no mesh table in front of it: the synthetic buffers
       that exercise this module's walker and gate directly. Those are MEA-first
       by construction, so the scan is exact for them.

    The fallback cannot reintroduce the SKA bug it replaced, because an arena
    archive has a walkable mesh table and so never reaches it.
    """
    anchor = find_anim_table_anchor(data)
    if anchor is None:
        # No mesh table to measure from: an anim table on its own. See above.
        signature = find_first_anim_signature(data)
        if signature is not None and signature >= _TFA_HEADER_BYTES:
            anchor = signature - _TFA_HEADER_BYTES
    if anchor is None:
        return None

    end = len(data)
    if anchor + 12 > end:
        raise AnimParseError(
            f"anim anchor {anchor} leaves only {end - anchor} byte(s) of "
            f"buffer, too few for the 12-byte header window")

    # read_anim_count is the shared reader, but it returns None when the word at
    # anchor + 8 is not 0x0A -- and that word is a per-record tag, so a table
    # whose first record is SKA is perfectly valid. Fall back to the count word
    # itself in that case; the anchor and the offsets are unaffected.
    count = read_anim_count(data)
    first_tag = struct.unpack_from("<I", data, anchor + 8)[0]
    if count is None:
        count = struct.unpack_from("<I", data, anchor + 4)[0]
        if count <= 0 or count > 0xFFFF:
            raise AnimParseError(
                f"anim record count {count} at offset {anchor + 4} is not a "
                f"plausible table size")

    records_start = anchor + ANIM_HEADER_BYTES
    records_end = end - ANIM_TRAILER_BYTES
    if records_end < records_start:
        raise AnimParseError(
            f"anim records start at {records_start} but the "
            f"{ANIM_TRAILER_BYTES}-byte trailer would begin at {records_end}, "
            f"before them")

    return AnimRegion(
        anchor=anchor,
        count=count,
        first_tag=first_tag,
        records_start=records_start,
        records_end=records_end,
        trailer=bytes(data[records_end:end]),
    )


# ---------------------------------------------------------------------------
# Task 5.4 -- the per-TYPE-TAG cursor-advance rules.
#
# Every constant and every check below is transcribed from the engine's own
# loaders, decoded in `.kiro/specs/anim-table-format/elf-notes.md` (tasks 3.1
# and 3.2) and validated byte-exactly against all 412 records of all five
# worlds in task 3.3. Nothing here is a guess and nothing here was tuned.
#
#   tag 0x0A  MEA  PMeshNodeAnimDataTable::Load  0x00174880
#       record  = 4 tag + 4 W0 + (76 if W0 == 0xFF414654 else 0)
#                 + sum(mea_node for i in range(N)) + 4 terminator == 11
#                 where N is the u32 at the end of the TFA header block on the
#                 TFA form, else W0 itself
#       node    = 32 name + 4 (== 4) + track
#       track   = 16 header + sum(channel for j in range(K))
#                 + L1 + L2*32 + 4 terminator == 5
#       channel = 8 header + (c0*16 if MODE == 1 else (c0+c1+c2)*16)
#
#   tag 0x0C  SKA  PSkeletonAnim::Load  0x0016BDC0
#       record  = 4 tag + 8 (A, B) + sum(ska_node for i in range(B))
#                 + 4 terminator == 13
#       node    = 52 header + L0 + L1 + L2 + L3 + C*32   (no terminator)
#
# The rules are RECURSIVE: an outer count bounds a loop whose body is a nested
# loader with its own length fields. So this is a recursive-descent measurer,
# not an arithmetic expression. In particular the IN-MEMORY strides the loaders
# allocate with -- 60 for an ska_node, 36 for a mea_node, 12 for a channel --
# are NOT the on-disk sizes (elf-notes explains why: they are copied header
# bytes plus pointers). A walker that multiplied a count by a stride would be
# wrong on every record.
#
# The terminator words (11 for a MEA record, 5 for a MEA track, 13 for a SKA
# record) and the mea_node `== 4` word are load-bearing correctness checks, not
# decoration: they sit at cursor positions computed from the record's own length
# fields, so a rule off by one byte anywhere fails one of them. They are
# verified, never skipped.
# ---------------------------------------------------------------------------

#: The ``TFA\xff`` magic as the u32 the MEA loader compares against
#: (``lui/ori $v0, 0xFF414654`` at ``0x001748E8``).
MEA_MAGIC_WORD = 0xFF414654

#: Bytes the TFA header block occupies: 4 + 32 + 4 + 32 + 4.
MEA_TFA_HEADER_BYTES = 76

MEA_RECORD_TERMINATOR = 11   # xori 11 at 0x00174A6C's check
MEA_TRACK_TERMINATOR = 5     # xori 5 at 0x001743D4
MEA_NODE_MARKER = 4          # asserted == 4 at 0x00174720
SKA_RECORD_TERMINATOR = 13   # xori 13 at 0x0016BF34

MEA_NAME_BYTES = 32
MEA_TRACK_HEADER_BYTES = 16
MEA_CHANNEL_HEADER_BYTES = 8
MEA_MAX_CHANNELS = 5         # asserted K < 5 at 0x00174160
SKA_NODE_HEADER_BYTES = 52

#: Sanity ceiling on any count or byte length read out of a record. No shipped
#: record comes remotely close (the largest is UNDER 89 at 20,656 bytes with
#: B = 39), so a value above this is corruption, and corruption must raise
#: rather than be trusted into a multi-megabyte bogus advance.
_IMPLAUSIBLE = 1 << 24


class _Reader:
    """Bounds-checked forward cursor over one record's bytes.

    Every out-of-range read raises ``AnimParseError`` naming the record offset,
    the field, the value and the expectation. Nothing is clamped and nothing is
    guessed -- the engine hard-asserts on a malformed record, so there is no
    skip-and-continue to imitate (elf-notes, ``0x00168144``).
    """

    __slots__ = ("data", "record_offset", "pos", "limit")

    def __init__(self, data, record_offset: int, limit: int):
        self.data = data
        self.record_offset = record_offset
        self.pos = record_offset
        self.limit = limit

    def _fail(self, field: str, detail: str) -> None:
        raise AnimParseError(
            f"anim record at {self.record_offset}: {field} at offset "
            f"{self.pos}: {detail}")

    def skip(self, nbytes: int, field: str) -> None:
        if nbytes < 0 or nbytes > _IMPLAUSIBLE:
            self._fail(field, f"length {nbytes} is not a plausible block size "
                              f"(expected 0..{_IMPLAUSIBLE})")
        end = self.pos + nbytes
        if end > self.limit:
            self._fail(field, f"{nbytes}-byte block would end at {end}, past "
                              f"the parse limit {self.limit}")
        self.pos = end

    def _need(self, nbytes: int, field: str) -> None:
        if self.pos + nbytes > self.limit:
            self._fail(field, f"{nbytes}-byte field would end at "
                              f"{self.pos + nbytes}, past the parse limit "
                              f"{self.limit}")

    def u32(self, field: str) -> int:
        self._need(4, field)
        value = struct.unpack_from("<I", self.data, self.pos)[0]
        self.pos += 4
        return value

    def u16(self, field: str) -> int:
        self._need(2, field)
        value = struct.unpack_from("<H", self.data, self.pos)[0]
        self.pos += 2
        return value

    def u8(self, field: str) -> int:
        self._need(1, field)
        value = self.data[self.pos]
        self.pos += 1
        return value

    def name(self, field: str) -> str:
        """A 32-byte NUL-padded name, decoded leniently for reporting only."""
        self._need(MEA_NAME_BYTES, field)
        raw = bytes(self.data[self.pos:self.pos + MEA_NAME_BYTES])
        self.pos += MEA_NAME_BYTES
        return raw.split(b"\x00", 1)[0].decode("latin-1")

    def expect_u32(self, field: str, expected: int) -> int:
        value = self.u32(field)
        if value != expected:
            self.pos -= 4
            self._fail(field, f"read {value} (0x{value:X}), expected "
                              f"{expected}")
        return value


@dataclass(frozen=True)
class AnimRecordInfo:
    """What a measured record declares about itself.

    Produced by ``measure_record``. ``record_length`` is the thin wrapper over
    it, so the two can never disagree about a length.

    Attributes
    ----------
    tag:
        The record's TYPE TAG, one of ``ACCEPTED_TAGS``.
    start / length:
        Byte extent, ``start + length`` being the next record's start.
    node_count:
        ``N`` for MEA, ``B`` for SKA -- the count that bounds the node loop.
    frame_word:
        SKA's ``A`` (the loader stores ``A - 1``; likely a frame count). ``None``
        for MEA.
    is_tfa:
        MEA only: whether the record carries the ``TFA\\xff`` header block. Task
        3.3 found the TFA form in 100% of shipped tag-``0x0A`` records, so
        ``False`` here has never been observed in real data.
    node_names:
        The 32-byte NUL-padded node names, in file order: MEA node names (all
        ``_NoName_`` in GRAVE) and SKA node names (``joint``, ``joint#2``, ...).
        Task 8.4 needs the SKA ones; task 8.2 wants the shape as role evidence.
    """

    tag: int
    start: int
    length: int
    node_count: int
    frame_word: int | None
    is_tfa: bool
    node_names: tuple[str, ...]

    @property
    def end(self) -> int:
        return self.start + self.length


def _mea_channel(reader: _Reader) -> None:
    """``0x001660C0``. 8-byte header, then 16 bytes per counted item."""
    kind = reader.u32("mea_channel.KIND")
    if not 1 <= kind <= 4:
        reader.pos -= 4
        reader._fail("mea_channel.KIND",
                     f"read {kind}, expected 1..4 (asserted at 0x0016615C / "
                     f"0x00166164)")
    mode = reader.u8("mea_channel.MODE")
    if mode not in (1, 2):
        reader.pos -= 1
        reader._fail("mea_channel.MODE",
                     f"read {mode}, expected 1 or 2 (asserted at 0x00166190)")
    c0 = reader.u8("mea_channel.c0")
    c1 = reader.u8("mea_channel.c1")
    c2 = reader.u8("mea_channel.c2")
    # MODE 1 reads one c0*16 block; MODE 2 reads three blocks totalling
    # (c0+c1+c2)*16, each zero count skipped -- same byte total either way.
    total = c0 if mode == 1 else c0 + c1 + c2
    if total:
        reader.skip(total * 16, "mea_channel.payload")


def _mea_track(reader: _Reader) -> None:
    """``0x001740F0``. 16-byte header, K channels, two optional blocks, term."""
    reader.u32("mea_track.word0")
    channels = reader.u32("mea_track.K")
    if channels >= MEA_MAX_CHANNELS:
        reader.pos -= 4
        reader._fail("mea_track.K",
                     f"read {channels}, expected < {MEA_MAX_CHANNELS} "
                     f"(asserted at 0x00174160)")
    reader.u32("mea_track.word2")
    l1 = reader.u16("mea_track.L1")
    l2 = reader.u16("mea_track.L2")
    for _ in range(channels):
        _mea_channel(reader)
    if l1:
        reader.skip(l1, "mea_track.L1_block")
    if l2:
        reader.skip(l2 * 32, "mea_track.L2_block")
    reader.expect_u32("mea_track.terminator", MEA_TRACK_TERMINATOR)


def _mea_node(reader: _Reader) -> str:
    """``0x00174690``. 32-byte name, a word that must be 4, then one track."""
    name = reader.name("mea_node.name")
    reader.expect_u32("mea_node.marker", MEA_NODE_MARKER)
    _mea_track(reader)
    return name


def _measure_mea(reader: _Reader, offset: int) -> AnimRecordInfo:
    w0 = reader.u32("mea.W0")
    is_tfa = w0 == MEA_MAGIC_WORD
    if is_tfa:
        # 4 + 32 + 4 + 32 + 4: the [01 01 00 00] word, the NAME, a u32, a second
        # 32-byte field, then N -- which overwrites W0 in the object.
        reader.u32("mea.tfa.word")
        reader.name("mea.tfa.name")
        reader.u32("mea.tfa.frames")
        reader.skip(32, "mea.tfa.second_field")
        node_count = reader.u32("mea.tfa.N")
    else:
        # The non-TFA form: the mismatch branch at 0x001749D0 skips the header
        # block entirely and W0 IS the node count. Implemented because the
        # loader accepts it -- but task 3.3 found ZERO non-TFA tag-0x0A records
        # in any of the five shipped worlds, so this branch is UNCOVERED BY REAL
        # BYTES. It comes from the ELF alone.
        node_count = w0
    if node_count > _IMPLAUSIBLE:
        reader._fail("mea.N", f"node count {node_count} is not plausible")

    names = [_mea_node(reader) for _ in range(node_count)]
    reader.expect_u32("mea.terminator", MEA_RECORD_TERMINATOR)
    return AnimRecordInfo(
        tag=TAG_MEA,
        start=offset,
        length=reader.pos - offset,
        node_count=node_count,
        frame_word=None,
        is_tfa=is_tfa,
        node_names=tuple(names),
    )


def _ska_node(reader: _Reader) -> str:
    """``0x0016B8B0``. 52-byte header naming a joint, then five sized blocks.

    Bytes ``0x00``-``0x1F`` of the header are a 32-byte NUL-padded name --
    ``joint``, ``joint#2``, ... as read in task 3.3. That is why SKA channels
    address bones BY NAME, which is what task 8.4 turns into a rig verdict.
    There is no terminator word; the node ends with the ``C*32`` block.
    """
    name = reader.name("ska_node.name")     # header 0x00..0x1F
    l0 = reader.u32("ska_node.L0")          # header 0x20
    l1 = reader.u32("ska_node.L1")          # header 0x24
    l2 = reader.u32("ska_node.L2")          # header 0x28
    l3 = reader.u32("ska_node.L3")          # header 0x2C
    count = reader.u32("ska_node.C")        # header 0x30
    # 32 name + 5 * 4 = 52 bytes, the single read at 0x0016B8F8.
    for value, field in ((l0, "L0"), (l1, "L1"), (l2, "L2"), (l3, "L3")):
        if value:
            reader.skip(value, f"ska_node.{field}_block")
    if count:
        reader.skip(count * 32, "ska_node.C_block")
    return name


def _measure_ska(reader: _Reader, offset: int) -> AnimRecordInfo:
    a = reader.u32("ska.A")
    b = reader.u32("ska.B")
    # B == 0 (and A == 0) is a LOAD FAILURE in the engine, not an early success:
    # the branch at 0x0016BE3C carries `move $v0, $zero` in its delay slot and
    # LoadAnimResTable prints the SKA error and returns 0. There is no 12-byte
    # SKA record.
    if a < 1:
        reader.pos -= 8
        reader._fail("ska.A", f"read {a}, expected >= 1 (A == 0 makes A - 1 "
                              f"negative, a load failure at 0x0016BE48)")
    if b < 1:
        reader.pos -= 4
        reader._fail("ska.B", f"read {b}, expected >= 1 (B == 0 is a load "
                              f"failure at 0x0016BE3C)")
    if b > _IMPLAUSIBLE:
        reader.pos -= 4
        reader._fail("ska.B", f"node count {b} is not plausible")

    names = [_ska_node(reader) for _ in range(b)]
    reader.expect_u32("ska.terminator", SKA_RECORD_TERMINATOR)
    return AnimRecordInfo(
        tag=TAG_SKA,
        start=offset,
        length=reader.pos - offset,
        node_count=b,
        frame_word=a,
        is_tfa=False,
        node_names=tuple(names),
    )


def measure_record(data, offset: int, *, limit: int | None = None) -> AnimRecordInfo:
    """Measure the record at ``offset`` from THAT record's bytes alone.

    ``limit`` defaults to ``len(data)`` and is normally the region's
    ``records_end``: a record that would run past it is corruption, not a big
    record, and raises.

    Raises ``AnimParseError`` for an unknown TYPE TAG, a field that would read
    past ``limit``, an out-of-range ``K`` / ``KIND`` / ``MODE``, ``A``/``B`` < 1,
    or a failed terminator. Never clamps and never falls back.
    """
    end = len(data) if limit is None else limit
    if end > len(data):
        end = len(data)
    if offset < 0 or offset + 4 > end:
        raise AnimParseError(
            f"anim record offset {offset} leaves no room for a TYPE TAG before "
            f"the parse limit {end}")

    reader = _Reader(data, offset, end)
    tag = reader.u32("record.tag")
    if tag == TAG_MEA:
        return _measure_mea(reader, offset)
    if tag == TAG_SKA:
        return _measure_ska(reader, offset)
    raise AnimParseError(
        f"anim record at {offset}: TYPE TAG {tag} (0x{tag:X}) is not accepted; "
        f"the engine's loader compares against exactly "
        f"{{0x{TAG_MEA:02X}, 0x{TAG_SKA:02X}}} and hard-asserts on anything "
        f"else (LoadAnimResTable 0x00168144)")


def record_length(data, offset: int, *, limit: int | None = None) -> tuple[int, int]:
    """``(tag, length)`` for the record at ``offset``. Design's walker hook.

    The single place the ELF-derived advance rules live, one branch per accepted
    tag. Depends only on the bytes of the record at ``offset``: no scanning, no
    marker search, no look-ahead to the next record.
    """
    info = measure_record(data, offset, limit=limit)
    return info.tag, info.length


# ---------------------------------------------------------------------------
# Task 5.5 -- the walker.
#
# `walk_anim_table` advances ONLY by `record_length`. There is no marker scan
# and no resync, deliberately:
#
#   * `prs_mesh_parser`'s resync recovery is exactly why CASTLE / SWAMP / UNDER
#     cannot be trusted as mesh-port targets -- a recovered walk reports a
#     boundary it did not derive.
#   * a signature scan is what failed for anim records in the first place: 57%
#     of them (the tag-0x0C SKA form) carry no magic at all.
#
# So a walk that would need recovery is a walk whose result must be REJECTED,
# not repaired. On the first record the rules cannot measure, the walker records
# an event naming the record index and the byte offset, and STOPS.
#
# The walker never raises for malformed data (Requirement 3.5): callers only
# ever see a walk that is not `clean`.
# ---------------------------------------------------------------------------

__all__ += [
    "AnimTableWalk",
    "walk_anim_table",
    "find_record_bounds",
    "extract_record_from_buffer",
]


@dataclass
class AnimTableWalk:
    """One walk of a world's anim table.

    Attributes
    ----------
    count:
        The DECLARED record count (``region.count``), i.e. what the walk was
        trying to reproduce -- not what it achieved. ``len(bounds)`` is what it
        achieved.
    bounds:
        ``(index, start, end, tag)`` per record, in table order. Mirrors
        ``MeshTableWalk.bounds``.
    events:
        ``(index, kind, detail)``. ``kind`` is ``"no_region"``,
        ``"region_error"``, ``"measure_failed"`` or ``"count_overrun"``. A
        non-empty list means the walk is not trustworthy; the gate (task 6.3)
        turns each into a reason string.
    end:
        Cursor after the last record the walk bounded. Equals
        ``region.records_start`` for a walk that failed on record 0.
    region:
        The located ``AnimRegion``, or ``None`` when the table is unreadable.
    """

    count: int
    bounds: list = None
    events: list = None
    end: int = 0
    region: AnimRegion | None = None

    def __post_init__(self):
        if self.bounds is None:
            self.bounds = []
        if self.events is None:
            self.events = []

    @property
    def trailing(self) -> int:
        """Bytes between the walk's end and ``region.records_end``.

        REPORTED, not required to be zero (design: "``walk.trailing`` is
        reported, not required to be zero"). In all five shipped worlds it IS
        zero, which is the strongest single piece of evidence that the
        ELF-derived rules are right -- but the walker states the fact rather
        than assuming it. ``0`` when there is no region to measure against.
        """
        if self.region is None:
            return 0
        return self.region.records_end - self.end

    @property
    def clean(self) -> bool:
        """Whether the walk consumed exactly ``count`` records with no events.

        Note this is STRICTER than ``MeshTableWalk.clean`` (which only checks
        events): a walk that stopped early has an event AND a short bound list,
        and both are checked here so a future event-free early stop cannot read
        as clean. ``trailing`` is deliberately NOT part of this -- see above.
        """
        return not self.events and len(self.bounds) == self.count

    def events_for(self, index: int) -> list:
        return [event for event in self.events if event[0] == index]

    @property
    def tags(self) -> list:
        return [bound[3] for bound in self.bounds]


def walk_anim_table(data, stop_after=None) -> AnimTableWalk:
    """Walk ``data``'s anim table by ``record_length`` alone. Never raises.

    ``stop_after``: stop once that index has been bounded, for callers that only
    need a prefix (``find_record_bounds``). A stopped-early walk is not
    ``clean``, by construction -- ``clean`` is a whole-table verdict.

    Reads only; ``data`` is untouched (Requirement 10.2).
    """
    try:
        region = find_anim_region(data)
    except AnimParseError as exc:
        walk = AnimTableWalk(count=0)
        walk.events.append((-1, "region_error", str(exc)))
        return walk

    if region is None:
        walk = AnimTableWalk(count=0)
        walk.events.append(
            (-1, "no_region",
             "no TFA\\xff anchor in this buffer, so the anim table is "
             "unreadable (Requirement 1.3)"))
        return walk

    walk = AnimTableWalk(count=region.count, region=region,
                         end=region.records_start)
    cursor = region.records_start
    for index in range(region.count):
        if cursor >= region.records_end:
            walk.events.append(
                (index, "count_overrun",
                 f"the table declares {region.count} record(s) but the region "
                 f"ends at {region.records_end}, reached after {index}"))
            break
        try:
            tag, length = record_length(data, cursor, limit=region.records_end)
        except AnimParseError as exc:
            # The one failure mode, and it is terminal for this walk. No scan,
            # no resync, no fabricated length.
            walk.events.append(
                (index, "measure_failed",
                 f"record {index} at offset {cursor} could not be measured "
                 f"from its own bytes: {exc}"))
            break
        walk.bounds.append((index, cursor, cursor + length, tag))
        cursor += length
        walk.end = cursor
        if stop_after is not None and index >= stop_after:
            break
    return walk


def find_record_bounds(data, index: int) -> tuple[int, int, int]:
    """``(start, end, tag)`` for record ``index``, by walking from the start.

    Positional, like ``mesh_port.find_entry_bounds``, and it HAS to be: task 3.3
    proved the two tags interleave (GRAVE's first SKA is index 10, MEA records
    reappear at index 52), so a record cannot be located by signature. Index ->
    offset is a chain walk from ``records_start`` or it is nothing.

    Unlike ``walk_anim_table`` this DOES raise -- it is a direct lookup, and a
    caller asking for one record's extent needs a value or an error, not a
    verdict object. Raises ``ValueError`` for an out-of-range index and
    ``AnimParseError`` when the walk could not reach ``index``.
    """
    if index < 0:
        raise ValueError(f"record index {index} is negative")
    walk = walk_anim_table(data, stop_after=index)
    if walk.region is None:
        raise AnimParseError(walk.events[0][2])
    if index >= walk.count:
        raise ValueError(
            f"record {index} out of range (table declares {walk.count} "
            f"record(s))")
    if walk.events:
        _, kind, detail = walk.events[0]
        raise AnimParseError(
            f"cannot reach record {index}: walk stopped ({kind}) -- {detail}")
    return walk.bounds[index][1], walk.bounds[index][2], walk.bounds[index][3]


def extract_record_from_buffer(data, index: int) -> tuple[bytes, int]:
    """``(record_bytes, tag)`` for record ``index`` of an in-memory ``.PRS``.

    Buffer-taking twin of ``mesh_port.extract_mesh_entry_from_buffer``, and the
    source side of task 9's append path: the bytes returned here are exactly
    what gets appended to a target table, tag word included.

    The slice is a real ``bytes`` copy, so the caller can drop the multi-megabyte
    ``.PRS`` buffer immediately afterwards -- which is what keeps the
    one-archive-at-a-time rule satisfiable during a port (Requirement 10.3).
    """
    start, end, tag = find_record_bounds(data, index)
    return bytes(data[start:end]), tag

# ---------------------------------------------------------------------------
# Task 6.1 -- the round trip.
#
# The trust gate (task 6.3) will not vouch for a record unless the record's
# bytes, taken OUT of the archive on their own, re-parse to the same length and
# re-serialise byte-identically. That is what makes appending a record to
# another world's table safe: a record whose extraction is not provably faithful
# must never be committed, because a partially-correct commit desynchronises the
# engine's sequential read of the WHOLE table and freezes the level load.
#
# `measure_record` is a pure LENGTH MEASURER, not a full deserialiser -- there
# is no object graph to write back out. So "re-serialise" here means: prove the
# slice is SELF-DESCRIBING and COMPLETE. Concretely,
#
#   (a) measuring the slice in isolation, with the parse limit set to the
#       slice's own length, yields exactly `len(slice)` -- nothing is read from
#       neighbouring records and nothing is left over;
#   (b) the bytes the measurement covers, re-emitted, equal the input slice
#       byte for byte;
#   (c) (buffer variant only) measuring the same record INSIDE its archive
#       yields the same tag and the same length, and the slice equals
#       `data[start:end]`.
#
# (a) and (b) are what a length-measuring parser can prove; (c) is the check
# that the extraction itself was faithful. Nothing here raises: a failed round
# trip is a REPORTED result, matching `walk_anim_table`'s contract, so the gate
# only ever sees "not trustworthy".
# ---------------------------------------------------------------------------

__all__ += [
    "RoundTrip",
    "round_trip_record",
    "round_trip_record_in_buffer",
]


def _first_difference(left: bytes, right: bytes) -> int | None:
    """Offset of the first differing byte, or of the shorter one's end."""
    if left == right:
        return None
    for offset, (a, b) in enumerate(zip(left, right)):
        if a != b:
            return offset
    return min(len(left), len(right))


@dataclass(frozen=True)
class RoundTrip:
    """Full detail of one record's round trip.

    ``round_trip_record`` returns the design's ``(consumed, events,
    byte_identical)`` triple; this is what those three come from, kept for the
    gate's ``details`` lines and for tests that want the first-difference
    offset without re-deriving it.
    """

    index: int
    consumed: int
    length: int
    tag: int | None
    events: tuple
    byte_identical: bool
    first_difference: int | None = None
    info: AnimRecordInfo | None = None

    @property
    def ok(self) -> bool:
        """Requirement 4.2: trustworthy only when nothing at all went wrong."""
        return (self.byte_identical and not self.events
                and self.consumed == self.length)

    def as_triple(self) -> tuple:
        return self.consumed, list(self.events), self.byte_identical


def round_trip_record(record_bytes, *, index: int = 0, original=None):
    """Re-parse ``record_bytes`` in ISOLATION and re-serialise it. Never raises.

    Returns ``(consumed, events, byte_identical)`` (Requirements 4.1, 4.3).
    ``consumed == len(record_bytes)`` with an empty event list and
    ``byte_identical`` True is the only trustworthy outcome (Requirement 4.2).

    ``events`` mirror the walker's ``(index, kind, detail)`` shape so the gate
    can turn walk events and round-trip events into reason strings uniformly.
    Kinds: ``"measure_failed"``, ``"length_mismatch"``, ``"byte_mismatch"``.

    ``index`` only labels the events. ``original`` is the byte string the
    re-serialised result is compared against when it differs from the input
    slice -- the buffer variant passes ``data[start:end]`` through it.

    On a mismatch the event names the FIRST differing byte offset, both byte
    values and the record index (Requirement 4.4).
    """
    return round_trip_record_detail(record_bytes, index=index,
                                    original=original).as_triple()


def round_trip_record_detail(record_bytes, *, index: int = 0,
                             original=None) -> RoundTrip:
    """``round_trip_record`` with the full ``RoundTrip`` instead of the triple."""
    slice_bytes = bytes(record_bytes)
    length = len(slice_bytes)
    reference = slice_bytes if original is None else bytes(original)
    events: list = []

    try:
        # The parse limit is the SLICE's own length: a record that would need a
        # neighbour's bytes to finish is not self-describing, and saying so is
        # the whole point of measuring in isolation.
        info = measure_record(slice_bytes, 0, limit=length)
    except AnimParseError as exc:
        events.append(
            (index, "measure_failed",
             f"record {index} does not re-parse in isolation from its own "
             f"{length} byte(s): {exc}"))
        return RoundTrip(index=index, consumed=0, length=length, tag=None,
                         events=tuple(events), byte_identical=False,
                         first_difference=None, info=None)

    consumed = info.length
    if consumed != length:
        events.append(
            (index, "length_mismatch",
             f"record {index} re-parsed in isolation consumed {consumed} "
             f"byte(s) of a {length}-byte slice "
             f"({consumed - length:+d}), so the extracted length is wrong"))

    # Re-serialisation: the measurement covers `consumed` bytes of the slice and
    # re-emitting the record means emitting exactly those bytes back.
    reserialised = slice_bytes[:consumed]
    difference = _first_difference(reserialised, reference)
    if difference is not None:
        left = (f"0x{reserialised[difference]:02X}"
                if difference < len(reserialised) else "end of re-serialised")
        right = (f"0x{reference[difference]:02X}"
                 if difference < len(reference) else "end of original")
        events.append(
            (index, "byte_mismatch",
             f"record {index} is not byte-identical after re-serialisation: "
             f"first difference at byte offset {difference} of the record "
             f"(re-serialised {left} vs original {right}); "
             f"{len(reserialised)} byte(s) re-serialised against "
             f"{len(reference)} original"))

    byte_identical = difference is None and consumed == length
    return RoundTrip(index=index, consumed=consumed, length=length,
                     tag=info.tag, events=tuple(events),
                     byte_identical=byte_identical,
                     first_difference=difference, info=info)


def round_trip_record_in_buffer(data, index: int) -> RoundTrip:
    """Round-trip record ``index`` of an in-memory ``.PRS``. Never raises.

    Extracts the record, re-parses the SLICE in isolation, and corroborates the
    extraction against the archive: same tag, same length measured in place, and
    the slice equal to ``data[start:end]``. This is the form the gate (task 6.3)
    calls per record.
    """
    try:
        start, end, tag = find_record_bounds(data, index)
    except (AnimParseError, ValueError) as exc:
        return RoundTrip(
            index=index, consumed=0, length=0, tag=None,
            events=((index, "measure_failed",
                     f"record {index} could not be bounded in its archive: "
                     f"{exc}"),),
            byte_identical=False)

    original = bytes(data[start:end])
    result = round_trip_record_detail(original, index=index)

    # In-archive corroboration: the same record measured in place must agree
    # with the isolated measurement, or the extraction is not faithful.
    events = list(result.events)
    if result.tag is not None and result.tag != tag:
        events.append(
            (index, "byte_mismatch",
             f"record {index} measured tag 0x{result.tag:X} in isolation but "
             f"0x{tag:X} inside the archive"))
    if result.consumed and result.consumed != end - start:
        events.append(
            (index, "length_mismatch",
             f"record {index} measured {result.consumed} byte(s) in isolation "
             f"but {end - start} byte(s) inside the archive"))
    if events == list(result.events):
        return result
    return RoundTrip(index=index, consumed=result.consumed,
                     length=result.length, tag=result.tag,
                     events=tuple(events), byte_identical=False,
                     first_difference=result.first_difference,
                     info=result.info)


__all__ += ["round_trip_record_detail"]

# ---------------------------------------------------------------------------
# Task 6.3 -- the trust gate.
#
# `assess_anim_table` is the anim-side twin of `mesh_port.assess_target_table`,
# down to the verdict shape (`ok` / `reasons` / `details` / `reason`), because
# `cross_world.refuse` already consumes that pattern.
#
# THE CONTRAST WITH THE MESH GATE IS THE POINT OF THIS FEATURE. Today
# `assess_target_table` passes for GRAVE ALONE: ICE resyncs inside entry 16, and
# CASTLE / SWAMP / UNDER walk 277,952 / 170,328 / 737,676 bytes short of their
# anim anchors. `assess_anim_table` passes for ALL FIVE WORLDS -- the walk
# reproduces every declared count exactly and lands on the archive trailer with
# zero trailing bytes in each. That is why anim porting is viable into worlds
# that mesh porting cannot target at all.
#
# Five conditions, each producing its OWN reason string so a caller can see
# every reason a table was rejected rather than just the first:
#
#   1. the walked record count equals the declared count;
#   2. the bounds tile the region -- first bound at `records_start`, each
#      subsequent bound starting exactly where its predecessor ended;
#   3. no walk event was raised (nothing was guessed);
#   4. every record round-trips byte-identically (Requirement 4.2);
#   5. SIGNATURE CORROBORATION -- see below.
#
# Condition 5 exists because conditions 1-4 all run through `record_length`. A
# gate built only from them asks the parser to confirm itself, which proves
# nothing about whether the parser is right. So the gate also SCANS the region
# for every `TFA\xff` occurrence -- a completely different method from a chain
# walk -- and requires that
#
#   * each occurrence lands exactly 4 bytes into a walked tag-0x0A record, i.e.
#     at `record_start + 4` where the MEA loader reads its magic, and
#   * the number of occurrences equals the number of tag-0x0A records.
#
# Agreement between a scan and a chain walk is independent evidence; the walk
# agreeing with itself is not. Task 3.3 measured this exactly -- GRAVE 23/23,
# CASTLE 38/38, SWAMP 23/23, ICE 34/34, UNDER 28/28, with no stray signature
# inside any payload -- so the check is calibrated against real bytes, and it is
# what catches a rule set that lands on the right COUNT by accident while
# sliding record boundaries across each other.
#
# `walk.trailing` is REPORTED in `details`, never required to be zero. It is
# zero in all five shipped worlds; the gate states the fact instead of assuming
# it, and task 9's append path preserves whatever is actually there.
#
# Nothing here raises for malformed data. A failed assessment is a verdict with
# `ok=False`, mirroring the walker's contract, so `cross_world` can turn it
# straight into a refusal.
# ---------------------------------------------------------------------------

__all__ += [
    "AnimTrustVerdict",
    "signature_offsets",
    "assess_anim_table",
    "assess_source_record",
]


def _as_searchable(data):
    """A buffer `.find` can be called on, without copying `bytes`/`bytearray`."""
    if isinstance(data, (bytes, bytearray)):
        return data
    return bytes(data)


def signature_offsets(data, start: int, end: int) -> list:
    """Every ``TFA\\xff`` offset in ``data[start:end]``, ascending.

    A plain scan, deliberately: it shares no code and no assumption with
    ``record_length``, which is the only reason its agreement with the walk
    means anything.
    """
    buf = _as_searchable(data)
    found = []
    pos = max(0, start)
    limit = min(end, len(buf))
    while True:
        hit = buf.find(ANIM_RECORD_SIGNATURE, pos, limit)
        if hit < 0:
            return found
        found.append(hit)
        pos = hit + 1


@dataclass
class AnimTrustVerdict:
    """Result of an anim trust gate. ``ok`` False means REFUSE.

    Deliberately the same shape as ``mesh_port.TrustVerdict``: ``reason`` joins
    ``reasons`` with ``"; "`` because that is what ``cross_world.refuse``
    already expects to receive.

    Attributes
    ----------
    ok:
        True only when every condition held.
    reasons:
        ONE string per VIOLATED condition, so a caller sees every reason the
        table was rejected (Requirement 5.2).
    details:
        Informational lines for logging, carrying the numbers that were actually
        checked -- declared and walked counts, MEA / SKA split, signature count,
        region offsets and ``trailing`` -- so a log reader can see the evidence
        rather than just the verdict.
    walk:
        The ``AnimTableWalk`` the verdict was formed from, ``None`` never
        (``walk_anim_table`` always returns one).
    record_bytes / tag:
        ``assess_source_record`` only: the extracted record, so the caller does
        not re-extract it. Mirrors ``mesh_port.TrustVerdict.entry_bytes``.
    """

    ok: bool
    reasons: list = field(default_factory=list)
    details: list = field(default_factory=list)
    walk: AnimTableWalk | None = None
    record_bytes: bytes | None = None
    tag: int | None = None

    @property
    def reason(self) -> str:
        return "; ".join(self.reasons)


def _describe_events(events) -> str:
    return "; ".join(f"record {index}: {kind}: {detail}"
                     for index, kind, detail in events)


def assess_anim_table(data, *, label="target") -> AnimTrustVerdict:
    """Gate a world's anim table. Never raises; a failure is a verdict.

    Passes only where the walked count equals the declared count, the bounds
    tile the region with no gap and no overlap, no walk event was raised, every
    record round-trips byte-identically, and the independent ``TFA\\xff`` scan
    corroborates the walk's tag-``0x0A`` records (Requirement 5.1). Each
    violated condition contributes its own reason (Requirement 5.2).

    Unlike ``mesh_port.assess_target_table``, which passes for GRAVE alone, this
    gate passes for ALL FIVE shipped worlds.
    """
    walk = walk_anim_table(data)
    region = walk.region
    reasons: list = []
    details: list = []

    if region is None:
        for index, kind, detail in walk.events:
            reasons.append(
                f"{label} anim table is unreadable ({kind}): {detail}")
        if not reasons:
            reasons.append(f"{label} anim table is unreadable: no region and "
                           f"no event to explain it")
        details.append(f"{label}: no anim region located, {len(walk.bounds)} "
                       f"record(s) walked")
        return AnimTrustVerdict(ok=False, reasons=reasons, details=details,
                               walk=walk)

    bounds = walk.bounds
    tags = [bound[3] for bound in bounds]
    mea = tags.count(TAG_MEA)
    ska = tags.count(TAG_SKA)
    signatures = signature_offsets(data, region.records_start,
                                  region.records_end)

    # -- Condition 1: the declared count is reproduced exactly.
    if len(bounds) != region.count:
        reasons.append(
            f"{label} anim-table walk bounded {len(bounds)} record(s) but the "
            f"table declares {region.count} ({len(bounds) - region.count:+d}), "
            f"so the walk does not reproduce the declared count")

    # -- Condition 2: the bounds tile the region, no gap and no overlap.
    tiling: list = []
    if bounds:
        if bounds[0][1] != region.records_start:
            tiling.append(
                f"record 0 starts at {bounds[0][1]}, not at the region start "
                f"{region.records_start}")
        for previous, current in zip(bounds, bounds[1:]):
            if current[1] != previous[2]:
                tiling.append(
                    f"record {current[0]} starts at {current[1]} but record "
                    f"{previous[0]} ends at {previous[2]} "
                    f"({current[1] - previous[2]:+d})")
    elif region.count:
        tiling.append(
            f"no record was bounded at all, so the region "
            f"[{region.records_start},{region.records_end}) is untiled")
    if tiling:
        shown = tiling[:5]
        more = "" if len(tiling) == len(shown) else \
            f" (+{len(tiling) - len(shown)} more)"
        reasons.append(
            f"{label} anim-table bounds do not tile the region: "
            f"{'; '.join(shown)}{more}")

    # -- Condition 3: nothing was guessed.
    if walk.events:
        reasons.append(
            f"{label} anim-table walk raised {len(walk.events)} event(s), so "
            f"part of the table was never derived: {_describe_events(walk.events)}")

    # -- Condition 4: every record round-trips byte-identically.
    # Driven off `walk.bounds` rather than `round_trip_record_in_buffer`, which
    # re-walks the table from the start per record -- quadratic over a 4.2 MB
    # archive. The corroboration this gate needs is the SCAN, not a second walk.
    failed: list = []
    for index, start, end, tag in bounds:
        result = round_trip_record_detail(bytes(data[start:end]), index=index)
        if not result.ok or result.tag != tag:
            failed.append((index, result, tag))
    if failed:
        shown = []
        for index, result, tag in failed[:5]:
            detail = (result.events[0][2] if result.events
                      else f"round trip reported no event but consumed "
                           f"{result.consumed} of {result.length} byte(s), tag "
                           f"0x{(result.tag or 0):X} vs 0x{tag:X}")
            shown.append(f"record {index}: {detail}")
        more = "" if len(failed) == len(shown) else \
            f" (+{len(failed) - len(shown)} more)"
        reasons.append(
            f"{label} anim table has {len(failed)} record(s) that do not "
            f"round-trip byte-identically: {'; '.join(shown)}{more}")

    # -- Condition 5: the independent signature scan corroborates the walk.
    mea_heads = {start for _i, start, _e, tag in bounds if tag == TAG_MEA}
    stray = [offset for offset in signatures if offset - 4 not in mea_heads]
    corroboration: list = []
    if stray:
        corroboration.append(
            f"{len(stray)} TFA\\xff signature(s) do not sit 4 bytes into a "
            f"walked tag-0x{TAG_MEA:02X} record (first at offset {stray[0]})")
    if len(signatures) != mea:
        corroboration.append(
            f"the region holds {len(signatures)} TFA\\xff signature(s) but the "
            f"walk tagged {mea} record(s) 0x{TAG_MEA:02X} "
            f"({mea - len(signatures):+d})")
    if corroboration:
        reasons.append(
            f"{label} anim-table walk is not corroborated by an independent "
            f"TFA\\xff scan: {'; '.join(corroboration)}")

    details.append(
        f"{label}: declared {region.count}, walked {len(bounds)}, "
        f"{mea} MEA + {ska} SKA, {len(signatures)} TFA\\xff signature(s), "
        f"anchor {region.anchor}, records [{region.records_start},"
        f"{region.records_end}), walk end {walk.end}, trailing "
        f"{walk.trailing}, {len(walk.events)} event(s)")
    details.append(
        f"{label}: trailing {walk.trailing} byte(s) REPORTED, not required to "
        f"be zero; {ANIM_TRAILER_BYTES}-byte trailer "
        f"{'standard' if region.trailer_is_standard else 'NON-STANDARD: ' + region.trailer.hex()}")

    return AnimTrustVerdict(ok=not reasons, reasons=reasons, details=details,
                            walk=walk)


def assess_source_record(data, index: int, *, label="source") -> AnimTrustVerdict:
    """Gate ONE source record: bounded, and round-tripping. Never raises.

    The per-record source-side twin of ``mesh_port.assess_source_entry``. On
    success ``record_bytes`` carries the extracted slice and ``tag`` its TYPE
    TAG, so the caller never extracts the record twice -- which is what lets the
    multi-megabyte source buffer be dropped before the target buffer is touched
    (Requirement 10.3).
    """
    reasons: list = []
    details: list = []
    walk = walk_anim_table(data, stop_after=index)

    if walk.region is None:
        for kind_index, kind, detail in walk.events:
            reasons.append(
                f"{label} anim table is unreadable ({kind}): {detail}")
        if not reasons:
            reasons.append(f"{label} anim table is unreadable")
        return AnimTrustVerdict(ok=False, reasons=reasons, walk=walk)

    if index < 0 or index >= walk.count:
        return AnimTrustVerdict(
            ok=False, walk=walk,
            reasons=[f"{label} anim[{index}] is out of range (table declares "
                     f"{walk.count} record(s))"])

    if walk.events:
        reasons.append(
            f"{label} anim table walk stopped before anim[{index}] was "
            f"trustworthy, so nothing may be taken from it: "
            f"{_describe_events(walk.events)}")

    wanted = [bound for bound in walk.bounds if bound[0] == index]
    if not wanted:
        reasons.append(
            f"{label} anim table walk never reached anim[{index}] "
            f"({len(walk.bounds)} record(s) walked before it gave up)")
        return AnimTrustVerdict(ok=False, reasons=reasons, walk=walk)

    _i, start, end, tag = wanted[0]
    result = round_trip_record_in_buffer(data, index)
    if not result.ok:
        detail = (_describe_events(result.events) if result.events
                  else f"consumed {result.consumed} of {result.length} byte(s)")
        reasons.append(
            f"{label} anim[{index}] does not round-trip in isolation: {detail}")

    record_bytes = bytes(data[start:end])
    details.append(
        f"{label} anim[{index}] tag=0x{tag:02X} [{start},{end}) "
        f"{len(record_bytes)} B, round-trip consumed {result.consumed}, "
        f"byte_identical {result.byte_identical}, "
        f"node_count {result.info.node_count if result.info else '?'}")

    ok = not reasons
    return AnimTrustVerdict(
        ok=ok, reasons=reasons, details=details, walk=walk,
        record_bytes=record_bytes if ok else None,
        tag=tag if ok else None)


# ---------------------------------------------------------------------------
# Clip_Role (task 8.2, Requirements 6.3 - 6.6)
# ---------------------------------------------------------------------------
#
# Task 8.1's verdict, recorded in `.kiro/specs/anim-table-format/role-notes.md`,
# is that NONE of the three candidate Role_Sources carries the Clip_Role:
#
#   * the BEF CATEGORY-2 ref ORDER is a stable authored per-enemy SLOT LIST, but
#     its slot -> role meaning is nowhere named and is not shared between
#     enemies, so it cannot pair a role across two different enemies;
#   * `MAXDEMOR.ELF` has exactly one caller of ``LoadAnimResTable`` (the generic
#     resource loader), no per-enemy anim-slot table in ``.rodata`` and no
#     animation-role string anywhere;
#   * the record bytes name BONES and MESH NODES, never clips.
#
# Consequence, stated plainly so nothing downstream mistakes this code for a
# working role resolver: ``resolve_roles`` returns ``ClipRole.UNKNOWN`` for
# EVERY ref of EVERY real enemy, and ``map_by_role`` therefore degrades to its
# positional fallback in every real case. The role machinery is implemented in
# full anyway so that a future role source (the lead in role-notes.md is the
# compiled script bytecode in the level `.PSX` files, unexamined and unproven)
# drops in without reworking the call path. No role is ever guessed here.


class ClipRole(enum.Enum):
    """A clip's gameplay role, or ``UNKNOWN`` when no evidence establishes one.

    The named roles are the ones the design lists. As of task 8.1 NO shipped
    record resolves to any of them -- they exist so that a future Role_Source
    has somewhere to land.
    """

    IDLE = "idle"
    WALK = "walk"
    ATTACK = "attack"
    DEATH = "death"
    HIT = "hit"
    UNKNOWN = "unknown"

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"ClipRole.{self.name}"


#: Pairing rule for ``map_by_role``'s positional fallback.
#:
#: ``"sorted_index"`` -- today's behaviour and the default: the enemy's DISTINCT
#: source indices, SORTED, are laid onto the donor's index list by position,
#: with the surplus clamped onto the donor's last index. Byte-for-byte what
#: ``cross_world._substitute_anim_refs`` does.
#:
#: ``"ref_order"`` -- slot *k* of the source's CATEGORY-2 ref list (IN REF
#: ORDER) pairs with donor slot *k*. Task 8.1 measured that the ref order is a
#: stable authored slot list while the sorted-index order is an artefact of
#: table packing, so this pairing is better founded. It is NOT wired into the
#: live substitution path: switching the live mapping is a behaviour change that
#: belongs with the routing decision in task 10.1 and must be a deliberate,
#: separately-testable step. Slots past the end of the donor list get NO entry
#: (the ref keeps its own index) rather than being clamped.
POSITIONAL_SORTED_INDEX = "sorted_index"
POSITIONAL_REF_ORDER = "ref_order"


@dataclass(frozen=True)
class RoleMapping:
    """Result of :func:`map_by_role`.

    ``by_slot`` maps a source ref POSITION (its index in the ref list, in ref
    order) to the donor anim index it should be rewritten to. A ref position
    ABSENT from ``by_slot`` must be left UNCHANGED (Requirement 6.4).

    ``by_source_index`` is the same mapping keyed by the source ANIM INDEX, for
    callers that rewrite per distinct index. Two slots may legitimately share
    one source index (Axe_Skeleton slots 6 and 7 are both index 21); if they map
    to DIFFERENT donor indices the source index is left out of
    ``by_source_index`` and listed in ``ambiguous_source_indices`` instead, so
    an index-keyed caller can never silently pick one of two answers.

    ``unmatched`` lists ``(slot, role)`` for every ref whose role found no
    same-role counterpart and so fell back positionally (Requirement 6.6).
    """

    by_slot: dict[int, int]
    by_source_index: dict[int, int]
    unmatched: list[tuple[int, ClipRole]]
    ambiguous_source_indices: list[int] = field(default_factory=list)
    positional_order: str = POSITIONAL_SORTED_INDEX


def resolve_roles(refs, *, evidence=None) -> list[ClipRole]:
    """One :class:`ClipRole` per ref, in REF ORDER (Requirements 6.3, 6.4).

    ``refs`` is any sequence -- typically the enemy's LOCAL CATEGORY-2
    :class:`~randomizer.bef.MaterialRef` list in record order. ``evidence``, if
    given, is a mapping from ref POSITION to a :class:`ClipRole`; it is the seam
    a future Role_Source plugs into and is the ONLY way a non-``UNKNOWN`` role
    can be produced. Passing ``evidence`` from a heuristic would be a guess and
    is not what it is for.

    For every enemy in the shipped data this returns all-``UNKNOWN``: task 8.1
    refuted the ELF and the record bytes as role sources and found the BEF ref
    order inconclusive. See
    ``.kiro/specs/anim-table-format/role-notes.md`` for the evidence. A ref
    resolved as ``UNKNOWN`` keeps its own index unless a positional fallback
    supplies one (Requirement 6.4).
    """
    roles: list[ClipRole] = []
    for position in range(len(refs)):
        role = ClipRole.UNKNOWN
        if evidence is not None:
            candidate = evidence.get(position)
            if isinstance(candidate, ClipRole):
                role = candidate
        roles.append(role)
    return roles


def _positional_pairs(source_refs, donor_indices, positional_order):
    """``{slot: donor_index}`` for the chosen positional rule.

    Slots with no counterpart are simply absent.
    """
    if not donor_indices or not source_refs:
        return {}

    if positional_order == POSITIONAL_REF_ORDER:
        return {slot: donor_indices[slot]
                for slot in range(len(source_refs))
                if slot < len(donor_indices)}

    if positional_order != POSITIONAL_SORTED_INDEX:
        raise ValueError(
            f"positional_order must be {POSITIONAL_SORTED_INDEX!r} or "
            f"{POSITIONAL_REF_ORDER!r}, got {positional_order!r}")

    # Today's rule, reproduced exactly: sorted DISTINCT source indices laid
    # positionally onto the donor's index list, surplus clamped onto the last.
    source_indices = sorted({_ref_index(ref) for ref in source_refs})
    per_index = {}
    for position, src in enumerate(source_indices):
        per_index[src] = donor_indices[min(position, len(donor_indices) - 1)]
    return {slot: per_index[_ref_index(ref)]
            for slot, ref in enumerate(source_refs)}


def _ref_index(ref) -> int:
    """The anim index of a ref, accepting a ``MaterialRef`` or a bare int."""
    return ref if isinstance(ref, int) else ref.index


def map_by_role(source_refs, source_roles, donor_indices, donor_roles, *,
                positional_order: str = POSITIONAL_SORTED_INDEX) -> RoleMapping:
    """Pair source refs with donor anims by Clip_Role, positionally otherwise.

    Same-role pairing comes first (Requirement 6.5): each source ref whose role
    is not ``UNKNOWN`` takes the next unconsumed donor anim carrying the SAME
    role, in donor order. Any ref left over -- because its role is ``UNKNOWN``,
    or because the donor has no more anims of that role -- falls back
    POSITIONALLY, and the fallback is PER-REF: it never disturbs a pairing that
    the roles already made (Requirement 6.6).

    ``source_refs`` is the ref list in REF ORDER (``MaterialRef`` objects or
    bare indices). ``source_roles`` must be the same length, as returned by
    :func:`resolve_roles`. ``donor_indices`` are the donor's anim indices in the
    donor's own order; ``donor_roles``, if shorter, is padded with ``UNKNOWN``,
    and if longer is truncated (a role with no anim behind it is not pairable).

    With ALL roles ``UNKNOWN`` and ``positional_order="sorted_index"`` the
    result is identical to what ``cross_world._substitute_anim_refs`` computes
    today (Requirement 6.6), which is why that is the default. See
    :data:`POSITIONAL_REF_ORDER` for the better-founded alternative and why it
    is not yet wired in.

    Deterministic: the result depends only on the arguments.
    """
    source_refs = list(source_refs)
    source_roles = list(source_roles)
    if len(source_roles) != len(source_refs):
        raise ValueError(
            f"resolve_roles must return one role per ref: got "
            f"{len(source_roles)} role(s) for {len(source_refs)} ref(s)")
    donor_indices = list(donor_indices)
    donor_roles = list(donor_roles) if donor_roles else []
    if len(donor_roles) < len(donor_indices):
        donor_roles = donor_roles + [ClipRole.UNKNOWN] * (
            len(donor_indices) - len(donor_roles))
    elif len(donor_roles) > len(donor_indices):
        # A role with no anim behind it cannot be paired with; drop it rather
        # than index past the end of the donor's list.
        donor_roles = donor_roles[:len(donor_indices)]

    by_slot: dict[int, int] = {}
    consumed: set[int] = set()

    # Pass 1 -- same-role pairing, in ref order, first unconsumed donor.
    for slot, role in enumerate(source_roles):
        if role is ClipRole.UNKNOWN:
            continue
        for d, donor_role in enumerate(donor_roles):
            if d in consumed or donor_role is not role:
                continue
            by_slot[slot] = donor_indices[d]
            consumed.add(d)
            break

    # Pass 2 -- per-ref positional fallback for whatever pass 1 did not pair.
    positional = _positional_pairs(source_refs, donor_indices,
                                   positional_order)
    unmatched: list[tuple[int, ClipRole]] = []
    for slot, role in enumerate(source_roles):
        if slot in by_slot:
            continue
        unmatched.append((slot, role))
        if slot in positional:
            by_slot[slot] = positional[slot]
        # else: no counterpart -> no entry -> the ref keeps its own index.

    by_source_index: dict[int, int] = {}
    ambiguous: list[int] = []
    for slot, ref in enumerate(source_refs):
        if slot not in by_slot:
            continue
        src = _ref_index(ref)
        dst = by_slot[slot]
        if src in by_source_index and by_source_index[src] != dst:
            ambiguous.append(src)
        else:
            by_source_index[src] = dst
    for src in ambiguous:
        by_source_index.pop(src, None)

    return RoleMapping(by_slot=by_slot,
                       by_source_index=by_source_index,
                       unmatched=unmatched,
                       ambiguous_source_indices=sorted(set(ambiguous)),
                       positional_order=positional_order)

# ---------------------------------------------------------------------------
# Task 8.4 -- Rig_Binding: does a record NAME its bones or INDEX them?
#
# VERDICT: BY_NAME. Measured, not assumed. The evidence, in the order it
# actually decides the question (full write-up in role-notes.md):
#
#   1. Every `ska_node` opens with a 32-byte NUL-padded NAME (`_ska_node`,
#      transcribed from PSkeletonAnim::Load 0x0016B8B0). The shipped values are
#      real rig identifiers and each enemy has its own convention:
#        Axe_Skeleton 0x2F  hip, l_thigh, l_shin, l_foot
#        Ghost        0x6B  hip, waist, neck, hair1#2
#        Raven        0x37  hips, lthigh, lshin, lfoot
#        Torso_Zombie 0x6A  HIPtop, WAIST, HEAD, JAWLOW
#      The engine's own local for the MEA equivalent is `aszAnimTreeMeshNodeName`
#      -- the executable calls that field a NAME.
#   2. Nothing in either loader indexes a bone array by a small integer taken
#      from the record, and there is no bone-index bounds check anywhere in
#      PSkeletonAnim::Load or ska_node. An index space the engine never bounds
#      is an index space the engine does not have.
#   3. The named discriminator, run rather than argued: the SKA per-record node
#      count `B` does NOT track the enemy's measured MESH bone count. Across the
#      GRAVE pool a record animates a SUBSET (0x2F: B = 22..39 against a 39-bone
#      mesh; 0x6A: B = 18..20 against 28; 0x37: B = 21..23 against 31; 0x6B:
#      B = 22..24 against 36), and B VARIES BETWEEN RECORDS OF THE SAME ENEMY.
#      A dense bone-index space would give one fixed B equal to (or bounding)
#      the bone count. A per-record subset selected by name is exactly what is
#      observed. `rig_binding_evidence` computes this per record so a caller can
#      re-check it instead of trusting this comment.
#   3b. The other half of the named discriminator -- the small-integer header
#      fields -- fails it in the opposite direction: an `ska_node` header's
#      count words reach 41 on Axe_Skeleton, which has 39 bones, so they do not
#      even FIT the index space they would have to index. They track the clip's
#      key/frame count, not the rig.
#   4. The only small integers in a channel header are the MEA `KIND` (1..4) and
#      `MODE` (1..2), which the ELF string pool names ANIM_TRANSLATE /
#      ANIM_ROTATE / ANIM_SCALE / ANIM_MISC and ANIM_EVAL_LINEAR /
#      ANIM_EVAL_SPLINE. Channel TYPE and interpolation mode, not bone indices,
#      and neither reaches 28 let alone 39.
#
# CONSEQUENCE (Requirement 7.4/7.6, recorded in role-notes.md): a Bone_Remap --
# a source-index -> target-index table -- is UNNECESSARY, because there are no
# indices to remap, and it would be IMPOSSIBLE to make a difference anyway: the
# binding key is the name string, so a record binds to a target rig if and only
# if the target rig carries every name the record addresses. `missing_bone_names`
# is that predicate; task 9.3 owns the refusal it feeds.
#
# `BY_INDEX` stays implemented and reachable (see `_index_evidence`) because
# Requirement 7.1 asks the question per record, and a record that answered it
# the other way must not be silently read as BY_NAME. No shipped record does.
# `UNDETERMINED` is returned whenever neither structure is present -- never a
# guess (Requirement 7.3, 7.5).
# ---------------------------------------------------------------------------

import enum

__all__ += [
    "RigBinding",
    "RigEvidence",
    "classify_rig_binding",
    "addressed_bones",
    "rig_binding_evidence",
    "missing_bone_names",
    "can_bind_to_rig",
    "bone_remap_required",
]


class RigBinding(enum.Enum):
    """How a record's channels address the bones they drive."""

    BY_NAME = "by_name"
    BY_INDEX = "by_index"
    UNDETERMINED = "undetermined"


def _is_name_like(value: str) -> bool:
    """Whether a decoded 32-byte field looks like an authored identifier.

    Deliberately permissive about CONTENT (`hair1#2`, `HIPtop`, `node#29` and
    `Combined_Polyset` are all real shipped names) and strict about SHAPE: a
    non-empty run of printable ASCII with no whitespace. A blank or
    non-printable field is not a name, and a record whose fields are all blank
    is `UNDETERMINED` rather than BY_NAME with an empty set.
    """
    if not value:
        return False
    return all(0x21 <= ord(ch) <= 0x7E for ch in value)


def _scan_ska_nodes(record_bytes) -> list:
    """``(name, L0, L1, L2, L3, C)`` per ``ska_node`` of a tag-0x0C record.

    A second, independent read of the node headers `_ska_node` measures with.
    It exists because the classifier needs the header's SMALL-INTEGER fields,
    not just the names, to run the bone-count discriminator -- and because a
    reader that agrees with the measurer about where every node starts is a
    corroboration of the layout rather than a restatement of it.

    Returns ``[]`` for anything that is not a parseable SKA record.
    """
    data = bytes(record_bytes)
    limit = len(data)
    try:
        reader = _Reader(data, 0, limit)
        if reader.u32("record.tag") != TAG_SKA:
            return []
        reader.u32("ska.A")
        count = reader.u32("ska.B")
        nodes = []
        for _ in range(count):
            name = reader.name("ska_node.name")
            l0 = reader.u32("ska_node.L0")
            l1 = reader.u32("ska_node.L1")
            l2 = reader.u32("ska_node.L2")
            l3 = reader.u32("ska_node.L3")
            c = reader.u32("ska_node.C")
            for value, tag in ((l0, "L0"), (l1, "L1"), (l2, "L2"), (l3, "L3")):
                if value:
                    reader.skip(value, f"ska_node.{tag}_block")
            if c:
                reader.skip(c * 32, "ska_node.C_block")
            nodes.append((name, l0, l1, l2, l3, c))
        return nodes
    except AnimParseError:
        return []


@dataclass(frozen=True)
class RigEvidence:
    """Everything the classifier looked at, so a caller can re-judge it.

    Attributes
    ----------
    binding:
        The verdict.
    names:
        The bone / mesh-node names the record addresses, in file order, when
        ``binding`` is ``BY_NAME``. NON-EMPTY whenever ``BY_NAME`` is returned
        (Requirement 7.4) -- that is the classifier's precondition for saying
        BY_NAME at all, not an afterthought.
    indices:
        The bone indices the record addresses when ``binding`` is ``BY_INDEX``.
        Empty otherwise.
    node_count:
        ``B`` for SKA, ``N`` for MEA -- how many nodes this ONE record animates.
    bone_count:
        The rig's measured bone count, when the caller supplied one
        (``mesh_port.mesh_entry_signature(prs, mesh_index)[2]``). ``None`` when
        it was not supplied; the verdict does not depend on it.
    highest_index:
        Requirement 7.2: the highest bone index the record addresses, ``None``
        unless ``binding`` is ``BY_INDEX``.
    index_in_range:
        Requirement 7.2's comparison: whether ``highest_index < bone_count``.
        ``None`` when either value is unavailable.
    tracks_bone_count:
        The task's named discriminator. True when ``node_count == bone_count``,
        i.e. when this record's node loop covers the rig exactly -- the shape a
        dense index space would have. False when the record animates a SUBSET.
        ``None`` without a ``bone_count``.
    small_int_max:
        The largest small integer (< 256) in the record's node / channel
        headers. A bone index would stay below ``bone_count`` and would GROW
        with the rig. Measured, it does neither: Axe_Skeleton reaches 41 against
        a 39-bone rig, i.e. it EXCEEDS the index space it would have to fit in.
        These words are key counts (they track the clip's frame count ``A``),
        not bone indices.
    notes:
        Human-readable evidence lines, for role-notes and for log output.
    """

    binding: RigBinding
    names: tuple = ()
    indices: tuple = ()
    #: The record's TYPE TAG, or ``None`` when the record does not parse. The
    #: MEA / SKA distinction decides which ENGINE BIND PATH a ported record
    #: goes through, and the two paths do NOT behave the same way about a name
    #: the target rig does not carry -- see ``MEA_UNMATCHED_NODE_IS_SKIPPED``.
    tag: int | None = None
    node_count: int = 0
    bone_count: int | None = None
    highest_index: int | None = None
    index_in_range: bool | None = None
    tracks_bone_count: bool | None = None
    small_int_max: int | None = None
    notes: tuple = ()

    @property
    def identities(self) -> tuple:
        """The bone identities addressed, whichever kind they are."""
        return self.names if self.binding is RigBinding.BY_NAME else self.indices


def _index_evidence(nodes, bone_count):
    """Whether SKA node headers look like a DENSE BONE-INDEX SPACE instead.

    The only field that could carry one is the node header's ``C`` word (header
    ``0x30``); every other word is a byte length the loader consumes. So the
    signature of an index space here is: no names at all, at least two nodes,
    every ``C`` distinct, and -- when a rig size is known -- every ``C`` below
    it. Returns the sorted indices or ``None``.

    NO SHIPPED RECORD MATCHES THIS. It is implemented because Requirement 7.1
    asks the question of each record and a record that answered differently must
    not be misread as BY_NAME; it is not implemented because the format is
    suspected of being index-based.
    """
    if len(nodes) < 2:
        return None
    if any(_is_name_like(node[0]) for node in nodes):
        return None
    values = [node[5] for node in nodes]
    if len(set(values)) != len(values):
        return None
    if bone_count is not None and any(v >= bone_count for v in values):
        return None
    return tuple(sorted(values))


def rig_binding_evidence(record_bytes, *, bone_count: int | None = None) -> RigEvidence:
    """Classify one record's Rig_Binding and report WHY. Never raises.

    ``record_bytes`` is one record INCLUDING its TYPE TAG word, as
    ``extract_record_from_buffer`` returns it. ``bone_count`` is the rig's
    measured bone count when the caller has it; it is used only for
    Requirement 7.2's comparison and for the tracking discriminator, never to
    decide the verdict -- a record's binding is a property of the record.

    An unparseable record is ``UNDETERMINED`` with the parse failure in
    ``notes``, matching this module's rule that a failure is reported and never
    guessed around.
    """
    data = bytes(record_bytes)
    notes: list = []
    try:
        info = measure_record(data, 0, limit=len(data))
    except AnimParseError as exc:
        return RigEvidence(
            binding=RigBinding.UNDETERMINED,
            bone_count=bone_count,
            notes=(f"record does not parse from its own bytes, so its binding "
                   f"cannot be determined: {exc}",))

    node_count = info.node_count
    tracks = None if bone_count is None else node_count == bone_count
    if bone_count is not None:
        notes.append(
            f"node_count {node_count} vs measured rig bone_count {bone_count}: "
            + ("EQUAL, so this record's node loop covers the rig exactly"
               if tracks else
               f"a SUBSET ({node_count - bone_count:+d}), which a dense bone-"
               f"index space cannot be"))

    if info.tag == TAG_SKA:
        nodes = _scan_ska_nodes(data)
        small_ints = [value for node in nodes for value in node[1:]
                      if value < 256]
        small_int_max = max(small_ints) if small_ints else None
        indices = _index_evidence(nodes, bone_count)
        if indices is not None:
            highest = indices[-1]
            notes.append(
                f"no ska_node carries a name and every node header C word is "
                f"distinct ({indices[0]}..{highest}), which is a dense "
                f"bone-index space")
            return RigEvidence(
                binding=RigBinding.BY_INDEX, indices=indices, tag=info.tag,
                node_count=node_count, bone_count=bone_count,
                highest_index=highest,
                index_in_range=None if bone_count is None else highest < bone_count,
                tracks_bone_count=tracks, small_int_max=small_int_max,
                notes=tuple(notes))
        names = tuple(node[0] for node in nodes if _is_name_like(node[0]))
        if names and len(names) == len(nodes):
            notes.append(
                f"every one of the {len(nodes)} ska_node headers opens with a "
                f"32-byte NUL-padded NAME ({', '.join(names[:4])}"
                f"{', ...' if len(names) > 4 else ''}), which is the bone "
                f"identity the engine binds on (PSkeletonAnim::Load, local "
                f"aszAnimTreeMeshNodeName in the MEA twin)")
            if small_int_max is not None:
                notes.append(
                    f"largest small integer in any node header is "
                    f"{small_int_max}"
                    + ("" if bone_count is None else
                       f", which does not reach bone_count - 1 = "
                       f"{bone_count - 1}"
                       if small_int_max < bone_count - 1 else
                       f", which is not below bone_count - 1 = "
                       f"{bone_count - 1}"))
            return RigEvidence(
                binding=RigBinding.BY_NAME, names=names, tag=info.tag,
                node_count=node_count, bone_count=bone_count,
                tracks_bone_count=tracks, small_int_max=small_int_max,
                notes=tuple(notes))
        notes.append(
            f"{len(names)} of {len(nodes)} ska_node header(s) carry a name and "
            f"no dense index space is present, so neither addressing mode is "
            f"established")
        return RigEvidence(
            binding=RigBinding.UNDETERMINED, node_count=node_count,
            tag=info.tag, bone_count=bone_count, tracks_bone_count=tracks,
            small_int_max=small_int_max, notes=tuple(notes))

    # MEA (tag 0x0A). Same question, different table: its nodes are MESH nodes,
    # named by the same 32-byte NUL-padded field, and the engine's own local for
    # it is `aszAnimTreeMeshNodeName`. A MEA record cannot drive a skeleton at
    # all (role-notes records two GRAVE pool "anims" that are MEA), but its
    # addressing mode is still by name.
    names = tuple(name for name in info.node_names if _is_name_like(name))
    if names and len(names) == len(info.node_names) and names:
        notes.append(
            f"every one of the {len(names)} mea_node headers opens with a "
            f"32-byte NUL-padded NAME ({', '.join(names[:4])}"
            f"{', ...' if len(names) > 4 else ''}); these name MESH NODES, not "
            f"bones, so this record cannot drive a skeleton")
        notes.append(
            "the only small integers in a mea_channel header are KIND (1..4 = "
            "ANIM_TRANSLATE / ANIM_ROTATE / ANIM_SCALE / ANIM_MISC) and MODE "
            "(1..2 = ANIM_EVAL_LINEAR / ANIM_EVAL_SPLINE), both named in the "
            "ELF string pool as enums, so neither is a bone index")
        return RigEvidence(
            binding=RigBinding.BY_NAME, names=names, node_count=node_count,
            tag=info.tag, bone_count=bone_count, tracks_bone_count=tracks,
            small_int_max=4, notes=tuple(notes))
    notes.append(
        f"{len(names)} of {len(info.node_names)} mea_node name field(s) are "
        f"name-like, so no addressing mode is established")
    return RigEvidence(
        binding=RigBinding.UNDETERMINED, node_count=node_count, tag=info.tag,
        bone_count=bone_count, tracks_bone_count=tracks, notes=tuple(notes))


def classify_rig_binding(record_bytes, *, bone_count: int | None = None) -> RigBinding:
    """``BY_NAME`` / ``BY_INDEX`` / ``UNDETERMINED`` for one record.

    Requirement 7.1. Thin wrapper over ``rig_binding_evidence`` so the verdict
    and its evidence can never disagree. ``UNDETERMINED`` for a record with no
    discriminating structure (Requirement 7.3); task 9.3's porter refuses those
    (Requirement 7.5).
    """
    return rig_binding_evidence(record_bytes, bone_count=bone_count).binding


def addressed_bones(record_bytes, *, bone_count: int | None = None) -> tuple:
    """``(binding, identities)`` -- the bone identities a record addresses.

    Requirements 7.2, 7.3, 7.4. ``identities`` is an ordered tuple in file
    order (order matters for reporting and is stable, unlike a set), NON-EMPTY
    whenever ``binding`` is ``BY_NAME``: names for BY_NAME, indices for
    BY_INDEX, empty for UNDETERMINED.
    """
    evidence = rig_binding_evidence(record_bytes, bone_count=bone_count)
    return evidence.binding, evidence.identities


def missing_bone_names(source_names, target_names) -> tuple:
    """Source bone names with NO counterpart in the target rig, in order.

    The binding key is the name STRING, so this is the whole compatibility
    question for a BY_NAME record: an empty result means every channel finds
    its bone, and a non-empty result means the port must be REFUSED (Requirement
    7.6 -- the refusal itself is task 9.3's; this is the predicate it asks).

    Matching is EXACT and case-sensitive, because the engine's lookup is a
    string compare: `HIPtop` is not `hip`. Duplicates are collapsed and order is
    the source's.
    """
    target = set(target_names)
    missing: list = []
    for name in source_names:
        if name not in target and name not in missing:
            missing.append(name)
    return tuple(missing)


#: MEASURED out of ``MAXDEMOR.ELF``: a MEA (tag ``0x0A``) node whose NAME the
#: target rig does not carry is SKIPPED by the engine, not fatal to it.
#:
#: ``RecurseAttachAnim__9PMeshNodeFP22PMeshNodeAnimDataTable`` @ ``0x00175B20``
#: walks the table's node entries (in-memory stride 36, bumped at
#: ``0x00175BCC``) and for each one does::
#:
#:     0x00175BA0  jal   Find__9PMeshNodeFPCcUi        ; look the NAME up
#:     0x00175BA8  beql  $v0, $zero, 0x00175BC4        ; NOT FOUND -> skip
#:     0x00175BAC  addiu $s1, $s1, 1                   ; (annulled)
#:     0x00175BB0  lw    $a1, 32($s3)                  ; the PAnim*
#:     0x00175BB4  jal   AttachAnim__9PMeshNodeFP5PAnim
#:
#: The ``beql`` (word ``0x50400006``) branches PAST the ``AttachAnim`` call to
#: the loop's own increment/compare when ``Find`` returns NULL, annulling its
#: delay slot. There is no ``CdsAssert``, no error return and no abort on that
#: path -- the function returns ``1`` unconditionally at ``0x00175BE4``. So an
#: unmatched MEA node costs exactly one un-animated node, and nothing else.
#:
#: NOT CLAIMED FOR SKA. ``PSkeletonAnim``'s bind path was not disassembled, so
#: an SKA record with an unmatched bone name keeps REFUSING. Requirement 7.6's
#: refusal stands wherever it has not been measured away.
MEA_UNMATCHED_NODE_IS_SKIPPED = True

__all__ += ["MEA_UNMATCHED_NODE_IS_SKIPPED", "unmatched_name_verdict"]


def unmatched_name_verdict(record_bytes, target_bone_names, *,
                           bone_count: int | None = None) -> tuple:
    """``(missing, tolerated, note)`` for a BY_NAME record against a rig.

    ``missing`` is :func:`missing_bone_names`. ``tolerated`` says whether the
    port may proceed ANYWAY, and it is True only when

    * the record is a MEA record (tag ``0x0A``), whose bind path is the one
      measured to SKIP an unmatched name (:data:`MEA_UNMATCHED_NODE_IS_SKIPPED`),
      and
    * at least one of the record's names DOES resolve.

    The second condition is a POLICY choice, not a measurement, and it is
    deliberately conservative: a record where NOTHING binds animates nothing, so
    committing it would trade a loud refusal for a silent loss. That is the one
    case where refusing is still the honest answer.

    An SKA record, a BY_INDEX record and an UNDETERMINED record are never
    tolerated -- the tolerance is exactly as wide as the evidence behind it.
    """
    evidence = rig_binding_evidence(record_bytes, bone_count=bone_count)
    missing = missing_bone_names(evidence.names, target_bone_names)
    if not missing:
        return (), False, ""
    if evidence.binding is not RigBinding.BY_NAME:
        return missing, False, ""
    if evidence.tag != TAG_MEA:
        return missing, False, (
            f"the record is tag 0x{(evidence.tag or 0):02X} (SKA), whose bind "
            f"path has NOT been measured, so an unmatched name is not "
            f"tolerated")
    resolved = len(evidence.names) - len(missing)
    if resolved <= 0:
        return missing, False, (
            "not one of the record's names resolves against the target rig, so "
            "the record would animate nothing; a silent no-op is worse than a "
            "refusal")
    shown = ", ".join(repr(name) for name in missing[:6])
    more = "" if len(missing) <= 6 else f" (+{len(missing) - 6} more)"
    return missing, True, (
        f"{len(missing)} of the {len(evidence.names)} MEA node name(s) this "
        f"record addresses ({shown}{more}) have no counterpart in the "
        f"{len(target_bone_names)}-name target rig; the other {resolved} do. "
        f"The record is appended UNCHANGED and the engine SKIPS the unmatched "
        f"node(s): RecurseAttachAnim @0x00175B20 beql-skips AttachAnim when "
        f"PMeshNode::Find returns NULL (word 0x50400006 @0x00175BA8), with no "
        f"assert and no error return. No name is substituted, no bone is "
        f"invented and no byte of the record is altered")


def can_bind_to_rig(record_bytes, target_bone_names, *,
                    bone_count: int | None = None) -> tuple:
    """``(ok, reasons)`` -- may this record be ported onto that rig?

    ``ok`` is True for a ``BY_NAME`` record every one of whose names the target
    rig carries, and -- since the ELF measurement recorded in
    :data:`MEA_UNMATCHED_NODE_IS_SKIPPED` -- for a MEA record some of whose
    names it does not, because the engine skips exactly those nodes. See
    :func:`unmatched_name_verdict` for the precise, deliberately narrow shape of
    that tolerance and for the two cases it still refuses.

    An ``UNDETERMINED`` binding is never portable (Requirement 7.5), and a
    ``BY_INDEX`` record needs a Bone_Remap the target rig cannot supply from a
    name list alone, so it is refused here too. An SKA record with an unmatched
    bone name is still refused under Requirement 7.6.

    Never raises; the caller turns ``reasons`` into a refusal.
    """
    evidence = rig_binding_evidence(record_bytes, bone_count=bone_count)
    if evidence.binding is RigBinding.UNDETERMINED:
        return False, [
            "the record's Rig_Binding is UNDETERMINED, so it must not be "
            "ported (Requirement 7.5): " + "; ".join(evidence.notes)]
    if evidence.binding is RigBinding.BY_INDEX:
        highest = evidence.highest_index
        return False, [
            f"the record addresses bones BY INDEX (highest {highest}), so a "
            f"Bone_Remap from the source rig's index space to the target's is "
            f"required and a target NAME LIST cannot supply one"]
    missing, tolerated, note = unmatched_name_verdict(
        record_bytes, target_bone_names, bone_count=bone_count)
    if missing and not tolerated:
        shown = ", ".join(repr(name) for name in missing[:6])
        more = "" if len(missing) <= 6 else f" (+{len(missing) - 6} more)"
        return False, [
            f"{len(missing)} of the {len(evidence.names)} bone name(s) this "
            f"record addresses have no counterpart in the target rig "
            f"({shown}{more}), so the port must be refused (Requirement 7.6)"
            + (f": {note}" if note else "")]
    return True, []


def bone_remap_required(record_bytes, *, bone_count: int | None = None) -> bool:
    """Whether porting this record between differing bone lists needs a remap.

    Requirement 7.4. True only for a ``BY_INDEX`` record: an index space is
    meaningless outside the rig that defines it. For ``BY_NAME`` -- which is
    every shipped record -- a remap is UNNECESSARY and would change nothing,
    because the binding key is the name; compatibility is decided by
    ``missing_bone_names`` instead. ``UNDETERMINED`` is not portable at all, so
    the question does not arise and the answer is False.
    """
    return classify_rig_binding(record_bytes,
                                bone_count=bone_count) is RigBinding.BY_INDEX

# ---------------------------------------------------------------------------
# Task 9.1 -- appending records to a target world's anim table.
#
# The measured layout this rebuild is built on (task 3.3, all five worlds, and
# now enforced by a gate that passes for all five):
#
#     anchor + 0   u32  2                    tail word of the mesh table
#     anchor + 4   u32  RECORD COUNT         bumped by len(records)
#     anchor + 8        records_start        record 0's TYPE TAG
#     ...               records              tile contiguously
#     records_end       16-byte TRAILER      preserved VERBATIM, after the
#     EOF                                   appended records
#
# So an append is one splice at `records_end` plus one u32 bump at `anchor + 4`.
# Everything before the count word, and every existing record byte, is copied
# through untouched (Requirement 8.3).
#
# The trailer is re-emitted from `region.trailer` -- whatever is ACTUALLY in the
# buffer -- and never from `ANIM_TRAILER`. All five shipped worlds carry the
# standard bytes, but a world that did not must round-trip its own.
#
# REFUSAL, not repair. `append_anim_records` raises `AnimParseError` rather than
# return a buffer when the region cannot be located, when any record to append
# does not measure cleanly in isolation, or when the resulting count is
# implausible. There is no partial commit: a table whose count and whose record
# chain disagree desynchronises the engine's sequential read of the WHOLE table
# and hard-freezes the level load. Half a correct append is worse than none.
# ---------------------------------------------------------------------------

__all__ += ["AnimAppendResult", "append_anim_records"]


@dataclass(frozen=True)
class AnimAppendResult:
    """What an append produced, beyond the buffer itself.

    ``append_anim_records`` returns ``(new_data, indices, trailer_offset)``; this
    is the same information with names, for callers (task 9.3) that would
    otherwise unpack a tuple in three places.
    """

    indices: tuple
    new_count: int
    old_count: int
    records_end: int
    trailer_offset: int
    appended_bytes: int


def append_anim_records(prs_bytes, records):
    """Append complete anim records to a world ``.PRS``'s anim table.

    ``records`` is a sequence of COMPLETE record byte strings, TYPE TAG word
    included, exactly as ``extract_record_from_buffer`` returns them (typically
    taken from another world and carried across after that world's buffer was
    dropped).

    Returns ``(new_data, indices, trailer_offset)``:

    * ``new_data`` -- the rebuilt buffer, ``bytes``;
    * ``indices`` -- the index each appended record now occupies, which is
      ``range(old_count, old_count + len(records))`` because the table is
      POSITIONAL: a record's index is its ordinal in the chain and nothing else
      names it;
    * ``trailer_offset`` -- where the preserved trailing bytes now sit
      (Requirement 8.4).

    The design writes this as returning ``(new buffer, indices)``. The third
    element is added because Requirement 8.4 asks the porter to REPORT where the
    trailing bytes ended up, and recomputing it in the caller would be a second
    place that has to agree about the layout.

    Appending zero records returns a byte-identical buffer and no indices.

    Raises ``AnimParseError`` -- producing NO buffer -- when the anim region
    cannot be located, when a record does not measure cleanly in isolation
    (``round_trip_record``), or when the resulting count would be implausible.
    Requirements 8.1, 8.2, 8.3, 8.4.

    MEMORY: the caller transiently holds TWO multi-megabyte buffers, the input
    and the result. The result is built in one pass (a single concatenation, no
    intermediate copies), and the caller should drop the input as soon as the
    result exists -- the one-``.PRS``-at-a-time rule (Requirement 10.3) is about
    steady state, and an append cannot avoid the moment of overlap.
    """
    records = [bytes(record) for record in records]

    region = find_anim_region(prs_bytes)
    if region is None:
        raise AnimParseError(
            "cannot append anim records: no TFA\\xff anchor in this buffer, so "
            "the anim table cannot be located (Requirement 8.1)")

    old_count = region.count
    new_count = old_count + len(records)
    if new_count > 0xFFFF:
        raise AnimParseError(
            f"cannot append anim records: the resulting count {new_count} "
            f"({old_count} + {len(records)}) is not a plausible table size")

    # Every record must be self-describing and complete ON ITS OWN before a
    # single byte is written. A record that only measures inside its original
    # archive is a record whose length was borrowed from its neighbour.
    for position, record in enumerate(records):
        consumed, events, byte_identical = round_trip_record(
            record, index=old_count + position)
        if events or not byte_identical or consumed != len(record):
            detail = (_describe_events(events) if events else
                      f"consumed {consumed} of {len(record)} byte(s), "
                      f"byte_identical {byte_identical}")
            raise AnimParseError(
                f"cannot append anim records: record {position} of "
                f"{len(records)} does not measure cleanly in isolation, so "
                f"nothing is appended: {detail}")

    if not records:
        # Nothing to splice and nothing to bump: hand back the same bytes.
        return (bytes(prs_bytes), [],
                region.records_end)

    appended = b"".join(records)
    new_data = b"".join((
        bytes(prs_bytes[:region.anchor + 4]),
        struct.pack("<I", new_count),
        bytes(prs_bytes[region.anchor + 8:region.records_end]),
        appended,
        region.trailer,
    ))
    indices = list(range(old_count, new_count))
    return new_data, indices, region.records_end + len(appended)


def append_anim_records_detail(prs_bytes, records):
    """``(new_data, AnimAppendResult)`` -- ``append_anim_records`` with names."""
    region = find_anim_region(prs_bytes)
    old_count = 0 if region is None else region.count
    records_end = 0 if region is None else region.records_end
    new_data, indices, trailer_offset = append_anim_records(prs_bytes, records)
    return new_data, AnimAppendResult(
        indices=tuple(indices),
        new_count=old_count + len(indices),
        old_count=old_count,
        records_end=records_end,
        trailer_offset=trailer_offset,
        appended_bytes=trailer_offset - records_end,
    )


__all__ += ["append_anim_records_detail"]

# ---------------------------------------------------------------------------
# Task 9.3 -- `port_anim_records`: port an enemy's OWN anim records.
#
# WHAT THIS FIXES, and why it is the function the whole spec was aimed at.
#
# The shipped cross-world feature cannot port anim records, so it SUBSTITUTES:
# a ported enemy's LOCAL CATEGORY-2 refs are repointed at a target-world
# NATIVE's anim indices (`cross_world.select_anim_donor` /
# `_substitute_anim_refs`, behind `ANIM_SUBSTITUTION_ENABLED`). Task 8.4 proved
# SKA records address bones BY NAME, and the four GRAVE donor enemies use four
# MUTUALLY INCOMPATIBLE bone naming conventions -- Torso_Zombie's `HIPtop` /
# `WAIST` shares not one name with Axe_Skeleton's `hip` / `l_thigh`. Matching is
# an exact string compare, so a BORROWED clip can never bind correctly. That is
# the measured explanation for the user's report that limbs move but the
# animation is wrong, and it is a HARD CEILING on the heuristic: no donor choice
# removes it, because the defect is not donor selection.
#
# Porting the enemy's OWN records carries its OWN bone names, so every channel
# resolves BY CONSTRUCTION. That is what this function does.
#
# THE CONSEQUENCE OF BY_NAME BINDING, and the reason for the Requirement 7.6
# refusal: a ported record NAMES bones that must exist in the rig it drives.
# When the enemy's own MESH is ported alongside it (which
# `cross_world._port_enemy` already does), the rig comes with it and the names
# match. So `target_bone_names` is the bone-name list of THE RIG THE RECORD WILL
# DRIVE -- the ported mesh's own bones -- NOT some target-world native's. A
# source bone with no counterpart there is a REFUSAL. Nothing is ever remapped
# and nothing is ever partially bound: `can_bind_to_rig` is the predicate and it
# refuses `UNDETERMINED` outright too (Requirement 7.5).
#
# ORDER OF OPERATIONS, exactly the design's, all on STAGED copies that the
# caller adopts only on FULL success:
#
#   1. gate the TARGET anim table (`assess_anim_table`); refuse if it fails;
#   2. per source record, `assess_source_record`; refuse if any fails;
#   3. rig classification + bone-name compatibility; refuse per above;
#   4. DEDUPE through the memo, mirroring `cross_world.mesh_memo`: a record
#      wanted by several refs, or by several enemies in one session, is appended
#      ONCE and every ref points at the one new index (Requirement 8.8);
#   5. `append_anim_records` on the staged buffer;
#   6. rewrite the enemy's LOCAL CATEGORY-2 refs with
#      `bef.set_material_ref_index`, PRESERVING THE FLAG BYTE. The flag carries
#      the category and the global bit, so a changed flag repoints the ref at a
#      different resource table entirely. GLOBAL refs are NEVER touched;
#   7. bounds-check every rewritten index against the NEW count (Req 8.6);
#   8. post-append re-walk: `assess_anim_table` on the staged result must pass
#      AND the walk must be clean with `trailing == 0` (Requirement 8.9);
#   9. only then does the caller adopt the staged buffer.
#
# REFUSAL SHAPE: this function NEVER raises for a recoverable condition. It
# returns an `AnimPortOutcome` whose `ok` is False and whose `reason` reads the
# way `mesh_port`'s verdicts do, because `cross_world.refuse(plan, reason,
# details)` already consumes exactly that -- the design's "cross_world converts
# a failed verdict into session.refuse(...)". The `_PortRefusal` route was the
# alternative; a verdict object was chosen so `anim_port` keeps its module-wide
# rule that no public function raises for malformed data, and so the caller does
# not have to catch across a staged-buffer boundary.
#
# ALL-OR-NOTHING: a refusal at ANY step mutates nothing. Nothing here writes
# through to the caller -- not the buffer, not the blob, not the memo. On a
# refusal `prs` and `blob` are None and the caller keeps its own bytes, which
# are therefore byte-identical.
#
# THE TWO HAZARDS the design calls out (task 9.5 owns their fixes; this function
# is written so as not to CREATE them):
#
#   * `WorldPortSession.target_anim_count` is cached and reads the PRISTINE
#     buffer, so it goes STALE the moment an append lands. This function never
#     consults it: every bound it checks comes from the count in the buffer it
#     was handed, and `new_count` is REPORTED so the caller can refresh.
#   * appending MESH entries SHIFTS the anim anchor (`find_anim_table_anchor` is
#     derived from the first `TFA\xff` offset, which sits after the mesh table).
#     So this function re-locates the region from the buffer it is ACTUALLY
#     HANDED, on every call, and caches no anchor across a mutation.
#
# ORDERING within a combined mesh+anim port -- mesh entries inserted first, the
# anim region re-located from the staged buffer afterwards -- is TASK 9.5's
# BUSINESS, not this function's. This function is correct for whatever buffer it
# is given; it does not decide when it is given it.
#
# This function is NOT wired into `WorldPortSession` and changes no live
# behaviour. The routing decision and `ANIM_PORT_ENABLED` are task 10.1.
# ---------------------------------------------------------------------------

__all__ += [
    "SourceRecord",
    "AnimSourceBundle",
    "collect_source_records",
    "AnimPortOutcome",
    "port_anim_records",
]


@dataclass(frozen=True)
class SourceRecord:
    """One gated source anim record, carried across a buffer boundary.

    Holds the record's BYTES, so the multi-megabyte source ``.PRS`` can be
    dropped before the target buffer is read -- which is what makes the
    one-``.PRS``-at-a-time rule satisfiable during a port (Requirement 10.3).
    """

    source_world: str
    index: int
    tag: int
    data: bytes
    binding: RigBinding
    bone_names: tuple

    @property
    def key(self) -> tuple:
        """Memo key, mirroring ``cross_world.mesh_memo``'s shape."""
        return (self.source_world, 2, self.index)


@dataclass
class AnimSourceBundle:
    """Every source record an enemy wants, gated while the source was resident.

    ``ok`` False means at least one record failed ``assess_source_record`` (or
    its rig classification); ``reasons`` says which. A refused bundle is refused
    by ``port_anim_records`` too, so a caller may collect first and decide later
    without having to re-check.
    """

    source_world: str
    records: dict = field(default_factory=dict)
    ok: bool = True
    reasons: list = field(default_factory=list)
    details: list = field(default_factory=list)

    @property
    def reason(self) -> str:
        return "; ".join(self.reasons)

    @property
    def bone_names(self) -> tuple:
        """Every bone / node name the bundle's records address, in order.

        The rig a ported enemy brings with it must carry all of these. Useful
        for reporting and for callers whose ported mesh's bone list is the union
        of what its own clips name.
        """
        seen: list = []
        for index in sorted(self.records):
            for name in self.records[index].bone_names:
                if name not in seen:
                    seen.append(name)
        return tuple(seen)


def collect_source_records(source_data, indices, *, source_world,
                           label=None) -> AnimSourceBundle:
    """Gate and extract source anim records. Never raises.

    Steps 2 and 3 of the port, run WHILE THE SOURCE BUFFER IS RESIDENT so the
    caller can drop it before touching the target: each wanted index goes
    through ``assess_source_record`` (which walks the source table, bounds the
    record and round-trips it in isolation) and then through
    ``rig_binding_evidence`` for its addressing mode and bone names.

    ``indices`` may repeat; distinct indices are gated once each.
    """
    world = str(source_world).upper()
    tag = label or f"{world}.PRS"
    bundle = AnimSourceBundle(source_world=world)
    for index in sorted({int(i) for i in indices}):
        verdict = assess_source_record(source_data, index, label=tag)
        bundle.details.extend(verdict.details)
        if not verdict.ok:
            bundle.ok = False
            bundle.reasons.extend(verdict.reasons)
            continue
        evidence = rig_binding_evidence(verdict.record_bytes)
        if evidence.binding is not RigBinding.BY_NAME:
            bundle.ok = False
            bundle.reasons.append(
                f"{tag} anim[{index}]'s Rig_Binding is "
                f"{evidence.binding.value.upper()}, so it must not be ported "
                f"(Requirement 7.5): {'; '.join(evidence.notes)}")
            continue
        bundle.records[index] = SourceRecord(
            source_world=world, index=index, tag=verdict.tag,
            data=verdict.record_bytes, binding=evidence.binding,
            bone_names=tuple(evidence.names))
        bundle.details.append(
            f"{tag} anim[{index}] tag=0x{verdict.tag:02X} "
            f"{len(verdict.record_bytes)} B, {evidence.binding.value}, "
            f"{len(evidence.names)} bone name(s)")
    return bundle


@dataclass
class AnimPortOutcome:
    """Result of one enemy's anim port. ``ok`` False means REFUSE.

    Deliberately the same shape as ``AnimTrustVerdict`` / ``mesh_port``'s
    verdicts -- ``ok`` / ``reasons`` / ``details`` / ``reason`` -- because
    ``cross_world.refuse(plan, reason, details)`` already consumes exactly that.

    Attributes
    ----------
    prs / blob:
        The STAGED buffer and STAGED blob, to be adopted by the caller ONLY when
        ``ok``. ``None`` on a refusal, so a caller cannot mistake a refusal for a
        rewrite and its own bytes stay byte-identical.
    new_indices:
        ``(source_world, source_index) -> target anim index``, covering both the
        records appended by this call and the ones the memo already had.
    appended:
        The source indices actually appended, in append order. Its length is the
        count delta, so a record wanted by several refs appears ONCE
        (Requirement 8.8).
    rewrites:
        ``blob idx_offset -> (flag, new_index)`` for every rewritten LOCAL
        CATEGORY-2 ref. The flag is the byte read back OUT of the staged blob,
        so it is evidence the flag survived rather than a restatement of intent.
    memo:
        The staged memo -- the caller's memo plus this port's additions. Adopt it
        only alongside ``prs`` and ``blob``, or a later enemy will be pointed at
        an index that was never committed.
    old_count / new_count:
        The target table's declared count before and after. ``new_count`` is what
        every rewritten index was bounds-checked against (Requirement 8.6) and
        what a caller caching an anim count must refresh to (the stale-cache
        hazard, task 9.5).
    trailer_offset:
        Where the preserved 16-byte trailer now sits (Requirement 8.4).
    """

    ok: bool
    reasons: list = field(default_factory=list)
    details: list = field(default_factory=list)
    prs: bytes | None = None
    blob: bytes | None = None
    new_indices: dict = field(default_factory=dict)
    appended: tuple = ()
    rewrites: dict = field(default_factory=dict)
    memo: dict = field(default_factory=dict)
    old_count: int | None = None
    new_count: int | None = None
    trailer_offset: int | None = None

    @property
    def reason(self) -> str:
        return "; ".join(self.reasons)

    @property
    def appended_count(self) -> int:
        return len(self.appended)


def _refuse(reasons, details, memo) -> AnimPortOutcome:
    """A refusal: no buffer, no blob, no memo addition. Mutates nothing."""
    return AnimPortOutcome(ok=False, reasons=list(reasons),
                           details=list(details), memo=dict(memo))


def port_anim_records(target_data, blob, refs, source, target_bone_names, *,
                      memo=None, source_world=None, label="target",
                      extra_refs=()):
    """Port an enemy's OWN anim records into ``target_data``. Never raises.

    Parameters
    ----------
    target_data:
        The target world's ``.PRS`` bytes AS THEY ARE NOW -- the staged buffer
        mid-port, not a pristine copy. The anim region is RE-LOCATED from this
        buffer on every call, so a mesh insert that shifted the anchor is
        accounted for automatically and no anchor is ever cached across a
        mutation. WHEN this is called relative to a mesh insert is task 9.5's
        ordering rule, not this function's business.
    blob:
        The enemy's ``.BEF`` blob, already carrying whatever CATEGORY-1 rewrites
        the mesh port made.
    refs:
        The blob's material refs as ``bef.parse_material_refs`` returned them
        FOR THIS BLOB. Only LOCAL CATEGORY-2 refs are touched; GLOBAL refs are
        never touched, and neither is anything in another category.
    source:
        An :class:`AnimSourceBundle` from :func:`collect_source_records` (the
        memory-disciplined form -- the source ``.PRS`` is already dropped), or a
        raw source ``.PRS`` buffer, in which case ``source_world`` is required
        and the records are gated here (both buffers resident, the design's
        literal ``port_anim_records(target_data, source_data, indices)`` shape).
    target_bone_names:
        The bone names of THE RIG THE RECORDS WILL DRIVE -- the ported enemy's
        own mesh, ported alongside. NOT a target-world native's rig: a native's
        names are a different convention and every channel would fail to bind,
        which is the very defect this function exists to remove.
    memo:
        The session dedupe memo, keyed ``(source_world, 2, source_index)`` like
        ``cross_world.mesh_memo``. READ, never written: the staged memo comes
        back in ``outcome.memo`` for the caller to adopt on success.

    Returns an :class:`AnimPortOutcome`. On a refusal it carries the reason and
    NOTHING ELSE -- the caller's buffer, blob and memo are untouched, so a
    refused port leaves them byte-identical (Requirement 8.7).
    """
    from .bef import (
        MATERIAL_CATEGORY_ANIM,
        MATERIAL_INDEX_MASK,
        MaterialSectionError,
        parse_material_refs,
        set_material_ref_index,
    )

    memo = {} if memo is None else memo
    details: list = []
    target_names = tuple(target_bone_names or ())

    local = [r for r in refs
             if not r.is_global and r.category == MATERIAL_CATEGORY_ANIM]
    untouched = len(refs) - len(local)

    # EXTRA REFS: LOCAL CATEGORY-2 references that `parse_material_refs` cannot
    # see because they do not sit at the START of a record body. They are ported
    # and rewritten exactly like `local`, but they are held SEPARATE from `refs`
    # because the proof below re-parses the blob and compares ref for ref -- the
    # parser will not see these afterwards either, so including them in `refs`
    # would make the length check fail and refuse the whole enemy. They get
    # their own byte-level proof instead.
    extra_local = [r for r in (extra_refs or ())
                   if not r.is_global and r.category == MATERIAL_CATEGORY_ANIM]
    all_local = list(local) + list(extra_local)
    if extra_local:
        details.append(
            f"{label}: {len(extra_local)} additional LOCAL CATEGORY-2 ref(s) "
            f"supplied that parse_material_refs cannot see, at "
            + ", ".join(f"0x{r.idx_offset:X}(idx{r.index})"
                        for r in extra_local))

    # ---- 1. the TARGET gate ------------------------------------------------
    # Assessed on the buffer handed in, not on a pristine copy: this is the
    # table the append actually lands in.
    gate = assess_anim_table(target_data, label=label)
    details.extend(gate.details)
    if not gate.ok:
        return _refuse(
            [f"the target anim table is not trustworthy, so nothing may be "
             f"appended to it (Requirement 5.3): {gate.reason}"],
            details, memo)
    region = gate.walk.region
    old_count = region.count

    if not all_local:
        # Nothing to port and nothing to rewrite. Reported as a success with a
        # byte-identical buffer rather than as a refusal: an enemy whose every
        # anim ref is GLOBAL is already correct in any world.
        details.append(
            f"{label}: the blob carries no LOCAL CATEGORY-2 (anim) reference, "
            f"so no record is appended and no byte changes")
        return AnimPortOutcome(
            ok=True, details=details, prs=bytes(target_data), blob=bytes(blob),
            memo=dict(memo), old_count=old_count, new_count=old_count,
            trailer_offset=region.records_end)

    wanted = sorted({r.index for r in all_local})
    details.append(
        f"{label}: {len(all_local)} LOCAL CATEGORY-2 ref(s) naming "
        f"{len(wanted)} distinct source anim index(es) {wanted}")

    # ---- 2. + 3. per-source-record assessment, rig classification ---------
    if isinstance(source, AnimSourceBundle):
        bundle = source
        if source_world and bundle.source_world != str(source_world).upper():
            return _refuse(
                [f"the source bundle carries {bundle.source_world} records but "
                 f"the port names {str(source_world).upper()} as its source "
                 f"world"], details, memo)
    else:
        if not source_world:
            return _refuse(
                ["porting from a raw source .PRS buffer requires "
                 "source_world, which names the memo namespace the records "
                 "dedupe in"], details, memo)
        bundle = collect_source_records(source, wanted,
                                       source_world=source_world)
    details.extend(bundle.details)
    if not bundle.ok:
        return _refuse(
            [f"a source anim record is not portable, so the whole enemy is "
             f"refused: {bundle.reason}"], details, memo)

    missing_records = [index for index in wanted if index not in bundle.records]
    if missing_records:
        return _refuse(
            [f"the source bundle carries no record for anim index(es) "
             f"{missing_records}, which this enemy's LOCAL CATEGORY-2 refs "
             f"name, so the port cannot be completed"], details, memo)

    # Bone-name compatibility against the rig the records will actually drive.
    # `can_bind_to_rig` refuses UNDETERMINED (Requirement 7.5) and any source
    # bone with no target counterpart (Requirement 7.6). Never remaps, never
    # binds partially.
    for index in wanted:
        record = bundle.records[index]
        ok, reasons = can_bind_to_rig(record.data, target_names)
        if not ok:
            return _refuse(
                [f"{bundle.source_world} anim[{index}] cannot bind to the "
                 f"target rig, so the port is refused: {'; '.join(reasons)}"],
                details, memo)
        missing, tolerated, note = unmatched_name_verdict(record.data,
                                                          target_names)
        if tolerated:
            # LOGGED EXPLICITLY, never silently: the port proceeds and the
            # engine drops these node(s) at attach time. Nothing is substituted.
            details.append(
                f"{bundle.source_world} anim[{index}] binds WITH "
                f"{len(missing)} UNMATCHED NODE(S) SKIPPED BY THE ENGINE: "
                f"{note}")
        else:
            details.append(
                f"{bundle.source_world} anim[{index}] binds: all "
                f"{len(record.bone_names)} bone name(s) exist in the "
                f"{len(target_names)}-bone target rig")

    # ---- 4. the DEDUPE memo ------------------------------------------------
    # Mirrors `cross_world.mesh_memo` exactly: a record already ported this
    # session -- by another ref of this enemy or by an earlier enemy -- is NOT
    # appended again; every naming ref points at the one existing index.
    staged_memo = dict(memo)
    new_indices: dict = {}
    to_append: list = []
    for index in wanted:
        key = (bundle.source_world, MATERIAL_CATEGORY_ANIM, index)
        if key in staged_memo:
            new_indices[(bundle.source_world, index)] = staged_memo[key]
            details.append(
                f"{bundle.source_world} anim[{index}] already ported this "
                f"session -> target anim[{staged_memo[key]}] (deduped, not "
                f"appended again)")
            continue
        to_append.append(index)

    # ---- 5. the append, on a STAGED buffer --------------------------------
    staged_prs = bytes(target_data)
    trailer_offset = region.records_end
    new_count = old_count
    if to_append:
        try:
            staged_prs, assigned, trailer_offset = append_anim_records(
                staged_prs, [bundle.records[i].data for i in to_append])
        except AnimParseError as exc:
            return _refuse(
                [f"appending {len(to_append)} anim record(s) to the target "
                 f"table failed, so nothing was appended: {exc}"],
                details, memo)
        if len(assigned) != len(to_append):
            return _refuse(
                [f"the append assigned {len(assigned)} index(es) for "
                 f"{len(to_append)} record(s)"], details, memo)
        for index, assigned_index in zip(to_append, assigned):
            new_indices[(bundle.source_world, index)] = assigned_index
            staged_memo[(bundle.source_world, MATERIAL_CATEGORY_ANIM, index)] = \
                assigned_index
        new_count = old_count + len(assigned)
        details.append(
            f"{label}: appended {len(assigned)} record(s) "
            f"({len(staged_prs) - len(target_data)} B), anim count "
            f"{old_count} -> {new_count}, "
            f"{', '.join(f'{i}->{n}' for i, n in zip(to_append, assigned))}, "
            f"trailer now at {trailer_offset}")

    # ---- 6. rewrite the LOCAL CATEGORY-2 refs, FLAG PRESERVED -------------
    staged_blob = bytes(blob)
    rewrites: dict = {}
    try:
        for ref in all_local:
            target_index = new_indices[(bundle.source_world, ref.index)]
            staged_blob = set_material_ref_index(staged_blob, ref, target_index)
            rewrites[ref.idx_offset] = (ref.flag, target_index)
    except (ValueError, KeyError) as exc:
        return _refuse(
            [f"rewriting a LOCAL CATEGORY-2 reference failed, so nothing is "
             f"committed: {type(exc).__name__}: {exc}"], details, memo)

    # ---- 7. bounds-check every rewritten index against the NEW count ------
    # Against the count in the buffer that was just built, never against a
    # cached one: a cached count is exactly the stale bound this feature's
    # design calls out (task 9.5).
    bad = sorted({index for _off, (_flag, index) in rewrites.items()
                  if not index < new_count})
    if bad:
        return _refuse(
            [f"rewritten anim index(es) {bad} are not < the NEW target anim "
             f"count {new_count}, so the port is refused (Requirement 8.6)"],
            details, memo)

    # Prove the rewrite on the staged blob: re-parse and compare ref for ref.
    # A moved flag byte would have changed the category or the global bit, which
    # repoints the reference at a different resource table entirely.
    try:
        after = parse_material_refs(staged_blob)
    except MaterialSectionError as exc:
        return _refuse(
            [f"the rewritten blob's material section no longer walks: {exc}"],
            details, memo)
    if len(after) != len(refs):
        return _refuse(
            [f"the rewritten blob parses {len(after)} material reference(s), "
             f"the source had {len(refs)}"], details, memo)
    expected_by_offset = {ref.idx_offset: rewrites[ref.idx_offset][1]
                          for ref in all_local}
    for before, now in zip(refs, after):
        if (now.flag, now.idx_offset, now.rec_type) != (
                before.flag, before.idx_offset, before.rec_type):
            return _refuse(
                [f"the anim rewrite changed more than an index: "
                 f"{before.describe()} became {now.describe()}"],
                details, memo)
        expected = expected_by_offset.get(before.idx_offset, before.index)
        if now.index != expected:
            return _refuse(
                [f"ref at 0x{before.idx_offset:X} should now read index "
                 f"{expected} but reads {now.index}"], details, memo)
    # BYTE-LEVEL PROOF for the extra refs. The parser cannot see them, so they
    # are absent from `after` and the ref-for-ref comparison above skips them.
    # Read each one back out of the staged blob and require BOTH the flag byte
    # and the index to be exactly what was written -- the same guarantee the
    # parsed refs get, obtained directly.
    for ref in extra_local:
        if ref.idx_offset + 4 > len(staged_blob):
            return _refuse(
                [f"extra anim ref at 0x{ref.idx_offset:X} lies outside the "
                 f"rewritten blob"], details, memo)
        raw_after = struct.unpack_from("<I", staged_blob, ref.idx_offset)[0]
        flag_after = (raw_after >> 24) & 0xFF
        index_after = raw_after & MATERIAL_INDEX_MASK
        expect = rewrites[ref.idx_offset][1]
        if flag_after != ref.flag:
            return _refuse(
                [f"extra anim ref at 0x{ref.idx_offset:X} changed its flag "
                 f"byte (0x{ref.flag:02X} -> 0x{flag_after:02X}), which would "
                 f"repoint it at a different resource table"], details, memo)
        if index_after != expect:
            return _refuse(
                [f"extra anim ref at 0x{ref.idx_offset:X} should now read "
                 f"index {expect} but reads {index_after}"], details, memo)
    if extra_local:
        details.append(
            f"{label}: {len(extra_local)} extra ref(s) verified byte-for-byte "
            f"at "
            + ", ".join(f"0x{r.idx_offset:X}->{rewrites[r.idx_offset][1]}"
                        for r in extra_local))

    details.append(
        f"{label}: {len(rewrites)} LOCAL CATEGORY-2 ref(s) rewritten, "
        f"{untouched} other ref(s) (GLOBAL and non-anim) untouched, "
        f"every flag byte re-read from the blob and unchanged")

    # ---- 8. the post-append re-walk --------------------------------------
    if to_append:
        post = assess_anim_table(staged_prs, label=f"{label} post-append")
        details.extend(post.details)
        if not post.ok:
            return _refuse(
                [f"the post-append re-walk of the target anim table does not "
                 f"pass, so nothing is committed (Requirement 8.9): "
                 f"{post.reason}"], details, memo)
        walk = post.walk
        if not walk.clean:
            return _refuse(
                [f"the post-append anim walk is not clean "
                 f"({len(walk.bounds)} of {walk.count} record(s) bounded, "
                 f"{len(walk.events)} event(s))"], details, memo)
        if walk.trailing != 0:
            return _refuse(
                [f"the post-append anim walk leaves {walk.trailing} trailing "
                 f"byte(s) before the archive trailer, so the appended records "
                 f"do not tile the region"], details, memo)
        if walk.count != new_count:
            return _refuse(
                [f"the post-append table declares {walk.count} record(s), "
                 f"expected {new_count}"], details, memo)

    # ---- 9. hand the staged buffers back for the caller to adopt ---------
    return AnimPortOutcome(
        ok=True, details=details, prs=staged_prs, blob=staged_blob,
        new_indices=new_indices, appended=tuple(to_append),
        rewrites=rewrites, memo=staged_memo, old_count=old_count,
        new_count=new_count, trailer_offset=trailer_offset)
