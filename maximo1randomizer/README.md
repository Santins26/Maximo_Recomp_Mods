# Maximo: Ghosts to Glory — Randomizer

A randomizer for **Maximo: Ghosts to Glory** (PS2). Shuffles enemies, items,
chests, level columns, and more across all five worlds — Grave, Under, Swamp,
Ice, Castle.

## Quick start

The easiest way is the GUI. Double-click `randomizer_gui.py` (or run
`python randomizer_gui.py`) and use the **Patch ISO** tab:

1. Pick your Maximo disc image (`.iso`, `.bin`, or `.cue`).
2. Optionally pick an output path — leave blank to patch in place with a backup.
3. Choose which worlds to randomize.
4. Click **Run randomizer**.

The script extracts all level files + the boot executable, randomizes them,
and writes the patches back into the disc image. No save state from before
the patch will work — start a fresh boot.

## CLI

If you'd rather use the command line:

```bash
# Patch a disc image directly (in place, with .backup)
python -m randomizer.iso_patcher Maximo.bin

# Patch with a specific seed and only certain worlds
python -m randomizer.iso_patcher Maximo.bin --seed 1234 --worlds grave,under

# Output to a new file (leaves source untouched)
python -m randomizer.iso_patcher Maximo.bin --output Maximo_random.bin

# Cross-world mode — any enemy can appear in any world
python -m randomizer.iso_patcher Maximo.bin --cross-world

# Folder mode — extract files yourself, patch a folder of PSX files
python -m randomizer.cli randomize game_files -o output --items
```

### `--shield-cost` — shield economy

Sets how many shield points one shield block spends. The option takes a named
preset, not a number:

| Preset | Points spent per block |
| --- | --- |
| `none` | 0 — shield use never depletes the bar |
| `vanilla` | 1 — the game's own cost, and the default |
| `light` | 2 |
| `medium` | 3 |
| `heavy` | 5 |

`vanilla` leaves the boot executable untouched. Available on both front ends,
and as the **Shield Cost** dropdown in the GUI:

```bash
python -m randomizer.cli randomize game_files -o output --shield-cost light
python -m randomizer.iso_patcher Maximo.bin --shield-cost none
```

Both retail builds are covered: **`SLUS_200.17` (US)** and **`SLPM_621.27`
(JP)**, three shield-spending sites patched in each, measured against the real
executables rather than inferred.

The shield's **variable damage-absorption** behaviour is deliberately
unaffected — that path subtracts a register-held amount and stays vanilla at
every preset.

## Layout

```
randomizer/                 The Python module
├── cli.py                  CLI entry point + folder-mode randomize
├── gui.py                  Tkinter GUI (Patch ISO + Folder mode)
├── iso.py                  ISO 9660 reader/writer (.iso/.bin/.cue)
├── iso_patcher.py          End-to-end disc-image patching pipeline
├── psx.py                  PSX (level instance) parser/writer
├── bef.py                  BEF (entity class library) parser + cross-world injector
├── catalog.py              Per-world enemy pools
├── items.py                Universal item randomization
└── elf_patch.py            SLUS_200.17 level-title patch (?)

randomizer_gui.py           Top-level launcher for the GUI
```

## What gets randomized

- **Walking-melee enemies** — every enemy in every map swaps to a different
  type from its world's pool (Basic_Skeleton, SwordSkeleton, Axe_Skeleton,
  Basic_Zombie, Ghost, Guard_Skeleton, Raven, Torso_Zombie, Bomb_Skeleton,
  Hammer_Devil, Doomed_Soul, Goat_Devil, Frozen_Zombie, PirateSkeleton,
  Snowman, Swamp_Zombie, Zombie_Crocodile, Plant_Monster, Dark_Knight,
  Crazed_Prisoner, Axe_Guard, Cursed_Hand). Each pool is filtered to types
  scripted in that world's BEF.
- **Cross-world enemies** (`--cross-world`) — injects enemy asset data from
  other worlds into each world's BEF file, then uses the full universal pool
  (all 21 enemy types) for every world. Snowmen in the graveyard, Goat_Devils
  in the ice ship, Dark_Knights in the swamp — anything goes. BEF files grow
  by ~30-50KB each and are relocated on disc if needed.
- **Items, chests, smashables** — every item-class entity rolls a new type
  from a world-filtered universal pool. About 20% of those rolls become
  enemies; the rest are coins, chests, gems, smashables, hazards, etc.
- **Chest contents** — wooden chests roll a new content tag (mimic, wizard,
  ability, key, armor, 1up, etc.). Locked chests use their own tag set.
- **Chest gold amounts** — re-rolled with a falloff distribution.
- **Ability IDs** — each Ability picks a new ability from the game's pool.
- **Collector drop codes** — each Collector independently picks a new drop.
- **Enemy tier values** — Basic_Skeleton, SwordSkeleton, Basic_Zombie,
  Guard_Skeleton each roll among their valid tier range.
- **Level columns (HUB blind select)** — the 3 unlocked level IDs in each
  HUB get shuffled and renamed to `???`.
- **Level title strings** — the 14 level/boss titles in `SLUS_200.17` are
  patched to `???` so the loading splash matches the blind select.
- **Shield economy** (`--shield-cost`, GUI **Shield Cost**) — not random, a
  chosen setting: how many shield points one block spends, as one of `none` (0),
  `vanilla` (1, default), `light` (2), `medium` (3) or `heavy` (5). `none` makes
  shield use non-depleting. Covers US `SLUS_200.17` and JP `SLPM_621.27` (three
  sites each). The shield's variable damage-absorption behaviour is unaffected.
- **Monster_Generator parameters** — radius, timer, and spawn count get
  varied (the enemy type each generator spawns is bound externally and
  can't be changed here).

## What's protected

The randomizer never modifies these:

- **`key1`** chest (gate key in G_INTRO) and **`gate1`** chest (level finisher).
- **All Gold_Key (0x2B) entities** — progression-critical, stay where vanilla
  placed them.
- **GraveDigger boss** and **all boss-arena maps** (`G_BOSS.PSX`,
  `S_BOSS.PSX`, `I_BOSS.PSX`, `U_BOSS.PSX`) — scripted encounters, never
  randomized, never written to output.
- **Sky maps and empty boss-arena PSX files** — no randomization, not written
  to output.

## Supported disc formats

- `.iso` (2048-byte sectors, plain DVD-format)
- `.bin` (2352-byte raw CD sectors, Mode 1 or Mode 2 Form 1)
- `.cue` (sheet pointing at a `.bin`)

The disc image format is auto-detected. For raw CD images, only the user-data
window of each sector is overwritten; sync, header, and EDC/ECC bytes are
preserved bit-for-bit.

## Requirements

Python 3.10+. No external dependencies — uses only the standard library
(including tkinter for the GUI).
