"""
End-to-end ISO patching for the Maximo: Ghosts to Glory randomizer.

Workflow:
  1. Take a vanilla Maximo ISO as input.
  2. Optionally make a backup copy.
  3. Extract the files we'll randomize (PSX maps + SLUS executable).
  4. Run the randomizer on those files.
  5. Write the randomized output back into the disc image, patching files at
     their existing location where they fit and relocating them where they
     do not.

Patched files may grow. `write_patched_assets` handles three cases per file:

  1. Same size or smaller — written in place at the file's original LBA.
  2. Grew but still fits the on-disc sector slot (original length rounded up
     to the next 2048-byte sector) — written in place, and the ISO 9660
     directory record's data length is updated so the engine reads the new
     length.
  3. Grew beyond the sector slot — appended at the end of the image via
     `iso.append_file`, with the directory record's LBA AND data length
     updated to point at the new location.

Cases 2 and 3 are collected into the `grown` and `relocated` lists, which
`patch_iso` logs. The output image therefore grows relative to the input:
cross-world mode routinely relocates BEF files, and PSX maps grow with their
records section.
"""
from __future__ import annotations
import argparse
import random
import shutil
import sys
import tempfile
from pathlib import Path

from .iso import IsoFile, find_maximo_files, SLUS_FILENAME, ASSET_FILE_EXTENSIONS
from .elf_patch import (resolve_shield_cost, SHIELD_COST_PRESETS,
                        resolve_sword_cost, SWORD_COST_MULTIPLIERS,
                        resolve_life_cost, LIFE_COST_PRESETS,
                        resolve_effect_duration, EFFECT_DURATION_PRESET_LABELS,
                        KOIN_FEE_VANILLA, KOIN_FEE_MIN, KOIN_FEE_MAX,
                        resolve_level_timer, LEVEL_TIMER_PRESET_NAMES,
                        resolve_object_budget, OBJECT_BUDGET_PRESETS,
                        OBJECT_BUDGET_VANILLA, OBJECT_BUDGET_MIN,
                        OBJECT_BUDGET_MAX, OBJECT_BUDGET_SAFE_MAX,
                        resolve_resource_pool, RESOURCE_POOL_PRESETS,
                        RESOURCE_POOL_VANILLA, RESOURCE_POOL_MIN,
                        RESOURCE_POOL_MAX, RESOURCE_POOL_RAISED,
                        RESOURCE_POOL_LOADED_MAX, RESOURCE_POOL_CRASHED_MIN,
                        RESOURCE_POOL_HIGH)
from .roster_selection import (resolve_max_pct as _resolve_max_pct,
                               LOADED_MAX_PCT as _LOADED_MAX_PCT,
                               DEFAULT_MAX_PCT as _DEFAULT_MAX_PCT,
                               CRASHED_MIN_PCT as _CRASHED_MIN_PCT)


# DEPRECATED / UNUSED -- do not read this to decide what round-trips.
# Verified unused: nothing in this module (or elsewhere in the package) reads
# ASSET_EXTENSIONS. write_patched_assets() iterates `file_map`, which comes from
# extract_assets() -> iso.find_maximo_files(), and discovery is driven solely by
# iso.ASSET_FILE_EXTENSIONS -- THE LIVE CONSTANT. Note this stale copy is also
# missing ".TEX". It is now an alias of the live constant so the two lists can
# never disagree again; prefer importing iso.ASSET_FILE_EXTENSIONS directly.
ASSET_EXTENSIONS = ASSET_FILE_EXTENSIONS


def extract_assets(iso_path: Path, out_dir: Path,
                   log=None) -> dict[str, tuple[int, int]]:
    """Extract every PSX and BEF file (plus SLUS_200.17) from the ISO into
    `out_dir`. Returns the LBA/length map so we can write back in-place later.

    `log`: optional callable(str), same mechanism `patch_iso` uses for progress
    lines. When supplied it receives iso.summarize_maximo_files()'s one-line
    disc summary, which names whether GLOBAL.PRT / GLOBAL.PRS were found. That
    line is the only place a user can see why cross-world porting produced
    nothing, so it must reach the run log.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    file_map: dict[str, tuple[int, int]] = {}
    with IsoFile(iso_path, writable=False) as iso:
        files = find_maximo_files(iso, log=log)
        for upper_name, (lba, length) in files.items():
            data = iso.read_file(lba, length)
            (out_dir / upper_name).write_bytes(data)
            file_map[upper_name] = (lba, length)
    return file_map


def write_patched_assets(
    iso_path: Path,
    patched_dir: Path,
    file_map: dict[str, tuple[int, int]],
    progress=None,
) -> dict:
    """Write randomized files back into the ISO in-place.

    `file_map`: {uppercase filename: (lba, length)} from `extract_assets`.
    `patched_dir`: folder containing patched files (typically the randomizer's
                   output folder). Files NOT in patched_dir are left as-is.
    `progress`: optional callable receiving (filename, written_bytes, total).

    Files that grew beyond their sector slot (e.g. BEFs with injected enemy
    assets) are relocated to the end of the disc and their directory entries
    updated to point to the new LBA.
    """
    SECTOR = 2048  # ISO 9660 user-data sector size; matches USER_DATA_SIZE
    written = 0
    skipped = []
    grown: list[tuple[str, int, int]] = []  # (name, old_len, new_len)
    relocated: list[tuple[str, int, int, int]] = []  # (name, old_lba, new_lba, new_len)
    with IsoFile(iso_path, writable=True) as iso:
        # Pre-compute every file's parent directory location so we can
        # update the data-length field of the directory record when a
        # patched file outgrew its original length.
        parents = iso.file_parents()
        for upper_name, (lba, length) in file_map.items():
            patched = patched_dir / upper_name
            if not patched.exists():
                # Try lower-cased / mixed-case match
                for candidate in patched_dir.iterdir():
                    if candidate.name.upper() == upper_name:
                        patched = candidate
                        break
            if not patched.exists():
                skipped.append(upper_name)
                continue
            data = patched.read_bytes()
            # The on-disc slot for each file is `length` rounded UP to the
            # next sector boundary. iso.write_file accepts data up to that
            # padded size and zero-fills the tail.
            slack = (-length) % SECTOR
            slot_size = length + slack
            if len(data) > slot_size:
                # File grew beyond its sector slot — relocate to end of disc.
                parent = parents.get(lba)
                if parent is None:
                    raise RuntimeError(
                        f"{upper_name}: grew beyond sector slot but parent "
                        f"directory unknown; cannot relocate."
                    )
                new_lba = iso.append_file(data)
                ok = iso.update_directory_entry_lba_and_length(
                    parent[0], parent[1], lba, new_lba, len(data),
                )
                if not ok:
                    raise RuntimeError(
                        f"{upper_name}: could not update directory record "
                        f"for relocation (parent_dir_lba=0x{parent[0]:X})"
                    )
                relocated.append((upper_name, lba, new_lba, len(data)))
            else:
                iso.write_file(lba, data, length)
                # If the file grew (within slack), patch its directory record so
                # ISO 9660 reports the new length and the engine reads the
                # appended bytes. Same-size writes leave the directory untouched.
                if len(data) > length:
                    parent = parents.get(lba)
                    if parent is not None:
                        ok = iso.update_directory_entry_length(
                            parent[0], parent[1], lba, len(data),
                        )
                        if not ok:
                            raise RuntimeError(
                                f"{upper_name}: could not update directory "
                                f"record (parent_dir_lba=0x{parent[0]:X})"
                            )
                        grown.append((upper_name, length, len(data)))
                    else:
                        raise RuntimeError(
                            f"{upper_name}: grew but parent directory unknown; "
                            f"cannot update ISO 9660 length field."
                        )
            written += 1
            if progress:
                progress(upper_name, written, len(file_map))
    return {"written": written, "skipped": skipped, "grown": grown,
            "relocated": relocated, "total": len(file_map)}


def patch_iso(
    iso_path: Path | str,
    seed: int | None = None,
    *,
    items: bool = True,
    chests: bool | None = None,
    skills: bool | None = None,
    columns: bool | None = None,
    spawn_location: bool = False,
    exit_location: bool = False,
    swap_spawn_with_exit: bool = False,
    gen_tier: bool = True,
    gate_mode: str | None = None,
    damage_taken: str = "normal",
    damage_dealt: str = "normal",
    shield_cost: int | None = None,
    life_cost: int | str | None = None,
    perma_death: bool = False,
    sword_cost: str | int | None = None,
    powerup_duration: str | int | None = None,
    wizard_duration: str | int | None = None,
    frozen_duration: str | int | None = None,
    koin_fee: int | None = None,
    disable_collision: bool = False,
    level_timer: str | None = None,
    timer_display: bool = True,
    widescreen: bool = False,
    #: DEFAULTS TO `more`, NOT `vanilla`: the raised on-screen object budget is a
    #: standard part of a build at the user's direction, so an omitted keyword
    #: RAISES it. Pass "vanilla" explicitly to leave the executable untouched.
    object_budget: str | int | None = "more",
    resource_pool: str | int | None = None,
    pool_occupancy: str | float | None = None,
    start_gold: int | None = None,
    start_lives: int | None = None,
    start_keys: int | None = None,
    start_deathcoins: int | None = None,
    start_souls: int | None = None,
    start_armor: int | None = None,
    start_shields: int | None = None,
    start_boxers: int | None = None,
    sword_enchant: int | None = None,
    elemental_shield: int | None = None,
    start_skills=None,
    randomize_start_inv: bool = False,
    randomize_levels: bool = False,
    randomize_levels_cross: bool = False,
    harder_mode: bool = False,
    preserve_chests: bool = False,
    preserve_iron_keys: bool = False,
    spawn_config_path: str | None = None,
    enemies: bool = True,
    all_enemies: bool = False,
    cross_world: bool = False,
    cross_world_structures: bool = False,
    worlds: set[str] | None = None,
    duplicate_bosses: bool = False,
    #: EXPERIMENTAL, default OFF. Cross-world ENEMY arrivals into the four `_B`
    #: boss arenas (see `boss_arena`). Independent of `duplicate_bosses` and
    #: `inject_boss`; costs into an arena are PREDICTED and the arena budget is
    #: UNVERIFIED on console.
    boss_arena_arrivals: bool = False,
    #: How many arrivals each arena receives: `easy` 3 / `normal` 6 / `hard` 9.
    #: `None` resolves to `boss_arena.ARENA_DIFFICULTY_DEFAULT` downstream.
    boss_arena_difficulty: str | None = None,
    inject_boss: bool = False,
    exclude_dark_knight: bool = False,
    boss_clones: int = 1,
    boss_clones_grave: int | None = None,
    boss_clones_swamp: int | None = None,
    boss_clones_ice: int | None = None,
    boss_clones_under: int | None = None,
    boss_clones_castle: int | None = None,
    output_iso: Path | str | None = None,
    backup: bool = True,
    workdir: Path | None = None,
    log=print,
) -> dict:
    """Run the full disc-image randomization pipeline.

    Args:
      iso_path:    Path to the Maximo disc image. Accepts .iso, .bin, or .cue.
                   For .cue, the referenced .bin is used.
      seed:        RNG seed (None = random).
      items:       Randomize items / chests / abilities / level columns.
      enemies:     Randomize walking-melee enemies.
      all_enemies: CHAOS mode — turn every item / structure into an enemy.
                   Overrides `items` / `enemies`. Gold Keys preserved.
      cross_world: Inject enemy assets across worlds so ANY enemy can appear
                   in ANY world. Modifies BEF files (grows them ~30-48KB each).
      cross_world_structures:
                   EXPERIMENTAL, default False. Also port STRUCTURES
                   (smashables, props, decorations) across worlds. Ported
                   structures are COSMETIC only: progression and puzzle props
                   are excluded, and structures are admitted after enemies out
                   of the same budget so they never displace one. Requires
                   `cross_world`. With no explicit per-route selection saved in
                   the spawn config, every SELECTABLE route is a candidate and
                   the resource budget decides how many fit; a saved selection
                   narrows it to exactly what was ticked. Every route stays
                   CANDIDATE until observed in game.
      worlds:      Set of worlds to randomize (subset of grave/under/swamp/ice/castle).
                   None means all worlds.
      output_iso:  Optional path for the patched output. When provided:
                     - The source disc is copied to this location FIRST.
                     - All patches are applied to the COPY, leaving the source
                       byte-for-byte untouched.
                     - For .cue input, the companion .bin is copied beside the
                       output and the .cue is rewritten to point at it.
                   When omitted, the source disc is patched in place (the
                   classic behavior — backup is created if `backup=True`).
      backup:      Only used when `output_iso` is None. Copies the source to
                   <name>.backup before in-place patching.
      workdir:     Folder to extract / write patched assets into. None = temp.
      log:         Callable for progress lines (default: print).

    Returns a dict with seed, files_written, skipped, paths.
    """
    iso_path = Path(iso_path)
    if not iso_path.exists():
        raise FileNotFoundError(iso_path)

    # Resolve the actual data file we will patch (the BIN, when given a CUE).
    is_cue = iso_path.suffix.lower() == ".cue"
    if is_cue:
        from .iso import parse_cue
        source_bin = parse_cue(iso_path)
        log(f"CUE sheet references: {source_bin.name}")
    else:
        source_bin = iso_path

    # Determine where the patches actually land. Two modes:
    #   1. output_iso provided -> copy source to that path, patch the copy
    #   2. output_iso missing  -> patch source in place (with optional backup)
    backup_path = None
    if output_iso is not None:
        target_path = _prepare_output_copy(iso_path, source_bin, Path(output_iso),
                                           is_cue=is_cue, log=log)
    else:
        target_path = source_bin
        if backup:
            backup_path = target_path.with_suffix(target_path.suffix + ".backup")
            if not backup_path.exists():
                log(f"Creating backup: {backup_path.name}")
                shutil.copy2(target_path, backup_path)
            else:
                log(f"Backup already exists: {backup_path.name} (skipping)")

    if seed is None:
        seed = random.randint(1, 999999)

    # Set up working directories
    cleanup_workdir = False
    if workdir is None:
        workdir = Path(tempfile.mkdtemp(prefix="maximo_rand_"))
        cleanup_workdir = True
    else:
        workdir = Path(workdir)
        workdir.mkdir(parents=True, exist_ok=True)
    extract_dir = workdir / "extracted"
    output_dir = workdir / "patched"

    try:
        log(f"Extracting Maximo assets from {target_path.name}...")
        # Indent the disc summary to match the other sub-step lines below.
        file_map = extract_assets(target_path, extract_dir,
                                  log=lambda line: log(f"  {line}"))
        log(f"  Extracted {len(file_map)} files to {extract_dir}")

        # Cross-world mode: BEF injection is handled inside cmd_randomize.
        # inject_cross_world_enemies() in bef.py rebuilds each world's BEF
        # with foreign enemy blobs remapped to GLOBAL resource loading,
        # then writes them back to extract_dir. write_patched_assets() below
        # pushes the grown BEF files to the disc image automatically.
        if cross_world:
            log(f"Cross-world mode: BEF files will be expanded with foreign enemy assets...")

        from .cli import cmd_randomize, ALL_WORLDS

        if worlds is None:
            worlds_arg = "all"
        else:
            worlds_arg = ",".join(sorted(worlds))

        class _Args:
            pass
        args = _Args()
        args.psx_folder = str(extract_dir)
        args.output = str(output_dir)
        args.seed = seed
        args.items = items
        args.chests = chests
        args.skills = skills
        args.columns = columns
        args.spawn_location = spawn_location
        args.exit_location = exit_location
        args.swap_spawn_with_exit = swap_spawn_with_exit
        args.gen_tier = gen_tier
        args.gate_mode = gate_mode
        args.damage_taken = damage_taken
        args.damage_dealt = damage_dealt
        # Stays an INTEGER on the wire; the resolver only makes the parameter
        # tolerant of a caller that hands us a preset label like "light".
        args.shield_cost = resolve_shield_cost(shield_cost)
        # An INTEGER count of lives per death on the wire; the resolver just makes
        # the parameter tolerant of a preset label like "double". None, an
        # unrecognised label, or 1 ("vanilla") means no patch.
        args.life_cost = resolve_life_cost(life_cost)
        # Plain flag: 0 lives, 0 death coins, first death ends the run.
        args.perma_death = perma_death
        # Same deal: an INTEGER multiplier on the wire, the resolver just makes
        # the parameter tolerant of a preset label like "heavy".
        args.sword_cost = resolve_sword_cost(sword_cost)
        # Three INDEPENDENT status-effect duration multipliers on the wire; the
        # resolver just makes each parameter tolerant of a preset label like
        # "easier". None, an unrecognised label, or 1 ("vanilla") means no patch.
        # powerup = Golden Armor + Mask of Sorrow (coupled), wizard = Baby +
        # Old Man (coupled), frozen = the object ice timer. The site id is
        # passed because "easier"/"harder" are INVERTED between the harmful
        # wizard/frozen timers and the beneficial power-up timer.
        args.powerup_duration = resolve_effect_duration("powerup", powerup_duration)
        args.wizard_duration = resolve_effect_duration("wizard", wizard_duration)
        args.frozen_duration = resolve_effect_duration("frozen", frozen_duration)
        # ONE integer koin fee driving BOTH the save price and the hub-travel
        # price (they share one on-screen label). None or 100 means no patch.
        args.koin_fee = koin_fee
        # DEBUG/RESEARCH ONLY: clears bDoCollision — the player falls through
        # the map. Not a playable mode.
        args.disable_collision = disable_collision
        # EXPERIMENTAL: level time limit difficulty preset (hard/medium/easy),
        # each with PER-WORLD durations. None/"off" = feature off; the clock
        # re-arms per attempt (every level entry AND respawn). Run through the
        # shared resolver so this entry point accepts the same spellings the CLI
        # and the GUI do.
        args.level_timer = resolve_level_timer(level_timer)
        # Companion toggle: draw the remaining seconds on screen. Only
        # meaningful when level_timer is set.
        args.timer_display = timer_display
        # Presentation only: render at 16:9 instead of 4:3 (static port of the
        # PCSX2 widescreen cheat).
        args.widescreen = widescreen
        # EXPERIMENTAL: the script VM's per-type on-screen object budget (WEIGHT
        # units, not object counts). Run through the shared resolver so this
        # entry point accepts the same spellings the CLI and the GUI do; None or
        # "vanilla" (100) = no patch.
        args.object_budget = resolve_object_budget(object_budget)
        # The engine's RESOURCE memory pool (pool[2]). Two lui halves in
        # MemoryManager::__ct, written together: the pool size and pool[0]'s
        # matching reserve. None or "vanilla" (0x640000) = no patch. Left None
        # here means cmd_randomize's cross-world default still applies.
        args.resource_pool = resolve_resource_pool(resource_pool)
        # EXPERIMENTAL: the SHARE of that pool the cross-world draw may occupy.
        # Set EXPLICITLY (never left to a getattr default) so a GUI or library
        # caller cannot end up with a silently dead option. None = the DEFAULT
        # 90.0% (`DEFAULT_MAX_PCT`), which is deliberately BELOW the observed-
        # loading 93.0% (`LOADED_MAX_PCT`); `resolve_max_pct` refuses 93.5% and
        # above, the crash floor, rather than clamping.
        args.pool_occupancy = _resolve_max_pct(pool_occupancy)
        args.start_gold = start_gold
        args.start_lives = start_lives
        args.start_keys = start_keys
        args.start_deathcoins = start_deathcoins
        args.start_souls = start_souls
        args.start_armor = start_armor
        args.start_shields = start_shields
        args.start_boxers = start_boxers
        args.sword_enchant = sword_enchant
        args.elemental_shield = elemental_shield
        args.start_skills = start_skills
        args.randomize_start_inv = randomize_start_inv
        args.randomize_levels = randomize_levels
        args.randomize_levels_cross = randomize_levels_cross
        args.harder_mode = harder_mode
        args.preserve_chests = preserve_chests
        args.preserve_iron_keys = preserve_iron_keys
        args.spawn_config_path = spawn_config_path
        args.no_enemies = not enemies
        args.all_enemies = all_enemies
        args.worlds = worlds_arg
        args.duplicate_bosses = duplicate_bosses
        # EXPLICIT: this bag has no argparse defaults either.
        args.inject_boss = inject_boss
        args.exclude_dark_knight = exclude_dark_knight
        args.boss_clones = boss_clones
        args.boss_clones_grave = boss_clones_grave
        args.boss_clones_swamp = boss_clones_swamp
        args.boss_clones_ice = boss_clones_ice
        args.boss_clones_under = boss_clones_under
        args.boss_clones_castle = boss_clones_castle
        args.cross_world = cross_world
        # EXPLICIT, like every other flag here: `cmd_randomize` reads this off
        # `args`, and omitting it would leave the feature permanently
        # unreachable from the ISO path.
        args.cross_world_structures = bool(cross_world_structures)
        # EXPLICIT for the same reason: this bag has no argparse defaults, so an
        # omitted keyword is a feature that silently never runs.
        args.boss_arena_arrivals = bool(boss_arena_arrivals)
        args.boss_arena_difficulty = boss_arena_difficulty

        mode_str = "ALL-ENEMIES chaos" if all_enemies else f"items={items}, enemies={enemies}"
        log(f"Running randomizer (seed={seed}, worlds={worlds_arg}, {mode_str})...")
        cmd_randomize(args)

        log(f"Patching disc image...")
        def _progress(name, n, total):
            log(f"  [{n}/{total}] {name}")
        result = write_patched_assets(target_path, output_dir, file_map, progress=_progress)
        log(f"Done. Wrote {result['written']} of {result['total']} files into the disc.")
        if result["skipped"]:
            log(f"  Skipped (no patched version available): {len(result['skipped'])}")
        if result.get("grown"):
            log(f"  Files grown into slack: {len(result['grown'])}")
            for name, old_len, new_len in result["grown"]:
                log(f"    {name}: {old_len:,} -> {new_len:,} bytes "
                    f"(+{new_len - old_len}b)")
        if result.get("relocated"):
            log(f"  Files relocated (grew beyond sector slot): {len(result['relocated'])}")
            for name, old_lba, new_lba, new_len in result["relocated"]:
                log(f"    {name}: LBA 0x{old_lba:X} -> 0x{new_lba:X} "
                    f"({new_len:,} bytes)")
        return {
            "seed": seed,
            "iso": str(target_path),
            "input": str(iso_path),
            "output_iso": str(output_iso) if output_iso else None,
            "backup": str(backup_path) if backup_path else None,
            "worlds": worlds_arg,
            **result,
        }
    finally:
        if cleanup_workdir:
            try:
                shutil.rmtree(workdir, ignore_errors=True)
            except Exception:
                pass


def _prepare_output_copy(
    input_path: Path,
    source_bin: Path,
    output_path: Path,
    is_cue: bool,
    log,
) -> Path:
    """Copy the source disc image to a new output location and return the
    path to the binary file the patcher should operate on.

    Three forms of `output_path`:
      1. A directory  -> copy source files into that dir, keeping the names.
      2. A .cue path  -> copy the BIN beside it (auto-named) and rewrite the CUE.
      3. A .bin/.iso  -> copy the source binary directly to that path.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # CASE 1: the user pointed at an existing directory (or one with no suffix).
    if output_path.is_dir() or (not output_path.suffix and not output_path.exists()):
        output_path.mkdir(parents=True, exist_ok=True)
        if is_cue:
            # Copy the .cue and the .bin both, preserving the original names so
            # the CUE's FILE reference still resolves.
            cue_dst = output_path / input_path.name
            bin_dst = output_path / source_bin.name
            log(f"Copying source disc image to: {output_path}")
            log(f"  {input_path.name} -> {cue_dst.name}")
            log(f"  {source_bin.name} -> {bin_dst.name}")
            shutil.copy2(input_path, cue_dst)
            shutil.copy2(source_bin, bin_dst)
            return bin_dst
        else:
            target = output_path / source_bin.name
            log(f"Copying source disc image to: {target}")
            shutil.copy2(source_bin, target)
            return target

    # CASE 2: explicit .cue output path.
    if output_path.suffix.lower() == ".cue":
        if not is_cue:
            raise ValueError(
                "Output path is a .cue but input is a single binary. "
                "Provide an .iso or .bin output path instead."
            )
        bin_dst = output_path.with_suffix(source_bin.suffix)
        log(f"Copying CUE+BIN to: {output_path.parent}")
        log(f"  {input_path.name} -> {output_path.name}")
        log(f"  {source_bin.name} -> {bin_dst.name}")
        # Rewrite the CUE's FILE line to point at the new BIN basename.
        cue_text = input_path.read_text(errors="replace")
        new_cue = _rewrite_cue_file(cue_text, bin_dst.name)
        output_path.write_text(new_cue)
        shutil.copy2(source_bin, bin_dst)
        return bin_dst

    # CASE 3: explicit .iso / .bin output path.
    log(f"Copying source disc image to: {output_path}")
    shutil.copy2(source_bin, output_path)
    return output_path


def _rewrite_cue_file(cue_text: str, new_bin_name: str) -> str:
    """Replace the BIN filename inside the FILE directive of a CUE sheet."""
    import re
    # FILE "name.bin" BINARY  (with quotes) — most common form
    new_text, n = re.subn(
        r'(FILE\s+")[^"]+(")',
        rf'\g<1>{new_bin_name}\g<2>',
        cue_text, count=1, flags=re.IGNORECASE,
    )
    if n:
        return new_text
    # FILE name.bin BINARY  (no quotes)
    new_text, n = re.subn(
        r'(FILE\s+)\S+',
        rf'\g<1>{new_bin_name}',
        cue_text, count=1, flags=re.IGNORECASE,
    )
    return new_text if n else cue_text


def main():
    p = argparse.ArgumentParser(
        prog="randomizer.iso_patcher",
        description="Patch a Maximo: Ghosts to Glory disc image with a randomized seed.",
    )
    p.add_argument("iso", help="Path to the Maximo disc image (.iso/.bin/.cue)")
    p.add_argument("--seed", type=int, default=None, help="RNG seed (default: random)")
    p.add_argument("--no-items", action="store_true", help="Skip item randomization")
    p.add_argument("--no-gen-tier", action="store_false", dest="gen_tier",
                   default=True,
                   help="Disable monster-generator class-level randomization (on by default)")
    p.add_argument("--gate-mode", dest="gate_mode", default=None,
                   choices=["isolated", "pool"],
                   help="Gate randomizer mode: 'isolated' or 'pool' (default: off)")
    p.add_argument("--damage-taken", dest="damage_taken", default="normal",
                   choices=["0.25", "0.50", "normal", "2x", "4x"],
                   help="Scale damage Maximo takes (default normal)")
    p.add_argument("--damage-dealt", dest="damage_dealt", default="normal",
                   choices=["0.25", "0.50", "normal", "2x", "4x"],
                   help="Scale melee damage Maximo deals to enemies (default normal)")
    p.add_argument("--shield-cost", dest="shield_cost", default=None,
                   choices=list(SHIELD_COST_PRESETS),
                   metavar="{" + "/".join(SHIELD_COST_PRESETS) + "}",
                   help="Shield points spent per shield block: none (0), "
                        "vanilla (1, default — executable untouched), "
                        "light (2), medium (3), heavy (5). 'none' makes "
                        "shield use non-depleting.")
    p.add_argument("--life-cost", dest="life_cost", default=None,
                   choices=list(LIFE_COST_PRESETS),
                   metavar="{" + "/".join(LIFE_COST_PRESETS) + "}",
                   help="Lives one death spends: vanilla (1, default — "
                        "executable untouched), none (0), double (2), "
                        "triple (3), quadruple (4). 'none' means deaths no "
                        "longer cost a life. A new game starts with 4 lives, so "
                        "quadruple means the first death ends the run.")
    p.add_argument("--perma-death", dest="perma_death", action="store_true",
                   help="PERMA DEATH: start with 0 lives and 0 death coins, "
                        "neither can ever be gained, and the first death ends "
                        "the run permanently with no continue offered. "
                        "Overrides any starting lives / death coins set "
                        "elsewhere.")
    p.add_argument("--sword-cost", dest="sword_cost", default=None,
                   choices=list(SWORD_COST_MULTIPLIERS),
                   metavar="{" + "/".join(SWORD_COST_MULTIPLIERS) + "}",
                   help="Multiplier on the sword enchantment points each "
                        "special move spends: none (0x, free), vanilla "
                        "(1x, default — executable untouched), light (2x), "
                        "medium (3x), heavy (5x).")
    p.add_argument("--powerup-duration", dest="powerup_duration", default=None,
                   choices=list(EFFECT_DURATION_PRESET_LABELS),
                   metavar="{" + "/".join(EFFECT_DURATION_PRESET_LABELS) + "}",
                   help="Preset for the Golden Armor AND Mask of Sorrow "
                        "timer (vanilla 20.0 s): vanilla (default — "
                        "executable untouched), none (1 tick, gone on the first "
                        "frame), easier (2x, 40.0 s), harder (0.5x, 10.0 s). "
                        "This power-up is BENEFICIAL, so easier means LONGER. "
                        "Both power-ups share one timer constant, so they are "
                        "LOCKED TOGETHER and cannot be set separately.")
    p.add_argument("--wizard-duration", dest="wizard_duration", default=None,
                   choices=list(EFFECT_DURATION_PRESET_LABELS),
                   metavar="{" + "/".join(EFFECT_DURATION_PRESET_LABELS) + "}",
                   help="Preset for the wizard-form timer covering Baby AND "
                        "Old Man (vanilla 10.0 s): vanilla (default — "
                        "executable untouched), none (1 tick, gone on the first "
                        "frame), easier (0.5x, 5.0 s), harder (2x, 20.0 s). "
                        "These forms are HARMFUL, so easier means SHORTER. Both "
                        "forms share one timer constant, so they are LOCKED "
                        "TOGETHER and cannot be set separately.")
    p.add_argument("--frozen-duration", dest="frozen_duration", default=None,
                   choices=list(EFFECT_DURATION_PRESET_LABELS),
                   metavar="{" + "/".join(EFFECT_DURATION_PRESET_LABELS) + "}",
                   help="Preset for how long Frozen lasts (vanilla 5.0 s): "
                        "vanilla (default — executable untouched), none (1 "
                        "tick, gone on the first frame), easier (0.5x, 2.5 s), "
                        "harder (2x, 10.0 s). Frozen is HARMFUL, so easier "
                        "means SHORTER. There is no mash-to-escape, so the "
                        "timer is the whole duration.")
    # Deliberately NOT choices=range(0, 101): argparse would render a 101-item
    # help string. The range is validated explicitly after parse_args().
    p.add_argument("--koin-fee", dest="koin_fee", type=int, default=None,
                   metavar="N",
                   help=f"Koin cost to SAVE the game and to TRAVEL between hubs "
                        f"(vanilla {KOIN_FEE_VANILLA}, range {KOIN_FEE_MIN}-"
                        f"{KOIN_FEE_MAX}). 0 makes both free. Both share ONE "
                        f"value because the on-screen label is a single shared "
                        f"string. Values above {KOIN_FEE_MAX} are REFUSED: the "
                        f"save record's koin subtract is unclamped, so a higher "
                        f"fee can drive the saved koin count negative. Omit to "
                        f"keep vanilla.")
    p.add_argument("--disable-collision", dest="disable_collision",
                   action="store_true",
                   help="DEBUG/RESEARCH ONLY — clear the developers' own "
                        "bDoCollision byte so the player and camera stop "
                        "colliding with the world. NOT a playable mode: you "
                        "WILL FALL THROUGH THE MAP as soon as gravity applies, "
                        "retail has no fly/noclip movement to compensate, and a "
                        "soft-lock is likely. Fully reversible (one byte). "
                        "Screenshot / research tool only.")
    p.add_argument("--level-timer", dest="level_timer", default=None,
                   choices=["off", *LEVEL_TIMER_PRESET_NAMES],
                   help="EXPERIMENTAL — level time limit difficulty preset. "
                        "Each preset carries PER-WORLD durations (tightest in "
                        "Grave, loosest in Castle; 'hard' < 'medium' < 'easy'), "
                        "with the Dungeon of Despair given its own longer "
                        "limit. The death uses the game's own death path. The "
                        "clock resets on EVERY level entry — hubs, intros and "
                        "boss arenas included — and on respawn, so the limit is "
                        "PER ATTEMPT, and it may keep running while paused or "
                        "during cutscenes. 'off' (the default) leaves the "
                        "executable alone.")
    p.add_argument("--no-timer-display", action="store_true",
                   dest="no_timer_display",
                   help="Turn OFF the on-screen countdown while keeping the "
                        "level time limit's death. Only meaningful together "
                        "with --level-timer.")
    p.add_argument("--widescreen", dest="widescreen", action="store_true",
                   help="render the game at 16:9 instead of 4:3 (static port "
                        "of the PCSX2 widescreen cheat: horizontal scale 1.0 "
                        "-> 0.75 and render aspect 1.4 -> 1.866666)")
    # DEFAULT `more`, matching `randomizer.cli` and the GUI: one name, one
    # default, one feature. `vanilla` leaves the executable untouched.
    p.add_argument("--object-budget", dest="object_budget", default="more",
                   metavar="{" + "/".join(OBJECT_BUDGET_PRESETS) + "/N}",
                   help=f"EXPERIMENTAL — raise the script VM's per-type "
                        f"on-screen object budget so more of a level's own "
                        f"objects and enemies can be active at once (vanilla "
                        f"{OBJECT_BUDGET_VANILLA} = executable untouched, "
                        f"'more' = {OBJECT_BUDGET_MAX}, or a raw integer "
                        f"{OBJECT_BUDGET_MIN}-{OBJECT_BUDGET_MAX}). These are "
                        f"WEIGHT budgets, NOT object counts. ENEMIES come from "
                        f"the PER-LEVEL LevelTable limit, which the engine "
                        f"re-applies at every level load, so it is patched "
                        f"together with the constructor default and scaled "
                        f"proportionally. Above {OBJECT_BUDGET_SAFE_MAX} is "
                        f"explicitly experimental and may cost frame rate; "
                        f"above {OBJECT_BUDGET_MAX} is REFUSED.")
    p.add_argument("--resource-pool", dest="resource_pool", default=None,
                   metavar="{" + "/".join(RESOURCE_POOL_PRESETS) + "/SIZE}",
                   help=f"raise the engine's RESOURCE memory pool (vanilla "
                        f"0x{RESOURCE_POOL_VANILLA:X} = executable untouched, "
                        f"'raised' = 0x{RESOURCE_POOL_RAISED:X} (THE DEFAULT for "
                        f"cross-world runs: it has loaded and played "
                        f"CONSISTENTLY across many builds), 'unreliable' "
                        f"(historical spelling 'loaded-max') = "
                        f"0x{RESOURCE_POOL_LOADED_MAX:X} (SELECTABLE if you want "
                        f"to retry it, NEVER a default: it LOADED and played "
                        f"once and later HUNG with infinite loading on another "
                        f"build), "
                        f"or ANY SIZE "
                        f"0x{RESOURCE_POOL_MIN:X}-"
                        f"0x{RESOURCE_POOL_CRASHED_MIN - 0x10000:X} in 64 KiB "
                        f"steps -- a leading "
                        f"0x is read as HEX, a bare number as DECIMAL). WHAT IS "
                        f"KNOWN, from console builds: "
                        f"0x{RESOURCE_POOL_RAISED:X} LOADED and played, "
                        f"consistently; 0x{RESOURCE_POOL_LOADED_MAX:X} LOADED "
                        f"ONCE and HUNG ONCE, so it is UNRELIABLE; "
                        f"0x{RESOURCE_POOL_CRASHED_MIN:X} and "
                        f"0x{RESOURCE_POOL_HIGH:X} both HUNG ON MAP LOAD and "
                        f"have NEVER loaded, so "
                        f"0x{RESOURCE_POOL_CRASHED_MIN:X} and above is REFUSED "
                        f"and the old 'high' preset was REMOVED. "
                        f"0x810000-0x870000 is still UNEXPLORED, and since the "
                        f"hang boundary is NOT a clean step one loading build "
                        f"there would prove nothing. "
                        f"SUSPECTED, NOT MEASURED, for the "
                        f"hang: the pools tile a fixed arena with no slack and "
                        f"no bounds check, so growing RESOURCE starves MAIN. "
                        f"PAYOFF: at the 93% ceiling each 64 KiB of pool buys "
                        f"~59 KiB of draw budget, and enemy routes cost 225 KB "
                        f"(Axe_Guard) to 841 KB (Plant_Monster), so ~+512 KB of "
                        f"pool is worth roughly ONE to TWO extra arrivals per "
                        f"world. Every byte given to "
                        f"RESOURCE is taken from the MAIN pool (~24 MB), and "
                        f"pool[0]'s reserve is raised by the same amount in the "
                        f"same edit so the three pools still tile the arena. "
                        f"Only the `lui` halves are editable, so the low 16 "
                        f"bits of the size must be 0. --cross-world turns this "
                        f"on by default ('raised', with or without "
                        f"--cross-world-structures); pass 'vanilla' to keep it "
                        f"off.")
    p.add_argument("--pool-occupancy", dest="pool_occupancy", default=None,
                   metavar="{measured/PCT}",
                   help=f"EXPERIMENTAL. The share of the RESOURCE pool the "
                        f"cross-world draw budget may occupy, as a percentage. "
                        f"THREE DIFFERENT NUMBERS, not to be conflated: the "
                        f"DEFAULT is {_DEFAULT_MAX_PCT} ('measured', 'default' "
                        f"and blank all mean this); the highest occupancy "
                        f"MEASURED TO LOAD is {_LOADED_MAX_PCT}; and "
                        f"{_CRASHED_MIN_PCT} and above is REFUSED, not clamped. "
                        f"The measurement is EMPIRICAL: the band was walked one "
                        f"console build at a time, and 95 CRASHED, 94 CRASHED, "
                        f"93.5 CRASHED, 93 LOADED and played, so "
                        f"{_LOADED_MAX_PCT} is the highest OBSERVED TO LOAD and "
                        f"{_CRASHED_MIN_PCT} the lowest OBSERVED TO CRASH. The "
                        f"default sits DELIBERATELY BELOW the measurement, at "
                        f"the user's request for testing; lowering is always "
                        f"safe because it can only ship a smaller lineup, and "
                        f"anything up to (not including) {_CRASHED_MIN_PCT} -- "
                        f"{_LOADED_MAX_PCT} included -- is still selectable "
                        f"here per build. At the default 0x800000 pool the "
                        f"default budgets 7,549,747 B (0.90 * 8,388,608), which "
                        f"is 251,658 B LESS than the 7,801,405 B at "
                        f"{_LOADED_MAX_PCT}, i.e. roughly ONE FEWER arrival "
                        f"where the budget rather than the lineup cap binds, "
                        f"since enemy routes cost 225 KB to 840 KB. Pool "
                        f"exhaustion is SILENT (MemoryPool::Allocate falls "
                        f"through and returns sentinel_block + 16, the caller "
                        f"corrupts allocator metadata and the game dies at "
                        f"level load with no log line and no test failure): a "
                        f"build that LOADS at a higher figure is evidence worth "
                        f"recording, a build that hangs at level load means "
                        f"drop back.")
    p.add_argument("--start-souls", dest="start_souls", type=int, default=None,
                   help="Starting souls / HUD spirit for a new game (0-999). "
                        "Omit to keep vanilla (0).")
    p.add_argument("--start-armor", dest="start_armor", type=int, default=None,
                   help="Starting armor segments for a new game (0-4). "
                        "Omit to keep vanilla (2).")
    p.add_argument("--start-shields", dest="start_shields", type=int,
                   default=None,
                   help="Starting shield count for a new game (0-4). Shield "
                        "points are set to 5x this to match. Omit to keep "
                        "vanilla (2).")
    p.add_argument("--start-boxers", dest="start_boxers", type=int,
                   default=None,
                   help="Which pair of boxers Maximo wears (0-3). Only visible "
                        "while armor is 0 or 1. Omit to keep vanilla (3).")
    p.add_argument("--harder-mode", action="store_true", dest="harder_mode",
                   help="Enable HARDER MODE curated difficulty profile.")
    p.add_argument("--preserve-chests", action="store_true", dest="preserve_chests",
                   help="Keep vanilla chests in place but randomize their contents.")
    p.add_argument("--preserve-iron-keys", action="store_true", dest="preserve_iron_keys",
                   help="Keep Iron Keys (Skeleton_Keys) in their vanilla positions "
                        "and don't add new ones — key count and placement stay vanilla.")
    p.add_argument("--spawn-location", action="store_true", dest="spawn_location",
                   help="Randomize Maximo's spawn position (moved to a random entity location)")
    p.add_argument("--randomize-exit-location", action="store_true", dest="exit_location",
                   help="Randomize the level-exit location (the End_Of_Sublevel_Column "
                        "trades positions with a random designer-placed structure)")
    p.add_argument("--swap-spawn-with-exit", action="store_true", dest="swap_spawn_with_exit",
                   help="Swap Maximo's level-start with the End_Of_Sublevel_Column position "
                        "(two-way). Overrides --spawn-location and --randomize-exit-location.")
    p.add_argument("--spawn-config", dest="spawn_config_path", default=None,
                   help="Path to a spawn-rate JSON config (per-tag/per-world weights + "
                        "enable flags). Omit to use the stock spawn rates.")
    p.add_argument("--no-enemies", action="store_true", help="Skip enemy randomization")
    p.add_argument("--all-enemies", action="store_true",
                   help="CHAOS mode: turn every item/structure into an enemy "
                        "(Gold Keys preserved, enemies drop keys).")
    p.add_argument("--cross-world", action="store_true",
                   help="Inject enemy assets across worlds so ANY enemy type "
                        "can appear in ANY world. Expands BEF files with "
                        "assets from other worlds (~30-50KB growth per BEF). "
                        "Enables a universal enemy pool for all worlds.")
    # THE SAME SWITCH as `randomizer.cli`'s `--boss-arena-arrivals`, spelled
    # identically so there is one name for one feature.
    p.add_argument("--boss-arena-arrivals", dest="boss_arena_arrivals",
                   action="store_true", default=False,
                   help="EXPERIMENTAL, default OFF. Let cross-world ENEMY "
                        "arrivals reach the four `_B` boss arenas (GRAVE_B / "
                        "SWAMP_B / ICE_B / UNDER_B; castle excluded). Victims "
                        "are ONLY the proven-decorative arena records; costs "
                        "into an arena are PREDICTED and the arena budget is "
                        "UNVERIFIED on console. Independent of "
                        "--duplicate-bosses and --inject-boss.")
    # THE SAME OPTION as `randomizer.cli`'s `--boss-arena-difficulty`, spelled
    # identically so there is one name for one feature.
    p.add_argument("--boss-arena-difficulty", dest="boss_arena_difficulty",
                   choices=("easy", "normal", "hard"), default=None,
                   help="How many cross-world enemies SPAWN in each boss arena "
                        "with --boss-arena-arrivals: easy=3, normal=6, hard=9 "
                        "per arena (default normal). Counts ENEMIES, NOT TYPES: "
                        "3 distinct types are always ported whatever the "
                        "setting, and higher counts reuse them across the "
                        "arena's decorative slots for no archive-byte cost. "
                        "CHOSEN FIGURES, NOT MEASURED LIMITS -- GRAVE has only 4 "
                        "victim slots and SWAMP 6, so 'hard' cannot be reached "
                        "in those two and the shortfall is logged.")
    # THE SAME SWITCH as `randomizer.cli`'s `--cross-world-structures`, spelled
    # identically so there is one name for one feature; this is the disc-image
    # entry point's spelling of it, not a second option.
    p.add_argument("--cross-world-structures", dest="cross_world_structures",
                   action="store_true", default=False,
                   help="EXPERIMENTAL. Also port STRUCTURES (smashables, props, "
                        "decorations) across worlds. Ported structures are "
                        "COSMETIC ONLY: progression and puzzle props are "
                        "EXCLUDED, and structures are admitted AFTER enemies "
                        "out of the same resource budget so they can never "
                        "displace an arriving enemy. INDEPENDENT of "
                        "--cross-world: either switch works on its own, and "
                        "with cross-world enemies off the structure pass gets "
                        "the WHOLE draw budget with nothing reserved. When BOTH "
                        "are on they share that budget, so this RESERVES A "
                        "SLICE of it (300,000 B) by lowering the ENEMY admission "
                        "ceiling by the same amount, costing roughly one "
                        "foreign enemy arrival per world. The RESOURCE pool "
                        "default stays 'raised' either way: the 'high' preset "
                        "was tried and OBSERVED TO CAUSE INFINITE LOADING ON "
                        "MAP LOAD. "
                        "OFF by default; turning it on with no per-route "
                        "selection saved in the spawn config makes EVERY "
                        "selectable route a candidate and lets the resource "
                        "budget decide how many fit, while a saved selection is "
                        "honoured exactly. Every route stays CANDIDATE until "
                        "you observe it in game.")
    p.add_argument(
        "--worlds", default="all",
        help="Comma-separated worlds: all, grave, under, swamp, ice, castle. Default: all",
    )
    p.add_argument("--duplicate-bosses", action="store_true",
                   help="EXPERIMENTAL: clone the boss in supported arenas "
                        "(Grave / GraveDigger and Swamp / BokorLaBas). May be unstable.")
    p.add_argument("--boss-clones", type=int, default=1,
                   help="Extra boss clones when --duplicate-bosses is set (default 1, "
                        "max 6; clamped per arena — GraveDigger caps at 4, BokorLaBas at 6).")
    p.add_argument("--no-dark-knight", action="store_true", dest="exclude_dark_knight",
                   help="Exclude Dark_Knight from randomizer pools (kept vanilla-only). "
                        "Dark_Knight can SOFT-LOCK a level if it lands on a progression "
                        "slot, so this is the safe option.")
    p.add_argument(
        "--output", "-o", default=None,
        help="Optional output path for the patched disc image. Can be a folder, "
             "an .iso/.bin path, or a .cue path (companion .bin is auto-named). "
             "When omitted, the source ISO is patched in place (with .backup).",
    )
    p.add_argument("--no-backup", action="store_true",
                   help="Don't create <name>.backup before in-place patching. "
                        "Has no effect when --output is used.")
    p.add_argument("--workdir", default=None,
                   help="Working directory for extracted/patched files (default: temp)")
    args = p.parse_args()

    # Explicit range check for --koin-fee (see the flag's comment: choices=
    # range(0, 101) would produce an unreadable 101-item help string).
    if args.koin_fee is not None \
            and not (KOIN_FEE_MIN <= args.koin_fee <= KOIN_FEE_MAX):
        p.error(f"--koin-fee must be {KOIN_FEE_MIN}-{KOIN_FEE_MAX} "
                f"(got {args.koin_fee}); above {KOIN_FEE_MAX} the save record's "
                f"unclamped koin subtract can drive the saved koin count "
                f"negative")

    # --object-budget accepts a preset label OR a raw integer, so it is checked
    # here rather than with argparse `choices`/`type=int`.
    if args.object_budget is not None and str(args.object_budget).strip():
        _ob = str(args.object_budget).strip().lower()
        if _ob not in OBJECT_BUDGET_PRESETS:
            try:
                _obv = int(_ob)
            except ValueError:
                p.error(f"--object-budget must be one of "
                        f"{'/'.join(OBJECT_BUDGET_PRESETS)} or an integer "
                        f"{OBJECT_BUDGET_MIN}-{OBJECT_BUDGET_MAX} "
                        f"(got {args.object_budget!r})")
            else:
                if not (OBJECT_BUDGET_MIN <= _obv <= OBJECT_BUDGET_MAX):
                    p.error(f"--object-budget must be {OBJECT_BUDGET_MIN}-"
                            f"{OBJECT_BUDGET_MAX} (got {_obv}); above "
                            f"{OBJECT_BUDGET_SAFE_MAX} is already experimental "
                            f"and above {OBJECT_BUDGET_MAX} the cushion between "
                            f"the densest level's renderable population and the "
                            f"engine's fixed 320-entry render list is gone")
                args.object_budget = _obv

    # --resource-pool accepts a preset label OR a raw integer (decimal or 0x…),
    # so it is checked here rather than with argparse `choices`/`type=int`.
    # ONE VALIDATOR, shared with cli.py and the GUI: resolve_resource_pool owns
    # the label mapping, the range and the 64 KiB alignment, and an unknown label
    # RAISES there rather than resolving to None (= vanilla) as it used to.
    if args.resource_pool is not None and str(args.resource_pool).strip():
        try:
            _rpv = resolve_resource_pool(args.resource_pool)
        except ValueError as _exc:
            p.error(f"--resource-pool: {_exc}")
        else:
            if _rpv is not None:
                args.resource_pool = _rpv

    # --pool-occupancy: label or float, so range-checked here like the pool size
    # above. Refused at the measured crash floor, never clamped.
    if args.pool_occupancy is not None and str(args.pool_occupancy).strip():
        _po = str(args.pool_occupancy).strip().lower()
        if _po in ("measured", "default", "vanilla"):
            args.pool_occupancy = _DEFAULT_MAX_PCT
        else:
            try:
                _pov = float(_po)
            except ValueError:
                p.error(f"--pool-occupancy must be 'measured' (= the default "
                        f"{_DEFAULT_MAX_PCT}, which is below the measured "
                        f"loading ceiling of {_LOADED_MAX_PCT}) or a percentage "
                        f"greater than 0 and "
                        f"below {_CRASHED_MIN_PCT} (got "
                        f"{args.pool_occupancy!r})")
            else:
                if _pov <= 0:
                    p.error(f"--pool-occupancy must be greater than 0 "
                            f"(got {_pov})")
                if _pov >= _CRASHED_MIN_PCT:
                    p.error(f"--pool-occupancy {_pov} is REFUSED: "
                            f"{_CRASHED_MIN_PCT}% is the MEASURED CRASH FLOOR "
                            f"-- builds at 94% and at 95% both crashed at level "
                            f"load, and pool exhaustion is SILENT "
                            f"(MemoryPool::Allocate returns sentinel_block + 16 "
                            f"and the game dies at level load with no log line, "
                            f"no build failure and no test failure). "
                            f"The measured LOADING ceiling is "
                            f"{_LOADED_MAX_PCT} (the default this build draws "
                            f"to is a lower {_DEFAULT_MAX_PCT}); "
                            f"{_LOADED_MAX_PCT}-{_CRASHED_MIN_PCT} is "
                            f"UNEXPLORED and may be walked one build at a time; "
                            f"at or above {_CRASHED_MIN_PCT} is measured to "
                            f"crash, so it is refused rather than clamped")
                args.pool_occupancy = _pov

    # Parse worlds string
    if args.worlds.lower() == "all":
        worlds_set = None
    else:
        worlds_set = {w.strip().lower() for w in args.worlds.split(",") if w.strip()}

    try:
        patch_iso(
            args.iso,
            seed=args.seed,
            items=not args.no_items,
            spawn_location=args.spawn_location,
            exit_location=args.exit_location,
            swap_spawn_with_exit=args.swap_spawn_with_exit,
            gen_tier=args.gen_tier,
            gate_mode=args.gate_mode,
            damage_taken=args.damage_taken,
            damage_dealt=args.damage_dealt,
            shield_cost=resolve_shield_cost(args.shield_cost),
            life_cost=resolve_life_cost(args.life_cost),
            perma_death=args.perma_death,
            sword_cost=resolve_sword_cost(args.sword_cost),
            powerup_duration=resolve_effect_duration("powerup", args.powerup_duration),
            wizard_duration=resolve_effect_duration("wizard", args.wizard_duration),
            frozen_duration=resolve_effect_duration("frozen", args.frozen_duration),
            koin_fee=args.koin_fee,
            disable_collision=args.disable_collision,
            level_timer=args.level_timer,
            timer_display=not args.no_timer_display,
            widescreen=args.widescreen,
            object_budget=args.object_budget,
            resource_pool=args.resource_pool,
            pool_occupancy=args.pool_occupancy,
            start_souls=args.start_souls,
            start_armor=args.start_armor,
            start_shields=args.start_shields,
            start_boxers=args.start_boxers,
            harder_mode=args.harder_mode,
            preserve_chests=args.preserve_chests,
            preserve_iron_keys=args.preserve_iron_keys,
            spawn_config_path=args.spawn_config_path,
            enemies=not args.no_enemies,
            all_enemies=args.all_enemies,
            cross_world=args.cross_world,
            cross_world_structures=args.cross_world_structures,
            boss_arena_arrivals=args.boss_arena_arrivals,
            boss_arena_difficulty=args.boss_arena_difficulty,
            worlds=worlds_set,
            duplicate_bosses=args.duplicate_bosses,
            exclude_dark_knight=args.exclude_dark_knight,
            boss_clones=args.boss_clones,
            output_iso=args.output,
            backup=not args.no_backup,
            workdir=args.workdir,
        )
    except Exception as e:
        print(f"\nERROR: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
