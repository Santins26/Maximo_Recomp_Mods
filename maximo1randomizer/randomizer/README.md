# randomizer/

The Python module that does the actual work. See the top-level `README.md`
for user-facing docs and quick start.

## Modules

| Module | Purpose |
|---|---|
| `cli.py` | Command-line entry point and folder-mode randomize pipeline |
| `gui.py` | Tkinter GUI — Patch ISO tab + Folder mode tab, world checkboxes |
| `iso.py` | ISO 9660 reader/writer. Auto-detects `.iso` / `.bin` / `.cue` formats |
| `iso_patcher.py` | End-to-end disc-image patching: extract → randomize → write back |
| `psx.py` | PSX (level instance) parser. Reads/writes records, translates trigger volumes |
| `catalog.py` | Per-world walking-melee pools and event-tied protection |
| `items.py` | Universal item randomization, chest contents, collector drops, tiers, level columns, monster generators |
| `elf_patch.py` | Patches all 14 level/boss title strings in `SLUS_200.17` to `???` |

## CLI subcommands

```
python -m randomizer.cli stats <psx_file>
python -m randomizer.cli list-types <psx_folder>
python -m randomizer.cli randomize <psx_folder> -o <out_folder> [options]
python -m randomizer.iso_patcher <disc_image> [options]
```

### `randomize` flags

| Flag | Default | Purpose |
|---|---|---|
| `--seed N` | random | RNG seed |
| `--items` | off | Randomize items/structures (universal type swap) + collectors, enemy tiers, generators, weather |
| `--chests` | mirrors `--items` | Randomize chest contents + gold amounts |
| `--skills` | mirrors `--items` | Randomize ability/skill pickup IDs |
| `--columns` | mirrors `--items` | Randomize HUB blind level-select columns + `???` level titles |
| `--spawn-config FILE` | none | Path to a spawn-rate JSON (per-tag/per-world weights + enable flags). Omit for stock rates |
| `--no-enemies` | off | Skip walking-melee enemy type swaps |
| `--worlds W` | `all` | Comma-separated worlds: `all`, `grave`, `under`, `swamp`, `ice`, `castle` |

Categories are independent: each unselected category keeps that part of the game
at its vanilla values. A bare `--items` (legacy) still enables all four.

### Spawn-rate editor

The GUI's **Edit spawn rates...** button opens a per-tag / per-world weight
editor (tabs: Items, Structures, Enemies → per world). Each entry has an
enable toggle and a 0-100 weight; disabling an entry (or setting weight 0)
removes it from every spawn pool so it never appears. Disabled *enemies* also
have their vanilla instances retyped into an enabled enemy. Settings save to
`spawn_config.json` and are picked up automatically on the next run. The CLI
reads the same file via `--spawn-config`.

> Note: authored entities that the randomizer never removes (e.g.
> Monster_Generator) stay in the map even when disabled — disabling only stops
> other items from *rolling into* that type.

### `iso_patcher` flags

Same as above, plus:

| Flag | Default | Purpose |
|---|---|---|
| `--output PATH` | (in-place) | Output path for the patched disc (file or folder). When omitted, source is patched in place. |
| `--no-backup` | off | Skip the `.backup` copy when patching in place |
| `--workdir PATH` | tempdir | Working directory for extracted/patched files |

## Tuning

All weights and probabilities live at the top of `items.py`:

- `DEFAULT_ITEM_WEIGHTS` — universal pool weights per type
- `ENEMY_SHARE_TARGET` — fraction of item rolls that result in an enemy
  (currently `0.20` = 20%, normalized at runtime per world)
- `CONTENT_TAG_OPTIONS` / `CONTENT_TAG_WEIGHTS` — wooden-chest content rolls
- `LOCKED_CONTENT_TAG_OPTIONS` / `LOCKED_CONTENT_TAG_WEIGHTS` — locked-chest rolls
- `GOLD_AMOUNTS` / `GOLD_WEIGHTS` — chest gold count distribution (in `aggressive_item_randomize`)
- `COLLECTOR_DROP_POOL` / `COLLECTOR_DROP_WEIGHTS` — collector drop-code rolls
- `ENEMY_TIER_TYPES` — per-enemy valid tier ranges

Per-world enemy pools are in `catalog.py`:
`GRAVE_WALKING_MELEE`, `UNDER_WALKING_MELEE`, `SWAMP_WALKING_MELEE`,
`ICE_WALKING_MELEE`, `CASTLE_WALKING_MELEE`.

ELF level-title slots are in `elf_patch.py`'s `LEVEL_TITLE_SLOTS` list.

## Protected records

- Chest **`key1`** (gate key in G_INTRO) and **`gate1`** (level finisher) — never modified
- Chest **content tags** `(31, 2)` and `(34, 1)` — protected progression tags
- **Gold_Key entities** (type 0x2B) — never randomized away or placed at random positions
- **Hazards as sources** — `0xCC` (ICE ship cannons), `0xD1` (Underworld swinging spikes),
  `0x40` (Underworld flame jets) stay in their authored positions. They remain valid
  destinations so other items can still roll INTO a hazard.
- **Per-world ability pool** — abilities only roll IDs whose assets the world's BEF
  loads. Otherwise the pickup spawns invisible / non-collectable.
- **Event-tied enemies** — currently empty after testing showed kill events fire by instance name, not type
