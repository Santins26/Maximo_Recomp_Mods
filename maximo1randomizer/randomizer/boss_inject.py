"""Boss injection into ordinary levels (GraveDigger `0x29` into grave).

PORTS HIS ASSETS. The old premise -- "every asset the boss references is a
Resident_Asset of `GRAVE.PRS`" -- is REFUTED BY MEASUREMENT: `GRAVE_B.PRS`
(2,782,263 B, 24 meshes, 49 anims) and `GRAVE_B.PRT` (550,512 B, 37 entries, 34
CLUTs) DO exist and ARE the boss's source archive. Blob `0x29`'s single LOCAL
category-1 reference is mesh 0, and in `GRAVE.PRS` index 0 resolves to a 0-bone
`tombdown` prop -- which is exactly why the injected boss was invisible and
froze the level. `prove_residency` is therefore GONE, replaced by a real port:

  * `GRAVE_B.PRS` mesh[0] (306,920 B, tag 6, 57 bones, 50 render passes),
  * `GRAVE_B.PRS` anim records 0..21 (569,861 B, every one tag 0x0C SKA),
  * `GRAVE_B.PRT` textures 0, 1, 2 (142,384 B), the only ones mesh[0]'s render
    passes reach,

= 1,019,165 B into `GRAVE.PRS`/`GRAVE.PRT`, with blob `0x29`'s references
rewritten to the new indices. The rest of the work is unchanged: derive and
merge a set of BEF types, place one `.PSX` record in a recycled slot, and merge
13 sound ids.

SECOND MEASURED BUG, NOW FIXED -- THE CLOSURE WAS PORTED FOR NOBODY BUT THE
BOSS. Porting only `0x29`'s assets and rewriting only `0x29`'s references left
the other TEN Merge_Set members injected VERBATIM with their `GRAVE_B`-LOCAL
references intact: 21 unrewritten LOCAL slots (12 category-1, 9 category-2)
naming 11 distinct source meshes and 10 distinct source anims. Every one of them
is IN RANGE of the live counts, so the pre-existing bounds check was blind to
it, and resolved against `GRAVE.PRS` they land on structurally incompatible
assets (`guswall`, 2,060 B / 0 bones, resolving onto a 61,160 B 31-bone
skeleton; `slameff` 5,256 B onto 142,532 B / 39 bones; `gus_soul_id10` 644 B onto
131,484 B / 36 bones; 7 of the 10 anim references crossing record class, source
MEA `0x0A` against target SKA `0x0C`). The companions are Spawn-on-activation
impact effects, which is why the symptom was a crash after walking the map.

The port is therefore DERIVED, not constant-driven, and covers EVERY injected
member: `derive_merge_set` runs FIRST, the union of every injected member's LOCAL
category-1/category-2 references is deduped and ported ONCE per distinct asset,
every ported mesh's render passes are remapped, and EVERY injected member's blob
is rewritten -- not just `0x29`'s. A new IDENTITY check (`Identity_Check`, not a
bounds check) requires every LOCAL reference of every injected member to read a
map VALUE, so a source index that merely happens to be in range now FAILS.

Project convention, deliberate and load-bearing here: NO public function in this
module raises on malformed data. A failure is a VERDICT carrying `ok`, `reasons`
and `details`, in the shape `cross_world.WorldPortSession.refuse(plan, reason,
details)` already consumes.

The refusal model is absolute rather than cautious because
`CVmEntityManager::LoadBoundEntityFile` ABORTS THE LOAD OF THE WHOLE `.BEF` if
any single entry fails to parse: a partially valid merge does not give a
slightly-wrong boss, it gives a level that will not load.

THE SCRIPT EDITS THIS MODULE MAKES, AND NOTHING ELSE. Injection works in game,
and killing the GraveDigger teleported the player into the GraveDigger boss
fight -- the `Early_Exit_Risk` firing. The user has REVERSED the earlier
"gotonextlevel doesnt need to be implemented" scoping and asked for the call to
go. That fill WORKED: no more teleport. It also let state 20 run to completion
for the first time, and the NEW in-game symptom is that Maximo is frozen in
place after the boss dies. `patch_boss_script` therefore applies up to TWO
measured, length-preserving, in-place 5-byte spans in the boss Script_Section:

  * span A, `0x13CA` -- the `GotoNextLevel` call site, replaced by
    `CutScene_Stop()` (api 185, arity 0) plus three nops, so the exit is still
    gone AND any running cutscene is stopped;
  * span B, `0x1125` -- the ONLY `CameraSetBossFlag(32)` call site, replaced by
    five nops, so the never-cleared boss camera flag is never set.

Both spans are the same three-instruction push/popapiparam/callapi idiom, both
are verified byte-exact before being rewritten and REFUSED on any mismatch, and
no instruction offset anywhere moves. Nothing else in any Script_Section is ever
touched. See the BOSS SCRIPT SPANS comment block below for the measurement. The
level-exit removal is confirmed in game; the other two edits are SUSPICION-
driven and nobody has watched them unfreeze anything.

Nothing in this module claims the boss renders, animates, moves or makes a
sound. Nobody has watched it. `CANDIDATE` is the status ceiling.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

from . import anim_port as _anim_port
from . import bef as _bef
from . import befscript as _befscript
from . import catalog as _catalog
from . import companion_port as _companion_port
from . import items as _items
from . import mesh_port as _mesh_port
from . import prs_mesh_parser as _pmp
from . import psx as _psx
from . import roster_selection as _roster_selection
from . import sound_bank as _sound_bank
from . import texture_port as _texture_port

# --- KILL SWITCH -----------------------------------------------------------
# Boss injection is EXPERIMENTAL and OFF by default. While this is False the
# pipeline receives nothing from this module and the output is byte-identical to
# a build without the feature, for a fixed seed. It is a KILL SWITCH, not a
# tuning knob: there is no partial mode. Same discipline as
# `cross_world.COMPANION_PORT_ENABLED`.
BOSS_INJECT_ENABLED: bool = False

#: Asset messages carry this prefix. `ELF patch:` is reserved for executable
#: patches and is NEVER used from this module.
LOG_PREFIX = "[cross-world]"

BOSS_TYPE = 0x29                     # Boss_Entity, GraveDigger
BOSS_RECORD_SIZE = 0x248             # measured in G_BOSS.PSX
CLOSURE_MAX_MEMBERS = 24             # Refusal 6 bound

#: Offset from the level's player-spawn point, in world units, where an
#: injected boss is placed. Tunable: if the boss lands inside geometry, change
#: this. Geometry fit is STATICALLY UNVERIFIABLE, so this is a heuristic
#: anchored on the one point in the level the player provably occupies.
BOSS_SPAWN_OFFSET: tuple = (0.0, 0.0, 12.0)

#: SANITY ONLY -- NOT THE PORTING CONTRACT. The Boss_Entity's own rig in
#: `GRAVE_B.PRS`: the ONE index its single LOCAL category-1 reference (blob
#: offset 0xD9) names. 306,920 B, tag 6, 57 bones, 50 render passes, 0 mesh-node
#: names. The port DERIVES its mesh set from the blobs of every INJECTED member
#: (see `collect_member_refs`); this constant only proves the derivation still
#: sees the boss where it was measured, and the port REFUSES if it does not.
BOSS_SOURCE_MESH_INDEX = 0

#: SANITY ONLY -- NOT THE PORTING CONTRACT, same as above. Blob `0x29` carries
#: 23 LOCAL category-2 references covering anim indices 0..21 (index 2 appears
#: twice). 569,861 B, all tag 0x0C SKA, all BY_NAME, none needing a Bone_Remap.
BOSS_SOURCE_ANIM_INDICES = tuple(range(22))

#: `.PRS` mesh count u32, and `.PRT` entry count (`count_b`) u32. Same offsets
#: `cross_world` and `texture_port` use; named here so the refusals can report
#: where a figure came from.
MESH_COUNT_OFFSET = 0x20
PRT_CLUT_COUNT_OFFSET = 0x08
PRT_ENTRY_COUNT_OFFSET = 0x0C

#: The RESOURCE pool budget this feature is measured against. `raised=True`
#: matches the build that applies `elf_patch.patch_resource_pool`; the check is
#: enforced against the LIVE emitted lengths, never a predicted sum.
POOL_BUDGET_RAISED = True

#: Entity classes treated as decorative and therefore recyclable as a
#: Host_Slot. A class enters this set ONLY with recorded evidence naming what
#: was inspected and where. An unrecognised class is NEVER a candidate --
#: a blocklist would admit every class nobody thought about.
#:
#: EVIDENCE for `Entity_Light` (0x6D): `items.BOSS_DUPLICATION["G_BOSS.PSX"]
#: ["victim_names"]` recycles four side lights (`r_moonlight`, `l_bottom`,
#: `r_mid`, `l_mid`) precisely because removing one only dims the arena
#: slightly. It is the one class this project already treats as recyclable.
#: EVIDENCE for `Smoke` (0x3B, record size 936 B >= 0x248): measured by
#: parsing each grave `.PSX` in `game_files/` one file per process with
#: `psx.PsxFile.parse`. Grave levels contain ZERO `Entity_Light` records, which
#: is why every one of them refused R9; `Smoke` is the highest-count class in
#: those files that clears every admission criterion.
#:   G_HUB.PSX     x9   names Smoke_8662, Smoke_8681, Smoke_8684, Smoke_8685,
#:                      Smoke_8693 (+4)
#:   G_SUB1.PSX    x5   names pitE_1, pitE_2, pitC_1, pitA_1, pitB_1
#:   G_SUB2.PSX    x3   names pit_haze1, pit_haze2, pit_haze3
#:   G_INTROB.PSX  x2   names Smoke_8682, Smoke_8683
#: Every instance is >= 0x248 (all are 936 B). Across all four files the
#: measured count caught by `items.get_progression_protected_names`,
#: `items.PROTECTED_INSTANCE_NAMES`, instance-name sharing with
#: `items.PROGRESSION_ANCHOR_TYPES`, `catalog.is_event_tied_enemy` or
#: `items.GOLD_KEY_TYPE` is ZERO, and no instance name matches a scripted-name
#: token (`end`, `gate`, `key`, `p1_`..`p4_`, `fp1_`..`fp4_`, `caveexit`,
#: `exit`, `start`, `trigger`, `spawn`, bare numerals). Judged decorative: a
#: particle haze puff sited in pits and over water, named per-pit or by bare
#: autogenerated id, carrying no gameplay role -- recycling one of several in
#: the same pit removes some haze.
#: `Fog_Volume` (x64/x36/x31/x29, 680 B) and `Fire` (x33/x28/x22/x19, 744 B) are
#: larger populations but were REJECTED: measured `gatefog*`, `gate1_torch`,
#: `gatetorch*` and `endcave_torch*` instances carry scripted-looking names.
HOST_SLOT_ALLOWED_CLASSES: frozenset = frozenset({"Entity_Light", "Smoke"})

#: The 13 literal sound ids the Boss_Entity's Script_Section plays. All present
#: in `BS_GRAVE.BD`, all absent from vanilla `GRAVE.BD`.
BOSS_SOUND_IDS: tuple = (1053, 1055, 1057, 1059, 1060, 1061, 1062,
                         1063, 1064, 1065, 1274, 1285, 1537)

#: The three types `GRAVE.BEF` and `GRAVE_B.BEF` share. Append-only keeps the
#: HOST copy of each.
SHARED_TYPES: tuple = (0x1C, 0x23, 0x2A)

# --- THE BOSS SCRIPT SPANS -------------------------------------------------
# Up to TWO measured, length-preserving, in-place 5-byte edits to the boss
# Script_Section, and nothing else in the section is ever touched. Both spans
# are the SAME three-instruction idiom -- `by_c_push` (0x04, len 2),
# `popapiparam` (0x15, len 1), `by_callapi` (0x8C, len 2) -- so both are exactly
# 5 bytes wide and both can be re-tiled by length-1 instructions without moving
# a single offset.
#
# WHY IT EXISTS. Boss injection works in game and killing the GraveDigger
# teleported the player into the GraveDigger boss fight -- the Early_Exit_Risk
# firing. The user, having earlier scoped `GotoNextLevel` OUT ("gotonextlevel
# doesnt need to be implemented"), has REVERSED that call and asked for the
# call to be removed. Both the earlier scoping and this reversal are the user's,
# recorded so the decision stays attributable.
#
# WHAT IS PATCHED. Blob `0x29` of `GRAVE_B.BEF`: 8,688 B, material section
# [0xD0,0x43A), Script_Section [0x536,0x21CF) of length 0x1C99 = 7,321 B,
# 2,996 instructions, clean decode, coverage 1.0, byte-exact `encode()` round
# trip. The call site is THREE CONTIGUOUS instructions, 5 bytes, at
# Script_Section offset 0x13CA (blob-absolute 0x1900 = 0x536 + 0x13CA), raw
# `04 00 15 8C 71`:
#
#     0x13CA  04 00   by_c_push  value=0        the literal argument
#     0x13CC  15      popapiparam               moves it into the API arg block
#     0x13CD  8C 71   by_callapi api_index=0x71 = 113 GotoNextLevel
#
# The call is the NARROW form `0x8C` (one-byte api index), not the wide `0x8B`.
#
# WHY FIVE BYTES, EXACTLY. Five is the smallest push/consume-BALANCED unit.
# Nopping only the 2-byte call leaves a stray 0 sitting in the API argument
# block; nopping 3 bytes (push + popapiparam) strands the pushed value on the
# VM stack. Taking all three instructions leaves both the stack and the arg
# block exactly as the surrounding code expects.
#
# WHY 0xFE. Opcode 0xFE is mnemonic `nop`, length 1, ZERO operands, handler
# 0x0010B230, confidence `verified`, present in BOTH the retail and prototype
# opcode maps and NOT in `befscript.INVALID_OPCODES` (0xE8..0xFD). Five
# length-1 arity-0 instructions tile the 5-byte span exactly, so the decode
# stays in phase and the section length is unchanged.
#
# WHY IT IS SAFE, all measured on the real bytes:
#   * exactly ONE branch targets the span -- `0x13B1 by_r_ifeqz` -> `0x13CA`,
#     i.e. the FIRST byte of the fill, so it lands on a nop and falls through;
#     nothing at all targets 0x13CB, 0x13CC, 0x13CD or 0x13CE;
#   * the four offset-directory tables start at 0x442 (0x43A holds the section
#     length 0x1C99, then a zero u32) with counts 22/22/8/4, and NO entry lies
#     inside or at either edge of 0x13CA..0x13CE -- the bracketing entries are
#     0x139D below and 0x13D6 above;
#   * `GotoNextLevel` is called EXACTLY ONCE out of 337 `callapi` sites, and
#     this blob has ZERO sites for every other transition API: EnterSubLevel
#     (94), TransportHubLevel (74), GetNextHubCount (73),
#     CheckSubLevelCompleted (66), PlayerSetStartPosition (62), ChangeMaximo
#     (61);
#   * after the fill control reaches `0x13CF by_c_push 0x14` -> `popapiparam`
#     -> `by_callapi SetState(20)` -> `exit`, so the death sequence still
#     completes.
#
# THE LEVEL-EXIT FILL IS CONFIRMED IN GAME: killing the injected GraveDigger no
# longer teleports the player.
#
# THE NEW SYMPTOM, AND WHY THESE TWO FURTHER EDITS EXIST. Maximo is now FROZEN
# IN PLACE after the boss dies and cannot move. The nop fill did NOT remove a
# teardown: it let state 20 run to COMPLETION for the first time, because
# `GotoNextLevel` previously fired two instructions before `SetState(20)`. A
# set/clear audit of ALL 337 callapi sites in this blob found exactly two
# SET-WITHOUT-CLEAR calls, and they are the suspects:
#
#   * `CameraSetBossFlag(32)` -- api 232 = 0xE8 -- at Script_Section 0x1128,
#     the ONLY site. Its inverse `CameraClearBossFlag` (233 = 0xE9) exists in
#     the API table and is called NOWHERE in the blob.
#   * `CutScene_Start(1000)` -- api 184 = 0xB8 -- at 0x0131. Its inverse
#     `CutScene_Stop` (185 = 0xB9, ARITY 0) exists and is called NOWHERE.
#
# WHY THE `CameraClearBossFlag(32)` SUBSTITUTION AT 0x13CA WAS REJECTED. It is
# provably ineffective, not merely unproven: state 20's ENTER handler at 0x1105
# CONTAINS the 0x1128 set, and `SetState(20)` is executed at 0x13D2 -- two
# instructions AFTER 0x13CA. Clearing the flag and then entering the state that
# sets it leaves the flag set. It is not implemented and must not be.
#
# SPAN B -- 0x1125, 5 bytes, `04 20 15 8C E8`:
#
#     0x1125  04 20   by_c_push  value=0x20 = 32   the flag mask
#     0x1127  15      popapiparam
#     0x1128  8C E8   by_callapi api_index=0xE8 = 232 CameraSetBossFlag
#
# replaced by FE FE FE FE FE. Removing the SET is the conservative move: nothing
# then needs clearing, and it takes a call away rather than introducing one the
# blob never made.
#
# SPAN A -- 0x13CA, the same 5 bytes as before, filled with
# `8C B9 FE FE FE` instead of five nops:
#
#     0x13CA  8C B9   by_callapi api_index=0xB9 = 185 CutScene_Stop
#     0x13CC  FE      nop
#     0x13CD  FE      nop
#     0x13CE  FE      nop
#
# The single inbound branch (`0x13B1 by_r_ifeqz` -> `0x13CA`) still lands on the
# FIRST byte of the span, which is now the first byte of the `by_callapi`, so it
# is still instruction-aligned; it calls `CutScene_Stop()` and falls through into
# `0x13CF by_c_push 0x14` -> `popapiparam` -> `SetState(20)` -> `exit` exactly as
# before. Nothing targets 0x13CB..0x13CE, so re-tiling inside the span is safe.
#
# `CutScene_Stop` is ARITY 0, so it needs no `by_c_push` and no `popapiparam`
# and fits inside the span already owned with three bytes to spare. The ORIGINAL
# bytes at the span are still `04 00 15 8C 71` and the existing guard still
# verifies them. `GotoNextLevel` is still gone; `CutScene_Stop()` now runs.
#
# BOTH NEW EDITS ARE ON SUSPICION. The evidence is the set/clear audit plus the
# API names, and nothing more. It is NOT established that boss camera flag 32 or
# the cutscene state gates player movement AT ALL, and neither edit has been
# observed to unfreeze the player. STRUCTURAL ONLY.

#: Neutralize the boss script's level-exit call. ON by default at the user's
#: explicit request after observing the teleport in game, and CONFIRMED in game
#: to stop the teleport. A kill switch, not a knob: False restores the verbatim
#: script exactly, including the full byte-identity static check.
NEUTRALIZE_LEVEL_EXIT: bool = True

#: Fill for the level-exit span. `CutScene_Stop` (api 185, arity 0) needs no
#: push, so it fits in the span already owned with three nops to spare. OFF
#: leaves the span as five nops, i.e. exactly today's behaviour. This switch
#: only has an effect while `NEUTRALIZE_LEVEL_EXIT` is True, because the call
#: is written into the span that neutralization owns -- no new bytes are ever
#: claimed for it.
STOP_CUTSCENE_ON_DEATH: bool = False

#: Offset of the three-instruction call site WITHIN the Script_Section (NOT
#: blob-absolute; blob-absolute is 0x536 + this = 0x1900).
LEVEL_EXIT_SCRIPT_OFFSET = 0x13CA

#: The exact 5 bytes that must be there. A different disc revision, a different
#: build, anything at all other than this, and the patch REFUSES. Bytes we did
#: not measure are never rewritten.
LEVEL_EXIT_ORIGINAL_BYTES = bytes((0x04, 0x00, 0x15, 0x8C, 0x71))

#: 0xFE `nop`: length 1, arity 0, verified in both revisions, and NOT in
#: `befscript.INVALID_OPCODES`.
LEVEL_EXIT_NOP_FILL      = bytes((0xFE,)) * 5              # exit removed only
LEVEL_EXIT_CUTSCENE_FILL = bytes((0x8C, 0xB9, 0xFE, 0xFE, 0xFE))  # + CutScene_Stop

#: --- THE REDIRECT (option four) ------------------------------------------
#: Neither freeze edit worked, and the ELF says why. `CameraSetBossFlag` is a
#: thin wrapper into `Camera::SetBossFlag` and cannot gate input;
#: `CutScene_Stop` only sets a deferred request word; all 106
#: `Player::SetAction` call sites live inside `Player` itself, so nothing
#: outside the player ever sets its action; and `mglGotoNextLevel` touches no
#: player state at all. What it DOES do is
#:     sw 6, gp-29056   (app mode)
#:     sw 3, gp-29092   (app SUBMODE = advance)
#:     swc1 f0, gp-29088  (transition time, arg scaled against 1000.0)
#: while `mglRestartLevel` writes 6 to that SAME submode word and nothing else.
#: So input is not being held by a lock the boss sets -- it is held because the
#: App never completes a transition, and in vanilla the level change is what
#: restores control.
#:
#: Therefore: do NOT remove the transition. REDIRECT it. `EnterSubLevel`
#: (api 94 = 0x5E, arity 1) is the SAME three-instruction
#: push/popapiparam/callapi idiom as the call being replaced, so the span stays
#: five bytes and no offset moves. The App still runs its mode-6 handoff and
#: restores input exactly the way vanilla does; it just lands in a sublevel of
#: the current world instead of the GraveDigger arena.
#:
#: Rejected: patching the submode constant at 0x129318 from 3 to 6, which would
#: turn EVERY level exit in the game into a restart and break progression
#: outright; and repointing a spare CScriptApi slot at `mglRestartLevel`, which
#: needs a dispatch table this module has NOT confirmed (the candidate found in
#: the ELF disagrees with the recovered spec at slots 0, 1 and 232, so it is
#: not the table).
REDIRECT_LEVEL_EXIT: bool = False

#: The sublevel operand. This is the ONE value not pinned by measurement:
#: `giNextSubLevel` is engine state and the grave hub's own index was never
#: read out. It is a single byte and it is safe to iterate on -- a wrong value
#: lands in a different sublevel, it does not corrupt anything.
BOSS_EXIT_SUBLEVEL: int = 0

#: api 94, arity 1, tail call `mglEnterSubLevel`.
ENTER_SUBLEVEL_API_INDEX = 0x5E

#: --- SPAN C: DO NOT RUN THE DEATH SEQUENCE AT ALL ------------------------
#: The chosen fix. Every attempt to release the freeze AFTER the death
#: sequence failed, and every candidate lock has been eliminated by
#: disassembly. So the death sequence never runs.
#:
#: The boss post-death chain is STRICTLY LINEAR and each state has EXACTLY ONE
#: entry site in the whole 2,996-instruction section:
#:     SetState(17) @0x170D  <- the only way in
#:     SetState(18) @0x10FF  (from state 17 UPDATE)
#:     SetState(21) @0x1363  (from state 18 UPDATE)
#:     SetState(20) @0x13D2  (from state 21 UPDATE)
#: `GotoNextLevel` @0x13CD lives inside that chain, as does
#: `CameraSetBossFlag(32)` @0x1128 and the 120-frame countdown. Replacing the
#: single entry at 0x170D makes the ENTIRE chain unreachable: no teleport, no
#: countdown, no camera flag, and nothing in it that could hold the player.
#:
#: `Kill` (api 28 = 0x1C, arity 1) is the same three-instruction
#: push/popapiparam/callapi idiom, so the span stays five bytes and no offset
#: moves. The boss dies and is removed instead of playing a death sequence.
#: COST: no death animation or effects -- he vanishes. That is the deliberate
#: trade for being able to walk afterwards.
#:
#: Because the chain is unreachable, span A is RETIRED and the level-exit bytes
#: ship VERBATIM: NEUTRALIZE_LEVEL_EXIT and REDIRECT_LEVEL_EXIT are both False.
KILL_INSTEAD_OF_DEATH_SEQUENCE: bool = False

#: Script_Section offset of the ONLY SetState(17) site (blob-absolute 0x1C40).
DEATH_ENTRY_SCRIPT_OFFSET = 0x170A

#: `by_c_push 17, popapiparam, by_callapi 0x2E (46 SetState)`. Verified, and the
#: patch REFUSES on any mismatch.
DEATH_ENTRY_ORIGINAL_BYTES = bytes((0x04, 0x11, 0x15, 0x8C, 0x2E))

#: `by_c_push 0, popapiparam, by_callapi 0x1C (28 Kill)`.
DEATH_ENTRY_KILL_FILL = bytes((0x04, 0x00, 0x15, 0x8C, 0x1C))

KILL_API_INDEX     = 0x1C

#: --- SPAN D: REMOVE THE EXIT AND EXPLICITLY UNFREEZE THE PLAYER ----------
#: `ControlPlayer` @0x00137B90 is called UNCONDITIONALLY from `MainLoop`
#: @0x0012AB14 -- the `app mode == 6` / `== 4` branches at 0x12AACC/0x12AAD8
#: skip only the camera updates and still fall into it. So input is always
#: READ; the freeze is the player's move state ignoring it, not a suppressed
#: input path.
#:
#: The mechanism was NOT identified. Nine candidates were eliminated against
#: the ELF (see design.md). Rather than assert a tenth, this span does the one
#: thing that directly names the symptom: it calls `ResetFreeze` on the player.
#:
#: `ResetFreeze` is api 3, arity 1, tail call GetEngineObjectLink. `-2` is the
#: player/target selector, established by THIS blob's own
#: `EnableCollisions(-2, 0, 0)` and `(-2, 5, 1)` calls at 0x0041, 0x0072,
#: 0x10AD, 0x10BA and 0x1703.
#:
#: TEN bytes are claimed, not five, because `c_push` of a negative literal is
#: 5 bytes on its own. The span covers the level-exit call AND the
#: `SetState(20)` that follows it. Losing `SetState(20)` is harmless: state 20
#: is the inert terminal state, both of whose handlers (T0[20]=0x138F,
#: T1[20]=0x1391) are a bare `exit`. The boss simply remains in state 21.
#:
#: WHY THIS SPAN IS REGRESSION-SAFE, unlike the state-17 attempt: 0x13CA is
#: PROVEN to execute only after death -- nopping it is what removed the
#: teleport in game. It cannot run during normal combat.
#:
#: HONEST STATUS: a grounded attempt, NOT a diagnosis. If `-2` is not the
#: player here, or freezing is not what holds him, this is a no-op and the
#: player stays stuck. It cannot make anything worse.
REPLACE_EXIT_WITH_UNFREEZE: bool = False

UNFREEZE_SCRIPT_OFFSET   = 0x13CA
UNFREEZE_ORIGINAL_BYTES  = bytes((0x04, 0x00, 0x15, 0x8C, 0x71,
                                  0x04, 0x14, 0x15, 0x8C, 0x2E))
UNFREEZE_FILL            = bytes((0x05, 0xFE, 0xFF, 0xFF, 0xFF,
                                  0x15, 0x8C, 0x03, 0x94, 0x00))
RESET_FREEZE_API_INDEX   = 0x03
SETSTATE_API_INDEX = 0x2E

#: Instruction count of the UNPATCHED section. Kept as a hard guard: if the
#: source section does not carry 2,996 instructions it is not the blob this
#: module was measured against and the patch REFUSES.
LEVEL_EXIT_INSTRUCTIONS_BEFORE = 2996

#: `GotoNextLevel`'s API slot. The AFTER instruction count and the AFTER callapi
#: total are NOT constants any more: they depend on which spans are enabled and
#: which fill each one gets, so `patch_boss_script` DERIVES them from the fills
#: it is about to write and reports before -> after instead of asserting a
#: literal. For the record, the single-span five-nop case is 2,996 -> 2,998
#: instructions and 337 -> 336 callapi sites.
LEVEL_EXIT_API_INDEX = 0x71
LEVEL_EXIT_CALLAPI_SITES_AFTER = 336

#: Span B: the boss camera flag set, removed outright.
REMOVE_BOSS_CAMERA_FLAG: bool = False
BOSS_FLAG_SCRIPT_OFFSET   = 0x1125
BOSS_FLAG_ORIGINAL_BYTES  = bytes((0x04, 0x20, 0x15, 0x8C, 0xE8))
BOSS_FLAG_NOP_FILL        = bytes((0xFE,)) * 5
CUTSCENE_STOP_API_INDEX   = 0xB9
BOSS_FLAG_SET_API_INDEX   = 0xE8
BOSS_FLAG_CLEAR_API_INDEX = 0xE9

#: Every api slot the report counts sites for, patched or not.
WATCHED_API_INDICES: tuple = (LEVEL_EXIT_API_INDEX, BOSS_FLAG_SET_API_INDEX,
                              BOSS_FLAG_CLEAR_API_INDEX, CUTSCENE_STOP_API_INDEX,
                              ENTER_SUBLEVEL_API_INDEX, KILL_API_INDEX,
                              SETSTATE_API_INDEX, RESET_FREEZE_API_INDEX)

#: ONE canonical string. Used verbatim by the option help text and the report.
#: It states what was PATCHED and nothing more: the neutralization is verified
#: STRUCTURALLY, in memory, and has NOT been observed in the running game.
EARLY_EXIT_RISK_TEXT: str = (
    "Early_Exit_Risk: the boss script's level exit is REDIRECTED, not removed. "
    "Script_Section 0x13CA, original bytes 04 00 15 8C 71 (by_c_push 0, "
    "popapiparam, by_callapi api 0x71 = 113 GotoNextLevel, the ONLY "
    "transition-API site in the blob), rewritten to 04 00 15 8C 5E "
    "(by_callapi api 0x5E = 94 EnterSubLevel, arity 1, the SAME 5-byte idiom). "
    "EXACTLY ONE BYTE changes and the instruction count is unchanged at 2,996. "
    "WHY REDIRECT: removing the call stopped the teleport but froze Maximo, and "
    "two attempts to release the freeze from inside the script both failed "
    "because there is no lock there to release. The ELF is explicit -- "
    "CameraSetBossFlag is a thin wrapper into Camera::SetBossFlag and cannot "
    "gate input, CutScene_Stop only sets a deferred request word, all 106 "
    "Player::SetAction call sites are inside Player itself so nothing external "
    "sets the player's action, and mglGotoNextLevel touches no player state at "
    "all -- it writes app mode 6 at gp-29056, submode 3 at gp-29092 and a "
    "transition time at gp-29088. Control is restored by the transition "
    "COMPLETING, which is why the only configuration ever seen working in game "
    "was the one that still called the exit. So the transition is kept and only "
    "its destination changes. UNMEASURED: BOSS_EXIT_SUBLEVEL is a guess at the "
    "grave hub's own sublevel index -- giNextSubLevel is engine state and was "
    "never read out. A wrong value lands in a different sublevel; it corrupts "
    "nothing. NOT OBSERVED IN GAME.")

#: The Tier 3 observations only the user, in the running game, can make.
OUTSTANDING_OBSERVATIONS: tuple = (
    "does the Boss_Entity render",
    "does it animate",
    "do its Orchestrated_Spawn minions appear",
    "does it have audio",
    "does the camera behave",
    "CAN MAXIMO MOVE AFTER THE BOSS DIES (the freeze; CutScene_Stop and the "
    "removal of CameraSetBossFlag(32) are the two suspicion-driven edits aimed "
    "at it -- structurally verified only, never observed in game)",
    "DOES KILLING HIM NOW LEAVE THE LEVEL INTACT (the exit call was "
    "neutralized -- CONFIRMED in game: no more teleport)",
    "does it fit the geometry -- clip, float, fall, or fail to path",
    "do the three shared types (0x1C, 0x23, 0x2A) behave with the host copy kept",
)

KNOWN_RISKS: tuple = (
    EARLY_EXIT_RISK_TEXT,
    "Patch-verification risk: every span is proven only STRUCTURALLY -- the "
    "patched section still decodes cleanly at coverage 1.0 with no failures, "
    "the instruction count is DERIVED from the fills actually written and "
    "reported before -> after, no callapi site naming a removed api remains, "
    "the section length and every byte outside the enabled spans are unchanged. "
    "What the VM does when it walks those bytes has NOT been watched. Only the "
    "running game can settle it.",
    "SUSPICION RISK on both freeze edits: removing CameraSetBossFlag(32) (span "
    "B, 0x1125) and calling CutScene_Stop() (span A, 0x13CA) rest on ONE piece "
    "of evidence each -- a set/clear audit of all 337 callapi sites showing "
    "these are the only two set-without-clear calls in the blob -- plus the API "
    "names. It is NOT ESTABLISHED that boss camera flag 32 or the cutscene "
    "state gates player movement AT ALL. The freeze may have an entirely "
    "different cause; neither edit has been observed to unfreeze Maximo.",
    "Shared-type variant risk: the boss script may expect the GRAVE_B copy of "
    "0x1C, 0x23 or 0x2A. Whether the two copies differ, and whether 0x29 "
    "depends on the difference, is UNMEASURED. The HOST copy is kept, because "
    "given a choice this design risks the new thing rather than the three "
    "pre-existing entities the player must get past.",
    "Geometry fit is STATICALLY UNVERIFIABLE: a boss-sized entity placed where "
    "a decorative record used to sit may clip, float, fall or fail to path.",
)


# ---------------------------------------------------------------------------
# Verdict shapes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Verdict:
    """Base shape every stage returns. `ok=False` is data, never an exception."""

    ok: bool = False
    reasons: tuple = ()
    details: tuple = ()

    @property
    def reason(self) -> Optional[str]:
        return self.reasons[0] if self.reasons else None


@dataclass(frozen=True)
class AnimTableVerdict(Verdict):
    """One anim table located WITHOUT the `TFA\\xff` signature search.

    See `derive_anim_table`: the signature anchor is wrong for `GRAVE_B.PRS`
    and is never used in this module.
    """

    count: int = 0
    walk_end: int = 0
    records_start: int = 0
    records_end: int = 0
    bounds: tuple = ()               # (index, start, end, tag)


@dataclass(frozen=True)
class SpanEdit:
    """ONE measured 5-byte Script_Section span this module may rewrite.

    `fill` is chosen by the switches, never by a caller. `removed_api` /
    `added_api` are what the report and the post-patch assertions are written
    against: a span that removes an api must leave ZERO sites naming it.
    """

    name: str
    offset: int
    original: bytes
    fill: bytes
    removed: str = ""
    added: str = ""
    removed_api: Optional[int] = None
    added_api: Optional[int] = None
    #: Filled in after the fill is disassembled on its own -- DERIVED, never a
    #: literal, so the expected instruction count follows the bytes written.
    fill_instructions: tuple = ()
    found: bytes = b""

    @property
    def stop(self) -> int:
        return self.offset + len(self.original)


@dataclass(frozen=True)
class LevelExitVerdict(Verdict):
    """The measured Script_Section edits this module is allowed to make.

    `applied=False` with `ok=True` is the all-switches-off case: nothing was
    touched. `ok=False` is a REFUSAL and carries no bytes, so the caller keeps
    the unpatched blob by construction.

    `offset`/`original_bytes`/`fill_bytes` describe the LEVEL-EXIT span when it
    is enabled (that is what the existing CLI/GUI reporting reads); `spans`
    carries every applied span, and is the field to read for the full picture.
    """

    applied: bool = False
    offset: int = LEVEL_EXIT_SCRIPT_OFFSET
    original_bytes: bytes = b""
    fill_bytes: bytes = b""
    found_bytes: bytes = b""
    instructions_before: int = 0
    instructions_after: int = 0
    instructions_expected_after: int = 0
    callapi_sites_before: int = 0
    callapi_sites_after: int = 0
    api_index_sites_after: int = 0
    section_length: int = 0
    nop_records: int = 0
    #: (SpanEdit, ...) -- every span actually applied, in ascending offset order.
    spans: tuple = ()
    #: ((api_index, before, after), ...) for every WATCHED_API_INDICES slot.
    api_site_counts: tuple = ()


@dataclass(frozen=True)
class MemberPort:
    """What one INJECTED Merge_Set member contributed to, and took from, the
    port. Reported per member so "which asset went where, for whom" is in the
    log rather than in somebody's head."""

    type_id: int
    source_mesh_refs: tuple = ()     # its LOCAL category-1 source indices
    source_anim_refs: tuple = ()     # its LOCAL category-2 source indices
    new_mesh_refs: tuple = ()        # the same slots after the rewrite
    new_anim_refs: tuple = ()
    cat0_count: int = 0
    #: ((anim_source_index, mesh_source_index) ...) -- which SINGLE mesh of
    #: THIS member each of its anim records binds to. Per-Attach_Tree, never
    #: unioned across members.
    anim_bindings: tuple = ()
    identity_ok: bool = False
    identity_detail: str = ""


@dataclass(frozen=True)
class PortVerdict(Verdict):
    """The result of porting the DERIVED asset closure of every injected member.

    `prs`/`prt`/`blob`/`blobs` are None/empty on refusal, so a refused port
    leaves the vanilla buffers untouched by construction.
    """

    prs: Optional[bytes] = None
    prt: Optional[bytes] = None
    blob: Optional[bytes] = None     # the rewritten BOSS blob (0x29)
    #: The level-exit neutralization verdict for the boss blob. `None` only
    #: when the port refused before reaching it.
    level_exit: Optional[LevelExitVerdict] = None
    #: ((type_id, rewritten_blob), ...) for EVERY injected member, boss
    #: included. This is what reaches `merge_bef(blobs=...)`.
    blobs: tuple = ()
    members: tuple = ()              # (MemberPort, ...)
    mesh_index_map: tuple = ()       # ((source_index, new_index), ...)
    anim_index_map: tuple = ()       # ((source_index, new_index), ...)
    tex_remap: tuple = ()            # ((source_index, new_index), ...)
    mesh_bytes: int = 0
    anim_bytes: int = 0
    tex_bytes: int = 0
    mesh_count: int = 0              # LIVE post-append counts
    anim_count: int = 0
    tex_count: int = 0
    clut_count: int = 0
    clut_cap: int = 0
    prs_bytes: int = 0
    prt_bytes: int = 0
    resident_total: int = 0
    budget: int = 0
    source_bone_count: int = 0
    #: ((type_id, anim_source_index, mesh_source_index, missing_names), ...)
    tolerated_anims: tuple = ()
    cat1_refs: tuple = ()            # the DEDUPED union of source mesh indices
    cat2_refs: tuple = ()            # the DEDUPED union of source anim indices
    cat0_count: int = 0
    mesh_entry_count: int = 0        # distinct mesh entries ported
    anim_record_count: int = 0       # distinct anim records ported

    @property
    def port_total(self) -> int:
        return self.mesh_bytes + self.anim_bytes + self.tex_bytes


@dataclass(frozen=True)
class ClosureMember:
    type_id: int
    depth: int                       # 0 = the Boss_Entity itself
    parent: Optional[int] = None     # who spawned it; None for the root
    direct: bool = False             # spawned by 0x29 itself


@dataclass(frozen=True)
class ClosureVerdict(Verdict):
    members: tuple = ()
    direct_ids: tuple = ()
    transitive_ids: tuple = ()
    unresolved_ids: tuple = ()


@dataclass(frozen=True)
class SharedTypeNote:
    type_id: int
    kept: str = "host"
    why: str = ""


@dataclass(frozen=True)
class BefMergeVerdict(Verdict):
    data: Optional[bytes] = None     # None on refusal - vanilla is untouched
    size_delta: int = 0
    per_type_bytes: tuple = ()
    emitted_type_count: int = 0
    vanilla_type_count: int = 0
    shared_types: tuple = ()


@dataclass(frozen=True)
class PlacementVerdict(Verdict):
    data: Optional[bytes] = None
    candidate_count: int = 0
    chosen_class: Optional[str] = None
    chosen_instance_name: Optional[str] = None
    chosen_size: Optional[int] = None
    victim_offset: Optional[int] = None
    victim_instance_id: Optional[int] = None
    justification: str = ""
    #: ACTUAL `len(out) - len(in)`. Negative when the boss record is smaller
    #: than the recycled Host_Slot, which is the normal case. Reported, never
    #: asserted to be 0.
    psx_size_delta: int = 0
    inventory: tuple = ()            # (class_name, type_id, size, count)
    #: "player_spawn" when the boss was placed at the player spawn plus
    #: `BOSS_SPAWN_OFFSET`, "victim_position" when the spawn was unreadable and
    #: the recycled decorative slot's own position was used instead.
    placement_strategy: str = ""
    #: The player-spawn coordinate as read from the level header, or None.
    player_spawn: Optional[tuple] = None
    #: The offset applied to it; () when the fallback was taken.
    spawn_offset: tuple = ()
    #: The FINAL world position the boss record carries.
    boss_position: Optional[tuple] = None


@dataclass(frozen=True)
class SoundVerdict(Verdict):
    data: Optional[bytes] = None
    merged_ids: tuple = ()
    skipped_ids: tuple = ()
    silent: bool = True
    bank_bytes: int = 0
    entry_count: int = 0
    ceiling: int = 0                 # from the VANILLA bank
    free_slots: int = 0


@dataclass(frozen=True)
class StaticCheck:
    name: str
    status: str                      # "PASS" | "FAIL" | "UNDETERMINED"
    inspected: int = 0               # 0 forces UNDETERMINED
    detail: str = ""


@dataclass(frozen=True)
class InjectionReport(Verdict):
    port: Optional[PortVerdict] = None
    closure: Optional[ClosureVerdict] = None
    bef: Optional[BefMergeVerdict] = None
    placement: Optional[PlacementVerdict] = None
    sound: Optional[SoundVerdict] = None
    checks: tuple = ()
    route_status: str = "REFUSED"    # "CANDIDATE" or "REFUSED"; never CONFIRMED
    outstanding: tuple = ()
    risks: tuple = ()
    target_name: str = ""


# ---------------------------------------------------------------------------
# Stage 1 -- THE PORT: the boss's own mesh, anim records and textures
# ---------------------------------------------------------------------------

def derive_anim_table(data, walk_end: int) -> AnimTableVerdict:
    """Locate a `.PRS` anim table FROM THE MESH WALK'S END. Never raises.

    WHY THIS EXISTS, and why `mesh_port.find_anim_table_anchor` must NOT be
    used from this module: that anchor is the first `TFA\\xff` occurrence minus
    12, and `TFA\\xff` is the MEA (tag 0x0A) record magic. `GRAVE_B.PRS`'s first
    22 records are SKA (tag 0x0C) and carry no such magic, so the signature
    lands on record 22 and the anchor reports a table of 13 records starting
    2,378,397 bytes in -- 569,861 bytes PAST the real table, and with every
    index shifted by 36. Measured: the real table starts at the mesh walk's end
    1,808,536 and declares 49 records, and those 49 records walk to 2,782,247
    == `len(data) - 16`. Slicing anim records 0..21 through the anchor would
    have handed the port 22 wrong byte ranges.

    The mesh walk's end is authoritative because it is the same cursor
    `insert_mesh_entry` writes at, so this is not an independent guess: it is
    the position the porter already commits to. It is corroborated instead by
    the format itself -- the u32 there must be the next resource table's type id
    (`prs_mesh_parser.NEXT_RESOURCE_TABLE_TYPE` == 2), and walking the declared
    count of records by `anim_port.record_length` must land EXACTLY on the
    trailer. Either check failing REFUSES; nothing is guessed or resynced.
    """
    end = len(data)
    if walk_end < 0 or walk_end + _anim_port.ANIM_HEADER_BYTES > end:
        return AnimTableVerdict(
            ok=False, walk_end=walk_end,
            reasons=("R1 anim table header does not fit the buffer",),
            details=(f"mesh walk ended at {walk_end}, buffer is {end} bytes, "
                     f"an {_anim_port.ANIM_HEADER_BYTES}-byte header needs "
                     f"more",))

    type_word = struct.unpack_from("<I", data, walk_end)[0]
    if type_word != _pmp.NEXT_RESOURCE_TABLE_TYPE:
        return AnimTableVerdict(
            ok=False, walk_end=walk_end,
            reasons=("R1 the word after the mesh table is not the anim "
                     "table's type id",),
            details=(f"u32 @{walk_end} reads {type_word} (0x{type_word:X}), "
                     f"expected {_pmp.NEXT_RESOURCE_TABLE_TYPE}; the mesh walk "
                     f"did not end where the anim table begins, so no offset "
                     f"derived from it can be trusted",))

    count = struct.unpack_from("<I", data, walk_end + 4)[0]
    if count <= 0 or count > 0xFFFF:
        return AnimTableVerdict(
            ok=False, walk_end=walk_end, count=count,
            reasons=("R1 anim record count is not a plausible table size",),
            details=(f"u32 @{walk_end + 4} reads {count}",))

    records_start = walk_end + _anim_port.ANIM_HEADER_BYTES
    limit = end - _anim_port.ANIM_TRAILER_BYTES
    bounds: list = []
    off = records_start
    for index in range(count):
        try:
            tag, length = _anim_port.record_length(data, off, limit=limit)
        except Exception as exc:                  # never raise out of a stage
            return AnimTableVerdict(
                ok=False, walk_end=walk_end, count=count,
                records_start=records_start, bounds=tuple(bounds),
                reasons=("R1 anim record did not measure",),
                details=(f"record {index} at {off}: {exc}",))
        bounds.append((index, off, off + length, tag))
        off += length

    if off != limit:
        return AnimTableVerdict(
            ok=False, walk_end=walk_end, count=count,
            records_start=records_start, records_end=off,
            bounds=tuple(bounds),
            reasons=("R1 anim walk did not end at the trailer",),
            details=(f"{count} record(s) walked from {records_start} ended at "
                     f"{off}, expected {limit} == len(data) - "
                     f"{_anim_port.ANIM_TRAILER_BYTES} ({off - limit:+d})",))

    return AnimTableVerdict(
        ok=True, count=count, walk_end=walk_end,
        records_start=records_start, records_end=off, bounds=tuple(bounds),
        details=(f"anim table at the mesh walk's end {walk_end}: type word "
                 f"{_pmp.NEXT_RESOURCE_TABLE_TYPE}, {count} record(s) "
                 f"[{records_start},{off}) ending exactly on the "
                 f"{_anim_port.ANIM_TRAILER_BYTES}-byte trailer",))


#: `BOSS_SOURCE_TEXTURES` and `BOSS_SOURCE_TEX_IDS` are GONE ON PURPOSE. They
#: named `GRAVE_B.PRT` entries 0, 1, 2 -- the three the BOSS's own mesh[0]
#: reaches -- and using them as the porting contract is precisely what left the
#: other ten Merge_Set members' textures behind. The reachable set is now
#: DERIVED from the render passes of every mesh actually ported (measured: the
#: closure adds {7,8,9,10,11,12,14,15,18,19,20,35,36}, 103,632 B, all fmt 0x13
#: indexed, for 13 palettes on top of the boss's 3). Two closure meshes (source
#: 19 and 23) bind only `-1`, and a `-1` pass is left exactly as it is.
#: Each entry's measured `(width, height, fmt, clut_index)` is REPORTED rather
#: than asserted against a table, because the table cannot be written for a set
#: that is derived at run time; the refusals that matter -- an entry the source
#: `.PRT` does not carry, a bound id the remap does not cover, a CLUT table that
#: cannot absorb the palettes -- are all still enforced below.


def _port_refusal(reason, details=(), **fields) -> PortVerdict:
    """A refused port. `prs`/`prt`/`blob` stay None, so the vanilla buffers are
    untouched BY CONSTRUCTION rather than by a caller remembering to."""
    return PortVerdict(ok=False, reasons=(reason,), details=tuple(details),
                       **fields)


def _member_refs(blob: bytes) -> tuple:
    """`(local_cat1, local_cat2, cat0_count, error)` for ONE member's blob.

    `error` is a string on failure and None otherwise; nothing raises out of
    here, so a malformed member becomes a refusal like any other.
    """
    try:
        refs = _bef.parse_material_refs(blob)
    except _bef.MaterialSectionError as exc:
        return (), (), 0, f"material section unwalkable: {exc}"
    except Exception as exc:                      # defensive
        return (), (), 0, f"material section unreadable: {exc!r}"
    cat1 = tuple(r for r in refs if not r.is_global
                 and r.category == _bef.MATERIAL_CATEGORY_MESH)
    cat2 = tuple(r for r in refs if not r.is_global
                 and r.category == _bef.MATERIAL_CATEGORY_ANIM)
    cat0 = sum(1 for r in refs if r.category == 0)
    return cat1, cat2, cat0, None


def _callapi_site_counts(disassembly, api_indices=WATCHED_API_INDICES) -> tuple:
    """`(total_callapi_sites, {api_index: sites_naming_it})`.

    Counts BOTH forms: wide `callapi` (0x8B, 4-byte `apiidx`) and narrow
    `by_callapi` (0x8C, 1-byte `api_index`). The measured sites are the narrow
    form, but counting only that form would miss a wide one, which is exactly
    the sort of blind spot this module refuses to have.
    """
    total = 0
    per_api = {int(a): 0 for a in api_indices}
    for ins in disassembly.instructions:
        if ins.mnemonic not in ("callapi", "by_callapi"):
            continue
        total += 1
        for operand in ins.operands:
            if operand.name != _befscript.API_INDEX_OPERAND_NAME:
                continue
            if operand.value in per_api:
                per_api[int(operand.value)] += 1
    return total, per_api


def _callapi_sites(disassembly) -> tuple:
    """`(total_callapi_sites, sites_naming_LEVEL_EXIT_API_INDEX)`. Kept for the
    one-api question; `_callapi_site_counts` answers it for every span."""
    total, per_api = _callapi_site_counts(disassembly, (LEVEL_EXIT_API_INDEX,))
    return total, per_api[LEVEL_EXIT_API_INDEX]


def enabled_spans() -> tuple:
    """The `SpanEdit`s the switches currently ask for, ascending by offset.

    `fill_instructions` is DERIVED here by disassembling each fill on its own,
    so the expected instruction count and the expected added/removed callapi
    sites follow the bytes that will actually be written rather than a literal
    somebody has to remember to update.
    """
    spans = []
    if REMOVE_BOSS_CAMERA_FLAG:
        spans.append(SpanEdit(
            name="boss camera flag set",
            offset=BOSS_FLAG_SCRIPT_OFFSET,
            original=BOSS_FLAG_ORIGINAL_BYTES,
            fill=BOSS_FLAG_NOP_FILL,
            removed=(f"by_c_push 32, popapiparam, by_callapi api "
                     f"0x{BOSS_FLAG_SET_API_INDEX:02X} "
                     f"({BOSS_FLAG_SET_API_INDEX} CameraSetBossFlag) -- the ONLY "
                     f"site setting boss camera flag 32, whose inverse "
                     f"CameraClearBossFlag ({BOSS_FLAG_CLEAR_API_INDEX}) is "
                     f"called nowhere in the blob"),
            added="nothing: five 0xFE nops",
            removed_api=BOSS_FLAG_SET_API_INDEX))
    if REPLACE_EXIT_WITH_UNFREEZE:
        spans.append(SpanEdit(
            name="level exit -> ResetFreeze(player)",
            offset=UNFREEZE_SCRIPT_OFFSET,
            original=UNFREEZE_ORIGINAL_BYTES,
            fill=UNFREEZE_FILL,
            removed=(f"by_c_push 0, popapiparam, by_callapi api "
                     f"0x{LEVEL_EXIT_API_INDEX:02X} ({LEVEL_EXIT_API_INDEX} "
                     f"GotoNextLevel) AND by_c_push 20, popapiparam, "
                     f"by_callapi api 0x{SETSTATE_API_INDEX:02X} "
                     f"({SETSTATE_API_INDEX} SetState) -- state 20 is the inert "
                     f"terminal, both handlers a bare exit, so the boss stays "
                     f"in state 21"),
            added=(f"c_push -2, popapiparam, by_callapi api "
                   f"0x{RESET_FREEZE_API_INDEX:02X} "
                   f"({RESET_FREEZE_API_INDEX} ResetFreeze, arity 1) then exit "
                   f"-- -2 is the player selector, per this blob's own "
                   f"EnableCollisions(-2, ...) calls"),
            removed_api=LEVEL_EXIT_API_INDEX,
            added_api=RESET_FREEZE_API_INDEX))
    if KILL_INSTEAD_OF_DEATH_SEQUENCE:
        spans.append(SpanEdit(
            name="death sequence entry -> Kill",
            offset=DEATH_ENTRY_SCRIPT_OFFSET,
            original=DEATH_ENTRY_ORIGINAL_BYTES,
            fill=DEATH_ENTRY_KILL_FILL,
            removed=(f"by_c_push 17, popapiparam, by_callapi api "
                     f"0x{SETSTATE_API_INDEX:02X} ({SETSTATE_API_INDEX} "
                     f"SetState) -- the ONLY SetState(17) site in the section, "
                     f"and the ONLY way into the post-death chain "
                     f"17 -> 18 -> 21 -> 20 that contains GotoNextLevel, "
                     f"CameraSetBossFlag(32) and the 120-frame countdown"),
            added=(f"by_c_push 0, popapiparam, by_callapi api "
                   f"0x{KILL_API_INDEX:02X} ({KILL_API_INDEX} Kill, arity 1) -- "
                   f"the SAME 5-byte idiom, so no offset moves. The boss is "
                   f"removed outright and the whole death chain becomes "
                   f"UNREACHABLE"),
            removed_api=SETSTATE_API_INDEX,
            added_api=KILL_API_INDEX))
    if NEUTRALIZE_LEVEL_EXIT and REDIRECT_LEVEL_EXIT:
        fill = bytes((0x04, BOSS_EXIT_SUBLEVEL & 0xFF, 0x15, 0x8C,
                      ENTER_SUBLEVEL_API_INDEX))
        spans.append(SpanEdit(
            name="level exit REDIRECTED to EnterSubLevel",
            offset=LEVEL_EXIT_SCRIPT_OFFSET,
            original=LEVEL_EXIT_ORIGINAL_BYTES,
            fill=fill,
            removed=(f"by_c_push 0, popapiparam, by_callapi api "
                     f"0x{LEVEL_EXIT_API_INDEX:02X} ({LEVEL_EXIT_API_INDEX} "
                     f"GotoNextLevel) -- the ONLY transition-API site in the "
                     f"blob"),
            added=(f"by_c_push {BOSS_EXIT_SUBLEVEL}, popapiparam, by_callapi "
                   f"api 0x{ENTER_SUBLEVEL_API_INDEX:02X} "
                   f"({ENTER_SUBLEVEL_API_INDEX} EnterSubLevel, arity 1) -- the "
                   f"SAME 5-byte idiom, so no offset moves. The App still "
                   f"completes its mode-6 transition and restores input the "
                   f"vanilla way; only the destination changes"),
            removed_api=LEVEL_EXIT_API_INDEX,
            added_api=ENTER_SUBLEVEL_API_INDEX))
    elif NEUTRALIZE_LEVEL_EXIT:
        cutscene = bool(STOP_CUTSCENE_ON_DEATH)
        spans.append(SpanEdit(
            name="level exit" + (" + cutscene stop" if cutscene else ""),
            offset=LEVEL_EXIT_SCRIPT_OFFSET,
            original=LEVEL_EXIT_ORIGINAL_BYTES,
            fill=LEVEL_EXIT_CUTSCENE_FILL if cutscene else LEVEL_EXIT_NOP_FILL,
            removed=(f"by_c_push 0, popapiparam, by_callapi api "
                     f"0x{LEVEL_EXIT_API_INDEX:02X} ({LEVEL_EXIT_API_INDEX} "
                     f"GotoNextLevel) -- the ONLY transition-API site in the "
                     f"blob"),
            added=(f"by_callapi api 0x{CUTSCENE_STOP_API_INDEX:02X} "
                   f"({CUTSCENE_STOP_API_INDEX} CutScene_Stop, ARITY 0 so no "
                   f"push is needed) + three 0xFE nops; CutScene_Start(1000) at "
                   f"0x0131 is stopped nowhere else in the blob"
                   if cutscene else "nothing: five 0xFE nops"),
            removed_api=LEVEL_EXIT_API_INDEX,
            added_api=CUTSCENE_STOP_API_INDEX if cutscene else None))

    out = []
    for span in sorted(spans, key=lambda s: s.offset):
        fill_dis = _befscript.disassemble(span.fill, _befscript.REVISION_RETAIL)
        out.append(SpanEdit(
            name=span.name, offset=span.offset, original=span.original,
            fill=span.fill, removed=span.removed, added=span.added,
            removed_api=span.removed_api, added_api=span.added_api,
            fill_instructions=tuple(
                (i.offset, i.mnemonic) for i in fill_dis.instructions)))
    return tuple(out)


def patch_boss_script(blob) -> tuple:
    """Apply the ENABLED measured Script_Section spans. Returns
    `(new_blob, LevelExitVerdict)` and NEVER raises.

    Length-preserving and byte-scoped: exactly the 5 bytes of each enabled span
    become that span's fill, and every other byte of the blob -- including the
    payload length at `0x0C`, the script offset at `0x38`, the material section
    and the offset-directory tables -- is proven unchanged before the patch is
    accepted. Every span verifies its ORIGINAL bytes exactly and REFUSES on
    mismatch, naming what it found. See the BOSS SCRIPT SPANS block above for
    the measurements this is built on.

    On refusal the ORIGINAL blob is returned with `ok=False`, so a caller that
    ignores the verdict still ships unpatched bytes rather than half-patched
    ones. `inject_boss` does not ignore it: a refusal refuses the injection.
    """
    blob = bytes(blob)
    spans = enabled_spans()
    exit_span = next((s for s in spans
                      if s.offset == LEVEL_EXIT_SCRIPT_OFFSET), None)
    headline = exit_span or (spans[0] if spans else None)

    def verdict(**kw):
        base = dict(
            offset=headline.offset if headline else LEVEL_EXIT_SCRIPT_OFFSET,
            original_bytes=headline.original if headline else b"",
            fill_bytes=headline.fill if headline else b"",
            spans=spans)
        base.update(kw)
        return LevelExitVerdict(**base)

    if not spans:
        return blob, LevelExitVerdict(
            ok=True, applied=False,
            original_bytes=LEVEL_EXIT_ORIGINAL_BYTES,
            fill_bytes=LEVEL_EXIT_NOP_FILL,
            details=(f"every span switch is off (NEUTRALIZE_LEVEL_EXIT="
                     f"{NEUTRALIZE_LEVEL_EXIT}, REMOVE_BOSS_CAMERA_FLAG="
                     f"{REMOVE_BOSS_CAMERA_FLAG}): the Script_Section ships "
                     f"verbatim and Early_Exit_Risk is un-mitigated",))

    # Overlapping spans would make "every byte outside the spans is unchanged"
    # meaningless, so they are refused rather than merged.
    for a, b in zip(spans, spans[1:]):
        if b.offset < a.stop:
            return blob, verdict(
                ok=False,
                reasons=("R16 two enabled spans overlap",),
                details=(f"[0x{a.offset:X},0x{a.stop:X}) and "
                         f"[0x{b.offset:X},0x{b.stop:X})",))
    for span in spans:
        if len(span.fill) != len(span.original):
            return blob, verdict(
                ok=False,
                reasons=("R16 a span fill is not length-preserving",),
                details=(f"{span.name}: {len(span.original)} B original vs "
                         f"{len(span.fill)} B fill",))

    try:
        section = _befscript.script_section(blob)
    except Exception as exc:
        return blob, verdict(
            ok=False,
            reasons=("R16 the boss blob's Script_Section could not be "
                     "extracted",),
            details=(f"{type(exc).__name__}: {exc}",))

    highest = max(s.stop for s in spans)
    if len(section) < highest:
        return blob, verdict(
            ok=False, section_length=len(section),
            reasons=("R16 the boss Script_Section is too short to contain the "
                     "measured span(s)",),
            details=(f"section is {len(section)} B, the spans need up to "
                     f"0x{highest:X}",))

    # --- every span's ORIGINAL bytes, verified before a byte is written ----
    checked = []
    for span in spans:
        found = bytes(section[span.offset:span.stop])
        if found != span.original:
            return blob, verdict(
                ok=False, found_bytes=found, section_length=len(section),
                reasons=(f"R16 the {span.name} span does not carry the "
                         f"measured bytes",),
                details=(f"Script_Section 0x{span.offset:X} reads "
                         f"{found.hex(' ')}, expected "
                         f"{span.original.hex(' ')} ({span.removed})",
                         "this is the guard against a different disc revision: "
                         "bytes that were not measured are NEVER rewritten",))
        checked.append(SpanEdit(
            name=span.name, offset=span.offset, original=span.original,
            fill=span.fill, removed=span.removed, added=span.added,
            removed_api=span.removed_api, added_api=span.added_api,
            fill_instructions=span.fill_instructions, found=found))
    spans = tuple(checked)

    before = _befscript.disassemble(section, _befscript.REVISION_RETAIL)
    calls_before, api_before = _callapi_site_counts(before)

    # DERIVED, never a literal, on BOTH sides. The original span used to be
    # assumed to be exactly three instructions -- the push/popapiparam/callapi
    # idiom -- but a span may claim more than one idiom (the ResetFreeze span
    # covers the exit call AND the SetState(20) after it, so six instructions
    # become four). Disassemble the original bytes and count them.
    def _orig_insn_count(span):
        return len(_befscript.disassemble(
            span.original, _befscript.REVISION_RETAIL).instructions)

    expected_after = len(before.instructions) + sum(
        len(s.fill_instructions) - _orig_insn_count(s) for s in spans)
    def _api_counts(raw):
        """Per-api callapi site counts in a raw byte string."""
        out = {}
        for ins in _befscript.disassemble(
                raw, _befscript.REVISION_RETAIL).instructions:
            for o in ins.operands:
                if (o.kind == "apiidx"
                        or o.name == _befscript.API_INDEX_OPERAND_NAME):
                    out[o.value] = out.get(o.value, 0) + 1
        return out

    # DERIVED FROM THE BYTES on both sides. `removed_api`/`added_api` are one
    # value each and cannot describe a span that drops TWO callapi sites and
    # adds one, which the ResetFreeze span does (GotoNextLevel and SetState out,
    # ResetFreeze in).
    expected_calls_after = calls_before + sum(
        sum(_api_counts(s.fill).values()) - sum(_api_counts(s.original).values())
        for s in spans)

    # --- the patch, in place, length-preserving ---------------------------
    script_start = int.from_bytes(
        blob[_befscript.BLOB_SCRIPT_OFFSET_OFFSET:
             _befscript.BLOB_SCRIPT_OFFSET_OFFSET + 4], "little")
    payload_end = int.from_bytes(
        blob[_befscript.BLOB_PAYLOAD_LENGTH_OFFSET:
             _befscript.BLOB_PAYLOAD_LENGTH_OFFSET + 4], "little")
    patched = bytearray(blob)
    for span in spans:
        patched[script_start + span.offset:script_start + span.stop] = span.fill
    patched = bytes(patched)

    if len(patched) != len(blob):
        return blob, verdict(
            ok=False,
            reasons=("R16 a fill changed the blob length",),
            details=(f"{len(blob)} B -> {len(patched)} B",))
    for label, offset in (("payload length @0x0C",
                           _befscript.BLOB_PAYLOAD_LENGTH_OFFSET),
                          ("script offset @0x38",
                           _befscript.BLOB_SCRIPT_OFFSET_OFFSET)):
        if patched[offset:offset + 4] != blob[offset:offset + 4]:
            return blob, verdict(
                ok=False,
                reasons=("R16 a fill disturbed a blob header field",),
                details=(f"{label} changed",))
    differing = [i for i in range(len(blob)) if patched[i] != blob[i]]
    allowed = set()
    for span in spans:
        allowed.update(range(script_start + span.offset,
                             script_start + span.stop))
    stray = [i for i in differing if i not in allowed]
    if stray:
        return blob, verdict(
            ok=False,
            reasons=("R16 a fill changed bytes outside the measured span(s)",),
            details=(f"{len(differing)} byte(s) differ, at most the "
                     f"{len(allowed)} inside "
                     + "; ".join(f"[0x{script_start + s.offset:X},"
                                 f"0x{script_start + s.stop:X})"
                                 for s in spans)
                     + f" are allowed; {len(stray)} unexpected offset(s) "
                     f"{[f'0x{i:X}' for i in stray[:16]]}",))
    if (payload_end != int.from_bytes(
            patched[_befscript.BLOB_PAYLOAD_LENGTH_OFFSET:
                    _befscript.BLOB_PAYLOAD_LENGTH_OFFSET + 4], "little")):
        return blob, verdict(     # defensive; covered above
            ok=False, reasons=("R16 the payload length field moved",))

    try:
        new_section = _befscript.script_section(patched)
    except Exception as exc:
        return blob, verdict(
            ok=False,
            reasons=("R16 the patched blob's Script_Section no longer "
                     "extracts",),
            details=(f"{type(exc).__name__}: {exc}",))
    if len(new_section) != len(section):
        return blob, verdict(
            ok=False,
            reasons=("R16 the patched Script_Section changed length",),
            details=(f"{len(section)} B -> {len(new_section)} B",))
    for span in spans:
        if bytes(new_section[span.offset:span.stop]) != span.fill:
            return blob, verdict(
                ok=False, reasons=(f"R16 the {span.name} fill did not land",),
                details=(f"0x{span.offset:X} reads "
                         f"{bytes(new_section[span.offset:span.stop]).hex(' ')},"
                         f" expected {span.fill.hex(' ')}",))

    # --- re-disassemble, revision EXPLICIT --------------------------------
    after = _befscript.disassemble(new_section, _befscript.REVISION_RETAIL)
    if (not after.clean or after.coverage != 1.0 or after.failures):
        return blob, verdict(
            ok=False, section_length=len(section),
            instructions_before=len(before.instructions),
            instructions_after=len(after.instructions),
            instructions_expected_after=expected_after,
            reasons=("R16 the patched Script_Section does not decode cleanly",),
            details=(f"clean={after.clean}, coverage={after.coverage}, "
                     f"{len(after.failures)} failure(s) "
                     f"{list(after.failures[:4])}",))
    if len(before.instructions) != LEVEL_EXIT_INSTRUCTIONS_BEFORE:
        return blob, verdict(
            ok=False, section_length=len(section),
            instructions_before=len(before.instructions),
            instructions_after=len(after.instructions),
            instructions_expected_after=expected_after,
            reasons=("R16 the unpatched Script_Section does not carry the "
                     "measured instruction count",),
            details=(f"{len(before.instructions)} instruction(s), measured "
                     f"{LEVEL_EXIT_INSTRUCTIONS_BEFORE}",))
    if len(after.instructions) != expected_after:
        return blob, verdict(
            ok=False, section_length=len(section),
            instructions_before=len(before.instructions),
            instructions_after=len(after.instructions),
            instructions_expected_after=expected_after,
            reasons=("R16 the patched instruction count is not the DERIVED "
                     "expectation",),
            details=(f"{len(before.instructions)} -> "
                     f"{len(after.instructions)}, derived expectation "
                     f"{expected_after} from "
                     + "; ".join(
                         f"{s.name}: {_orig_insn_count(s)} instruction(s) -> "
                         f"{len(s.fill_instructions)}" for s in spans),))

    # --- per span: the fill decodes to the intended instructions ----------
    decoded_per_span = []
    for span in spans:
        got = [i for i in after.instructions
               if span.offset <= i.offset < span.stop]
        want = [m for _o, m in span.fill_instructions]
        if [i.mnemonic for i in got] != want or \
                [i.offset - span.offset for i in got] != \
                [o for o, _m in span.fill_instructions]:
            return blob, verdict(
                ok=False, section_length=len(section),
                instructions_before=len(before.instructions),
                instructions_after=len(after.instructions),
                instructions_expected_after=expected_after,
                reasons=(f"R16 the {span.name} span does not decode as its "
                         f"intended fill",),
                details=("decoded: " + ", ".join(
                    f"@0x{i.offset:X} {i.mnemonic}" for i in got)
                    + "; intended: " + ", ".join(
                        f"+{o} {m}" for o, m in span.fill_instructions),))
        decoded_per_span.append(tuple(
            (i.offset, i.mnemonic) for i in got))

    calls_after, api_after = _callapi_site_counts(after)

    # Per-api EXPECTED DELTAS, aggregated over the enabled spans. A COUNT
    # check, not a zero check: `SetState` has ~40 sites and `Kill` already has
    # one at 0x156A, so "removed reaches 0" and "added equals 1" only hold for
    # apis that happen to be unique in this blob (GotoNextLevel,
    # CameraSetBossFlag). Deltas are correct in every case, including an api
    # that one span removes and another adds.
    delta = {}
    for span in spans:
        for idx, n in _api_counts(span.original).items():
            delta[idx] = delta.get(idx, 0) - n
        for idx, n in _api_counts(span.fill).items():
            delta[idx] = delta.get(idx, 0) + n
    for idx in sorted(delta):
        want = api_before[idx] + delta[idx]
        if api_after[idx] != want:
            return blob, verdict(
                ok=False, section_length=len(section),
                callapi_sites_before=calls_before,
                callapi_sites_after=calls_after,
                api_index_sites_after=api_after[LEVEL_EXIT_API_INDEX],
                instructions_before=len(before.instructions),
                instructions_after=len(after.instructions),
                instructions_expected_after=expected_after,
                reasons=(f"R16 the site count for api 0x{idx:02X} is not the "
                         f"DERIVED expectation after the fill(s)",),
                details=(f"api 0x{idx:02X}: {api_before[idx]} before, "
                         f"{api_after[idx]} after, expected {want} "
                         f"(delta {delta[idx]:+d})",))

    if calls_after != expected_calls_after:
        return blob, verdict(
            ok=False, section_length=len(section),
            callapi_sites_before=calls_before, callapi_sites_after=calls_after,
            instructions_before=len(before.instructions),
            instructions_after=len(after.instructions),
            instructions_expected_after=expected_after,
            reasons=("R16 the remaining callapi site count is not the DERIVED "
                     "expectation",),
            details=(f"{calls_before} -> {calls_after}, derived expectation "
                     f"{expected_calls_after}",))

    nop_records = sum(1 for s, decoded in zip(spans, decoded_per_span)
                      for _o, m in decoded if m == "nop")
    span_details = tuple(
        f"span {s.name} at Script_Section 0x{s.offset:X} (blob-absolute "
        f"0x{script_start + s.offset:X}): {s.original.hex(' ')} -> "
        f"{s.fill.hex(' ')}; REMOVED {s.removed}; ADDED {s.added}; decodes as "
        + ", ".join(f"@0x{o:X} {m}" for o, m in decoded)
        for s, decoded in zip(spans, decoded_per_span))

    return patched, LevelExitVerdict(
        ok=True, applied=True,
        offset=headline.offset, original_bytes=headline.original,
        fill_bytes=headline.fill, found_bytes=headline.found,
        spans=spans,
        section_length=len(new_section),
        instructions_before=len(before.instructions),
        instructions_after=len(after.instructions),
        instructions_expected_after=expected_after,
        callapi_sites_before=calls_before, callapi_sites_after=calls_after,
        api_index_sites_after=api_after[LEVEL_EXIT_API_INDEX],
        api_site_counts=tuple((a, api_before[a], api_after[a])
                              for a in WATCHED_API_INDICES),
        nop_records=nop_records,
        details=(f"{len(spans)} measured span(s) applied in place, "
                 f"length-preserving: "
                 + ", ".join(f"0x{s.offset:X} {s.name}" for s in spans),)
                + span_details
                + (f"LENGTH-PRESERVING: blob {len(blob):,} B unchanged, "
                   f"Script_Section {len(new_section):,} B unchanged, payload "
                   f"length @0x0C and script offset @0x38 unchanged, and "
                   f"exactly {len(differing)} byte(s) differ from the source -- "
                   f"all of them inside the enabled span(s); NO instruction "
                   f"offset moved, so the offset-directory tables at 0x442 stay "
                   f"instruction-aligned",
                   f"re-disassembled with revision "
                   f"{_befscript.REVISION_RETAIL} EXPLICIT: clean, coverage "
                   f"{after.coverage}, 0 failures, instructions "
                   f"{len(before.instructions)} -> {len(after.instructions)} "
                   f"against a DERIVED expectation of {expected_after}",
                   f"callapi sites {calls_before} -> {calls_after} (derived "
                   f"expectation {expected_calls_after}); per-api sites "
                   + ", ".join(
                       f"0x{a:02X} {api_before[a]}->{api_after[a]}"
                       for a in WATCHED_API_INDICES),
                   "STRUCTURAL ONLY. The level exit is removed AND "
                   "ResetFreeze(-2) is called on the player in its place, at "
                   "the one site PROVEN to run only after death. The freeze "
                   "MECHANISM WAS NOT IDENTIFIED: ControlPlayer runs "
                   "unconditionally from MainLoop, so input is always read and "
                   "the player is stuck in a move state instead. Nine "
                   "candidates were eliminated against the ELF -- see "
                   "design.md. This span is a GROUNDED ATTEMPT, NOT A "
                   "DIAGNOSIS: if -2 is not the player here, or freezing is "
                   "not what holds him, it is a no-op. It cannot regress "
                   "normal combat. NOT OBSERVED IN GAME"))


#: The old name. `patch_boss_script` applies more than the one span now, and the
#: name says so; this alias keeps any external caller working.
neutralize_level_exit = patch_boss_script


def neutralize_level_exit(blob) -> tuple:
    """Nop out the boss script's `GotoNextLevel` call. Returns
    `(new_blob, LevelExitVerdict)` and NEVER raises.

    Length-preserving and byte-scoped: exactly the 5 bytes at
    `LEVEL_EXIT_SCRIPT_OFFSET` within the Script_Section become
    `LEVEL_EXIT_NOP_FILL`, and every other byte of the blob -- including the
    payload length at `0x0C`, the script offset at `0x38`, the material section
    and the offset-directory tables -- is proven unchanged before the patch is
    accepted. See the LEVEL-EXIT NEUTRALIZATION block above for the measurement
    this is built on.

    On refusal the ORIGINAL blob is returned with `ok=False`, so a caller that
    ignores the verdict still ships unpatched bytes rather than half-patched
    ones. `inject_boss` does not ignore it: a refusal refuses the injection.
    """
    blob = bytes(blob)
    start = LEVEL_EXIT_SCRIPT_OFFSET
    stop = start + len(LEVEL_EXIT_ORIGINAL_BYTES)

    if not NEUTRALIZE_LEVEL_EXIT:
        return blob, LevelExitVerdict(
            ok=True, applied=False,
            original_bytes=LEVEL_EXIT_ORIGINAL_BYTES,
            fill_bytes=LEVEL_EXIT_NOP_FILL,
            details=("NEUTRALIZE_LEVEL_EXIT is False: the Script_Section ships "
                     "verbatim and Early_Exit_Risk is un-mitigated",))

    try:
        section = _befscript.script_section(blob)
    except Exception as exc:
        return blob, LevelExitVerdict(
            ok=False,
            reasons=("R16 the boss blob's Script_Section could not be "
                     "extracted",),
            details=(f"{type(exc).__name__}: {exc}",))

    if len(section) < stop:
        return blob, LevelExitVerdict(
            ok=False, section_length=len(section),
            reasons=("R16 the boss Script_Section is too short to contain the "
                     "measured level-exit call site",),
            details=(f"section is {len(section)} B, the site needs "
                     f"[0x{start:X},0x{stop:X})",))

    found = bytes(section[start:stop])
    if found != LEVEL_EXIT_ORIGINAL_BYTES:
        return blob, LevelExitVerdict(
            ok=False, found_bytes=found, section_length=len(section),
            original_bytes=LEVEL_EXIT_ORIGINAL_BYTES,
            reasons=("R16 the level-exit call site does not carry the measured "
                     "bytes",),
            details=(f"Script_Section 0x{start:X} reads "
                     f"{found.hex(' ')}, expected "
                     f"{LEVEL_EXIT_ORIGINAL_BYTES.hex(' ')} (by_c_push 0, "
                     f"popapiparam, by_callapi api 0x"
                     f"{LEVEL_EXIT_API_INDEX:02X})",
                     "this is the guard against a different disc revision: "
                     "bytes that were not measured are NEVER rewritten",))

    before = _befscript.disassemble(section, _befscript.REVISION_RETAIL)
    calls_before, slot_before = _callapi_sites(before)

    # --- the patch, in place, length-preserving ---------------------------
    script_start = int.from_bytes(
        blob[_befscript.BLOB_SCRIPT_OFFSET_OFFSET:
             _befscript.BLOB_SCRIPT_OFFSET_OFFSET + 4], "little")
    payload_end = int.from_bytes(
        blob[_befscript.BLOB_PAYLOAD_LENGTH_OFFSET:
             _befscript.BLOB_PAYLOAD_LENGTH_OFFSET + 4], "little")
    patched = bytearray(blob)
    patched[script_start + start:script_start + stop] = LEVEL_EXIT_NOP_FILL
    patched = bytes(patched)

    if len(patched) != len(blob):
        return blob, LevelExitVerdict(
            ok=False,
            reasons=("R16 the fill changed the blob length",),
            details=(f"{len(blob)} B -> {len(patched)} B",))
    for label, offset in (("payload length @0x0C",
                           _befscript.BLOB_PAYLOAD_LENGTH_OFFSET),
                          ("script offset @0x38",
                           _befscript.BLOB_SCRIPT_OFFSET_OFFSET)):
        if patched[offset:offset + 4] != blob[offset:offset + 4]:
            return blob, LevelExitVerdict(
                ok=False,
                reasons=("R16 the fill disturbed a blob header field",),
                details=(f"{label} changed",))
    differing = [i for i in range(len(blob)) if patched[i] != blob[i]]
    expected_span = list(range(script_start + start, script_start + stop))
    if differing != expected_span:
        stray = [i for i in differing if i not in set(expected_span)]
        return blob, LevelExitVerdict(
            ok=False,
            reasons=("R16 the fill changed bytes outside the measured span",),
            details=(f"{len(differing)} byte(s) differ, expected exactly the "
                     f"{len(expected_span)} at blob-absolute "
                     f"[0x{expected_span[0]:X},0x{expected_span[-1] + 1:X}); "
                     f"{len(stray)} unexpected offset(s) "
                     f"{[f'0x{i:X}' for i in stray[:16]]}",))
    if (payload_end != int.from_bytes(
            patched[_befscript.BLOB_PAYLOAD_LENGTH_OFFSET:
                    _befscript.BLOB_PAYLOAD_LENGTH_OFFSET + 4], "little")):
        return blob, LevelExitVerdict(     # defensive; covered above
            ok=False, reasons=("R16 the payload length field moved",))

    try:
        new_section = _befscript.script_section(patched)
    except Exception as exc:
        return blob, LevelExitVerdict(
            ok=False,
            reasons=("R16 the patched blob's Script_Section no longer "
                     "extracts",),
            details=(f"{type(exc).__name__}: {exc}",))
    if len(new_section) != len(section):
        return blob, LevelExitVerdict(
            ok=False,
            reasons=("R16 the patched Script_Section changed length",),
            details=(f"{len(section)} B -> {len(new_section)} B",))
    if new_section[start:stop] != LEVEL_EXIT_NOP_FILL:
        return blob, LevelExitVerdict(
            ok=False, reasons=("R16 the fill did not land",),
            details=(f"0x{start:X} reads "
                     f"{bytes(new_section[start:stop]).hex(' ')}",))

    # --- re-disassemble, revision EXPLICIT --------------------------------
    after = _befscript.disassemble(new_section, _befscript.REVISION_RETAIL)
    if (not after.clean or after.coverage != 1.0 or after.failures):
        return blob, LevelExitVerdict(
            ok=False, section_length=len(section),
            instructions_before=len(before.instructions),
            instructions_after=len(after.instructions),
            reasons=("R16 the patched Script_Section does not decode cleanly",),
            details=(f"clean={after.clean}, coverage={after.coverage}, "
                     f"{len(after.failures)} failure(s) "
                     f"{list(after.failures[:4])}",))
    if len(before.instructions) != LEVEL_EXIT_INSTRUCTIONS_BEFORE:
        return blob, LevelExitVerdict(
            ok=False, section_length=len(section),
            instructions_before=len(before.instructions),
            instructions_after=len(after.instructions),
            reasons=("R16 the unpatched Script_Section does not carry the "
                     "measured instruction count",),
            details=(f"{len(before.instructions)} instruction(s), measured "
                     f"{LEVEL_EXIT_INSTRUCTIONS_BEFORE}",))
    if len(after.instructions) != LEVEL_EXIT_INSTRUCTIONS_AFTER:
        return blob, LevelExitVerdict(
            ok=False, section_length=len(section),
            instructions_before=len(before.instructions),
            instructions_after=len(after.instructions),
            reasons=("R16 the patched instruction count is not the expected "
                     "one",),
            details=(f"{len(before.instructions)} -> "
                     f"{len(after.instructions)}, expected "
                     f"{LEVEL_EXIT_INSTRUCTIONS_BEFORE} -> "
                     f"{LEVEL_EXIT_INSTRUCTIONS_AFTER} (three instructions "
                     f"become five length-1 nops, so the count rises by "
                     f"exactly two)",))

    nops = [i for i in after.instructions
            if start <= i.offset < stop]
    if len(nops) != len(LEVEL_EXIT_NOP_FILL) or any(
            i.mnemonic != "nop" or i.operands for i in nops):
        return blob, LevelExitVerdict(
            ok=False, section_length=len(section), nop_records=len(nops),
            instructions_before=len(before.instructions),
            instructions_after=len(after.instructions),
            reasons=("R16 the filled span does not decode as five nops",),
            details=("decoded: " + ", ".join(
                f"@0x{i.offset:X} {i.mnemonic}" for i in nops),))

    calls_after, slot_after = _callapi_sites(after)
    if slot_after != 0:
        return blob, LevelExitVerdict(
            ok=False, section_length=len(section),
            callapi_sites_before=calls_before, callapi_sites_after=calls_after,
            api_index_sites_after=slot_after,
            instructions_before=len(before.instructions),
            instructions_after=len(after.instructions),
            reasons=(f"R16 a callapi site naming api "
                     f"0x{LEVEL_EXIT_API_INDEX:02X} remains after the fill",),
            details=(f"{slot_after} site(s) remain, {slot_before} before",))
    if calls_after != LEVEL_EXIT_CALLAPI_SITES_AFTER:
        return blob, LevelExitVerdict(
            ok=False, section_length=len(section),
            callapi_sites_before=calls_before, callapi_sites_after=calls_after,
            instructions_before=len(before.instructions),
            instructions_after=len(after.instructions),
            reasons=("R16 the remaining callapi site count is not the "
                     "expected one",),
            details=(f"{calls_before} -> {calls_after}, expected "
                     f"{LEVEL_EXIT_CALLAPI_SITES_AFTER}",))

    return patched, LevelExitVerdict(
        ok=True, applied=True, offset=start,
        original_bytes=LEVEL_EXIT_ORIGINAL_BYTES,
        fill_bytes=LEVEL_EXIT_NOP_FILL, found_bytes=found,
        section_length=len(new_section),
        instructions_before=len(before.instructions),
        instructions_after=len(after.instructions),
        callapi_sites_before=calls_before, callapi_sites_after=calls_after,
        api_index_sites_after=0, nop_records=len(nops),
        details=(f"level-exit call NEUTRALIZED in place at Script_Section "
                 f"0x{start:X} (blob-absolute 0x{script_start + start:X}): "
                 f"{LEVEL_EXIT_ORIGINAL_BYTES.hex(' ')} -> "
                 f"{LEVEL_EXIT_NOP_FILL.hex(' ')}",
                 f"the three replaced instructions were by_c_push value=0, "
                 f"popapiparam, by_callapi api_index=0x"
                 f"{LEVEL_EXIT_API_INDEX:02X} ({LEVEL_EXIT_API_INDEX} "
                 f"GotoNextLevel, narrow form 0x8C); 5 bytes is the smallest "
                 f"push/consume-BALANCED unit",
                 f"LENGTH-PRESERVING: blob {len(blob):,} B unchanged, "
                 f"Script_Section {len(new_section):,} B unchanged, payload "
                 f"length @0x0C and script offset @0x38 unchanged, and exactly "
                 f"{len(expected_span)} byte(s) differ from the source -- all "
                 f"of them inside the span",
                 f"re-disassembled with revision "
                 f"{_befscript.REVISION_RETAIL} EXPLICIT: clean, coverage "
                 f"{after.coverage}, 0 failures, instructions "
                 f"{len(before.instructions)} -> {len(after.instructions)}, "
                 f"the span decoding as {len(nops)} length-1 arity-0 nop(s)",
                 f"callapi sites {calls_before} -> {calls_after}; sites naming "
                 f"api 0x{LEVEL_EXIT_API_INDEX:02X} "
                 f"{slot_before} -> {slot_after}",
                 "STRUCTURAL ONLY: nobody has confirmed IN GAME that killing "
                 "the boss now leaves the host level intact"))


def port_boss_assets(game_files_dir, member_blobs, *, log=None) -> PortVerdict:
    """Port the DERIVED asset closure of EVERY INJECTED Merge_Set member.

    `member_blobs` is `{type_id: blob}` for every member that will actually be
    INJECTED -- i.e. the Merge_Set minus the types `GRAVE.BEF` already declares
    -- with `BOSS_TYPE` among them. That is why `derive_merge_set` now runs
    BEFORE this call: the port cannot know what to carry until it knows who is
    travelling. Porting `0x29`'s assets alone, which is what this function used
    to do, left the other ten members' LOCAL references pointing at whatever
    `GRAVE.PRS` happens to hold at the same index -- 21 slots, all in range, all
    wrong (see the module docstring).

    NOTHING here is constant-driven. For every injected member the LOCAL
    category-1 and category-2 references are parsed out of its own blob, the
    union is DEDUPED, each distinct mesh entry and anim record travels ONCE, the
    reachable texture set is derived from the ported meshes' own render passes,
    and EVERY member's blob is rewritten through the resulting maps.
    `BOSS_SOURCE_MESH_INDEX` / `BOSS_SOURCE_ANIM_INDICES` survive only as a
    SANITY check that the derivation still sees the boss where it was measured.

    Order, and one `.PRS` in memory at a time (at most four binaries in flight):

      1. `GRAVE_B.PRS`: gate the walk; for EVERY mesh in the union slice the
         entry, take its signature, its bone AND mesh-node names and its render
         passes; slice EVERY anim record in the union through `derive_anim_table`;
         run the per-member, per-mesh binding check. The buffer is dropped
         before the target is opened, so every byte needed later is sliced in
         this single pass.
      2. `GRAVE.PRS`: gate the walk, append each mesh entry
         (`mesh_port.insert_mesh_entry`) and every record in one
         `anim_port.append_anim_records` call.
      3. `GRAVE_B.PRT` -> `GRAVE.PRT`: port each distinct reachable texture ONCE
         with `texture_port.extract_texture` / `inject_texture`.
      4. Rewrite EVERY appended mesh copy's render-pass texture ids through the
         remap with `prs_mesh_parser.patch_texture_ids`; `-1` passes untouched.
      5. Rewrite EVERY injected member's LOCAL references with
         `bef.set_material_ref_index`, leaving category-0 refs alone, then
         re-parse and prove each one ref for ref -- and additionally prove
         IDENTITY: every rewritten index must equal a MAP VALUE, never a source
         index that merely happens to be in range.

    Every low-level call is the one `cross_world._port_enemy` uses, in the same
    order, under the same discipline.

    BINDING IS CHECKED PER MEMBER, PER MESH -- NEVER UNIONED. `RecurseAttachAnim`
    walks ONE `PMeshNode` tree, so a record must bind to a SINGLE mesh of the
    member that references it. `mesh_entry_anim_target_names` returns a PAIR and
    unpacking it is load-bearing: SKA (`0x0C`) records address BONES, MEA
    (`0x0A`) records address MESH NODES. Every companion record measured is MEA,
    so nodes are what matter for them; iterating the pair itself would hand the
    gate a two-element list of TUPLES that no name can ever match (the
    `companion_port` precedent).

    NAME TOLERANCE, SCOPED EXACTLY AS BEFORE. A record whose names do not
    resolve ships UNCHANGED only when (a) the rig is a mesh ported here
    byte-for-byte as `GRAVE_B` ships it and (b) the same names are missing from
    THAT SAME MESH IN THE SOURCE ARCHIVE, so the engine's lookup fails
    identically in vanilla -- porting renames nothing and changes only index
    NUMBERS. Every such case is recorded in `tolerated_anims` with the member,
    the record index, the mesh it was checked against and the missing names.
    `can_bind_to_rig` is NOT relaxed, `MEA_UNMATCHED_NODE_IS_SKIPPED` is NOT
    extended, and no name is substituted. A record whose binding is
    UNDETERMINED or BY_INDEX still REFUSES, and dropping a record is never an
    option: the blob carries references TO it, so omitting it would leave a
    DANGLING REFERENCE.
    """
    def emit(message: str) -> None:
        if log is not None:
            _emit(log, message)

    game_files_dir = Path(game_files_dir)
    src_prs_path = game_files_dir / "GRAVE_B.PRS"
    src_prt_path = game_files_dir / "GRAVE_B.PRT"
    dst_prs_path = game_files_dir / "GRAVE.PRS"
    dst_prt_path = game_files_dir / "GRAVE.PRT"
    for p in (src_prs_path, src_prt_path, dst_prs_path, dst_prt_path):
        if not p.exists():
            return _port_refusal(f"{p.name} is missing")

    budget = _roster_selection.pool_budget(raised=POOL_BUDGET_RAISED)
    ref_fields: dict = dict(budget=budget)

    member_blobs = dict(member_blobs or {})
    if BOSS_TYPE not in member_blobs:
        return _port_refusal(
            f"R2 the injected member set does not include the Boss_Entity "
            f"0x{BOSS_TYPE:02X}",
            (f"members offered: {[f'0x{t:02X}' for t in sorted(member_blobs)]}",),
            **ref_fields)
    member_ids = [BOSS_TYPE] + [t for t in sorted(member_blobs) if t != BOSS_TYPE]

    # --- every member's own references, before a byte of asset is read -----
    per_member_refs: dict = {}
    union_mesh: set = set()
    union_anim: set = set()
    cat0_total = 0
    for type_id in member_ids:
        cat1, cat2, cat0, error = _member_refs(member_blobs[type_id])
        if error is not None:
            return _port_refusal(
                f"R3 member 0x{type_id:02X}: {error}", **ref_fields)
        per_member_refs[type_id] = (cat1, cat2, cat0)
        union_mesh.update(r.index for r in cat1)
        union_anim.update(r.index for r in cat2)
        cat0_total += cat0
    ref_fields["cat0_count"] = cat0_total
    ref_fields["cat1_refs"] = tuple(sorted(union_mesh))
    ref_fields["cat2_refs"] = tuple(sorted(union_anim))

    # SANITY, not the contract: the boss must still resolve to mesh 0 and anims
    # 0..21. If the derivation stops seeing him where he was measured, the
    # measurement this module is built on no longer describes the data.
    boss_cat1, boss_cat2, _boss_cat0 = per_member_refs[BOSS_TYPE]
    boss_mesh_src = sorted({r.index for r in boss_cat1})
    boss_anim_src = sorted({r.index for r in boss_cat2})
    if boss_mesh_src != [BOSS_SOURCE_MESH_INDEX]:
        return _port_refusal(
            "R2 SANITY: the boss blob no longer names exactly one LOCAL "
            "category-1 mesh",
            (f"local category-1 indices {boss_mesh_src}, expected "
             f"[{BOSS_SOURCE_MESH_INDEX}]",), **ref_fields)
    if boss_anim_src != list(BOSS_SOURCE_ANIM_INDICES):
        return _port_refusal(
            "R2 SANITY: the boss blob no longer names anim records 0..21",
            (f"local category-2 indices {boss_anim_src}, expected "
             f"{list(BOSS_SOURCE_ANIM_INDICES)}",), **ref_fields)

    for type_id in member_ids:
        cat1, cat2, _c0 = per_member_refs[type_id]
        if cat2 and not cat1:
            return _port_refusal(
                f"R2 member 0x{type_id:02X} references anim records but names "
                f"no mesh of its own to bind them to",
                (f"{len(cat2)} LOCAL category-2 reference(s) "
                 f"{sorted({r.index for r in cat2})}, zero LOCAL category-1; "
                 f"binding is per-Attach_Tree, so there is nothing to check "
                 f"the records against",), **ref_fields)

    mesh_order = sorted(union_mesh)
    anim_order = sorted(union_anim)

    # --- 1. GRAVE_B.PRS, held alone and dropped before the target opens ---
    src = src_prs_path.read_bytes()
    try:
        walk = _mesh_port.walk_mesh_table(src)
        if walk.events:
            first = walk.events[0]
            return _port_refusal(
                f"R1 GRAVE_B.PRS mesh walk not clean: {len(walk.events)} "
                f"resync(s)",
                (f"declared mesh count {walk.count}, entries walked "
                 f"{len(walk.bounds)}, first resync in mesh[{first[0]}] via "
                 f"{first[1]}: {first[2]}",), **ref_fields)

        table = derive_anim_table(src, walk.end)
        if not table.ok:
            return _port_refusal(f"GRAVE_B.PRS: {table.reason}",
                                 table.details, **ref_fields)

        # Every mesh in the union: entry bytes, signature, target names.
        entry_bytes: dict = {}
        mesh_tag: dict = {}
        mesh_bone_count: dict = {}
        mesh_pass_count: dict = {}
        rig_bones: dict = {}
        rig_nodes: dict = {}
        for index in mesh_order:
            gate = _mesh_port.assess_source_entry(src, index,
                                                  label="GRAVE_B.PRS")
            if not gate.ok:
                return _port_refusal(f"R1 {gate.reason}",
                                     tuple(gate.details), **ref_fields)
            entry_bytes[index] = gate.entry_bytes
            _len, tag, bones, passes_declared = \
                _mesh_port.mesh_entry_signature(src, index)
            mesh_tag[index] = tag
            mesh_bone_count[index] = bones
            mesh_pass_count[index] = passes_declared
            # THE RETURN SHAPE IS A PAIR: SKA records name BONES, MEA records
            # name MESH NODES. Unpacked, never iterated.
            bone_names, node_names = _mesh_port.mesh_entry_anim_target_names(
                src, index)
            rig_bones[index] = tuple(bone_names)
            rig_nodes[index] = tuple(node_names)
        ref_fields["source_bone_count"] = \
            mesh_bone_count.get(BOSS_SOURCE_MESH_INDEX, 0)

        # Every ported mesh's render passes, from ONE parse of the source.
        src_bound: dict = {}
        src_untextured: dict = {}
        src_reachable: dict = {}
        with _pmp.parser_scope():
            reader = _pmp.Reader(src, MESH_COUNT_OFFSET)
            _pmp.load_mesh_res_table(reader)
            for index in mesh_order:
                passes = _pmp.find_render_passes_under(f"mesh[{index}]@")
                src_bound[index] = {tid for _o, tid, _p in passes if tid >= 0}
                src_untextured[index] = sum(1 for _o, tid, _p in passes
                                            if tid < 0)
                src_reachable[index] = len(passes)
        source_tex_ids = sorted(set().union(*src_bound.values())
                                if src_bound else set())

        # Every anim record in the union, sliced while the source is resident.
        records: dict = {}
        record_tags: dict = {}
        for index, start, stop, tag in table.bounds:
            if index in union_anim:
                records[index] = bytes(src[start:stop])
                record_tags[index] = tag
        absent = sorted(union_anim - set(records))
        if absent:
            return _port_refusal(
                "R1 GRAVE_B.PRS does not carry every anim record a member "
                "references",
                (f"absent source indices {absent} of a {table.count}-record "
                 f"table",), **ref_fields)

        # --- the per-member, per-mesh binding check -----------------------
        tolerated: list = []
        bindings: dict = {}
        for type_id in member_ids:
            cat1, cat2, _c0 = per_member_refs[type_id]
            own_meshes = sorted({r.index for r in cat1})
            for anim_index in sorted({r.index for r in cat2}):
                record = records[anim_index]
                tag = record_tags[anim_index]
                names_for = (rig_bones if tag == _anim_port.TAG_SKA
                             else rig_nodes)
                best = None
                bound_to = None
                for mesh_index in own_meshes:
                    names = names_for[mesh_index]
                    bone_count = mesh_bone_count[mesh_index]
                    ok, _reasons = _anim_port.can_bind_to_rig(
                        record, names, bone_count=bone_count)
                    missing, _tol, _note = _anim_port.unmatched_name_verdict(
                        record, names, bone_count=bone_count)
                    evidence = _anim_port.rig_binding_evidence(
                        record, bone_count=bone_count)
                    if ok:
                        bound_to = mesh_index
                        break
                    if best is None or len(missing) < len(best[1]):
                        best = (mesh_index, tuple(missing), evidence.binding,
                                len(names))
                if bound_to is not None:
                    bindings.setdefault(type_id, []).append(
                        (anim_index, bound_to))
                    continue
                if best is None:                  # no mesh at all to check
                    return _port_refusal(
                        f"R2 member 0x{type_id:02X} anim record {anim_index} "
                        f"has no mesh of its own to bind to", **ref_fields)
                mesh_index, missing, binding, name_count = best
                if (not missing
                        or binding is not _anim_port.RigBinding.BY_NAME):
                    return _port_refusal(
                        f"R1 anim record {anim_index} cannot bind to any "
                        f"single mesh of member 0x{type_id:02X}",
                        (f"checked against mesh(es) {own_meshes} of that "
                         f"member alone (binding is per-Attach_Tree, so the "
                         f"name sets are NEVER unioned across members); "
                         f"record tag 0x{tag:02X} "
                         f"({'SKA, addresses bones' if tag == _anim_port.TAG_SKA else 'MEA, addresses mesh nodes'}), "
                         f"Rig_Binding {binding}, closest mesh "
                         f"{mesh_index} with {name_count} name(s) and "
                         f"{len(missing)} unmatched {list(missing)}",),
                        **ref_fields)
                # SCOPED VANILLA PARITY: the same names are missing from THIS
                # SAME MESH IN THE SOURCE ARCHIVE -- the rig here IS the source
                # rig, byte-for-byte, since the entry is ported unchanged -- so
                # the lookup fails identically in vanilla GRAVE_B. Recorded,
                # not waved through. Dropping the record is NOT safer: the blob
                # carries a reference TO it.
                tolerated.append((type_id, anim_index, mesh_index,
                                  tuple(missing)))
                bindings.setdefault(type_id, []).append(
                    (anim_index, mesh_index))
                emit(f"member 0x{type_id:02X} anim[{anim_index}] (tag "
                     f"0x{tag:02X}) names {len(missing)} target(s) absent from "
                     f"ITS OWN mesh[{mesh_index}] IN THE SOURCE ARCHIVE "
                     f"{list(missing)}; shipped UNCHANGED because that mesh is "
                     f"ported byte-for-byte and the lookup fails identically "
                     f"in vanilla GRAVE_B, and because the blob carries a "
                     f"reference TO this record so dropping it would leave a "
                     f"dangling reference")
        ref_fields["tolerated_anims"] = tuple(tolerated)

        anim_records = [records[i] for i in anim_order]
        mesh_bytes = sum(len(entry_bytes[i]) for i in mesh_order)
        anim_bytes = sum(len(r) for r in anim_records)
        ref_fields["mesh_entry_count"] = len(mesh_order)
        ref_fields["anim_record_count"] = len(anim_records)
    finally:
        del src                                   # one .PRS at a time

    emit(f"GRAVE_B.PRS closure: {len(mesh_order)} distinct mesh entr"
         f"{'y' if len(mesh_order) == 1 else 'ies'} {mesh_order} "
         f"{mesh_bytes:,} B, {len(anim_records)} distinct anim record(s) "
         f"{anim_order} {anim_bytes:,} B, reachable source texture ids "
         f"{source_tex_ids}; derived from {len(member_ids)} INJECTED member(s) "
         + ", ".join(f"0x{t:02X}" for t in member_ids))
    for index in mesh_order:
        emit(f"  source mesh[{index}]: {len(entry_bytes[index]):,} B, tag "
             f"{mesh_tag[index]}, {mesh_bone_count[index]} bone(s), "
             f"{len(rig_nodes[index])} mesh-node name(s), "
             f"{src_reachable[index]} reachable render pass(es) binding "
             f"{sorted(src_bound[index])} with {src_untextured[index]} "
             f"binding -1")

    # --- 2. GRAVE.PRS: gate, then append ----------------------------------
    prs = dst_prs_path.read_bytes()
    vanilla_prs_bytes = len(prs)
    twalk = _mesh_port.walk_mesh_table(prs)
    if twalk.events:
        first = twalk.events[0]
        return _port_refusal(
            f"R1 GRAVE.PRS mesh walk not clean: {len(twalk.events)} resync(s)",
            (f"declared mesh count {twalk.count}, entries walked "
             f"{len(twalk.bounds)}, first resync in mesh[{first[0]}] via "
             f"{first[1]}: {first[2]}",), **ref_fields)
    before = derive_anim_table(prs, twalk.end)
    if not before.ok:
        return _port_refusal(f"GRAVE.PRS: {before.reason}", before.details,
                             **ref_fields)

    mesh_index_map: dict = {}
    for index in mesh_order:
        try:
            prs, new_index = _mesh_port.insert_mesh_entry(prs,
                                                          entry_bytes[index])
        except Exception as exc:
            return _port_refusal(
                f"R1 mesh append failed for source mesh {index}: "
                f"{type(exc).__name__}: {exc}", **ref_fields)
        mesh_index_map[index] = new_index
    ref_fields["mesh_index_map"] = tuple(sorted(mesh_index_map.items()))

    # Re-derive from the GROWN buffer: the inserts moved every anim byte later.
    awalk = _mesh_port.walk_mesh_table(prs)
    if awalk.events or awalk.count != twalk.count + len(mesh_order):
        return _port_refusal(
            "R1 GRAVE.PRS does not walk cleanly after the mesh appends",
            (f"{len(awalk.events)} resync(s), mesh count {awalk.count} "
             f"(expected {twalk.count + len(mesh_order)})",), **ref_fields)
    after_mesh = derive_anim_table(prs, awalk.end)
    if not after_mesh.ok:
        return _port_refusal(
            f"GRAVE.PRS after the mesh appends: {after_mesh.reason}",
            after_mesh.details, **ref_fields)
    if after_mesh.count != before.count:
        return _port_refusal(
            "R1 the mesh appends changed the anim record count",
            (f"{before.count} -> {after_mesh.count}",), **ref_fields)

    # `append_anim_records` locates the table through `find_anim_region`, i.e.
    # through the `TFA\xff` anchor. That anchor is CORRECT for GRAVE.PRS (its
    # first record is MEA, so the signature sits at the table's own start) but
    # correctness is PROVEN HERE rather than assumed, against the walk-derived
    # table this module trusts. A disagreement -- including an appended mesh's
    # bytes happening to contain `TFA\xff` -- REFUSES instead of splicing at a
    # guessed offset.
    try:
        region = _anim_port.find_anim_region(prs)
    except Exception as exc:
        region = None
        region_error = str(exc)
    else:
        region_error = "no TFA\\xff anchor in the buffer"
    if (region is None or region.anchor != after_mesh.walk_end
            or region.count != after_mesh.count
            or region.records_start != after_mesh.records_start
            or region.records_end != after_mesh.records_end):
        found = ("none: " + region_error if region is None else
                 f"anchor {region.anchor}, count {region.count}, records "
                 f"[{region.records_start},{region.records_end})")
        return _port_refusal(
            "R1 the append path's anim region disagrees with the walk-derived "
            "table",
            (f"walk-derived: anchor {after_mesh.walk_end}, count "
             f"{after_mesh.count}, records [{after_mesh.records_start},"
             f"{after_mesh.records_end}); append path found {found}",),
            **ref_fields)

    try:
        prs, anim_indices, _trailer_offset = _anim_port.append_anim_records(
            prs, anim_records)
    except Exception as exc:
        return _port_refusal(f"R1 anim append failed: {type(exc).__name__}: "
                             f"{exc}", **ref_fields)
    if len(anim_indices) != len(anim_records):
        return _port_refusal(
            "R1 the anim append returned the wrong number of indices",
            (f"{len(anim_indices)} for {len(anim_records)} record(s)",),
            **ref_fields)

    anim_index_map = {src_index: anim_indices[position]
                      for position, src_index in enumerate(anim_order)}
    ref_fields["anim_index_map"] = tuple(sorted(anim_index_map.items()))

    # The LIVE post-append counts every rewritten reference is checked against.
    final_walk = _mesh_port.walk_mesh_table(prs)
    if final_walk.events:
        return _port_refusal(
            "R1 GRAVE.PRS does not walk cleanly after the anim append",
            (f"{len(final_walk.events)} resync(s), mesh count "
             f"{final_walk.count}",), **ref_fields)
    final_table = derive_anim_table(prs, final_walk.end)
    if not final_table.ok:
        return _port_refusal(f"GRAVE.PRS after the anim append: "
                             f"{final_table.reason}", final_table.details,
                             **ref_fields)
    live_mesh_count = final_walk.count
    live_anim_count = final_table.count
    if live_anim_count != before.count + len(anim_records):
        return _port_refusal(
            "R1 the live anim count does not match the append",
            (f"{before.count} + {len(anim_records)} != {live_anim_count}",),
            **ref_fields)
    ref_fields["mesh_count"] = live_mesh_count
    ref_fields["anim_count"] = live_anim_count
    emit(f"GRAVE.PRS: mesh {twalk.count} -> {live_mesh_count} "
         + ", ".join(f"{o}->{n}" for o, n in sorted(mesh_index_map.items()))
         + f"; anim {before.count} -> {live_anim_count} "
         + ", ".join(f"{o}->{n}" for o, n in sorted(anim_index_map.items())))

    # --- 3. textures ------------------------------------------------------
    prt = dst_prt_path.read_bytes()
    vanilla_prt_bytes = len(prt)
    try:
        clut_count, tex_count = struct.unpack_from(
            "<II", prt, PRT_CLUT_COUNT_OFFSET)
    except Exception as exc:
        return _port_refusal(f"R13 GRAVE.PRT header unreadable: {exc}",
                             **ref_fields)
    ref_fields["clut_cap"] = _texture_port.CLUT_TABLE_SIZE
    ref_fields["clut_count"] = clut_count
    ref_fields["tex_count"] = tex_count
    clut_before = clut_count
    if clut_count + len(source_tex_ids) > _texture_port.CLUT_TABLE_SIZE:
        return _port_refusal(
            "R13 GRAVE.PRT's CLUT table cannot absorb the full closure's "
            "palettes",
            (f"{clut_count} palette(s) in use of a cap of "
             f"{_texture_port.CLUT_TABLE_SIZE} "
             f"(texture_port.CLUT_TABLE_SIZE), and {len(source_tex_ids)} more "
             f"are needed for the DERIVED reachable set {source_tex_ids} "
             f"(measured: the boss alone needs 3 and the full closure 13)",),
            **ref_fields)

    if source_tex_ids:
        try:
            listed = _texture_port.list_entries(str(src_prt_path),
                                                limit=max(source_tex_ids) + 1)
        except Exception as exc:
            return _port_refusal(f"R12 GRAVE_B.PRT did not walk: "
                                 f"{type(exc).__name__}: {exc}", **ref_fields)
    else:
        listed = []
    for index in source_tex_ids:
        if index >= len(listed):
            return _port_refusal(
                "R12 a reachable closure texture is absent from GRAVE_B.PRT",
                (f"entry {index} of {len(listed)} walked; the reachable set "
                 f"{source_tex_ids} is DERIVED from the render passes of the "
                 f"ported meshes {mesh_order}",), **ref_fields)
        got = listed[index]
        emit(f"GRAVE_B.PRT entry {index}: {got['width']}x{got['height']} fmt "
             f"0x{got['format']:02X} clut {got['clut_index']}")

    tex_remap: dict = {}
    for index in source_tex_ids:
        try:
            tex = _texture_port.extract_texture(str(src_prt_path), index,
                                                "GRAVE_B")
            prt = _texture_port.inject_texture(prt, tex)
        except Exception as exc:
            return _port_refusal(
                f"R13 texture port of GRAVE_B.PRT entry {index} failed: "
                f"{type(exc).__name__}: {exc}", **ref_fields)
        new_clut, new_count = struct.unpack_from("<II", prt,
                                                 PRT_CLUT_COUNT_OFFSET)
        if new_count != tex_count + 1:
            return _port_refusal(
                "R13 the .PRT entry count did not advance by exactly one",
                (f"{tex_count} -> {new_count} after entry {index}",),
                **ref_fields)
        tex_remap[index] = tex_count           # appended: index == old count
        clut_count, tex_count = new_clut, new_count
    ref_fields["clut_count"] = clut_count
    ref_fields["tex_count"] = tex_count
    tex_bytes = len(prt) - vanilla_prt_bytes
    ref_fields["tex_remap"] = tuple(sorted(tex_remap.items()))
    emit(f"GRAVE.PRT: {len(tex_remap)} distinct texture(s) ported "
         f"+{tex_bytes:,} B, entries -> {tex_count}, CLUTs "
         f"{clut_before} -> {clut_count} of "
         f"{_texture_port.CLUT_TABLE_SIZE}, remap "
         + ", ".join(f"{o}->{n}" for o, n in sorted(tex_remap.items())))

    # --- 4. EVERY appended mesh copy's render-pass texture ids ------------
    total_patched = 0
    try:
        with _pmp.parser_scope():
            reader = _pmp.Reader(prs, MESH_COUNT_OFFSET)
            _pmp.load_mesh_res_table(reader)
            for source_index in mesh_order:
                prefix = f"mesh[{mesh_index_map[source_index]}]@"
                appended = _pmp.find_render_passes_under(prefix)
                bound = {tid for _o, tid, _p in appended if tid >= 0}
                if bound - set(tex_remap):
                    return _port_refusal(
                        "R12 an appended mesh binds a texture id the remap "
                        "does not cover",
                        (f"appended {prefix} (source mesh {source_index}) "
                         f"binds {sorted(bound)}, remap covers "
                         f"{sorted(tex_remap)}",), **ref_fields)
                if len(appended) != src_reachable[source_index]:
                    return _port_refusal(
                        "R12 an appended mesh does not expose the source's "
                        "render passes",
                        (f"{len(appended)} reachable at {prefix}, "
                         f"{src_reachable[source_index]} at source mesh "
                         f"{source_index} in GRAVE_B.PRS",), **ref_fields)
                # patch_texture_ids is a pure equal-length overwrite, so the
                # one parse above stays valid across every prefix.
                prs, patched = _pmp.patch_texture_ids(prs, prefix, tex_remap)
                total_patched += len(patched)
    except Exception as exc:
        return _port_refusal(f"R12 render-pass texture rewrite failed: "
                             f"{type(exc).__name__}: {exc}", **ref_fields)

    # Re-read the ids: every bound pass must now name a NEW index, and every
    # `-1` pass must still read -1.
    with _pmp.parser_scope():
        reader = _pmp.Reader(prs, MESH_COUNT_OFFSET)
        _pmp.load_mesh_res_table(reader)
        for source_index in mesh_order:
            prefix = f"mesh[{mesh_index_map[source_index]}]@"
            now = _pmp.find_render_passes_under(prefix)
            now_bound = {tid for _o, tid, _p in now if tid >= 0}
            now_null = sum(1 for _o, tid, _p in now if tid < 0)
            if (now_bound - set(tex_remap.values())
                    or now_null != src_untextured[source_index]):
                return _port_refusal(
                    "R12 the render-pass rewrite did not land",
                    (f"{prefix} (source mesh {source_index}): bound ids now "
                     f"{sorted(now_bound)}, expected a subset of "
                     f"{sorted(tex_remap.values())}; {now_null} pass(es) bind "
                     f"-1, expected {src_untextured[source_index]}",),
                    **ref_fields)
    emit(f"GRAVE.PRS: {total_patched} render-pass texture id field(s) "
         f"rewritten across {len(mesh_order)} appended mesh entr"
         f"{'y' if len(mesh_order) == 1 else 'ies'}; every pass binding -1 "
         f"left alone")

    # --- 5. EVERY injected member's references ---------------------------
    mesh_values = set(mesh_index_map.values())
    anim_values = set(anim_index_map.values())
    out_blobs: dict = {}
    member_ports: list = []
    for type_id in member_ids:
        cat1, cat2, cat0 = per_member_refs[type_id]
        blob = member_blobs[type_id]
        expected: dict = {}
        for ref in cat1:
            if ref.index not in mesh_index_map:
                return _port_refusal(
                    f"R2 member 0x{type_id:02X} names a source mesh absent "
                    f"from the ported set",
                    (f"{ref.describe()} names source mesh {ref.index}, ported "
                     f"set {mesh_order}",), **ref_fields)
            blob = _bef.set_material_ref_index(blob, ref,
                                               mesh_index_map[ref.index])
            expected[ref.idx_offset] = mesh_index_map[ref.index]
        for ref in cat2:
            if ref.index not in anim_index_map:
                return _port_refusal(
                    f"R2 member 0x{type_id:02X} names a source anim record "
                    f"absent from the ported set",
                    (f"{ref.describe()} names source anim {ref.index}, ported "
                     f"set {anim_order}",), **ref_fields)
            blob = _bef.set_material_ref_index(blob, ref,
                                               anim_index_map[ref.index])
            expected[ref.idx_offset] = anim_index_map[ref.index]

        # Prove the rewrite, `_port_enemy`'s discipline: re-parse and compare
        # ref for ref. A flag byte that moved would have changed the category
        # or the global bit and silently repointed the reference at another
        # resource table.
        original = _bef.parse_material_refs(member_blobs[type_id])
        try:
            after = _bef.parse_material_refs(blob)
        except Exception as exc:
            return _port_refusal(
                f"R3 member 0x{type_id:02X}: the rewritten blob's material "
                f"section no longer walks: {exc}", **ref_fields)
        if len(after) != len(original):
            return _port_refusal(
                f"R3 member 0x{type_id:02X}: the rewritten blob parses a "
                f"different reference count",
                (f"{len(after)} after, {len(original)} before",), **ref_fields)
        for was, now_ref in zip(original, after):
            if (now_ref.flag, now_ref.idx_offset, now_ref.rec_type) != \
                    (was.flag, was.idx_offset, was.rec_type):
                return _port_refusal(
                    f"R3 member 0x{type_id:02X}: the rewrite changed more "
                    f"than an index",
                    (f"{was.describe()} became {now_ref.describe()}",),
                    **ref_fields)
            want = expected.get(was.idx_offset, was.index)
            if now_ref.index != want:
                return _port_refusal(
                    f"R3 member 0x{type_id:02X}: a rewritten reference does "
                    f"not read its new index",
                    (f"ref at 0x{was.idx_offset:X} should read {want} but "
                     f"reads {now_ref.index}",), **ref_fields)
            if now_ref.is_global:
                continue
            limit = (live_mesh_count
                     if now_ref.category == _bef.MATERIAL_CATEGORY_MESH
                     else live_anim_count
                     if now_ref.category == _bef.MATERIAL_CATEGORY_ANIM
                     else None)
            if limit is not None and now_ref.index >= limit:
                return _port_refusal(
                    f"R2 member 0x{type_id:02X}: a rewritten reference is out "
                    f"of range of the LIVE counts",
                    (f"{now_ref.describe()} against {live_mesh_count} mesh / "
                     f"{live_anim_count} anim",), **ref_fields)

        # IDENTITY, not bounds: every LOCAL category-1/2 reference must read a
        # MAP VALUE. An index that merely happens to be < the live count is
        # exactly the failure this whole change exists to kill.
        new_mesh_refs = tuple(r.index for r in after
                              if not r.is_global
                              and r.category == _bef.MATERIAL_CATEGORY_MESH)
        new_anim_refs = tuple(r.index for r in after
                              if not r.is_global
                              and r.category == _bef.MATERIAL_CATEGORY_ANIM)
        stray_mesh = sorted(set(new_mesh_refs) - mesh_values)
        stray_anim = sorted(set(new_anim_refs) - anim_values)
        if stray_mesh or stray_anim:
            return _port_refusal(
                f"R15 Identity_Check: member 0x{type_id:02X} carries a LOCAL "
                f"reference that was NOT rewritten through the port maps",
                (f"category-1 indices not a map VALUE: {stray_mesh}; "
                 f"category-2 indices not a map VALUE: {stray_anim}. This is "
                 f"an IDENTITY check, not a bounds check: such an index may "
                 f"well be in range of the live {live_mesh_count} mesh / "
                 f"{live_anim_count} anim counts and still resolve to a "
                 f"structurally unrelated GRAVE.PRS asset",), **ref_fields)

        out_blobs[type_id] = blob
        member_ports.append(MemberPort(
            type_id=type_id,
            source_mesh_refs=tuple(r.index for r in cat1),
            source_anim_refs=tuple(r.index for r in cat2),
            new_mesh_refs=new_mesh_refs, new_anim_refs=new_anim_refs,
            cat0_count=cat0,
            anim_bindings=tuple(bindings.get(type_id, ())),
            identity_ok=True,
            identity_detail=(
                f"{len(new_mesh_refs)} category-1 and {len(new_anim_refs)} "
                f"category-2 LOCAL reference(s), every one equal to a port map "
                f"VALUE; {cat0} category-0 reference(s) untouched")))
        emit(f"member 0x{type_id:02X}: category-1 "
             f"{list(r.index for r in cat1)} -> {list(new_mesh_refs)}, "
             f"category-2 {list(r.index for r in cat2)} -> "
             f"{list(new_anim_refs)}, {cat0} category-0 left untouched")

    ref_fields["members"] = tuple(member_ports)

    # --- 5b. the measured Script_Section spans, BOSS_TYPE ONLY ------------
    # ORDER IS DEFINED AND DELIBERATE: references first, THEN the script fill.
    # The two regions do not overlap -- material references live in
    # [0xD0,0x43A) and the Script_Section in [0x536,0x21CF) -- so either order
    # would produce the same bytes, but "rewrite refs, then neutralize" is the
    # order stated here and the one the static check is written against, so
    # there is no ambiguity about what the emitted blob is. Only the boss blob
    # is touched; no companion's script is ever modified.
    boss_blob, level_exit = patch_boss_script(out_blobs[BOSS_TYPE])
    if not level_exit.ok:
        return _port_refusal(level_exit.reason or "boss script patch refused",
                             level_exit.details,
                             level_exit=level_exit, **ref_fields)
    out_blobs[BOSS_TYPE] = boss_blob
    ref_fields["level_exit"] = level_exit
    if level_exit.applied:
        for span in level_exit.spans:
            emit(f"boss 0x{BOSS_TYPE:02X} script span {span.name!r} PATCHED at "
                 f"Script_Section 0x{span.offset:X}: "
                 f"{span.original.hex(' ')} -> {span.fill.hex(' ')}; removed "
                 f"{span.removed}; added {span.added}")
        emit(f"boss 0x{BOSS_TYPE:02X} script: instructions "
             f"{level_exit.instructions_before} -> "
             f"{level_exit.instructions_after} (derived expectation "
             f"{level_exit.instructions_expected_after}), callapi sites "
             f"{level_exit.callapi_sites_before} -> "
             f"{level_exit.callapi_sites_after}, per-api sites "
             + ", ".join(f"0x{a:02X} {b}->{c}"
                         for a, b, c in level_exit.api_site_counts)
             + "; length-preserving, no offset moved, STRUCTURAL ONLY")
    else:
        emit(f"boss 0x{BOSS_TYPE:02X} Script_Section ships VERBATIM: every span "
             f"switch is off (NEUTRALIZE_LEVEL_EXIT={NEUTRALIZE_LEVEL_EXIT}, "
             f"REMOVE_BOSS_CAMERA_FLAG={REMOVE_BOSS_CAMERA_FLAG}), so "
             f"Early_Exit_Risk is un-mitigated")

    ref_fields["blobs"] = tuple((t, out_blobs[t]) for t in member_ids)

    # --- the pool budget, on LIVE lengths --------------------------------
    resident_total = len(prs) + len(prt)
    ref_fields["resident_total"] = resident_total
    ref_fields["prs_bytes"] = len(prs)
    ref_fields["prt_bytes"] = len(prt)
    if resident_total > budget:
        return _port_refusal(
            "R14 the ported grave archives exceed the RESOURCE pool budget",
            (f"LIVE emitted lengths {len(prs):,} B .PRS + {len(prt):,} B .PRT "
             f"= {resident_total:,} B against a budget of {budget:,} B "
             f"(roster_selection.pool_budget(raised="
             f"{POOL_BUDGET_RAISED})), over by "
             f"{resident_total - budget:,} B",), **ref_fields)

    port_total = mesh_bytes + anim_bytes + tex_bytes
    return PortVerdict(
        ok=True, prs=prs, prt=prt, blob=out_blobs[BOSS_TYPE],
        mesh_bytes=mesh_bytes, anim_bytes=anim_bytes, tex_bytes=tex_bytes,
        **ref_fields,
        details=(f"ported {port_total:,} B = {mesh_bytes:,} B mesh "
                 f"({len(mesh_order)} distinct entr"
                 f"{'y' if len(mesh_order) == 1 else 'ies'}) + "
                 f"{anim_bytes:,} B anim ({len(anim_records)} distinct "
                 f"record(s)) + {tex_bytes:,} B texture ({len(tex_remap)} "
                 f"distinct entr{'y' if len(tex_remap) == 1 else 'ies'}), "
                 f"DERIVED from {len(member_ids)} injected member(s) and "
                 f"deduped",
                 f"source mesh union {mesh_order}, source anim union "
                 f"{anim_order}, source texture union {source_tex_ids}",
                 f"mesh_index_map {sorted(mesh_index_map.items())}, "
                 f"anim_index_map {sorted(anim_index_map.items())}, "
                 f"tex_remap {sorted(tex_remap.items())}",
                 f".PRS {vanilla_prs_bytes:,} -> {len(prs):,} B "
                 f"({len(prs) - vanilla_prs_bytes:+,}), .PRT "
                 f"{vanilla_prt_bytes:,} -> {len(prt):,} B "
                 f"({len(prt) - vanilla_prt_bytes:+,}); live resident total "
                 f"{resident_total:,} B of a {budget:,} B budget "
                 f"({budget - resident_total:,} B spare)",
                 f"CLUTs {clut_before} -> {clut_count} of a "
                 f"{_texture_port.CLUT_TABLE_SIZE} cap "
                 f"(texture_port.CLUT_TABLE_SIZE)",
                 "every INJECTED member's LOCAL references rewritten and "
                 "IDENTITY-checked (each reads a port map VALUE, never a "
                 "source index that happens to be in range): "
                 + ", ".join(
                     f"0x{m.type_id:02X}[{list(m.source_mesh_refs)}->"
                     f"{list(m.new_mesh_refs)}, {list(m.source_anim_refs)}->"
                     f"{list(m.new_anim_refs)}]" for m in member_ports),
                 "anim record(s) shipped UNCHANGED on SCOPED vanilla parity "
                 "(member, record, mesh checked against, missing names): "
                 + (", ".join(f"0x{t:02X}/anim{a}/mesh{m} "
                              f"({', '.join(names)})"
                              for t, a, m, names in tolerated) or "none")))


# ---------------------------------------------------------------------------
# Stage 2 -- Merge_Set closure
# ---------------------------------------------------------------------------

def derive_merge_set(boss_bef, host_bef, *,
                     revision: str = _befscript.REVISION_RETAIL,
                     max_members: int = CLOSURE_MAX_MEMBERS) -> ClosureVerdict:
    """Derive the Merge_Set from the game data. NEVER a hardcoded id list.

    Starts at `0x29`, disassembles with the Encoding_Revision selected
    EXPLICITLY, requires a Clean_Decode, reads the literal operands of API slot
    49 `Spawn`, and recurses into each merged companion. Visit-once dedupe, so a
    cycle terminates instead of refusing. Bounded by `max_members`: exceeding
    the bound REFUSES rather than truncating, because a truncated closure leaves
    a `Spawn` unresolved.
    """
    host_ids = host_bef.type_ids()
    members: list = [ClosureMember(type_id=BOSS_TYPE, depth=0, parent=None,
                                   direct=False)]
    seen = {BOSS_TYPE}
    queue: list = [(BOSS_TYPE, 0)]
    direct: set = set()
    transitive: set = set()

    while queue:
        type_id, depth = queue.pop(0)
        entry = boss_bef.get_entry(type_id)
        if entry is None:
            # Present in the host only (nothing to merge, nothing to walk).
            continue
        report = _companion_port.spawn_edge_report(entry.data, type_id, revision)
        if report.refused:
            return ClosureVerdict(
                ok=False, members=tuple(members),
                reasons=(f"decode refused for type 0x{type_id:02X}",),
                details=(report.reason or "",))
        if report.nonliteral_sites:
            site = report.nonliteral_sites[0]
            return ClosureVerdict(
                ok=False, members=tuple(members),
                reasons=("R5 non-literal (register-sourced) Spawn argument",),
                details=(f"owning type 0x{type_id:02X}, call site "
                         f"@0x{site.call_offset:X}: the closure is not "
                         f"provable, and an unprovable closure ships a boss "
                         f"whose minions may be missing",))
        for spawned in report.literal_edges:
            if spawned in seen:
                continue
            if spawned not in host_ids and boss_bef.get_entry(spawned) is None:
                return ClosureVerdict(
                    ok=False, members=tuple(members),
                    unresolved_ids=(spawned,),
                    reasons=("R4 spawn literal unresolvable",),
                    details=(f"type 0x{spawned:02X} spawned by "
                             f"0x{type_id:02X} is in NEITHER the boss archive "
                             f"nor the host BEF",))
            seen.add(spawned)
            if len(seen) > max_members:
                return ClosureVerdict(
                    ok=False, members=tuple(members),
                    reasons=("R6 closure size bound exceeded",),
                    details=(f"{len(members)} member(s) found, bound is "
                             f"{max_members}, crossed by 0x{spawned:02X} "
                             f"spawned by 0x{type_id:02X}",))
            members.append(ClosureMember(type_id=spawned, depth=depth + 1,
                                         parent=type_id,
                                         direct=(type_id == BOSS_TYPE)))
            if type_id == BOSS_TYPE:
                direct.add(spawned)
            else:
                transitive.add(spawned)
            queue.append((spawned, depth + 1))

    members.sort(key=lambda m: m.type_id)
    return ClosureVerdict(
        ok=True, members=tuple(members),
        direct_ids=tuple(sorted(direct)),
        transitive_ids=tuple(sorted(transitive)),
        details=(f"{len(members)} member(s) derived: "
                 + ", ".join(f"0x{m.type_id:02X}(d{m.depth}"
                             + (f",p=0x{m.parent:02X}" if m.parent is not None
                                else ",root")
                             + ")" for m in members),))


# ---------------------------------------------------------------------------
# Stage 3 -- BEF merge (APPEND-ONLY)
# ---------------------------------------------------------------------------

def _blob_parses(blob: bytes, type_id: int) -> Optional[str]:
    """None if the blob re-parses, else the failure point as a string."""
    if len(blob) < 0x40:
        return f"blob is only {len(blob)} bytes"
    magic = struct.unpack_from("<I", blob, 0x00)[0]
    if magic != 0xDEADC0DE:
        return f"blob magic is 0x{magic:08X}, expected 0xDEADC0DE"
    declared = struct.unpack_from("<I", blob, 0x10)[0]
    if declared != type_id:
        return (f"type id at +0x10 is 0x{declared:X}, expected "
                f"0x{type_id:02X}")
    try:
        _bef.material_section_bounds(blob)
        _bef.parse_material_refs(blob)
    except Exception as exc:
        return f"material section: {exc}"
    try:
        _befscript.script_section(blob)
    except Exception as exc:
        return f"Script_Section extraction: {exc}"
    return None


def merge_bef(host_bef, boss_bef, closure: ClosureVerdict, *,
              mesh_count: Optional[int] = None,
              anim_count: Optional[int] = None,
              blobs: Optional[dict] = None) -> BefMergeVerdict:
    """Append the Merge_Set into the host `.BEF`. APPEND-ONLY, always.

    Every type the host already declares is EXCLUDED, with a `SharedTypeNote`
    recording that the HOST copy was kept. Every staged blob is re-parsed BEFORE
    it enters `injected`, and the EMITTED buffer is re-parsed end to end
    afterwards -- both are REQUIRED, not belt-and-braces:
    `CVmEntityManager::LoadBoundEntityFile` aborts the load of the WHOLE `.BEF`
    if any single entry fails to parse, so a bad merge produces a level that
    will not load.

    No operand rewrite is performed or needed: every merged blob KEEPS ITS
    SOURCE TYPE ID, so the literal `Spawn` operands resolve as shipped.
    `rebuild_with_slot_replacement` and `replace=True` are never used.

    `blobs` supplies REPLACEMENT BYTES for individual Merge_Set members --
    `{type_id: blob}`. `inject_boss` passes `{0x29: port.blob}` so the blob that
    ships is the one whose LOCAL references were repointed at the ported
    indices; merging the SOURCE blob instead is precisely the bug that made the
    boss invisible. A replacement is re-parsed and re-range-checked exactly like
    any other staged blob, and it is still an APPEND: no host entry is touched.
    """
    blobs = dict(blobs or {})
    host_ids = host_bef.type_ids()
    vanilla_count = len(host_bef.entries)
    vanilla_blobs = {e.type_id: e.data for e in host_bef.entries}

    shared_notes = tuple(
        SharedTypeNote(type_id=t, kept="host",
                       why="append-only merge: the host copy is never "
                           "rewritten, so a pre-existing entity cannot be "
                           "silently replaced")
        for t in sorted(set(host_ids) & set(boss_bef.type_ids()))
    )

    injected: list = []
    per_type: list = []
    for member in closure.members:
        if member.type_id in host_ids:
            continue
        entry = boss_bef.get_entry(member.type_id)
        if entry is None:
            return BefMergeVerdict(
                ok=False, vanilla_type_count=vanilla_count,
                shared_types=shared_notes,
                reasons=("R4 spawn literal unresolvable",),
                details=(f"type 0x{member.type_id:02X} is absent from the "
                         f"boss archive",))
        data = blobs.get(member.type_id, entry.data)
        fail = _blob_parses(data, member.type_id)
        if fail is not None:
            return BefMergeVerdict(
                ok=False, vanilla_type_count=vanilla_count,
                shared_types=shared_notes,
                reasons=("R7 staged blob fails re-parse",),
                details=(f"type 0x{member.type_id:02X}: {fail}",))
        injected.append(_bef.BefEntry(type_id=member.type_id,
                                      data_offset=0, data=data))
        per_type.append((member.type_id, len(data)))

    if not injected:
        return BefMergeVerdict(
            ok=False, vanilla_type_count=vanilla_count,
            shared_types=shared_notes,
            reasons=("nothing to merge: the host already declares every "
                     "Merge_Set member",))

    try:
        emitted = host_bef.rebuild_with_injections(injected, replace=False)
    except Exception as exc:
        return BefMergeVerdict(
            ok=False, vanilla_type_count=vanilla_count,
            shared_types=shared_notes,
            reasons=(f"BEF rebuild failed: {exc}",))

    # RE-PARSE THE EMITTED BUFFER. Required -- see the docstring.
    try:
        reparsed = _bef.BefFile.parse(emitted)
    except Exception as exc:
        return BefMergeVerdict(
            ok=False, vanilla_type_count=vanilla_count,
            shared_types=shared_notes,
            reasons=("R8 emitted BEF fails re-parse",),
            details=(str(exc),))

    got_ids = reparsed.type_ids()
    for type_id in sorted(host_ids):
        e = reparsed.get_entry(type_id)
        if e is None:
            return BefMergeVerdict(
                ok=False, vanilla_type_count=vanilla_count,
                shared_types=shared_notes,
                reasons=("R8 emitted BEF fails re-parse",),
                details=(f"pre-existing type 0x{type_id:02X} is missing from "
                         f"the emitted file",))
        if e.data != vanilla_blobs[type_id]:
            return BefMergeVerdict(
                ok=False, vanilla_type_count=vanilla_count,
                shared_types=shared_notes,
                reasons=("R8 emitted BEF fails re-parse",),
                details=(f"pre-existing type 0x{type_id:02X} is NOT "
                         f"byte-identical to vanilla at offset "
                         f"0x{e.data_offset:X}",))

    for type_id, _size in per_type:
        e = reparsed.get_entry(type_id)
        if e is None:
            return BefMergeVerdict(
                ok=False, vanilla_type_count=vanilla_count,
                shared_types=shared_notes,
                reasons=("R8 emitted BEF fails re-parse",),
                details=(f"merged type 0x{type_id:02X} did not resolve",))
        fail = _blob_parses(e.data, type_id)
        if fail is not None:
            return BefMergeVerdict(
                ok=False, vanilla_type_count=vanilla_count,
                shared_types=shared_notes,
                reasons=("R8 emitted BEF fails re-parse",),
                details=(f"merged type 0x{type_id:02X} @0x{e.data_offset:X}: "
                         f"{fail}",))
        if mesh_count is None or anim_count is None:
            continue
        try:
            refs = _bef.parse_material_refs(e.data)
        except Exception as exc:
            return BefMergeVerdict(
                ok=False, vanilla_type_count=vanilla_count,
                shared_types=shared_notes,
                reasons=("R8 emitted BEF fails re-parse",),
                details=(f"merged type 0x{type_id:02X} material refs: {exc}",))
        for r in refs:
            if r.is_global:
                continue
            limit = (mesh_count if r.category == _bef.MATERIAL_CATEGORY_MESH
                     else anim_count if r.category == _bef.MATERIAL_CATEGORY_ANIM
                     else None)
            if limit is not None and r.index >= limit:
                return BefMergeVerdict(
                    ok=False, vanilla_type_count=vanilla_count,
                    shared_types=shared_notes,
                    reasons=("R8 merged material reference did not re-resolve",),
                    details=(f"type 0x{type_id:02X}: {r.describe()} exceeds "
                             f"the live count {limit}",))

    return BefMergeVerdict(
        ok=True, data=emitted,
        size_delta=len(emitted) - len(host_bef.raw),
        per_type_bytes=tuple(per_type),
        emitted_type_count=len(got_ids),
        vanilla_type_count=vanilla_count,
        shared_types=shared_notes,
        details=(f"appended {len(per_type)} type(s), "
                 f"+{len(emitted) - len(host_bef.raw)} B, "
                 f"{len(got_ids)} types emitted vs {vanilla_count} vanilla; "
                 f"every pre-existing blob byte-identical; no operand rewrite "
                 f"(each merged blob keeps its source type id); growth "
                 f"sector/relocation consequence: see "
                 f".kiro/specs/psx-file-growth/",))


# ---------------------------------------------------------------------------
# Stage 4 -- Host_Slot selection and placement
# ---------------------------------------------------------------------------

def _inventory(psx) -> tuple:
    """The FULL `(class_name, type_id, size, count)` inventory of records big
    enough to hold the boss record. Reported on refusal so the measurement is
    in the log rather than in somebody's head."""
    counts: dict = {}
    for r in psx.records:
        if r.size >= BOSS_RECORD_SIZE:
            key = (r.class_name, r.type_id, r.size)
            counts[key] = counts.get(key, 0) + 1
    return tuple((cn, tid, size, n)
                 for (cn, tid, size), n in sorted(counts.items()))


def select_host_slot(psx) -> PlacementVerdict:
    """Pick the record to recycle, by POSITIVE ALLOWLIST. `data` is None here.

    An unrecognised class is never a candidate. A blocklist would admit every
    class nobody thought about, and this codebase has already been surprised by
    `end*` kill groups made of ordinary enemies with no anchor-typed member.
    """
    protected = _items.get_progression_protected_names(psx)
    anchor_names = {r.instance_name for r in psx.records
                    if r.instance_name
                    and r.type_id in _items.PROGRESSION_ANCHOR_TYPES}
    inventory = _inventory(psx)

    excluded: dict = {}
    candidates: list = []
    for r in psx.records:
        if r.class_name not in HOST_SLOT_ALLOWED_CLASSES:
            continue
        name = r.instance_name or ""
        if name in protected:
            excluded["progression_protected_names"] = \
                excluded.get("progression_protected_names", 0) + 1
            continue
        if name in _items.PROTECTED_INSTANCE_NAMES:
            excluded["PROTECTED_INSTANCE_NAMES"] = \
                excluded.get("PROTECTED_INSTANCE_NAMES", 0) + 1
            continue
        if name and name in anchor_names:
            excluded["shares a PROGRESSION_ANCHOR_TYPES instance_name"] = \
                excluded.get("shares a PROGRESSION_ANCHOR_TYPES instance_name",
                             0) + 1
            continue
        if name and _catalog.is_event_tied_enemy(name):
            excluded["is_event_tied_enemy"] = \
                excluded.get("is_event_tied_enemy", 0) + 1
            continue
        if r.type_id == _items.GOLD_KEY_TYPE:
            excluded["GOLD_KEY_TYPE"] = excluded.get("GOLD_KEY_TYPE", 0) + 1
            continue
        if r.size < BOSS_RECORD_SIZE:
            excluded["size < 0x248"] = excluded.get("size < 0x248", 0) + 1
            continue
        candidates.append(r)

    per_class: dict = {}
    for r in psx.records:
        per_class[r.class_name] = per_class.get(r.class_name, 0) + 1

    if not candidates:
        largest = max((r.size for r in psx.records), default=0)
        details = [
            f"{len(psx.records)} record(s) in {psx.path.name if psx.path else '?'}; "
            f"largest record {largest} B; allowlist = "
            f"{sorted(HOST_SLOT_ALLOWED_CLASSES)}",
            "exclusions applied: " + (", ".join(
                f"{k}={v}" for k, v in sorted(excluded.items())) or "none"),
            "FULL inventory of records with size >= 0x248 "
            "(class_name, type_id, size, count):",
        ]
        details += [f"  {cn!r}, 0x{tid:02X}, {size}, x{n}"
                    for cn, tid, size, n in inventory]
        details.append("per-class record counts:")
        details += [f"  {cn!r} x{n}" for cn, n in sorted(per_class.items())]
        reason = ("R10 host slot too small"
                  if largest < BOSS_RECORD_SIZE
                  else "R9 no allowlisted Host_Slot")
        return PlacementVerdict(
            ok=False, candidate_count=0, inventory=inventory,
            reasons=(reason,), details=tuple(details),
            justification="the allowlist is NEVER widened at run time; "
                          "widening requires an explicit, evidence-carrying "
                          "source change")

    candidates.sort(key=lambda r: (r.class_name, r.offset))
    chosen = candidates[0]
    return PlacementVerdict(
        ok=True, candidate_count=len(candidates), inventory=inventory,
        chosen_class=chosen.class_name,
        chosen_instance_name=chosen.instance_name,
        chosen_size=chosen.size,
        victim_offset=chosen.offset,
        victim_instance_id=chosen.instance_id,
        justification=(f"class {chosen.class_name!r} is allowlisted as "
                       f"decorative, the record is {chosen.size} B >= "
                       f"0x248, it carries no progression role, and "
                       f"candidates are sorted by (class_name, offset) so a "
                       f"fixed seed gives a fixed slot"),
        details=(f"{len(candidates)} candidate(s) after allowlisting and "
                 f"exclusions (measured 271 records of size >= 0x248 in "
                 f"G_SUB1.PSX, counted BY SIZE not by class)",))


def place_boss(psx, boss_record) -> PlacementVerdict:
    """Retype the selected Host_Slot into the boss record, IN PLACE.

    The clone comes from `psx.make_record_with_template`, so the victim's own
    position (a record a level designer placed inside reachable geometry is
    strictly more information than a computed ring), its `instance_id` and the
    TRANSLATED `0x0C` trigger volumes all come from the proven helper. The
    256-byte instance-name field at `NAME_BUFFER_SIZE` is then overwritten with
    a FRESH name so the boss joins NO kill group -- afterwards, because the
    helper copies that field from the victim.

    The `.PSX` MAY change size. The boss record is 0x248 B and the Host_Slot is
    at least that, so the records section shrinks or stays equal; the shift is
    handled by `psx._build_replacement_bytes` and is MEASURED AND REPORTED, not
    refused. The record count never changes because the record is retyped in
    place, and the tail is preserved byte-for-byte.
    """
    selection = select_host_slot(psx)
    if not selection.ok:
        return selection

    victim = next((r for r in psx.records
                   if r.offset == selection.victim_offset), None)
    if victim is None:                            # defensive
        return PlacementVerdict(
            ok=False, reasons=("selected victim record vanished",),
            inventory=selection.inventory)

    # R11 belt-and-braces re-check: a selector bug that admits a protected
    # record must not reach an emitted file.
    protected = _items.get_progression_protected_names(psx)
    anchor_names = {r.instance_name for r in psx.records
                    if r.instance_name
                    and r.type_id in _items.PROGRESSION_ANCHOR_TYPES}
    vname = victim.instance_name or ""
    for rule, hit in (
        ("get_progression_protected_names", vname in protected),
        ("PROTECTED_INSTANCE_NAMES", vname in _items.PROTECTED_INSTANCE_NAMES),
        ("shares a PROGRESSION_ANCHOR_TYPES instance_name",
         bool(vname) and vname in anchor_names),
        ("is_event_tied_enemy",
         bool(vname) and _catalog.is_event_tied_enemy(vname)),
        ("GOLD_KEY_TYPE", victim.type_id == _items.GOLD_KEY_TYPE),
        ("class not allowlisted",
         victim.class_name not in HOST_SLOT_ALLOWED_CLASSES),
        ("size < 0x248", victim.size < BOSS_RECORD_SIZE),
    ):
        if hit:
            return PlacementVerdict(
                ok=False, inventory=selection.inventory,
                reasons=("R11 host slot touches progression",),
                details=(f"rule {rule} matched instance name {vname!r}",))

    # Build the clone with the PROVEN helper, not by hand. It keeps the
    # victim's position and instance_id, takes class_name/type_id/prop_count
    # from the boss template, and -- the reason hand-rolling was wrong --
    # TRANSLATES the record's 0x0C trigger-volume properties by the position
    # delta. Without that translation the boss's detection volume would stay in
    # G_BOSS arena coordinates (the Bone_Tower precedent in
    # `psx.make_record_with_template`).
    # WHERE. The recycled Host_Slot is decorative -- `Smoke` instances are pit
    # haze puffs (`pitE_1`, `pit_haze1`) -- so the victim's own coordinate can
    # sit INSIDE A PIT, which is where the boss was previously stranded.
    # `psx.get_player_spawn()` reads the level header's spawn triple, verified
    # across all worlds and, in HUB maps, matching the Level_Column the player
    # stands on: a provably occupied point. Place the boss relative to THAT.
    spawn = psx.get_player_spawn()
    if spawn is not None:
        bx = spawn[0] + BOSS_SPAWN_OFFSET[0]
        by = spawn[1] + BOSS_SPAWN_OFFSET[1]
        bz = spawn[2] + BOSS_SPAWN_OFFSET[2]
        strategy = "player_spawn"
        applied_offset = tuple(BOSS_SPAWN_OFFSET)
        where_detail = (
            f"placed at the player spawn ({spawn[0]:.2f}, {spawn[1]:.2f}, "
            f"{spawn[2]:.2f}) + BOSS_SPAWN_OFFSET {tuple(BOSS_SPAWN_OFFSET)} "
            f"= ({bx:.2f}, {by:.2f}, {bz:.2f})")
    else:
        bx, by, bz = victim.pos_x, victim.pos_y, victim.pos_z
        strategy = "victim_position"
        applied_offset = ()
        where_detail = (
            f"{LOG_PREFIX} player spawn was unreadable, so the decorative "
            f"slot's own position ({bx:.2f}, {by:.2f}, {bz:.2f}) was used "
            f"instead")

    # The DESIRED coordinate must reach `make_record_with_template` as the
    # `keep_position_from` record, NOT be written over the result afterwards:
    # the helper derives (dx,dy,dz) from `keep_position_from.pos_* -
    # template.pos_*` and translates the record's 0x0C trigger volumes by that
    # delta (the Bone_Tower precedent). Overwriting the position floats after
    # the call would leave the detection volume translated to the VICTIM's
    # position -- i.e. down in the pit -- while the boss stood elsewhere. So
    # build a SYNTHETIC victim carrying the final coordinate and pass that in.
    # Everything else (offset, instance_id, class_name, instance_name,
    # prop_count) stays the victim's, because the helper also copies its
    # instance_id and the 0x100..0x200 name region.
    anchor_raw = bytearray(victim.raw)
    struct.pack_into("<3f", anchor_raw, _psx.RECORD_HEADER_SIZE + 0x10,
                     bx, by, bz)
    anchor = _psx.PsxRecord(
        offset=victim.offset, class_name=victim.class_name,
        instance_name=victim.instance_name, type_id=victim.type_id,
        instance_id=victim.instance_id, prop_count=victim.prop_count,
        pos_x=bx, pos_y=by, pos_z=bz, raw=bytes(anchor_raw),
    )

    templated = _psx.make_record_with_template(
        template=boss_record, keep_position_from=anchor,
        new_offset=victim.offset)

    # REQUIRED, and REQUIRED AFTER the call: the helper copies
    # raw[NAME_BUFFER_SIZE:RECORD_HEADER_SIZE] from `keep_position_from`, so the
    # clone inherits the VICTIM's instance name and with it the victim's kill
    # group. Overwrite that 256-byte field with a fresh name so the boss joins
    # NO kill group. class_name / type_id still come from the boss template.
    raw = bytearray(templated.raw)
    new_name = f"GraveDigger_inject_{templated.instance_id}"
    nb = new_name.encode("ascii") + b"\x00"
    raw[_psx.NAME_BUFFER_SIZE:_psx.RECORD_HEADER_SIZE] = \
        nb + b"\x00" * (_psx.NAME_BUFFER_SIZE - len(nb))
    clone = _psx.PsxRecord(
        offset=templated.offset, class_name=templated.class_name,
        instance_name=new_name, type_id=templated.type_id,
        instance_id=templated.instance_id, prop_count=templated.prop_count,
        pos_x=templated.pos_x, pos_y=templated.pos_y, pos_z=templated.pos_z,
        raw=bytes(raw),
    )

    try:
        built = psx._build_replacement_bytes({victim.offset: clone})
    except Exception as exc:
        return PlacementVerdict(
            ok=False, inventory=selection.inventory,
            reasons=(f"PSX assembly failed: {exc}",))

    # A non-zero `total_shift` is the NORMAL, SUPPORTED case: see
    # `psx.PsxFile._build_replacement_bytes`, which rebuilds the records
    # section, preserves the tail byte-for-byte, and patches
    # `post_records_ptr` by `post_records_ptr - total_shift`. The record COUNT
    # field is deliberately left alone, and that is safe here because the record
    # is RETYPED IN PLACE -- none added, none removed. It is measured and
    # reported, never refused. The victim floor (`size >= BOSS_RECORD_SIZE`)
    # means we only ever shrink or stay equal, so the records-section ceiling
    # `_build_replacement_bytes` guards cannot be reached; if it ever were, the
    # `try/except` above turns it into a clean refusal.
    psx_size_delta = len(built.data) - len(psx.raw)

    return PlacementVerdict(
        ok=True, data=bytes(built.data),
        candidate_count=selection.candidate_count,
        inventory=selection.inventory,
        chosen_class=victim.class_name,
        chosen_instance_name=new_name,
        chosen_size=victim.size,
        victim_offset=victim.offset,
        victim_instance_id=victim.instance_id,
        psx_size_delta=psx_size_delta,
        placement_strategy=strategy,
        player_spawn=tuple(spawn) if spawn is not None else None,
        spawn_offset=applied_offset,
        boss_position=(bx, by, bz),
        justification=selection.justification,
        details=(f"boss 0x{boss_record.type_id:02X} placed over "
                 f"{victim.class_name!r} {victim.instance_name!r} "
                 f"@0x{victim.offset:X} ({victim.size} B), whose own position "
                 f"is ({victim.pos_x:.2f}, {victim.pos_y:.2f}, "
                 f"{victim.pos_z:.2f}); strategy {strategy}: {where_detail}; "
                 f"fresh instance name {new_name!r} so it "
                 f"joins no kill group; trigger volumes translated by "
                 f"make_record_with_template; total_shift="
                 f"{built.total_shift}, psx_size_delta={psx_size_delta} "
                 f"({len(built.data)} B out vs {len(psx.raw)} B in), record "
                 f"retyped IN PLACE so the record count is unchanged and the "
                 f"tail is preserved byte-for-byte",
                 "GEOMETRY FIT IS STATICALLY UNVERIFIABLE: the boss may clip, "
                 "float, fall or fail to path here. Only the running game can "
                 "show that.",))


# ---------------------------------------------------------------------------
# Stage 5 -- sound. NEVER refuses the injection.
# ---------------------------------------------------------------------------

def merge_boss_sounds(vanilla_bd: bytes, source_bd: bytes,
                      effect_ids: Sequence = BOSS_SOUND_IDS) -> SoundVerdict:
    """Merge the Boss_Sound_Set into the level bank. A failure SKIPS the merge.

    The ceiling is computed from the VANILLA bank, never from a reclaimed one:
    recomputing after a reclaim shrinks the ceiling by exactly the bytes the
    reclaim freed, so the merge silently no-ops while the reclaimed room is
    counted twice on paper and zero times in fact. `SPU_USABLE_CEILING` and
    `HEADROOM_CONSERVATIVE` are used as they stand and are NEVER raised.

    Over-ceiling or slot-exhausted => skip, one `[cross-world]` line, the boss
    ships SILENT. Sound never refuses the injection.
    """
    ids = tuple(dict.fromkeys(int(i) for i in effect_ids))
    try:
        target = _sound_bank.parse(vanilla_bd)
        source = _sound_bank.parse(source_bd)
    except Exception as exc:
        return SoundVerdict(
            ok=True, silent=True, skipped_ids=ids,
            reasons=(f"sound merge skipped: bank did not parse ({exc})",))

    ceiling = _sound_bank.ceiling_for(vanilla_bd)
    free_slots = _sound_bank.MAX_SFX - target.count
    imports, missing = _sound_bank.imports_from(source, ids, source="BS_GRAVE.BD")
    if not imports:
        return SoundVerdict(
            ok=True, silent=True, skipped_ids=ids, ceiling=ceiling,
            bank_bytes=len(vanilla_bd), entry_count=target.count,
            free_slots=free_slots,
            reasons=("sound merge skipped: the source bank carries none of the "
                     "Boss_Sound_Set ids",),
            details=(f"missing: {missing}",))
    if free_slots <= len(imports):
        return SoundVerdict(
            ok=True, silent=True, skipped_ids=ids, ceiling=ceiling,
            bank_bytes=len(vanilla_bd), entry_count=target.count,
            free_slots=free_slots,
            reasons=(f"sound merge skipped: {target.count} of "
                     f"{_sound_bank.MAX_SFX} entry slots used, only "
                     f"{free_slots} free for {len(imports)} import(s); the "
                     f"boss ships SILENT and the ceiling is not moved",))

    result = _sound_bank.merge(vanilla_bd, imports, ceiling=ceiling)
    merged = tuple(sorted(result.imported_ids))
    skipped = tuple(sorted(set(ids) - set(merged)))
    if not merged:
        return SoundVerdict(
            ok=True, silent=True, skipped_ids=ids, ceiling=ceiling,
            bank_bytes=len(vanilla_bd), entry_count=target.count,
            free_slots=free_slots,
            reasons=("sound merge skipped: nothing fit under the ceiling "
                     "computed from the VANILLA bank; the boss ships SILENT",),
            details=tuple(r.reason for r in result.refusals))

    problems = _sound_bank.validate(result.bank)
    if problems:
        return SoundVerdict(
            ok=True, silent=True, skipped_ids=ids, ceiling=ceiling,
            bank_bytes=len(vanilla_bd), entry_count=target.count,
            free_slots=free_slots,
            reasons=("sound merge skipped: the merged bank did not validate; "
                     "the boss ships SILENT",),
            details=tuple(str(p) for p in problems))

    return SoundVerdict(
        ok=True, data=result.data, merged_ids=merged, skipped_ids=skipped,
        silent=False, bank_bytes=len(result.data),
        entry_count=result.bank.count, ceiling=ceiling,
        free_slots=_sound_bank.MAX_SFX - result.bank.count,
        details=(f"merged {len(merged)}/{len(ids)} Boss_Sound_Set id(s) "
                 f"{merged}; skipped {skipped}; bank {len(result.data):,} B, "
                 f"{result.bank.count} entries, "
                 f"{_sound_bank.MAX_SFX - result.bank.count} slot(s) free "
                 f"under the driver's < {_sound_bank.MAX_SFX} bound; ceiling "
                 f"{ceiling:,} B from the VANILLA bank",))


# ---------------------------------------------------------------------------
# Stage 6 -- static checks
# ---------------------------------------------------------------------------

def run_static_checks(*, emitted_bef: bytes, boss_bef, closure: ClosureVerdict,
                      emitted_psx: bytes, placement: PlacementVerdict,
                      mesh_count: Optional[int] = None,
                      anim_count: Optional[int] = None,
                      injected_ids: Sequence = (),
                      mesh_index_values: Sequence = (),
                      anim_index_values: Sequence = ()) -> tuple:
    """In-memory checks that cannot pass over an empty inspected set."""
    checks: list = []

    def add(name, inspected, ok, detail):
        if not inspected:
            checks.append(StaticCheck(name, "UNDETERMINED", 0,
                                      "inspected set is empty"))
        else:
            checks.append(StaticCheck(name, "PASS" if ok else "FAIL",
                                      inspected, detail))

    try:
        emitted = _bef.BefFile.parse(emitted_bef)
    except Exception as exc:
        return (StaticCheck("emitted BEF re-parses", "FAIL", 1, str(exc)),)

    ids = emitted.type_ids()
    members = [m.type_id for m in closure.members]
    add("every Merge_Set member present in the emitted BEF", len(members),
        all(t in ids for t in members),
        f"{len(members)} member(s) checked against {len(ids)} emitted types")

    # THE SCRIPT SPAN CHECK. With any span switch on, byte-identity is
    # DELIBERATELY false, so the check is made PRECISE rather than weaker: the
    # emitted boss Script_Section must equal the source at EVERY byte except the
    # ENABLED spans, and each enabled span must equal its intended fill exactly.
    # ONE unexpected differing byte anywhere FAILS, and the count and offsets of
    # any unexpected difference are reported. With every switch off, full
    # byte-identity is still asserted.
    spans = enabled_spans()
    allowed_offsets = {i for s in spans for i in range(s.offset, s.stop)}
    script_check_name = (
        "boss Script_Section differs from source ONLY in the enabled patch "
        "span(s) "
        + ", ".join(f"{s.name} [0x{s.offset:X},0x{s.stop:X})" for s in spans)
        if spans else "Verbatim_Script byte-identical")
    src = boss_bef.get_entry(BOSS_TYPE)
    dst = emitted.get_entry(BOSS_TYPE)
    if src is None or dst is None:
        checks.append(StaticCheck(script_check_name, "UNDETERMINED", 0,
                                  "boss blob missing on one side"))
    else:
        try:
            a = _befscript.script_section(src.data)
            b = _befscript.script_section(dst.data)
            if not spans:
                add(script_check_name, len(a), a == b,
                    f"{len(a)} B source vs {len(b)} B emitted")
            elif len(a) != len(b):
                add(script_check_name, len(a), False,
                    f"every span is LENGTH-PRESERVING but the emitted "
                    f"section is {len(b)} B against {len(a)} B source")
            elif len(b) < max(s.stop for s in spans):
                add(script_check_name, len(a), False,
                    f"the section is only {len(b)} B, too short to contain "
                    + ", ".join(f"[0x{s.offset:X},0x{s.stop:X})" for s in spans))
            else:
                differing = [i for i in range(len(a)) if a[i] != b[i]]
                unexpected = [i for i in differing if i not in allowed_offsets]
                per_span = []
                fills_ok = True
                for s in spans:
                    filled = bytes(b[s.offset:s.stop])
                    ok = filled == s.fill
                    fills_ok = fills_ok and ok
                    per_span.append(
                        f"{s.name} [0x{s.offset:X},0x{s.stop:X}) reads "
                        f"{filled.hex(' ')}, expected {s.fill.hex(' ')} "
                        f"({'match' if ok else 'MISMATCH'})")
                add(script_check_name, len(a),
                    not unexpected and fills_ok,
                    f"{len(a)} B source vs {len(b)} B emitted; "
                    f"{len(differing)} byte(s) differ in total, "
                    f"{len(unexpected)} of them OUTSIDE the enabled span(s)"
                    + (" at " + ", ".join(f"0x{i:X}" for i in unexpected[:16])
                       + ("..." if len(unexpected) > 16 else "")
                       if unexpected else "")
                    + "; " + "; ".join(per_span))
        except Exception as exc:
            checks.append(StaticCheck(script_check_name, "FAIL", 1, str(exc)))

    if emitted_psx and placement.victim_offset is not None:
        off = placement.victim_offset
        size = placement.chosen_size or 0
        ok = (len(emitted_psx) >= off + size
              and emitted_psx[off + 0x204] == BOSS_TYPE)
        add("emitted .PSX carries the boss at the victim offset and size", 1,
            ok, f"offset 0x{off:X}, size {size} B")
    else:
        checks.append(StaticCheck(
            "emitted .PSX carries the boss at the victim offset and size",
            "UNDETERMINED", 0, "no emitted .PSX"))

    if mesh_count is None or anim_count is None:
        checks.append(StaticCheck("merged material refs re-resolve",
                                  "UNDETERMINED", 0,
                                  "live .PRS counts not supplied"))
    else:
        inspected = 0
        bad = ""
        for m in members:
            e = emitted.get_entry(m)
            if e is None:
                continue
            try:
                refs = _bef.parse_material_refs(e.data)
            except Exception as exc:
                bad = f"0x{m:02X}: {exc}"
                break
            for r in refs:
                if r.is_global:
                    continue
                inspected += 1
                limit = (mesh_count
                         if r.category == _bef.MATERIAL_CATEGORY_MESH
                         else anim_count
                         if r.category == _bef.MATERIAL_CATEGORY_ANIM else None)
                if limit is not None and r.index >= limit:
                    bad = f"0x{m:02X}: {r.describe()} >= {limit}"
                    break
            if bad:
                break
        add("merged material refs re-resolve", inspected, not bad,
            bad or f"{inspected} local reference(s) in range of {mesh_count} "
                   f"mesh / {anim_count} anim")

    # THE CHECK THAT WOULD HAVE CAUGHT THE BUG. Per INJECTED member, every LOCAL
    # category-1 and category-2 reference in the EMITTED .BEF must equal a port
    # map VALUE. This is an IDENTITY check, NOT a bounds check: the 21 references
    # that shipped wrong were all comfortably inside the live 40 mesh / 75 anim
    # counts, which is exactly why the bounds check above was blind to them. The
    # bounds check is KEPT -- it catches a different failure -- and this one is
    # added alongside it.
    mesh_values = set(mesh_index_values)
    anim_values = set(anim_index_values)
    injected = list(injected_ids)
    if not injected or not (mesh_values or anim_values):
        checks.append(StaticCheck(
            "Identity_Check: every injected member's LOCAL refs read a port "
            "map VALUE (IDENTITY, not bounds)", "UNDETERMINED", 0,
            "no injected member set or no port maps supplied"))
    else:
        inspected = 0
        problems: list = []
        for type_id in injected:
            e = emitted.get_entry(type_id)
            if e is None:
                problems.append(f"0x{type_id:02X}: absent from the emitted BEF")
                continue
            try:
                refs = _bef.parse_material_refs(e.data)
            except Exception as exc:
                problems.append(f"0x{type_id:02X}: {exc}")
                continue
            stray_mesh: list = []
            stray_anim: list = []
            for r in refs:
                if r.is_global:
                    continue
                if r.category == _bef.MATERIAL_CATEGORY_MESH:
                    inspected += 1
                    if r.index not in mesh_values:
                        stray_mesh.append(r.index)
                elif r.category == _bef.MATERIAL_CATEGORY_ANIM:
                    inspected += 1
                    if r.index not in anim_values:
                        stray_anim.append(r.index)
            if stray_mesh or stray_anim:
                problems.append(
                    f"0x{type_id:02X}: category-1 {sorted(set(stray_mesh))} "
                    f"and category-2 {sorted(set(stray_anim))} are NOT port "
                    f"map values (they may still be in range -- that is the "
                    f"point)")
            else:
                problems.append(f"0x{type_id:02X}: OK")
        failed = [p for p in problems if not p.endswith(": OK")]
        add("Identity_Check: every injected member's LOCAL refs read a port "
            "map VALUE (IDENTITY, not bounds)", inspected, not failed,
            f"{inspected} LOCAL reference(s) across {len(injected)} injected "
            f"member(s) against {len(mesh_values)} mesh / {len(anim_values)} "
            f"anim map value(s); per member: " + "; ".join(problems))

    return tuple(checks)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def _emit(log, message: str) -> None:
    line = f"{LOG_PREFIX} {message}"
    if log is None:
        print(line)
    else:
        log(line)


def _refused(log, target_name, reason, details=(), **stages) -> InjectionReport:
    _emit(log, f"boss injection REFUSED for {target_name}: {reason}")
    # A refusal must be self-explaining. `log_report` only runs on SUCCESS, so
    # without this the host-slot inventory `select_host_slot` builds precisely
    # for this case never reaches the user.
    for line in details:
        if line:
            _emit(log, f"  {line}")
    return InjectionReport(
        ok=False, reasons=(reason,), details=tuple(details),
        route_status="REFUSED", target_name=target_name,
        outstanding=OUTSTANDING_OBSERVATIONS, risks=KNOWN_RISKS, **stages)


def inject_boss(*, game_files_dir, target_psx, enabled: bool = BOSS_INJECT_ENABLED,
                log=None) -> InjectionReport:
    """Run the six stages. Writes NO file; the caller writes `output/`.

    The first non-ok verdict ends the injection with the vanilla bytes returned
    unchanged and exactly ONE `[cross-world]` line. Sound is a SEPARATE staged
    call, and `port_boss_assets` holds ONE `.PRS` at a time, so no single call
    holds more than four binaries.
    """
    game_files_dir = Path(game_files_dir)
    target_psx = Path(target_psx)
    name = target_psx.name

    if not enabled:
        return InjectionReport(
            ok=False, reasons=("boss injection is off",),
            route_status="REFUSED", target_name=name,
            outstanding=OUTSTANDING_OBSERVATIONS, risks=KNOWN_RISKS)

    boss_bef_path = game_files_dir / "GRAVE_B.BEF"
    host_bef_path = game_files_dir / "GRAVE.BEF"
    boss_psx_path = game_files_dir / "G_BOSS.PSX"
    for p in (game_files_dir / "GRAVE.PRS", game_files_dir / "GRAVE.PRT",
              game_files_dir / "GRAVE_B.PRS", game_files_dir / "GRAVE_B.PRT",
              boss_bef_path, host_bef_path, boss_psx_path, target_psx):
        if not p.exists():
            return _refused(log, name, f"{p.name} is missing")

    # --- Stage 1 + 2: GRAVE_B.BEF, then THE PORT ---------------------------
    try:
        boss_bef = _bef.BefFile.parse(boss_bef_path)
    except Exception as exc:
        return _refused(log, name, f"GRAVE_B.BEF did not parse: {exc}")
    boss_entry = boss_bef.get_entry(BOSS_TYPE)
    if boss_entry is None:
        return _refused(log, name,
                        f"GRAVE_B.BEF declares no type 0x{BOSS_TYPE:02X}")

    try:
        host_bef = _bef.BefFile.parse(host_bef_path)
    except Exception as exc:
        return _refused(log, name, f"GRAVE.BEF did not parse: {exc}")

    # THE CLOSURE COMES FIRST. The port cannot know what to carry until it knows
    # every member that will be injected: deriving the merge set AFTER the port
    # is what left ten members' LOCAL references pointing at GRAVE.PRS assets
    # they have nothing to do with (module docstring). Order is closure -> port
    # -> merge, and it is load-bearing.
    closure = derive_merge_set(boss_bef, host_bef)
    if not closure.ok:
        return _refused(log, name, closure.reason or "closure refused",
                        closure.details, closure=closure)

    host_ids = host_bef.type_ids()
    member_blobs: dict = {}
    for member in closure.members:
        if member.type_id in host_ids:
            continue                  # the HOST copy is kept; nothing injected
        entry = boss_bef.get_entry(member.type_id)
        if entry is None:
            return _refused(log, name, "R4 spawn literal unresolvable",
                            (f"type 0x{member.type_id:02X} is absent from the "
                             f"boss archive",), closure=closure)
        member_blobs[member.type_id] = entry.data
    _emit(log, f"{name}: {len(closure.members)} Merge_Set member(s), "
               f"{len(member_blobs)} of them INJECTED and therefore needing "
               f"their assets ported and their references rewritten: "
               + ", ".join(f"0x{t:02X}" for t in sorted(member_blobs)))

    port = port_boss_assets(game_files_dir, member_blobs, log=log)
    if not port.ok:
        return _refused(log, name, port.reason or "port refused",
                        port.details, port=port, closure=closure)

    # --- Stage 3: BEF merge, carrying the REWRITTEN boss blob --------------
    # EVERY rewritten blob travels, keyed by type id -- not just the boss's.
    merged = merge_bef(host_bef, boss_bef, closure,
                       mesh_count=port.mesh_count,
                       anim_count=port.anim_count,
                       blobs=dict(port.blobs))
    if not merged.ok:
        return _refused(log, name, merged.reason or "BEF merge refused",
                        merged.details, port=port, closure=closure,
                        bef=merged)

    # --- Stage 4: placement -------------------------------------------------
    try:
        boss_psx = _psx.PsxFile.parse(boss_psx_path)
        host_psx = _psx.PsxFile.parse(target_psx)
    except Exception as exc:
        return _refused(log, name, f"a .PSX did not parse: {exc}",
                        port=port, closure=closure, bef=merged)
    boss_records = boss_psx.find_records_by_type(BOSS_TYPE)
    if not boss_records:
        return _refused(log, name,
                        f"G_BOSS.PSX carries no type 0x{BOSS_TYPE:02X} record",
                        port=port, closure=closure, bef=merged)
    placement = place_boss(host_psx, boss_records[0])
    if placement.ok and placement.placement_strategy == "victim_position":
        _emit(log, f"{name}: the player spawn was unreadable, so the "
                   f"decorative host slot's own position was used for the "
                   f"boss instead of the spawn-relative point")
    if not placement.ok:
        return _refused(log, name, placement.reason or "placement refused",
                        placement.details, port=port,
                        closure=closure, bef=merged, placement=placement)

    # --- Stage 5: sound, in its own staged call (never refuses) -------------
    sound = _stage_sounds(game_files_dir)

    # --- Stage 6: static checks --------------------------------------------
    checks = run_static_checks(
        emitted_bef=merged.data, boss_bef=boss_bef, closure=closure,
        emitted_psx=placement.data, placement=placement,
        mesh_count=port.mesh_count, anim_count=port.anim_count,
        injected_ids=[t for t, _b in port.blobs],
        mesh_index_values=[n for _o, n in port.mesh_index_map],
        anim_index_values=[n for _o, n in port.anim_index_map])
    failed = [c for c in checks if c.status == "FAIL"]
    if failed:
        return _refused(log, name,
                        f"static check failed: {failed[0].name}",
                        tuple(c.detail for c in failed),
                        port=port, closure=closure, bef=merged,
                        placement=placement, sound=sound)

    report = InjectionReport(
        ok=True, route_status="CANDIDATE", target_name=name,
        port=port, closure=closure, bef=merged,
        placement=placement, sound=sound, checks=checks,
        outstanding=OUTSTANDING_OBSERVATIONS, risks=KNOWN_RISKS,
        details=("a passing static-check set proves DATA CONSISTENCY and does "
                 "NOT prove in-game behaviour",))
    log_report(report, log=log)
    return report


def _stage_sounds(game_files_dir) -> SoundVerdict:
    """Stage 5 as a separate call: only `BS_GRAVE.BD` + `GRAVE.BD` are held."""
    game_files_dir = Path(game_files_dir)
    src = game_files_dir / "BS_GRAVE.BD"
    dst = game_files_dir / "GRAVE.BD"
    if not src.exists() or not dst.exists():
        return SoundVerdict(
            ok=True, silent=True, skipped_ids=BOSS_SOUND_IDS,
            reasons=("sound merge skipped: BS_GRAVE.BD or GRAVE.BD is missing",))
    return merge_boss_sounds(dst.read_bytes(), src.read_bytes())


def log_report(report: InjectionReport, *, log=None) -> None:
    """`verify_session`-style summary. Every line carries `[cross-world]`."""
    e = lambda m: _emit(log, m)                                    # noqa: E731
    e(f"boss injection report for {report.target_name}: "
      f"route_status={report.route_status}")
    for stage_name, verdict in (("port", report.port),
                                ("closure", report.closure),
                                ("BEF merge", report.bef),
                                ("placement", report.placement),
                                ("sound", report.sound)):
        if verdict is None:
            e(f"  stage {stage_name}: not reached")
            continue
        e(f"  stage {stage_name}: {'ok' if verdict.ok else 'REFUSED'}"
          + (f" -- {verdict.reason}" if verdict.reason else ""))
        for d in verdict.details:
            e(f"    {d}")
    if report.port is not None and report.port.ok:
        p = report.port
        e(f"  port: {p.port_total:,} B = {p.mesh_bytes:,} B mesh "
          f"({p.mesh_entry_count} distinct entries) + {p.anim_bytes:,} B anim "
          f"({p.anim_record_count} distinct records) + {p.tex_bytes:,} B "
          f"texture ({len(p.tex_remap)} distinct entries)")
        e(f"    DEDUPED source unions: mesh {list(p.cat1_refs)}, anim "
          f"{list(p.cat2_refs)}, texture "
          f"{[o for o, _n in p.tex_remap]}")
        e(f"    mesh_index_map {list(p.mesh_index_map)}, anim_index_map "
          f"{list(p.anim_index_map)}, tex_remap {list(p.tex_remap)}")
        for m in p.members:
            e(f"    member 0x{m.type_id:02X}: category-1 "
              f"{list(m.source_mesh_refs)} -> {list(m.new_mesh_refs)}, "
              f"category-2 {list(m.source_anim_refs)} -> "
              f"{list(m.new_anim_refs)}, {m.cat0_count} category-0 untouched; "
              f"anim->mesh bindings (per-Attach_Tree, its OWN meshes only) "
              f"{list(m.anim_bindings)}; Identity_Check "
              f"{'PASS' if m.identity_ok else 'FAIL'} -- {m.identity_detail}")
        le = p.level_exit
        if le is None:
            e("    boss script patch: not reached")
        elif not le.applied:
            e(f"    boss script patch: OFF (NEUTRALIZE_LEVEL_EXIT="
              f"{NEUTRALIZE_LEVEL_EXIT}, STOP_CUTSCENE_ON_DEATH="
              f"{STOP_CUTSCENE_ON_DEATH}, REMOVE_BOSS_CAMERA_FLAG="
              f"{REMOVE_BOSS_CAMERA_FLAG}) -- the Script_Section ships VERBATIM "
              f"and Early_Exit_Risk is un-mitigated")
        else:
            e(f"    boss script patch: {len(le.spans)} span(s) applied "
              f"(NEUTRALIZE_LEVEL_EXIT={NEUTRALIZE_LEVEL_EXIT}, "
              f"STOP_CUTSCENE_ON_DEATH={STOP_CUTSCENE_ON_DEATH}, "
              f"REMOVE_BOSS_CAMERA_FLAG={REMOVE_BOSS_CAMERA_FLAG}); "
              f"instructions {le.instructions_before} -> "
              f"{le.instructions_after} (derived expectation "
              f"{le.instructions_expected_after}); callapi sites "
              f"{le.callapi_sites_before} -> {le.callapi_sites_after}; section "
              f"{le.section_length:,} B unchanged")
            for s in le.spans:
                e(f"      span {s.name!r} @Script_Section 0x{s.offset:X}: "
                  f"{s.original.hex(' ')} -> {s.fill.hex(' ')}; REMOVED "
                  f"{s.removed}; ADDED {s.added}")
            e("      per-api callapi sites: "
              + ", ".join(f"0x{a:02X} {b} -> {c}"
                          for a, b, c in le.api_site_counts))
            for d in le.details:
                e(f"      {d}")
        e(f"    live counts: {p.mesh_count} mesh, {p.anim_count} anim, "
          f"{p.tex_count} texture entries, {p.clut_count}/{p.clut_cap} CLUTs")
        e(f"    live resident total {p.resident_total:,} B of a {p.budget:,} B "
          f"budget ({p.budget - p.resident_total:,} B spare), .PRS "
          f"{p.prs_bytes:,} B + .PRT {p.prt_bytes:,} B")
        for type_id, index, mesh_index, names in p.tolerated_anims:
            e(f"    member 0x{type_id:02X} anim[{index}] ships UNCHANGED with "
              f"{len(names)} name{'' if len(names) == 1 else 's'} "
              f"{list(names)} absent from ITS OWN mesh[{mesh_index}]: that "
              f"mesh is ported byte-for-byte as GRAVE_B ships it and the same "
              f"name is missing from that same mesh IN THE SOURCE ARCHIVE, so "
              f"the lookup fails identically in vanilla -- porting changes "
              f"index numbers, never which names resolve. Dropping the record "
              f"is NOT safer: the blob carries a reference TO it.")
    if report.bef is not None and report.bef.ok:
        e(f"  .BEF size delta: +{report.bef.size_delta} B "
          f"({report.bef.emitted_type_count} types vs "
          f"{report.bef.vanilla_type_count} vanilla)")
        for tid, nbytes in report.bef.per_type_bytes:
            e(f"    merged 0x{tid:02X}: {nbytes} B")
        for note in report.bef.shared_types:
            e(f"    shared type 0x{note.type_id:02X}: kept the "
              f"{note.kept} copy -- {note.why}")
    if report.placement is not None and report.placement.ok:
        e(f"  host slot: {report.placement.chosen_class!r} "
          f"@0x{report.placement.victim_offset:X} "
          f"({report.placement.chosen_size} B), "
          f"psx_size_delta={report.placement.psx_size_delta}")
        p = report.placement
        spawn_txt = ("unreadable" if p.player_spawn is None
                     else "(" + ", ".join(f"{v:.2f}" for v in p.player_spawn) + ")")
        pos_txt = ("unknown" if p.boss_position is None
                   else "(" + ", ".join(f"{v:.2f}" for v in p.boss_position) + ")")
        e(f"    position: strategy={p.placement_strategy}, "
          f"player_spawn={spawn_txt}, offset={p.spawn_offset or '(not applied)'}, "
          f"boss position={pos_txt}")
        e(f"    justification: {report.placement.justification}")
    if report.sound is not None:
        e(f"  sound: {'SILENT (merge skipped)' if report.sound.silent else 'merged'} "
          f"{list(report.sound.merged_ids)}")
    for c in report.checks:
        e(f"  check {c.name}: {c.status} (inspected {c.inspected}) {c.detail}")
    e("  OUTSTANDING in-game observations (only the user can settle these):")
    for o in report.outstanding:
        e(f"    - {o}")
    e("  RISKS:")
    for r in report.risks:
        e(f"    - {r}")
    e("  a passing static-check set proves data consistency, NOT in-game "
      "behaviour; CANDIDATE is the ceiling and CONFIRMED is the user's to give")
