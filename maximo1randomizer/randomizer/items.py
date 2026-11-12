"""
Universal item randomizer for Maximo: Ghosts to Glory.

Every item-class entity in a level rolls independently into a different type
from the universal pool. Enemies are a one-way destination — they can be
rolled INTO from items, but enemy-to-enemy swaps are handled by the dedicated
walking-melee randomizer in cli.py.

PSX files may grow: a replacement record can be larger than the record it
replaces and the writer emits the longer file. Configured weights are
therefore literal — each slot simply takes the type it rolled, with no byte
budget, quota or same-size fallback in the way.
Trigger-volume properties are translated when entities move so detection
ranges work at the new world position. Skeleton keys placed at random
positions get their 'collectable' flags forced on.

See randomize_universal() for the main pipeline and aggressive_item_randomize()
for the multi-pass entry point used by the CLI.
"""
from __future__ import annotations
import random
import struct
from pathlib import Path

from .psx import PsxFile, PsxRecord, make_record_with_template, RECORD_HEADER_SIZE


# Items at size 0x248 (same-size pickup pool — safe swaps, no shifts)
ITEM_POOL_0x248 = {
    0x21: "GoldCoin",
    0x46: "Bag_Of_Gold",
    0x52: "Gem",
    0x0A: "Popup_Headstone",
}

# UNIVERSAL ITEM POOL — every item entity rolls into one of these.
# Order matters: largest sizes first (so they can shrink into the budget pool first).
UNIVERSAL_ITEM_POOL = (
    0x25,  # Wooden_Chest      (0x528)
    0x5B,  # Locked_Chest      (0x4A8)
    0x47,  # Basic_Skeleton    (0x328) — enemy (destination only)
    0x48,  # Basic_Zombie      (0x328) — enemy (destination only)
    0x10,  # Frozen_Zombie     (0x328) — ICE enemy (destination only)
    0x3A,  # PirateSkeleton    (0x328) — ICE enemy (destination only)
    0x39,  # SwordSkeleton     (0x308) — enemy (destination only)
    0x49,  # Bomb_Skeleton     (0x308) — enemy (destination only) — UNDER/ICE/CASTLE
    0x54,  # Guard_Skeleton    (0x2C8) — enemy (destination only) — NATIVE GRAVE
    0x19,  # Swamp_Zombie      (0x2C8) — SWAMP enemy (destination only)
    0x0D,  # Zombie_Crocodile  (0x2C8) — SWAMP enemy (destination only)
    0x13,  # Goat_Devil        (0x2C8) — UNDER enemy (destination only)
    0x01,  # Bone_Tower        (0x2E8)
    0x2F,  # Axe_Skeleton      (0x2E8) — enemy (destination only)
    0x53,  # Dark_Knight       (0x2E8) — CASTLE enemy (destination only)
    # NOTE: Flame_Jet_Dragon_Head (0x40) and Firing_Cannon_Hazard (0xCC)
    # are NOT in the pool — they're authored hazards that must never be
    # randomized (neither source NOR destination). See HAZARD_NEVER_TOUCH.
    0x65,  # Skeleton_Key      (0x288)
    0x45,  # Armor_Power_Up    (0x288) — permanent armor upgrade
    0x6B,  # Ghost             (0x2A8) — enemy (destination only)
    0x6A,  # Torso_Zombie      (0x2A8) — enemy (destination only)
    0x27,  # Hammer_Devil      (0x2A8) — UNDER-only enemy
    0x5D,  # Doomed_Soul       (0x2A8) — UNDER-only enemy
    0x1D,  # Snowman           (0x2A8) — ICE enemy (destination only)
    0x08,  # Plant_Monster     (0x2A8) — SWAMP+CASTLE enemy (destination only)
    0x58,  # Crazed_Prisoner   (0x2A8) — CASTLE enemy (destination only)
    0x24,  # Collector         (0x268)
    0x4E,  # Extra_Life        (0x268)
    0x20,  # Ability           (0x268)
    0x51,  # Full_Health       (0x268) — restores full HP
    0x16,  # Axe_Guard         (0x268) — CASTLE enemy (destination only)
    0x21,  # GoldCoin          (0x248)
    0x46,  # Bag_Of_Gold       (0x248)
    0x52,  # Gem               (0x248)
    0x0A,  # Popup_Headstone   (0x248)
    0x37,  # Raven             (0x248) — enemy (destination only) — small flying enemy
    0x6C,  # CoinContainer     (0x248) — UNDER coin pot (smashable)
    0xBB,  # Thorn_Vine        (0x248) — SWAMP smashable
    # NOTE: Swinging_Spike (0xD1) removed from the pool — authored UNDER
    # hazard, never randomized (see HAZARD_NEVER_TOUCH).
    0x7C,  # Ice_Plant         (0x248) — ICE smashable
    0x1B,  # Popup_Ice         (0x248) — ICE smashable / pop-up trap
    0x2B,  # Gold_Key          (0x248) — destination only (rare jackpot drop,
           #                              activated to collectable on placement)
    0x02,  # Bonus_Grave       (0x228)
    0xB0,  # Smashable_Torch   (0x228)
    0x30,  # Grave_Smashable_Glass (0x228) — smashable container
    0xA0,  # Coffin_Lid        (0x228) — GRAVE smashable
    0xC9,  # Breakable_Rockwall (0x228) — GRAVE smashable
    0xAE,  # Underworld_Lantern (0x228) — UNDER smashable
    0xD3,  # Breakable_TikkiTorch (0x228) — SWAMP smashable
    0xC8,  # Spirit_Statue     (0x228) — SWAMP smashable
    0xB5,  # Swamp_Coffin_Lid  (0x228) — SWAMP smashable (swamp "Bonus_Grave")
    0x17,  # Smashable_Snow_Pirate (0x228) — ICE smashable
    0xB1,  # Smashable_Ice_Wall (0x228) — ICE smashable
    0x18,  # Ice_Torch         (0x228) — ICE smashable (a.k.a. tower torch)
    0xAA,  # Smashable_Door    (0x228) — CASTLE smashable
    0xAD,  # Spirit_Torch      (0x228) — CASTLE smashable
    0x4A,  # Globe             (0x228) — CASTLE decoration (1 vanilla instance)
    0xBC,  # Smashable_Coffin  (0x428) — CASTLE smashable coffin
    0x50,  # Standing_Coffin   (0x428) — CASTLE smashable coffin variant
    0x5F,  # Monster_Generator (0x3C8) — wave spawner. Both a source (can be
           # randomized into other things, so it's no longer guaranteed every
           # seed) and a destination (items can roll into a generator).
)

# Enemy types are valid DESTINATIONS but should NOT be source candidates
# for the universal randomizer (so existing enemies don't get turned into items
# by the universal pool — they're handled by the dedicated enemy randomizer in
# cli.py).
ENEMY_DESTINATIONS = {
    # Common (multiple worlds)
    0x47, 0x48, 0x39, 0x2F, 0x6B, 0x54, 0x6A, 0x37, 0x49, 0x27, 0x5D,
    # World-specific enemies
    0x10,  # Frozen_Zombie (ICE)
    0x3A,  # PirateSkeleton (ICE)
    0x1D,  # Snowman (ICE)
    0x13,  # Goat_Devil (UNDER)
    0x19,  # Swamp_Zombie (SWAMP)
    0x0D,  # Zombie_Crocodile (SWAMP)
    0x08,  # Plant_Monster (SWAMP+CASTLE)
    0x53,  # Dark_Knight (CASTLE)
    0x58,  # Crazed_Prisoner (CASTLE)
    0x16,  # Axe_Guard (CASTLE)
}

# Hazards / traps / cannons that must NEVER be randomized — neither as a
# SOURCE (turned into something else) nor as a DESTINATION (something else
# turned into them). They are kept entirely out of UNIVERSAL_ITEM_POOL so
# they stay exactly where the level designer placed them. This set is also
# used as a belt-and-braces filter in the candidate-collection loops.
#   - Castle traps (smashables/spike doors) are scripted around fixed pillar
#     pairs and breaking them off-pattern softlocks rooms.
#   - Underworld traps (Swinging_Spike 0xD1, Flame_Jet 0x40, Bear_Trap 0x8D)
#     are timed with specific platforming sections; extra/moved ones make
#     sections unfair or block the path.
#   - Ice ship cannons (Firing_Cannon_Hazard 0xCC) are aimed at specific
#     player paths; randomizing them moves the projectile origin off-screen.
HAZARD_PROTECTED_AS_SOURCE = {
    # Physical hazards: fixed in their exact vanilla positions (size-matched
    # swaps would move them onto wrong geometry, breaking kill volumes).
    0xCC,  # Firing_Cannon_Hazard (ICE ship cannons)
    0xD1,  # Swinging_Spike (UNDER)
    0x40,  # Flame_Jet_Dragon_Head (UNDER)
    0x8D,  # Bear_Trap (UNDER)

    # Scripted world props: gates, triggers, and interactive scenery whose
    # instance_names are referenced by gate/progression scripts. Replacing
    # them with a different entity type silently removes the script target,
    # leaving the level's load-sequence waiting forever (infinite loading).
    # Confirmed crash causes in:
    #   S_SUB3 (Quick and the Dead): 0xC0, 0xDB, 0xD7, 0xAF
    #   U_SUB3 (Crushed Spirits):    0xAF, 0xF2
    #   G_SUB2 (Dead Heat):          0xAF
    0xC0,  # Swamp_Locked_Gate    — SWAMP gate (size 0x228, same group as Coffin_Lid)
    0xDB,  # Swamp_Crypt_Gate     — SWAMP gate (size 0x228)
    0xD7,  # Rain_Trigger         — SWAMP scripted trigger
    0xAF,  # TriggerOnHit         — all worlds scripted trigger
    0xF2,  # Ambient_Sound        — UNDER ambient (not in pool but listed for safety)

    # World-specific scripted props: these ARE in UNIVERSAL_ITEM_POOL and in
    # SAME_SIZE_GROUPS (0x228), so the randomizer can freely swap them with
    # each other. But in levels like S_SUB3 they are ordered puzzle chains
    # (the coffin lids, torches, and statues are numbered 1st/2nd/.../9th and
    # must remain in their vanilla sequence for the gate scripts to trigger
    # correctly). Moving them produces a different ordering that breaks the
    # gate progression, leaving the level in infinite loading.
    0xB5,  # Swamp_Coffin_Lid    — SWAMP ordered puzzle prop (S_SUB3)
    0xC8,  # Spirit_Statue       — SWAMP ordered puzzle prop (S_SUB3)
    0xD3,  # Breakable_TikkiTorch — SWAMP ordered puzzle prop (S_SUB3)
    0xAE,  # Underworld_Lantern  — UNDER ordered puzzle prop (U_SUB3)

    # Scripted / interactive world entities: share a size with pool items so
    # can become roll destinations, but the engine expects them at exactly their
    # designer-placed positions. Replacing ANY of these with a random item
    # causes one of three failure modes:
    #   - Infinite loading (gate/column script waits for a trigger that vanished)
    #   - Level becomes unexitable (End_Of_Sublevel_Column replaced)
    #   - Hard crash (Teleport_Pool / Holy_Ground memory access in wrong context)
    # Present across CRASHING levels (G_SUB2, S_SUB3, U_SUB3) and others.
    0x4D,  # End_Of_Sublevel_Column — ALL worlds, level-exit trigger (CRITICAL)
    0x59,  # Level_Column           — all hubs, progression anchor
    0x72,  # Mausoleum_Gate         — GRAVE scripted gate (size 0x228)
    0xE2,  # Graveintro_Locked_Swinging_Gate — GRAVE scripted gate (size 0x228)
    0x0C,  # Teleport_Pool          — all hubs (size 0x248)
    0x07,  # Holy_Ground            — all worlds, scripted safe-zone (size 0x248)
    0x23,  # Cursed_Hand            — GRAVE scripted interaction (size 0x248)
    0x41,  # Lift_Gate              — CASTLE/hub gate (size 0x248)
    0x9F,  # Flame_Jet              — GRAVE/CASTLE hazard anchor (size 0x248)
    0x15,  # Iron_Maiden            — CASTLE scripted hazard (size 0x268)
    0xCF,  # Snow                   — ICE ambient env (size 0x268)
    0xD0,  # Rain                   — SWAMP ambient env (size 0x268)
    0xCE,  # Fog_Volume             — GRAVE/UNDER ambient (size 0x2A8)
    0xF6,  # BGAnim_Sound           — GRAVE ambient (size 0x2A8)
    0xE0,  # Fire                   — GRAVE/CASTLE ambient hazard (size 0x2E8)
    0xA3,  # CamTrigger             — all worlds camera trigger (size 0x328)
    0x2D,  # Steam_Vent             — GRAVE/UNDER env hazard (size 0x3C8)
    0xE7,  # Ambient_Waterfall_Noise — GRAVE hub ambient (size 0x228)
}

# Same-size group lookup (no growth/shrink between members)
SAME_SIZE_GROUPS = {
    0x228: (0x02, 0xB0, 0x30, 0xA0, 0xC9,
            # 0xAE/0xD3/0xC8/0xB5 removed: ordered puzzle props now in
            # HAZARD_PROTECTED_AS_SOURCE -- see comment there.
            0x17, 0xB1, 0x18, 0xAA, 0xAD, 0x4A),
    0x248: (0x21, 0x46, 0x52, 0x0A, 0x37, 0x6C,
            0xBB, 0x7C, 0x1B, 0x2B),
    0x268: (0x24, 0x4E, 0x20, 0x51, 0x16),
    0x288: (0x65, 0x45),
    0x2A8: (0x6B, 0x6A, 0x27, 0x5D, 0x1D, 0x08, 0x58),
    0x2C8: (0x54, 0x19, 0x0D, 0x13),
    0x2E8: (0x01, 0x2F, 0x53),
    0x308: (0x39, 0x49),
    0x328: (0x47, 0x48, 0x10, 0x3A),
    0x3C8: (0x5F,),
    0x428: (0xBC, 0x50),
    0x4A8: (0x5B,),
    0x528: (0x25,),
}


# ============================================================================
# GATE structures
# ============================================================================
# Physical gates / locked barriers. NOT part of UNIVERSAL_ITEM_POOL by default
# (they're progression barriers), so they're only touched when the gate
# randomizer is explicitly enabled. Two modes (see aggressive_item_randomize's
# gate_mode):
#   "isolated" — gate slots roll among other GATE types only (in place), so
#                gates change identity but nothing else becomes a gate.
#   "pool"     — gates are added to the universal item pool, so a gate can roll
#                into an item AND an item can roll into a gate (gates appear at
#                random spots on the map).
# Sizes (from the discs): all gates are 0x228 except Lift_Gate (0x248).
GATE_TYPES = (0x04, 0x3F, 0x41, 0x72, 0x8B, 0xA1, 0xC0, 0xCB, 0xDB, 0xE2)
GATE_NAMES = {
    0x04: "Hut_Gate", 0x3F: "Underworld_Gate", 0x41: "Lift_Gate",
    0x72: "Mausoleum_Gate", 0x8B: "Ice_Gold_Gate", 0xA1: "Ice_Gate_Small",
    0xC0: "Swamp_Locked_Gate", 0xCB: "Drop_Locked_Gate",
    0xDB: "Swamp_Crypt_Gate", 0xE2: "Graveintro_Locked_Swinging_Gate",
}
# Weight for each gate type when it participates in the universal pool ("pool"
# mode). Low so gates only appear occasionally as item->gate rolls.
GATE_WEIGHTS = {tid: 4 for tid in GATE_TYPES}
# Isolated mode ("randomize gates into anything"): probability a gate KEEPS its
# own type instead of rolling into something else, so not every gate vanishes.
GATE_ISOLATED_STAY_RATE = 0.5

# Default weights for the universal pool — used ONLY when an item DOES change.
# Tuning rule:
#   - Coin highest (filler, common floor item)
#   - Wooden_Chest second, clearly behind coins (mimic/wizard surprises)
#   - Locked_Chest / Skeleton_Key sized together so keys match the locks
#   - Bag_Of_Gold and Gem are uncommon-but-real gold rewards
#   - Most structures/smashables sit around 5-8
#   - Extra_Life is the rarest entry
# Telemetry from the most recent randomize_universal() item pass: the number
# of slots that rolled a new type and the histogram of what was actually
# placed. Populated at the end of every call and cleared at the start, so a
# caller always reads THIS map's numbers.
LAST_UNIVERSAL_STATS: dict = {}

DEFAULT_ITEM_WEIGHTS = {
    # Top tier — coins are the most common drop; chests stay frequent but no
    # longer outnumber coins, each with freshly-randomized contents and gold.
    # CURRENCY REBALANCE (user request: "nerf a little bit the wooden chest,
    # locked chest, skeleton and gold key % and give it to the koins, bag of
    # gold and gem"). A MODEST shift of 43 weight, pool total unchanged:
    #   Wooden_Chest 200->170, Locked_Chest 60->52, Skeleton_Key 32->27
    #   GoldCoin     446->471, Bag_Of_Gold  52->62, Gem          34->42
    # Gold_Key 0x2B is NOT here and is deliberately NOT added: it is a
    # PROGRESSION_ANCHOR_TYPES member placed by a post-roll lottery
    # (GOLD_KEY_SPAWN_PROBABILITY), outside the spawnable pool entirely.
    # The Skeleton_Key trim is safe because 0x65 is never a randomization
    # SOURCE (see aggressive_item_randomize), so every vanilla key instance
    # stays exactly where it is; this weight only governs how many EXTRA keys
    # other slots roll into.
    0x21: 471,  # GoldCoin            (most common filler drop — +25 from 446)
    0x25: 170,  # Wooden_Chest        (nerfed from 200)
    # Mid tier
    0x46: 62,   # Bag_Of_Gold         (a real gold reward — +10 from 52)
    0x5B: 52,   # Locked_Chest        (nerfed from 60, alongside the key supply)
    # Enemy tier — all enemies normalized to weight 15 so they have EQUAL
    # chances of being rolled. The walking-melee enemy randomizer (in cli.py)
    # already picks uniformly within each world's pool. This block governs
    # how often an ITEM rolls INTO an enemy from the universal pool.
    0x47: 15,   # Basic_Skeleton
    0x39: 15,   # SwordSkeleton
    0x2F: 15,   # Axe_Skeleton
    0x48: 15,   # Basic_Zombie
    0x6B: 9,    # Ghost              (reduced — visually prominent floater)
    0x54: 6,    # Guard_Skeleton     (reduced — too common in vanilla)
    0x6A: 6,    # Torso_Zombie       (reduced — visually prominent crawler)
    0x37: 6,    # Raven              (reduced — small flying enemy, weak AI)
    0x49: 15,   # Bomb_Skeleton
    0x27: 15,   # Hammer_Devil      (UNDER)
    0x5D: 15,   # Doomed_Soul       (UNDER)
    # World-specific enemies — same equal weight
    0x10: 15,   # Frozen_Zombie     (ICE)
    0x3A: 15,   # PirateSkeleton    (ICE)
    0x1D: 15,   # Snowman           (ICE)
    0x13: 15,   # Goat_Devil        (UNDER)
    0x19: 15,   # Swamp_Zombie      (SWAMP)
    0x0D: 15,   # Zombie_Crocodile  (SWAMP)
    0x08: 15,   # Plant_Monster     (SWAMP+CASTLE)
    0x53: 15,   # Dark_Knight       (CASTLE)
    0x58: 15,   # Crazed_Prisoner   (CASTLE)
    0x16: 15,   # Axe_Guard         (CASTLE)
    # (Flame_Jet 0x40 / Firing_Cannon 0xCC removed from pool — never randomized)
    # Low (0.5% each except Bonus_Grave)
    0x52: 42,   # Gem                 (uncommon but worth finding — +8 from 34)
    0x0A: 5,    # Popup_Headstone     (0.5%)
    0x24: 25,   # Collector           (2.5% — bumped, was effectively invisible)
    0x20: 28,   # Ability             (2.8% — bumped further, abilities/skills are fun)
    0x01: 5,    # Bone_Tower          (0.5%)
    0x02: 25,   # Bonus_Grave         (2.5% — bumped, fun grave smashing)
    0xB0: 5,    # Smashable_Torch     (0.5%)
    0x30: 5,    # Grave_Smashable_Glass (0.5% — smashable container)
    # Smashables (0.5-1.0% each)
    0xA0: 8,    # Coffin_Lid          (0.8% — GRAVE smashable, tomb-style)
    0xC9: 3,    # Breakable_Rockwall  (0.3% — rare GRAVE)
    0xAE: 8,    # Underworld_Lantern  (0.8% — UNDER smashable)
    0xD3: 8,    # Breakable_TikkiTorch (0.8% — SWAMP smashable)
    0x6C: 10,   # CoinContainer       (1.0% — UNDER coin pot)
    # World-specific smashables / decorations
    0xC8: 8,    # Spirit_Statue       (0.8% — SWAMP smashable)
    0xB5: 8,    # Swamp_Coffin_Lid    (0.8% — SWAMP smashable, swamp "Bonus_Grave")
    0xBB: 8,    # Thorn_Vine          (0.8% — SWAMP smashable)
    0x17: 8,    # Smashable_Snow_Pirate (0.8% — ICE smashable)
    0xB1: 8,    # Smashable_Ice_Wall  (0.8% — ICE smashable)
    0x7C: 5,    # Ice_Plant           (0.5% — ICE smashable)
    0x1B: 8,    # Popup_Ice           (0.8% — ICE pop-up trap)
    0x18: 8,    # Ice_Torch           (0.8% — ICE tower torch / smashable)
    0xAA: 8,    # Smashable_Door      (0.8% — CASTLE smashable)
    0xAD: 8,    # Spirit_Torch        (0.8% — CASTLE smashable)
    0x4A: 3,    # Globe               (0.3% — CASTLE rare decoration)
    0xBC: 8,    # Smashable_Coffin    (0.8% — CASTLE smashable coffin)
    0x50: 8,    # Standing_Coffin     (0.8% — CASTLE smashable coffin variant)
    # (Swinging_Spike 0xD1 removed from pool — never randomized)
    # Pickups (0.3-0.5%)
    0x65: 27,   # Skeleton_Key        (nerfed from 32 — EXTRA keys only; the
                #                      vanilla key instances are never sourced)
    0x45: 5,    # Armor_Power_Up      (0.5% — permanent armor upgrade)
    0x51: 5,    # Full_Health         (0.5% — full HP restore)
    0x4E: 1,    # Extra_Life          (0.1%)
    # Monster_Generator: spawns a wave of enemies. Moderate weight so they
    # appear several times per map but don't dominate the pool.
    0x5F: 12,   # Monster_Generator   (1.2%)
}

# Gold_Key (0x2B) post-roll lottery probability.
# After every item finishes rolling its new type via the universal pool, this
# fraction of rolls "wins" a Gold_Key upgrade — overriding whatever was
# rolled. Set to 0.007 (= 0.7%) for every world.
#
# This is a POST-ROLL LOTTERY, not a pool weight: it is applied after the
# weighted pool has already chosen a type, so changing it neither consumes nor
# frees weight in DEFAULT_ITEM_WEIGHTS and must never be compensated for there.
#
# The override happens whenever a Gold_Key template is available (the file or
# extra_templates supplies one). There is no size condition: the lottery always
# fires at this probability.
GOLD_KEY_SPAWN_PROBABILITY = 0.007

# Skeleton_Key (0x65) post-roll lottery probability — used ONLY in the
# "all enemies" mode (see randomize_universal's `all_enemies` flag). In that
# mode every item slot rolls into an enemy, so without a key carve-out the
# player would have no Iron Keys to open locked chests / doors and could
# soft-lock. This lottery converts ~1% of would-be-enemy slots into a
# collectable Skeleton_Key instead, keeping the world beatable.
SKELETON_KEY_SPAWN_PROBABILITY = 0.01

# Per-world weight OVERRIDES. Merged on top of DEFAULT_ITEM_WEIGHTS for the
# matching world before rolling. Lets a single world bias specific drops
# without affecting the others.
#
# Castle: Skeleton_Key boosted — Castle Maximo is gating-heavy (lots of
# Lock_Boxes / Smashable_Doors needing Iron Keys) so the player needs a
# higher key supply there than the global default.
PER_WORLD_ITEM_WEIGHTS: dict[str, dict[int, int]] = {
    'castle': {
        0x65: 80,  # Skeleton_Key — boosted (~3x the global 27) for Castle
    },
}


def get_item_weights_for_world(world: str | None) -> dict[int, int]:
    """Return the item-weight table for a world: DEFAULT_ITEM_WEIGHTS with
    any PER_WORLD_ITEM_WEIGHTS override merged on top. Returns the shared
    default dict (not a copy) when no world override exists."""
    override = PER_WORLD_ITEM_WEIGHTS.get(world or "")
    if not override:
        return DEFAULT_ITEM_WEIGHTS
    merged = dict(DEFAULT_ITEM_WEIGHTS)
    merged.update(override)
    return merged


# Per-item stay-rate: probability that an item KEEPS its current type without rolling.
# 0.0 = always re-roll (every item rolls a new type from the weighted pool).
ITEM_STAY_RATE = 0.0

# Target percentage of item rolls that result in an ENEMY (vs a non-enemy item).
# The randomizer scales enemy weights at runtime so the enemy slot equals this
# fraction of the eligible pool — independent of world / pool size.
# Set to None to use raw weights without normalization.
#
# DEFAULT IS NOW None: THE WEIGHTS THE USER TYPES ARE USED AS TYPED. This used to
# be 0.20, which forced enemies to exactly 20% of every pool no matter what the
# spawn-rate editor said -- MEASURED on a real saved config, grave's typed 33.9%
# enemy share and castle's 48.8% both came out at exactly 20.0%, so changing a
# percentage had no visible effect. `apply_harder_mode` still sets 0.65 below, so
# Harder Mode keeps its forced enemy share; only the silent default is gone.
ENEMY_SHARE_TARGET = None

# Backwards-compat alias
COIN_TRANSFORM_POOL = UNIVERSAL_ITEM_POOL

# Indices that flag chest CONTENT (not cosmetic).
# Expanded after surveying all 5 worlds — additional content idxs found:
#   15, 17, 19, 23, 25, 28, 29, 31, 32 — all u32 content flags
# Reserved indices NOT included here (they're not content):
#   5, 33  — float configuration values (scale/timer/offset)
CHEST_CONTENT_INDICES = {3, 4, 6, 14, 15, 16, 17, 18, 19, 20, 21,
                         23, 25, 28, 29, 30, 31, 32, 34}

# Tag-pairs that MUST stay in their original chest and block content
# re-rolling entirely.
#
# VERIFIED against all 5 worlds:
#   (31, 2): gate-key chest ('key1' in G_INTRO) — drops the Gold_Key that
#            opens the first locked gate. Progression-critical.
#   (34, 1): level-finisher chest ('gate1' in G_INTRO) — the Grim Reaper
#            Coin / level-end trigger. Progression-critical.
#
# NOT included:
#   (31, 1): originally mistaken for a Gold_Key drop tag, but 'prize1' in
#            G_SUB1 carries this alongside (16,5) and (17,1) — it is a
#            plain prize-reward marker, not a gate key. Removing it from
#            PROTECTED_TAGS unblocks the two mis-blocked chests in G_SUB1.
#
# (34, 1) guard: only blocks when (34,1) appears as the *sole* content tag
# (or paired only with (18,1)), matching the exact vanilla 'gate1' pattern.
# 'startgrave1' in G_SUB1 carries (34,1) alongside (15,1),(16,5),(18,1) —
# that is a normal reward chest, not a level finisher. See
# chest_has_protected_tag() below which enforces this narrower check.
PROTECTED_TAGS = {(31, 2), (34, 1)}

# Specific chest INSTANCE NAMES that should never be modified (extra protection)
# - 'key1':  Wooden_Chest at (-69.4,2.6,-41.5), tag ((31,2),) — gate key for first locked gate
# - 'gate1': Wooden_Chest at (2.2,2.0,-33.5),  tag ((34,1),) — Grim Reaper Coin / LEVEL FINISHER
PROTECTED_INSTANCE_NAMES = {"key1"}

# Gold_Key entity (type 0x2B). Used in C_INTRO/C_SUB2 (Castle), G_SUB1 (Grave),
# and I_SUB2 (Ice). Vanilla Gold_Key records in PSX files are INACTIVE anchor
# records — the engine activates them at runtime when the player clears the
# kill-group of records sharing the same instance_name (a mix of enemies,
# bags, pickups, etc). We protect ALL Gold_Key entities by type so they can't
# be turned into other things AND so they aren't placed at random positions
# where the player can't reach them. Additionally, every record SHARING a
# Gold_Key's instance_name is protected by `get_progression_protected_names`.
GOLD_KEY_TYPE = 0x2B

# Types that ANCHOR a kill-group / progression trigger. Any record sharing an
# instance_name with one of these is protected from randomization.
#
# Discovered the hard way: there's a kill-group called 'end1' in EVERY world
# that contains 30+ siblings (Locked_Chests, Skeleton_Keys, Gold_Keys,
# enemies, smashables) and gates the level finisher. Randomizing any enemy
# inside that group breaks the death-count and the player can't finish.
# Same for kill-groups that hold a Level_Column (HUB level-select), or a
# Skeleton_Key (which the engine spawns when its kill-group is cleared).
#
#   0x2B Gold_Key       — boss-key drop, gates locked gates
#   0x59 Level_Column   — HUB level-select tile
#   0x65 Skeleton_Key   — small-key drop (locked chests, doors)
#   0x5F Monster_Generator — wave spawner; see bug note below
#
# NOT in this list:
#   0x25 Wooden_Chest, 0x5B Locked_Chest — these were originally treated as
#     anchors but doing so over-protected ~85% of all chests (every chest
#     with a non-auto-generated name like 'deadend1' / 'mimic' / 'first'
#     became kill-group-locked and never rolled). The engine binds chest
#     content gating via PROTECTED_TAGS and PROTECTED_INSTANCE_NAMES which
#     already cover the actual progression-critical chests (key1, gate1,
#     and chests carrying tag (31,2) or (34,1)). Plus the 'end1' group is
#     anchored by the Skeleton_Key it contains, so removing chest-anchors
#     doesn't compromise level-finisher protection.
#
# BUG FIX (investigated after reports of infinite loading on Dead Heat /
# Quick and the Dead / Crushed Spirits): Monster_Generator was previously
# NOT an anchor type, and was explicitly allowed as a randomization SOURCE
# (see its entry in HAZARD_PROTECTED_AS_SOURCE's sibling list) on the
# reasoning that losing a wave-spawner is only cosmetically worse (fewer
# enemies), never a break. That reasoning misses named kill-groups where
# the Monster_Generator IS the group's anchor member and something else in
# the same group is progression-critical. Confirmed case: G_SUB1.PSX (Dead
# Heat) has a 10-member 'crypt1' group containing a Locked_Chest (0x5B) and
# a Monster_Generator (0x5F) among ordinary enemies -- with no member of
# type Gold_Key/Level_Column/Skeleton_Key, this group was previously
# entirely unprotected, and its Monster_Generator was being randomized away
# (e.g. into a Wooden_Chest) by the base randomizer. Adding Monster_Generator
# as an anchor type protects every group like this by NAME (once any member
# is anchor-typed, the whole named group is protected -- see the loop
# below), without reopening the ~85%-of-chests over-protection problem
# Locked_Chest caused: Monster_Generator only appears 0-2 times per file, so
# this adds a small, precise set of named groups rather than a blanket rule.
PROGRESSION_ANCHOR_TYPES = frozenset({
    0x2B,  # Gold_Key
    0x59,  # Level_Column
    0x65,  # Skeleton_Key
    0x5F,  # Monster_Generator
})


def get_progression_protected_names(psx) -> set[str]:
    """Per-file protection set: every instance_name shared with a
    progression-critical anchor record (Gold_Key / Level_Column /
    Skeleton_Key), PLUS every instance_name matching the level-finisher
    naming convention ('end1', 'end2', 'end3', ...).

    Why: The engine uses kill-group bookkeeping based on shared
    instance_name to spawn keys, open gates, and trigger level-finishers.
    Vanilla observation: a kill-group named 'end1' exists in every world
    and contains the level-end Skeleton_Keys, Locked_Chests, and 30+
    enemies. Randomizing any sibling inside that group means the engine
    never sees the death-count threshold cross and the player can't finish
    the level.

    BUG FOUND (investigated after reports of infinite loading on Dead Heat /
    Quick and the Dead / Crushed Spirits with plain item/enemy randomization
    -- no level-shuffle options enabled): the type-based scan below assumes
    every progression-critical kill-group contains at least one
    PROGRESSION_ANCHOR_TYPES member (Gold_Key/Level_Column/Skeleton_Key).
    That assumption is FALSE for many levels -- a survey of the vanilla PSX
    files found 'end1'/'end2'/'end3' groups made up entirely of ordinary
    enemies (Basic_Skeleton, Axe_Skeleton, Goat_Devil, ...), a Wooden_Chest,
    a CamTrigger, etc, with NO anchor-typed member at all in G_SUB1.PSX,
    S_SUB3.PSX, and U_SUB1.PSX specifically (and likely others). In those
    files this function previously returned an empty protection set for the
    'end*' group, so the base randomizer was free to re-roll every enemy in
    the level-finisher kill-group -- when the finisher enemy got replaced,
    the engine's death-count threshold is never crossed and the level hangs
    on load. The name-pattern check below closes that gap directly, since
    'end<N>' is the one naming convention confirmed (by the comment above,
    predating this fix) to always mean level-finisher.

    Empty/blank instance_names (which most non-anchor enemies use) are NOT
    added — they would falsely trap unrelated records.
    """
    names: set[str] = set()
    for r in psx.records:
        n = r.instance_name
        if not n:
            continue
        # Level-finisher kill-groups: 'end1', 'end2', 'end3', ... in every
        # world. Protect ALL members regardless of type -- see bug note
        # above. Auto-generated per-record names (e.g. "Wooden_Chest_13815")
        # never collide with this pattern so no extra exclusion is needed
        # here.
        if n[:3] == "end" and n[3:].isdigit():
            names.add(n)
            continue
        if r.type_id not in PROGRESSION_ANCHOR_TYPES:
            continue
        # Skip the auto-generated names (e.g. "Wooden_Chest_13815") which
        # are unique per-record and don't represent kill-groups. They're
        # only "shared" with themselves, so no group protection is needed.
        # The randomizer's chest-specific protection still keeps these
        # chests' own progression tags intact, but their record-bodies are
        # free to roll content.
        if (n.startswith("Wooden_Chest_") or n.startswith("Locked_Chest_")
                or n.startswith("Skeleton_Key_") or n.startswith("Gold_Key_")
                or n.startswith("Level_Column_")):
            continue
        names.add(n)
    return names


# Backwards-compat alias — the old name was Gold_Key-only protection. Keep
# the symbol so external imports still work; they now get the broader set.
get_gold_key_protected_names = get_progression_protected_names


# Random content combos for chests, with WEIGHTS.
# Tag meanings (verified by chest instance names across all 5 worlds):
#   idx=3,  val=1: HIDDEN flag (chest invisible until activated — cosmetic)
#   idx=4,  val=1: skeleton key (Iron Key)
#   idx=6,  val=1: MIMIC spawn (chest is a mimic enemy)
#   idx=6,  val=2: WIZARD spawn (chest spawns a wizard)
#   idx=14, val=1: generic content (ability/skill variant)
#   idx=15, val=1: generic content (boss/wizard variant)
#   idx=16, val=N: gold coin count
#   idx=17, val=1: prize content
#   idx=18, val=1/2/3: extra life (1up)
#   idx=19, val=1: surprise drop
#   idx=20, val=1: "sucker" chest (teleport/trap)
#   idx=21, val=1: ability/skill pickup
#   idx=23, val=1: generic content
#   idx=25, val=1: hidden surprise
#   idx=28, val=1: special drop A
#   idx=29, val=1: special drop B
#   idx=30, val=1: armor upgrade
#   idx=31, val=2: GATE KEY (PROTECTED, key1 only)
#   idx=34, val=1: GATE KEY / level-finisher (PROTECTED)
#
# Design goals:
#   - Every meaningful drop type has a clear share of the pool.
#   - Mimic/wizard are fun surprises, not the dominant outcome (~10% combined).
#   - Skills/abilities, armor, gold, and iron keys are all common enough to
#     feel rewarding and varied across a run.
#   - Gold amounts span the full vanilla range (3-12 coins).
#   - Hidden chests are a minority so most chests are immediately rewarding.
CONTENT_TAG_OPTIONS = [
    # ===== Skill / ability =====
    ((21, 1),),                          # ability
    ((21, 1), (15, 1)),                  # ability + boss-style flag
    ((21, 1), (30, 1)),                  # ability + armor (rare jackpot)
    # ===== Armor =====
    ((30, 1),),                          # armor
    ((30, 1), (15, 1)),                  # armor + variant
    # ===== Iron Key =====
    ((4, 1),),                           # iron key
    ((4, 1), (21, 1)),                   # iron key + ability
    ((4, 1), (18, 1)),                   # iron key + 1up
    # ===== Extra life / 1UP =====
    ((18, 1),),                          # 1up
    ((18, 2),),                          # 1up variant 2
    ((18, 3),),                          # 1up variant 3
    # ===== Gold rewards =====
    ((16, 3),),                          # 3 gold
    ((16, 5),),                          # 5 gold
    ((16, 8),),                          # 8 gold
    ((16, 10),),                         # 10 gold
    ((16, 12),),                         # 12 gold
    ((16, 5), (21, 1)),                  # 5 gold + ability
    ((16, 8), (21, 1)),                  # 8 gold + ability
    ((16, 5), (30, 1)),                  # 5 gold + armor
    ((16, 8), (30, 1)),                  # 8 gold + armor
    ((16, 5), (4, 1)),                   # 5 gold + iron key
    ((16, 5), (18, 1)),                  # 5 gold + 1up
    ((16, 8), (18, 1)),                  # 8 gold + 1up
    ((16, 5), (17, 1)),                  # 5 gold + prize
    ((16, 10), (21, 1)),                 # 10 gold + ability
    ((16, 12), (30, 1)),                 # 12 gold + armor
    # ===== Prize / surprise drops =====
    ((17, 1),),                          # prize
    ((17, 2),),                          # prize variant
    ((17, 1), (18, 1)),                  # prize + 1up
    ((19, 1),),                          # surprise
    ((23, 1),),                          # generic content
    # ((14,1),) removed: verified against the vanilla discs that wooden-chest
    # records carry property indices 3-6 and 15-34 only — there is no idx=14
    # slot to write, so this option always produced a contentless chest.
    ((15, 1),),                          # boss/wizard variant
    ((25, 1),),                          # hidden surprise
    ((28, 1),),                          # special drop A
    ((29, 1),),                          # special drop B
    ((28, 1), (29, 1)),                  # both special drops
    # ===== Mimic / wizard — fun surprises, not dominant =====
    ((6, 1),),                           # MIMIC
    ((6, 2),),                           # WIZARD
    ((6, 1), (16, 3)),                   # mimic + bait gold
    ((6, 2), (16, 3)),                   # wizard + bait gold
    ((6, 2), (15, 1)),                   # wizard + boss-style flag
    ((32, 1),),                          # castle mimic variant
    # ===== Sucker / hidden =====
    ((20, 1),),                          # sucker trap
    ((3, 1),),                           # hidden empty
    ((3, 1), (4, 1)),                    # hidden + iron key
    ((3, 1), (21, 1)),                   # hidden + ability
    ((3, 1), (30, 1)),                   # hidden + armor
    ((3, 1), (18, 1)),                   # hidden + 1up
    ((3, 1), (17, 1)),                   # hidden + prize
    ((3, 1), (16, 5), (21, 1)),          # hidden + 5 gold + ability
    ((3, 1), (16, 5), (18, 1)),          # hidden + 5 gold + 1up
    ((3, 1), (16, 8), (30, 1)),          # hidden + 8 gold + armor
    # ===== Empty =====
    (),                                  # no content
]
# Total weight ~300 for readable percentages.
# Target distribution:
#   Skill/ability:   ~18%   (popular and fun, synergises with ability randomiser)
#   Armor:           ~10%   (meaningful upgrade)
#   Iron Key:        ~12%   (important for locked content)
#   Extra life:      ~10%   (classic chest reward)
#   Gold rewards:    ~22%   (filler but varied, spans 3-12)
#   Prize/surprise:  ~10%   (mystery drops)
#   Mimic/Wizard:    ~10%   (fun surprise, not dominant)
#   Hidden:          ~ 6%   (exploration reward)
#   Empty/sucker:    ~ 2%
CONTENT_TAG_WEIGHTS = [
    # ===== Skill / ability =====
    18,  # (21,1)        ability alone
     8,  # (21,1)+(15,1) ability + variant
     4,  # (21,1)+(30,1) ability + armor jackpot
    # ===== Armor =====
    18,  # (30,1)        armor alone
     5,  # (30,1)+(15,1) armor + variant
    # ===== Iron Key =====
    18,  # (4,1)         iron key
     5,  # (4,1)+(21,1)  iron key + ability
     5,  # (4,1)+(18,1)  iron key + 1up
    # ===== Extra life =====
    10,  # (18,1)        1up
     7,  # (18,2)        1up v2
     5,  # (18,3)        1up v3
    # ===== Gold =====
     8,  # (16,3)  3 gold
    10,  # (16,5)  5 gold
     8,  # (16,8)  8 gold
     6,  # (16,10) 10 gold
     4,  # (16,12) 12 gold
     8,  # (16,5)+(21,1)
     6,  # (16,8)+(21,1)
     6,  # (16,5)+(30,1)
     5,  # (16,8)+(30,1)
     5,  # (16,5)+(4,1)
     5,  # (16,5)+(18,1)
     4,  # (16,8)+(18,1)
     4,  # (16,5)+(17,1)
     4,  # (16,10)+(21,1)
     3,  # (16,12)+(30,1)
    # ===== Prize / surprise =====
     8,  # (17,1)        prize
     5,  # (17,2)        prize v2
     5,  # (17,1)+(18,1) prize + 1up
     5,  # (19,1)        surprise
     4,  # (23,1)        generic
     3,  # (15,1)        boss variant
     3,  # (25,1)        hidden surprise
     3,  # (28,1)        special A
     3,  # (29,1)        special B
     2,  # (28,1)+(29,1) both specials
    # ===== Mimic / wizard =====
     8,  # (6,1)         MIMIC
     8,  # (6,2)         WIZARD
     4,  # (6,1)+(16,3)  mimic + bait
     4,  # (6,2)+(16,3)  wizard + bait
     3,  # (6,2)+(15,1)  wizard + boss flag
     2,  # (32,1)        castle mimic
    # ===== Sucker / hidden =====
     2,  # (20,1)        sucker trap
     2,  # (3,1)         hidden empty
     4,  # (3,1)+(4,1)   hidden + key
     4,  # (3,1)+(21,1)  hidden + ability
     3,  # (3,1)+(30,1)  hidden + armor
     3,  # (3,1)+(18,1)  hidden + 1up
     2,  # (3,1)+(17,1)  hidden + prize
     3,  # (3,1)+(16,5)+(21,1)
     3,  # (3,1)+(16,5)+(18,1)
     2,  # (3,1)+(16,8)+(30,1)
    # ===== Empty =====
     3,  # ()            nothing
]

# Mimic / Wizard chest tags. The spawn editor exposes a direct PERCENT CHANCE
# (independent for each) that a wooden chest becomes a mimic or wizard.
MIMIC_TAG = (6, 1)
WIZARD_TAG = (6, 2)
# Each of mimic and wizard has TWO in-game versions: the plain one and a
# "boss/elite" variant flagged with (15, 1) (e.g. the end wizard 'Endwiz', the
# ice 'spinmimic'). The (15,1) variant appears across every world, so it's safe
# globally. When a chest rolls a mimic/wizard we pick uniformly between the two
# versions so both actually appear.
MIMIC_TAG_VERSIONS = [(MIMIC_TAG,), (MIMIC_TAG, (15, 1))]
WIZARD_TAG_VERSIONS = [(WIZARD_TAG,), (WIZARD_TAG, (15, 1))]


def content_options_without_specials():
    """CONTENT_TAG_OPTIONS / weights with every mimic- or wizard-bearing option
    removed — used as the 'normal' content roll when mimic/wizard are instead
    applied as explicit per-chest chances."""
    opts, weights = [], []
    for opt, w in zip(CONTENT_TAG_OPTIONS, CONTENT_TAG_WEIGHTS):
        if MIMIC_TAG in opt or WIZARD_TAG in opt:
            continue
        opts.append(opt)
        weights.append(w)
    return opts, weights

# Locked_Chest uses a DIFFERENT set of property indices than Wooden_Chest.
# Vanilla observations across all 5 worlds:
#   idx=4=1   → skeleton key drop
#   idx=6=1   → "is locked-chest" type marker (47/70 vanilla locked chests have it).
#               PRESERVED on chests that have it but never randomized.
#   idx=8     → drop-8 variants (vals 1, 3 seen)
#   idx=9     → drop-9 (vals 1, 2 seen)
#   idx=10=1  → generic
#   idx=12=1  → generic
#   idx=14=1  → generic
#   idx=15=1  → boss-style drop
#   idx=16=1  → drop variant
#   idx=17=1  → prize
#   idx=19=1  → drop
#   idx=21=1  → ability / sword upgrade
#   idx=22=1  → castle-only locked chest variant
#   idx=7=N   → gold count (NOT a content tag)
LOCKED_CHEST_CONTENT_INDICES = {3, 4, 8, 9, 10, 12, 14, 15, 16, 17, 19, 21, 22}
LOCKED_CHEST_GOLD_INDEX = 7  # gold count slot for locked chests
# Probability a locked chest carries a gold payout at all. Locked chests have
# no gold entry in LOCKED_CONTENT_TAG_OPTIONS (idx=7 is a plain count, not a
# content tag), so without this the gold pass would give every locked chest
# koins — the same flattening that made wooden chests feel gold-only. Roughly
# matches the ~22% gold share of the wooden content pool, nudged up because
# locked chests are meant to feel higher-value.
LOCKED_CHEST_GOLD_CHANCE = 0.3
LOCKED_CHEST_TYPE_MARKER = (6, 1)  # preserved if vanilla had it; never re-rolled

# Drop options for locked chests — each option is a tuple of (idx, val) tags.
# Locked chests are higher-value than wooden so the pool is biased toward
# multi-tag rewards rather than single drops. Each combo can stack with
# the gold count (idx=7) which is rolled separately by the gold pass.
LOCKED_CONTENT_TAG_OPTIONS = [
    # ===== Single-tag drops =====
    ((4, 1),),                          # skeleton key
    ((8, 1),),                          # drop-8
    ((8, 3),),                          # drop-8 variant 3
    ((9, 1),),                          # drop-9
    ((9, 2),),                          # drop-9 variant 2
    ((10, 1),),                         # generic
    ((12, 1),),                         # drop-12
    ((14, 1),),                         # generic
    ((15, 1),),                         # boss-style
    ((16, 1),),                         # drop variant
    ((17, 1),),                         # prize
    ((19, 1),),                         # drop
    ((21, 1),),                         # ability / sword upgrade
    ((22, 1),),                         # castle locked variant
    ((3, 1),),                          # hidden flag (no drop)
    # ===== Hidden combos =====
    ((3, 1), (4, 1)),                   # hidden + skeleton key
    ((3, 1), (21, 1)),                  # hidden + ability
    ((3, 1), (17, 1)),                  # hidden + prize
    ((3, 1), (12, 1)),                  # hidden + drop-12
    ((3, 1), (14, 1)),                  # hidden + generic
    ((3, 1), (19, 1)),                  # hidden + drop
    # ===== Reward combos =====
    ((4, 1), (21, 1)),                  # skeleton key + ability
    ((4, 1), (17, 1)),                  # skeleton key + prize
    ((21, 1), (17, 1)),                 # ability + prize (jackpot)
    ((21, 1), (15, 1)),                 # ability + boss-style
    ((21, 1), (12, 1)),                 # ability + drop-12
    ((19, 1), (21, 1)),                 # drop + ability
    ((17, 1), (14, 1)),                 # prize + generic
    ((9, 1), (12, 1)),                  # drop-9 + drop-12
    ((9, 2), (14, 1)),                  # drop-9 v2 + generic
    ((8, 1), (10, 1)),                  # drop-8 + generic
    ((8, 1), (12, 1)),                  # drop-8 + drop-12
    ((10, 1), (12, 1)),                 # generic + drop-12
    ((14, 1), (19, 1)),                 # generic + drop
    ((4, 1), (19, 1)),                  # skeleton key + drop
    ((4, 1), (10, 1)),                  # skeleton key + generic
    ((22, 1), (21, 1)),                 # castle variant + ability
    ((22, 1), (17, 1)),                 # castle variant + prize
    (),                                 # no content set
]
LOCKED_CONTENT_TAG_WEIGHTS = [
    # ===== Single-tag drops =====
    6,   # (4,1)   skeleton key
    6,   # (8,1)   drop-8
    3,   # (8,3)   drop-8 v3
    8,   # (9,1)   drop-9
    4,   # (9,2)   drop-9 v2
    5,   # (10,1) generic
    8,   # (12,1) drop-12
    8,   # (14,1) generic
    4,   # (15,1) boss-style
    4,   # (16,1) drop
    5,   # (17,1) prize
    4,   # (19,1) drop
    9,   # (21,1) ability/sword
    3,   # (22,1) castle variant
    3,   # (3,1)  hidden empty
    # ===== Hidden combos =====
    3,   # (3,1)+(4,1)
    4,   # (3,1)+(21,1)
    3,   # (3,1)+(17,1)
    3,   # (3,1)+(12,1)
    3,   # (3,1)+(14,1)
    2,   # (3,1)+(19,1)
    # ===== Reward combos =====
    4,   # (4,1)+(21,1)   skeleton key + ability
    3,   # (4,1)+(17,1)
    2,   # (21,1)+(17,1)  jackpot
    3,   # (21,1)+(15,1)
    3,   # (21,1)+(12,1)
    3,   # (19,1)+(21,1)
    3,   # (17,1)+(14,1)
    3,   # (9,1)+(12,1)
    3,   # (9,2)+(14,1)
    3,   # (8,1)+(10,1)
    3,   # (8,1)+(12,1)
    3,   # (10,1)+(12,1)
    3,   # (14,1)+(19,1)
    3,   # (4,1)+(19,1)
    3,   # (4,1)+(10,1)
    2,   # (22,1)+(21,1)
    2,   # (22,1)+(17,1)
    2,   # ()      nothing
]

# Ability IDs (idx=0 of an Ability entity, prop type 0x07 = u32). Each value
# corresponds to a different ability/power-up the player gets when collected.
#
# VERIFIED (found in vanilla PSX data, name read from instance_name):
#   ID    in-game name (vanilla label)
#   --    ----------------------------
#    4    Projectiles (axe throw)
#    5    Mask of Sorrow            (transformation power-up)
#   10    Shield (variant)
#   12    Shield
#   13    Shield Charge
#   15    Fire Sword  / "Flame Tongue"   (sword enchantment)
#   16    Ice Sword   / "Frostbiter"     (sword enchantment)
#   17    Sun Sword   / "Orange Sword"   (sword enchantment)
#   18    Armageddon
#   20    Magic Shield
#   22    Health (heart container)
#
# EXPERIMENTAL (NOT placed in any vanilla map, but the Prima strategy guide
# lists many more power-ups than the 11 above — Magic Bolt, Long Sword,
# Gold Seeker, Doomstrike, Wider Shockwave, Ring of Pain, Shield of
# Midas/Thunder/Storms, Throw Shield, Hovering Shield, Mighty Throw,
# Increased Armor, Mighty Blow, Second Strike, Furious Spin Attack).
# The engine's ability enum is contiguous, so the unused gap IDs
# (0,1,2,3,6,7,8,9,11,14,19,21) almost certainly map to those guide
# power-ups. We add the FULL 0..22 range so every power-up the guide
# describes can spawn. If any specific ID turns out to be a no-op or
# glitched in-game, remove it from GAME_ABILITY_POOL_EXPERIMENTAL.
GAME_ABILITY_POOL_VERIFIED = [
    4,   # Magic Bolt
    5,   # Masquerade (Mask of Sorrow)
    10,  # Smart Bomb
    12,  # Shield (equip / upgrade)
    13,  # Shield Stamina (+5)
    15,  # Fire Sword
    16,  # Ice Sword
    17,  # Sun Sword
    18,  # Armageddon Sword
    20,  # Magnetic Shield
    22,  # Full Health
]

# Gap IDs not used by any vanilla map — mapped (by elimination against the
# strategy guide's power-up list) to the remaining sword/shield power-ups.
GAME_ABILITY_POOL_EXPERIMENTAL = [
    0,   # Increase Sword Power
    1,   # Sword 720 Spin
    2,   # Double Slash
    3,   # Mighty Blow
    6,   # Return Shield
    7,   # Wide Shockwave
    8,   # Damage Shockwave
    9,   # Find Treasure
    11,  # Hover Shield
    14,  # Increase Armor
    19,  # Wind Shield
    21,  # Lightning Shield
]

# Full pool the randomizer rolls from. Experimental IDs included per user
# request so every power-up in the strategy guide can appear in-game.
GAME_ABILITY_POOL = GAME_ABILITY_POOL_VERIFIED + GAME_ABILITY_POOL_EXPERIMENTAL

# Ids that are legal to BAKE INTO A PLACED PICKUP when the user explicitly
# selects them in the Abilities & Skills editor. This is deliberately wider
# than GAME_ABILITY_POOL (which drives default random rolls):
#   23  Increase Throw     - real, measured ability
#   24  Sword Stamina (+5) - real, measured ability
# Vanilla maps never place 23/24, but the engine handles them fine, so the
# editor must be able to place them. Still ineligible on purpose:
#   25, 26      - no-ops (collecting them does nothing)
#   27..30      - the kisses (cutscene/story pickups, not abilities)
PLACEABLE_ABILITY_IDS = set(GAME_ABILITY_POOL) | {23, 24}

# Per-world ability pool.
#
# Per user request: every world rolls from the FULL ability pool, so any
# shield variant, any sword variant, and every other ability can spawn
# anywhere. The previous conservative filter limited each world to its
# vanilla-tested IDs to avoid "ghost" pickups whose assets the BEF didn't
# load — but the user has explicitly opted into the wider pool.
#
# All worlds share the same uniform-weight distribution, so e.g. Grave
# has equal odds of rolling Sun Sword (vanilla SWAMP-only), Ice Sword
# (vanilla ICE/UNDER), Shield Charge (vanilla UNDER), etc.
ABILITY_POOL_PER_WORLD: dict[str, list[int]] = {
    'grave':  list(GAME_ABILITY_POOL),
    'swamp':  list(GAME_ABILITY_POOL),
    'ice':    list(GAME_ABILITY_POOL),
    'under':  list(GAME_ABILITY_POOL),
    'castle': list(GAME_ABILITY_POOL),
}

# Equal weight on every VERIFIED id; experimental ids get a lower weight so
# the known sword/shield power-ups stay common while the unverified ones
# still appear regularly. If an experimental id turns out broken in-game,
# remove it from GAME_ABILITY_POOL_EXPERIMENTAL above.
_VERIFIED_SET = set(GAME_ABILITY_POOL_VERIFIED)
ABILITY_POOL_PER_WORLD_WEIGHTS: dict[str, list[int]] = {
    w: [3 if aid in _VERIFIED_SET else 1 for aid in p]
    for w, p in ABILITY_POOL_PER_WORLD.items()
}

# Enemy tier/class variant pool. The first property (idx=0) of certain enemies
# encodes the tier as a u32 (property type byte = 0x07). Higher tier = stronger
# variant.
#
# Tier assets are loaded by the engine globally, not per-world — so even tiers
# that vanilla never used in a given world DO work in-game (verified by user).
# The ranges below are the FULL set of working tiers per enemy type.
#
# CRITICAL: only enemies whose prop[0] is type byte 0x07 are tier-randomizable.
# Raven (0x37, prop[0] is type 0x0C trigger volume), Torso_Zombie (0x6A, type
# 0x04), Ghost (0x6B, type 0x01 bool), Goat_Devil (0x13, type 0x0B), Axe_Guard
# (0x16, type 0x0B), Crazed_Prisoner (0x58, type 0x0B), and Frozen_Zombie
# (0x10, type 0x0E) DO NOT belong here — touching their prop[0] corrupts
# unrelated data.
#
# Per-world tier tables: each world ships its own subset of enemy tier
# assets. Rolling an enemy onto a tier the BEF didn't load gives a broken /
# invisible / glitched enemy at runtime — e.g. Basic_Zombie tier 2 only
# loads in SWAMP and UNDER, so applying it in GRAVE yields a non-functional
# zombie. The ranges below mirror what vanilla actually used per world.
ENEMY_TIER_TYPES_PER_WORLD: dict[str, dict[int, tuple]] = {
    'castle': {
        0x47: ('Basic_Skeleton', [0, 1, 2]),
        0x39: ('SwordSkeleton',  [0, 1, 2]),  # confirmed working globally
        0x49: ('Bomb_Skeleton',  [0, 1, 2]),  # confirmed working globally
        0x54: ('Guard_Skeleton', [0, 1, 2]),  # confirmed working globally
        0x08: ('Plant_Monster',  [0, 1]),
    },
    'grave': {
        0x47: ('Basic_Skeleton', [0, 1, 2]),
        0x39: ('SwordSkeleton',  [0, 1, 2]),  # confirmed working globally
        0x48: ('Basic_Zombie',   [0, 1, 2]),  # tier 2 enabled per user request
                                              # ("basic zombies don't have a
                                              # second phase") — gives GRAVE
                                              # the splittable rotting form
        0x54: ('Guard_Skeleton', [0, 1, 2]),  # confirmed working globally
    },
    'ice': {
        0x47: ('Basic_Skeleton', [0, 1, 2]),
        0x39: ('SwordSkeleton',  [0, 1, 2]),  # confirmed working globally
        0x49: ('Bomb_Skeleton',  [0, 1, 2]),  # confirmed working globally
        0x54: ('Guard_Skeleton', [0, 1, 2]),  # confirmed working globally
        0x3A: ('PirateSkeleton', [0, 2]),
    },
    'swamp': {
        0x47: ('Basic_Skeleton', [0, 1, 2]),
        0x39: ('SwordSkeleton',  [0, 1, 2]),  # confirmed working globally
        0x48: ('Basic_Zombie',   [0, 1, 2]),
        0x54: ('Guard_Skeleton', [0, 1, 2]),  # confirmed working globally
        0x08: ('Plant_Monster',  [0, 1]),
    },
    'under': {
        0x47: ('Basic_Skeleton', [0, 1, 2]),
        0x39: ('SwordSkeleton',  [0, 1, 2]),  # confirmed working globally
        0x48: ('Basic_Zombie',   [0, 1, 2]),
        0x49: ('Bomb_Skeleton',  [0, 1, 2]),  # confirmed working globally
        0x54: ('Guard_Skeleton', [0, 1, 2]),  # confirmed working globally
    },
}

# Legacy alias — kept for any external imports. Full union of valid tiers per
# enemy across all worlds. NOT used for randomization; the per-world table is.
ENEMY_TIER_TYPES = {
    0x47: ('Basic_Skeleton', [0, 1, 2]),
    0x39: ('SwordSkeleton',  [0, 1, 2]),
    0x48: ('Basic_Zombie',   [0, 1, 2]),
    0x54: ('Guard_Skeleton', [0, 1]),
    0x49: ('Bomb_Skeleton',  [0, 1]),
    0x08: ('Plant_Monster',  [0, 1]),
    0x3A: ('PirateSkeleton', [0, 2]),
    # NOT TIERED (prop[0] is not a u32 tier — would corrupt other data):
    #   0x37 Raven, 0x6A Torso_Zombie, 0x6B Ghost, 0x13 Goat_Devil,
    #   0x16 Axe_Guard, 0x58 Crazed_Prisoner, 0x10 Frozen_Zombie,
    #   0x2F Axe_Skeleton, 0x1D Snowman, 0x0D Zombie_Crocodile,
    #   0x53 Dark_Knight, 0x5D Doomed_Soul, 0x27 Hammer_Devil
}
ENEMY_TIER_POOL = [0, 1, 2]  # legacy alias; per-enemy ranges live in ENEMY_TIER_TYPES
# Equal weights — each enemy has equal chance of being any of its valid tiers.
ENEMY_TIER_WEIGHTS = [1, 1, 1]


# Per-type tier weighting. Used to BIAS the tier roll for specific enemies
# whose vanilla second-form is visually distinctive and should appear often.
#
# Basic_Zombie (0x48): tier 2 is the rotten/decayed form — when cut in half
# the corpse spawns a Torso_Zombie. Vanilla GRAVE/ICE never used tier 2 (no
# asset loaded), but SWAMP and UNDER do, so we boost tier 2 there so the
# player actually encounters splittable zombies. Format: type_id ->
# {tier_value: weight}. Tiers absent from the per-world tier_pool are
# auto-removed at roll time.
ENEMY_TIER_WEIGHTS_PER_TYPE: dict[int, dict[int, int]] = {
    0x48: {0: 25, 1: 25, 2: 50},  # Basic_Zombie: 50% tier 2 (splittable)
}


# Harder mode: when True, reroll_enemy_tier forces every classed enemy to its
# TOP tier/class (e.g. Basic_Skeleton lvl 3, SwordSkeleton lvl 3, Basic_Zombie
# tier 2 / torso-splittable). Set by apply_harder_mode().
FORCE_MAX_TIER = False


def reroll_enemy_tier(rec: PsxRecord, rng: random.Random,
                      world: str | None = None,
                      tier_weights_per_type: dict | None = None) -> PsxRecord:
    """Roll a fresh tier (prop[0]) for a single enemy record if its type is
    tier-eligible. Returns a new PsxRecord with the new tier baked in, or
    the original record unchanged when:
      - the type isn't tier-eligible in the supplied world (or in any world
        when `world` is None — falls back to the legacy union table)
      - prop[0] isn't a u32 (different type byte — would corrupt other data)
      - the rolled tier already matches the current value

    Used both during the universal item randomizer (step 6) and after the
    final even-out pass in cli.py — every enemy that exists in the final
    file gets its tier rolled fresh from its own valid range.

    World-specific tier ranges keep us from rolling tiers the BEF didn't
    load — e.g. Basic_Zombie tier 2 only loads in SWAMP / UNDER, so applying
    it in GRAVE produces a non-functional zombie.

    Tier weighting: by default uses ENEMY_TIER_WEIGHTS_PER_TYPE (biases e.g.
    Basic_Zombie toward tier 2). A `tier_weights_per_type` override
    {type_id: {tier: weight}} (from the editable spawn config) takes
    precedence — letting the user set the % of each enemy class level. Tiers
    outside the world's valid pool are ignored; if every valid tier is
    weighted 0 the record's tier is left unchanged.
    """
    if world is not None:
        table = ENEMY_TIER_TYPES_PER_WORLD.get(world, {})
    else:
        table = ENEMY_TIER_TYPES
    if rec.type_id not in table:
        return rec
    if rec.prop_count < 1:
        return rec
    # Defensive: only proceed if prop[0] is actually a u32 tier (type 0x07).
    if rec.raw[0x228] != 0x07:
        return rec
    _, tier_pool = table[rec.type_id]
    if not tier_pool:
        return rec
    if FORCE_MAX_TIER:
        # Harder mode: force the enemy to its highest class/tier.
        new_tier = max(tier_pool)
        current = struct.unpack_from("<I", rec.raw, 0x228 + 8)[0]
        if current == new_tier:
            return rec
        new_raw = bytearray(rec.raw)
        struct.pack_into("<I", new_raw, 0x228 + 8, new_tier)
        return PsxRecord(
            offset=rec.offset, class_name=rec.class_name,
            instance_name=rec.instance_name, type_id=rec.type_id,
            instance_id=rec.instance_id, prop_count=rec.prop_count,
            pos_x=rec.pos_x, pos_y=rec.pos_y, pos_z=rec.pos_z,
            raw=bytes(new_raw),
        )
    wt_source = (tier_weights_per_type if tier_weights_per_type is not None
                 else ENEMY_TIER_WEIGHTS_PER_TYPE)
    weights_table = wt_source.get(rec.type_id)
    if weights_table:
        # Restrict to tiers allowed in this world AND with a positive weight.
        eligible = [t for t in tier_pool if weights_table.get(t, 0) > 0]
        if eligible:
            w = [weights_table[t] for t in eligible]
            new_tier = rng.choices(eligible, weights=w, k=1)[0]
        elif tier_weights_per_type is not None:
            # User explicitly zeroed every valid tier for this type — keep the
            # current tier rather than forcing a uniform roll.
            return rec
        else:
            new_tier = rng.choice(tier_pool)
    else:
        new_tier = rng.choice(tier_pool)
    current = struct.unpack_from("<I", rec.raw, 0x228 + 8)[0]
    if current == new_tier:
        return rec
    new_raw = bytearray(rec.raw)
    struct.pack_into("<I", new_raw, 0x228 + 8, new_tier)
    return PsxRecord(
        offset=rec.offset, class_name=rec.class_name,
        instance_name=rec.instance_name, type_id=rec.type_id,
        instance_id=rec.instance_id, prop_count=rec.prop_count,
        pos_x=rec.pos_x, pos_y=rec.pos_y, pos_z=rec.pos_z,
        raw=bytes(new_raw),
    )


# ============================================================================
# Enemy DEATH-DROP randomization (prop[5])
# ============================================================================
# Walking-melee enemies encode "what to drop on death" in prop[5]. Vanilla
# observed distribution across all 5 worlds (after surveying every PSX file):
#
#   Most enemies use type 0x07 (u32) with values 0-4:
#     0 = no drop (most common)
#     1 = drop A (varies by enemy — typically Skeleton_Key)
#     2 = drop B (typically Ability / power-up)
#     3 = drop C (typically Health / Bag_Of_Gold)
#     4 = drop D (rare — typically a specific gold/key combo)
#
#   Bomb_Skeleton (0x49) uses prop[5] type 0x01 (bool) with values 0/1.
#
# We randomize prop[5] with a controlled probability so most enemies still
# drop nothing (otherwise the player gets flooded with drops), but a portion of
# enemies drop something on death. The drop kind is rolled from the type's
# vanilla observed values (key / ability-sword-power / health / gold).
ENEMY_DROP_PROBABILITY = 0.35  # chance that any given enemy gets a non-zero drop


# Chest gold-amount distribution (coins dropped when a chest is opened).
# Module-level so harder mode can bias it toward "just 1 koin". Index = coin
# count (0..15), value = weight.
CHEST_GOLD_AMOUNTS = list(range(0, 16))  # 0 to 15 coins
CHEST_GOLD_WEIGHTS = [
    15,   # 0 coins  - empty stays plausible but uncommon
    25,   # 1 coin   - reduced from 70 (was top-heavy at 1)
    50,   # 2 coins  - peak
    45,   # 3 coins
    35,   # 4 coins
    25,   # 5 coins
    15,   # 6 coins
    10,   # 7 coins
     8,   # 8 coins
     6,   # 9 coins
     5,   # 10 coins
     4,   # 11 coins
     3,   # 12 coins
     3,   # 13 coins
     2,   # 14 coins
     2,   # 15 coins
]


def apply_harder_mode() -> dict:
    """Mutate the module's drop/weight tables in place to enable HARDER MODE.

    Idempotent-ish: intended to be called once at the start of a randomize run
    (the tool runs as a fresh process per invocation). Returns a summary dict.

    Changes (item/drop side — damage, boss-duplication and mimic/wizard chances
    are forced by the cli/iso orchestration):
      * Bag_Of_Gold / Gem / Full_Health / Extra_Life are removed from the item
        pool; their combined weight is given to Koins (GoldCoin).
      * Iron Keys, Collectors and skill/ability pickups are made much rarer.
      * Item->enemy conversion rate is increased a lot.
      * Enemies drop nothing far more often.
      * Every classed enemy spawns at its top class/tier.
      * Chests overwhelmingly drop just 1 koin.
    """
    global ENEMY_SHARE_TARGET, ENEMY_DROP_PROBABILITY, FORCE_MAX_TIER
    global CHEST_GOLD_WEIGHTS

    # 1. Disable Bag_Of_Gold (0x46), Gem (0x52), Full_Health (0x51),
    #    Extra_Life (0x4E) — give their combined weight to Koins (GoldCoin 0x21).
    moved = 0
    for tid in (0x46, 0x52, 0x51, 0x4E):
        moved += DEFAULT_ITEM_WEIGHTS.get(tid, 0)
        DEFAULT_ITEM_WEIGHTS[tid] = 0
    DEFAULT_ITEM_WEIGHTS[0x21] = DEFAULT_ITEM_WEIGHTS.get(0x21, 0) + moved

    # 2. Decrease the chance of Keys, Collectors and Skills/Abilities.
    DEFAULT_ITEM_WEIGHTS[0x65] = 8    # Skeleton_Key (Iron Key) — from 40
    DEFAULT_ITEM_WEIGHTS[0x24] = 4    # Collector       — from 25
    DEFAULT_ITEM_WEIGHTS[0x20] = 4    # Ability/skill   — from 28
    # Castle's boosted key supply is dialled down too (kept above the global
    # floor so the gating-heavy world stays beatable).
    castle = PER_WORLD_ITEM_WEIGHTS.get("castle")
    if castle is not None:
        castle[0x65] = 24

    # 5. Increase the chance of items turning into enemies (was 0.20). Harder
    #    mode also disables all "structure" world-objects below and routes
    #    their share here, so enemies dominate the non-koin/chest pool.
    ENEMY_SHARE_TARGET = 0.65

    # 5b. Disable ALL structures (smashables / decorations / containers / pop-
    #     ups / towers / graves / statues / torches). Their weight is removed
    #     from the pool; with the high ENEMY_SHARE_TARGET above that freed share
    #     is spawned as enemies instead.
    STRUCTURE_TYPES = (
        0x0A,  # Popup_Headstone
        0x01,  # Bone_Tower
        0x02,  # Bonus_Grave
        0xB0,  # Smashable_Torch
        0x30,  # Grave_Smashable_Glass
        0xA0,  # Coffin_Lid
        0xC9,  # Breakable_Rockwall
        0xAE,  # Underworld_Lantern
        0xD3,  # Breakable_TikkiTorch
        0x6C,  # CoinContainer
        0xC8,  # Spirit_Statue
        0xB5,  # Swamp_Coffin_Lid
        0xBB,  # Thorn_Vine
        0x17,  # Smashable_Snow_Pirate
        0xB1,  # Smashable_Ice_Wall
        0x7C,  # Ice_Plant
        0x1B,  # Popup_Ice
        0x18,  # Ice_Torch
        0xAA,  # Smashable_Door
        0xAD,  # Spirit_Torch
        0x4A,  # Globe
        0xBC,  # Smashable_Coffin
        0x50,  # Standing_Coffin
    )
    for tid in STRUCTURE_TYPES:
        DEFAULT_ITEM_WEIGHTS[tid] = 0
    # ...except the Monster_Generator: it's the one structure kept enabled (it
    # spawns enemy waves, which fits harder mode), and boosted so it's a real
    # presence now that the other structures are gone.
    DEFAULT_ITEM_WEIGHTS[0x5F] = 40  # Monster_Generator

    # 4b. Ghosts always spawn as Poltergeists (their top/"max" form). The Ghost
    #     variant flag is val=0 = Poltergeist, val=1 = Castle blue ghost; force
    #     100% Poltergeist.
    ENEMY_VARIANT_FLAGS[0x6B] = (0, [0, 1], [1, 0])

    # 6. Increase by a lot the chance of enemies dropping nothing (was 0.35;
    #    lower = more "nothing" since this is the chance of a NON-zero drop).
    ENEMY_DROP_PROBABILITY = 0.06

    # 4. Every classed enemy spawns at its max class/tier.
    FORCE_MAX_TIER = True

    # 7. Chests overwhelmingly drop just 1 koin.
    CHEST_GOLD_WEIGHTS = [
        4,    # 0 coins
        300,  # 1 coin   <- dominant
        10,   # 2 coins
        4,    # 3 coins
        2,    # 4 coins
        1,    # 5 coins
        0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    ]

    return {
        "koins_share_from_disabled": moved,
        "enemy_share_target": ENEMY_SHARE_TARGET,
        "enemy_drop_probability": ENEMY_DROP_PROBABILITY,
        "force_max_tier": FORCE_MAX_TIER,
    }

# Per-type drop value pools — vanilla-observed non-zero values per enemy.
# Empty list means the enemy never drops anything (safer to skip).
#
# Each entry is (values, weights) — sampled with rng.choices so the
# WEIGHTED distribution matches what vanilla actually used.
#
# CRITICAL FINDING: Axe_Skeleton (0x2F) is REMOVED from this map. Every
# vanilla in-level Axe_Skeleton has prop[5]=0 (no drop). Non-zero values
# only appear in HUB phase markers (P1_/P2_/P3_Axe_Skeleton in C_HUB,
# S_HUB, U_HUB) where they index into wave-spawning state, NOT into a
# drop kind. Setting prop[5]=1 on an in-level Axe_Skeleton makes the
# engine spawn a Skeleton_Key when the enemy dies — every time. So we
# leave Axe_Skeleton's prop[5] alone entirely. Same conservative rule
# applies to other types that carry prop[5] in HUB-only contexts.
#
# Vanilla per-type non-zero counts on IN-LEVEL records (HUBs excluded):
#   0x39 SwordSkeleton:  v1=18, v2=4, v3=6, v4=1
#   0x47 Basic_Skeleton: v1=17, v2=19, v3=17, v4=4
#   0x48 Basic_Zombie:   v1=22, v2=14, v3=3
#   0x3A PirateSkeleton: v1=2, v2=2, v3=3, v4=1
#   0x10 Frozen_Zombie:  v1=ALL
#   0x49 Bomb_Skeleton:  v1=ALL  (bool)
ENEMY_DROP_POOLS: dict[int, tuple[list[int], list[int]]] = {
    # Frozen_Zombie: vanilla shows ALL non-zero records use val=1. But that
    # means the drop is heavily key-biased. Cap it like the other zombies so
    # we get a varied 1/2/3 spread (key/ability/health) and most still drop
    # nothing per ENEMY_DROP_PROBABILITY.
    0x10: ([1, 2, 3],    [3, 8, 6]),          # Frozen_Zombie (key-rare)
    # Drop weights rebalanced so val=1 (Skeleton_Key drop) is RARE everywhere.
    # Vanilla had Basic_Skeleton key-drops at 30% of non-zero rolls and
    # SwordSkeleton at 62% — the user reported the resulting skeleton
    # population was dropping keys far too often (every melee skeleton
    # felt like a guaranteed key). Lowered val=1 weight to ~10% of the
    # type's non-zero pool everywhere.
    0x39: ([1, 2, 3, 4], [3, 12, 12, 4]),     # SwordSkeleton  (key-rare)
    0x3A: ([1, 2, 3, 4], [1, 4, 6, 2]),       # PirateSkeleton (key-rare)
    0x47: ([1, 2, 3, 4], [3, 22, 22, 6]),     # Basic_Skeleton (key-rare)
    0x48: ([1, 2, 3],    [3, 22, 14]),        # Basic_Zombie   (key-rare)
    0x49: ([1],          [1]),                # Bomb_Skeleton  (bool — keep)
    # NOT randomized:
    #   0x2F Axe_Skeleton — non-zero p5 only valid in HUB phase markers
    #     and force_enemy_props clamps p5=0 unconditionally.
    #   0x27 Hammer_Devil, 0x53 Dark_Knight — too few vanilla samples to
    #     justify; their non-zero values may be event-tied like Axe_Skel.
}


def reroll_enemy_drop(rec: PsxRecord, rng: random.Random) -> PsxRecord:
    """Roll a fresh death-drop kind (prop[5]) for an enemy record.

    With probability ENEMY_DROP_PROBABILITY, picks a value from the type's
    vanilla-weighted pool. Otherwise sets it to 0 (no drop).

    Skips if:
      - the enemy type isn't in ENEMY_DROP_POOLS
      - prop[5] isn't a u32 or bool (different type byte — would corrupt
        other data)
      - the rolled value already matches the current value

    Why this exists: cli.py's enemy randomizer uses a single template per
    type per world. Without re-rolling the drop, every enemy of that type
    inherits the template's drop value (so e.g. every Axe_Skeleton drops
    a Skeleton_Key because the template happened to come from a HUB record
    with prop[5]=1). This helper distributes drops across enemies more
    evenly so some drop keys, some drop abilities, some drop nothing,
    mirroring vanilla's per-type distribution so e.g. Axe_Skeletons don't
    constantly spawn Iron Keys.
    """
    if rec.type_id not in ENEMY_DROP_POOLS:
        return rec
    if rec.prop_count < 6:
        return rec
    eo = 0x228 + 5 * 0x20
    if eo + 0x20 > rec.size:
        return rec
    ptype = rec.raw[eo]
    # Bomb_Skeleton uses a 0x01 bool slot, others use 0x07 u32.
    if ptype not in (0x01, 0x07):
        return rec
    values, weights = ENEMY_DROP_POOLS[rec.type_id]
    if rng.random() < ENEMY_DROP_PROBABILITY and values:
        new_val = rng.choices(values, weights=weights, k=1)[0]
    else:
        new_val = 0
    current = struct.unpack_from("<I", rec.raw, eo + 8)[0]
    if current == new_val:
        return rec
    new_raw = bytearray(rec.raw)
    struct.pack_into("<I", new_raw, eo + 8, new_val)
    return PsxRecord(
        offset=rec.offset, class_name=rec.class_name,
        instance_name=rec.instance_name, type_id=rec.type_id,
        instance_id=rec.instance_id, prop_count=rec.prop_count,
        pos_x=rec.pos_x, pos_y=rec.pos_y, pos_z=rec.pos_z,
        raw=bytes(new_raw),
    )


# ============================================================================
# Hard prop-value forcing (NORMALIZATION)
# ============================================================================
# Some enemy props are NEVER safe to inherit from a template at non-zero
# values. The most-painful example: Axe_Skeleton's prop[5]. Vanilla in-level
# Axe_Skeletons all have p5=0; only HUB phase markers have p5={1,2,3,4} and
# those values index into wave-state, not a drop kind. If the cli.py template
# selector accidentally picks a HUB record (e.g. when the world has no in-
# level Axe_Skeletons in a given file), every cloned Axe_Skeleton inherits
# the HUB phase value and the engine treats it as "drop a Skeleton_Key on
# death" — every kill drops an Iron Key, which the player notices instantly.
#
# This dict says: for these (type, slot) pairs, always set the value to
# `force_val` AFTER all other randomization has run. It's belt-and-braces:
# even if a future code path forgets to re-roll a prop, this final pass
# clamps it to a vanilla-safe default.
#
# Format: type_id -> [(slot, force_val), ...]
ENEMY_FORCE_PROPS: dict[int, list[tuple[int, int]]] = {
    # Axe_Skeleton: every in-level vanilla record has p4=0 AND p5=0. Force
    # both to be safe. Vanilla shows 2 records with p4=1 (one in U_INTRO
    # 'gear1', one in C_HUB) which appear to be HUB phase markers / event
    # triggers, not regular drops. And HUB Axe_Skeletons have p5=1..4 which
    # the engine reads as "drop Skeleton_Key". Without this force, every
    # cloned Axe_Skeleton inherits the HUB template's drop value and drops
    # an Iron Key on death — extremely noticeable bug.
    0x2F: [(4, 0), (5, 0)],
    # Zombie_Crocodile and Swamp_Zombie: their templates often come from
    # S_HUB which carry prop[4]=3 (HUB phase 3 marker) or prop[4]=1 (wave
    # marker). When the cli.py template selector picks a HUB record (which
    # it does because the "neutral" filter looks for prop[5]==0 but these
    # types only have 5 props total — no prop[5] — so the fallback returns
    # recs[0] which is often the HUB record), every cloned zombie inherits
    # the wave-marker value. The engine then treats the zombie as part of
    # a HUB phase wave and skips spawning it during regular gameplay,
    # making both species effectively invisible in swamp levels. Force
    # prop[4]=0 so they spawn as regular enemies.
    0x0D: [(4, 0)],  # Zombie_Crocodile
    0x19: [(4, 0)],  # Swamp_Zombie
}


def force_enemy_props(rec: PsxRecord) -> PsxRecord:
    """Hard-clamp specific properties to vanilla-safe defaults.

    Runs as the LAST step of enemy randomization. No probability, no
    weighting — every record of the listed type gets the listed prop forced
    to the listed value, EXCEPT for vanilla HUB phase-marker records whose
    instance_name starts with 'P1_', 'P2_', 'P3_', 'P4_', 'FP1_'..'FP4_'.
    Those carry intentional non-zero phase-index values that the HUB
    spawn logic relies on; clamping them would break HUB level progression.

    The spec uses LOGICAL idx (the prop's idx field, not its physical slot
    in the record). The function walks every physical entry and writes the
    forced value into whichever slot has the matching idx field. This
    matters for enemies that skip indices — e.g. Swamp_Zombie has props at
    idx=1,2,3,4,5 (no idx=0), so its idx=4 lives at physical slot 3, not
    physical slot 4. Looking up by idx avoids that off-by-one trap.

    Used to break:
      - "every Axe_Skeleton drops a Skeleton_Key" (clamp 0x2F idx=5 to 0)
      - "every Zombie_Crocodile / Swamp_Zombie spawns invisible because
        cloned from S_HUB phase-marker template" (clamp idx=4 to 0)
    """
    spec = ENEMY_FORCE_PROPS.get(rec.type_id)
    if not spec:
        return rec
    # Skip vanilla HUB phase markers. Their prop[4] phase value is
    # required by the HUB wave spawner and must be preserved.
    name_lower = rec.instance_name.lower() if rec.instance_name else ""
    is_hub_phase = (
        name_lower.startswith(("p1_", "p2_", "p3_", "p4_",
                               "fp1_", "fp2_", "fp3_", "fp4_"))
    )
    if is_hub_phase:
        return rec
    new_raw = bytearray(rec.raw)
    changed = False
    for force_idx, force_val in spec:
        # Walk physical entries and find the one whose idx field matches.
        for k in range(rec.prop_count):
            eo = 0x228 + k * 0x20
            if eo + 0x20 > rec.size:
                break
            entry_idx = struct.unpack_from("<I", rec.raw, eo + 4)[0]
            if entry_idx != force_idx:
                continue
            # We only force u32 / bool slots. Other types (float / trigger
            # volume) are part of a different layout and writing here would
            # be corruption.
            ptype = rec.raw[eo]
            if ptype not in (0x01, 0x07):
                break
            current = struct.unpack_from("<I", new_raw, eo + 8)[0]
            if current != force_val:
                struct.pack_into("<I", new_raw, eo + 8, force_val)
                changed = True
            break  # found the entry, stop scanning for this force spec
    if not changed:
        return rec
    return PsxRecord(
        offset=rec.offset, class_name=rec.class_name,
        instance_name=rec.instance_name, type_id=rec.type_id,
        instance_id=rec.instance_id, prop_count=rec.prop_count,
        pos_x=rec.pos_x, pos_y=rec.pos_y, pos_z=rec.pos_z,
        raw=bytes(new_raw),
    )


# ============================================================================
# Per-enemy boolean VARIANT flags
# ============================================================================
# Some enemies carry a bool prop that flips a visual sub-type the engine
# renders differently. Verified by cross-referencing vanilla per-world data
# with in-game observations:
#
#   Ghost (0x6B), prop[0] type=0x01 bool:
#       Castle uses val=1, every other world uses val=0. Likely flips
#       between two visual sub-types ("Poltergeist" white floater vs the
#       Castle's blue ghost variant). Re-rolling per-record on a vanilla-
#       weighted distribution gives every world a small chance of either.
#
# REMOVED FLAGS (do not appear to do what we thought):
#
#   Basic_Zombie (0x48), prop[6]: original hypothesis was "splittable into
#       Torso_Zombie". Verified WRONG. The 2 vanilla Basic_Zombies that
#       actually have a paired Torso_Zombie (sharing instance_name) both
#       have prop[6]=0. Splitting is triggered by the engine spawning a
#       Torso_Zombie record bound to the same instance_name, not by a
#       per-record bool. Setting prop[6] to 0 or 1 changes nothing visible
#       in-game, so we no longer touch it.
#
#   Basic_Skeleton (0x47), prop[6]: same story — no observable effect tied
#       to it. Likely an unused / reserved slot.
#
# Pool format: type_id -> (slot, [values], [weights]).
ENEMY_VARIANT_FLAGS: dict[int, tuple[int, list[int], list[int]]] = {
    # (slot, values, weights).
    # Ghost: val=0 = Poltergeist (white floater, non-Castle), val=1 = Castle
    # blue ghost. Poltergeist weight nudged up (was 86) per user request so
    # Poltergeists are a bit more common without dominating entirely.
    0x6B: (0,  [0, 1], [90, 10]),  # Ghost: poltergeist (90%) vs castle/blue (10%)
}


def reroll_enemy_variant(rec: PsxRecord, rng: random.Random) -> PsxRecord:
    """Roll a fresh value for an enemy's boolean VARIANT flag — the per-
    record bool that selects between two visually-distinct sub-types
    (e.g. Ghost: Poltergeist vs Blue Ghost; Basic_Zombie: normal vs
    splittable into a Torso_Zombie).

    Why this exists: cli.py picks ONE template per (world, type) when
    swapping enemies. Every Ghost in Grave inherits the Grave template's
    bool (=0 Poltergeist) and every Ghost in Castle inherits the Castle
    template's bool (=1 Blue Ghost), so the post-randomize world is
    monocultured for that variant. Vanilla had the same monoculture per
    world, but with the randomizer mixing enemies across maps the bias
    sticks out — every Ghost in Grave is identical, every Zombie in
    Under is the non-splittable form, etc.

    Re-rolling per-record using vanilla-derived weights restores the
    natural Poltergeist/Blue mix and the splittable/non-splittable mix
    in every world.

    Defensive: only proceeds if the slot's prop type byte is 0x01 (bool).
    Anything else means the layout differs and writing here would corrupt
    other data, so the record is left alone.
    """
    if rec.type_id not in ENEMY_VARIANT_FLAGS:
        return rec
    slot, values, weights = ENEMY_VARIANT_FLAGS[rec.type_id]
    if rec.prop_count <= slot:
        return rec
    eo = 0x228 + slot * 0x20
    if eo + 0x20 > rec.size:
        return rec
    if rec.raw[eo] != 0x01:
        return rec
    new_val = rng.choices(values, weights=weights, k=1)[0]
    current = struct.unpack_from("<I", rec.raw, eo + 8)[0]
    if current == new_val:
        return rec
    new_raw = bytearray(rec.raw)
    struct.pack_into("<I", new_raw, eo + 8, new_val)
    return PsxRecord(
        offset=rec.offset, class_name=rec.class_name,
        instance_name=rec.instance_name, type_id=rec.type_id,
        instance_id=rec.instance_id, prop_count=rec.prop_count,
        pos_x=rec.pos_x, pos_y=rec.pos_y, pos_z=rec.pos_z,
        raw=bytes(new_raw),
    )

# Collector drop codes (idx=0 value).
#
# Each in-level collector rolls a random vanilla drop code. Values 0..10
# are all observed in vanilla (5 = the 'mystery' shop variant from
# S_SUB2). HUB phase-marker collectors (prop[1] != 0) are NOT rolled —
# those are level-select trackers, not player-collectable shops, and
# they're already source-protected from the universal pool.
COLLECTOR_DROP_POOL = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
COLLECTOR_DROP_WEIGHTS = [8, 12, 10, 6, 6, 12, 6, 6, 6, 6, 4]
# Mystery (5) gets a slight boost (12) over the other variants so it
# still feels like a regular shop encounter.


# ============================================================================
# Level_Column shuffle (HUB blind level select)
# ============================================================================
# In G_HUB.PSX, 4 Level_Columns let the player pick which level to enter.
# Each has prop[0] (idx=0, type=0x07) = the level ID to load (1, 2, or 3 for
# the 3 main levels; 0xFFFFFFFF for the locked/special one).
# We shuffle the level IDs among the 3 normal columns AND optionally rename
# the instance buffer to '???' so the player can't tell what level a column
# leads to before stepping on it. Blind level select.

LEVEL_COLUMN_TYPE = 0x59
LEVEL_COLUMN_LOCKED_VALUE = 0xFFFFFFFF  # don't touch columns with this value


# ============================================================================
# Monster_Generator parameter randomization
# ============================================================================
# Monster_Generators (0x5F) spawn enemies in waves. We can vary their props
# but NOT what enemy spawns (that's bound to the generator's instance name
# via external script logic, not stored in the PSX record).
# Tunable props per generator:
#   idx=0, idx=2: spawn radius (float, vanilla 25-55)
#   idx=1: timer between waves in ms (vanilla 1500-3000)
#   idx=7: monster count to spawn (vanilla 2-5)

MONSTER_GENERATOR_TYPE = 0x5F

# Chance that an ENEMY slot becomes a Monster_Generator instead of another
# enemy during the dedicated enemy randomizer pass. Deliberately small:
# generators spawn WAVES of enemies, so even a handful of extra portals per
# level changes the difficulty a lot more than a plain enemy swap does.
ENEMY_TO_GENERATOR_PROBABILITY = 0.02


def get_chest_content_tags(record: PsxRecord) -> tuple:
    """Read the chest's content tags. Uses the index set appropriate for the
    chest type (wooden chests use one set, locked chests use another)."""
    if record.type_id == 0x5B:
        valid_indices = LOCKED_CHEST_CONTENT_INDICES
    else:
        valid_indices = CHEST_CONTENT_INDICES
    tags = []
    for k in range(record.prop_count):
        eo = 0x228 + k * 0x20
        if eo + 0x20 > record.size:
            break
        idx = struct.unpack_from("<I", record.raw, eo + 4)[0]
        val = struct.unpack_from("<I", record.raw, eo + 8)[0]
        if idx in valid_indices and val != 0:
            tags.append((idx, val))
    return tuple(sorted(tags))


def chest_has_protected_tag(record: PsxRecord) -> bool:
    """Returns True if this chest carries a progression-critical tag and must
    never have its content re-rolled.

    Protection rules (verified against all 5 worlds):
      (31, 2) — gate-key marker: always blocks (only 'key1' in G_INTRO has it).
      (34, 1) — level-finisher / Grim Reaper Coin: blocks ONLY when it appears
               as the chest's sole content tag or paired only with (18, 1).
               'startgrave1' in G_SUB1 carries (34,1) alongside (15,1),(16,5),
               (18,1) and is a normal reward chest, not a level finisher.
    """
    if record.instance_name in PROTECTED_INSTANCE_NAMES:
        return True
    tags = set(get_chest_content_tags(record))
    if (31, 2) in tags:
        return True
    if (34, 1) in tags:
        # Only treat as a level-finisher if (34,1) is the *only* content tag,
        # or appears alongside at most (18,1) — the exact vanilla 'gate1'
        # pattern in G_INTRO.  Multi-reward chests (e.g. G_SUB1 'startgrave1'
        # which also carries (15,1) and (16,5)) are normal reward chests and
        # must not be blocked.
        other = tags - {(34, 1), (18, 1)}
        if not other:
            return True
    return False


def write_chest_with_tags(chest: PsxRecord, new_tags: tuple,
                          clear_indices: set | None = None) -> PsxRecord:
    """Write content tags into a chest. Clears all existing values at
    `clear_indices` first, then sets the values from `new_tags`.
    Defaults to wooden-chest indices for backward compatibility.
    """
    if clear_indices is None:
        clear_indices = CHEST_CONTENT_INDICES
    new_raw = bytearray(chest.raw)
    for k in range(chest.prop_count):
        eo = 0x228 + k * 0x20
        if eo + 0x20 > chest.size:
            break
        idx = struct.unpack_from("<I", new_raw, eo + 4)[0]
        if idx in clear_indices:
            struct.pack_into("<I", new_raw, eo + 8, 0)
    for new_idx, new_val in new_tags:
        for k in range(chest.prop_count):
            eo = 0x228 + k * 0x20
            if eo + 0x20 > chest.size:
                break
            idx = struct.unpack_from("<I", new_raw, eo + 4)[0]
            if idx == new_idx:
                struct.pack_into("<I", new_raw, eo + 8, new_val)
                break

    return PsxRecord(
        offset=chest.offset, class_name=chest.class_name,
        instance_name=chest.instance_name, type_id=chest.type_id,
        instance_id=chest.instance_id, prop_count=chest.prop_count,
        pos_x=chest.pos_x, pos_y=chest.pos_y, pos_z=chest.pos_z,
        raw=bytes(new_raw),
    )


def _activate_skeleton_key(rec: PsxRecord) -> PsxRecord:
    """Ensure a Skeleton_Key record has its 'collectable' flags set AND
    a unique instance_name so it doesn't bind to existing kill-groups.

    Vanilla observation (across all PSX files):
      - Only ONE key in the entire game has prop[4]=1 and prop[5]=1: the
        active/collectable one in G_INTRO ('Skeleton_Key_6967').
      - All other vanilla keys have those props at 0 — they're inactive
        anchor records spawned by chests/locks at runtime, not pickups.

    When the randomizer places a fresh Skeleton_Key at a coin/etc position,
    it usually copies a template from G_SUB1/U_INTRO whose flags are 0, so
    the new key spawns INVISIBLE/NON-COLLECTABLE. This helper:
      1. Forces idx=4=1 and idx=5=1 so the key is pickable.
      2. Replaces the instance_name with a unique 'Skeleton_Key_<id>' label.
         Without this, the key inherits the source record's name (from
         make_record_with_template) — which can match an existing
         kill-group like 'gatepath1' that ALSO has an Axe_Skeleton, an
         enemy, etc. The engine then ties the key spawn to that group's
         death-count, so killing the unrelated enemy spawns this key.
         Using a unique name puts the key in its OWN group, so it stays
         a passive collectable.
    """
    if rec.type_id != 0x65:
        return rec
    new_raw = bytearray(rec.raw)
    for k in range(rec.prop_count):
        eo = 0x228 + k * 0x20
        if eo + 0x20 > rec.size:
            break
        idx = struct.unpack_from("<I", new_raw, eo + 4)[0]
        if idx == 4 or idx == 5:
            struct.pack_into("<I", new_raw, eo + 8, 1)
    # Rewrite instance-name buffer to a unique label keyed off the
    # instance_id so it doesn't share a kill-group with vanilla enemies.
    new_name = f"Skeleton_Key_{rec.instance_id}"
    name_bytes = new_name.encode('ascii') + b'\x00'
    # Buffer is at offset 0x100 (NAME_BUFFER_SIZE), 256 bytes
    new_raw[0x100:0x100 + 256] = name_bytes + b'\x00' * (256 - len(name_bytes))
    return PsxRecord(
        offset=rec.offset, class_name=rec.class_name,
        instance_name=new_name, type_id=rec.type_id,
        instance_id=rec.instance_id, prop_count=rec.prop_count,
        pos_x=rec.pos_x, pos_y=rec.pos_y, pos_z=rec.pos_z,
        raw=bytes(new_raw),
    )


def _boost_raven_aggro(rec: PsxRecord, radius: float = 80.0) -> PsxRecord:
    """Expand a Raven's sight/patrol trigger volume so it actually engages
    the player from a normal distance.

    Raven (type 0x37) has a single property at prop[0]: a 0x0C "trigger
    volume" entry encoding (cx, cy, cz, sx, sy, sz). Vanilla Ravens ship
    with sx in the 15–38 range and sy=sz=0 — the AI only "wakes up" when
    the player crosses inside that small zone, so most Ravens feel like
    decorations until you walk directly underneath them.

    This helper forces sx, sy, sz all to `radius` (default 80) so the
    Raven detects the player from a much wider range and behaves like the
    other walking-melee enemies.
    """
    if rec.type_id != 0x37 or rec.prop_count < 1:
        return rec
    eo = 0x228  # prop[0]
    if eo + 0x20 > rec.size:
        return rec
    if rec.raw[eo] != 0x0C:
        return rec  # not a trigger volume — bail
    new_raw = bytearray(rec.raw)
    # Trigger entry layout: [type:1][3 pad][idx:4][cx,cy,cz, sx,sy,sz: 6 floats][2 pad]
    # Size lives at eo + 8 + 12 = eo + 20 (sx,sy,sz)
    struct.pack_into('<3f', new_raw, eo + 20, radius, radius, radius)
    return PsxRecord(
        offset=rec.offset, class_name=rec.class_name,
        instance_name=rec.instance_name, type_id=rec.type_id,
        instance_id=rec.instance_id, prop_count=rec.prop_count,
        pos_x=rec.pos_x, pos_y=rec.pos_y, pos_z=rec.pos_z,
        raw=bytes(new_raw),
    )


def _activate_gold_key(rec: PsxRecord) -> PsxRecord:
    """Ensure a Gold_Key record has its 'collectable' flag set AND a
    unique instance_name so it doesn't bind to existing kill-groups.

    Vanilla Gold_Key records are inactive anchors with prop[0] (idx=4) = 0.
    The engine flips that to 1 at runtime once the player clears the
    Gold_Key's kill-group (a chest opened, all enemies in a room killed,
    etc). When the randomizer drops a fresh Gold_Key at a coin/etc slot,
    there's no kill-group bound to it, so we have to force the flag on
    so the key spawns immediately collectable.

    Same kill-group hazard as _activate_skeleton_key: the source record's
    instance_name is kept by make_record_with_template, so a new Gold_Key
    might inherit an enemy's group name. Replace it with a unique
    'Gold_Key_<id>' label so it stays a passive collectable.
    """
    if rec.type_id != 0x2B or rec.prop_count < 1:
        return rec
    new_raw = bytearray(rec.raw)
    for k in range(rec.prop_count):
        eo = 0x228 + k * 0x20
        if eo + 0x20 > rec.size:
            break
        idx = struct.unpack_from("<I", new_raw, eo + 4)[0]
        if idx == 4:
            struct.pack_into("<I", new_raw, eo + 8, 1)
    # Rewrite instance-name buffer to a unique label.
    new_name = f"Gold_Key_{rec.instance_id}"
    name_bytes = new_name.encode('ascii') + b'\x00'
    new_raw[0x100:0x100 + 256] = name_bytes + b'\x00' * (256 - len(name_bytes))
    return PsxRecord(
        offset=rec.offset, class_name=rec.class_name,
        instance_name=new_name, type_id=rec.type_id,
        instance_id=rec.instance_id, prop_count=rec.prop_count,
        pos_x=rec.pos_x, pos_y=rec.pos_y, pos_z=rec.pos_z,
        raw=bytes(new_raw),
    )


# Files that must always have a Gold_Key waiting at the player's start.
# Per user request — these named levels should drop one Gold_Key right
# ahead of the player as they enter the map:
#   "Watery Grave"       — swamp locked column. Its exact PSX file is
#       ambiguous (the locked column doesn't map 1:1 to a sublevel file),
#       so we cover BOTH the swamp intro (S_INTRO) and the first swamp
#       sublevel (S_SUB1) to be certain Watery Grave is hit.
#   S_SUB3.PSX  -> "Quick and the Dead"  (swamp level 3)
#   C_INTRO.PSX -> "The Siege"           (castle intro / locked column)
#   C_SUB2.PSX  -> "Dungeon of Despair"  (castle level)
START_GOLD_KEY_FILES = {
    "S_INTRO.PSX": "Watery Grave (intro candidate)",
    "S_SUB1.PSX": "Watery Grave (sublevel candidate)",
    "S_SUB3.PSX": "Quick and the Dead",
    "C_INTRO.PSX": "The Siege",
    "C_SUB2.PSX": "Dungeon of Despair",
    "G_INTRO.PSX": "Grave Danger (first level) — start Gold_Key per user request",
}

# Files where ALL chest / ability / kill-group protections are LIFTED, so
# every chest and ability/skill on the map is fully randomizable (per user
# request for the first Grave level). NOTE: this also unprotects that level's
# gate-key ('key1') and level-finisher ('gate1') chests, so they can roll into
# anything — a Gold_Key is force-placed at the start to compensate.
LIFT_PROTECTION_FILES = {"G_INTRO.PSX"}

# Per-file EXTRA key injections (beyond the start key). Each entry says how
# many extra Gold_Keys and Skeleton_Keys to scatter through the level, by
# recycling filler pickups spread along the level so the keys land at
# different points (start / mid / late thirds), not all bunched up.
#
# Dungeon of Despair (C_SUB2) is a long gated level whose final 'Exit' gate
# needs a Gold_Key; the randomizer can disturb the vanilla key chain, so we
# guarantee extra Gold_Keys (one mid-level) plus extra Skeleton_Keys.
EXTRA_KEY_FILES: dict[str, dict[str, int]] = {
    "C_SUB2.PSX": {"gold_keys": 3, "skeleton_keys": 5},
}

# Per-file FIXED-POSITION Gold_Keys. Some levels have a specific gate that
# soft-locks if its key isn't reachable. We guarantee a Gold_Key by
# recycling the nearest filler pickup to the listed (x,y,z) target and
# moving it onto a reachable nearby pickup's spot.
#
# C_INTRO "The Siege": the catapult Lock_Box ('catapult_id30') at
# ~(156,-65,125) needs a key. The 'bigjump' coin trail leads right up to
# it, so we drop a Gold_Key on that path just before the catapult.
FIXED_GOLD_KEY_TARGETS: dict[str, list[tuple[float, float, float]]] = {
    "C_INTRO.PSX": [(94.2, -75.0, 163.7)],  # on the bigjump trail, pre-catapult
    # C_SUB2 "Dungeon of Despair": the final gate sits in the end cell room
    # near the Exit (~714,0,-58). Reachable 'cell' coins cluster at ~700,0,-18,
    # so we guarantee a Gold_Key right there for the last gate.
    "C_SUB2.PSX": [(701.7, 0.0, -20.5)],  # end cell room, by the final gate
}


def _make_gold_key_at(template: PsxRecord, source: PsxRecord,
                      px: float, py: float, pz: float,
                      unique_id: int) -> PsxRecord:
    """Build a collectable Gold_Key record from `template`, placed at the
    explicit world position (px,py,pz), reusing `source`'s record offset
    so it slots into the file in place of an existing record.

    Differs from make_record_with_template (which copies position FROM the
    source) — here we set an ARBITRARY position so the key lands exactly at
    the player's start, regardless of which item slot we recycled. Forces
    the collectable flag (idx=4=1) and a unique instance_name so the key is
    immediately pickable and not tied to any kill-group.
    """
    raw = bytearray(template.raw)
    # Write the explicit position into the entity header (prop region start
    # +0x10 holds the 3 position floats; see PsxRecord layout in psx.py).
    struct.pack_into("<3f", raw, RECORD_HEADER_SIZE + 0x10, px, py, pz)
    # Force collectable flag (idx=4 = 1).
    prop_count = template.prop_count
    for k in range(prop_count):
        eo = 0x228 + k * 0x20
        if eo + 0x20 > len(raw):
            break
        idx = struct.unpack_from("<I", raw, eo + 4)[0]
        if idx == 4:
            struct.pack_into("<I", raw, eo + 8, 1)
    # Unique instance name so the key isn't bound to a kill-group.
    new_name = f"Gold_Key_start_{unique_id}"
    name_bytes = new_name.encode("ascii") + b"\x00"
    raw[0x100:0x100 + 256] = name_bytes + b"\x00" * (256 - len(name_bytes))
    return PsxRecord(
        offset=source.offset, class_name=template.class_name,
        instance_name=new_name, type_id=0x2B,
        instance_id=source.instance_id, prop_count=prop_count,
        pos_x=px, pos_y=py, pos_z=pz,
        raw=bytes(raw),
    )


# ============================================================================
# Player-spawn (Maximo start location) randomization
# ============================================================================
# The player's spawn coordinate is a float triple in the level header (see
# PsxFile.player_spawn_offset). This randomizer relocates the spawn to the
# position of a random physically-placed entity — an item, structure, or
# enemy — so Maximo starts the level somewhere unexpected.
PLAYER_SPAWN_DEST_TYPES = set(UNIVERSAL_ITEM_POOL) | set(ENEMY_DESTINATIONS)


def randomize_player_spawn(psx: PsxFile, rng: random.Random) -> dict[int, bytes]:
    """Pick a random item/structure/enemy position and return a header patch
    {offset: packed_xyz} that moves Maximo's spawn there. Returns {} when the
    spawn offset is out of range or the level has no eligible entities."""
    off = psx.player_spawn_offset
    if off < 0 or off + 12 > len(psx.raw):
        return {}
    cur = psx.get_player_spawn()
    candidates = [r for r in psx.records if r.type_id in PLAYER_SPAWN_DEST_TYPES]
    if not candidates:
        candidates = list(psx.records)
    # Drop any candidate sitting exactly on the current spawn so the location
    # actually changes when possible.
    if cur is not None:
        moved = [r for r in candidates
                 if (r.pos_x, r.pos_y, r.pos_z) != (cur[0], cur[1], cur[2])]
        if moved:
            candidates = moved
    if not candidates:
        return {}
    pick = rng.choice(candidates)
    payload = struct.pack("<fff", pick.pos_x, pick.pos_y, pick.pos_z)
    return {off: payload}


# ============================================================================
# Level-exit (End_Of_Sublevel_Column) location randomization
# ============================================================================
# The level exit is entity type 0x4D. It stays in HAZARD_PROTECTED_AS_SOURCE —
# it is never type-swapped, deleted or replaced. This randomizer only MOVES it:
# the column and a designer-placed item/structure TRADE positions, so both end
# up in known-reachable spots and neither overlaps the other.
EXIT_COLUMN_TYPE = 0x4D

# Eligible trade partners: designer-placed items / structures / smashables.
# Enemies are excluded (they patrol and can be randomized into other types by
# the enemy pass) as are scripted props and hazards which must stay exactly
# where the level designer put them.
EXIT_SWAP_PARTNER_TYPES = (
    set(UNIVERSAL_ITEM_POOL) - set(ENEMY_DESTINATIONS) - HAZARD_PROTECTED_AS_SOURCE
)


def move_record_to(rec: PsxRecord, px: float, py: float, pz: float) -> PsxRecord:
    """Return a copy of `rec` placed at (px,py,pz). Type, name, ids, property
    entries and record SIZE are all untouched — only the position triple in
    the entity header changes."""
    raw = bytearray(rec.raw)
    struct.pack_into("<3f", raw, RECORD_HEADER_SIZE + 0x10, px, py, pz)
    return PsxRecord(
        offset=rec.offset, class_name=rec.class_name,
        instance_name=rec.instance_name, type_id=rec.type_id,
        instance_id=rec.instance_id, prop_count=rec.prop_count,
        pos_x=px, pos_y=py, pos_z=pz,
        raw=bytes(raw),
    )


def randomize_exit_location(psx: PsxFile, rng: random.Random) -> dict[int, PsxRecord]:
    """Relocate every End_Of_Sublevel_Column (0x4D) in `psx` by SWAPPING its
    position with a random designer-placed item/structure in the same map.

    Returns a {record_offset: PsxRecord} replacement dict (empty when the map
    has no exit column or no eligible partner). Records keep their type and
    size, so the file layout is unchanged.
    """
    exits = [r for r in psx.records if r.type_id == EXIT_COLUMN_TYPE]
    if not exits:
        return {}

    prot = get_progression_protected_names(psx)
    used: set[int] = {r.offset for r in exits}
    out: dict[int, PsxRecord] = {}

    for ex in exits:
        partners = [
            r for r in psx.records
            if r.type_id in EXIT_SWAP_PARTNER_TYPES
            and r.offset not in used
            and (r.instance_name or "") not in prot
            and (r.pos_x, r.pos_y, r.pos_z) != (ex.pos_x, ex.pos_y, ex.pos_z)
        ]
        if not partners:
            continue
        pick = rng.choice(partners)
        used.add(pick.offset)
        out[ex.offset] = move_record_to(ex, pick.pos_x, pick.pos_y, pick.pos_z)
        out[pick.offset] = move_record_to(pick, ex.pos_x, ex.pos_y, ex.pos_z)
    return out


def swap_spawn_with_exit(psx: PsxFile) -> tuple[dict[int, bytes], dict[int, PsxRecord]]:
    """Two-way swap between Maximo's level-start position and the level-exit
    column (0x4D): the spawn header is set to the column's position and the
    column is moved to the old spawn position.

    Deterministic, so no rng is needed. Returns
    (header_patch {offset: packed_xyz}, record_replacements {offset: PsxRecord}).
    Returns ({}, {}) when the map has no exit column, when the spawn offset is
    out of range, or when both positions already match. Only the FIRST 0x4D
    record participates — any additional columns are left untouched.
    """
    off = psx.player_spawn_offset
    if off < 0 or off + 12 > len(psx.raw):
        return {}, {}
    cur = psx.get_player_spawn()
    if cur is None:
        return {}, {}
    ex = next((r for r in psx.records if r.type_id == EXIT_COLUMN_TYPE), None)
    if ex is None:
        return {}, {}
    sx, sy, sz = cur[0], cur[1], cur[2]
    if (ex.pos_x, ex.pos_y, ex.pos_z) == (sx, sy, sz):
        return {}, {}
    header_patch = {off: struct.pack("<fff", ex.pos_x, ex.pos_y, ex.pos_z)}
    return header_patch, {ex.offset: move_record_to(ex, sx, sy, sz)}


# Harmless destination preference per record size for gate removal. A plain
# decoration / pickup is preferred so the slot becomes visually quiet scenery
# instead of another interactive prop. Anything else in the size's
# SAME_SIZE_GROUPS entry is used as a fallback.
GATE_REMOVAL_PREFERRED = {
    0x228: (0x02,),  # Bonus_Grave  — all gates except Lift_Gate are 0x228
    0x248: (0x21,),  # GoldCoin     — Lift_Gate (0x41) is 0x248
}
# Last-resort same-size decorations, used ONLY when a template exists in the
# SAME FILE. SWAMP is the case that needs this: no swamp map contains ANY
# SAME_SIZE_GROUPS[0x228] member, so without this tier every swamp gate would
# be skipped and swamp levels would stay blocked. These types sit in
# HAZARD_PROTECTED_AS_SOURCE because they form ORDERED puzzle chains (they are
# unsafe to MOVE), but as a retype DESTINATION they are plain scenery. Only
# same-file templates are allowed here so the mesh is guaranteed to be in that
# world's BEF.
GATE_REMOVAL_LAST_RESORT = {
    # Breakable_TikkiTorch / Spirit_Statue / Coffin_Lid (SWAMP),
    # Underworld_Lantern (UNDER — the only 0x228 prop under-world maps carry).
    0x228: (0xD3, 0xC8, 0xB5, 0xAE),
}


def remove_all_gates(
    psx: PsxFile,
    templates: dict[int, PsxRecord] | None = None,
) -> dict[int, PsxRecord]:
    """Retype every gate record in `psx` into a harmless same-size prop.

    Used by the spawn<->exit swap: starting Maximo at the level exit puts him
    on the far side of the locked gates, so backtracking is blocked and the
    level cannot be finished unless every gate is gone.

    Records can never be DELETED (the records section has a fixed layout and
    the count field / post-records pointer describe it), so each gate is
    RETYPED in place via `make_record_with_template`. The destination comes
    from SAME_SIZE_GROUPS for the gate's own size, preferring a plain
    decoration/pickup, and is only used when a template for it exists in this
    file or in the caller-supplied cross-file `templates` dict. A gate with no
    usable template is left untouched (the caller reports it as skipped). The
    destination is never larger than the gate, so the records section can
    only shrink.

    RISK — scripted gates: Mausoleum_Gate (0x72), Swamp_Locked_Gate (0xC0),
    Swamp_Crypt_Gate (0xDB) and Graveintro_Locked_Swinging_Gate (0xE2) are in
    HAZARD_PROTECTED_AS_SOURCE because level scripts reference their
    instance_names. Retyping them removes the script's target, so instead of
    an open path the level can hang in infinite loading. They are removed here
    ANYWAY, by design of this option — it is the same class of trade-off the
    option's own "can soft lock the game" warning describes.

    Returns a {record_offset: PsxRecord} replacement dict; {} when the map has
    no gates.
    """
    templates = templates or {}
    targets = [r for r in psx.records if r.type_id in GATE_TYPES]
    if not targets:
        return {}

    tpl_cache: dict[int, PsxRecord | None] = {}

    def template_for(tid: int) -> PsxRecord | None:
        if tid not in tpl_cache:
            recs = psx.find_records_by_type(tid)
            tpl_cache[tid] = recs[0] if recs else templates.get(tid)
        return tpl_cache[tid]

    def local_template_for(tid: int) -> PsxRecord | None:
        recs = psx.find_records_by_type(tid)
        return recs[0] if recs else None

    repl: dict[int, PsxRecord] = {}
    for r in targets:
        order: list[tuple[int, bool]] = []  # (type_id, same_file_only)
        for tid in (GATE_REMOVAL_PREFERRED.get(r.size, ())
                    + SAME_SIZE_GROUPS.get(r.size, ())):
            if tid in GATE_TYPES or any(t == tid for t, _ in order):
                continue
            order.append((tid, False))
        for tid in GATE_REMOVAL_LAST_RESORT.get(r.size, ()):
            if tid in GATE_TYPES or any(t == tid for t, _ in order):
                continue
            order.append((tid, True))
        for tid, same_file_only in order:
            tpl = local_template_for(tid) if same_file_only else template_for(tid)
            if tpl is None or tpl.size > r.size:
                continue  # never grow a record
            repl[r.offset] = make_record_with_template(tpl, r, r.offset)
            break
    return repl


def place_start_gold_key(
    psx: PsxFile,
    fname: str,
    gold_key_template: PsxRecord,
    existing_repl: dict[int, PsxRecord] | None = None,
) -> dict[int, PsxRecord]:
    """Guarantee a collectable Gold_Key near the player's start in `fname`.

    Only acts on the named levels in START_GOLD_KEY_FILES. Finds the cluster
    of 'start'-named records (the player spawn area), then recycles the
    NEAREST safe COLLECTABLE PICKUP (coin / gem / bag) into a Gold_Key,
    KEEPING that pickup's own world position. Pickups are guaranteed to sit
    in reachable, walkable space (the level designer placed them for the
    player to grab), so the key won't end up buried in geometry — which was
    the risk with snapping it to the raw start-centroid (the centroid can
    land inside a wall / under the floor).

    Falls back to a nearby smashable/decoration's position if no pickup is
    close. Returns a {offset: PsxRecord} replacement dict (possibly empty).
    Runs AFTER the main randomization so it overrides whatever that slot
    rolled into.
    """
    if fname.upper() not in {k.upper() for k in START_GOLD_KEY_FILES}:
        return {}
    if gold_key_template is None:
        return {}

    existing_repl = existing_repl or {}

    starts = [r for r in psx.records
              if (r.instance_name or "").lower().startswith("start")]
    if not starts:
        return {}
    cx = sum(r.pos_x for r in starts) / len(starts)
    cy = sum(r.pos_y for r in starts) / len(starts)
    cz = sum(r.pos_z for r in starts) / len(starts)

    prot = get_progression_protected_names(psx)

    def d2(r):
        return (r.pos_x - cx) ** 2 + (r.pos_y - cy) ** 2 + (r.pos_z - cz) ** 2

    # We recycle a filler slot's RECORD (so we don't grow the file), but we
    # PLACE the key at the position of the nearest 'start'-named record —
    # those sit exactly in the player's spawn area (enemies/items the player
    # meets on entry), so the key lands right in front of them. Using a
    # faraway pickup's own position (the previous approach) could drop the
    # key hundreds of units from spawn in big levels (e.g. The Siege).
    start_anchor = min(starts, key=d2)  # nearest start record to centroid
    kx, ky, kz = start_anchor.pos_x, start_anchor.pos_y, start_anchor.pos_z

    # PRIMARY: collectable floating pickups — safe filler to recycle.
    PICKUPS = {0x21, 0x46, 0x52, 0x4E, 0x51, 0x0A}  # coin, bag, gem, 1up, full-health, headstone
    # FALLBACK: ground decorations / smashables.
    DECOR = {0x02, 0xB0, 0x30, 0xA0, 0xC9, 0xAE, 0xD3, 0xC8, 0xB5, 0x17,
             0xB1, 0x18, 0xAA, 0xAD, 0x4A, 0xBB, 0x7C, 0x1B, 0x6C}

    def pick(types):
        c = [r for r in psx.records
             if r.type_id in types and r.size <= gold_key_template.size
             and r.instance_name not in prot]
        c.sort(key=d2)
        return c[0] if c else None

    victim = pick(PICKUPS) or pick(DECOR)
    if victim is None:
        return {}

    # Recycle the victim's record slot, but PLACE the key at the spawn anchor.
    new_key = _make_gold_key_at(
        gold_key_template, victim,
        kx, ky, kz,
        unique_id=victim.instance_id,
    )
    return {victim.offset: new_key}


def _make_skeleton_key_at(template: PsxRecord, source: PsxRecord,
                          unique_id: int) -> PsxRecord:
    """Build a collectable Skeleton_Key from `template`, recycling `source`'s
    record slot and KEEPING source's (reachable) position. Forces the
    collectable flags (idx=4=1, idx=5=1) and a unique instance_name so it's
    pickable and not bound to a kill-group."""
    raw = bytearray(template.raw)
    # Keep source position.
    struct.pack_into("<3f", raw, RECORD_HEADER_SIZE + 0x10,
                     source.pos_x, source.pos_y, source.pos_z)
    for k in range(template.prop_count):
        eo = 0x228 + k * 0x20
        if eo + 0x20 > len(raw):
            break
        idx = struct.unpack_from("<I", raw, eo + 4)[0]
        if idx in (4, 5):
            struct.pack_into("<I", raw, eo + 8, 1)
    new_name = f"Skeleton_Key_extra_{unique_id}"
    nb = new_name.encode("ascii") + b"\x00"
    raw[0x100:0x100 + 256] = nb + b"\x00" * (256 - len(nb))
    return PsxRecord(
        offset=source.offset, class_name=template.class_name,
        instance_name=new_name, type_id=0x65,
        instance_id=source.instance_id, prop_count=template.prop_count,
        pos_x=source.pos_x, pos_y=source.pos_y, pos_z=source.pos_z,
        raw=bytes(raw),
    )


def place_extra_keys(
    psx: PsxFile,
    fname: str,
    gold_key_template: PsxRecord,
    skeleton_key_template: PsxRecord,
    avoid_offsets: set[int] | None = None,
) -> dict[int, PsxRecord]:
    """Scatter EXTRA Gold_Keys / Skeleton_Keys through a level (per
    EXTRA_KEY_FILES), recycling filler pickups spread across the level so
    the keys land at different points (e.g. one mid-level) rather than all
    bunched at the start.

    Recycled slots are chosen from collectable pickups (coins/gems/bags)
    which sit in reachable space. To spread them out we sort candidates by
    their X coordinate and pick evenly across the level's length, skipping
    any offset already used (avoid_offsets, e.g. the start key).
    """
    cfg = EXTRA_KEY_FILES.get(fname.upper()) or EXTRA_KEY_FILES.get(fname)
    if not cfg:
        return {}
    avoid = set(avoid_offsets or set())
    prot = get_progression_protected_names(psx)

    PICKUPS = {0x21, 0x46, 0x52, 0x4E, 0x51, 0x0A}
    cands = [r for r in psx.records
             if r.type_id in PICKUPS and r.instance_name not in prot
             and r.offset not in avoid]
    if not cands:
        return {}
    # Sort by X so we can pick spots spread along the level length.
    cands.sort(key=lambda r: r.pos_x)

    repl: dict[int, PsxRecord] = {}
    used: set[int] = set()

    def take_spread(n, frac_lo, frac_hi):
        """Pick up to n candidates whose X falls in the [lo,hi] fraction of
        the level length, avoiding already-used slots."""
        if not cands:
            return []
        picked = []
        lo_i = int(len(cands) * frac_lo)
        hi_i = max(lo_i + 1, int(len(cands) * frac_hi))
        window = [c for c in cands[lo_i:hi_i] if c.offset not in used]
        # Even stride through the window.
        if not window:
            window = [c for c in cands if c.offset not in used]
        if not window:
            return []
        stride = max(1, len(window) // n)
        for i in range(0, len(window), stride):
            picked.append(window[i])
            if len(picked) >= n:
                break
        return picked

    # Gold keys: spread across the BACK HALF of the level (mid → exit) so one
    # lands mid-level and another near the final gate (Dungeon of Despair's
    # 'Exit' gate sits at the far end).
    n_gold = cfg.get("gold_keys", 0)
    if n_gold and gold_key_template is not None:
        for i, v in enumerate(take_spread(n_gold, 0.45, 1.0)):
            used.add(v.offset)
            repl[v.offset] = _make_gold_key_at(
                gold_key_template, v, v.pos_x, v.pos_y, v.pos_z,
                unique_id=v.instance_id,
            )

    # Skeleton keys: spread across the whole level.
    n_sk = cfg.get("skeleton_keys", 0)
    if n_sk and skeleton_key_template is not None:
        for i, v in enumerate(take_spread(n_sk, 0.0, 1.0)):
            used.add(v.offset)
            repl[v.offset] = _make_skeleton_key_at(
                skeleton_key_template, v, unique_id=v.instance_id,
            )

    return repl


def place_fixed_gold_keys(
    psx: PsxFile,
    fname: str,
    gold_key_template: PsxRecord,
    avoid_offsets: set[int] | None = None,
) -> dict[int, PsxRecord]:
    """Drop a Gold_Key at each FIXED target position listed for `fname` in
    FIXED_GOLD_KEY_TARGETS. Recycles the nearest filler pickup's record slot
    (no file growth) and places the key exactly at the target so it sits at
    a specific gate the player must open (e.g. The Siege's catapult).
    """
    targets = (FIXED_GOLD_KEY_TARGETS.get(fname.upper())
               or FIXED_GOLD_KEY_TARGETS.get(fname))
    if not targets or gold_key_template is None:
        return {}
    avoid = set(avoid_offsets or set())
    prot = get_progression_protected_names(psx)

    PICKUPS = {0x21, 0x46, 0x52, 0x4E, 0x51, 0x0A}
    DECOR = {0x02, 0xB0, 0x30, 0xA0, 0xC9, 0xAE, 0xD3, 0xC8, 0xB5, 0x17,
             0xB1, 0x18, 0xAA, 0xAD, 0x4A, 0xBB, 0x7C, 0x1B, 0x6C}

    repl: dict[int, PsxRecord] = {}
    used: set[int] = set(avoid)

    for (tx, ty, tz) in targets:
        def d2(r):
            return (r.pos_x - tx) ** 2 + (r.pos_y - ty) ** 2 + (r.pos_z - tz) ** 2

        def pick(types):
            c = [r for r in psx.records
                 if r.type_id in types and r.size <= gold_key_template.size
                 and r.instance_name not in prot
                 and r.offset not in used]
            c.sort(key=d2)
            return c[0] if c else None

        victim = pick(PICKUPS) or pick(DECOR)
        if victim is None:
            continue
        used.add(victim.offset)
        # Place the key exactly at the target gate position.
        repl[victim.offset] = _make_gold_key_at(
            gold_key_template, victim, tx, ty, tz,
            unique_id=victim.instance_id,
        )

    return repl


# ---------------------------------------------------------------------------
# EXPERIMENTAL: boss duplication.
#
# Boss arenas are normally skipped entirely. As an opt-in experiment we can
# clone the arena's single boss instance into a redundant slot so the fight
# has TWO bosses. This is byte-budget-safe: we overwrite a non-essential slot
# (e.g. one of the decorative arena lights, which is LARGER than the boss
# record) with the boss clone, so the file never grows.
#
# Only Grave is wired up. Its GraveDigger (type 0x29) sits alone in
# G_BOSS.PSX alongside 8 Entity_Light (0x6D) records — overwriting a side
# light with a second GraveDigger keeps the file size valid. We mirror the
# clone's position across the arena center so the two bosses don't overlap.
#
# NOTE: this is genuinely experimental. The boss death event / Boss_Camera
# are scripted around a single boss, so a second instance may not initialize
# cleanly, may break the victory trigger, or may behave oddly. It will not
# touch the main randomization — it only runs for the listed boss files when
# the caller explicitly enables it.
BOSS_DUPLICATION: dict[str, dict] = {
    "G_BOSS.PSX": {
        "boss_type": 0x29,          # GraveDigger
        # Slots we may recycle for the clone (decorative side lights).
        # These are cosmetic; removing one only dims the arena slightly.
        "victim_names": ["r_moonlight", "l_bottom", "r_mid", "l_mid"],
        # No clone_positions -> mirror across arena center (boss is off-center
        # at (13.2,3,-0.6), so mirroring cleanly separates the two).
    },
    "S_BOSS.PSX": {
        "boss_type": 0x2E,          # BokorLaBas
        # Recycle ambient fireflies (0x348, larger than the 0x228 boss). We
        # avoid the 'food'/'food2'/'food3' fireflies in case they are tied to
        # the fight, using only the plain ambient ones.
        "victim_names": ["Fire_Flies_10947", "Fire_Flies_10948",
                         "Fire_Flies_10949", "Fire_Flies_10950",
                         "Fire_Flies_10951", "Fire_Flies_10952"],
        # BokorLaBas spawns at the arena origin (0,1,0), so mirroring would
        # stack the clone on top of it. Use explicit spread-out positions
        # inside the arena (fireflies range out to ~45 units, so these are
        # safely within bounds).
        "clone_positions": [(22.0, 1.0, 0.0), (-22.0, 1.0, 0.0),
                            (0.0, 1.0, 22.0), (0.0, 1.0, -22.0),
                            (16.0, 1.0, 16.0)],
    },
    "I_BOSS.PSX": {
        "boss_type": 0x82,          # Pirate_Captain_Boss (Captain Cadaver)
        # Recycle the decorative Sea_Weed plants (type 0x12, size 0x228 ==
        # the boss record size, so the overwrite is byte-budget-exact). They
        # are purely cosmetic underwater plants scattered around the arena.
        "victim_names": ["Sea_Weed_12098", "Sea_Weed_12145", "Sea_Weed_12146",
                         "Sea_Weed_12147", "Sea_Weed_12148", "Sea_Weed_12149",
                         "Sea_Weed_12150", "Sea_Weed_12151"],
        # Captain Cadaver spawns at the arena origin (0,0,0), so mirroring
        # would stack the clone on top of it. Use explicit spread-out floor
        # positions, kept inside the fight area (~radius 12-14) and clear of
        # the boss origin and the three Ice_Boss_Grates (±16.9 / 0,19.3).
        "clone_positions": [(12.0, 0.0, 0.0), (-12.0, 0.0, 0.0),
                            (0.0, 0.0, -12.0), (0.0, 0.0, 12.0),
                            (10.0, 0.0, 10.0), (-10.0, 0.0, -10.0),
                            (10.0, 0.0, -10.0), (-10.0, 0.0, 10.0)],
    },
    "U_BOSS.PSX": {
        "boss_type": 0x8E,          # Lord_Of_Darkness (Lord Glutterscum)
        # Recycle decorative Steam_Vents (type 0x2D, size 0x3C8 > the 0x228
        # boss record, so the overwrite is byte-budget-safe). 16 exist; we use
        # a subset so plenty of arena steam remains.
        "victim_names": ["Steam_Vent_11297", "Steam_Vent_11304",
                         "Steam_Vent_11305", "Steam_Vent_11306",
                         "Steam_Vent_11307", "Steam_Vent_11308",
                         "Steam_Vent_11309", "Steam_Vent_11311",
                         "Steam_Vent_11312", "Steam_Vent_11313"],
        # Lord Glutterscum (and its Boss_Camera) sit high at y~210.2, near
        # x=0,z=-2.8. Clone at the same height, spread in x/z around the boss
        # so they share the fight's reference frame and don't stack.
        "clone_positions": [(12.0, 210.2, -2.8), (-12.0, 210.2, -2.8),
                            (0.0, 210.2, 9.2), (0.0, 210.2, -14.8),
                            (10.0, 210.2, 8.0), (-10.0, 210.2, -12.0),
                            (10.0, 210.2, -12.0), (-10.0, 210.2, 8.0),
                            (14.0, 210.2, 4.0), (-14.0, 210.2, 0.0)],
    },
    "C_KING.PSX": {
        "boss_type": 0x9C,          # King (castle boss)
        # Recycle the 8 wall-mounted Fire torches (type 0xE0, size 0x2E8 > the
        # 0x248 boss record). They're cosmetic flames high on the arena walls.
        "victim_names": ["Fire_13039", "Fire_13040", "Fire_13041", "Fire_13042",
                         "Fire_13043", "Fire_13044", "Fire_13045", "Fire_13046"],
        # King sits near the arena center at (0,0,-5). The four King_Electrodes
        # occupy (±15.4, 0, ±15.4); keep clones on the floor (y=0), spread
        # around the boss and clear of the origin and the electrode corners.
        "clone_positions": [(12.0, 0.0, -5.0), (-12.0, 0.0, -5.0),
                            (0.0, 0.0, 8.0), (0.0, 0.0, -20.0),
                            (22.0, 0.0, -5.0), (-22.0, 0.0, -5.0),
                            (0.0, 0.0, 22.0), (0.0, 0.0, -30.0)],
    },
}


# Maximum boss clones the editor/CLI should offer. The true per-boss limit is
# the count of recyclable decorative victim slots big enough to hold the boss
# record (GraveDigger = 4, BokorLaBas = 6). duplicate_boss() clamps the
# requested count to each file's available slots, so requesting the global max
# is always safe — Grave simply caps itself at 4.
MAX_BOSS_CLONES = max(
    (len(cfg.get("victim_names", [])) for cfg in BOSS_DUPLICATION.values()),
    default=1,
)

# Which world each duplicatable boss belongs to, and the max clones each can
# hold (= number of recyclable decorative victim slots). Used to drive the
# separate per-boss clone-count controls (Grave vs Swamp).
BOSS_FILE_WORLD = {"G_BOSS.PSX": "grave", "S_BOSS.PSX": "swamp",
                   "I_BOSS.PSX": "ice", "U_BOSS.PSX": "under",
                   "C_KING.PSX": "castle"}
BOSS_CLONE_MAX = {
    BOSS_FILE_WORLD[f]: len(cfg.get("victim_names", []))
    for f, cfg in BOSS_DUPLICATION.items() if f in BOSS_FILE_WORLD
}  # -> {"grave": 4, "swamp": 6, "ice": 8, "under": 10, "castle": 8}


def _make_boss_clone(boss: PsxRecord, victim: PsxRecord,
                     px: float, py: float, pz: float,
                     unique_id: int) -> PsxRecord:
    """Clone `boss`'s raw record into `victim`'s slot at world position
    (px,py,pz). Keeps the boss's full property/AI payload intact, only
    rewriting position and giving it a unique instance name so it is not
    bound to the original boss's kill-group/event."""
    raw = bytearray(boss.raw)
    # Position floats live at prop region start +0x10.
    struct.pack_into("<3f", raw, RECORD_HEADER_SIZE + 0x10, px, py, pz)
    new_name = f"{boss.class_name}_clone_{unique_id}"
    nb = new_name.encode("ascii") + b"\x00"
    raw[0x100:0x100 + 256] = nb + b"\x00" * (256 - len(nb))
    return PsxRecord(
        offset=victim.offset, class_name=boss.class_name,
        instance_name=new_name, type_id=boss.type_id,
        instance_id=victim.instance_id, prop_count=boss.prop_count,
        pos_x=px, pos_y=py, pos_z=pz,
        raw=bytes(raw),
    )


def duplicate_boss(psx: PsxFile, fname: str,
                   count: int = 1) -> dict[int, PsxRecord]:
    """Duplicate the boss in `fname` `count` times by recycling decorative
    slots (per BOSS_DUPLICATION). Returns a {offset: PsxRecord} replacement
    dict. Clones are placed at the configured explicit positions, or — if
    none are given — mirrored across the arena center so they don't spawn on
    top of the original. Returns {} if the file isn't a configured boss file,
    the boss isn't found, or no recyclable victim slot fits.
    """
    cfg = BOSS_DUPLICATION.get(fname.upper()) or BOSS_DUPLICATION.get(fname)
    if not cfg:
        return {}
    bosses = psx.find_records_by_type(cfg["boss_type"])
    if not bosses:
        return {}
    boss = bosses[0]
    import math

    # Candidate victim slots: named decorative records big enough to hold the
    # boss record. Larger-or-equal size keeps the write byte-budget valid.
    victim_names = [n.lower() for n in cfg.get("victim_names", [])]
    victims = [r for r in psx.records
               if (r.instance_name or "").lower() in victim_names
               and r.size >= boss.size]
    # Sort by the configured name order for deterministic selection.
    order = {n: i for i, n in enumerate(victim_names)}
    victims.sort(key=lambda r: order.get((r.instance_name or "").lower(), 999))

    clone_positions = cfg.get("clone_positions")

    repl: dict[int, PsxRecord] = {}
    for i in range(min(count, len(victims))):
        v = victims[i]
        if clone_positions and i < len(clone_positions):
            cx, cy, cz = clone_positions[i]
        elif clone_positions:
            # Ran out of explicit positions — reuse the last one nudged.
            bx, by, bz = clone_positions[-1]
            cx, cy, cz = bx, by, bz + 6.0 * (i + 1)
        else:
            # No explicit positions: spread clones around the arena center.
            # The boss sits off-center; place clones on a ring at the boss's
            # radius, rotated so none overlap the original or each other.
            r = max(8.0, (boss.pos_x ** 2 + boss.pos_z ** 2) ** 0.5)
            base = math.atan2(boss.pos_z, boss.pos_x)
            # Step around the circle, skipping angle 0 (the original).
            ang = base + math.pi * (i + 1) / (min(count, len(victims)) + 1)
            cx = r * math.cos(ang)
            cz = r * math.sin(ang)
            cy = boss.pos_y
        repl[v.offset] = _make_boss_clone(
            boss, v, cx, cy, cz, unique_id=v.instance_id,
        )
    return repl


def _randomize_all_enemies(
    psx: PsxFile,
    rng: random.Random,
    templates: dict[int, PsxRecord],
    enemy_pool: list[int],
    gold_key_names: set[str] | None = None,
    type_weights: dict[int, float] | None = None,
) -> dict[int, PsxRecord]:
    """Turn EVERY eligible item-class slot into an enemy.

    Used by the "all enemies" mode. Mirrors the source-protection rules of
    the normal universal randomizer (Gold_Key never sourced, hazards left in
    place, HUB phase collectors preserved, kill-group siblings locked) but
    the DESTINATION is always an enemy from `enemy_pool` — except for a small
    Skeleton_Key / Gold_Key carve-out so the world stays beatable.

    Conversion strategy: a single weighted pass. Every eligible slot either
    wins the Skeleton_Key / Gold_Key carve-out lottery or rolls an enemy
    straight out of `enemy_pool` against `type_weights`. Records may grow, so
    no slot is left unconverted and the enemy mix follows the weights
    literally.

    type_weights: per-type weight for enemy selection (default 1.0).
    """
    if gold_key_names is None:
        gold_key_names = get_gold_key_protected_names(psx)
    if type_weights is None:
        type_weights = {}

    # Collect eligible source slots (same protection rules as the normal pool).
    # In all-enemies mode we are MORE aggressive: abilities are also converted
    # (the user wants everything to become an enemy). Only truly progression-
    # critical records stay: Gold_Key, kill-group siblings, protected chests,
    # HUB phase collectors, and authored hazards.
    candidates: list[PsxRecord] = []
    for tid in UNIVERSAL_ITEM_POOL:
        if tid == GOLD_KEY_TYPE:   # Gold_Key — never sourced
            continue
        if tid in HAZARD_PROTECTED_AS_SOURCE:
            continue
        for r in psx.find_records_by_type(tid):
            if tid in (0x25, 0x5B) and chest_has_protected_tag(r):
                continue
            if r.instance_name in gold_key_names:
                continue
            if tid == 0x24 and r.prop_count >= 2:
                p1 = struct.unpack_from('<I', r.raw, 0x228 + 1*0x20 + 8)[0]
                if p1 != 0:        # HUB phase-marker collector — preserve
                    continue
            candidates.append(r)

    sk_tmpl = templates.get(0x65)
    gk_tmpl = templates.get(GOLD_KEY_TYPE)

    # ----- Single weighted pass -------------------------------------------
    # Each eligible slot: carve-out lottery first (Skeleton_Key, then
    # Gold_Key), otherwise roll the enemy pool against type_weights. Records
    # are free to grow, so every eligible slot converts.
    by_offset = {r.offset: r for r in candidates}
    weighted_pool = [t for t in enemy_pool]
    weighted_w = [type_weights.get(t, 1.0) for t in weighted_pool]

    assigned: dict[int, int] = {}      # offset -> enemy type id
    carve_keys: dict[int, int] = {}    # offset -> 0x65 / Gold_Key

    for r in candidates:
        roll = rng.random()
        if sk_tmpl is not None and roll < SKELETON_KEY_SPAWN_PROBABILITY:
            carve_keys[r.offset] = 0x65
            continue
        if (gk_tmpl is not None
                and roll < SKELETON_KEY_SPAWN_PROBABILITY + GOLD_KEY_SPAWN_PROBABILITY):
            carve_keys[r.offset] = GOLD_KEY_TYPE
            continue
        assigned[r.offset] = rng.choices(weighted_pool, weights=weighted_w, k=1)[0]

    # ----- Build replacement records --------------------------------------
    replacements: dict[int, PsxRecord] = {}
    for off, tid in assigned.items():
        r = by_offset[off]
        replacements[off] = make_record_with_template(templates[tid], r, off)
    for off, tid in carve_keys.items():
        r = by_offset[off]
        replacements[off] = make_record_with_template(templates[tid], r, off)

    # Post-process: activate any carve-out keys so they're collectable and
    # don't bind to existing kill-groups; boost Raven aggro.
    for off, rec in list(replacements.items()):
        if rec.type_id == 0x65:
            replacements[off] = _activate_skeleton_key(rec)
        elif rec.type_id == GOLD_KEY_TYPE:
            replacements[off] = _activate_gold_key(rec)
        elif rec.type_id == 0x37:
            replacements[off] = _boost_raven_aggro(rec)

    return replacements


def randomize_universal(
    psx: PsxFile,
    seed: int,
    weights: dict[int, int] | None = None,
    stay_rate: float | None = None,
    extra_templates: dict[int, PsxRecord] | None = None,
    all_enemies: bool = False,
    enemy_types: tuple[int, ...] | None = None,
    all_enemies_weights: dict[int, float] | None = None,
    excluded_types: set[int] | None = None,
    lift_protection: bool = False,
    gate_types: tuple[int, ...] = (),
    gate_weights: dict[int, int] | None = None,
    gate_source_types: tuple[int, ...] = (),
    gate_stay_rate: float = 0.0,
    preserve_chests: bool = False,
    preserve_iron_keys: bool = False,
) -> dict[int, PsxRecord]:
    """Universal item randomizer.

    Every item-class entity rolls independently:
      - With probability `stay_rate`, the item KEEPS its current type (no change).
      - Otherwise, it rolls a new type from the weighted pool. If the rolled type
        equals the current type (rare with low own-weights), the item also stays.

    The file is allowed to grow, so the configured weights are literal: every
    slot takes the type it rolled, whatever its size.

    all_enemies: when True, EVERY eligible item slot becomes an enemy (from
    `enemy_types`, falling back to ENEMY_DESTINATIONS) instead of rolling the
    normal weighted item pool. Gold_Keys stay protected (never sourced).
    A small SKELETON_KEY_SPAWN_PROBABILITY carve-out turns ~1% of slots into
    a collectable Skeleton_Key so locked content stays reachable.
    enemy_types: the enemy type pool to use as destinations in all_enemies
    mode. Should be the world's walking-melee pool so spawns are valid.

    extra_templates: per-type fallback templates harvested from sibling maps
    in the same world. Used for types (e.g., Ability) that may not appear in
    every map but should still be reachable as roll destinations.

    excluded_types: optional set of type-IDs that should NEVER appear as a
    randomization destination (item-to-enemy or item-to-item). Used to opt
    out of types known to soft-lock (e.g., Dark_Knight 0x53).
    """
    rng = random.Random(seed)
    # Drop the previous map's telemetry immediately: the early returns below
    # (no templates / all-enemies mode) never publish, and a caller reading
    # LAST_UNIVERSAL_STATS after such a call would otherwise report the
    # PREVIOUS map's numbers under this map's name.
    LAST_UNIVERSAL_STATS.clear()
    weights = weights or DEFAULT_ITEM_WEIGHTS
    if stay_rate is None:
        stay_rate = ITEM_STAY_RATE
    excluded_types = excluded_types or set()
    if preserve_chests:
        # Keep every vanilla chest exactly where/what it is: chests are not
        # sources (handled in the candidate loop below) AND nothing rolls INTO
        # a chest (exclude them as destinations). Their CONTENTS are still
        # randomized by the chest-content pass in aggressive_item_randomize.
        excluded_types = set(excluded_types) | {0x25, 0x5B}
    if preserve_iron_keys:
        # Keep every vanilla Skeleton_Key (Iron Key, 0x65) exactly where it
        # is: keys are not sources (kept in their original slots) AND nothing
        # rolls INTO a Skeleton_Key (excluded as a destination). This prevents
        # Iron Keys from being moved, removed, or added at random positions,
        # keeping key count / placement fully vanilla.
        excluded_types = set(excluded_types) | {0x65}

    # Effective pool / weights. In gate "pool" mode the gate types are appended
    # so gates are both a SOURCE (a gate can roll into an item) and a
    # DESTINATION (an item can roll into a gate).
    pool = UNIVERSAL_ITEM_POOL
    if gate_types:
        pool = tuple(UNIVERSAL_ITEM_POOL) + tuple(
            t for t in gate_types if t not in UNIVERSAL_ITEM_POOL)
        weights = dict(weights)
        for t, w in (gate_weights or GATE_WEIGHTS).items():
            weights.setdefault(t, w)

    # SOURCE pool. Gates are added as SOURCES in BOTH gate modes — a gate can
    # roll into any item / structure / enemy from the normal pool. In isolated
    # mode they're sources only (gate_types empty -> not destinations); in pool
    # mode they're both sources and destinations.
    source_pool = UNIVERSAL_ITEM_POOL
    if gate_source_types:
        source_pool = tuple(UNIVERSAL_ITEM_POOL) + tuple(
            t for t in gate_source_types if t not in UNIVERSAL_ITEM_POOL)

    # Find one representative template per type. For chests, pick a "simple" one
    # (no protected tags, single content tag) so transforms don't carry weird payloads.
    templates: dict[int, PsxRecord] = {}
    for tid in pool:
        recs = psx.find_records_by_type(tid)
        if not recs:
            continue
        if tid in (0x25, 0x5B):
            simple = [c for c in recs
                      if not chest_has_protected_tag(c)
                      and c.instance_name not in PROTECTED_INSTANCE_NAMES
                      and len(get_chest_content_tags(c)) <= 1]
            templates[tid] = simple[0] if simple else recs[0]
        else:
            templates[tid] = recs[0]

    # Fill in any missing types from cross-file templates so that maps which
    # don't natively have a given type (e.g., Ability in HUB files) can still
    # roll INTO that type.
    if extra_templates:
        for tid, tpl in extra_templates.items():
            if tid not in templates and tid in pool:
                templates[tid] = tpl

    # In "all enemies" mode, also pull in enemy templates supplied via
    # extra_templates even if they're not in UNIVERSAL_ITEM_POOL. The enemy
    # pool we roll into is `enemy_types` (the world's walking-melee set);
    # every one of those needs a usable template.
    if all_enemies and extra_templates:
        for tid, tpl in extra_templates.items():
            if tid not in templates:
                templates[tid] = tpl

    if not templates:
        return {}

    # ----- ALL-ENEMIES MODE ------------------------------------------------
    # Every eligible item slot becomes an enemy. We build the destination
    # pool from `enemy_types` (the world's walking-melee roster) restricted
    # to types that actually have a template available. A small carve-out
    # turns ~SKELETON_KEY_SPAWN_PROBABILITY of slots into a Skeleton_Key so
    # locked content remains reachable, and the existing Gold_Key lottery
    # still fires. Enemy selection is a single weighted pass — records may
    # grow, so every eligible slot converts to the enemy it rolled.
    if all_enemies:
        pool = [t for t in (enemy_types or tuple(ENEMY_DESTINATIONS))
                if t in templates and t not in excluded_types]
        if not pool:
            return {}
        return _randomize_all_enemies(
            psx, rng, templates, pool, gold_key_names=None,
            type_weights=all_enemies_weights,
        )

    # Build the weighted choice list once
    choice_types = [t for t in pool
                    if t in templates and weights.get(t, 0) > 0
                    and t not in excluded_types]
    choice_weights = [weights[t] for t in choice_types]

    # ---- Normalize ENEMY share to a fixed target percentage ---------------
    # The user wants the chance of an item rolling into an enemy to be the
    # same in every world. Each world has a different number of available
    # enemy types and item types, so a single fixed weight can't do this.
    # Instead, we scale all enemy weights at runtime so their combined share
    # of the eligible pool equals ENEMY_SHARE_TARGET. Item weights stay as-is.
    if ENEMY_SHARE_TARGET is not None and 0 < ENEMY_SHARE_TARGET < 1:
        enemy_idx = [i for i, t in enumerate(choice_types) if t in ENEMY_DESTINATIONS]
        item_idx = [i for i, t in enumerate(choice_types) if t not in ENEMY_DESTINATIONS]
        item_total = sum(choice_weights[i] for i in item_idx)
        enemy_total_now = sum(choice_weights[i] for i in enemy_idx)
        if enemy_idx and item_total > 0:
            # Solve for scale s such that:
            #   (s * enemy_total_now) / (s * enemy_total_now + item_total) = target
            # => s * enemy_total_now * (1 - target) = item_total * target
            # => s = item_total * target / (enemy_total_now * (1 - target))
            target = ENEMY_SHARE_TARGET
            scale = (item_total * target) / (enemy_total_now * (1 - target))
            for i in enemy_idx:
                choice_weights[i] = max(1, int(round(choice_weights[i] * scale)))

    # Collect all candidate records (every item-class entity in the file).
    # Enemy records are NOT candidates — only items can roll into enemies,
    # not the other way around. The dedicated enemy randomizer (cli.py)
    # handles enemy-to-enemy swaps.
    #
    # Ability entities (0x20) are also NOT candidates — they're protected as
    # SOURCES so every vanilla ability spawn survives in place (and gets a
    # new ability ID rolled in step 4). Without this protection ~70% of the
    # 48 vanilla abilities get swapped into coins/chests/enemies and the
    # player ends up with very few visible skill pickups in the maps.
    # Ability is still a valid DESTINATION (kept in DEFAULT_ITEM_WEIGHTS) so
    # other items can occasionally roll INTO an ability, adding extra spawns.
    candidates: list[PsxRecord] = []
    # Per-file Gold_Key kill-group protection: any record sharing an
    # instance_name with a Gold_Key entity in this file is locked. When
    # `lift_protection` is set (e.g. the first Grave level per user request),
    # all chest/ability/kill-group protections are dropped so EVERYTHING on
    # that map is randomizable.
    gold_key_names = set() if lift_protection else get_gold_key_protected_names(psx)
    for tid in source_pool:
        if tid in ENEMY_DESTINATIONS:
            continue
        if preserve_chests and tid in (0x25, 0x5B):
            continue  # chests keep their vanilla type/position
        if preserve_iron_keys and tid == 0x65:
            continue  # Iron Keys keep their vanilla type/position
        if tid == 0x20 and not lift_protection:  # Ability — protected as a source
            continue
        if tid == GOLD_KEY_TYPE:
            # Gold_Key (0x2B) is now in the universal pool as a DESTINATION
            # so the 0.01% lottery can place one in any item slot. It must
            # NOT be a source — vanilla Gold_Key positions are progression
            # locks (boss-key gates) and randomizing them away breaks the
            # level. Also skipped by the per-file kill-group check below
            # for redundancy.
            continue
        if tid == 0x65 and not lift_protection:  # Skeleton_Key — never a source
            # BUG FIX (investigated after reports of infinite loading on Dead
            # Heat / Quick and the Dead / Crushed Spirits with plain default
            # randomization): unlike Gold_Key, Skeleton_Key previously had NO
            # type-level source protection here -- it relied entirely on the
            # name-based `gold_key_names` check below. That check only
            # protects a Skeleton_Key whose instance_name is shared with
            # another progression record (a kill-group). get_progression_
            # protected_names() deliberately EXCLUDES auto-generated names
            # like "Skeleton_Key_11004" (assuming a unique name means no
            # kill-group to protect) -- but a Skeleton_Key is progression-
            # critical by TYPE alone, regardless of whether it shares a name
            # with anything: it's the literal key drop some kill-group
            # elsewhere spawns in. Confirmed case: G_SUB1.PSX (Dead Heat)
            # contains exactly this record ('Skeleton_Key_11004', vanilla
            # type 0x65) and it was being retyped away (e.g. into a
            # Wooden_Chest) by this loop, removing the key the level needs to
            # progress and causing the level to hang on load. Skeleton_Key
            # is still a valid DESTINATION (other items can roll into one),
            # matching how Gold_Key is handled.
            continue
        if tid in HAZARD_PROTECTED_AS_SOURCE:
            # Cannons / spike traps / flame jets stay where the level
            # designer placed them — see HAZARD_PROTECTED_AS_SOURCE for
            # rationale. They remain valid DESTINATIONS so other items
            # can still roll INTO a hazard.
            continue
        for r in psx.find_records_by_type(tid):
            # Skip protected chests (unless protection is lifted for this file)
            if (tid in (0x25, 0x5B) and not lift_protection
                    and chest_has_protected_tag(r)):
                continue
            # Skip records whose instance_name is shared with a Gold_Key.
            # The engine treats them as a kill-group; swapping any member
            # breaks the gold-key activation trigger.
            if r.instance_name in gold_key_names:
                continue
            # Skip HUB phase-marker collectors (Collector with prop[1] != 0).
            # These (P1_Collector / P2_Collector / etc) are HUB level-select
            # phase trackers, not in-level shops. Randomizing them away
            # breaks HUB level-select progression. They keep their vanilla
            # type AND vanilla prop values throughout the pipeline.
            if tid == 0x24 and r.prop_count >= 2:
                p1 = struct.unpack_from(
                    '<I', r.raw, 0x228 + 1*0x20 + 8
                )[0]
                if p1 != 0:
                    continue
            candidates.append(r)

    # Also skip Gold_Key entities entirely — they're progression-critical and
    # we don't randomize them away or to random positions. (Gold_Key is in
    # UNIVERSAL_ITEM_POOL as a destination only, never a source — see the
    # explicit GOLD_KEY_TYPE filter above.)

    # ----- Roll -> accept --------------------------------------------------
    # Records may grow, so there is no byte budget and no rationing: each slot
    # rolls the weighted pool once and takes what it rolled. Processing order
    # is therefore irrelevant to the outcome.
    replacements: dict[int, PsxRecord] = {}
    rolled_slots = 0          # candidates that actually rolled a new type

    for r in candidates:
        # Stay-rate roll: item keeps its current type with probability stay_rate.
        # With stay_rate=0, this never triggers and every item rolls a new type.
        # Gates use their own stay rate (gate_stay_rate) so the gate randomizer
        # can let a gate keep its identity some of the time.
        eff_stay = stay_rate
        if gate_source_types and r.type_id in gate_source_types:
            eff_stay = gate_stay_rate
        if eff_stay > 0 and rng.random() < eff_stay:
            continue

        # Build a per-record pool that EXCLUDES the item's own type, so the roll
        # always produces a real change (no "rolled itself, skip" outcomes).
        own_excluded = [(t, w) for t, w in zip(choice_types, choice_weights) if t != r.type_id]
        if not own_excluded:
            continue
        per_record_types = [t for t, _ in own_excluded]
        per_record_weights = [w for _, w in own_excluded]
        new_type = rng.choices(per_record_types, weights=per_record_weights, k=1)[0]

        # Gold_Key lottery — overrides the rolled type with probability
        # GOLD_KEY_SPAWN_PROBABILITY (1%). Done as a post-roll lottery
        # rather than a weight in the main pool so we can hit fractional
        # probabilities below what integer weights can express. Skipped
        # silently if no Gold_Key template is available in this file/world.
        if (0x2B in templates
                and r.type_id != 0x2B
                and new_type != 0x2B
                and rng.random() < GOLD_KEY_SPAWN_PROBABILITY):
            new_type = 0x2B

        rolled_slots += 1
        replacements[r.offset] = make_record_with_template(templates[new_type], r, r.offset)

    # Post-process: any Skeleton_Key the randomizer created needs its
    # collectable flags forced on (idx=4=1, idx=5=1). Templates copied from
    # G_SUB1/U_INTRO have those flags at 0 (inactive anchor records spawned
    # by chests at runtime), so the new key would be invisible/non-collectable
    # without this fix.
    for off, rec in list(replacements.items()):
        if rec.type_id == 0x65:
            replacements[off] = _activate_skeleton_key(rec)
        elif rec.type_id == 0x2B:
            # Gold_Key dropped via the 0.01% lottery — vanilla anchor
            # records have prop[0]=0 and rely on engine kill-group logic
            # to flip them on. Random placement has no kill-group, so we
            # force the flag so the key spawns immediately collectable.
            replacements[off] = _activate_gold_key(rec)
        elif rec.type_id == 0x37:
            # Boost Raven sight/aggro radius — vanilla ships small zones
            # (15–38 units) which makes Ravens feel passive after they get
            # placed at random new positions.
            replacements[off] = _boost_raven_aggro(rec)

    # Post-process: any Monster_Generator the randomizer created needs to
    # keep its TEMPLATE instance_name (e.g. "courtyard1") so the BEF script
    # still recognizes it and spawns enemies. The default copy logic in
    # make_record_with_template overwrites the name with the source item's
    # (e.g. "GoldCoin_1234"), which has no script binding and produces a
    # silent non-spawning generator.
    if 0x5F in templates:
        gen_template = templates[0x5F]
        # Copy class+instance name buffers from the template back into the
        # new record. NAME_BUFFER_SIZE=0x100 (class), then 0x100 (instance).
        for off, rec in list(replacements.items()):
            if rec.type_id == 0x5F:
                new_raw = bytearray(rec.raw)
                new_raw[0:0x200] = gen_template.raw[0:0x200]
                replacements[off] = PsxRecord(
                    offset=rec.offset,
                    class_name=gen_template.class_name,
                    instance_name=gen_template.instance_name,
                    type_id=rec.type_id,
                    instance_id=rec.instance_id,
                    prop_count=rec.prop_count,
                    pos_x=rec.pos_x, pos_y=rec.pos_y, pos_z=rec.pos_z,
                    raw=bytes(new_raw),
                )

    # ----- Publish telemetry ----------------------------------------------
    # `achieved` is a plain histogram of what was actually placed — with
    # literal weights it should track the configured shares closely. Reported
    # per call, so the caller must read it right after randomize_universal
    # returns.
    achieved: dict[int, int] = {}
    for rec in replacements.values():
        achieved[rec.type_id] = achieved.get(rec.type_id, 0) + 1
    LAST_UNIVERSAL_STATS.clear()
    LAST_UNIVERSAL_STATS.update({
        'rolled_slots': rolled_slots,
        'achieved': achieved,
    })

    return replacements


def aggressive_item_randomize(
    psx: PsxFile,
    seed: int,
    existing_repl: dict[int, PsxRecord] | None = None,
    extra_templates: dict[int, PsxRecord] | None = None,
    world: str | None = None,
    all_enemies: bool = False,
    enemy_types: tuple[int, ...] | None = None,
    all_enemies_weights: dict[int, float] | None = None,
    world_gen_templates: list | None = None,
    excluded_types: set[int] | None = None,
    do_items: bool = True,
    do_chests: bool = True,
    do_skills: bool = True,
    do_columns: bool = True,
    universal_weights: dict[int, float] | None = None,
    tier_weights: dict | None = None,
    weather_template: PsxRecord | None = None,
    chest_special_chances: dict | None = None,
    lift_protection: bool = False,
    randomize_gen_tier: bool = False,
    gate_mode: str | None = None,
    gate_templates: dict[int, PsxRecord] | None = None,
    preserve_chests: bool = False,
    preserve_iron_keys: bool = False,
    ability_weights: dict[int, int] | None = None,
) -> dict[int, PsxRecord]:
    """All-in-one: every item-class entity rolls into another type from the
    UNIVERSAL pool, then surviving chests/abilities get their content
    further randomized.

    existing_repl: replacements already produced upstream (e.g., enemy type
    swaps from cli.py). Step 6 (tier randomization) reads from this so it
    can re-tier the POST-swap type, not the vanilla original.
    extra_templates: cross-file templates per type, used so a map can roll
    INTO a type (e.g. Ability) it doesn't natively contain.
    world: which world this PSX belongs to. Used for per-world enemy tier
    ranges so we don't roll tiers the BEF didn't load.
    all_enemies: when True, the universal pass turns EVERY eligible item slot
    into an enemy (from enemy_types). Chest-content / ability / collector
    sub-passes are skipped because there are no surviving items to re-roll.
    enemy_types: the world's walking-melee pool used as enemy destinations
    in all_enemies mode.
    excluded_types: optional set of type-IDs to filter out of every roll
    destination. Used by the "no Dark_Knight" GUI option to drop
    progression-blocking enemy types from item-to-enemy rolls.

    Category toggles (each enables an independent sub-pass):
      do_items:   universal type swap of every item/structure entity, plus
                  the incidental world passes — collector drops, enemy tiers,
                  monster-generator params, and weather/ambience.
      do_chests:  chest content tags + gold amounts.
      do_skills:  ability/skill pickup IDs.
      do_columns: HUB blind level-select column shuffle.
    When a category is False, that part of the game is left at its vanilla
    values.

    ability_weights: optional {ability_id: weight} from the GUI's
    "Abilities & Skills" editor. When given, the baked Ability pickup ids are
    rolled ONLY from the ids with weight > 0 (so an unchecked ability never
    spawns), using those weights. Ids outside PLACEABLE_ABILITY_IDS are
    reported and ignored.
    If the resulting pool is empty the per-world pool is used instead so we
    never write an invalid id. Providing this dict also enables the ability
    pass even when do_skills is False.
    """
    rng_master = random.Random(seed)
    all_repl: dict[int, PsxRecord] = dict(existing_repl) if existing_repl else {}
    if preserve_chests:
        # Vanilla chests are kept in place; make sure their CONTENTS still get
        # randomized even if the chest category wasn't otherwise requested.
        do_chests = True
    # Per-file Gold_Key protection: every record sharing an instance_name
    # with a Gold_Key entity is locked from any modification (the engine
    # uses kill-group bookkeeping based on shared names). When
    # `lift_protection` is set, all chest/ability/kill-group protections are
    # dropped for this file so everything on it is randomizable.
    gold_key_names = set() if lift_protection else get_gold_key_protected_names(psx)

    # 1. Universal type swap: every item-class entity rolls into a new type.
    # Use the per-world weight table so e.g. Castle gets a boosted
    # Skeleton_Key supply (gating-heavy world). Gated by `do_items` — when
    # off, every item/structure keeps its vanilla type (chests/skills/columns
    # can still be randomized independently below).
    if do_items or all_enemies:
        s = rng_master.randrange(1 << 30)
        # Gate randomizer. In BOTH modes gates are SOURCES (a gate can roll into
        # any item / structure / enemy). In "pool" mode gates are ALSO
        # destinations (items can roll into gates, so gates appear anywhere);
        # we then make gate templates reachable via extra_templates.
        gp_src_types: tuple[int, ...] = ()
        gp_dest_types: tuple[int, ...] = ()
        gp_stay = 0.0
        gp_extra = extra_templates
        if gate_mode in ("isolated", "pool") and not all_enemies:
            gp_src_types = GATE_TYPES
        if gate_mode == "isolated" and not all_enemies:
            gp_stay = GATE_ISOLATED_STAY_RATE  # gates have a chance to stay put
        if gate_mode == "pool" and not all_enemies:
            gp_dest_types = GATE_TYPES
            if gate_templates:
                gp_extra = dict(extra_templates or {})
                for tid, tpl in gate_templates.items():
                    gp_extra.setdefault(tid, tpl)
        repl = randomize_universal(
            psx, seed=s,
            weights=universal_weights or get_item_weights_for_world(world),
            extra_templates=gp_extra,
            all_enemies=all_enemies,
            enemy_types=enemy_types,
            all_enemies_weights=all_enemies_weights,
            excluded_types=excluded_types,
            lift_protection=lift_protection,
            gate_types=gp_dest_types,
            gate_source_types=gp_src_types,
            gate_stay_rate=gp_stay,
            preserve_chests=preserve_chests,
            preserve_iron_keys=preserve_iron_keys,
        )
        for off, r in repl.items():
            all_repl[off] = r

    # In all_enemies mode there are no surviving items to re-roll content
    # for (chests, abilities, collectors all became enemies). Skip straight
    # to the enemy-tier pass so the new enemies still get varied tiers.
    if all_enemies:
        s = rng_master.randrange(1 << 30)
        trng = random.Random(s)
        for orig in psx.records:
            cur = all_repl.get(orig.offset, orig)
            new_rec = reroll_enemy_tier(cur, trng, world=world,
                                        tier_weights_per_type=tier_weights)
            if new_rec is not cur:
                all_repl[cur.offset] = new_rec
        return all_repl

    # 2. Chest contents: each chest in the final state rolls a content tag.
    # Wooden and Locked chests use DIFFERENT property index sets, so we
    # randomize each with its own pool.
    s = rng_master.randrange(1 << 30)
    crng = random.Random(s)
    # Mimic/Wizard as explicit per-chest CHANCES. When configured, each wooden
    # chest independently rolls: mimic with m_chance, else wizard with w_chance,
    # else a normal content tag (drawn from a pool with mimic/wizard removed so
    # they only come from these chances). Clamped so m+w <= 1.0.
    m_chance = w_chance = 0.0
    base_opts, base_weights = CONTENT_TAG_OPTIONS, CONTENT_TAG_WEIGHTS
    if chest_special_chances:
        m_chance = max(0.0, min(1.0, chest_special_chances.get("mimic", 0) / 100.0))
        w_chance = max(0.0, min(1.0 - m_chance,
                                chest_special_chances.get("wizard", 0) / 100.0))
        base_opts, base_weights = content_options_without_specials()
    use_chances = chest_special_chances is not None
    final_chests_pass2 = []
    if do_chests:
        for orig in psx.records:
            cur = all_repl.get(orig.offset, orig)
            if cur.type_id in (0x25, 0x5B) and (lift_protection or not chest_has_protected_tag(orig)):
                # Skip chests in a Gold_Key kill-group (sharing instance_name).
                if orig.instance_name in gold_key_names:
                    continue
                final_chests_pass2.append((orig.offset, cur))
    for off, cur in final_chests_pass2:
        if cur.type_id == 0x25:
            if use_chances:
                roll = crng.random()
                if roll < m_chance:
                    new_tags = crng.choice(MIMIC_TAG_VERSIONS)
                elif roll < m_chance + w_chance:
                    new_tags = crng.choice(WIZARD_TAG_VERSIONS)
                else:
                    new_tags = crng.choices(base_opts, weights=base_weights, k=1)[0]
            else:
                new_tags = crng.choices(CONTENT_TAG_OPTIONS, weights=CONTENT_TAG_WEIGHTS, k=1)[0]
            clear = CHEST_CONTENT_INDICES
        else:  # 0x5B locked chest
            new_tags = crng.choices(LOCKED_CONTENT_TAG_OPTIONS,
                                    weights=LOCKED_CONTENT_TAG_WEIGHTS, k=1)[0]
            clear = LOCKED_CHEST_CONTENT_INDICES
        if get_chest_content_tags(cur) == new_tags:
            continue
        all_repl[off] = write_chest_with_tags(cur, new_tags, clear_indices=clear)

    # 3. Chest gold amounts.
    # Re-rolls gold for ALL chests in the final state — including chests that
    # didn't exist in the source but were created by coin→chest transforms.
    # Weighted distribution: probability decreases as count grows so chests
    # rarely spawn many coins (1 coin > 2 coins > 3 coins > ...).
    s = rng_master.randrange(1 << 30)
    grng = random.Random(s)
    # Hand-tuned weights: peak at 2-4 coins, smooth decay, small chance of empty
    # or high-roll. Designed so chest contents feel rewarding without flooding
    # the economy. Earlier curves peaked at 1 coin which made ~60% of chests
    # feel near-empty; this curve shifts mass toward 2-5 coins. Lives at module
    # scope (CHEST_GOLD_AMOUNTS / CHEST_GOLD_WEIGHTS) so harder mode can bias
    # it toward "just 1 koin".
    GOLD_AMOUNTS = CHEST_GOLD_AMOUNTS
    GOLD_WEIGHTS = CHEST_GOLD_WEIGHTS
    # Same curve with the 0-coin entry dropped — used for chests that DID roll
    # a gold reward, where writing 0 would cancel that reward.
    GOLD_AMOUNTS_NONZERO = [a for a in GOLD_AMOUNTS if a != 0]
    GOLD_WEIGHTS_NONZERO = [w for a, w in zip(GOLD_AMOUNTS, GOLD_WEIGHTS) if a != 0]

    # Gather final-state chests: source chests that stayed chests, plus new
    # chests created by other items being transformed into chests.
    final_chest_offsets = set()
    if do_chests:
        for orig in psx.records:
            cur = all_repl.get(orig.offset, orig)
            if cur.type_id in (0x25, 0x5B) and (lift_protection or not chest_has_protected_tag(orig)):
                if orig.instance_name in gold_key_names:
                    continue
                final_chest_offsets.add(orig.offset)

    for off in final_chest_offsets:
        # base record: prefer all_repl entry if present, else original
        base = all_repl.get(off)
        if base is None:
            base = next(o for o in psx.records if o.offset == off)
        # Wooden chests use idx=16 for gold; locked chests use idx=7
        #
        # BUG FIX (chests "almost always give koins/health"): this pass used
        # to stamp a fresh gold count into EVERY final-state chest. With
        # CHEST_GOLD_WEIGHTS the 0-coin outcome is only 15/253 (~6%), so ~94%
        # of chests ended up carrying a gold tag on top of whatever step 2
        # rolled — an ability/armor/iron-key/prize chest still paid out koins,
        # and the gold drop visually dominates the other reward. Measured on
        # G_INTRO+G_SUB1 (seed 1234): 55/55 wooden chests carried (16,N).
        # So the content pool was already varied; the gold pass was flattening
        # it back to gold. Gold is now written ONLY when the chest's rolled
        # content actually asked for gold (wooden: the (16,N) tag is part of
        # the content roll), and the slot is explicitly zeroed otherwise so a
        # retyped chest can't inherit its template's gold either.
        # Locked chests keep gold as a separate roll (idx=7 is not part of
        # their content pool) but only at LOCKED_CHEST_GOLD_CHANCE, so they
        # aren't universally gold either.
        final_tags = get_chest_content_tags(base)
        if base.type_id == 0x5B:
            gold_idx = LOCKED_CHEST_GOLD_INDEX
            wants_gold = grng.random() < LOCKED_CHEST_GOLD_CHANCE
        else:
            gold_idx = 16
            wants_gold = any(i == 16 for i, _ in final_tags)
        for k in range(base.prop_count):
            eo = 0x228 + k * 0x20
            if eo + 0x20 > base.size:
                break
            idx = struct.unpack_from("<I", base.raw, eo + 4)[0]
            if idx == gold_idx:
                if wants_gold:
                    # Non-zero amounts only: a 0 here would silently erase the
                    # gold reward the content roll just chose.
                    new_amount = grng.choices(GOLD_AMOUNTS_NONZERO,
                                              weights=GOLD_WEIGHTS_NONZERO, k=1)[0]
                else:
                    new_amount = 0
                new_raw = bytearray(base.raw)
                struct.pack_into("<I", new_raw, eo + 8, new_amount)
                all_repl[base.offset] = PsxRecord(
                    offset=base.offset, class_name=base.class_name,
                    instance_name=base.instance_name, type_id=base.type_id,
                    instance_id=base.instance_id, prop_count=base.prop_count,
                    pos_x=base.pos_x, pos_y=base.pos_y, pos_z=base.pos_z,
                    raw=bytes(new_raw),
                )
                break

    # 4. Ability ID randomization on EVERY ability in the final state.
    #
    # Walks the post-step-1 final state (vanilla Ability records that
    # survived as abilities + new ability records created by the universal
    # pool when a coin/chest rolls INTO an Ability). The pool is global —
    # GAME_ABILITY_POOL contains every ability ID found anywhere in vanilla,
    # so any world can spawn any ability.
    #
    # Why this walks the final state instead of psx.find_records_by_type:
    # cli.py's world_item_templates supplies ONE Ability template per world
    # (whichever was harvested first from that world's PSX files), so every
    # coin-→-Ability in a given world inherits the same ID — Grave gets all
    # ID 20 (Magic Shield), Castle gets all ID 12/15/18, etc. Walking the
    # final state lets us roll each one independently from the global pool
    # so every world ends up with a mix of every ability.
    s = rng_master.randrange(1 << 30)
    arng = random.Random(s)
    # Use the per-world pool when available so we don't roll abilities the
    # current BEF doesn't load (which would spawn invisible/non-collectable
    # pickups). Fall back to the global pool only if world is None.
    if world and world in ABILITY_POOL_PER_WORLD:
        pool = ABILITY_POOL_PER_WORLD[world]
        pool_weights = ABILITY_POOL_PER_WORLD_WEIGHTS[world]
    else:
        pool = GAME_ABILITY_POOL
        pool_weights = [1] * len(pool)

    # Editor selection wins: only ids the user left enabled (weight > 0) can
    # be baked into a pickup. Empty selection -> keep the per-world pool.
    if ability_weights:
        _sel = [i for i in sorted(ability_weights)
                if ability_weights[i] > 0 and i in PLACEABLE_ABILITY_IDS]
        for i in sorted(ability_weights):
            if ability_weights[i] > 0 and i not in PLACEABLE_ABILITY_IDS:
                print(f"ELF patch: ability id {i} is not placeable "
                      f"(no-op / kiss id) - skipped")
        if _sel:
            pool = _sel
            pool_weights = [ability_weights[i] for i in _sel]

    # Collect every offset whose final type is 0x20 (Ability).
    final_ability_offsets: set[int] = set()
    if do_skills or ability_weights:
        for orig in psx.records:
            cur = all_repl.get(orig.offset, orig)
            if cur.type_id == 0x20:
                final_ability_offsets.add(orig.offset)
        # Also pick up offsets created entirely by step 1 (no vanilla record at
        # that offset — coin/chest slot the universal pool transformed into an
        # Ability).
        for off, rec in all_repl.items():
            if rec.type_id == 0x20:
                final_ability_offsets.add(off)

    for off in final_ability_offsets:
        cur = all_repl.get(off)
        if cur is None:
            cur = next(o for o in psx.records if o.offset == off)
        if cur.prop_count < 1:
            continue
        new_id = arng.choices(pool, weights=pool_weights, k=1)[0]
        current = struct.unpack_from("<I", cur.raw, 0x228 + 8)[0]
        if current == new_id:
            continue
        new_raw = bytearray(cur.raw)
        struct.pack_into("<I", new_raw, 0x228 + 8, new_id)
        all_repl[cur.offset] = PsxRecord(
            offset=cur.offset, class_name=cur.class_name,
            instance_name=cur.instance_name, type_id=cur.type_id,
            instance_id=cur.instance_id, prop_count=cur.prop_count,
            pos_x=cur.pos_x, pos_y=cur.pos_y, pos_z=cur.pos_z,
            raw=bytes(new_raw),
        )

    # 5. Collector drop randomization. Each in-level collector rolls a
    #    random drop code from the vanilla pool. HUB phase-marker
    #    collectors (vanilla prop[1] != 0) are kept untouched — they're
    #    level-select trackers, not in-level shops, and randomizing them
    #    breaks HUB progression. The universal pool already source-
    #    protects HUB phase markers, so they should never reach this step
    #    via item-roll either.
    s = rng_master.randrange(1 << 30)
    drng = random.Random(s)

    # Track HUB phase-marker offsets so we can skip them.
    hub_collector_offsets: set[int] = set()
    for orig in psx.records:
        if orig.type_id == 0x24 and orig.prop_count >= 2:
            p1 = struct.unpack_from("<I", orig.raw, 0x228 + 1*0x20 + 8)[0]
            if p1 != 0:
                hub_collector_offsets.add(orig.offset)

    final_collector_offsets: set[int] = set()
    for orig in psx.records:
        cur = all_repl.get(orig.offset, orig)
        if cur.type_id == 0x24 and cur.prop_count >= 1:
            final_collector_offsets.add(orig.offset)
    for off, rec in all_repl.items():
        if rec.type_id == 0x24 and rec.prop_count >= 1:
            final_collector_offsets.add(off)

    for off in final_collector_offsets:
        if off in hub_collector_offsets:
            continue  # leave HUB phase-marker collectors alone
        cur = all_repl.get(off)
        if cur is None:
            cur = next(o for o in psx.records if o.offset == off)
        new_raw = bytearray(cur.raw)
        # Roll a fresh drop code from the vanilla-weighted pool.
        new_code = drng.choices(COLLECTOR_DROP_POOL,
                                weights=COLLECTOR_DROP_WEIGHTS, k=1)[0]
        struct.pack_into("<I", new_raw, 0x228 + 8, new_code)
        # Force prop[1] = 0 in case the universal pool created this
        # collector by cloning a HUB-style template (which would carry
        # prop[1] = 1..4 phase index). That extra phase index on an in-
        # level collector causes the engine to skip rendering it as a
        # shop. Zeroing here keeps it as a regular shop.
        if cur.prop_count >= 2:
            struct.pack_into("<I", new_raw, 0x228 + 1*0x20 + 8, 0)
        all_repl[off] = PsxRecord(
            offset=cur.offset, class_name=cur.class_name,
            instance_name=cur.instance_name, type_id=cur.type_id,
            instance_id=cur.instance_id, prop_count=cur.prop_count,
            pos_x=cur.pos_x, pos_y=cur.pos_y, pos_z=cur.pos_z,
            raw=bytes(new_raw),
        )

    # 6. Enemy TIER randomization. Each enemy type has its own valid tier range
    # restricted to what vanilla actually ships (out-of-range tiers spawn
    # invisible because no asset exists). We also defensively check the prop
    # type byte is 0x07 (u32) — if it's anything else (trigger volume, float,
    # bool), we leave it alone to avoid corrupting unrelated data.
    #
    # IMPORTANT: we test the POST-SWAP type (cur.type_id), not the vanilla
    # original. A Ghost→Basic_Skeleton swap should still get tier-randomized
    # as a Basic_Skeleton — otherwise every swapped enemy is stuck on tier 0.
    s = rng_master.randrange(1 << 30)
    trng = random.Random(s)
    for orig in psx.records:
        cur = all_repl.get(orig.offset, orig)
        new_rec = reroll_enemy_tier(cur, trng, world=world,
                                    tier_weights_per_type=tier_weights)
        if new_rec is not cur:
            all_repl[cur.offset] = new_rec

    # 7. Level_Column shuffle (HUB blind level select).
    # In G_HUB, 4 columns let the player pick a level. Shuffle the level IDs
    # among the 3 unlocked ones AND rename their visible labels to '???' so
    # picking a column is a blind choice.
    s = rng_master.randrange(1 << 30)
    repl = randomize_level_columns(psx, seed=s, rename_labels=True, verbose=False)
    for off, r in repl.items():
        all_repl[off] = r

    # 8. Monster_Generator parameter randomization.
    # Each generator gets randomized count/timer/radius. (We can't randomize
    # WHICH enemy spawns from the PSX side — that's bound by external script
    # logic to the generator's instance name.)
    s = rng_master.randrange(1 << 30)
    repl = randomize_monster_generators(psx, seed=s,
                                        world_gen_templates=world_gen_templates,
                                        existing_repl=all_repl,
                                        randomize_tier=randomize_gen_tier,
                                        world=world)
    for off, r in repl.items():
        all_repl[off] = r

    # 9. Ambience randomization — re-tune Rain / Snow / Fog records so each
    # playthrough has a different weather mix. Some levels go clear (muted
    # rain/snow), some get heavier weather, fog patches breathe in/out.
    s = rng_master.randrange(1 << 30)
    repl = randomize_ambience(psx, seed=s, world=world,
                              weather_template=weather_template)
    for off, r in repl.items():
        all_repl[off] = r

    return all_repl


def randomize_level_columns(
    psx: PsxFile,
    seed: int,
    rename_labels: bool = True,
    verbose: bool = False,
) -> dict[int, PsxRecord]:
    """Shuffle the level IDs among Level_Column entities (G_HUB level select).

    The level ID lives at prop[0] (idx=0, type=0x07). Vanilla values are
    1, 2, 3 for the 3 main levels and 0xFFFFFFFF for the locked/special slot.
    We shuffle 1/2/3 among the unlocked columns and optionally rewrite the
    256-byte instance name buffer to '???' so the player can't tell which
    level a column launches.
    """
    rng = random.Random(seed)
    replacements: dict[int, PsxRecord] = {}

    columns = psx.find_records_by_type(LEVEL_COLUMN_TYPE)
    if not columns:
        return replacements

    # Separate locked (0xFFFFFFFF) from unlocked (regular level IDs)
    unlocked_columns = []
    unlocked_values = []
    for c in columns:
        if c.prop_count < 1:
            continue
        eo = 0x228
        if eo + 0x20 > c.size:
            continue
        val = struct.unpack_from('<I', c.raw, eo + 8)[0]
        if val == LEVEL_COLUMN_LOCKED_VALUE:
            continue  # don't touch locked columns
        unlocked_columns.append(c)
        unlocked_values.append(val)

    if len(unlocked_columns) < 2:
        return replacements  # nothing to shuffle

    # Shuffle the values among the unlocked columns
    shuffled = unlocked_values.copy()
    rng.shuffle(shuffled)

    for c, new_val in zip(unlocked_columns, shuffled):
        new_raw = bytearray(c.raw)

        # Rewrite the level-id property
        eo = 0x228
        struct.pack_into('<I', new_raw, eo + 8, new_val)

        # Optionally rewrite the instance-name buffer (256 bytes at +0x100)
        # to '???' so the player can't see which level this column leads to.
        new_instance_name = c.instance_name
        if rename_labels:
            label = b'???\x00'
            # The instance name buffer is at offset 0x100, 256 bytes
            new_raw[0x100:0x100 + 256] = label + b'\x00' * (256 - len(label))
            new_instance_name = '???'
            if verbose:
                print(f"    [Level_Column] '{c.instance_name}' (level {next((v for vc, v in zip(unlocked_columns, unlocked_values) if vc is c), '?')}) -> '???' (level {new_val})")

        replacements[c.offset] = PsxRecord(
            offset=c.offset,
            class_name=c.class_name,
            instance_name=new_instance_name,
            type_id=c.type_id,
            instance_id=c.instance_id,
            prop_count=c.prop_count,
            pos_x=c.pos_x, pos_y=c.pos_y, pos_z=c.pos_z,
            raw=bytes(new_raw),
        )

    return replacements


def randomize_monster_generators(
    psx: PsxFile,
    seed: int,
    count_range: tuple[int, int] = (2, 6),
    timer_range_ms: tuple[int, int] = (1500, 4000),
    radius_range: tuple[float, float] = (25.0, 50.0),
    world_gen_templates: list | None = None,
    existing_repl: dict[int, PsxRecord] | None = None,
    randomize_tier: bool = False,
    tier_range: tuple[int, int] = (0, 2),
    world: str | None = None,
) -> dict[int, PsxRecord]:
    """Vary Monster_Generator spawns.

    What actually picks a generator's enemy is its full identity — the
    instance_name (script-bound) together with the selector props (3,4,5,6,8)
    that index the world's BEF wave table. NOT (prop3, prop8) alone (e.g. in
    Grave both 'courtyard1' (basic skeleton) and 'crypt1' (sword skeleton)
    share p3=p8=0 but differ in p4/p5).

    Two passes:
      1. Vanilla in-level generators keep their identity (so their gate / kill
         triggers stay intact) — only count/timer/radius are re-rolled.
      2. ITEM-CREATED generators (coins/items the universal pool turned into a
         generator) were all clones of ONE template, so they all spawned the
         same enemy ("just sword skeletons"). Each is now re-templated to a
         RANDOM vanilla generator of the same world (full name + selector
         props), so they spawn the world's varied roster (e.g. Grave gets a
         basic + sword skeleton mix). `world_gen_templates` supplies those
         per-world vanilla generator records.
    """
    rng = random.Random(seed)
    replacements: dict[int, PsxRecord] = {}
    existing_repl = existing_repl or {}
    # Verified enemy-selector pool for this world (e.g. Grave -> sword/basic/
    # zombie). When set, each generator's enemy is rolled uniformly from it.
    enemy_sel_pool = WORLD_GEN_ENEMY_SELECTORS.get((world or "").lower())

    def _set_selector_value(buf: bytearray, prop_count: int, value: int) -> None:
        """Stamp the per-world enemy selector (props idx4/5/6 as bits) to
        `value`, choosing which enemy the generator spawns."""
        bits = {idx: (value >> i) & 1
                for i, idx in enumerate(GEN_SELECTOR_BIT_INDICES)}
        for k in range(prop_count):
            eo = 0x228 + k * 0x20
            if eo + 0x20 > len(buf):
                break
            idx = struct.unpack_from('<I', buf, eo + 4)[0]
            if idx in bits:
                struct.pack_into('<I', buf, eo + 8, bits[idx])

    def _randomize_params(buf: bytearray, prop_count: int) -> None:
        for k in range(prop_count):
            eo = 0x228 + k * 0x20
            if eo + 0x20 > len(buf):
                break
            idx = struct.unpack_from('<I', buf, eo + 4)[0]
            if idx in (0, 2):
                struct.pack_into('<f', buf, eo + 8, rng.uniform(*radius_range))
            elif idx == 1:
                struct.pack_into('<I', buf, eo + 8, rng.randint(*timer_range_ms))
            elif idx == 7:
                struct.pack_into('<I', buf, eo + 8, rng.randint(*count_range))

    vanilla_offsets = {g.offset for g in psx.find_records_by_type(MONSTER_GENERATOR_TYPE)}
    templates = [t for t in (world_gen_templates or [])
                 if not _is_hub_generator(t)]

    def _write_signature(buf: bytearray, prop_count: int, sig: tuple) -> None:
        sigmap = dict(zip(GEN_SELECTOR_INDICES, sig))
        for k in range(prop_count):
            eo = 0x228 + k * 0x20
            if eo + 0x20 > len(buf):
                break
            idx = struct.unpack_from('<I', buf, eo + 4)[0]
            if idx in sigmap:
                struct.pack_into('<I', buf, eo + 8, sigmap[idx])

    def _set_tier(buf: bytearray, prop_count: int) -> None:
        """EXPERIMENTAL: set the spawned-enemy class level (prop idx 3, a u32
        with prop-type byte 0x07). Vanilla uses 0 (base) or 1 (upgraded); we
        roll 0..2 to also reach the third class level. Only writes if the prop
        is present and the expected type, so it's a no-op on generators that
        don't carry it. Runs AFTER _write_signature so it overrides the
        template's class level with the rolled one."""
        tier = max(tier_range) if FORCE_MAX_TIER else rng.randint(*tier_range)
        for k in range(prop_count):
            eo = 0x228 + k * 0x20
            if eo + 0x20 > len(buf):
                break
            idx = struct.unpack_from('<I', buf, eo + 4)[0]
            if idx == GEN_TIER_INDEX and buf[eo] == 0x07:
                struct.pack_into('<I', buf, eo + 8, int(tier))
                return

    used_sigs: list[tuple] = []

    def _pick():
        if not templates:
            return None
        choices = [t for t in templates
                   if _gen_signature(t) not in used_sigs] or templates
        t = rng.choice(choices)
        used_sigs.append(_gen_signature(t))
        return t

    # 1. Vanilla in-level generators — NO LONGER LOCKED. We re-roll their enemy
    # by swapping in a random spawn signature from the world's generator pool,
    # but KEEP each generator's own instance_name so any gate / kill triggers
    # bound to that name stay intact. Count/timer/radius are varied too.
    #
    # HUB phase generators (P1_/P2_/... — e.g. Castle's hub waves) are normally
    # left alone. But for worlds with a verified enemy-selector pool we DO
    # randomize them (tier + params here, enemy in pass 3) so Castle's hub
    # generators aren't locked out of the randomizer. Their phase index (idx8)
    # and instance_name are still kept so the phase keeps firing — only the
    # enemy/tier/params change.
    for g in psx.find_records_by_type(MONSTER_GENERATOR_TYPE):
        cur = existing_repl.get(g.offset, g)
        if cur.type_id != MONSTER_GENERATOR_TYPE:
            continue  # this slot got randomized into something else
        if _is_hub_generator(cur) and not enemy_sel_pool:
            continue  # HUB phase markers — only randomized in pooled worlds
        new_raw = bytearray(cur.raw)
        tpl = _pick()
        if tpl is not None:
            _write_signature(new_raw, cur.prop_count, _gen_signature(tpl))
        _randomize_params(new_raw, cur.prop_count)
        if randomize_tier:
            _set_tier(new_raw, cur.prop_count)
        replacements[g.offset] = PsxRecord(
            offset=cur.offset, class_name=cur.class_name,
            instance_name=cur.instance_name, type_id=cur.type_id,
            instance_id=cur.instance_id, prop_count=cur.prop_count,
            pos_x=cur.pos_x, pos_y=cur.pos_y, pos_z=cur.pos_z,
            raw=bytes(new_raw),
        )

    # 2. Item-created generators — full template clone (no triggers to keep),
    # so they spawn the world's varied roster.
    if templates:
        for off, rec in list(existing_repl.items()):
            if rec.type_id != MONSTER_GENERATOR_TYPE or off in vanilla_offsets:
                continue
            if _is_hub_generator(rec):
                continue
            tpl = _pick()
            new_raw = bytearray(tpl.raw)
            # Keep the slot's world position + instance id (avoid id clashes).
            struct.pack_into('<3f', new_raw, RECORD_HEADER_SIZE + 0x10,
                             rec.pos_x, rec.pos_y, rec.pos_z)
            struct.pack_into('<I', new_raw, RECORD_HEADER_SIZE + 0x08,
                             rec.instance_id)
            _randomize_params(new_raw, tpl.prop_count)
            if randomize_tier:
                _set_tier(new_raw, tpl.prop_count)
            replacements[off] = PsxRecord(
                offset=off, class_name=tpl.class_name,
                instance_name=tpl.instance_name, type_id=MONSTER_GENERATOR_TYPE,
                instance_id=rec.instance_id, prop_count=tpl.prop_count,
                pos_x=rec.pos_x, pos_y=rec.pos_y, pos_z=rec.pos_z,
                raw=bytes(new_raw),
            )

    # 3. Enemy-selector override for worlds with a verified slot map
    # (WORLD_GEN_ENEMY_SELECTORS). Applies to EVERY generator — including HUB
    # phase generators such as Castle's hub waves — rewriting the enemy-selecting
    # props idx4/5/6. Combined with pass 1 (which now also randomizes hub
    # generators' tier + params in pooled worlds), Castle hub generators are
    # fully randomized. Only the phase index (idx8) and instance_name are kept
    # so the hub phase still fires; everything else is rolled.
    if enemy_sel_pool:
        gen_by_offset = {g.offset: g
                         for g in psx.find_records_by_type(MONSTER_GENERATOR_TYPE)}
        offsets = set(gen_by_offset)
        offsets |= {off for off, r in replacements.items()
                    if r.type_id == MONSTER_GENERATOR_TYPE}
        offsets |= {off for off, r in existing_repl.items()
                    if r.type_id == MONSTER_GENERATOR_TYPE}
        for off in offsets:
            cur = (replacements.get(off) or existing_repl.get(off)
                   or gen_by_offset.get(off))
            if cur is None or cur.type_id != MONSTER_GENERATOR_TYPE:
                continue
            new_raw = bytearray(cur.raw)
            _set_selector_value(new_raw, cur.prop_count, rng.choice(enemy_sel_pool))
            replacements[off] = PsxRecord(
                offset=cur.offset, class_name=cur.class_name,
                instance_name=cur.instance_name, type_id=cur.type_id,
                instance_id=cur.instance_id, prop_count=cur.prop_count,
                pos_x=cur.pos_x, pos_y=cur.pos_y, pos_z=cur.pos_z,
                raw=bytes(new_raw),
            )

    return replacements


# Selector props that (together with the instance_name) determine which enemy
# a Monster_Generator spawns from the world's BEF wave table.
GEN_SELECTOR_INDICES = (3, 4, 5, 6, 8)


# The spawned enemy's class level (tier) is carried by the generator's prop
# idx 3 — a u32 with prop-type byte 0x07, the SAME type used for the tier prop
# on placed enemies. Proof: in Swamp both sword-skeleton generators share the
# enemy-selecting flags (idx4=1, idx5=0) but 'ribs1' has idx3=0 (normal sword
# skeleton) while 'cave1' has idx3=1 (UPGRADED sword skeleton). Vanilla
# generators only use 0 or 1, but placed enemies of the same prop type go up to
# 2 (three class levels total), so we roll 0..2 to cover ALL available classes.
# (idx 8 is also type 0x07 but is always 0 and does nothing.)
GEN_TIER_INDEX = 3
GEN_TIER_RANGE = (0, 2)

# Verified per-world generator enemy selectors. The enemy a generator spawns is
# chosen by props idx4/idx5/idx6, which behave as independent one-hot enemy
# flags (idx4=bit0, idx5=bit1, idx6=bit2 -> a value). Confirmed in-game for
# Grave: value 1 (idx4) = sword skeleton, value 2 (idx5) = basic skeleton,
# value 4 (idx6) = ZOMBIE. Listing a world here makes its generators roll
# uniformly among these enemies (so Grave generators now mix sword/basic/zombie
# instead of only the two vanilla types). Worlds NOT listed keep the original
# template-signature behavior (we haven't verified their full slot map).
GEN_SELECTOR_BIT_INDICES = (4, 5, 6)
WORLD_GEN_ENEMY_SELECTORS = {
    # VERIFIED in-game (Grave): idx4=1 -> sword skeleton, idx5=1 -> basic
    # skeleton, idx6=1 -> zombie.
    "grave": (1, 2, 4),
    # GUESS for Swamp (unverified): same core enemies in the same BEF order as
    # Grave; its vanilla generators only use value 1 (sword). Reusing Grave's
    # map so Swamp generators can also roll basic/zombie.
    "swamp": (1, 2, 4),
    # GUESS for Castle (unverified): Castle's only generators are HUB phase
    # waves (all spawn basic = value 2 in vanilla). Castle's BEF loads
    # sword/basic/zombie in the same relative order as Grave, so reuse Grave's
    # map. Only the enemy is changed; phase tier/index are preserved (see
    # pass 3). Verify hub phases still clear in-game.
    "castle": (1, 2, 4),
    # ICE: sword + basic only — NO zombie (ICE's BEF has no Basic_Zombie mesh).
    # value 1 = the vanilla Ice generator slot, value 2 = basic skeleton (guess,
    # by analogy with Grave's BEF order). Verify in-game.
    "ice": (1, 2),
    # UNDER already spawns zombies from its own vanilla generators (arena1), so
    # it keeps the template system.
}


def _gen_signature(rec: PsxRecord) -> tuple:
    """The enemy-selecting signature of a generator: values at the selector
    prop indices."""
    vals = {}
    for k in range(rec.prop_count):
        eo = 0x228 + k * 0x20
        if eo + 0x20 > rec.size:
            break
        idx = struct.unpack_from('<I', rec.raw, eo + 4)[0]
        if idx in GEN_SELECTOR_INDICES:
            vals[idx] = struct.unpack_from('<I', rec.raw, eo + 8)[0]
    return tuple(vals.get(i, 0) for i in GEN_SELECTOR_INDICES)


def _is_hub_generator(rec: PsxRecord) -> bool:
    n = (rec.instance_name or "").lower()
    return n.startswith(("p1_", "p2_", "p3_", "p4_",
                         "fp1_", "fp2_", "fp3_", "fp4_"))


def collect_world_gen_templates(psx_list) -> list[PsxRecord]:
    """Gather one representative Monster_Generator record per distinct enemy
    signature across a world's PSX files (HUB phase markers excluded). These
    are used as templates to re-spawn item-created generators with the world's
    varied enemy roster."""
    templates: list[PsxRecord] = []
    seen: set[tuple] = set()
    for psx in psx_list:
        for g in psx.find_records_by_type(MONSTER_GENERATOR_TYPE):
            if _is_hub_generator(g):
                continue
            sig = _gen_signature(g)
            if sig not in seen:
                seen.add(sig)
                templates.append(g)
    return templates


# ============================================================================
# Ambience randomization (weather + fog)
# ============================================================================
# Vanilla Maximo has 3 ambience entity types we can safely re-tune:
#   Rain (0xD0)        — global rain emitter, size 0x268, 2 small-int props
#   Snow (0xCF)        — global snow emitter, size 0x268, 2 small-int props
#   Fog_Volume (0xCE)  — local fog patch, size 0x2A8, 4 props
#
# We DON'T type-swap weather emitters across worlds. The BEF only loads
# weather assets that vanilla used (rain particles in GRAVE/SWAMP, snow in
# ICE, fog everywhere) so flipping a Rain into a Snow gives invisible
# weather. We just re-tune the EXISTING records' density / radius / on-off
# so each playthrough has a different feel — some levels heavier rain, some
# muted, some fog rolled denser, etc.
RAIN_TYPE = 0xD0
SNOW_TYPE = 0xCF
FOG_VOLUME_TYPE = 0xCE

# Rain/Snow are kept VISIBLE whenever a level has (or is given) an emitter —
# we no longer mute them to nothing, so weather reliably shows up. Ranges are
# tuned to be clearly visible without flooding the screen.
WEATHER_MUTE_PROBABILITY = 0.0  # kept for backwards-compat; muting disabled

RAIN_DENSITY_RANGE = (45, 95)       # idx=0 particle count (always visible)
RAIN_INTENSITY_RANGE = (25, 60)     # idx=1

# Snow ranges derived from vanilla I_HUB sample (idx=0 val=75, idx=1 val=25).
SNOW_DENSITY_RANGE = (45, 110)      # idx=0
SNOW_INTENSITY_RANGE = (20, 55)     # idx=1

# Rain in GRAVE/SWAMP intro areas is driven by Rain_Trigger (0xD7) zones, not
# the emitter — each zone's idx=1 is the rain level applied when the player
# crosses it. We randomize the "on" zones (idx=1 > 0) so the real, in-level
# rain varies, and leave "off" zones (idx=1 == 0) at 0 so caves stay dry.
RAIN_TRIGGER_TYPE = 0xD7
SNOW_TRIGGER_TYPE = 0xD8
RAIN_TRIGGER_ON_RANGE = (25, 80)

# Per-level chance that a weather-capable level actually HAS weather this seed.
# Rolled independently per level, so across a playthrough some levels are
# rainy/snowy and others are clear — and it differs seed to seed.
WEATHER_PRESENT_PROBABILITY = 0.6

# Worlds whose BEF loads weather assets, and which emitter type they support.
# Used to INJECT an emitter into levels of that world that have none, so e.g.
# rain appears across all of Grave instead of only the intro.
WORLD_WEATHER_TYPE = {
    "grave": RAIN_TYPE,
    "swamp": RAIN_TYPE,
    "ice": SNOW_TYPE,
}

# Fog density (idx=0 small-int) and blend (idx=7 small-float) ranges
# derived from vanilla observation across all 479 Fog_Volume records:
#   density observed: 10..75 (small ints, common values 25, 30, 35, 40, 45)
#   blend observed:   0.0..3.0 (common 0.5, 0.6, 0.7, 1.0)
#
# Radius (idx=1 big float) is NOT randomized — vanilla shows the value is
# signed (-883..+819) and behaves like a positional offset / placement
# coordinate rather than a size. Touching it would relocate the fog, not
# resize it.
FOG_DENSITY_RANGE = (10, 75)         # idx=0 small-int (vanilla min..max)
FOG_BLEND_RANGE = (0.0, 3.0)         # idx=7 small float (vanilla min..max)


def randomize_ambience(
    psx: PsxFile,
    seed: int,
    world: str | None = None,
    weather_template: PsxRecord | None = None,
    present_probability: float = WEATHER_PRESENT_PROBABILITY,
) -> dict[int, PsxRecord]:
    """Re-tune weather + fog so each playthrough has a different ambience mix,
    and make weather appear (or not) across a world.

    Two different behaviors, on purpose:

      - VANILLA weather (a level that already ships with a Rain/Snow emitter or
        trigger — e.g. Grave Danger's rain, the Ice levels' snow) is ALWAYS
        kept on, with its INTENSITY randomized per seed. These levels are
        iconically rainy/snowy, so they should never go bone-dry — only vary
        in strength. This is what fixes "Grave Danger lost its rain".

      - INJECTED weather (a naturally-dry level in a weather world) is rolled
        present/absent with `present_probability`, seed-derived. So different
        dry levels get surprise weather on different seeds — that's the
        seed-to-seed variety.

    Details:
      - Rain (0xD0) / Snow (0xCF) emitters: trigger-driven levels keep the
        emitter and let randomized triggers drive intensity; others get a
        visible randomized density. Never muted.
      - Rain_Trigger (0xD7) / Snow_Trigger (0xD8) "on" zones (value > 0):
        intensity always re-randomized. "Off" zones (value 0) stay off so
        caves/interiors remain dry.
      - Fog density (idx=0) / blend (idx=7) always re-rolled across the vanilla
        range (fog is independent of the weather roll).
      - INJECTION: if `world` supports weather (grave/swamp = rain, ice = snow)
        and the injection roll is present but the level has NO emitter, a
        weather_template is cloned into a spare Fog_Volume slot.

    Left intact: fog radius (idx=1); only scalar props at targeted indices are
    written (defensive).
    """
    rng = random.Random(seed)
    replacements: dict[int, PsxRecord] = {}

    # Injection roll (seed-derived): controls whether a naturally-DRY level in
    # a weather world gets weather injected this seed. Existing/vanilla weather
    # is unaffected by this — it always stays on (intensity randomized).
    inject_present = rng.random() < present_probability

    def _rebuild(rec: PsxRecord, new_raw: bytes) -> PsxRecord:
        return PsxRecord(
            offset=rec.offset, class_name=rec.class_name,
            instance_name=rec.instance_name, type_id=rec.type_id,
            instance_id=rec.instance_id, prop_count=rec.prop_count,
            pos_x=rec.pos_x, pos_y=rec.pos_y, pos_z=rec.pos_z,
            raw=new_raw,
        )

    def _set_scalar(buf: bytearray, eo: int, ptype: int, value) -> None:
        """Write a scalar prop value preserving its on-disk type (float vs
        int). Small-float props use type byte 0x06; everything else here is
        an integer slot."""
        if ptype == 0x06:
            struct.pack_into('<f', buf, eo + 8, float(value))
        else:
            struct.pack_into('<I', buf, eo + 8, int(value))

    # 1. Existing Rain (0xD0) / Snow (0xCF) emitters + their triggers are left
    # COMPLETELY UNTOUCHED. Iconic rainy/snowy levels (Grave Danger's rain, the
    # Ice levels' snow) keep their EXACT vanilla weather. Earlier versions
    # randomized the emitter/trigger intensity, which silently broke Grave
    # Danger's rain in-game — so we no longer modify existing weather at all.
    # We only record which weather types already exist, to skip injection below.
    existing_weather: set[int] = set()
    for r in (psx.find_records_by_type(RAIN_TYPE)
              + psx.find_records_by_type(SNOW_TYPE)):
        existing_weather.add(r.type_id)

    # 3. Fog volumes — vary density and blend across the FULL vanilla range
    # so each fog patch has independently rolled values capped at vanilla
    # min/max. Radius is left untouched (positional / signed in vanilla).
    fog_records = psx.find_records_by_type(FOG_VOLUME_TYPE)
    for r in fog_records:
        new_raw = bytearray(r.raw)
        for k in range(r.prop_count):
            eo = 0x228 + k * 0x20
            if eo + 0x20 > r.size:
                break
            ptype = new_raw[eo]
            idx = struct.unpack_from('<I', new_raw, eo + 4)[0]
            if idx == 0 and ptype == 0x0E:
                # density (small-int) — random across vanilla range
                struct.pack_into('<I', new_raw, eo + 8,
                                 rng.randint(*FOG_DENSITY_RANGE))
            elif idx == 7 and ptype == 0x06:
                # blend (small float) — random across vanilla range
                struct.pack_into('<f', new_raw, eo + 8,
                                 rng.uniform(*FOG_BLEND_RANGE))
            # idx == 1 (radius) intentionally left intact — see docstring.
        replacements[r.offset] = _rebuild(r, bytes(new_raw))

    # 4. WEATHER INJECTION — if this world supports weather but the level has
    # no emitter, recycle a spare Fog_Volume slot into a weather emitter so
    # weather appears here too (e.g. rain in Grave levels beyond the intro).
    # The world's BEF already loads the weather assets, and the Rain/Snow
    # record (0x268) is smaller than a Fog_Volume (0x2A8), so it fits the slot.
    wtype = WORLD_WEATHER_TYPE.get((world or "").lower())
    if (inject_present and wtype is not None and weather_template is not None
            and wtype not in existing_weather):
        victim = next((r for r in fog_records
                       if r.size >= len(weather_template.raw)), None)
        if victim is not None:
            is_snow = wtype == SNOW_TYPE
            dens = rng.randint(*(SNOW_DENSITY_RANGE if is_snow
                                 else RAIN_DENSITY_RANGE))
            inten = rng.randint(*(SNOW_INTENSITY_RANGE if is_snow
                                  else RAIN_INTENSITY_RANGE))
            replacements[victim.offset] = _make_weather_emitter(
                weather_template, victim, dens, inten)

    return replacements


def _make_weather_emitter(template: PsxRecord, victim: PsxRecord,
                          density: int, intensity: int) -> PsxRecord:
    """Clone a Rain/Snow emitter from `template` into `victim`'s record slot,
    keeping victim's position and a unique instance name, with the given
    density (idx=0) and intensity (idx=1)."""
    raw = bytearray(template.raw)
    struct.pack_into("<3f", raw, RECORD_HEADER_SIZE + 0x10,
                     victim.pos_x, victim.pos_y, victim.pos_z)
    for k in range(template.prop_count):
        eo = 0x228 + k * 0x20
        if eo + 0x20 > len(raw):
            break
        ptype = raw[eo]
        if ptype not in (0x03, 0x06, 0x0E):
            continue
        idx = struct.unpack_from('<I', raw, eo + 4)[0]
        if idx == 0:
            if ptype == 0x06:
                struct.pack_into('<f', raw, eo + 8, float(density))
            else:
                struct.pack_into('<I', raw, eo + 8, int(density))
        elif idx == 1:
            if ptype == 0x06:
                struct.pack_into('<f', raw, eo + 8, float(intensity))
            else:
                struct.pack_into('<I', raw, eo + 8, int(intensity))
    prefix = "Rain_inj" if template.type_id == RAIN_TYPE else "Snow_inj"
    name = f"{prefix}_{victim.instance_id}"
    nb = name.encode('ascii') + b'\x00'
    raw[0x100:0x100 + 256] = nb + b'\x00' * (256 - len(nb))
    return PsxRecord(
        offset=victim.offset, class_name=template.class_name,
        instance_name=name, type_id=template.type_id,
        instance_id=victim.instance_id, prop_count=template.prop_count,
        pos_x=victim.pos_x, pos_y=victim.pos_y, pos_z=victim.pos_z,
        raw=bytes(raw),
    )
