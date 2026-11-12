"""
Maximo: Ghosts to Glory randomizer CLI.

Usage:
  python -m randomizer.cli randomize <psx_folder> --output <out_folder> [--seed N] [--items]
  python -m randomizer.cli stats <psx_file>
  python -m randomizer.cli list-types <psx_folder>
"""
from __future__ import annotations
import argparse
import json
import random
import struct
import sys
import io
from pathlib import Path

# Force stdout to UTF-8 to handle non-ASCII paths in PSX file BEF references.
# In a PyInstaller --windowed build, sys.stdout/sys.stderr are None — skip the
# wrapping in that case (the GUI redirects them to its log widget anyway).
if sys.stdout is not None and sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    if sys.stderr is not None:
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

from . import boss_arena
from . import boss_inject
from .psx import PsxFile, make_record_with_template
from .catalog import (
    WALKING_MELEE_POOL,
    GRAVE_WALKING_MELEE,
    UNDER_WALKING_MELEE,
    WALKING_MELEE_WEIGHTS,
    CROSS_WORLD_SAFE_ENEMIES,
    get_world_from_bef_path,
    get_walking_melee_for_world,
    name_for_type,
    is_event_tied_enemy,
    is_native as catalog_is_native,
)
from .items import (
    aggressive_item_randomize,
    LAST_UNIVERSAL_STATS,
    GAME_ABILITY_POOL,
    UNIVERSAL_ITEM_POOL,
    ENEMY_DESTINATIONS,
    reroll_enemy_tier,
    reroll_enemy_drop,
    reroll_enemy_variant,
    force_enemy_props,
    _boost_raven_aggro,
    get_gold_key_protected_names,
    place_start_gold_key,
    place_extra_keys,
    place_fixed_gold_keys,
    START_GOLD_KEY_FILES,
    EXTRA_KEY_FILES,
    FIXED_GOLD_KEY_TARGETS,
    LIFT_PROTECTION_FILES,
    GOLD_KEY_TYPE,
    collect_world_gen_templates,
    MONSTER_GENERATOR_TYPE,
    ENEMY_TO_GENERATOR_PROBABILITY,
    _is_hub_generator,
    GATE_TYPES,
    duplicate_boss,
    BOSS_DUPLICATION,
    BOSS_FILE_WORLD,
    randomize_player_spawn,
    randomize_exit_location,
    swap_spawn_with_exit,
    remove_all_gates,
    move_record_to,
    apply_harder_mode,
)
from .elf_patch import (patch_level_titles, apply_level_titles, patch_ability_drops,
                        ABILITY_DROP_NAMES,
                        patch_damage_taken, patch_damage_dealt, find_executable,
                        patch_starting_inventory, patch_starting_skills,
                        patch_randomize_levels, patch_shield_cost,
                        resolve_shield_cost, SHIELD_COST_PRESETS,
                        SHIELD_COST_MAX, SHIELD_COST_VANILLA,
                        patch_life_cost, resolve_life_cost,
                        LIFE_COST_PRESETS, LIFE_COST_VANILLA,
                        patch_perma_death,
                        patch_sword_cost, resolve_sword_cost,
                        SWORD_COST_MULTIPLIERS, SWORD_COST_VANILLA_MULT,
                        patch_effect_durations, resolve_effect_duration,
                        EFFECT_DURATION_PRESET_LABELS,
                        EFFECT_DURATION_VANILLA_MULT,
                        EFFECT_TICKS_PER_SECOND,
                        patch_koin_fee, KOIN_FEE_VANILLA,
                        KOIN_FEE_MIN, KOIN_FEE_MAX,
                        patch_disable_collision,
                        patch_widescreen,
                        patch_object_budget, resolve_object_budget,
                        OBJECT_BUDGET_PRESETS, OBJECT_BUDGET_VANILLA,
                        OBJECT_BUDGET_MIN, OBJECT_BUDGET_MAX,
                        OBJECT_BUDGET_SAFE_MAX,
                        patch_resource_pool, resolve_resource_pool,
                        RESOURCE_POOL_PRESETS, RESOURCE_POOL_VANILLA,
                        RESOURCE_POOL_MIN, RESOURCE_POOL_MAX,
                        RESOURCE_POOL_RAISED, RESOURCE_POOL_HIGH,
                        RESOURCE_POOL_LOADED_MAX, RESOURCE_POOL_CRASHED_MIN,
                        patch_level_timer, resolve_level_timer,
                        LEVEL_TIMER_PRESET_NAMES, LEVEL_TIMER_DUNGEON_ROW,
                        STARTING_SKILL_MASKS, KNOWN_EXECUTABLES)
from .spawn_config import (load_spawn_config, ITEM_TYPES as SPAWN_ITEM_NAMES,
                           STRUCTURE_TYPES as SPAWN_STRUCT_NAMES,
                           ENEMY_TYPES as SPAWN_ENEMY_NAMES)


# All worlds the randomizer knows about.
ALL_WORLDS = ("grave", "under", "swamp", "ice", "castle")

# Boss-arena PSX files. Skipped by the randomizer — boss rooms are
# scripted encounters and the boss instance carries event-tied state we
# don't want to touch (e.g. GraveDigger in G_BOSS.PSX).
BOSS_FILES = frozenset({
    "G_BOSS.PSX",  # GraveDigger
    "S_BOSS.PSX",  # Bokor LaBas
    "I_BOSS.PSX",  # Captain Cadaver
    "U_BOSS.PSX",  # Lord Glutterscum
    "C_KING.PSX",  # King (castle boss — duplicatable)
    "C_QUEEN.PSX", # Queen (castle final boss — protected from randomization)
})


def psx_record_size_at(psx: "PsxFile", offset: int) -> int:
    """Get the size of the record at this offset in the source PSX."""
    for r in psx.records:
        if r.offset == offset:
            return r.size
    return 0


# ----------------------------------------------------------------------
# THE PLACEMENT-ELIGIBILITY SEAM
#
# These three helpers used to be closures inside `run_randomizer`, which made
# the single most important property of cross-world mode untestable: that a
# type which was PORTED actually becomes ELIGIBLE FOR PLACEMENT. Porting was
# covered in isolation (buffers grow, bytes exact, budget holds) while the
# permitted pool silently stayed at the world's natives, so nothing foreign
# ever appeared in game and every test still passed.
#
# They are module level now so tests exercise the SAME functions the build
# runs. `run_randomizer` calls them; it no longer reimplements them.
# ----------------------------------------------------------------------
def committed_ported_types(session) -> set[int]:
    """Enemy type ids whose port actually COMMITTED in this world session.

    The session's own results are the authority — the same source the
    admission walk reads to decide COMMIT vs REFUSE. Deliberately NOT the
    drawn order and NOT the placement list: a REFUSED route (Dark_Knight
    0x53, measured to refuse) must never become permitted, or the placer
    retypes a record to an enemy whose mesh was never ported and the engine
    resolves unrelated geometry or runs off the end of the mesh table.

    Companion types are intentionally absent: a companion is not a
    ``results`` plan of its own, and companions are SPAWNED AT RUNTIME by
    their parent rather than placed by a level record, so admitting one to
    the retype pool would place a parentless companion.
    """
    return {r.plan.enemy_id for r in getattr(session, "results", ())
            if getattr(r, "ok", False)}


def catalog_permitted_pool_types(world: str, all_pool_types,
                                 ported=frozenset()) -> set[int]:
    """Pool types the catalog considers safe to place in `world`.

    Enemy types must be native to the world (per the catalog's measured
    native table) or ported into it this run. Non-enemy pool types
    (props/smashables/pickups) have no catalog native table; they stay
    permitted here and are filtered by the BEF intersection in the caller.
    """
    permitted = set(all_pool_types)
    ported = set(ported)
    for tid in set(all_pool_types):
        if tid in ENEMY_DESTINATIONS or tid in WALKING_MELEE_POOL:
            if not catalog_is_native(tid, world) and tid not in ported:
                permitted.discard(tid)
    return permitted


def enemy_pool_for_world(world: str, *, permitted, lineup=None,
                         excluded=frozenset(),
                         disabled=frozenset()) -> set[int]:
    """The per-record enemy pool for `world`.

    `permitted` is the asset guard's verdict (BEF-declared AND catalog-safe /
    ported-this-run); None means "no usable BEF", which yields an empty pool
    rather than everything. `lineup` is the seed-drawn lineup and can only
    NARROW: it is intersected with `permitted`, so a type the guard rejects
    can never be admitted by having been drawn.
    """
    if permitted is None:
        permitted = set()
    if lineup:
        pool = set(permitted) & get_walking_melee_for_world(
            world, cross_world=True, lineup=lineup)
    else:
        pool = set(permitted) & set(WALKING_MELEE_POOL.keys())
    return (pool - set(excluded)) - set(disabled)


def enemy_source_types(world: str, *, permitted, excluded=frozenset(),
                       disabled=frozenset()) -> set[int]:
    """Which record types COUNT AS a swappable enemy slot in `world`.

    The SOURCE set, and deliberately NOT `enemy_pool_for_world`'s result. The
    destination pool is narrowed by the seeded lineup, because the lineup is the
    roster a world SHIPS -- what an enemy may be turned INTO. Deciding what may
    be turned FROM with that same narrowed set is what broke enemy
    randomization: a Grave draw whose lineup came out all-foreign (6 foreign, 0
    native) made the source set six types that do not occur anywhere in a
    vanilla Grave map, so not one record was eligible, not one swap happened,
    and every ported mesh had nothing to attach to. A seed that drew a single
    native produced the same bug in weaker form -- only that one vanilla type
    was swappable, so foreign arrivals landed a handful of times and never in a
    map lacking it.

    Source eligibility is a property of the map and the asset guard, never of
    the draw, so no `lineup` parameter exists here -- there is nothing to pass
    one to. `excluded` still applies: an opted-out type such as Dark_Knight
    stays where vanilla put it.
    """
    return enemy_pool_for_world(world, permitted=permitted, lineup=None,
                                excluded=excluded, disabled=disabled)



# ----------------------------------------------------------------------
# CHALLENGE MODE: HOLY GROUND (checkpoint) SUPPRESSION
#
# Holy_Ground (type 0x07, class_name "Holy_Ground") is the mid-level
# checkpoint shrine: App::SetHolyGround stores it on touch and
# App::RestoreHolyGround puts Maximo back there on death/reload. 48 records
# exist across the 33 level files; hubs, boss arenas and skyboxes have none.
#
# When the Challenge Mode level timer is armed, a checkpoint anchor defeats
# the point of the clock (die, respawn mid-level, keep going), so every
# Holy_Ground is retyped into an inert entity.
#
# Why RETYPE and not delete: psx.py's writer works from an
# offset -> replacement map over the EXISTING record list. Removing a record
# would change the record count, which the writer does not support.
#
# Why the destination template must come from the SAME file: the engine
# resolves an entity's mesh through the world's BEF, and a type the BEF never
# loaded is a null-pointer lookup (a hard crash on level load). A template
# taken from a record that already exists in this very file is loadable by
# construction — no BEF table needs consulting and no type can be guessed
# wrong.
HOLY_GROUND_TYPE = 0x07
# Destination candidates, in preference order. All of them are looked up IN THE
# FILE BEING PROCESSED — a candidate that isn't there is simply skipped, so
# whatever is chosen is BEF-loadable by construction.
#
# Two tiers, ordered by PREFERENCE only — there is no affordability test.
#
# Tier 1, the invisible ambience volumes, is PREFERRED because nothing visible
# appears where the shrine used to be. Each is a 0x2A8 record against
# Holy_Ground's 0x248, i.e. +0x60 bytes per record; that cost NO LONGER MATTERS
# because the records section is allowed to grow (see
# RECORDS_SECTION_SIZE_CEILING in psx.py) — that is the whole point of the
# change. Fog_Volume is first and therefore wins whenever the file has one.
#
# Tier 2, the same-size pickups, remains purely for TEMPLATE AVAILABILITY:
# some files (confirmed: C_SUB1.PSX) contain no invisible emitter of any kind,
# and the destination template MUST come from a record that already exists in
# THIS file so the engine can resolve its mesh through the world's BEF. A
# visible coin where a shrine used to be is strictly better than a checkpoint
# that survives Challenge Mode.
HOLY_GROUND_INERT_INVISIBLE = (
    0xCE,  # Fog_Volume               — invisible ambience volume (preferred)
    0xF2,  # Ambient_Sound            — invisible sound emitter
    0xE7,  # Ambient_Waterfall_Noise  — invisible sound emitter
    0xF6,  # BGAnim_Sound             — invisible sound emitter
)
HOLY_GROUND_INERT_SAME_SIZE = (
    0x21,  # GoldCoin                 — visible pickup, availability fallback
    0x46,  # Bag_Of_Gold              — visible pickup, availability fallback
    0x52,  # Gem                      — visible pickup, availability fallback
)


def _report_records_growth(map_name: str, stats: dict) -> None:
    """Report how much this map's records section grew (Req 5.3 / 6.2).

    Printed as its OWN line, two-space indented like the other per-file lines
    in this subsystem, so a level-load crash can be correlated with the exact
    map and byte figure. `ELF patch:` is reserved for executable patches and is
    deliberately not used here.
    """
    growth = stats.get("records_growth") or 0
    if not growth:
        return
    print(f"  {map_name}: records section {growth:+,} bytes "
          f"(0x{stats['old_post_ptr']:X} -> 0x{stats['new_post_ptr']:X})")


def suppress_holy_ground(
    psx: "PsxFile",
    existing_repl: dict[int, "PsxRecord"] | None = None,
) -> tuple[dict[int, "PsxRecord"], int, int | None]:
    """Retype every Holy_Ground record in `psx` to an inert same-file entity.

    Returns (replacements, byte_delta, template_type_id); `byte_delta` is
    purely informational. The template is chosen from records that ALREADY
    EXIST in this file, so the engine can always resolve its mesh. The first
    invisible emitter present in this file wins, regardless of the resulting
    size increase; if the file has none, a same-size visible pickup is used as a
    template-availability fallback.

    When the file has no Holy_Ground, or has one but no inert template of its
    own, the replacement map is empty and template_type_id is None — the caller
    reports that rather than borrowing a type from another world.
    """
    existing_repl = existing_repl or {}
    targets = [r for r in psx.records
               if r.type_id == HOLY_GROUND_TYPE and r.offset not in existing_repl]
    if not targets:
        return {}, 0, None

    template = None
    template_type = None
    for tid in HOLY_GROUND_INERT_INVISIBLE + HOLY_GROUND_INERT_SAME_SIZE:
        recs = psx.find_records_by_type(tid)
        if not recs:
            continue
        template = recs[0]
        template_type = tid
        break
    if template is None:
        return {}, 0, None

    repl: dict[int, "PsxRecord"] = {}
    delta = 0
    for r in targets:
        new_rec = make_record_with_template(template, r, r.offset)
        delta += new_rec.size - r.size
        repl[r.offset] = new_rec
    return repl, delta, template_type


def cmd_stats(args) -> None:
    """Show statistics for a PSX file."""
    psx = PsxFile.parse(Path(args.psx_file))
    print(f"File: {psx.path.name}")
    print(f"Size: {len(psx.raw):,} bytes")
    print(f"BEF reference: {psx.bef_path}")
    world = get_world_from_bef_path(psx.bef_path)
    print(f"Detected world: {world or '(unknown)'}")
    print(f"Records: {len(psx.records)}")
    print(f"Records start: 0x{psx.records_start:X}")
    print(f"Record count field (0x{psx.record_count_offset:X}): {psx.record_count_field}")
    print(f"Post-records ptr (0x{psx.post_records_ptr_offset:X}): 0x{psx.post_records_ptr:X}")

    from collections import Counter
    types = Counter(r.type_id for r in psx.records)
    print(f"\nEntity types ({len(types)} unique):")
    for tid, count in sorted(types.items()):
        sample = psx.find_records_by_type(tid)[0]
        print(f"  0x{tid:02X}  {count:3} x {sample.class_name:30s}  size 0x{sample.size:X}")


def cmd_list_types(args) -> None:
    """List all entity types found across all PSX files in a folder."""
    folder = Path(args.psx_folder)
    # On Windows, glob is case-insensitive; deduplicate by resolved path
    seen = set()
    psx_files = []
    for p in sorted(folder.glob("*.PSX")) + sorted(folder.glob("*.psx")):
        rp = p.resolve()
        if rp not in seen:
            seen.add(rp)
            psx_files.append(p)
    if not psx_files:
        print(f"No PSX files in {folder}")
        return

    type_world_map: dict[int, dict[str, list[str]]] = {}
    for path in psx_files:
        try:
            psx = PsxFile.parse(path)
        except Exception as e:
            print(f"Skipping {path.name}: {e}")
            continue
        world = get_world_from_bef_path(psx.bef_path) or "unknown"
        for r in psx.records:
            type_world_map.setdefault(r.type_id, {}).setdefault(world, []).append(
                f"{path.name}({r.class_name})"
            )

    print(f"Found {len(type_world_map)} entity types across {len(psx_files)} files:\n")
    for tid in sorted(type_world_map):
        worlds = type_world_map[tid]
        # Pick a representative class name
        any_files = next(iter(worlds.values()))
        sample_name = any_files[0].split("(")[1].rstrip(")")
        worlds_str = ", ".join(sorted(worlds.keys()))
        total = sum(len(f) for f in worlds.values())
        print(f"  0x{tid:02X}  {sample_name:30s}  worlds=[{worlds_str}]  total={total}")


def cmd_randomize(args) -> None:
    """Randomize enemies in all PSX files in a folder."""
    in_folder = Path(args.psx_folder)
    out_folder = Path(args.output)
    out_folder.mkdir(parents=True, exist_ok=True)
    # EVERY OPTIONAL ATTRIBUTE IS READ WITH getattr AND THE SAME DEFAULT
    # ARGPARSE USES. cmd_randomize has two callers with different namespace
    # shapes: __main__'s argparse Namespace (which carries a default for every
    # flag) and gui.py's hand-written `_Args`, which sets ONLY the keywords
    # listed at its construction site and has no `__getattr__`, so any
    # attribute the list forgets raises AttributeError instead of falling back.
    # Reading through getattr makes the two entry points behave identically and
    # keeps a new flag from breaking the GUI before anyone notices.
    _seed_arg = getattr(args, "seed", None)
    seed = _seed_arg if _seed_arg is not None else random.randint(1, 999999)
    no_enemies = bool(getattr(args, "no_enemies", False))
    rng_master = random.Random(seed)

    psx_files = sorted(in_folder.glob("*.PSX")) + sorted(in_folder.glob("*.psx"))
    # Dedup (Windows filesystem is case-insensitive)
    _seen = set()
    _deduped = []
    for p in psx_files:
        rp = p.resolve()
        if rp not in _seen:
            _seen.add(rp)
            _deduped.append(p)
    psx_files = _deduped
    if not psx_files:
        print(f"ERROR: No PSX files in {in_folder}")
        sys.exit(1)

    # Resolve which worlds to randomize.
    selected_worlds = _resolve_worlds(getattr(args, "worlds", None))

    print(f"Randomizer running with seed: {seed}")
    print(f"Source: {in_folder}")
    print(f"Output: {out_folder}")
    print(f"PSX files found: {len(psx_files)}")
    print(f"Worlds enabled: {', '.join(sorted(selected_worlds))}")
    all_enemies_mode = getattr(args, "all_enemies", False)
    dup_bosses = getattr(args, "duplicate_bosses", False)
    # HARDER MODE: a single switch that enables a curated "harder" profile —
    # disabled gold/gem/health/life drops (folded into koins), disabled
    # structures (their share spawned as enemies), rarer keys/collectors/
    # skills, more item->enemy rolls, far more empty enemy drops, all enemies
    # at max class, mostly-1-koin chests, mimic/wizard at 33% each, boss
    # duplication (2 clones in Grave and Swamp), Maximo takes 2x damage and
    # deals 0.5x. (Dark_Knight is intentionally NOT excluded.)
    harder_mode = bool(getattr(args, "harder_mode", False))
    # Preserve all vanilla chests (keep their type/position) but still randomize
    # what's inside them. Independent of harder mode; composes with the other
    # category toggles.
    preserve_chests = bool(getattr(args, "preserve_chests", False))
    preserve_iron_keys = bool(getattr(args, "preserve_iron_keys", False))
    forced_chest_special: dict | None = None
    if harder_mode:
        hm = apply_harder_mode()
        print("HARDER MODE ON — "
              f"koins+{hm['koins_share_from_disabled']} from disabled drops, "
              f"enemy-share {hm['enemy_share_target']:.0%}, "
              f"enemy-drop {hm['enemy_drop_probability']:.0%}, max class, "
              "structures disabled, mimic/wizard 33% each, "
              "boss +1 clone (Grave+Swamp+Ice+Under+Castle), damage taken 2x / dealt 0.5x")
        dup_bosses = True
        forced_chest_special = {"mimic": 33, "wizard": 33}
    # Per-boss clone counts. Grave (GraveDigger) and Swamp (BokorLaBas) are
    # configured separately; each falls back to the legacy single
    # `boss_clones` value, then to 1.
    _legacy_clones = getattr(args, "boss_clones", 1) or 1
    dup_count_grave = getattr(args, "boss_clones_grave", None)
    dup_count_grave = _legacy_clones if dup_count_grave is None else dup_count_grave
    dup_count_swamp = getattr(args, "boss_clones_swamp", None)
    dup_count_swamp = _legacy_clones if dup_count_swamp is None else dup_count_swamp
    dup_count_ice = getattr(args, "boss_clones_ice", None)
    dup_count_ice = _legacy_clones if dup_count_ice is None else dup_count_ice
    dup_count_under = getattr(args, "boss_clones_under", None)
    dup_count_under = _legacy_clones if dup_count_under is None else dup_count_under
    dup_count_castle = getattr(args, "boss_clones_castle", None)
    dup_count_castle = _legacy_clones if dup_count_castle is None else dup_count_castle
    if harder_mode:
        dup_count_grave = 1
        dup_count_swamp = 1
        dup_count_ice = 1
        dup_count_under = 1
        dup_count_castle = 1
    _boss_clone_by_world = {"grave": dup_count_grave, "swamp": dup_count_swamp,
                            "ice": dup_count_ice, "under": dup_count_under,
                            "castle": dup_count_castle}
    # Cross-world mode: use the universal enemy pool for all worlds. Reads
    # the --cross-world CLI flag / GUI checkbox (args.cross_world).
    #
    # This was previously hardcoded to False (ignoring the flag/checkbox
    # entirely) as an emergency kill-switch after a map-load crash. The two
    # root causes of that crash are now fixed:
    #   1. bef.py's _NEVER_EVICT was out of sync with the real placeable
    #      item/enemy pool (items.py) -- fixed, now derived directly from it.
    #   2. This function's own target_enemy_types computation (below) wasn't
    #      filtered by CROSS_WORLD_SAFE_ENEMIES, so world-locked enemies
    #      (meshes only in a world-specific .PRS, e.g. Goat_Devil/
    #      Hammer_Devil/Doomed_Soul in UNDER.PRS) got injected into every
    #      world -- fixed, now intersected with CROSS_WORLD_SAFE_ENEMIES.
    cross_world_mode = bool(getattr(args, "cross_world", False))
    # Optional: drop Dark_Knight (0x53) from every randomization pool. The
    # Castle Dark_Knight has a kill-event quirk that can soft-lock a level
    # when it lands on a progression slot, so the user can opt to keep it
    # vanilla-only (it still appears where the game originally placed it).
    exclude_dark_knight = getattr(args, "exclude_dark_knight", False)
    DARK_KNIGHT_TYPE = 0x53
    excluded_types: set[int] = {DARK_KNIGHT_TYPE} if exclude_dark_knight else set()

    # CHALLENGE MODE: suppress mid-level checkpoints (Holy_Ground, 0x07) while
    # the level timer is armed. Deliberately NOT a separate user option — it is
    # part of Challenge Mode, so it is tied to the level timer's own resolved
    # value and needs no new GUI/CLI/ISO wiring (gui.py, iso_patcher.py and the
    # argparse layers already carry `level_timer`).
    do_no_holy_ground = resolve_level_timer(
        getattr(args, "level_timer", None)) is not None
    if do_no_holy_ground:
        print("Challenge Mode: HOLY GROUND checkpoints suppressed "
              "(level timer is armed — no mid-level respawn anchor)")
    holy_ground_total = 0
    holy_ground_skipped: list[str] = []

    # Granular item-pipeline categories. The old single `--items` flag is
    # split into independent toggles: items (universal type swap of every
    # item/structure entity), chests (contents + gold), skills (ability IDs),
    # and columns (HUB blind level-select + level-title ??? patch).
    #
    # Back-compat: a caller that only sets `args.items` (legacy) gets every
    # category. New callers (GUI / split CLI flags) set each one explicitly;
    # an unset category (None) mirrors the master `items` flag.
    do_items = bool(getattr(args, "items", False))

    def _category(name: str) -> bool:
        v = getattr(args, name, None)
        return do_items if v is None else bool(v)

    do_chests = _category("chests")
    do_skills = _category("skills")
    do_columns = _category("columns")
    if harder_mode:
        # The harder-mode drop/structure/enemy-share changes are applied by the
        # universal item pass + chest pass, so make sure both run.
        do_items = True
        do_chests = True
    if preserve_chests:
        # Chest-content randomization must run for the preserved chests.
        do_chests = True
    any_item_pipeline = do_items or do_chests or do_skills or do_columns

    # Maximo spawn-location randomizer: moves the player's level-start position
    # to a random item/structure/enemy location. Independent of the other
    # categories.
    do_spawn_location = bool(getattr(args, "spawn_location", False))
    if do_spawn_location:
        print("Maximo spawn-location randomization ON (start moved to a random entity position)")
    # Level-exit randomizer: the End_Of_Sublevel_Column trades positions with a
    # random designer-placed structure. The column is never replaced or removed.
    do_exit_location = bool(getattr(args, "exit_location", False))
    if do_exit_location:
        print("Level-exit location randomization ON (exit column swapped with a random structure position)")
    # Spawn<->exit two-way swap: Maximo starts where the exit column was, and the
    # column moves to Maximo's old start. Applied LAST and WINS — it owns both
    # the spawn header and the exit column, so the other two options are skipped
    # when it is on (otherwise all three would fight over the same bytes).
    do_swap_spawn_exit = bool(getattr(args, "swap_spawn_with_exit", False))
    if do_swap_spawn_exit:
        print("Spawn<->exit swap ON (Maximo starts at the level exit, exit moved to the old start)")
        if do_spawn_location or do_exit_location:
            print("  Override: spawn<->exit swap wins — spawn-location and "
                  "exit-location randomization skipped")
        do_spawn_location = False
        do_exit_location = False
    if bool(getattr(args, "gen_tier", True)):
        print("Monster-generator class-level randomization ON (base/upgraded/elite spawns)")
    # Editable spawn-rate config (per-tag / per-world weights + enable flags).
    # Only active when the user supplied a config file; otherwise `spawn` is
    # None and the randomizer uses its stock weight tables unchanged.
    spawn = load_spawn_config(getattr(args, "spawn_config_path", None))
    if spawn is not None:
        print("Custom spawn-rate config loaded (per-tag / per-world weights active)")
        # THE MIGRATION REPORT IS LOGGED HERE. `spawn_config` deliberately does
        # not print -- it produces the report as DATA and hands it over on
        # `SpawnConfig.migration_report` -- so a version <= 3 file that was
        # migrated on load says so exactly once, here, naming every mapped,
        # dropped and defaulted field.
        if getattr(spawn, "migration_report", None) is not None:
            from . import unified_report as _ureport_mig
            for _line in _ureport_mig.migration_lines(spawn.migration_report):
                print(f"  {_line}")

    # Abilities & Skills editor. One weight per ability id (0 = never), used for
    # BOTH layers: the per-level ELF drop tables (below) and the ability ids
    # baked into the pickups placed on the maps (passed to
    # aggressive_item_randomize as `ability_weights`).
    abil_overrides = spawn.ability_drop_overrides() if spawn is not None else {}
    ability_place_weights = {i: w for i, w in abil_overrides.items() if w > 0} or None
    if ability_place_weights:
        print(f"ELF patch: ability pool restricted to ids "
              f"{sorted(ability_place_weights)} (editor selection)")

    def enemy_weight_table(world: str) -> dict[int, float]:
        """Per-enemy relative weights for the even-out balancing bag. Uses the
        spawn config when present, else the stock WALKING_MELEE_WEIGHTS."""
        if spawn is not None:
            return spawn.enemy_weights_for_world(world)
        return WALKING_MELEE_WEIGHTS

    def enemy_tier_table(world: str):
        """Per-enemy class-level (tier) weights, or None to use stock tier
        weighting. Active only when a spawn config is supplied."""
        if spawn is not None:
            return spawn.enemy_tier_weights_for_world(world)
        return None

    def spawn_patch_for(psx_obj, fname: str, file_seed: int):
        """Build the player-spawn header patch for a level, or None. Skipped
        for HUB maps (level-select) so progression stays intact."""
        if not do_spawn_location:
            return None
        if "_HUB" in fname.upper():
            return None
        patch = randomize_player_spawn(psx_obj, random.Random(file_seed ^ 0x5A5A5A5A))
        return patch or None

    def apply_exit_location(psx_obj, fname: str, file_seed: int, repl: dict):
        """Relocate the level-exit column(s) in `fname` by swapping positions
        with a random designer-placed structure. Merges into the existing
        replacement dict `repl` in place. Skipped for HUB maps (level-select)
        so progression stays intact."""
        if not do_exit_location:
            return 0
        if "_HUB" in fname.upper():
            return 0
        moves = randomize_exit_location(psx_obj, random.Random(file_seed ^ 0x3C7B91A5))
        if not moves:
            return 0
        for off, rec in moves.items():
            prior = repl.get(off)
            if prior is not None:
                # Something else already rolled this slot — keep that roll and
                # just move it to the traded position.
                repl[off] = move_record_to(prior, rec.pos_x, rec.pos_y, rec.pos_z)
            else:
                repl[off] = rec
        return len(moves) // 2

    def apply_swap_spawn_exit(psx_obj, fname: str, repl: dict):
        """Two-way swap of Maximo's start with the level-exit column. Merges the
        column move into `repl` in place and returns the spawn header patch (or
        None). Skipped for HUB maps (level-select) so progression stays intact."""
        if not do_swap_spawn_exit:
            return None
        if "_HUB" in fname.upper():
            return None
        header_patch, moves = swap_spawn_with_exit(psx_obj)
        if not header_patch:
            return None
        for off, rec in moves.items():
            prior = repl.get(off)
            if prior is not None:
                repl[off] = move_record_to(prior, rec.pos_x, rec.pos_y, rec.pos_z)
            else:
                repl[off] = rec
        return header_patch

    def apply_remove_all_gates(psx_obj, world: str, repl: dict):
        """Retype every gate in the file into a harmless same-size prop, so a
        level started at its exit stays completable. Merges into `repl` in
        place and returns (removed, skipped). Runs on HUB maps too — hub gates
        block hub progression the same way. Cross-file templates come from the
        world's item-template registry, so a gate in a file that has no local
        template for the destination still converts."""
        if not do_swap_spawn_exit:
            return 0, 0
        present = sum(1 for r in psx_obj.records if r.type_id in GATE_TYPES)
        if not present:
            return 0, 0
        gate_repl = remove_all_gates(
            psx_obj, templates=world_item_templates.get(world, {}))
        for off, rec in gate_repl.items():
            repl[off] = rec
        return len(gate_repl), present - len(gate_repl)

    if all_enemies_mode:
        print("Mode: ALL-ENEMIES (chaos) — every item/structure becomes an enemy")
    else:
        print(f"Modes: enemies={not no_enemies}, items={do_items}, "
              f"chests={do_chests}, skills={do_skills}, columns={do_columns}")
    if cross_world_mode:
        print("Cross-world mode: ALL enemy types available in ALL worlds")
    else:
        print("Cross-world mode: OFF (world-locked enemies stay in their home worlds)")
    if dup_bosses:
        print(f"Experimental: boss duplication ON (Grave={dup_count_grave}, "
              f"Swamp={dup_count_swamp} clone(s))")
    if exclude_dark_knight:
        print("Dark_Knight EXCLUDED from randomizer pools (kept vanilla-only)")
    print()

    def pool_for_world(world: str) -> set[int]:
        """Return the walking-melee enemy type pool for a world, minus any
        types the user opted to exclude (e.g. Dark_Knight) or disabled in the
        spawn-rate config.

        NOTE: this initial definition is ONLY used for the pre-pass template
        harvest below (world_enemy_candidates / world_templates) -- it is
        replaced by a second, more accurate definition further down once
        bef_native_types is known (see the `cross_world_mode` block).

        This must NOT be narrowed to CROSS_WORLD_SAFE_ENEMIES even when
        cross_world_mode is on: CROSS_WORLD_SAFE_ENEMIES only says which
        enemies are safe to INJECT into a FOREIGN world -- it says nothing
        about a world's own native roster. Using it here starved the
        pre-pass of a Dark_Knight template even in CASTLE (his own home
        world), because 0x53 is PRS-locked (excluded from
        CROSS_WORLD_SAFE_ENEMIES) despite being perfectly native to Castle.
        With no template harvested anywhere, global_enemy_templates never
        got a 0x53 entry either, so Dark_Knight could never be chosen as a
        swap destination in ANY world (including Castle) once cross-world
        mode was on -- silently, with no warning, regardless of whether his
        mesh was later ported to other worlds via inject_locked_enemy. Using
        get_walking_melee_for_world(world, cross_world=False) here always
        harvests each world's FULL native roster (locked enemies included),
        which is what the pre-pass template harvest actually needs.
        """
        pool = get_walking_melee_for_world(world, cross_world=False) - excluded_types
        if spawn is not None:
            pool = pool - spawn.disabled_enemies(world)
        return pool

    # ------------------------------------------------------------------
    # Pre-pass: build a GLOBAL per-world enemy template registry.
    #
    # Why: a single map (e.g. C_HUB.PSX) may only contain Ghost/Raven/
    # Torso_Zombie in vanilla. If we only build templates from records in
    # that file, the "available swap targets" set is just those 3 types,
    # so every enemy ends up rolling Ghost/Raven/Torso again. By harvesting
    # templates across ALL maps in a world we can swap an enemy in C_HUB
    # into a Dark_Knight pulled from C_SUB1, etc.
    # ------------------------------------------------------------------
    parsed_psx: dict[Path, PsxFile] = {}
    world_templates: dict[str, dict[int, "PsxRecord"]] = {}
    world_item_templates: dict[str, dict[int, "PsxRecord"]] = {}
    # Per-world per-type CANDIDATE list: collect every record across every
    # file in the world, then pick the best template afterwards.
    world_enemy_candidates: dict[str, dict[int, list]] = {}
    for path in psx_files:
        if path.name.upper() in BOSS_FILES:
            continue
        try:
            psx = PsxFile.parse(path)
        except Exception:
            continue
        parsed_psx[path] = psx
        world = get_world_from_bef_path(psx.bef_path)
        if world is None:
            continue
        cand_slot = world_enemy_candidates.setdefault(world, {})
        pool_ids = pool_for_world(world)
        for tid in pool_ids:
            recs = psx.find_records_by_type(tid)
            if recs:
                cand_slot.setdefault(tid, []).extend(recs)
        # Cross-file ITEM template registry — needed so maps without a native
        # Ability/etc. record can still roll INTO that type from the universal
        # pool (HUB files in particular don't contain abilities natively).
        item_slot = world_item_templates.setdefault(world, {})
        for tid in UNIVERSAL_ITEM_POOL:
            if tid in item_slot:
                continue
            recs = psx.find_records_by_type(tid)
            if recs:
                item_slot[tid] = recs[0]

    # Cross-WORLD fallback for types that vanilla only places in some worlds.
    # Gold_Key (0x2B) only exists in CASTLE, GRAVE, ICE — but the user wants
    # it to spawn in every world via the 0.01% lottery, so we let SWAMP and
    # UNDER borrow a Gold_Key template from any world that has one. Same
    # principle applies if any other UNIVERSAL_ITEM_POOL type ever gets
    # added with partial-world coverage.
    GLOBAL_TEMPLATE_TYPES = {0x2B}  # Gold_Key
    cross_world_templates: dict[int, "PsxRecord"] = {}
    for tid in GLOBAL_TEMPLATE_TYPES:
        for slot in world_item_templates.values():
            if tid in slot:
                cross_world_templates[tid] = slot[tid]
                break
    for world_slot in world_item_templates.values():
        for tid, tpl in cross_world_templates.items():
            world_slot.setdefault(tid, tpl)

    # Monster_Generator (0x5F) DESTINATION template fix. Items can roll INTO a
    # generator, but the template must be a WORKING (non-HUB) generator — a
    # HUB phase generator cloned into a normal item slot becomes a silent,
    # never-triggering spawner. Castle's only native generators are HUB phase
    # markers (in C_HUB), so Castle LEVELS would otherwise only ever produce
    # dead generators. Harvest a non-HUB generator from any world and use it
    # as the generator template for every world whose own template is HUB-only
    # or missing — so Castle levels (and any other generator-less map) can
    # spawn functioning generators.
    global_nonhub_gen = None
    for slot in world_item_templates.values():
        tpl = slot.get(MONSTER_GENERATOR_TYPE)
        if tpl is not None and not _is_hub_generator(tpl):
            global_nonhub_gen = tpl
            break
    if global_nonhub_gen is not None:
        for world_slot in world_item_templates.values():
            cur = world_slot.get(MONSTER_GENERATOR_TYPE)
            if cur is None or _is_hub_generator(cur):
                world_slot[MONSTER_GENERATOR_TYPE] = global_nonhub_gen

    # Global Gold_Key template — used to guarantee a start-of-level Gold_Key
    # in the four named levels (START_GOLD_KEY_FILES). Swamp files have no
    # native Gold_Key, so we reuse one harvested from Castle/Grave/Ice.
    global_gold_key_template = cross_world_templates.get(GOLD_KEY_TYPE)

    # Global Skeleton_Key (0x65) template for the extra-key injections.
    global_skeleton_key_template = None
    for slot in world_item_templates.values():
        if 0x65 in slot:
            global_skeleton_key_template = slot[0x65]
            break

    # Global weather emitter templates — harvested from any level that has
    # one. Rain (0xD0) lives in Grave/Swamp intros, Snow (0xCF) in Ice. Used
    # to INJECT weather into weather-world levels that lack an emitter so e.g.
    # rain appears across all of Grave, not just the intro.
    global_rain_template = None
    global_snow_template = None
    for psx_obj in parsed_psx.values():
        if global_rain_template is None:
            recs = psx_obj.find_records_by_type(0xD0)
            if recs:
                global_rain_template = recs[0]
        if global_snow_template is None:
            recs = psx_obj.find_records_by_type(0xCF)
            if recs:
                global_snow_template = recs[0]
        if global_rain_template and global_snow_template:
            break

    def weather_template_for(world: str):
        """Weather emitter template appropriate for a world (rain for
        grave/swamp, snow for ice), or None."""
        if world in ("grave", "swamp"):
            return global_rain_template
        if world == "ice":
            return global_snow_template
        return None

    # Per-world Monster_Generator template pool. Gathering one generator per
    # distinct enemy signature across a world lets the randomizer re-spawn
    # item-created generators with the world's VARIED enemy roster (e.g. Grave
    # = basic + sword skeleton) instead of every created portal cloning a
    # single template and spawning the same enemy.
    world_gen_templates: dict[str, list] = {}
    _world_psx: dict[str, list] = {}
    for p, psx_obj in parsed_psx.items():
        wd = get_world_from_bef_path(psx_obj.bef_path)
        if wd:
            _world_psx.setdefault(wd, []).append(psx_obj)
    for wd, lst in _world_psx.items():
        world_gen_templates[wd] = collect_world_gen_templates(lst)

    # Per-world GATE template pool — one template per gate type a world loads.
    # Used by the gate randomizer so gates only swap into / appear as gate
    # types that world's BEF actually renders.
    world_gate_templates: dict[str, dict[int, "PsxRecord"]] = {}
    for wd, lst in _world_psx.items():
        slot = world_gate_templates.setdefault(wd, {})
        for psx_obj in lst:
            for tid in GATE_TYPES:
                if tid in slot:
                    continue
                recs = psx_obj.find_records_by_type(tid)
                if recs:
                    slot[tid] = recs[0]
    gate_mode = getattr(args, "gate_mode", None)
    if do_swap_spawn_exit:
        # Gate REMOVAL wins over gate randomization: the swap needs every gate
        # gone, and rolling gates into other gates would put barriers back.
        if gate_mode in ("isolated", "pool"):
            print("  Override: spawn<->exit swap removes every gate — "
                  "gate randomizer skipped")
        gate_mode = None
    if gate_mode in ("isolated", "pool"):
        print(f"Gate randomizer ON (mode={gate_mode})")

    # Pick the best enemy template per (world, type). Prefer in this order:
    #   1. A "neutral" record: not a HUB phase marker (instance_name doesn't
    #      start with P1_/P2_/P3_/P4_/FP1_..FP4_). HUB phase markers carry
    #      wave-index values in their props that the engine reads to bind
    #      the spawn to a specific HUB phase — cloning that template into
    #      regular in-level slots makes the cloned enemy never spawn during
    #      normal gameplay (it waits for a HUB phase trigger that never
    #      fires outside the HUB).
    #   2. Within the non-HUB pool, prefer records with prop[5] == 0
    #      (avoids the historic "every Axe_Skeleton drops Iron Key" bug
    #      from cloning a record whose prop[5] reads as a drop indicator).
    #   3. Otherwise the first non-HUB record found.
    #   4. As a last resort, any record (including HUB).
    def _is_hub_phase_record(r):
        n = r.instance_name.lower() if r.instance_name else ""
        return n.startswith(("p1_", "p2_", "p3_", "p4_",
                             "fp1_", "fp2_", "fp3_", "fp4_"))

    for world, cand_map in world_enemy_candidates.items():
        slot = world_templates.setdefault(world, {})
        for tid, recs in cand_map.items():
            non_hub = [r for r in recs if not _is_hub_phase_record(r)]
            preferred_pool = non_hub if non_hub else recs
            neutral = [
                r for r in preferred_pool
                if r.prop_count >= 6
                and r.raw[0x228 + 5*0x20] == 0x07
                and struct.unpack_from('<I', r.raw, 0x228 + 5*0x20 + 8)[0] == 0
            ]
            slot[tid] = (
                neutral[0] if neutral
                else preferred_pool[0] if preferred_pool
                else recs[0]
            )

    # Same preference for the universal pool's per-world item template
    # registry. Without this, world_item_templates picked S_HUB's first
    # Zombie_Crocodile record (prop[4]=3, a phase marker) so every coin
    # rolling INTO a Crocodile got an invisible HUB-bound enemy.
    for world, slot in world_item_templates.items():
        for tid in list(slot.keys()):
            tpl = slot[tid]
            if not _is_hub_phase_record(tpl):
                continue
            # Try to find a non-HUB candidate of this type in the world's files.
            replacement = None
            for path, psx in parsed_psx.items():
                pworld = get_world_from_bef_path(psx.bef_path)
                if pworld != world:
                    continue
                for r in psx.find_records_by_type(tid):
                    if not _is_hub_phase_record(r):
                        replacement = r
                        break
                if replacement:
                    break
            if replacement:
                slot[tid] = replacement

    if not no_enemies:
        for world in sorted(selected_worlds):
            avail = world_templates.get(world, {})
            names = [name_for_type(t) for t in sorted(avail.keys())]
            print(f"  enemy templates [{world}]: {len(avail)} types -> "
                  f"{', '.join(names) if names else '(none found)'}")
        print()

    # ------------------------------------------------------------------
    # CROSS-WORLD template sharing: borrow enemy record templates from
    # OTHER worlds' PSX files for types that already exist in the target
    # world's BEF but don't have a native PSX template. This means a
    # world that has the type in its BEF but never uses it in a vanilla
    # level can still spawn it using a template from a world that does.
    #
    # NOTE: We do NOT add types that aren't in the world's BEF — those
    # would crash because the engine can't load assets it doesn't have.
    # We only borrow TEMPLATES (PSX record data) for BEF-native types.
    # ------------------------------------------------------------------
    # Load each world's BEF to know its native enemy-type set.
    # Used in TWO places:
    #   1. cross_world_mode (below): borrow templates for BEF-native types.
    #   2. Item randomizer (always): exclude enemy types NOT in a world's BEF
    #      from the universal-pool destination set, so a GoldCoin in Grave
    #      can never roll into Goat_Devil (UNDER) or Dark_Knight (CASTLE).
    #      Without this, seeds that land on a world-specific enemy type cause
    #      the engine to look up a null mesh pointer in that world's BEF and
    #      crash — the intermittent "crash when loading a level" bug.
    from .bef import BefFile, BefEntry
    bef_folder = Path(args.psx_folder)
    bef_name_map = {
        "grave": "GRAVE.BEF", "under": "UNDER.BEF",
        "swamp": "SWAMP.BEF", "ice": "ICE.BEF", "castle": "CASTLE.BEF",
    }
    bef_native_types: dict[str, set[int]] = {}
    for world, bef_name in bef_name_map.items():
        bef_path = bef_folder / bef_name
        if bef_path.exists():
            try:
                bef = BefFile.parse(bef_path)
                bef_native_types[world] = bef.type_ids()
            except Exception:
                bef_native_types[world] = set()
        else:
            bef_native_types[world] = set()

    # All BEF-managed types in the universal pool — both enemies and world-
    # specific props (smashables, lanterns, statues, etc.). Any of these that
    # aren't defined in a world's BEF will crash the engine on mesh lookup if
    # placed in that world's file.
    ALL_POOL_BEF_TYPES: set[int] = set(ENEMY_DESTINATIONS) | set(UNIVERSAL_ITEM_POOL)

    # ------------------------------------------------------------------
    # Permission source of truth: the CATALOG, not the asset file.
    #
    # A world's BEF TOC is evidence that the engine can *look up* a type in
    # that world, but NOT that the type's mesh/textures are actually usable
    # there. GRAVE.BEF, for example, still declares 0x0D (Zombie_Crocodile)
    # and 0x16 (Axe_Guard) left over from earlier cross-world experiments;
    # their mesh indices are positional into SWAMP.PRS / CASTLE.PRS, so in
    # Grave they resolve to unrelated geometry or out of range -> invisible
    # or wrong-looking enemies. Trusting the BEF alone let the ordinary item
    # pass retype a Grave item into them with no mesh/texture port at all.
    #
    # So a type is permitted in a world only when it is BOTH declared by that
    # world's BEF AND known-safe for that world per the catalog.
    # ------------------------------------------------------------------
    from .catalog import is_native as _catalog_is_native

    # Types this run actually ported into a world (cross-world mode fills
    # this in after a successful mesh+texture port). A ported type is safe
    # even though the catalog doesn't list it as native.
    ported_types_by_world: dict[str, set[int]] = {}
    # STRUCTURE types this run actually ported into a world, kept SEPARATE from
    # `ported_types_by_world` (which mixes enemies in) because it feeds the item
    # pool's WEIGHTS, not permission. Declared here, at the same scope as the
    # permission dict, so the item pass can read it whether or not the
    # cross-world block below ever ran: empty means "no arrivals", which is the
    # untouched-weights path.
    structure_arrival_types_by_world: dict[str, set[int]] = {}
    _contamination_logged: set[tuple[str, int]] = set()

    def catalog_permitted_types(world: str) -> set[int]:
        """Pool types the catalog considers safe to place in `world`.

        Enemy types must be native to the world (or ported this run).
        Non-enemy pool types (props/smashables/pickups) have no catalog
        native table; they stay permitted here and are still filtered by the
        BEF intersection in `permitted_types_for_world`.
        """
        return catalog_permitted_pool_types(
            world, ALL_POOL_BEF_TYPES,
            ported_types_by_world.get(world, set()))

    def permitted_types_for_world(world: str) -> set[int] | None:
        """BEF-declared AND catalog-safe types for `world`.

        Returns None when the world's BEF is unavailable/unparseable, so
        callers can skip the filter instead of excluding everything.
        Logs once per (world, type) for anything the BEF declares that the
        catalog says was never ported into that world — that line means the
        asset file on disc is contaminated.
        """
        native = bef_native_types.get(world)
        if not native:
            return None
        allowed = catalog_permitted_types(world)
        contaminated = (native & ALL_POOL_BEF_TYPES) - allowed
        for tid in sorted(contaminated):
            key = (world, tid)
            if key in _contamination_logged:
                continue
            _contamination_logged.add(key)
            print(f"  asset guard [{world}]: 0x{tid:02X} {name_for_type(tid)} "
                  f"declared by {world.upper()}.BEF but not native/ported to "
                  f"{world} — excluded as declared-but-not-ported "
                  f"(contaminated asset file)")
        return native & allowed

    # ------------------------------------------------------------------------
    # THE STRUCTURE SWITCH, read in EXACTLY ONE PLACE and hoisted to HERE.
    #
    # `--cross-world-structures` is INDEPENDENT of `--cross-world`: either switch
    # works alone. That is why the read has to happen before both the RESOURCE
    # pool decision (a structures-only run grows the same `.PRS`/`.PRT`, so it
    # opts into the same raised pool) and the cross-world gate itself (the gate
    # now admits the block when EITHER feature is on).
    #
    # `STRUCTURE_PORT_ENABLED` stays a maintainer KILL SWITCH that must agree:
    # `_structures_on` requires both it and the user's flag, so flipping it False
    # kills the feature no matter what was asked for.
    # ------------------------------------------------------------------------
    from . import structure_draw as _sd

    # ------------------------------------------------------------------------
    # THE SPAWN-CONFIG FEATURE SWITCHES, the SECOND (narrowing) gate.
    #
    # The spawn-rate editor carries one checkbox per feature -- one in its
    # Structures section, one in its Enemies section -- persisted as
    # `SpawnConfig.cross_world_enemies_enabled` /
    # `.cross_world_structures_enabled`, both DEFAULT TRUE.
    #
    # THE COMBINATION IS AND, DELIBERATELY. Either source can turn a feature
    # OFF; neither can turn ON what the other refused. That keeps
    # `--cross-world` / `--cross-world-structures` meaning exactly what they say
    # while still letting the editor disable a feature, and it keeps this a
    # NARROWING gate: it never widens the permission oracle and it never touches
    # a per-route `enabled` flag, so a feature turned off and on again restores
    # the user's route selection exactly.
    #
    # With NO config loaded (`spawn is None`) both are True, i.e. the gate is a
    # no-op and the CLI switch alone decides.
    _cfg_enemies_allowed = (True if spawn is None else
                            bool(getattr(spawn, "cross_world_enemies_enabled",
                                         True)))
    _cfg_structures_allowed = (
        True if spawn is None else
        bool(getattr(spawn, "cross_world_structures_enabled", True)))
    # ------------------------------------------------------------------------

    _want_structures = (bool(getattr(args, "cross_world_structures", False))
                        and _cfg_structures_allowed)
    _structures_on = _want_structures and _sd.STRUCTURE_PORT_ENABLED
    # SAY WHICH SOURCE TURNED IT OFF when the two disagree, because otherwise a
    # user who unticked the editor checkbox reads a silently inert
    # `--cross-world-structures` as a broken flag.
    if (bool(getattr(args, "cross_world_structures", False))
            and not _cfg_structures_allowed):
        print("  [cross-world] structures: EFFECTIVE OFF. The command-line / "
              "GUI switch asked for them ON, but the spawn config's Structures "
              "section has 'Cross-world structures' UNCHECKED, and that is "
              "what turned them off. Re-check it in the spawn-rate editor to "
              "let the flag take effect.")
    elif not bool(getattr(args, "cross_world_structures", False)):
        print("  [cross-world] structures: EFFECTIVE OFF (the command-line / "
              "GUI switch did not ask for them).")
    else:
        print("  [cross-world] structures: EFFECTIVE ON (both the "
              "command-line / GUI switch and the spawn config agree).")
    if _want_structures and not _sd.STRUCTURE_PORT_ENABLED:
        print("  [WARN] cross-world structures were requested but the "
              "maintainer kill switch structure_draw.STRUCTURE_PORT_ENABLED is "
              "False, so no structure is ported this run")

    # RESOURCE pool decision, hoisted (it used to live next to the ELF patches
    # further down, which is still where it is APPLIED). The seeded cross-world
    # draw below needs it: its byte budget is an OCCUPANCY of the pool that will
    # actually ship, so it must never assume a patch this build did not make
    # (Requirement 6.5). Resolving it once here and reading it in both places
    # keeps that honest. `--cross-world` opts IN to the raised preset by
    # default; an explicit --resource-pool (including 'vanilla') always wins.
    #
    # ONE cross-world default, `raised`, WITH OR WITHOUT STRUCTURES.
    #
    # REVERTED ON AN OBSERVED IN-GAME FAILURE. This used to pick the `high`
    # preset (0x900000) when `--cross-world-structures` was on, to give ported
    # structures room. That build HUNG ON MAP LOAD -- INFINITE LOADING, no crash
    # -- while the immediately preceding `raised` (0x700000) build loaded and
    # played fine. The three pools tile a FIXED arena with no slack and the
    # engine has no bounds check, so starving MAIN by 0x2C0000 instead of
    # 0xC0000 is the credible cause. `high` remains SELECTABLE by an explicit
    # `--resource-pool high` with that observation recorded at
    # `elf_patch.RESOURCE_POOL_HIGH`, but it is never a default again.
    #
    # Structures get their room from `structure_draw.STRUCTURE_RESERVE_BYTES`
    # instead: the enemy gate's ceiling is lowered by that reserve so the
    # structure pass can spend it out of the SAME one budget. See below.
    resource_pool = resolve_resource_pool(getattr(args, "resource_pool", None))
    resource_pool_from_cross_world = False
    resource_pool_preset_label = None
    # EITHER cross-world feature opts in, because both grow the same per-world
    # `.PRS`/`.PRT`: a structures-only run needs the room just as much as an
    # enemies-only one. The default is `raised` (0x700000), the only raised size
    # that has loaded and played CONSISTENTLY; `high` is not a preset any more
    # and 0x880000 and above is refused outright.
    if resource_pool is None and getattr(args, "resource_pool", None) is None \
            and (cross_world_mode or _structures_on):
        # 0x700000. Walked on console: 0x900000 hung, 0x880000 hung, 0x800000
        # LOADED and played once and later HUNG with infinite loading on another
        # build, 0x700000 has loaded and played across many builds. Both
        # 0x800000 results are real, so that size is UNRELIABLE -- still
        # SELECTABLE via `--resource-pool unreliable` for a retry, never the
        # default. 0x880000 and above is REFUSED by `resolve_resource_pool`.
        resource_pool = RESOURCE_POOL_RAISED
        resource_pool_preset_label = "raised"
        resource_pool_from_cross_world = True
    do_resource_pool = (resource_pool is not None
                        and resource_pool != RESOURCE_POOL_VANILLA)
    # The budget comes from THAT decision, never from a literal: a build that did
    # not patch the pool draws against the vanilla 6,553,600 B and ships a
    # smaller lineup. `resource_pool_in_effect` is the EXACT size this build
    # patched, and it is what the budget is an occupancy of, so `high` (and any
    # custom size, above or below the raised preset) moves the budget instead of
    # being flattened into a boolean. `raised_pool_in_effect` remains only as the
    # plan's coarse "was the pool raised at all" flag.
    raised_pool_in_effect = bool(do_resource_pool
                                 and resource_pool >= RESOURCE_POOL_RAISED)
    resource_pool_in_effect = (resource_pool if do_resource_pool
                               else RESOURCE_POOL_VANILLA)

    # THE OTHER HALF OF THE BUDGET: what SHARE of that pool the draw may occupy.
    # `pool_size` decides what the budget is a fraction OF; this decides the
    # fraction. Defaults to the OBSERVED-LOADING 93.0 so a build that does not
    # ask is the build that loaded; `resolve_max_pct` refuses 93.5 and above (the
    # observed crash floor) rather than clamping. Resolved ONCE here and handed to every
    # `pool_budget` / `plan_draw` call below -- including the STRUCTURE pass,
    # which reads the same budget and must not silently keep the default.
    from .roster_selection import (resolve_max_pct as _resolve_max_pct,
                                   pool_budget as _sel_pb,
                                   LOADED_MAX_PCT as _LOADED_MAX_PCT,
                                   DEFAULT_MAX_PCT as _DEFAULT_MAX_PCT,
                                   CRASHED_MIN_PCT as _CRASHED_MIN_PCT)
    occupancy_in_effect = _resolve_max_pct(getattr(args, "pool_occupancy", None))

    # The seed-drawn lineup per target world: committed foreign arrivals plus
    # drawn natives. Filled in by the cross-world block below and read by
    # `pool_for_world`, which is where the drawn natives actually take effect.
    lineup_by_world: dict[str, tuple[int, ...]] = {}

    # THE UNIFIED TABLE AND WHAT THE PASS COMMITTED, declared HERE because both
    # the cross-world block and the retyping pass much further down need them.
    #
    # `committed_routes_by_world` is `(type_id, source_world, target_world)` per
    # route the unified pass ACTUALLY committed into a world. It is the whole of
    # Property 5: the retyping draw is fed
    # `native_weights(world, committed_routes=...)` AFTER admission for that world
    # has completed, so it observes admission's RESULT and never its RNG state.
    # Empty on a build with neither cross-world feature on, in which case
    # `native_weights` returns the pool with no arrivals in it.
    from . import spawn_config as _sc_mod

    unified_cfg = (spawn if spawn is not None
                   else _sc_mod.SpawnConfig.default())
    committed_routes_by_world: dict[str, tuple] = {}
    # The STRUCTURE half of the same committed set, kept separately because the
    # template injection below must know the SOURCE world of each route (and
    # must never see an enemy route -- enemies are placed through the enemy
    # path, which extends the roster directly and needs no item template).
    # Same shape: `(type_id, source_world, target_world)` per COMMITTED route.
    committed_structure_routes_by_world: dict[str, tuple] = {}

    # TWO INDEPENDENT SWITCHES, ONE BLOCK. Cross-world ENEMIES keeps its exact
    # old condition; cross-world STRUCTURES is NOT gated on `no_enemies` at all,
    # because a structures-only run with `--no-enemies` is legitimate -- it ports
    # no enemy and randomizes no enemy, it only widens the ITEM pool's type set.
    #
    # AND the spawn config's own switch in (resolved above, next to the
    # structures one): the editor's Enemies-section checkbox can turn the
    # feature OFF, and nothing can turn it ON that the CLI switch refused.
    _cw_enemies_on = (cross_world_mode and not no_enemies
                      and _cfg_enemies_allowed)
    if cross_world_mode and not no_enemies and not _cfg_enemies_allowed:
        print("  [cross-world] enemies: EFFECTIVE OFF. The command-line / GUI "
              "switch asked for them ON, but the spawn config's Enemies "
              "section has 'Cross-world enemies' UNCHECKED, and that is what "
              "turned them off. Re-check it in the spawn-rate editor to let "
              "the flag take effect.")
    else:
        print(f"  [cross-world] enemies: EFFECTIVE "
              f"{'ON' if _cw_enemies_on else 'OFF'}")
    if _cw_enemies_on or _structures_on:
        if _cw_enemies_on:
            print("Cross-world mode ENABLED: porting enemy meshes/textures/BEF blobs across worlds...")
            if _structures_on:
                print("Cross-world STRUCTURES also ENABLED: both features are on.")
        else:
            print("Cross-world STRUCTURES ENABLED: porting structure "
                  "meshes/textures/BEF blobs across worlds. Cross-world ENEMY "
                  "porting is OFF, so no enemy roster is built, no enemy is "
                  "drawn and no enemy admission gate runs this build.")

        # THE RAISED OCCUPANCY CEILING, said out loud. `[cross-world]` and not
        # `ELF patch:` -- `ELF patch:` is reserved for edits made to the
        # executable, and this changes no instruction at all, only how much of the
        # pool this build's DRAW is willing to fill.
        # The comparison is against `DEFAULT_MAX_PCT` -- what this build draws to
        # when the user chooses nothing -- because that is what "RAISED" is
        # raised FROM. The separate UNEXPLORED warning below is keyed off
        # `LOADED_MAX_PCT`, the MEASUREMENT, because that is the figure past
        # which no console build has ever loaded. The two are different numbers
        # now (90.0 default, 93.0 measured) and are reported as such.
        if occupancy_in_effect > _DEFAULT_MAX_PCT:
            _occ_budget = _sel_pb(pool_size=resource_pool_in_effect,
                                  max_pct=occupancy_in_effect)
            _occ_default = _sel_pb(pool_size=resource_pool_in_effect,
                                   max_pct=_DEFAULT_MAX_PCT)
            print(f"  [cross-world] draw occupancy ceiling RAISED to "
                  f"{occupancy_in_effect}% of the "
                  f"{resource_pool_in_effect:,} B RESOURCE pool = "
                  f"{_occ_budget:,} B budget, which is "
                  f"{_occ_budget - _occ_default:,} B MORE than the "
                  f"{_DEFAULT_MAX_PCT}% default ({_occ_default:,} B). The "
                  f"highest occupancy MEASURED TO LOAD is a separate figure, "
                  f"{_LOADED_MAX_PCT}%. Enemy routes cost 225 KB to 840 KB, so "
                  f"this is worth roughly one extra arrival per world.")
        if occupancy_in_effect > _LOADED_MAX_PCT:
            print(f"  [cross-world] {_LOADED_MAX_PCT}%..{_CRASHED_MIN_PCT}% is "
                  f"UNEXPLORED: the highest occupancy OBSERVED TO LOAD is "
                  f"{_LOADED_MAX_PCT}% (93 loaded and played), the lowest "
                  f"OBSERVED TO CRASH is {_CRASHED_MIN_PCT}% (94 and 95 both "
                  f"died at level load), and NO build has run in between. "
                  f"Exhaustion is "
                  f"SILENT -- there will be no log line, no build failure and "
                  f"no test failure. If this build LOADS, record the figure: "
                  f"that is evidence. If it HANGS on the loading screen or dies "
                  f"at level load, that is the expected failure of this band "
                  f"and it means DROPPING BACK to the last figure that loaded.")
        print()

        # ------------------------------------------------------------------
        # CROSS-WORLD ENEMY PORTING
        #
        # Mesh indices are WORLD-RELATIVE and render-pass texture ids are
        # absolute shared-cache indices, so a foreign enemy needs its mesh
        # appended to the target .PRS, its world-local textures ported into the
        # target .PRT and its BEF blob repointed. Every one of those decisions
        # -- classification, per-world session batching, texture arithmetic,
        # fallback and logging -- lives in cross_world.run_cross_world_placements.
        #
        # This block only (a) builds the placement list from the ALLOWLIST and
        # (b) routes the returned buffers into out_folder, which is the same
        # patched-output folder iso_patcher.write_patched_assets reads.
        #
        # NOTHING is written outside out_folder. The prototype this replaced
        # wrote temp_{world}_{id}.BEF/.PRS/.PRT into the CWD purely to satisfy
        # bef.inject_locked_enemy's path-only signature, and called it once per
        # enemy -- each call re-read the target files from disk, so porting a
        # second enemy into a world silently discarded the first.
        # inject_locked_enemy still exists in bef.py; it is simply not called
        # from here any more.
        # ------------------------------------------------------------------
        from . import cross_world as _cw
        from . import roster as _roster
        from . import roster_costs as _roster_costs
        from . import roster_selection as _sel
        from .catalog import load_allowlist as _load_allowlist
        from .catalog import drawn_allowlist as _drawn_allowlist

        # THE ENEMY ALLOWLIST, and it is EMPTY when cross-world enemies are off:
        # a structures-only run has no enemy route to permit, so there is nothing
        # to load, nothing to narrow and nothing to draw from. The structure pass
        # has its OWN permission oracle (`spawn_config.structure_routes_for_world`
        # over the structure allowlist) and never consults this one.
        _allowlist = _load_allowlist() if _cw_enemies_on else {}
        # THE ORACLE ITSELF, kept UN-NARROWED. `classify_placement` is a
        # PERMISSION test and must be asked against the permission oracle; the
        # SELECTION is the unified table's job, applied by
        # `enemy_route_weights`. Handing the editor-narrowed copy to
        # `classify_placement` would filter the same run twice, once through the
        # legacy `arrivals` table, and a legacy config could then veto a route the
        # unified table enables. Narrowing still happens -- just in one place.
        _oracle_allowlist = dict(_allowlist)

        # CROSS-WORLD ARRIVALS, as narrowed by the spawn-rate editor. This is a
        # FILTER over the ACTIVE allowlist and nothing else: it can only remove
        # keys, so a route the catalog holds back or never measured cannot be
        # switched on from the editor. `allowlist_lookup` remains the single
        # permission oracle. Enabling more routes buys VARIETY, not size — the
        # AdmissionGate still bounds the live resident total per draw.
        if spawn is not None and _cw_enemies_on:
            _before = len(_allowlist)
            _allowlist = spawn.narrow_allowlist(_allowlist)
            _dropped_n = _before - len(_allowlist)
            if _dropped_n:
                print(f"  cross-world: spawn config narrowed arrivals — "
                      f"{_dropped_n} of {_before} active route(s) disabled by "
                      f"the editor, {len(_allowlist)} remain drawable")
            for _w in sorted(selected_worlds):
                if (spawn.arrivals.get(_w)
                        and not any(k[2] == _w for k in _allowlist)):
                    print(f"  cross-world: [{_w}] no cross-world arrivals here "
                          f"— every arrival route is disabled in the spawn "
                          f"config (not an error)")

        # GLOBAL.PRT is NOT a dependency of cross-world porting. A mesh's
        # render-pass texture ids are direct indices into its own world's
        # .PRT (CASTLE.PRT entries 9-12 are byte-identical to ICE.PRT 21-24,
        # bound by different ids from the same shared asset), so the old
        # "GLOBAL.PRT missing -> every port aborted" gate measured a number
        # nothing uses. It is gone; ports depend only on the SOURCE and TARGET
        # world files.

        def _cw_log(msg: str) -> None:
            print(f"  {_cw.LOG_PREFIX} {msg}")

        # ------------------------------------------------------------------
        # THE SEEDED DRAW (randomized-enemy-roster, Tasks 5-9)
        #
        # The allowlist is no longer the shipped set. It is the set of routes a
        # seed MAY draw from; `roster_selection` fixes an ORDER from the seed and
        # admission decides how far down that order the budget reaches. The
        # selector PRODUCES an allowlist subset which is fed into the existing
        # `allowlist` parameter, so `allowlist_lookup` stays the single
        # permission oracle and no second permission path exists.
        #
        # Roster construction reads all five worlds' .BEF type tables, in TWO
        # BATCHES of at most four files, because `read_world_snapshots` enforces
        # the four-binaries-per-call limit rather than merely documenting it. The
        # snapshots are small (a type set plus derived companion closures) and
        # each .BEF is dropped as soon as it has been reduced.
        # ------------------------------------------------------------------
        def _draw_log(line: str) -> None:
            print(f"  {line}")

        # THE ENEMY ROSTER IS BUILT ONLY FOR THE ENEMY FEATURE. A structures-only
        # run has no enemy draw to order and no enemy gate to bound, so the five
        # `.BEF` type-table reads are skipped outright rather than producing an
        # empty plan that pretends to be one.
        # ------------------------------------------------------------------
        # THE LEGACY THREE-DRAW MACHINERY IS NO LONGER WHAT DECIDES A BUILD.
        #
        # `unified-randomization` task 4 replaced it with ONE seeded pass:
        # `cross_world.run_unified_cross_world` -> `unified_draw.unified_admit`,
        # reported by `unified_report`. So `roster.read_world_snapshots` /
        # `build_roster`, `roster_selection.plan_draw` / `AdmissionGate` /
        # `admit` and `structure_draw.run_structure_pass` are UNREACHED from a
        # build now.
        #
        # THEY ARE LEFT IN PLACE, GATED OFF BY THIS ONE FLAG, on purpose:
        # deleting them is a separate, deliberate decision and should not arrive
        # as a side effect of the wiring task. Flipping this True does NOT restore
        # the old build -- the unified pass still runs -- so it is a reading aid
        # and an escape hatch for a bisect, not a supported mode.
        # ------------------------------------------------------------------
        _LEGACY_ROSTER_DRAW = False

        _roster_map: dict = {}
        if _LEGACY_ROSTER_DRAW and _cw_enemies_on:
            try:
                _snaps = _roster.read_world_snapshots(
                    bef_folder, ["castle", "ice", "swamp", "under"])
                _snaps.update(_roster.read_world_snapshots(bef_folder, ["grave"]))
                _roster_map = _roster.build_roster(snapshots=_snaps)
                del _snaps
            except Exception as exc:  # noqa: BLE001
                # FAIL SOFT, NOT OPEN: with no roster there is no draw, so the
                # run falls back to today's behaviour (every active route
                # attempted) rather than to an unbounded one. The budget still
                # cannot be exceeded, because admission is skipped only when
                # there is nothing to admit against.
                print(f"  [WARN] cross-world: could not build the enemy roster "
                      f"({type(exc).__name__}: {exc}); falling back to the full "
                      f"active allowlist with no seeded draw")

        # THE USER SWITCH (Task 6, Req 7.1/7.2/7.3) is read ONCE, well ABOVE this
        # block -- it has to be, because the block's own gate and the RESOURCE
        # pool default both depend on it now that the two cross-world features
        # are independent. `_sd`, `_want_structures` and `_structures_on` are
        # simply reused here; the ENEMY gate's ceiling reads the same one value.
        if _structures_on:
            print("  cross-world: STRUCTURES ENABLED (EXPERIMENTAL) — ported "
                  "structures are COSMETIC variety only; progression and "
                  "puzzle props are excluded and every route stays CANDIDATE "
                  "until you observe it in game")

        _plans: dict[str, object] = {}
        _gates: dict[str, object] = {}
        _cands: dict[tuple[int, str], object] = {}
        placements: list[tuple[int, str, bytes]] = []

        if _roster_map:
            # ONE budget, TWO ceilings out of it. `_budget` is the shared
            # `pool_budget` value and the structure pass is handed it WHOLE;
            # `_enemy_budget` is the ceiling the ENEMY gate walks to. With
            # structures OFF the two are the same integer, byte for byte, so a
            # build without the feature is unchanged (Req 7.3).
            _budget = _sel.pool_budget(raised=raised_pool_in_effect,
                                       pool_size=resource_pool_in_effect,
                                       max_pct=occupancy_in_effect)
            _enemy_budget = _sd.enemy_budget_ceiling(
                _budget, structures_enabled=_structures_on)
            if _enemy_budget != _budget:
                print(f"  [cross-world] enemy admission ceiling LOWERED to "
                      f"{_enemy_budget:,} B of the shared {_budget:,} B draw "
                      f"budget, reserving {_sd.STRUCTURE_RESERVE_BYTES:,} B so "
                      f"cross-world STRUCTURES have room to commit out of the "
                      f"same one budget (measured: the enemy pass otherwise "
                      f"takes 99.4% of it and the cheapest structure route "
                      f"costs 38,576 B, so zero structures fit). Expect roughly "
                      f"one fewer foreign enemy arrival per world; a FOREIGN "
                      f"SHORTFALL line below, if any, is the real count and is "
                      f"not suppressed. Structures still commit only AFTER every "
                      f"enemy group for a world, and no committed enemy is ever "
                      f"revisited")
            _counts = _roster.classification_counts(_roster_map)
            for target_world in sorted(selected_worlds):
                targets_here = [e for e in _allowlist.values()
                                if e.target_world == target_world]
                if not targets_here:
                    # Requirement 7.3: say it was skipped and why, rather than
                    # producing a silently empty draw. A world has entries only
                    # once route costs were MEASURED into it, which is now ALL
                    # FIVE (grave 13, ice 12, swamp 10, under 9, castle 7). This
                    # branch is therefore not dead code but the standing rule: a
                    # world whose eligible routes all refused, or whose only
                    # measured routes are held back, gets NO entries and is
                    # LOGGED AS SKIPPED here rather than silently drawing
                    # nothing, and it stays available as a SOURCE.
                    _draw_log(f"[cross-world] {target_world.upper()} SKIPPED as "
                              f"a draw target: no allowlisted route targets it, "
                              f"so no measured cost row exists for any arrival "
                              f"into it. It remains available as a SOURCE world")
                    continue
                plan = _sel.plan_draw(
                    seed, target_world, _roster_map, _roster_costs.ROUTE_COSTS,
                    allowlist=_allowlist, raised_pool=raised_pool_in_effect,
                    pool_size=resource_pool_in_effect,
                    max_pct=occupancy_in_effect)
                _plans[target_world] = plan
                # THE LOWERED CEILING, and the only place it is applied. The
                # gate's rules are unchanged; it is simply handed a smaller
                # number when structures are enabled.
                _gates[target_world] = _sel.AdmissionGate(
                    plan, budget=_enemy_budget)

                # --- the [cross-world] draw block, Requirements 9.1-9.4 ------
                for line in plan.log_lines()[:1]:
                    _draw_log(line)
                _draw_log(f"[cross-world]   roster {sum(1 for e in _roster_map.values() if e.kind == _roster.ENEMY)} "
                          f"enemies of {len(_roster_map)} types: "
                          f"{len(_roster.eligible_foreign(_roster_map, target_world))} "
                          f"foreign eligible, "
                          f"{len(_roster.eligible_native(_roster_map, target_world))} "
                          f"native eligible")
                _draw_log("[cross-world]   excluded "
                          f"{len(_roster_map) - _counts.get(_roster.ENEMY, 0)}: "
                          + ", ".join(f"{n} {k}" for k, n in
                                      sorted(_counts.items()) if k != _roster.ENEMY))
                for line in plan.log_lines()[1:]:
                    _draw_log(line)

                for cand in plan.order:
                    source_bef = bef_folder / f"{cand.source_world.upper()}.BEF"
                    if not source_bef.exists():
                        print(f"  [WARN] cross-world: {source_bef.name} "
                              f"missing; cannot port {cand.name}")
                        continue
                    try:
                        blob = BefFile.parse(source_bef).get_entry(
                            cand.type_id).data
                    except Exception as exc:
                        print(f"  [WARN] cross-world: {cand.name} "
                              f"0x{cand.type_id:02X} blob unreadable in "
                              f"{source_bef.name}: {exc}")
                        continue
                    _cands[(cand.type_id, target_world)] = cand
                    # ORDER MATTERS: the placement list is built in the plan's
                    # order and `run_cross_world_placements` preserves it, which
                    # is what makes a refusal simply ADVANCE THE WALK.
                    placements.append((cand.type_id, target_world, blob))
        elif _LEGACY_ROSTER_DRAW and _cw_enemies_on:
            for target_world in sorted(selected_worlds):
                entries = sorted(
                    (e for e in _allowlist.values()
                     if e.target_world == target_world),
                    key=lambda e: (e.enemy_id, e.source_world))
                for entry in entries:
                    source_bef = bef_folder / f"{entry.source_world.upper()}.BEF"
                    if not source_bef.exists():
                        print(f"  [WARN] cross-world: {source_bef.name} "
                              f"missing; cannot port {entry.name}")
                        continue
                    try:
                        blob = BefFile.parse(source_bef).get_entry(
                            entry.enemy_id).data
                    except Exception as exc:
                        print(f"  [WARN] cross-world: {entry.name} "
                              f"0x{entry.enemy_id:02X} blob unreadable in "
                              f"{source_bef.name}: {exc}")
                        continue
                    placements.append((entry.enemy_id, target_world, blob))
        # ELSE: cross-world ENEMIES are off. `_roster_map` is empty because the
        # roster was never built, so neither arm ran and `placements` stays
        # empty -- deliberately, and not as an empty enemy plan dressed up as
        # one. No gate, no admission callback, no enemy allowlist.

        # Admission needs the LIVE buffer sizes, which only exist inside the
        # runner's per-world session loop -- so the runner calls back in here
        # between group commits. `_started` carries the candidates whose port has
        # begun but whose outcome has not been read yet; each is resolved from
        # the session's OWN results, so COMMIT and REFUSE come from what the
        # porter actually did rather than from a prediction.
        _started: dict[str, list] = {}

        def _sync(world: str, session) -> None:
            gate = _gates.get(world)
            if gate is None:
                return
            pending = _started.setdefault(world, [])
            while pending:
                cand = pending[0]
                outcomes = [r for r in session.results
                            if r.plan.enemy_id == cand.type_id
                            and r.plan.target_world == world]
                if not outcomes:
                    break
                if any(r.ok for r in outcomes):
                    gate.record_commit(cand)
                else:
                    last = outcomes[-1]
                    gate.record_refusal(
                        cand, (last.warnings[-1] if last.warnings
                               else "refused during porting"))
                pending.pop(0)

        def _admit_fn(plan_obj, session):
            world = plan_obj.target_world
            gate = _gates.get(world)
            cand = _cands.get((plan_obj.enemy_id, world))
            if gate is None or cand is None:
                return True     # no draw for this world: today's behaviour
            _sync(world, session)
            ok, reason = gate.consider(
                cand, lambda: len(session.prs) + len(session.prt))
            if ok:
                # THE WALK'S OWN BOOKKEEPING. `_started` was declared and read
                # by `_sync` but never appended to, so no port's outcome was
                # ever resolved: `record_commit` was never called, `admitted`
                # stayed empty, and every finished lineup came out all-native
                # while six routes had actually committed. That in turn made
                # the lineup NARROW the pool to natives only -- the same
                # "nothing foreign spawns" symptom by a second route.
                _started.setdefault(world, []).append(cand)
            return ok, reason

        # ------------------------------------------------------------------
        # CROSS-WORLD STRUCTURES (`cross-world-structures` Task 5)
        #
        # OFF BY DEFAULT, and off means ABSENT: unless the user passes
        # `--cross-world-structures` (or ticks the GUI checkbox) this block
        # builds nothing, `structure_fn` stays None, and
        # `run_cross_world_placements` takes exactly the path it took before the
        # parameter existed -- so the build is byte-identical for the same seed
        # (Requirement 7.3). With the flag ON and no explicit per-route selection
        # saved, every SELECTABLE route is a candidate and the resource budget
        # decides how many fit; a saved selection narrows that exactly.
        #
        # THE ORDER IS THE SAFETY PROPERTY. `run_cross_world_placements` calls
        # `structure_fn(session)` once per world AFTER every enemy group for that
        # world has been attempted, so the enemy arrivals are already in the
        # session's buffers and the structure pass budgets against what they
        # LEFT. A structure can never displace a committed enemy because no enemy
        # decision is revisited (Req 5.2). Both passes read the SAME
        # `roster_selection.pool_budget` value and the SAME live
        # `len(prs) + len(prt)`; there is no second budget.
        # ------------------------------------------------------------------
        # THE USER SWITCH (Task 6, Req 7.1/7.2/7.3). `--cross-world-structures`
        # / the GUI checkbox, default OFF, read here in EXACTLY ONE PLACE. It
        # replaces the module-level `STRUCTURE_PORT_ENABLED` read that used to
        # sit at the `structure_fn=` site: the kill switch stays in the module as
        # a hard floor (it is still consulted, so flipping it False kills the
        # feature no matter what the user asked for), but the user's flag is now
        # what normally decides. With the flag off `structure_fn` stays None and
        # `run_cross_world_placements` takes the path it took before the
        # parameter existed, so the build is byte-identical for the same seed.
        #
        # `_want_structures` / `_structures_on` / `_sd` are RESOLVED EARLIER, above
        # this block's own gate (which admits EITHER cross-world feature) and above
        # the RESOURCE pool default, and reused here unchanged: the enemy gate's
        # ceiling depends on the same one read.
        from . import spawn_config as _spawn_config

        # The candidate route set per world, from `spawn_config`. This is a
        # NARROWING and nothing else: `plan_structure_draw` can only REMOVE keys
        # with it, so a route the structure allowlist never offered or the cost
        # table never measured cannot be turned on from a config file.
        #
        # BUGFIX. This used to be `spawn.enabled_structure_arrival_keys(_w)`
        # alone, substituting `set()` when there was no config. Because every
        # stock route ships DISABLED and an old config file has no
        # `structure_arrivals` section at all, that set was ALWAYS empty and
        # ticking the switch could never port a structure.
        # `spawn_config.structure_routes_for_world` fixes the ANSWER without
        # touching the PERMISSION: when the user made no explicit selection the
        # candidate set is `selectable_structure_arrival_keys(_w)` -- the
        # permission oracle itself, so no withheld / refused / unmeasured /
        # inactive / held-back route can appear -- and the existing resource
        # budget decides how many fit. When the user DID select, that subset is
        # honoured exactly, empty included.
        _structure_routes: dict[str, set] | None = None
        if _structures_on:
            _structure_routes = {}
            for _w in sorted(selected_worlds):
                _routes, _case = _spawn_config.structure_routes_for_world(
                    spawn, _w)
                _structure_routes[_w] = _routes
                if _case == _spawn_config.STRUCTURE_ROUTES_CASE_FALLBACK:
                    print(f"  [cross-world] [{_w}] no explicit structure "
                          f"selection was saved, so ALL "
                          f"{len(_routes)} selectable structure route(s) are "
                          f"candidate — the resource budget decides how many "
                          f"fit")
                elif _routes:
                    print(f"  [cross-world] [{_w}] using your saved structure "
                          f"selection: {len(_routes)} route(s) selected")
                else:
                    print(f"  [cross-world] [{_w}] your saved structure "
                          f"selection has no routes ticked for this world, so "
                          f"nothing is ported here (not an error)")

        _structure_results: dict[str, object] = {}

        def _structure_fn(session):
            # THE FULL budget, deliberately: the reserve is spent by lowering the
            # ENEMY ceiling, not by handing the structure pass a second number.
            # It still reads the live `len(prs)+len(prt)`, so what it can spend is
            # whatever the enemy pass actually left -- which is now at least the
            # reserve, because the enemy walk stopped short of it.
            #
            # ON A STRUCTURES-ONLY RUN this is the WHOLE budget with nothing
            # subtracted, and no ceiling is lowered anywhere: there is no enemy
            # pass to reserve against, so `enemy_budget_ceiling` is never
            # consulted (it is read only where an enemy gate is actually built).
            # The number handed in here is the same `pool_budget(...)` either way.
            _res = _sd.run_structure_pass(
                session, seed=seed,
                budget=_sel.pool_budget(raised=raised_pool_in_effect,
                                        pool_size=resource_pool_in_effect,
                                        max_pct=occupancy_in_effect),
                game_files_dir=bef_folder, log=_cw_log,
                enabled_routes=(_structure_routes or {}).get(
                    session.target_world, set()))
            _structure_results[session.target_world] = _res
            for _line in _res.log:
                _draw_log(_line)
            return _res

        # ------------------------------------------------------------------
        # THE UNIFIED PASS (unified-randomization task 4).
        #
        # ONE entry point, ONE record per target world, BOTH admission phases,
        # the REAL live-size read (the session's own
        # `len(prs) + len(prt)`) and the REAL gated port path. `run_structure_pass`
        # and the enemy `AdmissionGate` are not called at all: the phase order IS
        # the enemy priority and the `STRUCTURE_RESERVE_BYTES` slice, both carved
        # inside `unified_admit` out of the SAME one budget resolved below.
        #
        # THE TWO SWITCHES STAY INDEPENDENT. `enemies_enabled` and
        # `structures_enabled` are passed separately, so enemies-only,
        # structures-only and both-on all work; with structures off phase 2 does
        # not run and no reserve is carved.
        #
        # WITH NO SAVED CONFIG the stock v4 table is used rather than nothing:
        # `SpawnConfig.default()` reproduces today's per-category behaviour, and
        # drawing from an absent config would silently port nothing.
        # ------------------------------------------------------------------
        from . import unified_report as _ureport

        cw_sessions: dict[str, object] = {}
        cw_files: dict[str, bytes] = {}
        _unified_results: dict[str, object] = {}
        _unified_cfg = unified_cfg
        if spawn is None:
            print("  [cross-world] no spawn config was loaded, so the STOCK "
                  "version-4 unified table is in effect (it reproduces today's "
                  "per-category behaviour)")
        # ONE budget, derived from the pool constant this build actually patched
        # and the occupancy resolved above -- never a literal, so a pool change
        # moves it with no second edit.
        _unified_budget = _sel.pool_budget(raised=raised_pool_in_effect,
                                           pool_size=resource_pool_in_effect,
                                           max_pct=occupancy_in_effect)
        _unified_worlds = tuple(sorted(selected_worlds))
        if _unified_worlds:
            cw_files, _unified_results = _cw.run_unified_cross_world(
                weights=_unified_cfg, seed=seed, game_files_dir=bef_folder,
                worlds=_unified_worlds,
                enemies_enabled=_cw_enemies_on,
                structures_enabled=_structures_on,
                budget=_unified_budget, log=_cw_log,
                sessions_out=cw_sessions,
                allowlist=(_oracle_allowlist if _cw_enemies_on else None))
            _ureport.report(_unified_results, config=_unified_cfg, seed=seed,
                            structures_enabled=_structures_on,
                            enemies_enabled=_cw_enemies_on,
                            emit=lambda line: print(f"  {line}"))

        # WHAT COMMITTED, per world, in ONE place. Read off the record rather
        # than re-derived from `session.results`, because the record is what the
        # pass decided and `session.results` mixes the two categories -- feeding
        # structure ids to the enemy half is exactly what Req 2.3 forbids.
        _committed_enemy_by_world: dict[str, tuple] = {
            _w: tuple(_r.committed_enemies)
            for _w, _r in _unified_results.items()}
        _committed_structures_by_world: dict[str, tuple] = {
            _w: tuple(_r.committed_structures)
            for _w, _r in _unified_results.items()}
        # ADMISSION IS OVER FOR EVERY WORLD BY HERE, so handing these to the
        # retyping projection cannot interleave the two draws (Property 5).
        for _w, _r in _unified_results.items():
            committed_routes_by_world[_w] = tuple(_r.committed_routes)
            # STRICTLY the committed structures -- not the allowlist, not the
            # config, not the drawn order. A type only gets a template if its
            # assets genuinely landed in this world's BEF/PRS/PRT.
            committed_structure_routes_by_world[_w] = tuple(
                (int(_c.type_id), _c.source_world, _c.target_world)
                for _c in _r.committed_structures)

        # Close the walk: read the last group's outcome, append the drawn
        # natives, check the bound and the floors, and log the result block.
        # `_admitted_by_world` keeps the COMMITTED candidates in DRAW ORDER for
        # the sound-bank merge below, which admits them in that same order.
        # THE COMMITTED ENEMY ROUTES, in the order the unified pass committed
        # them, for the sound-bank merge below. `Candidate` already carries
        # `type_id`, `source_world` and `name`, which is exactly what
        # `sound_port.merge_for_lineup` reads, so no shim is needed.
        #
        # `lineup_by_world` is deliberately LEFT EMPTY by the unified pass: the
        # legacy draw's native half and its `LINEUP_MAX` cap are retired, so
        # there is no lineup to narrow the enemy pool with. An empty entry means
        # "no draw for this world", which the callers below already handle by
        # using the un-narrowed guarded roster -- the unified weights are what
        # decide how often a type is picked now.
        _admitted_by_world: dict[str, tuple] = dict(_committed_enemy_by_world)
        for _world, _gate in sorted(_gates.items()):
            _session = cw_sessions.get(_world)
            if _session is None:
                continue
            _sync(_world, _session)
            # NOT THE LIVE SIZE, and this is the whole of the fix for the
            # `PoolBudgetExceeded` a real build hit once the structure reserve
            # existed. `run_cross_world_placements` calls `structure_fn(session)`
            # once per world BEFORE `session.finish()`, so by the time we get
            # here the live `len(prs) + len(prt)` ALREADY INCLUDES every byte the
            # STRUCTURE pass committed. This gate accounts for ENEMY spend only:
            # its ceiling is `pool_budget - STRUCTURE_RESERVE_BYTES`, and the
            # structure bytes were admitted AFTER it, out of that reserve, by the
            # structure pass's own gate against the FULL budget. Handing the
            # enemy gate the live size makes it answerable for bytes it never
            # admitted and cannot account for, against a ceiling deliberately set
            # 300,000 B below the shared budget -- so it refused to ship a build
            # that was in fact within both bounds. Before the reserve existed
            # both ceilings were the same number and the enemy walk's leftover
            # slack absorbed the structure spend, which is why the mistake was
            # invisible rather than absent.
            #
            # `StructureDrawResult.baseline_bytes` is exactly the number wanted:
            # `run_structure_pass` captures it as `baseline = live()` before it
            # draws anything. With no structure result for this world (structures
            # off, or no pass ran here) the live size IS the enemy total, so it
            # is used unchanged and an off build is byte-identical.
            #
            # THE BOUND ITSELF IS NOT TOUCHED. If the ENEMY spend genuinely
            # exceeds the enemy ceiling this still raises, which is correct.
            _sres_here = _structure_results.get(_world)
            _enemy_resident = (int(_sres_here.baseline_bytes)
                               if _sres_here is not None
                               else len(_session.prs) + len(_session.prt))
            _result = _gate.finish(_enemy_resident)
            _admitted_by_world[_world] = tuple(_result.admitted)
            for line in _result.log:
                _draw_log(line)
            lineup_by_world[_world] = _result.lineup
            _draw_log(f"[cross-world]   lineup types: "
                      + " ".join(f"0x{t:02X}" for t in _result.lineup)
                      + f"  (reproduce with --seed {seed})")

        # Single success/failure line a user can read at a glance, printed on
        # both the folder and the ISO path (bef_folder is the extracted asset
        # folder in ISO mode).
        if cw_sessions:
            _tex = ", ".join(
                f"{_w.upper()} .PRT textures="
                f"{getattr(_s, 'local_tex_count', '?')}"
                for _w, _s in sorted(cw_sessions.items()))
            print(f"  cross-world: OK — assets from {bef_folder}, {_tex}")
        elif placements:
            # Placements existed but no session was built: every placement was
            # skipped or refused before a world session opened.
            print(f"  cross-world: NO PORTS COMPLETED — assets from "
                  f"{bef_folder}; every placement was skipped or refused. "
                  f"Cross-world enemies will not appear in game.")

        # Route the returned buffers into the patched-output folder — the same
        # mechanism every grown asset uses. write_patched_assets picks up
        # anything present there by uppercase filename.
        for fname, data in sorted(cw_files.items()):
            (out_folder / fname).write_bytes(data)
            print(f"  cross-world: {fname} -> {out_folder.name} "
                  f"({len(data)} B)")

        # ------------------------------------------------------------------
        # SOUND: the merged `.BD`, for the enemies that actually COMMITTED.
        #
        # A ported enemy's samples stay in its home world's bank, and
        # `GetSpotFXID` linear-searches the LOADED bank's explicit EffectID
        # field -- so an id GRAVE.BD does not carry is simply not found and the
        # enemy is SILENT, with nothing logged and nothing failed. The merge
        # appends the foreign samples to the target world's own bank and keeps
        # the FILENAME, so bank 208 still loads it through the driver's existing
        # static path table: no EE patch, no IRX patch, no new bank id.
        #
        # The SPU budget is a HARD REFUSAL, exactly like the RESOURCE pool
        # budget, because the driver has no ceiling check and an overflow
        # corrupts SPU RAM silently. An enemy whose sounds do not fit stays
        # silent and is REFUSED WITH A LOG LINE.
        #
        # Only COMMITTED enemies are considered: a refused port left no enemy
        # to hear, so spending budget on it would buy nothing.
        # ------------------------------------------------------------------
        # SKIPPED ENTIRELY when cross-world ENEMIES are off. The merge exists so a
        # ported ENEMY is audible; a structures-only run ported no enemy, so there
        # is no foreign sample to import and nothing to reclaim against. Feeding
        # it the structure commits instead would build a lineup out of types the
        # enemy placer must never see (Req 2.3) and spend SPU budget on samples
        # nothing plays.
        if _cw_enemies_on:
            from . import sound_port as _sound

        for _world, _session in (sorted(cw_sessions.items()) if _cw_enemies_on
                                 else ()):
            _admitted = _admitted_by_world.get(_world)
            if _admitted is None:
                # No seeded draw for this world (roster unavailable). The
                # committed set still comes from what the porter ACTUALLY did,
                # in the order it did it, so the sound pass never gets ahead of
                # the port pass.
                from types import SimpleNamespace
                _seen: set = set()
                _shims = []
                for _r in _session.results:
                    if not _r.ok or _r.plan.enemy_id in _seen:
                        continue
                    _seen.add(_r.plan.enemy_id)
                    _shims.append(SimpleNamespace(
                        type_id=_r.plan.enemy_id,
                        source_world=(_r.plan.source_world or ""),
                        name=_r.plan.name))
                _admitted = tuple(_shims)
            if not _admitted:
                continue
            # THE LINEUP IS WHAT MAKES RECLAMATION SAFE. It is the final roster
            # this world ships -- foreign committed plus drawn natives -- so a
            # walking-melee type absent from it cannot spawn and its sounds
            # cannot be played. Passing None (no seeded draw) reclaims NOTHING,
            # which is the fail-closed direction.
            _bd, _merge, _problems = _sound.merge_for_lineup(
                _admitted, game_files_dir=bef_folder, target_world=_world,
                lineup=lineup_by_world.get(_world), log=_draw_log)
            if _bd is None:
                continue
            _bd_name = f"{_world.upper()}.BD"
            (out_folder / _bd_name).write_bytes(_bd)
            _freed = (_merge.reclamation.freed_bytes
                      if _merge.reclamation is not None else 0)
            print(f"  cross-world: {_bd_name} -> {out_folder.name} "
                  f"({len(_bd)} B, -{_freed} B reclaimed, "
                  f"+{_merge.added_bytes} B imported, "
                  f"{_merge.bank.count} entries, occupancy "
                  f"{_merge.occupancy:,}/{_merge.ceiling:,} B)")

        # Register the types that were genuinely ported this run so the asset
        # guard (catalog_permitted_types / permitted_types_for_world) admits
        # them. Without this the mesh is ported and then never placed.
        for _world, _session in sorted(cw_sessions.items()):
            # ENEMY ids only. `committed_ported_types` reads `session.results`,
            # and a structure route commits through the SAME `session.port_enemy`
            # (that is the point of Task 1's finding), so after the structure pass
            # this set contains structure type ids too. Permitting them twice is
            # harmless -- the structure loop below permits exactly the same ids --
            # but the lineup repair a few lines down would add a STRUCTURE to the
            # ENEMY lineup, and offering a structure to the enemy placer is what
            # Requirement 2.3 forbids. Subtract what the structure pass committed
            # so each loop reports and permits its own category.
            _s_committed_here = {int(_c.type_id) for _c in
                                 _committed_structures_by_world.get(_world, ())}
            _ported = committed_ported_types(_session) - _s_committed_here
            if not _ported:
                continue
            ported_types_by_world.setdefault(_world, set()).update(_ported)
            bef_native_types.setdefault(_world, set()).update(_ported)
            # The lineup NARROWS the pool, so it MUST CONTAIN everything that
            # committed — otherwise the run ports a mesh and then forbids it,
            # which is the same "nothing foreign spawns" symptom by a different
            # route. Repair rather than silently ship a lineup that drops a
            # committed arrival, and say so.
            _lineup_now = lineup_by_world.get(_world)
            if _lineup_now is not None:
                _dropped = _ported - set(_lineup_now)
                if _dropped:
                    print(f"  {_cw.LOG_PREFIX} [{_world}] lineup did not "
                          f"contain committed arrival(s) "
                          + " ".join(f"0x{t:02X}" for t in sorted(_dropped))
                          + " — added back (committed always outranks lineup)")
                    lineup_by_world[_world] = tuple(_lineup_now) + tuple(
                        sorted(_dropped))
            _names = [name_for_type(t) for t in sorted(_ported)]
            print(f"  cross-world [{_world}]: ported types now permitted -> "
                  f"{', '.join(_names)}")
            for _r in _session.results:
                if _r.ok and _r.fallback:
                    print(f"  cross-world [{_world}]: FALLBACK "
                          f"{_r.plan.name} 0x{_r.plan.enemy_id:02X} — "
                          f"{_r.warnings[0] if _r.warnings else 'see log above'}")

        # ------------------------------------------------------------------
        # PERMIT THE COMMITTED STRUCTURES AS DESTINATIONS (Req 8.3).
        #
        # THE SAME MECHANISM as the enemy loop above, and deliberately no other:
        # a type becomes placeable by entering `ported_types_by_world` (the
        # catalog half of the asset guard) and `bef_native_types` (the BEF half,
        # which was read off disc before this run injected anything). Structures
        # are then placed by the ITEM pool: `catalog_permitted_pool_types` leaves
        # non-enemy pool types permitted, the item pass excludes
        # `ALL_POOL_BEF_TYPES - permitted`, and every structure type is in
        # `UNIVERSAL_ITEM_POOL`. So this widens the TYPE pool and touches nothing
        # about which RECORDS are eligible (Req 3.5) -- the protections on
        # destinations (PROTECTED_TAGS, PROTECTED_INSTANCE_NAMES, the end* kill
        # groups, the Gold_Key / Level_Column / Skeleton_Key / Monster_Generator
        # anchors, HAZARD_PROTECTED_AS_SOURCE) all still run unchanged inside the
        # item pass.
        #
        # THE ENEMY LINEUP IS NOT TOUCHED. `lineup_by_world` narrows the ENEMY
        # pool; adding a structure to it would offer a structure to the enemy
        # placer, which Requirement 2.3 forbids.
        # ------------------------------------------------------------------
        for _world, _sres in sorted(_unified_results.items()):
            _s_ported = {int(_c.type_id) for _c in _sres.committed_structures}
            if not _s_ported:
                print(f"  cross-world [{_world}]: no structure arrivals "
                      f"committed ({len(_sres.skipped_budget)} skipped on "
                      f"budget, {len(_sres.refused_gate)} refused by a gate, "
                      f"{len(_sres.excluded_rule)} excluded by rule)")
                continue
            ported_types_by_world.setdefault(_world, set()).update(_s_ported)
            bef_native_types.setdefault(_world, set()).update(_s_ported)
            # AND record them as ARRIVALS so the item pass can weight them with
            # the number the editor's Weight column actually shows. Permission is
            # already granted above; this only decides how OFTEN a permitted
            # arrival is picked. Populated only from what COMMITTED, so a refused
            # or budget-skipped route never biases anything.
            structure_arrival_types_by_world.setdefault(
                _world, set()).update(_s_ported)
            # A permitted type with no RECORD TEMPLATE in this world cannot be
            # placed at all (`items.randomize_universal`'s `t in templates` gate
            # drops it before its weight is read), so the permit would be inert.
            # The template injection is NOT done here: it walks
            # `committed_structure_routes_by_world` so it can prefer the route's
            # SOURCE world and log the unplaceable leftovers. See
            # "STRUCTURE TEMPLATE INJECTION" further down, just before the
            # per-file loop.
            print(f"  cross-world [{_world}]: ported structure types now "
                  f"permitted -> "
                  + ", ".join(name_for_type(t) for t in sorted(_s_ported)))

        print()
        # EVERYTHING BELOW IS THE ENEMY HALF, so it runs only when cross-world
        # ENEMIES are on. A structures-only run must not borrow ENEMY templates
        # and must not replace `pool_for_world`: the enemy pool of a build that
        # did not ask for cross-world enemies stays exactly what it was.
        # Build a global template bank from ALL worlds
        global_enemy_templates: dict[int, "PsxRecord"] = {}
        for world_slot in (world_templates.values() if _cw_enemies_on else ()):
            for tid, tpl in world_slot.items():
                if tid not in global_enemy_templates:
                    global_enemy_templates[tid] = tpl

        # For each world, borrow templates ONLY for types its BEF has
        for world in (sorted(selected_worlds) if _cw_enemies_on else ()):
            slot = world_templates.setdefault(world, {})
            native = bef_native_types.get(world, set())
            # RULE: BEF-declared is NOT permission. Permission comes from the
            # asset guard. GRAVE.BEF declares leftover 0x0D Zombie_Crocodile
            # and 0x16 Axe_Guard whose meshes are not usable in Grave; borrowing
            # a template for them re-introduced the contaminated-GRAVE.BEF bug
            # (wrong models placed even with porting parked). Intersect the
            # borrow set with catalog_permitted_types (catalog + ported-this-run;
            # the `native &` below supplies the BEF half of the guard).
            native = native & catalog_permitted_types(world)
            # Types in BEF but without a template yet
            bef_enemies = native & set(WALKING_MELEE_POOL.keys())
            missing = bef_enemies - set(slot.keys()) - excluded_types
            borrowed = []
            for tid in missing:
                if tid in global_enemy_templates:
                    slot[tid] = global_enemy_templates[tid]
                    borrowed.append(tid)
            if borrowed:
                names = [name_for_type(t) for t in sorted(borrowed)]
                print(f"  cross-world [{world}]: +{len(borrowed)} borrowed "
                      f"templates -> {', '.join(names)}")

        # Override pool_for_world to BEF-declared AND catalog-safe types only.
        # BEF declaration alone is not permission — a declared-but-unported
        # type has a mesh index pointing into another world's PRS.
        #
        # ONLY when cross-world ENEMIES are on. This override is the cross-world
        # ENEMY pool; a structures-only build never asked for it, and installing
        # it there would silently change which enemies that build spawns.
        if _cw_enemies_on:
            def pool_for_world(world: str) -> set[int]:
                permitted = permitted_types_for_world(world)
                if permitted is None:
                    permitted = set()
                # THIS IS WHERE THE DRAWN NATIVES TAKE EFFECT. Without the lineup
                # the placer would keep drawing from every native the world owns,
                # so the native half of the seeded draw (Requirement 4.8) would
                # change nothing that shows up in game. The lineup NARROWS;
                # permission is still the asset guard's `permitted`, intersected
                # below, so a type the guard rejects cannot be admitted by being
                # drawn.
                return enemy_pool_for_world(
                    world, permitted=permitted,
                    lineup=lineup_by_world.get(world),
                    excluded=excluded_types,
                    disabled=(spawn.disabled_enemies(world)
                              if spawn is not None else frozenset()))

        print()


    def enemy_source_types_for_world(world: str) -> set[int]:
        """Which records in `world` COUNT AS a swappable enemy slot.

        This is the SOURCE set and it is deliberately NOT the destination pool.
        `pool_for_world` is narrowed by the seeded lineup, because the lineup is
        the roster a world SHIPS -- i.e. what an enemy may be turned INTO. Using
        that same narrowed set to decide what may be turned FROM is what broke
        enemy randomization: for a Grave draw whose lineup came out all-foreign
        (6 foreign, 0 native), the source set became six types that do not occur
        anywhere in a vanilla Grave map, so not one record matched, not one swap
        happened, and no ported enemy ever appeared -- the mesh was ported and
        then had nothing to attach to. A seed that happened to draw one native
        (e.g. 0x2F Axe_Skeleton) produced the same bug in weaker form: only that
        one vanilla type was swappable, so foreign arrivals showed up a handful
        of times and never in maps lacking it.

        See `enemy_source_types`, which owns the rule; this wrapper only
        supplies this run's guard verdict, exclusions and spawn-config state.
        Spawn-config-disabled types are added by the caller so their vanilla
        instances get retyped into something enabled.
        """
        if not lineup_by_world.get(world):
            # No draw for this world: the destination pool is already the
            # un-narrowed guarded roster, so reuse it unchanged.
            return set(pool_for_world(world))
        return enemy_source_types(
            world, permitted=permitted_types_for_world(world),
            excluded=excluded_types,
            disabled=(spawn.disabled_enemies(world)
                      if spawn is not None else frozenset()))

    # ----------------------------------------------------------------------
    # STRUCTURE TEMPLATE INJECTION (the reason ported structures never spawned)
    #
    # `items.randomize_universal` builds its destination list as
    #     [t for t in pool if t in templates and weights.get(t, 0) > 0 ...]
    # so a type with no TEMPLATE RECORD is dropped BEFORE its weight is read.
    # `extra_templates` is `world_item_templates[world]`, harvested from that
    # world's OWN .PSX files -- and a cross-world arrival is by definition a type
    # the target world has no records of. Its weight was computed and discarded.
    #
    # Fix: generalise the `GLOBAL_TEMPLATE_TYPES` (Gold_Key) mechanism. For every
    # COMMITTED structure route, lend the TARGET world a template record of that
    # type harvested from the ROUTE'S SOURCE world.
    #
    # GATED STRICTLY ON THE COMMITTED SET. Only `committed_structures` routes are
    # walked -- never the allowlist, the config or the drawn order. Injecting a
    # template for a type whose mesh/art did NOT land in this world's
    # BEF/PRS/PRT would place a record the engine cannot render (invisible at
    # best, infinite load or crash at worst).
    #
    # NOT PERMISSION, NOT SIZE RELAXATION. A route still needs its allowlist row,
    # its measured cost row, and must have committed. Retyping stays
    # size-constrained (`SAME_SIZE_GROUPS` / `make_record_with_template`): an
    # injected template whose size class has no victim in the target world simply
    # never gets chosen, which is the correct harmless outcome. Victim-selection
    # filters (every Requirement 7 protection) are untouched -- this changes what
    # a slot may BECOME, never which slots are ELIGIBLE.
    #
    # Reuses the already-harvested `world_item_templates`; no .PSX is re-parsed.
    # ----------------------------------------------------------------------
    # Imported locally: `_cw` above is bound only inside the cross-world block,
    # and this loop is a no-op on a build that never ran it.
    from .cross_world import LOG_PREFIX as _CW_TAG
    for _tgt_world in sorted(committed_structure_routes_by_world):
        _routes = committed_structure_routes_by_world.get(_tgt_world, ())
        if not _routes:
            continue
        _tgt_slot = world_item_templates.setdefault(_tgt_world, {})
        _injected: list[tuple[int, str]] = []
        _native: list[int] = []
        _unplaceable: list[tuple[int, str]] = []
        for _tid, _src_world, _route_tgt in _routes:
            if _route_tgt != _tgt_world:
                # Defensive: the results dict is already keyed per target world.
                continue
            if _tid in _tgt_slot:
                # The target world owns a record of this type: its own template
                # is the better one. `setdefault` semantics, made explicit.
                _native.append(_tid)
                continue
            _tpl = world_item_templates.get(_src_world, {}).get(_tid)
            if _tpl is None:
                # No template in the source world's harvested slot. Do NOT
                # synthesise a record, fall back to another type, or parse extra
                # files. A structure that ported but cannot be placed is a
                # LOGGED MISS, never a build failure.
                _unplaceable.append((_tid, _src_world))
                continue
            _tgt_slot.setdefault(_tid, _tpl)
            _injected.append((_tid, _src_world))
        for _tid, _src_world in _injected:
            print(f"  {_CW_TAG} [{_tgt_world}]: injected structure "
                  f"template {name_for_type(_tid)} (0x{_tid:02X}) from "
                  f"{_src_world} -- now placeable by retyping")
        if _native:
            print(f"  {_CW_TAG} [{_tgt_world}]: kept native template(s) "
                  f"for " + ", ".join(
                      f"{name_for_type(t)} (0x{t:02X})"
                      for t in sorted(set(_native))))
        for _tid, _src_world in _unplaceable:
            print(f"  {_CW_TAG} [{_tgt_world}]: PORTED BUT UNPLACEABLE "
                  f"{name_for_type(_tid)} (0x{_tid:02X}) -- no template record "
                  f"for it in source world {_src_world}; assets are in place "
                  f"but no retyping destination can become it")

    # ----------------------------------------------------------------------
    # CROSS-WORLD ENEMY ARRIVALS INTO THE BOSS ARENAS (`--boss-arena-arrivals`)
    #
    # OFF BY DEFAULT, and off means ABSENT: with the flag unset this block runs
    # nothing, opens no session, writes no file and the build is byte-identical
    # for the same seed.
    #
    # INDEPENDENT OF EVERYTHING ELSE. It is deliberately NOT nested inside the
    # cross-world block: an arena is its own archive trio with its own budget,
    # so it neither spends nor is spent by a world's. `--duplicate-bosses` and
    # `--inject-boss` are untouched -- the per-file loop below still runs the
    # duplication branch FIRST and unchanged, so a file that branch handles is
    # never touched here.
    #
    # BATCHING: `boss_arena.port_arena_arrivals` walks ONE arena at a time and
    # releases each session before the next opens, so at most four binaries are
    # live (the arena's .PRS + .PRT + .BEF, plus one source .BEF parsed and
    # dropped per route) and only ONE .PRS is ever resident.
    #
    # EVERY COST BELOW IS A PREDICTION and the arena budget is UNVERIFIED on
    # console. Both are documented where they are computed
    # (`boss_arena.predicted_cost`, `boss_arena.arena_budget`) and both are said
    # out loud in the log.
    # ----------------------------------------------------------------------
    _arena_on = bool(getattr(args, "boss_arena_arrivals", False))
    # easy 3 / normal 6 / hard 9 arrivals per arena. Read once here so the
    # admission walk and the placer below cannot disagree about the cap.
    _arena_difficulty = getattr(args, "boss_arena_difficulty", None)
    _arena_results: dict = {}
    _arena_templates: dict = {}
    if _arena_on:
        from . import boss_arena as _ba
        from .catalog import load_allowlist as _ba_load_allowlist

        def _ba_log(line: str) -> None:
            print(f"  {line}")

        print("Boss-arena ENEMY arrivals ENABLED (EXPERIMENTAL, opt-in): "
              "foreign enemies may be ported into the `_B` arena archives and "
              "retyped over PROVEN-DECORATIVE arena records only. Route costs "
              "into an arena are PREDICTED from the enemy's measured row into "
              "the arena's own world, and the arena budget is UNVERIFIED on "
              "console.")
        try:
            _arena_files, _arena_results = _ba.port_arena_arrivals(
                game_files_dir=bef_folder, seed=seed,
                worlds=selected_worlds,
                # NO `difficulty` HERE ON PURPOSE. This call decides what gets
                # PORTED, which is always `MAX_PORTED_TYPES_PER_ARENA` (3)
                # distinct types because porting is what spends archive bytes
                # against an unverified budget. Difficulty only sizes how many
                # enemies SPAWN, and is passed to `plan_arena_retypes` below.
                # THE PERMISSION ORACLE, UNWIDENED: `load_allowlist()` is
                # exactly the `catalog.CROSS_WORLD_ALLOWLIST` rows with
                # `active=True`, and `classify_placement` is still what decides.
                allowlist=_ba_load_allowlist(),
                raised_pool=raised_pool_in_effect,
                pool_size=resource_pool_in_effect,
                max_pct=occupancy_in_effect,
                log=_ba_log)
        except Exception as _exc:  # noqa: BLE001 - never lose a build over this
            print(f"  [WARN] {_ba.LOG_PREFIX} boss-arena arrivals could not "
                  f"run ({type(_exc).__name__}: {_exc}); the arenas ship "
                  f"vanilla")
            _arena_files = {}
        # Never outside this build's own output folder, same as every other
        # grown asset.
        for _fname, _data in sorted(_arena_files.items()):
            (out_folder / _fname).write_bytes(_data)
            print(f"  {_ba.LOG_PREFIX} arena {_fname} -> {out_folder.name} "
                  f"({len(_data)} B)")
        # THE TEMPLATE SLOT PER ARENA, gated STRICTLY on the route having
        # COMMITTED. Harvested from the ROUTE'S SOURCE world's already-harvested
        # `world_item_templates` slot -- no .PSX is re-parsed and no record is
        # ever synthesised.
        for _psx_name, _res in sorted(_arena_results.items()):
            _arena_templates[_psx_name] = _ba.harvest_templates(
                _res, world_item_templates, log=_ba_log)
            print(f"  {_ba.LOG_PREFIX} arena {_res.spec.label}: "
                  f"{len(_res.committed)} committed, "
                  f"{len(_res.skipped_budget)} skipped on budget, "
                  f"{len(_res.refused_gate)} refused by a gate, "
                  f"{len(_res.excluded_rule)} excluded by rule")

    summary = {
        "seed": seed,
        "modes": {
            "enemies": not no_enemies,
            "items": do_items,
            "chests": do_chests,
            "skills": do_skills,
            "columns": do_columns,
            "worlds": sorted(selected_worlds),
        },
        "files_processed": [],
        "errors": [],
    }

    for path in psx_files:
        # Boss-arena maps are scripted encounters — leave them alone, UNLESS
        # the experimental boss-duplication mode is enabled for this file.
        if path.name.upper() in BOSS_FILES:
            if dup_bosses and path.name.upper() in BOSS_DUPLICATION:
                try:
                    bpsx = PsxFile.parse(path)
                except Exception as e:
                    print(f"  [SKIP] {path.name}: {e}")
                    continue
                boss_world = BOSS_FILE_WORLD.get(path.name.upper())
                dup_count = _boss_clone_by_world.get(boss_world, _legacy_clones)
                boss_repl = duplicate_boss(bpsx, path.name, count=dup_count)
                if not boss_repl:
                    print(f"  [SKIP] {path.name}: boss arena (no clone slot, not written)")
                    continue
                out_path = out_folder / path.name
                try:
                    stats = bpsx.write_with_replacements(boss_repl, out_path)
                    print(f"  [BOSS] {path.name}: +{len(boss_repl)} boss clone(s) "
                          f"({boss_world}), shift={stats['total_shift']}b")
                    summary["files_processed"].append({
                        "file": path.name, "world": "boss",
                        "changes": len(boss_repl), "shift": stats["total_shift"],
                        "seed": seed, "mode": "boss_duplication",
                    })
                except Exception as e:
                    print(f"  [FAIL] {path.name}: {e}")
                    summary["errors"].append({"file": path.name, "error": str(e)})
                continue
            # CROSS-WORLD ARRIVALS INTO THIS ARENA. Reached only with
            # `--boss-arena-arrivals`, only for an arena whose routes COMMITTED,
            # and only AFTER the boss-duplication branch above has declined the
            # file -- so `--duplicate-bosses` keeps its exact old behaviour and
            # the two features never both write one arena.
            _arena_res = _arena_results.get(path.name.upper())
            _arena_tpl = _arena_templates.get(path.name.upper()) or {}
            if _arena_res is not None and _arena_res.committed and _arena_tpl:
                try:
                    bpsx = PsxFile.parse(path)
                except Exception as e:
                    print(f"  [SKIP] {path.name}: {e}")
                    continue
                from . import boss_arena as _ba2
                # TWO MODES, AND THE ARENA DECIDES WHICH. A RETYPE arena recycles
                # a proven-decorative record; an APPEND arena (the queen, which
                # has none) GAINS records after a byte-identical carrier and
                # needs the record-count header patch to go with them.
                _arena_hdr: dict = {}
                if _arena_res.spec.append_mode:
                    _arena_repl, _arena_hdr, _added = _ba2.plan_arena_appends(
                        bpsx, _arena_res.spec,
                        arrivals=[t for t, _s, _n in _arena_res.committed],
                        templates=_arena_tpl, seed=seed,
                        difficulty=_arena_difficulty,
                        log=lambda line: print(f"  {line}"))
                    # PROVEN IN BYTES BEFORE ANYTHING IS WRITTEN. A failure here
                    # means the assembled records section and its count do not
                    # agree, or an existing record was altered, so the arena
                    # ships VANILLA rather than risk an unloadable file.
                    _ok, _fails = _ba2.verify_arena_appends(
                        bpsx, _arena_res.spec, _arena_repl, _arena_hdr, _added,
                        log=lambda line: print(f"  {line}"))
                    if not _ok:
                        print(f"  [SKIP] {path.name}: boss arena (append "
                              f"verification failed, left vanilla)")
                        continue
                else:
                    _arena_repl = _ba2.plan_arena_retypes(
                        bpsx, _arena_res.spec,
                        arrivals=[t for t, _s, _n in _arena_res.committed],
                        templates=_arena_tpl, seed=seed,
                        # HOW MANY ENEMIES SPAWN: easy 3 / normal 6 / hard 9,
                        # filled by reusing the three ported types across the
                        # arena's decorative slots.
                        difficulty=_arena_difficulty,
                        log=lambda line: print(f"  {line}"))
                if not _arena_repl:
                    print(f"  [SKIP] {path.name}: boss arena (no arrival could "
                          f"be placed, not written)")
                    continue
                out_path = out_folder / path.name
                try:
                    stats = bpsx.write_with_replacements(
                        _arena_repl, out_path,
                        # EMPTY for a retype arena, so its output is unchanged.
                        header_patches=(_arena_hdr or None))
                    print(f"  {_ba2.LOG_PREFIX} [ARENA] {path.name}: "
                          f"{len(_arena_repl)} decorative record(s) retyped "
                          f"into cross-world arrival(s), "
                          f"shift={stats['total_shift']}b")
                    summary["files_processed"].append({
                        "file": path.name, "world": "boss_arena",
                        "changes": len(_arena_repl),
                        "shift": stats["total_shift"],
                        "seed": seed, "mode": "boss_arena_arrivals",
                    })
                except Exception as e:
                    print(f"  [FAIL] {path.name}: {e}")
                    summary["errors"].append({"file": path.name,
                                              "error": str(e)})
                continue
            print(f"  [SKIP] {path.name}: boss arena (not randomized, not written)")
            continue
        # Each file gets its own seed derived from master
        file_seed = rng_master.randrange(1 << 30)
        psx = parsed_psx.get(path)
        if psx is None:
            try:
                psx = PsxFile.parse(path)
            except Exception as e:
                print(f"  [SKIP] {path.name}: {e}")
                summary["errors"].append({"file": path.name, "error": str(e)})
                continue

        world = get_world_from_bef_path(psx.bef_path)
        if world is None:
            # Unknown world — don't write filler files.
            print(f"  [SKIP] {path.name}: unknown world (not written to output)")
            continue

        if world not in selected_worlds:
            print(f"  [SKIP] {path.name}: world '{world}' not in selected set")
            continue

        # Build all replacements for this file
        all_repl = {}
        rng = random.Random(file_seed)

        # Per-file PROGRESSION protection: every record sharing an
        # instance_name with a progression-anchor record (Gold_Key,
        # Level_Column, Skeleton_Key) is part of a kill-group whose
        # membership the engine counts to spawn the key / finish the level /
        # open the gate. Type-swapping any such enemy breaks the trigger.
        progression_names = get_gold_key_protected_names(psx)

        # CHALLENGE MODE: HOLY GROUND SUPPRESSION. Runs for EVERY processed
        # level file regardless of which other toggles are on — see the two
        # `_apply_holy_ground` calls below (one in the all_enemies_mode branch,
        # which returns early, one on the normal path). Both go through
        # `all_repl` so the byte budget stays correct, and both run AFTER the
        # item/enemy passes so the destination choice can spend bytes those
        # passes freed.
        holy_ground_count = 0

        def _apply_holy_ground(_repl: dict) -> int:
            """Suppress this file's checkpoints into `_repl`. Returns count."""
            nonlocal holy_ground_total
            if not do_no_holy_ground:
                return 0
            hg_repl, _hg_delta, _hg_tpl = suppress_holy_ground(
                psx, existing_repl=_repl)
            for _off, _r in hg_repl.items():
                _repl[_off] = _r
            if not hg_repl and any(r.type_id == HOLY_GROUND_TYPE
                                   for r in psx.records):
                # No usable inert same-file template (absent, or too big for
                # the remaining byte budget). Leave this file's checkpoints
                # alone rather than guessing at a cross-file type.
                holy_ground_skipped.append(path.name)
                print(f"  [WARN] {path.name}: Holy_Ground kept — no inert "
                      f"same-file template fits")
            holy_ground_total += len(hg_repl)
            return len(hg_repl)

        # ============================================================
        # ALL-ENEMIES (chaos) MODE
        # ============================================================
        # Every eligible item / chest / structure / smashable becomes an
        # enemy. Gold_Keys are preserved. Enemies can drop Skeleton Keys and
        # the 1% Skeleton-Key / Gold-Key map lotteries still run so the game
        # stays beatable. Skips the normal enemy-swap + even-out passes
        # because the universal all-enemies pass already populates the map.
        if all_enemies_mode:
            enemy_pool = tuple(sorted(pool_for_world(world)))
            # extra_templates must contain BOTH the world enemy roster and
            # the Skeleton_Key / Gold_Key templates for the carve-outs.
            extra = dict(world_item_templates.get(world, {}))
            for tid, tpl in world_templates.get(world, {}).items():
                extra.setdefault(tid, tpl)

            item_seed = rng.randrange(1 << 30)
            item_repl = aggressive_item_randomize(
                psx, seed=item_seed,
                existing_repl=all_repl,
                extra_templates=extra,
                world=world,
                all_enemies=True,
                enemy_types=enemy_pool,
                all_enemies_weights=enemy_weight_table(world),
                excluded_types=excluded_types,
                weather_template=weather_template_for(world),
                randomize_gen_tier=getattr(args, "gen_tier", True),
                gate_mode=gate_mode,
                gate_templates=world_gate_templates.get(world),
                ability_weights=ability_place_weights,
            )
            for off, r in item_repl.items():
                all_repl[off] = r

            # Enemy post-processing on the freshly-created enemies:
            #   - Raven aggro boost
            #   - death-drop reroll (so enemies can drop Skeleton Keys etc.)
            #   - variant flags
            #   - force-clamp dangerous props (Axe iron-key, croc/sz wave idx)
            for off in list(all_repl.keys()):
                rec = all_repl[off]
                rec = _boost_raven_aggro(rec)
                all_repl[off] = rec
            drop_rng = random.Random(file_seed ^ 0xD150D250)
            for off in list(all_repl.keys()):
                rec = all_repl[off]
                new_rec = reroll_enemy_drop(rec, drop_rng)
                if new_rec is not rec:
                    all_repl[off] = new_rec
            variant_rng = random.Random(file_seed ^ 0xBADBA17)
            for off in list(all_repl.keys()):
                rec = all_repl[off]
                new_rec = reroll_enemy_variant(rec, variant_rng)
                if new_rec is not rec:
                    all_repl[off] = new_rec
            for off in list(all_repl.keys()):
                rec = all_repl[off]
                new_rec = force_enemy_props(rec)
                if new_rec is not rec:
                    all_repl[off] = new_rec

            # CHALLENGE MODE checkpoint suppression — chaos mode too. This
            # branch returns early, so without this call the option would
            # silently do nothing whenever --all-enemies is on.
            holy_ground_count = _apply_holy_ground(all_repl)

            # START-OF-LEVEL Gold_Key guarantee (chaos mode too).
            placed_key_offsets: set[int] = set()
            if path.name.upper() in {k.upper() for k in START_GOLD_KEY_FILES}:
                key_repl = place_start_gold_key(
                    psx, path.name, global_gold_key_template,
                    existing_repl=all_repl,
                )
                for off, r in key_repl.items():
                    all_repl[off] = r
                    placed_key_offsets.add(off)

            # EXTRA mid-level keys (chaos mode too) — keep gated levels beatable.
            if path.name.upper() in {k.upper() for k in EXTRA_KEY_FILES}:
                extra_repl = place_extra_keys(
                    psx, path.name, global_gold_key_template,
                    global_skeleton_key_template,
                    avoid_offsets=placed_key_offsets,
                )
                for off, r in extra_repl.items():
                    all_repl[off] = r
                    placed_key_offsets.add(off)

            # FIXED-POSITION Gold_Keys (chaos mode too) — e.g. The Siege catapult.
            if path.name.upper() in {k.upper() for k in FIXED_GOLD_KEY_TARGETS}:
                fixed_repl = place_fixed_gold_keys(
                    psx, path.name, global_gold_key_template,
                    avoid_offsets=placed_key_offsets,
                )
                for off, r in fixed_repl.items():
                    all_repl[off] = r

            out_path = out_folder / path.name
            exit_moved = apply_exit_location(psx, path.name, file_seed, all_repl)
            if exit_moved:
                print(f"  Level exit relocated in {path.name} ({exit_moved} column(s) swapped)")
            spawn_patch = spawn_patch_for(psx, path.name, file_seed)
            swap_patch = apply_swap_spawn_exit(psx, path.name, all_repl)
            if swap_patch:
                spawn_patch = swap_patch
                print(f"  Maximo start swapped with the level exit in {path.name}")
            gates_removed, gates_skipped = apply_remove_all_gates(
                psx, world, all_repl)
            if gates_removed or gates_skipped:
                print(f"  Gates removed in {path.name} "
                      f"({gates_removed} removed, {gates_skipped} skipped)")
            if not all_repl and not spawn_patch:
                print(f"  [SKIP] {path.name}: no swaps (world={world})")
                continue
            try:
                stats = psx.write_with_replacements(all_repl, out_path,
                                                    header_patches=spawn_patch)
                print(f"  [DONE] {path.name}: {len(all_repl)} -> enemies, "
                      f"world={world}, shift={stats['total_shift']}b"
                      f"{f', holy_ground-={holy_ground_count}' if holy_ground_count else ''}"
                      f"{', spawn moved' if spawn_patch else ''}")
                _report_records_growth(path.name, stats)
                summary["files_processed"].append({
                    "file": path.name, "world": world,
                    "changes": len(all_repl), "shift": stats["total_shift"],
                    "seed": file_seed, "mode": "all_enemies",
                    "spawn_moved": bool(spawn_patch),
                    "holy_ground_suppressed": holy_ground_count,
                })
            except Exception as e:
                print(f"  [FAIL] {path.name}: {e}")
                summary["errors"].append({"file": path.name, "error": str(e)})
            continue  # done with this file in all-enemies mode

        # 1. Enemy randomization (if enabled). Use the GLOBAL per-world
        # template registry so even maps with a thin native enemy roster
        # (Ghost+Raven+Torso only, etc.) can swap in any walking-melee
        # type the world supports.
        # (progression_names already computed above for this file.)
        if not no_enemies:
            pool_ids = pool_for_world(world)
            templates = world_templates.get(world, {})

            # SOURCE set: which records count as swappable enemies. Includes
            # types DISABLED in the spawn config so their vanilla instances get
            # retyped into an enabled enemy instead of remaining in the map.
            # DESTINATIONS come from `available` (templates) which already
            # excludes disabled types — so disabled enemies never spawn.
            melee_source = enemy_source_types_for_world(world)
            if spawn is not None:
                melee_source |= spawn.disabled_enemies(world)

            # Non-HUB Monster_Generator template for this world. cli.py already
            # guarantees this entry is a WORKING (non-HUB) generator via the
            # global_nonhub_gen fixup above, so it's safe to clone into a
            # regular in-level enemy slot.
            gen_template = world_item_templates.get(world, {}).get(
                MONSTER_GENERATOR_TYPE)
            enemies_to_gens = 0

            if templates:
                # RULE: a template EXISTING for a type is NOT permission either
                # — templates are harvested globally. Permission comes from the
                # guarded pool (pool_for_world -> permitted_types_for_world).
                # See the contaminated-GRAVE.BEF case (0x0D/0x16).
                available = [t for t in templates.keys() if t in pool_ids]
                # Equal-weight uniform pick. Each per-record pool excludes the
                # enemy's own type so every eligible enemy guaranteed swaps.
                for r in psx.records:
                    if r.type_id not in melee_source:
                        continue
                    if is_event_tied_enemy(r.instance_name):
                        continue
                    if r.instance_name in progression_names:
                        continue
                    pool_excl = [t for t in available if t != r.type_id]
                    if not pool_excl:
                        continue
                    new_type = rng.choice(pool_excl)
                    tpl = templates[new_type]
                    # SMALL chance the slot becomes a Monster_Generator instead
                    # of another enemy, so generators appear in the ENEMY pool
                    # too and not only via the item pool. A generator record
                    # (0x3C8) is bigger than every enemy (max 0x328), so this
                    # always GROWS the record. That is fine: the PSX file is
                    # allowed to grow, and nothing downstream compensates for
                    # the extra bytes.
                    if (gen_template is not None
                            and rng.random() < ENEMY_TO_GENERATOR_PROBABILITY):
                        tpl = gen_template
                        enemies_to_gens += 1
                    new_rec = make_record_with_template(tpl, r, r.offset)
                    all_repl[r.offset] = new_rec

            if enemies_to_gens:
                print(f"  {path.name}: {enemies_to_gens} enemy slot(s) -> "
                      f"Monster_Generator")

        # 2. Item randomization (if enabled). Pass the enemy swaps in so
        # tier-randomization (step 6 inside aggressive_item_randomize) can
        # re-roll the tier of POST-swap enemies (e.g. a Ghost->Basic_Skeleton
        # gets a fresh tier 0/1/2 instead of being stuck at the template's
        # tier).
        if any_item_pipeline:
            item_seed = rng.randrange(1 << 30)
            # Per-world destination exclusions: global excludes (e.g.
            # Dark_Knight) plus any enemies disabled in the spawn config.
            world_excluded = set(excluded_types)
            # ONE table drives retyping now, saved config or not: `unified_cfg`
            # is the loaded config when there is one and the STOCK version-4
            # table otherwise, so a config-less build still weights the routes
            # this run committed instead of ignoring them.
            uni_weights = unified_cfg.native_weights(
                world,
                committed_routes=committed_routes_by_world.get(world, ()))
            tier_weights = None
            if spawn is not None:
                world_excluded |= spawn.disabled_enemies(world)
                # THE ARRIVAL WEIGHT REACHES THE POOL HERE. Without the second
                # argument a ported structure competed on its generic
                # `structures` weight (0.6-2.1 on the config scale, ~0.4% of
                # retyped slots) and the editor's Weight column changed nothing.
                # The set is empty for every world with no committed arrival and
                # on every build with cross-world structures off, and
                # `universal_weights` then returns exactly what it always did.
                # THE UNIFIED NATIVE PROJECTION IS SET ABOVE, for every build.
                # `items.randomize_universal` is UNCHANGED -- it takes the same
                # `{type_id: weight}` shape it always did; what changed is that
                # the shape now comes from the ONE table, with the
                # `enemy_share_target` renormalization applied inside
                # `native_weights` and nowhere else.
                #
                # PROPERTY 5, mechanically: admission for every world finished
                # before this loop started, and the pass's COMMITTED SET is the
                # only thing crossing over. The retyping stream also derives with
                # `phase="native"`, so it is not a continuation of the admission
                # stream -- retyping observes admission's RESULT and never how
                # many draws it consumed.
                tier_weights = spawn.enemy_tier_weights_for_world(world)
            # Exclude enemy types not defined in this world's BEF.
            # Without this, seeds that roll a world-specific enemy (e.g.
            # Goat_Devil 0x13 in a Grave-world file) cause the engine to
            # look up a null mesh pointer and crash — the intermittent
            # "crash when loading a level" bug. This covers BOTH enemy types
            # AND world-specific props (smashables, lanterns, statues, etc.)
            # in UNIVERSAL_ITEM_POOL — all are BEF-managed and will crash if
            # placed in a file whose BEF doesn't define them. bef_native_types
            # is empty for a world only if its BEF failed to parse; skip the
            # filter in that case so the tool doesn't crash itself.
            # BEF declaration alone is NOT permission: a type the BEF declares
            # but that was never ported into this world (e.g. 0x0D/0x16 left
            # over in GRAVE.BEF) carries a mesh index positional into another
            # world's PRS -> invisible or wrong-mesh enemies. Permission is the
            # intersection of BEF-declared and catalog-safe.
            permitted = permitted_types_for_world(world)
            if permitted is not None:
                world_excluded |= ALL_POOL_BEF_TYPES - permitted
            item_repl = aggressive_item_randomize(
                psx, seed=item_seed,
                existing_repl=all_repl,
                extra_templates=world_item_templates.get(world, {}),
                world=world,
                world_gen_templates=world_gen_templates.get(world),
                excluded_types=world_excluded,
                do_items=do_items,
                do_chests=do_chests,
                do_skills=do_skills,
                do_columns=do_columns,
                universal_weights=uni_weights,
                tier_weights=tier_weights,
                weather_template=weather_template_for(world),
                chest_special_chances=(forced_chest_special if forced_chest_special
                                       is not None else
                                       (spawn.chest_special_chances()
                                        if spawn is not None else None)),
                lift_protection=(path.name.upper() in LIFT_PROTECTION_FILES),
                randomize_gen_tier=getattr(args, "gen_tier", True),
                gate_mode=gate_mode,
                gate_templates=world_gate_templates.get(world),
                preserve_chests=preserve_chests,
                preserve_iron_keys=preserve_iron_keys,
                ability_weights=ability_place_weights,
            )
            for off, r in item_repl.items():
                all_repl[off] = r

        # 3. EVEN-OUT pass: guarantee a perfectly even distribution across
        # every walking-melee enemy in the world's pool.
        #
        # Both step 1 (cli.py uniform enemy swap) and step 2 (items rolling
        # into enemies via the universal pool) introduce statistical bias —
        # the two passes draw from different pools with different weights.
        # This final pass collects every
        # walking-melee record that ended up in the file (vanilla survivors
        # + items that became enemies) and reassigns their type using a
        # shuffled balanced bag so each pool member appears the same number
        # of times (off by one when the count doesn't divide evenly).
        # Event-tied enemies are excluded from rebalancing.
        if not no_enemies:
            pool_ids = pool_for_world(world)
            templates = world_templates.get(world, {})
            if templates:
                # RULE: BEF-declared / template-exists is NOT permission;
                # permission comes from the guard (pool_ids). Using raw
                # template keys here placed guard-excluded types such as the
                # leftover 0x0D/0x16 in GRAVE.BEF (contaminated asset file).
                pool_list = sorted(t for t in templates.keys()
                                   if t in pool_ids)  # deterministic
                # Final state of every record in the file (post-step-1+2)
                final_state: dict[int, "PsxRecord"] = {
                    r.offset: all_repl.get(r.offset, r) for r in psx.records
                }
                # Plus any newly-created enemy records that came in via items
                for off, rec in all_repl.items():
                    if off not in final_state:
                        final_state[off] = rec

                # Eligible enemy slots — ones we're allowed to retype.
                eligible: list["PsxRecord"] = []
                for off, rec in final_state.items():
                    # DELIBERATE: Monster_Generator (0x5F) is not in pool_ids,
                    # so generators created in step 1 (the small enemy->
                    # generator chance) and by the item pass are skipped here.
                    # Don't widen this test — retyping them back into enemies
                    # would undo those generators.
                    if rec.type_id not in pool_ids:
                        continue
                    if is_event_tied_enemy(rec.instance_name):
                        continue
                    if rec.instance_name in progression_names:
                        continue
                    eligible.append(rec)

                if eligible and len(pool_list) > 1:
                    # Build a WEIGHTED balanced bag.
                    # Per-type weights come from the spawn config (when the
                    # user supplied one) or the stock WALKING_MELEE_WEIGHTS —
                    # certain types (Raven/Ghost/Torso/Guard) default to a
                    # reduced weight so they appear less often. Slots are
                    # allocated proportionally and rounded with leftover slots
                    # going to the highest-weight types.
                    ewt = enemy_weight_table(world)
                    N = len(eligible)
                    weights = [ewt.get(t, 1.0)
                               for t in pool_list]
                    total_w = sum(weights) or 1.0
                    raw = [N * w / total_w for w in weights]
                    counts = [int(x) for x in raw]
                    leftover = N - sum(counts)
                    # Distribute the leftover seats by largest fractional part
                    # (standard "Hamilton" allocation).
                    if leftover > 0:
                        fracs = sorted(
                            range(len(pool_list)),
                            key=lambda i: (raw[i] - counts[i], weights[i]),
                            reverse=True,
                        )
                        for i in fracs[:leftover]:
                            counts[i] += 1
                    bag: list[int] = []
                    for tid, c in zip(pool_list, counts):
                        bag.extend([tid] * c)
                    rng.shuffle(bag)

                    # Pair each eligible record with a target type from the
                    # bag and reassign. Skip only if the record already has
                    # the rolled type (a no-op) — the file may grow, so every
                    # bag assignment is taken as rolled.
                    for rec, new_type in zip(eligible, bag):
                        if rec.type_id == new_type:
                            continue
                        tpl = templates[new_type]
                        new_rec = make_record_with_template(
                            tpl, rec, rec.offset
                        )
                        # The template's tier (prop[0]) is whatever the
                        # template happened to have — usually 0. Roll a fresh
                        # tier so step-3 reassignments don't all stick on T0.
                        new_rec = reroll_enemy_tier(
                            new_rec, rng, world=world,
                            tier_weights_per_type=enemy_tier_table(world))
                        all_repl[rec.offset] = new_rec

            # 4. Boost aggro range on every Raven in the final state.
            # Vanilla Ravens have a tiny 15-38 unit sight zone that makes
            # them feel passive — they only react when the player is
            # right under them. Bump every Raven's trigger volume to a
            # normal melee-style detection range so they actually engage.
            for r in psx.records:
                cur = all_repl.get(r.offset, r)
                if cur.type_id == 0x37:
                    new_rec = _boost_raven_aggro(cur)
                    if new_rec is not cur:
                        all_repl[r.offset] = new_rec
            # Also handle Ravens at offsets that didn't exist in vanilla
            # (e.g. created by the universal item randomizer at coin slots).
            for off, rec in list(all_repl.items()):
                if rec.type_id == 0x37:
                    new_rec = _boost_raven_aggro(rec)
                    if new_rec is not rec:
                        all_repl[off] = new_rec

            # 5. Roll a fresh death-drop kind on every walking-melee enemy.
            # Without this, every enemy of a given type inherits the
            # template's prop[5] value (so they all drop the same thing —
            # e.g. every Axe_Skeleton spawns a Skeleton_Key on death because
            # the chosen template happened to come from a HUB record where
            # prop[5] was 1). The drop randomizer rolls 0/1/2/3/4 from the
            # type's VANILLA-WEIGHTED pool (mirrors per-type frequency) so
            # most enemies still drop nothing but ~25% drop a key/ability/
            # health/gold and Iron Keys (val=1 on Axe_Skeleton) stay rare.
            drop_rng = random.Random(file_seed ^ 0xD150D250)
            for off in list(all_repl.keys()):
                rec = all_repl[off]
                new_rec = reroll_enemy_drop(rec, drop_rng)
                if new_rec is not rec:
                    all_repl[off] = new_rec
            # And any vanilla survivors not in all_repl yet.
            for r in psx.records:
                if r.offset in all_repl:
                    continue
                new_rec = reroll_enemy_drop(r, drop_rng)
                if new_rec is not r:
                    all_repl[r.offset] = new_rec

            # 6. Roll a fresh BOOLEAN VARIANT FLAG on every enemy that has
            # one — Ghost (Poltergeist vs Blue Ghost), Basic_Zombie (normal
            # vs splittable), Basic_Skeleton (variant flag). cli.py picks
            # ONE template per (world, type), so without this every enemy
            # of that type inherits the template's variant — e.g. Grave is
            # 100% Poltergeist (no Blue Ghost) and Under is 100% non-
            # splittable Zombie (the corpse never spawns a Torso_Zombie).
            # Using vanilla-derived weights restores the natural mix in
            # every world.
            variant_rng = random.Random(file_seed ^ 0xBADBA17)
            for off in list(all_repl.keys()):
                rec = all_repl[off]
                new_rec = reroll_enemy_variant(rec, variant_rng)
                if new_rec is not rec:
                    all_repl[off] = new_rec
            for r in psx.records:
                if r.offset in all_repl:
                    continue
                new_rec = reroll_enemy_variant(r, variant_rng)
                if new_rec is not r:
                    all_repl[r.offset] = new_rec

            # 7. FINAL NORMALIZATION (enemy-block portion): hard-clamp
            # known-bad props on every enemy in the final state. The
            # cli.py template-selection filter tries to pick "neutral"
            # templates (p5=0 etc.) but if the world only has HUB-style
            # records for a given type, the filter falls back to a HUB
            # record and every cloned enemy inherits the HUB phase value.
            # Most painful symptom: every Axe_Skeleton drops an Iron Key
            # on death because HUB Axe_Skeletons all have prop[5]=1 which
            # the engine reads as "drop Skeleton_Key". `force_enemy_props`
            # runs unconditionally on every record after everything else
            # and clamps these dangerous slots back to 0.
            for off in list(all_repl.keys()):
                rec = all_repl[off]
                new_rec = force_enemy_props(rec)
                if new_rec is not rec:
                    all_repl[off] = new_rec
            for r in psx.records:
                if r.offset in all_repl:
                    continue
                new_rec = force_enemy_props(r)
                if new_rec is not r:
                    all_repl[r.offset] = new_rec

        # FINAL NORMALIZATION (always runs, even when --no-enemies). Items
        # mode can still produce enemies via item-to-enemy rolls in the
        # universal pool, and those need force_enemy_props too. This second
        # pass is idempotent — rerunning on already-clamped records is a
        # no-op.
        for off in list(all_repl.keys()):
            rec = all_repl[off]
            new_rec = force_enemy_props(rec)
            if new_rec is not rec:
                all_repl[off] = new_rec
        for r in psx.records:
            if r.offset in all_repl:
                continue
            new_rec = force_enemy_props(r)
            if new_rec is not r:
                all_repl[r.offset] = new_rec

        # CHALLENGE MODE checkpoint suppression (normal path). Sits with the
        # other unconditional per-file passes, right after the final
        # force_enemy_props normalization, so it is independent of the enemy /
        # item / chest / skill / column toggles.
        holy_ground_count = _apply_holy_ground(all_repl)

        # START-OF-LEVEL Gold_Key guarantee. The four named levels in
        # START_GOLD_KEY_FILES must always have a collectable Gold_Key
        # right at the player's spawn. This OVERRIDES whatever the nearest
        # filler-item slot rolled into, so it runs last.
        placed_key_offsets: set[int] = set()
        if path.name.upper() in {k.upper() for k in START_GOLD_KEY_FILES}:
            key_repl = place_start_gold_key(
                psx, path.name, global_gold_key_template,
                existing_repl=all_repl,
            )
            for off, r in key_repl.items():
                all_repl[off] = r
                placed_key_offsets.add(off)

        # EXTRA mid-level Gold_Keys + extra Skeleton_Keys for gated levels
        # (e.g. Dungeon of Despair). Must be able to OVERRIDE universal swap
        # slots (the universal pass has usually already consumed every filler
        # pickup), so we only avoid the start-key slot — NOT every swap.
        if path.name.upper() in {k.upper() for k in EXTRA_KEY_FILES}:
            extra_repl = place_extra_keys(
                psx, path.name, global_gold_key_template,
                global_skeleton_key_template,
                avoid_offsets=placed_key_offsets,
            )
            for off, r in extra_repl.items():
                all_repl[off] = r
                placed_key_offsets.add(off)
                placed_key_offsets.add(off)

        # FIXED-POSITION Gold_Keys at specific gates that would otherwise
        # soft-lock (e.g. The Siege's catapult gate). This must be able to
        # OVERRIDE a universal swap slot (every nearby filler pickup has
        # usually already been swapped into an enemy/item), so we only avoid
        # the slots we used for the start / extra keys above.
        if path.name.upper() in {k.upper() for k in FIXED_GOLD_KEY_TARGETS}:
            fixed_repl = place_fixed_gold_keys(
                psx, path.name, global_gold_key_template,
                avoid_offsets=placed_key_offsets,
            )
            for off, r in fixed_repl.items():
                all_repl[off] = r

        out_path = out_folder / path.name
        exit_moved = apply_exit_location(psx, path.name, file_seed, all_repl)
        if exit_moved:
            print(f"  Level exit relocated in {path.name} ({exit_moved} column(s) swapped)")
        spawn_patch = spawn_patch_for(psx, path.name, file_seed)
        swap_patch = apply_swap_spawn_exit(psx, path.name, all_repl)
        if swap_patch:
            spawn_patch = swap_patch
            print(f"  Maximo start swapped with the level exit in {path.name}")
        gates_removed, gates_skipped = apply_remove_all_gates(
            psx, world, all_repl)
        if gates_removed or gates_skipped:
            print(f"  Gates removed in {path.name} "
                  f"({gates_removed} removed, {gates_skipped} skipped)")
        if not all_repl and not spawn_patch:
            print(f"  [SKIP] {path.name}: no swaps (world={world}, not written)")
            continue

        try:
            stats = psx.write_with_replacements(all_repl, out_path,
                                                header_patches=spawn_patch)
            print(f"  [DONE] {path.name}: {len(all_repl)} changes, world={world}, "
                  f"shift={stats['total_shift']}b"
                  f"{f', holy_ground-={holy_ground_count}' if holy_ground_count else ''}"
                  f"{', spawn moved' if spawn_patch else ''}")
            _report_records_growth(path.name, stats)
            summary["files_processed"].append({
                "file": path.name,
                "world": world,
                "changes": len(all_repl),
                "shift": stats["total_shift"],
                "seed": file_seed,
                "spawn_moved": bool(spawn_patch),
                "holy_ground_suppressed": holy_ground_count,
            })
        except Exception as e:
            print(f"  [FAIL] {path.name}: {e} — writing vanilla fallback")
            summary["errors"].append({"file": path.name, "error": str(e)})
            # CRITICAL: always write a vanilla copy to out_path so
            # write_patched_assets doesn't write a stale/partial file
            # from a previous run into the ISO, corrupting that sector.
            import shutil as _shutil
            _shutil.copy2(path, out_path)

    # BOSS INJECTION (EXPERIMENTAL, opt-in). Grave levels only, and never a
    # BOSS_FILES arena — those stay exactly as they are, and --duplicate-bosses
    # is untouched. A refusal logs one [cross-world] line and the build
    # continues unaffected. Injection runs on the ALREADY-EMITTED file in
    # out_folder when one exists, so it composes with the normal passes instead
    # of reverting them.
    if getattr(args, "inject_boss", False):
        _bi_bef_written = False
        for path in psx_files:
            upper = path.name.upper()
            if upper in BOSS_FILES or not upper.startswith("G_"):
                continue
            target = out_folder / path.name
            if not target.exists():
                target = path
            try:
                rep = boss_inject.inject_boss(
                    game_files_dir=in_folder, target_psx=target, enabled=True)
            except Exception as e:               # never take the build down
                print(f"{boss_inject.LOG_PREFIX} boss injection errored on "
                      f"{path.name}: {e}")
                continue
            if not rep.ok:
                continue
            _le = rep.port.level_exit
            if _le is not None and _le.applied:
                print(f"{boss_inject.LOG_PREFIX} {path.name}: boss script "
                      f"PATCHED, {len(_le.spans)} span(s) "
                      f"(NEUTRALIZE_LEVEL_EXIT="
                      f"{boss_inject.NEUTRALIZE_LEVEL_EXIT}, "
                      f"STOP_CUTSCENE_ON_DEATH="
                      f"{boss_inject.STOP_CUTSCENE_ON_DEATH}, "
                      f"REMOVE_BOSS_CAMERA_FLAG="
                      f"{boss_inject.REMOVE_BOSS_CAMERA_FLAG}); instructions "
                      f"{_le.instructions_before} -> {_le.instructions_after} "
                      f"(derived expectation "
                      f"{_le.instructions_expected_after}); callapi sites "
                      f"{_le.callapi_sites_before} -> "
                      f"{_le.callapi_sites_after}; per-api sites "
                      + ", ".join(f"0x{a:02X} {b}->{c}"
                                  for a, b, c in _le.api_site_counts)
                      + ". STRUCTURAL ONLY: the level-exit removal is confirmed "
                        "in game, the freeze edits are not.")
                for _sp in _le.spans:
                    print(f"{boss_inject.LOG_PREFIX} {path.name}:   span "
                          f"{_sp.name!r} @Script_Section 0x{_sp.offset:X}: "
                          f"{_sp.original.hex(' ')} -> {_sp.fill.hex(' ')}; "
                          f"REMOVED {_sp.removed}; ADDED {_sp.added}")
            elif _le is not None:
                print(f"{boss_inject.LOG_PREFIX} {path.name}: boss script patch "
                      f"OFF (NEUTRALIZE_LEVEL_EXIT="
                      f"{boss_inject.NEUTRALIZE_LEVEL_EXIT}, "
                      f"REMOVE_BOSS_CAMERA_FLAG="
                      f"{boss_inject.REMOVE_BOSS_CAMERA_FLAG}); the boss script "
                      f"ships VERBATIM and Early_Exit_Risk is un-mitigated")
            (out_folder / path.name).write_bytes(rep.placement.data)
            if not _bi_bef_written:
                (out_folder / "GRAVE.BEF").write_bytes(rep.bef.data)
                # THE PORTED ARCHIVES. Without these two the merged blob's
                # rewritten mesh/anim indices point past the end of the vanilla
                # tables and the level will not load -- the .BEF and the .PRS /
                # .PRT are one unit and are written together or not at all.
                (out_folder / "GRAVE.PRS").write_bytes(rep.port.prs)
                (out_folder / "GRAVE.PRT").write_bytes(rep.port.prt)
                if rep.sound is not None and rep.sound.data:
                    (out_folder / "GRAVE.BD").write_bytes(rep.sound.data)
                _bi_bef_written = True
            summary["files_processed"].append({
                "file": path.name, "world": "grave",
                "mode": "boss_injection",
                "route_status": rep.route_status,
                "bef_size_delta": rep.bef.size_delta,
                "host_slot": rep.placement.chosen_instance_name,
                "sound_silent": bool(rep.sound and rep.sound.silent),
                "port_mesh_bytes": rep.port.mesh_bytes,
                "port_anim_bytes": rep.port.anim_bytes,
                "port_tex_bytes": rep.port.tex_bytes,
                "port_total_bytes": rep.port.port_total,
                "mesh_index_map": list(rep.port.mesh_index_map),
                "anim_index_map": list(rep.port.anim_index_map),
                "tex_remap": list(rep.port.tex_remap),
                "resident_total": rep.port.resident_total,
                "pool_budget": rep.port.budget,
                "port_mesh_entry_count": rep.port.mesh_entry_count,
                "port_anim_record_count": rep.port.anim_record_count,
                "source_mesh_union": list(rep.port.cat1_refs),
                "source_anim_union": list(rep.port.cat2_refs),
                "clut_count": rep.port.clut_count,
                "clut_cap": rep.port.clut_cap,
                # THE BOSS SCRIPT SPANS. Reported in full, per span, so every
                # measured script edit is in the summary rather than only in
                # the log. The level-exit removal is confirmed in game; the
                # CutScene_Stop call and the CameraSetBossFlag removal are
                # SUSPICION-driven and structural only.
                "boss_script_patch": (
                    {"switches": {
                         "NEUTRALIZE_LEVEL_EXIT":
                             bool(boss_inject.NEUTRALIZE_LEVEL_EXIT),
                         "STOP_CUTSCENE_ON_DEATH":
                             bool(boss_inject.STOP_CUTSCENE_ON_DEATH),
                         "REMOVE_BOSS_CAMERA_FLAG":
                             bool(boss_inject.REMOVE_BOSS_CAMERA_FLAG)},
                     "applied": bool(rep.port.level_exit.applied),
                     "spans": [
                         {"name": s.name,
                          "script_offset": s.offset,
                          "original_bytes": s.original.hex(" "),
                          "fill_bytes": s.fill.hex(" "),
                          "removed": s.removed,
                          "added": s.added}
                         for s in rep.port.level_exit.spans],
                     "instructions_before":
                         rep.port.level_exit.instructions_before,
                     "instructions_after":
                         rep.port.level_exit.instructions_after,
                     "instructions_expected_after":
                         rep.port.level_exit.instructions_expected_after,
                     "callapi_sites_before":
                         rep.port.level_exit.callapi_sites_before,
                     "callapi_sites_after":
                         rep.port.level_exit.callapi_sites_after,
                     "api_site_counts": [
                         {"api_index": a, "before": b, "after": c}
                         for a, b, c in rep.port.level_exit.api_site_counts],
                     "api_113_sites_remaining":
                         rep.port.level_exit.api_index_sites_after,
                     "verified": "level exit CONFIRMED in game; the freeze "
                                 "edits are STRUCTURAL ONLY -- not observed "
                                 "in game"}
                    if rep.port.level_exit is not None else None),
                "injected_members": [
                    {"type_id": m.type_id,
                     "source_mesh_refs": list(m.source_mesh_refs),
                     "new_mesh_refs": list(m.new_mesh_refs),
                     "source_anim_refs": list(m.source_anim_refs),
                     "new_anim_refs": list(m.new_anim_refs),
                     "anim_bindings": [list(b) for b in m.anim_bindings],
                     "identity_check": bool(m.identity_ok)}
                    for m in rep.port.members],
                "anims_shipped_unchanged": [
                    {"type_id": t, "index": i, "checked_against_mesh": mi,
                     "missing_names": list(n),
                     "reason": "SCOPED vanilla parity: the rig is a mesh of "
                               "THAT member, ported byte-for-byte, and the "
                               "same name is missing from that same mesh in "
                               "the SOURCE archive, so the lookup fails "
                               "identically in vanilla; the blob carries a "
                               "reference TO this record, so dropping it "
                               "would leave a dangling reference"}
                    for t, i, mi, n in rep.port.tolerated_anims],
            })
            break                                # one injected boss per build

    # SLUS (ELF) patches. The executable is patched and written to out_folder
    # when EITHER:
    #   - columns are randomized (level-title '???' patch, world-aware), OR
    #   - skills are randomized (ability/skill DROP pool patch — lets every
    #     world drop every sword enchantment AND every skill, not just its
    #     vanilla ones).
    # Both patches are applied to ONE bytearray and written once.
    do_sword_drops = do_skills
    damage_taken = getattr(args, "damage_taken", None) or "normal"
    damage_dealt = getattr(args, "damage_dealt", None) or "normal"
    if harder_mode:
        # Harder mode forces Maximo to take 2x and deal 0.5x damage.
        damage_taken = "2x"
        damage_dealt = "0.50"
    # Starting inventory (new-game GameVarTbl). None = leave vanilla.
    start_gold = getattr(args, "start_gold", None)
    start_lives = getattr(args, "start_lives", None)
    start_keys = getattr(args, "start_keys", None)
    start_deathcoins = getattr(args, "start_deathcoins", None)
    sword_enchant = getattr(args, "sword_enchant", None)
    elemental_shield = getattr(args, "elemental_shield", None)
    start_souls = getattr(args, "start_souls", None)
    start_armor = getattr(args, "start_armor", None)
    start_shields = getattr(args, "start_shields", None)
    start_boxers = getattr(args, "start_boxers", None)
    # Starting skills: accept a comma-string (CLI) or an iterable (GUI/ISO).
    _ss_raw = getattr(args, "start_skills", None)
    if isinstance(_ss_raw, str):
        start_skills = {s.strip() for s in _ss_raw.split(",") if s.strip()}
    else:
        start_skills = set(_ss_raw or ())
    if getattr(args, "randomize_start_inv", False):
        # Roll seeded starting values (overrides any manual entries). Uses the
        # run seed so the same seed reproduces the same loadout.
        rng = random.Random((seed if seed is not None else 0) ^ 0x57A47125)
        start_gold = rng.choice([0, 0, 50, 100, 200, 350, 500, 750, 1000])
        start_lives = rng.randint(1, 9)
        start_keys = rng.randint(0, 3)
        start_deathcoins = rng.randint(1, 5)
        sword_enchant = rng.randint(0, 4)
        elemental_shield = rng.randint(0, 3)
        start_skills = {s for s in STARTING_SKILL_MASKS if rng.random() < 0.5}
        print(f"Starting inventory randomized: gold={start_gold} "
              f"lives={start_lives} keys={start_keys} deathcoins={start_deathcoins} "
              f"sword_enchant={sword_enchant} elemental_shield={elemental_shield} "
              f"skills={sorted(start_skills) or 'none'}")
    do_start_inv = any(v is not None for v in
                       (start_gold, start_lives, start_keys, start_deathcoins,
                        sword_enchant, elemental_shield,
                        start_souls, start_armor, start_shields,
                        start_boxers))
    do_start_skills = bool(start_skills)
    do_rand_levels = bool(getattr(args, "randomize_levels", False))
    # Shield cost (GameVar #10 decrement). None = leave vanilla. Harder Mode
    # deliberately does NOT override this: it is a curated profile that tunes
    # damage_taken/damage_dealt only, and the shield economy is left to the
    # user's explicit choice. Do not "fix" this by wiring it above.
    # The flag carries a preset LABEL; resolve it to the integer the patcher
    # takes at this edge, so the wire format below stays an int.
    shield_cost_label = getattr(args, "shield_cost", None)
    shield_cost = resolve_shield_cost(shield_cost_label)
    do_shield_cost = (shield_cost is not None
                      and shield_cost != SHIELD_COST_VANILLA)
    # Lives spent per death (inline decrement of GameVar #4). The flag carries a
    # preset LABEL; resolve it to the integer the patcher takes at this edge, so
    # the wire format below stays an int. None or 1 leaves the executable
    # untouched by this feature. Harder Mode deliberately does NOT touch life
    # cost — the omission is intentional, same reasoning as shield cost above:
    # Harder Mode tunes damage only.
    life_cost_label = getattr(args, "life_cost", None)
    life_cost = resolve_life_cost(life_cost_label)
    do_life_cost = life_cost is not None and life_cost != LIFE_COST_VANILLA
    # Perma death (GameVarTbl #4/#8 zeroed + capped, SetDeath game-over test,
    # ContinuePlayer life restore). Harder Mode deliberately does NOT enable it —
    # the omission is intentional, same reasoning as shield/life cost above:
    # Harder Mode tunes damage only, and perma-death is a run-defining choice.
    perma_death = bool(getattr(args, "perma_death", False))
    # Sword enchantment point cost (GameVarSub index 11). The flag carries a
    # preset LABEL naming a MULTIPLIER on each move's vanilla cost; resolve it to
    # the integer the patcher takes at this edge. Harder Mode deliberately does
    # NOT override it, same reasoning as shield cost above.
    sword_cost_label = getattr(args, "sword_cost", None)
    sword_cost = resolve_sword_cost(sword_cost_label)
    do_sword_cost = (sword_cost is not None
                     and sword_cost != SWORD_COST_VANILLA_MULT)
    # Status-effect durations: three INDEPENDENT sites, each a preset LABEL
    # naming a MULTIPLIER on that site's vanilla timer. Resolved to a number at
    # this edge. Harder Mode deliberately does NOT touch these — the omission is
    # intentional, same reasoning as shield/sword cost above: Harder Mode tunes
    # damage only.
    powerup_duration_label = getattr(args, "powerup_duration", None)
    wizard_duration_label = getattr(args, "wizard_duration", None)
    frozen_duration_label = getattr(args, "frozen_duration", None)
    # The site id matters: "easier"/"harder" are INVERTED between the harmful
    # wizard/frozen timers and the beneficial power-up timer.
    powerup_duration = resolve_effect_duration("powerup", powerup_duration_label)
    wizard_duration = resolve_effect_duration("wizard", wizard_duration_label)
    frozen_duration = resolve_effect_duration("frozen", frozen_duration_label)
    effect_duration_labels = {"powerup": powerup_duration_label,
                              "wizard": wizard_duration_label,
                              "frozen": frozen_duration_label}
    do_effect_durations = any(
        v is not None and v != EFFECT_DURATION_VANILLA_MULT
        for v in (powerup_duration, wizard_duration, frozen_duration))
    # Koin fee: ONE integer driving BOTH the save price and the hub-travel price,
    # because the on-screen label is a single shared string. None or 100
    # ("vanilla") leaves the executable untouched by this feature. Harder Mode
    # deliberately does NOT touch it — the omission is intentional, same
    # reasoning as shield/life/sword cost above: Harder Mode tunes damage only.
    koin_fee = getattr(args, "koin_fee", None)
    do_koin_fee = koin_fee is not None and koin_fee != KOIN_FEE_VANILLA
    # Disable world collision: the developers' own one-byte `bDoCollision`
    # switch. A DEVELOPER RESEARCH SWITCH, not a playable mode — the player falls
    # through the map. Harder Mode deliberately does NOT touch it — the omission
    # is intentional, same reasoning as shield/life/sword cost above: Harder Mode
    # tunes damage only, and this one is not a difficulty setting at all.
    do_disable_collision = bool(getattr(args, "disable_collision", False))
    # Level time limit (EXPERIMENTAL): one of three difficulty presets, each
    # carrying PER-WORLD durations, after which Maximo is killed through the
    # game's own death path. The clock re-arms on level entry AND on respawn, so
    # the limit is per attempt, and it arms in hubs, intros and boss arenas too.
    # Harder Mode deliberately does NOT touch it — the omission is intentional:
    # Harder Mode tunes damage, and a hard time limit is a different kind of
    # challenge entirely.
    level_timer = resolve_level_timer(getattr(args, "level_timer", None))
    do_level_timer = level_timer is not None
    # Companion toggle: the on-screen countdown (default on). `--no-timer-display`
    # turns the display off while keeping the death, so it only matters when the
    # level timer itself is enabled.
    timer_display = bool(getattr(args, "timer_display",
                                 not getattr(args, "no_timer_display", False)))
    # Widescreen (16:9): a static port of the PCSX2 widescreen cheat — two
    # float constants in the executable (horizontal scale and the render aspect
    # inside gViewFrustum). Presentation only, so Harder Mode never touches it.
    do_widescreen = bool(getattr(args, "widescreen", False))
    # On-screen object budget (EXPERIMENTAL): raise the script VM's per-type
    # load-class WEIGHT limit so more of a level's own objects/enemies can be
    # active at once. Two sites: the constructor default (classes 1..5) and the
    # per-level enemy override in the LevelTable, which mglRunLevel re-applies to
    # class 3 at every level load. Harder Mode deliberately does NOT touch it —
    # more simultaneous objects is a performance/density setting, not a damage one.
    object_budget = resolve_object_budget(getattr(args, "object_budget", None))
    do_object_budget = (object_budget is not None
                        and object_budget != OBJECT_BUDGET_VANILLA)
    # RESOURCE memory pool: raise the engine's pool[2] (and pool[0]'s matching
    # reserve, which is the same edit — see the section comment in elf_patch).
    #
    # CROSS-WORLD WIRING: cross-world porting grows each target world's PRS/PRT
    # with foreign meshes, animations and textures, and that data lands in the
    # RESOURCE pool, so --cross-world OPTS IN to the raised preset by default.
    # It is a default, not a force: an explicit --resource-pool (including
    # 'vanilla') always wins, so the user can still turn it off. Chosen over
    # doing it inside cross_world.py because cross_world.py only ever touches
    # assets — every executable edit lives here, behind the `ELF patch:` prefix.
    # `resource_pool`, `resource_pool_from_cross_world` and `do_resource_pool`
    # are RESOLVED EARLIER, just above the cross-world block, and reused here
    # unchanged. They were moved because the seeded enemy draw needs to know
    # whether the raised pool is actually in effect BEFORE it picks a lineup:
    # its budget is an occupancy of the pool that will ship, and assuming a
    # patch the build never applied is exactly Requirement 6.5's failure. One
    # decision, one place, read twice.
    # User-set ability drop weights (computed above, next to the spawn config).
    # These must be applied even when the blanket ability randomization is OFF,
    # so they open the ELF-patch gate.
    if (do_columns or do_sword_drops or abil_overrides or damage_taken != "normal"
            or damage_dealt != "normal" or do_start_inv or do_start_skills
            or do_rand_levels or do_shield_cost or do_life_cost
            or do_sword_cost or do_effect_durations or do_koin_fee
            or do_disable_collision or do_level_timer
            or do_widescreen or do_object_budget or do_resource_pool
            or perma_death):
        # Region-aware: patch whichever boot executable the disc shipped
        # (US SLUS_200.17 / JP SLPM_621.27 / ...). All patches below are located
        # by signature or anchor, so they apply to any region. The output keeps
        # the SAME filename so the ISO re-injection map matches.
        elf_src = find_executable(in_folder)
        if elf_src is not None:
            elf_out = out_folder / elf_src.name
            print(f"\nELF patch: target executable {elf_src.name}")
            try:
                data = bytearray(elf_src.read_bytes())
                if do_columns:
                    tres = apply_level_titles(data, worlds=selected_worlds)
                    print(f"\nELF patch: level titles ({len(tres['patched'])} slots)")
                    bad = [s for s in tres["skipped"] if "actual" in s]
                    if bad:
                        print(f"  WARNING: skipped {len(bad)} title slot(s) "
                              f"(text mismatch — wrong ELF version?)")
                if do_sword_drops or abil_overrides:
                    sres = patch_ability_drops(
                        data, seed if do_sword_drops else None,
                        overrides=abil_overrides)
                    if sres["found"]:
                        if do_sword_drops:
                            print(f"ELF patch: ability/skill drops randomized across "
                                  f"{sres['tables']} levels ({sres['abilities']} "
                                  f"abilities) — any world can drop any sword "
                                  f"enchantment and skill")
                        applied = sres.get("overrides") or {}
                        if applied:
                            names = ", ".join(
                                f"{ABILITY_DROP_NAMES.get(i, f'id {i}')}={applied[i]}"
                                for i in sorted(applied))
                            print(f"ELF patch: ability drop weights set for "
                                  f"{len(applied)} ability(ies) across "
                                  f"{sres['tables']} levels — {names}")
                        zg = sres.get("zero_guard") or []
                        if zg:
                            tbls = ", ".join(str(e["table"]) for e in zg)
                            print(f"ELF patch: WARNING ability drop table(s) "
                                  f"{tbls} would have been left all-zero "
                                  f"(unverified engine behaviour) — restored "
                                  f"the highest vanilla weight in each")
                        bad_ids = sres.get("rejected") or []
                        if bad_ids:
                            print(f"ELF patch: WARNING ignored {len(bad_ids)} "
                                  f"out-of-range ability id(s) {bad_ids} — "
                                  f"only ids 0-30 exist in the drop table")
                            summary.setdefault("elf_patch_warnings", []).append(
                                {"patch": "ability_drops",
                                 "reason": f"rejected ability ids {bad_ids}"})
                    else:
                        print("ELF patch: ability-drop table NOT found "
                              "(unexpected SLUS version?) — drops unchanged")
                if damage_taken != "normal":
                    dres = patch_damage_taken(data, damage_taken)
                    if dres.get("applied"):
                        print(f"ELF patch: Maximo damage-taken set to "
                              f"'{damage_taken}'")
                    else:
                        print(f"ELF patch: damage-taken patch NOT applied "
                              f"({dres.get('reason', 'site not found')})")
                if damage_dealt != "normal":
                    ddres = patch_damage_dealt(data, damage_dealt)
                    if ddres.get("applied"):
                        print(f"ELF patch: Maximo damage-dealt set to "
                              f"'{damage_dealt}'")
                    else:
                        print(f"ELF patch: damage-dealt patch NOT applied "
                              f"({ddres.get('reason', 'site not found')})")
                if do_shield_cost:
                    scres = patch_shield_cost(data, shield_cost)
                    if scres.get("applied"):
                        n = scres.get("sites", 0)
                        exp = scres.get("expected_sites", n)
                        label = (shield_cost_label
                                 if isinstance(shield_cost_label, str)
                                 else str(scres['cost']))
                        print(f"ELF patch: shield cost '{label}' — "
                              f"{scres['cost']} point(s) per block at "
                              f"{n} site(s)"
                              + (" — shield use does not deplete"
                                 if scres["cost"] == 0 else ""))
                        if n != exp:
                            reason = scres.get(
                                "reason", f"patched {n} of {exp} sites")
                            print(f"ELF patch: WARNING shield-cost site count "
                                  f"{n} != expected {exp}")
                            summary.setdefault("elf_patch_warnings", []).append(
                                {"patch": "shield_cost", "reason": reason})
                    else:
                        reason = scres.get("reason", "site not found")
                        print(f"ELF patch: shield-cost patch NOT applied "
                              f"({reason})")
                        summary.setdefault("elf_patch_warnings", []).append(
                            {"patch": "shield_cost", "reason": reason})
                if do_life_cost:
                    lcres = patch_life_cost(data, life_cost)
                    if lcres.get("applied"):
                        label = (life_cost_label
                                 if isinstance(life_cost_label, str)
                                 else str(lcres['cost']))
                        print(f"ELF patch: lives per death set to '{label}' "
                              f"({lcres['cost']} lives)"
                              + (" — deaths no longer cost a life"
                                 if lcres['cost'] == 0 else "")
                              + " (game-over test widened to lives <= 0)")
                        clamp = lcres.get("clamp") or {}
                        if clamp:
                            print(f"ELF patch: lives decrement clamped at 0 — "
                                  f"hook 0x{clamp['hook_vaddr']:08X} -> cave "
                                  f"0x{clamp['cave_vaddr']:08X} "
                                  f"({clamp['cave_bytes']} bytes"
                                  + (", reused" if clamp["reused"] else "")
                                  + ")")
                    else:
                        reason = lcres.get("reason", "site not found")
                        print(f"ELF patch: life-cost patch NOT applied "
                              f"({reason})")
                        summary.setdefault("elf_patch_warnings", []).append(
                            {"patch": "life_cost", "reason": reason})
                if do_sword_cost:
                    swres = patch_sword_cost(data, sword_cost)
                    if swres.get("applied"):
                        n = swres.get("sites", 0)
                        exp = swres.get("expected_sites", n)
                        label = (sword_cost_label
                                 if isinstance(sword_cost_label, str)
                                 else f"{swres['multiplier']}x")
                        print(f"ELF patch: sword point cost '{label}' — "
                              f"{swres['multiplier']}x vanilla cost at "
                              f"{n} site(s)"
                              + (" — special moves are free"
                                 if swres["multiplier"] == 0 else ""))
                        if n != exp:
                            reason = swres.get(
                                "reason", f"patched {n} of {exp} sites")
                            print(f"ELF patch: WARNING sword-cost site count "
                                  f"{n} != expected {exp}")
                            summary.setdefault("elf_patch_warnings", []).append(
                                {"patch": "sword_cost", "reason": reason})
                    else:
                        reason = swres.get("reason", "site not found")
                        print(f"ELF patch: sword-cost patch NOT applied "
                              f"({reason})")
                        summary.setdefault("elf_patch_warnings", []).append(
                            {"patch": "sword_cost", "reason": reason})
                if do_effect_durations:
                    edres = patch_effect_durations(
                        data, powerup=powerup_duration,
                        wizard=wizard_duration, frozen=frozen_duration)
                    # Each site is independent: report per-site, one line each.
                    for site_id, sres_d in edres.get("results", {}).items():
                        label = effect_duration_labels.get(site_id)
                        label = (label if isinstance(label, str)
                                 else f"{sres_d.get('multiplier')}x")
                        if sres_d.get("applied"):
                            print(f"ELF patch: {sres_d['label']} duration "
                                  f"'{label}' — {sres_d['ticks']} ticks "
                                  f"({sres_d['seconds']:.1f} s at "
                                  f"{EFFECT_TICKS_PER_SECOND} fps)")
                        else:
                            reason = sres_d.get("reason", "site not found")
                            print(f"ELF patch: {site_id}-duration patch NOT "
                                  f"applied ({reason})")
                            summary.setdefault(
                                "elf_patch_warnings", []).append(
                                {"patch": f"{site_id}_duration",
                                 "reason": reason})
                if do_koin_fee:
                    kfres = patch_koin_fee(data, koin_fee)
                    if kfres.get("applied"):
                        print(f"ELF patch: koin fee set to {kfres['fee']} — "
                              f"saving the game and hub travel both cost "
                              f"{kfres['fee']} koin(s) "
                              f"[{kfres.get('sites', 0)} site(s)]"
                              + (" — saving and hub travel are free"
                                 if kfres["fee"] == 0 else ""))
                        if not kfres.get("label_patched"):
                            reason = kfres.get(
                                "reason", "shared fee label not found")
                            print(f"ELF patch: WARNING koin-fee label NOT "
                                  f"updated ({reason})")
                            summary.setdefault("elf_patch_warnings", []).append(
                                {"patch": "koin_fee", "reason": reason})
                    else:
                        reason = kfres.get("reason", "site not found")
                        print(f"ELF patch: koin-fee patch NOT applied "
                              f"({reason})")
                        summary.setdefault("elf_patch_warnings", []).append(
                            {"patch": "koin_fee", "reason": reason})
                if do_disable_collision:
                    dcres = patch_disable_collision(data, True)
                    if dcres.get("applied"):
                        print(f"ELF patch: world collision DISABLED — "
                              f"bDoCollision cleared at file 0x"
                              f"{dcres['flag_offset']:X} (vaddr 0x"
                              f"{dcres['flag_vaddr']:X})"
                              + (" [already cleared]"
                                 if dcres.get("already_patched") else ""))
                        print("ELF patch: WARNING debug switch — the player and "
                              "camera no longer collide with the world, so you "
                              "WILL FALL THROUGH THE MAP as soon as gravity "
                              "applies. There is no noclip movement in retail "
                              "to compensate and a soft-lock is likely. "
                              "Research/screenshot tool only.")
                    else:
                        reason = dcres.get("reason", "site not found")
                        print(f"ELF patch: disable-collision patch NOT applied "
                              f"({reason})")
                        summary.setdefault("elf_patch_warnings", []).append(
                            {"patch": "disable_collision", "reason": reason})
                if do_level_timer:
                    ltres = patch_level_timer(data, level_timer,
                                              show_timer=timer_display)
                    if ltres.get("applied"):
                        per_world = ", ".join(
                            f"{k} {v}s" for k, v in ltres["times"].items())
                        print(f"ELF patch: level time limit preset "
                              f"'{ltres['preset']}' — {per_world} — "
                              f"cave at vaddr 0x{ltres['cave_vaddr']:X} "
                              f"({ltres['cave_bytes']} bytes), timer word 0x"
                              f"{ltres['timer_vaddr']:X}, value table 0x"
                              f"{ltres['table_vaddr']:X}, kill via GameVarSub 0x"
                              f"{ltres['gamevarsub']:X}")
                        print(f"ELF patch: level timer world selected at "
                              f"runtime — giLevelID disp -0x"
                              f"{0x10000 - ltres['levelid_disp']:X}, giLevel "
                              f"disp -0x{0x10000 - ltres['levelrow_disp']:X} "
                              f"(Dungeon of Despair = row "
                              f"{LEVEL_TIMER_DUNGEON_ROW})")
                        if ltres.get("show_timer"):
                            print(f"ELF patch: level timer countdown drawn on "
                                  f"screen — layer "
                                  f"{ltres['layer']}, AddString 0x"
                                  f"{ltres['addstring']:X}, RemoveLayer 0x"
                                  f"{ltres['removelayer']:X}, sprintf 0x"
                                  f"{ltres['sprintf']:X}, gEng 0x"
                                  f"{ltres['geng']:X}")
                        else:
                            print("ELF patch: level timer countdown display "
                                  "disabled (death timer only)")
                        print(f"ELF patch: level timer pauses during scripted "
                              f"chest/gate interactions — action id at "
                              f"Player+0x{ltres['action_id_vaddr']:X}, ids "
                              f"{', '.join(str(a) for a in ltres['pause_actions'])}"
                              f" — and STOPS on sub-level complete: hook at "
                              f"vaddr 0x{ltres['end_hook_vaddr']:X} -> cave 0x"
                              f"{ltres['end_vaddr']:X}")
                        print(f"ELF patch: level timer STOPS when the level-end "
                              f"column BREAKS — SetExtFunction hook at vaddr 0x"
                              f"{ltres['column_hook_vaddr']:X} -> cave 0x"
                              f"{ltres['column_vaddr']:X} (ext function "
                              f"{ltres['column_ext_function']}, case table 0x"
                              f"{ltres['column_case_table']:X})")
                        print(f"ELF patch: level timer NEVER KILLS once the "
                              f"level is over — per-frame kill gate at vaddr 0x"
                              f"{ltres['state_vaddr']:X} (ext-function queue: "
                              f"count Player+0x{ltres['ext_count_offset']:X}, "
                              f"records Player+0x{ltres['ext_record_offset']:X} "
                              f"stride {ltres['ext_record_stride']}, type "
                              f"{ltres['column_ext_function']}). The COUNTDOWN "
                              f"IS DELIBERATELY LEFT RUNNING — only the kill is "
                              f"gated.")
                        print(f"ELF patch: level timer STOP is DURABLE — re-arm "
                              f"gate word at vaddr 0x{ltres['gate_vaddr']:X} "
                              f"(empty = 0x{ltres['gate_none']:08X}), spill "
                              f"word 0x{ltres['spill_vaddr']:X}: a stop latches "
                              f"the stopped row, and PlaceAtStart refuses to "
                              f"re-arm that row. Death/respawn still re-arms "
                              f"(no stop fires on death).")
                        print(f"ELF patch: level timer DISARMED in the hubs — "
                              f"LevelTable rows "
                              f"{', '.join(str(r) for r in ltres['hub_rows'])} "
                              f"(identified by '_HUB' in their own PSX paths) "
                              f"map to slot {ltres['hub_slot']}, value 0 ticks")
                        print("ELF patch: WARNING experimental — the clock "
                              "re-arms on every level entry (intros and boss "
                              "arenas included, hubs excluded) AND on respawn "
                              "(per attempt) and may keep running while paused "
                              "or during cutscenes.")
                    else:
                        reason = ltres.get("reason", "site not found")
                        print(f"ELF patch: level-timer patch NOT applied "
                              f"({reason})")
                        summary.setdefault("elf_patch_warnings", []).append(
                            {"patch": "level_timer", "reason": reason})
                if do_widescreen:
                    res = patch_widescreen(data, True)
                    if res.get("applied"):
                        print(f"ELF patch: widescreen 16:9 enabled — horizontal scale at vaddr 0x{res['hor_vaddr']:X}, render aspect in gViewFrustum 0x{res['frustum_vaddr']:X}" + (" [already patched]" if res.get("already_patched") else ""))
                        print("ELF patch: NOTE the JP executable is patched through the US mechanism (the published JP horizontal cheat address is in BSS and cannot be patched statically).")
                    else:
                        reason = res.get("reason", "site not found")
                        print(f"ELF patch: widescreen patch NOT applied ({reason})")
                        summary.setdefault("elf_patch_warnings", []).append(
                            {"patch": "widescreen", "reason": reason})
                if do_object_budget:
                    obres = patch_object_budget(data, object_budget)
                    if obres.get("applied"):
                        print(f"ELF patch: on-screen object budget raised to "
                              f"{obres['budget']} (weight units, NOT object "
                              f"counts) — load-class constructor default at "
                              f"file 0x{obres['ctor_slot']:X} (classes 1-5) "
                              f"plus {obres['level_rows']} per-level enemy "
                              f"limit(s) in the LevelTable at file "
                              f"0x{obres['level_table']:X} "
                              f"[{obres.get('sites', 0)} site(s)]"
                              + (f" [already patched at "
                                 f"{obres.get('previous_budget')}]"
                                 if obres.get("already_patched") else ""))
                        if obres["budget"] > OBJECT_BUDGET_SAFE_MAX:
                            print(f"ELF patch: WARNING experimental — "
                                  f"{obres['budget']} is above the "
                                  f"verified-safe {OBJECT_BUDGET_SAFE_MAX} and "
                                  f"erodes the cushion between the densest "
                                  f"level's renderable population and the "
                                  f"engine's fixed 320-entry render list. "
                                  f"Expect frame-rate loss.")
                    else:
                        reason = obres.get("reason", "site not found")
                        print(f"ELF patch: object-budget patch NOT applied "
                              f"({reason})")
                        summary.setdefault("elf_patch_warnings", []).append(
                            {"patch": "object_budget", "reason": reason})
                if do_resource_pool:
                    rpres = patch_resource_pool(data, resource_pool)
                    if rpres.get("applied"):
                        print(f"ELF patch: RESOURCE memory pool raised to "
                              f"0x{rpres['size']:X} ({rpres['size']} B) — pool "
                              f"size `lui` at file 0x{rpres['size_slot']:X} = "
                              f"0x{rpres['size_word']:08X}, pool[0] reserve "
                              f"`lui` at file 0x{rpres['reserve_slot']:X} = "
                              f"0x{rpres['reserve_word']:08X} (derived reserve "
                              f"0x{rpres['reserve']:X}) "
                              f"[{rpres.get('sites', 0)} site(s)]"
                              + (f" [already patched at "
                                 f"0x{rpres.get('previous_size', 0):X}]"
                                 if rpres.get("already_patched") else ""))
                        print(f"ELF patch: NOTE this takes "
                              f"0x{rpres['size'] - RESOURCE_POOL_VANILLA:X} B "
                              f"off the MAIN pool (~24 MB) — the arena is fixed, "
                              f"so the pools only ever trade with each other."
                              + (f" Preset `{resource_pool_preset_label}` chosen"
                                 f" automatically by --cross-world / "
                                 f"--cross-world-structures (either one) — the only "
                                 f"preset that has been played in game, and the "
                                 f"default for either feature alone and for both "
                                 f"together (ported "
                                 f"structures get room from a reserved slice of "
                                 f"the draw budget instead); pass "
                                 f"--resource-pool vanilla to keep it off."
                                 if resource_pool_from_cross_world else ""))
                        # THE OBSERVED FAILURE, said out loud at the moment the
                        # user's explicit choice is applied. `high` was never
                        # reached automatically after the revert, so this can
                        # only be a deliberate --resource-pool high.
                        if rpres["size"] == RESOURCE_POOL_HIGH:
                            print("ELF patch: WARNING `high` "
                                  f"(0x{RESOURCE_POOL_HIGH:X}) is NOT "
                                  "RECOMMENDED: a real build with this preset "
                                  "in effect was OBSERVED TO CAUSE INFINITE "
                                  "LOADING ON MAP LOAD, while the otherwise "
                                  "identical `raised` "
                                  f"(0x{RESOURCE_POOL_RAISED:X}) build loaded "
                                  "and played fine. Nothing was fixed and the "
                                  "cause is unproven; the credible one is that "
                                  "it takes 0x200000 B more off MAIN than "
                                  "`raised` does, and the engine has no bounds "
                                  "check. It is never a default — you asked for "
                                  "it explicitly.")
                            summary.setdefault("elf_patch_warnings", []).append(
                                {"patch": "resource_pool",
                                 "reason": "preset `high` was OBSERVED to cause "
                                           "infinite loading on map load in a "
                                           "real build; not recommended"})
                    else:
                        reason = rpres.get("reason", "site not found")
                        print(f"ELF patch: resource-pool patch NOT applied "
                              f"({reason})")
                        summary.setdefault("elf_patch_warnings", []).append(
                            {"patch": "resource_pool", "reason": reason})
                if do_start_inv:
                    sinv = patch_starting_inventory(
                        data, gold=start_gold, lives=start_lives,
                        keys=start_keys, deathcoins=start_deathcoins,
                        sword_enchant=sword_enchant,
                        elemental_shield=elemental_shield,
                        souls=start_souls, armor=start_armor,
                        shields=start_shields, boxers=start_boxers)
                    if sinv.get("applied"):
                        print(f"ELF patch: starting inventory set "
                              f"{sinv['changed']}")
                        _sc = sinv.get("changed", {})
                        if start_souls is not None or start_armor is not None \
                                or start_shields is not None:
                            print(f"ELF patch: starting souls "
                                  f"({_sc.get('souls')}) / armor "
                                  f"({_sc.get('armor')}) / shields "
                                  f"({_sc.get('shields')})")
                        if start_boxers is not None:
                            print(f"ELF patch: starting boxers "
                                  f"({_sc.get('boxers')})")
                        if start_shields is not None:
                            print(f"ELF patch: shield points derived from "
                                  f"shield segments: "
                                  f"{_sc.get('shield_points')}")
                        if start_armor is not None \
                                and sinv.get("health_initial") is not None:
                            print(f"ELF patch: starting health derived from "
                                  f"starting armor: "
                                  f"{sinv.get('health_initial')} "
                                  f"(GameVarTbl[2] static initializer rewritten "
                                  f"at 0x{sinv.get('sinit_slot', 0):X}); note "
                                  f"a CONTINUE still restores vanilla 2 armor "
                                  f"/ 8 health")
                    else:
                        reason = sinv.get("reason", "GameVarTbl not found")
                        print(f"ELF patch: starting-inventory patch NOT applied "
                              f"({reason})")
                        if sinv.get("armor_skipped"):
                            print("ELF patch: WARNING starting armor was "
                                  "SKIPPED — the starting-health static "
                                  "initializer was not found, so armor was not "
                                  "written either (it would not have scaled "
                                  "health)")
                            summary.setdefault("elf_patch_warnings", []).append(
                                {"patch": "starting_armor", "reason": reason})
                # ORDER MATTERS: perma death runs AFTER starting inventory so its
                # zeroes WIN over any starting lives / death coins the user also
                # asked for. Both edit the same GameVarTbl entries (#4 and #8).
                if perma_death:
                    pdres = patch_perma_death(data, True)
                    if pdres.get("applied"):
                        print("ELF patch: PERMA DEATH enabled — 0 lives, "
                              "0 death coins, neither can be gained, first "
                              "death ends the run (no continue offered) "
                              f"[{pdres.get('sites', 0)} site(s)]")
                        pclamp = pdres.get("clamp") or {}
                        if pclamp:
                            print(f"ELF patch: lives decrement clamped at 0 — "
                                  f"hook 0x{pclamp['hook_vaddr']:08X} -> cave "
                                  f"0x{pclamp['cave_vaddr']:08X} "
                                  f"({pclamp['cave_bytes']} bytes"
                                  + (", reused" if pclamp["reused"] else "")
                                  + ")")
                        if start_lives or start_deathcoins:
                            print("ELF patch: note perma death overrode the "
                                  f"requested starting lives "
                                  f"({start_lives}) / death coins "
                                  f"({start_deathcoins})")
                    else:
                        reason = pdres.get("reason", "site not found")
                        print(f"ELF patch: perma-death patch NOT applied "
                              f"({reason})")
                        summary.setdefault("elf_patch_warnings", []).append(
                            {"patch": "perma_death", "reason": reason})
                if do_start_skills:
                    sskl = patch_starting_skills(data, start_skills)
                    if sskl.get("applied"):
                        print(f"ELF patch: starting skills set "
                              f"{sorted(sskl['skills'])}")
                    else:
                        print(f"ELF patch: starting-skills patch NOT applied "
                              f"({sskl.get('reason', 'InitVars site not found')})")
                if do_rand_levels:
                    rlv = patch_randomize_levels(
                        data, seed,
                        cross_world=bool(getattr(args, "randomize_levels_cross", False)))
                    if rlv.get("applied"):
                        if rlv.get("cross_world"):
                            print("ELF patch: WHOLE-WORLD swap (experimental) — "
                                  "entire worlds permuted:")
                            for dst, src in rlv.get("mapping", []):
                                if dst != src:
                                    print(f"    entering {dst} now plays {src}")
                            excl = rlv.get("excluded_worlds")
                            if excl:
                                print(f"    (kept vanilla, non-standard boss layout: "
                                      f"{', '.join(excl)})")
                        else:
                            print(f"ELF patch: levels RANDOMIZED — "
                                  f"{rlv['sublevels']} sub-levels shuffled within their worlds")
                            for dst, src in rlv.get("mapping", []):
                                if dst != src:
                                    print(f"    {dst}  loads  {src}")
                    else:
                        print(f"ELF patch: level-randomize patch NOT applied "
                              f"({rlv.get('reason', 'LevelTable not found')})")
                elf_out.write_bytes(bytes(data))
                print(f"ELF patch: wrote {elf_out}")
            except Exception as e:
                print(f"\nELF patch FAILED: {e}")
        else:
            print(f"\nELF patch SKIPPED: no boot executable "
                  f"({' / '.join(KNOWN_EXECUTABLES)}) found in source folder.")

    # Challenge Mode checkpoint suppression totals (0 when the level timer is
    # off, since the gate is the timer).
    summary["holy_ground_suppressed_total"] = holy_ground_total
    if holy_ground_skipped:
        summary["holy_ground_skipped_files"] = holy_ground_skipped
    if do_no_holy_ground:
        print(f"Holy_Ground checkpoints suppressed: {holy_ground_total}"
              + (f" (kept in {', '.join(holy_ground_skipped)}: no inert "
                 f"same-file template fit)" if holy_ground_skipped else ""))

    summary_path = out_folder / "randomizer_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2))
    print(f"\nSummary written to: {summary_path}")
    print(f"Total processed: {len(summary['files_processed'])}, errors: {len(summary['errors'])}")


def _resolve_worlds(value: str | None) -> set[str]:
    """Parse the --worlds argument. Returns a set of canonical world names.

    Accepted values:
      None or "all"            -> every world
      "grave,under,..."        -> only those worlds
      "grave"                  -> just one
    """
    if not value or value.lower() == "all":
        return set(ALL_WORLDS)
    parts = [p.strip().lower() for p in value.split(",") if p.strip()]
    bad = [p for p in parts if p not in ALL_WORLDS]
    if bad:
        print(f"ERROR: unknown world(s): {bad}. Valid: {', '.join(ALL_WORLDS)}")
        sys.exit(1)
    return set(parts)


def main():
    parser = argparse.ArgumentParser(
        prog="randomizer",
        description="Maximo: Ghosts to Glory enemy randomizer",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_stats = sub.add_parser("stats", help="Show statistics for a PSX file")
    p_stats.add_argument("psx_file", help="Path to a PSX file")
    p_stats.set_defaults(func=cmd_stats)

    p_list = sub.add_parser("list-types", help="List all entity types in a folder of PSX files")
    p_list.add_argument("psx_folder", help="Folder containing PSX files")
    p_list.set_defaults(func=cmd_list_types)

    p_rand = sub.add_parser("randomize", help="Randomize enemies/items in a folder of PSX files")
    p_rand.add_argument("psx_folder", help="Folder containing source PSX files")
    p_rand.add_argument("--output", "-o", required=True, help="Output folder")
    p_rand.add_argument("--seed", type=int, help="RNG seed (default: random)")
    p_rand.add_argument("--items", action="store_true", help="Randomize items/structures (universal type swap), plus collectors, enemy tiers, generators, and weather")
    p_rand.add_argument("--chests", action="store_true", default=None, help="Randomize chest contents and gold amounts")
    p_rand.add_argument("--skills", action="store_true", default=None, help="Randomize ability/skill pickup IDs")
    p_rand.add_argument("--columns", action="store_true", default=None, help="Randomize HUB level-select columns (blind select + '???' level titles)")
    p_rand.add_argument("--no-gen-tier", action="store_false", dest="gen_tier",
                        default=True,
                        help="Disable monster-generator class-level randomization "
                             "(on by default — generators spawn base/upgraded/elite variants)")
    p_rand.add_argument("--damage-taken", dest="damage_taken", default="normal",
                        choices=["0.25", "0.50", "normal", "2x", "4x"],
                        help="Scale the damage Maximo takes from enemies "
                             "(0.25/0.50/normal/2x/4x). Default normal.")
    p_rand.add_argument("--damage-dealt", dest="damage_dealt", default="normal",
                        choices=["0.25", "0.50", "normal", "2x", "4x"],
                        help="Scale the melee damage Maximo deals to enemies "
                             "(0.25/0.50/normal/2x/4x). Default normal.")
    p_rand.add_argument("--shield-cost", dest="shield_cost", default=None,
                        choices=list(SHIELD_COST_PRESETS),
                        metavar="{" + "/".join(SHIELD_COST_PRESETS) + "}",
                        help="Shield points one shield block spends: "
                             "none (0), vanilla (1), light (2), medium (3), "
                             "heavy (5). 'none' makes shield use "
                             "non-depleting. 'vanilla' is the default and "
                             "leaves the executable untouched.")
    p_rand.add_argument("--life-cost", dest="life_cost", default=None,
                        choices=list(LIFE_COST_PRESETS),
                        metavar="{" + "/".join(LIFE_COST_PRESETS) + "}",
                        help="Lives one death spends: vanilla (1), none (0), "
                             "double (2), triple (3), quadruple (4). 'none' "
                             "means deaths no longer cost a life. 'vanilla' is "
                             "the default and leaves the executable untouched. "
                             "Note a new game starts with 4 lives, so quadruple "
                             "means the first death ends the run.")
    p_rand.add_argument("--perma-death", dest="perma_death",
                        action="store_true",
                        help="PERMA DEATH: Maximo starts with 0 lives and 0 "
                             "death coins, neither can ever be gained (1-ups "
                             "and death coins are capped at 0), and the first "
                             "death ends the run permanently — no continue is "
                             "offered. Overrides any starting lives / death "
                             "coins set elsewhere (--start-lives, "
                             "--start-deathcoins, randomized loadouts).")
    p_rand.add_argument("--sword-cost", dest="sword_cost", default=None,
                        choices=list(SWORD_COST_MULTIPLIERS),
                        metavar="{" + "/".join(SWORD_COST_MULTIPLIERS) + "}",
                        help="Multiplier on the sword enchantment points each "
                             "special move spends: none (0x, free), vanilla "
                             "(1x), light (2x), medium (3x), heavy (5x). "
                             "'vanilla' is the default and leaves the "
                             "executable untouched.")
    p_rand.add_argument("--powerup-duration", dest="powerup_duration",
                        default=None,
                        choices=list(EFFECT_DURATION_PRESET_LABELS),
                        metavar="{" + "/".join(EFFECT_DURATION_PRESET_LABELS) + "}",
                        help="Preset for the Golden Armor AND Mask of Sorrow "
                             "timer (vanilla 20.0 s): vanilla (default — "
                             "executable untouched), none (1 tick, gone on the "
                             "first frame), easier (2x, 40.0 s), harder (0.5x, "
                             "10.0 s). This power-up is BENEFICIAL, so easier "
                             "means LONGER. NOTE both power-ups share one timer "
                             "constant in the executable, so they are LOCKED "
                             "TOGETHER and cannot be set separately.")
    p_rand.add_argument("--wizard-duration", dest="wizard_duration",
                        default=None,
                        choices=list(EFFECT_DURATION_PRESET_LABELS),
                        metavar="{" + "/".join(EFFECT_DURATION_PRESET_LABELS) + "}",
                        help="Preset for the wizard-form timer covering Baby "
                             "AND Old Man (vanilla 10.0 s): vanilla (default — "
                             "executable untouched), none (1 tick, gone on the "
                             "first frame), easier (0.5x, 5.0 s), harder (2x, "
                             "20.0 s). These forms are HARMFUL, so easier means "
                             "SHORTER. NOTE both forms share one timer "
                             "constant, so they are LOCKED TOGETHER and cannot "
                             "be set separately.")
    p_rand.add_argument("--frozen-duration", dest="frozen_duration",
                        default=None,
                        choices=list(EFFECT_DURATION_PRESET_LABELS),
                        metavar="{" + "/".join(EFFECT_DURATION_PRESET_LABELS) + "}",
                        help="Preset for how long Frozen lasts (vanilla "
                             "5.0 s): vanilla (default — executable "
                             "untouched), none (1 tick, gone on the first "
                             "frame), easier (0.5x, 2.5 s), harder (2x, "
                             "10.0 s). Frozen is HARMFUL, so easier means "
                             "SHORTER. There is no mash-to-escape, so the timer "
                             "is the whole duration.")
    # Deliberately NOT choices=range(0, 101): argparse would render a 101-item
    # help string. The range is validated explicitly after parse_args() instead.
    p_rand.add_argument("--koin-fee", dest="koin_fee", type=int, default=None,
                        metavar="N",
                        help=f"Koin cost to SAVE the game and to TRAVEL between "
                             f"hubs (vanilla {KOIN_FEE_VANILLA}, range "
                             f"{KOIN_FEE_MIN}-{KOIN_FEE_MAX}). 0 makes both "
                             f"free. Both share ONE value because the on-screen "
                             f"label is a single shared string ('<n> Koins for "
                             f"Save or Travel'). Values above {KOIN_FEE_MAX} are "
                             f"REFUSED: the save record's koin subtract is "
                             f"unclamped, so a higher fee can drive the saved "
                             f"koin count negative. Omit to keep vanilla.")
    p_rand.add_argument("--disable-collision", dest="disable_collision",
                        action="store_true",
                        help="DEBUG/RESEARCH ONLY — clear the developers' own "
                             "bDoCollision byte so the player and camera stop "
                             "colliding with the world. This is NOT a playable "
                             "mode: you WILL FALL THROUGH THE MAP as soon as "
                             "gravity applies, retail has no fly/noclip "
                             "movement to compensate, and a soft-lock is likely. "
                             "Fully reversible (one byte). Screenshot / "
                             "research tool only.")
    p_rand.add_argument("--level-timer", dest="level_timer", default=None,
                        choices=["off", *LEVEL_TIMER_PRESET_NAMES],
                        help="EXPERIMENTAL — level time limit difficulty "
                             "preset. Each preset carries PER-WORLD durations "
                             "(roughly 2-2.5 min in Grave rising to 3.5-4 min "
                             "in Castle; 'hard' is tightest, 'easy' loosest), "
                             "with the Dungeon of Despair given its own longer "
                             "limit. The death runs through the game's own "
                             "death path (lives, death camera, music). The "
                             "clock resets on EVERY level entry — hubs, intros "
                             "and boss arenas included — and on respawn, so "
                             "the limit is PER ATTEMPT. It may keep running "
                             "while paused or during cutscenes. 'off' (the "
                             "default) leaves the executable alone.")
    p_rand.add_argument("--widescreen", dest="widescreen",
                        action="store_true",
                        help="render the game at 16:9 instead of 4:3 (static "
                             "port of the PCSX2 widescreen cheat: horizontal "
                             "scale 1.0 -> 0.75 and render aspect 1.4 -> "
                             "1.866666)")
    # DEFAULT IS NOW `more`, NOT `vanilla`. The raised on-screen object budget is
    # a standard part of a build at the user's direction: it is what lets more of
    # a level's own objects and enemies be active at once. Pass
    # `--object-budget vanilla` to leave the executable untouched.
    p_rand.add_argument("--object-budget", dest="object_budget", default="more",
                        metavar="{" + "/".join(OBJECT_BUDGET_PRESETS) + "/N}",
                        help=f"EXPERIMENTAL — raise the script VM's per-type "
                             f"on-screen object budget so more of a level's own "
                             f"objects and enemies can be active at once "
                             f"(vanilla {OBJECT_BUDGET_VANILLA} = executable "
                             f"untouched, 'more' = {OBJECT_BUDGET_MAX}, or "
                             f"a raw integer {OBJECT_BUDGET_MIN}-"
                             f"{OBJECT_BUDGET_MAX}). These are WEIGHT budgets, "
                             f"NOT object counts: each entity spends its own "
                             f"cost. ENEMIES come from the PER-LEVEL LevelTable "
                             f"limit, which the engine re-applies at every "
                             f"level load, so both it and the constructor "
                             f"default are patched together; the per-level "
                             f"values are scaled proportionally so the one "
                             f"deliberately tighter Castle level stays tighter. "
                             f"Above {OBJECT_BUDGET_SAFE_MAX} is explicitly "
                             f"experimental and may cost frame rate; above "
                             f"{OBJECT_BUDGET_MAX} is REFUSED. Omit to keep "
                             f"vanilla.")
    p_rand.add_argument("--resource-pool", dest="resource_pool", default=None,
                        metavar="{" + "/".join(RESOURCE_POOL_PRESETS)
                                + "/SIZE}",
                        help=f"raise the engine's RESOURCE memory pool "
                             f"(vanilla 0x{RESOURCE_POOL_VANILLA:X} = "
                             f"executable untouched, 'raised' = "
                             f"0x{RESOURCE_POOL_RAISED:X} (THE DEFAULT for "
                             f"cross-world runs: it has loaded and played "
                             f"CONSISTENTLY across many builds), 'unreliable' "
                             f"(historical spelling 'loaded-max') = "
                             f"0x{RESOURCE_POOL_LOADED_MAX:X} (SELECTABLE if you "
                             f"want to retry it, NEVER a default: it LOADED and "
                             f"played once and later HUNG with infinite loading "
                             f"on another build), or ANY SIZE "
                             f"0x{RESOURCE_POOL_MIN:X}-"
                             f"0x{RESOURCE_POOL_CRASHED_MIN - 0x10000:X} in "
                             f"64 KiB steps -- a "
                             f"leading 0x is read as HEX, a bare number as "
                             f"DECIMAL). WHAT IS KNOWN, from console builds: "
                             f"0x{RESOURCE_POOL_RAISED:X} LOADED and played, "
                             f"consistently; 0x{RESOURCE_POOL_LOADED_MAX:X} "
                             f"LOADED ONCE and HUNG ONCE, so it is UNRELIABLE; "
                             f"0x{RESOURCE_POOL_CRASHED_MIN:X} and "
                             f"0x{RESOURCE_POOL_HIGH:X} both HUNG ON MAP LOAD "
                             f"and have NEVER loaded, "
                             f"so 0x{RESOURCE_POOL_CRASHED_MIN:X} and above is "
                             f"REFUSED and the old 'high' preset was REMOVED. "
                             f"0x810000-0x870000 is still UNEXPLORED, and since "
                             f"the hang boundary is NOT a clean step one "
                             f"loading build there would prove nothing. "
                             f"SUSPECTED, NOT MEASURED, for the hang: the "
                             f"three pools tile a fixed arena with no slack and "
                             f"no bounds check, so growing RESOURCE starves "
                             f"MAIN. PAYOFF: at the 93% occupancy ceiling each "
                             f"64 KiB of pool buys ~59 KiB of draw budget, and "
                             f"enemy routes cost 225 KB (Axe_Guard) to 841 KB "
                             f"(Plant_Monster), so ~+512 KB of pool is worth "
                             f"roughly ONE to TWO extra arrivals per world. The "
                             f"arena is fixed, so "
                             f"every byte given to RESOURCE is taken from the "
                             f"MAIN pool (~24 MB); pool[0]'s reserve is raised "
                             f"by the same amount in the same edit so the three "
                             f"pools still tile the arena exactly. Only the "
                             f"`lui` halves are editable, so the low 16 bits of "
                             f"the size must be 0. --cross-world turns this on "
                             f"by default ('raised', with or without "
                             f"--cross-world-structures); pass 'vanilla' to "
                             f"keep it off.")
    p_rand.add_argument("--pool-occupancy", dest="pool_occupancy", default=None,
                        metavar="{measured/PCT}",
                        help="EXPERIMENTAL. The share of the RESOURCE pool the "
                             "cross-world draw budget is allowed to occupy, as "
                             "a percentage. THREE DIFFERENT NUMBERS, not to be "
                             "conflated: the DEFAULT is 90.0 ('measured', "
                             "'default' and blank all mean this); the highest "
                             "occupancy MEASURED TO LOAD is 93.0; and 93.5 and "
                             "above is REFUSED, not clamped. The measurement is "
                             "EMPIRICAL: the band was walked one "
                             "console build at a time, and 95 CRASHED, 94 "
                             "CRASHED, 93.5 CRASHED, 93 "
                             "LOADED and played. So 93.0 is the highest "
                             "occupancy OBSERVED TO LOAD and 93.5 the lowest "
                             "OBSERVED TO CRASH, and only 93.0-93.5 remains "
                             "UNEXPLORED. The default sits DELIBERATELY BELOW "
                             "the measurement, set at the user's request for "
                             "testing; lowering is always safe because it can "
                             "only ship a smaller lineup, and anything up to "
                             "(not including) 93.5 -- 93.0 included -- is "
                             "still selectable here per build. WHAT IT COSTS: "
                             "at the default 0x800000 pool the default budgets "
                             "7,549,747 B (0.90 * 8,388,608), which is 251,658 "
                             "B LESS than the 7,801,405 B at 93.0, i.e. roughly "
                             "ONE FEWER arrival where the budget rather than "
                             "the lineup cap binds, since enemy routes cost "
                             "225 KB to 840 KB. WHAT IT RISKS: pool "
                             "exhaustion is SILENT -- MemoryPool::Allocate "
                             "falls through and returns sentinel_block + 16, "
                             "the caller corrupts allocator metadata and the "
                             "game dies at level load with NO log line, no "
                             "build failure and no test failure. A build that "
                             "loads at a higher figure is evidence worth "
                             "recording; a build that hangs on the loading "
                             "screen means drop back.")
    p_rand.add_argument("--start-gold", dest="start_gold", type=int, default=None,
                        help="Starting koins for a new game (0-9999). "
                             "Omit to keep vanilla (0).")
    p_rand.add_argument("--start-lives", dest="start_lives", type=int, default=None,
                        help="Starting lives for a new game (0-99). "
                             "Omit to keep vanilla (4).")
    p_rand.add_argument("--start-keys", dest="start_keys", type=int, default=None,
                        help="Starting keys for a new game (0-9). "
                             "Omit to keep vanilla (0).")
    p_rand.add_argument("--start-deathcoins", dest="start_deathcoins", type=int,
                        default=None,
                        help="Starting death coins for a new game (0-99). "
                             "Omit to keep vanilla (1).")
    p_rand.add_argument("--start-souls", dest="start_souls", type=int,
                        default=None,
                        help="Starting souls / HUD spirit for a new game "
                             "(0-999). Omit to keep vanilla (0).")
    p_rand.add_argument("--start-armor", dest="start_armor", type=int,
                        default=None,
                        help="Starting armor segments for a new game (0-4). "
                             "Omit to keep vanilla (2).")
    p_rand.add_argument("--start-shields", dest="start_shields", type=int,
                        default=None,
                        help="Starting shield count for a new game (0-4). "
                             "Shield points are set to 5x this to match. "
                             "Omit to keep vanilla (2).")
    p_rand.add_argument("--start-boxers", dest="start_boxers", type=int,
                        default=None,
                        help="Which pair of boxers Maximo wears (0-3). Only "
                             "visible while armor is 0 or 1. Omit to keep "
                             "vanilla (3).")
    p_rand.add_argument("--sword-enchant", dest="sword_enchant", type=int,
                        default=None,
                        help="Starting sword enchant tier: 0=normal, 1=Fire, "
                             "2=Ice, 3=Sun, 4=Armageddon. Omit to keep vanilla.")
    p_rand.add_argument("--elemental-shield", dest="elemental_shield", type=int,
                        default=None,
                        help="Starting elemental shield: 0=none, 1=Wind, "
                             "2=Magnetic, 3=Lightning. Omit to keep vanilla.")
    p_rand.add_argument("--start-skills", dest="start_skills", default=None,
                        help="Comma-separated new-game starting sword skills: "
                             "sword720,double_slash,mighty_blow,masquerade.")
    p_rand.add_argument("--randomize-start-inv", action="store_true",
                        dest="randomize_start_inv",
                        help="Roll seeded random starting gold/lives/keys/"
                             "deathcoins/skills (overrides the manual values).")
    p_rand.add_argument("--randomize-levels", action="store_true",
                        dest="randomize_levels",
                        help="Shuffle which level loads in each playable "
                             "sub-level slot, within each world (repoints the "
                             "engine's LevelTable file pointers; progression is "
                             "preserved).")
    p_rand.add_argument("--randomize-levels-cross", action="store_true",
                        dest="randomize_levels_cross",
                        help="EXPERIMENTAL: with --randomize-levels, permute "
                             "ENTIRE worlds (whole-world swap) so each hub plays "
                             "a different world end-to-end, instead of shuffling "
                             "sub-levels within each world.")
    p_rand.add_argument("--preserve-chests", action="store_true", dest="preserve_chests",
                        help="Keep every vanilla chest in place (type/position) "
                             "but randomize the contents inside them. Composes "
                             "with --harder-mode (opt-in add-on, not forced off).")
    p_rand.add_argument("--preserve-iron-keys", action="store_true", dest="preserve_iron_keys",
                        help="Keep every vanilla Iron Key (Skeleton_Key) in its "
                             "original position/count — keys are never moved, "
                             "removed, or added at random. Composes with "
                             "--harder-mode (opt-in add-on, not forced off).")
    p_rand.add_argument("--harder-mode", action="store_true", dest="harder_mode",
                        help="Enable HARDER MODE: koin-only economy (gold/gem/"
                             "health/life disabled), structures disabled and "
                             "spawned as enemies, rarer keys/collectors/skills, "
                             "more item->enemy rolls, mostly-empty enemy drops, "
                             "all enemies at max class, 1-koin chests, "
                             "mimic/wizard 33%% each, boss x2 (Grave+Swamp), "
                             "Maximo takes 2x / deals 0.5x damage.")
    p_rand.add_argument("--gate-mode", dest="gate_mode", default=None,
                        choices=["isolated", "pool"],
                        help="Gate randomizer: 'isolated' swaps gates among gate "
                             "types only; 'pool' adds gates to the item pool so "
                             "they can appear anywhere. Omit to leave gates alone.")
    p_rand.add_argument("--spawn-location", action="store_true", dest="spawn_location",
                        help="Randomize Maximo's level-start (spawn) position — moved to a random item/structure/enemy location")
    p_rand.add_argument("--swap-spawn-with-exit", action="store_true",
                        dest="swap_spawn_with_exit",
                        help="Swap Maximo's level-start with the level-exit column position "
                             "(two-way). Overrides --spawn-location and --randomize-exit-location.")
    p_rand.add_argument("--spawn-config", dest="spawn_config_path", default=None,
                        help="Path to a spawn-rate JSON config (per-tag/per-world weights + enable flags). "
                             "Omit to use the stock spawn rates.")
    p_rand.add_argument("--no-enemies", action="store_true", help="Don't randomize enemies (only items if --items)")
    p_rand.add_argument(
        "--all-enemies", action="store_true",
        help="CHAOS mode: transform every item, chest, structure and smashable "
             "into an enemy. Gold_Keys are preserved. Enemies can drop Skeleton "
             "Keys, and the 1%% Skeleton-Key / Gold-Key map lotteries still run "
             "so the game stays beatable.",
    )
    p_rand.add_argument(
        "--boss-arena-arrivals", dest="boss_arena_arrivals",
        action="store_true", default=False,
        help="EXPERIMENTAL, default OFF. Let CROSS-WORLD ENEMY arrivals reach "
             "the four `_B` BOSS ARENAS (GRAVE_B / SWAMP_B / ICE_B / UNDER_B; "
             "castle is excluded, its arenas are CASTLE_K/_Q and have no "
             "measured decorative victim set). Ports through the UNCHANGED "
             "gated path into the arena's own archives and retypes ONLY the "
             "proven-decorative records items.BOSS_DUPLICATION already names "
             "(side lights / ambient fireflies / sea weed / steam vents) -- the "
             "arena is NOT opened to general randomization, because boss death "
             "and victory triggers are keyed on instance names. Route costs "
             "into an arena are PREDICTED from the enemy's measured row into "
             "the arena's own world, and the arena byte budget is UNVERIFIED "
             "on console. Independent of --duplicate-bosses and --inject-boss.")
    p_rand.add_argument(
        "--boss-arena-difficulty", dest="boss_arena_difficulty",
        choices=sorted(boss_arena.ARENA_DIFFICULTY_ARRIVALS),
        default=boss_arena.ARENA_DIFFICULTY_DEFAULT,
        help=f"How many cross-world enemies SPAWN in each boss arena with "
             f"--boss-arena-arrivals: "
             f"easy={boss_arena.ARENA_DIFFICULTY_ARRIVALS['easy']}, "
             f"normal={boss_arena.ARENA_DIFFICULTY_ARRIVALS['normal']}, "
             f"hard={boss_arena.ARENA_DIFFICULTY_ARRIVALS['hard']} per arena "
             f"(default {boss_arena.ARENA_DIFFICULTY_DEFAULT}). This counts "
             f"ENEMIES, NOT TYPES: exactly "
             f"{boss_arena.MAX_PORTED_TYPES_PER_ARENA} distinct types are always "
             f"ported whatever the setting, and the higher counts are reached by "
             f"reusing them across the arena's decorative slots, which costs no "
             f"archive bytes. CHOSEN FIGURES, NOT MEASURED LIMITS. One enemy per "
             f"vetted decorative slot is the hard ceiling, and GRAVE has 4 slots "
             f"while SWAMP has 6, so those two cannot reach 9; the shortfall is "
             f"logged.")
    p_rand.add_argument(
        "--duplicate-bosses", action="store_true",
        help="EXPERIMENTAL: clone the boss in supported boss arenas (currently "
             "Grave / GraveDigger and Swamp / BokorLaBas) so the fight has "
             "multiple bosses. May be unstable — boss death/victory triggers "
             "are scripted for one boss.",
    )
    p_rand.add_argument(
        "--inject-boss", action="store_true",
        help="EXPERIMENTAL: inject the GraveDigger boss into an ordinary grave "
             "level by merging its BEF types and recycling one decorative "
             "record. Its OWN mesh, 22 animation records and 3 textures are "
             "PORTED from GRAVE_B.PRS / GRAVE_B.PRT into GRAVE.PRS / "
             "GRAVE.PRT and the blob's references repointed: they do NOT "
             "already live in GRAVE.PRS, where index 0 is a 0-bone prop. "
             + boss_inject.EARLY_EXIT_RISK_TEXT,
    )
    p_rand.add_argument(
        "--boss-clones", type=int, default=1,
        help="Legacy: extra boss clones for ALL bosses when --duplicate-bosses "
             "is set. Overridden by --boss-clones-grave / --boss-clones-swamp.",
    )
    p_rand.add_argument(
        "--boss-clones-grave", type=int, default=None,
        help="Extra GraveDigger clones (Grave) when --duplicate-bosses is set "
             "(max 4; 4 clones = 5 bosses).",
    )
    p_rand.add_argument(
        "--boss-clones-swamp", type=int, default=None,
        help="Extra BokorLaBas clones (Swamp) when --duplicate-bosses is set "
             "(max 6; 6 clones = 7 bosses).",
    )
    p_rand.add_argument(
        "--no-dark-knight", action="store_true", dest="exclude_dark_knight",
        help="Exclude Dark_Knight (Castle enemy) from every randomization "
             "pool, keeping only the vanilla-placed ones. Dark_Knight has a "
             "kill-event quirk that can SOFT-LOCK a level if it lands on a "
             "progression slot, so this is the safe option.",
    )
    p_rand.add_argument(
        "--worlds", default="all",
        help="Comma-separated worlds to randomize: all, grave, under, swamp, ice, castle. "
             "Examples: --worlds all (default), --worlds grave, --worlds grave,under",
    )
    p_rand.add_argument(
        "--cross-world", action="store_true",
        help="Use the universal enemy pool for all worlds. Requires BEF injection "
             "(done automatically by iso_patcher --cross-world). In folder mode, "
             "the BEF files in the input folder must already have been expanded. "
             "Enables ANY enemy type to spawn in ANY world.",
    )
    p_rand.add_argument(
        "--cross-world-structures", dest="cross_world_structures",
        action="store_true", default=False,
        help="EXPERIMENTAL. Also port STRUCTURES (smashables, props and "
             "decorations) across worlds, so an Ice Plant can appear in the "
             "graveyard. Ported structures are COSMETIC ONLY: progression and "
             "puzzle props are EXCLUDED (Bonus Grave, Bone Tower, Popup "
             "Headstone and the ordered puzzle chains are never ported), no "
             "protected record is ever a destination, and structures are "
             "admitted AFTER enemies out of the same resource budget so they "
             "can never displace an arriving enemy. INDEPENDENT of "
             "--cross-world: either switch works on its own, and with "
             "cross-world enemies off the structure pass gets the WHOLE draw "
             "budget with nothing reserved. When BOTH are on they share that "
             "budget, so this RESERVES A SLICE of it "
             "(300,000 B) by lowering the ENEMY admission ceiling by the same "
             "amount, which costs roughly one foreign enemy arrival per world. "
             "The RESOURCE pool default stays 'raised' either way: the 'high' "
             "preset was tried and OBSERVED TO CAUSE INFINITE LOADING ON MAP "
             "LOAD. OFF by default. Turning it on with no per-route "
             "selection saved "
             "makes EVERY selectable route a candidate and lets the resource "
             "budget decide how many actually fit; to narrow that down, tick "
             "exactly the routes you want in the spawn editor's Cross-World "
             "Arrivals dialog and your selection is honoured as ticked. Every "
             "route is CANDIDATE until you observe it in game.",
    )
    p_rand.set_defaults(func=cmd_randomize)

    args = parser.parse_args()

    # Explicit range check for --koin-fee (see the flag's comment: choices=
    # range(0, 101) would produce an unreadable 101-item help string). Exits
    # nonzero with a message naming the range and the reason for the cap.
    kf = getattr(args, "koin_fee", None)
    if kf is not None and not (KOIN_FEE_MIN <= kf <= KOIN_FEE_MAX):
        p_rand.error(
            f"--koin-fee must be {KOIN_FEE_MIN}-{KOIN_FEE_MAX} (got {kf}); "
            f"above {KOIN_FEE_MAX} the save record's unclamped koin subtract "
            f"can drive the saved koin count negative")

    # --object-budget accepts a preset label OR a raw integer, so it is checked
    # here rather than with argparse `choices`/`type=int`.
    ob = getattr(args, "object_budget", None)
    if ob is not None and str(ob).strip():
        label = str(ob).strip().lower()
        if label not in OBJECT_BUDGET_PRESETS:
            try:
                value = int(label)
            except ValueError:
                p_rand.error(
                    f"--object-budget must be one of "
                    f"{'/'.join(OBJECT_BUDGET_PRESETS)} or an integer "
                    f"{OBJECT_BUDGET_MIN}-{OBJECT_BUDGET_MAX} (got {ob!r})")
            else:
                if not (OBJECT_BUDGET_MIN <= value <= OBJECT_BUDGET_MAX):
                    p_rand.error(
                        f"--object-budget must be {OBJECT_BUDGET_MIN}-"
                        f"{OBJECT_BUDGET_MAX} (got {value}); above "
                        f"{OBJECT_BUDGET_SAFE_MAX} is already experimental and "
                        f"above {OBJECT_BUDGET_MAX} the cushion between the "
                        f"densest level's renderable population and the "
                        f"engine's fixed 320-entry render list is gone")
                args.object_budget = value

    # --resource-pool accepts a preset label OR a size (bare = DECIMAL, 0x… =
    # hex), so it is checked here rather than with argparse `choices`/`type=int`.
    # ONE VALIDATOR: `elf_patch.resolve_resource_pool` owns the range, the 64 KiB
    # alignment and the label mapping, and its refusal message is what the user
    # sees. An unknown label RAISES there (it used to resolve to None, i.e.
    # silently to VANILLA), so a typo like `--resource-pool raized` is now a
    # usage error instead of a vanilla pool paired with someone else's budget.
    rp = getattr(args, "resource_pool", None)
    if rp is not None and str(rp).strip():
        try:
            value = resolve_resource_pool(rp)
        except ValueError as exc:
            p_rand.error(f"--resource-pool: {exc}")
        else:
            # None means the explicit 'vanilla' label; leave it as the user
            # spelled it so the cross-world default below still sees a choice
            # was made.
            if value is not None:
                args.resource_pool = value

    # --pool-occupancy accepts the label 'measured' OR a float percentage, so it
    # is range-checked HERE, exactly like --resource-pool above, rather than with
    # argparse `type=float`. The refusal at the measured crash floor is the same
    # shape as the RESOURCE_POOL_MAX refusal: never clamped, because there is no
    # engine-side check that would catch it.
    po = getattr(args, "pool_occupancy", None)
    if po is not None and str(po).strip():
        from .roster_selection import (LOADED_MAX_PCT as _LOADED_MAX_PCT,
                                       DEFAULT_MAX_PCT as _DEFAULT_MAX_PCT,
                                       CRASHED_MIN_PCT as _CRASHED_MIN_PCT)
        label = str(po).strip().lower()
        if label in ("measured", "default", "vanilla"):
            args.pool_occupancy = _DEFAULT_MAX_PCT
        else:
            try:
                pct = float(label)
            except ValueError:
                p_rand.error(
                    f"--pool-occupancy must be 'measured' (= the default "
                    f"{_DEFAULT_MAX_PCT}, which is below the measured loading "
                    f"ceiling of {_LOADED_MAX_PCT}) or a percentage greater "
                    f"than 0 and below {_CRASHED_MIN_PCT} (got {po!r})")
            else:
                if pct <= 0:
                    p_rand.error(
                        f"--pool-occupancy must be greater than 0 (got {pct})")
                if pct >= _CRASHED_MIN_PCT:
                    p_rand.error(
                        f"--pool-occupancy {pct} is REFUSED: "
                        f"{_CRASHED_MIN_PCT}% is the MEASURED CRASH FLOOR -- "
                        f"both builds that crashed were at or above it, and "
                        f"pool exhaustion is SILENT (MemoryPool::Allocate falls "
                        f"through and returns sentinel_block + 16, the caller "
                        f"corrupts allocator metadata and the game dies at "
                        f"level load with no log line, no build failure and no "
                        f"test failure). The measured LOADING ceiling is "
                        f"{_LOADED_MAX_PCT} (the DEFAULT this build draws to is "
                        f"a lower {_DEFAULT_MAX_PCT}); {_LOADED_MAX_PCT}-"
                        f"{_CRASHED_MIN_PCT} is UNEXPLORED and may be walked "
                        f"one build at a time, but at or above "
                        f"{_CRASHED_MIN_PCT} is measured to crash, so it is "
                        f"refused rather than clamped")
                args.pool_occupancy = pct

    args.func(args)


if __name__ == "__main__":
    main()
