"""`.BD` sound-bank parse / rebuild / MERGE, and the SPU budget that caps it.

WHY THIS EXISTS
    A cross-world ported enemy arrives with its mesh, its textures and its
    script, but its SAMPLES stay in its home world's bank. `GetSpotFXID` is a
    LINEAR SEARCH over the bank's explicit EffectID field, so an id the loaded
    bank does not carry simply is not found and the enemy is SILENT. Nothing
    logs it and nothing fails; the user hears nothing and there is no evidence
    to look at.

    The fix ships GRAVE's bank with the foreign samples APPENDED. The filename
    stays `GRAVE.BD`, so bank 208 loads it through the driver's existing static
    path table -- no EE patch, no IRX patch, no new bank id.

THE FORMAT (`ProcessSoundDataHeader` @VA 0x4460 in MJOLNIR.IRX; measured, and
round-trip byte-identical on all five retail banks)

    u32  nSFX                            ; the driver enforces nSFX < 300
    nSFX x { u32 w0; u32 w1; u32 w2; }   ; 12 bytes per entry
    u8   samples[]                       ; contiguous; the header is NOT sent
                                         ; to the SPU

    w0 >> 16          EffectID, a GLOBAL 16-bit id
    w0 & 0xFF         volume percent
    (w0 >> 8) & 0xFF  flag
    w1                byte offset INSIDE THE SAMPLE BLOCK (block-relative)
    w2 >> 16          pitch
    w2 & 0xFFFF       dead at runtime -- PRESERVED VERBATIM, never recomputed

    Ids are searched linearly on the explicit field, so arbitrary ids in any
    order are legal. That is what makes appending foreign ids sound at all.

THE BUDGET, AND WHY IT IS A HARD REFUSAL
    The driver has NO ceiling check. A bank that overruns usable SPU RAM
    corrupts it SILENTLY -- exactly the failure mode the RESOURCE pool budget
    exists to prevent, and it is treated the same way here: a candidate that
    does not fit is REFUSED AS DATA, reported, and left silent. Nothing is ever
    truncated and no overflow is ever written.

    WHAT MUST FIT IS THE SAMPLE BLOCK, NOT THE FILE. `ProcessSoundDataHeader`
    parses the header ON THE IOP and advances the read pointer past it; only
    `samples[]` is ever transferred to SPU RAM (MJOLNIR.IRX §4/§5.3, and
    research/STREAM_MEMORY_END_TRACE.md §5). So every occupancy figure here is
    `roundup64(len(bank.samples))`. Charging `roundup64(filesize)` over-charged
    every bank by its 3,328-3,592 B header, which is what used to make UNDER and
    CASTLE look illegal.

    THE BANK DOES NOT START AT SPU ADDRESS ZERO. It starts at `SPU_BASE` =
    0x0001DA10 = 121,360 B, a LITERAL sent by RPC command 14
    (`SOUND_SetStreamMemoryEnd`) before EVERY bank load. `SPU_USABLE_CEILING` is
    therefore `SPU_TOTAL - SPU_BASE` = 1,975,792 B. Reverb is CONFIRMED never
    enabled, so the old "reverb reserves the top 128 KB" assumption is REFUTED
    -- but read the constants below before expecting a win: the base eats
    121,360 B, so the ceiling moves by only +9,712 B.

    STILL UNKNOWN, and it is why the refusal stays a refusal: "no reverb write"
    is not proof the top of SPU RAM is otherwise unused. See the note on
    `SPU_USABLE_CEILING`.

PURITY
    Everything here takes and returns bytes and dataclasses. No file is opened,
    no path is touched, nothing is logged. The caller owns I/O and logging, so
    the merge decision can be tested without a build.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

#: `ProcessSoundDataHeader` rejects a bank whose count is not below this.
#:
#: THIS, NOT THE BYTE CEILING, IS WHAT BLOCKS CASTLE. CASTLE.BD carries nSFX 299
#: against this hard 300, so it has ZERO free entry slots and no byte budget can
#: buy it one -- reclamation frees sample BYTES, never header ROWS. Raising
#: `SPU_USABLE_CEILING` legalises CASTLE's own shipped bank (see below) and
#: still cannot admit a single import into it.
MAX_SFX = 300

#: SPU2 RAM, structural (hardware): 0x200000.
SPU_TOTAL = 2_097_152

#: WHERE A BANK'S SAMPLE BLOCK STARTS IN SPU RAM. 0x0001DA10 = 121,360 B.
#:
#: MEASURED, and a LITERAL, not a computation: `SOUND_SetStreamMemoryEnd` (RPC
#: command 14) has exactly ONE caller in the whole image and it passes an
#: immediate pair -- `SLUS_200.17` VA 0x00194C5C/0x00194C64 (`lui $v0,0x0001` /
#: `ori $a0,$v0,0xda10`), and independently in `SLPM_621.27` at VA
#: 0x00194F5C/0x00194F64. Command 14 is re-sent UNCONDITIONALLY before every
#: bank load (command 6) from the per-level path at 0x00147510, always with that
#: same literal, so the base is per-level and CONSTANT -- it does not drift with
#: the IRX's post-load cursor advance, which is overwritten before it can
#: matter. See research/STREAM_MEMORY_END_TRACE.md §2-§3.
#:
#: Consequence: sample memory does NOT start at zero, and 121,360 B of the 2 MB
#: is gone before the first sample. What that region is FOR (music double-buffer
#: plus the SPU's reserved low area) is INFERRED and does not matter here.
SPU_BASE = 121_360

#: Usable SPU RAM for a bank's SAMPLE BLOCK: `SPU_TOTAL - SPU_BASE` =
#: 1,975,792 B.
#:
#: The old value 1,966,080 assumed reverb reserved the top 128 KB of the core at
#: 0x1DFFFF. THAT ASSUMPTION IS REFUTED: IRX commands 19-27 (the whole
#: `SOUND_*Effects` block) are sent by NEITHER retail build -- the EE wrappers
#: were never written, and the packet appender's address appears as a data word
#: zero times, so no `jalr` can reach it either; and reverb-on is separately
#: falsified by the shipped data, since it would leave 1,844,720 B while GRAVE's
#: sample block alone is 1,890,448 B and retail GRAVE works
#: (research/STREAM_MEMORY_END_TRACE.md §4).
#:
#: THE GAIN IS SMALL. Dropping the reverb reservation would have been +128 KB;
#: paying the measured 121,360 B base takes almost all of it back. The ceiling
#: moves 1,966,080 -> 1,975,792, i.e. **+9,712 B** -- less than the cheapest
#: non-free enemy sound outside grave. What the trace actually buys is that
#: UNDER's and CASTLE's own shipped banks stop being refused for a reason that
#: was never true; it does not buy meaningful import room anywhere.
#:
#: STILL UNKNOWN: absence of a reverb write is NOT proof that the top of SPU RAM
#: is unused by anything else. CASTLE's shipped bank ending 2,608 B below
#: 2 MB is strong circumstantial evidence that it is free, and nothing more.
#: Settling it needs an emulator memory watch on SPU2 RAM 0x1F0000-0x200000
#: across a CASTLE level load, checking nothing but the bank transfer writes
#: there (research/STREAM_MEMORY_END_TRACE.md §7.1). Until then this stays a
#: bound we refuse against rather than spend to: the driver has NO ceiling check
#: of ANY kind, so an overrun corrupts SPU RAM SILENTLY.
SPU_USABLE_CEILING = SPU_TOTAL - SPU_BASE          # 1,975,792

#: GRAVE's EXACT free sample-block space: `SPU_USABLE_CEILING -
#: roundup64(1,890,448)` = 1,975,792 - 1,890,496 = 85,296 B.
#:
#: The name is kept because callers pass it, but it no longer means what it did.
#: There is NO "measured band 68,672-72,256 B" any more: the band's WIDTH was
#: precisely the unknown SPU base, and the base is now a measured literal
#: (`SPU_BASE`), so this is one EXACT number rather than the pessimistic end of
#: a range. Nothing here is a guess and nothing here is conservative.
#:
#: It is a REPORTING figure, not the ceiling: the ceiling is absolute
#: (`ceiling_for`), because where usable SPU RAM ends does not depend on how big
#: the bank is.
HEADROOM_CONSERVATIVE = SPU_USABLE_CEILING - 1_890_496      # 85,296

#: The alignment the SPU upload is rounded to.
SPU_ALIGN = 64

_ENTRY = struct.Struct("<III")


def roundup64(size: int) -> int:
    """`size` rounded up to the SPU upload alignment."""
    size = int(size)
    return (size + SPU_ALIGN - 1) // SPU_ALIGN * SPU_ALIGN


class SoundBankError(ValueError):
    """A `.BD` that does not parse. Raised by `parse` only."""


@dataclass(frozen=True)
class SfxEntry:
    """One 12-byte header row, with `w0`/`w2` kept WHOLE.

    `w0` and `w2` are stored as the raw words rather than as decoded fields so
    a rebuild is byte-exact even where a bit's meaning is unknown -- notably
    `w2 & 0xFFFF`, which is dead at runtime and is still preserved.
    """

    w0: int
    w1: int
    w2: int

    @property
    def effect_id(self) -> int:
        return (self.w0 >> 16) & 0xFFFF

    @property
    def volume(self) -> int:
        return self.w0 & 0xFF

    @property
    def flag(self) -> int:
        return (self.w0 >> 8) & 0xFF

    @property
    def pitch(self) -> int:
        return (self.w2 >> 16) & 0xFFFF

    def with_offset(self, offset: int) -> "SfxEntry":
        """The same row pointing at a new block-relative offset.

        The ONLY field a merge is allowed to change. Volume, flag and pitch
        travel with the sample from its home bank.
        """
        return SfxEntry(self.w0, int(offset), self.w2)


@dataclass(frozen=True)
class SoundBank:
    """A parsed bank: the header rows plus the raw sample block."""

    entries: tuple
    samples: bytes

    @property
    def count(self) -> int:
        return len(self.entries)

    @property
    def header_size(self) -> int:
        return 4 + 12 * len(self.entries)

    @property
    def total_size(self) -> int:
        return self.header_size + len(self.samples)

    @property
    def effect_ids(self) -> tuple:
        return tuple(e.effect_id for e in self.entries)

    def index_of(self, effect_id: int) -> Optional[int]:
        for i, entry in enumerate(self.entries):
            if entry.effect_id == int(effect_id):
                return i
        return None

    def sample_span(self, index: int) -> tuple:
        """`(start, end)` of one entry's sample bytes inside the block.

        The end is the next offset IN OFFSET ORDER, not in entry order, so a
        bank whose rows are not sorted by offset still yields correct spans.
        Retail banks happen to tile in entry order; nothing here relies on it.
        """
        start = self.entries[index].w1
        ends = [e.w1 for e in self.entries if e.w1 > start]
        return start, (min(ends) if ends else len(self.samples))

    def sample_bytes(self, index: int) -> bytes:
        start, end = self.sample_span(index)
        return self.samples[start:end]

    def sample_size(self, effect_id: int) -> Optional[int]:
        index = self.index_of(effect_id)
        if index is None:
            return None
        start, end = self.sample_span(index)
        return end - start


def parse(data: bytes) -> SoundBank:
    """Parse a `.BD`. Round-trip byte-exact through `build` on retail banks.

    Raises `SoundBankError` for a truncated file, an `nSFX` the driver would
    itself reject, or an offset outside the sample block -- an out-of-range
    offset means the layout was misread, and merging on a misread layout is how
    a bank gets silently corrupted.
    """
    data = bytes(data)
    if len(data) < 4:
        raise SoundBankError(f"too short to hold nSFX ({len(data)} B)")
    (count,) = struct.unpack_from("<I", data, 0)
    if count >= MAX_SFX:
        raise SoundBankError(
            f"nSFX {count} is not below the driver's limit {MAX_SFX}")
    need = 4 + 12 * count
    if len(data) < need:
        raise SoundBankError(
            f"nSFX {count} needs a {need} B header, file is {len(data)} B")
    entries = tuple(SfxEntry(*_ENTRY.unpack_from(data, 4 + 12 * i))
                    for i in range(count))
    samples = data[need:]
    for i, entry in enumerate(entries):
        if entry.w1 > len(samples):
            raise SoundBankError(
                f"entry {i} (id {entry.effect_id}) offset {entry.w1} is past "
                f"the {len(samples)} B sample block")
    return SoundBank(entries=entries, samples=samples)


def build(entries: Sequence[SfxEntry], samples: bytes) -> bytes:
    """Serialise. `build(*parse(x))` is `x` for every retail bank."""
    out = bytearray(struct.pack("<I", len(entries)))
    for entry in entries:
        out += _ENTRY.pack(entry.w0 & 0xFFFFFFFF, entry.w1 & 0xFFFFFFFF,
                           entry.w2 & 0xFFFFFFFF)
    out += bytes(samples)
    return bytes(out)


# ---------------------------------------------------------------------------
# The merge
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SampleImport:
    """One sample to bring across, with its source row's fields VERBATIM.

    `w0` and `w2` are copied whole from the source bank so volume, flag and
    pitch are the ones the sound was authored with. They are NOT normalised: a
    normalised volume is a different sound, and the point of the merge is that
    the enemy sounds like itself.
    """

    effect_id: int
    w0: int
    w2: int
    sample: bytes
    source: str = ""

    @property
    def size(self) -> int:
        return len(self.sample)


@dataclass(frozen=True)
class ImportUnit:
    """The ATOMIC admission unit: everything one enemy needs to sound right.

    An enemy is admitted whole or not at all. A partial import gives an enemy
    some of its sounds and not others, which is a worse artifact than silence
    and is invisible in a log that only counts bytes.
    """

    label: str
    imports: tuple

    @property
    def effect_ids(self) -> tuple:
        return tuple(i.effect_id for i in self.imports)


@dataclass(frozen=True)
class Refusal:
    """A refusal, as DATA. The caller logs it; nothing here raises."""

    label: str
    effect_ids: tuple
    reason: str

    def log_line(self, prefix: str = "[cross-world]") -> str:
        ids = " ".join(str(i) for i in self.effect_ids) or "-"
        return f"{prefix}   SOUND REFUSE {self.label:<22} ids={ids}  {self.reason}"


@dataclass(frozen=True)
class Admission:
    """An admitted unit and what it actually cost after dedupe."""

    label: str
    imported_ids: tuple
    deduped_ids: tuple
    added_bytes: int

    def log_line(self, prefix: str = "[cross-world]", *, used: int = 0,
                 remaining: int = 0) -> str:
        free = (" free(already present)="
                + " ".join(str(i) for i in self.deduped_ids)
                if self.deduped_ids else "")
        return (f"{prefix}   SOUND COMMIT {self.label:<22} "
                f"ids={' '.join(str(i) for i in self.imported_ids) or '-'}"
                f"{free} +{self.added_bytes:,} B "
                f"(used {used:,} B, {remaining:,} B left)")


@dataclass
class MergeResult:
    """The merged bank plus every decision that produced it."""

    data: bytes
    bank: SoundBank
    admitted: tuple = ()
    refusals: tuple = ()
    ceiling: int = 0
    original_size: int = 0
    #: The target's SAMPLE-BLOCK size before the merge. Carried explicitly
    #: because `original_size` is a FILE size and occupancy is not: the header
    #: never reaches the SPU, so the starting occupancy cannot be derived from
    #: the file size without re-parsing.
    original_samples: int = 0
    #: Every decision in the order it was MADE, as `(Admission | Refusal, used)`.
    #: Kept because the log is the artifact a user reads to check the walk
    #: honoured the seed's order; grouping commits and refusals apart hides
    #: exactly that.
    decisions: tuple = ()
    #: The `Reclamation` that freed room before this merge, or None. Carried on
    #: the result so a caller can report what was removed alongside what was
    #: added; the two only make sense read together.
    reclamation: object = None

    @property
    def added_bytes(self) -> int:
        return len(self.data) - self.original_size

    @property
    def occupancy(self) -> int:
        """SPU bytes the merged bank occupies: the SAMPLE BLOCK, rounded.

        NOT `roundup64(len(self.data))`. The 4 + 12*nSFX header is parsed on the
        IOP and never uploaded, so counting it charged the bank for bytes that
        never enter SPU RAM (research/STREAM_MEMORY_END_TRACE.md §5).
        """
        return roundup64(len(self.bank.samples))

    @property
    def remaining(self) -> int:
        return self.ceiling - self.occupancy

    @property
    def imported_ids(self) -> tuple:
        return tuple(i for a in self.admitted for i in a.imported_ids)

    @property
    def changed(self) -> bool:
        return bool(self.imported_ids)

    def log_lines(self, prefix: str = "[cross-world]") -> list:
        lines = [
            f"{prefix} sound bank merge: ceiling {self.ceiling:,} B "
            f"(usable SPU {SPU_USABLE_CEILING:,} B = 2 MB - the measured "
            f"{SPU_BASE:,} B base), start "
            f"{roundup64(self.original_samples):,} B occupancy "
            f"(sample block only; the header is never uploaded)"]
        for decision, used in self.decisions:
            if isinstance(decision, Refusal):
                lines.append(decision.log_line(prefix))
            else:
                lines.append(decision.log_line(prefix, used=used,
                                               remaining=self.ceiling - used))
        lines.append(
            f"{prefix} sound bank: {self.bank.count} entries "
            f"(limit {MAX_SFX}), {len(self.data):,} B file, "
            f"{len(self.bank.samples):,} B samples, "
            f"{self.occupancy:,} B occupancy = "
            f"{100.0 * self.occupancy / SPU_USABLE_CEILING:.1f}% of usable SPU, "
            f"{self.remaining:,} B left")
        return lines


def ceiling_for(target_bytes: bytes = b"", *,
                headroom: int = HEADROOM_CONSERVATIVE) -> int:
    """The ABSOLUTE sample-block ceiling: `SPU_USABLE_CEILING`, always.

    This used to be `roundup64(len(target_bytes)) + headroom`, which was wrong
    twice over: it was FILE-SIZE based, and it was RELATIVE to the bank, so it
    shrank when the bank shrank -- which made reclamation a no-op, the trap
    `sound_port` documents at its `ceiling = sb.ceiling_for(...)` line. The
    ceiling is where usable SPU RAM ENDS relative to the MEASURED `SPU_BASE`; it
    does not move when the bank does.

    `target_bytes` and `headroom` are accepted ONLY so existing call sites keep
    working, and both are IGNORED. There is no bank-relative term left to apply
    and `HEADROOM_CONSERVATIVE` is now a reporting figure (GRAVE's exact free
    space), not an input -- feeding it back in would re-create the relative
    ceiling this replaced.
    """
    del target_bytes, headroom
    return SPU_USABLE_CEILING


def imports_from(bank: SoundBank, effect_ids: Iterable[int], *,
                 source: str = "") -> tuple:
    """`(imports, missing_ids)` for `effect_ids` taken out of `bank`.

    An id the source bank does not carry is REPORTED, never invented: it means
    the derivation named an id no bank owns, and the caller has to say so.
    """
    imports: list = []
    missing: list = []
    for effect_id in dict.fromkeys(int(i) for i in effect_ids):
        index = bank.index_of(effect_id)
        if index is None:
            missing.append(effect_id)
            continue
        entry = bank.entries[index]
        imports.append(SampleImport(
            effect_id=effect_id, w0=entry.w0, w2=entry.w2,
            sample=bank.sample_bytes(index), source=source))
    return tuple(imports), tuple(missing)


def merge(target_bytes: bytes, units: Sequence, *,
          headroom: int = HEADROOM_CONSERVATIVE,
          ceiling: Optional[int] = None,
          max_sfx: int = MAX_SFX) -> MergeResult:
    """Append as many whole `ImportUnit`s as fit, IN THE ORDER GIVEN.

    The order is the caller's -- for the build that is the seed's draw order, so
    the outcome is reproducible from the seed exactly the way the RESOURCE pool
    admission is.

    Admission is INCREMENTAL and reads the LIVE size before every decision, so
    a unit that turned out bigger than expected is absorbed by the next
    decision rather than overshooting. A unit is admitted WHOLE; if any part of
    it does not fit, the whole unit is refused and the walk CONTINUES, because a
    cheaper later enemy can still use the remaining room.

    An id already present in the target is DEDUPED: skipped, reported as free,
    and never re-imported. Refusals come back as data; this never raises.

    A `SampleImport` may also be passed in place of an `ImportUnit`, and is
    treated as a one-sample unit.
    """
    bank = parse(target_bytes)
    limit = int(ceiling if ceiling is not None
                else ceiling_for(target_bytes, headroom=headroom))

    entries = list(bank.entries)
    samples = bytearray(bank.samples)
    present = {e.effect_id for e in entries}
    admitted: list = []
    refusals: list = []
    decisions: list = []

    def live() -> int:
        # SAMPLE BLOCK ONLY. The 4 + 12*nSFX header is parsed on the IOP and is
        # never transferred to SPU RAM, so it must not be charged against the
        # ceiling (research/STREAM_MEMORY_END_TRACE.md §5).
        return roundup64(len(samples))

    if live() > limit:
        # Nothing was added and nothing can be: report it rather than pretend
        # a merge happened.
        refusals.append(Refusal(
            "(target)", (),
            f"target bank's sample block already occupies {live():,} B, over "
            f"the {limit:,} B ceiling; no import is possible"))
        return MergeResult(data=bytes(target_bytes), bank=bank, admitted=(),
                           refusals=tuple(refusals), ceiling=limit,
                           original_size=len(target_bytes),
                           original_samples=len(bank.samples))

    for unit in units:
        if isinstance(unit, SampleImport):
            unit = ImportUnit(label=f"id {unit.effect_id}", imports=(unit,))
        fresh = [i for i in unit.imports if i.effect_id not in present]
        free = tuple(i.effect_id for i in unit.imports
                     if i.effect_id in present)

        if not fresh:
            admitted.append(Admission(unit.label, (), free, 0))
            decisions.append((admitted[-1], live()))
            continue

        if len(entries) + len(fresh) >= max_sfx:
            refusals.append(Refusal(
                unit.label, tuple(i.effect_id for i in fresh),
                f"nSFX would reach {len(entries) + len(fresh)}, and the driver "
                f"requires it below {max_sfx}; refused whole (walk continues)"))
            decisions.append((refusals[-1], live()))
            continue

        added = sum(i.size for i in fresh)
        projected = roundup64(len(samples) + added)
        if projected > limit:
            refusals.append(Refusal(
                unit.label, tuple(i.effect_id for i in fresh),
                f"does not fit: live {live():,} B + {added:,} B of samples "
                f"would occupy {projected:,} B > ceiling {limit:,} B; refused "
                f"whole so the enemy is not left half-audible "
                f"(walk continues -- a cheaper enemy may still fit)"))
            decisions.append((refusals[-1], live()))
            continue

        for imp in fresh:
            entries.append(SfxEntry(imp.w0, len(samples), imp.w2))
            samples += imp.sample
            present.add(imp.effect_id)
        admitted.append(Admission(
            unit.label, tuple(i.effect_id for i in fresh), free, added))
        decisions.append((admitted[-1], live()))

    data = build(entries, bytes(samples))
    return MergeResult(data=data, bank=parse(data), admitted=tuple(admitted),
                       refusals=tuple(refusals), ceiling=limit,
                       original_size=len(target_bytes),
                       original_samples=len(bank.samples),
                       decisions=tuple(decisions))


# ---------------------------------------------------------------------------
# Reclamation
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Dropped:
    """One entry removed from a bank, with WHY, so the log can be audited."""

    effect_id: int
    size: int
    owners: tuple = ()
    reason: str = ""

    def log_line(self, prefix: str = "[cross-world]") -> str:
        owners = (" owner(s)=" + " ".join(f"0x{o:02X}" for o in self.owners)
                  if self.owners else "")
        return (f"{prefix}   SOUND DROP   id {self.effect_id:<5}"
                f" -{self.size:,} B{owners}  {self.reason}")


@dataclass(frozen=True)
class Reclamation:
    """A bank with entries REMOVED and every `w1` recomputed."""

    data: bytes
    bank: SoundBank
    dropped: tuple = ()
    original_size: int = 0

    @property
    def freed_bytes(self) -> int:
        return self.original_size - len(self.data)

    @property
    def dropped_ids(self) -> tuple:
        return tuple(d.effect_id for d in self.dropped)

    def log_lines(self, prefix: str = "[cross-world]") -> list:
        if not self.dropped:
            return [f"{prefix} sound reclamation: nothing was safe to drop; "
                    f"the bank is untouched"]
        lines = [d.log_line(prefix) for d in self.dropped]
        lines.append(
            f"{prefix} sound reclamation: {len(self.dropped)} entr"
            f"{'y' if len(self.dropped) == 1 else 'ies'} removed, "
            f"{self.freed_bytes:,} B freed "
            f"({self.original_size:,} B -> {len(self.data):,} B, nSFX "
            f"{self.bank.count})")
        return lines


def reclaim(target_bytes: bytes, drops: Iterable) -> Reclamation:
    """Remove entries by EffectID, rebuilding the block and every `w1`.

    `drops` is an iterable of `Dropped` (or bare ids). EVERY entry carrying a
    dropped id goes, which matters because retail banks carry two ids twice.

    THE SAMPLE BYTES OF EVERY KEPT ENTRY ARE UNCHANGED. Only the offsets move,
    and they move because the block is re-tiled in OFFSET ORDER with the freed
    spans closed up -- so `validate` stays clean: no gap, no overlap, nothing
    unreferenced.

    A span shared by several entries (two rows at the same offset) is dropped
    only when EVERY entry pointing at it is dropped. Nothing here raises for a
    dropped id the bank does not carry; it simply drops nothing.
    """
    bank = parse(target_bytes)
    requested = []
    for drop in drops:
        requested.append(drop if isinstance(drop, Dropped)
                         else Dropped(effect_id=int(drop), size=0))
    doomed = {d.effect_id for d in requested}

    # Span table keyed by START, because rows may share one.
    ends: dict = {}
    for index, entry in enumerate(bank.entries):
        ends[entry.w1] = bank.sample_span(index)[1]
    kept_starts = {e.w1 for e in bank.entries if e.effect_id not in doomed}

    mapping: dict = {}
    samples = bytearray()
    for start in sorted(ends):
        if start not in kept_starts:
            continue
        mapping[start] = len(samples)
        samples += bank.samples[start:ends[start]]

    entries = tuple(e.with_offset(mapping[e.w1]) for e in bank.entries
                    if e.effect_id not in doomed)
    data = build(entries, bytes(samples))

    # Report the ACTUAL freed size per id, measured from the bank, rather than
    # whatever the caller guessed.
    dropped = tuple(
        Dropped(effect_id=d.effect_id,
                size=sum(ends[e.w1] - e.w1 for e in bank.entries
                         if e.effect_id == d.effect_id
                         and e.w1 not in kept_starts),
                owners=d.owners, reason=d.reason)
        for d in requested if bank.index_of(d.effect_id) is not None)
    return Reclamation(data=data, bank=parse(data), dropped=dropped,
                       original_size=len(bytes(target_bytes)))


# ---------------------------------------------------------------------------
# Verification helpers -- used by the tests and by the on-disc inspector
# ---------------------------------------------------------------------------

def duplicate_ids(bank: SoundBank) -> tuple:
    """EffectIDs the bank carries more than once. Retail banks carry 2."""
    seen: dict = {}
    for entry in bank.entries:
        seen[entry.effect_id] = seen.get(entry.effect_id, 0) + 1
    return tuple(sorted(i for i, n in seen.items() if n > 1))


def validate(bank: SoundBank) -> list:
    """Every structural problem a bank has, as strings. Empty list == valid.

    Checks the four things a corrupt merge would break: the driver's count
    limit, offsets inside the block, no OVERLAP, and no GAP (retail banks tile
    exactly, so a gap means bytes were lost).
    """
    problems: list = []
    if bank.count >= MAX_SFX:
        problems.append(f"nSFX {bank.count} is not below {MAX_SFX}")
    # DUPLICATE IDS ARE NOT A PROBLEM. Every retail bank carries two (1435 and
    # 1444), so the format permits them and `GetSpotFXID`'s linear search simply
    # finds the first. Flagging them would make the ground truth "invalid",
    # which would be the check being wrong rather than the data. They are
    # reported by `duplicate_ids` instead, and the merge's dedupe means a merge
    # never ADDS one.
    spans = sorted((e.w1, bank.sample_span(i)[1])
                   for i, e in enumerate(bank.entries))
    cursor = 0
    for start, end in spans:
        if start < cursor:
            problems.append(f"offset {start} overlaps the span ending {cursor}")
        elif start > cursor:
            problems.append(f"gap of {start - cursor} B before offset {start}")
        if end > len(bank.samples):
            problems.append(
                f"span {start}-{end} runs past the {len(bank.samples)} B block")
        cursor = max(cursor, end)
    if cursor != len(bank.samples):
        problems.append(
            f"{len(bank.samples) - cursor} B of the sample block is unreferenced")
    return problems


def describe(data: bytes, *, name: str = "bank") -> list:
    """Human-readable lines about one bank, for the on-disc inspector."""
    try:
        bank = parse(data)
    except SoundBankError as exc:
        return [f"{name}: DOES NOT PARSE -- {exc}"]
    problems = validate(bank)
    # Occupancy is the SAMPLE BLOCK, not the file: the header is parsed on the
    # IOP and never uploaded (research/STREAM_MEMORY_END_TRACE.md §5).
    occupancy = roundup64(len(bank.samples))
    lines = [
        f"{name}: {len(data):,} B  nSFX={bank.count} (limit {MAX_SFX})  "
        f"header={bank.header_size:,} B  samples={len(bank.samples):,} B",
        f"{name}: sample-block occupancy roundup64={occupancy:,} B of usable SPU "
        f"{SPU_USABLE_CEILING:,} B "
        f"({100.0 * occupancy / SPU_USABLE_CEILING:.1f}%), "
        f"{SPU_USABLE_CEILING - occupancy:,} B spare",
        f"{name}: round-trip {'BYTE-IDENTICAL' if build(bank.entries, bank.samples) == bytes(data) else 'DIFFERS -- rebuild is not exact'}",
    ]
    dupes = duplicate_ids(bank)
    if dupes:
        lines.append(f"{name}: duplicate EffectIDs {list(dupes)} "
                     f"(legal -- every retail bank carries 1435 and 1444)")
    lines.append(f"{name}: layout {'OK' if not problems else 'PROBLEMS'}")
    lines.extend(f"{name}:   {p}" for p in problems)
    return lines
