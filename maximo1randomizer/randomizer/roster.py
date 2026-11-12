"""Roster construction for the randomized enemy roster.

Task 1 of `randomized-enemy-roster` adds exactly one thing here: COMPANION
CLOSURE DERIVATION. `AllowEntry.companions is None` means "uninvestigated" and
the companion porter refuses such a route, so a roster of 25-35 enemies is only
possible if the closures can be DERIVED from the game data instead of
hand-measured. `derive_companions` is that derivation, and the calibration test
in `tests/test_roster_companion_derivation.py` is what licenses trusting it: it
must reproduce every hand-measured `companions` literal in
`catalog.CROSS_WORLD_ALLOWLIST` exactly.

NOTHING IS REIMPLEMENTED HERE. The decoder is `befscript` (158 `verified`
opcodes), the Spawn-edge oracle and the bounded closure are
`companion_port.make_spawn_edge_oracle` / `companion_port.resolve_group`, both
unchanged. This module only adapts them to the shape the selector needs:
a `(closure, reason)` pair that NEVER raises, so an enemy whose closure cannot
be derived becomes an EXCLUSION rather than a crashed build (Requirement 2.3).

The three-valued distinction of Requirement 2.5 survives intact: a populated
closure and an empty closure are both `(closure, None)` -- "investigated" --
while "could not tell" is `(frozenset(), reason)` and is never confused with
"investigated and has none".
"""
from __future__ import annotations

from typing import Optional

from . import befscript
from . import companion_port
from .bef import BefFile

#: Every target of this feature is retail. The revision is always explicit and
#: is never inferred, matching `companion_port`'s rule.
REVISION = befscript.REVISION_RETAIL

_ORACLE_ATTR = "_roster_spawn_edge_oracle"

_VALUE_DESCRIPTIONS = {
    companion_port.VALUE_MEMBER: "member field",
    companion_port.VALUE_LOCAL: "local slot",
    companion_port.VALUE_REGISTER: "register",
    companion_port.VALUE_UNKNOWN: "an unrecoverable value",
}


def _spawn_edge_oracle(source_bef: BefFile):
    """The edge oracle for one parsed `.BEF`, cached on the file object.

    Building it disassembles every blob in the file, so a caller deriving many
    closures out of one world would otherwise pay that cost per enemy. The cache
    lives on the `BefFile` instance, so it is dropped exactly when the file is --
    which keeps the one-`.PRS`/few-binaries memory discipline intact.
    """
    oracle = getattr(source_bef, _ORACLE_ATTR, None)
    if oracle is not None and getattr(oracle, "revision", None) == REVISION:
        return oracle
    oracle = companion_port.make_spawn_edge_oracle(source_bef, REVISION)
    try:
        setattr(source_bef, _ORACLE_ATTR, oracle)
    except (AttributeError, TypeError):       # pragma: no cover - defensive
        pass
    return oracle


def _describe_argument(value) -> str:
    if value is None:
        return "no traceable argument"
    kind = getattr(value, "kind", companion_port.VALUE_UNKNOWN)
    label = _VALUE_DESCRIPTIONS.get(kind, kind)
    index = getattr(value, "value", None)
    if kind == companion_port.VALUE_UNKNOWN or index is None:
        return label
    return f"{label} {index}"


def _undecidable_reason(report, type_id: int, source_world: str) -> Optional[str]:
    """Why this entity's own Spawn edges cannot be read, or None if they can.

    The two cases are kept DISTINCT because they mean different things
    downstream: a decode failure is a gap in the decoder or a damaged blob,
    while a non-literal argument is a script that computes the spawned id at
    runtime (Bomb_Skeleton's fuse is `by_m_push member=12` on `CreateTimer`, so
    this compiler demonstrably does pass member fields, and a `Spawn` that did
    the same could not be resolved statically at all).
    """
    if report is None:
        return (f"0x{type_id:02X} has no blob in {source_world}.BEF, so its "
                f"Spawn edges cannot be read")
    if report.refused:
        return (f"0x{type_id:02X} in {source_world}.BEF: Script_Section did not "
                f"decode ({report.reason})")
    nonliteral = report.nonliteral_sites
    if nonliteral:
        site = nonliteral[0]
        arg = site.args[0] if site.args else None
        return (f"0x{type_id:02X} in {source_world}.BEF: Spawn at offset "
                f"{site.call_offset} takes {_describe_argument(arg)}, not a "
                f"literal, so the spawned type id is not derivable statically "
                f"({len(nonliteral)} such site(s))")
    return None


def derive_companions(
        type_id: int,
        source_world: str,
        source_bef: BefFile,
        *,
        target_bef: Optional[BefFile] = None,
        max_depth: int = companion_port.COMPANION_MAX_DEPTH,
        require_present: bool = True,
) -> tuple[frozenset, Optional[str]]:
    """`(closure, None)` on success, `(frozenset(), reason)` on failure.

    The closure is the transitive set of type ids the enemy SPAWNS, EXCLUDING
    the root, matching `AllowEntry.companions`. It is derived by decoding each
    entity's `Script_Section` with `befscript`, reading the literal operand of
    API slot 49 `Spawn`, and closing transitively through
    `companion_port.resolve_group` with visit-once dedupe -- so the retail
    `0x1E` <-> `0xBA` cycle inside Snowman `0x1D`'s group resolves rather than
    refusing.

    `resolve_group`'s `max_depth` and `require_present` behaviour is passed
    through unchanged; `companion_port` is not modified by this feature.

    THIS FUNCTION NEVER RAISES. An undecodable section, a non-literal (indirect)
    `Spawn` argument, an id the source world does not declare, and a closure
    that runs deeper than `max_depth` all come back as a REASON STRING, because
    downstream every one of them has to be an exclusion from the draw rather
    than a crashed build (Requirement 2.3).

    `target_bef` is accepted because callers in the cost layer hold it anyway;
    it deliberately does NOT change the closure. Whether a companion is already
    present in the target is a COST question (Requirement 2.4, zero added cost
    for a reused entry), not a membership question -- `0x3A`'s `0x35` is in
    `GRAVE.BEF` and is still a member of `0x3A`'s closure.
    """
    del target_bef                      # by design: see the docstring.
    try:
        oracle = _spawn_edge_oracle(source_bef)
        reports = getattr(oracle, "reports", {})

        root_reason = _undecidable_reason(reports.get(type_id), type_id,
                                         source_world)
        if root_reason is not None:
            return frozenset(), root_reason

        try:
            group = companion_port.resolve_group(
                type_id, source_world, source_bef, oracle,
                max_depth=max_depth, require_present=require_present)
        except companion_port.CompanionResolveError as exc:
            return frozenset(), (f"0x{type_id:02X} in {source_world}: companion "
                                 f"closure unresolvable ({exc})")

        closure = frozenset(node.type_id for node in group.members)
        for member in sorted(closure):
            reason = _undecidable_reason(reports.get(member), member,
                                         source_world)
            if reason is not None:
                # A member whose own edges are unreadable means the closure may
                # be INCOMPLETE, and an incomplete closure ports an enemy whose
                # objects may be missing -- the exact defect being avoided.
                return frozenset(), (
                    f"0x{type_id:02X} in {source_world}: companion {reason}")
        return closure, None
    except Exception as exc:            # pragma: no cover - defensive
        return frozenset(), (f"0x{type_id:02X} in {source_world}: companion "
                             f"derivation failed ({exc!r})")


# ===========================================================================
# Task 2: roster discovery and classification (Requirements 1.1 - 1.7)
# ===========================================================================
#
# MEMORY DISCIPLINE (Requirement 11.3, four binaries per process).
# `.BEF` files are read ONE AT A TIME and reduced immediately to a small
# picklable `WorldSnapshot` (a set of type ids plus the derived closures), then
# DROPPED. Peak residency is therefore one `.BEF`, never five. The five worlds
# still amount to five parses, which is one more than the per-process limit
# allows, so `build_roster` REFUSES to parse more than `BEF_PARSE_BUDGET` files
# in a single call: the caller reads the worlds in BATCHES (4 + 1) and hands the
# earlier batch's snapshots back in via `snapshots=`. That is the "batch or
# cache" approach the task asks for, and it is enforced rather than documented
# -- asking for all five worlds with an empty cache raises instead of silently
# returning a partial roster, because a partial roster is exactly the "an enemy
# present in the game data is silently absent" failure Requirement 1.1 exists to
# prevent.

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Optional as _Optional

from . import catalog
from . import items
from . import spawn_config

#: Classification kinds. `UNCLASSIFIED` is the fail-closed bucket (Req 1.6).
ENEMY = "enemy"
COMPANION_ONLY = "companion_only"
BOSS = "boss"
NON_ENEMY = "non_enemy"
UNCLASSIFIED = "unclassified"

#: Locomotion tiers. Tier 1 is the only tier the shipping path draws from.
LOCOMOTION_WALKER = "walker"        # Tier 1
LOCOMOTION_OTHER = "other"          # Tier 2, gated off (see eligible_foreign)
LOCOMOTION_UNKNOWN = "unknown"      # fail closed

#: The five worlds whose `.BEF` type tables define the roster (Requirement 1.1).
ROSTER_WORLDS = ("grave", "under", "swamp", "ice", "castle")

#: Requirement 11.3. See the block comment above for how five worlds fit.
BEF_PARSE_BUDGET = 4

# NON-ENEMY TYPES ARE DERIVED, NOT RE-ENUMERATED (Requirement 1.5).
# Every id below comes from a table that already exists for another purpose, so
# this set cannot drift away from progression protection the way a hand-copied
# list would. `items.PROGRESSION_ANCHOR_TYPES` and `items.GOLD_KEY_TYPE` are the
# type-level backbone of `items.get_progression_protected_names`, which is
# instance-name based and needs a parsed `.PSX` -- parsing five `.PSX` files
# here would breach Requirement 11.3, so the TYPE side of that function is what
# is reused. `items.PROTECTED_TAGS` gates CHEST CONTENT, so the types it can
# ever apply to are the chest/pickup types in `spawn_config.ITEM_TYPES` and
# `catalog.TREASURE_POOL`; both are included whole, which is strictly safer than
# selecting from them.
def _non_enemy_types() -> dict[int, str]:
    sources: tuple[tuple[str, Iterable[int]], ...] = (
        ("items.PROGRESSION_ANCHOR_TYPES", items.PROGRESSION_ANCHOR_TYPES),
        ("items.GOLD_KEY_TYPE", (items.GOLD_KEY_TYPE,)),
        ("items.GATE_TYPES", items.GATE_TYPES),
        ("spawn_config.ITEM_TYPES (PROTECTED_TAGS applies to these)",
         spawn_config.ITEM_TYPES),
        ("spawn_config.STRUCTURE_TYPES", spawn_config.STRUCTURE_TYPES),
        ("catalog.TREASURE_POOL", catalog.TREASURE_POOL),
        ("catalog.TRIGGER_POOL", catalog.TRIGGER_POOL),
    )
    out: dict[int, str] = {}
    for label, ids in sources:
        for type_id in ids:
            # An id an ENEMY table also claims must never be demoted here.
            if type_id in catalog.WALKING_MELEE_POOL:
                continue
            out.setdefault(int(type_id), label)
    return out


NON_ENEMY_TYPES: dict[int, str] = _non_enemy_types()

# BOSSES ARE HAND-DECLARED (Requirement 1.7 concedes this is a scoping call).
#
# THIS SET IS EMPTY, and that is a deliberate, safe state rather than an
# oversight. No boss type id has been identified from the data: bosses appear
# only in the `*_BOSS` / `*_KING` / `*_QUEEN` level rows that
# `elf_patch._LEVEL_BOSS_RE` matches, and reading which type ids those rows
# place would mean parsing those `.PSX` files -- outside Requirement 11.3's
# budget and outside this task.
#
# Nothing is admitted by the omission. A boss id is not in
# `WALKING_MELEE_POOL`, so the `ENEMY` rule below cannot claim it and it lands
# in `UNCLASSIFIED`, which is excluded from the draw and logged. Bosses are
# therefore excluded by the fail-closed rule today; adding an id here only
# improves the REASON string, never the safety.
BOSS_TYPES: dict[int, str] = {}


@dataclass(frozen=True)
class RosterEntry:
    """One entity type, with the evidence for how it was classified."""
    type_id: int
    name: str
    worlds: frozenset            # every world whose .BEF declares it (Req 1.2)
    kind: str
    locomotion: str
    reason: str                  # which rule fired (Req 1.5)
    companions: frozenset = frozenset()
    companion_reason: _Optional[str] = None

    @property
    def eligible(self) -> bool:
        """Tier-1 eligibility: an ENEMY with a known locomotion."""
        return self.kind == ENEMY and self.locomotion == LOCOMOTION_WALKER


@dataclass(frozen=True)
class WorldSnapshot:
    """Everything Task 2 needs from ONE world's `.BEF`, with the file dropped.

    Small and picklable on purpose: it is what a batch hands to the next call,
    so the five worlds never have to be resident (or even parsed) together.
    """
    world: str
    type_ids: frozenset
    closures: dict = field(default_factory=dict)          # type_id -> frozenset
    closure_failures: dict = field(default_factory=dict)  # type_id -> reason


def _name_for(type_id: int) -> str:
    """Readable name from the tables that already exist, else Unknown_0xNN."""
    name = catalog.name_for_type(type_id)
    if not name.startswith("Unknown_"):
        return name
    for table in (spawn_config.ENEMY_TYPES, spawn_config.ITEM_TYPES,
                  spawn_config.STRUCTURE_TYPES, items.GATE_NAMES):
        if type_id in table:
            return str(table[type_id]).replace(" ", "_")
    return name


def world_snapshot(game_files_dir=None, world: str = "", *, bef=None,
                   derive: bool = True) -> WorldSnapshot:
    """Read ONE world's `.BEF`, reduce it to a snapshot, drop the file.

    Closures are derived here rather than later because the Spawn-edge oracle
    needs the parsed blobs, and this is the only point at which they are in
    memory. `derive_companions` never raises, so an undecodable entity becomes a
    recorded failure reason (Requirement 2.3) and the snapshot still completes.
    """
    owned = bef is None
    if owned:
        path = Path(game_files_dir) / f"{world.upper()}.BEF"
        bef = BefFile.parse(path)
    try:
        type_ids = frozenset(bef.type_ids())
        closures: dict = {}
        failures: dict = {}
        if derive:
            for type_id in sorted(type_ids):
                closure, reason = derive_companions(type_id, world, bef)
                if reason is None:
                    closures[type_id] = closure
                else:
                    failures[type_id] = reason
        return WorldSnapshot(world=world, type_ids=type_ids, closures=closures,
                             closure_failures=failures)
    finally:
        if owned:
            del bef


def read_world_snapshots(game_files_dir, worlds: Iterable[str], *,
                         bef_cache: _Optional[Mapping] = None,
                         parse_budget: int = BEF_PARSE_BUDGET,
                         derive: bool = True) -> dict:
    """Snapshot several worlds, ONE resident `.BEF` at a time.

    Raises `ValueError` when the request would parse more than `parse_budget`
    files, which is Requirement 11.3 enforced rather than merely documented.
    Worlds supplied pre-parsed in `bef_cache` cost no parse.
    """
    worlds = list(dict.fromkeys(worlds))
    cache = dict(bef_cache or {})
    to_parse = [w for w in worlds if w not in cache]
    if len(to_parse) > parse_budget:
        raise ValueError(
            f"reading {len(to_parse)} .BEF files in one process exceeds the "
            f"four-binaries-per-process limit ({to_parse}). Read them in "
            f"batches of {parse_budget} and pass the earlier batch's snapshots "
            f"back in via snapshots=, or supply parsed files in bef_cache.")
    out: dict = {}
    for world in worlds:
        out[world] = world_snapshot(game_files_dir, world,
                                    bef=cache.get(world), derive=derive)
    return out


def classify_roster(snapshots: Mapping) -> dict:
    """Classify the union of the snapshots' type tables (Requirements 1.2-1.7).

    Precedence, exactly as the design states, with the rule that fired recorded
    in each entry's `reason`:

      1. `COMPANION_ONLY` -- the type appears in some entity's DERIVED closure
         and is not itself a type the placer ever places. `WALKING_MELEE_POOL`
         is the "placed as a standalone enemy" oracle here: it is the set the
         per-record placer draws from, so a type outside it is never placed by a
         level's own records through this randomizer.
      2. `NON_ENEMY`  -- derived from the existing protected-anchor / item /
         structure / trigger tables (see `NON_ENEMY_TYPES`).
      3. `BOSS`       -- hand-declared, evidence recorded. Empty today; see
         `BOSS_TYPES` for why that is safe.
      4. `ENEMY`      -- named by `WALKING_MELEE_POOL`.
      5. `UNCLASSIFIED` -- everything else. Excluded from the draw and logged.
    """
    worlds_by_type: dict[int, set] = {}
    for world, snap in snapshots.items():
        for type_id in snap.type_ids:
            worlds_by_type.setdefault(type_id, set()).add(world)

    # Who spawns what, across every world we read. First spawner found is the
    # one quoted as evidence; the count says how much evidence there is.
    spawned_by: dict[int, list[tuple[int, str]]] = {}
    for world, snap in snapshots.items():
        for root, closure in snap.closures.items():
            for member in closure:
                if member != root:
                    spawned_by.setdefault(member, []).append((root, world))

    roster: dict[int, RosterEntry] = {}
    for type_id in sorted(worlds_by_type):
        worlds = frozenset(worlds_by_type[type_id])
        in_pool = type_id in catalog.WALKING_MELEE_POOL
        locomotion = LOCOMOTION_WALKER if in_pool else LOCOMOTION_UNKNOWN

        # The closure and its failure reason, preferring a world that succeeded.
        companions = frozenset()
        companion_reason = None
        for world in sorted(worlds):
            snap = snapshots[world]
            if type_id in snap.closures:
                companions = snap.closures[type_id]
                companion_reason = None
                break
            if type_id in snap.closure_failures and companion_reason is None:
                companion_reason = snap.closure_failures[type_id]

        spawners = spawned_by.get(type_id, ())
        if spawners and not in_pool:
            root, root_world = spawners[0]
            kind = COMPANION_ONLY
            reason = (
                f"companion_only: appears in the derived companion closure of "
                f"0x{root:02X} {_name_for(root)} in {root_world} "
                f"({len(spawners)} spawner site(s)), and is not a type the "
                f"per-record placer ever places on its own "
                f"(absent from WALKING_MELEE_POOL)")
            if type_id in NON_ENEMY_TYPES:
                # Real overlap, e.g. Gold_Key 0x2B and Skeleton_Key 0x65 are
                # both SPAWNED and progression anchors. COMPANION_ONLY wins by
                # the design's precedence and both verdicts exclude the type,
                # but the anchor evidence is recorded so a reader of the log
                # cannot mistake this for protection having been dropped.
                reason += (f"; ALSO declared by {NON_ENEMY_TYPES[type_id]}, so "
                           f"it is progression/scenery too -- excluded either "
                           f"way")
        elif type_id in NON_ENEMY_TYPES:
            kind = NON_ENEMY
            reason = (f"non_enemy: declared by {NON_ENEMY_TYPES[type_id]}, so "
                      f"it is a progression / item / scenery / trigger entity, "
                      f"not a standalone enemy")
        elif type_id in BOSS_TYPES:
            kind = BOSS
            reason = (f"boss: hand-declared out of scope (Requirement 1.7) -- "
                      f"{BOSS_TYPES[type_id]}. Bosses appear only in the "
                      f"*_BOSS / *_KING / *_QUEEN level rows that "
                      f"elf_patch._LEVEL_BOSS_RE matches")
        elif in_pool:
            kind = ENEMY
            reason = (f"enemy: named by WALKING_MELEE_POOL, which is also the "
                      f"locomotion oracle -- Tier 1 walker, so the anim-donor "
                      f"search has a correct donor for it")
        else:
            kind = UNCLASSIFIED
            reason = (f"unclassified: declared by {sorted(worlds)} but claimed "
                      f"by no classification rule, and absent from "
                      f"WALKING_MELEE_POOL so its locomotion is unknown. "
                      f"EXCLUDED from the draw (Requirement 1.6)")

        roster[type_id] = RosterEntry(
            type_id=type_id, name=_name_for(type_id), worlds=worlds, kind=kind,
            locomotion=locomotion, reason=reason, companions=companions,
            companion_reason=companion_reason)
    return roster


def build_roster(game_files_dir=None, *, bef_cache: _Optional[Mapping] = None,
                 snapshots: _Optional[Mapping] = None,
                 worlds: Iterable[str] = ROSTER_WORLDS,
                 parse_budget: int = BEF_PARSE_BUDGET) -> dict:
    """Union the worlds' `.BEF` type tables, then classify each type.

    `snapshots` carries the result of an earlier batch (or of another process);
    `bef_cache` accepts already-parsed `BefFile`s the caller holds anyway. With
    neither, asking for all five worlds RAISES rather than returning a partial
    roster -- see `read_world_snapshots`.
    """
    known = dict(snapshots or {})
    wanted = list(dict.fromkeys(worlds))
    missing = [w for w in wanted if w not in known]
    if missing:
        known.update(read_world_snapshots(
            game_files_dir, missing, bef_cache=bef_cache,
            parse_budget=parse_budget))
    return classify_roster({w: known[w] for w in wanted})


# ---------------------------------------------------------------------------
# Eligibility (Requirements 1.3, 1.4)
# ---------------------------------------------------------------------------

def eligible_foreign(roster: Mapping, target_world: str, *,
                     allow_non_walkers: bool = False) -> list:
    """Enemies that would have to be PORTED into `target_world`.

    A native is deliberately absent from this list: it needs no port and costs
    no budget, so it is not a route (Requirement 1.3). That is a statement about
    PORTING COST only -- natives remain eligible for the lineup through
    `eligible_native`, so "not a route" never means "cannot appear".

    `allow_non_walkers` is the TIER 2 GATE. It is DEFAULT FALSE and the shipping
    path does not pass it. Non-walkers have no correct animation donor: a ported
    enemy borrows a native walker's animation set, and the anim-donor search
    itself filters on `WALKING_MELEE_POOL`. Admitting them would ship enemies
    animating wrongly, so the gate exists to be defined, not to be used.
    """
    out = []
    for type_id, entry in sorted(roster.items()):
        if entry.kind != ENEMY:
            continue
        if entry.locomotion != LOCOMOTION_WALKER and not allow_non_walkers:
            continue
        if entry.locomotion == LOCOMOTION_UNKNOWN and not allow_non_walkers:
            continue
        if catalog.is_native(type_id, target_world):
            continue
        if not (entry.worlds - {target_world}):
            continue            # declared nowhere else: no source world exists
        out.append(type_id)
    return out


def eligible_native(roster: Mapping, target_world: str) -> list:
    """ENEMY-kind types already native to `target_world` (Requirement 4.8).

    These are the natives the seeded draw varies. They cost zero budget.
    """
    return [type_id for type_id, entry in sorted(roster.items())
            if entry.kind == ENEMY
            and entry.locomotion == LOCOMOTION_WALKER
            and catalog.is_native(type_id, target_world)]


def classification_counts(roster: Mapping) -> dict:
    """`{kind: count}` for the `[cross-world]` log block (Requirement 9.1)."""
    counts: dict[str, int] = {}
    for entry in roster.values():
        counts[entry.kind] = counts.get(entry.kind, 0) + 1
    return counts


def excluded_log_lines(roster: Mapping, target_world: str,
                       *, kinds: Iterable[str] = (UNCLASSIFIED,)) -> list:
    """One `[cross-world]`-prefixed line per excluded type, with its reason.

    Requirement 1.6 asks for unclassified types to be LOGGED rather than
    speculatively included, and Requirement 1.4 asks for the exclusion reason to
    be recorded. `kinds` widens the report to the other excluded kinds.
    """
    wanted = set(kinds)
    return [f"[cross-world]   EXCLUDE 0x{entry.type_id:02X} {entry.name:<20} "
            f"{entry.reason}"
            for entry in sorted(roster.values(), key=lambda e: e.type_id)
            if entry.kind in wanted]


# ===========================================================================
# Task 2 of `cross-world-structures`: the SELECTION layer
# (Requirements 2.1 - 2.5, 3.2, 3.3, 3.4)
# ===========================================================================
#
# WHY HERE AND NOT IN `catalog`.
# `catalog` holds the per-route ALLOWLIST and the enemy allowlist rows; this
# module holds CLASSIFICATION. Candidacy and exclusion are classification
# questions, so they belong here, and Task 4's `CROSS_WORLD_STRUCTURE_ALLOWLIST`
# still belongs in `catalog` beside the enemy allowlist. There is also a hard
# reason: `spawn_config` imports `catalog` (line 32 of spawn_config.py), so
# `catalog` CANNOT import `spawn_config` without a cycle -- and Requirement 2.1
# demands the candidate set be derived from `spawn_config.STRUCTURE_TYPES`. This
# module already imports `catalog`, `items` AND `spawn_config`, which are exactly
# the three tables the exclusion rules must cross-reference. No new module.
#
# NOTHING BELOW CHANGES BEHAVIOUR. `classify_roster` is untouched, so every
# structure type keeps classifying `non_enemy` for the ENEMY draw
# (Requirement 2.3) -- the `STRUCTURE` kind is a SECOND, additive view returned
# by `structure_kind()` / `structure_classification()`, consulted only by the
# (not yet existing) structure draw. There is no switch yet; this task declares
# and classifies, and calls nothing.

#: The `STRUCTURE` roster kind (Requirement 2.2). It sits alongside `ENEMY`,
#: `COMPANION_ONLY`, `BOSS`, `NON_ENEMY` and `UNCLASSIFIED`, but it is
#: DELIBERATELY NOT returned by `classify_roster`: Requirement 2.3 says these
#: types must keep reporting `non_enemy` to the ENEMY draw so a structure can
#: never fill an enemy slot. `NON_ENEMY_TYPES` already derives them from
#: `spawn_config.STRUCTURE_TYPES`, and that derivation is left exactly as it is.
STRUCTURE = "structure"

#: Monster_Generator. Named once, here, so the reason strings and the exclusion
#: cannot drift apart.
MONSTER_GENERATOR_TYPE = 0x5F


def structure_candidates() -> dict[int, str]:
    """Every declared structure type, DERIVED from the table that defines it.

    Requirement 2.1: the candidate set is `spawn_config.STRUCTURE_TYPES`, never a
    second hand-written list, so the feature and the spawn editor cannot
    disagree. Names come back underscored to match `catalog.name_for_type`'s
    shape, which is what the log lines use.

    This is the RAW candidate set. It still contains the excluded and ineligible
    types; `portable_structure_sources()` is the filtered one.
    """
    return {int(type_id): str(name).replace(" ", "_")
            for type_id, name in sorted(spawn_config.STRUCTURE_TYPES.items())}


def _overlapping_tables(type_id: int) -> list[str]:
    """Which progression / anchor / hazard tables also claim this type.

    Requirement 2.5: where a type is in `STRUCTURE_TYPES` and in one of these,
    the EXCLUSION wins and the reason must NAME BOTH TABLES. This function
    supplies the second half of every such reason string, read out of the live
    tables rather than transcribed, so adding an id to a protection table
    automatically widens the reason instead of silently disagreeing with it.
    """
    tables: tuple[tuple[str, object], ...] = (
        ("items.PROGRESSION_ANCHOR_TYPES", items.PROGRESSION_ANCHOR_TYPES),
        ("items.GOLD_KEY_TYPE", (items.GOLD_KEY_TYPE,)),
        ("items.GATE_TYPES", items.GATE_TYPES),
        ("items.HAZARD_PROTECTED_AS_SOURCE", items.HAZARD_PROTECTED_AS_SOURCE),
    )
    return [label for label, ids in tables if type_id in set(ids)]


def _excluded_reason() -> dict[int, str]:
    """`0x5F` Monster_Generator's exclusion, with its MEASURED reason.

    Requirement 2.4. The reason is not a preference: the blob carries 6 GLOBAL
    material refs and ZERO local mesh/anim refs, so its assets already resolve
    against `GLOBAL.PRS` in every world. That is why it works everywhere today
    and why porting it would move nothing.

    It is ALSO an `items.PROGRESSION_ANCHOR_TYPES` member -- the Monster_Generator
    anchor Requirement 3.1 protects -- so per Requirement 2.5 the reason names
    both tables.
    """
    type_id = MONSTER_GENERATOR_TYPE
    name = structure_candidates().get(type_id, "Monster_Generator")
    also = _overlapping_tables(type_id)
    reason = (
        f"0x{type_id:02X} {name}: declared by spawn_config.STRUCTURE_TYPES but "
        f"EXCLUDED from porting -- measured 6 GLOBAL material refs and ZERO "
        f"local mesh/anim refs, so its assets already resolve against "
        f"GLOBAL.PRS in every world and it needs no port (Req 2.4)")
    if also:
        reason += (f"; ALSO declared by {', '.join(also)}, and the EXCLUSION "
                   f"wins over both (Req 2.5, Req 3.1 anchor)")
    return {type_id: reason}


#: Requirement 2.4 / 2.5. Excluded from PORTING outright -- not a route in either
#: direction, for any seed.
STRUCTURE_PORT_EXCLUDED: dict[int, str] = _excluded_reason()

# ORDERED PUZZLE PROPS ARE DERIVED, NOT INVENTED (Requirement 3.3).
#
# THE HOLD ON THESE FOUR IS NOW LIFTED TOO -- see `_ORDERED_PUZZLE_PROP_IDS`
# below and `STRUCTURE_HOLD_LIFTED`. The derivation is kept EXACTLY as it was,
# because it is still what identifies the four, and it still builds the reason
# strings below. What changed is where those four END UP: they are no longer in
# `STRUCTURE_INELIGIBLE_SOURCES` (which is now empty), and each of them has its
# own phase-2 entry in `_STRUCTURE_HOLD_LIFTED_REASONS` -- hand-written there,
# not generated from this derivation -- so they appear in
# `STRUCTURE_HOLD_LIFTED`. This derivation is therefore now a DERIVED-EVIDENCE
# check and the source of `_ORDERED_PUZZLE_PROP_IDS`; it does not itself feed
# either table.
#
# `items.HAZARD_PROTECTED_AS_SOURCE` already carries the measured evidence, in
# its own words: the coffin lids, torches and statues in levels like S_SUB3 "are
# numbered 1st/2nd/.../9th and must remain in their vanilla sequence for the gate
# scripts to trigger correctly", and moving them leaves the level in infinite
# loading. The four ids that block carries with that comment are
#   0xB5 Swamp_Coffin_Lid, 0xC8 Spirit_Statue, 0xD3 Breakable_TikkiTorch (S_SUB3)
#   0xAE Underworld_Lantern (U_SUB3)
# and they are exactly the INTERSECTION of that table with
# `spawn_config.STRUCTURE_TYPES` -- every other member of the hazard table is a
# gate, trigger, hazard or ambient entity that `STRUCTURE_TYPES` never declares.
# So the intersection IS the ordered-puzzle-prop set, taken from the existing
# evidence instead of retyped, and it widens automatically if either table grows.
def _ordered_puzzle_props() -> dict[int, str]:
    out: dict[int, str] = {}
    names = structure_candidates()
    for type_id in sorted(set(names) & set(items.HAZARD_PROTECTED_AS_SOURCE)):
        if type_id in STRUCTURE_PORT_EXCLUDED:
            continue
        out[type_id] = (
            f"0x{type_id:02X} {names[type_id]}: Ordered_Puzzle_Prop. Declared by "
            f"spawn_config.STRUCTURE_TYPES AND by "
            f"items.HAZARD_PROTECTED_AS_SOURCE, whose recorded evidence is that "
            f"these props form numbered chains (1st..9th) a level script indexes "
            f"in the S_SUB3 / U_SUB3 sequences, and breaking the order leaves "
            f"the level in infinite loading. A standalone copy in a foreign "
            f"world has no chain to join, so it is INELIGIBLE AS A SOURCE until "
            f"individually measured (Req 3.3); the exclusion wins over both "
            f"tables (Req 2.5)")
    return out


#: The four Ordered_Puzzle_Prop ids, DERIVED as before from the intersection of
#: `spawn_config.STRUCTURE_TYPES` with `items.HAZARD_PROTECTED_AS_SOURCE`. Still
#: derived rather than retyped so it cannot drift from the evidence, and still
#: read out of the LIVE tables -- `items.HAZARD_PROTECTED_AS_SOURCE` is not
#: modified by this change and this reads it, so if it ever loses one of these
#: ids that is visible here immediately.
_ORDERED_PUZZLE_PROP_IDS: tuple = tuple(sorted(_ordered_puzzle_props()))

#: The three progression-relevant grave structures (Requirement 3.4). Hand-named
#: because no existing table records WHY they gate progression -- each carries
#: its own reason rather than sharing one, as the task asks.
_PROGRESSION_STRUCTURE_REASONS: dict[int, str] = {
    0x01: ("Bone Tower is a script-driven reveal: the tower is assembled/"
           "collapsed by the level's own script rather than existing as inert "
           "scenery, so a standalone copy in a foreign world has no script to "
           "drive it"),
    0x02: ("Bonus Grave GATES BONUS ACCESS -- it is the entry point to a bonus "
           "area, so a copy in a foreign world either leads nowhere or offers a "
           "second entrance to content the run has already accounted for"),
    0x0A: ("Popup Headstone is a scripted reveal: it pops up on a script cue "
           "instead of being present at load, so ported standalone it has no cue "
           "and may never appear at all"),
}


# THE FOUR TASK-3 ADDITIONS, AND WHY THE HOLD ON THEM IS NOW LIFTED.
#
# HISTORY, KEPT BECAUSE IT IS THE REASON THIS TABLE HAS A SHAPE.
# `.kiro/specs/cross-world-structures/measurements.md` recommended four more
# INELIGIBLE SOURCES out of its script survey and its resolved companion
# closures -- `0x17` Smashable_Snow_Pirate, `0xAD` Spirit_Torch, `0x50`
# Standing_Coffin, `0xBC` Smashable_Coffin. Task 4 accepted all four on
# ASYMMETRIC RISK and withheld their 16 routes.
#
# THE HOLD IS NOW LIFTED, ON THE USER'S EXPLICIT INSTRUCTION. It was never a
# measurement failure. All 16 routes were MEASURED and `verify_session` green in
# Task 3, so what held them back was POLICY -- this repo's own caution about
# what a foreign copy might DO in game, not any evidence that the port is
# wrong. The user asked for these structures specifically, was told what is
# still unsettled about each, and ACCEPTED THE RISK that one of them
# misbehaves in game. That decision is theirs to make and it is recorded here
# rather than argued with.
#
# WHAT LIFTING THE HOLD DOES AND DOES NOT CHANGE.
#   It changes SOURCE eligibility only. Every DESTINATION protection runs
#   exactly as before, inside the item pass: PROTECTED_TAGS,
#   PROTECTED_INSTANCE_NAMES, the `end*` kill-groups, the Gold_Key /
#   Level_Column / Skeleton_Key / Monster_Generator anchors and
#   `items.HAZARD_PROTECTED_AS_SOURCE` (which still carries -- and must keep
#   carrying -- `0xAE`, `0xB5`, `0xC8`, `0xD3`). No protected record becomes a
#   destination because of this change. `catalog.HELD_BACK_ENEMIES_ON_OBSERVED_CRASH`
#   is untouched. Every new route is CANDIDATE; nothing is promoted.
#
# WHAT IS STILL UNSETTLED, PER SOURCE -- these routes are CANDIDATE and
# UNOBSERVED, and the questions below are open, not answered:
#   `0x17` / `0xAD` -- WHICH GAME VAR INDEX the single `GameVarSet` touches, and
#       whether any world's scripts READ that index. Decidable by decoding the
#       `GameVarSet` operand, which the Task 3 survey did not do. Until then a
#       foreign copy may be writing persistent state a world never expected.
#   `0x50` / `0xBC` -- WHAT A FOREIGN COFFIN ACTUALLY DROPS. Their resolved
#       closures name `0x2B` Gold_Key and `0x65`, both progression anchors, so
#       the drop question is design.md OPEN QUESTION 2 and cannot be settled in
#       memory. The anchors were REUSED, never injected (`inj` 0 in every
#       measured row), so the PORT is not what is unsafe -- the drop is.
#
# WHAT WAS LIFTED AFTERWARDS, IN PHASE 2, AND HAS SINCE BEEN MEASURED. `0x01`,
# `0x02`, `0x0A` (progression, Req 3.4) and the four Ordered_Puzzle_Props `0xAE`,
# `0xB5`, `0xC8`, `0xD3` (Req 3.3) were lifted too, on the same instruction, and
# each has its own record below. When those records were first written NONE of
# the seven had ever been measured, so Req 4.2 kept them unshippable. ALL 28 OF
# THEIR ROUTES HAVE NOW BEEN MEASURED -- 28 committed, `verify_session` green on
# every one, zero refusals -- so they now have 28 cost rows and 28 allowlist rows
# and they ship. See `_now_measured`, and note what it does NOT claim: the chain
# question and the script questions below are exactly as open as they were.
#
# WHAT IS STILL NOT LIFTED. `0x5F` stays in `STRUCTURE_PORT_EXCLUDED` -- its
# exclusion is MEASURED (GLOBAL assets, nothing to port), not policy, and
# evidence is not the user's to waive. `0x6C` Coin_Container's under->grave and under->swamp
# routes also stay REFUSED: those are EVIDENCE (surviving source-world texture
# ids, refusal S5), not policy, and evidence is not the user's to waive.
_GAMEVAR_SIGNATURE = "GameVarRead x3 + GameVarSet x1 (BEF script API slots 107/108)"

#: THE ONE CLAUSE THAT KEEPS THE PHASE-2 RECORDS HONEST.
#: Phase 1 (`0x17` / `0xAD` / `0x50` / `0xBC`) had all 16 of its routes MEASURED
#: and `verify_session` green before the hold was lifted, so those records could
#: cite numbers. The phase-2 seven had nothing of the kind when they were
#: written, and every one of their `still_unsettled` strings ended in a shared
#: clause saying so -- one wording, so the absence of evidence could not quietly
#: go missing from one of them.
#: THE PHASE-2 SEVEN HAVE NOW BEEN MEASURED, so that old `_NEVER_MEASURED`
#: clause -- "NO ROUTE OF THIS TYPE HAS EVER BEEN MEASURED ... CANNOT SHIP" --
#: became FALSE and is gone. What replaced it says what was actually measured,
#: in the same shared shape and for the same anti-drift reason, and it says in
#: the same breath what measurement does NOT settle: the per-prop chain question
#: and the per-source script question above are untouched by it.
#:
#: `{figures}` is the one hand-transcribed part, read from measurements.md's
#: "Phase-2 route costs" section. `roster` imports nothing from `roster_costs`
#: by design, so these are literals here and the tests pin them against the cost
#: table.
_MEASURED_FIGURES: dict[int, str] = {
    0x01: ("grave -> ice / swamp / castle / under at +83,928 .PRS / "
           "+36,896 .PRT / +2,624 .BEF each, 3 mesh entries / 2 anim records / "
           "2 textures, closure 0x9E NOT present in any target and so "
           "APPEND-ONLY INJECTED (inj 1) -- the only structure in the set that "
           "injects a companion at all"),
    0x02: ("grave -> ice / swamp / castle / under at +26,676 .PRS / "
           "+24,624 .PRT / +2,040 .BEF each, 3 mesh entries / 2 anim records / "
           "3 textures, companions 0x4B 0x67 both ALREADY PRESENT and REUSED "
           "(inj 0)"),
    0x0A: ("grave -> ice / swamp / castle / under at +93,847 .PRS / "
           "+35,872 .PRT / +1,272 .BEF each, 3 mesh entries / 2 anim records / "
           "2 textures, companion 0xEF REUSED (inj 0)"),
    0xAE: ("under -> grave / ice / swamp / castle at +146,852 .PRS / "
           "+17,424 .PRT / +2,088 .BEF each, 3 mesh entries / 3 anim records / "
           "1 texture, companions 0x4B 0x67 REUSED (inj 0)"),
    0xB5: ("swamp -> grave / ice / castle / under at +60,180 .PRS / "
           "+67,616 .PRT / +824 .BEF each, 3 mesh entries / 2 anim records / "
           "2 textures, companions measured as NONE"),
    0xC8: ("swamp -> grave / ice / castle / under at +140,896 .PRS / "
           "+21,552 .PRT / +2,024 .BEF each, 3 mesh entries / 2 anim records / "
           "3 textures, companions 0x4B 0x67 REUSED (inj 0)"),
    0xD3: ("swamp -> grave / ice / castle / under at +140,540 .PRS / "
           "+17,424 .PRT / +1,000 .BEF each, 2 mesh entries / 1 anim record / "
           "1 texture, companion 0xEF REUSED (inj 0)"),
}


def _now_measured(type_id: int) -> str:
    """The phase-2 measurement clause for one source, in one shared shape."""
    return (
        "and ALL 4 OF ITS ROUTES HAVE NOW BEEN MEASURED, which is what changed "
        "since this record was written: "
        + _MEASURED_FIGURES[type_id]
        + ". All 4 COMMITTED through the UNMODIFIED gated path, one route per "
          "child process, and verify_session green on every one, ZERO refusals "
          "-- so there are now 4 cost rows in "
          "roster_costs.STRUCTURE_ROUTE_COSTS and 4 rows in "
          "catalog.CROSS_WORLD_STRUCTURE_ALLOWLIST, and Req 4.2 no longer "
          "excludes the type. BUT MEASUREMENT DOES NOT ANSWER THE QUESTION "
          "ABOVE: it proves the assets port and the target archives verify, and "
          "nothing more -- every one of the 4 routes is CANDIDATE and "
          "UNOBSERVED, and only the user's in-game observation can promote one "
          "(Req 4.4)")

#: The phase-2 lift was a JUDGEMENT reversal, not a measurement result. Shared
#: `why_lifted` wording for all seven, for the same anti-drift reason.
_PHASE_TWO_WHY_LIFTED = (
    "what held it was this repo's JUDGEMENT about what a foreign copy might DO "
    "in game -- no measurement was ever run on it, so no measurement ever "
    "failed either; the user asked for these structures specifically, was told "
    "what is still open about each, and accepted the risk")

#: What `items.HAZARD_PROTECTED_AS_SOURCE` does and does not settle, repeated in
#: every Ordered_Puzzle_Prop record because it is the easiest thing to misread.
_CHAIN_TABLE_NOTE = (
    "items.HAZARD_PROTECTED_AS_SOURCE STILL CARRIES this id and MUST KEEP "
    "CARRYING it: that table governs the ITEM PASS -- it forbids RETYPING a "
    "level's OWN chain member -- which is a DIFFERENT question from whether "
    "the type may be ported IN to a foreign world as an ARRIVAL, and this lift "
    "answers only the second")

#: `{type_id: (what_was_held, why_lifted_is_defensible, still_unsettled)}` for
#: ALL ELEVEN sources whose POLICY hold was lifted -- the phase-1 four
#: (`0x17`, `0xAD`, `0x50`, `0xBC`, measured clean before the lift) and the
#: phase-2 seven (`0x01`, `0x02`, `0x0A`, `0xAE`, `0xB5`, `0xC8`, `0xD3`, lifted
#: UNMEASURED and MEASURED AFTERWARDS). This is a RECORD, not a
#: permission: permission comes from `roster_costs.STRUCTURE_ROUTE_COSTS` and
#: `catalog.CROSS_WORLD_STRUCTURE_ALLOWLIST` as it does for every other route.
#: The anchor table names are read out of the live `items` tables by
#: `_closure_anchor_note`, so they cannot drift (Req 2.5's habit, kept).
_STRUCTURE_HOLD_LIFTED_REASONS: dict[int, tuple[str, str, str]] = {
    0x17: (
        f"a POLICY hold on the {_GAMEVAR_SIGNATURE} persistent-state "
        f"signature -- byte-for-byte the same signature as survey control "
        f"0xC8 Spirit_Statue and as still-ineligible 0x02 Bonus_Grave",
        "its 4 routes were MEASURED and verify_session green (+183,452 .PRS / "
        "+68,640 .PRT / +1,880 .BEF, 3 meshes / 3 anims / 2 textures, "
        "companions 0x4B 0x67 both REUSED, inj 0), so nothing about the port "
        "failed; the user asked for it explicitly and accepted the risk",
        "WHICH GAME VAR INDEX the GameVarSet touches, and whether any world "
        "reads that index -- decidable by decoding the GameVarSet operand, "
        "which the Task 3 survey did not do",
    ),
    0xAD: (
        f"a POLICY hold on the same {_GAMEVAR_SIGNATURE} signature plus "
        f"Spawn x2, and on its being the castle sibling of 0xC8 "
        f"Spirit_Statue, whose ordered-chain evidence is recorded in "
        f"items.HAZARD_PROTECTED_AS_SOURCE",
        "its 4 routes were MEASURED and verify_session green (+112,564 .PRS / "
        "+37,936 .PRT / +1,928 .BEF, 3 meshes / 2 anims / 3 textures, "
        "companions 0x4B 0x67 both REUSED, inj 0); the user asked for it "
        "explicitly and accepted the risk",
        "the same GameVarSet operand decode -- which index, and whether any "
        "world reads it -- plus whether the castle torch joins a numbered "
        "chain the way its swamp sibling does, which needs the level .PSX "
        "records because no structure blob carries an instance-name literal",
    ),
    0x50: (
        "a POLICY hold because its resolved companion closure "
        "0x20 0x2B 0x45 0x4B 0x4E 0x51 0x65 0xEF NAMES {anchors}",
        "its 4 routes were MEASURED and verify_session green (+35,540 .PRS / "
        "+66,576 .PRT / +3,112 .BEF, 2 meshes / 1 anim / 1 texture) with "
        "EVERY companion REUSED and none injected (inj 0), so the port itself "
        "adds no anchor to any world; the user asked for it explicitly and "
        "accepted the risk",
        "WHAT A FOREIGN Standing_Coffin ACTUALLY DROPS -- design.md open "
        "question 2, unanswerable in memory and owed as an in-game "
        "observation",
    ),
    0xBC: (
        "a POLICY hold on the same closure reasoning as 0x50: its closure "
        "0x20 0x2B 0x45 0x4B 0x4E 0x51 0x65 is 0x50's minus 0xEF and NAMES "
        "the same {anchors}",
        "its 4 routes were MEASURED and verify_session green (+30,656 .PRS / "
        "+66,576 .PRT / +2,968 .BEF, 2 meshes / 1 anim / 1 texture), "
        "companions reused and none injected (inj 0); the user asked for it "
        "explicitly and accepted the risk",
        "WHAT A FOREIGN Smashable_Coffin ACTUALLY DROPS -- the same design.md "
        "open question 2, taken SEPARATELY from 0x50 because the two closures "
        "are not identical",
    ),
    # ---- PHASE 2: the seven lifted afterwards, on the same instruction. ----
    # These are recorded HERE, in the same table and the same shape, because a
    # source may not become portable without a record and these seven had none.
    # Read `_now_measured` before reading any of them: it carries what has since
    # been measured for each, and the standing caveat that measuring the assets
    # does not answer what the record says is still unsettled.
    0x01: (
        "a POLICY hold under Req 3.4 as a progression-relevant GRAVE "
        "structure: Bone Tower is a script-driven reveal, assembled/collapsed "
        "by the level's own script rather than existing as inert scenery, so a "
        "standalone copy in a foreign world has no script to drive it",
        _PHASE_TWO_WHY_LIFTED,
        "WHETHER A FOREIGN COPY EVER ASSEMBLES AT ALL -- the open question Req "
        "3.4 recorded is unchanged and unanswered: which script drives the "
        "assemble/collapse, and whether a world that never had a Bone Tower "
        "runs anything that would drive one; " + _now_measured(0x01),
    ),
    0x02: (
        "a POLICY hold under Req 3.4 as a progression-relevant GRAVE "
        "structure: Bonus Grave GATES BONUS ACCESS -- it is the entry point to "
        "a bonus area, so a copy in a foreign world either leads nowhere or "
        "offers a second entrance to content the run has already accounted for",
        _PHASE_TWO_WHY_LIFTED,
        "WHERE A FOREIGN BONUS GRAVE LEADS, and what the run does with a second "
        "entrance -- the Req 3.4 question, carried forward unanswered. It is "
        f"also the source measurements.md calibrated against: it uses "
        f"{_GAMEVAR_SIGNATURE}, the SAME persistent-state signature as 0x17 "
        f"Smashable_Snow_Pirate and 0xAD Spirit_Torch, so it inherits THEIR "
        f"open question too -- which game var index that GameVarSet touches, "
        f"and whether any world reads that index; " + _now_measured(0x02),
    ),
    0x0A: (
        "a POLICY hold under Req 3.4 as a progression-relevant GRAVE "
        "structure: Popup Headstone is a scripted reveal, popping up on a "
        "script cue instead of being present at load, so ported standalone it "
        "has no cue and may never appear at all",
        _PHASE_TWO_WHY_LIFTED,
        "WHETHER A FOREIGN COPY EVER POPS UP -- the Req 3.4 question verbatim "
        "and still open: nothing identifies the cue, so a ported copy may sit "
        "invisible for the whole level, which is a silent failure rather than a "
        "loud one; " + _now_measured(0x0A),
    ),
    0xAE: (
        "a POLICY hold under Req 3.3 as an Ordered_Puzzle_Prop, on the "
        "evidence recorded in items.HAZARD_PROTECTED_AS_SOURCE: these props "
        "form numbered chains (1st..9th) that a level script indexes in the "
        "S_SUB3 / U_SUB3 sequences -- 0xAE Underworld_Lantern is the U_SUB3 "
        "member -- and breaking the order leaves the level in INFINITE LOADING",
        _PHASE_TWO_WHY_LIFTED,
        "WHETHER A FOREIGN COPY JOINS OR DISTURBS THE TARGET WORLD'S CHAIN -- "
        "an arrival is a chain member's TYPE standing OUTSIDE any chain, and "
        "nothing measured says whether the indexing script ignores it or counts "
        "it; if it counts it, the recorded failure mode is infinite loading. "
        + _CHAIN_TABLE_NOTE + "; " + _now_measured(0xAE),
    ),
    0xB5: (
        "a POLICY hold under Req 3.3 as an Ordered_Puzzle_Prop, on the "
        "evidence recorded in items.HAZARD_PROTECTED_AS_SOURCE: these props "
        "form numbered chains (1st..9th) that a level script indexes in the "
        "S_SUB3 / U_SUB3 sequences -- 0xB5 Swamp_Coffin_Lid is an S_SUB3 "
        "member -- and breaking the order leaves the level in INFINITE LOADING",
        _PHASE_TWO_WHY_LIFTED,
        "WHETHER A FOREIGN COPY JOINS OR DISTURBS THE TARGET WORLD'S CHAIN -- "
        "unknown, and asked SEPARATELY per prop because each sits in a "
        "different sequence. " + _CHAIN_TABLE_NOTE + "; " + _now_measured(0xB5),
    ),
    0xC8: (
        "a POLICY hold under Req 3.3 as an Ordered_Puzzle_Prop, on the "
        "evidence recorded in items.HAZARD_PROTECTED_AS_SOURCE: these props "
        "form numbered chains (1st..9th) that a level script indexes in the "
        "S_SUB3 / U_SUB3 sequences -- 0xC8 Spirit_Statue is the S_SUB3 member "
        "measurements.md used as its known-bad CONTROL -- and breaking the "
        "order leaves the level in INFINITE LOADING",
        _PHASE_TWO_WHY_LIFTED,
        "WHETHER A FOREIGN COPY JOINS OR DISTURBS THE TARGET WORLD'S CHAIN, "
        "and separately what its persistent state does in a world that never "
        "had it -- this is the CONTROL whose signature got 0x17 and 0xAD held "
        "in the first place, so lifting it does not answer that question, it "
        "only stops the question blocking the port. " + _CHAIN_TABLE_NOTE
        + "; " + _now_measured(0xC8),
    ),
    0xD3: (
        "a POLICY hold under Req 3.3 as an Ordered_Puzzle_Prop, on the "
        "evidence recorded in items.HAZARD_PROTECTED_AS_SOURCE: these props "
        "form numbered chains (1st..9th) that a level script indexes in the "
        "S_SUB3 / U_SUB3 sequences -- 0xD3 Breakable_Tiki_Torch is an S_SUB3 "
        "member -- and breaking the order leaves the level in INFINITE LOADING",
        _PHASE_TWO_WHY_LIFTED,
        "WHETHER A FOREIGN COPY JOINS OR DISTURBS THE TARGET WORLD'S CHAIN -- "
        "open, and NOT settled by the script survey: measurements.md notes it "
        "calls GetEntity, which has arity 0 and returns the script's own "
        "entity, so it carries no information about chains either way. "
        + _CHAIN_TABLE_NOTE + "; " + _now_measured(0xD3),
    ),
}

#: The closure ids whose protection is what once disqualified `0x50` / `0xBC`.
_COFFIN_CLOSURE_ANCHORS: tuple[int, ...] = (0x2B, 0x65)


def _closure_anchor_note() -> str:
    """Name the anchor ids in the coffin closures AND their live tables.

    Requirement 2.5 in its strongest form: the reason must name BOTH the table
    that makes the type a structure and the table that protects it. Reading the
    labels back out of `items` means this sentence cannot drift from the tables
    it cites.
    """
    parts = []
    for anchor in _COFFIN_CLOSURE_ANCHORS:
        labels = _overlapping_tables(anchor) or ["no protection table"]
        parts.append(f"0x{anchor:02X} ({', '.join(labels)})")
    return " and ".join(parts)


def _hold_lifted_sources() -> dict[int, str]:
    """The ELEVEN sources whose POLICY hold was LIFTED, each with its own record.

    Deliberately NOT merged into `STRUCTURE_INELIGIBLE_SOURCES`: these are
    portable now, so an entry there would exclude them. This is the audit trail
    for why they became portable, in the same voice the exclusion reasons use,
    so a structure that misbehaves in game can be traced to the decision that
    admitted it (Req 8.4's habit applied to the decision itself).
    """
    out: dict[int, str] = {}
    names = structure_candidates()
    anchors = _closure_anchor_note()
    for type_id, (held, why_lifted, unsettled) in sorted(
            _STRUCTURE_HOLD_LIFTED_REASONS.items()):
        if type_id in STRUCTURE_PORT_EXCLUDED or type_id not in names:
            continue
        reason = (
            f"0x{type_id:02X} {names[type_id]}: HOLD LIFTED. It was "
            f"{held.format(anchors=anchors)}. THAT WAS POLICY, NOT "
            f"MEASUREMENT: {why_lifted}. It is now a portable SOURCE, and its "
            f"routes are CANDIDATE and UNOBSERVED -- no in-game observation "
            f"has been made, so nothing here is CONFIRMED (Req 4.4). STILL "
            f"UNSETTLED: {unsettled}. Destination protection is UNCHANGED by "
            f"this: PROTECTED_TAGS, PROTECTED_INSTANCE_NAMES, the end* "
            f"kill-groups and the Gold_Key / Level_Column / Skeleton_Key / "
            f"Monster_Generator anchors all still run in the item pass, and "
            f"items.HAZARD_PROTECTED_AS_SOURCE is untouched")
        also = _overlapping_tables(type_id)
        if also:
            reason += (f". NOTE it is ALSO declared by {', '.join(also)}, "
                       f"which this lift does NOT weaken -- that table still "
                       f"protects it as a DESTINATION")
        out[type_id] = reason
    return out


#: The eleven sources whose POLICY hold was lifted at the user's explicit
#: request, with what was held, why lifting it is defensible, and what remains
#: UNSETTLED. Reporting only -- membership here grants nothing; permission comes
#: from the cost and allowlist tables, and for the phase-2 seven the record now
#: states what was measured and what measurement still does not answer.
STRUCTURE_HOLD_LIFTED: dict[int, str] = _hold_lifted_sources()


def structure_hold_lifted_log_lines() -> list:
    """One `[cross-world]`-prefixed line per lifted POLICY hold (Req 8.2, 8.4).

    Same shape as `structure_exclusion_log_lines`, so a report can emit the
    lifted holds beside the exclusions and the reader sees both halves of the
    eligibility decision instead of only the refusals.
    """
    names = structure_candidates()
    return [f"[cross-world]   HOLD-LIFTED 0x{type_id:02X} "
            f"{names.get(type_id, _name_for(type_id)):<24} "
            f"{STRUCTURE_HOLD_LIFTED[type_id]}"
            for type_id in sorted(STRUCTURE_HOLD_LIFTED)]


def _ineligible_sources() -> dict[int, str]:
    """EMPTY. Every "not yet measured" hold has now been measured and lifted.

    THE TABLE AND ITS HELPER ARE KEPT DELIBERATELY, exactly as
    `roster_costs.STRUCTURE_WITHHELD_SOURCES` was kept: `structure_exclusion_reason`
    reads it, so keeping it preserves that function's shape, and a FUTURE hold --
    say a route the user observes misbehaving in game -- belongs here rather than
    in a newly invented symbol. An empty dict excludes nothing, which is the
    point.

    WHAT USED TO BE HERE, AND WHERE IT WENT.
      * the four Task-3 POLICY additions `0x17` / `0xAD` / `0x50` / `0xBC`:
        lifted earlier, recorded in `STRUCTURE_HOLD_LIFTED`;
      * the three progression grave structures `0x01` Bone_Tower / `0x02`
        Bonus_Grave / `0x0A` Popup_Headstone (Req 3.4);
      * the four Ordered_Puzzle_Props `0xAE` / `0xB5` / `0xC8` / `0xD3`
        (Req 3.3).
    All eleven now carry their own HOLD-LIFTED record in `STRUCTURE_HOLD_LIFTED`
    naming what is STILL UNSETTLED. The two halves are NOT equivalent, and the
    records say so: the phase-1 four were measured clean first, while the seven
    above have NEVER BEEN MEASURED -- no route, no cost row, no allowlist row.
    Requirement 3.3 / 3.4 said "INELIGIBLE UNTIL EACH IS INDIVIDUALLY MEASURED",
    and for the seven that measurement is still OWED; what this empty table means
    is only that the hold is no longer enforced HERE. Req 4.2 is what still keeps
    them from shipping.

    THE SUBTLE PART, AND THE ONE THING NOT TO GET WRONG.
      The four Ordered_Puzzle_Props were derived from
      `items.HAZARD_PROTECTED_AS_SOURCE` (see `_ORDERED_PUZZLE_PROP_IDS`), and
      that table is UNCHANGED and MUST STAY unchanged -- it still contains
      `0xAE`, `0xB5`, `0xC8`, `0xD3`. Membership there answers a DIFFERENT
      question from the one this table answers:

        `items.HAZARD_PROTECTED_AS_SOURCE`  ->  "the ITEM PASS may not RETYPE a
            level's OWN instance of this type into something else." That is what
            keeps the existing numbered S_SUB3 / U_SUB3 chains intact, and it
            keeps running, per destination, exactly as before.

        this table                          ->  "this type may not be the SOURCE
            of a cross-world PORT." That is about a NEW ARRIVAL in a foreign
            world, which cannot disturb any existing chain because it is not a
            member of one.

      So dropping the four from HERE does not weaken the protection THERE. What
      it does mean is honestly worse in a different way, and is recorded per
      prop below: a ported copy is a chain member's TYPE standing OUTSIDE any
      chain, and the recorded failure mode for a broken chain order is INFINITE
      LOADING. The user was told that and asked for it anyway.

      `0x5F` Monster_Generator is untouched by all of this: it stays in
      `STRUCTURE_PORT_EXCLUDED` for its measured reason (GLOBAL assets, nothing
      to port), and `structure_exclusion_reason` checks that table FIRST.
    """
    return {}


#: Requirements 3.3 / 3.4. INELIGIBLE AS SOURCES until each is individually
#: measured. Unlike `STRUCTURE_PORT_EXCLUDED` this is a "not yet", not a "never",
#: and it has already shrunk once: the four Task-3 POLICY additions (`0x17`,
#: `0xAD`, `0x50`, `0xBC`) were lifted out of it -- see `STRUCTURE_HOLD_LIFTED`.
#: What remains is the three progression grave structures (`0x01`, `0x02`,
#: `0x0A`) and the four Ordered_Puzzle_Props (`0xAE`, `0xB5`, `0xC8`, `0xD3`):
#: SEVEN entries, none of which has a measured cost row either.
STRUCTURE_INELIGIBLE_SOURCES: dict[int, str] = _ineligible_sources()


def structure_exclusion_reason(type_id: int) -> _Optional[str]:
    """Why this structure may not be a port SOURCE, or None if it may.

    Order matters and mirrors the requirements: the outright exclusion
    (Req 2.4) is checked before the not-yet-measured ineligibility (Req 3.3,
    3.4), and a type absent from `STRUCTURE_TYPES` is refused for not being a
    candidate at all (Req 2.1).
    """
    type_id = int(type_id)
    if type_id in STRUCTURE_PORT_EXCLUDED:
        return STRUCTURE_PORT_EXCLUDED[type_id]
    if type_id in STRUCTURE_INELIGIBLE_SOURCES:
        return STRUCTURE_INELIGIBLE_SOURCES[type_id]
    if type_id not in spawn_config.STRUCTURE_TYPES:
        return (f"0x{type_id:02X} is not declared by "
                f"spawn_config.STRUCTURE_TYPES, so it is not a structure "
                f"candidate (Req 2.1)")
    return None


def portable_structure_sources() -> dict[int, str]:
    """Candidates that MAY be measured as sources: candidates minus exclusions.

    This is a candidacy set, not a permission: Requirement 4.2 still refuses any
    route without a measured cost row, and Task 4 supplies those rows.
    """
    return {type_id: name for type_id, name in structure_candidates().items()
            if structure_exclusion_reason(type_id) is None}


def structure_kind(type_id: int) -> _Optional[str]:
    """`STRUCTURE` for a declared structure type, else None.

    This is the SECOND view Requirement 2.2 asks for. It does not and must not
    replace `RosterEntry.kind`: `classify_roster` keeps calling these types
    `non_enemy` (Requirement 2.3), which is what stops the enemy draw ever
    selecting one. A caller wanting "is this a structure" asks here; a caller
    filling an enemy slot asks the roster and gets `non_enemy`.
    """
    return STRUCTURE if int(type_id) in spawn_config.STRUCTURE_TYPES else None


def structure_classification(roster: Mapping) -> dict[int, str]:
    """`{type_id: STRUCTURE}` for the structure types a roster contains.

    A REPORTING view over an existing roster. It reads `roster` and returns a new
    dict; it never mutates an entry, so the ENEMY draw sees the same `non_enemy`
    verdicts it saw before this feature existed (Requirement 2.3).
    """
    return {type_id: STRUCTURE for type_id in sorted(roster)
            if structure_kind(type_id) is not None}


def structure_exclusion_log_lines() -> list:
    """One `[cross-world]`-prefixed line per excluded / ineligible structure.

    Mirrors `excluded_log_lines`' shape so Task 5's report can emit both blocks
    the same way (Requirement 8.1).
    """
    names = structure_candidates()
    lines = []
    for table, verdict in ((STRUCTURE_PORT_EXCLUDED, "EXCLUDE"),
                           (STRUCTURE_INELIGIBLE_SOURCES, "INELIGIBLE")):
        for type_id in sorted(table):
            lines.append(
                f"[cross-world]   {verdict} 0x{type_id:02X} "
                f"{names.get(type_id, _name_for(type_id)):<24} {table[type_id]}")
    return lines
