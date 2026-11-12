"""
Catalog of known entity types in Maximo: Ghosts to Glory worlds.

Entity-type IDs and their per-world availability. Built from analyzing G_INTRO.PSX
and U_INTRO.PSX. Each pool contains entities that have been verified as drop-in
compatible with each other (same record format, no cross-spawn dependencies).
"""
from __future__ import annotations

from dataclasses import dataclass


# Walking-melee enemy pool: safe to swap freely within a world.
# All have similar AI/spawn requirements (just appear and walk toward player).
# Ghost included so it gets type-swapped too (its AI is different but the
# template-copy approach has worked fine in our tests).
WALKING_MELEE_POOL = {
    0x47: "Basic_Skeleton",
    0x2F: "Axe_Skeleton",
    0x39: "SwordSkeleton",
    0x48: "Basic_Zombie",
    0x6B: "Ghost",
    0x54: "Guard_Skeleton",  # native to GRAVE in HUB/SUB1/SUB2
    0x6A: "Torso_Zombie",    # zombie variant
    0x37: "Raven",           # small flying enemy
    0x49: "Bomb_Skeleton",   # Underworld + Ice + Castle
    0x27: "Hammer_Devil",    # Underworld only
    0x5D: "Doomed_Soul",     # Underworld only
    # World-specific enemies (added per user request)
    0x10: "Frozen_Zombie",      # ICE (41 vanilla)
    0x3A: "PirateSkeleton",     # ICE (35 vanilla)
    0x1D: "Snowman",            # ICE (18 vanilla)
    0x13: "Goat_Devil",         # UNDER (14 vanilla)
    0x19: "Swamp_Zombie",       # SWAMP (20 vanilla)
    0x0D: "Zombie_Crocodile",   # SWAMP (16 vanilla)
    0x08: "Plant_Monster",      # SWAMP+CASTLE (23 vanilla)
    0x53: "Dark_Knight",        # CASTLE (10 vanilla)
    0x58: "Crazed_Prisoner",    # CASTLE (7 vanilla)
    0x16: "Axe_Guard",          # CASTLE (17 vanilla)
}

# Per-world available walking-melee types. These reflect what each
# world's BEF TOC actually declares (verified via BEF parsing) — NOT
# what cli.py originally guessed. Worlds whose catalog entry was
# narrower than reality (Castle missing 0x48/0x6A, Ice missing 0x6B)
# have been corrected so the BEF inject step doesn't waste budget
# trying to add types the host already has.
GRAVE_WALKING_MELEE  = {0x2F, 0x37, 0x39, 0x47, 0x48, 0x54, 0x6A, 0x6B}
UNDER_WALKING_MELEE  = {0x13, 0x27, 0x2F, 0x39, 0x47, 0x48, 0x49, 0x54,
                        0x5D, 0x6A, 0x6B}
SWAMP_WALKING_MELEE  = {0x08, 0x0D, 0x19, 0x2F, 0x37, 0x39, 0x47, 0x48,
                        0x54, 0x6A, 0x6B}
ICE_WALKING_MELEE    = {0x10, 0x1D, 0x39, 0x3A, 0x47, 0x49, 0x54, 0x6B}
CASTLE_WALKING_MELEE = {0x08, 0x16, 0x2F, 0x37, 0x39, 0x47, 0x48, 0x49,
                        0x53, 0x54, 0x58, 0x6A, 0x6B}

# Single source of truth for world-name -> native walking-melee set.
# NATIVE_WORLDS below is derived from this mapping, so the enemy-keyed and
# world-keyed representations cannot drift apart.
_NATIVE_SETS_BY_WORLD: dict[str, set[int]] = {
    "grave": GRAVE_WALKING_MELEE,
    "under": UNDER_WALKING_MELEE,
    "swamp": SWAMP_WALKING_MELEE,
    "ice": ICE_WALKING_MELEE,
    "castle": CASTLE_WALKING_MELEE,
}


def _invert_native_sets() -> dict[int, frozenset[str]]:
    """Invert the per-world native sets into an enemy-keyed map.

    World names match the lowercase strings used by
    get_walking_melee_for_world / get_world_from_bef_path.
    """
    inverted: dict[int, set[str]] = {}
    for world, enemies in _NATIVE_SETS_BY_WORLD.items():
        for enemy_id in enemies:
            inverted.setdefault(enemy_id, set()).add(world)
    return {eid: frozenset(worlds) for eid, worlds in inverted.items()}


# Enemy id -> worlds whose OWN .PRS mesh table holds that enemy's mesh.
#
# This table is derived ONCE at import time by inverting the per-world native
# sets above. It is NOT scanned from the .PRS files at runtime. Two reasons:
#   1. Speed — walking every world's .PRS mesh table on every run is slow, and
#      the answer never changes for a given retail disc.
#   2. Reliability — prs_mesh_parser has a demonstrated resync failure
#      (Snowman 0x1D in ICE, a LoadVertexAnims chunk) that would make the
#      native-world answer itself untrustworthy. A parser hiccup would
#      silently reclassify a native enemy as foreign and trigger a pointless
#      mesh port.
# See design.md, "Native-world table", for the full rationale.
NATIVE_WORLDS: dict[int, frozenset[str]] = _invert_native_sets()


def native_worlds(enemy_id: int) -> frozenset[str]:
    """Worlds whose own .PRS mesh table holds this enemy's mesh.

    Returns an empty frozenset for an unknown enemy id (fail closed: an
    unknown enemy is treated as native nowhere, so a port is required).
    """
    return NATIVE_WORLDS.get(enemy_id, frozenset())


def is_native(enemy_id: int, world: str) -> bool:
    """True when no mesh port is needed to place enemy_id in world.

    Unknown enemy ids and unknown world names both return False.
    """
    return world in NATIVE_WORLDS.get(enemy_id, frozenset())


# ---------------------------------------------------------------------------
# Cross-world allowlist
# ---------------------------------------------------------------------------
#
# WHY AN ALLOWLIST EXISTS AT ALL
# ------------------------------
# Cross-world placement used to be *derived* rather than declared:
#
#     CROSS_WORLD_SAFE_ENEMIES = UNIVERSAL_WALKING_MELEE - PRS_LOCKED_ENEMIES
#
# with PRS_LOCKED_ENEMIES emptied out. Subtracting an empty set admitted all
# 21 walking-melee types into every world, so an enemy whose mesh/textures had
# never been ported into the host world was still considered "safe" and got
# placed. That is exactly how unported enemies ended up in maps. An explicit,
# per-(enemy, source, target) allowlist replaces the derivation: an enemy may
# only cross into a world that someone actually verified.
#
# STATUS LIFECYCLE
# ----------------
#   CANDIDATE -> the port was verified byte-exact in memory (mesh + textures
#                land where expected), but nobody has seen it render in game
#                yet. CANDIDATE entries ARE active during a run, and every run
#                logs them as unconfirmed so the tester knows what to look at.
#   CONFIRMED -> promoted only after the user watched the enemy render
#                correctly in game. In-memory verification alone never
#                promotes an entry. NO ENTRY IS CONFIRMED TODAY: the two that
#                were have been demoted, because their promotion rested on a
#                byte-exact splice over a false field identification and both
#                render with the wrong model in game.
CONFIRMED = "CONFIRMED"
CANDIDATE = "CANDIDATE"


@dataclass(frozen=True)
class AllowEntry:
    """One verified (enemy, source world, target world) crossing.

    World names are the lowercase strings used everywhere else in this module
    (is_native, get_walking_melee_for_world, get_world_from_bef_path). The
    design document spells them uppercase in prose (CASTLE -> GRAVE); the code
    deliberately does NOT, because an uppercase name would silently miss every
    is_native / _NATIVE_SETS_BY_WORLD lookup instead of raising.

    ``active`` is the RUNTIME switch, orthogonal to ``status``. ``status``
    records how much EVIDENCE a route has (CANDIDATE / CONFIRMED); ``active``
    records whether the route is allowed to participate in a build right now.
    A route can be well-measured (CANDIDATE, byte-exact, verify_session green)
    and still be inactive because activating it CRASHED the console. Only
    entries with ``active=True`` reach load_allowlist(), and load_allowlist()
    feeds allowlist_lookup(), which remains the single permission oracle -
    ``active`` adds no second permission path, it only shrinks the one.
    """

    enemy_id: int
    name: str
    source_world: str
    target_world: str
    status: str
    note: str = ""
    active: bool = True
    #: THE COMPANION SET of this route's enemy: the entities it SPAWNS at
    #: runtime, as a transitive closure (Requirement 9.1). THREE-VALUED, and the
    #: distinction matters:
    #:
    #:   ``None``  -- UNINVESTIGATED. Nobody has decoded this enemy's
    #:                Script_Section, so whether it owns companions is unknown.
    #:                With ``cross_world.COMPANION_PORT_ENABLED`` True the
    #:                companion porter REFUSES such a route (Requirement 9.2)
    #:                rather than silently porting an enemy whose extra objects
    #:                may be missing -- which is the very defect this feature
    #:                exists to fix.
    #:   ``()``    -- INVESTIGATED AND MEASURED TO HAVE NONE. Not the same claim
    #:                as ``None`` at all, and porting it needs nothing extra.
    #:   ``(ids,)``-- the MEASURED closure, in the order the notes report it.
    #:
    #: Every non-``None`` value below was recovered by DECODING the enemy's
    #: Script_Section with `befscript` and tracing `CScriptApi::Spawn`'s literal
    #: argument, then closed transitively by `companion_port.resolve_group`. The
    #: tests re-derive these sets from the oracle against the real `.BEF` files
    #: and must agree, so the literals here cannot drift from the game data.
    #:
    #: Membership does NOT mean the companion is missing from the target world:
    #: `0x3A`'s `0x35` is already present in `GRAVE.BEF`, so that route reuses
    #: the existing entry and injects nothing.
    companions: tuple[int, ...] | None = None

    @property
    def key(self) -> tuple[int, str, str]:
        return (self.enemy_id, self.source_world, self.target_world)


# *** ALL THREE ROUTES REMAIN CANDIDATE. THE MECHANISM IS NOW CORRECTED. ***
#
# THE CORRECTED MECHANISM
#   A BEF blob's material section at [blob+0x20, blob+0x24) is framed
#   [record_count u32] then per record [len u32][body], and each body opens with
#   one or two 5-byte resource refs [rec_type u8][u32 = flag<<24 | index]:
#   index = bits 0-23, category = bits 24-30, is_global = bit 31. rec_type 0x05
#   carries CATEGORY 1 = a positional index into that world's .PRS MESH TABLE;
#   rec_type 0x04 carries CATEGORY 2 = an index into that world's .PRS ANIM
#   TABLE. The walk parses all 359 blobs of all five worlds exactly, and
#   max_local_cat1 == mesh_count-1 in every world (GRAVE 38/39, CASTLE 58/59,
#   SWAMP 41/42, ICE 40/41, UNDER 39/40). Proof: Dark_Knight 0x53's CASTLE cat-1
#   refs are 25, 25, 26, and CASTLE.PRS mesh[25] is the tag-6, 132,900-byte,
#   34-bone, 31-render-pass PSkeleton with textures exactly {33, 34}.
#   The u16 at blob offset 22 is NOT a mesh index and is no longer read.
#
# WHY NOTHING IS CONFIRMED
#   Every status below stays CANDIDATE. The previous promotions rested on
#   byte-exactness over a false field identification, and both enemies rendered
#   with the wrong model in game. Only the user seeing an enemy render correctly
#   promotes a route. Ports through the corrected path are also gated hard: only
#   GRAVE and ICE pass the target-table gate today, so castle/swamp/under can be
#   sources but not targets.
# ``cross_world.MESH_PORT_ENABLED`` is False, so every route below is refused
# before any mesh extraction and nothing is ever ported. The entries are kept
# deliberately, not deleted, so the routes and their measurements stay on
# HISTORICAL NOTE, kept because it cost a lot to learn
#   Dark_Knight 0x53 and Zombie_Crocodile 0x0D were once CONFIRMED. That rested
#   on a byte-exact splice performed with indices read from a BEF blob's u16@22,
#   which is NOT a mesh-table index (seven native GRAVE.BEF enemies all carry 25
#   there, and Basic_Skeleton 0x47 / Guard_Skeleton 0x54 render correctly in
#   vanilla while looking nothing alike). Both spawned in game with the WRONG
#   MODEL and were demoted. Byte-exactness proves the splice, never the premise.
#   Bomb_Skeleton 0x49's old "mesh[30]" was the same mistake: mesh[30] was never
#   its model, which is why that route froze GRAVE on load.
CROSS_WORLD_ALLOWLIST: tuple[AllowEntry, ...] = (
    AllowEntry(0x53, "Dark_Knight", "castle", "grave", CANDIDATE,
               "CORRECTED MECHANISM. CASTLE.BEF blob 0x53's LOCAL CATEGORY-1 "
               "material refs are [25, 25, 26] - three refs, TWO distinct "
               "source mesh indices, so the port dedupes and appends exactly "
               "two entries with both refs to 25 pointing at the one new "
               "index. CASTLE.PRS mesh[25] is a tag-6 PSkeleton, 132,900 B, 34 "
               "bones, 31 render passes, textures exactly {33, 34}; mesh[26] is "
               "a tag-1 prop, 243,788 B, textures {33, 34}. Both texture ids "
               "are below Global_Tex_Count 164, so they are GLOBAL and NO .PRT "
               "port is needed. Still CANDIDATE: the old CONFIRMED status came "
               "from the discredited u16@22 splice and the enemy rendered "
               "wrong; only the user seeing the correct model in game promotes "
               "it. COMPANIONS MEASURED: Spawn(0x8C) at Script_Section offset "
               "234 (its literal argument pushed at 231), and 0x8C spawns "
               "nothing further. 0x8C is ABSENT from GRAVE.BEF, so this route "
               "needs one injection. 0x8C's blob is 1,088 B. The bytecode "
               "proves what the enemy spawns; it does not prove 0x8C is the "
               "purple wall - only the user seeing it in game does that",
               companions=(0x8C,)),
    AllowEntry(0x0D, "Zombie_Crocodile", "swamp", "grave", CANDIDATE,
               "CORRECTED MECHANISM. SWAMP.BEF blob 0x0D's LOCAL CATEGORY-1 "
               "refs are [13, 13, 13, 13, 13] - five refs, ONE distinct source "
               "index, so exactly one entry is appended and all five refs are "
               "repointed at it. SWAMP.PRS mesh[13] is a tag-6 PSkeleton, 23 "
               "bones, textures {12} - id 12 is below Global_Tex_Count 164, so "
               "GLOBAL, no .PRT port needed (the old route's '4 textures "
               "ported' belonged to a different, wrongly-identified entry). "
               "Still CANDIDATE for the same reason as 0x53. COMPANIONS "
               "MEASURED AS NONE: its Script_Section carries no Spawn call at "
               "all - only one SpawnAbility at offset 1062, whose argument "
               "roles are undetermined and which is therefore never read as an "
               "edge. This is an empty tuple (investigated, none), NOT None "
               "(uninvestigated)",
               companions=()),
    AllowEntry(0x49, "Bomb_Skeleton", "castle", "grave", CANDIDATE,
               "CORRECTED MECHANISM. CASTLE.BEF blob 0x49's LOCAL CATEGORY-1 "
               "refs are 10 x8, 11 x2, 12 x1 - eleven refs, THREE distinct "
               "source indices, so three entries are appended and each ref "
               "group is repointed at its one new index. CASTLE.PRS mesh[10] is "
               "a tag-6 PSkeleton with 39 bones and textures {9,10,11,12}; "
               "mesh[11] is a tag-6 PSkeleton with 24 bones and textures "
               "{10,11,12}; mesh[12] is a tag-1 prop with textures {-1}. An "
               "all-(-1) entry is legitimate here - the blob itself names it, "
               "unlike the old u16@22 era where 'no texture bound' was evidence "
               "the bytes were the wrong object. The previous note's 'source "
               "mesh index 30' is void: 30 came from u16@22 and mesh[30] was "
               "never Bomb_Skeleton's model, which is what froze GRAVE on load. "
               "CANDIDATE pending in-game confirmation. COMPANIONS MEASURED: "
               "Spawn(0x5E) @1288 - immediately after a throw sound effect, so "
               "0x5E IS THE BOMB - Spawn(0xEB) @1965, Spawn(0xEB) @2509, "
               "Spawn(0x63) @2664, Spawn(0x44) @2767; and 0x5E itself spawns "
               "0x44 @487 and 0xEB @717, i.e. the blast effects, which is why "
               "the earlier standalone 0xEB injection did nothing: it satisfied "
               "the LEAF of the chain while the projectile that spawns it was "
               "never created. All four are ABSENT from GRAVE.BEF. Blob sizes: "
               "0x5E 2,096 B, 0x44 880 B, 0xEB 416 B, 0x63 400 B",
               companions=(0x5E, 0x44, 0xEB, 0x63)),
    # --- ROUTES 4-7: MEASURED. HELD BACK THEN, ACTIVE NOW -------------------
    #
    # READ THE BLOCK BELOW AS HISTORY, NOT AS CURRENT STATE. Three of the four
    # routes under this header -- 0x16 Axe_Guard, 0x27 Hammer_Devil and 0x5D
    # Doomed_Soul -- are ACTIVE (active=True) today. 0x08 Plant_Monster is the
    # only one still held back, and its own entry states why (it is MEASURED TO
    # REFUSE as a group on the same prs_mesh_parser resync gap as 0x53
    # Dark_Knight, and one deliberate known refusal in a draw is enough).
    #
    # WHY THEY WERE OFF, AND WHY THAT REASON EXPIRED
    #   They were held back when `active` was the ONLY size control: every
    #   active route shipped in every build, so activating a measured route GREW
    #   the build, and the build's total is what the RESOURCE pool bounds. With
    #   all seven active the user built an ISO and the game CRASHED ON GRAVE
    #   LEVEL LOAD, so routes were parked one at a time to bisect the ceiling.
    #
    #   `randomizer/roster_selection.py` removed that coupling. The draw is a
    #   seed-chosen SUBSET of the allowlist, and admission bounds the LIVE
    #   resident total (`len(session.prs) + len(session.prt)`) against
    #   `pool_budget` after every commit. So the allowlist is now the set a seed
    #   MAY draw from: a larger allowlist buys VARIETY, not size, and holding a
    #   measured route back no longer protects the pool -- it only makes every
    #   seed produce the same lineup. `active=False` keeps its ONE remaining
    #   meaning (Requirement 10.3): a route excluded from every draw regardless
    #   of seed, for a reason of its own.
    #
    #   THE BISECT HISTORY BELOW IS KEPT DELIBERATELY. It cost several ISO
    #   builds and in-game load tests to learn, it is the evidence the 90.4%
    #   occupancy budget is derived from, and none of it is reproducible from the
    #   code. It describes the world as it was while `active` was the size
    #   control; it does not describe which routes ship today.
    #
    #   All four passed the gated port path and verify_session's eight checks.
    #   That proves the in-memory buffers are STRUCTURALLY CONSISTENT. It says
    #   NOTHING about whether the console can hold or load them. The cumulative
    #   seven-route cost measured into GRAVE was:
    #     GRAVE.PRS  2,563,126 -> 5,123,325 B  (+2.56 MB, roughly double)
    #     GRAVE.PRT    817,232 -> 1,619,328 B  (+0.80 MB)
    #     mesh entries 39 -> 50, anim records 53 -> 115,
    #     .PRT textures 35 -> 54, BEF entries 70 -> 77
    #   PS2 has 32 MB main RAM and only 4 MB of VRAM for framebuffers plus all
    #   resident textures, so the .PRT growth is a separate and harder budget
    #   than the .PRS growth. The failure mode is therefore a RUNTIME RESOURCE
    #   LIMIT, not a structural defect - which is exactly why verify_session
    #   staying green cannot clear these routes.
    #
    #   The entries are kept, with their measurements intact, so the evidence is
    #   not lost and so the user can BISECT: flip ONE route's active=False to
    #   active=True, build, and see whether GRAVE still loads.
    #
    #   THE BRACKET, measured per route one at a time into a pristine GRAVE
    #   (.PRS growth incl. ported anim records / .PRT growth):
    #     ACTIVE THREE (this build LOADED in game):
    #       0x0D Zombie_Crocodile  +234,996 / +66,576
    #       0x49 Bomb_Skeleton     +395,946 / +184,384
    #       0x53 Dark_Knight       +612,154 / +100,384
    #       ---------------------- +1,243,096 / +351,344
    #     HELD BACK, cheapest first - bisect in this order:
    #       0x16 Axe_Guard         +87,263  / +138,288   (1 mesh, 3 tex, 1 anim)
    #       0x19 Swamp_Zombie      +359,716 / +110,656   (2 mesh, 4 tex, 10 anim)
    #       0x58 Crazed_Prisoner   +411,500 / +84,000    (1 mesh, 2 tex, 12 anim)
    #       0x3A PirateSkeleton    +458,624 / +117,808   (1 mesh, 3 tex, 15 anim)
    #     ALL SEVEN (this build CRASHED on GRAVE level load):
    #                              +2,560,199 / +801,792
    #   So the level-load ceiling lies somewhere above +1.24 MB and at or below
    #   +2.56 MB of .PRS growth (and above +0.34 MB, at or below +0.80 MB of
    #   .PRT growth). Nothing narrower is known, which is exactly what the
    #   bisect is for.
    #
    #   For the record, no COUNT cap was found in MAXDEMOR.ELF that any of these
    #   numbers approach: LoadAnimResTable (vaddr 0x00167F00) reads its record
    #   count and calls new(count*4) with only a zero check, no bound; the one
    #   texture-side bound found is an assert at vaddr 0x001A6178,
    #   slti $v0, $v1, 175, i.e. the 175-slot CLUT table (GRAVE goes 34 -> 53
    #   CLUTs with all seven, nowhere near it). Absence of a count cap is why
    #   the suspicion is total allocation, not table length.
    #
    # --- added by the exhaustive GRAVE-route measurement ---------------------
    # Every candidate route into GRAVE (every WALKING_MELEE_POOL enemy not
    # native to GRAVE, once per world it IS native to) was driven through the
    # UNMODIFIED gated path: a fresh WorldPortSession per route, a synthetic
    # PlacementPlan(path=PORT), then verify_session. 16 routes enumerated, 7
    # passed, 9 refused. NO GATE WAS RELAXED for any of them; the four entries
    # below are exactly the passing routes that were not already listed.
    # The 9 refusals, recorded rather than fixed (both causes are pre-existing
    # and out of scope here):
    #   * 8 refused by texture_port's OPACITY_MAP-on-non-indexed (fmt=1) case
    #     -- SINCE FIXED AND RE-MEASURED, see the block after 0x58 below --
    #     which makes the following .PRT entry's start undeterminable:
    #     Plant_Monster 0x08 (castle entry 44, swamp entry 27), Frozen_Zombie
    #     0x10 and Snowman 0x1D and Bomb_Skeleton 0x49 (ice entry 11),
    #     Hammer_Devil 0x27 and Bomb_Skeleton 0x49 and Doomed_Soul 0x5D
    #     (under entry 3). Fixing that one texture case would unblock 8 routes,
    #     though 0x49 already has a passing CASTLE source.
    #   * 1 refused by the source-side mesh-table walk: Goat_Devil 0x13's
    #     UNDER.PRS mesh[38] sits after a top-level resync at entries 33-38 and
    #     does not round-trip in isolation (2408 B slice, 132 consumed).
    #   * 0 refused by the reference_integrity texture-remap hole (measured on
    #     0x44 / 0x5E during the companion investigation): neither of those ids
    #     is in WALKING_MELEE_POOL, so no GRAVE enemy route hits it today.
    AllowEntry(0x16, "Axe_Guard", "castle", "grave", CANDIDATE,
               "MEASURED. CASTLE.BEF blob 0x16's LOCAL CATEGORY-1 refs are "
               "[4, 4] - two refs, ONE distinct source mesh index, so exactly "
               "one entry is appended and both refs are repointed at it. "
               "CASTLE.PRS mesh[4] signature (len, tag, bones, passes) = "
               "(72100, 6, 14, 22): a tag-6 PSkeleton. It names 1 distinct "
               "LOCAL CATEGORY-2 index [2], and the port appended 1 anim "
               "record of its own; 3 source-world-local textures were ported "
               "into GRAVE.PRT. Alone into a pristine GRAVE this takes mesh "
               "39->40, anim 53->54, .PRT 35->38, and verify_session passes "
               "all eight checks. CANDIDATE: no in-game confirmation yet. "
               "NOW ACTIVE (active=True) as the FIRST BISECT STEP after the "
               "seven-route ISO crashed on GRAVE level load: it is the cheapest "
               "of the four held-back routes (+0.23 MB .PRS total for the "
               "four-route build), so this build tests whether GRAVE still "
               "loads just above the known-good +1.24 MB. Revert to "
               "active=False if this build also crashes. "
               "THAT BUILD LOADED, and so did the five-route build containing "
               "it (0x0D/0x16/0x1D/0x49/0x53, +1,663,193 .PRS / +609,504 .PRT). "
               "NOW HELD BACK AGAIN (active=False) TO MAKE THE AXIS EXPERIMENT "
               "CLEAN, not because it is suspected of anything. The open "
               "question is WHICH BUDGET BINDS: the crashing six-route build "
               "(+2,121,817 .PRS / +727,312 .PRT) blew past both at once. "
               "Adding 0x58 Crazed_Prisoner on top of the five-route set would "
               "have measured +2,074,693 .PRS / +693,504 .PRT - BOTH axes above "
               "the best known-good pair, so a crash would again have been "
               "explainable either way. Since every route costs some .PRT, the "
               "only way to push .PRS while holding .PRT at or below a "
               "known-good figure is a SWAP, and this route is the one to drop: "
               "it is the most .PRT-expensive per .PRS byte of the active set "
               "(+138,288 .PRT for only +87,263 .PRS). Dropping it and adding "
               "0x58 gives +1,987,430 .PRS / +555,216 .PRT - .PRS well above "
               "the +1,788,983 known-good, .PRT 54,288 B BELOW the +609,504 "
               "known-good. Re-enable as soon as the axis verdict is in; "
               "Axe_Guard itself is proven to load. COMPANIONS MEASURED AS "
               "NONE: its Script_Section names no Spawn edge whatsoever. Empty "
               "tuple = investigated, none; not None. "
               "ACTIVE AGAIN (active=True) UNDER SEEDED SELECTION - see the "
               "WHY ACTIVATION IS NOW SAFE block below this entry group. It is "
               "MEASURED (+87,263 .PRS / +138,288 .PRT, roster_costs row) and "
               "expected to COMMIT, and it LOADED IN GAME twice, so there was "
               "never evidence against it: it was swapped out to pay for an "
               "axis experiment that no longer exists",
               active=True, companions=()),
    AllowEntry(0x19, "Swamp_Zombie", "swamp", "grave", CANDIDATE,
               "MEASURED. SWAMP.BEF blob 0x19's LOCAL CATEGORY-1 refs are "
               "19 x10 and 20 x1 - eleven refs, TWO distinct source mesh "
               "indices, so two entries are appended and each ref group is "
               "repointed at its one new index. SWAMP.PRS mesh[19] is "
               "(148476, 6, 36, 33), a tag-6 PSkeleton with 36 bones; mesh[20] "
               "is (43028, 1, 0, 15), a tag-1 prop. It names 10 distinct LOCAL "
               "CATEGORY-2 indices [23..32] and the port appended all 10 of "
               "its own anim records; 4 world-local textures ported. Alone "
               "into a pristine GRAVE: mesh 39->41, anim 53->63, .PRT 35->39, "
               "verify_session passes all eight checks. CANDIDATE: no in-game "
               "confirmation yet. HELD BACK (active=False) after the "
               "seven-route ISO crashed on GRAVE level load. COMPANIONS "
               "MEASURED: Spawn(0x1A) at offsets 2608, 2723 and 2886 - three "
               "call sites, ONE distinct companion, so the group has one member. "
               "0x1A is ABSENT from GRAVE.BEF and 0x1A spawns nothing further. "
               "NOW ACTIVE (active=True) AS CONFIG D. Config D is the active set "
               "0x0D/0x19/0x1D/0x49/0x53/0x58 built as a SINGLE SESSION with "
               "COMPANION_PORT_ENABLED True, and it measures "
               "+1,916,116 .PRS / +676,240 .PRT / +21,416 .BEF. FIVE routes "
               "commit; 0x53 Dark_Knight REFUSES, because its companion 0x8C "
               "needs CASTLE.PRS mesh[54], which sits after a top-level resync "
               "at entry 50, so mesh_port.assess_source_entry will not extract "
               "it. That refusal is CORRECT and mutates nothing - it is not to "
               "be relaxed. This route's own contribution is +377,851 .PRS / "
               "+110,656 .PRT: the enemy at +359,716 / +110,656 plus companion "
               "0x1A (832 B blob) at +18,135 .PRS / +0 .PRT - ZERO .PRT because "
               "every texture 0x1A's mesh binds was already ported by the enemy "
               "and came back out of the texture memo. "
               "THE RISK, STATED HONESTLY. .PRS at +1,916,116 is 71,314 B BELOW "
               "the proven-good +1,987,430, so that axis is not being pushed. "
               ".PRT at +676,240 is 66,736 B ABOVE the proven-good +609,504 and "
               "51,072 B below the crashing +727,312 - i.e. squarely inside the "
               "band on the .PRT axis that has NEVER been isolated. This build "
               "therefore carries genuine crash risk, on .PRT specifically. "
               "WHAT EITHER RESULT MEANS. The RESOURCE-pool model below predicts "
               "a LOAD at 91.1% of the pool. If it LOADS, the .PRT known-good "
               "rises to +676,240 and the pool model gains in-game "
               "corroboration. If it CRASHES, .PRT binds somewhere between "
               "+609,504 and +676,240 while .PRS sat under a figure already "
               "proven good - in that case revert THIS entry to active=False. "
               "THE POOL MODEL, which is the real reason to expect a load: the "
               "engine allocates every non-global .PRS/.PRT payload out of ONE "
               "hard-coded pool named RESOURCE, capacity 0x640000 = 6,553,600 B "
               "(MemoryManager::__ct @ 0x00191F40; PResourceManager::Load @ "
               "0x00168230 switches into RESOURCE for bLocal loads and back to "
               "MAIN on exit). On exhaustion MemoryPool::Allocate @ 0x00192950 "
               "logs 'MemoryPool %d ( Allocate Failed on <%d> bytes : Memory "
               "full )' at MemoryManager.cpp:351 and then FALLS THROUGH, "
               "returning sentinel_block + 16 - a pointer into the allocator's "
               "own block table - so the caller proceeds to corrupt allocator "
               "metadata. On retail, with no debug output, that is a silent hard "
               "crash at level load. Every build that LOADED is at or below "
               "90.4% of the pool; both builds that CRASHED are at or above "
               "95.1%. Config D is 5,972,714 / 6,553,600 = 91.1%. Retail carries "
               "the same constant: SLUS_200.17 'lui $a2, 0x0064' at file offset "
               "0xab5bc, SLPM_621.27 at 0xab8ac",
               active=True, companions=(0x1A,)),
    AllowEntry(0x3A, "PirateSkeleton", "ice", "grave", CANDIDATE,
               "MEASURED. ICE is refused as a PORT TARGET (its mesh walk needs "
               "two resyncs inside entry 16) but it is perfectly usable as a "
               "SOURCE: only the target table's end has to be trustworthy for "
               "an append, and the source side is gated per entry by "
               "assess_source_entry, which mesh[11] clears. ICE.BEF blob 0x3A's "
               "LOCAL CATEGORY-1 refs are 11 x16 - sixteen refs, ONE distinct "
               "source mesh index, so one entry is appended and all sixteen "
               "refs are repointed at it. ICE.PRS mesh[11] is "
               "(141128, 6, 42, 44), a tag-6 PSkeleton with 42 bones. Two "
               "further refs are GLOBAL and are left strictly alone. It names "
               "15 distinct LOCAL CATEGORY-2 indices and the port appended all "
               "15 of its own anim records; 3 world-local textures ported. "
               "Alone into a pristine GRAVE: mesh 39->40, anim 53->68, .PRT "
               "35->38, verify_session passes all eight checks. CANDIDATE: no "
               "in-game confirmation yet. "
               "NOW ACTIVE (active=True) as the SECOND BISECT STEP: the "
               "four-route build (the three originals plus 0x16 Axe_Guard, "
               "+1.33 MB .PRS / +0.47 MB .PRT) LOADED in game, so the ceiling "
               "is above +1.33 MB and at or below the crashing +2.56 MB. This "
               "route is the largest anim contributor of any candidate (15 of "
               "its own records) and lands the cumulative build near +1.79 MB "
               ".PRS, close to the midpoint of the remaining bracket, which is "
               "what makes it the informative probe. Revert to active=False if "
               "this build crashes. "
               "THAT BUILD LOADED (+1.79 MB .PRS / +0.61 MB .PRT), and it is "
               "now the KNOWN-GOOD CEILING REFERENCE. HELD BACK AGAIN "
               "(active=False) TO ISOLATE SNOWMAN 0x1D. The six-route build "
               "(this route plus Snowman) CRASHED, but that observation is "
               "CONFOUNDED: it added Snowman AND reached 83% of the .PRS / 91% "
               "of the .PRT growth of the already-crashing seven-route build, "
               "so 'Snowman is broken' and 'the build is too big' predict the "
               "same crash. This route was only ever a SIZE PROBE - the user "
               "never asked for PirateSkeleton - and dropping it removes "
               "~+0.46 MB .PRS, putting the five-route build with Snowman "
               "BELOW the +1.79 MB build that LOADED. That is the whole point "
               "of the swap: with size below a known-good build, a crash can "
               "only be Snowman. Re-enable only after Snowman's verdict is in. "
               "SNOWMAN'S VERDICT IS IN: the five-route Snowman build LOADED "
               "(+1,663,193 .PRS / +609,504 .PRT), so Snowman is exonerated and "
               "the six-route crash was cumulative size. This route stays HELD "
               "BACK - it was always a size probe, never an enemy the user "
               "asked for, and the current experiment (0x58 Crazed_Prisoner as "
               "the .PRS-vs-.PRT axis discriminator) needs the budget it would "
               "consume. COMPANIONS MEASURED: Spawn(0x35) @354, one member, and "
               "0x35 spawns nothing further. UNIQUE AMONG THE MEASURED ROUTES: "
               "0x35 IS ALREADY PRESENT IN GRAVE.BEF, so this route's companion "
               "group needs NO injection at all - it takes the reuse path "
               "(Requirement 5.8) and adds zero .PRS/.PRT/.BEF bytes. "
               "NOW ACTIVE AGAIN (active=True) ON THE RAISED RESOURCE POOL. "
               "Every held-back decision above was taken against the VANILLA "
               "pool of 0x640000 = 6,553,600 B, where config D already sat at "
               "91.1% and this route's +458,624 .PRS / +117,808 .PRT would have "
               "taken the resident total to 6,549,146 B = 99.93% of it - i.e. "
               "a near-certain allocation failure at GRAVE level load. "
               "randomizer.elf_patch.patch_resource_pool now raises the pool to "
               "0x700000 = 7,340,032 B (preset 'raised', auto-enabled for "
               "--cross-world), and the SAME resident total is 89.2% of that, "
               "below the <=90.4% every build that loaded came in at. So this "
               "route is re-enabled BECAUSE OF THE POOL PATCH and is coupled to "
               "it: without the patch this exact allowlist overflows. "
               "tests/test_cross_world_pool_headroom.py pins both halves of that "
               "coupling so a future addition that overflows fails loudly "
               "instead of shipping a crash",
               active=True, companions=(0x35,)),
    AllowEntry(0x58, "Crazed_Prisoner", "castle", "grave", CANDIDATE,
               "MEASURED. CASTLE.BEF blob 0x58's LOCAL CATEGORY-1 refs are "
               "27 x14 - fourteen refs, ONE distinct source mesh index, so one "
               "entry is appended and all fourteen refs are repointed at it. "
               "CASTLE.PRS mesh[27] is (147312, 6, 50, 46), a tag-6 PSkeleton "
               "and the largest rig of any passing route at 50 bones. It names "
               "12 distinct LOCAL CATEGORY-2 indices [49..60] and the port "
               "appended all 12 of its own anim records; 2 world-local textures "
               "ported. Alone into a pristine GRAVE: mesh 39->40, anim 53->65, "
               ".PRT 35->37, verify_session passes all eight checks. "
               "CANDIDATE: no in-game confirmation yet. Was HELD BACK "
               "(active=False) after the seven-route ISO crashed on GRAVE "
               "level load. "
               "NOW ACTIVE (active=True) AS THE AXIS DISCRIMINATOR. The "
               "five-route build 0x0D/0x16/0x1D/0x49/0x53 "
               "(+1,663,193 .PRS / +609,504 .PRT) LOADED in game, and the "
               "six-route build (+2,121,817 / +727,312) CRASHED - but that "
               "crash exceeded BOTH budgets at once, so WHICH AXIS is the real "
               "ceiling is UNKNOWN, and the two axes rank routes differently "
               "(0x08 Plant_Monster is mid-priced on .PRS at +321,824 yet the "
               "most expensive on .PRT at +264,256; 0x53 Dark_Knight is the "
               "reverse at +612,154 / +100,384). This route is the chosen "
               "discriminator because of every held-back route it has the "
               "CHEAPEST .PRT cost (+84,000 B, versus 0x19 110,656, 0x3A "
               "117,808, 0x5D 198,704, 0x08 264,256) while being expensive on "
               ".PRS (+411,500 B): it pushes the .PRS axis hard and barely "
               "moves .PRT, which is exactly what separates 'the .PRS budget "
               "binds' from 'the .PRT budget binds'. It is also a genuinely "
               "wanted enemy (Crazed_Prisoner, the largest rig of any passing "
               "route at 50 bones), so the build is useful either way. "
               "THE EXPERIMENT IS A SWAP, NOT AN ADDITION. Adding this route to "
               "the five that loaded would have measured +2,074,693 .PRS / "
               "+693,504 .PRT (measured, not estimated), which is above the "
               "known-good pair on BOTH axes and therefore confounded all over "
               "again - and since every route costs .PRT, no pure addition can "
               "ever hold .PRT at the known-good +609,504. So 0x16 Axe_Guard "
               "(the most .PRT-expensive per .PRS byte of the active set: "
               "+138,288 .PRT for +87,263 .PRS) is held back in exchange. The "
               "resulting active set 0x0D/0x1D/0x49/0x53/0x58 measures "
               "+1,987,430 .PRS / +555,216 .PRT: .PRS +198,447 ABOVE the "
               "known-good +1,788,983 and only 134,387 below the crashing "
               "+2,121,817, while .PRT is 54,288 BELOW the known-good +609,504. "
               "A CRASH therefore means the .PRS axis binds (the .PRT figure is "
               "under a level already proven to load); a LOAD pushes the "
               "known-good .PRS to within 6.3% of the crashing build and points "
               "at .PRT. Revert to active=False if this build crashes. "
               "COMPANIONS MEASURED AS NONE: its Script_Section decodes clean "
               "at 100% coverage (2,577 B) and carries NO Spawn call at all - "
               "only one SpawnAbility at offset 1685, whose argument roles are "
               "undetermined and which is therefore never read as an edge. "
               "Empty tuple = investigated and measured to have none, NOT None "
               "(uninvestigated), so with COMPANION_PORT_ENABLED True this "
               "route no longer refuses on Requirement 9.2",
               active=True, companions=()),
    # --- added after the .PRT OPACITY-MAP SIZE RULE WAS SOLVED ---------------
    #
    # WHAT CHANGED. The 8 refusals above blamed on texture_port's
    # "OPACITY_MAP on a non-indexed (fmt=1) entry" are gone: that entry's body
    # is declared_size + w*h (the colour block plus a ONE-BYTE-PER-PIXEL alpha
    # block), proven twice over - walking all 11 .PRT files with the rule
    # consumes each one exactly (entries == count_b, zero trailing bytes), and
    # the engine's own loader reads exactly w*h further bytes when flags & 2 is
    # set (MAXDEMOR.ELF: andi 2 at vaddr 0x001A62EC, w*h computed at
    # 0x001A634C-0x001A6360, malloc at 0x001A636C, sequential Read at
    # 0x001A63B0 asserting the byte count at 0x001A63BC). See
    # randomizer/texture_port.py's module docstring.
    # None of the routes below actually PORT an opacity-map entry - every
    # texture they inject is indexed (CLUT count grows by exactly the texture
    # count on each) - they only needed to WALK PAST one to reach a higher
    # indexed entry, which is why PirateSkeleton 0x3A (ICE textures 8-10, all
    # below the broken ICE.PRT entry 11) always worked and these did not.
    #
    # RE-MEASUREMENT OF THE 9 PREVIOUSLY-REFUSED GRAVE ROUTES, same unmodified
    # gated path (fresh WorldPortSession, synthetic PlacementPlan(path=PORT),
    # then verify_session; NO GATE RELAXED): 7 pass, 2 still refuse.
    #   PASS  0x08 castle, 0x08 swamp, 0x1D ice, 0x27 under, 0x49 ice,
    #         0x49 under, 0x5D under
    #   REFUSE 0x13 under Goat_Devil - unchanged and NOT expected to change:
    #         "UNDER.PRS mesh table needed a TOP-LEVEL resync at entries
    #         [33..38] ... UNDER.PRS mesh[38] does not round-trip in isolation:
    #         2408 byte slice, 132 consumed, 2276 left over, 2 resync(s)".
    #         A mesh-walk problem, nothing to do with textures.
    #   REFUSE 0x10 ice Frozen_Zombie - a DIFFERENT gate, reached only now that
    #         textures no longer refuse first: "ICE anim[41] cannot bind to the
    #         target rig ... 1 of the 18 bone name(s) this record addresses have
    #         no counterpart in the target rig". Anim rig binding, out of scope.
    # 0x49 ice and 0x49 under also pass but are NOT listed: 0x49 already has a
    # working CASTLE source and two sources for one (enemy, target) pair is
    # forbidden. 0x08 passes from BOTH castle and swamp with identical cost
    # (+321,824 / +264,256 either way); castle is listed, swamp discarded for
    # the same one-source rule.
    AllowEntry(0x1D, "Snowman", "ice", "grave", CANDIDATE,
               "MEASURED, AND THE ROUTE THE OPACITY-MAP FIX WAS FOR. Previously "
               "REFUSED by texture_port: reaching Snowman's textures means "
               "walking past ICE.PRT entry 11 (256x128, fmt=1, "
               "FLAG_OPACITY_MAP, declared 98,304 B + 32,768 B opacity), whose "
               "length was unknown. ICE.BEF blob 0x1D's LOCAL CATEGORY-1 refs "
               "resolve to ONE distinct source mesh index, 17, so exactly one "
               "entry is appended and every ref is repointed at it. ICE.PRS "
               "mesh[17] signature (len, tag, bones, passes) = "
               "(182268, 6, 39, 47), a tag-6 PSkeleton with 39 bones. It names "
               "7 distinct LOCAL CATEGORY-2 indices [47, 48, 49, 50, 52, 53, "
               "55] and the port appended all 7 of its own anim records "
               "(53->59 becomes 53..59 in GRAVE); 4 world-local textures were "
               "ported, all INDEXED, so GRAVE's CLUT count grows 34->38 and no "
               "opacity-map entry is injected - entry 11 only had to be walked "
               "past. Alone into a pristine GRAVE: .PRS 2,563,126->2,895,960 "
               "(+332,834), .PRT 817,232->937,104 (+119,872), mesh 39->40, "
               "anim 53->60, .PRT textures 35->39, and verify_session passes "
               "all eight checks. ACTIVE (active=True): this is the route the "
               "user asked for and the only newly-passing one that is active. "
               "CANDIDATE: no in-game confirmation yet. WARNING recorded with "
               "the evidence: the six-route cumulative build is +2,121,817 "
               ".PRS / +727,312 .PRT, i.e. 83% of the .PRS and 91% of the .PRT "
               "growth of the seven-route build that CRASHED on GRAVE level "
               "load, so a crash here is at least as likely to be the size as "
               "the enemy. "
               "BISECT STATE NOW: that six-route build DID crash, and because "
               "the observation was confounded, 0x3A PirateSkeleton (a pure "
               "size probe, never a route the user asked for) has been HELD "
               "BACK so that 0x1D Snowman is THE ONLY NEW VARIABLE relative to "
               "a known-good build. Active set is 0x0D, 0x16, 0x1D, 0x49, "
               "0x53, which measures BELOW the +1.79 MB .PRS five-route build "
               "that LOADED. If GRAVE still crashes, size is excluded and the "
               "cause is Snowman specifically; if it loads, Snowman is fine and "
               "the crash was the cumulative budget. Structurally, this is also "
               "the ONLY active route that must WALK PAST an opacity-map .PRT "
               "entry (ICE.PRT entry 11) to reach its textures - the newest and "
               "least-exercised code path in the port, and the first suspect if "
               "Snowman alone crashes. "
               "VERDICT IN: THAT BUILD LOADED IN GAME. The user confirmed the "
               "five-route build 0x0D/0x16/0x1D/0x49/0x53 at +1,663,193 .PRS / "
               "+609,504 .PRT loads GRAVE with no crash. Two things follow. "
               "(1) SNOWMAN IS NOT THE PROBLEM: the earlier six-route crash "
               "(+2,121,817 / +727,312, Snowman AND PirateSkeleton) was "
               "CUMULATIVE SIZE, not this enemy - Snowman loads fine when the "
               "build is sized below a known-good one. (2) THE .PRT "
               "OPACITY-MAP SIZE RULE IS NOW CORROBORATED ON HARDWARE: this is "
               "the only route that must walk past an opacity-map entry to "
               "reach its textures, and this is the FIRST build containing that "
               "code path that has loaded. The rule was previously proven only "
               "against the bytes (all 11 .PRT files consumed exactly) and the "
               "ELF loader (andi 2 at vaddr 0x001A62EC, w*h at "
               "0x001A634C-0x001A6360, sequential Read at 0x001A63B0); it now "
               "has in-game corroboration too. STATUS STAYS CANDIDATE: the user "
               "confirmed GRAVE LOADS, which is NOT the same as having watched "
               "this enemy render correctly. Only seeing the correct model in "
               "game promotes a route to CONFIRMED. COMPANIONS MEASURED, AND "
               "THIS IS THE ROUTE THAT CORRECTED REQUIREMENT 3.4: Spawn(0x1E) "
               "@640 and Spawn(0x9B) @990, and 0x1E spawns 0xBA while 0xBA "
               "spawns 0x1E BACK - a genuine cycle in retail ICE data. Under the "
               "original refuse-on-cycle rule resolve_group refused this route "
               "outright (companion cycle: 0x1D -> 0x1E -> 0xBA -> 0x1E); with "
               "visit-once dedupe the group is {0x1E, 0x9B, 0xBA}, all three "
               "ABSENT from GRAVE.BEF, so three injections",
               active=True, companions=(0x1E, 0x9B, 0xBA)),
    AllowEntry(0x08, "Plant_Monster", "castle", "grave", CANDIDATE,
               "MEASURED, newly passing after the opacity-map fix (CASTLE.PRT "
               "entry 44 and SWAMP.PRT entry 27, 128x128 fmt=1 opacity maps, "
               "used to block it from both sources). CASTLE.BEF blob 0x08's "
               "LOCAL CATEGORY-1 refs resolve to TWO distinct source mesh "
               "indices, 41 and 44, so two entries are appended. CASTLE.PRS "
               "mesh[41] signature (len, tag, bones, passes) = "
               "(128496, 6, 32, 60), a tag-6 PSkeleton with 32 bones; mesh[44] "
               "is (77044, 6, 24, 42). 6 distinct LOCAL CATEGORY-2 indices "
               "[73, 74, 75, 76, 78, 79] and 6 anim records appended; 4 "
               "textures ported, all indexed (CLUT 34->36 counts the 2 "
               "distinct palettes). Alone into a pristine GRAVE: .PRS "
               "+321,824, .PRT +264,256, mesh 39->41, anim 53->59, .PRT "
               "textures 35->39, verify_session passes all eight checks. The "
               "SWAMP source measures identically (+321,824 / +264,256, "
               "mesh[26] and mesh[29], anim [40..43, 45, 46]) and is discarded "
               "only because one (enemy, target) pair may have one source. "
               "HELD BACK (active=False) so the user's size bisect stays one "
               "variable at a time. COMPANIONS MEASURED: its Script_Section "
               "(3,008 B, clean, 100% coverage) has FOUR Spawn sites - "
               "Spawn(0x09) @1122 (arg @1119), Spawn(0x09) @1312 (@1309), "
               "Spawn(0x06) @1381 (@1378), Spawn(0x0B) @2555 (@2552) - plus one "
               "SpawnAbility @1455 that is never read as an edge. 0x0B itself "
               "spawns 0x09 @1001, already a depth-1 member, so the closure "
               "dedupes to THREE members {0x06, 0x09, 0x0B} at depth 1. ALL "
               "THREE ARE ABSENT FROM GRAVE.BEF, so this route needs three "
               "injections. Blob sizes: 0x06 400 B, 0x09 464 B, 0x0B 2,784 B - "
               "the biggest companion group of any route by blob bytes. NOTE "
               "the SWAMP source would need its own re-derivation: this set was "
               "measured from CASTLE.BEF, which is the listed source. "
               "NO LONGER HELD BACK, AND NO LONGER A REFUSAL. It was parked "
               "because the GROUP refused: companion 0x0B needs CASTLE.PRS "
               "mesh[49], the first PVertexAnim record in the file, and "
               "prs_mesh_parser read PVertexAnim's element count from header +0 "
               "instead of +4 (and PSTAnim's channel count from +2 instead of "
               "+0, which desynced the walk even earlier, at entry 24). Both "
               "offsets are now MEASURED out of the engine's loader chain "
               "(research/PRS_MESH_TABLE_ENGINE_LAYOUTS.md), CASTLE.PRS walks "
               "59/59 with ZERO resyncs, and the group RE-MEASURES AS A COMMIT "
               "at +564,622 .PRS / +276,592 .PRT / +7,280 .BEF with all three "
               "companions injected. STATUS STAYS CANDIDATE: nobody has watched "
               "it render",
               active=True, companions=(0x06, 0x09, 0x0B)),
    AllowEntry(0x10, "Frozen_Zombie", "ice", "grave", CANDIDATE,
               "MEASURED, COMMITS, verify_session green -- and UNBLOCKED BY AN "
               "ELF MEASUREMENT, not by a relaxed gate. This was the LAST "
               "eligible foreign route into GRAVE with no cost row. MECHANISM, "
               "same as every other entry: ICE.BEF blob 0x10's LOCAL CATEGORY-1 "
               "refs resolve to source mesh indices 13 and 14 -- mesh[13] "
               "signature (len, tag, bones, passes) = (127212, 6, 12, 13), a "
               "tag-6 PSkeleton with 12 bones; mesh[14] is (58872, 1, 0, 18), a "
               "tag-1 prop -- and its LOCAL CATEGORY-2 refs name 7 distinct anim "
               "indices [36, 37, 38, 40, 41, 42, 44]. WHAT USED TO BLOCK IT: "
               "ICE anim[41] is a MEA (tag 0x0A) record whose node 0 name field "
               "holds 28 00 00 00 followed by 28 NULs, which reads as the "
               "one-character name '(' -- and the corresponding node of its own "
               "mesh[14] carries 32 NULs, i.e. no name at all. The other 17 of "
               "its 18 node names match the ported rig exactly. Because the "
               "binding key is a string compare, the anim port refused the whole "
               "enemy, and a refused anim port is never downgraded to the "
               "substitution heuristic. WHAT SETTLED IT: "
               "RecurseAttachAnim__9PMeshNodeFP22PMeshNodeAnimDataTable "
               "@0x00175B20 calls Find__9PMeshNodeFPCcUi once per node and at "
               "0x00175BA8 `beql $v0, $zero` (word 0x50400006) branches PAST the "
               "AttachAnim call to the loop's own increment when the name is not "
               "found, annulling its delay slot -- no CdsAssert, no error return, "
               "and the function returns 1 unconditionally at 0x00175BE4. The "
               "engine SKIPS an unmatched MEA node. And since the rig here is "
               "the enemy's OWN ported mesh, byte-for-byte what ICE ships, that "
               "same lookup already fails identically in VANILLA ICE: porting "
               "changes nothing about which nodes bind. The record is appended "
               "UNCHANGED -- no name substituted, no bone invented, no byte "
               "altered -- and the tolerance is scoped to MEA records with at "
               "least one resolving name. SKA records with an unmatched bone "
               "name still REFUSE, because that bind path has not been measured. "
               "COMPANIONS MEASURED: 0x55 and 0x5C, both ABSENT from GRAVE.BEF "
               "and both injected. Group cost +253,253 .PRS / +239,712 .PRT / "
               "+6,792 .BEF: 4 mesh entries appended (mesh 39 -> 43), 5 textures "
               "ported, anim count 53 -> 62. STATUS IS CANDIDATE AND STAYS "
               "THERE: nobody has watched Frozen_Zombie render in GRAVE",
               active=True, companions=(0x55, 0x5C)),
    AllowEntry(0x13, "Goat_Devil", "under", "grave", CANDIDATE,
               "MEASURED, COMMITS, verify_session green -- and UNBLOCKED BY THE "
               "prs_mesh_parser FIELD-OFFSET CORRECTION. This route had no "
               "entry at all and no cost row: UNDER.PRS's mesh table resynced at "
               "entries 33-38, so mesh[38] could not be extracted (2,408 B "
               "slice, 132 consumed) and roster_costs recorded it as "
               "UNMEASURABLE. UNDER.PRS contains ZERO PVertexAnim records and 3 "
               "PSTAnim blocks, so that entire range was the single PSTAnim "
               "channel-count bug: the count is a u16 at header +0, not +2 "
               "(Load__7PSTAnimFP5PFile @0x00195BE0; see "
               "research/PRS_MESH_TABLE_ENGINE_LAYOUTS.md). UNDER.PRS now walks "
               "40/40 with ZERO resyncs and the mesh table ends exactly where "
               "the next resource table's type id sits. MECHANISM, same as every "
               "other entry: UNDER.BEF blob 0x13's LOCAL CATEGORY-1 material "
               "refs resolve to source mesh indices 38 and 39, which is what the "
               "port reads -- never blob offset 22. Re-measured through the "
               "unmodified gated path: TWO mesh entries appended (+442,604 "
               ".PRS, mesh 39 -> 41), 2 textures ported (+133,152 .PRT), 10 anim "
               "records appended (anim 53 -> 63), +3,544 .BEF, every "
               "verify_session check green. COMPANIONS MEASURED AS NONE (its "
               "script section carries no Spawn edge), so the group cost is the "
               "enemy's own by construction. STATUS IS CANDIDATE AND STAYS "
               "THERE: the walk is correct and the buffers verify, but nobody "
               "has watched Goat_Devil render in GRAVE. "
               "RE-ENABLED (active=True) BY EXPLICIT USER REQUEST, with the "
               "crash UNEXPLAINED. The observed failure stands: broken "
               "mesh/texture then a hard freeze, ALWAYS at spawn. No root "
               "cause was found and nothing was fixed; the user chose to ship "
               "it anyway and is testing it themselves. Re-arm the hold by "
               "putting 0x13 back into HELD_BACK_ENEMIES_ON_OBSERVED_CRASH and "
               "setting active=False. "
               "THE OBSERVED IN-GAME CRASH, AS RECORDED: the "
               "user reports broken mesh/texture on screen followed by a hard "
               "freeze, ALWAYS at spawn, while 0x08 Plant_Monster, 0x53 "
               "Dark_Knight and 0x10 Frozen_Zombie render correctly. The two "
               "leading hypotheses were MEASURED AND BOTH COME BACK CLEAN, so "
               "this note records what has been ruled OUT rather than a fix. "
               "(1) THE MULTI-MESH TEXTURE-REMAP GAP IS NOT THE CAUSE HERE. "
               "Placed alone into a pristine GRAVE, target .PRT count is 37 and "
               "the render-pass texture ids under the appended entries are "
               "mesh[39] -> {35} (38 passes) and mesh[40] -> {35, 36} (26 "
               "passes). tex_remap is {44: 35, 45: 36}: every id bound is a "
               "REMAPPED TARGET id, none is a surviving UNDER-local value, none "
               "is >= 37. Under BOTH appended entries, not just the first. "
               "(2) THE ANIM INDEX SPACE IS NOT THE CAUSE HERE. The blob's 13 "
               "LOCAL refs are 3 CATEGORY-1 (rec_type 0x05) reading 39, 39, 40 "
               "against a final mesh count of 41, and 10 CATEGORY-2 (rec_type "
               "0x04) reading 53..62 against a final anim count of 63. Every "
               "one was rewritten out of UNDER's index space: "
               "ported_anim_indices = {77:53, 79:54, 80:55, 81:56, 82:57, "
               "84:58, 85:59, 86:60, 87:61, 88:62}. No source-local survivor, "
               "nothing >= the target count. The 10 appended records are NINE "
               "SKA (tag 0x0C) plus ONE MEA (tag 0x0A, anim[88]); all ten are "
               "RigBinding.BY_NAME, all ten resolve EVERY name against the "
               "combined 61-name rig of source mesh[38] + mesh[39] (zero "
               "missing), and bone_remap_required is False for all ten -- so no "
               "BY_INDEX record slipped through and the route did NOT lean on "
               "the MEA-scoped tolerance added for 0x10. "
               "(3) THE COMPANION CLOSURE IS PROVABLY EMPTY, not assumed: "
               "derive_companions(0x13, 'under') returns an EMPTY closure with "
               "reason=None, i.e. it terminated without a single non-literal "
               "Spawn argument to report. Had any Spawn argument been "
               "non-literal the closure would not be provable and the route "
               "would have to be REFUSED rather than shipped. "
               "(4) SHAPE IS NOT WHAT DISTINGUISHES IT. All three WORKING "
               "controls are multi-mesh too and have the same silhouette: "
               "0x53 cat1=[25 (132900,tag6,34 bones,31 passes), 26 "
               "(243788,tag1,0,17)] with 10 SKA + 1 MEA; 0x10 cat1=[13 "
               "(127212,tag6,12,13), 14 (58872,tag1,0,18)] with 6 SKA + 1 MEA; "
               "0x08 cat1=[41 (128496,tag6,32,60), 44 (77044,tag6,24,42)] with "
               "6 SKA. 0x13 is cat1=[38 (140704,tag6,46 bones,38 passes), 39 "
               "(101576,tag1,0,26)] with 9 SKA + 1 MEA. The only figures where "
               "0x13 is the outlier are its 46-bone skeleton (highest measured) "
               "and its 10 appended anim records (most of any route) -- neither "
               "of which this pass could turn into a mechanism, and neither of "
               "which porting changes, since 46 bones render fine in vanilla "
               "UNDER. NO ROOT CAUSE FOUND, and the route SHIPS ANYWAY ON "
               "EXPLICIT USER INSTRUCTION rather than because anything was "
               "explained or fixed. Re-measured cost row unchanged at +442,604 .PRS / "
               "+133,152 .PRT / +3,544 .BEF, mesh 39 -> 41, anim 53 -> 63, "
               "verify_session still green -- the defect is invisible to every "
               "structural check we have, which is itself the finding",
               active=True, companions=()),
    AllowEntry(0x27, "Hammer_Devil", "under", "grave", CANDIDATE,
               "MEASURED, newly passing after the opacity-map fix (UNDER.PRT "
               "entry 3, a 128x128 fmt=1 opacity map, used to block it). "
               "UNDER.BEF blob 0x27's LOCAL CATEGORY-1 refs resolve to TWO "
               "distinct source mesh indices, 8 and 9. UNDER.PRS mesh[8] "
               "signature (len, tag, bones, passes) = (149248, 6, 34, 34), a "
               "tag-6 PSkeleton with 34 bones; mesh[9] is (82444, 1, 0, 17), a "
               "tag-1 prop. 10 distinct LOCAL CATEGORY-2 indices "
               "[8..15, 17, 18] and 10 anim records appended; 1 texture "
               "ported, indexed (CLUT 34->35). Alone into a pristine GRAVE: "
               ".PRS +357,855, .PRT +66,576 - the CHEAPEST .PRT cost of any "
               "route measured - mesh 39->41, anim 53->63, .PRT textures "
               "35->36, verify_session passes all eight checks. HELD BACK "
               "(active=False) so the user's size bisect stays one variable at "
               "a time. COMPANIONS MEASURED: Spawn(0x28) @976 (literal argument "
               "pushed at 973) is its ONE Spawn site (section 3,237 B, clean, "
               "100% coverage; one SpawnAbility @1876 is never read as an edge). "
               "0x28 spawns nothing further, so the closure is one member at "
               "depth 1. 0x28 is ABSENT from GRAVE.BEF, so this route needs one "
               "injection; its blob is 752 B. "
               "ACTIVE (active=True) UNDER SEEDED SELECTION - see the WHY "
               "ACTIVATION IS NOW SAFE block below. Its roster_costs row is "
               "+509,119 .PRS / +103,504 .PRT for the GROUP (the +357,855 .PRS "
               "figure above is the enemy alone, before companion 0x28) and it "
               "is expected to COMMIT",
               active=True, companions=(0x28,)),
    AllowEntry(0x5D, "Doomed_Soul", "under", "grave", CANDIDATE,
               "MEASURED, newly passing after the opacity-map fix (UNDER.PRT "
               "entry 3 again). UNDER.BEF blob 0x5D's LOCAL CATEGORY-1 refs "
               "resolve to ONE distinct source mesh index, 23. UNDER.PRS "
               "mesh[23] signature (len, tag, bones, passes) = "
               "(146884, 6, 33, 29), a tag-6 PSkeleton with 33 bones. 11 "
               "distinct LOCAL CATEGORY-2 indices [54..64] and 11 anim records "
               "appended - the most of any route measured; 3 textures ported, "
               "all indexed (CLUT 34->36). Alone into a pristine GRAVE: .PRS "
               "+340,786, .PRT +198,704, mesh 39->40, anim 53->64, .PRT "
               "textures 35->38, verify_session passes all eight checks. HELD "
               "BACK (active=False) so the user's size bisect stays one "
               "variable at a time. COMPANIONS MEASURED AS NONE: its "
               "Script_Section (2,952 B, clean, 100% coverage) carries no Spawn "
               "call at all - only one SpawnAbility @2458, never read as an "
               "edge. Empty tuple = investigated, none; not None. "
               "ACTIVE (active=True) UNDER SEEDED SELECTION - see the WHY "
               "ACTIVATION IS NOW SAFE block below. Its roster_costs row is "
               "+340,786 .PRS / +198,704 .PRT and it is expected to COMMIT",
               active=True, companions=()),
    # --- WHY ACTIVATION IS NOW SAFE, WHEN IT WAS NOT DURING THE BISECT -------
    #
    # 0x16, 0x27 and 0x5D were flipped to active=True here, and 0x08 was NOT.
    # The reason activation stopped being dangerous is a change in what `active`
    # controls, not new evidence about these three routes.
    #
    # DURING THE BISECT there was NO BUDGET ENFORCEMENT ANYWHERE. load_allowlist()
    # returned the shipped set: every active route was ported into every
    # cross-world build, the resident .PRS+.PRT total was whatever that summed
    # to, and nothing checked it against the RESOURCE pool. Pool exhaustion is
    # invisible - MemoryPool::Allocate falls through and returns
    # sentinel_block + 16, so the caller corrupts allocator metadata and the game
    # dies at level load with no log line, no build failure and no test failure.
    # So `active` WAS the only size control there was, and holding routes back
    # was the only way to stay inside the pool. That is why the seven-route build
    # crashed and why four routes went to active=False.
    #
    # NOW admission bounds the LIVE resident total. randomizer/roster_selection.py
    # walks a seed-fixed order and admits a group ONLY while
    # len(session.prs) + len(session.prt) plus that route's MEASURED cost still
    # fits pool_budget(raised=...) = 90.4% of the pool in effect. The bound is a
    # MEASUREMENT taken between group commits, not a sum of predictions, so a
    # route that costs more than its row says cannot overshoot: the next
    # admission reads the true size. Therefore:
    #
    #     ACTIVATING A ROUTE NO LONGER MEANS IT SHIPS IN EVERY BUILD.
    #     A LARGER ALLOWLIST BUYS VARIETY, NOT SIZE.
    #
    # And variety is exactly what was missing: with only the seven routes that
    # were active before this, every seed converged on the SAME lineup, because
    # the limit was the ALLOWLIST and not the budget. With the ten measured
    # routes allowlisted, 40 sampled seeds produced 6-7 foreign arrivals, 22
    # distinct resident totals and a peak occupancy of 90.32% - inside the band
    # every loading build came in at.
    #
    # WHAT WAS *NOT* ACTIVATED, and why:
    #   * 0x08 Plant_Monster stays active=False. It is MEASURED TO REFUSE as a
    #     group on the prs_mesh_parser top-level resync gap (companion 0x0B needs
    #     CASTLE.PRS mesh[49]), exactly like 0x53 Dark_Knight. It could safely be
    #     active - a refusal costs an enemy, not a build, and the walk backfills
    #     the budget it did not spend - but it has never been in a build and one
    #     known refusal (0x53, which Requirement 5.5 keeps deliberately) is
    #     enough. Not suspicion: it is simply not useful yet.
    #   * 0x53 Dark_Knight stays active=True and stays refusing. NOT worked
    #     around (Requirement 5.5).
    #   * 0x10 Frozen_Zombie and 0x13 Goat_Devil get NO ENTRY AT ALL. Neither
    #     could be measured (an anim-rig bind failure and the same mesh resync
    #     gap), so roster_costs.cost_for returns None and the selector excludes
    #     them regardless (Requirement 3.6).
    #
    # NOTHING HERE IS PROMOTED. All ten active routes remain CANDIDATE. Bytes,
    # measurements and draws are not render confirmations; only the user seeing
    # an enemy render correctly promotes a route (Requirement 10.4).

    # =======================================================================
    # MULTI-WORLD TARGETS, STAGE 1: ICE and SWAMP.
    #
    # GRAVE STOPS BEING THE ONLY TARGET HERE, and nothing about the mechanism
    # changed to allow it. `plan_draw`, `selection_rng`, `AdmissionGate`,
    # `lineup_by_world`, `ported_types_by_world` and `sound_port.merge_for_lineup`
    # were already per-target-world; the reason only grave had entries was that
    # only grave had MEASURED COST ROWS. It now has company:
    # `roster_costs.ROUTE_COSTS` carries 13 rows into ICE and 10 into SWAMP,
    # every one driven fresh through the unmodified gated path against that
    # world's OWN pristine archives, one route per process.
    #
    # WHY THESE TWO AND NOT ALL FOUR (research/MULTI_WORLD_TARGET_BUDGET_
    # MEASUREMENT.md §5). MEASURED pool headroom against
    # `pool_budget(raised=True)` = 6,635,388 B: ICE 2,398,271 B (~4 median
    # arrivals, up to 10 cheap), SWAMP 1,937,684 B (~3, up to 8). UNDER
    # (1,641,427 B) and CASTLE (802,663 B) are LATER STAGES and deliberately have
    # NO ENTRIES AND NO ROWS -- an absent target cannot be drawn by accident.
    #
    # THE RESOURCE POOL NEEDED NO PER-WORLD CHANGE, and that was CONFIRMED
    # rather than assumed: the pool ceiling is ABSOLUTE (one hard-coded pool,
    # `MemoryManager::__ct` @0x00191F40) and a `WorldPortSession` starts at the
    # TARGET WORLD'S OWN vanilla size, so `AdmissionGate` reading the live
    # `len(prs) + len(prt)` self-adjusts: ICE starts 856,759 B fuller than grave
    # and therefore admits fewer arrivals, with no new constant.
    #
    # SOUND IS A DIFFERENT STORY AND IT IS A REFUSAL. MEASURED roundup64 bank
    # occupancies against the 1,966,080 B `SPU_USABLE_CEILING`: SWAMP.BD leaves
    # 13,888 B and ICE.BD leaves 3,584 B, so the existing per-unit ceiling check
    # refuses almost every import. That is CORRECT and is LOGGED; no ceiling and
    # no headroom constant was moved.
    #
    # THE FOUR FIRST-PORT ROUTES into ICE -- 0x2F, 0x37, 0x48, 0x6A, all GRAVE
    # natives never ported anywhere before -- ALL COMMITTED and all verify green.
    #
    # ONE ROUTE COMMITTED AND IS STILL NOT HERE: 0x08 Plant_Monster into ICE. It
    # ports, but `verify_session` reports appended ICE mesh[41] carrying
    # surviving CASTLE-local texture ids (46 where 48 was assigned, 47 where 49
    # was). The same route into GRAVE verifies clean, so this is a per-TARGET
    # finding. Its cost row exists and states the failure; NO ENTRY IS GRANTED,
    # which excludes it for every seed without inventing a new permission path.
    #
    # EVERYTHING BELOW IS CANDIDATE AND NOTHING IS PROMOTED. Nobody has watched
    # a single one of these render.
    # -----------------------------------------------------------------------
    # ICE
    # -----------------------------------------------------------------------
    AllowEntry(0x0D, "Zombie_Crocodile", "swamp", "ice", CANDIDATE,
               "MEASURED into ICE, COMMITS, verify_session green. +234,996 .PRS "
               "/ +66,576 .PRT / +2,632 .BEF against ICE's own pristine "
               "3,082,301 / 1,154,816. COMPANIONS MEASURED AS NONE (empty "
               "tuple = investigated and none, not None). The .BEF delta "
               "differs from the grave route's -8 because ICE declares no 0x0D, "
               "so the blob is a real injection. CANDIDATE: nobody has seen it "
               "render in ICE",
               active=True, companions=()),
    AllowEntry(0x13, "Goat_Devil", "under", "ice", CANDIDATE,
               "MEASURED into ICE, COMMITS, verify_session green: +442,604 .PRS "
               "/ +133,152 .PRT / +3,544 .BEF, companions measured as NONE. "
               "RE-ENABLED (active=True) BY EXPLICIT USER REQUEST, with the "
               "crash UNEXPLAINED. The observed failure stands: broken "
               "mesh/texture then a hard freeze, ALWAYS at spawn. No root "
               "cause was found and nothing was fixed; the user chose to ship "
               "it anyway and is testing it themselves. Re-arm the hold by "
               "putting 0x13 back into HELD_BACK_ENEMIES_ON_OBSERVED_CRASH and "
               "setting active=False. "
               "The evidence against this "
               "enemy is about the ENEMY and not about grave: the user watched "
               "the GRAVE route produce a broken mesh/texture and then a hard "
               "IN-GAME CRASH, always at spawn, with every structural check in "
               "this repo green. The two leading hypotheses were ruled out for "
               "that route and the same reasoning applies here. (1) THE "
               "MULTI-MESH TEXTURE-REMAP GAP IS NOT THE CAUSE HERE: this route "
               "verifies clean, so no surviving source-local texture id and no "
               "out-of-range id is present under either appended entry. (2) THE "
               "ANIM INDEX SPACE IS NOT THE CAUSE HERE: verify_session's "
               "reference_integrity check passes, so every LOCAL CATEGORY-1 and "
               "CATEGORY-2 ref lands inside ICE's own tables. Porting it into a "
               "second world is not evidence that it renders in either; it "
               "ships regardless because the user asked for it and is testing "
               "it themselves",
               active=True, companions=()),
    AllowEntry(0x16, "Axe_Guard", "castle", "ice", CANDIDATE,
               "MEASURED into ICE, COMMITS, verify_session green. +87,263 .PRS "
               "/ +138,288 .PRT / +1,144 .BEF -- the CHEAPEST .PRS route into "
               "ICE and the cheapest .BEF of the whole stage. COMPANIONS "
               "MEASURED AS NONE: its Script_Section names no Spawn edge. The "
               "grave route of this enemy has LOADED in game twice, which is "
               "evidence about the enemy's assets and NOT about ICE, so this "
               "entry stays CANDIDATE",
               active=True, companions=()),
    AllowEntry(0x19, "Swamp_Zombie", "swamp", "ice", CANDIDATE,
               "MEASURED into ICE, COMMITS, verify_session green. +377,851 .PRS "
               "/ +110,656 .PRT / +4,720 .BEF for the GROUP: the enemy plus "
               "companion 0x1A, which ICE.BEF does not declare, so it is a real "
               "injection here. CANDIDATE: no in-game confirmation in ICE",
               active=True, companions=(0x1A,)),
    AllowEntry(0x27, "Hammer_Devil", "under", "ice", CANDIDATE,
               "MEASURED into ICE, COMMITS, verify_session green. +509,119 .PRS "
               "/ +103,504 .PRT / +4,784 .BEF for the GROUP, including "
               "companion 0x28 (one Spawn site, closure one member at depth 1). "
               "CANDIDATE: no in-game confirmation in ICE",
               active=True, companions=(0x28,)),
    AllowEntry(0x2F, "Axe_Skeleton", "castle", "ice", CANDIDATE,
               "MEASURED into ICE, COMMITS, verify_session green. FIRST PORT OF "
               "THIS ENEMY ANYWHERE: 0x2F is a GRAVE native with no measured "
               "route into any world before this, so it carried first-port risk "
               "on top of measurement risk. +354,044 .PRS / +250,960 .PRT / "
               "+6,720 .BEF for the GROUP. SOURCE IS CASTLE, which also declares "
               "it natively and is what `source_candidates` picks; the "
               "grave-sourced variant is UNMEASURED and has no entry. Companion "
               "0x36 was DERIVED by roster.derive_companions, the oracle "
               "calibrated 11/11 against the hand-measured literals, because no "
               "entry described this route to copy from",
               active=True, companions=(0x36,)),
    AllowEntry(0x37, "Raven", "castle", "ice", CANDIDATE,
               "MEASURED into ICE, COMMITS, verify_session green. FIRST PORT OF "
               "THIS ENEMY ANYWHERE (0x37 is a GRAVE native). +239,086 .PRS / "
               "+33,808 .PRT / +4,936 .BEF -- THE CHEAPEST .PRT COST IN THE "
               "WHOLE DATA SET, grave rows included, which is consistent with a "
               "small flying enemy. COMPANIONS MEASURED AS NONE",
               active=True, companions=()),
    AllowEntry(0x48, "Basic_Zombie", "castle", "ice", CANDIDATE,
               "MEASURED into ICE, COMMITS, verify_session green. FIRST PORT OF "
               "THIS ENEMY ANYWHERE (0x48 is a GRAVE native). +172,619 .PRS / "
               "+117,808 .PRT / +6,160 .BEF -- THE CHEAPEST RESIDENT COST OF "
               "ANY MEASURED ROUTE at 290,427 B. Its DERIVED closure is 0x35 "
               "and 0x6A: 0x6A Torso_Zombie is itself a walking-melee enemy with "
               "its own separate ICE entry, and being INJECTED as a companion is "
               "not the same as being DRAWN, so the two coexist without either "
               "granting the other permission",
               active=True, companions=(0x35, 0x6A)),
    AllowEntry(0x53, "Dark_Knight", "castle", "ice", CANDIDATE,
               "MEASURED into ICE, COMMITS, verify_session green. +616,642 .PRS "
               "/ +166,960 .PRT / +4,416 .BEF for the GROUP -- the most "
               "expensive route into ICE. Companion 0x8C is included, and it is "
               "only extractable because prs_mesh_parser's field offsets were "
               "corrected against the engine's loader chain, which made "
               "CASTLE.PRS mesh[54] addressable. CANDIDATE, and note the enemy's "
               "GRAVE route rendered WRONG once under the discredited u16@22 "
               "splice, so nothing about this enemy is confirmed anywhere",
               active=True, companions=(0x8C,)),
    AllowEntry(0x58, "Crazed_Prisoner", "castle", "ice", CANDIDATE,
               "MEASURED into ICE, COMMITS, verify_session green. +411,500 .PRS "
               "/ +84,000 .PRT / +3,544 .BEF, identical .PRS/.PRT to the grave "
               "route because the appended bytes are the enemy's own mesh and "
               "world-local textures and neither target owns them. COMPANIONS "
               "MEASURED AS NONE",
               active=True, companions=()),
    AllowEntry(0x5D, "Doomed_Soul", "under", "ice", CANDIDATE,
               "MEASURED into ICE, COMMITS, verify_session green. +340,786 .PRS "
               "/ +198,704 .PRT / +3,832 .BEF. COMPANIONS MEASURED AS NONE: its "
               "Script_Section carries no Spawn call at all, only a SpawnAbility "
               "that is never read as an edge. CANDIDATE",
               active=True, companions=()),
    AllowEntry(0x6A, "Torso_Zombie", "castle", "ice", CANDIDATE,
               "MEASURED into ICE, COMMITS, verify_session green. FIRST PORT OF "
               "THIS ENEMY ANYWHERE (0x6A is a GRAVE native). +172,619 .PRS / "
               "+117,808 .PRT / +2,200 .BEF. The .PRS/.PRT figures equal 0x48 "
               "Basic_Zombie's because the two share a mesh and texture set -- "
               "0x48's group even injects this enemy as a companion -- and only "
               "the .BEF delta separates them, this route paying for one blob "
               "where 0x48 pays for three. Both were measured independently, in "
               "their own processes; neither figure was copied. COMPANIONS "
               "MEASURED AS NONE",
               active=True, companions=()),
    # -----------------------------------------------------------------------
    # SWAMP
    # -----------------------------------------------------------------------
    AllowEntry(0x10, "Frozen_Zombie", "ice", "swamp", CANDIDATE,
               "MEASURED into SWAMP, COMMITS, verify_session green. +253,253 "
               ".PRS / +239,712 .PRT / +6,792 .BEF for the GROUP, including "
               "companions 0x55 and 0x5C. This is the enemy whose ICE anim[41] "
               "MEA node name the anim port used to refuse; the tolerance that "
               "freed it is unchanged and still scoped to MEA records with at "
               "least one resolving name, and SKA records with an unmatched "
               "bone name still REFUSE. CANDIDATE",
               active=True, companions=(0x55, 0x5C)),
    AllowEntry(0x13, "Goat_Devil", "under", "swamp", CANDIDATE,
               "MEASURED into SWAMP, COMMITS, verify_session green: +442,604 "
               ".PRS / +133,152 .PRT / +3,544 .BEF, companions measured as "
               "NONE. RE-ENABLED (active=True) BY EXPLICIT USER REQUEST, with "
               "the crash UNEXPLAINED. The observed failure stands: broken "
               "mesh/texture then a hard freeze, ALWAYS at spawn. No root "
               "cause was found and nothing was fixed; the user chose to ship "
               "it anyway and is testing it themselves. Re-arm the hold by "
               "putting 0x13 back into HELD_BACK_ENEMIES_ON_OBSERVED_CRASH and "
               "setting active=False. Same evidence as the ICE row: "
               "the user watched this enemy's GRAVE route render broken and then "
               "hard-freeze on hardware -- an IN-GAME CRASH, always at spawn -- "
               "with every structural check green, and that is evidence about "
               "the ENEMY. Both leading hypotheses stay ruled out here: THE "
               "MULTI-MESH TEXTURE-REMAP GAP IS NOT THE CAUSE HERE (this route "
               "verifies clean, so no surviving source-local or out-of-range "
               "texture id survives under either appended entry) and THE ANIM "
               "INDEX SPACE IS NOT THE CAUSE HERE (reference_integrity passes, "
               "so every LOCAL CATEGORY-1/CATEGORY-2 ref lands inside SWAMP's "
               "own tables). Measuring a second and third target does not "
               "explain the freeze, and the freeze is still unexplained now "
               "that the route ships",
               active=True, companions=()),
    AllowEntry(0x16, "Axe_Guard", "castle", "swamp", CANDIDATE,
               "MEASURED into SWAMP, COMMITS, verify_session green. +87,263 "
               ".PRS / +138,288 .PRT / +1,144 .BEF -- the cheapest .PRS route "
               "into SWAMP. COMPANIONS MEASURED AS NONE. CANDIDATE: no in-game "
               "confirmation in SWAMP",
               active=True, companions=()),
    AllowEntry(0x1D, "Snowman", "ice", "swamp", CANDIDATE,
               "MEASURED into SWAMP, COMMITS, verify_session green. +359,462 "
               ".PRS / +189,536 .PRT / +4,464 .BEF for the GROUP: companions "
               "0x1E, 0x9B and 0xBA, including the 0x1E <-> 0xBA cycle that "
               "visit-once dedupe has to survive. It is also the only route of "
               "this stage that walks past an opacity-map .PRT entry (ICE.PRT "
               "entry 11), so the size rule for those entries is exercised "
               "against a second target",
               active=True, companions=(0x1E, 0x9B, 0xBA)),
    AllowEntry(0x27, "Hammer_Devil", "under", "swamp", CANDIDATE,
               "MEASURED into SWAMP, COMMITS, verify_session green. +509,119 "
               ".PRS / +103,504 .PRT / +4,784 .BEF for the GROUP with companion "
               "0x28. CANDIDATE: no in-game confirmation in SWAMP",
               active=True, companions=(0x28,)),
    AllowEntry(0x3A, "PirateSkeleton", "ice", "swamp", CANDIDATE,
               "MEASURED into SWAMP, COMMITS, verify_session green. +458,624 "
               ".PRS / +117,808 .PRT / +5,256 .BEF for the GROUP. THE CLEAREST "
               "DEMONSTRATION IN THE PROJECT THAT A COST ROW DOES NOT TRANSFER "
               "BETWEEN TARGETS: into GRAVE, companion 0x35 is already present "
               "in GRAVE.BEF and takes the reuse path, contributing zero on all "
               "three axes. SWAMP.BEF does not declare 0x35, so here it is a "
               "real injection; the .PRS/.PRT totals still match because 0x35's "
               "mesh and textures came back out of the session memos, and the "
               "difference lands on .BEF",
               active=True, companions=(0x35,)),
    AllowEntry(0x49, "Bomb_Skeleton", "castle", "swamp", CANDIDATE,
               "MEASURED into SWAMP, COMMITS, verify_session green. +532,307 "
               ".PRS / +225,472 .PRT / +8,696 .BEF -- the most .BEF-expensive "
               "route measured anywhere, because all FOUR companions are absent "
               "from SWAMP.BEF. The closure was DERIVED by "
               "roster.derive_companions rather than copied, and it agrees with "
               "the grave route's hand-measured set exactly; only the order "
               "differs, because the oracle returns it sorted. CANDIDATE, and "
               "worth remembering that this enemy's earlier GRAVE route froze "
               "the game when its mesh index came from the discredited u16@22 "
               "field",
               active=True, companions=(0x44, 0x5E, 0x63, 0xEB)),
    AllowEntry(0x53, "Dark_Knight", "castle", "swamp", CANDIDATE,
               "MEASURED into SWAMP, COMMITS, verify_session green. +616,642 "
               ".PRS / +166,960 .PRT / +4,416 .BEF for the GROUP including "
               "companion 0x8C -- the most expensive route into SWAMP. "
               "CANDIDATE: this enemy has never been confirmed rendering "
               "correctly in any world",
               active=True, companions=(0x8C,)),
    AllowEntry(0x58, "Crazed_Prisoner", "castle", "swamp", CANDIDATE,
               "MEASURED into SWAMP, COMMITS, verify_session green. +411,500 "
               ".PRS / +84,000 .PRT / +3,544 .BEF. COMPANIONS MEASURED AS NONE. "
               "CANDIDATE: no in-game confirmation in SWAMP",
               active=True, companions=()),
    AllowEntry(0x5D, "Doomed_Soul", "under", "swamp", CANDIDATE,
               "MEASURED into SWAMP, COMMITS, verify_session green. +340,786 "
               ".PRS / +198,704 .PRT / +3,832 .BEF. COMPANIONS MEASURED AS "
               "NONE: no Spawn call in its Script_Section at all. CANDIDATE",
               active=True, companions=()),

    # =======================================================================
    # MULTI-WORLD TARGETS, STAGE 2: UNDER and CASTLE -- the last two worlds.
    #
    # ALL FIVE WORLDS ARE TARGETS AS OF HERE, and again nothing about the
    # mechanism changed to allow it. `plan_draw(seed, target_world, ...)`,
    # `selection_rng`, `AdmissionGate`, `lineup_by_world`, `ported_types_by_world`
    # and the per-target loop in `cli.py` were already target-agnostic; what
    # these two worlds were missing was MEASURED COST ROWS, and
    # `roster_costs.ROUTE_COSTS` now carries 10 into UNDER and 8 into CASTLE.
    # Every one was driven fresh through the unmodified gated path with
    # `--target under` / `--target castle`, ONE ROUTE PER PROCESS, against that
    # world's OWN pristine archives. No grave row was re-labelled: a row is per
    # (enemy, source, TARGET) and a grave row does not transfer.
    #
    # MEASURED headroom against `roster_selection.pool_budget(raised=True)` =
    # 6,635,388 B. UNDER's vanilla resident total is 4,993,961 B (68%), leaving
    # 1,641,427 B -- about 2-3 median arrivals. CASTLE's is 5,832,725 B, which is
    # 79%, THE TIGHTEST WORLD IN THE GAME, leaving 802,663 B: one arrival always,
    # a second only for a cheap pair. THAT AFFORDABILITY WAS MEASURED INTO
    # CASTLE, not inferred from grave -- the two cheapest CASTLE-measured routes
    # are 0x0D (301,572 B) and 0x19 (488,507 B), 790,079 B together, fitting by
    # 12,584 B. Costs are not additive, so the live `len(prs) + len(prt)` at
    # admission is still the authority and no build relies on that sum.
    #
    # SOUND IS IMPOSSIBLE IN BOTH, AND BOTH ARE REFUSED IN THE SOUND PATH RATHER
    # THAN LEFT SILENTLY EMPTY (`sound_port.merge_for_lineup`, refusing by NAME
    # OF THE MEASUREMENT, not by name of the world). UNDER.BD's roundup64
    # occupancy is 1,975,104 B, 9,024 B ABOVE `sound_bank.SPU_USABLE_CEILING`
    # BEFORE any import. CASTLE.BD is 10,688 B above it AND carries nSFX 299
    # against the driver's hard `< 300` (`sound_bank.MAX_SFX`), i.e. ZERO free
    # entry slots, so an import there is structurally impossible at ANY byte
    # size. NO CONSTANT WAS RAISED and no check was skipped: the reverb-off
    # argument that would legalise these banks is INFERRED from a call-graph
    # absence, and the StreamMemoryEnd writers at SLUS_200.17 VA 0x00141714 /
    # 0x00141858 are untraced. BOTH WORLDS ARE ASSETS ONLY.
    #
    # ONE MEASURED ROUTE GETS NO ENTRY: 0x08 Plant_Monster into UNDER. It
    # commits, but `verify_session` reports appended UNDER mesh[40] carrying
    # surviving CASTLE-local texture ids (46 where 48 was assigned, 47 where 49
    # was) -- the SAME defect the ICE route shows, and absent from the GRAVE
    # route, so it is a per-TARGET finding. Its cost row exists and states the
    # failure; no entry is granted, which excludes it for every seed.
    #
    # 0x13 Goat_Devil IS ELIGIBLE INTO CASTLE AND IS NOW SHIPPED, ON EXPLICIT
    # USER REQUEST, WITH ITS CRASH STILL UNEXPLAINED. Its entry below carries
    # active=True. The enemy-level hold mechanism
    # (`HELD_BACK_ENEMIES_ON_OBSERVED_CRASH`) is still wired and still
    # target-independent; it is simply EMPTY, so re-arming the hold is one
    # integer plus active=False.
    #
    # EVERYTHING BELOW IS CANDIDATE AND NOTHING IS PROMOTED. Nobody has watched a
    # single one of these render.
    # -----------------------------------------------------------------------
    # UNDER
    # -----------------------------------------------------------------------
    AllowEntry(0x0D, "Zombie_Crocodile", "swamp", "under", CANDIDATE,
               "MEASURED into UNDER, COMMITS, verify_session green. +234,996 "
               ".PRS / +66,576 .PRT / +2,632 .BEF against UNDER's own pristine "
               "3,641,513 / 1,352,448. THE CHEAPEST RESIDENT ROUTE INTO UNDER "
               "at 301,572 B. COMPANIONS MEASURED AS NONE (empty tuple = "
               "investigated and none, not None). CANDIDATE: nobody has seen it "
               "render in UNDER",
               active=True, companions=()),
    AllowEntry(0x10, "Frozen_Zombie", "ice", "under", CANDIDATE,
               "MEASURED into UNDER, COMMITS, verify_session green. +253,253 "
               ".PRS / +239,712 .PRT / +6,792 .BEF for the GROUP, including "
               "companions 0x55 and 0x5C. The MEA-node-name tolerance this "
               "enemy depends on is unchanged and still scoped to MEA records "
               "with at least one resolving name; SKA records with an unmatched "
               "bone name still REFUSE. CANDIDATE",
               active=True, companions=(0x55, 0x5C)),
    AllowEntry(0x16, "Axe_Guard", "castle", "under", CANDIDATE,
               "MEASURED into UNDER, COMMITS, verify_session green. +87,263 "
               ".PRS / +138,288 .PRT / +1,144 .BEF -- the cheapest .PRS route "
               "into UNDER, as it is into every other target. COMPANIONS "
               "MEASURED AS NONE. The grave route of this enemy has LOADED in "
               "game twice, which is evidence about the enemy's assets and NOT "
               "about UNDER, so this entry stays CANDIDATE",
               active=True, companions=()),
    AllowEntry(0x19, "Swamp_Zombie", "swamp", "under", CANDIDATE,
               "MEASURED into UNDER, COMMITS, verify_session green. +377,851 "
               ".PRS / +110,656 .PRT / +4,720 .BEF for the GROUP: the enemy "
               "plus companion 0x1A, which UNDER.BEF does not declare. "
               "CANDIDATE",
               active=True, companions=(0x1A,)),
    AllowEntry(0x1D, "Snowman", "ice", "under", CANDIDATE,
               "MEASURED into UNDER, COMMITS, verify_session green. +359,462 "
               ".PRS / +189,536 .PRT / +4,464 .BEF for the GROUP: companions "
               "0x1E, 0x9B and 0xBA, including the 0x1E <-> 0xBA cycle that "
               "visit-once dedupe has to survive. CANDIDATE",
               active=True, companions=(0x1E, 0x9B, 0xBA)),
    AllowEntry(0x37, "Raven", "castle", "under", CANDIDATE,
               "MEASURED into UNDER, COMMITS, verify_session green. +239,086 "
               ".PRS / +33,808 .PRT / +4,936 .BEF -- THE CHEAPEST .PRT COST IN "
               "THE WHOLE DATA SET, consistent with a small flying enemy. 0x37 "
               "is a GRAVE NATIVE and was this stage's nominated first-port "
               "risk, but the ICE stage already ported it for the first time "
               "anywhere from the SAME source: `source_candidates` sorts the "
               "mesh-owning worlds and CASTLE precedes GRAVE. The "
               "grave-sourced variant is UNMEASURED and has no entry. "
               "COMPANIONS MEASURED AS NONE",
               active=True, companions=()),
    AllowEntry(0x3A, "PirateSkeleton", "ice", "under", CANDIDATE,
               "MEASURED into UNDER, COMMITS, verify_session green. +458,624 "
               ".PRS / +117,808 .PRT / +5,256 .BEF for the GROUP with companion "
               "0x35, which UNDER.BEF DOES declare, so the companion takes the "
               "REUSE path here as it does into grave. CANDIDATE",
               active=True, companions=(0x35,)),
    AllowEntry(0x53, "Dark_Knight", "castle", "under", CANDIDATE,
               "MEASURED into UNDER, COMMITS, verify_session green. +616,642 "
               ".PRS / +166,960 .PRT / +4,416 .BEF for the GROUP including "
               "companion 0x8C -- THE MOST EXPENSIVE ROUTE INTO UNDER at "
               "783,602 B resident, about half the world's whole 1,641,427 B "
               "headroom, so a seed that draws it gets little else. Companion "
               "0x8C is only extractable because prs_mesh_parser's field "
               "offsets were corrected against the engine's loader chain. "
               "CANDIDATE: this enemy has never been confirmed rendering "
               "correctly in any world",
               active=True, companions=(0x8C,)),
    AllowEntry(0x58, "Crazed_Prisoner", "castle", "under", CANDIDATE,
               "MEASURED into UNDER, COMMITS, verify_session green. +411,500 "
               ".PRS / +84,000 .PRT / +3,544 .BEF. COMPANIONS MEASURED AS NONE. "
               "CANDIDATE",
               active=True, companions=()),
    # -----------------------------------------------------------------------
    # CASTLE -- 79% full in retail, so this is the world where the budget, not
    # the allowlist, does almost all of the deciding.
    # -----------------------------------------------------------------------
    AllowEntry(0x0D, "Zombie_Crocodile", "swamp", "castle", CANDIDATE,
               "MEASURED into CASTLE, COMMITS, verify_session green. +234,996 "
               ".PRS / +66,576 .PRT / +2,632 .BEF against CASTLE's own pristine "
               "4,199,541 / 1,633,184. THE CHEAPEST RESIDENT ROUTE INTO CASTLE "
               "at 301,572 B, and therefore the only route that can leave room "
               "for a second arrival in the tightest world in the game. "
               "COMPANIONS MEASURED AS NONE. CANDIDATE",
               active=True, companions=()),
    AllowEntry(0x10, "Frozen_Zombie", "ice", "castle", CANDIDATE,
               "MEASURED into CASTLE, COMMITS, verify_session green. +253,253 "
               ".PRS / +239,712 .PRT / +6,792 .BEF for the GROUP with "
               "companions 0x55 and 0x5C. CANDIDATE",
               active=True, companions=(0x55, 0x5C)),
    AllowEntry(0x13, "Goat_Devil", "under", "castle", CANDIDATE,
               "MEASURED into CASTLE, COMMITS, verify_session green: +442,604 "
               ".PRS / +133,152 .PRT / +3,544 .BEF, companions measured as "
               "NONE. RE-ENABLED (active=True) BY EXPLICIT USER REQUEST, with "
               "the crash UNEXPLAINED. The observed failure stands: broken "
               "mesh/texture then a hard freeze, ALWAYS at spawn. No root "
               "cause was found and nothing was fixed; the user chose to ship "
               "it anyway and is testing it themselves. Re-arm the hold by "
               "putting 0x13 back into HELD_BACK_ENEMIES_ON_OBSERVED_CRASH and "
               "setting active=False -- the enemy-level mechanism is still "
               "wired and stays TARGET-INDEPENDENT, covering worlds nobody has "
               "measured yet. "
               "The user watched this enemy's GRAVE route render broken and "
               "then hard-freeze on hardware -- an IN-GAME CRASH, always at "
               "spawn -- with every structural check in this repo green. This "
               "is now the FOURTH target measured (grave, ice, swamp, castle), "
               "all four with byte-identical deltas and all four verifying "
               "green, which is evidence that the defect is in the ENEMY or its "
               "PORT and not in any target world. Both leading hypotheses stay "
               "ruled out here: THE MULTI-MESH TEXTURE-REMAP GAP IS NOT THE "
               "CAUSE HERE (this route verifies clean, so no surviving "
               "source-local or out-of-range texture id survives under either "
               "appended entry) and THE ANIM INDEX SPACE IS NOT THE CAUSE HERE "
               "(reference_integrity passes, so every LOCAL CATEGORY-1 and "
               "CATEGORY-2 ref lands inside CASTLE's own tables). A fourth "
               "clean measurement is not an explanation, and the root cause is "
               "still not found",
               active=True, companions=()),
    AllowEntry(0x19, "Swamp_Zombie", "swamp", "castle", CANDIDATE,
               "MEASURED into CASTLE, COMMITS, verify_session green. +377,851 "
               ".PRS / +110,656 .PRT / +4,720 .BEF for the GROUP with companion "
               "0x1A. At 488,507 B resident it is the second cheapest route "
               "into CASTLE and the other half of the only affordable PAIR the "
               "measurement admits (with 0x0D, 790,079 B against 802,663 B of "
               "headroom). CANDIDATE",
               active=True, companions=(0x1A,)),
    AllowEntry(0x1D, "Snowman", "ice", "castle", CANDIDATE,
               "MEASURED into CASTLE, COMMITS, verify_session green. +359,462 "
               ".PRS / +189,536 .PRT / +4,464 .BEF for the GROUP: companions "
               "0x1E, 0x9B and 0xBA, including the 0x1E <-> 0xBA cycle. "
               "CANDIDATE",
               active=True, companions=(0x1E, 0x9B, 0xBA)),
    AllowEntry(0x27, "Hammer_Devil", "under", "castle", CANDIDATE,
               "MEASURED into CASTLE, COMMITS, verify_session green. +509,119 "
               ".PRS / +103,504 .PRT / +4,784 .BEF for the GROUP with companion "
               "0x28 (one Spawn site, closure one member at depth 1). At "
               "612,623 B resident it fits CASTLE's headroom alone but leaves "
               "no room for anything else. CANDIDATE",
               active=True, companions=(0x28,)),
    AllowEntry(0x3A, "PirateSkeleton", "ice", "castle", CANDIDATE,
               "MEASURED into CASTLE, COMMITS, verify_session green. +458,624 "
               ".PRS / +117,808 .PRT / +5,256 .BEF for the GROUP with companion "
               "0x35, which CASTLE.BEF DOES declare, so the companion takes the "
               "REUSE path. CANDIDATE",
               active=True, companions=(0x35,)),
    AllowEntry(0x5D, "Doomed_Soul", "under", "castle", CANDIDATE,
               "MEASURED into CASTLE, COMMITS, verify_session green. +340,786 "
               ".PRS / +198,704 .PRT / +3,832 .BEF. COMPANIONS MEASURED AS "
               "NONE: its Script_Section carries no Spawn call at all, only a "
               "SpawnAbility that is never read as an edge. CANDIDATE",
               active=True, companions=()),
)


#: Routes held back for a reason that is NOT a measured refusal: the user
#: OBSERVED THEM CRASH OR MISRENDER ON HARDWARE while every structural check in
#: this repo stayed green.
#:
#: Until now there was exactly one legitimate reason to park a route -- it is
#: measured to REFUSE -- because under seeded selection admission bounds the
#: live resident total, so activating a committing route costs variety, not
#: size. That rule is still right for SIZE. It is wrong for CORRECTNESS: a
#: route can commit, verify green on all eight checks, and still hang the
#: console, and when that happens the only honest state is HELD BACK. A route
#: listed here MUST carry ``active=False`` and a note that says what was
#: measured and what was ruled out, so the hold-back stays a recorded
#: hypothesis rather than a shrug.
#:
#: THE SET IS CURRENTLY EMPTY, AND NOT BECAUSE THE DEFECT WAS UNDERSTOOD.
#: `0x13` Goat_Devil was its ONLY member. It was REMOVED ON EXPLICIT USER
#: INSTRUCTION: the user asked for the goat back knowing it hard-froze their
#: console at spawn, and is testing it themselves. NO ROOT CAUSE WAS FOUND AND
#: NOTHING WAS FIXED. The mechanism and its rationale below are UNCHANGED and
#: stay fully wired, so the hold can be re-armed in one line:
#: put the enemy id back in this frozenset (``frozenset({0x13})``) and set its
#: AllowEntry rows back to ``active=False``.
#:
#: ROOT CAUSE FOUND FOR THE NON-ATTACKING GOAT (a SEPARATE defect from the
#: spawn freeze below, and this one IS fixed). The anim port collected only the
#: indices carried by cat-2 material refs. Goat_Devil's refs are anims
#: 77, 79, 80, 81, 82, 84, 85, 86, 87, 88 -- a CONTIGUOUS 77..88 block with
#: HOLES at 78 and 83 -- while its script drives `SeqPlay` slots 0..12. A clip
#: reached only through a `SeqPlay` slot number carries no material ref, so 78
#: and 83 were dropped; worse, dropping interior entries COMPACTED the block, so
#: every clip after each hole shifted down and every later slot played the wrong
#: animation. That is the "animation that attacks me is missing" report.
#: Measured: anims 78 and 83 are referenced by NO entry in UNDER.BEF, both
#: extract cleanly (9,207 B and 16,724 B), and between them they address ZERO
#: bone names absent from the ten records already shipping -- so they bind
#: exactly as well and add no refusal risk. `companion_port` now fills interior
#: holes in an entity's own referenced range, restricted to indices no other
#: entry references, which leaves another entity's index alone (e.g. UNDER 0x5E
#: fills nothing and leaves 54..64 untouched). Cost: +25,931 B .PRS for the goat.
#:
#: `0x13` Goat_Devil, as recorded: broken mesh/texture then a hard freeze, ALWAYS at spawn.
#: Its render-pass texture ids and its LOCAL CATEGORY-1/CATEGORY-2 refs were
#: both measured to be fully inside GRAVE's index space under BOTH appended
#: mesh entries, its 10 appended anim records are all BY_NAME with every name
#: resolving, and its companion closure is provably empty. No mechanism found;
#: see the entry's note for the figures.
#:
#: THE HOLD IS ON THE ENEMY, SO IT FOLLOWS THE ENEMY -- AND IT IS NOW WRITTEN
#: THAT WAY. This used to be a literal set of `(enemy, source, target)` TRIPLES,
#: which had a defect that only showed up as targets were added: the hold had to
#: be RE-TYPED for every new world, and a route into a world nobody had listed
#: yet was permitted by DEFAULT. That is backwards for an unexplained hard
#: freeze. The rule is stated once, on the ENEMY, below.
#:
#: WHY ENEMY-LEVEL IS THE HONEST SCOPE. `0x13` has now been measured into FOUR
#: targets (grave, ice, swamp, castle). All four COMMIT with byte-identical
#: deltas and all four pass every `verify_session` check -- which is exactly the
#: state the grave route was in when it hard-froze the console. So the evidence
#: says the defect is NOT a property of the target world and NOT anything the
#: structural checks can see; it is in the enemy or in its port. Porting it into
#: a fifth world would therefore very likely reproduce the freeze, and a clean
#: measurement is not an explanation. That reasoning is unchanged: anything
#: added here ships NOWHERE, for every world, including worlds not yet
#: measured. `0x13` is no longer here only because the user overrode the hold.
HELD_BACK_ENEMIES_ON_OBSERVED_CRASH: frozenset[int] = frozenset()


def is_held_back_on_observed_crash(type_id: int) -> bool:
    """True when this ENEMY is held back everywhere on an observed crash.

    TARGET-INDEPENDENT BY CONSTRUCTION. Callers must not special-case a world:
    the reason the hold exists is that the world was ruled out as the cause.
    """
    return int(type_id) in HELD_BACK_ENEMIES_ON_OBSERVED_CRASH


#: Every allowlisted ROUTE covered by the enemy-level hold, DERIVED rather than
#: typed, so adding a target cannot silently narrow the hold. Kept because the
#: route-keyed form is what the tests and the notes talk about. A route in here
#: MUST carry ``active=False`` and a note saying what was measured and what was
#: ruled out, so the hold stays a recorded hypothesis rather than a shrug.
HELD_BACK_ON_OBSERVED_CRASH: frozenset[tuple[int, str, str]] = frozenset(
    entry.key for entry in CROSS_WORLD_ALLOWLIST
    if entry.enemy_id in HELD_BACK_ENEMIES_ON_OBSERVED_CRASH)


def load_allowlist() -> dict[tuple[int, str, str], AllowEntry]:
    """Return the ACTIVE allowlist keyed by (enemy_id, source_world, target_world).

    The built-in CROSS_WORLD_ALLOWLIST entries with ``active=True`` only. An
    entry with ``active=False`` is defined and documented but takes no part in
    a build: it never reaches allowlist_lookup, so no placement can be granted
    on it, for every seed (Requirement 10.3).

    WHAT ``active`` MEANS NOW, AND WHY IT CHANGED
    --------------------------------------------
    This used to be the SHIPPED SET: every active route was ported into every
    cross-world build, so ``active`` doubled as a size control and the set was
    kept deliberately small while the GRAVE level-load crash was bisected. That
    is no longer true. ``randomizer/roster_selection.py`` draws a SEED-CHOSEN,
    BUDGET-BOUNDED subset of the allowlist per build, and admission bounds the
    LIVE resident total (``len(session.prs) + len(session.prt)``) against
    ``pool_budget`` before each group commits. So this dict is now the set of
    routes a seed MAY draw, not the set a build ships:

        a larger allowlist buys VARIETY, not size.

    That is precisely what the bisect era lacked - back then there was no budget
    enforcement of any kind, so activating a route did grow every build and a
    seventh route did crash the console. The bound, not the flag, is what keeps a
    build inside the pool now.

    THE ACTIVE SET IS 0x0D, 0x16, 0x19, 0x1D, 0x27, 0x3A, 0x49, 0x53, 0x58 AND
    0x5D - the ten allowlisted routes into GRAVE that ``roster_costs`` MEASURED.
    Nine of them commit; 0x53 Dark_Knight is measured to REFUSE as a group and
    stays active deliberately, because a refusal costs an enemy and never a
    build (Requirement 5.1) and the walk's next candidate backfills the budget
    it did not spend (Requirement 5.4).

    HELD BACK: 0x08 Plant_Monster only. It is measured to refuse as a group on
    the same ``prs_mesh_parser`` top-level resync gap as 0x53 (its companion
    0x0B needs CASTLE.PRS mesh[49]), and unlike 0x53 it has never been in a
    build at all, so it stays out until someone wants a second known refusal in
    the draw. Nothing about it is suspected; it is simply not useful yet.

    NOT ALLOWLISTED AT ALL: 0x10 Frozen_Zombie and 0x13 Goat_Devil. Both were
    driven and neither could be measured (an anim-rig bind failure and the mesh
    resync gap respectively), so ``roster_costs.cost_for`` returns ``None`` for
    them and the selector excludes them anyway (Requirement 3.6). An entry would
    add nothing but a second way to say no.

    STATUS IS UNTOUCHED BY ANY OF THIS. Every entry is CANDIDATE. Measuring a
    route, activating it and drawing it are all evidence about BYTES; only the
    user watching an enemy render correctly promotes anything to CONFIRMED
    (Requirement 10.4).

    THE BISECT HISTORY, kept because it cost a lot to learn. 0x16 Axe_Guard was
    step one and that build LOADED; 0x3A PirateSkeleton was step two and that
    build LOADED too, at +1.79 MB .PRS / +0.61 MB .PRT. Adding 0x1D Snowman on
    top of it crashed, but that was CONFOUNDED with size, so 0x3A - a pure size
    probe - was held back to make Snowman the only new variable. That five-route
    build (0x0D, 0x16, 0x1D, 0x49, 0x53, +1,663,193 .PRS / +609,504 .PRT) LOADED,
    which exonerated Snowman and attributed the six-route crash to cumulative
    size. Every held-back decision in that history was taken WITHOUT a budget;
    with one, the same routes are safe to allowlist because no build ships all
    of them.

    A user-supplied JSON overlay that could add CANDIDATE entries is deferred
    to (optional) Task 9 and is deliberately NOT implemented here.

    CURRENT STATE, WHICH SUPERSEDES THE GRAVE-ONLY PARAGRAPHS ABOVE. The
    allowlist now spans THREE TARGET WORLDS: 13 routes into GRAVE, 12 into ICE
    and 10 into SWAMP. 0x10 Frozen_Zombie and 0x13 Goat_Devil DO have entries
    (both became measurable -- the first through an ELF measurement of the
    engine's own bind loop, the second through the prs_mesh_parser field-offset
    correction), and 0x08 Plant_Monster's GRAVE route commits and is active. The
    only routes held back are 0x13's THREE, all listed in
    HELD_BACK_ON_OBSERVED_CRASH: it commits and verifies green everywhere and
    still froze the console in game.

    The key is per (enemy, source, TARGET), so the same enemy can be permitted
    into several worlds independently, and `allowlist_lookup(enemy, target)`
    resolves per target.

    STAGE 2 ADDED UNDER AND CASTLE, so ALL FIVE WORLDS ARE NOW TARGETS: 13
    routes into GRAVE, 12 into ICE, 10 into SWAMP, 9 into UNDER and 7 into
    CASTLE. The paragraph above that said UNDER and CASTLE have no entries is
    superseded -- what they lacked was MEASURED COST ROWS, and
    `roster_costs.ROUTE_COSTS` now carries 10 rows into UNDER and 8 into CASTLE,
    each driven fresh through the unmodified gated path against that world's own
    pristine archives. Two measured routes still get NO ENTRY:
    `(0x08, "castle", "under")`, which commits but fails `verify_session` on
    surviving source-local texture ids exactly as it does into ICE, and every
    `0x13` route, held back on the enemy-level observed-crash rule below.

    THE OBSERVED-CRASH HOLD IS ENFORCED HERE TOO, ON THE ENEMY. An entry for an
    enemy in ``HELD_BACK_ENEMIES_ON_OBSERVED_CRASH`` is dropped whatever its
    ``active`` flag says, for every target world including ones nobody has
    measured yet. The data already says ``active=False`` for all of them; this is
    the second lock, so a future entry cannot hand that enemy a permission by a
    single-field slip.
    """
    return {entry.key: entry for entry in CROSS_WORLD_ALLOWLIST
            if entry.active
            and entry.enemy_id not in HELD_BACK_ENEMIES_ON_OBSERVED_CRASH}


def drawn_allowlist(
    drawn_keys,
    allowlist: dict[tuple[int, str, str], AllowEntry] | None = None,
) -> dict[tuple[int, str, str], AllowEntry]:
    """The PER-RUN subset of the allowlist: only the routes this seed drew.

    ``drawn_keys`` is an iterable of ``(enemy_id, source_world, target_world)``
    keys, normally ``AdmissionResult.drawn_keys`` or a ``DrawPlan``'s order.
    The result is fed straight into ``run_cross_world_placements``'s existing
    ``allowlist`` parameter, so the selector PRODUCES an allowlist and adds NO
    second permission path: ``allowlist_lookup`` remains the single oracle and
    is untouched.

    A key that is not in the active allowlist is DROPPED, not invented. This is
    a filter, never a grant: a route the seed somehow proposed but that nobody
    verified (or that is held back with ``active=False``) cannot be admitted by
    passing through here.
    """
    if allowlist is None:
        allowlist = load_allowlist()
    wanted = list(drawn_keys)
    return {key: allowlist[key] for key in wanted if key in allowlist}


def allowlist_lookup(
    enemy_id: int,
    target_world: str,
    allowlist: dict[tuple[int, str, str], AllowEntry] | None = None,
) -> AllowEntry | None:
    """The single per-route permission oracle for cross-world placement.

    Returns the allowlist entry permitting enemy_id to be placed INTO
    target_world, or None when no such crossing has been verified. Callers
    MUST treat None as "not allowed" — there is no other permission check.

    If several entries permit the same enemy into the same target world from
    different source worlds, the one whose source_world sorts first is
    returned. The choice is arbitrary but fixed, so a run is reproducible
    regardless of dict/tuple ordering changes.
    """
    if allowlist is None:
        allowlist = load_allowlist()
    matches = [
        entry for entry in allowlist.values()
        if entry.enemy_id == enemy_id and entry.target_world == target_world
    ]
    if not matches:
        return None
    return min(matches, key=lambda e: e.source_world)


# ---------------------------------------------------------------------------
# Cross-world STRUCTURE allowlist (Task 4 of `cross-world-structures`)
# ---------------------------------------------------------------------------
#
# Same `AllowEntry` shape as the enemy allowlist above, deliberately: Task 1
# measured the port path to be ENTITY-GENERIC -- nothing on
# `port_enemy` / `_port_enemy` inspects what kind of entity it is handling, and
# `enemy_id` is a NAME, not a discriminator (it is interpolated into logs and
# used as the BEF TYPE ID queued for append-only injection). So a structure
# route is described by the same record, with `enemy_id` read as an ENTITY id.
#
# THIS TABLE GRANTS NOTHING TODAY. There is still no switch; the structure draw
# does not exist yet (Task 5). `load_allowlist` and `allowlist_lookup` above are
# untouched and never see these rows, so the enemy permission oracle and every
# existing build are byte-for-byte unaffected. This is data plus its own lookup.
#
# EVERY ROW IS `CANDIDATE` AND NOTHING HERE MAY BE `CONFIRMED` (Req 4.4). Only
# the user's in-game observation promotes a route, and in-memory verification is
# not an observation. `_structure_allowlist` asserts that invariant at import.
#
# WHAT IS NOT HERE, AND WHY (Req 4.2, Req 4.5, Req 2.5, Req 3.1)
#   92 routes have been driven in all: Task 3's 64, of which 62 committed and
#   verified, plus the 28 PHASE-2 routes, of which all 28 did. This table carries
#   all 90.
#     * The only 2 absences are MEASURED AND REFUSED: `0x6C` Coin_Container
#       under->grave and under->swamp both ported and then failed
#       `verify_session` on surviving source-world texture ids (refusal S5). A
#       refused route gets no row, and that stays true -- a refusal is EVIDENCE,
#       not policy, so it is not the user's to waive.
#   A missing row is therefore an EXCLUSION for every seed, which is stronger
#   than `active=False` and needs no second permission path.
#
# THE 16 WITHHELD ROUTES ARE NOW HERE, BECAUSE THE HOLD WAS POLICY.
#   This table used to carry 46. `0x17` Smashable_Snow_Pirate, `0xAD`
#   Spirit_Torch, `0x50` Standing_Coffin and `0xBC` Smashable_Coffin were held
#   back with all 16 of their routes MEASURED CLEAN and `verify_session` green
#   -- the hold was this repo's caution about what a foreign copy might DO in
#   game, not any port failure. The user asked for these four explicitly and
#   ACCEPTED THE RISK that one breaks in game, so their rows are admitted.
#   Every one is `CANDIDATE` and UNOBSERVED, and what is still unsettled is
#   recorded per source in `roster.STRUCTURE_HOLD_LIFTED`: for `0x17` / `0xAD`,
#   WHICH game var index their single `GameVarSet` touches and whether any world
#   reads it; for `0x50` / `0xBC`, WHAT A FOREIGN COFFIN ACTUALLY DROPS
#   (design.md open question 2). Their closures' progression anchors `0x2B`
#   Gold_Key and `0x65` were REUSED, never injected, so the PORT adds no anchor
#   to any world -- the drop is the open question, not the port.
#
#   THIS WIDENS SOURCES ONLY. No destination protection is weakened by it:
#   PROTECTED_TAGS, PROTECTED_INSTANCE_NAMES, the `end*` kill-groups, the
#   Gold_Key / Level_Column / Skeleton_Key / Monster_Generator anchors and
#   `items.HAZARD_PROTECTED_AS_SOURCE` all still run unchanged in the item pass,
#   and `HELD_BACK_ENEMIES_ON_OBSERVED_CRASH` is untouched.
#
#   THE PHASE-2 SEVEN ARE NOW MEASURED, SO THEY HAVE ROWS: 62 -> 90.
#   `0x01`, `0x02`, `0x0A` (progression, Req 3.4) and `0xAE`, `0xB5`, `0xC8`,
#   `0xD3` (Ordered_Puzzle_Props, Req 3.3) had their POLICY hold lifted with
#   nothing measured, so Req 4.2 kept them out of this table. All 28 of their
#   routes have since been driven through the same unmodified gated path, one per
#   child process: 28 COMMITTED, `verify_session` green on all 28, ZERO refusals.
#   The rows appear here BECAUSE the cost rows exist, not because the hold went;
#   the derivation below is what makes that structural.
#   MEASUREMENT ANSWERS ONLY THE ASSET QUESTION. Whether a foreign Bone Tower
#   assembles, where a foreign Bonus Grave leads, whether a Popup Headstone ever
#   pops up, and whether an arrival disturbs an S_SUB3 / U_SUB3 numbered chain
#   are all still open, recorded per source in `roster.STRUCTURE_HOLD_LIFTED`.
#   Every one of the 28 is `CANDIDATE` and UNOBSERVED.
#
#   STILL EXCLUDED, and not part of this: `0x5F` Monster_Generator (GLOBAL
#   assets, Req 2.4), whose exclusion is MEASURED and not the user's to waive.


def _structure_allowlist() -> tuple[AllowEntry, ...]:
    """Build one `CANDIDATE` row per shippable structure route.

    The evidence strings and the route set are the MEASURED data in
    `roster_costs.STRUCTURE_ROUTE_COSTS`, which is Task 3's `measurements.md`
    transcribed. Deriving the rows from the cost table rather than retyping them
    is what makes Requirement 4.2 structural: a route cannot have an allowlist
    row without a cost row, because the row IS the cost row's note.

    `roster_costs` imports nothing from this package, so this edge is acyclic;
    the import is local anyway so `catalog` keeps its module-level import list
    as it was.
    """
    from . import roster_costs

    rows = []
    for cost in roster_costs.STRUCTURE_ROUTE_COSTS:
        rows.append(AllowEntry(
            cost.type_id,
            _STRUCTURE_NAMES[cost.type_id],
            cost.source_world,
            cost.target_world,
            CANDIDATE,
            cost.measured_note
            + ". OUTSTANDING IN-GAME OBSERVATIONS, owed for every CANDIDATE: "
              "does it render, does it break and animate, does breaking it "
              "drop what it should, does it fit the geometry, and does the "
              "level still complete",
            active=True,
            companions=tuple(cost.companions)))
    assert all(row.status == CANDIDATE for row in rows), (
        "Req 4.4: a structure route may never be written CONFIRMED")
    return tuple(rows)


#: Structure names, in the underscored shape the log lines use. Kept here rather
#: than derived from `spawn_config` so `catalog` gains no new module dependency;
#: `tests/test_structure_allowlist.py` pins them against
#: `spawn_config.STRUCTURE_TYPES` so they cannot drift.
_STRUCTURE_NAMES: dict[int, str] = {
    0x01: "Bone_Tower",
    0x02: "Bonus_Grave",
    0x0A: "Popup_Headstone",
    0x17: "Smashable_Snow_Pirate",
    0x18: "Ice_Torch",
    0x1B: "Popup_Ice",
    0x30: "Grave_Smashable_Glass",
    0x4A: "Globe",
    0x50: "Standing_Coffin",
    0x6C: "Coin_Container",
    0x7C: "Ice_Plant",
    0xA0: "Coffin_Lid",
    0xAA: "Smashable_Door",
    0xAD: "Spirit_Torch",
    0xAE: "Underworld_Lantern",
    0xB0: "Smashable_Torch",
    0xB1: "Smashable_Ice_Wall",
    0xB5: "Swamp_Coffin_Lid",
    0xBB: "Thorn_Vine",
    0xBC: "Smashable_Coffin",
    0xC8: "Spirit_Statue",
    0xC9: "Breakable_Rockwall",
    0xD3: "Breakable_Tiki_Torch",
}

#: 90 rows: every measured structure route that committed and verified -- 62 from
#: Task 3's 64, plus the 28 PHASE-2 routes. The only two absences are the
#: MEASURED-AND-REFUSED `0x6C` pair. Was 46 until the POLICY hold on `0x17` /
#: `0xAD` / `0x50` / `0xBC` was lifted, then 62 until the phase-2 seven were
#: measured.
CROSS_WORLD_STRUCTURE_ALLOWLIST: tuple[AllowEntry, ...] = _structure_allowlist()

#: 90, asserted at import so a slip in the cost table cannot quietly shrink the
#: permission table with it.
STRUCTURE_ALLOWLIST_ROW_COUNT: int = 90
assert len(CROSS_WORLD_STRUCTURE_ALLOWLIST) == STRUCTURE_ALLOWLIST_ROW_COUNT, (
    f"expected {STRUCTURE_ALLOWLIST_ROW_COUNT} structure allowlist rows, got "
    f"{len(CROSS_WORLD_STRUCTURE_ALLOWLIST)}")


def load_structure_allowlist() -> dict[tuple[int, str, str], AllowEntry]:
    """The ACTIVE structure allowlist, keyed by (type_id, source, target).

    A SEPARATE oracle from `load_allowlist` on purpose. A structure key has the
    same shape as an enemy key, so merging the two tables would let the enemy
    permission oracle start returning structures -- a behaviour change, and one
    Requirement 2.3 forbids in spirit (a structure must never fill an enemy
    slot). Keeping them apart leaves the existing path identical.
    """
    return {entry.key: entry for entry in CROSS_WORLD_STRUCTURE_ALLOWLIST
            if entry.active}


def structure_allowlist_lookup(
    type_id: int,
    target_world: str,
    allowlist: dict[tuple[int, str, str], AllowEntry] | None = None,
) -> AllowEntry | None:
    """The per-route permission oracle for cross-world STRUCTURE placement.

    Returns the row permitting `type_id` into `target_world`, or `None` when no
    such crossing is measured-and-shippable. `None` MEANS NOT ALLOWED. Ties on
    source world resolve to the first-sorting source, matching
    `allowlist_lookup`, so a run stays reproducible.
    """
    if allowlist is None:
        allowlist = load_structure_allowlist()
    matches = [entry for entry in allowlist.values()
               if entry.enemy_id == int(type_id)
               and entry.target_world == target_world]
    if not matches:
        return None
    return min(matches, key=lambda e: e.source_world)


# CROSS-WORLD universal pool: ALL walking-melee enemy types. Only usable when
# cross-world BEF injection is active (every world's BEF has all enemy blobs).
UNIVERSAL_WALKING_MELEE = set(WALKING_MELEE_POOL.keys())

# Enemies whose mesh/texture resources are in WORLD-SPECIFIC PRS files.
# These CANNOT be injected into other worlds via cross-world mode — their
# visuals (and often animation/texture-page indices) only exist in their
# home world's resource pack, so injecting them elsewhere and forcing
# GLOBAL resource loading makes the engine look up a mesh/texture that was
# never loaded, crashing (ejecting the disc on PCSX2) as soon as that
# world's map tries to render the foreign enemy.
#
# IMPORTANT CORRECTION: every world actually ships its own .PRS/.PRT pair
# on the retail disc (GRAVE.PRS, UNDER.PRS, ICE.PRS/ICE_B.PRS, SWAMP.PRS/
# SWAMP_B.PRS, CASTLE.PRS/CASTLE_K.PRS/CASTLE_Q.PRS), in addition to
# GLOBAL.PRS. An earlier version of this comment claimed ICE/SWAMP/CASTLE
# had "no separate PRS" and treated their world-exclusive monsters as
# cross-world-safe — that was never verified and is contradicted by the
# files on disc. Any enemy that's only native to 1-2 worlds is almost
# certainly stored in that world's own PRS, not GLOBAL.PRS, and must be
# locked here. Only enemies natively used in 3+ worlds (strong evidence of
# a shared/global mesh, since it would be wasteful to duplicate the same
# monster's assets into 3+ separate per-world PRS files) are left unlocked.
# DEPRECATED / UNUSED. Kept only as an empty set so any stale import keeps
# working. It is NOT part of the eligibility path any more: cross-world
# permission now comes from allowlist_lookup(), never from subtracting a
# lock set. Do not add ids here — add an AllowEntry instead.
PRS_LOCKED_ENEMIES: set[int] = set()
# PRS_LOCKED_ENEMIES was previously populated with world-exclusive enemies
# based on the assumption that "only in 1-2 world BEFs = mesh in world PRS only".
# This heuristic was proven WRONG by GRAVE_CROSSWORLD.BEF:
#   - 0x0D (Zombie_Crocodile) is only in SWAMP.BEF but works perfectly in
#     GRAVE_CROSSWORLD.BEF (zero byte modifications to the blob).
#   - 0x16 (Axe_Guard) is only in CASTLE.BEF and also works in GRAVE_CROSSWORLD.
# Both enemies render correctly because ALL enemy meshes are in GLOBAL.PRS,
# not in world-specific PRS files. World-specific PRS files contain only
# level geometry, particles, and prop textures — not enemy meshes.
# inject_cross_world_enemies copies blobs as-is (remap_blob_textures is a no-op),
# so every enemy in the pool is safe to inject into every world's BEF.

# OVER-APPROXIMATING PRE-FILTER. NEVER A PERMISSION ORACLE.
#
# This is the target-BLIND union of every enemy id that appears anywhere in
# CROSS_WORLD_ALLOWLIST. It answers "is this enemy allowed to cross into ANY
# world at all", NOT "may it cross into THIS world". Membership here does not
# permit a single placement.
#
# Why flattening is nonetheless safe here:
#   (a) It can only ever NARROW a candidate pool by intersection; it cannot
#       grant a placement, because nothing downstream places an enemy on the
#       strength of this set alone.
#   (b) Real permission is decided exactly once, route-aware, by
#       allowlist_lookup(enemy_id, target_world).
#
# Any caller that needs a world-correct answer MUST use
# get_walking_melee_for_world(world, cross_world=True) (or allowlist_lookup
# directly). Trusting a flat, target-blind set is precisely the bug class that
# put unported enemies into maps — the old
# `UNIVERSAL_WALKING_MELEE - PRS_LOCKED_ENEMIES` derivation admitted all 21
# walking-melee types into every world. Do not reintroduce it.
# Derived from the ACTIVE entries only, so a held-back (active=False) route
# cannot even reach the pre-filter. Keeping it a superset of load_allowlist()
# preserves the "over-approximate, never grant" property above.
CROSS_WORLD_SAFE_ENEMIES = {e.enemy_id for e in CROSS_WORLD_ALLOWLIST if e.active}


# Per-type spawn weights for the even-out bag in cli.py.
# Default is 1.0 for every type. Lowered values mean a type appears LESS
# often in the balanced bag (slot count = round(weight * N / total_weight)).
#
# Raven, Ghost, and Torso_Zombie get reduced weight because:
#   - Raven has a small AI sight radius (vanilla) and often feels passive,
#     so an even share of Ravens makes maps feel under-populated.
#   - Ghost and Torso_Zombie are visually prominent floating/wandering enemies
#     and feel over-represented at full share even when statistically equal.
WALKING_MELEE_WEIGHTS = {
    0x37: 0.2,  # Raven         (reduced further — user reported too many)
    0x6B: 0.3,  # Ghost         (reduced further — user reported too many)
    0x6A: 0.2,  # Torso_Zombie  (reduced further — user reported too many)
    0x54: 0.4,  # Guard_Skeleton
    # Everything else defaults to 1.0
}


# Enemy instance-name patterns that are EVENT-TIED to level progression.
# These enemies trigger gates / level finisher / boss doors when killed —
# replacing them with foreign types that don't fire the death event will
# soft-lock the level. Match is substring + lowercase.
# Enemy instance-name patterns that are EVENT-TIED to level progression.
#
# This list is now EMPTY. Empirical testing + reverse-engineering confirmed
# that the engine wires kill events by instance_name + instance_id (which we
# always preserve when type-swapping), NOT by entity type. So any walking-melee
# enemy that dies in a 'gate1' / 'endgate1' / 'caveexit' slot fires the same
# event. Type-swapping is fully safe.
#
# Original protected patterns (now disabled): caveexit, final, goal, boss.
# All of them turned out to be triggered by the SAME instance-name event, so
# any walking-melee type works as a stand-in.
EVENT_ENEMY_NAME_PATTERNS: tuple = ()

# Specific instance names that ARE event-tied (override list). Empty.
EVENT_TIED_INSTANCE_NAMES = frozenset()


def is_event_tied_enemy(instance_name: str) -> bool:
    """True if this enemy's instance name suggests it's tied to a level event
    (level finisher, gate trigger, etc). Such enemies must NOT be replaced
    with foreign types — only swapped with other native walking-melee types
    whose death events behave the same way.

    Patterns are deliberately narrow now. The earlier 'end' / 'gate' substrings
    were over-protective: they matched regular kill-room enemies (end1..endN,
    cursedend, gatepath1, etc.) that don't actually fire any script trigger.
    Only enemies whose names map to KNOWN gate / level-finisher triggers are
    protected.
    """
    n = instance_name.lower()
    if n in EVENT_TIED_INSTANCE_NAMES:
        return True
    return any(pat in n for pat in EVENT_ENEMY_NAME_PATTERNS)


# Item/treasure pool: chests, coins, etc. Probably swappable within a category.
TREASURE_POOL = {
    0x21: "GoldCoin",
    0x46: "Bag_Of_Gold",
    0x6C: "CoinContainer",  # underworld
    0x4E: "Extra_Life",
    0x65: "Skeleton_Key",
    0x25: "Wooden_Chest",
    0x5B: "Locked_Chest",
}

# Trigger/control pool — typically NOT randomized (would break level flow)
TRIGGER_POOL = {
    0x1F: "Trigger",
    0xA3: "CamTrigger",
    0xD7: "Rain_Trigger",
    0xAF: "TriggerOnHit",
}


def get_world_from_bef_path(bef_path: str) -> str | None:
    """Identify a world from its BEF reference path."""
    bef_lower = bef_path.lower()
    if "grave" in bef_lower:
        return "grave"
    if "under" in bef_lower:
        return "under"
    if "swamp" in bef_lower:
        return "swamp"
    if "ice" in bef_lower:
        return "ice"
    if "castle" in bef_lower:
        return "castle"
    return None


def get_walking_melee_for_world(world: str, cross_world: bool = False,
                                lineup=None) -> set[int]:
    """Get available walking-melee type IDs for a world.

    cross_world=False -> the world's native set.
    cross_world=True  -> the native set UNION the enemies the allowlist
                         permits to ARRIVE in this specific world (entries
                         whose target_world == world). Deliberately NOT the
                         flat CROSS_WORLD_SAFE_ENEMIES, which is target-blind.

    ``lineup``, when supplied, is the SEED-DRAWN lineup for this world: the
    committed foreign arrivals plus the drawn natives
    (``AdmissionResult.lineup``). It RESTRICTS the pool to exactly those types,
    which is how the drawn natives actually take effect in the per-record pool -
    without it the placer would keep drawing from every native the world owns
    and the native half of the draw (Requirement 4.8) would have no effect.

    ``lineup`` NARROWS and never grants. It is intersected with nothing, because
    it is BUILT from routes ``allowlist_lookup`` already permitted plus natives
    that need no permission at all; permission is still decided exactly once,
    route-aware, by ``allowlist_lookup``. Passing an empty lineup returns an
    empty set on purpose - "the seed drew nothing" is a real answer and must not
    silently fall back to every native.

    With ``lineup`` omitted the behaviour is byte-for-byte what it was before
    this argument existed, on both the native and the cross-world branch.

    Always returns a fresh set, so a caller mutating the result cannot corrupt
    the module-level catalog sets (the previous version returned the bare
    module set for the native branches).
    """
    if lineup is not None:
        return set(int(t) for t in lineup)
    native = set(_NATIVE_SETS_BY_WORLD.get(world, {0x47, 0x48}))
    if not cross_world:
        return native
    arrivals = {
        entry.enemy_id
        for entry in load_allowlist().values()
        if entry.target_world == world
    }
    return native | arrivals


def name_for_type(type_id: int) -> str:
    """Get readable name for a type id (looks across all pools)."""
    for pool in (WALKING_MELEE_POOL, TREASURE_POOL, TRIGGER_POOL):
        if type_id in pool:
            return pool[type_id]
    return f"Unknown_0x{type_id:02X}"
