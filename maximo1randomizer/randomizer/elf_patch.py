"""
ELF (SLUS_200.17) patching for runtime-displayed level titles.

The PSX-stored Level_Column instance_name is just an internal identifier.
The actual level TITLE shown on-screen is hardcoded in the retail ELF at
fixed offsets, looked up by world+level_id.

Renaming Level_Columns alone has no visual effect — the engine reads
from the ELF string table. We have to patch the ELF too.

All 18 known level-title slots in retail SLUS_200.17 (verified by string
search against a Maximo level list):

  GRAVE:
    0x000CD1C8: 16 - "Grave Danger"      (world name)
    0x000CD1D8: 16 - "The Tomb Tower"    (intro/locked area)
    0x000CD1E8: 16 - "Dead Heat"         (level 1)
    0x000CD1F8: 24 - "Coffin Canyon"     (level 2)
    0x000CD210: 16 - "Bad to the Bone"   (level 3)
    0x000CD220: 16 - "Ghastly Gus"       (boss)
  SWAMP:
    0x000CD230: 16 - "Watery Grave"      (locked column)
    0x000CD240: 16 - "The Magic Tree"    (intro)
    0x000CD250: 16 - "Voodoo Village"    (level 1)
    0x000CD260: 16 - "'Dem Bones"        (level 2)
    0x000CD270: 24 - "Quick and the Dead" (level 3)
    0x000CD288: 24 - "Bokor LaBas"       (boss)
  ICE:
    0x000CD2A0: 24 - "Shiver Me Timbers" (locked column)
    0x000CD2B8: 24 - "The Dark House"    (intro)
    0x000CD2D0: 32 - "Go With The Floe"  (level 1)
    0x000CD2F0: 24 - "Dead in the Water" (level 2)
    0x000CD308: 24 - "Cannonball Run"    (level 3)
    0x000CD320: 16 - "Captain Cadaver"   (boss)
  UNDER:
    0x000CD330: 24 - "Infernal Devices"  (locked column)
    0x000CD348: 24 - "The Iron Tower"    (intro)
    0x000CD360: 16 - "Crushed Spirits"   (level 1)
    0x000CD370: 32 - "The Unkindest Cut" (level 2)
    0x000CD390: 16 - "Down The Gullet"   (level 3)
    0x000CD3A0: 24 - "Lord Glutterscum"  (boss)
  CASTLE:
    0x000CD3B8: 16 - "The Seige"         (locked column)
    0x000CD3C8: 24 - "The Keep"          (level 2 in C_HUB)
    0x000CD3E0: 16 - "The Great Escape"  (level 1 in C_HUB)
"""
from __future__ import annotations
import random
import re
import struct
from pathlib import Path


# Known boot-executable filenames per region. The randomizer auto-detects which
# one is present and applies the same patches to it (all patches are located by
# signature or anchor, so they are region-independent).
#   US: SLUS_200.17   JP: SLPM_621.27
KNOWN_EXECUTABLES = ("SLUS_200.17", "SLPM_621.27")


def find_executable(folder: Path | str) -> Path | None:
    """Return the path to the game's boot executable inside `folder`, matching
    any known region filename (case-insensitive). None if not found."""
    folder = Path(folder)
    names = {n.upper() for n in KNOWN_EXECUTABLES}
    # Fast path: exact known names.
    for n in KNOWN_EXECUTABLES:
        p = folder / n
        if p.exists():
            return p
    # Case-insensitive fallback.
    if folder.is_dir():
        for child in folder.iterdir():
            if child.is_file() and child.name.upper() in names:
                return child
    return None


# Title-table anchor for region-independent level-title patching. The English
# title block is byte-identical across US/JP; only its file offset shifts. We
# find this anchor and apply the delta to every LEVEL_TITLE_SLOTS offset.
TITLE_TABLE_ANCHOR = b"Grave Danger\x00"
TITLE_TABLE_ANCHOR_US_OFFSET = 0x000CD1C8


# ============================================================================
# Per-level ability/enchantment drop tables
# ============================================================================
# The retail SLUS holds 24 per-level "ability weight" tables (one per level),
# 31 bytes each at a 32-byte stride. Each table is indexed by ABILITY ID and
# the byte value is a spawn/drop weight (vanilla uses 0/5/10/15/20/25). These
# tables decide which power-ups a level can drop (verified: Grave's table has
# Fire Sword (id 15)=5, Swamp's has Sun (17)=5, Ice's has Ice (16)=5 — matching
# the world-locked sword enchantments enemies drop in vanilla).
#
# Ability id semantics (measured from the demo executable MAXDEMOR.ELF):
# ability pickups are spawned by the generic Ability entity (BEF type 0x20) and
# the ability id alone decides what the pickup is. SetAbility__6PlayerFib
# indexes a 32-entry, 20-byte-stride descriptor table `AbilityBits` @ demo
# vaddr 0x001ED6B0 and switches on field +0 of the entry. From that switch:
#   id 10 = Smart Bomb (NOT a shield — the old note here was wrong)
#   id 12 = the shield equip / upgrade bit
#   id 13 = SHIELD STAMINA: field0 == 0 -> GameVarAdd(this, 10, 5), i.e. +5
#           shield points, gated on the shield being equipped
#   id 24 = SWORD STAMINA: field0 == -7 -> GameVarAdd(this, 11, 5), +5 sword
#           units
#   ids 19/20/21 = wind / magnetic / lightning shield
#   ids 25 and 26 = code -8 no-ops (no effect), yet vanilla still gives them
#           non-zero weight in the drop tables
#   id 31 = `ration` — OUTSIDE the 31-byte drop table, so it is not droppable
#           and must never be written here
# 15-18 -> Fire / Ice / Sun / Armageddon sword: the ORDER is INFERRED (those
# descriptor entries carry no name string), not measured. 22=Health.
#
# To let ANY world drop ANY skill/enchantment, we make every ability that
# vanilla drops SOMEWHERE droppable in EVERY level (with seed-randomized
# weights). The set of indices is computed from the vanilla tables themselves
# (so we only ever enable proven-valid drops), always including the four sword
# enchantments. Health (22) is left per-level so the health economy is
# unchanged. The block is located by the vanilla signature of the first table
# (AbilityGIntro) so we don't hard-code a file offset.
ABILITY_TABLE_SIGNATURE = bytes.fromhex(
    "00000f0a00000a00000000000f0f00050000000000000f00050a0f00000000")
ABILITY_TABLE_COUNT = 24
ABILITY_TABLE_STRIDE = 32
ABILITY_TABLE_LEN = 31          # bytes actually used per table (32nd is pad)
SWORD_ENCHANT_INDICES = (15, 16, 17, 18)  # Fire / Ice / Sun / Armageddon
HEALTH_ABILITY_INDEX = 22       # consumable — left per-level (not forced)
ABILITY_DROP_WEIGHTS = (5, 10, 15)

# Measured stamina pickups (see the ability-id notes above).
ABILITY_SHIELD_STAMINA_INDEX = 13   # +5 shield points (GameVarAdd var 10)
ABILITY_SWORD_STAMINA_INDEX = 24    # +5 sword units  (GameVarAdd var 11)

# Editable weight range. 25 is the highest weight vanilla ever uses; the table
# byte's semantics ABOVE 25 are unverified, so the editor stays inside the
# proven-valid range. Dominance is still reachable by zeroing the other ids.
ABILITY_DROP_WEIGHT_MIN = 0
ABILITY_DROP_WEIGHT_MAX = 25

# Droppable ability ids (0..30 — the 31 usable bytes of a table) and their
# display names. Insertion-ordered; id 31 (`ration`) is deliberately absent.
ABILITY_DROP_NAMES: dict[int, str] = {
    0: "Increase Sword Power",
    1: "Sword 720 Spin",
    2: "Double Slash",
    3: "Mighty Blow",
    4: "Magic Bolt",
    5: "Masquerade (Mask of Sorrow)",
    6: "Return Shield",
    7: "Wide Shockwave",
    8: "Damage Shockwave",
    9: "Find Treasure",
    10: "Smart Bomb",
    11: "Hover Shield",
    12: "Shield (equip / upgrade)",
    13: "Shield Stamina (+5)",
    14: "Increase Armor",
    15: "Fire Sword",
    16: "Ice Sword",
    17: "Sun Sword",
    18: "Armageddon Sword",
    19: "Wind Shield",
    20: "Magnetic Shield",
    21: "Lightning Shield",
    22: "Full Health",
    23: "Increase Throw",
    24: "Sword Stamina (+5)",
    25: "Unused / no effect (id 25)",
    26: "Unused / no effect (id 26)",
    27: "Purple Kiss",
    28: "Green Kiss",
    29: "Blue Kiss",
    30: "Red Kiss",
}

# Ids the EDITOR does not offer. This is a UI/editor concern ONLY:
#   * 25 and 26 are code -8 no-ops — they change no game variable, so a
#     drop-rate slider for them is meaningless even though vanilla gives them
#     non-zero weight in the drop tables.
#   * 27-30 are the four kisses; they are not wanted as randomizable drops.
# `ABILITY_DROP_NAMES` above stays complete (the measured id -> meaning record
# recovered from AbilityBits) and `patch_ability_drops` still ACCEPTS any id in
# 0..ABILITY_TABLE_LEN-1, so this exclusion narrows only what the editor
# offers and does not change the patcher's contract. (Same convention as
# SHIELD_COST_PRESETS, the exposed subset, vs SHIELD_COST_MAX, the patcher's
# own wider limit.)
ABILITY_DROP_EDITOR_EXCLUDED = (25, 26, 27, 28, 29, 30)

# Insertion-ordered editable subset, derived so no id or name is duplicated.
ABILITY_DROP_EDITABLE: dict[int, str] = {
    _i: _n for _i, _n in ABILITY_DROP_NAMES.items()
    if _i not in ABILITY_DROP_EDITOR_EXCLUDED
}


def _sanitize_ability_overrides(overrides: dict | None) -> tuple[dict, list]:
    """Split `{ability_id: weight}` into (valid, rejected). Valid ids are ints
    in 0..ABILITY_TABLE_LEN-1 with weights clamped to the proven range."""
    valid: dict[int, int] = {}
    rejected: list = []
    for k, v in (overrides or {}).items():
        try:
            idx = int(k)
        except (TypeError, ValueError):
            rejected.append(k)
            continue
        if isinstance(k, bool) or not (0 <= idx < ABILITY_TABLE_LEN):
            rejected.append(k)
            continue
        try:
            w = float(v)
        except (TypeError, ValueError):
            rejected.append(k)
            continue
        w = max(ABILITY_DROP_WEIGHT_MIN, min(ABILITY_DROP_WEIGHT_MAX, w))
        valid[idx] = int(round(w))
    return valid, rejected


def patch_ability_drops(data: bytearray, seed: int | None,
                        overrides: dict | None = None) -> dict:
    """Randomize the per-level ability/skill DROP pool so every world can drop
    every skill and sword enchantment, not just its vanilla ones.

    Mutates `data` (the SLUS bytes) in place. Computes the union of every
    ability index that vanilla drops in any level (always including the four
    sword enchantments, excluding Health) and, for each of the 24 level tables,
    sets each of those indices to a seed-random non-zero weight. Returns stats.
    """
    ov, rejected = _sanitize_ability_overrides(overrides)
    base = bytes(data).find(ABILITY_TABLE_SIGNATURE)
    if base < 0:
        res = {"found": False, "patched": 0, "base": None, "abilities": 0,
               "overrides": ov, "override_ids": sorted(ov), "zero_guard": []}
        if rejected:
            res["rejected"] = rejected
        return res
    snap = bytes(data[base:base + ABILITY_TABLE_COUNT * ABILITY_TABLE_STRIDE])
    # `seed is None` + a caller-supplied override set means "apply only the
    # overrides"; the blanket randomization stays off (even if every id the
    # caller passed was rejected).
    randomize = seed is not None or not overrides
    if randomize:
        # Union of ability indices vanilla drops anywhere (proven-valid drops).
        union: set[int] = set(SWORD_ENCHANT_INDICES)
        for i in range(ABILITY_TABLE_COUNT):
            for idx in range(ABILITY_TABLE_LEN):
                if snap[i * ABILITY_TABLE_STRIDE + idx] != 0:
                    union.add(idx)
        union.discard(HEALTH_ABILITY_INDEX)
        # User-set ids are written verbatim, so keep the blanket randomization
        # from clobbering them.
        union -= set(ov)
    else:
        union = set()
    union_sorted = sorted(union)
    rng = random.Random((seed if seed is not None else 0) ^ 0x5AB11D1E)
    patched = 0
    for i in range(ABILITY_TABLE_COUNT):
        toff = base + i * ABILITY_TABLE_STRIDE
        for idx in union_sorted:
            o = toff + idx
            if o < len(data):
                data[o] = rng.choice(ABILITY_DROP_WEIGHTS)
                patched += 1
        for idx, w in ov.items():
            o = toff + idx
            if o < len(data):
                data[o] = w
                patched += 1
    # Zero guard: an all-zero drop table has unverified engine behaviour, so
    # restore that table's highest vanilla weight at its original index.
    zero_guard: list[dict] = []
    for i in range(ABILITY_TABLE_COUNT):
        toff = base + i * ABILITY_TABLE_STRIDE
        end = min(toff + ABILITY_TABLE_LEN, len(data))
        if any(data[toff:end]):
            continue
        vidx, vw = -1, 0
        for idx in range(ABILITY_TABLE_LEN):
            w = snap[i * ABILITY_TABLE_STRIDE + idx]
            if w > vw:
                vidx, vw = idx, w
        if vidx < 0:
            continue
        o = toff + vidx
        if o < len(data):
            data[o] = vw
            zero_guard.append({"table": i, "index": vidx, "weight": vw})
    res = {"found": True, "patched": patched, "base": base,
           "tables": ABILITY_TABLE_COUNT, "abilities": len(union),
           "overrides": ov, "override_ids": sorted(ov),
           "zero_guard": zero_guard}
    if rejected:
        res["rejected"] = rejected
    return res


# Backwards-compatible alias (older callers).
def patch_sword_enchant_drops(data: bytearray, seed: int | None,
                              overrides: dict | None = None) -> dict:
    return patch_ability_drops(data, seed, overrides)


# ============================================================================
# Damage Maximo TAKES from enemies
# ============================================================================
# Maximo's on-screen health bar is GameVar #2 (the player game-variable array
# lives at gPlayer+0x48, so index 2 == gPlayer+0x50 — the float the HUD reads
# and CheckPlayerFullHealth tests). Health damage is applied in Player::OnHit
# via a call to GameVarSub(this, /*var*/2, /*amount*/damage):
#     addiu a1, zero, 2      # 24050002   var index 2 = HEALTH
#     sw    v0, 0(v1)        # ac620000
#     por   a0, s4, zero     # 72802628   a0 = this (gPlayer)
#     por   a2, s2, zero     # 72403628   a2 = s2 = incoming damage   <-- target
#     jal   GameVarSub
#     por   a3, zero, zero   # 70003e28
# (The earlier player+0x70 write that we used to patch is a DIFFERENT HUD bar,
# not health — scaling it had no visible effect. Verified against the demo
# debug symbols: gPlayer @0x287500, health == GameVar #2, OnHit feeds the
# damage to GameVarSub with var index 2.)
#
# We replace the `por a2, s2, zero` move with `sra/sll a2, s2, N`, which both
# moves the damage into a2 AND scales it in a single instruction — no free slot
# needed and it lands exactly on the health-damage argument. The 16-byte
# sequence (addiu a1,2 / sw / por a0,s4 / por a2,s2) is unique in the SLUS, so
# we locate it by signature and patch its last word.
DAMAGE_HIT_SIGNATURE = bytes.fromhex("02000524000062ac2826807228364072")
DAMAGE_POR_A2_S2 = 0x72403628  # por a2, s2, zero  (the instruction we replace)
DAMAGE_SCALE_WORDS = {
    "0.25": 0x00123083,  # sra a2,s2,2  -> x0.25 damage
    "0.50": 0x00123043,  # sra a2,s2,1  -> x0.5  damage
    "2x":   0x00123040,  # sll a2,s2,1  -> x2    damage
    "4x":   0x00123080,  # sll a2,s2,2  -> x4    damage
}


def patch_damage_taken(data: bytearray, mode: str | None) -> dict:
    """Scale the damage Maximo takes from enemies by a power-of-two factor.

    mode: 'quarter' | 'half' | 'normal' | 'double' | 'quad' (None/'normal' =
    no change). Mutates `data` in place. Returns a stats dict.

    Patches the `por a2, s2, zero` that feeds the incoming damage into
    GameVarSub(this, 2 /*HEALTH*/, amount) inside Player::OnHit, turning the
    plain move into a power-of-two scale of the damage.
    """
    if not mode or mode == "normal":
        return {"applied": False, "mode": "normal"}
    word = DAMAGE_SCALE_WORDS.get(mode)
    if word is None:
        return {"applied": False, "mode": mode, "reason": "unknown mode"}
    sig = bytes(data).find(DAMAGE_HIT_SIGNATURE)
    if sig < 0:
        return {"found": False, "applied": False, "mode": mode}
    slot = sig + 12  # the `por a2, s2, zero` (last word of the signature)
    if bytes(data[slot:slot + 4]) != struct.pack("<I", DAMAGE_POR_A2_S2):
        return {"found": True, "applied": False, "mode": mode,
                "reason": "expected 'por a2,s2' not found at target"}
    struct.pack_into("<I", data, slot, word)
    return {"found": True, "applied": True, "mode": mode, "slot": slot}


# ============================================================================
# Damage Maximo DEALS to enemies
# ============================================================================
# Maximo's attack damage is configured in Player::SetAction, which writes the
# attacking object's damage range via DEDynamicObject::SetDamage(this, min,
# max). The `min` value (register a1) is the one scaled by Maximo's sword-power
# level (the per-action base in the low nibble of the action byte, multiplied
# by the sword upgrade level), so it is Maximo's effective melee damage. When
# the sword hitbox collides with an enemy, ObjectCollision reads it back with
# GetDamage and forwards it to CVirtualMachine::OnHit (the enemy-hit path —
# distinct from OnPlayerHit, so this does NOT touch the damage Maximo takes).
#
# Just before the SetDamage call the compiler emits a redundant
# `andi a1, a1, 0xff` (a1 was already masked one instruction earlier). We
# replace that redundant mask with `sra/sll a1, a1, N` to scale Maximo's
# outgoing melee damage in a single instruction. The 16-byte sequence
# (addu v0,a1,v0 / andi a1,v0,0xff / lw a0,0(s4) / andi a1,a1,0xff) is unique
# in the SLUS, so we locate it by signature and patch its last word.
DAMAGE_DEALT_SIGNATURE = bytes.fromhex("2110a200ff0045300000848eff00a530")
DAMAGE_DEALT_ANDI_A1 = 0x30a500ff  # andi a1, a1, 0xff (redundant — we replace it)
DAMAGE_DEALT_SCALE_WORDS = {
    "0.25": 0x00052883,  # sra a1,a1,2  -> x0.25 damage dealt
    "0.50": 0x00052843,  # sra a1,a1,1  -> x0.5  damage dealt
    "2x":   0x00052840,  # sll a1,a1,1  -> x2    damage dealt
    "4x":   0x00052880,  # sll a1,a1,2  -> x4    damage dealt
}


def patch_damage_dealt(data: bytearray, mode: str | None) -> dict:
    """Scale the melee damage Maximo deals to enemies by a power-of-two factor.

    mode: 'quarter' | 'half' | 'normal' | 'double' | 'quad' (None/'normal' =
    no change). Mutates `data` in place. Returns a stats dict.

    Replaces the redundant `andi a1, a1, 0xff` just before
    DEDynamicObject::SetDamage in Player::SetAction with a power-of-two scale of
    the damage (register a1).
    """
    if not mode or mode == "normal":
        return {"applied": False, "mode": "normal"}
    word = DAMAGE_DEALT_SCALE_WORDS.get(mode)
    if word is None:
        return {"applied": False, "mode": mode, "reason": "unknown mode"}
    sig = bytes(data).find(DAMAGE_DEALT_SIGNATURE)
    if sig < 0:
        return {"found": False, "applied": False, "mode": mode}
    slot = sig + 12  # the redundant `andi a1, a1, 0xff` (last word of signature)
    if bytes(data[slot:slot + 4]) != struct.pack("<I", DAMAGE_DEALT_ANDI_A1):
        return {"found": True, "applied": False, "mode": mode,
                "reason": "expected 'andi a1,a1,0xff' not found at target"}
    struct.pack_into("<I", data, slot, word)
    return {"found": True, "applied": True, "mode": mode, "slot": slot}


# ============================================================================
# Shield point COST per shield block
# ============================================================================
# The editor is exposed to users as FOUR NAMED PRESETS plus vanilla, not as a
# raw integer (see SHIELD_COST_PRESETS below):
#   none 0 | vanilla 1 (default, no patch) | light 2 | medium 3 | heavy 5
# The presets are a UI/CLI concern only — labels are resolved to an int at the
# two edges (argparse and the GUI combobox) via resolve_shield_cost(), and the
# wire format below this line stays an integer everywhere.
#
# Shield points are GameVar #10 ("shield_blocks" in the HUD), and they are NOT
# spent through GameVarSub. Confirmed two independent ways in MAXDEMOR.ELF:
#   * HUD route: InterfaceUpdate__10DarkEngine @ 0x00150D2C does
#     `lw $v1, 30064($at)` with $at = 0x0028 -> absolute 0x00287570 =
#     gPlayer(0x00287500) + 0x70 = 0x48 + 4*10  ->  index 10.
#   * GameVarTbl route: entry #10 = `0a 00 14 00 01 01`
#     (initial 10, cap 20, locked, persistent).
#   * Cross-check: GameVar #14 (initial 2, cap 4) is the HUD bar's SEGMENT count
#     and #10 == 5 * #14 exactly, matching the HUD's divide-by-5 and the 5-entry
#     ShieldBlock icon table at 0x001EF040.
#
# NO GameVarSub call site in this build carries index 10. Every GameVarSub uses
# index 2 (health), 5 (gold) or 11 (sword_units). The ONLY accessor call in the
# whole build that touches index 10 is GameVarAdd(this, 10, 5) in SetAbility
# @ 0x0013462C ("if Maximo has a shield, grant 5 blocks"). Shield points are
# spent by an inline read-modify-write on the raw GameVar word at gPlayer+0x70:
#
#   OnAttack__6Player
#     0013E060: 00000000  nop                   # part of the signature
#     0013E064: 8E620070  lw    $v0, 112($s3)   # 112 = 0x70 = GameVar #10
#     0013E068: 2442FFFF  addiu $v0, $v0, -1    # <-- PATCH TARGET
#     0013E06C: 1C4000CB  bgtz  $v0, 0x0013E39C # offset differs per copy: EXCLUDED
#     0013E070: AE620070  sw    $v0, 112($s3)   # store, in the DELAY SLOT
#     0013E074: 0C04ED58  jal   0x0013B560      # BreakShield, absolute: EXCLUDED
#
# The signatures below therefore stop at the `addiu` (nop / lw / addiu, 12 bytes,
# slot_delta 8) and never reach past the unusable bgtz/jal. The bare word
# 0x2442FFFF on its own appears 123 times 4-aligned in each retail executable —
# the nop+lw prefix is what narrows it to exactly 3 hits. Do NOT shorten it.
#
# Base registers differ: sites 1 and 2 use $s3, site 3 uses $s4. That register
# difference is the only thing separating the two signatures.
#
# Fixed-cost sites patched (demo vaddr / demo file offset / function):
#   0x0013E068  0x0003E0E8  OnAttack__6Player
#   0x0013E140  0x0003E1C0  OnAttack__6Player   (identical second copy)
#   0x0013E9A4  0x0003EA28  OnHit__6Player
#
# Measured slot offsets (all three hold `ff ff 42 24` in vanilla):
#   MAXDEMOR.ELF  0x03E0E8 / 0x03E1C0 / 0x03EA28   (on_attack 2, on_hit 1)
#   SLUS_200.17   0x05DC28 / 0x05DD00 / 0x05E548   (on_attack 2, on_hit 1)
#   SLPM_621.27   0x05DDE8 / 0x05DEC0 / 0x05E708   (on_attack 2, on_hit 1)
# So the patch covers BOTH US and JP retail; the JP counts are MEASURED, not
# inferred. Absorb signature in SLUS_200.17 sits at 0x05E3D4.
#
# OUT OF SCOPE, deliberately: the variable-amount absorption site in
# OnHit__6Player @ demo 0x0013E834 (lw v0,0x70(s4) / subu v0,v0,s2 /
# sw v0,0x70(s4) / lw v0,0x70(s4)). It subtracts a REGISTER, so there is no
# immediate to edit; scaling it would need a power-of-two shift, reintroducing
# for that one path exactly the limitation this editor exists to avoid, and it
# would put two different game mechanisms behind one user-facing option.
# SHIELD_ABSORB_SIGNATURE exists only as a guard so no write can land inside it.
#
# Cost 0 semantics — read this before "fixing" it: `addiu $v0,$v0,0` leaves the
# loaded count unchanged, `bgtz` tests that UNCHANGED value BEFORE the store, and
# the `sw` sits in the branch DELAY SLOT. With a positive shield count the branch
# is TAKEN, so the delay-slot store is SKIPPED and the count is never written
# back. Cost 0 is a non-depleting shield by STORE ELISION, not by storing an
# unchanged value. Do NOT "fix" the delay slot, and do not replace the addiu with
# a nop — the delay-slot placement is load-bearing. (With the count already at 0
# the branch is not taken, the store writes 0 and BreakShield runs, same as
# vanilla: cost 0 makes the shield non-depleting, it does not revive a broken
# shield.)
#
# GameVarTbl (17 x 6 = 102 bytes) is byte-identical across MAXDEMOR.ELF 0xED3C0,
# SLUS_200.17 0xD4EC0 and SLPM_621.27 0xD77A0, with entry #10 = `0a 00 14 00
# 01 01` in all three. That says NOTHING about code coverage — it is a data
# table and the three executables are separate builds. JP code coverage was
# measured separately and is the site count recorded above.
# ============================================================================

GAMEVAR_SHIELDUNITS_INDEX = 10          # "shield_blocks"; gPlayer + 0x48 + 4*10
SHIELD_UNITS_PLAYER_OFFSET = 0x70       # byte displacement used by the lw/sw pair

SHIELD_COST_ADDIU_BASE = 0x24420000     # addiu $v0, $v0, <imm>   (rs = rt = $v0)
SHIELD_COST_EXPECTED_WORD = 0x2442FFFF  # addiu $v0, $v0, -1      (vanilla)
# Validation ceiling for the patcher itself. The user-facing editor exposes only
# the named presets below, which are a SUBSET of 0..SHIELD_COST_MAX; MAX stays
# wider because it is the encoding's own sane limit (GameVar #10 has cap 20 and
# initial 10, so a cost of 10 already means "one block per full bar") and the
# patcher is a general API that must keep accepting any in-range integer.
SHIELD_COST_MAX = 10
SHIELD_COST_VANILLA = 1
SHIELD_COST_EXPECTED_SITES = 3          # measured in demo, SLUS and SLPM

# The editor exposes NAMED PRESETS, not a raw 0-10 integer. Insertion order is
# ascending cost on purpose: the CLI/GUI list these in this order, and sorting
# alphabetically would read "heavy, light, medium, none", which is nonsense.
# There is deliberately no cost 4 and nothing above 5.
#   none    0  shield use never depletes the bar (store elision, see above)
#   vanilla 1  the game's own cost — no patch is applied
#   light   2
#   medium  3
#   heavy   5
SHIELD_COST_PRESETS = {
    "none": 0,
    "vanilla": SHIELD_COST_VANILLA,
    "light": 2,
    "medium": 3,
    "heavy": 5,
}


def resolve_shield_cost(value) -> int | None:
    """Preset label (or int, or None) -> the integer cost patch_shield_cost wants.

    Single source of truth for both edges (the two argparse layers and the GUI
    combobox) so the label -> value mapping is never duplicated. Returns None for
    None, "vanilla", blank, and anything unrecognised — i.e. "leave the
    executable's own cost alone". Labels are case-insensitive.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    label = str(value).strip().lower()
    if not label or label == "vanilla":
        return None
    return SHIELD_COST_PRESETS.get(label)

# One entry per distinct code shape: (name, signature, slot_delta, occurrences).
# `occurrences` is that signature's measured match count — the two OnAttack
# copies are byte-identical over the first three words, so one signature
# legitimately matches TWICE. Each signature is 12 bytes with the target `addiu`
# as its last word, hence slot_delta = 8.
SHIELD_COST_SITES = (
    # nop / lw $v0,0x70($s3) / addiu $v0,$v0,-1   -- OnAttack, two copies
    ("on_attack", bytes.fromhex("000000007000628effff4224"), 8, 2),
    # nop / lw $v0,0x70($s4) / addiu $v0,$v0,-1   -- OnHit
    ("on_hit",    bytes.fromhex("000000007000828effff4224"), 8, 1),
)

# lw $v0,0x70($s4) / subu $v0,$v0,$s2 / sw $v0,0x70($s4) / lw $v0,0x70($s4)
# Guard only — the patcher never writes inside this block's byte range.
SHIELD_ABSORB_SIGNATURE = bytes.fromhex("7000828e23105200700082ae7000828e")


def _find_all(buf: bytes, needle: bytes) -> list[int]:
    """Every offset at which `needle` occurs in `buf` (bytes.find only ever
    returns the first, and one shield-cost signature matches twice)."""
    out: list[int] = []
    if not needle:
        return out
    start = 0
    while True:
        i = buf.find(needle, start)
        if i < 0:
            return out
        out.append(i)
        start = i + 1


def patch_shield_cost(data: bytearray, cost: int | None) -> dict:
    """Set how many shield points one shield block spends.

    cost: 0..SHIELD_COST_MAX, where 0 makes shield use non-depleting.
    None or SHIELD_COST_VANILLA (1) = no change. Mutates `data` in place.
    Returns a stats dict.

    Rewrites the 16-bit immediate of the `addiu $v0, $v0, -1` that decrements
    the raw GameVar #10 word at gPlayer+0x70, at every fixed-cost site. Shield
    points do NOT go through GameVarSub; see the section comment above. The
    immediate is negative, so the word is
        SHIELD_COST_ADDIU_BASE | ((-cost) & 0xFFFF)
    Unlike the damage editors this is an exact integer cost, not a power-of-two
    scale: the vanilla value is 1, so a shift-based multiplier could only ever
    produce 0, 2 or 4.

    The variable-amount absorption site in OnHit (subu $v0,$v0,$s2) is left
    alone by design and keeps vanilla behaviour.
    """
    # 1) Argument validation first, so an invalid cost never searches the buffer.
    if cost is None:
        return {"applied": False, "cost": None}
    if cost == SHIELD_COST_VANILLA:
        return {"applied": False, "cost": cost, "reason": "cost 1 is vanilla"}
    if not isinstance(cost, int) or isinstance(cost, bool) \
            or cost < 0 or cost > SHIELD_COST_MAX:
        return {"applied": False, "cost": cost,
                "reason": f"cost must be 0..{SHIELD_COST_MAX}"}

    buf = bytes(data)

    # The out-of-scope absorption block: nothing may ever be written inside it.
    absorb_ranges = [(o, o + len(SHIELD_ABSORB_SIGNATURE))
                     for o in _find_all(buf, SHIELD_ABSORB_SIGNATURE)]

    def in_absorb(slot: int) -> bool:
        return any(lo <= slot < hi or lo < slot + 4 <= hi
                   for lo, hi in absorb_ranges)

    # 2) Locate EVERY occurrence of EVERY signature, deduped by slot offset.
    located: dict[int, str] = {}
    for name, sig, slot_delta, _occ in SHIELD_COST_SITES:
        for match in _find_all(buf, sig):
            slot = match + slot_delta
            if slot + 4 > len(data) or in_absorb(slot):
                continue
            located.setdefault(slot, name)

    expected = SHIELD_COST_EXPECTED_SITES
    if not located:
        return {"found": False, "applied": False, "cost": cost, "sites": 0,
                "expected_sites": expected,
                "reason": "no shield-cost site found"}

    # 3) Per-slot word verification, then 4) the writes.
    # Note: with the current signatures the target `addiu` is the signature's own
    # last word, so a slot whose word has changed also fails to MATCH and shows up
    # as a missing site rather than in `skipped`. The check is kept anyway: it is
    # the contract, it costs nothing, and it stays correct if a future signature
    # ever stops covering the target word (an 8-byte nop+lw prefix cannot be used
    # for that, because the $s4 prefix also matches the out-of-scope absorption
    # site).
    word = SHIELD_COST_ADDIU_BASE | ((-cost) & 0xFFFF)
    vanilla = struct.pack("<I", SHIELD_COST_EXPECTED_WORD)
    slots: list[int] = []
    skipped: list[dict] = []
    for slot in sorted(located):
        if bytes(data[slot:slot + 4]) != vanilla:
            skipped.append({
                "site": located[slot], "slot": slot,
                "reason": "expected 'addiu $v0,$v0,-1' not found at target",
            })
            continue
        struct.pack_into("<I", data, slot, word)
        slots.append(slot)

    n = len(slots)
    if n == 0:
        return {"found": True, "applied": False, "cost": cost, "sites": 0,
                "expected_sites": expected, "slots": [], "skipped": skipped,
                "reason": "no shield-cost site had the expected instruction"}

    res = {"found": True, "applied": True, "cost": cost, "sites": n,
           "expected_sites": expected, "slots": slots}
    if skipped:
        res["skipped"] = skipped
    if n != expected:
        res["reason"] = f"patched {n} of {expected} shield-cost sites"
    return res


# ============================================================================
# LIVES spent per death
# ============================================================================
# Exposed to users as FIVE NAMED PRESETS (see LIFE_COST_PRESETS below), in this
# order, vanilla first because it is the default:
#   vanilla    1  the game's own cost — no patch is applied at all
#   none       0  deaths no longer cost a life (effectively infinite lives)
#   double     2
#   triple     3
#   quadruple  4
# The presets are a deliberate SUBSET of what patch_life_cost accepts: the
# function still validates LIFE_COST_MIN..LIFE_COST_MAX (0..9) so it stays usable
# beyond the exposed set. Labels are resolved to an int at the two edges
# (argparse and the GUI combobox) via resolve_life_cost(); the wire format below
# stays an integer everywhere.
#
# Lives is GameVar #4, the raw word at gPlayer + 0x48 + 4*4 = gPlayer + 0x58.
# GameVarTbl entry #4 = `04 00 63 00 01 01` (initial 4, cap 99, locked,
# persistent).
#
# ---------------------------------------------------------------------------
# COST 0 ("none") semantics
# ---------------------------------------------------------------------------
# The cave's decrement becomes `addiu $v0, $v0, 0`, so the read / decrement /
# clamp / store sequence puts the life count back UNCHANGED. Lives never
# decrease: effectively infinite lives. The `bgtz $v0` that follows then sees the
# unchanged (positive) count and takes the branch, skipping the StopMusic
# last-life cue — which is the correct behaviour, it is not the last life.
#
# The mandatory game-over companion patch (`bne $v0,$zero` -> `bgtz $v0`) is
# STILL applied at cost 0. It is strictly unnecessary there — with a zero
# decrement the count can never step past zero — but it is harmless and keeps the
# patch atomic and uniform: at a life count of 0 both `bne $v0,$zero` and
# `bgtz $v0` fall through to game over identically. Cost 0 is deliberately NOT
# special-cased to skip it; one code path is worth more than saving a word.
#
# Cost 0 also does not break the "already at 0 lives" case (a save resumed with
# no lives left): with lives at 0, SetDeath's patched `bgtz $v0` does not branch,
# so game over still fires exactly as vanilla.
#
# NOTE for the help text and the UI: initial lives is 4, so a cost of 5 or more
# means the very FIRST death ends the run, and values above 4 only make sense
# while collecting 1-ups.
#
# Like shield points and unlike sword points, lives are NOT spent through an
# accessor. An exhaustive sweep found NO GameVarSub/GameVarAdd/GameVarSet call
# site anywhere in the build carrying index 4. The decrement is an inline
# read-modify-write with the store in a BRANCH DELAY SLOT (the shield-points
# pattern), and it lives inside GameVarSub__6PlayerFiiUc itself, on the index-2
# (health) "health just reached 0" path:
#
#   GameVarSub__6PlayerFiiUc   ($s3 = this)
#     00131D50: 8E620058  lw    $v0, 88($s3)    # 88 = 0x58 = GameVar #4 Lives
#                                               # <-- HOOK (becomes `j CLAMP`)
#     00131D54: 2442FFFF  addiu $v0, $v0, -1    # the hook's DELAY SLOT; left
#                                               # vanilla, its result discarded
#     00131D58: 1C400004  bgtz  $v0, ...        # branch offset: EXCLUDED from sig
#     00131D5C: AE620058  sw    $v0, 88($s3)    # store, in the DELAY SLOT — it
#                                               # ALWAYS happens
#
# This is the ONLY lives decrement in the binary. SetDeath is also reached from
# DieMove, DrownMove, LavaDeathMove and FrozenMove, but none of those decrement
# lives, so rewriting this one immediate changes every death that costs a life
# today, UNIFORMLY. There is no partial-patch risk. Note the write bypasses both
# GameVarSub's zero-clamp and the 99 cap: it is a raw word store.
#
# ---------------------------------------------------------------------------
# PATCH 2 — the game-over test — IS MANDATORY whenever cost > 1, and is applied
# uniformly at cost 0 too (see the cost-0 notes above).
# ---------------------------------------------------------------------------
# Game-over is detected with an EXACT equality test in Player::SetDeath:
#
#   Player::SetDeath           ($s0 = this)
#     0013F6CC: 8E020058  lw  $v0, 88($s0)      # lives
#     0013F6D0: 1440000D  bne $v0, $zero, +0xD  # != 0 -> normal respawn
#                                               # fall through (== 0) -> game over
#
# An UNCLAMPED decrement could step PAST zero, and this test is an exact
# `== 0`: a cost of 2 with an odd life count would go 1 -> -1, the branch would
# see non-zero, and the respawn path would run forever (infinite lives). Lives is
# treated as a signed word throughout (addiu / bgtz / bne).
# Fix: turn `bne $v0,$zero` into `bgtz $v0` (identical branch distance), i.e.
# "game over when lives <= 0". With the clamp below the value here can only ever
# be 0, so the two tests now agree — `bgtz` is still applied, because "game over
# at <= 0" is the form that stays correct no matter what reaches it.
#
# ATOMICITY: the writes are ALL-OR-NOTHING. Applying the cost patch without the
# game-over patch produces silent infinite lives — strictly WORSE than not
# applying the option at all. So patch_life_cost plans the clamp and verifies the
# game-over slot before writing EITHER.
#
# ---------------------------------------------------------------------------
# LIVES CAN NEVER GO NEGATIVE — the ZERO CLAMP (code cave)
# ---------------------------------------------------------------------------
# The decrement is CLAMPED at zero: `lives = max(0, lives - cost)`, so a negative
# value is never stored and every downstream reader only ever sees a count
# vanilla already handles. This is not defence in depth — it fixes a real CRASH
# ON DEATH (quadruple cost with 3 lives left: 3 - 4 = -1; perma death with 0
# lives: 0 - 1 = -1).
#
# WHAT A NEGATIVE COUNT DID. Lives is HUD element "lives" — InterfaceData maps
# that name to var code 4 — and the HUD renders it as a NUMBER through
# DarkEngine::SetNumber, which sign-extends the value to 16 bits, splits it into
# digits with `div`, and looks each digit up in the 10-entry `Numbers` table:
#     sll $v1,$s2,2 ; addu $v0,$v0,$v1 ; lw $a1,0($v0)   # $s2 = the digit
# MIPS `div` truncates toward zero, so a negative count yields a NEGATIVE digit
# and the load reads `Numbers[-1]`. That word is 0x00000000 in every build, and
# the NULL is handed straight on as a node-name pointer — the crash. Verified in
# all three images (exactly ONE such lookup per image, exhaustive scan, so this
# is THE lives renderer):
#   demo MAXDEMOR.ELF  SetNumber 0x00150620, lookup 0x00150700..0x00150714,
#                      `Numbers` @ 0x001EEFA0 (symbol, 40 bytes), [-1] = 0
#   SLUS_200.17        lookup vaddr 0x0016DA64 (file 0x6DAE4),
#                      Numbers @ 0x001D61E0 ("g_num_0".."g_num_9"), [-1] = 0
#   SLPM_621.27        lookup vaddr 0x0016DB24 (file 0x6DBA4),
#                      Numbers @ 0x001D8AC0, [-1] = 0
# The other readers of gPlayer+0x58 are, exhaustively (demo symbol table):
# GameVarSub (the decrement itself), Player::SetDeath (the game-over test) and
# Player::ContinuePlayer (a raw restore to 4). None of them can fault on a
# negative value; the HUD digit lookup is the crash.
#
# WHY A CAVE. The patch slot is a single word and the very next instruction is
# the `bgtz`, so an inline multi-instruction clamp does not fit, and a `j` in the
# `addiu`'s place would put that `bgtz` in a branch delay slot. So the HOOK is
# the signature's FIRST word (the `lw`), which becomes `j CLAMP`:
#
#   HOOK+0: j CLAMP              # was lw $v0,0x58($s3)
#   HOOK+4: addiu $v0,$v0,-1     # untouched: the j's delay slot, result unused
#   HOOK+8: bgtz $v0, ...        # runs EXACTLY ONCE, on the clamped value
#   HOOK+C: sw $v0,0x58($s3)     # its delay slot: stores the clamped value
#
#   CLAMP:  lw    $v0,0x58($s3)  # re-read (the delay-slot addiu is discarded)
#           addiu $v0,$v0,-cost  # the cost lives HERE now
#           bgez  $v0,+2
#           nop
#           addiu $v0,$zero,0    # only reached when the result went negative
#           j     HOOK+8
#           nop                  # NOT a reload: $v0 must survive to the bgtz
#
# Seven words / 28 bytes, in the same string-padding caves the starting-skills
# and level-timer features use (`_find_string_padding_cave`), allocated from a run
# HEAD just past the starting-skills block; the level timer's tail allocator keeps
# that combined head reserve free, so no two caves can overlap. No register other
# than $v0 is touched — not even $at — because the hook sits mid-GameVarSub, and
# $v0 was dead there anyway (the displaced `lw` was defining it).
#
# BOTH FEATURES SHARE ONE IMPLEMENTATION. perma death needs the clamp for the
# same reason (0 lives, vanilla cost 1) and calls the SAME `_life_clamp_plan`,
# with `cost=None` so it installs the clamp without disturbing a life cost the
# user also asked for. Either call order works.
#
# IDEMPOTENT: the hook's patched shape (a `j` plus the untouched vanilla `addiu`)
# is recognised on a re-run, the cave is re-derived from the `j` target and
# confirmed word-for-word, and at most the ONE cost word is rewritten.
#
# Measured offsets and occurrence counts (exhaustive 1-byte-step scan):
#   cost signature      SLUS_200.17  sig 0x51E20  hook 0x51E20   1 occurrence
#                       SLPM_621.27  sig 0x52040  hook 0x52040   1 occurrence
#   (the hook IS the signature's first word — the cost immediate itself now lives
#    in the clamp cave, not in the image's own `addiu`)
#   game-over signature SLUS_200.17              slot 0x5F270    1 occurrence
#                       SLPM_621.27              slot 0x5F430    1 occurrence
# Both US and JP retail are covered, measured, not inferred.
#
# Sites NEVER to touch:
#   * ContinuePlayer__6PlayerFv hard-restores lives to 4 (`addiu $v0,$zero,4`
#     plus `sw $v0,0x58($a0)`). The two are non-adjacent, so it cannot collide
#     with the cost signature.
#   * GameVarTbl entry #4 is the INITIAL-lives value, already covered by the
#     separate starting-inventory patch — not this feature.
#   * There are no GameVarAdd(4,...) or GameVarSet(4,...) sites at all: 1-up
#     grants come from script via CScriptApi::GameVarAdd, so they are unreachable
#     from a signature patch and keep their normal behaviour.
# ============================================================================

# lw $v0,0x58($s3) / addiu $v0,$v0,-1   (the bgtz that follows is excluded: its
# branch offset is not signature-safe)
LIFE_COST_SIGNATURE = bytes.fromhex("5800628effff4224")
# Kept for reference: the `addiu` the cost USED to be written into. Nothing
# writes it any more — the hook is the signature's first word and the cost
# immediate lives in the clamp cave (see LIFE_CLAMP_* below).
LIFE_COST_SLOT_DELTA = 4
LIFE_COST_EXPECTED_WORD = 0x2442FFFF    # addiu $v0, $v0, -1      (vanilla)
LIFE_COST_ADDIU_BASE = 0x24420000       # addiu $v0, $v0, <imm>   (rs = rt = $v0)
LIFE_COST_VANILLA = 1                   # default: no patch is applied at all
# Validation FLOOR. 0 is a legitimate cost ("none"): see the cost-0 notes above.
# Negative costs stay rejected — they would ADD lives on death.
LIFE_COST_MIN = 0
# The immediate is a signed 16-bit field, so much larger values encode fine; the
# limit here is GAMEPLAY, not encoding. Initial lives is 4. The exposed presets
# stop at 4, but the ceiling stays 9 so the function remains usable beyond them.
LIFE_COST_MAX = 9

# Insertion-ordered so every listing (argparse choices, GUI combobox, help text)
# follows the order the user asked for: vanilla first as the default, then none,
# double, triple, quadruple. Alphabetical order would be nonsense here.
LIFE_COST_PRESETS = {
    "vanilla": LIFE_COST_VANILLA,
    "none": 0,
    "double": 2,
    "triple": 3,
    "quadruple": 4,
}


def resolve_life_cost(value) -> int | None:
    """Preset label (or int, or None) -> the integer cost patch_life_cost wants.

    Single source of truth for both edges (the two argparse layers and the GUI
    combobox) so the label -> value mapping is never duplicated. Returns None for
    None, "vanilla", blank, and anything unrecognised — i.e. "leave the
    executable's own cost alone". Labels are case-insensitive.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    label = str(value).strip().lower()
    if not label or label == "vanilla":
        return None
    return LIFE_COST_PRESETS.get(label)

# lw $v0,0x58($s0) / bne $v0,$zero,+0xD
LIFE_GAMEOVER_SIGNATURE = bytes.fromhex("5800028e0d004014")
LIFE_GAMEOVER_SLOT_DELTA = 4
LIFE_GAMEOVER_EXPECTED_WORD = 0x1440000D   # bne  $v0, $zero, +0xD  (vanilla)
LIFE_GAMEOVER_PATCHED_WORD = 0x1C40000D    # bgtz $v0, +0xD         (same distance)

# ---------------------------------------------------------------------------
# THE ZERO CLAMP — one implementation, shared by life cost AND perma death
# ---------------------------------------------------------------------------
# See the "LIVES CAN NEVER GO NEGATIVE" part of the banner above for why this
# exists and how the cave is laid out. The hook is the `lw $v0,0x58($s3)` at
# LIFE_COST_SIGNATURE + LIFE_CLAMP_HOOK_DELTA (i.e. the signature's FIRST word,
# not the `addiu`): a single-word `j` there is the only redirect that does not
# put the following `bgtz` into a branch delay slot.
LIFE_CLAMP_HOOK_DELTA = 0                # the `lw $v0,0x58($s3)`
LIFE_CLAMP_LW_WORD = 0x8E620058          # lw    $v0,0x58($s3)   (the hook word)
LIFE_CLAMP_BGEZ_SKIP = 0x04410002        # bgez  $v0,+2  (skip the zeroing)
LIFE_CLAMP_ZERO_V0 = 0x24020000          # addiu $v0,$zero,0
LIFE_CLAMP_CAVE_WORDS = 7                # 28 bytes
LIFE_CLAMP_COST_INDEX = 1                # cave word holding `addiu $v0,$v0,-N`
# What the hook site looks like AFTER the clamp is installed: a `j` (target
# masked out — it depends on where the cave landed) followed by the UNTOUCHED
# vanilla `addiu $v0,$v0,-1`, which is now the `j`'s delay slot and whose result
# the cave discards. This is what makes a re-run find the site again.
LIFE_COST_PATCHED_PATTERN = (
    (0x08000000, 0xFC000000),                 # j <cave>
    (LIFE_COST_EXPECTED_WORD, 0xFFFFFFFF),    # addiu $v0,$v0,-1  (delay slot)
)


def _life_clamp_words(cost: int, hook_vaddr: int) -> list[int]:
    """The clamp cave: `$v0 = max(0, lives - cost)`, then back to HOOK+8.

    Uses NO register other than $v0 (which the displaced `lw` was defining
    anyway, so it is dead on entry) — deliberately, because the hook sits in the
    middle of GameVarSub and even $at is not worth assuming free.
    The return `j`'s delay slot is a `nop`, so $v0 still holds the clamped value
    when the `bgtz` at HOOK+8 tests it and its own delay-slot `sw` stores it.
    """
    return [
        LIFE_CLAMP_LW_WORD,                             # 1 lw    $v0,0x58($s3)
        LIFE_COST_ADDIU_BASE | ((-cost) & 0xFFFF),      # 2 addiu $v0,$v0,-cost
        LIFE_CLAMP_BGEZ_SKIP,                           # 3 bgez  $v0,+2
        0x00000000,                                     # 4 nop  (delay)
        LIFE_CLAMP_ZERO_V0,                             # 5 addiu $v0,$zero,0
        0x08000000 | (((hook_vaddr + 8) >> 2)
                      & 0x03FFFFFF),                    # 6 j     HOOK+8
        0x00000000,                                     # 7 nop  (delay)
    ]


def _life_clamp_plan(data: bytearray, cost: int | None = None):
    """Plan the clamp. Returns a plan dict, or a `str` naming the failure.

    NOTHING is written here — the caller applies the plan with
    `_life_clamp_apply` only once every OTHER piece of its own feature has
    verified too, which is what keeps both features all-or-nothing.

    `cost` is the number of lives a death spends. None means "whatever is in
    place already" — a fresh install then encodes the vanilla cost of 1 and an
    existing clamp is left with the cost it already carries. That is what lets
    perma death install the clamp without stomping a life-cost value, and lets
    life cost re-point an existing clamp, in either call order.

    IDEMPOTENCE: if the hook already holds a `j` into a cave whose seven words
    are exactly the ones this function writes, the plan REUSES that cave — at
    most the one cost word is rewritten, and with `cost=None` the plan is empty.
    That installed-form check runs BEFORE the fresh-install search, because at
    cost 1 the cave's own first two words are byte-identical to the vanilla
    signature (see the comment on the check itself).
    """
    if data[:4] != b"\x7fELF":
        return "not ELF"
    seg = _first_load_segment(bytes(data))
    if seg is None:
        return "no LOAD segment"
    load_vaddr, load_off, filesz = seg
    buf = bytes(data)
    need = LIFE_CLAMP_CAVE_WORDS * 4

    # --- ALREADY INSTALLED, CHECKED FIRST: find the hook by its patched shape
    # and re-derive the cave from the `j` target. Every candidate is CONFIRMED by
    # checking the cave really is our seven words, so a stray `j`+`addiu` pair
    # elsewhere in the image cannot be mistaken for the hook.
    #
    # ORDER MATTERS — do not move this after the fresh-install search. The cave's
    # first two words are `lw $v0,0x58($s3)` + `addiu $v0,$v0,-1`, which at cost 1
    # (what perma death installs) ARE the vanilla LIFE_COST_SIGNATURE byte for
    # byte. The real site no longer matches once hooked (it holds a `j`), so a
    # fresh-install search on an installed image finds THE CAVE ITSELF and would
    # hook the cave's own `lw`, corrupting it. Recognising the installed form
    # first makes that unreachable.
    for off in _find_all_masked(buf, LIFE_COST_PATCHED_PATTERN):
        hook_vaddr = load_vaddr + (off - load_off)
        j_word = struct.unpack_from("<I", buf, off)[0]
        cave_vaddr = (hook_vaddr & 0xF0000000) | ((j_word & 0x03FFFFFF) << 2)
        cave_off = load_off + (cave_vaddr - load_vaddr)
        if cave_off < load_off or cave_off + need > min(load_off + filesz,
                                                       len(buf)):
            continue
        cur = list(struct.unpack_from("<%dI" % LIFE_CLAMP_CAVE_WORDS, buf,
                                      cave_off))
        expect = _life_clamp_words(LIFE_COST_VANILLA, hook_vaddr)
        if any(cur[i] != expect[i] for i in range(LIFE_CLAMP_CAVE_WORDS)
               if i != LIFE_CLAMP_COST_INDEX):
            continue
        if cur[LIFE_CLAMP_COST_INDEX] & 0xFFFF0000 != LIFE_COST_ADDIU_BASE:
            continue
        current_cost = -_lt_sext16(cur[LIFE_CLAMP_COST_INDEX] & 0xFFFF)
        writes = []
        if cost is not None and cost != current_cost:
            writes.append((cave_off + LIFE_CLAMP_COST_INDEX * 4,
                           LIFE_COST_ADDIU_BASE | ((-cost) & 0xFFFF)))
        return {"reused": True,
                "cost": current_cost if cost is None else cost,
                "hook_off": off, "hook_vaddr": hook_vaddr,
                "cave_off": cave_off, "cave_vaddr": cave_vaddr,
                "cave_bytes": need, "writes": writes}

    # --- fresh install: the vanilla lw/addiu pair is still there ---
    matches = _find_all(buf, LIFE_COST_SIGNATURE)
    if matches:
        hook_off = matches[0] + LIFE_CLAMP_HOOK_DELTA
        if hook_off + 8 > len(data):
            return "lives-decrement site past end of buffer"
        if struct.unpack_from("<I", buf, hook_off)[0] != LIFE_CLAMP_LW_WORD:
            return "expected 'lw $v0,0x58($s3)' at the lives-decrement hook"
        hook_vaddr = load_vaddr + (hook_off - load_off)
        # The cave is allocated from the HEAD of a string-padding run, past the
        # block `patch_starting_skills` first-fits there, so the two can never
        # overlap; `patch_level_timer` keeps the same combined head reserve free
        # when it allocates from the TAIL of a run.
        reserve = STARTING_SKILLS_CAVE_MAX_WORDS * 4
        head = _find_string_padding_cave(buf, load_vaddr, load_off, filesz,
                                         reserve + need)
        if head is None:
            return "no code cave found for the lives clamp"
        cave_off = head + reserve
        if cave_off + need > len(data):
            return "lives-clamp cave past end of buffer"
        if any(buf[cave_off:cave_off + need]):
            return "lives-clamp cave window is not clear"
        cave_vaddr = load_vaddr + (cave_off - load_off)
        words = _life_clamp_words(
            LIFE_COST_VANILLA if cost is None else cost, hook_vaddr)
        writes = [(cave_off + i * 4, w) for i, w in enumerate(words)]
        writes.append((hook_off,
                       0x08000000 | ((cave_vaddr >> 2) & 0x03FFFFFF)))
        return {"reused": False, "cost": LIFE_COST_VANILLA if cost is None
                else cost, "hook_off": hook_off, "hook_vaddr": hook_vaddr,
                "cave_off": cave_off, "cave_vaddr": cave_vaddr,
                "cave_bytes": need, "writes": writes}

    return "lives-decrement site not found"


def _life_clamp_apply(data: bytearray, plan: dict) -> None:
    """Perform a plan built by `_life_clamp_plan`. Never fails: every offset in
    the plan was bounds-checked and every window verified when it was built."""
    for off, word in plan["writes"]:
        struct.pack_into("<I", data, off, word)


def patch_life_cost(data: bytearray, cost: int | None) -> dict:
    """Set how many lives one death spends.

    cost: LIFE_COST_MIN..LIFE_COST_MAX (0..9); the exposed presets are a subset.
    Cost 0 means deaths never spend a life. None or LIFE_COST_VANILLA (1) = no
    change, and in
    that case the executable is left byte-identical by this feature. Mutates
    `data` in place. Returns a stats dict.

    Two pieces, and they are ATOMIC — see the section comment above. The first
    redirects the inline decrement of the raw GameVar #4 word at gPlayer+0x58
    into the shared CLAMP CAVE, which computes `lives = max(0, lives - cost)`
    so a negative count can never be stored (`_life_clamp_plan`). The second
    turns SetDeath's exact `bne $v0,$zero` game-over test into `bgtz $v0`, which
    is MANDATORY: without it a cost that steps past zero gives infinite lives.
    If either site is missing, or the cave cannot be placed, or the game-over
    slot does not hold either its vanilla or its already-patched word, NOTHING
    is written.
    """
    # 1) Argument validation first, so an invalid cost never searches the buffer.
    if cost is None:
        return {"applied": False, "cost": None, "sites": 0, "slots": []}
    if cost == LIFE_COST_VANILLA:
        return {"applied": False, "cost": cost, "sites": 0, "slots": [],
                "reason": "cost 1 is vanilla"}
    if not isinstance(cost, int) or isinstance(cost, bool) \
            or cost < LIFE_COST_MIN or cost > LIFE_COST_MAX:
        return {"applied": False, "cost": cost, "sites": 0, "slots": [],
                "reason": f"cost must be {LIFE_COST_MIN}..{LIFE_COST_MAX}"}

    buf = bytes(data)

    # 2) PHASE ONE — plan the clamp and locate the game-over test. Nothing is
    # written in this phase.
    plan = _life_clamp_plan(data, cost)
    if isinstance(plan, str):
        return {"found": False, "applied": False, "cost": cost, "sites": 0,
                "slots": [], "reason": plan}

    over_matches = _find_all(buf, LIFE_GAMEOVER_SIGNATURE) \
        or _find_all(buf, PERMA_GAMEOVER_PATCHED_SIGNATURE)
    if not over_matches:
        return {"found": False, "applied": False, "cost": cost, "sites": 0,
                "slots": [], "reason": "lives game-over test site not found"}
    over_slot = over_matches[0] + LIFE_GAMEOVER_SLOT_DELTA
    if over_slot + 4 > len(data):
        return {"found": True, "applied": False, "cost": cost, "sites": 0,
                "slots": [], "reason": "target slot past end of buffer"}

    # 3) Verify the game-over word BEFORE writing anything. The cost patch
    # without the game-over patch is strictly worse than no patch at all (silent
    # infinite lives), so this is all-or-nothing. Either the vanilla word or the
    # word we write is accepted — perma death patches the SAME word, so whichever
    # feature runs second must not report a bogus failure.
    over_word = struct.unpack_from("<I", data, over_slot)[0]
    if over_word not in (LIFE_GAMEOVER_EXPECTED_WORD,
                         LIFE_GAMEOVER_PATCHED_WORD):
        return {"found": True, "applied": False, "cost": cost, "sites": 0,
                "slots": [],
                "reason": f"expected 'bne $v0,$zero' at 0x{over_slot:X} in the "
                          f"game-over test, found 0x{over_word:08X}"}

    # 4) PHASE TWO — everything verified, so every write is safe to perform.
    _life_clamp_apply(data, plan)
    struct.pack_into("<I", data, over_slot, LIFE_GAMEOVER_PATCHED_WORD)
    return {"found": True, "applied": True, "cost": cost, "sites": 2,
            "slots": [plan["hook_off"], over_slot], "clamp": plan}


# ============================================================================
# PERMA DEATH
# ============================================================================
# One option, five coordinated pieces (eight writes): Maximo starts with 0 lives and 0 death
# coins, can never gain either, and the FIRST death ends the run permanently
# with no continue offered.
#
# HOW IT WORKS (all five pieces are load-bearing)
# ---------------------------------------------------------------------------
# 1) + 2) GameVarTbl entries #4 (Lives) and #8 (Death coins): both the INITIAL
#    field and the CAP field are set to 0.
#
#    The INITIAL field is what Player::InitVars copies into the player's 17-word
#    GameVar array on every new run — it is the ONLY place the array is seeded
#    from the table, so initial 0 gives 0 lives / 0 coins at the start.
#
#    The CAP field is what blocks every GAIN. Player::GameVarAdd clamps its
#    result to the per-var cap UNCONDITIONALLY, in the default arm that indices 4
#    and 8 both fall into (retail SLUS: `lh v0,2(s4)` @ 0x0015170C, `slt`, clamp
#    @ 0x00151720). All three CScriptApi::GameVar* wrappers call the Player
#    accessors on the static gPlayer, so SCRIPT-driven 1-up and death-coin grants
#    route through the same clamped GameVarAdd. Cap 0 therefore kills native and
#    script grants alike: a 1-up adds nothing.
#
#    persist stays 1 on both entries so saves store the zeroes. Entry #8's
#    `locked` flag is flipped 0 -> 1 as a cheap extra speed bump only — see the
#    caveat below, the CAP is what actually does the work.
#
# 3) Player::SetDeath's game-over test, `bne $v0,$zero` -> `bgtz $v0`. This is
#    MANDATORY, not defence-in-depth. With lives at 0 and the vanilla cost of 1
#    the inline decrement produces -1; vanilla `bne $v0,$zero` sees non-zero and
#    takes the respawn arm (mglNextLife) forever, so perma-death would silently
#    become INFINITE lives. With `bgtz $v0`, -1 falls through to
#    mglRestartLevel + CleanUpAfterGameOver = game over. This is the SAME word
#    patch_life_cost already patches, so the existing LIFE_GAMEOVER_* constants
#    are reused rather than duplicated.
#
# 3b) The lives ZERO CLAMP on the inline decrement, installed through the SAME
#    `_life_clamp_plan` the life-cost feature uses (see the LIVES banner above).
#    MANDATORY here: lives start at 0, so the first death's vanilla cost of 1
#    would store -1, and a negative count crashes the HUD's digit lookup
#    (`Numbers[-1]` is NULL). The clamp is installed with `cost=None`, i.e. the
#    vanilla cost of 1 on a fresh image and an untouched cost if the life-cost
#    option already installed the cave.
#
# 4) Player::ContinuePlayer's `addiu $v0,$zero,4` life restore -> 0. Recommended
#    defence-in-depth: it is the only other raw write to the life counter, and it
#    caps the residual script leak described below.
#
# WHY NO CONTINUE IS OFFERED
# ---------------------------------------------------------------------------
# The continue offer is gated on DEATH COINS, not lives. Retail SLUS 0x00149FF8:
# `slt at,v0,v1` with $v0 = death coins (*0x0025CEB8) and $v1 = the required cost
# from the table at 0x001CF840 = `01 01 02 02 03 03 04 04 05` — a MINIMUM cost of
# 1. With coins pinned at 0 the offer never appears, and ContinuePlayer (whose
# only caller is that flow) is unreachable.
#
# KNOWN RESIDUAL LEAK — documented, deliberately NOT fixed
# ---------------------------------------------------------------------------
# Player::GameVarSet has NO cap clamp and is reachable from level script through
# CScriptApi::GameVarSet with a SCRIPT-SUPPLIED index. A script doing
# GameVarSet(4, n) or GameVarSet(8, n) bypasses the cap. This could not be ruled
# out because level scripts live in the .PRS/.PSX data, not in the executable.
# The ContinuePlayer patch limits the damage to "a continue that restores 0
# lives", so the next death still ends the run. Closing the hole fully would mean
# adding a cap clamp INSIDE GameVarSet — an instruction-level edit needing free
# space — which is explicitly out of scope here.
#
# The `locked` flag is NOT reliable protection either: Player flag bit 0x2 at
# this+0x24 overrides it, and that bit is set by InitVars, SetDeath and
# ContinuePlayer. Again: the CAP is what does the work.
#
# 5) SAVE LOADS — Player::GameVarLoad restores all 17 GameVars as RAW WORDS
#    straight out of the save buffer, with no cap and no lock check. The
#    GameVarTbl cap only constrains Player::GameVarAdd, so it cannot help here:
#    loading a save made before perma-death was enabled used to hand back its old
#    lives and death coins. Fixed by appending two stores to the tail of
#    GameVarLoad:
#
#      vanilla                          patched
#      -------------------------------  -------------------------------------
#      jr    $ra                        nop                  <- drop the return
#      nop                              sw $zero,0x14($t1)   <- lives   = 0
#      (pad word, zero)                 jr $ra
#      (pad word, zero)                 sw $zero,0x24($t1)   <- coins = 0 (ds)
#      (pad word, zero)                 (pad word, zero, still spare)
#
#    WHY $t1 IS THE SAFE BASE. The var loop runs `addiu $t1,$t1,4` in a delay
#    slot 17 times starting from $t1 = this, so on exit $t1 = this + 0x44 and
#    nothing after the loop touches it. Lives is GameVar #4 at this+0x58 =
#    0x14($t1); death coins is #8 at this+0x68 = 0x24($t1). $a0 must NOT be used
#    — the trailing name-copy loop clobbers it.
#
#    WHY NO BRANCH IS NEEDED. GameVarLoad ends `jr $ra` + delay-slot `nop`,
#    followed by THREE spare zero words of inter-function padding before
#    GameVarSave. Turning the `jr` into a `nop` makes execution fall straight
#    through into that pad, so the two stores and the relocated `jr $ra` fit
#    in place with no jump and no relocation. The third pad word stays spare.
#
#    No follow-up bookkeeping: nothing in the function mirrors #4 or #8 (the
#    existing index-10 special case exists only because #10 mirrors a bit in
#    player+0x24). The `sb $v1,0x462($a0)` "vars refreshed" flag is written
#    BEFORE our stores and consumed after the call returns, so any refresh reads
#    the already-zeroed values; lives and coins are read live from the array, not
#    cached. There is exactly ONE caller (App::LoadGame) and one load path.
#
# ATOMICITY: all writes are ALL-OR-NOTHING. A half-applied perma-death (for
# example zeroed lives without the game-over fix) gives SILENT INFINITE LIVES,
# which is strictly worse than not applying the option at all. So everything is
# located and verified first, and only then written.
#
# Measured offsets (exhaustive scan, US and JP retail both covered):
#   GameVarTbl          SLUS_200.17  0xD4EC0 (entry #4 0xD4ED8, #8 0xD4EF0)
#                       SLPM_621.27  0xD77A0
#   game-over slot      SLUS_200.17  0x5F270        SLPM_621.27  0x5F430
#   ContinuePlayer slot SLUS_200.17  0x50AA8   1x   SLPM_621.27  0x50CC8   1x
#                       demo                   1x
#   GameVarLoad tail    SLUS_200.17  sig @ 0x051604 (slots 0x05161C..0x051628)
#   (byte-identical in  SLPM_621.27  sig @ 0x051824 (slots 0x05183C..0x051848)
#    demo + both retail; exactly 1 signature match in each)
# The table is located with the existing anchor-based find_gamevar_table(), never
# by a hardcoded offset.
# ============================================================================

# addiu v0,zero,4 / sll v1,v1,1 / sw v1,0x50(a0) / sw v0,0x58(a0) /
# lw v0,0x70(a0) / slti at,v0,10      -- Player::ContinuePlayer, no jal, no
# branches, so the whole run of words is signature-safe.
PERMA_CONTINUE_SIGNATURE = bytes.fromhex(
    "04000224" "40180300" "500083ac" "580082ac" "7000828c" "0a004128")
PERMA_CONTINUE_SLOT_DELTA = 0
PERMA_CONTINUE_EXPECTED_WORD = 0x24020004   # addiu $v0, $zero, 4   (vanilla)
PERMA_CONTINUE_PATCHED_WORD = 0x24020000    # addiu $v0, $zero, 0   (patched)
# The target word is the signature's OWN first word (slot delta 0), so once this
# feature has been applied the vanilla signature no longer matches. Keep the
# already-patched form around so a re-run still finds the site — see the
# idempotence note in patch_perma_death.
def _perma_patched_sig(sig: bytes, delta: int, word: int) -> bytes:
    """`sig` with the word at `delta` replaced by `word` — i.e. what the
    signature looks like AFTER this feature has written its slot. Both perma-death
    word signatures cover their own target word, so the vanilla signature stops
    matching once patched; this gives the re-run something to match on."""
    return sig[:delta] + struct.pack("<I", word) + sig[delta + 4:]


PERMA_CONTINUE_PATCHED_SIGNATURE = _perma_patched_sig(
    PERMA_CONTINUE_SIGNATURE, PERMA_CONTINUE_SLOT_DELTA,
    PERMA_CONTINUE_PATCHED_WORD)
PERMA_GAMEOVER_PATCHED_SIGNATURE = _perma_patched_sig(
    LIFE_GAMEOVER_SIGNATURE, LIFE_GAMEOVER_SLOT_DELTA,
    LIFE_GAMEOVER_PATCHED_WORD)

# GameVarTbl entry bytes: [0:2] int16 initial, [2:4] uint16 cap, [4] locked,
# [5] persist.  Vanilla -> target, per entry.
PERMA_LIVES_VANILLA_ENTRY = bytes.fromhex("040063000101")
PERMA_LIVES_PATCHED_ENTRY = bytes.fromhex("000000000101")
PERMA_DEATHCOINS_VANILLA_ENTRY = bytes.fromhex("010063000001")
PERMA_DEATHCOINS_PATCHED_ENTRY = bytes.fromhex("000000000101")

# Player::GameVarLoad tail — 7 words, no jal targets. The single `bne` offset is
# included and is stable because the branch is intra-loop:
#   slti v1,a3,31 / sb a1,0x424(a0) / sb zero,0x425(a0) / addiu a2,a2,1 /
#   bne v1,zero,-7 / addiu a0,a0,2 / jr ra
PERMA_VARLOAD_SIGNATURE = bytes.fromhex(
    "1f00e328" "240485a0" "250480a0" "0100c624" "f9ff6014" "02008424"
    "0800e003")
# (delta from signature start, expected vanilla word, patched word):
#   +24  jr $ra              -> nop                  (drop the return)
#   +28  pad                 -> sw $zero,0x14($t1)   (lives = 0)
#   +32  pad                 -> jr $ra
#   +36  pad                 -> sw $zero,0x24($t1)   (death coins = 0, ds)
# $t1 = this + 0x44 on loop exit and is untouched afterwards; $a0 is clobbered by
# the trailing name loop so it must not be used. See the banner above.
PERMA_VARLOAD_WRITES = (
    (24, 0x03E00008, 0x00000000),
    (28, 0x00000000, 0xAD200014),
    (32, 0x00000000, 0x03E00008),
    (36, 0x00000000, 0xAD200024),
)
# The signature's own last word is the `jr $ra` we turn into a `nop`, so the
# vanilla signature stops matching once patched. Keep the post-patch form for the
# re-run, same trick as the two word sites above.
PERMA_VARLOAD_PATCHED_SIGNATURE = _perma_patched_sig(
    PERMA_VARLOAD_SIGNATURE, PERMA_VARLOAD_WRITES[0][0],
    PERMA_VARLOAD_WRITES[0][2])


def _perma_find_gamevar_table(data: bytearray | bytes) -> int | None:
    """find_gamevar_table(), with a fallback for an ALREADY perma-death-patched
    buffer.

    find_gamevar_table verifies entry #4's cap == 99 as part of its sanity check,
    and this feature sets that cap to 0 — so on a second run the helper correctly
    reports "not the table I know". Rather than weaken the shared helper (other
    patches rely on that check), fall back to the SAME structural anchor and
    accept the buffer only when entries #4 and #8 already hold exactly the
    perma-death target bytes. Still anchor-based; no offset is hardcoded.
    """
    tbl = find_gamevar_table(data)
    if tbl is not None:
        return tbl
    m = _GAMEVAR_ANCHOR.search(bytes(data))
    if not m:
        return None
    tbl = m.start() - GAMEVAR_GOLD_INDEX * GAMEVAR_ENTRY_SIZE - 2
    if tbl < 0 or tbl + GAMEVAR_TABLE_LEN * GAMEVAR_ENTRY_SIZE > len(data):
        return None

    def entry(i: int) -> bytes:
        o = tbl + i * GAMEVAR_ENTRY_SIZE
        return bytes(data[o:o + GAMEVAR_ENTRY_SIZE])

    def cap(i: int) -> int:
        o = tbl + i * GAMEVAR_ENTRY_SIZE + 2
        return int.from_bytes(data[o:o + 2], "little")

    if (cap(5) == 9999 and cap(6) == 999 and cap(7) == 9
            and entry(GAMEVAR_LIVES_INDEX) == PERMA_LIVES_PATCHED_ENTRY
            and entry(GAMEVAR_DEATHCOINS_INDEX)
            == PERMA_DEATHCOINS_PATCHED_ENTRY):
        return tbl
    return None


def patch_perma_death(data: bytearray, enabled: bool) -> dict:
    """Enable PERMA DEATH: 0 lives, 0 death coins, neither can ever be gained,
    and the first death ends the run with no continue offered.

    `enabled` falsy = total no-op, the buffer is left byte-identical. Mutates
    `data` in place. Returns a stats dict with the usual `found` / `applied` /
    `reason` keys plus `writes` (one entry per region written) and `sites`.

    Eight coordinated writes, ATOMIC — see the section comment above. If any piece
    is missing or holds an unexpected value, NONE of them is applied and
    `applied` is False with a `reason` naming the failing piece.
    """
    if not enabled:
        return {"found": False, "applied": False, "enabled": False,
                "sites": 0, "writes": [], "reason": "not requested"}

    buf = bytes(data)

    # ---- PHASE ONE: locate and verify everything. Nothing is written here. ----
    #
    # IDEMPOTENCE. Every piece accepts EITHER the vanilla value OR the value this
    # function writes. Two reasons this is required, not merely nice:
    #   * patch_life_cost writes the SAME game-over word, so whichever of the two
    #     features runs second finds 0x1C40000D already in place. Treating that
    #     as a verification failure would make the two options mutually
    #     exclusive depending on call order.
    #   * Re-running the patcher over an already-patched executable must stay
    #     safe rather than reporting a bogus failure.
    # So "already holds the patched value" counts as SUCCESS for that piece.
    tbl = _perma_find_gamevar_table(data)
    if tbl is None:
        return {"found": False, "applied": False, "enabled": True, "sites": 0,
                "writes": [], "reason": "GameVarTbl not found"}

    entries = (
        ("gamevar_lives", GAMEVAR_LIVES_INDEX,
         PERMA_LIVES_VANILLA_ENTRY, PERMA_LIVES_PATCHED_ENTRY),
        ("gamevar_deathcoins", GAMEVAR_DEATHCOINS_INDEX,
         PERMA_DEATHCOINS_VANILLA_ENTRY, PERMA_DEATHCOINS_PATCHED_ENTRY),
    )
    entry_writes: list[tuple[str, int, bytes]] = []
    for name, idx, vanilla_entry, patched_entry in entries:
        off = tbl + idx * GAMEVAR_ENTRY_SIZE
        if off + GAMEVAR_ENTRY_SIZE > len(data):
            return {"found": True, "applied": False, "enabled": True,
                    "sites": 0, "writes": [],
                    "reason": f"{name}: GameVarTbl entry past end of buffer"}
        cur = bytes(data[off:off + GAMEVAR_ENTRY_SIZE])
        if cur not in (vanilla_entry, patched_entry):
            return {"found": True, "applied": False, "enabled": True,
                    "sites": 0, "writes": [],
                    "reason": f"{name}: unexpected GameVarTbl entry "
                              f"{cur.hex()} at 0x{off:X}"}
        entry_writes.append((name, off, patched_entry))

    word_sites = (
        ("setdeath_gameover",
         (LIFE_GAMEOVER_SIGNATURE, PERMA_GAMEOVER_PATCHED_SIGNATURE),
         LIFE_GAMEOVER_SLOT_DELTA,
         LIFE_GAMEOVER_EXPECTED_WORD, LIFE_GAMEOVER_PATCHED_WORD,
         "lives game-over test"),
        ("continue_lives",
         (PERMA_CONTINUE_SIGNATURE, PERMA_CONTINUE_PATCHED_SIGNATURE),
         PERMA_CONTINUE_SLOT_DELTA,
         PERMA_CONTINUE_EXPECTED_WORD, PERMA_CONTINUE_PATCHED_WORD,
         "ContinuePlayer life restore"),
    )
    word_writes: list[tuple[str, int, int]] = []
    for name, sigs, delta, expected, patched, label in word_sites:
        # Vanilla form first, then the already-patched form (idempotence).
        matches: list[int] = []
        for sig in sigs:
            matches = _find_all(buf, sig)
            if matches:
                break
        if not matches:
            return {"found": False, "applied": False, "enabled": True,
                    "sites": 0, "writes": [],
                    "reason": f"{name}: {label} site not found"}
        slot = matches[0] + delta
        if slot + 4 > len(data):
            return {"found": True, "applied": False, "enabled": True,
                    "sites": 0, "writes": [],
                    "reason": f"{name}: target slot past end of buffer"}
        cur = struct.unpack_from("<I", data, slot)[0]
        if cur not in (expected, patched):
            return {"found": True, "applied": False, "enabled": True,
                    "sites": 0, "writes": [],
                    "reason": f"{name}: expected word 0x{expected:08X} at "
                              f"0x{slot:X} in {label}, found 0x{cur:08X}"}
        word_writes.append((name, slot, patched))

    # GameVarLoad tail: four words, located off ONE signature match so no offset
    # is hardcoded. Same idempotence rule — each slot accepts vanilla or patched.
    vl_matches = _find_all(buf, PERMA_VARLOAD_SIGNATURE) \
        or _find_all(buf, PERMA_VARLOAD_PATCHED_SIGNATURE)
    if not vl_matches:
        return {"found": False, "applied": False, "enabled": True, "sites": 0,
                "writes": [], "reason": "varload_tail: GameVarLoad tail site "
                                        "not found"}
    vl_base = vl_matches[0]
    for i, (delta, expected, patched) in enumerate(PERMA_VARLOAD_WRITES):
        slot = vl_base + delta
        if slot + 4 > len(data):
            return {"found": True, "applied": False, "enabled": True,
                    "sites": 0, "writes": [],
                    "reason": f"varload_tail[{i}]: target slot past end of "
                              f"buffer"}
        cur = struct.unpack_from("<I", data, slot)[0]
        if cur not in (expected, patched):
            return {"found": True, "applied": False, "enabled": True,
                    "sites": 0, "writes": [],
                    "reason": f"varload_tail[{i}]: expected word "
                              f"0x{expected:08X} at 0x{slot:X} in GameVarLoad "
                              f"tail, found 0x{cur:08X}"}
        word_writes.append((f"varload_tail[{i}]", slot, patched))

    # The ZERO CLAMP on the inline lives decrement. Perma death needs it exactly
    # as much as a raised life cost does: lives start at 0, so the FIRST death
    # underflows the counter with the vanilla cost of 1. `cost=None` means
    # "install with the vanilla cost, and leave an existing clamp's cost alone",
    # so this works in either order with patch_life_cost and never overwrites a
    # life-cost value the user asked for. ONE implementation, shared.
    clamp = _life_clamp_plan(data, None)
    if isinstance(clamp, str):
        return {"found": False, "applied": False, "enabled": True, "sites": 0,
                "writes": [], "reason": f"lives_clamp: {clamp}"}

    # ---- PHASE TWO: everything verified, so every write is safe. ----
    writes: list[dict] = []
    for name, off, patched_entry in entry_writes:
        data[off:off + GAMEVAR_ENTRY_SIZE] = patched_entry
        writes.append({"site": name, "offset": off,
                       "bytes": patched_entry.hex()})
    for name, slot, patched in word_writes:
        struct.pack_into("<I", data, slot, patched)
        writes.append({"site": name, "offset": slot, "word": patched})
    _life_clamp_apply(data, clamp)
    if not clamp["reused"]:
        writes.append({"site": "lives_clamp", "offset": clamp["hook_off"],
                       "cave_vaddr": clamp["cave_vaddr"]})

    return {"found": True, "applied": True, "enabled": True,
            "sites": len(writes), "writes": writes,
            "table": tbl, "clamp": clamp}


# ============================================================================
# Sword enchantment point COST per special move
# ============================================================================
# Exposed to users as FOUR NAMED PRESETS plus vanilla (see
# SWORD_COST_MULTIPLIERS below), and unlike the shield editor the preset is a
# MULTIPLIER, not a flat cost:
#   none 0x | vanilla 1x (default, no patch) | light 2x | medium 3x | heavy 5x
# It has to be a multiplier because the five spend sites deliberately cost
# DIFFERENT amounts (4 / 2 / 2 / 2 / 1); one flat value would flatten that
# balance and make a shockwave as cheap as a normal attack. Labels are resolved
# to an int multiplier at the two edges (argparse and the GUI combobox) via
# resolve_sword_cost(); the wire format below stays an integer everywhere.
#
# Sword enchantment points are GameVar #11 ("sword_units"), the raw word at
# gPlayer+0x74 (= 0x48 + 4*11). GameVarTbl entry #11 = `00 00 14 00 01 01`
# (initial 0, CAP 20, locked, persistent).
#
# Unlike shield points, sword points ARE spent through the accessor:
# GameVarSub(this, 11, N). There is no inline read-modify-write on gPlayer+0x74
# at all — the 0x74 loads that exist are affordability gates, not spends. So
# every patch below rewrites the third argument of a `jal GameVarSub` call:
#     word = SWORD_COST_ADDIU_BASE | (cost & 0xFFFF)   ->  addiu $a2, $zero, N
# a POSITIVE immediate, unlike shield's negative one. GameVarSub clamps at zero,
# so no underflow is possible and cost 0 is a plain harmless no-op subtract —
# none of shield's store-elision subtleties apply here.
#
# Economy, for reference: the bar is refilled by GameVarSet(this, 11, 20) (full
# refill when the ability is acquired) and GameVarAdd(this, 11, 5) (pickup
# grant), both inside Player::SetAbility. NEITHER must ever be patched.
#
# FIVE spend sites. Four are direct cost-immediate rewrites:
#   shockwave   Player::SetShockWave    cost 4   slot word 0x24060004
#   downslice   Player::DownSliceMove   cost 2   slot word 0x24060002
#   projectile  Player::ProjectileMove  cost 2   slot word 0x24060002
#   onattack    Player::OnAttack        cost 1   slot word 0x24060001
#
# The fifth, SUNSLICE (Player::SunSliceMove, cost 2), is SPECIAL and must be
# handled exactly as coded below or it corrupts an unrelated field. At that site
# the cost register $a2 is ALSO stored into Player+0x110 (`sw $a2, 272($s3)`),
# which is the move's PHASE COUNTER, not a cost. Rewriting the cost immediate
# there would write the cost into the phase field; phase 0 re-runs the init
# phase forever and phase 1 re-runs the fire phase forever, draining the bar
# every frame. So we LEAVE `addiu $a2, $zero, 2` alone (it keeps feeding the
# phase store) and write the real cost into the `jal GameVarSub` DELAY SLOT,
# replacing `por $a3, $zero, $zero` (0x70003E28) with `addiu $a2, $zero, N`.
# $a3 is the third parameter `Uc` of GameVarSub__6PlayerFiiUc: it is captured
# into $s0 and read in exactly ONE place, the death branch
# (SetAction(a3 ? a3 : 19)). The index-11 handler is a separate explicit branch
# that falls through to the epilogue without ever reading $s0, so $a3 is
# provably dead on this path. Verified in retail SLUS too.
# Consequence: the sunslice signature is UNAFFECTED by its own patch, so
# locate/patch/revert are idempotent and the slot word tells you the current
# state (0x70003E28 = vanilla, 0x2406xxxx = patched). Its expected word is
# therefore 0x70003E28, not a 0x2406000N like the other four.
#
# Every signature measured EXACTLY ONE occurrence in BOTH SLUS_200.17 and
# SLPM_621.27 (exhaustive scan, not sampled), so US and JP retail are both
# covered. Measured slots:
#   SLUS_200.17  0x05BBE4 / 0x05D3DC / 0x05D5A0 / 0x05D7B8 / 0x05DF4C
#   SLPM_621.27  0x05BDA4 / 0x05D59C / 0x05D760 / 0x05D978 / 0x05E10C
# CAVEAT: the bare word 0x24060002 on its own occurs 28 times per retail
# executable, so the `$a1 = 11` + `por $a0,$sN` context is load-bearing. Do NOT
# shorten any signature.
#
# OUT OF SCOPE, deliberately: the AFFORDABILITY GATES are a separate surface and
# are left vanilla (DownSlice requires >=2, LungeMove >=2, StabDownMove branches
# at <4 — distinct `slti` immediates). So at the high presets a move can become
# available and then drain the bar (clamped at zero by GameVarSub) rather than
# being refused. Scaling the gates would be its own patch.
# ============================================================================

SWORD_UNITS_PLAYER_OFFSET = 0x74        # gPlayer + 0x48 + 4*11 (GameVar #11)

SWORD_COST_ADDIU_BASE = 0x24060000      # addiu $a2, $zero, <imm>  (positive)
SWORD_COST_VANILLA_MULT = 1             # 1x = the game's own costs, no patch
# The GameVar #11 cap. A per-site cost above the cap is indistinguishable from
# "this move always empties the bar", so it is rejected rather than silently
# clamped.
SWORD_COST_CAP = 20
SWORD_COST_EXPECTED_SITES = 5           # measured in SLUS and SLPM

# Named MULTIPLIER presets. Insertion order is ascending cost on purpose: the
# CLI/GUI list them in this order, and sorting alphabetically would read
# "heavy, light, medium, none", which is nonsense.
#   none    0x  every special move is free
#   vanilla 1x  the game's own costs — no patch is applied
#   light   2x  4/2/2/2/1  ->  8/4/4/4/2
#   medium  3x             -> 12/6/6/6/3
#   heavy   5x             -> 20/10/10/10/5   (shockwave hits the cap exactly)
SWORD_COST_MULTIPLIERS = {
    "none": 0,
    "vanilla": SWORD_COST_VANILLA_MULT,
    "light": 2,
    "medium": 3,
    "heavy": 5,
}


def resolve_sword_cost(value) -> int | None:
    """Preset label (or int, or None) -> the multiplier patch_sword_cost wants.

    Single source of truth for both edges (the two argparse layers and the GUI
    combobox) so the label -> value mapping is never duplicated. Returns None for
    None, "vanilla", blank, and anything unrecognised — i.e. "leave the
    executable's own costs alone". Labels are case-insensitive.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    label = str(value).strip().lower()
    if not label or label == "vanilla":
        return None
    return SWORD_COST_MULTIPLIERS.get(label)


# One entry per spend site:
#   (name, signature, slot_delta, expected_word, vanilla_cost, occurrences)
# Each site carries its OWN expected word — sunslice's target is the jal delay
# slot (`por $a3,$zero,$zero`), the other four target their own cost immediate.
SWORD_COST_SITES = (
    # addiu $a1,$zero,11 / addiu $a2,$zero,4 / por $a0,$zero,$s6  -- SetShockWave
    ("shockwave",
     bytes.fromhex("0b000524040006242826c072"), 4, 0x24060004, 4, 1),
    # addiu $a1,$zero,11 / addiu $a2,$zero,2 / por $a0,$zero,$s0  -- DownSliceMove
    ("downslice",
     bytes.fromhex("0b0005240200062428260072"), 4, 0x24060002, 2, 1),
    # addiu $a2,$zero,2 / addiu $a1,$zero,11 / sw $a2,272($s3) / por $a0,$zero,$s3
    # -- SunSliceMove. slot_delta 0x14 is the jal's DELAY SLOT, 4 bytes PAST the
    # signature, so the signature never covers the word we write (idempotent).
    ("sunslice",
     bytes.fromhex("020006240b000524100166ae28266072"), 0x14, 0x70003E28, 2, 1),
    # lw $s0,76($s2) / addiu $a1,$zero,11 / addiu $a2,$zero,2 / por $a0,$zero,$s2
    # -- ProjectileMove
    ("projectile",
     bytes.fromhex("4c00508e0b0005240200062428264072"), 8, 0x24060002, 2, 1),
    # addiu $a1,$zero,11 / addiu $a2,$zero,1 / por $a0,$zero,$s3  -- OnAttack
    ("onattack",
     bytes.fromhex("0b0005240100062428266072"), 4, 0x24060001, 1, 1),
)


def patch_sword_cost(data: bytearray, multiplier: int | None) -> dict:
    """Scale how many sword enchantment points each special move spends.

    multiplier: 0..(SWORD_COST_CAP // 4), applied to each site's own vanilla
    cost, so 0 makes every special move free. None or SWORD_COST_VANILLA_MULT
    (1) = no change. Mutates `data` in place. Returns a stats dict.

    Rewrites the 16-bit immediate that feeds the third argument of
    GameVarSub(this, 11, N) at every spend site. Sword points DO go through the
    accessor (shield points do not), and GameVarSub clamps at zero, so no
    underflow is possible. The sunslice site writes the delay slot instead of
    the cost immediate, because there the cost register is reused as the move's
    phase counter — see the section comment above.

    The affordability gates (slti immediates) are left vanilla by design.
    """
    # 1) Argument validation first, so an invalid multiplier never searches.
    if multiplier is None:
        return {"applied": False, "multiplier": None}
    if multiplier == SWORD_COST_VANILLA_MULT:
        return {"applied": False, "multiplier": multiplier,
                "reason": "1x is vanilla"}
    if not isinstance(multiplier, int) or isinstance(multiplier, bool) \
            or multiplier < 0:
        return {"applied": False, "multiplier": multiplier,
                "reason": "multiplier must be a non-negative integer"}
    # Resolve every per-site cost up front and validate against the GameVar cap.
    costs: dict[str, int] = {}
    for name, _sig, _delta, _word, vanilla_cost, _occ in SWORD_COST_SITES:
        cost = vanilla_cost * multiplier
        if cost > SWORD_COST_CAP:
            return {"applied": False, "multiplier": multiplier,
                    "reason": f"{name} cost {cost} exceeds the GameVar cap "
                              f"of {SWORD_COST_CAP}"}
        costs[name] = cost

    buf = bytes(data)

    # 2) Locate EVERY occurrence of EVERY signature, deduped by slot offset.
    located: dict[int, str] = {}
    for name, sig, slot_delta, _word, _vanilla_cost, _occ in SWORD_COST_SITES:
        for match in _find_all(buf, sig):
            slot = match + slot_delta
            if slot + 4 > len(data):
                continue
            located.setdefault(slot, name)

    expected = SWORD_COST_EXPECTED_SITES
    if not located:
        return {"found": False, "applied": False, "multiplier": multiplier,
                "sites": 0, "expected_sites": expected,
                "reason": "no sword-cost site found"}

    expected_words = {name: word
                      for name, _s, _d, word, _v, _o in SWORD_COST_SITES}

    # 3) Per-slot word verification, then 4) the writes.
    slots: list[int] = []
    skipped: list[dict] = []
    for slot in sorted(located):
        name = located[slot]
        if bytes(data[slot:slot + 4]) != struct.pack("<I", expected_words[name]):
            skipped.append({
                "site": name, "slot": slot,
                "reason": f"expected word 0x{expected_words[name]:08X} not "
                          f"found at target",
            })
            continue
        struct.pack_into("<I", data, slot,
                         SWORD_COST_ADDIU_BASE | (costs[name] & 0xFFFF))
        slots.append(slot)

    n = len(slots)
    if n == 0:
        return {"found": True, "applied": False, "multiplier": multiplier,
                "sites": 0, "expected_sites": expected, "slots": [],
                "skipped": skipped,
                "reason": "no sword-cost site had the expected instruction"}

    res = {"found": True, "applied": True, "multiplier": multiplier,
           "sites": n, "expected_sites": expected, "slots": slots,
           "costs": costs}
    if skipped:
        res["skipped"] = skipped
    if n != expected:
        res["reason"] = f"patched {n} of {expected} sword-cost sites"
    return res


# ============================================================================
# Status-effect DURATIONS (form timers + the frozen timer)
# ============================================================================
# Three INDEPENDENT editors, each a two-byte in-place rewrite of a `lui`
# immediate — the UPPER half-word of an inline float. These are inline
# immediates, NOT constant-pool entries, so nothing else shares them.
#
# THE FORM TIMER. Every form-based timed effect shares ONE field, Player+0x38.
# It is initialised by SetMaximo__6PlayerFUc (the `lui` we patch) and counted
# down per frame by CheckTimer__6PlayerFv:
#     lwc1 f1,0x38($s1)   ; timer
#     lwc1 f0,...         ; per-frame delta (forced to 1.0 during gameplay)
#     sub.s f0,f1,f0
#     c.le.s f0,f2        ; <= 0.0  -- a SAFE comparison, not == 0.0
#     bc1f ...
#     swc1 f0,0x38($s1)   ; delay-slot store
#     ...
#     sw   $zero,0x38($s1); expired -> revert form / thaw
# Because the exit test is `<= 0.0` (not an exact equality like the lives
# game-over test), ANY positive duration is safe — no second coordinated write
# is needed, so unlike perma-death there is no cross-site invariant here.
# UNITS ARE FRAMES at 60 ticks/second; CheckTimer divides by 60.0 purely for its
# HUD seconds readout. Retail CheckTimer is confirmed equivalent (SLUS
# 0x0573FC-0x057410, same `<= 0` exit).
#
# Form ids: 1 = Baby, 3 = Old Man, 5 = Mask of Sorrow, 6 = Golden Armor. The
# wizard picks WHICH form via the level script (CScriptApi::ChangeMaximo ->
# SetMaximo), but the DURATION lives in the executable, which is why it is
# patchable here at all.
#
# *** 0 IS PERMANENT, NOT "NO EFFECT" — WHY "none" IS 1 TICK, NOT 0. ***
# CheckTimer opens with an early `if (timer == 0.0) return;` guard, so a zero
# duration means the form NEVER expires. A permanent Baby or Old Man form is a
# real soft-lock (those forms swap in restricted movesets: BabyOffBoxes /
# BabyAdjust / OldmanOffBoxes / OldmanAdjust), and a permanent Golden Armor is
# infinite invincibility — the exact OPPOSITE of what "none" means.
# So the "none" preset writes ONE TICK: the float 1.0, `lui 0x3F80`, slot bytes
# `80 3F`. CheckTimer subtracts the per-frame delta (forced to 1.0 during
# gameplay), lands on exactly 0.0, and its `c.le.s f0, f2` (`<= 0.0`) fires on
# the very first check, so the effect expires on its first frame. That is "no
# time" from the player's point of view while staying strictly non-zero.
# Nothing SMALLER is usable: below 1.0 the only floats with a zero low
# half-word are denormals, and the PS2 EE FPU flushes denormals to zero, which
# lands straight back on the permanent-effect case. 1.0 is the correct floor,
# which is why EFFECT_DURATION_MIN_TICKS is 1 — but emitting an immediate of
# `0x0000` remains an ABSOLUTE never, and is asserted below.
#
# *** COUPLING — cannot be separated with a word edit. *** Golden Armor
# (arm 0x0012FB10) and Mask of Sorrow (arm 0x0012FB04) FALL THROUGH into the
# same two instructions, so they share ONE constant ("powerup"). Baby and Old
# Man likewise share a different single constant ("wizard"). Splitting either
# pair would require relocating code to make room for a second immediate —
# out of scope. Frozen is fully independent.
#
# FROZEN IS A DIFFERENT SUBSYSTEM. FrozenMove has no timer at all (that is the
# freeze-DEATH state). The real duration is the object ice timer, written only
# by OnHit__6PlayerFUiP15DEDynamicObjectiif and counted down by
# UpdateFreezeTimer__15DEDynamicObjectFf (sub.s + c.le.s + clamp + shatter
# sound). There is NO mash-to-escape input: nothing else writes obj+0x40, and
# only SetAction clears it. The instruction pair at this site ALSO loads a 1.0
# ice-mesh attach delay into obj+0x44 — that second float must NOT be touched,
# which is why the frozen slot_delta is 0 (the first `lui` of the pair).
#
# *** A SITE THAT MUST NEVER BE PATCHED. *** SetMaximo's out-of-range-form
# guard also uses `lui 0x4416` (600.0): see EFFECT_FORM_GUARD_SIGNATURE,
# SLUS slot 0x04FAB8 / SLPM slot 0x04FCD8. Its purpose is unconfirmed and it is
# reachable only from script ChangeMaximo(form >= 7). The "wizard" signature is
# distinct — it starts `6a 04 80 a2`, the guard starts `24 00 83 8e` — and the
# wizard signature matches EXACTLY ONCE in both retail builds, so it cannot
# collide with the guard. The guard constant is kept here only so comments and
# verification can reference it; no code path ever writes to it.
#
# ENCODABILITY RULE. We rewrite only the float's UPPER half-word, so the value
# must have a ZERO LOW HALF-WORD or the resulting float would be wrong. Powers
# of two only change the exponent, which is exactly why the scaling presets are
# 0.5x / 2x (and "none" lands on the exact power of two 1.0).
# patch_effect_duration recomputes the float and ASSERTS
# the low half-word is zero, returning a failure reason rather than writing a
# wrong value; the preset words are never hardcoded.
#
# Measured (exhaustive scan, EXACTLY ONE occurrence of each signature in BOTH
# retail builds — US and JP are both covered):
#   site      vanilla word  vanilla ticks   SLUS_200.17  SLPM_621.27
#   powerup   0x3C024496    1200 (20.0 s)   0x04FDF8     0x050018
#   wizard    0x3C024416     600 (10.0 s)   0x04FDBC     0x04FFDC
#   frozen    0x3C024396     300 ( 5.0 s)   0x05EA20     0x05EBE0
#   form guard (NEVER patch) 0x3C024416     0x04FAB8     0x04FCD8
#
# *** THE PRESET LABELS ARE PLAYER-FACING, SO THE DIRECTION IS INVERTED
# BETWEEN THE TWO GROUPS. *** "easier" ALWAYS favours the player. For the two
# HARMFUL effects a SHORTER duration is easier; for the BENEFICIAL power-up a
# LONGER duration is easier. So the label -> multiplier table cannot be shared:
# it is PER SITE (EFFECT_DURATION_SITE_PRESETS).
#
#   wizard / frozen (harmful: Baby, Old Man, frozen solid)
#       vanilla  no patch at all — the game's own timer
#       none     1 tick (see the "none" note above)
#       easier   0.5x  (HALF the original time)
#       harder   2x    (DOUBLE the original time)
#
#   powerup (beneficial: Golden Armor + Mask of Sorrow)
#       vanilla  no patch at all — the game's own timer
#       none     1 tick (see the "none" note above)
#       easier   2x    (DOUBLE the original time)
#       harder   0.5x  (HALF the original time)
# ============================================================================

EFFECT_TICKS_PER_SECOND = 60             # CheckTimer's /60.0 HUD divisor
EFFECT_DURATION_VANILLA_MULT = 1         # 1x = the game's own timers, no patch
# Floor of ONE tick. 0 would be PERMANENT (CheckTimer's `timer == 0.0` early
# return) — a soft-lock for Baby / Old Man and infinite invincibility for
# Golden Armor — and anything between 0 and 1 tick is a denormal that the EE
# FPU flushes to zero. 1 tick expires on its first frame, which is what the
# "none" preset means.
EFFECT_DURATION_MIN_TICKS = 1
EFFECT_DURATION_NONE_TICKS = 1           # "none" = 1 tick = float 1.0 = lui 0x3F80

# `lui $v0, <imm>` — the immediate's two bytes are the FIRST two bytes at the
# slot (little-endian); the `02 3c` that follows is the rest of the lui opcode
# and must be left alone.
EFFECT_LUI_OPCODE_TAIL = bytes.fromhex("023c")

# One entry per site: (signature, slot_delta, expected_word, vanilla_ticks, label)
EFFECT_DURATION_SITES = {
    # lui $v0,0x4496 / sw $v0,0x38($s4)  -- SetMaximo, Golden Armor + Mask of
    # Sorrow fall-through (ONE shared constant, see the coupling note).
    "powerup": (bytes.fromhex("282e60729644023c380082ae"), 4, 0x3C024496,
                1200, "Golden Armor + Mask of Sorrow"),
    # lbu .. / lui $v0,0x4416 / sw $v0,0x38($s4)  -- SetMaximo, Baby + Old Man
    # (ONE shared constant). Starts `6a 04 80 a2`, which is what distinguishes
    # it from the never-patch form>=7 guard (`24 00 83 8e`).
    "wizard": (bytes.fromhex("6a0480a21644023c380082ae28268072282e0070"), 4,
               0x3C024416, 600, "Baby + Old Man"),
    # lui $v0,0x4396 / mtc1 -> obj+0x40 ice timer, then lui $v0,0x3F80 (1.0)
    # -> obj+0x44 ice-mesh attach delay, which must NOT be touched. slot_delta 0
    # targets the FIRST lui only.
    "frozen": (bytes.fromhex("9643023c00608244803f023c00688244"), 0,
               0x3C024396, 300, "Frozen"),
}

# NEVER PATCHED. SetMaximo's out-of-range-form (>= 7) guard, which happens to
# use the same 600.0 immediate as the wizard site. Kept only for documentation
# and verification; no code path below writes to it.
EFFECT_FORM_GUARD_SIGNATURE = bytes.fromhex(
    "2400838e1644023c4200052428268072" "28360070")

# "easier" ALWAYS favours the player, so the DIRECTION is inverted between the
# harmful effects and the beneficial power-up (see the banner note). These two
# little tables are the ONLY place the directions live.
_EFFECT_HARMFUL_DIRECTION = {"easier": 0.5, "harder": 2}      # shorter = easier
_EFFECT_BENEFICIAL_DIRECTION = {"easier": 2, "harder": 0.5}   # longer = easier
_EFFECT_DURATION_DIRECTIONS = {
    "powerup": _EFFECT_BENEFICIAL_DIRECTION,   # Golden Armor + Mask of Sorrow
    "wizard": _EFFECT_HARMFUL_DIRECTION,       # Baby + Old Man
    "frozen": _EFFECT_HARMFUL_DIRECTION,       # frozen solid
}

# PER-SITE label -> multiplier. Built from the site table so "none" is exactly
# EFFECT_DURATION_NONE_TICKS regardless of that site's vanilla tick count, and
# so nothing is duplicated. Insertion-ordered: vanilla, none, easier, harder —
# vanilla first because it is the default (no patch at all), and this is the
# order every listing (argparse choices, GUI combobox, help text) shows.
EFFECT_DURATION_SITE_PRESETS = {
    _sid: {
        "vanilla": EFFECT_DURATION_VANILLA_MULT,
        "none": EFFECT_DURATION_NONE_TICKS / _site[3],
        **_EFFECT_DURATION_DIRECTIONS[_sid],
    }
    for _sid, _site in EFFECT_DURATION_SITES.items()
}

# The label list, derived — never a second hand-written copy.
EFFECT_DURATION_PRESET_LABELS = tuple(EFFECT_DURATION_SITE_PRESETS["powerup"])


def resolve_effect_duration(site_id, value):
    """(site id, preset label / number / None) -> the multiplier the patcher wants.

    Single source of truth for both edges (the two argparse layers and the GUI
    comboboxes) so the label -> value mapping is never duplicated. The site id
    is REQUIRED because the direction is inverted between the harmful effects
    and the beneficial power-up: "easier" is 0.5x for wizard/frozen but 2x for
    powerup. Returns None for None, "vanilla", blank, an unknown site, and
    anything unrecognised — i.e. "leave the executable's own timers alone".
    Labels are case-insensitive.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    label = str(value).strip().lower()
    if not label or label == "vanilla":
        return None
    return EFFECT_DURATION_SITE_PRESETS.get(site_id, {}).get(label)


def patch_effect_duration(data: bytearray, site_id: str, multiplier) -> dict:
    """Scale ONE status-effect duration site's timer.

    site_id: "powerup" (Golden Armor + Mask of Sorrow, coupled), "wizard"
    (Baby + Old Man, coupled) or "frozen". multiplier: a positive number applied
    to that site's vanilla tick count. None or EFFECT_DURATION_VANILLA_MULT (1)
    = no change, and in that case the executable is left byte-identical by this
    feature. Mutates `data` in place. Returns a stats dict.

    Rewrites only the TWO bytes of the `lui` immediate that carry the float's
    upper half-word. The value must therefore have a zero low half-word; that is
    checked, not assumed, and a non-encodable value is rejected rather than
    written wrong. A floor of EFFECT_DURATION_MIN_TICKS (1 tick) is enforced and
    an immediate of 0x0000 is NEVER emitted, because 0 ticks means PERMANENT
    (soft-lock risk) — see the section comment.
    """
    # 1) Argument validation first, so an invalid request never searches the
    # buffer and never writes. A preset LABEL is accepted here too (resolved
    # through the same single source of truth as the CLI/GUI edges); an
    # unrecognised label stays a REPORTED failure rather than a silent skip.
    if isinstance(multiplier, str):
        label_in = multiplier.strip().lower()
        if not label_in or label_in == "vanilla":
            return {"applied": False, "site": site_id, "multiplier": multiplier,
                    "reason": "1x is vanilla"}
        multiplier = resolve_effect_duration(site_id, label_in)
        if multiplier is None:
            return {"applied": False, "site": site_id, "multiplier": label_in,
                    "reason": f"unrecognised duration preset '{label_in}'"}
    if multiplier is None:
        return {"applied": False, "site": site_id, "multiplier": None}
    if multiplier == EFFECT_DURATION_VANILLA_MULT:
        return {"applied": False, "site": site_id, "multiplier": multiplier,
                "reason": "1x is vanilla"}
    site = EFFECT_DURATION_SITES.get(site_id)
    if site is None:
        return {"applied": False, "site": site_id, "multiplier": multiplier,
                "reason": f"unknown effect-duration site '{site_id}'"}
    signature, slot_delta, expected_word, vanilla_ticks, label = site
    if isinstance(multiplier, bool) or not isinstance(multiplier, (int, float)) \
            or multiplier <= 0:
        return {"applied": False, "site": site_id, "multiplier": multiplier,
                "reason": "multiplier must be a positive number"}
    ticks = vanilla_ticks * multiplier
    if ticks < EFFECT_DURATION_MIN_TICKS:
        return {"applied": False, "site": site_id, "multiplier": multiplier,
                "ticks": ticks,
                "reason": f"{label}: {ticks:g} ticks is below the minimum of "
                          f"{EFFECT_DURATION_MIN_TICKS} tick(s); 0 would be "
                          f"PERMANENT and anything between 0 and 1 tick is a "
                          f"denormal the EE FPU flushes to zero"}
    packed = struct.pack("<f", float(ticks))
    # ABSOLUTE rule, asserted rather than assumed: `lui $v0,0x0000` would load
    # the float 0.0, and CheckTimer's early `if (timer == 0.0) return;` guard
    # turns that into a PERMANENT effect (Baby / Old Man soft-lock, infinite
    # Golden Armor). No preset can reach here with 0 — the floor above stops it
    # — but a caller-supplied multiplier must never sneak one through either.
    assert packed[2:4] != b"\x00\x00", (
        f"refusing to emit lui 0x0000 for {label} ({ticks:g} ticks): a zero "
        f"duration is PERMANENT, not instant")
    if packed[:2] != b"\x00\x00":
        return {"applied": False, "site": site_id, "multiplier": multiplier,
                "ticks": ticks,
                "reason": f"{label}: {ticks:g} ticks is not encodable in the "
                          f"lui immediate (low half-word must be zero — use a "
                          f"power-of-two multiplier)"}

    buf = bytes(data)

    # 2) Locate. Each signature is measured as exactly one occurrence in both
    # retail builds; more than one means an unknown build, so bail rather than
    # guess which is the timer.
    matches = _find_all(buf, signature)
    if not matches:
        return {"found": False, "applied": False, "site": site_id,
                "multiplier": multiplier,
                "reason": f"{label}: duration site not found"}
    if len(matches) != 1:
        return {"found": True, "applied": False, "site": site_id,
                "multiplier": multiplier,
                "reason": f"{label}: expected 1 duration site, found "
                          f"{len(matches)}"}
    slot = matches[0] + slot_delta
    if slot + 4 > len(data):
        return {"found": True, "applied": False, "site": site_id,
                "multiplier": multiplier,
                "reason": f"{label}: target slot past end of buffer"}

    # 3) Verify the full 4-byte word. IDEMPOTENCE: we do NOT require the
    # immediate to still be the vanilla one, because re-running with a different
    # preset must work. What we DO require is that the word is still the same
    # `lui $v0` instruction (bytes 2-3 == `02 3c`) and that its immediate is a
    # float with a zero low half-word — i.e. that it is one of our own writes or
    # the vanilla value, and never some unrelated instruction.
    word_bytes = bytes(data[slot:slot + 4])
    if word_bytes[2:4] != EFFECT_LUI_OPCODE_TAIL:
        return {"found": True, "applied": False, "site": site_id,
                "multiplier": multiplier,
                "reason": f"{label}: expected a 'lui $v0' word at 0x{slot:X} "
                          f"(vanilla 0x{expected_word:08X}), found "
                          f"0x{struct.unpack('<I', word_bytes)[0]:08X}"}

    # 4) Write the two immediate bytes only; the `02 3c` opcode tail stays.
    data[slot:slot + 2] = packed[2:4]
    return {"found": True, "applied": True, "site": site_id,
            "multiplier": multiplier, "label": label,
            "ticks": int(ticks), "seconds": ticks / EFFECT_TICKS_PER_SECOND,
            "slot": slot, "sites": 1,
            "vanilla_ticks": vanilla_ticks}


def patch_effect_durations(data: bytearray, powerup=None, wizard=None,
                           frozen=None) -> dict:
    """Apply whichever of the three duration editors were requested.

    Convenience wrapper so the CLI has ONE call. Each site is INDEPENDENT —
    unlike perma-death there is no cross-site invariant, so one site failing
    must not stop the others. Per-site results are reported under "results".
    """
    requested = (("powerup", powerup), ("wizard", wizard), ("frozen", frozen))
    results: dict[str, dict] = {}
    for site_id, multiplier in requested:
        if multiplier is None:
            continue
        results[site_id] = patch_effect_duration(data, site_id, multiplier)
    applied = [k for k, v in results.items() if v.get("applied")]
    failed = {k: v.get("reason", "not applied")
              for k, v in results.items() if not v.get("applied")}
    return {"applied": bool(applied), "sites": len(applied),
            "applied_sites": applied, "failed": failed, "results": results}


# ============================================================================
# KOIN FEE — the price of SAVING the game and of HUB TRAVEL (one control)
# ============================================================================
# Koins are GameVar #5 (cap 9999). An exhaustive sweep of all 34 `jal` sites to
# Player::GameVarSub / GameVarAdd / GameVarSet found EXACTLY TWO carrying index
# 5, and BOTH live inside App::TransportHubLevel:
#
#   * the SAVE cost, an immediate written to `gSaveCost` (small-BSS,
#     $gp-relative). `gSaveCost` has exactly 4 references: this ONE writer and
#     three readers — App::SaveGame (charges it via GameVarSub, and only when it
#     is non-zero), App::UpdatePlayerVars (subtracts it from the koin word in the
#     save record) and InitSave (clears it). One immediate therefore drives every
#     use of the save price, so a single word patch stays self-consistent; there
#     is no second copy of the number to keep in step.
#   * the HUB TRAVEL fee, subtracted directly at the call site.
#
# Both fees are exposed as ONE user-facing control, not two, because the
# on-screen label is a SINGLE SHARED string covering both ("100 Koins for Save
# or Travel"). Two independent values could not be described by it, so the two
# immediates are always written with the SAME value.
#
# ---------------------------------------------------------------------------
# SAFETY CAP — 100 IS THE MAXIMUM. Do not raise it.
# ---------------------------------------------------------------------------
# App::UpdatePlayerVars subtracts the save cost from the saved koin count with NO
# zero clamp (unlike Player::GameVarSub, which does clamp). The invariant only
# holds today because the level script gates affordability at 100 and retail has
# the CdsAssert compiled out. A fee ABOVE 100 can therefore drive the saved koin
# count silently NEGATIVE. So the accepted range is KOIN_FEE_MIN..KOIN_FEE_MAX
# (0..100) and anything above is REFUSED with a reason that says why.
#
# ---------------------------------------------------------------------------
# THE TWO CODE SITES — both need MASKED word matching
# ---------------------------------------------------------------------------
# Both signatures contain fields that VARY between builds ($gp displacements and
# pointer half-words), so a plain `bytes.find` / `_find_all` is NOT usable here.
# That is what `_find_all_masked` below exists for: it matches word-by-word at
# 4-byte alignment under a per-word mask, so the build-varying fields can be
# masked out while the invariant opcodes still pin the site down.
#
#   KOIN_SAVE_PATTERN (8 words, target = word 0, slot delta 0)
#     0x24030064  FFFFFFFF   addiu $v1,$zero,100      <-- TARGET (the fee)
#     0x3C010001  FFFFFFFF   lui   $at,1
#     0x02210821  FFFFFFFF   addu  $at,$s1,$at
#     0xAF830000  FFFF0000   sw    $v1,disp($gp)      MASKED: $gp displacement
#     0xAC205878  FFFFFFFF   sw    $zero,0x5878($at)
#     0x3C010001  FFFFFFFF   lui   $at,1
#     0x02210821  FFFFFFFF   addu  $at,$s1,$at
#     0xAC205870  FFFFFFFF   sw    $zero,0x5870($at)
#
#   KOIN_TRAVEL_PATTERN (6 words, target = word 3, slot delta 12)
#     0x24050005  FFFFFFFF   addiu $a1,$zero,5        GameVar #5 = koins
#     0x3C020000  FFFF0000   lui   $v0,hi             MASKED: pointer half-word
#     0x24440000  FFFF0000   addiu $a0,$v0,lo         MASKED: pointer half-word
#     0x24060064  FFFFFFFF   addiu $a2,$zero,100      <-- TARGET (the fee)
#     0xAF850000  FFFF0000   sw    $a1,disp($gp)      MASKED: $gp displacement
#     0x70003E28  FFFFFFFF   (jal delay slot filler)
#
# Measured: exactly 1 occurrence of each pattern in each retail build.
#   slot offsets   SLUS_200.17   save 0x462F4   travel 0x463B0
#                  SLPM_621.27   save 0x463F4   travel 0x464B0
# Both hold their expected vanilla words (0x24030064 / 0x24060064) in retail.
#
# ---------------------------------------------------------------------------
# THE DISPLAY STRING
# ---------------------------------------------------------------------------
# String id 99 is the single shared label: "100 Koins for Save or Travel" — 28
# characters + NUL in a 32-byte slot, so 3 bytes of slack, which is enough for
# any 1..4 digit value. It is located by SEARCHING FOR ITS OWN BYTES (self-
# anchoring and unique), never by a hardcoded file offset — that is the project
# convention. For reference only it resolves to SLUS 0xCC1F0 / SLPM 0xCC460.
# The rewrite zero-fills the rest of the 32-byte slot so no stale tail bytes
# survive when the number gets shorter (e.g. 100 -> 0).
#
# The label is BEST-EFFORT while the two immediates are ATOMIC. Rationale: the
# immediates ARE the behaviour and the string is cosmetic, so a missing string
# must not block the functional change — but the user has to be told, hence the
# `label_patched` flag and a `reason`. The two immediates, by contrast, are
# all-or-nothing: charging one fee while the other stays vanilla would be
# incoherent for a single user-facing control that has one shared label.
#
# OUT OF SCOPE, deliberately: the ELEVEN Sorceress shop prices. They are NOT in
# the executable — they are level-script bytecode in .PRS/.PSX, with the
# displayed prices as separate hardcoded strings. Do not attempt them here.
# ============================================================================

KOIN_FEE_VANILLA = 100                  # the game's own price — no patch applied
KOIN_FEE_MIN = 0                        # 0 = saving and hub travel are free
# Hard ceiling, and NOT an arbitrary one: App::UpdatePlayerVars' koin subtract in
# the save record is UNCLAMPED, so a fee above the script's own 100 affordability
# gate can push the saved koin count negative. See the section comment.
KOIN_FEE_MAX = 100

# (word, mask) pairs, matched at 4-byte alignment. Masked words carry build-
# varying $gp displacements / pointer half-words — see the section comment.
KOIN_SAVE_PATTERN = (
    (0x24030064, 0xFFFFFFFF),   # addiu $v1,$zero,100   <-- TARGET
    (0x3C010001, 0xFFFFFFFF),
    (0x02210821, 0xFFFFFFFF),
    (0xAF830000, 0xFFFF0000),   # sw $v1,disp($gp)      MASKED
    (0xAC205878, 0xFFFFFFFF),
    (0x3C010001, 0xFFFFFFFF),
    (0x02210821, 0xFFFFFFFF),
    (0xAC205870, 0xFFFFFFFF),
)
KOIN_SAVE_SLOT_DELTA = 0
KOIN_SAVE_EXPECTED_WORD = 0x24030064    # addiu $v1,$zero,100    (vanilla)
KOIN_SAVE_ADDIU_BASE = 0x24030000       # addiu $v1,$zero,<imm>

KOIN_TRAVEL_PATTERN = (
    (0x24050005, 0xFFFFFFFF),   # addiu $a1,$zero,5  -> GameVar #5 (koins)
    (0x3C020000, 0xFFFF0000),   # lui   $v0,hi          MASKED
    (0x24440000, 0xFFFF0000),   # addiu $a0,$v0,lo      MASKED
    (0x24060064, 0xFFFFFFFF),   # addiu $a2,$zero,100   <-- TARGET
    (0xAF850000, 0xFFFF0000),   # sw $a1,disp($gp)      MASKED
    (0x70003E28, 0xFFFFFFFF),
)
KOIN_TRAVEL_SLOT_DELTA = 12
KOIN_TRAVEL_EXPECTED_WORD = 0x24060064  # addiu $a2,$zero,100    (vanilla)
KOIN_TRAVEL_ADDIU_BASE = 0x24060000     # addiu $a2,$zero,<imm>

# String id 99, the ONE label covering both fees. 28 chars + NUL in a 32-byte
# slot. Found by its own bytes, never by a hardcoded offset.
KOIN_FEE_LABEL_VANILLA = b"100 Koins for Save or Travel"
KOIN_FEE_LABEL_SLOT = 32
# The invariant tail every accepted form of the label shares, so an already
# patched label (any digit count) is still recognised on a re-run.
_KOIN_FEE_LABEL_TAIL = b" Koins for Save or Travel"


def _find_all_masked(buf: bytes, pattern) -> list[int]:
    """Every 4-byte-aligned offset at which `pattern` matches in `buf`.

    `pattern` is a sequence of (word, mask) pairs, each compared as
    `(actual & mask) == (word & mask)` against a little-endian 32-bit word.
    Needed because BOTH koin-fee signatures contain build-varying fields — $gp
    displacements and pointer half-words — that must be masked out, so plain
    byte matching via `_find_all` cannot be used for them. `_find_all` stays the
    tool for every fixed-byte signature in this module.
    """
    out: list[int] = []
    if not pattern:
        return out
    span = len(pattern) * 4
    if len(buf) < span:
        return out
    first_word, first_mask = pattern[0]
    first_word &= first_mask
    limit = len(buf) - span
    for off in range(0, limit + 1, 4):
        if struct.unpack_from("<I", buf, off)[0] & first_mask != first_word:
            continue
        for i, (word, mask) in enumerate(pattern[1:], start=1):
            if struct.unpack_from("<I", buf, off + i * 4)[0] & mask != word & mask:
                break
        else:
            out.append(off)
    return out


def _koin_fee_find_label(buf: bytes) -> int:
    """Offset of the shared save/travel label, or -1.

    IDEMPOTENCE: accepts the vanilla text AND any already-patched
    `<digits> Koins for Save or Travel`, so re-running with a different fee
    works — consistent with the other patchers in this module, which all
    tolerate re-runs.
    """
    hit = buf.find(KOIN_FEE_LABEL_VANILLA + b"\x00")
    if hit >= 0:
        return hit
    for tail in _find_all(buf, _KOIN_FEE_LABEL_TAIL):
        start = tail
        while start > 0 and 0x30 <= buf[start - 1] <= 0x39:
            start -= 1
        if start == tail:
            continue            # no digits in front: not our label
        end = tail + len(_KOIN_FEE_LABEL_TAIL)
        if end < len(buf) and buf[end] == 0:
            return start
    return -1


def patch_koin_fee(data: bytearray, fee: int | None) -> dict:
    """Set the koin price of SAVING the game and of HUB TRAVEL (one value).

    fee: KOIN_FEE_MIN..KOIN_FEE_MAX (0..100); 0 makes saving and hub travel
    free. None or KOIN_FEE_VANILLA (100) = no change, and in that case the
    executable is left byte-identical by this feature. Mutates `data` in place.
    Returns a stats dict.

    Rewrites the 16-bit immediate of TWO instructions inside
    App::TransportHubLevel — the `addiu $v1,$zero,100` whose value is stored to
    `gSaveCost`, and the `addiu $a2,$zero,100` that is the third argument of
    GameVarSub(this, 5, N) for hub travel — and rewrites the single shared
    on-screen label so the displayed price matches.

    The two immediates are ATOMIC (both verified before either is written); the
    label is BEST-EFFORT and reported via `label_patched`. Values above
    KOIN_FEE_MAX are REFUSED because the save record's koin subtract in
    App::UpdatePlayerVars is unclamped. See the section comment above.
    """
    # 1) Argument validation first, so an invalid fee never searches the buffer.
    if fee is None:
        return {"applied": False, "fee": None, "sites": 0, "slots": [],
                "label_patched": False}
    if fee == KOIN_FEE_VANILLA and isinstance(fee, int) \
            and not isinstance(fee, bool):
        return {"applied": False, "fee": fee, "sites": 0, "slots": [],
                "label_patched": False,
                "reason": f"{KOIN_FEE_VANILLA} koins is vanilla"}
    if not isinstance(fee, int) or isinstance(fee, bool) or fee < KOIN_FEE_MIN:
        return {"applied": False, "fee": fee, "sites": 0, "slots": [],
                "label_patched": False,
                "reason": f"fee must be an integer {KOIN_FEE_MIN}.."
                          f"{KOIN_FEE_MAX}"}
    if fee > KOIN_FEE_MAX:
        return {"applied": False, "fee": fee, "sites": 0, "slots": [],
                "label_patched": False,
                "reason": f"fee must be {KOIN_FEE_MIN}..{KOIN_FEE_MAX}: the "
                          f"save record's koin subtract in "
                          f"App::UpdatePlayerVars is UNCLAMPED, so a fee above "
                          f"{KOIN_FEE_MAX} can drive the saved koin count "
                          f"negative"}

    buf = bytes(data)

    # 2) PHASE ONE — locate BOTH immediates. Nothing is written in this phase.
    sites = (("save", KOIN_SAVE_PATTERN, KOIN_SAVE_SLOT_DELTA,
              KOIN_SAVE_EXPECTED_WORD, KOIN_SAVE_ADDIU_BASE),
             ("travel", KOIN_TRAVEL_PATTERN, KOIN_TRAVEL_SLOT_DELTA,
              KOIN_TRAVEL_EXPECTED_WORD, KOIN_TRAVEL_ADDIU_BASE))
    resolved: list[tuple[str, int, int]] = []     # (name, slot, word to write)
    for name, pattern, delta, expected, base in sites:
        matches = _find_all_masked(buf, pattern)
        if not matches:
            return {"found": False, "applied": False, "fee": fee, "sites": 0,
                    "slots": [], "label_patched": False,
                    "reason": f"koin {name}-fee site not found"}
        if len(matches) != 1:
            return {"found": True, "applied": False, "fee": fee, "sites": 0,
                    "slots": [], "label_patched": False,
                    "reason": f"koin {name}-fee: expected 1 site, found "
                              f"{len(matches)}"}
        slot = matches[0] + delta
        if slot + 4 > len(data):
            return {"found": True, "applied": False, "fee": fee, "sites": 0,
                    "slots": [], "label_patched": False,
                    "reason": f"koin {name}-fee: target slot past end of buffer"}
        # 3) Verify. IDEMPOTENCE: the vanilla word OR one of our own earlier
        # writes (same opcode/registers, any immediate) is accepted, so
        # re-running with a different fee works — the other patchers in this
        # module all tolerate re-runs and this one must not be the exception.
        word = struct.unpack_from("<I", data, slot)[0]
        if word != expected and (word & 0xFFFF0000) != base:
            return {"found": True, "applied": False, "fee": fee, "sites": 0,
                    "slots": [], "label_patched": False,
                    "reason": f"koin {name}-fee: expected word "
                              f"0x{expected:08X} at 0x{slot:X}, found "
                              f"0x{word:08X}"}
        resolved.append((name, slot, base | (fee & 0xFFFF)))

    # 4) PHASE TWO — both immediates verified, so both writes are safe. This is
    # all-or-nothing on purpose: one fee charged and the other left vanilla
    # would be incoherent for a single control with one shared label.
    for _name, slot, word in resolved:
        struct.pack_into("<I", data, slot, word)
    slots = [slot for _n, slot, _w in resolved]

    res = {"found": True, "applied": True, "fee": fee, "sites": len(slots),
           "slots": slots, "label_patched": False}

    # 5) The label, BEST-EFFORT — cosmetic, so a miss must not undo the two
    # functional writes above; the user is told via label_patched + reason.
    label_off = _koin_fee_find_label(buf)
    if label_off < 0:
        res["reason"] = ("koin fee applied, but the shared "
                         "'<n> Koins for Save or Travel' label was not found — "
                         "the displayed price will be stale")
        return res
    text = f"{fee}{_KOIN_FEE_LABEL_TAIL.decode('ascii')}".encode("ascii")
    assert len(text) + 1 <= KOIN_FEE_LABEL_SLOT, (
        f"refusing to write a {len(text)}-byte label plus NUL into a "
        f"{KOIN_FEE_LABEL_SLOT}-byte slot")
    # Zero-fill the whole slot so no stale tail bytes survive a shorter number.
    data[label_off:label_off + KOIN_FEE_LABEL_SLOT] = \
        text + b"\x00" * (KOIN_FEE_LABEL_SLOT - len(text))
    res["label_patched"] = True
    res["label_slot"] = label_off
    res["label"] = text.decode("ascii")
    return res


# ============================================================================
# DISABLE WORLD COLLISION — the developers' own `bDoCollision` switch
# ============================================================================
# !!! SAFETY WARNING — READ THIS FIRST !!!
# This is a DEVELOPER RESEARCH SWITCH, not a playable mode. With collision off
# the player falls out of the level as soon as gravity applies, and retail has NO
# fly/noclip movement mode to compensate (the demo paired this flag with a debug
# free-fly camera whose display code retail does not contain). A SOFT-LOCK is
# LIKELY if the kill-plane check sits inside the skipped block — that was NOT
# verified. It is fully reversible (one byte). Treat it as a screenshot /
# research tool, never as a gameplay option.
#
# WHAT THE FLAG IS
# ---------------------------------------------------------------------------
# `bDoCollision` is a ONE-BYTE master switch for PLAYER and CAMERA world
# collision. It lives in INITIALISED DATA, so it is already `1` at boot — no
# code sets it. It has exactly TWO readers and PROVABLY ZERO WRITERS in each
# retail build (verified by an exhaustive scan for both $gp-relative and
# lui+offset absolute reference forms), so NOTHING in the game can ever change
# it: clearing the data byte disables collision permanently for that run.
#
# Both readers are an `lbu` immediately followed by `beq $v0,$zero,<skip>`, i.e.
#   if (bDoCollision) { ...collision... }
#
# THE TWO READER SITES — both need MASKED word matching
# ---------------------------------------------------------------------------
# The `lbu` displacement varies by build (different $gp), and branch offsets are
# never signature-safe, so `_find_all_masked` is used with those fields masked.
#
#   COLLISION_READER1_PATTERN — Player::ControlPlayer (6 words, target = word 4)
#     0x8E620128  FFFFFFFF   lw   $v0,296($s3)
#     0x30420001  FFFFFFFF   andi $v0,$v0,1
#     0x104001BD  FFFF0000   beq  $v0,$zero,...   MASKED: branch offset
#     0x00000000  FFFFFFFF   nop
#     0x93820000  FFFF0000   lbu  $v0,disp($gp)   <-- TARGET; MASKED: $gp disp
#     0x10400000  FFFF0000   beq  $v0,$zero,...   MASKED: branch offset
#
#   COLLISION_READER2_PATTERN — Camera::Update (5 words, target = word 1)
#     0xE6000214  FFFFFFFF   swc1 $zero,532($s0)
#     0x93820000  FFFF0000   lbu  $v0,disp($gp)   <-- TARGET; MASKED: $gp disp
#     0x10400000  FFFF0000   beq  $v0,$zero,...   MASKED: branch offset
#     0x00000000  FFFFFFFF   nop
#     0x8E030250  FFFFFFFF   lw   $v1,592($s0)
#
# Measured by exhaustive 4-byte-aligned masked scan: reader 1 matches exactly
# ONCE in SLUS (vaddr 0x157DE4) and ONCE in SLPM (vaddr 0x157FF4); reader 2
# matches exactly ONCE in SLUS (vaddr 0x166090) and ONCE in SLPM (0x166220).
#
# MEASURED VALUES FOR ALL THREE BUILDS (reference only — see below, NOTHING here
# is hardcoded in the patcher):
#                      SLUS_200.17   SLPM_621.27   demo
#   flag vaddr          0x22BD98      0x2332B8     0x25B3B0
#   flag file offset    0x12BE18      0x133338     0x15B430
#   vanilla bytes      01 00 00 00   01 00 00 00  01 00 00 00
#   reader 1 (lbu)      0x157DF4      0x157FF4     0x1380DC
#   reader 2 (lbu)      0x166094      0x166224     0x1467A0
#   _gp                 0x233B70      0x23B070     0x262FF0
#
# THE FLAG ADDRESS IS DERIVED, NOT HARDCODED
# ---------------------------------------------------------------------------
# Deliberately no hardcoded flag offset. Instead:
#   1) locate a reader with the masked signature above,
#   2) decode that `lbu`'s SIGNED 16-bit displacement,
#   3) read `_gp` from the ELF's .reginfo section (ri_gp_value),
#   4) flag_vaddr = gp + disp, and
#      flag_file_off = 0x80 + (flag_vaddr - 0x100000) — the same vaddr -> file
#      mapping the rest of this module uses,
#   5) sanity-check the result lies inside the first PT_LOAD segment.
# That makes the patch build-independent BY CONSTRUCTION. BOTH readers are
# resolved and they MUST agree on the same address; a disagreement is a strong
# signal that a signature matched the wrong place, so the patch refuses with a
# clear reason rather than writing.
#
# WHY THE DATA BYTE AND NOT THE BRANCHES
# ---------------------------------------------------------------------------
# The `beq` instructions are left completely alone. Clearing the developers' own
# data byte is the cleanest and most reversible form: ONE byte, restorable by
# writing 0x01 back, and it is exactly what the original debug build did.
# ============================================================================

COLLISION_READER1_PATTERN = (
    (0x8E620128, 0xFFFFFFFF),   # lw   $v0,296($s3)
    (0x30420001, 0xFFFFFFFF),   # andi $v0,$v0,1
    (0x104001BD, 0xFFFF0000),   # beq  $v0,$zero,...   MASKED: branch offset
    (0x00000000, 0xFFFFFFFF),   # nop
    (0x93820000, 0xFFFF0000),   # lbu  $v0,disp($gp)   <-- TARGET; disp MASKED
    (0x10400000, 0xFFFF0000),   # beq  $v0,$zero,...   MASKED: branch offset
)
COLLISION_READER1_LBU_DELTA = 16

COLLISION_READER2_PATTERN = (
    (0xE6000214, 0xFFFFFFFF),   # swc1 $zero,532($s0)
    (0x93820000, 0xFFFF0000),   # lbu  $v0,disp($gp)   <-- TARGET; disp MASKED
    (0x10400000, 0xFFFF0000),   # beq  $v0,$zero,...   MASKED: branch offset
    (0x00000000, 0xFFFFFFFF),   # nop
    (0x8E030250, 0xFFFFFFFF),   # lw   $v1,592($s0)
)
COLLISION_READER2_LBU_DELTA = 4

COLLISION_FLAG_ENABLED = 0x01           # vanilla: collision on (initialised data)
COLLISION_FLAG_DISABLED = 0x00          # what this patch writes


def _reginfo_gp(data: bytes) -> int | None:
    """`ri_gp_value` from the ELF's .reginfo section, or None.

    MIPS puts the link-time $gp in a SHT_MIPS_REGINFO section (type 0x70000006)
    whose 24-byte payload ends with ri_gp_value at +0x14. Needed because the
    collision flag's address is DERIVED from a `$gp`-relative displacement
    instead of being hardcoded. Follows the header-walking style of
    `_first_load_segment` above — no external ELF library.
    """
    if data[:4] != b"\x7fELF":
        return None
    try:
        e_shoff = struct.unpack_from("<I", data, 0x20)[0]
        e_shentsize = struct.unpack_from("<H", data, 0x2E)[0]
        e_shnum = struct.unpack_from("<H", data, 0x30)[0]
    except struct.error:
        return None
    if not e_shoff or not e_shnum or e_shentsize < 40:
        return None
    for i in range(e_shnum):
        o = e_shoff + i * e_shentsize
        if o + 40 > len(data):
            return None
        sh_type = struct.unpack_from("<I", data, o + 4)[0]
        if sh_type != 0x70000006:            # SHT_MIPS_REGINFO
            continue
        sh_offset, sh_size = struct.unpack_from("<II", data, o + 16)
        if sh_size < 24 or sh_offset + 24 > len(data):
            return None
        return struct.unpack_from("<I", data, sh_offset + 0x14)[0]
    return None


def patch_disable_collision(data: bytearray, enabled: bool) -> dict:
    """Disable PLAYER and CAMERA world collision via `bDoCollision`.

    Falsy `enabled` = total no-op; the buffer is left byte-identical. Mutates
    `data` in place. Returns a stats dict, plus `flag_offset` and `flag_vaddr`.

    !!! DEVELOPER RESEARCH SWITCH — NOT A PLAYABLE MODE. The player falls out of
    the level as soon as gravity applies and retail has no noclip movement to
    compensate; a soft-lock is likely. See the section comment above.

    ONE byte is written: the developers' own `bDoCollision` data byte goes from
    0x01 to 0x00. The two `beq $v0,$zero` readers are NOT touched. The flag's
    address is DERIVED from the matched `lbu`'s signed $gp displacement plus
    `_gp` from .reginfo, so this is build-independent; BOTH readers must be
    located and must resolve to the SAME address before anything is written.
    Re-runs are fine: a byte that already reads 0x00 reports success.
    """
    # 1) Argument check first, so a no-op never searches the buffer.
    if not enabled:
        return {"applied": False, "sites": 0, "flag_offset": None,
                "flag_vaddr": None}
    if data[:4] != b"\x7fELF":
        return {"found": False, "applied": False, "sites": 0,
                "flag_offset": None, "flag_vaddr": None, "reason": "not ELF"}

    buf = bytes(data)
    seg = _first_load_segment(buf)
    if seg is None:
        return {"found": False, "applied": False, "sites": 0,
                "flag_offset": None, "flag_vaddr": None,
                "reason": "no LOAD segment"}
    load_vaddr, load_off, filesz = seg

    gp = _reginfo_gp(buf)
    if gp is None:
        return {"found": False, "applied": False, "sites": 0,
                "flag_offset": None, "flag_vaddr": None,
                "reason": "could not read _gp from the ELF's .reginfo section"}

    # 2) PHASE ONE — locate BOTH readers and derive the flag address from each.
    # Nothing is written in this phase.
    readers = (("player", COLLISION_READER1_PATTERN,
                COLLISION_READER1_LBU_DELTA),
               ("camera", COLLISION_READER2_PATTERN,
                COLLISION_READER2_LBU_DELTA))
    resolved: dict[str, int] = {}       # reader name -> flag vaddr
    lbu_slots: dict[str, int] = {}
    for name, pattern, delta in readers:
        matches = _find_all_masked(buf, pattern)
        if not matches:
            return {"found": False, "applied": False, "sites": 0,
                    "flag_offset": None, "flag_vaddr": None,
                    "reason": f"bDoCollision {name} reader not found"}
        if len(matches) != 1:
            return {"found": True, "applied": False, "sites": 0,
                    "flag_offset": None, "flag_vaddr": None,
                    "reason": f"bDoCollision {name} reader: expected 1 site, "
                              f"found {len(matches)}"}
        lbu_off = matches[0] + delta
        disp = struct.unpack_from("<h", buf, lbu_off)[0]   # SIGNED 16-bit
        resolved[name] = (gp + disp) & 0xFFFFFFFF
        lbu_slots[name] = lbu_off

    # 3) The two readers MUST agree. A disagreement means a signature matched the
    # wrong place, so refuse loudly instead of writing a guessed address.
    if resolved["player"] != resolved["camera"]:
        return {"found": True, "applied": False, "sites": 0,
                "flag_offset": None, "flag_vaddr": None,
                "reason": f"the two bDoCollision readers disagree on the flag "
                          f"address (player 0x{resolved['player']:X}, camera "
                          f"0x{resolved['camera']:X}) — a signature matched the "
                          f"wrong place"}

    flag_vaddr = resolved["player"]
    flag_off = 0x80 + (flag_vaddr - 0x100000)
    # Sanity: the derived byte must live inside the first PT_LOAD segment.
    if not (load_off <= flag_off < load_off + filesz) or flag_off >= len(data):
        return {"found": True, "applied": False, "sites": 0,
                "flag_offset": flag_off, "flag_vaddr": flag_vaddr,
                "reason": f"derived bDoCollision address 0x{flag_vaddr:X} "
                          f"(file 0x{flag_off:X}) is outside the LOAD segment"}

    # 4) Verify the byte. IDEMPOTENCE: already-cleared counts as success.
    cur = data[flag_off]
    if cur == COLLISION_FLAG_DISABLED:
        return {"found": True, "applied": True, "sites": 1,
                "flag_offset": flag_off, "flag_vaddr": flag_vaddr,
                "gp": gp, "readers": lbu_slots, "already_patched": True}
    if cur != COLLISION_FLAG_ENABLED:
        return {"found": True, "applied": False, "sites": 0,
                "flag_offset": flag_off, "flag_vaddr": flag_vaddr,
                "reason": f"expected bDoCollision 0x{COLLISION_FLAG_ENABLED:02X} "
                          f"at 0x{flag_off:X}, found 0x{cur:02X}"}

    # 5) PHASE TWO — the single write. The `beq`s are left untouched by design.
    data[flag_off] = COLLISION_FLAG_DISABLED
    return {"found": True, "applied": True, "sites": 1,
            "flag_offset": flag_off, "flag_vaddr": flag_vaddr,
            "gp": gp, "readers": lbu_slots, "already_patched": False}


# ============================================================================
# Starting inventory (GameVarTbl)
# ============================================================================
# Player::InitVars() seeds the player's 17-entry GameVar array (at player+0x48,
# 4 bytes/var) from a constant table `GameVarTbl`. Each table entry is 6 bytes:
#   [0:2] int16  initial value (what a NEW GAME starts with)
#   [2:4] uint16 max/cap value
#   [4]   u8     "locked/read-only" flag (checked by GameVarSet/Sub)
#   [5]   u8     persist flag
#
# Index meanings were recovered from the demo debug symbols (GameVarSub/Add
# special-cases): #4 = Lives (decremented on death, game-over at 0),
# #5 = Gold/Koins (cap 9999), #7 = Keys (drives UpdateKey). #2 = Health,
# #0/#3 = max/current armor.
#
# ---------------------------------------------------------------------------
# INDEX 2 (HEALTH) IS NOT SEEDED FROM THIS TABLE — A STATIC INITIALIZER
# REWRITES IT AT BOOT
# ---------------------------------------------------------------------------
# GameVarTbl[2] is REWRITTEN AT RUNTIME by a static initializer that runs BEFORE
# Player::InitVars. In the demo build at 0x0020E634:
#     lw  $t7, ARMOR_HEALTH($gp)      ; ARMOR_HEALTH = 4
#     sll $t6, $t7, 1                 ; 4 * 2   <- the 2 is hard-coded vanilla
#                                     ;            starting armor
#     sh  $t6, GameVarTbl[2].initial  ; = 8
#     lui $at, ...
#     sh  $t7, GameVarTbl[2].cap      ; = 4
# InitVars then copies the initials VERBATIM, with NO clamp. Consequences:
#   * Writing index 2's table initial from Python is USELESS — the sinit
#     overwrites it at every boot.
#   * Writing only index 3 (armor) gives the requested number of armor
#     SEGMENTS with vanilla health 8 — the reported "starting armor does not
#     scale starting health" symptom.
# So starting health is changed by rewriting that `sll` word instead (see
# ARMOR_HEALTH_SINIT_PATTERN below), and the armor write is made ATOMIC with it.
#
# KNOWN REMAINING LIMITATION, DELIBERATELY NOT FIXED HERE: `ContinuePlayer`
# separately HARD-CODES GameVar[3] = 2 and GameVar[2] = ARMOR_HEALTH << 1, so
# starting armor/health do NOT survive a continue — after a game over the player
# is back to vanilla 2 segments / 8 health. Fixing that needs a second, separate
# patch site and is out of scope for this change.
#
# The table is byte-identical across DEMO/US/JP; only its file offset shifts,
# so we locate it by a unique structural anchor: entry #5's cap 0x270f (9999)
# followed 6 bytes later by entry #6's cap 0x03e7 (999). We then verify a few
# known caps before writing, and only overwrite the initial-value field of the
# requested vars (clamped to each var's existing cap — caps are left intact so
# the HUD never overflows).
import re as _re

GAMEVAR_TABLE_LEN = 17
GAMEVAR_ENTRY_SIZE = 6
# entry5 = ".. .. 0f 27 .. 01" , entry6 = ".. .. e7 03 .." -> anchor on the two caps
_GAMEVAR_ANCHOR = _re.compile(rb"\x0f\x27.\x01..\xe7\x03", _re.DOTALL)
GAMEVAR_GOLD_INDEX = 5
GAMEVAR_LIVES_INDEX = 4
GAMEVAR_KEYS_INDEX = 7
GAMEVAR_DEATHCOINS_INDEX = 8   # HUD "deathcoins#2" -> code 0x108 (cap 99)
GAMEVAR_SWORD_INDEX = 1        # sword enchant tier (1=Fire 2=Ice 3=Sun 4=Armageddon)
GAMEVAR_SHIELD_INDEX = 9       # elemental shield (1=Wind 2=Magnetic 3=Lightning)
GAMEVAR_SWORDUNITS_INDEX = 11  # sword charge/points (HUD "sword_units", cap 20)
# --- Additional indices recovered by call-site analysis --------------------
# ARMOR = index 3 (initial 2, cap 4). Evidence: GameVarAdd(3, n) reads/writes
#   gPlayer+0x54, calls SetMaximo(ArmorMaximo[new]) + CreateArmorUpFx, and
#   recursively does GameVarAdd(2, ARMOR_HEALTH * delta). The `increase_armor`
#   ability calls GameVarAdd(this, 3, 1). Index 0 is REFUTED as armor: it has
#   no HUD entry and no accessor call site at all — its role is UNKNOWN, so
#   nothing here ever writes it.
# SOULS = index 6 ("spirit", initial 0, cap 999). Evidence: the HUD element
#   table maps the name `spirit` to index 6, and GameVarSub's `index == 6`
#   branch is the only caller of CreateSoulSuckFx.
# SHIELD SEGMENTS = index 14 (initial 2, cap 4) — how many shields Maximo has.
#   Index 10 is the shield POINTS backing the bar (initial 10, cap 20). The
#   vanilla invariant is 10 == 5 * 14, so the two must be written together.
# HEALTH = index 2. Its TABLE CAP IS 0 because the real cap is DYNAMIC
#   (armor x ARMOR_HEALTH_PER_SEGMENT). Never clamp anything against index 2's
#   table cap.
GAMEVAR_ARMOR_INDEX = 3         # armor segments (initial 2, cap 4) — NOT index 0
GAMEVAR_SOULS_INDEX = 6         # souls / HUD "spirit" (initial 0, cap 999)
GAMEVAR_SHIELD_SEGMENTS_INDEX = 14  # shield segment count (initial 2, cap 4)
# BOXERS = index 16. Boxers are NOT an armor level — they are their own
#   GameVar. Evidence (disassembly): GameVars live at player+0x48 + index*4, so
#   index 16 is gPlayer+0x88. GameVarTbl is 17 records of 6 bytes
#   {s16 initial, s16 max, u8 blockedWhileInactive, u8 saved}; record 16 reads
#   `03 00 03 00 00 01` in retail US SLUS_200.17 (max 3, saved). The only
#   consumer is SetMaximo type 2, which forwards the value to
#   EnableOneRenderPassOnly — it picks WHICH render pass (texture) of the
#   armour-less Maximo mesh is drawn, so in the executable this is purely
#   cosmetic.
GAMEVAR_BOXERS_INDEX = 16
ARMOR_HEALTH_PER_SEGMENT = 4    # GameVarAdd(3,d) also does GameVarAdd(2, 4*d)
SHIELD_POINTS_PER_SEGMENT = 5   # vanilla invariant: var10 == 5 * var14

# --- The GameVarTbl[2] static initializer -----------------------------------
# (word, mask) pairs at 4-byte alignment, for `_find_all_masked`. The $gp
# displacement of the `lw`, both `sh` displacements AND their base registers /
# the `lui` half-word all vary by build, so they are MASKED OUT; the invariant
# opcode sequence `lw` / `sll rd,rt,1` / `sh` / `sh` is what pins the site down.
ARMOR_HEALTH_SINIT_PATTERN = (
    (0x8F8F0000, 0xFFFF0000),   # lw  $t7,ARMOR_HEALTH($gp)   disp MASKED
    (0x000F7040, 0xFFFFFFFF),   # sll $t6,$t7,1               <-- TARGET
    (0xA40E0000, 0xFC1F0000),   # sh  $t6,GameVarTbl[2].initial(base) MASKED
    (0x3C000000, 0xFC000000),   # lui $at,hi                       MASKED
    (0xA40F0000, 0xFC1F0000),   # sh  $t7,GameVarTbl[2].cap(base)  MASKED
)
ARMOR_HEALTH_SINIT_SLL_DELTA = 4
ARMOR_HEALTH_SINIT_SLL_EXPECTED = 0x000F7040    # sll $t6,$t7,1  (vanilla)
# IDEMPOTENCE: the same five-word block with the `sll` already replaced by our
# `addiu rd,$zero,<imm>`. rs is pinned to $zero and the immediate + destination
# register are masked, so a re-run recognises any previously written value.
ARMOR_HEALTH_SINIT_PATCHED_PATTERN = (
    ARMOR_HEALTH_SINIT_PATTERN[0],
    (0x24000000, 0xFFE00000),   # addiu rd,$zero,<imm>        (already patched)
    ARMOR_HEALTH_SINIT_PATTERN[2],
    ARMOR_HEALTH_SINIT_PATTERN[3],
    ARMOR_HEALTH_SINIT_PATTERN[4],
)
ARMOR_HEALTH_SINIT_ADDIU_BASE = 0x24000000      # addiu rd,$zero,<imm>
ARMOR_HEALTH_SINIT_IMM_MAX = 32767


def _find_armor_health_sinit(buf: bytes) -> dict:
    """Locate the `sll $t6,$t7,1` that computes GameVarTbl[2]'s starting health.

    Returns {"ok": True, "slot": <file offset of the word>, "rd": <destination
    register number read OUT OF the matched word>, "word": <current word>,
    "site": <offset of the 5-word block>} or {"ok": False, "reason": ...}.
    EXACTLY ONE match is required — anything else is refused rather than guessed.
    """
    for pattern, patched in ((ARMOR_HEALTH_SINIT_PATTERN, False),
                             (ARMOR_HEALTH_SINIT_PATCHED_PATTERN, True)):
        matches = _find_all_masked(buf, pattern)
        if not matches:
            continue
        if len(matches) != 1:
            return {"ok": False, "reason": f"GameVarTbl[2] health static "
                    f"initializer: expected 1 site, found {len(matches)}"}
        site = matches[0]
        slot = site + ARMOR_HEALTH_SINIT_SLL_DELTA
        word = struct.unpack_from("<I", buf, slot)[0]
        # The destination register is read out of the matched word, never
        # hardcoded, so the rewritten `addiu` targets the SAME register.
        rd = ((word >> 11) & 0x1F) if not patched else ((word >> 16) & 0x1F)
        return {"ok": True, "slot": slot, "site": site, "rd": rd,
                "word": word, "already_patched": patched}
    return {"ok": False, "reason": "GameVarTbl[2] health static initializer "
                                   "not found"}


def find_gamevar_table(data: bytearray | bytes) -> int | None:
    """Locate GameVarTbl (starting-inventory table) by structural anchor.
    Returns the file offset of entry #0, or None if not found / not verified."""
    m = _GAMEVAR_ANCHOR.search(bytes(data))
    if not m:
        return None
    tbl = m.start() - GAMEVAR_GOLD_INDEX * GAMEVAR_ENTRY_SIZE - 2
    if tbl < 0 or tbl + GAMEVAR_TABLE_LEN * GAMEVAR_ENTRY_SIZE > len(data):
        return None

    def cap(i: int) -> int:
        o = tbl + i * GAMEVAR_ENTRY_SIZE + 2
        return int.from_bytes(data[o:o + 2], "little")

    # Verify known caps: gold=9999, score=999, lives=99, keys=9.
    if cap(5) == 9999 and cap(6) == 999 and cap(4) == 99 and cap(7) == 9:
        return tbl
    return None


def patch_starting_inventory(data: bytearray, gold: int | None = None,
                             lives: int | None = None,
                             keys: int | None = None,
                             deathcoins: int | None = None,
                             sword_enchant: int | None = None,
                             elemental_shield: int | None = None,
                             souls: int | None = None,
                             armor: int | None = None,
                             shields: int | None = None,
                             boxers: int | None = None) -> dict:
    """Set Maximo's NEW-GAME starting inventory by editing GameVarTbl.

    Any arg left as None is unchanged. `sword_enchant` sets the sword tier
    GameVar #1 (0=normal,1=Fire,2=Ice,3=Sun,4=Armageddon). `elemental_shield`
    sets GameVar #9 (0=none,1=Wind,2=Magnetic,3=Lightning) — the combat element
    read by ApplyGroundEffect/OnHit/ShieldThrowMove (no SetAbility, so no
    freeze). Values are clamped to each var's valid range.

    `souls` sets GameVar #6 (HUD "spirit", cap 999). `armor` sets GameVar #3 —
    ARMOR IS INDEX 3, NOT INDEX 0 (index 0 has no HUD entry and no accessor
    call site; its role is unknown and it is never written here).

    ARMOR ALSO SCALES STARTING HEALTH, and NOT through index 2's table initial:
    a static initializer rewrites GameVarTbl[2] at boot as
    `ARMOR_HEALTH << 1` (vanilla 2 armor -> 8 health) before InitVars copies the
    initials verbatim, so index 2's table initial is NOT the source of starting
    health. Instead the `sll $t6,$t7,1` in that initializer is replaced with
    `addiu rd,$zero,(armor * ARMOR_HEALTH_PER_SEGMENT)` — one word, same slot,
    same destination register (read out of the matched word). An `addiu`
    immediate is used rather than a wider shift because a shift cannot express
    ODD armor counts (3 segments would need x3). The derived value is reported
    as `health_initial` and the rewritten word's offset as `sinit_slot`.

    The armor write is ATOMIC with that rewrite: the initializer is located and
    verified BEFORE anything is written, and if it is missing the armor table
    initial is NOT written either — the call returns `applied: False` rather
    than leaving armor and health inconsistent.

    KNOWN REMAINING LIMITATION (deliberately not fixed here): `ContinuePlayer`
    hard-codes GameVar[3] = 2 and GameVar[2] = ARMOR_HEALTH << 1 of its own, so
    a custom starting armor/health does NOT survive a continue.

    `shields` sets the shield SEGMENT count (GameVar #14, cap 4) and also
    writes shield POINTS (GameVar #10) to shields * SHIELD_POINTS_PER_SEGMENT,
    clamped to #10's own cap, preserving the vanilla `10 == 5 * 14` invariant.
    The derived value is reported as `shield_points`.

    `boxers` sets GameVar #16 (cap 3), which selects which pair of boxers the
    armour-less Maximo mesh is drawn with (SetMaximo type 2 ->
    EnableOneRenderPassOnly). Only the record's initial field is written; its
    max and `saved` flag are left alone.
    """
    targets = {GAMEVAR_GOLD_INDEX: gold,
               GAMEVAR_LIVES_INDEX: lives,
               GAMEVAR_KEYS_INDEX: keys,
               GAMEVAR_DEATHCOINS_INDEX: deathcoins,
               GAMEVAR_SWORD_INDEX: sword_enchant,
               GAMEVAR_SHIELD_INDEX: elemental_shield,
               GAMEVAR_SOULS_INDEX: souls,
               GAMEVAR_ARMOR_INDEX: armor,
               GAMEVAR_SHIELD_SEGMENTS_INDEX: shields,
               GAMEVAR_BOXERS_INDEX: boxers}
    if all(v is None for v in targets.values()):
        return {"applied": False, "reason": "no values requested"}
    tbl = find_gamevar_table(data)
    if tbl is None:
        return {"found": False, "applied": False}
    # PHASE ONE for armor: locate + verify the health static initializer BEFORE
    # writing anything, so armor and starting health are all-or-nothing.
    sinit = None
    health_initial = None
    if armor is not None:
        sinit = _find_armor_health_sinit(bytes(data))
        if not sinit.get("ok"):
            return {"found": True, "applied": False, "table": tbl,
                    "changed": {}, "armor_skipped": True,
                    "reason": "armor skipped because starting health could not "
                              "be made to match it: " + sinit["reason"]}
        a_off = tbl + GAMEVAR_ARMOR_INDEX * GAMEVAR_ENTRY_SIZE
        a_cap = int.from_bytes(data[a_off + 2:a_off + 4], "little")
        armor_v = max(0, min(int(armor), a_cap))
        health_initial = armor_v * ARMOR_HEALTH_PER_SEGMENT
        if not 0 <= health_initial <= ARMOR_HEALTH_SINIT_IMM_MAX:
            return {"found": True, "applied": False, "table": tbl,
                    "changed": {}, "armor_skipped": True,
                    "reason": f"armor skipped: derived starting health "
                              f"{health_initial} does not fit a 16-bit immediate"}
    changed: dict[str, int] = {}
    names = {GAMEVAR_GOLD_INDEX: "gold", GAMEVAR_LIVES_INDEX: "lives",
             GAMEVAR_KEYS_INDEX: "keys", GAMEVAR_DEATHCOINS_INDEX: "deathcoins",
             GAMEVAR_SWORD_INDEX: "sword_enchant",
             GAMEVAR_SHIELD_INDEX: "elemental_shield",
             GAMEVAR_SOULS_INDEX: "souls",
             GAMEVAR_ARMOR_INDEX: "armor",
             GAMEVAR_SHIELD_SEGMENTS_INDEX: "shields",
             GAMEVAR_BOXERS_INDEX: "boxers"}
    for idx, val in targets.items():
        if val is None:
            continue
        off = tbl + idx * GAMEVAR_ENTRY_SIZE
        cap = int.from_bytes(data[off + 2:off + 4], "little")
        if idx == GAMEVAR_SWORD_INDEX:
            cap = min(cap, 4)   # only tiers 0-4 are valid sword enchants
        if idx == GAMEVAR_SHIELD_INDEX:
            cap = min(cap, 3)   # only 0-3 are valid elemental shields
        if idx == GAMEVAR_BOXERS_INDEX:
            cap = min(cap, 3)   # only render passes 0-3 exist
        v = max(0, min(int(val), cap))
        struct.pack_into("<h", data, off, v)
        changed[names[idx]] = v
    # A sword enchant needs charge/points to be usable; SetAbility's enchant
    # path sets sword-units (GameVar #11) to its cap. Mirror that so the
    # enchanted move isn't spent after a single use.
    if sword_enchant is not None and changed.get("sword_enchant", 0) > 0:
        u_off = tbl + GAMEVAR_SWORDUNITS_INDEX * GAMEVAR_ENTRY_SIZE
        u_cap = int.from_bytes(data[u_off + 2:u_off + 4], "little")
        struct.pack_into("<h", data, u_off, u_cap)
        changed["sword_units"] = u_cap
    # Shield segments (#14) and shield points (#10) are one coupled pair in
    # vanilla: #10 == SHIELD_POINTS_PER_SEGMENT * #14. Keep that invariant.
    if shields is not None:
        seg = changed.get("shields", 0)
        p_off = tbl + GAMEVAR_SHIELDUNITS_INDEX * GAMEVAR_ENTRY_SIZE
        p_cap = int.from_bytes(data[p_off + 2:p_off + 4], "little")
        pts = max(0, min(seg * SHIELD_POINTS_PER_SEGMENT, p_cap))
        struct.pack_into("<h", data, p_off, pts)
        changed["shield_points"] = pts
    # PHASE TWO for armor: the one-word rewrite of the health static initializer.
    if sinit is not None:
        struct.pack_into("<I", data, sinit["slot"],
                         ARMOR_HEALTH_SINIT_ADDIU_BASE
                         | ((sinit["rd"] & 0x1F) << 16)
                         | (health_initial & 0xFFFF))
        changed["health_initial"] = health_initial
    result = {"found": True, "applied": bool(changed), "table": tbl,
              "changed": changed}
    if sinit is not None:
        result["health_initial"] = health_initial
        result["sinit_slot"] = sinit["slot"]
    return result


# ============================================================================
# Starting skills (full grant via the icon array + LoadIcons)
# ============================================================================
# Maximo's learned abilities live in two places: the active bit-flags in
# player+0x24, AND the "icon slot" array at player+0x424 (31 slots x 2 bytes:
# [ability id, state]; id 0xff/-1 = empty) that drives the skills MENU. On every
# level start Player::LoadIcons() walks player+0x424 and calls SetAbility(id)
# for each non-empty slot — which does the FULL grant: sets the 0x24 bit (so the
# move works) AND builds the coloured menu icon. Player::InitVars() fills the
# whole 0x424 array with -1 on a new game.
#
# So to grant starting skills properly (move + menu icon) we just write the
# chosen ability IDs into the first 0x424 slots at new-game time and let the
# game's own LoadIcons do the rest. We do this with a tiny code cave:
#   * InitVars tail (located by signature) is: addiu v1,zero,0x12 ; sw v1,0x24(a0)
#     ; jr ra ; sb zero,0xb6(a0).  $a0 is the player throughout InitVars.
#   * We replace the `jr ra` with `j CAVE` (keeping the `sb 0xb6` delay slot), so
#     after the default flags are written we branch to the cave.
#   * The cave does, per selected skill: addiu v1,zero,<id> ; sb v1,0x424+slot*2(a0)
#     then `jr ra ; nop`. $a0 (player) is preserved across the jump.
# The cave is placed in zero padding that follows a NUL-terminated ASCII string
# (string-table padding — loaded, executable on the EE's RWX segment, and never
# used at runtime). Region-independent: hook by signature, cave by scan.
STARTING_SKILLS_SIGNATURE = bytes.fromhex("12000324240083ac")
# name -> ability id (index into the ability descriptor table)
# IMPORTANT: the sword ENCHANTMENTS (ids 15-18, SetAbility handler type -2) call
# a destructive icon-array routine that rewrites 0x424 while LoadIcons is walking
# it -> re-entrant freeze before the map loads. They are EXCLUDED here and are
# instead granted by setting the sword-tier GameVar #1 directly (see
# `sword_enchant` in patch_starting_inventory). Everything below is safe to grant
# from inside LoadIcons (verified in-game).
STARTING_SKILL_IDS = {
    # sword techniques
    "sword720":         1,   # 720-degree spin attack
    "double_slash":     2,   # double-sword slash
    "mighty_blow":      3,   # mighty sword blow
    "masquerade":       5,   # masquerade
    "sword_power":      0,   # increase sword power
    "projectile":       4,   # throwing projectile
    # shields (flag-based, safe to grant via LoadIcons; the ELEMENTAL shields
    # are NOT here — their type -3 SetAbility re-enters the icon array and
    # freezes LoadIcons, so they're applied via GameVar #9 instead, see
    # `elemental_shield` in patch_starting_inventory)
    "return_shield":    6,   # throw / return shield
    "hover_shield":     11,
    # armor
    "increase_armor":   14,
    # throw / misc
    "wide_shockwave":   7,
    "damage_shockwave": 8,
    "find_treasure":    9,
    "smart_bomb":       10,
    "increase_throw":   23,
    # FOOT CHEESE IS NOT HERE — see STARTING_SKILL_FLAG_ONLY below.
}

# Skills granted by the player+0x24 BIT ALONE, with NO entry in the player+0x424
# icon array. name -> the 0x24 bit.
#
# WHY foot cheese lives here and MUST NEVER be written into the icon array:
# ability id 31 is NOT a plain flag path. `SetAbility__6PlayerFib` ORs the
# ability's bitmask into player+0x24 and THEN switches on the ability id, and id
# 31 has its own case (demo 0x0013493C) that does
#   lw this->obj / lw obj->[0] / lw $t9,0($a0) / lw $t9,0x20($t9) / jalr $t9
# — an UNGUARDED virtual call — followed by SetAction(this,108,false),
# `sw 1 -> player+0x110` and two float scale writes to obj+0xE0/+0xE8. Every
# other case null-checks that chain first; case 31 checks NOTHING. At InitVars
# time the object chain is not set up yet, so SetAbility(31) dereferences
# garbage and HANGS THE BOOT.
#
# The bit alone is the correct mechanism, and it is also what VANILLA does:
# LoadIcons' tail (demo 0x0013119C) reads a cheat word, tests 0x00400000, ORs it
# into player+0x24 and then calls StartLeft/RightFootCheeseFx. No icon-array
# entry is involved anywhere. Every consumer — SetInvisible, SetMaximo,
# ApplyGroundEffect, GetGroundAction, OnHit, JumpMove — reads player+0x24
# directly.
STARTING_SKILL_FLAG_ONLY: dict[str, int] = {
    "foot_cheese": 0x00400000,
}

# Backwards-compat alias (older callers referenced *_MASKS for the KEY SET only,
# never for the values), so the flag-only skills are folded in to keep them
# selectable.
STARTING_SKILL_MASKS = {**STARTING_SKILL_IDS, **STARTING_SKILL_FLAG_ONLY}

# The player+0x24 ability bit for each FLAG ability (0 = not a flag ability, i.e.
# granted purely via SetAbility GameVars: armor/elemental shields). Move
# animations are loaded early in InitPlayer from these 0x24 bits, so we set them
# in the cave BEFORE LoadIcons runs — otherwise the move works but has no anim.
STARTING_SKILL_FLAGS = {
    "sword720":         0x00000100,
    "double_slash":     0x00001000,
    "mighty_blow":      0x00002000,
    "masquerade":       0x00004000,
    "sword_power":      0x01000000,
    "projectile":       0x00800000,
    "return_shield":    0x00010000,
    "hover_shield":     0x00020000,
    "wide_shockwave":   0x00080000,
    "damage_shockwave": 0x00100000,
    "find_treasure":    0x00200000,
    "smart_bomb":       0x40000000,
    "increase_throw":   0x00040000,
    # Foot cheese: 0x00400000 in the flags word at gPlayer+0x24 (SetInvisible ->
    # EnableFootCheese, SetMaximo -> Start*FootCheeseFx both test it). This bit
    # is the WHOLE grant — foot cheese has no icon-array entry, exactly as
    # vanilla grants it in LoadIcons' cheat tail.
    # CORRECTION: the earlier claim here — that ability 31's descriptor field +0
    # is "a positive bitmask, so it uses the safe AddIcon tail" — WAS WRONG, and
    # it hung the boot. The descriptor bit is positive, but id 31 still has its
    # own SetAbility case that makes an UNGUARDED virtual call through
    # this->obj->[0] (invalid at InitVars time) before touching any icon. See
    # STARTING_SKILL_FLAG_ONLY. The bit must be OR'd and nothing else.
    "foot_cheese":      0x00400000,
    # armor / elemental shields have no 0x24 flag (state lives in GameVars)
    "increase_armor":   0,
    "wind_shield":      0,
    "magnetic_shield":  0,
    "lightning_shield": 0,
}

_JR_RA = 0x03e00008
_SB_ZERO_B6_A0 = 0xa08000b6


def _first_load_segment(data: bytes):
    """Return (vaddr, file_off, filesz) of the first PT_LOAD segment, or None."""
    if data[:4] != b"\x7fELF":
        return None
    e_phoff = struct.unpack_from("<I", data, 0x1c)[0]
    e_phnum = struct.unpack_from("<H", data, 0x2c)[0]
    e_phentsize = struct.unpack_from("<H", data, 0x2a)[0]
    for i in range(e_phnum):
        o = e_phoff + i * e_phentsize
        p_type, p_off, p_vaddr, p_paddr, p_filesz = struct.unpack_from("<IIIII", data, o)
        if p_type == 1:
            return p_vaddr, p_off, p_filesz
    return None


def _find_string_padding_cave(data: bytes, load_vaddr: int, load_off: int,
                              filesz: int, need: int) -> int | None:
    """Find a zero-run >= `need` bytes that follows a NUL-terminated ASCII
    string within the loaded image (string-table padding). Returns the cave
    FILE offset (4-aligned, a few bytes past the string terminator)."""
    end = min(load_off + filesz, len(data))
    for m in re.finditer(rb"\x00{%d,}" % (need + 8), data[load_off:end]):
        run_start = load_off + m.start()
        # the bytes immediately before the run should be printable ASCII (the
        # string body); the run's leading zeros are its NUL terminator + padding.
        k = run_start - 1
        ascii_len = 0
        while k > load_off and 0x20 <= data[k] < 0x7f:
            k -= 1
            ascii_len += 1
        if ascii_len >= 4:
            # leave the string terminator intact; start a few bytes in, 4-aligned
            cave = (run_start + 4) & ~3
            if cave + need <= run_start + len(m.group()):
                return cave
    return None


def patch_starting_skills(data: bytearray, skills) -> dict:
    """Grant Maximo new-game starting skills (move + skills-menu icon) by
    writing their ability IDs into player+0x424 via a code cave hooked at
    Player::InitVars. `skills` is an iterable of names from STARTING_SKILL_IDS
    or STARTING_SKILL_FLAG_ONLY; the latter get their player+0x24 bit OR'd and
    are deliberately kept OUT of the icon array (see STARTING_SKILL_FLAG_ONLY —
    an icon-array entry for foot cheese hangs the boot).
    Mutates `data` in place. No-op (graceful) if the site/cave isn't found."""
    sel = [s for s in (skills or ())
           if s in STARTING_SKILL_IDS or s in STARTING_SKILL_FLAG_ONLY]
    if not sel:
        return {"applied": False, "reason": "no skills requested"}
    if data[:4] != b"\x7fELF":
        return {"found": False, "applied": False, "reason": "not ELF"}
    seg = _first_load_segment(bytes(data))
    if seg is None:
        return {"found": False, "applied": False, "reason": "no LOAD seg"}
    load_vaddr, load_off, filesz = seg

    sig = bytes(data).find(STARTING_SKILLS_SIGNATURE)
    if sig < 0:
        return {"found": False, "applied": False, "reason": "InitVars not found"}
    jr_off = sig + 8       # the `jr ra`
    if struct.unpack_from("<I", data, jr_off)[0] != _JR_RA:
        return {"found": True, "applied": False, "reason": "jr ra not at +8"}
    if struct.unpack_from("<I", data, jr_off + 4)[0] != _SB_ZERO_B6_A0:
        return {"found": True, "applied": False, "reason": "sb 0xb6 not at +12"}

    # Build the cave. First (if any flag abilities are selected) OR their
    # 0x24 ability bits in EARLY so InitPlayer loads the move animations:
    #   lw v1,0x24(a0) ; lui v0,HI ; ori v0,v0,LO ; or v1,v1,v0 ; sw v1,0x24(a0)
    # Then per skill write the icon id:  addiu v1,zero,id ; sb v1,off(a0)
    # End: jr ra ; nop.   ($a0 = player throughout InitVars.)
    # Icon slots come ONLY from the id-bearing skills; flag-only skills get the
    # bit and nothing more.
    ids = [STARTING_SKILL_IDS[s] for s in sel if s in STARTING_SKILL_IDS]
    flag_only = [s for s in sel if s not in STARTING_SKILL_IDS]
    combined = 0
    for s in sel:
        combined |= STARTING_SKILL_FLAGS.get(s, 0)
    cave_words = []
    if combined:
        cave_words.append(0x8c830024)                        # lw  v1,0x24(a0)
        cave_words.append(0x3c020000 | ((combined >> 16) & 0xffff))  # lui v0,HI
        cave_words.append(0x34420000 | (combined & 0xffff))  # ori v0,v0,LO
        cave_words.append(0x00621825)                        # or  v1,v1,v0
        cave_words.append(0xac830024)                        # sw  v1,0x24(a0)
    for slot, aid in enumerate(ids):
        off = 0x424 + slot * 2
        cave_words.append(0x24030000 | (aid & 0xffff))      # addiu v1,zero,id
        cave_words.append(0xa0830000 | (off & 0xffff))      # sb v1,off(a0)
    # Set player+0x462 = 1 so the first LoadIcons PROCESSES the icon array (on a
    # new game it's 0, so the grant is otherwise skipped until a death runs
    # ResetAbility). SAFE because the icon list now contains only abilities whose
    # SetAbility uses the in-place AddIcon tail — the re-entrant type -2/-3
    # abilities (sword enchants, elemental shields) are applied via GameVars.
    # Only emitted when icon slots were actually written — with an empty icon
    # array there is nothing for LoadIcons to process.
    if ids:
        cave_words.append(0x24030001)                        # addiu v1,zero,1
        cave_words.append(0xa0830462)                        # sb v1,0x462(a0)
    cave_words.append(_JR_RA)                                # jr ra
    cave_words.append(0x00000000)                            # nop (delay)
    need = len(cave_words) * 4

    cave_off = _find_string_padding_cave(bytes(data), load_vaddr, load_off,
                                         filesz, need)
    if cave_off is None or cave_off + need > len(data):
        return {"found": True, "applied": False, "reason": "no cave"}
    # Ensure the cave region is actually all-zero where we write.
    if any(data[cave_off:cave_off + need]):
        return {"found": True, "applied": False, "reason": "cave not clear"}

    cave_vaddr = load_vaddr + (cave_off - load_off)
    # write the cave
    for i, w in enumerate(cave_words):
        struct.pack_into("<I", data, cave_off + i * 4, w)
    # hook: replace `jr ra` with `j cave`
    j_instr = 0x08000000 | ((cave_vaddr >> 2) & 0x03ffffff)
    struct.pack_into("<I", data, jr_off, j_instr)
    return {"found": True, "applied": True, "skills": sel,
            "flag_only": flag_only,
            "cave_vaddr": cave_vaddr, "cave_bytes": need,
            "slots": len(ids)}


# ============================================================================
# LEVEL TIME LIMIT — kill Maximo after N seconds inside a level (code cave)
# ============================================================================
# EXPERIMENTAL. Two hooks + one code cave (the size is COMPUTED, never fixed —
# it depends on the on-screen countdown being enabled). After the selected
# world's share of level
# time the cave calls the game's own damage entry point with a lethal amount, so
# the death runs through the ENTIRE vanilla death path (death camera, lives
# decrement, StopMusic, SetDeath) — nothing about dying is reimplemented here.
#
# ---------------------------------------------------------------------------
# HOOK 1 — the per-frame tick
# ---------------------------------------------------------------------------
# `Player::ControlPlayer` contains an INLINE underwater-timer block. We hook the
# instruction immediately AFTER that block's `jal CheckTimer`:
#   US vaddr 0x00157D28 (file 0x57DA8) / JP vaddr 0x00157F28 (file 0x57FA8).
# The word there is `lwc1 $f2,0x3c($s0)` (0xC602003C); we replace it with
# `j CAVE_TICK`. Its delay slot (`mtc1 $zero,$f1`, 0x44800800) still executes,
# the cave re-executes the displaced `lwc1` in its own return delay slot, and
# control resumes at HOOK+8.
#
# WHY `CheckTimer` ITSELF WAS REJECTED: CheckTimer is a shared generic helper —
# every status effect, powerup and script timer funnels through it. Hooking it
# would fire in menus and for non-player timers and gives no reliable handle on
# the Player object. The inline site gives us `$s0` = the Player, for free.
#
# WHY THE REGISTER USE IS PROVABLY SAFE: the site sits immediately after a
# `jal`, so by the o32/EABI calling convention anything still live across that
# call must already be in `$s*`/`$sp`/`$gp`. `$at`, `$v0`-`$v1`, `$a0`-`$a3`,
# `$t0`-`$t9` and `$ra` are therefore all dead at that point. `$ra` in
# particular is spilled in the prologue (`sq $31,128($sp)`) and reloaded at the
# single epilogue, so the cave MAY use `jal`. The cave clobbers only $at, $v0,
# $a0-$a3 and $ra.
#
# ---------------------------------------------------------------------------
# HOOK 2 — the per-level reset
# ---------------------------------------------------------------------------
# The tail of `Player::PlaceAtStart` (entry US 0x00158BF0 / JP 0x00158DD0). Its
# `jr $ra` at entry+0x90 is replaced with `j CAVE_RESET`; the delay slot
# (`addiu $sp,$sp,48`) still runs, so `$sp` is already restored on cave entry,
# and the cave ends with the `jr $ra` it displaced. `$v0`/`$v1`/`$at` are free:
# PlaceAtStart is void and both callers discard the result.
#
# PlaceAtStart has exactly TWO callers per build — `mglNextLife` (respawn) and
# `mglRunLevel` (level entry), with `a0` = gPlayer in both. So this one hook
# rearms the clock on level entry, hub transition, level restart AND respawn:
# the limit is PER ATTEMPT, which is the intended behaviour.
#
# ---------------------------------------------------------------------------
# THE KILL GATE — what actually stops the clock killing you after the level
# ---------------------------------------------------------------------------
# HOOKING THE LEVEL-END EVENT FAILED IN PLAY TWICE. Both stop hooks (HOOK 3 and
# HOOK 4 below) are applied, verified per build and provably in the right
# functions, and the clock still ran on through the end-of-level sequence: the
# column's script path lives in the PSX data, not in the executable, so the
# executable-side events we can reach are not the ones that fire.
#
# SO THE CLOCK IS NO LONGER STOPPED AT ALL. It is left running, deliberately: it
# keeps counting and the countdown keeps drawing (and may reach 0 and read 0
# during the victory sequence). ONLY THE KILL IS DISABLED once the level is over.
# That single change removes the whole reason the stop was fragile — there is no
# longer anything for `PlaceAtStart`'s unconditional re-arm to undo, so the kill
# gate needs no re-arm gate, no latch and no second write.
#
# THE GATE SITS IN THE TICK'S KILL CHECK, and nowhere else: immediately before
# the existing `bgtz T,done` the tick jumps to `_lt_build_state_block`, which
# either jumps straight back (kill path unchanged, byte for byte) or, when the
# level is over, jumps to the cave's own `done` — exactly the way the pause test
# short-circuits. The decrement, the -1 latch, the store to `T` and the whole
# display block are untouched, and the gate performs NO store of any kind.
#
# WHAT "THE LEVEL IS OVER" MEANS HERE, and what it does NOT: the player's own
# EXT-FUNCTION QUEUE holds a record whose type is the level-end column's ext
# function (LEVEL_TIMER_COLUMN_EXT_FUNCTION), read with the count/record/stride
# displacements taken out of the S6 match. That is player-side state, tested
# every frame, so no event has to fire for it to work. It is NOT `Player+0x8`
# bit 5: that halfword is the NEWLY PRESSED PAD BUTTON mask
# (`ReadJoystick__6PlayerFi`, demo 0x00130434: `cur & (cur ^ prev)` from
# Player+0x4 / Player+0x6 stored with `sh $v1,8($s1)`), and the level-column case
# waiting on it is waiting for a BUTTON PRESS. Gating the kill on that bit would
# have suppressed the kill for one frame whenever that button was pressed
# anywhere in the game and would have done nothing about the level being over.
# The full proof, and the one thing that stays INFERRED (that the column's script
# re-queues its ext function every frame it waits), is in
# `_lt_build_state_block`.
#
# REGISTERS: the gate uses `$v0`-free code — `$at`, `$v1` and `$a0` only — all
# already used freely by the tick, which sits immediately after a `jal`, so it
# adds NO new liveness assumption whatsoever.
#
# THE TWO STOP HOOKS AND THE RE-ARM GATE ARE STILL APPLIED, unchanged. They are
# harmless (storing the disarmed value 0 is idempotent and can only make the
# clock kill LESS) and they are kept because they are proven, cheap and would
# still be correct in a build where they do fire. They are simply no longer what
# this feature relies on.
#
# THAT UNCONDITIONAL RE-ARM IS WHY THE STOP USED TO LOOK BROKEN. PlaceAtStart
# runs on every level AND sub-level entry — including the transition the column
# break itself starts, and including the hub — so both stop hooks zeroed the
# countdown word correctly and this cave armed it again microseconds later. In
# play that read as: breaking the end-of-level column only froze the clock for
# the victory animation, and the clock then ran (and could kill) in the hub. The
# fix is the RE-ARM GATE (`_lt_build_end_block` / `_lt_build_reset_block`): the
# stop hooks latch the row they stopped into the gate word `G`, and this cave
# refuses to arm while `G` equals the row being entered, clearing `G` back to
# LEVEL_TIMER_GATE_NONE as soon as a DIFFERENT row arms. `G` is written ONLY by
# the stop hooks, so death — which goes SetDeath -> mglNextLife -> PlaceAtStart
# and never through a stop — still re-arms exactly as it always did.
#
# ---------------------------------------------------------------------------
# DIFFICULTY PRESETS — PER-LEVEL-SLOT durations, selected AT RUNTIME
# ---------------------------------------------------------------------------
# There is no single "seconds" value any more. Three presets (hard / medium /
# easy) each carry TWELVE durations — the six world/area limits plus SIX
# dedicated BOSS limits — and the reset cave PICKS one every time it runs, so a
# single patched executable gives every slot its own limit:
#
#              grave  swamp  ice    under  castle  Dungeon of Despair
#   hard       2:00   2:10   2:20   2:30   3:15    4:15
#   medium     2:15   2:30   2:35   2:45   3:30    4:30
#   easy       2:30   2:45   2:55   3:00   3:45    4:45
#
#              grave_boss swamp_boss ice_boss under_boss king   queen
#   hard       0:45       0:55       0:55     0:55       1:15   1:15
#   medium     0:55       1:05       1:05     1:05       1:25   1:25
#   easy       1:00       1:10       1:10     1:10       1:30   1:30
#
# THE BOSS LIMITS ARE DELIBERATELY MUCH SHORTER THAN THE AREA LIMITS. A boss
# arena is one scripted fight with no exploration, so the same 2-4 minutes that
# a level needs would never expire; 45 s to 1:30 is what makes the clock matter
# there. Each boss slot carries 15 s more than the original tuning, which was
# too tight to finish the fight in.
#
# The times live in `LEVEL_TIMER_PRESETS` as MM:SS strings and the tick counts
# are COMPUTED from them with LEVEL_TIMER_TICKS_PER_SECOND, so the table stays
# auditable against what was asked for — no tick constant is written twice.
# Every value is asserted to fit a 16-bit immediate (1..32767).
#
# THE TIMER ARMS ON EVERY LEVEL ENTRY, INTROS AND BOSS ARENAS INCLUDED, WITH
# EXACTLY TWO EXCEPTIONS:
#
#   * THE HUBS. Every hub row maps to a 13th "no timer" value-table slot whose
#     value is 0 — the tick's existing DISARMED value — so the reset cave arms
#     nothing there. The hub rows are NOT hardcoded: they are the rows whose
#     `+0x00` PSX path filename contains `_HUB` (case-insensitive), read out of
#     the located LevelTable exactly the way LEVEL_TIMER_ROW_EXPECT validates the
#     boss rows, and a build where no hub row can be identified REFUSES the
#     patch. INTRO rows stay timed — they are real levels.
#   * THE ROW WHOSE END SEQUENCE IS RUNNING — the re-arm gate, see STOP below.
#
# Nothing else is gated by level type.
#
# HOW THE SELECTION WORKS — PURELY ROW-INDEXED, one global, DERIVED:
#
#   `giLevel` — the LevelTable ROW index (4 bytes, $gp-relative). Its
#   displacement comes from signature SIG_B (US 0x00146C28, JP 0x00146D28,
#   one hit each), which ALSO materialises the `LevelTable` base as a
#   lui/addiu pair: that base is cross-checked against `find_level_table`'s own
#   structural result and a mismatch REFUSES the patch.
#
# The cave does NOT look at the world index any more. It bounds-checks the row
# (`sltiu $at,$v1,31`), reads ONE BYTE out of a 31-byte ROW -> SLOT map
# (`LEVEL_TIMER_ROW_SLOTS`) and uses it to index the 12-word value table; an
# out-of-range row falls back to slot 0 (grave). That is two table lookups and
# no compares, and adding a per-boss limit is a data edit — the previous
# world-lookup form would have needed six more compare/branch pairs.
#
# THE ROW MAP (6 rows per world: INTRO / HUB / SUB1 / SUB2 / SUB3-or-INTROB /
# BOSS), with the boss rows called out:
#
#   rows 0-4   -> grave        row 5  -> grave_boss   (G_BOSS)
#   rows 6-10  -> swamp        row 11 -> swamp_boss   (S_BOSS)
#   rows 12-16 -> ice          row 17 -> ice_boss     (I_BOSS)
#   rows 18-22 -> under        row 23 -> under_boss   (U_BOSS)
#   rows 24-26 -> castle       row 27 -> dungeon      (C_SUB2, Dungeon of
#   row  28    -> king         (C_KING)                Despair)
#   row  29    -> queen        (C_QUEEN)
#   row  30    -> grave        (G_BONUS)
#
# THOSE ROW INDICES ARE VERIFIED AGAINST THE IMAGE, NOT ASSUMED. They were
# inferred from the 6-rows-per-world pattern, so before anything is written the
# patch reads each significant row's `+0x00` PSX path out of the located
# LevelTable and asserts the filename matches (rows 5/11/17/23 -> G_BOSS /
# S_BOSS / I_BOSS / U_BOSS, row 27 -> C_SUB2, row 28 -> C_KING, row 29 ->
# C_QUEEN), and that the table has at least 30 rows. A mismatch REFUSES the
# patch naming the row, the expected name and what was actually found, so a
# differently ordered build can never silently arm the wrong timer.
#
# WHY THE ROW INDEX IS THE RIGHT KEY: the packed identity at +0x28 cannot
# discriminate the Dungeon of Despair — rows 26 (C_SUB1) and 27 (C_SUB2) both
# hold 0x00040819 — and world 5 covers both castle bosses. The row index
# separates every case on its own. Cross-checked two ways: the 30-entry HUD
# title table maps 1:1 onto LevelTable rows and index 27 resolves to the string
# "Dungeon of Despair"; and row 27's path is PSXDATA\CASTLE\C_SUB2.PSX,
# matching this project's own note in items.py.
#
# `giLevelID` (the WORLD index, from signature SIG_A, the ICE gate in
# `Player::CheckTimer`) is NO LONGER USED to select anything. It is still
# derived, deliberately, purely as a BUILD SANITY CHECK: SIG_A must still match
# exactly once, which keeps that one-hit property under test on every build and
# keeps the reported `levelid_disp` in the CLI's log line. Nothing branches on
# its value.
#
# WHY READING `giLevel` AT PlaceAtStart IS SAFE (verified): it is read by the
# level-runner idiom before the level runs, and PlaceAtStart is only reachable
# from inside that call, so the row index is live and correct there. `$gp` is
# live through PlaceAtStart — it makes two `jal`s and never saves, reloads or
# writes `$gp` — so the global is one $gp-relative `lw`, with the displacement
# half-word copied VERBATIM from the signature match. `$gp`'s value is never
# needed, and there is nothing to relocate.
#
# ---------------------------------------------------------------------------
# THE KILL
# ---------------------------------------------------------------------------
# `GameVarSub(this, 2, 999, 0)` — GameVar #2 is health. Its address is DERIVED,
# never hardcoded: DAMAGE_HIT_SIGNATURE matches exactly once per image and the
# word at sig+16 is the `jal` to GameVarSub, which we decode (US 0x00151A30,
# JP 0x00151C50) after asserting it really is a `jal` and that the target lands
# inside the first PT_LOAD segment. `a3 = 0` is exactly what the vanilla damage
# site passes, so it is the provably safe argument.
#
# ---------------------------------------------------------------------------
# STORAGE, FAIL-SAFE AND THE RETRY LOOP
# ---------------------------------------------------------------------------
# The countdown lives in a reserved word at the END of the tick cave (not in a
# Player field and not in BSS — that deliberately avoids any unprovable "this
# word is unused" claim). It boots as 0, and the tick treats 0 as DISARMED, so
# if the reset hook somehow never runs the feature does nothing at all.
# On reaching zero the counter latches -1 and the cave RETRIES the kill every
# frame while health > 0. That is deliberate: GameVarSub's index-2 arm has its
# own early-outs (player+0x84, player+0x486, player+0x24 & 2) and the kill can
# silently no-op, so a fire-once design could fail to kill. Do not "optimise".
#
# HEALTH GUARD: the cave reads health at Player+0x50 (the GameVar array is
# player+0x48 and health is index 2) and only fires while health > 0, so it can
# never double-kill. This displacement is INFERRED, not proven — but the read is
# READ-ONLY and guards only WHEN the kill fires, never what gets written, so a
# wrong offset can never corrupt anything.
#
# ---------------------------------------------------------------------------
# THE ON-SCREEN COUNTDOWN (show_timer=True — the default)
# ---------------------------------------------------------------------------
# The same cave also DRAWS the remaining seconds, reading the SAME storage word
# the kill logic reads, so the number on screen and the death time cannot drift.
# The per-slot presets changed ONLY the reset cave (which value it selects); the
# tick cave, the display block and the kill check are untouched and keep reading
# the one shared word `T`, so they stay synced by construction.
# `show_timer=False` emits the kill-only cave unchanged.
#
# API: `DarkEngine::AddString(char* text, float x, float y, float scale, int,
# int, int)` — `this` in $a0, text in $a1, x/y/scale in $f12/$f13/$f14, the three
# ints in $a2/$a3/$t0 (the 7th set in the `jal` delay slot), returning the new
# string object in $v0. The LAYER is NOT an argument: it is assigned afterwards
# with `sh <layer>,6(<returned object>)` (CreateString defaults it to 0).
# `CreateString` calls strlen/new[] and converts the text to glyph geometry
# INSIDE the call, so the source char* is never retained — one static scratch
# buffer in the cave is safe to overwrite every time.
#
# LAYER 40 is provably free. The complete set of layer ids used anywhere in
# retail is {0,1,10,11,12,20,21,22}, confirmed across all 72 `RemoveLayer` call
# sites (all compile-time constants), every `sh <reg>,6(<obj>)` assignment and
# the layer byte of the 299-record string-descriptor table. The layer is a pure
# REMOVAL TAG — never an array index, never range-checked, no rendering effect.
#
# DRAW ON CHANGE ONLY. Strings PERSIST until removed, and vanilla itself only
# redraws when the displayed integer changes. Drawing every frame would `new` a
# string object plus a text buffer 60x/second into a growing PArray — an
# unbounded leak. The cave keeps the last displayed value in `L` and skips the
# entire draw when it is unchanged.
#
# VANILLA'S CALL ORDER IS REPLICATED EXACTLY: AddString -> save $v0 ->
# RemoveLayer(40) -> `sh 40,6(saved)`. Create the new string FIRST, then remove
# the old one (which is still the only object carrying the tag), then tag the
# new one. Any other order either removes the string it just made or leaves two
# alive. `$v0` is spilled to the cave word `S` across the RemoveLayer call
# because nothing but memory survives a call here.
#
# NO TEARDOWN HOOK IS NEEDED. `RemoveAllLayer` (US 0x001721E0) is unconditional
# — it destroys EVERY string regardless of layer — and is reached on level entry,
# level exit, death, respawn, hub transition and game over via mglRunLevel /
# InitWorld / DarkEngine::ResetGame (Player::SetDeath additionally calls
# RemoveLayer(gEng,12) directly). Pause does NOT bulk-clear, so a layer-40
# string stays visible while paused. Accepted.
#
# WHY CALLING THESE FROM THE CAVE IS SAFE: `CheckTimer` makes these exact calls,
# and its single call site is 8 bytes BEFORE our hook (US 0x00157D20; the hook is
# 0x00157D28). We call the same functions from the same point of the same frame
# as shipped code, with $ra already dead (spilled in ControlPlayer's prologue).
#
# ADDRESSES ARE DERIVED, NOT HARDCODED: signature S3 (LEVEL_TIMER_HUD_SIGNATURE)
# pins the vanilla form-countdown block (exactly one hit per build), and fixed
# deltas off that match give `jal sprintf` (+0x58), `jal AddString` (+0x8C),
# `jal RemoveLayer` (+0xA0) and the lui/addiu pair materialising `this` = gEng
# (+0x84/+0x88). Each `jal` is asserted to really be a `jal` whose target lands
# inside the first PT_LOAD segment; gEng is decoded with the sign-extended low
# half, range-checked, and cross-checked against the known build map — a
# mismatch REFUSES the patch instead of guessing.
#
# FORMATTING matches vanilla: format "%d" (our own 4-byte copy in the cave, not
# the engine's) and the value printed is seconds + 1, so a 30 s timer reads 30 on
# the first second and 1 on the last; an expired/latched timer reads 0.
#
# POSITION is x=+160.0, y=+160.0, scale 1.0, encoded as float bit patterns via
# lui+mtc1. The space is centred-origin with y up, mirroring vanilla's
# (-240,+160). Confidence in the exact placement is MODERATE — but a wrong
# position cannot crash anything, so the half-words live in named constants
# (LEVEL_TIMER_DISPLAY_X_LUI etc.) and are safe to iterate on visually.
#
# ---------------------------------------------------------------------------
# CAVE ALLOCATION — real collision with patch_starting_skills
# ---------------------------------------------------------------------------
# The largest usable zero run in both builds is the 569 bytes following the
# string "MAXIMO3.ICO" (US file 0xCE62F / JP 0xD100F). `patch_starting_skills`
# first-fits into that SAME run (US 0xCE630), so calling the head-allocating
# finder again would hand back an occupied offset and bail. This feature
# therefore allocates from the TAIL of the run instead:
#   cave_off = (run_start + run_len - need) & ~3
# clear of the largest possible skills cave, so the two features can be enabled
# in either order. `_find_string_padding_cave` is left untouched (other code
# depends on its head-allocating behaviour).
#
# THE CAVE IS THREE BLOCKS, IN THREE DIFFERENT RUNS (both cave shapes, kill-only
# and display). Nothing in this feature needs to be contiguous beyond each
# block: every cross-block reference is an absolute `j` or an absolute lui/lo
# pair. The split is not cosmetic — with the re-arm gate's two words and the
# "no timer" slot the contiguous form is 416 bytes (kill-only) / 540 bytes
# (display), and the largest padding run in either retail build is 569 bytes of
# which 176 are reserved for the head-allocating features. The blocks are:
#   A  tick + countdown draw + kill check + pause (+ the countdown word T in the
#      kill-only shape)                                    128 / 340 bytes
#   B  the reset block + the two stop blocks               212 / 228 bytes
#   C  the data words: value table, row map, gate word G, spill word V, armed-row
#      word R (plus T/L/S/B/F in the display shape)         96 / 120 bytes
# `_lt_alloc_blocks` places B and C in runs no head allocation can reach and in
# a different run from A and from each other.
#
# ---------------------------------------------------------------------------
# PAUSE (chest / gate) AND STOP (level-end column)
# ---------------------------------------------------------------------------
# PAUSE. The tick reads the CURRENT ACTION ID at Player+0x115 — one `lbu` off
# `$s0`, which is already the Player at the hook — and if it is one of the
# scripted-object-interaction actions it jumps straight to the cave's `done`.
# Skipping the frame is the whole mechanism: the stored word is not written at
# all, so the remaining time is preserved EXACTLY (never reset, never extended)
# and the countdown on screen simply holds its number.
#
# WHAT IS PROVEN about the action set (LEVEL_TIMER_PAUSE_ACTIONS = 72, 73, 77,
# 90, 91, 92):
#   * Player+0x115 is the current action id. `Player::SetAction`'s tail is
#     `sb <id>,0x115(this)` / `sw <entry>,0x10C(this)`; `Player::ContinuePlayer`
#     clears it; `Player::ControlPlayer` itself branches on it
#     (`lbu $v1,0x115($s0)` then compares 0x2F / 0x30). Signature S4 pins this
#     in every build we patch.
#   * Those six ids are exactly the ids `Player::ProcessExtFunction` sets for a
#     scripted interaction with an object: each of the six sites stores the
#     object into Player+0x410 and then calls SetAction with that literal id.
#     Verified present, unique and calling the same SetAction in BOTH retail
#     builds and in the demo debug build.
#   * Chest and gate opening are necessarily inside that set: the handlers for
#     them (`Player::OpenChestMove`, `UnlockChestMove`, `UnlockGateMove`,
#     `LiftGateMove`) are among the handlers that read Player+0x410, and
#     ProcessExtFunction is the ONLY writer of that field in the whole image.
# WHAT IS NOT PROVEN: which individual id is which interaction. The id -> move
# handler mapping is built at runtime and is not in the file, so the set is a
# verified SUPERSET: it certainly contains chest-open and gate-open, and it also
# contains the sibling object interactions (grab-object and friends). The
# gold-paying interaction (id 81, the one that compares Player+0x5C against the
# script parameter) is deliberately EXCLUDED. Pausing during a sibling scripted
# interaction is a behaviour choice, not a failure mode: the clock cannot stall
# (these actions are one-shot animation-driven and every action change goes
# through SetAction) and it cannot fire at the wrong moment.
#
# STOP. TWO hooks, both storing 0 into the countdown word from the SAME builder
# (`_lt_build_end_block`). 0 is the DISARMED value the tick tests first, so a
# stopped clock cannot be misread as expired and can never reach the kill —
# which is also why storing it twice is idempotent. The next level re-arms
# through the `PlaceAtStart` reset cave either way.
#
# THE STOP IS DURABLE ONLY BECAUSE OF THE RE-ARM GATE. Both stop blocks also
# latch the row into the gate word `G` (spilling `$v1` through the word `V` so
# they provably clobber nothing at either site), and the reset cave will not arm
# a row that equals `G`. Without that, PlaceAtStart re-armed the clock
# immediately after the stop zeroed it — that, not the choice of hook, is the
# actual reason the stop appeared to do nothing. `G` starts at
# LEVEL_TIMER_GATE_NONE (-1), which no row index can equal, and is cleared back
# to it by the first DIFFERENT row that arms. Only a stop ever writes `G`.
#
# THE STOP BLOCKS DELIBERATELY DO NOT READ `$gp`, AND THAT IS THE POINT. An
# earlier form of the gate had both stops read the row themselves with
# `lw $v1,D_LV($gp)`. That silently extended the "$gp is live here" claim — which
# was only ever justified for `Player::PlaceAtStart` — to `mglEnterSubLevel` and
# to the middle of `Player::SetExtFunction`. If it did not hold at either site
# the stop latched a garbage row, the reset cave's `beq` never matched,
# PlaceAtStart re-armed, and the clock stopped for a few frames and then RESUMED
# — the exact symptom reported from play, and one that no word-level simulation
# could ever catch, because the simulation supplies `$gp` for itself.
#
# Disassembly says `$gp` is in fact a fixed global register in all three builds
# (the only definition reachable in code is `or $gp,$a0,$zero` in the boot stub
# at 0x00100050, and `.reginfo` carries the fixed `gp_value` 0x00262FF0 in the
# demo), that `mglEnterSubLevel` addresses ALL FOUR of its globals through `$gp`
# directly, and that `Player::SetExtFunction` neither saves, reloads nor writes
# it. So the old form very probably worked. It is still gone: the dependency is
# now REMOVED rather than relied upon, because the cost of removing it is two
# instructions and the cost of being wrong about it is this bug.
#
# HOW: the RESET cave — the one site where `$gp` is proven live, and which
# already holds the row in a register — PUBLISHES the row it armed into a data
# word `R`. Both stop blocks then latch the gate with a plain `R` -> `G` copy
# built from absolute lui/lo pairs only. `R` starts at LEVEL_TIMER_GATE_NONE, so
# a stop that somehow fires before any arm latches the sentinel and blocks
# nothing. Everything else is unchanged: `G` starts at the sentinel, only a stop
# writes `G`, the reset cave refuses to arm a row equal to `G` and clears `G`
# otherwise, and death (which never runs a stop) still re-arms.
#
#   HOOK 4 — THE EARLY STOP, and the one that actually matters. Fires when the
#   End_Of_Sublevel_Column (PSX entity type 0x4D) is BROKEN, by hooking
#   `Player::SetExtFunction` (signature S6) and testing the queued ext-function
#   type against 12, the level-column case. This is the earliest point in the
#   executable that unambiguously means "the level is over"; the ordered call
#   chain and everything that is verified per build (the type register, the
#   `bne` it belongs to, and type 12 itself against this build's own
#   ProcessExtFunction case table) is written out in the S6 banner below.
#
#   HOOK 3 — the LATE stop, kept as a second, idempotent stop. On
#   `mglEnterSubLevel` (signature S5), which the column's ext-function case
#   reaches only at the END of the victory sequence — it waits every frame for
#   the player's flags bit 5 first — so on its own it left the clock running (and
#   able to kill) all the way through the level-end animation. That was the bug.
#   The script API's own EnterSubLevel is its other caller, so a scripted
#   sub-level transition still stops the clock, which is the same intent.
#
# NEITHER hook can fire for other smashables, for a hit that does not destroy the
# column, or for the hub `Level_Column` (PSX type 0x59, a different entity that
# transitions through CScriptApi::TransportHubLevel and never reaches ext
# function 12 or mglEnterSubLevel).
#
# WHAT WAS REJECTED, and why:
#   * `gPlayer+0x24 & 2` as a "player inactive" gate. The bit reads the other way
#     round — `SetAction` and `GameVarSet/Add/Sub` all REQUIRE it to be set
#     (`andi $v0,$v1,2` / `bne $v0,$zero,<continue>`), so it is an "player is
#     live" enable flag, not an interaction signal, and it is not touched by the
#     chest/gate paths.
#   * `Player+0x378` (miLevelColumn, written by `SetLevelColumn`) as the
#     level-complete signal. It is the spawn/exit column index, written on level
#     ENTRY paths too (EnterSubLevel, TransportHubLevel, MaximoMain) and read
#     back by `RestoreColumnPosition` from `mglRunLevel`. It would have stopped
#     the clock at the start of a level.
#
# ---------------------------------------------------------------------------
# UNPROVEN RISKS (all accepted, all stated plainly)
# ---------------------------------------------------------------------------
#  1. The padding run follows a filename string and has the shape of a
#     `char path[]` buffer. It is never observed to be written, but it cannot be
#     PROVEN that nothing writes it at runtime. If something did, the cave would
#     be corrupted mid-level.
#  2. `ControlPlayer` may keep running while the game is paused or during
#     cutscenes/inventory, so the clock may tick then. The limit is wall-ish
#     level time, not strictly "time in control of Maximo".
#  3. The pause set is a verified superset of chest/gate (see above), so a
#     couple of sibling scripted interactions pause the clock as well.
# ---------------------------------------------------------------------------

# Signature S1 — the underwater-timer tail inside ControlPlayer. Masked because
# the two `lwc1`/`pmove` base registers are allocation-dependent and the `bc1t`
# carries a build-varying displacement. Measured: exactly ONE match per build.
LEVEL_TIMER_TICK_PATTERN = (
    (0xC602003C, 0xFFE0FFFF),   # lwc1 $f2,0x3c($s0)   (base reg masked)
    (0x44800800, 0xFFFFFFFF),   # mtc1 $zero,$f1
    (0x00000000, 0xFFFFFFFF),   # nop
    (0x46011032, 0xFFFFFFFF),   # c.eq.s $f2,$f1
    (0x00000000, 0xFFFFFFFF),   # nop
    (0x4501000C, 0xFFFF0000),   # bc1t  ...            (displacement masked)
    (0x72002628, 0xFFE0FFFF),   # pmove $a0,$s0,$zero  (source reg masked)
)
# The word the tick hook displaces, re-executed in the cave's return delay slot.
LEVEL_TIMER_TICK_DISPLACED = 0xC602003C     # lwc1 $f2,0x3c($s0)

# Signature S2 — the head of Player::PlaceAtStart. No masks needed; measured
# exactly ONE match per build.
LEVEL_TIMER_RESET_SIGNATURE = b"".join(struct.pack("<I", w) for w in (
    0x27BDFFD0, 0x44806000, 0x7FBF0020, 0x7FB10010, 0x7FB00000,
    0x70A08628, 0x8C850000, 0x78830470, 0x24020001, 0x70808E28,
))
LEVEL_TIMER_RESET_JR_DELTA = 0x90           # entry -> the `jr $ra` we replace
LEVEL_TIMER_RESET_JR = _JR_RA               # 0x03E00008
LEVEL_TIMER_RESET_DELAY = 0x27BD0030        # addiu $sp,$sp,48 (its delay slot)

# ---------------------------------------------------------------------------
# PAUSE — signature S4 and the action-id test
# ---------------------------------------------------------------------------
# Signature S4 — the TAIL of Player::SetAction, which is what proves the two
# Player displacements this feature reads:
#   `sb <id>,0x115(this)`  -> Player+0x115 is the CURRENT ACTION ID (a byte)
#   `sw <entry>,0x10C(this)` -> Player+0x10C is the current action-table entry
# S4 is not hooked; it is required to match EXACTLY ONCE purely as proof that
# 0x115 means what the pause test assumes in THIS build. Measured: one hit in
# each retail build (US file 0x5596C, JP file 0x55B8C).
LEVEL_TIMER_SETACTION_TAIL_SIGNATURE = b"".join(struct.pack("<I", w) for w in (
    0xA2930115,     # sb    $s3,0x115($s4)   <- current action id
    0x24020001,     # addiu $v0,$zero,1
    0xAE820110,     # sw    $v0,0x110($s4)
    0xAE90010C,     # sw    $s0,0x10C($s4)   <- current action entry
    0xA2800114,     # sb    $zero,0x114($s4)
))
LEVEL_TIMER_ACTION_ID_OFFSET = 0x115        # Player+0x115, byte

# The action ids Player::ProcessExtFunction starts for a SCRIPTED OBJECT
# INTERACTION. Each of these six sites stores the interacted object into
# Player+0x410 and immediately calls SetAction with the literal id below, and
# each id appears EXACTLY ONCE per build (verified in both retail images and in
# the demo debug build, all calling the same SetAction). Chest opening and gate
# opening are necessarily inside this set — the chest/gate move handlers
# (Player::OpenChestMove / UnlockChestMove / UnlockGateMove / LiftGateMove) are
# exactly the handlers that read Player+0x410, and ProcessExtFunction is the
# only writer of that field. See the banner for what is proven and what is not.
LEVEL_TIMER_PAUSE_ACTIONS = (72, 73, 77, 90, 91, 92)
LEVEL_TIMER_PAUSE_BASE = min(LEVEL_TIMER_PAUSE_ACTIONS)
LEVEL_TIMER_PAUSE_SPAN = max(LEVEL_TIMER_PAUSE_ACTIONS) - LEVEL_TIMER_PAUSE_BASE + 1
LEVEL_TIMER_PAUSE_MASK = 0
for _a in LEVEL_TIMER_PAUSE_ACTIONS:
    LEVEL_TIMER_PAUSE_MASK |= 1 << (_a - LEVEL_TIMER_PAUSE_BASE)
del _a
assert LEVEL_TIMER_PAUSE_SPAN <= 32          # the mask is one 32-bit word
LEVEL_TIMER_PAUSE_WORDS = 10

# ---------------------------------------------------------------------------
# STOP — signature S5, the sub-level-complete hook (`mglEnterSubLevel`)
# ---------------------------------------------------------------------------
# `mglEnterSubLevel(int)` is the whole "this sub-level is over, go to the next
# one" event: an 8-instruction leaf that writes three $gp globals (state 6,
# state 4 and the next sub-level index). Its TWO callers are the script API's
# EnterSubLevel and the ProcessExtFunction case for the End_Of_Sublevel_Column
# (which calls it and then Player::SetLevelColumn) — i.e. breaking the level-end
# column lands here. We replace its `jr $ra` (entry+0x18) with `j CAVE_END`; the
# delay slot (`sw $v1,<next sublevel>($gp)`) still runs, so the engine's own
# work is untouched, and the cave stores 0 into the countdown word and returns.
# ZERO IS THE DISARMED VALUE, not "expired": the tick's first test is
# `beq T,$zero,done`, so a zeroed clock can never reach the kill path. The four
# $gp displacements are build-varying and therefore masked out of the match.
# Measured: exactly ONE hit per build (US file 0x47250, JP file 0x47350).
LEVEL_TIMER_END_PATTERN = (
    (0x24030006, 0xFFFFFFFF),   # addiu $v1,$zero,6
    (0x8F850000, 0xFFFF0000),   # lw    $a1,disp($gp)          (disp MASKED)
    (0xAF830000, 0xFFFF0000),   # sw    $v1,disp($gp)          (disp MASKED)
    (0x24030004, 0xFFFFFFFF),   # addiu $v1,$zero,4
    (0xAF830000, 0xFFFF0000),   # sw    $v1,disp($gp)          (disp MASKED)
    (0x00A41821, 0xFFFFFFFF),   # addu  $v1,$a1,$a0
    (0x03E00008, 0xFFFFFFFF),   # jr    $ra              <- the word we replace
    (0xAF830000, 0xFFFF0000),   # sw    $v1,disp($gp)    (its delay slot)
)
LEVEL_TIMER_END_JR_DELTA = 0x18             # entry -> the `jr $ra` we replace
# spill/restore pair + the R->G copy (two absolute lui/lo pairs, NO `$gp`) + the
# T store + jr/nop. See `_lt_build_end_block`: the stop LATCHES the row the reset
# cave last armed (the data word R) into the re-arm gate word G, which is what
# makes the stop survive PlaceAtStart.
LEVEL_TIMER_END_WORDS = 12

# ---------------------------------------------------------------------------
# STOP — signature S6, the COLUMN-BROKEN hook (`Player::SetExtFunction`)
# ---------------------------------------------------------------------------
# THE REAL "level over" moment, and the reason this hook exists: hook 3 above
# fires at the END of the victory sequence, not when the column breaks, so the
# clock kept running (and could still kill) all the way through the level-end
# animation. The ordered chain, traced in the demo debug build's own symbols:
#
#   End_Of_Sublevel_Column (PSX entity type 0x4D) is smashed
#     -> its VM script's event handler runs (script side, in the PSX data — the
#        entity type NEVER appears in the executable, so there is nothing
#        type-based to hook)
#     -> CScriptApi::SetPlayerExtFunction            (demo 0x00119D10)
#     -> Player::SetExtFunction(type, param, object) (demo 0x001369D0)
#          QUEUES a record: count at Player+0x388, records at Player+0x38C,
#          8 bytes each — `sb type,0x38C(p)` / `sh param,0x38E(p)` /
#          `sw object,0x390(p)`.                     <-- WE HOOK HERE
#     -> Player::ProcessExtFunction                  (demo 0x00136AF0), once per
#          frame from ControlPlayer, switches on the queued type through a
#          13-entry jump table and, for the column's type, WAITS every frame
#          until Player+0x8 has bit 5 set. Player+0x8 is NOT a flags field: it is
#          the NEWLY PRESSED PAD BUTTONS halfword (`ReadJoystick` computes
#          `cur & (cur ^ prev)` from Player+0x4 / Player+0x6 and stores it there,
#          demo 0x00130434-0x00130444), so that wait is a BUTTON PRESS — which is
#          what makes the transition land at the end of the victory sequence
#     -> mglEnterSubLevel + Player::SetLevelColumn   (hook 3, the late stop)
#
# So `SetExtFunction(type = 12)` is the EARLIEST point in the executable that
# unambiguously means "the level-end column was destroyed, the level is over".
# It cannot fire for a hit that does not destroy the column (the script only
# calls the API on its destroy event), it cannot fire for other smashables (no
# other script calls this ext function type), and it is unrelated to the hub
# `Level_Column` (type 0x59), which transitions through
# CScriptApi::TransportHubLevel -> App::TransportHubLevel instead and never
# reaches this case. Sub-level transitions driven by a script's own
# EnterSubLevel still stop the clock through hook 3, which is the same intent.
#
# TYPE 12 IS VERIFIED PER BUILD, NOT ASSUMED. `_lt_verify_column_case` finds the
# jump table structurally: a 13-word table of in-image code pointers whose
# entry 12 is the case body that calls `mglEnterSubLevel` (whose address is
# itself derived from signature S5) and whose entries 0/5/9/10/11 are all the
# same default label. Exactly ONE such table exists per build (demo 0x002094B0,
# US 0x001DCDA0, JP 0x001E4720) — a build that laid the cases out differently
# would refuse to patch instead of stopping the clock on the wrong event.
#
# THE HOOK ITSELF replaces word 0 of S6, `addiu <v>,$zero,11` (the constant the
# following `bne` compares the type against), with `j CAVE_COLUMN`. Its delay
# slot — `addiu <a>,<c>,1`, the incremented queue count — still runs, and the
# cave re-executes the displaced `addiu <v>,$zero,11` in its own return delay
# slot, so the engine's own work is byte-for-byte unchanged. The type is read
# out of `$s2` — DERIVED from the `sb`'s rt field in the match, never assumed —
# and the only register the cave touches is `$at`, the assembler temporary,
# which is dead here (its last use is the queue-full `slti`/`beq` pair well
# before the hook). Measured: exactly ONE hit per build, with identical register
# allocation in all three (US file 0x568D8 -> vaddr 0x00156858, JP file 0x56AE8
# -> vaddr 0x00156A68, demo file 0x36AD8 -> vaddr 0x00136A58).
LEVEL_TIMER_COLUMN_PATTERN = (
    (0x2400000B, 0xFFE0FFFF),   # 0 addiu <v>,$zero,11   <- the word we replace
    (0x24000001, 0xFC00FFFF),   # 1 addiu <a>,<c>,1      (its delay slot)
    (0xAC000388, 0xFC00FFFF),   # 2 sw    <a>,0x388(<p>) (new queue count)
    (0x000000C0, 0xFFE007FF),   # 3 sll   <a>,<c>,3      (record stride 8)
    (0x00000021, 0xFC0007FF),   # 4 addu  <a>,<p>,<a>
    (0xA000038C, 0xFC00FFFF),   # 5 sb    <type>,0x38C(<a>)  <- type register
    (0xA400038E, 0xFC00FFFF),   # 6 sh    <param>,0x38E(<a>)
    (0x14000000, 0xFC000000),   # 7 bne   <type>,<v>,...
    (0xAC000390, 0xFC00FFFF),   # 8 sw    <object>,0x390(<a>)
)
LEVEL_TIMER_COLUMN_HOOK_INDEX = 0       # word of the match we replace
LEVEL_TIMER_COLUMN_TYPE_INDEX = 5       # the `sb` whose rt field IS the type
LEVEL_TIMER_COLUMN_EXT_FUNCTION = 12    # ext-function type of the level column
# The QUEUE LAYOUT the per-frame level-over test reads, taken out of the SAME S6
# match rather than written down here: word 2's displacement is the queue COUNT
# field, word 5's displacement is the first record's TYPE byte, and word 3's
# shift amount is the record STRIDE (`sll <a>,<c>,3` -> 8). All three fields are
# INSIDE the pattern's mask, so a match already pins them; they are re-read and
# re-checked anyway so nothing about the layout is hardcoded twice.
LEVEL_TIMER_COLUMN_COUNT_INDEX = 2      # `sw <a>,0x388(<p>)`  -> queue count
LEVEL_TIMER_COLUMN_RECORD_INDEX = 5     # `sb <type>,0x38C(<a>)` -> record type
LEVEL_TIMER_COLUMN_STRIDE_INDEX = 3     # `sll <a>,<c>,3`      -> record stride
LEVEL_TIMER_COLUMN_CASES = 13           # entries in the ProcessExtFunction table
# Jump-table entries that must all hold the same "nothing to do" default label;
# used to identify the table structurally (see `_lt_verify_column_case`).
LEVEL_TIMER_COLUMN_DEFAULT_CASES = (0, 5, 9, 10, 11)
# How far back from a `jal mglEnterSubLevel` the case body can start.
LEVEL_TIMER_COLUMN_CASE_LOOKBACK = 8
LEVEL_TIMER_COLUMN_WORDS = 14           # guard(2) + stop body(10) + j/delay

# ---------------------------------------------------------------------------
# THE PER-FRAME KILL GATE — no hook, no event, and the clock is left alone
# ---------------------------------------------------------------------------
# See the banner section "THE KILL GATE". The tick reaches this block from its
# KILL CHECK — and from nowhere else — and it asks the PLAYER'S OWN MEMORY
# whether the level-end column's ext function is queued, every single frame:
#   count = Player[EXT COUNT]; if (count <= 0 || count > SCAN_MAX) -> kill path
#   for i in 0..count-1: if (Player[EXT RECORD + i*STRIDE] == 12) -> `done`
# `done` is the tick's own return-to-game label, so a level that is over simply
# never kills. NOTHING else is touched: T is not stored, the latch is not
# changed and the countdown keeps counting and keeps drawing, which is exactly
# why this needs no re-arm gate and cannot fight `PlaceAtStart`.
# The scan bound is a DEFENSIVE CLAMP on a count this patch does not write, not a
# claim about the engine: `Player::SetExtFunction` refuses to queue a 17th record
# (`slti $at,<count>,0x10`), so a count outside 1..16 cannot be a real queue and
# the test skips the scan instead of walking arbitrary memory.
LEVEL_TIMER_EXT_QUEUE_SCAN_MAX = 16
LEVEL_TIMER_STATE_WORDS = 15

LEVEL_TIMER_TICKS_PER_SECOND = 60           # ControlPlayer runs at 60 Hz
# 34 = the 32-word tick body + the 2-word `j KILL GATE` pair that now sits
# inside the KILL CHECK (it used to be a prologue at word 0 that disarmed the
# clock; the count is unchanged, the pair only moved).
LEVEL_TIMER_TICK_WORDS = 34
LEVEL_TIMER_STORAGE_WORD_INDEX = 33         # tick word 34 -> block index 33
LEVEL_TIMER_RESET_WORDS = 27                # the kill-only reset cave (gate +
#                                             the R publish + lookup; 30 with
#                                             the L invalidate)
LEVEL_TIMER_VALUE_TABLE_WORDS = 13          # 6 areas + 6 boss slots + "none"
# The ROW -> SLOT byte map, padded up to whole words so the cave stays word
# aligned (31 bytes -> 8 words; the trailing byte is unused padding).
LEVEL_TIMER_ROW_MAP_WORDS = 8
# Three extra data words the re-arm gate needs:
#   G  the row whose end sequence is running (LEVEL_TIMER_GATE_NONE when no stop
#      is latched). Written ONLY by a stop.
#   V  a one-word spill slot, so the stop blocks can borrow a second register at
#      sites where only `$at` is provably dead.
#   R  the row the RESET cave last armed. Published by the reset cave (the one
#      site where `$gp` is proven live) and copied into G by the stops, which is
#      what lets the stop blocks latch the gate WITHOUT reading `$gp` at all.
#      Initialised to LEVEL_TIMER_GATE_NONE so a stop that somehow fires before
#      any arm cannot latch a real row.
LEVEL_TIMER_GATE_WORDS = 3
LEVEL_TIMER_CAVE_WORDS = (LEVEL_TIMER_TICK_WORDS + LEVEL_TIMER_RESET_WORDS
                          + LEVEL_TIMER_END_WORDS
                          + LEVEL_TIMER_COLUMN_WORDS
                          + LEVEL_TIMER_VALUE_TABLE_WORDS
                          + LEVEL_TIMER_ROW_MAP_WORDS
                          + LEVEL_TIMER_GATE_WORDS
                          + LEVEL_TIMER_STATE_WORDS)
# The kill-only cave is allocated as THREE independent blocks, exactly like the
# display cave, because the re-arm gate word, the spill word and the "no timer"
# slot pushed the contiguous form (LEVEL_TIMER_CAVE_WORDS = 104 words, 416
# bytes) past the largest padding run either retail build has. Every block is
# reached by an absolute `j` or an absolute lui/lo pair, so none of them has to
# follow another:
#   A  the tick block (which also holds the countdown word T)
#   B  the reset + end + column blocks
#   C  the value table, the row map and the three gate words (G, V, R)
LEVEL_TIMER_BLOCK_B_WORDS = (LEVEL_TIMER_RESET_WORDS + LEVEL_TIMER_END_WORDS
                             + LEVEL_TIMER_COLUMN_WORDS)
# Block C is the data words FOLLOWED BY the kill gate. The gate is code, not
# data, and it lives here for a MEASURED reason: block B's run is the 258-byte
# one in both retail builds (250 usable) and B is already 212/228 bytes, so the
# 60-byte gate does not fit behind it, while block C's run is 256 bytes (248
# usable) and the data words are only 96/120 of them. Every reference into this
# block is an absolute `j` or lui/lo pair, so it does not matter which run it is
# in — see the allocation section of the banner.
LEVEL_TIMER_BLOCK_C_DATA_WORDS = (LEVEL_TIMER_VALUE_TABLE_WORDS
                                  + LEVEL_TIMER_ROW_MAP_WORDS
                                  + LEVEL_TIMER_GATE_WORDS)
LEVEL_TIMER_BLOCK_C_WORDS = (LEVEL_TIMER_BLOCK_C_DATA_WORDS
                             + LEVEL_TIMER_STATE_WORDS)
assert (LEVEL_TIMER_TICK_WORDS + LEVEL_TIMER_BLOCK_B_WORDS
        + LEVEL_TIMER_BLOCK_C_WORDS) == LEVEL_TIMER_CAVE_WORDS

# --- DIFFICULTY PRESETS ----------------------------------------------------
# Slot order IS the value table's order, and the index the row map holds: the
# six area slots first (so their indices never moved), then the six boss slots.
LEVEL_TIMER_NO_TIMER_SLOT = "none"      # the "no timer here" slot (value 0)
LEVEL_TIMER_WORLD_SLOTS = ("grave", "swamp", "ice", "under", "castle",
                           "dungeon",
                           "grave_boss", "swamp_boss", "ice_boss",
                           "under_boss", "king", "queen",
                           # LAST, so no existing slot index moved. Its value is
                           # 0 — the SAME disarmed value the tick's first test
                           # (`beq T,$zero,done`) already means, so a row mapped
                           # here arms nothing and can never expire or kill.
                           LEVEL_TIMER_NO_TIMER_SLOT)
# MM:SS per slot, exactly as requested. Ticks are computed from these (see
# `_lt_preset_ticks`) so there is a single source of truth for every duration.
# Boss limits stay well below the area limits — a boss arena is one scripted
# fight with no exploration, so a level-sized limit there would never expire.
# They each carry 15 seconds more than the original tuning to leave room for the
# fight itself, while still keeping the clock a real threat (45 s to 1:30).
LEVEL_TIMER_PRESETS = {
    "hard":   {"grave": "2:00", "swamp": "2:10", "ice": "2:20",
               "under": "2:30", "castle": "3:15", "dungeon": "4:15",
               "grave_boss": "0:45", "swamp_boss": "0:55",
               "ice_boss": "0:55", "under_boss": "0:55",
               "king": "1:15", "queen": "1:15"},
    "medium": {"grave": "2:15", "swamp": "2:30", "ice": "2:35",
               "under": "2:45", "castle": "3:30", "dungeon": "4:30",
               "grave_boss": "0:55", "swamp_boss": "1:05",
               "ice_boss": "1:05", "under_boss": "1:05",
               "king": "1:25", "queen": "1:25"},
    "easy":   {"grave": "2:30", "swamp": "2:45", "ice": "2:55",
               "under": "3:00", "castle": "3:45", "dungeon": "4:45",
               "grave_boss": "1:00", "swamp_boss": "1:10",
               "ice_boss": "1:10", "under_boss": "1:10",
               "king": "1:30", "queen": "1:30"},
}
LEVEL_TIMER_PRESET_NAMES = tuple(LEVEL_TIMER_PRESETS)   # insertion-ordered
# "off" and friends: an explicit, total no-op (see `resolve_level_timer`).
LEVEL_TIMER_OFF_ALIASES = ("", "off", "none", "vanilla", "no", "disabled")
# The cave arms `T` with `lw`-loaded table words, but every value must still fit
# a signed 16-bit immediate so the table stays interchangeable with the old
# lui/ori form and can never be misread as negative (= expired/latched).
LEVEL_TIMER_TICK_MIN = 1
LEVEL_TIMER_TICK_MAX = 32767

# Runtime selection constants (see the banner).
LEVEL_TIMER_DUNGEON_ROW = 27            # giLevel row for Dungeon of Despair
# The ROW -> SLOT map, one entry per LevelTable row, written as SLOT NAMES so it
# reads like the layout it encodes; `_lt_row_map_bytes` turns the names into the
# byte indices the cave loads, so no index is ever written twice. 6 rows per
# world: INTRO / HUB / SUB1 / SUB2 / SUB3-or-INTROB / BOSS.
LEVEL_TIMER_ROW_SLOTS = (
    "grave", "grave", "grave", "grave", "grave", "grave_boss",       # 0-5
    "swamp", "swamp", "swamp", "swamp", "swamp", "swamp_boss",       # 6-11
    "ice", "ice", "ice", "ice", "ice", "ice_boss",                   # 12-17
    "under", "under", "under", "under", "under", "under_boss",       # 18-23
    "castle", "castle", "castle",                                   # 24-26
    "dungeon",                                                      # 27
    "king",                                                         # 28
    "queen",                                                        # 29
    "grave",                                                        # 30 G_BONUS
)
LEVEL_TIMER_ROW_COUNT = len(LEVEL_TIMER_ROW_SLOTS)          # 31
# The row indices above were INFERRED from the 6-rows-per-world pattern, so they
# are CHECKED against the image before patching: each of these rows' `+0x00` PSX
# path must contain this filename (case-insensitive) or the patch is refused.
LEVEL_TIMER_ROW_EXPECT = {
    5: "G_BOSS", 11: "S_BOSS", 17: "I_BOSS", 23: "U_BOSS",
    27: "C_SUB2", 28: "C_KING", 29: "C_QUEEN",
}
LEVEL_TIMER_MIN_ROWS = 30               # refuse to index a shorter table
# The RE-ARM GATE's empty value. `giLevel` is a non-negative row index (the cave
# bounds-checks it against LEVEL_TIMER_ROW_COUNT with `sltiu`), so -1 can never
# equal a row — the same sentinel discipline `L` (the last displayed value) uses.
LEVEL_TIMER_GATE_NONE = 0xFFFFFFFF
# Marker string used to identify hub rows in the image's own LevelTable paths.
LEVEL_TIMER_HUB_MARKER = "_HUB"

# SIG_A — the ICE gate inside `Player::CheckTimer`. Word 0's low half-word IS
# the `giLevelID` $gp displacement (US -> 0x0022C2B0, JP -> 0x002337B0).
# Measured EXACTLY ONE hit per retail build (US 0x001575D0, JP 0x001577E0).
# NOTE: selection is purely row-indexed now, so `giLevelID` is NOT read by the
# cave. This signature is kept ON PURPOSE as a BUILD SANITY CHECK — it must
# still match exactly once — and its displacement is still reported in the log.
LEVEL_TIMER_LEVELID_PATTERN = (
    (0x8F840000, 0xFFFF0000),   # lw    $a0,D_ID($gp)     <-- displacement here
    (0x24030002, 0xFFFFFFFF),   # addiu $v1,$zero,2       (world 2 = ice)
    (0x14830000, 0xFFFF0000),   # bne   $a0,$v1,<skip>    (displacement masked)
)
# SIG_B — the level-runner idiom that indexes `LevelTable` with `giLevel`.
# Word 0's low half-word IS the `giLevel` $gp displacement; words 4 and 9 are the
# lui/addiu pair materialising the LevelTable base, used as an independent
# cross-check against `find_level_table`. EXACTLY ONE hit per retail build
# (US 0x00146C28, JP 0x00146D28).
LEVEL_TIMER_LEVELROW_PATTERN = (
    (0x8F850000, 0xFFFF0000),   #  0 lw    $a1,D_LV($gp)  <-- displacement here
    (0x24020001, 0xFFFFFFFF),   #  1 addiu $v0,$zero,1
    (0x3C010000, 0xFFFF0000),   #  2 lui   $at,...
    (0xA0220000, 0xFFFF0000),   #  3 sb    $v0,...($at)
    (0x3C02001D, 0xFFFFFFFF),   #  4 lui   $v0,0x001D    (LevelTable HI)
    (0x00051840, 0xFFFFFFFF),   #  5 sll   $v1,$a1,1
    (0x00651821, 0xFFFFFFFF),   #  6 addu  $v1,$v1,$a1
    (0x00031880, 0xFFFFFFFF),   #  7 sll   $v1,$v1,2
    (0x00651821, 0xFFFFFFFF),   #  8 addu  $v1,$v1,$a1
    (0x24420000, 0xFFFF0000),   #  9 addiu $v0,$v0,LO(LevelTable)
    (0x00031880, 0xFFFFFFFF),   # 10 sll   $v1,$v1,2
    (0x00432021, 0xFFFFFFFF),   # 11 addu  $a0,$v0,$v1
)
LEVEL_TIMER_LEVELTABLE_HI_INDEX = 4
LEVEL_TIMER_LEVELTABLE_LO_INDEX = 9
_LT_OPCODE_J = 2                            # MIPS `j`   (0x08000000 >> 26)
_LT_OPCODE_JAL = 3                          # MIPS `jal` (0x0C000000 >> 26)
_LT_OPCODE_LUI = 0x0F
_LT_OPCODE_ADDIU = 0x09

# --- ON-SCREEN COUNTDOWN (show_timer=True) ---------------------------------
# Layer 40: the complete set of layer ids retail uses anywhere is
# {0,1,10,11,12,20,21,22} (all 72 RemoveLayer call sites take compile-time
# constants, every `sh <reg>,6(<obj>)` layer assignment, and the layer byte of
# the 299-record string-descriptor table). The layer is a pure REMOVAL TAG — it
# is never an array index, never range-checked and has no rendering effect — so
# 40 is provably free and cannot collide with anything the game removes.
LEVEL_TIMER_DISPLAY_LAYER = 40
# Position/scale as raw `lui` half-words (float bit patterns, low half zero).
# Coordinate space is centred-origin with y up; (+160,+160) mirrors vanilla's
# (-240,+160). MODERATE confidence, and a wrong position cannot crash anything,
# so these are named to make visual iteration a one-line change.
LEVEL_TIMER_DISPLAY_X_LUI = 0x4320          # +160.0f
LEVEL_TIMER_DISPLAY_Y_LUI = 0x4320          # +160.0f
LEVEL_TIMER_DISPLAY_SCALE_LUI = 0x3F80      # 1.0f
LEVEL_TIMER_DISPLAY_OFFSET = 1              # vanilla prints seconds + 1
LEVEL_TIMER_DISPLAY_FORMAT = b"%d\x00\x00"  # 4 bytes, our own "%d" (see banner)

# Signature S3 — the vanilla form-countdown HUD block. Exact bytes (no masked
# fields); measured EXACTLY ONE hit per retail build (SLUS vaddr 0x00157508 /
# file 0x57588, SLPM vaddr 0x00157718 / file 0x57798). Every address the display
# needs is DERIVED from fixed deltas off this match — nothing is hardcoded.
LEVEL_TIMER_HUD_SIGNATURE = b"".join(struct.pack("<I", w) for w in (
    0x922400B6,     # lbu   $a0,0xB6($s1)
    0x24030005,     # addiu $v1,$zero,5
    0x10830006,     # beq   $a0,$v1,+6
    0x00000000,     # nop
    0x24030006,     # addiu $v1,$zero,6
    0x10830003,     # beq   $a0,$v1,+3
))
LEVEL_TIMER_HUD_SPRINTF_DELTA = 0x58        # -> jal sprintf      (US 0x0011C988)
LEVEL_TIMER_HUD_ADDSTRING_DELTA = 0x8C      # -> jal AddString    (US 0x00171F30)
LEVEL_TIMER_HUD_REMOVELAYER_DELTA = 0xA0    # -> jal RemoveLayer  (US 0x00172050)
LEVEL_TIMER_HUD_GENG_HI_DELTA = 0x84        # -> lui   of `this` = gEng
LEVEL_TIMER_HUD_GENG_LO_DELTA = 0x88        # -> addiu of `this` = gEng
# Build fingerprint -> expected gEng. Keyed on the tick hook's vaddr (US
# 0x00157D28 / JP 0x00157F28), which is itself derived from signature S1. This
# is a CROSS-CHECK on the derivation, not a source of addresses: if a known
# build derives an unexpected gEng we refuse to patch rather than guess.
LEVEL_TIMER_KNOWN_GENG = {0x00157D28: 0x00254F20, 0x00157F28: 0x0025C420}

# Worst case size of the HEAD-allocated `patch_starting_skills` cave: the 5-word
# ability-flag preamble + 2 words per grantable skill + the 4-word tail. Used
# only for the overlap guard below (see the comment there).
STARTING_SKILLS_CAVE_MAX_WORDS = 5 + 2 * len(STARTING_SKILL_IDS) + 4


def _lt_sext16(v: int) -> int:
    return v - 0x10000 if v & 0x8000 else v


def _lt_load_memsz(data: bytes) -> int:
    """p_memsz of the first PT_LOAD segment (0 if unreadable).

    gEng lives in .bss, i.e. INSIDE the loaded image but PAST p_filesz, so the
    range check on it must use memsz, not filesz.
    """
    if data[:4] != b"\x7fELF":
        return 0
    e_phoff = struct.unpack_from("<I", data, 0x1c)[0]
    e_phnum = struct.unpack_from("<H", data, 0x2c)[0]
    e_phentsize = struct.unpack_from("<H", data, 0x2a)[0]
    for i in range(e_phnum):
        o = e_phoff + i * e_phentsize
        p_type, _off, _va, _pa, _fsz, p_memsz = struct.unpack_from("<IIIIII", data, o)
        if p_type == 1:
            return p_memsz
    return 0


def _lt_derive_hud(buf: bytes, load_vaddr: int, load_off: int, filesz: int,
                   hook_vaddr: int):
    """Derive sprintf / AddString / RemoveLayer / gEng from signature S3.

    Returns a dict on success, or a string describing the failure. Every `jal`
    target is decoded from the instruction word and asserted to be a real `jal`
    landing inside the first PT_LOAD segment; gEng is decoded from the
    `lui`/`addiu` pair (with the sign-extended low half) and asserted to be
    inside the loaded image plus cross-checked against the known build map.
    """
    matches = _find_all(buf, LEVEL_TIMER_HUD_SIGNATURE)
    if not matches:
        return "HUD form-countdown block not found"
    if len(matches) != 1:
        return f"HUD form-countdown block is ambiguous ({len(matches)} matches)"
    sig = matches[0]

    def decode_jal(delta, name):
        off = sig + delta
        if off + 4 > len(buf):
            return f"{name} jal past end of buffer"
        w = struct.unpack_from("<I", buf, off)[0]
        if w >> 26 != _LT_OPCODE_JAL:
            return f"word at HUD block +0x{delta:X} is not a jal ({name})"
        vaddr = load_vaddr + (off - load_off)
        target = (vaddr & 0xF0000000) | ((w & 0x03FFFFFF) << 2)
        if not (load_vaddr <= target < load_vaddr + filesz):
            return f"derived {name} target is outside the LOAD segment"
        return target

    out = {"hud_off": sig, "hud_vaddr": load_vaddr + (sig - load_off)}
    for name, delta in (("sprintf", LEVEL_TIMER_HUD_SPRINTF_DELTA),
                        ("addstring", LEVEL_TIMER_HUD_ADDSTRING_DELTA),
                        ("removelayer", LEVEL_TIMER_HUD_REMOVELAYER_DELTA)):
        got = decode_jal(delta, name)
        if isinstance(got, str):
            return got
        out[name] = got

    hi_off = sig + LEVEL_TIMER_HUD_GENG_HI_DELTA
    lo_off = sig + LEVEL_TIMER_HUD_GENG_LO_DELTA
    if lo_off + 4 > len(buf):
        return "HUD gEng pair past end of buffer"
    hi_w = struct.unpack_from("<I", buf, hi_off)[0]
    lo_w = struct.unpack_from("<I", buf, lo_off)[0]
    if hi_w >> 26 != _LT_OPCODE_LUI or lo_w >> 26 != _LT_OPCODE_ADDIU:
        return "HUD gEng pair is not lui/addiu"
    geng = ((hi_w & 0xFFFF) << 16) + _lt_sext16(lo_w & 0xFFFF)
    memsz = max(filesz, _lt_load_memsz(buf))
    if not (load_vaddr <= geng < load_vaddr + memsz):
        return "derived gEng is outside the loaded image"
    expect = LEVEL_TIMER_KNOWN_GENG.get(hook_vaddr)
    if expect is not None and geng != expect:
        return (f"derived gEng 0x{geng:08X} != expected 0x{expect:08X} "
                f"for this build")
    out["geng"] = geng
    return out


def _find_string_padding_cave_tail(data: bytes, load_vaddr: int, load_off: int,
                                   filesz: int, need: int, reserve: int = 0,
                                   exclude_runs=()) -> int | None:
    """Like `_find_string_padding_cave`, but returns the 4-aligned TAIL of the
    zero run instead of its head.

    Same run discovery (a zero run of at least `need` + 8 bytes that follows a
    NUL-terminated ASCII string inside the loaded image), different allocation
    end. Exists as a SEPARATE function precisely so the head-allocating finder —
    which `patch_starting_skills` and anything else added later depends on —
    keeps its behaviour unchanged; allocating from opposite ends is what lets
    both features share the one big run without colliding.

    `reserve` bytes are kept free at the run's HEAD, so the returned block
    provably cannot collide with a head-allocated cave in the same run: a run
    that cannot hold `reserve + need` is SKIPPED rather than allocated into.
    Without this the caller could only detect the collision after the fact and
    had to fail the whole patch. `exclude_runs` is a collection of run start
    offsets to skip entirely, which is how two blocks get placed in two
    different runs.
    """
    end = min(load_off + filesz, len(data))
    for m in re.finditer(rb"\x00{%d,}" % (need + 8), data[load_off:end]):
        run_start = load_off + m.start()
        run_len = len(m.group())
        if run_start in exclude_runs:
            continue
        k = run_start - 1
        ascii_len = 0
        while k > load_off and 0x20 <= data[k] < 0x7f:
            k -= 1
            ascii_len += 1
        if ascii_len >= 4:
            cave = (run_start + run_len - need) & ~3
            # never eat the string's NUL terminator, keep `reserve` bytes free
            # at the head for a head-allocated cave, and stay inside the run
            if cave >= run_start + 4 + reserve:
                return cave
    return None


def _string_padding_run_start(data: bytes, load_off: int, cave_off: int) -> int:
    """The start offset of the zero run that `cave_off` was allocated from.

    Used to EXCLUDE that run when allocating a second block, so the two land in
    different runs and cannot overlap regardless of their sizes.
    """
    k = cave_off
    while k > load_off and data[k - 1] == 0:
        k -= 1
    return k


def _lt_head_allocated_runs(data: bytes, load_vaddr: int, load_off: int,
                            filesz: int, reserve: int) -> set[int]:
    """Every zero run a HEAD allocation could possibly land in.

    `_find_string_padding_cave` first-fits, so the run it returns depends on the
    size asked for: a small block can land in an earlier, smaller run than a
    large one. This enumerates the run for every head-allocated size from one
    word up to `reserve` (the worst case of everything that head-allocates) and
    returns the set of run starts. Excluding those runs is what lets a
    tail-allocated block use the rest of the image without keeping `reserve`
    bytes free in runs no head allocation can ever reach.
    """
    runs: set[int] = set()
    for need in range(4, reserve + 1, 4):
        off = _find_string_padding_cave(data, load_vaddr, load_off, filesz,
                                       need)
        if off is not None:
            runs.add(_string_padding_run_start(data, load_off, off))
    return runs


def _lt_alloc_blocks(buf: bytes, load_vaddr: int, load_off: int, filesz: int,
                     needs, exclude_runs=()):
    """Tail-allocate several INDEPENDENT blocks, each in its own zero run.

    Returns the list of file offsets (same order as `needs`) or None if any
    block cannot be placed. Each allocation excludes the runs every previous one
    landed in, so the blocks provably cannot overlap each other, and every
    window is re-checked to be all zeroes before it is handed back — nothing is
    written here, so a failure costs nothing.

    This exists because the level-timer cave is no longer one contiguous block:
    the gate word, the spill word and the "no timer" slot grew it past the
    largest padding run in either retail build, and every part of it is reached
    by an absolute `j` or an absolute lui/lo pair anyway.
    """
    offs = []
    ex = set(exclude_runs)
    for need in needs:
        off = _find_string_padding_cave_tail(buf, load_vaddr, load_off, filesz,
                                            need, reserve=0,
                                            exclude_runs=tuple(ex))
        if off is None or off + need > len(buf) or any(buf[off:off + need]):
            return None
        offs.append(off)
        ex.add(_string_padding_run_start(buf, load_off, off))
    return offs


def _lt_hi_lo(vaddr: int) -> tuple[int, int]:
    """Split `vaddr` into the (HI, LO) pair a `lui`/`lw`-`sw` pair needs.

    The load/store displacement is SIGN-extended, so when LO has bit 15 set the
    HI half must be incremented to compensate. Standard MIPS %hi/%lo rule.
    """
    lo = vaddr & 0xFFFF
    hi = (vaddr >> 16) & 0xFFFF
    if lo & 0x8000:
        hi = (hi + 1) & 0xFFFF
    return hi, lo


def _lt_seconds(spec) -> int:
    """Seconds from a `MM:SS` string (or a plain int/str of seconds).

    The preset table is written as MM:SS so it reads exactly like the request it
    came from; this is the ONLY place that turns it into a number.
    """
    if isinstance(spec, int) and not isinstance(spec, bool):
        return spec
    text = str(spec).strip()
    if ":" in text:
        mm, ss = text.split(":", 1)
        return int(mm) * 60 + int(ss)
    return int(text)


def _lt_preset_ticks(preset: str) -> list[int]:
    """The 12 value-table words for `preset`, in LEVEL_TIMER_WORLD_SLOTS order.

    Ticks are DERIVED from the MM:SS table times LEVEL_TIMER_TICKS_PER_SECOND —
    never hardcoded next to them — and each one is asserted to fit the 16-bit
    immediate range the cave's arming path relies on.
    """
    times = LEVEL_TIMER_PRESETS[preset]
    out = []
    for slot in LEVEL_TIMER_WORLD_SLOTS:
        if slot == LEVEL_TIMER_NO_TIMER_SLOT:
            # The "no timer" slot is 0 on purpose and is DELIBERATELY exempt
            # from LEVEL_TIMER_TICK_MIN: 0 is the tick's existing disarmed
            # value, not a duration, so it cannot be misread as expired. Every
            # REAL duration below still has to pass the range assert.
            out.append(0)
            continue
        ticks = _lt_seconds(times[slot]) * LEVEL_TIMER_TICKS_PER_SECOND
        assert LEVEL_TIMER_TICK_MIN <= ticks <= LEVEL_TIMER_TICK_MAX, \
            f"{preset}/{slot} = {ticks} ticks does not fit a 16-bit immediate"
        out.append(ticks)
    return out


def _lt_row_map_bytes(hub_rows=()) -> bytes:
    """`LEVEL_TIMER_ROW_SLOTS` as the byte map the cave's `lbu` reads.

    Slot NAMES are resolved to their value-table indices here, so the layout is
    written once (as names) and the indices are derived — a slot can be added or
    reordered in LEVEL_TIMER_WORLD_SLOTS without touching the map. Padded with
    zeros to a whole number of words (31 -> 32 bytes).

    `hub_rows` are the rows the IMAGE ITSELF identified as hubs (their `+0x00`
    PSX path filename contains `_HUB`); each is redirected to the "no timer"
    slot, whose value is 0, so the reset cave arms NOTHING in a hub. The rows are
    never hardcoded — see `_lt_hub_rows`.
    """
    out = bytearray(LEVEL_TIMER_ROW_MAP_WORDS * 4)
    for row, slot in enumerate(LEVEL_TIMER_ROW_SLOTS):
        out[row] = LEVEL_TIMER_WORLD_SLOTS.index(slot)
    none_slot = LEVEL_TIMER_WORLD_SLOTS.index(LEVEL_TIMER_NO_TIMER_SLOT)
    for row in hub_rows:
        assert 0 <= row < LEVEL_TIMER_ROW_COUNT
        out[row] = none_slot
    assert len(LEVEL_TIMER_ROW_SLOTS) == LEVEL_TIMER_ROW_COUNT <= len(out)
    return bytes(out)


def _lt_row_map_words(hub_rows=()) -> list[int]:
    """The row map as little-endian words, ready to emit into the cave."""
    b = _lt_row_map_bytes(hub_rows)
    return [struct.unpack_from("<I", b, i)[0] for i in range(0, len(b), 4)]


def _lt_level_row_paths(data):
    """The `+0x00` PSX path string of every LevelTable row, or None.

    Reads the table `find_level_table` located (stride LEVEL_TABLE_STRIDE, each
    row's first dword pointing at the level's PSX path) and follows each pointer
    back into the file. Used to VERIFY the row map's inferred indices.
    """
    found = find_level_table(data)
    if found is None:
        return None
    t_off, count, (t_vaddr, t_loadoff, filesz) = found
    b = bytes(data)
    end = min(t_loadoff + filesz, len(b))
    out = []
    for i in range(count):
        ptr = struct.unpack_from("<I", b, t_off + i * LEVEL_TABLE_STRIDE)[0]
        off = t_loadoff + (ptr - t_vaddr)
        if not (t_loadoff <= off < end):
            out.append(None)
            continue
        nul = b.find(b"\x00", off, end)
        out.append(b[off:nul if nul >= 0 else end].decode("latin-1"))
    return out


def _lt_check_level_rows(data):
    """Verify the row map's boss/dungeon rows against the image's own paths.

    Returns None when every checked row's PSX filename matches what
    LEVEL_TIMER_ROW_EXPECT says it should be, otherwise a string naming the row,
    the expected filename and what was actually found. This is what turns the
    "6 rows per world" INFERENCE into a checked fact: a differently ordered
    build refuses to patch instead of silently arming the wrong timer.
    """
    paths = _lt_level_row_paths(data)
    if paths is None:
        return "LevelTable not found (needed to verify the timer row map)"
    if len(paths) < LEVEL_TIMER_MIN_ROWS:
        return (f"LevelTable has only {len(paths)} rows, need at least "
                f"{LEVEL_TIMER_MIN_ROWS} to map the timer rows")
    for row, want in sorted(LEVEL_TIMER_ROW_EXPECT.items()):
        got = paths[row] if row < len(paths) else None
        if got is None or want.lower() not in got.lower():
            return (f"LevelTable row {row} should be {want} but holds "
                    f"{got!r} — refusing to patch the level timer")
    return None


def _lt_hub_rows(data):
    """The LevelTable rows that are HUBS, identified from the image's own paths.

    A hub row is one whose `+0x00` PSX path filename contains `_HUB`
    (case-insensitive) — the SAME kind of check `LEVEL_TIMER_ROW_EXPECT` uses to
    validate the boss rows, so the hub indices are never assumed. Returns a
    sorted tuple of rows inside the mapped range, or a string describing the
    failure; a build where no hub row can be identified REFUSES the patch rather
    than arming a clock in the hub.

    INTRO rows are deliberately NOT included — they are real levels and stay
    timed.
    """
    paths = _lt_level_row_paths(data)
    if paths is None:
        return "LevelTable not found (needed to identify the timer hub rows)"
    rows = tuple(sorted(
        row for row, p in enumerate(paths)
        if row < LEVEL_TIMER_ROW_COUNT and p
        and LEVEL_TIMER_HUB_MARKER.lower() in p.lower()))
    if not rows:
        return ("no LevelTable row's PSX path contains '_HUB' — refusing to "
                "patch the level timer (the hub rows could not be identified)")
    return rows


def resolve_level_timer(value):
    """Canonical preset name for `value`, or None when the feature is off.

    THE single edge mapping, shared by both argparse layers and the GUI so the
    accepted spellings can never drift apart. Case-insensitive; None, blank and
    the LEVEL_TIMER_OFF_ALIASES all mean "off"; anything unrecognised also
    returns None (callers that want to complain compare against their own
    choices list first). Never raises.
    """
    if value is None:
        return None
    try:
        key = str(value).strip().lower()
    except Exception:
        return None
    if key in LEVEL_TIMER_OFF_ALIASES:
        return None
    return key if key in LEVEL_TIMER_PRESETS else None


def _lt_preset_seconds(preset: str) -> dict:
    """`{world: seconds}` for `preset` — what the CLI reports it wrote."""
    return {slot: _lt_seconds(LEVEL_TIMER_PRESETS[preset][slot])
            for slot in LEVEL_TIMER_WORLD_SLOTS
            if slot != LEVEL_TIMER_NO_TIMER_SLOT}


def _lt_derive_level_vars(buf: bytes, data, load_vaddr: int, load_off: int):
    """Derive the `giLevelID` / `giLevel` $gp displacements from SIG_A / SIG_B.

    Returns {"d_id", "d_lv", "leveltable", "sig_a_off", "sig_b_off"} or a string
    describing the failure. Both signatures must match EXACTLY ONCE (they do in
    every retail build), and SIG_B's lui/addiu pair is decoded into the
    `LevelTable` base and cross-checked against `find_level_table`'s independent
    structural search — a mismatch refuses the patch rather than guessing.
    """
    a = _find_all_masked(buf, LEVEL_TIMER_LEVELID_PATTERN)
    if not a:
        return "giLevelID reader (CheckTimer ice gate) not found"
    if len(a) != 1:
        return f"giLevelID reader is ambiguous ({len(a)} matches)"
    b = _find_all_masked(buf, LEVEL_TIMER_LEVELROW_PATTERN)
    if not b:
        return "giLevel reader (level-runner LevelTable index) not found"
    if len(b) != 1:
        return f"giLevel reader is ambiguous ({len(b)} matches)"
    d_id = struct.unpack_from("<I", buf, a[0])[0] & 0xFFFF
    d_lv = struct.unpack_from("<I", buf, b[0])[0] & 0xFFFF
    hi = struct.unpack_from("<I", buf, b[0]
                            + LEVEL_TIMER_LEVELTABLE_HI_INDEX * 4)[0] & 0xFFFF
    lo = struct.unpack_from("<I", buf, b[0]
                            + LEVEL_TIMER_LEVELTABLE_LO_INDEX * 4)[0] & 0xFFFF
    table = (hi << 16) + _lt_sext16(lo)
    found = find_level_table(data)
    if found is None:
        return "LevelTable not found (needed to cross-check giLevel's reader)"
    t_off, _count, (t_vaddr, t_loadoff, _fsz) = found
    structural = t_vaddr + (t_off - t_loadoff)
    if structural != table:
        return (f"derived LevelTable 0x{table:08X} != structural "
                f"0x{structural:08X} — refusing to patch")
    return {"d_id": d_id, "d_lv": d_lv, "leveltable": table,
            "sig_a_off": a[0], "sig_b_off": b[0],
            "levelid_vaddr": load_vaddr + (a[0] - load_off),
            "levelrow_vaddr": load_vaddr + (b[0] - load_off)}


def _lt_pause_words(base: int, done: int) -> list[int]:
    """The PAUSE test: skip this frame entirely while the player is inside a
    scripted object interaction (chest / gate).

    Reads the current action id (Player+0x115, one `lbu` off `$s0`, which is the
    Player at the tick hook), biases it by the lowest paused id and tests ONE
    32-bit membership mask with a variable shift. That is a fixed
    LEVEL_TIMER_PAUSE_WORDS words no matter how many ids are in the set, which
    matters because the display cave is already within ~50 bytes of the largest
    usable padding run.

    `base` is this block's own word index inside the cave and `done` the index of
    the cave's `done` label; both branch displacements are computed from them, so
    the block can be dropped into either cave layout unchanged.

    Taking the pause branch jumps to `done`, which returns to the game WITHOUT
    touching the stored word — the remaining time is preserved exactly, never
    reset and never extended. `$v1` and `$at` are dead at the hook (it sits
    immediately after a `jal`), and the two branch delay slots only ever load a
    constant into `$at`, so they are harmless when the branch is taken.
    """
    hi = (LEVEL_TIMER_PAUSE_MASK >> 16) & 0xFFFF
    lo = LEVEL_TIMER_PAUSE_MASK & 0xFFFF
    nopause = base + LEVEL_TIMER_PAUSE_WORDS
    w = [
        0x92030000 | LEVEL_TIMER_ACTION_ID_OFFSET,      # 0 lbu  $v1,0x115($s0)
        0x24630000 | ((-LEVEL_TIMER_PAUSE_BASE) & 0xFFFF),  # 1 addiu $v1,$v1,-72
        0x2C610000 | LEVEL_TIMER_PAUSE_SPAN,            # 2 sltiu $at,$v1,21
        0x10200000 | ((nopause - (base + 4)) & 0xFFFF),  # 3 beq $at,$zero,nopause
        0x3C010000 | hi,                                # 4 lui  $at,HI(MASK)
        0x34210000 | lo,                                # 5 ori  $at,$at,LO(MASK)
        0x00610806,                                     # 6 srlv $at,$at,$v1
        0x30210001,                                     # 7 andi $at,$at,1
        0x14200000 | ((done - (base + 9)) & 0xFFFF),    # 8 bne  $at,$zero,done
        0x00000000,                                     # 9 nop
    ]
    assert len(w) == LEVEL_TIMER_PAUSE_WORDS
    return w


def _lt_build_end_block(t_hi: int, t_lo: int, g_vaddr: int, v_vaddr: int,
                        r_vaddr: int, guard_reg: int | None = None,
                        ret_vaddr: int | None = None,
                        ret_delay: int = 0x00000000) -> list[int]:
    """The STOP block: the level is over — DISARM the clock (store 0) and go on.

    ONE builder for BOTH stop hooks, so the disarmed value and the store can
    never drift apart:

    * `guard_reg=None, ret_vaddr=None` — CAVE_END, hooked on
      `mglEnterSubLevel`'s `jr $ra` (LEVEL_TIMER_END_PATTERN), whose delay slot
      has already done the engine's own store by the time we get here. Ends with
      the `jr $ra` it displaced. LEVEL_TIMER_END_WORDS words.
    * `guard_reg=<reg>, ret_vaddr=<addr>` — CAVE_COLUMN, hooked inside
      `Player::SetExtFunction` (LEVEL_TIMER_COLUMN_PATTERN). Runs for EVERY
      queued ext function, so it first tests the type in `guard_reg` against
      LEVEL_TIMER_COLUMN_EXT_FUNCTION and stores nothing at all for any other
      type, then returns with `j ret_vaddr` and `ret_delay` (the displaced
      instruction) in the delay slot. LEVEL_TIMER_COLUMN_WORDS words.

    Storing 0 is what stops the clock: 0 is the tick's DISARMED value (its very
    first test is `beq T,$zero,done`), so a stopped clock can never be mistaken
    for an expired one and can never reach the kill. Storing it twice (both
    hooks fire for a normal level exit) is idempotent for exactly that reason.

    THE RE-ARM GATE — why this block writes a second word. `PlaceAtStart` runs on
    EVERY level and sub-level entry, including the transition the column break
    itself starts, so the reset cave used to re-arm `T` immediately after the
    stop had zeroed it. THAT is why the stop appeared not to work at all. Both
    stop blocks therefore LATCH the row into the gate word `G`; the reset cave
    refuses to arm while `G` equals the row it is entering. `G` starts at
    LEVEL_TIMER_GATE_NONE (-1), which no row can equal, and the reset cave clears
    it back to that as soon as a DIFFERENT row arms.

    THESE BLOCKS DELIBERATELY DO NOT READ `$gp`. An earlier form read the row
    here with `lw $v1,D_LV($gp)`, which made the whole stop depend on `$gp`
    holding the small-data pointer at BOTH stop sites — something only ever
    justified for the RESET cave. If it did not, the stop latched a garbage row,
    the reset cave's compare never matched and PlaceAtStart re-armed: the clock
    stopped for a few frames and then resumed, which is exactly the symptom that
    was reported from play three times, and which no word-level simulation could
    catch because the simulation supplied `$gp` for itself. The dependency is now
    REMOVED rather than relied on: the reset cave publishes the row it armed into
    the data word `R` (it already holds the row in a register, at the one site
    where `$gp` is proven live), and the stops copy `R` into `G` using ONLY
    absolute lui/lo pairs. `R` is initialised to LEVEL_TIMER_GATE_NONE, so a stop
    that somehow fires before any arm latches the sentinel and blocks nothing.

    REGISTERS. `$at` is the assembler temporary and is dead at both sites
    (verified by disassembly: at the column site its last use is the queue-full
    `slti`/`beq` pair well before the hook and it is never read again in the
    function; at the `mglEnterSubLevel` site we are in the return path of a leaf,
    where `$at` cannot be live). The R->G copy still needs a second register to
    carry the value, so the block SPILLS `$v1` to the cave word `V` and restores
    it. `$v1` is in fact dead at both sites — the column site's own displaced
    instruction (`addiu <v>,$zero,11`) redefines it on every return path, and
    `mglEnterSubLevel` is a void leaf whose `$v1` no caller may rely on — but the
    spill is two words and it makes "this block clobbers nothing" true without
    any liveness argument at all, so it stays. The guard's `bne` delay slot is
    the spill `lui`, which only loads a constant into `$at`, so it is harmless
    when the branch is taken.
    """
    g_hi, g_lo = _lt_hi_lo(g_vaddr)
    v_hi, v_lo = _lt_hi_lo(v_vaddr)
    r_hi, r_lo = _lt_hi_lo(r_vaddr)
    body = [
        0x3C010000 | v_hi,                  # lui  $at,HI(V)
        0xAC230000 | v_lo,                  # sw   $v1,LO(V)($at)   (spill)
        0x3C010000 | r_hi,                  # lui  $at,HI(R)
        0x8C230000 | r_lo,                  # lw   $v1,LO(R)($at)   (armed row)
        0x3C010000 | g_hi,                  # lui  $at,HI(G)
        0xAC230000 | g_lo,                  # sw   $v1,LO(G)($at)   (latch row)
        0x3C010000 | v_hi,                  # lui  $at,HI(V)
        0x8C230000 | v_lo,                  # lw   $v1,LO(V)($at)   (restore)
        0x3C010000 | t_hi,                  # lui  $at,HI(T)
        0xAC200000 | t_lo,                  # sw   $zero,LO(T)($at) (DISARM)
    ]
    w: list[int] = []
    if guard_reg is not None:
        # addiu $at,<type>,-12 ; bne $at,$zero,<after the whole body>
        w.append(0x24010000 | (guard_reg << 21)
                 | ((-LEVEL_TIMER_COLUMN_EXT_FUNCTION) & 0xFFFF))
        w.append(0x14200000 | len(body))
    w += body
    if ret_vaddr is None:
        w.append(_JR_RA)             # jr  $ra
        w.append(0x00000000)         # nop
        assert len(w) == LEVEL_TIMER_END_WORDS
    else:
        w.append(0x08000000 | ((ret_vaddr >> 2) & 0x03FFFFFF))   # j ret
        w.append(ret_delay & 0xFFFFFFFF)                         # displaced
        assert len(w) == LEVEL_TIMER_COLUMN_WORDS
    return w


def _lt_build_state_block(back_vaddr: int, done_vaddr: int, count_off: int,
                          rec_off: int, stride: int) -> list[int]:
    """THE PER-FRAME KILL GATE — the last thing the tick asks before it kills.

    Entered by an absolute `j` from inside the tick's KILL CHECK and left either
    by `j back_vaddr` (the kill path, exactly as it was before this block
    existed) or, when the level is over, by `j done_vaddr` — the tick's own
    return-to-game label — so the kill simply never happens.

    IT DOES NOT STOP THE CLOCK, ON PURPOSE. The countdown keeps decrementing,
    keeps latching -1 and keeps drawing; only the KILL is gated. That is the
    whole point: stopping the clock meant fighting `PlaceAtStart`'s re-arm with a
    gate word, and hooking the level-end EVENT to stop it was tried twice in play
    and never fired (the column's script path lives in PSX data, not in the
    executable). Gating the kill needs neither.

    WHAT IT TESTS, and why it is the right state: the player's own EXT-FUNCTION
    QUEUE. `Player::SetExtFunction` (signature S6 — the same match this patch
    already requires) writes the queue count at `Player+count_off` and the i-th
    record's TYPE byte at `Player+rec_off + i*stride`; `Player::ProcessExtFunction`
    walks records 0..count-1 every frame and switches on that type byte through
    the 13-entry case table `_lt_verify_column_case` verifies per build, whose
    entry LEVEL_TIMER_COLUMN_EXT_FUNCTION is the case that calls
    `mglEnterSubLevel`. So "a queued record's type is 12" means, in player memory,
    "the level-end column's ext function is pending" — the level is over. All
    three displacements come out of the S6 match, so nothing here is hardcoded.

    PROVEN (from the demo debug build's own symbol table, MAXDEMOR.ELF):
      * the queue layout above, from `SetExtFunction__6PlayerFiiP15DEDynamicObject`
        (count at +0x388 with its own `slti <at>,<count>,0x10` queue-full guard,
        `sb <type>,0x38C`, `sh <param>,0x38E`, `sw <object>,0x390`, stride 8);
      * `ProcessExtFunction__6PlayerFv` reads the type with `lb <t>,0(<rec>)`,
        iterates while `i < Player[+0x388]` stepping the record pointer by 8, and
        its case table entry 12 is the body that calls `mglEnterSubLevel__Fi`;
      * `ProcessExtFunction` is called from `ControlPlayer+0x494`, i.e. AFTER this
        tick site (`ControlPlayer+0x44c`), and it is the only thing that clears
        the count mid-level — so at the tick the queue still holds whatever was
        queued for this frame;
      * `ResetExtFunctions__6PlayerFv` zeroes both the count and every record's
        type byte, so a count of 0 can never be read as a queued type.
    INFERRED (stated, not proven): that the column's script re-queues its ext
    function on each frame it is waiting. It must, because the count is cleared at
    the end of every `ProcessExtFunction` call while the case waits many frames —
    if it were queued only once, the case could never wait for anything. The gate
    is re-evaluated every frame, so it holds for exactly the frames the record is
    queued, which is what a kill gate (as opposed to a one-shot disarm) needs.

    WHY NOT `Player+0x8` BIT 5. Because that bit is a PAD BUTTON, and this was
    checked in the demo debug build before writing a line of it:
    `ReadJoystick__6PlayerFi` (demo 0x00130160) reads the pad, keeps this frame's
    button halfword at Player+0x4 and the previous frame's at Player+0x6, and at
    0x00130434-0x00130444 computes `cur & (cur ^ prev)` — the NEWLY PRESSED
    buttons — and stores it with `sh $v1,8($s1)`. The level-column case in
    `ProcessExtFunction` (demo 0x00137340, the case body that calls
    `mglEnterSubLevel__Fi` and `SetLevelColumn__6PlayerFUi`) does
    `lhu $v1,8($s5)` / `andi $v1,$v1,0x20` / `beq` — i.e. it WAITS FOR A BUTTON
    PRESS, which is why the transition lands at the end of the victory sequence.
    Testing that bit in the tick would therefore have disabled the kill on the
    single frame the player pressed that button, anywhere in the game, and done
    nothing whatsoever about the level being over. Player+0x8 is not a flags
    field at all.

    REGISTERS: `$at`, `$v1` and `$a0` only — never `$v0`, which the caller is
    still holding the countdown in — and all three are dead at the tick site (it
    sits immediately after a `jal`, so nothing live can be outside
    `$s*`/`$sp`/`$gp`). `$s0` is the Player, which is what makes the whole test
    two loads and a compare. NO `$gp`, no `jal`, and no store of any kind: this
    block cannot change T, L, the latch or the display even in principle.
    """
    LOOP = 5
    BACK = 11
    OVER = 13
    w = [
        # --- is anything queued at all? -----------------------------------
        0x8E030000 | (count_off & 0xFFFF),      # 0 lw    $v1,COUNT($s0)
        0x18600000 | ((BACK - 2) & 0xFFFF),     # 1 blez  $v1,back
        0x2C610000 | (LEVEL_TIMER_EXT_QUEUE_SCAN_MAX + 1),
        #                                         2 sltiu $at,$v1,17 (delay)
        0x10200000 | ((BACK - 4) & 0xFFFF),     # 3 beq   $at,$zero,back
        0x26040000 | (rec_off & 0xFFFF),        # 4 addiu $a0,$s0,RECORDS (delay)
        # --- scan the queue for the level-end column's ext function -------
        0x90810000,                             # 5 loop: lbu $at,0($a0)
        0x24210000 | ((-LEVEL_TIMER_COLUMN_EXT_FUNCTION) & 0xFFFF),
        #                                         6 addiu $at,$at,-12
        0x10200000 | ((OVER - 8) & 0xFFFF),     # 7 beq   $at,$zero,over
        0x2463FFFF,                             # 8 addiu $v1,$v1,-1  (delay)
        0x1C600000 | ((LOOP - 10) & 0xFFFF),    # 9 bgtz  $v1,loop
        0x24840000 | (stride & 0xFFFF),         # 10 addiu $a0,$a0,8  (delay)
        # --- not over: back to the kill path, exactly as before -----------
        0x08000000 | ((back_vaddr >> 2) & 0x03FFFFFF),   # 11 back: j KILL
        0x00000000,                             # 12 nop
        # --- over: NEVER KILL. No store, nothing else changed. ------------
        0x08000000 | ((done_vaddr >> 2) & 0x03FFFFFF),   # 13 over: j done
        0x00000000,                             # 14 nop
    ]
    assert len(w) == LEVEL_TIMER_STATE_WORDS
    return [x & 0xFFFFFFFF for x in w]


def _lt_verify_column_case(buf: bytes, load_vaddr: int, load_off: int,
                           filesz: int, mgl_vaddr: int):
    """Prove that ext-function type LEVEL_TIMER_COLUMN_EXT_FUNCTION really is the
    level-end column's case IN THIS BUILD.

    Finds `Player::ProcessExtFunction`'s jump table structurally: a
    LEVEL_TIMER_COLUMN_CASES-word run of in-image code pointers whose
    LEVEL_TIMER_COLUMN_DEFAULT_CASES entries are all the same default label and
    whose entry LEVEL_TIMER_COLUMN_EXT_FUNCTION points at a case body that calls
    `mglEnterSubLevel` (address derived from signature S5) within the next
    LEVEL_TIMER_COLUMN_CASE_LOOKBACK instructions.

    Returns the table's vaddr, or a string describing the failure. Exactly one
    table matches in every build we patch; anything else refuses the patch.
    """
    end = min(load_off + filesz, len(buf))
    # every `jal mglEnterSubLevel`, as a vaddr
    starts: set[int] = set()
    for off in range(load_off, end - 4, 4):
        w = struct.unpack_from("<I", buf, off)[0]
        if w >> 26 != _LT_OPCODE_JAL:
            continue
        va = load_vaddr + (off - load_off)
        if ((va & 0xF0000000) | ((w & 0x03FFFFFF) << 2)) == mgl_vaddr:
            for k in range(LEVEL_TIMER_COLUMN_CASE_LOOKBACK):
                starts.add(va - 4 * k)
    if not starts:
        return "no call to mglEnterSubLevel found (needed to verify the " \
               "level-column ext function)"
    idx = LEVEL_TIMER_COLUMN_EXT_FUNCTION
    found = []
    for off in range(load_off, end - 4, 4):
        w = struct.unpack_from("<I", buf, off)[0]
        if w not in starts:
            continue
        t_off = off - idx * 4
        if t_off < load_off or t_off + LEVEL_TIMER_COLUMN_CASES * 4 > end:
            continue
        e = [struct.unpack_from("<I", buf, t_off + i * 4)[0]
             for i in range(LEVEL_TIMER_COLUMN_CASES)]
        if not all(load_vaddr <= x < load_vaddr + filesz for x in e):
            continue
        d = {e[i] for i in LEVEL_TIMER_COLUMN_DEFAULT_CASES}
        if len(d) != 1 or e[idx] in d:
            continue
        found.append(load_vaddr + (t_off - load_off))
    if not found:
        return (f"ProcessExtFunction case table with ext function {idx} -> "
                f"mglEnterSubLevel not found — refusing to patch the level "
                f"timer column stop")
    if len(found) != 1:
        return (f"ProcessExtFunction case table is ambiguous "
                f"({len(found)} candidates)")
    return found[0]


def _lt_build_reset_block(t_hi: int, t_lo: int, tbl_vaddr: int,
                          map_vaddr: int, d_lv: int, g_vaddr: int,
                          r_vaddr: int, last_pair=None) -> list[int]:
    """CAVE_RESET: pick THIS ROW's duration out of the value table, store it.

    `$v0`, `$v1` and `$at` are free (PlaceAtStart is void and both callers
    discard the result) and `$gp` is live, so the row index is one `lw` using the
    displacement copied verbatim from SIG_B. No `jal`, so `$ra` stays intact for
    the closing `jr $ra`.

    SELECTION IS PURELY ROW-INDEXED — no world lookup, no compares: bounds-check
    `giLevel` against LEVEL_TIMER_ROW_COUNT, `lbu` the slot index out of the
    31-byte ROW MAP, then `lw` the ticks out of the 12-word value table. An
    out-of-range row falls back to slot 0 (grave). Extending this to another
    per-slot limit is a data edit, not another branch.

    ADDRESSING BOTH TABLES: `lui $at,HI` / `addu $at,$at,<scaled index>` /
    `lbu`-or-`lw $v0,LO($at)`. `_lt_hi_lo` already bumps HI when LO's bit 15 is
    set, so (HI<<16) + sext16(LO) == BASE exactly; adding the index to the base
    BEFORE the sign-extended displacement is applied therefore lands on
    BASE + index with the %hi/%lo carry correctly accounted for, for every index.
    """
    tb_hi, tb_lo = _lt_hi_lo(tbl_vaddr)
    mp_hi, mp_lo = _lt_hi_lo(map_vaddr)
    g_hi, g_lo = _lt_hi_lo(g_vaddr)
    r_hi, r_lo = _lt_hi_lo(r_vaddr)
    FALLBACK = 18
    HAVE = 19
    NOARM = 25 if last_pair is None else 28
    w = [
        0x8F830000 | (d_lv & 0xFFFF),               #  0 lw    $v1,D_LV($gp)
        # --- THE RE-ARM GATE ---------------------------------------------
        # `G` holds the row a stop hook latched when it zeroed `T`. If that is
        # the row we are entering, the level's own end sequence is running and
        # PlaceAtStart must NOT re-arm — leave `T` at 0. Any other row (a real
        # new level, or a death/respawn in a level that was never stopped) arms
        # normally and clears the gate. Without this the stop was overwritten
        # microseconds later, which is why it looked like it did nothing.
        0x3C010000 | g_hi,                          #  1 lui   $at,HI(G)
        0x8C220000 | g_lo,                          #  2 lw    $v0,LO(G)($at)
        0x10430000 | ((NOARM - 4) & 0xFFFF),        #  3 beq   $v0,$v1,noarm
        0x00000000,                                 #  4 nop
        0x2402FFFF,                                 #  5 addiu $v0,$zero,-1
        0x3C010000 | g_hi,                          #  6 lui   $at,HI(G)
        0xAC220000 | g_lo,                          #  7 sw    $v0,LO(G)($at)
        # --- PUBLISH THE ROW FOR THE STOP BLOCKS --------------------------
        # `R` is the row we are arming. THIS is the only site that reads the row
        # out of `$gp` — the stop blocks copy `R` instead, so they never touch
        # `$gp` at all (see `_lt_build_end_block`).
        0x3C010000 | r_hi,                          #  8 lui   $at,HI(R)
        0xAC230000 | r_lo,                          #  9 sw    $v1,LO(R)($at)
        # --- ROW -> SLOT -> DURATION --------------------------------------
        0x2C610000 | LEVEL_TIMER_ROW_COUNT,         # 10 sltiu $at,$v1,31
        0x10200000 | ((FALLBACK - 12) & 0xFFFF),    # 11 beq   $at,$zero,fallback
        0x00000000,                                 # 12 nop
        0x3C010000 | mp_hi,                         # 13 lui   $at,HI(MAP)
        0x00230821,                                 # 14 addu  $at,$at,$v1
        0x90220000 | mp_lo,                         # 15 lbu   $v0,LO(MAP)($at)
        0x10000000 | ((HAVE - 17) & 0xFFFF),        # 16 b     have
        0x00000000,                                 # 17 nop
        0x24020000,                                 # 18 fallback: addiu $v0,0
        0x00021080,                                 # 19 have: sll $v0,$v0,2
        0x3C010000 | tb_hi,                         # 20 lui  $at,HI(TBL)
        0x00220821,                                 # 21 addu $at,$at,$v0
        0x8C220000 | tb_lo,                         # 22 lw   $v0,LO(TBL)($at)
        0x3C010000 | t_hi,                          # 23 lui  $at,HI(T)
        0xAC220000 | t_lo,                          # 24 sw   $v0,LO(T)($at)
    ]
    if last_pair is not None:
        # Invalidate L (the last displayed value) so the first tick of every
        # attempt always draws — see the display block's own comment.
        hiL, loL = last_pair
        w.append(0x2402FFFF)                        # addiu $v0,$zero,-1
        w.append(0x3C010000 | hiL)                  # lui   $at,HI(L)
        w.append(0xAC220000 | loL)                  # sw    $v0,LO(L)($at)
    assert len(w) == NOARM                          # noarm: (see the gate)
    w.append(_JR_RA)                                # jr   $ra
    w.append(0x00000000)                            # nop
    return w


# Data words trailing the display cave: T, L, S, B (8 bytes), F ("%d\0"), the
# 13-word per-slot VALUE TABLE, the 8-word (31-byte) ROW -> SLOT map (both of
# which the reset cave indexes) and the three re-arm-gate words G, V and R.
_LT_DATA_WORDS = (6 + LEVEL_TIMER_VALUE_TABLE_WORDS
                  + LEVEL_TIMER_ROW_MAP_WORDS
                  + LEVEL_TIMER_GATE_WORDS)


def _lt_build_display_block(cave_vaddr: int, hook_vaddr: int, values,
                            gamevarsub: int, hud, d_lv: int,
                            data_vaddr: int | None = None,
                            column=None, hub_rows=(),
                            code_b_vaddr: int | None = None,
                            queue=None):
    """The extended (kill + on-screen countdown) cave, as a list of words.

    Layout, one contiguous block: tick code, then the reset lookup, then the
    data words T (remaining ticks, 0 = disarmed), L (last displayed value),
    S (spill slot for AddString's return across the RemoveLayer call),
    B (8-byte text buffer), F ("%d\\0"), TBL (the 12 per-slot durations) and MAP
    (the 31-byte ROW -> SLOT map, padded to 8 words).

    Internally two-pass: the first pass only measures the code length (word
    count is address-independent) so the data words can be addressed. Call with
    `cave_vaddr=0` when you only need the block SIZE.

    The data words are addressed by ABSOLUTE lui/lo pairs, so they do not have
    to follow the code: pass `data_vaddr` to put them in a SEPARATE cave. The
    returned words are still one list — `code_len` is where the data starts, so
    the caller writes `words[:code_len]` at `cave_vaddr` and `words[code_len:]`
    at `data_vaddr`. This is what lets the 384-byte code block and the 48-byte
    data block live in two different padding runs (no single run in either
    retail build is large enough to hold both the whole 432-byte contiguous
    block AND the head-allocated `patch_starting_skills` cave).
    """
    def build(data_vaddr: dict) -> tuple[list[int], dict]:
        W: list[int] = []
        lab: dict[str, int] = {}
        fix: list[tuple[int, int, str]] = []

        def emit(*ws):
            for w in ws:
                W.append(w & 0xFFFFFFFF)

        def label(name):
            lab[name] = len(W)

        def br(tmpl, name):
            fix.append((len(W), tmpl, name))
            W.append(0)

        def hl(sym):
            return _lt_hi_lo(data_vaddr[sym])

        def jal(target):
            return 0x0C000000 | ((target >> 2) & 0x03FFFFFF)

        # ---- 1) load T; 0 == disarmed (fail-safe) --------------------------
        hi, lo = hl("T")
        emit(0x3C010000 | hi)                    # lui   $at,HI(T)
        emit(0x8C220000 | lo)                    # lw    $v0,LO(T)($at)
        # PAUSE — chest / gate. Same block as the kill-only cave, emitted here
        # through the label fixer so it jumps to this cave's own `done`.
        emit(0x92030000 | LEVEL_TIMER_ACTION_ID_OFFSET)   # lbu  $v1,0x115($s0)
        emit(0x24630000 | ((-LEVEL_TIMER_PAUSE_BASE) & 0xFFFF))  # addiu $v1,-72
        emit(0x2C610000 | LEVEL_TIMER_PAUSE_SPAN)         # sltiu $at,$v1,21
        br(0x10200000, "nopause")                # beq   $at,$zero,nopause
        emit(0x3C010000 | ((LEVEL_TIMER_PAUSE_MASK >> 16) & 0xFFFF))  # lui $at
        emit(0x34210000 | (LEVEL_TIMER_PAUSE_MASK & 0xFFFF))          # ori $at
        emit(0x00610806)                         # srlv  $at,$at,$v1
        emit(0x30210001)                         # andi  $at,$at,1
        br(0x14200000, "done")                   # bne   $at,$zero,done
        emit(0x00000000)                         # nop
        label("nopause")
        br(0x10400000, "done")                   # beq   $v0,$zero,done
        emit(0x2442FFFF)                         # addiu $v0,$v0,-1  (delay)
        br(0x14400000, "store")                  # bne   $v0,$zero,store
        emit(0x00000000)                         # nop
        emit(0x2402FFFF)                         # addiu $v0,$zero,-1 (latch)
        label("store")
        emit(0x3C010000 | hi)                    # lui   $at,HI(T)
        emit(0xAC220000 | lo)                    # sw    $v0,LO(T)($at)

        # ---- 2) DISPLAY: value = (T < 0) ? 0 : T/60 + 1 --------------------
        br(0x04400000, "expired")                # bltz  $v0,expired
        emit(0x00000000)                         # nop
        emit(0x2401_0000 | LEVEL_TIMER_TICKS_PER_SECOND)   # addiu $at,$zero,60
        emit(0x0041001B)                         # divu  $v0,$at (divisor const 60,
        emit(0x00000000)                         # nop            never zero)
        emit(0x00000000)                         # nop   (divu->mflo hazard)
        emit(0x00001812)                         # mflo  $v1
        emit(0x24630000 | LEVEL_TIMER_DISPLAY_OFFSET)      # addiu $v1,$v1,+1
        br(0x10000000, "haveval")                # b     haveval
        emit(0x00000000)                         # nop
        label("expired")
        emit(0x24030000)                         # addiu $v1,$zero,0
        label("haveval")
        # DRAW ON CHANGE ONLY — see the banner: drawing every frame leaks an
        # object plus a text buffer 60x/second.
        hiL, loL = hl("L")
        emit(0x3C010000 | hiL)                   # lui   $at,HI(L)
        emit(0x8C220000 | loL)                   # lw    $v0,LO(L)($at)
        br(0x10430000, "skipdraw")               # beq   $v0,$v1,skipdraw
        emit(0x00000000)                         # nop
        emit(0x3C010000 | hiL)                   # lui   $at,HI(L)
        emit(0xAC230000 | loL)                   # sw    $v1,LO(L)($at)

        # sprintf(B, F, value)
        hiB, loB = hl("B")
        hiF, loF = hl("F")
        emit(0x3C040000 | hiB, 0x24840000 | loB)  # lui/addiu $a0 = B
        emit(0x3C050000 | hiF, 0x24A50000 | loF)  # lui/addiu $a1 = F
        emit(0x00603025)                          # or    $a2,$v1,$zero
        emit(jal(hud["sprintf"]))                 # jal   sprintf
        emit(0x00000000)                          # nop   (delay)

        # AddString(gEng, B, x, y, scale, 0, 0, 0) -> $v0
        ghi, glo = _lt_hi_lo(hud["geng"])
        emit(0x3C040000 | ghi, 0x24840000 | glo)  # lui/addiu $a0 = gEng
        emit(0x3C050000 | hiB, 0x24A50000 | loB)  # lui/addiu $a1 = B
        emit(0x3C090000 | LEVEL_TIMER_DISPLAY_X_LUI, 0x44896000)  # $f12 = x
        emit(0x3C090000 | LEVEL_TIMER_DISPLAY_Y_LUI, 0x44896800)  # $f13 = y
        emit(0x3C090000 | LEVEL_TIMER_DISPLAY_SCALE_LUI, 0x44897000)  # $f14
        emit(0x24060000)                          # addiu $a2,$zero,0
        emit(0x24070000)                          # addiu $a3,$zero,0
        emit(jal(hud["addstring"]))               # jal   AddString
        emit(0x24080000)                          # addiu $t0,$zero,0 (7th arg)
        hiS, loS = hl("S")
        emit(0x3C010000 | hiS)                    # lui   $at,HI(S)
        emit(0xAC220000 | loS)                    # sw    $v0,LO(S)($at)

        # RemoveLayer(gEng, 40) — vanilla's ORDER: create, then remove the old
        # (still carrying the layer tag), then tag the new one.
        emit(0x3C040000 | ghi, 0x24840000 | glo)  # lui/addiu $a0 = gEng
        emit(jal(hud["removelayer"]))             # jal   RemoveLayer
        emit(0x24050000 | LEVEL_TIMER_DISPLAY_LAYER)   # addiu $a1,$zero,40

        # sh 40,6(new string) — the layer is NOT an AddString argument.
        emit(0x3C010000 | hiS)                    # lui   $at,HI(S)
        emit(0x8C220000 | loS)                    # lw    $v0,LO(S)($at)
        br(0x10400000, "skipdraw")                # beq   $v0,$zero,skipdraw
        emit(0x24030000 | LEVEL_TIMER_DISPLAY_LAYER)   # addiu $v1,$zero,40
        emit(0xA4430006)                          # sh    $v1,6($v0)
        label("skipdraw")

        # ---- 3) KILL CHECK (unchanged logic, re-reads the SAME word) -------
        # THE KILL GATE, and the only thing that changed here: hand the frame to
        # the per-frame level-over test (block C — see `_lt_build_state_block`),
        # which either jumps straight back to the word after this pair (the kill
        # path, byte for byte as before) or, when the level is over, jumps to this
        # cave's own `done`. It stores nothing, so T, L and the display are
        # untouched by it and the clock deliberately keeps running.
        emit(0x08000000 | ((data_vaddr["STATE"] >> 2) & 0x03FFFFFF))  # j STATE
        emit(0x00000000)                         # nop (delay slot)
        kill_index = len(W)
        emit(0x3C010000 | hi)                     # lui   $at,HI(T)
        emit(0x8C220000 | lo)                     # lw    $v0,LO(T)($at)
        br(0x1C400000, "done")                    # bgtz  $v0,done
        emit(0x00000000)                          # nop
        emit(0x8E020050)                          # lw    $v0,0x50($s0)
        br(0x18400000, "done")                    # blez  $v0,done
        emit(0x00000000)                          # nop
        emit(0x72002628)                          # pmove $a0,$s0,$zero
        emit(0x24050002)                          # addiu $a1,$zero,2
        emit(0x240603E7)                          # addiu $a2,$zero,999
        emit(jal(gamevarsub))                     # jal   GameVarSub
        emit(0x70003E28)                          # pmove $a3,$zero,$zero
        label("done")
        emit(0x08000000 | (((hook_vaddr + 8) >> 2) & 0x03FFFFFF))  # j HOOK+8
        emit(LEVEL_TIMER_TICK_DISPLACED)          # lwc1 $f2,0x3c($s0) (delay)

        # ---- everything below lives in the SECOND cave --------------------
        # REGION SPLIT. The tick/display code above is the only part that has to
        # be one contiguous block; the reset and end blocks are entered by
        # absolute `j`s and the data words by absolute lui/lo pairs, so they go
        # with the data. This is what keeps the code block inside the one padding
        # run big enough for it (see the allocation comment in
        # `patch_level_timer`) now that the pause and end blocks have been added.
        code_split = len(W)

        # ---- CAVE_RESET ---------------------------------------------------
        # The per-world lookup (see `_lt_build_reset_block`), plus the 3 words
        # that invalidate L. L MUST be invalidated here: every transition runs
        # RemoveAllLayer, which destroys our string, so a stale "last displayed
        # value" would suppress the draw until the number happened to change (up
        # to a second with no countdown on screen). -1 can never be a displayed
        # value, so the first tick of every attempt always draws.
        label("reset")
        emit(*_lt_build_reset_block(hi, lo, data_vaddr["TBL"],
                                    data_vaddr["MAP"], d_lv,
                                    data_vaddr["G"], data_vaddr["R"],
                                    last_pair=(hiL, loL)))

        # ---- CAVE_END (stop on sub-level complete) -------------------------
        label("end")
        emit(*_lt_build_end_block(hi, lo, data_vaddr["G"], data_vaddr["V"],
                                  data_vaddr["R"]))

        # ---- CAVE_COLUMN (stop when the level-end column is BROKEN) --------
        # The early stop — see the S6 banner. Same builder, same stored 0 and the
        # same latched re-arm gate.
        if column is not None:
            label("column")
            emit(*_lt_build_end_block(hi, lo, data_vaddr["G"], data_vaddr["V"],
                                      data_vaddr["R"], guard_reg=column[0],
                                      ret_vaddr=column[1],
                                      ret_delay=column[2]))

        code_len = len(W)
        # ---- data ---------------------------------------------------------
        emit(0x00000000)                              # T (0 = disarmed)
        emit(0xFFFFFFFF)                              # L (impossible value ->
        #                                               first frame always draws)
        emit(0x00000000)                              # S
        emit(0x00000000, 0x00000000)                  # B[8]
        emit(struct.unpack("<I", LEVEL_TIMER_DISPLAY_FORMAT)[0])   # F = "%d\0"
        emit(*values)                                 # TBL[13] per-slot ticks
        emit(*_lt_row_map_words(hub_rows))            # MAP[31 bytes] row -> slot
        emit(LEVEL_TIMER_GATE_NONE)                   # G (no stop latched)
        emit(0x00000000)                              # V (register spill slot)
        emit(LEVEL_TIMER_GATE_NONE)                   # R (no row armed yet)

        # ---- THE PER-FRAME KILL GATE, still in block C --------------------
        # Code, not data, and deliberately here: block B's padding run cannot
        # hold another 60 bytes in either retail build and this one can (see
        # LEVEL_TIMER_BLOCK_C_WORDS). Entered by the absolute `j` emitted as word
        # 0 above and left by an absolute `j` either way, so its run is
        # irrelevant.
        if len(W) - code_len != _LT_DATA_WORDS:
            raise AssertionError("level timer data words moved")
        state_index = len(W)
        emit(*_lt_build_state_block(cave_vaddr + kill_index * 4,
                                    cave_vaddr + lab["done"] * 4,
                                    queue[0], queue[1], queue[2]))

        for idx, tmpl, name in fix:
            W[idx] = (tmpl | ((lab[name] - (idx + 1)) & 0xFFFF)) & 0xFFFFFFFF
        return {"words": W, "code_split": code_split, "code_len": code_len,
                "reset_index": lab["reset"], "end_index": lab["end"],
                "column_index": lab.get("column"), "state_index": state_index,
                "kill_index": kill_index, "done_index": lab["done"]}

    # pass 1 — measure only (word counts are address-independent)
    dummy = {k: 0 for k in ("T", "L", "S", "B", "F", "TBL", "MAP", "G", "V",
                            "R", "STATE")}
    meta = build(dummy)
    code_split, code_len = meta["code_split"], meta["code_len"]
    # THREE independent regions, each addressed absolutely (`j` for the code
    # blocks, lui/lo pairs for the data words), so none of them has to follow
    # another:
    #   A  cave_vaddr    tick + countdown draw + kill check + pause
    #   B  code_b_vaddr  reset + end + column blocks   (default: after A)
    #   C  data_vaddr    T/L/S/B/F + TBL + MAP + G + V (default: after B)
    # Splitting B from C is what lets the whole feature fit the padding runs
    # once the re-arm gate and the "no timer" slot grew the block: no single run
    # left in either retail build can hold B and C together.
    base_b = (cave_vaddr + code_split * 4 if code_b_vaddr is None
              else code_b_vaddr)
    base = (base_b + (code_len - code_split) * 4 if data_vaddr is None
            else data_vaddr)
    map_v = base + 24 + LEVEL_TIMER_VALUE_TABLE_WORDS * 4
    gate_v = map_v + LEVEL_TIMER_ROW_MAP_WORDS * 4
    real = {"T": base, "L": base + 4, "S": base + 8, "B": base + 12,
            "F": base + 20, "TBL": base + 24,
            "MAP": map_v, "G": gate_v, "V": gate_v + 4, "R": gate_v + 8,
            # The level-over test follows the data words inside block C.
            "STATE": base + _LT_DATA_WORDS * 4}
    out = build(real)
    if out["code_split"] != code_split or out["code_len"] != code_len \
            or len(out["words"]) != (code_len + _LT_DATA_WORDS
                                     + LEVEL_TIMER_STATE_WORDS):
        raise AssertionError("level timer cave layout is not stable")
    out["base_b"] = base_b
    out["data_words_vaddr"] = base
    out["reset_vaddr"] = base_b + (out["reset_index"] - code_split) * 4
    out["end_vaddr"] = base_b + (out["end_index"] - code_split) * 4
    if out["column_index"] is not None:
        out["column_vaddr"] = base_b + (out["column_index"] - code_split) * 4
    out["timer_vaddr"] = base
    out["gate_vaddr"] = gate_v
    out["spill_vaddr"] = gate_v + 4
    out["row_vaddr"] = gate_v + 8
    # The tick's `j` was assembled from real["STATE"], so the block had better be
    # exactly there: block C starts at `base`, its data words come first, and the
    # test starts at word `state_index` of the whole list.
    out["state_vaddr"] = base + (out["state_index"] - code_len) * 4
    if out["state_vaddr"] != real["STATE"]:
        raise AssertionError("level timer state block is not where it is called")
    out["kill_vaddr"] = cave_vaddr + out["kill_index"] * 4
    out["done_vaddr"] = cave_vaddr + out["done_index"] * 4
    return out


def patch_level_timer(data: bytearray, preset,
                      show_timer: bool = True) -> dict:
    """Kill Maximo after this world's share of level time (per attempt).

    With `show_timer` (the default) the cave ALSO draws the remaining seconds on
    screen, reading the SAME storage word the kill logic uses so the number and
    the death can never drift. `show_timer=False` emits the kill-only cave.

    `preset` is `None`/`"off"` (a TOTAL no-op — the executable is left
    byte-identical by this feature) or one of LEVEL_TIMER_PRESETS
    (hard/medium/easy, case-insensitive). Each preset carries TWELVE durations —
    six areas plus six bosses — and the reset cave selects one per level entry by
    indexing a ROW -> SLOT map with `giLevel` (the LevelTable row).
    Mutates `data` in place and returns a stats dict. See the section banner
    above for the whole design.

    ALL-OR-NOTHING: all FOUR hook words (tick, reset, the early column-broken
    stop and the late sub-level stop), the derived GameVarSub target and the cave
    window are all located and verified BEFORE anything is written. A
    half-applied code cave crashes the game, so any failure writes nothing and
    returns `applied: False` naming the piece that failed.

    IDEMPOTENCE: a re-run is REFUSED rather than re-armed. If ANY hook word
    already holds a `j` (opcode 8) instead of its vanilla instruction the
    function reports `already_patched` and writes nothing. Re-arming in place
    would mean trusting a cave this function did not write (its layout, its
    storage-word position) — refusing is the only option that cannot corrupt an
    already-patched image, and callers only ever patch a fresh copy anyway.
    """
    # 1) Argument validation FIRST, so a bad value never searches the buffer.
    name = resolve_level_timer(preset)
    if name is None:
        off_alias = preset is None or (
            str(preset).strip().lower() in LEVEL_TIMER_OFF_ALIASES)
        if off_alias:                   # feature disabled — total no-op
            return {"applied": False, "preset": None, "ticks": []}
        return {"applied": False, "preset": preset, "ticks": [],
                "reason": f"level timer preset must be one of off/"
                          f"{'/'.join(LEVEL_TIMER_PRESET_NAMES)}"}
    values = _lt_preset_ticks(name)     # asserts the table fits the immediate
    times = _lt_preset_seconds(name)

    if data[:4] != b"\x7fELF":
        return {"found": False, "applied": False, "preset": name,
                "ticks": values, "reason": "not ELF"}
    seg = _first_load_segment(bytes(data))
    if seg is None:
        return {"found": False, "applied": False, "preset": name,
                "ticks": values, "reason": "no LOAD segment"}
    load_vaddr, load_off, filesz = seg

    def fail(reason, found=True):
        return {"found": found, "applied": False, "preset": name,
                "ticks": values, "reason": reason}

    buf = bytes(data)

    # 2) PHASE ONE — locate every piece. Nothing is written in this phase.
    tick_matches = _find_all_masked(buf, LEVEL_TIMER_TICK_PATTERN)
    if not tick_matches:
        # An ALREADY PATCHED image cannot match S1 any more (we overwrote its
        # first word with a `j`), so distinguish the two cases here: S2 is
        # untouched by this patch, so its `jr $ra` slot tells us which it is.
        for off in _find_all(buf, LEVEL_TIMER_RESET_SIGNATURE):
            w = struct.unpack_from("<I", buf, off + LEVEL_TIMER_RESET_JR_DELTA)[0]
            if w >> 26 == _LT_OPCODE_J:
                return {"found": True, "applied": False, "preset": name,
                        "ticks": values, "already_patched": True,
                        "reason": "level timer is already patched in — "
                                  "re-arming is not supported"}
        return fail("ControlPlayer tick site not found", found=False)
    hook_off = tick_matches[0]
    hook_word = struct.unpack_from("<I", buf, hook_off)[0]
    if hook_word >> 26 == _LT_OPCODE_J:
        return {"found": True, "applied": False, "preset": name,
                "ticks": values, "already_patched": True,
                "reason": "tick hook already holds a jump — already patched"}
    if hook_word != LEVEL_TIMER_TICK_DISPLACED:
        return fail("expected 'lwc1 $f2,0x3c($s0)' at tick hook")

    reset_matches = _find_all(buf, LEVEL_TIMER_RESET_SIGNATURE)
    if not reset_matches:
        return fail("Player::PlaceAtStart not found", found=False)
    reset_off = reset_matches[0]
    jr_off = reset_off + LEVEL_TIMER_RESET_JR_DELTA
    if jr_off + 8 > len(buf):
        return fail("PlaceAtStart tail past end of buffer")
    jr_word = struct.unpack_from("<I", buf, jr_off)[0]
    if jr_word >> 26 == _LT_OPCODE_J:
        return {"found": True, "applied": False, "preset": name,
                "ticks": values, "already_patched": True,
                "reason": "reset hook already holds a jump — already patched"}
    if jr_word != LEVEL_TIMER_RESET_JR:
        return fail("expected 'jr $ra' at PlaceAtStart+0x90")
    if struct.unpack_from("<I", buf, jr_off + 4)[0] != LEVEL_TIMER_RESET_DELAY:
        return fail("expected 'addiu $sp,$sp,48' at PlaceAtStart+0x94")

    # HOOK 3 — the sub-level-complete stop (`mglEnterSubLevel`, signature S5).
    end_matches = _find_all_masked(buf, LEVEL_TIMER_END_PATTERN)
    if not end_matches:
        return fail("mglEnterSubLevel not found", found=False)
    if len(end_matches) != 1:
        return fail(f"mglEnterSubLevel: expected 1 site, "
                    f"found {len(end_matches)}")
    end_jr_off = end_matches[0] + LEVEL_TIMER_END_JR_DELTA
    if end_jr_off + 8 > len(buf):
        return fail("mglEnterSubLevel tail past end of buffer")
    end_jr_word = struct.unpack_from("<I", buf, end_jr_off)[0]
    if end_jr_word >> 26 == _LT_OPCODE_J:
        return {"found": True, "applied": False, "preset": name,
                "ticks": values, "already_patched": True,
                "reason": "sub-level-complete hook already holds a jump — "
                          "already patched"}
    if end_jr_word != LEVEL_TIMER_RESET_JR:      # the same `jr $ra` word
        return fail("expected 'jr $ra' at mglEnterSubLevel+0x18")
    mgl_vaddr = load_vaddr + (end_matches[0] - load_off)

    # HOOK 4 — the EARLY stop: the level-end column has been BROKEN, which is
    # `Player::SetExtFunction(type = 12)` (signature S6, see its banner). Hook 3
    # above only fires at the END of the victory sequence, so it is kept as a
    # second, idempotent stop rather than the only one.
    col_matches = _find_all_masked(buf, LEVEL_TIMER_COLUMN_PATTERN)
    if not col_matches:
        return fail("Player::SetExtFunction queue store not found",
                    found=False)
    if len(col_matches) != 1:
        return fail(f"Player::SetExtFunction queue store: expected 1 site, "
                    f"found {len(col_matches)}")
    col_off = col_matches[0] + LEVEL_TIMER_COLUMN_HOOK_INDEX * 4
    col_words = [struct.unpack_from("<I", buf, col_matches[0] + i * 4)[0]
                 for i in range(len(LEVEL_TIMER_COLUMN_PATTERN))]
    if col_words[LEVEL_TIMER_COLUMN_HOOK_INDEX] >> 26 == _LT_OPCODE_J:
        return {"found": True, "applied": False, "preset": name,
                "ticks": values, "already_patched": True,
                "reason": "column-broken hook already holds a jump — "
                          "already patched"}
    col_displaced = col_words[LEVEL_TIMER_COLUMN_HOOK_INDEX]
    # The type register is READ OUT OF THE MATCH (the `sb`'s rt field), never
    # assumed, and the `bne` two words later must compare that same register
    # against the register word 0 loads — that is what proves the match really
    # is the type test this hook keys on.
    col_reg = (col_words[LEVEL_TIMER_COLUMN_TYPE_INDEX] >> 16) & 0x1F
    col_v = (col_displaced >> 16) & 0x1F        # rt of `addiu <v>,$zero,11`
    bne = col_words[7]
    if ((bne >> 21) & 0x1F, (bne >> 16) & 0x1F) != (col_reg, col_v):
        return fail("SetExtFunction type test does not compare the stored "
                    "type against the loaded constant")
    # The displaced word is re-executed in the cave's RETURN delay slot, so the
    # instruction that stays in the hook's own delay slot must neither read nor
    # write the register it defines.
    delay = col_words[LEVEL_TIMER_COLUMN_HOOK_INDEX + 1]
    if col_v in (((delay >> 16) & 0x1F), ((delay >> 21) & 0x1F)) or col_v == 0:
        return fail("SetExtFunction hook delay slot conflicts with the "
                    "displaced instruction")
    if col_reg == 0:
        return fail("SetExtFunction type register decoded as $zero")
    col_ret = load_vaddr + (col_off - load_off) + 8

    # THE EXT-FUNCTION QUEUE LAYOUT, read straight out of the same match: the
    # count field, the record base and the record stride. These are what the
    # per-frame LEVEL-OVER TEST reads out of the Player every frame
    # (`_lt_build_state_block`); deriving them here rather than writing them down
    # means a build whose queue is laid out differently cannot match S6 in the
    # first place. The stride comes from the `sll <a>,<c>,3` the same match
    # verifies, and both displacements must be positive and fit the `lw`/`lbu`
    # displacement they are pasted into.
    q_count = col_words[LEVEL_TIMER_COLUMN_COUNT_INDEX] & 0xFFFF
    q_rec = col_words[LEVEL_TIMER_COLUMN_RECORD_INDEX] & 0xFFFF
    q_stride = 1 << ((col_words[LEVEL_TIMER_COLUMN_STRIDE_INDEX] >> 6) & 0x1F)
    if not (0 < q_count < 0x8000) or not (0 < q_rec < 0x8000):
        return fail("SetExtFunction queue displacements are out of range")
    if q_rec <= q_count or q_stride < 4 or q_stride > 0x100:
        return fail("SetExtFunction queue layout does not look like a "
                    "count followed by fixed-stride records")

    # Ext-function type 12 is VERIFIED against this build's own
    # ProcessExtFunction case table, never assumed (see `_lt_verify_column_case`).
    col_table = _lt_verify_column_case(buf, load_vaddr, load_off, filesz,
                                      mgl_vaddr)
    if isinstance(col_table, str):
        return fail(col_table)

    # PAUSE PROOF — Player::SetAction's tail, which is where Player+0x115 (the
    # current action id the pause test reads) is written. Not hooked; required
    # to match exactly once so a build that lays the Player out differently
    # REFUSES the patch instead of testing a meaningless byte.
    sa_matches = _find_all(buf, LEVEL_TIMER_SETACTION_TAIL_SIGNATURE)
    if len(sa_matches) != 1:
        return fail(f"Player::SetAction tail (proves Player+0x"
                    f"{LEVEL_TIMER_ACTION_ID_OFFSET:X} is the action id): "
                    f"expected 1 site, found {len(sa_matches)}",
                    found=bool(sa_matches))

    # GameVarSub — DERIVED from the damage site's own `jal`, never hardcoded.
    sig = buf.find(DAMAGE_HIT_SIGNATURE)
    if sig < 0:
        return fail("damage-hit site not found (needed to derive GameVarSub)",
                    found=False)
    jal_off = sig + 16
    if jal_off + 4 > len(buf):
        return fail("damage-hit jal past end of buffer")
    jal_word = struct.unpack_from("<I", buf, jal_off)[0]
    if jal_word >> 26 != _LT_OPCODE_JAL:
        return fail("word at damage-hit site +16 is not a jal")
    jal_vaddr = load_vaddr + (jal_off - load_off)
    gamevarsub = (jal_vaddr & 0xF0000000) | ((jal_word & 0x03FFFFFF) << 2)
    if not (load_vaddr <= gamevarsub < load_vaddr + filesz):
        return fail("derived GameVarSub target is outside the LOAD segment")

    hook_vaddr = load_vaddr + (hook_off - load_off)

    # giLevelID / giLevel — the two $gp displacements the reset cave needs, both
    # DERIVED from their own one-hit signatures, with SIG_B's LevelTable base
    # cross-checked against the structural search (see `_lt_derive_level_vars`).
    lv = _lt_derive_level_vars(buf, data, load_vaddr, load_off)
    if isinstance(lv, str):
        return fail(lv)
    # `d_lv` is what the cave reads. `d_id` is NOT used by the cave any more —
    # SIG_A is still required to match exactly once as a build sanity check and
    # its displacement is still reported (see the banner).
    d_id, d_lv = lv["d_id"], lv["d_lv"]

    # The row map's boss/dungeon indices are INFERRED, so verify them against
    # the image's own LevelTable paths before writing anything.
    rows_bad = _lt_check_level_rows(data)
    if rows_bad:
        return fail(rows_bad)

    # HUB ROWS — identified from the image's own LevelTable paths (`_HUB` in the
    # PSX filename), never assumed. They are mapped to the "no timer" slot so the
    # reset cave arms nothing there; a build where they cannot be identified
    # refuses to patch.
    hub_rows = _lt_hub_rows(data)
    if isinstance(hub_rows, str):
        return fail(hub_rows)

    hud = None
    if show_timer:
        hud = _lt_derive_hud(buf, load_vaddr, load_off, filesz, hook_vaddr)
        if isinstance(hud, str):
            return fail(hud)

    # Block size depends on show_timer, so it is COMPUTED, never fixed. The
    # display build is laid out by `_lt_build_display_block` (two passes: the
    # first only measures the code length so the data words can be addressed).
    #
    # HEAD RESERVE: `patch_starting_skills` first-fits from the HEAD of the same
    # zero runs, so every allocation here keeps its worst case
    # (STARTING_SKILLS_CAVE_MAX_WORDS) free at the run head. A run that cannot
    # hold both is SKIPPED, not allocated into and then rejected.
    #
    # WHY THE DISPLAY CAVE IS SPLIT IN TWO: the only padding run big enough for
    # its code at all is the 569-byte "MAXIMO3.ICO" run, which leaves 389 bytes
    # after the head reserve. Region A (tick + countdown draw + kill check +
    # pause) is 340 of those; the reset block, the stop block and the data words
    # are all reached by absolute `j`s / lui-lo pairs and never need to follow
    # the code, so they go in a SECOND cave in a different run.
    #
    # WHY THE SECOND CAVE DOES NOT KEEP THE HEAD RESERVE: it cannot — the second
    # block needs 204 bytes and every run except the 569-byte one (already taken
    # by region A) is smaller than reserve + 204, which is exactly why this
    # feature used to report "no data cave found" and silently refuse to apply
    # whenever the countdown was enabled. The reserve is only ever needed in a
    # run a HEAD allocator could pick, so instead of reserving blindly we
    # enumerate every run `_find_string_padding_cave` could return for any
    # head-allocated size up to `reserve` and EXCLUDE those runs outright. That
    # is strictly stronger than the byte reserve (it cannot be defeated by a
    # small head allocation landing in an earlier, smaller run) and it costs
    # nothing in the runs the head allocator can never reach.
    # The lives ZERO CLAMP also head-allocates, immediately AFTER the
    # starting-skills block, so the reserve covers both.
    reserve = (STARTING_SKILLS_CAVE_MAX_WORDS + LIFE_CLAMP_CAVE_WORDS) * 4
    if show_timer:
        sized = _lt_build_display_block(0, 0, values, gamevarsub, hud, d_lv,
                                       column=(col_reg, col_ret,
                                               col_displaced),
                                       hub_rows=hub_rows,
                                       queue=(q_count, q_rec, q_stride))
        need = sized["code_split"] * 4
        code_b_need = (sized["code_len"] - sized["code_split"]) * 4
        data_need = (len(sized["words"]) - sized["code_len"]) * 4
    else:
        need = LEVEL_TIMER_TICK_WORDS * 4        # 136 bytes, block A
        code_b_need = LEVEL_TIMER_BLOCK_B_WORDS * 4      # 212 bytes, block B
        data_need = LEVEL_TIMER_BLOCK_C_WORDS * 4        # 180 bytes, block C
    cave_off = _find_string_padding_cave_tail(buf, load_vaddr, load_off,
                                              filesz, need, reserve=reserve)
    if cave_off is None or cave_off + need > len(buf):
        return fail("no code cave found")
    if any(buf[cave_off:cave_off + need]):
        return fail("code cave window is not clear")
    # Blocks B and C go in OTHER runs: the run block A took, plus every run a
    # HEAD allocator (`patch_starting_skills` / the lives clamp) could ever
    # first-fit into, are all excluded outright — strictly stronger than keeping
    # `reserve` bytes free, and it costs nothing in runs no head allocation can
    # reach. See `_lt_alloc_blocks`.
    code_run = _string_padding_run_start(buf, load_off, cave_off)
    head_runs = _lt_head_allocated_runs(buf, load_vaddr, load_off, filesz,
                                        reserve)
    got = _lt_alloc_blocks(buf, load_vaddr, load_off, filesz,
                           (code_b_need, data_need),
                           exclude_runs=(code_run,) + tuple(head_runs))
    if got is None:
        return fail("no data cave found")
    code_b_off, data_off = got
    # OVERLAP GUARD (now belt-and-braces — the head reserve above makes this
    # structural rather than a post-hoc rejection). Kept because it is cheap and
    # it re-checks the one invariant a future change to the run discovery could
    # break.
    head = _find_string_padding_cave(buf, load_vaddr, load_off, filesz,
                                     reserve)
    if head is not None:
        head_end = head + reserve
        for off, size in ((cave_off, need), (code_b_off, code_b_need),
                          (data_off, data_need)):
            if off is not None and off < head_end and off + size > head:
                return fail("tail cave overlaps the head-allocated cave")
    # No two blocks may overlap each other either (they are in different runs by
    # construction; this is the cheap re-check of that).
    spans = sorted(((cave_off, need), (code_b_off, code_b_need),
                    (data_off, data_need)))
    for (o1, s1), (o2, _s2) in zip(spans, spans[1:]):
        if o1 + s1 > o2:
            return fail("level timer cave blocks overlap")

    # 3) Build the block. Code first, then the data words, contiguous, so the
    # ONE storage word is at a known offset from every reader.
    cave_vaddr = load_vaddr + (cave_off - load_off)
    tick_vaddr = cave_vaddr
    code_b_vaddr = load_vaddr + (code_b_off - load_off)
    data_vaddr = load_vaddr + (data_off - load_off)
    if show_timer:
        built = _lt_build_display_block(cave_vaddr, hook_vaddr, values,
                                       gamevarsub, hud, d_lv,
                                       data_vaddr=data_vaddr,
                                       column=(col_reg, col_ret,
                                               col_displaced),
                                       hub_rows=hub_rows,
                                       code_b_vaddr=code_b_vaddr,
                                       queue=(q_count, q_rec, q_stride))
        words = built["words"]
        code_words = built["code_split"]
        code_len = built["code_len"]
        reset_vaddr = built["reset_vaddr"]
        end_vaddr = built["end_vaddr"]
        column_vaddr = built["column_vaddr"]
        timer_vaddr = built["timer_vaddr"]
        # THREE padding runs (see the allocation comment): words[:code_split]
        # (tick + draw + kill) at cave_off, words[code_split:code_len] (the
        # reset / end / column blocks) at code_b_off, and the data words at
        # data_off.
        for i, w in enumerate(words[:code_words]):
            struct.pack_into("<I", data, cave_off + i * 4, w)
        for i, w in enumerate(words[code_words:code_len]):
            struct.pack_into("<I", data, code_b_off + i * 4, w)
        for i, w in enumerate(words[code_len:]):
            struct.pack_into("<I", data, data_off + i * 4, w)
        struct.pack_into("<I", data, hook_off,
                         0x08000000 | ((tick_vaddr >> 2) & 0x03FFFFFF))
        struct.pack_into("<I", data, jr_off,
                         0x08000000 | ((reset_vaddr >> 2) & 0x03FFFFFF))
        struct.pack_into("<I", data, end_jr_off,
                         0x08000000 | ((end_vaddr >> 2) & 0x03FFFFFF))
        struct.pack_into("<I", data, col_off,
                         0x08000000 | ((column_vaddr >> 2) & 0x03FFFFFF))
        return {"found": True, "applied": True, "preset": name,
                "ticks": values, "times": times,
                "levelid_disp": d_id, "levelrow_disp": d_lv,
                "leveltable": lv["leveltable"],
                "table_vaddr": timer_vaddr + 24,
                "map_vaddr": (timer_vaddr + 24
                              + LEVEL_TIMER_VALUE_TABLE_WORDS * 4),
                "gate_vaddr": built["gate_vaddr"],
                "spill_vaddr": built["spill_vaddr"],
                "row_vaddr": built["row_vaddr"],
                "state_vaddr": built["state_vaddr"],
                "kill_gate_return_vaddr": built["kill_vaddr"],
                "kill_gate_done_vaddr": built["done_vaddr"],
                "ext_count_offset": q_count, "ext_record_offset": q_rec,
                "ext_record_stride": q_stride,
                "ext_queue_scan_max": LEVEL_TIMER_EXT_QUEUE_SCAN_MAX,
                "hub_rows": tuple(hub_rows),
                "hub_slot": LEVEL_TIMER_WORLD_SLOTS.index(
                    LEVEL_TIMER_NO_TIMER_SLOT),
                "gate_none": LEVEL_TIMER_GATE_NONE,
                "cave_off": cave_off, "cave_vaddr": cave_vaddr,
                "cave_bytes": need, "timer_vaddr": timer_vaddr,
                "code_b_off": code_b_off, "code_b_vaddr": code_b_vaddr,
                "code_b_bytes": code_b_need,
                "data_off": data_off, "data_vaddr": data_vaddr,
                "data_bytes": data_need,
                "gamevarsub": gamevarsub, "tick_hook_vaddr": hook_vaddr,
                "reset_hook_vaddr": load_vaddr + (jr_off - load_off),
                "show_timer": True, "layer": LEVEL_TIMER_DISPLAY_LAYER,
                "sprintf": hud["sprintf"], "addstring": hud["addstring"],
                "removelayer": hud["removelayer"], "geng": hud["geng"],
                "hud_vaddr": hud["hud_vaddr"],
                "end_hook_vaddr": load_vaddr + (end_jr_off - load_off),
                "end_vaddr": end_vaddr,
                "column_hook_vaddr": load_vaddr + (col_off - load_off),
                "column_vaddr": column_vaddr,
                "column_ext_function": LEVEL_TIMER_COLUMN_EXT_FUNCTION,
                "column_case_table": col_table,
                "pause_actions": LEVEL_TIMER_PAUSE_ACTIONS,
                "action_id_vaddr": LEVEL_TIMER_ACTION_ID_OFFSET,
                "sites": 4,
                "slots": [hook_off, jr_off, end_jr_off, col_off]}

    # Block A holds the tick code and the countdown word T; block B the reset /
    # end / column blocks; block C the value table, the row map and the two gate
    # words. Every cross-block reference is an absolute `j` or lui/lo pair.
    timer_vaddr = cave_vaddr + LEVEL_TIMER_STORAGE_WORD_INDEX * 4
    reset_vaddr = code_b_vaddr
    end_vaddr = reset_vaddr + LEVEL_TIMER_RESET_WORDS * 4
    column_vaddr = end_vaddr + LEVEL_TIMER_END_WORDS * 4
    table_vaddr = data_vaddr
    map_vaddr = table_vaddr + LEVEL_TIMER_VALUE_TABLE_WORDS * 4
    gate_vaddr = map_vaddr + LEVEL_TIMER_ROW_MAP_WORDS * 4
    spill_vaddr = gate_vaddr + 4
    row_vaddr = gate_vaddr + 8
    # The per-frame KILL GATE follows the data words inside block C.
    state_vaddr = data_vaddr + LEVEL_TIMER_BLOCK_C_DATA_WORDS * 4
    hi, lo = _lt_hi_lo(timer_vaddr)
    # Word indices (0-based) of the internal labels. PAUSE is 10 words wide and
    # sits between the load and the decrement, so every label below moves with
    # it — they are written here as `2 + LEVEL_TIMER_PAUSE_WORDS + n` rather than
    # as bare numbers so the layout stays correct if the pause block changes.
    _p = 2 + LEVEL_TIMER_PAUSE_WORDS           # first word after the pause test
    store = _p + 5                             # `store: lui $at,HI(T)`
    kill = _p + 9                              # `kill: bgtz $v0,done` (gate ret)
    done = _p + 19                             # `done: j HOOK+8`
    words = [
        0x3C010000 | hi,                       #  0 lui   $at,HI(T)
        0x8C220000 | lo,                       #  1 lw    $v0,LO(T)($at)
    ]
    # ---- PAUSE (chest / gate): skip the whole frame, touch nothing ----------
    words += _lt_pause_words(len(words), done)
    words += [
        0x10400000 | ((done - (_p + 1)) & 0xFFFF),   # beq $v0,$zero,done
        0x2442FFFF,                            # addiu $v0,$v0,-1  (delay)
        0x14400000 | ((store - (_p + 3)) & 0xFFFF),  # bne $v0,$zero,store
        0x00000000,                            # nop
        0x2402FFFF,                            # addiu $v0,$zero,-1 (latch)
        0x3C010000 | hi,                       # store: lui $at,HI(T)
        0xAC220000 | lo,                       # sw    $v0,LO(T)($at)
        # THE KILL GATE — the only thing between the store and the kill. It
        # returns to `kill` below (the kill path, byte for byte as before) or, if
        # the level is over, straight to `done`. It stores nothing and does not
        # touch `$v0`, so the decrement, the latch, the store and (in the display
        # cave) the drawing are all unaffected and the clock keeps running.
        0x08000000 | ((state_vaddr >> 2) & 0x03FFFFFF),   # j STATE
        0x00000000,                            # nop (delay slot)
        0x1C400000 | ((done - (_p + 10)) & 0xFFFF),  # kill: bgtz $v0,done
        0x00000000,                            # nop
        0x8E020050,                            # lw   $v0,0x50($s0)
        0x18400000 | ((done - (_p + 13)) & 0xFFFF),  # blez $v0,done
        0x00000000,                            # nop
        0x72002628,                            # pmove $a0,$s0,$zero
        0x24050002,                            # addiu $a1,$zero,2
        0x240603E7,                            # addiu $a2,$zero,999
        0x0C000000 | ((gamevarsub >> 2) & 0x03FFFFFF),   # jal GameVarSub
        0x70003E28,                            # pmove $a3,$zero,$zero
        0x08000000 | (((hook_vaddr + 8) >> 2) & 0x03FFFFFF),  # done: j HOOK+8
        LEVEL_TIMER_TICK_DISPLACED,            # lwc1 $f2,0x3c($s0) (delay)
        0x00000000,                            # T: .word 0  (0 = disarmed)
    ]
    if len(words) != LEVEL_TIMER_TICK_WORDS:    # pure self-check
        return fail("internal cave layout error (tick block)")
    # ---- BLOCK B: CAVE_RESET + CAVE_END + CAVE_COLUMN -------------------
    words_b = _lt_build_reset_block(hi, lo, table_vaddr, map_vaddr, d_lv,
                                    gate_vaddr, row_vaddr)
    words_b += _lt_build_end_block(hi, lo, gate_vaddr, spill_vaddr, row_vaddr)
    words_b += _lt_build_end_block(hi, lo, gate_vaddr, spill_vaddr, row_vaddr,
                                   guard_reg=col_reg,
                                   ret_vaddr=col_ret, ret_delay=col_displaced)
    if len(words_b) != LEVEL_TIMER_BLOCK_B_WORDS:   # pure self-check
        return fail("internal cave layout error (reset/stop block)")
    # ---- BLOCK C: VALUE TABLE + ROW MAP + the three gate words ----------
    words_c = list(values)
    words_c += _lt_row_map_words(hub_rows)
    # G (gate), V (spill), R (row last armed — starts at the same sentinel so a
    # stop before any arm latches nothing real).
    words_c += [LEVEL_TIMER_GATE_NONE, 0x00000000, LEVEL_TIMER_GATE_NONE]
    if len(words_c) != LEVEL_TIMER_BLOCK_C_DATA_WORDS:   # pure self-check
        return fail("internal cave layout error (data block)")
    # ...followed by the per-frame LEVEL-OVER TEST, which is code but lives here
    # because block B's padding run has no room for it in either retail build.
    words_c += _lt_build_state_block(cave_vaddr + kill * 4,
                                     cave_vaddr + done * 4,
                                     q_count, q_rec, q_stride)
    if len(words_c) != LEVEL_TIMER_BLOCK_C_WORDS:   # pure self-check
        return fail("internal cave layout error (data + state block)")

    # 4) PHASE TWO — everything verified, so all the writes are safe.
    for i, w in enumerate(words):
        struct.pack_into("<I", data, cave_off + i * 4, w)
    for i, w in enumerate(words_b):
        struct.pack_into("<I", data, code_b_off + i * 4, w)
    for i, w in enumerate(words_c):
        struct.pack_into("<I", data, data_off + i * 4, w)
    struct.pack_into("<I", data, hook_off,
                     0x08000000 | ((tick_vaddr >> 2) & 0x03FFFFFF))
    struct.pack_into("<I", data, jr_off,
                     0x08000000 | ((reset_vaddr >> 2) & 0x03FFFFFF))
    struct.pack_into("<I", data, end_jr_off,
                     0x08000000 | ((end_vaddr >> 2) & 0x03FFFFFF))
    struct.pack_into("<I", data, col_off,
                     0x08000000 | ((column_vaddr >> 2) & 0x03FFFFFF))
    return {"found": True, "applied": True, "preset": name, "ticks": values,
            "times": times, "levelid_disp": d_id, "levelrow_disp": d_lv,
            "leveltable": lv["leveltable"], "table_vaddr": table_vaddr,
            "map_vaddr": map_vaddr, "gate_vaddr": gate_vaddr,
            "spill_vaddr": spill_vaddr, "row_vaddr": row_vaddr,
            "state_vaddr": state_vaddr,
            "kill_gate_return_vaddr": cave_vaddr + kill * 4,
            "kill_gate_done_vaddr": cave_vaddr + done * 4,
            "ext_count_offset": q_count, "ext_record_offset": q_rec,
            "ext_record_stride": q_stride,
            "ext_queue_scan_max": LEVEL_TIMER_EXT_QUEUE_SCAN_MAX,
            "hub_rows": tuple(hub_rows),
            "hub_slot": LEVEL_TIMER_WORLD_SLOTS.index(
                LEVEL_TIMER_NO_TIMER_SLOT),
            "gate_none": LEVEL_TIMER_GATE_NONE,
            "cave_off": cave_off, "cave_vaddr": cave_vaddr, "cave_bytes": need,
            "code_b_off": code_b_off, "code_b_vaddr": code_b_vaddr,
            "code_b_bytes": code_b_need,
            "data_off": data_off, "data_vaddr": data_vaddr,
            "data_bytes": data_need,
            "timer_vaddr": timer_vaddr, "gamevarsub": gamevarsub,
            "tick_hook_vaddr": hook_vaddr,
            "reset_hook_vaddr": load_vaddr + (jr_off - load_off),
            "end_hook_vaddr": load_vaddr + (end_jr_off - load_off),
            "end_vaddr": end_vaddr,
            "column_hook_vaddr": load_vaddr + (col_off - load_off),
            "column_vaddr": column_vaddr,
            "column_ext_function": LEVEL_TIMER_COLUMN_EXT_FUNCTION,
            "column_case_table": col_table,
            "pause_actions": LEVEL_TIMER_PAUSE_ACTIONS,
            "action_id_vaddr": LEVEL_TIMER_ACTION_ID_OFFSET,
            "show_timer": False,
            "sites": 4,
            "slots": [hook_off, jr_off, end_jr_off, col_off]}


# ============================================================================
# Widescreen (16:9)
# ============================================================================
# A static, in-ELF port of the published PCSX2 widescreen cheats for Maximo.
# Those cheats poke two floats at runtime; this patch writes the SAME two floats
# straight into the executable so no cheat engine / pnach is needed.
#
# THE ASPECT MATHS
# ---------------------------------------------------------------------------
# Retail renders for a 4:3 display. Two independent constants have to change:
#
#   SITE A — horizontal geometry scale, in DarkEngine::EngineInit.
#            vanilla 1.0f -> vanilla x (4/3)/(16/9) = x 0.75 -> 0.75f
#            This squeezes the horizontal axis so the wider frustum below does
#            not stretch everything.
#   SITE B — render aspect ratio, field +4 of `gViewFrustum`.
#            vanilla 1.4f -> vanilla x 4/3 = 1.8666666...f
#            Widens the frustum so the extra horizontal field of view is
#            actually rendered rather than cropped.
#
# 1.8666666... is not exactly representable; the nearest float is 0x3FEEEEEF.
# We write 0x3FEEEEEE, which is 1 ULP below it — that is what the published
# cheat uses, and either value is visually and numerically indistinguishable at
# this scale, so both work. It is kept identical to the cheat for traceability.
#
# WHY SITE A IS EXACTLY 0.75 AND WHY 16:10 IS NOT OFFERED
# ---------------------------------------------------------------------------
# SITE A is not a data float: it is the `lui $v0,0x3F80` half of a
# load-immediate (1.0f = 0x3F800000, whose low 16 bits are zero, so the
# compiler emitted only the `lui`). That means ONLY THE TOP 16 BITS of the
# constant are settable in place — the low half stays 0x0000. 0.75f is
# 0x3F400000, which fits that constraint exactly, which is precisely why 16:9
# is patchable this way. 16:10 would need (4/3)/(16/10) = 0.8333..., i.e.
# 0x3F555555, whose low half is non-zero and therefore NOT writable without
# adding an instruction. So only 16:9 is offered here.
#
# THE JP BUILD IS PATCHED THROUGH THE US MECHANISM
# ---------------------------------------------------------------------------
# The JP release has its own published widescreen cheat at 0x00262FE0, but that
# address lives in BSS: it is zero in the file on disk and only gets a value at
# runtime, so it is NOT statically patchable. Both sites below are located by
# signature instead and are present, unique and identical in shape in the JP
# build, so JP gets widescreen through exactly the same two edits as US.
# Patching `viewDist` (another candidate reachable statically) was evaluated and
# REJECTED: it squeezes the image vertically as well as widening it, so the
# result is distorted rather than wider.
#
# gViewFrustum
# ---------------------------------------------------------------------------
# `gViewFrustum` is a 4-float struct of initialised data, {60.0, 1.4, 1.0,
# 150.0} = {fov, aspect, near, far}. Its SOLE consumer is
# PWorld::CullMainWorld, which is also how we find it: the function loads
# fields +0 / +8 / +0xC through `lui $at` + `lwc1` pairs, so the struct's
# address is DERIVED from the matched `lui`/`lwc1` immediate pair rather than
# hardcoded. Before writing we assert field[0] == 60.0 and field[3] == 150.0,
# which pins the struct identity independently of the code match.
#
# MEASURED VALUES (reference only — nothing here is hardcoded except the
# signatures and the vanilla/patched words):
#                          SLUS_200.17     SLPM_621.27
#   SITE A `lui` slot        0x06EF3C        0x06EFFC
#   CullMainWorld match      0x192284        0x192584
#   aspect slot (frustum+4)  0x0D7394        0x0DED44
# Each signature matches EXACTLY ONCE in each retail build; anything else is
# refused rather than guessed at.
#
# ATOMIC: both sites are located and both words verified BEFORE either is
# written, so a build where only one site resolves is left byte-identical.
# IDEMPOTENT: a slot already holding the patched word counts as success.
# ============================================================================

# SITE A — DarkEngine::EngineInit, 12 fixed words, no build-varying fields, so
# plain byte matching is enough. The target `lui` is word 0 (delta 0).
WIDESCREEN_HOR_SIGNATURE = bytes.fromhex(
    "803f023c"      # lui   $v0,0x3F80      <-- TARGET (1.0f)
    "00688244"      # mtc1  $v0,$f13
    "c06b0426"      # addiu $a0,$s0,0x6BC0
    "c66c0046"      # (cop1)
    "f03e023c"      # lui   $v0,0x3EF0
    "d7a34234"      # ori   $v0,$v0,0xA3D7
    "00708244"      # mtc1  $v0,$f14
    "9c45023c"      # lui   $v0,0x459C
    "00404234"      # ori   $v0,$v0,0x4000
    "0000a2af"      # sw    $v0,0($sp)
    "0045023c"      # lui   $v0,0x4500
    "00788244"      # mtc1  $v0,$f15
)
WIDESCREEN_HOR_SLOT_DELTA = 0
WIDESCREEN_HOR_VANILLA = 0x3C023F80        # lui $v0,0x3F80  -> 1.0f
WIDESCREEN_HOR_PATCHED = 0x3C023F40        # lui $v0,0x3F40  -> 0.75f
# The signature COVERS its own target word, so the vanilla form stops matching
# once patched; this gives a re-run something to match on (IDEMPOTENCE).
WIDESCREEN_HOR_PATCHED_SIGNATURE = _perma_patched_sig(
    WIDESCREEN_HOR_SIGNATURE, WIDESCREEN_HOR_SLOT_DELTA,
    WIDESCREEN_HOR_PATCHED)

# SITE B — PWorld::CullMainWorld. The `lui`/`lwc1` immediates are the pointer
# half-words we want to READ, so they are masked out of the match.
WIDESCREEN_FRUSTUM_PATTERN = (
    (0x3C010000, 0xFFFF0000),   # lui  $at,HI(gViewFrustum)  <-- w0, MASKED
    (0xC42C0000, 0xFFFF0000),   # lwc1 $f12,LO($at)          <-- w1, MASKED
    (0x70809628, 0xFFFFFFFF),
    (0x26440820, 0xFFFFFFFF),
    (0x70A08E28, 0xFFFFFFFF),
    (0x3C010000, 0xFFFF0000),   # lui  $at,HI(gViewFrustum)     MASKED
    (0xC42D0000, 0xFFFF0000),   # lwc1 $f13,LO($at)  (+8)       MASKED
    (0x3C010000, 0xFFFF0000),   # lui  $at,HI(gViewFrustum)     MASKED
    (0xC42E0000, 0xFFFF0000),   # lwc1 $f14,LO($at)  (+0xC)     MASKED
)
WIDESCREEN_ASPECT_FIELD = 4                # gViewFrustum[1] = aspect ratio
WIDESCREEN_ASPECT_VANILLA = 0x3FB33333     # 1.4f
WIDESCREEN_ASPECT_PATCHED = 0x3FEEEEEE     # 1.866666f (1 ULP below 0x3FEEEEEF)
WIDESCREEN_FRUSTUM_FOV = 0x42700000        # field[0] guard: 60.0f
WIDESCREEN_FRUSTUM_FAR = 0x43160000        # field[3] guard: 150.0f


def patch_widescreen(data: bytearray, enabled: bool) -> dict:
    """Render the game at 16:9 instead of 4:3.

    Falsy `enabled` = total no-op; the buffer is left byte-identical. Mutates
    `data` in place and returns a stats dict.

    EIGHT bytes are written, four at each of two sites: the horizontal geometry
    scale in DarkEngine::EngineInit (1.0f -> 0.75f, encoded in a `lui`) and the
    render aspect ratio in `gViewFrustum` (1.4f -> 1.866666f). Both sites are
    located and fully verified before either is written, so a build where only
    one resolves stays untouched. Re-runs are fine: a slot already holding the
    patched word reports success.
    """
    # 1) Argument check first, so a no-op never searches the buffer.
    if not enabled:
        return {"applied": False, "enabled": False, "sites": 0, "slots": []}
    if data[:4] != b"\x7fELF":
        return {"found": False, "applied": False, "enabled": True, "sites": 0,
                "slots": [], "reason": "not ELF"}

    buf = bytes(data)
    seg = _first_load_segment(buf)
    if seg is None:
        return {"found": False, "applied": False, "enabled": True, "sites": 0,
                "slots": [], "reason": "no LOAD segment"}
    load_vaddr, load_off, filesz = seg

    # 2) PHASE ONE — locate and verify BOTH sites. Nothing is written here.
    # SITE A: the `lui` carrying the horizontal scale.
    hor_matches = _find_all(buf, WIDESCREEN_HOR_SIGNATURE) \
        or _find_all(buf, WIDESCREEN_HOR_PATCHED_SIGNATURE)
    if not hor_matches:
        return {"found": False, "applied": False, "enabled": True, "sites": 0,
                "slots": [],
                "reason": "widescreen horizontal-scale signature not found"}
    if len(hor_matches) != 1:
        return {"found": True, "applied": False, "enabled": True, "sites": 0,
                "slots": [],
                "reason": f"widescreen horizontal-scale signature: expected 1 "
                          f"site, found {len(hor_matches)}"}
    hor_slot = hor_matches[0] + WIDESCREEN_HOR_SLOT_DELTA
    if hor_slot + 4 > len(data):
        return {"found": True, "applied": False, "enabled": True, "sites": 0,
                "slots": [],
                "reason": "widescreen horizontal-scale slot past end of buffer"}

    # SITE B: derive `gViewFrustum` from CullMainWorld's lui/lwc1 pair.
    fr_matches = _find_all_masked(buf, WIDESCREEN_FRUSTUM_PATTERN)
    if not fr_matches:
        return {"found": False, "applied": False, "enabled": True, "sites": 0,
                "slots": [],
                "reason": "gViewFrustum reference (PWorld::CullMainWorld) not found"}
    if len(fr_matches) != 1:
        return {"found": True, "applied": False, "enabled": True, "sites": 0,
                "slots": [],
                "reason": f"gViewFrustum reference: expected 1 site, "
                          f"found {len(fr_matches)}"}
    w0 = struct.unpack_from("<I", buf, fr_matches[0])[0]
    w1 = struct.unpack_from("<I", buf, fr_matches[0] + 4)[0]
    frustum_vaddr = (((w0 & 0xFFFF) << 16) + _lt_sext16(w1 & 0xFFFF)) & 0xFFFFFFFF
    frustum_off = load_off + (frustum_vaddr - load_vaddr)
    if not (load_off <= frustum_off < load_off + filesz) \
            or frustum_off + 16 > min(load_off + filesz, len(data)):
        return {"found": True, "applied": False, "enabled": True, "sites": 0,
                "slots": [],
                "reason": f"derived gViewFrustum 0x{frustum_vaddr:X} "
                          f"(file 0x{frustum_off:X}) is outside the LOAD segment"}

    # GUARDS — pin the struct identity: {60.0, aspect, 1.0, 150.0}.
    fov = struct.unpack_from("<I", buf, frustum_off)[0]
    far = struct.unpack_from("<I", buf, frustum_off + 12)[0]
    if fov != WIDESCREEN_FRUSTUM_FOV or far != WIDESCREEN_FRUSTUM_FAR:
        return {"found": True, "applied": False, "enabled": True, "sites": 0,
                "slots": [],
                "reason": f"gViewFrustum guard failed at 0x{frustum_off:X}: "
                          f"expected fov 0x{WIDESCREEN_FRUSTUM_FOV:08X} and far "
                          f"0x{WIDESCREEN_FRUSTUM_FAR:08X}, found 0x{fov:08X} "
                          f"and 0x{far:08X}"}

    aspect_slot = frustum_off + WIDESCREEN_ASPECT_FIELD

    # Verify both words. IDEMPOTENCE: the patched word counts as success.
    hor_cur = struct.unpack_from("<I", buf, hor_slot)[0]
    if hor_cur not in (WIDESCREEN_HOR_VANILLA, WIDESCREEN_HOR_PATCHED):
        return {"found": True, "applied": False, "enabled": True, "sites": 0,
                "slots": [],
                "reason": f"expected horizontal scale 0x{WIDESCREEN_HOR_VANILLA:08X} "
                          f"at 0x{hor_slot:X}, found 0x{hor_cur:08X}"}
    asp_cur = struct.unpack_from("<I", buf, aspect_slot)[0]
    if asp_cur not in (WIDESCREEN_ASPECT_VANILLA, WIDESCREEN_ASPECT_PATCHED):
        return {"found": True, "applied": False, "enabled": True, "sites": 0,
                "slots": [],
                "reason": f"expected aspect ratio 0x{WIDESCREEN_ASPECT_VANILLA:08X} "
                          f"at 0x{aspect_slot:X}, found 0x{asp_cur:08X}"}

    already = (hor_cur == WIDESCREEN_HOR_PATCHED
               and asp_cur == WIDESCREEN_ASPECT_PATCHED)

    # 3) PHASE TWO — both sites verified, so write. 4 bytes each.
    struct.pack_into("<I", data, hor_slot, WIDESCREEN_HOR_PATCHED)
    struct.pack_into("<I", data, aspect_slot, WIDESCREEN_ASPECT_PATCHED)
    return {"found": True, "applied": True, "enabled": True, "sites": 2,
            "slots": [hor_slot, aspect_slot],
            "frustum_vaddr": frustum_vaddr,
            "hor_vaddr": load_vaddr + (hor_slot - load_off),
            "already_patched": already}


# ============================================================================
# Level randomization (cross-world level loading via LevelTable)
# ============================================================================
# The engine has a master `LevelTable`: an array of `LevelFiles` entries, one
# per level slot, stride 0x34 (52 bytes). Recovered from the demo debug
# symbols (LevelTable @ demo vaddr 0x1e98a0). Each entry:
#   +0x00  u32  PSX path ptr      (e.g. "PSXDATA\GRAVE\G_SUB1.PSX")
#   +0x04  u32  sky PSX ptr
#   +0x08  u32  directory ptr
#   +0x0c  u32  extra ptr (usually 0)
#   +0x10  u32  world-code<<24 | global-index<<16 | flags
#   +0x14..+0x1c  camera / params
#   +0x20  u32  0
#   +0x24  u32  ability-table ptr
#   +0x28  u32  level-id (world<<16 | type<<8 | local-index)  <- IDENTITY
#   +0x2c  u32  flag
#   +0x30  u32  progression ptr
#
# To randomize levels without breaking hub/save progression we shuffle ONLY the
# file-pointer block [0x00:0x10] (PSX, sky, dir, extra — the 16 bytes that decide
# which geometry/textures load) among the playable sub-levels (paths matching
# "_SUB\d"). Everything else stays with the slot, so the hub still treats the
# column as its original level and progression advances normally.
#
# IMPORTANT — the shuffle is constrained to WITHIN EACH WORLD. Cross-world
# loading (e.g. a Swamp level in a Grave slot) hangs the engine on an infinite
# "loading" screen: the level's per-world resource context (BEF enemy/instance
# definitions, sound-effect bank at entry +0x13, world streaming set) is bound
# to the hub's world, not to the file pointers, so a foreign level waits forever
# on resources that never arrive. Keeping the shuffle inside one world means the
# loaded geometry always matches the already-resident world assets, so it loads
# cleanly. World membership is taken from the PSX path's folder
# (GRAVE/SWAMP/ICESHIP/UNDER/CASTLE).
#
# The table is located by STRUCTURAL signature (region-portable): a dword array
# at stride 0x34 whose +0x00 dword points at a ".PSX"-ending string for >=10
# consecutive entries. Verified: US file 0xcefc0, JP file 0xd19a0,
# demo file 0xe9920.
LEVEL_TABLE_STRIDE = 0x34
LEVEL_TABLE_MIN_ENTRIES = 10
LEVEL_FILE_BLOCK = 0x10            # PSX(+0x00) sky(+0x04) dir(+0x08) extra(+0x0c)
LEVEL_GEO_BLOCK = 0x08            # PSX(+0x00) + sky(+0x04) only (no dir change)
LEVEL_DEF_BLOCK = 0x28
# Byte ranges (offset, length) that a level swap copies from the source entry to
# the destination entry. We move:
#   [0x00:0x10]  the four file pointers (PSX / sky / directory / extra)
#   [0x14:0x1e]  the level's camera + player-start/world-bounds params (read by
#                InitWorld on level load) — this is what makes the player spawn
#                on the loaded geometry / inside the right kill-plane.
# We deliberately do NOT touch:
#   [0x10:0x14]  world/completion/music/sfx bytes,
#   [0x1e:0x20]  the LOAD-CLASS LIMIT (mglRunLevel feeds +0x1e to
#                SetLoadClassLimit) — a foreign value here makes the loader wait
#                on a class count that never matches and hangs the title /
#                attract "loading" screen,
#   [0x20:0x24]  an aux param,
#   [0x24:0x28]  the per-level drop/ability-table POINTER, and
#   [0x28:0x34]  the identity/flag/progression fields.
LEVEL_SWAP_RANGES = ((0x00, 0x10), (0x14, 0x0a))
_LEVEL_PSX_STR_RE = re.compile(rb"\x00([ -~]{3,}\.PSX)\x00")
# Boss-arena entries (scripted encounters): *_BOSS / *_KING / *_QUEEN. A world
# whose block has a boss-arena anywhere except the final slot has a non-standard
# layout (Castle: KING then QUEEN) and is EXCLUDED from the whole-world swap, so
# we never drop a boss arena into a normal level slot (which hangs the loader).
_LEVEL_BOSS_RE = re.compile(rb"_(?:BOSS|KING|QUEEN)\d*\.PSX$", re.IGNORECASE)
_LEVEL_SUBLEVEL_RE = re.compile(rb"_SUB\d", re.IGNORECASE)
# Extract the world folder from a path like "PSXDATA\GRAVE\G_SUB1.PSX".
_LEVEL_WORLD_RE = re.compile(rb"[\\/]([A-Za-z0-9]+)[\\/][^\\/]+\.PSX", re.IGNORECASE)


def find_level_table(data: bytearray | bytes):
    """Locate the engine's LevelTable by structural signature.

    Returns (table_file_offset, entry_count, (load_vaddr, load_off, filesz))
    or None if not found. Region-portable (no hard-coded offsets): scans the
    first PT_LOAD segment for a stride-0x34 dword array whose +0x00 entry points
    to a ".PSX"-terminated string for at least LEVEL_TABLE_MIN_ENTRIES rows."""
    b = bytes(data)
    if b[:4] != b"\x7fELF":
        return None
    seg = _first_load_segment(b)
    if seg is None:
        return None
    load_vaddr, load_off, filesz = seg
    seg_end = min(load_off + filesz, len(b))
    # Collect vaddrs of ".PSX"-ending strings within the loaded image.
    psx_vaddrs: set[int] = set()
    for m in _LEVEL_PSX_STR_RE.finditer(b):
        s_off = m.start() + 1               # string starts after the leading NUL
        if load_off <= s_off < seg_end:
            psx_vaddrs.add(load_vaddr + (s_off - load_off))
    if not psx_vaddrs:
        return None
    # Scan for a stride-0x34 array whose +0x00 points at a PSX string.
    o = load_off
    limit = seg_end - 4
    while o <= limit:
        count = 0
        while True:
            eo = o + count * LEVEL_TABLE_STRIDE
            if eo + 4 > seg_end:
                break
            ptr = struct.unpack_from("<I", b, eo)[0]
            if ptr in psx_vaddrs:
                count += 1
            else:
                break
        if count >= LEVEL_TABLE_MIN_ENTRIES:
            return o, count, seg
        o += 4
    return None


def patch_randomize_levels(data: bytearray, seed: int | None,
                           cross_world: bool = False) -> dict:
    """Randomize which level(s) load. Mutates `data` (SLUS/SLPM bytes) in place.

    cross_world=False (default): shuffle the playable sub-levels WITHIN each
    world (paths matching "_SUB\\d"), swapping only their file-pointer block
    [0x00:0x10]. Same-world geometry always matches the resident world assets
    (BEF/PRS/PRT/textures/sfx), so levels load cleanly. Slot identity (+0x28)
    and progression (+0x30) are untouched, so the hub/save still treat each
    column as its original level.

    cross_world=True (WHOLE-WORLD SWAP): permute ENTIRE worlds. Each world in
    the LevelTable is a contiguous run of entries (intro, hub, sub-levels,
    boss). We seed-permute these world blocks and, position-by-position, copy
    each entry's file pointers (and, for sub-levels/boss, its camera/spawn
    params too) from the source world to the destination world, AND repoint
    the destination's static-data index (+0x28 upper 16 bits) at the source
    world's resource set (BEF/PRS/PRT) so the loaded geometry's entities
    resolve against the right templates — without this, every swapped entry
    silently ran on the wrong world's resource set (see set_static_data_index
    above). Intro/intro-B and hub get file-pointer-only swaps (their own
    camera/spawn params stay put); sub-levels/boss get the full swap. Worlds
    with a non-standard boss layout (boss-arena filename outside the final
    slot, e.g. Castle's C_KING/C_QUEEN) are excluded from the permutable set
    entirely. Slot identity/progression fields are left in place so the
    world-map/save still track each slot."""
    res = find_level_table(data)
    if res is None:
        return {"found": False, "applied": False, "reason": "LevelTable not found"}
    tbl_off, count, seg = res
    load_vaddr, load_off, filesz = seg
    b = bytes(data)

    def read_path(eo: int) -> bytes:
        ptr = struct.unpack_from("<I", b, eo)[0]
        s_off = load_off + (ptr - load_vaddr)
        if not (load_off <= s_off < load_off + filesz):
            return b""
        nul = b.find(b"\x00", s_off)
        return b[s_off:nul if nul >= 0 else s_off]

    def world_of(eo: int) -> str:
        wm = _LEVEL_WORLD_RE.search(read_path(eo))
        return wm.group(1).decode("ascii", "replace").upper() if wm else "?"

    rng = random.Random((seed if seed is not None else 0) ^ 0x1E7E1AB1)

    def _is_standard_boss_layout(entries) -> bool:
        """True iff a boss-arena-style filename (_BOSS/_KING/_QUEEN) only
        appears in this block's FINAL entry. Worlds whose boss content is
        split across multiple non-final slots (Castle: C_KING then C_QUEEN,
        both matching the boss pattern, with C_KING in slot 4 of 6) have a
        non-standard layout: position-by-position copying would drop a
        normal sub-level's file/camera data into a scripted boss-arena slot
        (or vice versa), which hangs the loader. Such blocks are excluded
        from the permutable set entirely (documented intent above —
        previously unenforced: _LEVEL_BOSS_RE existed but was never called)."""
        for pos, eo in enumerate(entries):
            path = read_path(eo).upper()
            if _LEVEL_BOSS_RE.search(path) and pos != len(entries) - 1:
                return False
        return True

    def copy_entry(dst_eo: int, src_full: bytes) -> None:
        """Copy the swappable ranges (files + camera/spawn params) from a source
        entry snapshot into the destination entry, leaving identity / pointer /
        boot-read fields untouched."""
        for roff, rlen in LEVEL_SWAP_RANGES:
            data[dst_eo + roff:dst_eo + roff + rlen] = src_full[roff:roff + rlen]

    def set_static_data_index(dst_eo: int, src_full: bytes) -> None:
        """Intentionally a no-op.

        A previous version wrote the upper 16 bits of +0x28 from the source
        entry into the destination entry, believing +0x28 to be a static-data
        resource index. This was a misread of the disassembly.

        +0x28 is the level IDENTITY word (world<<16 | type<<8 | local-index),
        documented in LEVEL_TABLE layout above and confirmed in the LevelTable
        dump. Writing a foreign world's value here corrupts the slot identity,
        breaking the title-screen attract-mode loader: it reads this field to
        select the resource context, gets a mismatched value, and hangs or
        causes the PS2 to reboot with repeated CDVD Read Aborts.

        The field MUST stay with the destination slot. Cross-world resource
        loading is handled by BEF injection (inject_cross_world_enemies in
        bef.py), not by patching this identity field.
        """
        pass  # deliberately do nothing — +0x28 must never be modified

    # ---------------------------------------------------------------- WORLD SWAP
    if cross_world:
        # Build contiguous runs of entries that share a world folder; each run
        # is one world block (intro/hub/subs/boss).
        runs = []  # list of [entry_offset, ...]
        run_world = []
        prev = None
        for i in range(count):
            eo = tbl_off + i * LEVEL_TABLE_STRIDE
            w = world_of(eo)
            if w == prev and runs:
                runs[-1].append(eo)
            else:
                runs.append([eo])
                run_world.append(w)
            prev = w
        # The five main worlds are equal-length blocks; pick the dominant block
        # length and permute only those (skips the 1-entry bonus run, etc.).
        from collections import Counter
        lengths = Counter(len(r) for r in runs)
        block_len = max(lengths, key=lambda L: (lengths[L], L))

        candidate_idxs = [i for i, r in enumerate(runs) if len(r) == block_len]
        excluded_worlds = [run_world[i] for i in candidate_idxs
                           if not _is_standard_boss_layout(runs[i])]
        blocks = [(run_world[i], runs[i]) for i in candidate_idxs
                  if _is_standard_boss_layout(runs[i])]
        if len(blocks) < 2:
            return {"found": True, "applied": False, "table": tbl_off,
                    "entries": count, "cross_world": True,
                    "excluded_worlds": excluded_worlds,
                    "reason": "could not identify swappable world blocks"}
        # Snapshot each world block's full entries, then permute the blocks and
        # copy position-by-position (intro->intro, hub->hub, sub->sub,
        # boss->boss for same-shaped worlds). Position-by-position keeps a
        # one-to-one mapping (no duplicate level-table entries), which the boot
        # / save-init needs — duplicating an entry hangs the title menu.
        src_blocks = [[bytes(data[eo:eo + LEVEL_TABLE_STRIDE]) for eo in r]
                      for _, r in blocks]
        order = list(range(len(blocks)))
        for _ in range(12):
            rng.shuffle(order)
            if all(order[i] != i for i in range(len(order))):
                break
        mapping = []
        applied = False
        for dst_i, src_i in enumerate(order):
            dst_world, dst_entries = blocks[dst_i]
            src_world = blocks[src_i][0]
            for pos, dst_eo in enumerate(dst_entries):
                src_full = src_blocks[src_i][pos]
                _p = read_path(dst_eo).upper()
                if b"_INTRO" in _p:
                    # INTRO / INTRO-B: file-pointer swap only (camera/spawn
                    # params stay the destination's own — same caution as
                    # HUB below), PLUS the static-data-index fix. Previously
                    # these were left fully untouched because the title-
                    # screen attract-mode hang was (incorrectly) attributed to
                    # them; that demo path actually uses hardcoded literal
                    # filenames unrelated to this table (confirmed in the
                    # ELF disassembly), and the real cause — a missing
                    # static-data-index swap — is fixed below, so intro can
                    # now safely take part in the whole-world swap.
                    data[dst_eo:dst_eo + LEVEL_FILE_BLOCK] = src_full[0:LEVEL_FILE_BLOCK]
                    set_static_data_index(dst_eo, src_full)
                elif b"_HUB" in _p:
                    # The HUB is the in-world level-select "menu" — file-
                    # pointer-only swap (keep the host's camera/spawn params)
                    # plus the static-data-index fix.
                    data[dst_eo:dst_eo + LEVEL_FILE_BLOCK] = src_full[0:LEVEL_FILE_BLOCK]
                    set_static_data_index(dst_eo, src_full)
                else:
                    copy_entry(dst_eo, src_full)
                    set_static_data_index(dst_eo, src_full)
            if src_i != dst_i:
                applied = True
            mapping.append((dst_world, src_world))
        return {"found": True, "applied": applied, "table": tbl_off,
                "entries": count, "cross_world": True,
                "block_len": block_len,
                "worlds": [w for w, _ in blocks],
                "excluded_worlds": excluded_worlds,
                "mapping": mapping}

    # ------------------------------------------------------- WITHIN-WORLD SHUFFLE
    groups: dict[str, list] = {}   # world -> [(entry_offset, file_block, path)]
    for i in range(count):
        eo = tbl_off + i * LEVEL_TABLE_STRIDE
        path = read_path(eo)
        if not _LEVEL_SUBLEVEL_RE.search(path):
            continue
        wm = _LEVEL_WORLD_RE.search(path)
        world = wm.group(1).decode("ascii", "replace").upper() if wm else "?"
        groups.setdefault(world, []).append(
            (eo, bytes(data[eo:eo + LEVEL_TABLE_STRIDE]), path))

    total_subs = sum(len(v) for v in groups.values())
    if total_subs < 2:
        return {"found": True, "applied": False, "table": tbl_off,
                "entries": count, "sublevels": total_subs,
                "reason": "not enough sub-levels to shuffle"}

    mapping = []
    shuffled_any = False
    for world in sorted(groups):
        subs = groups[world]
        if len(subs) < 2:
            continue
        order = list(range(len(subs)))
        for _ in range(8):
            rng.shuffle(order)
            if any(order[i] != i for i in range(len(order))):
                break
        for dst_i, src_i in enumerate(order):
            dst_eo = subs[dst_i][0]
            src_full = subs[src_i][1]
            # BUG FIX (investigated after reports of infinite loading on Dead
            # Heat / Quick and the Dead / Crushed Spirits with the GUI's
            # "always on" within-world level shuffle): this loop used to call
            # copy_entry(), which copies LEVEL_SWAP_RANGES = [0x00:0x10] AND
            # [0x14:0x1e] -- the camera/player-start/world-bounds params --
            # even though the docstring above explicitly says the within-
            # world shuffle "swaps only their file-pointer block [0x00:0x10]".
            # Ground-truth disassembly of InitWorld (MAXDEMOR.ELF debug build,
            # research/CROSSWORLD_TEXTURE_RESEARCH.md) confirms the engine
            # does real per-level math on the 0x14:0x1e halfwords (grid-size
            # division, conditional branches) -- two DIFFERENT sub-levels in
            # the same world can have geometry-specific bounds here, so
            # swapping them (even within one world) can hand a level bounds
            # that don't match its own geometry and hang on load. Restricting
            # this path to the file-pointer block only (as documented) avoids
            # that; cross_world=True still needs the fuller copy_entry() copy
            # since there the geometry itself is also being swapped.
            data[dst_eo:dst_eo + LEVEL_FILE_BLOCK] = src_full[0:LEVEL_FILE_BLOCK]
            if src_i != dst_i:
                shuffled_any = True
            mapping.append((subs[dst_i][2].decode("ascii", "replace"),
                            subs[src_i][2].decode("ascii", "replace")))
    return {"found": True, "applied": shuffled_any, "table": tbl_off,
            "entries": count, "sublevels": total_subs,
            "cross_world": False,
            "worlds": {w: len(v) for w, v in groups.items()},
            "mapping": mapping}


# ============================================================================
# ON-SCREEN OBJECT BUDGET — the script VM's per-type "load class" weight limit
# ============================================================================
# WHAT THE CAP IS
# ---------------------------------------------------------------------------
# `CVirtualMachine::ExecuteApparitions` keeps a RUNNING PER-CLASS TOTAL
# (`mpuCurLoadTable`, this+0xD4) and compares it against a PER-CLASS LIMIT
# (`muLoadLimitTable`, this+0x118). When
#       limit < curLoad + entityCost
# it branches PAST `CVmExecutionEngine::ExecuteApparition`, so that entity's
# script never runs at all: no collision, no damage, invisible. That is the
# "objects stop appearing once the level gets busy" behaviour.
#
# The budget is in WEIGHT UNITS, NOT OBJECT COUNTS. Each entity contributes its
# own `mpEntityInfo+0x58` cost, so a limit of 100 is not "100 objects" — heavy
# entities eat more of the budget than light ones.
#
# The seven load classes:
#   0 Invalid   1 Objects/Pickup   2 Level Mechanics   3 Enemies
#   4 Extras    5 Triggers         6 Always Present
#
# TWO INDEPENDENT SITES — BOTH ARE REQUIRED
# ---------------------------------------------------------------------------
# SITE A — the constructor default in `CVirtualMachine::__ct__`. ONE
# `addiu $a0,$zero,100` immediate feeds five consecutive `sw` stores to
# this+0x11C/0x120/0x124/0x128/0x12C (classes 1..5), and the ADJACENT
# `addiu $v1,$zero,1000` feeds this+0x130 (class 6, Always Present). Raising the
# single 100 immediate lifts Objects/Pickup (chests!), Level Mechanics, Extras
# and Triggers together. The 1000 is left ALONE — it is the always-present
# class and has nothing to do with the visible-population problem.
#
#   Retail SLUS_200.17 file offsets (reference/cross-check ONLY — the patch
#   locates the site by the 15-word signature below): the 100 at 0x272EC, the
#   1000 at 0x272F0. Demo vaddrs: 0x1037DC / 0x1037E0.
#
# SITE B — the PER-LEVEL ENEMY OVERRIDE. `mglRunLevel` does
#       lh $a2,0x1E($s5)            ; LevelFiles->enemyLoadLimit
#       jal SetLoadClassLimit       ; with addiu $a1,$zero,3
# at EVERY level load, so the LevelTable value OVERWRITES class 3 and therefore
# WINS over site A for enemies. Patching site A alone visibly does NOTHING for
# enemies. Both sites are patched together or the feature is refused.
#
#   The values live in the `LevelTable` DATA rows at +0x1E (u16), located with
#   this module's existing `find_level_table` (structural, region-portable) —
#   deliberately NOT a second locator and NOT a hardcoded offset. Reference:
#   US file 0xCEFC0, JP file 0xD19A0, 31 rows of stride 0x34.
#
#   Vanilla is 100 on every row EXCEPT row 24 (a Castle level) at 80. That 80 is
#   a deliberate designer choice, so rows are SCALED PROPORTIONALLY
#   (round(row * budget / 100)) rather than flattened to one number — the tighter
#   level stays relatively tighter than its neighbours.
#
# `lh` is SIGNED (sign-extending halfword) and site A's immediate is a 16-bit
# signed field, so 32767 is the encoding ceiling. We stay FAR below it — see
# below for why.
#
# THE SAFE VALUE, AND WHY 150
# ---------------------------------------------------------------------------
# The fixed-capacity arrays this budget indirectly protects:
#   RENDER_SIZE = 320               `m_DynamicRenderList`, DarkEngine+0x6B40
#                                   — per-FRAME-VISIBLE
#   ACTIVE_SIZE = 384               `mActiveObjList`, DarkEngine+0x6B50
#   DEF_MAX_NUM_APPARITIONS = 384   static u8[4][384] apparition data
#                                   — per-ENTITY-IN-LEVEL
# Retail compiles the asserts OUT, so an overflow is a SILENT memory stomp.
#
# Occupancy: the densest level is G_SUB1 at 304 records, of which ~240 are
# plausibly renderable. The randomizer NEVER changes the record COUNT — it
# retypes in place, one replacement per source offset — so the apparition figure
# is unchanged from vanilla by any randomizer option. A level's own entity
# population, not this budget, is the real ceiling: raising the budget can only
# let MORE OF THE LEVEL'S OWN entities run, it cannot create new ones.
#
# That is why 100 -> 150 cannot open a memory-stomp path on vanilla geometry.
# Going PAST 150 erodes the cushion between the densest level's renderable
# population and RENDER_SIZE = 320, and the real limiting bound for adding
# entities is DEF_MAX_NUM_APPARITIONS = 384. Anything above OBJECT_BUDGET_SAFE_MAX is
# EXPLICITLY EXPERIMENTAL; above OBJECT_BUDGET_MAX the patch REFUSES with a
# reason rather than silently clamping.
# ============================================================================

OBJECT_BUDGET_VANILLA = 100     # the engine's own limit — no patch applied
OBJECT_BUDGET_MIN = OBJECT_BUDGET_VANILLA   # lowering it is not the feature
# Hard ceiling for the patcher, and NOT the encoding's limit (that is 32767).
# 150 is the defensible value (see the section comment); 151..200 is accepted as
# explicitly experimental, and anything above is REFUSED because it erodes the
# cushion between the densest level's renderable population and RENDER_SIZE=320.
OBJECT_BUDGET_MAX = 200
# Highest value verified safe against RENDER_SIZE=320 on vanilla geometry. It is
# no longer a preset — it is retained purely as the threshold above which the
# patch reports itself as experimental.
OBJECT_BUDGET_SAFE_MAX = 150

# The editor exposes NAMED PRESETS, not a raw integer. Insertion order is
# ascending on purpose: the CLI/GUI list them in this order.
#   vanilla 100  the engine's own budget — no patch is applied
#   more    200  play-tested, explicitly experimental — the patcher's ceiling
OBJECT_BUDGET_PRESETS = {
    "vanilla": OBJECT_BUDGET_VANILLA,
    "more": OBJECT_BUDGET_MAX,
}

# Accepted spellings that are NOT offered anywhere (absent from the combobox and
# from every help string) — retained only so a settings profile saved by an older
# build still loads. `much_more` was the 200 preset before it became `more`.
OBJECT_BUDGET_PRESET_ALIASES = {
    "much_more": OBJECT_BUDGET_MAX,
}

# SITE A signature — 15 fixed words, no $gp displacements and no branch offsets,
# so plain word matching is exact. Word 0 is the TARGET immediate; word 1 is the
# adjacent 1000 we must NOT touch; words 2..14 are the `sw` sequence that PROVES
# this is the load-limit block (displacements 0x11C/0x120/0x124/0x128/0x12C and
# finally 0x130). Matches EXACTLY ONCE in SLUS_200.17 (0x272EC) and ONCE in
# SLPM_621.27 (0x272EC).
OBJECT_BUDGET_CTOR_PATTERN = (
    (0x24040064, 0xFFFFFFFF),   # addiu $a0,$zero,100     <-- TARGET
    (0x240303E8, 0xFFFFFFFF),   # addiu $v1,$zero,1000    (class 6 — untouched)
    (0xAC4500D0, 0xFFFFFFFF),   # sw    $a1,0xD0($v0)
    (0x8FA2001C, 0xFFFFFFFF),   # lw    $v0,0x1C($sp)
    (0xAC44011C, 0xFFFFFFFF),   # sw    $a0,0x11C($v0)    class 1 Objects/Pickup
    (0x8FA2001C, 0xFFFFFFFF),
    (0xAC440120, 0xFFFFFFFF),   # sw    $a0,0x120($v0)    class 2 Level Mechanics
    (0x8FA2001C, 0xFFFFFFFF),
    (0xAC440124, 0xFFFFFFFF),   # sw    $a0,0x124($v0)    class 3 Enemies
    (0x8FA2001C, 0xFFFFFFFF),
    (0xAC440128, 0xFFFFFFFF),   # sw    $a0,0x128($v0)    class 4 Extras
    (0x8FA2001C, 0xFFFFFFFF),
    (0xAC44012C, 0xFFFFFFFF),   # sw    $a0,0x12C($v0)    class 5 Triggers
    (0x8FA2001C, 0xFFFFFFFF),
    (0xAC430130, 0xFFFFFFFF),   # sw    $v1,0x130($v0)    class 6 (the 1000)
)
# Same 15 words with word 0's immediate wildcarded, so an ALREADY-PATCHED
# executable is still located on a re-run (see patch_widescreen / the koin fee
# label for the same idempotence stance).
OBJECT_BUDGET_CTOR_PATCHED_PATTERN = (
    (0x24040000, 0xFFFF0000),
    *OBJECT_BUDGET_CTOR_PATTERN[1:],
)
OBJECT_BUDGET_CTOR_SLOT_DELTA = 0
OBJECT_BUDGET_CTOR_ADDIU_BASE = 0x24040000  # addiu $a0,$zero,<imm>
OBJECT_BUDGET_CTOR_EXPECTED = 0x24040064    # addiu $a0,$zero,100   (vanilla)
OBJECT_BUDGET_ALWAYS_WORD = 0x240303E8      # the adjacent 1000 — guarded, never written

# LevelTable row field holding the per-level ENEMY class limit (u16, read by
# mglRunLevel's `lh $a2,0x1E($s5)`).
OBJECT_BUDGET_LEVEL_FIELD = 0x1E
# Sanity window for a row's CURRENT value. Anything outside it means we are not
# looking at the field we think we are, and the patch refuses.
OBJECT_BUDGET_LEVEL_MIN = 1


def resolve_object_budget(value) -> int | None:
    """Preset label (or int, or None) -> the integer patch_object_budget wants.

    Single source of truth for both edges (the two argparse layers and the GUI
    combobox) so the label -> value mapping is never duplicated. Returns None for
    None, "vanilla", blank, and anything unrecognised — i.e. "leave the
    executable's own budget alone". Labels are case-insensitive.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    label = str(value).strip().lower()
    if not label or label == "vanilla":
        return None
    if label in OBJECT_BUDGET_PRESETS:
        return OBJECT_BUDGET_PRESETS[label]
    return OBJECT_BUDGET_PRESET_ALIASES.get(label)


def patch_object_budget(data: bytearray, budget: int | None) -> dict:
    """Raise the script VM's per-type on-screen object budget.

    budget: OBJECT_BUDGET_MIN..OBJECT_BUDGET_MAX. None or
    OBJECT_BUDGET_VANILLA (100) = no change, and in that case the executable is
    left byte-identical by this feature. Mutates `data` in place and returns a
    stats dict.

    Writes TWO things, and both or neither:
      SITE A  the `addiu $a0,$zero,100` immediate in CVirtualMachine::__ct__
              that feeds the class 1..5 limit stores (the adjacent 1000 for
              class 6 is guarded and never written), and
      SITE B  the LevelTable `+0x1E` per-level ENEMY limit of every row, SCALED
              PROPORTIONALLY so the one deliberately tighter Castle level stays
              relatively tighter.
    Site B is mandatory, not optional: mglRunLevel re-applies it to class 3 at
    every level load, so patching site A alone does nothing for enemies.

    The budget is a WEIGHT total, not an object count. Values above
    OBJECT_BUDGET_MAX are REFUSED (not clamped) — see the section comment.

    Re-runs are supported: site A's current immediate is treated as the previous
    budget and the rows are un-scaled through it, so applying the patch twice, or
    changing the value, lands on exactly the same bytes. `already_patched` says
    which happened.
    """
    # 1) Argument validation first, so an invalid budget never searches the buffer.
    none_res = {"applied": False, "budget": None, "sites": 0, "slots": [],
                "level_rows": 0}
    if budget is None:
        return dict(none_res)
    if not isinstance(budget, int) or isinstance(budget, bool):
        return {**none_res, "budget": budget,
                "reason": f"budget must be an integer {OBJECT_BUDGET_MIN}.."
                          f"{OBJECT_BUDGET_MAX}"}
    if budget == OBJECT_BUDGET_VANILLA:
        return {**none_res, "budget": budget,
                "reason": f"{OBJECT_BUDGET_VANILLA} is vanilla"}
    if budget < OBJECT_BUDGET_MIN:
        return {**none_res, "budget": budget,
                "reason": f"budget must be {OBJECT_BUDGET_MIN}.."
                          f"{OBJECT_BUDGET_MAX}: this feature RAISES the "
                          f"engine's own limit, it does not lower it"}
    if budget > OBJECT_BUDGET_MAX:
        return {**none_res, "budget": budget,
                "reason": f"budget must be {OBJECT_BUDGET_MIN}.."
                          f"{OBJECT_BUDGET_MAX}: above {OBJECT_BUDGET_MAX} the "
                          f"cushion between the densest level's renderable "
                          f"population and the engine's fixed RENDER_SIZE=320 "
                          f"list is gone, and retail compiles the overflow "
                          f"asserts out (silent memory stomp). "
                          f"{OBJECT_BUDGET_SAFE_MAX} is the verified-safe value"}
    if data[:4] != b"\x7fELF":
        return {**none_res, "found": False, "budget": budget,
                "reason": "not ELF"}

    buf = bytes(data)

    # 2) PHASE ONE — locate and verify BOTH sites. Nothing is written here.
    # SITE A: the constructor immediate.
    matches = _find_all_masked(buf, OBJECT_BUDGET_CTOR_PATTERN) \
        or _find_all_masked(buf, OBJECT_BUDGET_CTOR_PATCHED_PATTERN)
    if not matches:
        return {**none_res, "found": False, "budget": budget,
                "reason": "load-class limit constructor signature "
                          "(CVirtualMachine::__ct__) not found"}
    if len(matches) != 1:
        return {**none_res, "found": True, "budget": budget,
                "reason": f"load-class limit constructor signature: expected 1 "
                          f"site, found {len(matches)}"}
    ctor_slot = matches[0] + OBJECT_BUDGET_CTOR_SLOT_DELTA
    if ctor_slot + 4 > len(data):
        return {**none_res, "found": True, "budget": budget,
                "reason": "load-class limit slot past end of buffer"}
    # IDEMPOTENCE: the vanilla word OR one of our own earlier writes (same
    # opcode/registers, any immediate) is accepted.
    ctor_word = struct.unpack_from("<I", buf, ctor_slot)[0]
    if ctor_word != OBJECT_BUDGET_CTOR_EXPECTED \
            and (ctor_word & 0xFFFF0000) != OBJECT_BUDGET_CTOR_ADDIU_BASE:
        return {**none_res, "found": True, "budget": budget,
                "reason": f"expected load-class limit word "
                          f"0x{OBJECT_BUDGET_CTOR_EXPECTED:08X} at "
                          f"0x{ctor_slot:X}, found 0x{ctor_word:08X}"}
    # GUARD — the adjacent class-6 (Always Present) 1000 must be intact. This is
    # what pins the pair, and we never write it.
    always = struct.unpack_from("<I", buf, ctor_slot + 4)[0]
    if always != OBJECT_BUDGET_ALWAYS_WORD:
        return {**none_res, "found": True, "budget": budget,
                "reason": f"class-6 guard failed at 0x{ctor_slot + 4:X}: "
                          f"expected 0x{OBJECT_BUDGET_ALWAYS_WORD:08X}, found "
                          f"0x{always:08X}"}
    prev_budget = ctor_word & 0xFFFF
    if prev_budget < OBJECT_BUDGET_MIN:
        return {**none_res, "found": True, "budget": budget,
                "reason": f"load-class limit at 0x{ctor_slot:X} holds "
                          f"{prev_budget}, which is below vanilla "
                          f"{OBJECT_BUDGET_VANILLA} — refusing to guess what "
                          f"patched it"}

    # SITE B: the per-level ENEMY overrides in the LevelTable. Reuses this
    # module's existing structural locator — deliberately not a second one.
    found = find_level_table(buf)
    if found is None:
        return {**none_res, "found": False, "budget": budget,
                "reason": "LevelTable not found, so the per-level enemy limit "
                          "cannot be raised; refusing to patch the constructor "
                          "alone because mglRunLevel would overwrite the enemy "
                          "class at every level load"}
    tbl_off, count, _seg = found
    rows: list[tuple[int, int, int]] = []       # (offset, before, after)
    for i in range(count):
        off = tbl_off + i * LEVEL_TABLE_STRIDE + OBJECT_BUDGET_LEVEL_FIELD
        if off + 2 > len(data):
            return {**none_res, "found": True, "budget": budget,
                    "reason": f"LevelTable row {i} enemy-limit field past end "
                              f"of buffer"}
        before = struct.unpack_from("<H", buf, off)[0]
        # `lh` is SIGNED, so the field must stay well inside 0..32767; a value
        # outside the sane window means this is not the field we think it is.
        if not (OBJECT_BUDGET_LEVEL_MIN <= before
                <= OBJECT_BUDGET_MAX * prev_budget // OBJECT_BUDGET_VANILLA):
            return {**none_res, "found": True, "budget": budget,
                    "reason": f"LevelTable row {i} enemy limit at 0x{off:X} is "
                              f"{before}, outside the expected "
                              f"{OBJECT_BUDGET_LEVEL_MIN}.."
                              f"{OBJECT_BUDGET_MAX} window — refusing to write"}
        # Un-scale through the PREVIOUS budget (100 on a vanilla file) so the
        # designers' own per-level proportions survive any number of re-runs,
        # then re-scale to the requested one. Row 24's deliberately tighter 80
        # stays proportionally tighter instead of being flattened.
        vanilla_row = round(before * OBJECT_BUDGET_VANILLA / prev_budget)
        after = round(vanilla_row * budget / OBJECT_BUDGET_VANILLA)
        rows.append((off, before, after))

    # 3) PHASE TWO — both sites verified, so write. Site A is 2 bytes (the
    # immediate half of the word), site B is 2 bytes per changed row.
    struct.pack_into("<I", data, ctor_slot,
                     OBJECT_BUDGET_CTOR_ADDIU_BASE | (budget & 0xFFFF))
    changed: list[dict] = []
    for off, before, after in rows:
        if after != before:
            struct.pack_into("<H", data, off, after)
            changed.append({"offset": off, "before": before, "after": after})

    return {"found": True, "applied": True, "budget": budget,
            "sites": 2, "slots": [ctor_slot],
            "ctor_slot": ctor_slot,
            "level_table": tbl_off, "level_entries": count,
            "level_rows": len(changed), "level_changes": changed,
            "already_patched": prev_budget != OBJECT_BUDGET_VANILLA,
            "previous_budget": prev_budget}


# ============================================================================
# RESOURCE MEMORY POOL — raise the engine's second heap
# ============================================================================
# WHAT THE ENGINE DOES
# ---------------------------------------------------------------------------
# `MemoryManager::__ct` is 0xC8 bytes at vaddr 0x001AB4B0 (SLUS_200.17) /
# 0x001AB7A0 (SLPM_621.27). It malloc's ONE arena and TILES three MemoryPool
# objects across it. Construction order is pool[0], pool[2], pool[1], at
# this+0x020, this+0x260 and this+0x140 (stride 0x120):
#
#   member            size                  how it is materialised
#   pool[0] MAIN      arena - 0x70B030      lui $v0,0xff8f + ori $v0,$v0,0x4fd0
#                     (runtime, ~24 MB)     (= -0x70B030) then addu $a2,$s1,$v0
#   pool[2] RESOURCE  0x00640000 (6.25 MiB) a SINGLE `lui $a2,0x0064` — the low
#                                           half is implicitly 0, there is NO
#                                           paired ori/addiu
#   pool[1]           0x000CB000 (812 KiB)  lui $v0,0x000c + ori $a2,$v0,0xb000
#
# THE INVARIANT — WHY BOTH EDITS ARE MANDATORY
# ---------------------------------------------------------------------------
#       0x70B030 == 0x640000 + 0xCB000 + 0x30
#
# i.e. pool[0]'s reserve is EXACTLY the sum of the other two pool sizes plus
# 0x30 (three 16-byte guard/rounding allowances — `MemoryPool::__ct` rounds each
# size up via `(size+15)>>4`). The three pools therefore tile the arena with no
# slack. Raising pool[2] WITHOUT also raising the reserve constructs pool[1]
# past the end of the allocation and writes its free-list header OUT OF BOUNDS.
# That is silent heap corruption, NOT a rejection. The two edits are mandatory
# together, which is why this patcher is ATOMIC: both sites verified in phase
# one, both written in phase two, or nothing is written at all.
#
# THERE IS NO ENGINE-SIDE CHECK
# ---------------------------------------------------------------------------
# There is NO bounds check, NO comparison and NO failure path anywhere in
# `MemoryManager::__ct` or in `MemoryPool::__ct` (which accepts any size and
# just rounds it). Nothing will reject an oversized pool. The only real
# constraint is that pool[0] stays positive — hence the sanity cap below.
#
# RAISING RESOURCE SHRINKS MAIN BY THE SAME AMOUNT
# ---------------------------------------------------------------------------
# The arena is fixed, so every byte handed to RESOURCE is taken from MAIN.
# MAIN is roughly 24 MB, so the 0x700000 (7 MiB) preset costs MAIN about
# 0xC0000 (768 KiB) more than vanilla — near 3% of MAIN. That is the whole
# trade: more room for per-level resource data, slightly less general heap.
#
# THE ENCODING RULE — LOW 16 BITS MUST BE ZERO
# ---------------------------------------------------------------------------
# Only the `lui` HALF of each site is editable. pool[2]'s size is a lone `lui`,
# so any size with a non-zero low half-word simply cannot be encoded there. The
# reserve is `lui $v0,<hi> ; ori $v0,$v0,0x4fd0`, and we do NOT rewrite the
# `ori`, so the low half of `-(size + 0xCB000 + 0x30)` must come out as 0x4FD0.
# Deriving that: 0xCB030's low half is 0xB030, and (-(low(size) + 0xB030)) mod
# 0x10000 == 0x4FD0 exactly when low(size) == 0. So the practical rule is a
# single one — REFUSE any size whose low 16 bits are non-zero — and the code
# computes the low half rather than asserting the 0x4FD0 coincidence blindly.
#
# THE TARGET CASE
# ---------------------------------------------------------------------------
# size = 0x700000 (7,340,032) -> reserve 0x7CB030 -> `lui $a2,0x0070`
# (word 0x3C060070) and `lui $v0,0xff83` (word 0x3C02FF83).
#
#   Measured file offsets, REFERENCE / CROSS-CHECK ONLY — the patch locates both
#   sites by signature and NEVER hardcodes an offset (project convention):
#     SLUS_200.17  pool-2 0xAB5BC   reserve 0xAB598
#     SLPM_621.27  pool-2 0xAB8AC   reserve 0xAB888
#   Each masked signature below matches EXACTLY ONCE per retail build.
# ============================================================================

RESOURCE_POOL_VANILLA = 0x00640000      # the engine's own size — no patch applied
RESOURCE_POOL_MIN = RESOURCE_POOL_VANILLA   # lowering it is not the feature
# Hard ceiling, and NOT the encoding's limit (a lone `lui` could express up to
# 0xFFFF0000). 0x00A00000 (10 MiB) is chosen because MAIN is only ~24 MB at
# runtime and the arena is fixed: 10 MiB leaves MAIN around 14 MB, which is
# still comfortably more than vanilla MAIN minus its largest observed peak.
# Anything above this is REFUSED rather than silently clamped, because there is
# no engine-side check that would catch driving pool[0] to nothing.
#
# NOT THE BINDING LIMIT ANY MORE, and a reader should not think 0xA00000 is
# reachable: `RESOURCE_POOL_CRASHED_MIN` (0x880000) sits BELOW this and is the
# floor that actually binds, because it is MEASURED to hang on map load. This
# ceiling is the ENCODING/ARENA argument; the crash floor is the EVIDENCE, and
# evidence outranks the argument. Kept because it is the reason no size above
# 10 MiB will ever be entertained even if the hang is one day explained.
RESOURCE_POOL_MAX = 0x00A00000
# THE DEFAULT for every cross-world build, and the ONLY symbol that means "what
# a cross-world build patches when the caller chooses nothing". Cross-world
# porting grows a world's PRS/PRT with foreign enemy meshes, animations and
# textures, and those land in the RESOURCE pool, so a cross-world build wants
# MORE than vanilla -- but only as much as the console has actually tolerated
# REPEATEDLY.
#
# WHY 0x700000 AND NOT HIGHER. 0x800000 was briefly the default on the strength
# of ONE build that loaded and played. A LATER build at 0x800000 HUNG with
# INFINITE LOADING. Both results are real console results, so 0x800000 is
# UNRELIABLE: loaded once, hung once. 0x700000 is the only raised size that has
# loaded and played CONSISTENTLY, across many builds, which is why the default
# came back to it. See `RESOURCE_POOL_LOADED_MAX` for the full history.
#
# LOWERING the pool is always safe: it can only ship a SMALLER lineup and can
# never overflow.
RESOURCE_POOL_RAISED = 0x00700000

#: 0x800000 -- SELECTABLE, NOT A DEFAULT, and UNRELIABLE. The name is HISTORICAL:
#: this constant was once "the largest size observed to load" and that claim is
#: NO LONGER TRUE AS STATED, because the same size later hung.
#:
#: THE FULL RECORDED HISTORY, every console result kept, none deleted:
#:
#:   * 0x900000 (`high`)  HUNG -- infinite loading at map load;
#:   * 0x880000           HUNG -- infinite loading at map load;
#:   * 0x800000           LOADED and played on the FIRST build tested at this
#:                        size, and LATER HUNG with INFINITE LOADING on ANOTHER
#:                        build. Both observations stand. Neither report was
#:                        wrong;
#:   * 0x700000           LOADED and played CONSISTENTLY, across many builds --
#:                        which is why it is the default (`RESOURCE_POOL_RAISED`).
#:
#: WHAT THE CONTRADICTION MEANS, and it is itself the finding: THE HANG BOUNDARY
#: IS NOT A CLEAN STEP. A size that loads ONCE is NOT PROVEN -- whether a build
#: survives near the boundary depends on what else that particular build put in
#: the pool, so there is no single size above 0x700000 that can be called good.
#: That is why there is no "largest safe size" claimed anywhere any more, and why
#: the UNEXPLORED band 0x810000..0x870000 is not worth another console build: a
#: single loading result there would prove nothing either.
#:
#: Still SELECTABLE, because a user who wants to retry it must be able to, and
#: because it sits BELOW `RESOURCE_POOL_CRASHED_MIN` (0x880000) it is reachable
#: through the resolver. It is never chosen automatically.
#:
#: WHY THIS MATTERS AT ALL: pool exhaustion is SILENT. `MemoryPool::Allocate`
#: falls through and returns `sentinel_block + 16`, so the caller corrupts
#: allocator metadata and the game dies (or hangs) at load with no log line, no
#: build failure and no test failure. A green test run says nothing about pool
#: size, which is exactly why the number has to come from a console build.
RESOURCE_POOL_LOADED_MAX = 0x00800000

#: The SMALLEST pool size OBSERVED TO HANG, and a HARD REFUSAL, in the same
#: spirit as `roster_selection.CRASHED_MIN_PCT`. Builds at 0x880000 and at
#: 0x900000 both hung on map load with infinite loading, so a build is never
#: allowed to ask for 0x880000 or more.
#:
#: DELIBERATELY NOT MOVED DOWN to 0x800000 even though a 0x800000 build hung: the
#: same size also loaded and played, so it is UNRELIABLE rather than measured to
#: hang, and it must stay REACHABLE for a user who wants to retry it. This floor
#: records sizes that have NEVER loaded.
#:
#: REFUSED, NOT CLAMPED: there is no engine-side check that would catch an
#: oversized pool, and exhaustion is SILENT (see above), so a silent clamp would
#: hide the one decision the user needs to make and would ship an executable
#: whose pool disagrees with the size the draw budget was computed from. The
#: refusal floor can only ever move DOWN on new evidence, which makes this guard
#: STRICTER over time, never looser.
#:
#: This floor sits BELOW `RESOURCE_POOL_MAX` (0xA00000), so IT is what binds.
RESOURCE_POOL_CRASHED_MIN = 0x00880000

# 0x00900000 (9 MiB), +0x2C0000 over vanilla, under RESOURCE_POOL_MAX and
# encodable. NO LONGER SELECTABLE AT ALL: the `high` preset was REMOVED from
# `RESOURCE_POOL_PRESETS` and this size is above `RESOURCE_POOL_CRASHED_MIN`, so
# `resolve_resource_pool` refuses it by whatever spelling it arrives as. The
# CONSTANT is kept anyway, because the OBSERVATION is worth more than the preset
# was and the refusal message cites it by name.
#
# THE OBSERVED IN-GAME FAILURE, AS RECORDED (same stance as
# `catalog.HELD_BACK_ENEMIES_ON_OBSERVED_CRASH`: an observation the structural
# checks cannot see outranks a clean measurement). A build with this preset in
# effect — it was briefly the default when `--cross-world-structures` was on —
# HUNG ON MAP LOAD: INFINITE LOADING, no crash, no message. The immediately
# preceding build, identical but for the `raised` preset (0x700000), loaded and
# played fine. WHAT THAT MEANS, and what was ruled out: the ELF patch itself
# applies cleanly and atomically at both sites and the arithmetic verifies (see
# tests/test_resource_pool_high.py), so the patcher is not the fault. The
# credible cause is the trade this pool makes: the three pools tile a FIXED
# arena with no slack and there is NO bounds check anywhere in
# `MemoryManager::__ct` or `MemoryPool::__ct`, so taking 0x2C0000 off MAIN
# instead of `raised`'s 0xC0000 starves MAIN by an extra 0x200000 and a map's
# MAIN-pool allocation never completes. Nothing was fixed; the preset is kept
# because the OBSERVATION is worth more than the code.
#
# So: UNREACHABLE through the resolver, and never chosen automatically. Cross-world structures get
# their room from `structure_draw.STRUCTURE_RESERVE_BYTES` — a reserved slice of
# the SAME draw budget — not from this pool.
RESOURCE_POOL_HIGH = 0x00900000

# WHAT IS ACTUALLY KNOWN ABOUT POOL SIZE, AND WHAT IS NOT
# ---------------------------------------------------------------------------
# The band was walked on console, but UNLIKE the occupancy ceiling (93.0 LOADS /
# 93.5 CRASHES) it did NOT come out as a clean step:
#   0x700000 (`raised`)  LOADED and played, CONSISTENTLY, many builds  <== DEFAULT
#   0x800000             LOADED and played ONCE, then HUNG (infinite loading) on
#                        a LATER build -- UNRELIABLE, selectable, never default
#   0x880000             INFINITE LOADING at map load
#   0x900000 (`high`)    INFINITE LOADING at map load
# The two 0x800000 results CONTRADICT each other and both are real. That is the
# finding: THE HANG BOUNDARY IS NOT A CLEAN STEP, so one loading build does not
# prove a size. 0x810000..0x870000 stays UNEXPLORED and exploring it would prove
# nothing for the same reason. At or above 0x880000 is not unexplored -- it has
# NEVER loaded, so it is REFUSED (see RESOURCE_POOL_CRASHED_MIN), and the `high`
# preset was removed so a measured hang cannot be selected by name.
#
# SUSPECTED CAUSE of the 0x900000 hang, an INFERENCE and NOT a measurement: the
# three pools tile a fixed arena with no slack and there is no bounds check
# anywhere, so growing RESOURCE starves MAIN and a map's MAIN-pool allocation
# never completes. Nothing localised the hang to MAIN; this is reasoning from
# the layout, not evidence. Nothing here is CONFIRMED.
#
# THE PAYOFF, honestly: at the 93% occupancy ceiling each 64 KiB of pool buys
# about 59 KiB (0.93 * 65,536 = 60,948 B) of draw budget. Enemy routes cost
# 225 KB (Axe_Guard) to 841 KB (Plant_Monster), so ~+512 KB of pool — eight
# 64 KiB steps, ~476 KB of budget — is worth roughly ONE to TWO extra arrivals
# per world, depending on which routes the draw lands on. Smaller steps buy
# fractions of an arrival and may buy none at all.
RESOURCE_POOL_STEP = 0x10000            # the smallest encodable step, 64 KiB

# Named presets, resolved at the edges by resolve_resource_pool(). Insertion
# order is ascending on purpose: the CLI/GUI list them in this order.
#   vanilla  0x640000  the engine's own size — NO patch is applied at all
#   raised   0x700000  +0xC0000 for RESOURCE, the same amount off MAIN
#              (MAIN ~24 MB -> ~23 MB). THE DEFAULT for every cross-world build,
#              and the only raised size that has loaded and played CONSISTENTLY.
#   unreliable 0x800000  loaded and played ONCE, then HUNG (infinite loading) on
#              a later build. SELECTABLE for a retry, NEVER a default. The older
#              spelling `loaded-max` still resolves to the same size so existing
#              command lines keep working, but the name was a claim the evidence
#              no longer supports. See RESOURCE_POOL_LOADED_MAX.
# `high` (0x900000) WAS a preset and is GONE: it is measured to hang, so it must
# not be selectable by name. The constant survives as recorded evidence.
RESOURCE_POOL_PRESETS = {
    "vanilla": RESOURCE_POOL_VANILLA,
    "raised": RESOURCE_POOL_RAISED,
    "unreliable": RESOURCE_POOL_LOADED_MAX,
    "loaded-max": RESOURCE_POOL_LOADED_MAX,   # historical spelling, same size
}

# pool[2] construction. Word 3 is the TARGET `lui`; the surrounding words pin it
# to the pool[2] call (this+0x260) and to the pool[1] call that follows
# (this+0x140), so this cannot be confused with any other lui 0x0064.
RESOURCE_POOL_SIZE_PATTERN = (
    (0x8E050004, 0xFFFFFFFF),   # lw    $a1, 4($s0)
    (0x26040260, 0xFFFFFFFF),   # addiu $a0, $s0, 608     pool[2] @ this+0x260
    (0x0C000000, 0xFC000000),   # jal   MemoryPool::__ct  MASKED: build-varying
    (0x3C060064, 0xFFFFFFFF),   # lui   $a2, 0x0064       <== TARGET
    (0xAE020004, 0xFFFFFFFF),   # sw    $v0, 4($s0)
    (0x8E050004, 0xFFFFFFFF),   # lw    $a1, 4($s0)
    (0x3C02000C, 0xFFFFFFFF),   # lui   $v0, 0x000c       pool[1] size hi
    (0x26040140, 0xFFFFFFFF),   # addiu $a0, $s0, 320     pool[1] @ this+0x140
)
# Same words with the target immediate wildcarded, so an ALREADY-PATCHED
# executable is still located on a re-run (same idempotence stance as
# patch_widescreen / patch_object_budget).
RESOURCE_POOL_SIZE_PATCHED_PATTERN = (
    *RESOURCE_POOL_SIZE_PATTERN[:3],
    (0x3C060000, 0xFFFF0000),
    *RESOURCE_POOL_SIZE_PATTERN[4:],
)
RESOURCE_POOL_SIZE_SLOT_DELTA = 12
RESOURCE_POOL_SIZE_LUI_BASE = 0x3C060000    # lui $a2,<imm>
RESOURCE_POOL_SIZE_EXPECTED = 0x3C060064    # lui $a2,0x0064   (vanilla)

# pool[0] reserve. Word 0 is the TARGET `lui`; word 1 is the `ori` we never
# touch and which fixes the low half at 0x4FD0.
POOL_TOTAL_RESERVE_PATTERN = (
    (0x3C02FF8F, 0xFFFFFFFF),   # lui   $v0, 0xff8f       <== TARGET
    (0x34424FD0, 0xFFFFFFFF),   # ori   $v0, $v0, 0x4fd0  (unchanged)
    (0x02223021, 0xFFFFFFFF),   # addu  $a2, $s1, $v0
    (0x0C000000, 0xFC000000),   # jal   MemoryPool::__ct  MASKED
    (0x26040020, 0xFFFFFFFF),   # addiu $a0, $s0, 32      pool[0] @ this+0x020
)
POOL_TOTAL_RESERVE_PATCHED_PATTERN = (
    (0x3C020000, 0xFFFF0000),
    *POOL_TOTAL_RESERVE_PATTERN[1:],
)
POOL_TOTAL_RESERVE_SLOT_DELTA = 0
POOL_TOTAL_RESERVE_LUI_BASE = 0x3C020000    # lui $v0,<imm>
POOL_TOTAL_RESERVE_EXPECTED = 0x3C02FF8F    # lui $v0,0xff8f  (vanilla)
POOL_TOTAL_RESERVE_ORI_WORD = 0x34424FD0    # the guarded, never-written `ori`
POOL_TOTAL_RESERVE_ORI_LOW = 0x4FD0         # its immediate — the fixed low half

# pool[1]'s size and the three 16-byte rounding allowances: the constants that
# turn a RESOURCE size into pool[0]'s reserve.
RESOURCE_POOL_SIBLING_SIZE = 0x000CB000
RESOURCE_POOL_GUARD_SLACK = 0x30


def resource_pool_reserve(size: int) -> int:
    """RESOURCE pool size -> the pool[0] reserve that keeps the arena tiled.

    The single expression of the invariant
    `0x70B030 == 0x640000 + 0xCB000 + 0x30`, generalised.
    """
    return size + RESOURCE_POOL_SIBLING_SIZE + RESOURCE_POOL_GUARD_SLACK


class ResourcePoolRefused(ValueError):
    """A requested RESOURCE pool size that cannot be encoded or is out of range.

    Same shape and same stance as `roster_selection.OccupancyRefused`: REFUSED
    loudly rather than clamped or silently dropped, because there is no
    engine-side check that would catch a bad pool size.
    """


def _refuse_resource_pool(what: str) -> "ResourcePoolRefused":
    """The one refusal message, so every edge says the same thing."""
    return ResourcePoolRefused(
        f"{what}. The RESOURCE pool must be 0x{RESOURCE_POOL_MIN:X}.."
        f"0x{RESOURCE_POOL_MAX:X} and a multiple of 0x10000 (64 KiB): only the "
        f"`lui` HALVES of the two sites are rewritable (pool[2]'s size is a "
        f"lone `lui` and pool[0]'s reserve keeps its fixed "
        f"`ori ...,0x{POOL_TOTAL_RESERVE_ORI_LOW:X}` low half), so the low 16 "
        f"bits of the size must be 0 and 64 KiB is the smallest encodable step. "
        f"Every byte given to RESOURCE is taken from MAIN (~24 MB at runtime): "
        f"the three pools tile a FIXED arena with no slack and the engine has "
        f"NO bounds check anywhere in `MemoryManager::__ct` or "
        f"`MemoryPool::__ct`, so nothing in game will reject an oversized pool. "
        f"OBSERVED: 0x{RESOURCE_POOL_RAISED:X} loads and plays CONSISTENTLY and "
        f"is THE DEFAULT; 0x{RESOURCE_POOL_LOADED_MAX:X} LOADED ONCE and later "
        f"HUNG (infinite loading) on another build, so it is UNRELIABLE -- "
        f"selectable if you want to retry it, never a default; "
        f"0x{RESOURCE_POOL_CRASHED_MIN:X} and 0x{RESOURCE_POOL_HIGH:X} both HUNG "
        f"ON MAP LOAD (infinite loading) and have NEVER loaded, so 0x"
        f"{RESOURCE_POOL_CRASHED_MIN:X} and above is REFUSED rather than "
        f"clamped. 0x810000..0x870000 is still UNEXPLORED, and because the hang "
        f"boundary is NOT a clean step one loading build there would prove "
        f"nothing.")


def resolve_resource_pool(value) -> int | None:
    """Preset label, size, or None -> the integer patch_resource_pool wants.

    Single source of truth for every edge (both argparse layers and the GUI
    control) so neither the label mapping nor the validation is duplicated.

    Accepted:
      * None and blank      -> None (leave the executable's own pool alone)
      * "vanilla"           -> None (same: no patch is applied at all)
      * a preset label      -> its size, case-insensitive, surrounding space ok
      * an int              -> itself, after validation
      * "0x710000"/"0X..."  -> HEX, because the 0x prefix says so
      * "7405568"           -> DECIMAL. A bare digit string is NOT guessed as
                              hex: guessing would silently ship a pool the user
                              did not ask for. A bare decimal that is not 64 KiB
                              aligned or is out of range is REFUSED below, which
                              is the loud outcome we want.

    Raises `ResourcePoolRefused` for an unrecognised label, a size out of
    `RESOURCE_POOL_MIN`..`RESOURCE_POOL_MAX`, or a size whose low 16 bits are
    non-zero.

    An UNKNOWN LABEL RAISES. It used to fall out of
    `RESOURCE_POOL_PRESETS.get(label)` as None, and None means VANILLA — so
    `--resource-pool raized` silently shipped a vanilla pool while the draw
    budget may have been computed for something else. That was a real bug.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        # Not a size and not a label; a boolean cannot say WHICH pool.
        raise _refuse_resource_pool(f"{value!r} is not a RESOURCE pool size")
    if isinstance(value, int):
        return _validate_resource_pool(value)
    label = str(value).strip()
    if not label or label.lower() == "vanilla":
        return None
    if label.lower() in RESOURCE_POOL_PRESETS:
        return RESOURCE_POOL_PRESETS[label.lower()]
    lowered = label.lower()
    try:
        if lowered.startswith("0x") or lowered.startswith("-0x"):
            parsed = int(lowered, 16)
        else:
            parsed = int(lowered, 10)
    except ValueError:
        raise _refuse_resource_pool(
            f"{value!r} is not one of {'/'.join(RESOURCE_POOL_PRESETS)} and is "
            f"not a size (prefix a hex size with 0x; a bare number is read as "
            f"DECIMAL)")
    return _validate_resource_pool(parsed)


def _validate_resource_pool(size: int) -> int:
    """Range and encodability, shared by every accepted spelling."""
    if not (RESOURCE_POOL_MIN <= size <= RESOURCE_POOL_MAX):
        raise _refuse_resource_pool(
            f"RESOURCE pool size 0x{size:X} ({size:,}) is out of range")
    # The MEASURED crash floor, checked BEFORE encodability and sitting BELOW
    # RESOURCE_POOL_MAX, so it is the limit that actually binds.
    if size >= RESOURCE_POOL_CRASHED_MIN:
        raise _refuse_resource_pool(
            f"RESOURCE pool size 0x{size:X} ({size:,}) is REFUSED: "
            f"0x{RESOURCE_POOL_CRASHED_MIN:X} is the MEASURED HANG FLOOR -- a "
            f"0x{RESOURCE_POOL_CRASHED_MIN:X} build and a "
            f"0x{RESOURCE_POOL_HIGH:X} build (the removed `high` preset) BOTH "
            f"HUNG ON MAP LOAD with infinite loading and have NEVER loaded. "
            f"0x{RESOURCE_POOL_LOADED_MAX:X} stays BELOW this floor and stays "
            f"SELECTABLE, but it is UNRELIABLE: it LOADED and played once and "
            f"later HUNG on another build. THE DEFAULT is "
            f"0x{RESOURCE_POOL_RAISED:X}, the only raised size that has loaded "
            f"and played consistently; 0x810000..0x870000 is still UNEXPLORED "
            f"and, since the hang boundary is not a clean step, a single loading "
            f"build there would prove nothing")
    if size & 0xFFFF:
        raise _refuse_resource_pool(
            f"RESOURCE pool size 0x{size:X} is not encodable")
    return size


def patch_resource_pool(data: bytearray, size: int | None) -> dict:
    """Raise the engine's RESOURCE memory pool (pool[2]).

    size: RESOURCE_POOL_MIN..RESOURCE_POOL_MAX, low 16 bits zero. None or
    RESOURCE_POOL_VANILLA (0x640000) = no change, and in that case the
    executable is left byte-identical by this feature. Mutates `data` in place
    and returns a stats dict.

    Writes TWO words, and both or neither:
      SITE A  the lone `lui $a2,<hi>` that IS pool[2]'s size, and
      SITE B  the `lui $v0,<hi>` half of pool[0]'s negative reserve, raised by
              the same amount so the three pools still tile the arena exactly.
    Site B is mandatory, not optional: without it pool[1] is constructed past
    the end of the malloc'd arena and its free-list header is written out of
    bounds — silent heap corruption, since the engine checks nothing.

    Raising RESOURCE SHRINKS MAIN by the same number of bytes (see the section
    comment). Sizes whose low 16 bits are non-zero cannot be encoded (only the
    `lui` halves are editable) and are REFUSED, as is anything above
    RESOURCE_POOL_MAX.

    Re-runs are supported: a slot already holding one of our own writes counts
    as located, and both words are recomputed from the REQUESTED size.
    `already_patched` says which happened.
    """
    # 1) Argument validation first, so an invalid size never searches the buffer.
    none_res = {"applied": False, "size": None, "sites": 0, "slots": []}
    if size is None:
        return dict(none_res)
    if not isinstance(size, int) or isinstance(size, bool):
        return {**none_res, "size": size,
                "reason": f"size must be an integer 0x{RESOURCE_POOL_MIN:X}.."
                          f"0x{RESOURCE_POOL_MAX:X}"}
    if size == RESOURCE_POOL_VANILLA:
        return {**none_res, "size": size,
                "reason": f"0x{RESOURCE_POOL_VANILLA:X} is vanilla"}
    if size < RESOURCE_POOL_MIN:
        return {**none_res, "size": size,
                "reason": f"size must be 0x{RESOURCE_POOL_MIN:X}.."
                          f"0x{RESOURCE_POOL_MAX:X}: this feature RAISES the "
                          f"engine's own RESOURCE pool, it does not lower it"}
    if size > RESOURCE_POOL_MAX:
        return {**none_res, "size": size,
                "reason": f"size must be 0x{RESOURCE_POOL_MIN:X}.."
                          f"0x{RESOURCE_POOL_MAX:X}: every byte given to "
                          f"RESOURCE is taken from MAIN (~24 MB at runtime) and "
                          f"the engine has NO bounds check that would catch "
                          f"pool[0] being driven to nothing"}
    if size & 0xFFFF:
        return {**none_res, "size": size,
                "reason": f"size 0x{size:X} is not encodable: pool[2]'s size is "
                          f"a lone `lui` and the reserve's low half is a fixed "
                          f"`ori ...,0x{POOL_TOTAL_RESERVE_ORI_LOW:X}` that this "
                          f"patch does not rewrite, so only the `lui` halves are "
                          f"editable and the low 16 bits of the size must be 0"}
    reserve = resource_pool_reserve(size)
    neg = (-reserve) & 0xFFFFFFFF
    # Derived, not assumed: the untouched `ori` fixes the low half, so the low
    # half of -reserve has to come out as exactly that immediate.
    if neg & 0xFFFF != POOL_TOTAL_RESERVE_ORI_LOW:
        return {**none_res, "size": size,
                "reason": f"size 0x{size:X} gives reserve 0x{reserve:X}, whose "
                          f"negation ends in 0x{neg & 0xFFFF:04X} rather than "
                          f"the fixed `ori` immediate "
                          f"0x{POOL_TOTAL_RESERVE_ORI_LOW:04X}; only the `lui` "
                          f"half is editable"}
    if data[:4] != b"\x7fELF":
        return {**none_res, "found": False, "size": size, "reason": "not ELF"}

    buf = bytes(data)

    # 2) PHASE ONE — locate and verify BOTH sites. Nothing is written here.
    # SITE A: pool[2]'s size `lui`.
    matches = _find_all_masked(buf, RESOURCE_POOL_SIZE_PATTERN) \
        or _find_all_masked(buf, RESOURCE_POOL_SIZE_PATCHED_PATTERN)
    if not matches:
        return {**none_res, "found": False, "size": size,
                "reason": "RESOURCE pool size signature "
                          "(MemoryManager::__ct pool[2]) not found"}
    if len(matches) != 1:
        return {**none_res, "found": True, "size": size,
                "reason": f"RESOURCE pool size signature: expected 1 site, "
                          f"found {len(matches)}"}
    size_slot = matches[0] + RESOURCE_POOL_SIZE_SLOT_DELTA
    if size_slot + 4 > len(data):
        return {**none_res, "found": True, "size": size,
                "reason": "RESOURCE pool size slot past end of buffer"}
    size_word_now = struct.unpack_from("<I", buf, size_slot)[0]
    if size_word_now != RESOURCE_POOL_SIZE_EXPECTED \
            and (size_word_now & 0xFFFF0000) != RESOURCE_POOL_SIZE_LUI_BASE:
        return {**none_res, "found": True, "size": size,
                "reason": f"expected RESOURCE pool size word "
                          f"0x{RESOURCE_POOL_SIZE_EXPECTED:08X} at "
                          f"0x{size_slot:X}, found 0x{size_word_now:08X}"}
    prev_size = (size_word_now & 0xFFFF) << 16

    # SITE B: pool[0]'s negative reserve `lui`.
    res_matches = _find_all_masked(buf, POOL_TOTAL_RESERVE_PATTERN) \
        or _find_all_masked(buf, POOL_TOTAL_RESERVE_PATCHED_PATTERN)
    if not res_matches:
        return {**none_res, "found": False, "size": size,
                "reason": "pool[0] reserve signature "
                          "(MemoryManager::__ct pool[0]) not found, so the "
                          "arena reserve cannot be raised; refusing to raise "
                          "RESOURCE alone because pool[1] would then be built "
                          "past the end of the allocation"}
    if len(res_matches) != 1:
        return {**none_res, "found": True, "size": size,
                "reason": f"pool[0] reserve signature: expected 1 site, found "
                          f"{len(res_matches)}"}
    reserve_slot = res_matches[0] + POOL_TOTAL_RESERVE_SLOT_DELTA
    if reserve_slot + 8 > len(data):
        return {**none_res, "found": True, "size": size,
                "reason": "pool[0] reserve slot past end of buffer"}
    reserve_word_now = struct.unpack_from("<I", buf, reserve_slot)[0]
    if reserve_word_now != POOL_TOTAL_RESERVE_EXPECTED \
            and (reserve_word_now & 0xFFFF0000) != POOL_TOTAL_RESERVE_LUI_BASE:
        return {**none_res, "found": True, "size": size,
                "reason": f"expected pool[0] reserve word "
                          f"0x{POOL_TOTAL_RESERVE_EXPECTED:08X} at "
                          f"0x{reserve_slot:X}, found 0x{reserve_word_now:08X}"}
    # GUARD — the `ori` that supplies the reserve's low half must be intact. It
    # is what makes the lui-only edit valid, and we never write it.
    ori_word = struct.unpack_from("<I", buf, reserve_slot + 4)[0]
    if ori_word != POOL_TOTAL_RESERVE_ORI_WORD:
        return {**none_res, "found": True, "size": size,
                "reason": f"reserve `ori` guard failed at 0x{reserve_slot + 4:X}"
                          f": expected 0x{POOL_TOTAL_RESERVE_ORI_WORD:08X}, "
                          f"found 0x{ori_word:08X}"}

    size_word = RESOURCE_POOL_SIZE_LUI_BASE | (size >> 16)
    reserve_word = POOL_TOTAL_RESERVE_LUI_BASE | (neg >> 16)

    # 3) PHASE TWO — both sites verified, so write. Two bytes each (the
    # immediate half of each word), but packed as whole words for clarity.
    struct.pack_into("<I", data, size_slot, size_word)
    struct.pack_into("<I", data, reserve_slot, reserve_word)

    return {"found": True, "applied": True, "size": size,
            "sites": 2, "slots": [size_slot, reserve_slot],
            "reserve": reserve,
            "size_slot": size_slot, "reserve_slot": reserve_slot,
            "size_word": size_word, "reserve_word": reserve_word,
            "already_patched": prev_size != RESOURCE_POOL_VANILLA,
            "previous_size": prev_size}


# Slots that the randomizer overwrites: only the columns the player can pick.
# We do NOT overwrite world names or boss titles by default — those are
# expected to remain readable.
#
# Each tuple: (offset, slot_size, original_text, world)
LEVEL_TITLE_SLOTS = [
    # GRAVE — 3 unlocked columns
    (0x000CD1E8, 16, b"Dead Heat",         "grave"),
    (0x000CD1F8, 24, b"Coffin Canyon",     "grave"),
    (0x000CD210, 16, b"Bad to the Bone",   "grave"),
    # SWAMP — 3 unlocked columns
    (0x000CD250, 16, b"Voodoo Village",    "swamp"),
    (0x000CD260, 16, b"'Dem Bones",        "swamp"),
    (0x000CD270, 24, b"Quick and the Dead","swamp"),
    # ICE — 3 unlocked columns
    (0x000CD2D0, 32, b"Go With The Floe",  "ice"),
    (0x000CD2F0, 24, b"Dead in the Water", "ice"),
    (0x000CD308, 24, b"Cannonball Run",    "ice"),
    # UNDER — 3 unlocked columns
    (0x000CD360, 16, b"Crushed Spirits",   "under"),
    (0x000CD370, 32, b"The Unkindest Cut", "under"),
    (0x000CD390, 16, b"Down The Gullet",   "under"),
    # CASTLE — 5 unlocked columns + boss titles
    (0x000CD3C8, 24, b"The Keep",          "castle"),  # level
    (0x000CD3E0, 32, b"The Great Escape",  "castle"),  # level
    (0x000CD400, 32, b"Dungeon of Despair","castle"),  # level
    (0x000CD420, 24, b"Atop the Great Drill","castle"),  # level
    (0x000CD438, 24, b"Demon Queen",       "castle"),  # boss
]


# ============================================================================
# BEF VALIDATION BYPASS
# ============================================================================
# The engine's BEF loader at vaddr 0x00129390 has a validation check that
# compares the loaded file against an expected state (likely a size check
# returned by a vtable call). If the comparison fails, the loader aborts with
# "***BEF file load failed!! ***".
#
# To allow modified BEF files (with injected enemy type blobs), we patch the
# conditional branch at vaddr 0x001293F4 to always succeed.
#
# Original: beq $s0, $v0, +6  (0x12020006) — branches to success only if equal
# Patched:  beq $zero, $zero, +6  (0x10000006) — always branches to success
#
# ELF layout: base vaddr 0x100000, file offset 0x80 for first LOAD segment.
# vaddr 0x001293F4 => file offset = 0x001293F4 - 0x100000 + 0x80 = 0x029474

BEF_VALIDATION_BYPASS_OFFSET = 0x029474
BEF_VALIDATION_ORIGINAL = b'\x06\x00\x02\x12'  # beq $s0, $v0, +6
BEF_VALIDATION_PATCHED = b'\x06\x00\x00\x10'   # beq $zero, $zero, +6


def patch_bef_validation_bypass(data: bytearray) -> bool:
    """Patch the BEF loader's validation check to always succeed.

    This allows the engine to load modified BEF files (with injected enemy
    type blobs from other worlds) without crashing on map load.

    Args:
        data: Mutable ELF bytes (modified in-place).

    Returns:
        True if patch was applied, False if the expected bytes weren't found.
    """
    off = BEF_VALIDATION_BYPASS_OFFSET
    if off + 4 > len(data):
        return False
    current = bytes(data[off:off + 4])
    if current == BEF_VALIDATION_PATCHED:
        return True  # already patched
    if current != BEF_VALIDATION_ORIGINAL:
        return False  # unexpected bytes, don't patch
    data[off:off + 4] = BEF_VALIDATION_PATCHED
    return True


def apply_level_titles(
    data: bytearray,
    new_title: bytes = b"???",
    worlds: set[str] | None = None,
) -> dict:
    """Core level-title patch operating on a mutable SLUS bytearray (so it can
    be combined with other SLUS patches before writing once). See
    patch_level_titles for the file-based wrapper.

    Version-independent: the English level-title strings are identical across
    regions (US SLUS / JP SLPM) but sit at different file offsets — JP is
    shifted by a constant. We anchor on the first world-name string and apply
    the resulting delta to every slot, so the same LEVEL_TITLE_SLOTS table
    works for any region that shares the English title block."""
    patched = []
    skipped = []
    # Locate the title table by anchor so US and JP (and other English-title
    # regions) both work. "Grave Danger" sits at the very start of the table
    # at US offset 0xCD1C8; the delta is applied to every slot offset below.
    delta = 0
    anchor_at = bytes(data).find(TITLE_TABLE_ANCHOR)
    if anchor_at >= 0:
        delta = anchor_at - TITLE_TABLE_ANCHOR_US_OFFSET
    for off, slot_size, expected, world in LEVEL_TITLE_SLOTS:
        off += delta
        if worlds is not None and world not in worlds:
            skipped.append({"offset": off, "world": world,
                            "expected": expected.decode("ascii", errors="replace"),
                            "reason": f"world '{world}' not in selected set"})
            continue
        if len(new_title) >= slot_size:
            skipped.append({"offset": off, "world": world,
                            "expected": expected.decode("ascii", errors="replace"),
                            "reason": "new_title too long for slot"})
            continue
        actual = bytes(data[off:off + len(expected)])
        if actual != expected:
            skipped.append({"offset": off, "world": world,
                            "expected": expected.decode("ascii", errors="replace"),
                            "actual": actual.decode("ascii", errors="replace")})
            continue
        data[off:off + slot_size] = new_title + b"\x00" * (slot_size - len(new_title))
        patched.append({"offset": off, "world": world,
                        "old": expected.decode("ascii", errors="replace"),
                        "new": new_title.decode("ascii", errors="replace")})
    return {"patched": patched, "skipped": skipped, "delta": delta}


def patch_level_titles(
    elf_in: Path | str,
    elf_out: Path | str,
    new_title: bytes = b"???",
    worlds: set[str] | None = None,
) -> dict:
    """Overwrite per-world level titles in SLUS_200.17 with `new_title`.

    `worlds`: optional set of world names to limit which slots get patched.
              None (default) means ALL worlds.

    Returns a stats dict with patched and skipped slots.
    """
    elf_in = Path(elf_in)
    elf_out = Path(elf_out)
    data = bytearray(elf_in.read_bytes())
    res = apply_level_titles(data, new_title=new_title, worlds=worlds)
    elf_out.parent.mkdir(parents=True, exist_ok=True)
    elf_out.write_bytes(bytes(data))
    res["out_path"] = str(elf_out)
    res["size"] = len(data)
    return res


if __name__ == "__main__":
    src = Path("game_files") / "SLUS_200.17"
    out = Path("output") / "SLUS_200.17"
    res = patch_level_titles(src, out)
    print(f"Wrote: {res['out_path']} ({res['size']:,} bytes)")
    print(f"Patched {len(res['patched'])} slots:")
    for p in res["patched"]:
        print(f"  [{p['world'].upper():<6}] @0x{p['offset']:08X}: '{p['old']}' -> '{p['new']}'")
    if res["skipped"]:
        print(f"Skipped {len(res['skipped'])} slots:")
        for s in res["skipped"]:
            print(f"  [{s['world'].upper():<6}] @0x{s['offset']:08X}: expected '{s['expected']}'"
                  f"{', got ' + repr(s['actual']) if 'actual' in s else ''}"
                  f"{'  (' + s['reason'] + ')' if 'reason' in s else ''}")
