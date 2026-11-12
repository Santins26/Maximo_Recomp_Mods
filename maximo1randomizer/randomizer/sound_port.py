"""Give the COMMITTED cross-world arrivals their voices, inside the SPU budget.

This is the only module in the sound feature that touches a file. `sound_bank`
is the pure format-and-merge layer and `sound_ids` is the pure derivation layer;
this one reads the banks and the `.BEF`s, walks the seed's draw order, and hands
back the merged bank as bytes. The caller writes it.

THE THREE RULES THIS ENFORCES

  1. ONLY COMMITTED ENEMIES GET SOUND. A refused port left no enemy behind to
     be heard, so importing its samples would spend budget on silence. The input
     is therefore the admission gate's `admitted` list, not the draw order and
     not the allowlist.

  2. INCREMENTALLY ADMITTED, IN THE SEED'S DRAW ORDER. The walk is
     `roster_selection.admit`'s: admit while it still fits, CONTINUE past a
     non-fit. The ORDER is the seed's SPAWN DRAW ORDER -- the order
     `collect_plans` returns, which is draw position ascending -- AT THE USER'S
     EXPLICIT REQUEST. No cost sorting is applied. Which enemies are audible is
     therefore a plain consequence of the seed's own order; see the comment at
     the merge call for the measured price of that choice. The order cannot
     change which enemies spawn or any seeded stream, because this pass runs
     after all of them and only consumes their results, and it is reproducible
     from the seed because the committed set and its order both are.

  2b. RECLAMATION BEFORE ADMISSION. Sounds belonging to walking-melee types
     that this run's lineup does not contain can never be played, so their
     samples are removed first and the room goes to arrivals that can be heard.
     What is safe to drop is derived from the target world's own `.BEF`; see
     `reclaimable` for the four conditions and `Attribution` for why an
     UNATTRIBUTED id -- most of the bank -- is never one of them.

  3. AN ENEMY IS A UNIT. Its own ids and its companions' ids are admitted
     together or not at all. Half an enemy's sounds is worse than none: the
     enemy then has some sounds and not others, which sounds like a bug and
     leaves no evidence.

MEMORY
    One source world at a time. Its `.BD` and its `.BEF` are parsed, reduced to
    a few dozen KB of samples, and DROPPED before the next world is opened, so
    peak residency is the target bank plus one source world.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence

from . import roster as _roster
from . import sound_bank as sb
from . import sound_ids as si
from .bef import BefFile

LOG_PREFIX = "[cross-world]"

#: The DEFAULT target world, NOT the only one. `merge_for_lineup` takes
#: `target_world` and the caller passes the world it is actually building, so
#: every world gets its own merge (or its own logged refusal). Named rather than
#: implied: the merged file keeps its own filename so the world's own bank id
#: loads it through the driver's existing static path table, with no new bank id
#: and no patch, whichever world it is.
#:
#: THE SPU BUDGET, NOT THE CODE, IS WHAT MAKES GRAVE SPECIAL. MEASURED
#: `roundup64(SAMPLE BLOCK)` occupancies against the 1,975,792 B
#: `SPU_USABLE_CEILING` (= 2 MB - the measured 121,360 B `SPU_BASE`;
#: research/STREAM_MEMORY_END_TRACE.md §5):
#:
#:     grave  1,890,496 ->  85,296 B free
#:     swamp  1,948,736 ->  27,056 B free
#:     ice    1,959,104 ->  16,688 B free
#:     under  1,971,648 ->   4,144 B free
#:     castle 1,973,184 ->   2,608 B free
#:
#: ALL FIVE WORLDS NOW FIT LEGALLY. The earlier figures here (grave 72,256 /
#: swamp 13,888 / ice 3,584 / under -9,024 / castle -10,688, with under and
#: castle refused WHOLESALE) were wrong on both counts: they charged
#: `roundup64(FILE size)`, which over-charges each bank by its 3,328-3,592 B
#: header that never reaches the SPU, and they assumed reverb reserved the top
#: 128 KB, which is refuted.
#:
#: BE HONEST ABOUT WHAT THAT BUYS: it LEGALISES under's and castle's OWN SHIPPED
#: BANKS. It does not buy them meaningful import room -- 4,144 B and 2,608 B are
#: below the cheapest non-free enemy sound (10,240 B), so outside grave almost
#: every import is still refused on the per-unit ceiling check, and castle is
#: blocked outright on ENTRY COUNT (nSFX 299 vs the driver's < 300), which no
#: byte budget can fix. Castle's 2,608 B free is a 0.12% fit on 2 MB, and that
#: near-miss independently corroborates the 121,360 B base from a direction
#: entirely outside the disassembly.
#:
#: Those refusals are the CORRECT outcome and are logged; no constant was
#: relaxed to avoid them, and nothing became CONFIRMED.
DEFAULT_TARGET_WORLD = "grave"


@dataclass(frozen=True)
class SoundPlan:
    """One committed enemy's derived sound requirement, before admission."""

    type_id: int
    name: str
    source_world: str
    unit: sb.ImportUnit
    derived: si.DerivedSounds
    missing_ids: tuple = ()

    @property
    def bytes_needed(self) -> int:
        return sum(i.size for i in self.unit.imports)


def _bank_path(game_files_dir, world: str) -> Path:
    return Path(game_files_dir) / f"{world.upper()}.BD"


def collect_plans(admitted: Sequence, *, game_files_dir,
                  log: Optional[Callable] = None) -> tuple:
    """Derive each committed enemy's sound requirement, IN DRAW ORDER.

    `admitted` is the admission gate's committed candidates: anything with
    `type_id`, `source_world` and (optionally) `name`.

    Returns `(plans, problems)`. A source world with no `.BD`, an enemy whose
    script will not decode, and an id no source bank carries are all `problems`
    -- strings for the caller to log -- and never exceptions, because every one
    of them has to cost that enemy its voice rather than cost the build.
    """
    plans: list = []
    problems: list = []

    # Group by source world so each world is opened ONCE, while remembering the
    # draw position so the merge still walks the seed's order.
    order: list = []
    by_world: dict = {}
    for position, cand in enumerate(admitted):
        world = str(getattr(cand, "source_world", "") or "").lower()
        by_world.setdefault(world, []).append((position, cand))
        order.append(cand)

    for world, members in by_world.items():
        path = _bank_path(game_files_dir, world)
        bef_path = Path(game_files_dir) / f"{world.upper()}.BEF"
        if not path.exists() or not bef_path.exists():
            missing = path.name if not path.exists() else bef_path.name
            problems.append(
                f"{missing} is not present, so the {len(members)} committed "
                f"arrival(s) from {world.upper()} cannot be given sound: "
                + ", ".join(f"0x{c.type_id:02X}" for _p, c in members))
            continue
        try:
            bank = sb.parse(path.read_bytes())
        except sb.SoundBankError as exc:
            problems.append(f"{path.name} does not parse ({exc}); the "
                            f"{len(members)} arrival(s) from {world.upper()} "
                            f"stay silent")
            continue
        bef = BefFile.parse(bef_path)
        try:
            for position, cand in members:
                closure, closure_reason = _roster.derive_companions(
                    cand.type_id, world, bef)
                if closure_reason is not None:
                    # The port already committed, so the closure WAS derivable
                    # when it mattered. Reaching here means the two derivations
                    # disagree, which is worth saying rather than swallowing.
                    problems.append(
                        f"0x{cand.type_id:02X} companion closure not derivable "
                        f"for sound ({closure_reason}); importing the root's "
                        f"own sounds only")
                    closure = frozenset()
                derived = si.derive_sound_ids(
                    cand.type_id, world, bef, bank_ids=bank.effect_ids,
                    companions=closure)
                problems.extend(derived.notes)
                imports, missing = sb.imports_from(
                    bank, derived.effect_ids, source=world)
                if missing:
                    problems.append(
                        f"0x{cand.type_id:02X}: EffectID(s) "
                        f"{list(missing)} are named by the script but absent "
                        f"from {path.name}; not imported")
                if derived.unknown_values:
                    problems.append(
                        f"0x{cand.type_id:02X}: literal(s) "
                        f"{list(derived.unknown_values)} at PlaySoundEffect "
                        f"sites are not EffectIDs {path.name} carries; ignored")
                name = str(getattr(cand, "name", "") or f"0x{cand.type_id:02X}")
                plans.append((position, SoundPlan(
                    type_id=cand.type_id, name=name, source_world=world,
                    unit=sb.ImportUnit(
                        label=f"0x{cand.type_id:02X} {name}",
                        imports=tuple(imports)),
                    derived=derived, missing_ids=tuple(missing))))
        finally:
            del bef
            del bank

    plans.sort(key=lambda pair: pair[0])
    if log is not None:
        for line in problems:
            log(f"{LOG_PREFIX}   SOUND NOTE {line}")
    return tuple(p for _pos, p in plans), tuple(problems)


# ---------------------------------------------------------------------------
# Attribution and reclamation: whose sounds are in the TARGET bank, and which
# of them nothing in this run can ever play
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Attribution:
    """Which target-world entity types reference each EffectID the bank carries.

    Derived AT BUILD TIME from the target world's own `.BEF` via `sound_ids`,
    the same slot-31 read the import side uses. NOT a table: a hardcoded
    attribution would go stale the moment the roster or the bank changed, and a
    stale attribution here does not merely miss a saving -- it DELETES a sound
    something still plays.

    An id with NO owner is UNATTRIBUTED. That is 71.8% of GRAVE.BD, and it is
    the player, the HUD and the ambience: nothing in the enemy roster claims it,
    so nothing in the enemy roster licenses removing it. Unattributed is
    therefore NEVER droppable, which is why `owners` is deliberately a lookup
    that returns the empty set rather than a defaultdict that invents one.
    """

    owners: dict = field(default_factory=dict)
    refused_types: tuple = ()

    def owners_of(self, effect_id: int) -> frozenset:
        return self.owners.get(int(effect_id), frozenset())

    def is_attributed(self, effect_id: int) -> bool:
        return bool(self.owners_of(effect_id))


def derive_attribution(bank, target_bef, *,
                       revision: str = si.REVISION) -> Attribution:
    """`Attribution` for `bank` from the target world's parsed `.BEF`.

    Only ids the BANK ACTUALLY CARRIES are recorded, so a script literal naming
    an id no bank owns cannot make an unrelated id look attributed. A blob that
    will not decode is REPORTED and contributes no ownership -- which is the
    fail-closed direction: an undecodable owner makes its ids look unattributed,
    and unattributed is never dropped.
    """
    owners: dict = {}
    refused: list = []
    carried = set(bank.effect_ids)
    for type_id, report in sorted(si.sound_reports(target_bef, revision).items()):
        if report.refused:
            refused.append(int(type_id))
            continue
        for value in report.literal_values:
            if value in carried:
                owners[value] = owners.get(value, frozenset()) | {int(type_id)}
    return Attribution(owners=owners, refused_types=tuple(refused))


def reclaimable(bank, attribution: Attribution, *, lineup: Iterable[int],
                needed_ids: Iterable[int],
                walking_melee: Iterable[int]) -> tuple:
    """The `Dropped`s that are safe: FOUR conditions, all of them necessary.

    An id is dropped only when

      1. it is ATTRIBUTED -- some blob in this world references it. An
         unattributed id is the player/HUD/ambience and is never touched;
      2. at least one owner is a WALKING-MELEE type, i.e. a type the spawn pool
         actually draws, so absence from the lineup means absence from the run;
      3. EVERY owner is walking-melee. One non-walking-melee owner (a boss, a
         prop, a trigger) is enough to keep it, because those are not drawn from
         the lineup and their presence cannot be ruled out from it;
      4. EVERY owner is ABSENT from the final lineup -- foreign committed plus
         drawn natives.

    and additionally never when an admitted import needs it, which would be
    dropping a sample only to re-add it.

    Deterministic: sorted by EffectID, so a fixed lineup yields a fixed drop
    list and the artifact reproduces from the seed.
    """
    present = set(int(t) for t in lineup)
    needed = {int(i) for i in needed_ids}
    melee = {int(t) for t in walking_melee}

    drops: list = []
    for effect_id in sorted(attribution.owners):
        owners = attribution.owners_of(effect_id)
        if not owners:                                  # 1
            continue
        if effect_id in needed:
            continue
        if not (owners & melee):                        # 2
            continue
        if owners - melee:                              # 3
            continue
        if owners & present:                            # 4
            continue
        drops.append(sb.Dropped(
            effect_id=effect_id, size=(bank.sample_size(effect_id) or 0),
            owners=tuple(sorted(owners)),
            reason=("every owner is a walking-melee type absent from this "
                    "run's lineup, so nothing can play it")))
    return tuple(drops)


def reclaim_for_lineup(target_bytes: bytes, plans: Sequence, *,
                       game_files_dir, target_world: str,
                       lineup: Optional[Iterable[int]] = None,
                       walking_melee: Optional[Iterable[int]] = None,
                       log: Optional[Callable] = None):
    """`(Reclamation | None, problems)` for one world's final lineup.

    Returns None -- reclaiming NOTHING -- when the lineup is unknown or the
    target `.BEF` is unreadable. Both are the fail-closed direction: without a
    lineup no owner can be shown absent, and a bank that cannot be attributed
    has no id that meets the four conditions. Refusing to reclaim costs some
    enemies their voices; reclaiming on a guess deletes sounds the run plays.
    """
    problems: list = []
    if lineup is None:
        problems.append(
            f"no lineup available for {target_world.upper()}, so no sound "
            f"entry can be shown unplayable; nothing is reclaimed")
        if log is not None:
            log(f"{LOG_PREFIX}   SOUND NOTE {problems[-1]}")
        return None, tuple(problems)

    bef_path = Path(game_files_dir) / f"{target_world.upper()}.BEF"
    if not bef_path.is_file():
        problems.append(
            f"{bef_path.name} is not present, so the target bank cannot be "
            f"attributed; nothing is reclaimed")
        if log is not None:
            log(f"{LOG_PREFIX}   SOUND NOTE {problems[-1]}")
        return None, tuple(problems)

    if walking_melee is None:
        from .catalog import WALKING_MELEE_POOL
        walking_melee = tuple(WALKING_MELEE_POOL)

    bank = sb.parse(target_bytes)
    bef = BefFile.parse(bef_path)
    try:
        attribution = derive_attribution(bank, bef)
    finally:
        del bef
    if attribution.refused_types:
        problems.append(
            f"{bef_path.name}: {len(attribution.refused_types)} blob(s) did not "
            f"decode, so their sounds count as unattributed and are kept: "
            + " ".join(f"0x{t:02X}" for t in attribution.refused_types))

    # EVERY id any plan wants, admitted or not: an id we might import must not
    # be dropped first, and using the whole plan set rather than the eventual
    # admissions keeps the drop list independent of the admission walk (which
    # runs after, and against the reclaimed bank).
    needed = {i.effect_id for p in plans for i in p.unit.imports}

    drops = reclaimable(bank, attribution, lineup=lineup, needed_ids=needed,
                        walking_melee=walking_melee)
    reclamation = sb.reclaim(target_bytes, drops)
    if log is not None:
        log(f"{LOG_PREFIX} sound reclamation: "
            f"{len(attribution.owners)} of {len(set(bank.effect_ids))} "
            f"EffectID(s) in {target_world.upper()}.BD are attributed to a "
            f"{target_world.upper()}.BEF blob; the rest are player/HUD/ambience "
            f"and are never touched")
        for line in reclamation.log_lines(LOG_PREFIX):
            log(line)
    for problem in problems:
        if log is not None:
            log(f"{LOG_PREFIX}   SOUND NOTE {problem}")
    return reclamation, tuple(problems)


def merge_for_lineup(admitted: Sequence, *, game_files_dir,
                     target_world: str = DEFAULT_TARGET_WORLD,
                     # Kept for callers that name it. `sb.ceiling_for` is now
                     # ABSOLUTE and ignores it: the ceiling is where usable SPU
                     # RAM ends above the measured base, not a function of the
                     # bank. `HEADROOM_CONSERVATIVE` is GRAVE's exact free space,
                     # a reporting figure.
                     headroom: int = sb.HEADROOM_CONSERVATIVE,
                     lineup: Optional[Iterable[int]] = None,
                     walking_melee: Optional[Iterable[int]] = None,
                     log: Optional[Callable] = None):
    """The merged target bank for one world's committed lineup.

    Returns `(data_or_None, result_or_None, problems)`. `data` is None when
    there is nothing to write -- no committed arrival, no target bank, or every
    import deduped to free -- and in that case the vanilla bank is left alone
    rather than rewritten identically.

    Never raises.
    """
    problems: list = []
    target_path = _bank_path(game_files_dir, target_world)
    if not target_path.exists():
        problems.append(
            f"{target_path.name} is not present in {game_files_dir}, so the "
            f"cross-world arrivals cannot be given sound and will be SILENT")
        if log is not None:
            log(f"{LOG_PREFIX}   SOUND REFUSE {problems[-1]}")
        return None, None, tuple(problems)

    plans, plan_problems = collect_plans(
        admitted, game_files_dir=game_files_dir, log=log)
    problems.extend(plan_problems)
    if not plans:
        return None, None, tuple(problems)

    target_bytes = target_path.read_bytes()

    # A WORLD WHOSE SHIPPED BANK IS ALREADY OVER THE CEILING IS NOT MERGED INTO
    # AT ALL. This is not the same case as "no import fits": reclamation could
    # free room and make individual imports pass the per-unit ceiling check
    # while the bank as a whole still sits above the bound, i.e. we would ship a
    # bank that cannot load. MEASURED `roundup64(SAMPLE BLOCK)` occupancies
    # (research/STREAM_MEMORY_END_TRACE.md §5): grave 1,890,496 · swamp
    # 1,948,736 · ice 1,959,104 · under 1,971,648 · castle 1,973,184 -- ALL FIVE
    # under the 1,975,792 B `SPU_USABLE_CEILING`. So NO world trips this check
    # today; it is kept because it is the check that would catch a bank that
    # grew, and because it refuses by name of the BYTES and not by name of the
    # world.
    #
    # The occupancy compared here is the SAMPLE BLOCK, not the file size. The
    # header is parsed on the IOP and never uploaded, and charging it is exactly
    # what used to put under (-9,024) and castle (-10,688) over a ceiling they
    # were never actually over. The prior note here -- that the reverb-off
    # argument was INFERRED and that "the StreamMemoryEnd writers at SLUS_200.17
    # VA 0x00141714 / 0x00141858 are untraced" -- is retracted on both halves:
    # those two addresses belong to a DIFFERENT RPC server (a file/CD server;
    # trace §0), the real writer is 0x001B8100 called once from 0x00194C60 with
    # the literal 0x0001DA10, and reverb-off is now CONFIRMED (trace §4).
    #
    # WHAT IS STILL UNKNOWN, and why this remains a refusal rather than a spend:
    # no reverb write is not proof that the top of SPU RAM is otherwise unused.
    # Settling it needs an emulator memory watch on SPU2 RAM 0x1F0000-0x200000
    # across a CASTLE level load (trace §7.1). Overflow is SILENT -- the driver
    # has no ceiling check of any kind -- so the bound is refused against.
    # BOTH STRUCTURAL REFUSALS ARE EVALUATED, NOT SHORT-CIRCUITED, so a world
    # that fails both says so: CASTLE.BD is over the ceiling AND has no free
    # entry slot, and reporting only the first would leave the second to be
    # rediscovered by whoever tries to fix the first.
    _structural: list = []
    _occupancy = None
    try:
        _target_bank = sb.parse(target_bytes)
        _nsfx = _target_bank.count
        # SAMPLE BLOCK, not file size: the header never reaches the SPU.
        _occupancy = sb.roundup64(len(_target_bank.samples))
        del _target_bank
    except sb.SoundBankError as exc:
        _nsfx = None
        _structural.append(
            f"{target_path.name} does not parse ({exc}), so no import can be "
            f"placed in it")
    if _occupancy is not None and _occupancy > sb.SPU_USABLE_CEILING:
        _structural.append(
            f"{target_path.name}'s sample block already occupies "
            f"{_occupancy:,} B, which is "
            f"{_occupancy - sb.SPU_USABLE_CEILING:,} B ABOVE the "
            f"{sb.SPU_USABLE_CEILING:,} B usable-SPU ceiling (2 MB minus the "
            f"measured {sb.SPU_BASE:,} B StreamMemoryEnd base) BEFORE any "
            f"import, so merging into it could only produce a bank that cannot "
            f"load. The ceiling is not raised further: whether anything other "
            f"than reverb occupies the top of SPU RAM is STILL UNKNOWN, and "
            f"overflow is silent")

    # A WORLD WITH NO FREE SFX ENTRY SLOT IS NOT MERGED INTO EITHER, AND THIS IS
    # A DIFFERENT REFUSAL FROM THE ONE ABOVE. The ceiling check is about BYTES;
    # this one is about SLOTS, and no number of bytes can satisfy it.
    # `ProcessSoundDataHeader` rejects a bank whose nSFX is not below
    # `sb.MAX_SFX` (300), so a bank already at 299 entries cannot receive a
    # single import AT ANY SIZE -- and reclamation cannot help, because
    # reclaiming samples frees BYTES, not header rows. MEASURED nSFX: grave 277
    # (22 free) · swamp 286 (13) · ice 283 (16) · under 286 (13) · CASTLE 299
    # (ZERO). THIS IS NOW WHERE CASTLE BINDS: the StreamMemoryEnd trace legalised
    # castle's shipped bank on bytes (2,608 B free), so bytes no longer refuse it
    # and raising the byte ceiling cannot help -- it is blocked on ENTRY COUNT.
    # So CASTLE is structurally impossible, and it is refused HERE, by
    # the measured slot count and not by the world's name: a future bank with
    # room passes this check unchanged, and a world that fills up later starts
    # failing it without an edit.
    if _nsfx is not None and sb.MAX_SFX - 1 - _nsfx <= 0:
        _structural.append(
            f"{target_path.name} carries nSFX {_nsfx} against the driver's hard "
            f"limit of fewer than {sb.MAX_SFX} entries, so it has ZERO free SFX "
            f"entry slots and an import is STRUCTURALLY IMPOSSIBLE AT ANY BYTE "
            f"SIZE -- reclamation frees sample bytes, never header rows. The "
            f"limit is the driver's own (ProcessSoundDataHeader @VA 0x4460, "
            f"MJOLNIR.IRX) and is not raised here")

    if _structural:
        problems.append(
            f"{target_world.upper()} takes NO sound imports and its "
            f"cross-world arrivals will be SILENT ({len(_structural)} "
            f"structural reason(s), each fatal on its own): "
            + " || ".join(_structural))
        if log is not None:
            log(f"{LOG_PREFIX}   SOUND REFUSE {problems[-1]}")
        return None, None, tuple(problems)

    # THE CEILING IS AN ABSOLUTE SPU ADDRESS LIMIT, and it is computed ONCE,
    # HERE, FROM THE VANILLA BANK -- before a single byte is reclaimed.
    #
    # `ceiling_for` NOW MATCHES THAT EXPLANATION INSTEAD OF APPROXIMATING IT: it
    # returns the absolute `SPU_USABLE_CEILING` (2 MB minus the MEASURED
    # 121,360 B `StreamMemoryEnd` base), not "this bank's occupancy plus
    # headroom". The old relative form read like a function of the bank, and that
    # was the trap: recomputing it on the reclaimed bytes shrank the ceiling by
    # exactly what was freed, so reclamation bought nothing -- the feature would
    # run, log every drop, and admit the same two enemies. The call is still made
    # here, from the VANILLA bytes, so the ordering stays pinned by its test even
    # though the function is now bank-independent.
    # `test_the_ceiling_is_computed_from_the_original_bank_not_the_reclaimed_one`
    # fails if this line ever moves below the reclamation.
    ceiling = sb.ceiling_for(target_bytes, headroom=headroom)

    reclamation, recl_problems = reclaim_for_lineup(
        target_bytes, plans, game_files_dir=game_files_dir,
        target_world=target_world, lineup=lineup,
        walking_melee=walking_melee, log=log)
    problems.extend(recl_problems)
    merge_source = reclamation.data if reclamation is not None else target_bytes

    # THE ORDER IS THE SEED'S DRAW ORDER. `collect_plans` already sorts by draw
    # position, so `plans` is handed to the merge exactly as it comes back; no
    # cost sorting happens anywhere in this module.
    #
    # WHAT THAT COSTS, MEASURED -- kept here because it is the known price of
    # this choice, not a doubt about it
    # (research/grave_bd_sound_reclamation_measurement.md):
    #   * sound is ALL-OR-NOTHING per enemy (rule 3): a unit is admitted whole
    #     or not at all;
    #   * grave has 85,296 B free before reclamation;
    #   * per-enemy costs run from 0 B (0x49 Bomb_Skeleton -- every id it needs
    #     is already present) to 86,192 B (0x5D Doomed_Soul);
    #   * so ONE expensive enemy landing early in the draw order can consume the
    #     whole headroom and leave every cheaper enemy behind it SILENT.
    # Cheapest-first ordering measured 3 audible enemies in grave; draw order
    # measures fewer. Draw order was chosen by the user anyway. The tradeoff is
    # FEWER AUDIBLE ENEMIES ON AVERAGE, in exchange for the audible set being a
    # plain consequence of the seed rather than of sample cost.
    result = sb.merge(merge_source, [p.unit for p in plans],
                      ceiling=ceiling)
    result.reclamation = reclamation
    if log is not None:
        log(f"{LOG_PREFIX} sound admission order (the seed's draw order): "
            + " ".join(f"0x{p.type_id:02X}" for p in plans))
        for line in result.log_lines(LOG_PREFIX):
            log(line)
    if not result.changed:
        # Every id was already in the target: the arrivals are audible with the
        # vanilla bank, so there is nothing to ship.
        if log is not None:
            log(f"{LOG_PREFIX}   sound bank unchanged: every derived EffectID "
                f"is already in {target_path.name}")
        return None, result, tuple(problems)
    return result.data, result, tuple(problems)
