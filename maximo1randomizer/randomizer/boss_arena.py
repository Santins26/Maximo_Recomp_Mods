"""CROSS-WORLD ENEMY ARRIVALS INTO THE BOSS ARENAS. Opt-in, default OFF.

WHAT THIS IS
    A boss arena is a `.PSX` the randomizer has always SKIPPED outright
    (`cli.BOSS_FILES`, refused in two places: the template-harvest loop and the
    per-file loop) and an archive trio the porter has never opened as a TARGET.
    This module makes the four `_B` arenas porting targets for FOREIGN ENEMIES
    and retypes a small, proven-decorative set of arena records into the
    arrivals that COMMITTED.

    It ports NOTHING ITSELF. `cross_world.port_enemy_route` is called with a
    `WorldPortSession` opened on the ARENA's archive stem, so the source
    mesh-table walk, the target mesh trust gate, the anim trust gate and its rig
    bind check, the texture remap, the append-only BEF injection and
    `verify_session` all run UNMODIFIED. A gate refusal refuses THAT ROUTE and
    nothing else.

WHICH ARENAS, AND WHY THE QUEEN IS STILL OUT
    Each of the four `_B` arenas ships its OWN archive trio, CONFIRMED present
    in `game_files/`: `GRAVE_B`, `SWAMP_B`, `ICE_B`, `UNDER_B`, each with
    `.PRS` / `.PRT` / `.BEF`. `boss_inject` already reads and writes the
    `GRAVE_B` trio, so touching an arena archive is a path with mileage on it.

    CASTLE'S KING ARENA IS NOW SERVED TOO, as `C_KING.PSX` / `CASTLE_K`. It was
    excluded on two grounds and both were retired by measurement, not by
    argument:
      * ASSETS. `CASTLE_K.PRS` and `CASTLE_Q.PRS` are SKA-first archives, so the
        old `TFA\xff`-derived anim anchor landed 160-286 KB inside their own
        tables and BOTH failed the mesh and anim trust gates. The anchor now
        comes from the mesh walk's end, and both archives PASS both gates.
      * VICTIMS. The old note said `boss_inject` had rejected the `Fire` class
        because measured `gate1_torch` / `gatetorch*` / `endcave_torch*`
        instances carry scripted-looking names. That is a fact about torches
        ELSEWHERE in the game; `C_KING.PSX`'s eight are plainly numbered
        `Fire_13039`..`Fire_13046`, `items.BOSS_DUPLICATION` already vets them,
        and shipped `duplicate_boss` already recycles these exact records for
        castle boss clones. At 744 B they also fit all 21 melee types.
    Routes were never the obstacle: 8 active allowlist rows target `castle`,
    each with a measured `roster_costs` row and none recording a group refusal.

    `C_QUEEN.PSX` REMAINS EXCLUDED, and cannot be fixed by choosing better
    victims: the whole file is NINE records -- one `Queen` and eight
    `QueenWaypoint` path nodes. There is no cosmetic record in it to recycle,
    and the waypoints are steered by the fight. See `EXCLUDED_ARENAS`.

THE THREE THINGS THAT ARE NOT MEASURED, SAID OUT LOUD
    1. ROUTE COST INTO AN ARENA IS A PREDICTION. See `predicted_cost`.
    2. THE ARENA BUDGET IS UNVERIFIED ON CONSOLE. See `arena_budget`.
    3. NOBODY HAS PLAYED AN ARENA WITH AN ARRIVAL IN IT. Every route stays
       CANDIDATE; nothing here promotes anything to CONFIRMED and the
       permission oracle (`catalog.CROSS_WORLD_ALLOWLIST` via
       `catalog.load_allowlist` -> `classify_placement`) is never widened.

WHAT IS NEVER DONE HERE
    The arena is NOT opened to general randomization. Boss death and victory
    triggers are keyed on INSTANCE NAMES, and retyping a referenced record is
    the recorded infinite-loading / unwinnable-arena failure. Victims are drawn
    EXCLUSIVELY from that arena's `items.BOSS_DUPLICATION["victim_names"]`, and
    every existing protection runs ON TOP of that narrowing (see
    `eligible_victims`).

    No file is written from this module. `port_arena_arrivals` returns buffers
    keyed by filename, exactly as `WorldPortSession.finish()` does, and the
    caller drops them into the build's own `output/`.
"""
from __future__ import annotations

import struct as _struct
from dataclasses import dataclass, field
from pathlib import Path

from . import catalog as _catalog
from . import items as _items
from . import roster_costs as _roster_costs
from . import roster_selection as _sel

#: Asset messages carry this prefix, the same one every other cross-world
#: message uses. `ELF patch:` is reserved for executable patches and is NEVER
#: used from here -- this module patches no instruction.
LOG_PREFIX = "[cross-world]"

#: At most FOUR binaries live per arena, and this is how it is batched: the
#: arena's own `.PRS` + `.PRT` + `.BEF` are held by the one session (three), and
#: a route's SOURCE `.BEF` is parsed and dropped INSIDE
#: `cross_world.port_enemy_route` (the fourth, transient). Arenas are walked ONE
#: AT A TIME and each session is finished and released before the next opens, so
#: there is never more than one `.PRS` resident.
MAX_BINARIES_PER_ARENA = 4

#: HOW MANY ENEMIES ACTUALLY SPAWN IN ONE ARENA, BY DIFFICULTY. THESE ARE THE
#: USER'S CHOSEN FIGURES, NOT MEASURED LIMITS -- no arena has ever been played
#: with more than three arrivals in it, so `normal` and `hard` are BOTH less
#: tested than `easy`.
#:
#: THIS IS A COUNT OF SPAWNED ENEMIES, NOT OF PORTED TYPES. The two used to be
#: the same number because each committed route consumed exactly one victim slot,
#: which made `hard` mean "nine different enemies ported" -- nine times the
#: archive bytes, into a budget that has never been verified on console. It now
#: means "nine enemies standing in the arena", and the same ported type may fill
#: several slots. THAT REPETITION IS FREE: a second `Snowman` costs one more
#: `.PSX` record and NOT ONE BYTE of `.PRS` / `.PRT` / `.BEF`, because the mesh,
#: anim and texture data it needs is already resident from the first one.
#:
#: WHAT ACTUALLY LIMITS EACH ARENA, AND WHY `hard` CANNOT ALWAYS DELIVER 9. Every
#: spawn needs its own VICTIM SLOT, and the slots are counted from each arena's
#: vetted decorative records (MEASURED against the real `.PSX` files):
#:
#:     G_BOSS / GRAVE_B    4 slots (`Entity_Light`, 680 B)
#:     S_BOSS / SWAMP_B    6 slots (`Fire_Flies`,   840 B)
#:     C_KING / CASTLE_K   8 slots (`Fire`,         744 B)
#:     I_BOSS / ICE_B      9 slots (`bubbles`,      936 B)
#:     U_BOSS / UNDER_B   10 slots (`Steam_Vent`,   968 B)
#:
#: So the SLOT COUNT is the ceiling on spawns: GRAVE can never exceed 4 and SWAMP
#: never 6, whatever the difficulty says. A slot also has a SIZE: a template
#: larger than the slot is refused for it (the arena `.PSX` is never grown), and
#: GRAVE's 680 B ceiling fits only 8 of the 21 melee types. The per-arena BYTE
#: BUDGET and the allowlisted route count bound how many DISTINCT types get
#: ported, not how many enemies spawn; both already log when they bind.
ARENA_DIFFICULTY_ARRIVALS: dict = {
    "easy": 3,
    "normal": 6,
    "hard": 9,
}

#: The difficulty used when the caller names none.
ARENA_DIFFICULTY_DEFAULT = "normal"

#: HOW MANY DISTINCT ENEMY TYPES ARE PORTED INTO ONE ARENA. ALWAYS THREE, AND
#: DIFFICULTY DOES NOT MOVE IT.
#:
#: This is the ONLY figure in this feature with any testing behind it, and it is
#: the one that spends archive bytes. Porting is what grows `.PRS` / `.PRT` /
#: `.BEF` against a per-arena byte budget that HAS NEVER BEEN VERIFIED ON
#: CONSOLE, so letting `hard` triple it would triple the risk of the one failure
#: mode nobody can see coming (an arena that will not load is an unwinnable
#: game). Difficulty raises the number of enemies STANDING IN THE ARENA instead,
#: by reusing these three -- which costs `.PSX` records only and not one archive
#: byte, because the assets are already resident.
MAX_PORTED_TYPES_PER_ARENA = 3

#: Historical name for the figure above, and it means exactly what it always
#: meant: how many routes the admission walk commits. Kept so nothing that reads
#: it changes behaviour.
MAX_ARRIVALS_PER_ARENA = MAX_PORTED_TYPES_PER_ARENA


def arrivals_for_difficulty(label) -> int:
    """`easy` / `normal` / `hard` -> how many enemies SPAWN per arena. Unknown or
    `None` -> the default, so a stale settings profile degrades to `normal` and
    never to zero arrivals (which would look exactly like the feature being
    broken)."""
    if label is None:
        return MAX_ARRIVALS_PER_ARENA
    return ARENA_DIFFICULTY_ARRIVALS.get(str(label).strip().lower(),
                                        MAX_ARRIVALS_PER_ARENA)


@dataclass(frozen=True)
class ArenaSpec:
    """One arena: its `.PSX`, the WORLD it belongs to, its archive stem and the
    ONLY records it may recycle.

    `append_carriers` and `boss_type` serve the APPEND-MODE arenas only (see
    `ARENA_APPEND_ARENAS`); a RETYPE-mode arena leaves both at their defaults and
    behaves exactly as it always has.
    """

    psx_name: str
    world: str
    stem: str
    victim_names: tuple
    append_carriers: tuple = ()
    boss_type: int | None = None

    @property
    def label(self) -> str:
        return f"{self.psx_name} / {self.stem}"

    @property
    def append_mode(self) -> bool:
        """True when this arena GAINS records instead of recycling them."""
        return bool(self.append_carriers) and not self.victim_names


#: THE FOUR `_B` ARENAS. `world` and `victim_names` are READ FROM the existing
#: tables (`items.BOSS_FILE_WORLD`, `items.BOSS_DUPLICATION`) rather than
#: restated, so a change there moves this with no second edit. The archive stem
#: is the one new fact, and it is the `_B` naming the disc uses.
_ARENA_STEMS: dict = {
    "G_BOSS.PSX": "GRAVE_B",
    "S_BOSS.PSX": "SWAMP_B",
    "I_BOSS.PSX": "ICE_B",
    "U_BOSS.PSX": "UNDER_B",
    # CASTLE'S KING ARENA. Not a `_B` trio -- the disc spells it `CASTLE_K` --
    # which is why the stem is listed rather than derived. Served now that its
    # two blockers are gone, both MEASURED rather than argued:
    #   * ASSETS. `CASTLE_K.PRS` is SKA-first, so the old `TFA\xff` anchor put
    #     its anim region 160 KB inside its own table and it FAILED both trust
    #     gates. With the anchor taken from the mesh walk's end it PASSES the
    #     mesh gate and the anim gate, and declares 43 anim records.
    #   * VICTIMS. Its 8 `Fire` torches are 744 B, which fits all 21 melee
    #     types, and `items.BOSS_DUPLICATION` already names them -- shipped
    #     `duplicate_boss` recycles these exact records for castle boss clones.
    # Routes exist too: 8 active allowlist rows target `castle`, every one with
    # a measured cost row and none recording a group refusal.
    "C_KING.PSX": "CASTLE_K",
    # CASTLE'S QUEEN ARENA, served in APPEND MODE because it has nothing to
    # recycle. See `ARENA_APPEND_ARENAS`. Its archive also only passes the trust
    # gates because of the SKA-first anchor fix: `CASTLE_Q.PRS` declares 10 anim
    # records and used to fail both gates.
    "C_QUEEN.PSX": "CASTLE_Q",
}

#: WHERE THIS FEATURE NEEDS A DIFFERENT VICTIM SET THAN BOSS DUPLICATION, AND
#: WHY THE TABLES CANNOT SIMPLY BE SHARED.
#:
#: `items.BOSS_DUPLICATION["victim_names"]` was chosen for `duplicate_boss`,
#: whose replacement record is a BOSS -- 0x228 (552 B) in every arena. This
#: feature writes an ENEMY record instead, and MEASURED across every non-arena
#: `.PSX` on the disc the melee-enemy records run 584-808 B, so the SMALLEST
#: enemy on the disc is 584 B -- 32 B LARGER than a boss record. The size floor
#: is therefore genuinely different between the two features, and a victim set
#: that is exactly right for one can be unusable for the other.
#:
#: `I_BOSS.PSX` is where that bites. Its eight `Sea_Weed` victims are 552 B --
#: picked precisely BECAUSE that "== the boss record size, so the overwrite is
#: byte-budget-exact" (`items.BOSS_DUPLICATION`). MEASURED: that cap fits
#: 0 of 21 melee types, so `plan_arena_retypes` skipped EVERY arrival for size
#: and the ice arena could never place anything on any seed, while the assets
#: still landed in `ICE_B.PRS` / `.PRT` / `.BEF`.
#:
#: The replacement is `bubbles` (type 0x3B, NINE records, 936 B each), which
#: fits 21 of 21 melee types. It is the only usable alternative in the file, and
#: the rest of `I_BOSS.PSX`'s >= 584 B records are all records to leave alone:
#: `Boss_Camera` (0x33), the three `Ice_Boss_Grate` (0x77, a boss mechanic), the
#: `grill*` hazards (0xCE) and one scripted `killer_whale` (0x11). `bubbles`
#: carries no hazard type, no protected name, no progression anchor and no
#: event tie (all four checked against the live file, not assumed).
#:
#: ALL NINE SHARE THE INSTANCE NAME `bubbles`, which is why one entry covers the
#: set: `eligible_victims` matches on the instance name, so this admits all nine
#: as candidates and the seeded pairing picks among them.
#:
#: `items.BOSS_DUPLICATION` IS DELIBERATELY NOT EDITED. It is what
#: `--duplicate-bosses` reads, and `BOSS_CLONE_MAX` is derived from the length of
#: its victim lists, so changing it there would move a shipped feature's clone
#: ceiling and its recycled records as a side effect of fixing this one. An
#: arena with no entry here keeps reading `BOSS_DUPLICATION` exactly as before.
ARENA_VICTIM_OVERRIDES: dict = {
    "I_BOSS.PSX": ("bubbles",),
}

#: Why each arena the project knows about but this feature does not serve is
#: OUT. Logged, so the exclusion is visible in a build rather than implied by
#: absence.
#: ARENAS THAT GAIN RECORDS INSTEAD OF RECYCLING THEM, and why that mode exists.
#:
#: `C_QUEEN.PSX` holds NINE records in total, MEASURED off the file: one `Queen`
#: (0x9D, 552 B) and eight `QueenWaypoint` (0x8A, 680 B) path nodes. There is no
#: cosmetic record in it. Every other arena works by RETYPING a proven-decorative
#: record, and the queen has none to retype -- the only records big enough are the
#: waypoints her fight steers along, and retyping one of those is the recorded
#: unwinnable-arena failure.
#:
#: So she is served the other way round: NOTHING IS RETYPED. An arrival's record
#: is APPENDED immediately after a CARRIER record, and the carrier itself is
#: copied through BYTE-FOR-BYTE. The waypoint keeps its offset-in-order, its
#: bytes, its name and its properties; the arena simply ends up with more records
#: than it shipped with.
#:
#: WHY THAT IS SOUND, and the one thing about it that is NOT verified:
#:   * the records section is allowed to grow. `psx.RECORDS_SECTION_SIZE_CEILING`
#:     is None by default, `_build_replacement_bytes` already handles a
#:     replacement of a DIFFERENT size, and it patches the post-records SIZE
#:     field for growth and shrink alike. Shipped `items.duplicate_boss` already
#:     changes that size (it writes a 0x228 boss over a larger victim), so the
#:     field's semantics are exercised in real builds.
#:   * the record COUNT field must be bumped, and this is the FIRST THING IN THE
#:     PROJECT THAT ADDS A RECORD, so it is the first thing that needs it.
#:     `psx.py` says so itself, at the point where it declines to write it: "if a
#:     future change ever adds or removes records, this field becomes
#:     load-bearing". `plan_arena_appends` returns the patch for it and
#:     `verify_arena_appends` re-parses the assembled bytes and refuses unless
#:     the count, the carrier bytes and the appended records all agree.
#:   * NOT VERIFIED ON CONSOLE. Nobody has loaded a `.PSX` with an appended
#:     record. The buffer-level proof is strong and the failure mode is expected
#:     to be "the added enemy is not there", but an arena that will not load is
#:     an unwinnable game, so treat a queen build as an experiment.
ARENA_APPEND_ARENAS: dict = {
    "C_QUEEN.PSX": {
        "world": "castle",
        "stem": "CASTLE_Q",
        # Carriers are the records the new ones are appended AFTER. They are
        # NEVER modified -- naming them here does not put them at risk, it only
        # says where in the section the new bytes land.
        "carrier_names": ("QueenWaypoint_12985", "QueenWaypoint_12986",
                          "QueenWaypoint_12987", "QueenWaypoint_12988",
                          "QueenWaypoint_12989", "QueenWaypoint_12991",
                          "QueenWaypoint_12992", "QueenWaypoint_12993"),
        # Queen (Malevolent Queen). Used for the position ring, the same way
        # `items.duplicate_boss` mirrors across the arena centre when a row has
        # no explicit `clone_positions` -- and this row deliberately has none,
        # because no floor coordinate in this arena has ever been measured.
        "boss_type": 0x9D,
    },
}

EXCLUDED_ARENAS: dict = {
    # THE QUEEN ARENA CANNOT BE SERVED, AND THIS IS A MEASUREMENT, NOT A
    # PRECAUTION. `C_QUEEN.PSX` holds NINE RECORDS IN TOTAL, read off the real
    # file: one `Queen` (0x9D, 552 B) and eight `QueenWaypoint` (0x8A, 680 B).
    # That is the boss plus her flight path, and NOTHING ELSE -- there is no
    # light, no ambience, no prop, not one cosmetic record in the file to
    # recycle.
    #
    # The waypoints are the only records big enough to hold an enemy, and they
    # are precisely what must never be retyped: they are referenced BY INSTANCE
    # NAME by the fight that steers along them, and retyping a referenced record
    # is the recorded infinite-loading / unwinnable-arena failure this whole
    # module is built to avoid. Spending the Queen's path to gain three enemies
    # trades a finishable game for a cosmetic win.
    #
    # THE QUEEN IS NO LONGER HERE. She is served in APPEND MODE instead --
    # nothing in her arena is retyped, records are ADDED after a byte-identical
    # carrier. See `ARENA_APPEND_ARENAS` for what that costs and what about it is
    # still unverified. This dict is now empty, and that is the honest state:
    # every arena the project knows about is served one way or the other.
}


def arena_specs(game_files_dir) -> tuple:
    """`(enabled, excluded)` -- the arenas this build can serve, and why the
    rest cannot. An arena is ENABLED only when all three of its archives and
    its `.PSX` are present on disc AND it has a measured victim set."""
    game_files_dir = Path(game_files_dir)
    enabled: list = []
    excluded: list = list(EXCLUDED_ARENAS.items())
    for psx_name, stem in sorted(_ARENA_STEMS.items()):
        append_cfg = ARENA_APPEND_ARENAS.get(psx_name) or {}
        world = (_items.BOSS_FILE_WORLD.get(psx_name)
                 or append_cfg.get("world"))
        cfg = _items.BOSS_DUPLICATION.get(psx_name) or {}
        # The ENEMY-record size floor is higher than the boss-record one, so an
        # arena may need its own victim set here. See ARENA_VICTIM_OVERRIDES;
        # an arena without an override reads BOSS_DUPLICATION unchanged.
        victims = tuple(ARENA_VICTIM_OVERRIDES.get(psx_name)
                        or cfg.get("victim_names", ()))
        carriers = tuple(append_cfg.get("carrier_names", ()))
        # An APPEND-MODE arena has no victims BY DESIGN and must not be excluded
        # for lacking them -- carriers are what it needs instead.
        if not world or not (victims or carriers):
            excluded.append((psx_name, "no BOSS_FILE_WORLD / victim_names row "
                                       "and no append carriers"))
            continue
        missing = [f"{stem}.{ext}" for ext in ("PRS", "PRT", "BEF")
                   if not (game_files_dir / f"{stem}.{ext}").is_file()]
        if missing:
            excluded.append((psx_name,
                             f"archive(s) absent from game_files: "
                             f"{', '.join(missing)}"))
            continue
        enabled.append(ArenaSpec(psx_name=psx_name, world=world, stem=stem,
                                 victim_names=victims,
                                 append_carriers=carriers,
                                 boss_type=append_cfg.get("boss_type")))
    return tuple(enabled), tuple(excluded)


# ---------------------------------------------------------------------------
# COSTS AND BUDGET -- both are PREDICTIONS, and neither pretends otherwise
# ---------------------------------------------------------------------------

def predicted_cost(type_id: int, source_world: str, arena_world: str):
    """The PREDICTED resident cost of one route into an arena, or `None`.

    THIS IS AN APPROXIMATION, NOT A MEASUREMENT, AND IT IS THE ONLY ONE IN THIS
    FEATURE. `roster_costs.MEASURED_TARGETS` covers grave / ice / swamp /
    castle / under and NOTHING ELSE: there are ZERO measured routes into any
    `_B` archive, so no row exists for `(enemy, source, ARENA)` and none is
    invented. What is used instead is the enemy's EXISTING MEASURED row into
    the arena's own WORLD, read straight out of `roster_costs.cost_for`.

    WHY THAT IS A DEFENSIBLE PREDICTION. A route's bytes are dominated by the
    SOURCE enemy's mesh / anim / texture data, and prior measurement showed
    that this is a property of the SOURCE and not of the target: the `.PRS` and
    `.PRT` deltas came out BYTE-IDENTICAL across several targets (see the ICE
    and UNDER blocks of `roster_costs`, and the structure table's explicit
    "COSTS ARE PER SOURCE, NOT PER TARGET, AND THAT IS A MEASUREMENT"), while
    only the `.BEF` delta moved with what the target already declares -- and
    `.BEF` is not what the RESOURCE pool bounds.

    WHY IT IS SAFE TO BE WRONG. The prediction is used for ORDERING and for the
    admission decision only. The REAL bound is the LIVE session size
    `len(session.prs) + len(session.prt)`, which is read BEFORE EVERY
    admission, so a route that turns out to cost more than predicted is
    absorbed by the NEXT decision rather than by the pool. A missing row is an
    EXCLUSION, never an estimate.

    NOTHING IS WRITTEN BACK. No row is copied into `roster_costs.ROUTE_COSTS`
    as though it were measured, and no arena is added to `MEASURED_TARGETS`.
    """
    return _roster_costs.cost_for(int(type_id), source_world, arena_world)


def arena_resident_bytes(spec: ArenaSpec, game_files_dir) -> int:
    """The arena's VANILLA `.PRS` + `.PRT` resident size, READ FROM DISC.

    Read with `stat()` rather than hardcoded, for the same reason the world
    baselines in `roster_costs` are asserted per process: a different disc
    revision must move this figure by itself, and a stale literal is a budget
    that is silently wrong.
    """
    game_files_dir = Path(game_files_dir)
    return ((game_files_dir / f"{spec.stem}.PRS").stat().st_size
            + (game_files_dir / f"{spec.stem}.PRT").stat().st_size)


def arena_budget(*, raised: bool, pool_size, max_pct) -> int:
    """The arena's byte budget, derived EXACTLY the way a world's is.

    Same function, same pool, same occupancy: `roster_selection.pool_budget`.
    The ONLY difference is what it is judged against -- the ARENA's own vanilla
    `.PRS` + `.PRT` resident size (`arena_resident_bytes`) instead of the
    world's, because an arena loads its own archive trio and not the world's.

    THIS BUDGET IS UNVERIFIED ON CONSOLE. No arena has ever been built with an
    arrival in it, so no occupancy figure for an arena has ever been observed
    to load or to crash. Arena archives are SMALLER than world archives, so the
    headroom is EXPECTED to be larger and more arrivals are EXPECTED to fit --
    but expected is not measured, and the failure mode of an over-full pool is
    SILENT (no log line, no build failure, the game dies at level load). Treat a
    build with arena arrivals as an experiment and record what it does.
    """
    return _sel.pool_budget(raised=bool(raised), pool_size=pool_size,
                            max_pct=max_pct)


# ---------------------------------------------------------------------------
# THE DRAW
# ---------------------------------------------------------------------------

@dataclass
class ArenaResult:
    """What one arena got. `committed` is `(type_id, source_world, name)`."""

    spec: ArenaSpec
    budget: int
    baseline_bytes: int
    committed: list = field(default_factory=list)
    skipped_budget: list = field(default_factory=list)
    refused_gate: list = field(default_factory=list)
    excluded_rule: list = field(default_factory=list)
    verified: bool = True
    verify_failures: tuple = ()

    @property
    def committed_types(self) -> set:
        return {int(t) for t, _s, _n in self.committed}


def _candidates(spec: ArenaSpec, allowlist: dict) -> tuple:
    """Every route the PERMISSION ORACLE already allows into this arena's
    world, paired with its PREDICTED cost. Permission is not widened by one
    row: the candidate set is a subset of `catalog.load_allowlist()`, whose
    entries are exactly `CROSS_WORLD_ALLOWLIST` rows with `active=True`.
    """
    out = []
    for entry in allowlist.values():
        if entry.target_world != spec.world:
            continue
        row = predicted_cost(entry.enemy_id, entry.source_world, spec.world)
        if row is None:
            out.append((entry, None))
            continue
        out.append((entry, row))
    out.sort(key=lambda pair: (pair[0].enemy_id, pair[0].source_world))
    return tuple(out)


def port_arena_arrivals(*, game_files_dir, seed, worlds, allowlist,
                        raised_pool=False, pool_size=None, max_pct=None,
                        max_types=None, log=None) -> tuple:
    """Port foreign enemies into every ENABLED arena. Returns
    `({filename: bytes}, {psx_name: ArenaResult})`.

    ONE arena at a time, ONE session each, finished and released before the
    next opens (`MAX_BINARIES_PER_ARENA`). Nothing is written to disk here.

    HOW MANY TYPES THIS PORTS IS FIXED AT `MAX_PORTED_TYPES_PER_ARENA` (3) AND
    DIFFICULTY IS NOT AN INPUT HERE. Difficulty decides how many enemies SPAWN,
    which is `plan_arena_retypes`' business; this function decides what gets
    ported, which is what spends archive bytes. `max_types` overrides the figure
    for a caller that needs to.
    """
    from . import cross_world as _cw

    emit = log if log is not None else (lambda line: None)
    game_files_dir = Path(game_files_dir)
    wanted = {str(w) for w in (worlds or ())}
    cap = max(0, int(max_types) if max_types is not None
              else MAX_PORTED_TYPES_PER_ARENA)
    emit(f"{LOG_PREFIX} arena injection: {cap} DISTINCT enemy type(s) per arena, "
         f"and difficulty does NOT change this. Porting is what grows the arena "
         f"archives against a byte budget that is UNVERIFIED ON CONSOLE, so the "
         f"type count stays at the one figure with testing behind it. The "
         f"difficulty instead sets how many ENEMIES SPAWN, by reusing these "
         f"types across the arena's decorative slots -- which costs `.PSX` "
         f"records only and not one archive byte")

    enabled, excluded = arena_specs(game_files_dir)
    for psx_name, why in excluded:
        emit(f"{LOG_PREFIX} arena {psx_name} EXCLUDED BY RULE: {why}")

    merged: dict = {}
    results: dict = {}

    for spec in enabled:
        if wanted and spec.world not in wanted:
            emit(f"{LOG_PREFIX} arena {spec.label} SKIPPED: world "
                 f"'{spec.world}' is not in this build's selected set")
            continue

        budget = arena_budget(raised=raised_pool, pool_size=pool_size,
                              max_pct=max_pct)
        baseline = arena_resident_bytes(spec, game_files_dir)
        pct = (100.0 * baseline / budget) if budget else 0.0
        emit(f"{LOG_PREFIX} arena {spec.label} [{spec.world}]: budget "
             f"{budget:,} B, vanilla resident {baseline:,} B "
             f"({pct:.1f}% of it), headroom {budget - baseline:,} B. "
             f"THE BUDGET IS UNVERIFIED ON CONSOLE -- no arena has ever been "
             f"built with an arrival in it -- and every arena route COST below "
             f"is PREDICTED from the enemy's measured row into "
             f"{spec.world.upper()}, not measured into {spec.stem}")

        result = ArenaResult(spec=spec, budget=budget, baseline_bytes=baseline)
        results[spec.psx_name] = result

        cands = _candidates(spec, allowlist)
        if not cands:
            emit(f"{LOG_PREFIX} arena {spec.label}: no allowlisted route "
                 f"targets {spec.world}, so there is nothing to draw "
                 f"(not an error)")
            continue

        # Seeded, per-arena, and derived the same way every other draw in this
        # project is (`roster_selection.selection_rng`), keyed on the ARENA so
        # an arena draw is independent of its world's.
        rng = _sel.selection_rng(seed, f"{spec.stem}")
        order = list(cands)
        rng.shuffle(order)

        session = _cw.WorldPortSession(spec.world, game_files_dir,
                                      log=emit, archive_stem=spec.stem)
        try:
            for entry, row in order:
                # THE CAP IS APPLIED TO ADMISSION. Once three routes have
                # COMMITTED the walk stops, so the fourth candidate is never
                # handed to the porter: no mesh / anim / texture bytes are spent
                # on an arrival that would have no victim slot and no player to
                # see it. Breaking (not `continue`) is deliberate -- a cheaper
                # later candidate must not sneak past the cap.
                if len(result.committed) >= cap:
                    emit(f"{LOG_PREFIX} arena {spec.label}: arrival cap "
                         f"{cap} "
                         f"reached, so the admission walk STOPS here -- the "
                         f"remaining candidates are never ported and their "
                         f"bytes are never spent. This cap is the user's "
                         f"chosen figure for a first playable test, NOT a "
                         f"measured limit")
                    break
                tag = (f"0x{entry.enemy_id:02X} {entry.name} "
                       f"{entry.source_world}->{spec.stem}")
                if row is None:
                    result.excluded_rule.append((entry.enemy_id,
                                                 entry.source_world))
                    emit(f"{LOG_PREFIX} arena {spec.label}: {tag} EXCLUDED BY "
                         f"RULE -- no measured row into {spec.world.upper()} to "
                         f"predict from, and a cost is never estimated")
                    continue
                if row.refuses:
                    result.excluded_rule.append((entry.enemy_id,
                                                 entry.source_world))
                    emit(f"{LOG_PREFIX} arena {spec.label}: {tag} EXCLUDED BY "
                         f"RULE -- the measured row records a GROUP REFUSAL")
                    continue
                live = len(session.prs) + len(session.prt)
                predicted = int(row.resident_bytes)
                if live + predicted > budget:
                    result.skipped_budget.append((entry.enemy_id,
                                                  entry.source_world))
                    emit(f"{LOG_PREFIX} arena {spec.label}: {tag} SKIPPED ON "
                         f"BUDGET -- live {live:,} B + PREDICTED {predicted:,} "
                         f"B > {budget:,} B")
                    continue
                ok, why = _cw.port_enemy_route(
                    session, type_id=entry.enemy_id,
                    source_world=entry.source_world,
                    game_files_dir=game_files_dir,
                    allowlist=allowlist, emit=emit)
                if ok:
                    grown = (len(session.prs) + len(session.prt)) - live
                    result.committed.append((int(entry.enemy_id),
                                             entry.source_world, entry.name))
                    emit(f"{LOG_PREFIX} arena {spec.label}: {tag} COMMITTED "
                         f"-- PREDICTED {predicted:,} B, ACTUAL "
                         f"{grown:,} B resident; live now "
                         f"{len(session.prs) + len(session.prt):,} B of "
                         f"{budget:,} B")
                else:
                    result.refused_gate.append((entry.enemy_id,
                                                entry.source_world))
                    emit(f"{LOG_PREFIX} arena {spec.label}: {tag} REFUSED BY A "
                         f"GATE -- {why}; this route only, nothing else is "
                         f"affected and no buffer was mutated")

            # The SAME verification a world session gets, buffer-only.
            report = _cw.verify_session(session)
            result.verified = bool(report.ok)
            result.verify_failures = tuple(report.failures)
            if not report.ok:
                # DISCARD THE WHOLE ARENA. A failed verification means the
                # buffers are not trustworthy, and an arena that will not load
                # is an unwinnable game rather than a cosmetic miss.
                result.committed = []
                emit(f"{LOG_PREFIX} arena {spec.label}: verify_session FAILED "
                     f"-- {'; '.join(report.failures)}. EVERY arrival into this "
                     f"arena is DISCARDED and its archives are left vanilla")
                continue
            out = session.finish()
            collide = set(out) & set(merged)
            assert not collide, (f"{LOG_PREFIX} two arenas produced the same "
                                 f"output filename(s): {sorted(collide)}")
            merged.update(out)
        finally:
            del session      # one arena `.PRS` resident at a time

    return merged, results


# ---------------------------------------------------------------------------
# THE ARENA `.PSX`: VICTIMS AND RETYPING
# ---------------------------------------------------------------------------

def eligible_victims(psx, spec: ArenaSpec) -> tuple:
    """The records this arena may recycle, and nothing else.

    THE NARROWING IS THE SAFETY PROPERTY. The candidate set STARTS as the
    arena's `items.BOSS_DUPLICATION["victim_names"]` -- the side lights,
    ambient fireflies, sea weed and steam vents that were each recorded as
    decorative WITH the measurement that says so -- and every existing
    protection is then applied ON TOP, in the same shape
    `boss_inject.select_host_slot` applies them: the per-file progression set,
    `PROTECTED_INSTANCE_NAMES`, instance-name sharing with a
    `PROGRESSION_ANCHOR_TYPES` record (the `end*` kill groups reach this way),
    `catalog.is_event_tied_enemy`, `GOLD_KEY_TYPE` and `PROTECTED_TAGS` via
    `chest_has_protected_tag`.

    `HAZARD_PROTECTED_AS_SOURCE` IS DELIBERATELY NOT ONE OF THEM, and that is a
    decision with a measurement behind it. That list exists to stop a hazard
    being consumed as a RANDOMIZATION SOURCE out in a normal level, where the
    candidate set is "every record in the file" and a hazard is swept up
    incidentally. Nothing is incidental here: a record must already be named in
    this arena's vetted victim list to be a candidate at all, and that list is
    hand-checked evidence that the specific named records are decorative.

    Applying the list ON TOP made the two tables contradict each other and cost
    the whole under arena. MEASURED: all ten of `U_BOSS.PSX`'s vetted
    `Steam_Vent` victims are type 0x2D, which is IN
    `HAZARD_PROTECTED_AS_SOURCE`, so every one was vetoed, `eligible_victims`
    returned EMPTY, and `plan_arena_retypes` placed nothing while the arrivals'
    assets still landed in `UNDER_B.PRS` / `.PRT` / `.BEF`. There was no way out
    by choosing different victims: the only other `U_BOSS.PSX` records at or
    above the 584 B enemy floor are `Fire` (0xE0, also on the list) and
    `Boss_Camera` (0x33), which must not be retyped at any price.

    The vetted list is the authority for these records, and it does not stand
    alone: `items.duplicate_boss` -- the shipped `--duplicate-bosses` path --
    already overwrites these exact `Steam_Vent` records and has never consulted
    the hazard list, and `items.BOSS_DUPLICATION` records the check that says
    so ("16 exist; we use a subset so plenty of arena steam remains"). The
    remaining protections above still run unchanged, so a vetted name that is
    ALSO progression-bearing, event-tied or a key is still refused.

    TWO ARENAS DEPEND ON THIS, both hazard-typed and both hand-vetted: UNDER's
    `Steam_Vent` (0x2D) and CASTLE_K's `Fire` (0xE0). Grave's `Entity_Light`
    (0x6D), swamp's `Fire_Flies` (0x56) and ice's `bubbles` (0x3B) carry no
    hazard type, so their victim sets are byte-identical to before. `Fire` has
    the same shipped precedent `Steam_Vent` does -- `duplicate_boss` recycles the
    same eight `C_KING.PSX` torches for castle boss clones and has never
    consulted the hazard list either.

    The arena is NOT opened to general randomization by this: a record outside
    the victim list is never a candidate, whatever it is. Boss death and
    victory triggers are keyed on INSTANCE NAMES and retyping a referenced
    record is the recorded infinite-loading / unwinnable-arena failure.
    """
    allowed = set(spec.victim_names)
    protected = _items.get_progression_protected_names(psx)
    anchor_names = {r.instance_name for r in psx.records
                    if r.instance_name
                    and r.type_id in _items.PROGRESSION_ANCHOR_TYPES}
    out = []
    for rec in psx.records:
        name = rec.instance_name or ""
        if name not in allowed:
            continue
        if name in protected or name in _items.PROTECTED_INSTANCE_NAMES:
            continue
        if name in anchor_names:
            continue
        if _catalog.is_event_tied_enemy(name):
            continue
        if rec.type_id == _items.GOLD_KEY_TYPE:
            continue
        # NO HAZARD_PROTECTED_AS_SOURCE CHECK HERE, ON PURPOSE. See the
        # docstring: `rec` is already a hand-vetted decorative record from this
        # arena's victim list, and vetoing it by type vetoed the whole under
        # arena (all ten Steam_Vents are 0x2D).
        try:
            if _items.chest_has_protected_tag(rec):
                continue
        except Exception:      # noqa: BLE001 - a non-chest simply has no tag
            pass
        out.append(rec)
    out.sort(key=lambda r: r.offset)
    return tuple(out)


def arrival_positions(psx, spec: ArenaSpec, count: int) -> tuple:
    """`((x, y, z, provenance), ...)` -- up to `count` PROVEN ARENA FLOOR
    positions for this arena's arrivals, derived THE WAY `items.duplicate_boss`
    DERIVES THEM and from the same table.

    THIS EXISTS BECAUSE OF A REAL BUG. The first cut of `plan_arena_retypes`
    passed the VICTIM as `keep_position_from`, which copies the victim's
    position -- and every victim in `items.BOSS_DUPLICATION["victim_names"]` is
    DECORATIVE LIGHT OR AMBIENCE mounted on a wall or floating: `G_BOSS`'s
    `r_moonlight` / `l_bottom` / `r_mid` / `l_mid` are `Entity_Light`s, `S_BOSS`
    recycles ambient `Fire_Flies_*`, `I_BOSS` `Sea_Weed`, `U_BOSS` `Steam_Vent`.
    Every ported enemy was therefore spawned where a wall light hangs -- in the
    air, inside geometry or out of bounds -- and nothing appeared in game.

    `items.duplicate_boss` never reused the victim's position for exactly this
    reason, and its logic is COPIED rather than reinvented:
      * `clone_positions` when the arena's row has them (`S_BOSS`, `I_BOSS`,
        `U_BOSS` carry explicit spreads measured to sit inside the fight area);
      * otherwise MIRRORED across the arena centre from the BOSS's OWN position
        -- `G_BOSS` has no `clone_positions` because its boss sits off-centre at
        `(13.2, 3, -0.6)`, so a ring at the boss's own radius separates the
        arrivals from it and from each other.

    An arena whose boss record cannot be found AND which has no
    `clone_positions` yields NOTHING. That is the correct outcome: the only
    other coordinate available is the victim's, and that is the bug.
    """
    import math

    cfg = (_items.BOSS_DUPLICATION.get(spec.psx_name.upper())
           or _items.BOSS_DUPLICATION.get(spec.psx_name) or {})
    count = max(0, int(count))
    if not count:
        return ()
    clone_positions = list(cfg.get("clone_positions") or ())
    # `spec.boss_type` is the fallback for an arena with no BOSS_DUPLICATION row
    # at all -- the queen. It only ever supplies the ring's centre and radius;
    # the row's own `boss_type` still wins where there is one.
    boss_type = cfg.get("boss_type", spec.boss_type)
    bosses = (psx.find_records_by_type(boss_type)
              if boss_type is not None else [])
    boss = bosses[0] if bosses else None

    out = []
    for i in range(count):
        if clone_positions:
            if i < len(clone_positions):
                cx, cy, cz = clone_positions[i]
                why = f"clone_positions[{i}] of {spec.psx_name}"
            else:
                # Ran out of explicit positions -- nudge the last one, exactly
                # as `duplicate_boss` does, so arrivals never stack.
                bx, by, bz = clone_positions[-1]
                cx, cy, cz = bx, by, bz + 6.0 * (i + 1)
                why = (f"clone_positions[{len(clone_positions) - 1}] of "
                       f"{spec.psx_name} nudged +{6.0 * (i + 1):.1f} in z "
                       f"(explicit positions exhausted)")
        elif boss is not None:
            # No explicit positions: ring at the boss's own radius about the
            # arena centre, skipping the boss's own angle.
            r = max(8.0, (boss.pos_x ** 2 + boss.pos_z ** 2) ** 0.5)
            base = math.atan2(boss.pos_z, boss.pos_x)
            ang = base + math.pi * (i + 1) / (count + 1)
            cx = r * math.cos(ang)
            cz = r * math.sin(ang)
            cy = boss.pos_y
            why = (f"mirrored across the arena centre from the boss "
                   f"(0x{boss.type_id:02X}) at ({boss.pos_x:.2f}, "
                   f"{boss.pos_y:.2f}, {boss.pos_z:.2f}), radius {r:.2f}, "
                   f"angle {math.degrees(ang):.1f} deg")
        else:
            break
        out.append((float(cx), float(cy), float(cz), why))
    return tuple(out)


def _anchor_at(victim, x: float, y: float, z: float):
    """The victim record with ONE thing changed: the position triple.

    The desired coordinate has to reach `psx.make_record_with_template` AS the
    `keep_position_from` record, not be written over the result afterwards --
    the helper derives `(dx,dy,dz)` from `keep_position_from.pos_* -
    template.pos_*` and translates the record's `0x0C` TRIGGER-VOLUME
    properties by that delta (the Bone_Tower precedent). Overwriting the floats
    after the call would leave an enemy's detection volume at the template's
    home-world coordinates, i.e. an enemy that never notices the player. This
    is the same synthetic-anchor trick `boss_inject` uses for the injected boss,
    and it is why `items.move_record_to` is NOT used here: that helper changes
    only the position triple and translates no volume.

    Everything else stays the victim's -- offset, `instance_id`, the
    `0x100..0x200` name region -- so the record still RECYCLES the victim's slot
    and the `.PSX` does not grow.
    """
    import struct as _struct

    from . import psx as _psx

    raw = bytearray(victim.raw)
    _struct.pack_into("<3f", raw, _psx.RECORD_HEADER_SIZE + 0x10, x, y, z)
    return _psx.PsxRecord(
        offset=victim.offset, class_name=victim.class_name,
        instance_name=victim.instance_name, type_id=victim.type_id,
        instance_id=victim.instance_id, prop_count=victim.prop_count,
        pos_x=x, pos_y=y, pos_z=z, raw=bytes(raw),
    )


def plan_arena_retypes(psx, spec: ArenaSpec, *, arrivals, templates, seed,
                       max_arrivals=None, difficulty=None, log=None) -> dict:
    """`{offset: PsxRecord}` replacements retyping victims into `arrivals`.

    `arrivals` is the COMMITTED type ids for this arena, in commit order, and
    `templates` is the arena's template slot -- which only ever holds a record
    for a type whose route COMMITTED (see `harvest_templates`). A type with no
    template is a LOGGED MISS and never a synthesised record.

    THE CAP COUNTS ENEMIES SPAWNED, NOT TYPES PORTED, so a ported type is REUSED
    across slots when fewer types committed than the cap asks for. One slot still
    holds one enemy; what changed is that slot 4 may hold a second `Snowman`
    instead of being left empty because only three types committed.

    WHY REPETITION IS THE RIGHT WAY TO FILL THE CAP. A spawn needs two things: an
    arena `.PSX` record to occupy, and the enemy's mesh / anim / textures resident
    in the arena's archives. The archives are the EXPENSIVE half and the one whose
    budget has never been verified on console; the `.PSX` record is free, because
    a victim is recycled in place and the file does not grow. Repeating an
    already-ported type therefore adds an enemy for ZERO archive bytes, while
    porting a fourth type to fill a fourth slot would add tens of kilobytes to an
    unverified budget. Variety still comes first -- every committed type is placed
    at least once before any type is repeated.

    POSITION DOES NOT COME FROM THE VICTIM. See `arrival_positions` -- the
    victim slot is recycled for its OFFSET, size ceiling, `instance_id` and name
    region only, while the world position is derived from the BOSS the way
    `items.duplicate_boss` derives it.

    THREE THINGS CAP THE SPAWN COUNT BELOW `max_arrivals`, and each is logged:
    the arena's eligible VICTIM COUNT (GRAVE 4, SWAMP 6 -- neither can reach 9),
    the number of derivable POSITIONS, and per-slot SIZE (a slot smaller than
    every available template stays a decoration).

    ENEMIES ONLY. Nothing here handles a structure: the only porter this feature
    reaches is `cross_world.port_enemy_route`, and templates come from the
    committed ENEMY routes. The world-level cross-world STRUCTURES feature is a
    separate path and is untouched.

    A template LARGER than the victim record is REFUSED for that slot. Arena
    records were recycled by `items.duplicate_boss` precisely because the
    victims are >= the record written over them, and keeping that discipline
    means the arena `.PSX` never grows here.
    """
    from .psx import make_record_with_template

    emit = log if log is not None else (lambda line: None)
    usable = [tid for tid in arrivals if tid in templates]
    for tid in arrivals:
        if tid not in usable:
            emit(f"{LOG_PREFIX} arena {spec.label}: PORTED BUT UNPLACEABLE "
                 f"{_catalog.name_for_type(tid)} (0x{tid:02X}) -- no template "
                 f"record for it, so no victim slot can become it")
    if not usable:
        return {}

    victims = eligible_victims(psx, spec)
    if not victims:
        emit(f"{LOG_PREFIX} arena {spec.label}: no eligible victim slot "
             f"(every proven-decorative record is protected or absent)")
        return {}

    # Seeded and per-arena, so which victim becomes which arrival is
    # reproducible from `--seed` alone. The victim SET is not affected by the
    # seed -- only the pairing is -- so no protection can be shuffled away.
    rng = _sel.selection_rng(seed, f"{spec.stem}.PSX")
    victims = list(victims)
    rng.shuffle(victims)

    # HOW MANY ENEMIES TO SPAWN. `max_arrivals` wins, else the difficulty's
    # figure. This is a COUNT OF ENEMIES, and it is deliberately NOT bounded by
    # `len(usable)`: three ported types fill nine slots by repeating.
    cap = max(0, int(max_arrivals) if max_arrivals is not None
              else arrivals_for_difficulty(difficulty))

    # Every spawn needs its own victim slot and its own position, so the real
    # target is the smallest of the three. Both shortfalls are reported below.
    target = min(cap, len(victims))
    places = arrival_positions(psx, spec, target)
    if not places:
        emit(f"{LOG_PREFIX} arena {spec.label}: NO ARENA POSITION could be "
             f"derived (no clone_positions row and the boss record was not "
             f"found), so NOTHING is placed. The victim's own position is NOT "
             f"used as a fallback -- every victim is a wall light or floating "
             f"ambience, and placing an enemy there is the bug this guards")
        return {}
    target = min(target, len(places))
    if target < cap:
        emit(f"{LOG_PREFIX} arena {spec.label}: spawn target {cap} reduced to "
             f"{target} -- {len(victims)} eligible victim slot(s) and "
             f"{len(places)} derivable position(s). The slot count is a property "
             f"of the arena, not of the difficulty, so this arena cannot hold "
             f"{cap} however high the setting goes")

    # VARIETY BEFORE REPETITION. The type order is shuffled once with the same
    # seeded rng, then walked as a CYCLE, so every committed type is placed once
    # before any is placed twice, and the whole assignment is reproducible from
    # `--seed` alone.
    pool = list(usable)
    rng.shuffle(pool)

    repl: dict = {}
    cursor = 0
    repeats = 0
    for idx in range(target):
        victim = victims[idx]
        px, py, pz, why = places[idx]
        # Take the next type in the cycle that FITS this slot. A slot smaller
        # than a given template is not a failure of the run, only of that
        # pairing, so the cycle keeps looking before giving the slot up.
        chosen = None
        for step in range(len(pool)):
            candidate = pool[(cursor + step) % len(pool)]
            if len(templates[candidate].raw) <= victim.size:
                chosen = candidate
                cursor = (cursor + step + 1) % len(pool)
                break
        if chosen is None:
            emit(f"{LOG_PREFIX} arena {spec.label}: victim "
                 f"{victim.instance_name!r} ({victim.size} B) LEFT AS "
                 f"DECORATION -- none of the {len(pool)} ported type(s) fits it "
                 f"(smallest is "
                 f"{min(len(templates[t].raw) for t in pool)} B), and the arena "
                 f"`.PSX` is never grown to make one fit")
            continue
        tpl = templates[chosen]
        nth = sum(1 for r in repl.values() if r.type_id == chosen) + 1
        if nth > 1:
            repeats += 1
        # The victim's OFFSET, size ceiling, `instance_id` and name region are
        # RECYCLED; only the POSITION comes from somewhere else.
        repl[victim.offset] = make_record_with_template(
            tpl, _anchor_at(victim, px, py, pz), victim.offset)
        emit(f"{LOG_PREFIX} arena {spec.label}: PLACED "
             f"{_catalog.name_for_type(chosen)} (0x{chosen:02X})"
             f"{f' [copy {nth}]' if nth > 1 else ''} at "
             f"({px:.2f}, {py:.2f}, {pz:.2f}) -- position source: {why}; "
             f"recycled victim slot {victim.instance_name!r} "
             f"(0x{victim.type_id:02X}, {victim.size} B, offset "
             f"0x{victim.offset:X}); the victim's own position "
             f"({victim.pos_x:.2f}, {victim.pos_y:.2f}, {victim.pos_z:.2f}) "
             f"was NOT used; trigger volumes translated by "
             f"make_record_with_template")

    emit(f"{LOG_PREFIX} arena {spec.label}: {len(repl)} enemy record(s) placed "
         f"from {len(pool)} ported type(s) ({repeats} of them a reused copy, "
         f"which cost no archive bytes); spawn target was {cap}")
    return repl


def _reidentified(record, template, instance_id: int):
    """`record` with the TEMPLATE's name region and a FRESH instance id.

    Undoes the two things `psx.make_record_with_template` copies off the anchor
    that an APPENDED record must not inherit -- the `0x100..0x200` instance-name
    region and the instance id at prop+0x08 -- while keeping everything it does
    that an appended record DOES want: the position triple and the translated
    `0x0C` trigger volumes.

    The class buffer at `0x000..0x100` is left exactly as the template had it,
    because that is what `psx._is_class_buffer` scans for and therefore what
    makes the appended record discoverable at all.
    """
    from . import psx as _psx

    raw = bytearray(record.raw)

    # A FRESH, UNIQUE INSTANCE NAME -- NOT THE TEMPLATE'S EITHER.
    # Taking the carrier's name would impersonate a waypoint, but taking the
    # TEMPLATE's is not safe either: a template carries whatever name it had in
    # its home world, and MEASURED on real templates those are neither unique nor
    # inert -- appending eight arrivals produced three records called
    # `P3_Zombie_Crocodile1` and two called `start1`. Duplicated names are
    # ambiguous to anything that looks a record up by name, and a name like
    # `start1` is exactly the kind a map or script might already mean something
    # by. The name is therefore SYNTHESISED from the class plus the fresh
    # instance id, which is unique by construction and cannot collide with a
    # vanilla name (`verify_arena_appends` checks that it does not).
    name = f"{record.class_name}_arr{int(instance_id)}"[:0x40]
    name_buf = bytearray(b"\xCD" * _psx.NAME_BUFFER_SIZE)
    encoded = name.encode("ascii", "replace")
    name_buf[:len(encoded)] = encoded
    name_buf[len(encoded)] = 0        # NUL, then 0xCD filler as the format uses
    raw[_psx.NAME_BUFFER_SIZE:_psx.RECORD_HEADER_SIZE] = name_buf

    _struct.pack_into("<I", raw, _psx.RECORD_HEADER_SIZE + 0x08,
                      int(instance_id) & 0xFFFFFFFF)
    return _psx.PsxRecord(
        offset=record.offset, class_name=record.class_name,
        instance_name=name, type_id=record.type_id,
        instance_id=int(instance_id), prop_count=record.prop_count,
        pos_x=record.pos_x, pos_y=record.pos_y, pos_z=record.pos_z,
        raw=bytes(raw),
    )


def plan_arena_appends(psx, spec: ArenaSpec, *, arrivals, templates, seed,
                       max_arrivals=None, difficulty=None, log=None) -> tuple:
    """`({offset: PsxRecord}, {offset: bytes}, added)` for an APPEND-MODE arena.

    NOTHING IS RETYPED HERE. Each returned replacement is a CARRIER record whose
    bytes are `carrier.raw + <one appended enemy record>`: the carrier is copied
    through BYTE-FOR-BYTE and the enemy lands immediately after it. The arena
    therefore GAINS records and keeps every one it shipped with, which is the
    only way to give the queen enemies without touching a `QueenWaypoint`.

    The second return value is the HEADER PATCH that bumps the record COUNT. It
    is not optional and it is not cosmetic: this is the first thing in the
    project to add a record, and `psx._build_replacement_bytes` deliberately
    leaves that field alone precisely because nothing did until now. Hand it to
    `write_with_replacements(..., header_patches=...)`.

    `added` is how many records were appended, so a caller can report it and
    `verify_arena_appends` can check the count arithmetic independently.

    POSITIONS come from `arrival_positions` exactly as they do for a retyped
    arena, and for the queen that means the ring mirrored about the arena centre
    from the boss's own position -- there is no measured floor coordinate for her
    arena, and the carrier's own position is NOT used as a fallback for the same
    reason a victim's never is (a waypoint is a point in the air).
    """
    from .psx import PsxRecord, make_record_with_template

    emit = log if log is not None else (lambda line: None)
    usable = [tid for tid in arrivals if tid in templates]
    for tid in arrivals:
        if tid not in usable:
            emit(f"{LOG_PREFIX} arena {spec.label}: PORTED BUT UNPLACEABLE "
                 f"{_catalog.name_for_type(tid)} (0x{tid:02X}) -- no template "
                 f"record for it, so nothing can be appended for it")
    if not usable:
        return {}, {}, 0

    by_name = {r.instance_name: r for r in psx.records if r.instance_name}
    carriers = [by_name[n] for n in spec.append_carriers if n in by_name]
    if not carriers:
        emit(f"{LOG_PREFIX} arena {spec.label}: none of the "
             f"{len(spec.append_carriers)} declared carrier record(s) is in this "
             f".PSX, so there is nowhere to append and NOTHING is added")
        return {}, {}, 0

    cap = max(0, int(max_arrivals) if max_arrivals is not None
              else arrivals_for_difficulty(difficulty))
    # ONE APPEND PER CARRIER. Two records appended after the same carrier would
    # work in bytes, but keeping it to one keeps the mapping legible and bounds
    # the growth by the carrier count.
    target = min(cap, len(carriers))
    places = arrival_positions(psx, spec, target)
    if not places:
        emit(f"{LOG_PREFIX} arena {spec.label}: NO ARENA POSITION could be "
             f"derived (no clone_positions row and the boss record was not "
             f"found), so NOTHING is appended")
        return {}, {}, 0
    target = min(target, len(places))
    if target < cap:
        emit(f"{LOG_PREFIX} arena {spec.label}: spawn target {cap} reduced to "
             f"{target} -- {len(carriers)} carrier(s) and {len(places)} "
             f"derivable position(s)")

    rng = _sel.selection_rng(seed, f"{spec.stem}.PSX.append")
    pool = list(usable)
    rng.shuffle(pool)

    # One past the highest instance id the arena already uses, so an appended
    # record can never collide with an existing one.
    next_instance_id = max((int(r.instance_id) for r in psx.records
                            if r.instance_id is not None), default=0) + 1

    repl: dict = {}
    added = 0
    for index in range(target):
        carrier = carriers[index]
        px, py, pz, why = places[index]
        tid = pool[index % len(pool)]
        tpl = templates[tid]
        # The appended record's position is anchored the same way a retyped one
        # is, through `make_record_with_template`, so its trigger volumes are
        # translated with it rather than left at the source world's coordinates.
        enemy = make_record_with_template(
            tpl, _anchor_at(carrier, px, py, pz), carrier.offset)
        # *** AND THEN ITS IDENTITY IS TAKEN BACK OFF THE CARRIER. ***
        # `make_record_with_template` copies the anchor's INSTANCE NAME region
        # and INSTANCE ID along with the position, which is exactly right when
        # RETYPING (the new record inherits the slot it replaced) and exactly
        # wrong when APPENDING. Left alone it would give the appended enemy the
        # carrier's own name -- two records called `QueenWaypoint_12985` -- and
        # the queen's fight looks its waypoints up BY INSTANCE NAME. An enemy
        # answering to a waypoint's name is the impersonation this whole mode
        # exists to avoid, so the name region comes back from the TEMPLATE and
        # the instance id is a fresh one no record in this arena uses.
        enemy = _reidentified(enemy, tpl, next_instance_id + index)
        grown = PsxRecord(
            offset=carrier.offset, class_name=carrier.class_name,
            instance_name=carrier.instance_name, type_id=carrier.type_id,
            instance_id=carrier.instance_id, prop_count=carrier.prop_count,
            pos_x=carrier.pos_x, pos_y=carrier.pos_y, pos_z=carrier.pos_z,
            raw=bytes(carrier.raw) + bytes(enemy.raw),
        )
        repl[carrier.offset] = grown
        added += 1
        emit(f"{LOG_PREFIX} arena {spec.label}: APPENDED "
             f"{_catalog.name_for_type(tid)} (0x{tid:02X}) at "
             f"({px:.2f}, {py:.2f}, {pz:.2f}) -- position source: {why}; "
             f"placed immediately after carrier {carrier.instance_name!r} "
             f"(0x{carrier.type_id:02X}, {carrier.size} B, offset "
             f"0x{carrier.offset:X}), which is copied through BYTE-FOR-BYTE and "
             f"keeps its name, properties and position; the arena `.PSX` GROWS "
             f"by {len(enemy.raw)} B for this arrival")

    header_patches: dict = {}
    if added:
        new_count = int(psx.record_count_field) + added
        header_patches[int(psx.record_count_offset)] = _struct.pack(
            "<I", new_count)
        emit(f"{LOG_PREFIX} arena {spec.label}: record COUNT field @"
             f"0x{psx.record_count_offset:X} bumped "
             f"{psx.record_count_field} -> {new_count} for {added} appended "
             f"record(s). THIS FIELD HAS NEVER BEEN WRITTEN BY THIS PROJECT "
             f"BEFORE -- every other path retypes in place, so the count could "
             f"not change. `verify_arena_appends` re-parses the assembled bytes "
             f"and refuses the arena if it disagrees")
    return repl, header_patches, added


def verify_arena_appends(psx, spec: ArenaSpec, replacements: dict,
                         header_patches: dict, added: int, log=None) -> tuple:
    """`(ok, failures)` -- prove an APPEND built what it claimed, in BYTES.

    Assembles the output with `psx._build_replacement_bytes` (which writes
    nothing) and re-parses it from scratch, then asserts the three things that
    would make an appended arena unloadable:

      1. EVERY CARRIER SURVIVES BYTE-FOR-BYTE. The whole point of append mode is
         that no existing record is altered, so each carrier is compared against
         its ORIGINAL bytes in the re-parsed output.
      2. THE RECORD COUNT MATCHES WHAT IS ACTUALLY THERE. The re-parsed record
         list must be exactly `original + added` long, and the count FIELD in the
         assembled header must equal that. A count that disagrees with the
         section is the failure mode that makes the engine's own walk run off.
      3. EVERY ORIGINAL RECORD IS STILL PRESENT BY NAME, so an append cannot have
         silently swallowed one by mis-sizing a carrier.

    A failure here means the buffers are not trustworthy and the caller must ship
    the arena vanilla -- the same stance `verify_session` takes for the archives.
    """
    emit = log if log is not None else (lambda line: None)
    from .psx import _parse_records

    failures: list = []
    try:
        built = psx._build_replacement_bytes(replacements, header_patches)
        data = bytes(built.data)
        # Re-parse the ASSEMBLED BYTES with the same scanner `PsxFile.parse`
        # uses, from the same records_start. Nothing is written to disk to do
        # this, so a failed verification costs no file.
        first = min(r.offset for r in psx.records)
        fresh_records = _parse_records(data, first)

        # 1. EVERY ORIGINAL RECORD SURVIVES BYTE-FOR-BYTE, MATCHED BY ORDER.
        # NOT by offset: an append SHIFTS every record after it, so only the
        # first carrier keeps its original offset and an offset-keyed comparison
        # reports the rest as "missing" when they are simply further along.
        # In offset order the output is the originals with the appended records
        # interleaved, so walking both lists and skipping the additions is the
        # comparison that actually holds.
        originals = sorted(psx.records, key=lambda r: r.offset)
        fresh_sorted = sorted(fresh_records, key=lambda r: r.offset)
        appended_raws = []
        for off, grown in replacements.items():
            base = next((r for r in originals if r.offset == off), None)
            if base is not None:
                appended_raws.append(bytes(grown.raw[base.size:]))
        cursor = 0
        for original in originals:
            matched = False
            while cursor < len(fresh_sorted):
                candidate = fresh_sorted[cursor]
                cursor += 1
                if bytes(candidate.raw) == bytes(original.raw):
                    matched = True
                    break
                if bytes(candidate.raw) in appended_raws:
                    continue          # an appended record, skip it
                failures.append(
                    f"record {original.instance_name!r} is NOT byte-identical "
                    f"in the output; append mode must never alter an existing "
                    f"record")
                matched = True
                break
            if not matched:
                failures.append(
                    f"record {original.instance_name!r} is missing from the "
                    f"re-parsed output")
        # 2. count arithmetic
        expected = len(psx.records) + added
        if len(fresh_records) != expected:
            failures.append(
                f"the output re-parses to {len(fresh_records)} record(s) but "
                f"{expected} were expected ({len(psx.records)} original + "
                f"{added} appended)")
        for off, payload in (header_patches or {}).items():
            if off == int(psx.record_count_offset):
                written = _struct.unpack_from("<I", data, off)[0]
                if written != expected:
                    failures.append(
                        f"the record COUNT field @0x{off:X} reads {written} but "
                        f"the section actually holds {len(fresh_records)} "
                        f"record(s); a count that disagrees with the section is "
                        f"what makes the engine's walk run off the end")
        # 3. nothing swallowed
        original_names = {r.instance_name for r in psx.records
                          if r.instance_name}
        fresh_names = {r.instance_name for r in fresh_records
                       if r.instance_name}
        lost = sorted(n for n in original_names if n not in fresh_names)
        if lost:
            failures.append(f"record(s) present in the vanilla arena are GONE "
                            f"from the output: {lost[:6]}")
        # 4. NO APPENDED RECORD IMPERSONATES AN EXISTING ONE. The fight looks
        # records up by instance name, so an appended enemy answering to a
        # carrier's name is the one way this mode could break the encounter it is
        # designed to leave alone.
        for off, grown in replacements.items():
            base = next((r for r in originals if r.offset == off), None)
            if base is None:
                continue
            for extra in _parse_records(bytes(grown.raw[base.size:]), 0):
                if extra.instance_name and extra.instance_name in original_names:
                    failures.append(
                        f"an appended record carries the EXISTING instance name "
                        f"{extra.instance_name!r}; it would impersonate the "
                        f"record the fight looks up by that name")
    except Exception as exc:  # noqa: BLE001 - a verification never raises out
        failures.append(f"append verification failed to run: "
                        f"{type(exc).__name__}: {exc}")

    if failures:
        emit(f"{LOG_PREFIX} arena {spec.label}: APPEND VERIFICATION FAILED -- "
             f"{'; '.join(failures[:4])}. The arena is left VANILLA")
    else:
        emit(f"{LOG_PREFIX} arena {spec.label}: append verified in bytes -- "
             f"{len(replacements)} carrier(s) byte-identical, {added} record(s) "
             f"appended, count field and re-parsed section agree at "
             f"{len(psx.records) + added}")
    return (not failures), tuple(failures)


def harvest_templates(result: ArenaResult, world_item_templates: dict,
                      log=None) -> dict:
    """The arena's template slot, built from the COMMITTED routes only.

    THE SAME DEFECT AS THE STRUCTURES ONE, AND THE SAME FIX. A retype
    destination needs a TEMPLATE RECORD of the destination type; an arena has
    no record of a foreign enemy by definition, so without this the arrival's
    assets land in the arena archives and nothing can ever become it.

    GATED STRICTLY ON COMMITMENT. Only `result.committed` is walked -- never the
    allowlist, never the drawn order, never the config. A template for a type
    whose mesh / anim / texture did NOT land in this arena's `.PRS` / `.PRT` /
    `.BEF` would place a record the engine cannot render: invisible at best,
    infinite load at worst. No template means a LOGGED MISS; nothing is ever
    synthesised.
    """
    emit = log if log is not None else (lambda line: None)
    slot: dict = {}
    for tid, src_world, name in result.committed:
        tpl = (world_item_templates.get(src_world) or {}).get(tid)
        if tpl is None:
            emit(f"{LOG_PREFIX} arena {result.spec.label}: TEMPLATE MISSING "
                 f"for {name} (0x{tid:02X}) -- source world {src_world} has no "
                 f"harvested record of it, so the arrival stays unplaceable "
                 f"(logged miss, nothing synthesised)")
            continue
        slot.setdefault(tid, tpl)
        emit(f"{LOG_PREFIX} arena {result.spec.label}: injected template "
             f"{name} (0x{tid:02X}) from {src_world} -- now a retyping "
             f"destination inside this arena")
    return slot
