"""Which sound EffectIDs an entity actually plays, DERIVED from its script.

The ids are not hardcoded anywhere. An entity's `Script_Section` calls API slot
31 `CScriptApi::PlaySoundEffect` (arity 2, `(EffectID, volume)`), and this
module reads the LITERAL operands of those call sites -- the same way
`roster.derive_companions` reads slot 49 `Spawn` to build a companion closure.
A hardcoded id list would go stale the moment the roster changed and would be
indistinguishable from a guess when it did.

NOTHING IS REIMPLEMENTED. The decoder is `befscript`; the calling-convention
stack machine, the push-mnemonic table and the `ScriptValue` provenance model
are `companion_port`'s, imported and reused rather than copied, so there is
exactly one model of how this compiler passes arguments in the project.

WHICH ARGUMENT IS THE ID -- MEASURED, NOT ASSUMED
    Arguments come back in DRAIN order (first popped first, i.e. last pushed
    first). Reading the real call sites settles the role: every
    `PlaySoundEffect` site in `ICE.BEF` and `UNDER.BEF` has the shape

        PlaySoundEffect @618 args=[856 (0x358) @611; 128 (0x80) @609]

    -- drain position 0 is the EffectID and position 1 is the volume, which is
    the constant 128 at every site examined. So position 0 is the id, and the
    OTHER argument is deliberately ignored.

    That distinction is load-bearing rather than cosmetic. Treating "any literal
    the source bank carries" as an id looks safer and is not: `UNDER.BD`
    ACTUALLY CARRIES an EffectID 128, so the volume literal would have been
    imported as a sound. Bank membership is still applied to position 0 as a
    CHECK -- a literal the source bank does not carry is reported, never
    imported -- but it is a check, not the role oracle.

REFUSALS, NEVER GUESSES
    A blob that will not Clean_Decode and a call whose id comes from a register,
    member or local are both REPORTED. A register-sourced id is computed at
    runtime and cannot be resolved statically at all; reporting it leaves that
    one sound missing, whereas guessing would import the wrong sample.

    Nothing here raises. Every failure is a reason string, because downstream
    every one of them has to be a log line rather than a crashed build.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from . import befscript
from . import companion_port as _cp

#: `CScriptApi::PlaySoundEffect`, slot 31, arity 2, handler 0x00122B10.
PLAY_SOUND_API_INDEX = 31

#: Every target of this feature is retail; the revision is always explicit.
REVISION = befscript.REVISION_RETAIL

_REPORTS_ATTR = "_sound_id_reports"


@dataclass(frozen=True)
class SoundSite:
    """One `PlaySoundEffect` call site."""

    call_offset: int
    args: tuple

    @property
    def id_argument(self):
        """The argument carrying the EffectID: drain position 0. See the module
        docstring for the measurement that fixes the role."""
        return self.args[0] if self.args else None

    @property
    def literal_effect_id(self) -> Optional[int]:
        arg = self.id_argument
        return arg.value if (arg is not None and arg.is_literal) else None

    @property
    def has_nonliteral(self) -> bool:
        """True when the ID argument specifically is not a literal.

        The volume argument is not checked: it is not imported, so it being
        computed at runtime costs nothing and must not refuse the sound.
        """
        return self.literal_effect_id is None

    def describe(self) -> str:
        args = "; ".join(a.describe() for a in self.args) or "(no args traced)"
        return f"PlaySoundEffect @{self.call_offset} args=[{args}]"


@dataclass(frozen=True)
class SoundReport:
    """What one blob's `Script_Section` says about the sounds it plays."""

    type_id: int
    refused: bool = False
    reason: Optional[str] = None
    sites: tuple = ()

    @property
    def literal_values(self) -> tuple:
        """Distinct literal EffectIDs across every site, sorted."""
        return tuple(sorted({s.literal_effect_id for s in self.sites
                             if s.literal_effect_id is not None}))

    @property
    def nonliteral_sites(self) -> tuple:
        return tuple(s for s in self.sites if s.has_nonliteral)


def _trace(disassembly) -> list:
    """Recover `PlaySoundEffect` sites, reusing `companion_port`'s convention.

    The stack machine is `companion_port.trace_api_arguments`' model exactly --
    its push table, its drain mnemonics, its "anything else discards the stack"
    rule -- applied to a different slot. Reusing the tables rather than copying
    them means a correction to the convention cannot apply to `Spawn` and miss
    `PlaySoundEffect`.
    """
    sites: list = []
    stack: list = []
    block: list = []

    def drain(count: int) -> None:
        for _ in range(count):
            block.append(stack.pop() if stack
                         else _cp.ScriptValue(_cp.VALUE_UNKNOWN, None, None))

    for ins in disassembly.instructions:
        mnemonic = ins.mnemonic
        operands = {o.name: o.value for o in ins.operands}
        if mnemonic in _cp._PUSH_KINDS:
            kind, name = _cp._PUSH_KINDS[mnemonic]
            stack.append(_cp.ScriptValue(kind, operands.get(name), ins.offset))
            continue
        if mnemonic in _cp._DRAIN_ONE:
            drain(1)
            continue
        if mnemonic in _cp._DRAIN_MANY:
            drain(max(0, int(operands.get("count", 0))))
            continue
        if mnemonic in _cp._CALL_API:
            index = operands.get(befscript.API_INDEX_OPERAND_NAME)
            if index == PLAY_SOUND_API_INDEX:
                sites.append(SoundSite(call_offset=ins.offset,
                                       args=tuple(block)))
            block.clear()
            stack.clear()
            continue
        stack.clear()
    return sites


def sound_report(blob: bytes, type_id: int, revision: str = REVISION
                 ) -> SoundReport:
    """One blob's `PlaySoundEffect` sites. NEVER raises.

    A blob that does not Clean_Decode is a REFUSAL with a reason. That is not
    the same claim as "this entity plays no sounds", and the two are kept apart
    because one means "silent by design" and the other means "we could not
    tell".
    """
    refused, reason, dis = _cp.spawn_edges_for_blob(blob, revision)
    if refused:
        return SoundReport(type_id=type_id, refused=True, reason=reason)
    return SoundReport(type_id=type_id, sites=tuple(_trace(dis)))


def sound_reports(source_bef, revision: str = REVISION) -> dict:
    """One `SoundReport` per entry of a parsed `.BEF`, cached on the file.

    Building it disassembles every blob, so the cache is what keeps deriving
    ids for several enemies out of one world from paying that cost per enemy.
    It lives on the `BefFile`, so it is dropped exactly when the file is.
    """
    cached = getattr(source_bef, _REPORTS_ATTR, None)
    if cached is not None:
        return cached
    reports = {entry.type_id: sound_report(entry.data, entry.type_id, revision)
               for entry in source_bef.entries}
    try:
        setattr(source_bef, _REPORTS_ATTR, reports)
    except (AttributeError, TypeError):        # pragma: no cover - defensive
        pass
    return reports


@dataclass(frozen=True)
class DerivedSounds:
    """The ids one enemy (with its companions) plays, plus what went unread."""

    type_id: int
    source_world: str
    effect_ids: tuple = ()
    unknown_values: tuple = ()      # literals no source bank carries
    notes: tuple = ()               # reasons: refusals, non-literal sites

    @property
    def complete(self) -> bool:
        """True when every site's id was recovered. False means SOME sound is
        missing even if the enemy got most of them."""
        return not self.notes


def derive_sound_ids(type_id: int, source_world: str, source_bef, *,
                     bank_ids: Iterable[int],
                     companions: Iterable[int] = (),
                     revision: str = REVISION) -> DerivedSounds:
    """Every EffectID `type_id` AND ITS COMPANIONS play, from the source world.

    COMPANIONS ARE INCLUDED. A companion that makes noise needs its ids too:
    `0x1D` Snowman's group is what the player actually hears, and porting the
    root's sounds alone would leave the group half-silent for no saving.

    `bank_ids` is the source world's bank id set, used as a CHECK on the ids the
    script named: an id the bank does not carry lands in `unknown_values` and is
    never imported, because there is no sample to import. It is not the role
    oracle -- drain position 0 is (see the module docstring). Passing an empty
    set derives nothing, which is the fail-closed direction.

    NEVER RAISES. A blob that will not decode, an id no bank carries, and a
    register-sourced argument all come back in `notes` / `unknown_values`.
    """
    known = {int(i) for i in bank_ids}
    reports = sound_reports(source_bef, revision)

    ids: list = []
    unknown: list = []
    notes: list = []
    for member in [int(type_id)] + [int(c) for c in companions
                                    if int(c) != int(type_id)]:
        report = reports.get(member)
        if report is None:
            notes.append(f"0x{member:02X} has no blob in {source_world}.BEF, "
                         f"so its PlaySoundEffect sites cannot be read")
            continue
        if report.refused:
            notes.append(f"0x{member:02X} in {source_world}.BEF: Script_Section "
                         f"did not decode ({report.reason})")
            continue
        for site in report.nonliteral_sites:
            notes.append(
                f"0x{member:02X} in {source_world}.BEF: PlaySoundEffect at "
                f"offset {site.call_offset} takes "
                f"{site.id_argument.describe() if site.id_argument is not None else 'no traceable argument'}"
                f", so the EffectID is not derivable statically -- that sound "
                f"stays silent rather than being guessed")
        for value in report.literal_values:
            if value in known:
                if value not in ids:
                    ids.append(value)
            elif value not in unknown:
                unknown.append(value)
    return DerivedSounds(type_id=int(type_id), source_world=source_world,
                         effect_ids=tuple(sorted(ids)),
                         unknown_values=tuple(sorted(unknown)),
                         notes=tuple(notes))
